# -*- coding: utf-8 -*-
"""
Pacer-proxy для Hermes агента на VPS.

Проблема: бесплатный лимит провайдера (например Gemini 60 RPM). Сложный
запрос агента делает десятки LLM-вызовов подряд — лимит выбивается,
агент получает 429 и многократно ждёт минуту.

Решение (честное, без изменения еёрмитиевского ядра): прозрачный
reverse-прокси N-на-N, встающий между hermes и провайдером.  Ограничение
частоты скользящим окном (60 запросов / 60 сек): запросы сверх окна ждут
свободный слот и идут дальше НЕ изменёнными — провайдер не падает в лимит,
агент не должен ждать по 429.  Число вызовов не уменьшается, но сложные
задачи больше не валит rate-limit.

Прокси чисто на stdlib Python3 (http.server + http.client) — никаких
pip-зависимостей на VPS.  Ставится в ~/.hermes/pacer_proxy.py, запускается
nohup-ом на 127.0.0.1:<PORT>, прозрачно (body/заголовки не меняются).

base_url hermes переключается на http://127.0.0.1:<PORT> — запросы уходят
в прокси, который форвардит их на исходный адрес провайдера (сохраняется в
~/.hermes/pacer.conf при первом включении).  Откат: вернуть base_url в
прежнее значение вручную (hermes config set model.base_url <старое>) или
удалить ~/.hermes/pacer.conf + pacer_proxy.py и сбросить base_url.
"""

import base64
import os
import shlex

# Порт прокси (внутренний, не пересекается с 17850 обратного доступа)
PACER_PORT = "17901"
# Лимит скользящего окна: запросов в 60 секунд
PACER_RPM = "60"

PACER_SCRIPT = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import http.client
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PACER_PORT", "17901"))
RPM = int(os.environ.get("PACER_RPM", "60"))
CONF = os.environ.get("PACER_CONF", "")
LOG_FILE = os.environ.get("PACER_LOG", "")


