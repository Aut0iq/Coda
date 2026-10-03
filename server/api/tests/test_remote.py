"""Передача на другой сервер: настоящий SSH+SFTP-сервер поднимается прямо в тесте (asyncssh)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import asyncssh

from tests import _env  # noqa: F401

import remote


class _Srv(asyncssh.SSHServer):
    """Вход по паролю или по ключу из <root>/.ssh/authorized_keys — как у настоящего sshd."""

    def __init__(self, root: Path, password: str) -> None:
        self.root, self.password = root, password

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return password == self.password

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        try:
            text = (self.root / ".ssh" / "authorized_keys").read_text()
        except OSError:
            return False
        blob = key.export_public_key("openssh").decode().split()[1]
        return any(blob in line for line in text.splitlines())


class _SshBase(unittest.IsolatedAsyncioTestCase):
    """Поднимает «сервер Navidrome» (SSH+SFTP в корне self.nav) и каталоги сервера скачивания."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.nav = base / "nav"            # «диск сервера Navidrome»
        self.data = base / "data"          # /data сервера скачивания
        self.stage = base / "stage"        # /music сервера скачивания
        for d in (self.nav, self.data, self.stage):
            d.mkdir()
        self.hostkey = asyncssh.generate_private_key("ssh-ed25519")
        self.srv = None
        self.port = 0
        await self.start_server()
        self.said = []

    async def asyncTearDown(self):
        await self.stop_server()
        self.tmp.cleanup()

    async def start_server(self, hostkey=None):
        root = self.nav
        self.srv = await asyncssh.create_server(
            lambda: _Srv(root, "pw"), "127.0.0.1", self.port, server_host_keys=[hostkey or self.hostkey],
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root).encode()))
        self.port = self.srv.sockets[0].getsockname()[1]

    async def stop_server(self):
        if self.srv:
            self.srv.close()
            await self.srv.wait_closed()
            self.srv = None

    def params(self, **kw):
        p = {"host": "127.0.0.1", "port": self.port, "user": "u", "password": "pw", "music_dir": "/lib"}
        p.update(kw)
        return p

    def say(self, kind, text):
        self.said.append((kind, text))

    def put(self, rel, data=b"x" * 1000, age=60):
        p = self.stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        old = time.time() - age
        os.utime(p, (old, old))
        return p

    async def configure(self):
        return await remote.setup(self.data, self.params(), self.say)



