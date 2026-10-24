import sys
import os
import unittest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

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
from gi.repository import Gtk, Gio, GLib

res_file = Path(__file__).resolve().parent / "build-dir/files/share/popcorn-box/cine.gresource"
if res_file.exists():
    res = Gio.Resource.load(str(res_file))
    Gio.resources_register(res)

from src.window import CineWindow, MovieDetailsPage


class TestRobustFilterAndSpinners(unittest.TestCase):
    def setUp(self):
        self.mock_window = MagicMock(spec=CineWindow)
        self.mock_window.discover_search_entry = Gtk.SearchEntry()
        self.mock_window.current_discover_search_query = ""
        self.mock_window.discover_spinner = Gtk.Spinner()
        self.mock_window._syncing_discover_toolbar = False
        self.mock_window._clear_discover_search_on_filter_change = lambda: CineWindow._clear_discover_search_on_filter_change(self.mock_window)

    def test_clear_discover_search_on_filter_change(self):
        """Verify that _clear_discover_search_on_filter_change clears entry text and current query."""
        self.mock_window.discover_search_entry.set_text("virgin")
        self.mock_window.current_discover_search_query = "virgin"

        CineWindow._clear_discover_search_on_filter_change(self.mock_window)

        self.assertEqual(self.mock_window.discover_search_entry.get_text(), "")
        self.assertEqual(self.mock_window.current_discover_search_query, "")

    def test_discover_filter_changed_clears_search_and_spins(self):
        """Changing any discover filter must clear any stale search query and activate spinner."""
        self.mock_window.discover_search_entry.set_text("virgin")
        self.mock_window.current_discover_search_query = "virgin"
        self.mock_window._get_current_discover_filter_dict = MagicMock(return_value={"genre": "Animation", "query": ""})
        self.mock_window.reload_catalog_grid = MagicMock()
        self.mock_window._set_discover_spinner_active = MagicMock()

        CineWindow._on_discover_filter_changed(self.mock_window)

        self.assertEqual(self.mock_window.discover_search_entry.get_text(), "")
        self.assertEqual(self.mock_window.current_discover_search_query, "")
        self.mock_window._set_discover_spinner_active.assert_called_with(True)
        self.mock_window.reload_catalog_grid.assert_called_once_with({"genre": "Animation", "query": ""})

    def test_discover_spinner_active_toggle(self):
        """The discover spinner animates on/off but keeps its toolbar slot, so the dropdowns never reflow."""
        spinner = Gtk.Spinner()
        spinner.set_visible(True)
        self.mock_window.discover_spinner = spinner

        CineWindow._set_discover_spinner_active(self.mock_window, True)
        self.assertTrue(spinner.get_visible())

        CineWindow._set_discover_spinner_active(self.mock_window, False)
        self.assertTrue(spinner.get_visible())

    def test_discover_spinner_repeated_state_is_not_reapplied(self):
        """Batch after batch reports the same loading state; the animation must not restart."""
        spinner = MagicMock()
        spinner.start = MagicMock()
        spinner.stop = MagicMock()
        window = MagicMock()
        window.discover_spinner = spinner
        window._discover_spinner_active = None

        for _ in range(5):
            CineWindow._set_discover_spinner_active(window, True)
        spinner.start.assert_called_once()

        for _ in range(5):
            CineWindow._set_discover_spinner_active(window, False)
        spinner.stop.assert_called_once()

    def test_movie_details_provider_spinner(self):
        """MovieDetailsPage._set_provider_spinner_active manages provider_spinner visibility and state."""
        page = MagicMock(spec=MovieDetailsPage)
        spinner = Gtk.Spinner()
        page.provider_spinner = spinner

        MovieDetailsPage._set_provider_spinner_active(page, True)
        self.assertTrue(spinner.get_visible())

        MovieDetailsPage._set_provider_spinner_active(page, False)
        self.assertFalse(spinner.get_visible())

    def test_cancel_all_background_fetching_stops_spinner(self):
        """_cancel_all_background_fetching must stop the discover spinner."""
        self.mock_window._set_discover_spinner_active = MagicMock()
        CineWindow._cancel_all_background_fetching(self.mock_window)
        self.mock_window._set_discover_spinner_active.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()
