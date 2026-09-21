# -*- coding: utf-8 -*-
"""Тесты авторизации веб-API (web-desktop/backend/main.py).

Проверяем middleware напрямую (httpx/TestClient недоступны) и токен-хелперы.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND, ROOT  # noqa: E402,F401

import main as webmain  # noqa: E402


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, path, host="127.0.0.1:8000", token=None):
        self.url = _FakeURL(path)
        self.headers = {"host": host}
        if token is not None:
            self.headers["x-auth-token"] = token


class TestWebAuth(unittest.TestCase):
    def tearDown(self):
        webmain.auth_tokens.clear()

    def _call(self, method, path, host="127.0.0.1:8000", token=None):
        request = _FakeRequest(path, host=host, token=token)

        async def call_next(req):
            return "NEXT"

        return asyncio.run(webmain.auth_middleware(request, call_next))

    def test_server_token_exists(self):
        self.assertTrue(webmain.SERVER_TOKEN)
        self.assertGreaterEqual(len(webmain.SERVER_TOKEN), 20)

    def test_session_token_validity(self):
        self.assertFalse(webmain._is_valid_session_token("nope"))
        webmain.auth_tokens["sid"] = "tok-123"
        self.assertTrue(webmain._is_valid_session_token("tok-123"))

    def test_dns_rebinding_guard(self):
        result = self._call("GET", "/api/files/list", host="evil.com")
        self.assertEqual(result.status_code, 403)

    def test_localhost_host_allowed(self):
        result = self._call("GET", "/", host="localhost:8000")
        self.assertEqual(result, "NEXT")

    def test_api_requires_token(self):
        result = self._call("GET", "/api/files/list")
        self.assertEqual(result.status_code, 403)

    def test_api_rejects_wrong_token(self):
        result = self._call("GET", "/api/files/list", token="wrong")
        self.assertEqual(result.status_code, 403)

    def test_api_accepts_session_token(self):
        webmain.auth_tokens["sid"] = "tok-123"
        result = self._call("GET", "/api/files/list", token="tok-123")
        self.assertEqual(result, "NEXT")

    def test_api_accepts_server_token(self):
        result = self._call("GET", "/api/files/list", token=webmain.SERVER_TOKEN)
        self.assertEqual(result, "NEXT")

    def test_connect_requires_server_token(self):
        result = self._call("POST", "/api/connect", token="session-only")
        self.assertEqual(result.status_code, 403)
        webmain.auth_tokens["sid"] = "session-only"
        result = self._call("POST", "/api/connect", token="session-only")
        self.assertEqual(result.status_code, 403)
        result = self._call("POST", "/api/connect", token=webmain.SERVER_TOKEN)
        self.assertEqual(result, "NEXT")

    def test_non_api_path_is_open(self):
        self.assertEqual(self._call("GET", "/hermes"), "NEXT")

    def test_resource_path_frontend_exists(self):
        frontend = webmain._resource_path("frontend")
        self.assertTrue(os.path.isdir(frontend))
        self.assertTrue(os.path.isfile(webmain._resource_path("frontend/index.html")))


if __name__ == "__main__":
    unittest.main()
