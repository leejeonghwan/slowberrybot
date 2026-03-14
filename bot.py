"""
텔레그램 봇 + 오케스트레이터
─────────────────────────────
텔레그램으로 명령 → Pi5가 작업 실행 → 결과를 텔레그램으로 보고

명령어:
  /status        - 시스템 상태 (DB 통계, 마지막 수집 시각 등)
  /discover      - API 엔드포인트 자동 탐색
  /collect [대상] - 데이터 수집 (meetings/bills/votes/members/all)
  /parse [회의ID] - 회의록 파싱 (ID 없으면 미처리분 배치)
  /tag_rule [N]   - 규칙 기반 태깅 배치 (N건, 없으면 전체)
  /tag_rule_stats - 규칙 태깅 통계 조회
  /tag_llm [N]    - LLM(Haiku) 태깅 배치 (N건, 기본 50)
  /aggregate [주] - Feature Store 집계 (주 미지정시 최근 4주)
  /detect [주]    - 신호 탐지
  /report [주]    - 주간 리포트 생성/전송
  /signals [N]    - 최근 상위 신호 N개 조회
  /content_fetch [N] - 회의록 본문 HTML 수집 (N건, 없으면 전체)
  /backfill [phase] - 22대 국회 전체 백필 (phase: 1~7, 없으면 전체)
  /backfill_status  - 백필 진행 상황 확인
  /pipeline       - 전체 파이프라인 실행 (수집→파싱→태깅→집계→탐지→리포트)
  /help           - 도움말
"""
import os
import json
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# 텔레그램 봇 라이브러리
try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

# 내부 모듈
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.settings import DB_PATH, LOG_DIR

logger = logging.getLogger(__name__)

# ── 환경 변수 ──
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ═══════════════════════════════════════
# 오케스트레이터: 각 파이프라인 단계 실행
# ═══════════════════════════════════════

