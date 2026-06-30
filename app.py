"""
Flask web server — Forum Converter Bot + Telethon Forwarder.

Routes:
  GET  /                     – Admin dashboard
  GET  /media/<token>        – Serve media (redirect → Telegram CDN)
  ── Admin pages ──
  GET  /admin/links          – Share links manager
  GET  /admin/topics         – Cloned topics overview
  GET  /admin/logs           – Forward logs
  GET  /admin/settings       – Bot settings
  ── Forwarder pages ──
  GET  /forwarder            – Forwarder dashboard (session list)
  GET  /forwarder/new        – New session form
  POST /forwarder/start      – Start new session
  POST /forwarder/stop/<key> – Stop running session
  POST /forwarder/delete/<key> – Delete session
  GET  /forwarder/session/<key> – Session detail + live log
  GET  /forwarder/stream/<key>  – SSE progress stream
  ── REST API ──
  GET  /api/links, /api/topics, /api/logs, /api/stats, /api/settings (POST)
  GET  /api/forwarder/sessions
  GET  /api/forwarder/session/<key>
"""

import asyncio
import json
import logging
import os
import time
import threading

from flask import (
    Flask, render_template, jsonify, request,
    redirect, url_for, abort, send_file, Response, stream_with_context
)
from telegram import Bot

from config.settings import (
    BOT_TOKEN, BASE_URL, SECRET_KEY, WEB_HOST, WEB_PORT, MEDIA_DIR,
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION, TELETHON_PHONE,
)
from database.models import (
    init_db, get_media_link, peek_media_link, list_media_links,
    delete_share_token, get_share_stats, list_all_share_links,
    list_topics, list_forward_logs, all_settings, set_setting, get_conn,
)
from forwarder.state import (
    db_list_sessions, db_get_session, db_delete_session,
    sync_file_sessions_to_db,
)
from forwarder.runner import get_runner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder="web/templates",
    static_folder="web/static"
)
app.secret_key = SECRET_KEY

os.makedirs(MEDIA_DIR, exist_ok=True)

# ─── Telethon runner init ─────────────────────────────────────────────────────

_runner = get_runner()
_telethon_ready = False
_telethon_error = ""


def _init_telethon():
    """
    Validate Telethon config and session file.
    Does NOT create TelegramClient here (would fail without asyncio loop).
    The client is created lazily inside each worker thread when a session starts.
    """
    global _telethon_ready, _telethon_error
    if not TELETHON_API_ID or not TELETHON_API_HASH:
        _telethon_error = "TELETHON_API_ID / TELETHON_API_HASH chưa được set trong .env"
        return

    from forwarder.state import STATE_DIR as FWD_STATE_DIR
    session_path = os.path.join(FWD_STATE_DIR, TELETHON_SESSION)
    session_file = session_path + ".session"

    if not os.path.exists(session_file):
        _telethon_error = (
            f"Session file chưa tồn tại: {session_file}\n"
            "Chạy một lần để xác thực: python run.py --auth"
        )
        return

    # Store config in runner (TelegramClient created in worker thread later)
    _runner.init_client(TELETHON_API_ID, TELETHON_API_HASH,
                        TELETHON_SESSION, TELETHON_PHONE)
    _telethon_ready = True
    logger.info(f"Telethon configured (session: {session_file})")


# ─── Telegram Bot helper (for media serving) ──────────────────────────────────

_bot: Bot | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None


def get_bot() -> Bot | None:
    global _bot, _bot_loop
    if _bot is None and BOT_TOKEN:
        _bot_loop = asyncio.new_event_loop()
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def run_async(coro):
    global _bot_loop
    if _bot_loop is None:
        _bot_loop = asyncio.new_event_loop()
    return _bot_loop.run_until_complete(coro)


