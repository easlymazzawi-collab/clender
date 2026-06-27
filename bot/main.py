"""
Entry-point for the Telegram bot.
Run: python bot/main.py
"""
import logging
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from config.settings import BOT_TOKEN
from database.models import init_db
from bot.handlers import (
    cmd_start, cmd_help, cmd_share, cmd_forward, cmd_fwd_anon,
    cmd_clone_topic, cmd_stats, cmd_links, cmd_del_link,
    cmd_settings, cmd_set,
    handle_media, handle_callback
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("share", cmd_share))
    app.add_handler(CommandHandler("forward", cmd_forward))
    app.add_handler(CommandHandler("fwd_anon", cmd_fwd_anon))
    app.add_handler(CommandHandler("clone_topic", cmd_clone_topic))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("del_link", cmd_del_link))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))

    # Media messages (auto-link generation)
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL |
        filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE |
        filters.ANIMATION | filters.Sticker.ALL,
        handle_media
    ))

    # Callback queries (inline keyboard buttons)
    app.add_handler(CallbackQueryHandler(handle_callback))

    return app


def main():
    app = build_app()
    logger.info("Bot started. Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
