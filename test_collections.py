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
        database.reset_json_cache()

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
        win.nav_stack = []
        win._restore_nav_entry = lambda entry: CineWindow._restore_nav_entry(win, entry)
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

    def test_fetch_collection_details_structure(self):
        from src.tmdb_helper import fetch_collection_details
        mock_tmdb_col = {
            "id": 726871,
            "name": "Dune Collection",
            "overview": "The Dune Collection chronicles a mythic journey...",
            "poster_path": "/lxIGYkpvYjLtYtZH684AQft0FhD.jpg",
            "backdrop_path": "/lzWHmYZrWnDK7tc1Elsa58x7pmb.jpg",
            "parts": [
                {
                    "id": 438631,
                    "title": "Dune",
                    "overview": "Paul Atreides leads nomadic tribes...",
                    "release_date": "2021-09-15",
                    "vote_average": 7.8,
                    "genre_ids": [878, 12]
                },
                {
                    "id": 693134,
                    "title": "Dune: Part Two",
                    "overview": "Follow the mythic journey...",
                    "release_date": "2024-02-27",
                    "vote_average": 8.2,
                    "genre_ids": [878, 12]
                }
            ]
        }
        with patch("src.tmdb_helper._get_cached_request", return_value=mock_tmdb_col):
            with patch("src.tmdb_helper.resolve_to_imdb_id", side_effect=lambda mid, mtype, title: "tt1160419" if "438631" in mid else "tt15239678"):
                col = fetch_collection_details("ctmdb.726871")
                self.assertIsNotNone(col)
                self.assertEqual(col["title"], "Dune Collection")
                self.assertEqual(col["imdbRating"], "8.0")
                self.assertEqual(len(col["videos"]), 2)
                self.assertEqual(col["videos"][0]["id"], "tt1160419")
                self.assertEqual(col["videos"][0]["title"], "Dune")
                self.assertEqual(col["videos"][0]["episode"], 1)
                self.assertEqual(col["videos"][1]["id"], "tt15239678")
                self.assertEqual(col["videos"][1]["title"], "Dune: Part Two")
                self.assertEqual(col["videos"][1]["episode"], 2)
                self.assertIn("Science Fiction", col["genres"])

    def test_fetch_movie_details_collection_fallback(self):
        from src import api
        # When addon returns empty meta ({"meta": []}), fetch_movie_details must fall back to fetch_collection_details
        mock_collection = {
            "id": "ctmdb.726871",
            "title": "Dune Collection",
            "year": "2021",
            "medium_cover_image": "https://image.tmdb.org/t/p/w500/dune.jpg",
            "background": "",
            "description": "Dune collection overview",
            "runtime": "",
            "genre": "Science Fiction",
            "imdbRating": "8.0",
            "trailer": None,
            "videos": [
                {"id": "tt1160419", "season": 1, "episode": 1, "title": "Dune", "overview": "", "released": "2021-09-15", "thumbnail": ""},
                {"id": "tt15239678", "season": 1, "episode": 2, "title": "Dune: Part Two", "overview": "", "released": "2024-02-27", "thumbnail": ""}
            ],
            "cast": [],
            "genres": ["Science Fiction"],
            "type": "collections",
            "is_collection": True
        }
        with patch("src.tmdb_helper.fetch_collection_details", return_value=mock_collection):
            with patch("src.api._get_cached_request", return_value={"meta": []}):
                details = api.fetch_movie_details("ctmdb.726871", media_type="collections", use_cache=False)
                self.assertIsNotNone(details)
                self.assertEqual(details["title"], "Dune Collection")
                self.assertEqual(len(details["videos"]), 2)
                self.assertEqual(details["videos"][0]["id"], "tt1160419")

    def test_collection_item_click_opens_movie_details(self):
        from src.window import MovieDetailsPage
        win = MagicMock()
        col_stub = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "type": "collections",
            "is_collection": True
        }
        with patch.object(MovieDetailsPage, "load_details_async"):
            page = MovieDetailsPage(col_stub, on_back=lambda: None, window=win)
            self.assertTrue(page.is_collection)

            ep1 = {
                "id": "tt0175142",
                "title": "Scary Movie",
                "season": 1,
                "episode": 1,
                "released": "2000-07-07",
                "overview": "[IMDB: 6.3⭐] A familiar-looking masked killer stalks...",
                "thumbnail": "https://image.tmdb.org/t/p/w500/scary.jpg"
            }
            page.current_episodes = [ep1]
            page.render_episodes_list()

            row_btn = page.episodes_list_box.get_first_child()
            self.assertIsNotNone(row_btn)

            row_btn.emit("clicked")

            win._on_movie_clicked.assert_called_once()
            clicked_movie = win._on_movie_clicked.call_args[0][0]
            self.assertEqual(clicked_movie["id"], "tt0175142")
            self.assertEqual(clicked_movie["title"], "Scary Movie")
            self.assertEqual(clicked_movie["type"], "movie")
            self.assertEqual(clicked_movie["year"], "2000")
            self.assertEqual(clicked_movie["imdbRating"], "6.3")
            self.assertEqual(clicked_movie["medium_cover_image"], "https://image.tmdb.org/t/p/w500/scary.jpg")
            self.assertIn("A familiar-looking masked killer", clicked_movie["description"])

    def test_series_item_click_does_not_open_movie_details(self):
        from src.window import MovieDetailsPage
        win = MagicMock()
        series_stub = {
            "id": "tt0944947",
            "title": "Game of Thrones",
            "type": "series"
        }
        with patch.object(MovieDetailsPage, "load_details_async"):
            page = MovieDetailsPage(series_stub, on_back=lambda: None, window=win)
            self.assertFalse(page.is_collection)

            ep1 = {
                "id": "tt1480055",
                "title": "Winter Is Coming",
                "season": 1,
                "episode": 1,
                "released": "2011-04-17",
                "overview": "Jon Arryn is dead...",
                "thumbnail": "https://image.tmdb.org/t/p/w500/got.jpg"
            }
            page.current_episodes = [ep1]
            page.render_episodes_list()

            row_btn = page.episodes_list_box.get_first_child()
            self.assertIsNotNone(row_btn)

            with patch.object(page, "fetch_torrents_async"):
                row_btn.emit("clicked")

            win._on_movie_clicked.assert_not_called()
            self.assertEqual(page.sidebar_stack.get_visible_child_name(), "streams")

    def test_collection_navigation_stack_and_back_restore(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        win.main_stack.get_visible_child_name.return_value = "details"
        win.details_box = MagicMock()
        win.library_stack = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "content"
        win.category_btn_stack = MagicMock()
        win.category_btn_stack.get_visible_child_name.return_value = "movies"
        win.nav_stack = []

        col_stub = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "type": "collections"
        }
        mock_col_page = MagicMock()
        mock_col_page.movie_stub = col_stub
        mock_col_page.movie_details = {
            "id": "ctmdb.4246",
            "title": "Scary Movie Collection",
            "videos": [{"id": "tt0175142", "title": "Scary Movie"}]
        }
        mock_col_page.media_type = "collections"
        win.details_box.get_first_child.return_value = mock_col_page

        # 1. Push current nav state from collection page
        CineWindow._push_current_nav_state(win)
        self.assertEqual(len(win.nav_stack), 1)
        entry = win.nav_stack[0]
        self.assertEqual(entry["main_page"], "details")
        self.assertEqual(entry["movie_data"]["id"], "ctmdb.4246")
        self.assertEqual(entry["movie_data"]["type"], "collections")
        self.assertIn("videos", entry["movie_data"])

        # 2. Simulate restoring navigation entry
        win._on_movie_clicked = MagicMock()
        win._is_same_movie = lambda a, b: False
        win.details_box.get_first_child.return_value = None
        CineWindow._restore_nav_entry(win, entry)

        win._on_movie_clicked.assert_called_once_with(entry["movie_data"], push_history=False)

    def test_is_valid_meta_rejects_incomplete_movie_stub(self):
        from src.api import is_valid_meta
        # Stub from poster fallback or minimal catalog card
        bare_stub = {
            "id": "tt1757678",
            "title": "Avatar: Fire and Ash",
            "year": "2025",
            "medium_cover_image": "https://image.tmdb.org/t/p/w500/avatar3.jpg"
        }
        self.assertFalse(is_valid_meta(bare_stub))

        # Stub with generic placeholder description
        stub_with_placeholder = {
            "id": "tt1757678",
            "title": "Avatar: Fire and Ash",
            "description": "No synopsis available."
        }
        self.assertFalse(is_valid_meta(stub_with_placeholder))

    def test_is_valid_meta_accepts_complete_movie(self):
        from src.api import is_valid_meta
        # Real synopsis
        meta_with_desc = {
            "id": "tt1757678",
            "title": "Avatar: Fire and Ash",
            "description": "Jake and Neytiri family..."
        }
        self.assertTrue(is_valid_meta(meta_with_desc))

        # Runtime
        meta_with_runtime = {
            "id": "tt1757678",
            "title": "Avatar: Fire and Ash",
            "runtime": "198 min"
        }
        self.assertTrue(is_valid_meta(meta_with_runtime))

        # Genres
        meta_with_genres = {
            "id": "tt1757678",
            "title": "Avatar: Fire and Ash",
            "genres": ["Action", "Adventure"]
        }
        self.assertTrue(is_valid_meta(meta_with_genres))

    def test_fetch_movie_details_bypasses_cached_incomplete_stub(self):
        from src import database, api
        # Save an incomplete stub in cache
        stub_data = {
            "id": "tt9999999",
            "title": "Incomplete Stub Movie",
            "year": "2025",
            "medium_cover_image": "https://example.com/poster.jpg"
        }
        database.save_cached_metadata("tt9999999", "movie", stub_data)

        # Mock fetch_addon_meta/cinemeta returning rich data
        full_meta = {
            "id": "tt9999999",
            "title": "Incomplete Stub Movie",
            "year": "2025",
            "medium_cover_image": "https://example.com/poster.jpg",
            "description": "Full rich synopsis fetched from addon.",
            "runtime": "120 min",
            "genre": "Sci-Fi",
            "videos": []
        }

        with patch("src.api._get_cached_request") as mock_req:
            mock_req.return_value = {
                "meta": {
                    "id": "tt9999999",
                    "name": "Incomplete Stub Movie",
                    "description": "Full rich synopsis fetched from addon.",
                    "runtime": "120 min",
                    "genres": ["Sci-Fi"]
                }
            }
            res = api.fetch_movie_details("tt9999999", media_type="movie", use_cache=True)
            self.assertEqual(res.get("description"), "Full rich synopsis fetched from addon.")
            self.assertEqual(res.get("runtime"), "120 min")

    def test_movie_details_page_forces_refresh_on_incomplete_stub(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
        from src import database
        from src.window import MovieDetailsPage

        stub_data = {
            "id": "tt8888888",
            "title": "Avatar 3 Stub",
            "year": "2025",
            "medium_cover_image": "https://example.com/poster.jpg"
        }
        database.save_cached_metadata("tt8888888", "movie", stub_data)

        movie_stub = {
            "id": "tt8888888",
            "title": "Avatar 3 Stub",
            "description": "Overview from collection item"
        }

        with patch.object(MovieDetailsPage, "load_details_async") as mock_load, \
             patch.object(MovieDetailsPage, "fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, lambda: None, window=None)
            # Must call load_details_async with force_refresh=True because cached metadata is incomplete stub
            mock_load.assert_called_once_with(force_refresh=True)
            # Initial UI was populated with movie_stub overview
            self.assertEqual(page.desc_label.get_text(), "Overview from collection item")

if __name__ == "__main__":
    unittest.main()
