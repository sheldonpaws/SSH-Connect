"""
Интеграция с Hermes Agent (Nous Research) на удалённом сервере.

Агент запускается на VPS через его собственный CLI:
  hermes chat -q "сообщение" -Q [--resume ID] [-s SKILLS] [-m MODEL] [--yolo]
  hermes sessions list
  hermes skills list

Каждое сообщение пользователя — отдельный процесс на сервере; вывод читается
инкрементально (стриминг в браузер через поллинг джоб). Команды собираются
через shlex.quote — инъекции исключены.
"""

import json
import re
import shlex
import threading
import time
import uuid
import base64
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    yaml = None


# Тихий режим: без цвета/анимации, только финальный ответ + инфо о сессии
_HERMES_ENV = "NO_COLOR=1 TERM=dumb LC_ALL=C.UTF-8"


def build_chat_command(message, resume=None, model=None, skills=None,
                       toolsets=None, yolo=False):
    """Собрать bash-команду one-shot запроса к hermes chat.

    skills: список имён или строка через запятую. resume: id сессии.
    """
    parts = ["hermes", "chat", "-q", message, "-Q"]
    if resume:
        parts += ["--resume", str(resume)]
    if model:
        parts += ["-m", str(model)]
    if skills:
        if isinstance(skills, (list, tuple)):
            skills = ",".join(s for s in skills if s)
        if skills.strip():
            parts += ["-s", skills.strip()]
    if toolsets:
        parts += ["-t", str(toolsets)]
    if yolo:
        parts += ["--yolo"]
    # shlex.join экранирует КАЖДЫЙ аргумент отдельно (пробелы/кавычки в сообщении)
    inner = _HERMES_ENV + " " + shlex.join(parts)
    return "bash -lc " + shlex.quote(inner)


def build_detached_chat_command(job_id, message, resume=None, model=None,
                                skills=None, toolsets=None, yolo=False):
    """Запустить `hermes chat` на сервере как фоновую (detached) задачу.

    Задача НЕ привязана к SSH-каналу и веб-сессии: `nohup ... &` отвязывает
    процесс от канала, вывод идёт редиректом в ~/.hermes/jobs/hj_<job_id>.log,
    запрос — в ~/.hermes/jobs/hj_<job_id>.q (base64, безопасно от любых
    символов). По завершении inner печатает маркер __RC__=<код> в лог.

    Возвращает (bash-команда для exec_command, путь_к_логу).
    """
    jid = str(job_id)
    inner = (
        'mkdir -p "$HOME/.hermes/jobs" && '
        'L="$HOME/.hermes/jobs/hj_' + jid + '" && '
        'echo ' + base64.b64encode(message.encode("utf-8")).decode() + ' | base64 -d > "$L.q" && '
        'echo __START__ > "$L.log" && '
        'hermes chat --query-file "$L.q" -Q'
    )
    if resume:
        inner += " --resume " + shlex.quote(str(resume))
    if model:
        inner += " -m " + shlex.quote(str(model))
    if skills:
        if isinstance(skills, (list, tuple)):
            skills = ",".join(s for s in skills if s)
        if skills.strip():
            inner += " -s " + shlex.quote(skills.strip())
    if toolsets:
        inner += " -t " + shlex.quote(str(toolsets))
    if yolo:
        inner += " --yolo"
    inner += ' >> "$L.log" 2>&1; echo "__RC__=$?" >> "$L.log"'
    launcher = "nohup bash -lc " + shlex.quote(inner) + " < /dev/null > /dev/null 2>&1 & echo PID=$!"
    return "bash -lc " + shlex.quote(launcher), "$HOME/.hermes/jobs/hj_" + jid + ".log"


def build_list_command(subcmd, args=""):
    """Команда вида: hermes sessions list / hermes skills list."""
    inner = f"{_HERMES_ENV} hermes {subcmd} {args}".strip()
    return "bash -lc " + shlex.quote(inner)


def build_export_command(sid):
    """Выгрузка переписки сессии в JSONL на stdout (`hermes sessions export`)."""
    inner = f"{_HERMES_ENV} hermes sessions export --session-id {shlex.quote(sid)} --format jsonl -"
    return "bash -lc " + shlex.quote(inner)


