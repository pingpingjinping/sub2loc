"""AniList title search. Searches send only the typed query, never subtitle contents."""
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib import error, request
from .workspace import Work

SEARCH = '''query ($search: String!) {
  Page(page: 1, perPage: 20) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id title { romaji english native } seasonYear format episodes
    }
  }
}'''
CHARACTERS = '''query ($id: Int!) {
  Media(id: $id) {
    characters(perPage: 25, sort: ROLE) { nodes { name { full native } } }
  }
}'''
PROFILE = '''query ($id: Int!) {
  Media(id: $id) {
    id title { native romaji english } seasonYear format episodes genres
    description(asHtml: false)
    characters(perPage: 25, sort: [ROLE, RELEVANCE, ID]) {
      edges { role node { name { native full } description(asHtml: false) } }
    }
  }
}'''

DEFAULT_INSTRUCTIONS = '''이름·호칭·용어는 사용자 고정 메모를 우선하고 일관되게 유지한다.
말투와 높임말은 현재 대사·화자·앞뒤 문맥을 근거로 결정한다.
작품·인물 소개는 배경 참고다. 소개에만 있는 관계·정체·전개를 자막에 추가하지 않는다.'''


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'): self.hidden.append(tag)
            return
        if tag in ('script', 'style') or 'spoiler' in (dict(attrs).get('class') or '').lower():
            self.hidden.append(tag)
        elif tag in ('br', 'p', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                self.hidden = self.hidden[:len(self.hidden) - 1 - self.hidden[::-1].index(tag)]
        elif tag in ('p', 'div', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain_text(value, limit=1200):
    if not isinstance(value, str): return ''
    # AniList's markdown spoiler blocks and marked HTML spoilers are omitted.
    value = re.sub(r'~!.*?(?:!~|$)', '', value, flags=re.DOTALL)
    parser = PlainText(); parser.feed(value)
    text = ' '.join(''.join(parser.parts).split())
    return text[:limit] + ('…' if len(text) > limit else '')


@dataclass(frozen=True)
class GuidanceDraft:
    work_id: str
    title: str
    instructions: str
    reference: str


def media_id(work_id):
    if not re.fullmatch(r'anilist:[1-9]\d*', work_id):
        raise ValueError('AniList에서 매칭한 작품만 정보를 가져올 수 있어요.')
    return int(work_id.split(':')[1])


class CatalogError(RuntimeError):
    pass


class Catalog:
    def __init__(self, endpoint='https://graphql.anilist.co'):
        self.endpoint = endpoint

    def query(self, query, variables):
        req = request.Request(self.endpoint, json.dumps({'query': query, 'variables': variables}).encode(),
                              headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'sub2loc/0.2'})
        try:
            with request.urlopen(req, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise CatalogError('작품 검색 응답이 너무 큽니다.')
                result = json.loads(raw)
            if result.get('errors') or not result.get('data'):
                raise CatalogError('작품 정보를 가져오지 못했습니다. 직접 작품을 등록할 수도 있어요.')
            return result['data']
        except error.HTTPError as exc:
            if exc.code == 429:
                raise CatalogError('검색 요청이 많습니다. 잠시 후 다시 검색하세요.') from None
            raise CatalogError(f'작품 검색 서버 오류 ({exc.code}). 직접 등록은 계속 사용할 수 있어요.') from None
        except (error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise CatalogError('작품 검색에 연결하지 못했습니다. 직접 작품을 등록할 수도 있어요.') from exc

    def search(self, text, media_type="ANIME"):
        if media_type not in ("ANIME","MANGA"):raise ValueError("작품 유형을 확인하세요.")
        if not text.strip() or len(text) > 200:
            raise ValueError('검색어를 1–200자로 입력하세요.')
        data = self.query(SEARCH.replace('type: ANIME','type: '+media_type), {'search': text.strip()})
        works = []
        for row in data['Page']['media']:
            title = row.get('title') or {}
            name = title.get('romaji') or title.get('english') or title.get('native')
            if name:
                works.append(Work('anilist:' + str(row['id']), name, title.get('native') or '',
                                  str(row.get('seasonYear') or ''), row.get('format') or '',
                                  str(row.get('episodes') or ''), reference='AniList 작품 ID: ' + str(row['id']) +
                                  '\n원제: ' + (title.get('native') or name)))
        return works

    def character_notes(self, work_id):
        if not work_id.startswith('anilist:'):
            raise ValueError('AniList에서 매칭한 작품만 인물 정보를 가져올 수 있어요.')
        data = self.query(CHARACTERS, {'id': int(work_id.split(':', 1)[1])})
        nodes = data['Media']['characters']['nodes']
        names = []
        for node in nodes:
            name = node.get('name') or {}
            parts = list(dict.fromkeys(x for x in (name.get('native'), name.get('full')) if x))
            if parts:
                names.append(' / '.join(parts))
        return 'AniList 등장인물 이름 (화자 확정·관계 추측의 근거로 쓰지 말 것):\n' + '\n'.join(names)

    def guidance(self, work_id):
        identifier = media_id(work_id)
        row = self.query(PROFILE, {'id': identifier}).get('Media')
        if not row or row.get('id') != identifier:
            raise CatalogError('선택한 작품의 상세 정보를 찾지 못했습니다.')
        title = row.get('title') or {}
        titles = list(dict.fromkeys(plain_text(title.get(k), 200) for k in ('native', 'romaji', 'english') if title.get(k)))
        name = ' / '.join(titles) or f'AniList {identifier}'
        names, biographies = [], []
        roles = {'MAIN': '주요', 'SUPPORTING': '조연', 'BACKGROUND': '배경'}
        for edge in (row.get('characters') or {}).get('edges') or []:
            node = edge.get('node') or {}; character = node.get('name') or {}
            parts = list(dict.fromkeys(plain_text(character.get(k), 100) for k in ('native', 'full') if character.get(k)))
            if not parts: continue
            person = ' / '.join(parts)
            item = person + (' · ' + roles[edge['role']] if edge.get('role') in roles else '')
            if item not in names: names.append(item)
            bio = plain_text(node.get('description'), 240)
            if bio and edge.get('role') == 'MAIN' and len(biographies) < 4:
                biographies.append(person + ': ' + bio)
        reference = ['[AniList 배경 자료 · 소개는 제공된 원문]', '작품: ' + name]
        details = [str(row[k]) for k in ('seasonYear', 'format') if row.get(k)]
        if row.get('episodes'): details.append(str(row['episodes']) + '화')
        if details: reference.append(' · '.join(details))
        if names:
            reference += ['주요 이름 참고 (한국어 표기는 고정 메모에서 지정):', '\n'.join(names[:6])]
        genres = [plain_text(g, 80) for g in row.get('genres') or [] if g]
        if genres: reference.append('장르: ' + ', '.join(genres[:8]))
        description = plain_text(row.get('description'), 900)
        if description: reference += ['작품 소개 원문:', description]
        if len(names) > 6: reference += ['추가 등장인물 이름:', '\n'.join(names[6:25])]
        if biographies: reference += ['주요 인물 소개 원문 (현재 회차의 사실로 확정하지 않음):', '\n'.join(biographies)]
        reference.append(f'출처: https://anilist.co/anime/{identifier}')
        return GuidanceDraft(work_id, name, DEFAULT_INSTRUCTIONS, '\n'.join(reference))
