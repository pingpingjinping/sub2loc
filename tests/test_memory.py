import json
from pathlib import Path
import tempfile
import threading
import unittest
from sub2loc.api import ApiError,ChatResult
from sub2loc.ass import AssDocument
from sub2loc.engine import Engine,Cancelled
from sub2loc.memory import summary_for,summary_request
from test_identified import ass,config,Model,answer,reply


class MemoryTests(unittest.TestCase):
    def model(self,fail=False,cancel=None):
        def result(m,data,kwargs):
            if 'preceding_rows' in data:
                if cancel:cancel.set();kwargs['check_cancel']()
                if fail:raise ApiError('summary failed')
                return ChatResult('앞 장면의 짧은 메모.','stop')
            return ChatResult(reply([answer(r) for r in data['translate']]),'stop')
        return Model(result)

    def test_incremental_budget_and_only_contiguous_accepted_prefix(self):
        cues=AssDocument(ass(['猫']*210)).cues;saved={str(c.id):'고양이' for c in cues}
        msg,end=summary_request(cues,saved,205,{},20000)
        data=json.loads(msg[-1]['content']);self.assertEqual(end,100)
        self.assertEqual([r['number'] for r in data['preceding_rows']],list(range(1,101)))
        msg,end=summary_request(cues,saved,205,dict(covered=100,text='이전 메모'),20000)
        data=json.loads(msg[-1]['content']);self.assertEqual(end,200)
        self.assertEqual(data['preceding_rows'][0]['number'],101)
        self.assertEqual(data['previous_summary'],'이전 메모')
        del saved['102']
        _,end=summary_request(cues,saved,205,dict(covered=100,text='이전 메모'),20000)
        self.assertEqual(end,102)
        self.assertIsNone(summary_request(cues,saved,100,{},100))
        self.assertEqual(summary_for(dict(covered=100,text='미래'),50),'')

    def test_pipeline_updates_summary_before_each_new_batch_and_preserves_cache(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=self.model();cfg=config(use_summary=True,batch_size=2)
            files=[('01.ass',ass(['猫']*6))];updates=[]
            engine.translate(cfg,files,client=model,progress=lambda **d:updates.append(d))
            translations=[c[0] for c in model.calls if 'translate' in c[0]]
            summaries=[c[0] for c in model.calls if 'preceding_rows' in c[0]]
            self.assertNotIn('preceding_summary',translations[0])
            self.assertEqual([d['preceding_summary']['through_cue'] for d in translations[1:]],[2,4])
            self.assertEqual([[r['number'] for r in d['preceding_rows']] for d in summaries],[[1,2],[3,4]])
            self.assertEqual(updates[-1]['requests'],5)
            self.assertEqual(sum(d.get('phase')=='summary_request' for d in updates),2)
            engine.translate(config(use_summary=False,batch_size=2),files,client=model)
            self.assertEqual(len(model.calls),5)

    def test_summary_failure_does_not_stop_or_retry_translation(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=self.model(fail=True);updates=[]
            outputs,_=engine.translate(config(use_summary=True,batch_size=2),[('01.ass',ass(['猫']*8))],client=model,progress=lambda **d:updates.append(d))
            self.assertEqual(len(AssDocument(outputs[0][1]).cues),8)
            self.assertEqual(sum('preceding_rows' in c[0] for c in model.calls),2)
            self.assertEqual(sum('translate' in c[0] for c in model.calls),4)
            self.assertTrue(any('요약 갱신 실패' in d.get('message','') for d in updates))

    def test_files_do_not_receive_other_episode_summaries(self):
        with tempfile.TemporaryDirectory() as root:
            model=self.model();Engine(Path(root)).translate(config(use_summary=True,batch_size=2),
                [('01.ass',ass(['猫']*4)),('02.ass',ass(['犬']*4))],client=model)
            second=[c[0] for c in model.calls if c[0].get('episode')=='02.ass' and 'translate' in c[0]]
            self.assertNotIn('preceding_summary',second[0]);self.assertEqual(second[1]['preceding_summary']['through_cue'],2)

    def test_cancel_during_summary_preserves_accepted_ids_and_resume(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));cancel=threading.Event();cfg=config(use_summary=True,batch_size=2)
            files=[('01.ass',ass(['猫']*4))]
            with self.assertRaises(Cancelled):engine.translate(cfg,files,cancel,client=self.model(cancel=cancel))
            state=json.loads(next(Path(root).glob('*/runs/*.json')).read_text())
            self.assertEqual(len(state['files']['01.ass']),2)
            model=self.model();engine.translate(cfg,files,client=model)
            self.assertEqual(sum(len(c[0]['translate']) for c in model.calls if 'translate' in c[0]),2)
