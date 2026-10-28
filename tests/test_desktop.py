import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sub2loc.workspace import Entry, Work, Workspace, translate_queue, write_result
from sub2loc.catalog import Catalog, CatalogError

SAMPLE = Path(__file__).resolve().parents[1].joinpath('examples/demo.ass').read_bytes()


def settings():
    return dict(url='http://127.0.0.1:1234/v1', model='local-12b', key='private-test-key', source='auto',
                target='한국어', encoding='utf-8-sig', batch_size=1, max_tokens=4096, timeout=5)


def translated(messages):
    data = json.loads(messages[1]['content'])
    return json.dumps({'translations': [{'id': row['id'], 'text': row['text'] + ' 번역'} for row in data['translate']],
                       'context': data['previous_context'] + '|memory'})


class FakeClient:
    def __init__(self): self.calls = []
    def complete(self, messages):
        self.calls.append(json.loads(messages[1]['content']))
        return translated(messages)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ('A/02.ass', 'A/01.ass', 'B/01.ass', 'A/old.translated.ass', 'A/ignored.txt'):
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(SAMPLE)

    def test_recursive_natural_order_duplicates_and_outputs(self):
        ws = Workspace(); count, errors = ws.add_paths([self.root])
        self.assertEqual(count, 3); self.assertEqual(errors, [])
        self.assertEqual([Path(e.path).relative_to(self.root).as_posix() for e in ws.entries], ['A/01.ass', 'A/02.ass', 'B/01.ass'])
        self.assertEqual(ws.add_paths([self.root / 'A', self.root / 'B/01.ass'])[0], 0)
        ws = Workspace(); ws.add_paths([self.root / 'A/01.ass', self.root / 'B/01.ass'])
        ws.add_paths([self.root / 'A/02.ass'])
        self.assertEqual([Path(e.path).parent.name for e in ws.entries], ['A', 'A', 'B'])

    def test_matching_save_restore_and_key_exclusion(self):
        ws = Workspace(); ws.add_paths([self.root]); work = Work.manual('테스트 · 시즌 1'); work.notes = '고정 이름'
        ws.assign([0, 1], work); ws.assign([2], Work.manual('다른 작품'))
        ws.assign([0], Work.manual('테스트 · 시즌 1'))
        self.assertEqual(ws.works[work.id].notes, '고정 이름')
        ws.settings = settings(); ws.entries[0].status = '번역 중'
        ws.save(self.root / 'state.json'); restored = Workspace.load(self.root / 'state.json')
        self.assertEqual(restored.entries[0].status, '준비')
        self.assertNotIn('private-test-key', (self.root / 'state.json').read_text())
        self.assertEqual([w.title for w, _ in restored.groups()], ['테스트 · 시즌 1', '다른 작품'])

    def test_duplicate_names_output_safety_context_and_resume(self):
        ws = Workspace(); ws.add_paths([self.root]); work = Work.manual('CON / 작품')
        ws.assign([0, 1, 2], work); client = FakeClient()
        output = self.root / 'output'
        results = translate_queue(ws, settings(), self.root / 'state', output, client=client)
        self.assertEqual(len(results), 3); self.assertEqual(len(set(results)), 3)
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(client.calls[2]['previous_context'], '|memory|memory')
        self.assertTrue(all(Path(p).is_file() for p in results))
        self.assertEqual((self.root / 'A/01.ass').read_bytes(), SAMPLE)
        self.assertEqual(translate_queue(ws, settings(), self.root / 'state', output, client=client), results)
        self.assertEqual(len(client.calls), 6)
        before = Path(results[0]).read_bytes()
        self.assertNotEqual(write_result(Path(results[0]), b'changed'), Path(results[0]))
        self.assertEqual(Path(results[0]).read_bytes(), before)
        # Same titles with different catalog IDs must have separate continuity stores.
        other = Work('anilist:1', work.title)
        ws.assign([0, 1, 2], other); other_client = FakeClient()
        translate_queue(ws, settings(), self.root / 'state', output, client=other_client)
        self.assertEqual(other_client.calls[0]['previous_context'], '')

    def test_unmatched_and_invalid_later_file_never_call_api(self):
        ws = Workspace(); ws.add_paths([self.root]); client = FakeClient()
        with self.assertRaises(ValueError): translate_queue(ws, settings(), self.root / 'state', self.root / 'out', client=client)
        ws.assign(range(3), Work.manual('Test'))
        (self.root / 'B/01.ass').write_bytes(b'invalid')
        with self.assertRaises(ValueError): translate_queue(ws, settings(), self.root / 'state', self.root / 'out', client=client)
        self.assertEqual(client.calls, [])


