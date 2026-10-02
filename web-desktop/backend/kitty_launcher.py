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


def find_kitty_window(hostname: str | None = None) -> int | None:
    """Найти видимое окно KiTTY.

    Приоритет: окно, в заголовке которого есть hostname подключения (чтобы
    команда не улетела в чужое окно KiTTY, открытое вручную); если hostname
    не задан — первое видимое окно класса "KiTTY".
    """
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )
    result = [None]
    fallback = [None]

    def callback(hwnd, lParam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != "KiTTY":
            return True
        if hostname:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if hostname.lower() in title.value.lower():
                result[0] = hwnd
                return False
        if fallback[0] is None:
            fallback[0] = hwnd
        return True

    proc = EnumWindowsProc(callback)
    user32.EnumWindows(proc, 0)
    return result[0] if result[0] is not None else fallback[0]


def send_command(command: str, hostname: str | None = None) -> tuple[bool, str]:
    """Отправить команду в окно KiTTY через PostMessage WM_CHAR.

    Команда отправляется посимвольно + Enter. hostname — чтобы найти именно
    окно нужного подключения (иначе любое окно KiTTY).
    Возвращает (успех, сообщение).
    """
    hwnd = find_kitty_window(hostname) if hostname else find_kitty_window()
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