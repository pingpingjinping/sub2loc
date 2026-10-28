"""Small OpenAI-compatible HTTP client, without third party dependencies."""
import json
import http.client
from dataclasses import dataclass, field
import time
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
    def __init__(self, url, model='', key='', timeout=180, max_tokens=4096, *, temperature=None):
        self.temperature = temperature
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
                         'temperature': self.temperature if self.temperature is not None else 0.2, 'max_tokens': self.max_tokens, 'stream': False})
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
                         'temperature': self.temperature if self.temperature is not None else 0.1, 'max_tokens': max_tokens, 'stream': False,
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


    def chat(self, messages, *, max_tokens, temperature, stream=False,
                   reasoning_effort=None, check_cancel=lambda: None, on_event=lambda **kw: None):
        payload = dict(model=self.model, messages=messages, temperature=temperature,
                       max_tokens=max_tokens, stream=stream)
        if reasoning_effort is not None:
            payload['reasoning_effort'] = reasoning_effort
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream' if stream else 'application/json'}
        if self.key: headers['Authorization'] = 'Bearer ' + self.key
        req = request.Request(self.url + '/chat/completions', data=json.dumps(payload).encode(), headers=headers)
        result = ChatResult(''); started = time.monotonic(); size = 0
        def update(data):
            if isinstance(data.get('usage'), dict): result.usage = data['usage']
            if isinstance(data.get('timings'), dict): result.timings = data['timings']
            choices = data.get('choices') or []
            choice = next((c for c in choices if c.get('index', 0) == 0), {})
            delta = choice.get('delta', {}) if stream else choice.get('message', {})
            content = delta.get('content')
            if content is not None and not isinstance(content, str):
                raise ApiError('번역 응답 content가 문자열이 아닙니다.', retryable=False)
            result.content += content or ''  # reasoning_content is deliberately excluded.
            if choice.get('finish_reason'): result.finish_reason = choice['finish_reason']
            result.seconds = max(0.000001, time.monotonic() - started)
            on_event(characters=len(result.content), usage=result.usage, timings=result.timings, seconds=result.seconds)
        try:
            check_cancel()
            with self.opener.open(req, timeout=self.timeout) as response:
                if not stream:
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024: raise ApiError('API 응답이 너무 큽니다.', retryable=False)
                    check_cancel(); update(json.loads(raw))
                else:
                    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                        raise ApiError('서버가 SSE 스트리밍을 반환하지 않습니다. 스트리밍을 끄고 다시 시도하세요.', retryable=False)
                    event = []
                    def consume():
                        if not event: return False
                        data = '\n'.join(event); event.clear()
                        if data.strip() == '[DONE]': return True
                        item = json.loads(data)
                        if item.get('error'): raise ApiError('서버가 스트리밍 오류를 반환했습니다.')
                        update(item); return False
                    while True:
                        check_cancel()
                        line = response.readline(1024 * 1024 + 1)
                        if not line:
                            consume(); break
                        size += len(line)
                        if size > 4 * 1024 * 1024: raise ApiError('API 응답이 너무 큽니다.', retryable=False)
                        line = line.decode('utf-8').rstrip('\r\n')
                        if not line:
                            if consume(): break
                        elif line.startswith('data:'):
                            event.append(line[5:].lstrip(' '))
            check_cancel()
            if not result.finish_reason:
                raise StreamInterrupted(result)
            return result
        except error.HTTPError as exc:
            issue = RequestError(exc.code)
            # Classify context errors without returning server bodies or input.
            body = exc.read(8192).decode('utf-8', errors='replace').lower()
            issue.context_overflow = exc.code in (400, 413, 422) and any(
                x in body for x in ('context', 'n_ctx', 'too many tokens', 'token limit'))
            raise issue from None
        except StreamInterrupted:
            raise
        except ApiError as exc:
            if result.content and exc.retryable:
                result.interrupted = True
                raise StreamInterrupted(result) from None
            raise
        except (error.URLError, TimeoutError, OSError, ValueError, UnicodeError, AttributeError, TypeError, http.client.HTTPException) as exc:
            if result.content:
                result.seconds = max(0.000001, time.monotonic() - started)
                result.interrupted = True
                raise StreamInterrupted(result) from None
            raise ApiError('API 연결 또는 응답 해석 실패. 서버 상태와 스트리밍 설정을 확인하세요.') from exc


    def discover(self):
        records = self.call('/models').get('data', [])
        records = [m for m in records if isinstance(m, dict) and isinstance(m.get('id'), str)
                   and 'image' not in m.get('architecture', {}).get('output_modalities', [])]
        loaded = [m for m in records if m.get('status', {}).get('value') == 'loaded' and not m.get('alias_of')]
        candidate = (loaded or records)
        if not candidate: raise ApiError('사용 가능한 텍스트 모델이 없습니다. PolyStrata에서 모델을 로드하세요.', retryable=False)
        return candidate[0]


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ID-preserving chat transport. Legacy complete()/complete_text() remain intact.
@dataclass
class ChatResult:
    content: str
    finish_reason: str = ''
    usage: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    seconds: float = 0.0
    interrupted: bool = False


class StreamInterrupted(ApiError):
    def __init__(self, partial):
        super().__init__('스트리밍 응답이 완료 전에 중단되었습니다.')
        self.partial = partial


class RequestError(ApiError):
    def __init__(self, status):
        super().__init__(f'API HTTP {status}. 주소·인증·서버 로그를 확인하세요.',
                         retryable=status in (408, 429) or status >= 500)
        self.status = status
        self.context_overflow = False
