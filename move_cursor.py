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

Листаем не только KiTTY: если заголовок активного окна содержит одну из
подстрок настройки title_matches (по умолчанию «hermes agent»), PgUp/PgDn
работают и в нём — так историю чата в дашборде Hermes, открытом в браузере,
листаем ровно как историю tmux. Для таких окон колесо вводится через
SendInput (одно событие на нажатие, дельта = notches × 120) — обычный
PostMessage WM_MOUSEWHEEL до браузера доходит ненадёжно: PgUp рывками,
PgDn вовсе молчит. Для KiTTY/tmux остался прежний PostMessage (работает
даже в свёрнутом окне), точка колеса — под курсором (use_cursor_point),
если курсор вне окна или настройка выключена — центр окна.

Хук не делает тяжёлого: он только кладёт задачу в очередь и глотает клавишу.
Все паузы и отправки — в отдельном потоке: обработчик WH_KEYBOARD_LL имеет
~300 мс (LowLevelHooksTimeout), за это время Windows снимает хук, и PgUp/PgDn
перестают работать до перезапуска.

Настройки — в move_cursor.json рядом со скриптом (создаётся сам).
Журнал действий — move_cursor.log.
Если запущено с флагом --data-dir <папка>, настройки и журнал лежат там
(папка пользователя программы, user_data).
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import sys
import threading
import time

# ── Win32 constants ───────────────────────────────────────────────
WM_MOUSEWHEEL   = 0x020A
WM_MOUSEHWHEEL  = 0x020E
WHEEL_DELTA     = 120

# SendInput: ввод колеса как от настоящей мыши
INPUT_MOUSE       = 0x0000
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

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
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


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


def _set_data_dir(data_dir: str):
    """Перенаправить настройки/журнал в папку пользователя (--data-dir)."""
    global CONFIG_FILE, LOG_FILE
    os.makedirs(data_dir, exist_ok=True)
    CONFIG_FILE = os.path.join(data_dir, "move_cursor.json")
    LOG_FILE = os.path.join(data_dir, "move_cursor.log")

