from flask import Flask, render_template, request, redirect, session
from threading import Thread
import telebot
import logging
import subprocess
from functools import wraps
import os
import time
from config import TOKEN, SOURCE_USER_ID, DESTINATION_CHAT_ID, SOURCE_TEXT, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import uuid # For unique job IDs

# تنظیمات اولیه
app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(timezone="Asia/Tehran") # Timezone can be configured
scheduler.start()

# غیرفعال کردن لاگ‌های Flask
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ساختار داده اشتراکی
data = {
    'stats': {
        'text': 0,
        'photo': 0,
        'voice': 0,
        'video': 0,
        'audio': 0,  # اضافه کردن آمار برای فایل‌های صوتی
        'scheduled': 0, # آمار پیام های زمانبندی شده
        'total': 0
    },
    'pending_media': {}, # Stores media waiting for caption or immediate send
    'scheduled_jobs': {} # Stores info about scheduled jobs {job_id: media_info}
}

# وضعیت ربات
bot_status = {"active": True, "signature": SOURCE_TEXT, "version": "1.3.0"} # Version updated

# تنظیمات لاگ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log"),  # ذخیره لاگ در فایل
        logging.StreamHandler()  # نمایش لاگ در کنسول
    ])
logger = logging.getLogger(__name__)

# ایجاد ربات
bot = telebot.TeleBot(TOKEN)


# --- توابع کمکی ---
def add_source_text(caption):
    return (caption or "") + bot_status["signature"]


