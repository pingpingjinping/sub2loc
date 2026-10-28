"""Single-turn completions adapter for hell0ks/ja-ko-vn-12b-v2.

Prompt framing follows the author's GGUF template:
https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf/blob/main/template
"""
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


def prompt_for(source, notes, pairs, max_tokens, context_tokens=4096, *, multiline=False):
    """Never truncate source. Limit hints within the configured context.

UTF-8 bytes provide a conservative token-count bound for the byte-fallback
vocabulary. Leave space for output and framing; no tokenizer dependency needed.
"""
    max_tokens = min(max_tokens, context_tokens // 2)
    instruction = INSTRUCTION + (' 원문 한 줄마다 한 줄로 번역하고 줄 수와 순서를 유지하세요.' if multiline else '')
    prefix = '<start_of_turn>system\n' + instruction
    suffix = '<end_of_turn>\n<start_of_turn>user\n' + source + '<end_of_turn>\n<start_of_turn>model\n'
    marker = ' 번역 지침: '
    budget = context_tokens - max_tokens - 64 - len((prefix + suffix + marker).encode('utf-8'))
    if budget < 0:
        raise PromptError('요청 원문이 설정한 컨텍스트 예산보다 깁니다. 원문 대사를 나누거나 최대 출력 토큰을 줄이세요.')
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


def remember(cue, translated, pairs):
    pair = {'speaker': cue.speaker, 'source': TOKEN.sub('', cue.text), 'target': TOKEN.sub('', translated)}
    # Store a bounded window of real translations instead of model-invented facts.
    pairs = (pairs + [pair])[-6:]
    return pairs, '\n'.join(f"{p['speaker']}: {p['source']} → {p['target']}" for p in pairs)
