"""
Обратный доступ: локальный HTTP-файловый сервер (чтение + запись) для Hermes-агента.

Windows открывает выбранные папки на 127.0.0.1 через threading HTTPServer, затем
через ОБРАТНЫЙ SSH-туннель (paramiko `request_port_forward`) выставляет порт
как 127.0.0.1:<vps_port> на VPS. Агент на сервере получает доступ по
http://127.0.0.1:<vps_port>/... через обычный curl:

    curl http://127.0.0.1:PORT/                             # список монтирований (JSON)
    curl http://127.0.0.1:PORT/<slug>/                      # список каталога (JSON)
    curl http://127.0.0.1:PORT/<slug>/repos/proj/a.py       # прочитать файл
    curl -X PUT --data-binary @a.py \
        http://127.0.0.1:PORT/<slug>/repos/proj/a.py        # записать (создаёт каталоги)
    curl -X DELETE http://127.0.0.1:PORT/<slug>/tmp.txt     # удалить
    curl -X MKCOL http://127.0.0.1:PORT/<slug>/newdir       # создать каталог

Можно отдавать НЕСКОЛЬКО папок одновременно на одном порту: каждая монтируется
под своим URL-префиксом (слагом), например D:/Projects/A -> http://.../proj-a/,
D:/Backups -> http://.../backups/. Слаг строится из имени папки автоматически.
Манифест ~/.hermes/reverse_access.json у агента содержит {папка: url} для каждой
монтированной папки (см. GUI).

Порт на VPS ВСЕГДА фиксированный (см. ReverseAccess.FIXED_PORT). Если он занят —
старт не происходит, случайный порт не берётся (иначе URL у агента «плавает» и
путается). Keepalive на SSH-транспорте ставится в src/ssh.py, поэтому туннель
не рвётся по таймауту NAT.

Безопасность: сервер слушает ТОЛЬКО 127.0.0.1 на Windows, туннель привязан к
127.0.0.1 на VPS; каждая смонтированная папка жёстко ограничена своим корнем
(проверка commonpath, ни один путь наружу через "../" не проходит).
"""

import json
import mimetypes
import os
import re
import shutil
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

# Транслит кириллицы для слагов (папки пользователей часто русские).
_CYRILLIC_TRANS = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    'А': 'a', 'Б': 'b', 'В': 'v', 'Г': 'g', 'Д': 'd', 'Е': 'e', 'Ё': 'e',
    'Ж': 'zh', 'З': 'z', 'И': 'i', 'Й': 'i', 'К': 'k', 'Л': 'l', 'М': 'm',
    'Н': 'n', 'О': 'o', 'П': 'p', 'Р': 'r', 'С': 's', 'Т': 't', 'У': 'u',
    'Ф': 'f', 'Х': 'h', 'Ц': 'ts', 'Ч': 'ch', 'Ш': 'sh', 'Щ': 'sch',
    'Ъ': '', 'Ы': 'i', 'Ь': '', 'Э': 'e', 'Ю': 'yu', 'Я': 'ya',
})


def make_slug(folder, used=None):
    """URL-безопасный слаг из имени папки (a-z0-9 и дефисы), кириллицу транслитерирует."""
    used = used if used is not None else set()
    base = os.path.basename(os.path.normpath(folder)) or "folder"
    slug = _SLUG_RE.sub("-", base.translate(_CYRILLIC_TRANS)).strip("-").lower()
    if not slug:
        slug = "share"
    candidate, n = slug, 1
    while candidate in used:
        n += 1
        candidate = "{}-{}".format(slug, n)
    used.add(candidate)
    return candidate


