# -*- coding: utf-8 -*-
"""Тесты src/ssh.py — только оффлайн-пути (fake/мок, без реального VPS)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

import paramiko  # noqa: E402
from src.ssh import SSHClient  # noqa: E402


class FakeKey:
    def get_name(self):
        return "ssh-ed25519"


class TestSSHClient(unittest.TestCase):
    def setUp(self):
        self.ssh = SSHClient()
        self.patch_policy = mock.patch(
            "src.hostkeys.prepare_host_keys", return_value=None)
        self.patch_policy.start()

    def tearDown(self):
        self.patch_policy.stop()

    def test_changed_host_key_returns_russian_warning(self):
        with mock.patch.object(
                paramiko.SSHClient, "connect",
                side_effect=paramiko.ssh_exception.BadHostKeyException(
                    "myserver", FakeKey(), FakeKey())):
            ok, msg = self.ssh.connect(hostname="myserver", port=22,
                                       username="root", password="x")
        self.assertFalse(ok)
        self.assertIn("Host-key для myserver изменился", msg)
        self.assertIn("MITM", msg)

    def test_other_error_passthrough(self):
        with mock.patch.object(
                paramiko.SSHClient, "connect",
                side_effect=OSError("connection refused")):
            ok, msg = self.ssh.connect(hostname="h", port=22,
                                       username="u", password="p")
        self.assertFalse(ok)
        self.assertEqual(msg, "connection refused")


if __name__ == "__main__":
    unittest.main()