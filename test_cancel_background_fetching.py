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
else:
    try:
        tmppath = Path(schema_temp_dir.name)
        for ui_name in ["preferences.ui", "window.ui", "history.ui", "playlist.ui", "options.ui", "shortcuts-dialog.ui"]:
            (tmppath / ui_name).write_text("<interface></interface>")
        (tmppath / "style.css").write_text("")
        file_tags = "\n".join(f"    <file>{f}</file>" for f in ["preferences.ui", "window.ui", "history.ui", "playlist.ui", "options.ui", "shortcuts-dialog.ui", "style.css"])
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<gresources>
  <gresource prefix="/io/github/fastrizwaan/PopcornBox">
{file_tags}
  </gresource>
</gresources>"""
        (tmppath / "res.xml").write_text(xml_content)
        subprocess.run(["glib-compile-resources", f"--sourcedir={schema_temp_dir.name}", str(tmppath / "res.xml"), f"--target={schema_temp_dir.name}/test.gresource"], check=True)
        res = Gio.Resource.load(f"{schema_temp_dir.name}/test.gresource")
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
        win.details_box = None
        win.person_box = None

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

    def test_cancel_all_background_fetching_cleans_details_and_prewarm(self):
        win = MagicMock()
        win.unique_request_id = 1
        win.discover_request_id = 1
        win.content_request_id = 1
        win.search_request_id = 1

        prewarm_ev = threading.Event()
        win._discover_prewarm_abort_event = prewarm_ev

        detail_child = MagicMock()
        detail_child.stream_abort_event = threading.Event()
        detail_child.page_abort_event = threading.Event()
        detail_child._live_check_abort_event = threading.Event()
        detail_child._trailer_fast_abort = threading.Event()
        detail_child._fetching_streams = True
        detail_child.get_next_sibling.return_value = None

        win.details_box = MagicMock()
        win.details_box.get_first_child.return_value = detail_child

        win.person_box = MagicMock()
        win.person_box.get_first_child.return_value = None

        CineWindow._cancel_all_background_fetching(win)

        self.assertTrue(prewarm_ev.is_set())
        self.assertTrue(detail_child.stream_abort_event.is_set())
        self.assertTrue(detail_child.page_abort_event.is_set())
        self.assertTrue(detail_child._live_check_abort_event.is_set())
        self.assertTrue(detail_child._trailer_fast_abort.is_set())
        self.assertFalse(detail_child._fetching_streams)

    def test_catalog_cache_allow_stale_and_freshness(self):
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            db_dir = Path(tmpdir)
            with patch("src.database.CONFIG_DIR", db_dir), patch("src.database.DB_FILE", db_dir / "data.json"):
                from src import database
                test_key = "test:stale_catalog_key"
                items = [{"id": "m1", "title": "Movie 1"}]

                database.save_cached_catalog(test_key, items)
                self.assertTrue(database.is_catalog_cache_fresh(test_key, max_age_hours=24))

                # Simulate 25 hours old entry
                with database._cache_db_lock:
                    conn = database._get_cache_db()
                    conn.execute("UPDATE catalog_cache SET updated_at = ? WHERE cache_key = ?", (time.time() - 25 * 3600, test_key))
                    conn.commit()
                with database._MEM_CATALOG_LOCK:
                    database._MEM_CATALOG_CACHE.pop(test_key, None)

                self.assertFalse(database.is_catalog_cache_fresh(test_key, max_age_hours=24))

                # With allow_stale=True, returns data immediately for instant card rendering
                cached = database.get_cached_catalog(test_key, max_age_hours=24, allow_stale=True)
                self.assertIsNotNone(cached)
                self.assertEqual(len(cached), 1)
                self.assertEqual(cached[0]["id"], "m1")


if __name__ == "__main__":
    unittest.main()
