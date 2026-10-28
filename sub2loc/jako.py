"""Single-turn completions adapter for hell0ks/ja-ko-vn-12b-v2.

Prompt framing follows the author's GGUF template:
https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf/blob/main/template
"""
import re
from .ass import TOKEN

INSTRUCTION = '당신은 전문 일한 번역가입니다. 주어진 일본어를 한국어로 번역하세요.'
MODES = ('auto', 'chat', 'ja-ko-vn')


class PromptError(ValueError):
    pass


def mode_for(config):
    mode = config.get('api_mode', 'auto')
    if mode not in MODES:
        raise ValueError('API 호출 방식이 올바르지 않습니다.')
    model = config.get('model', '').lower().replace('_', '-')
    if mode == 'auto':
        mode = 'ja-ko-vn' if 'ja-ko-vn' in model and '12b' in model else 'chat'
    if mode == 'ja-ko-vn':
        if config.get('target', '한국어').strip().casefold() not in ('한국어', '한글', 'ko', 'ko-kr', 'korean', '한국말'):
            raise ValueError('ja-ko-vn 전용 모드는 일본어 → 한국어만 지원합니다.')
        if config.get('source', 'auto').strip().casefold() not in ('auto', '자동', '일본어', 'ja', 'jp', 'ja-jp', 'japanese', '日本語'):
            raise ValueError('ja-ko-vn 전용 모드의 원문 언어는 일본어 또는 auto로 설정하세요.')
    return mode


def prompt_for(source, notes, pairs, max_tokens):
    """Never truncate source. Limit hints conservatively for the tested 4096 context.

UTF-8 bytes provide a conservative token-count bound for the byte-fallback
vocabulary. Leave space for output and framing; no tokenizer dependency needed.
"""
    max_tokens = min(max_tokens, 1024)
    prefix = '<start_of_turn>system\n' + INSTRUCTION
    suffix = '<end_of_turn>\n<start_of_turn>user\n' + source + '<end_of_turn>\n<start_of_turn>model\n'
    marker = ' 번역 지침: '
    budget = 4096 - max_tokens - 64 - len((prefix + suffix + marker).encode('utf-8'))
    if budget < 0:
        raise PromptError('한 대사가 모델의 4096 컨텍스트 예산보다 깁니다. 원문 대사를 나누거나 최대 출력 토큰을 줄이세요.')
    # The user's fixed names/terms take precedence. Recent aligned text is only
    # a reference; this translator is never asked to infer a plot summary.
    history = '\n'.join(f"{p.get('speaker', '')}: {p['source']} → {p['target']}" for p in reversed(pairs[-6:]))
    guide = notes.strip() + ('\n직전 대사 참고 (최신순):\n' + history if history else '')
    # A copied subtitle or note must not inject additional template turns.
    if any(token in source for token in ('<start_of_turn>', '<end_of_turn>', '<bos>', '<eos>')):
        raise PromptError('원문에 모델 프롬프트 구분자가 포함되어 있습니다.')
    for token in ('<start_of_turn>', '<end_of_turn>', '<bos>', '<eos>'):
        guide = guide.replace(token, '')
    guide = guide.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
    return prefix + (marker + guide if guide else '') + suffix, max_tokens


def translate_cue(cue, client, notes, pairs, max_tokens, check_cancel=lambda: None):
    """Translate visible spans; restore ASS controls locally, never via the model."""
    output, pos = [], 0
    boundaries = list(TOKEN.finditer(cue.text))
    for match in boundaries + [None]:
        end = match.start() if match else len(cue.text)
        text = cue.text[pos:end]
        if text:
            body = text.strip()
            # Punctuation, spaces, and numeric-only spans need no model request.
            if not body or not re.search(r'[^\W\d_]', body, re.UNICODE):
                output.append(text)
            else:
                check_cancel()
                prompt, limit = prompt_for(body, notes, pairs, max_tokens)
                translated = client.complete_text(prompt, limit).strip()
                if not translated:
                    raise ValueError('번역문이 비어 있습니다.')
                start = len(text) - len(text.lstrip())
                trail = len(text) - len(text.rstrip())
                output.append(text[:start] + translated + (text[len(text) - trail:] if trail else ''))
        if match:
            output.append(match.group())
            pos = match.end()
    translated = ''.join(output)
    cue.restore(translated)
    return translated


def remember(cue, translated, pairs):
    pair = {'speaker': cue.speaker, 'source': TOKEN.sub('', cue.text), 'target': TOKEN.sub('', translated)}
    # Store a bounded window of real translations instead of model-invented facts.
    pairs = (pairs + [pair])[-6:]
    return pairs, '\n'.join(f"{p['speaker']}: {p['source']} → {p['target']}" for p in pairs)
