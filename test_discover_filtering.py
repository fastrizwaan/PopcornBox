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

res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass
else:
    try:
        tmppath = Path(schema_temp_dir.name)
        for ui_name in ["preferences.ui", "window.ui", "history.ui", "playlist.ui", "options.ui", "shortcuts-dialog.ui"]:
            (tmppath / ui_name).write_text("<interface></interface>")
        (tmppath / "style.css").write_text("")
        file_tags = "\n".join(f"    <file>{f}</file>" for f in ["preferences.ui", "window.ui", "history.ui", "playlist.ui", "options.ui", "shortcuts-dialog.ui", "style.css"])
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<gresources>
  <gresource prefix="/io/github/fastrizwaan/PopcornBox">
{file_tags}
  </gresource>
</gresources>"""
        (tmppath / "res.xml").write_text(xml_content)
        subprocess.run(["glib-compile-resources", f"--sourcedir={schema_temp_dir.name}", str(tmppath / "res.xml"), f"--target={schema_temp_dir.name}/test.gresource"], check=True)
        res = Gio.Resource.load(f"{schema_temp_dir.name}/test.gresource")
        Gio.resources_register(res)
    except Exception:
        pass

from src.window import PopcornBoxWindow
from src import api, database, tmdb_helper


class TestDiscoverFiltering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_base_dir = database.BASE_DIR
        self.orig_db_file = database.DB_FILE

        database.CONFIG_DIR = Path(self.temp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database.reset_json_cache()

        self.win = MagicMock()
        self.win.type_dropdown = Gtk.DropDown()
        self.win.catalog_dropdown = Gtk.DropDown()
        self.win.section_dropdown = Gtk.DropDown()
        self.win.genre_dropdown = Gtk.DropDown()
        self.win.year_dropdown = Gtk.DropDown()
        self.win.language_dropdown = Gtk.DropDown()
        self.win.country_dropdown = Gtk.DropDown()
        self.win.network_dropdown = Gtk.DropDown()
        self.win.current_sort = "Popular"
        self.win._syncing_discover_toolbar = False

        self.win._is_unique_mode_active = lambda: False
        self.win._is_audio_or_live_tv_discover_context = lambda a, ct, ac=None: False
        self.win._catalog_supports_extra = lambda a, ac, ex: PopcornBoxWindow._catalog_supports_extra(self.win, a, ac, ex)
        self.win._restore_dropdown_selection = lambda dd, it, ts: PopcornBoxWindow._restore_dropdown_selection(self.win, dd, it, ts)
        self.win._discover_result_search_query = lambda: ""

    def tearDown(self):
        database.CONFIG_DIR = self.orig_config_dir
        database.BASE_DIR = self.orig_base_dir
        database.DB_FILE = self.orig_db_file
        self.temp_dir.cleanup()

    def test_default_tmdb_catalogs_includes_trending(self):
        catalogs = tmdb_helper.get_default_tmdb_catalogs("movie")
        cat_ids = [c.get("catalog_id") for c in catalogs]
        self.assertIn("popular", cat_ids)
        self.assertIn("trending", cat_ids)
        self.assertIn("top-rated", cat_ids)
        self.assertIn("new-on-streaming", cat_ids)

    @patch("src.tmdb_helper._get_cached_request")
    def test_fetch_default_tmdb_catalog_page_filters(self, mock_get):
        mock_get.return_value = {
            "results": [
                {
                    "id": 101,
                    "title": "Action Movie",
                    "poster_path": "/p1.jpg",
                    "vote_average": 8.1,
                    "release_date": "2024-05-01",
                    "popularity": 95.0,
                }
            ],
            "total_pages": 5,
        }

        items, total_pages = tmdb_helper.fetch_default_tmdb_catalog_page(
            catalog_id="popular",
            media_type="movie",
            page=1,
            genre="Action",
            year="2024",
            language="Hindi",
            country="India",
            network="Netflix",
            sort_by="Rating",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "tmdb:101")
        self.assertTrue(mock_get.called)
        called_url = mock_get.call_args[0][0]
        self.assertIn("discover/movie", called_url)
        self.assertIn("sort_by=vote_average.desc", called_url)
        self.assertIn("primary_release_year=2024", called_url)
        self.assertIn("with_genres=28", called_url)  # Action is 28
        self.assertIn("with_original_language=hi", called_url)
        self.assertIn("with_origin_country=IN", called_url)

    def test_discover_section_dropdown_tmdb_sections(self):
        PopcornBoxWindow._populate_discover_section_dropdown(self.win, addon=None, chosen_type="movie", catalog_item=None)
        items = self.win._discover_section_items
        display_names = [it["display_name"] for it in items]
        self.assertEqual(display_names[0], "Sort by")
        self.assertIn("Popular", display_names)
        self.assertIn("Trending", display_names)
        self.assertIn("Top Rated", display_names)
        self.assertIn("New on Streaming", display_names)
        self.assertTrue(self.win.section_dropdown.get_sensitive())
        self.assertTrue(self.win.section_dropdown.get_visible())

    def test_discover_section_dropdown_hides_when_single_or_zero_sections(self):
        # 0 sections:
        empty_addon = {"name": "EmptyAddon", "catalogs": []}
        PopcornBoxWindow._populate_discover_section_dropdown(self.win, addon=empty_addon, chosen_type="movie", catalog_item=None)
        self.assertFalse(self.win.section_dropdown.get_visible())
        self.assertFalse(self.win.section_dropdown.get_sensitive())

        # 1 section:
        single_addon = {
            "name": "SingleAddon",
            "catalogs": [{"id": "single", "name": "Featured", "type": "movie"}]
        }
        PopcornBoxWindow._populate_discover_section_dropdown(self.win, addon=single_addon, chosen_type="movie", catalog_item=None)
        self.assertFalse(self.win.section_dropdown.get_visible())
        self.assertFalse(self.win.section_dropdown.get_sensitive())

        # Multiple sections:
        multi_addon = {
            "name": "MultiAddon",
            "catalogs": [
                {"id": "cat1", "name": "Section 1", "type": "movie"},
                {"id": "cat2", "name": "Section 2", "type": "movie"}
            ]
        }
        PopcornBoxWindow._populate_discover_section_dropdown(self.win, addon=multi_addon, chosen_type="movie", catalog_item=None)
        self.assertTrue(self.win.section_dropdown.get_visible())
        self.assertTrue(self.win.section_dropdown.get_sensitive())

    def test_catalog_capability_checking_hides_unsupported_filters(self):
        cinemeta_mock = {
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "catalogs": [
                {
                    "id": "top",
                    "name": "Popular",
                    "type": "movie",
                    "extraSupported": ["genre", "skip"],
                    "genres": ["Action", "Comedy", "Drama"],
                }
            ],
        }

        active_cat = cinemeta_mock["catalogs"][0]

        # Genre is supported
        PopcornBoxWindow._populate_discover_genre_dropdown(self.win, cinemeta_mock, "movie", active_cat)
        self.assertTrue(self.win.genre_dropdown.get_sensitive())
        self.assertTrue(self.win.genre_dropdown.get_visible())

        # Year is not supported by Cinemeta 'top'
        PopcornBoxWindow._populate_discover_year_dropdown(self.win, cinemeta_mock, "movie", active_cat)
        self.assertFalse(self.win.year_dropdown.get_visible())

        # Language is not supported by Cinemeta
        PopcornBoxWindow._populate_discover_language_dropdown(self.win, cinemeta_mock, "movie", active_cat)
        self.assertFalse(self.win.language_dropdown.get_visible())

        # Country is not supported by Cinemeta
        PopcornBoxWindow._populate_discover_country_dropdown(self.win, cinemeta_mock, "movie", active_cat)
        self.assertFalse(self.win.country_dropdown.get_visible())

        # Network is not supported by Cinemeta
        PopcornBoxWindow._populate_discover_network_dropdown(self.win, cinemeta_mock, "movie", active_cat)
        self.assertFalse(self.win.network_dropdown.get_visible())

    def test_catalog_capability_checking_shows_all_for_default_tmdb(self):
        # Default TMDb has addon=None
        PopcornBoxWindow._populate_discover_genre_dropdown(self.win, None, "movie", None)
        self.assertTrue(self.win.genre_dropdown.get_visible())

        PopcornBoxWindow._populate_discover_year_dropdown(self.win, None, "movie", None)
        self.assertTrue(self.win.year_dropdown.get_visible())

        PopcornBoxWindow._populate_discover_language_dropdown(self.win, None, "movie", None)
        self.assertTrue(self.win.language_dropdown.get_visible())

        PopcornBoxWindow._populate_discover_country_dropdown(self.win, None, "movie", None)
        self.assertTrue(self.win.country_dropdown.get_visible())

        PopcornBoxWindow._populate_discover_network_dropdown(self.win, None, "movie", None)
        self.assertTrue(self.win.network_dropdown.get_visible())

    def test_flowbox_sort_func(self):
        c1 = MagicMock()
        c2 = MagicMock()
        w1 = MagicMock()
        w2 = MagicMock()
        c1.get_child.return_value = w1
        c2.get_child.return_value = w2
        w1._is_empty_box = False
        w2._is_empty_box = False

        w1._load_index = 0
        w2._load_index = 1
        res = PopcornBoxWindow._discover_flowbox_sort_func(self.win, c1, c2)
        self.assertEqual(res, -1)  # c1 precedes c2

        res_rev = PopcornBoxWindow._discover_flowbox_sort_func(self.win, c2, c1)
        self.assertEqual(res_rev, 1)  # c2 comes after c1

    def test_get_current_discover_filter_dict_section_and_sort(self):
        self.win._discover_type_items = [("Type", "all")]
        self.win._discover_catalog_items = [{"manifest_url": None}]
        self.win._discover_section_items = [
            {"catalog_id": None, "display_name": "Sort by"},
            {"catalog_id": "popular", "display_name": "Popular"}
        ]
        self.win.section_dropdown.set_model(Gtk.StringList.new(["Sort by", "Popular"]))

        # When "Sort by" placeholder is selected:
        self.win.section_dropdown.set_selected(0)
        filters = PopcornBoxWindow._get_current_discover_filter_dict(self.win)
        self.assertIsNone(filters["catalog_id"])
        self.assertIsNone(filters["section"])
        self.assertIsNone(filters["sort_by"])

        # When "Popular" is selected:
        self.win.section_dropdown.set_selected(1)
        filters = PopcornBoxWindow._get_current_discover_filter_dict(self.win)
        self.assertEqual(filters["catalog_id"], "popular")
        self.assertEqual(filters["section"], "Popular")
        self.assertIsNone(filters["sort_by"])


if __name__ == "__main__":
    unittest.main()
