import unittest
import tempfile
import os
import sys
from pathlib import Path
import subprocess
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

from src.window import CineWindow
from src import tmdb_helper, api, database


class TestHomeAndDiscoverDropdowns(unittest.TestCase):
    def setUp(self):
        self.win = MagicMock()
        self.win.category_btn_stack = Gtk.Stack()
        self.win.library_stack = Gtk.Stack()
        self.win.library_sort_dropdown = Gtk.DropDown()
        self.win.library_genre_dropdown = Gtk.DropDown()
        self.win.library_unique_check = Gtk.CheckButton()
        self.win.discover_back_box = Gtk.Box()
        self.win.discover_grid_title = Gtk.Label()
        self.win.content_flowbox = Gtk.FlowBox()
        self.win._syncing_sort_dropdown = False
        self.win._syncing_genre_dropdown = False
        self.win.current_sort = "Popular"
        self.win.current_year = None
        self.win.current_language = None
        self.win.current_genre = None
        self.win.current_media_type = "movie"
        self.win._current_discover_type = "all"
        self.win.current_catalog = {"catalog_id": "top", "manifest_url": "https://v3-cinemeta.strem.io/manifest.json", "display_name": "Popular Movies"}
        self.win._is_unique_mode_active = MagicMock(return_value=False)
        self.win._cancel_all_background_fetching = MagicMock()
        self.win._refresh_content = MagicMock()
        self.win._refresh_unique_combined_view = MagicMock()
        self.win._refresh_discover_page = MagicMock()
        self.win._update_genre_dropdown = lambda media_type=None: CineWindow._update_genre_dropdown(self.win, media_type)
        self.win._apply_sort_to_current_catalog = lambda: CineWindow._apply_sort_to_current_catalog(self.win)

    def test_dropdown_1_options_contain_primary_filters(self):
        """Dropdown 1 should contain Popular, Recent, Featured, Trending, Year, Language, Genre."""
        CineWindow._update_sort_dropdown(self.win, "movie")
        model = self.win.library_sort_dropdown.get_model()
        items = [model.get_string(i) for i in range(model.get_n_items())]
        
        self.assertIn("Popular", items)
        self.assertIn("Recent", items)
        self.assertIn("Featured", items)
        self.assertIn("Trending", items)
        self.assertIn("Year", items)
        self.assertIn("Language", items)
        self.assertIn("Genre", items)

    def test_context_aware_dropdown_2_shows_years_when_sort_is_year(self):
        """When Dropdown 1 is Year, Dropdown 2 shows years list."""
        self.win.current_sort = "Year"
        self.win.current_year = "2024"
        CineWindow._update_genre_dropdown(self.win, "movie")
        
        model = self.win.library_genre_dropdown.get_model()
        items = [model.get_string(i) for i in range(model.get_n_items())]
        
        self.assertEqual(items[0], "All Years")
        self.assertIn("2026", items)
        self.assertIn("2025", items)
        self.assertIn("2024", items)
        self.assertIn("2020", items)
        self.assertNotIn("Action", items)
        self.assertNotIn("English", items)

        selected_idx = self.win.library_genre_dropdown.get_selected()
        self.assertEqual(model.get_string(selected_idx), "2024")

    def test_context_aware_dropdown_2_shows_languages_when_sort_is_language(self):
        """When Dropdown 1 is Language, Dropdown 2 shows languages list."""
        self.win.current_sort = "Language"
        self.win.current_language = "Spanish"
        CineWindow._update_genre_dropdown(self.win, "movie")
        
        model = self.win.library_genre_dropdown.get_model()
        items = [model.get_string(i) for i in range(model.get_n_items())]
        
        self.assertIn("English", items)
        self.assertIn("Hindi", items)
        self.assertIn("Spanish", items)
        self.assertIn("Japanese", items)
        self.assertNotIn("Action", items)
        self.assertNotIn("2024", items)

        selected_idx = self.win.library_genre_dropdown.get_selected()
        self.assertEqual(model.get_string(selected_idx), "Spanish")

    def test_context_aware_dropdown_2_shows_genres_when_sort_is_popular(self):
        """When Dropdown 1 is Popular, Dropdown 2 shows genres with All Genres."""
        self.win.current_sort = "Popular"
        self.win.current_genre = "Action"
        CineWindow._update_genre_dropdown(self.win, "movie")
        
        model = self.win.library_genre_dropdown.get_model()
        items = [model.get_string(i) for i in range(model.get_n_items())]
        
        self.assertIn("All Genres", items)
        self.assertIn("Action", items)
        self.assertIn("Comedy", items)
        self.assertNotIn("2024", items)

        selected_idx = self.win.library_genre_dropdown.get_selected()
        self.assertEqual(model.get_string(selected_idx), "Action")

    def test_selecting_all_years_in_dropdown_2_updates_catalog_and_refreshes(self):
        """Selecting All Years in Dropdown 2 sets current_year to None and refreshes content grid."""
        self.win.current_sort = "Year"
        dropdown = Gtk.DropDown.new_from_strings(["All Years", "2026", "2025", "2024"])
        dropdown.set_selected(0)  # "All Years"
        
        CineWindow._on_library_genre_changed(self.win, dropdown, None)
        
        self.assertIsNone(self.win.current_year)
        self.assertEqual(self.win.current_catalog["catalog_id"], "year")
        self.assertIn("All Years", self.win.current_catalog["display_name"])
        self.win._refresh_content.assert_called_once()

    def test_selecting_year_in_dropdown_2_updates_catalog_and_refreshes(self):
        """Selecting a year in Dropdown 2 sets current_year and refreshes content grid."""
        self.win.current_sort = "Year"
        dropdown = Gtk.DropDown.new_from_strings(["All Years", "2026", "2025", "2024"])
        dropdown.set_selected(3)  # "2024"
        
        CineWindow._on_library_genre_changed(self.win, dropdown, None)
        
        self.assertEqual(self.win.current_year, "2024")
        self.assertEqual(self.win.current_catalog["catalog_id"], "year")
        self.assertIn("2024", self.win.current_catalog["display_name"])
        self.win._refresh_content.assert_called_once()

    def test_selecting_language_in_dropdown_2_updates_catalog_and_refreshes(self):
        """Selecting a language in Dropdown 2 sets current_language and refreshes content grid."""
        self.win.current_sort = "Language"
        dropdown = Gtk.DropDown.new_from_strings(["English", "Hindi", "French"])
        dropdown.set_selected(1)  # "Hindi"
        
        CineWindow._on_library_genre_changed(self.win, dropdown, None)
        
        self.assertEqual(self.win.current_language, "Hindi")
        self.assertEqual(self.win.current_genre, "Hindi")
        self.assertIn("Hindi", self.win.current_catalog["display_name"])
        self.win._refresh_content.assert_called_once()

    def test_selecting_year_in_dropdown_1_switches_dropdown_2_to_years(self):
        """When user selects 'Year' in Dropdown 1, Dropdown 2 is updated to years starting with All Years."""
        dropdown = Gtk.DropDown.new_from_strings(["Popular", "Recent", "Featured", "Trending", "Year", "Language"])
        dropdown.set_selected(4)  # "Year"
        
        CineWindow._on_library_sort_changed(self.win, dropdown, None)
        
        self.assertEqual(self.win.current_sort, "Year")
        # Verify Dropdown 2 now contains All Years and descending years
        model2 = self.win.library_genre_dropdown.get_model()
        items2 = [model2.get_string(i) for i in range(model2.get_n_items())]
        self.assertEqual(items2[0], "All Years")
        self.assertIn("2026", items2)
        self.assertIn("2025", items2)
        self.assertIn("2024", items2)
        self.win._refresh_content.assert_called_once()

    def test_recent_sort_logic_with_and_without_genre(self):
        """Recent sort with a genre uses 'top' catalog; without a genre uses 'year' catalog."""
        # 1. Recent without genre
        self.win.current_sort = "Recent"
        self.win.current_genre = None
        self.win.current_media_type = "movie"
        CineWindow._apply_sort_to_current_catalog(self.win)
        self.assertEqual(self.win.current_catalog["catalog_id"], "year")
        self.assertIn("Recent Movies", self.win.current_catalog["display_name"])

        # 2. Recent with genre
        self.win.current_genre = "Action"
        CineWindow._apply_sort_to_current_catalog(self.win)
        self.assertEqual(self.win.current_catalog["catalog_id"], "top")
        self.assertIn("Recent Movies", self.win.current_catalog["display_name"])

    def test_home_active_btn_contains_unique_view_menu_item(self):
        """Home menu button model includes the toggle-unique item."""
        self.win.home_active_btn = Gtk.MenuButton()
        self.win.discover_active_btn = Gtk.MenuButton()
        self.win.movies_active_btn = Gtk.MenuButton()
        self.win.series_active_btn = Gtk.MenuButton()
        self.win.anime_active_btn = Gtk.MenuButton()
        self.win._schedule_deferred_menu_build = MagicMock()
        self.win._build_discover_menu = MagicMock(return_value=Gio.Menu.new())

        CineWindow._init_default_category_menus(self.win)
        model = self.win.home_active_btn.get_menu_model()
        self.assertIsNotNone(model)
        
        # Check that one of the items targets win.toggle-unique
        found_unique = False
        for i in range(model.get_n_items()):
            action_val = model.get_item_attribute_value(i, Gio.MENU_ATTRIBUTE_ACTION)
            if action_val and action_val.get_string() == "win.toggle-unique":
                found_unique = True
                break
        self.assertTrue(found_unique)

    def test_tmdb_standalone_metadata_enrichment(self):
        """Verify fetch_tmdb_standalone_metadata fallback returns valid metadata structure."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'''{
                "id": 550,
                "title": "Fight Club",
                "overview": "An insomniac office worker...",
                "poster_path": "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg",
                "backdrop_path": "/hZkgoQYus5vegHoetLkCJzb17zJ.jpg",
                "vote_average": 8.433,
                "release_date": "1999-10-15",
                "runtime": 139,
                "genres": [{"id": 18, "name": "Drama"}, {"id": 53, "name": "Thriller"}]
            }'''
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            
            meta = tmdb_helper.fetch_tmdb_standalone_metadata("tt0137523", "movie", "Fight Club")
            self.assertIsNotNone(meta)
            self.assertEqual(meta["title"], "Fight Club")
            self.assertEqual(meta["year"], "1999")
            self.assertIn("Drama", meta["genres"])
            self.assertIn("Thriller", meta["genres"])
            self.assertIn("insomniac", meta["description"])
            self.assertEqual(meta["runtime"], "139 min")

    def test_content_scroll_triggers_fetch_when_unfilled_viewport_no_scrollbar(self):
        """When cards do not fill the window (upper <= page_size + 50), trigger _fetch_content_page to get scrollbar."""
        self.win.is_fetching_content = False
        self.win.has_more_content = True
        self.win.current_catalog = {"catalog_id": "top"}
        self.win._fetch_content_page = MagicMock()
        self.win.library_stack.get_visible_child_name = MagicMock(return_value="content")

        adj = MagicMock()
        adj.get_value.return_value = 0
        adj.get_page_size.return_value = 1100  # large window viewport height
        adj.get_upper.return_value = 800       # cards only take 800px (no scrollbar)

        CineWindow._on_content_scroll(self.win, adj)
        self.win._fetch_content_page.assert_called_once()

    def test_content_scroll_triggers_fetch_when_near_bottom(self):
        """When user scrolls near the bottom of cards, trigger _fetch_content_page."""
        self.win.is_fetching_content = False
        self.win.has_more_content = True
        self.win.current_catalog = {"catalog_id": "top"}
        self.win._fetch_content_page = MagicMock()
        self.win.library_stack.get_visible_child_name = MagicMock(return_value="content")

        adj = MagicMock()
        adj.get_value.return_value = 1200
        adj.get_page_size.return_value = 1000
        adj.get_upper.return_value = 2400  # val + page_size (2200) >= upper - 500 (1900)

        CineWindow._on_content_scroll(self.win, adj)
        self.win._fetch_content_page.assert_called_once()

    def test_content_scroll_does_not_trigger_when_viewport_filled_and_not_near_bottom(self):
        """When cards fill viewport with scrollbar and user is at top, do not trigger fetch."""
        self.win.is_fetching_content = False
        self.win.has_more_content = True
        self.win.current_catalog = {"catalog_id": "top"}
        self.win._fetch_content_page = MagicMock()
        self.win.library_stack.get_visible_child_name = MagicMock(return_value="content")

        adj = MagicMock()
        adj.get_value.return_value = 0
        adj.get_page_size.return_value = 1000
        adj.get_upper.return_value = 2500  # upper > page_size + 50, val + page_size (1000) < 2000

        CineWindow._on_content_scroll(self.win, adj)
        self.win._fetch_content_page.assert_not_called()

    def test_database_get_cached_items_by_genre(self):
        """Test retrieving cached items by genre from database."""
        # Insert a sample item into database cache
        sample_item = {
            "id": "tt_test_anim_123",
            "title": "Test Animated Movie",
            "year": "2024",
            "type": "movie",
            "genres": ["Animation", "Family"]
        }
        database.save_cached_catalog("discover:test_cat:p1", [sample_item])
        
        results = database.get_cached_items_by_genre("Animation", media_type="movie")
        self.assertTrue(any(it.get("id") == "tt_test_anim_123" for it in results))

    def test_database_get_cached_items_by_year(self):
        """Test retrieving cached items by release year and media_type filtering."""
        movie_item = {
            "id": "tt_test_yr_mov_2025",
            "title": "Test 2025 Movie",
            "year": "2025",
            "type": "movie",
            "genres": ["Sci-Fi"]
        }
        series_item = {
            "id": "tt_test_yr_ser_2025",
            "title": "Test 2025 Series",
            "year": "2025",
            "type": "series",
            "genres": ["Drama"]
        }
        database.save_cached_catalog("discover:test_year_cat:p1", [movie_item, series_item])

        all_2025 = database.get_cached_items_by_year("2025")
        self.assertTrue(any(it.get("id") == "tt_test_yr_mov_2025" for it in all_2025))
        self.assertTrue(any(it.get("id") == "tt_test_yr_ser_2025" for it in all_2025))

        movie_2025 = database.get_cached_items_by_year("2025", media_type="movie")
        self.assertTrue(any(it.get("id") == "tt_test_yr_mov_2025" for it in movie_2025))
        self.assertFalse(any(it.get("id") == "tt_test_yr_ser_2025" for it in movie_2025))

        series_2025 = database.get_cached_items_by_year("2025", media_type="series")
        self.assertTrue(any(it.get("id") == "tt_test_yr_ser_2025" for it in series_2025))
        self.assertFalse(any(it.get("id") == "tt_test_yr_mov_2025" for it in series_2025))

    def test_api_filter_items_by_year(self):
        """Test filter_items_by_genre filters correctly when given a 4-digit year."""
        from src import api
        items = [
            {"id": "1", "title": "Film A", "year": "2025", "type": "movie"},
            {"id": "2", "title": "Film B", "releaseInfo": "2025-05-12", "type": "movie"},
            {"id": "3", "title": "Film C", "year": "2024", "type": "movie"},
        ]
        res = api.filter_items_by_genre(items, "2025")
        self.assertEqual(len(res), 2)
        self.assertEqual({it["id"] for it in res}, {"1", "2"})

    def test_api_catalog_supports_year(self):
        """Test catalog_supports_genre with year extra."""
        from src import api
        cat = {
            "id": "year",
            "name": "Year",
            "extra": [{"name": "genre", "options": ["2026", "2025", "2024"]}]
        }
        self.assertTrue(api.catalog_supports_genre(cat, "2025"))
        self.assertFalse(api.catalog_supports_genre(cat, "1950"))

    def test_year_sort_label_discover_and_movies(self):
        """Test _apply_sort_to_current_catalog sets proper title for Discover and Movies tabs."""
        # 1. Discover tab
        self.win.current_sort = "Year"
        self.win.current_year = "2025"
        self.win.current_media_type = "movie"
        self.win._current_discover_type = "all"
        self.win.category_btn_stack = MagicMock()
        self.win.category_btn_stack.get_visible_child_name.return_value = "discover"

        CineWindow._apply_sort_to_current_catalog(self.win)
        self.assertEqual(self.win.current_catalog["catalog_id"], "year")
        self.assertEqual(self.win.current_catalog["display_name"], "Discover — 2025")

        # 2. Movies tab
        self.win._current_discover_type = "movie"
        self.win.category_btn_stack.get_visible_child_name.return_value = "movies"

        CineWindow._apply_sort_to_current_catalog(self.win)
        self.assertEqual(self.win.current_catalog["catalog_id"], "year")
        self.assertEqual(self.win.current_catalog["display_name"], "Movies — 2025")

    def test_discover_scroll_threshold_requires_scroll(self):
        """Test that _on_discover_scroll only triggers when scrolled near bottom."""
        self.win.library_stack = MagicMock()
        self.win.library_stack.get_visible_child_name.return_value = "discover"
        self.win._discover_is_loading_rows = False
        self.win._discover_catalog_list = [{"title": f"Cat {i}"} for i in range(20)]
        self.win._discover_next_row_index = 6
        self.win._load_next_discover_batch = MagicMock()

        adj = MagicMock()
        # Case 1: val=0, upper <= page_size (no scrollable content)
        adj.get_value.return_value = 0.0
        adj.get_page_size.return_value = 600.0
        adj.get_upper.return_value = 600.0
        CineWindow._on_discover_scroll(self.win, adj)
        self.win._load_next_discover_batch.assert_not_called()

        # Case 2: val=0, upper=2000 (at top of tall page)
        adj.get_upper.return_value = 2000.0
        CineWindow._on_discover_scroll(self.win, adj)
        self.win._load_next_discover_batch.assert_not_called()

        # Case 3: val=1500, page_size=600, upper=2000 (scrolled near bottom: 1500+600=2100 >= 2000-600)
        adj.get_value.return_value = 1500.0
        CineWindow._on_discover_scroll(self.win, adj)
        self.win._load_next_discover_batch.assert_called_once()

    def test_discover_cache_hit_does_not_fetch_rows(self):
        """Test that switching back to a cached discover view does not trigger network fetches."""
        self.win._discover_views = {}
        view_key = "all|all|all|all|Popular"
        cached_box = MagicMock()
        self.win._discover_views[view_key] = {
            "box": cached_box,
            "catalog_list": [{"title": "Cat 1"}],
            "next_row_index": 1,
            "request_id": 1
        }
        self.win.discover_scrolled = MagicMock()
        self.win.discover_scrolled.get_child.return_value = None
        self.win._load_next_discover_batch = MagicMock()
        self.win._update_continue_watching_section = MagicMock()

        CineWindow._refresh_discover_page(self.win, filter_media_type="all", filter_addon_url=None, force_refresh=False)

        self.win.discover_scrolled.set_child.assert_called_once_with(cached_box)
        self.win._load_next_discover_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
