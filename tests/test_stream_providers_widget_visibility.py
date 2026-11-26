import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Mock mpv before importing window
sys.modules["mpv"] = MagicMock()

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

import tempfile
import subprocess
from pathlib import Path

os.environ["POPCORN_NO_METADATA_PREFETCH"] = "1"
os.environ["POPCORN_TEST_MODE"] = "1"


class TestStreamProvidersWidgetVisibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        from gi.repository import Gio
        res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
        if res_file.exists():
            try:
                res = Gio.Resource.load(str(res_file))
                Gio.resources_register(res)
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

    def setUp(self):
        from src import database
        # Start each test with only catalog/metadata addons (no stream providers)
        self.catalog_addons = [
            {
                "id": "cinemeta",
                "name": "Cinemeta",
                "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
                "resources": ["catalog", "meta"],
                "types": ["movie", "series"],
                "enabled": True,
                "streams_enabled": False,
            },
            {
                "id": "popcornbox.catalog",
                "name": "PopcornBox Catalog",
                "manifest_url": "builtin://catalog",
                "resources": ["catalog"],
                "types": ["movie", "series"],
                "enabled": True,
                "streams_enabled": False,
            },
        ]
        self.stream_addon = {
            "id": "torrentio",
            "name": "Torrentio",
            "manifest_url": "https://torrentio.strem.fun/manifest.json",
            "resources": ["stream"],
            "types": ["movie", "series"],
            "enabled": True,
            "streams_enabled": True,
        }

    def test_movie_details_hides_stream_providers_widget_when_no_stream_addons_installed(self):
        """When no stream addons or plugins are installed, the stream providers widget on the right is hidden."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async") as mock_fetch:
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)

            # Sidebar box (stream providers widget on movie details) should be hidden
            self.assertFalse(page.sidebar_box.get_visible())
            # Background stream fetching should not be triggered when no stream addons exist
            mock_fetch.assert_not_called()

    def test_movie_details_shows_stream_providers_widget_when_stream_addon_installed(self):
        """When stream addons are installed, the stream providers widget on the right is shown."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        addons_with_stream = list(self.catalog_addons) + [self.stream_addon]
        with patch("src.database.get_addons", return_value=addons_with_stream), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async") as mock_fetch:
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)

            # Sidebar box should be visible
            self.assertTrue(page.sidebar_box.get_visible())
            # Stream fetching should be called
            mock_fetch.assert_called_once_with(force=False)

    def test_movie_details_shows_stream_providers_widget_when_custom_plugin_installed(self):
        """When custom scraper plugins are installed and enabled, the widget is shown."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=True), \
             patch("src.custom_plugins.get_enabled_scrapers_for_type", return_value=[MagicMock()]), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertTrue(page.sidebar_box.get_visible())

    def test_movie_details_shows_stream_providers_widget_when_cloudstream_installed(self):
        """When runnable CloudStream providers are installed, the widget is shown."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=True), \
             patch("src.cloudstream.get_all_plugins", return_value=[{"enabled": True}]), \
             patch("src.cloudstream.is_provider_runnable", return_value=True), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertTrue(page.sidebar_box.get_visible())

    def test_movie_details_shows_stream_providers_widget_when_direct_stream_present(self):
        """When direct playable stream URL is present in stub, widget is shown even without addons."""
        from src.window import MovieDetailsPage

        movie_stub = {
            "id": "tt0106179",
            "title": "Iron Man",
            "type": "movie",
            "stream_url": "https://example.com/direct_video.mp4"
        }
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertTrue(page.sidebar_box.get_visible())

    def test_handle_addons_changed_toggles_widget_visibility(self):
        """Installing an addon dynamically updates the widget visibility."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        current_addons = list(self.catalog_addons)

        with patch("src.database.get_addons", side_effect=lambda: current_addons), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertFalse(page.sidebar_box.get_visible())

            # Simulate installing a stream addon
            current_addons.append(self.stream_addon)
            page.handle_addons_changed()
            self.assertTrue(page.sidebar_box.get_visible())

            # Simulate uninstalling the stream addon
            current_addons.pop()
            page.handle_addons_changed()
            self.assertFalse(page.sidebar_box.get_visible())

    def test_series_keeps_sidebar_visible_for_episodes(self):
        """Series details keeps the right sidebar visible to browse seasons and episodes."""
        from src.window import MovieDetailsPage

        series_stub = {
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series",
            "videos": [
                {"id": "tt0106179:1:1", "season": 1, "episode": 1, "title": "Pilot"},
            ]
        }
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(series_stub, on_back=lambda: None)
            self.assertTrue(page.sidebar_box.get_visible())

    def test_horizontal_carousel_hides_scrollbar_and_toggles_nav_arrows(self):
        """Horizontal carousels hide scrollbars, show > when scrollable, and toggle < with scroll position."""
        from gi.repository import Gtk
        from src.window import _wrap_horizontal_carousel

        sw = Gtk.ScrolledWindow()
        overlay = _wrap_horizontal_carousel(sw)
        h_policy, v_policy = sw.get_policy()
        self.assertEqual(h_policy, Gtk.PolicyType.EXTERNAL)
        self.assertEqual(v_policy, Gtk.PolicyType.NEVER)
        h_sb = sw.get_hscrollbar()
        if h_sb:
            self.assertFalse(h_sb.get_visible())

        left_btn = overlay._carousel_left_btn
        right_btn = overlay._carousel_right_btn
        self.assertFalse(left_btn.get_visible())
        self.assertFalse(right_btn.get_visible())

        hadj = sw.get_hadjustment()
        hadj.configure(0.0, 0.0, 1200.0, 20.0, 200.0, 400.0)
        self.assertFalse(left_btn.get_visible())
        self.assertTrue(right_btn.get_visible())

        # Scrolling right reveals <
        hadj.set_value(250.0)
        self.assertTrue(left_btn.get_visible())
        self.assertTrue(right_btn.get_visible())

        # Reaching the far right hides >
        hadj.set_value(800.0)
        self.assertTrue(left_btn.get_visible())
        self.assertFalse(right_btn.get_visible())

        # Returning to original position hides <
        hadj.set_value(0.0)
        self.assertFalse(left_btn.get_visible())
        self.assertTrue(right_btn.get_visible())

    def test_details_carousels_and_compact_provider_padding(self):
        """Details carousels wrap with nav arrows and reserve hover margins to prevent clipping."""
        from src.window import MovieDetailsPage

        movie_stub = {"id": "tt0106179", "title": "Iron Man", "type": "movie"}
        with patch("src.database.get_addons", return_value=list(self.catalog_addons)), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.window.MovieDetailsPage.load_details_async"), \
             patch("src.window.MovieDetailsPage.fetch_torrents_async"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            self.assertIsNotNone(page.cast_carousel._carousel_right_btn)
            self.assertIsNotNone(page.crew_carousel._carousel_right_btn)
            self.assertIsNotNone(page.prod_carousel._carousel_right_btn)
            self.assertGreaterEqual(page.cast_hbox.get_margin_top(), 6)
            self.assertGreaterEqual(page.prod_hbox.get_margin_top(), 6)
            self.assertLessEqual(page.streams_list_box.get_margin_end(), 4)
            self.assertLessEqual(page.split_hbox.get_margin_end(), 16)


if __name__ == "__main__":
    unittest.main()
