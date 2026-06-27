"""
Telethon-based Forum Forwarder — core async logic.
Ported from CLI v2.5.8 to be importable and controllable from the web app.

Changes from original CLI version:
  - Config comes from arguments / dicts, not hardcoded
  - Progress reported via callback (for real-time web updates)
  - Stop signal via asyncio.Event
  - State stored in STATE_DIR/  instead of ./
  - No user input (input()) — all config pre-supplied
"""

import asyncio
import os
import re
import logging
import aiohttp

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message, Channel, MessageEntityCustomEmoji
from telethon.tl.functions.channels import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.functions.messages import ForwardMessagesRequest

from .state import (
    load_last_id, save_last_id,
    load_topic_map, save_topic_map,
    save_session_meta, db_set_session_progress,
    make_key, STATE_DIR,
)
from .link_sender import (
    store_album_token, store_single_token,
    _build_caption, _get_caption, _media_type_of,
    send_single_as_link, send_album_as_links,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# ENTITY / LINK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_link(raw: str) -> dict:
    """Parse Telegram link or ID → {channel_raw, message_id, topic_id}."""
    raw = raw.strip()
    result = {"channel_raw": raw, "message_id": None, "topic_id": None}

    m = re.search(r't\.me/c/(\d+)/(\d+)/(\d+)', raw)
    if m:
        result["channel_raw"] = f"-100{m.group(1)}"
        result["topic_id"]    = int(m.group(2))
        result["message_id"]  = int(m.group(3))
        return result
    m = re.search(r't\.me/c/(\d+)/(\d+)', raw)
    if m:
        result["channel_raw"] = f"-100{m.group(1)}"
        result["message_id"]  = int(m.group(2))
        return result
    m = re.search(r't\.me/([^/]+)/(\d+)/(\d+)', raw)
    if m:
        result["channel_raw"] = m.group(1)
        result["topic_id"]    = int(m.group(2))
        result["message_id"]  = int(m.group(3))
        return result
    m = re.search(r't\.me/([^/]+)/(\d+)$', raw)
    if m:
        result["channel_raw"] = m.group(1)
        result["message_id"]  = int(m.group(2))
        return result
    return result


async def resolve_entity(client: TelegramClient, raw: str):
    raw = raw.strip()
    try:
        return await client.get_entity(int(raw))
    except ValueError:
        return await client.get_entity(raw)


def is_forum(entity) -> bool:
    return isinstance(entity, Channel) and getattr(entity, "forum", False)


# ══════════════════════════════════════════════════════════════════════════════
# TOPIC HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def preload_topics_full(client: TelegramClient, entity) -> dict:
    """Load all topics with icon info → {id: {title, icon_color, icon_emoji_id}}"""
    topics: dict = {}
    try:
        offset_topic = 0
        while True:
            r = await client(GetForumTopicsRequest(
                channel=entity, q="",
                offset_date=0, offset_id=0,
                offset_topic=offset_topic, limit=100,
            ))
            if not r.topics:
                break
            for t in r.topics:
                topics[t.id] = {
                    "title":         getattr(t, "title", f"Topic {t.id}"),
                    "icon_color":    getattr(t, "icon_color", None),
                    "icon_emoji_id": getattr(t, "icon_emoji_id", None),
                }
            if len(r.topics) < 100:
                break
            offset_topic = r.topics[-1].id
    except Exception as e:
        logger.warning(f"preload_topics_full: {e}")
    return topics


async def preload_topics(client: TelegramClient, entity) -> dict:
    """Load topics → {id: title}"""
    full = await preload_topics_full(client, entity)
    return {tid: info["title"] for tid, info in full.items()}


async def get_or_create_topic_fast(
    client, dst_entity, src_topic_id,
    topic_map, map_key, auto_topic,
    src_titles, dst_by_title,
) -> int | None:
    if src_topic_id in topic_map:
        return topic_map[src_topic_id]
    title = src_titles.get(src_topic_id, f"Topic {src_topic_id}")
    if title in dst_by_title:
        tid = dst_by_title[title]
        topic_map[src_topic_id] = tid
        save_topic_map(map_key, topic_map)
        return tid
    if not auto_topic:
        return None
    try:
        cr = await client(CreateForumTopicRequest(
            channel=dst_entity, title=title,
            random_id=int.from_bytes(os.urandom(8), "little") & 0x7FFFFFFFFFFFFFFF,
        ))
        new_id = None
        for u in cr.updates:
            if hasattr(u, "message") and hasattr(u.message, "action"):
                new_id = u.message.id
                break
        if new_id is None:
            for u in cr.updates:
                if hasattr(u, "id"):
                    new_id = u.id
                    break
        if new_id:
            topic_map[src_topic_id] = new_id
            dst_by_title[title] = new_id
            save_topic_map(map_key, topic_map)
            return new_id
    except Exception as e:
        logger.warning(f"get_or_create_topic '{title}': {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SEND / FORWARD
# ══════════════════════════════════════════════════════════════════════════════

PERMANENT_ERROR_NAMES = {
    "MessageIdInvalidError",
    "MessageEmptyError",
    "ChatForwardsRestrictedError",
    "MediaEmptyError",
    "FileReferenceExpiredError",
}


def is_permanent_error(exc) -> bool:
    return type(exc).__name__ in PERMANENT_ERROR_NAMES


class PermanentForwardError(Exception):
    def __init__(self, orig):
        self.orig = orig
        super().__init__(f"{type(orig).__name__}: {orig}")


def _parse_new_ids_from_updates(result) -> list[int]:
    """
    Parse a Telethon Updates object returned by ForwardMessagesRequest
    and return the new message IDs in the destination (sorted ascending).
    """
    ids = []
    for upd in getattr(result, "updates", []):
        msg = getattr(upd, "message", None)
        if msg and hasattr(msg, "id") and not getattr(msg, "action", None):
            ids.append(msg.id)
    return sorted(ids)


async def send_msgs(client, msgs, dst_entity, hide_sender, dst_topic,
                    src_peer, dst_peer, max_attempts=4):
    """
    Forward messages in one API call.
    hide_sender=True → drop_author=True (no "Forwarded from", keeps formatting).
    Permanent errors → PermanentForwardError (do not retry).
    Returns the raw Updates result so callers can extract dest message IDs.
    """
    if not msgs:
        return None
    kw = {}
    if dst_topic and dst_topic != 1:
        kw["top_msg_id"] = dst_topic

    for attempt in range(max_attempts):
        try:
            result = await client(ForwardMessagesRequest(
                from_peer=src_peer, to_peer=dst_peer,
                id=[m.id for m in msgs],
                random_id=[
                    int.from_bytes(os.urandom(8), "big") & 0x7FFFFFFFFFFFFFFF
                    for _ in msgs
                ],
                drop_author=bool(hide_sender),
                noforwards=False,
                **kw,
            ))
            return result   # caller can extract dest IDs from Updates
        except FloodWaitError as e:
            wait = e.seconds + 2
            logger.info(f"FloodWait {wait}s (attempt {attempt+1}/{max_attempts})")
            await asyncio.sleep(wait)
        except Exception as e:
            if is_permanent_error(e):
                raise PermanentForwardError(e)
            if attempt == max_attempts - 1:
                raise
            backoff = 2 * (attempt + 1)
            logger.warning(f"send retry {attempt+1}/{max_attempts}: {type(e).__name__}: {e} — wait {backoff}s")
            await asyncio.sleep(backoff)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PINNED MESSAGE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def get_pinned_message_ids(client: TelegramClient, entity,
                                 topic_id: int | None) -> list[int]:
    """
    Return list of pinned message IDs that belong to this topic.

    BUG NOTE: In Telethon, passing reply_to= together with filter= causes
    filter to be IGNORED (Telethon uses GetRepliesRequest internally which
    ignores filters). So we fetch ALL pinned messages for the whole chat,
    then manually filter by checking which topic each pinned message belongs to.
    """
    from telethon.tl.types import InputMessagesFilterPinned
    try:
        all_pinned = await client.get_messages(
            entity,
            filter=InputMessagesFilterPinned,
            limit=100,
        )
        if not all_pinned:
            return []

        result = []
        for m in all_pinned:
            if not m or getattr(m, "action", None):
                continue
            rt      = getattr(m, "reply_to", None)
            msg_top = getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)

            if not topic_id or topic_id == 1:
                # General topic: no thread header, OR thread==1, OR msg IS the General header
                if not msg_top or msg_top == 1 or m.id == 1:
                    result.append(m.id)
            else:
                # Specific topic: top must match topic_id, OR msg itself IS topic header
                if msg_top == topic_id or m.id == topic_id:
                    result.append(m.id)

        return result
    except Exception as e:
        logger.warning(f"get_pinned_message_ids topic={topic_id}: {e}")
        return []


async def pin_message(client: TelegramClient, entity, msg_id: int,
                      chat_id: int = None,
                      silent: bool = True,
                      max_flood_wait: int = 30) -> bool:
    """
    Pin msg_id in destination.

    Strategy (in order):
    1. Bot API (python-telegram-bot) — try first, usually no severe FloodWait.
       Requires BOT_TOKEN and bot must be admin in the destination chat.
    2. Telethon (user account) — fallback.
       If FloodWait > max_flood_wait seconds → skip (don't wait).

    chat_id: numeric ID of destination chat (needed for Bot API).
             If None, skip Bot API and use Telethon only.
    """
    from telethon.tl.functions.messages import UpdatePinnedMessageRequest
    from telethon.errors import FloodWaitError
    from config.settings import BOT_TOKEN

    # ── 1. Try Bot API first ──────────────────────────────────────────────────
    if BOT_TOKEN and chat_id:
        try:
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg_id,
                disable_notification=silent,
            )
            logger.info(f"pin_message id={msg_id} via Bot API ✅")
            return True
        except Exception as e:
            logger.warning(f"pin_message id={msg_id} Bot API failed: {e} — trying Telethon")

    # ── 2. Telethon fallback ──────────────────────────────────────────────────
    for attempt in range(2):
        try:
            await client(UpdatePinnedMessageRequest(
                peer=entity, id=msg_id,
                silent=silent, unpin=False, pm_oneside=False,
            ))
            logger.info(f"pin_message id={msg_id} via Telethon ✅")
            return True
        except FloodWaitError as e:
            if e.seconds > max_flood_wait:
                logger.warning(
                    f"pin_message id={msg_id}: FloodWait {e.seconds}s "
                    f"> limit {max_flood_wait}s — skipping"
                )
                return False
            logger.info(f"pin_message id={msg_id}: FloodWait {e.seconds}s, waiting…")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            logger.warning(f"pin_message id={msg_id} Telethon: {e}")
            return False
    return False


