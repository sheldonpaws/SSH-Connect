# -*- coding: utf-8 -*-
"""Тесты pacer-прокси (web-desktop/backend/hermes_pacer.py).

Проверяем: синтаксис встроенного скрипта, константы/команду развёртывания,
парсер состояния и реальный прокси-режим (forward + /_health + 502).
"""

import base64
import http.client
import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND  # noqa: E402,F401

import hermes_pacer as hp  # noqa: E402


class TestPacerModule(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(hp.PACER_PORT, "17901")
        self.assertEqual(hp.PACER_RPM, "60")
        # не должен совпадать с портом обратного доступа
        self.assertNotEqual(hp.PACER_PORT, "17850")

    def test_script_is_valid_python(self):
        compile(hp.PACER_SCRIPT, "pacer_proxy.py", "exec")

    def test_pacer_script_text_matches(self):
        self.assertEqual(hp.pacer_script_text(), hp.PACER_SCRIPT)

    def test_ensure_command_shape(self):
        cmd = hp.build_pacer_ensure_command()
        self.assertTrue(cmd.startswith("bash -lc "))
        inner = shlex.split(cmd[len("bash -lc "):])[0]
        self.assertNotIn("{B64}", inner)
        self.assertIn("PORT=17901", inner)
        payload = base64.b64encode(hp.PACER_SCRIPT.encode("utf-8")).decode("ascii")
        self.assertIn(payload, inner)
        self.assertIn("_health", inner)
        self.assertIn("pacer.conf", inner)

    def test_parse_pacer_state(self):
        text = (
            "noise\n"
            "PACER_UPSTREAM=https://generativelanguage.googleapis.com\n"
            "PACER_PORT=17901\n"
            "PACER_RUNNING=yes\n"
            "PACER_CFG=http://127.0.0.1:17901\n"
        )
        st = hp.parse_pacer_state(text)
        self.assertTrue(st["running"])
        self.assertEqual(st["port"], "17901")
        self.assertEqual(st["upstream"], "https://generativelanguage.googleapis.com")
        self.assertEqual(st["base_url"], "http://127.0.0.1:17901")

    def test_parse_pacer_state_empty(self):
        st = hp.parse_pacer_state("")
        self.assertFalse(st["running"])
        self.assertIsNone(st["port"])
        self.assertEqual(st["upstream"], "")


class _StubUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received = []

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append((self.command, self.path, body))
        out = b"UPSTREAM-OK:" + body
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = _handle
    do_POST = _handle


class TestPacerProxyIntegration(unittest.TestCase):
    """Крутит PACER_SCRIPT в namespace и форвардит на локальную заглушку."""

    @classmethod
    def setUpClass(cls):
        _StubUpstream.received = []
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

        cls.tmp = tempfile.TemporaryDirectory()
        cls.conf = os.path.join(cls.tmp.name, "pacer.conf")
        with open(cls.conf, "w", encoding="utf-8") as f:
            f.write("upstream=http://127.0.0.1:%d\n" % cls.upstream_port)

        os.environ["PACER_CONF"] = cls.conf
        os.environ["PACER_LOG"] = os.path.join(cls.tmp.name, "pacer.log")
        os.environ["PACER_RPM"] = "3600"
        cls.ns = {"__name__": "pacer_test"}
        exec(compile(hp.PACER_SCRIPT, "pacer_proxy.py", "exec"), cls.ns)

        cls.server = cls.ns["ThreadingHTTPServer"](
            ("127.0.0.1", 0), cls.ns["_Handler"]
        )
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.tmp.cleanup()
        os.environ.pop("PACER_CONF", None)
        os.environ.pop("PACER_LOG", None)
        os.environ.pop("PACER_RPM", None)

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Connection": "close"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        conn.close()
        return status, data

    def test_health(self):
        status, data = self._request("GET", "/_health")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"ok")

    def test_upstream_from_conf(self):
        self.assertTrue(self.ns["_upstream"]().startswith("http://127.0.0.1:"))

    def test_forward_post_body(self):
        status, data = self._request("POST", "/v1/echo", b"payload-123")
        self.assertEqual(status, 201)
        self.assertEqual(data, b"UPSTREAM-OK:payload-123")
        self.assertEqual(_StubUpstream.received[-1], ("POST", "/v1/echo", b"payload-123"))

    def test_forward_get(self):
        status, data = self._request("GET", "/v1/models")
        self.assertEqual(status, 201)
        self.assertEqual(_StubUpstream.received[-1][0], "GET")

    def test_pacer_allows_within_limit(self):
        pacer = self.ns["_Pacer"](5)
        started = time.monotonic()
        for _ in range(5):
            pacer.wait()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_pacer_prunes_old_stamps(self):
        pacer = self.ns["_Pacer"](1)
        pacer.wait()
        pacer._stamps = [time.monotonic() - 120.0]
        started = time.monotonic()
        pacer.wait()
        self.assertLess(time.monotonic() - started, 2.0)


class TestPacerUpstreamDown(unittest.TestCase):
    def test_upstream_down_returns_502(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conf = os.path.join(tmp.name, "pacer.conf")
        with open(conf, "w", encoding="utf-8") as f:
            # заведомо закрытый порт
            f.write("upstream=http://127.0.0.1:1\n")
        os.environ["PACER_CONF"] = conf
        os.environ["PACER_LOG"] = os.path.join(tmp.name, "pacer.log")
        os.environ["PACER_RPM"] = "3600"
        self.addCleanup(os.environ.pop, "PACER_CONF", None)
        self.addCleanup(os.environ.pop, "PACER_LOG", None)
        self.addCleanup(os.environ.pop, "PACER_RPM", None)

        ns = {"__name__": "pacer_down"}
        exec(compile(hp.PACER_SCRIPT, "pacer_proxy.py", "exec"), ns)
        server = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["_Handler"])
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/v1/x", headers={"Connection": "close"})
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            self.assertEqual(resp.status, 502)
            self.assertIn(b"pacer upstream error", data)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
