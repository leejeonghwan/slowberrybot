"""
22대 국회 전체 백필 수집기
─────────────────────────
- 22대 국회(2024.5.30~) 전체 데이터를 처음부터 수집
- 중단/재개 가능 (progress 파일로 추적)
- Pi5 메모리 고려: 페이지 단위 수집→즉시 적재→메모리 해제
- 텔레그램으로 진행 상황 실시간 보고

수집 순서:
  Phase 1: API 엔드포인트 탐색 (어떤 API가 있는지)
  Phase 2: 의원 정보 (speaker 매핑의 기초)
  Phase 3: 위원회 목록
  Phase 4: 회의 목록 (본회의 + 위원회)
  Phase 5: 법안/의안 정보
  Phase 6: 표결 정보
  Phase 7: 회의록 본문 (가장 무거움, 회의 단위로 분할)
"""
import json
import time
import sqlite3
import logging
import traceback
from datetime import datetime
from pathlib import Path

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    API_KEY, API_BASE, API_PAGE_SIZE, API_SLEEP_SEC,
    DB_PATH, RAW_DIR, LOG_DIR
)

logger = logging.getLogger(__name__)

ASSEMBLY_ID = 22  # 22대 국회

# ── 확인된 API 서비스코드 ──
KNOWN_ENDPOINTS = {
    "본회의_회의록": "nzbyfwhwaoanttzje",
    "위원회_회의록": "ncwgseseafwbuheph",
    "의원_정보": "nwvrqwxyaytdsfvhu",
    "법안_정보": "nayjnliqaexiioauy",
}

# ── 회의록 수집 기간 (연도별) ──
CONF_DATE_YEARS = ["2024", "2025", "2026"]

# ── 진행 상황 파일 ──
PROGRESS_FILE = DB_PATH.parent / "backfill_progress.json"


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {
        "phase": 0,
        "current_endpoint": None,
        "current_page": 0,
        "total_collected": {},
        "started_at": None,
        "last_updated": None,
    }


