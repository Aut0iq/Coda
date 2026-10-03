"""
Подбор аудио на YouTube по метаданным Deezer, скачивание yt-dlp и теги mutagen.

Почему не spotdl: YouTube Music отдаёт серверу пустую выдачу, spotdl
откатывается на слепой поиск и молча качает не то (или ничего, но с кодом 0).
Здесь каждый кандидат из обычного поиска YouTube оценивается по длительности,
названию и каналу (официальные «Artist - Topic» в приоритете), а результат
проверяется по длительности скачанного файла.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, COMM
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from yt_dlp import YoutubeDL

from sources import norm

log = logging.getLogger("music-bot.dl")

FMT = os.environ.get("AUDIO_FORMAT") or os.environ.get("SPOTDL_FORMAT", "m4a")
if FMT not in ("m4a", "mp3"):
    FMT = "m4a"
# cookies YouTube (Netscape-формат). Используются ТОЛЬКО для роликов 18+,
# файл загружается в настройках приложения.
COOKIE_FILE = os.environ.get("COOKIE_FILE") or str(Path(__file__).resolve().parent / "cookies.txt")
SOUNDCLOUD = os.environ.get("SOUNDCLOUD_FALLBACK", "1") == "1"
# запасные выходы в сеть: тот же yt-dlp, но запросы идут через прокси (другой регион/IP).
# PROXY_URLS — список через запятую, пробуются по очереди; пусто — повторной попытки нет.
# PROXY_URL / PROXY2_URL — старые имена, поддерживаются для совместимости.
PROXY_LIST = [u.strip() for u in (
    os.environ.get("PROXY_URLS", "").split(",")
    + [os.environ.get("PROXY_URL", ""), os.environ.get("PROXY2_URL", "")]
) if u.strip()]
PROXY = PROXY_LIST[0] if PROXY_LIST else ""
PROXIES = [(url, f"прокси {n}") for n, url in enumerate(dict.fromkeys(PROXY_LIST), start=1)]
# временные файлы кладём рядом с библиотекой (та же файловая система): готовый трек
# переезжает атомарным rename, и Navidrome не видит недокачанное
TMP_DIR = os.environ.get("MB_TMP_DIR") or None
AGE_RE = re.compile(r"confirm your age|age.restricted|inappropriate for some users", re.I)
# YouTube принял дата-центровый адрес за бота; из-под аккаунта пускает
BOT_RE = re.compile(r"not a bot", re.I)
MAX_CANDIDATES = 8        # сколько кандидатов вообще перебираем (включая 18+)
TRACK_NUMBERS = os.environ.get("TRACK_NUMBERS", "1") == "1"
SEARCH_RESULTS = int(os.environ.get("SEARCH_RESULTS", "10"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "55"))
MAX_TRIES = int(os.environ.get("MAX_TRIES", "3"))

# слова, которых не должно быть в названии видео, если их нет в названии трека
BAD_WORDS = [
    "live", "concert", "cover", "karaoke", "instrumental", "remix", "sped up",
    "speed up", "slowed", "nightcore", "8d", "reaction", "reverb", "acoustic",
    "full album", "tutorial", "lesson", "guitar", "piano", "drum", "bass boosted",
    "лайв", "концерт", "живое", "кавер", "караоке", "минус", "реакция", "разбор",
    "полный альбом", "урок", "remastered 20", "extended", "mashup", "fan made",
    "ai cover", "перевод", "rus sub", "teaser", "snippet", "trailer", "mmd",
    "dance practice", "3dmv", "3d music video", "short ver", "tv size", "game ver",
    "a cappella", "acapella", "а капелла", "акапелла", "vocals only", "only vocals",
]
# мягкий штраф: скорее всего тот же звук, но не официальный аплоад
SOFT_WORDS = [
    "lyrics", "lyric", "letra", "текст", "kan", "rom", "eng", "sub", "color coded",
    "pv", "mv", "music video", "official video", "клип", "full ver", "full version",
]
BRACKETS_RE = re.compile(r"\s*[\(\[（【][^\)\]）】]*[\)\]）】]")


def sanitize(name: str) -> str:
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "", name).strip().strip(".")
    return name[:150] or "Unknown"


def dest_path(root: str, it: dict) -> Path:
    base = sanitize(it["title"])
    if TRACK_NUMBERS and it.get("nn"):
        nn = f"{it['nn']:02d}"
        if it.get("discs", 1) > 1:
            nn = f"{it['disc']}-{nn}"
        base = f"{nn} - {base}"
    return Path(root) / sanitize(it["album_artist"]) / sanitize(it["album"]) / f"{base}.{FMT}"


# ------------------------------- оценка -------------------------------------
def _has(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def score(c: dict, it: dict) -> float | None:
    """Чем больше, тем вероятнее, что видео — нужный трек. None — точно нет."""
    title = norm(c.get("title", ""))
    channel = norm(c.get("channel") or c.get("uploader") or "")
    hay = f"{title} {channel}"
    s = 0.0

    # длительность — самый надёжный признак
    target, dur = it.get("duration") or 0, c.get("duration") or 0
    if target and dur:
        tol = max(12, target * 0.07)
        diff = abs(dur - target)
        if diff > tol * 2:
            return None
        s += 35 * max(0.0, 1 - diff / tol)
    elif target and not dur:
        s -= 10

    artist = norm(it["artist"])
    topic = channel.endswith(" topic")
    own_topic = topic and bool(artist) and artist in channel

    # слова названия (без пояснений в скобках) должны быть в названии видео
    want = norm(BRACKETS_RE.sub("", it["title"])).split() or norm(it["title"]).split()
    if want:
        cov = sum(1 for w in want if _has(hay, w)) / len(want)
        if cov < 0.6:
            # официальный Topic с точной длительностью — это он, просто название
            # на другом языке (フラジール вместо Fragile)
            if not (own_topic and target and dur and abs(dur - target) <= 2):
                return None
            cov = 0.4
        s += 35 * cov
        extra = norm(" ".join(BRACKETS_RE.findall(it["title"]))).split()
        if extra:
            s += 5 * sum(1 for w in extra if _has(hay, w)) / len(extra)

    # исполнитель
    a_words = artist.split()
    if a_words:
        s += 15 * sum(1 for w in a_words if _has(hay, w)) / len(a_words)

    # официальный звук: «Artist - Topic», VEVO, канал самого артиста
    if own_topic:
        s += 25
    elif topic:
        s += 10
    elif artist and (channel == artist or channel == artist.replace(" ", "")):
        s += 12
    elif "vevo" in channel:
        s += 8

    # лишнее: живые версии, каверы, ремиксы и т.п.
    ref = norm(f"{it['title']} {it['album']}")
    for w in BAD_WORDS:
        if _has(title, w) and not _has(ref, w):
            s -= 40
    if not topic:
        for w in SOFT_WORDS:
            if _has(title, w) and not _has(ref, w):
                s -= 7
    return s


# ------------------------------ yt-dlp --------------------------------------
def have_cookies() -> bool:
    try:
        return os.path.getsize(COOKIE_FILE) > 0
    except OSError:
        return False


# ролики 18+ для проверки cookies: если один удалят, берём следующий
COOKIE_TEST_VIDEOS = [v for v in os.environ.get(
    "COOKIE_TEST_VIDEOS", "Mujq-iZU35E,rpJCn8Nfo_A,YDqY3_DjpdA").split(",") if v]
STALE_RE = re.compile(r"no longer valid|sign in to confirm|login required|not a bot", re.I)


def check_cookies() -> tuple[str, str]:
    """Живы ли cookies: ok | invalid | absent | unknown (+ пояснение).

    Проверяем на ролике 18+: без годных cookies YouTube его не отдаёт.
    «unknown» — когда ролик сам недоступен или не отвечает сеть: это не повод
    кричать, что cookies протухли.
    """
    if not have_cookies():
        return "absent", "файл cookies не подключён"

    # 1) плейлист «Понравившиеся» виден только вошедшему в аккаунт — это и есть
    #    проверка самих cookies, не зависящая от того, что YouTube сделал с роликами
    try:
        opts = _base_opts(cookies=True) | {"extract_flat": "in_playlist",
                                           "skip_download": True, "playlistend": 1}
        with YoutubeDL(opts) as y:
            info = y.extract_info("https://www.youtube.com/playlist?list=LL", download=False)
        if info is not None and info.get("entries") is not None:
            return "ok", ""
    except Exception as exc:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines()[0]
        if STALE_RE.search(msg) or "private" in msg.lower() or "does not exist" in msg.lower():
            return "invalid", "YouTube больше не принимает эти cookies"
        log.debug("проверка cookies по плейлисту LL: %s", msg[:150])

    # 2) запасной путь: ролик 18+ без годных cookies не отдаётся
    last = ""
    for vid in COOKIE_TEST_VIDEOS:
        try:
            with YoutubeDL(_base_opts(cookies=True) | {"skip_download": True}) as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            if info and (info.get("formats") or info.get("url")):
                return "ok", ""
            last = "ролик отдался без аудио"
        except Exception as exc:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines()[0]
            if AGE_RE.search(msg) or STALE_RE.search(msg):
                return "invalid", "YouTube больше не принимает эти cookies"
            last = msg[:150]
            log.debug("проверка cookies на %s: %s", vid, last)
    return "unknown", last or "не смог проверить"


def _base_opts(cookies: bool = False, proxy: str = "") -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
    }
    if cookies and have_cookies():
        o["cookiefile"] = COOKIE_FILE
    if proxy:
        o["proxy"] = proxy
    return o


def _search(query: str, n: int, proxy: str = "") -> list[dict]:
    opts = _base_opts(proxy=proxy) | {"extract_flat": "in_playlist", "skip_download": True}
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries") or [] if e and e.get("id")]


def find_candidates(it: dict, proxy: str = "") -> list[tuple[float, dict]]:
    core = BRACKETS_RE.sub("", it["title"]).strip() or it["title"]
    queries = [f"{it['artist']} {core} topic", f"{it['artist']} - {core}"]
    if it.get("album") and it["album"] != it["title"]:
        queries.append(f"{it['artist']} {core} {it['album']}")
    seen: dict[str, tuple[float, dict]] = {}
    for q in queries:
        try:
            results = _search(q, SEARCH_RESULTS, proxy=proxy)
        except Exception as exc:
            log.warning("поиск «%s» упал: %s", q, exc)
            continue
        for c in results:
            sc = score(c, it)
            if sc is not None and c["id"] not in seen:
                seen[c["id"]] = (sc, c)
        best = max((v[0] for v in seen.values()), default=0)
        if best >= 90:          # уже нашли уверенно — второй запрос не нужен
            break
    ranked = sorted(seen.values(), key=lambda x: -x[0])
    return [r for r in ranked if r[0] >= MIN_SCORE]


def find_soundcloud(it: dict) -> list[tuple[float, dict]]:
    core = BRACKETS_RE.sub("", it["title"]).strip() or it["title"]
    try:
        results = _search_sc(f"{it['artist']} {core}", 8)
    except Exception as exc:
        log.warning("поиск SoundCloud упал: %s", exc)
        return []
    ranked = []
    for c in results:
        sc = score(c, it)
        if sc is not None and sc >= MIN_SCORE - 10:
            ranked.append((sc, c))
    return sorted(ranked, key=lambda x: -x[0])


def _search_sc(query: str, n: int) -> list[dict]:
    opts = _base_opts() | {"extract_flat": "in_playlist", "skip_download": True}
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"scsearch{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries") or [] if e and e.get("url")]


def playlist_entries(url: str, limit: int = 200) -> tuple[str, list[dict]]:
    """Содержимое плейлиста YouTube без скачивания: (название, треки)."""
    opts = _base_opts() | {"extract_flat": "in_playlist", "skip_download": True,
                           "playlistend": limit}
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e and e.get("id")]
    return (info or {}).get("title") or "Плейлист YouTube", entries


def _download(url: str, tmpdir: str, cookies: bool = False, proxy: str = "") -> tuple[Path, dict]:
    if FMT == "m4a":
        # 18 = mp4 360p с AAC: единственное, что YouTube отдаёт по 18+ роликам,
        # если возраст аккаунта не подтверждён документом
        fmt = "140/bestaudio[ext=m4a]/bestaudio/18/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
    else:
        fmt = "bestaudio/18/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
    opts = _base_opts(cookies, proxy) | {
        "format": fmt,
        "outtmpl": os.path.join(tmpdir, "a.%(ext)s"),
        "postprocessors": [pp],
        "noplaylist": True,
    }
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    path = Path(tmpdir) / f"a.{FMT}"
    if not path.exists():
        raise RuntimeError("файл после скачивания не найден")
    return path, info


def _audio_len(path: Path) -> float:
    return (MP4(path) if FMT == "m4a" else MP3(path)).info.length


# -------------------------------- теги --------------------------------------
def _tag(path: Path, it: dict, cover: bytes | None, source: str) -> None:
    year = (it.get("date") or "")[:10]
    if FMT == "m4a":
        f = MP4(path)
        f.delete()
        f["\xa9nam"] = it["title"]
        f["\xa9ART"] = it["artist"]
        f["aART"] = it["album_artist"]
        f["\xa9alb"] = it["album"]
        if it.get("nn"):
            f["trkn"] = [(it["nn"], it.get("total") or 0)]
        f["disk"] = [(it.get("disc") or 1, it.get("discs") or 1)]
        if year:
            f["\xa9day"] = year
        if it.get("genre"):
            f["\xa9gen"] = it["genre"]
        if it.get("isrc"):
            f["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(it["isrc"].encode())]
        f["\xa9cmt"] = source
        if cover:
            f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    else:
        f = MP3(path, ID3=ID3)
        f.delete()
        f.tags = ID3()
        t = f.tags
        t.add(TIT2(encoding=3, text=it["title"]))
        t.add(TPE1(encoding=3, text=it["artist"]))
        t.add(TPE2(encoding=3, text=it["album_artist"]))
        t.add(TALB(encoding=3, text=it["album"]))
        if it.get("nn"):
            t.add(TRCK(encoding=3, text=f"{it['nn']}/{it.get('total') or 0}"))
        t.add(TPOS(encoding=3, text=f"{it.get('disc') or 1}/{it.get('discs') or 1}"))
        if year:
            t.add(TDRC(encoding=3, text=year))
        if it.get("genre"):
            t.add(TCON(encoding=3, text=it["genre"]))
        if it.get("isrc"):
            t.add(TSRC(encoding=3, text=it["isrc"]))
        t.add(COMM(encoding=3, lang="eng", desc="", text=source))
        if cover:
            t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        f.save()


# ---------------------------- один трек целиком ------------------------------
def fetch_sync(it: dict, root: str, cover: bytes | None) -> tuple[bool, str]:
    """Возвращает (успех, пояснение). Блокирующая — вызывать через to_thread."""
    if it.get("video_id") and not it.get("title"):
        # голая ссылка YouTube: метаданные берём из самого видео
        url = f"https://www.youtube.com/watch?v={it['video_id']}"
        try:
            with YoutubeDL(_base_opts() | {"skip_download": True}) as y:
                info = y.extract_info(url, download=False)
        except Exception as exc:
            if not AGE_RE.search(str(exc)):
                raise
            if not have_cookies():
                return False, "18+ на YouTube, нужен cookies.txt (загрузи в настройках)"
            with YoutubeDL(_base_opts(cookies=True) | {"skip_download": True}) as y:
                info = y.extract_info(url, download=False)
        it["title"] = info.get("track") or info.get("title") or it["video_id"]
        it["artist"] = info.get("artist") or info.get("creator") or info.get("uploader") or "Unknown"
        it["artist"] = re.sub(r"\s*-\s*Topic$", "", it["artist"])
        it["album_artist"] = it["artist"]
        it["album"] = info.get("album") or "YouTube"
        it["date"] = (info.get("release_date") or info.get("upload_date") or "")[:4]

    dest = dest_path(root, it)
    if dest.exists() and dest.stat().st_size > 100_000:
        return True, "уже был"

    # video_id — ролик, на который пользователь показал сам (ссылка или плейлист):
    # берём его первым, а если не подойдёт, ищем обычным путём
    if it.get("video_id"):
        cands = [(100.0, {"id": it["video_id"], "title": it["title"]})]
    else:
        cands = find_candidates(it)

    last_err, tries, age_hit = "", 0, False
    age_later: list[tuple[str, str]] = []
    for sc, c in cands[:MAX_CANDIDATES]:
        if tries >= MAX_TRIES:
            break
        url = f"https://www.youtube.com/watch?v={c['id']}"
        ok, err, logged = _try(url, it, cover, dest, f"{c['id']} «{c.get('title')}» (score {sc:.0f})")
        if ok:
            return True, f"{c['id']} (cookies)" if logged else c["id"]
        if AGE_RE.search(err):
            age_hit = True
            # 18+ не считаем попыткой: сначала ищем обычные загрузки и SoundCloud,
            # а через cookies качаем в последнюю очередь (там только 360p-звук)
            age_later.append((url, c["id"]))
            log.info("  %s: 18+, отложил", c["id"])
            continue
        tries += 1
        last_err = err

    # ролик из ссылки не подошёл — пробуем найти трек поиском
    if it.get("video_id") and it.get("duration"):
        for sc, c in find_candidates(it)[:MAX_TRIES]:
            if c["id"] == it["video_id"]:
                continue
            ok, err, logged = _try(f"https://www.youtube.com/watch?v={c['id']}", it, cover, dest,
                                   f"{c['id']} «{c.get('title')}» (score {sc:.0f})")
            if ok:
                return True, f"{c['id']} (cookies)" if logged else c["id"]
            last_err = err

    # запасной источник — SoundCloud (там тоже бывают оригиналы от лейблов)
    if SOUNDCLOUD:
        for sc, c in find_soundcloud(it)[:2]:
            ok, err = _attempt(c["url"], it, cover, dest,
                               f"SoundCloud «{c.get('title')}» от {c.get('uploader')} (score {sc:.0f})")
            if ok:
                return True, "soundcloud"
            last_err = last_err or err

    if age_later and have_cookies():
        for url, vid in age_later[:2]:
            ok, err = _attempt(url, it, cover, dest, f"{vid} с cookies (18+)", cookies=True)
            if ok:
                return True, f"{vid} (18+)"
            last_err = err

    # всё здешнее исчерпано — пробуем с запасных серверов по очереди: у них другой
    # регион, и выдача поиска там тоже другая. Следующий — только если не вышло на
    # предыдущем.
    for n, (proxy, name) in enumerate(PROXIES, start=1):
        reason = "proxy" if n == 1 else f"proxy{n}"
        if it.get("video_id"):
            ok, err, _ = _try(f"https://www.youtube.com/watch?v={it['video_id']}", it, cover,
                              dest, f"{it['video_id']} с {name}", proxy=proxy)
            if ok:
                return True, reason
            last_err = err
        try:
            remote = find_candidates(it, proxy=proxy)
        except Exception as exc:
            remote, last_err = [], str(exc)[:150]
        for sc, c in remote[:MAX_TRIES]:
            ok, err, _ = _try(f"https://www.youtube.com/watch?v={c['id']}", it, cover, dest,
                              f"{c['id']} «{c.get('title')}» с {name} (score {sc:.0f})",
                              proxy=proxy)
            if ok:
                return True, reason
            last_err = err

    if age_hit and not have_cookies():
        return False, "18+ на YouTube, нужен cookies.txt (загрузи в настройках)"
    return False, human_error(last_err) or "не нашёл подходящего аудио"


def human_error(err: str) -> str:
    """Короткая причина вместо простыни yt-dlp со ссылками на FAQ."""
    if not err:
        return ""
    if BOT_RE.search(err):
        return "YouTube принял сервер за бота и не отдал ролик"
    if AGE_RE.search(err):
        return "ролик 18+, нужны рабочие cookies"
    if re.search(r"video unavailable|not available|private video|removed", err, re.I):
        return "ролик недоступен"
    if "Requested format is not available" in err:
        return "у ролика нет аудиопотока"
    err = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?[\w-]{11}:\s*", "", err)
    err = re.sub(r"\s*(Use --cookies|See\s+https?://).*$", "", err)
    return err[:120]


def _try(url: str, it: dict, cover: bytes | None, dest: Path, label: str,
         proxy: str = "") -> tuple[bool, str, bool]:
    """Попытка, а если YouTube просит «подтвердить, что не бот», — повтор с cookies.
    -> (успех, ошибка, понадобились ли cookies)."""
    ok, err = _attempt(url, it, cover, dest, label, proxy=proxy)
    if not ok and BOT_RE.search(err) and have_cookies():
        log.info("  %s: YouTube просит войти — повторяю с cookies", label)
        ok, err = _attempt(url, it, cover, dest, label + " с cookies", cookies=True, proxy=proxy)
        if ok:
            return True, "", True
    return ok, err, False


def _attempt(url: str, it: dict, cover: bytes | None, dest: Path, label: str,
             cookies: bool = False, proxy: str = "") -> tuple[bool, str]:
    tmpdir = tempfile.mkdtemp(prefix="mb-", dir=TMP_DIR)
    try:
        try:
            path, _ = _download(url, tmpdir, cookies, proxy)
        except Exception as exc:
            if "403" not in str(exc):
                raise
            log.info("  %s: 403, пробую ещё раз", label)       # часто разовый сбой
            path, _ = _download(url, tmpdir, cookies, proxy)
        length = _audio_len(path)
        if it.get("duration") and abs(length - it["duration"]) > max(15, it["duration"] * 0.1):
            err = f"длительность {length:.0f}с вместо {it['duration']}с"
            log.info("  отбросил %s: %s", label, err)
            return False, err
        _tag(path, it, cover, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        log.info("OK  %s — %s  <- %s", it["artist"], it["title"], label)
        return True, ""
    except Exception as exc:
        err = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines()[0][:200]
        log.warning("  %s не скачался: %s", label, err)
        return False, err
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def get_cover(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.content
    except Exception as exc:
        log.warning("обложка не скачалась: %s", exc)
        return None
