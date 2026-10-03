#!/usr/bin/env python3
"""
Настройка передачи музыки на другой сервер (где стоит Navidrome).

Запускается приложением внутри контейнера api:
    docker compose exec -T api python remote_setup.py            # параметры — JSON в stdin

В stdin: {"host","port","user","password","music_dir","nd_url","nd_user","nd_pass"}.
Пароль сервера Navidrome приходит ТОЛЬКО так (по уже открытому SSH до этого сервера) — не в аргументах
(их видно в списке процессов) и не по HTTP. Нигде не сохраняется: см. remote.setup().

Вывод — тот же протокол, что у install.sh: ::step / ::log / ::warn КОД текст / ::error КОД текст /
::result {json}. Код выхода 0 — всё готово.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import remote


def main() -> int:
    n = 0

    def say(kind: str, text: str) -> None:
        nonlocal n
        if kind == "step":
            n += 1
            print(f"::step remote{n} {text}", flush=True)
        elif kind == "warn":
            print(f"::warn REMOTE_WARN {text}", flush=True)
        else:
            print(f"::log {text}", flush=True)

    try:
        params = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        print("::error REMOTE_ARGS Параметры настройки не прочитались", flush=True)
        return 1
    data_dir = Path(os.environ.get("HUB_DATA", "/data"))
    try:
        info = asyncio.run(remote.setup(data_dir, params, say))
    except remote.RemoteError as exc:
        print(f"::error {exc.code} {exc.text}", flush=True)
        return 1
    except Exception as exc:                      # неожиданное — но без трассировки с параметрами
        print(f"::error REMOTE_UNEXPECTED {type(exc).__name__}: {exc}", flush=True)
        return 1
    print("::result " + json.dumps({"ok": True, **info}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
