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
            file_ids     TEXT,               -- JSON [{type,file_id}] cho media nhận trực tiếp qua bot
            expires_at   REAL,               -- timestamp hết hạn (NULL = vĩnh viễn)
            max_views    INTEGER DEFAULT 0,  -- giới hạn lượt xem (0 = không giới hạn)
            allow_forward INTEGER DEFAULT 1, -- 1 = cho forward, 0 = chặn (protect_content)
            created_at   DATETIME DEFAULT (datetime('now')),
            access_count INTEGER  DEFAULT 0,
            is_active    INTEGER  DEFAULT 1
        );

        -- Người dùng bot (để gửi thông báo / broadcast)
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id    INTEGER PRIMARY KEY,
            name       TEXT,
            username   TEXT,
            first_seen DATETIME DEFAULT (datetime('now')),
            last_seen  DATETIME DEFAULT (datetime('now')),
            is_blocked INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_media_token  ON media_links(token);
        CREATE INDEX IF NOT EXISTS idx_album_token  ON media_albums(token);
        CREATE INDEX IF NOT EXISTS idx_fwd_source   ON forward_log(source_chat_id, source_msg_id);
    """)

    # Migration: thêm cột mới nếu DB cũ chưa có
    cols = [r[1] for r in cur.execute("PRAGMA table_info(media_albums)").fetchall()]
    for col, ddl in [
        ("file_ids",      "ALTER TABLE media_albums ADD COLUMN file_ids TEXT"),
        ("expires_at",    "ALTER TABLE media_albums ADD COLUMN expires_at REAL"),
        ("max_views",     "ALTER TABLE media_albums ADD COLUMN max_views INTEGER DEFAULT 0"),
        ("allow_forward", "ALTER TABLE media_albums ADD COLUMN allow_forward INTEGER DEFAULT 1"),
    ]:
        if col not in cols:
            try:
                cur.execute(ddl)
            except Exception:
                pass

    try:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_src "
            "ON topics(source_chat_id, source_topic_id)"
        )
    except Exception:
        pass

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


def get_media_link(token: str, increment: bool = True) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM media_links WHERE token=? AND is_active=1", (token,)
    ).fetchone()
    if row and increment:
        conn.execute(
            "UPDATE media_links SET access_count=access_count+1 WHERE token=?", (token,)
        )
        conn.commit()
    conn.close()
    return dict(row) if row else None


def peek_media_link(token: str) -> dict | None:
    return get_media_link(token, increment=False)


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
           ON CONFLICT(source_chat_id, source_topic_id) DO UPDATE SET
                dest_chat_id=excluded.dest_chat_id,
                dest_topic_id=excluded.dest_topic_id,
                topic_name=excluded.topic_name,
                is_active=1""",
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


def create_album_from_file_ids(token: str, file_ids: list, caption: str = "") -> dict:
    """
    Tạo album record từ danh sách file_id (media nhận trực tiếp qua bot).
    file_ids: list[{"type": "photo"/"video"/..., "file_id": "..."}]
    """
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO media_albums
           (token, src_chat_id, src_msg_ids, caption, file_ids)
           VALUES (?,?,?,?,?)""",
        (token, 0, _json.dumps([]), caption or "", _json.dumps(file_ids))
    )
    conn.commit()
    row = conn.execute("SELECT * FROM media_albums WHERE token=?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _album_row_to_dict(row) -> dict:
    d = dict(row)
    d["src_msg_ids"] = _json.loads(d["src_msg_ids"])
    try:
        d["file_ids"] = _json.loads(d.get("file_ids") or "[]")
    except Exception:
        d["file_ids"] = []
    return d


def peek_media_album(token: str) -> dict | None:
    """Đọc album không tăng access_count."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM media_albums WHERE token=? AND is_active=1", (token,)
    ).fetchone()
    conn.close()
    return _album_row_to_dict(row) if row else None


def increment_album_access(token: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE media_albums SET access_count=access_count+1 WHERE token=?", (token,)
    )
    conn.commit()
    conn.close()


def get_media_album(token: str) -> dict | None:
    """Fetch album by token; increments access_count (legacy API)."""
    d = peek_media_album(token)
    if d:
        increment_album_access(token)
        d["access_count"] = d.get("access_count", 0) + 1
    return d


