import unittest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
sys.modules["mpv"] = MagicMock()

import subprocess
from gi.repository import Gio


class MockBox:
    def __init__(self):
        self.children = []
    def get_first_child(self):
        return self.children[0] if self.children else None
    def append(self, child):
        self.children.append(child)
    def remove(self, child):
        if child in self.children:
            self.children.remove(child)


class MockStack:
    def __init__(self, initial="library"):
        self.visible_child_name = initial
    def get_visible_child_name(self):
        return self.visible_child_name
    def set_visible_child_name(self, name):
        self.visible_child_name = name


class TestPlayerNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        res_file = Path("cine.gresource")
        if not res_file.exists():
            res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
        if res_file.exists():
            try:
                res = Gio.Resource.load(str(res_file))
                Gio.resources_register(res)
            except Exception as e:
                print("Failed to load resource:", e)

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

    def setUp(self):
        from src.window import CineWindow
        self.win = CineWindow.__new__(CineWindow)
        self.win.nav_stack = []
        self.win.previous_page_before_player = None
        self.win.is_loading_stream = False
        self.win.stream_request_id = 0
        self.win.stream_queue = []
        self.win.stream_queue_index = 0
        self.win.stream_queue_title = None
        self.win._current_playing_item = None

        self.win.main_stack = MockStack("library")
        self.win.library_stack = MockStack("content")
        self.win.category_btn_stack = MockStack("movies")
        self.win.details_box = MockBox()
        self.win.person_box = MockBox()

        self.win.player_loading_box = MagicMock()
        self.win.spinner = MagicMock()
        self.win.player_buffering_label = MagicMock()
        self.win.revealer_ui = MagicMock()
        self.win._show_ui = MagicMock()
        self.win._show_toast = MagicMock()
        self.win._populate_local_db_page = MagicMock()
        self.win._update_continue_watching_section = MagicMock()

    def test_back_from_player_to_details(self):
        """User opens details -> plays movie -> clicks back from player -> returns instantly to details."""
        # 1. User starts at Library
        self.win.main_stack.set_visible_child_name("library")
        self.win.library_stack.set_visible_child_name("content")
        self.win.category_btn_stack.set_visible_child_name("movies")

        # 2. Movie clicked -> details opened
        self.win._push_current_nav_state()
        movie_data = {"id": "tt12345", "title": "Test Movie", "type": "movie"}
        mock_page = MagicMock()
        mock_page.movie_stub = movie_data
        mock_page._destroyed = False
        mock_page._auto_play_on_streams_loaded = False
        self.win.details_box.append(mock_page)
        self.win.main_stack.set_visible_child_name("details")

        self.assertEqual(len(self.win.nav_stack), 1)
        self.assertEqual(self.win.nav_stack[0]["main_page"], "library")

        # 3. Stream starts loading / player opened
        self.win._show_player_loading_ui("Loading streams...", "Test Movie")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")
        self.assertEqual(len(self.win.nav_stack), 2)
        self.assertEqual(self.win.nav_stack[-1]["main_page"], "details")
        self.assertEqual(self.win.previous_page_before_player, "details")

        # 4. User clicks Back on player
        self.win._close_player(remove_torrent=True)

        # 5. Must return to details page without blank screen
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "details")
        self.assertEqual(self.win.details_box.get_first_child(), mock_page)
        self.assertIsNone(self.win.previous_page_before_player)
        mock_page.update_continue_btn.assert_called_once()

        # 6. Now clicking Back on details returns to library
        self.win._go_back()
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "library")

    def test_back_from_player_to_continue_watching_on_library(self):
        """User clicks Continue Watching card on Library -> plays -> clicks back -> returns to Library."""
        # 1. User on library / discover / series
        self.win.main_stack.set_visible_child_name("library")
        self.win.library_stack.set_visible_child_name("discover")
        self.win.category_btn_stack.set_visible_child_name("series")

        # 2. Continue watching card clicked -> player loading
        curr_page = self.win.main_stack.get_visible_child_name()
        self.win.previous_page_before_player = curr_page
        self.win._push_current_nav_state()

        self.win._show_player_loading_ui("Connecting...", "Series Ep 1")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")
        self.assertEqual(self.win.nav_stack[-1]["main_page"], "library")
        self.assertEqual(self.win.nav_stack[-1]["library_page"], "discover")
        self.assertEqual(self.win.nav_stack[-1]["category_btn"], "series")

        # 3. User clicks back from player
        self.win._close_player(remove_torrent=True)

        # 4. Must return to library with discover and series active, NOT details
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "library")
        self.assertEqual(self.win.library_stack.get_visible_child_name(), "discover")
        self.assertEqual(self.win.category_btn_stack.get_visible_child_name(), "series")

    def test_back_from_player_to_downloads(self):
        """User on downloads page -> plays download -> clicks back -> returns to downloads."""
        self.win.main_stack.set_visible_child_name("downloads")

        self.win.previous_page_before_player = "downloads"
        self.win._push_current_nav_state()

        self.win._show_player_loading_ui("Starting stream...", "Downloaded File")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")

        self.win._close_player(remove_torrent=True)

        self.assertEqual(self.win.main_stack.get_visible_child_name(), "downloads")
        self.win._populate_local_db_page.assert_called_with("downloads")

    def test_go_back_keyboard_alt_left_in_player(self):
        """Pressing Alt+Left (_go_back) while in player cleanly closes player and restores state."""
        self.win.main_stack.set_visible_child_name("library")
        self.win._push_current_nav_state()

        movie_data = {"id": "tt9999", "title": "Alt Left Test"}
        mock_page = MagicMock()
        mock_page.movie_stub = movie_data
        mock_page._destroyed = False
        self.win.details_box.append(mock_page)
        self.win.main_stack.set_visible_child_name("details")

        self.win._show_player_loading_ui("Playing...", "Alt Left Test")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")

        # User presses <Alt>Left -> calls _go_back()
        self.win._go_back()

        # Should invoke _close_player logic and return to details
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "details")
        self.assertEqual(self.win.details_box.get_first_child(), mock_page)

    def test_empty_details_safety_prevents_blank_screen(self):
        """If details_box has no child and no movie data, _restore_nav_entry never sets 'details'."""
        # details_box is completely empty
        while child := self.win.details_box.get_first_child():
            self.win.details_box.remove(child)

        entry = {"main_page": "details"}  # No movie_data, no existing widget
        self.win._restore_nav_entry(entry)

        # Must NOT switch to "details" (which would be blank), must fall back to "library"
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "library")

    def test_empty_person_safety_prevents_blank_screen(self):
        """If person_box has no child and no person data, _restore_nav_entry never sets 'person'."""
        while child := self.win.person_box.get_first_child():
            self.win.person_box.remove(child)

        entry = {"main_page": "person"}  # No person_data, no existing widget
        self.win._restore_nav_entry(entry)

        # Must NOT switch to "person" (which would be blank), must fall back to "library"
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "library")

    def test_all_streams_exhausted_returns_gracefully(self):
        """When streams fail/exhaust, _handle_all_streams_exhausted restores view without blank screen."""
        self.win.main_stack.set_visible_child_name("library")
        self.win._push_current_nav_state()

        movie_data = {"id": "tt7777", "title": "Failing Movie"}
        mock_page = MagicMock()
        mock_page.movie_stub = movie_data
        mock_page._destroyed = False
        self.win.details_box.append(mock_page)
        self.win.main_stack.set_visible_child_name("details")

        self.win._show_player_loading_ui("Fetching...", "Failing Movie")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")

        self.win._handle_all_streams_exhausted()

        # Returned cleanly to details
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "details")
        self.win._show_toast.assert_called()

    def test_close_player_disarms_background_autoplay(self):
        """Closing player disarms _auto_play_on_streams_loaded so late addon responses don't hijack UI."""
        mock_page = MagicMock()
        mock_page._auto_play_on_streams_loaded = True
        mock_page._auto_play_next = True
        mock_page.movie_stub = {"id": "tt111", "title": "Late Addon"}
        mock_page._destroyed = False
        self.win.details_box.append(mock_page)

        self.win.main_stack.set_visible_child_name("player")
        self.win.nav_stack = [{"main_page": "details", "movie_data": mock_page.movie_stub}]

        self.win._close_player(remove_torrent=True)

        self.assertFalse(mock_page._auto_play_on_streams_loaded)
        self.assertFalse(mock_page._auto_play_next)

    def test_active_player_to_downloads_and_back_returns_to_player(self):
        """When a video is playing and user opens downloads from menu, clicking back returns to player."""
        # 1. Video is playing
        movie_data = {"id": "tt12345", "title": "Playing Movie"}
        mock_page = MagicMock()
        mock_page.movie_stub = movie_data
        mock_page._destroyed = False
        self.win.details_box.append(mock_page)
        self.win.main_stack.set_visible_child_name("player")
        self.win._current_playing_item = movie_data
        self.win.mpv = MagicMock()
        self.win.mpv.idle_active = False
        self.win.nav_stack = [{"main_page": "details", "movie_data": movie_data}]

        # 2. User goes to downloads from menu
        self.win._open_local_page("downloads")
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "downloads")
        # Ensure player state was pushed to nav_stack
        self.assertEqual(self.win.nav_stack[-1], {"main_page": "player"})

        # 3. User clicks back button on downloads page
        self.win._go_back()
        # Must return to player!
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "player")

        # 4. In player, user closes player -> should return to details
        self.win._close_player(remove_torrent=True)
        self.assertEqual(self.win.main_stack.get_visible_child_name(), "details")


if __name__ == "__main__":
    unittest.main()
