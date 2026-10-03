"""
Откуда берём, ЧТО качать: Deezer (поиск, альбомы, метаданные) и разбор ссылок.

Любая задача сводится к списку треков `items` с полными метаданными Deezer —
по ним downloader ищет аудио на YouTube и прописывает теги.

Ссылки Spotify читаются через публичную embed-страницу (без API-ключей)
и сопоставляются с Deezer, чтобы теги и обложки были одинаково точными.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
import uuid
from difflib import SequenceMatcher

import httpx

DEEZER = "https://api.deezer.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

_albums_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 3600


# ------------------------------- утилиты ------------------------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(re.findall(r"\w+", s))


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def new_job(kind: str, name: str, artist: str, items: list[dict], **extra) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "album_id": None,
        "album_artist": artist,
        "album_name": name,
        "year": "",
        "cover": "",
        "items": items,
        "total": len(items),
        "total_raw": len(items),     # до отсева дублей и live: показываем в очереди
        "via": {},                   # трек взят не с YouTube: {"soundcloud": [...], "cookies": [...]}
        "done": 0,
        "failed": 0,
        "failed_tracks": [],
        "status": "queued",
        "queued_at": time.time(),
    }
    job.update(extra)
    return job


# ----------------------------- Deezer API ----------------------------------
async def dz_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20, headers={"Accept-Language": "en"}) as c:
        r = await c.get(DEEZER + path, params=params or {})
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Deezer: {data['error'].get('message', data['error'])}")
    return data


async def search_artists(name: str, limit: int = 8) -> list[dict]:
    data = await dz_get("/search/artist", {"q": name, "limit": limit})
    return data.get("data", [])


async def search_catalog(query: str, limit: int = 12) -> tuple[list[dict], list[dict]]:
    """Песни и альбомы по строке запроса — для поиска из плеера."""
    tracks, albums = await asyncio.gather(
        dz_get("/search/track", {"q": query, "limit": limit}),
        dz_get("/search/album", {"q": query, "limit": limit}),
    )
    return tracks.get("data", []), albums.get("data", [])


async def get_albums(artist_id: str) -> list[dict]:
    """Альбомы артиста: альбомы -> EP -> синглы, без дублей по названию."""
    cached = _albums_cache.get(artist_id)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    data = await dz_get(f"/artist/{artist_id}/albums", {"limit": 200})
    items = data.get("data", [])

    seen: set[str] = set()
    out: list[dict] = []
    # сначала альбомы, потом EP, сборники и в самом конце синглы
    order = {"album": 0, "ep": 1, "compile": 2, "single": 3}
    from library import is_live_album

    def rank(x: dict) -> tuple:
        year = (x.get("release_date") or "")[:4]
        return (
            order.get(x.get("record_type", "album"), 9),
            is_live_album(x.get("title", "")),      # live — в конец своей группы
            -int(year) if year.isdigit() else 0,    # внутри группы: от новых к старым
            x.get("title", "").lower(),
        )

    for a in sorted(items, key=rank):
        key = a["title"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": str(a["id"]),
                "title": a["title"],
                "year": (a.get("release_date") or "")[:4],
                "type": a.get("record_type", "album"),
                "cover": a.get("cover_medium") or a.get("cover") or "",
                "cover_big": a.get("cover_big") or a.get("cover_medium") or "",
                "tracks": a.get("nb_tracks", 0),
                "live": is_live_album(a.get("title", "")),
            }
        )

    _albums_cache[artist_id] = (time.time(), out)
    return out


def _item(t: dict, alb: dict, total: int, discs: int) -> dict:
    """Трек Deezer -> элемент задачи со всем, что нужно для поиска и тегов."""
    album_artist = alb.get("artist", {}).get("name") or t.get("artist", {}).get("name", "Unknown")
    genres = [g["name"] for g in (alb.get("genres", {}) or {}).get("data", [])]
    return {
        "title": t.get("title") or "Unknown",
        "artist": t.get("artist", {}).get("name") or album_artist,
        "album": alb.get("title") or "Unknown",
        "album_artist": album_artist,
        "nn": int(t.get("track_position") or 0),
        "disc": int(t.get("disk_number") or 1),
        "discs": discs,
        "total": total,
        "date": alb.get("release_date") or "",
        "genre": genres[0] if genres else "",
        "isrc": t.get("isrc") or "",
        "duration": int(t.get("duration") or 0),
        "cover_url": alb.get("cover_xl") or alb.get("cover_big") or "",
        "label": alb.get("label") or "",
    }


async def album_info(album_id: str) -> tuple[dict, list[dict]]:
    alb = await dz_get(f"/album/{album_id}")
    tracks = (await dz_get(f"/album/{album_id}/tracks", {"limit": 500})).get("data", [])
    for i, t in enumerate(tracks, start=1):
        t.setdefault("track_position", i)
    discs = max((int(t.get("disk_number") or 1) for t in tracks), default=1)
    items = [_item(t, alb, len(tracks), discs) for t in tracks]
    return alb, items


async def get_album_job(album_id: str) -> dict:
    alb, items = await album_info(album_id)
    return new_job(
        "album",
        alb.get("title", "Unknown"),
        alb.get("artist", {}).get("name", "Unknown"),
        items,
        album_id=str(album_id),
        artist_id=str(alb.get("artist", {}).get("id", "")),
        year=(alb.get("release_date") or "")[:4],
        cover=alb.get("cover_medium") or "",
        track_titles=[i["title"] for i in items],
    )


async def track_item(track_id: str) -> dict:
    """Одиночный трек: кладём в папку его альбома с правильным номером."""
    t = await dz_get(f"/track/{track_id}")
    alb = await dz_get(f"/album/{t['album']['id']}")
    discs = 1
    total = alb.get("nb_tracks") or 0
    return _item(t, alb, total, discs)


async def get_track_job(track_id: str) -> dict:
    it = await track_item(track_id)
    return new_job(
        "track", it["title"], it["artist"], [it],
        year=it["date"][:4], cover=it["cover_url"].replace("1000x1000", "250x250"),
    )


# --------------------------- поиск соответствий ----------------------------
async def dz_find_album(artist: str, title: str, n_tracks: int = 0) -> str | None:
    q = f'artist:"{artist}" album:"{title}"'
    data = (await dz_get("/search/album", {"q": q, "limit": 10})).get("data", [])
    if not data:
        data = (await dz_get("/search/album", {"q": f"{artist} {title}", "limit": 10})).get("data", [])
    best, best_s = None, 0.0
    for a in data:
        s = similar(a["title"], title) * 2 + similar(a.get("artist", {}).get("name", ""), artist)
        if n_tracks and a.get("nb_tracks") == n_tracks:
            s += 0.3
        if a.get("record_type") == "album":
            s += 0.1
        if s > best_s:
            best, best_s = a, s
    return str(best["id"]) if best and best_s >= 2.2 else None


async def dz_find_track(artist: str, title: str, duration: int = 0) -> str | None:
    q = f'artist:"{artist}" track:"{title}"'
    data = (await dz_get("/search/track", {"q": q, "limit": 10})).get("data", [])
    if not data:
        data = (await dz_get("/search/track", {"q": f"{artist} {title}", "limit": 10})).get("data", [])
    best, best_s = None, 0.0
    for t in data:
        s = similar(t["title"], title) * 2 + similar(t.get("artist", {}).get("name", ""), artist)
        if duration and t.get("duration"):
            s += 0.5 if abs(t["duration"] - duration) <= 3 else 0.0
        if s > best_s:
            best, best_s = t, s
    return str(best["id"]) if best and best_s >= 2.2 else None


# ------------------------------- Spotify -----------------------------------
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?|spotify:)(album|track|playlist)[/:]([A-Za-z0-9]+)")


async def spotify_embed(kind: str, sid: str) -> dict:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as c:
        r = await c.get(f"https://open.spotify.com/embed/{kind}/{sid}")
        r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Spotify не отдал данные о ссылке")
    return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]


def _first_artist(subtitle: str) -> str:
    return (subtitle or "").split(",")[0].strip()


async def spotify_job(kind: str, sid: str) -> dict:
    e = await spotify_embed(kind, sid)
    name = e.get("name") or e.get("title") or "Spotify"
    artist = _first_artist(e.get("subtitle", ""))
    tracks = e.get("trackList") or []

    if kind == "album":
        album_id = await dz_find_album(artist, name, len(tracks))
        if album_id:
            return await get_album_job(album_id)
        raise RuntimeError(f"Не нашёл «{artist} — {name}» на Deezer")

    if kind == "track":
        dur = int((e.get("duration") or 0) / 1000)
        track_id = await dz_find_track(artist, name, dur)
        if track_id:
            return await get_track_job(track_id)
        raise RuntimeError(f"Не нашёл «{artist} — {name}» на Deezer")

    # плейлист: каждый трек ищем отдельно, раскладываем по папкам альбомов
    sem = asyncio.Semaphore(4)

    async def one(t: dict) -> dict | None:
        async with sem:
            try:
                tid = await dz_find_track(
                    _first_artist(t.get("subtitle", "")), t.get("title", ""),
                    int((t.get("duration") or 0) / 1000),
                )
                return await track_item(tid) if tid else None
            except Exception:
                return None

    found = await asyncio.gather(*(one(t) for t in tracks))
    items = [f for f in found if f]
    missing = [f"{_first_artist(t.get('subtitle', ''))} — {t.get('title', '')}"
               for t, f in zip(tracks, found) if not f]
    if not items:
        raise RuntimeError("Ни один трек плейлиста не нашёлся на Deezer")
    return new_job("playlist", name, "Плейлист Spotify", items, not_found=missing,
                   cover=items[0]["cover_url"].replace("1000x1000", "250x250"))


# ------------------------------- разбор ссылок ------------------------------
DEEZER_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(album|track)/(\d+)")
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
YT_LIST_RE = re.compile(r"(?:youtube\.com|youtu\.be)/\S*[?&]list=([A-Za-z0-9_-]{10,})")
# «Artist - Topic» и мусор в хвосте названия ролика
TOPIC_RE = re.compile(r"\s*-\s*Topic$", re.I)
JUNK_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(?:official|video|audio|lyric|lyrics|hd|hq|mv|clip|visualizer|"
    r"remaster(?:ed)?|explicit|official\s*music)[^\)\]]*[\)\]]", re.I)


def yt_artist_title(entry: dict) -> tuple[str, str]:
    """Исполнитель и название из ролика YouTube: «Artist - Title (Official Video)»."""
    title = JUNK_RE.sub("", entry.get("title") or "").strip(" -–—")
    channel = TOPIC_RE.sub("", entry.get("channel") or entry.get("uploader") or "").strip()
    if entry.get("artist") or entry.get("track"):      # у Topic-роликов поля заполнены
        return (entry.get("artist") or channel), (entry.get("track") or title)
    m = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(m) == 2 and len(m[0]) < 60:
        return m[0].strip(), m[1].strip()
    return channel, title


def is_link(text: str) -> bool:
    return bool(
        SPOTIFY_RE.search(text) or DEEZER_RE.search(text) or YT_RE.search(text)
        or YT_LIST_RE.search(text) or "deezer.page.link" in text or "link.deezer.com" in text
    )


async def youtube_playlist_job(url: str) -> dict:
    """Плейлист YouTube: метаданные берём с Deezer, чего там нет — качаем с YouTube."""
    import downloader

    name, entries = await asyncio.to_thread(downloader.playlist_entries, url)
    if not entries:
        raise RuntimeError("В этом плейлисте нет роликов (или он закрыт)")

    sem = asyncio.Semaphore(4)

    async def one(i: int, e: dict) -> dict:
        artist, title = yt_artist_title(e)
        async with sem:
            try:
                tid = await dz_find_track(artist, title, int(e.get("duration") or 0))
                if tid:
                    item = await track_item(tid)
                    # теги с Deezer, звук — из того ролика, на который показал пользователь
                    item["video_id"] = e["id"]
                    return item
            except Exception:
                pass
        # на Deezer не нашлось — качаем именно этот ролик
        return {
            "video_id": e["id"], "title": title or e.get("title") or e["id"],
            "artist": artist or "Unknown", "album": name,
            "album_artist": artist or "Unknown", "nn": i, "disc": 1, "discs": 1,
            "total": len(entries), "date": "", "genre": "", "isrc": "",
            "duration": int(e.get("duration") or 0), "cover_url": "", "label": "",
            "yt_only": True,            # метаданных Deezer нет, теги будут из ролика
        }

    items = list(await asyncio.gather(*(one(i, e) for i, e in enumerate(entries, start=1))))
    from_yt = [i["title"] for i in items if i.get("yt_only")]
    artists = {i["album_artist"] for i in items}
    return new_job(
        "playlist", name, artists.pop() if len(artists) == 1 else "Плейлист YouTube", items,
        cover=next((i["cover_url"].replace("1000x1000", "250x250")
                    for i in items if i.get("cover_url")), ""),
        from_youtube=from_yt,
    )


async def resolve_link(text: str) -> dict:
    text = text.strip()
    if "deezer.page.link" in text or "link.deezer.com" in text:
        url = re.search(r"https?://\S+", text).group(0)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": UA}) as c:
            text = str((await c.get(url)).url)

    if m := DEEZER_RE.search(text):
        kind, did = m.groups()
        return await (get_album_job(did) if kind == "album" else get_track_job(did))

    if m := SPOTIFY_RE.search(text):
        return await spotify_job(*m.groups())

    if YT_LIST_RE.search(text) and "watch?v=" not in text:
        return await youtube_playlist_job(re.search(r"https?://\S+", text).group(0))

    if m := YT_RE.search(text):
        vid = m.group(1)
        it = {
            "video_id": vid, "title": "", "artist": "", "album": "YouTube",
            "album_artist": "", "nn": 0, "disc": 1, "discs": 1, "total": 0,
            "date": "", "genre": "", "isrc": "", "duration": 0, "cover_url": "", "label": "",
        }
        return new_job("youtube", f"YouTube {vid}", "YouTube", [it])

    if YT_LIST_RE.search(text):
        return await youtube_playlist_job(re.search(r"https?://\S+", text).group(0))

    raise ValueError("Не понял ссылку. Поддерживаются Deezer, Spotify и YouTube.")
