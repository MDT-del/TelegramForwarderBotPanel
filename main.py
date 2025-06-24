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
import jdatetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import atexit

# تنظیمات اولیه
app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(timezone="Asia/Tehran")
if not scheduler.running:
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
        'audio': 0,
        'scheduled': 0,
        'total': 0
    },
    'pending_media': {},
    'scheduled_jobs': {}
}

# وضعیت ربات
bot_status = {"active": True, "signature": SOURCE_TEXT, "version": "1.4.0"} # Version updated for interactive schedule

# تنظیمات لاگ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ])
logger = logging.getLogger(__name__)
telebot_logger = telebot.logger
telebot_logger.setLevel(logging.INFO) # telebot's own logger

# ایجاد ربات
if TOKEN is None:
    logger.error("🚨 توکن ربات یافت نشد. لطفاً متغیر محیطی TELEGRAM_BOT_TOKEN را تنظیم کنید.")
    # exit() # Or handle more gracefully depending on desired behavior when TOKEN is missing
bot = telebot.TeleBot(TOKEN, threaded=True)


# Dictionary to store user's interactive scheduling state
user_schedule_sessions = {} # Key: chat_id, Value: session_data

# --- توابع کمکی ---
def add_source_text(caption_text):
    return (caption_text or "") + bot_status["signature"]

