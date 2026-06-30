"""
Unified launcher — Forum Converter Bot + Web Admin.

QUAN TRỌNG: Cần chạy CẢ HAI thành phần song song:

  Cách 1 — 1 lệnh duy nhất (khuyến nghị):
    python run.py
    → Chạy Bot Telegram + Web server cùng lúc

  Cách 2 — 2 terminal riêng:
    Terminal 1: python run.py --web   (web admin: localhost:5000)
    Terminal 2: python run.py --bot   (Telegram bot — XỬ LÝ DEEP LINK /start TOKEN)

  Xác thực Telethon (chạy 1 lần):
    python run.py --auth

Luồng link chia sẻ:
  Telethon forwarder → lưu album vào DB → tạo t.me/bot?start=TOKEN
  User click link → Telegram gửi /start TOKEN đến Bot
  Bot cần đang CHẠY mới nhận được và gửi media về cho user!
"""

import sys
import time
import threading
import logging
import urllib.request

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
# Suppress Telethon's noisy background update logs (Got difference, etc.)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_web_error: str | None = None


def _start_backup():
    """Backup chạy nền — bật với mọi mode (kể cả --bot)."""
    try:
        from utils.backup import start_backup_scheduler
        start_backup_scheduler()
    except Exception as e:
        logger.warning(f"backup scheduler: {e}")


def run_web():
    global _web_error
    from config.settings import WEB_HOST, WEB_PORT
    try:
        from database.models import init_db
        from forwarder.state import sync_file_sessions_to_db
        from app import app
        init_db()
        sync_file_sessions_to_db()
        logger.info(f"Web server → http://{WEB_HOST}:{WEB_PORT}")
        app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
    except OSError as e:
        _web_error = str(e)
        if "Address already in use" in str(e) or getattr(e, "errno", None) == 98:
            logger.error(
                f"❌ Web server KHÔNG khởi động — cổng {WEB_PORT} đã bị chiếm.\n"
                f"   Kiểm tra: ss -tlnp | grep {WEB_PORT}\n"
                f"   Hoặc đổi WEB_PORT trong .env"
            )
        else:
            logger.error(f"❌ Web server lỗi OSError: {e}")
    except Exception as e:
        _web_error = str(e)
        logger.error(f"❌ Web server crash: {e}", exc_info=True)


def _wait_for_web(timeout: float = 15.0) -> bool:
    """Chờ web server sẵn sàng sau khi start thread."""
    from config.settings import WEB_PORT
    url = f"http://127.0.0.1:{WEB_PORT}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _web_error:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    logger.info("✅ Web server đã sẵn sàng")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    logger.error(
        f"❌ Web server không phản hồi sau {timeout}s — "
        f"kiểm tra WEB_PORT={WEB_PORT} và firewall"
    )
    return False


def run_bot():
    from bot.main import main
    logger.info("Telegram bot starting… (xử lý /start TOKEN cho deep links)")
    main()


def run_auth():
    import asyncio
    import forwarder.auth as _auth_module
    asyncio.run(_auth_module.main())


def main():
    args = sys.argv[1:]

    if "--auth" in args:
        run_auth()
        return

    _start_backup()

    if "--web" in args:
        logger.warning(
            "⚠️  Chỉ chạy web server. Bot Telegram KHÔNG hoạt động!\n"
            "   Deep link t.me/bot?start=TOKEN sẽ KHÔNG được xử lý.\n"
            "   Dùng 'python run.py' để chạy cả hai."
        )
        run_web()
        return

    if "--bot" in args:
        logger.info(
            "Chỉ chạy bot. Web admin tắt — dùng 'python run.py' để bật cả web."
        )
        run_bot()
        return

    # Default: both (web in background thread, bot in foreground)
    logger.info("Khởi động cả Bot + Web server…")
    web_thread = threading.Thread(target=run_web, daemon=True, name="web-server")
    web_thread.start()
    if not _wait_for_web():
        logger.warning(
            "⚠️  Bot vẫn chạy nhưng WEB KHÔNG hoạt động — "
            "forwarder dashboard và /api/* sẽ không truy cập được."
        )
    run_bot()   # Bot runs in main thread (blocks until Ctrl+C)


if __name__ == "__main__":
    main()
