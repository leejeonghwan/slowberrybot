-- ================================================================
-- 국회 회의록 신호 탐지 시스템 - DB 스키마
-- 설계 원칙: 발언이 아니라 이슈-타깃-안건-시간 이벤트가 기본 객체
-- ================================================================

PRAGMA journal_mode=WAL;          -- Pi5 SD카드 쓰기 최적화
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;         -- 64MB 캐시

-- ────────────────────────────────────────
-- Layer 0: 원시 수집 데이터
-- ────────────────────────────────────────

-- 수집 이력 관리 (증분 수집용)
CREATE TABLE IF NOT EXISTS collect_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT NOT NULL,
    params_json TEXT,
    page_index  INTEGER,
    row_count   INTEGER,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    checksum    TEXT          -- 중복 방지용
);

-- ────────────────────────────────────────
-- Layer 1: 정규화된 엔터티
-- ────────────────────────────────────────

-- 대수 (회기)
CREATE TABLE IF NOT EXISTS assembly (
    assembly_id   INTEGER PRIMARY KEY,  -- 예: 22
    start_date    DATE,
    end_date      DATE
);

-- 위원회
CREATE TABLE IF NOT EXISTS committee (
    committee_id    TEXT PRIMARY KEY,     -- API 제공 코드
    committee_name  TEXT NOT NULL,
    committee_type  TEXT,                 -- 상임위/특별위/소위/예결위
    parent_id       TEXT REFERENCES committee(committee_id)
);

-- 의원
CREATE TABLE IF NOT EXISTS member (
    member_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    party           TEXT,
    party_status    TEXT,                -- 여당/야당/무소속
    district        TEXT,
    elected_count   INTEGER,
    committees_json TEXT                 -- 소속 위원회 목록
);

-- 정부측 인사 (장관/차관/수석 등)
CREATE TABLE IF NOT EXISTS government_official (
    official_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    title           TEXT,                -- 장관/차관/국장 등
    ministry        TEXT,
    period_start    DATE,
    period_end      DATE
);

-- 안건/법안
CREATE TABLE IF NOT EXISTS agenda (
    agenda_id       TEXT PRIMARY KEY,     -- 의안번호 등
    agenda_type     TEXT,                 -- 법안/예산/청원/감사사안/기타
    title           TEXT NOT NULL,
    proposer        TEXT,
    propose_date    DATE,
    committee_id    TEXT REFERENCES committee(committee_id),
    status          TEXT,                 -- 계류/가결/부결/철회/대안반영
    vote_date       DATE,
    vote_yes        INTEGER,
    vote_no         INTEGER,
    vote_abstain    INTEGER
);

-- ────────────────────────────────────────
-- Layer 2: 회의 → 발언 → 절(clause) 계층
-- ────────────────────────────────────────

