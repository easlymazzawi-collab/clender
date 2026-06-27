"""
Link-mode sender for Telethon Forwarder.

When link_mode=True, instead of forwarding the raw file, the forwarder:
  - VIDEO / ANIMATION / VIDEO_NOTE → send thumbnail image + caption chứa link
  - PHOTO                          → send photo + caption chứa link
  - DOCUMENT / AUDIO / VOICE       → send text message chứa link + tên file
  - TEXT only                      → forward as-is (no link needed)

Share links are stored in the bot's SQLite database (database/models.py)
and served at BASE_URL/media/<token>.
"""

import logging
import os
from telethon.tl.types import (
    Message, MessageMediaPhoto, MessageMediaDocument,
    DocumentAttributeVideo, DocumentAttributeAudio,
    DocumentAttributeFilename,
)
from telethon import TelegramClient

from utils.token import generate_token
from database.models import create_media_link
from config.settings import BASE_URL, LINK_CAPTION_TEMPLATE

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_url(token: str) -> str:
    return f"{BASE_URL}/media/{token}"


def _media_type_of(msg: Message) -> str | None:
    """Return a simple type string for the message's media, or None."""
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.gif or msg.animation:
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


def _file_name_of(msg: Message) -> str | None:
    doc = getattr(msg.media, "document", None)
    if not doc:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def _mime_type_of(msg: Message) -> str | None:
    doc = getattr(msg.media, "document", None)
    if doc:
        return getattr(doc, "mime_type", None)
    return None


def _file_id_of(msg: Message) -> str | None:
    """Return the Telethon access hash string — we use document/photo id as file_id."""
    if msg.photo:
        return str(msg.photo.id)
    doc = getattr(msg.media, "document", None)
    if doc:
        return str(doc.id)
    return None


def _thumb_of(msg: Message):
    """Return the thumbnail Photo object (if any) for video/document."""
    doc = getattr(msg.media, "document", None)
    if doc and doc.thumbs:
        # Pick the largest thumbnail
        best = sorted(
            [t for t in doc.thumbs if hasattr(t, "w") and t.w],
            key=lambda t: t.w, reverse=True
        )
        return best[0] if best else doc.thumbs[-1]
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


# ─── DB persistence helper ────────────────────────────────────────────────────

def _store_link(msg: Message, ftype: str, uploader_name: str | None = None) -> str:
    """
    Store a share-link record in the bot database.
    Returns the token string.
    """
    token = generate_token(14)
    file_id   = _file_id_of(msg) or "unknown"
    file_name = _file_name_of(msg)
    mime_type = _mime_type_of(msg)
    caption   = msg.message or ""

    # thumb_file_id: we store the thumbnail document-id if available
    thumb = _thumb_of(msg)
    thumb_id = str(thumb.file_reference) if thumb and hasattr(thumb, "file_reference") else None

    create_media_link(
        token=token,
        file_id=file_id,
        file_type=ftype,
        file_name=file_name,
        mime_type=mime_type,
        thumb_file_id=thumb_id,
        caption=caption,
        uploader_id=None,
        uploader_name=uploader_name or "forwarder",
    )
    return token


# ─── Main send function ───────────────────────────────────────────────────────

async def send_as_link(
    client: TelegramClient,
    msg: Message,
    dst_chat_id: int,
    dst_topic_id: int | None,
    caption_template: str | None = None,
    uploader_name: str | None = None,
):
    """
    Send a message to dst_chat_id in link-mode:
      - Video/animation → thumbnail (if available) + share-link caption
      - Photo           → photo + share-link caption
      - Document/audio  → text message with share-link
      - Text only       → send as-is (no link created)

    Returns the sent Message object (or None).
    """
    ftype = _media_type_of(msg)
    if not ftype:
        # Plain text — forward as-is
        if msg.message:
            return await client.send_message(
                entity=dst_chat_id,
                message=msg.message,
                reply_to=dst_topic_id,
                parse_mode="html",
                formatting_entities=msg.entities,
            )
        return None

    template = caption_template or LINK_CAPTION_TEMPLATE
    token    = _store_link(msg, ftype, uploader_name)
    url      = _build_url(token)
    emoji    = _emoji_for_type(ftype)
    fname    = _file_name_of(msg) or ""

    orig_caption = (msg.message or "").strip()

    # Build final caption  (original caption if any, then link line)
    if orig_caption:
        link_line = template.format(url=url)
        full_cap  = f"{orig_caption}\n\n{link_line}"
    else:
        full_cap = template.format(url=url)

    # Truncate to Telegram limit (1024 for media caption, 4096 for text)
    full_cap_media = full_cap[:1024]
    full_cap_text  = full_cap[:4096]

    send_kw = dict(
        entity=dst_chat_id,
        reply_to=dst_topic_id,
    )

    try:
        # ── Video / Animation / VideoNote → send thumbnail if available ──────
        if ftype in ("video", "animation", "video_note"):
            thumb = _thumb_of(msg)
            if thumb:
                # Download thumbnail bytes then send as photo
                thumb_bytes = await client.download_media(
                    msg, thumb=True, bytes=True
                )
                if thumb_bytes:
                    return await client.send_file(
                        file=thumb_bytes,
                        caption=full_cap_media,
                        parse_mode="md",
                        **send_kw,
                    )
            # No thumbnail — send as text link only
            fname_part = f" ({fname})" if fname else ""
            return await client.send_message(
                message=f"{emoji} *Video{fname_part}*\n\n{full_cap_text}",
                parse_mode="md",
                **send_kw,
            )

        # ── Photo → send photo + link caption ────────────────────────────────
        if ftype == "photo":
            photo_bytes = await client.download_media(msg, bytes=True)
            if photo_bytes:
                return await client.send_file(
                    file=photo_bytes,
                    caption=full_cap_media,
                    parse_mode="md",
                    **send_kw,
                )
            # Fallback: text link
            return await client.send_message(
                message=f"{emoji} *Ảnh*\n\n{full_cap_text}",
                parse_mode="md",
                **send_kw,
            )

        # ── Document / Audio / Voice / Sticker → text link only ──────────────
        fname_part = f" `{fname}`" if fname else ""
        return await client.send_message(
            message=f"{emoji} *{ftype.capitalize()}{fname_part}*\n\n{full_cap_text}",
            parse_mode="md",
            **send_kw,
        )

    except Exception as e:
        logger.warning(f"send_as_link ftype={ftype} id={msg.id}: {e}")
        # Final fallback: send just the link as text
        try:
            return await client.send_message(
                message=f"{emoji} {full_cap_text}",
                parse_mode="md",
                **send_kw,
            )
        except Exception as e2:
            logger.error(f"send_as_link fallback failed: {e2}")
            return None
