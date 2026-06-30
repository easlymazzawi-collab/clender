# 🤖 Forum Converter Bot

Hệ thống **chuyển đổi diễn đàn Telegram thông minh** — gồm 2 thành phần:

1. **Telethon Forwarder** — Clone toàn bộ forum (topics + messages) với tốc độ cao (batch 50 msg/call, album giữ grouped_id, resume được)
2. **Telegram Bot** — Tạo link chia sẻ media, forward có/ẩn tên, auto-clone topics
3. **Web Admin Dashboard** — Quản trị toàn bộ qua giao diện web dark theme

---

## 📁 Cấu trúc dự án

```
forum-bot/
├── forwarder/
│   ├── __init__.py
│   ├── auth.py          # CLI auth helper (chạy 1 lần)
│   ├── core.py          # Telethon async forwarder (v2.5.8 compat)
│   ├── runner.py        # Background session runner (thread + asyncio)
│   └── state.py         # State persistence (file + SQLite mirror)
├── bot/
│   ├── main.py          # Entry-point bot Telegram
│   └── handlers.py      # Command & message handlers
├── config/settings.py   # Đọc biến môi trường
├── database/models.py   # SQLite WAL models
├── utils/               # Token, thumbnail helpers
├── web/
│   ├── static/          # CSS + JS
│   └── templates/       # Jinja2 templates
│       ├── dashboard.html
│       ├── forwarder_dashboard.html   ← Quản lý phiên
│       ├── forwarder_new.html         ← Tạo phiên mới
│       ├── forwarder_session.html     ← Chi tiết + log trực tiếp (SSE)
│       ├── forwarder_setup.html       ← Hướng dẫn auth Telethon
│       ├── links.html, topics.html, logs.html, settings.html
├── app.py               # Flask web server + REST API
├── run.py               # Launcher tổng hợp
├── requirements.txt
├── .env.example
└── forwarder_state/     # State files + Telethon session (tự tạo)
```

---

## ⚡ Cài đặt

### 1. Clone & cài dependencies

```bash
git clone <repo>
cd forum-bot
pip install -r requirements.txt
```

### 2. Cấu hình

```bash
cp .env.example .env
# Điền đầy đủ các trường trong .env
```

### 3. Xác thực Telethon (1 lần duy nhất)

```bash
python3 run.py --auth
# Nhập số điện thoại và OTP từ Telegram
# Session được lưu vào: forwarder_state/session_main.session
```

### 4. Chạy (Windows / local)

```bash
python run.py            # Bot + Web (khuyến nghị — deep link hoạt động)
python run.py --web      # Chỉ web admin (port 5000)
python run.py --bot      # Chỉ Telegram bot
python run.py --auth     # Xác thực Telethon (1 lần)
```

**Web admin**: http://localhost:5000

### VPS Ubuntu (24/7)

```bash
bash deploy/setup_ubuntu.sh
# Service chạy: python run.py (bot + web + backup)
journalctl -u forumbot -f
```

### Windows VPS (24/7)

```cmd
REM Cách 1 — chạy tay (có tự restart nếu crash)
deploy\start_windows.bat

REM Cách 2 — tự chạy khi VPS khởi động (chạy CMD as Administrator)
deploy\install_windows_task.bat
```

`python run.py` có **web watchdog** — mỗi 60s kiểm tra `/health`, web chết thì tự bật lại (bot không bị ảnh hưởng).

Mở cổng `WEB_PORT` (mặc định 5000) trên firewall nếu truy cập từ ngoài.

### Cập nhật code (giữ data)

Chỉ thay file code (`.py`, `web/`, …). **Giữ nguyên**:

- `.env`
- `database/` (SQLite)
- `forwarder_state/` (session Telethon + tiến độ resume clone)

Sau đó: `pip install -r requirements.txt` (nếu dependencies đổi) → chạy lại `python run.py`.

---

