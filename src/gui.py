"""
SSH Connect — графический интерфейс (Tkinter).

Интерактивный терминал — НЕ tkinter, а kitty.exe (KiTTY). KittyController
запускает KiTTY с сессионным логом %TEMP%\\kitty_session.log, отправляет
пароль через PostMessage WM_CHAR (никогда не на командной строке) и команды
в окно KiTTY тем же способом, зеркально web-desktop/backend/kitty_launcher.py.

SSHApp — главное окно: форма подключения, профили (ssh_connections.json),
tmux-ряд (row 5), кнопки веб-интерфейса / docker-браузера / обратного доступа.
Внешний вид — палитра FangUnion (фиолетовый, серебристый, бархатно-чёрный,
золотые акценты), перенесён из оригинала (бэкап D:\\SSH-Connect).
"""

import json
import os
import re
import shlex
import socket
import sys
import subprocess
import tempfile
import threading
import time
import base64
import urllib.request
import webbrowser
import ctypes
import ctypes.wintypes as wintypes
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime

from src import paths
from src.clipboard_sync import ClipboardSyncThread
from src.hermes_skills import install_skill_if_missing
from src.reverse_access import ReverseAccess, make_slug
from src.ssh import SSHClient
from src.utils import load_connections, save_connections
from src.hostkeys import get_hostkey_blob

PROJECT_ROOT = paths.project_root()
CONNECTIONS_FILE = paths.user_data_path("ssh_connections.json")
LAST_PROFILE_FILE = paths.user_data_path("last_profile.txt")
USER_DATA_DIR = paths.user_data_dir()
KITTY_EXE = os.path.join(PROJECT_ROOT, "kitty.exe")
KITTY_LOG_FILE = os.path.join(tempfile.gettempdir(), "kitty_session.log")
BACKEND_DIR = os.path.join(PROJECT_ROOT, "web-desktop", "backend")
SERVER_TOKEN_FILE = os.path.join(BACKEND_DIR, ".server_token")
SERVER_LOG_FILE = os.path.join(BACKEND_DIR, "server.log")

WM_CHAR = 0x0102
SW_MINIMIZE = 6
SW_RESTORE = 9

# Запасные пути python для веб-бэкенда, если в SSH_env нет uvicorn/fastapi
PYTHON_CANDIDATES = [
    os.path.join(PROJECT_ROOT, "SSH_env", "Scripts", "python.exe"),
    r"D:\Program Files\Python312\python.exe",
    r"C:\Program Files\Python312\python.exe",
    r"C:\Program Files (x86)\Python312\python.exe",
    r"C:\Users\sdvij\AppData\Local\Programs\Python\Python312\python.exe",
    r"C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe",
]

_RE_HOST = re.compile(r"^[A-Za-z0-9.\-_]+$")
_RE_USER = re.compile(r"^[A-Za-z0-9_\-]+$")


# ===========================================================================
# KittyController — запуск и управление KiTTY через Win32
# ===========================================================================

class KittyController:
    """Управление kitty.exe (KiTTY) через Win32.

    Пароль и команды отправляются посредством PostMessage WM_CHAR — работает
    даже со свёрнутым окном и никогда не попадает в командную строку
    (зеркально original + web-desktop/backend/kitty_launcher.py). Сессионный
    лог (%TEMP%\\kitty_session.log, а для дополнительных окон —
    %TEMP%\\kitty_session_<N>.log) отслеживается, чтобы найти приглашение
    пароля и поймать ошибку авторизации.
    """

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda msg, level="info": None)
        self.process = None
        self.hwnd = None
        self._hostname = None
        self._stop_event = threading.Event()
        self._user32 = ctypes.windll.user32
        self.log_file = KITTY_LOG_FILE
        self.name = None  # подпись окна в GUI («Окно 1», …)
        self.slot = 1     # номер слота окна (1 — основное, 2..5 — доп.)
        # Готово ли окно принимать команды. Свежеоткрытое окно ещё
        # проходит вход по SSH (ввод пароля, баннер) — команда, отправленная
        # в этот момент, просто теряется, поэтому её ставят в очередь.
        self._ready = threading.Event()
        self._started_at = time.time()
        self._password_at = None
        self._last_log_change = time.time()
        self._tail = ""
        self._password_queued = ""
        self._password_sent = False
        self._denied_reported = False
        self._prompt_wait_started = time.time()
        # Находимся ли мы внутри tmux в этом окне: False — окно открыто,
        # tmux не запускали (нужен attach), True — уже внутри сессии
        # (нужен switch-client), None — неизвестно (терминал решит сам).
        self.in_tmux = False

    def _log(self, msg, level="info"):
        try:
            self.on_log(msg, level)
        except Exception:
            pass

    def find_kitty_window(self, pid=None):
        """Найти видимое окно с классом "KiTTY".

        Приоритет: (1) если есть наш процесс — строго по PID (чтобы не
        угнать чужое окно KiTTY, открытое вручную; в этом режиме на другие
        окна НЕ откатываемся), (2) без PID — по заголовку окна, содержащему
        hostname подключения, (3) любое окно класса "KiTTY".
        """
        pid = pid if pid is not None else (
            self.process.pid if self.process is not None else None)
        result = [None]
        fallback = [None]
        win_pid = wintypes.DWORD()
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def callback(hwnd, lparam):
            if not self._user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            self._user32.GetClassNameW(hwnd, buf, 256)
            if buf.value != "KiTTY":
                return True
            self._user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(win_pid))
            if pid is None:
                # нет своего процесса: точный заголовок важнее «любого окна»
                title = ctypes.create_unicode_buffer(512)
                self._user32.GetWindowTextW(hwnd, title, 512)
                if self._hostname and \
                        self._hostname.lower() in title.value.lower():
                    result[0] = hwnd
                    return False
                if fallback[0] is None:
                    fallback[0] = hwnd
                return True
            if win_pid.value == pid:
                result[0] = hwnd
                return False
            return True

        proc = EnumWindowsProc(callback)
        self._user32.EnumWindows(proc, 0)
        hwnd = result[0] if result[0] is not None else fallback[0]
        self.hwnd = hwnd
        return self.hwnd

    def _find_hwnd(self):
        """Актуальный hwnd окна KiTTY (с проверкой, что он ещё живой).

        Кэш используется только если окно существует; иначе ищем заново:
        при живом процессе — строго по PID, иначе по заголовку/классу.
        """
        if self.hwnd and self._user32.IsWindow(self.hwnd):
            return self.hwnd
        self.hwnd = None
        self.find_kitty_window()
        return self.hwnd

    def minimize_window(self):
        """Свернуть окно KiTTY (PostMessage работает и со свёрнутым окном)."""
        hwnd = self._find_hwnd()
        if hwnd:
            try:
                self._user32.ShowWindow(hwnd, SW_MINIMIZE)
            except Exception:
                pass

    # сколько пикселей терминал должен возвышаться над верхним краем главного окна
    RESTORE_RAISE = 60

    def restore_window(self):
        """Развернуть окно KiTTY чуть выше главного окна.

        Терминал разворачивается, центрируется по горизонтали относительно
        главного окна и приподнимается так, чтобы его верхний край был на
        RESTORE_RAISE px выше верха главного окна. z-order не трогаем —
        окно остаётся там, где оно есть (обычно поверх главного).
        """
        hwnd = self._find_hwnd()
        if not hwnd:
            return False
        try:
            self._user32.ShowWindow(hwnd, SW_RESTORE)
            term_rect = wintypes.RECT()
            self._user32.GetWindowRect(hwnd, ctypes.byref(term_rect))
            tw = term_rect.right - term_rect.left
            th = term_rect.bottom - term_rect.top

            main_hwnd = self._user32.FindWindowW(
                None, "SSH Client by Dog Wisdom Project")
            if main_hwnd:
                main_rect = wintypes.RECT()
                self._user32.GetWindowRect(main_hwnd, ctypes.byref(main_rect))
                target_y = max(0, main_rect.top - self.RESTORE_RAISE)
                target_x = main_rect.left + (
                    (main_rect.right - main_rect.left) - tw) // 2
            else:
                target_x = term_rect.left
                target_y = term_rect.top

            # Переместить окно без смены z-order и без активации — только позиция.
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            self._user32.SetWindowPos(hwnd, 0, target_x, target_y, tw, th,
                                      SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return True
        except Exception:
            return False

    def _post_text(self, hwnd, text, delay=0.02):
        for ch in text:
            self._user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
            time.sleep(delay)

    def _post_enter(self, hwnd):
        self._user32.PostMessageW(hwnd, WM_CHAR, 0x0D, 0)

    def send_command(self, command):
        """Отправить команду в окно KiTTY через PostMessage WM_CHAR + Enter.

        Возвращает (успех, сообщение). Работает и со свёрнутым окном.
        """
        hwnd = self._find_hwnd()
        if not hwnd:
            return False, "Окно KiTTY не найдено"
        if not self._user32.IsWindow(hwnd):
            self.hwnd = None
            return False, "Окно KiTTY недоступно"
        try:
            self._post_text(hwnd, command)
            self._post_enter(hwnd)
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def start_kitty_with_logging(self, hostname, port, username, password,
                                 key_file=None, hostkey=None, log_file=None):
        """Запустить kitty.exe с сессионным логом и фоном следить за ним.

        Аргументы передаются списком (без shell=True) — защита от cmd-инъекции
        через имя пользователя/хоста; сами значения строго валидируются.
        `hostkey` — "<тип> <base64>" из known_hosts: передаётся в -hostkey,
        чтобы KiTTY молча проверил ключ и НЕ показывал свой английский диалог
        про host-key при первом подключении.
        `log_file` — свой файл лога для этого окна (несколько окон KiTTY не
        должны затирать лог друг друга).
        Возвращает (успех, сообщение).
        """
        if not _RE_HOST.match(hostname):
            return False, "Некорректный hostname"
        if not _RE_USER.match(username):
            return False, "Некорректный username"
        if not os.path.exists(KITTY_EXE):
            return False, "kitty.exe не найден в папке проекта"

        self._hostname = hostname
        self.log_file = log_file or KITTY_LOG_FILE

        # Свежий файл лога для каждой сессии
        try:
            if os.path.exists(self.log_file):
                os.remove(self.log_file)
        except OSError:
            pass

        args = [KITTY_EXE, "-ssh", "{}@{}".format(username, hostname),
                "-P", str(int(port)), "-log", self.log_file]
        if hostkey:
            args += ["-hostkey", hostkey]
        if key_file:
            args += ["-i", key_file]

        try:
            self.process = subprocess.Popen(
                args,
                cwd=PROJECT_ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            return False, str(e)

        self._password_queued = password or ""
        self._password_sent = False
        self._denied_reported = False
        self._ready.clear()
        self._started_at = time.time()
        self._password_at = None
        self._last_log_change = time.time()
        self._tail = ""
        self._prompt_wait_started = time.time()
        self._stop_event.clear()
        threading.Thread(
            target=self._monitor,
            args=(username or "",),
            daemon=True,
        ).start()
        self._log("🐱 Запуск KiTTY...")
        return True, "OK"

    def stop_watcher(self):
        """Остановить фоновое слежение за логом (окно KiTTY не трогаем)."""
        self._stop_event.set()

    def close_window(self):
        """Закрыть окно KiTTY (WM_CLOSE)."""
        hwnd = self._find_hwnd()
        if hwnd and self._user32.IsWindow(hwnd):
            self._user32.PostMessageW(hwnd, 0x0010, 0, 0)
            self.hwnd = None

    def terminate(self):
        """Принудительно завершить процесс KiTTY, если он ещё жив."""
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    # ------- фон -------

    def _monitor(self, username):
        hwnd = None
        minimized = False
        seen = 0
        pid = self.process.pid if self.process else None

        while not self._stop_event.wait(0.25):
            if not minimized:
                hwnd = self.find_kitty_window(pid)
                if hwnd:
                    self.minimize_window()
                    minimized = True

            try:
                size = os.path.getsize(self.log_file)
                if size < seen:
                    seen = 0
                if size > seen:
                    with open(self.log_file, "r", encoding="utf-8",
                              errors="replace") as f:
                        f.seek(seen)
                        data = f.read()
                    seen = size
                    low = data.lower()
                    self._last_log_change = time.time()
                    self._tail = (self._tail + data)[-600:]

                    # Окно готово принимать команды: пароль принят, а вывод
                    # затих (оболочка ждёт ввода) — либо в логе уже видно
                    # приглашение shell (вход по ключу, без пароля).
                    self._check_ready()

                    # Приглашение пароля появилось в логе — вводим пароль
                    if (self._password_queued and not self._password_sent
                            and ("assword:" in low or "password" in low
                                 or "passphrase" in low)):
                        self._send_password()

                    # Таймаут ожидания приглашения — предупреждаем пользователя
                    if (self._password_queued and not self._password_sent
                            and time.time() - self._prompt_wait_started > 20):
                        self._password_queued = ""
                        self._log("⚠️ KiTTY не показал приглашение пароля — "
                                  "введите его вручную", "warning")

                    # KiTTY отказал в доступе
                    if (self._password_sent and not self._denied_reported
                            and re.search(r"access denied|denied|"
                                          r"authentication failed",
                                          data, re.IGNORECASE)):
                        self._denied_reported = True
                        self._password_sent = False
                        self._password_queued = ""
                        self._log("❌ KiTTY: отказ в доступе. "
                                  "Проверьте логин/пароль.", "error")

                    # Защита от приглашения "login as" (если host без @user)
                    if username and "login as" in low and hwnd is None:
                        if self.find_kitty_window(pid):
                            self._post_text(hwnd, username)
                            self._post_enter(hwnd)
            except OSError:
                pass

            # Готовность окна проверяем каждый тик: если пароль не вводился
            # (вход по ключу), приглашение shell может быть в хвосте лога.
            self._check_ready()

    # Приглашение оболочки в конце строки: «user@host:~$ », «root@host:~# ».
    _SHELL_PROMPT_RE = re.compile(r"[\$#%>][ \t]*\r?\n?$")
    READY_TIMEOUT = 25.0

    def is_ready(self):
        """Готово ли окно KiTTY принимать команды (вход завершён)."""
        return self._ready.is_set()

    def _check_ready(self):
        """Отметить окно готовым, как только завершился вход по SSH.

        Основной сигнал — пароль отправлен и вывод терминала затих: значит
        оболочка уже напечатала приглашение и ждёт ввода. Для входа по ключу
        (пароля нет) ориентируемся на приглашение shell в логе. Любая проверка
        не сработала за READY_TIMEOUT — считаем окно готовым всё равно: лучше
        отправить команду «вслепую», чем потерять её молча.
        """
        if self._ready.is_set():
            return
        now = time.time()
        if now - self._started_at > self.READY_TIMEOUT:
            self._ready.set()
            return
        if (self._password_sent and self._password_at
                and now - self._password_at > 1.0
                and now - self._last_log_change > 0.5):
            self._ready.set()
            return
        if not self._password_queued and self._SHELL_PROMPT_RE.search(
                self._tail or ""):
            self._ready.set()

    def queue_command(self, text, restore=False, timeout=None):
        """Отправить команду, как только окно будет готово к вводу.

        Нужно для свежеоткрытого окна: сразу после запуска идёт вход по SSH,
        ввод пароля и загрузка баннера — команда, отправленная в этот момент,
        теряется. Выполняется в отдельном потоке, UI не блокируется.
        """
        limit = self.READY_TIMEOUT if timeout is None else timeout

        def worker():
            if not self._ready.wait(limit):
                # окно не отмечено готовым (нет приглашения shell) — небольшая
                # пауза, чтобы допечатался баннер, и отправляем в любом случае
                time.sleep(1.0)
            try:
                if restore:
                    self.restore_window()
                self.send_command(text)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _send_password(self):
        """Ввести пароль в KiTTY через PostMessage (не зависит от фокуса)."""
        if not self._password_queued:
            return
        hwnd = self._find_hwnd()
        if not hwnd:
            return
        if not self._user32.IsWindow(hwnd):
            self.hwnd = None
            return
        try:
            self._post_text(hwnd, self._password_queued)
            self._post_enter(hwnd)
            self._password_sent = True
            self._password_at = time.time()
            self._log("🔑 Пароль отправлен в KiTTY")
        except Exception as e:
            self._log("⚠️ Не удалось ввести пароль: {}".format(e), "error")


# ===========================================================================
# SSHApp — главное окно приложения
# ===========================================================================


class Tooltip:
    """Всплывающая подсказка для tkinter-виджета.

    Появляется при наведении курсора, исчезает при уходе.
    Вызов через bind:
        Tooltip(widget, text=\"...\\n...\\n...\")
    """

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._id = None
        self._tip_window = None
        self.widget.bind("<Enter>", self._schedule)
        self.widget.bind("<Leave>", self._hide)
        self.widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, event=None):
        self._hide()
        self._id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self._tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 25
        y = self.widget.winfo_rooty() + 25
        self._tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (x, y))
        tw.wm_attributes("-topmost", True)
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                          background="#241c2e", foreground="#e8dff0",
                          borderwidth=1, relief="solid",
                          font=("Georgia", 9),
                          padx=6, pady=4)
        label.pack()

    def _hide(self, event=None):
        if self._id:
            self.widget.after_cancel(self._id)
            self._id = None
        if self._tip_window:
            self._tip_window.destroy()
            self._tip_window = None


