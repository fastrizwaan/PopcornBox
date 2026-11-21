import os
import sys
import unittest
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

    def test_youtube_custom_scraper(self):
        p_dir = custom_plugins.get_custom_plugins_dir()
        root_plugin = Path(__file__).parent / "plugins" / "youtube_scraper.py"
        self.assertTrue(root_plugin.exists(), "plugins/youtube_scraper.py must exist")

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


if __name__ == "__main__":
    unittest.main()