async def clone_pinned_messages(client: TelegramClient,
                                src_entity, dst_entity,
                                src_topic_id: int | None,
                                id_map: dict[int, int]) -> int:
    """
    After a topic is fully forwarded, pin the correct messages in dst.

    id_map: {src_msg_id: dst_msg_id} — built during forwarding.
    Returns number of messages successfully pinned.
    """
    pinned_src_ids = await get_pinned_message_ids(client, src_entity, src_topic_id)
    if not pinned_src_ids:
        return 0

    # Get numeric chat_id for Bot API pinning
    dst_chat_id = getattr(dst_entity, "id", None)
    if dst_chat_id and dst_chat_id > 0:
        dst_chat_id = -dst_chat_id   # channels/supergroups are negative

    pinned = 0
    # Pin in REVERSE order so the "first" pinned message in source
    # ends up on top in destination (Telegram stacks pins newest-first)
    for src_id in reversed(pinned_src_ids):
        dst_id = id_map.get(src_id)
        if dst_id:
            ok = await pin_message(client, dst_entity, dst_id,
                                   chat_id=dst_chat_id)
            if ok:
                pinned += 1
                logger.info(f"Pinned dst={dst_id} (src={src_id})")
            await asyncio.sleep(0.5)
        else:
            logger.debug(f"No dst mapping for pinned src_id={src_id} — skipped")

    return pinned


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════

