"""A real one-line translation probe using the same adapter as production jobs."""
from pathlib import Path
import tempfile
from .ass import AssDocument
from .engine import Engine
from .jako import mode_for
from .workspace import validate_settings

SOURCE = '今日はいい天気ですね。'


def test_translation(settings):
    config = validate_settings(settings)
    config = dict(config, work='API 연결 테스트', notes='', timeout=min(config['timeout'], 60))
    raw = ('[Events]\nFormat: Text\nDialogue:' + SOURCE + '\n').encode('utf-8')
    # A probe must not read or modify continuity, uploads, or real work history.
    config['encoding'] = 'utf-8-sig'
    with tempfile.TemporaryDirectory(prefix='sub2loc-probe-') as folder:
        outputs, _ = Engine(Path(folder)).translate(config, [('probe.ass', raw)])
        result = AssDocument(outputs[0][1]).cues[0].text
    return {'mode': mode_for(config), 'source': SOURCE, 'translation': result}
