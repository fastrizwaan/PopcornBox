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

from src import database, api
from src.window import CineWindow


class TestCountryFilter(unittest.TestCase):
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

    def tearDown(self):
        database.close_cache_db()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_normalize_country_name(self):
        """Test country name normalization and alias resolution."""
        self.assertEqual(database.normalize_country_name("usa"), "United States")
        self.assertEqual(database.normalize_country_name("US"), "United States")
        self.assertEqual(database.normalize_country_name("United States of America"), "United States")
        self.assertEqual(database.normalize_country_name("uk"), "United Kingdom")
        self.assertEqual(database.normalize_country_name("GB"), "United Kingdom")
        self.assertEqual(database.normalize_country_name("in"), "India")
        self.assertEqual(database.normalize_country_name("korea"), "South Korea")
        self.assertEqual(database.normalize_country_name("KR"), "South Korea")
        self.assertEqual(database.normalize_country_name("jp"), "Japan")
        self.assertEqual(database.normalize_country_name("france"), "France")

    def test_save_cached_metadata_indexes_countries(self):
        """Metadata cache saves and indexes item countries in SQLite item_countries table."""
        details = {
            "id": "tt1234567",
            "title": "Indian Blockbuster",
            "country": "India",
            "language": "Hindi",
            "year": "2023",
            "type": "movie"
        }
        database.save_cached_metadata("tt1234567", "movie", details)

        # Query countries
        countries = database.get_available_countries(media_type="movie")
        self.assertIn("India", countries)

        # Query items by country
        items = database.get_cached_items_by_country("India", media_type="movie")
        self.assertTrue(any(it["id"] == "tt1234567" for it in items))
        found_item = next(it for it in items if it["id"] == "tt1234567")
        self.assertEqual(found_item["country"], "India")

    def test_api_filter_items_by_country(self):
        """filter_items_by_country filters matching items by country."""
        items = [
            {"id": "1", "title": "Movie 1", "country": "India"},
            {"id": "2", "title": "Movie 2", "country": "United States"},
            {"id": "3", "title": "Movie 3", "countries": ["Japan", "France"]},
            {"id": "4", "title": "Movie 4", "country": ""},
        ]
        res_india = api.filter_items_by_country(items, "India")
        self.assertEqual(len(res_india), 1)
        self.assertEqual(res_india[0]["id"], "1")

        res_japan = api.filter_items_by_country(items, "Japan")
        self.assertEqual(len(res_japan), 1)
        self.assertEqual(res_japan[0]["id"], "3")

        res_us = api.filter_items_by_country(items, "United States")
        self.assertEqual(len(res_us), 1)
        self.assertEqual(res_us[0]["id"], "2")

    def test_hydrate_items_from_metadata_country(self):
        """hydrate_items_from_metadata hydrates country from DB."""
        details = {
            "id": "tt9876543",
            "title": "Korean Thriller",
            "country": "South Korea",
            "year": "2022",
            "type": "movie"
        }
        database.save_cached_metadata("tt9876543", "movie", details)

        raw_items = [{"id": "tt9876543", "title": "Korean Thriller", "type": "movie"}]
        database.hydrate_items_from_metadata(raw_items, media_type="movie")
        self.assertEqual(raw_items[0].get("country"), "South Korea")


if __name__ == "__main__":
    unittest.main()
