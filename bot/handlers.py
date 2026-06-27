"""
Telegram bot message handlers.

Commands:
  /start          – Welcome + help
  /help           – Feature list
  /share          – Reply to a message to create a shareable link
  /forward        – Reply to forward (named) to destination forum
  /fwd_anon       – Reply to forward (anonymous) to destination forum
  /clone_topic    – Admin: clone current topic to dest forum
  /stats          – Admin: usage statistics
  /links          – Admin: list recent share links
  /settings       – Admin: bot settings menu
  /del_link <tok> – Admin: deactivate a share link
  /cancel         – Cancel current operation
"""

import asyncio
import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config.settings import (
    ADMIN_IDS, DEST_FORUM_ID, BASE_URL, LINK_CAPTION_TEMPLATE,
    CLONE_DELAY_SECONDS, BOT_USERNAME,
)
from database.models import (
    create_media_link, get_media_link, list_media_links,
    delete_media_link, upsert_topic, list_topics,
    list_forward_logs, log_forward, get_setting, set_setting, all_settings,
    get_media_album, list_media_albums,
)
from utils.token import generate_token

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def build_share_url(token: str) -> str:
    return f"{BASE_URL}/media/{token}"


def extract_file_info(msg) -> dict | None:
    """Extract file_id, file_type, thumb, etc. from a Telegram message."""
    if msg.photo:
        ph = msg.photo[-1]
        return {"file_id": ph.file_id, "file_type": "photo",
                "thumb_file_id": msg.photo[0].file_id if len(msg.photo) > 1 else ph.file_id,
                "file_name": None, "mime_type": "image/jpeg"}
    if msg.video:
        v = msg.video
        return {"file_id": v.file_id, "file_type": "video",
                "thumb_file_id": v.thumbnail.file_id if v.thumbnail else None,
                "file_name": v.file_name, "mime_type": v.mime_type}
    if msg.document:
        d = msg.document
        return {"file_id": d.file_id, "file_type": "document",
                "thumb_file_id": d.thumbnail.file_id if d.thumbnail else None,
                "file_name": d.file_name, "mime_type": d.mime_type}
    if msg.audio:
        a = msg.audio
        return {"file_id": a.file_id, "file_type": "audio",
                "thumb_file_id": a.thumbnail.file_id if a.thumbnail else None,
                "file_name": a.file_name, "mime_type": a.mime_type}
    if msg.voice:
        return {"file_id": msg.voice.file_id, "file_type": "voice",
                "thumb_file_id": None, "file_name": "voice.ogg",
                "mime_type": "audio/ogg"}
    if msg.video_note:
        vn = msg.video_note
        return {"file_id": vn.file_id, "file_type": "video_note",
                "thumb_file_id": vn.thumbnail.file_id if vn.thumbnail else None,
                "file_name": None, "mime_type": "video/mp4"}
    if msg.animation:
        ani = msg.animation
        return {"file_id": ani.file_id, "file_type": "animation",
                "thumb_file_id": ani.thumbnail.file_id if ani.thumbnail else None,
                "file_name": ani.file_name, "mime_type": ani.mime_type}
    if msg.sticker:
        return {"file_id": msg.sticker.file_id, "file_type": "sticker",
                "thumb_file_id": msg.sticker.thumbnail.file_id if msg.sticker.thumbnail else None,
                "file_name": None, "mime_type": "image/webp"}
    return None


