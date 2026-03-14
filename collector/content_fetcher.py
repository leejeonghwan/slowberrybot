"""
Phase 7: 회의록 본문 수집기
─────────────────────────
- meeting 테이블의 CONFER_NUM으로 record.assembly.go.kr 뷰어 페이지 접근
- HTML에서 발언 블록(.speaker) 추출 → 구조화된 JSON 저장
- Pi5 메모리 고려: 회의 1건씩 처리 후 즉시 저장/메모리 해제
- 텔레그램으로 진행 상황 보고

데이터 소스:
  https://record.assembly.go.kr/assembly/viewer/minutes/xml.do?id={CONFER_NUM}&type=view

HTML 구조 (2026.03 확인):
  .minutes_body 내부에 div.speaker 블록이 발언 단위로 나열
  각 .speaker 블록:
    .name   → 발언자 이름
    .role   → 직함 (위원장, 장관, 실장 등) — 텍스트 노드에 포함
    .area   → 선거구 (위원인 경우)
    innerText → 발언 전문 (이름/직함/선거구 포함)
"""
import json
import time
import sqlite3
import logging
import traceback
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH, RAW_DIR, API_SLEEP_SEC

logger = logging.getLogger(__name__)

VIEWER_BASE = "https://record.assembly.go.kr/assembly/viewer/minutes/xml.do"

# ── 역할 추출 정규식 ──
import re

# speaker div의 텍스트에서 역할 추출
ROLE_PATTERNS = re.compile(
    r'(위원장|부위원장|위원|의장|부의장|의원|'
    r'국무위원|국무총리|부총리|'
    r'장관|차관|처장|청장|국장|과장|실장|수석|비서관|'
    r'증인|참고인|진술인|감정인|전문위원|수석전문위원|'
    r'정부위원|대리인|보좌관|사무총장|의사국장|법제실장|'
    r'기획관리실장|입법차장|사무차장)'
)


@dataclass
class SpeakerBlock:
    """하나의 발언 블록"""
    sequence_no: int
    speaker_name: str
    speaker_role: str = ""
    speaker_area: str = ""        # 선거구
    speaker_class: str = ""       # CSS class (spk_mem 등)
    text: str = ""                # 발언 전문
    char_count: int = 0


@dataclass
class MeetingContent:
    """하나의 회의록 전체 내용"""
    meeting_id: str
    confer_num: int
    title: str = ""
    meeting_date: str = ""
    speaker_blocks: list = field(default_factory=list)
    agenda_items: list = field(default_factory=list)
    total_speakers: int = 0
    total_chars: int = 0
    fetch_url: str = ""
    fetched_at: str = ""


