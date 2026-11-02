import unittest
import tempfile
import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    import subprocess
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

from src import database
from src import plugins
from src import api


class TestNuvioPlugins(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "test_data.json"
        self.patcher_db = patch.object(database, "DB_FILE", self.db_file)
        self.patcher_db.start()
        database._json_cache = None
        database._json_cache_valid = False

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.patcher_db.stop()
        self.temp_dir.cleanup()

    def test_normalize_manifest_url(self):
        self.assertEqual(
            plugins.normalize_manifest_url("https://github.com/D3adlyRocket/All-in-One-Nuvio"),
            "https://raw.githubusercontent.com/D3adlyRocket/All-in-One-Nuvio/refs/heads/main/manifest.json",
        )
        self.assertEqual(
            plugins.normalize_manifest_url("https://github.com/user/repo/blob/main/manifest.json"),
            "https://raw.githubusercontent.com/user/repo/main/manifest.json",
        )
        self.assertEqual(
            plugins.normalize_manifest_url("https://example.com/repo"),
            "https://example.com/repo/manifest.json",
        )
        self.assertEqual(
            plugins.normalize_manifest_url("https://example.com/manifest.json"),
            "https://example.com/manifest.json",
        )

    def test_js_runtime_detection(self):
        runtime_cmd = plugins.find_js_runtime()
        self.assertIsNotNone(runtime_cmd)
        self.assertTrue(len(runtime_cmd) >= 1)
        # Runner bundle path should exist
        bundle_path = plugins.get_runner_bundle_path()
        self.assertTrue(os.path.exists(bundle_path), f"Bundle not found at {bundle_path}")

    def test_repository_lifecycle_and_db(self):
        manifest_url = "https://example.com/test-repo/manifest.json"
        repo_data = {
            "manifest_url": manifest_url,
            "name": "Test Repo",
            "version": "1.0.0",
            "description": "A test repository",
            "scraper_count": 1,
        }
        scraper_data = [{
            "id": f"{manifest_url}:test-scraper",
            "raw_id": "test-scraper",
            "repository_url": manifest_url,
            "name": "Test Scraper",
            "description": "Scrapes test streams",
            "version": "1.0.0",
            "filename": "test.js",
            "supported_types": ["movie", "tv"],
            "enabled": True,
            "code": "async function getStreams() { return []; }; module.exports = { getStreams };",
        }]

        # Save repository and scraper
        database.save_plugin_repository(repo_data)
        database.save_plugin_scrapers(scraper_data)

        # Verify retrieval
        repos = plugins.get_installed_repositories()
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "Test Repo")

        scrapers = plugins.get_all_scrapers()
        self.assertEqual(len(scrapers), 1)
        self.assertEqual(scrapers[0]["name"], "Test Scraper")
        self.assertTrue(scrapers[0]["enabled"])

        # Test toggle
        plugins.toggle_scraper(scrapers[0]["id"], False)
        scrapers = plugins.get_all_scrapers()
        self.assertFalse(scrapers[0]["enabled"])

        plugins.toggle_scraper(scrapers[0]["id"], True)
        scrapers = plugins.get_all_scrapers()
        self.assertTrue(scrapers[0]["enabled"])

        # Test remove repository
        plugins.remove_repository(manifest_url)
        self.assertEqual(len(plugins.get_installed_repositories()), 0)
        self.assertEqual(len(plugins.get_all_scrapers()), 0)

    def test_execute_scraper_and_stream_formatting(self):
        js_code = """
        async function getStreams(tmdbId, mediaType, season, episode) {
            return [
                {
                    name: "Alpha Provider",
                    title: "The Matrix 1080p Bluray",
                    url: "https://stream.example.com/matrix.mp4",
                    quality: "1080p",
                    size: "2.1 GB",
                    headers: {
                        "Referer": "https://stream.example.com/",
                        "User-Agent": "CustomAgent/1.0"
                    }
                }
            ];
        }
        module.exports = { getStreams };
        """
        scraper = {
            "id": "repo:alpha",
            "raw_id": "alpha",
            "name": "Alpha Provider",
            "repository_url": "https://example.com/repo/manifest.json",
            "supported_types": ["movie"],
            "enabled": True,
            "code": js_code,
        }

        streams = plugins.execute_scraper(scraper, tmdb_id=603, media_type="movie")
        self.assertEqual(len(streams), 1)
        s = streams[0]
        self.assertEqual(s["url"], "https://stream.example.com/matrix.mp4")
        self.assertEqual(s["quality"], "1080p")
        self.assertTrue(s["is_nuvio_plugin"])
        self.assertEqual(s["addon_name"], "Alpha Provider")
        self.assertIn("Referer", s["headers"])
        self.assertEqual(s["headers"]["Referer"], "https://stream.example.com/")
        self.assertIn("proxyHeaders", s["behaviorHints"])

    def test_global_plugin_toggle(self):
        plugins.set_plugins_enabled(True)
        self.assertTrue(plugins.is_plugins_enabled())

        plugins.set_plugins_enabled(False)
        self.assertFalse(plugins.is_plugins_enabled())

        plugins.set_plugins_enabled(True)
        self.assertTrue(plugins.is_plugins_enabled())

    def test_has_stream_addons_with_plugins(self):
        manifest_url = "https://example.com/repo/manifest.json"
        scraper_data = [{
            "id": f"{manifest_url}:beta",
            "raw_id": "beta",
            "repository_url": manifest_url,
            "name": "Beta Provider",
            "supported_types": ["movie"],
            "enabled": True,
            "code": "async function getStreams() { return []; } module.exports = { getStreams };",
        }]
        database.save_plugin_scrapers(scraper_data)

        # Plugins enabled
        plugins.set_plugins_enabled(True)
        self.assertTrue(api.has_stream_addons("movie"))

        # Plugins disabled
        plugins.set_plugins_enabled(False)
        with patch.object(api, "get_stream_addons", return_value=[]):
            self.assertFalse(api.has_stream_addons("movie"))

        plugins.set_plugins_enabled(True)


if __name__ == "__main__":
    unittest.main()
