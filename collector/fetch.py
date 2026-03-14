"""
Step 1: 데이터 수집기
- API에서 회의록/법안/표결 데이터를 증분 수집
- 원시 JSON을 로컬 저장 + SQLite에 메타데이터 적재
- Pi5 cron 또는 텔레그램 명령으로 실행
"""
import json
import time
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    API_KEY, API_BASE, API_PAGE_SIZE, API_SLEEP_SEC,
    DB_PATH, RAW_DIR, BATCH_SIZE
)

logger = logging.getLogger(__name__)


class Collector:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH))
        self._init_db()
        self._load_endpoint_map()

    def _init_db(self):
        """스키마 초기화"""
        schema_path = DB_PATH.parent / "schema.sql"
        if schema_path.exists():
            self.conn.executescript(schema_path.read_text(encoding="utf-8"))
            self.conn.commit()

    def _load_endpoint_map(self):
        """discover 결과 로드"""
        map_path = DB_PATH.parent / "endpoint_map.json"
        if map_path.exists():
            with open(map_path, "r", encoding="utf-8") as f:
                self.endpoint_map = json.load(f)
        else:
            self.endpoint_map = {}
            logger.warning("endpoint_map.json이 없습니다. discover.py를 먼저 실행하세요.")

    def _api_call(self, endpoint: str, extra_params: dict = None) -> tuple[list, int]:
        """단일 API 페이지 호출. (rows, total_count) 반환"""
        url = f"{API_BASE}/{endpoint}"
        params = {
            "KEY": API_KEY,
            "Type": "json",
            "pIndex": 1,
            "pSize": API_PAGE_SIZE,
        }
        if extra_params:
            params.update(extra_params)

        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"API 호출 실패 [{endpoint}]: {e}")
            return [], 0

        # 열린국회정보 응답 파싱 (endpoint명이 최상위 키)
        svc_data = data.get(endpoint, [])
        if not svc_data:
            # 다른 키 구조일 수 있음
            for key in data:
                if isinstance(data[key], list):
                    svc_data = data[key]
                    break

        rows = []
        total = 0
        for block in svc_data:
            if isinstance(block, dict):
                if "head" in block:
                    for h in block["head"]:
                        if isinstance(h, dict) and "list_total_count" in h:
                            total = h["list_total_count"]
                if "row" in block:
                    rows = block["row"]

        return rows, total

    def _checksum(self, data) -> str:
        return hashlib.md5(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _is_collected(self, endpoint: str, params_json: str, checksum: str) -> bool:
        """이미 수집한 데이터인지 확인"""
        cur = self.conn.execute(
            "SELECT 1 FROM collect_log WHERE endpoint=? AND checksum=? LIMIT 1",
            (endpoint, checksum)
        )
        return cur.fetchone() is not None

    def _log_collection(self, endpoint, params, page, count, checksum):
        self.conn.execute(
            "INSERT INTO collect_log (endpoint, params_json, page_index, row_count, checksum) VALUES (?,?,?,?,?)",
            (endpoint, json.dumps(params, ensure_ascii=False), page, count, checksum)
        )
        self.conn.commit()

    def fetch_all_pages(self, endpoint: str, extra_params: dict = None, label: str = "") -> list[dict]:
        """endpoint의 모든 페이지를 순회하며 수집"""
        all_rows = []
        page = 1
        total = None
        label = label or endpoint

        while True:
            params = {"pIndex": page, "pSize": API_PAGE_SIZE}
            if extra_params:
                params.update(extra_params)

            rows, total_count = self._api_call(endpoint, params)
            if total is None and total_count:
                total = total_count
                logger.info(f"[{label}] 총 {total}건 수집 시작")

            if not rows:
                break

            cs = self._checksum(rows)
            if not self._is_collected(endpoint, json.dumps(params), cs):
                all_rows.extend(rows)
                self._log_collection(endpoint, params, page, len(rows), cs)
                logger.info(f"[{label}] page {page}: {len(rows)}건 (누적 {len(all_rows)})")
            else:
                logger.debug(f"[{label}] page {page}: 이미 수집됨, skip")

            if total and len(all_rows) >= total:
                break

            page += 1
            time.sleep(API_SLEEP_SEC)

        return all_rows

    def save_raw(self, category: str, rows: list[dict]):
        """원시 데이터를 JSON 파일로 저장"""
        if not rows:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RAW_DIR / f"{category}_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        logger.info(f"원시 데이터 저장: {path} ({len(rows)}건)")

    # ── 카테고리별 수집 + 적재 ──

    def collect_meetings(self, assembly_id: int = 22) -> int:
        """회의 정보 수집 → meeting 테이블 적재"""
        count = 0
        for cat in ["conf_plenary", "conf_committee"]:
            apis = self.endpoint_map.get(cat, [])
            for api in apis:
                endpoint = api["api_id"]
                rows = self.fetch_all_pages(endpoint, label=f"{cat}/{api['api_name']}")
                self.save_raw(cat, rows)
                for row in rows:
                    self._upsert_meeting(row, cat)
                    count += 1
        self.conn.commit()
        logger.info(f"회의 정보 {count}건 적재 완료")
        return count

    def collect_bills(self, assembly_id: int = 22) -> int:
        """법안/의안 수집 → agenda 테이블 적재"""
        count = 0
        for cat in ["bill_propose", "bill_status", "agenda_info"]:
            apis = self.endpoint_map.get(cat, [])
            for api in apis:
                endpoint = api["api_id"]
                rows = self.fetch_all_pages(
                    endpoint,
                    extra_params={"AGE": str(assembly_id)} if assembly_id else None,
                    label=f"{cat}/{api['api_name']}"
                )
                self.save_raw(cat, rows)
                for row in rows:
                    self._upsert_agenda(row)
                    count += 1
        self.conn.commit()
        logger.info(f"법안/의안 {count}건 적재 완료")
        return count

    def collect_votes(self, assembly_id: int = 22) -> int:
        """표결 수집 → agenda 테이블 표결 필드 업데이트"""
        count = 0
        for cat in ["vote_result", "vote_member"]:
            apis = self.endpoint_map.get(cat, [])
            for api in apis:
                endpoint = api["api_id"]
                rows = self.fetch_all_pages(endpoint, label=f"{cat}/{api['api_name']}")
                self.save_raw(cat, rows)
                count += len(rows)
        self.conn.commit()
        logger.info(f"표결 {count}건 수집 완료")
        return count

    def collect_members(self) -> int:
        """의원 정보 수집"""
        count = 0
        for cat in ["member_current", "member_profile"]:
            apis = self.endpoint_map.get(cat, [])
            for api in apis:
                endpoint = api["api_id"]
                rows = self.fetch_all_pages(endpoint, label=f"{cat}/{api['api_name']}")
                self.save_raw(cat, rows)
                for row in rows:
                    self._upsert_member(row)
                    count += 1
        self.conn.commit()
        logger.info(f"의원 정보 {count}건 적재 완료")
        return count

    # ── DB Upsert 헬퍼 ──

    def _upsert_meeting(self, row: dict, category: str):
        """API 응답 row를 meeting 테이블에 맞게 변환/삽입"""
        # 필드명은 API마다 다를 수 있으므로 유연하게 매핑
        meeting_id = (
            row.get("CONF_ID") or row.get("MEETING_ID") or
            row.get("CT_ID") or row.get("CONFER_NUM") or
            f"{row.get('UNIT_CD','')}-{row.get('CONF_DT','')}-{row.get('CONF_MEET_CNT','')}"
        )
        meeting_type = "본회의" if "plenary" in category else "위원회"
        meeting_date = row.get("CONF_DT") or row.get("MEETING_DATE") or row.get("MTG_DT") or ""

        self.conn.execute("""
            INSERT OR REPLACE INTO meeting
                (meeting_id, assembly_id, committee_id, meeting_type, meeting_date, meeting_nth)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(meeting_id),
            row.get("AGE") or row.get("ERACO") or None,
            row.get("UNIT_CD") or row.get("CMIT_CD") or row.get("COMMITTEE_ID") or None,
            meeting_type,
            meeting_date[:10] if meeting_date else None,
            row.get("CONF_MEET_CNT") or row.get("MEETING_CNT") or None,
        ))

    def _upsert_agenda(self, row: dict):
        agenda_id = row.get("BILL_NO") or row.get("BILL_ID") or row.get("AGENDA_ID") or ""
        self.conn.execute("""
            INSERT OR REPLACE INTO agenda
                (agenda_id, agenda_type, title, proposer, propose_date, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(agenda_id),
            row.get("BILL_KIND") or row.get("AGENDA_TYPE") or "법안",
            row.get("BILL_NAME") or row.get("BILL_NM") or row.get("TITLE") or "",
            row.get("PROPOSER") or row.get("PUBL_PROPOSER") or "",
            row.get("PROPOSE_DT") or row.get("PPSL_DT") or "",
            row.get("PROC_RESULT") or row.get("STATUS") or "",
        ))

    def _upsert_member(self, row: dict):
        member_id = row.get("MONA_CD") or row.get("MEMBER_ID") or row.get("NAAS_CD") or ""
        self.conn.execute("""
            INSERT OR REPLACE INTO member
                (member_id, name, party, district, elected_count)
            VALUES (?, ?, ?, ?, ?)
        """, (
            str(member_id),
            row.get("HG_NM") or row.get("MEMBER_NAME") or row.get("NAAS_NM") or "",
            row.get("POLY_NM") or row.get("PARTY") or "",
            row.get("ORIG_NM") or row.get("DISTRICT") or "",
            row.get("GTELT_ERACO") or row.get("ELECTED_CNT") or None,
        ))

    def close(self):
        self.conn.close()


def run(targets: list[str] = None):
    """수집 실행. targets: ["meetings","bills","votes","members"] 또는 None(전부)"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(Path(__file__).resolve().parent.parent / "logs" / "collect.log")),
        ]
    )

    c = Collector()
    results = {}

    all_targets = targets or ["meetings", "bills", "votes", "members"]

    if "meetings" in all_targets:
        results["meetings"] = c.collect_meetings()
    if "bills" in all_targets:
        results["bills"] = c.collect_bills()
    if "votes" in all_targets:
        results["votes"] = c.collect_votes()
    if "members" in all_targets:
        results["members"] = c.collect_members()

    c.close()
    return results


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] if len(sys.argv) > 1 else None
    results = run(targets)
    print(json.dumps(results, ensure_ascii=False, indent=2))
