"""
Step 3-A: 규칙 기반 태거
- 비용 0원. Pi5 로컬에서 전부 처리
- institutional_context, speaker_role은 메타데이터에서 직접 추출
- policy_domain은 키워드 사전 매칭 (CAP 기반)
- speech_act 중 절차 발언은 규칙으로 분류
- named_entity는 정규식으로 1차 추출
"""
import re
import sqlite3
import json
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


# ── CAP 기반 policy_domain (닫힌 축) ──
# Comparative Agendas Project 한국 적용판
POLICY_DOMAINS = {
    "거시경제": ["경제성장", "물가", "금리", "환율", "GDP", "경기", "인플레이션", "디플레이션"],
    "시민권/자유": ["인권", "차별", "표현의자유", "집회", "시위", "개인정보", "프라이버시"],
    "보건의료": ["건강보험", "의료", "병원", "약가", "의약품", "감염병", "코로나", "공중보건", "의사", "간호사"],
    "농업/식품": ["농업", "농민", "식품안전", "축산", "수산", "어업", "쌀값"],
    "노동/고용": ["고용", "실업", "임금", "최저임금", "근로", "비정규직", "노동조합", "산재"],
    "교육": ["교육", "학교", "대학", "입시", "수능", "교원", "학생", "등록금", "교육과정"],
    "환경": ["환경", "기후", "탄소", "미세먼지", "폐기물", "재활용", "생태", "온실가스"],
    "에너지": ["에너지", "원전", "원자력", "신재생", "태양광", "풍력", "전기요금", "한전"],
    "이민": ["이민", "외국인", "다문화", "난민", "체류", "비자"],
    "교통": ["교통", "도로", "철도", "KTX", "GTX", "버스", "지하철", "항공"],
    "법과질서": ["범죄", "형법", "검찰", "경찰", "수사", "재판", "교도소", "마약", "사기"],
    "복지": ["복지", "연금", "기초생활", "출산", "육아", "보육", "저출생", "고령화", "장애인"],
    "주거": ["주택", "부동산", "전세", "월세", "아파트", "분양", "임대", "재건축", "재개발", "전세사기", "깡통전세"],
    "금융": ["금융", "은행", "주식", "증권", "보험", "대출", "가계부채", "암호화폐", "가상자산"],
    "국방": ["국방", "군사", "안보", "군대", "방위", "무기", "미사일", "북한", "핵"],
    "과학기술": ["과학", "기술", "연구개발", "R&D", "AI", "인공지능", "반도체", "데이터", "디지털"],
    "무역": ["무역", "수출", "수입", "관세", "FTA", "통상", "WTO"],
    "외교": ["외교", "동맹", "유엔", "UN", "정상회담", "대사", "조약"],
    "정부운영": ["공무원", "정부조직", "행정", "규제", "규제개혁", "민원"],
    "토지/수자원": ["토지", "국토", "댐", "수자원", "하천", "매립"],
    "문화/여가": ["문화", "예술", "체육", "스포츠", "관광", "콘텐츠", "게임", "방송", "언론"],
}

# policy_domain 키워드를 정규식으로 컴파일
DOMAIN_PATTERNS = {}
for domain, keywords in POLICY_DOMAINS.items():
    pattern = re.compile("|".join(re.escape(k) for k in keywords))
    DOMAIN_PATTERNS[domain] = pattern


# ── speech_act 절차 분류 규칙 ──
PROCEDURAL_ACTS = {
    "개의선포": re.compile(r'(개의|개회|속개)\s*(하겠습니다|합니다|선포)'),
    "산회선포": re.compile(r'(산회|폐회|정회)\s*(하겠습니다|합니다|선포)'),
    "안건상정": re.compile(r'(상정|회부|부의)합니다'),
    "표결공지": re.compile(r'(표결|투표|거수|기립)\s*(하여|해\s*주시)'),
    "의결선포": re.compile(r'(가결|부결|의결|통과)\s*(되었|됐|선포)'),
    "보고개시": re.compile(r'(제안설명|보고|심사보고|검토보고)\s*(듣|있|하겠)'),
    "자료요구": re.compile(r'(자료|서류)\s*(제출|제시|요구|요청)'),
    "질의개시": re.compile(r'(질의|질문)\s*(있으시|하시|없으시)'),
    "발언허가": re.compile(r'(발언|말씀)\s*(하시|해\s*주시|허가)'),
}

# ── 명명 개체(named entity) 패턴 ──
ENTITY_PATTERNS = {
    "LAW": re.compile(r'(?:「|「)([^」」]+)(?:」|」)'),  # 「법률명」
    "BILL": re.compile(r'([가-힣]+(?:법|안))\s*(일부)?개정(?:법률)?안'),
    "ORG": re.compile(
        r'(국토교통부|보건복지부|교육부|법무부|국방부|외교부|'
        r'기획재정부|산업통상자원부|환경부|고용노동부|여성가족부|'
        r'행정안전부|문화체육관광부|농림축산식품부|해양수산부|'
        r'중소벤처기업부|과학기술정보통신부|통일부|국가보훈부|'
        r'금융위원회|공정거래위원회|방송통신위원회|국민권익위원회|'
        r'감사원|국정원|국가정보원|검찰|경찰청|국세청|관세청|'
        r'한국은행|금융감독원|국민연금|건강보험공단)'
    ),
    "PROGRAM": re.compile(r'([가-힣]+(사업|정책|제도|프로그램|대책|방안|계획|로드맵))'),
    "PERSON": re.compile(r'([가-힣]{2,4})\s*(대통령|총리|장관|차관|위원장|의원|대표)'),
}


