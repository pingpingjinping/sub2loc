"""Keep ASS controls local. Internal positions use length/word boundaries.

This preserves markup, not semantic word alignment or karaoke timing.
"""
from dataclasses import dataclass
import re
import unicodedata
from .ass import TOKEN


@dataclass
class CueLayout:
    cue: object
    source: str
    anchors: list

    def render(self, translated):
        if translated == self.source:
            return self.cue.text
        result, cursor, previous = [], 0, 0
        boundaries = [0, len(translated)]
        boundaries += [m.start() for m in re.finditer(r'\s+|[、。，．！？!?]', translated)]
        for position, token, separator in self.anchors:
            if position == 0:
                target = 0
            elif position >= len(self.source):
                target = len(translated)
            else:
                target = round(position * len(translated) / len(self.source))
                nearby = min(boundaries, key=lambda b: abs(b - target))
                if 0 < nearby < len(translated) and abs(nearby - target) <= max(2, len(translated) // 12):
                    target = nearby
                while target < len(translated) and unicodedata.combining(translated[target]):
                    target += 1
            target = max(previous, target)
            result.append(translated[cursor:target]); result.append(token)
            cursor = max(cursor, target)
            # Replace only whitespace with a break/space control, never a
            # visible character. Consecutive controls retain their order.
            if separator and cursor < len(translated) and translated[cursor].isspace():
                cursor += 1
            previous = cursor
        result.append(translated[cursor:])
        masked = ''.join(result)
        self.cue.restore(masked)
        return masked


def layout_for(cue):
    parts, anchors, position, start = [], [], 0, 0
    drawing = False
    for match in TOKEN.finditer(cue.text):
        text = cue.text[start:match.start()]
        parts.append(text); position += len(text)
        control = cue.protected[int(match.group()[5:-1])]
        separator = not drawing and control in (r'\N', r'\n', r'\h')
        anchors.append((position, match.group(), separator))
        if separator:
            parts.append(' '); position += 1
        if control.startswith('{'):
            modes = re.findall(r'\\p(\d+)(?!\d)', control)
            if modes:
                drawing = int(modes[-1]) != 0
        start = match.end()
    parts.append(cue.text[start:])
    return CueLayout(cue, ''.join(parts), anchors)
