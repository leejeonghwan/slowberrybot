"""
Step 3-A: 규칙 기반 태거 (v2)
──────────────────────────────
- 비용 0원. Pi5 로컬에서 전부 처리
- institutional_context, speaker_role은 메타데이터에서 직접 추출
- policy_domain은 키워드 사전 매칭 (CAP 기반)
- speech_act: 절차+실질 발언 모두 분류 (v2 신규)
- tone_conflict: 갈등 수준 규칙 탐지 (v2 신규)
- named_entity는 정규식으로 1차 추출
- evidence_type: 통계/사례/법령/해외사례/전문가의견/언론보도

v2 개선사항:
- 배치 한도 제거 (limit=0이면 전체 처리)
- 비절차 speech_act 규칙 (질문/비판/제안/지지/반대/설명/보고/수사적질문)
- tone_conflict 규칙 (협력/중립/긴장/갈등/적대)
- notify_fn으로 텔레그램 진행 보고
- 회의 단위 커밋 (메모리 절약)
"""
import re
import sqlite3
import json
import logging
import time
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


# ── speech_act 실질 발언 분류 규칙 (v2 신규) ──
# 비절차 발언에 대해 speech_act를 부여
# 우선순위: 위에서부터 매칭 (첫 매칭 채택)
SUBSTANTIVE_ACTS = [
    # (act_name, pattern, confidence)
    ("수사적질문", re.compile(
        r'(아닙니까|아니겠습니까|않겠습니까|않습니까|'
        r'맞지\s*않습니까|되겠습니까|하겠습니까|'
        r'말이\s*됩니까|몰랐습니까|있겠습니까)'), 0.75),
    ("질문", re.compile(
        r'(입니까\?|습니까\?|인가요\?|인지요\?|나요\?|까요\?|'
        r'말씀해\s*주시|답변\s*(해|하여)\s*주시|'
        r'어떻게\s*(생각|보시|되어|됩니)|'
        r'알고\s*계시|파악하고\s*계시|'
        r'확인\s*(해|좀)|설명\s*(해|좀)|근거가\s*뭡니까)'), 0.7),
    ("비판", re.compile(
        r'(문제가\s*(있|많|심각|크)|심각하|부실하|부적절|'
        r'미흡하|실패|엉망|직무유기|무책임|'
        r'국민[이을]\s*(우롱|속이|기만)|말이\s*(안|않)\s*됩니다|'
        r'해명\s*(하시|해\s*주)|책임\s*(지셔야|져야|물어야)|'
        r'거짓|허위|왜곡|은폐|축소|눈속임|'
        r'방관|방치|뒷짐|외면|무시|묵인|묵살|'
        r'도대체|대체\s*왜|어떻게\s*이런)'), 0.7),
    ("공격", re.compile(
        r'(사퇴|파면|경질|문책|탄핵|해임|'
        r'구속|수사\s*(해야|하라|촉구)|처벌|엄벌|'
        r'거짓말|사기|범죄|비리|부패|'
        r'국민\s*앞에\s*사과|사죄|퇴진|물러나)'), 0.75),
    ("제안", re.compile(
        r'(제안|건의|요청|촉구|권고|개선\s*(방안|책|해야)|'
        r'해야\s*합니다|해\s*주셔야|마련\s*(해야|해\s*주)|'
        r'방안을|대책을|대안을|계획을\s*(세우|마련)|'
        r'검토\s*(해\s*주시|하시|부탁)|'
        r'필요\s*합니다|바랍니다|당부|부탁)'), 0.65),
    ("지지", re.compile(
        r'(찬성|동의|지지|환영|공감|긍정적|잘\s*하고|'
        r'좋은\s*(정책|방향|방안)|바람직|타당|적절|'
        r'동감|높이\s*평가|감사\s*드립니다)'), 0.65),
    ("반대", re.compile(
        r'(반대|불가|안\s*됩니다|할\s*수\s*없|'
        r'동의\s*(할\s*수\s*없|하기\s*어렵)|'
        r'수용\s*(할\s*수\s*없|하기\s*어렵|불가)|'
        r'철회|재고|보류|유보|중단)'), 0.7),
    ("설명", re.compile(
        r'(말씀\s*드리|보고\s*드리|설명\s*드리|'
        r'답변\s*드리|알려\s*드리|'
        r'현황을\s*보면|현재\s*상황|진행\s*상황|추진\s*경과)'), 0.6),
    ("방어", re.compile(
        r'(검토\s*(하겠|중입니|하고\s*있)|'
        r'노력\s*(하겠|하고\s*있)|개선\s*(하겠|하고\s*있)|'
        r'말씀하신\s*부분|지적하신\s*부분|'
        r'충분히\s*(이해|공감)|'
        r'조치\s*(하겠|하고)|확인\s*(하겠|해\s*보겠)|'
        r'관계\s*부처와\s*협의)'), 0.6),
    ("보고", re.compile(
        r'(보고\s*(드리|올리|하겠)|현황\s*보고|실적\s*보고|'
        r'추진\s*실적|업무\s*보고|경과\s*보고)'), 0.7),
]


