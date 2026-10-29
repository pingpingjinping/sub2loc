import json
from pathlib import Path
import tempfile
import unittest

from sub2loc.ass import AssDocument, TOKEN
from sub2loc.api import ChatResult
from sub2loc.engine import Engine
from sub2loc.framing import frame_for
from sub2loc.identified import ResponseFormatError
from test_identified import ass,config,Model,answer,reply


class FramingTests(unittest.TestCase):
    def test_position_color_and_closing_style_are_retained_without_model_copy(self):
        raw=ass([r'{\pos(240,496)\c&H0000FFFF\i1}全部乾かしてやるぞーっ！{\i0}'])
        model=Model()
        with tempfile.TemporaryDirectory() as root:
            out,_=Engine(Path(root)).translate(config(),[('01.ass',raw)],client=model)
        self.assertEqual(model.calls[0][0]['translate'][0]['text'],'全部乾かしてやるぞーっ！')
        a,b=AssDocument(raw).cues[0],AssDocument(out[0][1]).cues[0]
        self.assertEqual(a.prefix,b.prefix);self.assertEqual(a.protected,b.protected)
        self.assertEqual(TOKEN.sub('',b.text),'번역문')

    def test_internal_word_boundary_stays_in_whole_cue_and_restores_original_ids(self):
        cue=AssDocument(ass([r'{\pos(10,20)}ゾンビ{\fscx50}たち{\fscx100}が来た{\i0}'])).cues[0]
        frame=frame_for(cue)
        self.assertEqual(frame.source,'ゾンビ⟪S2L:0⟫たち⟪S2L:1⟫が来た')
        restored=frame.restore('좀비⟪S2L:0⟫들⟪S2L:1⟫이 왔다')
        self.assertEqual(TOKEN.findall(restored),['⟪S2L:0⟫','⟪S2L:1⟫','⟪S2L:2⟫','⟪S2L:3⟫'])
        cue.restore(restored)
        with self.assertRaises(ValueError):frame.restore('좀비들이 왔다')
        with self.assertRaises(ValueError):frame.restore('좀비⟪S2L:1⟫들⟪S2L:0⟫이 왔다')

    def test_visual_breaks_remain_required_and_cannot_be_silently_deleted(self):
        cue=AssDocument(ass([r'{\i1}前半\N後半\h続き{\i0}'])).cues[0]
        frame=frame_for(cue)
        with self.assertRaises(ValueError):frame.restore('모두 한 줄')
        self.assertEqual(cue.protected,AssDocument(ass([cue.restore(frame.restore(frame.source))])).cues[0].protected)

    def test_frame_identity_preserves_drawing_whitespace_and_literal_placeholders(self):
        for text in (r'{\p1}m 0 0 l 10 20{\p0}本文',r'本文{\p1}m 0 0 l 10 20{\p0}',
                     r'  {\i1}本文{\i0}  ',r'⟪S2L:7⟫本文',r'\N本文\N'):
            cue=AssDocument(ass([text])).cues[0]
            frame=frame_for(cue)
            self.assertEqual(frame.restore(frame.source),cue.text)
            self.assertEqual(cue.restore(frame.restore(frame.source)),text)

    def test_model_cannot_inject_raw_markup_even_when_all_original_tags_are_local(self):
        raw=ass([r'{\i1}本文{\i0}'])
        model=Model(lambda m,d,k:ChatResult(reply([
            dict(id=r['id'],text=r'{\pos(9,9)}새 태그') for r in d['translate']]),'stop'))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ResponseFormatError):
                Engine(Path(root)).translate(config(quality_retries=0),[('01.ass',raw)],client=model)
            state=json.loads(next(Path(root).glob('*/runs/*.json')).read_text())
            self.assertEqual(state['files']['01.ass'],{})

    def test_terminal_failure_saves_valid_later_rows_and_reports_exact_failed_tokens(self):
        from sub2loc.api import ChatResult
        def broken(m,d,k):
            rows=[answer(r) for r in d['translate']];rows[0]['text']='토큰 삭제'
            return ChatResult(reply(rows),'stop')
        raw=ass([r'{\pos(10,20)}ゾンビ{\fscx50}たち{\fscx100}','猫','犬'])
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));updates=[];cfg=config(quality_retries=0)
            with self.assertRaises(ResponseFormatError) as caught:
                engine.translate(cfg,[('01.ass',raw)],client=Model(broken),progress=lambda **d:updates.append(d))
            state=json.loads(next(Path(root).glob('*/runs/*.json')).read_text())
            self.assertEqual(set(state['files']['01.ass']),{'1','2'})
            self.assertEqual(len(state['pairs']),2)
            details=caught.exception.diagnostic['failed_rows'][0]
            self.assertEqual(details['cue'],1);self.assertEqual(details['expected_tokens'],['⟪S2L:0⟫'])
            self.assertEqual(details['actual_tokens'],[]);self.assertIn('토큰 삭제',str(caught.exception))
            self.assertTrue(any(d.get('done')==2 for d in updates))
            recovered=Model();out,_=engine.translate(cfg,[('01.ass',raw)],client=recovered)
            self.assertEqual(len(recovered.calls[0][0]['translate']),1)
            self.assertTrue(recovered.calls[0][0]['translate'][0]['id'].startswith('e000001-'))
            self.assertEqual(len(AssDocument(out[0][1]).cues),3)

    def test_failed_source_and_reply_diagnostics_redact_configured_api_key(self):
        from sub2loc.api import ChatResult
        secret='PRIVATE-KEY-DO-NOT-LOG'
        raw=ass(['本文'+r'{\i1}'+secret+r'{\i0}'])
        model=Model(lambda m,d,k:ChatResult(reply([dict(id=r['id'],text=secret) for r in d['translate']]),'stop'))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ResponseFormatError) as caught:
                Engine(Path(root)).translate(config(quality_retries=0,key=secret),[('01.ass',raw)],client=model)
            self.assertNotIn(secret,str(caught.exception));self.assertNotIn(secret,caught.exception.path.read_text('utf-8'))
