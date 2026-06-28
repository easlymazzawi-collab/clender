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
import re
import time
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config.settings import (
    ADMIN_IDS, DEST_FORUM_ID, BASE_URL,
    LINK_CAPTION_TEMPLATE,
    BOT_USERNAME, MAX_FILE_SIZE_MB,
)
from database.models import (
    list_media_links,
    upsert_topic, list_topics, list_forward_logs,
    log_forward, get_setting, set_setting, all_settings,
    peek_media_album, increment_album_access, validate_album_access,
    resolve_share_record, get_media_link, list_media_albums, get_conn,
    create_album_from_file_ids, delete_share_token,
    record_user, list_user_ids, count_users, mark_user_blocked,
    update_album_settings, get_album_settings,
)
from utils.token import generate_numeric_token

# Admin đang chờ nhập nội dung broadcast: {admin_id: True}
_broadcast_pending: set = set()

# Chống spam bấm link: {user_id: [timestamps]}
_serve_times: dict = defaultdict(list)
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


def _show_thumbnail() -> bool:
    """Mặc định TẮT — bot chỉ trả link. Bật qua /panel hoặc /set show_thumbnail 1."""
    return get_setting("show_thumbnail", "0") == "1"


def _check_serve_rate(user_id: int) -> tuple[bool, int]:
    """
    Chống spam bấm link. Giới hạn số lần serve / phút mỗi user.
    Setting: serve_per_min (mặc định 15, 0 = không giới hạn).
    Trả về (cho_phép, giây_chờ).
    """
    limit = int(get_setting("serve_per_min", "15") or 15)
    if limit <= 0:
        return True, 0
    now   = time.time()
    times = [t for t in _serve_times[user_id] if now - t < 60]
    _serve_times[user_id] = times
    if len(times) >= limit:
        wait = int(60 - (now - times[0])) + 1
        return False, wait
    _serve_times[user_id].append(now)
    return True, 0


# ══════════════════════════════════════════════════════════════════════════════
# SHARE LINK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def build_share_url(token: str) -> str:
    """
    Tạo link chia sẻ theo LINK_MODE:
      web → BASE_URL/d/TOKEN (bền vững, đổi bot không chết link)
      bot → t.me/BOT_USERNAME?start=TOKEN (trực tiếp)
    """
    from config.settings import LINK_MODE
    if LINK_MODE == "web" and BASE_URL:
        return f"{BASE_URL.rstrip('/')}/d/{token}"
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start={token}"
    return f"{BASE_URL.rstrip('/')}/d/{token}"


# ── Menu cấu hình link (hết hạn / giới hạn xem / cho forward) ────────────────

def _fmt_expiry(expires_at) -> str:
    if not expires_at:
        return "Vĩnh viễn"
    import datetime as _dt
    left = expires_at - time.time()
    if left <= 0:
        return "Đã hết hạn"
    h = int(left // 3600)
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    if h >= 1:
        return f"{h}h"
    return f"{int(left // 60)}m"


def _link_config_kb(token: str) -> InlineKeyboardMarkup:
    """Menu nút bấm cấu hình link."""
    s = get_album_settings(token) or {}
    exp_label = _fmt_expiry(s.get("expires_at"))
    mv        = s.get("max_views") or 0
    mv_label  = "Không" if mv == 0 else str(mv)
    fwd       = s.get("allow_forward", 1)
    fwd_label = "✅ Có" if fwd else "🚫 Không"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱ Hết hạn: {exp_label}", callback_data=f"cfg:{token}:exp_menu")],
        [InlineKeyboardButton(f"👁 Giới hạn xem: {mv_label}", callback_data=f"cfg:{token}:view_menu")],
        [InlineKeyboardButton(f"↪️ Cho forward: {fwd_label}", callback_data=f"cfg:{token}:fwd_toggle")],
        [InlineKeyboardButton("✅ Xong", callback_data=f"cfg:{token}:done")],
    ])


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
    from utils.token import generate_numeric_token
    token = generate_numeric_token(16)
    cap   = msg.caption or msg.text or ""
    create_album_from_file_ids(
        token,
        [{"type": fi["file_type"], "file_id": fi["file_id"]}],
        cap,
    )
    return token


