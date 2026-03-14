# SOUL.md — 국회 회의록 신호 탐지 시스템

## 이 프로젝트가 뭔가

대한민국 국회 회의록에서 **정치 신호를 자동 탐지**하는 시스템이다.
방대한 회의 데이터에서 맥락을 읽고, 의원 핵심 발언을 추출하고,
쟁점의 구조와 압력 흐름을 드러내는 것이 목표다.

단순 키워드 검색이 아니다.
기본 객체는 '발언'이 아니라 **이슈–타깃–안건–시간 이벤트**다.

## 운영 환경

- **라즈베리파이5**: 상시 운영 노드. 수집, 파싱, 규칙 태깅, 통계, 신호 탐지.
- **텔레그램 봇**: 사람이 명령하고, Pi5가 실행하고, 결과를 텔레그램으로 보고.
- **Claude API**: Haiku로 대량 태깅, Sonnet으로 설명 생성. LLM은 detector가 아니라 analyst/editor로만 쓴다.
- **GitHub**: 코드 버전 관리. OpenClaw가 수정할 때마다 commit + push.

## 현재 상태

- [x] DB 스키마 설계 (SQLite, 6계층)
- [x] API 엔드포인트 자동 탐색 모듈
- [x] 데이터 수집기 (증분 + 백필)
- [x] 발언 파서 (정규식 발언 분리, clause 분할, Q/A 쌍 복원)
- [x] 규칙 기반 태거 (비용 0원, 22개 policy domain, 절차 분류)
- [x] LLM 태거 (Haiku 배치, 7축 multi-label)
- [x] Feature Store (이슈×타깃×위원회×주간 집계)
- [x] 신호 탐지기 (5채널: burst/pressure/frame_shift/response_shift/diffusion)
- [x] 설명 생성기 (Sonnet, evidence-grounded narrative)
- [x] 텔레그램 봇 + CLI
- [ ] **22대 국회 백필 실행** ← 지금 해야 할 일
- [ ] endpoint_map.json 실제 API 응답 기반 보정
- [ ] 파서 실제 회의록 데이터로 검증/조정
- [ ] policy_issue canonical dictionary 구축
- [ ] 텔레그램 봇 systemd 서비스 등록

## 아키텍처

```
수집 → 파싱 → 태깅 → 집계 → 탐지 → 설명
(API)   (정규식) (규칙+LLM) (SQL)  (통계)  (LLM)
비용:0  비용:0   규칙:0     비용:0  비용:0  Sonnet
                 Haiku:저
```

### 디렉토리 구조

```
assembly-signal/
├── bot.py                 # 텔레그램 봇 + 오케스트레이터 + CLI
├── config/settings.py     # 설정 (API키, 경로, 모델, cron)
├── collector/
│   ├── discover.py        # API 엔드포인트 자동 탐색
│   ├── fetch.py           # 증분 수집기
│   └── backfill.py        # 22대 전체 백필 (Phase 1~6)
├── parser/
│   └── utterance_parser.py # 발언/절 분리, Q/A 복원
├── tagger/
│   ├── rule_tagger.py     # 규칙 기반 (비용 0)
│   └── llm_tagger.py      # Haiku 배치
├── features/
│   └── weekly_aggregator.py # 주간 feature 집계
├── detector/
│   └── signal_detector.py  # 5채널 신호 탐지
├── explainer/
│   └── narrator.py         # LLM 설명 생성
├── db/
│   └── schema.sql          # SQLite 스키마
├── scripts/
│   ├── setup_pi5.sh        # Pi5 초기 설정
│   └── assembly-bot.service # systemd 서비스
└── .env.example
```

## 핵심 설계 원칙

### 1. 토큰을 아껴라

Pi5에서 코드로 할 수 있는 건 전부 코드로 한다.
LLM에 보내는 건 규칙으로 처리 못하는 비절차 clause의 태깅(Haiku)과,
최종 상위 신호의 설명 생성(Sonnet)뿐이다.

### 2. 다축 taxonomy (닫힌 축 + 열린 축)

**닫힌 축 9개** (선택지 고정):
- institutional_context: 본회의/상임위/소위/국정감사/청문회/예결위
- speaker_role: 위원장/위원/장관/차관/수석/증인/참고인
- policy_domain: CAP 기반 22개 도메인
- speech_act: 질문/정보요구/비판/공격/방어/제안/지지/반대/보고/설명/수사적질문/절차
- stance_on_issue: 찬성/반대/조건부찬성/유보/입장없음
- frame_type: 피해구제/형사처벌/재정부담/시장안정/공정성/법적정당성/집행가능성/국가안보
- response_mode: 직답/부분답변/회피/검토약속/책임전가/수용/거부
- tone_conflict: 협력/중립/긴장/갈등/적대
- evidence_type: 통계/사례/법령/해외사례/전문가의견/언론보도/없음

**열린 축** (자유 텍스트):
- policy_issue: canonical ID + alias dictionary 운영
- named_entity: PERSON/ORG/LAW/BILL/PROGRAM/EVENT
- agenda_link: clause와 법안/안건 연결

