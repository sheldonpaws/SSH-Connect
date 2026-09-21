# -*- coding: utf-8 -*-
"""Тесты hermes_manager: сборка команд (инъекции) и парсеры вывода."""

import base64
import os
import re
import shlex
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND  # noqa: E402,F401
from _helpers import FakeSSH  # noqa: E402

import hermes_manager as hm  # noqa: E402


def _inner(cmd):
    """Развернуть 'bash -lc <quoted>' -> исходная внутренняя строка."""
    assert cmd.startswith("bash -lc ")
    return shlex.split(cmd[len("bash -lc "):])[0]


class TestBuildChatCommand(unittest.TestCase):
    def test_base(self):
        cmd = hm.build_chat_command("hello")
        inner = _inner(cmd)
        self.assertIn("NO_COLOR=1", inner)
        self.assertIn("hermes chat", inner)
        self.assertIn("-Q", inner)
        tokens = shlex.split(inner)
        self.assertEqual(tokens[tokens.index("-q") + 1], "hello")

    def test_message_with_spaces_and_quotes_not_injected(self):
        msg = 'he"llo \'world\' ; rm -rf /'
        inner = _inner(hm.build_chat_command(msg))
        tokens = shlex.split(inner)
        self.assertEqual(tokens[tokens.index("-q") + 1], msg)

    def test_all_flags(self):
        inner = _inner(
            hm.build_chat_command(
                "m", resume="20260821_133032_62b770", model="laguna-s-2.1-free",
                skills="reverse-ssh-tunnel", toolsets="web", yolo=True,
            )
        )
        self.assertIn("--resume", inner)
        self.assertIn("20260821_133032_62b770", inner)
        self.assertIn("-m", inner)
        self.assertIn("laguna-s-2.1-free", inner)
        self.assertIn("-s", inner)
        self.assertIn("-t", inner)
        self.assertIn("--yolo", inner)

    def test_skills_list_joined(self):
        cmd = hm.build_chat_command("m", skills=["a", "b", ""])
        self.assertIn("a,b", _inner(cmd))

    def test_empty_skills_ignored(self):
        self.assertNotIn("-s", _inner(hm.build_chat_command("m", skills="  ")))


class TestBuildDetachedChatCommand(unittest.TestCase):
    def test_shape_and_log_path(self):
        cmd, log = hm.build_detached_chat_command("abc123", "hi")
        self.assertTrue(cmd.startswith("bash -lc "))
        self.assertTrue(log.endswith(".hermes/jobs/hj_abc123.log"))
        inner = _inner(cmd)
        self.assertIn("nohup", inner)
        self.assertIn("__START__", inner)
        self.assertIn("__RC__=$?", inner)
        self.assertIn("PID=$!", inner)
        self.assertIn("--query-file", inner)

    def test_message_base64_roundtrip(self):
        msg = "привет; rm -rf / && echo 'x'"
        cmd, _ = hm.build_detached_chat_command("j1", msg)
        self.assertNotIn(msg, cmd)
        m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", _inner(cmd))
        self.assertIsNotNone(m)
        self.assertEqual(base64.b64decode(m.group(1)).decode("utf-8"), msg)

    def test_flags_quoted(self):
        cmd, _ = hm.build_detached_chat_command(
            "j2", "m", resume="sid", model="m1", skills="sk", yolo=True
        )
        inner = _inner(cmd)
        self.assertIn("--resume sid", inner)
        self.assertIn("-m m1", inner)
        self.assertIn("-s sk", inner)
        self.assertIn("--yolo", inner)


class TestOtherCommands(unittest.TestCase):
    def test_list_command(self):
        self.assertIn("hermes skills list", _inner(hm.build_list_command("skills", "list")))

    def test_export_command(self):
        inner = _inner(hm.build_export_command("20260821_133032_62b770"))
        self.assertIn("sessions export", inner)
        self.assertIn("--session-id", inner)
        self.assertIn("jsonl", inner)

    def test_delete_session_command(self):
        inner = _inner(hm.build_delete_session_command("sid"))
        self.assertIn("sessions delete", inner)
        self.assertIn("--yes", inner)


