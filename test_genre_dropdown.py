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

from src import database
from src import api


class TestGenreDropdownDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_base_dir = database.BASE_DIR
        self.orig_db_file = database.DB_FILE
        self.orig_cache_conn = database._cache_conn
        self.orig_cache_initialized = database._cache_db_initialized

        database.CONFIG_DIR = Path(self.temp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database._cache_conn = None
        database._cache_db_initialized = False

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
        self.temp_dir.cleanup()

    def test_default_genres_loaded(self):
        movie_genres = database.get_available_genres("movie")
        self.assertIn("Action", movie_genres)
        self.assertIn("Comedy", movie_genres)
        self.assertIn("Drama", movie_genres)
        # Years must NOT be in genres list
        self.assertNotIn("2025", movie_genres)
        self.assertNotIn("2024", movie_genres)

        sort_options = database.get_available_sort_options("movie")
        self.assertEqual(sort_options[0], "Popular")
        self.assertEqual(sort_options[1], "Featured")
        self.assertEqual(sort_options[2], "Recent")
        self.assertIn("2025", sort_options)

        series_genres = database.get_available_genres("series")
        self.assertIn("Reality-TV", series_genres)

        anime_genres = database.get_available_genres("anime")
        self.assertIn("Demons", anime_genres)
        self.assertIn("Mecha", anime_genres)

    def test_save_cached_metadata_extracts_genres_and_year(self):
        listener_called = False
        def on_genre_update():
            nonlocal listener_called
            listener_called = True

        database.register_genre_listener(on_genre_update)

        mock_details = {
            "id": "tt1234567",
            "title": "Cosmic Adventure",
            "genres": ["Sci-Fi", "Animation", "Cyberpunk", "Space Opera", "Hindi"],
            "year": "2028",
            "type": "movie"
        }
        database.save_cached_metadata("tt1234567", "movie", mock_details)

        self.assertTrue(listener_called)

        stored = database.get_stored_genres("movie")
        self.assertIn("Sci-Fi", stored)
        self.assertIn("Animation", stored)
        self.assertIn("Hindi", stored)
        # Non-canonical genres must NOT be stored
        self.assertNotIn("Cyberpunk", stored)
        self.assertNotIn("Space Opera", stored)

        stored_years = database.get_stored_years("movie")
        self.assertIn("2028", stored_years)

        available_genres = database.get_available_genres("movie")
        self.assertIn("Sci-Fi", available_genres)
        self.assertIn("Animation", available_genres)
        self.assertNotIn("Hindi", available_genres)
        self.assertIn("Hindi", database.get_available_languages())
        self.assertNotIn("Cyberpunk", available_genres)
        self.assertNotIn("2028", available_genres)

        sort_options = database.get_available_sort_options("movie")
        self.assertIn("2028", sort_options)

        database.unregister_genre_listener(on_genre_update)

    def test_adult_filtering(self):
        mock_details = {
            "id": "ttadult1",
            "title": "Adult Title",
            "genres": ["Action", "Pornography", "Blowjob"],
            "year": "2024",
            "type": "movie"
        }
        database.save_cached_metadata("ttadult1", "movie", mock_details)

        genres = database.get_available_genres("movie")
        self.assertIn("Action", genres)
        self.assertNotIn("Pornography", genres)
        self.assertNotIn("Blowjob", genres)

    def test_language_genres_normalization(self):
        self.assertEqual(database.normalize_imdb_genre("Hindi"), "Hindi")
        self.assertEqual(database.normalize_imdb_genre("hindi"), "Hindi")
        self.assertEqual(database.normalize_imdb_genre("hi"), "Hindi")
        self.assertEqual(database.normalize_imdb_genre("hin"), "Hindi")
        self.assertEqual(database.normalize_imdb_genre("Tamil"), "Tamil")
        self.assertEqual(database.normalize_imdb_genre("ta"), "Tamil")
        self.assertEqual(database.normalize_imdb_genre("Telugu"), "Telugu")
        self.assertEqual(database.normalize_imdb_genre("Korean"), "Korean")
        self.assertEqual(database.normalize_imdb_genre("Japanese"), "Japanese")
        self.assertEqual(database.normalize_imdb_genre("English"), "English")
        self.assertIsNone(database.normalize_imdb_genre("Cyberpunk"))
        self.assertIsNone(database.normalize_imdb_genre("Blowjob"))

    def test_language_genres_in_catalog_supports_and_filter(self):
        cat_hi = {"id": "hi", "name": "Hindi - IndianRegional", "genres": None}
        cat_ta = {"id": "ta", "name": "Tamil - IndianRegional", "genres": None}
        cat_cinemeta = {"id": "top", "name": "Popular", "genres": ["Action", "Comedy"]}

        self.assertTrue(api.catalog_supports_genre(cat_hi, "Hindi"))
        self.assertFalse(api.catalog_supports_genre(cat_ta, "Hindi"))
        self.assertFalse(api.catalog_supports_genre(cat_cinemeta, "Hindi"))
        self.assertTrue(api.catalog_supports_genre(cat_cinemeta, "Action"))

        items = [
            {"id": "tt1", "title": "Hindi Movie", "genres": ["Hindi", "Crime"]},
            {"id": "tt2", "title": "English Movie", "genres": ["Action", "Sci-Fi"]},
        ]
        filtered = api.filter_items_by_genre(items, "Hindi")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], "tt1")


