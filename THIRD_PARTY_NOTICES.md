# Third-party software

The Windows distribution includes Python, Qt for Python (PySide6 Essentials and Shiboken6), Qt runtime libraries, and the PyInstaller bootloader. Versions and package metadata are included in `licenses/`.

- Python: https://www.python.org/ — Python Software Foundation License (license text included when available from the build interpreter).
- Qt for Python / Shiboken6: https://pyside.org/ — the open-source packages are distributed under LGPL-3.0-only (alternative GPL licenses are listed by the upstream package). Source: https://code.qt.io/cgit/pyside/pyside-setup.git/ . Version used: 6.12.0.
- Qt libraries: https://www.qt.io/ — source: https://code.qt.io/cgit/qt/ . Qt licensing information: https://doc.qt.io/qt-6/licenses-used-in-qt.html . Use the corresponding Qt version supplied by PySide6 6.12.0.
- PyInstaller: https://pyinstaller.org/ — GPL with an exception for applications built with PyInstaller; its upstream license is copied from the build package.

The Qt runtime is shipped as separate dynamic libraries in the portable application folder. The distribution includes LGPLv3 and GPLv3 license texts. This notice does not change any upstream software license.