def save_progress(prog: dict):
    prog["last_updated"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


class BackfillCollector:
    """22대 국회 전체 백필"""

    def __init__(self, notify_fn=None):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(DB_PATH))
        self._init_db()
        self.progress = load_progress()
        # 텔레그램 알림 콜백 (없으면 print)
        self.notify = notify_fn or (lambda msg: print(msg))

    def _init_db(self):
        schema_path = DB_PATH.parent / "schema.sql"
        if schema_path.exists():
            self.conn.executescript(schema_path.read_text(encoding="utf-8"))
            self.conn.commit()

    # ── 범용 API 호출기 ──

    def _api_call(self, endpoint: str, params: dict = None) -> tuple[list, int]:
        """API 호출 → (rows, total_count)"""
        url = f"{API_BASE}/{endpoint}"
        base_params = {"KEY": API_KEY, "Type": "json"}
        if params:
            base_params.update(params)

        try:
            resp = requests.get(url, params=base_params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API 호출 실패 [{endpoint}]: {e}")
            return [], 0
        except json.JSONDecodeError:
            logger.error(f"JSON 파싱 실패 [{endpoint}]: {resp.text[:200]}")
            return [], 0

        # 열린국회정보 응답 파싱
        # 구조: {endpoint_name: [{"head": [...]}, {"row": [...]}]}
        svc_data = None
        for key in data:
            if isinstance(data[key], list):
                svc_data = data[key]
                break

        if not svc_data:
            return [], 0

        rows = []
        total = 0
        for block in svc_data:
            if isinstance(block, dict):
                if "head" in block:
                    for h in block["head"]:
                        if isinstance(h, dict):
                            if "list_total_count" in h:
                                total = h["list_total_count"]
                            # 에러 체크
                            if "RESULT" in h:
                                result = h["RESULT"]
                                if result.get("CODE") not in ("INFO-000", "INFO-200", None):
                                    logger.warning(f"API 응답 코드: {result}")
                                    return [], 0
                if "row" in block:
                    rows = block["row"]

        return rows, total

    def _fetch_all(self, endpoint: str, extra_params: dict = None,
                   label: str = "", start_page: int = 1) -> list[dict]:
        """endpoint의 모든 페이지를 순회 수집. 중단 지점부터 재개 가능."""
        all_rows = []
        page = start_page
        total = None
        label = label or endpoint

        while True:
            params = {"pIndex": page, "pSize": API_PAGE_SIZE}
            if extra_params:
                params.update(extra_params)

            rows, total_count = self._api_call(endpoint, params)

            if total is None and total_count:
                total = total_count
                self.notify(f"📦 [{label}] 총 {total:,}건 수집 시작 (page {page}~)")

            if not rows:
                break

            all_rows.extend(rows)

            # 진행 상황 저장 (매 페이지)
            self.progress["current_endpoint"] = endpoint
            self.progress["current_page"] = page
            save_progress(self.progress)

            if page % 10 == 0:
                logger.info(f"[{label}] page {page}: 누적 {len(all_rows):,}건 / 총 {total or '?'}건")

            if total and len(all_rows) >= total:
                break

            page += 1
            time.sleep(API_SLEEP_SEC)

        self.notify(f"✅ [{label}] 수집 완료: {len(all_rows):,}건")
        return all_rows

    def _save_raw(self, category: str, rows: list[dict], chunk_idx: int = 0):
        """원시 JSON 저장 (청크 단위)"""
        if not rows:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RAW_DIR / f"{category}_{ts}_chunk{chunk_idx}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        logger.debug(f"원시 저장: {path}")

    # ═══════════════════════════════════
    # Phase 1: API 엔드포인트 탐색
    # ═══════════════════════════════════

    def phase1_discover(self) -> dict:
        """사용 가능한 API 목록을 모두 수집하고 분류"""
        self.notify("🔍 **Phase 1: API 엔드포인트 탐색**")

        rows = self._fetch_all("OPENSRVAPI", label="API 목록")
        self._save_raw("api_list", rows)

        # 키워드 기반 자동 분류
        import re
        categories = {
            "회의록_본회의": [],
            "회의록_위원회": [],
            "회의_정보": [],
            "의원_정보": [],
            "법안_발의": [],
            "법안_심사": [],
            "표결": [],
            "위원회_정보": [],
            "국정감사": [],
            "청원": [],
            "기타": [],
        }

        keyword_rules = [
            ("회의록_본회의", r"본회의.*회의록|회의록.*본회의"),
            ("회의록_위원회", r"위원회.*회의록|회의록.*위원회"),
            ("회의_정보",    r"회의일정|회의정보|안건.*회의"),
            ("의원_정보",    r"의원.*정보|의원.*현황|의원.*인적"),
            ("법안_발의",    r"발의.*법률안|의원.*발의"),
            ("법안_심사",    r"심사.*처리|의안.*정보|법률안.*심사"),
            ("표결",        r"표결.*현황|표결.*정보"),
            ("위원회_정보",  r"위원회.*현황|위원회.*목록|소관위"),
            ("국정감사",    r"국정감사|감사.*정보"),
            ("청원",        r"청원"),
        ]

        for row in rows:
            name = row.get("SRVC_NM", "") or ""
            desc = row.get("SRVC_DC", "") or ""
            api_id = row.get("SRVC_ID", "") or ""
            combined = f"{name} {desc}"

            matched = False
            for cat, pattern in keyword_rules:
                if re.search(pattern, combined):
                    categories[cat].append({
                        "id": api_id, "name": name, "desc": desc[:100]
                    })
                    matched = True
                    break
            if not matched:
                categories["기타"].append({
                    "id": api_id, "name": name, "desc": desc[:100]
                })

        # 결과 저장
        map_path = DB_PATH.parent / "endpoint_map.json"
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(categories, f, ensure_ascii=False, indent=2)

        # 보고
        report_lines = ["**발견된 API:**"]
        for cat, apis in categories.items():
            if apis and cat != "기타":
                report_lines.append(f"  • {cat}: {len(apis)}개")
                for a in apis[:3]:
                    report_lines.append(f"    - `{a['id']}` {a['name']}")
        report_lines.append(f"  • 기타: {len(categories['기타'])}개")

        self.notify("\n".join(report_lines))
        return categories

    # ═══════════════════════════════════
    # Phase 2: 의원 정보
    # ═══════════════════════════════════

    def phase2_members(self, endpoint_map: dict = None) -> int:
        """22대 의원 정보 수집 → member 테이블"""
        self.notify("👤 **Phase 2: 의원 정보 수집**")

        endpoint = KNOWN_ENDPOINTS.get("의원_정보")
        if not endpoint:
            self.notify("❌ 의원_정보 엔드포인트 미발견")
            return 0

        rows = self._fetch_all(
            endpoint,
            label="의원 정보 (22대)"
        )
        self._save_raw("members", rows)
        count = 0

        for row in rows:
            member_id = (row.get("MONA_CD") or row.get("NAAS_CD") or
                        row.get("MEMBER_ID") or row.get("NUM") or "")
            name = (row.get("HG_NM") or row.get("EMPNM") or
                   row.get("MEMBER_NAME") or "")
            if not name:
                continue

            party = row.get("POLY_NM") or row.get("PLYNM") or ""
            self.conn.execute("""
                INSERT OR REPLACE INTO member
                    (member_id, name, party, district, elected_count)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(member_id) or name,
                name, party,
                row.get("ORIG_NM") or row.get("ELECD") or "",
                row.get("GTELT_ERACO") or row.get("REELE_GBN_NM") or None,
            ))
            count += 1

        self.conn.commit()
        self.notify(f"✅ 의원 {count:,}명 적재 완료")
        return count

    # ═══════════════════════════════════
    # Phase 3: 위원회 목록
    # ═══════════════════════════════════

    def phase3_committees(self, endpoint_map: dict = None) -> int:
        self.notify("🏛️ **Phase 3: 위원회 정보 수집**")

        endpoints = self._get_endpoints(endpoint_map, "위원회_정보")
        count = 0

        for api in endpoints:
            rows = self._fetch_all(api["id"], label=f"위원회/{api['name']}")
            self._save_raw("committees", rows)

            for row in rows:
                cid = (row.get("CMIT_CD") or row.get("UNIT_CD") or
                      row.get("HR_DEPT_CD") or "")
                name = (row.get("CMIT_NM") or row.get("UNIT_NM") or
                       row.get("DEPT_NM") or "")
                if not name:
                    continue

                self.conn.execute("""
                    INSERT OR REPLACE INTO committee
                        (committee_id, committee_name, committee_type)
                    VALUES (?, ?, ?)
                """, (str(cid) or name, name, "상임위"))
                count += 1

        self.conn.commit()
        self.notify(f"✅ 위원회 {count:,}건 적재 완료")
        return count

    # ═══════════════════════════════════
    # Phase 4: 회의 목록
    # ═══════════════════════════════════

    def phase4_meetings(self, endpoint_map: dict = None) -> int:
        """본회의 + 위원회 회의 목록 수집 (연도별 분할)"""
        self.notify("📋 **Phase 4: 회의 목록 수집 (본회의 + 위원회)**")

        count = 0
        
        # 본회의 + 위원회 (연도별 분할 호출)
        for meeting_type_key in ["본회의_회의록", "위원회_회의록"]:
            endpoint = KNOWN_ENDPOINTS.get(meeting_type_key)
            if not endpoint:
                logger.warning(f"엔드포인트 미발견: {meeting_type_key}")
                continue
            
            meeting_type = "본회의" if "본회의" in meeting_type_key else "위원회"
            
            for year in CONF_DATE_YEARS:
                all_rows = self._fetch_all(
                    endpoint,
                    extra_params={
                        "DAE_NUM": str(ASSEMBLY_ID),
                        "CONF_DATE": year
                    },
                    label=f"{meeting_type} {year}년"
                )
                self._save_raw(f"meetings_{meeting_type}_{year}", all_rows)
                
                for row in all_rows:
                    # 회의 ID (여러 필드 조합)
                    mid = (row.get("CONF_ID") or row.get("CT_ID") or
                          row.get("MEETING_ID") or row.get("CONFER_NUM") or
                          f"{row.get('UNIT_CD','')}-{row.get('CONF_DT','')}-{row.get('CONF_MEET_CNT','')}")

                    # 회의 날짜
                    meeting_date = (row.get("CONF_DT") or row.get("MTG_DT") or
                                  row.get("MEETING_DATE") or "")
                    if meeting_date:
                        meeting_date = meeting_date[:10]

                    # 위원회 정보
                    committee = (row.get("UNIT_CD") or row.get("CMIT_CD") or
                               row.get("UNIT_NM") or row.get("CMIT_NM") or "")

                    # 회의록 본문 URL/경로
                    content_url = (row.get("CONF_CNTNT_URL") or row.get("LINK_URL") or
                                 row.get("DET_LINK_URL") or "")
                    raw_text = row.get("CONF_CNTNT") or row.get("MEETING_CONTENT") or ""

                    self.conn.execute("""
                        INSERT OR REPLACE INTO meeting
                            (meeting_id, assembly_id, committee_id,
                             meeting_type, meeting_date, meeting_nth,
                             agenda_ids_json, raw_text_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(mid), ASSEMBLY_ID, committee,
                        meeting_type, meeting_date,
                        row.get("CONF_MEET_CNT") or row.get("MEETING_CNT") or None,
                        json.dumps(
                            [row.get("AGENDA_ID"), row.get("BILL_NO")],
                            ensure_ascii=False
                        ) if row.get("AGENDA_ID") or row.get("BILL_NO") else None,
                        content_url or None,
                    ))

                    # 본문이 직접 포함된 경우 파싱 대기
                    if raw_text and len(raw_text) > 100:
                        text_path = RAW_DIR / f"text_{mid}.txt"
                        text_path.write_text(raw_text, encoding="utf-8")
                        self.conn.execute(
                            "UPDATE meeting SET raw_text_path = ? WHERE meeting_id = ?",
                            (str(text_path), str(mid))
                        )

                    count += 1

        self.conn.commit()
        self.notify(f"✅ 회의 {count:,}건 적재 완료")
        return count

    # ═══════════════════════════════════
    # Phase 5: 법안/의안
    # ═══════════════════════════════════

    def phase5_bills(self, endpoint_map: dict = None) -> int:
        self.notify("📜 **Phase 5: 법안/의안 수집**")

        endpoint = KNOWN_ENDPOINTS.get("법안_정보")
        if not endpoint:
            self.notify("❌ 법안_정보 엔드포인트 미발견")
            return 0

        rows = self._fetch_all(
            endpoint,
            extra_params={"AGE": str(ASSEMBLY_ID)},
            label="법안 정보 (22대)"
        )
        self._save_raw("bills", rows)

        count = 0
        for row in rows:
            aid = (row.get("BILL_NO") or row.get("BILL_ID") or
                  row.get("AGENDA_ID") or "")
            title = (row.get("BILL_NAME") or row.get("BILL_NM") or
                    row.get("TITLE") or "")
            if not title:
                continue

            self.conn.execute("""
                INSERT OR REPLACE INTO agenda
                    (agenda_id, agenda_type, title, proposer,
                     propose_date, committee_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(aid),
                row.get("BILL_KIND") or row.get("BILL_KIND_CD") or "법안",
                title,
                row.get("PROPOSER") or row.get("PUBL_PROPOSER") or row.get("RST_PROPOSER") or "",
                row.get("PROPOSE_DT") or row.get("PPSL_DT") or "",
                row.get("CMIT_NM") or row.get("CURR_CMIT") or "",
                row.get("PROC_RESULT") or row.get("RGS_PROC_RESULT_CD") or "",
            ))
            count += 1

        self.conn.commit()
        self.notify(f"✅ 법안/의안 {count:,}건 적재 완료")
        return count

    # ═══════════════════════════════════
    # Phase 6: 표결
    # ═══════════════════════════════════

    def phase6_votes(self, endpoint_map: dict = None) -> int:
        self.notify("🗳️ **Phase 6: 표결 정보 수집**")

        endpoints = self._get_endpoints(endpoint_map, "표결")
        count = 0

        for api in endpoints:
            rows = self._fetch_all(
                api["id"],
                extra_params={"AGE": str(ASSEMBLY_ID)},
                label=f"표결/{api['name']}"
            )
            self._save_raw("votes", rows)

            for row in rows:
                aid = row.get("BILL_NO") or row.get("BILL_ID") or ""
                if not aid:
                    continue

                # agenda 테이블 표결 필드 업데이트
                self.conn.execute("""
                    UPDATE agenda SET
                        vote_date = COALESCE(?, vote_date),
                        vote_yes = COALESCE(?, vote_yes),
                        vote_no = COALESCE(?, vote_no),
                        vote_abstain = COALESCE(?, vote_abstain)
                    WHERE agenda_id = ?
                """, (
                    row.get("VOTE_DATE") or row.get("VOT_DT"),
                    row.get("YES_CNT") or row.get("APRV_CNT"),
                    row.get("NO_CNT") or row.get("OPPS_CNT"),
                    row.get("ABSTAIN_CNT") or row.get("ABST_CNT"),
                    str(aid),
                ))
                count += 1

        self.conn.commit()
        self.notify(f"✅ 표결 {count:,}건 적재 완료")
        return count

    # ═══════════════════════════════════
    # 헬퍼
    # ═══════════════════════════════════

    def _get_endpoints(self, endpoint_map: dict, category: str) -> list[dict]:
        """endpoint_map에서 해당 카테고리 API 목록 반환"""
        if endpoint_map is None:
            map_path = DB_PATH.parent / "endpoint_map.json"
            if map_path.exists():
                with open(map_path, "r") as f:
                    endpoint_map = json.load(f)
            else:
                return []
        return endpoint_map.get(category, [])

    def get_status(self) -> str:
        """현재 백필 진행 상황"""
        prog = self.progress
        stats = {}
        for table in ["member", "committee", "meeting", "agenda", "utterance", "clause"]:
            try:
                row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = row[0]
            except:
                stats[table] = 0

        lines = [
            "📊 **백필 진행 상황**",
            f"현재 Phase: {prog.get('phase', 0)}",
            f"시작: {prog.get('started_at', '미시작')}",
            f"최종 업데이트: {prog.get('last_updated', '-')}",
            "",
            f"의원: {stats['member']:,}명",
            f"위원회: {stats['committee']:,}건",
            f"회의: {stats['meeting']:,}건",
            f"법안: {stats['agenda']:,}건",
            f"발언: {stats['utterance']:,}건",
            f"절(clause): {stats['clause']:,}건",
        ]

        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        if db_size > 1024**2:
            lines.append(f"DB 크기: {db_size/1024**2:.1f} MB")
        else:
            lines.append(f"DB 크기: {db_size/1024:.1f} KB")

        return "\n".join(lines)

    # ═══════════════════════════════════
    # 메인 실행
    # ═══════════════════════════════════

    def run(self, start_phase: int = None, end_phase: int = None) -> str:
        """
        전체 백필 실행.
        start_phase: 시작 phase (None이면 마지막 중단점부터)
        end_phase: 종료 phase (None이면 끝까지)
        """
        if start_phase is None:
            start_phase = self.progress.get("phase", 0)
        if end_phase is None:
            end_phase = 6

        if not self.progress.get("started_at"):
            self.progress["started_at"] = datetime.now().isoformat()

        self.notify(
            f"🚀 **22대 국회 백필 시작**\n"
            f"Phase {start_phase} → {end_phase}"
        )

        endpoint_map = None
        results = {}

        try:
            # Phase 1
            if start_phase <= 1 <= end_phase:
                self.progress["phase"] = 1
                save_progress(self.progress)
                endpoint_map = self.phase1_discover()
                results["Phase 1 (API 탐색)"] = f"{sum(len(v) for v in endpoint_map.values())}개 API 발견"

            # Phase 2
            if start_phase <= 2 <= end_phase:
                self.progress["phase"] = 2
                save_progress(self.progress)
                n = self.phase2_members(endpoint_map)
                results["Phase 2 (의원)"] = f"{n:,}명"

            # Phase 3
            if start_phase <= 3 <= end_phase:
                self.progress["phase"] = 3
                save_progress(self.progress)
                n = self.phase3_committees(endpoint_map)
                results["Phase 3 (위원회)"] = f"{n:,}건"

            # Phase 4
            if start_phase <= 4 <= end_phase:
                self.progress["phase"] = 4
                save_progress(self.progress)
                n = self.phase4_meetings(endpoint_map)
                results["Phase 4 (회의)"] = f"{n:,}건"

            # Phase 5
            if start_phase <= 5 <= end_phase:
                self.progress["phase"] = 5
                save_progress(self.progress)
                n = self.phase5_bills(endpoint_map)
                results["Phase 5 (법안)"] = f"{n:,}건"

            # Phase 6
            if start_phase <= 6 <= end_phase:
                self.progress["phase"] = 6
                save_progress(self.progress)
                n = self.phase6_votes(endpoint_map)
                results["Phase 6 (표결)"] = f"{n:,}건"

            self.progress["phase"] = 7  # 완료
            save_progress(self.progress)

        except Exception as e:
            error_msg = f"❌ Phase {self.progress['phase']}에서 오류: {e}"
            logger.error(f"{error_msg}\n{traceback.format_exc()}")
            save_progress(self.progress)
            self.notify(error_msg + "\n\n다시 시작하면 중단점부터 재개합니다.")
            results["오류"] = str(e)

        # 최종 보고
        report_lines = ["", "═" * 30, "📊 **백필 결과 요약**", ""]
        for phase, result in results.items():
            report_lines.append(f"  • {phase}: {result}")
        report_lines.append("")
        report_lines.append(self.get_status())

        report = "\n".join(report_lines)
        self.notify(report)
        return report

    def close(self):
        self.conn.close()


# ═══════════════════════════════════════
# CLI 실행
# ═══════════════════════════════════════

def run(start_phase=None, end_phase=None, notify_fn=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_DIR / "backfill.log")),
        ]
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    collector = BackfillCollector(notify_fn=notify_fn)
    result = collector.run(start_phase=start_phase, end_phase=end_phase)
    collector.close()
    return result


if __name__ == "__main__":
    import sys
    start = int(sys.argv[1]) if len(sys.argv) > 1 else None
    end = int(sys.argv[2]) if len(sys.argv) > 2 else None
    run(start_phase=start, end_phase=end)
