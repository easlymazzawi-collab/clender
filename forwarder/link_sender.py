"""
Link-mode sender for Telethon Forwarder.

Single message (link_mode):
  VIDEO / ANIMATION / VIDEO_NOTE → thumbnail + "Nhấp để xem: <link>" caption
  PHOTO                          → photo + link caption
  DOCUMENT / AUDIO / VOICE       → text message với link
  TEXT only                      → forward as-is

Album (grouped_id) in link_mode — send_album_as_links():
  Toàn bộ album gộp thành 1 lần gửi media group:
    Photo  → download bytes → InputMediaPhoto
    Video  → download thumbnail bytes → InputMediaPhoto (thumbnail đại diện)
    File   → không đưa vào album, thêm link text vào caption
  Caption của item cuối = tất cả share links gộp lại (photo1_link, video1_link, …)
  → Người xem thấy album ảnh đầy đủ + nhấn link để xem/tải bản gốc.

Share links stored in bot SQLite (database/models.py), served at BASE_URL/media/<token>.
"""

import asyncio
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import (
    Message,
    DocumentAttributeFilename,
)

from utils.token import generate_token
from database.models import create_media_link
from config.settings import BASE_URL, LINK_CAPTION_TEMPLATE

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_url(token: str) -> str:
    return f"{BASE_URL}/media/{token}"


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


def _file_name_of(msg: Message) -> Optional[str]:
    doc = getattr(msg.media, "document", None)
    if not doc:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def _mime_type_of(msg: Message) -> Optional[str]:
    doc = getattr(msg.media, "document", None)
    if doc:
        return getattr(doc, "mime_type", None)
    return None


def _file_id_of(msg: Message) -> Optional[str]:
    if msg.photo:
        return str(msg.photo.id)
    doc = getattr(msg.media, "document", None)
    if doc:
        return str(doc.id)
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


def _store_link(msg: Message, ftype: str, uploader_name: str = "forwarder") -> str:
    """Persist share-link record → return token."""
    token     = generate_token(14)
    file_id   = _file_id_of(msg) or "unknown"
    file_name = _file_name_of(msg)
    mime_type = _mime_type_of(msg)
    caption   = msg.message or ""
    create_media_link(
        token=token, file_id=file_id, file_type=ftype,
        file_name=file_name, mime_type=mime_type,
        thumb_file_id=None, caption=caption,
        uploader_id=None, uploader_name=uploader_name,
    )
    return token


# ─── Single message ───────────────────────────────────────────────────────────

