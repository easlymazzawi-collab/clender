"""
Telegram bot message handlers.

Commands (all users):
  /start [TOKEN]  – Welcome + serve album if TOKEN given
  /help           – Feature list
  /share          – Reply to media → create share link
  /forward        – Reply → forward (named) to destination forum
  /fwd_anon       – Reply → forward (anonymous) to destination forum
  /mylinks        – List my own share links (last 5)

Commands (admin only):
  /clone_topic [name] – Clone current topic to dest forum
  /stats              – Bot usage statistics
  /links              – List recent share links
  /del_link <token>   – Deactivate a share link
  /settings           – View bot settings
  /set <key> <value>  – Update a setting
  /allow <user_id>    – Add user to upload whitelist
  /disallow <user_id> – Remove user from whitelist
  /whitelist          – Show current whitelist

Settings keys (via /set or web):
  upload_mode   : "all" | "admin" | "whitelist"  (who can upload media)
  max_file_mb   : max file size in MB (default 50)
  rate_limit    : max uploads per user per hour (default 20, 0=unlimited)
"""

import asyncio
import logging
import time
from collections import defaultdict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config.settings import (
    ADMIN_IDS, DEST_FORUM_ID, BASE_URL, LINK_CAPTION_TEMPLATE,
    CLONE_DELAY_SECONDS, BOT_USERNAME, MAX_FILE_SIZE_MB,
)
from bot.membership import gate, invalidate as invalidate_membership_cache
from database.models import (
    create_media_link, get_media_link, list_media_links,
    delete_media_link, upsert_topic, list_topics,
    list_forward_logs, log_forward, get_setting, set_setting, all_settings,
    get_media_album, list_media_albums,
)

logger = logging.getLogger(__name__)

# ─── Rate-limit tracker (in-memory) ──────────────────────────────────────────
_upload_times: dict[int, list[float]] = defaultdict(list)


# ═══════════════════════════════════════════════════════════════════════════════
# PERMISSION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _get_upload_mode() -> str:
    """all | admin | whitelist"""
    return get_setting("upload_mode", "all")


def _get_whitelist() -> set[int]:
    raw = get_setting("upload_whitelist", "")
    ids = set()
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.add(int(x))
    return ids


def _add_to_whitelist(user_id: int):
    wl = _get_whitelist()
    wl.add(user_id)
    set_setting("upload_whitelist", ",".join(str(x) for x in wl))


def _remove_from_whitelist(user_id: int):
    wl = _get_whitelist()
    wl.discard(user_id)
    set_setting("upload_whitelist", ",".join(str(x) for x in wl))