class TestGenreDropdownWindow(unittest.TestCase):
    def test_sync_library_genre_dropdown_selection(self):
        from src.window import CineWindow
        win = MagicMock()
        win.library_genre_dropdown = Gtk.DropDown.new_from_strings(["All Genres", "Action", "Comedy", "Drama"])
        win._syncing_genre_dropdown = False

        CineWindow._sync_library_genre_dropdown_selection(win, "Comedy")
        selected_idx = win.library_genre_dropdown.get_selected()
        self.assertEqual(win.library_genre_dropdown.get_model().get_string(selected_idx), "Comedy")

        CineWindow._sync_library_genre_dropdown_selection(win, None)
        selected_idx = win.library_genre_dropdown.get_selected()
        self.assertEqual(win.library_genre_dropdown.get_model().get_string(selected_idx), "All Genres")

    def test_on_library_genre_changed(self):
        from src.window import CineWindow
        win = MagicMock()
        win._syncing_genre_dropdown = False
        win.library_stack.get_visible_child_name.return_value = "content"
        win.current_catalog = {"display_name": "Popular Movies"}
        win.current_genre = None

        dropdown = Gtk.DropDown.new_from_strings(["All Genres", "Action", "Comedy"])
        dropdown.set_selected(1)  # "Action"

        CineWindow._on_library_genre_changed(win, dropdown, None)
        self.assertEqual(win.current_genre, "Action")
        win._apply_sort_to_current_catalog.assert_called_once()
        win._refresh_content.assert_called_once()

    def test_update_genre_dropdown_preserves_valid_genre(self):
        from src.window import CineWindow
        win = MagicMock()
        win.library_genre_dropdown = Gtk.DropDown()
        win._syncing_genre_dropdown = False
        win.current_genre = "Action"

        CineWindow._update_genre_dropdown(win, "movie")
        selected_idx = win.library_genre_dropdown.get_selected()
        selected_genre = win.library_genre_dropdown.get_model().get_string(selected_idx)
        self.assertEqual(selected_genre, "Action")
        self.assertEqual(win.current_genre, "Action")

    def test_update_genre_dropdown_resets_invalid_genre(self):
        from src.window import CineWindow
        win = MagicMock()
        win.library_genre_dropdown = Gtk.DropDown()
        win._syncing_genre_dropdown = False
        win.current_genre = "NonExistentGenreXYZ"

        CineWindow._update_genre_dropdown(win, "movie")
        selected_idx = win.library_genre_dropdown.get_selected()
        selected_genre = win.library_genre_dropdown.get_model().get_string(selected_idx)
        self.assertEqual(selected_genre, "All Genres")
        self.assertIsNone(win.current_genre)

    def test_sort_dropdown_sync_and_change(self):
        from src.window import CineWindow
        win = MagicMock()
        win.library_sort_dropdown = Gtk.DropDown.new_from_strings(["Popular", "Featured", "Recent", "2025", "2024"])
        win._syncing_sort_dropdown = False
        win.current_sort = "Popular"
        win.current_year = None
        win.library_stack.get_visible_child_name.return_value = "content"
        win.current_catalog = {"catalog_id": "top", "display_name": "Popular Movies"}

        CineWindow._sync_library_sort_dropdown_selection(win, "Featured")
        selected_idx = win.library_sort_dropdown.get_selected()
        self.assertEqual(win.library_sort_dropdown.get_model().get_string(selected_idx), "Featured")

        # Test selecting a Year "2024"
        dropdown = Gtk.DropDown.new_from_strings(["Popular", "Featured", "Recent", "2025", "2024"])
        dropdown.set_selected(4)  # "2024"
        CineWindow._on_library_sort_changed(win, dropdown, None)
        self.assertEqual(win.current_sort, "2024")
        self.assertEqual(win.current_year, "2024")
        win._apply_sort_to_current_catalog.assert_called_once()
        win._refresh_content.assert_called_once()


