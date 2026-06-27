"""
Force-join membership gate.

Flow:
  1. Bất kỳ user nào dùng bot → check_membership() được gọi
  2. Nếu user KHÔNG ở trong kênh bắt buộc → gửi nút "Tham gia kênh" + chặn
  3. Cache kết quả (mặc định 5 phút) để không gọi API mỗi tin nhắn
  4. Sau cache hết hạn → check lại → nếu đã out → chặn ngay
  5. Admin và ADMIN_IDS luôn được bypass

Cài đặt qua /set hoặc web dashboard:
  force_join_channel   : @username hoặc -100xxx (để trống = tắt)
  force_join_check_sec : số giây giữa 2 lần kiểm tra (mặc định 300 = 5 phút)
  force_join_message   : tin nhắn hiện khi chưa tham gia

Kết quả cache:
  _cache[user_id] = {"ok": bool, "ts": float}
  ok=True  → thành viên, lưu đến ts + check_sec
  ok=False → không phải thành viên, lưu 60 giây (recheck nhanh hơn)
"""

import time
import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus

from database.models import get_setting
from config.settings import ADMIN_IDS

logger = logging.getLogger(__name__)

# ─── In-memory cache ──────────────────────────────────────────────────────────
# { user_id: {"ok": bool, "ts": float} }
_cache: dict[int, dict] = {}

# TTL khi là thành viên (giây) — đọc từ setting mỗi lần
_DEFAULT_CHECK_SEC = 300   # 5 phút
# TTL khi KHÔNG là thành viên (giây) — recheck nhanh hơn
_NON_MEMBER_TTL = 60


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_channel() -> str:
    """Return configured channel username/ID or empty string."""
    return (get_setting("force_join_channel", "") or "").strip()


def _get_check_sec() -> int:
    try:
        return int(get_setting("force_join_check_sec", str(_DEFAULT_CHECK_SEC)))
    except (ValueError, TypeError):
        return _DEFAULT_CHECK_SEC


def _get_join_message() -> str:
    return get_setting(
        "force_join_message",
        "🔒 Bạn cần tham gia kênh để sử dụng bot.\nNhấn nút bên dưới, rồi nhấn /start lại."
    ) or ""


def _cached_ok(user_id: int) -> bool | None:
    """
    Return True/False if cache is fresh, None if expired/missing.
    """
    entry = _cache.get(user_id)
    if not entry:
        return None
    check_sec = _get_check_sec() if entry["ok"] else _NON_MEMBER_TTL
    if time.time() - entry["ts"] < check_sec:
        return entry["ok"]
    return None   # expired


def _set_cache(user_id: int, ok: bool):
    _cache[user_id] = {"ok": ok, "ts": time.time()}


def invalidate(user_id: int):
    """Force re-check on next request (e.g. after user presses Join)."""
    _cache.pop(user_id, None)


# ─── Core check ───────────────────────────────────────────────────────────────

MEMBER_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}


async def is_member(bot: Bot, user_id: int, channel: str) -> bool:
    """
    Ask Telegram API whether user_id is a member of channel.

    - User chắc chắn KHÔNG ở kênh (status left/kicked) → False (chặn)
    - Lỗi cấu hình (bot không vào kênh, sai username) → True (fail-open,
      tránh khóa toàn bộ user vì admin cấu hình sai)
    """
    try:
        cm = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        is_in = cm.status in MEMBER_STATUSES
        if not is_in:
            logger.info(f"is_member: user {user_id} KHÔNG ở {channel} (status={cm.status})")
        return is_in
    except Exception as e:
        msg = str(e).lower()
        # Lỗi cấu hình → fail-open (cho qua) nhưng cảnh báo rõ
        if "not found" in msg or "invalid" in msg or "no rights" in msg:
            logger.warning(
                f"⚠️ FORCE-JOIN CẤU HÌNH SAI: bot không truy cập được kênh '{channel}' "
                f"({e}). Đang cho qua để không khóa user. "
                f"Dùng /forcejoin @kênh để set lại."
            )
            return True
        # Lỗi khác (user bị hạn chế...) → fail-open
        logger.warning(f"is_member({user_id}, {channel}): {e} — cho qua")
        return True


async def check_user(bot: Bot, user_id: int) -> bool:
    """
    Full membership check with cache.
    Returns True if user is allowed (no channel configured, is admin, or is member).
    """
    # Admins always pass
    if user_id in ADMIN_IDS:
        return True

    channel = _get_channel()
    if not channel:
        return True   # feature disabled

    # Check cache first
    cached = _cached_ok(user_id)
    if cached is not None:
        return cached

    # Call Telegram API
    ok = await is_member(bot, user_id, channel)
    _set_cache(user_id, ok)
    if ok:
        logger.info(f"Membership OK: user={user_id} channel={channel}")
    else:
        logger.info(f"Membership FAIL: user={user_id} channel={channel}")
    return ok


# ─── Decorator / gate function for handlers ───────────────────────────────────

async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Call at the start of every user-facing handler.
    Returns True if user may proceed, False if blocked (message already sent).

    Usage:
        if not await gate(update, ctx):
            return
    """
    user = update.effective_user
    if not user:
        return True

    ok = await check_user(ctx.bot, user.id)
    if ok:
        return True

    # Build "join" button
    channel = _get_channel()
    msg     = _get_join_message()

    # Build invite URL
    if channel.startswith("@"):
        invite_url = f"https://t.me/{channel.lstrip('@')}"
    elif channel.lstrip("-").isdigit():
        # Private channel — can't build direct link without invite link
        # Use a generic "check" button instead
        invite_url = None
    else:
        invite_url = f"https://t.me/{channel}"

    buttons = []
    if invite_url:
        buttons.append([InlineKeyboardButton("📢 Tham gia kênh", url=invite_url)])
    buttons.append([InlineKeyboardButton("✅ Tôi đã tham gia — Kiểm tra lại", callback_data="check_membership")])

    reply_msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if reply_msg:
        await reply_msg.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    return False
