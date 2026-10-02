# -*- coding: utf-8 -*-
"""Тесты блока установки docker-браузера (первое нажатие на хосте)."""

import os
import sys
import unittest
import tkinter as tk
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

_GUI_EXC = None
try:
    from src.gui import (DOCKER_BROWSER_READY, DOCKER_CHECK_COMMAND,
                         DOCKER_SETUP_BLOCK, SSHApp, docker_setup_done,
                         mark_docker_setup_done, parse_docker_installed)
except Exception as exc:  # pragma: no cover
    _GUI_EXC = exc


class _ImmediateRoot:
    """Стаб Tk-рута: `after` выполняет колбэк сразу (root.after из потока
    срабатывает только под mainloop, а тесты крутят update())."""

    def after(self, delay, callback):
        callback()
        return "after-id"


class _SyncThread:
    """Стаб threading.Thread: запускает target синхронно."""

    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class TestDockerInstallCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _GUI_EXC is not None:  # pragma: no cover
            raise unittest.SkipTest("не удалось импортировать src.gui: %s" % _GUI_EXC)

    def test_check_probes_docker_and_chrome(self):
        self.assertIn("command -v docker", DOCKER_CHECK_COMMAND)
        self.assertIn("grep -qx chrome", DOCKER_CHECK_COMMAND)
        self.assertIn(DOCKER_BROWSER_READY, DOCKER_CHECK_COMMAND)

    def test_parse_ready(self):
        self.assertTrue(parse_docker_installed(DOCKER_BROWSER_READY + "\n"))
        self.assertTrue(parse_docker_installed("x\n" + DOCKER_BROWSER_READY))

    def test_parse_missing_or_empty(self):
        self.assertFalse(parse_docker_installed("DOCKER_BROWSER_MISSING"))
        self.assertFalse(parse_docker_installed(""))
        self.assertFalse(parse_docker_installed(None))


class TestDockerSetupBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _GUI_EXC is not None:  # pragma: no cover
            raise unittest.SkipTest("не удалось импортировать src.gui: %s" % _GUI_EXC)

    def test_block_not_empty(self):
        lines = [ln for ln in DOCKER_SETUP_BLOCK.splitlines() if ln.strip()]
        self.assertGreater(len(lines), 5)

    def test_block_has_docker_install_and_run(self):
        self.assertIn("docker-ce", DOCKER_SETUP_BLOCK)
        self.assertIn("--name=chrome", DOCKER_SETUP_BLOCK)
        self.assertIn("-p 3001:3001", DOCKER_SETUP_BLOCK)

    def test_marker_absent_means_not_done(self):
        self.assertFalse(docker_setup_done(
            "never-seen-host-{}".format(id(self))))

    def test_marker_roundtrip_per_host(self):
        host = "host-roundtrip-{}".format(id(self))
        other = "host-other-{}".format(id(self))
        mark_docker_setup_done(host)
        self.assertTrue(docker_setup_done(host))
        self.assertFalse(docker_setup_done(other))


class TestTypeDockerSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _GUI_EXC is not None:  # pragma: no cover
            raise unittest.SkipTest("не удалось импортировать src.gui: %s" % _GUI_EXC)
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover
            raise unittest.SkipTest("нет дисплея: %s" % exc)
        root.withdraw()
        cls.root = root
        try:
            cls.app = SSHApp(root)
        except Exception:
            root.destroy()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_types_each_line_and_returns_true(self):
        sent = []
        calls = []

        class FakeKitty:
            def restore_window(self):
                calls.append("restore")

            def send_command(self, command):
                sent.append(command)
                return True, "OK"

        self.app._kitty = FakeKitty()
        self.assertTrue(self.app._type_docker_setup())
        expected = [ln.strip() for ln in DOCKER_SETUP_BLOCK.splitlines() if ln.strip()]
        self.assertEqual(sent, expected)
        self.assertTrue(calls)
        self.assertGreater(len(sent), 5)

    def test_stops_on_send_failure(self):
        sent = []

        class FakeKitty:
            def restore_window(self):
                pass

            def send_command(self, command):
                sent.append(command)
                return False, "Окно KiTTY недоступно"

        self.app._kitty = FakeKitty()
        self.assertFalse(self.app._type_docker_setup())
        self.assertEqual(len(sent), 1)

    def test_no_kitty_returns_false(self):
        self.app._kitty = None
        self.assertFalse(self.app._type_docker_setup())

    # --- проверка установки на сервере (не ставить в каждой версии) --------
    def test_server_installed_skips_setup(self):
        shown, typed = [], []
        with mock.patch.object(self.app, "_show_docker_browser", shown.append), \
                mock.patch.object(self.app, "_type_docker_setup",
                                  lambda: (typed.append(True), True)[1]):
            self.app._apply_docker_check("host-installed", True)
        self.assertEqual(shown, ["host-installed"])
        self.assertEqual(typed, [])

    def test_server_missing_types_setup(self):
        shown, typed = [], []
        with mock.patch.object(self.app, "_show_docker_browser", shown.append), \
                mock.patch.object(self.app, "_type_docker_setup",
                                  lambda: (typed.append(True), True)[1]):
            self.app._apply_docker_check(
                "host-missing-{}".format(id(self)), False)
        self.assertEqual(typed, [True])
        self.assertEqual(shown, [])

    def test_local_marker_opens_without_probing_server(self):
        host = "marker-host-{}".format(id(self))
        mark_docker_setup_done(host)
        shown, probed = [], []
        self.app._connected = True
        self.app._last_creds = {"hostname": host}
        with mock.patch.object(self.app, "_show_docker_browser", shown.append), \
                mock.patch.object(self.app, "_execute_server_command",
                                  lambda c: (probed.append(c), (True, ""))[1]):
            self.app._open_docker_browser()
        self.assertEqual(shown, [host])
        self.assertEqual(probed, [])

    def test_open_probes_server_and_skips_setup_when_installed(self):
        host = "fresh-host-{}".format(id(self))
        shown, typed = [], []
        self.app._connected = True
        self.app._last_creds = {"hostname": host}
        with mock.patch.object(self.app, "_show_docker_browser", shown.append), \
                mock.patch.object(self.app, "_type_docker_setup",
                                  lambda: (typed.append(True), True)[1]), \
                mock.patch.object(self.app, "_execute_server_command",
                                  lambda c: (True, DOCKER_BROWSER_READY)), \
                mock.patch.object(self.app, "root", _ImmediateRoot()), \
                mock.patch("threading.Thread", _SyncThread):
            self.app._open_docker_browser()
        self.assertEqual(shown, [host])
        self.assertEqual(typed, [])

    def test_open_probes_server_and_types_setup_when_missing(self):
        host = "empty-host-{}".format(id(self))
        shown, typed = [], []
        self.app._connected = True
        self.app._last_creds = {"hostname": host}
        with mock.patch.object(self.app, "_show_docker_browser", shown.append), \
                mock.patch.object(self.app, "_type_docker_setup",
                                  lambda: (typed.append(True), True)[1]), \
                mock.patch.object(self.app, "_execute_server_command",
                                  lambda c: (True, "DOCKER_BROWSER_MISSING")), \
                mock.patch.object(self.app, "root", _ImmediateRoot()), \
                mock.patch("threading.Thread", _SyncThread):
            self.app._open_docker_browser()
        self.assertEqual(typed, [True])
        self.assertEqual(shown, [])


if __name__ == "__main__":
    unittest.main()