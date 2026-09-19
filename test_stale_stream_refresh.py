import sys
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import os
import subprocess

sys.modules["mpv"] = MagicMock()

class TestStaleStreamRefresh(unittest.TestCase):
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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"

        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

        from src import database
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database
        database.reset_json_cache()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_is_stream_stale(self):
        from src.database import is_stream_stale

        now = time.time()

        # 1. Magnet stream is never stale by time
        magnet_st = {
            "magnet": "magnet:?xt=urn:btih:abcdef123456",
            "is_http": False,
            "last_watched": now - (3 * 86400)  # 3 days old
        }
        self.assertFalse(is_stream_stale(magnet_st))

        # 2. Fresh HTTP stream (< 4 hours) is not stale
        fresh_http = {
            "stream_url": "https://debrid.provider.com/stream/xyz123?token=fresh",
            "is_http": True,
            "last_watched": now - 3600  # 1 hour ago
        }
        self.assertFalse(is_stream_stale(fresh_http))

        # 3. HTTP stream older than 4 hours is stale
        stale_http = {
            "stream_url": "https://debrid.provider.com/stream/xyz123?token=old",
            "is_http": True,
            "last_watched": now - (2 * 86400)  # 2 days ago
        }
        self.assertTrue(is_stream_stale(stale_http))

        # 4. HTTP stream recently refreshed is not stale even if watched 2 days ago
        refreshed_http = {
            "stream_url": "https://debrid.provider.com/stream/xyz123?token=new",
            "is_http": True,
            "last_watched": now - (2 * 86400),
            "last_refreshed": now - 300  # refreshed 5 mins ago
        }
        self.assertFalse(is_stream_stale(refreshed_http))

    def test_streams_match_with_refreshed_token(self):
        from src.api import streams_match

        # Stream saved 2 days ago with old token
        s1 = {
            "url": "https://torrentio.strem.fun/realdebrid/token_day_1/playback/Show.S01E03.1080p.mkv?expires=1726000000",
            "is_http": True,
            "addon_names": ["Torrentio [RD+]"],
            "filename": "Show.S01E03.1080p.mkv",
            "stream_title": "Show (S01E03) 1080p BluRay",
            "quality": "1080p",
            "size_gb": 1.5
        }

        # Fresh stream fetched from Torrentio with new token
        s2 = {
            "url": "https://torrentio.strem.fun/realdebrid/token_day_3/playback/Show.S01E03.1080p.mkv?expires=1726172800",
            "is_http": True,
            "addon_names": ["Torrentio [RD+]"],
            "filename": "Show.S01E03.1080p.mkv",
            "stream_title": "Show (S01E03) 1080p BluRay",
            "quality": "1080p",
            "size_gb": 1.5
        }

        # Another stream from a different provider or different quality
        s_other = {
            "url": "https://other.provider.com/stream/file720.mp4",
            "is_http": True,
            "addon_names": ["OtherProvider"],
            "filename": "Other.Release.720p.mp4",
            "stream_title": "Other Release 720p",
            "quality": "720p",
            "size_gb": 0.8
        }

        # s1 and s2 should MATCH even though URLs differ!
        self.assertTrue(streams_match(s1, s2))
        self.assertTrue(streams_match(s2, s1))

        # s1 and s_other should NOT match
        self.assertFalse(streams_match(s1, s_other))

    def test_find_best_matching_stream(self):
        from src.api import find_best_matching_stream

        target = {
            "url": "https://old.provider.com/stream_old.mkv",
            "is_http": True,
            "addon_names": ["SuperDebrid"],
            "filename": "Breaking.Bad.S01E01.1080p.mkv",
            "stream_title": "Breaking Bad S01E01 1080p",
            "quality": "1080p",
            "size_gb": 2.1
        }

        candidates = [
            {
                "url": "https://other.com/s1.mkv",
                "is_http": True,
                "addon_names": ["OtherProvider"],
                "filename": "Breaking.Bad.S01E01.720p.mkv",
                "stream_title": "Breaking Bad 720p",
                "quality": "720p",
                "size_gb": 1.0
            },
            {
                "url": "https://new.provider.com/stream_refreshed.mkv",
                "is_http": True,
                "addon_names": ["SuperDebrid"],
                "filename": "Breaking.Bad.S01E01.1080p.mkv",
                "stream_title": "Breaking Bad S01E01 1080p",
                "quality": "1080p",
                "size_gb": 2.1
            },
            {
                "url": "https://third.com/s3.mkv",
                "is_http": True,
                "addon_names": ["ThirdAddon"],
                "filename": "Breaking.Bad.S01E01.1080p.mkv",
                "stream_title": "Breaking Bad 1080p",
                "quality": "1080p",
                "size_gb": 2.1
            }
        ]

        matched = find_best_matching_stream(target, candidates, preferred_provider="SuperDebrid")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["url"], "https://new.provider.com/stream_refreshed.mkv")
        self.assertEqual(matched["addon_names"], ["SuperDebrid"])

    def test_refresh_stream_integration(self):
        from src import api

        target = {
            "url": "https://old-link.com/stale.mp4",
            "is_http": True,
            "addon_names": ["Torrentio"],
            "filename": "Movie.2024.1080p.mkv",
            "stream_title": "Movie 2024 1080p",
            "quality": "1080p"
        }

        fresh_results = [
            {
                "url": "https://fresh-link.com/new_token.mp4",
                "is_http": True,
                "addon_names": ["Torrentio"],
                "filename": "Movie.2024.1080p.mkv",
                "stream_title": "Movie 2024 1080p",
                "quality": "1080p"
            },
            {
                "url": "https://other-provider.com/720p.mp4",
                "is_http": True,
                "addon_names": ["Other"],
                "filename": "Movie.2024.720p.mkv",
                "stream_title": "Movie 2024 720p",
                "quality": "720p"
            }
        ]

        with patch("src.api.get_torrents", return_value=fresh_results):
            refreshed, queue = api.refresh_stream(
                "tt9999999",
                media_type="movie",
                old_stream=target,
                provider="Torrentio"
            )

            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed["url"], "https://fresh-link.com/new_token.mp4")
            self.assertTrue(refreshed.get("_refreshed"))

    def test_provider_saved_in_continue_watching(self):
        from src import database

        item = {
            "id": "tt1122334",
            "title": "Sample Movie",
            "type": "movie",
            "selected_torrent": {
                "url": "https://debrid.com/stream.mp4",
                "is_http": True,
                "addon_names": ["Torbox"]
            }
        }
        database.save_continue_watching(item)

        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0].get("provider"), "Torbox")

    def test_queue_prioritizes_same_provider_on_failover(self):
        # Setup a mock window with stream queue containing streams from multiple providers
        win = MagicMock()
        win.stream_request_id = 1
        win.stream_queue_index = 0
        s0_torbox = {"url": "http://torbox.com/s0", "addon_names": ["Torbox"]}
        s1_other = {"url": "http://other.com/s1", "addon_names": ["OtherProvider"]}
        s2_torbox = {"url": "http://torbox.com/s2", "addon_names": ["Torbox"]}
        s3_third = {"url": "http://third.com/s3", "addon_names": ["ThirdProvider"]}

        win.stream_queue = [s0_torbox, s1_other, s2_torbox, s3_third]

        # Import the method and call it on mock window
        from src.window import CineWindow
        CineWindow._advance_to_next_stream_in_queue(win, request_id=1, failed_st=s0_torbox)

        # After s0 failed, s2 (also from Torbox) should be moved before s1 and s3!
        self.assertEqual(win.stream_queue_index, 1)
        self.assertEqual(win.stream_queue[1], s2_torbox)
        self.assertEqual(win.stream_queue[2], s1_other)
        self.assertEqual(win.stream_queue[3], s3_third)

    def test_play_stream_with_failover_defines_cur_q(self):
        win = MagicMock()
        win.stream_request_id = 1
        win.details_box = MagicMock()
        win.details_box.get_first_child.return_value = None
        win.main_stack = MagicMock()
        win.main_stack.get_visible_child_name.return_value = "discover"

        queue = [{
            "url": "http://stream.com/video.mp4",
            "quality": "1080p",
            "addon_names": ["Torbox"]
        }]

        from src.window import CineWindow
        # Call play_stream_with_failover directly; verify no NameError occurs
        CineWindow.play_stream_with_failover(
            win,
            queue=queue,
            initial_index=0,
            title="Sample Title",
            imdb_id="tt1234567",
            media_type="movie"
        )
        self.assertIsNotNone(win._current_playing_item)
        self.assertEqual(win._current_playing_item["quality"], "1080p")
        self.assertEqual(win._current_playing_item["provider"], "Torbox")

    def test_auto_heal_apply_refreshed_sets_last_refreshed(self):
        win = MagicMock()
        win.stream_request_id = 1
        win.stream_queue_index = 0
        failed_st = {
            "url": "http://provider.com/stale.mp4",
            "is_http": True,
            "addon_names": ["Torbox"],
            "_refreshed": False
        }
        win.stream_queue = [failed_st]
        win._current_playing_item = {
            "id": "tt1234567",
            "selected_torrent": failed_st,
            "stream_url": failed_st["url"]
        }
        refreshed_st = {
            "url": "http://provider.com/fresh.mp4",
            "is_http": True,
            "addon_names": ["Torbox"]
        }

        from src.window import CineWindow
        with patch("src.api.refresh_stream", return_value=(refreshed_st, [refreshed_st])), \
             patch("gi.repository.GLib.idle_add", side_effect=lambda cb, *a: cb(*a) if callable(cb) else None), \
             patch("threading.Thread") as mock_thread:
            
            def fake_start():
                mock_thread.call_args[1]["target"]()
            mock_thread.return_value.start = fake_start

            CineWindow._try_next_stream_in_queue(win)

        self.assertEqual(win.stream_queue[0], refreshed_st)
        self.assertEqual(win._current_playing_item["stream_url"], "http://provider.com/fresh.mp4")
        self.assertIn("last_refreshed", win._current_playing_item)
        self.assertIsInstance(win._current_playing_item["last_refreshed"], float)
        self.assertAlmostEqual(win._current_playing_item["last_refreshed"], time.time(), delta=5)

if __name__ == "__main__":
    unittest.main()
