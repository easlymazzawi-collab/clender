"""
SQLite database models using raw sqlite3.
Tables:
  - media_links   : maps unique token → Telegram file_id + metadata (web links)
  - media_albums  : maps unique token → album of (src_chat_id + msg_ids) for bot deep links
  - topics        : cloned topic mapping between forums
  - forward_log   : log every forwarded/cloned message
  - settings      : key/value admin settings
"""

import sqlite3
import os
from config.settings import DB_PATH


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS media_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT    UNIQUE NOT NULL,
            file_id     TEXT    NOT NULL,
            file_type   TEXT    NOT NULL,   -- photo/video/document/audio/voice
            file_name   TEXT,
            mime_type   TEXT,
            thumb_file_id TEXT,             -- thumbnail for video/document
            caption     TEXT,
            uploader_id INTEGER,
            uploader_name TEXT,
            created_at  DATETIME DEFAULT (datetime('now')),
            access_count INTEGER DEFAULT 0,
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS topics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id  INTEGER NOT NULL,
            source_topic_id INTEGER,
            dest_chat_id    INTEGER NOT NULL,
            dest_topic_id   INTEGER,
            topic_name      TEXT,
            cloned_at       DATETIME DEFAULT (datetime('now')),
            last_synced_at  DATETIME,
            msg_count       INTEGER DEFAULT 0,
            is_active       INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS forward_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id  INTEGER,
            source_msg_id   INTEGER,
            dest_chat_id    INTEGER,
            dest_msg_id     INTEGER,
            source_topic_id INTEGER,
            dest_topic_id   INTEGER,
            media_token     TEXT,
            forward_mode    TEXT DEFAULT 'named',  -- named / anonymous
            status          TEXT DEFAULT 'ok',
            created_at      DATETIME DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key     TEXT PRIMARY KEY,
            value   TEXT,
            updated_at DATETIME DEFAULT (datetime('now'))
        );

        -- Bot deep-link albums: token → original source chat + message IDs
        -- When user clicks t.me/bot?start=TOKEN, bot copies these messages to user.
        CREATE TABLE IF NOT EXISTS media_albums (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token        TEXT    UNIQUE NOT NULL,
            src_chat_id  INTEGER NOT NULL,
            src_msg_ids  TEXT    NOT NULL,   -- JSON array of message IDs e.g. [101,102,103]
            caption      TEXT,               -- original caption of the album
            thumb_file_id TEXT,              -- file_id of thumbnail for the destination post
            created_at   DATETIME DEFAULT (datetime('now')),
            access_count INTEGER  DEFAULT 0,
            is_active    INTEGER  DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_media_token  ON media_links(token);
        CREATE INDEX IF NOT EXISTS idx_album_token  ON media_albums(token);
        CREATE INDEX IF NOT EXISTS idx_fwd_source   ON forward_log(source_chat_id, source_msg_id);
    """)

    conn.commit()
    conn.close()


# ─── Media Link helpers ───────────────────────────────────────────────────────

def create_media_link(token, file_id, file_type, file_name=None,
                      mime_type=None, thumb_file_id=None, caption=None,
                      uploader_id=None, uploader_name=None) -> dict:
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO media_links
           (token, file_id, file_type, file_name, mime_type,
            thumb_file_id, caption, uploader_id, uploader_name)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (token, file_id, file_type, file_name, mime_type,
         thumb_file_id, caption, uploader_id, uploader_name)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM media_links WHERE token=?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_media_link(token: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM media_links WHERE token=? AND is_active=1", (token,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE media_links SET access_count=access_count+1 WHERE token=?", (token,)
        )
        conn.commit()
    conn.close()
    return dict(row) if row else None


def list_media_links(limit=50, offset=0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM media_links ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_media_link(token: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE media_links SET is_active=0 WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return True


# ─── Topic helpers ────────────────────────────────────────────────────────────

def upsert_topic(source_chat_id, source_topic_id, dest_chat_id,
                 dest_topic_id, topic_name) -> dict:
    conn = get_conn()
    conn.execute(
        """INSERT INTO topics
               (source_chat_id, source_topic_id, dest_chat_id,
                dest_topic_id, topic_name)
           VALUES (?,?,?,?,?)
           ON CONFLICT DO NOTHING""",
        (source_chat_id, source_topic_id, dest_chat_id, dest_topic_id, topic_name)
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM topics WHERE source_chat_id=? AND source_topic_id=?",
        (source_chat_id, source_topic_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def list_topics(limit=100, offset=0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM topics ORDER BY cloned_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def increment_topic_msg_count(source_chat_id, source_topic_id):
    conn = get_conn()
    conn.execute(
        """UPDATE topics SET msg_count=msg_count+1,
               last_synced_at=datetime('now')
           WHERE source_chat_id=? AND source_topic_id=?""",
        (source_chat_id, source_topic_id)
    )
    conn.commit()
    conn.close()


# ─── Forward log helpers ──────────────────────────────────────────────────────

def log_forward(source_chat_id, source_msg_id, dest_chat_id, dest_msg_id,
                source_topic_id=None, dest_topic_id=None,
                media_token=None, forward_mode="named", status="ok"):
    conn = get_conn()
    conn.execute(
        """INSERT INTO forward_log
           (source_chat_id, source_msg_id, dest_chat_id, dest_msg_id,
            source_topic_id, dest_topic_id, media_token, forward_mode, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_chat_id, source_msg_id, dest_chat_id, dest_msg_id,
         source_topic_id, dest_topic_id, media_token, forward_mode, status)
    )
    conn.commit()
    conn.close()


def list_forward_logs(limit=100, offset=0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM forward_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Settings helpers ─────────────────────────────────────────────────────────

def get_setting(key: str, default=None) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute(
        """INSERT INTO bot_settings (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=datetime('now')""",
        (key, value)
    )
    conn.commit()
    conn.close()


def all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


# ─── Media Album helpers (bot deep-link) ─────────────────────────────────────
import json as _json


def create_media_album(token: str, src_chat_id: int, src_msg_ids: list,
                       caption: str = "", thumb_file_id: str = None) -> dict:
    """
    Store an album record for bot deep-link serving.
    src_msg_ids: list of Telegram message IDs in the album (ints).
    Returns the created record as dict.
    """
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO media_albums
           (token, src_chat_id, src_msg_ids, caption, thumb_file_id)
           VALUES (?,?,?,?,?)""",
        (token, src_chat_id, _json.dumps(src_msg_ids), caption or "", thumb_file_id)
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM media_albums WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return {}
    d = dict(row)
    d["src_msg_ids"] = _json.loads(d["src_msg_ids"])
    return d


def get_media_album(token: str) -> dict | None:
    """Fetch album by token; increments access_count."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM media_albums WHERE token=? AND is_active=1", (token,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE media_albums SET access_count=access_count+1 WHERE token=?",
            (token,)
        )
        conn.commit()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["src_msg_ids"] = _json.loads(d["src_msg_ids"])
    return d


def list_media_albums(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM media_albums ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["src_msg_ids"] = _json.loads(d["src_msg_ids"])
        result.append(d)
    return result


def delete_media_album(token: str):
    conn = get_conn()
    conn.execute("UPDATE media_albums SET is_active=0 WHERE token=?", (token,))
    conn.commit()
    conn.close()