class Orchestrator:
    """파이프라인 단계를 순차/병렬로 실행하고 결과를 반환"""

    def status(self) -> str:
        """시스템 상태 조회"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            stats = {}

            for table in ["meeting", "utterance", "clause", "clause_tag", "qa_pair",
                          "weekly_feature", "signal", "agenda", "member"]:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    stats[table] = row[0]
                except:
                    stats[table] = 0

            # 마지막 수집 시각
            last_collect = conn.execute(
                "SELECT MAX(collected_at) FROM collect_log"
            ).fetchone()[0] or "없음"

            # 마지막 신호
            last_signal = conn.execute(
                "SELECT MAX(detected_at) FROM signal"
            ).fetchone()[0] or "없음"

            conn.close()

            lines = [
                "📊 **시스템 상태**",
                "",
                f"회의: {stats['meeting']:,}건",
                f"발언: {stats['utterance']:,}건",
                f"절(clause): {stats['clause']:,}건",
                f"태그: {stats['clause_tag']:,}건",
                f"Q/A쌍: {stats['qa_pair']:,}건",
                f"안건: {stats['agenda']:,}건",
                f"의원: {stats['member']:,}건",
                f"주간 feature: {stats['weekly_feature']:,}건",
                f"탐지 신호: {stats['signal']:,}건",
                "",
                f"마지막 수집: {last_collect}",
                f"마지막 탐지: {last_signal}",
                f"DB 크기: {self._db_size()}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 상태 조회 실패: {e}"

    def _db_size(self) -> str:
        if DB_PATH.exists():
            size = DB_PATH.stat().st_size
            if size > 1024**3:
                return f"{size/1024**3:.1f} GB"
            elif size > 1024**2:
                return f"{size/1024**2:.1f} MB"
            else:
                return f"{size/1024:.1f} KB"
        return "0 KB"

    def discover(self) -> str:
        from collector.discover import run as discover_run
        result = discover_run()
        if result:
            lines = ["✅ **API 엔드포인트 탐색 완료**", ""]
            for cat, apis in result.items():
                names = [a["api_name"] for a in apis]
                lines.append(f"• {cat}: {', '.join(names)}")
            return "\n".join(lines)
        return "❌ API 탐색 실패. 인증키를 확인하세요."

    def backfill(self, start_phase: int = None, end_phase: int = None,
                 notify_fn=None) -> str:
        from collector.backfill import BackfillCollector
        collector = BackfillCollector(notify_fn=notify_fn)
        result = collector.run(start_phase=start_phase, end_phase=end_phase)
        collector.close()
        return result

    def backfill_status(self) -> str:
        from collector.backfill import BackfillCollector
        collector = BackfillCollector()
        status = collector.get_status()
        collector.close()
        return status

    def content_fetch(self, limit: int = 0, notify_fn=None) -> str:
        """회의록 본문 HTML 수집 (Phase 7)"""
        from collector.content_fetcher import ContentFetcher
        fetcher = ContentFetcher(notify_fn=notify_fn or (lambda m: None))
        stats = fetcher.fetch_batch(limit=limit, skip_existing=True)
        fetcher.close()
        return (
            f"📖 **회의록 본문 수집 완료**\n"
            f"수집: {stats['collected']:,}건 / "
            f"건너뜀: {stats['skipped']:,}건 / "
            f"오류: {stats['errors']:,}건"
        )

    def collect(self, targets: list[str] = None) -> str:
        from collector.fetch import run as collect_run
        results = collect_run(targets)
        lines = ["✅ **데이터 수집 완료**", ""]
        for key, count in results.items():
            lines.append(f"• {key}: {count:,}건")
        return "\n".join(lines)

    def parse(self, meeting_id: str = None) -> str:
        from parser.utterance_parser import MeetingProcessor
        proc = MeetingProcessor()

        if meeting_id:
            # 특정 회의 파싱
            conn = sqlite3.connect(str(DB_PATH))
            text = conn.execute(
                "SELECT raw_text_path FROM meeting WHERE meeting_id = ?",
                (meeting_id,)
            ).fetchone()
            conn.close()

            if text and text[0]:
                raw_path = Path(text[0])
                if raw_path.exists():
                    if str(raw_path).endswith(".json"):
                        stats = proc.process_meeting(meeting_id, json_path=str(raw_path))
                    else:
                        raw_text = raw_path.read_text(encoding="utf-8")
                        stats = proc.process_meeting(meeting_id, raw_text=raw_text)
                    proc.close()
                    return f"✅ 파싱 완료: {json.dumps(stats, ensure_ascii=False)}"
            proc.close()
            return f"❌ 회의 {meeting_id}의 원문을 찾을 수 없음"
        else:
            # 미처리분 배치 파싱
            conn = sqlite3.connect(str(DB_PATH))
            unprocessed = conn.execute("""
                SELECT m.meeting_id, m.raw_text_path
                FROM meeting m
                LEFT JOIN utterance u ON m.meeting_id = u.meeting_id
                WHERE u.utterance_id IS NULL AND m.raw_text_path IS NOT NULL
                LIMIT 20
            """).fetchall()
            conn.close()

            total = {"meetings": 0, "utterances": 0}
            for mid, rpath in unprocessed:
                if rpath and Path(rpath).exists():
                    if rpath.endswith(".json"):
                        stats = proc.process_meeting(mid, json_path=rpath)
                    else:
                        raw_text = Path(rpath).read_text(encoding="utf-8")
                        stats = proc.process_meeting(mid, raw_text=raw_text)
                    total["meetings"] += 1
                    total["utterances"] += stats.get("utterances", 0)

            proc.close()
            return f"✅ 배치 파싱 완료: {total['meetings']}개 회의, {total['utterances']}개 발언"

    def tag_rule(self, limit: int = 0, notify_fn=None) -> str:
        """규칙 기반 태깅 (limit=0이면 전체, Pi5 장기 실행용)"""
        from tagger.rule_tagger import RuleTagger
        tagger = RuleTagger(notify_fn=notify_fn or (lambda m: None))
        stats = tagger.tag_all_untagged(limit=limit)
        tagger.close()
        return (
            f"✅ **규칙 태깅 완료**\n"
            f"회의: {stats['meetings']:,}개 / "
            f"clause: {stats['clauses_tagged']:,}개 / "
            f"태그: {stats['tags_added']:,}개 / "
            f"오류: {stats.get('errors', 0):,}건"
        )

    def tag_rule_stats(self) -> str:
        """규칙 태깅 통계 조회"""
        from tagger.rule_tagger import RuleTagger
        tagger = RuleTagger()
        result = tagger.get_stats()
        tagger.close()
        return result

    def tag_llm(self, limit: int = 50) -> str:
        from tagger.llm_tagger import LLMTagger
        tagger = LLMTagger()
        stats = tagger.tag_batch(limit=limit)
        tagger.close()
        return f"✅ LLM 태깅 완료: {json.dumps(stats, ensure_ascii=False)}"

    def aggregate(self, year_week: str = None, notify_fn=None) -> str:
        """주간 feature 집계. year_week 없으면 전체 기간 집계."""
        from features.weekly_aggregator import WeeklyAggregator
        agg = WeeklyAggregator(notify_fn=notify_fn or (lambda m: None))

        if year_week:
            stats = agg.aggregate_week(year_week)
        else:
            stats = agg.aggregate_all()
        agg.close()
        return (
            f"✅ **Feature 집계 완료**\n"
            f"{stats.get('weeks', 0)}주 / feature {stats.get('rows_created', 0):,}개"
        )

    def detect(self, year_week: str = None) -> str:
        from detector.signal_detector import SignalDetector
        detector = SignalDetector()

        if not year_week:
            now = datetime.now()
            year_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

        signals = detector.detect_week(year_week)
        detector.close()

        if not signals:
            return f"📡 [{year_week}] 탐지된 신호 없음"

        lines = [f"📡 **[{year_week}] {len(signals)}개 신호 탐지**", ""]
        for s in signals[:5]:
            score = s["composite_score"]
            emoji = "🔴" if score > 0.5 else "🟡" if score > 0.3 else "🟢"
            lines.append(
                f"{emoji} {s['issue_id']} → {s['target_entity']} "
                f"({s['signal_type']}, {score:.3f})"
            )
        return "\n".join(lines)

    def report(self, year_week: str = None) -> str:
        from explainer.narrator import Narrator
        narrator = Narrator()

        if not year_week:
            now = datetime.now()
            year_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

        report = narrator.generate_weekly_report(year_week)
        narrator.close()
        return report

    def signals(self, limit: int = 10) -> str:
        from detector.signal_detector import SignalDetector
        detector = SignalDetector()
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("""
            SELECT year_week, signal_type, issue_id, target_entity, composite_score
            FROM signal ORDER BY composite_score DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        detector.close()

        if not rows:
            return "신호 없음"

        lines = ["📋 **상위 신호 목록**", ""]
        for yw, stype, issue, target, score in rows:
            emoji = "🔴" if score > 0.5 else "🟡" if score > 0.3 else "🟢"
            lines.append(f"{emoji} [{yw}] {issue} → {target} ({stype}, {score:.3f})")
        return "\n".join(lines)

    def full_pipeline(self) -> str:
        """전체 파이프라인 순차 실행"""
        results = []
        steps = [
            ("수집", lambda: self.collect()),
            ("파싱", lambda: self.parse()),
            ("규칙 태깅", lambda: self.tag_rule()),
            ("LLM 태깅", lambda: self.tag_llm(50)),
            ("Feature 집계", lambda: self.aggregate()),
            ("신호 탐지", lambda: self.detect()),
            ("리포트 생성", lambda: self.report()),
        ]

        for name, fn in steps:
            try:
                result = fn()
                results.append(f"✅ {name}: 완료")
                logger.info(f"[pipeline] {name}: {result[:100]}")
            except Exception as e:
                results.append(f"❌ {name}: {e}")
                logger.error(f"[pipeline] {name} 실패: {e}")

        return "\n".join(["🔄 **전체 파이프라인 완료**", ""] + results)


