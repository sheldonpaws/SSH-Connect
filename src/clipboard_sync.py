"""
Синхронизация буфера обмена между локальной машиной (Windows) и браузером
в Docker-контейнере (linuxserver/chrome, Wayland) через SSH-соединение.

Контейнер работает на Wayland (labwc), поэтому буфер читается/пишется
командами wl-paste / wl-copy внутри контейнера. Текст передаётся в контейнер
через base64 (stdin docker exec) — никаких инъекций в команду.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import threading
import time
import os

# Имя контейнера с браузером; можно переопределить переменной окружения
DOCKER_CONTAINER = os.environ.get("SSH_CONNECT_CLIPBOARD_CONTAINER", "chrome")

# Окружение Wayland внутри контейнера.
# ВАЖНО: linuxserver/chrome (labwc) экспортирует WAYLAND_DISPLAY=wayland-1
# (см. /defaults/startwm_wayland.sh); сокет wayland-0 в контейнере тоже
# существует, но к нему не подключён браузер — писать надо только в wayland-1.
_WL_ENV = "XDG_RUNTIME_DIR=/config/.XDG WAYLAND_DISPLAY=wayland-1"

# Интервал опроса (сек)
POLL_INTERVAL = 0.7

# === Локальный буфер обмена Windows (через Win32 API) ===

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = ctypes.c_bool
user32.EmptyClipboard.restype = ctypes.c_bool
user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.CloseClipboard.restype = ctypes.c_bool
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.c_bool


def _open_clipboard(retries: int = 10) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def get_local_clipboard():
    """Прочитать текст из буфера обмена Windows. None — пусто/не текст."""
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_local_clipboard(text: str) -> bool:
    """Записать текст в буфер обмена Windows."""
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode('utf-16-le') + b'\x00\x00'
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


# === Буфер обмена контейнера (через SSH docker exec) ===

def _wl_paste_cmd():
    return (
        "docker exec {c} sh -c \"{env} wl-paste\" 2>/dev/null".format(
            c=DOCKER_CONTAINER, env=_WL_ENV)
    )


def _wl_copy_cmd(text: str):
    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
    return (
        "echo {b64} | base64 -d | docker exec -i {c} sh -c \"{env} wl-copy\" 2>/dev/null".format(
            b64=b64, c=DOCKER_CONTAINER, env=_WL_ENV)
    )


def get_docker_clipboard(ssh_client):
    """Прочитать буфер контейнера через SSH. None — пусто/ошибка."""
    if not ssh_client or not ssh_client.is_connected:
        return None
    try:
        ok, out = ssh_client.execute_command(_wl_paste_cmd())
        if not ok:
            return None
        text = out.rstrip('\n')
        # wl-paste выводит это, когда в буфере ничего нет
        if text in ("", "Nothing is copied"):
            return None
        return text
    except Exception:
        return None


def set_docker_clipboard(ssh_client, text: str) -> bool:
    """Записать текст в буфер контейнера через SSH."""
    if not ssh_client or not ssh_client.is_connected:
        return False
    try:
        ok, _ = ssh_client.execute_command(_wl_copy_cmd(text))
        return ok
    except Exception:
        return False


class ClipboardSyncThread(threading.Thread):
    """Фоновая двусторонняя синхронизация буфера обмена.

    Поллит локальный (Windows) и удалённый (контейнер) буферы. Если
    изменился локальный — пишем в контейнер; изменился удалённый — в
    локальный. Пометки last_* защищают от петли (echo).
    """

    def __init__(self, ssh_client, log_callback=None):
        super().__init__(daemon=True)
        self.ssh = ssh_client
        self._log = log_callback or (lambda m, t: None)
        self._stop = threading.Event()
        self._last_local = None
        self._last_remote = None
        self._trouble_logged = False

    def stop(self):
        self._stop.set()

    def run(self):
        self._log("📋 Синхронизация буфера обмена запущена", "info")
        while not self._stop.wait(POLL_INTERVAL):
            try:
                self._sync_once()
            except Exception:
                if not self._trouble_logged:
                    self._log("⚠️ Ошибка синхронизации буфера обмена", "error")
                    self._trouble_logged = True
        self._log("📋 Синхронизация буфера обмена остановлена", "info")

    def _sync_once(self):
        cur_local = get_local_clipboard()
        cur_remote = get_docker_clipboard(self.ssh)

        # Локальный изменился → пишем в контейнер
        if cur_local is not None and cur_local != self._last_local:
            if cur_local != self._last_remote:
                if set_docker_clipboard(self.ssh, cur_local):
                    self._last_remote = cur_local
            self._last_local = cur_local
            return

        # Удалённый изменился → пишем в локальный
        if cur_remote is not None and cur_remote != self._last_remote:
            if cur_remote != self._last_local:
                if set_local_clipboard(cur_remote):
                    self._last_local = cur_remote
            self._last_remote = cur_remote
