from pathlib import Path
import tempfile
import unittest

from sub2loc.ass import AssDocument
from sub2loc.ass_cleanup import strip_inline_decoration
from sub2loc.engine import Engine
from sub2loc.framing import frame_for
from sub2loc.subtitles import document_for
from sub2loc.workspace import validate_settings
from test_identified import ass, config, Model


class AssCleanupTests(unittest.TestCase):
    def test_removes_exact_inline_colour_and_emphasis_commands_only(self):
        source=r'{\pos(10,20)\c&H00FFFF00\1c&HFFFFFF&\2c&H112233&\3c&H44&\4c&H55&\b700\i1\u1\s1}台詞{\b0\i0\u0\s0\c}'
        self.assertEqual(strip_inline_decoration(source),r'{\pos(10,20)}台詞')

    def test_similarly_named_tags_and_unrelated_layout_visibility_drawing_stay_exact(self):
        tags=r'\clip(1,2,3,4)\iclip(1,2,3,4)\bord2\blur1\shad3\fs40\fscx50\fscy100\alpha&H80&\1a&HFF&\p1'
        self.assertEqual(strip_inline_decoration('{'+tags+r'\b1}m 0 0 l 10 20{\p0}'),'{'+tags+r'}m 0 0 l 10 20{\p0}')

    def test_removes_empty_colour_transforms_but_keeps_other_transformed_tags(self):
        self.assertEqual(strip_inline_decoration(r'{\t(0,500,\c&HFF0000&\b1)}字'), '字')
        self.assertEqual(strip_inline_decoration(r'{\t(0,500,\c&HFF0000&\bord2)}字'), r'{\t(0,500,\bord2)}字')
        self.assertEqual(strip_inline_decoration(r'{\t(0,500,\clip(1,2,3,4)\i1)}字'),r'{\t(0,500,\clip(1,2,3,4))}字')

    def test_plain_dialogue_comments_and_unknown_malformed_commands_are_not_rewritten(self):
        for source in (r'台詞 \b1', '{comment}', r'{\colourX\border2\c&HZZZ&\b1.5}字'):
            with self.subTest(source=source):self.assertEqual(strip_inline_decoration(source),source)

    def test_render_preserves_ass_metadata_and_breaks_and_changes_only_selected_text_tags(self):
        source=r'{\pos(20,30)\c&H0000FF&}猫{\b1}たち\N来た{\b0\fscx50}！'
        raw=ass([source])
        original=AssDocument(raw)
        default=document_for(raw,config(strip_ass_decoration=False))
        self.assertEqual(default.render({'0':default.cues[0].text}),raw)
        cleaned=document_for(raw,config(strip_ass_decoration=True))
        output=cleaned.render({'0':cleaned.cues[0].text})
        expected=raw.replace(source.encode(),r'{\pos(20,30)}猫たち\N来た{\fscx50}！'.encode())
        self.assertEqual(output,expected)
        self.assertEqual(cleaned.cues[0].prefix,original.cues[0].prefix)
        self.assertEqual((cleaned.cues[0].start,cleaned.cues[0].end),('0:00:01.00','0:00:04.00'))

    def test_width_boundaries_like_408_remain_and_are_still_strictly_checked(self):
        raw=ass([r'{\pos(320,496)\c&H00FFFF00}前{\fscx50}　{\fscx100}後'])
        doc=document_for(raw,config(strip_ass_decoration=True));frame=frame_for(doc.cues[0])
        self.assertEqual(frame.tokens,['⟪S2L:0⟫','⟪S2L:1⟫'])
        with self.assertRaises(ValueError):frame.restore(frame.source+'⟪S2L:2⟫')

    def test_cleanup_pipeline_is_separate_but_disabled_option_reuses_existing_ass_cache(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=Model();files=[('01.ass',ass([r'前{\c&HFF0000&}後']))]
            engine.translate(config(),files,client=model)
            engine.translate(config(strip_ass_decoration=False),files,client=model)
            self.assertEqual(len(model.calls),1)
            out,_=engine.translate(config(strip_ass_decoration=True),files,client=model)
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[1][0]['recent_translations'],[])
            self.assertNotIn('⟪S2L:',model.calls[1][0]['translate'][0]['text'])
            self.assertNotIn(r'\c&H',out[0][1].decode('utf-8-sig'))

    def test_cleanup_preference_is_ignored_for_srt_smi_without_invalidating_plain_cache(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));model=Model();files=[('01.ass',ass([r'前{\b1}後']))]
            engine.translate(config(output_format='srt'),files,client=model)
            engine.translate(config(output_format='smi',strip_ass_decoration=True),files,client=model)
            self.assertEqual(len(model.calls),1)
        with self.assertRaises(ValueError):validate_settings(config(strip_ass_decoration='true'))