def build_delete_session_command(sid):
    """Удаление сессии (`hermes sessions delete --yes <id>`).

    --yes пропускает интерактивное подтверждение (CLI вызывается из API).
    Несуществующий id печатает 'Session ... not found.' и не зависает.
    """
    inner = f"{_HERMES_ENV} hermes sessions delete --yes {shlex.quote(str(sid))}"
    return "bash -lc " + shlex.quote(inner)


def build_skills_descriptions_command():
    """Собрать name<TAB>description для ВСЕХ SKILL.md в один bash-вызов.

    Описания живут в front-matter каждого установленного скилла
    ($HOME/.hermes/skills/<Категория>/<Имя>/SKILL.md) и в `hermes skills list`
    не попадают. Один вызов вместо ~80 чтений файлов по отдельности.

    Скрипт печатается base64 и выполняется через `base64 -d | bash`, чтобы
    `shlex.quote` не ломал внутренние кавычки (внутри '...' двойные кавычки
    bash не интерпретировал бы, а одинарные закрыли бы строку).
    """
    script = (
        'cd "$HOME/.hermes/skills"\n'
        ': > /tmp/.hermes_skills_desc.$$\n'
        'while IFS= read -r f; do\n'
        '  n=$(head -30 "$f" | grep -m1 \'^name:\' | sed "s/^name:[[:space:]]*//;s/\\"//")\n'
        '  d=$(head -30 "$f" | grep -m1 \'^description:\' | sed "s/^description:[[:space:]]*//;s/^\\"//;s/\\"$//")\n'
        '  [ -n "$n" ] && printf "%s\\t%s\\n" "$n" "$d" >> /tmp/.hermes_skills_desc.$$\n'
        'done < <(find . -name SKILL.md)\n'
        'cat /tmp/.hermes_skills_desc.$$; rm -f /tmp/.hermes_skills_desc.$$'
    )
    b64 = base64.b64encode(script.encode('utf-8')).decode('ascii')
    return 'echo ' + b64 + ' | base64 -d | bash'


def parse_skills_descriptions(text):
    """Разобрать name<TAB>description -> {имя: описание}."""
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        if '\t' in line:
            name, _, desc = line.partition('\t')
            if name and desc:
                out[name.strip()] = desc.strip()
    return out


def translate_text(text, target='ru', source='en'):
    """Перевести фразу en->ru через бесплатный google translate endpoint.

    Возвращает переведённую строку (при ошибках — оригинал). Никаких ключей
    не нужно, сервер приложения ходит наружу напрямую (urllib).
    """
    text = (text or '').strip()
    if not text:
        return ''
    try:
        q = urllib.parse.quote(text)
        url = f'https://translate.googleapis.com/translate_a/single?client=gtx&sl={source}&tl={target}&dt=t&q={q}'
        with urllib.request.urlopen(url, timeout=12) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
        parts = []
        for seg in (data or [None])[0] or []:
            if seg and seg[0]:
                parts.append(seg[0])
        res = ''.join(parts).strip()
        return res or text
    except Exception:
        return text


def translate_skills_pack(descs, chunk=20):
    """Перевести словарь {имя: описание} целиком, объединяя в пакеты.

    Google endpoint переводит весь текст за раз с переносами, поэтому строки
    склеиваются по \n, переводятся пачкой и разрезаются обратно по позициям.
    При ошибках/рассинхроне остаются оригиналы.
    """
    out = {}
    items = [(k, v) for k, v in (descs or {}).items() if v]
    for i in range(0, len(items), chunk):
        batch = items[i:i + chunk]
        joined = '\n'.join(v for _, v in batch)
        raw = translate_text(joined)
        lines = raw.split('\n')
        if len(lines) == len(batch):
            for (k, _), line in zip(batch, lines):
                out[k] = (line or '').strip()
        else:
            # google переставил/объединил строки — не рискуем, оставляем как есть
            for k, _ in batch:
                out[k] = ''
    for k, v in items:
        if not out.get(k):
            out[k] = v
    return out