## 🔧 Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|-------------|------------------------|------------|
| Bot hoạt động, **web không vào được** | Chạy `python run.py --bot` (chỉ bot) | Dùng `python run.py` (cả bot + web) |
| Web không phản hồi | Cổng 5000 bị chiếm / firewall chặn | `ss -tlnp \| grep 5000`, mở `WEB_PORT` |
| **Backup không chạy** | Trước đây backup chỉ bật khi web chạy | Cập nhật code mới — backup chạy với mọi mode |
| Web chết lại sau vài ngày | Thread web crash, bot vẫn sống | Dùng `deploy\start_windows.bat` hoặc cập nhật code có watchdog |
| Backup thủ công | — | `python utils/backup.py` hoặc `POST /api/backup` |
| Kiểm tra nhanh | — | `curl http://127.0.0.1:5000/health` |

**Lưu ý:** `python run.py --bot` chỉ chạy Telegram bot — web admin và forwarder dashboard **sẽ tắt**. Dùng `python run.py` để chạy đầy đủ.

---

## ⚡ Telethon Forwarder (Tính năng chính)

### Chế độ Forward Thường
- Forward từ kênh/topic cụ thể, bắt đầu từ message ID tuỳ chọn
- Hỗ trợ kênh thường và forum với topics
- **Ẩn tên** (drop_author): giữ 100% nội dung — emoji premium, bold/italic, album

### Chế độ Backup Forum (Full Clone)
**Phase 1: Clone Topics**
- Clone toàn bộ topic structure
- Hỗ trợ 3 chế độ icon: clone từ nguồn / emoji cố định / chỉ title
- Skip General topic tuỳ chọn

**Phase 2: Forward Messages**
- Batch 50 msg/call → nhanh 10-20× so với từng msg
- Album forward trong 1 call → giữ nguyên grouped_id ở forum đích
- Lỗi permanent (MessageIdInvalid...) → skip thông minh, không dừng
- FloodWait handling với retry tự động
- **Resume**: chạy lại sẽ tiếp tục từ chỗ dừng, không forward lại msg cũ

### Quản lý qua Web
- Tạo phiên mới qua form web (không cần gõ lệnh)
- Xem tiến độ real-time qua SSE (Server-Sent Events)
- Progress bar, stats (msg forwarded, topics, lỗi, skip)
- Live log stream
- Dừng phiên, xóa phiên

---

## 🤖 Telegram Bot

| Lệnh | Chức năng |
|------|-----------|
| `/start` | Khởi động + menu |
| `/share` | Reply vào media → tạo link chia sẻ |
| `/forward` | Reply → forward **có tên** đến forum đích |
| `/fwd_anon` | Reply → forward **ẩn tên** |
| `/clone_topic [tên]` | *(Admin)* Clone topic sang forum mới |
| `/stats`, `/links` | *(Admin)* Thống kê |
| `/del_link <token>` | *(Admin)* Xóa link |
| `/set <key> <value>` | *(Admin)* Cài đặt |

**Gửi media trực tiếp** → bot tự tạo link + gửi thumbnail+link (video)

---

## 🔌 REST API

```
GET    /api/stats                   Thống kê tổng quan
GET    /api/links                   Danh sách link chia sẻ
DELETE /api/links/<token>           Xóa link
GET    /api/topics                  Chủ đề Bot đã clone
GET    /api/logs                    Lịch sử forward Bot
POST   /api/settings                Cập nhật cài đặt
GET    /api/forwarder/sessions      Danh sách phiên Telethon
GET    /api/forwarder/session/<key> Chi tiết phiên
GET    /media/<token>               Serve media (→ Telegram CDN)
GET    /forwarder/stream/<key>      SSE progress stream
```

---

## 🔧 Yêu cầu hệ thống

- Python 3.11+
- Telethon: API ID + API Hash từ [my.telegram.org/apps](https://my.telegram.org/apps)
- Bot phải là **Admin** trong cả forum nguồn và forum đích (cho Bot forward)
- Telethon account phải là **Admin** hoặc có quyền đọc trong forum nguồn
- Forum đích phải bật **Topics** (Supergroup với forum mode)

---

## 📜 Giấy phép

MIT — Tự do sử dụng, chỉnh sửa, phân phối.
