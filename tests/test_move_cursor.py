# -*- coding: utf-8 -*-
"""Тесты move_cursor.py (конфиг + раскладка клавиш; хук не запускаем)."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

import move_cursor as mc  # noqa: E402


class TestVkMapping(unittest.TestCase):
    def test_known_keys(self):
        self.assertEqual(mc._vk("page_up"), 0x21)
        self.assertEqual(mc._vk("Page_Down"), 0x22)
        self.assertEqual(mc._vk("f12"), 0x7B)
        self.assertEqual(mc._vk("home"), 0x24)

    def test_disabled_and_unknown(self):
        self.assertEqual(mc._vk("none"), 0)
        self.assertEqual(mc._vk(""), 0)
        self.assertEqual(mc._vk("bogus"), 0)
        self.assertEqual(mc._vk(None), 0)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "move_cursor.json")
        self.log_path = os.path.join(self.tmp.name, "move_cursor.log")
        self._patches = [
            mock.patch.object(mc, "CONFIG_FILE", self.cfg_path),
            mock.patch.object(mc, "LOG_FILE", self.log_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_defaults_when_missing(self):
        cfg = mc._load_config()
        self.assertEqual(cfg["up_key"], "page_up")
        self.assertEqual(cfg["pause_key"], "f12")
        self.assertIn("notches", cfg)
        self.assertTrue(os.path.exists(self.cfg_path))

    def test_partial_override_merges(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"notches": 7}, f)
        cfg = mc._load_config()
        self.assertEqual(cfg["notches"], 7)
        self.assertEqual(cfg["down_key"], "page_down")

    def test_invalid_json_resets_to_defaults(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        cfg = mc._load_config()
        self.assertEqual(cfg["notches"], mc.DEFAULTS["notches"])
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), mc.DEFAULTS)

    def test_scroller_vk_parsing(self):
        scroller = mc.Scroller(dict(mc.DEFAULTS))
        self.assertEqual(scroller._up_vk, 0x21)
        self.assertEqual(scroller._dn_vk, 0x22)
        self.assertEqual(scroller._pause_vk, 0x7B)
        self.assertFalse(scroller._paused)

    def test_log_respects_flag(self):
        mc._log("hidden", {"log": False})
        self.assertFalse(os.path.exists(self.log_path))
        mc._log("shown", {"log": True})
        self.assertTrue(os.path.exists(self.log_path))
        with open(self.log_path, "r", encoding="utf-8") as f:
            self.assertIn("shown", f.read())

    def test_set_data_dir_moves_config_and_log(self):
        data_dir = os.path.join(self.tmp.name, "user_data")
        mc._set_data_dir(data_dir)
        self.assertTrue(os.path.isdir(data_dir))
        self.assertEqual(
            os.path.normpath(mc.CONFIG_FILE),
            os.path.join(data_dir, "move_cursor.json"))
        self.assertEqual(
            os.path.normpath(mc.LOG_FILE),
            os.path.join(data_dir, "move_cursor.log"))


if __name__ == "__main__":
    unittest.main()
