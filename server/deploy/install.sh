#!/usr/bin/env bash
# Music Hub: установка и обновление на чистом сервере. Повторный запуск безопасен
# (токен, пароль Navidrome и данные сохраняются) — так же делается обновление.
#
#   sudo bash install.sh --host 203.0.113.7            # HTTPS: <ip>.sslip.io + Let's Encrypt
#   sudo bash install.sh --host music.example.com      # свой домен (A-запись → этот сервер)
#   sudo bash install.sh --host 203.0.113.7 --tls off --http-port 8080   # без HTTPS
#
# Вывод — для приложения. Строки с «::» разбирает оно, остальное (apt, docker pull) — это
# просто журнал:
#   ::step <id> <подпись>      начался этап
#   ::log <текст>              сообщение
#   ::warn <КОД> <текст>       не мешает работе, но стоит сказать пользователю
#   ::error <КОД> <текст>      установка остановлена
#   ::result <json>            готово: адреса, токен, логин Navidrome
set -Eeuo pipefail
exec 2>&1
export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
# на «закалённых» серверах umask бывает 027 или 077: файлы, которые мы копируем, должны читаться пользователем 1000 в контейнерах
umask 022

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HUB_DIR=${HUB_DIR:-/opt/music-hub}
HUB_VERSION=$(cat "$SRC/VERSION" 2>/dev/null || echo dev)
TLS=auto
HOST=""
HTTP_PORT=""
ND_USER_ARG=""
SKIP_DOCKER=0
MODE=""               # local: Navidrome ставится здесь же; remote: здесь только скачивание, музыка уезжает на другой сервер
MUSIC_DIR_ARG=""      # local: своя папка с музыкой вместо <каталог установки>/data/music

step() { printf '::step %s %s\n' "$1" "$2"; }
say()  { printf '::log %s\n' "$*"; }
warn() { printf '::warn %s %s\n' "$1" "${*:2}"; }
FAILED=""
die()  { FAILED=1; printf '::error %s %s\n' "$1" "${*:2}"; exit 1; }
trap 'rc=$?; if [ $rc -ne 0 ] && [ -z "$FAILED" ]; then printf "::error UNEXPECTED Установщик остановился на строке %s (код %s)\n" "${LINENO}" "$rc"; fi' EXIT

have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
  case $1 in
    --host) HOST=$2; shift 2 ;;
    --tls) TLS=$2; shift 2 ;;
    --http-port) HTTP_PORT=$2; shift 2 ;;
    --nd-user) ND_USER_ARG=$2; shift 2 ;;
    --dir) HUB_DIR=$2; shift 2 ;;
    --mode) MODE=$2; shift 2 ;;
    --music-dir) MUSIC_DIR_ARG=$2; shift 2 ;;
    --no-docker-install) SKIP_DOCKER=1; shift ;;
    *) die BAD_ARGS "Неизвестный параметр: $1" ;;
  esac
done
[ -n "$HOST" ] || die BAD_ARGS "Не указан --host (IP или домен сервера)"
case $TLS in auto|off) ;; *) die BAD_ARGS "--tls: auto или off" ;; esac
if [ -z "$MODE" ]; then   # не указан — остаёмся в том режиме, в котором сервер уже был установлен
  MODE=$(sed -n 's/^HUB_MODE=//p' "$HUB_DIR/.env" 2>/dev/null | tail -1)
  MODE=${MODE:-local}
fi
case $MODE in local|remote) ;; *) die BAD_ARGS "--mode: local или remote" ;; esac
if [ -n "$MUSIC_DIR_ARG" ]; then
  [ "$MODE" = local ] || die BAD_ARGS "--music-dir имеет смысл только для режима local (в remote папку задаёшь на сервере Navidrome)"
  printf '%s' "$MUSIC_DIR_ARG" | grep -Eq '^/[A-Za-z0-9._/@+-]+$' || die BAD_ARGS "Папка с музыкой: полный путь из латиницы, цифр и . _ / @ + -"
  case $MUSIC_DIR_ARG in */../*|*/..|/) die BAD_ARGS "Странная папка с музыкой: $MUSIC_DIR_ARG" ;; esac
