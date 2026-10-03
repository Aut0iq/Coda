"""
Автоплейлисты по исполнителям для Navidrome (Subsonic API).

Живёт рядом с bot.py. Плейлисты создаются от имени ND_USER, то есть принадлежат
тому же пользователю, что запускает скан (aut0iq).

Правило: плейлист создаётся, если у исполнителя в Navidrome не меньше
PL_MIN_TRACKS разных песен (по умолчанию 5), в него идут все его треки,
включая сборники и фиты. PL_MIN_ALBUMS — доп. порог по альбомам (0 = нет).

Созданные плейлисты бот помечает комментарием MARK. Уже существующий плейлист
исполнителя (в т.ч. сделанный руками, с аббревиатурой вроде RHCP/SOAD или с
опечаткой в имени) не дублируется, а дополняется недостающими песнями.

Обновление — только добавление недостающих треков, порядок и ручные правки
внутри плейлиста не ломаются.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
import unicodedata

import httpx

log = logging.getLogger("music-bot.playlists")

ND_URL = os.environ.get("ND_URL", "")
ND_USER = os.environ.get("ND_USER", "")
ND_PASS = os.environ.get("ND_PASS", "")

PL_ENABLED = os.environ.get("PL_ENABLED", "1") == "1"
PL_MIN_ALBUMS = int(os.environ.get("PL_MIN_ALBUMS", "0"))
PL_MIN_TRACKS = int(os.environ.get("PL_MIN_TRACKS", "5"))
PL_PREFIX = os.environ.get("PL_PREFIX", "")            # например "★ "
PL_SCAN_WAIT = int(os.environ.get("PL_SCAN_WAIT", "300"))
PL_RECENT = int(os.environ.get("PL_RECENT", "10"))

MARK = "auto:artist"      # метка «этот плейлист собрал бот»
BATCH = 100               # сколько songId кладём в один запрос
API_V = "1.16.1"
CLIENT = "music-bot"

# одновременно синхронизируем только одну пачку, чтобы не гонять Navidrome
_lock = asyncio.Lock()


# ------------------------------- утилиты ------------------------------------
def configured() -> bool:
    return bool(PL_ENABLED and ND_URL and ND_USER and ND_PASS)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


async def _call(c: httpx.AsyncClient, method: str, params: list[tuple[str, str]] | None = None) -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5((ND_PASS + salt).encode()).hexdigest()
    query = [
        ("u", ND_USER), ("t", token), ("s", salt),
        ("v", API_V), ("c", CLIENT), ("f", "json"),
    ] + (params or [])
    r = await c.get(f"{ND_URL.rstrip('/')}/rest/{method}.view", params=query)
    r.raise_for_status()
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        err = body.get("error", {})
        raise RuntimeError(f"{method}: {err.get('message') or 'неизвестная ошибка'} (код {err.get('code')})")
    return body


# ------------------------------ ожидание скана -------------------------------
async def _wait_scan(c: httpx.AsyncClient) -> None:
    """Ждём, пока Navidrome досканирует новые файлы, иначе треков ещё нет в базе."""
    # скан мог ещё не успеть стартовать
    for _ in range(8):
        try:
            if (await _call(c, "getScanStatus"))["scanStatus"].get("scanning"):
                break
        except Exception as exc:
            log.debug("getScanStatus: %s", exc)
            return
        await asyncio.sleep(2)

    waited = 0
    while waited < PL_SCAN_WAIT:
        try:
            if not (await _call(c, "getScanStatus"))["scanStatus"].get("scanning"):
                return
        except Exception as exc:
            log.debug("getScanStatus: %s", exc)
            return
        await asyncio.sleep(3)
        waited += 3
    log.warning("Скан Navidrome идёт дольше %d с — собираю плейлисты по текущим данным", PL_SCAN_WAIT)


# ------------------------------ чтение библиотеки ----------------------------
async def _find_artist(c: httpx.AsyncClient, name: str) -> dict | None:
    """Ищем исполнителя по точному совпадению имени."""
    target = _norm(name)
    data = await _call(c, "search3", [
        ("query", name), ("artistCount", "20"), ("albumCount", "0"), ("songCount", "0"),
    ])
    for a in data.get("searchResult3", {}).get("artist", []) or []:
        if _norm(a.get("name", "")) == target:
            return a
    # запасной путь: полный список исполнителей
    data = await _call(c, "getArtists")
    for idx in data.get("artists", {}).get("index", []) or []:
        for a in idx.get("artist", []) or []:
            if _norm(a.get("name", "")) == target:
                return a
    return None


async def _artist_songs(c: httpx.AsyncClient, artist_id: str) -> tuple[str, int, list[tuple[str, str]]]:
    """Имя исполнителя, число альбомов и id ВСЕХ его треков: свои альбомы
    (год → альбом → диск → номер), затем треки на сборниках и в фитах.
    Одна и та же песня из разных релизов (оригинал, ремастер, сборник) — один раз."""
    import library     # здесь, а не наверху: library сам импортирует playlists

    artist = (await _call(c, "getArtist", [("id", artist_id)]))["artist"]
    name = artist.get("name", "")
    albums = artist.get("album", []) or []
    albums.sort(key=lambda a: (int(a.get("year") or 0), _norm(a.get("name", ""))))
    own_albums = {a["id"] for a in albums}

    songs: list[dict] = []
    for alb in albums:
        part = (await _call(c, "getAlbum", [("id", alb["id"])]))["album"].get("song", []) or []
        part.sort(key=lambda s: (int(s.get("discNumber") or 1), int(s.get("track") or 0), _norm(s.get("title", ""))))
        songs += part

    # треки исполнителя на чужих альбомах: сборники Various Artists, фиты
    target = library.primary_artist(name)
    extra: list[dict] = []
    offset = 0
    while offset < 5000:
        data = await _call(c, "search3", [
            ("query", name), ("artistCount", "0"), ("albumCount", "0"),
            ("songCount", "500"), ("songOffset", str(offset)),
        ])
        found = data.get("searchResult3", {}).get("song", []) or []
        for s in found:
            if s.get("albumId") in own_albums:
                continue
            ids = {a.get("id") for a in s.get("artists") or []}
            if (artist_id in ids or library.primary_artist(s.get("artist", "")) == target
                    or target in _parts(s.get("artist", ""))
                    or target in _parts(s.get("displayAlbumArtist") or s.get("albumArtist") or "")):
                extra.append(s)
        if len(found) < 500:
            break
        offset += 500
    extra.sort(key=lambda s: (int(s.get("year") or 0), _norm(s.get("album", "")),
                              int(s.get("discNumber") or 1), int(s.get("track") or 0)))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for s in songs + extra:
        key = library.song_key(name, s.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append((s["id"], key))
    return name, len(albums), out


# «A/B», «A & B», «A feat. B», «A, B», «A x B» — совместные релизы
COLLAB_SPLIT_RE = re.compile(r"\s*/\s*|\s+&\s+|\s*,\s*|\s*;\s*|\s+(?:feat\.?|ft\.?|featuring|x|vs\.?|and|и)\s+", re.I)
_artists_cache: tuple[float, set[str]] = (0.0, set())


def _parts(name: str) -> list[str]:
    return [_norm(p) for p in COLLAB_SPLIT_RE.split(name or "") if _norm(p)]


async def _artist_names(c: httpx.AsyncClient) -> set[str]:
    global _artists_cache
    if time.time() - _artists_cache[0] < 600:
        return _artists_cache[1]
    data = await _call(c, "getArtists")
    names = {_norm(a.get("name", ""))
             for idx in data.get("artists", {}).get("index", []) or []
             for a in idx.get("artist", []) or []}
    _artists_cache = (time.time(), names)
    return names


async def _is_collab(c: httpx.AsyncClient, name: str) -> bool:
    """Совместный исполнитель: имя склеено из исполнителей, которые есть в
    библиотеке сами по себе («Curta'n Wall/Elvya»). «Leo/need» и «Pride & Glory» —
    обычные группы: их частей отдельными исполнителями нет."""
    parts = _parts(name)
    if len(parts) < 2:
        return False
    names = await _artist_names(c)
    return any(p in names and p != _norm(name) for p in parts)


async def _own_playlists(c: httpx.AsyncClient) -> dict[str, dict]:
    data = await _call(c, "getPlaylists")
    out: dict[str, dict] = {}
    for p in data.get("playlists", {}).get("playlist", []) or []:
        if p.get("owner", ND_USER) == ND_USER:
            out[_norm(p.get("name", ""))] = p
    return out


async def _playlist_contents(c: httpx.AsyncClient, playlist_id: str, artist: str) -> tuple[set[str], set[str]]:
    """id треков плейлиста и ключи песен (чтобы не добавлять ту же песню с другого релиза)."""
    import library

    data = await _call(c, "getPlaylist", [("id", playlist_id)])
    entries = data.get("playlist", {}).get("entry", []) or []
    return {e["id"] for e in entries}, {library.song_key(artist, e.get("title", "")) for e in entries}


def _initials(name: str) -> str:
    words = _norm(name).replace("'", " ").split()
    return "".join(w[0] for w in words if w) if len(words) >= 3 else ""


def _find_playlist(own: dict[str, dict], artist: str) -> dict | None:
    """Плейлист исполнителя среди плейлистов пользователя: по точному имени,
    по аббревиатуре (RHCP, SOAD, MGR:R) или по почти совпадающему имени
    (Twisted Sisters, Curtain Wall)."""
    from difflib import SequenceMatcher

    target = _norm(f"{PL_PREFIX}{artist}")
    if target in own:
        return own[target]
    flat = lambda x: "".join(ch for ch in _norm(x) if ch.isalnum())
    ini = _initials(artist)
    best, best_r = None, 0.0
    for key, p in own.items():
        if ini and flat(p.get("name", "")) == ini:
            return p
        a, b = flat(key), flat(artist)
        if min(len(a), len(b)) >= 6:
            r = SequenceMatcher(None, a, b).ratio()
            if r > best_r:
                best, best_r = p, r
    return best if best_r >= 0.88 else None


# ------------------------------ запись плейлиста -----------------------------
async def _add_songs(c: httpx.AsyncClient, playlist_id: str, ids: list[str]) -> None:
    for chunk in _chunks(ids, BATCH):
        await _call(c, "updatePlaylist",
                    [("playlistId", playlist_id)] + [("songIdToAdd", s) for s in chunk])


async def _upsert(c: httpx.AsyncClient, name: str, songs: list[tuple[str, str]],
                  own: dict[str, dict]) -> tuple[str, int, str]:
    """(created|updated|unchanged, сколько добавлено, имя плейлиста).
    Существующий плейлист — свой или созданный ботом — только дополняется:
    ничего не удаляется и не переставляется."""
    ids = [i for i, _ in songs]
    existing = _find_playlist(own, name)

    if existing is None:
        title = f"{PL_PREFIX}{name}"
        head, tail = ids[:BATCH], ids[BATCH:]
        resp = await _call(c, "createPlaylist",
                           [("name", title)] + [("songId", s) for s in head])
        pl_id = (resp.get("playlist") or {}).get("id")
        if not pl_id:                       # часть версий возвращает пустой ответ
            own_now = await _own_playlists(c)
            pl_id = (own_now.get(_norm(title)) or {}).get("id")
        if not pl_id:
            raise RuntimeError(f"не смог получить id созданного плейлиста «{title}»")
        if tail:
            await _add_songs(c, pl_id, tail)
        await _call(c, "updatePlaylist", [("playlistId", pl_id), ("comment", MARK), ("public", "true")])
        own[_norm(title)] = {"id": pl_id, "name": title, "comment": MARK, "public": True}
        return "created", len(ids), title

    if existing.get("comment") == MARK and not existing.get("public"):
        await _call(c, "updatePlaylist", [("playlistId", existing["id"]), ("public", "true")])
        existing["public"] = True
    have_ids, have_keys = await _playlist_contents(c, existing["id"], name)
    missing = [i for i, k in songs if i not in have_ids and k not in have_keys]
    if not missing:
        return "unchanged", 0, existing.get("name", name)
    await _add_songs(c, existing["id"], missing)
    return "updated", len(missing), existing.get("name", name)


async def _sync_one(c: httpx.AsyncClient, artist_id: str, own: dict[str, dict]) -> tuple[str, str, int] | None:
    """(действие, имя плейлиста, сколько добавлено) либо None, если порог не набран."""
    name, n_albums, songs = await _artist_songs(c, artist_id)
    if await _is_collab(c, name):
        log.info("Пропуск %s: совместный исполнитель, треки уйдут в плейлисты участников", name)
        return None
    if n_albums < PL_MIN_ALBUMS or len(songs) < PL_MIN_TRACKS:
        log.info("Пропуск %s: %d альбом(ов), %d песен", name, n_albums, len(songs))
        return None
    action, added, pl_name = await _upsert(c, name, songs, own)
    return action, pl_name, added


# -------------------------------- отчёты ------------------------------------
def _report(results: list[tuple[str, str, int]], verbose: bool = False) -> str:
    created = [f"{n} ({k})" for a, n, k in results if a == "created"]
    updated = [f"{n} (+{k})" for a, n, k in results if a == "updated"]
    parts = []
    if created:
        parts.append("создано: " + ", ".join(created))
    if updated:
        parts.append("дополнено: " + ", ".join(updated))
    return "🎧 Плейлисты — " + "; ".join(parts) if parts else ""


# ------------------------------ точки входа ---------------------------------
async def sync_artist(artist_name: str) -> str:
    """После загрузки альбома: пересобрать плейлист одного исполнителя."""
    if not configured():
        return ""
    try:
        async with _lock:
            async with httpx.AsyncClient(timeout=60) as c:
                await _wait_scan(c)
                artist = await _find_artist(c, artist_name)
                if not artist:
                    log.warning("Исполнитель %r не найден в Navidrome", artist_name)
                    return f"⚠️ Плейлист: не нашёл «{artist_name}» в Navidrome, попробуй /playlists позже."
                res = await _sync_one(c, artist["id"], await _own_playlists(c))
                return _report([res]) if res else ""
    except Exception as exc:
        log.warning("sync_artist(%r): %s", artist_name, exc)
        return f"⚠️ Плейлисты: {exc}"


async def sync_recent(limit: int | None = None) -> str:
    """После загрузки по ссылке: обновить плейлисты недавно добавленных альбомов."""
    if not configured():
        return ""
    try:
        async with _lock:
            async with httpx.AsyncClient(timeout=60) as c:
                await _wait_scan(c)
                data = await _call(c, "getAlbumList2", [
                    ("type", "newest"), ("size", str(limit or PL_RECENT)),
                ])
                albums = data.get("albumList2", {}).get("album", []) or []
                seen: list[str] = []
                for a in albums:
                    aid = a.get("artistId")
                    if aid and aid not in seen:
                        seen.append(aid)
                own = await _own_playlists(c)
                results = []
                for aid in seen:
                    try:
                        res = await _sync_one(c, aid, own)
                    except Exception as exc:
                        log.warning("Плейлист для artistId=%s не собрался: %s", aid, exc)
                        continue
                    if res:
                        results.append(res)
                return _report(results)
    except Exception as exc:
        log.warning("sync_recent: %s", exc)
        return f"⚠️ Плейлисты: {exc}"


async def sync_all() -> str:
    """Разовый проход по всей библиотеке."""
    if not configured():
        return "Плейлисты выключены или не заданы ND_URL / ND_USER / ND_PASS."
    try:
        async with _lock:
            async with httpx.AsyncClient(timeout=120) as c:
                await _wait_scan(c)
                data = await _call(c, "getArtists")
                own = await _own_playlists(c)
                results: list[tuple[str, str, int]] = []
                total = 0
                for idx in data.get("artists", {}).get("index", []) or []:
                    for a in idx.get("artist", []) or []:
                        total += 1
                        try:
                            res = await _sync_one(c, a["id"], own)
                        except Exception as exc:
                            log.warning("Плейлист для %s не собрался: %s", a.get("name"), exc)
                            continue
                        if res:
                            results.append(res)
                rep = _report(results, verbose=True)
                return rep or f"Прошёл по {total} исполнител(ю/ям) — все плейлисты уже в порядке."
    except Exception as exc:
        log.warning("sync_all: %s", exc)
        return f"⚠️ Плейлисты: {exc}"