_PROVIDER_ENV_VARS = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "nous": "NOUS_API_KEY",
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _env_checks():
    """Строки проверки наличия непустого ключа в ~/.hermes/.env.

    Никогда не печатаем значения — только имя:SET или имя:MISSING.
    """
    env = '"$HOME/.hermes/.env"'
    parts = []
    for name in sorted(_PROVIDER_ENV_VARS.values()):
        parts.append(
            f'grep -q "^[[:space:]]*{name}=.." {env} '
            f'&& echo "{name}:SET" || echo "{name}:MISSING"'
        )
    return " ; ".join(parts)


def build_config_read_command():
    """Команда чтения текущей модели/провайдера и состояния ключей.

    Читает ~/.hermes/config.yaml через `hermes config get` и проверяет,
    какие ключи *_API_KEY прописаны в ~/.hermes/.env с непустым значением.
    Возвращает внешнюю bash-команду для exec_command.
    """
    script = "\n".join([
        'echo "MODEL:"',
        'hermes config get model.default 2>/dev/null',
        'echo "PROVIDER:"',
        'hermes config get model.provider 2>/dev/null',
        'echo "BASE_URL:"',
        'hermes config get model.base_url 2>/dev/null',
        'echo "FALLBACK_MODEL:"',
        'hermes config get fallback_model.model 2>/dev/null',
        'echo "FALLBACK_PROVIDER:"',
        'hermes config get fallback_model.provider 2>/dev/null',
        'echo "KEYS:"',
        _env_checks(),
    ])
    return "bash -lc " + shlex.quote(_HERMES_ENV + " ; " + script)


def parse_config_read_output(text):
    """Разобрать вывод build_config_read_command в словарь состояния.

    Возвращает:
        {"model": str, "provider": str, "base_url": str,
         "fallback_model": str, "fallback_provider": str,
         "keys_set": {"OPENROUTER_API_KEY": bool, ...}, "raw": str}
    """
    state = {
        "model": "", "provider": "", "base_url": "",
        "fallback_model": "", "fallback_provider": "",
        "keys_set": {}, "raw": text or "",
    }
    if not text:
        return state
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line == "MODEL:":
            section = "model"; continue
        if line == "PROVIDER:":
            section = "provider"; continue
        if line == "BASE_URL:":
            section = "base_url"; continue
        if line == "FALLBACK_MODEL:":
            section = "fallback_model"; continue
        if line == "FALLBACK_PROVIDER:":
            section = "fallback_provider"; continue
        if line == "KEYS:":
            section = "keys"; continue
        if section == "keys":
            m = re.match(r"^([A-Z0-9_]+):(SET|MISSING)$", line)
            if m:
                state["keys_set"][m.group(1)] = (m.group(2) == "SET")
        elif section in ("model", "provider", "base_url", "fallback_model", "fallback_provider"):
            if line and not line.startswith(("error", "Error")):
                state[section] = line
    return state


def build_config_save_command(model=None, provider=None, base_url=None,
                              fallback_model=None, fallback_provider=None):
    """Применить модель/провайдера через `hermes config set`.

    Изменяет только те ключи, что переданы (не пустые). Значения
    экранируются через shlex.quote — инъекций нет.
    """
    pairs = [
        ("model.default", model),
        ("model.provider", provider),
        ("model.base_url", base_url),
        ("fallback_model.model", fallback_model),
        ("fallback_model.provider", fallback_provider),
    ]
    cmds = [
        "hermes config set " + shlex.quote(key) + " " + shlex.quote(str(val))
        for key, val in pairs if val is not None and str(val).strip()
    ]
    if not cmds:
        return None
    script = _HERMES_ENV + " ; " + " ; ".join(cmds)
    return "bash -lc " + shlex.quote(script)


