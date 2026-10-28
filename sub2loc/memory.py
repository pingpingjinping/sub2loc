"""Bounded incremental context, built only from accepted preceding source IDs."""
import json
from .ass import TOKEN

from pathlib import Path

SUMMARY_SYSTEM = Path(__file__).with_name('templates').joinpath('context_summary_ko.txt').read_text('utf-8').strip()


def summary_for(record, before):
    if (isinstance(record,dict) and type(record.get('covered')) is int
        and 0 < record['covered'] <= before and isinstance(record.get('text'),str)):
        return record['text'][:1600]
    return ''


def summary_request(cues, saved, before, record, budget, notes=''):
    covered = record.get('covered',0) if isinstance(record,dict) else 0
    if type(covered) is not int or not 0 <= covered <= before:return None
    data = dict(previous_summary=summary_for(record,before),fixed_notes=notes[:1000],preceding_rows=[])
    messages=[dict(role='system',content=SUMMARY_SYSTEM),dict(role='user',content='')]
    end=covered
    for cue in cues[covered:min(before,covered+100)]:
        if str(cue.id) not in saved:break
        row=dict(number=cue.id+1,source=TOKEN.sub('',cue.text),translation=TOKEN.sub('',saved[str(cue.id)]))
        data['preceding_rows'].append(row)
        messages[1]['content']=json.dumps(data,ensure_ascii=False)
        if len(json.dumps(messages,ensure_ascii=False).encode('utf-8'))+128 > budget:
            data['preceding_rows'].pop();break
        end=cue.id+1
    if end==covered:return None
    messages[1]['content']=json.dumps(data,ensure_ascii=False)
    return messages,end
