import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sub2loc.api import ApiError, ChatResult
from sub2loc.balloon import BalloonDocument
from sub2loc.engine import Cancelled
from sub2loc.ocr_review import ReviewSession
from sub2loc.profiles import DEFAULT_TEMPLATE, LEGACY_TEMPLATE, MANGA_TEMPLATE, template_for
from test_identified import config

SOURCE = '### a.png\n\n1. 一緒に一緒に一緒に一緒に\n\n2. 猫が来た。\n\n3. \n\n\n### b.png\n\n1. 犬が来た。\n'


def suspect(row, candidate='一緒に来た。'):
    return dict(id=row['id'], reason='文字列が繰り返されています。画像未確認です。', candidate=candidate, confidence='low')


class Model:
    def __init__(self, callback=None): self.calls = []; self.callback = callback
    def chat(self, messages, **kwargs):
        data = json.loads(messages[-1]['content']); self.calls.append((data, kwargs, messages))
        if self.callback: return self.callback(data)
        if 'rows' in data:
            return ChatResult(json.dumps(dict(suspects=[suspect(data['rows'][0])]), ensure_ascii=False), 'stop')
        return ChatResult(json.dumps(dict(id=data['translate'][0]['id'], text='같이 왔어.'), ensure_ascii=False), 'stop')


class OcrReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'book_source.txt'; self.path.write_text(SOURCE, encoding='utf-8')
        self.session = ReviewSession(self.path, self.root/'data')
        self.translation = self.root/'book_source.translated.txt'
        self.translation.write_bytes(self.session.doc.render({'0':'함께 함께 함께 함께', '1':'고양이가 왔다.', '2':'개가 왔다.'}))
        self.session.load_translation(self.translation)

    def test_preserved_structure_and_source_repeat_is_warning_only(self):
        self.assertTrue(self.session.issues(0)); self.assertFalse(self.session.issues(1))
        before = self.translation.read_bytes()
        self.session.keep(0)
        restored = ReviewSession(self.path, self.root/'data')
        self.assertEqual(restored.rows['0']['decision'], 'keep')
        self.assertTrue(restored.issues(0))
        self.assertEqual(self.path.read_text('utf-8'), SOURCE)
        self.assertEqual(self.translation.read_bytes(), before)

    def test_candidate_is_unapplied_and_review_thinking_differs_from_translation(self):
        model = Model(); cfg = config(ocr_thinking='low', thinking='none')
        self.session.scan(cfg, indices=[0], client=model)
        self.assertEqual(model.calls[0][1]['reasoning_effort'], 'low')
        self.assertIsNone(model.calls[0][1]['thinking_enabled'])
        self.assertEqual(self.session.rows['0']['candidate'], '一緒に来た。')
        self.assertEqual(self.session.current_translation(0), '함께 함께 함께 함께')
        self.assertTrue(all(r['page']=='a.png' for r in model.calls[0][0]['rows']))
        self.assertEqual([x['block'] for x in model.calls[0][0]['rows'][0]['context']], [2])
        result = self.session.retranslate(0, '一緒に来た。', cfg, client=model)
        self.assertEqual(model.calls[-1][1]['reasoning_effort'], 'none')
        self.assertFalse(model.calls[-1][1]['thinking_enabled'])
        self.assertEqual(len(model.calls[-1][0]['translate']), 1)
        self.assertIn(MANGA_TEMPLATE, model.calls[-1][2][0]['content'])
        old = self.translation.read_bytes()
        saved = self.session.apply(0, result['candidate'], result['text'])
        self.assertNotEqual(saved, self.translation)
        self.assertEqual(self.translation.read_bytes(), old)
        self.assertEqual(self.path.read_text('utf-8'), SOURCE)
        doc = BalloonDocument(saved.read_bytes())
        self.assertEqual([c.text for c in doc.cues], ['같이 왔어.', '고양이가 왔다.', '개가 왔다.'])
        self.assertEqual([len(p['blocks']) for p in doc.pages], [3,1])
        report = json.loads(saved.with_suffix('.ocr-review.json').read_text('utf-8'))
        self.assertFalse(report['image_verified']); self.assertEqual(report['rows']['0']['decision'], 'applied')

    def test_bad_ids_duplicate_keys_and_truncation_never_edit_files(self):
        bad = [lambda d:json.dumps({'suspects':[dict(suspect(d['rows'][0]), id='wrong')]}),
               lambda d:json.dumps({'suspects':[suspect(d['rows'][0]), suspect(d['rows'][0])]}),
               lambda d:'{"suspects":[],"suspects":[]}',
               lambda d:json.dumps({'suspects':[dict(suspect(d['rows'][0]), confidence='certain')]}),
               lambda d:json.dumps({'suspects':[suspect(d['rows'][0], '한국어 번역')]}),
               lambda d:'{"suspects":[']
        before = self.translation.read_bytes()
        for callback in bad:
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                self.session.scan(config(), indices=[0], client=Model(lambda d:ChatResult(callback(d),'stop')))
        with self.assertRaises(ValueError):
            self.session.scan(config(), indices=[0], client=Model(lambda d:ChatResult('{"suspects":[]}', 'length')))
        self.assertEqual(self.translation.read_bytes(), before)
        self.assertEqual(self.path.read_text('utf-8'), SOURCE)

    def test_partial_scan_survives_later_failure_and_cancellation(self):
        self.path.write_text('### p.png\n'+''.join(f'{i+1}. 猫{i}\n' for i in range(41)), encoding='utf-8')
        session = ReviewSession(self.path,self.root/'data'); calls=[]
        def fail(data):
            calls.append(data)
            if len(calls)>1: raise ApiError('interrupted')
            return ChatResult(json.dumps({'suspects':[suspect(data['rows'][0],None)]}), 'stop')
        with self.assertRaises(ApiError):session.scan(config(),client=Model(fail))
        restored=ReviewSession(self.path,self.root/'data')
        self.assertTrue(restored.issues(0)); self.assertIsNone(restored.rows['0']['candidate'])
        def cancel():raise Cancelled('cancelled')
        with self.assertRaises(Cancelled):restored.scan(config(),client=Model(),check=cancel)
        self.assertTrue(ReviewSession(self.path,self.root/'data').issues(0))

    def test_stale_preview_source_and_translation_are_rejected(self):
        model=Model(); result=self.session.retranslate(0,'一緒に来た。',config(),client=model)
        with self.assertRaises(ValueError):self.session.apply(0,'別の候補',result['text'])
        self.translation.write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError):self.session.apply(0,result['candidate'],result['text'])
        self.path.write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError):self.session.scan(config(),client=model)

    def test_wrong_output_structure_and_unreviewed_translation_rejected(self):
        path=self.root/'other.txt';path.write_text('### wrong.png\n1. 다른 것',encoding='utf-8')
        with self.assertRaises(ValueError):self.session.load_translation(path)
        def bad(d):return ChatResult(json.dumps(dict(id=d['translate'][0]['id'],text='猫')), 'stop')
        result=self.session.retranslate(0,'一緒に来た。',config(),client=Model(bad))
        self.assertTrue(result['issues'])
        with self.assertRaises(ValueError):self.session.apply(0,result['candidate'],result['text'])

    def test_optional_full_model_scan_accepts_no_suspects_without_claiming_verified(self):
        model=Model(lambda d:ChatResult('{"suspects":[]}', 'stop'))
        self.assertEqual(self.session.scan(config(provider='openai'),client=model),3)
        self.assertTrue(self.session.issues(0))
        self.assertIsNone(model.calls[0][1]['reasoning_effort'])
        self.assertIsNone(model.calls[0][1]['thinking_enabled'])
        with self.assertRaises(ValueError):self.session.scan(config(input_tokens=256),client=model)

    def test_name_correction_option_is_review_only_and_never_auto_applied(self):
        model=Model(lambda d:ChatResult('{"suspects":[]}', 'stop'))
        for enabled in (False,True):
            self.session.scan(config(ocr_name_suggestions=enabled,notes='名前=이름'),indices=[1],client=model)
            system=model.calls[-1][2][0]['content']
            self.assertIn('User permits name/term correction proposals' if enabled else 'normalization to an existing glossary entry is disabled',system)
            self.assertEqual(self.session.current_translation(1),'고양이가 왔다.')

    def test_real_http_controls_and_selected_row_only(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(data)
                body=json.loads(data['messages'][-1]['content'])
                content=json.dumps({'suspects':[suspect(body['rows'][0])]}) if 'rows' in body else json.dumps({'id':body['translate'][0]['id'],'text':'같이 왔어.'})
                raw=json.dumps({'choices':[{'index':0,'finish_reason':'stop','message':{'content':content}}]}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            cfg=config(url=f'http://127.0.0.1:{server.server_port}/v1',model='test',stream=False)
            self.session.scan(cfg,indices=[0])
            self.session.retranslate(0,'一緒に来た。',cfg)
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(calls[0]['reasoning_effort'],'low');self.assertNotIn('chat_template_kwargs',calls[0])
        self.assertEqual(calls[1]['reasoning_effort'],'none');self.assertEqual(calls[1]['chat_template_kwargs'],{'enable_thinking':False})
        self.assertEqual(len(json.loads(calls[1]['messages'][-1]['content'])['translate']),1)

    def test_prompt_presets_and_exact_builtin_migration_preserve_custom(self):
        self.assertEqual(template_for({'input_format':'balloon'}),MANGA_TEMPLATE)
        self.assertEqual(template_for({'prompt_template':LEGACY_TEMPLATE}),DEFAULT_TEMPLATE)
        custom=LEGACY_TEMPLATE+'\n내 사용자 지침'
        self.assertEqual(template_for({'input_format':'balloon','prompt_template':custom}),custom)


try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from sub2loc.ocr_desktop import OcrReviewDialog
    HAS_QT=True
except ImportError: HAS_QT=False


@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt for native UI tests')
class OcrDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_keep_apply_and_changed_candidate_invalidates_preview(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'book_source.txt';source.write_text(SOURCE,encoding='utf-8')
            output=root/'book_source.translated.txt';doc=BalloonDocument(source.read_bytes())
            output.write_bytes(doc.render({'0':'함께 함께 함께 함께','1':'고양이','2':'개'}))
            dialog=OcrReviewDialog(source,root/'data',config(),str(output));self.addCleanup(dialog.close)
            self.assertEqual(dialog.tree.topLevelItemCount(),1)
            self.assertEqual(dialog.index,0)
            self.assertEqual(dialog.thinking.currentData(),'low')
            dialog.keep();self.assertEqual(dialog.session.rows['0']['decision'],'keep')
            dialog.candidate.setPlainText('一緒に来た。')
            result=dialog.session.retranslate(0,'一緒に来た。',config(),client=Model())
            dialog.preview.setPlainText(result['text']);dialog.update_buttons();self.assertTrue(dialog.apply_button.isEnabled())
            dialog.candidate.setPlainText('一緒に行く。');self.assertFalse(dialog.apply_button.isEnabled());self.assertEqual(dialog.preview.toPlainText(),'')
            dialog.candidate.setPlainText('一緒に来た。')
            result=dialog.session.retranslate(0,'一緒に来た。',config(),client=Model())
            dialog.preview.setPlainText(result['text']);dialog.apply()
            self.assertTrue(dialog.new_output.is_file());self.assertNotEqual(dialog.new_output,output)
            dialog.only_suspects.setChecked(False);self.assertEqual(dialog.tree.topLevelItemCount(),3)

    def test_background_candidate_and_translation_buttons_and_main_entry_output(self):
        from sub2loc.desktop import MainWindow
        from sub2loc.workspace import Work, Workspace
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'book_source.txt';source.write_text(SOURCE,encoding='utf-8')
            output=root/'book_source.translated.txt';doc=BalloonDocument(source.read_bytes())
            output.write_bytes(doc.render({'0':'함께 함께 함께 함께','1':'고양이','2':'개'}))
            dialog=OcrReviewDialog(source,root/'data',config(),str(output));self.addCleanup(dialog.close)
            model=Model()
            def wait():
                deadline=time.monotonic()+5
                while dialog.task is not None:
                    if time.monotonic()>deadline:self.fail('OCR task timed out')
                    QTest.qWait(10)
            with patch.object(dialog.session,'_client',return_value=model):
                dialog.propose();self.assertFalse(dialog.scan_button.isEnabled());wait()
                self.assertEqual(dialog.candidate.toPlainText(),'一緒に来た。')
                self.assertTrue(dialog.translate_button.isEnabled())
                dialog.retranslate();wait();self.assertEqual(dialog.preview.toPlainText(),'같이 왔어.')
                self.assertTrue(dialog.apply_button.isEnabled());dialog.apply()
            window=MainWindow(root/'desktop');self.addCleanup(window.close)
            window.workspace.settings=config();window.load_settings();window.workspace.add_paths([source]);window.workspace.assign([0],Work.manual('책'))
            window.workspace.entries[0].output=str(output);window.refresh();window.rows[0].setSelected(True)
            captured=[]
            class Finished:
                def __init__(self,*args):captured.append(args);self.new_output=dialog.new_output
                def exec(self):return 0
            with patch('sub2loc.ocr_desktop.OcrReviewDialog',Finished):window.review_ocr()
            self.assertEqual(captured[0][3],str(output))
            self.assertEqual(window.workspace.entries[0].output,str(dialog.new_output))
            self.assertEqual(Workspace.load(window.workspace_path).entries[0].output,str(dialog.new_output))


if __name__=='__main__':unittest.main()