def _send_media_action(media_info, job_id=None):
    """Internal function to send media. Can be called directly or by scheduler."""
    caption = add_source_text(media_info.get('caption', ''))
    media_type = media_info['type']
    file_id = media_info['file_id']

    try:
        if media_type == 'photo':
            bot.send_photo(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'voice':
            bot.send_voice(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'video':
            bot.send_video(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'audio':
            bot.send_audio(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'text': # Handling scheduled text messages
            bot.send_message(DESTINATION_CHAT_ID, caption) # Caption already includes signature for text

        data['stats'][media_type] = data['stats'].get(media_type, 0) + 1
        data['stats']['total'] += 1
        logger.info(f"✅ {media_type.capitalize()} ارسال شد (Job ID: {job_id if job_id else 'N/A'})")
        if job_id and job_id in data['scheduled_jobs']:
            del data['scheduled_jobs'][job_id]
            data['stats']['scheduled'] = max(0, data['stats']['scheduled'] -1)

    except Exception as e:
        logger.error(f"❌ خطا در ارسال {media_type}: {e} (Job ID: {job_id if job_id else 'N/A'})")
        if job_id and job_id in data['scheduled_jobs']: # Remove failed job
            del data['scheduled_jobs'][job_id]
            data['stats']['scheduled'] = max(0, data['stats']['scheduled'] -1)


def schedule_media_job(media_info, scheduled_time_dt):
    """Schedules a media sending job."""
    job_id = str(uuid.uuid4())
    try:
        scheduler.add_job(_send_media_action, 'date', run_date=scheduled_time_dt, args=[media_info, job_id], id=job_id)
        data['scheduled_jobs'][job_id] = media_info
        data['stats']['scheduled'] +=1
        logger.info(f"🗓️ {media_info['type']} برای ارسال در {scheduled_time_dt.strftime('%Y-%m-%d %H:%M:%S')} زمانبندی شد. Job ID: {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"❌ خطا در زمانبندی {media_info['type']}: {e}")
        return None

def send_media_after_timeout(msg_id_str):
    """Sends media after a 15-second timeout if no caption/schedule command is given."""
    time.sleep(15) # Original timeout
    if msg_id_str in data['pending_media']:
        media_to_send = data['pending_media'].pop(msg_id_str)
        logger.info(f"⏳ ارسال خودکار {media_to_send['type']} پس از timeout برای msg_id: {msg_id_str}")
        _send_media_action(media_to_send)


# --- هندلرهای ربات ---
@bot.message_handler(commands=['schedule'])
def handle_schedule_command(message):
    if message.from_user.id != SOURCE_USER_ID:
        logger.warning(f"⛔ دسترسی غیرمجاز به دستور /schedule از {message.from_user.first_name}")
        return

    parts = message.text.split(maxsplit=3) # /schedule YYYY-MM-DD HH:MM (optional text for text message)
                                          # /schedule YYYY-MM-DD HH:MM (when replying to media)

    scheduled_item_info = None
    custom_text_to_schedule = None

    # Check if replying to a media message that is pending
    if message.reply_to_message and str(message.reply_to_message.message_id) in data['pending_media']:
        replied_msg_id_str = str(message.reply_to_message.message_id)
        scheduled_item_info = data['pending_media'].pop(replied_msg_id_str)
        logger.info(f"ℹ️ دستور /schedule برای رسانه در حال انتظار دریافت شد (msg_id: {replied_msg_id_str})")
        # Caption for media can be included after time or taken from original media caption
        if len(parts) > 3 : # /schedule YYYY-MM-DD HH:MM Caption for media
             scheduled_item_info['caption'] = parts[3]
        # else: use existing caption if any, or None

    elif message.reply_to_message and message.reply_to_message.content_type == 'text' and len(parts) >= 3 :
        # Scheduling a replied text message
        # /schedule YYYY-MM-DD HH:MM (replying to a text)
        # The replied text becomes the content
        scheduled_item_info = {
            'type': 'text',
            'file_id': None, # Not applicable for text
            'caption': message.reply_to_message.text, # The text to be sent
            'msg_id': message.reply_to_message.message_id # For logging/tracking
        }
        logger.info(f"ℹ️ دستور /schedule برای متن ریپلای شده دریافت شد.")

    elif not message.reply_to_message and len(parts) >= 4 and parts[0].lower() == '/schedule':
        # /schedule YYYY-MM-DD HH:MM Your text message here
        # Scheduling a new text message directly
        custom_text_to_schedule = parts[3]
        scheduled_item_info = {
            'type': 'text',
            'file_id': None,
            'caption': custom_text_to_schedule, # The text to be sent
            'msg_id': message.message_id # For logging/tracking
        }
        logger.info(f"ℹ️ دستور /schedule برای ارسال متن جدید دریافت شد.")

    else:
        bot.reply_to(message, "فرمت دستور صحیح نیست. \nبرای زمانبندی رسانه: به رسانه مورد نظر ریپلای کنید و بنویسید: `/schedule YYYY-MM-DD HH:MM` (کپشن اختیاری بعد از زمان)\nبرای زمانبندی متن جدید: `/schedule YYYY-MM-DD HH:MM متن پیام شما`\nبرای زمانبندی متن ریپلای شده: به متن مورد نظر ریپلای کنید و بنویسید: `/schedule YYYY-MM-DD HH:MM`")
        return

    if not scheduled_item_info:
        bot.reply_to(message, "موردی برای زمانبندی یافت نشد. لطفاً طبق راهنما عمل کنید.")
        return

    try:
        datetime_str = f"{parts[1]} {parts[2]}"
        scheduled_time_dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")

        if scheduled_time_dt < datetime.now(scheduler.timezone):
            bot.reply_to(message, "⚠️ زمان مشخص شده گذشته است. لطفاً یک زمان در آینده انتخاب کنید.")
            # If it was a pending media, put it back, or it will be lost
            if message.reply_to_message and str(message.reply_to_message.message_id) not in data['pending_media'] and scheduled_item_info['type'] != 'text':
                 data['pending_media'][str(message.reply_to_message.message_id)] = scheduled_item_info
            return

        job_id = schedule_media_job(scheduled_item_info, scheduled_time_dt)
        if job_id:
            bot.reply_to(message, f"✅ {scheduled_item_info['type'].capitalize()} برای ارسال در {scheduled_time_dt.strftime('%Y-%m-%d %H:%M:%S')} زمانبندی شد.")
        else:
            bot.reply_to(message, f"❌ خطا در زمانبندی {scheduled_item_info['type']}.")
            # If it was a pending media, put it back
            if message.reply_to_message and str(message.reply_to_message.message_id) not in data['pending_media'] and scheduled_item_info['type'] != 'text':
                 data['pending_media'][str(message.reply_to_message.message_id)] = scheduled_item_info


    except ValueError:
        bot.reply_to(message, "⚠️ فرمت تاریخ یا ساعت نامعتبر است. لطفاً از فرمت `YYYY-MM-DD HH:MM` استفاده کنید.")
        # If it was a pending media, put it back
        if message.reply_to_message and str(message.reply_to_message.message_id) not in data['pending_media'] and scheduled_item_info['type'] != 'text':
             data['pending_media'][str(message.reply_to_message.message_id)] = scheduled_item_info
    except Exception as e:
        logger.error(f"🔥 خطای ناشناخته در پردازش دستور /schedule: {e}")
        bot.reply_to(message, "❌ بروز خطا در پردازش درخواست شما.")
        # If it was a pending media, put it back
        if message.reply_to_message and str(message.reply_to_message.message_id) not in data['pending_media'] and scheduled_item_info['type'] != 'text':
            data['pending_media'][str(message.reply_to_message.message_id)] = scheduled_item_info


@bot.message_handler(content_types=['text', 'photo', 'voice', 'video', 'audio'])
def handle_messages(message):
    if not bot_status['active']:
        logger.warning("⏸️ ربات موقتاً غیرفعال است")
        return

    logger.info(
        f"📩 پیام از {message.from_user.first_name} ({message.from_user.id}) | نوع: {message.content_type} | متن: {message.text if message.text else '[رسانه]'}"
    )

    if message.from_user.id != SOURCE_USER_ID:
        logger.warning(f"⛔ دسترسی غیرمجاز از {message.from_user.first_name} ({message.from_user.id})")
        return

    try:
        # پردازش ریپلای (فقط برای تنظیم کپشن، نه زمانبندی)
        if message.content_type == 'text' and message.reply_to_message:
            replied = message.reply_to_message
            replied_msg_id_str = str(replied.message_id)

            if replied.content_type in ['photo', 'voice', 'video', 'audio'] and replied_msg_id_str in data['pending_media']:
                # This is a caption for a pending media, not a schedule command
                if not message.text.lower().startswith('/schedule'):
                    data['pending_media'][replied_msg_id_str]['caption'] = message.text
                    logger.info(f"💾 کپشن برای {replied.content_type} (msg_id: {replied_msg_id_str}) ذخیره شد.")
                    # The timeout thread for this media is already running.
                    # If user provides caption, it will be used by send_media_after_timeout.
                    return
                # If it starts with /schedule, it will be handled by handle_schedule_command

        # پردازش رسانه (ذخیره برای ارسال فوری با تاخیر یا زمانبندی بعدی)
        if message.content_type in ['photo', 'voice', 'video', 'audio']:
            file_id = None
            if message.content_type == 'photo':
                file_id = message.photo[-1].file_id
            elif message.content_type == 'voice':
                file_id = message.voice.file_id
            elif message.content_type == 'video':
                file_id = message.video.file_id
            elif message.content_type == 'audio':
                file_id = message.audio.file_id

            if file_id:
                media_info = {
                    'type': message.content_type,
                    'file_id': file_id,
                    'caption': message.caption, # Original caption
                    'msg_id': message.message_id # Original message ID
                }
                # Store in pending_media, a thread will try to send it after timeout
                # if no /schedule command (with reply) or caption (with reply) is received.
                data['pending_media'][str(message.message_id)] = media_info

                # Start the timeout thread
                threading.Thread(target=send_media_after_timeout, args=(str(message.message_id),)).start()
                logger.info(f"📥 {message.content_type} (msg_id: {message.message_id}) برای ارسال با تاخیر یا زمانبندی بعدی ذخیره شد.")
            else:
                logger.error(f"❌ file_id برای {message.content_type} (msg_id: {message.message_id}) یافت نشد")

        elif message.content_type == 'text':
            # Text messages are sent immediately unless they are a command
            # Normal text messages (not commands, not replies to set caption)
            if not message.text.startswith('/'):
                 # Send text immediately
                final_text = add_source_text(message.text)
                bot.send_message(DESTINATION_CHAT_ID, final_text)
                data['stats']['text'] += 1
                data['stats']['total'] += 1
                logger.info("📝 متن ارسال شد")

    except Exception as e:
        logger.error(f"🔥 خطا در handle_messages: {e}")


# --- راه‌اندازی سرور مدیریت ---
def login_required(f):

    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)

    return decorated


@app.route("/")
def home():
    return redirect("/dashboard")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form['username'] == ADMIN_USERNAME and request.form[
                'password'] == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect("/dashboard")
        return render_template("login.html", error="اطلاعات ورود نامعتبر")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/dashboard")
@login_required
def dashboard():
    try:
        logs = subprocess.check_output(['tail', '-n', '30',
                                        'bot.log']).decode('utf-8')
    except:
        logs = "لاگ در دسترس نیست"

    bot_status.update({
        'forward_count': data['stats']['total'],
        'logs': logs,
        'message_stats': data['stats'],
        'pending_count': len(data['pending_media']),
        'scheduled_count': data['stats']['scheduled'] # Add scheduled count to dashboard
    })
    return render_template("dashboard.html", status=bot_status)


@app.route("/update-signature", methods=["POST"])
@login_required
def update_signature():
    bot_status["signature"] = "\n\n" + request.form['signature'].strip()
    return redirect("/dashboard")


@app.route("/toggle-bot", methods=["POST"])
@login_required
def toggle_bot():
    bot_status["active"] = not bot_status["active"]
    status = "فعال" if bot_status["active"] else "غیرفعال"
    logger.info(f"🔌 وضعیت ربات تغییر کرد به: {status}")
    return redirect("/dashboard")


def run_bot():
    logger.info("🤖 ربات شروع به کار کرد")
    bot.infinity_polling()


def run_server():
    app.run(host='0.0.0.0', port=8080)

def shutdown_scheduler():
    logger.info("🛑 متوقف کردن scheduler...")
    if scheduler.running:
        scheduler.shutdown()

if __name__ == "__main__":
    import threading
    import atexit

    # Register scheduler shutdown hook
    atexit.register(shutdown_scheduler)

    threading.Thread(target=run_bot, daemon=True).start()
    run_server()
