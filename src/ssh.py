"""
SSH модуль — простая логика подключения
"""

import paramiko
from typing import Callable, Optional, Tuple, List

from src.hostkeys import prepare_host_keys


class SSHClient:
    """Класс для управления SSH соединением"""

    def __init__(self):
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp = None
        self.is_connected = False

    def connect(
        self,
        hostname: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        key_file: Optional[str] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        """Подключение к серверу"""
        try:
            self.client = paramiko.SSHClient()
            prepare_host_keys(self.client, on_warning=on_warning)

            if key_file:
                self.client.connect(
                    hostname=hostname,
                    port=port,
                    username=username,
                    key_filename=key_file,
                    timeout=10,
                    allow_agent=True
                )
            else:
                self.client.connect(
                    hostname=hostname,
                    port=port,
                    username=username,
                    password=password,
                    timeout=10
                )

            self.sftp = self.client.open_sftp()
            self.is_connected = True
            # keepalive чтобы NAT/firewall не срубали туннель при простое
            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(30)
            return True, "Подключение успешно!"

        except paramiko.ssh_exception.BadHostKeyException as e:
            return False, (
                f"⚠️ Host-key для {e.hostname} изменился "
                f"(сервер прислал {e.key.get_name()})."
                "\nВозможна подмена сервера (MITM-атака) или его "
                "переустановка — соединение отклонено.\n"
                "Чтобы подключиться заново, удалите старый ключ сервера "
                "из known_hosts (папка user_data/SshHostKeys)."
            )
        except Exception as e:
            return False, str(e)

    def execute_command(self, command: str) -> Tuple[bool, str]:
        """Выполнение команды"""
        if not self.is_connected or not self.client:
            return False, "Нет подключения"

        try:
            stdin, stdout, stderr = self.client.exec_command(command)
            output = stdout.read().decode('utf-8', errors='replace')
            error = stderr.read().decode('utf-8', errors='replace')
            exit_status = stdout.channel.recv_exit_status()

            if exit_status != 0 and error:
                return False, error
            return True, output if output else error

        except Exception as e:
            return False, str(e)

    def upload_file(self, local_path: str, remote_path: str) -> Tuple[bool, str]:
        """Загрузка файла на сервер"""
        if not self.is_connected:
            return False, "Нет подключения"

        try:
            self.sftp.put(local_path, remote_path)
            return True, f"Файл загружен: {remote_path}"
        except Exception as e:
            return False, str(e)

    def download_file(self, remote_path: str, local_path: str) -> Tuple[bool, str]:
        """Скачивание файла с сервера"""
        if not self.is_connected:
            return False, "Нет подключения"

        try:
            self.sftp.get(remote_path, local_path)
            return True, f"Файл скачан: {local_path}"
        except Exception as e:
            return False, str(e)

    def list_directory(self, path: str = '.') -> Tuple[bool, List[str]]:
        """Список файлов в директории"""
        if not self.is_connected:
            return False, []

        try:
            files = self.sftp.listdir(path)
            return True, files
        except Exception as e:
            return False, []

    def disconnect(self) -> Tuple[bool, str]:
        """Отключение от сервера"""
        try:
            if self.sftp:
                self.sftp.close()
            if self.client:
                self.client.close()
            self.is_connected = False
            return True, "Отключено"
        except Exception as e:
            return False, str(e)
