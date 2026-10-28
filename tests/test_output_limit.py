import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sub2loc.api import Client, OutputLimitError
from sub2loc.engine import Engine
from test_jako import SAMPLE, TRANSLATIONS, Translator, config, translate_sample


class OutputLimitTests(unittest.TestCase):
    def test_bounded_preview_usage_and_credential_redaction(self):
        key = 'PRIVATE-CREDENTIAL'
        exc = OutputLimitError(1024, key + '내용' * 1000 + key,
                               {'prompt_tokens': 123, 'completion_tokens': 1024, 'unexpected': key}, key)
        exc.with_source('원문 ' + key, key)
        text = str(exc)
        self.assertNotIn(key, text)
        self.assertIn('1024', text)
        self.assertIn('prompt_tokens=123', text)
        self.assertIn('API 키 삭제', text)
        self.assertIn('중간 생략', text)
        self.assertLess(len(text), 1200)
        self.assertFalse(exc.retryable)

    def test_limit_stops_once_preserves_checkpoint_and_resumes(self):
        class Limited(Translator):
            def complete_text(self, prompt, limit):
                self.prompts.append(prompt)
                source = prompt.split('<start_of_turn>user\n')[1].split('<end_of_turn>')[0]
                if 'アキラ' in source:
                    raise OutputLimitError(limit, '번역 반복 ' * 300,
                                           {'prompt_tokens': 200, 'completion_tokens': limit})
                return translate_sample(source)
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); limited = Limited(); cfg = config(); cfg['batch_size'] = 1
            with self.assertRaises(OutputLimitError) as caught:
                engine.translate(cfg, [('episode.ass', SAMPLE)], client=limited)
            self.assertEqual(len(limited.prompts), 4)  # A smaller changed request after the two-span failure.
            self.assertEqual(caught.exception.filename, 'episode.ass')
            self.assertEqual(caught.exception.first, 2)
            self.assertIn('アキラ', str(caught.exception))
            checkpoints = list(Path(folder).glob('*/runs/*.json'))
            self.assertEqual(len(checkpoints), 1)
            state = json.loads(checkpoints[0].read_text())
            self.assertEqual(state['offset'], 1)
            self.assertEqual(set(state['translations']), {'0'})
            self.assertEqual(state['outputs'], [])
            recovered = Translator()
            outputs, _ = engine.translate(cfg, [('episode.ass', SAMPLE)], client=recovered)
            self.assertEqual(len(recovered.prompts), 1)
            self.assertIn('좋은 아침'.encode(), outputs[0][1])

    def test_real_http_response_keeps_diagnostic_text_and_token_counts(self):
        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body = {'choices': [{'text': '첫 번역\n다음 내용 반복', 'finish_reason': 'length'}],
                        'usage': {'prompt_tokens': 42, 'completion_tokens': 64}}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = Client(f'http://127.0.0.1:{server.server_port}/v1', 'model')
            with self.assertRaises(OutputLimitError) as caught:
                client.complete_text('prompt', 64)
            self.assertIn('첫 번역', str(caught.exception))
            self.assertIn('completion_tokens=64', str(caught.exception))
            self.assertIn('prompt_tokens=42', str(caught.exception))
        finally:
            server.shutdown(); server.server_close()


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from sub2loc.desktop import MainWindow, STYLE
    from sub2loc.workspace import Work
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Install desktop requirements')
class OutputLimitUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([]); cls.app.setStyleSheet(STYLE)

    def test_file_failure_shows_copyable_details_without_expanding_footer(self):
        requests = []
        key = 'PRIVATE-CREDENTIAL'
        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = {'choices': [{'text': '번역 ' + key + '\n반복 ' * 200, 'finish_reason': 'length'}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 1024}}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder); source = root / 'my-episode.ass'; source.write_bytes(SAMPLE)
                window = MainWindow(root / 'data'); window.show(); window.add_paths([source])
                window.workspace.assign([0], Work.manual('내 작품')); window.refresh()
                window.url.setText(f'http://127.0.0.1:{server.server_port}/v1')
                window.model.setCurrentText(config()['model']); window.key.setText(key); window.batch.setValue(1)
                window.output.setText(str(root / 'output'))
                QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
                deadline = time.monotonic() + 10
                while window.worker is not None:
                    if time.monotonic() > deadline: self.fail('Error UI timed out')
                    QTest.qWait(10)
                self.assertEqual(len(requests), 2)
                details = window.probe_result.toPlainText()
                self.assertIn('my-episode.ass', details)
                self.assertIn('おはよう、ミサキ。', details)
                self.assertIn('생성된 출력', details)
                self.assertNotIn(key, details)
                self.assertEqual(window.settings_tabs.currentIndex(), 1)
                self.assertNotIn('\n', window.status.text())
                self.assertLess(len(window.status.text()), 150)
                self.assertEqual(window.workspace.entries[0].status, '오류')
                QTest.mouseClick(window.copy_result_button, Qt.MouseButton.LeftButton)
                self.assertEqual(QApplication.clipboard().text(), details)
                self.assertEqual(list((root / 'output').rglob('*.ass')), [])
                window.close()
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__': unittest.main()
