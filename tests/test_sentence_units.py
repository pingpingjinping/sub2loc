import os
from pathlib import Path
import tempfile
import unittest

from sub2loc.ass import AssDocument, TOKEN
from sub2loc.engine import Engine
from sub2loc.workspace import validate_settings
from test_jako import config


def ass(texts):
    return ('\ufeff[Events]\r\nFormat: Layer, Start, End, Style, Name, Text\r\n' + ''.join(
        f'Dialogue: 0,0:00:01.00,0:00:04.00,Default,,{text}\r\n' for text in texts)).encode('utf-8')


def source(prompt):
    return prompt.split('<start_of_turn>user\n')[1].split('<end_of_turn>')[0]


class SentenceUnitTests(unittest.TestCase):
    def test_typography_does_not_create_isolated_suffix_or_counter_requests(self):
        raw = ass([r'{\pos(50,80)}ネコ{\fscx50}たち{\fscx100}が来た！',
                   r'{\pos(50,120)}３{\fscx100}台ある。'])
        answer = '\n'.join([r'{\pos(50,80)}고양이{\fscx50}들{\fscx100}이 왔다!',
                            r'{\pos(50,120)}차가 ３{\fscx100}대 있다.'])
        class Model:
            calls = []
            def complete_text(self, prompt, limit):
                self.calls.append(source(prompt)); return answer
        with tempfile.TemporaryDirectory() as folder:
            model = Model(); output, _ = Engine(Path(folder)).translate(config(), [('sample.ass', raw)], client=model)
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(model.calls[0].splitlines(), [c.restore(c.text) for c in AssDocument(raw).cues])
            self.assertNotIn('たち', model.calls[0].splitlines())
            before, after = AssDocument(raw), AssDocument(output[0][1])
            for a, b in zip(before.cues, after.cues):
                self.assertEqual(a.prefix, b.prefix); self.assertEqual(a.protected, b.protected)
            self.assertIn('고양이', output[0][1].decode('utf-8-sig'))

    def test_changed_native_controls_are_rejected_before_safe_span_fallback(self):
        raw = ass([r'{\pos(50,80)}ネコ{\fscx50}たち'])
        class Model:
            def __init__(self): self.calls = []
            def complete_text(self, prompt, limit):
                text = source(prompt); self.calls.append(text)
                if '{' in text: return r'{\pos(999,999)}고양이들'
                return '고양이\n들'
        with tempfile.TemporaryDirectory() as folder:
            model = Model(); updates = []
            output, _ = Engine(Path(folder)).translate(config(), [('sample.ass', raw)], client=model,
                                                       progress=lambda **d: updates.append(d))
            self.assertEqual(len(model.calls), 2)
            result = output[0][1].decode('utf-8-sig')
            self.assertNotIn('999', result); self.assertIn(r'{\pos(50,80)}고양이{\fscx50}들', result)
            self.assertTrue(any('구간별' in d.get('message', '') for d in updates))

    def test_failed_tagged_native_request_is_not_repeated_across_batches_and_files(self):
        raw = ass([r'{\pos(50,80)}' + word + r'{\fscx50}たち' for word in ('猫', '犬', '鳥', '牛', '馬')])
        class Model:
            def __init__(self, reject): self.calls = []; self.reject = reject
            def complete_text(self, prompt, limit):
                text = source(prompt); self.calls.append(text)
                return text.replace(r'\pos(50,80)', r'\pos(999,999)') if self.reject and '{' in text else text
        for reject in (True, False):
            with self.subTest(reject=reject), tempfile.TemporaryDirectory() as folder:
                cfg = dict(config(), batch_size=2); model = Model(reject); engine = Engine(Path(folder))
                files = [('first.ass', raw), ('second.ass', raw)]
                outputs, _ = engine.translate(cfg, files, client=model)
                native = [text for text in model.calls if '{' in text]
                self.assertEqual(len(native), 1 if reject else 6)
                self.assertTrue(all(result == raw for _, result in outputs))
                count = len(model.calls)
                engine.translate(cfg, files, client=model)
                self.assertEqual(len(model.calls), count)

    def test_readings_opt_in_comments_original_rows_and_preserves_encoding_and_controls(self):
        raw = ass([r'{\pos(80,42)\fscx50\fscy50}ねこ', r'{\pos(40,96)}猫が来た。',
                   r'{\pos(50,42)\fscx50}ああ', r'{\pos(60,42)\fscx50\fscy50}Hi'])
        original = AssDocument(raw)
        self.assertEqual(original.reading_count, 1); self.assertEqual(len(original.cues), 4)
        hidden = AssDocument(raw, hide_readings=True)
        self.assertEqual(len(hidden.cues), 3)
        output = hidden.render({str(c.id): c.text for c in hidden.cues})
        self.assertTrue(output.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(output, raw.replace(b'Dialogue:', b'Comment:', 1))
        self.assertEqual(len(AssDocument(output).cues), 3)
        with self.assertRaises(ValueError): validate_settings(dict(config(), hide_readings='true'))

    def test_plain_small_text_other_time_and_missing_base_are_not_hidden(self):
        lone = AssDocument(ass([r'{\pos(80,42)\fscx50\fscy50}ねこ']), hide_readings=True)
        self.assertEqual(lone.reading_count, 0); self.assertEqual(len(lone.cues), 1)
        raw = ass([r'{\pos(80,42)\fscx50\fscy50}ねこ', r'{\pos(40,96)}猫が来た。'])
        raw = raw.replace(b'0:00:01.00,0:00:04.00', b'0:00:05.00,0:00:08.00', 1)
        self.assertEqual(AssDocument(raw).reading_count, 0)

    def test_hidden_readings_never_reach_model_and_reopening_uses_same_cache(self):
        raw = ass([r'{\pos(80,42)\fscx50\fscy50}ねこ', r'{\pos(40,96)}猫が来た。'])
        class Echo:
            def __init__(self): self.calls = []
            def complete_text(self, prompt, limit):
                self.calls.append(source(prompt)); return source(prompt)
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); model = Echo(); cfg = dict(config(), hide_readings=True)
            output, _ = engine.translate(cfg, [('sample.ass', raw)], client=model)
            self.assertEqual(len(model.calls), 1); self.assertNotIn('ねこ', model.calls[0])
            self.assertIn(b'Comment:', output[0][1])
            engine.translate(cfg, [('sample.ass', raw)], client=model)
            self.assertEqual(len(model.calls), 1)


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from sub2loc.desktop import MainWindow
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Install desktop requirements')
class ReadingSettingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def test_reading_option_is_off_by_default_and_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(Path(folder))
            self.assertFalse(window.hide_readings.isChecked())
            window.hide_readings.setChecked(True); window.close()
            restored = MainWindow(Path(folder))
            self.assertTrue(restored.settings()['hide_readings']); restored.close()


if __name__ == '__main__': unittest.main()
