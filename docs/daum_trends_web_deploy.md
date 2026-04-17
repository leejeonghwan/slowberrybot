# Daum 실시간 트렌드 웹 앱 배포

이 앱은 `gunicorn` 뒤에서 실행되고, `nginx`가 80 포트에서 프록시하는 구성을 기본으로 합니다.

## 1. 가장 빠른 실행

```bash
python3 -m venv .venv-web
source .venv-web/bin/activate
pip install -r requirements-web.txt
gunicorn --bind 0.0.0.0:8787 --workers 2 --threads 4 --worker-class gthread --timeout 30 daum_trends_web.wsgi:app
```

브라우저에서 `http://서버IP:8787`로 접속할 수 있습니다.

## 2. Docker로 실행

```bash
docker build -t daum-trends-web .
docker run -d --name daum-trends-web -p 8787:8787 \
  -e DAUM_TRENDS_CACHE_TTL=60 \
  -e DAUM_TRENDS_STALE_IF_ERROR_SECONDS=21600 \
  -e DAUM_TRENDS_TIMEOUT_SECONDS=10 \
  -e DAUM_TRENDS_STATE_PATH=/app/runtime/daum_trends_snapshot.json \
  daum-trends-web
```

## 3. systemd + nginx

```bash
chmod +x scripts/setup_daum_trends_web.sh
DOMAIN=example.com HOST=127.0.0.1 PORT=8787 ./scripts/setup_daum_trends_web.sh
```

스크립트가 아래 파일을 생성합니다.

- `tmp/daum-trends-web.service`
- `tmp/daum-trends-web.nginx.conf`

생성 후:

```bash
sudo cp tmp/daum-trends-web.service /etc/systemd/system/daum-trends-web.service
sudo cp tmp/daum-trends-web.nginx.conf /etc/nginx/sites-available/daum-trends-web
sudo ln -sf /etc/nginx/sites-available/daum-trends-web /etc/nginx/sites-enabled/daum-trends-web
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl daemon-reload
sudo systemctl enable --now daum-trends-web
```

## 4. 운영 메모

- `/healthz`로 헬스 체크가 가능합니다.
- `runtime/daum_trends_snapshot.json`에 마지막 성공 응답을 저장합니다.
- Daum 응답이 일시적으로 실패하면, 최근 성공 데이터를 최대 6시간 동안 대체 표시합니다.
- 조정 가능한 환경 변수:
  - `DAUM_TRENDS_CACHE_TTL`
  - `DAUM_TRENDS_STALE_IF_ERROR_SECONDS`
  - `DAUM_TRENDS_TIMEOUT_SECONDS`
  - `DAUM_TRENDS_STATE_PATH`