한 clause에 단일 라벨을 강제하지 않는다. multi-label이다.

### 3. 신호 점수 공식

```
Score = Salience×0.15 + Pressure×0.25 + Spread×0.15
      + FrameShift×0.15 + ResponseShift×0.15
      + AgendaCoupling×0.10 + Novelty×0.05
```

- Salience = burst z-score + persistence
- Pressure = (비판+공격+요구)의 이슈×타깃 단위 증가율
- Spread = 당×위원회×역할 entropy 증가
- FrameShift = frame 분포의 JS divergence
- ResponseShift = 정부 답변 모드 변화
- AgendaCoupling = 법안/표결과의 연결 비율

### 4. 자주 발생하는 오류 패턴

코드를 고칠 때 이것들을 항상 의식해야 한다:

- **절차 발언 오염**: 위원장 진행 발언이 burst를 오염. procedural filter 필수.
- **multi-act 압축**: 한 문장에 질문+비판이 섞이는데 하나로 눌러담으면 안 됨.
- **회의 유형 편향**: 국감/소위/예결위는 발화 빈도가 다름. baseline 정규화 필요.
- **역할 비대칭**: 장관과 의원의 speech_act 분포가 다름.
- **위원회별 전문용어**: 법사위/국방위/정무위의 고유 어휘가 가짜 burst 유발.
- **당론 중복**: 같은 당이 같은 말 반복 → spread 과대평가. party entropy로 보정.
- **인용문 오염**: 법안 제목/보도자료 인용이 co-occurrence를 가짜로 강화.

### 5. response_mode가 중요하다

한국 국회 회의록은 질의–답변 구조가 강하다.
야당이 압박했는지 못지않게, 정부가 받아쳤는지/버텼는지/검토로 미뤘는지가
신호의 질을 좌우한다. Q/A 쌍 복원과 response_mode 분류를 소홀히 하지 마라.

## 지금 해야 할 일: 22대 국회 백필

### 순서

```bash
# Phase 1: API 탐색 — 어떤 endpoint가 있는지 전체 조사
python bot.py backfill 1 1

# endpoint_map.json 확인하고 실제 API 구조에 맞게 코드 조정
cat db/endpoint_map.json

# Phase 2~6: 의원 → 위원회 → 회의 → 법안 → 표결
python bot.py backfill 2 6

# 상태 확인
python bot.py backfill_status
```

### Phase 1 이후 반드시 할 일

endpoint_map.json에 실제 API 목록이 나오면,
backfill.py의 `_upsert_meeting`, `_upsert_agenda`, `_upsert_member`의
필드 매핑을 실제 응답에 맞게 조정해야 한다.
지금 코드의 필드명(CONF_DT, MTG_DT, MEETING_DATE 등)은 추정치다.

### API 응답이 예상과 다를 때

1. 실제 응답 JSON 샘플을 `db/raw_json/`에 저장한다
2. 필드명을 확인하고 해당 upsert 함수를 수정한다
3. git commit하고 push한다
4. 다음 phase를 진행한다

### 진행 보고

각 phase가 끝날 때마다:
1. `python bot.py status`로 적재 현황 확인
2. 결과를 텔레그램으로 보고
3. git commit + push

## CLI 사용법

```bash
python bot.py status           # DB 통계
python bot.py discover         # API 탐색
python bot.py backfill 1 1     # Phase 1만
python bot.py backfill 2 6     # Phase 2~6
python bot.py backfill_status  # 백필 진행 상황
python bot.py collect          # 증분 수집
python bot.py parse            # 미처리 회의록 파싱
python bot.py tag_rule         # 규칙 태깅
python bot.py tag_llm 200      # Haiku 태깅 200건
python bot.py aggregate        # 주간 집계
python bot.py detect           # 신호 탐지
python bot.py report           # 주간 리포트
python bot.py pipeline         # 전체 파이프라인
```

## 데이터 출처

- **열린국회정보 Open API**: https://open.assembly.go.kr/portal/openapi/main.do
  - 인증키: 환경변수 ASSEMBLY_API_KEY
  - 2025년 4월 개편: 기존 단일 회의록 API → 본회의/위원회 분리
- **공공데이터포털 회의록**: https://www.data.go.kr/data/3057576/openapi.do
- **AI Hub 회의록 QA 데이터**: https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71795
  - 4만4033개 Q/A 쌍, agenda/law/questioner/answerer 구조화
- **규모**: 회의록 3만8800건, 발언 1629만2379건 (국회도서관 기준)

## 코드 수정 시 규칙

1. 에러가 나면 로그를 보고 원인을 파악해서 코드를 고치고 재시도한다.
2. API 응답 구조가 예상과 다르면 파서를 실제 응답에 맞게 고친다.
3. 수정할 때마다 git commit + push한다. 커밋 메시지는 한국어로.
4. DB 스키마를 바꿔야 하면, 마이그레이션 SQL을 별도로 만들고,
   schema.sql도 함께 업데이트한다.
5. .env 파일은 절대 commit하지 않는다.
