# -*- coding: utf-8 -*-
"""Тесты окон KiTTY: основное окно 1 + фиксированные слоты 2..5.

Селектор «Окно:» содержит ровно 4 дополнительных окна и никогда не растёт.
Новая сессия tmux открывается в первом свободном слоте.
"""

import os
import sys
import time
import unittest
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

_GUI = None
try:
    import src.gui as _gui
    _GUI = _gui
except Exception:  # pragma: no cover
    pass


class FakeKitty:
    """Подмена KittyController: ничего не запускает, только пишет события."""

    created = []
    foreground = None  # hwnd, который подставит ctypes.GetForegroundWindow
    start_result = (True, "OK")
    ready = True  # окно "готово" — иначе команды идут в очередь

    def __init__(self, on_log=None):
        self.name = None
        self.slot = 1
        self.in_tmux = False
        self.events = []
        self.log_file_arg = None
        self.sent = []
        self.queued = []
        self.hwnd = None
        self.process = None
        FakeKitty.created.append(self)

    def is_ready(self):
        return FakeKitty.ready

    def queue_command(self, text, restore=False, timeout=None):
        self.queued.append(text)

    def start_kitty_with_logging(self, hostname, port, username, password,
                                 key_file=None, hostkey=None, log_file=None):
        self.log_file_arg = log_file
        return FakeKitty.start_result

    def find_kitty_window(self, pid=None):
        return self.hwnd

    def restore_window(self):
        self.events.append("restore")

    def send_command(self, text):
        self.sent.append(text)
        return True, ""

    def stop_watcher(self):
        self.events.append("stop")

    def close_window(self):
        self.events.append("close")

    def terminate(self):
        self.events.append("terminate")


class _Fg:
    """Подмена ctypes.windll.user32 для GetForegroundWindow."""

    def __init__(self, holder):
        self._holder = holder
        self.GetForegroundWindow = self._get

    def _get(self):
        return self._holder.foreground


class _Alive:
    """Процесс kitty.exe, который ещё жив."""

    def poll(self):
        return None


def _make_app():
    if _GUI is None:  # pragma: no cover
        raise unittest.SkipTest("не удалось импортировать src.gui")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover
        raise unittest.SkipTest("нет дисплея: %s" % exc)
    root.withdraw()
    try:
        return root, _GUI.SSHApp(root)
    except Exception:
        root.destroy()
        raise


class TestTmuxAttachCommand(unittest.TestCase):
    """Команда монтирования сессии зависит от состояния окна."""

    def test_fresh_window_gets_attach_only(self):
        cmd = _GUI.build_tmux_attach_command("work", inside_tmux=False)
        self.assertTrue(cmd.startswith("tmux attach -t work"))
        self.assertNotIn("switch-client", cmd)
        self.assertIn("Нет сессии tmux", cmd)

    def test_inside_tmux_gets_switch_with_attach_fallback(self):
        cmd = _GUI.build_tmux_attach_command("work", inside_tmux=True)
        self.assertTrue(cmd.startswith('[ -n "$TMUX" ] && tmux switch-client'))
        # ручной detach (Ctrl-b d) делает наш флаг устаревшим — нужен откат
        self.assertIn("|| tmux attach -t work", cmd)

    def test_unknown_state_detects_tmux_env(self):
        for inside in (True, None):
            cmd = _GUI.build_tmux_attach_command("work",
                                                 inside_tmux=inside)
            self.assertIn('[ -n "$TMUX" ]', cmd)
            self.assertIn("tmux switch-client", cmd)
            self.assertIn("tmux attach", cmd)

    def test_session_name_is_quoted(self):
        cmd = _GUI.build_tmux_attach_command("my session", inside_tmux=False)
        self.assertIn("'my session'", cmd)

    def test_fresh_window_chain_has_no_switch_client(self):
        # именно тот баг: в свежем окне уходил switch-client, и цепочка
        # доходила до эха «Нет сессии tmux» вместо подключения
        cmd = _GUI.build_tmux_attach_command("work", inside_tmux=False)
        self.assertNotIn("switch-client", cmd)
        self.assertEqual(cmd, "tmux attach -t work || echo 'Нет сессии tmux'")


