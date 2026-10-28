"""Desktop queue, explicit work matching, and safe output planning (no Qt dependency)."""
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Callable
from .subtitles import document_for, output_format
from .api import base_url, OutputLimitError
from .jako import mode_for
from .engine import Engine, Cancelled, atomic_json, digest
from .timing import ThroughputETA


def natural_key(value):
    return tuple((1, int(x)) if x.isdigit() else (0, x.casefold()) for x in re.split(r'(\d+)', str(value)))


def default_data_dir():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'sub2loc'
    return Path.home() / '.sub2loc'


@dataclass
class Work:
    id: str
    title: str
    native: str = ''
    year: str = ''
    format: str = ''
    episodes: str = ''
    notes: str = ''
    reference: str = ''
    instructions: str = ''
    glossary: str = ''

    @classmethod
    def manual(cls, title):
        title = title.strip()
        if not title or len(title) > 200:
            raise ValueError('작품명은 1–200자로 입력하세요.')
        return cls('manual:' + digest(title.casefold())[:24], title)


@dataclass
class Entry:
    path: str
    work_id: str = ''
    status: str = '미매칭'


@dataclass
class Workspace:
    works: dict[str, Work] = field(default_factory=dict)
    entries: list[Entry] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        if not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text('utf-8'))
        ws = cls({key: Work(**value) for key, value in data['works'].items()},
                 [Entry(**value) for value in data['entries']], data.get('settings', {}))
        for entry in ws.entries:
            if entry.status in ('번역 중', '대기', '중단', '오류'):
                entry.status = '준비' if entry.work_id in ws.works else '미매칭'
        ws.normalize_folder_order()
        ws.settings.pop('key', None)
        return ws

    def normalize_folder_order(self):
        folders = {}
        for entry in self.entries:
            folders.setdefault(str(Path(entry.path).parent), []).append(entry)
        self.entries = [entry for group in folders.values() for entry in group]

    def save(self, path):
        atomic_json(Path(path), {'version': 1, 'works': {k: asdict(v) for k, v in self.works.items()},
                                 'entries': [asdict(e) for e in self.entries],
                                 'settings': {k: v for k, v in self.settings.items() if k != 'key'}})

    def add_paths(self, paths):
        found, errors = [], []
        for value in paths:
            path = Path(value)
            if path.is_dir():
                for folder, _, names in os.walk(path, followlinks=False, onerror=lambda e: errors.append(str(e))):
                    found.extend(Path(folder) / n for n in names if n.lower().endswith(('.ass', '_source.txt')))
            elif path.is_file() and path.suffix.lower() in ('.ass','.txt'):
                found.append(path)
            else:
                errors.append(f'ASS 파일 또는 폴더가 아닙니다: {path}')
        existing = {os.path.normcase(e.path) for e in self.entries}
        count = 0
        for path in sorted(found, key=natural_key):
            # Never import our own generated outputs recursively.
            if path.name.lower().endswith(('.translated.ass','.translated.txt')):
                continue
            resolved = str(path.resolve())
            if os.path.normcase(resolved) not in existing:
                self.entries.append(Entry(resolved))
                existing.add(os.path.normcase(resolved))
                count += 1
        self.normalize_folder_order()
        return count, errors

    def assign(self, indices, work):
        previous = self.works.get(work.id)
        if previous:
            # Re-matching must never discard the user's names/character notes.
            work.glossary = previous.glossary
            work.notes = previous.notes
            work.instructions = previous.instructions
            work.reference = previous.reference or work.reference
        self.works[work.id] = work
        for i in indices:
            self.entries[i].work_id = work.id
            self.entries[i].status = '준비'

    def groups(self):
        groups = {}
        for i, entry in enumerate(self.entries):
            if entry.work_id not in self.works:
                raise ValueError(f'작품을 매칭하세요: {Path(entry.path).name}')
            groups.setdefault(entry.work_id, []).append((i, entry))
        if not groups:
            raise ValueError('폴더나 ASS 파일을 먼저 추가하세요.')
        return [(self.works[key], values) for key, values in groups.items()]


