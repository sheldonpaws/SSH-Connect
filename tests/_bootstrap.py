# -*- coding: utf-8 -*-
"""Подготовка sys.path для тестов.

Тесты работают с тремя корнями:
  - ROOT    — корень репозитория (пакет src, move_cursor.py, skills/);
  - BACKEND — web-desktop/backend (плоские импорты main/ssh_manager/...);
  - TESTS   — каталог tests (этот файл + _helpers).

Импортируется в начале каждого тест-модуля:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _bootstrap import ROOT, BACKEND  # noqa: E402
"""

import os
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
BACKEND = os.path.join(ROOT, "web-desktop", "backend")

# Тесты пишут runtime-файлы (ssh_connections.json, known_hosts, move_cursor.*)
# во временную папку пользователя, а не в репозиторий.
os.environ.setdefault(
    "SSH_CONNECT_USER_DATA",
    tempfile.mkdtemp(prefix="sshconnect_user_data_"),
)

for _p in (TESTS, ROOT, BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)
