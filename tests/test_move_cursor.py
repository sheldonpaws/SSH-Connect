# -*- coding: utf-8 -*-
"""Тесты move_cursor.py (конфиг + раскладка клавиш; хук не запускаем)."""

import ctypes
import json
import os
import sys
import tempfile
import threading
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


# ---------------------------------------------------------------------------
# Новое: цель PgUp/PgDn по ЗАГОЛОВКУ окна (дашборд Hermes в браузере)
# и точка, в которую уходит колесо.
# ---------------------------------------------------------------------------


def _set_point(obj, x, y):
    """Записать координаты в POINT, переданный в Win32-вызов по byref."""
    pt = ctypes.cast(obj, ctypes.POINTER(mc.wintypes.POINT)).contents
    pt.x, pt.y = x, y


def _set_rect(obj, left, top, right, bottom):
    """Записать прямоугольник в RECT, переданный в Win32-вызов по byref."""
    rc = ctypes.cast(obj, ctypes.POINTER(mc.wintypes.RECT)).contents
    rc.left, rc.top, rc.right, rc.bottom = left, top, right, bottom


class FakeUser32:
    """Подмена user32: точка колеса считается без реальных окон."""

    def __init__(self, cursor=(100, 50), client=(0, 0, 800, 600),
                 to_client=None, cursor_ok=True):
        self.cursor = cursor          # экранные координаты курсора
        self.client = client          # client rect: (left, top, right, bottom)
        self.to_client = to_client    # результат ScreenToClient, иначе = cursor
        self.cursor_ok = cursor_ok    # GetCursorPos может «не увидеть» курсор
        self.posted = []
        self.sent = []                # что приняли через SendInput

    def GetCursorPos(self, p):
        if self.cursor_ok:
            _set_point(p, *self.cursor)
        return 1 if self.cursor_ok else 0

    def ScreenToClient(self, hwnd, p):
        _set_point(p, *(self.to_client if self.to_client is not None else self.cursor))
        return 1

    def GetClientRect(self, hwnd, rc):
        _set_rect(rc, *self.client)
        return 1

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posted.append((hwnd, msg, wparam, lparam))
        return 1

    def SendInput(self, n, ptr, size):
        inp = ctypes.cast(ptr, ctypes.POINTER(mc.INPUT)).contents
        self.sent.append((n, size, int(inp.type), int(inp.mi.mouseData),
                          int(inp.mi.dwFlags)))
        return n


class TestTargetMatch(unittest.TestCase):
    """_match_target: класс KiTTY/PuTTY ИЛИ заголовок из title_matches."""

    def test_kitty_and_putty_class_match(self):
        self.assertTrue(mc._match_target("kittynext", "любой заголовок", {}))
        self.assertTrue(mc._match_target("putty", "", {}))

    def test_title_match_dashboard_when_enabled(self):
        # по умолчанию перехват выключен (только KiTTY)…
        self.assertEqual(mc.DEFAULTS["title_matches"], [])
        # …но включается настройкой title_matches
        cfg = {"title_matches": ["hermes agent"]}
        self.assertTrue(mc._match_target(
            "chrome_widgetwin_1", "Hermes Agent - Dashboard - Google Chrome", cfg))

    def test_title_match_case_insensitive(self):
        cfg = {"title_matches": ["HeRmEs AgEnT"]}
        self.assertTrue(mc._match_target(
            "explorer_window", "HERMES AGENT - dashboard", cfg))

    def test_foreign_windows_not_matched(self):
        cfg = dict(mc.DEFAULTS)
        self.assertFalse(mc._match_target(
            "chrome_widgetwin_1", "Почта — Google Chrome", cfg))
        self.assertFalse(mc._match_target("chrome_widgetwin_1", "", cfg))
        self.assertFalse(mc._match_target(
            "chrome_widgetwin_1", "Hermes Agent", {"title_matches": []}))
        self.assertFalse(mc._match_target("", "", {}))
        self.assertFalse(mc._match_target("", "", {"title_matches": None}))
        # пустая/нестроковая подстрока не должна ловить всё подряд
        self.assertFalse(mc._match_target("", "", {"title_matches": ["  "]}))


