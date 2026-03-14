"""
Step 2: 발언 파싱/정규화
- 회의록 원문 텍스트를 발언(utterance) 단위로 분리
- 발언자 이름/역할 추출
- 질의-답변 쌍(Q/A pair) 복원
- 절(clause) 단위 분할
- 절차 발언 필터링 (rule-based)

입력 소스 2가지:
  1) plain text (구 시스템, ◯ 마커 기반)
  2) HTML 파싱된 JSON (content_fetcher.py가 생성한 speaker_blocks)

Pi5에서 전부 로컬로 처리. LLM 호출 없음.
"""
import re
import json
import sqlite3
import logging
from dataclasses import dataclass, field
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


# ── 정규식 패턴 ──

# 발언자 패턴: "◯위원장 홍길동", "◯홍길동 위원", "◯국무위원 김철수" 등
SPEAKER_PATTERNS = [
    # ◯ 또는 ○ 뒤에 직함+이름 또는 이름+직함
    re.compile(
        r'[◯○●]\s*'
        r'(?:'
        r'(?P<role1>위원장|부위원장|위원|의장|부의장|의원|국무위원|국무총리|'
        r'장관|차관|처장|청장|국장|과장|실장|수석|비서관|'
        r'증인|참고인|진술인|감정인|전문위원|수석전문위원|'
        r'정부위원|대리인|보좌관)\s*(?P<name1>[가-힣]{2,4})'
        r'|'
        r'(?P<name2>[가-힣]{2,4})\s*(?P<role2>위원장|부위원장|위원|의장|부의장|의원|'
        r'장관|차관|처장|청장|국장|과장|실장|수석|비서관|'
        r'증인|참고인|진술인|감정인|전문위원|수석전문위원|'
        r'정부위원|대리인|보좌관)'
        r')'
    ),
    # 이름만 있는 경우: "◯홍길동"
    re.compile(r'[◯○●]\s*(?P<name>[가-힣]{2,4})\s'),
]

# 역할 → speaker_role 정규화 매핑
ROLE_MAP = {
    "위원장": "위원장", "부위원장": "위원장",
    "의장": "위원장", "부의장": "위원장",
    "위원": "위원", "의원": "위원",
    "국무위원": "장관", "국무총리": "장관",
    "장관": "장관", "차관": "차관",
    "처장": "차관", "청장": "차관",
    "국장": "차관", "과장": "차관",
    "실장": "차관", "수석": "수석",
    "비서관": "수석",
    "증인": "증인", "참고인": "참고인",
    "진술인": "증인", "감정인": "참고인",
    "전문위원": "참고인", "수석전문위원": "참고인",
    "정부위원": "장관", "대리인": "참고인",
    "보좌관": "참고인",
}

# 절차 발언 패턴 (rule-based 필터)
PROCEDURAL_PATTERNS = [
    re.compile(r'(개의|개회|산회|폐회|정회|속개)\s*(하겠습니다|합니다|선포)'),
    re.compile(r'의사일정\s*(제\s*\d+\s*항|에\s*들어가)'),
    re.compile(r'(상정|회부|보고)합니다'),
    re.compile(r'(가결|부결|의결)\s*(되었습니다|됐습니다)'),
    re.compile(r'(이의|재석위원)\s*(없|과반)'),
    re.compile(r'(찬성|반대)\s*(하여\s*주시기|투표하여)'),
    re.compile(r'(출석|서명)\s*(날인|요청)'),
    re.compile(r'자료를?\s*(제출|요구|요청)'),
    re.compile(r'(다음|그러면|그다음)\s*(순서|안건)'),
    re.compile(r'질의하실\s*위원'),
    re.compile(r'(수고하셨습니다|감사합니다)\s*$'),
    re.compile(r'^(예|네|알겠습니다)\s*[.,]?\s*$'),
]

# 절(clause) 분할: 문장 종결 어미 기준
CLAUSE_SPLIT = re.compile(
    r'(?<=[다요죠까나])[.!?]\s+|'
    r'(?<=[다요죠까나]),\s+(?=[그이저])|'
    r'(?<=습니다)\.\s+|'
    r'(?<=합니다)\.\s+|'
    r'(?<=됩니다)\.\s+|'
    r'(?<=있습니다)\.\s+'
)


@dataclass
class ParsedUtterance:
    sequence_no: int
    speaker_name: str
    speaker_role: str = ""
    raw_text: str = ""
    is_procedural: bool = False
    clauses: list[str] = field(default_factory=list)


@dataclass
class QAPair:
    questioner_name: str
    answerer_name: str
    question_sequences: list[int] = field(default_factory=list)
    answer_sequences: list[int] = field(default_factory=list)


