"""Complete alternative prompts; options are compiled once, not stacked at runtime."""
import copy
import json
from pathlib import Path
import uuid

from .profiles import DEFAULT_TEMPLATE, MANGA_TEMPLATE

ROOT = Path(__file__).with_name('templates')
OPTIONS = json.loads((ROOT/'prompt_options.json').read_text('utf-8'))
SOURCE_PROFILES = {
    'kone-mybest': dict(name='코네 소미소프트 · mybest · OCR 만화 · 2026-05-26',
        source='https://kone.gg/s/somisoft/dqSPytdL_mLAbWt14Qy54b?mode=hot', date='2026-05-26',
        defaults=dict(adult=True),
        changes='출처별 자체 적용본 · 원문 복제 아님. 7/30 작성자 댓글의 특수문자 금지 제거 안내를 참고해 장식문자 제한은 기본 OFF. OCR 복원은 별도 후보 검수로 이동. 화자 불명 시 성별 추측 금지, ID/ASS 규약 유지.',
        template='kone_mybest_ja_ko.txt'),
    'dc-wizone': dict(name='디시 라이트노벨 · wIZ*ONEw · 호칭 보존 · 2025-01-24',
        source='https://gall.dcinside.com/board/view/?id=web_fic&no=162128', date='2025-01-24',
        defaults=dict(honorifics=True, pronouns=True, notes=True),
        changes='출처별 자체 적용본 · 원문 복제 아님. 오래된 자료이며 최신 또는 품질 우위로 표시하지 않음. 소설 문단 재구성 대신 ID 경계 유지. 문화 주석은 같은 ID 안의 짧은 괄호로 제한. 역할극 도입문 생략.',
        template='dc_wizone_ja_ko.txt'),
}


def compile_source(source_key, options):
    if source_key not in SOURCE_PROFILES: raise ValueError('출처별 프롬프트를 선택하세요.')
    if set(options)-set(OPTIONS) or any(type(v) is not bool for v in options.values()):
        raise ValueError('프롬프트 체크옵션 형식을 확인하세요.')
    result = {}
    for domain in ('anime','manga'):
        text = (ROOT/SOURCE_PROFILES[source_key]['template']).read_text('utf-8').strip()
        text = text.replace('{{medium}}', '애니메이션 자막' if domain=='anime' else '만화 말풍선·독백·설명·효과음')
        for key, spec in OPTIONS.items(): text = text.replace('{{'+key+'}}', spec['on' if options.get(key,False) else 'off'])
        result[domain] = text
    return result


def builtins():
    items=[dict(id='general',name='범용 · 현재 기본',anime=DEFAULT_TEMPLATE,manga=MANGA_TEMPLATE,
                options={},source_key='',source='',changes='현재 범용 프롬프트를 그대로 유지합니다.',experimental=False)]
    for key, spec in SOURCE_PROFILES.items():
        options={k:spec['defaults'].get(k,False) for k in OPTIONS}
        items.append(dict(id=key,name=spec['name'],source_key=key,source=spec['source'],published=spec['date'],
                          reviewed='2026-10-29',experimental=True,options=options,changes=spec['changes'],
                          **compile_source(key,options)))
    return items


def validate_profile(profile):
    if not isinstance(profile,dict): raise ValueError('저장 프롬프트 형식을 확인하세요.')
    for key, maximum in (('id',80),('name',100),('anime',20000),('manga',20000)):
        if not isinstance(profile.get(key),str) or not profile[key].strip() or len(profile[key])>maximum:
            raise ValueError('프롬프트 이름과 애니·만화 본문을 확인하세요 (본문 최대 20000자).')
    options=profile.get('options',{})
    if 'experimental' in profile and type(profile['experimental']) is not bool:raise ValueError('프롬프트 검증 상태 형식을 확인하세요.')
    if not isinstance(options,dict) or set(options)-set(OPTIONS) or any(type(v) is not bool for v in options.values()):
        raise ValueError('저장 프롬프트의 옵션 형식을 확인하세요.')
    for key in ('source_key','source','published','reviewed','changes'):
        if not isinstance(profile.get(key,''),str) or len(profile.get(key,''))>2000:raise ValueError('프롬프트 출처 정보를 확인하세요.')
    return copy.deepcopy({k:profile[k] for k in ('id','name','anime','manga','options','source_key','source','published','reviewed','changes','experimental') if k in profile})


def library(saved):
    if not isinstance(saved,list) or len(saved)>50: raise ValueError('개인 프롬프트는 최대 50개까지 저장할 수 있습니다.')
    items = builtins(); ids = {r['id'] for r in items}
    for entry in saved:
        item=validate_profile(entry)
        if item['id'] in ids: raise ValueError('프롬프트 ID가 중복됐습니다.')
        ids.add(item['id']); items.append(item)
    return items


def personal(name, anime, manga, **metadata):
    return validate_profile(dict(id='user:'+uuid.uuid4().hex, name=name.strip(),anime=anime.strip(),manga=manga.strip(),**metadata))