# Блок установки docker + контейнера chrome (linuxserver/chrome, порт 3001):
# печатается в KiTTY при ПЕРВОМ нажатии кнопки «Docker-браузер» на этом хосте,
# дальше кнопка просто открывает браузер. Маркер «показывали» хранится в
# user_data/docker_setup_done.json — блок не повторяется ни в этой сессии, ни
# после перезапуска. Строки уходят в терминал по одной с Enter в конце, поэтому
# многострочная конкатенация `\` здесь свёрнута в одну команду.
DOCKER_SETUP_BLOCK = """\
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker run -d --name=chrome -e PUID=1000 -e PGID=1000 -e TZ=Europe/Moscow -e LC_ALL=ru_RU.UTF-8 -p 3001:3001 -v /opt/chrome/config:/config --shm-size=1gb --restart unless-stopped lscr.io/linuxserver/chrome:latest
"""


# Проверка «уже установлено?» на САМОМ сервере. Локальный маркер
# user_data/docker_setup_done.json теряется при переустановке программы
# (новая версия = новая папка user_data), поэтому перед показом блока
# установки спрашиваем у VPS: есть ли docker и контейнер 'chrome'. Так
# блок не повторяется в каждой новой версии программы.
DOCKER_BROWSER_READY = "DOCKER_BROWSER_READY"
DOCKER_CHECK_COMMAND = (
    "if command -v docker >/dev/null 2>&1 && "
    "docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx chrome; "
    "then echo " + DOCKER_BROWSER_READY + "; "
    "else echo DOCKER_BROWSER_MISSING; fi"
)


def parse_docker_installed(output) -> bool:
    """Установлен ли docker-браузер по ответу DOCKER_CHECK_COMMAND."""
    return DOCKER_BROWSER_READY in (output or "")


def _docker_setup_marker_path():
    return paths.user_data_path("docker_setup_done.json")


def docker_setup_done(hostname) -> bool:
    """Показывался ли блок установки docker этому хосту (по файлу-маркеру)."""
    try:
        with open(_docker_setup_marker_path(), "r", encoding="utf-8") as f:
            return hostname in json.load(f)
    except Exception:
        return False


def mark_docker_setup_done(hostname):
    """Запомнить, что блоку установки этот хост уже показывался."""
    path = _docker_setup_marker_path()
    try:
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        data[hostname] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def build_tmux_attach_command(name, inside_tmux=None):
    """Команда монтирования сессии tmux в окно терминала.

    `inside_tmux` — что мы знаем про целевое окно:
      * False — окно только что открыто, tmux в нём ещё не запускался:
        нужен именно `attach`. `switch-client` здесь бессмысленен (клиента
        нет), из-за чего цепочка `switch || attach || echo` раньше доходила
        до эха «Нет сессии tmux» вместо подключения;
      * True — окно по нашим данным внутри tmux: сначала `switch-client`,
        но с проверкой $TMUX и откатом на `attach` — если пользователь
        отсоединился вручную (Ctrl-b d), флаг устарел и команда сама
        восстановится;
      * None — состояние неизвестно: терминал выбирает сам по $TMUX.
    Если сессия исчезла — печатаем русскую заглушку.
    """
    q = shlex.quote(name)
    echo = " || echo 'Нет сессии tmux'"
    if inside_tmux is False:
        return "tmux attach -t {}{}".format(q, echo)
    return ('[ -n "$TMUX" ] && tmux switch-client -t {q} 2>/dev/null '
            "|| tmux attach -t {q} 2>/dev/null{e}").format(q=q, e=echo)


