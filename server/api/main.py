#!/usr/bin/env python3
"""
music-hub API
=============
Серверная часть приложения: поиск через Deezer, скачивание с YouTube (yt-dlp) с
точным подбором по длительности/названию/каналу, теги и обложки из Deezer,
библиотека кладётся прямо в папку Navidrome (общий том), потом скан и плейлисты.

Это движок бывшего Telegram-бота без Telegram: клиентом стало мобильное
приложение, доступ — по токену (см. auth.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
import time
import urllib.parse
from pathlib import Path

import httpx
from aiohttp import web

VERSION = "0.1.1"
API_LEVEL = 1                   # растёт, когда API ломает совместимость с приложением

DATA_DIR = Path(os.environ.get("HUB_DATA", "/data"))
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")

import deps as deps_mod  # noqa: E402

# обновления yt-dlp лежат на томе данных и должны найтись раньше версии из образа — до любого импорта yt_dlp
deps_mod.activate(DATA_DIR)
# до импорта движка: пути, которые он читает из окружения при загрузке
os.environ.setdefault("COOKIE_FILE", str(DATA_DIR / "cookies.txt"))
os.environ.setdefault("LIBRARY_INDEX", str(DATA_DIR / "library.json"))
os.environ.setdefault("MB_TMP_DIR", str(Path(MUSIC_DIR) / ".incoming"))

import remote as remote_mod  # noqa: E402

# Navidrome на другом сервере: его адрес и логин берём из настройки передачи (remote.json) — движок
# читает ND_* при загрузке, поэтому до импорта playlists/library
_rc = remote_mod.load_config(DATA_DIR)
if _rc and _rc.get("nd_url"):
    os.environ["ND_URL"] = _rc["nd_url"]
    os.environ["ND_USER"] = _rc.get("nd_user", "")
    os.environ["ND_PASS"] = _rc.get("nd_pass", "")

import settings as settings_mod  # noqa: E402

settings_mod.Settings(DATA_DIR / "settings.json").apply_env()

import auth  # noqa: E402
import cookies as cookies_mod  # noqa: E402
import downloader  # noqa: E402
import library  # noqa: E402
import netcheck  # noqa: E402
import playlists  # noqa: E402
import sources  # noqa: E402
from sources import get_album_job, get_albums, search_artists  # noqa: E402
from store import Store, _light  # noqa: E402

HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "500"))
REMOTE_RETRY_SEC = 300             # как часто пробовать дослать музыку на сервер Navidrome
WEB_ROOT = Path(__file__).resolve().parent / "webapp"
IMG_HOSTS = {"cdn-images.dzcdn.net", "e-cdns-images.dzcdn.net", "e-cdn-images.dzcdn.net"}
COOKIE_CHECK_HOURS = float(os.environ.get("COOKIE_CHECK_HOURS", "24"))
RENOTIFY_DAYS = 7               # пока не обновили cookies — напоминаем не чаще раза в неделю
PLAN_TTL = 300
PLAN_TIMEOUT = 20
RESOLVE_TTL = 600

log = logging.getLogger("hub")


def _sweep(cache: dict, ttl: int) -> None:
    now = time.time()
    for k in [k for k, (ts, _) in cache.items() if now - ts > ttl]:
        cache.pop(k, None)


def _item_key(it: dict) -> str:
    return f"{it.get('nn') or 0}|{it.get('title') or it.get('video_id', '')}"


# ================================== состояние ==================================
class Hub:
    """Всё, что нужно обработчикам: очередь, настройки, проверка сети, cookies."""

    def __init__(self, data_dir: Path = DATA_DIR, music_dir: str = MUSIC_DIR) -> None:
        self.data_dir = data_dir
        self.music_dir = music_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = settings_mod.Settings(data_dir / "settings.json")
        self.concurrency = self.cfg.apply_live(downloader, library, playlists)
        self.store = Store(data_dir / "state.json", HISTORY_LIMIT)
        self.remote = remote_mod.Remote(data_dir)
        self.deps = deps_mod.Deps(data_dir, restart=self.restart)
        self._remote_failing = False
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.net = netcheck.NetCheck(data_dir / "netcheck.json",
                                     lambda: self.cfg.values["proxies"])
        self.cookie_state_file = data_dir / "cookies_state.json"
        self.cookie_status: dict = {"status": "", "checked_at": 0, "why": ""}
        self.plan_cache: dict[str, tuple[float, dict]] = {}
        self.resolved: dict[str, tuple[float, dict]] = {}
        self.started = time.time()
        self.tasks: list[asyncio.Task] = []
        # папка для недокачанных файлов: внутри библиотеки (одна ФС → атомарный rename),
        # но с точки в имени и .ndignore, чтобы Navidrome её не сканировал
        tmp = Path(downloader.TMP_DIR) if downloader.TMP_DIR else None
        if tmp:
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / ".ndignore").touch()

    # ----------------------------- Navidrome -----------------------------
    @property
    def navidrome_ready(self) -> bool:
        return bool(playlists.ND_URL and playlists.ND_USER and playlists.ND_PASS)

    async def scan(self) -> str:
        if not self.navidrome_ready:
            return ""
        salt = secrets.token_hex(8)
        token = hashlib.md5((playlists.ND_PASS + salt).encode()).hexdigest()
        params = {"u": playlists.ND_USER, "t": token, "s": salt, "v": "1.16.1",
                  "c": "music-hub", "f": "json"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(playlists.ND_URL.rstrip("/") + "/rest/startScan.view", params=params)
                r.raise_for_status()
            return "🔄 Navidrome: пересканирование запущено."
        except Exception as exc:
            return f"⚠️ Скан Navidrome не запустился: {exc}"

    # ----------------------- обновление загрузчика -----------------------
    @staticmethod
    def restart() -> None:
        """Мягко завершает сервис: Docker поднимет его заново (restart: unless-stopped), уже с новым yt-dlp."""
        os.kill(os.getpid(), signal.SIGTERM)

    async def update_before_job(self) -> bool:
        """Перед загрузкой: вышел новый yt-dlp — ставим и перезапускаемся. -> True, если начат перезапуск
        (задача при этом остаётся в очереди на диске и продолжится после запуска)."""
        try:
            await self.deps.refresh(max_age=deps_mod.FRESH_FOR)
        except Exception as exc:
            log.warning("проверка обновлений перед загрузкой не прошла: %s", exc)
            return False
        return self.deps.restart_if_needed()

    # ------------------- передача на сервер Navidrome -------------------
    @property
    def role(self) -> str:
        return "remote" if self.remote.configured else "local"

    async def push_remote(self, job: dict | None = None, min_age: float = 0.0) -> dict | None:
        """Переносит готовое из промежуточной папки на сервер Navidrome. Если он не настроен — None."""
        if not self.remote.configured:
            return None

        def progress(done: int, total: int, nbytes: int, total_bytes: int) -> None:
            if job is not None:
                job["upload"] = {"done": done, "total": total, "bytes": nbytes, "total_bytes": total_bytes}
                self.store.touch()

        res = await self.remote.sync(Path(self.music_dir), progress=progress, min_age=min_age)
        if job is not None:
            job.pop("upload", None)
            job["uploaded"] = res["files"]
        if res.get("error"):
            if not self._remote_failing:             # сообщаем при смене состояния, а не при каждой попытке
                self.store.notify("⚠️ Не получается передать музыку на сервер Navidrome: "
                                  f"{res['error']} Файлы лежат на этом сервере и уедут сами, когда связь появится.", "remote")
            self._remote_failing = True
            log.warning("передача на сервер Navidrome: %s", res["error"])
        else:
            if self._remote_failing and res["files"]:
                self.store.notify("✅ Связь с сервером Navidrome восстановилась, накопившаяся музыка передана.", "remote")
            self._remote_failing = False
        return res

    async def remote_watch(self) -> None:
        """Досылает то, что не уехало сразу (сервер был недоступен, перезапуск посреди передачи)."""
        await asyncio.sleep(30)
        while True:
            try:
                if self.remote.configured and not self.store.active:
                    res = await self.push_remote(None, min_age=2.0)
                    if res and res["files"]:
                        await self.scan()
            except Exception as exc:
                log.warning("досылка на сервер Navidrome не прошла: %s", exc)
            await asyncio.sleep(REMOTE_RETRY_SEC)

    # ------------------------------ очередь ------------------------------
    def enqueue(self, job: dict) -> int:
        self.store.add(job)
        self.queue.put_nowait(job["id"])
        return len(self.store.pending)

    async def process_job(self, job: dict) -> None:
        store = self.store
        job["stage"] = "analyze"
        store.touch()
        # продолжение после перезапуска: уже скачанное не трогаем и дублями не считаем
        done_before = set(job.get("done_keys") or [])
        if done_before:
            job["items"] = [i for i in job["items"] if _item_key(i) not in done_before]
        items, dupes, lives = await library.filter_items(job)
        job["items"] = items
        job["skipped_dupes"] = (job.get("skipped_dupes") or []) + dupes
        job["skipped_live"] = (job.get("skipped_live") or []) + lives
        job["total"] = len(done_before) + len(items)
        store.save_queue()
        store.touch()

        if not items:
            job["stage"] = "done"
            store.touch()
            return

        job["stage"] = "downloading"
        store.touch()
        covers: dict[str, bytes | None] = {}
        for it in items:
            u = it.get("cover_url")
            if u and u not in covers:
                covers[u] = await downloader.get_cover(u)

        sem = asyncio.Semaphore(self.concurrency)
        running: list[str] = []

        async def one(it: dict) -> None:
            async with sem:
                if job.get("cancel"):        # остановлено: начатое доскачаем, новое — нет
                    job["skipped_cancel"] = job.get("skipped_cancel", 0) + 1
                    return
                label = f"{it['artist']} — {it['title']}" if it.get("title") else it.get("video_id", "")
                running.append(label)
                job["now"] = label
                store.touch()
                try:
                    ok, why = await asyncio.to_thread(
                        downloader.fetch_sync, it, self.music_dir, covers.get(it.get("cover_url")))
                except Exception as exc:
                    ok, why = False, str(exc)[:200]
                running.remove(label)
                if ok:
                    job["done"] += 1
                    job.setdefault("done_keys", []).append(_item_key(it))
                    # чем взяли трек, если не обычным YouTube
                    if why == "soundcloud":
                        job.setdefault("via", {}).setdefault("soundcloud", []).append(label)
                    elif why.startswith("proxy"):    # proxy — первый прокси, proxy2 — второй…
                        job.setdefault("via", {}).setdefault(why, []).append(label)
                    elif why.endswith("(18+)") or why.endswith("(cookies)"):
                        job.setdefault("via", {}).setdefault("cookies", []).append(label)
                    library.remember(it)
                else:
                    job["failed"] += 1
                    job["failed_tracks"].append(f"{it.get('nn') or '·'}. {it.get('title') or label} — {why}")
                    log.warning("НЕ СКАЧАН: %s (%s)", label, why)
                job["now"] = running[-1] if running else None
                store.save_queue()           # после каждого трека: перезапуск ничего не потеряет
                store.touch()

        await asyncio.gather(*(one(it) for it in items))
        job["now"] = None

        # cover.jpg в папку альбома — Navidrome и плееры берут его охотнее встроенной
        for it in items:
            data = covers.get(it.get("cover_url"))
            if data:
                folder = downloader.dest_path(self.music_dir, it).parent
                if folder.is_dir() and not (folder / "cover.jpg").exists():
                    tmp_cover = folder / ".cover.jpg.tmp"       # атомарно: недописанный файл не должен уехать на другой сервер
                    tmp_cover.write_bytes(data)
                    tmp_cover.replace(folder / "cover.jpg")

        if job["done"] == 0:
            job["stage"] = "done"
            store.touch()
            return

        if self.remote.configured:
            job["stage"] = "upload"
            store.touch()
            up = await self.push_remote(job)
            if up and up.get("error") and not up["files"]:
                # Navidrome ничего нового не получил — сканировать пока нечего; файлы дошлёт remote_watch
                job["scan"] = "⏳ Музыка скачана и ждёт передачи на сервер Navidrome."
                job["stage"] = "done"
                store.touch()
                return
        job["stage"] = "scan"
        store.touch()
        job["scan"] = await self.scan()
        job["stage"] = "playlists"
        store.touch()
        if job["kind"] == "album":
            job["playlists"] = await playlists.sync_artist(job["album_artist"])
        else:
            job["playlists"] = await playlists.sync_recent()
        job["stage"] = "done"
        store.touch()

    async def worker(self) -> None:
        store = self.store
        while True:
            job_id = await self.queue.get()
            if any(j["id"] == job_id for j in store.pending) and await self.update_before_job():
                return                           # сервис перезапускается, очередь продолжится после него
            job = store.start(job_id)
            if job is None:                  # отменили, пока стояла в очереди
                self.queue.task_done()
                continue
            try:
                await self.process_job(job)
                if job["done"] == 0 and job["failed"]:
                    job["error"] = (job["failed_tracks"] or ["ничего не скачалось"])[0][:300]
                    status = "error"
                elif job["done"] == 0 and not job["total"]:
                    status = "done"          # всё уже было в библиотеке
                else:
                    status = "done" if not job["failed"] else "partial"
            except Exception as exc:
                log.exception("job failed")
                job["error"] = str(exc)[:300]
                status = "error"
            store.finish(status)
            self.queue.task_done()

    # ------------------------------- cookies -------------------------------
    def _cookie_state(self) -> dict:
        try:
            return json.loads(self.cookie_state_file.read_text("utf-8"))
        except Exception:
            return {}

    def _save_cookie_state(self, state: dict) -> None:
        try:
            self.cookie_state_file.write_text(json.dumps(state), "utf-8")
        except OSError as exc:
            log.warning("не записал %s: %s", self.cookie_state_file, exc)

    async def cookie_check(self) -> tuple[str, str]:
        """Проверяет cookies и оставляет заметку в приложении, если они протухли."""
        status, why = await asyncio.to_thread(downloader.check_cookies)
        prev, now = self._cookie_state(), time.time()
        state = {"status": status, "checked_at": now, "notified_at": prev.get("notified_at", 0)}
        say = ""
        if status == "invalid":
            stale = now - prev.get("notified_at", 0) > RENOTIFY_DAYS * 86400
            if prev.get("status") != "invalid" or stale:
                say = ("🔞 Cookies YouTube устарели — треки 18+ пока качаться не будут. "
                       "Загрузи свежий экспорт cookies.txt в настройках.")
        elif status == "ok" and prev.get("status") == "invalid":
            say = "✅ Cookies снова работают — треки 18+ качаются."
        if say:
            state["notified_at"] = now
            self.store.notify(say, "cookies")
        self._save_cookie_state(state)
        self.cookie_status.update(state, why=why)
        log.info("cookies: %s %s", status, why)
        return status, why

    async def cookie_watch(self) -> None:
        await asyncio.sleep(90)              # не мешаем запуску
        while True:
            try:
                await self.cookie_check()
            except Exception as exc:
                log.warning("проверка cookies не прошла: %s", exc)
            await asyncio.sleep(COOKIE_CHECK_HOURS * 3600)

    def cookies_info(self) -> dict:
        have = downloader.have_cookies()
        age = None
        if have:
            try:
                age = round((time.time() - os.path.getmtime(downloader.COOKIE_FILE)) / 86400, 1)
            except OSError:
                pass
        return {"have": have, "age_days": age, "status": self.cookie_status.get("status", ""),
                "why": self.cookie_status.get("why", ""),
                "checked_at": self.cookie_status.get("checked_at", 0)}

    # ------------------------------- план -------------------------------
    async def plan_for(self, job: dict, cache_key: str = "") -> dict:
        """Что будет с каждым треком: скачаем, уже есть или live. Если Navidrome
        молчит — отдаём «план неизвестен», карточка всё равно должна открыться."""
        _sweep(self.plan_cache, PLAN_TTL)
        if cache_key and (hit := self.plan_cache.get(cache_key)):
            return hit[1]
        items = job.get("items") or []
        try:
            rows = await asyncio.wait_for(library.analyze(job), PLAN_TIMEOUT)
        except Exception as exc:
            log.warning("план для «%s» не собрался: %s", job.get("album_name"), exc)
            return {
                "unknown": True, "keep": len(items), "dupes": 0, "lives": 0,
                "live_album": bool(job.get("live_album")),
                "tracks": [{"nn": i.get("nn") or 0, "title": i.get("title", ""),
                            "state": "keep", "where": ""} for i in items],
                "not_found": job.get("not_found") or [],
            }
        plan = {
            "unknown": False,
            "keep": sum(r["state"] == "keep" for r in rows),
            "dupes": sum(r["state"] == "dupe" for r in rows),
            "lives": sum(r["state"] == "live" for r in rows),
            "live_album": bool(job.get("live_album")),
            "tracks": [{"nn": r["nn"], "title": r["title"], "state": r["state"],
                        "where": r["where"]} for r in rows],
            "not_found": job.get("not_found") or [],
        }
        if cache_key:
            self.plan_cache[cache_key] = (time.time(), plan)
        return plan

    def sheet(self, job: dict) -> dict:
        """Карточка релиза для интерфейса."""
        return {
            "id": job.get("album_id"),
            "kind": job["kind"],
            "artist": job["album_artist"],
            "title": job["album_name"],
            "year": job.get("year", ""),
            "cover": job.get("cover", ""),
            "total": job.get("total_raw") or job["total"],
            "tracks": job.get("track_titles") or [i.get("title", "") for i in job.get("items", [])],
            "owned": self.store.has_album(str(job.get("album_id"))) if job.get("album_id") else False,
        }

    # ------------------------------- жизненный цикл -------------------------------
    def start_background(self) -> None:
        loop = asyncio.get_running_loop()
        for job in self.store.pending:       # восстановленные после перезапуска
            self.queue.put_nowait(job["id"])
        self.tasks = [loop.create_task(self.worker()),
                      loop.create_task(self.cookie_watch()),
                      loop.create_task(self.net.watch()),
                      loop.create_task(self.remote_watch()),
                      loop.create_task(self.deps.watch(lambda: not self.store.active and not self.store.pending))]

    async def stop_background(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.net.stop()
        await asyncio.gather(*self.tasks, return_exceptions=True)


HUB_KEY = web.AppKey("hub", Hub)
BG_KEY = web.AppKey("bg", set)


def hub_of(request: web.Request) -> Hub:
    return request.app[HUB_KEY]


def bad(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


async def body_json(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "нужен JSON"}),
                                 content_type="application/json") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "нужен JSON-объект"}),
                                 content_type="application/json")
    return data


# ================================== обработчики ==================================
async def api_info(request: web.Request) -> web.Response:
    """Проверка связи и токена: приложение зовёт это первым делом."""
    h = hub_of(request)
    return web.json_response({
        "name": "music-hub", "version": VERSION, "api": API_LEVEL,
        "uptime": int(time.time() - h.started),
        "navidrome": h.navidrome_ready,
        "role": h.role,
        "ytdlp": h.deps.running,                       # local — Navidrome здесь же; remote — музыка уходит на другой сервер
    })


async def api_deps_get(request: web.Request) -> web.Response:
    return web.json_response(hub_of(request).deps.info())


async def api_deps_check(request: web.Request) -> web.Response:
    """«Проверить обновления»: сверяется с PyPI сейчас; перезапуск — только если ничего не качается."""
    h = hub_of(request)
    info = await h.deps.refresh(force=True)
    idle = not h.store.active and not h.store.pending
    if info["restart_needed"] and idle:
        info["restarting"] = True
        asyncio.get_running_loop().call_later(1.0, h.deps.restart_if_needed)   # сначала ответим приложению
    return web.json_response(info)


async def api_remote_get(request: web.Request) -> web.Response:
    h = hub_of(request)
    return web.json_response(h.remote.info(Path(h.music_dir)))


async def api_remote_test(request: web.Request) -> web.Response:
    return web.json_response(await hub_of(request).remote.test())


async def api_remote_sync(request: web.Request) -> web.Response:
    """«Передать сейчас»: досылка того, что накопилось у нас."""
    h = hub_of(request)
    if not h.remote.configured:
        return bad("передача на другой сервер не настроена")
    res = await h.push_remote(None)
    scan = await h.scan() if res and res["files"] else ""
    return web.json_response({"ok": not (res or {}).get("error"), "files": (res or {}).get("files", 0),
                              "error": (res or {}).get("error"), "scan": scan})


async def api_search(request: web.Request) -> web.Response:
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"artists": []})
    try:
        artists = await search_artists(q, limit=10)
    except Exception as exc:
        return bad(str(exc), 502)
    return web.json_response({"artists": [
        {"id": str(a["id"]), "name": a["name"], "picture": a.get("picture_medium") or "",
         "fans": a.get("nb_fan", 0)} for a in artists]})


FIND_ARTISTS = 8                # для стольких исполнителей сверяемся с Navidrome за один поиск


async def api_find(request: web.Request) -> web.Response:
    """Поиск песен и альбомов для плеера: что из найденного уже лежит в библиотеке."""
    h = hub_of(request)
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"tracks": [], "albums": []})
    try:
        tracks, albums = await sources.search_catalog(q)
    except Exception as exc:
        return bad(str(exc), 502)

    def who(x: dict) -> str:
        return (x.get("artist") or {}).get("name") or ""

    artists = [a for a in dict.fromkeys(who(x) for x in tracks + albums) if a][:FIND_ARTISTS]
    known = await library.library_for(set(artists))
    have_albums = {library._norm(v) for v in known.values() if v}
    return web.json_response({
        "tracks": [{
            "id": str(t["id"]), "title": t.get("title") or "", "artist": who(t),
            "album": (t.get("album") or {}).get("title") or "",
            "album_id": str((t.get("album") or {}).get("id") or ""),
            "duration": int(t.get("duration") or 0),
            "cover": (t.get("album") or {}).get("cover_medium") or "",
            "owned": library.song_key(who(t), t.get("title") or "") in known,
        } for t in tracks],
        "albums": [{
            "id": str(a["id"]), "title": a.get("title") or "", "artist": who(a),
            "cover": a.get("cover_medium") or "", "total": int(a.get("nb_tracks") or 0),
            "kind": a.get("record_type") or "album",
            "owned": h.store.has_album(str(a["id"])) or library._norm(a.get("title") or "") in have_albums,
        } for a in albums],
    })


async def api_albums(request: web.Request) -> web.Response:
    h = hub_of(request)
    try:
        albums = await get_albums(request.match_info["artist_id"])
    except Exception as exc:
        return bad(str(exc), 502)
    return web.json_response({"albums": albums, "owned": h.store.owned(),
                              "progress": h.store.progress()})


async def api_album(request: web.Request) -> web.Response:
    h = hub_of(request)
    album_id = request.match_info["album_id"]
    try:
        job = await get_album_job(album_id)
    except Exception as exc:
        return bad(str(exc), 502)
    out = h.sheet(job)
    if request.query.get("plan"):
        out["plan"] = await h.plan_for(job, f"album:{album_id}")
    return web.json_response(out)


async def api_resolve(request: web.Request) -> web.Response:
    """Разбирает ссылку и показывает карточку, НЕ ставя в очередь."""
    h = hub_of(request)
    url = str((await body_json(request)).get("url") or "").strip()
    if not url:
        return bad("нужен url")
    try:
        job = await sources.resolve_link(url)
    except Exception as exc:
        return bad(str(exc), 502)
    _sweep(h.resolved, RESOLVE_TTL)
    h.resolved[job["id"]] = (time.time(), job)
    out = h.sheet(job)
    out["token"] = job["id"]
    out["plan"] = await h.plan_for(job, f"album:{job['album_id']}" if job.get("album_id") else "")
    return web.json_response(out)


async def api_meta(request: web.Request) -> web.Response:
    h = hub_of(request)
    ck = h.cookies_info()
    return web.json_response({
        "cookies": ck["have"],
        "cookies_age_days": ck["age_days"],
        "cookies_status": ck["status"],
        "skip_dupes": library.SKIP_DUPES,
        "skip_live": library.SKIP_LIVE,
        "soundcloud": downloader.SOUNDCLOUD,
        "proxy": bool(downloader.PROXY),
        "proxies": len(downloader.PROXIES),
        "format": downloader.FMT,
        "concurrency": h.concurrency,
        "navidrome": h.navidrome_ready,
        "playlists": playlists.configured(),
    })


async def api_job(request: web.Request) -> web.Response:
    """Полная запись задачи — единственное место, где ездят длинные списки."""
    job = hub_of(request).store.find(request.match_info["job_id"])
    if not job:
        return bad("не нашёл такую задачу", 404)
    return web.json_response(_light(job))


async def api_enqueue(request: web.Request) -> web.Response:
    h = hub_of(request)
    body = await body_json(request)
    album_id, url, token = body.get("album_id"), body.get("url"), body.get("token")
    track_id = body.get("track_id")
    try:
        if token and (hit := h.resolved.pop(str(token), None)):
            job = hit[1]                     # уже разобранная ссылка из /api/resolve
        elif album_id:
            job = await get_album_job(str(album_id))
        elif track_id:
            job = await sources.get_track_job(str(track_id))
        elif url:
            job = await sources.resolve_link(str(url))
        else:
            return bad("нужен album_id, track_id, url или token")
    except Exception as exc:
        return bad(str(exc), 502)
    if body.get("ignore_dupes"):
        job["ignore_dupes"] = True
    pos = h.enqueue(job)
    return web.json_response({"ok": True, "position": pos, "job": job["id"]})


async def api_cancel(request: web.Request) -> web.Response:
    store = hub_of(request).store
    job_id = request.match_info["job_id"]
    if store.active and store.active["id"] == job_id:
        store.active["cancel"] = True        # начатые треки доскачаются
        store.touch()
        return web.json_response({"ok": True, "stopping": True})
    return web.json_response({"ok": store.cancel(job_id)})


async def api_status(request: web.Request) -> web.Response:
    """Снимок состояния. С ?since=N ждёт изменения до 25 секунд (long-poll)."""
    store = hub_of(request).store
    since = request.query.get("since")
    if since is not None:
        try:
            since_i = int(since)
        except ValueError:
            since_i = -1
        deadline = time.time() + 25
        while store.revision == since_i and time.time() < deadline:
            await asyncio.sleep(0.4)
    # клиент говорит, какую историю уже держит; совпало — не гоняем её снова
    hrev = request.query.get("hrev")
    fresh = hrev is not None and hrev.isdigit() and int(hrev) == store.history_rev
    return web.json_response(store.snapshot(history=not fresh))


async def api_scan(request: web.Request) -> web.Response:
    return web.json_response({"message": await hub_of(request).scan() or "Скан не настроен."})


async def api_retry_failed(request: web.Request) -> web.Response:
    """Повторить все альбомы с ошибкой или скачанные частично (кроме исправленных
    и уже стоящих в очереди)."""
    h = hub_of(request)
    store = h.store
    body = await body_json(request)
    statuses = set(body.get("statuses") or ["error", "partial"]) & {"error", "partial"}
    fixed = store.fixed_ids()
    busy = {str(j.get("album_id")) for j in store.pending if j.get("album_id")}
    if store.active and store.active.get("album_id"):
        busy.add(str(store.active["album_id"]))

    targets, seen, no_album = [], set(), 0
    for rec in reversed(store.history):
        if rec.get("status") not in statuses or rec.get("id") in fixed:
            continue
        aid = rec.get("album_id")
        if not aid:
            no_album += 1                    # трек или плейлист: повторить нечем
            continue
        if str(aid) in seen or str(aid) in busy:
            continue
        seen.add(str(aid))
        targets.append(str(aid))

    sem = asyncio.Semaphore(4)

    async def build(aid: str) -> dict | None:
        async with sem:
            try:
                return await get_album_job(aid)
            except Exception as exc:
                log.warning("повтор альбома %s не собрался: %s", aid, exc)
                return None

    jobs = [j for j in await asyncio.gather(*(build(a) for a in targets)) if j]
    for job in jobs:
        h.enqueue(job)
    return web.json_response({
        "ok": True, "queued": len(jobs), "no_album": no_album,
        "already": len(busy & {str(x.get("album_id")) for x in store.history
                               if x.get("status") in statuses})})


async def api_history_remove(request: web.Request) -> web.Response:
    """Убрать записи из списка: по статусу, исправленные или по id."""
    store = hub_of(request).store
    body = await body_json(request)
    ids = {str(i) for i in body.get("ids") or []}
    statuses = set(body.get("statuses") or []) & {"error", "partial"}
    fixed = store.fixed_ids()
    # видимые (те, что на кнопке) и уже исправленные — их в списке и так не видно
    visible = {x.get("id") for x in store.history
               if x.get("status") in statuses and x.get("id") not in fixed}
    already_fixed = {x.get("id") for x in store.history
                     if x.get("status") in statuses and x.get("id") in fixed}
    ids |= visible | already_fixed
    if body.get("fixed"):
        ids |= fixed
    ids.discard(None)
    store.remove_history(ids)
    return web.json_response({"ok": True, "removed": len(visible),
                              "fixed_removed": len(already_fixed)})


async def api_playlists(request: web.Request) -> web.Response:
    """Сборка идёт минутами — запускаем фоном, итог приедет в long-poll."""
    store = hub_of(request).store
    if "playlists" in store.busy:
        return web.json_response({"ok": False, "error": "уже собираю"}, status=409)
    store.busy.add("playlists")
    store.touch()

    async def run() -> None:
        try:
            store.notify(await playlists.sync_all(), "playlists")
        except Exception as exc:
            store.notify(f"⚠️ Плейлисты: {exc}", "error")
        finally:
            store.busy.discard("playlists")
            store.touch()

    bg = request.app[BG_KEY]
    bg.add(task := asyncio.create_task(run()))
    task.add_done_callback(bg.discard)
    return web.json_response({"ok": True, "started": True})


async def api_img(request: web.Request) -> web.StreamResponse:
    """Прокси обложек Deezer: один источник, работает при блокировке CDN у клиента."""
    src = request.query.get("u", "")
    if urllib.parse.urlparse(src).netloc not in IMG_HOSTS:
        raise web.HTTPForbidden(text="host not allowed")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(src)
            r.raise_for_status()
            return web.Response(body=r.content,
                                content_type=r.headers.get("content-type", "image/jpeg"),
                                headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        raise web.HTTPBadGateway(text="upstream error") from None


# ----- настройки -----
async def api_settings_get(request: web.Request) -> web.Response:
    return web.json_response({"values": hub_of(request).cfg.values,
                              "defaults": settings_mod.DEFAULTS})


async def api_settings_set(request: web.Request) -> web.Response:
    h = hub_of(request)
    patch = await body_json(request)
    try:
        h.cfg.update(patch)
    except ValueError as exc:
        return bad(str(exc))
    h.concurrency = h.cfg.apply_live(downloader, library, playlists)
    # смена прокси могла изменить картину «запретов» — обновим проверку в фоне
    if "proxies" in patch:
        h.net.start()
    return web.json_response({"ok": True, "values": h.cfg.values})


# ----- проверка запретов -----
async def api_netcheck_get(request: web.Request) -> web.Response:
    return web.json_response(hub_of(request).net.snapshot())


async def api_netcheck_run(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "started": hub_of(request).net.start()})


# ----- cookies -----
async def api_cookies_get(request: web.Request) -> web.Response:
    return web.json_response(hub_of(request).cookies_info())


async def api_cookies_set(request: web.Request) -> web.Response:
    h = hub_of(request)
    text = str((await body_json(request)).get("text") or "")
    if len(text) > 1_000_000:
        return bad("слишком большой файл")
    lines, err = cookies_mod.parse(text)
    if lines is None:
        return bad(err)
    path = Path(downloader.COOKIE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(cookies_mod.render(lines), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    h._save_cookie_state({})                 # забываем прежние предупреждения
    status, why = await h.cookie_check()
    return web.json_response({"ok": True, "saved": len(lines), "status": status, "why": why})


async def api_cookies_delete(request: web.Request) -> web.Response:
    h = hub_of(request)
    try:
        os.remove(downloader.COOKIE_FILE)
    except FileNotFoundError:
        pass
    h._save_cookie_state({})
    h.cookie_status.update(status="", why="", checked_at=0)
    return web.json_response({"ok": True})


async def api_cookies_check(request: web.Request) -> web.Response:
    status, why = await hub_of(request).cookie_check()
    return web.json_response({"status": status, "why": why})


# ================================== приложение ==================================
async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def app_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("app/")


async def app_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-cache"})


def build_app(hub: Hub, token: str) -> web.Application:
    app = web.Application(middlewares=[auth.make_middleware(token)], client_max_size=2 * 1024 * 1024)
    app[HUB_KEY] = hub
    app[BG_KEY] = set()
    app.add_routes([
        web.get("/api/info", api_info),
        web.get("/api/search", api_search),
        web.get("/api/find", api_find),
        web.get("/api/artist/{artist_id}/albums", api_albums),
        web.get("/api/album/{album_id}", api_album),
        web.get("/api/meta", api_meta),
        web.get("/api/job/{job_id}", api_job),
        web.post("/api/resolve", api_resolve),
        web.post("/api/enqueue", api_enqueue),
        web.post("/api/cancel/{job_id}", api_cancel),
        web.get("/api/status", api_status),
        web.post("/api/scan", api_scan),
        web.post("/api/playlists", api_playlists),
        web.post("/api/retry_failed", api_retry_failed),
        web.post("/api/history/remove", api_history_remove),
        web.get("/api/deps", api_deps_get),
        web.post("/api/deps/check", api_deps_check),
        web.get("/api/remote", api_remote_get),
        web.post("/api/remote/test", api_remote_test),
        web.post("/api/remote/sync", api_remote_sync),
        web.get("/api/img", api_img),
        web.get("/api/settings", api_settings_get),
        web.post("/api/settings", api_settings_set),
        web.get("/api/netcheck", api_netcheck_get),
        web.post("/api/netcheck/run", api_netcheck_run),
        web.get("/api/cookies", api_cookies_get),
        web.post("/api/cookies", api_cookies_set),
        web.delete("/api/cookies", api_cookies_delete),
        web.post("/api/cookies/check", api_cookies_check),
        web.get("/healthz", healthz),
        web.get("/app", app_redirect),
        web.get("/app/", app_index),
        web.static("/app/", WEB_ROOT, show_index=False),
    ])

    async def on_start(app: web.Application) -> None:
        app[HUB_KEY].start_background()

    async def on_stop(app: web.Application) -> None:
        await app[HUB_KEY].stop_background()

    if os.environ.get("HUB_NO_BACKGROUND") != "1":     # тесты гоняют обработчики без фоновых задач
        app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s  %(levelname)s  %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = auth.load_token(DATA_DIR / "token")
    hub = Hub()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8081"))
    log.info("music-hub %s: формат %s, %d потока, музыка %s, данные %s",
             VERSION, downloader.FMT, hub.concurrency, MUSIC_DIR, DATA_DIR)
    web.run_app(build_app(hub, token), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