# ══════════════════════════════════════════════════════════════════════════════
# /start — deep link album serving
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user.id, user.full_name, user.username or "")

    if ctx.args:
        token = ctx.args[0].strip()
        if not await gate(update, ctx):
            # Bị chặn vì chưa join kênh → lưu token để serve sau khi join
            from bot.membership import set_pending_token
            set_pending_token(user.id, token)
            return
        # Chống spam bấm link
        ok, wait = _check_serve_rate(user.id)
        if not ok:
            await update.message.reply_text(
                f"⏳ Bạn đang bấm quá nhanh! Vui lòng chờ {wait} giây rồi thử lại."
            )
            return
        album = resolve_share_record(token)
        if not album:
            await update.message.reply_text("❌ Link không hợp lệ hoặc đã hết hạn.")
            return

        if album.get("_source") == "link":
            link = get_media_link(token)
            if link:
                await _serve_legacy_link(update, ctx, link)
            return

        ok_access, reason = validate_album_access(album)
        if not ok_access:
            if reason == "expired":
                await update.message.reply_text("⏱ Link này đã hết hạn.")
            else:
                await update.message.reply_text(
                    f"👁 Link đã đạt giới hạn {album.get('max_views', 0)} lượt xem."
                )
            return

        increment_album_access(token)
        album["access_count"] = album.get("access_count", 0) + 1
        await _serve_album(update, ctx, album)
        return

    if not await gate(update, ctx):
        return

    # Member: chào mừng tối giản
    if not is_admin(user.id):
        await update.message.reply_text(
            f"👋 Xin chào *{user.first_name}*!\n\n"
            "🤖 Đây là bot chia sẻ media.\n"
            "👀 Bấm vào link chia sẻ để xem nội dung — bot sẽ gửi về cho bạn.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Admin: đầy đủ
    kb = None
    if _is_public_url(BASE_URL):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Web Admin", url=BASE_URL)]])
    await update.message.reply_text(
        f"👋 Xin chào *{user.first_name}* (Admin)!\n\n"
        "🤖 *Forum Converter Bot*\n\n"
        "📌 *Cách dùng:*\n"
        "• Gửi media → tạo link chia sẻ\n"
        "• /panel — bảng điều khiển\n"
        "• /help — toàn bộ lệnh\n\n"
        f"🔒 Quyền upload: `{_upload_mode()}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _serve_legacy_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, record: dict):
    """Serve media_links cũ (file_id) — tương thích /share trước đây."""
    chat_id = update.effective_chat.id
    ft = record.get("file_type", "document")
    fid = record.get("file_id")
    cap = record.get("caption") or ""
    if not fid:
        await update.message.reply_text("❌ Link không còn media.")
        return
    kw = {"caption": cap[:1024]} if cap else {}
    try:
        if ft == "photo":
            await ctx.bot.send_photo(chat_id=chat_id, photo=fid, **kw)
        elif ft in ("video", "animation", "video_note"):
            await ctx.bot.send_video(chat_id=chat_id, video=fid, **kw)
        elif ft == "audio":
            await ctx.bot.send_audio(chat_id=chat_id, audio=fid, **kw)
        elif ft == "voice":
            await ctx.bot.send_voice(chat_id=chat_id, voice=fid, **kw)
        else:
            await ctx.bot.send_document(chat_id=chat_id, document=fid, **kw)
    except Exception as e:
        logger.warning(f"_serve_legacy_link: {e}")
        await update.message.reply_text(f"❌ Không gửi được media: {e}")


async def _serve_album(update: Update, ctx: ContextTypes.DEFAULT_TYPE, album: dict):
    """
    Gửi album media gốc cho user khi click deep link t.me/bot?start=TOKEN.

    Yêu cầu: Bot phải là member/admin trong forum NGUỒN (src_chat_id)
    để có thể copy/forward messages từ đó về cho user.
    """
    user        = update.effective_user
    chat_id     = update.effective_chat.id
    reply_to    = update.effective_message   # works cho cả message lẫn callback
    src_chat_id = album["src_chat_id"]
    src_msg_ids = album["src_msg_ids"]
    file_ids    = album.get("file_ids") or []
    protect     = not album.get("allow_forward", 1)   # chặn forward nếu tắt

    # ── Trường hợp media nhận trực tiếp qua bot → gửi lại bằng file_id ────────
    if file_ids:
        from telegram import (
            InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
        )
        cap = album.get("caption") or ""
        media_group = []
        for i, f in enumerate(file_ids):
            c = cap if i == len(file_ids) - 1 else None
            t = f.get("type")
            fid = f.get("file_id")
            if t == "photo":
                media_group.append(InputMediaPhoto(fid, caption=c))
            elif t in ("video", "animation", "video_note"):
                media_group.append(InputMediaVideo(fid, caption=c))
            elif t == "audio":
                media_group.append(InputMediaAudio(fid, caption=c))
            else:
                media_group.append(InputMediaDocument(fid, caption=c))
        try:
            if len(media_group) == 1:
                m = file_ids[0]
                send_map = {
                    "photo": ctx.bot.send_photo, "video": ctx.bot.send_video,
                    "animation": ctx.bot.send_animation, "audio": ctx.bot.send_audio,
                    "voice": ctx.bot.send_voice, "document": ctx.bot.send_document,
                }
                fn = send_map.get(m.get("type"), ctx.bot.send_document)
                kw = {"caption": cap} if cap else {}
                key = {"photo":"photo","video":"video","animation":"animation",
                       "audio":"audio","voice":"voice"}.get(m.get("type"),"document")
                await fn(chat_id=chat_id, **{key: m.get("file_id")},
                         protect_content=protect, **kw)
            else:
                await ctx.bot.send_media_group(chat_id=chat_id, media=media_group,
                                               protect_content=protect)
            logger.info(f"serve_album OK via file_ids ({len(file_ids)})")
            return
        except Exception as e:
            logger.warning(f"serve via file_ids failed: {e}")

    n = len(src_msg_ids)
    logger.info(f"serve_album: user={user.id} src={src_chat_id} msgs={src_msg_ids}")

    if not src_msg_ids:
        await reply_to.reply_text("❌ Album trống.")
        return

    # ── Thử 1: copy_messages (batch, không có 'Forwarded from') ──────────────
    from telegram.error import RetryAfter
    for attempt in range(3):
        try:
            result = await ctx.bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=src_chat_id,
                message_ids=src_msg_ids,
                protect_content=protect,
            )
            if result:
                logger.info(f"serve_album OK via copy_messages ({n} msgs)")
                return
            break
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3)) + 1
            logger.warning(f"copy_messages FloodWait {wait}s (lần {attempt+1}/3)")
            if attempt == 0:
                try:
                    await reply_to.reply_text(
                        f"⏳ Bot đang bận, chờ {wait}s rồi gửi…"
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait)
        except Exception as e:
            logger.warning(f"copy_messages from {src_chat_id}: {type(e).__name__}: {e}")
            break

    # ── Thử 2: forward_messages (batch, có 'Forwarded from') ─────────────────
    try:
        result = await ctx.bot.forward_messages(
            chat_id=chat_id,
            from_chat_id=src_chat_id,
            message_ids=src_msg_ids,
            protect_content=protect,
        )
        if result:
            logger.info(f"serve_album OK via forward_messages ({n} msgs)")
            return
    except Exception as e:
        logger.warning(f"forward_messages from {src_chat_id}: {type(e).__name__}: {e}")

    # ── Thử 3: từng message riêng lẻ ─────────────────────────────────────────
    sent = 0
    for mid in src_msg_ids:
        try:
            await ctx.bot.copy_message(
                chat_id=chat_id, from_chat_id=src_chat_id, message_id=mid,
                protect_content=protect,
            )
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e1:
            try:
                await ctx.bot.forward_message(
                    chat_id=chat_id, from_chat_id=src_chat_id, message_id=mid
                )
                sent += 1
                await asyncio.sleep(0.2)
            except Exception as e2:
                logger.warning(f"Cannot serve msg {mid}: {e2}")

    if sent == 0:
        await reply_to.reply_text(
            "❌ Không thể gửi media.\n\n"
            "Nguyên nhân có thể:\n"
            "• Bot chưa được thêm vào forum nguồn\n"
            "• File đã bị xóa ở nguồn\n"
            "• Forum nguồn bị khóa forward\n\n"
            f"Chat nguồn: `{src_chat_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.error(f"serve_album FAILED: src={src_chat_id} msgs={src_msg_ids}")
    elif sent < n:
        await reply_to.reply_text(
            f"⚠️ Đã gửi {sent}/{n} file. {n - sent} file không khả dụng."
        )
        logger.info(f"serve_album partial: {sent}/{n}")


# ══════════════════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return

    # Member: hướng dẫn tối giản — chỉ bấm link xem
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "📖 *Hướng dẫn:*\n\n"
            "👀 Bấm vào link chia sẻ để xem media.\n"
            "Bot sẽ gửi nội dung về cho bạn.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Admin: đầy đủ
    await update.message.reply_text(
        "📖 *Hướng dẫn (Admin):*\n\n"
        "• /panel – 🎛 Bảng điều khiển (nút bấm)\n"
        "• /mylinks – Link của tôi\n"
        "• /share – Reply vào media → tạo link\n"
        "• /forward – Reply → forward có tên\n"
        "• /fwd\\_anon – Reply → forward ẩn tên\n"
        "• /clone\\_topic `[tên]` – Clone topic sang forum mới\n"
        "• /forcejoin `@kênh` | off – Bật/tắt force-join\n"
        "• /allow `/disallow /whitelist` – Whitelist\n"
        "• /stats /links /del\\_link /set – Quản lý\n\n"
        "📁 Gửi media trực tiếp → tạo link chia sẻ",
        parse_mode=ParseMode.MARKDOWN
    )


# ══════════════════════════════════════════════════════════════════════════════
# /mylinks
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_mylinks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return   # member không có quyền — im lặng
    if not await gate(update, ctx):
        return
    conn = get_conn()
    rows = conn.execute(
        "SELECT token, access_count, created_at FROM media_albums "
        "WHERE is_active=1 ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 Bạn chưa có link nào.")
        return
    lines = [
        f"• `{r['token']}` — 📦 album — 👁️{r['access_count']}\n"
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
    if not is_admin(update.effective_user.id):
        return   # member không có quyền tạo link
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
    if not is_admin(update.effective_user.id):
        return   # member không có quyền forward
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
    if not is_admin(update.effective_user.id):
        return   # member không có quyền forward
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
    if delete_share_token(ctx.args[0]):
        await update.message.reply_text(f"✅ Đã xóa `{ctx.args[0]}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Không tìm thấy token.")


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
        return

    val = _normalize_channel(val)

    # Kiểm tra ngay: bot có truy cập được kênh không?
    try:
        chat = await ctx.bot.get_chat(val)
        # Kiểm tra bot có phải member/admin để xem được danh sách thành viên
        try:
            me = await ctx.bot.get_me()
            cm = await ctx.bot.get_chat_member(val, me.id)
            bot_in = cm.status in ("member", "administrator", "creator")
        except Exception:
            bot_in = False

        set_setting("force_join_channel", val)
        warn = "" if bot_in else (
            "\n\n⚠️ *Bot CHƯA ở trong kênh này!*\n"
            "Hãy thêm bot vào kênh `" + val + "` (làm member hoặc admin)\n"
            "nếu không bot không kiểm tra được thành viên."
        )
        await update.message.reply_text(
            f"✅ Force-join bật: `{val}`\n"
            f"📋 Tên kênh: {chat.title}{warn}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Bot không truy cập được kênh `{val}`\n"
            f"Lỗi: {e}\n\n"
            "Kiểm tra:\n"
            "• Username/link đúng chưa?\n"
            "• Kênh public, hoặc bot đã được thêm vào kênh private?\n\n"
            "Force-join CHƯA được bật.",
            parse_mode=ParseMode.MARKDOWN
        )


def _normalize_channel(val: str) -> str:
    """
    Chuẩn hóa input kênh về dạng @username hoặc -100xxx.
    Chấp nhận: @user, user, https://t.me/user, t.me/user, -100xxx
    """
    val = val.strip()
    # Link t.me/username hoặc https://t.me/username
    m = re.search(r"t\.me/([A-Za-z]\w{3,31})", val)
    if m:
        return "@" + m.group(1)
    # Link t.me/c/123 (private) → -100123
    m = re.search(r"t\.me/c/(\d+)", val)
    if m:
        return f"-100{m.group(1)}"
    # ID âm
    if val.lstrip("-").isdigit():
        return val
    # username thuần
    return val if val.startswith("@") else "@" + val


# ══════════════════════════════════════════════════════════════════════════════
# MEDIA HANDLER — CHỈ PRIVATE CHAT
# ══════════════════════════════════════════════════════════════════════════════

# Buffer album theo media_group_id: {key: {"items":[...], "task":..., "msg":...}}
_album_buf: dict = {}
_ALBUM_FLUSH_DELAY = 1.5   # giây chờ gom đủ các phần của album


async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Tạo link chia sẻ khi user gửi/forward media vào bot (PRIVATE CHAT).

    Album (media_group_id): gom tất cả phần → 1 token → 1 link duy nhất.
    Single: 1 token → 1 link.

    CHỈ PRIVATE CHAT — tránh tự kích hoạt khi bot là admin forum đích.
    """
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return

    user = update.effective_user

    # Admin đang broadcast → media này là nội dung broadcast
    if user.id in _broadcast_pending and is_admin(user.id):
        _broadcast_pending.discard(user.id)
        await _do_broadcast(ctx, msg, user)
        return

    if not await gate(update, ctx):
        return

    record_user(user.id, user.full_name, user.username or "")
    fi   = extract_file_info(msg)
    if not fi:
        return

    # Member chỉ được xem — không tạo link bằng cách gửi media
    if not is_admin(user.id):
        await msg.reply_text(
            "👀 Bạn chỉ có thể bấm vào link để xem media.\n"
            "Tính năng tạo link chỉ dành cho admin."
        )
        return

    ok, reason = can_upload(user.id)
    if not ok:
        await msg.reply_text(reason)
        return
    if fi.get("size_mb", 0) > _max_mb():
        await msg.reply_text(f"❌ File quá lớn ({fi['size_mb']:.1f} MB). Giới hạn: {_max_mb()} MB.")
        return

    # ── Album: gom theo media_group_id ────────────────────────────────────────
    # Lưu message_id (không phải file_id) để serve bằng copy_messages
    # → giữ NGUYÊN caption premium emoji + entities.
    mgid = msg.media_group_id
    if mgid:
        key = f"{user.id}_{mgid}"
        entry = _album_buf.get(key)
        if entry and entry.get("task"):
            entry["task"].cancel()
        if not entry:
            entry = {"msg_ids": [], "msg": msg, "first_fi": fi}
            _album_buf[key] = entry
        entry["msg_ids"].append(msg.message_id)
        entry["task"] = asyncio.create_task(_flush_album(key, ctx, user))
        return

    # ── Single media → 1 link (lưu message_id, serve qua copy_messages) ───────
    from database.models import create_media_album
    token = generate_numeric_token(16)
    create_media_album(token, user.id, [msg.message_id], msg.caption or "")

    url      = build_share_url(token)
    orig_cap = (msg.caption or msg.text or "").strip()
    full_cap = _caption_with_link(orig_cap, url)

    # Mặc định: chỉ trả link (text). Bật show_thumbnail → kèm thumbnail/ảnh
    if _show_thumbnail():
        if fi["file_type"] in ("video", "video_note", "animation") and fi.get("thumb_file_id"):
            await msg.reply_photo(photo=fi["thumb_file_id"], caption=full_cap[:1024])
        elif fi["file_type"] == "photo":
            await msg.reply_photo(photo=fi["file_id"], caption=full_cap[:1024])
        else:
            await msg.reply_text(full_cap[:4096])
    else:
        await msg.reply_text(full_cap[:4096], disable_web_page_preview=False)

    # Menu cấu hình link (hết hạn / giới hạn xem / forward)
    await msg.reply_text(
        "⚙️ *Tuỳ chỉnh link* (tuỳ chọn):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_link_config_kb(token),
    )


async def _flush_album(key: str, ctx: ContextTypes.DEFAULT_TYPE, user):
    """Sau khi gom đủ album → tạo 1 token (lưu message_ids) + trả 1 link."""
    try:
        await asyncio.sleep(_ALBUM_FLUSH_DELAY)
    except asyncio.CancelledError:
        return

    entry = _album_buf.pop(key, None)
    if not entry or not entry["msg_ids"]:
        return

    from database.models import create_media_album
    msg_ids = entry["msg_ids"]
    msg     = entry["msg"]
    fi      = entry["first_fi"]

    # Lưu message_ids trong chat user↔bot → serve bằng copy_messages
    # (giữ premium emoji + album nguyên vẹn)
    token = generate_numeric_token(16)
    create_media_album(token, user.id, msg_ids, msg.caption or "")

    url      = build_share_url(token)
    full_cap = _caption_with_link((msg.caption or "").strip(), url)

    # Mặc định: chỉ trả link. Bật show_thumbnail → kèm ảnh/thumbnail đầu album
    try:
        if _show_thumbnail():
            if fi["file_type"] == "photo":
                await msg.reply_photo(photo=fi["file_id"], caption=full_cap[:1024])
            elif fi.get("thumb_file_id"):
                await msg.reply_photo(photo=fi["thumb_file_id"], caption=full_cap[:1024])
            else:
                await msg.reply_text(f"📦 Album {len(msg_ids)} media\n\n{full_cap}"[:4096])
        else:
            await msg.reply_text(f"📦 Album {len(msg_ids)} media\n\n{full_cap}"[:4096])
    except Exception as e:
        logger.warning(f"_flush_album reply: {e}")
        await msg.reply_text(full_cap[:4096])

    # Menu cấu hình link
    try:
        await msg.reply_text(
            "⚙️ *Tuỳ chỉnh link* (tuỳ chọn):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_link_config_kb(token),
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# BROADCAST
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Bắt text trong private chat.
    - Admin đang chờ broadcast → nội dung này là broadcast
    - Còn lại → gợi ý cách dùng (text thuần không tạo link được)
    """
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return
    user = update.effective_user

    if not await gate(update, ctx):
        return

    # Admin đang broadcast → phát nội dung
    if user.id in _broadcast_pending and is_admin(user.id):
        _broadcast_pending.discard(user.id)
        await _do_broadcast(ctx, msg, user)
        return

    record_user(user.id, user.full_name, user.username or "")

    # Text thuần (không media) → gợi ý nhẹ
    kb = None
    if is_admin(user.id):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎛 Mở bảng điều khiển", callback_data="panel:main")
        ]])
    await msg.reply_text(
        "💡 Gửi *ảnh / video / file* vào đây để tạo link chia sẻ.\n"
        "Hoặc forward bài (kèm media) → bot trả link tự động.\n\n"
        "Text thuần không tạo được link.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _do_broadcast(ctx: ContextTypes.DEFAULT_TYPE, msg, admin):
    """
    Phát nội dung msg tới tất cả NGƯỜI DÙNG đã dùng bot (copy_message giữ định dạng).
    KHÔNG gửi cho admin (ADMIN_IDS).
    """
    user_ids = [uid for uid in list_user_ids(only_active=True)
                if uid not in ADMIN_IDS]
    total    = len(user_ids)
    notice = await msg.reply_text(
        f"📨 Đang gửi tới {total} người dùng (đã loại {len(ADMIN_IDS)} admin)…"
    )

    sent = fail = 0
    for uid in user_ids:
        try:
            await ctx.bot.copy_message(
                chat_id=uid,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            sent += 1
        except Exception as e:
            fail += 1
            em = str(e).lower()
            if "blocked" in em or "deactivated" in em or "not found" in em:
                mark_user_blocked(uid)
        if (sent + fail) % 20 == 0:
            await asyncio.sleep(1)   # tránh flood
        else:
            await asyncio.sleep(0.05)

    try:
        await notice.edit_text(
            f"✅ *Broadcast xong!*\n\n"
            f"📤 Gửi thành công: `{sent}`\n"
            f"❌ Thất bại (chặn bot): `{fail}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY
# ══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    user = update.effective_user

    if data == "check_membership":
        invalidate_membership_cache(user.id)
        from bot.membership import check_user, pop_pending_token
        ok = await check_user(ctx.bot, user.id)
        if not ok:
            await q.answer("❌ Bạn vẫn chưa tham gia kênh. Hãy join rồi bấm lại.",
                           show_alert=True)
            return
        await q.answer("✅ OK")
        token = pop_pending_token(user.id)
        if token:
            album = resolve_share_record(token)
            if album and album.get("_source") == "album":
                ok_access, reason = validate_album_access(album)
                if ok_access:
                    await q.message.edit_text("✅ Xác nhận thành công! Đang gửi media…")
                    ok2, wait = _check_serve_rate(user.id)
                    if ok2:
                        increment_album_access(token)
                        album["access_count"] = album.get("access_count", 0) + 1
                        await _serve_album(update, ctx, album)
                    else:
                        await q.message.reply_text(f"⏳ Chờ {wait}s rồi bấm lại link.")
                    return
                elif reason == "expired":
                    await q.message.edit_text("⏱ Link này đã hết hạn.")
                    return
        await q.message.edit_text(
            "✅ Xác nhận thành công! Bấm lại link để xem media."
        )
        return

    await q.answer()
    if data.startswith("panel:"):
        if not is_admin(user.id):
            await q.answer("🚫 Không có quyền.", show_alert=True)
            return
        await _handle_panel(q, ctx, data[len("panel:"):])
        return

    # ── Link config callbacks (cfg:TOKEN:action[:value]) ────────────────────────
    if data.startswith("cfg:"):
        if not is_admin(user.id):
            await q.answer("🚫 Không có quyền.", show_alert=True)
            return
        await _handle_link_config(q, data[len("cfg:"):])
        return


async def _handle_link_config(q, rest: str):
    """Xử lý menu cấu hình link: rest = 'TOKEN:action[:value]'."""
    parts  = rest.split(":")
    token  = parts[0]
    action = parts[1] if len(parts) > 1 else ""

    if action == "done":
        s = get_album_settings(token) or {}
        await q.message.edit_text(
            "✅ *Đã lưu cấu hình link!*\n\n"
            f"⏱ Hết hạn: {_fmt_expiry(s.get('expires_at'))}\n"
            f"👁 Giới hạn xem: {s.get('max_views') or 'Không'}\n"
            f"↪️ Cho forward: {'Có' if s.get('allow_forward',1) else 'Không'}\n\n"
            f"🔗 {build_share_url(token)}",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return

    if action == "exp_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Vĩnh viễn", callback_data=f"cfg:{token}:exp:0"),
             InlineKeyboardButton("1 giờ",     callback_data=f"cfg:{token}:exp:1")],
            [InlineKeyboardButton("6 giờ",      callback_data=f"cfg:{token}:exp:6"),
             InlineKeyboardButton("24 giờ",     callback_data=f"cfg:{token}:exp:24")],
            [InlineKeyboardButton("3 ngày",     callback_data=f"cfg:{token}:exp:72"),
             InlineKeyboardButton("7 ngày",     callback_data=f"cfg:{token}:exp:168")],
            [InlineKeyboardButton("◀️ Quay lại", callback_data=f"cfg:{token}:back")],
        ])
        await q.message.edit_reply_markup(reply_markup=kb)
        return

    if action == "exp":
        hours = int(parts[2])
        exp = 0 if hours == 0 else (time.time() + hours * 3600)
        update_album_settings(token, expires_at=exp)
        await q.answer("✅ Đã đặt hết hạn")
        await q.message.edit_reply_markup(reply_markup=_link_config_kb(token))
        return

    if action == "view_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Không giới hạn", callback_data=f"cfg:{token}:view:0")],
            [InlineKeyboardButton("10",  callback_data=f"cfg:{token}:view:10"),
             InlineKeyboardButton("50",  callback_data=f"cfg:{token}:view:50"),
             InlineKeyboardButton("100", callback_data=f"cfg:{token}:view:100")],
            [InlineKeyboardButton("500", callback_data=f"cfg:{token}:view:500"),
             InlineKeyboardButton("1000",callback_data=f"cfg:{token}:view:1000")],
            [InlineKeyboardButton("◀️ Quay lại", callback_data=f"cfg:{token}:back")],
        ])
        await q.message.edit_reply_markup(reply_markup=kb)
        return

    if action == "view":
        update_album_settings(token, max_views=int(parts[2]))
        await q.answer("✅ Đã đặt giới hạn xem")
        await q.message.edit_reply_markup(reply_markup=_link_config_kb(token))
        return

    if action == "fwd_toggle":
        s = get_album_settings(token) or {}
        update_album_settings(token, allow_forward=not s.get("allow_forward", 1))
        await q.answer("✅ Đã đổi")
        await q.message.edit_reply_markup(reply_markup=_link_config_kb(token))
        return

    if action == "back":
        await q.message.edit_reply_markup(reply_markup=_link_config_kb(token))
        return


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL — giao diện nút bấm
# ══════════════════════════════════════════════════════════════════════════════

