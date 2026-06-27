"""
Telethon auth helper — run this ONCE via CLI to create the session file.
After authentication, the web app can use the session without re-auth.

Usage:
  python3 forwarder/auth.py
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

api_id   = int(os.getenv("TELETHON_API_ID", "0"))
api_hash = os.getenv("TELETHON_API_HASH", "")
phone    = os.getenv("TELETHON_PHONE", "")
session  = os.getenv("TELETHON_SESSION", "forwarder_state/session_main")


async def main():
    if not api_id or not api_hash:
        print("❌ Set TELETHON_API_ID và TELETHON_API_HASH trong .env trước.")
        sys.exit(1)
    if not phone:
        phone_in = input("📱 Số điện thoại (VD: +84901234567): ").strip()
    else:
        phone_in = phone
        print(f"📱 Dùng phone từ .env: {phone_in}")

    from telethon import TelegramClient

    os.makedirs(os.path.dirname(session), exist_ok=True)
    client = TelegramClient(session, api_id, api_hash)

    await client.start(phone=phone_in)
    me = await client.get_me()
    print(f"\n✅ Đăng nhập thành công!")
    print(f"   Tài khoản : {me.first_name} {me.last_name or ''}")
    print(f"   Username  : @{me.username or 'N/A'}")
    print(f"   Session   : {session}.session")
    print(f"\n👉 Giờ bạn có thể chạy web app: python3 run.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
