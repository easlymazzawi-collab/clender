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


def send_backup_to_telegram(zip_path: str) -> bool:
    """Gửi file backup lên topic 'backup' trong forum đích qua bot."""
    from config.settings import BOT_TOKEN, DEST_FORUM_ID
    if not BOT_TOKEN or not DEST_FORUM_ID:
        logger.warning(
            "backup_to_telegram: thiếu BOT_TOKEN/DEST_FORUM_ID — "
            "đặt BACKUP_TO_TELEGRAM=1 và DEST_FORUM_ID trong .env"
        )
        return False
    if not zip_path or not os.path.exists(zip_path):
        logger.warning("backup_to_telegram: file zip không tồn tại")
        return False

    async def _send():
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        tid = await _ensure_backup_topic(bot, DEST_FORUM_ID)
        import datetime as _dt
        cap = f"📦 Backup {_dt.datetime.now().strftime('%d/%m/%Y %H:%M')}"
        kwargs = {
            "chat_id": DEST_FORUM_ID,
            "document": open(zip_path, "rb"),
            "filename": os.path.basename(zip_path),
            "caption": cap,
        }
        if tid:
            kwargs["message_thread_id"] = tid
        try:
            await bot.send_document(**kwargs)
        finally:
            kwargs["document"].close()

    try:
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_send())
        loop.close()
        logger.info("✅ Đã gửi backup lên topic Telegram")
        return True
    except Exception as e:
        logger.error(f"send_backup_to_telegram thất bại: {e}")
        return False


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


_scheduler_started = False


def get_backup_status() -> dict:
    """Trạng thái backup — dùng cho /health và debug."""
    from config.settings import BOT_TOKEN, DEST_FORUM_ID, DB_PATH

    to_tg = os.getenv("BACKUP_TO_TELEGRAM", "0") == "1"
    files = []
    if os.path.isdir(BACKUP_DIR):
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".zip")],
            reverse=True,
        )
    last_file = files[0] if files else None
    last_mtime = None
    if last_file:
        last_mtime = datetime.datetime.fromtimestamp(
            os.path.getmtime(os.path.join(BACKUP_DIR, last_file))
        ).isoformat(timespec="seconds")

    reasons = []
    if not to_tg:
        reasons.append("BACKUP_TO_TELEGRAM=0 — chỉ lưu file local, không gửi Telegram")
    if to_tg and not BOT_TOKEN:
        reasons.append("Thiếu BOT_TOKEN")
    if to_tg and not DEST_FORUM_ID:
        reasons.append("Thiếu DEST_FORUM_ID")

    return {
        "scheduler_on": _scheduler_started,
        "interval_hours": INTERVAL_H,
        "keep_days": KEEP_DAYS,
        "backup_dir": BACKUP_DIR,
        "telegram_enabled": to_tg,
        "telegram_ready": to_tg and bool(BOT_TOKEN and DEST_FORUM_ID),
        "local_file_count": len(files),
        "last_backup_file": last_file,
        "last_backup_at": last_mtime,
        "db_exists": os.path.exists(DB_PATH),
        "skip_reasons": reasons,
    }


def start_backup_scheduler() -> bool:
    """Khởi động thread backup nền (idempotent — gọi an toàn nhiều lần)."""
    global _scheduler_started
    if _scheduler_started:
        return False
    _scheduler_started = True
    to_tg = os.getenv("BACKUP_TO_TELEGRAM", "0") == "1"
    t = threading.Thread(target=_backup_loop, daemon=True, name="backup-scheduler")
    t.start()
    if to_tg:
        logger.info(
            f"📦 Auto-backup bật: mỗi {INTERVAL_H}h → {BACKUP_DIR}/ "
            f"+ gửi Telegram (forum {os.getenv('DEST_FORUM_ID', '?')})"
        )
    else:
        logger.info(
            f"📦 Auto-backup bật: mỗi {INTERVAL_H}h → {BACKUP_DIR}/ "
            f"(chỉ local — đặt BACKUP_TO_TELEGRAM=1 để gửi lên Telegram)"
        )
    return True


def run_backup_now(send_telegram: bool | None = None) -> dict:
    """Chạy backup ngay (thủ công hoặc qua API)."""
    path = create_backup()
    rotate_backups()
    sent = False
    if send_telegram is None:
        send_telegram = os.getenv("BACKUP_TO_TELEGRAM", "0") == "1"
    if send_telegram and path:
        sent = send_backup_to_telegram(path)
    return {
        "ok": bool(path),
        "path": path,
        "sent_telegram": sent,
        "status": get_backup_status(),
    }


if __name__ == "__main__":
    # Chạy backup thủ công:
    #   python utils/backup.py              (theo .env)
    #   python utils/backup.py --telegram   (bắt buộc gửi Telegram)
    import sys
    logging.basicConfig(level=logging.INFO)
    force_tg = "--telegram" in sys.argv
    result = run_backup_now(send_telegram=True if force_tg else None)
    print(f"Backup: {result}")
