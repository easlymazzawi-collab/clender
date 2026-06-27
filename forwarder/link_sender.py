"""
Link-mode sender — đúng thiết kế gốc "share file bot lấy link".

Ý tưởng:
  Forum đích KHÔNG chứa file gốc nặng. Nó chỉ chứa PREVIEW nhẹ + link.
  - Ảnh   → download ảnh (nhẹ) → gửi như ảnh nén
  - Video → CHỈ download THUMBNAIL → gửi như ảnh nén (không gửi video nặng)
  - Album → gửi tất cả preview thành 1 media group
  - Caption item cuối = caption gốc + "Nhấp vào link để xem: t.me/bot?start=TOKEN"

  File gốc nằm ở forum nguồn. Bot lưu (src_chat_id + msg_ids) theo TOKEN.
  User click link → bot copy_messages từ nguồn → trả MEDIA GỐC (video full, ảnh gốc).

Lợi ích:
  - Forum đích nhẹ (chỉ thumbnail, không nhân đôi video nặng)
  - Tốc độ clone nhanh (chỉ tải thumbnail nhỏ)
  - User vẫn xem được bản gốc qua bot
"""

import io
import asyncio
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import Message, DocumentAttributeFilename

from utils.token import generate_numeric_token
from database.models import create_media_album
from config.settings import BOT_USERNAME, LINK_CAPTION_TEMPLATE

logger = logging.getLogger(__name__)


# ─── Bot deep link ────────────────────────────────────────────────────────────

def build_bot_link(token: str) -> str:
    username = (BOT_USERNAME or "").lstrip("@").strip()
    if not username:
        logger.error("BOT_USERNAME chưa set trong .env — link sẽ không hoạt động!")
        return f"https://t.me/SET_BOT_USERNAME_IN_ENV?start={token}"
    return f"https://t.me/{username}?start={token}"


# ─── Caption ──────────────────────────────────────────────────────────────────

