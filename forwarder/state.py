"""
State persistence layer.
Keeps the original file-based state format (fully compatible with the CLI tool)
AND mirrors everything to the SQLite database for the web UI.

File format (unchanged from CLI tool v2.5.8):
  state_v25_{key}.txt              → last processed message ID (global)
  state_v25_{key}_topic_{tid}.txt  → last processed message ID per topic
  topicmap_v25_{key}.txt           → src_topic_id:dst_topic_id mapping
  session_meta_v25_{key}.json      → session metadata + progress
"""

import os
import json
import time
import datetime
import sqlite3
import re
import logging

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# ─── File-name constants (match CLI v2.5.8) ───────────────────────────────────
STATE_FILE          = "state_v25"
TOPIC_MAP_FILE      = "topicmap_v25"
SESSION_META_PREFIX = "session_meta_v25_"

STATE_DIR = os.getenv("FWD_STATE_DIR", "forwarder_state")
# Always use an absolute path so the directory is found regardless of
# the working directory the process was launched from.
STATE_DIR = os.path.abspath(STATE_DIR)
os.makedirs(STATE_DIR, exist_ok=True)


def _sp(filename: str) -> str:
    """Prefix filename with STATE_DIR."""
    return os.path.join(STATE_DIR, filename)


# ─── Low-level file helpers (identical API to CLI tool) ───────────────────────

def load_last_id(key: str) -> int | None:
    p = _sp(f"{STATE_FILE}_{key}.txt")
    try:
        return int(open(p).read().strip()) if os.path.exists(p) else None
    except ValueError:
        return None


def save_last_id(key: str, mid: int):
    with open(_sp(f"{STATE_FILE}_{key}.txt"), "w") as f:
        f.write(str(mid))


def load_topic_map(key: str) -> dict:
    p = _sp(f"{TOPIC_MAP_FILE}_{key}.txt")
    m: dict = {}
    if os.path.exists(p):
        for line in open(p):
            if ":" in line:
                a, b = line.strip().split(":", 1)
                try:
                    m[int(a)] = int(b)
                except ValueError:
                    pass
    return m


def save_topic_map(key: str, mapping: dict):
    with open(_sp(f"{TOPIC_MAP_FILE}_{key}.txt"), "w") as f:
        for a, b in mapping.items():
            f.write(f"{a}:{b}\n")


# ─── Session metadata (file + DB mirror) ──────────────────────────────────────

def save_session_meta(key, src_name, dst_name, mode, cfg, progress=None):
    path = _sp(f"{SESSION_META_PREFIX}{key}.json")
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    data = {
        "key":          key,
        "src_name":     src_name,
        "dst_name":     dst_name,
        "mode":         mode,
        "cfg":          cfg,
        "created":      existing.get("created", time.time()),
        "last_updated": time.time(),
    }
    if progress:
        data["progress"] = progress
    elif "progress" in existing:
        data["progress"] = existing["progress"]

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"save_session_meta file: {e}")

    # Mirror to DB
    _db_upsert_session(data)


