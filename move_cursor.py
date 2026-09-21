# -*- coding: utf-8 -*-
"""
move_cursor — PgUp/PgDn → WM_MOUSEWHEEL для KiTTY + tmux.

Использует низкоуровневый перехват клавиатуры (WH_KEYBOARD_LL) через
ctypes: нажатия PgUp/PgDn глотаются, пока активное окно из KiTTY/PuTTY,
и вместо них отправляются синтетические события колеса мыши
(WM_MOUSEWHEEL / WM_MOUSEHWHEEL).  tmux с `mouse on` ловит колесо и
листает свою историю на `notches` насечек за нажатие.

Глотание ключа важно: иначе сама клавиша PgUp доходит до KiTTY/tmux,
и они листают встроенной страницей, игнорируя настройку насечек.

Настройки — в move_cursor.json рядом со скриптом (создаётся сам).
Журнал действий — move_cursor.log.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import sys
import time

# ── Win32 constants ───────────────────────────────────────────────
WM_MOUSEWHEEL   = 0x020A
WM_MOUSEHWHEEL  = 0x020E
WHEEL_DELTA     = 120

WH_KEYBOARD_LL  = 13
HC_ACTION       = 0
WM_KEYDOWN      = 0x0100
WM_SYSKEYDOWN   = 0x0104
WM_KEYUP        = 0x0101
WM_SYSKEYUP     = 0x0105

VK_PRIOR        = 0x21   # PgUp
VK_NEXT         = 0x22   # PgDn
VK_SHIFT        = 0x10
VK_F12          = 0x7B

# 64-битные типы Windows (в ctypes.wintypes их нет)
LRESULT = ctypes.c_ssize_t
WPARAM  = ctypes.c_size_t
LPARAM  = ctypes.c_ssize_t
HHOOK   = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


# Типизация win32-вызовов (важно для 64-бит: иначе хэндлы/WPARAM/LPARAM
# обрезаются до 32 бит через дефолтный c_int)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, HINSTANCE, wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM, LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "move_cursor.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "move_cursor.log")

DEFAULTS = {
    "pause_key": "f12",
    "up_key": "page_up",
    "down_key": "page_down",
    "notches": 4,          # насечек колеса на одно нажатие (1 ≈ 5 строк tmux)
    "notch_delay": 0.05,   # сек между насечками, чтобы KiTTY не склеивал
    "horizontal": True,    # Shift + PgUp/PgDn — горизонтальный скролл
    "log": True,
}

# VK-коды клавиш
VK_BY_NAME = {
    "page_up": VK_PRIOR,
    "page_down": VK_NEXT,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f12": VK_F12,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "scroll_lock": 0x91,
    "pause": 0x13,
    "home": 0x24,
    "end": 0x23,
}


def _log(msg: str, cfg=None):
    if cfg is not None and not cfg.get("log", True):
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def _load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return cfg


def _vk(name: str):
    name = (name or "").lower().strip()
    if name in ("none", ""):
        return 0
    return VK_BY_NAME.get(name, 0)


def _find_kitty() -> int:
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindow(hwnd):
        return 0
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value.lower()
    if "kitty" in cls or "putty" in cls:
        return hwnd
    return 0


def _send_wheel(hwnd: int, msg: int, delta: int):
    """PostMessage WM_MOUSEWHEEL в центр окна (работает и свёрнутым)."""
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    cx = ((rc.left + rc.right) // 2) & 0xFFFF
    cy = ((rc.top + rc.bottom) // 2) & 0xFFFF
    wparam = (delta & 0xFFFF) << 16
    lparam = (cy << 16) | cx
    user32.PostMessageW(hwnd, msg, wparam, lparam)


class Scroller:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._paused = False
        self._hook_proc = None     # держим ссылку, иначе GC убьёт прок
        self._hook = None
        self._up_vk = _vk(cfg["up_key"])
        self._dn_vk = _vk(cfg["down_key"])
        self._pause_vk = _vk(cfg["pause_key"])

    def _shift_active(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)

    def _do(self, direction: str):
        hwnd = _find_kitty()
        if not hwnd:
            return
        horizontal = self._shift_active() and self._cfg.get("horizontal", True)
        msg = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
        sign = 1 if direction == "up" else -1
        notches = max(1, int(self._cfg.get("notches", 1)))
        delay = max(0.0, float(self._cfg.get("notch_delay", 0.05)))
        for i in range(notches):
            _send_wheel(hwnd, msg, sign * WHEEL_DELTA)
            if i < notches - 1:
                time.sleep(delay)
        _log(f"{'H' if horizontal else 'V'} {direction} x{notches} "
             f"hwnd={hwnd}", self._cfg)
        return True

    def _hook_handler(self, n_code, w_param, l_param):
        if n_code == HC_ACTION:
            msg = w_param & 0xFFFF
            is_down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = msg in (WM_KEYUP, WM_SYSKEYUP)
            data = ctypes.cast(
                l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = data.vkCode

            if is_down and self._pause_vk and vk == self._pause_vk:
                self._paused = not self._paused
                _log(f"пауза {'ВКЛ' if self._paused else 'ВЫКЛ'}", self._cfg)

            if self._paused:
                return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

            if is_down and (vk == self._up_vk or vk == self._dn_vk):
                if _find_kitty():
                    direction = "up" if vk == self._up_vk else "down"
                    self._do(direction)
                    # Глотаем клавишу — пусть KiTTY/tmux не листают сами
                    return 1

            if is_up and (vk == self._up_vk or vk == self._dn_vk):
                if _find_kitty():
                    return 1

        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    def start(self):
        cfg = self._cfg
        _log(f"start: pause={cfg['pause_key']} up={cfg['up_key']} "
             f"down={cfg['down_key']} notches={cfg.get('notches')} "
             f"delay={cfg.get('notch_delay')} horizontal={cfg.get('horizontal')}",
             cfg)
        self._hook_proc = HOOKPROC(self._hook_handler)
        # Для WH_KEYBOARD_LL модуль можно передать NULL (хук локальный,
        # callback живёт в нашем процессе рядом с message loop)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not self._hook:
            _log(f"SetWindowsHookEx failed: {ctypes.get_last_error()}", cfg)
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


def main():
    cfg = _load_config()
    Scroller(cfg).start()


if __name__ == "__main__":
    main()