class UtteranceParser:
    """회의록 원문 → 구조화된 발언 리스트"""

    def parse_text(self, raw_text: str) -> list[ParsedUtterance]:
        """원문 텍스트를 발언 단위로 분리"""
        utterances = []
        current = None
        seq = 0

        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # 발언자 패턴 매칭
            speaker_match = self._match_speaker(line)
            if speaker_match:
                # 이전 발언 저장
                if current and current.raw_text.strip():
                    current.is_procedural = self._is_procedural(current.raw_text)
                    current.clauses = self._split_clauses(current.raw_text)
                    utterances.append(current)

                seq += 1
                name, role = speaker_match
                current = ParsedUtterance(
                    sequence_no=seq,
                    speaker_name=name,
                    speaker_role=ROLE_MAP.get(role, role) if role else "",
                    raw_text="",
                )
                # 발언자 이름 이후 텍스트 추출
                text_after = self._extract_text_after_speaker(line)
                if text_after:
                    current.raw_text = text_after
            elif current:
                current.raw_text += " " + line

        # 마지막 발언 저장
        if current and current.raw_text.strip():
            current.is_procedural = self._is_procedural(current.raw_text)
            current.clauses = self._split_clauses(current.raw_text)
            utterances.append(current)

        return utterances

    def _match_speaker(self, line: str) -> tuple[str, str] | None:
        """발언자 패턴 매칭. (이름, 역할) 반환"""
        for pattern in SPEAKER_PATTERNS:
            m = pattern.search(line)
            if m:
                groups = m.groupdict()
                name = groups.get("name1") or groups.get("name2") or groups.get("name") or ""
                role = groups.get("role1") or groups.get("role2") or ""
                if name:
                    return (name, role)
        return None

    def _extract_text_after_speaker(self, line: str) -> str:
        """발언자 표시 이후의 실제 발언 텍스트 추출"""
        # 발언자+역할 부분 제거
        for pattern in SPEAKER_PATTERNS:
            m = pattern.search(line)
            if m:
                return line[m.end():].strip()
        return ""

    def _is_procedural(self, text: str) -> bool:
        """절차 발언 여부 판별 (rule-based)"""
        text = text.strip()
        if len(text) < 50:  # 짧은 발언은 절차일 가능성 높음
            for pat in PROCEDURAL_PATTERNS:
                if pat.search(text):
                    return True
        return False

    def _split_clauses(self, text: str) -> list[str]:
        """발언을 절(clause) 단위로 분할"""
        text = text.strip()
        if not text:
            return []

        clauses = CLAUSE_SPLIT.split(text)
        # 너무 짧은 절은 이전 절에 합치기
        merged = []
        for c in clauses:
            c = c.strip()
            if not c:
                continue
            if merged and len(c) < 15:
                merged[-1] += " " + c
            else:
                merged.append(c)

        return merged if merged else [text]

    def parse_html_json(self, json_path: str | Path) -> list[ParsedUtterance]:
        """
        content_fetcher.py가 생성한 JSON 파일에서 발언 파싱.
        HTML 기반이므로 발언자·역할이 이미 구조화되어 있음.
        여기서는 절차 판별, clause 분할만 수행.
        """
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return self.parse_speaker_blocks(data.get("speaker_blocks", []))

    def parse_speaker_blocks(self, blocks: list[dict]) -> list[ParsedUtterance]:
        """
        speaker_blocks 리스트 → ParsedUtterance 리스트.
        content_fetcher 결과를 직접 받을 때 사용.
        """
        utterances = []
        for block in blocks:
            name = block.get("speaker_name", "").strip()
            if not name:
                continue

            role_raw = block.get("speaker_role", "")
            role = ROLE_MAP.get(role_raw, role_raw)
            text = block.get("text", "").strip()
            if not text:
                continue

            utt = ParsedUtterance(
                sequence_no=block.get("sequence_no", len(utterances) + 1),
                speaker_name=name,
                speaker_role=role,
                raw_text=text,
                is_procedural=self._is_procedural(text),
                clauses=self._split_clauses(text),
            )
            utterances.append(utt)

        return utterances

    def extract_qa_pairs(self, utterances: list[ParsedUtterance]) -> list[QAPair]:
        """발언 시퀀스에서 질의-답변 쌍 복원"""
        pairs = []
        i = 0
        while i < len(utterances):
            utt = utterances[i]

            # 위원(질의자) 발언 → 바로 다음 비위원(답변자) 발언 = Q/A 쌍
            if utt.speaker_role in ("위원", "") and not utt.is_procedural:
                q_seqs = [utt.sequence_no]
                questioner = utt.speaker_name

                # 같은 질의자의 연속 발언 묶기
                j = i + 1
                while j < len(utterances) and utterances[j].speaker_name == questioner:
                    q_seqs.append(utterances[j].sequence_no)
                    j += 1

                # 답변자 발언 찾기
                if j < len(utterances) and utterances[j].speaker_role in ("장관", "차관", "수석", "참고인", "증인"):
                    a_seqs = [utterances[j].sequence_no]
                    answerer = utterances[j].speaker_name

                    # 같은 답변자의 연속 발언 묶기
                    k = j + 1
                    while k < len(utterances) and utterances[k].speaker_name == answerer:
                        a_seqs.append(utterances[k].sequence_no)
                        k += 1

                    pairs.append(QAPair(
                        questioner_name=questioner,
                        answerer_name=answerer,
                        question_sequences=q_seqs,
                        answer_sequences=a_seqs,
                    ))
                    i = k
                    continue

            i += 1

        return pairs


