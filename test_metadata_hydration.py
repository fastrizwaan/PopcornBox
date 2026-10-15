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

if __name__ == "__main__":
    unittest.main()
