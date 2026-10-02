# -*- coding: utf-8 -*-
"""Тесты tmux-логики в GUI (парсинг списков, устойчивость к сбоям)."""

import os
import sys
import unittest
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401


class FakeSSH:
    def __init__(self, result):
        self.is_connected = True
        self._result = result

    def execute_command(self, command):
        return self._result


def _make_app():
    try:
        from src.gui import SSHApp
    except Exception as exc:
        raise unittest.SkipTest("не удалось импортировать src.gui: %s" % exc)
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover
        raise unittest.SkipTest("нет дисплея: %s" % exc)
    root.withdraw()
    try:
        return root, SSHApp(root)
    except Exception:
        root.destroy()
        raise


class TestTmuxSessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.app = _make_app()
        cls.app_cls = type(cls.app)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _with_output(self, out):
        self.app.ssh = FakeSSH((True, out))
        self.app._connected = True
        return self.app._tmux_sessions()

    def test_parses_names(self):
        ok, names = self._with_output(
            "0: 2 windows (created Thu ...)\n"
            "work: 1 windows (attached)\n")
        self.assertTrue(ok)
        self.assertEqual(names, ["0", "work"])

    def test_session_name_containing_error_is_kept(self):
        ok, names = self._with_output("error-prod: 3 windows (created ...)\n")
        self.assertTrue(ok)
        self.assertEqual(names, ["error-prod"])

    def test_no_server_running_is_empty(self):
        ok, names = self._with_output(
            "no server running on /tmp/tmux-0/default")
        self.assertTrue(ok)
        self.assertEqual(names, [])

    def test_blank_output_is_empty(self):
        ok, names = self._with_output("")
        self.assertTrue(ok)
        self.assertEqual(names, [])

    def test_command_failure_returns_not_ok(self):
        self.app.ssh = FakeSSH((False, "connection reset"))
        self.app._connected = True
        ok, names = self.app._tmux_sessions()
        self.assertFalse(ok)
        self.assertEqual(names, [])

    def test_disconnected_returns_not_ok(self):
        self.app.ssh = None
        self.app._connected = False
        ok, names = self.app._tmux_sessions()
        self.assertFalse(ok)
        self.assertEqual(names, [])

    def test_apply_sessions_sets_combobox(self):
        self.app._apply_tmux_sessions(["dev", "prod"])
        values = self.app.tmux_session_combo.cget("values")
        self.assertEqual(list(values), ["dev", "prod"])
        self.app._apply_tmux_sessions([])
        values = self.app.tmux_session_combo.cget("values")
        self.assertEqual(list(values), [self.app_cls.TMUX_NO_SESSIONS])


if __name__ == "__main__":
    unittest.main()