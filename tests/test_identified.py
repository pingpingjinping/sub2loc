import copy
import json
import os
import re
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sub2loc.api import Client, ChatResult, RequestError, StreamInterrupted
from sub2loc.ass import AssDocument, TOKEN
from sub2loc.engine import Engine, Cancelled
from sub2loc.identified import parse_rows, project_lock, ResponseFormatError
from sub2loc.profiles import POLYSTRATA
from sub2loc.review import inspect_translation, normalize_interjection
from sub2loc.workspace import validate_settings, Workspace, Work, translate_queue


def config(**kwargs):
    return dict(POLYSTRATA, work='테스트 작품', notes='', encoding='utf-8-sig', source='auto',
                target='한국어', timeout=5, **kwargs) if not kwargs else dict(config(),**kwargs)


def ass(texts):
    return ('\ufeff[Script Info]\r\nTitle: Metadata test\r\n[Events]\r\n'
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n'
            'Comment: 2,0:00:00.00,0:00:01.00,Default,Actor,1,2,3,effect,원문 주석\r\n'+''.join(
                f'Dialogue: 3,0:00:01.00,0:00:04.00,OP/ED,Actor,1,2,3,fx,{text}\r\n' for text in texts)).encode('utf-8')


def reply(rows): return '\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)
def payload(messages): return json.loads(messages[-1]['content'])
def target_text(row,label='번역문'):
    return ''.join(piece if TOKEN.fullmatch(piece) else label if piece.strip() else piece for piece in re.split('('+TOKEN.pattern+')',row['text']))
def answer(row): return dict(id=row['id'],text=target_text(row))


class Model:
    def __init__(self,callback=None): self.calls=[]; self.callback=callback
    def chat(self,messages,**kwargs):
        data=payload(messages); self.calls.append((data,kwargs,messages))
        if self.callback: return self.callback(self,data,kwargs)
        return ChatResult(reply([answer(r) for r in data['translate']]),'stop')
    def discover(self): return {'id':'gemma-4-26b-a4b','meta':{'n_ctx':32768}}


