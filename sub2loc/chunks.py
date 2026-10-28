"""Context-budgeted ASS chunks with positional alignment and adaptive splitting."""
from dataclasses import dataclass
import re
import threading
from .api import ApiError, OutputLimitError, safe_preview
from .ass import TOKEN, protect
from .jako import PromptError, prompt_for, remember


class AlignmentError(ValueError):
    pass


@dataclass
class Piece:
    source: str
    before: str = ''
    after: str = ''


@dataclass
class CuePlan:
    cue: object
    parts: list

    @property
    def pieces(self):
        return [part for part in self.parts if isinstance(part, Piece)]

    def render(self, translated):
        text = ''.join(part if isinstance(part, str) else
                       part.before + translated[id(part)] + part.after for part in self.parts)
        self.cue.restore(text)
        return text


def source_budget(notes, pairs, max_tokens, context_tokens):
    base, limit = prompt_for('', '', [], max_tokens, context_tokens, multiline=True, preserve_controls=True)
    # Reserve some context for names and recent translations before packing input.
    hints = len(notes.encode('utf-8')) + sum(len(str(p).encode('utf-8')) for p in pairs[-6:])
    reserve = min(hints, 768, context_tokens // 6)
    return max(32, context_tokens - limit - 64 - len(base.encode('utf-8')) - reserve)


def split_text(text, budget):
    """Retain every source character; prefer punctuation/space boundaries."""
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


def make_plan(cue, budget):
    parts, pos = [], 0
    for match in list(TOKEN.finditer(cue.text)) + [None]:
        end = match.start() if match else len(cue.text)
        text = cue.text[pos:end]
        for chunk in split_text(text, budget):
            body = chunk.strip()
            if not body or not re.search(r'[^\W\d_]', body, re.UNICODE):
                parts.append(chunk)
            else:
                first = len(chunk) - len(chunk.lstrip())
                trail = len(chunk) - len(chunk.rstrip())
                parts.append(Piece(body, chunk[:first], chunk[len(chunk) - trail:] if trail else ''))
        if match:
            parts.append(match.group()); pos = match.end()
    return CuePlan(cue, parts)


def choose_end(cues, start, batch_size, notes, pairs, max_tokens, context_tokens):
    budget = source_budget(notes, pairs, max_tokens, context_tokens)
    used, end = 0, start
    while end < len(cues) and end - start < batch_size:
        cost = len(cues[end].restore(cues[end].text).encode('utf-8')) + 1
        if end > start and used + cost > budget:
            break
        used += cost; end += 1
    return end


def translate_adaptive(cues, client, notes, pairs, max_tokens, context_tokens, *,
                       on_chunk, progress=lambda **kwargs: None, check_cancel=lambda: None,
                       cancel=None):
    """Commit each validated subchunk immediately; failed results never shift rows."""
    cancel = cancel or threading.Event()
    budget = source_budget(notes, pairs, max_tokens, context_tokens)
    plans = [make_plan(cue, budget) for cue in cues]

    def request(pieces, history, first, last, *, preserve_controls=False):
        source = '\n'.join(p.source for p in pieces)
        prompt, limit = prompt_for(source, notes, history, max_tokens, context_tokens,
                                   multiline=len(pieces) > 1, preserve_controls=preserve_controls)
        for attempt in range(3):
            check_cancel()
            progress(phase='request', first=first, last=last,
                     message=f'{first}–{last} 대사 · {len(pieces)}개 텍스트 구간 요청')
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
        if len(rows) != len(pieces) or any(not row.strip() for row in rows):
            raise AlignmentError(
                f'대사 {first}–{last}: 번역 줄 수 불일치 (원문 {len(pieces)}줄 / 응답 {len(rows)}줄).\n'
                '요청한 원문:\n' + safe_preview(source, getattr(client, 'key', ''), 240) +
                '\n생성된 출력 미리보기:\n' + safe_preview(raw, getattr(client, 'key', '')))
        # A model may not inject a new protected token or ASS control into a span.
        for row in rows:
            if not preserve_controls and (TOKEN.search(row) or re.search(r'[{}\r\n]|\\[Nnh]', row)):
                raise AlignmentError(f'대사 {first}–{last}: 번역문에 새 ASS 제어문자가 포함되었습니다.')
        return {id(piece): row.strip() for piece, row in zip(pieces, rows)}

    def split_reason(exc, count):
        reason = '출력 한도' if isinstance(exc, OutputLimitError) else (
                 '컨텍스트 예산' if isinstance(exc, PromptError) else '줄·태그 대응')
        progress(phase='retry', message=f'{reason} 확인 · {count}개 묶음을 더 작게 나눕니다')

    def translate_pieces(pieces, history, cue_id):
        if not pieces:
            return {}
        try:
            return request(pieces, history, cue_id, cue_id)
        except (AlignmentError, OutputLimitError, PromptError) as exc:
            if len(pieces) <= 1:
                raise
            split_reason(exc, len(pieces))
            mid = len(pieces) // 2
            result = translate_pieces(pieces[:mid], history, cue_id)
            result.update(translate_pieces(pieces[mid:], history, cue_id))
            return result

    def run(group, history):
        check_cancel()
        pieces = [piece for plan in group for piece in plan.pieces]
        try:
            # Keep the complete cue in one model input, including its original
            # controls: typography must not turn words or counters into isolated
            # fragments. Accept only the exact original control sequence.
            native = [Piece(plan.cue.restore(plan.cue.text)) for plan in group]
            values = request(native, history, group[0].cue.id + 1, group[-1].cue.id + 1,
                             preserve_controls=True) if pieces else {id(p): p.source for p in native}
            translated = {}
            for plan, piece in zip(group, native):
                masked, controls = protect(values[id(piece)])
                if controls != plan.cue.protected:
                    raise AlignmentError('번역문에서 원본 ASS 태그·줄바꿈의 개수·순서·내용이 달라졌습니다.')
                plan.cue.restore(masked)
                translated[str(plan.cue.id)] = masked
        except (OutputLimitError, ValueError) as exc:
            if len(group) > 1:
                split_reason(exc, len(group))
                mid = len(group) // 2
                history = run(group[:mid], history)
                return run(group[mid:], history)
            # Keep the complete cue atomic even when its style-separated parts
            # need smaller requests. A one-piece failure is not retried unchanged.
            if not pieces or (not isinstance(exc, PromptError) and len(pieces) == 1
                              and group[0].cue.restore(group[0].cue.text).strip() == pieces[0].source):
                raise
            progress(phase='retry', message='문장 단위 태그 검증·예산 확인 실패 · 보호 구간별 번역으로 전환합니다')
            values = translate_pieces(pieces, history, group[0].cue.id + 1)
            translated = {str(group[0].cue.id): group[0].render(values)}
        context = ''
        for plan in group:
            history, context = remember(plan.cue, translated[str(plan.cue.id)], history)
        # Persistence/encoding failures are deliberately outside the recovery try.
        on_chunk([plan.cue for plan in group], translated, history, context)
        return history

    return run(plans, pairs)
