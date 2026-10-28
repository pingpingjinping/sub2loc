"""Loopback-only web UI. Run: python -m sub2loc"""
import argparse
import base64
import binascii
import io
import json
import os
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import zipfile
from .api import Client, base_url
from .engine import Engine, Cancelled

ENCODINGS = {'utf-8-sig', 'utf-16', 'cp932', 'cp949'}


def validate(data):
    def string(key, limit, default=''):
        value = data.get(key, default)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{key} 입력이 올바르지 않습니다.')
        return value.strip()

    def integer(key, default, low, high):
        value = data.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key}: {low}–{high} 범위의 정수를 입력하세요.')
        return value

    config = {'work': string('work', 200), 'url': base_url(string('url', 2000)),
              'model': string('model', 200), 'key': string('key', 4000),
              'source': string('source', 100, 'auto'), 'target': string('target', 100, '한국어'),
              'notes': string('notes', 5000), 'encoding': string('encoding', 40, 'utf-8-sig'),
              'batch_size': integer('batch_size', 12, 1, 64),
              'context_tokens': integer('context_tokens', 4096, 1024, 131072),
              'timeout': integer('timeout', 180, 5, 1800),
              'max_tokens': integer('max_tokens', 4096, 256, 32768)}
    if not config['work'] or not config['model'] or not config['target']:
        raise ValueError('작품명·모델명·목표 언어를 입력하세요.')
    if config['encoding'] not in ENCODINGS:
        raise ValueError('지원하지 않는 인코딩입니다.')
    items = data.get('files')
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError('ASS 파일을 1–50개 선택하세요.')
    files, names = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('파일 입력이 올바르지 않습니다.')
        name = item.get('name', '')
        if not isinstance(name, str) or not name.lower().endswith('.ass') or len(name) > 240 or re.search(r'[/\\\x00-\x1f]', name):
            raise ValueError('파일명이 올바르지 않습니다. .ass 파일을 사용하세요.')
        if name.casefold() in names or Path(name).stem.casefold() in names:
            raise ValueError('중복 파일명이 있습니다.')
        names.update((name.casefold(), Path(name).stem.casefold()))
        encoded = item.get('data', '')
        if not isinstance(encoded, str):
            raise ValueError('파일 데이터가 올바르지 않습니다.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('파일 데이터가 올바르지 않습니다.') from None
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise ValueError('각 파일은 1바이트–4MB여야 합니다.')
        files.append((name, raw))
    return config, files


class Application:
    def __init__(self, root):
        self.engine = Engine(root)
        self.guard = threading.Lock()
        self.cancel = threading.Event()
        self.status = {'state': 'idle', 'message': '파일을 선택하면 시작할 수 있어요.', 'done': 0, 'total': 0}
        self.archive = None
        self.thread = None

    def update(self, **kwargs):
        with self.guard:
            self.status.update(kwargs)

    def start(self, config, files):
        with self.guard:
            if self.status['state'] == 'running':
                raise ValueError('이미 번역 중입니다.')
            self.cancel = threading.Event()
            self.archive = None
            self.status = {'state': 'running', 'message': 'ASS 파일을 확인하고 있습니다.', 'done': 0, 'total': 0}
        def run():
            try:
                outputs, context = self.engine.translate(config, files, self.cancel, self.update)
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for name, raw in outputs:
                        archive.writestr(name, raw)
                    archive.writestr('context.txt', context)
                with self.guard:
                    self.archive = buffer.getvalue()
                    self.status.update(state='complete', context=context, message='완료! 번역된 ASS 파일을 내려받으세요.')
            except Cancelled as exc:
                self.update(state='cancelled', message=str(exc))
            except (ValueError, UnicodeError) as exc:
                self.update(state='error', message=str(exc))
            except Exception as exc:
                # API errors contain a safe user-facing message; unexpected exceptions stay terse.
                from .api import ApiError
                self.update(state='error', message=str(exc) if isinstance(exc, ApiError) else
                            '처리에 실패했습니다. 저장 폴더의 쓰기 권한과 API 서버를 확인하세요.')
            finally:
                config.pop('key', None)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not print filenames, subtitles, API keys, or request bodies.
            pass

        def trusted(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed:
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + h for h in allowed}:
                return False
            return True

        def send(self, code, body, mime='application/json; charset=utf-8', download=False):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="sub2loc-translated.zip"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.trusted():
                self.send(403, {'error': '로컬 주소로 접속하세요.'})
                return
            path = urlsplit(self.path).path
            if path == '/':
                self.send(200, Path(__file__).with_name('ui.html').read_bytes(), 'text/html; charset=utf-8')
            elif path == '/api/status':
                with app.guard:
                    status = app.status.copy()
                self.send(200, status)
            elif path == '/api/projects':
                projects = []
                for p in app.engine.root.glob('*/project.json'):
                    try:
                        projects.append(json.loads(p.read_text('utf-8'))['work'])
                    except (OSError, ValueError, KeyError):
                        continue
                self.send(200, {'projects': sorted(projects)})
            elif path == '/api/download':
                with app.guard:
                    archive = app.archive
                if archive is None:
                    self.send(404, {'error': '완료된 번역이 없습니다.'})
                else:
                    self.send(200, archive, 'application/zip', True)
            else:
                self.send(404, {'error': '찾을 수 없습니다.'})

        def do_POST(self):
            if not self.trusted() or self.headers.get('X-Sub2loc') != '1':
                self.send(403, {'error': '허용되지 않은 요청입니다.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 32 * 1024 * 1024:
                    raise ValueError('전체 업로드 크기는 32MB 이하여야 합니다.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('JSON 객체가 필요합니다.')
                if self.path == '/api/start':
                    config, files = validate(data)
                    app.start(config, files)
                    self.send(202, {'ok': True})
                elif self.path == '/api/stop':
                    app.cancel.set()
                    self.send(200, {'ok': True})
                elif self.path == '/api/models':
                    url, key = data.get('url'), data.get('key', '')
                    if not isinstance(url, str) or not isinstance(key, str):
                        raise ValueError('API 주소와 키를 확인하세요.')
                    self.send(200, {'models': Client(url, key=key, timeout=10).models()})
                else:
                    self.send(404, {'error': '찾을 수 없습니다.'})
            except (ValueError, UnicodeError) as exc:
                self.send(400, {'error': str(exc)})
            except Exception:
                self.send(502, {'error': 'API 연결에 실패했습니다. 주소와 서버 상태를 확인하세요.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='작품 맥락을 유지하는 로컬 ASS 자막 번역기')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('SUB2LOC_DATA', Path.home() / '.sub2loc')))
    args = parser.parse_args()
    app = Application(args.data_dir)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(app))
    server.daemon_threads = True
    print(f'sub2loc: http://127.0.0.1:{server.server_port}\n종료: Ctrl+C', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.cancel.set()
    finally:
        server.server_close()
