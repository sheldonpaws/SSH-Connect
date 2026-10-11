# -*- coding: utf-8 -*-
"""Тесты REST-роутов веб-бэкенда (web-desktop/backend/main.py).

httpx/TestClient в SSH_env нет, поэтому хендлеры вызываются напрямую как
корутины. Проверяем контракт, на который завязан frontend/app.js: набор путей,
возвращаемые ключи и 404 на чужую сессию.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND, ROOT  # noqa: E402,F401

import main as webmain  # noqa: E402
import docker_manager  # noqa: E402


def _call(coro):
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self, session_id="sid-1", hostname="example.com",
                 username="root", password="pw"):
        self.session_id = session_id
        self.hostname = hostname
        self.username = username
        self.password = password
        self.client = None
        self.closed = False

    def close(self):
        self.closed = True


class TestRoutesRegistered(unittest.TestCase):
    """Пути, которые вызывает frontend/app.js, должны существовать."""

    def test_all_frontend_endpoints_present(self):
        present = {(r.path, m) for r in webmain.app.routes
                   for m in getattr(r, 'methods', ())}
        for path, method in [
            ('/', 'GET'),
            ('/api/connect', 'POST'),
            ('/api/status', 'GET'),
            ('/api/files/list', 'POST'),
            ('/api/files/read', 'POST'),
            ('/api/files/save', 'POST'),
            ('/api/files/mkdir', 'POST'),
            ('/api/files/rename', 'POST'),
            ('/api/files/delete', 'POST'),
            ('/api/files/upload', 'POST'),
            ('/api/files/download', 'GET'),
            ('/api/files/download', 'POST'),
            ('/api/programs', 'POST'),
            ('/api/launch', 'POST'),
            ('/api/docker/start', 'POST'),
        ]:
            self.assertIn((path, method), present, path)

    def test_no_hermes_routes_left(self):
        """Hermes удалён из проекта — его эндпоинтов быть не должно."""
        paths = [r.path for r in webmain.app.routes if hasattr(r, 'path')]
        self.assertFalse([p for p in paths if 'hermes' in p])


class TestFrontendAuthParams(unittest.TestCase):
    """app.js стирает ?s=/?t= из адресной строки — значит обязан уметь
    взять их из sessionStorage, иначе F5 ломает всю страницу."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, 'web-desktop', 'frontend', 'js', 'app.js'),
                  encoding='utf-8') as fh:
            cls.js = fh.read()

    def test_params_survive_url_stripping(self):
        self.assertIn('replaceState', self.js)
        self.assertIn('sessionStorage.getItem', self.js)
        # s/t читаются из query, но при отсутствии берутся из хранилища
        self.assertRegex(self.js, r"sessionId\s*=\s*params\.get\('s'\)\s*\|\|")
        self.assertRegex(self.js, r"authToken\s*=\s*params\.get\('t'\)\s*\|\|")

    def test_dead_session_token_is_dropped_on_403(self):
        self.assertIn("'ssh_token', ''", self.js)


class TestSessionGuards(unittest.TestCase):
    def setUp(self):
        webmain.sessions.clear()
        self.addCleanup(webmain.sessions.clear)

    def test_404_on_unknown_session(self):
        from fastapi import HTTPException
        for coro in (
            webmain.file_list(webmain.FileListRequest(session_id='nope')),
            webmain.docker_start(webmain.DockerRequest(session_id='nope')),
        ):
            with self.assertRaises(HTTPException) as ctx:
                _call(coro)
            self.assertEqual(ctx.exception.status_code, 404)


class _DockerClient:
    """SSH-клиент, отвечающий на проверку docker фиксированным выводом."""

    def __init__(self, output):
        self.output = output
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)

        class _Out:
            def __init__(self, data):
                self._data = data.encode('utf-8')
                self._pos = 0

            def read(self, n):
                chunk = self._data[self._pos:self._pos + n]
                self._pos += len(chunk)
                return chunk

        return None, _Out(self.output), None


class TestDockerRoutes(unittest.TestCase):
    def setUp(self):
        webmain.sessions.clear()
        self.addCleanup(webmain.sessions.clear)

    def test_install_when_missing(self):
        sess = _FakeSession('s1')
        sess.client = _DockerClient(docker_manager.DOCKER_BROWSER_MISSING)
        webmain.sessions['s1'] = sess
        data = _call(webmain.docker_start(webmain.DockerRequest(session_id='s1')))
        self.assertTrue(data['success'])
        self.assertEqual(data['state'], 'install')
        self.assertEqual(data['commands'], docker_manager.DOCKER_SETUP_COMMANDS)

    def test_ready_returns_url(self):
        sess = _FakeSession('s1', hostname='vps.my')
        sess.client = _DockerClient(docker_manager.DOCKER_BROWSER_READY)
        webmain.sessions['s1'] = sess
        data = _call(webmain.docker_start(webmain.DockerRequest(session_id='s1')))
        self.assertTrue(data['success'])
        self.assertEqual(data['state'], 'ready')
        self.assertEqual(data['url'], 'https://vps.my:3001')

    def test_no_auto_install_side_effects(self):
        """Проверка лишь читает — никаких sudo/установки в потоке команд."""
        sess = _FakeSession('s1')
        client = _DockerClient(docker_manager.DOCKER_BROWSER_MISSING)
        sess.client = client
        webmain.sessions['s1'] = sess
        _call(webmain.docker_start(webmain.DockerRequest(session_id='s1')))
        self.assertEqual(len(client.commands), 1)
        self.assertNotIn('sudo', client.commands[0])


class TestDownloadGuards(unittest.TestCase):
    def setUp(self):
        webmain.sessions.clear()
        self.addCleanup(webmain.sessions.clear)

    def test_rejects_relative_path(self):
        from fastapi import HTTPException
        webmain.sessions['s1'] = _FakeSession('s1')
        with self.assertRaises(HTTPException) as ctx:
            _call(webmain._download_response('s1', '../etc/passwd'))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_get_without_session_is_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _call(webmain.download_file_get('/etc/hosts'))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
