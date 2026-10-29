"""BallonsTranslator's numbered page TXT interchange; never guess plain TXT IDs."""
import re
from .subtitles import PlainCue
from .ocr_checks import source_issues


class BalloonDocument:
    output_encoding = 'utf-8'
    reading_count = 0

    def __init__(self, raw):
        text=raw.decode('utf-8-sig');self.pages=[];self.cues=[]
        page=None;block=None;seen=set()
        for line in text.splitlines():
            if line.startswith('### '):
                name=line[4:].strip()
                if not name or name in seen:raise ValueError('벌룬 TXT의 페이지명이 비었거나 중복됐습니다.')
                seen.add(name);page=dict(name=name,blocks=[]);self.pages.append(page);block=None
            elif re.match(r'^\d+\.',line):
                if page is None:raise ValueError('말풍선 앞에 ### 페이지명이 필요합니다.')
                number,body=line.split('.',1)
                if int(number)!=len(page['blocks'])+1:raise ValueError('벌룬 TXT의 말풍선 번호가 누락·중복·변경됐습니다.')
                block=dict(number=int(number),lines=[body.lstrip()],cue=None);page['blocks'].append(block)
            elif line.strip():
                if block is None:raise ValueError('페이지·말풍선 구분을 확인하세요. 벌룬 원문 TXT만 지원합니다.')
                block['lines'].append(line)
        if not self.pages:raise ValueError('벌룬트랜스 원문 내보내기 TXT(### 페이지 / 1. 대사)를 사용하세요.')
        for page in self.pages:
            for block in page['blocks']:
                source=' '.join(' '.join(block['lines']).split())
                if not source:continue
                cue=PlainCue(len(self.cues),0,page['name']+'\0'+str(block['number']),'','','',source,[])
                cue.page=page['name'];cue.block=block['number']
                cue.ocr_warnings=source_issues(source)
                block['cue']=cue.id;self.cues.append(cue)

    def render(self, translations):
        if set(translations)!={str(c.id) for c in self.cues}:raise ValueError('말풍선 번역 수가 원문과 다릅니다.')
        values={c.id:c.restore(translations[str(c.id)]) for c in self.cues}
        for value in values.values():
            if re.match(r'^\d+\.',value) or value.startswith('###'):raise ValueError('번역문이 벌룬 구분자와 충돌합니다.')
        return '\n\n\n'.join('\n\n'.join(['### '+p['name']]+[str(b['number'])+'. '+values.get(b['cue'],'') for b in p['blocks']]) for p in self.pages).encode('utf-8')