def can_upload(user_id: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    Checks: upload_mode → rate_limit → file permissions.
    """
    mode = _get_upload_mode()

    if mode == "admin" and not is_admin(user_id):
        return False, "🚫 Chỉ Admin mới được upload media."

    if mode == "whitelist" and not is_admin(user_id):
        if user_id not in _get_whitelist():
            return False, "🚫 Bạn chưa được cấp quyền upload. Liên hệ admin."

    # Rate limit
    rate_limit = int(get_setting("rate_limit", "20"))
    if rate_limit > 0:
        now = time.time()
        times = _upload_times[user_id]
        # Keep only last hour
        _upload_times[user_id] = [t for t in times if now - t < 3600]
        if len(_upload_times[user_id]) >= rate_limit:
            return False, f"⏳ Bạn đã upload {rate_limit} file trong 1 giờ. Thử lại sau."
        _upload_times[user_id].append(now)

    return True, ""


def _get_max_file_mb() -> int:
    return int(get_setting("max_file_mb", str(MAX_FILE_SIZE_MB)))


# ═══════════════════════════════════════════════════════════════════════════════
# LINK HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def build_share_url(token: str) -> str:
    """Bot deep link: t.me/BOT?start=TOKEN"""
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start={token}"
    return f"{BASE_URL}/media/{token}"


def extract_file_info(msg) -> dict | None:
    if msg.photo:
        ph = msg.photo[-1]
        # Check size
        size_mb = (ph.file_size or 0) / 1024 / 1024
        return {"file_id": ph.file_id, "file_type": "photo",
                "thumb_file_id": msg.photo[0].file_id if len(msg.photo) > 1 else ph.file_id,
                "file_name": None, "mime_type": "image/jpeg", "size_mb": size_mb}
    if msg.video:
        v = msg.video
        size_mb = (v.file_size or 0) / 1024 / 1024
        return {"file_id": v.file_id, "file_type": "video",
                "thumb_file_id": v.thumbnail.file_id if v.thumbnail else None,
                "file_name": v.file_name, "mime_type": v.mime_type, "size_mb": size_mb}
    if msg.document:
        d = msg.document
        size_mb = (d.file_size or 0) / 1024 / 1024
        return {"file_id": d.file_id, "file_type": "document",
                "thumb_file_id": d.thumbnail.file_id if d.thumbnail else None,
                "file_name": d.file_name, "mime_type": d.mime_type, "size_mb": size_mb}
    if msg.audio:
        a = msg.audio
        size_mb = (a.file_size or 0) / 1024 / 1024
        return {"file_id": a.file_id, "file_type": "audio",
                "thumb_file_id": a.thumbnail.file_id if a.thumbnail else None,
                "file_name": a.file_name, "mime_type": a.mime_type, "size_mb": size_mb}
    if msg.voice:
        size_mb = (msg.voice.file_size or 0) / 1024 / 1024
        return {"file_id": msg.voice.file_id, "file_type": "voice",
                "thumb_file_id": None, "file_name": "voice.ogg",
                "mime_type": "audio/ogg", "size_mb": size_mb}
    if msg.video_note:
        vn = msg.video_note
        size_mb = (vn.file_size or 0) / 1024 / 1024
        return {"file_id": vn.file_id, "file_type": "video_note",
                "thumb_file_id": vn.thumbnail.file_id if vn.thumbnail else None,
                "file_name": None, "mime_type": "video/mp4", "size_mb": size_mb}
    if msg.animation:
        ani = msg.animation
        size_mb = (ani.file_size or 0) / 1024 / 1024
        return {"file_id": ani.file_id, "file_type": "animation",
                "thumb_file_id": ani.thumbnail.file_id if ani.thumbnail else None,
                "file_name": ani.file_name, "mime_type": ani.mime_type, "size_mb": size_mb}
    if msg.sticker:
        size_mb = (msg.sticker.file_size or 0) / 1024 / 1024
        return {"file_id": msg.sticker.file_id, "file_type": "sticker",
                "thumb_file_id": msg.sticker.thumbnail.file_id if msg.sticker.thumbnail else None,
                "file_name": None, "mime_type": "image/webp", "size_mb": size_mb}
    return None


async def create_link_for_message(msg, uploader_id, uploader_name) -> str | None:
    fi = extract_file_info(msg)
    if not fi:
        return None
    from utils.token import generate_token
    token = generate_token(14)
    caption = msg.caption or msg.text or ""
    create_media_link(
        token=token, file_id=fi["file_id"], file_type=fi["file_type"],
        file_name=fi.get("file_name"), mime_type=fi.get("mime_type"),
        thumb_file_id=fi.get("thumb_file_id"), caption=caption,
        uploader_id=uploader_id, uploader_name=uploader_name,
    )
    return token


# ═══════════════════════════════════════════════════════════════════════════════
# /start — welcome + deep link album serving
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Deep link: /start TOKEN — check membership even for deep links
    if ctx.args:
        token = ctx.args[0].strip()
        if not await gate(update, ctx):
            return
        album = get_media_album(token)
        if album:
            await _serve_album(update, ctx, album)
            return
        await update.message.reply_text("❌ Link không hợp lệ hoặc đã hết hạn.")
        return

    # Normal /start — show join prompt if not a member, else welcome
    if not await gate(update, ctx):
        return

    mode = _get_upload_mode()
    mode_text = {"all": "Tất cả", "admin": "Chỉ Admin", "whitelist": "Whitelist"}.get(mode, mode)

    keyboard = [
        [InlineKeyboardButton("📁 Chia sẻ Media", callback_data="help_share")],
        [InlineKeyboardButton("↩️ Forward có tên", callback_data="help_fwd"),
         InlineKeyboardButton("👤 Forward ẩn tên", callback_data="help_anon")],
        [InlineKeyboardButton("🌐 Trang Quản Trị", url=BASE_URL)],
    ]
    await update.message.reply_text(
        f"👋 Xin chào *{user.first_name}*!\n\n"
        "🤖 *Forum Converter Bot*\n\n"
        "📌 *Tính năng:*\n"
        "• 🔗 Gửi media → bot tạo link chia sẻ\n"
        "• 🎬 Video → thumbnail + link\n"
        "• 📦 Album → preview + 1 link (click để nhận full)\n"
        "• ↩️ Forward có/ẩn tên\n"
        "• 📋 Clone chủ đề giữa các forum\n\n"
        f"🔒 *Chế độ upload:* `{mode_text}`\n"
        "💡 Dùng /help để xem toàn bộ lệnh",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _serve_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE, album: dict):
    """
    Serve an album to the user.

    Strategy:
      1. copy_messages() — send ALL at once, Telegram preserves album grouping.
         No 'Forwarded from' shown. Requires Bot API 6.4+ (PTB 20.3+).
      2. forward_messages() — same but shows 'Forwarded from'.
      3. Individual copy_message() per item — last resort (breaks album).
    """
    chat_id     = update.effective_chat.id
    src_chat_id = album["src_chat_id"]
    src_msg_ids = album["src_msg_ids"]

    if not src_msg_ids:
        await update.message.reply_text("❌ Album trống.")
        return

    n = len(src_msg_ids)

    # ── 1. copy_messages (batch, no Forwarded-from, preserves album) ──────────
    try:
        result = await ctx.bot.copy_messages(
            chat_id=chat_id,
            from_chat_id=src_chat_id,
            message_ids=src_msg_ids,
        )
        # copy_messages returns list of MessageId objects when successful
        if result and len(result) > 0:
            return
    except Exception as e:
        logger.warning(f"copy_messages failed: {e} — trying forward_messages")

    # ── 2. forward_messages (batch, shows Forwarded-from, preserves album) ────
    try:
        result = await ctx.bot.forward_messages(
            chat_id=chat_id,
            from_chat_id=src_chat_id,
            message_ids=src_msg_ids,
        )
        if result and len(result) > 0:
            return
    except Exception as e:
        logger.warning(f"forward_messages failed: {e} — falling back to individual sends")

    # ── 3. Individual sends (last resort) ────────────────────────────────────
    sent = 0
    for msg_id in src_msg_ids:
        try:
            await ctx.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=src_chat_id,
                message_id=msg_id,
            )
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e1:
            try:
                await ctx.bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=src_chat_id,
                    message_id=msg_id,
                )
                sent += 1
                await asyncio.sleep(0.2)
            except Exception as e2:
                logger.warning(f"Cannot send msg {msg_id}: copy={e1} | fwd={e2}")

    if sent == 0:
        await update.message.reply_text(
            "❌ Không thể gửi media.\n"
            "Bot cần được add vào forum nguồn hoặc file đã bị xóa."
        )
    elif sent < n:
        await update.message.reply_text(
            f"⚠️ Đã gửi {sent}/{n} file. {n - sent} file không khả dụng."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    mode = _get_upload_mode()
    mode_text = {"all": "Tất cả mọi người", "admin": "Chỉ Admin",
                 "whitelist": "Danh sách được phép"}.get(mode, mode)

    admin_section = ""
    if is_admin(update.effective_user.id):
        admin_section = (
            "\n*🔑 Lệnh Admin:*\n"
            "• /clone\\_topic `[tên]` – Clone topic sang forum mới\n"
            "• /stats – Thống kê bot\n"
            "• /links – Danh sách link gần đây\n"
            "• /del\\_link `<token>` – Xóa link\n"
            "• /allow `<user_id>` – Thêm vào whitelist\n"
            "• /disallow `<user_id>` – Xóa khỏi whitelist\n"
            "• /whitelist – Xem whitelist hiện tại\n"
            "• /settings – Xem cài đặt\n"
            "• /set `<key>` `<value>` – Thay đổi cài đặt\n"
        )

    await update.message.reply_text(
        "📖 *Hướng dẫn sử dụng Bot*\n\n"
        "*📤 Lệnh cơ bản:*\n"
        "• /start – Khởi động\n"
        "• /help – Hướng dẫn\n"
        "• /mylinks – Link của tôi (5 gần nhất)\n"
        "• /share – Reply vào media → tạo link\n"
        "• /forward – Reply → forward có tên\n"
        "• /fwd\\_anon – Reply → forward ẩn tên\n\n"
        "*📁 Gửi media trực tiếp:*\n"
        "Gửi ảnh/video/file → bot tạo link chia sẻ.\n"
        "Video → thumbnail + link. Album → 1 link.\n\n"
        f"🔒 *Quyền upload hiện tại:* `{mode_text}`"
        + admin_section,
        parse_mode=ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /mylinks — show user's own links
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_mylinks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    user_id = update.effective_user.id
    conn = __import__("database.models", fromlist=["get_conn"]).get_conn()
    rows = conn.execute(
        """SELECT token, file_type, file_name, access_count, created_at
           FROM media_links WHERE uploader_id=? AND is_active=1
           ORDER BY created_at DESC LIMIT 5""",
        (user_id,)
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📭 Bạn chưa tạo link nào.")
        return

    lines = []
    for r in rows:
        url = build_share_url(r["token"])
        lines.append(
            f"• `{r['token']}` — {r['file_type']} — 👁️{r['access_count']}\n"
            f"  [{url}]({url})"
        )
    await update.message.reply_text(
        "🔗 *Link của bạn (5 gần nhất):*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /share
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    user = update.effective_user
    allowed, reason = can_upload(user.id)
    if not allowed:
        await update.message.reply_text(reason)
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "↩️ Hãy *reply* vào tin nhắn có media rồi dùng /share",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target = update.message.reply_to_message
    fi = extract_file_info(target)
    if not fi:
        await update.message.reply_text(
            "❌ Tin nhắn này không chứa media được hỗ trợ.\n"
            "Hỗ trợ: ảnh, video, file, âm thanh, giọng nói, sticker."
        )
        return

    # File size check
    max_mb = _get_max_file_mb()
    if fi.get("size_mb", 0) > max_mb:
        await update.message.reply_text(
            f"❌ File quá lớn ({fi['size_mb']:.1f} MB). Giới hạn: {max_mb} MB."
        )
        return

    token = await create_link_for_message(target, user.id, user.full_name)
    if not token:
        await update.message.reply_text("❌ Không thể tạo link.")
        return

    url      = build_share_url(token)
    orig_cap = (target.caption or target.text or "").strip()
    tpl      = LINK_CAPTION_TEMPLATE.replace("\\n", "\n")
    link_line = tpl.format(url=url)
    full_cap  = f"{orig_cap}\n\n{link_line}" if orig_cap else link_line
    await update.message.reply_text(
        full_cap[:4096],
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Nhấp để xem", url=url)
        ]])
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /forward + /fwd_anon
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
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
            source_chat_id=target.chat_id, source_msg_id=target.message_id,
            dest_chat_id=DEST_FORUM_ID, dest_msg_id=sent.message_id,
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
    if not await gate(update, ctx):
        return
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
    try:
        sent = await _copy_message_anon(ctx.bot, target, DEST_FORUM_ID, None)
        log_forward(
            source_chat_id=target.chat_id, source_msg_id=target.message_id,
            dest_chat_id=DEST_FORUM_ID, dest_msg_id=sent.message_id if sent else 0,
            forward_mode="anonymous"
        )
        await update.message.reply_text(
            "✅ *Forward ẩn danh thành công*",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Anon forward error: {e}")
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def _copy_message_anon(bot, msg, dest_chat_id: int, dest_topic_id: int | None):
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
            text=msg.text, parse_mode=ParseMode.HTML
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN: /clone_topic, /stats, /links, /del_link, /settings, /set
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_clone_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    chat        = update.effective_chat
    topic_id    = update.message.message_thread_id
    topic_name  = " ".join(ctx.args) if ctx.args else f"Topic-{topic_id}"

    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình `DEST_FORUM_ID`.")
        return

    try:
        new_topic = await ctx.bot.create_forum_topic(
            chat_id=DEST_FORUM_ID, name=topic_name
        )
        upsert_topic(
            source_chat_id=chat.id, source_topic_id=topic_id,
            dest_chat_id=DEST_FORUM_ID,
            dest_topic_id=new_topic.message_thread_id,
            topic_name=topic_name
        )
        await update.message.reply_text(
            f"✅ *Topic đã được đăng ký clone!*\n\n"
            f"📌 Tên: `{topic_name}`\n"
            f"🔢 ID nguồn: `{topic_id}` → đích: `{new_topic.message_thread_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Clone topic error: {e}")
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    links        = list_media_links(limit=9999)
    topics       = list_topics(limit=9999)
    logs         = list_forward_logs(limit=9999)
    albums       = list_media_albums(limit=9999)
    total_access = sum(l.get("access_count", 0) for l in links)
    album_access = sum(a.get("access_count", 0) for a in albums)

    await update.message.reply_text(
        "📊 *Thống kê Bot*\n\n"
        f"🔗 Link chia sẻ: `{len(links)}` (👁️ {total_access} lượt)\n"
        f"📦 Bot albums  : `{len(albums)}` (👁️ {album_access} lượt)\n"
        f"📋 Topic clone : `{len(topics)}`\n"
        f"↩️ Forward log : `{len(logs)}`\n\n"
        f"🔒 Upload mode : `{_get_upload_mode()}`\n"
        f"📏 Max file    : `{_get_max_file_mb()} MB`\n"
        f"⏱️ Rate limit  : `{get_setting('rate_limit','20')}/giờ`",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    links = list_media_links(limit=10)
    if not links:
        await update.message.reply_text("📭 Chưa có link nào.")
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
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /del_link <token>")
        return
    delete_media_link(ctx.args[0])
    await update.message.reply_text(
        f"✅ Đã xóa link `{ctx.args[0]}`", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    s = all_settings()
    lines = "\n".join(f"• `{k}` = `{v}`" for k, v in s.items()) if s else "_Chưa có_"
    fj_channel = get_setting("force_join_channel", "") or "_(tắt)_"
    fj_sec     = get_setting("force_join_check_sec", "300")
    await update.message.reply_text(
        "⚙️ *Cài đặt hiện tại:*\n\n" + lines + "\n\n"
        "*Các key hữu ích:*\n"
        "• `upload_mode` = `all` / `admin` / `whitelist`\n"
        "• `max_file_mb` = số MB tối đa (mặc định 50)\n"
        "• `rate_limit` = số upload/giờ (0=không giới hạn)\n"
        "• `caption_template` = mẫu caption link\n"
        "• `force_join_channel` = kênh bắt buộc\n"
        "• `force_join_check_sec` = giây giữa 2 lần check thành viên\n"
        "• `force_join_message` = tin nhắn khi chưa vào kênh\n\n"
        f"📢 *Force-Join:* `{fj_channel}` (check mỗi `{fj_sec}` giây)\n\n"
        "Dùng /forcejoin để cài đặt nhanh.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /set <key> <value>")
        return
    key   = ctx.args[0]
    value = " ".join(ctx.args[1:])
    set_setting(key, value)
    await update.message.reply_text(
        f"✅ `{key}` = `{value}`", parse_mode=ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN: Whitelist management
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /allow <user_id>")
        return
    uid = int(ctx.args[0])
    _add_to_whitelist(uid)
    await update.message.reply_text(
        f"✅ Đã thêm `{uid}` vào whitelist.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_disallow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /disallow <user_id>")
        return
    uid = int(ctx.args[0])
    _remove_from_whitelist(uid)
    await update.message.reply_text(
        f"✅ Đã xóa `{uid}` khỏi whitelist.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_forcejoin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: /forcejoin @channel | off — configure force-join requirement."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return

    if not ctx.args:
        current = get_setting("force_join_channel", "") or "_(tắt)_"
        check   = get_setting("force_join_check_sec", "300")
        await update.message.reply_text(
            "📢 *Cài đặt Force-Join:*\n\n"
            f"• Kênh: `{current}`\n"
            f"• Kiểm tra lại mỗi: `{check}` giây\n\n"
            "*Lệnh:*\n"
            "• `/forcejoin @kenhcuaban` – bật & đặt kênh\n"
            "• `/forcejoin off` – tắt\n"
            "• `/set force_join_check_sec 600` – check 10 phút/lần",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    val = ctx.args[0].strip()
    if val.lower() in ("off", "0", "none", "tắt"):
        set_setting("force_join_channel", "")
        await update.message.reply_text(
            "✅ Đã tắt yêu cầu tham gia kênh.\n"
            "Mọi người đều dùng bot được."
        )
    else:
        # Ensure starts with @ or is numeric ID
        if not val.startswith("@") and not val.lstrip("-").isdigit():
            val = "@" + val
        set_setting("force_join_channel", val)
        check_sec = get_setting("force_join_check_sec", "300")
        await update.message.reply_text(
            f"✅ *Đã bật Force-Join!*\n\n"
            f"📢 Kênh bắt buộc: `{val}`\n"
            f"⏱️ Kiểm tra lại mỗi: `{check_sec}` giây\n\n"
            "⚠️ Đảm bảo bot là **Admin** trong kênh đó\n"
            "để có thể kiểm tra thành viên.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    wl = _get_whitelist()
    if not wl:
        await update.message.reply_text(
            "📋 Whitelist trống.\n"
            "Dùng /allow <user_id> để thêm."
        )
        return
    lines = "\n".join(f"• `{uid}`" for uid in sorted(wl))
    await update.message.reply_text(
        f"📋 *Whitelist ({len(wl)} người):*\n\n{lines}",
        parse_mode=ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Media handler — auto-create link when user sends media
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    if not msg:
        return

    # Membership gate first (re-checks every N seconds, catches members who left)
    if not await gate(update, ctx):
        return

    user = update.effective_user
    fi   = extract_file_info(msg)
    if not fi:
        return

    # Permission check
    allowed, reason = can_upload(user.id)
    if not allowed:
        await msg.reply_text(reason)
        return

    # File size check
    max_mb = _get_max_file_mb()
    if fi.get("size_mb", 0) > max_mb:
        await msg.reply_text(
            f"❌ File quá lớn ({fi['size_mb']:.1f} MB). Giới hạn: {max_mb} MB.\n"
            f"Liên hệ admin để tăng giới hạn."
        )
        return

    # Create link
    token = await create_link_for_message(msg, user.id, user.full_name)
    if not token:
        return

    url          = build_share_url(token)
    caption_text = LINK_CAPTION_TEMPLATE.format(url=url)

    # Reply với format giống post ở forum đích:
    # [caption gốc nếu có] + link — không hiện token, không hiện "✅ Đã tạo link"
    orig_cap = (msg.caption or msg.text or "").strip()
    if orig_cap:
        full_cap = f"{orig_cap}\n\n{caption_text}"
    else:
        full_cap = caption_text

    if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Xem video", url=url)]])
        await msg.reply_photo(
            photo=fi["thumb_file_id"],
            caption=full_cap[:1024],
            reply_markup=kb
        )
    elif fi["file_type"] == "photo":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Nhấp để xem", url=url)]])
        await msg.reply_photo(
            photo=fi["file_id"],
            caption=full_cap[:1024],
            reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Nhấp để xem", url=url)]])
        await msg.reply_text(
            full_cap[:4096],
            reply_markup=kb
        )

    # Auto-clone to registered dest topics
    await _auto_clone_to_dest(ctx.bot, msg, token)


async def _auto_clone_to_dest(bot, msg, token: str):
    topics   = list_topics()
    topic_id = msg.message_thread_id
    for t in topics:
        if t["source_chat_id"] == msg.chat_id and t["source_topic_id"] == topic_id:
            dest_topic = t.get("dest_topic_id")
            try:
                await asyncio.sleep(CLONE_DELAY_SECONDS)
                sent = await _clone_media_as_link(bot, msg, t["dest_chat_id"],
                                                  dest_topic, token)
                from database.models import increment_topic_msg_count
                increment_topic_msg_count(msg.chat_id, topic_id)
                log_forward(
                    source_chat_id=msg.chat_id, source_msg_id=msg.message_id,
                    dest_chat_id=t["dest_chat_id"],
                    dest_msg_id=sent.message_id if sent else 0,
                    source_topic_id=topic_id, dest_topic_id=dest_topic,
                    media_token=token, forward_mode="clone"
                )
            except Exception as e:
                logger.error(f"Auto-clone error: {e}")
                log_forward(
                    source_chat_id=msg.chat_id, source_msg_id=msg.message_id,
                    dest_chat_id=t["dest_chat_id"], dest_msg_id=0,
                    source_topic_id=topic_id, dest_topic_id=dest_topic,
                    media_token=token, forward_mode="clone",
                    status=f"error: {e}"
                )


async def _clone_media_as_link(bot, msg, dest_chat_id, dest_topic_id, token):
    fi = extract_file_info(msg)
    if not fi:
        return None
    url          = build_share_url(token)
    caption_text = LINK_CAPTION_TEMPLATE.format(url=url)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Nhấp để xem", url=url)]])
    kwargs = dict(chat_id=dest_chat_id, message_thread_id=dest_topic_id, reply_markup=kb)
    if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
        orig_cap    = msg.caption or ""
        full_caption = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_photo(
            photo=fi["thumb_file_id"],
            caption=full_caption[:1024],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )
    elif fi["file_type"] == "photo":
        orig_cap    = msg.caption or ""
        full_caption = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_photo(
            photo=fi["file_id"],
            caption=full_caption[:1024],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )
    else:
        orig_cap    = msg.caption or ""
        text = (f"{orig_cap}\n\n" if orig_cap else "") + caption_text
        return await bot.send_message(
            text=text[:4096],
            parse_mode=ParseMode.MARKDOWN,
            **kwargs
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Callback query handler
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = update.effective_user

    # ── "Tôi đã tham gia — kiểm tra lại" button ──────────────────────────────
    if data == "check_membership":
        # Force re-check by clearing cache for this user
        invalidate_membership_cache(user.id)
        from bot.membership import check_user
        ok = await check_user(ctx.bot, user.id)
        if ok:
            await q.message.edit_text(
                "✅ Xác nhận thành công! Bạn đã là thành viên.\n"
                "Dùng /start để bắt đầu.",
            )
        else:
            await q.answer(
                "❌ Bạn vẫn chưa tham gia kênh. Hãy tham gia rồi nhấn lại.",
                show_alert=True
            )
        return

    # ── Help inline buttons ────────────────────────────────────────────────────
    texts = {
        "help_share": (
            "📁 *Chia sẻ Media:*\n\n"
            "1. Gửi bất kỳ ảnh/video/file vào chat\n"
            "2. Bot tự tạo link chia sẻ\n"
            "3. Hoặc reply vào tin nhắn có media rồi gõ /share"
        ),
        "help_fwd": (
            "↩️ *Forward có tên:*\n\n"
            "1. Reply vào tin nhắn muốn forward\n"
            "2. Gõ /forward\n"
            "3. Gửi đến forum đích với tên người gửi gốc"
        ),
        "help_anon": (
            "👤 *Forward ẩn tên:*\n\n"
            "1. Reply vào tin nhắn muốn forward\n"
            "2. Gõ /fwd\\_anon\n"
            "3. Gửi mà không hiện tên người gửi gốc"
        ),
    }
    if data in texts:
        await q.message.reply_text(texts[data], parse_mode=ParseMode.MARKDOWN)