fi
printf '%s' "$HOST" | grep -Eq '^[A-Za-z0-9.:-]+$' || die BAD_ARGS "Странный адрес сервера: $HOST"
if [ "$TLS" = auto ] && printf '%s' "$HOST" | grep -q :; then die BAD_ARGS "Для HTTPS по IPv6 нужен домен: укажи его вместо адреса"; fi
if [ -n "$HTTP_PORT" ]; then printf '%s' "$HTTP_PORT" | grep -Eq '^[0-9]{2,5}$' || die BAD_ARGS "--http-port: число"; fi

# ------------------------------------------------------------------ система
step detect "Проверяю сервер"
[ "$(id -u)" = 0 ] || die NOT_ROOT "Нужен root (или sudo)"
[ "$(uname -s)" = Linux ] || die OS "Поддерживается только Linux"
case $(uname -m) in x86_64|aarch64|arm64) ;; *) die ARCH "Архитектура $(uname -m) не поддерживается (нужна x86_64 или arm64)" ;; esac

FAMILY=other
if [ -r /etc/os-release ]; then
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) FAMILY=debian ;;
    *" rhel "*|*" fedora "*|*" centos "*) FAMILY=rhel ;;
  esac
  say "Система: ${PRETTY_NAME:-${ID:-unknown}} ($(uname -m))"
fi

mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
[ "${mem_mb:-0}" -ge 600 ] || warn LOW_MEMORY "Мало памяти (${mem_mb} МБ): скачивание и сканирование будут медленными"
disk_mb=$(df -Pm "$(dirname "$HUB_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
[ "${disk_mb:-0}" -ge 2048 ] || warn LOW_DISK "Мало места на диске (${disk_mb} МБ свободно)"

pkg_install() {
  if have apt-get; then apt-get update -qq && apt-get install -y -qq "$@"
  elif have dnf; then dnf install -y -q "$@"
  elif have yum; then yum install -y -q "$@"
  else return 1; fi
}
have curl || { say "Ставлю curl"; pkg_install curl ca-certificates || die NO_CURL "Не смог поставить curl"; }

retry() { # retry <раз> <команда…>
  local n=$1 i; shift
  for i in $(seq 1 "$n"); do "$@" && return 0; [ "$i" -lt "$n" ] && sleep 4; done
  return 1
}

# ------------------------------------------------------------------- Docker
step docker "Docker"
docker_ok() { have docker && docker info >/dev/null 2>&1; }
compose_ok() { docker compose version >/dev/null 2>&1; }

install_compose_plugin() {
  local pin=${1:-latest} arch dir=/usr/local/lib/docker/cli-plugins url
  case $(uname -m) in x86_64) arch=x86_64 ;; *) arch=aarch64 ;; esac
  mkdir -p "$dir"
  if [ "$pin" = latest ]; then
    url="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$arch"
  else
    url="https://github.com/docker/compose/releases/download/$pin/docker-compose-linux-$arch"
  fi
  curl -fsSL --retry 3 --connect-timeout 15 -o "$dir/docker-compose.tmp" "$url" \
    && chmod +x "$dir/docker-compose.tmp" && mv "$dir/docker-compose.tmp" "$dir/docker-compose"
}

if ! docker_ok; then
  [ "$SKIP_DOCKER" = 0 ] || die NO_DOCKER "Docker не установлен или не запущен"
  say "Docker не найден — ставлю"
  if ! timeout 900 sh -c 'curl -fsSL --connect-timeout 15 https://get.docker.com | sh'; then
    warn DOCKER_SCRIPT "get.docker.com недоступен или не сработал — ставлю Docker из пакетов системы"
    case $FAMILY in
      debian) pkg_install docker.io ;;
      rhel) pkg_install docker ;;
      *) die DOCKER_INSTALL "Не смог поставить Docker: для этой системы нет запасного способа" ;;
    esac || die DOCKER_INSTALL "Не смог поставить Docker из пакетов системы"
  fi
  if [ -d /run/systemd/system ]; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  docker_ok || die DOCKER_START "Docker поставился, но не запускается (docker info не отвечает)"
fi
say "Docker: $(docker --version | sed 's/Docker version //; s/,.*//')"

if ! compose_ok; then
  say "Нет docker compose — ставлю плагин"
  if have apt-get && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
    pkg_install docker-compose-v2 >/dev/null 2>&1 || true
  fi
  compose_ok || install_compose_plugin latest || warn COMPOSE_DL "Не скачался плагин compose с GitHub"
  compose_ok || die COMPOSE_INSTALL "Не получилось поставить docker compose"
