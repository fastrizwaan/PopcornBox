import unittest
import tempfile
import sqlite3
import json
import os
import sys
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database, api
from src.window import CineWindow


class TestMediaTypeGenreSeparation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_base_dir = database.BASE_DIR
        self.orig_db_file = database.DB_FILE
        self.orig_cache_conn = database._cache_conn
        self.orig_cache_initialized = database._cache_db_initialized

        database.CONFIG_DIR = Path(self.temp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database.reset_json_cache()
        database._cache_conn = None
        database._cache_db_initialized = False
        api.reset_addon_health_cache()

    def tearDown(self):
        if database._cache_conn is not None:
            try:
                database._cache_conn.close()
            except Exception:
                pass
        database._cache_conn = self.orig_cache_conn
        database._cache_db_initialized = self.orig_cache_initialized
        database.CONFIG_DIR = self.orig_config_dir
        database.BASE_DIR = self.orig_base_dir
        database.DB_FILE = self.orig_db_file
        database.reset_json_cache()
        self.temp_dir.cleanup()
        api.reset_addon_health_cache()

    def test_is_type_match_strict_groups(self):
        """Test that movie and series type matching are strictly separated."""
        self.assertTrue(api.is_type_match("movie", "movie"))
        self.assertTrue(api.is_type_match("movies", "movie"))
        self.assertTrue(api.is_type_match("film", "movie"))
        self.assertTrue(api.is_type_match("films", "movie"))

        self.assertTrue(api.is_type_match("series", "series"))
        self.assertTrue(api.is_type_match("tvshow", "series"))
        self.assertTrue(api.is_type_match("tv_series", "series"))
        self.assertTrue(api.is_type_match("tvshows", "series"))

        # Cross checks MUST be False
        self.assertFalse(api.is_type_match("movie", "series"))
        self.assertFalse(api.is_type_match("series", "movie"))
        self.assertFalse(api.is_type_match("tvshow", "movie"))
        self.assertFalse(api.is_type_match("film", "series"))
        self.assertFalse(api.is_type_match("tv", "movie"))
        self.assertFalse(api.is_type_match("tv", "series"))

    def test_is_item_type_compatible(self):
        """Test item-level compatibility against media type."""
        movie_item = {"id": "tt101", "type": "movie", "title": "Titanic"}
        series_item = {"id": "tt102", "type": "series", "title": "Bridgerton"}
        tvshow_item = {"id": "tt103", "type": "tvshow", "title": "Outlander"}

        # Movies mode
        self.assertTrue(api.is_item_type_compatible(movie_item, "movie"))
        self.assertFalse(api.is_item_type_compatible(series_item, "movie"))
        self.assertFalse(api.is_item_type_compatible(tvshow_item, "movie"))

        # Series mode
        self.assertTrue(api.is_item_type_compatible(series_item, "series"))
        self.assertTrue(api.is_item_type_compatible(tvshow_item, "series"))
        self.assertFalse(api.is_item_type_compatible(movie_item, "series"))

        # Item without explicit type field but seasons/episodes
        series_stub = {"id": "tt104", "title": "Show Without Type", "seasons": [1, 2]}
        self.assertFalse(api.is_item_type_compatible(series_stub, "movie"))
        self.assertTrue(api.is_item_type_compatible(series_stub, "series"))

    def test_filter_items_by_genre_enforces_media_type(self):
        """When selecting genre Romance in Movies, no Series are returned, and vice versa."""
        movie_romance = {"id": "tt201", "type": "movie", "title": "Before Sunrise", "genres": ["Romance", "Drama"]}
        series_romance = {"id": "tt202", "type": "series", "title": "Normal People", "genres": ["Romance", "Drama"]}
        movie_action = {"id": "tt203", "type": "movie", "title": "Die Hard", "genres": ["Action"]}
        series_action = {"id": "tt204", "type": "series", "title": "24", "genres": ["Action"]}

        items = [movie_romance, series_romance, movie_action, series_action]

        # 1. Filter Movies by Romance -> ONLY movie_romance
        res_movie_romance = api.filter_items_by_genre(items, "Romance", media_type="movie")
        self.assertEqual(res_movie_romance, [movie_romance])

        # 2. Filter Series by Romance -> ONLY series_romance
        res_series_romance = api.filter_items_by_genre(items, "Romance", media_type="series")
        self.assertEqual(res_series_romance, [series_romance])

        # 3. Filter Movies with All genres -> Only movies
        res_movie_all = api.filter_items_by_genre(items, "All", media_type="movie")
        self.assertEqual(res_movie_all, [movie_romance, movie_action])

        # 4. Filter Series with All genres -> Only series
        res_series_all = api.filter_items_by_genre(items, "All", media_type="series")
        self.assertEqual(res_series_all, [series_romance, series_action])

    def test_get_available_catalogs_excludes_cross_type_catalogs(self):
        """Apple TV movies (atv in id) should NOT be in series catalogs, and series catalogs should NOT be in movies."""
        fake_addon = {
            "name": "Streaming Catalogs Plus",
            "manifest_url": "https://fake.addon/manifest.json",
            "catalogs": [
                {
                    "type": "movie",
                    "id": "provider_atv_popular_cv20",
                    "name": "Apple TV+ Popular Movies",
                    "extra": [{"name": "genre", "options": ["Romance", "Action"]}]
                },
                {
                    "type": "series",
                    "id": "provider_atv_popular_series",
                    "name": "Apple TV+ Popular Series",
                    "extra": [{"name": "genre", "options": ["Romance", "Drama"]}]
                },
                {
                    "type": "series",
                    "id": "hub_movies_and_series",
                    "name": "Movies & Series Hub - Series",
                    "extra": [{"name": "genre", "options": ["Romance"]}]
                }
            ],
            "resources": ["catalog"],
            "types": ["movie", "series"]
        }
        database.add_addon(fake_addon)
        api.invalidate_catalogs_cache()

        # Movie catalogs must only have movie type
        movie_cats = api.get_available_catalogs("movie")
        movie_cat_ids = [c["catalog_id"] for c in movie_cats]
        self.assertIn("provider_atv_popular_cv20", movie_cat_ids)
        self.assertNotIn("provider_atv_popular_series", movie_cat_ids)
        self.assertNotIn("hub_movies_and_series", movie_cat_ids)

        # Series catalogs must only have series type (NOT provider_atv_popular_cv20!)
        series_cats = api.get_available_catalogs("series")
        series_cat_ids = [c["catalog_id"] for c in series_cats]
        self.assertNotIn("provider_atv_popular_cv20", series_cat_ids)
        self.assertIn("provider_atv_popular_series", series_cat_ids)
        self.assertIn("hub_movies_and_series", series_cat_ids)

    def test_window_unique_item_filtering_by_media_type(self):
        """Window _is_unique_item rejects series when in movie unique mode and vice versa."""
        win = CineWindow.__new__(CineWindow)
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()

        # In Movies unique mode
        win._current_unique_media_type = "movie"
        movie_item = {"id": "tt301", "type": "movie", "title": "La La Land"}
        series_item = {"id": "tt302", "type": "series", "title": "Modern Love"}

        self.assertTrue(win._is_unique_item(movie_item))
        self.assertFalse(win._is_unique_item(series_item))

        # Reset seen sets
        win.unique_seen_ids.clear()
        win.unique_seen_keys.clear()

        # In Series unique mode
        win._current_unique_media_type = "series"
        self.assertFalse(win._is_unique_item(movie_item))
        self.assertTrue(win._is_unique_item(series_item))


if __name__ == "__main__":
    unittest.main()