def safe_component(title):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:70]
    # Prefix also avoids reserved Windows device names (CON, AUX, etc.).
    return 'work-' + (clean or 'untitled')


def write_result(path, raw):
    """Reuse identical outputs; never silently overwrite an existing different file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path
    for n in range(1, 10000):
        try:
            with candidate.open('xb') as f:
                f.write(raw)
            return candidate
        except FileExistsError:
            if candidate.read_bytes() == raw:
                return candidate
            # Keep the selected output extension and generated-file marker.
            suffix = '.translated' + path.suffix
            base = path.name.removesuffix(suffix)
            candidate = path.with_name(f'{base} ({n + 1}){suffix}')
    raise ValueError('동일 이름의 결과 파일이 너무 많습니다.')


def validate_settings(settings):
    config = dict(settings)
    if type(config.get('use_summary',False)) is not bool:raise ValueError('누적 문맥 요약 설정을 확인하세요.')
    output_format(config)
    if type(config.get('strip_ass_decoration',False)) is not bool:
        raise ValueError('ASS 색상·강조 제거 설정이 올바르지 않습니다.')
    config['url'] = base_url(config.get('url', ''))
    if (not config.get('model', '').strip() and config.get('provider') != 'polystrata') or not config.get('target', '').strip():
        raise ValueError('모델명과 번역 언어를 입력하세요.')
    for key, low, high in (('batch_size', 1, 200), ('timeout', 5, 1800), ('max_tokens', 256, 32768)):
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f'{key} 설정이 올바르지 않습니다.')
    if config.get('encoding') not in ('utf-8-sig', 'utf-16', 'cp932', 'cp949'):
        raise ValueError('원본 인코딩을 확인하세요.')
    config.setdefault('context_tokens', 4096)
    if type(config['context_tokens']) is not int or not 1024 <= config['context_tokens'] <= 131072:
        raise ValueError('컨텍스트 크기는 1024–131072 범위로 입력하세요.')
    if type(config.get('hide_readings', False)) is not bool:
        raise ValueError('독음 숨기기 설정이 올바르지 않습니다.')
    if config.get('provider', 'openai') not in ('openai','polystrata'):
        raise ValueError('API 제공자 설정이 올바르지 않습니다.')
    if 'temperature' in config and (type(config['temperature']) not in (int,float) or not math.isfinite(config['temperature']) or not 0 <= config['temperature'] <= 2):
        raise ValueError('Temperature는 0–2 범위로 입력하세요.')
    if type(config.get('stream',False)) is not bool or config.get('thinking','none') not in ('server','none','low','medium','high'):
        raise ValueError('스트리밍/Thinking 설정이 올바르지 않습니다.')
    if mode_for(config) == 'identified':
        config.setdefault('input_tokens', min(20000, config['context_tokens']-config['max_tokens']-1024))
        if type(config['input_tokens']) is not int or not 256 <= config['input_tokens'] <= 131072 or config['input_tokens']+config['max_tokens']+1024 > config['context_tokens']:
            raise ValueError('입력 예산 + 최대 출력 + 여유 1024토큰이 컨텍스트 이하여야 합니다.')
        if type(config.get('quality_retries',2)) is not int or not 0 <= config.get('quality_retries',2) <= 3:
            raise ValueError('검수 재번역은 0–3회로 설정하세요.')
        for key in ('prompt_template','allowed_foreign'):
            if not isinstance(config.get(key,''),str) or len(config.get(key,''))>20000:
                raise ValueError('프롬프트/허용 표기는 20000자 이하의 문자열이어야 합니다.')
        if 'prompt_template' in config and not config['prompt_template'].strip():
            raise ValueError('번역 프롬프트를 입력하세요.')
    return config


def translate_queue(workspace: Workspace, settings, root, output_root, cancel=None,
                    progress: Callable = lambda **kwargs: None, client=None):
    """Run every matched work in queue order; continuity is separated by stable work ID."""
    cancel = cancel or threading.Event()
    settings = validate_settings(settings)
    groups = workspace.groups()
    plans = []
    # Preflight the complete queue, so a bad later file cannot waste API requests.
    for work, entries in groups:
        files, targets, names = [], [], {}
        for _, entry in entries:
            path = Path(entry.path)
            if not path.is_file() or not 0 < path.stat().st_size <= 4 * 1024 * 1024:
                raise ValueError(f'파일이 없거나 크기가 4MB를 넘습니다: {path.name}')
            raw = path.read_bytes()
            document_for(raw, settings, path.name)
            name = path.stem
            names[name.casefold()] = names.get(name.casefold(), 0) + 1
            files.append((path, raw))
        folder = Path(output_root) / (safe_component(work.title) + '-' + digest(work.id)[:8])
        for path, _ in files:
            stem = path.stem[:160]
            if names[path.stem.casefold()] > 1:
                stem += '-' + digest(str(path))[:8]
            targets.append(folder / (stem + '.translated.' + ('txt' if path.suffix.lower()=='.txt' else output_format(settings))))
        plans.append((work, entries, files, targets))
    engine, results = Engine(Path(root) / 'translations'), []
    queue_total = sum(len(document_for(raw,settings,str(path)).cues) for _,_,source,_ in plans for path,raw in source)
    prior_done = 0; prior_metrics = dict(requests=0,retries=0,errors=0)
    queue_eta = ThroughputETA()
    for work_index, (work, entries, source, targets) in enumerate(plans):
        if cancel.is_set():
            raise Cancelled('중단했습니다. 같은 목록·설정으로 다시 시작하면 이어서 처리합니다.')
        notes = ('작품 용어집:\n'+work.glossary+'\n\n' if work.glossary.strip() else '') + work.notes
        if work.instructions.strip() and mode_for(settings) == 'ja-ko-vn':
            notes += '\n\n번역 지침 (사용자 설정):\n' + work.instructions
        config = dict(settings, work=work.title, work_id=work.id,
                      notes=(notes + '\n\n' + work.reference).strip())
        if work.instructions.strip(): config['instructions'] = work.instructions.strip()
        # Stable path-aware internal names avoid duplicate basenames across folders.
        files = [(f'{i + 1:04d}-{digest(str(path))[:8]}-{path.name}', raw)
                 for i, (path, raw) in enumerate(source)]
        mapping = {name: index for (name, _), (index, _) in zip(files, entries)}
        work_done = [0]; work_metrics = dict(requests=0,retries=0,errors=0)
        def report(**data):
            work_done[0] = data.get('done',work_done[0])
            data['global_done'] = prior_done + work_done[0]
            data['global_total'] = queue_total
            for key in work_metrics:
                if key in data:
                    work_metrics[key] = data[key]
                    data[key] += prior_metrics[key]
            data.update(queue_eta.update(data.get('phase'),data['global_done'],queue_total))
            data.update(work=work.title, work_index=work_index + 1, work_total=len(plans))
            if 'file' in data:
                data['index'] = mapping[data['file']]
                if 'message' in data:
                    original = Path(workspace.entries[data['index']].path).name
                    data['message'] = data['message'].replace(data['file'], original)
            progress(**data)
        try:
            outputs, context = engine.translate(config, files, cancel, report, client)
        except OutputLimitError as exc:
            if exc.filename in mapping:
                exc.filename = Path(workspace.entries[mapping[exc.filename]].path).name
            raise
        review = getattr(engine, 'last_review', [])
        for (index, _), (output_name, raw), target in zip(entries, outputs, targets):
            saved = write_result(target, raw)
            results.append(str(saved))
            internal_name = files[[i for i, _ in entries].index(index)][0]
            file_review = [dict(r,file=Path(workspace.entries[index].path).name) for r in review if r.get('file') == internal_name]
            if file_review:
                atomic_json(saved.with_suffix('.review.json'), {'file': saved.name, 'mechanical_checks_only': True, 'items': file_review})
            progress(index=index, status='완료', output=str(saved), work=work.title)
        prior_done += sum(len(document_for(raw,settings,str(path)).cues) for path,raw in source)
        for key in prior_metrics: prior_metrics[key] += work_metrics[key]
        # The engine's project file remains the canonical continuity checkpoint.
        atomic_json(targets[0].parent / 'context.json', {'work': work.title, 'id': work.id, 'context': context})
    return results
