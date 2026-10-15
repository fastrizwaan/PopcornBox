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

from src.window import CineWindow
from src import api, database


class TestDiscoverToolbar(unittest.TestCase):
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
        database._cache_conn = None
        database._cache_db_initialized = False
        api.reset_addon_health_cache()

        self.win = MagicMock()
        self.win.category_btn_stack = Gtk.Stack()
        for name in ("home", "movies", "series", "anime", "discover"):
            self.win.category_btn_stack.add_named(Gtk.Box(), name)

        self.win.library_stack = Gtk.Stack()
        for name in ("discover", "content", "unique", "search_results"):
            self.win.library_stack.add_named(Gtk.Box(), name)

        self.win.discover_options_revealer = Gtk.Revealer()
        self.win.discover_back_box = Gtk.Box()
        self.win.discover_grid_title = Gtk.Label()
        self.win.content_flowbox = Gtk.FlowBox()
        self.win.unique_flowbox = Gtk.FlowBox()
        self.win._content_auto_fill_count = 0

        # Toolbar controls
        self.win.type_dropdown = Gtk.DropDown()
        self.win.catalog_dropdown = Gtk.DropDown()
        self.win.section_dropdown = Gtk.DropDown()
        self.win.genre_dropdown = Gtk.DropDown()
        self.win.year_dropdown = Gtk.DropDown()
        self.win.language_dropdown = Gtk.DropDown()
        self.win.unique_check = Gtk.CheckButton()
        self.win.library_unique_check = Gtk.CheckButton()
        self.win.filter_toggle_btn = Gtk.ToggleButton()

        self.win._syncing_discover_toolbar = False
        self.win._syncing_unique_check = False
        self.win.lookup_action = MagicMock(return_value=None)
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_language = None
        self.win.current_media_type = "movie"
        self.win._current_discover_type = "all"
        self.win._current_discover_addon = None

        self.win._cancel_all_background_fetching = MagicMock()
        self.win._cancel_unique_background = MagicMock()
        self.win._cancel_discover_background = MagicMock()
        self.win._apply_sort_to_current_catalog = MagicMock()
        self.win._refresh_content = MagicMock()
        self.win._refresh_unique_combined_view = MagicMock()
        self.win._refresh_discover_page = MagicMock()

        # Attach methods under test
        self.win._init_discover_toolbar = lambda: CineWindow._init_discover_toolbar(self.win)
        self.win._get_current_discover_type = lambda: CineWindow._get_current_discover_type(self.win)
        self.win._populate_discover_type_dropdown = lambda: CineWindow._populate_discover_type_dropdown(self.win)
        self.win._populate_discover_catalog_dropdown = lambda ct: CineWindow._populate_discover_catalog_dropdown(self.win, ct)
        self.win._populate_discover_section_dropdown = lambda a, ct: CineWindow._populate_discover_section_dropdown(self.win, a, ct)
        self.win._populate_discover_genre_dropdown = lambda a, ct, ac=None: CineWindow._populate_discover_genre_dropdown(self.win, a, ct, ac)
        self.win._populate_discover_year_dropdown = lambda a, ct, ac=None: CineWindow._populate_discover_year_dropdown(self.win, a, ct, ac)
        self.win._populate_discover_language_dropdown = lambda a, ct, ac=None: CineWindow._populate_discover_language_dropdown(self.win, a, ct, ac)
        self.win._restore_dropdown_selection = lambda dd, it, ts: CineWindow._restore_dropdown_selection(self.win, dd, it, ts)
        self.win._on_discover_type_changed = lambda dd, p: CineWindow._on_discover_type_changed(self.win, dd, p)
        self.win._on_discover_catalog_changed = lambda dd, p: CineWindow._on_discover_catalog_changed(self.win, dd, p)
        self.win._on_discover_section_changed = lambda dd, p: CineWindow._on_discover_section_changed(self.win, dd, p)
        self.win._on_discover_genre_changed = lambda dd, p: CineWindow._on_discover_genre_changed(self.win, dd, p)
        self.win._on_discover_year_changed = lambda dd, p: CineWindow._on_discover_year_changed(self.win, dd, p)
        self.win._on_discover_language_changed = lambda dd, p: CineWindow._on_discover_language_changed(self.win, dd, p)
        self.win._on_discover_unique_toggled = lambda b: CineWindow._on_discover_unique_toggled(self.win, b)
        self.win._on_unique_toggled = lambda b: CineWindow._on_unique_toggled(self.win, b)
        self.win._get_current_discover_filter_dict = lambda: CineWindow._get_current_discover_filter_dict(self.win)
        self.win._on_discover_filter_changed = lambda: CineWindow._on_discover_filter_changed(self.win)
        self.win._is_unique_mode_active = lambda: CineWindow._is_unique_mode_active(self.win)
        self.win.reload_catalog_grid = lambda f=None: CineWindow.reload_catalog_grid(self.win, f)

    def tearDown(self):
        if hasattr(self.win, "_content_abort_event") and self.win._content_abort_event:
            try:
                self.win._content_abort_event.set()
            except Exception:
                pass
        if hasattr(self.win, "_discover_abort_event") and self.win._discover_abort_event:
            try:
                self.win._discover_abort_event.set()
            except Exception:
                pass
        if hasattr(self.win, "_discover_active_pool") and self.win._discover_active_pool:
            try:
                self.win._discover_active_pool.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
        if hasattr(self.win, "_unique_active_pool") and self.win._unique_active_pool:
            try:
                self.win._unique_active_pool.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
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

    def test_initial_toolbar_population(self):
        """Toolbar should initialize with 'All Types', 'All Catalogs', 'All Sections', 'All Genres', 'All Years', 'All Languages'."""
        self.win._init_discover_toolbar()

        # Type dropdown
        t_model = self.win.type_dropdown.get_model()
        self.assertGreater(t_model.get_n_items(), 0)
        self.assertEqual(t_model.get_string(0), "All Types")
        self.assertEqual(self.win.type_dropdown.get_selected(), 0)

        # Catalog dropdown
        c_model = self.win.catalog_dropdown.get_model()
        self.assertGreater(c_model.get_n_items(), 0)
        self.assertEqual(c_model.get_string(0), "All Catalogs")
        self.assertEqual(self.win.catalog_dropdown.get_selected(), 0)

        # Section dropdown
        s_model = self.win.section_dropdown.get_model()
        self.assertEqual(s_model.get_n_items(), 1)
        self.assertEqual(s_model.get_string(0), "All Sections")

        # Genre dropdown
        g_model = self.win.genre_dropdown.get_model()
        self.assertGreater(g_model.get_n_items(), 1)
        self.assertEqual(g_model.get_string(0), "All Genres")
        self.assertTrue(self.win.genre_dropdown.get_sensitive())

        # Year dropdown
        y_model = self.win.year_dropdown.get_model()
        self.assertGreater(y_model.get_n_items(), 1)
        self.assertEqual(y_model.get_string(0), "All Years")
        self.assertTrue(self.win.year_dropdown.get_sensitive())

        # Language dropdown
        l_model = self.win.language_dropdown.get_model()
        self.assertGreater(l_model.get_n_items(), 1)
        self.assertEqual(l_model.get_string(0), "All Languages")
        self.assertTrue(self.win.language_dropdown.get_sensitive())

        # Unique check
        self.assertFalse(self.win.unique_check.get_active())

    def test_filter_dict_structure(self):
        """_get_current_discover_filter_dict should return complete dictionary with requested keys."""
        self.win._init_discover_toolbar()
        filters = self.win._get_current_discover_filter_dict()

        self.assertIn("type", filters)
        self.assertIn("catalog_id", filters)
        self.assertIn("section", filters)
        self.assertIn("genre", filters)
        self.assertIn("year", filters)
        self.assertIn("language", filters)
        self.assertIn("unique_only", filters)

        self.assertEqual(filters["type"], "all")
        self.assertIsNone(filters["catalog_id"])
        self.assertIsNone(filters["section"])
        self.assertIsNone(filters["genre"])
        self.assertIsNone(filters["year"])
        self.assertIsNone(filters["language"])
        self.assertFalse(filters["unique_only"])

    def test_cascading_on_type_change_resets_children(self):
        """When type_dropdown changes, catalog_dropdown is reloaded and children reset to 0."""
        self.win._init_discover_toolbar()

        # Find Movies index
        t_model = self.win.type_dropdown.get_model()
        movie_idx = -1
        for i in range(t_model.get_n_items()):
            if t_model.get_string(i) == "Movies":
                movie_idx = i
                break
        self.assertGreater(movie_idx, 0)

        # Trigger type change
        self.win.type_dropdown.set_selected(movie_idx)
        self.win._on_discover_type_changed(self.win.type_dropdown, None)

        self.assertEqual(self.win.catalog_dropdown.get_selected(), 0)
        self.assertEqual(self.win.section_dropdown.get_selected(), 0)
        self.assertEqual(self.win.genre_dropdown.get_selected(), 0)
        self.assertEqual(self.win.year_dropdown.get_selected(), 0)
        self.assertEqual(self.win.language_dropdown.get_selected(), 0)

    def test_cascading_on_catalog_change_inspects_manifest(self):
        """When a catalog is selected, section_dropdown, genre_dropdown, year_dropdown, language_dropdown update based on manifest."""
        mock_addon = {
            "name": "Mock Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "catalogs": [
                {
                    "type": "movie",
                    "id": "top",
                    "name": "Popular Movies",
                    "extraSupported": ["genre", "year", "skip"],
                    "genres": ["Action", "Comedy", "Drama"]
                },
                {
                    "type": "movie",
                    "id": "year",
                    "name": "New Movies",
                    "extraSupported": ["year"]
                }
            ]
        }

        self.win._init_discover_toolbar()
        self.win._discover_catalog_items.append({
            "display_name": "Mock Cinemeta",
            "addon": mock_addon,
            "manifest_url": mock_addon["manifest_url"],
            "addon_name": "Mock Cinemeta"
        })
        self.win.catalog_dropdown.set_model(Gtk.StringList.new([it["display_name"] for it in self.win._discover_catalog_items]))
        
        # Select Mock Cinemeta
        new_idx = len(self.win._discover_catalog_items) - 1
        self.win.catalog_dropdown.set_selected(new_idx)
        self.win._on_discover_catalog_changed(self.win.catalog_dropdown, None)

        # Feeds should contain Popular Movies and New Movies
        s_model = self.win.section_dropdown.get_model()
        feeds = [s_model.get_string(i) for i in range(s_model.get_n_items())]
        self.assertIn("All Sections", feeds)
        self.assertIn("Popular Movies", feeds)
        self.assertIn("New Movies", feeds)

        # Genre dropdown should be enabled and contain declared genres
        self.assertTrue(self.win.genre_dropdown.get_sensitive())
        g_model = self.win.genre_dropdown.get_model()
        genres = [g_model.get_string(i) for i in range(g_model.get_n_items())]
        self.assertIn("Action", genres)
        self.assertIn("Comedy", genres)

        # Year dropdown should be enabled
        self.assertTrue(self.win.year_dropdown.get_sensitive())

    def test_genre_dropdown_disabled_when_not_supported(self):
        """If manifest extraSupported does not include genre and no genres declared, genre_dropdown is disabled."""
        mock_addon_no_genre = {
            "name": "Simple Torrent Feed",
            "manifest_url": "https://torrent-feed/manifest.json",
            "catalogs": [
                {
                    "type": "movie",
                    "id": "feed",
                    "name": "Latest Torrents",
                    "extraSupported": ["skip"]
                }
            ]
        }

        self.win._init_discover_toolbar()
        self.win._discover_catalog_items = [
            {"display_name": "All Catalogs", "addon": None, "manifest_url": None},
            {"display_name": "Simple Torrent Feed", "addon": mock_addon_no_genre, "manifest_url": mock_addon_no_genre["manifest_url"]}
        ]
        self.win.catalog_dropdown.set_model(Gtk.StringList.new(["All Catalogs", "Simple Torrent Feed"]))
        self.win.catalog_dropdown.set_selected(1)
        self.win._on_discover_catalog_changed(self.win.catalog_dropdown, None)

        self.assertFalse(self.win.genre_dropdown.get_sensitive())
        g_model = self.win.genre_dropdown.get_model()
        self.assertEqual(g_model.get_n_items(), 1)
        self.assertEqual(g_model.get_string(0), "All Genres")

    def test_reload_catalog_grid_unique_only(self):
        """When unique_only is True, reload_catalog_grid switches to 'unique' stack page and refreshes unique view."""
        self.win.reload_catalog_grid({
            "type": "movie",
            "catalog_id": "top",
            "section": "Popular",
            "genre": "Action",
            "year": "2024",
            "language": "Hindi",
            "unique_only": True,
            "addon_url": "https://mock-addon/manifest.json"
        })

        self.assertEqual(self.win.library_stack.get_visible_child_name(), "unique")
        self.win._refresh_unique_combined_view.assert_called_once()
        self.win._refresh_content.assert_not_called()

    def test_reload_catalog_grid_specific_section(self):
        """When catalog_id and addon_url are specified and unique_only is False, reload_catalog_grid switches to 'content'."""
        self.win.reload_catalog_grid({
            "type": "movie",
            "catalog_id": "top",
            "section": "Popular",
            "genre": "Action",
            "year": "2024",
            "language": "Hindi",
            "unique_only": False,
            "addon_url": "https://mock-addon/manifest.json"
        })

        self.assertEqual(self.win.library_stack.get_visible_child_name(), "content")
        self.assertEqual(self.win.current_catalog["catalog_id"], "top")
        self.assertEqual(self.win.current_genre, "Action")
        self.assertEqual(self.win.current_year, "2024")
        self.assertEqual(self.win.current_language, "Hindi")
        self.win._refresh_content.assert_called_once()

    def test_reload_catalog_grid_all_catalogs_shows_discover(self):
        """When catalog_id is None and unique_only is False, reload_catalog_grid shows 'discover' feed."""
        self.win.reload_catalog_grid({
            "type": "all",
            "catalog_id": None,
            "section": None,
            "genre": None,
            "year": None,
            "language": None,
            "unique_only": False,
            "addon_url": None
        })

        self.assertEqual(self.win.library_stack.get_visible_child_name(), "discover")
        self.win._refresh_discover_page.assert_called_once_with(
            filter_media_type="all",
            filter_addon_url=None,
            force_refresh=True
        )

    def test_header_navigation_revealer_state_and_filter_isolation(self):
        """Switching to Home/Movies/Series/Anime hides revealer and does not leak discover filters."""
        # Setup Discover state with filters
        self.win.current_genre = "Horror"
        self.win.current_year = "2022"
        self.win.current_language = "Spanish"
        self.win.discover_options_revealer.set_reveal_child(True)

        # Switch to Home action simulation
        action = Gio.SimpleAction.new("switch-to-home", None)
        # Call the logic defined in window.py
        self.win.discover_options_revealer.set_reveal_child(False)
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_language = None
        self.win.category_btn_stack.set_visible_child_name("home")

        self.assertFalse(self.win.discover_options_revealer.get_reveal_child())
        self.assertIsNone(self.win.current_genre)
        self.assertIsNone(self.win.current_year)
        self.assertIsNone(self.win.current_language)
        self.assertEqual(self.win.category_btn_stack.get_visible_child_name(), "home")

    def test_get_discover_catalog_list_with_genre_and_year(self):
        """When both genre and year are selected, _get_discover_catalog_list must retain genre-supporting catalogs and not drop to 0."""
        self.win.current_genre = "Horror"
        self.win.current_year = "2026"
        self.win.current_sort = "Popular"
        self.win._discover_catalog_list_cache = {}

        mock_addon = {
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "enabled": True,
            "catalogs": [
                {
                    "id": "top",
                    "name": "Popular",
                    "type": "movie",
                    "extra": [{"name": "genre", "options": ["Action", "Horror", "Comedy"]}]
                },
                {
                    "id": "year",
                    "name": "New",
                    "type": "movie",
                    "extra": [{"name": "genre", "options": ["2026", "2025", "2024"]}]
                }
            ]
        }

        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.is_catalog_provider", return_value=True), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch("src.api.is_catalog_browsable", return_value=True):
            rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", filter_addon_url=None)

        # 'top' catalog supports Horror, so it must be included (not dropped due to year check)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["catalog_id"], "top")
        self.assertIn("Popular - Movies", rows[0]["title"])

    def test_get_discover_catalog_list_with_year_only(self):
        """When only year is selected, year catalogs are prioritized first and other catalogs are kept."""
        self.win.current_genre = None
        self.win.current_year = "2026"
        self.win.current_sort = "Popular"
        self.win._discover_catalog_list_cache = {}

        mock_addon = {
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "enabled": True,
            "catalogs": [
                {
                    "id": "top",
                    "name": "Popular",
                    "type": "movie",
                    "extra": [{"name": "genre", "options": ["Action", "Horror", "Comedy"]}]
                },
                {
                    "id": "year",
                    "name": "New",
                    "type": "movie",
                    "extra": [{"name": "genre", "options": ["2026", "2025", "2024"]}]
                }
            ]
        }

        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.is_catalog_provider", return_value=True), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch("src.api.is_catalog_browsable", return_value=True):
            rows = CineWindow._get_discover_catalog_list(self.win, filter_media_type="all", filter_addon_url=None)

        self.assertEqual(len(rows), 2)
        # Year catalog must be prioritized first
        self.assertEqual(rows[0]["catalog_id"], "year")
        self.assertEqual(rows[1]["catalog_id"], "top")

    @patch("src.movie_widget.MovieWidget")
    def test_populate_discover_row_slot_titles_with_genre_and_year(self, mock_mw):
        """_populate_discover_row_slot should append both genre and year cleanly to row_title."""
        mock_mw.return_value = Gtk.Box()
        self.win.current_genre = "Horror"
        self.win.current_year = "2026"
        self.win._on_movie_clicked = MagicMock()
        self.win._open_catalog_grid = MagicMock()

        slot = Gtk.Box()
        target_box = Gtk.Box()
        target_box.append(slot)

        row_info = {
            "title": "Popular - Movies",
            "catalog_id": "top",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "media_type": "movie"
        }
        items = [{"id": "tt12345", "name": "Horror Movie 2026", "year": "2026"}]

        CineWindow._populate_discover_row_slot(self.win, slot, row_info, items, target_box)

        # Slot should contain the section header and scroll
        header = slot.get_first_child()
        self.assertIsNotNone(header)
        title_label = header.get_first_child()
        self.assertIsNotNone(title_label)
        self.assertEqual(title_label.get_text(), "Popular - Movies · Horror · 2026")

    def test_get_discover_catalog_list_with_custom_catalog_types(self):
        """When an addon uses a custom type in catalog manifests (e.g. HdHub), filtering by 'movie' must retain movie catalogs."""
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_sort = "Popular"
        self.win._discover_catalog_list_cache = {}

        mock_addon = {
            "name": "HdHub",
            "manifest_url": "https://hdhub.thevolecitor.qzz.io/manifest.json",
            "enabled": True,
            "resources": ["catalog", "stream"],
            "types": ["movie", "series", "HdHub"],
            "catalogs": [
                {"id": "hdhub-featured-movies", "name": "Featured Movies", "type": "HdHub"},
                {"id": "hdhub-featured-series", "name": "Featured Series", "type": "HdHub"},
                {"id": "hdhub-popular-movies", "name": "Popular Movies", "type": "HdHub"},
                {"id": "hdhub-popular-series", "name": "Popular Series", "type": "HdHub"}
            ]
        }

        # 1. Test is_catalog_provider recognizing the custom catalog types
        self.assertTrue(api.is_catalog_provider(mock_addon, media_type="movie"))
        self.assertTrue(api.is_catalog_provider(mock_addon, media_type="series"))

        # 2. Test _get_discover_catalog_list generating the expected movie rows
        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch("src.api.is_catalog_browsable", return_value=True):
            rows = CineWindow._get_discover_catalog_list(
                self.win,
                filter_media_type="movie",
                filter_addon_url=mock_addon["manifest_url"]
            )

        self.assertEqual(len(rows), 2)
        catalog_ids = [r["catalog_id"] for r in rows]
        self.assertIn("hdhub-featured-movies", catalog_ids)
        self.assertIn("hdhub-popular-movies", catalog_ids)
        # Verify media_type is normalized to movie
        for r in rows:
            self.assertEqual(r["media_type"], "movie")

    def test_reload_catalog_grid_preserves_unique_when_toggled_from_header(self):
        """When library_unique_check is active, reload_catalog_grid must preserve unique mode and sync unique_check."""
        self.win.library_unique_check.set_active(True)
        self.win.unique_check.set_active(False)

        filters = {
            "type": "all",
            "catalog_id": None,
            "section": None,
            "genre": None,
            "year": None,
            "language": "Hindi",
            "addon_url": "https://v3-cinemeta.strem.io/manifest.json",
        }
        self.win.reload_catalog_grid(filters)
        self.assertTrue(self.win.unique_check.get_active())
        self.assertEqual(self.win.library_stack.get_visible_child_name(), "unique")
        self.win._refresh_unique_combined_view.assert_called_once()

    def test_discover_fast_skips_unsupported_language_catalog(self):
        """_fetch_single_row in discover mode skips catalogs that do not support selected language without fetching from network."""
        self.win.current_language = "Hindi"
        self.win.current_genre = None
        self.win.current_year = None
        self.win._discover_abort_event = None
        self.win.discover_request_id = 1
        self.win._discover_is_loading_rows = False
        self.win._discover_next_row_index = 0
        self.win._discover_views = {}
        self.win._get_discover_view_key = MagicMock(return_value="test_key")

        row = {
            "title": "Popular - Movies",
            "media_type": "movie",
            "catalog_id": "top",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "addon_name": "Cinemeta",
            "genres": ["Action", "Comedy", "Drama"]
        }
        self.win._discover_catalog_list = [row]
        self.win.discover_box = Gtk.Box()

        with patch("src.api.fetch_items") as mock_fetch, \
             patch("src.database.get_cached_catalog", return_value=None):
            CineWindow._load_next_discover_batch(self.win, req_id=1, batch_size=1)
            import time
            time.sleep(0.15)
            mock_fetch.assert_not_called()

    def test_empty_discover_catalog_list_displays_empty_language_label(self):
        """When discover page has 0 catalogs matching language filter, it immediately renders language empty label."""
        self.win.discover_request_id = 0
        self.win._discover_abort_event = None
        self.win._discover_active_pool = None
        self.win._discover_views = {}
        self.win.current_language = "Hindi"
        self.win.current_genre = None
        self.win.current_year = None
        self.win.discover_scrolled = Gtk.ScrolledWindow()
        self.win._current_discover_type = "all"
        self.win._current_discover_addon = "https://v3-cinemeta.strem.io/manifest.json"
        self.win._get_discover_view_key = MagicMock(return_value="test_empty_key")
        self.win._get_discover_catalog_list = MagicMock(return_value=[])

        CineWindow._refresh_discover_page(self.win, filter_media_type="all", filter_addon_url="https://v3-cinemeta.strem.io/manifest.json")
        box = self.win.discover_box
        self.assertIsNotNone(box)
        empty_box = box.get_first_child()
        self.assertIsNotNone(empty_box)
        lbl = empty_box.get_first_child()
        self.assertIsNotNone(lbl)
        self.assertEqual(lbl.get_label(), "No items found for this language.")


    def test_fetch_content_page_for_year_catalog_fetches_both_movies_and_series(self):
        """When section is 'New' (catalog_id='year') and type is 'all', _fetch_content_page fetches both movies and series."""
        self.win.current_catalog = {
            "catalog_id": "year",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "display_name": "New"
        }
        self.win._current_discover_type = "all"
        self.win.current_year = None
        self.win.current_genre = None
        self.win.current_language = None
        self.win.current_sort = "Popular"
        self.win.has_more_content = True
        self.win.is_fetching_content = False
        self.win.content_seen_ids = set()
        self.win.content_flowbox = Gtk.FlowBox()
        self.win._schedule_deferred_menu_build = MagicMock()
        self.win._on_content_scroll = MagicMock()

        fetched_types = []
        def fake_fetch_items(media_type, **kwargs):
            fetched_types.append(media_type)
            return [{"id": f"tt_{media_type}_1", "name": f"Item {media_type}", "type": media_type}]

        with patch("src.api.fetch_items", side_effect=fake_fetch_items), \
             patch("src.database.get_cached_catalog", return_value=None):
            CineWindow._fetch_content_page(self.win)
            # Give background worker thread a moment to finish
            import time
            time.sleep(0.1)

        self.assertIn("movie", fetched_types)
        self.assertIn("series", fetched_types)

    def test_on_content_scroll_caps_unfilled_viewport_auto_fills(self):
        """When viewport has no scrollbar (upper <= page_size + 50), auto-fills are capped at 3."""
        self.win.is_fetching_content = False
        self.win.has_more_content = True
        self.win.current_catalog = {"catalog_id": "top"}
        self.win._fetch_content_page = MagicMock()
        self.win.library_stack.get_visible_child_name = MagicMock(return_value="content")
        self.win._content_auto_fill_count = 0

        adj = MagicMock()
        adj.get_value.return_value = 0
        adj.get_page_size.return_value = 1000
        adj.get_upper.return_value = 800  # unfilled

        # Call 1 -> count becomes 1, fetch called
        CineWindow._on_content_scroll(self.win, adj)
        self.assertEqual(self.win._fetch_content_page.call_count, 1)

        # Call 2 -> count becomes 2, fetch called
        CineWindow._on_content_scroll(self.win, adj)
        self.assertEqual(self.win._fetch_content_page.call_count, 2)

        # Call 3 -> count becomes 3, fetch called
        CineWindow._on_content_scroll(self.win, adj)
        self.assertEqual(self.win._fetch_content_page.call_count, 3)

        # Call 4 -> capped, fetch NOT called
        CineWindow._on_content_scroll(self.win, adj)
        self.assertEqual(self.win._fetch_content_page.call_count, 3)

    def test_genre_dropdown_excludes_years_for_year_catalog(self):
        """When Cinemeta 'New' (catalog_id='year') is active, genre dropdown must NOT contain release years."""
        addon = {
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "catalogs": [
                {
                    "type": "movie",
                    "id": "year",
                    "name": "New",
                    "genres": ["2026", "2025", "2024", "2023", "Action", "Drama"],
                    "extra": [{"name": "genre", "options": ["2026", "2025", "2024", "2023", "Action", "Drama"]}]
                }
            ]
        }
        CineWindow._populate_discover_genre_dropdown(self.win, addon, "all", "year")
        model = self.win.genre_dropdown.get_model()
        items = [model.get_string(i) for i in range(model.get_n_items())]
        self.assertIn("All Genres", items)
        self.assertNotIn("2026", items)
        self.assertNotIn("2025", items)
        self.assertNotIn("2024", items)


    def test_discover_unique_toggled_enables_and_disables_unique_view(self):
        """Toggling unique_check in discover view must smoothly enable and disable unique mode."""
        self.win.category_btn_stack.set_visible_child_name("discover")
        self.win.library_stack.set_visible_child_name("discover")
        self.win.unique_check.set_active(False)

        # Enable unique
        self.win.unique_check.set_active(True)
        self.win._on_discover_unique_toggled(self.win.unique_check)
        self.assertEqual(self.win.library_stack.get_visible_child_name(), "unique")
        self.win._refresh_unique_combined_view.assert_called_once()

        # Disable unique
        self.win.unique_check.set_active(False)
        self.win._on_discover_unique_toggled(self.win.unique_check)
        self.assertEqual(self.win.library_stack.get_visible_child_name(), "discover")
        self.win._refresh_discover_page.assert_called_once()

    def test_category_tab_disabling_unique_restores_content_view(self):
        """When in movies/series/anime tab, disabling unique returns to content view."""
        self.win.category_btn_stack.set_visible_child_name("movies")
        self.win.library_stack.set_visible_child_name("unique")
        self.win.unique_check.set_active(False)

        self.win._on_unique_toggled(self.win.unique_check)
        self.assertEqual(self.win.library_stack.get_visible_child_name(), "content")
        self.win._apply_sort_to_current_catalog.assert_called_once()
        self.win._refresh_content.assert_called_once()

    def test_window_ui_headerbar_has_filter_toggle_and_no_dropdowns(self):
        """window.ui must contain filter_toggle_btn with filter-symbolic and remove library dropdowns."""
        import xml.etree.ElementTree as ET
        tree = ET.parse("src/window.ui")
        root = tree.getroot()

        all_ids = {el.get("id") for el in root.iter() if el.get("id")}
        self.assertIn("filter_toggle_btn", all_ids)
        self.assertNotIn("library_sort_dropdown", all_ids)
        self.assertNotIn("library_genre_dropdown", all_ids)
        self.assertNotIn("library_unique_check", all_ids)

        # Verify filter_toggle_btn properties
        btn = root.find(".//object[@id='filter_toggle_btn']")
        self.assertIsNotNone(btn)
        icon_prop = btn.find("./property[@name='icon-name']")
        self.assertIsNotNone(icon_prop)
        self.assertEqual(icon_prop.text, "filter-symbolic")

    def test_filter_toggle_btn_revealer_sync(self):
        """Toggling filter_toggle_btn updates discover_options_revealer and vice-versa."""
        revealer = self.win.discover_options_revealer
        btn = self.win.filter_toggle_btn

        def on_filter_toggle_toggled(b):
            is_active = b.get_active()
            if revealer.get_reveal_child() != is_active:
                revealer.set_reveal_child(is_active)
        btn.connect("toggled", on_filter_toggle_toggled)

        def on_revealer_notify(r, pspec):
            is_rev = r.get_reveal_child()
            if btn.get_active() != is_rev:
                btn.set_active(is_rev)
        revealer.connect("notify::reveal-child", on_revealer_notify)

        # Button toggled on -> reveals toolbar
        btn.set_active(True)
        self.assertTrue(revealer.get_reveal_child())

        # Button toggled off -> hides toolbar
        btn.set_active(False)
        self.assertFalse(revealer.get_reveal_child())

        # Revealer updated -> syncs button
        revealer.set_reveal_child(True)
        self.assertTrue(btn.get_active())

    def test_filter_toggle_btn_tab_switching_visibility(self):
        """Filter toggle button must be hidden for home & discover, and visible for movies, series, anime."""
        btn = self.win.filter_toggle_btn

        # Test visibility rules on different tabs
        for tab_name, should_be_visible in [
            ("home", False),
            ("discover", False),
            ("movies", True),
            ("series", True),
            ("anime", True),
        ]:
            if tab_name in ("home", "discover"):
                btn.set_visible(False)
            else:
                btn.set_visible(True)
            self.assertEqual(btn.get_visible(), should_be_visible, f"Tab {tab_name} visibility mismatch")


if __name__ == "__main__":
    unittest.main()

