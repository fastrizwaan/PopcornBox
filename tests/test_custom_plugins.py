import os
import sys
import unittest
import unittest.mock
import tempfile
import shutil
from pathlib import Path

# Ensure PopcornBox is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import custom_plugins, database, api


class TestCustomPlugins(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for plugins
        self.test_dir = tempfile.mkdtemp(prefix="popcorn_test_plugins_")
        self.orig_base_dir = getattr(database, "BASE_DIR", None)
        database.BASE_DIR = Path(self.test_dir)
        # Reset memory state
        custom_plugins._LOADED_SCRAPERS.clear()
        custom_plugins._LOADED_MODULES.clear()
        custom_plugins.set_custom_plugins_enabled(True)
        database.set_setting("disabled_custom_scrapers", [])
        database.set_setting("disabled_custom_scraper_capabilities", {})

    def tearDown(self):
        database.BASE_DIR = self.orig_base_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_empty_plugins_dir(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        self.assertTrue(p_dir.exists())
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        self.assertEqual(len(scrapers), 0)
        all_info = custom_plugins.get_all_custom_scrapers()
        self.assertEqual(len(all_info), 0)

    def test_load_valid_scraper(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        plugin_code = '''
class Scraper:
    id = "custom.dummy"
    name = "Dummy Scraper"
    version = "1.0.0"
    media_types = ["movie"]

    def get_streams(self, imdb_id, media_type="movie", **kwargs):
        return [
            {
                "name": "Dummy 1080p",
                "title": "1080p | 👤 10 | 1 GB",
                "infoHash": "0123456789abcdef0123456789abcdef01234567",
                "quality": "1080p"
            }
        ]
'''
        (p_dir / "dummy_scraper.py").write_text(plugin_code, encoding="utf-8")

        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        self.assertEqual(len(scrapers), 1)
        scraper = scrapers[0]
        self.assertEqual(scraper.id, "custom.dummy")
        self.assertEqual(scraper.name, "Dummy Scraper")

        # Test execute_custom_scraper
        streams = custom_plugins.execute_custom_scraper(scraper, "tt1234567", "movie")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["infoHash"], "0123456789abcdef0123456789abcdef01234567")
        self.assertTrue(streams[0]["is_custom_plugin"])
        self.assertEqual(streams[0]["custom_plugin_id"], "custom.dummy")

    def test_toggle_and_filter_scrapers(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        movie_code = '''
class Scraper:
    id = "custom.movie"
    name = "Movie Scraper"
    media_types = ["movie"]
    def get_streams(self, *a, **k): return []
'''
        series_code = '''
class Scraper:
    id = "custom.series"
    name = "Series Scraper"
    media_types = ["series"]
    def get_streams(self, *a, **k): return []
'''
        (p_dir / "movie.py").write_text(movie_code, encoding="utf-8")
        (p_dir / "series.py").write_text(series_code, encoding="utf-8")

        custom_plugins.load_all_custom_scrapers(force_reload=True)

        movie_scrapers = custom_plugins.get_enabled_scrapers_for_type("movie")
        self.assertEqual(len(movie_scrapers), 1)
        self.assertEqual(movie_scrapers[0].id, "custom.movie")

        series_scrapers = custom_plugins.get_enabled_scrapers_for_type("series")
        self.assertEqual(len(series_scrapers), 1)
        self.assertEqual(series_scrapers[0].id, "custom.series")

        # Disable movie scraper
        custom_plugins.toggle_scraper("custom.movie", False)
        self.assertFalse(custom_plugins.is_scraper_enabled("custom.movie"))
        self.assertEqual(len(custom_plugins.get_enabled_scrapers_for_type("movie")), 0)

        # Re-enable
        custom_plugins.toggle_scraper("custom.movie", True)
        self.assertTrue(custom_plugins.is_scraper_enabled("custom.movie"))
        self.assertEqual(len(custom_plugins.get_enabled_scrapers_for_type("movie")), 1)

    def test_stream_and_catalog_capabilities(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        both_code = '''
class Scraper:
    id = "custom.both"
    name = "Both"
    media_types = ["movie"]
    catalog_sections = [{"id": "latest", "name": "Latest", "type": "movie"}]
    def get_streams(self, *a, **k): return []
    def get_catalog_items(self, *a, **k): return []
'''
        stream_only_code = '''
class Scraper:
    id = "custom.streamonly"
    name = "Stream Only"
    media_types = ["movie"]
    capabilities = ["stream"]
    catalog_sections = [{"id": "latest", "name": "Latest", "type": "movie"}]
    def get_streams(self, *a, **k): return []
    def get_catalog_items(self, *a, **k): return []
'''
        (p_dir / "both.py").write_text(both_code, encoding="utf-8")
        (p_dir / "streamonly.py").write_text(stream_only_code, encoding="utf-8")
        custom_plugins.load_all_custom_scrapers(force_reload=True)

        info = {s["id"]: s for s in custom_plugins.get_all_custom_scrapers()}
        self.assertEqual(info["custom.both"]["capabilities"], ["stream", "catalog"])
        self.assertEqual(info["custom.streamonly"]["capabilities"], ["stream"])

        def catalog_ids():
            return {c["custom_plugin_id"] for c in custom_plugins.get_catalogs("all")}

        def stream_ids():
            return {s.id for s in custom_plugins.get_enabled_scrapers_for_type("movie")}

        self.assertEqual(catalog_ids(), {"custom.both"})
        self.assertEqual(stream_ids(), {"custom.both", "custom.streamonly"})

        custom_plugins.toggle_capability("custom.both", "catalog", False)
        self.assertEqual(catalog_ids(), set())
        self.assertIn("custom.both", stream_ids())

        custom_plugins.toggle_capability("custom.both", "catalog", True)
        custom_plugins.toggle_capability("custom.both", "stream", False)
        self.assertEqual(stream_ids(), {"custom.streamonly"})
        self.assertEqual(catalog_ids(), {"custom.both"})

        custom_plugins.toggle_capability("custom.both", "stream", True)
        self.assertEqual(catalog_ids(), {"custom.both"})
        self.assertEqual(stream_ids(), {"custom.both", "custom.streamonly"})

    def test_disabling_every_capability_disables_plugin(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        (p_dir / "both.py").write_text('''
class Scraper:
    id = "custom.both"
    media_types = ["movie"]
    catalog_sections = [{"id": "latest", "name": "Latest", "type": "movie"}]
    def get_streams(self, *a, **k): return []
    def get_catalog_items(self, *a, **k): return []
''', encoding="utf-8")
        (p_dir / "streamonly.py").write_text('''
class Scraper:
    id = "custom.streamonly"
    media_types = ["movie"]
    capabilities = ["stream"]
    def get_streams(self, *a, **k): return []
''', encoding="utf-8")
        custom_plugins.load_all_custom_scrapers(force_reload=True)

        self.assertTrue(custom_plugins.toggle_capability("custom.both", "stream", False))
        self.assertFalse(custom_plugins.toggle_capability("custom.both", "catalog", False))
        self.assertFalse(custom_plugins.is_scraper_enabled("custom.both"))

        # Re-enabling the plugin brings both roles back.
        custom_plugins.toggle_scraper("custom.both", True)
        self.assertTrue(custom_plugins.is_capability_enabled("custom.both", "stream"))
        self.assertTrue(custom_plugins.is_capability_enabled("custom.both", "catalog"))

        # A single-capability plugin is disabled as soon as that one role goes off.
        self.assertFalse(custom_plugins.toggle_capability("custom.streamonly", "stream", False))
        self.assertFalse(custom_plugins.is_scraper_enabled("custom.streamonly"))
        custom_plugins.toggle_scraper("custom.streamonly", True)
        self.assertEqual(
            {s.id for s in custom_plugins.get_enabled_scrapers_for_type("movie")},
            {"custom.both", "custom.streamonly"},
        )

    def test_load_listener_called_after_background_load(self):
        """UI listeners hear about background loads so catalog lists built too early get rebuilt."""
        import threading
        p_dir = custom_plugins.get_custom_plugins_dir()
        (p_dir / "cat.py").write_text('''
class Scraper:
    id = "custom.cat"
    catalog_sections = [{"id": "latest", "name": "Latest", "type": "movie"}]
    def get_streams(self, *a, **k): return []
    def get_catalog_items(self, *a, **k): return []
''', encoding="utf-8")
        seen = []
        done = threading.Event()
        def listener(scrapers):
            seen.append([s.id for s in scrapers]); done.set()
        custom_plugins.add_load_listener(listener)
        try:
            custom_plugins.reload_custom_scrapers_async(force=True)
            self.assertTrue(done.wait(20))
            self.assertIn("custom.cat", seen[-1])
        finally:
            custom_plugins._LOAD_LISTENERS.remove(listener)

    def test_plugins_cannot_import_sibling_helpers(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        (p_dir / "_helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        (p_dir / "needs_helper.py").write_text('''
import _helper
class Scraper:
    id = "custom.needs_helper"
    def get_streams(self, *a, **k): return []
''', encoding="utf-8")
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        self.assertNotIn("custom.needs_helper", [s.id for s in scrapers])

    def test_global_disable(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        movie_code = '''
class Scraper:
    id = "custom.movie"
    name = "Movie Scraper"
    media_types = ["movie"]
    def get_streams(self, *a, **k): return []
'''
        (p_dir / "movie.py").write_text(movie_code, encoding="utf-8")
        custom_plugins.load_all_custom_scrapers(force_reload=True)

        self.assertEqual(len(custom_plugins.get_enabled_scrapers_for_type("movie")), 1)
        custom_plugins.set_custom_plugins_enabled(False)
        self.assertFalse(custom_plugins.is_custom_plugins_enabled())
        self.assertEqual(len(custom_plugins.get_enabled_scrapers_for_type("movie")), 0)

    def test_broken_plugin_does_not_crash(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        (p_dir / "broken.py").write_text("def syntax_error(:", encoding="utf-8")
        (p_dir / "good.py").write_text('''
class Scraper:
    id = "custom.good"
    name = "Good"
    media_types = ["movie"]
    def get_streams(self, *a, **k): return []
''', encoding="utf-8")

        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        self.assertEqual(len(scrapers), 1)
        self.assertEqual(scrapers[0].id, "custom.good")

    def test_import_and_delete_plugin(self):
        outside_file = Path(self.test_dir) / "test_import.py"
        outside_file.write_text('''
class Scraper:
    id = "custom.imported"
    name = "Imported"
    media_types = ["movie"]
    def get_streams(self, *a, **k): return []
''', encoding="utf-8")

        ok, msg = custom_plugins.import_plugin_file(str(outside_file))
        self.assertTrue(ok)
        self.assertIn("imported", msg.lower())

        scrapers = custom_plugins.load_all_custom_scrapers()
        self.assertTrue(any(s.id == "custom.imported" for s in scrapers))

        # Delete
        ok_del, msg_del = custom_plugins.delete_plugin_file("test_import.py")
        self.assertTrue(ok_del)
        scrapers_after = custom_plugins.load_all_custom_scrapers(force_reload=True)
        self.assertFalse(any(s.id == "custom.imported" for s in scrapers_after))

    def test_api_process_raw_streams_integration(self):
        raw = [
            {
                "name": "Custom 1080p",
                "title": "1080p | 👤 100 | 2.5 GB",
                "infoHash": "aabbccddeeff00112233445566778899aabbccdd",
                "is_custom_plugin": True,
                "custom_plugin_id": "custom.yts",
                "addon_name": "YTS (Popcorn)",
            }
        ]
        processed = api.process_raw_streams(raw)
        self.assertEqual(len(processed), 1)
        entry = processed[0]
        self.assertEqual(entry["hash"], "aabbccddeeff00112233445566778899aabbccdd")
        self.assertEqual(entry["addon_name"], "YTS (Popcorn)")
        self.assertTrue(entry["is_custom_plugin"])
        self.assertEqual(entry["custom_plugin_id"], "custom.yts")


    def test_catalog_support(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        catalog_code = '''
class Scraper:
    id = "custom.cat"
    name = "Catalog Test"
    media_types = ["movie", "series"]
    catalog_sections = [
        {"id": "trending", "name": "Trending", "type": "movie", "genres": ["Action", "Sci-Fi"]},
        {"id": "shows_popular", "name": "Popular Series", "type": "series", "genres": ["Drama"]}
    ]

    def get_catalog_items(self, catalog_id, media_type="movie", genre=None, page=1, query=None):
        return [
            {
                "id": "tt9999999",
                "title": f"Item in {catalog_id} (genre={genre})",
                "year": "2024",
                "medium_cover_image": "https://example.com/poster.jpg",
                "type": media_type
            }
        ]
'''
        (p_dir / "catalog_plugin.py").write_text(catalog_code, encoding="utf-8")
        custom_plugins.load_all_custom_scrapers(force_reload=True)

        # Test get_catalogs
        movie_cats = custom_plugins.get_catalogs("movie")
        self.assertEqual(len(movie_cats), 1)
        self.assertEqual(movie_cats[0]["catalog_name"], "Trending")
        self.assertEqual(movie_cats[0]["genres"], ["Action", "Sci-Fi"])
        self.assertEqual(movie_cats[0]["manifest_url"], "custom://catalog/custom.cat")

        series_cats = custom_plugins.get_catalogs("series")
        self.assertEqual(len(series_cats), 1)
        self.assertEqual(series_cats[0]["catalog_name"], "Popular Series")

        # Test fetch_catalog_items
        items = custom_plugins.fetch_catalog_items("custom.cat", "trending", genre="Action", page=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "tt9999999")
        self.assertIn("Action", items[0]["title"])

        # Test through api.fetch_items
        api_items = api.fetch_items(catalog_url="custom://catalog/custom.cat", catalog_id="trending", genre="Sci-Fi", page=1)
        self.assertEqual(len(api_items), 1)
        self.assertEqual(api_items[0]["id"], "tt9999999")
        self.assertIn("Sci-Fi", api_items[0]["title"])

    def test_subprocess_isolation_exit_handling(self):
        """A scraper calling sys.exit() must not kill the main application process."""
        p_dir = custom_plugins.get_custom_plugins_dir()
        exit_code = '''
import sys
class Scraper:
    id = "custom.exit"
    name = "Exit Scraper"
    media_types = ["movie"]
    def get_streams(self, *a, **k):
        sys.exit(42)
'''
        (p_dir / "exit_scraper.py").write_text(exit_code, encoding="utf-8")
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        exit_scraper = next((s for s in scrapers if s.id == "custom.exit"), None)
        self.assertIsNotNone(exit_scraper)

        streams = custom_plugins.execute_custom_scraper(exit_scraper, "tt1234567", "movie")
        self.assertEqual(streams, [])

    def test_subprocess_isolation_timeout_handling(self):
        """A scraper hanging in an infinite loop must be terminated without hanging the main process."""
        p_dir = custom_plugins.get_custom_plugins_dir()
        hang_code = '''
import time
class Scraper:
    id = "custom.hang"
    name = "Hang Scraper"
    media_types = ["movie"]
    def get_streams(self, *a, **k):
        time.sleep(10)
        return []
'''
        (p_dir / "hang_scraper.py").write_text(hang_code, encoding="utf-8")
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        hang_scraper = next((s for s in scrapers if s.id == "custom.hang"), None)
        self.assertIsNotNone(hang_scraper)

        # Call with a very small timeout (0.5s)
        streams = custom_plugins.execute_custom_scraper(hang_scraper, "tt1234567", "movie", timeout=0.5)
        self.assertEqual(streams, [])

    def test_top_level_crash_isolation(self):
        """A plugin that calls sys.exit() or crashes on import must not crash PopcornBox."""
        p_dir = custom_plugins.get_custom_plugins_dir()
        crash_code = '''
import sys
sys.exit(99)
'''
        (p_dir / "crash_on_import.py").write_text(crash_code, encoding="utf-8")
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        # Verify the crash file was not loaded and PopcornBox is still alive
        self.assertFalse(any(s.id == "custom.crash_on_import" for s in scrapers))

    def test_youtube_custom_scraper(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        root_plugin = Path(__file__).parent / "plugins" / "youtube_scraper.py"
        if not root_plugin.exists():
            root_plugin = Path.home() / "plugins" / "youtube_scraper.py"
        if not root_plugin.exists():
            import subprocess
            try:
                res = subprocess.run(["git", "show", "HEAD:plugins/youtube_scraper.py"], capture_output=True, text=True, cwd=str(Path(__file__).parent))
                if res.returncode == 0 and res.stdout:
                    (p_dir / "youtube_scraper.py").write_text(res.stdout, encoding="utf-8")
                else:
                    self.skipTest("plugins/youtube_scraper.py not found on disk")
                    return
            except Exception:
                self.skipTest("plugins/youtube_scraper.py not found on disk")
                return
        else:
            shutil.copy2(root_plugin, p_dir / "youtube_scraper.py")
        scrapers = custom_plugins.load_all_custom_scrapers(force_reload=True)
        yt_scrapers = [s for s in scrapers if s.id == "custom.youtube"]
        self.assertEqual(len(yt_scrapers), 1)
        yt = yt_scrapers[0]
        self.assertEqual(yt.name, "YouTube")
        self.assertIn("movie", yt.media_types)
        self.assertIn("series", yt.media_types)

        # Test classic catalog
        classic_items = yt.get_catalog_items(catalog_id="classic_movies", media_type="movie")
        self.assertGreater(len(classic_items), 0)
        self.assertTrue(any("night of the living dead" in it["title"].lower() for it in classic_items))

        # Test movie stream parsing / scoring
        movie_streams = custom_plugins.execute_custom_scraper(
            yt,
            imdb_id="tt0063350",
            media_type="movie",
            title="Night of the Living Dead",
            year="1968"
        )
        self.assertGreater(len(movie_streams), 0)
        top_stream = movie_streams[0]
        self.assertTrue(top_stream["url"].startswith("https://www.youtube.com/watch?v="))
        self.assertTrue(top_stream["is_custom_plugin"])
        self.assertEqual(top_stream["custom_plugin_id"], "custom.youtube")
        self.assertEqual(top_stream["addon_name"], "YouTube")

        # Test series stream parsing / scoring
        series_streams = custom_plugins.execute_custom_scraper(
            yt,
            imdb_id="tt0096657",
            media_type="series",
            season=1,
            episode=1,
            title="Mr. Bean"
        )
        self.assertGreater(len(series_streams), 0)
        self.assertTrue(series_streams[0]["url"].startswith("https://www.youtube.com/watch?v="))

        # Test strict title matching: fuzzy results like 'Campus beats' or 'MrBeast' must be rejected for 'betas'
        self.assertFalse(yt._is_title_match("betas", "Campus Beats Season 1 Episode 1"))
        self.assertFalse(yt._is_title_match("betas", "Beast Games | Episode 1 (MrBeast)"))
        self.assertFalse(yt._is_title_match("betas", "Workout Video Day 1 | Zumba Fitness With Unique Beats"))
        self.assertFalse(yt._is_title_match("betas", "Alpha Betas I FULL MOVIE"))
        self.assertTrue(yt._is_title_match("betas", "Betas Season 1 Episode 1"))
        self.assertTrue(yt._is_title_match("betas", "The Betas S01E01"))

        # Verify series stream extraction for 'betas' never returns 'Campus beats' or 'MrBeast'
        betas_streams = yt.get_streams(title="betas", media_type="series", season=1, episode=1)
        for s in betas_streams:
            s_title = s.get("title", "").lower()
            self.assertNotIn("campus beats", s_title)
            self.assertNotIn("mrbeast", s_title)
            self.assertNotIn("beast games", s_title)

        # Verify review/critique variants are strictly rejected even for long videos (> 3600s)
        mock_series_videos = [
            {"id": "rev1", "title": "Betas Season 1 Episode 1 Review", "duration": 4200, "badges": []},
            {"id": "rev2", "title": "Betas Season 1 Episode 1 - Reviews and Breakdown", "duration": 5000, "badges": []},
            {"id": "rev3", "title": "Reviewing Betas Season 1 Episode 1", "duration": 3600, "badges": []},
            {"id": "rev4", "title": "Betas Episode 1: Reviewer Discussion", "duration": 4000, "badges": []},
            {"id": "rev5", "title": "Betas Season 1 Episode 1 Critique and Talk", "duration": 3700, "badges": []},
            {"id": "rev6", "title": "Betas S01E01 Reaction & Discussion", "duration": 4500, "badges": []},
            {"id": "valid1", "title": "Betas Season 1 Episode 1 Full Episode", "duration": 1800, "badges": ["HD"]},
        ]
        with unittest.mock.patch.object(yt, "_query_videos", return_value=mock_series_videos):
            filtered_series = yt.get_streams(title="Betas", media_type="series", season=1, episode=1)
            self.assertEqual(len(filtered_series), 1)
            self.assertEqual(filtered_series[0]["url"], "https://www.youtube.com/watch?v=valid1")

        mock_movie_videos = [
            {"id": "mrev1", "title": "Betas The Movie Review", "duration": 5400, "badges": []},
            {"id": "mrev2", "title": "Betas 2013 - Reviews and In-depth Critique", "duration": 7200, "badges": []},
            {"id": "mrev3", "title": "Reviewing Betas 2013 Full Movie", "duration": 6000, "badges": []},
            {"id": "mrev4", "title": "Betas 2013 Movie Reviewer Discussion & Talk", "duration": 4500, "badges": []},
            {"id": "mvalid", "title": "Betas 2013 Full Movie", "duration": 5400, "badges": ["HD"]},
        ]
        with unittest.mock.patch.object(yt, "_query_videos", return_value=mock_movie_videos):
            filtered_movie = yt.get_streams(title="Betas", media_type="movie", year="2013")
            self.assertEqual(len(filtered_movie), 1)
            self.assertEqual(filtered_movie[0]["url"], "https://www.youtube.com/watch?v=mvalid")

        # Verify movies legitimately titled with 'Review' are preserved
        mock_legit_review_movie = [
            {"id": "rev_legit", "title": "The Review 2020 Full Movie", "duration": 5400, "badges": ["HD"]},
        ]
        with unittest.mock.patch.object(yt, "_query_videos", return_value=mock_legit_review_movie):
            legit_movie = yt.get_streams(title="The Review", media_type="movie", year="2020")
            self.assertEqual(len(legit_movie), 1)
            self.assertEqual(legit_movie[0]["url"], "https://www.youtube.com/watch?v=rev_legit")




class TestCustomPluginLoaderThread(unittest.TestCase):
    def test_reloads_run_on_one_background_thread_and_coalesce(self):
        import threading
        import time

        seen = []
        started = threading.Event()
        release = threading.Event()

        def fake_load(force_reload=False):
            seen.append((threading.current_thread().name, force_reload))
            started.set()
            release.wait(5)
            return ["scraper"]

        callbacks = []
        with unittest.mock.patch.object(custom_plugins, "load_all_custom_scrapers", side_effect=fake_load):
            custom_plugins.reload_custom_scrapers_async(force=False, callback=lambda s: callbacks.append(("a", s)))
            self.assertTrue(started.wait(5))
            # Two requests while the first pass runs: merged into one forced pass.
            custom_plugins.reload_custom_scrapers_async(force=True, callback=lambda s: callbacks.append(("b", s)))
            custom_plugins.reload_custom_scrapers_async(force=False, callback=lambda s: callbacks.append(("c", s)))
            release.set()
            deadline = time.time() + 5
            while custom_plugins._LOADER_THREAD is not None and time.time() < deadline:
                time.sleep(0.01)

        self.assertIsNone(custom_plugins._LOADER_THREAD)
        self.assertEqual(seen, [("CustomPluginsLoader", False), ("CustomPluginsLoader", True)])
        self.assertEqual(sorted(c[0] for c in callbacks), ["a", "b", "c"])
        self.assertFalse(custom_plugins._PRELOAD_INFLIGHT)


def _zip_plugin(plugin_id, name):
    return f'''
class Scraper:
    id = "{plugin_id}"
    name = "{name}"
    version = "1.0.0"
    media_types = ["movie"]

    def get_streams(self, imdb_id, media_type="movie", **kwargs):
        return []
'''


class TestCustomPluginZipImport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="popcorn_test_plugin_zip_")
        self.orig_base_dir = getattr(database, "BASE_DIR", None)
        database.BASE_DIR = Path(self.test_dir) / "base"
        custom_plugins._LOADED_SCRAPERS.clear()
        custom_plugins._LOADED_MODULES.clear()
        custom_plugins.set_custom_plugins_enabled(True)
        database.set_setting("disabled_custom_scrapers", [])
        database.set_setting("disabled_custom_scraper_capabilities", {})

    def tearDown(self):
        database.BASE_DIR = self.orig_base_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_zip(self, members):
        import zipfile
        path = Path(self.test_dir) / "plugins.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return str(path)

    def test_imports_every_plugin_in_zip(self):
        zpath = self._make_zip({
            "PopcornBox-plugins/bollyflix.py": _zip_plugin("custom.bollyflix", "BollyFlix"),
            "PopcornBox-plugins/yts.py": _zip_plugin("custom.yts", "YTS"),
            "PopcornBox-plugins/README.txt": "readme",
            "PopcornBox-plugins/_helper.py": "x = 1",
            "__MACOSX/PopcornBox-plugins/._yts.py": "junk",
            "PopcornBox-plugins/broken.py": "<html>",
        })
        ok, msg = custom_plugins.import_plugin_file(zpath)
        self.assertTrue(ok, msg)
        self.assertIn("2 plugins", msg)
        self.assertIn("1 skipped", msg)
        p_dir = custom_plugins.get_custom_plugins_dir()
        self.assertEqual(sorted(p.name for p in p_dir.glob("*.py")), ["bollyflix.py", "yts.py"])
        self.assertNotEqual((p_dir / "README.txt").read_text(), "readme")
        ids = sorted(s["id"] for s in custom_plugins.get_all_custom_scrapers())
        self.assertEqual(ids, ["custom.bollyflix", "custom.yts"])

    def test_renamed_plugin_replaces_old_file(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        (p_dir / "bollyflix_scraper.py").write_text(_zip_plugin("custom.bollyflix", "Bollyflix"), encoding="utf-8")
        (p_dir / "mine.py").write_text(_zip_plugin("custom.mine", "Mine"), encoding="utf-8")
        custom_plugins.load_all_custom_scrapers(force_reload=True)

        zpath = self._make_zip({"bollyflix.py": _zip_plugin("custom.bollyflix", "BollyFlix")})
        ok, msg = custom_plugins.import_plugin_file(zpath)
        self.assertTrue(ok, msg)
        self.assertEqual(sorted(p.name for p in p_dir.glob("*.py")), ["bollyflix.py", "mine.py"])
        names = {s["id"]: s["name"] for s in custom_plugins.get_all_custom_scrapers()}
        self.assertEqual(names["custom.bollyflix"], "BollyFlix")

    def test_zip_without_plugins_fails(self):
        zpath = self._make_zip({"README.txt": "nothing here"})
        ok, msg = custom_plugins.import_plugin_file(zpath)
        self.assertFalse(ok)
        self.assertIn("No plugins", msg)

    def test_rejects_other_file_types(self):
        other = Path(self.test_dir) / "notes.txt"
        other.write_text("hi")
        ok, _msg = custom_plugins.import_plugin_file(str(other))
        self.assertFalse(ok)


class TestCustomPluginUrlImport(TestCustomPluginZipImport):
    """Install from https:// / file:// links, served by a local HTTP server."""

    def setUp(self):
        super().setUp()
        import http.server
        import threading as _threading
        self.www = Path(self.test_dir) / "www"
        self.www.mkdir()
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(self.www), **kw)
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        _threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _installed(self):
        return sorted(p.name for p in custom_plugins.get_custom_plugins_dir().glob("*.py"))

    def test_zip_over_http(self):
        import zipfile
        with zipfile.ZipFile(self.www / "pack.zip", "w") as zf:
            zf.writestr("pack/yts.py", _zip_plugin("custom.yts", "YTS"))
            zf.writestr("pack/torrentio.py", _zip_plugin("custom.torrentio", "Torrentio"))
        ok, msg = custom_plugins.import_plugin_url(f"{self.base}/pack.zip")
        self.assertTrue(ok, msg)
        self.assertEqual(self._installed(), ["torrentio.py", "yts.py"])

    def test_py_over_http(self):
        (self.www / "yts.py").write_text(_zip_plugin("custom.yts", "YTS"), encoding="utf-8")
        ok, msg = custom_plugins.import_plugin_url(f"{self.base}/yts.py")
        self.assertTrue(ok, msg)
        self.assertEqual(self._installed(), ["yts.py"])

    def test_zip_without_extension_is_detected(self):
        import zipfile
        with zipfile.ZipFile(self.www / "download", "w") as zf:
            zf.writestr("yts.py", _zip_plugin("custom.yts", "YTS"))
        ok, msg = custom_plugins.import_plugin_url(f"{self.base}/download")
        self.assertTrue(ok, msg)
        self.assertEqual(self._installed(), ["yts.py"])

    def test_html_page_is_rejected(self):
        (self.www / "page").write_text("<html><body>Not found</body></html>")
        ok, msg = custom_plugins.import_plugin_url(f"{self.base}/page")
        self.assertFalse(ok)
        self.assertEqual(self._installed(), [])

    def test_missing_url_fails_cleanly(self):
        ok, msg = custom_plugins.import_plugin_url(f"{self.base}/nope.zip")
        self.assertFalse(ok)
        self.assertIn("Download failed", msg)

    def test_file_url_and_plain_path(self):
        src = Path(self.test_dir) / "local.py"
        src.write_text(_zip_plugin("custom.local", "Local"), encoding="utf-8")
        ok, msg = custom_plugins.import_plugin_url(src.as_uri())
        self.assertTrue(ok, msg)
        self.assertEqual(self._installed(), ["local.py"])
        ok, msg = custom_plugins.import_plugin_url(str(src))
        self.assertTrue(ok, msg)

    def test_other_schemes_rejected(self):
        ok, _msg = custom_plugins.import_plugin_url("ftp://example.com/p.zip")
        self.assertFalse(ok)

    def test_github_blob_link_becomes_raw(self):
        self.assertEqual(
            custom_plugins._github_raw_url("https://github.com/u/r/blob/main/pack.zip"),
            "https://raw.githubusercontent.com/u/r/main/pack.zip",
        )


if __name__ == "__main__":
    unittest.main()