# ─── Startup ──────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()
    sync_file_sessions_to_db()
    _init_telethon()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PAGES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    conn = get_conn()
    share = get_share_stats()
    stats = {
        "total_links":    share["total_links"],
        "total_views":    share["total_views"],
        "total_topics":   conn.execute("SELECT COUNT(*) FROM topics WHERE is_active=1").fetchone()[0],
        "total_forwards": conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0],
    }
    try:
        stats["fwd_sessions"] = conn.execute(
            "SELECT COUNT(*) FROM fwd_sessions").fetchone()[0]
    except Exception:
        stats["fwd_sessions"] = 0
    conn.close()
    recent_links  = list_all_share_links(limit=5)
    recent_topics = list_topics(limit=5)
    running_sessions = {k: v for k, v in _runner.all_statuses().items()
                        if v.get("status") == "running"}
    return render_template(
        "dashboard.html",
        stats=stats,
        recent_links=recent_links,
        recent_topics=recent_topics,
        running_sessions=running_sessions,
        base_url=BASE_URL,
        telethon_ready=_telethon_ready,
        telethon_error=_telethon_error,
    )


# ─── Media ────────────────────────────────────────────────────────────────────

@app.route("/d/<token>")
def deep_redirect(token: str):
    """
    Link bền vững: redirect sang bot hiện tại.
    Nếu đổi bot (BOT_USERNAME mới) → mọi link /d/<token> cũ tự trỏ bot mới.
    → Không bao giờ chết dù mất username bot cũ.
    """
    from config.settings import BOT_USERNAME as _bu
    username = (_bu or "").lstrip("@").strip()
    if not username:
        return "BOT_USERNAME chưa cấu hình", 503
    target = f"https://t.me/{username}?start={token}"
    # Trang trung gian: vừa auto-redirect, vừa có nút phòng khi redirect chặn
    html = f"""<!DOCTYPE html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đang mở…</title>
<meta http-equiv="refresh" content="0; url={target}">
<style>body{{font-family:system-ui;background:#0f1117;color:#e2e8f0;
display:flex;flex-direction:column;align-items:center;justify-content:center;
height:100vh;margin:0;gap:20px}}
a.btn{{background:#4f8ef7;color:#fff;padding:14px 28px;border-radius:10px;
text-decoration:none;font-weight:600;font-size:16px}}</style>
</head><body>
<div style="font-size:48px">📦</div>
<div>Đang mở media trong Telegram…</div>
<a class="btn" href="{target}">▶️ Mở ngay</a>
<script>location.href="{target}";</script>
</body></html>"""
    return html


@app.route("/media/<token>")
def serve_media(token: str):
    record = get_media_link(token, increment=True) or peek_media_link(token)
    if not record:
        abort(404)
    bot = get_bot()
    if bot:
        try:
            tg_file = run_async(bot.get_file(record["file_id"]))
            from flask import redirect
            return redirect(tg_file.file_path, code=302)
        except Exception as e:
            logger.error(f"Telegram get_file error: {e}")
    local_path = os.path.join(MEDIA_DIR, f"{token}.bin")
    if os.path.exists(local_path):
        return send_file(local_path,
                         mimetype=record.get("mime_type", "application/octet-stream"),
                         as_attachment=True,
                         download_name=record.get("file_name") or token)
    abort(503)


@app.route("/media/<token>/info")
def media_info(token: str):
    record = get_media_link(token)
    if not record:
        abort(404)
    return jsonify(record)


# ─── Admin Pages ──────────────────────────────────────────────────────────────

@app.route("/admin/links")
def page_links():
    links = list_all_share_links(limit=100)
    return render_template("links.html", links=links, base_url=BASE_URL)


@app.route("/admin/topics")
def page_topics():
    topics = list_topics(limit=100)
    return render_template("topics.html", topics=topics)


@app.route("/admin/logs")
def page_logs():
    logs = list_forward_logs(limit=200)
    return render_template("logs.html", logs=logs)


@app.route("/admin/settings", methods=["GET", "POST"])
def page_settings():
    if request.method == "POST":
        for k, v in request.form.items():
            if k.startswith("setting_"):
                set_setting(k[len("setting_"):], v)
        return redirect(url_for("page_settings"))
    return render_template("settings.html", settings=all_settings())


