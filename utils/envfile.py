"""
Đọc / ghi file .env, giữ nguyên comment và thứ tự dòng.
Dùng cho trang quản lý cấu hình trên web.
"""

import os

ENV_PATH = os.getenv("ENV_FILE", ".env")

# Các key nhạy cảm — che giá trị khi hiển thị (chỉ hiện ***), nhưng vẫn sửa được
SENSITIVE_KEYS = {
    "BOT_TOKEN", "TELETHON_API_HASH", "SECRET_KEY", "TELETHON_PHONE",
}

# Mô tả ngắn cho từng key (hiện trên web)
KEY_HINTS = {
    "BOT_TOKEN":         "Token bot từ @BotFather",
    "BOT_USERNAME":      "Username bot (không @)",
    "ADMIN_IDS":         "ID admin, cách nhau dấu phẩy",
    "LINK_MODE":         "bot = t.me trực tiếp | web = qua domain (bền vững)",
    "TELETHON_API_ID":   "API ID từ my.telegram.org",
    "TELETHON_API_HASH": "API Hash từ my.telegram.org",
    "TELETHON_PHONE":    "Số điện thoại userbot",
    "TELETHON_SESSION":  "Tên file session",
    "SOURCE_FORUM_ID":   "ID forum nguồn",
    "DEST_FORUM_ID":     "ID forum đích",
    "BASE_URL":          "URL công khai (domain/IP VPS)",
    "WEB_PORT":          "Cổng web server",
    "SECRET_KEY":        "Khóa bí mật Flask",
    "BACKUP_KEEP_DAYS":  "Số ngày giữ backup",
    "BACKUP_INTERVAL_HOURS": "Backup mỗi N giờ",
    "BACKUP_TO_TELEGRAM": "1 = gửi backup lên topic Telegram",
    "LINK_CAPTION_TEMPLATE": "Mẫu caption ({url}, {caption})",
}


def read_env() -> list[dict]:
    """
    Đọc .env → list các dòng:
      {"type": "kv", "key", "value", "sensitive", "hint"}
      {"type": "comment"/"blank", "raw"}
    """
    lines = []
    if not os.path.exists(ENV_PATH):
        return lines
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for raw in f.read().splitlines():
            s = raw.strip()
            if not s:
                lines.append({"type": "blank", "raw": ""})
            elif s.startswith("#"):
                lines.append({"type": "comment", "raw": raw})
            elif "=" in s:
                key, _, val = raw.partition("=")
                key = key.strip()
                lines.append({
                    "type": "kv",
                    "key": key,
                    "value": val,
                    "sensitive": key in SENSITIVE_KEYS,
                    "hint": KEY_HINTS.get(key, ""),
                })
            else:
                lines.append({"type": "comment", "raw": raw})
    return lines


def read_env_dict() -> dict:
    return {l["key"]: l["value"] for l in read_env() if l["type"] == "kv"}


def write_env(updates: dict):
    """
    Cập nhật các key trong .env (giữ comment + thứ tự).
    Key mới (chưa có) sẽ được thêm vào cuối.
    """
    existing = read_env() if os.path.exists(ENV_PATH) else []
    seen = set()
    out = []
    for l in existing:
        if l["type"] == "kv" and l["key"] in updates:
            out.append(f"{l['key']}={updates[l['key']]}")
            seen.add(l["key"])
        elif l["type"] == "kv":
            out.append(f"{l['key']}={l['value']}")
        else:
            out.append(l["raw"])
    # Thêm key mới
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")

    # Ghi an toàn (file tạm rồi replace)
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, ENV_PATH)


def mask(value: str) -> str:
    """Che giá trị nhạy cảm: giữ 4 ký tự đầu + cuối."""
    if not value or len(value) <= 8:
        return "••••"
    return value[:4] + "••••" + value[-4:]
