"""
Telegram bot handlers.

Yêu cầu hệ thống:
  - User gửi /start TOKEN → bot gửi album media gốc (copy_messages)
  - User gửi media vào BOT (private chat) → tạo link, trả về link
  - /forward, /fwd_anon → forward sang forum đích
  - Force-join: kiểm tra membership trước mọi hành động
  - Upload permissions: all / admin / whitelist
  - handle_media CHỈ chạy trong PRIVATE CHAT — không tự kích hoạt trong group/channel
    (vì bot là admin trong forum đích, nếu không lọc → tự xử lý mọi ảnh Telethon gửi)
"""

import asyncio
import logging
import time
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ParseMode

from config.settings import (
    ADMIN_IDS, DEST_FORUM_ID, BASE_URL,
    LINK_CAPTION_TEMPLATE, CLONE_DELAY_SECONDS,
    BOT_USERNAME, MAX_FILE_SIZE_MB,
)
from database.models import (
    create_media_link, list_media_links, delete_media_link,
    upsert_topic, list_topics, list_forward_logs,
    log_forward, get_setting, set_setting, all_settings,
    get_media_album, list_media_albums, get_conn,
)
from bot.membership import gate, invalidate as invalidate_membership_cache

logger = logging.getLogger(__name__)

# ─── Rate limit tracker ───────────────────────────────────────────────────────
_upload_times: dict[int, list[float]] = defaultdict(list)


# ══════════════════════════════════════════════════════════════════════════════
# PERMISSION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _upload_mode() -> str:
    return get_setting("upload_mode", "all")


def _whitelist() -> set[int]:
    raw = get_setting("upload_whitelist", "") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def _add_whitelist(uid: int):
    wl = _whitelist(); wl.add(uid)
    set_setting("upload_whitelist", ",".join(str(x) for x in wl))


def _remove_whitelist(uid: int):
    wl = _whitelist(); wl.discard(uid)
    set_setting("upload_whitelist", ",".join(str(x) for x in wl))


def can_upload(user_id: int) -> tuple[bool, str]:
    mode = _upload_mode()
    if mode == "admin" and not is_admin(user_id):
        return False, "🚫 Chỉ Admin mới được upload."
    if mode == "whitelist" and not is_admin(user_id) and user_id not in _whitelist():
        return False, "🚫 Bạn chưa được cấp quyền upload. Liên hệ admin."

    limit = int(get_setting("rate_limit", "20") or 20)
    if limit > 0:
        now = time.time()
        _upload_times[user_id] = [t for t in _upload_times[user_id] if now - t < 3600]
        if len(_upload_times[user_id]) >= limit:
            return False, f"⏳ Bạn đã upload {limit} file trong 1 giờ. Thử lại sau."
        _upload_times[user_id].append(now)
    return True, ""


def _max_mb() -> int:
    return int(get_setting("max_file_mb", str(MAX_FILE_SIZE_MB)) or MAX_FILE_SIZE_MB)


# ══════════════════════════════════════════════════════════════════════════════
# SHARE LINK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def build_share_url(token: str) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start={token}"
    return f"{BASE_URL}/media/{token}"


def _caption_with_link(orig_cap: str, url: str) -> str:
    tpl = LINK_CAPTION_TEMPLATE.replace("\\n", "\n")
    link_line = tpl.format(url=url)
    return f"{orig_cap}\n\n{link_line}" if orig_cap else link_line