def validate_album_access(album: dict) -> tuple[bool, str]:
    """Kiểm tra hết hạn / max_views (trước khi serve, chưa tăng count)."""
    import time
    exp = album.get("expires_at")
    if exp and time.time() > exp:
        return False, "expired"
    mv = album.get("max_views") or 0
    if mv > 0 and album.get("access_count", 0) >= mv:
        return False, "max_views"
    return True, ""


def resolve_share_record(token: str) -> dict | None:
    """Tìm token trong media_albums hoặc media_links (không tăng view)."""
    album = peek_media_album(token)
    if album:
        album["_source"] = "album"
        return album
    link = peek_media_link(token)
    if link:
        link["_source"] = "link"
        return link
    return None


def delete_share_token(token: str) -> bool:
    had = bool(peek_media_album(token) or peek_media_link(token))
    if peek_media_album(token):
        delete_media_album(token)
    if peek_media_link(token):
        delete_media_link(token)
    return had


def get_share_stats() -> dict:
    conn = get_conn()
    albums = conn.execute(
        "SELECT COUNT(*) FROM media_albums WHERE is_active=1"
    ).fetchone()[0]
    links = conn.execute(
        "SELECT COUNT(*) FROM media_links WHERE is_active=1"
    ).fetchone()[0]
    views = conn.execute(
        "SELECT COALESCE(SUM(access_count),0) FROM media_albums WHERE is_active=1"
    ).fetchone()[0]
    views += conn.execute(
        "SELECT COALESCE(SUM(access_count),0) FROM media_links WHERE is_active=1"
    ).fetchone()[0]
    conn.close()
    return {
        "total_links": albums + links,
        "total_albums": albums,
        "total_legacy_links": links,
        "total_views": views,
    }


def list_all_share_links(limit: int = 100, offset: int = 0) -> list[dict]:
    """Gộp album + legacy link cho web admin."""
    albums = list_media_albums(limit=limit, offset=offset)
    for a in albums:
        a["share_type"] = "album"
        a["file_type"] = "album"
        a["file_name"] = None
        a["uploader_name"] = None
    if len(albums) >= limit:
        return albums
    rest = limit - len(albums)
    links = list_media_links(limit=rest, offset=0)
    for l in links:
        l["share_type"] = "link"
    return albums + links


def update_album_settings(token: str, expires_at=None, max_views=None,
                          allow_forward=None) -> None:
    """Cập nhật cài đặt link (chỉ field nào truyền vào, None = giữ nguyên)."""
    sets, vals = [], []
    if expires_at is not None:
        sets.append("expires_at=?");    vals.append(expires_at if expires_at else None)
    if max_views is not None:
        sets.append("max_views=?");     vals.append(int(max_views))
    if allow_forward is not None:
        sets.append("allow_forward=?"); vals.append(1 if allow_forward else 0)
    if not sets:
        return
    vals.append(token)
    conn = get_conn()
    conn.execute(f"UPDATE media_albums SET {','.join(sets)} WHERE token=?", vals)
    conn.commit()
    conn.close()


def get_album_settings(token: str) -> dict | None:
    """Lấy nhanh settings của album (không tăng access_count)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT expires_at, max_views, allow_forward, access_count "
        "FROM media_albums WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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


# ─── Bot users (broadcast) ────────────────────────────────────────────────────

def record_user(user_id: int, name: str = "", username: str = ""):
    conn = get_conn()
    conn.execute(
        """INSERT INTO bot_users (user_id, name, username, last_seen)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               name=excluded.name, username=excluded.username,
               last_seen=datetime('now'), is_blocked=0""",
        (user_id, name, username)
    )
    conn.commit()
    conn.close()


def list_user_ids(only_active: bool = True) -> list[int]:
    conn = get_conn()
    q = "SELECT user_id FROM bot_users"
    if only_active:
        q += " WHERE is_blocked=0"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def count_users() -> int:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
    conn.close()
    return n


def mark_user_blocked(user_id: int):
    conn = get_conn()
    conn.execute("UPDATE bot_users SET is_blocked=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
