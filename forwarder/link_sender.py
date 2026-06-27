"""
Bot Deep-Link sender for Telethon Forwarder.

Cách hoạt động:
  1. Forwarder (Telethon) quét album ở forum nguồn
  2. Lưu {src_chat_id, src_msg_ids[]} vào bảng media_albums với 1 token duy nhất
  3. Tạo bot deep link: https://t.me/BOT_USERNAME?start=TOKEN
  4. Gửi sang forum đích:
       - Video  → thumbnail ảnh + caption "Nhấp vào link để xem: t.me/bot?start=TOKEN"
       - Ảnh    → ảnh đầu tiên của album + caption với link
       - File   → text với link
  5. Khi user click link → mở Telegram → /start TOKEN gửi đến bot
  6. Bot copy toàn bộ album từ forum nguồn về cho user (copy_message)

Ưu điểm:
  - Không cần web server để serve file
  - File được gửi trực tiếp qua Telegram (tốc độ cao)
  - Hỗ trợ album (nhiều ảnh/video trong 1 post) — 1 link duy nhất
  - Bot copy_message: không hiện "Forwarded from" nếu bật hide_sender
"""

import asyncio
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import Message, DocumentAttributeFilename

from utils.token import generate_token
from database.models import create_media_album
from config.settings import BOT_USERNAME, LINK_CAPTION_TEMPLATE

logger = logging.getLogger(__name__)


# ─── Deep link builder ────────────────────────────────────────────────────────

def build_bot_link(token: str) -> str:
    """t.me/BOT_USERNAME?start=TOKEN"""
    username = BOT_USERNAME.lstrip("@")
    return f"https://t.me/{username}?start={token}"


# ─── Media type helpers ───────────────────────────────────────────────────────

def _media_type_of(msg: Message) -> Optional[str]:
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if getattr(msg, "gif", None) or getattr(msg, "animation", None):
        return "animation"
    if msg.video_note:
        return "video_note"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.document:
        return "document"
    if msg.sticker:
        return "sticker"
    return None


def _emoji_for_type(ftype: str) -> str:
    return {
        "photo":      "🖼️",
        "video":      "🎬",
        "animation":  "🎞️",
        "video_note": "📹",
        "audio":      "🎵",
        "voice":      "🎙️",
        "document":   "📄",
        "sticker":    "🎭",
    }.get(ftype, "📁")


def _file_name_of(msg: Message) -> Optional[str]:
    doc = getattr(msg.media, "document", None)
    if not doc:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


# ─── Thumbnail download ───────────────────────────────────────────────────────

async def _dl(client: TelegramClient, msg, **kwargs) -> Optional[bytes]:
    """
    Download media to bytes. Works with Telethon 1.36+.
    Pass file=bytes (the type) to get raw bytes returned directly.
    """
    try:
        data = await client.download_media(msg, file=bytes, **kwargs)
        return data if isinstance(data, (bytes, bytearray)) else None
    except Exception as e:
        logger.debug(f"_dl id={getattr(msg,'id','?')}: {e}")
        return None


async def _download_thumb(client: TelegramClient, msg: Message) -> Optional[bytes]:
    """Download best available thumbnail bytes for a video/document message."""
    try:
        doc = getattr(msg.media, "document", None)
        if doc and getattr(doc, "thumbs", None):
            best = sorted(
                [t for t in doc.thumbs if getattr(t, "w", 0) > 0],
                key=lambda t: t.w, reverse=True,
            )
            if best:
                data = await _dl(client, msg, thumb=best[0])
                if data:
                    return data
        # fallback: let Telethon pick
        return await _dl(client, msg, thumb=-1)
    except Exception as e:
        logger.debug(f"_download_thumb id={msg.id}: {e}")
        return None


# ─── Store album record ───────────────────────────────────────────────────────

def _make_album_token(msgs: list) -> tuple[str, str]:
    """Generate token and store album in DB. Returns (token, bot_link)."""
    token       = generate_token(16)
    src_chat_id = msgs[0].chat_id
    src_msg_ids = [m.id for m in msgs]
    caption     = " ".join(
        filter(None, [m.message for m in msgs if m.message])
    ).strip()
    create_media_album(token, src_chat_id, src_msg_ids, caption)
    return token, build_bot_link(token)


def _make_single_token(msg: Message) -> tuple[str, str]:
    """Generate token for a single message. Returns (token, bot_link)."""
    token       = generate_token(16)
    src_chat_id = msg.chat_id
    create_media_album(token, src_chat_id, [msg.id], msg.message or "")
    return token, build_bot_link(token)


# ─── Caption builder ──────────────────────────────────────────────────────────

def _build_caption(orig_cap: str, url: str, template: Optional[str]) -> str:
    """Combine original caption + link line."""
    tpl      = template or LINK_CAPTION_TEMPLATE
    link_line = tpl.format(url=url)
    if orig_cap:
        return f"{orig_cap}\n\n{link_line}"
    return link_line


# ─── Single message ───────────────────────────────────────────────────────────

