#!/usr/bin/env python3
"""
국회 회의록 JSON → PDF 변환 + Google Drive 업로드
설치: pip install reportlab
사용: python3 convert_json_to_pdf.py [--limit 10]
"""

import sqlite3
import json
import subprocess
from pathlib import Path
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_LEFT, TA_CENTER

DB_PATH = Path(__file__).parent / "db" / "assembly.db"
PDF_OUTPUT_DIR = Path("/tmp/assembly_pdfs")


def json_to_pdf(json_path: str, pdf_path: str):
    """JSON 회의록 → PDF 변환"""
    
    try:
        # JSON 파일 읽기
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 문서 정보 추출
        title = data.get('title', '국회 회의록')
        date = data.get('meetingDate', '날짜 미상')
        committee = data.get('committee', '위원회 미상')
        
        # PDF 생성
        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2*cm,
            bottomMargin=2*cm
        )
        
        # 스타일 정의
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=16,
            textColor='#000000',
            spaceAfter=12,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        meta_style = ParagraphStyle(
            'Meta',
            parent=styles['Normal'],
            fontSize=10,
            textColor='#666666',
            spaceAfter=6,
            alignment=TA_LEFT
        )
        
        content_style = styles['Normal']
        content_style.fontSize = 10
        content_style.leading = 14
        
        # 문서 구성
        story = []
        
        # 제목
        story.append(Paragraph(f"<b>{title}</b>", title_style))
        story.append(Spacer(1, 0.3*cm))
        
        # 메타정보
        story.append(Paragraph(f"<b>날짜:</b> {date}", meta_style))
        story.append(Paragraph(f"<b>위원회:</b> {committee}", meta_style))
        story.append(Spacer(1, 0.5*cm))
        
        # 발언 내용
        utterances = data.get('utterances', [])
        
        for utterance in utterances:
            speaker = utterance.get('speaker', '발언자 미상')
            role = utterance.get('role', '')
            text = utterance.get('text', '')
            
            # 발언자 정보
            if role:
                speaker_line = f"<b>{speaker}({role}):</b>"
            else:
                speaker_line = f"<b>{speaker}:</b>"
            
            story.append(Paragraph(speaker_line, content_style))
            
            # 발언 텍스트 (길이 제한)
            if text:
                text = text[:500]  # 최대 500자
                story.append(Paragraph(text, content_style))
            
            story.append(Spacer(1, 0.2*cm))
        
        # PDF 생성
        doc.build(story)
        return True
    
    except Exception as e:
        print(f"❌ PDF 변환 실패: {e}")
        return False


def convert_and_upload(limit: int = None):
    """회의록 JSON → PDF 변환 + Google Drive 업로드"""
    
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    
    query = "SELECT meeting_id, meeting_date, raw_text_path FROM meeting WHERE raw_text_path IS NOT NULL ORDER BY meeting_date DESC"
    
    if limit:
        query += f" LIMIT {limit}"
    
    meetings = db.execute(query).fetchall()
    db.close()
    
    PDF_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"🏛️ **국회 회의록 JSON → PDF 변환**")
    print(f"📋 회의: {len(meetings)}건")
    print()
    
    converted = 0
    failed = 0
    
    for i, m in enumerate(meetings, 1):
        meeting_id = m['meeting_id']
        meeting_date = m['meeting_date']
        json_path = m['raw_text_path']
        
        p_json = Path(json_path)
        
        if not p_json.exists():
            print(f"⏳ {i}/{len(meetings)} {meeting_id} - ⚠️ JSON 파일 없음")
            failed += 1
            continue
        
        # PDF 파일 경로
        pdf_filename = f"{meeting_id}_{meeting_date}.pdf"
        pdf_path = PDF_OUTPUT_DIR / pdf_filename
        
        # JSON → PDF 변환
        if json_to_pdf(str(p_json), str(pdf_path)):
            # Google Drive에 업로드
            year = meeting_date[:4]
            month = meeting_date[5:7]
            gdrive_dir = f"gdrive:국회회의록/22대/{year}/{month}/"
            
            try:
                cmd = ["rclone", "copy", str(pdf_path), gdrive_dir]
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                
                if result.returncode == 0:
                    print(f"✅ {i}/{len(meetings)} {meeting_id} ({meeting_date}) - PDF 업로드 완료")
                    converted += 1
                else:
                    print(f"⚠️ {i}/{len(meetings)} {meeting_id} - PDF 생성 완료, 업로드 실패")
                    converted += 1
                    failed += 1
            
            except Exception as e:
                print(f"⚠️ {i}/{len(meetings)} {meeting_id} - {e}")
                converted += 1
        else:
            print(f"❌ {i}/{len(meetings)} {meeting_id} - PDF 변환 실패")
            failed += 1
    
    print()
    print(f"📊 **결과**")
    print(f"  ✅ 변환: {converted}건")
    print(f"  ❌ 실패: {failed}건")
    print(f"  📂 Google Drive: gdrive:국회회의록/22대/")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="제한 (기본: 모두)")
    args = parser.parse_args()
    
    convert_and_upload(limit=args.limit)
