# -*- coding: utf-8 -*-
"""Тесты FileManager (web-desktop/backend/file_manager.py) на in-memory SFTP."""

import io
import stat
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND  # noqa: E402,F401
from _helpers import FakeSession, FakeSFTP, FakeSFTPClient  # noqa: E402

from file_manager import FileManager  # noqa: E402


class TestFileManager(unittest.TestCase):
    def setUp(self):
        self.client = FakeSFTPClient()
        self.fm = FileManager(FakeSession(self.client))

    def _seed(self, path, data=b"data"):
        self.client._sftp.files[path] = data

    def test_list_directory_dirs_first_sorted(self):
        self.client._sftp.dirs.add("/root")
        self._seed("/root/b.txt")
        self._seed("/root/a.txt")
        self.client._sftp.dirs.add("/root/zdir")
        self._seed("/root/zdir/inner", b"x")
        ok, entries, err = self.fm.list_directory("/root")
        self.assertTrue(ok, err)
        self.assertEqual([e["name"] for e in entries], ["zdir", "a.txt", "b.txt"])
        self.assertTrue(entries[0]["is_dir"])
        self.assertEqual(entries[0]["size"], 0)
        self.assertFalse(entries[1]["is_dir"])

    def test_read_file(self):
        self._seed("/f.txt", "привет".encode("utf-8"))
        ok, content, err = self.fm.read_file("/f.txt")
        self.assertTrue(ok, err)
        self.assertEqual(content, "привет")

    def test_read_file_too_big(self):
        self._seed("/big.txt", b"x" * 100)
        ok, content, err = self.fm.read_file("/big.txt", max_size=10)
        self.assertFalse(ok)
        self.assertIn("слишком большой", err)

    def test_read_missing(self):
        ok, _, err = self.fm.read_file("/nope.txt")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_save_then_read(self):
        ok, msg = self.fm.save_file("/new.txt", "hello")
        self.assertTrue(ok, msg)
        self.assertEqual(self.client._sftp.files["/new.txt"], b"hello")

    def test_delete_file(self):
        self._seed("/gone.txt")
        ok, msg = self.fm.delete("/gone.txt")
        self.assertTrue(ok, msg)
        self.assertNotIn("/gone.txt", self.client._sftp.files)

    def test_delete_dir_recursive(self):
        self.client._sftp.dirs.add("/d")
        self._seed("/d/a.txt", b"a")
        self.client._sftp.dirs.add("/d/sub")
        self._seed("/d/sub/b.txt", b"b")
        ok, msg = self.fm.delete("/d")
        self.assertTrue(ok, msg)
        self.assertNotIn("/d/a.txt", self.client._sftp.files)
        self.assertNotIn("/d/sub/b.txt", self.client._sftp.files)
        self.assertNotIn("/d", self.client._sftp.dirs)

    def test_rename(self):
        self._seed("/old.txt", b"z")
        ok, msg = self.fm.rename("/old.txt", "/new.txt")
        self.assertTrue(ok, msg)
        self.assertIn("/new.txt", self.client._sftp.files)
        self.assertNotIn("/old.txt", self.client._sftp.files)

    def test_mkdir(self):
        ok, msg = self.fm.mkdir("/x")
        self.assertTrue(ok, msg)
        self.assertIn("/x", self.client._sftp.dirs)

    def test_stat_path(self):
        self.client._sftp.dirs.add("/dir")
        self._seed("/file", b"12345")
        self.assertEqual(self.fm.stat_path("/dir"), (True, True, 0))
        self.assertEqual(self.fm.stat_path("/file"), (True, False, 5))
        self.assertEqual(self.fm.stat_path("/missing"), (False, False, 0))

    def test_download_info(self):
        self._seed("/a.bin", b"1234")
        ok, info, err = self.fm.download_info("/a.bin")
        self.assertTrue(ok, err)
        self.assertEqual(info["name"], "a.bin")
        self.assertEqual(info["size"], 4)
        self.assertFalse(info["is_dir"])

    def test_iter_download_chunks(self):
        data = bytes(range(256)) * 4
        self._seed("/big.bin", data)
        out = b"".join(self.fm.iter_download("/big.bin", chunk_size=100))
        self.assertEqual(out, data)

    def test_iter_tar_command_and_stream(self):
        self.client._tar_data = b"TARGZ"
        chunks = b"".join(self.fm.iter_tar("/home/user", "proj dir"))
        self.assertEqual(chunks, b"TARGZ")
        command, timeout = self.client.exec_calls[-1]
        self.assertIn("tar -czf -", command)
        self.assertIn("-C", command)
        self.assertIn("'proj dir'", command)
        self.assertIn("/home/user", command)

    def test_upload_creates_intermediate_dirs(self):
        source = io.BytesIO(b"A" * 150000)
        ok, msg = self.fm.upload_file("/deep/a/b/c.bin", source, chunk_size=64 * 1024)
        self.assertTrue(ok, msg)
        self.assertEqual(self.client._sftp.files["/deep/a/b/c.bin"], b"A" * 150000)
        self.assertIn("/deep", self.client._sftp.dirs)
        self.assertIn("/deep/a", self.client._sftp.dirs)
        self.assertIn("/deep/a/b", self.client._sftp.dirs)

    def test_upload_mkdirs_race_safe(self):
        sftp = self.client._sftp
        original_mkdir = sftp.mkdir

        def racing_mkdir(path):
            original_mkdir(path)
            raise IOError("already exists (parallel upload)")

        # первый stat скажет "нет" -> mkdir, которая после создания бросит IOError
        sftp.dirs.discard("/race")
        sftp.mkdir = racing_mkdir
        self.fm._mkdirs(sftp, "/race/sub")
        self.assertIn("/race", sftp.dirs)
        self.assertIn("/race/sub", sftp.dirs)

    def test_mkdirs_root_is_noop(self):
        self.fm._mkdirs(self.client._sftp, "/")
        self.assertEqual(self.client._sftp.dirs, {"/"})


if __name__ == "__main__":
    unittest.main()