fi
say "Compose: $(docker compose version --short)"

# ----------------------------------------------------------------- параметры
step config "Настройка"
mkdir -p "$HUB_DIR"
ENV_FILE="$HUB_DIR/.env"
FIRST_RUN=1
[ -f "$ENV_FILE" ] && FIRST_RUN=0

get_env() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 || true; }
set_env() { # set_env KEY VALUE
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; fi
}
rand() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# head перед tr, а не после /dev/urandom: иначе tr ловит SIGPIPE и pipefail роняет скрипт
rand_alnum() { head -c 600 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c "$1"; }

HUB_TOKEN=$(get_env HUB_TOKEN); [ -n "$HUB_TOKEN" ] || HUB_TOKEN=$(rand 32)
ND_USER=$(get_env ND_USER); [ -n "$ND_USER" ] || ND_USER=${ND_USER_ARG:-music}
ND_PASS=$(get_env ND_PASS); [ -n "$ND_PASS" ] || ND_PASS=$(rand_alnum 20)

is_ipv4() { printf '%s' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }
if [ "$TLS" = auto ]; then
  if is_ipv4 "$HOST"; then ADDR="$(printf '%s' "$HOST" | tr . -).sslip.io"; else ADDR=$HOST; fi
  PUBLIC_URL="https://$ADDR"
  HTTP_BIND=80; HTTPS_BIND=443; HUB_ADDR=$ADDR
else
  [ -n "$HTTP_PORT" ] || HTTP_PORT=8080
  PUBLIC_URL="http://$HOST:$HTTP_PORT"
  HTTP_BIND=$HTTP_PORT
  HTTPS_BIND="127.0.0.1:"        # 443 наружу не открываем: случайный порт на localhost
  HUB_ADDR="http://:80"
fi

# порты: свои (уже работающий наш Caddy) не считаются занятыми
port_busy_by() { # -> пусто, если свободен
  have ss || return 0
  ss -Hltnp "sport = :$1" 2>/dev/null | head -1 | sed -n 's/.*users:(("\([^"]*\)".*/\1/p;t;s/.*/неизвестный процесс/p'
}
ours_listening() {
  [ -f "$HUB_DIR/docker-compose.yml" ] || return 1
  (cd "$HUB_DIR" && docker compose ps -q caddy 2>/dev/null | grep -q .)
}
if ! ours_listening; then
  if [ "$TLS" = auto ]; then
    for p in 80 443; do
      who=$(port_busy_by $p)
      [ -z "$who" ] || die PORTS_BUSY "Порт $p занят ($who). Для HTTPS нужны 80 и 443: освободи их или поставь без HTTPS на другом порту"
    done
  else
    who=$(port_busy_by "$HTTP_PORT")
    [ -z "$who" ] || die PORT_BUSY "Порт $HTTP_PORT занят ($who). Выбери другой порт"
  fi
fi

# ----------------------------------------------------------- файлы и каталоги
step files "Копирую файлы"
if [ "$MODE" = remote ]; then
  # только скачивание: Navidrome стоит на другом сервере, сюда музыка попадает лишь на время передачи
  MUSIC_HOST="$HUB_DIR/data/stage"
  mkdir -p "$MUSIC_HOST" "$HUB_DIR/data/hub"
  cp "$SRC/docker-compose.remote.yml" "$HUB_DIR/docker-compose.yml"
  cp "$SRC/Caddyfile.remote" "$HUB_DIR/Caddyfile"
else
  # при обновлении без --music-dir остаёмся в ранее выбранной папке (если сервер был в режиме local)
  PREV_DIR=""
  [ "$(get_env HUB_MODE)" = remote ] || PREV_DIR=$(get_env HUB_MUSIC_DIR)
  MUSIC_HOST=${MUSIC_DIR_ARG:-${PREV_DIR:-$HUB_DIR/data/music}}
  mkdir -p "$MUSIC_HOST" "$HUB_DIR/data/navidrome" "$HUB_DIR/data/hub"
  cp "$SRC/docker-compose.yml" "$SRC/Caddyfile" "$HUB_DIR/"
fi
for f in install.sh uninstall.sh preflight.sh VERSION; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$HUB_DIR/" || true; done
rm -rf "$HUB_DIR/api"; cp -r "$SRC/api" "$HUB_DIR/api"       # исходники — на случай сборки образа на месте
if [ "$FIRST_RUN" = 1 ]; then
  chown -R 1000:1000 "$HUB_DIR/data"
