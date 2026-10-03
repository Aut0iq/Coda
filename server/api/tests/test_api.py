import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tests import _env  # noqa: F401

import auth
import downloader
import library
import main
import playlists
import sources

H = {"Authorization": "Bearer test-token"}


def fake_job(jid="job1", album="42"):
    items = [{"nn": i, "title": f"t{i}", "artist": "Art", "album": "Alb", "album_artist": "Art",
              "cover_url": ""} for i in (1, 2, 3)]
    return {"id": jid, "album_id": album, "kind": "album", "album_artist": "Art", "album_name": "Alb",
            "year": "2020", "items": items, "track_titles": [i["title"] for i in items],
            "total": 3, "total_raw": 3, "done": 0, "failed": 0, "failed_tracks": []}


class ApiCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = main.Hub(Path(self.tmp.name) / "data", str(Path(self.tmp.name) / "music"))
        self.client = TestClient(TestServer(main.build_app(self.hub, "test-token")))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    # ---- доступ ----
    async def test_auth(self):
        c = self.client
        self.assertEqual((await c.get("/healthz")).status, 200)
        self.assertEqual((await c.get("/api/info")).status, 401)
        self.assertEqual((await c.get("/api/info", headers={"Authorization": "Bearer nope"})).status, 401)
        self.assertEqual((await c.get("/api/info", headers={"Authorization": "test-token"})).status, 401)
        r = await c.get("/api/info", headers=H)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual((body["name"], body["api"]), ("music-hub", main.API_LEVEL))

    async def test_bruteforce_is_locked_per_ip(self):
        c = self.client
        for _ in range(auth.MAX_FAILS):
            await c.get("/api/info", headers={"Authorization": "Bearer x", "X-Forwarded-For": "6.6.6.6"})
        r = await c.get("/api/info", headers={**H, "X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(r.status, 429)                     # даже верный токен — после блокировки
        r = await c.get("/api/info", headers={**H, "X-Forwarded-For": "7.7.7.7"})
        self.assertEqual(r.status, 200)                     # чужой адрес не страдает

    async def test_old_telegram_header_is_not_accepted(self):
        r = await self.client.get("/api/status", headers={"X-Init-Data": "query_id=1"})
        self.assertEqual(r.status, 401)

    async def test_app_static_served_without_token(self):
        r = await self.client.get("/app/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.headers["Content-Type"])
        self.assertEqual((await self.client.get("/app/style.css")).status, 200)

    # ---- настройки ----
    async def test_settings_roundtrip_applies_live(self):
        c = self.client
        r = await c.post("/api/settings", headers=H, json={"audio_format": "mp3", "concurrency": 2,
                                                            "skip_live": False,
                                                            "proxies": ["socks5://1.2.3.4:1080"]})
        self.assertEqual(r.status, 200)
        self.assertEqual(downloader.FMT, "mp3")
        self.assertFalse(library.SKIP_LIVE)
        self.assertEqual(downloader.PROXIES, [("socks5://1.2.3.4:1080", "прокси 1")])
        self.assertEqual(self.hub.concurrency, 2)
        meta = await (await c.get("/api/meta", headers=H)).json()
        self.assertEqual((meta["format"], meta["proxies"], meta["concurrency"]), ("mp3", 1, 2))
        got = await (await c.get("/api/settings", headers=H)).json()
        self.assertEqual(got["values"]["audio_format"], "mp3")
        # вернуть как было, чтобы не влиять на остальные тесты
        await c.post("/api/settings", headers=H, json={"audio_format": "m4a", "skip_live": True,
                                                       "proxies": [], "concurrency": 3})

    async def test_settings_rejects_garbage(self):
        r = await self.client.post("/api/settings", headers=H, json={"concurrency": 100})
        self.assertEqual(r.status, 400)
        self.assertIn("concurrency", (await r.json())["error"])
        r = await self.client.post("/api/settings", headers=H, data="not json")
        self.assertEqual(r.status, 400)

    # ---- cookies ----
    async def test_cookies_flow(self):
        c = self.client
        r = await c.post("/api/cookies", headers=H, json={"text": "мусор"})
        self.assertEqual(r.status, 400)
        self.assertFalse((await (await c.get("/api/cookies", headers=H)).json())["have"])

        good = "\n".join(f".youtube.com\tTRUE\t/\tTRUE\t1900000000\t{n}\tv" for n in ("SID", "LOGIN_INFO"))
        orig = downloader.check_cookies
        downloader.check_cookies = lambda: ("ok", "")
        try:
            r = await c.post("/api/cookies", headers=H, json={"text": good})
            body = await r.json()
            self.assertEqual((r.status, body["saved"], body["status"]), (200, 2, "ok"))
            info = await (await c.get("/api/cookies", headers=H)).json()
            self.assertTrue(info["have"])
            self.assertEqual(info["status"], "ok")
        finally:
            downloader.check_cookies = orig
        self.assertEqual((await c.delete("/api/cookies", headers=H)).status, 200)
        self.assertFalse((await (await c.get("/api/cookies", headers=H)).json())["have"])

    # ---- проверка запретов ----
    async def test_netcheck_endpoint(self):
        called = []

        async def fake_run(proxies=None):
            called.append(proxies)
            return netcheck_result

        import netcheck
        netcheck_result = netcheck.evaluate({"country": "RU", "ip": "1.1.1.1"},
                                            [{"id": "youtube", "name": "YouTube", "state": "reset", "ms": 1, "detail": ""}])
        orig = netcheck.run_checks
        netcheck.run_checks = fake_run
        try:
            snap = await (await self.client.get("/api/netcheck", headers=H)).json()
            self.assertIsNone(snap["result"])
            r = await (await self.client.post("/api/netcheck/run", headers=H)).json()
            self.assertTrue(r["started"])
            for _ in range(50):
                await asyncio.sleep(0.02)
                snap = await (await self.client.get("/api/netcheck", headers=H)).json()
                if not snap["running"]:
                    break
            self.assertEqual(snap["result"]["advice"], ["vpn", "zapret"])
            self.assertGreater(snap["checked_at"], 0)
        finally:
            netcheck.run_checks = orig

    # ---- очередь ----
    async def test_enqueue_and_cancel(self):
        orig = main.get_album_job

        async def fake(album_id):
            return fake_job(album=album_id)

        main.get_album_job = fake
        try:
            r = await self.client.post("/api/enqueue", headers=H, json={"album_id": "42"})
            body = await r.json()
            self.assertEqual((r.status, body["position"]), (200, 1))
            st = await (await self.client.get("/api/status", headers=H)).json()
            self.assertEqual(len(st["pending"]), 1)
            self.assertNotIn("items", st["pending"][0])
            self.assertIn("42", st["owned"])
            r = await (await self.client.post(f"/api/cancel/{body['job']}", headers=H)).json()
            self.assertTrue(r["ok"])
            self.assertEqual((await (await self.client.get("/api/status", headers=H)).json())["pending"], [])
        finally:
            main.get_album_job = orig

    async def test_find_marks_what_is_already_in_library(self):
        orig_search, orig_lib = sources.search_catalog, library.library_for
        seen = {}

        async def fake_search(query, limit=12):
            return ([{"id": 1, "title": "Have It", "duration": 200, "artist": {"name": "Art"},
                      "album": {"id": 10, "title": "Alb", "cover_medium": "c1"}},
                     {"id": 2, "title": "Miss It", "duration": 180, "artist": {"name": "Art"},
                      "album": {"id": 11, "title": "Other", "cover_medium": "c2"}}],
                    [{"id": 10, "title": "Alb", "nb_tracks": 9, "record_type": "album",
                      "artist": {"name": "Art"}, "cover_medium": "c1"},
                     {"id": 11, "title": "Other", "nb_tracks": 3, "record_type": "ep",
                      "artist": {"name": "Art"}, "cover_medium": "c2"}])

        async def fake_library(artists):
            seen["artists"] = artists
            return {library.song_key("Art", "Have It"): "Alb"}

        sources.search_catalog, library.library_for = fake_search, fake_library
        try:
            self.assertEqual(await (await self.client.get("/api/find?q=a", headers=H)).json(),
                             {"tracks": [], "albums": []})
            body = await (await self.client.get("/api/find?q=art", headers=H)).json()
        finally:
            sources.search_catalog, library.library_for = orig_search, orig_lib
        self.assertEqual(seen["artists"], {"Art"})
        self.assertEqual([(t["id"], t["owned"]) for t in body["tracks"]], [("1", True), ("2", False)])
        self.assertEqual(body["tracks"][1], {"id": "2", "title": "Miss It", "artist": "Art", "album": "Other",
                                             "album_id": "11", "duration": 180, "cover": "c2", "owned": False})
        self.assertEqual([(a["id"], a["kind"], a["total"], a["owned"]) for a in body["albums"]],
                         [("10", "album", 9, True), ("11", "ep", 3, False)])

    async def test_find_reports_catalog_failure(self):
        orig = sources.search_catalog

        async def boom(query, limit=12):
            raise RuntimeError("Deezer: quota")

        sources.search_catalog = boom
        try:
            r = await self.client.get("/api/find?q=art", headers=H)
        finally:
            sources.search_catalog = orig
        self.assertEqual((r.status, (await r.json())["error"]), (502, "Deezer: quota"))

    async def test_enqueue_single_track(self):
        orig = sources.get_track_job

        async def fake(track_id):
            job = fake_job(jid="trk" + track_id, album=None)
            job["kind"] = "track"
            return job

        sources.get_track_job = fake
        try:
            r = await self.client.post("/api/enqueue", headers=H, json={"track_id": 7})
            body = await r.json()
            self.assertEqual((r.status, body["job"]), (200, "trk7"))
            job = await (await self.client.get(f"/api/job/{body['job']}", headers=H)).json()
            self.assertEqual(job["kind"], "track")
            self.assertNotIn("items", job)
        finally:
            sources.get_track_job = orig

    async def test_enqueue_requires_something(self):
        r = await self.client.post("/api/enqueue", headers=H, json={})
        self.assertEqual(r.status, 400)

    async def test_status_long_poll_returns_on_change(self):
        rev = self.hub.store.revision
        task = asyncio.create_task(self.client.get(f"/api/status?since={rev}", headers=H))
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())
        self.hub.store.touch()
        r = await asyncio.wait_for(task, 3)
        self.assertEqual((await r.json())["revision"], rev + 1)

    async def test_img_proxy_only_allows_deezer_hosts(self):
        r = await self.client.get("/api/img?u=https://evil.example/x.jpg")
        self.assertEqual(r.status, 403)


class WorkerLifecycle(unittest.IsolatedAsyncioTestCase):
    """Задача проходит весь путь: анализ → загрузка → скан → плейлисты → история."""

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = main.Hub(Path(self.tmp.name) / "data", str(Path(self.tmp.name) / "music"))
        self.saved = (library.filter_items, downloader.fetch_sync, downloader.get_cover,
                      playlists.sync_artist, playlists.sync_recent, library.remember)
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

        library.filter_items = filter_items
        downloader.get_cover = get_cover
        playlists.sync_artist = sync_artist
        library.remember = lambda it: None
        self.hub.scan = scan

    async def asyncTearDown(self):
        (library.filter_items, downloader.fetch_sync, downloader.get_cover,
         playlists.sync_artist, playlists.sync_recent, library.remember) = self.saved
        self.tmp.cleanup()

    async def run_one(self, job):
        self.hub.enqueue(job)
        w = asyncio.create_task(self.hub.worker())
        await asyncio.wait_for(self.hub.queue.join(), 5)
        w.cancel()
        return self.hub.store.history[-1]

    async def test_all_downloaded(self):
        downloader.fetch_sync = lambda it, root, cover: (True, "")
        rec = await self.run_one(fake_job())
        self.assertEqual((rec["status"], rec["done"], rec["total"]), ("done", 3, 3))
        self.assertEqual(rec["playlists"], "🎧 Плейлисты — ок")
        self.assertEqual(self.scans, 1)
        self.assertNotIn("items", rec)
        self.assertIsNone(self.hub.store.active)

    async def test_partial_and_sources(self):
        results = iter([(True, ""), (True, "soundcloud"), (False, "ролик недоступен")])
        downloader.fetch_sync = lambda it, root, cover: next(results)
        rec = await self.run_one(fake_job())
        self.assertEqual((rec["status"], rec["done"], rec["failed"]), ("partial", 2, 1))
        self.assertEqual(len(rec["via"]["soundcloud"]), 1)
        self.assertIn("ролик недоступен", rec["failed_tracks"][0])

    async def test_all_failed_is_error_and_skips_scan(self):
        downloader.fetch_sync = lambda it, root, cover: (False, "нет аудио")
        rec = await self.run_one(fake_job())
        self.assertEqual(rec["status"], "error")
        self.assertIn("нет аудио", rec["error"])
        self.assertEqual(self.scans, 0)

    async def test_proxy_source_is_recorded(self):
        downloader.fetch_sync = lambda it, root, cover: (True, "proxy2")
        rec = await self.run_one(fake_job())
        self.assertEqual(len(rec["via"]["proxy2"]), 3)

    async def test_resume_skips_downloaded(self):
        calls = []
        downloader.fetch_sync = lambda it, root, cover: calls.append(it["title"]) or (True, "")
        job = fake_job()
        job["done"], job["done_keys"] = 1, ["1|t1"]
        rec = await self.run_one(job)
        self.assertEqual(calls, ["t2", "t3"])
        self.assertEqual((rec["status"], rec["total"]), ("done", 3))

    async def test_incoming_dir_is_hidden_from_navidrome(self):
        from pathlib import Path
        tmp = Path(downloader.TMP_DIR)
        self.assertTrue(tmp.name.startswith("."))
        self.assertTrue((tmp / ".ndignore").exists())


if __name__ == "__main__":
    unittest.main()
