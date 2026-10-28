"""Small OpenAI-compatible HTTP client, without third party dependencies."""
import json
from urllib import request, error, parse


class ApiError(RuntimeError):
    pass


def base_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parsed = parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('API 주소는 http(s) 기본 URL이어야 합니다. 예: http://127.0.0.1:포트/v1')
    return value


class Client:
    def __init__(self, url, model='', key='', timeout=180, max_tokens=4096):
        self.url = base_url(url)
        self.model, self.key, self.timeout, self.max_tokens = model, key, timeout, max_tokens
        # Ignore environment proxies: local-model requests stay on the configured route.
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def call(self, path, data=None):
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        req = request.Request(self.url + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ApiError('API 응답이 너무 큽니다.')
                return json.loads(raw)
        except error.HTTPError as exc:
            # Never echo response bodies: providers sometimes include credentials/input.
            raise ApiError(f'API HTTP {exc.code}. 주소·인증·서버 로그를 확인하세요.') from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ApiError('API 연결 실패 또는 시간 초과. 서버 실행 여부와 주소를 확인하세요.') from exc
        except (ValueError, UnicodeError) as exc:
            raise ApiError('API가 유효한 JSON 응답을 반환하지 않았습니다.') from exc

    def models(self):
        data = self.call('/models')
        return [m['id'] for m in data.get('data', []) if isinstance(m, dict) and isinstance(m.get('id'), str)]

    def complete(self, messages):
        data = self.call('/chat/completions', {'model': self.model, 'messages': messages,
                         'temperature': 0.2, 'max_tokens': self.max_tokens, 'stream': False})
        try:
            if data['choices'][0].get('finish_reason') == 'length':
                raise ApiError('출력이 잘렸습니다. 묶음 크기를 줄이거나 최대 출력 토큰을 늘리세요.')
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise TypeError()
            return content
        except (KeyError, IndexError, TypeError):
            raise ApiError('chat/completions 응답 형식이 맞지 않습니다.') from None


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