def build_env_key_command(var_name, value):
    """Дописать/обновить переменную (API-ключ) в ~/.hermes/.env с правами 600.

    Значение экранируется для sh и — в случае sed-замены — для подстановки
    в replacement (символы & и \\). Само значение на сервере не логируется
    и в конфиг не пишется.
    """
    safe_var = re.sub(r"[^A-Z0-9_]", "", var_name.upper())
    raw = str(value).strip()
    # запрещаем перевод строки и управляющие символы в значении
    if "\n" in raw or "\r" in raw or "\x00" in raw:
        return None
    # экранирование для sed replacement: \ сначала, потом & -> \&
    sed_val = raw.replace("\\", "\\\\").replace("&", "\\&")
    # экранирование для echo (двойные кавычки) -> одинарные кавычки надёжнее:
    # значение без \n, поэтому берём одинарные кавычки, внутри экранируем ' -> '\''
    echo_val = raw.replace("'", "'\\''")
    script = "\n".join([
        'ENV="$HOME/.hermes/.env"',
        'touch "$ENV"',
        'chmod 600 "$ENV"',
        f'if grep -q "^{safe_var}=" "$ENV"; then',
        f'  sed -i -E "s%^({safe_var}=).*%\\1{sed_val}%" "$ENV"',
        "else",
        f"  echo '{safe_var}={echo_val}' >> \"$ENV\"",
        "fi",
        'chmod 600 "$ENV"',
    ])
    return "bash -lc " + shlex.quote(script)


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# hermes chat -Q печатает первой строкой "session_id: 20260821_132040_0f20d2"
_SESSION_LINE_RE = re.compile(r"session_id:\s*([A-Za-z0-9_\-]+)")

# ANSI-управляющие последовательности (цвет и пр.) — вычищаются из вывода джобы
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"            # CSI (цвет, курсор и т.п.)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC с терминатором
    r"|\x1b[@-Z\\-_]"                        # прочие двухбайтовые
)
# Возможный НЕзавершённый хвост последовательности в конце чанка
_INCOMPLETE_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07]*)?$")


def extract_session_id(text):
    """Достать id сессии из вывода hermes chat -Q.

    Сначала ищем строку "session_id: <id>" (формат вида 20260821_132040_0f20d2),
    как запасной вариант — последний UUID в тексте.
    """
    if not text:
        return None
    m = _SESSION_LINE_RE.search(text)
    if m:
        return m.group(1)
    matches = _UUID_RE.findall(text)
    return matches[-1] if matches else None


_ROLE_ALIASES = {
    "user": "user", "human": "user", "prompt": "user",
    "user_message": "user", "user_prompt": "user",
    "assistant": "assistant", "ai": "assistant", "agent": "assistant",
    "assistant_message": "assistant",
}


