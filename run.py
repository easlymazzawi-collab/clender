"""
Unified launcher — Forum Converter Bot + Web Admin.

QUAN TRỌNG: Cần chạy CẢ HAI thành phần song song:

  Cách 1 — 1 lệnh duy nhất (khuyến nghị):
    python run.py
    → Chạy Bot Telegram + Web server cùng lúc (+ watchdog tự restart web)

  Cách 2 — 2 terminal riêng:
    Terminal 1: python run.py --web   (web admin: localhost:5000)
    Terminal 2: python run.py --bot   (Telegram bot — XỬ LÝ DEEP LINK /start TOKEN)

  Windows 24/7:
    deploy\\start_windows.bat
    deploy\\install_windows_task.bat   (tự chạy khi VPS khởi động)

  Xác thực Telethon (chạy 1 lần):
    python run.py --auth

Luồng link chia sẻ:
  Telethon forwarder → lưu album vào DB → tạo t.me/bot?start=TOKEN
  User click link → Telegram gửi /start TOKEN đến Bot
  Bot cần đang CHẠY mới nhận được và gửi media về cho user!
"""

import sys
import time
import threading
import logging
import urllib.request

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
# Suppress Telethon's noisy background update logs (Got difference, etc.)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_web_error: str | None = None
_web_thread: threading.Thread | None = None
_web_lock = threading.Lock()
_watchdog_stop = threading.Event()
WATCHDOG_INTERVAL = int(__import__("os").getenv("WEB_WATCHDOG_SEC", "60"))


def _start_backup():
    """Backup chạy nền — bật với mọi mode (kể cả --bot)."""
    try:
        from utils.backup import start_backup_scheduler
        start_backup_scheduler()
    except Exception as e:
        logger.warning(f"backup scheduler: {e}")


def run_web():
    global _web_error
    from config.settings import WEB_HOST, WEB_PORT
    try:
        from database.models import init_db
        from forwarder.state import sync_file_sessions_to_db
        from app import app
        init_db()
        sync_file_sessions_to_db()
        logger.info(f"Web server → http://{WEB_HOST}:{WEB_PORT}")
        app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
    except OSError as e:
        _web_error = str(e)
        if "Address already in use" in str(e) or getattr(e, "errno", None) in (48, 98, 10048):
            logger.error(
                f"❌ Web server KHÔNG khởi động — cổng {WEB_PORT} đã bị chiếm.\n"
                f"   Windows: netstat -ano | findstr :{WEB_PORT}\n"
                f"   Linux:   ss -tlnp | grep {WEB_PORT}\n"
                f"   Hoặc đổi WEB_PORT trong .env"
            )
        else:
            logger.error(f"❌ Web server lỗi OSError: {e}")
    except Exception as e:
        _web_error = str(e)
        logger.error(f"❌ Web server crash: {e}", exc_info=True)
    finally:
        logger.warning("Web server thread đã dừng")


def _web_is_healthy() -> bool:
    from config.settings import WEB_PORT
    url = f"http://127.0.0.1:{WEB_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_web_thread() -> threading.Thread:
    """Khởi động (hoặc khởi động lại) web server thread."""
    global _web_thread, _web_error
    with _web_lock:
        _web_error = None
        t = threading.Thread(target=run_web, daemon=True, name="web-server")
        t.start()
        _web_thread = t
        return t


def _wait_for_web(timeout: float = 15.0) -> bool:
    """Chờ web server sẵn sàng sau khi start thread."""
    from config.settings import WEB_PORT
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _web_error:
            return False
        if _web_is_healthy():
            logger.info("✅ Web server đã sẵn sàng")
            return True
        time.sleep(0.5)
    logger.error(
        f"❌ Web server không phản hồi sau {timeout}s — "
        f"kiểm tra WEB_PORT={WEB_PORT} và firewall"
    )
    return False


def _web_watchdog():
    """
    Kiểm tra /health mỗi WEB_WATCHDOG_SEC giây.
    Nếu web chết im lặng (thread dừng hoặc không phản hồi) → tự restart.
    Bot vẫn chạy bình thường trong main thread.
    """
    logger.info(f"🔄 Web watchdog bật — kiểm tra mỗi {WATCHDOG_INTERVAL}s")
    while not _watchdog_stop.wait(WATCHDOG_INTERVAL):
        healthy = _web_is_healthy()
        alive = _web_thread is not None and _web_thread.is_alive()
        if healthy and alive:
            continue
        if not alive:
            logger.warning("⚠️ Web thread đã chết — tự khởi động lại…")
        else:
            logger.warning("⚠️ Web không phản hồi /health — tự khởi động lại…")
        _start_web_thread()
        if not _wait_for_web():
            logger.error("❌ Watchdog restart web thất bại — thử lại sau vòng tiếp theo")


def _start_web_watchdog():
    t = threading.Thread(target=_web_watchdog, daemon=True, name="web-watchdog")
    t.start()
    return t


def run_bot():
    from bot.main import main
    logger.info("Telegram bot starting… (xử lý /start TOKEN cho deep links)")
    main()


def run_auth():
    import asyncio
    import forwarder.auth as _auth_module
    asyncio.run(_auth_module.main())


def main():
    args = sys.argv[1:]

    if "--auth" in args:
        run_auth()
        return

    _start_backup()

    if "--web" in args:
        logger.warning(
            "⚠️  Chỉ chạy web server. Bot Telegram KHÔNG hoạt động!\n"
            "   Deep link t.me/bot?start=TOKEN sẽ KHÔNG được xử lý.\n"
            "   Dùng 'python run.py' để chạy cả hai."
        )
        run_web()
        return

    if "--bot" in args:
        logger.info(
            "Chỉ chạy bot. Web admin tắt — dùng 'python run.py' để bật cả web."
        )
        run_bot()
        return

    # Default: web + watchdog in background, bot in foreground
    logger.info("Khởi động cả Bot + Web server…")
    _start_web_thread()
    if not _wait_for_web():
        logger.warning(
            "⚠️  Bot vẫn chạy nhưng WEB KHÔNG hoạt động — "
            "forwarder dashboard và /api/* sẽ không truy cập được."
        )
    if "--no-watchdog" not in args:
        _start_web_watchdog()
    run_bot()   # Bot runs in main thread (blocks until Ctrl+C)


if __name__ == "__main__":
    main()
