"""Mechanical checks only; these do not establish semantic accuracy."""
import re
from .ass import TOKEN

CJK = re.compile(r'[ぁ-ゖァ-ヺｦ-ﾟ一-鿿㐀-䶿豈-﫿\U00020000-\U0003134f]')


def normalize_interjection(source, target):
    visible = TOKEN.sub('', target).strip()
    original = TOKEN.sub('', source).strip()
    if re.fullmatch(r'[んうあえおンウアエオー～~….!！?？\s]+', original) and re.fullmatch(r'[嗯呃唔]+[ー～~….!！?？\s]*', visible):
        return target.translate(str.maketrans({'嗯': '음', '呃': '어', '唔': '음'}))
    return target


def structure_issues(cue,text):
    if not TOKEN.sub('',text).strip():return ['빈 번역']
    expected=[f'⟪S2L:{i}⟫' for i in range(len(cue.protected))]
    if TOKEN.findall(text)!=expected:return ['ASS 보호 토큰의 누락·중복·순서 변경']
    try: cue.restore(text)
    except ValueError:return ['번역문에 새 ASS 제어문자 또는 실제 개행 삽입']
    breaks = set(); drawing = False
    for i,control in enumerate(cue.protected):
        if not drawing and control in (r'\N',r'\n'): breaks.add(f'⟪S2L:{i}⟫')
        if control.startswith('{'):
            modes=re.findall(r'\\p(\d+)(?!\d)',control)
            if modes: drawing=int(modes[-1])!=0
    def lines(value):
        return TOKEN.sub(lambda m:'\n' if m.group() in breaks else '',value).split('\n')
    if any(a.strip() and not b.strip() for a,b in zip(lines(cue.text),lines(text))):
        return ['ASS 시각적 줄의 빈 번역']
    return []


def inspect_translation(cue, text, *, korean=True, allowed=()):
    issues = structure_issues(cue,text)
    visible = TOKEN.sub('', text).strip()
    source = TOKEN.sub('', cue.text).strip()
    if not visible: return issues or ['빈 번역']
    foreign = visible
    for word in sorted(allowed, key=len, reverse=True):
        if word: foreign = foreign.replace(word, '')
    if korean and CJK.search(foreign): issues.append('일본어·중국어 문자 혼입 의심')
    if re.search(r'(.{3,80}?)\1{4,}', visible): issues.append('비정상 반복 의심')
    if len(visible) > max(80, len(source) * 4 + 20): issues.append('원문 대비 과도한 길이')
    return issues
