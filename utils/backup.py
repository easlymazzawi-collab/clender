"""
Auto-backup hệ thống — chạy nền, sao lưu mỗi ngày.

Sao lưu:
  - database/forum_bot.db   (toàn bộ token→media, users, settings, sessions)
  - forwarder_state/*.txt   (state file resume clone)
  - forwarder_state/*.json  (session metadata)

KHÔNG backup session_main.session (chứa thông tin đăng nhập — giữ riêng, bí mật).

Cơ chế:
  - Mỗi 24h tạo 1 file zip trong backups/
  - Tên: backup_YYYY-MM-DD_HHMM.zip
  - Tự xóa backup cũ hơn KEEP_DAYS ngày (rotate)
  - Backup DB dùng sqlite backup API (an toàn, không lỗi khi đang ghi)
"""

import os
import time
import zipfile
import sqlite3
import logging
import threading
import datetime

logger = logging.getLogger(__name__)

BACKUP_DIR  = os.getenv("BACKUP_DIR", "backups")
KEEP_DAYS   = int(os.getenv("BACKUP_KEEP_DAYS", "7"))
INTERVAL_H  = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))


def _safe_copy_db(src_db: str, dst_db: str) -> bool:
    """Copy DB an toàn bằng sqlite backup API (không lỗi dù đang ghi)."""
    try:
        if not os.path.exists(src_db):
            return False
        src = sqlite3.connect(src_db)
        dst = sqlite3.connect(dst_db)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return True
    except Exception as e:
        logger.warning(f"backup db: {e}")
        return False


def create_backup() -> str | None:
    """Tạo 1 file backup zip. Trả về đường dẫn file hoặc None."""
    from config.settings import DB_PATH
    from forwarder.state import STATE_DIR

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    zpath = os.path.join(BACKUP_DIR, f"backup_{ts}.zip")

    # 1. Copy DB an toàn ra file tạm
    tmp_db = os.path.join(BACKUP_DIR, "_tmp_db.db")
    has_db = _safe_copy_db(DB_PATH, tmp_db)

    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            if has_db:
                z.write(tmp_db, arcname="forum_bot.db")
            # State files (txt + json), KHÔNG lấy .session
            if os.path.isdir(STATE_DIR):
                for fname in os.listdir(STATE_DIR):
                    if fname.endswith((".txt", ".json")):
                        z.write(os.path.join(STATE_DIR, fname),
                                arcname=f"forwarder_state/{fname}")
        logger.info(f"✅ Backup tạo: {zpath}")
        return zpath
    except Exception as e:
        logger.error(f"create_backup: {e}")
        return None
    finally:
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except Exception:
                pass


def rotate_backups():
    """Xóa backup cũ hơn KEEP_DAYS ngày."""
    if not os.path.isdir(BACKUP_DIR):
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for fname in os.listdir(BACKUP_DIR):
        if fname.startswith("backup_") and fname.endswith(".zip"):
            fpath = os.path.join(BACKUP_DIR, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    logger.info(f"🗑️ Xóa backup cũ: {fname}")
            except Exception:
                pass


async def _ensure_backup_topic(bot, dest_chat_id: int) -> int | None:
    """Tìm hoặc tạo topic tên 'backup' trong forum đích. Trả về topic_id."""
    from database.models import get_setting, set_setting
    # Cache topic id để khỏi tạo lại
    cached = get_setting("backup_topic_id", "")
    if cached.isdigit():
        return int(cached)
    try:
        t = await bot.create_forum_topic(chat_id=dest_chat_id, name="backup")
        tid = t.message_thread_id
        set_setting("backup_topic_id", str(tid))
        return tid
    except Exception as e:
        logger.warning(f"create backup topic: {e}")
        return None


def send_backup_to_telegram(zip_path: str):
    """Gửi file backup lên topic 'backup' trong forum đích qua bot."""
    from config.settings import BOT_TOKEN, DEST_FORUM_ID
    if not BOT_TOKEN or not DEST_FORUM_ID:
        logger.info("backup_to_telegram: thiếu BOT_TOKEN/DEST_FORUM_ID — bỏ qua")
        return
    if not zip_path or not os.path.exists(zip_path):
        return

    async def _send():
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        tid = await _ensure_backup_topic(bot, DEST_FORUM_ID)
        import datetime as _dt
        cap = f"📦 Backup {_dt.datetime.now().strftime('%d/%m/%Y %H:%M')}"
        with open(zip_path, "rb") as f:
            await bot.send_document(
                chat_id=DEST_FORUM_ID,
                message_thread_id=tid,
                document=f,
                filename=os.path.basename(zip_path),
                caption=cap,
            )

    try:
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_send())
        loop.close()
        logger.info("✅ Đã gửi backup lên topic Telegram")
    except Exception as e:
        logger.warning(f"send_backup_to_telegram: {e}")


def _backup_loop():
    """Vòng lặp nền: backup mỗi INTERVAL_H giờ."""
    to_tg = os.getenv("BACKUP_TO_TELEGRAM", "0") == "1"
    time.sleep(60)   # chờ hệ thống ổn định
    while True:
        try:
            path = create_backup()
            rotate_backups()
            if to_tg and path:
                send_backup_to_telegram(path)
        except Exception as e:
            logger.error(f"backup_loop: {e}")
        time.sleep(INTERVAL_H * 3600)


def start_backup_scheduler():
    """Khởi động thread backup nền (gọi 1 lần khi app start)."""
    t = threading.Thread(target=_backup_loop, daemon=True, name="backup-scheduler")
    t.start()
    logger.info(f"📦 Auto-backup bật: mỗi {INTERVAL_H}h, giữ {KEEP_DAYS} ngày → {BACKUP_DIR}/")


if __name__ == "__main__":
    # Chạy backup thủ công 1 lần: python utils/backup.py
    logging.basicConfig(level=logging.INFO)
    path = create_backup()
    rotate_backups()
    print(f"Backup: {path}")
