"""Run a private 037–136 comparison on the user's own PC; never uploads fixtures."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sub2loc.ass import AssDocument, TOKEN
from sub2loc.engine import Engine
from sub2loc.profiles import POLYSTRATA
from sub2loc.workspace import validate_settings, write_result


def main():
    parser=argparse.ArgumentParser(description='PolyStrata 자막 구간 실측 (원본 수정 없음, 매번 새 캐시)')
    parser.add_argument('ass',type=Path)
    parser.add_argument('--start',type=int,default=37);parser.add_argument('--end',type=int,default=136)
    parser.add_argument('--url',default=POLYSTRATA['url']);parser.add_argument('--model',default='')
    parser.add_argument('--output',type=Path,default=Path('benchmark-output'))
    parser.add_argument('--notes',type=Path);parser.add_argument('--no-stream',action='store_true')
    args=parser.parse_args()
    raw=args.ass.read_bytes();doc=AssDocument(raw)
    if not 1<=args.start<=args.end<=len(doc.cues): parser.error('대사 범위가 원본 범위를 벗어났습니다.')
    selected=doc.cues[args.start-1:args.end];lines={c.line for c in selected}
    sample=''.join(line for i,line in enumerate(doc.lines) if not line.lower().startswith('dialogue:') or i in lines)
    sample=sample.encode('utf-8-sig' if doc.bom else 'utf-8')
    cfg=validate_settings(dict(POLYSTRATA,url=args.url,model=args.model,work='개별 성능 비교',source='auto',target='한국어',
          notes=args.notes.read_text('utf-8-sig') if args.notes else '',encoding='utf-8-sig',timeout=180,
          stream=not args.no_stream,key=os.environ.get('SUB2LOC_API_KEY','')))
    events=[];started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='sub2loc-benchmark-') as root:
        engine=Engine(Path(root));out,_=engine.translate(cfg,[(args.ass.name,sample)],progress=lambda **d:events.append(d))
        elapsed=time.monotonic()-started
        args.output.mkdir(parents=True,exist_ok=True)
        destination=write_result(args.output/f'{args.ass.stem}.sample-{args.start:03}-{args.end:03}.translated.ass',out[0][1])
        translated=AssDocument(out[0][1]);last=events[-1]
        report=dict(measured_on_user_pc=True,elapsed_seconds=elapsed,start=args.start,end=args.end,
             requests=last.get('requests'),retries=last.get('retries'),errors=last.get('errors'),
             tokens_per_second=last.get('tokens_per_second'),speed_kind=last.get('speed_kind'),review=engine.last_review,
             lines=[dict(original_number=args.start+i,source=TOKEN.sub('',a.text),translation=TOKEN.sub('',b.text))
                    for i,(a,b) in enumerate(zip(selected,translated.cues))])
        report_path=destination.with_suffix('.benchmark.json');report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        print(f'{len(selected)}대사 / {elapsed:.2f}초 / 요청 {last.get("requests")} / 재시도 {last.get("retries")}')
        print(destination);print(report_path)


if __name__=='__main__':main()