async def send_as_link(
    client: TelegramClient,
    msg: Message,
    dst_chat_id: int,
    dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
    uploader_name: str = "forwarder",
):
    """
    Send a single message in link-mode.
    Returns the sent Message object or None.
    """
    ftype = _media_type_of(msg)
    if not ftype:
        # Plain text — forward as-is
        if msg.message:
            return await client.send_message(
                entity=dst_chat_id,
                message=msg.message,
                reply_to=dst_topic_id,
                formatting_entities=msg.entities,
            )
        return None

    template  = caption_template or LINK_CAPTION_TEMPLATE
    token     = _store_link(msg, ftype, uploader_name)
    url       = _build_url(token)
    emoji     = _emoji_for_type(ftype)
    fname     = _file_name_of(msg) or ""
    orig_cap  = (msg.message or "").strip()

    link_line = template.format(url=url)
    full_cap  = f"{orig_cap}\n\n{link_line}" if orig_cap else link_line

    kw = dict(entity=dst_chat_id, reply_to=dst_topic_id)

    try:
        # ── Video / Animation / VideoNote → thumbnail + link ─────────────────
        if ftype in ("video", "animation", "video_note"):
            thumb_bytes = await _download_thumb(client, msg)
            if thumb_bytes:
                return await client.send_file(
                    file=thumb_bytes,
                    caption=full_cap[:1024],
                    **kw,
                )
            # No thumbnail — text only
            fname_part = f" ({fname})" if fname else ""
            return await client.send_message(
                message=f"{emoji} **Video{fname_part}**\n\n{full_cap[:4096]}",
                **kw,
            )

        # ── Photo → send photo + link ─────────────────────────────────────────
        if ftype == "photo":
            photo_bytes = await client.download_media(msg, bytes=True)
            if photo_bytes:
                return await client.send_file(
                    file=photo_bytes,
                    caption=full_cap[:1024],
                    **kw,
                )
            return await client.send_message(
                message=f"{emoji} **Ảnh**\n\n{full_cap[:4096]}",
                **kw,
            )

        # ── Document / Audio / Voice → text link ─────────────────────────────
        fname_part = f" `{fname}`" if fname else ""
        return await client.send_message(
            message=f"{emoji} **{ftype.capitalize()}{fname_part}**\n\n{full_cap[:4096]}",
            **kw,
        )

    except Exception as e:
        logger.warning(f"send_as_link ftype={ftype} id={msg.id}: {e}")
        try:
            return await client.send_message(
                message=f"{emoji} {full_cap[:4096]}",
                **kw,
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
    Handle a Telegram album (grouped_id) in link-mode.

    Strategy:
      1. For each photo  → download bytes (will appear in album as photo)
      2. For each video  → download thumbnail bytes (replaces video in album)
      3. Build ONE combined caption with all share-links
      4. Send as a single media-group (album) to destination:
           [photo1, photo2, thumb_video1, …] + caption on the LAST item
      5. If album has document / audio / voice items, send them as text links
         BEFORE the album (they cannot be in a media group with photos).

    Caption format (per item):
      🖼️  Link ảnh 1: https://…/media/<tok>
      🖼️  Link ảnh 2: https://…/media/<tok>
      🎬  Link video: https://…/media/<tok>
    """
    if not msgs:
        return []

    template = caption_template or LINK_CAPTION_TEMPLATE

    # Separate into visual (photo/video thumbnail) and non-visual (doc/audio…)
    visual_items  = []   # list of {"bytes": b, "token": t, "ftype": f, "label": l}
    text_links    = []   # list of strings for non-visual items
    orig_captions = []

    for msg in msgs:
        ftype = _media_type_of(msg)
        if not ftype:
            # Plain text inside album (rare) — ignore
            continue

        if msg.message:
            orig_captions.append(msg.message.strip())

        token = _store_link(msg, ftype, uploader_name)
        url   = _build_url(token)
        emoji = _emoji_for_type(ftype)
        fname = _file_name_of(msg) or ""

        if ftype == "photo":
            try:
                data = await client.download_media(msg, bytes=True)
                if data:
                    visual_items.append({
                        "bytes": data, "token": token,
                        "ftype": "photo", "label": f"{emoji} {url}",
                    })
                    continue
            except Exception as e:
                logger.warning(f"download photo id={msg.id}: {e}")
            text_links.append(f"{emoji} {url}")

        elif ftype in ("video", "animation", "video_note"):
            try:
                data = await _download_thumb(client, msg)
                if data:
                    label = f"🎬 {url}"
                    visual_items.append({
                        "bytes": data, "token": token,
                        "ftype": "video_thumb", "label": label,
                    })
                    continue
            except Exception as e:
                logger.warning(f"download thumb id={msg.id}: {e}")
            text_links.append(f"🎬 {url}")

        else:
            # document / audio / voice / sticker
            fname_part = f" `{fname}`" if fname else ""
            text_links.append(f"{emoji}{fname_part}: {url}")

    sent_msgs = []
    kw = dict(entity=dst_chat_id, reply_to=dst_topic_id)

    # ── 1. Send text links for non-visual items first ─────────────────────────
    if text_links:
        try:
            s = await client.send_message(
                message="\n".join(text_links),
                **kw,
            )
            sent_msgs.append(s)
        except Exception as e:
            logger.warning(f"send text links for album: {e}")

    # ── 2. Send visual items as a media group ─────────────────────────────────
    if not visual_items:
        return sent_msgs

    # Build combined caption
    all_link_lines = [item["label"] for item in visual_items]
    orig_cap = " | ".join(dict.fromkeys(orig_captions))  # deduplicate
    links_block = "\n".join(all_link_lines)
    full_cap = f"{orig_cap}\n\n{links_block}" if orig_cap else links_block
    full_cap = full_cap[:1024]

    files = [item["bytes"] for item in visual_items]

    # Caption: empty for all except last
    if len(files) == 1:
        captions = [full_cap]
    else:
        captions = [""] * (len(files) - 1) + [full_cap]

    try:
        result = await client.send_file(
            file=files,
            caption=captions,
            **kw,
        )
        if isinstance(result, list):
            sent_msgs.extend(result)
        elif result:
            sent_msgs.append(result)
    except Exception as e:
        logger.warning(f"send_album_as_links media group failed ({len(files)} items): {e}")
        # Fallback: send items individually
        for i, item in enumerate(visual_items):
            try:
                cap = item["label"] if i == len(visual_items) - 1 else ""
                s = await client.send_file(
                    file=item["bytes"],
                    caption=cap,
                    **kw,
                )
                sent_msgs.append(s)
                await asyncio.sleep(0.5)
            except Exception as e2:
                logger.error(f"individual fallback failed: {e2}")

    return sent_msgs


# ─── Internal helpers ─────────────────────────────────────────────────────────

async def _download_thumb(client: TelegramClient, msg: Message) -> Optional[bytes]:
    """Download thumbnail bytes for a video message. Returns None if unavailable."""
    try:
        doc = getattr(msg.media, "document", None)
        if doc and doc.thumbs:
            best = sorted(
                [t for t in doc.thumbs if hasattr(t, "w") and getattr(t, "w", 0) > 0],
                key=lambda t: t.w, reverse=True,
            )
            if best:
                return await client.download_media(msg, thumb=best[0], bytes=True)
        # Fallback: let Telethon pick best thumb
        data = await client.download_media(msg, thumb=-1, bytes=True)
        return data
    except Exception as e:
        logger.debug(f"_download_thumb id={msg.id}: {e}")
        return None
