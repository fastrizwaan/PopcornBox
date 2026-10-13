import unittest
from unittest.mock import MagicMock, patch
import sys
import tempfile
import os
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


if __name__ == "__main__":
    unittest.main()