class TestStrictImdbGenreFiltering(unittest.TestCase):
    def test_catalog_supports_genre(self):
        # 1. TorrentClaw popular (no extra genre support)
        tc_popular = {"type": "movie", "id": "tc-popular", "extra": [{"name": "skip"}]}
        self.assertFalse(api.catalog_supports_genre(tc_popular, "Animation"))
        self.assertFalse(api.catalog_supports_genre(tc_popular, "Action"))
        # All/None always returns True
        self.assertTrue(api.catalog_supports_genre(tc_popular, None))
        self.assertTrue(api.catalog_supports_genre(tc_popular, "All"))

        # 2. TorrentClaw streaming (restricted options)
        tc_streaming = {
            "type": "movie",
            "id": "tc-streaming-top",
            "extra": [{"name": "genre", "options": ["Netflix", "Prime Video", "Disney+"]}]
        }
        self.assertFalse(api.catalog_supports_genre(tc_streaming, "Animation"))
        self.assertTrue(api.catalog_supports_genre(tc_streaming, "Netflix"))

        # 3. Cinemeta top (supports standard IMDb genres)
        cinemeta_top = {
            "type": "movie",
            "id": "top",
            "genres": ["Action", "Animation", "Comedy", "Crime"],
            "extra": [{"name": "genre", "options": ["Action", "Animation", "Comedy", "Crime"]}]
        }
        self.assertTrue(api.catalog_supports_genre(cinemeta_top, "Animation"))
        self.assertTrue(api.catalog_supports_genre(cinemeta_top, "animation"))
        self.assertFalse(api.catalog_supports_genre(cinemeta_top, "Romance"))

        # 4. Free-form genre extra
        free_form = {"type": "movie", "id": "custom", "extra": [{"name": "genre"}]}
        self.assertTrue(api.catalog_supports_genre(free_form, "Animation"))

    def test_filter_items_by_genre(self):
        items = [
            {
                "id": "tt22084616",
                "title": "Spider-Man: Brand New Day",
                "genres": ["Action", "Adventure"]
            },
            {
                "id": "tt1756855",
                "title": "Coyote vs. Acme",
                "genres": ["Animation", "Adventure", "Comedy"]
            },
            {
                "id": "tt35538033",
                "title": "Resident Evil",
                "genres": ["Action", "Horror"]
            }
        ]

        # Filter strictly by Animation
        anim_items = api.filter_items_by_genre(items, "Animation")
        anim_titles = [m["title"] for m in anim_items]
        self.assertEqual(anim_titles, ["Coyote vs. Acme"])
        self.assertNotIn("Spider-Man: Brand New Day", anim_titles)
        self.assertNotIn("Resident Evil", anim_titles)

        # Filter strictly by Action
        action_items = api.filter_items_by_genre(items, "Action")
        action_titles = [m["title"] for m in action_items]
        self.assertIn("Spider-Man: Brand New Day", action_titles)
        self.assertIn("Resident Evil", action_titles)
        self.assertNotIn("Coyote vs. Acme", action_titles)

    def test_normalize_imdb_genre(self):
        self.assertEqual(database.normalize_imdb_genre("Animation"), "Animation")
        self.assertEqual(database.normalize_imdb_genre("animation"), "Animation")
        self.assertEqual(database.normalize_imdb_genre("Science Fiction"), "Sci-Fi")
        self.assertEqual(database.normalize_imdb_genre("sci fi"), "Sci-Fi")
        self.assertEqual(database.normalize_imdb_genre("Talk"), "Talk-Show")
        self.assertEqual(database.normalize_imdb_genre("Talk-Show"), "Talk-Show")
        # Language genres normalized canonically
        self.assertEqual(database.normalize_imdb_genre("Hindi"), "Hindi")
        self.assertEqual(database.normalize_imdb_genre("Tamil"), "Tamil")
        # Non-canonical tags rejected
        self.assertIsNone(database.normalize_imdb_genre("Multi-Lang"))
        self.assertIsNone(database.normalize_imdb_genre("TV Movie"))
        self.assertIsNone(database.normalize_imdb_genre("Soap"))

    def test_metadata_language_enrichment(self):
        # Non-English title prepends language to genres matching IMDb chips
        res_hindi = {
            "id": "tt27050858",
            "title": "Drishyam: The Conclusion",
            "genres": ["Crime", "Drama", "Mystery"],
            "genre": "Crime, Drama, Mystery"
        }
        with patch("src.tmdb_helper.fetch_title_language_and_genres") as mock_fetch:
            mock_fetch.return_value = {
                "language_code": "hi",
                "language_name": "Hindi",
                "genres": ["Thriller", "Drama", "Mystery", "Crime"]
            }
            enriched = api.enrich_metadata_with_language_and_genres(res_hindi, "tt27050858")
            self.assertEqual(enriched["language"], "Hindi")
            self.assertEqual(enriched["language_code"], "hi")
            self.assertEqual(enriched["genres"][0], "Hindi")
            self.assertIn("Thriller", enriched["genres"])
            self.assertEqual(enriched["genre"], "Hindi, Crime, Drama, Mystery, Thriller")

        # English title retains narrative genres without prepending English
        res_eng = {
            "id": "tt1375666",
            "title": "Inception",
            "genres": ["Action", "Sci-Fi", "Adventure"],
            "genre": "Action, Sci-Fi, Adventure"
        }
        with patch("src.tmdb_helper.fetch_title_language_and_genres") as mock_fetch:
            mock_fetch.return_value = {
                "language_code": "en",
                "language_name": "English",
                "genres": ["Action", "Sci-Fi", "Adventure"]
            }
            enriched = api.enrich_metadata_with_language_and_genres(res_eng, "tt1375666")
            self.assertEqual(enriched["language"], "English")
            self.assertNotIn("English", enriched["genres"])
            self.assertEqual(enriched["genres"], ["Action", "Sci-Fi", "Adventure"])


if __name__ == "__main__":
    unittest.main()
