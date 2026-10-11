"""
Docker-браузер («Docker-браузер» вместо RDP в веб-интерфейсе).

Бэкенд по SSH проверяет наличие docker и контейнера chrome на VPS.
Если готов — фронт открывает https://<хост>:3001.
Если нет — бэкенд ничего не делает.
"""

DOCKER_BROWSER_READY = "DOCKER_BROWSER_READY"
DOCKER_BROWSER_MISSING = "DOCKER_BROWSER_MISSING"

DOCKER_PORT = 3001

# Проверка: есть ли docker и контейнер 'chrome' (без pty, sudo не нужен).
DOCKER_CHECK_COMMAND = (
    "if command -v docker >/dev/null 2>&1 && "
    "docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx chrome; "
    "then echo " + DOCKER_BROWSER_READY + "; "
    "else echo " + DOCKER_BROWSER_MISSING + "; fi"
)

# Команды РУЧНОЙ установки (пользователь выполняет их сам на сервере).
# Порядок важен: сначала подключается репозиторий docker.com (в штатных
# репозиториях Ubuntu пакета docker-ce нет), затем установка и запуск.
# Пароль нигде не хранится — sudo спросит его интерактивно у пользователя.
DOCKER_SETUP_COMMANDS = [
    "sudo install -m 0755 -d /etc/apt/keyrings",
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | "
    "sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg",
    "sudo chmod a+r /etc/apt/keyrings/docker.gpg",
    'echo "deb [arch=$(dpkg --print-architecture) '
    'signed-by=/etc/apt/keyrings/docker.gpg] '
    'https://download.docker.com/linux/ubuntu '
    '$(. /etc/os-release && echo "$VERSION_CODENAME") stable" | '
    'sudo tee /etc/apt/sources.list.d/docker.list > /dev/null',
    "sudo apt-get update",
    "sudo apt-get install -y docker-ce docker-ce-cli containerd.io "
    "docker-buildx-plugin docker-compose-plugin",
    "sudo systemctl enable --now docker",
    "sudo docker run -d --name=chrome -e PUID=1000 -e PGID=1000 "
    "-e TZ=Europe/Moscow -e LC_ALL=ru_RU.UTF-8 -p 3001:3001 "
    "-v /opt/chrome/config:/config --shm-size=1gb --restart unless-stopped "
    "lscr.io/linuxserver/chrome:latest",
]

# Тот же набор одной строкой — для кнопки «копировать всё».
DOCKER_SETUP_BLOCK = " && \\\n".join(DOCKER_SETUP_COMMANDS)


def parse_docker_installed(output) -> bool:
    """Установлен ли docker-браузер по ответу DOCKER_CHECK_COMMAND."""
    return DOCKER_BROWSER_READY in (output or "")


def check_ready(client) -> bool:
    """Спросить у сервера, готов ли Docker-браузер (один SSH-запрос, без sudo)."""
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
    """Адрес Docker-браузера на сервере (HTTPS на порту 3001)."""
    return "https://{}:{}".format(hostname or "127.0.0.1", DOCKER_PORT)