def extract_file_info(msg) -> dict | None:
    if msg.photo:
        ph = msg.photo[-1]
        return {"file_id": ph.file_id, "file_type": "photo",
                "file_name": None, "mime_type": "image/jpeg",
                "size_mb": (ph.file_size or 0) / 1024 / 1024,
                "thumb_file_id": ph.file_id}
    if msg.video:
        v = msg.video
        return {"file_id": v.file_id, "file_type": "video",
                "file_name": v.file_name, "mime_type": v.mime_type,
                "size_mb": (v.file_size or 0) / 1024 / 1024,
                "thumb_file_id": v.thumbnail.file_id if v.thumbnail else None}
    if msg.document:
        d = msg.document
        return {"file_id": d.file_id, "file_type": "document",
                "file_name": d.file_name, "mime_type": d.mime_type,
                "size_mb": (d.file_size or 0) / 1024 / 1024,
                "thumb_file_id": d.thumbnail.file_id if d.thumbnail else None}
    if msg.audio:
        a = msg.audio
        return {"file_id": a.file_id, "file_type": "audio",
                "file_name": a.file_name, "mime_type": a.mime_type,
                "size_mb": (a.file_size or 0) / 1024 / 1024, "thumb_file_id": None}
    if msg.voice:
        return {"file_id": msg.voice.file_id, "file_type": "voice",
                "file_name": "voice.ogg", "mime_type": "audio/ogg",
                "size_mb": (msg.voice.file_size or 0) / 1024 / 1024, "thumb_file_id": None}
    if msg.video_note:
        vn = msg.video_note
        return {"file_id": vn.file_id, "file_type": "video_note",
                "file_name": None, "mime_type": "video/mp4",
                "size_mb": (vn.file_size or 0) / 1024 / 1024,
                "thumb_file_id": vn.thumbnail.file_id if vn.thumbnail else None}
    if msg.animation:
        a = msg.animation
        return {"file_id": a.file_id, "file_type": "animation",
                "file_name": a.file_name, "mime_type": a.mime_type,
                "size_mb": (a.file_size or 0) / 1024 / 1024,
                "thumb_file_id": a.thumbnail.file_id if a.thumbnail else None}
    if msg.sticker:
        s = msg.sticker
        return {"file_id": s.file_id, "file_type": "sticker",
                "file_name": None, "mime_type": "image/webp",
                "size_mb": (s.file_size or 0) / 1024 / 1024,
                "thumb_file_id": s.thumbnail.file_id if s.thumbnail else None}
    return None


async def _create_link(msg, user_id: int, user_name: str) -> str | None:
    fi = extract_file_info(msg)
    if not fi:
        return None
    from utils.token import generate_token
    token = generate_token(14)
    cap   = msg.caption or msg.text or ""
    create_media_link(
        token=token, file_id=fi["file_id"], file_type=fi["file_type"],
        file_name=fi.get("file_name"), mime_type=fi.get("mime_type"),
        thumb_file_id=fi.get("thumb_file_id"), caption=cap,
        uploader_id=user_id, uploader_name=user_name,
    )
    return token


# ══════════════════════════════════════════════════════════════════════════════
# /start — deep link album serving
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

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

    if not await gate(update, ctx):
        return

    mode_label = {"all": "Tất cả", "admin": "Chỉ Admin",
                  "whitelist": "Danh sách"}.get(_upload_mode(), _upload_mode())

    await update.message.reply_text(
        f"👋 Xin chào *{user.first_name}*!\n\n"
        "🤖 *Forum Converter Bot*\n\n"
        "📌 *Cách dùng:*\n"
        "• Gửi ảnh/video/file → bot tạo link chia sẻ\n"
        "• Click link → bot gửi media gốc về\n"
        "• /help — xem toàn bộ lệnh\n\n"
        f"🔒 Quyền upload: `{mode_label}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🌐 Web Admin", url=BASE_URL)
        ]])
    )


