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
import threading
import logging

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
# Suppress Telethon's noisy background update logs (Got difference, etc.)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def run_web():
    from database.models import init_db
    from forwarder.state import sync_file_sessions_to_db
    from app import app
    from config.settings import WEB_HOST, WEB_PORT
    init_db()
    sync_file_sessions_to_db()
    logger.info(f"Web server → http://localhost:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


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

    if "--web" in args:
        logger.warning(
            "⚠️  Chỉ chạy web server. Bot Telegram KHÔNG hoạt động!\n"
            "   Deep link t.me/bot?start=TOKEN sẽ KHÔNG được xử lý.\n"
            "   Dùng 'python run.py' để chạy cả hai."
        )
        run_web()
        return

    if "--bot" in args:
        run_bot()
        return

    # Default: both (web in background thread, bot in foreground)
    logger.info("Khởi động cả Bot + Web server…")
    web_thread = threading.Thread(target=run_web, daemon=True, name="web-server")
    web_thread.start()
    run_bot()   # Bot runs in main thread (blocks until Ctrl+C)


if __name__ == "__main__":
    main()
