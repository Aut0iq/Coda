"""
Передача музыки на ДРУГОЙ сервер, где стоит Navidrome.

Схема: сервер скачивания (где крутится этот API) сам заходит по SSH на сервер Navidrome и кладёт
файлы по SFTP в указанную там папку. Пароль нужен один раз — при настройке: мы кладём на тот сервер
свой открытый ключ (в authorized_keys), дальше вход идёт только по ключу, пароль нигде не хранится.
Ключ хоста запоминается при настройке и проверяется при каждом подключении.

На сервере Navidrome нужен только SSH с SFTP (оболочка не требуется).

Файлы лежат у нас в промежуточной папке (/music) и после успешной передачи удаляются: недоехавшее
(сеть, сервер выключен) остаётся и уедет при следующей попытке.
"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import os
import posixpath
import secrets
import time
from pathlib import Path
from typing import Callable

import asyncssh
import httpx

log = logging.getLogger("hub.remote")

CONFIG_NAME = "remote.json"
STATE_NAME = "remote_state.json"
KEY_DIR = "ssh"
KEY_NAME = "id_ed25519"
PART = ".musichub-part"          # временное имя на сервере Navidrome: сканер такие файлы не знает
SKIP_SUFFIXES = (".part", ".tmp", ".ytdl", PART)
MIN_FREE = 2 * 1024 ** 3         # меньше — предупреждаем


class RemoteError(Exception):
    """code — машинное имя для приложения (REMOTE_AUTH, REMOTE_CONN…), text — по-русски для человека."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