async def notify_webhook(webhook_url: str, payload: dict):
    if not webhook_url:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                logger.info(f"Webhook → {resp.status}")
    except Exception as e:
        logger.warning(f"Webhook error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# EMOJI PARSER
# ══════════════════════════════════════════════════════════════════════════════

async def parse_emoji_input(client, raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    parsed = parse_link(raw)
    if not parsed.get("message_id"):
        return None
    try:
        entity = await resolve_entity(client, parsed["channel_raw"])
        msg = await client.get_messages(entity, ids=parsed["message_id"])
        if not msg or not msg.entities:
            return None
        for ent in msg.entities:
            if isinstance(ent, MessageEntityCustomEmoji):
                return ent.document_id
    except Exception as e:
        logger.warning(f"parse_emoji_input: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Clone Topic Structure
# ══════════════════════════════════════════════════════════════════════════════

async def clone_all_topics(client, dst_entity, map_key,
                           src_topics, dst_by_title,
                           icon_mode, custom_emoji_id,
                           skip_general,
                           progress_cb=None, stop_event=None) -> dict:
    """
    Clone all src topics → dst forum.
    Returns topic_map {src_id: dst_id}.
    """
    topic_map = load_topic_map(map_key)
    sorted_topics = sorted(src_topics.items(), key=lambda x: x[0])
    total   = len(sorted_topics)
    created = 0
    mapped  = 0
    skipped = 0

    logger.info(f"Phase 1: clone {total} topic(s)…")

    for i, (src_id, info) in enumerate(sorted_topics, 1):
        if stop_event and stop_event.is_set():
            break

        title = info["title"]

        if src_id == 1:
            if skip_general:
                skipped += 1
                continue
            topic_map[1] = 1
            mapped += 1
            save_topic_map(map_key, topic_map)
            continue

        if src_id in topic_map:
            mapped += 1
            continue

        if title in dst_by_title:
            topic_map[src_id] = dst_by_title[title]
            save_topic_map(map_key, topic_map)
            mapped += 1
            continue

        icon_color    = info.get("icon_color")
        icon_emoji_id = None
        if icon_mode == "clone":
            icon_emoji_id = info.get("icon_emoji_id")
        elif icon_mode == "fixed" and custom_emoji_id:
            icon_emoji_id = custom_emoji_id

        new_id = None
        for attempt in range(4):
            try:
                req_kwargs = {
                    "channel":   dst_entity,
                    "title":     title,
                    "random_id": int.from_bytes(os.urandom(8), "little") & 0x7FFFFFFFFFFFFFFF,
                }
                if icon_color is not None:
                    req_kwargs["icon_color"] = icon_color
                if icon_emoji_id:
                    req_kwargs["icon_emoji_id"] = icon_emoji_id

                cr = await client(CreateForumTopicRequest(**req_kwargs))
                for u in cr.updates:
                    if hasattr(u, "message") and hasattr(u.message, "action"):
                        new_id = u.message.id
                        break
                if new_id is None:
                    for u in cr.updates:
                        if hasattr(u, "id") and not hasattr(u, "message"):
                            new_id = u.id
                            break
                break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                if attempt == 3:
                    logger.warning(f"Cannot create topic '{title}': {e}")
                else:
                    await asyncio.sleep(2 * (attempt + 1))

        if new_id:
            topic_map[src_id]   = new_id
            dst_by_title[title] = new_id
            save_topic_map(map_key, topic_map)
            created += 1

        if progress_cb:
            progress_cb({
                "phase":         "clone_topics",
                "topics_done":   i,
                "topics_total":  total,
                "current_topic": title,
            })

        await asyncio.sleep(1)

    logger.info(f"Phase 1 done | created={created} | matched={mapped} | skipped={skipped}")
    return topic_map


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Forward Messages per Topic
# ══════════════════════════════════════════════════════════════════════════════

async def forward_topic_messages(client, src_entity, dst_entity,
                                 src_peer, dst_peer,
                                 src_topic_id, dst_topic_id,
                                 cfg, map_key, stats,
                                 progress_cb=None, stop_event=None):
    """
    Forward all messages of one topic. Returns (count, skipped).

    link_mode=False (default): batch 50 msg/call via ForwardMessagesRequest — fastest.
    link_mode=True: each media message → thumbnail/photo + share-link caption.
      Albums are sent photo-by-photo (each with its own link).
      Text-only messages are sent as-is.
    """
    only_filter   = cfg.get("only_filter", "all")
    hide_sender   = cfg.get("hide_sender", False)
    link_mode     = cfg.get("link_mode", False)
    caption_tpl   = cfg.get("caption_template") or None
    clone_pins    = cfg.get("clone_pins", True)   # pin matching messages in dest

    # src_id → dst_id mapping (populated during forward, used for pinning)
    id_map: dict[int, int] = {}

    topic_state_key = f"{map_key}_topic_{src_topic_id}"
    last_id         = load_last_id(topic_state_key)
    min_id          = last_id if last_id else 0

    count              = 0
    skipped            = 0
    last_id_buf        = last_id or 0
    consecutive_errors = 0

    batch_buffer       = []
    BATCH_SIZE         = 50
    pending_album      = {"gid": None, "msgs": []}

    SKIP_LOG_EVERY            = 20
    MAX_CONSECUTIVE_PERMANENT = 500
    SCAN_SAVE_EVERY           = 500
    skip_batch_count          = 0
    skip_batch_first          = None

    def flush_skip_log(last_id_in_batch=None):
        nonlocal skip_batch_count, skip_batch_first
        if skip_batch_count > 0:
            logger.debug(f"skip {skip_batch_count} msgs (id={skip_batch_first}→{last_id_in_batch})")
            skip_batch_count = 0
            skip_batch_first = None

    def add_to_skip_batch(msg_id):
        nonlocal skip_batch_count, skip_batch_first
        if skip_batch_first is None:
            skip_batch_first = msg_id
        skip_batch_count += 1
        if skip_batch_count >= SKIP_LOG_EVERY:
            flush_skip_log(msg_id)

    def has_pending():
        return bool(batch_buffer) or bool(pending_album["msgs"])

    async def flush_batch():
        nonlocal count, skipped, last_id_buf, consecutive_errors
        if not batch_buffer:
            return
        n        = len(batch_buffer)
        first_id = batch_buffer[0].id
        last_mid = batch_buffer[-1].id
        src_ids  = [m.id for m in batch_buffer]
        try:
            flush_skip_log(first_id - 1)
            result = await send_msgs(client, list(batch_buffer), dst_entity, hide_sender,
                                     dst_topic_id, src_peer, dst_peer)
            # Build src→dest ID mapping from Updates result
            if result is not None:
                dest_ids = _parse_new_ids_from_updates(result)
                if len(dest_ids) == len(src_ids):
                    for s, d in zip(src_ids, dest_ids):
                        id_map[s] = d
            count += n
            stats["normal"] += n
            last_id_buf = last_mid
            save_last_id(topic_state_key, last_id_buf)
            consecutive_errors = 0
            batch_buffer.clear()
            if progress_cb:
                progress_cb({"phase": "forward", "total_forwarded": count + stats.get("_base", 0)})
        except PermanentForwardError:
            fallback = list(batch_buffer)
            batch_buffer.clear()
            for m in fallback:
                try:
                    await send_msgs(client, [m], dst_entity, hide_sender,
                                    dst_topic_id, src_peer, dst_peer)
                    count += 1
                    stats["normal"] += 1
                    last_id_buf = m.id
                    save_last_id(topic_state_key, last_id_buf)
                    consecutive_errors = 0
                except PermanentForwardError:
                    skipped += 1
                    stats["skipped"] += 1
                    consecutive_errors += 1
                    last_id_buf = m.id
                    save_last_id(topic_state_key, last_id_buf)
                    add_to_skip_batch(m.id)
                except Exception as ex:
                    stats["errors"] += 1
                    last_id_buf = m.id
                    save_last_id(topic_state_key, last_id_buf)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Batch ({first_id}→{last_mid}) error: {type(e).__name__}: {e}")
            last_id_buf = last_mid
            save_last_id(topic_state_key, last_id_buf)
            batch_buffer.clear()

    async def flush_pending_album():
        nonlocal count, skipped, last_id_buf, consecutive_errors
        if not pending_album["msgs"]:
            return
        album    = list(pending_album["msgs"])
        n        = len(album)
        first_id = album[0].id
        last_mid = album[-1].id
        pending_album["gid"]  = None
        pending_album["msgs"] = []

        # ── Link mode: send_file(media_list) + caption gốc + link ──────────────
        if link_mode:
            flush_skip_log(first_id - 1)
            try:
                sent_list = await send_album_as_links(
                    client, album, dst_entity,
                    dst_topic_id if dst_topic_id and dst_topic_id != 1 else None,
                    caption_tpl,
                )
                if sent_list:
                    last_sent = sent_list[-1]
                    if last_sent:
                        for m in album:
                            id_map[m.id] = last_sent.id
                    count += n
                    stats["album"] += 1
                    stats["links_created"] = stats.get("links_created", 0) + 1
                else:
                    skipped += n; stats["skipped"] += n
                last_id_buf = last_mid
                save_last_id(topic_state_key, last_id_buf)
                consecutive_errors = 0
            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"album link_mode ({first_id}→{last_mid}): {e}")
                last_id_buf = last_mid
                save_last_id(topic_state_key, last_id_buf)
            if progress_cb:
                progress_cb({"phase": "forward",
                             "total_forwarded": count + stats.get("_base", 0),
                             "links_created": stats.get("links_created", 0)})
            return

        # ── Normal mode: forward whole album in one API call ─────────────────
        alb_src_ids = [m.id for m in album]
        try:
            flush_skip_log(first_id - 1)
            result = await send_msgs(client, album, dst_entity, hide_sender,
                                     dst_topic_id, src_peer, dst_peer)
            # Build ID mapping for album items
            if result is not None:
                dest_ids = _parse_new_ids_from_updates(result)
                if len(dest_ids) == len(alb_src_ids):
                    for s, d in zip(alb_src_ids, dest_ids):
                        id_map[s] = d
            count += n
            stats["album"] += 1
            last_id_buf = last_mid
            save_last_id(topic_state_key, last_id_buf)
            consecutive_errors = 0
            if progress_cb:
                progress_cb({"phase": "forward", "total_forwarded": count + stats.get("_base", 0)})
        except PermanentForwardError as e:
            skipped += n
            stats["skipped"] += n
            consecutive_errors += 1
            last_id_buf = last_mid
            save_last_id(topic_state_key, last_id_buf)
            add_to_skip_batch(last_mid)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"Album ({first_id}→{last_mid}) error: {e}")
            last_id_buf = last_mid
            save_last_id(topic_state_key, last_id_buf)

    if src_topic_id == 1:
        iterator       = client.iter_messages(src_entity, min_id=min_id, reverse=True)
        filter_general = True
    else:
        iterator       = client.iter_messages(src_entity, reply_to=src_topic_id,
                                              min_id=min_id, reverse=True)
        filter_general = False

    scan_count = 0

    async for msg in iterator:
        if stop_event and stop_event.is_set():
            break

        scan_count += 1

        if not isinstance(msg, Message):
            if not has_pending():
                last_id_buf = getattr(msg, "id", last_id_buf)
                if last_id_buf and scan_count % SCAN_SAVE_EVERY == 0:
                    save_last_id(topic_state_key, last_id_buf)
            continue

        if getattr(msg, "action", None) is not None:
            skipped += 1
            stats["skipped"] += 1
            if not has_pending():
                last_id_buf = msg.id
                save_last_id(topic_state_key, last_id_buf)
            add_to_skip_batch(msg.id)
            continue

        if filter_general:
            rt      = getattr(msg, "reply_to", None)
            msg_top = getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)
            if msg_top is not None and msg_top != 1:
                if not has_pending():
                    last_id_buf = msg.id
                    if scan_count % SCAN_SAVE_EVERY == 0:
                        save_last_id(topic_state_key, last_id_buf)
                continue

        if msg.grouped_id:
            if pending_album["gid"] != msg.grouped_id:
                await flush_pending_album()
                if not link_mode:
                    await flush_batch()
                pending_album["gid"]  = msg.grouped_id
                pending_album["msgs"] = [msg]
            else:
                pending_album["msgs"].append(msg)
            continue

        if pending_album["msgs"]:
            await flush_pending_album()

        def _skip_media():
            nonlocal last_id_buf
            if not has_pending():
                last_id_buf = msg.id
                save_last_id(topic_state_key, last_id_buf)

        if only_filter == "media" and not msg.media:
            _skip_media(); continue
        if only_filter == "video" and not msg.video:
            _skip_media(); continue
        if only_filter == "photo" and not msg.photo:
            _skip_media(); continue

        # ── Link mode: send_file(msg.media) + caption gốc + link ────────────────
        if link_mode:
            try:
                flush_skip_log(msg.id - 1)
                sent = await send_single_as_link(
                    client, msg, dst_entity,
                    dst_topic_id if dst_topic_id and dst_topic_id != 1 else None,
                    caption_tpl,
                )
                if sent:
                    id_map[msg.id] = sent.id
                    count += 1
                    stats["normal"] += 1
                    stats["links_created"] = stats.get("links_created", 0) + 1
                else:
                    skipped += 1; stats["skipped"] += 1

                last_id_buf = msg.id
                save_last_id(topic_state_key, last_id_buf)
                consecutive_errors = 0
                if progress_cb:
                    progress_cb({"phase": "forward",
                                 "total_forwarded": count + stats.get("_base", 0),
                                 "links_created": stats.get("links_created", 0)})
                await asyncio.sleep(0.1)
            except FloodWaitError as e:
                stats["flood_wait"] = stats.get("flood_wait", 0) + 1
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                stats["errors"] += 1
                consecutive_errors += 1
                logger.warning(f"link_mode id={msg.id}: {type(e).__name__}: {e}")
                last_id_buf = msg.id
                save_last_id(topic_state_key, last_id_buf)
                await asyncio.sleep(1)
            continue

        # ── Normal batch mode ─────────────────────────────────────────────────
        batch_buffer.append(msg)
        if len(batch_buffer) >= BATCH_SIZE:
            await flush_batch()
            if consecutive_errors >= MAX_CONSECUTIVE_PERMANENT:
                flush_skip_log(last_id_buf)
                logger.warning("Too many consecutive permanent errors — stopping topic")
                break

    await flush_pending_album()
    if not link_mode:
        await flush_batch()
    flush_skip_log(last_id_buf)

    if last_id_buf:
        save_last_id(topic_state_key, last_id_buf)

    # ── Clone pinned messages ─────────────────────────────────────────────────
    pinned_count = 0
    if clone_pins and id_map:
        logger.info(f"Cloning pinned messages for topic={src_topic_id}…")
        pinned_count = await clone_pinned_messages(
            client, src_entity, dst_entity,
            src_topic_id, id_map,
        )
        if pinned_count:
            stats["pins_cloned"] = stats.get("pins_cloned", 0) + pinned_count
            logger.info(f"Pinned {pinned_count} message(s) in dst topic {dst_topic_id}")

    return count, skipped


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUNNERS
# ══════════════════════════════════════════════════════════════════════════════

