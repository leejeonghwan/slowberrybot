#!/bin/bash
# ================================================
# 라즈베리파이5 초기 설정 스크립트
# 국회 회의록 신호 탐지 시스템
# ================================================
set -e

echo "🏛️ 국회 회의록 신호 탐지 시스템 - Pi5 설정"
echo "============================================"

# ── 1. 시스템 패키지 ──
echo "[1/6] 시스템 패키지 설치..."
sudo apt-get update
sudo apt-get install -y \
    python3-pip python3-venv \
    sqlite3 \
    git curl jq

# ── 2. 프로젝트 디렉토리 ──
echo "[2/6] 프로젝트 디렉토리 설정..."
PROJECT_DIR="${HOME}/assembly-signal"
mkdir -p "${PROJECT_DIR}"/{db/raw_json,logs}

# 이 스크립트가 있는 디렉토리에서 프로젝트 파일 복사
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$SCRIPT_DIR" != "$PROJECT_DIR" ]; then
    cp -r "${SCRIPT_DIR}"/* "${PROJECT_DIR}/"
fi

# ── 3. Python 가상환경 + 패키지 ──
echo "[3/6] Python 가상환경 설정..."
cd "${PROJECT_DIR}"
python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install \
    requests \
    python-telegram-bot \
    anthropic

# ── 4. 환경 변수 설정 ──
echo "[4/6] 환경 변수 설정..."
ENV_FILE="${PROJECT_DIR}/.env"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# 국회 열린정보 API
export ASSEMBLY_API_KEY="c3b18004d3f648108853f46531aacdda"

# Anthropic API (LLM 태깅/설명 생성용)
export ANTHROPIC_API_KEY=""

# 텔레그램 봇
export TELEGRAM_BOT_TOKEN=""
export TELEGRAM_CHAT_ID=""
ENVEOF
    echo "  → .env 파일 생성됨. 텔레그램 봇 토큰과 Anthropic API 키를 입력하세요."
    echo "  → nano ${ENV_FILE}"
else
    echo "  → .env 파일 이미 존재"
fi

# ── 5. DB 초기화 ──
echo "[5/6] SQLite DB 초기화..."
source "$ENV_FILE" 2>/dev/null || true
sqlite3 "${PROJECT_DIR}/db/assembly.db" < "${PROJECT_DIR}/db/schema.sql"
echo "  → DB 생성 완료: ${PROJECT_DIR}/db/assembly.db"

# ── 6. cron 설정 ──
echo "[6/6] cron 스케줄 설정..."
CRON_SCRIPT="${PROJECT_DIR}/scripts/cron_pipeline.sh"
cat > "$CRON_SCRIPT" << 'CRONEOF'
#!/bin/bash
# cron에서 실행되는 파이프라인 스크립트
cd "${HOME}/assembly-signal"
source .env
source venv/bin/activate

LOG="logs/cron_$(date +%Y%m%d_%H%M%S).log"

echo "=== 파이프라인 시작: $(date) ===" >> "$LOG"

# 수집
python bot.py collect >> "$LOG" 2>&1

# 파싱
python bot.py parse >> "$LOG" 2>&1

# 규칙 태깅
python bot.py tag_rule >> "$LOG" 2>&1

# LLM 태깅 (매일 200건씩)
python bot.py tag_llm 200 >> "$LOG" 2>&1

echo "=== 파이프라인 완료: $(date) ===" >> "$LOG"
CRONEOF
chmod +x "$CRON_SCRIPT"

WEEKLY_SCRIPT="${PROJECT_DIR}/scripts/cron_weekly.sh"
cat > "$WEEKLY_SCRIPT" << 'WEEKEOF'
#!/bin/bash
# 주간 집계 + 탐지 + 리포트
cd "${HOME}/assembly-signal"
source .env
source venv/bin/activate

LOG="logs/weekly_$(date +%Y%m%d_%H%M%S).log"

echo "=== 주간 분석 시작: $(date) ===" >> "$LOG"

# Feature 집계
python bot.py aggregate >> "$LOG" 2>&1

# 신호 탐지
python bot.py detect >> "$LOG" 2>&1

# 리포트 생성 (텔레그램 전송은 봇이 돌아갈 때)
python bot.py report >> "$LOG" 2>&1

echo "=== 주간 분석 완료: $(date) ===" >> "$LOG"
WEEKEOF
chmod +x "$WEEKLY_SCRIPT"

# cron 등록
(crontab -l 2>/dev/null | grep -v "assembly-signal"; cat << CRONTAB
# 국회 회의록 시스템 - 매일 새벽 3시 수집/파싱/태깅
0 3 * * * ${PROJECT_DIR}/scripts/cron_pipeline.sh
# 매주 월요일 새벽 5시 주간 분석
0 5 * * 1 ${PROJECT_DIR}/scripts/cron_weekly.sh
CRONTAB
) | crontab -

echo ""
echo "============================================"
echo "✅ 설치 완료!"
echo ""
echo "다음 단계:"
echo "  1. .env 파일에 API 키 입력:"
echo "     nano ${PROJECT_DIR}/.env"
echo ""
echo "  2. 텔레그램 봇 실행:"
echo "     cd ${PROJECT_DIR}"
echo "     source venv/bin/activate && source .env"
echo "     python bot.py bot"
echo ""
echo "  3. 또는 CLI로 테스트:"
echo "     python bot.py discover"
echo "     python bot.py status"
echo "     python bot.py pipeline"
echo ""
echo "  4. systemd 서비스로 등록 (봇 상시 실행):"
echo "     sudo cp scripts/assembly-bot.service /etc/systemd/system/"
echo "     sudo systemctl enable assembly-bot"
echo "     sudo systemctl start assembly-bot"
echo "============================================"
