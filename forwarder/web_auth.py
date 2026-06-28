"""
Đăng nhập Telethon qua web (nhiều bước: phone → code → 2FA).

Vì Telethon login bất đồng bộ và có state, ta giữ 1 client tạm trong 1 thread
riêng có event loop riêng, gọi các bước qua run_coroutine_threadsafe.

Luồng:
  1. start_login(phone)  → gửi mã OTP, lưu phone_code_hash
  2. submit_code(code)   → đăng nhập; nếu cần 2FA → trả "need_password"
  3. submit_password(pw) → đăng nhập với mật khẩu 2FA
  → Khi xong, session lưu vào forwarder_state/<TELETHON_SESSION>.session
"""

import asyncio
import threading
import logging
import os

logger = logging.getLogger(__name__)


class WebAuth:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._client = None
        self._phone = None
        self._phone_hash = None
        self._lock = threading.Lock()

    def _ensure_loop(self):
        if self._loop and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="webauth-loop")
        self._thread.start()

    def _run(self, coro, timeout=60):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── Bước 1: gửi mã ──────────────────────────────────────────────────────
    def start_login(self, phone: str) -> dict:
        from config.settings import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION
        from forwarder.state import STATE_DIR
        from telethon import TelegramClient

        if not TELETHON_API_ID or not TELETHON_API_HASH:
            return {"ok": False, "error": "Chưa đặt TELETHON_API_ID / API_HASH trong .env"}

        self._ensure_loop()
        session_path = os.path.join(STATE_DIR, TELETHON_SESSION)

        async def _do():
            self._client = TelegramClient(session_path, TELETHON_API_ID, TELETHON_API_HASH)
            await self._client.connect()
            if await self._client.is_user_authorized():
                return {"ok": True, "already": True}
            sent = await self._client.send_code_request(phone)
            self._phone = phone
            self._phone_hash = sent.phone_code_hash
            return {"ok": True, "already": False}

        try:
            return self._run(_do())
        except Exception as e:
            logger.error(f"start_login: {e}")
            return {"ok": False, "error": str(e)}

    # ── Bước 2: nhập mã OTP ─────────────────────────────────────────────────
    def submit_code(self, code: str) -> dict:
        from telethon.errors import SessionPasswordNeededError
        if not self._client:
            return {"ok": False, "error": "Chưa bắt đầu đăng nhập"}

        async def _do():
            try:
                await self._client.sign_in(self._phone, code,
                                           phone_code_hash=self._phone_hash)
                me = await self._client.get_me()
                await self._client.disconnect()
                return {"ok": True, "done": True,
                        "name": me.first_name, "username": me.username}
            except SessionPasswordNeededError:
                return {"ok": True, "need_password": True}

        try:
            return self._run(_do())
        except Exception as e:
            logger.error(f"submit_code: {e}")
            return {"ok": False, "error": str(e)}

    # ── Bước 3: nhập mật khẩu 2FA ───────────────────────────────────────────
    def submit_password(self, password: str) -> dict:
        if not self._client:
            return {"ok": False, "error": "Chưa bắt đầu đăng nhập"}

        async def _do():
            await self._client.sign_in(password=password)
            me = await self._client.get_me()
            await self._client.disconnect()
            return {"ok": True, "done": True,
                    "name": me.first_name, "username": me.username}

        try:
            return self._run(_do())
        except Exception as e:
            logger.error(f"submit_password: {e}")
            return {"ok": False, "error": str(e)}


_web_auth = WebAuth()


def get_web_auth() -> WebAuth:
    return _web_auth
