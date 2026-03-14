"""
국회 회의록 신호 탐지 시스템 - 설정
라즈베리파이5 기준 최적화
"""
import os
from pathlib import Path

# ── 경로 ──
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "assembly.db"
RAW_DIR = BASE_DIR / "db" / "raw_json"
LOG_DIR = BASE_DIR / "logs"

# ── API ──
API_KEY = os.environ.get("ASSEMBLY_API_KEY", "c3b18004d3f648108853f46531aacdda")
API_BASE = "https://open.assembly.go.kr/portal/openapi"
API_PAGE_SIZE = 100          # 한 번에 가져올 건수
API_SLEEP_SEC = 0.5          # 호출 간 대기 (서버 부담 방지)

# ── API 엔드포인트 목록 ──
# 2025년 개편 이후 분리된 endpoint 구조
ENDPOINTS = {
    # 1) 전체 API 서비스 목록 (endpoint 자동 탐색용)
    "api_list":         "OPENSRVAPI",

    # 2) 회의 관련
    "conf_plenary":     "nwbpacrgavhjryiph",   # 본회의 회의정보 (추정, 탐색 후 교체)
    "conf_committee":   "nzmimeepazxkubdpn",   # 위원회 회의정보 (추정, 탐색 후 교체)

    # 3) 법안/의안
    "bill_propose":     "nzmimeepazxkubdpn",   # 의원발의법률안
    "bill_status":      "nknalejkafmcgpoqr",   # 법률안 심사 및 처리 (추정)
    "vote_result":      "nojepdqqaweusdfbi",   # 의안별 표결현황 (추정)
    "vote_member":      "nwvrqwxyaytdsfvhu",   # 의원 본회의 표결정보 (추정)

    # 4) 의원 정보
    "member_current":   "nwvrqwxyaytdsfvhu",   # 현역 의원 (추정)
}

# ※ 위 endpoint ID는 추정치.
# 첫 실행 시 OPENSRVAPI로 전체 목록을 받아서
# 실제 endpoint를 자동 매핑하는 discover 단계를 거칩니다.

# ── 라즈베리파이5 리소스 제한 ──
MAX_WORKERS = 2              # 동시 처리 스레드 (Pi5 4코어 중 2개만)
BATCH_SIZE = 50              # DB 일괄 삽입 단위
MEMORY_LIMIT_MB = 2048       # 최대 메모리 사용 (8GB 모델 기준 2GB)

# ── LLM API (외부 호출용) ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL_TAG = "claude-haiku-4-5-20251001"       # 대량 태깅용
LLM_MODEL_EXPLAIN = "claude-sonnet-4-6-20250514"  # 설명 생성용

# ── 분석 주기 ──
COLLECT_CRON = "0 3 * * *"   # 매일 새벽 3시 수집
FEATURE_CRON = "0 5 * * 1"  # 매주 월요일 새벽 5시 feature 집계
DETECT_CRON  = "0 6 * * 1"  # 매주 월요일 새벽 6시 신호 탐지
