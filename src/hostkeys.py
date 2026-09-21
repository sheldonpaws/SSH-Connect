"""
Проверка host-ключей SSH — защита от атаки MITM.

Новые ключи добавляются в known_hosts с предупреждением,
изменившиеся ключи отклоняются (соединение прерывается).
"""

import os

import paramiko
from paramiko.client import MissingHostKeyPolicy


def get_known_hosts_path() -> str:
    """Путь к файлу known_hosts проекта"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.join(project_root, "SshHostKeys")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "known_hosts")


class WarnOnNewRejectChangedPolicy(MissingHostKeyPolicy):
    """Новые ключи добавляются с предупреждением, изменённые — отклоняются."""

    def __init__(self, known_hosts_path: str, on_warning=None):
        self._known_hosts_path = known_hosts_path
        self._on_warning = on_warning

    def missing_host_key(self, client, hostname: str, key):
        message = (
            f"⚠️ Новый host-key ({key.get_name()}) для {hostname}. "
            f"Ключ сохранён в {os.path.basename(self._known_hosts_path)}. "
            "Убедитесь, что это именно ваш сервер."
        )
        if self._on_warning:
            self._on_warning(message)
        client._host_keys.add(hostname, key.get_name(), key)
        client.save_host_keys(self._known_hosts_path)


def prepare_host_keys(client, on_warning=None):
    """Загрузить known_hosts и включить проверку host-ключей для клиента."""
    path = get_known_hosts_path()
    if not os.path.exists(path):
        with open(path, "a"):
            pass
    client.load_host_keys(path)
    client.set_missing_host_key_policy(
        WarnOnNewRejectChangedPolicy(path, on_warning=on_warning)
    )