# ─── Quản lý file .env qua web ─────────────────────────────────────────────────

@app.route("/admin/env", methods=["GET", "POST"])
def page_env():
    from utils.envfile import read_env, write_env, mask
    if request.method == "POST":
        updates = {}
        for k, v in request.form.items():
            if k.startswith("env_"):
                key = k[len("env_"):]
                # Bỏ qua trường nhạy cảm nếu giữ nguyên mask (không sửa)
                if v == "__KEEP__":
                    continue
                updates[key] = v
        write_env(updates)
        # Reload config trong process
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
            import importlib, config.settings
            importlib.reload(config.settings)
        except Exception as e:
            logger.warning(f"reload config: {e}")
        return redirect(url_for("page_env", saved="1"))

    lines = read_env()
    return render_template("env.html", lines=lines, mask=mask,
                           saved=request.args.get("saved"))


# ─── Đăng nhập Telethon qua web ────────────────────────────────────────────────

@app.route("/admin/login")
def page_login():
    return render_template("login.html", ready=_telethon_ready)


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    from forwarder.web_auth import get_web_auth
    phone = (request.json or {}).get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Thiếu số điện thoại"})
    return jsonify(get_web_auth().start_login(phone))


@app.route("/api/login/code", methods=["POST"])
def api_login_code():
    from forwarder.web_auth import get_web_auth
    code = (request.json or {}).get("code", "").strip()
    result = get_web_auth().submit_code(code)
    if result.get("done"):
        _init_telethon()   # nạp session mới
    return jsonify(result)


@app.route("/api/login/password", methods=["POST"])
def api_login_password():
    from forwarder.web_auth import get_web_auth
    pw = (request.json or {}).get("password", "")
    result = get_web_auth().submit_password(pw)
    if result.get("done"):
        _init_telethon()
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════════
# FORWARDER PAGES
# ══════════════════════════════════════════════════════════════════════════════

_ACTIVE_FWD = frozenset({"starting", "running", "stopping"})


def _find_live_session(key: str) -> tuple[str | None, dict | None]:
    """Resolve pending/tmp/real key → (memory key, live session dict)."""
    live = _runner.all_statuses()
    if key in live and live[key].get("status"):
        return key, live[key]
    for k, v in live.items():
        if not v.get("status"):
            continue
        if v.get("real_key") == key or v.get("tmp_key") == key or v.get("key") == key:
            return k, v
    resolved = _runner._resolve_key(key)
    if resolved and resolved in live and live[resolved].get("status"):
        return resolved, live[resolved]
    st = _runner.get_status(key)
    if st.get("status"):
        return resolved or key, st
    return None, None


def _merge_fwd_sessions() -> list[dict]:
    """Gộp DB + phiên đang chạy trong RAM (kể cả pending_*)."""
    sessions = db_list_sessions(limit=100)
    live = _runner.all_statuses()
    matched: set[str] = set()

    for s in sessions:
        _canon, lv = _find_live_session(s["key"])
        if lv:
            s["live"] = lv
            s["live_key"] = _canon or s["key"]
            matched.update(filter(None, (_canon, lv.get("real_key"), lv.get("tmp_key"), s["key"])))
            s["progress"] = lv.get("progress") or s.get("progress") or {}
        else:
            s["live"] = None
            s["live_key"] = s["key"]

    for k, v in live.items():
        if k in matched or not v.get("status"):
            continue
        rk = v.get("real_key")
        if rk and any(row["key"] == rk for row in sessions):
            continue
        cfg = v.get("cfg") or {}
        sessions.insert(0, {
            "key": rk or k,
            "live_key": k,
            "src_name": cfg.get("src_raw", "…"),
            "dst_name": cfg.get("dst_raw", "…"),
            "mode": v.get("mode", "forward"),
            "status": v.get("status", "starting"),
            "progress": v.get("progress") or {},
            "cfg": cfg,
            "live": v,
            "created_str": "—",
            "updated_str": "Đang chạy",
        })
        matched.add(k)

    return sessions


