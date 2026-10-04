"""Самообновление yt-dlp: PyPI и pip подменены, проверяется логика вокруг них."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401

import deps
import main


def fake_dist(target: Path, version: str) -> None:
    d = target / f"yt_dlp-{version}.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: yt-dlp\nVersion: {version}\n", "utf-8")
    (target / "yt_dlp").mkdir()
    (target / "yt_dlp" / "__init__.py").write_text("", "utf-8")


class DepsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.latest = "2026.10.01"
        self.pypi_calls = 0
        self.installs = []
        self.restarts = 0
        self.pip_code = 0

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, running="2026.09.01", **kw):
        async def latest():
            self.pypi_calls += 1
            if self.latest is None:
                raise OSError("нет сети")
            return self.latest

        async def install(target, reqs):
            self.installs.append(list(reqs))
            if self.pip_code:
                return self.pip_code, "ERROR: No matching distribution"
            fake_dist(target, self.latest)
            return 0, ""

        def restart():
            self.restarts += 1

        return deps.Deps(self.data, restart=restart, latest=latest, install=install,
                         running=lambda: running, enabled=kw.pop("enabled", True))

    def test_version_order(self):
        self.assertGreater(deps.vkey("2026.10.01"), deps.vkey("2026.9.26"))
        self.assertEqual(deps.vkey("2026.09.26"), deps.vkey("2026.9.26"))
        self.assertGreater(deps.vkey("2026.9.26.232845"), deps.vkey("2026.9.26"))
        self.assertEqual(deps.vkey(""), ())

    async def test_up_to_date_does_nothing(self):
        d = self.make(running="2026.10.01")
        info = await d.refresh()
        self.assertEqual((self.installs, info["restart_needed"], info["latest"]), ([], False, "2026.10.01"))
        self.assertFalse(d.restart_if_needed())
        self.assertFalse(deps.deps_dir(self.data).exists())

    async def test_new_version_is_installed_and_needs_restart(self):
        d = self.make()
        info = await d.refresh()
        self.assertEqual(self.installs, [deps.REQUIREMENTS])
        self.assertEqual((info["running"], info["installed"]), ("2026.09.01", "2026.10.01"))
        self.assertTrue(info["restart_needed"] and info["updated_at"] > 0 and not info["error"])
        self.assertTrue((deps.deps_dir(self.data) / "yt_dlp").is_dir())
        self.assertFalse(deps.deps_dir(self.data).with_name("pydeps.new").exists())
        self.assertTrue(d.restart_if_needed())
        self.assertEqual(self.restarts, 1)
        # следующая проверка ничего заново не ставит
        await d.refresh()
        self.assertEqual(len(self.installs), 1)

    async def test_second_update_replaces_the_first(self):
        d = self.make()
        await d.refresh()
        self.latest = "2026.11.05"
        info = await d.refresh()
        self.assertEqual(info["installed"], "2026.11.05")
        left = sorted(p.name for p in deps.deps_dir(self.data).glob("*.dist-info"))
        self.assertEqual(left, ["yt_dlp-2026.11.05.dist-info"])              # старая версия не осталась рядом
        self.assertFalse(deps.deps_dir(self.data).with_name("pydeps.old").exists())

    async def test_failed_install_keeps_the_working_version(self):
        d = self.make()
        await d.refresh()                                                     # стоит 2026.10.01
        self.latest, self.pip_code = "2026.11.05", 1
        info = await d.refresh()
        self.assertEqual(info["installed"], "2026.10.01")
        self.assertIn("не поставилось", info["error"])
        self.assertTrue((deps.deps_dir(self.data) / "yt_dlp").is_dir())

    async def test_pypi_down_is_not_an_error_for_downloads(self):
        self.latest = None
        d = self.make()
        info = await d.refresh()                                              # не бросает
        self.assertIn("PyPI недоступен", info["error"])
        self.assertEqual((self.installs, info["restart_needed"]), ([], False))

    async def test_same_version_written_differently_is_not_an_update(self):
        d = self.make(running="2026.10.01")
        fake_dist(deps.deps_dir(self.data), "2026.10.1")                      # так версию записывает pip
        info = await d.refresh()
        self.assertEqual((info["installed"], info["restart_needed"], self.installs), ("2026.10.01", False, []))

    async def test_throttle_and_disabled(self):
        d = self.make()
        await d.refresh(max_age=600)
        await d.refresh(max_age=600)
        self.assertEqual(self.pypi_calls, 1)                                  # перед каждой загрузкой PyPI не дёргаем
        await d.refresh(max_age=600, force=True)
        self.assertEqual(self.pypi_calls, 2)
        off = self.make(enabled=False)
        await off.refresh()
        self.assertEqual(self.pypi_calls, 2)

    async def test_activate_puts_updates_first_on_path(self):
        import sys
        d = self.make()
        await d.refresh()
        before = list(sys.path)
        try:
            deps.activate(self.data)
            self.assertEqual(sys.path[0], str(deps.deps_dir(self.data)))
        finally:
            sys.path[:] = before

    async def test_job_waits_for_restart_and_stays_in_queue(self):
        hub = main.Hub(self.data / "hub", str(self.data / "music"))
        hub.deps = self.make()
        job = {"id": "j1", "album_id": "1", "kind": "album", "album_artist": "A", "album_name": "B", "items": [],
               "total": 0, "total_raw": 0, "done": 0, "failed": 0, "failed_tracks": []}
        hub.enqueue(job)
        await asyncio.wait_for(hub.worker(), 5)                              # воркер вышел сам: начат перезапуск
        self.assertEqual(self.restarts, 1)
        self.assertIsNone(hub.store.active)
        self.assertEqual([j["id"] for j in hub.store.pending], ["j1"])       # задача не потеряна и не начата
        # после перезапуска работает уже новая версия — задача идёт как обычно
        hub2 = main.Hub(self.data / "hub", str(self.data / "music"))
        hub2.deps = self.make(running="2026.10.01")
        self.assertEqual([j["id"] for j in hub2.store.pending], ["j1"])
        self.assertFalse(await hub2.update_before_job())


if __name__ == "__main__":
    unittest.main()
