"""
Background session runner.
Each forwarder session runs in its own thread with a dedicated asyncio event loop.
"""

import asyncio
import threading
import time
import logging
import os

from .state import (
    db_set_session_status, db_set_session_progress,
    db_upsert_session_row,
    STATE_DIR,
)
from .core import run_session, run_forum_backup, run_relink, resolve_entity, make_key

logger = logging.getLogger(__name__)


class ForwarderRunner:
    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._client_config: tuple | None = None

    def init_client(self, api_id: int, api_hash: str, session_name: str,
                    phone: str | None = None):
        session_path = os.path.join(STATE_DIR, session_name)
        self._client_config = (api_id, api_hash, session_path, phone)
        logger.info(f"Forwarder configured: session={session_path}")
        return self

    def is_configured(self) -> bool:
        return self._client_config is not None

    def _resolve_key(self, key: str) -> str | None:
        with self._lock:
            if key in self._sessions:
                return key
            for k, v in self._sessions.items():
                if v.get("real_key") == key or v.get("tmp_key") == key or v.get("key") == key:
                    return k
        return None

    def is_running(self, key: str) -> bool:
        resolved = self._resolve_key(key)
        if not resolved:
            return False
        with self._lock:
            s = self._sessions.get(resolved)
            return s is not None and s.get("status") in ("starting", "running", "stopping")

    def get_status(self, key: str) -> dict:
        resolved = self._resolve_key(key) or key
        with self._lock:
            return dict(self._sessions.get(resolved, {}))

    def all_statuses(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._sessions.items()}

    def stop_session(self, key: str):
        resolved = self._resolve_key(key)
        if not resolved:
            return
        with self._lock:
            s = self._sessions.get(resolved)
            if s:
                flag = s.get("stop_flag")
                if flag:
                    flag.set()
                s["status"] = "stopping"
                logger.info(f"Stop signal sent to session {resolved}")

    def start_session(self, cfg: dict, mode: str = "forward") -> str:
        if not self._client_config:
            raise RuntimeError(
                "Telethon chưa được cấu hình. "
                "Kiểm tra TELETHON_API_ID, TELETHON_API_HASH và session file."
            )

        tmp_key = f"pending_{int(time.time())}"
        stop_flag = threading.Event()

        with self._lock:
            session_info = {
                "key":        tmp_key,
                "tmp_key":    tmp_key,
                "real_key":   None,
                "cfg":        cfg,
                "mode":       mode,
                "status":     "starting",
                "stop_flag":  stop_flag,
                "progress":   {},
                "started_at": time.time(),
                "log":        [],
                "error":      None,
            }
            self._sessions[tmp_key] = session_info

        client_config = self._client_config

        def progress_cb(p: dict):
            with self._lock:
                db_key = session_info.get("real_key") or tmp_key
                if p.get("phase") != "done":
                    session_info["status"] = "running"
                session_info["progress"] = p
                db_set_session_progress(db_key, p)
                session_info["log"].append(f"[{_ts()}] {_fmt_progress(p)}")
                if len(session_info["log"]) > 500:
                    session_info["log"] = session_info["log"][-400:]

        def run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            final_status = "error"
            try:
                final_status = loop.run_until_complete(
                    _async_run(client_config, cfg, mode, stop_flag,
                               progress_cb, session_info, tmp_key, self)
                )
            except Exception as e:
                logger.error(f"Session {tmp_key} crashed: {e}", exc_info=True)
                with self._lock:
                    session_info["status"] = "error"
                    session_info["error"] = str(e)
                db_key = session_info.get("real_key") or tmp_key
                db_set_session_status(db_key, "error")
            finally:
                loop.close()
                with self._lock:
                    if session_info.get("status") not in ("error",):
                        session_info["status"] = final_status
                db_key = session_info.get("real_key") or tmp_key
                db_set_session_status(db_key, session_info.get("status", final_status))

        t = threading.Thread(target=run_in_thread, daemon=True, name=f"fwd-{tmp_key}")
        t.start()
        with self._lock:
            session_info["thread"] = t

        return tmp_key

    def cleanup_done(self):
        cutoff = time.time() - 3600
        with self._lock:
            to_del = [
                k for k, v in self._sessions.items()
                if v.get("status") in ("done", "error", "stopped")
                and v.get("started_at", 0) < cutoff
            ]
            for k in to_del:
                del self._sessions[k]


