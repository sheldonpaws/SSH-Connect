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
        ]:
            self.assertIn((path, method), present, path)

    def test_no_hermes_chat_routes_left(self):
        """Веб-чат Hermes удалён из проекта — его роутов быть не должно.

        Мост /api/hermes/start|stop (кнопка «🧠 Hermes», 21.10) легитимен,
        поэтому проверяется только отсутствие роутов ЧАТА (hermes_manager).
        """
        paths = [r.path for r in webmain.app.routes if hasattr(r, 'path')]
        chat = [p for p in paths if 'hermes' in p and p not in
                ('/api/hermes/start', '/api/hermes/stop')]
        self.assertFalse(chat)

    def test_no_docker_routes_left(self):
        """Docker-браузер удалён (инцидент 2026-11) — docker-роутов быть не должно."""
        paths = [r.path for r in webmain.app.routes if hasattr(r, 'path')]
        self.assertFalse([p for p in paths if 'docker' in p])


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
        ):
            with self.assertRaises(HTTPException) as ctx:
                _call(coro)
            self.assertEqual(ctx.exception.status_code, 404)



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
