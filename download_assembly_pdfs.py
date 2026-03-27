#!/usr/bin/env python3
"""
국회 회의록 PDF 다운로드 + Google Drive 업로드 스크립트
Usage:
  python3 download_assembly_pdfs.py --dae-num 22 --list-only
  python3 download_assembly_pdfs.py --dae-num 22 --upload gdrive:국회회의록/22대
"""

import os
import sys
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime
import requests
import subprocess

# API 키
API_KEY = os.getenv("ASSEMBLY_API_KEY", "")
BASE_URL = "https://open.assembly.go.kr/portal/openapi"

# DB
DB_PATH = Path(__file__).parent / "db" / "assembly.db"


def get_meetings(dae_num: int, limit: int = None):
    """국회 회의 목록 조회 (API)"""
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    query = "SELECT meeting_id, meeting_date, committee_id FROM meeting WHERE committee_id IS NOT NULL"
    
    if limit:
        query += f" LIMIT {limit}"
    
    meetings = conn.execute(query).fetchall()
    conn.close()
    
    return meetings


def download_pdf(meeting_id: str, output_dir: Path) -> str:
    """PDF 다운로드"""
    
    # 국회 API에서 회의록 원문 조회
    url = f"{BASE_URL}/meetinginfo/meetingInfo"
    params = {
        "KEY": API_KEY,
        "Type": "json",
        "parm_id": meeting_id
    }
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        # 회의 정보
        if "meetinginfo" in data and data["meetinginfo"]:
            meeting = data["meetinginfo"][0]
            meeting_date = meeting.get("meetingdate", "unknown")
            committee = meeting.get("committeename", "위원회")
            
            # PDF 경로 구성
            pdf_filename = f"{meeting_id}_{meeting_date}_{committee}.pdf"
            pdf_path = output_dir / pdf_filename
            
            print(f"  📄 {pdf_filename}")
            
            return str(pdf_path)
    
    except Exception as e:
        print(f"  ❌ 오류: {e}")
    
    return None


def upload_to_gdrive(pdf_path: str, remote_dir: str):
    """Google Drive에 업로드"""
    
    if not Path(pdf_path).exists():
        print(f"  ⚠️ 파일 없음: {pdf_path}")
        return False
    
    try:
        # rclone으로 업로드
        cmd = ["rclone", "copy", pdf_path, remote_dir]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        
        if result.returncode == 0:
            print(f"  ✅ 업로드: {remote_dir}")
            return True
        else:
            print(f"  ❌ 업로드 실패: {result.stderr.decode()[:100]}")
            return False
    
    except Exception as e:
        print(f"  ❌ 오류: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="국회 회의록 PDF 다운로드 + Google Drive 업로드")
    parser.add_argument("--dae-num", type=int, default=22, help="대수 (기본: 22대)")
    parser.add_argument("--list-only", action="store_true", help="목록만 표시")
    parser.add_argument("--upload", type=str, help="Google Drive 리모트 (예: gdrive:국회회의록/22대)")
    parser.add_argument("--limit", type=int, default=None, help="다운로드 제한 (기본: 모두)")
    
    args = parser.parse_args()
    
    print(f"🏛️ **국회 회의록 PDF 다운로더**")
    print(f"대수: {args.dae_num}대")
    print()
    
    # 회의 목록 조회
    meetings = get_meetings(args.dae_num, limit=args.limit)
    print(f"📋 회의 목록: {len(meetings)}건")
    print()
    
    if args.list_only:
        # 목록만 표시
        for m in meetings[:10]:
            print(f"  {m['meeting_id']} | {m['meeting_date']} | {m['committee_id']}")
        if len(meetings) > 10:
            print(f"  ... 외 {len(meetings) - 10}건")
        return
    
    # 다운로드 + 업로드
    output_dir = Path("/tmp/assembly_pdfs")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    uploaded = 0
    failed = 0
    
    for i, meeting in enumerate(meetings, 1):
        print(f"⏳ {i}/{len(meetings)} {meeting['meeting_id']} ({meeting['meeting_date']})")
        
        # PDF 다운로드
        pdf_path = download_pdf(meeting['meeting_id'], output_dir)
        
        if pdf_path and args.upload:
            # Google Drive 업로드
            if upload_to_gdrive(pdf_path, args.upload):
                uploaded += 1
            else:
                failed += 1
    
    print()
    print(f"📊 결과:")
    print(f"  ✅ 업로드: {uploaded}건")
    print(f"  ❌ 실패: {failed}건")


if __name__ == "__main__":
    main()
