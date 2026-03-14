#!/usr/bin/env python3
"""
assembly.db 백업 스크립트
─────────────────────────
1) SQLite online backup → db/backup/assembly_YYYY-MM-DD.db
2) (선택) rclone으로 Google Drive 업로드
3) 오래된 로컬 백업 자동 정리

사용법:
  python scripts/backup_db.py                  # 로컬 백업만
  python scripts/backup_db.py --gdrive         # 로컬 + Google Drive
  python scripts/backup_db.py --keep 7         # 최근 7일만 보관 (기본 14일)
  python scripts/backup_db.py --gdrive --notify  # 텔레그램 알림 포함

crontab 예시 (매일 새벽 4시):
  0 4 * * * cd /home/pi/slowberrybot && python scripts/backup_db.py --gdrive --notify >> logs/backup.log 2>&1

Pi5 초기 설정 (한 번만):
  1) sudo apt install rclone
  2) rclone config  →  New remote → name: gdrive → type: drive → 인증
  3) rclone mkdir gdrive:slowberrybot-backup
"""
import sqlite3
import shutil
import subprocess
import sys
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

# 프로젝트 루트 기준 경로 설정
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
DB_PATH = BASE_DIR / "db" / "assembly.db"
BACKUP_DIR = BASE_DIR / "db" / "backup"
LOG_DIR = BASE_DIR / "logs"

# Google Drive rclone 리모트명
GDRIVE_REMOTE = "gdrive:slowberrybot-backup"

logger = logging.getLogger("backup")


def backup_local(db_path: Path, backup_dir: Path) -> Path | None:
    """
    SQLite online backup API로 안전한 백업 생성.
    WAL 모드에서도 일관된 스냅샷을 만든다.
    """
    if not db_path.exists():
        logger.error(f"DB 파일 없음: {db_path}")
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    backup_path = backup_dir / f"assembly_{today}.db"

    # 이미 오늘 백업이 있으면 덮어쓰기
    try:
        # SQLite online backup (WAL 안전)
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()

        size_mb = backup_path.stat().st_size / (1024 * 1024)
        logger.info(f"✅ 로컬 백업 완료: {backup_path.name} ({size_mb:.1f} MB)")
        return backup_path

    except Exception as e:
        logger.error(f"❌ 백업 실패: {e}")
        return None


def upload_gdrive(backup_path: Path) -> bool:
    """rclone으로 Google Drive에 업로드"""
    # rclone 설치 확인
    if not shutil.which("rclone"):
        logger.warning("⚠️ rclone 미설치. Google Drive 업로드 건너뜀.")
        logger.info("  설치: sudo apt install rclone && rclone config")
        return False

    try:
        result = subprocess.run(
            ["rclone", "copy", str(backup_path), GDRIVE_REMOTE,
             "--progress", "--transfers", "1"],
            capture_output=True, text=True, timeout=600
        )

        if result.returncode == 0:
            logger.info(f"☁️ Google Drive 업로드 완료: {backup_path.name}")
            return True
        else:
            logger.error(f"❌ rclone 오류: {result.stderr[:300]}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("❌ rclone 타임아웃 (10분 초과)")
        return False
    except Exception as e:
        logger.error(f"❌ 업로드 실패: {e}")
        return False


def cleanup_old(backup_dir: Path, keep_days: int = 14):
    """오래된 로컬 백업 파일 삭제"""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0

    for f in sorted(backup_dir.glob("assembly_*.db")):
        # 파일명에서 날짜 추출: assembly_2026-03-14.db
        try:
            date_str = f.stem.replace("assembly_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                removed += 1
                logger.info(f"🗑️ 삭제: {f.name}")
        except ValueError:
            continue

    if removed:
        logger.info(f"🗑️ {removed}개 오래된 백업 삭제 (보관: {keep_days}일)")


def notify_telegram(message: str):
    """텔레그램으로 백업 결과 알림 (선택)"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        return

    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        logger.warning(f"텔레그램 알림 실패: {e}")


def get_db_stats(db_path: Path) -> str:
    """백업 후 DB 요약 통계"""
    try:
        conn = sqlite3.connect(str(db_path))
        stats = {}
        for table in ["meeting", "utterance", "clause", "clause_tag"]:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = row[0]
            except:
                stats[table] = 0
        conn.close()

        return (
            f"회의 {stats['meeting']:,} / "
            f"발언 {stats['utterance']:,} / "
            f"clause {stats['clause']:,} / "
            f"태그 {stats['clause_tag']:,}"
        )
    except:
        return "통계 조회 실패"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="assembly.db 백업")
    parser.add_argument("--gdrive", action="store_true",
                        help="Google Drive에도 업로드")
    parser.add_argument("--keep", type=int, default=14,
                        help="로컬 백업 보관 일수 (기본 14)")
    parser.add_argument("--notify", action="store_true",
                        help="텔레그램으로 결과 알림")
    args = parser.parse_args()

    # 로깅 설정
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_DIR / "backup.log")),
        ]
    )

    logger.info("=" * 50)
    logger.info("🔄 백업 시작")

    # 1) 로컬 백업
    backup_path = backup_local(DB_PATH, BACKUP_DIR)
    if not backup_path:
        msg = "❌ DB 백업 실패"
        logger.error(msg)
        if args.notify:
            notify_telegram(msg)
        sys.exit(1)

    # 2) DB 통계
    db_stats = get_db_stats(backup_path)
    size_mb = backup_path.stat().st_size / (1024 * 1024)

    # 3) Google Drive 업로드
    gdrive_ok = False
    if args.gdrive:
        gdrive_ok = upload_gdrive(backup_path)

    # 4) 오래된 백업 정리
    cleanup_old(BACKUP_DIR, keep_days=args.keep)

    # 5) 결과 정리
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg_lines = [
        f"💾 **DB 백업 완료** ({today})",
        f"파일: {backup_path.name} ({size_mb:.1f} MB)",
        f"DB: {db_stats}",
    ]
    if args.gdrive:
        msg_lines.append(f"☁️ Google Drive: {'✅' if gdrive_ok else '❌'}")

    msg = "\n".join(msg_lines)
    logger.info(msg)

    if args.notify:
        notify_telegram(msg)

    # 현재 백업 목록
    backups = sorted(BACKUP_DIR.glob("assembly_*.db"))
    logger.info(f"📦 보관 중인 백업: {len(backups)}개")
    for b in backups[-5:]:  # 최근 5개만 표시
        logger.info(f"  {b.name} ({b.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
