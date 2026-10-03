"""
Что уже есть в библиотеке: защита от дублей и отсев live-версий.

Песня считается той же, если совпадают основной исполнитель и «ядро» названия —
без пометок вида (Remastered 2015), - Single Version, (feat. …). Поэтому
«Wind of Change» из студийного альбома и «Wind of Change - Remastered 2011» из
сборника — один трек, а (Acoustic), (Remix), (Demo) остаются отдельными.

Источники знания о библиотеке:
  * Navidrome (Subsonic search3) — всё, что уже доставлено и отсканировано;
  * library.json — всё, что скачал сам бот (закрывает окно до скана Navidrome).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

import httpx

import playlists

log = logging.getLogger("music-bot.library")

INDEX_FILE = Path(os.environ.get("LIBRARY_INDEX", str(Path(__file__).resolve().parent / "library.json")))
SKIP_DUPES = os.environ.get("SKIP_DUPES", "1") == "1"
SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"

# пометки, которые не делают трек другой песней
VERSION_RE = re.compile(
    r"remaster|remastered|re-master|mono|stereo|single|album version|radio edit|"
    r"\bedit\b|original mix|original version|bonus|deluxe|anniversary|expanded|"
    r"\b(19|20)\d\d\b|\bfeat\b|\bft\b|\bwith\b|explicit|clean|версия|ремастер",
    re.I,
)
BRACKET_RE = re.compile(r"[\(\[（【]([^\)\]）】]*)[\)\]）】]")
DASH_RE = re.compile(r"\s+[-–—]\s+(.+)$")
# live только в пометках: «Live Wire», «Live and Let Die» — обычные песни
LIVE_WORD_RE = re.compile(
    r"\blive\b|\bunplugged\b|\bin concert\b|\bконцерт|\bживьём\b|\bживое\b|\blive at\b|\blive in\b",
    re.I,
)
ARTIST_SPLIT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|&|and|x|vs\.?)\s+|,|;", re.I)
# альбом live: «Live at …», «World Wide Live», «Live Bites», «MTV Unplugged», но не «Live Wire»
LIVE_ALBUM_RE = re.compile(
    r"\blive (?:at|in|from|on|aus)\b|\bunplugged\b|\bin concert\b|\blive$|\blive\s*(?:19|20)\d\d|"
    r"^live\b(?!\s+(?:wire|and|to|forever|like|it|your|free|or|for|fast|young|evil|with|a|the|is)\b)|"
    r"\bконцерт|\bживьём\b",
    re.I,
)


# ------------------------------- нормализация -------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", s))


def primary_artist(artist: str) -> str:
    return _norm(ARTIST_SPLIT_RE.split(artist or "")[0])


def _tags(title: str) -> list[str]:
    """Все пометки названия: содержимое скобок и хвост после « - »."""
    tags = BRACKET_RE.findall(title or "")
    rest = BRACKET_RE.sub("", title or "")
    m = DASH_RE.search(rest)
    if m:
        tags.append(m.group(1))
    return tags


def is_live(title: str) -> bool:
    return any(LIVE_WORD_RE.search(t) for t in _tags(title))


def is_live_album(title: str) -> bool:
    # в названии альбома live ищем везде: «Live Bites», «World Wide Live», «MTV Unplugged»
    t = BRACKET_RE.sub("", title or "").strip()
    return is_live(title) or bool(LIVE_ALBUM_RE.search(t)) or bool(LIVE_ALBUM_RE.search(title or ""))


def core_title(title: str) -> str:
    t = title or ""
    # убираем скобки с «версионными» пометками, остальные оставляем
    t = BRACKET_RE.sub(
        lambda m: "" if VERSION_RE.search(m.group(1)) or LIVE_WORD_RE.search(m.group(1)) else m.group(0), t
    )
    m = DASH_RE.search(t)
    if m and (VERSION_RE.search(m.group(1)) or LIVE_WORD_RE.search(m.group(1))):
        t = t[: m.start()]
    return _norm(t) or _norm(title)


def song_key(artist: str, title: str) -> str:
    key = f"{primary_artist(artist)}|{core_title(title)}"
    return key + "|live" if is_live(title) else key


# ------------------------------ локальный индекс ------------------------------
class Index:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}      # ключ -> «Альбом» (для отчёта)
        self.isrc: dict[str, str] = {}
        try:
            raw = json.loads(INDEX_FILE.read_text("utf-8"))
            self.keys = raw.get("keys", {})
            self.isrc = raw.get("isrc", {})
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("не прочитал %s: %s", INDEX_FILE, exc)

    def add(self, it: dict) -> None:
        where = it.get("album") or ""
        self.keys[song_key(it["artist"], it["title"])] = where
        if it.get("isrc"):
            self.isrc[it["isrc"]] = where

    def save(self) -> None:
        try:
            tmp = INDEX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"keys": self.keys, "isrc": self.isrc}, ensure_ascii=False), "utf-8")
            tmp.replace(INDEX_FILE)
        except Exception as exc:
            log.warning("не записал %s: %s", INDEX_FILE, exc)


index = Index()


# ------------------------------ Navidrome ------------------------------------
def _nd_ready() -> bool:
    return bool(playlists.ND_URL and playlists.ND_USER and playlists.ND_PASS)


async def _nd_artist_songs(c: httpx.AsyncClient, artist: str) -> dict[str, str]:
    """Все песни исполнителя в Navidrome: ключ -> альбом. Ищем и как автора трека,
    и как исполнителя альбома — так ловятся и сборники."""
    target = primary_artist(artist)
    out: dict[str, str] = {}
    offset = 0
    while offset < 5000:
        data = await playlists._call(c, "search3", [
            ("query", artist), ("artistCount", "0"), ("albumCount", "0"),
            ("songCount", "500"), ("songOffset", str(offset)),
        ])
        songs = data.get("searchResult3", {}).get("song", []) or []
        for s in songs:
            if target in (primary_artist(s.get("artist", "")), primary_artist(s.get("displayAlbumArtist") or s.get("albumArtist") or "")):
                who = s.get("artist") or artist
                if primary_artist(who) != target:
                    who = artist
                out[song_key(who, s.get("title", ""))] = s.get("album", "")
                if s.get("isrc"):
                    isrcs = s["isrc"] if isinstance(s["isrc"], list) else [s["isrc"]]
                    for i in isrcs:
                        out["isrc:" + i] = s.get("album", "")
        if len(songs) < 500:
            break
        offset += 500
    return out


async def library_for(artists: set[str]) -> dict[str, str]:
    """Ключи песен, которые уже есть: Navidrome + локальный индекс."""
    known: dict[str, str] = dict(index.keys)
    known.update({"isrc:" + k: v for k, v in index.isrc.items()})
    if not _nd_ready():
        return known
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            sem = asyncio.Semaphore(4)

            async def one(a: str) -> dict[str, str]:
                async with sem:
                    return await _nd_artist_songs(c, a)

            # sorted, а не set: иначе на сборнике из 60+ исполнителей каждый запуск
            # проверял бы других, и состав дублей плавал бы
            for part in await asyncio.gather(*(one(a) for a in sorted(artists)[:80])):
                known.update(part)
    except Exception as exc:
        log.warning("Navidrome недоступен для проверки дублей: %s", exc)
    return known


# ------------------------------ разбор задачи ---------------------------------
async def analyze(job: dict) -> list[dict]:
    """Что бот сделает с каждым треком задачи. Единственное место с этой логикой.

    -> [{"nn", "title", "artist", "state": keep|dupe|live, "where", "label"}]
    where — альбом, в котором песня уже лежит (для дублей).
    job["ignore_dupes"] — «скачать заново»: дубли не отсеиваем, live всё равно режем.
    """
    items = job["items"]
    live_album = job.get("kind") == "album" and (
        is_live_album(job.get("album_name", "")) or (items and all(is_live(i["title"]) for i in items))
    )
    job["live_album"] = live_album
    check_dupes = SKIP_DUPES and not job.get("ignore_dupes")

    known = await library_for({i["artist"] for i in items if i.get("artist")}) if check_dupes else {}

    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        row = {"nn": it.get("nn") or 0, "title": it.get("title", ""),
               "artist": it.get("artist", ""), "state": "keep", "where": "", "label": ""}
        if not it.get("title"):             # голая ссылка YouTube — метаданных ещё нет
            out.append(row)
            continue
        row["label"] = label = f"{it.get('nn') or '·'}. {it['title']}"
        # live-треки из сборников и студийных альбомов не нужны; live-альбом,
        # выбранный явно, качаем целиком
        if SKIP_LIVE and not live_album and is_live(it["title"]):
            row["state"] = "live"
            out.append(row)
            continue
        key = song_key(it["artist"], it["title"])
        if check_dupes:
            where = known.get(key) or (known.get("isrc:" + it["isrc"]) if it.get("isrc") else None)
            if where is not None:
                row.update(state="dupe", where=where)
                out.append(row)
                continue
            if key in seen:
                row.update(state="dupe", where="", label=f"{label}  (повтор в этом же релизе)")
                out.append(row)
                continue
        seen.add(key)
        out.append(row)
    return out


async def filter_items(job: dict) -> tuple[list[dict], list[str], list[str]]:
    """Возвращает (что качать, пропущенные дубли, пропущенные live)."""
    rows = await analyze(job)
    keep, dupes, lives = [], [], []
    for it, row in zip(job["items"], rows):
        if row["state"] == "keep":
            keep.append(it)
        elif row["state"] == "live":
            lives.append(row["label"])
        elif row["where"]:
            dupes.append(f"{row['label']}  (есть в «{row['where']}»)")
        else:
            dupes.append(row["label"])
    return keep, dupes, lives


def remember(it: dict) -> None:
    index.add(it)
    index.save()