class CatalogTests(unittest.TestCase):
    def test_search_and_character_mapping(self):
        class Stub(Catalog):
            def query(self, query, variables):
                if 'search' in variables:
                    return {'Page': {'media': [{'id': 42, 'title': {'romaji': 'Season 2', 'native': '第二期'},
                                               'seasonYear': 2024, 'format': 'TV', 'episodes': 12}]}}
                return {'Media': {'characters': {'nodes': [{'name': {'full': 'Akira', 'native': 'アキラ'}}]}}}
        catalog = Stub(); work = catalog.search('Season')[0]
        self.assertEqual((work.id, work.native, work.year, work.episodes), ('anilist:42', '第二期', '2024', '12'))
        self.assertIn('アキラ / Akira', catalog.character_notes(work.id))
        with self.assertRaises(ValueError): catalog.search('')
        with self.assertRaises(ValueError): catalog.character_notes('manual:test')


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog
    from sub2loc.desktop import MainWindow, MatchDialog, STYLE
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Install requirements-desktop.txt for native UI tests')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion'); cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.files = self.root / '字幕'; self.files.mkdir()
        for name in ('01.ass', '02.ass'): (self.files / name).write_bytes(SAMPLE)
        self.window = MainWindow(self.root / 'appdata'); self.window.show()
        self.addCleanup(self.window.close)

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline: self.fail('GUI task timed out')
            QTest.qWait(10)

    def test_folder_selection_matching_notes_reorder_and_restore(self):
        with patch('sub2loc.desktop.QFileDialog.getExistingDirectory', return_value=str(self.files)):
            QTest.mouseClick(self.window.folder_button, Qt.MouseButton.LeftButton)
        parent = self.window.tree.topLevelItem(0); parent.setSelected(True)
        work = Work.manual('테스트 시즌 1')
        class Accepted:
            selected_work = work
            def __init__(self, *args, **kwargs): pass
            def exec(self): return QDialog.DialogCode.Accepted
        with patch('sub2loc.desktop.MatchDialog', Accepted):
            QTest.mouseClick(self.window.match_button, Qt.MouseButton.LeftButton)
        self.assertTrue(all(e.work_id == work.id for e in self.window.workspace.entries))
        self.window.notes.setPlainText('아키라=반말')
        self.assertEqual(self.window.workspace.works[work.id].notes, '아키라=반말')
        self.window.tree.clearSelection(); self.window.rows[1].setSelected(True)
        QTest.mouseClick(self.window.up, Qt.MouseButton.LeftButton)
        self.assertEqual(Path(self.window.workspace.entries[0].path).name, '02.ass')
        restored = Workspace.load(self.window.workspace_path)
        self.assertEqual(restored.works[work.id].notes, '아키라=반말')
        self.assertEqual(Path(restored.entries[0].path).name, '02.ass')

    def test_search_dialog_explicit_choice_and_manual(self):
        work = Work('anilist:42', 'Anime · Season 2', '第二期', '2024', 'TV', '12')
        class Stub:
            def search(self, query): return [work]
        dialog = MatchDialog({}, catalog=Stub(), parent=self.window); dialog.show()
        dialog.query.setText('Anime')
        QTest.mouseClick(dialog.search_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: dialog.task is None)
        self.assertEqual(dialog.results.count(), 1)
        self.assertIsNone(dialog.selected_work)
        dialog.results.setCurrentRow(0); dialog.accept_work()
        self.assertEqual(dialog.selected_work.id, 'anilist:42')
        manual = MatchDialog({}, parent=self.window); manual.tabs.setCurrentIndex(2)
        manual.manual.setText('직접 등록'); manual.accept_work()
        self.assertEqual(manual.selected_work.title, '직접 등록')

    def test_native_ui_to_real_http_api_and_output_files(self):
        calls = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(data)
                payload = {'choices': [{'message': {'content': translated(data['messages'])}, 'finish_reason': 'stop'}]}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.window.add_paths([self.files]); self.window.workspace.assign([0, 1], Work.manual('API test'))
        self.window.refresh(); self.window.url.setText(f'http://127.0.0.1:{server.server_port}/v1')
        self.window.model.setCurrentText('local-12b'); self.window.key.setText('private-test-key')
        self.window.output.setText(str(self.root / 'results'))
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: self.window.worker is None)
        self.assertEqual(len(self.window.results), 2)
        self.assertTrue(all(e.status == '완료' for e in self.window.workspace.entries))
        self.assertTrue(all('번역'.encode() in Path(p).read_bytes() for p in self.window.results))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('private-test-key', self.window.workspace_path.read_text())

    def test_output_format_choice_persists_and_is_locked_during_translation(self):
        self.assertEqual(self.window.settings()['output_format'], 'ass')
        self.assertTrue(self.window.settings()['use_summary'])
        self.window.use_summary.setChecked(False)
        self.window.strip_ass_decoration.setChecked(True)
        self.window.output_format.setCurrentIndex(self.window.output_format.findData('smi'))
        self.assertFalse(self.window.strip_ass_decoration.isEnabled())
        self.assertIn('보호 토큰 없이', self.window.format_hint.text())
        self.window.set_busy(True)
        self.assertFalse(self.window.output_format.isEnabled())
        self.window.set_busy(False); self.window.save()
        restored = MainWindow(self.root / 'appdata')
        try:
            self.assertEqual(restored.settings()['output_format'], 'smi')
            self.assertFalse(restored.settings()['use_summary'])
            self.assertTrue(restored.settings()['strip_ass_decoration'])
            restored.output_format.setCurrentIndex(restored.output_format.findData('ass'))
            self.assertTrue(restored.strip_ass_decoration.isEnabled())
            self.assertTrue(restored.strip_ass_decoration.isChecked())
        finally:restored.close()

    def test_native_format_selection_to_http_api_and_srt_smi_exports(self):
        calls = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data = json.loads(request['messages'][-1]['content']);calls.append(data)
                content = '\n'.join(json.dumps(dict(id=r['id'],text='안녕, 반가워요!'),ensure_ascii=False) for r in data['translate'])
                body = json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}]}).encode()
                self.send_response(200);self.end_headers();self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1',0),Model)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.window.apply_polystrata();self.window.streaming.setChecked(False)
        self.window.add_paths([self.files / '01.ass'])
        self.window.workspace.assign([0],Work.manual('Plain API'));self.window.refresh()
        self.window.url.setText(f'http://127.0.0.1:{server.server_port}/v1')
        self.window.model.setCurrentText('mock-model');self.window.output.setText(str(self.root/'out'))
        for fmt in ('srt','smi'):
            self.window.output_format.setCurrentIndex(self.window.output_format.findData(fmt))
            QTest.mouseClick(self.window.start_button,Qt.MouseButton.LeftButton)
            self.wait_for(lambda:self.window.worker is None)
            self.assertEqual(len(self.window.results),1,self.window.status.text())
            output=Path(self.window.results[0]);self.assertEqual(output.suffix,'.'+fmt)
            self.assertIn('안녕, 반가워요!',output.read_text('utf-8-sig'))
        self.assertEqual(len(calls),1)  # Switching the renderer reuses validated plain text.
        self.assertNotIn('⟪S2L:',json.dumps(calls,ensure_ascii=False))
        self.assertEqual((self.files/'01.ass').read_bytes(),SAMPLE)


if __name__ == '__main__': unittest.main()
