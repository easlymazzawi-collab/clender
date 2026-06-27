"""
Background session runner.
Each forwarder session runs in its own thread with a dedicated asyncio event loop.
The TelegramClient is created INSIDE the worker thread (not in the Flask/main thread)
to avoid 'There is no current event loop in thread' errors on Python 3.10+.
"""

import asyncio
import threading
import time
import logging
import os

from .state import (
    db_set_session_status, db_set_session_progress,
    save_session_meta, sync_file_sessions_to_db,
    STATE_DIR,
)
from .core import run_session, run_forum_backup, run_relink

logger = logging.getLogger(__name__)


class ForwarderRunner:
    """
    Manages multiple concurrent forwarder sessions.
    Client config is stored here; actual TelegramClient is created
    per-session inside each worker thread's asyncio event loop.
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Client config (set by init_client); no TelegramClient created here
        self._client_config: tuple | None = None   # (api_id, api_hash, session_path, phone)

    # ── Client config ──────────────────────────────────────────────────────────

    def init_client(self, api_id: int, api_hash: str, session_name: str,
                    phone: str | None = None):
        """
        Store Telethon credentials. The actual TelegramClient is created
        inside each worker thread to avoid event-loop thread issues.
        """
        session_path = os.path.join(STATE_DIR, session_name)
        self._client_config = (api_id, api_hash, session_path, phone)
        logger.info(f"Forwarder configured: session={session_path}")
        return self   # return self so caller can check is_configured()

    def is_configured(self) -> bool:
        return self._client_config is not None

    # ── Session management ────────────────────────────────────────────────────

    def is_running(self, key: str) -> bool:
        with self._lock:
            s = self._sessions.get(key)
            return s is not None and s.get("status") == "running"

    def get_status(self, key: str) -> dict:
        with self._lock:
            return dict(self._sessions.get(key, {}))

    def all_statuses(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._sessions.items()}

    def stop_session(self, key: str):
        with self._lock:
            s = self._sessions.get(key)
            if s and "stop_event" in s:
                s["stop_event"].set()
                s["status"] = "stopping"
                logger.info(f"Stop signal sent to session {key}")

    def start_session(self, cfg: dict, mode: str = "forward") -> str:
        """
        Start a forwarder session in a background thread.
        Returns a temporary session key (will be replaced with real src_dst key).
        """
        if not self._client_config:
            raise RuntimeError(
                "Telethon chưa được cấu hình. "
                "Kiểm tra TELETHON_API_ID, TELETHON_API_HASH và session file."
            )

        tmp_key    = f"pending_{int(time.time())}"
        stop_event = asyncio.Event()

        with self._lock:
            session_info = {
                "key":        tmp_key,
                "cfg":        cfg,
                "mode":       mode,
                "status":     "starting",
                "stop_event": stop_event,
                "progress":   {},
                "started_at": time.time(),
                "log":        [],
                "error":      None,
            }
            self._sessions[tmp_key] = session_info

        client_config = self._client_config  # capture for thread

        def progress_cb(p: dict):
            with self._lock:
                real_key = session_info.get("real_key", tmp_key)
                session_info["progress"] = p
                session_info["status"]   = "running" if p.get("phase") != "done" else "done"
                if p.get("phase") == "done":
                    db_set_session_status(real_key, "done")
                db_set_session_progress(real_key, p)
                session_info["log"].append(f"[{_ts()}] {_fmt_progress(p)}")
                if len(session_info["log"]) > 500:
                    session_info["log"] = session_info["log"][-400:]

        def run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    _async_run(client_config, cfg, mode, stop_event,
                               progress_cb, session_info, tmp_key, self)
                )
            except Exception as e:
                logger.error(f"Session {tmp_key} crashed: {e}", exc_info=True)
                with self._lock:
                    session_info["status"] = "error"
                    session_info["error"]  = str(e)
                real_key = session_info.get("real_key", tmp_key)
                db_set_session_status(real_key, "error")
            finally:
                loop.close()
                with self._lock:
                    if session_info.get("status") not in ("done", "error"):
                        session_info["status"] = "stopped"
                real_key = session_info.get("real_key", tmp_key)
                db_set_session_status(real_key, session_info.get("status", "stopped"))

        t = threading.Thread(target=run_in_thread, daemon=True,
                             name=f"fwd-{tmp_key}")
        t.start()
        with self._lock:
            session_info["thread"] = t

        return tmp_key

    def cleanup_done(self):
        """Remove done/error sessions older than 1h from in-memory store."""
        cutoff = time.time() - 3600
        with self._lock:
            to_del = [
                k for k, v in self._sessions.items()
                if v.get("status") in ("done", "error", "stopped")
                and v.get("started_at", 0) < cutoff
            ]
            for k in to_del:
                del self._sessions[k]


async def _async_run(client_config, cfg, mode, stop_event,
                     progress_cb, session_info, tmp_key, runner):
    """
    Runs inside the worker thread's own asyncio event loop.
    Creates a fresh TelegramClient here (safe — loop already set).
    """
    from telethon import TelegramClient

    api_id, api_hash, session_path, phone = client_config

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session không hợp lệ. "
                "Chạy lại: python run.py --auth"
            )

        if mode == "backup":
            result = await run_forum_backup(client, cfg,
                                            progress_cb=progress_cb,
                                            stop_event=stop_event)
        elif mode == "relink":
            result = await run_relink(client, cfg,
                                      progress_cb=progress_cb,
                                      stop_event=stop_event)
        else:
            result = await run_session(client, cfg,
                                       progress_cb=progress_cb,
                                       stop_event=stop_event)

        real_key = result.get("key", tmp_key)
        with runner._lock:
            if real_key != tmp_key:
                runner._sessions[real_key] = session_info
                runner._sessions.pop(tmp_key, None)
            session_info["real_key"] = real_key
            session_info["key"]      = real_key
            session_info["status"]   = "done"
            session_info["result"]   = result

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _fmt_progress(p: dict) -> str:
    phase = p.get("phase", "")
    parts = []
    if phase == "clone_topics":
        parts.append(f"Tạo topic {p.get('topics_done',0)}/{p.get('topics_total',0)}: {p.get('current_topic','')}")
    elif phase == "forward":
        parts.append(f"Forward {p.get('total_forwarded',0)} msg")
        if p.get("links_created"):
            parts.append(f"link: {p['links_created']}")
        if p.get("current_topic"):
            parts.append(f"topic: {p['current_topic']}")
        if p.get("topics_done") is not None and p.get("topics_total"):
            parts.append(f"({p['topics_done']}/{p['topics_total']} topics)")
    elif phase == "done":
        parts.append(f"✅ Xong! {p.get('total_forwarded',0)} msg, lỗi: {p.get('errors',0)}")
    return " | ".join(parts) if parts else str(p)


# ─── Singleton ────────────────────────────────────────────────────────────────

_runner = ForwarderRunner()


def get_runner() -> ForwarderRunner:
    return _runner