def _get_caption(msg: Message) -> str:
    return (getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()


def _get_caption_raw(msg: Message) -> tuple[str, list]:
    """
    Lấy caption GỐC + entities (giữ emoji premium, bold, link ẩn…).
    Trả về (text, entities). text KHÔNG strip để giữ đúng offset entities.
    """
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    entities = list(getattr(msg, "entities", None) or [])
    return text, entities


def _utf16_len(s: str) -> int:
    """Độ dài chuỗi tính theo UTF-16 code units (chuẩn offset của Telegram)."""
    return len(s.encode("utf-16-le")) // 2


def _shift_entities(entities: list, offset_utf16: int) -> list:
    """Dịch offset của tất cả entities thêm offset_utf16 (UTF-16 units)."""
    if not entities or offset_utf16 == 0:
        return entities
    shifted = []
    for e in entities:
        try:
            import copy
            e2 = copy.copy(e)
            e2.offset = e.offset + offset_utf16
            shifted.append(e2)
        except Exception:
            shifted.append(e)
    return shifted


def _build_caption(orig_cap: str, url: str, template: Optional[str] = None) -> str:
    """Phiên bản text-only (không entities) — dùng cho fallback/text message."""
    tpl = (template or LINK_CAPTION_TEMPLATE).replace("\\n", "\n")
    if "{caption}" in tpl:
        return tpl.format(url=url.strip(), caption=orig_cap)
    link_block = tpl.format(url=url.strip())
    return f"{orig_cap}\n\n{link_block}" if orig_cap else link_block


def _build_caption_entities(orig_text: str, orig_entities: list,
                            url: str, template: Optional[str] = None) -> tuple[str, list]:
    """
    Ghép caption cuối + GIỮ entities gốc (emoji premium, link ẩn…).
    Dịch offset entities theo UTF-16 nếu caption không nằm ở đầu.
    Trả về (final_text, final_entities).
    """
    tpl = (template or LINK_CAPTION_TEMPLATE).replace("\\n", "\n")
    url = url.strip()

    if "{caption}" in tpl:
        # Thay {url} trước → tìm vị trí {caption} → tính offset
        tpl_with_url = tpl.replace("{url}", url)
        idx = tpl_with_url.index("{caption}")
        prefix = tpl_with_url[:idx]
        suffix = tpl_with_url[idx + len("{caption}"):]
        final_text = prefix + orig_text + suffix
        offset = _utf16_len(prefix)
        final_entities = _shift_entities(orig_entities, offset)
        return final_text, final_entities

    # Default: caption GỐC ở đầu (offset 0 → entities giữ nguyên) + link phía sau
    link_block = tpl.format(url=url)
    if orig_text:
        final_text = f"{orig_text}\n\n{link_block}"
        return final_text, orig_entities  # offset 0, không cần dịch
    return link_block, []


# ─── Media type ───────────────────────────────────────────────────────────────

def _media_type_of(msg: Message) -> Optional[str]:
    if msg.photo:         return "photo"
    if msg.video:         return "video"
    if getattr(msg, "gif", None) or getattr(msg, "animation", None): return "animation"
    if msg.video_note:    return "video_note"
    if msg.audio:         return "audio"
    if msg.voice:         return "voice"
    if msg.document:      return "document"
    if msg.sticker:       return "sticker"
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
    token       = generate_numeric_token(16)
    src_chat_id = msgs[0].chat_id
    src_msg_ids = [m.id for m in msgs]
    seen, caps  = set(), []
    for m in msgs:
        c = _get_caption(m)
        if c and c not in seen:
            seen.add(c); caps.append(c)
    caption = "\n".join(caps)
    create_media_album(token, src_chat_id, src_msg_ids, caption)
    return token, build_bot_link(token)


def store_single_token(msg: Message) -> tuple[str, str]:
    token = generate_numeric_token(16)
    create_media_album(token, msg.chat_id, [msg.id], _get_caption(msg))
    return token, build_bot_link(token)


# ─── Preview download ─────────────────────────────────────────────────────────

def _photo_bio(data: bytes, name: str = "preview.jpg") -> io.BytesIO:
    """Wrap bytes in BytesIO với tên .jpg để Telethon gửi như ảnh nén."""
    bio = io.BytesIO(data)
    bio.name = name
    bio.seek(0)
    return bio


async def _download_preview(client: TelegramClient, msg: Message) -> Optional[io.BytesIO]:
    """
    Tải preview nhẹ cho 1 message:
      - photo → tải ảnh (nén lại nhỏ)
      - video/animation/video_note → CHỈ tải thumbnail
      - document có thumb → tải thumb
    Trả về BytesIO(.jpg) hoặc None nếu không có preview.
    """
    ftype = _media_type_of(msg)
    try:
        if ftype == "photo":
            data = await client.download_media(msg, file=bytes)
            return _photo_bio(data) if data else None

        if ftype in ("video", "animation", "video_note", "document"):
            # Chỉ tải thumbnail (nhẹ) — không tải file gốc nặng
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
            # fallback: thumb mặc định
            data = await client.download_media(msg, file=bytes, thumb=-1)
            return _photo_bio(data) if data else None

    except Exception as e:
        logger.debug(f"_download_preview id={msg.id}: {e}")
    return None


# ─── Send preview (single) ────────────────────────────────────────────────────

async def send_preview_single(
    client: TelegramClient, msg: Message,
    dst_entity, dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
) -> object:
    """
    Gửi preview của 1 message sang đích + caption gốc + link.
    Video → thumbnail. Ảnh → ảnh. Click link → bot trả bản gốc.
    """
    orig_text, orig_ent = _get_caption_raw(msg)
    token, url = store_single_token(msg)
    full_cap, full_ent = _build_caption_entities(orig_text, orig_ent, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    preview = await _download_preview(client, msg)
    if preview:
        return await client.send_file(
            dst_entity, file=preview,
            caption=full_cap[:1024],
            formatting_entities=full_ent or None,
            force_document=False,
            **kw,
        )
    # Không có preview (document không thumb, audio…) → gửi text + link
    emoji = {"document": "📄", "audio": "🎵", "voice": "🎙️"}.get(_media_type_of(msg), "📎")
    fname = _file_name_of(msg) or ""
    head  = f"{emoji} {fname}".strip()
    return await client.send_message(
        dst_entity, message=(f"{head}\n\n{full_cap}")[:4096], **kw
    )


# ─── Send preview (album) ─────────────────────────────────────────────────────

async def send_preview_album(
    client: TelegramClient, msgs: list,
    dst_entity, dst_topic_id: Optional[int],
    caption_template: Optional[str] = None,
) -> list:
    """
    Gửi preview của cả album thành 1 media group + caption gốc + link.
    Mỗi video → thumbnail, mỗi ảnh → ảnh. 1 album = 1 link.
    """
    if not msgs:
        return []

    # Lấy caption gốc + entities (từ item đầu tiên có caption)
    orig_text, orig_ent = "", []
    for m in msgs:
        t, e = _get_caption_raw(m)
        if t.strip():
            orig_text, orig_ent = t, e
            break
    token, url = store_album_token(msgs)
    full_cap, full_ent = _build_caption_entities(orig_text, orig_ent, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    # Tải preview cho từng item
    previews = []
    for m in msgs:
        p = await _download_preview(client, m)
        if p:
            previews.append(p)

    if not previews:
        return [await client.send_message(
            dst_entity, message=full_cap[:4096],
            formatting_entities=full_ent or None, **kw)]

    n = len(previews)
    # Caption + entities chỉ gắn vào item cuối của album
    captions = [""] * (n - 1) + [full_cap[:1024]]

    try:
        result = await client.send_file(
            dst_entity, file=previews,
            caption=captions,
            formatting_entities=full_ent or None,
            force_document=False,
            **kw,
        )
        return result if isinstance(result, list) else ([result] if result else [])
    except Exception as e:
        logger.warning(f"send_preview_album ({n} items): {e}")
        try:
            s = await client.send_message(dst_entity, message=full_cap[:4096], **kw)
            return [s]
        except Exception as e2:
            logger.error(f"send_preview_album fallback failed: {e2}")
            return []