async def run_session(client: TelegramClient, cfg: dict,
                      progress_cb=None, stop_event=None):
    """Forward session (single channel, optional topic filter)."""
    src_raw        = cfg["src_raw"]
    dst_raw        = cfg["dst_raw"]
    start_msg_id   = cfg.get("start_msg_id")
    force_topic_id = cfg.get("force_topic_id")
    only_filter    = cfg.get("only_filter", "all")
    hide_sender    = cfg.get("hide_sender", False)
    webhook_url    = cfg.get("webhook_url", "")
    auto_topic     = cfg.get("auto_topic", True)
    link_mode      = cfg.get("link_mode", False)
    caption_tpl    = cfg.get("caption_template") or None
    clone_pins     = cfg.get("clone_pins", True)

    try:
        src_entity = await resolve_entity(client, src_raw)
        dst_entity = await resolve_entity(client, dst_raw)
    except Exception as e:
        raise RuntimeError(f"Cannot resolve entities: {e}")

    src_peer = await client.get_input_entity(src_entity)
    dst_peer = await client.get_input_entity(dst_entity)
    key      = make_key(src_entity.id, dst_entity.id)

    src_name = str(getattr(src_entity, "title", src_entity.id))
    dst_name = str(getattr(dst_entity, "title", dst_entity.id))
    save_session_meta(key, src_name, dst_name, "forward", cfg)

    src_forum  = is_forum(src_entity)
    dst_forum  = is_forum(dst_entity)
    use_topics = src_forum and dst_forum

    topic_map    = {}
    src_titles   = {}
    dst_by_title = {}
    filter_topic_id = None

    if src_forum and force_topic_id:
        filter_topic_id = force_topic_id

    if use_topics:
        topic_map    = load_topic_map(key)
        src_titles   = await preload_topics(client, src_entity)
        dst_by_title = {v: k for k, v in (await preload_topics(client, dst_entity)).items()}

    last_id = load_last_id(key)
    if last_id:
        start_msg_id = max(start_msg_id or 0, last_id)

    min_id = (start_msg_id - 1) if start_msg_id else 0

    count      = 0
    last_id_buf = 0
    SAVE_EVERY = 50
    stats      = {"normal": 0, "album": 0, "flood_wait": 0, "errors": 0}
    pending_album = {"gid": None, "msgs": [], "dst_topic": None}

    async def flush_pending():
        nonlocal count, last_id_buf
        if not pending_album["msgs"]:
            return
        album = list(pending_album["msgs"])
        dt    = pending_album["dst_topic"]
        pending_album["gid"]  = None
        pending_album["msgs"] = []
        pending_album["dst_topic"] = None

        if link_mode:
            # send_file(media_list) + caption gốc + link (không tải lại, không Forwarded-from)
            try:
                n = len(album)
                sent_list = await send_album_as_links(
                    client, album, dst_entity,
                    dt if dt and dt != 1 else None,
                    caption_tpl,
                )
                if sent_list:
                    count += n; stats["album"] += 1
                    stats["links_created"] = stats.get("links_created", 0) + 1
                last_id_buf = album[-1].id
            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"album link_mode: {e}")
                last_id_buf = album[-1].id
            return

        try:
            await send_msgs(client, album, dst_entity, hide_sender, dt, src_peer, dst_peer)
            count += len(album)
            stats["album"] += 1
            last_id_buf = album[-1].id
        except FloodWaitError as e:
            stats["flood_wait"] += 1
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"album id={album[0].id}→{album[-1].id}: {e}")
            last_id_buf = album[-1].id
            await asyncio.sleep(1)

    async for msg in client.iter_messages(src_entity, min_id=min_id, reverse=True):
        if stop_event and stop_event.is_set():
            break
        if not isinstance(msg, Message):
            continue
        if getattr(msg, "action", None) is not None:
            continue
        if only_filter == "media" and not msg.media:
            continue
        if only_filter == "video" and not msg.video:
            continue
        if only_filter == "photo" and not msg.photo:
            continue
        if filter_topic_id is not None:
            rt      = getattr(msg, "reply_to", None)
            msg_top = getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)
            if msg_top != filter_topic_id and msg.id != filter_topic_id:
                last_id_buf = msg.id
                continue

        dst_topic = None
        if use_topics:
            rt      = getattr(msg, "reply_to", None)
            src_top = getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)
            if src_top:
                dst_topic = await get_or_create_topic_fast(
                    client, dst_entity, src_top,
                    topic_map, key, auto_topic,
                    src_titles, dst_by_title,
                )

        try:
            if msg.grouped_id:
                if pending_album["gid"] != msg.grouped_id:
                    await flush_pending()
                    pending_album["gid"]       = msg.grouped_id
                    pending_album["msgs"]      = [msg]
                    pending_album["dst_topic"] = dst_topic
                else:
                    pending_album["msgs"].append(msg)
                if count % SAVE_EVERY == 0 and last_id_buf:
                    save_last_id(key, last_id_buf)
                continue

            if pending_album["msgs"]:
                await flush_pending()

            # ── Link mode: send_file(msg.media) + caption + link ─────────────────
            if link_mode and _media_type_of(msg):
                sent = await send_single_as_link(
                    client, msg, dst_entity,
                    dst_topic if dst_topic and dst_topic != 1 else None,
                    caption_tpl,
                )
                if sent:
                    id_map[msg.id] = sent.id
                    count += 1; stats["normal"] += 1
                    stats["links_created"] = stats.get("links_created", 0) + 1
                last_id_buf = msg.id
            else:
                await send_msgs(client, [msg], dst_entity, hide_sender, dst_topic, src_peer, dst_peer)
                count += 1
                stats["normal"] += 1
                last_id_buf = msg.id

            if count % SAVE_EVERY == 0 and last_id_buf:
                save_last_id(key, last_id_buf)
                if progress_cb:
                    progress_cb({"phase": "forward", "total_forwarded": count,
                                 "links_created": stats.get("links_created", 0)})

        except FloodWaitError as e:
            stats["flood_wait"] += 1
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"id={msg.id}: {e}")
            if last_id_buf > 1:
                save_last_id(key, last_id_buf)
            await asyncio.sleep(1)

    await flush_pending()
    if last_id_buf:
        save_last_id(key, last_id_buf)

    final_progress = {
        "total_forwarded": count, "errors": stats["errors"],
        "links_created": stats.get("links_created", 0),
    }
    save_session_meta(key, src_name, dst_name, "forward", cfg, progress=final_progress)

    if progress_cb:
        progress_cb({**final_progress, "phase": "done", "stats": stats})

    if webhook_url:
        await notify_webhook(webhook_url, {
            "event": "forward_done",
            "source": src_raw, "destination": dst_raw,
            "total": count, "normal": stats["normal"],
            "albums": stats["album"], "errors": stats["errors"],
            "links_created": stats.get("links_created", 0),
        })

    return {"key": key, "count": count, "stats": stats}