# ── tone_conflict 규칙 (v2 신규) ──
# 갈등 수준 탐지: 키워드 기반 스코어링 → 범주 분류
TONE_HOSTILE_WORDS = re.compile(
    r'(사퇴|파면|탄핵|해임|퇴진|구속|처벌|엄벌|'
    r'거짓말|사기|범죄|비리|부패|은폐|왜곡|조작|'
    r'사죄|사과|국민\s*기만|국민\s*우롱|직무유기|'
    r'도대체|대체\s*뭘|어떻게\s*이런|말이\s*됩니까|'
    r'몰랐습니까|뻔뻔|후안무치|염치)'
)
TONE_TENSION_WORDS = re.compile(
    r'(문제가|심각|미흡|부실|실패|부적절|무책임|'
    r'해명|책임|우려|걱정|유감|반대|불가|중단|'
    r'비판|지적|질타|추궁|따지)'
)
TONE_COOPERATIVE_WORDS = re.compile(
    r'(감사|수고|협력|공감|동의|환영|합의|'
    r'좋은\s*말씀|잘\s*하고\s*계시|격려|응원|'
    r'함께|같이|협조|상생|소통)'
)


# ── PERSON 오탐 블랙리스트 ──
# "지역구 의원", "비례 대표" 등 사람 이름이 아닌 일반 명사
PERSON_BLACKLIST = {
    # 선거/지역 관련
    "지역구", "지역", "비례", "비례대표", "광역", "기초", "선거구",
    # 대명사/지시어
    "각각", "해당", "관련", "소관", "존경하는", "존경",
    "여당", "야당", "여야", "정부", "우리", "본인", "저희", "그것",
    "이것", "여기", "거기", "어디", "무엇", "그분", "이분", "어느",
    # 일반 명사
    "모든", "다른", "일부", "전체", "상임", "특별", "예결",
    "국회", "의회", "위원", "간사", "의원님", "선생님",
    "다수", "소수", "과반", "만장일치", "대다수", "일동",
    "오늘", "내일", "금일", "금번", "이번", "다음",
    # 부사/접속사 (entity로 잘못 잡히는 것)
    "지금", "현재", "최근", "우선", "다만", "아까",
    "그래서", "따라서", "그런데", "그러나", "하지만",
    "그리고", "또한", "아울러", "더불어", "특히",
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
    "PERSON": re.compile(
        r'(?<![가-힣])([가-힣]{2,4})\s*(대통령|총리|장관|차관|위원장|의원|대표)'
        r'(?!\s*(선거|비례|지역|광역|기초))'  # 오탐 방지
    ),
}