def _is_public_url(url: str) -> bool:
    """Telegram không chấp nhận localhost/IP nội bộ trong inline button URL."""
    u = (url or "").lower()
    if not u.startswith(("http://", "https://")):
        return False
    bad = ("localhost", "127.0.0.1", "0.0.0.0", "10.", "192.168.", "://[")
    return not any(b in u for b in bad)


def _panel_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Thống kê", callback_data="panel:stats"),
         InlineKeyboardButton("🔗 Link gần đây", callback_data="panel:links")],
        [InlineKeyboardButton("🔒 Quyền Upload", callback_data="panel:upload"),
         InlineKeyboardButton("📢 Force-Join", callback_data="panel:forcejoin")],
        [InlineKeyboardButton("👥 Whitelist", callback_data="panel:whitelist"),
         InlineKeyboardButton("⚙️ Cài đặt khác", callback_data="panel:settings")],
        [InlineKeyboardButton("📨 Gửi thông báo (broadcast)", callback_data="panel:broadcast")],
    ]
    # Chỉ thêm nút Web Admin nếu BASE_URL là URL công khai hợp lệ
    if _is_public_url(BASE_URL):
        rows.append([InlineKeyboardButton("🌐 Web Admin", url=BASE_URL)])
    return InlineKeyboardMarkup(rows)


