# -*- coding: utf-8 -*-
"""Тесты обратного доступа (src/reverse_access.py): слаги, монтирования, HTTP API."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

from src import reverse_access as ra  # noqa: E402


class TestMakeSlug(unittest.TestCase):
    def test_cyrillic_transliteration(self):
        self.assertEqual(ra.make_slug("Мои документы"), "moi-dokumenti")

    def test_simple_and_uppercase(self):
        self.assertEqual(ra.make_slug("Proj"), "proj")
        self.assertEqual(ra.make_slug("my folder"), "my-folder")

    def test_uses_basename_only(self):
        self.assertEqual(ra.make_slug("D:/Projects/proj"), "proj")
        self.assertEqual(ra.make_slug(r"D:\Projects\proj\\"), "proj")

    def test_empty_becomes_share(self):
        # пустой basename -> "folder", а "пустой после чистки" (только символы) -> "share"
        self.assertEqual(ra.make_slug(""), "share")
        self.assertEqual(ra.make_slug("///"), "folder")
        self.assertEqual(ra.make_slug("..."), "share")
        self.assertEqual(ra.make_slug("!!!"), "share")

    def test_uniqueness_with_used_set(self):
        used = set()
        self.assertEqual(ra.make_slug("proj", used), "proj")
        self.assertEqual(ra.make_slug("proj", used), "proj-2")
        self.assertEqual(ra.make_slug("proj", used), "proj-3")
        self.assertEqual(used, {"proj", "proj-2", "proj-3"})

    def test_fixed_port(self):
        self.assertEqual(ra.ReverseAccess.FIXED_PORT, 17850)


class TestReverseAccessObject(unittest.TestCase):
    def test_mounts_from_list(self):
        ra_obj = ra.ReverseAccess(["D:/Projects/Proj", "D:/Backups"])
        self.assertEqual(set(ra_obj.mounts), {"proj", "backups"})
        self.assertTrue(os.path.isabs(ra_obj.mounts["proj"]))

    def test_mounts_from_dict(self):
        ra_obj = ra.ReverseAccess({"my": "D:/x", "other": "D:/y"})
        self.assertEqual(set(ra_obj.mounts), {"my", "other"})

    def test_url_for(self):
        ra_obj = ra.ReverseAccess({"my": "D:/x"})
        ra_obj._vps_port = 17850
        self.assertEqual(ra_obj.vps_url, "http://127.0.0.1:17850/")
        self.assertEqual(ra_obj.url_for("my"), "http://127.0.0.1:17850/my/")

    def test_start_rejects_when_already_running(self):
        ra_obj = ra.ReverseAccess({"my": "D:/x"})
        ra_obj._running = True
        ok, msg = ra_obj.start(None)
        self.assertFalse(ok)
        self.assertIn("Уже", msg)


class TestReverseHttpServer(unittest.TestCase):
    """Поднимает реальный _ReverseServer на 127.0.0.1:0 и ходит по HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = cls.tmp.name
        os.makedirs(os.path.join(cls.root, "sub"))
        with open(os.path.join(cls.root, "a.txt"), "wb") as f:
            f.write(b"hello-bytes")
        # секрет вне корня монтирования — не должен быть доступен ни при каком обходе
        cls.secret_path = os.path.join(os.path.dirname(cls.root), "outside_secret.txt")
        with open(cls.secret_path, "w", encoding="utf-8") as f:
            f.write("TOPSECRET")

        cls.server = ra._ReverseServer({"share": cls.root})
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()
        try:
            os.remove(cls.secret_path)
        except OSError:
            pass

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Connection": "close"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_root_lists_mounts(self):
        status, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        payload = json.loads(data.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["type"], "dir")
        names = [e["name"] for e in payload["entries"]]
        self.assertIn("share", names)
        self.assertIn("/share/", [e["url"] for e in payload["entries"]])

    def test_directory_listing(self):
        status, data = self._request("GET", "/share/sub/")
        self.assertEqual(status, 200)
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(payload["type"], "dir")

    def test_read_file(self):
        status, data = self._request("GET", "/share/a.txt")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"hello-bytes")

    def test_put_creates_dirs_and_file(self):
        status, data = self._request("PUT", "/share/deep/nested/new.txt", b"written")
        self.assertEqual(status, 200)
        payload = json.loads(data.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bytes"], 7)
        with open(os.path.join(self.root, "deep", "nested", "new.txt"), "rb") as f:
            self.assertEqual(f.read(), b"written")

    def test_mkcol(self):
        status, data = self._request("MKCOL", "/share/brand-new-dir")
        self.assertEqual(status, 201)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "brand-new-dir")))

    def test_delete_file(self):
        target = os.path.join(self.root, "to-delete.txt")
        with open(target, "wb") as f:
            f.write(b"x")
        status, _ = self._request("DELETE", "/share/to-delete.txt")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(target))

    def test_delete_dir_recursive(self):
        target = os.path.join(self.root, "doomed")
        os.makedirs(target)
        with open(os.path.join(target, "f"), "wb") as f:
            f.write(b"x")
        status, _ = self._request("DELETE", "/share/doomed")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(target))

    def test_unknown_slug_404(self):
        status, _ = self._request("GET", "/nope/")
        self.assertEqual(status, 404)

    def test_missing_file_404(self):
        status, _ = self._request("GET", "/share/does-not-exist.txt")
        self.assertEqual(status, 404)

    def test_traversal_forbidden_plain(self):
        status, data = self._request("GET", "/share/../outside_secret.txt")
        self.assertEqual(status, 403)
        self.assertNotIn(b"TOPSECRET", data)

    def test_traversal_forbidden_encoded(self):
        status, data = self._request("GET", "/share/..%2f..%2foutside_secret.txt")
        self.assertEqual(status, 403)
        self.assertNotIn(b"TOPSECRET", data)

    def test_put_traversal_forbidden(self):
        # тело не шлём: отказ приходит до чтения body (иначе сервер может
        # закрыть соединение с непрочитанным телом и отдать RST)
        status, _ = self._request("PUT", "/share/../evil.txt")
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "evil.txt")))

    def test_put_on_root_rejected(self):
        status, _ = self._request("PUT", "/")
        self.assertEqual(status, 400)

    def test_mkcol_on_root_rejected(self):
        status, _ = self._request("MKCOL", "/")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
