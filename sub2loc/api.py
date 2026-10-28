"""Small OpenAI-compatible HTTP client, without third party dependencies."""
import json
from urllib import request, error, parse


class ApiError(RuntimeError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def safe_preview(text, key='', limit=600):
    text = text if isinstance(text, str) else ''
    if key:
        text = text.replace(key, '[API 키 삭제]')
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
    if len(text) > limit:
        text = text[:limit * 2 // 3] + '\n… [중간 생략] …\n' + text[-limit // 3:]
    return text


class OutputLimitError(ApiError):
    """A failed response is diagnostic data, never a successful partial translation."""
    def __init__(self, limit, output, usage=None, key=''):
        super().__init__(f'번역 출력이 한도에 도달했습니다. 요청 상한: {limit}토큰.', retryable=False)
        self.output_preview = safe_preview(output, key)
        self.source_preview = ''
        self.filename = ''
        self.first = self.last = 0
        usage = usage if isinstance(usage, dict) else {}
        self.usage = {k: v for k, v in usage.items()
                      if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int}

    def with_source(self, source, key=''):
        self.source_preview = safe_preview(source, key, 240)
        return self

    def __str__(self):
        parts = [super().__str__()]
        if self.filename:
            parts.append(f'파일: {self.filename} / 대사: {self.first}–{self.last}')
        if self.usage:
            parts.append('서버 토큰 수: ' + ', '.join(f'{k}={v}' for k, v in self.usage.items()))
        parts.append('같은 요청은 자동 재시도하지 않았고, 잘린 응답은 번역 결과로 저장하지 않았습니다.')
        if self.source_preview:
            parts += ['요청한 원문:', self.source_preview]
        parts += ['생성된 출력 미리보기:', self.output_preview or '(빈 응답)']
        return '\n'.join(parts)



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
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise TypeError()
            if data['choices'][0].get('finish_reason') == 'length':
                raise OutputLimitError(self.max_tokens, content, data.get('usage'), self.key)
            return content
        except (KeyError, IndexError, TypeError):
            raise ApiError('chat/completions 응답 형식이 맞지 않습니다.') from None


    def complete_text(self, prompt, max_tokens=1024):
        data = self.call('/completions', {'model': self.model, 'prompt': prompt,
                         'temperature': 0.1, 'max_tokens': max_tokens, 'stream': False,
                         'stop': ['<end_of_turn>', '<eos>', '<start_of_turn>'], 'echo': False})
        try:
            content = data['choices'][0]['text']
            if not isinstance(content, str):
                raise TypeError()
            if data['choices'][0].get('finish_reason') == 'length':
                raise OutputLimitError(max_tokens, content, data.get('usage'), self.key)
            return content
        except (KeyError, IndexError, TypeError):
            raise ApiError('completions 응답 형식이 맞지 않습니다. 서버의 /v1/completions 지원을 확인하세요.') from None


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