class IdentifiedTests(unittest.TestCase):
    def run_model(self,raw,model,cfg=None,root=None,updates=None):
        return Engine(Path(root or self.root)).translate(cfg or config(),[('01.ass',raw)],client=model,
               progress=lambda **d: (updates if updates is not None else []).append(d))
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=self.tmp.name

    def test_pretty_json_envelope_is_bound_by_ids_and_duplicates_still_rejected(self):
        raw=json.dumps({'translations':[{'id':'b','text':'둘'},{'id':'a','text':'하나'}]},indent=2)
        values,duplicates,unknown=parse_rows(raw,{'a','b'})
        self.assertEqual(values,{'a':'하나','b':'둘'});self.assertFalse(duplicates);self.assertFalse(unknown)
        values,duplicates,_=parse_rows(reply([{'id':'a','text':'하나'},{'id':'a','text':'다른 것'}]),{'a'})
        self.assertFalse(values);self.assertEqual(duplicates,{'a'})

    def test_repair_issues_do_not_repeat_other_pending_ids(self):
        def limited(m,d,k):
            if len(m.calls)==1:return ChatResult(reply([dict(id=r['id'],text='태그 삭제') for r in d['translate']]),'length')
            return ChatResult(reply([answer(r) for r in d['translate']]),'stop')
        model=Model(limited);self.run_model(ass([r'{\i1}猫{\i0}',r'{\i1}犬{\i0}']),model)
        for data,_,_ in model.calls[1:]:
            self.assertEqual(set(data['repair_issues']),{r['id'] for r in data['translate']})

    def test_pretty_jsonl_and_per_record_fences_support_100_exact_ids(self):
        for fenced in (False,True):
            with self.subTest(fenced=fenced),tempfile.TemporaryDirectory() as root:
                def pretty(m,d,k):
                    rows=[json.dumps(answer(r),ensure_ascii=False,indent=2) for r in d['translate']]
                    if fenced: rows=['```json\n'+r+'\n```' for r in rows]
                    return ChatResult('\n'.join(rows),'stop')
                model=Model(pretty);out,_=self.run_model(ass(['猫']*100),model,root=root)
                self.assertEqual(len(model.calls),1);self.assertEqual(len(AssDocument(out[0][1]).cues),100)

    def test_pretty_jsonl_partial_tail_never_discards_complete_records(self):
        def partial(m,d,k):
            if len(m.calls)==1:
                return ChatResult('\n'.join(json.dumps(answer(r),indent=2) for r in d['translate'][:2])+'\n{\n  "id":','length')
            return ChatResult(reply([answer(r) for r in d['translate']]),'stop')
        model=Model(partial);self.run_model(ass(['猫','犬','鳥']),model)
        self.assertEqual([len(c[0]['translate']) for c in model.calls],[3,1])

    def test_whole_response_format_failure_stops_once_and_summarizes_instead_of_100_retries(self):
        def numbered(m,d,k): return ChatResult('\n'.join(f'{i+1:03} | 번역' for i in range(len(d['translate']))),'stop',{'completion_tokens':1200})
        model=Model(numbered);updates=[]
        with self.assertRaises(ResponseFormatError) as caught:self.run_model(ass(['猫']*100),model,updates=updates)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(caught.exception.diagnostic['usable_rows'],0)
        self.assertEqual(caught.exception.diagnostic['usage']['completion_tokens'],1200)
        self.assertIn('001 | 번역',str(caught.exception))
        self.assertLess(len([d for d in updates if d.get('phase')=='review']),5)
        self.assertEqual(updates[-1]['errors'],100)
        self.assertTrue(caught.exception.path.is_file())

    def test_numeric_ids_are_reported_without_guessing_their_opaque_id(self):
        def numeric(m,d,k): return ChatResult(reply([dict(id=i+1,text='번역') for i,_ in enumerate(d['translate'])]),'stop')
        model=Model(numeric)
        with self.assertRaises(ResponseFormatError) as caught:self.run_model(ass(['猫','犬']),model)
        self.assertEqual(caught.exception.diagnostic['observed_id_types'],{'int':2})
        self.assertEqual(len(model.calls),1)

    def test_correct_id_wrong_text_key_is_reported_as_schema_failure(self):
        def wrong_key(m,d,k): return ChatResult(reply([dict(id=r['id'],translation='번역') for r in d['translate']]),'stop')
        with self.assertRaises(ResponseFormatError) as caught:self.run_model(ass(['猫']),Model(wrong_key))
        self.assertEqual(caught.exception.diagnostic['matched_ids'],1)
        self.assertEqual(caught.exception.diagnostic['usable_rows'],0)
        self.assertIn('translation',caught.exception.diagnostic['response_preview'])

    def test_empty_response_has_finish_and_usage_diagnostic_and_no_retry(self):
        model=Model(lambda m,d,k:ChatResult('','length',{'completion_tokens':8192},reasoning_characters=3000))
        with self.assertRaises(ResponseFormatError) as caught:self.run_model(ass(['猫']),model)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(caught.exception.diagnostic['finish_reason'],'length')
        self.assertIn('본문이 비어',str(caught.exception));self.assertEqual(caught.exception.diagnostic['reasoning_characters'],3000)

    def test_diagnostic_redacts_api_key_in_preview_and_file(self):
        secret='PRIVATE-API-CREDENTIAL'
        model=Model(lambda m,d,k:ChatResult(secret+' '+('본문'*5000)+secret,'stop'))
        with self.assertRaises(ResponseFormatError) as caught:self.run_model(ass(['猫']),model,config(key=secret))
        self.assertNotIn(secret,str(caught.exception));self.assertNotIn(secret,caught.exception.path.read_text('utf-8'))
        self.assertLess(len(caught.exception.diagnostic['response_preview']),2450)
        self.assertNotIn('key',caught.exception.diagnostic)

    def test_whole_format_failure_preserves_previous_validated_ids_for_resume(self):
        def some(m,d,k):
            if len(m.calls)==1:return ChatResult(reply([answer(r) for r in d['translate'][:2]]),'stop')
            return ChatResult('003 | 번역','stop')
        raw=ass(['猫','犬','鳥']);model=Model(some)
        with self.assertRaises(ResponseFormatError):self.run_model(raw,model)
        recovered=Model();self.run_model(raw,recovered)
        self.assertEqual(len(recovered.calls[0][0]['translate']),1)
        self.assertTrue(recovered.calls[0][0]['translate'][0]['id'].startswith('e000003-'))

    def test_reordered_output_maps_by_id_and_preserves_every_ass_field(self):
        raw=ass([r'{\i1}一行\N二行\h次{\i0}',r'{\pos(10,20)}看板',r'{\p1}m 0 0 l 2 3{\p0}'])
        def reverse(m,d,k):
            return ChatResult(reply([dict(id=r['id'],text=target_text(r,'대사'+str(i))) for i,r in reversed(list(enumerate(d['translate'])))]),'stop')
        out,_=self.run_model(raw,Model(reverse))
        a,b=AssDocument(raw),AssDocument(out[0][1])
        self.assertEqual(len(a.lines),len(b.lines)); self.assertEqual(len(a.cues),len(b.cues))
        for i,(before,after) in enumerate(zip(a.cues,b.cues)):
            self.assertEqual(before.prefix,after.prefix); self.assertEqual(before.protected,after.protected)
            self.assertTrue(TOKEN.sub('',after.text).startswith('대사'+str(i)))
        self.assertEqual(a.lines[-1],b.lines[-1]); self.assertEqual(a.lines[4],b.lines[4])

    def test_only_missing_id_is_retried_with_surrounding_context(self):
        def missing(m,d,k):
            rows=d['translate']; selected=rows[:1]+rows[2:] if len(m.calls)==1 else rows
            return ChatResult(reply([answer(r) for r in selected]),'stop')
        model=Model(missing); updates=[]
        self.run_model(ass(['猫','犬','鳥']),model,updates=updates)
        self.assertEqual([len(c[0]['translate']) for c in model.calls],[3,1])
        self.assertEqual(model.calls[1][0]['translate'][0]['id'],model.calls[0][0]['translate'][1]['id'])
        self.assertEqual(len(model.calls[1][0]['context']),2)
        self.assertEqual(updates[-1]['done'],3); self.assertEqual(updates[-1]['retries'],1)
        self.assertEqual(updates[-1]['errors'],1)

    def test_duplicate_id_is_quarantined_without_retranslating_other_rows(self):
        def duplicated(m,d,k):
            rows=[answer(r) for r in d['translate']]
            return ChatResult(reply(rows+[rows[0]]) if len(m.calls)==1 else reply(rows),'stop')
        model=Model(duplicated); self.run_model(ass(['猫','犬']),model)
        self.assertEqual([len(c[0]['translate']) for c in model.calls],[2,1])

    def test_renumbered_unknown_id_never_uses_positional_fallback(self):
        def renamed(m,d,k): return ChatResult(reply([dict(id='001',text='오류')]),'stop')
        model=Model(renamed)
        with self.assertRaisesRegex(ResponseFormatError,'자동 재요청'):
            self.run_model(ass(['猫']),model)
        self.assertEqual(len(model.calls),1)
        saved=json.loads(next(Path(self.root).glob('*/runs/*.json')).read_text())
        self.assertEqual(saved['files']['01.ass'],{})

    def test_wrong_ass_tokens_only_retry_affected_id(self):
        def broken(m,d,k):
            rows=[answer(r) for r in d['translate']]
            if len(m.calls)==1: rows[1]['text']='태그 삭제'
            return ChatResult(reply(rows),'stop')
        model=Model(broken); self.run_model(ass(['猫',r'{\i1}犬{\i0}']),model)
        self.assertEqual([len(c[0]['translate']) for c in model.calls],[2,1])

    def test_foreign_script_is_retranslated_but_interjection_is_safely_normalized(self):
        def foreign(m,d,k):
            rows=[answer(r) for r in d['translate']]
            if len(m.calls)==1: rows[0]['text']='ゾンビ'; rows[1]['text']='嗯~!'
            return ChatResult(reply(rows),'stop')
        model=Model(foreign); out,_=self.run_model(ass(['猫','ん～！','犬']),model)
        self.assertEqual([len(c[0]['translate']) for c in model.calls],[3,1])
        self.assertIn('음~!',out[0][1].decode('utf-8-sig'))

    def test_persistent_quality_suspect_is_kept_with_review_instead_of_deleted(self):
        def foreign(m,d,k): return ChatResult(reply([dict(id=r['id'],text='傀人') for r in d['translate']]),'stop')
        engine=Engine(Path(self.root)); model=Model(foreign)
        out,_=engine.translate(config(),[('01.ass',ass(['猫']))],client=model)
        self.assertIn('傀人',out[0][1].decode('utf-8-sig')); self.assertEqual(len(model.calls),3)
        self.assertFalse(engine.last_review[-1]['resolved'])

    def test_valid_rows_from_length_or_interrupted_output_are_never_repeated(self):
        for interruption in (False,True):
            with self.subTest(interruption=interruption),tempfile.TemporaryDirectory() as root:
                def partial(m,d,k):
                    rows=[answer(r) for r in d['translate']]
                    if len(m.calls)==1:
                        result=ChatResult(reply(rows[:2])+'\n{"id":', 'length' if not interruption else '')
                        if interruption: raise StreamInterrupted(result)
                        return result
                    return ChatResult(reply(rows),'stop')
                model=Model(partial); self.run_model(ass(['猫','犬','鳥']),model,root=root)
                sent=[r['id'] for call in model.calls[1:] for r in call[0]['translate']]
                self.assertTrue(all(r['id'] not in sent for r in model.calls[0][0]['translate'][:2]))

    def test_non_prefix_checkpoint_survives_api_failure_and_resumes_only_missing_id(self):
        def fail(m,d,k):
            if len(m.calls)==1: return ChatResult(reply([answer(d['translate'][2])]),'stop')
            raise RequestError(401)
        raw=ass(['猫','犬','鳥']); model=Model(fail)
        with self.assertRaises(RequestError): self.run_model(raw,model)
        model=Model(); self.run_model(raw,model)
        self.assertEqual(len(model.calls[0][0]['translate']),2)
        self.assertNotIn('e000003',str(model.calls[0][0]['translate']))

    def test_complete_cache_makes_no_requests_and_template_change_invalidates_run(self):
        raw=ass(['猫']); model=Model(); self.run_model(raw,model); self.run_model(raw,model)
        self.assertEqual(len(model.calls),1)
        self.run_model(raw,model,config(prompt_template='다른 지침'))
        self.assertEqual(len(model.calls),2)

    def test_pack_honors_both_token_budgets_and_can_send_100_cues(self):
        model=Model(); raw=ass(['猫']*100); self.run_model(raw,model)
        self.assertEqual(len(model.calls[0][0]['translate']),100)
        for data,kwargs,messages in model.calls:
            self.assertLessEqual(len(json.dumps(messages,ensure_ascii=False).encode())+128,20000)
            self.assertEqual(kwargs['max_tokens'],8192)
        self.assertEqual(model.calls[0][1]['reasoning_effort'],'none')
        with tempfile.TemporaryDirectory() as root:
            smaller=Model(); self.run_model(ass(['猫'*100]*20),smaller,config(input_tokens=5000),root=root)
            self.assertGreater(len(smaller.calls),1)

    def test_generic_identified_provider_never_sends_thinking_extension(self):
        model=Model(); self.run_model(ass(['猫']),model,config(provider='openai',model='other'))
        self.assertIsNone(model.calls[0][1]['reasoning_effort'])

    def test_server_context_error_adjusts_budget_and_auth_failure_is_not_retried(self):
        def overflow(m,d,k):
            if len(m.calls)==1:
                err=RequestError(400); err.context_overflow=True; raise err
            return ChatResult(reply([answer(r) for r in d['translate']]),'stop')
        model=Model(overflow); self.run_model(ass(['猫'*40]*50),model)
        self.assertLess(len(model.calls[1][0]['translate']),len(model.calls[0][0]['translate']))
        with tempfile.TemporaryDirectory() as root:
            def auth(m,d,k): raise RequestError(401)
            model=Model(auth)
            with self.assertRaises(RequestError): self.run_model(ass(['猫']),model,root=root)
            self.assertEqual(len(model.calls),1)

    def test_cancel_never_saves_unvalidated_stream_output(self):
        cancel=threading.Event()
        def cancelled(m,d,k): cancel.set(); return ChatResult(reply([answer(r) for r in d['translate']]),'stop')
        with self.assertRaises(Cancelled): Engine(Path(self.root)).translate(config(),[('01.ass',ass(['猫']))],cancel=cancel,client=Model(cancelled))
        self.assertFalse(list(Path(self.root).glob('*/runs/*.json')))

    def test_same_work_os_lock_and_other_work_independence(self):
        path=Path(self.root)/'work.lock'
        with project_lock(path):
            with self.assertRaises(ValueError):
                with project_lock(path): pass
            with project_lock(Path(self.root)/'other.lock'): pass

    def test_checks_do_not_delete_names_or_force_semantic_replacements(self):
        cue=AssDocument(ass(['猫'])).cues[0]
        self.assertTrue(inspect_translation(cue,'嗯~!'))
        self.assertFalse(inspect_translation(cue,'傀人',allowed=['傀人']))
        self.assertIn('비정상 반복 의심',inspect_translation(cue,'고양이'*10))
        self.assertIn('원문 대비 과도한 길이',inspect_translation(cue,'가'*100))
        self.assertEqual(normalize_interjection('一般文','嗯~!'),'嗯~!')

    def test_visual_line_with_content_cannot_silently_become_empty(self):
        def empty_visual(m,d,k):
            return ChatResult(reply([dict(id=r['id'],text='한 줄'+''.join(TOKEN.findall(r['text']))) for r in d['translate']]),'stop')
        model=Model(empty_visual)
        with self.assertRaisesRegex(ValueError,'ID/ASS'):
            self.run_model(ass([r'前半\N後半']),model)
        self.assertEqual(len(model.calls),3)

    def test_semantic_review_examples_are_sent_verbatim_without_forced_substitution(self):
        cases=json.loads(Path(__file__).with_name('fixtures').joinpath('anime_semantics.json').read_text('utf-8'))
        model=Model();self.run_model(ass([c['source'] for c in cases]),model)
        self.assertEqual([r['text'] for r in model.calls[0][0]['translate']],[c['source'] for c in cases])
        # The deliberately generic fake output is not replaced by golden answers.
        self.assertEqual(len(model.calls),1)

    def test_settings_validation_rejects_invalid_budgets_and_unknown_controls(self):
        self.assertEqual(validate_settings(config())['batch_size'],100)
        for change in (dict(input_tokens=30000),dict(temperature=float('nan')),dict(thinking='fake'),dict(quality_retries=10),dict(prompt_template='')):
            with self.subTest(change=change),self.assertRaises(ValueError): validate_settings(config(**change))

    def test_two_works_keep_glossary_context_progress_and_review_report_separate(self):
        root=Path(self.root); ws=Workspace()
        for i in range(2):
            path=root/f'{i}.ass';path.write_bytes(ass(['猫']))
            ws.add_paths([path]); work=Work.manual(str(i));work.glossary='猫=고양이';work.notes='서로 존댓말'
            ws.assign([i],work)
        model=Model(); updates=[]
        out=translate_queue(ws,config(),root/'state',root/'out',client=model,progress=lambda **d:updates.append(d))
        self.assertEqual(len(out),2); self.assertEqual(model.calls[1][0]['recent_translations'],[])
        self.assertIn('猫=고양이',model.calls[0][0]['fixed_notes'])
        self.assertEqual(updates[-2]['global_total'],2)
        self.assertTrue(any(d.get('global_done')==2 and d.get('requests')==2 for d in updates))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.seen=[]; self.structured=False; self.cut=False; owner=self
        class Server(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'data':[
                    {'id':'image','architecture':{'output_modalities':['image']}},
                    {'id':'old','status':{'value':'unloaded'}},
                    {'id':'gemma','status':{'value':'loaded'},'meta':{'n_ctx':32768}}]}).encode())
            def do_POST(self):
                d=json.loads(self.rfile.read(int(self.headers['Content-Length'])));owner.seen.append(d)
                self.send_response(200);self.send_header('Content-Type','text/event-stream' if d['stream'] else 'application/json');self.end_headers()
                content='정답'
                if owner.structured:
                    rows=payload(d['messages'])['translate']
                    selected=rows[1:] if len(owner.seen)==1 else rows
                    content=reply([answer(r) for r in selected])
                if not d['stream']:
                    self.wfile.write(json.dumps({'choices':[{'message':{'content':'정답'},'finish_reason':'stop'}],'usage':{'completion_tokens':2}}).encode());return
                for obj in ({'choices':[{'delta':{'reasoning_content':'비공개 생각'}}]},
                            {'choices':[{'delta':{'content':content}}]},
                            {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'completion_tokens':2},'timings':{'predicted_per_second':88.5}}):
                    if owner.cut and obj.get('usage'): continue
                    self.wfile.write(('data: '+json.dumps(obj,ensure_ascii=False)+'\r\n\r\n').encode());self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Server)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.client=Client(f'http://127.0.0.1:{self.server.server_port}/v1',model='gemma')
    def test_sse_multibyte_usage_reasoning_separation_and_supported_payload(self):
        events=[];result=self.client.chat([],max_tokens=500,temperature=.7,stream=True,reasoning_effort='none',on_event=lambda **d:events.append(d))
        self.assertEqual(result.content,'정답');self.assertEqual(result.usage['completion_tokens'],2)
        self.assertEqual(result.reasoning_characters,len('비공개 생각'))
        self.assertEqual(result.timings['predicted_per_second'],88.5)
        self.assertEqual(set(self.seen[0]),{'model','messages','temperature','max_tokens','stream','reasoning_effort'})
        self.assertEqual(self.seen[0]['reasoning_effort'],'none')
    def test_nonstream_generic_and_loaded_model_detection(self):
        self.assertEqual(self.client.discover()['id'],'gemma')
        self.assertEqual(self.client.chat([],max_tokens=500,temperature=.7).content,'정답')
        self.assertNotIn('reasoning_effort',self.seen[0])

    def test_real_http_plain_response_stops_with_diagnostic_after_one_request(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ResponseFormatError) as caught:
                Engine(Path(root)).translate(config(url=self.client.url),[('01.ass',ass(['猫','犬']))])
            self.assertEqual(len(self.seen),1);self.assertEqual(caught.exception.diagnostic['response_preview'],'정답')

    def test_real_streaming_pipeline_auto_model_and_targeted_repair(self):
        self.structured=True
        with tempfile.TemporaryDirectory() as root:
            updates=[]; cfg=config(url=self.client.url)
            out,_=Engine(Path(root)).translate(cfg,[('01.ass',ass(['猫']*100))],progress=lambda **d:updates.append(d))
            self.assertEqual([len(payload(r['messages'])['translate']) for r in self.seen],[100,1])
            self.assertTrue(all(r['model']=='gemma' and r['reasoning_effort']=='none' for r in self.seen))
            self.assertEqual(len(AssDocument(out[0][1]).cues),100)
            self.assertTrue(any(d.get('tokens_per_second')==88.5 for d in updates))
            self.assertEqual(updates[-1]['done'],100)

    def test_benchmark_cli_selects_037_to_136_without_claiming_semantic_verification(self):
        self.structured=True
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);original=ass(['猫']*150);path=root/'source.ass';path.write_bytes(original)
            script=Path(__file__).resolve().parents[1]/'scripts'/'benchmark_polystrata.py'
            result=subprocess.run([sys.executable,str(script),str(path),'--url',self.client.url,'--output',str(root/'out')],
                                  capture_output=True,text=True,encoding='utf-8',timeout=30,
                                  env=dict(os.environ,PYTHONUTF8='1'))
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(next((root/'out').glob('*.benchmark.json')).read_text('utf-8'))
            self.assertEqual([r['original_number'] for r in report['lines']],list(range(37,137)))
            self.assertFalse(report['semantic_quality_verified']);self.assertTrue(report['measured_on_command_host'])
            self.assertEqual(path.read_bytes(),original)

    def test_real_stream_without_finish_salvages_complete_ids(self):
        self.structured=True; self.cut=True
        with tempfile.TemporaryDirectory() as root:
            Engine(Path(root)).translate(config(url=self.client.url),[('01.ass',ass(['猫','犬','鳥']))])
            self.assertEqual([len(payload(r['messages'])['translate']) for r in self.seen],[3,1])



