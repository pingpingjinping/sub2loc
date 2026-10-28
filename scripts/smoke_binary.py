"""Launch the frozen app and verify that the native window renders successfully."""
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path(sys.argv[1]).resolve()
screenshot = Path(sys.argv[2]).resolve()
with tempfile.TemporaryDirectory() as data:
    subprocess.run([str(binary), '--data-dir', data, '--smoke-test', str(screenshot)], timeout=60, check=True)
if not screenshot.is_file() or screenshot.stat().st_size < 1000:
    raise SystemExit('The frozen desktop app did not produce a screenshot.')
print('Frozen app launch and window rendering passed.')
