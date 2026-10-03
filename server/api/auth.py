"""Доступ по токену.

Токен создаёт установщик и отдаёт приложению по SSH — по сети он до первого
запроса не ездит. Дальше приложение шлёт его в Authorization: Bearer.
Неверные попытки считаем по адресу: подбор токена упирается в 429.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

OPEN_PATHS = {"/api/img"}          # <img> не умеет слать заголовки; хосты ограничены белым списком
MAX_FAILS = 10
WINDOW = 600                       # секунд, за которые считаем неудачи
BLOCK = 300                        # на сколько запираем


def load_token(path: Path) -> str:
    """HUB_TOKEN из окружения, иначе файл, иначе создаём новый."""
    env = os.environ.get("HUB_TOKEN", "").strip()
    if env:
        return env
    try:
        tok = path.read_text("utf-8").strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tok, "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return tok


class Throttle:
    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}

    def locked(self, who: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        until = self.blocked.get(who, 0)
        if until > now:
            return True
        self.blocked.pop(who, None)
        return False

    def fail(self, who: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        hits = [t for t in self.fails.get(who, []) if now - t < WINDOW] + [now]
        self.fails[who] = hits
        if len(hits) >= MAX_FAILS:
            self.blocked[who] = now + BLOCK
            self.fails.pop(who, None)
        if len(self.fails) > 2000:                 # не растём без предела
            self.fails = {k: v for k, v in self.fails.items() if now - v[-1] < WINDOW}

    def ok(self, who: str) -> None:
        self.fails.pop(who, None)


def client_ip(request: web.Request) -> str:
    # API торчит только в сеть compose, к нему ходит один Caddy — он и ставит X-Forwarded-For
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "?"


def make_middleware(token: str, throttle: Throttle | None = None):
    throttle = throttle or Throttle()
    want = token.encode()

    @web.middleware
    async def auth_mw(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/") or path in OPEN_PATHS:
            return await handler(request)
        who = client_ip(request)
        if throttle.locked(who):
            return web.json_response({"error": "too_many_attempts"}, status=429,
                                     headers={"Retry-After": str(BLOCK)})
        header = request.headers.get("Authorization", "")
        got = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not got or not hmac.compare_digest(got.encode(), want):
            throttle.fail(who)
            return web.json_response({"error": "unauthorized"}, status=401)
        throttle.ok(who)
        return await handler(request)

    return auth_mw
