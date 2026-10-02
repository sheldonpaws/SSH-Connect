"""
SSH менеджер для веб-десктопа
Управляет SSH подключениями и выполнением команд
"""

import codecs
import os
import paramiko
import threading
import time
from typing import Optional


def _get_known_hosts_path() -> str:
    """Путь к known_hosts в папке пользователя (user_data/SshHostKeys).

    Зеркало src/hostkeys.py (бэкенд работает отдельным процессом):
    project_root -> user_data (переопределяется SSH_CONNECT_USER_DATA).
    """
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(backend_dir, "..", "..")
    user_data = os.path.join(project_root, "user_data")
    user_data = os.environ.get("SSH_CONNECT_USER_DATA") or user_data
    folder = os.path.join(user_data, "SshHostKeys")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "known_hosts")


class _WarnOnNewRejectChangedPolicy(paramiko.client.MissingHostKeyPolicy):
    """Новые ключи добавляются с предупреждением, изменённые — отклоняются."""

    def __init__(self, known_hosts_path: str):
        self._known_hosts_path = known_hosts_path

    def missing_host_key(self, client, hostname: str, key):
        print(
            f"⚠️ Новый host-key ({key.get_name()}) для {hostname} добавлен в known_hosts"
        )
        client._host_keys.add(hostname, key.get_name(), key)
        client.save_host_keys(self._known_hosts_path)


def _prepare_host_keys(client: paramiko.SSHClient):
    """Загрузить known_hosts и включить проверку host-ключей."""
    path = _get_known_hosts_path()
    if not os.path.exists(path):
        with open(path, "a"):
            pass
    client.load_host_keys(path)
    client.set_missing_host_key_policy(_WarnOnNewRejectChangedPolicy(path))


class SSHSession:
    """Одна SSH сессия с поддержкой интерактивного shell"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.client: Optional[paramiko.SSHClient] = None
        self.channel = None
        self.shell_thread: Optional[threading.Thread] = None
        self.running = False
        self.callbacks = []
        self.hostname: Optional[str] = None

    def connect(self, hostname: str, port: int, username: str,
                password: Optional[str] = None, key_file: Optional[str] = None) -> tuple[bool, str]:
        """Подключение к серверу"""
        try:
            self.hostname = hostname
            self.client = paramiko.SSHClient()
            _prepare_host_keys(self.client)

            if key_file:
                self.client.connect(
                    hostname=hostname, port=port, username=username,
                    key_filename=key_file, timeout=10
                )
            else:
                self.client.connect(
                    hostname=hostname, port=port, username=username,
                    password=password, timeout=10
                )

            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(30)

            # Открываем интерактивный shell
            self.channel = self.client.invoke_shell(
                term='xterm-256color',
                width=120,
                height=30
            )
            self.channel.settimeout(0)

            self.running = True
            self.shell_thread = threading.Thread(
                target=self._read_shell_output,
                daemon=True,
                name=f"shell-reader-{self.session_id}"
            )
            self.shell_thread.start()

            return True, "Подключено"

        except Exception as e:
            return False, str(e)

    def _read_shell_output(self):
        """Фоновое чтение вывода shell"""
        # Инкрементальный декодер не режет многобайтовые UTF-8 символы
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while self.running:
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    text = decoder.decode(data)

                    if text:
                        for callback in self.callbacks:
                            try:
                                callback(text)
                            except Exception:
                                pass

                time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def send_input(self, data: str):
        """Отправка ввода в shell"""
        if self.channel and self.channel.send_ready():
            self.channel.send(data)

    def add_callback(self, callback):
        """Добавить обработчик вывода"""
        self.callbacks.append(callback)

    def remove_callback(self, callback):
        """Удалить обработчик"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def resize(self, width: int, height: int):
        """Изменить размер терминала"""
        if self.channel:
            self.channel.resize_pty(width=width, height=height)

    def close(self):
        """Закрыть сессию"""
        self.running = False
        if self.shell_thread:
            self.shell_thread.join(timeout=2)
        if self.channel:
            self.channel.close()
        if self.client:
            self.client.close()
