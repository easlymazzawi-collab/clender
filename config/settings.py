import os
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram Bot ────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ─── Source / Destination Forums ─────────────────────────────────────────────
SOURCE_FORUM_ID = int(os.getenv("SOURCE_FORUM_ID", "0"))
DEST_FORUM_ID = int(os.getenv("DEST_FORUM_ID", "0"))

# ─── Web Server ───────────────────────────────────────────────────────────────
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "database/forum_bot.db")

# ─── Media Storage ────────────────────────────────────────────────────────────
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

# ─── Clone Settings ───────────────────────────────────────────────────────────
CLONE_DELAY_SECONDS = float(os.getenv("CLONE_DELAY_SECONDS", "1.5"))
THUMBNAIL_QUALITY = int(os.getenv("THUMBNAIL_QUALITY", "85"))

# ─── Link format ──────────────────────────────────────────────────────────────
LINK_CAPTION_TEMPLATE = os.getenv(
    "LINK_CAPTION_TEMPLATE",
    "🔗 Nhấp vào link để xem:\n{url}"
)
