"""Lossless ASS dialogue parsing; only Text fields are replaced."""
from dataclasses import dataclass
import re

TOKEN = re.compile(r'⟪S2L:\d+⟫')
CONTROL = re.compile(r'\{[^{}]*\}|[{}]|\\[Nnh]|⟪S2L:\d+⟫')


@dataclass
class Cue:
    id: int
    line: int
    prefix: str
    ending: str
    speaker: str
    style: str
    text: str
    protected: list[str]

    def restore(self, translated: str) -> str:
        expected = [f'⟪S2L:{i}⟫' for i in range(len(self.protected))]
        if TOKEN.findall(translated) != expected:
            raise ValueError(f'대사 {self.id}: ASS 태그/줄바꿈 토큰이 변경되었습니다.')
        # Raw controls from the model must never become executable ASS markup.
        if re.search(r'[{}\r\n]|\\[Nnh]', TOKEN.sub('', translated)):
            raise ValueError(f'대사 {self.id}: 새 ASS 제어문자 또는 줄바꿈이 포함되었습니다.')
        if not TOKEN.sub('', translated).strip():
            raise ValueError(f'대사 {self.id}: 번역문이 비어 있습니다.')
        return TOKEN.sub(lambda m: self.protected[int(m.group()[5:-1])], translated)


def protect(text: str) -> tuple[str, list[str]]:
    parts, protected = [], []
    drawing = False
    pos = 0

    def add(value, hidden=False):
        if not value:
            return
        if hidden:
            parts.append(f'⟪S2L:{len(protected)}⟫')
            protected.append(value)
        else:
            parts.append(value)

    for match in CONTROL.finditer(text):
        add(text[pos:match.start()], drawing)
        add(match.group(), True)
        if match.group().startswith('{'):
            modes = re.findall(r'\\p(\d+)(?!\d)', match.group())
            if modes:
                drawing = int(modes[-1]) != 0
        pos = match.end()
    add(text[pos:], drawing)
    return ''.join(parts), protected


class AssDocument:
    def __init__(self, raw: bytes, encoding='utf-8-sig'):
        self.encoding = encoding
        self.bom = raw.startswith(b'\xef\xbb\xbf')
        self.output_bom = b''
        # utf-8-sig reads BOM optionally; preserve its presence on output.
        self.output_encoding = 'utf-8' if encoding == 'utf-8-sig' and not self.bom else encoding
        text = raw.decode(encoding, errors='strict')
        if encoding == 'utf-16':
            self.output_bom = raw[:2]
            self.output_encoding = 'utf-16-be' if raw.startswith(b'\xfe\xff') else 'utf-16-le'
        self.lines = text.splitlines(keepends=True)
        self.cues: list[Cue] = []
        section, fields = '', None
        for n, line in enumerate(self.lines):
            body = line.rstrip('\r\n')
            ending = line[len(body):]
            stripped = body.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                section = stripped.lower()
                fields = None
            if section != '[events]':
                continue
            if body.lower().startswith('format:'):
                fields = [x.strip().lower() for x in body.split(':', 1)[1].split(',')]
                if not fields or fields[-1] != 'text' or len(set(fields)) != len(fields):
                    raise ValueError('ASS Events Format의 마지막 필드는 Text여야 합니다.')
            if not body.lower().startswith('dialogue:'):
                continue
            if not fields:
                raise ValueError('Dialogue 앞에 Events Format이 필요합니다.')
            lead, payload = body.split(':', 1)
            values = payload.split(',', len(fields) - 1)
            if len(values) != len(fields):
                raise ValueError(f'ASS {n + 1}행: 필드 수가 맞지 않습니다.')
            masked, saved = protect(values[-1])
            if not TOKEN.sub('', masked).strip():
                continue
            get = lambda name: values[fields.index(name)].strip() if name in fields else ''
            prefix = lead + ':' + (','.join(values[:-1]) + ',' if len(values) > 1 else '')
            self.cues.append(Cue(len(self.cues), n, prefix,
                                 ending, get('name'), get('style'), masked, saved))
        if section == '' or not any(x.strip().lower() == '[events]' for x in self.lines):
            raise ValueError('ASS Events 섹션을 찾을 수 없습니다.')

    def render(self, translations: dict[str, str]) -> bytes:
        if set(translations) != {str(c.id) for c in self.cues}:
            raise ValueError('번역 대사 수가 원문과 다릅니다.')
        output = self.lines.copy()
        for cue in self.cues:
            output[cue.line] = cue.prefix + cue.restore(translations[str(cue.id)]) + cue.ending
        return self.output_bom + ''.join(output).encode(self.output_encoding, errors='strict')
