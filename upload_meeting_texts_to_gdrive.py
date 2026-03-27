#!/usr/bin/env python3
"""
국회 회의록 원문(JSON) → Google Drive 업로드
사용: python3 upload_meeting_texts_to_gdrive.py [--limit 100]
"""

import sqlite3
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "db" / "assembly.db"


def upload_meeting_texts(limit: int = None):
    """회의록 원문 파일 → Google Drive 업로드"""
    
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    
    query = "SELECT meeting_id, meeting_date, raw_text_path FROM meeting WHERE raw_text_path IS NOT NULL ORDER BY meeting_date DESC"
    
    if limit:
        query += f" LIMIT {limit}"
    
    meetings = db.execute(query).fetchall()
    db.close()
    
    print(f"🏛️ **국회 회의록 원문 → Google Drive 업로드**")
    print(f"📋 회의: {len(meetings)}건")
    print()
    
    uploaded = 0
    failed = 0
    
    for i, m in enumerate(meetings, 1):
        meeting_id = m['meeting_id']
        meeting_date = m['meeting_date']
        raw_path = m['raw_text_path']
        
        p = Path(raw_path)
        
        if not p.exists():
            print(f"⏳ {i}/{len(meetings)} {meeting_id} ({meeting_date}) - ⚠️ 파일 없음")
            failed += 1
            continue
        
        # Google Drive 폴더 구성: gdrive:국회회의록/22대/YYYY/MM/
        year = meeting_date[:4]
        month = meeting_date[5:7]
        gdrive_dir = f"gdrive:국회회의록/22대/{year}/{month}/"
        
        # rclone으로 업로드
        try:
            cmd = ["rclone", "copy", str(p), gdrive_dir, "-v"]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            
            if result.returncode == 0:
                size_kb = p.stat().st_size / 1024
                print(f"✅ {i}/{len(meetings)} {meeting_id} ({meeting_date}) - {size_kb:.1f}KB")
                uploaded += 1
            else:
                print(f"❌ {i}/{len(meetings)} {meeting_id} - 업로드 실패")
                failed += 1
        
        except Exception as e:
            print(f"❌ {i}/{len(meetings)} {meeting_id} - {e}")
            failed += 1
    
    print()
    print(f"📊 **결과**")
    print(f"  ✅ 업로드: {uploaded}건")
    print(f"  ❌ 실패: {failed}건")
    print(f"  📂 Google Drive: gdrive:국회회의록/22대/")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="제한 (기본: 모두)")
    args = parser.parse_args()
    
    upload_meeting_texts(limit=args.limit)