def _extract_message_specs(obj):
    """Обойти JSON-структуру (словари/списки) и вернуть узлы с role+content.

    `hermes sessions export` может отдавать и построчный jsonl, и один JSON-
    объект с вложенным массивом сообщений (формат менялся между версиями).
    Рекурсивно находим ВСЕ dictionary-узлы похожие на сообщение — контейнер
    (messages/entries/data и т.п.) не обязателен.
    """
    found = []
    if isinstance(obj, dict):
        role_raw = str(obj.get("role") or obj.get("type") or "").lower()
        if _ROLE_ALIASES.get(role_raw) is not None and (
            "content" in obj or "text" in obj
        ):
            found.append(obj)
            return found
        for v in obj.values():
            found.extend(_extract_message_specs(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_extract_message_specs(v))
    return found


def parse_session_jsonl(text, max_messages=200):
    """Разобрать выгрузку `hermes sessions export` в список сообщений.

    Формат не документирован и меняется между версиями: поддерживаем как
    построчный jsonl, так и единый JSON-объект с вложенным массивом сообщений
    (и построчно, и сразу всю выгрузку). Записи без текста (вызовы
    инструментов, метаданные) пропускаются.
    """
    msgs = []
    if not text:
        return msgs

    def handle_efrec(rec_dict):
        role_raw = str(rec_dict.get("role") or rec_dict.get("type") or "").lower()
        role = _ROLE_ALIASES.get(role_raw)
        if role is None:
            return
        content = rec_dict.get("content", rec_dict.get("text"))
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype not in (None, "text", "output_text"):
                        continue  # tool_use / tool_result и прочий шум
                    s = block.get("text") or block.get("content")
                    if isinstance(s, str):
                        texts.append(s)
                elif isinstance(block, str):
                    texts.append(block)
        body = "\n".join(t for t in texts if t).strip()
        if body:
            msgs.append({"role": role, "text": body})

    # 1) построчный jsonl
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for node in _extract_message_specs(obj):
            handle_efrec(node)

    # 2) если ничего не нашли — пробуем всю выгрузку одним JSON-объектом
    if not msgs:
        try:
            whole = json.loads(text)
        except ValueError:
            whole = None
        if whole is not None:
            for node in _extract_message_specs(whole):
                handle_efrec(node)

    return msgs[-max_messages:]


def parse_rich_table(text):
    """Разобрать таблицу rich (рамки │/┃) в список словарей.

    Возвращает список строк-словарей. Строки-рамки (из ─━┄ и углов)
    пропускаются; заголовок — первая не-рамочная строка.
    """
    rows = []
    if not text:
        return rows
    sep = re.compile(r"[│┃]")
    junk = re.compile(r"^[\s─━═┄┈┌┐└┘┏┓┗┛├┤┬┴┼╔╗╚╝╠╣╦╩╬╇╈║]")
    header = None
    for line in text.splitlines():
        if not sep.search(line):
            continue
        cells = [c.strip() for c in sep.split(line)]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if not cells or all(junk.match(c) for c in cells if c):
            continue
        if header is None:
            header = cells
            continue
        row = {}
        for i, val in enumerate(cells):
            key = header[i] if i < len(header) else f"col{i}"
            row[key] = val
        rows.append(row)
    return rows


_BORDER_CHARS = set("─━═┄┈┌┐└┘┏┓┗┛├┤┬┴┼╔╗╚╝╠╣╦╩╬╇╈")
_SESSION_ID_CELL = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Известные колонки `hermes sessions list` (от длинных к коротким, чтобы "Last
# Active" не схватился раньше "Active" и т.п.)
_SESSION_HEADERS = ["Last Active", "Workspace", "Title", "ID"]


def parse_sessions_table(text):
    """Разобрать `hermes sessions list` в список словарей.

    Текущая версия hermes печатает таблицу сессий в моноширинном виде с
    колонками, разделёнными пробелами (без рамок │), плюс линия из ─. Исторически
    встречались и рамки rich (┃/│) — поддерживаем оба варианта.

    Возвращает строки словарями с ключами заголовков: Title, Workspace,
    Last Active, ID. Позиции колонок берутся из строки-заголовка по известным
    именам колонок, поэтому «Last Active» и значения с пробелами режутся верно.
    """
    if not text:
        return []
    lines = [l.rstrip("\n") for l in text.splitlines()]

    # Вариант A: рамки box-рисования (┃ / │)
    if any(("┃" in l or "│" in l) for l in lines):
        return parse_rich_table(text)

    # Вариант B: моноширинная таблица с колонками по пробелам.
    # Заголовок — строка, где есть и Title, и ID.
    header_idx = None
    for i, l in enumerate(lines):
        if re.search(r"\bTitle\b", l) and re.search(r"\bID\b", l):
            header_idx = i
            break
    if header_idx is None:
        return []

    header_line = lines[header_idx]
    # Позиции начала колонок: ищем известные заголовки слева направо.
    cols = []  # (name, start)
    for name in _SESSION_HEADERS:
        idx = header_line.find(name)
        if idx >= 0:
            cols.append((name, idx))
    cols.sort(key=lambda c: c[1])
    if not cols:
        return []

    # Границы колонок: [start_i .. start_{i+1}), последняя — до конца самой
    # длинной строки (заголовок короче данных: ID может выходить за длину шапки).
    names = [c[0] for c in cols]
    starts = [c[1] for c in cols]
    max_len = max((len(l) for l in lines), default=0)
    ends = starts[1:] + [max_len]

    rows = []
    for l in lines[header_idx + 1:]:
        if not l.strip():
            continue
        if all(ch in _BORDER_CHARS or ch.isspace() for ch in l.strip()):
            continue
        if _SESSION_ID_CELL.search(l) is None:
            continue
        row = {}
        for i, name in enumerate(names):
            row[name] = l[starts[i]:ends[i]].strip()
        rows.append(row)
    return rows


class HermesJob:
    """Наблюдатель за фоновой (detached) задачей hermes на сервере.

    Сама задача запускается через `nohup ... &` (см. build_detached_chat_command)
    и НЕ привязана к SSH-каналу/веб-сессии — на VPS она продолжит работу, даже
    если эту сторону отключат. Вывод пишется в лог-файл на сервере, а этот
    объект лишь «подглядывает» за файлом по таймеру (пока жив SSH/сервер).
    """

    def __init__(self, job_id, web_session_id, command, log_path):
        self.id = job_id
        self.web_session_id = web_session_id
        self.command = command
        self.log_path = log_path
        self.buffer = ""
        self.done = False
        self.exit_code = None
        self.error = None
        self.stopped = False
        self.created = time.time()
        self._read_cursor = 0
        self._lock = threading.Lock()

    def _fetch_new(self, ssh_client):
        """Прочитать новые байты лога с сервера (инкрементально)."""
        # путь — константа нашей задачи (bash-переменная $HOME): не кавычим
        cmd = "tail -c +%d %s 2>/dev/null" % (self._read_cursor + 1, self.log_path)
        try:
            _, stdout, _ = ssh_client.client.exec_command(cmd, timeout=20)
            return stdout.read().decode("utf-8", errors="replace")
        except Exception:
            return None

    def _watch(self, ssh_client):
        """Тред-наблюдатель: периодически дочитывает лог пока не появится __RC__."""
        while not self.stopped:
            raw = self._fetch_new(ssh_client)
            if raw:
                with self._lock:
                    self.buffer += raw
                    self._read_cursor = len(self.buffer)
            idx = self.buffer.rfind("__RC__=")
            if idx != -1:
                mm = re.match(r"__RC__=(\d+)", self.buffer[idx:])
                if mm:
                    try:
                        self.exit_code = int(mm.group(1))
                    except Exception:
                        self.exit_code = None
                    with self._lock:
                        self.done = True
                    break
            time.sleep(1.0)
        with self._lock:
            self.done = True

    def chunk_since(self, offset):
        with self._lock:
            data = self.buffer[offset:]
            return {
                "job_id": self.id,
                "done": self.done,
                "exit_code": self.exit_code,
                "offset": len(self.buffer),
                "chunk": data,
                "error": self.error,
            }

    def stop(self):
        """Остановить наблюдение (сама задача на VPS продолжает работать)."""
        self.stopped = True


class HermesJobManager:
    """Реестр джобов Hermes. Одна активная джоба на веб-сессию."""

    MAX_JOBS = 40
    MAX_BUFFER = 512 * 1024

    def __init__(self):
        self.jobs = {}
        self._active = {}  # web_session_id -> job_id
        self._lock = threading.Lock()

    def start(self, job_id, web_session_id, command, log_path, ssh_client):
        """Запустить detached-задачу на сервере и начать наблюдать за её логом.

        job_id задаётся вызывающим (он же в имени лога hj_<job_id>).
        Возвращает (job_id, None) или (None, running_job) если занято.
        """
        with self._lock:
            active = self._active.get(web_session_id)
            if active and active in self.jobs and not self.jobs[active].done:
                return None, self.jobs[active]
            job = HermesJob(job_id, web_session_id, command, log_path)
            # запуск detached на сервере (не привязан к каналу)
            try:
                _, _, _ = ssh_client.client.exec_command(command, timeout=60)
            except Exception as e:
                return None, None
            self.jobs[job_id] = job
            self._active[web_session_id] = job_id
            self._cleanup_locked()
        t = threading.Thread(target=job._watch, args=(ssh_client,), daemon=True)
        t.start()
        return job_id, None

    def stop_active(self, web_session_id):
        """Остановить наблюдение за задачей (сама задача на VPS продолжит)."""
        with self._lock:
            job_id = self._active.get(web_session_id)
            job = self.jobs.get(job_id) if job_id else None
        if job and not job.done:
            job.stop()
            return True
        return False

    def get(self, job_id):
        return self.jobs.get(job_id)

    def drop_web_session(self, web_session_id):
        """Прекратить наблюдение за джобами веб-сессии, НЕ убивая задачи на VPS."""
        self.stop_active(web_session_id)
        with self._lock:
            self._active.pop(web_session_id, None)
            dead = [j for j, job in self.jobs.items() if job.web_session_id == web_session_id]
            for j in dead:
                self.jobs.pop(j, None)

    def _cleanup_locked(self):
        if len(self.jobs) <= self.MAX_JOBS:
            return
        finished = sorted(
            (j for j, job in self.jobs.items() if job.done),
            key=lambda j: self.jobs[j].created,
        )
        for j in finished[: len(finished) - (self.MAX_JOBS // 2)]:
            self.jobs.pop(j, None)
