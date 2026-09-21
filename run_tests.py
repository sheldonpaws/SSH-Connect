# -*- coding: utf-8 -*-
"""Запуск всех тестов SSH-Connect на stdlib unittest (pytest не нужен).

Из корня репозитория:
    SSH_env\\Scripts\\python.exe run_tests.py
    SSH_env\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    suite = unittest.TestLoader().discover(
        os.path.join(ROOT, "tests"), top_level_dir=ROOT
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
