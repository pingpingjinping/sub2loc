"""Changed-language text must not change executable ASS structure."""
import random
import unittest
from sub2loc.ass import AssDocument, TOKEN, protect
from sub2loc.layout import layout_for
from test_sentence_units import ass


class LocalLayoutTests(unittest.TestCase):
    def test_controls_stay_in_order_with_changed_text_length_and_leading_trailing_tags(self):
        cue = AssDocument(ass([r'{\pos(80,90)\i1}ネコ{\fscx50}たち{\fscx100}が来た！{\i0}'])).cues[0]
        plan = layout_for(cue)
        self.assertEqual(plan.source, 'ネコたちが来た！')
        target = '고양이들이 여기까지 몰려왔어!'
        raw = cue.restore(plan.render(target))
        masked, controls = protect(raw)
        self.assertEqual(TOKEN.sub('', masked), target)
        self.assertEqual(controls, cue.protected)
        self.assertTrue(raw.startswith(r'{\pos(80,90)\i1}'))
        self.assertTrue(raw.endswith(r'{\i0}'))

    def test_break_is_restored_without_removing_visible_target_characters(self):
        cue = AssDocument(ass([r'{\i1}前半\N後半\h続き\n最後{\i0}'])).cues[0]
        plan = layout_for(cue)
        self.assertEqual(plan.source, '前半 後半 続き 最後')
        target = '먼저 시작하고 나중에 마저 이야기하자'
        raw = cue.restore(plan.render(target))
        masked, controls = protect(raw)
        self.assertEqual(controls, cue.protected)
        self.assertEqual(''.join(TOKEN.sub('', masked).split()), ''.join(target.split()))

    def test_drawing_payload_and_breaks_inside_drawing_never_expose_or_swallow_text(self):
        cue = AssDocument(ass([r'先{\p1}m 0 0 l 10 20\N{\p0}後'])).cues[0]
        plan = layout_for(cue)
        self.assertEqual(plan.source, '先後')
        raw = cue.restore(plan.render('앞과 뒤'))
        masked, controls = protect(raw)
        self.assertEqual(TOKEN.sub('', masked), '앞과 뒤')
        self.assertEqual(controls, cue.protected)

    def test_dense_karaoke_preserves_bytes_without_claiming_syllable_alignment(self):
        cue = AssDocument(ass([r'{\k10}あ{\k20}い{\k30}う'])).cues[0]
        raw = cue.restore(layout_for(cue).render('아름다운 노래'))
        masked, controls = protect(raw)
        self.assertEqual(controls, cue.protected)
        self.assertEqual(TOKEN.sub('', masked), '아름다운 노래')

    def test_random_control_density_and_translation_lengths_preserve_all_visible_characters(self):
        randomizer = random.Random(27)
        controls = [r'{\i1}', r'{\i0}', r'{\fscx50}', r'{\fscx100}', r'\N', r'\h']
        for _ in range(150):
            text = r'{\pos(50,80)}' + ''.join(randomizer.choice(controls) + '日本語' for _ in range(randomizer.randrange(1, 15)))
            cue = AssDocument(ass([text])).cues[0]
            plan = layout_for(cue)
            self.assertEqual(plan.render(plan.source), cue.text)
            target = '한글 대사 ' * randomizer.randrange(1, 15)
            raw = cue.restore(plan.render(target))
            masked, restored = protect(raw)
            self.assertEqual(restored, cue.protected)
            self.assertEqual(''.join(TOKEN.sub('', masked).split()), ''.join(target.split()))
