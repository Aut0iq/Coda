"""Проверка запретов: достаёт ли этот сервер до того, что нужно для скачивания.

Проверяем с самого сервера (контейнер выходит в сеть тем же путём, что и yt-dlp):
  * страна сервера по IP;
  * YouTube — отвечает ли вообще;
  * YouTube — скорость отдачи аудио с googlevideo (в РФ её режут, и качать
    становится невозможно, хотя страница открывается);
  * Deezer (поиск и теги) и SoundCloud (запасной источник).

Результат — не готовый текст, а коды: тексты плашки живут в приложении. Сервер
говорит «что случилось и что посоветовать» (vpn, zapret, cookies), приложение
подбирает слова.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("hub.netcheck")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TEST_VIDEO = "jNQXAC9IVRw"           # «Me at the zoo», 19 секунд, лежит с 2005 года
SPEED_BYTES = 1_500_000
SLOW_KBIT = 800                      # ниже — считаем, что YouTube режут
PROBE_TIMEOUT = 8
RECHECK_HOURS = 6

GEO_PROVIDERS = (
    ("https://ipinfo.io/json", lambda j: (j.get("ip"), j.get("country"))),
    ("https://api.country.is/", lambda j: (j.get("ip"), j.get("country"))),
    ("https://ipapi.co/json/", lambda j: (j.get("ip"), j.get("country_code"))),
)

PROBES = (
    ("youtube", "YouTube", "https://www.youtube.com/generate_204", {200, 204}),
    ("deezer", "Deezer", "https://api.deezer.com/infos", {200}),
    ("soundcloud", "SoundCloud", "https://soundcloud.com/", {200}),
)

# состояния проверки: ok | slow | timeout | reset | http | bot_check | geo | error
BAD_HTTP = {403, 451}


async def geo_lookup(client: httpx.AsyncClient) -> dict:
    for url, pick in GEO_PROVIDERS:
        try:
            r = await client.get(url, timeout=6)
            r.raise_for_status()
            ip, country = pick(r.json())
            if country:
                return {"ip": ip or "", "country": str(country).upper()}
        except Exception as exc:
            log.debug("геолокация %s: %s", url, exc)
    return {"ip": "", "country": ""}


def classify_exc(exc: Exception) -> str:
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout,
                        httpx.WriteTimeout, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError,
                        httpx.WriteError)):
        # сброс соединения или сбой TLS при живой сети — типичный след блокировки по SNI
        return "reset"
    return "error"


async def probe_http(client: httpx.AsyncClient, pid: str, name: str, url: str,
                     ok_codes: set[int]) -> dict:
    t0 = time.monotonic()
    try:
        r = await client.get(url, timeout=PROBE_TIMEOUT)
    except Exception as exc:
        return {"id": pid, "name": name, "state": classify_exc(exc),
                "ms": int((time.monotonic() - t0) * 1000), "detail": type(exc).__name__}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code in ok_codes:
        state = "ok"
    elif r.status_code in BAD_HTTP:
        state = "http"
    else:
        state = "error"
    return {"id": pid, "name": name, "state": state, "ms": ms, "detail": f"HTTP {r.status_code}"}


# ------------------------------ скорость YouTube ------------------------------
def _ytdlp_state(msg: str) -> str:
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return "bot_check"
    if "not available in your country" in low or "blocked it in your country" in low:
        return "geo"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if any(w in low for w in ("connection reset", "connection refused", "unable to download",
                              "name resolution", "ssl", "eof occurred")):
        return "reset"
    return "error"


def speed_probe_sync(proxy: str = "") -> dict:
    """Достаёт ссылку на аудио тестового ролика через yt-dlp и тянет первые ~1.5 МБ."""
    from yt_dlp import YoutubeDL

    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "format": "bestaudio/best", "socket_timeout": 12, "retries": 1,
            "extractor_retries": 1}
    if proxy:
        opts["proxy"] = proxy
    t0 = time.monotonic()
    try:
        with YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={TEST_VIDEO}", download=False)
        url = info["url"]
        headers = dict(info.get("http_headers") or {"User-Agent": UA})
    except Exception as exc:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines()[0][:200]
        return {"id": "youtube_speed", "name": "Скорость YouTube", "state": _ytdlp_state(msg),
                "ms": int((time.monotonic() - t0) * 1000), "detail": msg}

    got, started = 0, None
    try:
        with httpx.Client(proxy=proxy or None, timeout=httpx.Timeout(10, read=10),
                          follow_redirects=True) as c:
            with c.stream("GET", f"{url}&range=0-{SPEED_BYTES - 1}", headers=headers) as r:
                if r.status_code in BAD_HTTP:
                    return {"id": "youtube_speed", "name": "Скорость YouTube", "state": "http",
                            "ms": 0, "detail": f"HTTP {r.status_code}"}
                r.raise_for_status()
                started = time.monotonic()
                deadline = started + 15
                for chunk in r.iter_bytes(65536):
                    got += len(chunk)
                    if got >= SPEED_BYTES or time.monotonic() > deadline:
                        break
    except Exception as exc:
        if got < 50_000:
            return {"id": "youtube_speed", "name": "Скорость YouTube",
                    "state": classify_exc(exc), "ms": 0, "detail": type(exc).__name__}
    elapsed = max(time.monotonic() - (started or time.monotonic()), 0.05)
    kbit = int(got * 8 / 1000 / elapsed)
    state = "ok" if kbit >= SLOW_KBIT else "slow"
    return {"id": "youtube_speed", "name": "Скорость YouTube", "state": state,
            "ms": int(elapsed * 1000), "kbit": kbit, "detail": f"{kbit} кбит/с"}


# --------------------------------- вердикт ----------------------------------
HARD = {"timeout", "reset", "http"}


def evaluate(geo: dict, checks: list[dict], proxy_ok: bool = False) -> dict:
    """Чистая функция: из результатов проверок — вердикт и советы.

    level: ok        — всё доступно (или недоступное закрывает прокси из настроек);
           limited   — работает, но медленно или с оговорками (бот-проверка YouTube, замедление);
           blocked   — что-то нужное не открывается вовсе.
    advice: vpn / zapret / cookies / proxy — коды, тексты подбирает приложение.
    """
    ru = geo.get("country") == "RU"
    issues = [c for c in checks if c["state"] != "ok"]
    out = {"country": geo.get("country", ""), "ip": geo.get("ip", ""), "ru": ru,
           "proxy_ok": proxy_ok, "checks": checks, "issues": [c["id"] for c in issues]}
    if not issues or proxy_ok:
        out.update(level="ok", advice=[])
        return out

    blocked = any(c["state"] in HARD for c in issues if c["id"] in ("youtube", "deezer"))
    out["level"] = "blocked" if blocked else "limited"

    advice = []
    bot_only = all(c["state"] == "bot_check" for c in issues if c["id"].startswith("youtube"))
    only_bot = bool(issues) and all(c["state"] == "bot_check" for c in issues)
    if only_bot:
        # адрес дата-центра, а не запрет: помогают cookies аккаунта или прокси с другим IP
        advice = ["cookies", "proxy"]
    else:
        advice = ["vpn"]
        if ru:
            advice.append("zapret")
        if not bot_only and any(c["state"] == "bot_check" for c in issues):
            advice.append("cookies")
    out["advice"] = advice
    return out


# ------------------------------------ запуск ---------------------------------
async def run_checks(proxies: list[str] | None = None) -> dict:
    headers = {"User-Agent": UA}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        geo_task = asyncio.create_task(geo_lookup(client))
        probes = [probe_http(client, pid, name, url, ok) for pid, name, url, ok in PROBES]
        speed = asyncio.wait_for(asyncio.to_thread(speed_probe_sync, ""), 60)
        results = await asyncio.gather(*probes, speed, return_exceptions=True)
        geo = await geo_task

    checks = []
    for r in results:
        if isinstance(r, Exception):
            checks.append({"id": "youtube_speed", "name": "Скорость YouTube",
                           "state": classify_exc(r), "ms": 0, "detail": type(r).__name__})
        else:
            checks.append(r)

    # если напрямую YouTube не отдаёт, а прокси из настроек есть — смотрим, спасает ли он
    proxy_ok = False
    yt_bad = any(c["id"].startswith("youtube") and c["state"] != "ok" for c in checks)
    if yt_bad and proxies:
        for p in proxies:
            try:
                r = await asyncio.wait_for(asyncio.to_thread(speed_probe_sync, p), 60)
            except Exception:
                continue
            if r["state"] == "ok":
                proxy_ok = True
                break
    # Deezer/SoundCloud прокси не покрывает — вердикт «ok» только если плохо одно лишь YouTube
    other_bad = any(not c["id"].startswith("youtube") and c["state"] != "ok" for c in checks)
    return evaluate(geo, checks, proxy_ok=proxy_ok and not other_bad)


class NetCheck:
    """Хранит последний результат, умеет запускаться в фоне и раз в несколько часов."""

    def __init__(self, path: Path, proxies_getter=lambda: []) -> None:
        self.path = path
        self.proxies_getter = proxies_getter
        self.running = False
        self.checked_at = 0.0
        self.result: dict | None = None
        self._task: asyncio.Task | None = None
        try:
            raw = json.loads(path.read_text("utf-8"))
            self.result, self.checked_at = raw.get("result"), raw.get("checked_at", 0.0)
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {"running": self.running, "checked_at": self.checked_at, "result": self.result}

    def start(self) -> bool:
        """Запускает проверку в фоне. False — уже идёт."""
        if self.running:
            return False
        self.running = True
        self._task = asyncio.get_running_loop().create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            self.result = await run_checks(list(self.proxies_getter()))
            self.checked_at = time.time()
            log.info("проверка запретов: %s, страна %s, проблемы: %s", self.result["level"],
                     self.result["country"] or "?", ",".join(self.result["issues"]) or "нет")
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"result": self.result, "checked_at": self.checked_at}),
                               "utf-8")
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("не записал %s: %s", self.path, exc)
        except Exception:
            log.exception("проверка запретов упала")
        finally:
            self.running = False

    async def watch(self) -> None:
        await asyncio.sleep(20)               # не мешаем запуску
        while True:
            if time.time() - self.checked_at > RECHECK_HOURS * 3600:
                self.start()
            await asyncio.sleep(1800)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
