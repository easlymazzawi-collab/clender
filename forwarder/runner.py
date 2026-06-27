"""
Background session runner.
Each forwarder session runs in its own thread with a dedicated asyncio event loop.
Progress is pushed to the DB and to an in-memory store for SSE streaming.
"""

import asyncio
import threading
import time
import logging
import os
from collections import defaultdict
from typing import Callable

from telethon import TelegramClient

from .state import (
    db_set_session_status, db_set_session_progress,
    save_session_meta, sync_file_sessions_to_db,
    make_key, STATE_DIR,
)
from .core import run_session, run_forum_backup

logger = logging.getLogger(__name__)


# ─── Singleton runner ─────────────────────────────────────────────────────────

class ForwarderRunner:
    """
    Manages multiple concurrent forwarder sessions.
    Thread-safe: the dict `_sessions` is protected by a lock.
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}   # key → session info
        self._lock = threading.Lock()
        self._client: TelegramClient | None = None

    # ── Telethon client management ────────────────────────────────────────────

    def init_client(self, api_id: int, api_hash: str, session_name: str,
                    phone: str | None = None) -> TelegramClient:
        """Create (or return existing) Telethon client and ensure it's connected."""
        if self._client is None:
            # Import STATE_DIR from state module to get the resolved absolute path
            from .state import STATE_DIR as _state_dir
            session_path = os.path.join(_state_dir, session_name)
            self._client = TelegramClient(session_path, api_id, api_hash)
        return self._client

    def get_client(self) -> TelegramClient | None:
        return self._client

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

    def start_session(self, cfg: dict, mode: str = "forward") -> str:
        """
        Start a forwarder session in a background thread.
        Returns the session key.
        Raises RuntimeError if Telethon client is not initialized.
        """
        client = self._client
        if client is None:
            raise RuntimeError("Telethon client not initialized. Run auth first.")

        src_raw = cfg["src_raw"]
        dst_raw = cfg["dst_raw"]
        # Derive a temporary key — will be replaced with real IDs after entity resolve
        # Use raw strings as placeholder
        tmp_key = f"pending_{int(time.time())}"

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
            }
            self._sessions[tmp_key] = session_info

        def progress_cb(p: dict):
            with self._lock:
                real_key = session_info.get("real_key", tmp_key)
                session_info["progress"] = p
                session_info["status"] = "running" if p.get("phase") != "done" else "done"
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
                loop.run_until_complete(_async_run(
                    client, cfg, mode, stop_event, progress_cb, session_info, tmp_key
                ))
            except Exception as e:
                logger.error(f"Session {tmp_key} crashed: {e}", exc_info=True)
                with self._lock:
                    session_info["status"]  = "error"
                    session_info["error"]   = str(e)
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
            to_del = [k for k, v in self._sessions.items()
                      if v.get("status") in ("done", "error", "stopped")
                      and v.get("started_at", 0) < cutoff]
            for k in to_del:
                del self._sessions[k]


async def _async_run(client, cfg, mode, stop_event, progress_cb, session_info, tmp_key):
    """Actual async run inside the thread's event loop."""
    # Ensure client is connected
    if not client.is_connected():
        await client.connect()

    if mode == "backup":
        result = await run_forum_backup(client, cfg, progress_cb=progress_cb,
                                        stop_event=stop_event)
    else:
        result = await run_session(client, cfg, progress_cb=progress_cb,
                                   stop_event=stop_event)

    real_key = result.get("key", tmp_key)
    with _runner._lock:
        # Register under real key
        if real_key != tmp_key:
            _runner._sessions[real_key] = session_info
            if tmp_key in _runner._sessions:
                del _runner._sessions[tmp_key]
        session_info["real_key"] = real_key
        session_info["key"]      = real_key
        session_info["status"]   = "done"
        session_info["result"]   = result


def _ts():
    return time.strftime("%H:%M:%S")


def _fmt_progress(p: dict) -> str:
    phase = p.get("phase", "")
    parts = []
    if phase == "clone_topics":
        parts.append(f"Tạo topic {p.get('topics_done',0)}/{p.get('topics_total',0)}: {p.get('current_topic','')}")
    elif phase == "forward":
        parts.append(f"Forward {p.get('total_forwarded',0)} msg")
        if p.get("current_topic"):
            parts.append(f"topic: {p['current_topic']}")
        if p.get("topics_done") is not None:
            parts.append(f"({p['topics_done']}/{p['topics_total']} topics)")
    elif phase == "done":
        parts.append(f"✅ Xong! Tổng: {p.get('total_forwarded',0)} msg, lỗi: {p.get('errors',0)}")
    return " | ".join(parts) if parts else str(p)


# ─── Singleton ────────────────────────────────────────────────────────────────

_runner = ForwarderRunner()


def get_runner() -> ForwarderRunner:
    return _runner
