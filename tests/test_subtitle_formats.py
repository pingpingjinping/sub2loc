import base64
import html
import json
import re
from pathlib import Path
import tempfile
import unittest

from sub2loc.api import ChatResult
from sub2loc.ass import AssDocument
from sub2loc.engine import Engine
from sub2loc.identified import ResponseFormatError
from sub2loc.server import validate
from sub2loc.subtitles import document_for, milliseconds, output_format
from sub2loc.workspace import Work, Workspace, translate_queue, validate_settings, write_result
from test_identified import ass, config, Model, answer, reply


def smi_events(raw):
    # Read the rendered timeline independently of the writer's active-set logic.
    return [(int(at), html.unescape(text).replace('\xa0', '')) for at, text in
            re.findall(r'<SYNC Start=(\d+)><P Class=SUBCC>(.*?)</P></SYNC>', raw.decode('utf-8-sig'))]


class SubtitleFormatTests(unittest.TestCase):
    def test_plain_source_drops_styles_drawings_and_boundaries_but_keeps_dialogue(self):
        source = r'{\pos(10,20)\c&HFF0000&}(ユウ)前{\i1}半\N後半\h続き\n最後{\p1}m 0 0 l 10 20{\p0}！'
        raw = ass([source])
        doc = document_for(raw, config(output_format='srt'))
        self.assertEqual(doc.cues[0].text, '(ユウ)前半 後半 続き 最後！')
        self.assertEqual(doc.cues[0].protected, [])
        self.assertEqual(doc.cues[0].speaker, 'Actor')
        self.assertEqual(AssDocument(raw).cues[0].restore(AssDocument(raw).cues[0].text), source)

    def test_srt_preserves_exact_times_event_order_and_utf8_output(self):
        raw = ass(['先', '後']).decode('utf-8-sig')
        raw = raw.replace('원문 주석', 'comment').replace('0:00:01.00,0:00:04.00', '1:02:03.45,1:02:04.56', 1)
        raw = raw.replace('0:00:01.00,0:00:04.00', '0:00:00.12,0:00:00.34', 1)
        doc = document_for(raw.encode('cp932'), config(output_format='srt', encoding='cp932'))
        result = doc.render({'0': '첫 대사', '1': '두 번째'})
        self.assertTrue(result.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(result.decode('utf-8-sig'),
                         '1\r\n01:02:03,450 --> 01:02:04,560\r\n첫 대사\r\n\r\n'
                         '2\r\n00:00:00,120 --> 00:00:00,340\r\n두 번째\r\n\r\n')

    def test_time_fields_use_ass_format_names_not_fixed_column_numbers(self):
        raw = ('[Events]\nFormat: Name, End, Style, Start, Text\n'
               'Dialogue: A,0:00:03.21,Default,0:00:01.23,猫\n').encode()
        doc = document_for(raw, config(output_format='srt'))
        self.assertIn('00:00:01,230 --> 00:00:03,210', doc.render({'0': '고양이'}).decode('utf-8-sig'))

    def test_smi_preserves_overlapping_and_simultaneous_dialogue_and_clear_events(self):
        raw = ass(['一', '二', '三']).decode('utf-8-sig')
        raw = raw.replace('0:00:01.00,0:00:04.00', '0:00:01.00,0:00:03.00', 1)
        raw = raw.replace('0:00:01.00,0:00:04.00', '0:00:02.00,0:00:04.00', 1)
        raw = raw.replace('0:00:01.00,0:00:04.00', '0:00:02.00,0:00:03.00', 1)
        doc = document_for(raw.encode(), config(output_format='smi'))
        self.assertEqual(smi_events(doc.render({'0': '하나', '1': '둘', '2': '셋'})),
                         [(0, ''), (1000, '하나'), (2000, '하나<BR>둘<BR>셋'), (3000, '둘'), (4000, '')])

    def test_markup_like_dialogue_is_escaped_and_never_becomes_subtitle_tags(self):
        for fmt in ('srt', 'smi'):
            with self.subTest(format=fmt):
                doc = document_for(ass(['猫']), config(output_format=fmt))
                result = doc.render({'0': '<b>글자</b> & {괄호}'}).decode('utf-8-sig')
                self.assertIn('&lt;b&gt;글자&lt;/b&gt; &amp; {괄호}', result)
                self.assertNotIn('<b>글자</b>', result)

    def test_invalid_missing_or_reversed_times_are_rejected_only_in_plain_mode(self):
        for value in ('0:00:xx.00,0:00:04.00', '0:00:01.00,0:00:01.00', '0:00:04.00,0:00:01.00'):
            raw = ass(['猫']).replace(b'0:00:01.00,0:00:04.00', value.encode())
            AssDocument(raw)  # The existing ASS-preserving parser stays lossless.
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '대사 1'):
                document_for(raw, config(output_format='srt'))
        self.assertEqual(milliseconds('10:59:59.99'), 39599990)
        with self.assertRaises(ValueError): milliseconds('0:60:00.00')

    def test_plain_validation_rejects_phantom_tokens_controls_and_empty_text(self):
        doc = document_for(ass(['猫']), config(output_format='srt'))
        for value in ('', '  ', '번역⟪S2L:0⟫', r'번역\N추가', r'{\i1}번역', '줄\n추가', '줄\x00추가'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                doc.render({'0': value})
        with self.assertRaises(ValueError):doc.render({})

    def test_identified_plain_payload_has_no_ass_controls_and_reordered_ids_still_match(self):
        def reversed_rows(model, data, kwargs):
            self.assertNotIn('⟪S2L:', json.dumps(data, ensure_ascii=False))
            self.assertNotIn(r'\pos', json.dumps(data))
            self.assertEqual([r['text'] for r in data['translate']], ['前 後', '別台詞'])
            rows=[dict(id=r['id'],text=t) for r,t in zip(data['translate'], ['앞 뒤', '다른 대사'])]
            return ChatResult(reply(rows[::-1]), 'stop')
        with tempfile.TemporaryDirectory() as root:
            model = Model(reversed_rows)
            outputs, _ = Engine(Path(root)).translate(config(output_format='srt'),
                [('01.ass', ass([r'{\pos(10,20)}前\N後', r'別{\i1}台詞{\i0}']))], client=model)
            self.assertEqual(outputs[0][0], '01.translated.srt')
            blocks=outputs[0][1].decode('utf-8-sig').split('\r\n\r\n')
            self.assertTrue(blocks[0].endswith('앞 뒤'));self.assertTrue(blocks[1].endswith('다른 대사'))

    def test_only_a_bad_plain_id_is_retried_with_neighbors(self):
        def result(model, data, kwargs):
            rows=[answer(r) for r in data['translate']]
            if len(model.calls)==1: rows[2]['text']='번역⟪S2L:0⟫'
            return ChatResult(reply(rows),'stop')
        with tempfile.TemporaryDirectory() as root:
            model=Model(result)
            Engine(Path(root)).translate(config(output_format='smi'), [('01.ass',ass(['猫']*5))],client=model)
            self.assertEqual([len(c[0]['translate']) for c in model.calls],[5,1])
            self.assertEqual(model.calls[1][0]['translate'][0]['id'],model.calls[0][0]['translate'][2]['id'])
            self.assertTrue(model.calls[1][0]['context'])

    def test_srt_and_smi_share_plain_checkpoints_but_ass_state_is_separate(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=Model();files=[('01.ass',ass([r'{\i1}前\N後{\i0}']))]
            engine.translate(config(),files,client=model)
            self.assertEqual(len(model.calls),1)
            engine.translate(config(output_format='ass'),files,client=model)
            self.assertEqual(len(model.calls),1)  # Explicit ASS preserves old identity.
            engine.translate(config(output_format='srt'),files,client=model)
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[1][0]['recent_translations'],[])
            out,_=engine.translate(config(output_format='smi'),files,client=model)
            self.assertEqual(len(model.calls),2)
            self.assertEqual(out[0][0],'01.translated.smi');self.assertTrue(smi_events(out[0][1]))

    def test_plain_resume_keeps_good_rows_after_fatal_id_and_requests_only_missing(self):
        def broken(model,data,kwargs):
            rows=[answer(r) for r in data['translate']];rows[1]['text']=''
            return ChatResult(reply(rows),'stop')
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));cfg=config(output_format='srt',quality_retries=0)
            files=[('01.ass',ass(['猫']*5))]
            with self.assertRaises(ResponseFormatError):engine.translate(cfg,files,client=Model(broken))
            resumed=Model();engine.translate(cfg,files,client=resumed)
            self.assertEqual([len(c[0]['translate']) for c in resumed.calls],[1])

    def test_legacy_chat_and_jako_adapters_can_translate_plain_documents(self):
        class Legacy:
            def complete(self,messages):
                data=json.loads(messages[1]['content'])
                self.data=data
                return json.dumps(dict(translations=[dict(id=r['id'],text='고양이들') for r in data['translate']],context='메모'))
            def complete_text(self,prompt,limit):
                self.data=prompt
                return '고양이들'
        for mode in ('chat','ja-ko-vn'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                model=Legacy()
                out,_=Engine(Path(root)).translate(config(api_mode=mode,provider='openai',model='test',output_format='srt'),
                    [('01.ass',ass([r'{\i1}猫\Nたち{\i0}']))],client=model)
                self.assertEqual(out[0][0],'01.translated.srt')
                self.assertNotIn('⟪S2L:',str(model.data))
                self.assertIn('고양이들',out[0][1].decode('utf-8-sig'))

    def test_queue_uses_selected_extension_keeps_sources_and_avoids_overwriting(self):
        for fmt in ('srt','smi'):
            with self.subTest(format=fmt),tempfile.TemporaryDirectory() as root:
                root=Path(root);path=root/'01.ass';raw=ass([r'{\i1}猫\Nたち{\i0}']);path.write_bytes(raw)
                ws=Workspace();ws.add_paths([path]);ws.assign([0],Work.manual('작품'))
                results=translate_queue(ws,config(output_format=fmt),root/'state',root/'out',client=Model())
                saved=Path(results[0]);self.assertTrue(saved.name.endswith('.translated.'+fmt))
                self.assertEqual(path.read_bytes(),raw)
                alternate=write_result(saved,b'different')
                self.assertTrue(alternate.name.endswith('.translated.'+fmt));self.assertNotEqual(saved,alternate)
                self.assertNotEqual(saved.read_bytes(),b'different')

    def test_invalid_later_plain_timestamp_preflights_whole_queue_without_api_calls(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            (root/'01.ass').write_bytes(ass(['猫']))
            (root/'02.ass').write_bytes(ass(['猫']).replace(b'0:00:04.00',b'0:00:00.00'))
            ws=Workspace();ws.add_paths([root]);ws.assign([0,1],Work.manual('작품'));model=Model()
            with self.assertRaises(ValueError):
                translate_queue(ws,config(output_format='srt'),root/'state',root/'out',client=model)
            self.assertEqual(model.calls,[])

    def test_format_validation_desktop_and_web_and_default_backwards_compatibility(self):
        self.assertEqual(output_format({}),'ass')
        self.assertNotIn('output_format',validate_settings(config()))
        for fmt in ('ass','srt','smi'):
            self.assertEqual(validate_settings(config(output_format=fmt))['output_format'],fmt)
            data=dict(config(output_format=fmt),model='model',batch_size=12,files=[dict(name='01.ass',data=base64.b64encode(ass(['猫'])).decode())])
            self.assertEqual(validate(data)[0]['output_format'],fmt)
        for fmt in ('txt','ASS',None,[],{}):
            with self.subTest(value=fmt),self.assertRaises(ValueError):validate_settings(config(output_format=fmt))