class TestTitleConfig(unittest.TestCase):
    """Новые ключи переживают слияние с существующим move_cursor.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "move_cursor.json")
        self._patches = [
            mock.patch.object(mc, "CONFIG_FILE", self.cfg_path),
            mock.patch.object(mc, "LOG_FILE",
                              os.path.join(self.tmp.name, "move_cursor.log")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_new_defaults_present(self):
        cfg = mc._load_config()
        self.assertEqual(cfg["title_matches"], [])
        self.assertTrue(cfg["use_cursor_point"])
        self.assertTrue(cfg["sendinput_for_title"])

    def test_old_config_file_keeps_working(self):
        # так выглядит конфиг, созданный ДО появления title_matches
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"notches": 4, "horizontal": True}, f)
        cfg = mc._load_config()
        self.assertEqual(cfg["notches"], 4)
        self.assertEqual(cfg["title_matches"], [])
        self.assertTrue(cfg["use_cursor_point"])

    def test_title_matches_can_be_overridden(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"title_matches": ["мой дашборд"], "use_cursor_point": False}, f)
        cfg = mc._load_config()
        self.assertEqual(cfg["title_matches"], ["мой дашборд"])
        self.assertFalse(cfg["use_cursor_point"])


class TestWheelPoint(unittest.TestCase):
    """_wheel_point: курсор внутри окна — колесо под ним, иначе — центр."""

    def _fake(self, **kw):
        fake = FakeUser32(**kw)
        patcher = mock.patch.object(mc, "user32", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_cursor_inside_window(self):
        self._fake()
        self.assertEqual(mc._wheel_point(1, dict(mc.DEFAULTS)), (100, 50))

    def test_cursor_outside_falls_back_to_center(self):
        # курсор за пределами клиентской области (другой монитор / свёрнуто)
        self._fake(to_client=(-40, -10))
        self.assertEqual(mc._wheel_point(1, dict(mc.DEFAULTS)), (400, 300))

    def test_cursor_point_disabled(self):
        self._fake()
        cfg = dict(mc.DEFAULTS, use_cursor_point=False)
        self.assertEqual(mc._wheel_point(1, cfg), (400, 300))

    def test_getcursorpos_failure_falls_back_to_center(self):
        self._fake(cursor_ok=False)
        self.assertEqual(mc._wheel_point(1, dict(mc.DEFAULTS)), (400, 300))

    def test_send_wheel_packs_lparam(self):
        fake = self._fake()
        mc._send_wheel(0x42, mc.WM_MOUSEWHEEL, mc.WHEEL_DELTA, dict(mc.DEFAULTS))
        hwnd, msg, wparam, lparam = fake.posted[-1]
        self.assertEqual(hwnd, 0x42)
        self.assertEqual(msg, mc.WM_MOUSEWHEEL)
        self.assertEqual(wparam, mc.WHEEL_DELTA << 16)
        self.assertEqual(lparam, (50 << 16) | 100)   # точка под курсором

    def test_send_wheel_packs_center_when_disabled(self):
        fake = self._fake()
        cfg = dict(mc.DEFAULTS, use_cursor_point=False)
        mc._send_wheel(0x42, mc.WM_MOUSEWHEEL, -mc.WHEEL_DELTA, cfg)
        _, _, wparam, lparam = fake.posted[-1]
        self.assertEqual(wparam, ((-mc.WHEEL_DELTA) & 0xFFFF) << 16)
        self.assertEqual(lparam, (300 << 16) | 400)  # центр 800x600


# ---------------------------------------------------------------------------
# Новое: SendInput для окон по заголовку (браузер) + очередь вместо сна в хуке.
# ---------------------------------------------------------------------------


class TestScrollPaths(unittest.TestCase):
    """_scroll: заголовочное окно → одно событие ввода, KiTTY → прежние насечки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, fname in (("CONFIG_FILE", "move_cursor.json"),
                            ("LOG_FILE", "move_cursor.log")):
            p = mock.patch.object(mc, name, os.path.join(self.tmp.name, fname))
            p.start()
            self.addCleanup(p.stop)
        self.fake = FakeUser32()
        p = mock.patch.object(mc, "user32", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_title_window_one_sendinput(self):
        s = mc.Scroller(dict(mc.DEFAULTS))
        s._scroll(0x42, "title", "up", False)
        self.assertEqual(len(self.fake.sent), 1)
        n, size, typ, data, flags = self.fake.sent[0]
        # 4 насечки = одно событие с дельтой 480, флаг вертикального колеса
        self.assertEqual((n, typ, data, flags),
                         (1, mc.INPUT_MOUSE, 4 * mc.WHEEL_DELTA,
                          mc.MOUSEEVENTF_WHEEL))
        self.assertEqual(size, ctypes.sizeof(mc.INPUT))
        self.assertEqual(self.fake.posted, [])   # PostMessage не понадобился

    def test_title_window_down_is_negative_delta(self):
        s = mc.Scroller(dict(mc.DEFAULTS))
        s._scroll(0x42, "title", "down", False)
        _n, _size, _typ, data, _flags = self.fake.sent[0]
        self.assertEqual(data, (-4 * mc.WHEEL_DELTA) & 0xFFFFFFFF)

    def test_title_window_horizontal_uses_hwheel(self):
        s = mc.Scroller(dict(mc.DEFAULTS))
        s._scroll(0x42, "title", "up", True)
        _n, _size, _typ, _data, flags = self.fake.sent[0]
        self.assertEqual(flags, mc.MOUSEEVENTF_HWHEEL)

    def test_title_window_can_fall_back_to_post(self):
        cfg = dict(mc.DEFAULTS, sendinput_for_title=False, notch_delay=0)
        mc.Scroller(cfg)._scroll(0x42, "title", "up", False)
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(len(self.fake.posted), 4)

    def test_kitty_keeps_postmessage(self):
        cfg = dict(mc.DEFAULTS, notch_delay=0)
        mc.Scroller(cfg)._scroll(0x42, "kitty", "down", False)
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(len(self.fake.posted), 4)
        hwnd, msg, wparam, _lparam = self.fake.posted[0]
        self.assertEqual(hwnd, 0x42)
        self.assertEqual(msg, mc.WM_MOUSEWHEEL)
        self.assertEqual(wparam, ((-mc.WHEEL_DELTA) & 0xFFFF) << 16)

    def test_queue_worker_scrolls_everything(self):
        """_submit не делает работу сам — её выполняет поток-исполнитель."""
        s = mc.Scroller(dict(mc.DEFAULTS))
        worker = threading.Thread(target=s._worker_loop, daemon=True)
        worker.start()
        s._submit(0x42, "title", "up", False)
        s._submit(0x42, "title", "down", False)
        s._queue.put(None)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "поток не завершился")
        self.assertEqual(len(self.fake.sent), 2)   # обе задачи отработали

    def test_queue_is_capped(self):
        """Очередь не растёт без меры: хук просто кладёт задачу."""
        s = mc.Scroller(dict(mc.DEFAULTS))
        for _ in range(40):
            s._submit(0x42, "title", "up", False)
        self.assertLessEqual(s._queue.qsize(), 30)


if __name__ == "__main__":
    unittest.main()
