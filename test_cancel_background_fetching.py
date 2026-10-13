import unittest
from unittest.mock import MagicMock, patch
import sys
import tempfile
import os
import subprocess
import threading
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

from src.window import CineWindow, MovieDetailsPage, _is_event_set


class TestCancelBackgroundFetching(unittest.TestCase):

    def test_is_event_set_helper(self):
        # 1. None
        self.assertFalse(_is_event_set(None))

        # 2. Real unset event
        ev = threading.Event()
        self.assertFalse(_is_event_set(ev))

        # 3. Real set event
        ev.set()
        self.assertTrue(_is_event_set(ev))

        # 4. Mock without return_value specified (should evaluate to False, not truthy Mock)
        mock_ev = MagicMock()
        self.assertFalse(_is_event_set(mock_ev))

        # 5. Mock with return_value=True
        mock_ev.is_set.return_value = True
        self.assertTrue(_is_event_set(mock_ev))

    def test_cancel_all_background_fetching(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.discover_request_id = 1
        win.content_request_id = 1
        win.search_request_id = 1

        old_unique_ev = threading.Event()
        old_discover_ev = threading.Event()
        old_content_ev = threading.Event()
        old_search_ev = threading.Event()

        win._unique_abort_event = old_unique_ev
        win._discover_abort_event = old_discover_ev
        win._content_abort_event = old_content_ev
        win._search_abort_event = old_search_ev

        unique_pool = MagicMock()
        discover_pool = MagicMock()
        search_pool = MagicMock()
        win._unique_active_pool = unique_pool
        win._discover_active_pool = discover_pool
        win._search_pool = search_pool

        with patch("src.movie_widget.cancel_pending_image_downloads") as mock_cancel_img:
            CineWindow._cancel_all_background_fetching(win)
            mock_cancel_img.assert_called_once()

        # All old events must be set
        self.assertTrue(old_unique_ev.is_set())
        self.assertTrue(old_discover_ev.is_set())
        self.assertTrue(old_content_ev.is_set())
        self.assertTrue(old_search_ev.is_set())

        # Request IDs must be incremented
        self.assertEqual(win.unique_request_id, 2)
        self.assertEqual(win.discover_request_id, 2)
        self.assertEqual(win.content_request_id, 2)
        self.assertEqual(win.search_request_id, 2)

        # Thread pools must be shut down with wait=False, cancel_futures=True
        unique_pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        discover_pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        search_pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

        # Active pool references must be cleared
        self.assertIsNone(win._unique_active_pool)
        self.assertIsNone(win._discover_active_pool)
        self.assertIsNone(win._search_pool)

    def test_movie_details_destroy_sets_page_abort_event(self):
        page = MagicMock(spec=MovieDetailsPage)
        page.page_abort_event = threading.Event()
        page.stream_abort_event = threading.Event()
        page._live_check_abort_event = threading.Event()
        page._destroyed = False

        MovieDetailsPage.destroy_page(page)

        self.assertTrue(page._destroyed)
        self.assertTrue(page.page_abort_event.is_set())
        self.assertTrue(page.stream_abort_event.is_set())
        self.assertTrue(page._live_check_abort_event.is_set())


if __name__ == "__main__":
    unittest.main()