-- 회의
CREATE TABLE IF NOT EXISTS meeting (
    meeting_id      TEXT PRIMARY KEY,
    assembly_id     INTEGER REFERENCES assembly(assembly_id),
    committee_id    TEXT REFERENCES committee(committee_id),
    meeting_type    TEXT NOT NULL,        -- 본회의/상임위/소위/국정감사/청문회/예결위
    meeting_date    DATE NOT NULL,
    meeting_nth     INTEGER,             -- 제N차
    agenda_ids_json TEXT,                -- 관련 안건 목록
    raw_text_path   TEXT,                -- 원문 파일 경로
    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meeting_date ON meeting(meeting_date);
CREATE INDEX IF NOT EXISTS idx_meeting_type ON meeting(meeting_type);
CREATE INDEX IF NOT EXISTS idx_meeting_committee ON meeting(committee_id);

-- 발언 (utterance)
CREATE TABLE IF NOT EXISTS utterance (
    utterance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      TEXT NOT NULL REFERENCES meeting(meeting_id),
    sequence_no     INTEGER NOT NULL,    -- 회의 내 발언 순서
    speaker_name    TEXT NOT NULL,
    speaker_id      TEXT,                -- member_id 또는 official_id
    speaker_role    TEXT,                -- 위원장/위원/장관/차관/증인/참고인
    speaker_party   TEXT,
    raw_text        TEXT NOT NULL,
    char_count      INTEGER,
    is_procedural   BOOLEAN DEFAULT 0,   -- 절차 발언 여부
    qa_pair_id      INTEGER,             -- 질의-답변 쌍 ID
    qa_role         TEXT,                -- questioner/answerer
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_utt_meeting ON utterance(meeting_id);
CREATE INDEX IF NOT EXISTS idx_utt_speaker ON utterance(speaker_id);
CREATE INDEX IF NOT EXISTS idx_utt_procedural ON utterance(is_procedural);

-- 절 (clause) - 발언 내 의미 단위
CREATE TABLE IF NOT EXISTS clause (
    clause_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id    INTEGER NOT NULL REFERENCES utterance(utterance_id),
    sequence_no     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    char_count      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_clause_utt ON clause(utterance_id);

-- ────────────────────────────────────────
-- Layer 3: 다축 태깅 (clause 단위, multi-label)
-- ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS clause_tag (
    tag_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clause_id       INTEGER NOT NULL REFERENCES clause(clause_id),
    axis            TEXT NOT NULL,        -- 축 이름
    value           TEXT NOT NULL,        -- 태그 값
    confidence      REAL DEFAULT 1.0,
    tagger          TEXT,                -- rule/haiku/manual
    tagged_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tag_clause ON clause_tag(clause_id);
CREATE INDEX IF NOT EXISTS idx_tag_axis_value ON clause_tag(axis, value);

-- 닫힌 축 참조 테이블
-- axis: institutional_context → 본회의/상임위/소위/국정감사/청문회/예결위
-- axis: speaker_role → 위원장/위원/장관/차관/수석/증인/참고인
-- axis: policy_domain → (CAP 기반 closed set)
-- axis: speech_act → 질문/정보요구/비판/공격/방어/제안/지지/반대/보고/설명/수사적질문/절차
-- axis: stance_on_issue → 찬성/반대/조건부찬성/유보/입장없음
-- axis: frame_type → 피해구제/형사처벌/재정부담/시장안정/공정성/법적정당성/집행가능성/국가안보
-- axis: response_mode → 직답/부분답변/회피/검토약속/책임전가/수용/거부
-- axis: tone_conflict → 협력/중립/긴장/갈등/적대
-- axis: evidence_type → 통계/사례/법령/해외사례/전문가의견/언론보도/없음

-- 열린 축: policy_issue, named_entity, agenda_link
CREATE TABLE IF NOT EXISTS policy_issue (
    issue_id        TEXT PRIMARY KEY,     -- canonical ID
    display_name    TEXT NOT NULL,
    domain          TEXT,                 -- policy_domain FK
    aliases_json    TEXT                  -- ["전세사기","깡통전세","보증금미반환"]
);

CREATE TABLE IF NOT EXISTS clause_issue (
    clause_id       INTEGER REFERENCES clause(clause_id),
    issue_id        TEXT REFERENCES policy_issue(issue_id),
    PRIMARY KEY (clause_id, issue_id)
);

CREATE TABLE IF NOT EXISTS clause_entity (
    clause_id       INTEGER REFERENCES clause(clause_id),
    entity_type     TEXT,                 -- PERSON/ORG/LAW/BILL/PROGRAM/EVENT
    entity_text     TEXT,
    entity_id       TEXT,                 -- canonical ID (있으면)
    PRIMARY KEY (clause_id, entity_type, entity_text)
);

CREATE TABLE IF NOT EXISTS clause_agenda (
    clause_id       INTEGER REFERENCES clause(clause_id),
    agenda_id       TEXT REFERENCES agenda(agenda_id),
    link_type       TEXT,                 -- 직접언급/암시/질의대상/답변대상
    PRIMARY KEY (clause_id, agenda_id)
);

-- ────────────────────────────────────────
-- Layer 4: Q/A 쌍
-- ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS qa_pair (
    qa_pair_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      TEXT REFERENCES meeting(meeting_id),
    questioner_id   TEXT,
    answerer_id     TEXT,
    question_utt_ids TEXT,               -- JSON array
    answer_utt_ids   TEXT,               -- JSON array
    agenda_id       TEXT REFERENCES agenda(agenda_id),
    response_mode   TEXT,                -- 직답/부분답변/회피/검토약속/책임전가/수용/거부
    pressure_score  REAL                 -- 압박 강도 (0-1)
);

-- ────────────────────────────────────────
-- Layer 5: 주간 Feature Store
-- ────────────────────────────────────────

-- 이슈 × 타깃 × 위원회 × 주간 집계
CREATE TABLE IF NOT EXISTS weekly_feature (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year_week       TEXT NOT NULL,        -- "2026-W11"
    issue_id        TEXT,
    target_entity   TEXT,                -- 타깃 (부처/인물/기관)
    committee_id    TEXT,
    -- 빈도
    mention_count   INTEGER DEFAULT 0,
    speaker_count   INTEGER DEFAULT 0,
    party_count     INTEGER DEFAULT 0,
    -- 행위 분포
    act_question    INTEGER DEFAULT 0,
    act_critique    INTEGER DEFAULT 0,
    act_attack      INTEGER DEFAULT 0,
    act_defend      INTEGER DEFAULT 0,
    act_propose     INTEGER DEFAULT 0,
    act_support     INTEGER DEFAULT 0,
    -- 신호 feature
    pressure_score  REAL DEFAULT 0,      -- 압박 강도
    spread_entropy  REAL DEFAULT 0,      -- 당×위원회×역할 entropy
    frame_dist_json TEXT,                -- frame_type 분포
    response_dist_json TEXT,             -- response_mode 분포
    agenda_coupling REAL DEFAULT 0,      -- 안건/법안 연결 비율
    UNIQUE(year_week, issue_id, target_entity, committee_id)
);

CREATE INDEX IF NOT EXISTS idx_wf_week ON weekly_feature(year_week);
CREATE INDEX IF NOT EXISTS idx_wf_issue ON weekly_feature(issue_id);

-- ────────────────────────────────────────
-- Layer 6: 신호 탐지 결과
-- ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signal (
    signal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    year_week       TEXT NOT NULL,
    signal_type     TEXT NOT NULL,        -- burst/pressure_growth/frame_shift/response_shift/diffusion
    issue_id        TEXT,
    target_entity   TEXT,
    -- 점수
    salience        REAL,                -- burst + persistence
    pressure        REAL,                -- targeted pressure growth
    spread          REAL,                -- institutional spread entropy
    frame_shift     REAL,                -- frame distribution divergence
    response_shift  REAL,                -- response mode change
    agenda_coupling REAL,
    novelty         REAL,
    composite_score REAL,                -- 종합 점수
    -- 증거
    evidence_json   TEXT,                -- evidence packet
    explanation     TEXT,                -- LLM 생성 설명
    status          TEXT DEFAULT 'candidate'  -- candidate/confirmed/dismissed
);

CREATE INDEX IF NOT EXISTS idx_signal_week ON signal(year_week);
CREATE INDEX IF NOT EXISTS idx_signal_score ON signal(composite_score);
