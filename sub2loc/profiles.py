"""Explicit providers; never infer nonstandard controls from a model name."""
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).with_name('templates').joinpath('anime_ja_ko.txt').read_text('utf-8').strip()
MANGA_TEMPLATE = Path(__file__).with_name('templates').joinpath('manga_ja_ko.txt').read_text('utf-8').strip()
LEGACY_TEMPLATE = Path(__file__).with_name('templates').joinpath('anime_ja_ko_v1.txt').read_text('utf-8').strip()
POLYSTRATA = dict(provider='polystrata', api_mode='identified', url='http://127.0.0.1:8080/v1',
                 model='', temperature=0.7, thinking='none', stream=True,
                 context_tokens=32768, input_tokens=20000, max_tokens=8192, batch_size=100,
                 quality_retries=2, prompt_template=DEFAULT_TEMPLATE, allowed_foreign='')


def template_for(config):
    text = config.get('prompt_template', DEFAULT_TEMPLATE).strip()
    if text == LEGACY_TEMPLATE: text = DEFAULT_TEMPLATE
    if config.get('input_format')=='balloon' and config.get('manga_prompt_template'):
        return config['manga_prompt_template'].strip()
    if config.get('input_format') == 'balloon' and text == DEFAULT_TEMPLATE:
        return MANGA_TEMPLATE
    return text
