"""
Telethon auth helper — run this ONCE via CLI to create the session file.
After authentication, the web app can use the session without re-auth.

Usage (Windows):
  python run.py --auth

Usage (Linux/Mac):
  python3 run.py --auth
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

api_id   = int(os.getenv("TELETHON_API_ID", "0"))
api_hash = os.getenv("TELETHON_API_HASH", "")
phone    = os.getenv("TELETHON_PHONE", "")

# TELETHON_SESSION is just the session *name* (e.g. "session_main").
# The actual file is always saved inside the forwarder_state/ directory.
_session_name = os.getenv("TELETHON_SESSION", "session_main")
STATE_DIR     = os.path.abspath(os.getenv("FWD_STATE_DIR", "forwarder_state"))
SESSION_PATH  = os.path.join(STATE_DIR, _session_name)   # full path without .session


async def main():
    if not api_id or not api_hash:
        print("❌ Thiếu TELETHON_API_ID hoặc TELETHON_API_HASH trong file .env")
        print("   Lấy tại: https://my.telegram.org/apps")
        sys.exit(1)

    if not phone:
        phone_in = input("📱 Số điện thoại (VD: +84901234567): ").strip()
    else:
        phone_in = phone
        print(f"📱 Dùng phone từ .env: {phone_in}")

    from telethon import TelegramClient

    # Always ensure the state directory exists before creating session
    os.makedirs(STATE_DIR, exist_ok=True)

    print(f"\n🔐 Đang kết nối Telegram…")
    client = TelegramClient(SESSION_PATH, api_id, api_hash)

    await client.start(phone=phone_in)
    me = await client.get_me()

    session_file = SESSION_PATH + ".session"
    print(f"\n✅ Đăng nhập thành công!")
    print(f"   Tài khoản : {me.first_name} {me.last_name or ''}")
    print(f"   Username  : @{me.username or 'N/A'}")
    print(f"   Session   : {session_file}")
    print(f"\n👉 Giờ chạy web app:")
    print(f"   python run.py --web")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