class TestExtractSessionId(unittest.TestCase):
    def test_session_line(self):
        text = "session_id: 20260821_133032_62b770\nответ..."
        self.assertEqual(hm.extract_session_id(text), "20260821_133032_62b770")

    def test_uuid_fallback(self):
        text = "no session line 11111111-2222-3333-4444-555555555555 end"
        self.assertEqual(
            hm.extract_session_id(text), "11111111-2222-3333-4444-555555555555"
        )

    def test_none(self):
        self.assertIsNone(hm.extract_session_id(""))
        self.assertIsNone(hm.extract_session_id("just text"))


class TestParseSessionJsonl(unittest.TestCase):
    def test_single_object_nested_messages(self):
        text = ('{"session_id": "x", "messages": ['
                '{"role": "user", "content": "привет"},'
                '{"role": "assistant", "content": [{"type": "text", "text": "здравствуй"}]}'
                ']}')
        msgs = hm.parse_session_jsonl(text)
        self.assertEqual(msgs, [
            {"role": "user", "text": "привет"},
            {"role": "assistant", "text": "здравствуй"},
        ])

    def test_line_delimited(self):
        text = ('{"role": "user", "content": "q"}\n'
                '{"role": "assistant", "content": "a"}\n')
        self.assertEqual(hm.parse_session_jsonl(text), [
            {"role": "user", "text": "q"},
            {"role": "assistant", "text": "a"},
        ])

    def test_role_aliases(self):
        text = '{"messages":[{"role":"human","content":"h"},{"role":"ai","content":"b"}]}'
        msgs = hm.parse_session_jsonl(text)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])

    def test_tool_noise_skipped(self):
        text = ('{"messages":[{"role":"assistant","content":'
                '[{"type":"tool_use","text":"rm -rf /"}]},'
                '{"role":"user","content":"real"}]}')
        msgs = hm.parse_session_jsonl(text)
        self.assertEqual(msgs, [{"role": "user", "text": "real"}])

    def test_non_json_ignored(self):
        self.assertEqual(hm.parse_session_jsonl("not json at all"), [])

    def test_max_messages_keeps_tail(self):
        lines = "\n".join(
            '{"role":"user","content":"m%d"}' % i for i in range(10)
        )
        msgs = hm.parse_session_jsonl(lines, max_messages=3)
        self.assertEqual([m["text"] for m in msgs], ["m7", "m8", "m9"])


