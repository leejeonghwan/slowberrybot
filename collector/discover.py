"""
Step 0: API 엔드포인트 자동 탐색
OPENSRVAPI를 호출해서 사용 가능한 전체 API 목록을 받아오고,
회의록/법안/표결 관련 endpoint를 자동 매핑한다.
"""
import json
import time
import logging
import requests
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import API_KEY, API_BASE, API_SLEEP_SEC, DB_PATH

logger = logging.getLogger(__name__)


def fetch_all_apis() -> list[dict]:
    """OPENSRVAPI에서 전체 API 목록을 수집"""
    all_rows = []
    page = 1
    while True:
        url = f"{API_BASE}/OPENSRVAPI"
        params = {
            "KEY": API_KEY,
            "Type": "json",
            "pIndex": page,
            "pSize": 100,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            data = resp.json()
        except Exception as e:
            logger.error(f"OPENSRVAPI 호출 실패 (page={page}): {e}")
            break

        # 응답 구조: {"OPENSRVAPI": [{"head": [...]}, {"row": [...]}]}
        svc = data.get("OPENSRVAPI", [])
        rows = []
        for block in svc:
            if "row" in block:
                rows = block["row"]
                break

        if not rows:
            break

        all_rows.extend(rows)
        logger.info(f"OPENSRVAPI page {page}: {len(rows)}건 수집 (누적 {len(all_rows)})")

        # head에서 총 건수 확인
        total = 0
        for block in svc:
            if "head" in block:
                for h in block["head"]:
                    if isinstance(h, dict) and "list_total_count" in h:
                        total = h["list_total_count"]
        if total and len(all_rows) >= total:
            break

        page += 1
        time.sleep(API_SLEEP_SEC)

    return all_rows


# 회의록/법안/표결 관련 키워드 매칭 규칙
KEYWORD_MAP = {
    "conf_plenary":    ["본회의", "회의정보"],
    "conf_committee":  ["위원회", "회의록"],
    "bill_propose":    ["발의법률안", "의원발의"],
    "bill_status":     ["법률안심사", "심사처리", "의안정보"],
    "vote_result":     ["표결현황", "의안별표결"],
    "vote_member":     ["의원.*표결", "본회의표결정보"],
    "member_current":  ["의원정보", "현역의원", "의원현황"],
    "member_profile":  ["의원프로필", "의원인적"],
    "agenda_info":     ["안건정보", "의안목록"],
    "minutes_text":    ["회의록텍스트", "회의록본문", "발언내용"],
    "inspection":      ["국정감사", "감사정보"],
    "petition":        ["청원", "국민동의청원"],
}


def discover_endpoints(rows: list[dict]) -> dict:
    """API 목록에서 키워드 매칭으로 endpoint를 자동 분류
    
    실제 API 응답 필드명:
    - INF_ID: API ID
    - INF_NM: API 이름
    - INF_EXP: API 설명
    - CATE_NM: 카테고리
    """
    import re
    result = {}

    for row in rows:
        # 실제 응답 필드명
        api_name = row.get("INF_NM", "") or ""
        api_desc = row.get("INF_EXP", "") or ""
        api_id = row.get("INF_ID", "") or ""
        combined = api_name + " " + api_desc

        for category, keywords in KEYWORD_MAP.items():
            for kw in keywords:
                if re.search(kw, combined, re.IGNORECASE):
                    if category not in result:
                        result[category] = []
                    result[category].append({
                        "api_id": api_id,
                        "api_name": api_name,
                        "api_desc": api_desc,
                    })
                    break

    return result


def save_discovery(rows: list[dict], mapped: dict, out_dir: Path = None):
    """탐색 결과를 JSON으로 저장"""
    if out_dir is None:
        out_dir = Path(__file__).resolve().parent.parent / "db"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 전체 API 목록
    with open(out_dir / "all_apis.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    # 매핑 결과
    with open(out_dir / "endpoint_map.json", "w", encoding="utf-8") as f:
        json.dump(mapped, f, ensure_ascii=False, indent=2)

    logger.info(f"전체 {len(rows)}개 API 중 {sum(len(v) for v in mapped.values())}개 매핑 완료")
    for cat, apis in mapped.items():
        names = [a["api_name"] for a in apis]
        logger.info(f"  {cat}: {names}")


def run():
    """탐색 실행"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logger.info("=== API 엔드포인트 자동 탐색 시작 ===")
    rows = fetch_all_apis()
    if not rows:
        logger.error("API 목록을 가져오지 못했습니다. 인증키를 확인하세요.")
        return {}

    mapped = discover_endpoints(rows)
    save_discovery(rows, mapped)
    return mapped


if __name__ == "__main__":
    run()