async def _serve_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE, album: dict):
    """Gửi album media gốc cho user khi click deep link."""
    chat_id     = update.effective_chat.id
    src_chat_id = album["src_chat_id"]
    src_msg_ids = album["src_msg_ids"]

    if not src_msg_ids:
        await update.message.reply_text("❌ Album trống.")
        return

    # copy_messages: gửi tất cả cùng lúc → Telegram tự group thành album
    try:
        result = await ctx.bot.copy_messages(
            chat_id=chat_id,
            from_chat_id=src_chat_id,
            message_ids=src_msg_ids,
        )
        if result:
            return
    except Exception as e:
        logger.warning(f"copy_messages failed: {e} — trying forward_messages")

    # Fallback: forward_messages (có "Forwarded from")
    try:
        result = await ctx.bot.forward_messages(
            chat_id=chat_id,
            from_chat_id=src_chat_id,
            message_ids=src_msg_ids,
        )
        if result:
            return
    except Exception as e:
        logger.warning(f"forward_messages failed: {e} — individual fallback")

    # Last resort: từng message riêng lẻ
    sent = 0
    for mid in src_msg_ids:
        try:
            await ctx.bot.copy_message(chat_id=chat_id,
                                       from_chat_id=src_chat_id, message_id=mid)
            sent += 1
            await asyncio.sleep(0.2)
        except Exception:
            try:
                await ctx.bot.forward_message(chat_id=chat_id,
                                              from_chat_id=src_chat_id, message_id=mid)
                sent += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(f"Cannot serve msg {mid}: {e}")

    if sent == 0:
        await update.message.reply_text(
            "❌ Không thể gửi media.\n"
            "Bot cần được thêm vào forum nguồn, hoặc file đã bị xóa."
        )


# ══════════════════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    admin_txt = ""
    if is_admin(update.effective_user.id):
        admin_txt = (
            "\n*🔑 Admin:*\n"
            "• /clone\\_topic `[tên]` – Clone topic sang forum mới\n"
            "• /forcejoin `@kênh` | off – Bật/tắt force-join\n"
            "• /allow `/disallow /whitelist` – Quản lý whitelist\n"
            "• /stats /links /del\\_link – Thống kê & link\n"
            "• /settings /set – Cài đặt\n"
        )
    await update.message.reply_text(
        "📖 *Hướng dẫn:*\n\n"
        "• /start – Bắt đầu\n"
        "• /mylinks – Link của tôi\n"
        "• /share – Reply vào media → tạo link\n"
        "• /forward – Reply → forward có tên\n"
        "• /fwd\\_anon – Reply → forward ẩn tên\n\n"
        "📁 Gửi ảnh/video/file trực tiếp → bot tạo link chia sẻ\n"
        + admin_txt,
        parse_mode=ParseMode.MARKDOWN
    )


# ══════════════════════════════════════════════════════════════════════════════
# /mylinks
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_mylinks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid  = update.effective_user.id
    conn = get_conn()
    rows = conn.execute(
        "SELECT token, file_type, access_count, created_at FROM media_links "
        "WHERE uploader_id=? AND is_active=1 ORDER BY created_at DESC LIMIT 5",
        (uid,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 Bạn chưa có link nào.")
        return
    lines = [
        f"• `{r['token']}` — {r['file_type']} — 👁️{r['access_count']}\n"
        f"  [{build_share_url(r['token'])}]({build_share_url(r['token'])})"
        for r in rows
    ]
    await update.message.reply_text(
        "🔗 *Link của bạn (5 gần nhất):*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# /share
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    user = update.effective_user
    ok, reason = can_upload(user.id)
    if not ok:
        await update.message.reply_text(reason)
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply vào media rồi dùng /share")
        return
    target = update.message.reply_to_message
    fi = extract_file_info(target)
    if not fi:
        await update.message.reply_text("❌ Không phải media được hỗ trợ.")
        return
    if fi.get("size_mb", 0) > _max_mb():
        await update.message.reply_text(f"❌ File quá lớn. Giới hạn: {_max_mb()} MB.")
        return
    token = await _create_link(target, user.id, user.full_name)
    if not token:
        return
    url      = build_share_url(token)
    orig_cap = target.caption or target.text or ""
    await update.message.reply_text(
        _caption_with_link(orig_cap, url),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Nhấp để xem", url=url)
        ]])
    )


