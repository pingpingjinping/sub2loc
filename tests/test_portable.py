from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sub2loc.workspace import default_data_dir


class PortablePathTests(unittest.TestCase):
    def test_frozen_path_uses_exe_not_working_directory_or_appdata(self):
        with tempfile.TemporaryDirectory() as root:
            exe=(Path(root)/'installed'/'sub2loc.exe').resolve()
            with patch('sub2loc.workspace.sys.frozen',True,create=True),patch('sub2loc.workspace.sys.executable',str(exe)):
                self.assertEqual(default_data_dir(),exe.parent/'data')

    def test_source_path_is_repo_data(self):
        with patch('sub2loc.workspace.sys.frozen',False,create=True):
            self.assertEqual(default_data_dir(),Path(__file__).resolve().parents[1]/'data')
