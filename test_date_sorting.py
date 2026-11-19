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
from gi.repository import Gtk, Gio

res_file = Path(__file__).resolve().parent / "build-dir/files/share/popcorn-box/popcornbox.gresource"
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src.window import PopcornBoxWindow, _discover_item_release_sort_key
from src import cloudstream, api


class TestDateSorting(unittest.TestCase):
    def setUp(self):
        self.win = MagicMock()
        self.win.current_sort = "Popular"
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_language = None
        self.win.current_country = None
        self.win.current_network = None
        self.win._current_discover_type = "all"
        self.win._current_discover_addon = None
        self.win.unique_flowbox = Gtk.FlowBox()
        self.win.unique_request_id = 1
        self.win._unique_abort_event = None
        self.win.reload_catalog_grid = lambda f=None: PopcornBoxWindow.reload_catalog_grid(self.win, f)
        self.win._is_unique_mode_active = MagicMock(return_value=False)
        self.win._discover_result_search_query = MagicMock(return_value="")
        self.win._cancel_all_background_fetching = MagicMock()
        self.win._refresh_content = MagicMock()
        self.win._refresh_discover_page = MagicMock()
        self.win._refresh_unique_combined_view = MagicMock()

    def test_discover_row_does_not_force_date_sort_for_popular_or_categories(self):
        items = [
            {"id": "tt1", "title": "Old Hit", "release_date": "2010-05-01"},
            {"id": "tt2", "title": "Newer Film", "release_date": "2024-01-01"},
            {"id": "tt3", "title": "Classic", "release_date": "1994-10-14"},
        ]
        row_info = {
            "catalog_id": "top",
            "catalog_name": "Popular Movies",
            "title": "Popular - Movies",
            "_source": None,
        }

        # Simulating the row sort logic in _append_discover_row
        sort_mode = str(getattr(self.win, "current_sort", "") or "").strip().lower()
        row_id = str(row_info.get("catalog_id") or "").lower()
        row_name = str(row_info.get("catalog_name") or row_info.get("title") or "").lower()
        is_date_row = (
            row_info.get("tmdb_sort") == "date"
            or any(k in row_id or k in row_name for k in ("new", "recent", "latest", "last added"))
        )

        row_items = list(items)
        if row_info.get("_source") == "tmdb_catalog" and row_info.get("tmdb_sort") == "date":
            row_items = sorted(row_items, key=lambda item: str(item.get("release_date") or ""), reverse=True)
        elif is_date_row and sort_mode in ("recent", "new", "latest", "last added"):
            row_items = sorted(row_items, key=_discover_item_release_sort_key, reverse=True)

        # Order must be preserved exactly as given by the provider
        self.assertEqual([it["id"] for it in row_items], ["tt1", "tt2", "tt3"])

    def test_discover_row_sorts_by_date_when_date_row(self):
        items = [
            {"id": "tt1", "title": "Old Hit", "release_date": "2010-05-01"},
            {"id": "tt2", "title": "Newer Film", "release_date": "2024-01-01"},
            {"id": "tt3", "title": "Mid Film", "release_date": "2020-08-15"},
        ]
        row_info = {
            "catalog_id": "latest-movies",
            "catalog_name": "Latest Movies",
            "title": "Latest Movies",
            "_source": None,
        }
        self.win.current_sort = "Recent"

        sort_mode = str(getattr(self.win, "current_sort", "") or "").strip().lower()
        row_id = str(row_info.get("catalog_id") or "").lower()
        row_name = str(row_info.get("catalog_name") or row_info.get("title") or "").lower()
        is_date_row = (
            row_info.get("tmdb_sort") == "date"
            or any(k in row_id or k in row_name for k in ("new", "recent", "latest", "last added"))
        )

        row_items = list(items)
        if row_info.get("_source") == "tmdb_catalog" and row_info.get("tmdb_sort") == "date":
            row_items = sorted(row_items, key=lambda item: str(item.get("release_date") or ""), reverse=True)
        elif is_date_row and sort_mode in ("recent", "new", "latest", "last added"):
            row_items = sorted(row_items, key=_discover_item_release_sort_key, reverse=True)

        # Should be ordered newest first
        self.assertEqual([it["id"] for it in row_items], ["tt2", "tt3", "tt1"])

    def test_reload_catalog_grid_updates_current_sort(self):
        # 1. Hindi Movies -> Popular (not date forced)
        filters_hindi = {
            "type": "movie",
            "catalog_id": "hindi-movies",
            "section": "Hindi Movies",
            "addon_url": "cloudstream://catalog/fake",
        }
        self.win.reload_catalog_grid(filters_hindi)
        self.assertEqual(self.win.current_sort, "Popular")

        # 2. English Movies -> Popular (not date forced)
        filters_english = {
            "type": "movie",
            "catalog_id": "english-movies",
            "section": "English Movies",
            "addon_url": "cloudstream://catalog/fake",
        }
        self.win.reload_catalog_grid(filters_english)
        self.assertEqual(self.win.current_sort, "Popular")

        # 3. Latest Movies -> Recent (date sorted)
        filters_latest = {
            "type": "movie",
            "catalog_id": "latest-movies",
            "section": "Latest Movies",
            "addon_url": "cloudstream://catalog/fake",
        }
        self.win.reload_catalog_grid(filters_latest)
        self.assertEqual(self.win.current_sort, "Recent")

        # 4. New / Last Added -> Recent (date sorted)
        filters_new = {
            "type": "movie",
            "catalog_id": "new",
            "section": "New",
            "addon_url": "https://v3-cinemeta.strem.io/manifest.json",
        }
        self.win.reload_catalog_grid(filters_new)
        self.assertEqual(self.win.current_sort, "Recent")

    def test_fourkhdhub_hindi_and_english_sections(self):
        provider = cloudstream.FourKHDHubCloudStreamProvider()
        mock_html = """
        <html>
            <a href="/hindi-film-1/" class="movie-card">
                <img src="/img1.jpg" alt="Hindi Film">
                <div class="card-title">Hindi Film (2024)</div>
            </a>
        </html>
        """
        with patch("src.cloudstream._http_get_text", return_value=mock_html):
            sections = provider.get_main_page(page=1)
            sec_dict = dict(sections)
            self.assertIn("Hindi Movies", sec_dict)
            self.assertIn("English Movies", sec_dict)
            self.assertEqual(len(sec_dict["Hindi Movies"]), 1)
            self.assertEqual(len(sec_dict["English Movies"]), 1)


if __name__ == "__main__":
    unittest.main()
