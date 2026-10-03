#!/usr/bin/env bash
# Останавливает Music Hub. Музыка и данные остаются; --purge удаляет и их.
set -Eeuo pipefail
HUB_DIR=${HUB_DIR:-/opt/music-hub}
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
[ "$(id -u)" = 0 ] || { echo "::error NOT_ROOT Нужен root"; exit 1; }
if [ -f "$HUB_DIR/docker-compose.yml" ]; then
  (cd "$HUB_DIR" && docker compose down $([ $PURGE = 1 ] && echo --volumes) --remove-orphans) || true
fi
if [ $PURGE = 1 ]; then
  # своя папка с музыкой лежит вне каталога установки — её не трогаем никогда
  KEEP=$(sed -n 's/^HUB_MUSIC_DIR=//p' "$HUB_DIR/.env" 2>/dev/null | tail -1)
  rm -rf "$HUB_DIR"
  case "$KEEP" in
    ""|"$HUB_DIR"/*) echo "::log Удалено вместе с музыкой и данными: $HUB_DIR" ;;
    *) echo "::log Удалено: $HUB_DIR. Папка с музыкой не тронута: $KEEP" ;;
  esac
else
  echo "::log Остановлено. Музыка и настройки остались в $HUB_DIR/data"
fi
