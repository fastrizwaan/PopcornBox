import unittest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
mpv_mock = MagicMock()
mpv_mock.ShutdownError = type("ShutdownError", (Exception,), {})
sys.modules["mpv"] = mpv_mock

import subprocess
from gi.repository import Gio, GLib


class MockBox:
    def __init__(self):
        self.children = []
        self._visible = True
    def get_first_child(self):
        return self.children[0] if self.children else None
    def append(self, child):
        self.children.append(child)
    def remove(self, child):
        if child in self.children:
            self.children.remove(child)
    def set_visible(self, val):
        self._visible = val
    def get_visible(self):
        return self._visible


class MockLabel:
    def __init__(self, label=""):
        self.text = label
    def set_text(self, text):
        self.text = text
    def get_text(self):
        return self.text


class MockStack:
    def __init__(self, initial="library"):
        self.visible_child_name = initial
    def get_visible_child_name(self):
        return self.visible_child_name
    def set_visible_child_name(self, name):
        self.visible_child_name = name


class TestTorrentStreamingProgress(unittest.TestCase):
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
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

    def setUp(self):
        from src.window import CineWindow
        self.win = CineWindow.__new__(CineWindow)
        self.win.main_stack = MockStack("details")
        self.win.library_stack = MockStack("content")
        self.win.category_btn_stack = MockStack("movies")
        self.win.details_box = MockBox()
        self.win.player_loading_box = MockBox()
        self.win.spinner = MockBox()
        self.win.player_buffering_label = MockLabel()
        self.win.is_loading_stream = False
        self.win.stream_request_id = 1
        self.win.stream_queue = []
        self.win.stream_queue_index = 0
        self.win.stream_queue_title = "Test Torrent"
        self.win.mpv = MagicMock()
        self.win.next_episode_revealer = MagicMock()
        self.win._current_playing_item = None
        self.win.previous_page_before_player = "details"
        self.win.nav_stack = []

    def _drain_main_loop(self):
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass

    def test_format_stream_stats_text_only(self):
        stats = {"status": "Initializing stream engine..."}
        result = self.win.format_stream_stats(stats)
        self.assertEqual(result, "Initializing stream engine...")

    def test_format_stream_stats_connecting(self):
        stats = {
            "status": "Fetching metadata...",
            "progress": 0.0,
            "downloaded": 0,
            "totalLength": 0,
            "downloadSpeed": 0,
            "uploadSpeed": 0,
            "activePeers": 8,
            "seeds": 3,
        }
        result = self.win.format_stream_stats(stats)
        self.assertIn("Fetching metadata...", result)
        self.assertIn("0.0% - Connecting...", result)
        self.assertIn("Peers: 8 / Seeds: 3", result)

    def test_format_stream_stats_downloading_speeds_and_sizes(self):
        stats = {
            "status": "Downloading",
            "progress": 0.155,
            "downloaded": 300 * 1024 * 1024,
            "totalLength": 2 * 1024 * 1024 * 1024,
            "downloadSpeed": 3.5 * 1024 * 1024,
            "uploadSpeed": 120 * 1024,
            "activePeers": 35,
            "seeds": 20,
        }
        result = self.win.format_stream_stats(stats)
        # Should format 15.5%, MB/s for download, KB/s for upload, and size
        self.assertIn("15.5%", result)
        self.assertIn("3.5 MB/s", result)
        self.assertIn("120.0 KB/s", result)
        self.assertIn("300.0 MB / 2.00 GB", result)
        self.assertIn("Peers: 35 / Seeds: 20", result)

    def test_update_player_loading_sets_label_and_keeps_box_visible(self):
        self.win.is_loading_stream = True
        self.win.player_loading_box.set_visible(False)
        self.win.update_player_loading("Buffering: 5.0%")
        self._drain_main_loop()

        self.assertEqual(self.win.player_buffering_label.get_text(), "Buffering: 5.0%")
        self.assertTrue(self.win.player_loading_box.get_visible())

    def test_play_stream_does_not_prematurely_hide_loading_box(self):
        self.win.show_player_loading("Starting stream...", "Test Title")
        self._drain_main_loop()
        self.assertTrue(self.win.is_loading_stream)
        self.assertTrue(self.win.player_loading_box.get_visible())

        # Calling _play_stream should NOT hide player loading box; it remains until playback actually begins
        self.win._play_stream("http://127.0.0.1:45678/stream?idx=0", "Test Title", preserve_queue=True)
        self._drain_main_loop()

        self.assertTrue(self.win.is_loading_stream)
        self.assertTrue(self.win.player_loading_box.get_visible())

    def test_hide_player_loading_clears_loading_state(self):
        self.win.is_loading_stream = True
        self.win.player_loading_box.set_visible(True)
        self.win.hide_player_loading()
        self._drain_main_loop()

        self.assertFalse(self.win.is_loading_stream)
        self.assertFalse(self.win.player_loading_box.get_visible())

    @patch("src.player.play_magnet")
    def test_play_stream_from_queue_uses_play_magnet_for_torrent(self, mock_play_magnet):
        torrent_item = {
            "name": "Big Buck Bunny",
            "infoHash": "0123456789abcdef0123456789abcdef01234567",
            "magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            "sources": ["tracker:udp://tracker.opentrackr.org:1337/announce"],
            "file_index": 0,
        }
        self.win.stream_queue = [torrent_item]
        self.win.stream_queue_index = 0
        self.win.stream_request_id = 42

        self.win._play_current_stream_from_queue(42)
        mock_play_magnet.assert_called_once()
        args, kwargs = mock_play_magnet.call_args
        self.assertIn("0123456789abcdef0123456789abcdef01234567", args[0])
        self.assertIsNotNone(kwargs.get("progress_callback"))


if __name__ == "__main__":
    unittest.main()
