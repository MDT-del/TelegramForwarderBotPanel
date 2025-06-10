import os

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SOURCE_USER_ID = int(os.environ.get("SOURCE_USER_ID"))
DESTINATION_CHAT_ID = int(os.environ.get("DESTINATION_CHAT_ID"))
SECRET_KEY = os.environ.get("SECRET_KEY", "fallback-secret")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")
SOURCE_TEXT = "\n\n🆔 @Maryam_Turki🇹🇷"