class SSHApp:
    """Главное окно SSH-клиента. Внешний вид — палитра FangUnion."""

    TMUX_NO_SESSIONS = "Нет сессий"

    # Окна KiTTY: 1 — основное (открывается при подключении, работает как
    # раньше), 2..5 — четыре ДОПОЛНИТЕЛЬНЫХ окна. Список слотов фиксирован:
    # он никогда не растёт, поэтому при каждом подключении в селекторе
    # остаются ровно «Окно 2», «Окно 3», «Окно 4», «Окно 5».
    EXTRA_WINDOW_SLOTS = (2, 3, 4, 5)

    REVERSE_JSON = "~/.hermes/reverse_access.json"

    # Палитра FangUnion (frontend/style.css проекта FangUnion)
    PAL_V_DEEP = "#2a1a3a"        # --violet-deep
    PAL_VIOLET = "#5c3d7a"        # --violet
    PAL_V_MID = "#7a5a9e"         # --violet-mid
    PAL_V_LIGHT = "#9678b6"       # --violet-light
    PAL_V_PALE = "#c8a8e8"        # --violet-pale
    PAL_SILVER = "#b8b0c0"        # --silver
    PAL_BLACK_VELVET = "#1a1220"  # --black-velvet
    PAL_BLACK_SOFT = "#241c2e"    # --black-soft
    PAL_BLACK_MID = "#342840"     # --black-mid
    PAL_TEXT = "#e8dff0"          # --text
    PAL_GOLD = "#d4a853"          # --gold

    def __init__(self, root):
        self.root = root
        self.root.title("SSH Client by Dog Wisdom Project")
        self.root.geometry("980x460")
        self.root.minsize(980, 460)

        self.ssh = None
        self._kitty = None
        self._kitty_windows = {}  # слот окна KiTTY (1..5) -> KittyController
        self._connected = False
        self._last_creds = None
        self._clip_thread = None
        self._reverse = None
        self._reverse_active = False
        self._reverse_folders = []  # список выбранных папок для доступа
        self._reverse_list = None   # Listbox в диалоге менеджера
        self._reverse_slugs = {}    # папка -> уже назначенный слаг (стабильность)
        self._move_cursor_proc = None  # процесс move_cursor (для убийства при выходе)
        self._web_server_proc = None   # процесс uvicorn-бэкенда (для убийства при выходе)
        self._server_log_fd = None     # файловый дескриптор server.log (закрывать при выходе)
        # Тихая установка скила Hermes 'reverse-ssh-tunnel' на VPS (в фоне,
        # не блокирует UI). Результат — в self._hermes_skill_state:
        # 'already_present' | 'installed' | 'hermes_missing'
        self._hermes_skill_state = 'hermes_missing'
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Папка пользователя (создаётся при первом запуске).
        paths.ensure_user_data("SshHostKeys", "Sessions", "Downloads")
        # В собранном exe — ярлык на рабочем столе (создаётся, если его нет,
        # независимо от того, создал ли user_data ещё build.bat).
        self._create_desktop_shortcut()

        self.connections_file = CONNECTIONS_FILE
        self.last_profile_file = LAST_PROFILE_FILE
        self.saved_connections = load_connections(CONNECTIONS_FILE)

        self._setup_styles()
        self._create_widgets()
        self._load_last_profile()
        self._log("=== SSH Client запущен ===", "info")
        self._log("Нажмите '⚡ Подключиться' для входа на сервер", "info")

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ------------------------------------------------------------------ стили

    def _setup_styles(self):
        """Настройка стилей — палитра FangUnion: фиолетовый, серебристый,
        бархатно-чёрный, золотые акценты."""
        P = self  # палитра — константы класса (self.PAL_*)

        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.root.config(bg=P.PAL_BLACK_VELVET)

        self.style.configure(".", background=P.PAL_BLACK_SOFT,
                             foreground=P.PAL_TEXT, font=("Georgia", 10))

        self.style.configure("TLabelframe", background=P.PAL_BLACK_SOFT,
                             bordercolor=P.PAL_V_MID, relief="solid",
                             borderwidth=1)
        self.style.configure("TLabelframe.Label", background=P.PAL_BLACK_SOFT,
                             foreground=P.PAL_V_PALE,
                             font=("Georgia", 10, "bold"))

        self.style.configure("TFrame", background=P.PAL_BLACK_SOFT)
        self.style.configure("TLabel", background=P.PAL_BLACK_SOFT,
                             foreground=P.PAL_SILVER)

        # Кнопки: фиолетовый бархат с подсветкой при наведении
        self.style.configure("TButton", background=P.PAL_V_MID,
                             foreground="#f4eef6",
                             bordercolor=P.PAL_V_LIGHT,
                             lightcolor=P.PAL_V_MID, darkcolor=P.PAL_VIOLET,
                             focuscolor=P.PAL_V_PALE, padding=(10, 5))
        self.style.map("TButton",
                       background=[("pressed", P.PAL_V_DEEP),
                                   ("active", P.PAL_VIOLET),
                                   ("disabled", P.PAL_BLACK_MID)],
                       foreground=[("disabled", "#908898")],
                       bordercolor=[("focus", P.PAL_GOLD)])

        # Опасная кнопка (необратимые удаления) — тёмно-красная
        self.style.configure("Danger.TButton", background="#8a3a3a",
                             foreground="#f4eef6", bordercolor="#b05252",
                             lightcolor="#8a3a3a", darkcolor="#6e2c2c",
                             focuscolor="#f0d0d0", padding=(10, 5))
        self.style.map("Danger.TButton",
                       background=[("pressed", "#6e2c2c"),
                                   ("active", "#a04444"),
                                   ("disabled", P.PAL_BLACK_MID)],
                       foreground=[("disabled", "#908898")],
                       bordercolor=[("focus", P.PAL_GOLD)])

        # Поля ввода: тёмный фон, светлый текст
        self.style.configure("TEntry", fieldbackground=P.PAL_BLACK_VELVET,
                             foreground=P.PAL_TEXT, insertcolor=P.PAL_TEXT,
                             bordercolor=P.PAL_V_MID,
                             lightcolor=P.PAL_BLACK_VELVET,
                             darkcolor=P.PAL_BLACK_VELVET)
        self.style.map("TEntry",
                       bordercolor=[("focus", P.PAL_V_LIGHT)],
                       lightcolor=[("focus", P.PAL_V_LIGHT)],
                       darkcolor=[("focus", P.PAL_V_LIGHT)])

        # Выпадающие списки
        self.style.configure("TCombobox", fieldbackground=P.PAL_BLACK_VELVET,
                             background=P.PAL_BLACK_MID,
                             foreground=P.PAL_TEXT,
                             arrowcolor=P.PAL_V_PALE,
                             bordercolor=P.PAL_V_MID,
                             lightcolor=P.PAL_BLACK_VELVET,
                             darkcolor=P.PAL_BLACK_VELVET)
        self.style.map("TCombobox",
                       fieldbackground=[("readonly", P.PAL_BLACK_VELVET),
                                       ("disabled", P.PAL_BLACK_SOFT)],
                       foreground=[("readonly", P.PAL_TEXT),
                                   ("disabled", P.PAL_SILVER)])
        self.root.option_add("*TCombobox*Listbox*Background", P.PAL_BLACK_SOFT)
        self.root.option_add("*TCombobox*Listbox*Foreground", P.PAL_TEXT)
        self.root.option_add("*TCombobox*Listbox*selectBackground", P.PAL_VIOLET)
        self.root.option_add("*TCombobox*Listbox*selectForeground", P.PAL_GOLD)
        self.root.option_add("*TCombobox*Listbox*Font", ("Georgia", 10))

        self.style.configure("TScrollbar", background=P.PAL_BLACK_MID,
                             troughcolor=P.PAL_BLACK_VELVET,
                             bordercolor=P.PAL_V_MID,
                             arrowcolor=P.PAL_V_PALE,
                             lightcolor=P.PAL_V_MID, darkcolor=P.PAL_V_MID)
        self.style.map("TScrollbar",
                       background=[("pressed", P.PAL_VIOLET),
                                   ("active", P.PAL_V_MID)],
                       arrowcolor=[("disabled", P.PAL_SILVER)])

        self._apply_dark_titlebar(self.root)

    def _apply_dark_titlebar(self, window):
        """Тёмный заголовок в цвет окна (Windows 10 1809+; Win11 также красит
        фон заголовка). На старых системах атрибуты тихо игнорируются."""
        try:
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            if not hwnd:
                return
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on),
                                      ctypes.sizeof(on))
            caption = ctypes.c_uint(0x20121A)  # COLORREF 0x00BBGGRR -> #1a1220
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption),
                                      ctypes.sizeof(caption))
        except Exception:
            pass

    def _set_placeholder(self, entry, placeholder):
        """Плейсхолдер для Entry: приглушённый пример, исчезает при вводе"""
        entry.insert(0, placeholder)
        entry.config(foreground="#908898")

        def on_focus_in(event):
            if entry.get() == placeholder:
                entry.delete(0, tk.END)
                entry.config(foreground="#e8dff0")

        def on_focus_out(event):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.config(foreground="#908898")
            else:
                entry.config(foreground="#e8dff0")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    # ------------------------------------------------------------------ UI

    def _create_widgets(self):
        # Шапка: заголовок раздела «Подключение» и ссылка на документацию
        # на одной строке (ссылка справа).
        header = tk.Frame(self.root, bg=self.PAL_BLACK_VELVET)
        header.pack(fill=tk.X, padx=10, pady=(5, 0))

        title = tk.Label(header, text="Подключение", foreground=self.PAL_V_PALE,
                         background=self.PAL_BLACK_VELVET,
                         font=("Georgia", 10, "bold"))
        title.pack(side=tk.LEFT)

        link = tk.Label(header, text="Документация", foreground=self.PAL_GOLD,
                        cursor="hand2", background=self.PAL_BLACK_VELVET,
                        font=("Georgia", 10, "underline"))
        link.pack(side=tk.RIGHT)
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://github.com/sheldonpaws/Help/tree/main/VPS%20%D0%B8%20Hermes"))

        self._create_connection_panel()
        self._create_status_area()

    def _create_connection_panel(self):
        """Панель подключения (заголовок вынесен в шапку)"""
        frame = tk.Frame(self.root, bg=self.PAL_BLACK_SOFT,
                         highlightthickness=1, highlightbackground=self.PAL_V_MID,
                         padx=10, pady=10)
        frame.pack(fill=tk.X, padx=10, pady=5)

        # Профиль
        ttk.Label(frame, text="Профиль:").grid(row=0, column=0, padx=5, pady=5)
        self.profile_combo = ttk.Combobox(frame, width=20, state="readonly")
        self.profile_combo['values'] = (list(self.saved_connections.keys())
                                        if self.saved_connections
                                        else ["Новое подключение"])
        self.profile_combo.current(0)
        self.profile_combo.grid(row=0, column=1, padx=5, pady=5)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_select)

        ttk.Button(frame, text="Сохранить",
                   command=self._save_connection).grid(row=0, column=2,
                                                       padx=5, pady=5)
        ttk.Button(frame, text="Удалить",
                   command=self._delete_connection).grid(row=0, column=3,
                                                         padx=5, pady=5)

        # Хост
        ttk.Label(frame, text="Хост:").grid(row=1, column=0, padx=5, pady=5)
        self.host_entry = ttk.Entry(frame, width=30)
        self.host_entry.grid(row=1, column=1, padx=5, pady=5)
        self._set_placeholder(self.host_entry, "напр. 185.22.123.45")

        # Порт
        ttk.Label(frame, text="Порт:").grid(row=1, column=2, padx=5, pady=5)
        self.port_entry = ttk.Entry(frame, width=10)
        self.port_entry.grid(row=1, column=3, padx=5, pady=5)
        self.port_entry.insert(0, "22")

        # Пользователь
        ttk.Label(frame, text="Пользователь:").grid(row=2, column=0,
                                                     padx=5, pady=5)
        self.username_entry = ttk.Entry(frame, width=30)
        self.username_entry.grid(row=2, column=1, padx=5, pady=5)
        self.username_entry.insert(0, "root")

        # Пароль
        ttk.Label(frame, text="Пароль:").grid(row=2, column=2, padx=5, pady=5)
        self.password_entry = ttk.Entry(frame, width=20, show="*")
        self.password_entry.grid(row=2, column=3, padx=5, pady=5)

        # Ключ
        ttk.Label(frame, text="SSH ключ:").grid(row=3, column=0, padx=5, pady=5)
        self.key_file_entry = ttk.Entry(frame, width=30)
        self.key_file_entry.grid(row=3, column=1, padx=5, pady=5)
        ttk.Button(frame, text="Обзор...",
                   command=self._browse_key_file).grid(row=3, column=2,
                                                       padx=5, pady=5)
        ttk.Button(frame, text="О ключе",
                   command=self._show_key_info).grid(row=3, column=3,
                                                     padx=5, pady=5)

        # Кнопки подключения
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=4, pady=10)

        self.connect_btn = ttk.Button(btn_frame, text="Подключиться",
                                      command=self._on_connect, width=15)
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.disconnect_btn = ttk.Button(btn_frame, text="Отключиться",
                                         command=self._on_disconnect, width=15,
                                         state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)

        self.webserver_btn = ttk.Button(btn_frame, text="Веб-интерфейс",
                                        command=self._open_web_interface,
                                        width=15, state=tk.DISABLED)
        self.webserver_btn.pack(side=tk.LEFT, padx=5)

        self.docker_browser_btn = ttk.Button(btn_frame, text="Docker-браузер",
                                             command=self._open_docker_browser,
                                             width=16, state=tk.DISABLED)
        self.docker_browser_btn.pack(side=tk.LEFT, padx=5)

        self.access_btn = ttk.Button(btn_frame, text="Доступ",
                                     command=self._toggle_reverse, width=8,
                                     state=tk.DISABLED)
        self.access_btn.pack(side=tk.LEFT, padx=5)
        # индикатор обратного доступа: заполненный кружок ● (зелёный = активен)
        self.access_status = ttk.Label(btn_frame, text="●",
                                       foreground="#908898", font=("Georgia", 16))
        self.access_status.pack(side=tk.LEFT, padx=(0, 10))
        # всплывающая подсказка на индикаторе доступа
        self._status_tooltip = Tooltip(
            self.access_status,
            "Индикатор обратного доступа:\n"
            "● серый — доступ не запущен\n"
            "● зелёный — папки ПК открыты Hermes\n"
            "              через туннель 127.0.0.1:17850"
        )

        # tmux: управление сессиями терминала
        tmux_frame = ttk.Frame(frame)
        tmux_frame.grid(row=5, column=0, columnspan=4, padx=5, pady=5)

        ttk.Label(tmux_frame, text="tmux:").pack(side=tk.LEFT, padx=5)
        self.tmux_session_combo = ttk.Combobox(tmux_frame, state="disabled",
                                               width=16)
        self.tmux_session_combo.pack(side=tk.LEFT, padx=5)
        self.tmux_session_combo.bind("<Button-1>", self._on_tmux_combo_click)
        self.tmux_attach_btn = ttk.Button(tmux_frame, text="Подключить",
                                          command=self._tmux_attach, width=11,
                                          state=tk.DISABLED)
        self.tmux_attach_btn.pack(side=tk.LEFT, padx=5)

        self.tmux_new_btn = ttk.Button(tmux_frame, text="Новая",
                                       command=self._tmux_new, width=8,
                                       state=tk.DISABLED)
        self.tmux_new_btn.pack(side=tk.LEFT, padx=5)
        self.tmux_kill_btn = ttk.Button(tmux_frame, text="Удалить",
                                        command=self._tmux_kill, width=9,
                                        state=tk.DISABLED)
        self.tmux_kill_btn.pack(side=tk.LEFT, padx=5)

        # KiTTY: фиксированный выбор одного из 4 дополнительных окон.
        # Список никогда не меняется — выбранный слот либо открывает окно,
        # либо показывает уже открытое. Окно 1 (основное) в списке нет.
        ttk.Label(tmux_frame, text="Окно:").pack(side=tk.LEFT, padx=(10, 0))
        self.kitty_window_combo = ttk.Combobox(
            tmux_frame, state="disabled", width=9,
            values=[self._slot_label(s) for s in self.EXTRA_WINDOW_SLOTS])
        self.kitty_window_combo.pack(side=tk.LEFT, padx=5)
        self.kitty_window_combo.bind("<<ComboboxSelected>>",
                                     self._on_kitty_window_select)

        # Заглушка в списке tmux до подключения
        self.tmux_session_combo["values"] = [self.TMUX_NO_SESSIONS]
        self.tmux_session_combo.set(self.TMUX_NO_SESSIONS)

    def _create_status_area(self):
        """Компактная область статуса — только лог"""
        frame = ttk.LabelFrame(self.root, text="Журнал", padding=10)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        log_wrap = tk.Frame(frame, bg=self.PAL_BLACK_VELVET)
        log_wrap.pack(fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(log_wrap, orient=tk.VERTICAL)
        self.output_text = tk.Text(log_wrap, wrap=tk.WORD,
                                   bg=self.PAL_BLACK_VELVET,
                                   fg=self.PAL_TEXT, font="Consolas 11",
                                   height=8, insertbackground=self.PAL_TEXT,
                                   relief=tk.FLAT, padx=6, pady=4,
                                   yscrollcommand=sb.set)
        sb.config(command=self.output_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.output_text.tag_configure("command", foreground="#c8a8e8")
        self.output_text.tag_configure("output", foreground="#d4cde0")
        self.output_text.tag_configure("error", foreground="#d97070")
        self.output_text.tag_configure("success", foreground="#6aab7e")
        self.output_text.tag_configure("info", foreground="#9678b6")
        self.output_text.tag_configure("warning", foreground="#d4a853")

    # ================= Диалоги в палитре FangUnion =================

    def _dark_dialog(self, title, message, buttons, icon="", width=380):
        """Модальный диалог в палитре FangUnion.

        buttons: список кортежей (текст, возвращаемое_значение, стиль),
        стиль None -> обычная фиолетовая кнопка, "danger" -> красная.
        Возвращает значение нажатой кнопки либо None при закрытии окна.
        """
        P = self
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=P.PAL_BLACK_SOFT)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        result = {"value": None}

        def close(code):
            result["value"] = code
            dlg.destroy()

        body = tk.Frame(dlg, bg=P.PAL_BLACK_SOFT, padx=24, pady=18,
                        highlightbackground=P.PAL_V_MID, highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(body, bg=P.PAL_BLACK_SOFT)
        header.pack(fill=tk.X)
        if icon:
            tk.Label(header, text=icon, font=("Segoe UI Emoji", 22),
                     bg=P.PAL_BLACK_SOFT, fg=P.PAL_GOLD).pack(
                side=tk.LEFT, padx=(0, 14))
        tk.Label(header, text=title, font=("Georgia", 13, "bold"),
                 wraplength=width - 100, justify=tk.LEFT,
                 bg=P.PAL_BLACK_SOFT,
                 fg=P.PAL_V_PALE).pack(side=tk.LEFT, anchor="w")

        tk.Label(body, text=message, font=("Georgia", 10),
                 wraplength=width - 48, justify=tk.LEFT,
                 bg=P.PAL_BLACK_SOFT,
                 fg=P.PAL_TEXT).pack(anchor="w", pady=(12, 16))

        btn_row = tk.Frame(body, bg=P.PAL_BLACK_SOFT)
        btn_row.pack(fill=tk.X)
        for text, code, bstyle in reversed(buttons):
            btn = ttk.Button(btn_row, text=text, width=10,
                             command=lambda c=code: close(c))
            if bstyle:
                btn.configure(style=bstyle)
            btn.pack(side=tk.RIGHT, padx=(8, 0))

        self._apply_dark_titlebar(dlg)

        dlg.update_idletasks()
        rx = self.root.winfo_rootx() + (self.root.winfo_width()
                                        - dlg.winfo_reqwidth()) // 2
        ry = self.root.winfo_rooty() + (self.root.winfo_height()
                                        - dlg.winfo_reqheight()) // 3
        dlg.geometry("+{}+{}".format(max(rx, 0), max(ry, 0)))
        dlg.grab_set()
        dlg.focus_set()
        dlg.bind("<Escape>", lambda e: close(None))
        self.root.wait_window(dlg)
        return result["value"]

    def _dlg_info(self, title, message):
        self._dark_dialog(title, message, [("Ок", None, None)], icon="ℹ")

    def _dlg_error(self, title, message):
        self._dark_dialog(title, message, [("Ок", None, None)], icon="✖")

    def _dlg_confirm(self, title, message, danger=False):
        """Вопрос да/нет; danger=True рисует красную кнопку «Да»"""
        return bool(self._dark_dialog(
            title, message,
            [("Да", True, "Danger.TButton" if danger else None),
             ("Нет", False, None)],
            icon="⚠" if danger else "?",
        ))

    def _dlg_askstring(self, title, prompt, initial=""):
        """Модальный ввод строки; Enter=Ок, Esc=отмена. None при отмене."""
        P = self
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=P.PAL_BLACK_SOFT)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        result = {"value": None}

        def ok(event=None):
            result["value"] = entry.get()
            dlg.destroy()

        def cancel(event=None):
            dlg.destroy()

        body = tk.Frame(dlg, bg=P.PAL_BLACK_SOFT, padx=24, pady=18,
                        highlightbackground=P.PAL_V_MID, highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text=prompt, font=("Georgia", 11),
                 bg=P.PAL_BLACK_SOFT, fg=P.PAL_SILVER).pack(anchor="w")
        entry = ttk.Entry(body, width=34)
        entry.pack(fill=tk.X, pady=(8, 14))
        if initial:
            entry.insert(0, initial)
        entry.bind("<Return>", ok)
        entry.bind("<Escape>", cancel)

        btn_row = tk.Frame(body, bg=P.PAL_BLACK_SOFT)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Отмена", width=10,
                   command=cancel).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_row, text="Ок", width=10, command=ok).pack(side=tk.RIGHT)

        self._apply_dark_titlebar(dlg)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        dlg.bind("<Escape>", cancel)

        dlg.update_idletasks()
        rx = self.root.winfo_rootx() + (self.root.winfo_width()
                                        - dlg.winfo_reqwidth()) // 2
        ry = self.root.winfo_rooty() + (self.root.winfo_height()
                                        - dlg.winfo_reqheight()) // 3
        dlg.geometry("+{}+{}".format(max(rx, 0), max(ry, 0)))
        dlg.grab_set()
        entry.focus_set()
        entry.icursor(tk.END)
        self.root.wait_window(dlg)
        return result["value"]

    # ------------------------------------------------------- лог

    def _log(self, message, tag="output"):
        """Вывод сообщения в журнал (цветной по тэгу)"""
        self.output_text.insert(tk.END, message + "\n", tag)
        self.output_text.see(tk.END)

    def _queue_log(self, msg, level="info"):
        try:
            self.root.after(0, lambda: self._log(msg, level))
        except Exception:
            pass

    def _thread_log(self, msg, level="info"):
        """Лог из фоновых потоков — перебрасываем в главный через root.after."""
        self._queue_log(msg, level)

    # ================= Профили =================

    def _save_connection(self):
        """Сохранение профиля"""
        profile_name = self._dlg_askstring("Сохранить профиль",
                                           "Введите имя профиля:")
        if profile_name:
            connection = {
                "host": self.host_entry.get(),
                "port": self.port_entry.get(),
                "username": self.username_entry.get(),
                "password": self.password_entry.get(),
                "key_file": self.key_file_entry.get(),
            }
            self.saved_connections[profile_name] = connection
            save_connections(self.connections_file, self.saved_connections)
            self.profile_combo['values'] = list(self.saved_connections.keys())
            self.profile_combo.set(profile_name)
            self._save_last_profile(profile_name)
            self._log("💾 Профиль '{}' сохранён".format(profile_name),
                      "success")

    def _on_profile_select(self, event):
        """Загрузка профиля"""
        profile_name = self.profile_combo.get()
        if profile_name in self.saved_connections:
            self._apply_profile(profile_name)
            self._save_last_profile(profile_name)

    def _apply_profile(self, profile_name):
        """Применение профиля"""
        if profile_name not in self.saved_connections:
            return
        c = self.saved_connections[profile_name]
        self.host_entry.delete(0, tk.END)
        self.host_entry.insert(0, c.get("host", ""))
        self.port_entry.delete(0, tk.END)
        self.port_entry.insert(0, c.get("port", "22"))
        self.username_entry.delete(0, tk.END)
        self.username_entry.insert(0, c.get("username", "root"))
        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, c.get("password", ""))
        self.key_file_entry.delete(0, tk.END)
        self.key_file_entry.insert(0, c.get("key_file", ""))
        # снимаем серый цвет плейсхолдера, т.к. поле реально заполнено
        self.host_entry.config(foreground="#e8dff0")

    def _delete_connection(self):
        """Удаление профиля"""
        profile_name = self.profile_combo.get()
        if profile_name in self.saved_connections:
            if self._dlg_confirm("Удалить",
                                 "Удалить '{}'?".format(profile_name),
                                 danger=True):
                del self.saved_connections[profile_name]
                save_connections(self.connections_file, self.saved_connections)
                self.profile_combo['values'] = (
                    list(self.saved_connections.keys())
                    if self.saved_connections else ["Новое подключение"])
                self.profile_combo.current(0)
                self._log("🗑️ Профиль удалён", "info")

    def _load_last_profile(self):
        """Загрузка последнего профиля"""
        if not os.path.exists(self.last_profile_file):
            if self.saved_connections:
                p = list(self.saved_connections.keys())[0]
                self.profile_combo.set(p)
                self._apply_profile(p)
            return
        try:
            with open(self.last_profile_file, 'r', encoding='utf-8') as f:
                last = f.read().strip()
            if last in self.saved_connections:
                self.profile_combo.set(last)
                self._apply_profile(last)
        except Exception:
            pass

    def _save_last_profile(self, profile_name):
        try:
            with open(self.last_profile_file, 'w', encoding='utf-8') as f:
                f.write(profile_name)
        except Exception:
            pass

    # ================= Подключение =================

    def _collect_creds(self):
        host = self.host_entry.get().strip()
        port = self.port_entry.get().strip() or "22"
        user = self.username_entry.get().strip()
        pwd = self.password_entry.get()
        key = self.key_file_entry.get().strip()
        if not host or not user:
            return None, "Укажите сервер и пользователя"
        if not _RE_HOST.match(host):
            return None, "Некорректный hostname"
        if not _RE_USER.match(user):
            return None, "Некорректное имя пользователя"
        try:
            port_i = int(port)
            if not 1 <= port_i <= 65535:
                return None, "Порт вне диапазона 1-65535"
        except ValueError:
            return None, "Порт должен быть числом"
        return {
            "hostname": host, "port": port_i, "username": user,
            "password": pwd or None, "key_file": key or None,
        }, None

    def _on_connect(self):
        """Подключение к серверу"""
        if self._connected:
            return
        creds, err = self._collect_creds()
        if err:
            self._dlg_error("Ошибка", err)
            return

        self._log("Подключение к {}:{}...".format(creds["hostname"],
                                                  creds["port"]), "info")
        self.connect_btn.config(state=tk.DISABLED)

        ssh = SSHClient()
        ok, msg = ssh.connect(
            hostname=creds["hostname"],
            port=creds["port"],
            username=creds["username"],
            password=creds["password"],
            key_file=creds["key_file"],
            on_warning=self._log,
        )
        self.connect_btn.config(state=tk.NORMAL)

        if not ok:
            self._log("❌ Ошибка: {}".format(msg), "error")
            self._dlg_error("Подключение",
                            "Не удалось подключиться:\n{}".format(msg))
            return

        self.ssh = ssh
        self._connected = True
        self._last_creds = creds
        self._set_connected_ui(True)
        self._log("✅ Подключение успешно! ({}:{})".format(
            creds["hostname"], creds["port"]), "success")

        profile_name = self.profile_combo.get()
        if profile_name and profile_name != "Новое подключение":
            self._save_last_profile(profile_name)

        self._open_kitty()
        self._refresh_tmux_combo()

        # Тихая установка скила Hermes 'reverse-ssh-tunnel' на VPS (в фоне,
        # не блокирует UI). Результат — в self._hermes_skill_state:
        # 'already_present' | 'installed' | 'hermes_missing'
        threading.Thread(
            target=self._do_ensure_hermes_skill,
            args=(ssh,), daemon=True
        ).start()

    def _do_ensure_hermes_skill(self, ssh):
        """Поставить скил Hermes в фоне + показать результат в UI."""
        from src.hermes_skills import install_skill_if_missing
        state = install_skill_if_missing(ssh, on_log=self._log)
        self._hermes_skill_state = state
        self.root.after(0, self._on_hermes_skill_done, state)

    def _on_hermes_skill_done(self, state):
        """Обновить tooltip индикатора доступа после установки скила."""
        if state == 'installed':
            tooltip = (
                "Скилл reverse-ssh-tunnel установлен ✓\n"
                "Hermes видит доступ к папкам ПК через туннель 127.0.0.1:17850"
            )
            self._log("🛠 Скил reverse-ssh-tunnel установлен на VPS", "success")
        elif state == 'already_present':
            tooltip = (
                "Скилл reverse-ssh-tunnel уже установлен ✓\n"
                "Hermes видит доступ к папкам ПК через туннель 127.0.0.1:17850"
            )
        else:
            # hermes_missing — Hermes не установлен, ничего меняем
            return
        try:
            self.access_status.configure(text="●", foreground="#908898")
            if self._status_tooltip is not None:
                self._status_tooltip.text = tooltip
        except Exception:
            pass

    def _open_kitty(self):
        """Открыть KiTTY с логированием для интерактивного терминала.

        Окно 1 — основное: открывается при каждом подключении и работает как
        раньше. Перед открытием закрываются окна, оставшиеся от прошлого
        подключения (иначе они держат слоты занятыми и не закрываются).
        """
        self._close_kitty()
        if self._launch_kitty_window(1):
            self._start_move_cursor()

    def _slot_label(self, slot):
        return "Окно {}".format(slot)

    def _free_kitty_slot(self):
        """Номер первого свободного дополнительного окна (None — все заняты)."""
        self._prune_kitty_windows()
        for slot in self.EXTRA_WINDOW_SLOTS:
            if slot not in self._kitty_windows:
                return slot
        return None

    def _use_kitty_slot(self, slot):
        """Открыть окно KiTTY для слота (или показать уже открытое).

        Возвращает контроллер окна или None при ошибке запуска.
        """
        self._prune_kitty_windows()
        kit = self._kitty_windows.get(slot)
        if kit is None:
            kit = self._launch_kitty_window(slot)
            if kit is None:
                return None
        self._kitty = kit
        try:
            kit.restore_window()
        except Exception:
            pass
        return kit

    def _on_kitty_window_select(self, event=None):
        """Выбор окна в списке «Окно: 2/3/4/5» — открыть его или показать."""
        if not self._connected:
            return
        label = (self.kitty_window_combo.get() or "").strip()
        for slot in self.EXTRA_WINDOW_SLOTS:
            if label == self._slot_label(slot):
                self._use_kitty_slot(slot)
                return

    def _launch_kitty_window(self, slot):
        """Запустить окно KiTTY для конкретного слота (1..5).

        Каждое окно — свой KittyController со своим pid и лог-файлом,
        поэтому в них можно держать разные tmux-сессии. Слот занят/свободен
        решает вызывающий (`_free_kitty_slot`), здесь только запуск.
        """
        creds = self._last_creds
        if not creds:
            return None
        kit = KittyController(on_log=self._queue_log)
        log_file = KITTY_LOG_FILE if slot == 1 else os.path.join(
            tempfile.gettempdir(), "kitty_session_{}.log".format(slot))
        hostkey = get_hostkey_blob(creds["hostname"], int(creds["port"]))
        okk, mm = kit.start_kitty_with_logging(
            creds["hostname"], creds["port"], creds["username"],
            creds["password"] or "", creds["key_file"], hostkey=hostkey,
            log_file=log_file)
        if not okk:
            self._log("⚠️ KiTTY: {}".format(mm), "error")
            return None
        kit.slot = slot
        kit.name = self._slot_label(slot)
        self._kitty_windows[slot] = kit
        self._kitty = kit
        self._log("🐱 {}: открыто {} (окон: {})".format(
            creds["hostname"], kit.name, len(self._kitty_windows)), "info")
        return kit

    def _prune_kitty_windows(self):
        """Забыть окна KiTTY, процесс которых уже завершился.

        Prune только по известному состоянию: `process is None` (окно ещё не
        запускалось / процесс недоступен) считаем живым, чтобы не отбросить
        рабочее окно. Закрыл пользователь окно — слот снова свободен.
        """
        for slot, kit in list(self._kitty_windows.items()):
            proc = getattr(kit, "process", None)
            try:
                exited = proc is not None and proc.poll() is not None
            except Exception:
                exited = False
            if exited:
                try:
                    kit.stop_watcher()
                except Exception:
                    pass
                del self._kitty_windows[slot]
                self._log("🪟 {} закрыто — слот свободен".format(kit.name),
                          "info")
        if self._kitty is not None and self._kitty not in list(
                self._kitty_windows.values()):
            self._kitty = self._kitty_windows.get(1)

    def _focused_kitty(self):
        """Окно KiTTY, которое сейчас в фокусе (None — ни одного).

        Активным считается то окно, с которым работает пользователь: кликнул
        по нужному терминалу — туда и уходят команды.
        """
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None
        if not fg:
            return None
        for kit in self._kitty_windows.values():
            try:
                hwnd = kit.find_kitty_window()
            except Exception:
                continue
            if hwnd and hwnd == fg:
                return kit
        return None

    def _active_kitty(self):
        """Окно-получатель команд.

        Приоритет: 1) окно в фокусе Windows (с ним работает пользователь),
        2) окно, выбранное в списке «Окно:» / последнее открытое — чтобы
        выбор окна работал и после клика на комбобокс сессий tmux,
        3) основное окно 1.
        """
        self._prune_kitty_windows()
        kit = self._focused_kitty()
        if kit is not None:
            self._kitty = kit
        elif self._kitty not in list(self._kitty_windows.values()):
            self._kitty = self._kitty_windows.get(1)
        return self._kitty

    def _create_desktop_shortcut(self):
        """Создать ярлык на рабочем столе в собранном exe.

        Best-effort: ошибки молча игнорируются; существующий ярлык не трогаем.
        Вызывается при каждом запуске, но сам ярлык создаётся только один раз
        (если `SSH-Connect.lnk` уже есть — poweshell сразу выходит).
        Ярлык указывает на саму программу (иконка берётся из exe).
        """
        if not getattr(sys, "frozen", False):
            return
        try:
            exe = sys.executable
            desktop = r"[Environment]::GetFolderPath('Desktop')"
            ps = (
                "$d={d}; $l=Join-Path $d 'SSH-Connect.lnk'; "
                "if(Test-Path $l){{exit}}; "
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); "
                "$s.TargetPath='{e}'; $s.WorkingDirectory='{w}'; "
                "$s.IconLocation='{e},0'; $s.Description='SSH-Connect'; $s.Save()"
            ).format(
                d=desktop,
                e=str(exe).replace("'", "''"),
                w=str(PROJECT_ROOT).replace("'", "''"),
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", ps],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=30)
        except Exception:
            pass

    def _interpreter_for_helpers(self):
        """Интерпретатор для вспомогательных скриптов (move_cursor и т.п.).

        В собранном exe sys.executable — это сам GUI, поэтому нужен внешний
        python (SSH_env рядом с программой), иначе скрипт не запустить.
        """
        if not getattr(sys, "frozen", False):
            return sys.executable
        for cand in (
            os.path.join(PROJECT_ROOT, "SSH_env", "Scripts", "pythonw.exe"),
            os.path.join(PROJECT_ROOT, "SSH_env", "Scripts", "python.exe"),
        ):
            if os.path.exists(cand):
                return cand
        return None

    def _start_move_cursor(self):
        """Запустить move_cursor (клавиши → колесо мыши для tmux).

        Единый файл <корень программы>/move_cursor.py. Настройки и журнал —
        в папке пользователя (user_data), передаётся флагом --data-dir.
        При повторном запуске старый процесс убивается.
        """
        self._stop_move_cursor()
        move_cursor_path = os.path.join(PROJECT_ROOT, "move_cursor.py")
        py = self._interpreter_for_helpers()
        if not os.path.exists(move_cursor_path):
            self._log("⚠️ move_cursor не найден — PgUp/PgDn не будут "
                       "работать в tmux", "warning")
            return
        if not py:
            self._log("⚠️ move_cursor: не найден python рядом с программой "
                       "(SSH_env/Scripts/pythonw.exe)", "warning")
            return
        self._log("Запуск Move Cursor...", "info")
        self._move_cursor_proc = subprocess.Popen(
            [py, move_cursor_path, "--data-dir", paths.user_data_dir()],
            creationflags=subprocess.CREATE_NO_WINDOW)

    def _stop_move_cursor(self):
        """Остановить процесс move_cursor, если он запущен."""
        proc = self._move_cursor_proc
        if proc is None:
            return
        self._move_cursor_proc = None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def _on_disconnect(self):
        """Отключение"""
        if not self._connected:
            return
        self._stop_clipboard_sync()
        self._stop_reverse_access()
        self._stop_move_cursor()

        try:
            if self.ssh:
                self.ssh.disconnect()
        except Exception as e:
            self._log("⚠️ Ошибка при отключении: {}".format(e), "error")
        finally:
            self.ssh = None
        self._connected = False
        self._set_connected_ui(False)
        self._log("Отключено", "info")

    _disconnect = _on_disconnect

    def _set_connected_ui(self, connected):
        """Кнопки управления активны только пока есть подключение."""
        self.connect_btn.configure(
            state=tk.NORMAL if not connected else tk.DISABLED)
        for w in (self.disconnect_btn, self.webserver_btn,
                  self.docker_browser_btn, self.access_btn,
                  self.tmux_attach_btn,
                  self.tmux_new_btn, self.tmux_kill_btn):
            w.configure(state=tk.NORMAL if connected else tk.DISABLED)
        self.tmux_session_combo.configure(
            state="readonly" if connected else "disabled")
        self.kitty_window_combo.configure(
            state="readonly" if connected else "disabled")
        try:
            self.access_status.configure(text="●",
                                         foreground="#6aab7e"
                                         if (connected and self._reverse_active)
                                         else "#908898")
        except Exception:
            pass

    def _on_closing(self):
        """Очистка и закрытие"""
        self._stop_clipboard_sync()
        self._stop_reverse_access()
        self._stop_move_cursor()
        self._stop_web_server()
        self._close_kitty()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _close_kitty(self):
        """Закрыть все окна KiTTY и дождаться выхода процессов.

        Без этого kitty.exe остаётся висеть и держит свой exe-файл в папке
        программы — удалить дистрибутив можно только после перезагрузки.
        """
        windows = list(self._kitty_windows.values())
        if self._kitty is not None and self._kitty not in windows:
            windows.append(self._kitty)
        self._kitty_windows = {}
        self._kitty = None
        for kitty in windows:
            try:
                kitty.stop_watcher()
            except Exception:
                pass
            try:
                kitty.close_window()
            except Exception:
                pass
        if windows:
            time.sleep(0.3)
        for kitty in windows:
            try:
                kitty.terminate()
            except Exception:
                pass

    def _stop_web_server(self):
        """Остановить запущенный нами uvicorn-бэкенд и закрыть его лог-файл.

        Бэкенд выполняется из папки программы (SSH_env/pythonw.exe + main.py)
        и держит файлы папки, если его не убить при выходе.
        """
        proc = self._web_server_proc
        self._web_server_proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                pass
        if self._server_log_fd is not None:
            try:
                self._server_log_fd.close()
            except Exception:
                pass
            self._server_log_fd = None

    # ================= обратный доступ =================

    def _toggle_reverse(self):
        """Включить/управлять обратным доступом к папкам Windows.

        Пока доступ выключен — выбрать первую папку и запустить. Когда включён —
        открыть менеджер папок (добавить/убрать/остановить). Все папки живут
        на одном порту 17850 как подпути /<slug>/ (см. ReverseAccess).
        """
        if not (self._connected and self.ssh):
            self._log("❌ Нет подключения", "error")
            self._dlg_error("Обратный доступ", "Сначала нужно подключиться.")
            return
        if self._reverse_active:
            self._manage_reverse_dialog()
            return
        folder = filedialog.askdirectory(
            title="Папка для обратного доступа")
        if not folder:
            return
        if folder not in self._reverse_folders:
            self._reverse_folders.append(folder)
        self._start_reverse_access()

    def _start_reverse_access(self):
        """Запустить (или перезапустить с новым списком) обратный доступ."""
        if not (self._connected and self.ssh):
            return
        if not self._reverse_folders:
            return
        transport = self.ssh.client.get_transport()
        if transport is None:
            self._log("❌ Нет SSH-транспорта", "error")
            return
        if self._reverse_active and self._reverse:
            self._reverse.stop()
            self._reverse = None
            self._reverse_active = False
        # сохраняем старые слаги, чтобы папки не переименовывались при рестарте
        used = set(self._reverse_slugs.values())
        for folder in self._reverse_folders:
            if folder not in self._reverse_slugs:
                self._reverse_slugs[folder] = make_slug(folder, used)
                used.add(self._reverse_slugs[folder])
        mounts = {self._reverse_slugs[f]: f for f in self._reverse_folders}
        rev = ReverseAccess(mounts, on_log=self._log)
        ok, msg = rev.start(transport, port=ReverseAccess.FIXED_PORT)
        if not ok:
            self._reverse_folders = []
            self._reverse_slugs = {}
            self._log("❌ Обратный доступ: {}".format(msg), "error")
            self._dlg_error("Обратный доступ", msg)
            return
        self._reverse = rev
        self._reverse_active = True
        try:
            self.access_status.configure(text="●", foreground="#6aab7e")
        except Exception:
            pass
        self._sync_reverse_manifest()
        summary = "; ".join(
            "{} -> {}".format(f, rev.url_for(s))
            for s, f in rev.mounts.items())
        self._log("🟢 Обратный доступ: " + summary, "success")

    def _manage_reverse_dialog(self):
        """Менеджер расшаренных папок: добавить/убрать/остановить доступ."""
        if not (self._connected and self.ssh):
            return
        P = self
        dlg = tk.Toplevel(self.root)
        dlg.title("Обратный доступ")
        dlg.configure(bg=P.PAL_BLACK_SOFT)
        dlg.transient(self.root)
        result = {"value": None}

        body = tk.Frame(dlg, bg=P.PAL_BLACK_SOFT, padx=20, pady=16,
                        highlightbackground=P.PAL_V_MID, highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text="Папки для доступа (порт {})".format(
            ReverseAccess.FIXED_PORT), font=("Georgia", 12, "bold"),
            bg=P.PAL_BLACK_SOFT, fg=P.PAL_V_PALE).pack(anchor=tk.W)
        list_frame = tk.Frame(body, bg=P.PAL_BLACK_SOFT)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 8))
        self._reverse_list = tk.Listbox(
            list_frame, bg="#241a2e", fg=P.PAL_TEXT, selectbackground=P.PAL_V_MID,
            selectforeground="#ffffff", relief=tk.FLAT, height=8,
            highlightthickness=1, highlightbackground=P.PAL_V_MID,
            font=("Consolas", 10))
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                           command=self._reverse_list.yview)
        self._reverse_list.configure(yscrollcommand=sb.set)
        self._reverse_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        def refresh_list():
            self._reverse_list.delete(0, tk.END)
            if self._reverse:
                for slug, folder in self._reverse.mounts.items():
                    self._reverse_list.insert(
                        tk.END, "{}  ->  {}".format(folder,
                                                    self._reverse.url_for(slug)))
            elif self._reverse_folders:
                for f in self._reverse_folders:
                    self._reverse_list.insert(tk.END, "{}  (не запущен)".format(f))

        def add_folder():
            folder = filedialog.askdirectory(
                title="Добавить папку для обратного доступа")
            if not folder:
                return
            if folder not in self._reverse_folders:
                self._reverse_folders.append(folder)
            self._start_reverse_access()
            refresh_list()

        def stop_all():
            self._stop_reverse_access()
            dlg.destroy()

        refresh_list()

        btn_row = tk.Frame(body, bg=P.PAL_BLACK_SOFT)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Добавить", width=10,
                   command=add_folder).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Готово", width=10,
                   command=dlg.destroy).pack(side=tk.RIGHT)
        if self._reverse_active:
            ttk.Button(btn_row, text="Стоп доступ", width=12,
                       style="Danger.TButton",
                       command=stop_all).pack(side=tk.RIGHT, padx=(0, 8))

        self._apply_dark_titlebar(dlg)
        dlg.update_idletasks()
        rx = self.root.winfo_rootx() + (self.root.winfo_width()
                                        - dlg.winfo_reqwidth()) // 2
        ry = self.root.winfo_rooty() + (self.root.winfo_height()
                                        - dlg.winfo_reqheight()) // 3
        dlg.geometry("+{}+{}".format(max(rx, 0), max(ry, 0)))
        dlg.grab_set()
        dlg.focus_set()
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _stop_reverse_access(self):
        """Остановить обратный доступ и убрать записи с сервера."""
        if not self._reverse_active or not self._reverse:
            if self._reverse_folders:
                self._reverse_folders = []
            return
        try:
            self._reverse.stop()
        except Exception as e:
            self._log("⚠️ Ошибка остановки обратного доступа: {}".
                      format(e), "error")
        finally:
            self._reverse = None
            self._reverse_active = False
            self._reverse_folders = []
            self._reverse_slugs = {}
            try:
                self.access_status.configure(text="●",
                                             foreground="#908898")
            except Exception:
                pass
            self._log("⚪ Обратный доступ выключен", "info")
        self._write_reverse_json({})

    def _read_reverse_json(self):
        """Прочитать ~/.hermes/reverse_access.json с VPS.

        Возвращает ЧИСТЫЙ словарь {папка: url} — записи старого формата
        (records-массив, "active" флаги и т.п.) отбрасываются, чтобы не
        замусоривать файл при записи.
        """
        if not (self._connected and self.ssh):
            return {}
        ok, out = self._execute_server_command(
            "python3 -c \"import json,os;"
            "f=os.path.expanduser('~/.hermes/reverse_access.json');"
            "print(json.dumps(json.load(open(f))) if os.path.exists(f) else '{}')\"")
        if not ok:
            return {}
        try:
            data = json.loads(out.strip())
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        clean = {}
        for folder, url in data.items():
            if isinstance(folder, str) and isinstance(url, str) and url.startswith("http"):
                clean[folder] = url
        return clean

    def _write_reverse_json(self, data):
        """Записать reverse_access.json на VPS (base64, без проблем с кавычками)."""
        if not (self._connected and self.ssh):
            return
        payload = base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii")
        cmd = ("mkdir -p ~/.hermes && "
               "echo {} | base64 -d > ~/.hermes/reverse_access.json"
               ).format(payload)
        self._execute_server_command(cmd)

    def _sync_reverse_manifest(self):
        """Записать в ~/.hermes/reverse_access.json манифест всех активных
        папок: {папка: http://127.0.0.1:17850/<slug>/}. Вызывается после
        (пере)запуска доступа; при остановке файл становится {} (пусто)."""
        if not (self._connected and self.ssh):
            return
        data = {}
        if self._reverse_active and self._reverse:
            for slug, folder in self._reverse.mounts.items():
                data[folder] = self._reverse.url_for(slug)
        self._write_reverse_json(data)

    # ================= tmux =================

    def _execute_server_command(self, command):
        if not self.ssh or not self.ssh.is_connected:
            return False, "Нет подключения"
        return self.ssh.execute_command(command)

    def _ensure_hermes_skill(self, ssh):
        """Поставить скил Hermes 'reverse-ssh-tunnel' на VPS в фоновом потоке.

        Вызывается после успешного подключения; ничего не блокирует и молчит
        об ошибках (если hermes нет на сервере — просто пропускает).
        """
        install_skill_if_missing(ssh, on_log=self._log)

    def _send_to_window(self, kit, text, restore=True):
        """Отправить команду в конкретное окно KiTTY.

        Если окно ещё проходит вход по SSH (только что открыто), команда
        ставится в очередь и уходит сразу после готовности терминала —
        иначе она была бы введена до появления shell и пропала бы.
        restore=True — развернуть окно перед отправкой (пользователь должен
        увидеть результат, напр. tmux new/attach).
        """
        if not self._kitty_ready(kit):
            self._log("⏳ {} ещё входит на сервер — команда уйдёт сразу "
                      "после входа".format(getattr(kit, "name", "окно")),
                      "info")
            kit.queue_command(text, restore=restore)
            return
        if restore:
            try:
                kit.restore_window()
            except Exception:
                pass
        ok, msg = kit.send_command(text)
        if not ok:
            self._log("❌ KiTTY: {}".format(msg), "error")

    def _kitty_ready(self, kit):
        """Готово ли окно к вводу (у подделок в тестах — всегда да)."""
        try:
            return kit.is_ready()
        except Exception:
            return True

    def _send_to_kitty(self, text, restore=True):
        """Отправить команду в активное окно KiTTY (должно быть запущено).

        Активное окно — то, что сейчас в фокусе Windows, иначе основное
        окно 1 (либо последнее открытое дополнительное).
        """
        self._prune_kitty_windows()
        kit = self._active_kitty()
        if not kit:
            self._log("❌ KiTTY не запущен", "error")
            return
        self._send_to_window(kit, text, restore=restore)

    def _tmux_sessions(self):
        """Список сессий tmux на сервере.

        Возвращает (ok, names): ok=False — проверить не удалось (нет
        подключения / сбой сети) — в этом случае UI НЕ должен сбрасывать
        список; ok=True — достоверный ответ сервера (пустой список =
        сервер не запущен или сессий нет).
        """
        if not self._connected:
            return False, []
        ok, out = self._execute_server_command("tmux ls 2>&1")
        if not ok:
            return False, []
        out = (out or "").strip()
        if not out:
            return True, []
        low = out.lower()
        if any(low.startswith(marker) for marker in (
                "no server running", "no sessions", "error connecting",
                "server error", "failed to connect")):
            return True, []
        names = []
        seen = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            name = line.split(":", 1)[0].strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return True, names

    def _apply_tmux_sessions(self, sessions):
        """Применить список сессий к комбобоксу (вызывается только в UI-потоке)."""
        if sessions:
            self.tmux_session_combo["values"] = sessions
            current = self.tmux_session_combo.get()
            if current not in sessions:
                self.tmux_session_combo.set(sessions[0])
        else:
            self.tmux_session_combo["values"] = [self.TMUX_NO_SESSIONS]
            self.tmux_session_combo.set(self.TMUX_NO_SESSIONS)

    def _refresh_tmux_combo(self):
        """Обновить выпадающий список сессий tmux (в фоне, без зависания UI).

        `tmux ls` ходит на сервер по SSH синхронно; чтобы клик не блокировал
        окно, запрос выполняется в отдельном потоке, а комбобокс обновляется
        через root.after. Если проверить не удалось (сбой сети) — текущий
        список сохраняется, НЕ сбрасывается на «Нет сессий».
        """
        if getattr(self, "_tmux_refreshing", False):
            return
        self._tmux_refreshing = True

        def worker():
            try:
                ok, sessions = self._tmux_sessions()
                if not ok:
                    return
                root = self.root
                try:
                    root.after(0, lambda: self._apply_tmux_sessions(sessions))
                except Exception:
                    pass
            finally:
                self._tmux_refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_tmux_combo_click(self, event=None):
        """Клик по выпадающему списку — перечитать сессии с сервера."""
        if self.tmux_session_combo.cget("state") == "disabled":
            return
        self._refresh_tmux_combo()

    def _tmux_new(self):
        """Создать новую сессию tmux в СВОБОДНОМ дополнительном окне KiTTY.

        Так каждое окно терминала получает свою сессию, а основное окно 1
        остаётся нетронутым. Если все 4 дополнительных окна заняты — сессия
        создаётся в активном окне с предупреждением.
        """
        if not self._connected:
            return
        name = self._dlg_askstring("Новая сессия tmux", "Имя сессии:")
        if not name:
            return
        if not name.strip():
            self._log("Имя сессии не может быть пустым", "error")
            return
        slot = self._free_kitty_slot()
        kit = self._use_kitty_slot(slot) if slot is not None else None
        if slot is not None and kit is None:
            return  # не удалось открыть окно — сообщение уже в журнале
        if kit is None:
            kit = self._active_kitty()
            if kit is None:
                self._log("❌ KiTTY не запущен", "error")
                return
            self._log("⚠️ Все дополнительные окна заняты — сессия "
                      "откроется в {}", "warning".format(kit.name))
        else:
            self._log("🪟 Сессия откроется в {}".format(kit.name), "info")
        self._log("Создаю сессию tmux '{}'...".format(name), "info")
        self._send_to_window(kit, "tmux new -s {}".format(shlex.quote(name)))
        kit.in_tmux = True  # после `tmux new` окно окажется внутри сессии
        self.root.after(3000, self._refresh_tmux_combo)

    def _tmux_selected_name(self):
        """Имя выбранной в комбобоксе сессии (или None при заглушке).

        Если в списке заглушка «Нет сессий» — синхронно перечитаем разок:
        это единственный случай, когда список мог устареть (сессия появилась
        после последнего обновления), а задержка тут приемлема.
        """
        name = (self.tmux_session_combo.get() or "").strip()
        if name and name != self.TMUX_NO_SESSIONS:
            return name
        ok, sessions = self._tmux_sessions()
        if ok and sessions:
            self._apply_tmux_sessions(sessions)
            name = (self.tmux_session_combo.get() or "").strip()
            if name and name != self.TMUX_NO_SESSIONS:
                return name
        return None

    def _tmux_attach(self):
        """Подключить окно терминала к сессии tmux из выпадающего списка.

        Команда зависит от того, что мы знаем про окно: только что открытое
        окно монтируется через `tmux attach`, окно, уже находящееся внутри
        сессии, — через `tmux switch-client` (внутри tmux attach запрещён).
        Если сессия исчезла — печатается русская заглушка.
        """
        name = self._tmux_selected_name()
        if not name:
            self._log("❌ Нет активных сессий tmux. Создайте новую "
                      "кнопкой «Новая»", "error")
            return
        kit = self._active_kitty()
        if kit is None:
            self._log("❌ KiTTY не запущен", "error")
            return
        inside = getattr(kit, "in_tmux", None)
        action = "подключаю" if inside is False else "переключаю на"
        self._log("{} сессию tmux '{}' в {}".format(
            action, name, getattr(kit, "name", "окно")), "info")
        self._send_to_window(
            kit, build_tmux_attach_command(name, inside_tmux=inside))
        # после монтирования окно окажется внутри сессии
        kit.in_tmux = True

    def _tmux_kill(self):
        """Удалить сессию tmux (необратимо).

        Сначала выходим из текущей сессии в KiTTY (`tmux detach`), чтобы
        клиент не «умер» вместе с убитой сессией, затем отправляем kill.
        """
        name = self._tmux_selected_name()
        if not name:
            self._log("❌ Нет активных сессий tmux для удаления", "error")
            return
        if not self._dlg_confirm(
                "Подтверждение",
                "Удалить сессию '{}'? Все процессы в ней будут завершены. "
                "Это действие необратимо.".format(name),
                danger=True):
            return
        self._log("Удаляю сессию tmux '{}'...".format(name), "info")
        kit = self._active_kitty()
        if kit is not None:
            self._log("Выхожу из текущей сессии tmux (tmux detach)...",
                      "info")
            self._send_to_window(kit, "tmux detach", restore=False)
            kit.in_tmux = False  # после detach мы снова вне сессии
            time.sleep(0.3)
        self._send_to_kitty("tmux kill-session -t {}".format(
            shlex.quote(name)), restore=False)
        self.root.after(1000, self._refresh_tmux_combo)

    # ================= веб-интерфейс =================

    def _port_open(self, port):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    def _server_token(self):
        try:
            with open(SERVER_TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
            return token or None
        except OSError:
            return None

    def _find_backend_python(self):
        for cand in PYTHON_CANDIDATES:
            if not os.path.exists(cand):
                continue
            try:
                probe = subprocess.run(
                    [cand, "-c",
                     "import fastapi, uvicorn, paramiko, multipart"],
                    capture_output=True, timeout=25,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                if probe.returncode == 0:
                    return cand
            except Exception:
                continue
        return None

    def _ensure_web_server(self):
        """Убедиться, что веб-бэкенд слушает 127.0.0.1:8000; иначе запустить."""
        if self._port_open(8000):
            return True
        py = self._find_backend_python()
        if not py:
            self._log("⚠️ Не найдено python с uvicorn/fastapi "
                      "для веб-интерфейса", "error")
            return False
        try:
            self._server_log_fd = open(SERVER_LOG_FILE, "a", encoding="utf-8")
        except OSError:
            self._server_log_fd = None
        try:
            self._web_server_proc = subprocess.Popen(
                [py, "main.py"],
                cwd=BACKEND_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=self._server_log_fd or subprocess.DEVNULL,
                stderr=self._server_log_fd or subprocess.DEVNULL,
            )
            self._log("🖥️ Запускаю веб-сервер: " + py, "info")
        except Exception as e:
            self._log("⚠️ Не удалось запустить веб-сервер: {}".format(e),
                      "error")
            return False
        for _ in range(30):
            if self._port_open(8000):
                return True
            time.sleep(0.5)
        self._log("⚠️ Веб-сервер не поднялся за 15 c", "error")
        return False

    def _open_web_interface(self):
        """Запустить локальный веб-интерфейс и открыть в браузере."""
        if not self._connected:
            self._log("❌ Ошибка: сначала подключитесь к серверу!", "error")
            return
        if not self._ensure_web_server():
            return
        token = self._server_token()
        if not token:
            self._log("⚠️ Не найден .server_token бэкенда", "error")
            return
        creds = self._last_creds
        payload = {
            "hostname": creds.get("hostname", ""),
            "port": creds.get("port", 22),
            "username": creds.get("username", ""),
            "password": creds.get("password"),
            "key_file": creds.get("key_file"),
        }
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8000/api/connect",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-Auth-Token": token},
                method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            self._log("❌ Веб-интерфейс: {}".format(e), "error")
            return
        if not resp.get("success"):
            self._log("❌ Веб-интерфейс: {}".format(
                resp.get("message", "неизвестная ошибка")), "error")
            return
        sid = resp.get("session_id", "")
        auth = resp.get("auth_token", "")
        url = "http://127.0.0.1:8000/?s={}&t={}".format(sid, auth)
        self._log("🌐 Открываю веб-интерфейс...", "info")
        webbrowser.open(url)

    # ================= docker-браузер =================

    def _open_docker_browser(self):
        """Открыть Docker-браузер на сервере (https://<хост>:3001) и включить
        синхронизацию буфера обмена с ним."""
        if not self._connected:
            self._log("❌ Ошибка: сначала подключитесь к серверу!", "error")
            return
        hostname = self._last_creds.get("hostname", "")
        if not hostname:
            self._log("❌ Ошибка: укажите хост сервера", "error")
            return
        # Локальный маркер: блок установки уже показывали этому хосту.
        if docker_setup_done(hostname):
            self._show_docker_browser(hostname)
            return
        # Маркера нет (в т.ч. после переустановки программы) — спрашиваем
        # сам сервер, установлен ли docker + контейнер chrome, чтобы НЕ
        # запускать установку заново в каждой новой версии программы.
        self._log("🔎 Проверяю установку Docker-браузера на сервере…", "info")
        root = self.root

        def worker():
            try:
                ok, out = self._execute_server_command(DOCKER_CHECK_COMMAND)
                installed = bool(ok) and parse_docker_installed(out)
            except Exception:
                installed = False
            try:
                root.after(0, lambda: self._apply_docker_check(
                    hostname, installed))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_docker_check(self, hostname, installed):
        """Итог серверной проверки (вызывается в UI-потоке)."""
        if installed:
            mark_docker_setup_done(hostname)
            self._log("✅ Docker-браузер уже установлен на сервере", "success")
            self._show_docker_browser(hostname)
            return
        # Не установлено — печатаем в KiTTY блок установки docker + chrome
        # вместо открытия браузера.
        if self._type_docker_setup():
            mark_docker_setup_done(hostname)
            self._log("🔧 Команды установки docker/контейнера введены в "
                      "терминал. Когда контейнер 'chrome' поднимется — "
                      "нажмите «Docker-браузер» ещё раз.", "info")

    def _show_docker_browser(self, hostname):
        """Открыть браузер и включить синхронизацию буфера обмена."""
        url = "https://{}:3001".format(hostname)
        self._log("🐳 Открываю Docker-браузер: {}".format(url), "info")
        webbrowser.open(url)
        if not self._clip_thread or not self._clip_thread.is_alive():
            self._clip_thread = ClipboardSyncThread(
                self.ssh, log_callback=self._thread_log)
            self._clip_thread.start()

    def _type_docker_setup(self):
        """Печатать в KiTTY блок установки docker + контейнера chrome.

        Строки отправляются по одной с Enter в конце — так каждая команда
        выполняется отдельно (как если бы пользователь вводил её вручную).
        Возвращает True, если все строки отправлены.
        """
        if not self._kitty:
            self._log("❌ KiTTY не запущен", "error")
            return False
        self._kitty.restore_window()
        for line in DOCKER_SETUP_BLOCK.splitlines():
            line = line.strip()
            if not line:
                continue
            ok, msg = self._kitty.send_command(line)
            if not ok:
                self._log("❌ KiTTY: не удалось отправить команду: {}".format(
                    msg), "error")
                return False
            time.sleep(0.1)
        return True

    def _stop_clipboard_sync(self):
        """Остановить синхронизацию буфера обмена."""
        if self._clip_thread and self._clip_thread.is_alive():
            try:
                self._clip_thread.stop()
            except Exception:
                pass
        self._clip_thread = None

    # ================= прочее =================

    def _browse_key_file(self):
        """Выбор SSH ключа"""
        file_path = filedialog.askopenfilename(
            title="Выберите SSH ключ",
            filetypes=[("SSH Keys", "*.pem *.key *.ppk"), ("All Files", "*.*")])
        if file_path:
            self.key_file_entry.delete(0, tk.END)
            self.key_file_entry.insert(0, file_path)

    def _show_key_info(self):
        """Справка по SSH-ключу"""
        self._dlg_info(
            "SSH ключ",
            "Поле необязательно, но желательно для безопасности.\n\n"
            "Вместо пароля можно авторизоваться по SSH-ключу — это безопаснее "
            "(ключ подобрать практически невозможно) и позволяет подключаться "
            "без ввода пароля.\n\n"
            "Укажите путь к приватному ключу (.pem, .key или .ppk). Публичную "
            "часть ключа нужно добавить на сервер в ~/.ssh/authorized_keys."
        )