async def run_forum_backup(client: TelegramClient, cfg: dict,
                           progress_cb=None, stop_event=None):
    """Full forum backup: clone topic structure then forward all messages."""
    src_raw      = cfg["src_raw"]
    dst_raw      = cfg["dst_raw"]
    icon_mode    = cfg.get("icon_mode", "clone")
    emoji_raw    = cfg.get("emoji_raw", "")
    skip_general = cfg.get("skip_general", False)
    webhook_url  = cfg.get("webhook_url", "")

    try:
        src_entity = await resolve_entity(client, src_raw)
        dst_entity = await resolve_entity(client, dst_raw)
    except Exception as e:
        raise RuntimeError(f"Cannot resolve entities: {e}")

    if not is_forum(src_entity):
        raise RuntimeError("Source is not a forum — enable Topics first")
    if not is_forum(dst_entity):
        raise RuntimeError("Destination is not a forum — enable Topics first")

    src_peer = await client.get_input_entity(src_entity)
    dst_peer = await client.get_input_entity(dst_entity)
    map_key  = make_key(src_entity.id, dst_entity.id)

    src_name = str(getattr(src_entity, "title", src_entity.id))
    dst_name = str(getattr(dst_entity, "title", dst_entity.id))
    save_session_meta(map_key, src_name, dst_name, "backup", cfg)

    # Parse emoji (icon_mode=fixed)
    custom_emoji_id = None
    if icon_mode == "fixed" and emoji_raw:
        custom_emoji_id = await parse_emoji_input(client, emoji_raw)
        if not custom_emoji_id:
            icon_mode = "none"

    # Preload topics
    src_topics = await preload_topics_full(client, src_entity)
    dst_topics = await preload_topics_full(client, dst_entity)
    dst_by_title = {info["title"]: tid for tid, info in dst_topics.items()}

    if not src_topics:
        raise RuntimeError("Cannot load source topics — admin permission required")

    # ── Phase 1: clone topic structure ────────────────────────────────────────
    if progress_cb:
        progress_cb({"phase": "clone_topics", "topics_total": len(src_topics)})

    topic_map = await clone_all_topics(
        client, dst_entity, map_key,
        src_topics, dst_by_title,
        icon_mode, custom_emoji_id,
        skip_general, progress_cb, stop_event,
    )

    # ── Phase 2: forward messages per topic ───────────────────────────────────
    stats = {"normal": 0, "album": 0, "flood_wait": 0, "errors": 0, "skipped": 0}
    total_forwarded = 0
    total_skipped   = 0
    per_topic_count = {}
    sorted_src_ids  = sorted(src_topics.keys())
    total_topics    = len(sorted_src_ids)

    for idx, src_id in enumerate(sorted_src_ids, 1):
        if stop_event and stop_event.is_set():
            break

        title = src_topics[src_id]["title"]
        if src_id not in topic_map:
            continue

        dst_id = topic_map[src_id]
        # Carry base count for progress reporting
        stats["_base"] = total_forwarded

        if progress_cb:
            progress_cb({
                "phase":         "forward",
                "topics_done":   idx - 1,
                "topics_total":  total_topics,
                "current_topic": title,
                "total_forwarded": total_forwarded,
            })

        try:
            n, sk = await forward_topic_messages(
                client, src_entity, dst_entity,
                src_peer, dst_peer,
                src_id, dst_id,
                cfg, map_key, stats,
                progress_cb=progress_cb,
                stop_event=stop_event,
            )
            per_topic_count[title] = n
            total_forwarded += n
            total_skipped   += sk
        except Exception as e:
            logger.error(f"forward topic '{title}': {e}")
            stats["errors"] += 1

        save_session_meta(map_key, src_name, dst_name, "backup", cfg, progress={
            "total_forwarded": total_forwarded,
            "topics_done":     idx,
            "topics_total":    total_topics,
            "current_topic":   title,
        })

    pins_cloned = stats.get("pins_cloned", 0)
    final_progress = {
        "total_forwarded": total_forwarded,
        "topics_done":     total_topics,
        "topics_total":    total_topics,
        "errors":          stats["errors"],
        "skipped":         total_skipped,
        "pins_cloned":     pins_cloned,
    }
    save_session_meta(map_key, src_name, dst_name, "backup", cfg, progress=final_progress)

    if progress_cb:
        progress_cb({**final_progress, "phase": "done", "stats": stats})

    if webhook_url:
        await notify_webhook(webhook_url, {
            "event": "forum_backup_done",
            "source": src_raw, "destination": dst_raw,
            "total": total_forwarded, "normal": stats["normal"],
            "albums": stats["album"], "skipped": total_skipped,
            "errors": stats["errors"], "topics": len(per_topic_count),
            "pins_cloned": pins_cloned,
            "per_topic": per_topic_count,
        })

    return {"key": map_key, "count": total_forwarded, "stats": stats, "per_topic": per_topic_count}
