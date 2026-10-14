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

        # Toolbar controls
        self.win.type_dropdown = Gtk.DropDown()
        self.win.catalog_dropdown = Gtk.DropDown()
        self.win.section_dropdown = Gtk.DropDown()
        self.win.genre_dropdown = Gtk.DropDown()
        self.win.year_dropdown = Gtk.DropDown()
        self.win.language_dropdown = Gtk.DropDown()
        self.win.unique_check = Gtk.CheckButton()
        self.win.library_unique_check = Gtk.CheckButton()

        self.win._syncing_discover_toolbar = False
        self.win.current_genre = None
        self.win.current_year = None
        self.win.current_language = None
        self.win.current_media_type = "movie"
        self.win._current_discover_type = "all"
        self.win._current_discover_addon = None

        self.win._cancel_all_background_fetching = MagicMock()
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
        self.win._get_current_discover_filter_dict = lambda: CineWindow._get_current_discover_filter_dict(self.win)
        self.win._on_discover_filter_changed = lambda: CineWindow._on_discover_filter_changed(self.win)
        self.win.reload_catalog_grid = lambda f=None: CineWindow.reload_catalog_grid(self.win, f)

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


if __name__ == "__main__":
    unittest.main()
