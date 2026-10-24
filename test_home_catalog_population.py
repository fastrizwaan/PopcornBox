import unittest
import sys
import tempfile
import os
import subprocess
import time
import threading
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

    def test_home_catalogs_exclude_tv_radio_music(self):
        """Home feed must strictly include movie, series, and anime catalogs, excluding tv, radio, and music."""
        mixed_addon = {
            "name": "Entertainment Addon",
            "manifest_url": "https://entertainment.test/manifest.json",
            "enabled": True,
            "catalogs": [
                {"id": "c_movie", "name": "Popular Movies", "type": "movie"},
                {"id": "c_series", "name": "Trending Series", "type": "series"},
                {"id": "c_anime", "name": "Anime Hits", "type": "anime"},
                {"id": "c_tv", "name": "Live TV Channels", "type": "tv"},
                {"id": "c_channel", "name": "News Channels", "type": "channel"},
                {"id": "c_radio", "name": "FM Radio", "type": "radio"},
                {"id": "c_music", "name": "Top Songs", "type": "music"},
                {"id": "c_podcast", "name": "Talk Podcasts", "type": "podcast"},
            ]
        }

        with patch("src.database.get_addons", return_value=[mixed_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.is_catalog_provider", return_value=True), \
             patch("src.api.get_addon_catalogs", side_effect=lambda a, cache_only=False: a.get("catalogs", [])), \
             patch("src.api.is_catalog_browsable", return_value=True):
            home_rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", is_home=True)

        home_cat_ids = {r["catalog_id"] for r in home_rows}
        self.assertEqual(home_cat_ids, {"c_movie", "c_series", "c_anime"})
        for r in home_rows:
            self.assertIn(r["media_type"], ("movie", "series", "anime"))
            self.assertNotIn(r["media_type"], ("tv", "channel", "radio", "music", "podcast"))

    def test_discover_catalogs_include_tv_radio_music(self):
        """Discover view (is_home=False) retains tv, channel, radio, and music catalogs."""
        mixed_addon = {
            "name": "Entertainment Addon",
            "manifest_url": "https://entertainment.test/manifest.json",
            "enabled": True,
            "catalogs": [
                {"id": "c_movie", "name": "Popular Movies", "type": "movie"},
                {"id": "c_series", "name": "Trending Series", "type": "series"},
                {"id": "c_anime", "name": "Anime Hits", "type": "anime"},
                {"id": "c_tv", "name": "Live TV Channels", "type": "tv"},
                {"id": "c_channel", "name": "News Channels", "type": "channel"},
                {"id": "c_radio", "name": "FM Radio", "type": "radio"},
                {"id": "c_music", "name": "Top Songs", "type": "music"},
            ]
        }

        with patch("src.database.get_addons", return_value=[mixed_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.is_catalog_provider", return_value=True), \
             patch("src.api.get_addon_catalogs", side_effect=lambda a, cache_only=False: a.get("catalogs", [])), \
             patch("src.api.is_catalog_browsable", return_value=True):
            discover_rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", is_home=False)

        disc_cat_ids = {r["catalog_id"] for r in discover_rows}
        self.assertIn("c_tv", disc_cat_ids)
        self.assertIn("c_channel", disc_cat_ids)
        self.assertIn("c_radio", disc_cat_ids)
        self.assertIn("c_music", disc_cat_ids)
        self.assertIn("c_movie", disc_cat_ids)
        self.assertIn("c_series", disc_cat_ids)
        self.assertIn("c_anime", disc_cat_ids)

    def test_home_and_discover_cache_isolation(self):
        """Home and Discover catalog list cache entries are isolated and never overwrite each other."""
        self.win._discover_catalog_list_cache = {}
        addon = {
            "name": "AllInOne",
            "manifest_url": "https://allinone.test/manifest.json",
            "enabled": True,
            "catalogs": [
                {"id": "m1", "name": "Movie 1", "type": "movie"},
                {"id": "tv1", "name": "Live TV 1", "type": "tv"},
            ]
        }
        with patch("src.database.get_addons", return_value=[addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.is_catalog_provider", return_value=True), \
             patch("src.api.get_addon_catalogs", side_effect=lambda a, cache_only=False: a.get("catalogs", [])), \
             patch("src.api.is_catalog_browsable", return_value=True):
            home_rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", is_home=True)
            discover_rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", is_home=False)

        self.assertEqual(len(home_rows), 1)
        self.assertEqual(home_rows[0]["catalog_id"], "m1")
        self.assertEqual(len(discover_rows), 2)
        # Both cache keys exist independently
        home_key = ("home", "all", "", "Popular", "", "", "", "")
        disc_key = ("all", "", "Popular", "", "", "", "")
        self.assertIn(home_key, self.win._discover_catalog_list_cache)
        self.assertIn(disc_key, self.win._discover_catalog_list_cache)
        self.assertEqual(len(self.win._discover_catalog_list_cache[home_key]), 1)
        self.assertEqual(len(self.win._discover_catalog_list_cache[disc_key]), 2)

    def test_row_horizontal_scrollbar_hidden(self):
        """Every discover row and continue watching row must have PolicyType.EXTERNAL and hidden horizontal scrollbar."""
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        row_info = {"title": "Test Row", "media_type": "movie", "catalog_id": "test", "manifest_url": "https://test/manifest.json"}
        items = [{"id": f"tt{i}", "title": f"Movie {i}", "type": "movie"} for i in range(10)]

        CineWindow._populate_discover_row_slot(self.win, slot, row_info, items, self.win.discover_box)
        sec_scroll = slot.get_first_child().get_next_sibling()
        self.assertIsInstance(sec_scroll, Gtk.ScrolledWindow)
        h_policy, v_policy = sec_scroll.get_policy()
        self.assertEqual(h_policy, Gtk.PolicyType.EXTERNAL)
        self.assertEqual(v_policy, Gtk.PolicyType.NEVER)
        h_sb = sec_scroll.get_hscrollbar()
        if h_sb:
            self.assertFalse(h_sb.get_visible())

    def test_home_initial_batch_loads_all_catalogs_upfront(self):
        """Home feed dispatches all non-tv/music/radio catalogs upfront so all sections are ready without appearing on scroll."""
        catalogs = [
            {"title": f"Cat {i}", "media_type": "movie", "catalog_id": f"c{i}", "manifest_url": f"https://addon{i}/manifest.json"}
            for i in range(12)
        ]
        self.win._discover_catalog_list = catalogs
        self.win._load_next_discover_batch = MagicMock()

        self.win._get_discover_catalog_list = MagicMock(return_value=catalogs)
        self.win.category_btn_stack.set_visible_child_name("home")
        CineWindow._refresh_discover_page(self.win, filter_media_type="all")

        # Initial batch must be all 12 catalogs, not capped at 6
        self.win._load_next_discover_batch.assert_called_once()
        args, kwargs = self.win._load_next_discover_batch.call_args
        batch_size = kwargs.get("batch_size") or (args[1] if len(args) > 1 else None)
        self.assertEqual(batch_size, 12)

    def test_duplicate_refresh_while_loading_does_not_abort_worker(self):
        """Redundant in-flight _refresh_discover_page call for the same view does not abort the active worker."""
        self.win.category_btn_stack.set_visible_child_name("home")
        view_key = "home|all|all|all|all|all|all|Popular"
        self.win._get_discover_view_key = MagicMock(return_value=view_key)
        self.win._active_discover_view_key = view_key
        self.win._discover_is_loading_rows = True
        self.win.discover_request_id = 42
        self.win._load_next_discover_batch = MagicMock()

        CineWindow._refresh_discover_page(self.win, filter_media_type="all")

        # Request ID must not be bumped, and no new batch dispatched
        self.assertEqual(self.win.discover_request_id, 42)
        self.win._load_next_discover_batch.assert_not_called()

    def test_cache_hit_with_zero_rendered_rows_resumes_loading(self):
        """If a cached view has 0 rendered catalog rows, next_row_index resets to 0 and loads remaining."""
        self.win.category_btn_stack.set_visible_child_name("home")
        view_key = "home|all|all|all|all|all|all|Popular"
        self.win._get_discover_view_key = MagicMock(return_value=view_key)
        
        cached_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Add continue watching header and scroll (so has_valid_content is true)
        cw_hdr = Gtk.Box()
        cw_hdr.add_css_class("continue-watching-header")
        cached_box.append(cw_hdr)

        catalogs = [{"title": f"Cat {i}"} for i in range(10)]
        self.win._discover_views = {
            view_key: {
                "box": cached_box,
                "catalog_list": catalogs,
                "next_row_index": 10,  # Falsely advanced previously
                "request_id": 1,
            }
        }
        self.win._load_next_discover_batch = MagicMock()

        CineWindow._refresh_discover_page(self.win, filter_media_type="all")

        # Must have reset next_row_index to 0 because 0 catalog rows were rendered
        self.assertEqual(self.win._discover_views[view_key]["next_row_index"], 0)
        self.assertEqual(self.win._discover_next_row_index, 0)
        self.assertIsNotNone(self.win._discover_batch_timer_id)

    def test_aborted_batch_cleans_empty_slots(self):
        """An aborted batch removes unpopulated placeholder slots so no dead widgets linger in discover_box."""
        catalogs = [{"title": f"Cat {i}", "media_type": "movie", "catalog_id": f"c{i}"} for i in range(5)]
        self.win._discover_catalog_list = catalogs
        self.win._discover_next_row_index = 0

        # Simulate abort event already set
        abort_ev = threading.Event()
        abort_ev.set()
        self.win._discover_abort_event = abort_ev
        self.win.discover_request_id = 99

        CineWindow._load_next_discover_batch(self.win, req_id=99, batch_size=5)

        self._drain_glib(10)

        # No empty slot children left behind in discover_box
        child = self.win.discover_box.get_first_child()
        count = 0
        while child:
            count += 1
            child = child.get_next_sibling()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
