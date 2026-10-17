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
from gi.repository import Gtk, Adw, Gio

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

import unittest
import threading
from src import api, database
from src.window import MovieDetailsPage

class TestEpisodeStreamReliability(unittest.TestCase):
    def test_get_stream_cache_key_normalizes_episode_id(self):
        """Ensure full episode ids (tt...:season:ep) normalize to the base ID so cache hits succeed."""
        key1 = api.get_stream_cache_key("tt39281887:1:3", media_type="series", season=1, episode=3)
        key2 = api.get_stream_cache_key("tt39281887", media_type="series", season=1, episode=3)
        self.assertEqual(key1, "tt39281887:S1:E3")
        self.assertEqual(key2, "tt39281887:S1:E3")
        self.assertEqual(key1, key2)

    def test_aborted_stream_request_does_not_trip_circuit_breaker(self):
        """Aborted requests (e.g. when switching episodes) must never count as server network failures."""
        manifest_url = "https://mock-stream-addon.example/manifest.json"
        addon = {
            "name": "Mock Stream Addon",
            "manifest_url": manifest_url,
            "types": ["series"]
        }
        api.reset_addon_session_status(manifest_url)
        abort_ev = threading.Event()
        abort_ev.set()

        # Request with aborted event
        res = api._request_addon_streams(addon, f"https://mock-stream-addon.example/stream/series/tt1234:1:1.json", abort_event=abort_ev)
        self.assertEqual(res, [])
        with api._ADDON_ONLINE_LOCK:
            fail_count = api._ADDON_SESSION_FAILURES.get(manifest_url, 0)
            is_blocked = manifest_url in api._ADDON_SESSION_BLOCKED
        self.assertEqual(fail_count, 0)
        self.assertFalse(is_blocked)

    def test_provider_dropdown_lists_installed_providers_on_init(self):
        """Provider dropdown must list all active stream providers immediately, not just 'All Providers'."""
        mock_addon = {
            "name": "PlavioHD",
            "manifest_url": "https://plaviohd.example/manifest.json",
            "types": ["movie", "series"],
            "resources": [{"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt"]}],
            "enabled": True,
            "streams_enabled": True
        }
        movie_stub = {
            "id": "tt39281887",
            "title": "Jaadugar",
            "type": "series",
            "videos": [{"id": "tt39281887:1:1", "season": 1, "episode": 1, "title": "Ep 1"}]
        }

        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.get_torrents_streamed"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            model = page.provider_dropdown.get_model()
            self.assertIsNotNone(model)
            items = [model.get_string(i) for i in range(model.get_n_items())]
            self.assertIn("All Providers", items)
            self.assertIn("PlavioHD", items)

    def test_render_streams_list_shows_loading_while_fetching(self):
        """render_streams_list must not show 'No streams available' prematurely while streams are in-flight."""
        movie_stub = {
            "id": "tt39281887",
            "title": "Jaadugar",
            "type": "series"
        }
        with patch("src.database.get_addons", return_value=[]), \
             patch("src.api.get_torrents_streamed"):
            page = MovieDetailsPage(movie_stub, on_back=lambda: None)
            page.current_t_list = []
            page._fetching_streams = True
            page.render_streams_list()

            first_child = page.streams_list_box.get_first_child()
            self.assertIsNotNone(first_child)
            self.assertIsInstance(first_child, Gtk.Box)
            spinner = first_child.get_first_child()
            self.assertIsInstance(spinner, Gtk.Spinner)
            self.assertTrue(spinner.get_spinning())

            # Now mark fetching complete with empty list
            page._fetching_streams = False
            page.render_streams_list()
            child_after = page.streams_list_box.get_first_child()
            self.assertIsInstance(child_after, Gtk.Label)
            self.assertEqual(child_after.get_text(), "No streams available")

if __name__ == "__main__":
    unittest.main()
