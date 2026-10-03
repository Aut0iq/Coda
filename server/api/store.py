"""Очередь, текущая задача и история загрузок (на диске).

Логика та же, что была в Telegram-боте; от чатов избавились, пути и лимит
истории приходят в конструктор.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("hub.store")

# ============================ состояние загрузок =============================
class Store:
    """Очередь, текущая задача и история. Историю держим на диске."""

    def __init__(self, state_file: Path, history_limit: int = 500) -> None:
        self.state_file = state_file
        self.queue_file = state_file.with_name("queue.json")
        self.history_limit = history_limit
        self.pending: list[dict] = []
        self.active: dict | None = None
        self.history: list[dict] = []
        self.revision = 0
        self.history_rev = 0          # меняется только когда пополняется история
        self.notice: dict | None = None   # итог фоновой операции для мини-аппа
        self.busy: set[str] = set()       # что сейчас выполняется: {"playlists"}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text("utf-8"))
            self.history = raw.get("history", [])
            log.info("История загружена: %d записей", len(self.history))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Не смог прочитать %s: %s", self.state_file, exc)
        self._load_queue()

    def _load_queue(self) -> None:
        """Очередь, прерванная перезапуском. Незаконченная задача встаёт первой
        и продолжается: скачанное раньше (done_keys) повторно не трогаем."""
        try:
            raw = json.loads(self.queue_file.read_text("utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("Не смог прочитать %s: %s", self.queue_file, exc)
            return
        self.pending = [j for j in raw.get("pending") or [] if j.get("items") is not None]
        active = raw.get("active")
        if active and active.get("items") is not None and not active.get("cancel"):
            active.update(status="queued", failed=0, failed_tracks=[], stage="",
                          now=None, resumed=True)
            for k in ("push", "scan", "playlists", "error", "started"):
                active.pop(k, None)
            self.pending.insert(0, active)
        if self.pending:
            log.info("Очередь восстановлена: %d задач%s", len(self.pending),
                     " (прерванная — первой)" if active else "")

    def save_queue(self) -> None:
        try:
            keep = lambda j: {k: v for k, v in j.items() if k != "now"}
            data = {"pending": [keep(j) for j in self.pending],
                    "active": keep(self.active) if self.active else None}
            tmp = self.queue_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self.queue_file)
        except Exception as exc:
            log.warning("Не смог записать %s: %s", self.queue_file, exc)

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"history": self.history[-self.history_limit:]}, ensure_ascii=False),
                "utf-8",
            )
            tmp.replace(self.state_file)
        except Exception as exc:
            log.warning("Не смог записать %s: %s", self.state_file, exc)

    def touch(self) -> None:
        self.revision += 1

    def notify(self, text: str, kind: str) -> None:
        self.notice = {"text": text, "kind": kind, "at": time.time()}
        self.touch()

    def find(self, job_id: str) -> dict | None:
        if self.active and self.active["id"] == job_id:
            return self.active
        for j in self.pending:
            if j["id"] == job_id:
                return j
        for h in reversed(self.history):
            if h.get("id") == job_id:
                return h
        return None

    def add(self, job: dict) -> None:
        self.pending.append(job)
        self.save_queue()
        self.touch()

    def start(self, job_id: str) -> dict | None:
        for i, j in enumerate(self.pending):
            if j["id"] == job_id:
                self.active = self.pending.pop(i)
                self.active["status"] = "downloading"
                self.active["started"] = time.time()
                self.save_queue()
                self.touch()
                return self.active
        return None

    def finish(self, status: str) -> None:
        if not self.active:
            return
        self.active["status"] = status
        self.active["finished"] = time.time()
        self.active.pop("items", None)
        self.active.pop("track_titles", None)
        self.history.append(self.active)
        self.active = None
        self.history_rev += 1
        self.save()
        self.save_queue()
        self.touch()

    def cancel(self, job_id: str) -> bool:
        for i, j in enumerate(self.pending):
            if j["id"] == job_id:
                self.pending.pop(i)
                self.save_queue()
                self.touch()
                return True
        return False

    def has_album(self, album_id: str) -> bool:
        if self.active and self.active.get("album_id") == album_id:
            return True
        if any(j.get("album_id") == album_id for j in self.pending):
            return True
        return any(
            h.get("album_id") == album_id and h.get("status") == "done"
            for h in self.history
        )

    def fixed_ids(self) -> set[str]:
        """Записи с ошибкой или частичные, которые потом исправлены: альбом
        перекачан позже, и последняя запись по нему — «done»."""
        last: dict[str, dict] = {}
        for h in self.history:
            if h.get("album_id"):
                last[str(h["album_id"])] = h
        out: set[str] = set()
        for h in self.history:
            aid = h.get("album_id")
            if aid and h.get("status") in ("error", "partial"):
                newest = last.get(str(aid))
                if newest is not h and newest.get("status") == "done":
                    out.add(h.get("id"))
        return out

    def remove_history(self, ids: set[str]) -> int:
        """Убирает записи из истории (только список в боте — файлы не трогаем)."""
        before = len(self.history)
        self.history = [h for h in self.history if h.get("id") not in ids]
        removed = before - len(self.history)
        if removed:
            self.history_rev += 1
            self.save()
            self.touch()
        return removed

    def owned(self) -> list[str]:
        return sorted(
            {
                str(h["album_id"])
                for h in self.history
                if h.get("album_id") and h.get("status") in ("done", "partial")
            }
            | {str(j["album_id"]) for j in self.pending if j.get("album_id")}
            | (
                {str(self.active["album_id"])}
                if self.active and self.active.get("album_id")
                else set()
            )
        )

    def progress(self) -> dict[str, dict]:
        """album_id -> сколько скачано из скольких: для пометок в сетке альбомов."""
        out: dict[str, dict] = {}
        for h in self.history:
            aid = h.get("album_id")
            if aid:
                out[str(aid)] = {"done": h.get("done", 0), "total": h.get("total", 0),
                                 "raw": h.get("total_raw") or h.get("total", 0),
                                 "status": h.get("status", "")}
        return out

    def snapshot(self, *, history: bool = True) -> dict:
        snap = {
            "revision": self.revision,
            "history_rev": self.history_rev,
            "active": _light(self.active),
            "pending": [_light(j) for j in self.pending],
            "owned": self.owned(),
            "notice": self.notice,
            "busy": sorted(self.busy),
        }
        if history:
            fixed = self.fixed_ids()
            snap["history"] = [
                _hist(h) | ({"fixed": True} if h.get("id") in fixed else {})
                for h in reversed(self.history[-self.history_limit:])
            ]
        return snap


# в очереди и в активной задаче длинные списки не нужны
_HEAVY = ("items", "track_titles", "tracks")


def _light(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {k: v for k, v in job.items() if k not in _HEAVY}


def _hist(job: dict) -> dict:
    """Запись истории для списка: без треклистов и чатов, длинные списки — счётчиками.
    Старые записи (до 21 сентября) тащили в каждом ответе полный треклист."""
    out = {k: v for k, v in job.items()
           if k not in _HEAVY + ("chat_id", "user_id", "now", "skipped_dupes",
                                 "skipped_live", "failed_tracks", "not_found", "via")}
    out["n_dupes"] = len(job.get("skipped_dupes") or [])
    out["n_live"] = len(job.get("skipped_live") or [])
    out["n_failed"] = len(job.get("failed_tracks") or [])
    out["n_not_found"] = len(job.get("not_found") or [])
    via = job.get("via") or {}
    out["n_soundcloud"] = len(via.get("soundcloud") or [])
    out["n_cookies"] = len(via.get("cookies") or [])
    return out
