import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from sub2loc.catalog import Catalog, CatalogError, DEFAULT_INSTRUCTIONS, GuidanceDraft, plain_text
from sub2loc.workspace import Work, Workspace, translate_queue
from test_desktop import SAMPLE, settings, translated
from test_jako import Translator, config as jako_settings

MEDIA = dict(id=42, title=dict(native='第一期', romaji='Season One', english='The First Season'),
             seasonYear=2024, format='TV', episodes=12, genres=['Drama', 'Fantasy'],
             description='<p>Two friends meet &amp; travel.</p><br>~!Secret ending!~',
             characters=dict(edges=[dict(role='MAIN', node=dict(name=dict(native='ミサキ', full='Misaki'),
                         description='A student. ~!Hidden identity!~')),
                        dict(role='SUPPORTING', node=dict(name=dict(native='アキラ', full='Akira'), description=None))]))


class ProfileServer(BaseHTTPRequestHandler):
    calls = []
    release = None
    def log_message(self, *args): pass
    def do_POST(self):
        type(self).calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        if type(self).release is not None: type(self).release.wait(5)
        payload = json.dumps({'data': {'Media': MEDIA}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(payload)


class GuidanceTests(unittest.TestCase):
    def test_http_profile_uses_only_media_id_and_keeps_original_names_and_source(self):
        ProfileServer.calls = []; ProfileServer.release = None
        server = ThreadingHTTPServer(('127.0.0.1', 0), ProfileServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            draft = Catalog(f'http://127.0.0.1:{server.server_port}').guidance('anilist:42')
            self.assertEqual(ProfileServer.calls[0]['variables'], {'id': 42})
            self.assertEqual(set(ProfileServer.calls[0]), {'query', 'variables'})
            self.assertIn('description(asHtml: false)', ProfileServer.calls[0]['query'])
            self.assertIn('第一期', draft.reference); self.assertIn('ミサキ / Misaki', draft.reference)
            self.assertIn('Two friends meet & travel.', draft.reference)
            self.assertIn('https://anilist.co/anime/42', draft.reference)
            self.assertNotIn('Secret ending', draft.reference); self.assertNotIn('Hidden identity', draft.reference)
            self.assertEqual(draft.instructions, DEFAULT_INSTRUCTIONS)
        finally:
            server.shutdown(); server.server_close()

    def test_mismatched_missing_media_and_manual_ids_never_create_a_draft(self):
        class Stub(Catalog):
            def query(self, query, variables): return {'Media': dict(MEDIA, id=43)}
        with self.assertRaises(CatalogError): Stub().guidance('anilist:42')
        for identifier in ('manual:test', 'anilist:0', 'anilist:1anything', 'anilist:-1'):
            with self.assertRaises(ValueError): Stub().guidance(identifier)
        class Sparse(Catalog):
            def query(self, query, variables): return {'Media': {'id': 42}}
        draft = Sparse().guidance('anilist:42')
        self.assertNotIn('등장인물', draft.reference)
        self.assertIn('AniList 42', draft.title)

    def test_imported_html_and_marked_spoilers_are_plain_bounded_text(self):
        text = '<p>Visible &lt;tag&gt;</p><script>bad()</script><span class="spoiler"><b>hidden</b></span><br>After'
        self.assertEqual(plain_text(text), 'Visible <tag> After')
        self.assertEqual(plain_text('Fine ~!Unclosed secret'), 'Fine')
        self.assertEqual(plain_text('<span class>Text</span>'), 'Text')
        self.assertLessEqual(len(plain_text('long' * 5000, 900)), 901)

    def test_old_workspace_load_and_rematch_preserve_user_instructions_and_imported_details(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'workspace.json'
            old = Work('anilist:42', 'Season One', notes='ミサキ=미사키', reference='old reference')
            value = dict(vars(old)); value.pop('instructions')
            path.write_text(json.dumps(dict(works={old.id: value}, entries=[], settings={})))
            ws = Workspace.load(path); self.assertEqual(ws.works[old.id].instructions, '')
            ws.works[old.id].instructions = '존댓말은 대사를 근거로 유지'
            ws.works[old.id].reference = 'Detailed imported reference'
            ws.assign([], Work(old.id, 'Season One', reference='minimal search reference'))
            ws.save(path); restored = Workspace.load(path).works[old.id]
            self.assertEqual(restored.notes, 'ミサキ=미사키')
            self.assertEqual(restored.instructions, '존댓말은 대사를 근거로 유지')
            self.assertEqual(restored.reference, 'Detailed imported reference')

    def test_custom_rules_reach_both_adapters_and_changes_invalidate_only_the_run(self):
        class Chat:
            def __init__(self): self.messages = []
            def complete(self, messages): self.messages.append(messages); return translated(messages)
        for cfg, client in ((settings(), Chat()), (jako_settings(), Translator())):
            with self.subTest(model=cfg['model']), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); source = root / '01.ass'; source.write_bytes(SAMPLE)
                ws = Workspace(); ws.add_paths([source]); work = Work.manual('My Work')
                work.notes = 'ミサキ=미사키'; work.instructions = '문장을 간결하게 번역하세요.'
                work.reference = 'Imported background'; ws.assign([0], work)
                translate_queue(ws, cfg, root/'data', root/'output', client=client)
                calls = client.messages if hasattr(client, 'messages') else client.prompts
                prompt = calls[0][0]['content'] if hasattr(client, 'messages') else calls[0]
                self.assertIn(work.instructions, prompt)
                count = len(calls)
                translate_queue(ws, cfg, root/'data', root/'output', client=client)
                self.assertEqual(len(calls), count)
                work.instructions = '의역보다 원문의 의미를 우선하세요.'
                translate_queue(ws, cfg, root/'data', root/'output', client=client)
                self.assertGreater(len(calls), count)


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialogButtonBox
    from sub2loc.desktop import MainWindow, GuidanceDialog, STYLE
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Install desktop requirements')
class GuidanceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([]); cls.app.setStyleSheet(STYLE)

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline: self.fail('Guidance UI timed out')
            QTest.qWait(10)

    def test_actual_fetch_preview_apply_after_selection_switch_preserves_notes_and_work_identity(self):
        ProfileServer.calls = []; ProfileServer.release = threading.Event()
        server = ThreadingHTTPServer(('127.0.0.1', 0), ProfileServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        timer = QTimer(); timer.setInterval(10)
        applied = []
        def apply_preview():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, GuidanceDialog):
                applied.append(dialog.reference.toPlainText())
                dialog.instructions.setPlainText('미사키의 존댓말은 원문에 맞추세요.')
                QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
                timer.stop()
        timer.timeout.connect(apply_preview)
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for name in ('01.ass', '02.ass'): (root/name).write_bytes(SAMPLE)
                window = MainWindow(root/'data'); window.show(); window.add_paths([root/'01.ass', root/'02.ass'])
                work = Work('anilist:42', 'Season One', notes='ミサキ=미사키', instructions='기존 번역 지침')
                other = Work.manual('Other work'); window.workspace.assign([0], work); window.workspace.assign([1], other)
                window.refresh(); window.rows[0].setSelected(True)
                self.assertEqual(window.instructions.toPlainText(), '기존 번역 지침')
                with patch('sub2loc.desktop.Catalog', return_value=Catalog(f'http://127.0.0.1:{server.server_port}')):
                    QTest.mouseClick(window.guidance_button, Qt.MouseButton.LeftButton)
                    self.wait_for(lambda: bool(ProfileServer.calls))
                    self.assertNotIn('Two friends', work.reference)
                    window.tree.clearSelection(); window.rows[1].setSelected(True)
                    timer.start(); ProfileServer.release.set()
                    self.wait_for(lambda: not window.background_tasks)
                self.assertEqual(len(applied), 1)
                self.assertEqual(work.notes, 'ミサキ=미사키')
                self.assertEqual(work.instructions, '미사키의 존댓말은 원문에 맞추세요.')
                self.assertIn('Two friends', work.reference)
                self.assertEqual(other.reference, ''); self.assertEqual(other.instructions, '')
                self.assertEqual(window.instructions.toPlainText(), '')
                window.tree.clearSelection(); window.rows[0].setSelected(True)
                window.instructions.setPlainText('수정한 지침')
                restored = Workspace.load(window.workspace_path)
                self.assertEqual(restored.works[work.id].instructions, '수정한 지침')
                window.close()
        finally:
            timer.stop(); ProfileServer.release.set(); server.shutdown(); server.server_close()

    def test_cancel_and_fetch_failure_leave_saved_settings_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'01.ass'; source.write_bytes(SAMPLE)
            window = MainWindow(root/'data'); window.show(); window.add_paths([source])
            work = Work('anilist:42', 'My Work', notes='고정 이름', reference='saved reference', instructions='saved rules')
            window.workspace.assign([0], work); window.refresh(); window.rows[0].setSelected(True)
            draft = GuidanceDraft(work.id, work.title, DEFAULT_INSTRUCTIONS, 'new reference')
            class Canceled(GuidanceDialog):
                def exec(self): return self.DialogCode.Rejected
            with patch('sub2loc.desktop.Catalog.guidance', return_value=draft), patch('sub2loc.desktop.GuidanceDialog', Canceled):
                QTest.mouseClick(window.guidance_button, Qt.MouseButton.LeftButton)
                self.wait_for(lambda: not window.background_tasks)
            self.assertEqual((work.instructions, work.reference), ('saved rules', 'saved reference'))
            with patch('sub2loc.desktop.Catalog.guidance', side_effect=CatalogError('offline')):
                QTest.mouseClick(window.guidance_button, Qt.MouseButton.LeftButton)
                self.wait_for(lambda: not window.background_tasks)
            self.assertEqual(window.status.text(), 'offline')
            self.assertTrue(window.guidance_button.isEnabled())
            self.assertEqual((work.instructions, work.reference), ('saved rules', 'saved reference'))
            window.close()


if __name__ == '__main__': unittest.main()
