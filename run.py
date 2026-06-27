"""
Launch both the Telegram bot and Flask web server in the same process.

Usage:
  python run.py           # runs both bot + web
  python run.py --bot     # bot only
  python run.py --web     # web only
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
    from app import app
    from config.settings import WEB_HOST, WEB_PORT
    init_db()
    logger.info(f"Web server starting on {WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


def run_bot():
    from bot.main import main
    logger.info("Telegram bot starting…")
    main()


def main():
    args = sys.argv[1:]
    bot_only = "--bot" in args
    web_only = "--web" in args

    if web_only:
        run_web()
        return

    if bot_only:
        run_bot()
        return

    # Both: run web in background thread, bot in foreground
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    logger.info("Web server thread started.")
    run_bot()


if __name__ == "__main__":
    main()
