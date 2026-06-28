"""
Link-mode sender — preview nhẹ + link bot deep-link.

Video → thumbnail; ảnh → ảnh. User click link → bot copy_messages từ nguồn.
Giữ premium emoji qua formatting_entities (cắt caption an toàn UTF-16).
"""

import copy
import io
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import Message, DocumentAttributeFilename
from telethon.errors import (
    MessageNotModifiedError,
    FloodWaitError,
    MessageIdInvalidError,
    ChatAdminRequiredError,
    MessageAuthorRequiredError,
)

from utils.token import generate_numeric_token
from database.models import create_media_album
from config.settings import BOT_USERNAME, LINK_CAPTION_TEMPLATE

logger = logging.getLogger(__name__)

CAPTION_MAX_UTF16 = 1024


# ─── Bot deep link ────────────────────────────────────────────────────────────

def build_bot_link(token: str) -> str:
    from config.settings import LINK_MODE, BASE_URL
    if LINK_MODE == "web" and BASE_URL:
        return f"{BASE_URL.rstrip('/')}/d/{token}"
    username = (BOT_USERNAME or "").lstrip("@").strip()
    if not username:
        logger.error("BOT_USERNAME chưa set trong .env — link sẽ không hoạt động!")
        return f"https://t.me/SET_BOT_USERNAME_IN_ENV?start={token}"
    return f"https://t.me/{username}?start={token}"


# ─── Caption helpers ──────────────────────────────────────────────────────────

