"""Context-bounded complete cues with direct, cue-level alignment recovery."""
import re
import threading
from .api import ApiError, OutputLimitError, safe_preview
from .ass import TOKEN
from .jako import PromptError, prompt_for, remember
from .layout import layout_for


class AlignmentError(ValueError):
    pass


def source_budget(notes, pairs, max_tokens, context_tokens):
    base, limit = prompt_for('', '', [], max_tokens, context_tokens, multiline=True)
    hints = len(notes.encode('utf-8')) + sum(len(str(p).encode('utf-8')) for p in pairs[-6:])
    reserve = min(hints, 768, context_tokens // 6)
    return max(32, context_tokens - limit - 64 - len(base.encode('utf-8')) - reserve)


def split_text(text, budget):
    """Only split a cue that exceeds context; prefer sentence boundaries."""
    while text:
        prefix = text.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
        if not prefix:
            raise PromptError('컨텍스트 예산이 너무 작습니다.')
        if len(prefix) < len(text):
            boundaries = list(re.finditer(r'[。！？!?](?:[」』”】])*\s*|\s+', prefix))
            suitable = [m.end() for m in boundaries if m.end() >= len(prefix) // 2]
            if suitable:
                prefix = prefix[:suitable[-1]]
        yield prefix
        text = text[len(prefix):]


def choose_end(cues, start, batch_size, notes, pairs, max_tokens, context_tokens):
    budget = source_budget(notes, pairs, max_tokens, context_tokens)
    used, end = 0, start
    while end < len(cues) and end - start < batch_size:
        cost = len(layout_for(cues[end]).source.encode('utf-8')) + 1
        if end > start and used + cost > budget:
            break
        used += cost; end += 1
    return end


def translate_adaptive(cues, client, notes, pairs, max_tokens, context_tokens, *,
                       on_chunk, progress=lambda **kwargs: None, check_cancel=lambda: None,
                       cancel=None):
    """Reject ambiguous batches once, then checkpoint complete cues directly."""
    cancel = cancel or threading.Event()
    budget = source_budget(notes, pairs, max_tokens, context_tokens)
    layouts = [layout_for(cue) for cue in cues]

    def request(sources, history, first, last):
        source = '\n'.join(sources)
        prompt, limit = prompt_for(source, notes, history, max_tokens, context_tokens,
                                   multiline=len(sources) > 1)
        for attempt in range(3):
            check_cancel()
            progress(phase='request', first=first, last=last,
                     message=f'{first}–{last} 대사 · {len(sources)}개 문장 요청 (태그는 로컬 보존)')
            try:
                raw = client.complete_text(prompt, limit)
                break
            except OutputLimitError as exc:
                exc.first, exc.last = first, last
                raise exc.with_source(source, getattr(client, 'key', '')) from None
            except ApiError as exc:
                if attempt == 2 or not exc.retryable:
                    raise
                progress(phase='retry', message=f'API 요청 실패 · 재시도 {attempt + 2}/3')
                if cancel.wait(1 + attempt):
                    check_cancel()
        rows = raw.strip().splitlines()
        # With one cue, visual line breaks cannot shift a different cue.
        # Original ASS line breaks stay local and replace this whitespace.
        if len(sources) == 1:
            rows = [' '.join(row.strip() for row in rows if row.strip())]
        if len(rows) != len(sources) or any(not row.strip() for row in rows):
            raise AlignmentError(
                f'대사 {first}–{last}: 번역 줄 수 불일치 (원문 {len(sources)}줄 / 응답 {len(rows)}줄).\n'
                '요청한 원문:\n' + safe_preview(source, getattr(client, 'key', ''), 240) +
                '\n생성된 출력 미리보기:\n' + safe_preview(raw, getattr(client, 'key', '')))
        for row in rows:
            if TOKEN.search(row) or re.search(r'[{}\r\n]|\\[Nnh]', row):
                raise AlignmentError(f'대사 {first}–{last}: 번역문에 새 ASS 제어문자가 포함되었습니다.')
        # Keep whitespace at context-split seams without adding spaces within
        # Japanese/Korean words. Echoes retain the complete original layout.
        return [original if row.strip() == original.strip() else
                original[:len(original) - len(original.lstrip())] + row.strip() +
                (original[len(original.rstrip()):] if original.rstrip() != original else '')
                for original, row in zip(sources, rows)]

    def accept(group, values, history):
        translated = {str(plan.cue.id): plan.render(value) for plan, value in zip(group, values)}
        context = ''
        for plan in group:
            history, context = remember(plan.cue, translated[str(plan.cue.id)], history)
        # Encoding/persistence errors must never be mistaken for model errors.
        on_chunk([plan.cue for plan in group], translated, history, context)
        return history

    def individual(plan, history):
        check_cancel()
        parts = list(split_text(plan.source, budget))
        if len(parts) > 1:
            progress(phase='retry', message=f'대사 {plan.cue.id + 1}: 문장 전체가 컨텍스트 예산을 넘어 {len(parts)}개로 나눕니다')
        values = [request([part], history, plan.cue.id + 1, plan.cue.id + 1)[0] for part in parts]
        return accept([plan], [''.join(values)], history)

    if len(layouts) == 1:
        return individual(layouts[0], pairs)
    try:
        values = request([plan.source for plan in layouts], pairs, cues[0].id + 1, cues[-1].id + 1)
    except (AlignmentError, OutputLimitError, PromptError) as exc:
        progress(phase='retry', message=str(exc).splitlines()[0] +
                 ' · 이 묶음만 대사별 문장 전체로 처리합니다 (반복 반분·태그 구간 분할 없음)')
        history = pairs
        for plan in layouts:
            history = individual(plan, history)
        return history
    return accept(layouts, values, pairs)
