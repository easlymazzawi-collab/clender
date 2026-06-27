"""
Link-mode sender — đơn giản và đúng:

  Thay vì tải lại ảnh/video:
    1. Tạo bot token (lưu src_chat_id + msg_ids vào DB)
    2. Forward nguyên album sang topic đích (giữ nguyên cấu trúc album)
    3. Edit caption của message cuối để thêm bot link

  Kết quả:
    - Album giữ nguyên (không bị vỡ thành nhiều ảnh riêng lẻ)
    - Caption gốc được giữ + link được chèn vào cuối
    - Không có nút bấm (link trong text là đủ, có thể click được)
    - Không tải lại file (nhanh hơn nhiều)
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


# ─── Bot deep link ────────────────────────────────────────────────────────────

def build_bot_link(token: str) -> str:
    username = (BOT_USERNAME or "").lstrip("@").strip()
    if not username:
        logger.error("BOT_USERNAME chưa set trong .env — link sẽ không hoạt động!")
        return f"https://t.me/SET_BOT_USERNAME_IN_ENV?start={token}"
    return f"https://t.me/{username}?start={token}"


# ─── Caption helpers ──────────────────────────────────────────────────────────

def _get_caption(msg: Message) -> str:
    return (
        getattr(msg, "message", None)
        or getattr(msg, "text", None)
        or ""
    ).strip()


def _build_caption(orig_cap: str, url: str, template: Optional[str] = None) -> str:
    tpl = (template or LINK_CAPTION_TEMPLATE).replace("\\n", "\n")
    link_line = tpl.format(url=url.strip())
    if orig_cap:
        return f"{orig_cap}\n\n{link_line}"
    return link_line


# ─── Media type helper ────────────────────────────────────────────────────────

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


# ─── DB helpers ───────────────────────────────────────────────────────────────

def store_album_token(msgs: list, uploader_name: str = "forwarder") -> tuple[str, str]:
    """
    Store album in DB, return (token, bot_link).
    One token covers the whole album.
    """
    token       = generate_token(16)
    src_chat_id = msgs[0].chat_id
    src_msg_ids = [m.id for m in msgs]
    seen, caps  = set(), []
    for m in msgs:
        cap = _get_caption(m)
        if cap and cap not in seen:
            seen.add(cap); caps.append(cap)
    caption = "\n".join(caps)
    create_media_album(token, src_chat_id, src_msg_ids, caption)
    return token, build_bot_link(token)


def store_single_token(msg: Message) -> tuple[str, str]:
    """Store single message album, return (token, bot_link)."""
    token = generate_token(16)
    create_media_album(token, msg.chat_id, [msg.id], _get_caption(msg))
    return token, build_bot_link(token)


# ─── Send with caption (no re-upload, no Forwarded-from) ─────────────────────

async def send_single_as_link(
    client: TelegramClient,
    msg: Message,
    dst_entity,
    dst_topic_id: int | None,
    caption_template: str | None = None,
) -> object:
    """
    Gửi 1 message sang đích với caption = caption gốc + bot link.
    Dùng msg.media (file reference có sẵn) → không tải lại file.
    Không hiện 'Forwarded from'.
    """
    orig_cap = _get_caption(msg)
    token, url = store_single_token(msg)
    full_cap   = _build_caption(orig_cap, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    if not msg.media:
        # Tin nhắn thuần text
        return await client.send_message(
            entity=dst_entity, message=full_cap[:4096], **kw
        )

    try:
        return await client.send_file(
            entity=dst_entity,
            file=msg.media,       # tái dùng file reference, không tải lại
            caption=full_cap[:1024],
            **kw,
        )
    except Exception as e:
        logger.warning(f"send_single_as_link id={msg.id}: {e}")
        # Fallback: gửi text với link
        return await client.send_message(
            entity=dst_entity, message=full_cap[:4096], **kw
        )


async def send_album_as_links(
    client: TelegramClient,
    msgs: list,
    dst_entity,
    dst_topic_id: int | None,
    caption_template: str | None = None,
) -> list:
    """
    Gửi album sang đích với caption cuối = caption gốc + bot link.
    Dùng list[msg.media] → gửi thành album, không tải lại file.
    Không hiện 'Forwarded from'.
    """
    if not msgs:
        return []

    orig_cap = next((_get_caption(m) for m in msgs if _get_caption(m)), "")
    token, url = store_album_token(msgs)
    full_cap   = _build_caption(orig_cap, url, caption_template)

    kw = {}
    if dst_topic_id and dst_topic_id != 1:
        kw["reply_to"] = dst_topic_id

    # Lấy media objects từ các message trong album
    media_list = [m.media for m in msgs if m.media]
    if not media_list:
        # Không có media → gửi text
        return [await client.send_message(entity=dst_entity, message=full_cap[:4096], **kw)]

    n = len(media_list)
    # Caption: trống cho các item trừ item cuối
    captions = [""] * (n - 1) + [full_cap[:1024]]

    try:
        result = await client.send_file(
            entity=dst_entity,
            file=media_list,
            caption=captions,
            **kw,
        )
        # send_file với list trả về list hoặc 1 message
        if isinstance(result, list):
            return result
        return [result] if result else []
    except Exception as e:
        logger.warning(f"send_album_as_links ({len(media_list)} items): {e}")
        # Fallback: gửi text với link
        try:
            s = await client.send_message(entity=dst_entity, message=full_cap[:4096], **kw)
            return [s]
        except Exception as e2:
            logger.error(f"send_album_as_links fallback failed: {e2}")
            return []
