import json
import os
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401

import auth
import cookies
import netcheck
import settings
from store import Store


class CookiesParse(unittest.TestCase):
    ROW = ".youtube.com\tTRUE\t/\tTRUE\t1900000000\t{name}\tvalue"

    def text(self, *names):
        return "# Netscape HTTP Cookie File\n" + "\n".join(self.ROW.format(name=n) for n in names)

    def test_ok(self):
        lines, err = cookies.parse(self.text("SID", "LOGIN_INFO", "PREF"))
        self.assertEqual(err, "")
        self.assertEqual(len(lines), 3)

    def test_spaces_instead_of_tabs_are_fixed(self):
        raw = self.text("SID", "__Secure-3PSID").replace("\t", " ")
        lines, err = cookies.parse(raw)
        self.assertEqual(err, "")
        self.assertEqual(len(lines), 2)

    def test_no_login_cookies(self):
        lines, err = cookies.parse(self.text("PREF", "VISITOR_INFO1_LIVE"))
        self.assertIsNone(lines)
        self.assertIn("SID", err)

    def test_not_youtube(self):
        lines, err = cookies.parse(".example.com\tTRUE\t/\tTRUE\t1900000000\tSID\tx")
        self.assertIsNone(lines)
        self.assertIn("Netscape", err)

    def test_service_cookies_skipped(self):
        lines, _ = cookies.parse(self.text("SID", "ST-abc"))
        self.assertEqual(len(lines), 1)

    def test_render_roundtrip(self):
        lines, _ = cookies.parse(self.text("SID"))
        self.assertTrue(cookies.render(lines).startswith("# Netscape HTTP Cookie File\n"))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_persist(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            s = settings.Settings(p)
            self.assertEqual(s.values["audio_format"], "m4a")
            s.update({"audio_format": "mp3", "concurrency": 5, "proxies": ["socks5://h:1080", "socks5://h:1080"]})
            again = settings.Settings(p)
            self.assertEqual(again.values["audio_format"], "mp3")
            self.assertEqual(again.values["concurrency"], 5)
            self.assertEqual(again.values["proxies"], ["socks5://h:1080"])     # дубль убран
            if os.name != "nt":      # в Windows прав POSIX нет
                self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")

    def test_validation(self):
        for bad in ({"concurrency": 0}, {"concurrency": "x"}, {"audio_format": "flac"},
                    {"proxies": ["not a proxy"]}, {"proxies": "socks5://h:1"}, {"nope": 1},
                    {"skip_dupes": "yes"}, {"proxies": [f"http://h:{i}" for i in range(6)]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                settings.validate(bad)

    def test_broken_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"concurrency": 99, "audio_format": "mp3", "junk": 1}))
            s = settings.Settings(p)
            self.assertEqual(s.values["concurrency"], 3)       # битое поле пропущено
            self.assertEqual(s.values["audio_format"], "mp3")  # здоровое взято

    def test_env_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings.Settings(Path(d) / "s.json")
            s.update({"proxies": ["socks5://a:1", "http://b:2"], "skip_live": False})
            env = s.env()
            self.assertEqual(env["PROXY_URLS"], "socks5://a:1,http://b:2")
            self.assertEqual(env["SKIP_LIVE"], "0")


