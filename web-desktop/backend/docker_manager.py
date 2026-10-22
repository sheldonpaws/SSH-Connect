"""
Docker-браузер. Только проверка готовности docker + контейнер chrome на VPS.
Если готов — фронт открывает https://<хост>:3001. Никакой автоустановки.
"""

DOCKER_PORT = 3001
DOCKER_BROWSER_READY = "chrome"
DOCKER_BROWSER_MISSING = ""

DOCKER_CHECK_COMMAND = (
    "command -v docker >/dev/null 2>&1 && "
    "docker ps -a --format '{{.Names}}' 2>/dev/null | grep -x chrome"
)


def parse_docker_installed(output) -> bool:
    return DOCKER_BROWSER_READY in (output or "")


def check_ready(client) -> bool:
    stdin, stdout, _stderr = client.exec_command(
        DOCKER_CHECK_COMMAND + " 2>&1", timeout=15)
    chunks = []
    while True:
        data = stdout.read(4096)
        if not data:
            break
        chunks.append(data)
    return parse_docker_installed(b"".join(chunks).decode("utf-8", errors="replace"))


def browser_url(hostname) -> str:
    return "https://{}:{}".format(hostname or "127.0.0.1", DOCKER_PORT)
