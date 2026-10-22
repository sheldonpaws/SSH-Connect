# -*- coding: utf-8 -*-
"""Тесты Docker-браузера (web-desktop/backend/docker_manager.py).

Без сети/сервера: SSH-клиент подменяется фейком. Проверяем, что бэкенд только
ПРОВЕРЯЕТ готовность, ничего не ставя и не отдавая команд установки — их
пользователь берёт из своей документации и выполняет вручную в KiTTY.

Регрессии, которые уже случались на этом коде (не переписывать вслепую):
  * `grep -q` в DOCKER_CHECK_COMMAND — флаг глушит stdout, а check_ready
    решает по содержимому stdout → проверка всегда False при живом docker;
  * `docker ps -a` без `--format` печатает строки таблицы целиком, поэтому
    `grep -x chrome` никогда не совпадёт — имя контейнера выдаёт только
    `--format '{{.Names}}'`;
  * ссылка на удалённую константу DOCKER_SETUP_COMMANDS в main.py роняла
    роут с AttributeError → HTTP 500.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND, ROOT  # noqa: E402,F401

import docker_manager as dm  # noqa: E402


class _FakeStdout:
    def __init__(self, data):
        self._data = data.encode("utf-8")
        self._pos = 0

    def read(self, n):
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeClient:
    def __init__(self, output):
        self.output = output
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return None, _FakeStdout(self.output), None


class _FakeSession:
    def __init__(self, session_id="s1", hostname="vps.my", client=None):
        self.session_id = session_id
        self.hostname = hostname
        self.client = client


class TestDockerCommands(unittest.TestCase):
    def test_parse_docker_installed(self):
        self.assertTrue(dm.parse_docker_installed(dm.DOCKER_BROWSER_READY))
        self.assertTrue(dm.parse_docker_installed("noise\n" + dm.DOCKER_BROWSER_READY))
        self.assertFalse(dm.parse_docker_installed(dm.DOCKER_BROWSER_MISSING))
        self.assertFalse(dm.parse_docker_installed(""))
        self.assertFalse(dm.parse_docker_installed(None))

    def test_check_command_sane(self):
        self.assertIn("command -v docker", dm.DOCKER_CHECK_COMMAND)
        self.assertIn("docker ps -a", dm.DOCKER_CHECK_COMMAND)
        self.assertIn("chrome", dm.DOCKER_CHECK_COMMAND)
        self.assertIn(dm.DOCKER_BROWSER_READY, dm.DOCKER_CHECK_COMMAND)
        self.assertNotIn("sudo", dm.DOCKER_CHECK_COMMAND)

    def test_check_command_must_print_match(self):
        """check_ready решает по stdout → команда обязана ПЕЧАТАТЬ совпадение.

        `grep -q` запрещён (глушит вывод), имя контейнера добирается только
        через `--format '{{.Names}}'` — строка таблицы `docker ps -a` целиком
        с `grep -x` не совпадает никогда.
        """
        cmd = dm.DOCKER_CHECK_COMMAND
        self.assertNotIn("grep -q", cmd)
        self.assertIn("--format", cmd)
        self.assertIn("{{.Names}}", cmd)
        self.assertIn("grep -x", cmd)

    def test_port_constant(self):
        self.assertEqual(dm.DOCKER_PORT, 3001)

    def test_browser_url_is_https(self):
        self.assertEqual(dm.browser_url("vps.my"), "https://vps.my:3001")
        self.assertEqual(dm.browser_url(None), "https://127.0.0.1:3001")


class TestCheckReady(unittest.TestCase):
    def test_ready(self):
        client = _FakeClient(dm.DOCKER_BROWSER_READY)
        self.assertTrue(dm.check_ready(client))

    def test_missing(self):
        client = _FakeClient(dm.DOCKER_BROWSER_MISSING)
        self.assertFalse(dm.check_ready(client))

    def test_empty(self):
        self.assertFalse(dm.check_ready(_FakeClient("")))

    def test_uses_check_command_without_sudo(self):
        client = _FakeClient(dm.DOCKER_BROWSER_READY)
        dm.check_ready(client)
        self.assertEqual(client.commands[0], dm.DOCKER_CHECK_COMMAND + " 2>&1")
        self.assertNotIn("sudo", client.commands[0])


class TestNoAutoInstall(unittest.TestCase):
    """Регресс: бэкенд не должен уметь ставить docker сам и отдавать команды.

    Команды установки пользователь выполняет вручную по своей документации.
    """

    def test_no_install_or_job_api(self):
        for name in ("DockerJob", "start_job", "get_job", "drop_session",
                     "DOCKER_INSTALL_COMMAND", "DOCKER_SETUP_COMMANDS",
                     "DOCKER_SETUP_BLOCK"):
            self.assertFalse(hasattr(dm, name), name)


if __name__ == "__main__":
    unittest.main()