class ThrottleTests(unittest.TestCase):
    def test_lock_after_max_fails_and_release(self):
        t = auth.Throttle()
        for i in range(auth.MAX_FAILS):
            self.assertFalse(t.locked("ip", now=100 + i))
            t.fail("ip", now=100 + i)
        self.assertTrue(t.locked("ip", now=120))
        self.assertFalse(t.locked("ip", now=100 + auth.BLOCK + 50))

    def test_success_resets(self):
        t = auth.Throttle()
        for _ in range(auth.MAX_FAILS - 1):
            t.fail("ip", now=1)
        t.ok("ip")
        t.fail("ip", now=2)
        self.assertFalse(t.locked("ip", now=3))

    def test_old_fails_expire(self):
        t = auth.Throttle()
        for _ in range(auth.MAX_FAILS - 1):
            t.fail("ip", now=0)
        t.fail("ip", now=auth.WINDOW + 5)
        self.assertFalse(t.locked("ip", now=auth.WINDOW + 6))

    def test_token_file_created_once(self):
        old = os.environ.pop("HUB_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "token"
                a = auth.load_token(p)
                self.assertGreaterEqual(len(a), 32)
                self.assertEqual(auth.load_token(p), a)
                if os.name != "nt":
                    self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        finally:
            if old is not None:
                os.environ["HUB_TOKEN"] = old


def chk(cid, state):
    return {"id": cid, "name": cid, "state": state, "ms": 1, "detail": ""}


ALL_OK = [chk("youtube", "ok"), chk("youtube_speed", "ok"), chk("deezer", "ok"), chk("soundcloud", "ok")]


class Evaluate(unittest.TestCase):
    def test_all_ok_in_russia_has_no_banner(self):
        r = netcheck.evaluate({"country": "RU"}, ALL_OK)
        self.assertEqual((r["level"], r["advice"], r["ru"]), ("ok", [], True))

    def test_russia_blocked_offers_vpn_and_zapret(self):
        checks = [chk("youtube", "reset"), chk("youtube_speed", "reset"), chk("deezer", "ok"), chk("soundcloud", "ok")]
        r = netcheck.evaluate({"country": "RU"}, checks)
        self.assertEqual(r["level"], "blocked")
        self.assertEqual(r["advice"], ["vpn", "zapret"])

    def test_russia_throttled_is_limited(self):
        checks = [chk("youtube", "ok"), chk("youtube_speed", "slow"), chk("deezer", "ok"), chk("soundcloud", "ok")]
        r = netcheck.evaluate({"country": "RU"}, checks)
        self.assertEqual(r["level"], "limited")
        self.assertIn("zapret", r["advice"])

    def test_abroad_blocked_offers_only_vpn(self):
        checks = [chk("youtube", "timeout")] + ALL_OK[1:]
        r = netcheck.evaluate({"country": "DE"}, checks)
        self.assertEqual(r["advice"], ["vpn"])

    def test_bot_check_is_not_a_ban(self):
        checks = [chk("youtube", "ok"), chk("youtube_speed", "bot_check"), chk("deezer", "ok"), chk("soundcloud", "ok")]
        r = netcheck.evaluate({"country": "DE"}, checks)
        self.assertEqual(r["level"], "limited")
        self.assertEqual(r["advice"], ["cookies", "proxy"])

    def test_deezer_blocked_in_russia(self):
        checks = ALL_OK[:2] + [chk("deezer", "http"), chk("soundcloud", "ok")]
        r = netcheck.evaluate({"country": "RU"}, checks)
        self.assertEqual(r["level"], "blocked")
        self.assertEqual(r["issues"], ["deezer"])

    def test_proxy_that_works_removes_banner(self):
        checks = [chk("youtube", "reset"), chk("youtube_speed", "reset")] + ALL_OK[2:]
        r = netcheck.evaluate({"country": "RU"}, checks, proxy_ok=True)
        self.assertEqual((r["level"], r["advice"]), ("ok", []))


class Probes(unittest.IsolatedAsyncioTestCase):
    async def test_probe_states(self):
        import httpx

        def handler(request):
            path = request.url.path
            if path == "/ok":
                return httpx.Response(204)
            if path == "/forbidden":
                return httpx.Response(403)
            if path == "/boom":
                raise httpx.ConnectError("reset")
            if path == "/slow":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            get = lambda p: netcheck.probe_http(c, "x", "X", "https://h" + p, {200, 204})
            self.assertEqual((await get("/ok"))["state"], "ok")
            self.assertEqual((await get("/forbidden"))["state"], "http")
            self.assertEqual((await get("/boom"))["state"], "reset")
            self.assertEqual((await get("/slow"))["state"], "timeout")
            self.assertEqual((await get("/other"))["state"], "error")

    def test_ytdlp_error_mapping(self):
        f = netcheck._ytdlp_state
        self.assertEqual(f("ERROR: Sign in to confirm you’re not a bot"), "bot_check")
        self.assertEqual(f("The read operation timed out"), "timeout")
        self.assertEqual(f("Unable to download webpage: connection reset"), "reset")
        self.assertEqual(f("Video is not available in your country"), "geo")
        self.assertEqual(f("something else"), "error")


class StoreTests(unittest.TestCase):
    def job(self, jid, album="1", **kw):
        return {"id": jid, "album_id": album, "kind": "album", "album_artist": "A", "album_name": "B",
                "items": [{"nn": 1, "title": "t"}], "done": 0, "failed": 0, "failed_tracks": [],
                "total": 1, **kw}

    def test_queue_survives_restart_and_active_goes_first(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "state.json")
            st.add(self.job("a"))
            st.add(self.job("b", album="2"))
            st.start("a")
            st.active["done_keys"] = ["1|t"]
            st.save_queue()
            again = Store(Path(d) / "state.json")
            self.assertEqual([j["id"] for j in again.pending], ["a", "b"])
            self.assertTrue(again.pending[0]["resumed"])
            self.assertEqual(again.pending[0]["done_keys"], ["1|t"])

    def test_cancelled_active_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "state.json")
            st.add(self.job("a"))
            st.start("a")
            st.active["cancel"] = True
            st.save_queue()
            self.assertEqual(Store(Path(d) / "state.json").pending, [])

    def test_finish_moves_to_history_and_fixed_ids(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "state.json")
            st.add(self.job("a")); st.start("a"); st.finish("error")
            st.add(self.job("b")); st.start("b"); st.finish("done")
            self.assertEqual(st.fixed_ids(), {"a"})
            self.assertEqual(st.owned(), ["1"])
            snap = st.snapshot()
            self.assertEqual(len(snap["history"]), 2)
            self.assertNotIn("items", snap["history"][0])

    def test_snapshot_skips_history_when_client_is_current(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "state.json")
            self.assertNotIn("history", st.snapshot(history=False))



class WebappStyleTests(unittest.TestCase):
    def test_hidden_attribute_wins_over_display(self):
        """Без глобального [hidden]{display:none} бейджи, точки и шторки видны всегда (правила display их перебивают)."""
        css = (Path(__file__).resolve().parent.parent / "webapp" / "style.css").read_text("utf-8")
        compact = "".join(css.split())
        self.assertIn("[hidden]{display:none!important}", compact)

if __name__ == "__main__":
    unittest.main()
