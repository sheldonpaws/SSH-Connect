"""
Запуск программ в окне KiTTY через Win32.

Веб-интерфейс сам не имеет терминала: команда отправляется в уже открытое
окно KiTTY символами PostMessage WM_CHAR (пароль и команды никогда не попадают
в командную строку). Класс окна определяется как "KiTTY" (см. kitty.ini).
"""

import ctypes
import ctypes.wintypes as wintypes
import time

WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


def find_kitty_window() -> int | None:
    """Найти видимое окно KiTTY"""
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )
    result = [None]

    def callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value == "KiTTY":
                result[0] = hwnd
                return False
        return True

    proc = EnumWindowsProc(callback)
    user32.EnumWindows(proc, 0)
    return result[0]


def send_command(command: str) -> tuple[bool, str]:
    """Отправить команду в окно KiTTY через PostMessage WM_CHAR.

    Команда отправляется посимвольно + Enter. Возвращает (успех, сообщение).
    """
    hwnd = find_kitty_window()
    if not hwnd:
        return False, "Окно KiTTY не найдено"

    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return False, "Окно KiTTY недоступно"

    try:
        for char in command:
            user32.PostMessageW(hwnd, WM_CHAR, ord(char), 1)
            time.sleep(0.01)
        # Enter — запуск команды
        user32.PostMessageW(hwnd, WM_CHAR, 0x0D, 1)
        return True, "OK"
    except Exception as e:
        return False, str(e)