# ------------------------------ конфиг ------------------------------
def load_config(data_dir: Path) -> dict | None:
    try:
        cfg = json.loads((Path(data_dir) / CONFIG_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if cfg.get("host") and cfg.get("music_dir") else None


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_config(data_dir: Path, cfg: dict) -> None:
    _write_private(Path(data_dir) / CONFIG_NAME, json.dumps(cfg, ensure_ascii=False, indent=1))


def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEY_DIR / KEY_NAME


def _known_hosts(cfg: dict):
    host, port = cfg["host"], int(cfg.get("port") or 22)
    pattern = host if port == 22 else f"[{host}]:{port}"
    return asyncssh.import_known_hosts(f"{pattern} {cfg['host_key']}\n")


def _ensure_local_user() -> None:
    """asyncssh при каждом подключении спрашивает имя локального пользователя (getpass.getuser). В контейнере под
    uid 1000 записи в passwd нет, и это падало бы с «Unknown local username» — подставляем имя сами."""
    try:
        getpass.getuser()
    except Exception:
        os.environ["USER"] = "music-hub"


def _map_connect_error(exc: BaseException, who: str) -> RemoteError:
    if isinstance(exc, asyncssh.PermissionDenied):
        return RemoteError("REMOTE_AUTH", f"{who}: неверный логин или пароль (или вход по паролю отключён)")
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return RemoteError("REMOTE_HOSTKEY", f"{who}: ключ сервера не совпадает с запомненным. Если систему не "
                                             "переустанавливали, это может быть подмена — повтори настройку только если уверен")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return RemoteError("REMOTE_CONN", f"{who}: не отвечает. Проверь адрес и порт SSH и что сервер включён")
    if isinstance(exc, ConnectionRefusedError):
        return RemoteError("REMOTE_CONN", f"{who}: отказал в соединении — порт SSH закрыт или указан не тот")
    if isinstance(exc, OSError):
        return RemoteError("REMOTE_CONN", f"{who}: не удалось подключиться ({exc.strerror or exc})")
    return RemoteError("REMOTE_CONN", f"{who}: {exc}")


async def _connect_key(cfg: dict, data_dir: Path) -> asyncssh.SSHClientConnection:
    """Вход по нашему ключу на сервер Navidrome, ключ хоста сверяется с запомненным."""
    _ensure_local_user()
    kp = key_path(data_dir)
    if not kp.exists():
        raise RemoteError("REMOTE_NOKEY", "Нет ключа для сервера Navidrome — настрой передачу заново")
    try:
        return await asyncssh.connect(
            cfg["host"], int(cfg.get("port") or 22), username=cfg["user"],
            client_keys=[str(kp)], agent_path=None, known_hosts=_known_hosts(cfg),
            password=None, connect_timeout=15, keepalive_interval=20, keepalive_count_max=3)
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
        raise _map_connect_error(exc, "Сервер Navidrome") from exc


# ------------------------------ настройка ------------------------------
async def navidrome_ping(url: str, user: str, password: str) -> tuple[bool, str]:
    """Subsonic ping с токен-авторизацией. -> (ok, пояснение)."""
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    params = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "music-hub", "f": "json"}
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(url.rstrip("/") + "/rest/ping.view", params=params)
        body = r.json()["subsonic-response"]
    except Exception as exc:
        return False, f"Navidrome по адресу {url} не отвечает ({type(exc).__name__})"
    if body.get("status") == "ok":
        return True, "ok"
    return False, "Navidrome не принял логин или пароль" if (body.get("error") or {}).get("code") in (40, 41) \
        else f"Navidrome ответил ошибкой: {(body.get('error') or {}).get('message', 'неизвестно')}"


def _check_params(p: dict) -> dict:
    host = str(p.get("host") or "").strip()
    user = str(p.get("user") or "").strip()
    music_dir = str(p.get("music_dir") or "").strip()
    try:
        port = int(p.get("port") or 22)
    except (TypeError, ValueError):
        raise RemoteError("REMOTE_ARGS", "Порт SSH сервера Navidrome должен быть числом")
    if not host or not user or not p.get("password"):
        raise RemoteError("REMOTE_ARGS", "Укажи адрес, логин и пароль сервера Navidrome")
    if any(c in host for c in " /\\'\"") or not 0 < port < 65536:
        raise RemoteError("REMOTE_ARGS", "Странный адрес или порт сервера Navidrome")
    if not music_dir.startswith("/") or ".." in music_dir.split("/") or len(music_dir) < 2:
        raise RemoteError("REMOTE_ARGS", "Папка с музыкой на сервере Navidrome — полный путь, например /srv/music")
    nd_url = str(p.get("nd_url") or "").strip()
    if nd_url and not nd_url.startswith(("http://", "https://")):
        raise RemoteError("REMOTE_ARGS", "Адрес Navidrome должен начинаться с http:// или https://")
    return {"host": host, "port": port, "user": user, "music_dir": posixpath.normpath(music_dir),
            "nd_url": nd_url.rstrip("/"), "nd_user": str(p.get("nd_user") or ""), "nd_pass": str(p.get("nd_pass") or "")}


async def setup(data_dir: Path, params: dict, say: Callable[[str, str], None]) -> dict:
    """
    Одноразовая настройка по паролю: ключ → authorized_keys на сервере Navidrome → папка с музыкой →
    проверка входа по ключу → проверка самого Navidrome. Пароль остаётся только в памяти.
    say(kind, text): kind — step | log | warn. При ошибке бросает RemoteError.
    """
    data_dir = Path(data_dir)
    _ensure_local_user()
    cfg = _check_params(params)
    password = str(params["password"])

    say("step", "Подключаюсь к серверу Navidrome")
    try:
        conn = await asyncssh.connect(
            cfg["host"], cfg["port"], username=cfg["user"], password=password,
            client_keys=None, agent_path=None, known_hosts=None, connect_timeout=15)
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
        raise _map_connect_error(exc, "Сервер Navidrome") from exc

    async with conn:
        hk = conn.get_server_host_key()
        cfg["host_key"] = hk.export_public_key("openssh").decode().strip()
        cfg["fingerprint"] = hk.get_fingerprint()
        say("log", f"Отпечаток ключа сервера Navidrome: {cfg['fingerprint']}")

        kp = key_path(data_dir)
        if not kp.exists():
            kp.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(kp.parent, 0o700)
            key = asyncssh.generate_private_key("ssh-ed25519", comment="music-hub")
            tmp = kp.with_suffix(".tmp")
            tmp.write_bytes(key.export_private_key("openssh"))
            os.chmod(tmp, 0o600)
            tmp.replace(kp)
        pub = asyncssh.read_private_key(str(kp)).export_public_key("openssh").decode().strip()
        free = None

        say("step", "Кладу ключ и проверяю папку с музыкой")
        try:
            async with conn.start_sftp_client() as sftp:
                await _install_key(sftp, pub)
                try:
                    await sftp.makedirs(cfg["music_dir"], exist_ok=True)
                    probe = posixpath.join(cfg["music_dir"], f".musichub-{secrets.token_hex(3)}.test")
                    async with sftp.open(probe, "wb") as f:
                        await f.write(b"ok")
                    await sftp.remove(probe)
                except asyncssh.SFTPPermissionDenied as exc:
                    raise RemoteError("REMOTE_DIR", f"На сервере Navidrome нет прав писать в {cfg['music_dir']}. "
                                                    "Выбери папку, которая принадлежит этому пользователю, "
                                                    "или войди под пользователем с доступом к ней") from exc
                except asyncssh.SFTPError as exc:
                    raise RemoteError("REMOTE_DIR", f"Не получилось создать папку {cfg['music_dir']}: {exc}") from exc
                free = await _free_bytes(sftp, cfg["music_dir"])
        except RemoteError:
            raise
        except (asyncssh.ChannelOpenError, asyncssh.SFTPError) as exc:
            raise RemoteError("REMOTE_SFTP", "На сервере Navidrome выключен SFTP. Включи подсистему sftp в sshd_config "
                                             "(строка «Subsystem sftp …») и повтори") from exc
    # ключ работает без пароля? (иначе передача потом молча не заработает)
    say("step", "Проверяю вход по ключу")
    kconn = await _connect_key(cfg, data_dir)
    async with kconn:
        async with kconn.start_sftp_client() as sftp:
            await sftp.stat(cfg["music_dir"])

    if free is not None:
        say("log", f"Свободно на сервере Navidrome: {free / 1024 ** 3:.1f} ГБ")
        if free < MIN_FREE:
            say("warn", "На сервере Navidrome мало свободного места")

    cfg["configured_at"] = int(time.time())
    if cfg["nd_url"]:
        say("step", "Проверяю Navidrome")
        ok, why = await navidrome_ping(cfg["nd_url"], cfg["nd_user"], cfg["nd_pass"])
        if not ok:
            if "логин" in why:
                raise RemoteError("REMOTE_ND_AUTH", why)
            say("warn", why + ". Передача файлов будет работать, но проверка дублей, пересканирование и плейлисты — нет")
    else:
        say("warn", "Адрес Navidrome не указан: дубли не проверяются, пересканирование и плейлисты не работают")

    save_config(data_dir, cfg)
    return {k: v for k, v in cfg.items() if k not in ("nd_pass", "host_key")}


async def _install_key(sftp: asyncssh.SFTPClient, pub: str) -> None:
    """Дописывает наш ключ в ~/.ssh/authorized_keys через SFTP (оболочка на сервере не нужна)."""
    try:
        await sftp.mkdir(".ssh")
    except asyncssh.SFTPError:
        pass                                       # уже есть
    try:
        await sftp.chmod(".ssh", 0o700)
    except asyncssh.SFTPError:
        pass
    old = b""
    try:
        async with sftp.open(".ssh/authorized_keys", "rb") as f:
            old = await f.read()
    except asyncssh.SFTPNoSuchFile:
        pass
    blob = pub.split()[1]
    if blob.encode() not in old:
        new = old + (b"" if not old or old.endswith(b"\n") else b"\n") + pub.encode() + b"\n"
        async with sftp.open(".ssh/authorized_keys", "wb") as f:
            await f.write(new)
    try:
        await sftp.chmod(".ssh/authorized_keys", 0o600)
    except asyncssh.SFTPError:
        pass


async def _free_bytes(sftp: asyncssh.SFTPClient, path: str) -> int | None:
    try:
        v = await sftp.statvfs(path)
        return int(v.f_bavail * v.f_frsize)
    except (asyncssh.SFTPError, AttributeError, TypeError):
        return None                                # сервер не умеет statvfs — не страшно


# ------------------------------ передача ------------------------------
def iter_files(root: Path, min_age: float = 0.0) -> list[tuple[Path, str, int]]:
    """Готовые файлы промежуточной папки: (путь, относительный путь через «/», размер)."""
    now = time.time()
    out: list[tuple[Path, str, int, float]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]      # .incoming и прочее скрытое
        for fn in filenames:
            if fn.startswith(".") or fn.endswith(SKIP_SUFFIXES):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            if min_age and now - st.st_mtime < min_age:                   # ещё пишется
                continue
            out.append((p, p.relative_to(root).as_posix(), st.st_size, st.st_mtime))
    out.sort(key=lambda t: t[3])
    return [(p, rel, size) for p, rel, size, _ in out]


def _prune_empty(root: Path) -> None:
    for dirpath, dirnames, _ in os.walk(root, topdown=False):
        if Path(dirpath) == root or Path(dirpath).name.startswith("."):
            continue
        try:
            os.rmdir(dirpath)                       # только пустые
        except OSError:
            pass


class Remote:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = asyncio.Lock()

    # ---- сведения ----
    @property
    def configured(self) -> bool:
        return load_config(self.data_dir) is not None

    def _state(self) -> dict:
        try:
            return json.loads((self.data_dir / STATE_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw) -> None:
        st = self._state()
        st.update(kw)
        try:
            (self.data_dir / STATE_NAME).write_text(json.dumps(st, ensure_ascii=False), "utf-8")
        except OSError as exc:
            log.warning("не записал состояние передачи: %s", exc)

    def info(self, root: Path | None = None) -> dict:
        """Без секретов: показывается в приложении."""
        cfg = load_config(self.data_dir)
        if not cfg:
            return {"configured": False}
        pend = iter_files(root) if root else []
        return {"configured": True, "host": cfg["host"], "port": cfg.get("port", 22), "user": cfg["user"],
                "music_dir": cfg["music_dir"], "nd_url": cfg.get("nd_url", ""),
                "fingerprint": cfg.get("fingerprint", ""),
                "pending_files": len(pend), "pending_bytes": sum(s for _, _, s in pend),
                "last": self._state().get("last") or {}}

    # ---- проверка связи ----
    async def test(self) -> dict:
        cfg = load_config(self.data_dir)
        if not cfg:
            return {"ok": False, "error": "Сервер Navidrome не настроен"}
        try:
            conn = await _connect_key(cfg, self.data_dir)
            async with conn:
                async with conn.start_sftp_client() as sftp:
                    await sftp.stat(cfg["music_dir"])
                    free = await _free_bytes(sftp, cfg["music_dir"])
        except RemoteError as exc:
            return {"ok": False, "code": exc.code, "error": exc.text}
        except (asyncssh.Error, OSError) as exc:
            return {"ok": False, "code": "REMOTE_CONN", "error": f"Сервер Navidrome недоступен: {exc}"}
        return {"ok": True, "free_bytes": free}

    # ---- передача ----
    async def sync(self, root: Path, progress: Callable[[int, int, int, int], None] | None = None,
                   min_age: float = 0.0) -> dict:
        """
        Переносит всё готовое из root на сервер Navidrome (и удаляет у нас). min_age — не трогать файлы
        моложе стольких секунд (для фоновой досылки, пока идёт другая работа).
        -> {files, bytes, total, errors:[…], error: str|None}. Не бросает: итог — в словаре.
        """
        async with self._lock:
            cfg = load_config(self.data_dir)
            res: dict = {"files": 0, "bytes": 0, "total": 0, "errors": [], "error": None}
            if not cfg:
                return res
            files = await asyncio.to_thread(iter_files, Path(root), min_age)
            res["total"] = len(files)
            if not files:
                return res
            total_bytes = sum(s for _, _, s in files)
            try:
                conn = await _connect_key(cfg, self.data_dir)
            except RemoteError as exc:
                res["error"], res["code"] = exc.text, exc.code
                self._save_state(last={"at": int(time.time()), "ok": False, "error": exc.text})
                return res
            try:
                async with conn:
                    async with conn.start_sftp_client() as sftp:
                        made: set[str] = set()
                        for path, rel, size in files:
                            remote = posixpath.join(cfg["music_dir"], rel)
                            part = remote + PART
                            try:
                                parent = posixpath.dirname(remote)
                                if parent not in made:
                                    await sftp.makedirs(parent, exist_ok=True)
                                    made.add(parent)
                                await sftp.put(str(path), part)
                                got = (await sftp.stat(part)).size
                                if got != size:
                                    raise asyncssh.SFTPFailure(f"размер на сервере {got} вместо {size}")
                                try:
                                    await sftp.posix_rename(part, remote)
                                except (asyncssh.SFTPOpUnsupported, asyncssh.SFTPFailure):
                                    try:
                                        await sftp.remove(remote)
                                    except asyncssh.SFTPError:
                                        pass
                                    await sftp.rename(part, remote)
                                os.remove(path)
                            except asyncssh.SFTPError as exc:
                                res["errors"].append(f"{rel}: {exc}")
                                try:
                                    await sftp.remove(part)
                                except asyncssh.SFTPError:
                                    pass
                                continue
                            res["files"] += 1
                            res["bytes"] += size
                            if progress:
                                progress(res["files"], len(files), res["bytes"], total_bytes)
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                res["error"] = f"Связь с сервером Navidrome прервалась: {exc}"
                res["code"] = "REMOTE_CONN"
            await asyncio.to_thread(_prune_empty, Path(root))
            if res["errors"] and not res["error"]:
                res["error"] = f"{len(res['errors'])} файл(ов) не передано: {res['errors'][0]}"
            self._save_state(last={"at": int(time.time()), "ok": not res["error"], "files": res["files"],
                                   "bytes": res["bytes"], "error": res["error"] or ""})
            return res
