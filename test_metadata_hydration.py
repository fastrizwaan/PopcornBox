import unittest
import sys
import tempfile
import os
import subprocess
from pathlib import Path
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

import time
from src import database, api
from src.window import CineWindow

class TestMetadataHydration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"

        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

        database.close_cache_db()
        database._json_cache_valid = False
        database._json_cache = None

        with api._HYDRATION_LOCK:
            api._HYDRATION_SEEN.clear()
        while not api._HYDRATION_QUEUE.empty():
            try:
                api._HYDRATION_QUEUE.get_nowait()
            except Exception:
                break

    def tearDown(self):
        database.close_cache_db()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_local_metadata_cache_hydration(self):
        """Catalog items missing genres/year are instantly hydrated from local metadata_cache."""
        # Pre-seed metadata_cache with Dexter
        details = {
            "id": "tt0773262",
            "title": "Dexter",
            "genres": ["Crime", "Drama", "Mystery"],
            "year": "2006",
            "medium_cover_image": "https://example.com/dexter.jpg"
        }
        database.save_cached_metadata("tt0773262", "series", details)

        # Bare catalog items (like NoTorrent sends)
        catalog_items = [
            {
                "id": "tt0773262",
                "title": "Dexter",
                "genres": [],
                "year": "",
                "type": "series"
            }
        ]

        updated = api.hydrate_missing_catalog_items(catalog_items, media_type="series")
        self.assertTrue(updated)
        self.assertEqual(catalog_items[0]["genres"], ["Crime", "Drama", "Mystery"])
        self.assertEqual(catalog_items[0]["year"], "2006")
        self.assertEqual(catalog_items[0]["medium_cover_image"], "https://example.com/dexter.jpg")

    def test_save_cached_catalog_enriches_genres(self):
        """save_cached_catalog looks up metadata_cache and indexes genres into item_genres."""
        # Pre-seed metadata_cache
        details = {
            "id": "tt0101065",
            "title": "Step by Step",
            "genres": ["Comedy", "Family"],
            "year": "1991",
            "type": "series"
        }
        database.save_cached_metadata("tt0101065", "series", details)

        # Now save catalog with bare item
        bare_items = [
            {"id": "tt0101065", "title": "Step by Step", "genres": [], "year": "", "type": "series"}
        ]
        database.save_cached_catalog("test_cat", bare_items)

        # Check item was enriched in memory
        self.assertEqual(bare_items[0]["genres"], ["Comedy", "Family"])
        self.assertEqual(bare_items[0]["year"], "1991")

        # Check item_genres in SQLite was indexed
        stored_items = database.get_cached_items_by_genre("Comedy", "series")
        self.assertTrue(any(it.get("id") == "tt0101065" for it in stored_items))

    def test_queue_metadata_hydration_updates_db(self):
        """queue_metadata_hydration fetches Cinemeta metadata in background and saves to SQLite."""
        mock_cinemeta_res = {
            "meta": {
                "id": "tt9876543",
                "name": "Secret Space Mystery",
                "genres": ["Sci-Fi", "Mystery"],
                "year": "2023",
                "poster": "https://example.com/space.jpg"
            }
        }

        with patch("src.api._get_cached_request", return_value=mock_cinemeta_res):
            bare_item = {"id": "tt9876543", "title": "Secret Space Mystery", "genres": [], "year": "", "type": "movie"}
            queued = api.queue_metadata_hydration("tt9876543", "movie", item_dict=bare_item)
            self.assertTrue(queued)

            # Wait briefly for background hydrator to process (timeout=2s)
            start_t = time.time()
            found = False
            while time.time() - start_t < 2.5:
                meta = database.get_cached_metadata("tt9876543", "movie")
                if meta and meta.get("genres") == ["Sci-Fi", "Mystery"]:
                    found = True
                    break
                time.sleep(0.05)

            self.assertTrue(found)
            self.assertEqual(bare_item.get("genres"), ["Sci-Fi", "Mystery"])
            self.assertEqual(bare_item.get("year"), "2023")

    def test_unique_mode_enables_genre_and_year_dropdowns(self):
        """When unique mode is active, genre and year dropdowns are enabled even for manifest without declared genres."""
        win = MagicMock(spec=CineWindow)
        win.genre_dropdown = Gtk.DropDown()
        win.year_dropdown = Gtk.DropDown()
        win._is_unique_mode_active = MagicMock(return_value=True)

        mock_addon_no_genre = {
            "name": "NoTorrent",
            "manifest_url": "https://addon.notorrent/manifest.json",
            "catalogs": [{"type": "series", "id": "primevideo", "name": "Prime Video"}]
        }

        CineWindow._populate_discover_genre_dropdown(win, mock_addon_no_genre, "series")
        self.assertTrue(win.genre_dropdown.get_sensitive())
        g_model = win.genre_dropdown.get_model()
        self.assertGreater(g_model.get_n_items(), 1)
        self.assertEqual(g_model.get_string(0), "All Genres")

        CineWindow._populate_discover_year_dropdown(win, mock_addon_no_genre, "series")
        self.assertTrue(win.year_dropdown.get_sensitive())
        y_model = win.year_dropdown.get_model()
        self.assertGreater(y_model.get_n_items(), 1)
        self.assertEqual(y_model.get_string(0), "All Years")

    def test_extract_clean_year(self):
        """extract_clean_year properly extracts 4-digit years from various formats."""
        self.assertEqual(database.extract_clean_year("2014"), "2014")
        self.assertEqual(database.extract_clean_year("2019–2026"), "2019")
        self.assertEqual(database.extract_clean_year("2016–"), "2016")
        self.assertEqual(database.extract_clean_year("2013-2023"), "2013")
        self.assertEqual(database.extract_clean_year({"releaseInfo": "2019–2026"}), "2019")
        self.assertEqual(database.extract_clean_year({"released": "2026-06-19T00:00:00.000Z"}), "2026")
        self.assertEqual(database.extract_clean_year(""), "")
        self.assertEqual(database.extract_clean_year(None), "")

    def test_year_range_saved_as_clean_year(self):
        """Saving metadata with releaseInfo or year ranges stores clean 4-digit year in SQLite item_genres."""
        details = {
            "id": "tt11034066",
            "name": "Welcome to Demon School",
            "releaseInfo": "2019–2026",
            "genres": ["Animation", "Comedy"]
        }
        database.save_cached_metadata("tt11034066", "series", details)

        stored_years = database.get_stored_years("series")
        self.assertIn("2019", stored_years)

        # Ensure item is retrievable by year 2019
        by_yr = database.get_cached_items_by_year("2019", "series")
        self.assertTrue(any(it.get("id") == "tt11034066" for it in by_yr))

    def test_tmdb_fallback_when_cinemeta_has_empty_genres(self):
        """When Cinemeta returns empty genres, worker falls back to TMDB helper."""
        mock_cinemeta_res = {
            "meta": {
                "id": "tt38734636",
                "name": "Oasis",
                "genres": [],
                "releaseInfo": "2026"
            }
        }
        mock_tmdb_res = {
            "language_name": "Spanish",
            "genres": ["Crime", "Drama", "Mystery"],
            "release_date": "2026"
        }

        with patch("src.api._get_cached_request", return_value=mock_cinemeta_res), \
             patch("src.tmdb_helper.fetch_title_language_and_genres", return_value=mock_tmdb_res):
            bare_item = {"id": "tt38734636", "title": "Oasis", "genres": [], "year": "", "type": "series"}
            api.queue_metadata_hydration("tt38734636", "series", item_dict=bare_item)

            start_t = time.time()
            found = False
            while time.time() - start_t < 2.5:
                meta = database.get_cached_metadata("tt38734636", "series")
                if meta and meta.get("genres") == ["Crime", "Drama", "Mystery"]:
                    found = True
                    break
                time.sleep(0.05)

            self.assertTrue(found)
            self.assertEqual(bare_item.get("genres"), ["Crime", "Drama", "Mystery"])
            self.assertEqual(bare_item.get("year"), "2026")

    def test_kitsu_alias_id_queueing_and_enrichment(self):
        """Kitsu items with id='kitsu:...' and imdb_id='tt...' are properly queued and enriched."""
        items = [
            {
                "id": "kitsu:7442",
                "imdb_id": "tt2560140",
                "title": "Attack on Titan",
                "genres": [],
                "year": "",
                "type": "anime"
            }
        ]
        mock_cinemeta_res = {
            "meta": {
                "id": "tt2560140",
                "name": "Attack on Titan",
                "genres": ["Animation", "Action", "Adventure"],
                "releaseInfo": "2013-2023"
            }
        }
        while not api._HYDRATION_QUEUE.empty():
            try:
                api._HYDRATION_QUEUE.get_nowait()
            except Exception:
                break

        with patch("src.api._get_cached_request", return_value=mock_cinemeta_res):
            api.hydrate_missing_catalog_items(items, media_type="anime")

            # Wait for background queue
            start_t = time.time()
            found = False
            while time.time() - start_t < 5.0:
                meta = database.get_cached_metadata("tt2560140", "anime")
                if meta and meta.get("genres") and items[0].get("genres"):
                    found = True
                    break
                time.sleep(0.05)

            self.assertTrue(found)
            # Item in memory should have clean year and genres
            self.assertEqual(items[0].get("genres"), ["Animation", "Action", "Adventure"])
            self.assertEqual(items[0].get("year"), "2013")

    def test_cinemeta_name_poster_and_description_hydration(self):
        """Cinemeta metadata with name, poster, description, imdbRating, runtime is enriched onto item_dict."""
        mock_cinemeta_res = {
            "meta": {
                "id": "tt1375666",
                "name": "Inception",
                "genres": ["Action", "Sci-Fi"],
                "year": "2010",
                "poster": "https://example.com/inception.jpg",
                "description": "A thief who steals corporate secrets through the use of dream-sharing technology.",
                "imdbRating": "8.8",
                "runtime": "148 min",
                "cast": ["Leonardo DiCaprio", "Joseph Gordon-Levitt"]
            }
        }
        while not api._HYDRATION_QUEUE.empty():
            try:
                api._HYDRATION_QUEUE.get_nowait()
            except Exception:
                break

        with patch("src.api._get_cached_request", return_value=mock_cinemeta_res):
            bare_item = {
                "id": "tt1375666",
                "title": "Inception",
                "genres": [],
                "year": "",
                "type": "movie"
            }
            api.queue_metadata_hydration("tt1375666", "movie", item_dict=bare_item)

            start_t = time.time()
            found = False
            while time.time() - start_t < 3.0:
                meta = database.get_cached_metadata("tt1375666", "movie")
                if meta and meta.get("description"):
                    found = True
                    break
                time.sleep(0.05)

            self.assertTrue(found)
            self.assertIn("dream-sharing technology", bare_item.get("description", ""))
            self.assertEqual(bare_item.get("imdbRating"), "8.8")
            self.assertEqual(bare_item.get("runtime"), "148 min")
            self.assertEqual(bare_item.get("cast"), ["Leonardo DiCaprio", "Joseph Gordon-Levitt"])

    def test_is_valid_meta_with_name_key(self):
        """is_valid_meta accepts raw metadata where title is specified via 'name'."""
        self.assertTrue(api.is_valid_meta({"name": "The Dark Knight", "description": "Batman fights Joker"}))
        self.assertTrue(api.is_valid_meta({"title": "The Dark Knight", "description": "Batman fights Joker"}))
        self.assertFalse(api.is_valid_meta({"description": "No title or name"}))
        self.assertFalse(api.is_valid_meta({"name": "Error getting meta"}))

    def test_get_cached_metadata_with_candidate_list(self):
        """get_cached_metadata finds metadata when given a list/tuple of candidate IDs."""
        details = {
            "id": "tt0133093",
            "name": "The Matrix",
            "genres": ["Action", "Sci-Fi"],
            "year": "1999",
            "description": "Neo discovers the simulation."
        }
        database.save_cached_metadata("tt0133093", "movie", details)

        res = database.get_cached_metadata(["kitsu:9999", "tt0133093", "tmdb:603"], "movie")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("title"), "The Matrix")
        self.assertEqual(res.get("description"), "Neo discovers the simulation.")

    def test_fetch_items_retains_catalog_description_and_rating(self):
        """fetch_items retains description, rating, runtime, and cast returned in catalog metas."""
        mock_catalog_data = {
            "metas": [
                {
                    "id": "tt22084616",
                    "name": "Spider-Man: Brand New Day",
                    "year": "2026",
                    "poster": "https://example.com/spiderman.jpg",
                    "description": "Peter Parker lives alone as a full-time Spider-Man.",
                    "imdbRating": "8.0",
                    "runtime": "145 min",
                    "cast": ["Tom Holland", "Zendaya"],
                    "genres": ["Action", "Adventure"]
                }
            ]
        }
        with patch("src.api._get_cached_request", return_value=mock_catalog_data):
            items = api.fetch_items(catalog_url="https://v3-cinemeta.strem.io/manifest.json", catalog_id="imdbRating")
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.get("description"), "Peter Parker lives alone as a full-time Spider-Man.")
            self.assertEqual(item.get("imdbRating"), "8.0")
            self.assertEqual(item.get("runtime"), "145 min")
            self.assertEqual(item.get("cast"), ["Tom Holland", "Zendaya"])

            # Verify it was also saved into local metadata_cache
            cached = database.get_cached_metadata("tt22084616")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.get("description"), "Peter Parker lives alone as a full-time Spider-Man.")
            self.assertEqual(cached.get("imdbRating"), "8.0")

    def test_fetch_movie_details_refreshes_incomplete_cached_stub(self):
        """fetch_movie_details does not short-circuit on cached stubs lacking a substantive synopsis."""
        incomplete_stub = {
            "id": "tt22084616",
            "title": "Spider-Man: Brand New Day",
            "year": "2026",
            "genres": ["Action", "Adventure"]
            # Notice: no description, runtime, or cast
        }
        database.save_cached_metadata("tt22084616", "movie", incomplete_stub)

        full_meta = {
            "meta": {
                "id": "tt22084616",
                "name": "Spider-Man: Brand New Day",
                "year": "2026",
                "poster": "https://example.com/spiderman.jpg",
                "description": "Full synopsis from Cinemeta.",
                "imdbRating": "8.0",
                "runtime": "145 min",
                "genres": ["Action", "Adventure"]
            }
        }
        with patch("src.api._get_cached_request", return_value=full_meta):
            det = api.fetch_movie_details("tt22084616", "movie", use_cache=True)
            self.assertIsNotNone(det)
            self.assertEqual(det.get("description"), "Full synopsis from Cinemeta.")
            self.assertEqual(det.get("runtime"), "145 min")

if __name__ == "__main__":
    unittest.main()
