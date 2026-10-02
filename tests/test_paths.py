# -*- coding: utf-8 -*-
"""Тесты src/paths.py (корень программы + папка пользователя user_data)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

from src import paths  # noqa: E402


class TestProjectRoot(unittest.TestCase):
    def test_dev_root_is_repo(self):
        self.assertEqual(
            os.path.normpath(paths.project_root()),
            os.path.normpath(ROOT))

    def test_frozen_root_is_meipass_parent(self):
        meipass = "C:\\Release\\_internal"
        with mock.patch.object(sys, "_MEIPASS", meipass, create=True):
            self.assertEqual(paths.project_root(), "C:\\Release")


class TestUserData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # env указывает на ещё не созданную папку (имитация первого запуска)
        self.ud = os.path.join(self.tmp.name, "appdata")
        self._env = mock.patch.dict(
            os.environ, {"SSH_CONNECT_USER_DATA": self.ud})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_default_dir_is_beside_project(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSH_CONNECT_USER_DATA", None)
            self.assertEqual(
                os.path.normpath(paths.user_data_dir()),
                os.path.normpath(os.path.join(ROOT, "user_data")))

    def test_env_overrides_dir(self):
        self.assertEqual(
            os.path.normpath(paths.user_data_dir()),
            os.path.normpath(self.ud))

    def test_user_data_path_joins_under_dir(self):
        p = paths.user_data_path("ssh_connections.json")
        self.assertEqual(p, os.path.join(self.ud, "ssh_connections.json"))

    def test_ensure_creates_subdirs_and_first_flag(self):
        first = paths.ensure_user_data("SshHostKeys", "Sessions", "Downloads")
        self.assertTrue(first)
        for sub in ("SshHostKeys", "Sessions", "Downloads"):
            self.assertTrue(os.path.isdir(os.path.join(self.ud, sub)))
        second = paths.ensure_user_data()
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()