async def send_as_link(
    client: TelegramClient,
    msg: Message,
    dst_chat_id: int,
    dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
    uploader_name: str = "forwarder",
) -> Optional[Message]:
    """
    Send a single message in bot-link mode.
    Returns sent Message or None.
    """
    ftype = _media_type_of(msg)

    # Plain text → send as-is
    if not ftype:
        if msg.message:
            return await client.send_message(
                entity=dst_chat_id, reply_to=dst_topic_id,
                message=msg.message, formatting_entities=msg.entities,
            )
        return None

    token, url = _make_single_token(msg)
    orig_cap   = (msg.message or "").strip()
    full_cap   = _build_caption(orig_cap, url, caption_template)
    kw = dict(entity=dst_chat_id, reply_to=dst_topic_id)

    try:
        if ftype in ("video", "animation", "video_note"):
            # Thumbnail + link
            thumb_bytes = await _download_thumb(client, msg)
            if thumb_bytes:
                return await client.send_file(
                    file=thumb_bytes, caption=full_cap[:1024], **kw
                )
            # No thumbnail → text only
            fname = _file_name_of(msg) or "video"
            return await client.send_message(
                message=f"🎬 **{fname}**\n\n{full_cap[:4096]}", **kw
            )

        if ftype == "photo":
            photo_bytes = await _dl(client, msg)
            if photo_bytes:
                return await client.send_file(
                    file=photo_bytes, caption=full_cap[:1024], **kw
                )
            return await client.send_message(
                message=f"🖼️\n\n{full_cap[:4096]}", **kw
            )

        # Document / audio / voice → text link
        emoji     = _emoji_for_type(ftype)
        fname     = _file_name_of(msg) or ftype
        return await client.send_message(
            message=f"{emoji} **{fname}**\n\n{full_cap[:4096]}", **kw
        )

    except Exception as e:
        logger.warning(f"send_as_link id={msg.id} ftype={ftype}: {e}")
        try:
            return await client.send_message(
                message=f"🔗 {full_cap[:4096]}", **kw
            )
        except Exception as e2:
            logger.error(f"send_as_link fallback failed id={msg.id}: {e2}")
            return None


# ─── Album (media group) ──────────────────────────────────────────────────────

async def send_album_as_links(
    client: TelegramClient,
    msgs: list,
    dst_chat_id: int,
    dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
    uploader_name: str = "forwarder",
) -> list:
    """
    Handle a Telegram album (grouped_id) in bot-link mode.

    1 album = 1 token = 1 bot deep link.
    Destination gets a visual preview + 1 link to the whole album.

    Visual preview strategy:
      - Collect all photos + video-thumbnails
      - Send as ONE media group (album) at destination
      - Caption of last item = 1 link to the whole album
    Non-visual items (doc/audio) → text link appended to caption.
    """
    if not msgs:
        return []

    # ONE token for the whole album
    token, url = _make_album_token(msgs)
    orig_cap   = " ".join(filter(None, [m.message for m in msgs if m.message])).strip()
    full_cap   = _build_caption(orig_cap, url, caption_template)

    kw = dict(entity=dst_chat_id, reply_to=dst_topic_id)

    # Build list of visual bytes (photos / video thumbnails)
    visual_bytes   = []   # list of bytes objects
    text_only_info = []   # types that can't be in media group (doc/audio)

    for msg in msgs:
        ftype = _media_type_of(msg)
        if not ftype:
            continue

        if ftype == "photo":
            try:
                data = await _dl(client, msg)
                if data:
                    visual_bytes.append(data)
                    continue
            except Exception as e:
                logger.warning(f"download photo id={msg.id}: {e}")
            text_only_info.append(f"🖼️ {url}")

        elif ftype in ("video", "animation", "video_note"):
            try:
                data = await _download_thumb(client, msg)
                if data:
                    visual_bytes.append(data)
                    continue
            except Exception as e:
                logger.warning(f"download thumb id={msg.id}: {e}")
            # No thumbnail — will be in caption text
            text_only_info.append(f"🎬 {url}")

        else:
            # Document / audio → add info to caption, not visual
            fname = _file_name_of(msg) or ftype
            text_only_info.append(f"{_emoji_for_type(ftype)} **{fname}**")

    sent_msgs = []

    # Build final caption: link + any non-visual items
    extra = "\n".join(text_only_info)
    if extra:
        final_cap = f"{full_cap}\n{extra}"
    else:
        final_cap = full_cap
    final_cap = final_cap[:1024]

    if visual_bytes:
        # Send as media group; caption on last item only
        captions_list = [""] * (len(visual_bytes) - 1) + [final_cap]
        try:
            result = await client.send_file(
                file=visual_bytes, caption=captions_list, **kw
            )
            if isinstance(result, list):
                sent_msgs.extend(r for r in result if r)
            elif result:
                sent_msgs.append(result)
        except Exception as e:
            logger.warning(f"send_album_as_links media group ({len(visual_bytes)} items): {e}")
            # Fallback: send first visual item with full caption
            try:
                s = await client.send_file(
                    file=visual_bytes[0], caption=final_cap, **kw
                )
                sent_msgs.append(s)
            except Exception as e2:
                logger.error(f"send_album_as_links fallback failed: {e2}")
                # Last resort: text only
                try:
                    s = await client.send_message(message=final_cap, **kw)
                    sent_msgs.append(s)
                except Exception:
                    pass
    else:
        # No visual items — send text with link
        try:
            s = await client.send_message(message=final_cap, **kw)
            sent_msgs.append(s)
        except Exception as e:
            logger.error(f"send_album_as_links text fallback: {e}")

    return sent_msgs