class RemoteTests(_SshBase):
    # ------------------------------------------------------------------
    async def test_setup_installs_key_and_creates_folder(self):
        info = await self.configure()
        self.assertEqual(info["music_dir"], "/lib")
        self.assertTrue((self.nav / "lib").is_dir())
        auth = (self.nav / ".ssh" / "authorized_keys").read_text()
        self.assertIn("ssh-ed25519", auth)
        cfg = json.loads((self.data / "remote.json").read_text())
        self.assertNotIn("password", json.dumps(cfg).lower().replace("nd_pass", ""))   # пароль сервера не хранится
        self.assertTrue(cfg["host_key"].startswith("ssh-ed25519 "))
        self.assertTrue(remote.key_path(self.data).exists())
        if os.name != "nt":
            self.assertEqual(oct((self.data / "remote.json").stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(remote.key_path(self.data).stat().st_mode & 0o777), "0o600")
        self.assertTrue(any(k == "warn" for k, _ in self.said))        # адрес Navidrome не указан
        # повторная настройка не плодит ключи
        await self.configure()
        self.assertEqual((self.nav / ".ssh" / "authorized_keys").read_text().count("ssh-ed25519"), 1)
        self.assertEqual(remote.Remote(self.data).info()["host"], "127.0.0.1")
        self.assertNotIn("nd_pass", remote.Remote(self.data).info())

    async def test_wrong_password_and_bad_params(self):
        with self.assertRaises(remote.RemoteError) as cm:
            await remote.setup(self.data, self.params(password="nope"), self.say)
        self.assertEqual(cm.exception.code, "REMOTE_AUTH")
        self.assertFalse((self.data / "remote.json").exists())
        for bad, code in (({"music_dir": "music"}, "REMOTE_ARGS"), ({"music_dir": "/a/../b"}, "REMOTE_ARGS"),
                          ({"host": ""}, "REMOTE_ARGS"), ({"port": "x"}, "REMOTE_ARGS"),
                          ({"nd_url": "navidrome.local"}, "REMOTE_ARGS")):
            with self.assertRaises(remote.RemoteError) as cm:
                await remote.setup(self.data, self.params(**bad), self.say)
            self.assertEqual(cm.exception.code, code, bad)

    @unittest.skipIf(os.name == "nt", "модуль pwd есть только в Linux; тест гоняется в контейнере")
    async def test_works_without_a_local_user_entry(self):
        """В контейнере под uid 1000 нет записи в passwd: getpass.getuser() падает, а asyncssh зовёт его при подключении."""
        import pwd
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k not in ("LOGNAME", "USER", "LNAME", "USERNAME")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(pwd, "getpwuid", side_effect=KeyError("uid")):
            with self.assertRaises(Exception):          # условие воспроизведено: getuser() действительно падает
                __import__("getpass").getuser()
            await self.configure()
            self.put("Art/Alb/t.m4a")
            res = await remote.Remote(self.data).sync(self.stage, min_age=0)
        self.assertIsNone(res["error"], res)
        self.assertTrue((self.nav / "lib/Art/Alb/t.m4a").exists())

    async def test_closed_port(self):
        await self.stop_server()
        with self.assertRaises(remote.RemoteError) as cm:
            await remote.setup(self.data, self.params(), self.say)
        self.assertEqual(cm.exception.code, "REMOTE_CONN")

    async def test_sync_moves_files_and_cleans_up(self):
        await self.configure()
        a = self.put("Artist/Album/01 - a.m4a", b"A" * 5000)
        self.put("Artist/Album/cover.jpg", b"C" * 100)
        self.put(".incoming/dl.part", b"zzz")
        self.put(".incoming/.ndignore", b"")
        r = remote.Remote(self.data)
        seen = []
        res = await r.sync(self.stage, progress=lambda *a: seen.append(a), min_age=0)
        self.assertIsNone(res["error"], res)
        self.assertEqual((res["files"], res["bytes"]), (2, 5100))
        self.assertEqual((self.nav / "lib/Artist/Album/01 - a.m4a").read_bytes(), b"A" * 5000)
        self.assertTrue((self.nav / "lib/Artist/Album/cover.jpg").exists())
        self.assertFalse(a.exists())                                     # у нас удалено
        self.assertFalse((self.stage / "Artist").exists())               # пустые папки убраны
        self.assertTrue((self.stage / ".incoming/dl.part").exists())     # служебное не тронуто
        self.assertFalse(list((self.nav / "lib").rglob("*" + remote.PART)))
        self.assertTrue(seen and seen[-1][0] == 2)
        self.assertTrue(r.info(self.stage)["last"]["ok"])
        self.assertEqual(r.info(self.stage)["pending_files"], 0)

    async def test_overwrites_existing_remote_file(self):
        await self.configure()
        (self.nav / "lib/Artist/Album").mkdir(parents=True)
        (self.nav / "lib/Artist/Album/t.m4a").write_bytes(b"old")
        self.put("Artist/Album/t.m4a", b"NEW" * 10)
        res = await remote.Remote(self.data).sync(self.stage, min_age=0)
        self.assertIsNone(res["error"], res)
        self.assertEqual((self.nav / "lib/Artist/Album/t.m4a").read_bytes(), b"NEW" * 10)

    async def test_offline_keeps_files_and_retries(self):
        await self.configure()
        self.put("Artist/Album/t1.m4a")
        r = remote.Remote(self.data)
        await self.stop_server()
        res = await r.sync(self.stage, min_age=0)
        self.assertEqual(res["code"], "REMOTE_CONN")
        self.assertEqual(res["files"], 0)
        self.assertTrue((self.stage / "Artist/Album/t1.m4a").exists())   # ничего не потеряно
        self.assertEqual(r.info(self.stage)["pending_files"], 1)
        self.assertFalse(r.info(self.stage)["last"]["ok"])
        await self.start_server()                                        # сервер вернулся (тот же ключ хоста)
        res = await r.sync(self.stage, min_age=0)
        self.assertIsNone(res["error"], res)
        self.assertTrue((self.nav / "lib/Artist/Album/t1.m4a").exists())

    async def test_changed_host_key_is_refused(self):
        await self.configure()
        self.put("Artist/Album/t1.m4a")
        await self.stop_server()
        await self.start_server(hostkey=asyncssh.generate_private_key("ssh-ed25519"))   # «подмена»
        res = await remote.Remote(self.data).sync(self.stage, min_age=0)
        self.assertEqual(res["code"], "REMOTE_HOSTKEY")
        self.assertTrue((self.stage / "Artist/Album/t1.m4a").exists())
        t = await remote.Remote(self.data).test()
        self.assertFalse(t["ok"])

    async def test_fresh_files_are_not_touched(self):
        await self.configure()
        self.put("Artist/Album/new.m4a", age=0)                          # только что записан — ещё может писаться
        res = await remote.Remote(self.data).sync(self.stage, min_age=5)
        self.assertEqual((res["total"], res["files"]), (0, 0))
        self.assertTrue((self.stage / "Artist/Album/new.m4a").exists())

    async def test_test_and_unconfigured(self):
        r = remote.Remote(self.data)
        self.assertFalse(r.configured)
        self.assertEqual(r.info(), {"configured": False})
        self.assertEqual((await r.sync(self.stage))["files"], 0)
        self.assertFalse((await r.test())["ok"])
        await self.configure()
        self.assertTrue(r.configured)
        self.assertTrue((await r.test())["ok"])


def _fake_job(jid="job1", n=3):
    items = [{"nn": i, "title": f"t{i}", "artist": "Art", "album": "Alb", "album_artist": "Art", "cover_url": ""}
             for i in range(1, n + 1)]
    return {"id": jid, "album_id": "42", "kind": "album", "album_artist": "Art", "album_name": "Alb",
            "year": "2020", "items": items, "track_titles": [i["title"] for i in items],
            "total": n, "total_raw": n, "done": 0, "failed": 0, "failed_tracks": []}


class RemoteJobTests(_SshBase):
    """Задача целиком: скачано → передано на другой сервер → пересканирование."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import downloader, library, playlists, main
        self.mods = (downloader, library, playlists)
        self.saved = (library.filter_items, downloader.fetch_sync, downloader.get_cover,
                      playlists.sync_artist, library.remember)
        self.scans = 0

        async def filter_items(job):
            return job["items"], [], []

        async def get_cover(url):
            return None

        async def sync_artist(name):
            return "🎧 Плейлисты — ок"

        async def scan():
            self.scans += 1
            return "scan"

        def fetch(it, root, cover):
            path = downloader.dest_path(root, it)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"AUDIO" * 200)
            return True, ""

        library.filter_items, downloader.get_cover = filter_items, get_cover
        playlists.sync_artist, library.remember = sync_artist, lambda it: None
        downloader.fetch_sync = fetch
        self.hub = main.Hub(self.data, str(self.stage))
        self.hub.scan = scan

    async def asyncTearDown(self):
        downloader, library, playlists = self.mods
        (library.filter_items, downloader.fetch_sync, downloader.get_cover,
         playlists.sync_artist, library.remember) = self.saved
        await super().asyncTearDown()

    async def run_one(self, job):
        import asyncio
        self.hub.enqueue(job)
        w = asyncio.create_task(self.hub.worker())
        await asyncio.wait_for(self.hub.queue.join(), 30)
        w.cancel()
        return self.hub.store.history[-1]

    async def test_downloaded_files_go_to_the_other_server_then_scan(self):
        await self.configure()
        self.assertEqual(self.hub.role, "remote")
        rec = await self.run_one(_fake_job())
        self.assertEqual((rec["status"], rec["done"], rec.get("uploaded")), ("done", 3, 3))
        got = sorted(p.name for p in (self.nav / "lib/Art/Alb").iterdir())
        self.assertEqual(len(got), 3, got)
        self.assertTrue(all(n.endswith(".m4a") or n.endswith(".mp3") for n in got), got)
        self.assertEqual(list(self.stage.rglob("*.m4a")) + list(self.stage.rglob("*.mp3")), [])   # у нас не осталось
        self.assertEqual(self.scans, 1)
        self.assertNotIn("upload", rec)

    async def test_server_down_keeps_music_and_notifies_once_then_recovers(self):
        await self.configure()
        await self.stop_server()
        rec = await self.run_one(_fake_job())
        self.assertEqual(rec["status"], "done")
        self.assertEqual(self.scans, 0)                                   # сканировать нечего
        self.assertIn("ждёт передачи", rec["scan"])
        self.assertEqual(self.hub.store.notice["kind"], "remote")
        left = [p for p in self.stage.rglob("*") if p.is_file() and not p.name.startswith(".")]
        self.assertEqual(len(left), 3)                                    # ничего не потеряно
        first = self.hub.store.notice["at"]
        await self.hub.push_remote(None)                                  # вторая неудачная попытка — без нового уведомления
        self.assertEqual(self.hub.store.notice["at"], first)
        await self.start_server()
        res = await self.hub.push_remote(None)                            # то, что делает фоновая досылка
        self.assertEqual((res["files"], res["error"]), (3, None))
        self.assertEqual(self.hub.store.notice["kind"], "remote")
        self.assertIn("восстановилась", self.hub.store.notice["text"])
        self.assertEqual(len(list((self.nav / "lib/Art/Alb").iterdir())), 3)

    async def test_api_endpoints(self):
        from aiohttp.test_utils import TestClient, TestServer
        import main
        client = TestClient(TestServer(main.build_app(self.hub, "test-token")))
        await client.start_server()
        try:
            H = {"Authorization": "Bearer test-token"}
            self.assertEqual((await (await client.get("/api/info", headers=H)).json())["role"], "local")
            self.assertEqual((await (await client.get("/api/remote", headers=H)).json()), {"configured": False})
            self.assertEqual((await client.post("/api/remote/sync", headers=H)).status, 400)
            await self.configure()
            info = await (await client.get("/api/info", headers=H)).json()
            self.assertEqual(info["role"], "remote")
            r = await (await client.get("/api/remote", headers=H)).json()
            self.assertTrue(r["configured"])
            self.assertNotIn("nd_pass", r)
            self.assertNotIn("host_key", r)
            self.assertTrue((await (await client.post("/api/remote/test", headers=H)).json())["ok"])
            self.put("Art/Alb/x.m4a")
            res = await (await client.post("/api/remote/sync", headers=H)).json()
            self.assertEqual((res["ok"], res["files"]), (True, 1))
            self.assertTrue((self.nav / "lib/Art/Alb/x.m4a").exists())
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
