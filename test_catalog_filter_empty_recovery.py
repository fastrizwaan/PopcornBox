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


class TestCatalogFilterEmptyRecovery(unittest.TestCase):
    def setUp(self):
        self.win = CineWindow.__new__(CineWindow)
        self.win._discover_catalog_list_cache = {}
        self.win._discover_views = {}
        self.win.current_sort = "Popular"
        self.win.current_year = None
        self.win.current_genre = None
        self.win.current_language = None
        self.win.current_media_type = "movie"
        self.win._current_discover_type = "all"
        self.win._current_discover_addon = None

        self.win.discover_scrolled = Gtk.ScrolledWindow()
        self.win.discover_box = Gtk.Box()
        self.win.discover_scrolled.set_child(self.win.discover_box)
        self.win.library_stack = Gtk.Stack()
        self.win.library_stack.add_named(self.win.discover_scrolled, "discover")
        self.win.content_flowbox = Gtk.FlowBox()
        content_box = Gtk.Box()
        content_box.append(self.win.content_flowbox)
        self.win.library_stack.add_named(content_box, "content")
        self.win.unique_flowbox = Gtk.FlowBox()
        unique_box = Gtk.Box()
        unique_box.append(self.win.unique_flowbox)
        self.win.library_stack.add_named(unique_box, "unique")
        self.win.library_stack.set_visible_child_name("discover")

        self.win.catalog_dropdown = Gtk.DropDown()
        self.win.catalog_dropdown.set_model(Gtk.StringList.new(["All Catalogs", "Addon A"]))
        self.win.category_btn_stack = Gtk.Stack()
        self.win.category_btn_stack.add_named(Gtk.Box(), "home")
        self.win.category_btn_stack.set_visible_child_name("home")

        import threading
        self.win._unique_lock = threading.Lock()
        self.win._unique_abort_event = None
        self.win.unique_scrolled = MagicMock()
        self.win.unique_seen_ids = set()
        self.win.unique_seen_keys = set()
        self.win.unique_page_per_catalog = {}
        self.win.unique_exhausted_catalogs = set()
        self.win.unique_truly_empty_catalogs = set()
        self.win.unique_productive_catalogs = []
        self.win.unique_empty_streak = {}
        self.win.unique_catalog_idx = 0
        self.win.discover_back_box = Gtk.Box()
        self.win.genre_dropdown = None
        self.win._sync_library_sort_dropdown_selection = MagicMock()
        self.win._sync_library_genre_dropdown_selection = MagicMock()
        self.win._update_continue_watching_section = MagicMock()
        self.win._load_next_discover_batch = MagicMock()
        self.win._apply_sort_to_current_catalog = MagicMock()
        self.win._refresh_content = MagicMock()

    def test_empty_catalog_list_is_never_cached(self):
        """When an addon produces 0 rows, _discover_catalog_list_cache must NOT cache empty []."""
        dead_addon_url = "https://dead-addon.test/manifest.json"
        with patch("src.database.get_addons", return_value=[]):
            rows = CineWindow._get_discover_catalog_list(
                self.win,
                filter_media_type="all",
                filter_addon_url=dead_addon_url
            )
        self.assertEqual(len(rows), 0)
        cache_key = ("all", dead_addon_url, "Popular", "", "", "")
        self.assertNotIn(cache_key, self.win._discover_catalog_list_cache)

    def test_subsequent_fetch_succeeds_after_transient_empty(self):
        """If an addon was temporarily offline and had 0 rows, coming online immediately generates rows without cache poisoning."""
        addon_url = "https://live-addon.test/manifest.json"
        mock_addon = {
            "name": "Live Addon",
            "manifest_url": addon_url,
            "enabled": True,
            "catalogs_enabled": True,
            "resources": ["catalog"],
            "catalogs": [{"id": "live-cat", "name": "Live Cat", "type": "movie"}]
        }

        # Step 1: Temporarily empty / offline
        with patch("src.database.get_addons", return_value=[]):
            rows1 = CineWindow._get_discover_catalog_list(
                self.win,
                filter_media_type="all",
                filter_addon_url=addon_url
            )
        self.assertEqual(len(rows1), 0)

        # Step 2: Now addon is present
        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch("src.api.is_catalog_browsable", return_value=True):
            rows2 = CineWindow._get_discover_catalog_list(
                self.win,
                filter_media_type="all",
                filter_addon_url=addon_url
            )
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0]["catalog_id"], "live-cat")

    def test_refresh_discover_page_clears_addon_on_zero_rows_and_shows_reset_button(self):
        """When a filtered addon has 0 rows, _current_discover_addon is cleared and reset button is added."""
        dead_addon_url = "https://empty-addon.test/manifest.json"
        self.win._current_discover_addon = dead_addon_url

        with patch.object(self.win, "_get_discover_catalog_list", return_value=[]):
            CineWindow._refresh_discover_page(
                self.win,
                filter_media_type="all",
                filter_addon_url=dead_addon_url
            )

        # 1. _current_discover_addon must be auto-cleared so user isn't trapped
        self.assertIsNone(self.win._current_discover_addon)

        # 2. Empty box contains message and 'Show All Catalogs' button
        target_box = self.win.discover_box
        empty_box = target_box.get_first_child()
        self.assertIsNotNone(empty_box)
        lbl = empty_box.get_first_child()
        self.assertIsNotNone(lbl)
        self.assertIn("No catalogs available", lbl.get_label())

        reset_btn = lbl.get_next_sibling()
        self.assertIsNotNone(reset_btn)
        self.assertIsInstance(reset_btn, Gtk.Button)
        self.assertEqual(reset_btn.get_label(), "Show All Catalogs")

    def test_library_sort_changed_navigates_to_content_for_specific_catalog(self):
        """When a user selects an actual catalog from the sort dropdown, it navigates to 'content' view."""
        self.win._dropdown_catalogs_map = {
            "PlavioHD - Sports": {
                "manifest_url": "https://plaviohd.test/manifest.json",
                "catalog_id": "plaviohd-sports",
                "media_type": "tv",
                "display_name": "PlavioHD - Sports"
            }
        }
        dropdown = Gtk.DropDown()
        dropdown.set_model(Gtk.StringList.new(["Popular", "PlavioHD - Sports"]))
        dropdown.set_selected(1)

        CineWindow._on_library_sort_changed(self.win, dropdown, None)

        self.assertEqual(self.win.library_stack.get_visible_child_name(), "content")
        self.assertEqual(self.win.current_catalog["catalog_id"], "plaviohd-sports")
        self.win._refresh_content.assert_called_once()

    def test_library_genre_changed_resets_incompatible_addon(self):
        """When an active addon filter does not support the selected genre, _current_discover_addon is reset to None."""
        addon_url = "https://movies-only.test/manifest.json"
        self.win._current_discover_addon = addon_url
        self.win.current_genre = "Anime"
        self.win.current_sort = "Genre"

        mock_addon = {
            "name": "Movies Addon",
            "manifest_url": addon_url,
            "catalogs": [{"id": "top", "genres": ["Action", "Comedy"]}]
        }

        dropdown = Gtk.DropDown()
        dropdown.set_model(Gtk.StringList.new(["Anime"]))
        dropdown.set_selected(0)

        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch.object(self.win, "_refresh_discover_page") as mock_refresh:
            CineWindow._on_library_genre_changed(self.win, dropdown, None)
            # Must reset _current_discover_addon so other addons supporting Anime can be displayed
            self.assertIsNone(self.win._current_discover_addon)
            mock_refresh.assert_called_with(filter_media_type="all", filter_addon_url=None, force_refresh=True)

    def test_unique_combined_view_fallbacks_when_filtered_addon_has_no_catalogs(self):
        """When an addon filter yields 0 catalogs in unique mode, it falls back to all catalogs instead of staying blank."""
        dead_addon_url = "https://dead.test/manifest.json"
        self.win._current_discover_addon = dead_addon_url
        self.win._is_unique_mode_active = MagicMock(return_value=True)

        all_catalogs = [{"manifest_url": "https://cinemeta/manifest.json", "catalog_id": "top", "media_type": "movie"}]

        def mock_get_cat_list(filter_media_type=None, filter_addon_url=None):
            if filter_addon_url == dead_addon_url:
                return []
            return all_catalogs

        self.win._get_discover_catalog_list = MagicMock(side_effect=mock_get_cat_list)

        with patch("src.api.is_addon_online", return_value=True):
            CineWindow._refresh_unique_combined_view(self.win)

        self.assertIsNone(self.win._current_discover_addon)
        self.assertEqual(len(self.win.unique_catalogs), 1)
        self.assertEqual(self.win.unique_catalogs[0]["catalog_id"], "top")


if __name__ == "__main__":
    unittest.main()