class _ReverseHandler(BaseHTTPRequestHandler):
    """HTTP: GET (файл/JSON-список каталога), PUT, DELETE, MKCOL.

    Монтирования живут в self.server.mounts {slug: абс_папка}. Корень "/" отдаёт
    список монтирований; "/<slug>/..." разрешается внутри соответствующей папки.
    """

    protocol_version = "HTTP/1.1"
    server_version = "ReverseAccess/1.0"
    sys_version = ""

    # ---- утилиты ----

    def log_message(self, fmt, *args):
        pass  # не пишем в stdout (сервер запускается из GUI)

    def _resolve(self):
        """Разобрать URL: (kind, target, base_rel_url).

        kind: "root" — монтирования; "missing" — неизвестный слаг;
        "forbidden" — путь наружу за корень монтирования; "file" — валидный.
        target — абсолютный путь (для file); base_rel_url — URL-префикс
        (<slug>, <slug>/<подкаталог>) для построения ссылок в листингах.
        """
        raw = self.path.split("?", 1)[0]
        unquoted = urllib.parse.unquote(raw)
        try:
            # сырой UTF-8 в URL (curl с кириллицей без percent-encoding):
            # HTTP-парсер декодирует path как latin-1 -> mojibake; пробуем вернуть
            # UTF-8. Для percent-encoded строк этот путь просто не меняет строку
            # (уже валидный юникод), поэтому повторное срабатывание безопасно.
            fixed = unquoted.encode("latin-1").decode("utf-8")
            if fixed != unquoted:
                unquoted = fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        rel = unquoted.strip("/")
        if not rel:
            return ("root", None, "/")
        parts = rel.split("/", 1)
        slug = urllib.parse.unquote(parts[0])
        rest = parts[1] if len(parts) > 1 else ""
        root_abs = self.server.mounts.get(slug)
        if root_abs is None:
            return ("missing", None, None)
        norm_rest = os.path.normpath(rest)
        if norm_rest == ".":
            norm_rest = ""
        target = os.path.abspath(os.path.join(root_abs, norm_rest))
        root_norm = os.path.normcase(root_abs)
        target_norm = os.path.normcase(target)
        inside = (target_norm == root_norm) or (
            os.path.commonpath([root_norm, target_norm]) == root_norm)
        if not inside:
            return ("forbidden", None, None)
        base = "/" + slug
        if norm_rest:
            base += "/" + norm_rest.replace(os.sep, "/")
        return ("file", target, base)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code, message):
        self._send_json(code, {"ok": False, "error": message})

    # ---- методы ----

    def do_GET(self):
        kind, target, base_rel_url = self._resolve()
        if kind == "root":
            entries = [{
                "name": slug,
                "type": "dir",
                "path": os.path.basename(os.path.normpath(folder)),
                "url": "/{}/".format(urllib.parse.quote(slug)),
            } for slug, folder in sorted(self.server.mounts.items())]
            self._send_json(200, {"ok": True, "path": "/", "type": "dir",
                                  "entries": entries})
            return
        if kind == "missing":
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        if kind == "forbidden":
            self._send_error_json(403, "Доступ запрещён: путь вне корня")
            return
        if not os.path.lexists(target):
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        if os.path.isdir(target):
            entries = []
            for name in sorted(os.listdir(target)):
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                    is_dir = os.path.isdir(full)
                except OSError:
                    st = None
                    is_dir = False
                # готовый percent-encoded URL (кириллица/пробелы/« » корректны)
                url = base_rel_url.rstrip("/") + "/" + urllib.parse.quote(name)
                if is_dir:
                    url += "/"
                entries.append({
                    "name": name,
                    "type": "dir" if is_dir else "file",
                    "size": st.st_size if st else None,
                    "mtime": int(st.st_mtime) if st else None,
                    "url": url,
                })
            self._send_json(200, {
                "ok": True,
                "path": base_rel_url or ("/" + slug),
                "type": "dir",
                "entries": entries,
            })
            return
        # файл — отдаём байты
        try:
            st = os.stat(target)
            with open(target, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_error_json(500, str(e))
            return
        ctype, _ = mimetypes.guess_type(target)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(st.st_size))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        kind, target, _ = self._resolve()
        if kind == "missing":
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        if kind == "forbidden":
            self._send_error_json(403, "Доступ запрещён: путь вне корня")
            return
        if kind == "root":
            self._send_error_json(400, "PUT не на монтирование")
            return
        parent = os.path.dirname(target)
        try:
            os.makedirs(parent, exist_ok=True)
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else b""
            with open(target, "wb") as f:
                f.write(data)
        except Exception as e:
            self._send_error_json(500, str(e))
            return
        self._send_json(200, {"ok": True, "path": self.path, "bytes": len(data)})

    def do_DELETE(self):
        kind, target, _ = self._resolve()
        if kind == "missing":
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        if kind == "forbidden":
            self._send_error_json(403, "Доступ запрещён: путь вне корня")
            return
        if kind == "root":
            self._send_error_json(400, "DELETE не на монтирование")
            return
        if not os.path.lexists(target):
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        try:
            os.unlink(target) if os.path.isfile(target) else shutil.rmtree(target)
        except Exception as e:
            self._send_error_json(500, str(e))
            return
        self._send_json(200, {"ok": True, "path": self.path})

    def do_MKCOL(self):
        kind, target, _ = self._resolve()
        if kind == "missing":
            self._send_error_json(404, "Не найдено: " + self.path)
            return
        if kind == "forbidden":
            self._send_error_json(403, "Доступ запрещён: путь вне корня")
            return
        if kind == "root":
            self._send_error_json(400, "MKCOL не на монтирование")
            return
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            self._send_error_json(500, str(e))
            return
        self._send_json(201, {"ok": True, "path": self.path})


