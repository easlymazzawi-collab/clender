#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Cài đặt Forum Converter Bot trên Ubuntu VPS (24/7)
# ══════════════════════════════════════════════════════════════
#  Chạy: bash deploy/setup_ubuntu.sh
# ══════════════════════════════════════════════════════════════
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "📂 App dir: $APP_DIR"

# 1. Cài system deps
echo "📦 Cài system packages…"
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

# 2. Tạo virtualenv
echo "🐍 Tạo virtualenv…"
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Tạo .env nếu chưa có
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠️  Đã tạo .env từ mẫu — HÃY ĐIỀN thông tin rồi chạy lại!"
    echo "    nano $APP_DIR/.env"
    exit 0
fi

# 4. Xác thực Telethon (nếu chưa có session)
if [ ! -f forwarder_state/session_main.session ]; then
    echo "🔐 Chưa có session Telethon. Xác thực ngay:"
    ./venv/bin/python run.py --auth
fi

# 5. Cài systemd service
echo "⚙️  Cài systemd service…"
SERVICE_FILE="/etc/systemd/system/forumbot.service"
USER_NAME="$(whoami)"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Forum Converter Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python run.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable forumbot
sudo systemctl restart forumbot

echo ""
echo "✅ HOÀN TẤT! Bot đang chạy 24/7."
echo ""
echo "📋 Lệnh quản lý:"
echo "   sudo systemctl status forumbot     # xem trạng thái"
echo "   sudo systemctl restart forumbot    # khởi động lại"
echo "   sudo systemctl stop forumbot       # dừng"
echo "   journalctl -u forumbot -f          # xem log realtime"
echo ""
echo "🌐 Web admin: http://<IP-VPS>:5000  (mở cổng WEB_PORT trong firewall)"
echo "   Kiểm tra: curl http://127.0.0.1:5000/health"
