import unittest
import sys
import tempfile
import os
import subprocess
import time
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

res_file = Path(__file__).resolve().parent / "build-dir/files/share/popcorn-box/cine.gresource"
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src.window import CineWindow, DISCOVER_ROW_CARD_LIMIT
from src import api, database


class TestHomeCatalogPopulation(unittest.TestCase):
    def setUp(self):
        self._net_patchers = [
            patch("src.api.queue_metadata_hydration", return_value=False),
            patch("src.movie_widget.fetch_fallback_poster"),
            patch("src.movie_widget.load_image_into_picture"),
        ]
        for patcher in self._net_patchers:
            patcher.start()

        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_db_file = database.DB_FILE
        database.CONFIG_DIR = Path(self.temp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database.reset_json_cache()
        with database._cache_db_lock:
            database._cache_conn = None
            database._cache_db_initialized = False
        api.reset_addon_health_cache()

        self.win = MagicMock()
        self.win.category_btn_stack = Gtk.Stack()
        for name in ("home", "movies", "series", "anime", "discover"):
            self.win.category_btn_stack.add_named(Gtk.Box(), name)
        self.win.category_btn_stack.set_visible_child_name("home")

        self.win.library_stack = Gtk.Stack()
        self.win.library_stack.add_named(Gtk.Box(), "discover")
        self.win.library_stack.set_visible_child_name("discover")

        self.win.discover_scrolled = Gtk.ScrolledWindow()
        self.win.discover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.win.discover_scrolled.set_child(self.win.discover_box)
        self.win.details_box = Gtk.Box()
        self.win.content_flowbox = Gtk.FlowBox()
        self.win.discover_back_box = Gtk.Box()
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_language = None
        self.win.current_country = None

        self.win.discover_request_id = 1
        self.win._discover_is_loading_rows = False
        self.win._discover_next_row_index = 0
        self.win._discover_empty_batches = 0
        self.win._discover_auto_fill_count = 0
        self.win._discover_views = {}
        self.win._discover_abort_event = None
        self.win._discover_batch_timer_id = None
        self.win._discover_scroll_parked_rows = None
        self.win._discover_scroll_parked_upper = None

        self.win._load_next_discover_batch = lambda req_id, batch_size=4: CineWindow._load_next_discover_batch(self.win, req_id, batch_size)
        self.win._populate_discover_row_slot = lambda slot, row_info, items, target_box: CineWindow._populate_discover_row_slot(self.win, slot, row_info, items, target_box)
        self.win._set_discover_spinner_active = MagicMock()
        self.win._discover_result_matches_item = lambda it: True
        self.win._discover_row_card_metrics = lambda: (15, 130)
        self.win._fit_discover_row_card = lambda card: None
        self.win._queue_next_discover_tmdb_page = MagicMock(return_value=False)
        self.win._cancel_discover_batch_timer = lambda: CineWindow._cancel_discover_batch_timer(self.win)
        self.win._get_discover_view_key = MagicMock(return_value="home|all|all|all|all|all|all|Popular")

    def tearDown(self):
        self.win._cancel_discover_batch_timer()
        for patcher in self._net_patchers:
            patcher.stop()
        with database._cache_db_lock:
            if database._cache_conn:
                try:
                    database._cache_conn.close()
                except Exception:
                    pass
                database._cache_conn = None
        database.CONFIG_DIR = self.orig_config_dir
        database.DB_FILE = self.orig_db_file

    def _drain_glib(self, max_iterations=50):
        ctx = GLib.MainContext.default()
        for _ in range(max_iterations):
            time.sleep(0.01)
            while ctx.iteration(False):
                pass

    def test_discover_row_card_limit_is_30(self):
        """Row card limit constant is 30 for efficient, smooth rendering."""
        self.assertEqual(DISCOVER_ROW_CARD_LIMIT, 30)

    def test_home_continuous_population_loads_all_batches(self):
        """Home continuously populates all catalog batches until all rows are available without needing scroll."""
        catalogs = [
            {"title": f"Catalog {i}", "media_type": "movie", "catalog_id": f"c{i}",
             "manifest_url": f"https://addon{i}/manifest.json", "addon_name": f"Addon {i}", "genres": []}
            for i in range(10)
        ]
        self.win._discover_catalog_list = catalogs

        sample_items = [{"id": f"tt_{j}", "title": f"Movie {j}", "type": "movie"} for j in range(10)]

        with patch("src.api.fetch_items", return_value=sample_items), \
             patch("src.database.get_cached_catalog", return_value=None), \
             patch("src.database.save_cached_catalog"), \
             patch("src.api.is_addon_online", return_value=True):

            # Start loading with initial batch of 4
            CineWindow._load_next_discover_batch(self.win, req_id=1, batch_size=4)

            # Wait for background batches to chain through all 10 catalogs
            timeout = time.time() + 5.0
            while time.time() < timeout and (self.win._discover_next_row_index < 10 or self.win._discover_is_loading_rows):
                self._drain_glib(10)

        # All 10 catalogs should have been processed
        self.assertEqual(self.win._discover_next_row_index, 10)
        self.assertFalse(self.win._discover_is_loading_rows)
        # Spinner stopped after complete availability
        self.win._set_discover_spinner_active.assert_called_with(False)

        # Count rendered rows in discover_box
        row_count = 0
        child = self.win.discover_box.get_first_child()
        while child:
            # Each row slot has header and scrolled window
            if child.get_first_child() is not None:
                row_count += 1
            child = child.get_next_sibling()
        self.assertEqual(row_count, 10)

    def test_row_cards_capped_at_30(self):
        """Each catalog row is capped at exactly DISCOVER_ROW_CARD_LIMIT (30)."""
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        row_info = {"title": "Test Row", "media_type": "movie", "catalog_id": "test", "manifest_url": "https://test/manifest.json"}
        # 50 items
        items = [{"id": f"tt{i}", "title": f"Movie {i}", "type": "movie"} for i in range(50)]

        CineWindow._populate_discover_row_slot(self.win, slot, row_info, items, self.win.discover_box)

        sec_scroll = slot.get_first_child().get_next_sibling()
        self.assertIsNotNone(sec_scroll)
        viewport = sec_scroll.get_child()
        sec_row = viewport.get_child() if hasattr(viewport, "get_child") else viewport
        card_count = 0
        c = sec_row.get_first_child()
        while c:
            card_count += 1
            c = c.get_next_sibling()
        self.assertEqual(card_count, 30)

    def test_scroll_to_bottom_triggers_immediate_batch(self):
        """Scrolling to bottom immediately consumes pending gentle timer and loads next batch."""
        self.win._discover_catalog_list = [{"title": f"Cat {i}"} for i in range(12)]
        self.win._discover_next_row_index = 4
        self.win._load_next_discover_batch = MagicMock()

        # Set an active batch timer
        self.win._discover_batch_timer_id = GLib.timeout_add(1000, lambda: False)

        adj = MagicMock()
        adj.get_value.return_value = 1500.0
        adj.get_page_size.return_value = 600.0
        adj.get_upper.return_value = 2000.0  # at_bottom is True

        CineWindow._on_discover_scroll(self.win, adj)

        # Pending timer cancelled
        self.assertIsNone(self.win._discover_batch_timer_id)
        # Immediate batch dispatched
        self.win._load_next_discover_batch.assert_called_once_with(1, batch_size=4)

    def test_cancel_all_background_fetching_cleans_timer(self):
        """_cancel_all_background_fetching terminates in-flight batch timer without warning."""
        self.win._discover_batch_timer_id = GLib.timeout_add(500, lambda: False)
        CineWindow._cancel_all_background_fetching(self.win)
        self.assertIsNone(self.win._discover_batch_timer_id)


if __name__ == "__main__":
    unittest.main()
