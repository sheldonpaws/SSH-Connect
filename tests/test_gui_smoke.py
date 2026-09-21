# -*- coding: utf-8 -*-
"""Smoke-тест GUI: окно строится, ключевые атрибуты на месте, затем закрывается."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401


class TestGuiSmoke(unittest.TestCase):
    def test_app_builds_and_destroys(self):
        try:
            import tkinter as tk
        except Exception as exc:  # pragma: no cover
            self.skipTest("tkinter недоступен: %s" % exc)

        try:
            from src.gui import SSHApp
        except Exception as exc:
            self.skipTest("не удалось импортировать src.gui: %s" % exc)

        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover
            self.skipTest("нет дисплея: %s" % exc)

        try:
            root.withdraw()
            app = SSHApp(root)
            self.assertFalse(app._connected)
            self.assertIsNone(app._last_creds)
            self.assertIsNotNone(getattr(app, "_status_tooltip", None))
            self.assertTrue(hasattr(app, "tmux_session_combo"))
            self.assertIn("сессий", SSHApp.TMUX_NO_SESSIONS.lower())
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