class _ReverseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mounts):
        self.mounts = {slug: os.path.abspath(folder)
                       for slug, folder in mounts.items()}
        super().__init__(("127.0.0.1", 0), _ReverseHandler)


def _pump(src, dst):
    """Перебросить байты из одного сокета/канала в другой до EOF."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except Exception:
        pass


class ReverseAccess:
    """Локальный файловый сервер (несколько монтирований) + обратный SSH-туннель.

    Способы передачи mounts:
      - dict {slug: папка} — слаги заданы явно;
      - строка / список строк — слаги строятся из имени папки (make_slug).

    start(transport) открывает корни папок локально и просит SSH-сервер VPS
    забиндить FIXED_PORT на 127.0.0.1 -> обратно на Windows. Если порт занят —
    старт НЕ происходит (случайный порт не берём, чтобы URL у агента не плавал).
    Возвращает (ok, message). stop() снимает форвард и гасит сервер.
    """

    FIXED_PORT = 17850

    def __init__(self, mounts, on_log=None):
        used = set()
        self.mounts = {}
        if isinstance(mounts, dict):
            for slug, folder in mounts.items():
                slug = str(slug) or make_slug(folder, used)
                used.add(slug)
                self.mounts[slug] = os.path.abspath(folder)
        else:
            if isinstance(mounts, str):
                mounts = [mounts]
            for folder in mounts:
                slug = make_slug(folder, used)
                self.mounts[slug] = os.path.abspath(folder)
        self.root_dir = next(iter(self.mounts.values()), None) or ""
        self.on_log = on_log or (lambda *a, **k: None)
        self._server = None
        self._transport = None
        self._vps_addr = None
        self._vps_port = None
        self._running = False
        self._accept_thread = None
        self._serve_thread = None
        self.local_port = None
        self._lock = threading.Lock()
        self._active_pairs = set()  # (chan, sock) — для принудительного закрытия

    def _log(self, msg, level="info"):
        try:
            self.on_log(msg, level)
        except Exception:
            pass

    def start(self, transport, port=None):
        """Запустить сервер и туннель. Возвращает (ok, message).

        port — фиксированный порт на VPS (по умолчанию FIXED_PORT). Если занят
        или указан другой — НЕ берём случайный, старт проваливается с ошибкой.
        """
        if self._running:
            return False, "Уже запущено"
        if port and int(port) != self.FIXED_PORT:
            self._log("⚠️ Порт {p} не поддерживается — используется фикс. "
                      "{f}".format(p=port, f=self.FIXED_PORT), "warning")
            port = self.FIXED_PORT
        try:
            self._server = _ReverseServer(self.mounts)
            self.local_port = self._server.server_address[1]
            self._serve_thread = threading.Thread(
                target=self._serve_forever_safe, daemon=True)
            self._serve_thread.start()
        except Exception as e:
            self._log(f"❌ Не удалось поднять локальный сервер: {e}", "error")
            return False, str(e)

        try:
            payload = transport.request_port_forward(
                "127.0.0.1", self.FIXED_PORT)
            # paramiko возвращает просто int-порт; старые версии — кортеж
            if isinstance(payload, tuple):
                addr, port = payload
            else:
                addr, port = "127.0.0.1", int(payload)
            self._vps_addr, self._vps_port = addr, port
        except Exception as e:
            self._stop_local_server()
            self._log(f"❌ Обратный туннель не удался: {e}", "error")
            return False, (f"Порт {self.FIXED_PORT} занят на VPS или туннель "
                           f"не создан: {e}")

        self._transport = transport
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return True, f"127.0.0.1:{self._vps_port}"

    def _serve_forever_safe(self):
        """Обёртка над serve_forever: при закрытии сервера поток выходит
        молча, без трейсбека в stdout (иначе падало бы в ошибки.txt)."""
        try:
            self._server.serve_forever()
        except Exception:
            if self._running:
                self._log("⚠️ Сервер обратного доступа остановился", "warning")
        finally:
            try:
                self._server.server_close()
            except Exception:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                chan = self._transport.accept(0.5)
            except Exception:
                break
            if chan is None:
                continue
            threading.Thread(target=self._bridge, args=(chan,), daemon=True).start()

    def _bridge(self, chan):
        try:
            sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=10)
        except OSError:
            try:
                chan.close()
            except Exception:
                pass
            return
        with self._lock:
            self._active_pairs.add((chan, sock))
        t1 = threading.Thread(target=_pump, args=(sock, chan), daemon=True)
        t2 = threading.Thread(target=_pump, args=(chan, sock), daemon=True)
        t1.start()
        t2.start()
        try:
            t1.join()
            t2.join()
        finally:
            with self._lock:
                self._active_pairs.discard((chan, sock))
            try:
                sock.close()
            except Exception:
                pass
            try:
                chan.close()
            except Exception:
                pass

    @property
    def vps_url(self):
        return f"http://127.0.0.1:{self._vps_port}/"

    def url_for(self, slug):
        """URL конкретного монтирования: http://127.0.0.1:<vps_port>/<slug>/."""
        return "{}/{}".format(self.vps_url.rstrip("/"),
                              urllib.parse.quote(slug)) + "/"

    def stop(self):
        self._running = False
        # принудительно закрыть все активные соединения, разблокировать _pump
        with self._lock:
            pairs = list(self._active_pairs)
            self._active_pairs.clear()
        for chan, sock in pairs:
            try:
                chan.close()
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        if self._transport and self._vps_port:
            try:
                self._transport.cancel_port_forward(self._vps_addr or "127.0.0.1",
                                                    self._vps_port)
            except Exception:
                pass
        if self._server:
            self._stop_local_server()
        self._transport = None
        self._server = None
        self._vps_addr = None
        self._vps_port = None

    def _stop_local_server(self):
        """Остановить локальный HTTP-сервер чисто.

        shutdown() останавливает serve_forever и ДОЛЖЕН идти первым, иначе
        server_close() закрывает сокет, а serve_forever ещё выбирает на нём —
        WinError 10038. shutdown() блокирует до выхода serve_forever, поэтому
        вызывается только если поток сервера ещё жив (иначе зависнет).
        """
        serve_thread = getattr(self, "_serve_thread", None)
        if serve_thread is not None and serve_thread.is_alive():
            try:
                self._server.shutdown()
            except Exception:
                pass
        try:
            self._server.server_close()
        except Exception:
            pass