class TestTables(unittest.TestCase):
    def test_parse_rich_table_heavy_and_light(self):
        text = (
            "┏━━━━━━┳━━━━━━━━┓\n"
            "┃ ID   ┃ Title  ┃\n"
            "┡━━━━━━╇━━━━━━━━┩\n"
            "│ 1    │ first  │\n"
            "│ 2    │ second │\n"
            "└──────┴────────┘\n"
        )
        rows = hm.parse_rich_table(text)
        self.assertEqual(rows, [
            {"ID": "1", "Title": "first"},
            {"ID": "2", "Title": "second"},
        ])

    def test_parse_rich_table_empty(self):
        self.assertEqual(hm.parse_rich_table(""), [])

    def test_parse_sessions_monospace(self):
        text = (
            "Title                 Workspace   Last Active   ID\n"
            "-------------------   ---------   -----------   ----------------------\n"
            "Hello world           proj        2h ago        20260821_133032_62b770\n"
        )
        rows = hm.parse_sessions_table(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ID"], "20260821_133032_62b770")
        self.assertEqual(rows[0]["Title"], "Hello world")

    def test_parse_sessions_rich_variant(self):
        text = (
            "┃ Title ┃ Workspace ┃ Last Active ┃ ID ┃\n"
            "│ Hi    │ proj      │ 1h ago      │ 20260821_133032_62b770 │\n"
        )
        rows = hm.parse_sessions_table(text)
        self.assertEqual(rows[0]["Title"], "Hi")

    def test_parse_sessions_empty(self):
        self.assertEqual(hm.parse_sessions_table("nothing here"), [])

    def test_parse_skills_descriptions(self):
        text = "reverse-ssh-tunnel\ttunnel diagnostics\nother\tdesc here\nbad no tab\n"
        self.assertEqual(hm.parse_skills_descriptions(text), {
            "reverse-ssh-tunnel": "tunnel diagnostics",
            "other": "desc here",
        })

    def test_parse_config_read_output(self):
        text = (
            "MODEL:\nlaguna-s-2.1-free\n"
            "PROVIDER:\nopencode-free\n"
            "BASE_URL:\nhttp://127.0.0.1:17901\n"
            "FALLBACK_MODEL:\n"
            "FALLBACK_PROVIDER:\n"
            "KEYS:\nGEMINI_API_KEY:SET\nOPENROUTER_API_KEY:MISSING\n"
        )
        st = hm.parse_config_read_output(text)
        self.assertEqual(st["model"], "laguna-s-2.1-free")
        self.assertEqual(st["provider"], "opencode-free")
        self.assertEqual(st["base_url"], "http://127.0.0.1:17901")
        self.assertTrue(st["keys_set"]["GEMINI_API_KEY"])
        self.assertFalse(st["keys_set"]["OPENROUTER_API_KEY"])


class TestConfigCommands(unittest.TestCase):
    def test_save_empty_returns_none(self):
        self.assertIsNone(hm.build_config_save_command())
        self.assertIsNone(hm.build_config_save_command(model="", provider="  "))

    def test_save_quotes_values(self):
        cmd = hm.build_config_save_command(model="my model", base_url="http://x/y")
        self.assertTrue(cmd.startswith("bash -lc "))
        self.assertIn("config set", cmd)
        self.assertIn("my model", _inner(cmd))

    def test_env_key_rejects_newline(self):
        self.assertIsNone(hm.build_env_key_command("OPENROUTER_API_KEY", "a\nb"))
        self.assertIsNone(hm.build_env_key_command("X", "a\rb"))
        self.assertIsNone(hm.build_env_key_command("X", "a\x00b"))

    def test_env_key_sanitizes_var_and_protects_file(self):
        cmd = hm.build_env_key_command("openrouter-api key!", "secret-value")
        inner = _inner(cmd)
        self.assertIn("OPENROUTERAPIKEY", inner)
        self.assertIn("chmod 600", inner)
        self.assertIn("grep -q", inner)

    def test_env_checks_mentions_all_providers(self):
        checks = hm._env_checks()
        for var in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            self.assertIn(var, checks)

    def test_config_read_command_shape(self):
        inner = _inner(hm.build_config_read_command())
        for marker in ("MODEL:", "PROVIDER:", "BASE_URL:", "KEYS:"):
            self.assertIn(marker, inner)


class TestHermesJob(unittest.TestCase):
    def test_chunk_since(self):
        job = hm.HermesJob("j", "s", "cmd", "$HOME/log")
        job.buffer = "hello world"
        chunk = job.chunk_since(6)
        self.assertEqual(chunk["chunk"], "world")
        self.assertEqual(chunk["offset"], 11)
        self.assertFalse(chunk["done"])

    def test_stop(self):
        job = hm.HermesJob("j", "s", "cmd", "log")
        self.assertFalse(job.stopped)
        job.stop()
        self.assertTrue(job.stopped)

    def test_manager_one_active_per_session(self):
        original = hm.HermesJob._watch
        hm.HermesJob._watch = lambda self, client: None
        try:
            mgr = hm.HermesJobManager()
            ssh = FakeSSH()
            jid, busy = mgr.start("j1", "sess", "cmd", "log", ssh)
            self.assertEqual(jid, "j1")
            self.assertIsNone(busy)
            jid2, busy2 = mgr.start("j2", "sess", "cmd", "log", ssh)
            self.assertIsNone(jid2)
            self.assertIsNotNone(busy2)
            self.assertIs(mgr.get("j1"), busy2)
        finally:
            hm.HermesJob._watch = original

    def test_manager_drop_and_stop(self):
        mgr = hm.HermesJobManager()
        self.assertFalse(mgr.stop_active("nope"))
        self.assertIsNone(mgr.get("nope"))
        mgr.drop_web_session("nope")
        self.assertGreaterEqual(hm.HermesJobManager.MAX_JOBS, 1)


if __name__ == "__main__":
    unittest.main()