def _get_caption(msg: Message) -> str:
    return (getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()


def _get_caption_raw(msg: Message) -> tuple[str, list]:
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    entities = list(getattr(msg, "entities", None) or [])
    return text, entities


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _shift_entities(entities: list, offset_utf16: int) -> list:
    if not entities or offset_utf16 == 0:
        return entities
    shifted = []
    for e in entities:
        try:
            e2 = copy.copy(e)
            e2.offset = e.offset + offset_utf16
            shifted.append(e2)
        except Exception:
            shifted.append(e)
    return shifted


def _truncate_caption_utf16(text: str, entities: list,
                            max_utf16: int = CAPTION_MAX_UTF16) -> tuple[str, list]:
    """Cắt caption theo UTF-16, giữ entities còn nằm trong phạm vi."""
    if _utf16_len(text) <= max_utf16:
        return text, entities
    units = 0
    out = []
    for ch in text:
        clen = len(ch.encode("utf-16-le")) // 2
        if units + clen > max_utf16:
            break
        out.append(ch)
        units += clen
    truncated = "".join(out)
    tlen = _utf16_len(truncated)
    valid = [
        e for e in (entities or [])
        if getattr(e, "offset", 0) + getattr(e, "length", 0) <= tlen
    ]
    return truncated, valid


def _build_caption(orig_cap: str, url: str, template: Optional[str] = None) -> str:
    tpl = (template or LINK_CAPTION_TEMPLATE).replace("\\n", "\n")
    if "{caption}" in tpl:
        return tpl.format(url=url.strip(), caption=orig_cap)
    link_block = tpl.format(url=url.strip())
    return f"{orig_cap}\n\n{link_block}" if orig_cap else link_block


def _build_caption_entities(orig_text: str, orig_entities: list,
                            url: str, template: Optional[str] = None) -> tuple[str, list]:
    tpl = (template or LINK_CAPTION_TEMPLATE).replace("\\n", "\n")
    url = url.strip()

    if "{caption}" in tpl:
        tpl_with_url = tpl.replace("{url}", url)
        idx = tpl_with_url.index("{caption}")
        prefix = tpl_with_url[:idx]
        suffix = tpl_with_url[idx + len("{caption}"):]
        final_text = prefix + orig_text + suffix
        offset = _utf16_len(prefix)
        final_entities = _shift_entities(orig_entities, offset)
    else:
        link_block = tpl.format(url=url)
        if orig_text:
            final_text = f"{orig_text}\n\n{link_block}"
            final_entities = orig_entities
        else:
            final_text = link_block
            final_entities = []

    return _truncate_caption_utf16(final_text, final_entities)


def _pick_best_caption_msg(msgs: list) -> Message:
    """Chọn item album có caption + nhiều entity nhất (giữ premium emoji)."""
    best, best_score = msgs[0], -1
    for m in msgs:
        text, ents = _get_caption_raw(m)
        if not text.strip():
            continue
        premium = sum(1 for e in ents if getattr(e, "custom_emoji_id", None))
        score = premium * 1000 + len(ents) * 10 + len(text)
        if score > best_score:
            best_score, best = score, m
    return best


# ─── Media type ───────────────────────────────────────────────────────────────

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


# ─── DB token ─────────────────────────────────────────────────────────────────

def store_album_token(msgs: list) -> tuple[str, str]:
    token = generate_numeric_token(16)
    src_chat_id = msgs[0].chat_id
    src_msg_ids = [m.id for m in msgs]
    seen, caps = set(), []
    for m in msgs:
        c = _get_caption(m)
        if c and c not in seen:
            seen.add(c)
            caps.append(c)
    create_media_album(token, src_chat_id, src_msg_ids, "\n".join(caps))
    return token, build_bot_link(token)


def store_single_token(msg: Message) -> tuple[str, str]:
    token = generate_numeric_token(16)
    create_media_album(token, msg.chat_id, [msg.id], _get_caption(msg))
    return token, build_bot_link(token)


# ─── Preview download ─────────────────────────────────────────────────────────

def _photo_bio(data: bytes, name: str = "preview.jpg") -> io.BytesIO:
    bio = io.BytesIO(data)
    bio.name = name
    bio.seek(0)
    return bio


async def _download_preview(client: TelegramClient, msg: Message) -> Optional[io.BytesIO]:
    ftype = _media_type_of(msg)
    try:
        if ftype == "photo":
            data = await client.download_media(msg, file=bytes)
            return _photo_bio(data) if data else None

        if ftype in ("video", "animation", "video_note", "document"):
            doc = getattr(msg.media, "document", None)
            if doc and getattr(doc, "thumbs", None):
                best = sorted(
                    [t for t in doc.thumbs if getattr(t, "w", 0) > 0],
                    key=lambda t: t.w, reverse=True,
                )
                if best:
                    data = await client.download_media(msg, file=bytes, thumb=best[0])
                    if data:
                        return _photo_bio(data)
            data = await client.download_media(msg, file=bytes, thumb=-1)
            return _photo_bio(data) if data else None
    except Exception as e:
        logger.debug(f"_download_preview id={msg.id}: {e}")
    return None


async def _send_text_caption(client, dst_entity, text: str, entities: list,
                             head: str = "", **kw) -> object:
    body = f"{head}\n\n{text}" if head else text
    body, entities = _truncate_caption_utf16(body[:4096], entities, max_utf16=4096)
    return await client.send_message(
        dst_entity, message=body,
        formatting_entities=entities or None, **kw,
    )


# ─── Edit caption (relink + post-edit) ────────────────────────────────────────

async def edit_caption_safe(client, entity, msg_id, new_text, new_entities) -> bool:
    """Edit caption giữ formatting_entities — dùng cho relink mode."""
    text, entities = _truncate_caption_utf16(new_text or "", list(new_entities or []))
    try:
        await client.edit_message(
            entity, msg_id, text=text,
            formatting_entities=entities or None,
        )
        return True
    except MessageNotModifiedError:
        return True
    except (MessageIdInvalidError, ChatAdminRequiredError, MessageAuthorRequiredError) as e:
        logger.debug(f"edit_caption_safe id={msg_id}: {e}")
        return False
    except FloodWaitError as e:
        logger.info(f"edit_caption_safe FloodWait {e.seconds}s")
        import asyncio
        await asyncio.sleep(e.seconds + 1)
        try:
            await client.edit_message(
                entity, msg_id, text=text,
                formatting_entities=entities or None,
            )
            return True
        except Exception as e2:
            logger.warning(f"edit_caption_safe retry id={msg_id}: {e2}")
            return False
    except Exception as e:
        logger.warning(f"edit_caption_safe id={msg_id}: {e}")
        return False


# ─── Send preview (single) ────────────────────────────────────────────────────

async def send_preview_single(
    client: TelegramClient, msg: Message,
    dst_entity, dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
) -> object:
    orig_text, orig_ent = _get_caption_raw(msg)
    token, url = store_single_token(msg)
    full_cap, full_ent = _build_caption_entities(orig_text, orig_ent, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    preview = await _download_preview(client, msg)
    if preview:
        try:
            return await client.send_file(
                dst_entity, file=preview,
                caption=full_cap,
                formatting_entities=full_ent or None,
                force_document=False,
                **kw,
            )
        except Exception as e:
            logger.warning(f"send_preview_single send_file id={msg.id}: {e}")

    emoji = {"document": "📄", "audio": "🎵", "voice": "🎙️"}.get(_media_type_of(msg), "📎")
    fname = _file_name_of(msg) or ""
    head = f"{emoji} {fname}".strip()
    return await _send_text_caption(client, dst_entity, full_cap, full_ent, head=head, **kw)


# ─── Send preview (album) ─────────────────────────────────────────────────────

async def send_preview_album(
    client: TelegramClient, msgs: list,
    dst_entity, dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
) -> list:
    if not msgs:
        return []

    cap_msg = _pick_best_caption_msg(msgs)
    orig_text, orig_ent = _get_caption_raw(cap_msg)
    token, url = store_album_token(msgs)
    full_cap, full_ent = _build_caption_entities(orig_text, orig_ent, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    previews = []
    for m in msgs:
        p = await _download_preview(client, m)
        if p:
            previews.append(p)

    if not previews:
        s = await _send_text_caption(client, dst_entity, full_cap, full_ent, **kw)
        return [s]

    n = len(previews)
    captions = [""] * (n - 1) + [full_cap]
    fmt_entities = [None] * (n - 1) + [full_ent or None]

    try:
        result = await client.send_file(
            dst_entity, file=previews,
            caption=captions,
            formatting_entities=fmt_entities,
            force_document=False,
            **kw,
        )
        return result if isinstance(result, list) else ([result] if result else [])
    except Exception as e:
        logger.warning(f"send_preview_album ({n} items): {e}")
        try:
            s = await _send_text_caption(client, dst_entity, full_cap, full_ent, **kw)
            return [s]
        except Exception as e2:
            logger.error(f"send_preview_album fallback failed: {e2}")
            return []
