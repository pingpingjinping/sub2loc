import json
from pathlib import Path
import tempfile
import unittest
from sub2loc.balloon import BalloonDocument
from sub2loc.engine import Engine
from sub2loc.workspace import Workspace,Work,translate_queue
from sub2loc.catalog import Catalog
from test_identified import config,Model

SOURCE='### 001.png\n\n1. 猫\nが来た\n\n2. \n\n\n### 002.png\n\n1. 犬\n'

class BalloonTests(unittest.TestCase):
    def test_roundtrip_page_block_empty_and_multiline_source(self):
        doc=BalloonDocument(SOURCE.encode())
        self.assertEqual([c.text for c in doc.cues],['猫 が来た','犬'])
        out=doc.render({'0':'고양이가 왔다','1':'개'})
        again=BalloonDocument(out)
        self.assertEqual([p['name'] for p in again.pages],['001.png','002.png'])
        self.assertEqual([len(p['blocks']) for p in again.pages],[2,1])
        self.assertEqual([c.text for c in again.cues],['고양이가 왔다','개'])

    def test_invalid_structure_is_rejected(self):
        for source in ('plain text','### a\n2. 猫','### a\n1. 猫\n1. 犬','### a\n1. 猫\n### a\n1. 犬'):
            with self.subTest(source=source),self.assertRaises(ValueError):BalloonDocument(source.encode())

    def test_model_ids_page_metadata_resume_and_queue_extension(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);path=root/'book_source.txt';path.write_text(SOURCE)
            ws=Workspace();ws.add_paths([root]);ws.assign([0],Work.manual('漫画'))
            model=Model();results=translate_queue(ws,config(),root/'state',root/'out',client=model)
            self.assertEqual(Path(results[0]).suffix,'.txt')
            self.assertEqual(len(BalloonDocument(Path(results[0]).read_bytes()).cues),2)
            rows=model.calls[0][0]['translate']
            self.assertEqual([(r['page'],r['block']) for r in rows],[('001.png',1),('002.png',1)])
            translate_queue(ws,config(),root/'state',root/'out',client=model)
            self.assertEqual(len(model.calls),1)
            self.assertEqual(path.read_text(),SOURCE)

    def test_ocr_warning_is_reported_not_claimed_repaired(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));engine.translate(config(),[('book.txt','### p.png\n1. 猫�'.encode())],client=Model())
            self.assertTrue(any(not r['resolved'] and r.get('page')=='p.png' for r in engine.last_review))

    def test_manga_search_and_character_query_allow_manga_id(self):
        class Stub(Catalog):
            def query(self,q,v):
                self.last=q
                if 'search' in v:return {'Page':{'media':[]}}
                return {'Media':{'characters':{'nodes':[]}}}
        c=Stub();c.search('漫画',media_type='MANGA');self.assertIn('type: MANGA',c.last)
        c.character_notes('anilist:1');self.assertNotIn('type: ANIME',c.last)