@app.route("/forwarder")
def forwarder_dashboard():
    return render_template(
        "forwarder_dashboard.html",
        sessions=_merge_fwd_sessions(),
        telethon_ready=_telethon_ready,
        telethon_error=_telethon_error,
    )


@app.route("/forwarder/new")
def forwarder_new():
    if not _telethon_ready:
        return render_template("forwarder_setup.html",
                               telethon_error=_telethon_error)
    return render_template("forwarder_new.html")


@app.route("/forwarder/start", methods=["POST"])
def forwarder_start():
    if not _telethon_ready:
        return jsonify({"ok": False, "error": "Telethon chưa sẵn sàng. " + _telethon_error}), 400

    from forwarder.core import parse_link

    f = request.form
    mode = f.get("mode", "forward")

    src_in = f.get("src_raw", "").strip()
    dst_in = f.get("dst_raw", "").strip()

    # Relink chỉ cần forum đích; các mode khác cần cả nguồn lẫn đích
    if mode == "relink":
        if not dst_in:
            return jsonify({"ok": False, "error": "Thiếu forum đích cần sửa link"}), 400
        src_in = src_in or dst_in   # placeholder
    elif not src_in or not dst_in:
        return jsonify({"ok": False, "error": "Thiếu kênh nguồn hoặc đích"}), 400

    src_parsed = parse_link(src_in)
    dst_parsed = parse_link(dst_in)

    cfg: dict = {
        "src_raw":     src_parsed["channel_raw"],   # đã sạch (chỉ ID/username)
        "dst_raw":     dst_parsed["channel_raw"],
        "only_filter": f.get("only_filter", "all"),
        "hide_sender": f.get("hide_sender") == "on",
        "webhook_url": f.get("webhook_url", "").strip(),
    }

    # Link mode (shared between both modes)
    cfg["link_mode"]        = f.get("link_mode") == "on"
    cfg["caption_template"] = f.get("caption_template", "").strip() or None

    if mode == "relink":
        # Sửa link bot cũ → mới trong forum đích (dst_raw)
        cfg["old_bot"]       = f.get("old_bot", "").strip()
        cfg["new_link_base"] = f.get("new_link_base", "auto").strip() or "auto"
    elif mode == "backup":
        cfg["icon_mode"]    = f.get("icon_mode", "clone")
        cfg["emoji_raw"]    = f.get("emoji_raw", "").strip()
        cfg["skip_general"] = f.get("skip_general") == "on"
        cfg["clone_pins"]   = f.get("clone_pins") == "on"
        # Nếu link nguồn trỏ tới 1 topic cụ thể → chỉ clone topic đó,
        # bắt đầu từ message trong link (nếu có).
        cfg["only_topic_id"]   = src_parsed.get("topic_id")
        cfg["backup_start_id"] = src_parsed.get("message_id")
    else:
        # Forward mode: lấy start_msg_id / topic từ link nguồn nếu có,
        # ưu tiên giá trị nhập tay trong form
        form_start = int(f.get("start_msg_id") or 0) or None
        form_topic = int(f.get("force_topic_id") or 0) or None
        cfg["start_msg_id"]   = form_start or src_parsed.get("message_id")
        cfg["force_topic_id"] = form_topic or src_parsed.get("topic_id")
        cfg["auto_topic"]     = f.get("auto_topic") == "on"

    try:
        tmp_key = _runner.start_session(cfg, mode=mode)
        return redirect(url_for("forwarder_session", key=tmp_key))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/forwarder/session/<key>")
