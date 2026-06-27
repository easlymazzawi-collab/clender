"""
Entry-point for the Telegram bot.
Run: python run.py --bot
"""
import logging
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from config.settings import BOT_TOKEN
from database.models import init_db
from bot.handlers import (
    cmd_start, cmd_help, cmd_mylinks,
    cmd_share, cmd_forward, cmd_fwd_anon,
    cmd_clone_topic, cmd_stats, cmd_links, cmd_del_link,
    cmd_settings, cmd_set,
    cmd_allow, cmd_disallow, cmd_whitelist,
    cmd_forcejoin, cmd_panel,
    handle_media, handle_callback,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
# Suppress Telethon's noisy internal update logs
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ── User commands ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("mylinks",  cmd_mylinks))
    app.add_handler(CommandHandler("share",    cmd_share))
    app.add_handler(CommandHandler("forward",  cmd_forward))
    app.add_handler(CommandHandler("fwd_anon", cmd_fwd_anon))

    # ── Admin commands ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("clone_topic", cmd_clone_topic))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("links",       cmd_links))
    app.add_handler(CommandHandler("del_link",    cmd_del_link))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("set",         cmd_set))
    app.add_handler(CommandHandler("allow",       cmd_allow))
    app.add_handler(CommandHandler("disallow",    cmd_disallow))
    app.add_handler(CommandHandler("whitelist",   cmd_whitelist))
    app.add_handler(CommandHandler("forcejoin",   cmd_forcejoin))
    app.add_handler(CommandHandler("panel",       cmd_panel))
    app.add_handler(CommandHandler("admin",       cmd_panel))

    # ── Media handler ──────────────────────────────────────────────────────────
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL |
        filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE |
        filters.ANIMATION | filters.Sticker.ALL,
        handle_media
    ))

    # ── Inline keyboard callbacks ──────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback))

    # ── Global error handler ───────────────────────────────────────────────────
    async def on_error(update, context):
        logger.error(f"Bot error: {context.error}", exc_info=context.error)
    app.add_error_handler(on_error)

    return app


def main():
    app = build_app()
    logger.info("Bot started. Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
