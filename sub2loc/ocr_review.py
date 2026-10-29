"""Image-free OCR proposals and explicit, coordinate-bound TXT revisions.

This is a separate review workflow. It never changes source files, translation
checkpoints or automatic summaries; an accepted edit creates a new output copy.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from .api import Client
from .balloon import BalloonDocument
from .engine import atomic_json, Cancelled
from .profiles import template_for
from .review import inspect_translation

REVIEW_PROMPT = Path(__file__).with_name('templates').joinpath('ocr_review_ja.txt').read_text('utf-8').strip()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def coordinate(cue):
    return cue.page, cue.block


def single_line(value, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or re.search(r'[\x00-\x1f\x7f]|⟪S2L:|\{\\|\\[Nnh]', value)
            or re.match(r'^\s*(?:###|\d+\.)', value)):
        raise ValueError('후보는 구분자·제어문자 없는 한 줄의 텍스트여야 합니다.')
    return value.strip()


class ReviewSession:
    def __init__(self, source, root):
        self.source = Path(source).resolve()
        if not 0 < self.source.stat().st_size <= 4*1024*1024:
            raise ValueError('OCR 원문 TXT는 4MB 이하여야 합니다.')
        self.raw = self.source.read_bytes()
        self.doc = BalloonDocument(self.raw)
        self.signature = sha(self.raw)
        self.state_path = Path(root) / 'ocr-review' / (sha((str(self.source)+'\0'+self.signature).encode())+'.json')
        self.rows = {str(c.id):dict(issues=list(c.ocr_warnings), decision='pending') for c in self.doc.cues}
        self.translated = None
        self.output = None
        self.output_raw = None
        self.reported_issues = {}
        self.output_hash = None
        self.generated = None
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text('utf-8'))
            if saved.get('source_sha256') == self.signature:
                for key, row in saved.get('rows', {}).items():
                    if key in self.rows and isinstance(row, dict):
                        self.rows[key].update(row)

    def check_source(self):
        if self.source.read_bytes() != self.raw:
            raise ValueError('원문 파일이 변경되었습니다. OCR 검수 창을 다시 여세요.')

    def save(self):
        self.check_source()
        atomic_json(self.state_path, dict(version=1, source=str(self.source), source_sha256=self.signature,
                                         image_verified=False, rows=self.rows))

    def wire(self, cue):
        return 'ocr-' + sha((self.signature+'\0'+cue.page+'\0'+str(cue.block)+'\0'+cue.text).encode())[:24]

    def neighbors(self, cue):
        return [dict(block=c.block, text=c.text) for c in self.doc.cues
                if c.page == cue.page and c.id != cue.id and abs(c.block-cue.block) <= 2]

    def load_translation(self, path):
        self.check_source()
        path = Path(path).resolve()
        if not 0 < path.stat().st_size <= 4*1024*1024:
            raise ValueError('번역 TXT는 4MB 이하여야 합니다.')
        raw = path.read_bytes()
        doc = BalloonDocument(raw)
        if ([p['name'] for p in self.doc.pages] != [p['name'] for p in doc.pages]
                or [len(p['blocks']) for p in self.doc.pages] != [len(p['blocks']) for p in doc.pages]
                or [coordinate(c) for c in self.doc.cues] != [coordinate(c) for c in doc.cues]):
            raise ValueError('번역 TXT의 페이지·말풍선 번호 또는 빈 블록 구조가 원문과 다릅니다.')
        self.translated = {coordinate(c): c.text for c in doc.cues}
        self.output, self.output_hash, self.output_raw = path, sha(raw), raw
        self.generated = None
        self.reported_issues = {}
        report = path.with_suffix('.review.json')
        if report.exists():
            data = json.loads(report.read_text('utf-8'))
            for item in data.get('items', []):
                if item.get('resolved') is False and type(item.get('cue')) is int:
                    index = item['cue']-1
                    if 0 <= index < len(self.doc.cues):
                        self.reported_issues[str(index)] = list(item.get('issues', []))

    def current_translation(self, index):
        return self.translated.get(coordinate(self.doc.cues[index]), '') if self.translated is not None else ''

    def issues(self, index):
        return list(dict.fromkeys(self.rows[str(index)].get('issues', [])+self.reported_issues.get(str(index), [])))

    def keep(self, index):
        self.rows[str(index)]['decision'] = 'keep'
        self.save()

    def _client(self, config, client=None):
        if client is not None:
            return client
        client = Client(config['url'], config.get('model', ''), config.get('key', ''), config.get('timeout', 180))
        if not client.model:
            if config.get('provider') != 'polystrata':
                raise ValueError('번역 API 탭에서 모델을 선택하세요.')
            client.model = client.discover()['id']
        return client

    def _chat(self, client, config, messages, check, *, review):
        check()
        effort = config.get('ocr_thinking', 'low') if review else config.get('thinking', 'none')
        if review and effort not in ('none', 'low', 'medium'):
            raise ValueError('OCR Thinking 설정은 OFF, Low, Medium 중에서 선택하세요.')
        poly = config.get('provider') == 'polystrata'
        # The same approved controls as the translation pipeline. No vendor
        # fields are sent to a generic OpenAI-compatible service.
        result = client.chat(messages, max_tokens=min(config.get('max_tokens', 8192), 4096 if review else 2048),
                             temperature=0.2 if review else config.get('temperature', 0.7),
                             stream=config.get('stream', False),
                             reasoning_effort=effort if poly and effort != 'server' else None,
                             thinking_enabled=False if poly and effort == 'none' else None,
                             check_cancel=check)
        check()
        if result.finish_reason != 'stop':
            raise ValueError('검수/재번역 응답이 완성되지 않았습니다. 기존 원문과 번역은 유지했습니다.')
        return result.content

    def _candidate(self, cue, value):
        value = single_line(value, max(120, min(2000, len(cue.text)*3+40)))
        if re.search(r'[ぁ-ゖァ-ヺ一-鿿]', cue.text) and re.search(r'[가-힣]', value):
            raise ValueError('OCR 교정 후보는 한국어 번역이 아닌 일본어 원문이어야 합니다.')
        return value

    def scan(self, config, *, indices=None, client=None, check=lambda: None, progress=lambda _: None):
        """Incrementally persist only flagged records; no proposal is auto-applied."""
        self.check_source()
        client = self._client(config, client)
        pending = list(range(len(self.doc.cues))) if indices is None else list(dict.fromkeys(indices))
        if any(type(i) is not int or not 0 <= i < len(self.doc.cues) for i in pending):
            raise ValueError('검수할 블록 번호가 올바르지 않습니다.')
        completed = 0
        while pending:
            check()
            group, rows = [], []
            budget = min(config.get('input_tokens', 20000), config.get('context_tokens', 32768)-5120)
            for index in pending[:20]:
                c = self.doc.cues[index]
                row = dict(id=self.wire(c), page=c.page, block=c.block, text=c.text, context=self.neighbors(c))
                trial = rows+[row]
                messages = [dict(role='system', content=REVIEW_PROMPT),
                            dict(role='user', content=json.dumps(dict(rows=trial, fixed_notes=config.get('notes', '')[:2000]), ensure_ascii=False))]
                if len(json.dumps(messages, ensure_ascii=False).encode())+128 > budget:
                    if not rows:
                        raise ValueError(f'{c.page} / {c.block}: OCR 검수 입력 예산이 부족합니다. 원문을 자르지 않았습니다.')
                    break
                group.append(index); rows = trial
            progress(f'OCR 모델 검수 {completed+1}–{completed+len(group)} / {completed+len(pending)} · 이미지 미확인')
            # Rebuild for the accepted group (the last trial may exceed budget).
            messages[-1]['content'] = json.dumps(dict(rows=rows, fixed_notes=config.get('notes', '')[:2000]), ensure_ascii=False)
            raw = self._chat(client, config, messages, check, review=True)
            def unique(items):
                result = {}
                for k, v in items:
                    if k in result: raise ValueError('OCR 검수 JSON의 키가 중복됐습니다.')
                    result[k] = v
                return result
            try:
                data = json.loads(raw, object_pairs_hook=unique)
                if not isinstance(data, dict) or set(data) != {'suspects'} or not isinstance(data['suspects'], list):
                    raise ValueError('OCR 검수 응답은 suspects 배열을 가진 JSON이어야 합니다.')
                expected = {self.wire(self.doc.cues[i]): i for i in group}
                updates = {}; seen = set()
                for item in data['suspects']:
                    if not isinstance(item, dict): raise ValueError('검수 행 형식 오류')
                    key = item.get('id')
                    if not isinstance(key, str) or key not in expected or key in seen:
                        raise ValueError('OCR 검수 ID가 누락·중복·변경됐습니다.')
                    seen.add(key)
                    reason = single_line(item.get('reason'), 600)
                    if item.get('confidence') not in ('low', 'medium'):
                        raise ValueError('이미지 미확인 검수의 확신도 형식이 올바르지 않습니다.')
                    index = expected[key]; candidate = item.get('candidate')
                    if candidate is not None: candidate = self._candidate(self.doc.cues[index], candidate)
                    updates[str(index)] = dict(reason=reason, candidate=candidate, confidence=item['confidence'])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError('OCR 검수 JSON 형식 오류 · 기존 결과는 유지했습니다.') from exc
            check()
            for key, update in updates.items():
                self.rows[key].update(update)
                self.rows[key]['issues'] = list(dict.fromkeys(self.rows[key].get('issues', [])+['모델 OCR 의심 · 이미지 미확인']))
            self.save()  # A later request/cancellation cannot lose prior groups.
            completed += len(group); del pending[:len(group)]
        return completed

    def retranslate(self, index, candidate, config, *, client=None, check=lambda: None):
        from .identified import PLAIN_PROTOCOL, parse_rows
        self.check_source()
        if self.translated is None:
            raise ValueError('기본 번역 TXT를 먼저 불러오세요. 선택한 블록만 다시 번역합니다.')
        cue = self.doc.cues[index]
        candidate = self._candidate(cue, candidate)
        identity = self.wire(cue)
        data = dict(work=config.get('work', ''), fixed_notes=config.get('notes', ''),
                    context=self.neighbors(cue), original_ocr=cue.text,
                    translate=[dict(id=identity, page=cue.page, block=cue.block, text=candidate)])
        cfg = dict(config, input_format='balloon')
        system = template_for(cfg)+'\n'+config.get('instructions', '')+'\n'+PLAIN_PROTOCOL
        system += '\nThe user selected this proposed corrected source for a new translation. Translate only its text; original_ocr is reference only.'
        messages = [dict(role='system', content=system), dict(role='user', content=json.dumps(data, ensure_ascii=False))]
        if len(json.dumps(messages, ensure_ascii=False).encode())+128 > min(config.get('input_tokens', 20000),config.get('context_tokens',32768)-3072):
            raise ValueError('선택 블록과 문맥이 재번역 예산을 넘습니다. 기존 결과를 유지했습니다.')
        raw = self._chat(self._client(config, client), config, messages, check, review=False)
        values, duplicate, unknown = parse_rows(raw, {identity})
        if duplicate or unknown or set(values) != {identity} or not isinstance(values[identity], str):
            raise ValueError('선택 블록의 응답 ID·번역문 형식 오류 · 적용하지 않았습니다.')
        text = single_line(values[identity])
        problems = inspect_translation(replace(cue, text=candidate), text,
                                       korean=config.get('target', '한국어').casefold() in ('한국어','ko','ko-kr','korean'),
                                       allowed=config.get('allowed_foreign', '').splitlines())
        check()
        self.generated = dict(index=index, candidate=candidate, text=text, issues=problems)
        return dict(self.generated)

    def apply(self, index, candidate, text):
        from .workspace import write_result
        self.check_source()
        if self.generated is None or (index, candidate, text) != (self.generated['index'], self.generated['candidate'], self.generated['text']):
            raise ValueError('현재 후보로 재번역한 미리보기를 먼저 확인하세요.')
        if self.generated['issues']:
            raise ValueError('재번역에 검수 의심 항목이 남아 있습니다. 후보를 수정해 다시 시도하거나 기존 번역을 유지하세요.')
        if self.output.read_bytes() != self.output_raw:
            raise ValueError('불러온 번역 파일이 변경됐습니다. 다시 불러오세요.')
        cue = self.doc.cues[index]
        translations = dict(self.translated)
        translations[coordinate(cue)] = text
        raw = self.doc.render({str(c.id):translations[coordinate(c)] for c in self.doc.cues})
        saved = write_result(self.output, raw)
        self.rows[str(index)].update(decision='applied', candidate=candidate, translation=text,
                                     previous_translation=self.current_translation(index))
        atomic_json(saved.with_suffix('.ocr-review.json'), dict(source=str(self.source), source_sha256=self.signature,
                    image_verified=False, output_sha256=sha(raw), rows=self.rows))
        self.save()
        self.translated, self.output, self.output_hash, self.output_raw = translations, saved, sha(raw), raw
        self.generated = None
        return saved