def forwarder_session(key: str):
    _canon, live = _find_live_session(key)
    if live and live.get("real_key") and key.startswith("pending_") and key != live["real_key"]:
        return redirect(url_for("forwarder_session", key=live["real_key"]))
    page_key = (live.get("real_key") if live else None) or _canon or key
    db_s = db_get_session(page_key) or db_get_session(key)
    return render_template(
        "forwarder_session.html",
        key=page_key,
        live=live or {},
        db_session=db_s,
    )


@app.route("/forwarder/stop/<key>", methods=["POST"])
def forwarder_stop(key: str):
    _runner.stop_session(key)
    return jsonify({"ok": True, "key": key, "status": "stopping"})


@app.route("/forwarder/resume/<key>", methods=["POST"])
def forwarder_resume(key: str):
    """
    Tiếp tục phiên đã dừng.
    Đọc cfg + mode từ DB → start_session lại.
    State file (keyed by src_dst) còn nguyên → tự resume từ chỗ dừng.
    """
    if not _telethon_ready:
        return jsonify({"ok": False, "error": "Telethon chưa sẵn sàng"}), 400

    if _runner.is_running(key):
        return jsonify({"ok": False, "error": "Phiên đang chạy"}), 400

    db_s = db_get_session(key)
    if not db_s:
        return jsonify({"ok": False, "error": "Không tìm thấy phiên"}), 404

    cfg  = db_s.get("cfg", {})
    mode = db_s.get("mode", "forward")
    if not cfg.get("src_raw") or not cfg.get("dst_raw"):
        return jsonify({"ok": False, "error": "Cấu hình phiên không hợp lệ"}), 400

    try:
        tmp_key = _runner.start_session(cfg, mode=mode)
        return redirect(url_for("forwarder_session", key=tmp_key))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/forwarder/delete/<key>", methods=["POST"])
def forwarder_delete(key: str):
    _runner.stop_session(key)
    db_delete_session(key)
    return redirect(url_for("forwarder_dashboard"))


# ─── SSE stream ───────────────────────────────────────────────────────────────

@app.route("/forwarder/stream/<key>")
def forwarder_stream(key: str):
    """Server-Sent Events endpoint for real-time progress updates."""
    def generate():
        last_log_idx = 0
        for _ in range(600):   # max 10 min at 1s interval
            _canon, status = _find_live_session(key)

            if status and status.get("status"):
                log = status.get("log", [])
                new_lines = log[last_log_idx:]
                last_log_idx = len(log)
                payload = {
                    "status":   status.get("status"),
                    "progress": status.get("progress", {}),
                    "log":      new_lines,
                    "error":    status.get("error"),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if status.get("status") in ("done", "error", "stopped"):
                    yield "data: {\"status\":\"closed\"}\n\n"
                    return
            else:
                yield f"data: {{\"status\":\"not_found\"}}\n\n"
                return
            time.sleep(1)
        yield "data: {\"status\":\"timeout\"}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# REST API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/links")
def api_links():
    limit  = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    links  = list_all_share_links(limit=limit, offset=offset)
    return jsonify({"ok": True, "data": links, "count": len(links)})


@app.route("/api/links/<token>", methods=["DELETE"])
def api_delete_link(token):
    delete_share_token(token)
    return jsonify({"ok": True, "token": token})


@app.route("/api/topics")
def api_topics():
    return jsonify({"ok": True, "data": list_topics(limit=100)})


@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 100))
    return jsonify({"ok": True, "data": list_forward_logs(limit=limit)})


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    share = get_share_stats()
    data = {
        "total_links":    share["total_links"],
        "total_views":    share["total_views"],
        "total_albums":   share["total_albums"],
        "total_topics":   conn.execute("SELECT COUNT(*) FROM topics WHERE is_active=1").fetchone()[0],
        "total_forwards": conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0],
        "by_type":        {},
    }
    try:
        data["fwd_sessions"] = conn.execute("SELECT COUNT(*) FROM fwd_sessions").fetchone()[0]
    except Exception:
        data["fwd_sessions"] = 0
    rows = conn.execute(
        "SELECT file_type, COUNT(*) as cnt FROM media_links WHERE is_active=1 GROUP BY file_type"
    ).fetchall()
    conn.close()
    data["by_type"] = {r["file_type"]: r["cnt"] for r in rows}
    if share["total_albums"]:
        data["by_type"]["album"] = share["total_albums"]
    return jsonify({"ok": True, "data": data})


