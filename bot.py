import telebot
import logging
import time
import threading
from config import TOKEN, SOURCE_USER_ID, DESTINATION_CHAT_ID, SOURCE_TEXT
from shared import data

# تنظیمات لاگ‌گیری
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# ایجاد شیء ربات
bot = telebot.TeleBot(TOKEN)


def add_source_text(caption):
    """اضافه کردن متن پیش‌فرض به کپشن"""
    return (caption or "") + SOURCE_TEXT


def send_media(media_data):
    """تابع برای ارسال رسانه پس از دریافت کپشن یا timeout"""
    time.sleep(25)  # حداکثر 15 ثانیه انتظار

    msg_id = str(media_data['msg_id'])
    if msg_id in data['pending_media']:
        media = data['pending_media'].pop(msg_id)

        caption = add_source_text(media.get('caption', ''))

        try:
            if media['type'] == 'photo':
                bot.send_photo(chat_id=DESTINATION_CHAT_ID,
                               photo=media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'voice':
                bot.send_voice(chat_id=DESTINATION_CHAT_ID,
                               voice=media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'video':
                bot.send_video(chat_id=DESTINATION_CHAT_ID,
                               video=media['file_id'],
                               caption=caption,
                               parse_mode='html')
            elif media['type'] == 'audio':  # مدیریت فایل‌های صوتی عمومی
                bot.send_audio(chat_id=DESTINATION_CHAT_ID,
                               audio=media['file_id'],
                               caption=caption,
                               parse_mode='html')
            data['forward_count'] += 1
            logger.info(f"✅ {media['type']} با کپشن ارسال شد")
        except Exception as e:
            logger.error(f"❌ خطای ارسال {media['type']}: {str(e)}")


@bot.message_handler(
    content_types=['text', 'photo', 'voice', 'video', 'audio'])
def handle_messages(message):
    """مدیریت پیام‌های دریافتی"""
    logger.info(
        f"📩 پیام جدید از {message.from_user.first_name} | نوع: {message.content_type}"
    )

    if message.from_user.id != SOURCE_USER_ID:
        logger.warning("⛔ کاربر مجاز نیست")
        return

    try:
        # پردازش متن ریپلای شده به عنوان کپشن
        if message.content_type == 'text' and message.reply_to_message:
            replied = message.reply_to_message
            replied_msg_id = str(replied.message_id)
            if replied.content_type in [
                    'photo', 'voice', 'video', 'audio'
            ] and replied_msg_id in data['pending_media']:
                data['pending_media'][replied_msg_id]['caption'] = message.text
                logger.info(f"💾 کپشن برای {replied.content_type} ذخیره شد")
                return

        # پردازش رسانه جدید
        if message.content_type in ['photo', 'voice', 'video', 'audio']:
            file_id = None
            if message.content_type == 'photo':
                file_id = message.photo[
                    -1].file_id  # آخرین تصویر (با بالاترین کیفیت)
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

                # شروع تایمر برای ارسال خودکار پس از 15 ثانیه
                threading.Thread(target=send_media,
                                 args=({
                                     'msg_id': message.message_id
                                 }, )).start()
                logger.info(
                    f"📥 {message.content_type} ذخیره شد، منتظر کپشن...")
            else:
                logger.error(f"❌ file_id برای {message.content_type} یافت نشد")

        # پردازش متن ساده
        elif message.content_type == 'text':
            bot.send_message(DESTINATION_CHAT_ID, message.text + SOURCE_TEXT)
            data['forward_count'] += 1
            logger.info("📝 متن ساده ارسال شد")

    except Exception as e:
        logger.error(f"❌ خطا: {str(e)}")


if __name__ == "__main__":
    logger.info("🚀 ربات شروع به کار کرد")
    bot.polling(none_stop=True)
