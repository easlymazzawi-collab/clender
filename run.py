"""
Unified launcher.

Usage:
  python3 run.py           – Bot + Web server (parallel)
  python3 run.py --bot     – Telegram bot only
  python3 run.py --web     – Web server only
  python3 run.py --auth    – Telethon auth helper (one-time setup)
"""

import sys
import threading
import logging

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def run_web():
    from database.models import init_db
    from forwarder.state import sync_file_sessions_to_db
    from app import app
    from config.settings import WEB_HOST, WEB_PORT
    init_db()
    sync_file_sessions_to_db()
    logger.info(f"Web server → http://{WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


def run_bot():
    from bot.main import main
    logger.info("Telegram bot starting…")
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
        run_web()
        return

    if "--bot" in args:
        run_bot()
        return

    # Default: both (web in background thread, bot in foreground)
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    logger.info("Web server thread started.")
    run_bot()


if __name__ == "__main__":
    main()