def _db_upsert_session(data: dict):
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fwd_sessions (
                key          TEXT PRIMARY KEY,
                src_name     TEXT,
                dst_name     TEXT,
                mode         TEXT,
                cfg_json     TEXT,
                progress_json TEXT,
                status       TEXT DEFAULT 'idle',
                created_at   REAL,
                updated_at   REAL
            )
        """)
        conn.execute("""
            INSERT INTO fwd_sessions
                (key, src_name, dst_name, mode, cfg_json, progress_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                src_name=excluded.src_name,
                dst_name=excluded.dst_name,
                mode=excluded.mode,
                cfg_json=excluded.cfg_json,
                progress_json=excluded.progress_json,
                updated_at=excluded.updated_at
        """, (
            data["key"],
            data.get("src_name", "?"),
            data.get("dst_name", "?"),
            data.get("mode", "forward"),
            json.dumps(data.get("cfg", {}), ensure_ascii=False),
            json.dumps(data.get("progress", {}), ensure_ascii=False),
            data.get("created", time.time()),
            data.get("last_updated", time.time()),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"_db_upsert_session: {e}")


def db_upsert_session_row(key: str, src_name: str, dst_name: str, mode: str,
                          cfg: dict, status: str = "running", progress: dict | None = None):
    """Tạo hoặc cập nhật row fwd_sessions (dùng khi session bắt đầu)."""
    _db_upsert_session({
        "key": key,
        "src_name": src_name,
        "dst_name": dst_name,
        "mode": mode,
        "cfg": cfg,
        "progress": progress or {},
        "created": time.time(),
        "last_updated": time.time(),
    })
    db_set_session_status(key, status)


def db_set_session_status(key: str, status: str):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE fwd_sessions SET status=?, updated_at=? WHERE key=?",
            (status, time.time(), key),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"db_set_session_status: {e}")


def db_set_session_progress(key: str, progress: dict):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE fwd_sessions SET progress_json=?, updated_at=? WHERE key=?",
            (json.dumps(progress, ensure_ascii=False), time.time(), key),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"db_set_session_progress: {e}")


def _fmt_ts(ts) -> str:
    """Unix timestamp → 'dd/mm/YYYY HH:MM'."""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "—"


def db_list_sessions(limit=100) -> list[dict]:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM fwd_sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["cfg"]          = json.loads(d.get("cfg_json") or "{}")
            d["progress"]     = json.loads(d.get("progress_json") or "{}")
            d["created_str"]  = _fmt_ts(d.get("created_at"))
            d["updated_str"]  = _fmt_ts(d.get("updated_at"))
            result.append(d)
        return result
    except Exception:
        return []


def db_get_session(key: str) -> dict | None:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM fwd_sessions WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        d["cfg"]      = json.loads(d.get("cfg_json") or "{}")
        d["progress"] = json.loads(d.get("progress_json") or "{}")
        return d
    except Exception:
        return None


def db_delete_session(key: str):
    """Remove from DB and delete all state files."""
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("DELETE FROM fwd_sessions WHERE key=?", (key,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"db_delete_session: {e}")

    # Delete state files
    patterns = [
        _sp(f"{SESSION_META_PREFIX}{key}.json"),
        _sp(f"{STATE_FILE}_{key}.txt"),
        _sp(f"{TOPIC_MAP_FILE}_{key}.txt"),
    ]
    prefix = _sp(f"{STATE_FILE}_{key}_topic_")
    try:
        for fname in os.listdir(STATE_DIR):
            full = os.path.join(STATE_DIR, fname)
            if full.startswith(prefix) and full.endswith(".txt"):
                patterns.append(full)
    except Exception:
        pass

    for p in patterns:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            logger.warning(f"delete {p}: {e}")


# ─── Load all sessions (file-first, then DB fill) ─────────────────────────────

def load_all_file_sessions() -> list[dict]:
    sessions: list[dict] = []
    try:
        for fname in os.listdir(STATE_DIR):
            if fname.startswith(SESSION_META_PREFIX) and fname.endswith(".json"):
                try:
                    with open(_sp(fname), "r", encoding="utf-8") as f:
                        sessions.append(json.load(f))
                except Exception:
                    continue
    except Exception:
        pass
    sessions.sort(key=lambda x: x.get("last_updated", 0), reverse=True)
    return sessions


def sync_file_sessions_to_db():
    """Read all JSON meta files and upsert into DB (called at startup)."""
    for s in load_all_file_sessions():
        _db_upsert_session(s)


# ─── Misc helpers ─────────────────────────────────────────────────────────────

def make_key(src_id: int, dst_id: int) -> str:
    return f"{src_id}_{dst_id}"


def format_session_line(s: dict) -> str:
    mode_tag = "📦 BACKUP" if s.get("mode") == "backup" else "📤 FORWARD"
    src  = s.get("src_name", "?")
    dst  = s.get("dst_name", "?")
    last = s.get("last_updated", 0) or s.get("updated_at", 0)
    last_str = datetime.datetime.fromtimestamp(last).strftime("%d/%m %H:%M") if last else "?"
    prog = s.get("progress", {})
    parts = []
    if prog.get("total_forwarded") is not None:
        parts.append(f"✅{prog['total_forwarded']} msg")
    if prog.get("topics_done") is not None and prog.get("topics_total"):
        parts.append(f"{prog['topics_done']}/{prog['topics_total']} topic")
    tail = f"[{last_str}]"
    if parts:
        tail = f"— {' · '.join(parts)} " + tail
    return f"{mode_tag}  {src}  →  {dst}  {tail}"