class ContentFetcher:
    """회의록 HTML 본문 수집기"""

    def __init__(self, notify_fn=None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.notify = notify_fn or (lambda msg: print(msg))
        self.text_dir = RAW_DIR / "meeting_texts"
        self.text_dir.mkdir(parents=True, exist_ok=True)

    def fetch_meeting(self, confer_num: int) -> MeetingContent | None:
        """
        단일 회의록 HTML을 가져와 파싱.
        confer_num: API 응답의 CONFER_NUM (= 뷰어 id 파라미터)
        """
        url = f"{VIEWER_BASE}?id={confer_num}&type=view"

        try:
            resp = requests.get(url, timeout=60, headers={
                "User-Agent": "Mozilla/5.0 (compatible; AssemblySignalBot/1.0)"
            })
            resp.raise_for_status()
            resp.encoding = "utf-8"
        except requests.RequestException as e:
            logger.error(f"회의록 HTML 가져오기 실패 [{confer_num}]: {e}")
            return None

        return self._parse_html(resp.text, confer_num, url)

    def _parse_html(self, html: str, confer_num: int, url: str) -> MeetingContent:
        """HTML에서 발언 블록 추출"""
        soup = BeautifulSoup(html, "html.parser")

        content = MeetingContent(
            meeting_id="",
            confer_num=confer_num,
            fetch_url=url,
            fetched_at=datetime.now().isoformat(),
        )

        # 제목 추출
        title_el = soup.select_one(".minutes_header .tit, .tit_lg, h2")
        if title_el:
            content.title = title_el.get_text(strip=True)

        # 발언자 블록 추출
        body = soup.select_one(".minutes_body")
        if not body:
            # fallback: 전체 페이지에서 speaker 찾기
            speakers = soup.select(".speaker")
        else:
            speakers = body.select(".speaker")

        seq = 0
        for sp_div in speakers:
            seq += 1
            block = self._parse_speaker_block(sp_div, seq)
            if block and block.text.strip():
                content.speaker_blocks.append(block)

        # 안건 추출
        agenda_els = body.select("p.angun") if body else []
        for ag in agenda_els:
            text = ag.get_text(strip=True)
            if text:
                content.agenda_items.append(text)

        content.total_speakers = len(content.speaker_blocks)
        content.total_chars = sum(b.char_count for b in content.speaker_blocks)

        return content

    def _parse_speaker_block(self, div, seq: int) -> SpeakerBlock | None:
        """개별 speaker div에서 발언 정보 추출"""
        block = SpeakerBlock(sequence_no=seq)

        # CSS 클래스에서 speaker 유형 파악
        classes = div.get("class", [])
        if isinstance(classes, list):
            block.speaker_class = " ".join(classes)
        else:
            block.speaker_class = str(classes)

        # 이름 추출
        name_el = div.select_one(".name")
        if name_el:
            block.speaker_name = name_el.get_text(strip=True)

        # 선거구 추출
        area_el = div.select_one(".area")
        if area_el:
            block.speaker_area = area_el.get_text(strip=True).strip("()")

        # 역할 추출: .name 앞의 텍스트 또는 별도 요소
        # HTML 구조: <div class="speaker">
        #   <span class="name">윤한홍</span> 앞에 "위원장" 텍스트가 있음
        full_text = div.get_text(separator="\n", strip=True)

        # 역할: 이름 앞에 나오는 직함 텍스트
        if block.speaker_name:
            name_idx = full_text.find(block.speaker_name)
            if name_idx > 0:
                before_name = full_text[:name_idx].strip()
                role_match = ROLE_PATTERNS.search(before_name)
                if role_match:
                    block.speaker_role = role_match.group(1)

            # 발언 텍스트: 이름(+선거구) 이후의 모든 텍스트
            # 선거구가 있으면 선거구 이후부터, 없으면 이름 이후부터
            if block.speaker_area:
                area_marker = f"({block.speaker_area})"
                area_idx = full_text.find(area_marker)
                if area_idx >= 0:
                    block.text = full_text[area_idx + len(area_marker):].strip()
                else:
                    block.text = full_text[name_idx + len(block.speaker_name):].strip()
            else:
                block.text = full_text[name_idx + len(block.speaker_name):].strip()
        else:
            block.text = full_text

        block.char_count = len(block.text)
        return block

    def save_content(self, content: MeetingContent) -> Path:
        """파싱된 회의록을 JSON으로 저장"""
        filename = f"content_{content.confer_num}.json"
        path = self.text_dir / filename

        # SpeakerBlock을 dict로 변환
        data = {
            "meeting_id": content.meeting_id,
            "confer_num": content.confer_num,
            "title": content.title,
            "meeting_date": content.meeting_date,
            "total_speakers": content.total_speakers,
            "total_chars": content.total_chars,
            "fetch_url": content.fetch_url,
            "fetched_at": content.fetched_at,
            "agenda_items": content.agenda_items,
            "speaker_blocks": [asdict(b) for b in content.speaker_blocks],
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

        return path

    def fetch_and_store(self, confer_num: int, meeting_id: str = "",
                        meeting_date: str = "") -> dict | None:
        """수집 → 파싱 → 저장 → DB 업데이트 한 번에"""
        content = self.fetch_meeting(confer_num)
        if not content:
            return None

        content.meeting_id = meeting_id
        content.meeting_date = meeting_date

        # JSON 저장
        json_path = self.save_content(content)

        # DB meeting 테이블의 raw_text_path 업데이트
        if meeting_id:
            self.conn.execute(
                "UPDATE meeting SET raw_text_path = ? WHERE meeting_id = ?",
                (str(json_path), meeting_id)
            )
            self.conn.commit()

        return {
            "confer_num": confer_num,
            "meeting_id": meeting_id,
            "speakers": content.total_speakers,
            "chars": content.total_chars,
            "agendas": len(content.agenda_items),
            "path": str(json_path),
        }

    def fetch_batch(self, limit: int = 0, skip_existing: bool = True) -> dict:
        """
        meeting 테이블에서 본문 미수집 건을 배치로 수집.
        limit: 최대 수집 건수 (0=전체)
        skip_existing: JSON 파일이 이미 있으면 건너뜀
        """
        # meeting_nth에 CONFER_NUM이 저장되어 있음
        rows = self.conn.execute("""
            SELECT meeting_id, meeting_nth, meeting_date
            FROM meeting
            WHERE meeting_nth IS NOT NULL
            ORDER BY meeting_date DESC
        """).fetchall()

        self.notify(f"📖 **Phase 7: 회의록 본문 수집**\n총 {len(rows):,}건 대상")

        collected = 0
        skipped = 0
        errors = 0

        for mid, confer_num, mdate in rows:
            if limit and collected >= limit:
                break

            # 이미 수집된 건 건너뛰기
            if skip_existing:
                json_path = self.text_dir / f"content_{confer_num}.json"
                if json_path.exists():
                    skipped += 1
                    continue

            try:
                confer_num_int = int(confer_num)
            except (ValueError, TypeError):
                skipped += 1
                continue

            result = self.fetch_and_store(confer_num_int, mid, mdate or "")
            if result:
                collected += 1
                if collected % 10 == 0:
                    self.notify(
                        f"📖 진행: {collected}건 수집 "
                        f"(건너뜀: {skipped}, 오류: {errors})"
                    )
            else:
                errors += 1

            time.sleep(API_SLEEP_SEC)  # 서버 부담 방지

        stats = {
            "collected": collected,
            "skipped": skipped,
            "errors": errors,
            "total_target": len(rows),
        }
        self.notify(
            f"✅ **회의록 본문 수집 완료**\n"
            f"수집: {collected:,}건 / 건너뜀: {skipped:,}건 / 오류: {errors:,}건"
        )
        return stats

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 테스트: 정무위원회 제432회 제3차 (2026.02.23) 1건 수집
    fetcher = ContentFetcher()
    result = fetcher.fetch_and_store(
        confer_num=56291,
        meeting_id="test_56291",
        meeting_date="2026-02-23",
    )
    if result:
        print(f"\n=== 수집 결과 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")

        # JSON 파일 미리보기
        json_path = Path(result["path"])
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"\n첫 3개 발언 미리보기:")
        for b in data["speaker_blocks"][:3]:
            print(f"  [{b['sequence_no']}] {b['speaker_role']} {b['speaker_name']}: "
                  f"{b['text'][:80]}...")
    else:
        print("수집 실패")

    fetcher.close()
