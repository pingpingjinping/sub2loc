"""Build a portable Windows folder containing sub2loc.exe and its Qt runtime."""
import hashlib
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows EXE는 Windows에서 빌드하세요. GitHub Actions도 사용할 수 있습니다.')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
                    '--onedir', '--noupx', '--name', 'sub2loc', '--version-file',
                    str(ROOT / 'scripts/windows-version.txt'), str(ROOT / 'desktop_entry.py')], cwd=ROOT, check=True)
    target = ROOT / 'dist/sub2loc'
    for name in ('README.md', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, target / name)
    shutil.copytree(ROOT / 'licenses', target / 'licenses', dirs_exist_ok=True)
    for name in ('PySide6-Essentials', 'shiboken6', 'PyInstaller'):
        distribution = metadata.distribution(name)
        folder = target / 'licenses' / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'METADATA.txt').write_text(str(distribution.metadata), encoding='utf-8')
        for item in distribution.files or []:
            if any(part.lower() in ('licenses', 'license', 'copying.txt') for part in item.parts):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    shutil.copy2(source, folder / source.name)
    for source in (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'LICENSE'):
        if source.is_file():
            shutil.copy2(source, target / 'licenses/PYTHON-LICENSE.txt')
    archive = Path(shutil.make_archive(str(ROOT / 'dist/sub2loc-windows-x64'), 'zip', ROOT / 'dist', 'sub2loc'))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ROOT / 'dist/SHA256SUMS.txt').write_text(checksum + '  ' + archive.name + '\n', encoding='utf-8')
    print(f'Built {archive.name} ({archive.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