async def _register_real_key(client, cfg, mode, session_info, tmp_key, runner):
    try:
        dst_e = await resolve_entity(client, cfg["dst_raw"])
        if mode == "relink":
            real_key = f"relink_{dst_e.id}"
            src_name = str(getattr(dst_e, "title", dst_e.id))
        else:
            src_e = await resolve_entity(client, cfg["src_raw"])
            real_key = make_key(src_e.id, dst_e.id)
            src_name = str(getattr(src_e, "title", src_e.id))
        dst_name = str(getattr(dst_e, "title", dst_e.id))
    except Exception as e:
        logger.warning(f"Early key resolve failed: {e}")
        return tmp_key

    db_upsert_session_row(real_key, src_name, dst_name, mode, cfg, status="running")

    with runner._lock:
        session_info["real_key"] = real_key
        session_info["key"] = real_key
        if real_key != tmp_key:
            runner._sessions[real_key] = session_info
            runner._sessions.pop(tmp_key, None)

    db_set_session_status(real_key, "running")
    return real_key


async def _async_run(client_config, cfg, mode, stop_flag, progress_cb,
                     session_info, tmp_key, runner):
    from telethon import TelegramClient

    api_id, api_hash, session_path, _phone = client_config
    client = TelegramClient(session_path, api_id, api_hash)
    stop_event = asyncio.Event()
    was_stopped = False

    async def _poll_stop():
        while not stop_flag.is_set():
            await asyncio.sleep(0.25)
        stop_event.set()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session không hợp lệ. Chạy lại: python run.py --auth"
            )

        asyncio.create_task(_poll_stop())
        await _register_real_key(client, cfg, mode, session_info, tmp_key, runner)

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

        was_stopped = stop_event.is_set()
        real_key = result.get("key", session_info.get("real_key", tmp_key))

        with runner._lock:
            session_info["real_key"] = real_key
            session_info["key"] = real_key
            session_info["result"] = result
            if not was_stopped:
                session_info["status"] = "done"
                progress_cb({
                    "phase": "done",
                    "total_forwarded": result.get("count", 0),
                    **(result.get("stats") or {}),
                })
                db_set_session_status(real_key, "done")

        return "stopped" if was_stopped else "done"

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
        parts.append(
            f"Tạo topic {p.get('topics_done', 0)}/{p.get('topics_total', 0)}: "
            f"{p.get('current_topic', '')}"
        )
    elif phase == "relink":
        parts.append(
            f"Relink scan={p.get('scanned', 0)} edit={p.get('edited', 0)} "
            f"skip={p.get('skipped', 0)} err={p.get('errors', 0)}"
        )
    elif phase == "forward":
        parts.append(f"Forward {p.get('total_forwarded', 0)} msg")
        if p.get("links_created"):
            parts.append(f"link: {p['links_created']}")
        if p.get("current_topic"):
            parts.append(f"topic: {p.get('current_topic')}")
        if p.get("topics_done") is not None and p.get("topics_total"):
            parts.append(f"({p['topics_done']}/{p['topics_total']} topics)")
    elif phase == "done":
        parts.append(
            f"✅ Xong! {p.get('total_forwarded', 0)} msg, lỗi: {p.get('errors', 0)}"
        )
    return " | ".join(parts) if parts else str(p)


_runner = ForwarderRunner()


def get_runner() -> ForwarderRunner:
    return _runner
