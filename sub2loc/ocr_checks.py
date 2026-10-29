"""Cheap source warnings, never automatic OCR edits or semantic verdicts."""
import re


def source_issues(text):
    issues = []
    if '\ufffd' in text or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', text):
        issues.append('원문 OCR 확인 필요: 깨진 문자·제어문자')
    if re.search(r'(.{2,40}?)\1{3,}', text):
        issues.append('원문 반복 의심 · 의도된 효과음일 수도 있음')
    return issues
