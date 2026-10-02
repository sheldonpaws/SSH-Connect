# -*- coding: utf-8 -*-
"""Тесты уборки процессов при закрытии GUI (чтобы папку можно было удалить)."""

import os
import sys
import tempfile
import unittest
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401


class FakeProc:
    def __init__(self):
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=3):
        return 0


class FakeKitty:
    def __init__(self):
        self.events = []

    def stop_watcher(self):
        self.events.append("stop_watcher")

    def close_window(self):
        self.events.append("close_window")

    def terminate(self):
        self.events.append("terminate")


def _make_app():
    try:
        from src.gui import SSHApp
    except Exception as exc:  # pragma: no cover
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


class TestShutdownHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_stop_web_server_terminates_and_closes_log(self):
        proc = FakeProc()
        log = tempfile.NamedTemporaryFile("w", delete=False)
        self.app._web_server_proc = proc
        self.app._server_log_fd = log
        self.app._stop_web_server()
        self.assertTrue(proc.terminated)
        self.assertIsNone(self.app._web_server_proc)
        self.assertTrue(log.closed)
        self.assertIsNone(self.app._server_log_fd)
        log.close()
        try:
            os.remove(log.name)
        except OSError:
            pass

    def test_stop_web_server_handles_none(self):
        self.app._web_server_proc = None
        self.app._server_log_fd = None
        self.app._stop_web_server()

    def test_close_kitty_order(self):
        kitty = FakeKitty()
        self.app._kitty = kitty
        self.app._close_kitty()
        self.assertEqual(kitty.events,
                         ["stop_watcher", "close_window", "terminate"])

    def test_close_kitty_without_kitty(self):
        self.app._kitty = None
        self.app._close_kitty()


class TestOnClosing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_on_closing_cleans_everything(self):
        proc = FakeProc()
        kitty = FakeKitty()
        self.app._web_server_proc = proc
        self.app._kitty = kitty
        self.app._on_closing()
        self.assertTrue(proc.terminated)
        self.assertEqual(set(kitty.events),
                         {"stop_watcher", "close_window", "terminate"})
        self.assertIsNone(self.app._web_server_proc)


if __name__ == "__main__":
    unittest.main()