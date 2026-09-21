# -*- coding: utf-8 -*-
"""Тесты профилей подключений (src/utils.py)."""

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

from src import utils  # noqa: E402


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ssh_connections.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(utils.load_connections(self.path), {})

    def test_roundtrip(self):
        data = {
            "vps": {
                "host": "89.125.126.18",
                "port": "22",
                "username": "sheldon",
                "password": "s3cret-пароль",
                "key_file": "C:/keys/id_rsa",
            }
        }
        self.assertTrue(utils.save_connections(self.path, data))
        loaded = utils.load_connections(self.path)
        self.assertEqual(loaded, data)

    def test_password_encrypted_at_rest(self):
        utils.save_connections(
            self.path, {"p": {"host": "h", "username": "u", "password": "TOP-SECRET"}}
        )
        raw = open(self.path, "r", encoding="utf-8").read()
        self.assertNotIn("TOP-SECRET", raw)
        self.assertIn("password", raw)

    def test_save_fills_defaults(self):
        utils.save_connections(self.path, {"p": {"host": "h"}})
        loaded = utils.load_connections(self.path)
        self.assertEqual(loaded["p"]["port"], "22")
        self.assertEqual(loaded["p"]["username"], "root")
        self.assertEqual(loaded["p"]["password"], "")
        self.assertEqual(loaded["p"]["key_file"], "")

    def test_corrupt_json_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(utils.load_connections(self.path), {})

    def test_get_timestamp_format(self):
        self.assertRegex(utils.get_timestamp(), r"^\d{2}:\d{2}:\d{2}$")

    def test_format_remote_path(self):
        self.assertEqual(utils.format_remote_path("/home/u", "a.txt"), "/home/u/a.txt")


if __name__ == "__main__":
    unittest.main()
