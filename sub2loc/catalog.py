"""AniList title search. Searches send only the typed query, never subtitle contents."""
import json
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
  Media(id: $id, type: ANIME) {
    characters(perPage: 25, sort: ROLE) { nodes { name { full native } } }
  }
}'''


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

    def search(self, text):
        if not text.strip() or len(text) > 200:
            raise ValueError('검색어를 1–200자로 입력하세요.')
        data = self.query(SEARCH, {'search': text.strip()})
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
