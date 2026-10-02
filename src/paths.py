# -*- coding: utf-8 -*-
"""
Единый источник путей приложения.

Раскладка (важна и для собранного exe):
  PROJECT_ROOT  — корень программы. В разработке — корень репозитория;
                  в собранном exe — папка, в которой лежит _internal
                  (вычисляется через sys._MEIPASS).
  USER_DATA_DIR — папка пользователя PROJECT_ROOT/user_data. Создаётся при
                  первом запуске (ensure_user_data). Внутри лежат
                  ssh_connections.json, last_profile.txt, SshHostKeys/,
                  Sessions/, Downloads/, move_cursor.json, move_cursor.log.
  PUTTY.RND и find_window_class.py — остаются в корне (PROJECT_ROOT), не
  в папке пользователя.

Папка пользователя переопределяется env-переменной SSH_CONNECT_USER_DATA
(используется в тестах и при ручном запуске).
"""

import os
import sys

DEFAULT_USER_DATA_NAME = "user_data"


def project_root() -> str:
    """Корень программы: рядом с _internal в собранном exe, иначе корень репо."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.dirname(os.path.abspath(meipass))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    """Путь к папке пользователя (env-переопределяемый)."""
    override = os.environ.get("SSH_CONNECT_USER_DATA")
    if override:
        return os.path.normpath(override)
    return os.path.join(project_root(), DEFAULT_USER_DATA_NAME)


def user_data_path(*parts) -> str:
    """Путь к файлу/подпапке внутри папки пользователя."""
    return os.path.join(user_data_dir(), *parts)


def ensure_user_data(*subdirs) -> bool:
    """Создать папку пользователя и подпапки (при первом запуске программы).

    Возвращает True, если папка пользователя создана впервые.
    """
    root = user_data_dir()
    first = not os.path.isdir(root)
    os.makedirs(root, exist_ok=True)
    for sub in subdirs:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return first