class RuleTagger:
    """규칙 기반 태거: 비용 0원, Pi5 로컬 처리"""

    def __init__(self, notify_fn=None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.notify = notify_fn or (lambda msg: logger.info(msg))

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

            # 4. speech_act
            if is_procedural:
                # 절차 발언: 세부 분류
                act = "절차"
                for act_name, pat in PROCEDURAL_ACTS.items():
                    if pat.search(text):
                        act = act_name
                        break
                tags.append(("speech_act", act, 0.9))
            else:
                # 실질 발언: 규칙 기반 분류 (v2)
                act_tagged = False
                for act_name, pat, conf in SUBSTANTIVE_ACTS:
                    if pat.search(text):
                        tags.append(("speech_act", act_name, conf))
                        act_tagged = True
                        break  # 첫 매칭만 (우선순위)
                # 매칭 안 되면 태그 안 붙임 → LLM 태거가 처리

            # 5. tone_conflict (v2 신규)
            tone = self._detect_tone(text)
            if tone:
                tags.append(("tone_conflict", tone[0], tone[1]))

            # 6. named_entity 추출
            for ent_type, pattern in ENTITY_PATTERNS.items():
                for m in pattern.finditer(text):
                    entity_text = m.group(1) if m.lastindex else m.group(0)
                    self._upsert_entity(clause_id, ent_type, entity_text)

            # 7. evidence_type 규칙 추출
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

        # 회의 단위 커밋 (Pi5 메모리 절약)
        self.conn.commit()
        logger.info(f"[{meeting_id}] 규칙 태깅: {stats}")
        return stats

    def _detect_tone(self, text: str) -> tuple | None:
        """
        tone_conflict 탐지: 키워드 스코어링 → 범주 분류
        Returns: (tone_value, confidence) or None
        """
        if len(text) < 10:
            return None

        hostile = len(TONE_HOSTILE_WORDS.findall(text))
        tension = len(TONE_TENSION_WORDS.findall(text))
        cooperative = len(TONE_COOPERATIVE_WORDS.findall(text))

        total = hostile + tension + cooperative
        if total == 0:
            return None  # 단서 없음 → LLM에게 위임

        # 적대 키워드가 2개 이상이면 적대
        if hostile >= 2:
            return ("적대", 0.7)
        # 적대 1개 + 긴장 있으면 갈등
        if hostile >= 1 and tension >= 1:
            return ("갈등", 0.65)
        # 적대 1개만이면 긴장
        if hostile >= 1:
            return ("긴장", 0.6)
        # 긴장 키워드 2개 이상이면 긴장
        if tension >= 2:
            return ("긴장", 0.6)
        # 긴장 1개 + 협력 없으면 긴장
        if tension >= 1 and cooperative == 0:
            return ("긴장", 0.55)
        # 협력 키워드가 우세하면 협력
        if cooperative >= 2 and tension == 0:
            return ("협력", 0.65)
        if cooperative >= 1 and tension == 0:
            return ("협력", 0.55)

        # 혼재: 중립
        if tension >= 1 and cooperative >= 1:
            return ("중립", 0.5)

        return None

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
        # PERSON 오탐 필터링
        if entity_type == "PERSON" and entity_text in PERSON_BLACKLIST:
            return
        # 1글자 entity 무시
        if len(entity_text.strip()) <= 1:
            return
        self.conn.execute("""
            INSERT OR IGNORE INTO clause_entity (clause_id, entity_type, entity_text)
            VALUES (?, ?, ?)
        """, (clause_id, entity_type, entity_text))

    def tag_all_untagged(self, limit: int = 0) -> dict:
        """
        아직 태깅 안 된 회의를 배치 처리.
        limit: 최대 회의 수 (0=전체, Pi5 장기 실행용)
        """
        query = """
            SELECT DISTINCT m.meeting_id
            FROM meeting m
            JOIN utterance u ON m.meeting_id = u.meeting_id
            LEFT JOIN clause c ON u.utterance_id = c.utterance_id
            LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id AND ct.tagger='rule'
            WHERE ct.tag_id IS NULL
        """
        if limit > 0:
            query += f" LIMIT {limit}"

        meetings = self.conn.execute(query).fetchall()

        total_count = len(meetings)
        self.notify(
            f"🏷️ **규칙 태깅 시작**\n"
            f"대상: {total_count:,}개 회의"
        )

        total_stats = {"meetings": 0, "clauses_tagged": 0, "tags_added": 0}
        errors = 0
        start_time = time.time()

        for i, (meeting_id,) in enumerate(meetings, 1):
            try:
                stats = self.tag_meeting(meeting_id)
                total_stats["meetings"] += 1
                total_stats["clauses_tagged"] += stats["clauses_tagged"]
                total_stats["tags_added"] += stats["tags_added"]
            except Exception as e:
                errors += 1
                logger.error(f"[{meeting_id}] 태깅 실패: {e}")

            # 10건마다 진행 보고
            if i % 10 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total_count - i) / rate if rate > 0 else 0
                self.notify(
                    f"🏷️ 진행: {i}/{total_count} "
                    f"({i/total_count*100:.0f}%) "
                    f"태그 {total_stats['tags_added']:,}개 "
                    f"[{rate:.1f}회의/초, 남은 시간 ~{eta/60:.0f}분]"
                )

        elapsed = time.time() - start_time
        self.notify(
            f"✅ **규칙 태깅 완료** ({elapsed/60:.1f}분 소요)\n"
            f"회의: {total_stats['meetings']:,}개 / "
            f"clause: {total_stats['clauses_tagged']:,}개 / "
            f"태그: {total_stats['tags_added']:,}개 / "
            f"오류: {errors:,}건"
        )

        total_stats["errors"] = errors
        return total_stats

    def get_stats(self) -> str:
        """현재 태깅 통계"""
        rows = self.conn.execute("""
            SELECT axis, COUNT(*), COUNT(DISTINCT clause_id)
            FROM clause_tag WHERE tagger='rule'
            GROUP BY axis ORDER BY COUNT(*) DESC
        """).fetchall()

        total_clauses = self.conn.execute(
            "SELECT COUNT(*) FROM clause"
        ).fetchone()[0]
        tagged_clauses = self.conn.execute(
            "SELECT COUNT(DISTINCT clause_id) FROM clause_tag WHERE tagger='rule'"
        ).fetchone()[0]

        lines = [
            f"🏷️ **규칙 태깅 통계**",
            f"전체 clause: {total_clauses:,}",
            f"태깅된 clause: {tagged_clauses:,} ({tagged_clauses/max(total_clauses,1)*100:.1f}%)",
            "",
        ]
        for axis, tag_count, clause_count in rows:
            lines.append(f"  {axis}: {tag_count:,}개 태그 ({clause_count:,} clauses)")

        # 축별 상위 값 미리보기
        for axis in ["policy_domain", "speech_act", "tone_conflict"]:
            top = self.conn.execute("""
                SELECT value, COUNT(*) as cnt FROM clause_tag
                WHERE tagger='rule' AND axis=?
                GROUP BY value ORDER BY cnt DESC LIMIT 5
            """, (axis,)).fetchall()
            if top:
                lines.append(f"\n  [{axis} 상위]")
                for val, cnt in top:
                    lines.append(f"    {val}: {cnt:,}")

        return "\n".join(lines)

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="규칙 기반 태거")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "stats", "meeting"],
                        help="실행 모드: run(배치), stats(통계), meeting(단일)")
    parser.add_argument("--limit", type=int, default=0,
                        help="배치 최대 회의 수 (0=전체)")
    parser.add_argument("--meeting-id", type=str,
                        help="단일 회의 태깅 대상 ID")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    tagger = RuleTagger(notify_fn=lambda msg: print(msg))

    if args.command == "stats":
        print(tagger.get_stats())
    elif args.command == "meeting" and args.meeting_id:
        stats = tagger.tag_meeting(args.meeting_id)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        stats = tagger.tag_all_untagged(limit=args.limit)
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    tagger.close()
