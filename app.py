"""
Flask web server.

Routes:
  GET  /               – Admin dashboard
  GET  /media/<token>  – Serve media (redirects to Telegram CDN or sends file)
  GET  /api/links      – JSON list of share links
  GET  /api/topics     – JSON list of cloned topics
  GET  /api/logs       – JSON list of forward logs
  GET  /api/stats      – JSON stats summary
  POST /api/settings   – Update settings
  DELETE /api/links/<token> – Deactivate link
"""

import asyncio
import logging
import os
import threading

from flask import (
    Flask, render_template, jsonify, request,
    redirect, url_for, abort, send_file
)
from telegram import Bot

from config.settings import (
    BOT_TOKEN, BASE_URL, SECRET_KEY, WEB_HOST, WEB_PORT, MEDIA_DIR
)
from database.models import (
    init_db, get_media_link, list_media_links, delete_media_link,
    list_topics, list_forward_logs, all_settings, set_setting,
    get_conn
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder="web/templates",
    static_folder="web/static"
)
app.secret_key = SECRET_KEY

os.makedirs(MEDIA_DIR, exist_ok=True)

# ─── Telegram bot helper ──────────────────────────────────────────────────────

_bot: Bot | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None


def get_bot() -> Bot | None:
    global _bot, _bot_loop
    if _bot is None and BOT_TOKEN:
        _bot_loop = asyncio.new_event_loop()
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def run_async(coro):
    """Run an async coroutine from a sync Flask context."""
    global _bot_loop
    if _bot_loop is None:
        _bot_loop = asyncio.new_event_loop()
    return _bot_loop.run_until_complete(coro)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    conn = get_conn()
    stats = {
        "total_links": conn.execute(
            "SELECT COUNT(*) FROM media_links WHERE is_active=1").fetchone()[0],
        "total_views": conn.execute(
            "SELECT COALESCE(SUM(access_count),0) FROM media_links").fetchone()[0],
        "total_topics": conn.execute(
            "SELECT COUNT(*) FROM topics WHERE is_active=1").fetchone()[0],
        "total_forwards": conn.execute(
            "SELECT COUNT(*) FROM forward_log").fetchone()[0],
    }
    conn.close()
    recent_links = list_media_links(limit=5)
    recent_topics = list_topics(limit=5)
    return render_template(
        "dashboard.html",
        stats=stats,
        recent_links=recent_links,
        recent_topics=recent_topics,
        base_url=BASE_URL
    )


@app.route("/media/<token>")
def serve_media(token: str):
    """
    Main share-link endpoint.
    Tries to get a direct file URL from Telegram CDN, then redirects.
    Falls back to a download proxy.
    """
    record = get_media_link(token)
    if not record:
        abort(404)

    bot = get_bot()
    if bot:
        try:
            tg_file = run_async(bot.get_file(record["file_id"]))
            return redirect(tg_file.file_path, code=302)
        except Exception as e:
            logger.error(f"Telegram get_file error: {e}")

    # Fallback: stream from local media dir
    local_path = os.path.join(MEDIA_DIR, f"{token}.bin")
    if os.path.exists(local_path):
        return send_file(local_path, mimetype=record.get("mime_type", "application/octet-stream"),
                         as_attachment=True,
                         download_name=record.get("file_name") or token)

    abort(503)


@app.route("/media/<token>/info")
def media_info(token: str):
    """JSON info page for a share link."""
    record = get_media_link(token)
    if not record:
        abort(404)
    return jsonify(record)


# ─── Admin Pages ──────────────────────────────────────────────────────────────

@app.route("/admin/links")
def page_links():
    links = list_media_links(limit=100)
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
                key = k[len("setting_"):]
                set_setting(key, v)
        return redirect(url_for("page_settings"))
    settings = all_settings()
    return render_template("settings.html", settings=settings)


# ─── REST API ─────────────────────────────────────────────────────────────────

@app.route("/api/links")
def api_links():
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    links = list_media_links(limit=limit, offset=offset)
    return jsonify({"ok": True, "data": links, "count": len(links)})


@app.route("/api/links/<token>", methods=["DELETE"])
def api_delete_link(token):
    delete_media_link(token)
    return jsonify({"ok": True, "token": token})


@app.route("/api/topics")
def api_topics():
    topics = list_topics(limit=100)
    return jsonify({"ok": True, "data": topics})


@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 100))
    logs = list_forward_logs(limit=limit)
    return jsonify({"ok": True, "data": logs})


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    data = {
        "total_links": conn.execute(
            "SELECT COUNT(*) FROM media_links WHERE is_active=1").fetchone()[0],
        "total_views": conn.execute(
            "SELECT COALESCE(SUM(access_count),0) FROM media_links").fetchone()[0],
        "total_topics": conn.execute(
            "SELECT COUNT(*) FROM topics WHERE is_active=1").fetchone()[0],
        "total_forwards": conn.execute(
            "SELECT COUNT(*) FROM forward_log").fetchone()[0],
        "by_type": {}
    }
    rows = conn.execute(
        "SELECT file_type, COUNT(*) as cnt FROM media_links GROUP BY file_type"
    ).fetchall()
    conn.close()
    data["by_type"] = {r["file_type"]: r["cnt"] for r in rows}
    return jsonify({"ok": True, "data": data})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    payload = request.get_json(force=True)
    for k, v in payload.items():
        set_setting(k, str(v))
    return jsonify({"ok": True})


# ─── Error handlers ───────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(503)
def unavailable(e):
    return render_template("503.html"), 503


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
