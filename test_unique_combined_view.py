import unittest
from unittest.mock import MagicMock, patch
import sys
import tempfile
import os
import threading
import subprocess
from pathlib import Path

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src.window import CineWindow


class TestUniqueCombinedView(unittest.TestCase):

    def test_is_unique_item_deduplication(self):
        win = MagicMock()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()

        # 1. First IMDb movie
        m1 = {"id": "tt0111161", "title": "The Shawshank Redemption", "year": "1994"}
        self.assertTrue(CineWindow._is_unique_item(win, m1))
        self.assertIn("tt0111161", win.unique_seen_ids)

        # 2. Duplicate with same IMDb ID
        m2 = {"id": "tt0111161", "title": "The Shawshank Redemption", "year": "1994"}
        self.assertFalse(CineWindow._is_unique_item(win, m2))

        # 3. Duplicate with episode / stream suffix on ID
        m3 = {"id": "tt0111161:1:1", "imdb_id": "tt0111161", "title": "The Shawshank Redemption", "year": "1994"}
        self.assertFalse(CineWindow._is_unique_item(win, m3))

        # 4. Cross-addon duplicate: different ID (e.g. tmdb:278) but same IMDb ID
        m4 = {"id": "tmdb:278", "imdb_id": "tt0111161", "title": "The Shawshank Redemption", "year": "1994"}
        self.assertFalse(CineWindow._is_unique_item(win, m4))

        # 5. Cross-addon duplicate: different ID and no IMDb ID, but exact same Title and Year
        m5 = {"id": "custom:999", "title": "The Shawshank Redemption", "year": "1994"}
        self.assertFalse(CineWindow._is_unique_item(win, m5))

        # 6. Different movie with different Title and Year
        m6 = {"id": "tt0068646", "title": "The Godfather", "year": "1972"}
        self.assertTrue(CineWindow._is_unique_item(win, m6))

        # 7. Same title but different release year (e.g. remake)
        m7 = {"id": "tt0077651", "title": "Halloween", "year": "1978"}
        m8 = {"id": "tt1502407", "title": "Halloween", "year": "2018"}
        self.assertTrue(CineWindow._is_unique_item(win, m7))
        self.assertTrue(CineWindow._is_unique_item(win, m8))

        # 8. Invalid non-dict item
        self.assertFalse(CineWindow._is_unique_item(win, None))
        self.assertFalse(CineWindow._is_unique_item(win, "invalid"))

    def test_on_unique_toggled_active_and_inactive(self):
        win = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "discover"

        btn = MagicMock()
        btn.get_active.return_value = True

        # Toggle ON
        CineWindow._on_unique_toggled(win, btn)
        win.library_stack.set_visible_child_name.assert_called_with("unique")
        win._refresh_unique_combined_view.assert_called_once()
        win.discover_back_box.set_visible.assert_called_with(False)
        self.assertFalse(getattr(win, "_unique_was_content_page", False))

        # Toggle OFF (reverts to discover)
        btn.get_active.return_value = False
        CineWindow._on_unique_toggled(win, btn)
        win.library_stack.set_visible_child_name.assert_called_with("discover")
        win._refresh_discover_page.assert_called_once()

    def test_on_unique_toggled_reverts_to_content_if_was_content(self):
        win = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "content"
        win.current_catalog = {"catalog_id": "top", "manifest_url": "https://addon/manifest.json"}

        btn = MagicMock()
        btn.get_active.return_value = True

        # Toggle ON from content page
        CineWindow._on_unique_toggled(win, btn)
        self.assertTrue(getattr(win, "_unique_was_content_page", False))
        win.library_stack.set_visible_child_name.assert_called_with("unique")

        # Toggle OFF reverts to content page
        btn.get_active.return_value = False
        CineWindow._on_unique_toggled(win, btn)
        win.library_stack.set_visible_child_name.assert_called_with("content")
        win.discover_back_box.set_visible.assert_called_with(True)
        win._refresh_content.assert_called_once()

    def test_refresh_unique_combined_view_combines_catalogs_and_prioritizes_current(self):
        win = MagicMock()
        win.unique_flowbox = Gtk.FlowBox()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.category_btn_stack.get_visible_child_name.return_value = "movies"
        win._current_discover_type = "movie"
        win._current_discover_addon = None

        cat1 = {"catalog_id": "cat1", "manifest_url": "https://url1/manifest.json", "media_type": "movie", "title": "Cat 1"}
        cat2 = {"catalog_id": "cat2", "manifest_url": "https://url2/manifest.json", "media_type": "movie", "title": "Cat 2"}
        win._get_discover_catalog_list.return_value = [cat1, cat2]

        # Active catalog is cat2 -> should be moved to front of catalogs
        win.current_catalog = {"catalog_id": "cat2", "manifest_url": "https://url2/manifest.json", "media_type": "movie"}

        with patch("src.window.cancel_pending_image_downloads", create=True):
            CineWindow._refresh_unique_combined_view(win)

        self.assertEqual(len(win.unique_catalogs), 2)
        # Prioritized current_catalog at index 0
        self.assertEqual(win.unique_catalogs[0]["catalog_id"], "cat2")
        self.assertEqual(win.unique_catalogs[1]["catalog_id"], "cat1")
        win._load_next_unique_batch.assert_called_once()

    def test_filters_refresh_unique_when_active(self):
        win = MagicMock()
        win._syncing_genre_dropdown = False
        win._syncing_sort_dropdown = False
        win._is_unique_mode_active.return_value = True
        win.current_genre = None
        win.current_sort = "Popular"

        # Genre changed
        dropdown = Gtk.DropDown.new_from_strings(["All Genres", "Hindi", "Comedy"])
        dropdown.set_selected(1)
        CineWindow._on_library_genre_changed(win, dropdown, None)
        self.assertEqual(win.current_genre, "Hindi")
        win._refresh_unique_combined_view.assert_called_once()

        # Sort changed
        sort_dropdown = Gtk.DropDown.new_from_strings(["Popular", "Recent", "2024"])
        sort_dropdown.set_selected(2)
        CineWindow._on_library_sort_changed(win, sort_dropdown, None)
        self.assertEqual(win.current_sort, "2024")
        self.assertEqual(win.current_year, "2024")
        self.assertEqual(win._refresh_unique_combined_view.call_count, 2)

    def test_deliver_unique_slices_to_ui(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = True
        win.unique_flowbox = MagicMock()

        # Call with 12 items -> should split into slices of 5 items
        items = [{"id": f"tt{i}", "title": f"Movie {i}"} for i in range(12)]
        
        idle_callbacks = []
        with patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *args: idle_callbacks.append((fn, args))) as mock_idle, \
             patch("src.movie_widget.MovieWidget", side_effect=lambda it, cb: MagicMock()) as mock_mw:
            CineWindow._deliver_unique_slices_to_ui(win, items, req_id=1, has_more=True)
            self.assertTrue(len(idle_callbacks) >= 1)

            # Execute slices sequentially
            while idle_callbacks:
                fn, args = idle_callbacks.pop(0)
                fn(*args)

            # Verify all 12 widgets were appended to unique_flowbox
            self.assertEqual(win.unique_flowbox.append.call_count, 12)
            self.assertFalse(win.is_fetching_unique)

    def test_on_unique_scroll_threshold(self):
        win = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "unique"
        win.has_more_unique = True
        win.is_fetching_unique = False

        adj = MagicMock()
        # Case 1: content under-filled (upper <= page_size) -> SHOULD trigger to fill viewport
        adj.get_value.return_value = 0
        adj.get_page_size.return_value = 800
        adj.get_upper.return_value = 600
        CineWindow._on_unique_scroll(win, adj)
        win._load_next_unique_batch.assert_called_once()
        win._load_next_unique_batch.reset_mock()

        # Case 2: scrolled near top (far from bottom) -> should NOT trigger
        adj.get_upper.return_value = 4000
        adj.get_value.return_value = 500
        CineWindow._on_unique_scroll(win, adj)
        win._load_next_unique_batch.assert_not_called()

        # Case 3: scrolled near bottom (val + page_size >= upper - 800) -> SHOULD trigger
        adj.get_value.return_value = 3500
        CineWindow._on_unique_scroll(win, adj)
        win._load_next_unique_batch.assert_called_once()

    def test_on_unique_scroll_fetches_more_when_catalogs_exhausted(self):
        win = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "unique"
        win.has_more_unique = False
        win.is_fetching_unique = False

        cat1 = {"manifest_url": "https://addon1/manifest.json", "catalog_id": "top", "media_type": "movie"}
        cat2 = {"manifest_url": "https://addon2/manifest.json", "catalog_id": "popular", "media_type": "movie"}
        win.unique_catalogs = [cat1, cat2]
        cat1_key = ("https://addon1/manifest.json", "top", "movie")
        cat2_key = ("https://addon2/manifest.json", "popular", "movie")

        win.unique_exhausted_catalogs = {cat1_key, cat2_key}
        win.unique_truly_empty_catalogs = {cat2_key}  # cat2 confirmed 0 items at API end
        win.unique_retryable_catalogs = {cat1_key}  # cat1 hit transient failure, retry candidate
        win.unique_page_per_catalog = {cat1_key: 3, cat2_key: 1}

        adj = MagicMock()
        adj.get_page_size.return_value = 1000
        adj.get_upper.return_value = 4000
        adj.get_value.return_value = 3500  # near bottom

        CineWindow._on_unique_scroll(win, adj)

        # cat1 was soft-exhausted, so it should be discarded from exhausted set and allow fetching more pages
        self.assertNotIn(cat1_key, win.unique_exhausted_catalogs)
        # cat2 was truly empty, so it remains exhausted
        self.assertIn(cat2_key, win.unique_exhausted_catalogs)
        self.assertTrue(win.has_more_unique)
        win._load_next_unique_batch.assert_called_once()

    def test_deliver_unique_slices_empty_batch_schedules_retry_if_has_more(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = True

        timeout_callbacks = []
        with patch("gi.repository.GLib.timeout_add", side_effect=lambda ms, fn, *args: timeout_callbacks.append((ms, fn, args))) as mock_timeout:
            CineWindow._deliver_unique_slices_to_ui(win, [], req_id=1, has_more=True)
            self.assertFalse(win.is_fetching_unique)
            self.assertEqual(len(timeout_callbacks), 1)
            self.assertEqual(timeout_callbacks[0][0], 100)
            self.assertEqual(timeout_callbacks[0][1], win._load_next_unique_batch)

    def test_filter_items_by_genre_numeric_tmdb_and_aliases(self):
        from src import api
        items = [
            {"id": "tt1", "title": "Action Hero", "genres": [28]},                 # TMDB Action numeric
            {"id": "tt2", "title": "Anime Adventure", "genres": ["16"], "type": "anime"},  # TMDB Animation string
            {"id": "tt3", "title": "SciFi World", "genre_ids": [878]},             # TMDB Sci-Fi numeric in genre_ids
            {"id": "tt4", "title": "Action Adventurer", "genres": ["Action & Adventure"]}, # Compound string
            {"id": "tt5", "title": "Only Drama", "genres": ["Drama", 18]},         # Non-matching
        ]

        # Action: should match tt1 (numeric 28) and tt4 ("Action & Adventure")
        res_action = api.filter_items_by_genre(items, "Action")
        action_ids = [m["id"] for m in res_action]
        self.assertIn("tt1", action_ids)
        self.assertIn("tt4", action_ids)
        self.assertNotIn("tt2", action_ids)
        self.assertNotIn("tt5", action_ids)

        # Animation: should match tt2 (string "16" and anime type)
        res_anim = api.filter_items_by_genre(items, "Animation")
        anim_ids = [m["id"] for m in res_anim]
        self.assertIn("tt2", anim_ids)
        self.assertNotIn("tt1", anim_ids)
        self.assertNotIn("tt5", anim_ids)

        # Sci-Fi: should match tt3 (878)
        res_scifi = api.filter_items_by_genre(items, "Sci-Fi")
        scifi_ids = [m["id"] for m in res_scifi]
        self.assertIn("tt3", scifi_ids)
        self.assertNotIn("tt1", scifi_ids)

    def test_check_viewport_fill_triggers_when_underfilled(self):
        win = MagicMock()
        win.unique_request_id = 5
        win.has_more_unique = True
        win.is_fetching_unique = False
        win.unique_scrolled = MagicMock()
        adj = MagicMock()
        adj.get_value.return_value = 0
        adj.get_page_size.return_value = 1440
        adj.get_upper.return_value = 1600  # upper <= page_size + 400
        win.unique_scrolled.get_vadjustment.return_value = adj

        # Mock FlowBox with 20 items (less than 48 target)
        flowbox = Gtk.FlowBox()
        for i in range(20):
            flowbox.append(Gtk.Label(label=f"Item {i}"))
        win.unique_flowbox = flowbox

        timeout_callbacks = []
        with patch("gi.repository.GLib.timeout_add", side_effect=lambda ms, fn, *args: timeout_callbacks.append((ms, fn, args))):
            CineWindow._deliver_unique_slices_to_ui(win, [], req_id=5, has_more=True)

        # Execute timeout callback
        self.assertEqual(len(timeout_callbacks), 1)
        timeout_callbacks[0][1]()
        win._load_next_unique_batch.assert_called_once()


    def test_refresh_unique_ensures_library_stack_unique(self):
        win = MagicMock()
        win.unique_request_id = 0
        win._unique_abort_event = None
        win._unique_active_pool = None
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.unique_truly_empty_catalogs = set()
        win.unique_productive_catalogs = []
        win.unique_empty_streak = {}
        win.unique_flowbox = MagicMock()
        win.library_stack = MagicMock()
        win.library_stack.get_visible_child_name.return_value = "content"
        win.discover_back_box = MagicMock()
        win._get_discover_catalog_list.return_value = []

        CineWindow._refresh_unique_combined_view(win)

        win.library_stack.set_visible_child_name.assert_called_with("unique")
        win.discover_back_box.set_visible.assert_called_with(False)
        win.unique_flowbox.remove_all.assert_called_once()

    def test_database_dropdown_caches(self):
        from src import database
        database.invalidate_dropdown_caches()
        genres_1 = database.get_available_genres("movie")
        genres_2 = database.get_available_genres("movie")
        self.assertEqual(genres_1, genres_2)
        self.assertIn("Action", genres_1)

        sort_1 = database.get_available_sort_options("movie")
        sort_2 = database.get_available_sort_options("movie")
        self.assertEqual(sort_1, sort_2)
        self.assertIn("Popular", sort_1)
        self.assertIn("Featured", sort_1)

        database.invalidate_dropdown_caches()
        genres_3 = database.get_available_genres("movie")
        self.assertEqual(genres_1, genres_3)

    def test_populate_discover_row_slot(self):
        win = MagicMock()
        win.current_genre = "Action"
        win.current_year = None
        win._on_movie_clicked = MagicMock()

        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        target_box.append(slot)

        row_info = {
            "media_type": "movie",
            "catalog_id": "top",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "title": "Popular - Movies",
            "catalog_name": "Popular",
            "addon_name": "Cinemeta"
        }
        items = [
            {"id": "tt1", "title": "Movie 1", "type": "movie"},
            {"id": "tt2", "title": "Movie 2", "type": "movie"}
        ]

        CineWindow._populate_discover_row_slot(win, slot, row_info, items, target_box)

        # Slot should now contain header box and scrolled window
        header = slot.get_first_child()
        self.assertIsNotNone(header)
        scroll = header.get_next_sibling()
        self.assertIsNotNone(scroll)
        self.assertTrue(isinstance(scroll, Gtk.ScrolledWindow))

    def test_do_close_request_handles_none_search_pool(self):
        win = MagicMock()
        win._search_pool = None
        win._unique_active_pool = None
        win._discover_active_pool = None
        win._unique_abort_event = threading.Event()
        win._discover_abort_event = threading.Event()
        win._current_playing_item = None
        win.preview_player = None
        win.mpv = MagicMock()
        win.mpv.playlist = []
        win.inhibit_cookie = 0

        # Should not raise AttributeError when _search_pool is None
        ret = CineWindow.do_close_request(win)
        self.assertFalse(ret)
        self.assertTrue(win._unique_abort_event.is_set())
        self.assertTrue(win._discover_abort_event.is_set())

    def test_do_close_request_shuts_down_active_pools(self):
        win = MagicMock()
        search_pool = MagicMock()
        unique_pool = MagicMock()
        discover_pool = MagicMock()
        win._search_pool = search_pool
        win._unique_active_pool = unique_pool
        win._discover_active_pool = discover_pool
        win._current_playing_item = None
        win.preview_player = None
        win.mpv = MagicMock()
        win.mpv.playlist = []
        win.inhibit_cookie = 0

        ret = CineWindow.do_close_request(win)
        self.assertFalse(ret)
        search_pool.shutdown.assert_called_with(wait=False, cancel_futures=True)
        unique_pool.shutdown.assert_called_with(wait=False, cancel_futures=True)
        discover_pool.shutdown.assert_called_with(wait=False, cancel_futures=True)

    def test_unique_scrolled_policy_never_horizontal(self):
        # Verify unique_scrolled in window.ui has hscrollbar-policy=2 (NEVER) and vscrollbar-policy=1 (AUTOMATIC)
        from pathlib import Path
        ui_content = Path("src/window.ui").read_text()
        self.assertIn('<object class="GtkScrolledWindow" id="unique_scrolled">', ui_content)
        idx = ui_content.find('id="unique_scrolled"')
        chunk = ui_content[idx:idx + 300]
        self.assertIn('<property name="hscrollbar-policy">2</property>', chunk)
        self.assertIn('<property name="vscrollbar-policy">1</property>', chunk)

    def test_deliver_unique_slices_auto_fills_viewport(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = True
        win.has_more_unique = True
        win.unique_flowbox = MagicMock()
        win.unique_flowbox.get_first_child.return_value = None
        win.unique_scrolled = MagicMock()
        mock_adj = MagicMock()
        mock_adj.get_value.return_value = 0
        mock_adj.get_page_size.return_value = 800
        mock_adj.get_upper.return_value = 400  # under-filled
        win.unique_scrolled.get_vadjustment.return_value = mock_adj

        items = [{"id": f"tt{i}", "title": f"Movie {i}"} for i in range(6)]
        idle_callbacks = []
        with patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *args: idle_callbacks.append((fn, args))), \
             patch("src.movie_widget.MovieWidget", side_effect=lambda it, cb: MagicMock()):
            CineWindow._deliver_unique_slices_to_ui(win, items, req_id=1, has_more=True)
            while idle_callbacks:
                fn, args = idle_callbacks.pop(0)
                fn(*args)

            # _on_unique_scroll should have been called to fill the viewport
            win._on_unique_scroll.assert_called_with(mock_adj)

    def test_deliver_unique_slices_shows_empty_language_label(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = True
        win.current_language = "Hindi"
        win.unique_flowbox = Gtk.FlowBox()

        CineWindow._deliver_unique_slices_to_ui(win, [], req_id=1, has_more=False)
        self.assertFalse(win.has_more_unique)
        self.assertFalse(win.is_fetching_unique)
        first_child = win.unique_flowbox.get_first_child()
        self.assertIsNotNone(first_child)
        box = first_child.get_child() if hasattr(first_child, "get_child") else first_child
        lbl = box.get_first_child()
        self.assertIsNotNone(lbl)
        self.assertEqual(lbl.get_label(), "No items found for this language.")

    def test_unique_skips_unsupported_language_catalogs_without_network(self):
        import threading
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = False
        win.has_more_unique = True
        win.current_language = "Hindi"
        win.current_genre = None
        win.current_year = None
        win._unique_abort_event = None
        win._unique_lock = threading.Lock()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.unique_truly_empty_catalogs = set()
        win.unique_productive_catalogs = []
        win.unique_empty_streak = {}
        win.unique_catalog_idx = 0
        win.unique_flowbox = Gtk.FlowBox()

        cinemeta_cat = {
            "addon_name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "catalog_id": "top",
            "catalog_name": "Popular",
            "media_type": "movie",
            "genres": ["Action", "Comedy", "Drama"]
        }
        win.unique_catalogs = [cinemeta_cat]
        win.unique_catalogs_by_key = {
            ("https://v3-cinemeta.strem.io/manifest.json", "top", "movie"): cinemeta_cat
        }

        with patch("src.api.fetch_items") as mock_fetch, \
             patch("src.database.get_cached_catalog", return_value=None):
            CineWindow._load_next_unique_batch(win)
            import time
            time.sleep(0.15)

            mock_fetch.assert_not_called()
            self.assertIn(("https://v3-cinemeta.strem.io/manifest.json", "top", "movie"), win.unique_exhausted_catalogs)

    def test_unique_combined_view_with_search_query_uses_db_then_catalog_search(self):
        """Unique view queries local DB first via query_cache, followed by catalog_search for addons."""
        win = MagicMock()
        win.category_btn_stack.get_visible_child_name.return_value = "movies"
        win._current_discover_type = "movie"
        win._current_discover_addon = None
        win.current_genre = None
        win.current_discover_search_query = "maid"
        win._is_discover_search_title_only.return_value = True

        cat1 = {"catalog_id": "top", "manifest_url": "https://v3-cinemeta.strem.io/manifest.json", "media_type": "movie", "title": "Top"}
        win._get_discover_catalog_list.return_value = [cat1]
        win.current_catalog = cat1

        search_cat = {
            "addon_name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "catalog_id": "top",
            "catalog_name": "Popular",
            "media_type": "movie"
        }
        with patch("src.window.cancel_pending_image_downloads", create=True), \
             patch("src.api.get_available_search_catalogs", return_value=[search_cat]):
            CineWindow._refresh_unique_combined_view(win)

        sources = [c.get("_source") for c in win.unique_catalogs]
        self.assertEqual(sources[0], "query_cache")
        self.assertIn("catalog_search", sources)
        search_entries = [c for c in win.unique_catalogs if c.get("_source") == "catalog_search"]
        self.assertTrue(len(search_entries) >= 1)
        self.assertTrue(search_entries[0].get("title_only"))
        win._load_next_unique_batch.assert_called_once()

    def test_query_cache_meta_search_when_title_only_disabled(self):
        """When Title Only is disabled, SQLite query searches metadata fields (title, name, description, genres)."""
        from src import database
        # Seed our own entry instead of relying on whatever other tests left in
        # the shared cache; the full suite runs against the same database.
        token = "zqxmetasearchtoken"
        seeded = [{
            "id": "tt9900001",
            "type": "movie",
            "name": "Seed Title",
            "title": "Seed Title",
            "description": f"A story about {token}.",
            "genres": ["Drama"],
        }]
        database.save_cached_catalog(f"unit-test-meta-search-{token}", seeded)

        items = database.get_cached_items_by_search(token, title_only=False, limit=60)
        self.assertTrue(len(items) > 0)
        self.assertTrue(
            any(token in str(item.get("description", "")).lower() for item in items),
            "metadata search should match the description",
        )
        # The token is not in any title, so a title-only search must miss it.
        self.assertEqual(
            database.get_cached_items_by_search(token, title_only=True, limit=60), []
        )

    def test_empty_filter_box_has_clear_button(self):
        """Empty filter box includes clear button when active query is set."""
        win = MagicMock()
        win.current_discover_search_query = "virgin"
        win.discover_search_entry = MagicMock()
        empty_box = CineWindow._create_empty_filter_box(win, "No Animation items matching 'virgin'.")
        self.assertIsNotNone(empty_box)
        self.assertTrue(getattr(empty_box, "_is_empty_box", False))
        
        # Verify clear button exists in empty box
        children = []
        ch = empty_box.get_first_child()
        while ch:
            children.append(ch)
            ch = ch.get_next_sibling()
        self.assertEqual(len(children), 2)  # Label + Button
        btn = children[1]
        self.assertIsInstance(btn, Gtk.Button)
        self.assertEqual(btn.get_label(), "Clear Search Filter")

    def test_update_flowbox_empty_state_shows_and_hides(self):
        """_update_flowbox_empty_state reveals empty box when all cards are hidden, removes it when cards are visible."""
        win = MagicMock()
        win.current_discover_search_query = "xyznonexistent"
        win.current_genre = "Animation"
        win.is_fetching_unique = False
        fb = Gtk.FlowBox()
        win.unique_flowbox = fb

        # With 0 cards in flowbox, empty box is added
        CineWindow._update_flowbox_empty_state(win, fb)
        first_ch = fb.get_first_child()
        self.assertIsNotNone(first_ch)
        widget = first_ch.get_child() if hasattr(first_ch, "get_child") else first_ch
        self.assertTrue(getattr(widget, "_is_empty_box", False))

        # With visible card added, empty box is removed
        card = Gtk.Box()
        fb.append(card)
        CineWindow._update_flowbox_empty_state(win, fb)
        has_empty = False
        ch = fb.get_first_child()
        while ch:
            w = ch.get_child() if hasattr(ch, "get_child") else ch
            if getattr(w, "_is_empty_box", False):
                has_empty = True
            ch = ch.get_next_sibling()
        self.assertFalse(has_empty)

    def test_refresh_unique_adds_tmdb_country_catalog(self):
        """Country filters get a TMDb page in Unique too; no provider catalog filters by country."""
        win = MagicMock()
        win.unique_flowbox = Gtk.FlowBox()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.category_btn_stack.get_visible_child_name.return_value = "movies"
        win._current_discover_type = "movie"
        win._current_discover_addon = None
        win.current_country = "India"
        win.current_catalog = None
        win._unique_section_scope = None
        win._get_discover_catalog_list.return_value = []

        with patch("src.window.cancel_pending_image_downloads", create=True):
            CineWindow._refresh_unique_combined_view(win)

        sources = [c.get("_source") for c in win.unique_catalogs]
        self.assertIn("tmdb_country", sources)
        country_cat = next(c for c in win.unique_catalogs if c.get("_source") == "tmdb_country")
        self.assertEqual(country_cat["country"], "India")
        # (manifest_url, catalog_id, media_type) keys page and exhaust sources,
        # so the synthetic rows must not collide with the local-cache row.
        self.assertEqual(country_cat["catalog_id"], "tmdb_country")
        self.assertTrue(country_cat["supports_pagination"])
        self.assertEqual(country_cat["tmdb_total_pages"], 0)
        keys = [(c.get("manifest_url"), c.get("catalog_id"), c.get("media_type")) for c in win.unique_catalogs]
        self.assertEqual(len(keys), len(set(keys)))

    def test_refresh_unique_adds_tmdb_language_catalog(self):
        """A language alone gets its own TMDb page; most addons do not offer one."""
        win = MagicMock()
        win.unique_flowbox = Gtk.FlowBox()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.category_btn_stack.get_visible_child_name.return_value = "movies"
        win._current_discover_type = "movie"
        win._current_discover_addon = None
        win.current_country = None
        win.current_language = "Tamil"
        win.current_catalog = None
        win._unique_section_scope = None
        win._get_discover_catalog_list.return_value = []

        with patch("src.window.cancel_pending_image_downloads", create=True):
            CineWindow._refresh_unique_combined_view(win)

        tmdb_cats = [c for c in win.unique_catalogs if c.get("_source") == "tmdb_country"]
        self.assertEqual(len(tmdb_cats), 1)
        self.assertIsNone(tmdb_cats[0]["country"])
        self.assertEqual(tmdb_cats[0]["language"], "Tamil")

    def test_unique_fetches_tmdb_country_cards(self):
        """The TMDb country page supplies India cards without querying provider catalogs."""
        import threading
        import time
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = False
        win.has_more_unique = True
        win.current_country = "India"
        win.current_genre = None
        win.current_language = None
        win.current_year = None
        win._unique_abort_event = None
        win._unique_lock = threading.Lock()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.unique_truly_empty_catalogs = set()
        win.unique_productive_catalogs = []
        win.unique_empty_streak = {}
        win.unique_catalog_idx = 0
        win.unique_flowbox = Gtk.FlowBox()

        country_cat = {
            "_source": "tmdb_country",
            "addon_name": "",
            "manifest_url": None,
            "catalog_id": "tmdb_country",
            "catalog_name": "India · TMDb",
            "media_type": "movie",
            "country": "India",
            "supports_pagination": True,
            "tmdb_total_pages": 1,
        }
        cat_key = (None, "tmdb_country", "movie")
        win.unique_catalogs = [country_cat]
        win.unique_catalogs_by_key = {cat_key: country_cat}

        cards = [
            {"id": "tmdb:1", "title": "Kantara", "type": "movie", "country": "India"},
            {"id": "tmdb:2", "title": "Jai Bhim", "type": "movie", "country": "India"},
        ]

        idle_calls = []
        with patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *args: idle_calls.append((fn, args))), \
             patch("src.database.get_cached_catalog", return_value=None), \
             patch("src.database.save_cached_catalog") as save_mock, \
             patch("src.tmdb_helper.fetch_country_catalog_page", return_value=(cards, 1)) as fetch_mock, \
             patch("src.api.fetch_items") as provider_fetch:
            CineWindow._load_next_unique_batch(win)
            time.sleep(0.2)

        fetch_mock.assert_called_once()
        self.assertEqual(fetch_mock.call_args.kwargs["country"], "India")
        self.assertEqual(fetch_mock.call_args.kwargs["limit"], 50)
        self.assertEqual(fetch_mock.call_args.kwargs["page"], 1)
        provider_fetch.assert_not_called()
        save_mock.assert_called_once()
        # TMDb reported a single page, so the row is finished and cannot be asked
        # for a page 2.
        self.assertIn(cat_key, win.unique_exhausted_catalogs)
        self.assertEqual(country_cat.get("tmdb_total_pages"), 1)

        delivered = [
            args[0] for fn, args in idle_calls
            if fn == win._append_unique_slice_to_ui and args
        ]
        self.assertEqual(len(delivered), 1)
        self.assertEqual([it["title"] for it in delivered[0]], ["Kantara", "Jai Bhim"])

    def test_unique_browses_tmdb_pages_past_the_first(self):
        """A country with deeper results keeps fetching until TMDb says it is over."""
        import threading
        import time
        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = False
        win.has_more_unique = True
        win.current_country = "India"
        win.current_genre = None
        win.current_language = None
        win.current_year = None
        win._unique_abort_event = None
        win._unique_lock = threading.Lock()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.unique_truly_empty_catalogs = set()
        win.unique_productive_catalogs = []
        win.unique_empty_streak = {}
        win.unique_catalog_idx = 0
        win.unique_flowbox = Gtk.FlowBox()

        country_cat = {
            "_source": "tmdb_country",
            "addon_name": "",
            "manifest_url": None,
            "catalog_id": "tmdb_country",
            "catalog_name": "India · TMDb",
            "media_type": "movie",
            "country": "India",
            "supports_pagination": True,
            "tmdb_total_pages": 0,
        }
        cat_key = (None, "tmdb_country", "movie")
        win.unique_catalogs = [country_cat]
        win.unique_catalogs_by_key = {cat_key: country_cat}

        asked_pages = []

        def fake_page(country=None, page=1, exclude_ids=None, **kwargs):
            asked_pages.append((int(page), set(exclude_ids or ())))
            if page > 2:
                return [], 0
            cards = [{
                "id": f"tmdb:{page}-{i}",
                "title": f"India card {page}-{i}",
                "type": "movie",
                "country": "India",
            } for i in range(50)]
            return cards, 2

        # The real dedupe records the ids a view has shown; the snapshot the next
        # TMDb page is fetched against is built from them.
        def record_item(item):
            win.unique_seen_ids.add(str(item.get("id")))
            return True

        win._is_unique_item = record_item

        idle_calls = []
        with patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *args: idle_calls.append((fn, args))), \
             patch("src.database.get_cached_catalog", return_value=None), \
             patch("src.database.save_cached_catalog"), \
             patch("src.tmdb_helper.fetch_country_catalog_page", side_effect=fake_page), \
             patch("src.api.fetch_items") as provider_fetch:
            CineWindow._load_next_unique_batch(win)
            time.sleep(0.3)
            win.is_fetching_unique = False
            CineWindow._load_next_unique_batch(win)
            time.sleep(0.3)
            win.is_fetching_unique = False
            CineWindow._load_next_unique_batch(win)
            time.sleep(0.3)

        provider_fetch.assert_not_called()
        # The first page has nothing to exclude; each later batch advances the
        # cursor and is fetched against the ids the view already rendered.
        self.assertEqual([page for page, _seen in asked_pages], [1, 2])
        self.assertEqual(asked_pages[0][1], set())
        self.assertEqual(len(asked_pages[1][1]), 50)
        # TMDb reported two pages, so the third batch is refused from the row's
        # cursor without spending another request.
        self.assertEqual(country_cat["tmdb_total_pages"], 2)
        self.assertIn(cat_key, win.unique_exhausted_catalogs)


    def test_unique_lists_cached_cards_before_network_cards(self):
        """What this machine already has reaches the grid first; a catalog that has
        to be fetched is appended behind it instead of cutting in."""
        import threading
        import time

        win = MagicMock()
        win.unique_request_id = 1
        win.is_fetching_unique = False
        win.has_more_unique = True
        win.current_country = None
        win.current_genre = None
        win.current_language = None
        win.current_year = None
        win._unique_abort_event = None
        win._unique_lock = threading.Lock()
        win.unique_seen_ids = set()
        win.unique_seen_keys = set()
        win.unique_page_per_catalog = {}
        win.unique_exhausted_catalogs = set()
        win.unique_truly_empty_catalogs = set()
        win.unique_productive_catalogs = []
        win.unique_empty_streak = {}
        win.unique_catalog_idx = 0
        win.unique_flowbox = Gtk.FlowBox()

        cached_cat = {
            "addon_name": "Cached Addon",
            "manifest_url": "https://cached.test/manifest.json",
            "catalog_id": "top",
            "catalog_name": "Saved Page",
            "media_type": "movie",
        }
        network_cat = {
            "addon_name": "Slow Addon",
            "manifest_url": "https://slow.test/manifest.json",
            "catalog_id": "top",
            "catalog_name": "Live Page",
            "media_type": "movie",
        }
        win.unique_catalogs = [network_cat, cached_cat]
        win.unique_catalogs_by_key = {
            ("https://cached.test/manifest.json", "top", "movie"): cached_cat,
            ("https://slow.test/manifest.json", "top", "movie"): network_cat,
        }
        stored_cards = [
            {"id": "tt:cached1", "title": "Cached One", "type": "movie"},
            {"id": "tt:cached2", "title": "Cached Two", "type": "movie"},
        ]
        live_cards = [
            {"id": "tt:live1", "title": "Fetched One", "type": "movie"},
            {"id": "tt:live2", "title": "Fetched Two", "type": "movie"},
        ]

        def fake_cached_catalog(cache_key, *args, **kwargs):
            # The catalog cache answers one of the two catalogs; the other one has
            # to go to the network, which answers slowly.
            if "cached.test" in cache_key:
                return list(stored_cards)
            return None

        def slow_fetch(*args, **kwargs):
            time.sleep(0.4)
            return list(live_cards)

        idle_calls = []
        with patch("gi.repository.GLib.idle_add",
                   side_effect=lambda fn, *args: idle_calls.append((fn, args))), \
             patch("src.database.get_cached_catalog", side_effect=fake_cached_catalog), \
             patch("src.database.save_cached_catalog"), \
             patch("src.database.hydrate_items_from_metadata"), \
             patch("src.api.hydrate_missing_catalog_items"), \
             patch("src.api.fetch_items", side_effect=slow_fetch):
            CineWindow._load_next_unique_batch(win)
            time.sleep(1.2)

        delivered = [
            [it["title"] for it in args[0]]
            for fn, args in idle_calls
            if fn == win._append_unique_slice_to_ui and args
        ]
        self.assertEqual(delivered[0], ["Cached One", "Cached Two"])
        self.assertIn(["Fetched One", "Fetched Two"], delivered)
        self.assertLess(
            delivered.index(["Cached One", "Cached Two"]),
            delivered.index(["Fetched One", "Fetched Two"]),
            "a network slice was listed before the cached one",
        )


if __name__ == "__main__":
    unittest.main()




