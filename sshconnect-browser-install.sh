#!/usr/bin/env bash
# sshconnect-browser-install.sh  (v2)
# Веб-браузер (Xvfb + chrome + x11vnc + noVNC) для SSH-Connect.
# ВСЁ слушает только 127.0.0.1 — наружу не светится ничего.
# Запуск: sudo bash sshconnect-browser-install.sh   (повторный запуск = обновление)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/snap/bin"

if [ "$(id -u)" -ne 0 ]; then
  echo "ОШИБКА: запусти от root: sudo bash $0"; exit 1
fi

echo "== 1/6: пакеты =="
apt-get update -qq
apt-get install -y -qq xvfb fluxbox x11vnc websockify dbus-x11 novnc curl \
  gnupg >/dev/null

echo "== 2/6: браузер =="
find_browser() {
  command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null \
    || command -v google-chrome 2>/dev/null || true
}
BROWSER="$(find_browser)"
if [ -z "$BROWSER" ]; then
  echo "  apt chromium..."
  apt-get install -y -qq chromium >/dev/null 2>&1 || true
  BROWSER="$(find_browser)"
fi

# Google Chrome ставим через ОФИЦИАЛЬНЫЙ apt-репозиторий (автообновления),
# а не голым .deb — иначе Chrome жалуется «не удалось обновить».
install_google_repo() {
  echo "  репозиторий Google Chrome (для автообновлений)..."
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor --yes -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -qq
  apt-get install -y -qq google-chrome-stable >/dev/null 2>&1 || true
}
if [ -z "$BROWSER" ]; then
  install_google_repo
  BROWSER="$(find_browser)"
elif [ "$BROWSER" = "/usr/bin/google-chrome" ] \
    && [ ! -f /etc/apt/sources.list.d/google-chrome.list ]; then
  # Chrome уже стоит голым .deb без репозитория — подключаем обновления
  install_google_repo
  BROWSER="$(find_browser)"
fi
if [ -z "$BROWSER" ]; then
  echo "  snap chromium (последний шанс)..."
  snap install chromium >/dev/null 2>&1 || true
  BROWSER="$(find_browser)"
fi
if [ -z "$BROWSER" ]; then
  echo "ОШИБКА: браузер установить не удалось"; exit 1
fi
echo "  браузер: $BROWSER"

# Managed-политика: уведомления об обновлении не показываем (пусть apt
# обновляет молча), а при ЗАКРЫТИИ последнего окна и повторном запуске
# Chrome восстанавливает вкладки — «закрыл нечаянно → окно вернулось».
# Свои каталоги у google-chrome и у chromium.
echo "== 3/6: политика Chrome =="
case "$BROWSER" in
  *google-chrome*) pol=/etc/opt/chrome/policies/managed ;;
  *)              pol=/etc/chromium/policies/managed ;;
esac
install -d -m 0755 "$pol"
printf '{"UpdateNotification": false, "RestoreOnStartup": 4}\n' \
  > "$pol/sshconnect-browser.json"
echo "  политика: $pol/sshconnect-browser.json"

echo "== 4/6: лаунчер и юнит =="
cat > /usr/local/bin/sshconnect-browser-run <<'RUNNER'
#!/bin/sh
# Xvfb + оконный менеджер + x11vnc + websockify + браузер (с автоперезапуском).
# Все порты — только 127.0.0.1. Гаснет вместе с юнитом (весь cgroup).
set -e
export HOME=/var/lib/sshconnect-browser
export DISPLAY=:99
mkdir -p "$HOME"
BROWSER_BIN="$1"
# stderr/stdout Chrome — в лог, чтобы при падении была причина.
: > "$HOME/chrome.log"

Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
XVFB_PID=$!
sleep 1
fluxbox >/dev/null 2>&1 &
WM_PID=$!
x11vnc -display :99 -rfbport 5900 -localhost -shared -nopw \
  -forever -quiet &
VNC_PID=$!
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 \
  >/dev/null 2>&1 &
WS_PID=$!

# Браузер в цикле: упал — поднимается через 2 секунды. Пустого «белого
# стола» больше не бывает: пока Chrome перезапускается, окно появится снова.
(
  while :; do
    "$BROWSER_BIN" --no-first-run --no-default-browser-check \
      --disable-gpu --disable-dev-shm-usage --password-store=basic \
      --user-data-dir="$HOME/profile" --window-size=1366,768 \
      >>"$HOME/chrome.log" 2>&1 || true
    sleep 2
  done
) &
LOOP_PID=$!

trap 'kill $XVFB_PID $WM_PID $VNC_PID $WS_PID $LOOP_PID 2>/dev/null \
  || true; exit 0' INT TERM EXIT
wait
RUNNER
chmod 755 /usr/local/bin/sshconnect-browser-run

cat > /etc/systemd/system/sshconnect-browser.service <<UNIT
[Unit]
Description=SSH-Connect Browser (noVNC, localhost only)
After=network.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=sshconnect-browser
Environment=HOME=/var/lib/sshconnect-browser
ExecStart=/usr/local/bin/sshconnect-browser-run $BROWSER
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT

# права для запуска/остановки БЕЗ пароля: только этот юнит и только юзер,
# от имени которого ставили (для профилей не-root)
SUDO_USER="${SUDO_USER:-}"
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
  cat > /etc/sudoers.d/sshconnect-browser <<SUDOERS
$SUDO_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start sshconnect-browser.service, /usr/bin/systemctl stop sshconnect-browser.service, /usr/bin/systemctl is-active sshconnect-browser.service
SUDOERS
  chmod 440 /etc/sudoers.d/sshconnect-browser
  visudo -cf /etc/sudoers.d/sshconnect-browser >/dev/null \
    || { echo "ОШИБКА: sudoers не прошёл проверку"; exit 1; }
  echo "  sudoers: $SUDO_USER может стартовать/гасить сервис"
else
  echo "  sudoers: пропущен (запуск от root напрямую)"
fi

echo "== 5/6: перезапуск systemd =="
systemctl daemon-reload
systemctl stop sshconnect-browser 2>/dev/null || true

echo "== 6/6: проверка =="
systemctl start sshconnect-browser
sleep 6
echo "  статус: $(systemctl is-active sshconnect-browser)"
EN="$(systemctl is-enabled sshconnect-browser 2>/dev/null)" || true
echo "  автозагрузка: ${EN:-disabled} (старт по кнопке)"
echo "  порты:"
ss -tln | grep -E '127.0.0.1:(5900|6080)' | sed 's/^/    /' || true
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  http://127.0.0.1:6080/vnc.html || true)"
echo "  noVNC HTTP: $code (ожидается 200)"
if pgrep -f '/opt/google/chrome/chrome|/usr/bin/chromium' >/dev/null 2>&1; then
  echo "  chrome: жив"
else
  echo "  chrome: НЕ жив — причина в /var/lib/sshconnect-browser/chrome.log"
fi
echo
echo "Готово. Веб-интерфейс: http://127.0.0.1:6080/vnc.html"
echo "Наружу порты НЕ открыты (только localhost)."
echo "Лог Chrome (root): /var/lib/sshconnect-browser/chrome.log"
echo "Остановить: systemctl stop sshconnect-browser"
