"""Настройки, которые меняются из приложения.

Лежат в DATA_DIR/settings.json. Движок читает свои параметры из переменных
окружения при импорте, поэтому main.py сначала зовёт apply_env(), а потом
импортирует downloader/library/playlists. Позже, когда настройки поменяли из
приложения, apply_live() переставляет те же значения в уже загруженных модулях —
перезапуск не нужен.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("hub.settings")

DEFAULTS: dict = {
    "audio_format": "m4a",       # m4a (AAC с YouTube без перекодирования) | mp3
    "concurrency": 3,            # сколько треков альбома качать одновременно
    "skip_dupes": True,          # не качать то, что уже есть в Navidrome
    "skip_live": True,           # пропускать live-версии в студийных альбомах
    "soundcloud": True,          # запасной источник
    "track_numbers": False,      # номер трека в имени файла (в тегах он есть всегда)
    "playlists": True,           # автоплейлисты по исполнителям
    "pl_min_tracks": 5,          # от скольких песен исполнителя создавать плейлист
    "proxies": [],               # запасные выходы: socks5://… / http://…
}

PROXY_RE = re.compile(r"^(socks5h?|socks4a?|https?)://[^\s,]+$", re.I)


def _int(v, lo: int, hi: int, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None
    if not lo <= n <= hi:
        raise ValueError(f"{name}: от {lo} до {hi}")
    return n


def _bool(v, name: str) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError(f"{name}: нужно true или false")


def validate(patch: dict) -> dict:
    """Проверяет присланные поля. Неизвестные ключи — ошибка, чтобы опечатка
    в приложении не терялась молча."""
    out: dict = {}
    for key, val in patch.items():
        if key not in DEFAULTS:
            raise ValueError(f"неизвестная настройка: {key}")
        if key == "audio_format":
            if val not in ("m4a", "mp3"):
                raise ValueError("audio_format: m4a или mp3")
            out[key] = val
        elif key == "concurrency":
            out[key] = _int(val, 1, 8, key)
        elif key == "pl_min_tracks":
            out[key] = _int(val, 1, 100, key)
        elif key == "proxies":
            if not isinstance(val, list):
                raise ValueError("proxies: нужен список")
            clean = []
            for u in val:
                u = str(u).strip()
                if not u:
                    continue
                if not PROXY_RE.match(u):
                    raise ValueError(f"прокси «{u[:40]}»: нужен адрес вида socks5://host:port")
                if u not in clean:
                    clean.append(u)
            if len(clean) > 5:
                raise ValueError("proxies: не больше пяти")
            out[key] = clean
        else:
            out[key] = _bool(val, key)
    return out


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.values: dict = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("не прочитал %s: %s", self.path, exc)
            return
        # битое или устаревшее поле не должно ронять запуск — берём остальное
        for key, val in raw.items():
            try:
                self.values.update(validate({key: val}))
            except ValueError as exc:
                log.warning("настройка %s пропущена: %s", key, exc)

    def update(self, patch: dict) -> dict:
        self.values.update(validate(patch))
        self.save()
        return self.values

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, ensure_ascii=False, indent=1), "utf-8")
        os.chmod(tmp, 0o600)           # в прокси могут быть логин и пароль
        tmp.replace(self.path)

    def env(self) -> dict[str, str]:
        v = self.values
        return {
            "AUDIO_FORMAT": v["audio_format"],
            "CONCURRENCY": str(v["concurrency"]),
            "SKIP_DUPES": "1" if v["skip_dupes"] else "0",
            "SKIP_LIVE": "1" if v["skip_live"] else "0",
            "SOUNDCLOUD_FALLBACK": "1" if v["soundcloud"] else "0",
            "TRACK_NUMBERS": "1" if v["track_numbers"] else "0",
            "PL_ENABLED": "1" if v["playlists"] else "0",
            "PL_MIN_TRACKS": str(v["pl_min_tracks"]),
            "PROXY_URLS": ",".join(v["proxies"]),
        }

    def apply_env(self) -> None:
        """До импорта движка: заданное в настройках важнее переменных окружения."""
        os.environ.update(self.env())
        # старые имена, если кто-то оставил их в окружении, не должны перебивать список
        os.environ.pop("PROXY_URL", None)
        os.environ.pop("PROXY2_URL", None)

    def apply_live(self, downloader, library, playlists) -> int:
        """Переставляет значения в уже импортированных модулях. -> новая параллельность."""
        v = self.values
        downloader.FMT = v["audio_format"]
        downloader.SOUNDCLOUD = v["soundcloud"]
        downloader.TRACK_NUMBERS = v["track_numbers"]
        downloader.PROXY_LIST = list(v["proxies"])
        downloader.PROXY = v["proxies"][0] if v["proxies"] else ""
        downloader.PROXIES = [(u, f"прокси {n}") for n, u in enumerate(v["proxies"], start=1)]
        library.SKIP_DUPES = v["skip_dupes"]
        library.SKIP_LIVE = v["skip_live"]
        playlists.PL_ENABLED = v["playlists"]
        playlists.PL_MIN_TRACKS = v["pl_min_tracks"]
        return v["concurrency"]
