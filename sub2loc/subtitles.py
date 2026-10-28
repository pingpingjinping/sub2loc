"""Explicit ASS preservation or plain SRT/SAMI output from ASS dialogue."""
from collections import defaultdict
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
import re

from .ass import AssDocument, Cue, TOKEN, protect
from .ass_cleanup import strip_inline_decoration

FORMATS = (('ASS · 서식 보존', 'ass'), ('SRT · 일반 자막', 'srt'), ('SMI · 일반 자막', 'smi'))


def output_format(config):
    value = config.get('output_format', 'ass')
    if value not in ('ass', 'srt', 'smi'):
        raise ValueError('저장 형식은 ASS, SRT, SMI 중에서 선택하세요.')
    return value


def output_name(name, config):
    return Path(name).stem + '.translated.' + output_format(config)


def plain_source(cue):
    """Drop styles/drawings; flatten visual line and spacing controls locally."""
    def replace(match):
        control = cue.protected[int(match.group()[5:-1])]
        return ' ' if control in (r'\N', r'\n', r'\h') else ''
    return re.sub(r'\s+', ' ', TOKEN.sub(replace, cue.text)).strip()


def milliseconds(value):
    match = re.fullmatch(r'(\d+):([0-5]\d):([0-5]\d)\.(\d{2})', value)
    if not match:
        raise ValueError('ASS 시간은 시:분:초.백분의초 형식이어야 합니다: ' + value[:40])
    hours, minutes, seconds, fraction = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction * 10


def srt_time(value):
    seconds, ms = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}'


def subtitle_timeline(rows):
    """Active prepared text at each boundary; intervals are [start, end)."""
    events = defaultdict(lambda: ([], [])); values = {}
    for identity, (start, end, text) in enumerate(rows):
        if end <= start:
            raise ValueError('종료 시간이 시작 시간보다 늦어야 합니다.')
        values[identity] = text
        events[start][0].append(identity)
        events[end][1].append(identity)
    active = set()
    for at, (begin, finish) in sorted(events.items()):
        active.difference_update(finish); active.update(begin)
        yield at, tuple(values[i] for i in sorted(active))


def render_srt(rows):
    """Split overlaps into nonoverlapping spans, retaining active text in order."""
    timeline = list(subtitle_timeline(rows))
    blocks = []
    for (start, lines), (end, _) in zip(timeline, timeline[1:]):
        if lines:
            blocks.append(f'{len(blocks)+1}\r\n{srt_time(start)} --> {srt_time(end)}\r\n' + '\r\n'.join(lines) + '\r\n\r\n')
    return ''.join(blocks)


@dataclass
class PlainCue(Cue):
    plain = True

    def restore(self, translated):
        if not translated.strip():
            raise ValueError('빈 번역')
        if TOKEN.search(translated) or re.search(r'\{\\|\\[Nnh]|[\x00-\x1f\x7f]', translated):
            raise ValueError('일반 자막에 서식 토큰·ASS 제어문자 또는 실제 개행 삽입')
        return translated


class PlainDocument:
    def __init__(self, raw, config):
        self.format = output_format(config)
        self.output_encoding = 'utf-8-sig'
        self.target = config.get('target', '한국어').strip().casefold()
        original = AssDocument(raw, config['encoding'], hide_readings=config.get('hide_readings', False))
        self.reading_count = original.reading_count
        self.cues = [PlainCue(**dict(vars(c), text=plain_source(c), protected=[])) for c in original.cues]
        self.times = []
        for cue in self.cues:
            try:
                start, end = milliseconds(cue.start), milliseconds(cue.end)
                if end <= start:
                    raise ValueError('종료 시간이 시작 시간보다 늦어야 합니다.')
            except ValueError as exc:
                raise ValueError(f'대사 {cue.id + 1}: 일반 자막으로 변환할 수 없는 시간입니다. {exc}') from None
            self.times.append((start, end))

    def render(self, translations):
        if set(translations) != {str(c.id) for c in self.cues}:
            raise ValueError('번역 대사 수가 원문과 다릅니다.')
        values = {c.id: escape(c.restore(translations[str(c.id)]), quote=False) for c in self.cues}
        if self.format == 'srt':
            result = render_srt((start, end, values[c.id]) for c, (start, end) in zip(self.cues, self.times))
        else:
            timeline = list(subtitle_timeline((start, end, values[c.id]) for c, (start, end) in zip(self.cues, self.times)))
            blocks = []
            if timeline and timeline[0][0] > 0:
                blocks.append('<SYNC Start=0><P Class=SUBCC>&nbsp;</P></SYNC>')
            for at, lines in timeline:
                text = '<BR>'.join(lines) if lines else '&nbsp;'
                blocks.append(f'<SYNC Start={at}><P Class=SUBCC>{text}</P></SYNC>')
            language = 'ko-KR' if self.target in ('한국어', '한글', 'ko', 'ko-kr', 'korean', '한국말') else 'und'
            result = ('<SAMI>\r\n<HEAD>\r\n<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=utf-8">\r\n'
                      '<STYLE TYPE="text/css">\r\n<!--\r\n'
                      'P {font-family: sans-serif; text-align: center; color: white;}\r\n'
                      f'.SUBCC {{Name: Subtitles; lang: {language}; SAMIType: CC;}}\r\n'
                      '-->\r\n</STYLE>\r\n</HEAD>\r\n<BODY>\r\n' + '\r\n'.join(blocks) + '\r\n</BODY>\r\n</SAMI>\r\n')
        return result.encode(self.output_encoding)


def document_for(raw, config):
    if type(config.get('strip_ass_decoration', False)) is not bool:
        raise ValueError('ASS 색상·강조 제거 설정이 올바르지 않습니다.')
    if output_format(config) != 'ass':
        return PlainDocument(raw, config)
    doc = AssDocument(raw, config['encoding'], hide_readings=config.get('hide_readings', False))
    if config.get('strip_ass_decoration', False):
        cues = []
        for cue in doc.cues:
            masked, saved = protect(strip_inline_decoration(cue.restore(cue.text)))
            cues.append(replace(cue, text=masked, protected=saved))
        doc.cues = cues
    return doc