# ═══════════════════════════════════════
# 텔레그램 봇 핸들러
# ═══════════════════════════════════════

orch = Orchestrator()

if HAS_TELEGRAM:

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = orch.status()
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 API 탐색 중...")
        msg = orch.discover()
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """22대 국회 전체 백필. 진행 상황을 텔레그램으로 실시간 보고."""
        start = int(context.args[0]) if context.args else 1
        end = int(context.args[1]) if len(context.args) > 1 else 7
        await update.message.reply_text(
            f"🚀 22대 국회 백필 시작 (Phase {start}~{end})...\n"
            f"진행 상황을 여기로 보고합니다."
        )

        async def notify(msg):
            try:
                # 텔레그램 메시지 길이 제한
                if len(msg) > 4000:
                    msg = msg[:4000] + "..."
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(msg)

        # 동기 함수를 비동기로 실행 (봇 블로킹 방지)
        import functools
        loop = asyncio.get_event_loop()

        # notify를 동기 콜백으로 래핑
        pending_messages = []
        def sync_notify(msg):
            pending_messages.append(msg)

        result = await loop.run_in_executor(
            None,
            functools.partial(orch.backfill, start, end, sync_notify)
        )

        # 쌓인 메시지 전송
        for msg in pending_messages:
            await notify(msg)

        await notify(f"🏁 백필 완료!\n\n{orch.backfill_status()}")

    async def cmd_backfill_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = orch.backfill_status()
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_content_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """회의록 본문 HTML 수집 (Phase 7)"""
        limit = int(context.args[0]) if context.args else 0
        await update.message.reply_text(
            f"📖 회의록 본문 수집 시작 (limit={limit or '전체'})..."
        )

        pending_messages = []
        def sync_notify(msg):
            pending_messages.append(msg)

        import functools
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(orch.content_fetch, limit, sync_notify)
        )

        for msg in pending_messages:
            try:
                if len(msg) > 4000:
                    msg = msg[:4000] + "..."
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(msg)

        await update.message.reply_text(result, parse_mode="Markdown")

    async def cmd_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
        targets = context.args if context.args else None
        await update.message.reply_text(f"📥 수집 시작: {targets or 'all'}...")
        msg = orch.collect(targets)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_parse(update: Update, context: ContextTypes.DEFAULT_TYPE):
        meeting_id = context.args[0] if context.args else None
        await update.message.reply_text("📝 파싱 중...")
        msg = orch.parse(meeting_id)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_tag_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
        limit = int(context.args[0]) if context.args else 0
        await update.message.reply_text(
            f"🏷️ 규칙 태깅 시작 (limit={limit or '전체'})..."
        )

        pending_messages = []
        def sync_notify(msg):
            pending_messages.append(msg)

        import functools
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(orch.tag_rule, limit, sync_notify)
        )

        for msg in pending_messages:
            try:
                if len(msg) > 4000:
                    msg = msg[:4000] + "..."
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(msg)

        await update.message.reply_text(result, parse_mode="Markdown")

    async def cmd_tag_rule_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = orch.tag_rule_stats()
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_tag_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        limit = int(context.args[0]) if context.args else 50
        await update.message.reply_text(f"🤖 LLM 태깅 중 ({limit}건)...")
        msg = orch.tag_llm(limit)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_aggregate(update: Update, context: ContextTypes.DEFAULT_TYPE):
        week = context.args[0] if context.args else None
        await update.message.reply_text("📊 집계 중...")
        msg = orch.aggregate(week)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_detect(update: Update, context: ContextTypes.DEFAULT_TYPE):
        week = context.args[0] if context.args else None
        await update.message.reply_text("📡 탐지 중...")
        msg = orch.detect(week)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
        week = context.args[0] if context.args else None
        await update.message.reply_text("📰 리포트 생성 중...")
        msg = orch.report(week)
        # 텔레그램 메시지 길이 제한 (4096자)
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await update.message.reply_text(msg[i:i+4000], parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
        limit = int(context.args[0]) if context.args else 10
        msg = orch.signals(limit)
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔄 전체 파이프라인 시작... (시간이 걸립니다)")
        msg = orch.full_pipeline()
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """🏛️ **국회 회의록 신호 탐지 시스템**

📋 **명령어**
/status - 시스템 상태
/discover - API 엔드포인트 탐색
/collect [대상] - 데이터 수집
/parse [회의ID] - 회의록 파싱
/tag\\_rule [N] - 규칙 태깅 (N건, 없으면 전체)
/tag\\_rule\\_stats - 규칙 태깅 통계
/tag\\_llm [N] - LLM 태깅 (N건)
/aggregate [주] - Feature 집계
/detect [주] - 신호 탐지
/report [주] - 주간 리포트
/signals [N] - 상위 신호 조회
/content\\_fetch [N] - 회의록 본문 수집
/backfill [phase] - 22대 전체 백필
/backfill\\_status - 백필 진행 상황
/pipeline - 전체 파이프라인

📌 주간 형식: 2026-W11"""
        await update.message.reply_text(help_text, parse_mode="Markdown")


def run_bot():
    """텔레그램 봇 실행"""
    if not HAS_TELEGRAM:
        print("python-telegram-bot 패키지를 설치하세요: pip install python-telegram-bot")
        return

    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN 환경변수를 설정하세요.")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("discover", cmd_discover))
    app.add_handler(CommandHandler("backfill", cmd_backfill))
    app.add_handler(CommandHandler("backfill_status", cmd_backfill_status))
    app.add_handler(CommandHandler("content_fetch", cmd_content_fetch))
    app.add_handler(CommandHandler("collect", cmd_collect))
    app.add_handler(CommandHandler("parse", cmd_parse))
    app.add_handler(CommandHandler("tag_rule", cmd_tag_rule))
    app.add_handler(CommandHandler("tag_rule_stats", cmd_tag_rule_stats))
    app.add_handler(CommandHandler("tag_llm", cmd_tag_llm))
    app.add_handler(CommandHandler("aggregate", cmd_aggregate))
    app.add_handler(CommandHandler("detect", cmd_detect))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    logger.info("텔레그램 봇 시작")
    app.run_polling()


# ═══════════════════════════════════════
# CLI 모드 (텔레그램 없이 직접 실행)
# ═══════════════════════════════════════

def run_cli():
    """CLI 모드"""
    import sys
    if len(sys.argv) < 2:
        print("사용법: python bot.py <command> [args]")
        print("명령어: status, discover, backfill, backfill_status, collect, parse, tag_rule, tag_rule_stats, tag_llm, aggregate, detect, report, signals, pipeline")
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "status": lambda: orch.status(),
        "discover": lambda: orch.discover(),
        "backfill": lambda: orch.backfill(
            int(args[0]) if args else 1,
            int(args[1]) if len(args) > 1 else 7
        ),
        "backfill_status": lambda: orch.backfill_status(),
        "content_fetch": lambda: orch.content_fetch(
            int(args[0]) if args else 0
        ),
        "collect": lambda: orch.collect(args if args else None),
        "parse": lambda: orch.parse(args[0] if args else None),
        "tag_rule": lambda: orch.tag_rule(
            int(args[0]) if args else 0,
            lambda msg: print(msg)
        ),
        "tag_rule_stats": lambda: print(orch.tag_rule_stats()),
        "tag_llm": lambda: orch.tag_llm(int(args[0]) if args else 50),
        "aggregate": lambda: orch.aggregate(args[0] if args else None),
        "detect": lambda: orch.detect(args[0] if args else None),
        "report": lambda: orch.report(args[0] if args else None),
        "signals": lambda: orch.signals(int(args[0]) if args else 10),
        "pipeline": lambda: orch.full_pipeline(),
    }

    if cmd in commands:
        result = commands[cmd]()
        print(result)
    else:
        print(f"알 수 없는 명령: {cmd}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_DIR / "bot.log")),
        ]
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    import sys
    if len(sys.argv) > 1 and sys.argv[1] != "bot":
        run_cli()
    else:
        run_bot()
