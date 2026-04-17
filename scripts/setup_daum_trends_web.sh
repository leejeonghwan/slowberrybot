#!/bin/bash
# ================================================
# Daum 실시간 트렌드 웹 앱 배포 준비 스크립트
# ================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv-web}"
RUNTIME_DIR="${RUNTIME_DIR:-${PROJECT_DIR}/runtime}"
APP_USER="${APP_USER:-$(id -un)}"
APP_GROUP="${APP_GROUP:-$(id -gn)}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
DOMAIN="${DOMAIN:-example.com}"
TMP_DIR="${PROJECT_DIR}/tmp"

echo "Daum 실시간 트렌드 웹 앱 배포 준비"
echo "===================================="
echo "PROJECT_DIR=${PROJECT_DIR}"
echo "VENV_DIR=${VENV_DIR}"
echo "RUNTIME_DIR=${RUNTIME_DIR}"

mkdir -p "${RUNTIME_DIR}" "${TMP_DIR}"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements-web.txt"

cat > "${TMP_DIR}/daum-trends-web.service" <<EOF
[Unit]
Description=Daum Trends Web App
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${PROJECT_DIR}
Environment=DAUM_TRENDS_CACHE_TTL=60
Environment=DAUM_TRENDS_STALE_IF_ERROR_SECONDS=21600
Environment=DAUM_TRENDS_TIMEOUT_SECONDS=10
Environment=DAUM_TRENDS_STATE_PATH=${RUNTIME_DIR}/daum_trends_snapshot.json
ExecStart=${VENV_DIR}/bin/gunicorn --bind ${HOST}:${PORT} --workers 2 --threads 4 --worker-class gthread --timeout 30 daum_trends_web.wsgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > "${TMP_DIR}/daum-trends-web.nginx.conf" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /static/ {
        proxy_pass http://${HOST}:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        expires 5m;
        add_header Cache-Control "public, max-age=300";
    }

    location / {
        proxy_pass http://${HOST}:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }
}
EOF

echo
echo "준비 완료"
echo "생성 파일:"
echo "  - ${TMP_DIR}/daum-trends-web.service"
echo "  - ${TMP_DIR}/daum-trends-web.nginx.conf"
echo
echo "다음 명령으로 반영하세요:"
echo "  sudo cp ${TMP_DIR}/daum-trends-web.service /etc/systemd/system/daum-trends-web.service"
echo "  sudo cp ${TMP_DIR}/daum-trends-web.nginx.conf /etc/nginx/sites-available/daum-trends-web"
echo "  sudo ln -sf /etc/nginx/sites-available/daum-trends-web /etc/nginx/sites-enabled/daum-trends-web"
echo "  sudo nginx -t && sudo systemctl reload nginx"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now daum-trends-web"

