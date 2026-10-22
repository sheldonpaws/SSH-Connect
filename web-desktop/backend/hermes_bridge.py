"""
Hermes Dashboard Bridge — локальный мост к дашборду Hermes на VPS.

Дашборд Hermes слушает 127.0.0.1:9119 ТОЛЬКО на сервере (это конфиг, ключи,
сессии — наружу не светится). Чтобы открыть его из браузера на этом ПК,
нужен SSH-проброс. Раньше это настраивали руками в KiTTY (Tunnels) — запутанно
и не сохранялось. Теперь проброс поднимает сама программа:

    браузер -> 127.0.0.1:9120 (это listening-сокет здесь)
             -> paramiko direct-tcpip канал через ЖИВУЮ SSH-сессию
             -> VPS 127.0.0.1:9119 (дашборд)

Трафик пропускается байт-в-байт (raw TCP pipe), поэтому работает всё:
HTML, JS-бандлы, REST API, вебсокеты, SSE-стриминг. Мост ничего не
интерпретирует и не модифицирует — дашборд ведёт себя ровно так же, как
при прямом открытии, включая свою собственную авторизацию.

Зависимости: только stdlib (socket/threading). paramiko НЕ импортируется —
транспорт передаётся снаружи из уже открытой SSH-сессии (SSHSession.client),
поэтому модуль компилируется и тестируется без paramiko.
"""
import os
import socket
import threading

DEFAULT_LISTEN_HOST = "127.0.0.1"   # только localhost: наружу не светится
DEFAULT_LISTEN_PORT = 9120
DEFAULT_TARGET = ("127.0.0.1", 9119)  # адрес дашборда НА СЕРВЕРЕ
MAX_HEAD = 65536


def _pump(src, dst):
    """Байтовая перекачка src -> dst до первого EOF/ошибки, потом закрыть оба."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class HermesBridge:
    """Один мост на процесс бэкенда.

    start() поднимает listening-сокет и accept-цикл в фоне; каждый входящий
    соединение получает СВОЙ канал direct-tcpip через переданный транспорт,
    затем оба потока пампятся. stop() закрывает listening-сокет и даёт
    активным соединениям дожить до конца (каналы закроются сами).
    """

    def __init__(self, transport, listen_host: str = None, listen_port: int = None,
                 target: tuple = None):
        self.transport = transport
        self.listen_host = listen_host or os.environ.get("HERMES_BRIDGE_HOST", DEFAULT_LISTEN_HOST)
        self.listen_port = listen_port or int(os.environ.get("HERMES_BRIDGE_PORT", DEFAULT_LISTEN_PORT))
        self.target = target or DEFAULT_TARGET
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self.session_id = None  # заполняет владелец: чья сессия держит мост

    @property
    def url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stop.is_set()

    def start(self):
        """Поднять listening-сокет. Бросает OSError (порт занят) или
        Exception транспорта — вызывающий обязан показать ошибку пользователю."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.listen_host, self.listen_port))
        srv.listen(32)
        # Таймаут обязателен: close() на сокете НЕ разбуживает заблокированный
        # accept() в другом потоке на Linux — цикл иначе не видит _stop и
        # продолжает принимать соединения после stop().
        srv.settimeout(0.5)
        self._server = srv
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name=f"hermes-bridge-{self.listen_port}")
        self._thread.start()

    def _accept_loop(self):
        srv = self._server  # локальная ссылка: stop() обнуляет self._server
        while not self._stop.is_set():
            try:
                csock, _addr = srv.accept()
            except socket.timeout:
                continue  # просто проверить флаг остановки заново
            except OSError:
                break  # сокет закрыт (stop())
            if self._stop.is_set():
                # stop() успел пройти между accept() и этой строкой —
                # соединение принимать нельзя, закрываем молча.
                try:
                    csock.close()
                except OSError:
                    pass
                break
            threading.Thread(target=self._handle_client, args=(csock,), daemon=True).start()

    def _handle_client(self, csock: socket.socket):
        channel = None
        try:
            csock.settimeout(60)
            # Прочитать заголовок запроса целиком (HEAD + возможное тело
            # до Content-Length не трогаем — просто догоняем остаток пайпом).
            head = b""
            while b"\r\n\r\n" not in head and len(head) < MAX_HEAD:
                chunk = csock.recv(4096)
                if not chunk:
                    return
                head += chunk

            # Открыть канал до сервера через живой SSH-транспорт.
            # origin = адрес КЛИЕНТА на этой машине (для логов sshd на VPS).
            channel = self.transport.open_channel(
                "direct-tcpip", self.target, ("127.0.0.1", csock.getsockname()[1])
            )
            csock.settimeout(None)
            channel.settimeout(None)
            channel.sendall(head)

            back = threading.Thread(target=_pump, args=(channel, csock), daemon=True)
            back.start()
            _pump(csock, channel)
            back.join(timeout=5)
        except OSError as exc:
            # Транспорт мёртв / порт закрыт на сервере — отвечаем как прокси.
            try:
                msg = f"Hermes bridge: {exc}".encode("utf-8", "replace")
                csock.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg
                )
            except OSError:
                pass
        finally:
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass
            try:
                csock.close()
            except OSError:
                pass

    def stop(self):
        """Остановить мост. Активные соединения доживают (каналы закроются)."""
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
