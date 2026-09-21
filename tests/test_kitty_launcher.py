# -*- coding: utf-8 -*-
"""Тесты web-desktop/backend/kitty_launcher.py (Win32 PostMessage)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND  # noqa: E402,F401

import kitty_launcher as kl  # noqa: E402


class TestKittyLauncher(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(kl.WM_CHAR, 0x0102)
        self.assertEqual(kl.WM_KEYDOWN, 0x0100)
        self.assertEqual(kl.WM_KEYUP, 0x0101)

    def test_send_command_no_window(self):
        original = kl.find_kitty_window
        kl.find_kitty_window = lambda: None
        try:
            ok, msg = kl.send_command("echo hi")
        finally:
            kl.find_kitty_window = original
        self.assertFalse(ok)
        self.assertEqual(msg, "Окно KiTTY не найдено")

    def test_send_command_stale_window_handle(self):
        original = kl.find_kitty_window
        kl.find_kitty_window = lambda: 987654321
        try:
            ok, msg = kl.send_command("echo hi")
        finally:
            kl.find_kitty_window = original
        self.assertFalse(ok)
        self.assertIn("недоступно", msg)


if __name__ == "__main__":
    unittest.main()
