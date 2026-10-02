# -*- coding: utf-8 -*-
"""Тесты KittyController (src.gui.py) — запуск kitty.exe с аргументами."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401


def _make_controller():
    try:
        import src.gui  # noqa: F401
    except Exception as exc:
        raise unittest.SkipTest("не удалось импортировать src.gui: %s" % exc)
    from src.gui import KittyController
    return KittyController(on_log=lambda *a, **k: None)


class TestKittyController(unittest.TestCase):
    def test_start_kitty_passes_hostkey_arg(self):
        kit = _make_controller()
        with mock.patch("subprocess.Popen") as popen:
            ok, msg = kit.start_kitty_with_logging(
                "example.com", 22, "root", "pw", None,
                hostkey="ssh-ed25519 AAAA")
        self.assertTrue(ok)
        args = popen.call_args[0][0]
        self.assertIn("-hostkey", args)
        self.assertEqual(
            args[args.index("-hostkey") + 1], "ssh-ed25519 AAAA")

    def test_start_kitty_without_hostkey(self):
        kit = _make_controller()
        with mock.patch("subprocess.Popen") as popen:
            ok, msg = kit.start_kitty_with_logging(
                "example.com", 22, "root", "pw", None)
        self.assertTrue(ok)
        args = popen.call_args[0][0]
        self.assertNotIn("-hostkey", args)

    def test_rejects_bad_hostname(self):
        kit = _make_controller()
        ok, msg = kit.start_kitty_with_logging(
            "evil;rm", 22, "root", "pw", None)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()