class RuleTagger:
    """규칙 기반 태거: 비용 0원, Pi5 로컬 처리"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))

    def tag_meeting(self, meeting_id: str) -> dict:
        """회의 내 모든 clause에 규칙 기반 태그 부여"""
        stats = {"clauses_tagged": 0, "tags_added": 0}

        # 회의 메타데이터에서 institutional_context
        meeting = self.conn.execute(
            "SELECT meeting_type, committee_id FROM meeting WHERE meeting_id=?",
            (meeting_id,)
        ).fetchone()
        if not meeting:
            return stats

        inst_context = meeting[0]  # 본회의/상임위/소위/국정감사/청문회/예결위

        # 해당 회의의 모든 clause 조회
        rows = self.conn.execute("""
            SELECT c.clause_id, c.text, u.speaker_role, u.speaker_name, u.is_procedural
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            WHERE u.meeting_id = ?
            ORDER BY u.sequence_no, c.sequence_no
        """, (meeting_id,)).fetchall()

        for clause_id, text, speaker_role, speaker_name, is_procedural in rows:
            tags = []

            # 1. institutional_context (회의 단위)
            tags.append(("institutional_context", inst_context, 1.0))

            # 2. speaker_role (발언 단위)
            if speaker_role:
                tags.append(("speaker_role", speaker_role, 1.0))

            # 3. policy_domain (clause 단위, multi-label)
            for domain, pattern in DOMAIN_PATTERNS.items():
                if pattern.search(text):
                    tags.append(("policy_domain", domain, 0.8))

            # 4. speech_act: 절차 발언 분류
            if is_procedural:
                act = "절차"
                for act_name, pat in PROCEDURAL_ACTS.items():
                    if pat.search(text):
                        act = act_name
                        break
                tags.append(("speech_act", act, 0.9))

            # 5. named_entity 추출
            for ent_type, pattern in ENTITY_PATTERNS.items():
                for m in pattern.finditer(text):
                    entity_text = m.group(1) if m.lastindex else m.group(0)
                    self._upsert_entity(clause_id, ent_type, entity_text)

            # 6. evidence_type 규칙 추출
            evidence = self._detect_evidence(text)
            if evidence:
                tags.append(("evidence_type", evidence, 0.7))

            # DB 적재
            for axis, value, confidence in tags:
                self.conn.execute("""
                    INSERT OR IGNORE INTO clause_tag
                        (clause_id, axis, value, confidence, tagger)
                    VALUES (?, ?, ?, ?, 'rule')
                """, (clause_id, axis, value, confidence))
                stats["tags_added"] += 1

            stats["clauses_tagged"] += 1

        self.conn.commit()
        logger.info(f"[{meeting_id}] 규칙 태깅: {stats}")
        return stats

    def _detect_evidence(self, text: str) -> str | None:
        """증거 유형 탐지"""
        if re.search(r'\d+[%퍼센트]|\d+[만억조]\s*원|\d+[명건개]', text):
            return "통계"
        if re.search(r'(사례|사건|피해자|피해사례)', text):
            return "사례"
        if re.search(r'(법\s*제\d+조|시행령|시행규칙|법률)', text):
            return "법령"
        if re.search(r'(외국|해외|미국|일본|유럽|독일|영국|OECD)', text):
            return "해외사례"
        if re.search(r'(전문가|교수|연구원|박사|연구)', text):
            return "전문가의견"
        if re.search(r'(보도|기사|언론|뉴스|신문)', text):
            return "언론보도"
        return None

    def _upsert_entity(self, clause_id: int, entity_type: str, entity_text: str):
        self.conn.execute("""
            INSERT OR IGNORE INTO clause_entity (clause_id, entity_type, entity_text)
            VALUES (?, ?, ?)
        """, (clause_id, entity_type, entity_text))

    def tag_all_untagged(self, limit: int = 100) -> dict:
        """아직 태깅 안 된 회의를 배치 처리"""
        meetings = self.conn.execute("""
            SELECT DISTINCT m.meeting_id
            FROM meeting m
            JOIN utterance u ON m.meeting_id = u.meeting_id
            LEFT JOIN clause c ON u.utterance_id = c.utterance_id
            LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id AND ct.tagger='rule'
            WHERE ct.tag_id IS NULL
            LIMIT ?
        """, (limit,)).fetchall()

        total_stats = {"meetings": 0, "clauses_tagged": 0, "tags_added": 0}
        for (meeting_id,) in meetings:
            stats = self.tag_meeting(meeting_id)
            total_stats["meetings"] += 1
            total_stats["clauses_tagged"] += stats["clauses_tagged"]
            total_stats["tags_added"] += stats["tags_added"]

        return total_stats

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tagger = RuleTagger()
    stats = tagger.tag_all_untagged()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    tagger.close()
