# -*- coding: utf-8 -*-
"""Тесты проверки host-ключей (src/hostkeys.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401
from _helpers import FakeKey, FakeParamikoClient  # noqa: E402

from src import hostkeys  # noqa: E402


class TestHostKeys(unittest.TestCase):
    def test_known_hosts_path(self):
        path = hostkeys.get_known_hosts_path()
        self.assertTrue(path.replace("\\", "/").endswith("SshHostKeys/known_hosts"))
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        self.assertTrue(os.path.exists(path))

    def test_policy_adds_key_and_saves(self):
        client = FakeParamikoClient()
        warnings = []
        policy = hostkeys.WarnOnNewRejectChangedPolicy(
            "C:/x/known_hosts", on_warning=warnings.append
        )
        key = FakeKey("ssh-ed25519")
        policy.missing_host_key(client, "example.com", key)

        self.assertIs(client._host_keys.data["example.com"]["ssh-ed25519"], key)
        self.assertEqual(client.saved, ["C:/x/known_hosts"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("example.com", warnings[0])
        self.assertIn("ssh-ed25519", warnings[0])

    def test_policy_without_callback(self):
        client = FakeParamikoClient()
        policy = hostkeys.WarnOnNewRejectChangedPolicy("p")
        policy.missing_host_key(client, "h", FakeKey())
        self.assertIn("h", client._host_keys.data)

    def test_prepare_host_keys(self):
        client = FakeParamikoClient()
        hostkeys.prepare_host_keys(client)
        self.assertEqual(client.loaded, [hostkeys.get_known_hosts_path()])
        self.assertIsInstance(client.policy, hostkeys.WarnOnNewRejectChangedPolicy)

    def test_prepare_host_keys_passes_callback(self):
        client = FakeParamikoClient()
        seen = []
        hostkeys.prepare_host_keys(client, on_warning=seen.append)
        client.policy.missing_host_key(client, "h", FakeKey())
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