DEFAULTS = {
    "pause_key": "f12",
    "up_key": "page_up",
    "down_key": "page_down",
    "notches": 4,          # насечек колеса на одно нажатие (1 ≈ 5 строк tmux)
    "notch_delay": 0.05,   # сек между насечками, чтобы KiTTY не склеивал
    "horizontal": True,    # Shift + PgUp/PgDn — горизонтальный скролл
    # Заголовки окон (без учёта регистра), где PgUp/PgDn тоже превращаются
    # в колесо. По умолчанию пусто — листаем только KiTTY/tmux, как и раньше.
    # Чат дашборда Hermes листает свой плагин chat-scroll, включать браузер
    # сюда больше не нужно (значение оставлено как опция для отладки).
    "title_matches": [],
    "use_cursor_point": True,   # колесо под курсором (как в tmux); иначе — центр
    # Для окон по заголовку (браузер) колесо — через SendInput, одним событием.
    # False = старый способ PostMessage (для диагностики).
    "sendinput_for_title": True,
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


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value.lower()


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _match_target(cls: str, title: str, cfg: dict) -> bool:
    """Наше ли активное окно: KiTTY/PuTTY по классу ИЛИ заголовок из title_matches."""
    cls = (cls or "").lower()
    if "kitty" in cls or "putty" in cls:
        return True
    title_l = (title or "").lower()
    for needle in cfg.get("title_matches") or []:
        needle = str(needle).lower().strip()
        if needle and needle in title_l:
            return True
    return False


def _find_target(cfg: dict) -> tuple:
    """(хэндл активного окна, тип цели): «» / «kitty» / «title».

    «» — активное окно не наше, PgUp/PgDn в нём не трогаем.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindow(hwnd):
        return 0, ""
    cls = _window_class(hwnd)
    if not _match_target(cls, _window_title(hwnd), cfg):
        return 0, ""
    kind = "kitty" if ("kitty" in cls or "putty" in cls) else "title"
    return hwnd, kind


def _wheel_point(hwnd: int, cfg: dict) -> tuple:
    """Точка для WM_MOUSEWHEEL: под курсором, если он внутри окна, иначе центр.

    «Как в tmux»: колесо работает там, куда смотришь — иначе в браузере
    событие упало бы не в ту панель. Курсор вне окна (свёрнуто, другой
    монитор) или use_cursor_point = false → центр, как раньше.
    """
    if cfg.get("use_cursor_point", True):
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            client = wintypes.POINT(pt.x, pt.y)
            if user32.ScreenToClient(hwnd, ctypes.byref(client)):
                rc = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                if rc.left <= client.x < rc.right and rc.top <= client.y < rc.bottom:
                    return client.x, client.y
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    return (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2


def _send_wheel(hwnd: int, msg: int, delta: int, cfg: dict = None):
    """PostMessage WM_MOUSEWHEEL (работает и при свёрнутом окне). Возвращает точку."""
    cx, cy = _wheel_point(hwnd, cfg or {})
    wparam = (delta & 0xFFFF) << 16
    lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    user32.PostMessageW(hwnd, msg, wparam, lparam)
    return cx, cy


def _send_input_wheel(delta: int, horizontal: bool = False) -> bool:
    """Одно событие колеса через SendInput — как будто крутанули мышь.

    Windows сама находит окно и элемент под курсором, поэтому до браузера
    доходит гарантированно и вверх, и вниз. delta = кол-во насечек × 120.
    """
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.mouseData = delta & 0xFFFFFFFF
    inp.mi.dwFlags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return bool(sent)


class Scroller:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._paused = False
        self._hook_proc = None     # держим ссылку, иначе GC убьёт прок
        self._hook = None
        self._up_vk = _vk(cfg["up_key"])
        self._dn_vk = _vk(cfg["down_key"])
        self._pause_vk = _vk(cfg["pause_key"])
        self._queue = queue.Queue()
        self._worker = None

    def _shift_active(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)

    def _submit(self, hwnd: int, kind: str, direction: str, horizontal: bool):
        """Поставить прокрутку в очередь. Из-под хука НИЧЕГО не делаем:
        ни sleep, ни PostMessage, ни запись в журнал — у обработчика
        WH_KEYBOARD_LL ~300 мс, иначе Windows снимает хук."""
        if self._queue.qsize() >= 30:
            return                      # защита от «разгона»: очередь и так позади
        self._queue.put((hwnd, kind, direction, horizontal))

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._scroll(*item)
            except Exception as e:
                _log(f"ошибка прокрутки: {type(e).__name__}: {e}", self._cfg)

    def _scroll(self, hwnd: int, kind: str, direction: str, horizontal: bool):
        sign = 1 if direction == "up" else -1
        notches = max(1, int(self._cfg.get("notches", 1)))

        # Браузер (окно по заголовку): одно атомарное событие ввода.
        if kind == "title" and self._cfg.get("sendinput_for_title", True):
            ok = _send_input_wheel(sign * notches * WHEEL_DELTA, horizontal)
            _log(f"{'H' if horizontal else 'V'} {direction} x{notches} "
                 f"sendinput={'ok' if ok else 'FAIL'}", self._cfg)
            return

        # KiTTY/PuTTY: как раньше — насечки через PostMessage (работает свёрнутым)
        msg = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
        delay = max(0.0, float(self._cfg.get("notch_delay", 0.05)))
        point = None
        for i in range(notches):
            point = _send_wheel(hwnd, msg, sign * WHEEL_DELTA, self._cfg)
            if i < notches - 1:
                time.sleep(delay)
        _log(f"{'H' if horizontal else 'V'} {direction} x{notches} "
             f"hwnd={hwnd} pt={point}", self._cfg)

    def _hook_handler(self, n_code, w_param, l_param):
        try:
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
                    return user32.CallNextHookEx(
                        self._hook, n_code, w_param, l_param)

                if is_down and (vk == self._up_vk or vk == self._dn_vk):
                    hwnd, kind = _find_target(self._cfg)
                    if hwnd:
                        direction = "up" if vk == self._up_vk else "down"
                        horizontal = (self._shift_active()
                                      and self._cfg.get("horizontal", True))
                        # Глотаем и уводим в поток: хук только решает и ставит
                        # задачу, иначе окно листает PgUp своей страницей
                        # вместо настроенного числа насечек.
                        self._submit(hwnd, kind, direction, horizontal)
                        return 1

                if is_up and (vk == self._up_vk or vk == self._dn_vk):
                    hwnd, _kind = _find_target(self._cfg)
                    if hwnd:
                        return 1
        except Exception as e:
            # Исключение в колбэке ctypes печатается и возвращает 0 — клавиша
            # уйдёт в окно. Ловим и фиксируем, чтобы хук не «дёргался».
            try:
                _log(f"ошибка хука: {type(e).__name__}: {e}", self._cfg)
            except Exception:
                pass

        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    def start(self):
        cfg = self._cfg
        _log(f"start: pause={cfg['pause_key']} up={cfg['up_key']} "
             f"down={cfg['down_key']} notches={cfg.get('notches')} "
             f"delay={cfg.get('notch_delay')} horizontal={cfg.get('horizontal')} "
             f"title_matches={cfg.get('title_matches')} "
             f"cursor_point={cfg.get('use_cursor_point')} "
             f"sendinput={cfg.get('sendinput_for_title')}",
             cfg)
        # Отдельный поток шлёт колесо: сам хук спать и писать не должен.
        self._worker = threading.Thread(
            target=self._worker_loop, name="move_cursor", daemon=True)
        self._worker.start()
        self._hook_proc = HOOKPROC(self._hook_handler)
        # Для WH_KEYBOARD_LL модуль можно передать NULL (хук локальный,
        # callback живёт в нашем процессе рядом с message loop)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not self._hook:
            _log(f"SetWindowsHookEx failed: {ctypes.get_last_error()}", cfg)
            self._queue.put(None)
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._worker:
            self._queue.put(None)       # воркер доработает очередь и выйдет
            self._worker = None


def main():
    parser = argparse.ArgumentParser(
        prog="move_cursor",
        description="PgUp/PgDn → колесо мыши для KiTTY/tmux")
    parser.add_argument("--data-dir", default=None, metavar="DIR",
                        help="папка для move_cursor.json и move_cursor.log "
                             "(по умолчанию — рядом со скриптом)")
    args = parser.parse_args()
    if args.data_dir:
        _set_data_dir(args.data_dir)
    cfg = _load_config()
    Scroller(cfg).start()


if __name__ == "__main__":
    main()