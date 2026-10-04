"""
Самообновление загрузчика. YouTube регулярно ломает скачивание, и чинится это только свежим yt-dlp,
поэтому сервер сам следит за новой версией: раз в час и перед началом каждой загрузки.

Образ собран от root, а сервис работает от пользователя 1000 и системные пакеты менять не может.
Поэтому обновления ставятся в каталог на томе данных (<data>/pydeps), который стоит первым в путях
поиска модулей (см. начало main.py) и переживает пересоздание контейнера. Установка идёт в соседний
каталог и подменяется переименованием: недокачанное обновление рабочую версию не портит.

Уже загруженный в память модуль сам не обновится — после установки сервису нужен перезапуск. Он делается
только когда ничего не качается; очередь лежит на диске и после перезапуска продолжается.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("hub.deps")

DIR_NAME = "pydeps"
STATE_NAME = "deps_state.json"
PACKAGE = "yt-dlp"                    # за его версией следим
# что ставим: сам yt-dlp со всеми его необязательными частями (решатель подписей, curl_cffi и т.п.)
REQUIREMENTS = [r for r in os.environ.get("HUB_DEPS", "yt-dlp[default]").split() if r]
CHECK_EVERY = 3600                    # фоновая проверка
FRESH_FOR = 600                       # перед загрузкой не дёргаем PyPI чаще
PYPI_URL = "https://pypi.org/pypi/{name}/json"


def deps_dir(data_dir: Path) -> Path:
    return Path(data_dir) / DIR_NAME


def activate(data_dir: Path) -> None:
    """Ставит каталог обновлений первым в sys.path. Вызывается до импорта yt_dlp."""
    d = deps_dir(data_dir)
    if d.is_dir() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


def vkey(version: str) -> tuple[int, ...]:
    """'2025.09.26' и '2025.9.26.232845' → сравнимые кортежи."""
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def running_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return ""


async def pypi_latest(name: str = PACKAGE) -> str:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        r = await c.get(PYPI_URL.format(name=name))
        r.raise_for_status()
        return str(r.json()["info"]["version"])


async def pip_install(target: Path, requirements: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--upgrade",
        "--target", str(target), *requirements,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 420)
    except asyncio.TimeoutError:
        p.kill()
        return 1, "pip не уложился в 7 минут"
    return p.returncode or 0, out.decode("utf-8", "replace")[-600:]


class Deps:
    def __init__(self, data_dir: Path, restart: Callable[[], None] | None = None, *,
                 latest: Callable[[], Awaitable[str]] = pypi_latest,
                 install: Callable[[Path, list[str]], Awaitable[tuple[int, str]]] = pip_install,
                 running: Callable[[], str] = running_version,
                 enabled: bool | None = None) -> None:
        self.data_dir = Path(data_dir)
        self._restart = restart
        self._latest, self._install, self._running = latest, install, running
        self.enabled = os.environ.get("HUB_AUTO_UPDATE", "1") != "0" if enabled is None else enabled
        self.restart_needed = False
        self._lock = asyncio.Lock()
        self.running = running()

    # ---- состояние ----
    def _state(self) -> dict:
        try:
            return json.loads((self.data_dir / STATE_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **kw) -> None:
        st = self._state()
        st.update(kw)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / STATE_NAME).write_text(json.dumps(st, ensure_ascii=False), "utf-8")
        except OSError as exc:
            log.warning("не записал состояние обновлений: %s", exc)

    def installed_version(self) -> str:
        """Версия, лежащая на диске (после обновления она новее работающей — до перезапуска)."""
        # читаем имя каталога yt_dlp-<версия>.dist-info сами: importlib.metadata кэширует список по времени
        # изменения каталога и после быстрой подмены отдаёт старый
        d = deps_dir(self.data_dir)
        found = [p.name[len("yt_dlp-"):-len(".dist-info")] for p in d.glob("yt_dlp-*.dist-info")] if d.is_dir() else []
        return max(found, key=vkey) if found else self.running

    def info(self) -> dict:
        st = self._state()
        installed = self.installed_version()
        if vkey(installed) == vkey(self.running):
            installed = self.running              # «2026.8.19» и «2026.08.19» — одна версия, пишем одинаково
        return {"enabled": self.enabled, "package": PACKAGE, "running": self.running,
                "installed": installed, "latest": st.get("latest", ""),
                "checked_at": st.get("checked_at", 0), "updated_at": st.get("updated_at", 0),
                "error": st.get("error", ""), "restart_needed": self.restart_needed}

    # ---- проверка и установка ----
    async def refresh(self, max_age: float = 0, force: bool = False) -> dict:
        """
        Сверяется с PyPI и ставит новую версию, если она вышла. max_age — не проверять, если проверяли
        меньше стольких секунд назад. Не бросает: сеть до PyPI может быть закрыта, скачиванию это не мешает.
        """
        if not self.enabled and not force:
            return self.info()
        async with self._lock:
            st = self._state()
            if not force and max_age and time.time() - st.get("checked_at", 0) < max_age:
                return self.info()
            try:
                latest = await asyncio.wait_for(self._latest(), 15)
            except Exception as exc:
                self._save(checked_at=time.time(), error=f"PyPI недоступен: {type(exc).__name__}")
                return self.info()
            self._save(checked_at=time.time(), latest=latest, error="")
            have = self.installed_version()
            if vkey(latest) <= vkey(have):
                # на диске уже свежее, но работает старое — нужен перезапуск (например, обновили и не успели)
                self.restart_needed = vkey(have) > vkey(self.running)
                return self.info()
            log.info("yt-dlp: вышла %s (стоит %s) — обновляю", latest, have)
            ok, why = await self._update()
            if ok:
                self._save(updated_at=time.time(), error="")
                self.restart_needed = True
                log.info("yt-dlp обновлён до %s, нужен перезапуск сервиса", self.installed_version())
            else:
                self._save(error=f"Обновление не поставилось: {why}"[:400])
                log.warning("yt-dlp не обновился: %s", why)
            return self.info()

    async def _update(self) -> tuple[bool, str]:
        cur = deps_dir(self.data_dir)
        new, old = cur.with_name(DIR_NAME + ".new"), cur.with_name(DIR_NAME + ".old")
        for d in (new, old):
            shutil.rmtree(d, ignore_errors=True)
        new.mkdir(parents=True, exist_ok=True)
        try:
            code, out = await self._install(new, REQUIREMENTS)
        except Exception as exc:
            code, out = 1, f"{type(exc).__name__}: {exc}"
        if code != 0:
            shutil.rmtree(new, ignore_errors=True)
            return False, out.strip().splitlines()[-1] if out.strip() else f"pip завершился с кодом {code}"
        try:
            if cur.exists():
                cur.rename(old)
            new.rename(cur)
        except OSError as exc:
            if old.exists() and not cur.exists():
                old.rename(cur)                     # возвращаем рабочую версию на место
            return False, f"не подменил каталог: {exc}"
        shutil.rmtree(old, ignore_errors=True)
        return True, ""

    def restart_if_needed(self) -> bool:
        """Перезапускает сервис, если обновление ждёт. -> True, если перезапуск начат."""
        if not self.restart_needed or self._restart is None:
            return False
        log.info("перезапуск сервиса: загружаю новый yt-dlp")
        self._restart()
        return True

    async def watch(self, is_idle: Callable[[], bool]) -> None:
        await asyncio.sleep(120)                    # не мешаем запуску
        while True:
            try:
                await self.refresh()
                if is_idle():
                    self.restart_if_needed()
            except Exception as exc:
                log.warning("проверка обновлений не прошла: %s", exc)
            await asyncio.sleep(CHECK_EVERY)