class TestKittyReadiness(unittest.TestCase):
    """Окно нельзя считать готовым, пока не завершился вход по SSH."""

    def _kit(self):
        return _GUI.KittyController()

    def test_not_ready_right_after_start(self):
        kit = self._kit()
        kit._check_ready()
        self.assertFalse(kit.is_ready())

    def test_key_auth_prompt_makes_ready(self):
        kit = self._kit()
        kit._password_queued = ""  # вход по ключу, пароля нет
        kit._tail = "Last login: Thu Oct 1\r\nroot@host:~# "
        kit._check_ready()
        self.assertTrue(kit.is_ready())

    def test_password_sent_and_output_quiet_makes_ready(self):
        import time as _t
        now = _t.time()
        kit = self._kit()
        kit._password_queued = "secret"
        kit._password_sent = True
        kit._password_at = now - 2.0
        kit._last_log_change = now - 1.0
        kit._started_at = now - 3.0
        kit._check_ready()
        self.assertTrue(kit.is_ready())

    def test_password_sent_but_still_printing_not_ready(self):
        import time as _t
        now = _t.time()
        kit = self._kit()
        kit._password_queued = "secret"
        kit._password_sent = True
        kit._password_at = now - 2.0
        kit._last_log_change = now  # баннер ещё печатается
        kit._started_at = now - 3.0
        kit._check_ready()
        self.assertFalse(kit.is_ready())

    def test_timeout_marks_ready_anyway(self):
        import time as _t
        kit = self._kit()
        kit._started_at = _t.time() - kit.READY_TIMEOUT - 1
        kit._check_ready()
        self.assertTrue(kit.is_ready())

    def test_queue_command_waits_for_ready(self):
        import time as _t
        kit = self._kit()
        kit.sent = []
        kit.send_command = lambda text: (kit.sent.append(text), (True, ""))[1]
        kit.queue_command("tmux new -s work", timeout=5.0)
        time.sleep(0.2)
        self.assertEqual(kit.sent, [])  # окно ещё не готово — ждём
        kit._ready.set()
        deadline = _t.time() + 3.0
        while not kit.sent and _t.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(kit.sent, ["tmux new -s work"])


class TestKittyWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        FakeKitty.created = []
        FakeKitty.foreground = None
        FakeKitty.start_result = (True, "OK")
        FakeKitty.ready = True
        # подмены отдельных тестов не должны утекать в следующие
        for attr in ("_send_to_kitty", "_tmux_selected_name",
                     "_dlg_askstring", "_dlg_confirm"):
            self.app.__dict__.pop(attr, None)
        self.app._kitty_windows = {}
        self.app._kitty = None
        self.app._connected = True
        self.app._last_creds = {
            "hostname": "example.com", "port": "22", "username": "u",
            "password": "p", "key_file": None,
        }
        self._orig_kitty = _GUI.KittyController
        _GUI.KittyController = FakeKitty
        self._orig_fg = _GUI.ctypes.windll.user32
        _GUI.ctypes.windll.user32 = _Fg(FakeKitty)

    def tearDown(self):
        _GUI.KittyController = self._orig_kitty
        _GUI.ctypes.windll.user32 = self._orig_fg

    # --- фиксированный селектор ----------------------------------------
    def test_selector_has_exactly_four_extra_windows(self):
        values = list(self.app.kitty_window_combo.cget("values"))
        self.assertEqual(values, ["Окно 2", "Окно 3", "Окно 4", "Окно 5"])
        self.assertEqual(self.app.EXTRA_WINDOW_SLOTS, (2, 3, 4, 5))

    def test_selector_does_not_grow_after_connects(self):
        before = list(self.app.kitty_window_combo.cget("values"))
        for _ in range(5):
            self.app._open_kitty()
        self.assertEqual(list(self.app.kitty_window_combo.cget("values")),
                         before)

    def test_selector_ignores_selection_when_disconnected(self):
        self.app._connected = False
        self.app.kitty_window_combo.set("Окно 3")
        self.app._on_kitty_window_select()
        self.assertEqual(FakeKitty.created, [])

    def test_no_new_window_button(self):
        texts = [w.cget("text") for w in self.app.kitty_window_combo.master
                 .winfo_children() if "text" in w.keys()]
        self.assertNotIn("Новое окно", texts)
        self.assertIn("Окно:", texts)

    # --- выбор окна ------------------------------------------------------
    def test_select_opens_window(self):
        self.app.kitty_window_combo.set("Окно 3")
        self.app._on_kitty_window_select()
        self.assertEqual(len(FakeKitty.created), 1)
        kit = FakeKitty.created[0]
        self.assertEqual(kit.slot, 3)
        self.assertEqual(kit.name, "Окно 3")
        self.assertIs(self.app._kitty_windows[3], kit)
        self.assertIs(self.app._kitty, kit)

    def test_select_existing_window_does_not_relaunch(self):
        self.app._use_kitty_slot(4)
        self.app.kitty_window_combo.set("Окно 2")
        self.app._on_kitty_window_select()
        self.app.kitty_window_combo.set("Окно 4")
        self.app._on_kitty_window_select()
        self.assertEqual(len(FakeKitty.created), 2)  # 4 и 2, без дублей
        self.assertIs(self.app._kitty, self.app._kitty_windows[4])

    def test_failed_start_leaves_slot_free(self):
        FakeKitty.start_result = (False, "нет kitty.exe")
        self.assertIsNone(self.app._launch_kitty_window(2))
        self.assertEqual(self.app._kitty_windows, {})
        self.assertEqual(self.app._free_kitty_slot(), 2)

    # --- логи окон ------------------------------------------------------
    def test_log_files_are_per_slot(self):
        for slot in (1, 2, 5):
            self.app._launch_kitty_window(slot)
        first, second, fifth = FakeKitty.created
        self.assertEqual(first.log_file_arg, _GUI.KITTY_LOG_FILE)
        self.assertTrue(os.path.basename(second.log_file_arg)
                        == "kitty_session_2.log")
        self.assertTrue(os.path.basename(fifth.log_file_arg)
                        == "kitty_session_5.log")

    # --- свободные окна / новая сессия tmux ------------------------------
    def test_free_slot_order(self):
        self.assertEqual(self.app._free_kitty_slot(), 2)
        self.app._use_kitty_slot(2)
        self.assertEqual(self.app._free_kitty_slot(), 3)
        self.app._use_kitty_slot(3)
        self.assertEqual(self.app._free_kitty_slot(), 4)

    def test_no_free_slot_returns_none(self):
        for slot in self.app.EXTRA_WINDOW_SLOTS:
            self.app._use_kitty_slot(slot)
        self.assertIsNone(self.app._free_kitty_slot())

    def test_closed_window_frees_its_slot(self):
        for slot in self.app.EXTRA_WINDOW_SLOTS:
            kit = self.app._use_kitty_slot(slot)
            kit.process = _Alive()

        class Dead:
            def poll(self):
                return 0

            def stop_watcher(self):
                pass

        self.app._kitty_windows[2].process = Dead()
        self.app._prune_kitty_windows()
        self.assertEqual(sorted(self.app._kitty_windows), [3, 4, 5])
        self.assertEqual(self.app._free_kitty_slot(), 2)

    def test_tmux_new_opens_in_free_window(self):
        self.app._use_kitty_slot(1)
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        self.assertEqual(sorted(self.app._kitty_windows), [1, 2])
        w2 = self.app._kitty_windows[2]
        self.assertEqual(w2.sent, ["tmux new -s work"])

    def test_tmux_new_skips_occupied_window(self):
        self.app._use_kitty_slot(2)
        self.app._use_kitty_slot(3)
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        self.assertEqual(sorted(self.app._kitty_windows), [2, 3, 4])
        self.assertEqual(self.app._kitty_windows[4].sent,
                         ["tmux new -s work"])

    def test_tmux_new_reuses_slot_after_close(self):
        for slot in self.app.EXTRA_WINDOW_SLOTS:
            self.app._use_kitty_slot(slot)

        class Dead:
            def poll(self):
                return 0

            def stop_watcher(self):
                pass

        self.app._kitty_windows[3].process = Dead()
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        self.assertEqual(sorted(self.app._kitty_windows), [2, 3, 4, 5])
        self.assertEqual(self.app._kitty_windows[3].sent,
                         ["tmux new -s work"])

    def test_tmux_new_without_free_windows_uses_active(self):
        self.app._use_kitty_slot(1)
        for slot in self.app.EXTRA_WINDOW_SLOTS:
            self.app._use_kitty_slot(slot)
        for kit in FakeKitty.created:
            kit.hwnd = None
        FakeKitty.foreground = 999
        self.app._kitty = self.app._kitty_windows[1]
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        self.assertEqual(self.app._kitty_windows[1].sent,
                         ["tmux new -s work"])
        self.assertEqual(len(self.app._kitty_windows), 5)

    # --- маршрутизация команд -------------------------------------------
    def test_focused_window_receives_command(self):
        self.app._use_kitty_slot(1)
        self.app._use_kitty_slot(3)
        w1, w3 = self.app._kitty_windows[1], self.app._kitty_windows[3]
        w1.hwnd, w3.hwnd = 111, 333
        FakeKitty.foreground = 333
        self.app._send_to_kitty("echo hi", restore=False)
        self.assertEqual(w3.sent, ["echo hi"])
        self.assertEqual(w1.sent, [])

    def test_selected_window_is_target_after_focus_leaves(self):
        # выбрали «Окно 5», затем кликнули комбобокс сессий tmux в главном
        # окне — команда всё равно должна уйти в выбранное окно
        self.app._use_kitty_slot(1)
        self.app._use_kitty_slot(5)
        FakeKitty.foreground = 999  # фокус на главном окне приложения
        self.app._send_to_kitty("tmux ls", restore=False)
        self.assertEqual(self.app._kitty_windows[5].sent, ["tmux ls"])
        self.assertEqual(self.app._kitty_windows[1].sent, [])

    def test_falls_back_to_window_one_when_nothing_selected(self):
        self.app._use_kitty_slot(1)
        self.app._kitty = None
        FakeKitty.foreground = 999
        self.app._send_to_kitty("tmux ls", restore=False)
        self.assertEqual(self.app._kitty_windows[1].sent, ["tmux ls"])

    # --- маршрутизация tmux attach по состоянию окна ----------------------
    def test_attach_into_fresh_extra_window_uses_attach(self):
        self.app._use_kitty_slot(1)
        self.app._use_kitty_slot(3)
        w1, w3 = self.app._kitty_windows[1], self.app._kitty_windows[3]
        w1.hwnd, w3.hwnd = 111, 333
        FakeKitty.foreground = 333  # работаем в окне 3
        self.app._tmux_selected_name = lambda: "work"
        self.app._tmux_attach()
        self.assertEqual(w3.sent,
                         ["tmux attach -t work || echo 'Нет сессии tmux'"])
        self.assertEqual(w1.sent, [])
        self.assertTrue(w3.in_tmux)

    def test_attach_inside_tmux_uses_switch(self):
        self.app._use_kitty_slot(2)
        w2 = self.app._kitty_windows[2]
        w2.in_tmux = True
        self.app._tmux_selected_name = lambda: "work"
        self.app._tmux_attach()
        sent = w2.sent[0]
        self.assertIn("tmux switch-client -t work", sent)
        self.assertIn("tmux attach -t work", sent)  # откат после detach

    def test_attach_marks_window_inside_tmux(self):
        self.app._use_kitty_slot(4)
        w4 = self.app._kitty_windows[4]
        self.assertFalse(w4.in_tmux)  # свежее окно — вне tmux
        self.app._tmux_selected_name = lambda: "work"
        self.app._tmux_attach()
        self.assertTrue(w4.in_tmux)

    def test_kill_detach_marks_window_outside_tmux(self):
        self.app._use_kitty_slot(1)
        w1 = self.app._kitty_windows[1]
        w1.in_tmux = True
        self.app._tmux_selected_name = lambda: "work"
        self.app._dlg_confirm = lambda *a, **kw: True
        self.app._tmux_kill()
        self.assertIn("tmux detach", w1.sent)
        self.assertFalse(w1.in_tmux)

    def test_tmux_new_marks_window_inside_tmux(self):
        self.app._use_kitty_slot(2)  # занят — новая сессия уйдёт в слот 3
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        self.assertEqual(sorted(self.app._kitty_windows), [2, 3])
        self.assertEqual(self.app._kitty_windows[3].sent,
                         ["tmux new -s work"])
        self.assertTrue(self.app._kitty_windows[3].in_tmux)

    # --- команда для ещё не вошедшего окна встаёт в очередь --------------
    def test_new_session_queued_while_window_still_logging_in(self):
        FakeKitty.ready = False  # окно ещё входит на сервер
        self.app._dlg_askstring = lambda *a, **kw: "work"
        self.app._tmux_new()
        w2 = self.app._kitty_windows[2]
        self.assertEqual(w2.sent, [])  # раньше команда терялась
        self.assertEqual(w2.queued, ["tmux new -s work"])

    def test_attach_queued_while_window_still_logging_in(self):
        FakeKitty.ready = False
        self.app._use_kitty_slot(3)
        w3 = self.app._kitty_windows[3]
        self.app._tmux_selected_name = lambda: "work"
        self.app._tmux_attach()
        self.assertEqual(w3.sent, [])
        self.assertEqual(w3.queued,
                         ["tmux attach -t work || echo 'Нет сессии tmux'"])

    def test_ready_window_gets_command_directly(self):
        FakeKitty.ready = True
        self.app._use_kitty_slot(2)
        w2 = self.app._kitty_windows[2]
        self.app._tmux_selected_name = lambda: "work"
        self.app._tmux_attach()
        self.assertEqual(w2.sent, [
            "tmux attach -t work || echo 'Нет сессии tmux'"])
        self.assertEqual(w2.queued, [])

    def test_close_kitty_closes_all_windows(self):
        for slot in (1, 2, 3):
            self.app._use_kitty_slot(slot)
        for fake in FakeKitty.created:
            fake.events = []  # сбросить restore от _use_kitty_slot
        self.app._close_kitty()
        for fake in FakeKitty.created:
            self.assertEqual(fake.events, ["stop", "close", "terminate"])
        self.assertEqual(self.app._kitty_windows, {})
        self.assertIsNone(self.app._kitty)

    def test_connect_replaces_previous_windows(self):
        for slot in (1, 2, 3):
            self.app._use_kitty_slot(slot)
        self.app._open_kitty()
        self.assertEqual(sorted(self.app._kitty_windows), [1])
        self.assertEqual(self.app._kitty_windows[1].name, "Окно 1")


if __name__ == "__main__":
    unittest.main()