async def create_link_for_message(msg, uploader_id, uploader_name) -> str | None:
    """Create a share link for a message with media. Returns token or None."""
    fi = extract_file_info(msg)
    if not fi:
        return None
    caption = msg.caption or msg.text or ""
    token = generate_token(14)
    create_media_link(
        token=token,
        file_id=fi["file_id"],
        file_type=fi["file_type"],
        file_name=fi.get("file_name"),
        mime_type=fi.get("mime_type"),
        thumb_file_id=fi.get("thumb_file_id"),
        caption=caption,
        uploader_id=uploader_id,
        uploader_name=uploader_name,
    )
    return token


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # ── Deep link: /start TOKEN → serve album ─────────────────────────────
    if ctx.args:
        token = ctx.args[0].strip()
        album = get_media_album(token)
        if album:
            await _serve_album(update, ctx, album)
            return
        # Unknown token
        await update.message.reply_text(
            "❌ Link không hợp lệ hoặc đã hết hạn."
        )
        return

    # ── Normal /start ──────────────────────────────────────────────────────
    keyboard = [
        [InlineKeyboardButton("📁 Chia sẻ Media", callback_data="help_share")],
        [InlineKeyboardButton("↩️ Forward có tên", callback_data="help_fwd"),
         InlineKeyboardButton("👤 Forward ẩn tên", callback_data="help_anon")],
        [InlineKeyboardButton("🌐 Trang Quản Trị", url=BASE_URL)],
    ]
    await update.message.reply_text(
        f"👋 Xin chào *{user.first_name}*!\n\n"
        "🤖 *Forum Converter Bot* – Hệ thống chuyển đổi diễn đàn thông minh\n\n"
        "📌 *Tính năng chính:*\n"
        "• 🔗 Tạo link chia sẻ media (ảnh, video, file)\n"
        "• ↩️ Forward tin nhắn có/không ẩn tên\n"
        "• 📋 Clone chủ đề giữa các forum\n"
        "• 🖼️ Tự động lấy thumbnail cho video\n"
        "• 📊 Thống kê truy cập link\n\n"
        "💡 Dùng /help để xem toàn bộ lệnh",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _serve_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE, album: dict):
    """
    Serve an album to the user who clicked the bot deep link.
    Uses copy_message to re-send original messages without "Forwarded from".
    Falls back to forward_message if copy fails.
    """
    user    = update.effective_user
    chat_id = update.effective_chat.id
    src_chat_id = album["src_chat_id"]
    src_msg_ids = album["src_msg_ids"]   # list of int

    if not src_msg_ids:
        await update.message.reply_text("❌ Album trống.")
        return

    await update.message.reply_text(
        f"⏳ Đang gửi {len(src_msg_ids)} file…",
        parse_mode=ParseMode.MARKDOWN,
    )

    sent = 0
    for msg_id in src_msg_ids:
        try:
            # copy_message: no "Forwarded from" attribution
            await ctx.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=src_chat_id,
                message_id=msg_id,
            )
            sent += 1
            await asyncio.sleep(0.3)
        except Exception as copy_err:
            # Fallback: forward_message (shows "Forwarded from")
            try:
                await ctx.bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=src_chat_id,
                    message_id=msg_id,
                )
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as fwd_err:
                logger.warning(
                    f"serve_album: cannot serve msg {msg_id} "
                    f"from {src_chat_id}: copy={copy_err}, fwd={fwd_err}"
                )

    if sent == 0:
        await update.message.reply_text(
            "❌ Không thể gửi media. Bot cần được thêm vào forum nguồn.\n"
            "Liên hệ admin để được hỗ trợ."
        )
    elif sent < len(src_msg_ids):
        await update.message.reply_text(
            f"⚠️ Đã gửi {sent}/{len(src_msg_ids)} file. "
            f"{len(src_msg_ids) - sent} file không khả dụng."
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin_section = ""
    if is_admin(update.effective_user.id):
        admin_section = (
            "\n*🔑 Lệnh Admin:*\n"
            "• /clone\\_topic – Clone chủ đề hiện tại sang forum mới\n"
            "• /stats – Thống kê bot\n"
            "• /links – Danh sách link chia sẻ gần đây\n"
            "• /settings – Cài đặt bot\n"
            "• /del\\_link `<token>` – Xóa link chia sẻ\n"
        )

    await update.message.reply_text(
        "📖 *Hướng dẫn sử dụng Bot*\n\n"
        "*📤 Lệnh cơ bản:*\n"
        "• /start – Khởi động bot\n"
        "• /help – Xem hướng dẫn\n"
        "• /share – Reply vào media để tạo link chia sẻ\n"
        "• /forward – Reply để forward có tên\n"
        "• /fwd\\_anon – Reply để forward ẩn tên\n\n"
        "*📁 Gửi trực tiếp:*\n"
        "Gửi bất kỳ ảnh/video/file nào, bot sẽ tự động\n"
        "tạo link chia sẻ và gửi lại cho bạn.\n"
        + admin_section,
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reply to a message with /share to generate a shareable link."""
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "↩️ Hãy *reply* vào tin nhắn có media rồi dùng /share",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target = update.message.reply_to_message
    user = update.effective_user
    token = await create_link_for_message(
        target, user.id, user.full_name
    )

    if not token:
        await update.message.reply_text(
            "❌ Tin nhắn này không chứa media được hỗ trợ.\n"
            "Hỗ trợ: ảnh, video, file, âm thanh, giọng nói, sticker."
        )
        return

    url = build_share_url(token)
    caption = LINK_CAPTION_TEMPLATE.format(url=url)

    await update.message.reply_text(
        f"✅ *Link đã được tạo!*\n\n"
        f"{caption}\n\n"
        f"🔑 Token: `{token}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Mở link", url=url)
        ]])
    )


async def cmd_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Forward replied message to destination forum (keep sender name)."""
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "↩️ Hãy *reply* vào tin nhắn muốn forward rồi dùng /forward",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình `DEST_FORUM_ID`.")
        return

    target = update.message.reply_to_message
    try:
        sent = await ctx.bot.forward_message(
            chat_id=DEST_FORUM_ID,
            from_chat_id=target.chat_id,
            message_id=target.message_id
        )
        log_forward(
            source_chat_id=target.chat_id,
            source_msg_id=target.message_id,
            dest_chat_id=DEST_FORUM_ID,
            dest_msg_id=sent.message_id,
            forward_mode="named"
        )
        await update.message.reply_text(
            "✅ *Forward thành công* (có tên người gửi)",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Forward error: {e}")
        await update.message.reply_text(f"❌ Lỗi khi forward: {e}")


async def cmd_fwd_anon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Forward replied message anonymously (copy without sender info)."""
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "↩️ Hãy *reply* vào tin nhắn muốn forward ẩn danh rồi dùng /fwd\\_anon",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình `DEST_FORUM_ID`.")
        return

    target = update.message.reply_to_message
    user = update.effective_user
    try:
        sent = await _copy_message_anon(ctx.bot, target, DEST_FORUM_ID, None)
        log_forward(
            source_chat_id=target.chat_id,
            source_msg_id=target.message_id,
            dest_chat_id=DEST_FORUM_ID,
            dest_msg_id=sent.message_id if sent else 0,
            forward_mode="anonymous"
        )
        await update.message.reply_text(
            "✅ *Forward ẩn danh thành công* (không hiện tên)",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Anon forward error: {e}")
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def _copy_message_anon(bot, msg, dest_chat_id: int, dest_topic_id: int | None):
    """Copy a message without the forwarded-from attribution."""
    kwargs = dict(
        chat_id=dest_chat_id,
        message_thread_id=dest_topic_id,
        caption=msg.caption,
        parse_mode=ParseMode.HTML if msg.caption else None,
    )
    if msg.photo:
        return await bot.send_photo(photo=msg.photo[-1].file_id, **kwargs)
    if msg.video:
        thumb = msg.video.thumbnail.file_id if msg.video.thumbnail else None
        return await bot.send_video(video=msg.video.file_id, thumbnail=thumb, **kwargs)
    if msg.document:
        thumb = msg.document.thumbnail.file_id if msg.document.thumbnail else None
        return await bot.send_document(document=msg.document.file_id, thumbnail=thumb, **kwargs)
    if msg.audio:
        return await bot.send_audio(audio=msg.audio.file_id, **kwargs)
    if msg.voice:
        return await bot.send_voice(voice=msg.voice.file_id, **kwargs)
    if msg.video_note:
        return await bot.send_video_note(video_note=msg.video_note.file_id,
                                         chat_id=dest_chat_id,
                                         message_thread_id=dest_topic_id)
    if msg.animation:
        return await bot.send_animation(animation=msg.animation.file_id, **kwargs)
    if msg.sticker:
        return await bot.send_sticker(sticker=msg.sticker.file_id,
                                      chat_id=dest_chat_id,
                                      message_thread_id=dest_topic_id)
    if msg.text:
        return await bot.send_message(
            chat_id=dest_chat_id,
            message_thread_id=dest_topic_id,
            text=msg.text,
            parse_mode=ParseMode.HTML
        )
    return None


async def cmd_clone_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: Register current topic for cloning to destination forum."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền thực hiện lệnh này.")
        return

    chat = update.effective_chat
    topic_id = update.message.message_thread_id
    topic_name = ctx.args[0] if ctx.args else f"Topic-{topic_id}"

    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình `DEST_FORUM_ID`.")
        return

    # Create topic in destination forum
    try:
        new_topic = await ctx.bot.create_forum_topic(
            chat_id=DEST_FORUM_ID,
            name=topic_name
        )
        upsert_topic(
            source_chat_id=chat.id,
            source_topic_id=topic_id,
            dest_chat_id=DEST_FORUM_ID,
            dest_topic_id=new_topic.message_thread_id,
            topic_name=topic_name
        )
        await update.message.reply_text(
            f"✅ *Chủ đề đã được đăng ký clone!*\n\n"
            f"📌 Tên: `{topic_name}`\n"
            f"🔢 ID nguồn: `{topic_id}`\n"
            f"🔢 ID đích: `{new_topic.message_thread_id}`\n"
            f"🏠 Forum đích: `{DEST_FORUM_ID}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Clone topic error: {e}")
        await update.message.reply_text(f"❌ Lỗi tạo chủ đề: {e}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: show bot statistics."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    links = list_media_links(limit=9999)
    topics = list_topics(limit=9999)
    logs = list_forward_logs(limit=9999)
    total_access = sum(l.get("access_count", 0) for l in links)

    await update.message.reply_text(
        "📊 *Thống kê Bot*\n\n"
        f"🔗 Tổng link: `{len(links)}`\n"
        f"👁️ Lượt xem: `{total_access}`\n"
        f"📋 Chủ đề đã clone: `{len(topics)}`\n"
        f"↩️ Tổng forward: `{len(logs)}`\n"
        f"🌐 Base URL: `{BASE_URL}`",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: list recent share links."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    links = list_media_links(limit=10)
    if not links:
        await update.message.reply_text("📭 Chưa có link nào được tạo.")
        return

    lines = []
    for l in links:
        url = build_share_url(l["token"])
        lines.append(
            f"• `{l['token']}` – {l['file_type']} – 👁️{l['access_count']}\n"
            f"  [{url}]({url})"
        )

    await update.message.reply_text(
        "🔗 *10 link gần nhất:*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


async def cmd_del_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: deactivate a share link by token."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /del_link <token>")
        return

    token = ctx.args[0]
    delete_media_link(token)
    await update.message.reply_text(f"✅ Đã vô hiệu hóa link `{token}`",
                                     parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: view/edit settings."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    s = all_settings()
    text = "⚙️ *Cài đặt hiện tại:*\n\n"
    if s:
        for k, v in s.items():
            text += f"• `{k}` = `{v}`\n"
    else:
        text += "_Chưa có cài đặt nào._"

    text += (
        "\n\n💡 Để thay đổi, dùng:\n"
        "`/set <key> <value>`\n\n"
        "Ví dụ:\n"
        "`/set caption_template 🔗 Xem tại: {url}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: set a bot setting."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /set <key> <value>")
        return

    key = ctx.args[0]
    value = " ".join(ctx.args[1:])
    set_setting(key, value)
    await update.message.reply_text(
        f"✅ Đã cập nhật: `{key}` = `{value}`",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── Media message handler (auto-create link) ─────────────────────────────────

async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Auto-generate a share link when user sends any media.
    Also handles auto-clone if message is in a registered topic.
    """
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    fi = extract_file_info(msg)
    if not fi:
        return  # not media

    token = await create_link_for_message(msg, user.id, user.full_name)
    if not token:
        return

    url = build_share_url(token)
    caption_text = LINK_CAPTION_TEMPLATE.format(url=url)

    # For video: send thumbnail + link caption
    if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Xem video", url=url)]])
        await msg.reply_photo(
            photo=fi["thumb_file_id"],
            caption=f"🎬 *{fi.get('file_name') or 'Video'}*\n\n{caption_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Mở link", url=url)]])
        await msg.reply_text(
            f"✅ *Media đã được lưu!*\n\n{caption_text}\n\n🔑 Token: `{token}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )

    # Auto-clone to dest forum if topic is registered
    await _auto_clone_to_dest(ctx.bot, msg, token)


async def _auto_clone_to_dest(bot, msg, token: str):
    """If this chat/topic is registered for cloning, copy to dest forum."""
    from database.models import list_topics, increment_topic_msg_count
    import asyncio

    topics = list_topics()
    topic_id = msg.message_thread_id

    for t in topics:
        if t["source_chat_id"] == msg.chat_id and t["source_topic_id"] == topic_id:
            dest_topic = t.get("dest_topic_id")
            try:
                await asyncio.sleep(CLONE_DELAY_SECONDS)
                sent = await _clone_media_as_link(bot, msg, t["dest_chat_id"],
                                                  dest_topic, token)
                increment_topic_msg_count(msg.chat_id, topic_id)
                log_forward(
                    source_chat_id=msg.chat_id,
                    source_msg_id=msg.message_id,
                    dest_chat_id=t["dest_chat_id"],
                    dest_msg_id=sent.message_id if sent else 0,
                    source_topic_id=topic_id,
                    dest_topic_id=dest_topic,
                    media_token=token,
                    forward_mode="clone"
                )
            except Exception as e:
                logger.error(f"Auto-clone error: {e}")
                log_forward(
                    source_chat_id=msg.chat_id,
                    source_msg_id=msg.message_id,
                    dest_chat_id=t["dest_chat_id"],
                    dest_msg_id=0,
                    source_topic_id=topic_id,
                    dest_topic_id=dest_topic,
                    media_token=token,
                    forward_mode="clone",
                    status=f"error: {e}"
                )


async def _clone_media_as_link(bot, msg, dest_chat_id, dest_topic_id, token):
    """Send thumbnail + share link to destination forum."""
    fi = extract_file_info(msg)
    if not fi:
        return None

    url = build_share_url(token)
    caption_text = LINK_CAPTION_TEMPLATE.format(url=url)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Nhấp để xem", url=url)]])
    kwargs = dict(
        chat_id=dest_chat_id,
        message_thread_id=dest_topic_id,
        reply_markup=kb
    )

    if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
        orig_cap = msg.caption or ""
        full_caption = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_photo(
            photo=fi["thumb_file_id"],
            caption=full_caption[:1024],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )
    elif fi["file_type"] == "photo":
        orig_cap = msg.caption or ""
        full_caption = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_photo(
            photo=fi["file_id"],
            caption=full_caption[:1024],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )
    else:
        orig_cap = msg.caption or ""
        text = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_message(
            text=text[:4096],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )


# ─── Callback query handler ───────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "help_share":
        await q.message.reply_text(
            "📁 *Chia sẻ Media:*\n\n"
            "1. Gửi bất kỳ ảnh/video/file vào chat\n"
            "2. Bot tự động tạo link chia sẻ\n"
            "3. Hoặc reply vào tin nhắn có media rồi gõ /share",
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "help_fwd":
        await q.message.reply_text(
            "↩️ *Forward có tên:*\n\n"
            "1. Reply vào tin nhắn muốn forward\n"
            "2. Gõ /forward\n"
            "3. Tin nhắn sẽ được gửi đến forum đích với tên người gửi gốc",
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "help_anon":
        await q.message.reply_text(
            "👤 *Forward ẩn tên:*\n\n"
            "1. Reply vào tin nhắn muốn forward\n"
            "2. Gõ /fwd\\_anon\n"
            "3. Tin nhắn sẽ được gửi mà không hiện tên người gửi gốc",
            parse_mode=ParseMode.MARKDOWN
        )
