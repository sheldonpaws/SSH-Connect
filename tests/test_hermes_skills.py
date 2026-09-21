# -*- coding: utf-8 -*-
"""Тесты src/hermes_skills.py: содержимое скилла и установка по SSH."""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401
from _helpers import FakeSSH  # noqa: E402

from src import hermes_skills as hs  # noqa: E402


class TestSkillContent(unittest.TestCase):
    def test_markers(self):
        skill = hs.REVERSE_SSH_TUNNEL_SKILL
        self.assertIn("name: reverse-ssh-tunnel", skill)
        self.assertIn("category: devops", skill)
        self.assertIn("17850", skill)
        self.assertIn("do NOT touch sshd", skill)
        self.assertIn("reverse_access.json", skill)

    def test_remote_path(self):
        self.assertEqual(
            hs.HERMES_SKILL_REMOTE_PATH,
            "$HOME/.hermes/skills/devops/reverse-ssh-tunnel/SKILL.md",
        )


class _RaisingSSH(FakeSSH):
    def execute_command(self, command):
        raise RuntimeError("network down")


class TestInstallSkill(unittest.TestCase):
    def test_none_ssh(self):
        self.assertEqual(hs.install_skill_if_missing(None), "hermes_missing")

    def test_not_connected(self):
        self.assertEqual(
            hs.install_skill_if_missing(FakeSSH(is_connected=False)),
            "hermes_missing",
        )

    def test_hermes_absent(self):
        ssh = FakeSSH(responses=[(True, "NO")])
        self.assertEqual(hs.install_skill_if_missing(ssh), "hermes_missing")
        self.assertEqual(len(ssh.commands), 1)

    def test_already_present(self):
        ssh = FakeSSH(responses=[(True, "YES"), (True, "YES\n")])
        self.assertEqual(hs.install_skill_if_missing(ssh), "already_present")
        self.assertEqual(len(ssh.commands), 2)

    def test_installed(self):
        logs = []
        ssh = FakeSSH(responses=[(True, "YES"), (True, "NO"), (True, "")])
        state = hs.install_skill_if_missing(
            ssh, on_log=lambda msg, level: logs.append((msg, level))
        )
        self.assertEqual(state, "installed")
        self.assertEqual(len(ssh.commands), 3)
        payload = base64.b64encode(
            hs.REVERSE_SSH_TUNNEL_SKILL.encode("utf-8")
        ).decode("ascii")
        self.assertIn(payload, ssh.commands[2])
        self.assertTrue(any(level == "success" for _, level in logs))

    def test_install_failure(self):
        logs = []
        ssh = FakeSSH(responses=[(True, "YES"), (True, "NO"), (False, "boom")])
        state = hs.install_skill_if_missing(
            ssh, on_log=lambda msg, level: logs.append((msg, level))
        )
        self.assertEqual(state, "hermes_missing")
        self.assertTrue(any(level == "warning" for _, level in logs))

    def test_exception_is_swallowed(self):
        self.assertEqual(
            hs.install_skill_if_missing(_RaisingSSH()), "hermes_missing"
        )


if __name__ == "__main__":
    unittest.main()
