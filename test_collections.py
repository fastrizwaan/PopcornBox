import unittest
import tempfile
import os
import sys
import sqlite3
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
sys.modules["mpv"] = MagicMock()

import subprocess

class TestCollections(unittest.TestCase):
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
                Gio.resources_register(res)
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"
        self.cache_db = self.db_dir / "cache.db"
        
        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()
        
        from src import database
        database._json_cache_valid = False
        database._json_cache = None
        database._cache_conn = None
        database._cache_db_initialized = False

    def tearDown(self):
        from src import database
        if database._cache_conn:
            try:
                database._cache_conn.close()
            except Exception:
                pass
            database._cache_conn = None
            database._cache_db_initialized = False
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_resolve_to_imdb_id_preserves_collection(self):
        from src.tmdb_helper import resolve_to_imdb_id
        # ctmdb collection IDs must never be resolved to a single movie IMDb ID
        res = resolve_to_imdb_id("ctmdb.4246", "collections", "Scary Movie Collection")
        self.assertEqual(res, "ctmdb.4246")

        res_movie = resolve_to_imdb_id("ctmdb.4246", "movie", "Scary Movie Collection")
        self.assertEqual(res_movie, "ctmdb.4246")

        # List containing collection ID
        res_list = resolve_to_imdb_id(["ctmdb.4246"], "collections", "Scary Movie Collection")
        self.assertEqual(res_list, ["ctmdb.4246"])

    def test_is_valid_meta_collection(self):
        from src.api import is_valid_meta
        # Valid collection with videos
        valid_col = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "videos": [{"id": "tt0175142", "title": "Scary Movie"}]
        }
        self.assertTrue(is_valid_meta(valid_col))

        # Invalid collection without videos
        invalid_col = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "videos": []
        }
        self.assertFalse(is_valid_meta(invalid_col))

        # Invalid dummy fallback item
        dummy_item = {
            "id": "ctmdb.531241",
            "title": "Media Item",
            "description": "Synopsis temporarily unavailable.",
            "videos": []
        }
        self.assertFalse(is_valid_meta(dummy_item))

    def test_get_cached_metadata_invalidates_empty_collection(self):
        from src import database
        # Save a bad collection to cache directly in sqlite
        conn = database._get_cache_db()
        c = conn.cursor()
        bad_data = {
            "id": "ctmdb.531241",
            "title": "Media Item",
            "description": "Synopsis temporarily unavailable.",
            "videos": []
        }
        c.execute(
            "INSERT OR REPLACE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
            ("ctmdb.531241", "movie", json.dumps(bad_data), 12345)
        )
        conn.commit()

        # get_cached_metadata must return None for empty collection
        res = database.get_cached_metadata("ctmdb.531241")
        self.assertIsNone(res)

        # Save a valid collection
        good_data = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "videos": [{"id": "tt0175142", "title": "Scary Movie"}]
        }
        c.execute(
            "INSERT OR REPLACE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
            ("ctmdb.4246", "collections", json.dumps(good_data), 12345)
        )
        conn.commit()

        good_res = database.get_cached_metadata("ctmdb.4246")
        self.assertIsNotNone(good_res)
        self.assertEqual(good_res["title"], "Scary Movie Collection")

    @patch("src.api._get_cached_request")
    @patch("src.database.get_addons")
    def test_fetch_movie_details_preserves_collection(self, mock_get_addons, mock_get_cached):
        from src import api
        mock_get_addons.return_value = [
            {
                "id": "org.stremio.tmdbcollections",
                "name": "TMDB Collections",
                "manifest_url": "https://tmdb-collections.club/manifest.json",
                "enabled": True,
                "resources": [{"name": "meta", "types": ["movie"], "idPrefixes": ["ctmdb."]}],
                "types": ["movie", "collections"],
                "idPrefixes": ["ctmdb."]
            }
        ]

        def fake_get_cached(url, **kwargs):
            if "manifest.json" in url:
                return {
                    "resources": [{"name": "meta", "types": ["movie"], "idPrefixes": ["ctmdb."]}],
                    "types": ["movie", "collections"],
                    "idPrefixes": ["ctmdb."]
                }
            if "meta/movie/ctmdb.4246.json" in url:
                return {
                    "meta": {
                        "id": "ctmdb.4246",
                        "name": "Scary Movie Collection",
                        "type": "movie",
                        "videos": [
                            {"id": "tt0175142", "title": "Scary Movie", "season": 1, "episode": 1},
                            {"id": "tt0257106", "title": "Scary Movie 2", "season": 1, "episode": 2}
                        ]
                    }
                }
            return None

        mock_get_cached.side_effect = fake_get_cached

        # When calling fetch_movie_details with ctmdb.4246
        details = api.fetch_movie_details("ctmdb.4246", "collections", "Scary Movie Collection", use_cache=False)
        self.assertIsNotNone(details)
        self.assertEqual(details.get("id"), "ctmdb.4246")
        self.assertEqual(details.get("type"), "collections")
        self.assertEqual(len(details.get("videos", [])), 2)
        self.assertEqual(details["videos"][0]["id"], "tt0175142")
        self.assertEqual(details["videos"][1]["id"], "tt0257106")

    @patch("src.api._get_cached_request")
    @patch("src.database.get_addons")
    def test_fetch_movie_details_list_with_collection_id(self, mock_get_addons, mock_get_cached):
        from src import api
        mock_get_addons.return_value = [
            {
                "id": "org.stremio.tmdbcollections",
                "name": "TMDB Collections",
                "manifest_url": "https://tmdb-collections.club/manifest.json",
                "enabled": True,
                "resources": [{"name": "meta", "types": ["movie"], "idPrefixes": ["ctmdb."]}],
                "types": ["movie", "collections"],
                "idPrefixes": ["ctmdb."]
            }
        ]

        def fake_get_cached(url, **kwargs):
            if "meta/movie/ctmdb.4246.json" in url:
                return {
                    "meta": {
                        "id": "ctmdb.4246",
                        "name": "Scary Movie Collection",
                        "videos": [{"id": "tt0175142", "title": "Scary Movie"}]
                    }
                }
            return None

        # Even if list has alias IDs, ctmdb. should be preferred
        details = api.fetch_movie_details(["ctmdb.4246", "tt32093575"], "collections", "Scary Movie Collection", use_cache=False)
        self.assertIsNotNone(details)
        self.assertEqual(details.get("id"), "ctmdb.4246")
        self.assertEqual(details.get("type"), "collections")

    def test_download_playback_isolation_and_navigation(self):
        from src.window import CineWindow
        win = MagicMock()
        win.details_box = MagicMock()
        # Pretend a collection details page is alive in details_box
        mock_page = MagicMock()
        mock_page.movie_details = {"id": "ctmdb.4246", "title": "Scary Movie Collection"}
        mock_page.movie_stub = {"id": "ctmdb.4246"}
        mock_page.media_type = "collections"
        win.details_box.get_first_child.return_value = mock_page
        win.main_stack = MagicMock()
        win.mpv = MagicMock()
        win.mpv.time_pos = 10.0
        win.mpv.duration = 100.0
        win._current_playing_item = None
        win.previous_page_before_player = "details"

        # Now simulate playing a download
        CineWindow._play_stream(
            win,
            url="http://127.0.0.1:8888/download_file.mkv",
            title="Some Downloaded Movie",
            item_id="tt9999999",
            media_type="movie",
            is_download=True
        )

        # 1. _current_playing_item must use the download item_id, NOT ctmdb.4246
        self.assertIsNotNone(win._current_playing_item)
        self.assertEqual(win._current_playing_item["id"], "tt9999999")
        self.assertTrue(win._current_playing_item["is_download"])
        self.assertEqual(win.previous_page_before_player, "downloads")
        self.assertEqual(win.main_stack.set_visible_child_name.call_args[0][0], "player")

        # 2. Now close the player
        with patch("src.player.stop_player"):
            CineWindow._close_player(win)
            # Must navigate back to 'downloads', NOT 'details'
            win.main_stack.set_visible_child_name.assert_called_with("downloads")
            # _current_playing_item must be cleared
            self.assertIsNone(win._current_playing_item)

if __name__ == "__main__":
    unittest.main()
