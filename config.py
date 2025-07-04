import os
import secrets

# Generate a secure secret key if not provided
def generate_secret_key():
    return secrets.token_hex(32)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

SOURCE_USER_ID = os.environ.get("SOURCE_USER_ID")
if not SOURCE_USER_ID:
    raise ValueError("SOURCE_USER_ID environment variable is required")
SOURCE_USER_ID = int(SOURCE_USER_ID)

DESTINATION_CHAT_ID = os.environ.get("DESTINATION_CHAT_ID")
if not DESTINATION_CHAT_ID:
    raise ValueError("DESTINATION_CHAT_ID environment variable is required")
DESTINATION_CHAT_ID = int(DESTINATION_CHAT_ID)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = generate_secret_key()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

SOURCE_TEXT = os.getenv("SOURCE_TEXT", "\n\n🆔 @Maryam_Turki🇹🇷")

# Additional security settings
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "3"))
SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "3600"))  # 1 hour
