# -*- coding: utf-8 -*-
"""Единый источник истины: src/hermes_skills.py должен совпадать с
skills/reverse-ssh-tunnel.md через skills/gen_hermes_skills.py.

Ловит ручные правки сгенерированного файла и устаревший шаблон генератора.
"""

import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

GEN_PATH = os.path.join(ROOT, "skills", "gen_hermes_skills.py")
OUT_PATH = os.path.join(ROOT, "src", "hermes_skills.py")


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_hermes_skills", GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGeneratorSync(unittest.TestCase):
    def test_generated_file_matches_master(self):
        gen = _load_gen()
        master = gen.load_master()
        expected = gen.build_source(master)
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            current = f.read()
        self.assertEqual(
            current,
            expected,
            "src/hermes_skills.py разошёлся с skills/reverse-ssh-tunnel.md — "
            "запусти: python skills/gen_hermes_skills.py",
        )

    def test_escapes_backslashes(self):
        gen = _load_gen()
        src = gen.build_source("path C:\\Users\\x")
        # в сгенерированном исходнике обратные слэши удвоены
        self.assertIn("C:\\\\Users\\\\x", src)


if __name__ == "__main__":
    unittest.main()
