import unittest
import tempfile
import os
import sys
import time
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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_base_dir = database.BASE_DIR
        self.orig_db_file = database.DB_FILE
        self.orig_cache_conn = database._cache_conn
        self.orig_cache_initialized = database._cache_db_initialized

        database.CONFIG_DIR = Path(self.temp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database.reset_json_cache()
        with database._cache_db_lock:
            database._cache_conn = None
            database._cache_db_initialized = False
        api.reset_addon_health_cache()

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
        self.win._current_discover_addon = None
        self.win.current_catalog = {"catalog_id": "top", "manifest_url": "https://v3-cinemeta.strem.io/manifest.json", "display_name": "Popular Movies"}
        self.win._content_auto_fill_count = 0
        self.win._is_unique_mode_active = MagicMock(return_value=False)
        self.win._cancel_all_background_fetching = MagicMock()
        self.win._refresh_content = MagicMock()
        self.win._refresh_unique_combined_view = MagicMock()
        self.win._refresh_discover_page = MagicMock()
        self.win._get_discover_view_key = lambda: CineWindow._get_discover_view_key(self.win)
        self.win._update_genre_dropdown = lambda media_type=None: CineWindow._update_genre_dropdown(self.win, media_type)
        self.win._apply_sort_to_current_catalog = lambda: CineWindow._apply_sort_to_current_catalog(self.win)

    def tearDown(self):
        # Background fetchers hold _cache_db_lock while running statements;
        # closing the connection underneath them segfaults libsqlite3.
        with database._cache_db_lock:
            if database._cache_conn is not None:
                try:
                    database._cache_conn.close()
                except Exception:
                    pass
            database._cache_conn = self.orig_cache_conn
            database._cache_db_initialized = self.orig_cache_initialized
        database.CONFIG_DIR = self.orig_config_dir
        database.BASE_DIR = self.orig_base_dir
        database.DB_FILE = self.orig_db_file
        database.reset_json_cache()
        self.temp_dir.cleanup()
        api.reset_addon_health_cache()

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
        mock_data = {
            "id": 550,
            "title": "Fight Club",
            "overview": "An insomniac office worker...",
            "poster_path": "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg",
            "backdrop_path": "/hZkgoQYus5vegHoetLkCJzb17zJ.jpg",
            "vote_average": 8.433,
            "release_date": "1999-10-15",
            "runtime": 139,
            "genres": [{"id": 18, "name": "Drama"}, {"id": 53, "name": "Thriller"}]
        }
        with patch("src.tmdb_helper.resolve_to_tmdb_id", return_value="550"), \
             patch("src.tmdb_helper._get_cached_request", return_value=mock_data):
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
        view_key = CineWindow._get_discover_view_key(self.win)
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


class TestCountryCatalogFetch(unittest.TestCase):
    def test_fetch_country_catalog_queries_tmdb_origin_country(self):
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            if "/discover/movie?" in url:
                return {"results": [{
                    "id": 11, "title": "Indian Movie", "poster_path": "/m.jpg",
                    "release_date": "2024-03-01", "vote_average": 7.4,
                    "genre_ids": [18], "popularity": 5.0, "original_language": "hi",
                }]}
            return {"results": [{
                "id": 22, "name": "Indian Show", "poster_path": "/s.jpg",
                "first_air_date": "2023-06-01", "vote_average": 8.1,
                "genre_ids": [18], "popularity": 9.0, "original_language": "hi",
            }]}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items = tmdb_helper.fetch_country_catalog(
                "India", media_type="all", genre="Drama", year="2024", sort_mode="Recent", limit=36
            )

        country_pages = [u for u in calls if "with_origin_country=IN" in u]
        self.assertEqual(len(country_pages), 2)
        for url in calls:
            self.assertIn("with_genres=18", url)
        movie_url = next(u for u in country_pages if "/discover/movie?" in u)
        tv_url = next(u for u in country_pages if "/discover/tv?" in u)
        self.assertIn("primary_release_year=2024", movie_url)
        self.assertIn("first_air_date_year=2024", tv_url)

        self.assertEqual({it["type"] for it in items}, {"movie", "series"})
        for it in items:
            self.assertEqual(it["country"], "India")
            self.assertEqual(it["countries"], ["India"])
            self.assertTrue(it["id"].startswith("tmdb:"))
            self.assertTrue(it["poster"].startswith("https://image.tmdb.org/"))

    def test_fetch_country_catalog_fans_out_over_multi_language_countries(self):
        """India's titles are labelled per cinema, so language pages complement the origin-country page."""
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            return {"results": []}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            tmdb_helper.fetch_country_catalog("India", media_type="all")

        by_country = [u for u in calls if "with_origin_country=IN" in u]
        by_language = [u for u in calls if "with_origin_country" not in u]
        self.assertEqual(len(by_country), 2)
        self.assertEqual(len(by_language), 6)
        self.assertEqual(
            sorted({u.split("with_original_language=")[1][:2] for u in by_language}),
            ["hi", "ta", "te"],
        )
        for url in by_language:
            self.assertNotIn("with_origin_country", url)

    def test_fetch_country_catalog_skips_fanout_for_single_language_country(self):
        """Japanese titles are already complete through with_origin_country, so no extra pages."""
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            return {"results": []}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            tmdb_helper.fetch_country_catalog("Japan", media_type="all")

        self.assertEqual(len(calls), 2)
        for url in calls:
            self.assertIn("with_origin_country=JP", url)
            self.assertNotIn("with_original_language", url)

    def test_fetch_country_catalog_unknown_country_makes_no_request(self):
        with patch.object(tmdb_helper, "_get_cached_request") as request:
            items = tmdb_helper.fetch_country_catalog("Wakanda", media_type="all")
        self.assertEqual(items, [])
        request.assert_not_called()

    def test_fetch_country_catalog_page_deepens_instead_of_fanning_out(self):
        """Page 2 asks the primary query once per media type and reports the depth."""
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            return {"results": [], "total_pages": 47}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page("India", media_type="all", page=2)

        self.assertEqual(items, [])
        self.assertEqual(total_pages, tmdb_helper._MAX_DISCOVER_PAGES)
        self.assertEqual(len(calls), 2)
        for url in calls:
            self.assertIn("page=2", url)
            self.assertIn("with_origin_country=IN", url)
            self.assertNotIn("with_original_language", url)

    def test_fetch_country_catalog_page_stops_at_the_depth_cap(self):
        """Browsing is capped so a long scroll cannot turn into a load test on TMDb."""
        with patch.object(tmdb_helper, "_get_cached_request") as request:
            result = tmdb_helper.fetch_country_catalog_page(
                "India", media_type="all", page=tmdb_helper._MAX_DISCOVER_PAGES + 1
            )
        self.assertEqual(result, ([], 0))
        request.assert_not_called()

    def test_fetch_country_catalog_page_browses_past_the_old_five_page_cap(self):
        """A long scroll keeps fetching; five pages x 36 cards used to be the ceiling."""
        calls = []

        def fake_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, *args, **kwargs):
            if cache_only:
                return None
            calls.append(url)
            return {"results": [{
                "id": 100 + len(calls), "title": f"Deep {len(calls)}", "poster_path": "/d.jpg",
                "release_date": "2024-01-01", "original_language": "hi",
            }], "total_pages": 30}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page("Japan", media_type="movie", page=8)

        self.assertEqual(len(calls), 1)
        self.assertIn("page=8", calls[0])
        self.assertTrue(items)
        self.assertEqual(total_pages, tmdb_helper._MAX_DISCOVER_PAGES)

    def test_fetch_country_catalog_page_never_re_requests_a_cached_page(self):
        """A page the filter already has is served from disk and stays off the wire."""
        asked = []

        def fake_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, *args, **kwargs):
            asked.append((url, cache_only))
            if cache_only and "/discover/tv?" in url:
                return {"results": [{
                    "id": 9, "name": "Saved Show", "poster_path": "/s.jpg",
                    "first_air_date": "2023-05-01", "original_language": "ta", "popularity": 4.0,
                }], "total_pages": 2}
            if cache_only:
                return None
            if "/discover/movie?" in url:
                return {"results": [{
                    "id": 5, "title": "Fresh Movie", "poster_path": "/m.jpg",
                    "release_date": "2024-04-01", "original_language": "hi", "popularity": 9.0,
                }], "total_pages": 2}
            return {"results": [], "total_pages": 2}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page("India", media_type="all", page=2)

        # Both media types answer, whichever way their page arrived.
        self.assertEqual({it["id"] for it in items}, {"tmdb:9", "tmdb:5"})
        # The cached TV page is never fetched again; the movie page, which was
        # missing, is asked for exactly once.
        self.assertNotIn("/discover/tv?", [url for url, cache_only in asked if not cache_only])
        self.assertEqual(len([url for url, cache_only in asked
                              if not cache_only and "/discover/movie?" in url]), 1)

    def test_fetch_country_catalog_page_with_nothing_to_fetch_stays_off_the_network(self):
        """A filter the user has already browsed fills the row without one request."""
        asked = []

        def fake_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, *args, **kwargs):
            asked.append(cache_only)
            if not cache_only:
                return None
            return {"results": [{
                "id": 1, "title": "Stored", "poster_path": "/a.jpg",
                "release_date": "2024-01-01", "original_language": "hi", "popularity": 3.0,
            }], "total_pages": 3}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page("India", media_type="all", page=1)

        self.assertTrue(asked, "the cache was never consulted")
        self.assertNotIn(False, asked, "a fully cached filter still went out to TMDb")
        self.assertEqual([it["id"] for it in items], ["tmdb:1"])

    def test_a_year_old_catalog_row_still_fills_the_row(self):
        """Cards already stored are shown forever: a row from last year renders
        without a request, which is the point of keeping them."""
        key = "discover:https://v3-cinemeta.strem.io/manifest.json:top:movie:row36"
        database.save_cached_catalog(key, [{
            "id": "tmdb:1", "title": "Stored last year",
            "medium_cover_image": "https://image.tmdb.org/t/p/w300/a.jpg",
        }])
        with database._cache_db_lock:
            conn = database._get_cache_db()
            conn.execute("UPDATE catalog_cache SET updated_at = ? WHERE cache_key = ?",
                         (time.time() - 400 * 24 * 3600, key))
            conn.commit()
        with database._MEM_CATALOG_LOCK:
            database._MEM_CATALOG_CACHE.clear()

        def fail(url, *args, **kwargs):
            raise AssertionError("a stored catalog row went out to the network")

        with patch.object(api, "_get_cached_request", side_effect=fail):
            rows = database.get_cached_catalog(key, max_age_hours=database.CATALOG_CACHE_HOURS)

        self.assertEqual([row["id"] for row in (rows or [])], ["tmdb:1"])

    def test_fetch_country_catalog_pages_are_read_without_an_expiry(self):
        """Discover rows are card metadata: a page stored last month is as good as
        one stored a minute ago, so no read of this path may declare an expiry."""
        seen_ttl = []

        def fake_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, *args, **kwargs):
            seen_ttl.append(max_age_hours)
            return {"results": [{
                "id": 1, "title": "Stored", "poster_path": "/a.jpg",
                "release_date": "2024-01-01", "original_language": "hi", "popularity": 3.0,
            }], "total_pages": 3}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, _ = tmdb_helper.fetch_country_catalog_page("India", media_type="all", page=1)

        self.assertTrue(seen_ttl, "the fan-out never read the cache")
        self.assertEqual(set(seen_ttl), {api.METADATA_CACHE_HOURS},
                         "a discover page was read with an expiry, so the same cards "
                         "would be asked of TMDb again once it lapsed")

    def test_fetch_country_catalog_page_skips_ids_the_view_already_showed(self):
        """Deeper pages return new titles, not the ones the first page rendered."""
        def fake_request(url, *args, **kwargs):
            return {"results": [
                {"id": 1, "title": "Seen", "poster_path": "/a.jpg", "release_date": "2024-01-01", "original_language": "hi"},
                {"id": 2, "title": "Fresh", "poster_path": "/b.jpg", "release_date": "2024-02-01", "original_language": "ta"},
            ], "total_pages": 3}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page(
                "India", media_type="movie", page=2, exclude_ids={"tmdb:1"}
            )
        self.assertEqual([it["id"] for it in items], ["tmdb:2"])
        self.assertEqual(total_pages, 3)
        self.assertEqual(items[0]["language"], "Tamil")

    def test_fetch_country_catalog_page_handles_language_without_country(self):
        """A language alone is a complete discover query; the country field stays empty."""
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            return {"results": [
                {"id": 7, "title": "Tamil Film", "poster_path": "/t.jpg", "release_date": "2024-05-05", "original_language": "ta"},
            ], "total_pages": 1}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            items, total_pages = tmdb_helper.fetch_country_catalog_page(language="Tamil", media_type="movie")

        self.assertEqual(len(calls), 1)
        self.assertIn("with_original_language=ta", calls[0])
        self.assertNotIn("with_origin_country", calls[0])
        self.assertEqual(total_pages, 1)
        self.assertEqual(items[0]["country"], "")
        self.assertEqual(items[0]["language"], "Tamil")

    def test_fetch_country_catalog_page_keeps_both_filters_for_country_and_language(self):
        """India + Tamil is an AND query; dropping the country would show every Tamil title's origin."""
        calls = []

        def fake_request(url, *args, **kwargs):
            calls.append(url)
            return {"results": [], "total_pages": 1}

        with patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_request):
            tmdb_helper.fetch_country_catalog_page(country="India", language="Tamil", media_type="movie")

        self.assertEqual(len(calls), 1)
        self.assertIn("with_origin_country=IN", calls[0])
        self.assertIn("with_original_language=ta", calls[0])

    def test_fetch_country_catalog_page_without_filter_makes_no_request(self):
        with patch.object(tmdb_helper, "_get_cached_request") as request:
            result = tmdb_helper.fetch_country_catalog_page(media_type="movie")
        self.assertEqual(result, ([], 0))
        request.assert_not_called()

    def test_get_country_iso_code(self):
        self.assertEqual(database.get_country_iso_code("India"), "IN")
        self.assertEqual(database.get_country_iso_code("us"), "US")
        self.assertEqual(database.get_country_iso_code("United Kingdom"), "GB")
        self.assertIsNone(database.get_country_iso_code("Wakanda"))


if __name__ == "__main__":
    unittest.main()
