"""Retain edge ASS controls locally; expose only internal boundaries to the model."""
from dataclasses import dataclass
import re
from .ass import TOKEN


@dataclass
class CueFrame:
    prefix: str
    suffix: str
    source: str
    original: list

    @property
    def tokens(self): return [f'⟪S2L:{i}⟫' for i in range(len(self.original))]

    def repair_trailing_token(self,text):
        """Drop only one nonexistent next token appended after a complete row.

        All expected tokens must already occur exactly once in order. Never
        repair a missing/reordered/duplicated token, an internal extra marker,
        multiple extras or a guessed index. restore() remains strict.
        """
        extra=f'⟪S2L:{len(self.original)}⟫'
        if text.endswith(extra) and TOKEN.findall(text)==self.tokens+[extra]:
            value=text[:-len(extra)]
            if TOKEN.sub('',value).strip():return value,True
        return text,False

    def restore(self,text):
        if TOKEN.findall(text)!=self.tokens:
            raise ValueError('ASS 내부 보호 토큰의 누락·중복·순서 변경')
        lookup=dict(zip(self.tokens,self.original))
        return self.prefix+TOKEN.sub(lambda m:lookup[m.group()],text)+self.suffix


def frame_for(cue):
    leading=re.match(r'^(?:⟪S2L:\d+⟫)+',cue.text)
    prefix=leading.group() if leading else ''
    body=cue.text[len(prefix):]
    trailing=re.search(r'(?:⟪S2L:\d+⟫)+$',body)
    suffix=trailing.group() if trailing else ''
    if suffix: body=body[:-len(suffix)]
    original=TOKEN.findall(body)
    lookup={token:f'⟪S2L:{i}⟫' for i,token in enumerate(original)}
    return CueFrame(prefix,suffix,TOKEN.sub(lambda m:lookup[m.group()],body),original)