def _panel_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Quay lại", callback_data="panel:main")
    ]])


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin control panel với nút bấm."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Bạn không có quyền.")
        return
    await update.message.reply_text(
        "🎛 *BẢNG ĐIỀU KHIỂN ADMIN*\n\nChọn chức năng:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_panel_main_kb()
    )


async def _handle_panel(q, ctx, action: str):
    """Xử lý các nút trong admin panel."""
    if action == "main":
        await q.message.edit_text(
            "🎛 *BẢNG ĐIỀU KHIỂN ADMIN*\n\nChọn chức năng:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=_panel_main_kb()
        )
        return

    if action == "stats":
        links  = list_media_links(limit=9999)
        albums = list_media_albums(limit=9999)
        topics = list_topics(limit=9999)
        logs   = list_forward_logs(limit=9999)
        await q.message.edit_text(
            "📊 *THỐNG KÊ*\n\n"
            f"🔗 Link đơn: `{len(links)}` (👁️{sum(l.get('access_count',0) for l in links)})\n"
            f"📦 Album: `{len(albums)}` (👁️{sum(a.get('access_count',0) for a in albums)})\n"
            f"📋 Topics clone: `{len(topics)}`\n"
            f"↩️ Forwards: `{len(logs)}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=_panel_back_kb()
        )
        return

    if action == "links":
        from database.models import list_all_share_links
        items = list_all_share_links(limit=8)
        if not items:
            txt = "📭 Chưa có link nào."
        else:
            lines = [
                f"• `{l['token']}` — {l.get('share_type', 'link')} — 👁️{l.get('access_count', 0)}"
                for l in items
            ]
            txt = "🔗 *LINK GẦN ĐÂY:*\n\n" + "\n".join(lines)
        await q.message.edit_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=_panel_back_kb())
        return

    if action == "upload":
        mode = _upload_mode()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(("✅ " if mode=="all" else "") + "Tất cả",
                                  callback_data="panel:setmode:all")],
            [InlineKeyboardButton(("✅ " if mode=="admin" else "") + "Chỉ Admin",
                                  callback_data="panel:setmode:admin")],
            [InlineKeyboardButton(("✅ " if mode=="whitelist" else "") + "Whitelist",
                                  callback_data="panel:setmode:whitelist")],
            [InlineKeyboardButton("◀️ Quay lại", callback_data="panel:main")],
        ])
        await q.message.edit_text(
            f"🔒 *QUYỀN UPLOAD*\n\nHiện tại: `{mode}`\n\nChọn chế độ:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    if action.startswith("setmode:"):
        mode = action.split(":", 1)[1]
        set_setting("upload_mode", mode)
        await q.answer(f"✅ Đã đặt: {mode}")
        await _handle_panel(q, ctx, "upload")
        return

    if action == "forcejoin":
        ch  = get_setting("force_join_channel", "") or "(tắt)"
        sec = get_setting("force_join_check_sec", "300")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Tắt Force-Join", callback_data="panel:fj_off")],
            [InlineKeyboardButton("◀️ Quay lại", callback_data="panel:main")],
        ])
        await q.message.edit_text(
            f"📢 *FORCE-JOIN*\n\n"
            f"Kênh: `{ch}`\nCheck mỗi: `{sec}`s\n\n"
            "Để bật/đổi kênh, gõ lệnh:\n"
            "`/forcejoin @kênhcủabạn`\n"
            "(chấp nhận cả link t.me/...)",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    if action == "fj_off":
        set_setting("force_join_channel", "")
        await q.answer("✅ Đã tắt Force-Join")
        await _handle_panel(q, ctx, "forcejoin")
        return

    if action == "whitelist":
        wl = _whitelist()
        body = "\n".join(f"• `{u}`" for u in sorted(wl)) if wl else "_trống_"
        await q.message.edit_text(
            f"👥 *WHITELIST ({len(wl)})*\n\n{body}\n\n"
            "Thêm/xóa bằng lệnh:\n`/allow <id>`  `/disallow <id>`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=_panel_back_kb()
        )
        return

    if action == "settings":
        thumb_on = _show_thumbnail()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                ("🖼️ Thumbnail: BẬT ✅" if thumb_on else "🖼️ Thumbnail: TẮT ❌"),
                callback_data="panel:toggle_thumb")],
            [InlineKeyboardButton("◀️ Quay lại", callback_data="panel:main")],
        ])
        await q.message.edit_text(
            "⚙️ *CÀI ĐẶT KHÁC*\n\n"
            f"🖼️ Hiện thumbnail khi trả link: `{'BẬT' if thumb_on else 'TẮT'}`\n"
            f"📏 Max file: `{_max_mb()} MB`\n"
            f"⏱️ Giới hạn upload: `{get_setting('rate_limit','20')}/giờ`\n"
            f"🛡️ Chống spam bấm link: `{get_setting('serve_per_min','15')}/phút`\n\n"
            "Bấm nút để bật/tắt thumbnail.\n"
            "Đổi giới hạn bằng lệnh:\n"
            "`/set max_file_mb 50`\n"
            "`/set rate_limit 20`\n"
            "`/set serve_per_min 15`  (0 = tắt chống spam)",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    if action == "toggle_thumb":
        new_val = "0" if _show_thumbnail() else "1"
        set_setting("show_thumbnail", new_val)
        await q.answer("✅ Đã " + ("BẬT" if new_val == "1" else "TẮT") + " thumbnail")
        await _handle_panel(q, ctx, "settings")
        return

    if action == "broadcast":
        n = count_users()
        _broadcast_pending.add(q.from_user.id)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Hủy", callback_data="panel:bc_cancel")
        ]])
        await q.message.edit_text(
            f"📨 *GỬI THÔNG BÁO*\n\n"
            f"Sẽ gửi tới `{n}` người dùng đã dùng bot.\n\n"
            "👉 Hãy gửi nội dung muốn broadcast (text/ảnh/video).\n"
            "Tin nhắn tiếp theo bạn gửi sẽ được phát đi.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    if action == "bc_cancel":
        _broadcast_pending.discard(q.from_user.id)
        await _handle_panel(q, ctx, "main")
        return