def _log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _upstream():
    """Базовый адрес провайдера из pacer.conf (upstream=https://...)."""
    if CONF:
        try:
            with open(CONF, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("upstream="):
                        u = line.split("=", 1)[1].strip()
                        if u.startswith(("http://", "https://")):
                            return u
        except OSError:
            pass
    return "https://generativelanguage.googleapis.com"


class _Pacer:
    """Скользящее окно: не более `limit` вызовов за 60 секунд."""

    def __init__(self, limit):
        self._limit = limit
        self._lock = threading.Lock()
        self._stamps = []

    def wait(self):
        while True:
            now = time.monotonic()
            with self._lock:
                self._stamps = [t for t in self._stamps if now - t < 60.0]
                if len(self._stamps) < self._limit:
                    self._stamps.append(now)
                    return
                wait = self._stamps[0] + 60.0 - now
            time.sleep(max(0.05, wait))


_PACER = _Pacer(int(RPM))

_SKIP_HDRS = {"host", "content-length", "connection", "accept-encoding",
              "transfer-encoding", "keep-alive", "proxy-connection",
              "te", "upgrade"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Pacer/1.0"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, status, body, ctype="text/plain"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        if self.path == "/_health":
            self._reply(200, "ok")
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else None

        _PACER.wait()

        upstream = _upstream()
        parts = urllib.parse.urlsplit(upstream)
        http_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        conn = http_cls(parts.netloc, timeout=180)
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in _SKIP_HDRS:
                continue
            headers[k] = v
        try:
            conn.request(self.command, self.path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in _SKIP_HDRS or k.lower() == "content-encoding":
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass
            _log("fwd %s %s -> %d (%d bytes)"
                 % (self.command, self.path, resp.status, len(data)))
        except Exception as e:
            _log("fwd error %s %s: %s" % (self.command, self.path, e))
            try:
                self._reply(502, "pacer upstream error")
            except OSError:
                pass
        finally:
            conn.close()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle


def main():
    _log("pacer start port=%s rpm=%s upstream=%s"
         % (PORT, RPM, _upstream()))
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
'''


_ENSURE_SCRIPT = r"""set +e
H="$HOME/.hermes"
PORT=17901
PV="http://127.0.0.1:$PORT"
mkdir -p "$H"
echo {B64} | base64 -d > "$H/pacer_proxy.py"
chmod +x "$H/pacer_proxy.py" 2>/dev/null

# --- сохранить исходный адрес провайдера (host, без пути) ---
U="$(hermes config get model.base_url 2>/dev/null | sed -e 's/\x1b\[[0-9;]*m//g' | tr -d '"' | tr -d '\r\n' | grep -Eo 'https?://[^/[:space:]]+' | head -1)"
case "$U" in
  http://127.0.0.1*|http://localhost*) U="";;
esac
if [ -z "$U" ] && [ -f "$H/pacer.conf" ]; then
  U="$(grep '^upstream=' "$H/pacer.conf" | cut -d= -f2- | tr -d '\r' | head -1)"
fi
if echo "$U" | grep -qE '^https?://'; then
  printf 'upstream=%s\n' "$U" > "$H/pacer.conf"
fi
UP="$(grep '^upstream=' "$H/pacer.conf" 2>/dev/null | cut -d= -f2- | tr -d '\r' | head -1)"

# --- поднять прокси, если он не отвечает ---
RUNNING=no
if curl -s -m 2 "http://127.0.0.1:$PORT/_health" 2>/dev/null | grep -q '^ok$'; then
  RUNNING=yes
else
  PACER_PORT=$PORT PACER_CONF="$H/pacer.conf" PACER_LOG="$H/pacer_proxy.log" \
    nohup python3 "$H/pacer_proxy.py" < /dev/null >> "$H/pacer_proxy.log" 2>&1 &
  sleep 1
  curl -s -m 2 "http://127.0.0.1:$PORT/_health" 2>/dev/null | grep -q '^ok$' && RUNNING=yes
fi

# --- переключить base_url только если прокси жив ---
if [ "$RUNNING" = "yes" ]; then
  CUR="$(hermes config get model.base_url 2>/dev/null | sed -e 's/\x1b\[[0-9;]*m//g' | tr -d '"' | tr -d '\r\n' | tail -1)"
  if [ -n "$CUR" ] && [ "$CUR" != "$PV" ]; then
    hermes config set model.base_url "$PV" >/dev/null 2>&1
  fi
fi

echo "PACER_UPSTREAM=${UP:-MISSING}"
echo "PACER_PORT=$PORT"
echo "PACER_RUNNING=$RUNNING"
echo "PACER_CFG=$PV"
"""

# Порт в шаблоне должен совпадать с PACER_PORT (константа выше).
_ENSURE_SCRIPT = _ENSURE_SCRIPT.replace('PORT=17901', 'PORT=' + PACER_PORT)


def build_pacer_ensure_command():
    """Развернуть/поднять прокси и переключить base_url hermes на него.

    Идемпотентна: при каждом вызове проверяет /_health, перезапускает
    только если прокси не жив.  base_url переключается только после
    успешного старта (иначе остаётся прямой доступ к провайдеру).

    Возвращает внешнюю bash-команду для exec_command.
    """
    b64 = base64.b64encode(PACER_SCRIPT.encode('utf-8')).decode('ascii')
    script = _ENSURE_SCRIPT.replace('{B64}', b64)
    return "bash -lc " + shlex.quote(script)


def parse_pacer_state(text):
    """Разобрать вывод build_pacer_ensure_command в словарь состояния."""
    st = {
        "running": False,
        "port": None,
        "upstream": "",
        "base_url": "",
        "raw": text or "",
    }
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("PACER_UPSTREAM="):
            st["upstream"] = line.split("=", 1)[1]
        elif line.startswith("PACER_PORT="):
            st["port"] = line.split("=", 1)[1]
        elif line.startswith("PACER_RUNNING="):
            st["running"] = line.split("=", 1)[1].lower() == "yes"
        elif line.startswith("PACER_CFG="):
            st["base_url"] = line.split("=", 1)[1]
    return st


# --- локальный самопроверочный вариант (только для тестов на Windows) ---
def pacer_script_text():
    return PACER_SCRIPT