class MeetingProcessor:
    """회의 단위로 파싱 → DB 적재"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.parser = UtteranceParser()

    def process_meeting(self, meeting_id: str, raw_text: str = "",
                        json_path: str = "") -> dict:
        """
        회의 원문 → 발언/절/Q&A 파싱 → DB 적재.
        json_path가 주어지면 HTML 파싱된 JSON에서, 아니면 plain text에서 파싱.
        """
        if json_path and Path(json_path).exists() and json_path.endswith(".json"):
            utterances = self.parser.parse_html_json(json_path)
        else:
            utterances = self.parser.parse_text(raw_text)
        qa_pairs = self.parser.extract_qa_pairs(utterances)

        # DB 적재
        utt_count = 0
        clause_count = 0

        for utt in utterances:
            cur = self.conn.execute("""
                INSERT INTO utterance
                    (meeting_id, sequence_no, speaker_name, speaker_role,
                     raw_text, char_count, is_procedural)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                meeting_id, utt.sequence_no, utt.speaker_name, utt.speaker_role,
                utt.raw_text, len(utt.raw_text), utt.is_procedural,
            ))
            utt_id = cur.lastrowid
            utt_count += 1

            for ci, clause_text in enumerate(utt.clauses, 1):
                self.conn.execute("""
                    INSERT INTO clause (utterance_id, sequence_no, text, char_count)
                    VALUES (?, ?, ?, ?)
                """, (utt_id, ci, clause_text, len(clause_text)))
                clause_count += 1

        # Q/A 쌍 적재
        qa_count = 0
        for qa in qa_pairs:
            self.conn.execute("""
                INSERT INTO qa_pair
                    (meeting_id, questioner_id, answerer_id,
                     question_utt_ids, answer_utt_ids)
                VALUES (?, ?, ?, ?, ?)
            """, (
                meeting_id,
                qa.questioner_name,  # 나중에 member_id로 교체
                qa.answerer_name,
                str(qa.question_sequences),
                str(qa.answer_sequences),
            ))
            qa_count += 1

        self.conn.commit()

        stats = {
            "meeting_id": meeting_id,
            "utterances": utt_count,
            "clauses": clause_count,
            "qa_pairs": qa_count,
            "procedural": sum(1 for u in utterances if u.is_procedural),
        }
        logger.info(f"파싱 완료: {stats}")
        return stats

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 테스트용
    sample = """
    ◯위원장 김민석  지금부터 보건복지위원회 제1차 전체회의를 개의하겠습니다.
    의사일정 제1항 국민건강보험법 일부개정법률안을 상정합니다.

    ◯홍길동 위원  장관님, 최근 전세사기 피해자들의 건강보험료 문제가
    심각합니다. 보증금을 돌려받지 못한 피해자들이 건강보험료까지
    체납하게 되는 상황인데, 이에 대한 대책이 있으신지요.

    ◯국무위원 박영희  위원님께서 지적하신 부분은 저희도 잘 인지하고
    있습니다. 현재 관계부처와 협의 중이며, 건강보험료 감면 방안을
    검토하고 있습니다.

    ◯홍길동 위원  검토가 아니라 지금 당장 필요한 건데요.
    구체적인 시행 시기를 말씀해 주시기 바랍니다.

    ◯국무위원 박영희  올해 하반기까지 구체적인 방안을 마련하겠습니다.
    """

    parser = UtteranceParser()
    utts = parser.parse_text(sample)
    qas = parser.extract_qa_pairs(utts)

    print(f"\n=== 파싱 결과 ===")
    for u in utts:
        proc = " [절차]" if u.is_procedural else ""
        print(f"  [{u.sequence_no}] {u.speaker_role} {u.speaker_name}{proc}: {u.raw_text[:60]}...")
        for ci, c in enumerate(u.clauses, 1):
            print(f"      clause {ci}: {c[:50]}...")

    print(f"\n=== Q/A 쌍 ===")
    for qa in qas:
        print(f"  Q: {qa.questioner_name} (발언 {qa.question_sequences})")
        print(f"  A: {qa.answerer_name} (발언 {qa.answer_sequences})")