def _send_media_action(media_info, job_id=None):
    caption = add_source_text(media_info.get('caption'))
    media_type = media_info['type']
    file_id = media_info.get('file_id') # Make sure to handle if file_id is None for text
    text_content = media_info.get('text_content')


    try:
        if media_type == 'photo':
            bot.send_photo(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'voice':
            bot.send_voice(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'video':
            bot.send_video(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'audio':
            bot.send_audio(DESTINATION_CHAT_ID, file_id, caption=caption, parse_mode='html')
        elif media_type == 'text':
            # For text, 'caption' here IS the text_content with signature
            bot.send_message(DESTINATION_CHAT_ID, caption) # caption already includes signature for text

        data['stats'][media_type] = data['stats'].get(media_type, 0) + 1
        data['stats']['total'] += 1
        logger.info(f"✅ {media_type.capitalize()} ارسال شد (Job ID: {job_id if job_id else 'N/A'})")
        if job_id and job_id in data['scheduled_jobs']:
            del data['scheduled_jobs'][job_id]
            data['stats']['scheduled'] = max(0, data['stats']['scheduled'] - 1)

    except Exception as e:
        logger.error(f"❌ خطا در ارسال {media_type} (Job ID: {job_id if job_id else 'N/A'}): {e}", exc_info=True)
        if job_id and job_id in data['scheduled_jobs']:
            del data['scheduled_jobs'][job_id]
            data['stats']['scheduled'] = max(0, data['stats']['scheduled'] - 1)

def schedule_media_job(media_info, scheduled_time_dt_aware):
    job_id = str(uuid.uuid4())
    try:
        scheduler.add_job(_send_media_action, 'date', run_date=scheduled_time_dt_aware, args=[media_info, job_id], id=job_id)
        data['scheduled_jobs'][job_id] = media_info # Store basic info for tracking
        data['stats']['scheduled'] += 1
        logger.info(f"🗓️ {media_info['type']} برای ارسال در {scheduled_time_dt_aware.strftime('%Y-%m-%d %H:%M:%S %Z')} زمانبندی شد. Job ID: {job_id}")
        return job_id
    except Exception as e:
        logger.error(f"❌ خطا در زمانبندی {media_info['type']} (Job ID: {job_id}): {e}", exc_info=True)
        return None

def send_media_after_timeout(msg_id_str):
    time.sleep(25)
    if msg_id_str in data['pending_media']:
        pending_item = data['pending_media'][msg_id_str]
        if pending_item.get('interactive_session_active', False):
            logger.info(f"⏳ Timeout برای msg_id: {msg_id_str} لغو شد چون درگیر جلسه تعاملی است یا توسط آن مدیریت شده.")
            return

        media_to_send = data['pending_media'].pop(msg_id_str)
        logger.info(f"⏳ ارسال خودکار {media_to_send['type']} پس از timeout برای msg_id: {msg_id_str}")
        _send_media_action(media_to_send)

# --- Helper function to clean up sessions and pending media ---
def cleanup_session_and_pending_media(chat_id, original_msg_id_to_check=None):
    session_cleaned = False
    pending_media_cleaned = False

    if chat_id in user_schedule_sessions:
        session = user_schedule_sessions[chat_id]
        session_original_msg_id = session.get('original_msg_id')

        if original_msg_id_to_check is None or session_original_msg_id == original_msg_id_to_check:
            del user_schedule_sessions[chat_id]
            session_cleaned = True
            logger.info(f"🗑️ جلسه زمانبندی برای کاربر {chat_id} (پیام اصلی: {session_original_msg_id}) پاک شد.")

            if session_original_msg_id:
                pending_key = str(session_original_msg_id)
                if pending_key in data['pending_media'] and \
                   data['pending_media'][pending_key].get('interactive_session_active', False) and \
                   data['pending_media'][pending_key].get('msg_id') == session_original_msg_id : # Ensure it's the exact item
                    del data['pending_media'][pending_key]
                    pending_media_cleaned = True
                    logger.info(f"🗑️ آیتم {pending_key} از pending_media به دلیل پاک شدن جلسه، حذف شد.")
        elif original_msg_id_to_check:
             logger.warning(f"⚠️ تلاش برای پاک کردن جلسه کاربر {chat_id} اما original_msg_id ({original_msg_id_to_check}) با ID جلسه ({session_original_msg_id}) مطابقت نداشت.")

    # If original_msg_id_to_check is provided, and session was not cleaned (maybe session was for a different msg_id)
    # still try to clean the specific pending_media item if it exists and is marked interactive.
    if original_msg_id_to_check and not pending_media_cleaned:
        pending_key = str(original_msg_id_to_check)
        if pending_key in data['pending_media'] and \
           data['pending_media'][pending_key].get('interactive_session_active', False) and \
           data['pending_media'][pending_key].get('msg_id') == original_msg_id_to_check:
            del data['pending_media'][pending_key]
            logger.info(f"🗑️ آیتم {pending_key} از pending_media (مستقل از جلسه) پاک شد.")


# --- Message Handler ---
@bot.message_handler(content_types=['text', 'photo', 'voice', 'video', 'audio'])
def handle_messages(message):
    if not bot_status['active']:
        logger.warning("⏸️ ربات موقتاً غیرفعال است.")
        return

    logger.debug(f"📩 پیام دریافتی از {message.from_user.first_name} ({message.from_user.id}) | نوع: {message.content_type} | متن: {message.text if message.text else '[رسانه]'}")

    if message.from_user.id != SOURCE_USER_ID:
        logger.warning(f"⛔ دسترسی غیرمجاز از {message.from_user.first_name} ({message.from_user.id})")
        return

    chat_id = message.chat.id

    if message.text and message.text.lower() == '/cancel_schedule':
        if chat_id in user_schedule_sessions:
            logger.info(f"🚫 کاربر {chat_id} دستور /cancel_schedule را در حین جلسه ارسال کرد.")
            handle_cancel_command_in_session(message, user_schedule_sessions[chat_id])
        else:
            bot.reply_to(message, "در حال حاضر جلسه زمانبندی فعالی برای لغو وجود ندارد.")
        return

    if chat_id in user_schedule_sessions:
        current_stage = user_schedule_sessions[chat_id].get('stage')
        expected_next_step_stages = ['awaiting_year', 'awaiting_month', 'awaiting_day',
                                     'awaiting_hour', 'awaiting_minute', 'awaiting_caption']
        if current_stage in expected_next_step_stages:
            logger.debug(f"💬 پیام دریافتی در حین جلسه (مرحله: {current_stage}) برای {chat_id}. منتظر next_step_handler.")
            return
        elif current_stage == 'awaiting_initial_choice':
             logger.info(f"💬 کاربر {chat_id} پیام جدیدی ارسال کرد در حالی که منتظر انتخاب گزینه inline بود. جلسه قبلی (msg_id: {user_schedule_sessions[chat_id].get('original_msg_id')}) لغو می‌شود.")
             cleanup_session_and_pending_media(chat_id, user_schedule_sessions[chat_id].get('original_msg_id'))
    try:
        # --- مدیریت ریپلای برای کپشن (خارج از جلسه تعاملی) ---
        if message.content_type == 'text' and message.reply_to_message:
            replied_msg = message.reply_to_message
            replied_msg_id_str = str(replied_msg.message_id)

            if replied_msg_id_str in data['pending_media']:
                pending_item = data['pending_media'][replied_msg_id_str]
                # Only if interactive session is NOT active for this item, treat as normal caption
                if not pending_item.get('interactive_session_active', False): # Important check
                    if replied_msg.content_type in ['photo', 'voice', 'video', 'audio']:
                        pending_item['caption'] = message.text
                        logger.info(f"💾 کپشن عادی برای {pending_item['type']} (msg_id: {replied_msg_id_str}) ذخیره شد: '{message.text[:30]}...'")
                        try:
                            bot.reply_to(message, "کپشن برای ارسال با تاخیر ثبت شد.")
                        except Exception as e_reply:
                            logger.warning(f"Could not send caption confirmation reply: {e_reply}")
                        return # Crucial: stop processing this message further if it was a caption
                    # else: # Reply to a text message in pending_media (not an interactive session) - currently no specific action
                # else: # interactive_session_active is true, let next_step_handler or other logic handle it
            # else: # Reply to a message not in pending_media, or not relevant to captioning. Fall through.

        # --- ادامه پردازش پیام اگر کپشن نبود ---
        original_msg_id = message.message_id
        original_content_type = message.content_type
        original_file_id = None
        original_caption = None
        text_content = None

        if original_content_type in ['photo', 'voice', 'video', 'audio']:
            if original_content_type == 'photo': original_file_id = message.photo[-1].file_id
            elif original_content_type == 'voice': original_file_id = message.voice.file_id
            elif original_content_type == 'video': original_file_id = message.video.file_id
            elif original_content_type == 'audio': original_file_id = message.audio.file_id
            original_caption = message.caption
        elif original_content_type == 'text':
            text_content = message.text
            if text_content.startswith('/'):
                 logger.info(f"ℹ️ دستور {text_content} توسط handle_messages برای جلسه جدید نادیده گرفته شد.")
                 return
        else:
            logger.warning(f"⚠️ نوع محتوای ناشناخته در handle_messages: {original_content_type}")
            return

        if not original_file_id and original_content_type != 'text':
            logger.error(f"❌ file_id برای {original_content_type} (msg_id: {original_msg_id}) یافت نشد.")
            return

        if chat_id in user_schedule_sessions: # Should have been cleaned if stage was awaiting_initial_choice
            logger.warning(f"⚠️ جلسه زمانبندی قبلی برای کاربر {chat_id} (پیام {user_schedule_sessions[chat_id].get('original_msg_id')}) به دلیل پیام جدید، پاکسازی شد.")
            cleanup_session_and_pending_media(chat_id, user_schedule_sessions[chat_id].get('original_msg_id'))

        pending_media_key = str(original_msg_id)
        media_data_for_session = {
            'type': original_content_type,
            'file_id': original_file_id,
            'caption': original_caption,
            'text_content': text_content,
            'msg_id': original_msg_id,
            'interactive_session_active': True
        }
        data['pending_media'][pending_media_key] = media_data_for_session

        user_schedule_sessions[chat_id] = {
            'stage': 'awaiting_initial_choice',
            'original_msg_id': original_msg_id,
            'original_chat_id': chat_id, # Store chat_id for validation in next_step_handlers
            'media_info': media_data_for_session.copy()
        }
        logger.info(f"➕ جلسه زمانبندی جدید برای {chat_id}, پیام اصلی {original_msg_id} آغاز شد. مرحله: awaiting_initial_choice")

        # Start the timeout thread for this pending media item *immediately*.
        # It will only act if interactive_session_active becomes false or the item is not removed by other flows.
        if original_content_type != 'text': # Timeout primarily for media, text is handled differently or scheduled
            threading.Thread(target=send_media_after_timeout, args=(pending_media_key,)).start()
            logger.debug(f"🧵 ترد send_media_after_timeout برای msg_id: {pending_media_key} شروع شد.")

        markup = InlineKeyboardMarkup(row_width=1)
        btn_schedule_shamsi = InlineKeyboardButton("📅 زمان‌بندی شمسی", callback_data=f"sch_shamsi_start_{original_msg_id}")
        btn_send_delayed = InlineKeyboardButton("⏰ ارسال با تأخیر/کپشن", callback_data=f"sch_send_delayed_{original_msg_id}")
        btn_cancel_op = InlineKeyboardButton("❌ لغو عملیات", callback_data=f"sch_cancel_op_{original_msg_id}")
        markup.add(btn_schedule_shamsi, btn_send_delayed, btn_cancel_op)

        bot.reply_to(message, "چه کاری می‌خواهید با این پیام انجام دهید؟", reply_markup=markup)

    except Exception as e:
        logger.error(f"🔥 خطا در handle_messages (بخش تعاملی جدید): {e}", exc_info=True)
        cleanup_session_and_pending_media(chat_id)
        bot.reply_to(message, "متاسفانه در پردازش پیام شما خطایی رخ داد. لطفاً دوباره تلاش کنید.")

# --- Callback Query Handler ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('sch_'))
def handle_schedule_callbacks(call):
    chat_id = call.message.chat.id
    bot_message_id = call.message.message_id

    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.warning(f"⚠️Could not answer callback query {call.id}: {e}")

    try:
        parts = call.data.split('_')
        action_type = parts[1]
        action_verb = parts[2]
        original_msg_id = int(parts[3])

        session = user_schedule_sessions.get(chat_id)

        if not session or session.get('original_msg_id') != original_msg_id:
            logger.warning(f"⚠️ جلسه نامعتبر/منقضی برای callback {call.data} از {chat_id}. Session: {session}, CB original_msg_id: {original_msg_id}")
            bot.edit_message_text("این گزینه دیگر معتبر نیست.", chat_id, bot_message_id, reply_markup=None)
            cleanup_session_and_pending_media(chat_id, original_msg_id)
            return

        logger.info(f"📞 Callback: {call.data}, Stage: {session.get('stage')}, User: {chat_id}, Original Msg: {original_msg_id}")

        try:
            bot.edit_message_reply_markup(chat_id, bot_message_id, reply_markup=None)
        except Exception as e:
            logger.warning(f"⚠️ نتوانست دکمه‌های inline را ویرایش کند (msg {bot_message_id}): {e}")

        pending_media_key = str(original_msg_id)

        if action_type == "shamsi" and action_verb == "start":
            if session['stage'] == 'awaiting_initial_choice':
                session['stage'] = 'awaiting_year'
                logger.info(f"🔄->{session['stage']} برای {chat_id}, msg {original_msg_id}")
                bot.edit_message_text("📅 **زمان‌بندی شمسی**\nلطفاً سال شمسی را وارد کنید (مثلاً ۱۴۰۳).\nبرای لغو، /cancel_schedule را ارسال کنید.", chat_id, bot_message_id)
                bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_year_step, session)
            else:
                handle_invalid_stage(chat_id, bot_message_id, session, "shamsi_start")

        elif action_type == "send" and action_verb == "delayed":
            bot.edit_message_text(f"درخواست شما برای 'ارسال با تاخیر/کپشن' دریافت شد.", chat_id, bot_message_id)
            if pending_media_key in data['pending_media']:
                data['pending_media'][pending_media_key]['interactive_session_active'] = False
                media_info = data['pending_media'][pending_media_key]

                if media_info['type'] == 'text':
                    logger.info(f"📝 ارسال فوری متن (msg {original_msg_id}) توسط {chat_id} با گزینه 'delayed'.")
                    _send_media_action(media_info)
                    if pending_media_key in data['pending_media']: del data['pending_media'][pending_media_key]
                else:
                    bot.send_message(chat_id, f"آماده برای ارسال {media_info['type']}.\n"
                                             f"با ریپلای به پیام اصلی خودتان، کپشن اضافه کنید.\n"
                                             f"وگرنه پس از ۲۵ ثانیه ارسال می‌شود.")
                logger.info(f"⏰ 'ارسال با تاخیر/کپشن' برای msg {original_msg_id} توسط {chat_id} انتخاب شد.")
            else:
                logger.error(f"خطا: اطلاعات پیام اصلی {pending_media_key} یافت نشد برای send_delayed.")
                bot.send_message(chat_id, "خطا: اطلاعات پیام اصلی یافت نشد.")
            cleanup_session_and_pending_media(chat_id, original_msg_id)

        elif action_type == "cancel" and action_verb == "op":
            bot.edit_message_text("عملیات توسط شما لغو شد.", chat_id, bot_message_id)
            logger.info(f"❌ عملیات برای msg {original_msg_id} توسط {chat_id} (دکمه لغو) لغو شد.")
            cleanup_session_and_pending_media(chat_id, original_msg_id)

    except Exception as e:
        logger.error(f"🔥 خطا در handle_schedule_callbacks: {e}", exc_info=True)
        try:
            bot.edit_message_text("خطایی در پردازش درخواست شما رخ داد.", call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            bot.send_message(call.message.chat.id, "خطایی در پردازش درخواست شما رخ داد.")
        cleanup_session_and_pending_media(call.message.chat.id)


def handle_invalid_stage(chat_id, bot_message_id, session_data, attempted_action=""):
    stage = session_data.get('stage', 'نامشخص')
    original_msg_id = session_data.get('original_msg_id', 'نامشخص')
    logger.warning(f"⚠️ اقدام '{attempted_action}' در وضعیت نامعتبر {stage} برای کاربر {chat_id}, پیام اصلی {original_msg_id}")
    try:
        bot.edit_message_text("خطای داخلی: عملیات در وضعیت فعلی مجاز نیست. لطفاً دوباره تلاش کنید.", chat_id, bot_message_id)
    except Exception: # if editing fails
        bot.send_message(chat_id, "خطای داخلی: عملیات در وضعیت فعلی مجاز نیست. لطفاً دوباره تلاش کنید.")
    cleanup_session_and_pending_media(chat_id, original_msg_id)


def handle_cancel_command_in_session(message, session_data):
    chat_id = message.chat.id
    original_msg_id = session_data.get('original_msg_id')

    logger.info(f"↩️ کاربر {chat_id} دستور /cancel_schedule را در حین جلسه (پیام اصلی: {original_msg_id}) ارسال کرد.")
    bot.reply_to(message, "عملیات زمانبندی فعلی لغو شد.")
    cleanup_session_and_pending_media(chat_id, original_msg_id)

# --- Next Step Handler Functions ---
def process_shamsi_year_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_year' or session_data.get('original_chat_id') != chat_id :
        logger.warning(f"⚠️ process_shamsi_year_step: جلسه نامعتبر یا مرحله ({session_data.get('stage') if session_data else 'N/A'}) مطابقت ندارد. کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id: # Only cleanup if it's this user's session
             cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        # Do not send message if session_data is None or not for this user, to avoid spamming.
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    year_text = message.text.strip()
    try:
        year = int(year_text)
        if not (1300 <= year <= 1500):
            raise ValueError("سال شمسی باید بین ۱۳۰۰ تا ۱۵۰۰ باشد.")

        session_data['s_year'] = year
        session_data['stage'] = 'awaiting_month'
        logger.info(f"🔄 کاربر {chat_id} سال را وارد کرد: {year}. تغییر وضعیت به: {session_data['stage']}")
        bot.reply_to(message, f"سال شمسی: {year}\n👍 اکنون ماه شمسی را وارد کنید (۱-۱۲).\nبرای لغو، /cancel_schedule را ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_month_step, session_data)
    except ValueError as e:
        logger.warning(f"⚠️ ورودی سال نامعتبر '{year_text}' از کاربر {chat_id}: {e}")
        bot.reply_to(message, f"ورودی سال نامعتبر است: {e}. لطفاً یک سال شمسی معتبر (مانند ۱۴۰۳) وارد کنید یا با /cancel_schedule لغو کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_year_step, session_data)

def process_shamsi_month_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_month' or session_data.get('original_chat_id') != chat_id:
        logger.warning(f"⚠️ process_shamsi_month_step: جلسه/مرحله نامعتبر. جلسه: {session_data}, کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id:
             cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    month_text = message.text.strip()
    try:
        month = int(month_text)
        if not (1 <= month <= 12):
            raise ValueError("ماه شمسی باید بین ۱ تا ۱۲ باشد.")

        session_data['s_month'] = month
        session_data['stage'] = 'awaiting_day'
        logger.info(f"🔄 کاربر {chat_id} ماه را وارد کرد: {month}. تغییر وضعیت به: {session_data['stage']}")
        bot.reply_to(message, f"ماه شمسی: {month}\n👍 اکنون روز را وارد کنید (۱-۳۱).\nبرای لغو، /cancel_schedule را ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_day_step, session_data)
    except ValueError as e:
        logger.warning(f"⚠️ ورودی ماه نامعتبر '{month_text}' از کاربر {chat_id}: {e}")
        bot.reply_to(message, f"ورودی ماه نامعتبر است: {e}. لطفاً یک عدد بین ۱ تا ۱۲ وارد کنید یا با /cancel_schedule لغو کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_month_step, session_data)

def process_shamsi_day_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_day' or session_data.get('original_chat_id') != chat_id:
        logger.warning(f"⚠️ process_shamsi_day_step: جلسه/مرحله نامعتبر. جلسه: {session_data}, کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id:
            cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    day_text = message.text.strip()
    try:
        day = int(day_text)
        s_year = session_data['s_year']
        s_month = session_data['s_month']

        jdatetime.date(s_year, s_month, day) # Validate day using jdatetime

        session_data['s_day'] = day
        session_data['stage'] = 'awaiting_hour'
        logger.info(f"🔄 کاربر {chat_id} روز را وارد کرد: {day}. تغییر وضعیت به: {session_data['stage']}")
        bot.reply_to(message, f"روز شمسی: {day}\n👍 اکنون ساعت را وارد کنید (۰۰-۲۳).\nبرای لغو، /cancel_schedule را ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_hour_step, session_data)
    except ValueError as e:
        logger.warning(f"⚠️ ورودی روز نامعتبر '{day_text}' از کاربر {chat_id}: {e}")
        bot.reply_to(message, f"ورودی روز نامعتبر است: {e}. لطفاً یک روز معتبر وارد کنید یا با /cancel_schedule لغو کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_day_step, session_data)


def process_shamsi_hour_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_hour' or session_data.get('original_chat_id') != chat_id:
        logger.warning(f"⚠️ process_shamsi_hour_step: جلسه/مرحله نامعتبر. جلسه: {session_data}, کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id:
            cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    hour_text = message.text.strip()
    try:
        hour = int(hour_text)
        if not (0 <= hour <= 23):
            raise ValueError("ساعت باید بین ۰۰ تا ۲۳ باشد.")

        session_data['s_hour'] = hour
        session_data['stage'] = 'awaiting_minute'
        logger.info(f"🔄 کاربر {chat_id} ساعت را وارد کرد: {hour}. تغییر وضعیت به: {session_data['stage']}")
        bot.reply_to(message, f"ساعت: {hour:02d}\n👍 اکنون دقیقه را وارد کنید (۰۰-۵۹).\nبرای لغو، /cancel_schedule را ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_minute_step, session_data)
    except ValueError as e:
        logger.warning(f"⚠️ ورودی ساعت نامعتبر '{hour_text}' از کاربر {chat_id}: {e}")
        bot.reply_to(message, f"ورودی ساعت نامعتبر است: {e}. لطفاً یک عدد بین ۰۰ تا ۲۳ وارد کنید یا با /cancel_schedule لغو کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_hour_step, session_data)

def process_shamsi_minute_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_minute' or session_data.get('original_chat_id') != chat_id:
        logger.warning(f"⚠️ process_shamsi_minute_step: جلسه/مرحله نامعتبر. جلسه: {session_data}, کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id:
            cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    minute_text = message.text.strip()
    try:
        minute = int(minute_text)
        if not (0 <= minute <= 59):
            raise ValueError("دقیقه باید بین ۰۰ تا ۵۹ باشد.")

        session_data['s_minute'] = minute

        s_year = session_data['s_year']
        s_month = session_data['s_month']
        s_day = session_data['s_day']
        s_hour = session_data['s_hour']

        shamsi_dt = jdatetime.datetime(s_year, s_month, s_day, s_hour, minute)
        gregorian_dt_naive = shamsi_dt.togregorian()

        # Correct way to make naive datetime aware with zoneinfo
        if scheduler.timezone:
            gregorian_dt_aware = gregorian_dt_naive.replace(tzinfo=scheduler.timezone)
            current_aware_time = datetime.now(scheduler.timezone)
        else:
            # Fallback if scheduler.timezone is None (should not happen if initialized correctly)
            gregorian_dt_aware = gregorian_dt_naive
            current_aware_time = datetime.now()
            logger.warning("⚠️ scheduler.timezone تنظیم نشده است. از زمان naive برای مقایسه استفاده می‌شود.")

        # --- Enhanced Debug Logging ---
        logger.info(f"DEBUG: Shamsi Input: {s_year}/{s_month}/{s_day} {s_hour}:{minute}")
        logger.info(f"DEBUG: Gregorian Naive from Shamsi: {gregorian_dt_naive.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"DEBUG: Gregorian Aware (Target): {gregorian_dt_aware.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info(f"DEBUG: Current Aware Time ({scheduler.timezone}): {current_aware_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info(f"DEBUG: Comparison: Is {gregorian_dt_aware.isoformat()} < {current_aware_time.isoformat()} ?")
        # --- End Enhanced Debug Logging ---

        if gregorian_dt_aware < current_aware_time:
            bot.reply_to(message, "⚠️ تاریخ و زمان وارد شده مربوط به گذشته است. لطفاً دوباره از ابتدا سال را وارد کنید.")
            logger.warning(f"⚠️ تاریخ گذشته ({shamsi_dt.strftime('%Y/%m/%d %H:%M')}) توسط {chat_id} انتخاب شد. زمان هدف: {gregorian_dt_aware.strftime('%Y-%m-%d %H:%M:%S %Z')}, زمان فعلی: {current_aware_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            session_data['stage'] = 'awaiting_year'
            bot.send_message(chat_id, "لطفاً سال شمسی را مجدداً وارد کنید (مثلاً ۱۴۰۳):\nبرای لغو، /cancel_schedule را ارسال کنید.")
            bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_year_step, session_data)
            return

        session_data['gregorian_datetime_aware'] = gregorian_dt_aware
        formatted_shamsi_dt = shamsi_dt.strftime("%Y/%m/%d ساعت %H:%M")
        logger.info(f"📅 تاریخ و زمان کامل شمسی ({formatted_shamsi_dt}) برای {chat_id} دریافت شد.")

        media_type = session_data['media_info']['type']
        if media_type != 'text':
            session_data['stage'] = 'awaiting_caption'
            logger.info(f"🔄->{session_data['stage']} برای {chat_id}")
            bot.reply_to(message, f"تاریخ تنظیم شد: {formatted_shamsi_dt}\n"
                                 f"👍 حالا کپشن مورد نظر را برای {media_type} ارسال کنید. "
                                 f"اگر کپشن نمی‌خواهید، کلمه `ندارد` را ارسال کنید.\n"
                                 f"برای لغو، /cancel_schedule را ارسال کنید.")
            bot.register_next_step_handler_by_chat_id(chat_id, process_caption_step, session_data)
        else:
            session_data['media_info']['caption'] = session_data['media_info']['text_content']
            finalize_schedule(message, session_data)

    except ValueError as e:
        logger.warning(f"⚠️ ورودی دقیقه/تاریخ نامعتبر '{minute_text}' از کاربر {chat_id}: {e}")
        bot.reply_to(message, f"ورودی دقیقه یا تاریخ نامعتبر است: {e}. لطفاً از ابتدا سال را وارد کنید یا با /cancel_schedule لغو کنید.")
        session_data['stage'] = 'awaiting_year'
        bot.send_message(chat_id, "خطا در پردازش تاریخ/دقیقه. لطفاً سال شمسی را مجدداً وارد کنید (مثلاً ۱۴۰۳):\nبرای لغو، /cancel_schedule را ارسال کنید.")
        bot.register_next_step_handler_by_chat_id(chat_id, process_shamsi_year_step, session_data)

def process_caption_step(message, session_data):
    chat_id = message.chat.id
    if not session_data or session_data.get('stage') != 'awaiting_caption' or session_data.get('original_chat_id') != chat_id:
        logger.warning(f"⚠️ process_caption_step: جلسه/مرحله نامعتبر. جلسه: {session_data}, کاربر: {chat_id}")
        if session_data and session_data.get('original_chat_id') == chat_id:
            cleanup_session_and_pending_media(chat_id, session_data.get('original_msg_id'))
        return

    if message.text and message.text.lower() == '/cancel_schedule':
        handle_cancel_command_in_session(message, session_data)
        return

    caption_text = message.text
    if caption_text.strip().lower() == 'ندارد':
        session_data['media_info']['caption'] = None
        logger.info(f"💬 کاربر {chat_id} برای رسانه کپشنی انتخاب نکرد ('ندارد').")
    else:
        session_data['media_info']['caption'] = caption_text
        logger.info(f"💬 کپشن '{caption_text[:50]}...' برای {chat_id} دریافت شد.")

    finalize_schedule(message, session_data)

def finalize_schedule(message, session_data):
    chat_id = message.chat.id
    original_msg_id = session_data.get('original_msg_id')
    try:
        media_to_schedule = session_data['media_info']
        gregorian_dt_aware = session_data['gregorian_datetime_aware']

        job_id = schedule_media_job(media_to_schedule, gregorian_dt_aware)

        s_year, s_month, s_day, s_hour, s_minute = session_data['s_year'], session_data['s_month'], session_data['s_day'], session_data['s_hour'], session_data['s_minute']
        shamsi_dt_str_display = f"{s_year}/{s_month:02d}/{s_day:02d} ساعت {s_hour:02d}:{s_minute:02d}"

        if job_id:
            bot.reply_to(message, f"✅ {media_to_schedule['type'].capitalize()} با موفقیت برای تاریخ {shamsi_dt_str_display} زمان‌بندی شد.")
            logger.info(f"✅ {media_to_schedule['type']} برای {chat_id} (پیام اصلی: {original_msg_id}) در {shamsi_dt_str_display} (Job ID: {job_id}) زمانبندی شد.")
        else:
            bot.reply_to(message, f"❌ متاسفانه در زمان‌بندی {media_to_schedule['type']} خطایی رخ داد. لطفاً با ادمین تماس بگیرید.")
            logger.error(f"❌ خطا در ایجاد جاب زمانبندی برای {chat_id} (پیام اصلی: {original_msg_id}) در {shamsi_dt_str_display}.")

    except Exception as e:
        logger.error(f"🔥 خطای نهایی در زمانبندی برای کاربر {chat_id} (پیام اصلی: {original_msg_id}): {e}", exc_info=True)
        bot.reply_to(message, "خطای غیرمنتظره‌ای در هنگام نهایی کردن زمان‌بندی رخ داد. لطفاً با ادمین تماس بگیرید.")
    finally:
        cleanup_session_and_pending_media(chat_id, original_msg_id)

# --- Flask Web Routes ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

@app.route("/")
def home():
    return redirect("/dashboard")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form['username'] == ADMIN_USERNAME and request.form['password'] == ADMIN_PASSWORD:
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
        # Ensure bot.log path is correct if running in Docker vs locally
        log_file_path = "bot.log"
        logs_output = subprocess.check_output(['tail', '-n', '50', log_file_path]).decode('utf-8')
    except Exception as e:
        logger.error(f"Error reading log file for dashboard: {e}")
        logs_output = "لاگ در دسترس نیست یا خطایی در خواندن آن رخ داده است."

    # Update bot_status with latest counts
    bot_status.update({
        'forward_count': data['stats']['total'],
        'logs': logs_output,
        'message_stats': data['stats'], # This now includes 'scheduled'
        'pending_count': len(data['pending_media']), # Media waiting for timeout/caption OR in interactive setup
        'scheduled_count': data['stats']['scheduled'], # Number of active APScheduler jobs
        'active_sessions': len(user_schedule_sessions) # Number of users in interactive scheduling
    })
    return render_template("dashboard.html", status=bot_status)

@app.route("/update-signature", methods=["POST"])
@login_required
def update_signature_route():
    bot_status["signature"] = "\n\n" + request.form['signature'].strip()
    logger.info(f"امضا به‌روزرسانی شد: {bot_status['signature']}")
    return redirect("/dashboard")

@app.route("/toggle-bot", methods=["POST"])
@login_required
def toggle_bot_route():
    bot_status["active"] = not bot_status["active"]
    status_text = "فعال" if bot_status["active"] else "غیرفعال"
    logger.info(f"🔌 وضعیت ربات تغییر کرد به: {status_text}")
    return redirect("/dashboard")

def run_bot_polling():
    logger.info("🤖 ربات شروع به کار کرد (infinity_polling)")
    try:
        bot.infinity_polling(logger_level=logging.INFO, skip_pending=True)
    except Exception as e:
        logger.critical(f"🚨 خطای مرگبار در infinity_polling ربات: {e}", exc_info=True)
        # Potentially restart or alert admin
        # For now, just log and the thread will exit.
        # Consider more robust error handling for production.

def run_flask_server():
    logger.info("🌐 سرور Flask در حال اجرا در http://0.0.0.0:8080")
    app.run(host='0.0.0.0', port=8080, debug=False) # debug=False for production

def shutdown_app_scheduler():
    logger.info("🛑 متوقف کردن APScheduler...")
    if scheduler.running:
        try:
            scheduler.shutdown(wait=True) # wait for jobs to complete
            logger.info("✅ APScheduler متوقف شد.")
        except Exception as e:
            logger.error(f"❌ خطا در متوقف کردن APScheduler: {e}")

if __name__ == "__main__":
    atexit.register(shutdown_app_scheduler)

    flask_thread = Thread(target=run_flask_server, daemon=True)
    bot_thread = Thread(target=run_bot_polling, daemon=True)

    flask_thread.start()
    bot_thread.start()

    # Keep main thread alive to allow daemon threads to run
    # and to catch KeyboardInterrupt for graceful shutdown.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 دریافت دستور KeyboardInterrupt. در حال خاموش کردن...")
    finally:
        # shutdown_app_scheduler() will be called by atexit
        logger.info("👋 برنامه خاتمه یافت.")

# Ensure all handlers are defined before bot.polling() is called if __name__ == "__main__"
# which is handled by placing them before the main block.
