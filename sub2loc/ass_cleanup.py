"""Opt-in removal of inline colour/emphasis, preserving unrelated ASS controls."""
import re

# Match complete known commands only. In particular, \c must never eat \clip,
# \b must never eat \bord/\blur, and \s must never eat \shad/\shad variants.
DECORATION = re.compile(
    r'\\(?:[1-4]?c)(?:[ \t]*&[Hh][0-9a-fA-F]{1,8}&?|[ \t]*[+-]?\d+)?(?=\\|[ \t,)]|$)'
    r'|\\[bius](?:[ \t]*[+-]?\d+)?(?=\\|[ \t,)]|$)')
EMPTY_TRANSFORM = re.compile(r'\\t\([\d+.,\- \t]*\)')


def strip_inline_decoration(text):
    def clean(match):
        original = match.group()[1:-1]
        value = DECORATION.sub('', original)
        if value == original:
            return match.group()
        value = EMPTY_TRANSFORM.sub('', value)
        return '{' + value + '}' if value.strip() else ''
    return re.sub(r'\{[^{}]*\}', clean, text)
