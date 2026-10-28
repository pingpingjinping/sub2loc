"""Desktop queue, explicit work matching, and safe output planning (no Qt dependency)."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import threading
from typing import Callable
from .ass import AssDocument
from .api import base_url, OutputLimitError
from .jako import mode_for
from .engine import Engine, Cancelled, atomic_json, digest


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
                    found.extend(Path(folder) / n for n in names if n.lower().endswith('.ass'))
            elif path.is_file() and path.suffix.lower() == '.ass':
                found.append(path)
            else:
                errors.append(f'ASS 파일 또는 폴더가 아닙니다: {path}')
        existing = {os.path.normcase(e.path) for e in self.entries}
        count = 0
        for path in sorted(found, key=natural_key):
            # Never import our own generated outputs recursively.
            if path.name.lower().endswith('.translated.ass'):
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
            # Keep .translated.ass suffix so recursive import can filter outputs.
            base = path.name.removesuffix('.translated.ass')
            candidate = path.with_name(f'{base} ({n + 1}).translated.ass')
    raise ValueError('동일 이름의 결과 파일이 너무 많습니다.')


def validate_settings(settings):
    config = dict(settings)
    config['url'] = base_url(config.get('url', ''))
    if not config.get('model', '').strip() or not config.get('target', '').strip():
        raise ValueError('모델명과 번역 언어를 입력하세요.')
    for key, low, high in (('batch_size', 1, 64), ('timeout', 5, 1800), ('max_tokens', 256, 32768)):
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f'{key} 설정이 올바르지 않습니다.')
    if config.get('encoding') not in ('utf-8-sig', 'utf-16', 'cp932', 'cp949'):
        raise ValueError('원본 인코딩을 확인하세요.')
    config.setdefault('context_tokens', 4096)
    if type(config['context_tokens']) is not int or not 1024 <= config['context_tokens'] <= 131072:
        raise ValueError('컨텍스트 크기는 1024–131072 범위로 입력하세요.')
    mode_for(config)
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
            AssDocument(raw, settings['encoding'])
            name = path.stem
            names[name.casefold()] = names.get(name.casefold(), 0) + 1
            files.append((path, raw))
        folder = Path(output_root) / (safe_component(work.title) + '-' + digest(work.id)[:8])
        for path, _ in files:
            stem = path.stem[:160]
            if names[path.stem.casefold()] > 1:
                stem += '-' + digest(str(path))[:8]
            targets.append(folder / (stem + '.translated.ass'))
        plans.append((work, entries, files, targets))
    engine, results = Engine(Path(root) / 'translations'), []
    for work_index, (work, entries, source, targets) in enumerate(plans):
        if cancel.is_set():
            raise Cancelled('중단했습니다. 같은 목록·설정으로 다시 시작하면 이어서 처리합니다.')
        notes = work.notes
        if work.instructions.strip() and mode_for(settings) == 'ja-ko-vn':
            notes += '\n\n번역 지침 (사용자 설정):\n' + work.instructions
        config = dict(settings, work=work.title, work_id=work.id,
                      notes=(notes + '\n\n' + work.reference).strip())
        if work.instructions.strip(): config['instructions'] = work.instructions.strip()
        # Stable path-aware internal names avoid duplicate basenames across folders.
        files = [(f'{i + 1:04d}-{digest(str(path))[:8]}-{path.name}', raw)
                 for i, (path, raw) in enumerate(source)]
        mapping = {name: index for (name, _), (index, _) in zip(files, entries)}
        def report(**data):
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
        for (index, _), (_, raw), target in zip(entries, outputs, targets):
            saved = write_result(target, raw)
            results.append(str(saved))
            progress(index=index, status='완료', output=str(saved), work=work.title)
        # The engine's project file remains the canonical continuity checkpoint.
        atomic_json(targets[0].parent / 'context.json', {'work': work.title, 'id': work.id, 'context': context})
    return results
