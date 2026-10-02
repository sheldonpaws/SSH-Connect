"""
Проверка host-ключей SSH — защита от атаки MITM.

Новые ключи добавляются в known_hosts с предупреждением,
изменившиеся ключи отклоняются (соединение прерывается).
"""

import os
from typing import Optional

import paramiko
from paramiko.client import MissingHostKeyPolicy

from src import paths


def get_known_hosts_path() -> str:
    """Путь к файлу known_hosts в папке пользователя (user_data/SshHostKeys)."""
    paths.ensure_user_data("SshHostKeys")
    return paths.user_data_path("SshHostKeys", "known_hosts")


def get_hostkey_blob(hostname: str, port: int) -> Optional[str]:
    """Вернуть сохранённый host-key сервера как "<тип> <base64>" (для KiTTY).

    KiTTY принимает тот же blob, что параметр -hostkey (короткий путь —
    запись в known_hosts, которую только что сохранил paramiko). Если передать
    его, KiTTY не показывает свой английский диалог про host-key, а молча
    проверяет ключ. Не найдено/нечитаемо → None (KiTTY спросит сам).
    """
    path = get_known_hosts_path()
    if not os.path.exists(path):
        return None
    name = hostname if int(port) == 22 else "[{}]:{}".format(hostname, int(port))
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("|"):
                    continue
                parts = line.split()
                if len(parts) >= 3 and parts[0] == name:
                    return "{} {}".format(parts[1], parts[2])
    except OSError:
        return None
    return None


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
