#!/usr/bin/env bash
# Только чтение: ничего не ставит и не меняет. Печатает key=value — приложение разбирает
# вывод и решает, что показать до установки (в том числе плашку про запреты).
#   preflight.sh            # от root или от пользователя с sudo без пароля
#   сам sudo проверяется командой `sudo -n true` отдельно, приложением
set -u
export LC_ALL=C

kv() { printf '%s=%s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------ система ------------------------------
kv os "$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}")"
kv os_version "$(. /etc/os-release 2>/dev/null && echo "${VERSION_ID:-}")"
kv kernel "$(uname -s)"
kv arch "$(uname -m)"
kv root "$([ "$(id -u)" = 0 ] && echo yes || echo no)"
kv systemd "$([ -d /run/systemd/system ] && echo yes || echo no)"
kv mem_mb "$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)"
kv disk_free_mb "$(df -Pm /opt 2>/dev/null | awk 'NR==2 {print $4}')"
kv curl "$(have curl && echo yes || echo no)"

# -------------------------------- Docker -------------------------------
if have docker; then
  kv docker "$(docker --version 2>/dev/null | sed 's/Docker version \([^,]*\).*/\1/')"
  if docker compose version >/dev/null 2>&1; then
    kv compose "$(docker compose version --short 2>/dev/null)"
  else
    kv compose none
  fi
  kv docker_running "$(docker info >/dev/null 2>&1 && echo yes || echo no)"
else
  kv docker none
  kv compose none
  kv docker_running no
fi
# уже стоит наш стек?
if [ -f /opt/music-hub/.env ]; then kv installed yes; else kv installed no; fi

# ---------------------------------- порты ---------------------------------
port_state() {
  local port=$1 line
  if have ss; then
    line=$(ss -Hltnp "sport = :$port" 2>/dev/null | head -1)
  elif have netstat; then
    line=$(netstat -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {print; exit}')
  else
    echo unknown; return
  fi
  if [ -z "$line" ]; then echo free; else
    echo "busy:$(printf '%s' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1)"
  fi
}
kv port80 "$(port_state 80)"
kv port443 "$(port_state 443)"
kv port8080 "$(port_state 8080)"

# ---------------------------- сеть и «запреты» --------------------------
if have curl; then
  probe() {  # probe <имя> <url> <ожидаемые коды через |>
    local name=$1 url=$2 ok=$3 out code time
    out=$(curl -sS -o /dev/null -m 8 -w '%{http_code} %{time_total}' "$url" 2>&1)
    local rc=$?
    code=${out%% *}; time=${out##* }
    if [ $rc -eq 0 ]; then
      if printf '%s' "$code" | grep -Eq "^($ok)$"; then kv "net_$name" "ok"; kv "net_${name}_ms" "$(awk -v t="$time" 'BEGIN{printf "%d", t*1000}')"
      elif [ "$code" = 403 ] || [ "$code" = 451 ]; then kv "net_$name" http
      else kv "net_$name" error; fi
    elif [ $rc -eq 28 ]; then kv "net_$name" timeout
    elif [ $rc -eq 35 ] || [ $rc -eq 56 ] || [ $rc -eq 52 ] || [ $rc -eq 7 ]; then kv "net_$name" reset
    else kv "net_$name" error; fi
  }
  ip=$(curl -fsS -m 6 https://ipinfo.io/ip 2>/dev/null || curl -fsS -m 6 https://api.ipify.org 2>/dev/null || true)
  country=$(curl -fsS -m 6 https://ipinfo.io/country 2>/dev/null || curl -fsS -m 6 https://ifconfig.co/country-iso 2>/dev/null || true)
  kv ip "$(printf '%s' "$ip" | tr -d '[:space:]')"
  kv country "$(printf '%s' "$country" | tr -d '[:space:]' | tr 'a-z' 'A-Z')"
  probe youtube    https://www.youtube.com/generate_204 '200|204'
  probe deezer     https://api.deezer.com/infos '200'
  probe soundcloud https://soundcloud.com/ '200'
  # то, без чего не поставится сам стек
  probe registry   https://registry-1.docker.io/v2/ '200|401'
  probe ghcr       https://ghcr.io/v2/ '200|401'
  probe getdocker  https://get.docker.com/ '200'
fi
kv done yes
