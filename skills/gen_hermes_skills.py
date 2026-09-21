"""Генератор: собирает src/hermes_skills.py из мастер-скилла skills/reverse-ssh-tunnel.md.

ЕДИНЫЙ источник истины для содержимого скилла — файл skills/reverse-ssh-tunnel.md.
Никогда не правь строку REVERSE_SSH_TUNNEL_SKILL вручную: она пересобирается отсюда.
Это убирает ручную синхронизацию между скиллом на VPS и его копией в коде, о которой
говорил AGENTS.md («mirrored manually against the VPS copy»).

Запуск (из корня репозитория SSH-Connect):
    python skills/gen_hermes_skills.py [--check]

--check — не перезаписывать файл, а только проверить, совпадает ли текущий
src/hermes_skills.py с тем, что должно получиться (exit 0 = совпадает,
exit 1 = расходится). Удобно в pre-commit / CI.

Шаблон генерируемого файла держим здесь, внизу (SKILL_FILE_TEMPLATE), чтобы весь
src/hermes_skills.py был производным артефактом одного файла-генератора.
"""

from __future__ import annotations

import argparse
import os
import sys

# Пути относительно корня репозитория (родитель каталога skills/ по этому файлу).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = os.path.join(REPO_ROOT, "skills", "reverse-ssh-tunnel.md")
OUT_PATH = os.path.join(REPO_ROOT, "src", "hermes_skills.py")


def load_master() -> str:
    """Прочитать мастер-скилл из skills/reverse-ssh-tunnel.md."""
    if not os.path.exists(MASTER_PATH):
        raise SystemExit("Мастер-скилл не найден: {}".format(MASTER_PATH))
    with open(MASTER_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Шаблон генерируемого src/hermes_skills.py. Плейсхолдер {SKILL_CONTENT}
# подставляется ниже из skills/reverse-ssh-tunnel.md. Этот шаблон — ОБВЯЗКА
# (docstring, путь, функции установки), она не должна меняться вручную в
# сгенерированном файле — правь её здесь и запусти генератор заново.
# ---------------------------------------------------------------------------
SKILL_FILE_TEMPLATE = '''"""
Скилы Hermes, которые SSH-Connect ставит на VPS автоматически.

Файл генерируется из skills/reverse-ssh-tunnel.md скриптом
skills/gen_hermes_skills.py — НЕ редактируй его руками. Содержимое скилла живёт
в skills/reverse-ssh-tunnel.md (единый источник), а установка идёт по SSH при
каждом успешном подключении (см. SSHApp._ensure_reverse_skill): если файла на
VPS нет — он создаётся. Категория devops уже существует у встроенных скилов,
поэтому простой записи файла достаточно, чтобы Hermes увидел его как
local+enabled (в новой категории скил НЕ подхватывается — проверено).
"""

# ~/.hermes/skills/devops/reverse-ssh-tunnel/SKILL.md — доступ агента к папкам
# Windows через обратный туннель 127.0.0.1:17850 (см. src/reverse_access.py).
REVERSE_SSH_TUNNEL_SKILL = """{SKILL_CONTENT}"""

# Проверка наличия: путь к SKILL.md внутри ~/.hermes.skills у текущего юзера
HERMES_SKILL_REMOTE_REL = "devops/reverse-ssh-tunnel/SKILL.md"
HERMES_SKILL_REMOTE_PATH = ("$HOME/.hermes/skills/" + HERMES_SKILL_REMOTE_REL)


def install_skill_if_missing(ssh, on_log=None):
    """Поставить скил Hermes 'reverse-ssh-tunnel' на VPS, если его ещё нет.

    Возвращает одно из трёх состояний (str):
        'already_present' — скилл уже был установлен
        'installed'       — скилл был установлен в этой сессии
        'hermes_missing'  — Hermes не установлен на сервере (ничего не делаем)

    ssh — src.ssh.SSHClient (уже подключён). Ничего не делает, если ssh
    не подключён.
    """
    log = on_log or (lambda *a, **k: None)
    if ssh is None or not getattr(ssh, "is_connected", False):
        return 'hermes_missing'
    try:
        # hermes установлен? (не на PATH нема_nonlogin; проверяем через bash -lc)
        ok, _ = ssh.execute_command(
            "bash -lc 'command -v hermes >/dev/null 2>&1 && echo YES || echo NO'")
        if not ok or "YES" not in (_ or ""):
            return 'hermes_missing'
        # уже есть?
        ok, out = ssh.execute_command(
            "bash -lc 'test -f {} && echo YES || echo NO'".format(
                HERMES_SKILL_REMOTE_PATH))
        if ok and (out or "").strip().endswith("YES"):
            return 'already_present'
        # нет — создаём (base64, без проблем с кавычками/кириллицей)
        import base64 as _b64
        payload = _b64.b64encode(REVERSE_SSH_TUNNEL_SKILL.encode("utf-8")).decode()
        cmd = ("mkdir -p \\"$HOME/.hermes/skills/devops/reverse-ssh-tunnel\\" && "
               "echo {} | base64 -d > {}"
               ).format(payload, HERMES_SKILL_REMOTE_PATH)
        ok2, out2 = ssh.execute_command("bash -lc " + _shquote(cmd))
        if not ok2:
            log("⚠️ Не удалось поставить скил Hermes: {}".format(out2), "warning")
            return 'hermes_missing'
        log("🛠 Скил Hermes 'reverse-ssh-tunnel' установлен на VPS", "success")
        return 'installed'
    except Exception as e:
        log("⚠️ Ошибка установки скила Hermes: {}".format(e), "warning")
        return 'hermes_missing'


def _shquote(s):
    import shlex
    return shlex.quote(s)
'''


def build_source(master: str) -> str:
    """Собрать полный текст src/hermes_skills.py из мастер-скилла."""
    escaped = master.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    # .replace (не .format) — в шаблоне остаются фигурные скобки нужные
    # генерируемому коду (cmd.format(...)); .format споткнулся бы о них.
    return SKILL_FILE_TEMPLATE.replace("{SKILL_CONTENT}", escaped)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="сравнить, но не перезаписывать")
    args = ap.parse_args()

    master = load_master()
    new_source = build_source(master)

    exists = os.path.exists(OUT_PATH)
    if exists:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            cur = f.read()
    else:
        cur = ""

    if cur == new_source:
        print("OK: src/hermes_skills.py уже актуален (совпадает с мастером).")
        return 0

    if args.check:
        print("РАСХОЖДЕНИЕ: src/hermes_skills.py устарел относительно "
              "skills/reverse-ssh-tunnel.md. Запусти: python skills/gen_hermes_skills.py")
        return 1

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(new_source)
    print("Готово: src/hermes_skills.py пересобран из "
          "skills/reverse-ssh-tunnel.md ({} байт).".format(len(new_source)))
    return 0


if __name__ == "__main__":
    sys.exit(main())