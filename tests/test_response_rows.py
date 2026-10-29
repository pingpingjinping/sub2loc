import json
from pathlib import Path
import tempfile
import unittest

from sub2loc.api import ChatResult
from sub2loc.engine import Engine
from sub2loc.identified import parse_rows
from sub2loc.response_rows import repair_row_tail,response_items
from test_identified import ass,config,Model,answer,reply


def malformed(row,kind='quote'):
    encoded=json.dumps(row,ensure_ascii=False)
    return encoded[:-1]+(',}' if kind=='comma' else ', "}')


class ResponseRowTests(unittest.TestCase):
    def test_reported_12_record_shape_recovers_all_ids_without_changing_strings(self):
        rows=[dict(id=f'case-{i}',text=f'문장 {i}⟪S2L:0⟫。') for i in range(12)]
        lines=[json.dumps(r,ensure_ascii=False) if i in (0,2) else malformed(r,'comma' if i==4 else 'quote') for i,r in enumerate(rows)]
        raw=',\n'.join(lines);stats={}
        found,duplicates,unknown=parse_rows(raw,{r['id'] for r in rows},stats)
        self.assertEqual(found,{r['id']:r['text'] for r in rows})
        self.assertFalse(duplicates);self.assertFalse(unknown)
        self.assertEqual(stats,dict(recovered_rows=11,syntax_repairs=10))

    def test_valid_later_records_survive_an_unrepairable_middle_record(self):
        first=dict(id='a',text='앞');last=dict(id='c',text='뒤')
        raw=reply([first])+'\n{"id":"b","text":"unterminated\n'+reply([last])
        found,_,_=parse_rows(raw,{'a','b','c'})
        self.assertEqual(found,{'a':'앞','c':'뒤'})

    def test_repairs_never_touch_quoted_punctuation_braces_escapes_or_placeholders(self):
        value='따옴표 " ,} ,"} { [ ] \\ 경로 ⟪S2L:0⟫ 😀'
        for kind in ('comma','quote'):
            with self.subTest(kind=kind):
                row=dict(id='a',text=value)
                self.assertEqual(repair_row_tail(malformed(row,kind)),row)

    def test_unknown_keys_duplicate_keys_nonstring_values_and_truncated_strings_are_not_repaired(self):
        cases=['{"id":"a","text":"정상","other":"추가", "}',
               '{"id":"a","id":"b","text":"애매함", "}',
               '{"id":1,"text":"숫자 ID", "}',
               '{"id":"a","text":["배열"], "}',
               '{"id":"a","text":"완성 안 됨',
               '{"id":"a","text":"완성했지만 괄호 없음"',
               '{"id":"a","text":"닫힘","추가 필드 없음"}']
        for raw in cases:
            with self.subTest(raw=raw):self.assertIsNone(repair_row_tail(raw))

    def test_literal_json_inside_a_complete_text_string_is_not_extracted_as_another_id(self):
        value='예시 '+json.dumps(dict(id='b',text='가짜'),ensure_ascii=False)
        found,_,_=parse_rows(json.dumps(dict(id='a',text=value)),{'a','b'})
        self.assertEqual(found,{'a':value})

    def test_complete_envelopes_cover_nested_rows_without_duplicate_counting(self):
        raw=json.dumps({'translations':[dict(id='a',text='하나'),dict(id='b',text='둘')]},indent=2)
        found,duplicates,_=parse_rows(raw,{'a','b'})
        self.assertEqual(found,{'a':'하나','b':'둘'});self.assertFalse(duplicates)
        raw=json.dumps({'context':[dict(id='a',text='참고 전용')]},indent=2)
        self.assertEqual(parse_rows(raw,{'a'})[0],{})

    def test_duplicate_id_across_valid_and_repaired_records_is_still_quarantined(self):
        raw=reply([dict(id='a',text='첫째')])+'\n'+malformed(dict(id='a',text='둘째'))+'\n'+reply([dict(id='b',text='다른 줄')])
        found,duplicates,_=parse_rows(raw,{'a','b'})
        self.assertEqual(found,{'b':'다른 줄'});self.assertEqual(duplicates,{'a'})

    def test_duplicate_json_keys_never_silently_choose_last_id(self):
        raw='{"id":"a","id":"b","text":"애매함"}\n'+reply([dict(id='c',text='독립 행')])
        found,_,_=parse_rows(raw,{'a','b','c'})
        self.assertEqual(found,{'c':'독립 행'})

    def test_reverse_key_order_is_allowed_but_ids_are_not_coerced(self):
        raw=malformed(dict(text='역순도 유지',id='opaque'))
        self.assertEqual(parse_rows(raw,{'opaque'})[0],{'opaque':'역순도 유지'})
        self.assertEqual(parse_rows(malformed(dict(id='001',text='짧은 번호')),{'opaque'})[0],{})

    def test_a_missing_final_record_is_not_fabricated(self):
        raw=reply([dict(id='a',text='완성')])+'\n'+malformed(dict(id='b',text='복구'))+'\n{"id":"c","text":"잘림'
        self.assertEqual(parse_rows(raw,{'a','b','c'})[0],{'a':'완성','b':'복구'})

    def test_request_omits_empty_speaker_but_keeps_known_speaker_and_metadata(self):
        for actor in ('Actor',''):
            with self.subTest(actor=actor),tempfile.TemporaryDirectory() as root:
                raw=ass(['猫']).replace(b',Actor,',(','+actor+',').encode())
                model=Model();Engine(Path(root)).translate(config(),[('01.ass',raw)],client=model)
                row=model.calls[0][0]['translate'][0]
                if actor:self.assertEqual(row['speaker'],actor)
                else:self.assertNotIn('speaker',row)

    def test_pipeline_only_retries_the_two_real_tag_failures_after_syntax_recovery(self):
        def result(m,data,kwargs):
            rows=[answer(r) for r in data['translate']]
            if len(m.calls)==1:
                for i in (9,11):rows[i]['text']=rows[i]['text'].replace('⟪S2L:1⟫','')
                lines=[json.dumps(r,ensure_ascii=False) if i in (0,2) else malformed(r,'comma' if i==4 else 'quote') for i,r in enumerate(rows)]
                return ChatResult(',\n'.join(lines),'stop')
            return ChatResult(reply(rows),'stop')
        raw=ass([r'{\pos(10,20)}猫{\fscx50}たち{\fscx100}が来た']*12)
        with tempfile.TemporaryDirectory() as root:
            model=Model(result);updates=[]
            Engine(Path(root)).translate(config(),[('01.ass',raw)],client=model,progress=lambda **d:updates.append(d))
            self.assertEqual([len(c[0]['translate']) for c in model.calls],[12,2])
            self.assertEqual({r['id'] for r in model.calls[1][0]['translate']},
                             {model.calls[0][0]['translate'][i]['id'] for i in (9,11)})
            self.assertTrue(any('외부 문법 10개 복구' in d.get('message','') for d in updates))

    def test_pipeline_does_not_retry_valid_rows_after_an_unrepairable_record(self):
        def result(m,data,kwargs):
            rows=[answer(r) for r in data['translate']]
            if len(m.calls)==1:
                return ChatResult(reply(rows[:1])+'\n{"id":"'+rows[1]['id']+'","text":"잘림\n'+reply(rows[2:]),'stop')
            return ChatResult(reply(rows),'stop')
        with tempfile.TemporaryDirectory() as root:
            model=Model(result)
            Engine(Path(root)).translate(config(),[('01.ass',ass(['猫']*12))],client=model)
            self.assertEqual([len(c[0]['translate']) for c in model.calls],[12,1])
            self.assertEqual(model.calls[1][0]['translate'][0]['id'],model.calls[0][0]['translate'][1]['id'])
