import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error
import zipfile
from sub2loc.ass import AssDocument, protect
from sub2loc.api import Client, ApiError, base_url
from sub2loc.engine import Engine, Cancelled, parse_reply
from sub2loc.server import Application, handler, validate

SAMPLE = Path(__file__).resolve().parents[1].joinpath('examples/demo.ass').read_bytes()

def settings():
    return dict(work='테스트 작품 시즌 1', url='http://127.0.0.1:1234/v1', model='test-12b', key='SECRET-TEST-KEY',
                source='auto', target='한국어', notes='Akira=아키라', encoding='utf-8-sig',
                batch_size=1, max_tokens=4096, timeout=10)

def response(payload):
    return json.dumps({'translations': [{'id': c['id'], 'text': c['text'] + ' 번역'} for c in payload['translate']],
                       'context': payload['previous_context'] + '|인물 메모'}, ensure_ascii=False)

class FakeClient:
    def __init__(self): self.calls = []
    def complete(self, messages):
        payload = json.loads(messages[1]['content'])
        self.calls.append(payload)
        return response(payload)

@contextmanager
def serving(factory):
    server = ThreadingHTTPServer(('127.0.0.1', 0), factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

class AssTests(unittest.TestCase):
    def test_exact_identity_utf8_bom_crlf_and_no_final_newline(self):
        for raw in (SAMPLE, b'\xef\xbb\xbf' + SAMPLE.replace(b'\n', b'\r\n'), SAMPLE.rstrip(b'\n')):
            doc = AssDocument(raw)
            self.assertEqual(doc.render({str(c.id): c.text for c in doc.cues}), raw)
    def test_utf16_and_cp932_identity(self):
        for encoding in ('utf-16', 'cp932'):
            raw = SAMPLE.decode().encode(encoding)
            doc = AssDocument(raw, encoding)
            self.assertEqual(doc.render({str(c.id): c.text for c in doc.cues}), raw)
        raw = b'\xfe\xff' + SAMPLE.decode().encode('utf-16-be')
        doc = AssDocument(raw, 'utf-16')
        self.assertEqual(doc.render({str(c.id): c.text for c in doc.cues}), raw)
    def test_only_text_changes(self):
        doc = AssDocument(SAMPLE)
        self.assertEqual(len(doc.cues), 2)
        output = doc.render({str(c.id): c.text + ' 번역' for c in doc.cues}).decode().splitlines()
        source = SAMPLE.decode().splitlines()
        changed_lines = {c.line for c in doc.cues}
        for i, (before, after) in enumerate(zip(source, output)):
            if i in changed_lines: self.assertEqual(before.split(',', 9)[:9], after.split(',', 9)[:9])
            else: self.assertEqual(before, after)
    def test_protection_drawing_mixed_and_literal_placeholder(self):
        text = r'{\p1}m 0 0 l 20 20{\p0}Hello\N{\i1}world{\i0}\h⟪S2L:99⟫'
        masked, controls = protect(text)
        self.assertNotIn('m 0 0', masked)
        cue = AssDocument(SAMPLE).cues[0]
        cue.text, cue.protected = masked, controls
        self.assertEqual(cue.restore(masked), text)
    def test_bad_tokens_and_new_markup_rejected(self):
        cue = AssDocument(SAMPLE).cues[0]
        for bad in ('번역', cue.text + '⟪S2L:0⟫', cue.text.replace('⟪S2L:0⟫', '⟪S2L:9⟫'),
                    cue.text + r'{\b1}', cue.text + '\n', cue.text + r'\N'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): cue.restore(bad)
    def test_invalid_format_and_missing_translations(self):
        with self.assertRaises(ValueError): AssDocument(b'[Events]\nDialogue: 0,hello\n')
        with self.assertRaises(ValueError): AssDocument(b'[Events]\nFormat: Text, Start\n')
        with self.assertRaises(ValueError): AssDocument(SAMPLE).render({})
    def test_text_only_format_and_literal_braces(self):
        raw = b'[Events]\nFormat: Text\nDialogue:hello { literal } and {open\n'
        doc = AssDocument(raw)
        self.assertEqual(doc.render({str(c.id): c.text for c in doc.cues}), raw)
    def test_reply_ids_strict_and_fence(self):
        cues = AssDocument(SAMPLE).cues[:1]
        valid = {'translations': [{'id': 0, 'text': cues[0].text}], 'context': 'memo'}
        self.assertEqual(parse_reply('```json\n' + json.dumps(valid) + '\n```', cues)[1], 'memo')
        for value in ('0', True, 1):
            valid['translations'][0]['id'] = value
            with self.assertRaises(ValueError): parse_reply(json.dumps(valid), cues)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name))
    def test_context_checkpoints_and_key_not_saved(self):
        client, config = FakeClient(), settings()
        files = [('01.ass', SAMPLE), ('02.ass', SAMPLE)]
        outputs, context = self.engine.translate(config, files, client=client)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[2]['previous_context'], '|인물 메모|인물 메모')
        self.assertEqual(client.calls[0]['fixed_notes'], 'Akira=아키라')
        self.assertEqual(client.calls[0]['translate'][0]['speaker'], 'Akira')
        for p in Path(self.temp.name).rglob('*.json'): self.assertNotIn('SECRET-TEST-KEY', p.read_text('utf-8'))
        config['key'] = 'rotated-key'
        self.assertEqual(self.engine.translate(config, files, client=client), (outputs, context))
        self.assertEqual(len(client.calls), 4)
        later = FakeClient()
        self.engine.translate(config, [('03.ass', SAMPLE)], client=later)
        self.assertEqual(later.calls[0]['previous_context'], context)
        self.engine.translate(config, files, client=client)
        last = FakeClient()
        self.engine.translate(config, [('04.ass', SAMPLE)], client=last)
        self.assertEqual(last.calls[0]['previous_context'], context + '|인물 메모|인물 메모')
    def test_cancel_resume_at_saved_chunk(self):
        cancel = threading.Event()
        class Cancelling(FakeClient):
            def complete(self, messages):
                result = super().complete(messages)
                cancel.set()
                return result
        first = Cancelling()
        with self.assertRaises(Cancelled): self.engine.translate(settings(), [('01.ass', SAMPLE)], cancel, client=first)
        self.assertEqual(len(first.calls), 1)
        resumed = FakeClient()
        self.engine.translate(settings(), [('01.ass', SAMPLE)], client=resumed)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(resumed.calls[0]['translate'][0]['id'], 1)
        self.assertEqual(resumed.calls[0]['previous_context'], '|인물 메모')
    def test_invalid_reply_retry_no_bad_checkpoint(self):
        class Broken(FakeClient):
            def complete(self, messages):
                super().complete(messages)
                return '{"translations":[],"context":"wrong"}'
        client = Broken()
        with self.assertRaises(ValueError): self.engine.translate(settings(), [('01.ass', SAMPLE)], client=client)
        self.assertEqual(len(client.calls), 3)
        self.assertFalse(list(Path(self.temp.name).rglob('*.json')))
        fixed = FakeClient()
        self.engine.translate(settings(), [('01.ass', SAMPLE)], client=fixed)
        self.assertEqual(fixed.calls[0]['previous_context'], '')
    def test_work_isolated_all_files_prevalidated(self):
        self.engine.translate(settings(), [('01.ass', SAMPLE)], client=FakeClient())
        config = settings(); config['work'] = '다른 작품'
        client = FakeClient()
        self.engine.translate(config, [('01.ass', SAMPLE)], client=client)
        self.assertEqual(client.calls[0]['previous_context'], '')
        client = FakeClient()
        with self.assertRaises(ValueError): self.engine.translate(settings(), [('01.ass', SAMPLE), ('bad.ass', b'not ASS')], client=client)
        self.assertEqual(client.calls, [])
    def test_nonrepresentable_encoding_not_checkpointed(self):
        config = settings(); config['encoding'] = 'cp932'
        with self.assertRaises(UnicodeEncodeError):
            self.engine.translate(config, [('01.ass', SAMPLE.decode().encode('cp932'))], client=FakeClient())
        self.assertFalse(list(Path(self.temp.name).rglob('*.json')))

