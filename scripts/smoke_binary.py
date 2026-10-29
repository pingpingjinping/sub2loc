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
subprocess.run([str(binary), '--smoke-test', str(screenshot)], timeout=60, check=True)
if not (binary.parent / 'data' / 'workspace.json').is_file():
    raise SystemExit('The default portable data folder was not created next to the EXE.')
print('Default portable EXE data storage passed.')
ocr_screenshot = screenshot.with_name('windows-ocr-smoke.png')
with tempfile.TemporaryDirectory() as data:
    subprocess.run([str(binary), '--data-dir', data, '--smoke-ocr-test', str(ocr_screenshot)], timeout=60, check=True)
if not ocr_screenshot.is_file() or ocr_screenshot.stat().st_size < 1000:
    raise SystemExit('The frozen OCR review window did not render.')
print('Frozen OCR review dialog and bundled prompt loading passed (no API).')