# ══════════════════════════════════════════════════════════════════════════════
# /forward + /fwd_anon
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply vào tin nhắn muốn forward rồi dùng /forward")
        return
    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình DEST_FORUM_ID.")
        return
    target = update.message.reply_to_message
    try:
        sent = await ctx.bot.forward_message(
            chat_id=DEST_FORUM_ID,
            from_chat_id=target.chat_id,
            message_id=target.message_id
        )
        log_forward(source_chat_id=target.chat_id, source_msg_id=target.message_id,
                    dest_chat_id=DEST_FORUM_ID, dest_msg_id=sent.message_id, forward_mode="named")
        await update.message.reply_text("✅ Forward thành công (có tên)")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def cmd_fwd_anon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply vào tin nhắn muốn forward ẩn danh")
        return
    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình DEST_FORUM_ID.")
        return
    target = update.message.reply_to_message
    try:
        sent = await ctx.bot.copy_message(
            chat_id=DEST_FORUM_ID,
            from_chat_id=target.chat_id,
            message_id=target.message_id
        )
        log_forward(source_chat_id=target.chat_id, source_msg_id=target.message_id,
                    dest_chat_id=DEST_FORUM_ID, dest_msg_id=sent.message_id, forward_mode="anonymous")
        await update.message.reply_text("✅ Forward ẩn danh thành công")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_clone_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if not DEST_FORUM_ID:
        await update.message.reply_text("⚠️ Chưa cấu hình DEST_FORUM_ID.")
        return
    topic_name = " ".join(ctx.args) if ctx.args else f"Topic-{update.message.message_thread_id}"
    try:
        t = await ctx.bot.create_forum_topic(chat_id=DEST_FORUM_ID, name=topic_name)
        upsert_topic(
            source_chat_id=update.effective_chat.id,
            source_topic_id=update.message.message_thread_id,
            dest_chat_id=DEST_FORUM_ID,
            dest_topic_id=t.message_thread_id,
            topic_name=topic_name
        )
        await update.message.reply_text(
            f"✅ Clone topic: `{topic_name}` → ID đích: `{t.message_thread_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    links  = list_media_links(limit=9999)
    albums = list_media_albums(limit=9999)
    topics = list_topics(limit=9999)
    logs   = list_forward_logs(limit=9999)
    await update.message.reply_text(
        "📊 *Thống kê:*\n\n"
        f"🔗 Link: `{len(links)}` (👁️{sum(l.get('access_count',0) for l in links)})\n"
        f"📦 Albums: `{len(albums)}` (👁️{sum(a.get('access_count',0) for a in albums)})\n"
        f"📋 Topics: `{len(topics)}`\n"
        f"↩️ Forwards: `{len(logs)}`\n"
        f"🔒 Upload mode: `{_upload_mode()}`",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    links = list_media_links(limit=10)
    if not links:
        await update.message.reply_text("📭 Chưa có link.")
        return
    lines = [
        f"• `{l['token']}` – {l['file_type']} – 👁️{l['access_count']}\n"
        f"  [{build_share_url(l['token'])}]({build_share_url(l['token'])})"
        for l in links
    ]
    await update.message.reply_text(
        "🔗 *10 link gần nhất:*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
    )


async def cmd_del_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /del_link <token>")
        return
    delete_media_link(ctx.args[0])
    await update.message.reply_text(f"✅ Đã xóa `{ctx.args[0]}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    s = all_settings()
    lines = "\n".join(f"• `{k}` = `{v}`" for k, v in s.items()) if s else "_Chưa có_"
    fj = get_setting("force_join_channel", "") or "_(tắt)_"
    await update.message.reply_text(
        "⚙️ *Cài đặt:*\n\n" + lines + "\n\n"
        f"📢 Force-join: `{fj}`\n\n"
        "Key hữu ích:\n"
        "`upload_mode` (all/admin/whitelist)\n"
        "`max_file_mb` `rate_limit`\n"
        "`force_join_channel` `force_join_check_sec`\n"
        "Dùng /set để thay đổi",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /set <key> <value>")
        return
    set_setting(ctx.args[0], " ".join(ctx.args[1:]))
    await update.message.reply_text(f"✅ `{ctx.args[0]}` = `{' '.join(ctx.args[1:])}`",
                                     parse_mode=ParseMode.MARKDOWN)


async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /allow <user_id>")
        return
    _add_whitelist(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Đã thêm `{ctx.args[0]}` vào whitelist.",
                                     parse_mode=ParseMode.MARKDOWN)


async def cmd_disallow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /disallow <user_id>")
        return
    _remove_whitelist(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Đã xóa `{ctx.args[0]}` khỏi whitelist.",
                                     parse_mode=ParseMode.MARKDOWN)


async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    wl = _whitelist()
    if not wl:
        await update.message.reply_text("📋 Whitelist trống. Dùng /allow <user_id>.")
        return
    await update.message.reply_text(
        f"📋 *Whitelist ({len(wl)}):*\n\n" + "\n".join(f"• `{uid}`" for uid in sorted(wl)),
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_forcejoin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Không có quyền.")
        return
    if not ctx.args:
        ch = get_setting("force_join_channel", "") or "_(tắt)_"
        sec = get_setting("force_join_check_sec", "300")
        await update.message.reply_text(
            f"📢 Force-join: `{ch}` (check mỗi `{sec}` giây)\n\n"
            "• `/forcejoin @kênh` – bật\n• `/forcejoin off` – tắt",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    val = ctx.args[0].strip()
    if val.lower() in ("off", "0", "tắt"):
        set_setting("force_join_channel", "")
        await update.message.reply_text("✅ Đã tắt force-join.")
    else:
        if not val.startswith("@") and not val.lstrip("-").isdigit():
            val = "@" + val
        set_setting("force_join_channel", val)
        await update.message.reply_text(
            f"✅ Force-join bật: `{val}`\n⚠️ Bot phải là Admin trong kênh đó.",
            parse_mode=ParseMode.MARKDOWN
        )


# ══════════════════════════════════════════════════════════════════════════════
# MEDIA HANDLER — CHỈ PRIVATE CHAT
# ══════════════════════════════════════════════════════════════════════════════

async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Tạo link chia sẻ khi user gửi media.

    QUAN TRỌNG: Chỉ xử lý trong PRIVATE CHAT.
    Nếu không lọc: bot là admin trong forum đích → nhận mọi ảnh Telethon
    forward vào đó → tự kích hoạt → gửi lại ảnh → spam.
    """
    msg = update.message
    if not msg:
        return

    # CHỈ PRIVATE CHAT — quan trọng nhất
    if update.effective_chat.type != "private":
        return

    if not await gate(update, ctx):
        return

    user = update.effective_user
    fi   = extract_file_info(msg)
    if not fi:
        return

    ok, reason = can_upload(user.id)
    if not ok:
        await msg.reply_text(reason)
        return

    if fi.get("size_mb", 0) > _max_mb():
        await msg.reply_text(f"❌ File quá lớn ({fi['size_mb']:.1f} MB). Giới hạn: {_max_mb()} MB.")
        return

    token = await _create_link(msg, user.id, user.full_name)
    if not token:
        return

    url      = build_share_url(token)
    orig_cap = (msg.caption or msg.text or "").strip()
    full_cap = _caption_with_link(orig_cap, url)

    # Gửi lại: ảnh/thumbnail + caption với link
    if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
        await msg.reply_photo(
            photo=fi["thumb_file_id"],
            caption=full_cap[:1024],
        )
    elif fi["file_type"] == "photo":
        await msg.reply_photo(
            photo=fi["file_id"],
            caption=full_cap[:1024],
        )
    else:
        await msg.reply_text(full_cap[:4096])


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY
# ══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    user = update.effective_user

    if data == "check_membership":
        invalidate_membership_cache(user.id)
        from bot.membership import check_user
        ok = await check_user(ctx.bot, user.id)
        if ok:
            await q.message.edit_text("✅ Xác nhận thành công! Dùng /start để bắt đầu.")
        else:
            await q.answer("❌ Bạn vẫn chưa tham gia kênh.", show_alert=True)