class ApiTests(unittest.TestCase):
    def test_models_chat_payload_and_length_failure(self):
        calls = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"data":[{"id":"local-12b"}]}')
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, self.headers.get('Authorization'), data))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'message':{'content':'{}'}, 'finish_reason':'length' if len(calls)>1 else 'stop'}]}).encode())
        with serving(Model) as (_, url):
            client = Client(url + '/v1', 'local-12b', 'test-key')
            self.assertEqual(client.models(), ['local-12b'])
            self.assertEqual(client.complete([]), '{}')
            with self.assertRaises(ApiError): client.complete([])
        self.assertEqual(calls[0][0], '/v1/chat/completions')
        self.assertEqual(calls[0][1], 'Bearer test-key')
        self.assertFalse(calls[0][2]['stream'])
    def test_invalid_urls(self):
        for url in ('file:///tmp/x', 'http://x?key=secret', 'http://u:p@localhost', 'http://x/#a'):
            with self.assertRaises(ValueError): base_url(url)

class ServerTests(unittest.TestCase):
    def test_upload_validation(self):
        data = settings(); data['files'] = [{'name':'one.ass','data':base64.b64encode(SAMPLE).decode()}]
        self.assertEqual(validate(data)[1][0][1], SAMPLE)
        for name in ('../bad.ass', 'a\\b.ass', 'x.txt', 'line\nbreak.ass'):
            data['files'][0]['name'] = name
            with self.assertRaises(ValueError): validate(data)
        data['files'][0]['name'] = 'one.ass'; data['batch_size'] = True
        with self.assertRaises(ValueError): validate(data)
    def test_web_to_http_model_to_zip(self):
        seen = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(payload)
                result = response(json.loads(payload['messages'][1]['content']))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'message':{'content':result},'finish_reason':'stop'}]}).encode())
        with tempfile.TemporaryDirectory() as tmp, serving(Model) as (_, model_url):
            app = Application(Path(tmp))
            with serving(handler(app)) as (_, url):
                with request.urlopen(url) as r: self.assertIn('한 작품의 맥락'.encode(), r.read())
                data = settings(); data['url'] = model_url + '/v1'
                data['files'] = [{'name':'01.ass','data':base64.b64encode(SAMPLE).decode()}]
                req = request.Request(url+'/api/start', json.dumps(data).encode(), headers={'X-Sub2loc':'1','Content-Type':'application/json'})
                with request.urlopen(req) as r: self.assertEqual(r.status, 202)
                app.thread.join(timeout=10)
                self.assertFalse(app.thread.is_alive())
                with request.urlopen(url+'/api/status') as r: self.assertEqual(json.load(r)['state'], 'complete')
                with request.urlopen(url+'/api/download') as r: archive = zipfile.ZipFile(io.BytesIO(r.read()))
                self.assertEqual(set(archive.namelist()), {'01.translated.ass','context.txt'})
                self.assertIn('번역'.encode(), archive.read('01.translated.ass'))
                for headers in ({}, {'X-Sub2loc':'1','Origin':'https://evil.example'}, {'X-Sub2loc':'1','Host':'evil.example'}):
                    req = request.Request(url+'/api/stop', b'{}', headers=headers)
                    with self.assertRaises(error.HTTPError) as exc: request.urlopen(req)
                    self.assertEqual(exc.exception.code, 403)
        self.assertEqual(len(seen), 2)

if __name__ == '__main__': unittest.main()