@app.route("/api/backup", methods=["GET", "POST"])
def api_backup_now():
    """GET: trạng thái backup. POST: chạy backup thủ công."""
    from utils.backup import run_backup_now, get_backup_status
    if request.method == "GET":
        return jsonify({"ok": True, "status": get_backup_status()})
    send_tg = (request.json or {}).get("telegram") if request.is_json else None
    result = run_backup_now(send_telegram=send_tg)
    return jsonify(result), (200 if result["ok"] else 500)


@app.route("/api/settings", methods=["POST"])
def api_settings():
    payload = request.get_json(force=True)
    for k, v in payload.items():
        set_setting(k, str(v))
    return jsonify({"ok": True})


@app.route("/api/forwarder/sessions")
def api_fwd_sessions():
    sessions = _merge_fwd_sessions()
    for s in sessions:
        lv = s.get("live") or {}
        s["is_running"] = bool(lv.get("status") in _ACTIVE_FWD) or _runner.is_running(s["key"])
    return jsonify({"ok": True, "data": sessions})


@app.route("/api/forwarder/session/<key>")
def api_fwd_session(key: str):
    _canon, live = _find_live_session(key)
    page_key = (live.get("real_key") if live else None) or _canon or key
    db_s = db_get_session(page_key) or db_get_session(key) or {}
    return jsonify({"ok": True, "live": live or {}, "db": db_s, "key": page_key})


# ─── Error handlers ───────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/health")
def health():
    """
    Health check — shows which components are running.
    Useful to diagnose 'bot không phản ứng' issues.
    """
    from config.settings import BOT_TOKEN, BOT_USERNAME, TELETHON_API_ID
    from forwarder.state import STATE_DIR
    from utils.backup import get_backup_status
    import os

    bot_token_ok  = bool(BOT_TOKEN)
    bot_user_ok   = bool(BOT_USERNAME)
    telethon_ok   = bool(TELETHON_API_ID)
    session_file  = os.path.join(STATE_DIR, os.getenv("TELETHON_SESSION","session_main") + ".session")
    session_ok    = os.path.exists(session_file)

    conn = get_conn()
    albums = conn.execute("SELECT COUNT(*) FROM media_albums WHERE is_active=1").fetchone()[0]
    links  = conn.execute("SELECT COUNT(*) FROM media_links  WHERE is_active=1").fetchone()[0]
    conn.close()

    backup = get_backup_status()

    return jsonify({
        "status": "ok",
        "components": {
            "web_server":      True,
            "bot_token_set":   bot_token_ok,
            "bot_username_set": bot_user_ok,
            "telethon_api_set": telethon_ok,
            "telethon_session": session_ok,
            "telethon_ready":  _telethon_ready,
        },
        "database": {
            "albums_stored": albums,
            "links_stored":  links,
        },
        "backup": backup,
        "warnings": [
            w for w in [
                None if bot_token_ok   else "BOT_TOKEN not set — bot cannot start",
                None if bot_user_ok    else "BOT_USERNAME not set — deep links will be broken",
                None if telethon_ok    else "TELETHON_API_ID not set — forwarder disabled",
                None if session_ok     else f"Session file missing: run 'python run.py --auth'",
                None if _telethon_ready else _telethon_error or "Telethon not initialized",
            ] if w
        ] + backup.get("skip_reasons", []),
        "note": (
            "Bot Telegram phải đang CHẠY để xử lý deep link /start TOKEN. "
            "Dùng 'python run.py' (không phải --web) để chạy cả bot lẫn web."
        ),
    })


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(503)
def unavailable(e):
    return render_template("503.html"), 503


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
