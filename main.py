from flask import Flask, render_template, request, redirect, session
from threading import Thread
import telebot
import logging
import subprocess
from functools import wraps
import os
import time
from config import TOKEN, SOURCE_USER_ID, DESTINATION_CHAT_ID, SOURCE_TEXT, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD

# تنظیمات اولیه
app = Flask(__name__)
app.secret_key = SECRET_KEY

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
        'total': 0
    },
    'pending_media': {}
}

# وضعیت ربات
bot_status = {"active": True, "signature": SOURCE_TEXT, "version": "1.2.0"}

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


def send_media(media_data):
    """ارسال رسانه پس از دریافت کپشن یا timeout"""
    time.sleep(15)

    if str(media_data['msg_id']) in data['pending_media']:
        media = data['pending_media'].pop(str(media_data['msg_id']))
        caption = add_source_text(media.get('caption', ''))

        try:
            if media['type'] == 'photo':
                bot.send_photo(DESTINATION_CHAT_ID,
                               media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'voice':
                bot.send_voice(DESTINATION_CHAT_ID,
                               media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'video':
                bot.send_video(DESTINATION_CHAT_ID,
                               media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'audio':  # مدیریت فایل‌های صوتی عمومی
                bot.send_audio(DESTINATION_CHAT_ID,
                               media['file_id'],
                               caption=caption,
                               parse_mode='html')

            data['stats'][media['type']] += 1
            data['stats']['total'] += 1
            logger.info(f"✅ {media['type']} ارسال شد")
        except Exception as e:
            logger.error(f"❌ خطا در ارسال رسانه: {e}")


# --- هندلرهای ربات ---
@bot.message_handler(
    content_types=['text', 'photo', 'voice', 'video', 'audio'])
def handle_messages(message):
    if not bot_status['active']:
        logger.warning("⏸️ ربات موقتاً غیرفعال است")
        return

    logger.info(
        f"📩 پیام از {message.from_user.first_name} | نوع: {message.content_type}"
    )

    if message.from_user.id != SOURCE_USER_ID:
        logger.warning("⛔ دسترسی غیرمجاز")
        return

    try:
        # پردازش ریپلای
        if message.content_type == 'text' and message.reply_to_message:
            replied = message.reply_to_message
            if replied.content_type in [
                    'photo', 'voice', 'video', 'audio'
            ] and str(replied.message_id) in data['pending_media']:
                data['pending_media'][str(
                    replied.message_id)]['caption'] = message.text
                logger.info(f"💾 کپشن ذخیره شد برای {replied.content_type}")
                return

        # پردازش رسانه
        if message.content_type in ['photo', 'voice', 'video', 'audio']:
            file_id = None
            if message.content_type == 'photo':
                file_id = message.photo[-1].file_id
            elif message.content_type == 'voice':
                file_id = message.voice.file_id
            elif message.content_type == 'video':
                file_id = message.video.file_id
            elif message.content_type == 'audio':  # مدیریت فایل‌های صوتی عمومی
                file_id = message.audio.file_id

            if file_id:
                data['pending_media'][str(message.message_id)] = {
                    'type': message.content_type,
                    'file_id': file_id,
                    'caption':
                    message.caption if hasattr(message, 'caption') else None,
                    'msg_id': message.message_id
                }

                threading.Thread(target=send_media,
                                 args=({
                                     'msg_id': message.message_id
                                 }, )).start()
                logger.info(f"📥 {message.content_type} ذخیره شد")
            else:
                logger.error(f"❌ file_id برای {message.content_type} یافت نشد")

        elif message.content_type == 'text':
            bot.send_message(DESTINATION_CHAT_ID,
                             add_source_text(message.text))
            data['stats']['text'] += 1
            data['stats']['total'] += 1
            logger.info("📝 متن ارسال شد")

    except Exception as e:
        logger.error(f"🔥 خطا: {e}")


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
        'pending_count': len(data['pending_media'])
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


if __name__ == "__main__":
    import threading
    threading.Thread(target=run_bot, daemon=True).start()
    run_server()