os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from sub2loc.desktop import MainWindow
    QT=True
except ImportError: QT=False

@unittest.skipUnless(QT,'Install desktop requirements')
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def test_format_failure_preview_is_copyable_in_existing_error_panel(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            model=Model(lambda m,d,k:ChatResult('001 | 실제 응답','stop'))
            try:Engine(Path(root)/'engine').translate(config(),[('01.ass',ass(['猫']))],client=model)
            except ResponseFormatError as issue:message=str(issue)
            w=MainWindow(Path(root)/'ui');w.worker=SimpleNamespace(cancel=threading.Event());w.job_error(message)
            self.assertIn('001 | 실제 응답',w.probe_result.toPlainText())
            w.copy_result_button.click();self.assertEqual(QApplication.clipboard().text(),message)
            w.worker=None;w.close()

    def test_preset_editable_template_persistence_and_live_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            w=MainWindow(Path(folder));w.apply_polystrata()
            s=w.settings();self.assertEqual(s['url'],POLYSTRATA['url']);self.assertEqual(s['temperature'],.7)
            self.assertEqual(s['thinking'],'none');self.assertEqual(s['batch_size'],100)
            w.prompt_template.setPlainText('나만의 번역 지침');w.close()
            w=MainWindow(Path(folder));self.assertEqual(w.settings()['prompt_template'],'나만의 번역 지침')
            w.job_progress(dict(done=50,total=100,global_done=50,global_total=100,requests=2,retries=1,errors=3,
                                first=37,last=136,stage='재번역',eta_seconds=15,tokens_per_second=86.5))
            self.assertIn('50/100',w.metrics_label.text());self.assertIn('86.5 tok/s',w.metrics_label.text())
            self.assertIn('ETA 추정 15초',w.metrics_label.text());self.assertIn('오류 자막 3',w.metrics_label.text())
            w.close()
