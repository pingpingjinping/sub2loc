import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sub2loc.api import ApiError, OutputLimitError
from sub2loc.ass import AssDocument
from sub2loc.chunks import choose_end, split_text
from sub2loc.engine import Engine, PROMPT_VERSION, digest
from sub2loc.workspace import validate_settings
from test_jako import SAMPLE, Translator, config


def document(texts):
    head = '[Script Info]\r\nTitle: context test\r\n[Events]\r\nFormat: Start, End, Style, Name, Text\r\n'
    return ('\ufeff' + head + ''.join(
        f'Dialogue: 0:00:{i:02}.00,0:00:{i+1:02}.00,Default,名前,{text}\r\n'
        for i, text in enumerate(texts))).encode('utf-8')


class Echo:
    key = 'test-key'
    def __init__(self): self.sources = []; self.prompts = []; self.limits = []
    def complete_text(self, prompt, limit):
        source = prompt.split('<start_of_turn>user\n')[1].split('<end_of_turn>')[0]
        self.sources.append(source); self.prompts.append(prompt); self.limits.append(limit)
        return source


class ChunkTests(unittest.TestCase):
    def test_packing_uses_context_and_cue_ceiling_without_truncating_source(self):
        cues = AssDocument(document(['あ' * 110] * 12)).cues
        end = choose_end(cues, 0, 12, '', [], 4096, 4096)
        self.assertGreater(end, 1); self.assertLess(end, 12)
        self.assertEqual(choose_end(cues, 0, 2, '', [], 4096, 4096), 2)
        hints = '用語=용어\n' * 1000
        smaller = choose_end(cues, 0, 12, hints, [], 4096, 4096)
        self.assertLess(smaller, end)
        with tempfile.TemporaryDirectory() as folder:
            client = Echo(); cfg = config(); cfg['notes'] = hints
            raw = document(['あ' * 110] * 12)
            outputs, _ = Engine(Path(folder)).translate(cfg, [('test.ass', raw)], client=client)
            self.assertEqual(outputs[0][1], raw)
            self.assertEqual(sum(len(s.splitlines()) for s in client.sources), 12)
            for prompt, limit in zip(client.prompts, client.limits):
                self.assertLessEqual(len(prompt.encode()) + limit + 64, 4096)

    def test_long_cue_keeps_every_character_controls_bom_and_timing(self):
        text = r'{\i1}  ' + '日本語の長い文章です。' * 300 + r'  \N 後半です。{\i0}'
        raw = document([text])
        for budget in (32, 100, 1000):
            parts = list(split_text(text, budget))
            self.assertEqual(''.join(parts), text)
            self.assertTrue(all(len(p.encode()) <= budget for p in parts))
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(); cfg['context_tokens'] = 1024
            client = Echo()
            outputs, _ = Engine(Path(folder)).translate(cfg, [('long.ass', raw)], client=client)
            self.assertEqual(outputs[0][1], raw)
            self.assertGreater(len(client.sources), 1)
            for source, prompt, limit in zip(client.sources, client.prompts, client.limits):
                self.assertNotIn(r'\N', source); self.assertNotIn('{', source)
                self.assertLessEqual(len(prompt.encode()) + limit + 64, 1024)

    def test_wrong_line_count_splits_and_checkpoints_valid_prefix_before_leaf_failure(self):
        class Mismatched(Echo):
            def complete_text(self, prompt, limit):
                source = super().complete_text(prompt, limit)
                if '\n' in source: return '잘못 합쳐진 번역'
                if source == '第三': raise OutputLimitError(limit, '반복')
                return source
        raw = document(['第一', '第二', '第三', '第四'])
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); client = Mismatched()
            with self.assertRaises(OutputLimitError) as caught:
                engine.translate(config(), [('split.ass', raw)], client=client)
            self.assertEqual(caught.exception.first, 3)
            self.assertEqual(client.sources.count('第三'), 1)
            checkpoint = json.loads(next(Path(folder).glob('*/runs/*.json')).read_text())
            self.assertEqual(checkpoint['offset'], 2)
            self.assertEqual(set(checkpoint['translations']), {'0', '1'})
            recovered = Echo()
            outputs, _ = engine.translate(config(), [('split.ass', raw)], client=recovered)
            self.assertEqual(recovered.sources, ['第三\n第四'])
            self.assertIn('第一 → 第一', recovered.prompts[0])
            self.assertEqual(outputs[0][1], raw)

    def test_output_limit_retries_smaller_requests_and_recombines_original_positions(self):
        class Limited(Echo):
            def complete_text(self, prompt, limit):
                source = super().complete_text(prompt, limit)
                if '\n' in source: raise OutputLimitError(limit, '잘림')
                return '번역' + source
        raw = document(['第一', '第二', '第三', '第四'])
        with tempfile.TemporaryDirectory() as folder:
            client = Limited()
            output, _ = Engine(Path(folder)).translate(config(), [('test.ass', raw)], client=client)
            self.assertEqual(len(client.sources), 7)
            self.assertEqual(len(set(client.sources)), 7)
            before, after = AssDocument(raw), AssDocument(output[0][1])
            for original, translated in zip(before.cues, after.cues):
                self.assertEqual(original.prefix, translated.prefix)
                self.assertEqual(original.ending, translated.ending)
                self.assertEqual('번역' + original.text, translated.text)

    def test_tag_separated_single_cue_can_split_without_saving_partial_cue(self):
        class Limited(Echo):
            def complete_text(self, prompt, limit):
                source = super().complete_text(prompt, limit)
                if '\n' in source: raise OutputLimitError(limit, '잘림')
                return source
        raw = document([r'{\i1}前半{\i0}後半\N次の行'])
        with tempfile.TemporaryDirectory() as folder:
            client = Limited(); updates = []
            output, _ = Engine(Path(folder)).translate(config(), [('test.ass', raw)], client=client,
                                                       progress=lambda **d: updates.append(d))
            self.assertEqual(output[0][1], raw)
            self.assertEqual([d['done'] for d in updates if d.get('phase') == 'validated'], [1])

    def test_transient_api_failure_retries_only_current_request(self):
        class Temporary(Echo):
            def complete_text(self, prompt, limit):
                source = super().complete_text(prompt, limit)
                if len(self.sources) == 1: raise ApiError('Temporary failure')
                return source
        with tempfile.TemporaryDirectory() as folder:
            client = Temporary(); raw = document(['第一', '第二'])
            output, _ = Engine(Path(folder)).translate(config(), [('test.ass', raw)], client=client)
            self.assertEqual(output[0][1], raw)
            self.assertEqual(client.sources, ['第一\n第二'] * 2)

    def test_context_setting_validation_and_old_incomplete_checkpoint_migration(self):
        self.assertEqual(validate_settings(config())['context_tokens'], 4096)
        for value in (True, 512, 131073, '4096'):
            with self.assertRaises(ValueError): validate_settings(dict(config(), context_tokens=value))
        from sub2loc.jako import remember
        cfg = config(); cfg['context_tokens'] = 4096
        old_identity = {k: v for k, v in cfg.items() if k not in ('key', 'api_mode', 'context_tokens')}
        old_identity['request_mode'] = 'ja-ko-vn-v1'
        old_id = digest({'version': PROMPT_VERSION, 'config': old_identity,
                         'files': [('old.ass', hashlib.sha256(SAMPLE).hexdigest())]})
        cue = AssDocument(SAMPLE).cues[0]
        translated = cue.text.replace('おはよう、ミサキ。', '좋은 아침, 미사키.')
        pairs, context = remember(cue, translated, [])
        with tempfile.TemporaryDirectory() as folder:
            project_id = digest({'work': cfg['work'], 'mode': 'ja-ko-vn'})[:24]
            old_path = Path(folder) / project_id / 'runs' / (old_id + '.json')
            old_path.parent.mkdir(parents=True)
            old_path.write_text(json.dumps(dict(file=0, offset=1, translations={'0': translated},
                                                outputs=[], context=context, recent_pairs=pairs)))
            client = Translator()
            Engine(Path(folder)).translate(cfg, [('old.ass', SAMPLE)], client=client)
            self.assertEqual(len(client.prompts), 1)
            self.assertNotIn('おはよう、ミサキ。', client.prompts[0].split('<start_of_turn>user\n')[1])
            self.assertEqual(json.loads(old_path.read_text())['offset'], 1)


if __name__ == '__main__': unittest.main()
