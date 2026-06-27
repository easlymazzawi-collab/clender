# 🤖 Forum Converter Bot

Hệ thống **chuyển đổi diễn đàn Telegram thông minh** – tự động clone chủ đề, tạo link chia sẻ media, forward có/ẩn tên, và quản trị qua web dashboard.

---

## 📁 Cấu trúc dự án

```
forum-bot/
├── bot/
│   ├── __init__.py
│   ├── main.py          # Entry-point bot Telegram
│   └── handlers.py      # Tất cả command & message handlers
├── config/
│   ├── __init__.py
│   └── settings.py      # Đọc biến môi trường
├── database/
│   ├── __init__.py
│   └── models.py        # SQLite models + helpers
├── utils/
│   ├── __init__.py
│   ├── token.py         # Tạo token ngẫu nhiên
│   └── thumbnail.py     # Trích xuất thumbnail video
├── web/
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/app.js
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── links.html
│       ├── topics.html
│       ├── logs.html
│       ├── settings.html
│       ├── 404.html
│       └── 503.html
├── app.py               # Flask web server
├── run.py               # Launcher (bot + web)
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚡ Cài đặt nhanh

### 1. Clone & cài dependencies

```bash
git clone <repo>
cd forum-bot
pip install -r requirements.txt
```

### 2. Cấu hình

```bash
cp .env.example .env
nano .env   # điền BOT_TOKEN, ADMIN_IDS, DEST_FORUM_ID, BASE_URL
```

### 3. Chạy

```bash
# Chạy cả bot lẫn web server
python run.py

# Chỉ chạy bot
python run.py --bot

# Chỉ chạy web server
python run.py --web
```

Truy cập web admin: **http://localhost:5000**

---

## 🤖 Lệnh Bot

| Lệnh | Mô tả |
|------|-------|
| `/start` | Khởi động & xem hướng dẫn |
| `/help` | Danh sách đầy đủ lệnh |
| `/share` | Reply vào media → tạo link chia sẻ |
| `/forward` | Reply → forward có tên đến forum đích |
| `/fwd_anon` | Reply → forward **ẩn tên** đến forum đích |
| `/clone_topic [tên]` | *(Admin)* Clone chủ đề hiện tại sang forum mới |
| `/stats` | *(Admin)* Thống kê tổng quan |
| `/links` | *(Admin)* Danh sách link gần đây |
| `/del_link <token>` | *(Admin)* Xóa link chia sẻ |
| `/settings` | *(Admin)* Xem cài đặt |
| `/set <key> <value>` | *(Admin)* Đặt giá trị cài đặt |

---

## 🌟 Tính năng

### 📁 Chia sẻ Media qua Link
- Gửi bất kỳ media nào (ảnh, video, file, âm thanh, sticker...) → bot tự tạo link
- Link dạng: `https://yourdomain.com/media/<token>`
- Khi click → redirect đến Telegram CDN (không lưu file trên server)
- Đếm lượt truy cập cho mỗi link

### 🎬 Xử lý Video
- Tự động lấy thumbnail từ Telegram
- Gửi thumbnail + nút "▶️ Xem video" thay vì gửi trực tiếp file lớn
- Caption định dạng: `🔗 Nhấp vào link để xem: <url>`

### 📋 Clone Chủ Đề (Topic)
- Dùng `/clone_topic` trong topic nguồn → bot tạo topic mới ở forum đích
- Mọi media gửi vào topic nguồn → tự động clone sang topic đích (dưới dạng thumbnail + link)
- Giữ nguyên caption gốc + thêm link chia sẻ

### ↩️ Forward Linh Hoạt
- **Có tên** (`/forward`): Giữ nguyên thông tin người gửi gốc
- **Ẩn tên** (`/fwd_anon`): Copy nội dung mà không hiện tên người gửi

### 🌐 Web Admin Dashboard
- Tổng quan thống kê (links, views, topics, forwards)
- Biểu đồ hoạt động + phân loại media
- Quản lý link chia sẻ (xem, copy, xóa)
- Quản lý chủ đề đã clone
- Lịch sử forward (lọc theo chế độ/trạng thái)
- Cài đặt bot trực tiếp qua giao diện

### 🔌 REST API
```
GET  /api/stats          Thống kê tổng quan
GET  /api/links          Danh sách link
DELETE /api/links/<tok>  Xóa link
GET  /api/topics         Danh sách chủ đề
GET  /api/logs           Lịch sử forward
POST /api/settings       Cập nhật cài đặt
```

---

## 🔧 Yêu cầu

- Python 3.11+
- Bot phải là **Admin** trong cả forum nguồn và forum đích
- Forum đích phải bật **Topics** (Supergroup với forum mode)
- (Tùy chọn) `ffmpeg` để trích xuất thumbnail từ video local

---

## 🚀 Deploy với Gunicorn

```bash
# Web server
gunicorn -w 2 -b 0.0.0.0:5000 app:app

# Bot (chạy song song)
python run.py --bot
```

---

## 📜 Giấy phép

MIT – Tự do sử dụng, chỉnh sửa, phân phối.
