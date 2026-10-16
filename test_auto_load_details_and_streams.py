import unittest
import sys
from unittest.mock import MagicMock, patch
import time

# Mock mpv before importing GTK / window
sys.modules["mpv"] = MagicMock()

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

import tempfile
import os
import subprocess
from pathlib import Path

class TestAutoLoadDetailsAndStreams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        from gi.repository import Gio
        res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
        if res_file.exists():
            try:
                res = Gio.Resource.load(str(res_file))
                res._register()
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()
    def test_get_cached_request_uncollapsed_none_max_age_hours(self):
        """Verify _get_cached_request_uncollapsed does not raise TypeError when max_age_hours is None."""
        from src import api
        with api._MEM_CACHE_LOCK:
            api._MEM_CACHE["https://test-url-none-age.strem.io"] = ({"meta": {"name": "Test"}}, time.time())
        try:
            res = api._get_cached_request_uncollapsed("https://test-url-none-age.strem.io", max_age_hours=None)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("meta", {}).get("name"), "Test")
        finally:
            with api._MEM_CACHE_LOCK:
                api._MEM_CACHE.pop("https://test-url-none-age.strem.io", None)

    def test_get_torrents_streamed_preserves_cache_on_empty_addons(self):
        """Verify that when stream addons return empty or fail, existing cached streams are preserved."""
        from src import api, database
        cache_key = api.get_stream_cache_key("tt1234567", "series", 1, 1)
        cached_streams = [{"name": "Working Stream", "title": "720p", "infoHash": "abc12345"}]
        database.save_cached_streams(cache_key, cached_streams)

        try:
            mock_addon = {
                "name": "TestAddon",
                "manifest_url": "https://test.addon/manifest.json",
                "resources": ["stream"],
                "types": ["series"],
                "idPrefixes": ["tt"]
            }
            with patch("src.database.get_addons", return_value=[mock_addon]), \
                 patch("src.api._get_cached_request", return_value={"streams": []}):
                delivered = []
                res = api.get_torrents_streamed(
                    "tt1234567",
                    "series",
                    1,
                    1,
                    callback=lambda t, is_cached=False, is_complete=False: delivered.append((t, is_cached, is_complete)),
                    force_refresh=True
                )
                self.assertTrue(len(res) > 0)
                self.assertEqual(res[0]["name"], "Working Stream")
                re_read = database.get_cached_streams(cache_key)
                self.assertIsNotNone(re_read)
                self.assertEqual(len(re_read), 1)
        finally:
            database.delete_cached_streams(cache_key)

    def test_movie_details_page_auto_refreshes_metadata_and_streams(self):
        """Verify MovieDetailsPage always starts background force_refresh."""
        from src.window import MovieDetailsPage

        movie_stub = {
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series"
        }

        with patch("src.window.MovieDetailsPage.load_details_async") as mock_load, \
             patch("src.window.MovieDetailsPage.fetch_torrents_async") as mock_fetch:
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            mock_load.assert_called_with(force_refresh=True)

    def test_series_without_videos_does_not_fetch_invalid_streams(self):
        """Verify that a series without videos does not invoke stream addons with None, None."""
        from src.window import MovieDetailsPage
        movie_stub = {
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series"
        }
        with patch("src.api.get_torrents_streamed") as mock_get_streams, \
             patch("src.window.MovieDetailsPage.load_details_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            page.fetch_torrents_async(force=False)
            mock_get_streams.assert_not_called()

    def test_series_with_videos_fetches_active_episode_streams(self):
        """Verify that when videos are present, active episode streams are fetched."""
        from src.window import MovieDetailsPage
        movie_stub = {
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series",
            "videos": [
                {"id": "tt0106179:1:1", "season": 1, "episode": 1, "title": "Pilot"},
                {"id": "tt0106179:1:2", "season": 1, "episode": 2, "title": "Deep Throat"},
            ]
        }
        with patch("src.api.get_torrents_streamed") as mock_get_streams, \
             patch("src.window.MovieDetailsPage.load_details_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertEqual(page.selected_season, 1)
            self.assertEqual(page.selected_episode, 1)
            mock_get_streams.assert_called()
            args, kwargs = mock_get_streams.call_args
            self.assertEqual(args[1], "series")
            self.assertEqual(args[2], 1)
            self.assertEqual(args[3], 1)

if __name__ == "__main__":
    unittest.main()
