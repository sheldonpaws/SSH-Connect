# -*- coding: utf-8 -*-
"""Тесты базовой механики UI: очередь команд KiTTY и диспетчеризация в
главный поток Tk.

`_dispatch_ui` — единственный легальный способ звать Tk из фонового потока:
сначала root.after, а если тот кидает RuntimeError (когда главный поток не в
mainloop), — очередь self._ui_queue, которую выгребает таймер главного потока.
"""

import collections
import os
import sys
import threading
import time
import unittest
import tkinter as tk
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

_GUI_EXC = None
try:
    from src.gui import KittyController, SSHApp
except Exception as exc:  # pragma: no cover
    _GUI_EXC = exc


class _ImmediateRoot:
    """Стаб Tk-рута: `after` выполняет колбэк сразу (root.after из потока
    срабатывает только под mainloop, а тесты крутят update())."""

    def after(self, delay, callback):
        callback()
        return "after-id"


class _BrokenRoot:
    """Стаб, воспроизводящий реальный гвоздь: root.after из фонового потока
    кидает RuntimeError('main thread is not in main loop'), когда главный
    поток не в Tcl-цикле."""

    def after(self, delay, callback=None):
        raise RuntimeError("main thread is not in main loop")


class _SyncThread:
    """Стаб threading.Thread: запускает target синхронно."""

    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class TestCommandQueue(unittest.TestCase):
    """Очередь отложенных команд в KittyController: блок из нескольких
    команд обязан уходить в терминал строго по одной и по порядку, иначе
    символы перемешиваются (регресс tmux-окон и команды подряд)."""

    @classmethod
    def setUpClass(cls):
        if _GUI_EXC is not None:  # pragma: no cover
            raise unittest.SkipTest("не удалось импортировать src.gui: %s" % _GUI_EXC)

    def setUp(self):
        self.events = []
        self.kit = object.__new__(KittyController)
        self.kit._ready = threading.Event()
        self.kit._q_lock = threading.Lock()
        self.kit._q_items = collections.deque()
        self.kit._q_running = False
        self.kit.restore_window = lambda: self.events.append("restore")

        def send(cmd):
            self.events.append("send:" + cmd)
            # делаем вид, что символы печатаются не мгновенно
            time.sleep(0.005)
            return True, "OK"

        self.kit.send_command = send

    def _wait_drained(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.kit._q_lock:
                if not self.kit._q_items and not self.kit._q_running:
                    return True
            time.sleep(0.02)
        return False

    def test_commands_are_sent_sequentially_in_order(self):
        lines = ["echo a", "echo b", "echo c"]
        self.kit._ready.set()
        with mock.patch.object(KittyController, "QUEUE_GAP", 0.01):
            for line in lines:
                self.kit.queue_command(line, restore=True)
        self.assertTrue(self._wait_drained())
        sends = [e[5:] for e in self.events if e.startswith("send:")]
        self.assertEqual(sends, lines)
        self.assertEqual([e for e in self.events if e == "restore"],
                         ["restore"] * len(lines))

    def test_waits_for_readiness_before_sending(self):
        """Пока окно не готово — ни одна команда не уходит."""
        with mock.patch.object(KittyController, "QUEUE_GAP", 0.01):
            for line in ("echo a", "echo b"):
                self.kit.queue_command(line)
            time.sleep(0.2)
            self.assertEqual(self.events, [])
            self.kit._ready.set()
            self.assertTrue(self._wait_drained())
        self.assertEqual([e[5:] for e in self.events], ["echo a", "echo b"])

    def test_single_worker_drains_everything(self):
        """Все команды обслуживает ОДИН поток, а не по потоку на команду."""
        started = []
        real_start = threading.Thread.start

        def spy(self, *a, **kw):
            started.append(1)
            return real_start(self, *a, **kw)

        self.kit._ready.set()
        with mock.patch.object(threading.Thread, "start", spy), \
                mock.patch.object(KittyController, "QUEUE_GAP", 0.01):
            for i in range(5):
                self.kit.queue_command("echo %d" % i)
            self.assertTrue(self._wait_drained())
        self.assertEqual(len(started), 1)


class TestUiDispatchFromThreads(unittest.TestCase):
    """Фоновый поток не может трогать Tk напрямую: root.after из потока кидает
    RuntimeError('main thread is not in main loop'), отчего результат фоновой
    проверки терялся. Проверяем запасной путь через self._ui_queue."""

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

    def _empty_queue(self):
        import queue
        while True:
            try:
                self.app._ui_queue.get_nowait()
            except queue.Empty:
                return

    def test_dispatch_uses_root_after_when_it_works(self):
        self._empty_queue()
        calls = []
        with mock.patch.object(self.app, "root", _ImmediateRoot()):
            self.assertTrue(self.app._dispatch_ui(lambda: calls.append(1)))
        self.assertEqual(calls, [1])

    def test_dispatch_falls_back_to_queue_when_after_raises(self):
        self._empty_queue()
        calls = []
        with mock.patch.object(self.app, "root", _BrokenRoot()):
            self.assertTrue(self.app._dispatch_ui(lambda: calls.append(1)))
        self.assertEqual(calls, [])  # сразу не выполнилось
        self.app._poll_ui_queue()    # таймер главного потока вытаскивает
        self.assertEqual(calls, [1])

    def test_execute_server_command_propagates_timeout(self):
        calls = []

        ssh = mock.Mock()
        ssh.is_connected = True
        ssh.execute_command = (
            lambda cmd, timeout=None: (calls.append((cmd, timeout)),
                                       (True, "ok"))[1])
        with mock.patch.object(self.app, "ssh", ssh):
            ok, out = self.app._execute_server_command("echo hi", timeout=7)
        self.assertTrue(ok)
        self.assertEqual(out, "ok")
        self.assertEqual(calls, [("echo hi", 7)])

    def test_poll_reports_exception_without_dying(self):
        self._empty_queue()
        logged = []

        def boom():
            raise ValueError("boom")

        with mock.patch.object(self.app, "_log",
                               lambda msg, tag="output": logged.append(msg)), \
                mock.patch.object(self.app, "root", _BrokenRoot()):
            self.app._ui_queue.put(boom)
            self.app._poll_ui_queue()
        self.assertTrue(any("boom" in m for m in logged), logged)


if __name__ == "__main__":
    unittest.main()