else
  chown 1000:1000 "$HUB_DIR/data" "$HUB_DIR"/data/* 2>/dev/null || true   # без рекурсии: библиотека может быть огромной
fi
if [ "$MODE" = local ] && [ "$MUSIC_HOST" != "$HUB_DIR/data/music" ]; then
  # своя папка (возможно, с уже существующей музыкой): владельца меняем только у неё самой, не рекурсивно
  [ "$(stat -c %u "$MUSIC_HOST")" = 1000 ] || chown 1000:1000 "$MUSIC_HOST" 2>/dev/null || true
  if find "$MUSIC_HOST" -maxdepth 3 ! -user 1000 -print -quit 2>/dev/null | grep -q .; then
    warn MUSIC_OWNER "В папке $MUSIC_HOST есть файлы другого владельца: сервис работает от пользователя 1000 и не сможет дописывать в такие папки. Выполни на сервере: chown -R 1000:1000 $MUSIC_HOST"
  fi
fi

set_env HUB_MODE "$MODE"
set_env HUB_MUSIC_DIR "$MUSIC_HOST"
set_env HUB_TOKEN "$HUB_TOKEN"
set_env ND_USER "$ND_USER"
set_env ND_PASS "$ND_PASS"
set_env HUB_ADDR "$HUB_ADDR"
set_env HUB_HTTP_BIND "$HTTP_BIND"
set_env HUB_HTTPS_BIND "$HTTPS_BIND"
set_env TZ "$(cat /etc/timezone 2>/dev/null || echo UTC)"

compose() { (cd "$HUB_DIR" && docker compose "$@"); }

# ----------------------------------------------------------------- firewall
if have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
  if [ "$TLS" = auto ]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; say "ufw: открыл порты 80 и 443"
  else ufw allow "$HTTP_PORT"/tcp >/dev/null; say "ufw: открыл порт $HTTP_PORT"; fi
fi
if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
  if [ "$TLS" = auto ]; then firewall-cmd -q --permanent --add-port=80/tcp --add-port=443/tcp
  else firewall-cmd -q --permanent --add-port="$HTTP_PORT"/tcp; fi
  firewall-cmd -q --reload && say "firewalld: порты открыты"
fi

# -------------------------------------------------------------------- образы
step images "Скачиваю образы"
if [ "$MODE" = remote ]; then PULL_SERVICES="caddy"; else PULL_SERVICES="navidrome caddy"; fi
try_pull() { retry 3 compose pull $PULL_SERVICES; }
if ! try_pull; then
  warn REGISTRY "Docker Hub недоступен с этого сервера — пробую зеркало mirror.gcr.io"
  set_env REG "mirror.gcr.io/"
  try_pull || die IMAGES "Образы не скачиваются ни с Docker Hub, ни с зеркала. Скорее всего, они заблокированы для этого сервера: нужен VPN/прокси для Docker или другой сервер"
fi
pull_api() {
  retry 2 compose pull api && return 0
  say "Готовый образ API недоступен — собираю его на сервере (несколько минут)"
  compose build api
}
pull_api || die BUILD "Не удалось ни скачать, ни собрать образ API"

# ------------------------------------------------------------------- запуск
step up "Запускаю"
if ! out=$(compose up -d --remove-orphans 2>&1); then
  printf '%s\n' "$out"
  if printf '%s' "$out" | grep -qi 'client version .* too new\|API version'; then
    warn COMPOSE_OLD "Плагин compose новее Docker на сервере — ставлю совместимую версию"
    install_compose_plugin v2.24.7 || die COMPOSE_INSTALL "Не получилось поставить совместимый compose"
    compose up -d --remove-orphans || die UP "Контейнеры не запустились"
  else
    die UP "Контейнеры не запустились"
  fi
else
  printf '%s\n' "$out"
fi

wait_healthy() { # wait_healthy <сервис> <попыток по 3 с> -> 0, если контейнер стал healthy
  local cid st i
  cid=$(compose ps -q "$1")
  for i in $(seq 1 "$2"); do
    st=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)
    [ "$st" = healthy ] && return 0
    sleep 3
  done
  return 1
}

if [ "$MODE" = remote ]; then
  # Navidrome на другом сервере: его здесь нет, ждём только свой сервис; связь с тем сервером настраивает приложение
  step api "Жду сервис скачивания"
  wait_healthy api 40 || die API_START "Сервис скачивания не запустился за 2 минуты. Логи: docker compose -f $HUB_DIR/docker-compose.yml logs api"
else
step navidrome "Жду Navidrome"
wait_healthy navidrome 60 || die NAVIDROME_START "Navidrome не запустился за 3 минуты. Логи: docker compose -f $HUB_DIR/docker-compose.yml logs navidrome"

nd_post() { # nd_post <путь> -> HTTP-код
  compose exec -T api python - "$1" "$ND_USER" "$ND_PASS" <<'PY' 2>/dev/null | tail -1
import json, sys, urllib.request, urllib.error
path, user, pw = sys.argv[1:4]
req = urllib.request.Request("http://navidrome:4533" + path, method="POST",
    data=json.dumps({"username": user, "password": pw}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERR")
PY
}
code=$(nd_post /auth/createAdmin || echo ERR)
case $code in
  200) say "Создал пользователя Navidrome «$ND_USER»" ;;
  403)
    if [ "$(nd_post /auth/login || echo ERR)" = 200 ]; then say "Navidrome уже настроен, пароль подходит"
    else warn ND_CREDS "В Navidrome уже есть другой администратор — логин и пароль выше не подойдут. Зайди в Navidrome сам и задай их, либо удали $HUB_DIR/data/navidrome и запусти установку заново"; fi ;;
  *) warn ND_ADMIN "Не удалось создать пользователя Navidrome (ответ: $code)" ;;
esac
fi

# --------------------------------------------------------------------- HTTPS
TLS_OK=false
if [ "$TLS" = auto ]; then
  step tls "Сертификат HTTPS"
  say "Let's Encrypt выпускает сертификат для $ADDR — это до пары минут"
  for i in $(seq 1 40); do
    if curl -fsS -m 8 --resolve "$ADDR:443:127.0.0.1" "https://$ADDR/mb/healthz" >/dev/null 2>&1; then TLS_OK=true; break; fi
    sleep 4
  done
  if [ "$TLS_OK" = false ]; then
    logs=$(compose logs --no-color --tail 80 caddy 2>&1 || true)
    if printf '%s' "$logs" | grep -qi 'rateLimited\|too many certificates'; then
      warn TLS_RATE "Let's Encrypt временно отказал из-за лимита выпусков для $ADDR. Подожди час или используй свой домен"
    elif printf '%s' "$logs" | grep -qi 'timeout during connect\|connection refused\|no route'; then
      warn TLS_PORTS "Let's Encrypt не достучался до сервера. Открой порты 80 и 443 в панели хостера (файрвол/группа безопасности) и запусти установку ещё раз"
    else
      warn TLS_FAIL "Сертификат не выпустился. Логи: docker compose -f $HUB_DIR/docker-compose.yml logs caddy"
    fi
  fi
else
  # API стартует после Navidrome и после пересоздания (обновление) отвечает не сразу — ждём, как и с HTTPS
  HTTP_OK=false
  for i in $(seq 1 30); do
    if curl -fsS -m 5 "http://127.0.0.1:$HTTP_PORT/mb/healthz" >/dev/null 2>&1; then HTTP_OK=true; break; fi
    sleep 2
  done
  if [ "$HTTP_OK" = true ]; then say "Отвечает на порту $HTTP_PORT"
  else warn HTTP_FAIL "Сервис не отвечает на порту $HTTP_PORT. Логи: docker compose -f $HUB_DIR/docker-compose.yml logs api caddy"; fi
fi

step done "Готово"
if [ "$MODE" = remote ]; then
  # Navidrome здесь нет: его адрес и логин приложение берёт у пользователя при настройке передачи
  ND_JSON=null
else
  ND_JSON=$(printf '{"url":"%s","user":"%s","password":"%s"}' "$PUBLIC_URL" "$ND_USER" "$ND_PASS")
fi
printf '::result {"ok":true,"version":"%s","url":"%s","api":"%s/mb","token":"%s","navidrome":%s,"tls":%s,"mode":"%s","dir":"%s","role":"%s","music_dir":"%s"}\n' \
  "$HUB_VERSION" "$PUBLIC_URL" "$PUBLIC_URL" "$HUB_TOKEN" "$ND_JSON" "$TLS_OK" "$TLS" "$HUB_DIR" "$MODE" "$MUSIC_HOST"
