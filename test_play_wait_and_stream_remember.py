import unittest
import tempfile
import os
import sys
import time
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

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

res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass


class TestPlayWaitAndStreamRemember(unittest.TestCase):
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
        database.close_cache_db()
        database.reset_json_cache()
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database
        database.reset_json_cache()
        database.close_cache_db()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_database_get_working_stream_across_aliases(self):
        """Verify get_working_stream resolves across IMDb IDs, TMDB IDs, and alias lists."""
        from src import database

        stream = {
            "url": "https://provider.com/video.mp4",
            "is_http": True,
            "quality": "1080p",
            "stream_title": "Test Movie 1080p",
            "addon_names": ["TestAddon"]
        }

        # Save under tmdb:12345
        database.save_working_stream("tmdb:12345", stream_info=stream)

        # Retrieve using exact ID
        res1 = database.get_working_stream("tmdb:12345")
        self.assertIsNotNone(res1)
        self.assertEqual(res1.get("url"), stream["url"])

        # Retrieve passing a list of IDs containing the ID
        res2 = database.get_working_stream(["tt9999999", "tmdb:12345"])
        self.assertIsNotNone(res2)
        self.assertEqual(res2.get("url"), stream["url"])

        # Save with metadata caching alias resolution
        database.save_cached_metadata("tmdb:55555", "movie", {
            "id": "tmdb:55555",
            "imdb_id": "tt5555555",
            "alias_ids": ["tt5555555", "tmdb:55555"]
        })
        database.save_working_stream("tt5555555", stream_info=stream)

        # Retrieve using the TMDB ID when it was saved under IMDb ID
        res3 = database.get_working_stream("tmdb:55555")
        self.assertIsNotNone(res3)
        self.assertEqual(res3.get("url"), stream["url"])

    def test_stream_sort_key_all_prioritizes_remembered_stream(self):
        """Verify stream_sort_key_all ranks the remembered stream above generic higher-tier streams."""
        from src.window import stream_sort_key_all

        remembered = {
            "stream_title": "My Favorite 720p Stream",
            "quality": "720p",
            "url": "https://prov-b.com/stream.mp4",
            "is_http": True,
            "addon_names": ["ProviderB"]
        }

        higher_tier_other = {
            "stream_title": "Generic 1080p Stream",
            "quality": "1080p",
            "url": "https://prov-a.com/stream.mp4",
            "is_http": True,
            "addon_names": ["ProviderA"]
        }

        # Sorting key for both streams
        k_rem = stream_sort_key_all(remembered, pref_prov="ProviderA", rem_st=remembered)
        k_other = stream_sort_key_all(higher_tier_other, pref_prov="ProviderA", rem_st=remembered)

        # The remembered stream must sort ahead of the higher tier other stream
        self.assertGreater(k_rem, k_other)

    def test_on_watch_clicked_waits_when_streams_not_loaded(self):
        """Verify clicking play before streams load arms auto-play and shows loading without failing."""
        from src.window import MovieDetailsPage

        movie = {
            "id": "tt1234567",
            "title": "Uncached Movie",
            "type": "movie"
        }

        page = MovieDetailsPage(movie, on_back=lambda: None, window=MagicMock())
        page.torrents = []
        page._fetching_streams = True
        page._auto_play_on_streams_loaded = False

        # Simulate user clicking play while streams are still empty
        page.on_watch_clicked(btn=None)

        # Auto-play must be armed
        self.assertTrue(page._auto_play_on_streams_loaded)
        # Window loading indicator must be displayed
        page.window.show_player_loading.assert_called_once()
        # Playback must NOT have proceeded to play_stream_with_failover yet
        page.window.play_stream_with_failover.assert_not_called()

    def test_stream_dropdown_and_selection_keeps_remembered_stream(self):
        """Verify UI stream dropdown keeps remembered stream as selected rather than forcing index 0."""
        from src.window import MovieDetailsPage

        movie = {
            "id": "tt7777777",
            "title": "Stream Selection Movie",
            "type": "movie"
        }

        page = MovieDetailsPage(movie, on_back=lambda: None, window=MagicMock())

        stream1 = {
            "stream_title": "First Result 1080p",
            "quality": "1080p",
            "url": "https://other.com/video.mp4",
            "is_http": True,
            "addon_names": ["AddonA"]
        }
        stream2_remembered = {
            "stream_title": "My Used Stream 720p",
            "quality": "720p",
            "url": "https://remembered.com/video.mp4",
            "is_http": True,
            "addon_names": ["AddonB"]
        }

        page.remembered_working_stream = stream2_remembered
        page.selected_torrent = stream2_remembered

        # In target stream list where stream2 is at index 1
        stream_list = [stream1, stream2_remembered]

        # Call update_file_dropdown_ui logic via process_batch or dropdown
        matched_idx = -1
        for idx, item in enumerate(stream_list):
            from src.window import _streams_match
            if _streams_match(page.remembered_working_stream, item):
                matched_idx = idx
                break

        self.assertEqual(matched_idx, 1)

        # When process_batch applies target_t_list, it must select matched index
        from src.window import _find_stream_index_exact
        found_idx = _find_stream_index_exact(page.remembered_working_stream, stream_list)
        self.assertEqual(found_idx, 1)

        if found_idx >= 0:
            page.selected_torrent = stream_list[found_idx]

        self.assertEqual(page.selected_torrent, stream2_remembered)

    def test_extract_stream_headers_default_browser_user_agent(self):
        """Verify extract_stream_headers sets DEFAULT_BROWSER_USER_AGENT for HTTP streams unless custom or hakunaymatata."""
        from src import api

        # 1. Direct HTTP stream with no headers -> sets default browser User-Agent
        stream = {
            "url": "https://floral-dawn-5cec.workers.dev/video.mp4",
            "is_http": True
        }
        headers = api.extract_stream_headers(stream)
        self.assertIn("User-Agent", headers)
        self.assertEqual(headers["User-Agent"], api.DEFAULT_BROWSER_USER_AGENT)

        # 2. hakunaymatata.com -> strips User-Agent to bypass 428/429
        hakuna_stream = {
            "url": "https://hakunaymatata.com/stream/file.mp4",
            "is_http": True
        }
        h_headers = api.extract_stream_headers(hakuna_stream)
        self.assertNotIn("User-Agent", h_headers)

        # 3. Custom User-Agent in behaviorHints proxyHeaders -> preserves custom UA
        custom_ua_stream = {
            "url": "https://custom.com/stream.mp4",
            "is_http": True,
            "behaviorHints": {
                "proxyHeaders": {
                    "request": {
                        "User-Agent": "CustomPlayer/1.0",
                        "Referer": "https://custom.com"
                    }
                }
            }
        }
        c_headers = api.extract_stream_headers(custom_ua_stream)
        self.assertEqual(c_headers.get("User-Agent"), "CustomPlayer/1.0")
        self.assertEqual(c_headers.get("Referer"), "https://custom.com")

    def test_continue_watching_alias_resolution_for_working_streams(self):
        """Verify continue_watching entries provide cross-alias lookup for get_working_stream."""
        from src import database

        stream = {
            "url": "https://debrid.worker/file.mp4",
            "is_http": True,
            "stream_title": "Movie 1080p",
            "quality": "1080p",
            "addon_names": ["Torrentio"]
        }

        # Save continue watching with alias IDs
        database.save_continue_watching({
            "id": "tmdb:99999",
            "imdb_id": "tt9999999",
            "alias_ids": ["tt9999999", "tmdb:99999"],
            "title": "Alias Test Movie",
            "selected_torrent": stream,
            "stream_url": stream["url"]
        })

        # Save working stream under tt9999999
        database.save_working_stream("tt9999999", stream_info=stream)

        # Lookup by tmdb:99999 should find it via continue_watching alias resolution
        found = database.get_working_stream("tmdb:99999")
        self.assertIsNotNone(found)
        self.assertEqual(found.get("url"), stream["url"])

    def test_play_button_proceeds_when_stream_already_ready(self):
        """Verify MovieDetailsPage.on_watch_clicked plays immediately when a working stream is already loaded."""
        from src.window import MovieDetailsPage

        movie_data = {
            "id": "tt1234567",
            "imdb_id": "tt1234567",
            "title": "Immediate Play Movie",
            "type": "movie"
        }
        page = MovieDetailsPage(movie_data, on_back=lambda: None)

        ready_stream = {
            "url": "https://fast-stream.org/movie.mp4",
            "is_http": True,
            "stream_title": "Fast Stream 1080p"
        }
        page.remembered_working_stream = ready_stream
        page.selected_torrent = ready_stream
        page.torrents = None  # Streams still fetching from network

        # Calling on_watch_clicked should not arm _auto_play_on_streams_loaded since stream is already ready
        with patch.object(page, "_start_streaming") as mock_start:
            page.on_watch_clicked()
            mock_start.assert_called_once()
            self.assertFalse(getattr(page, "_auto_play_on_streams_loaded", False))

    def test_hls_proxy_resolve_relative_url_preserves_query_tokens(self):
        """Verify _resolve_relative_url preserves master playlist tokens on child variants."""
        from src.hls_proxy import _resolve_relative_url

        base = "https://cdn.example.com/master.m3u8?token=jwt123&app=test"
        # Relative variant without query
        rel1 = "video/1080p.m3u8"
        res1 = _resolve_relative_url(base, rel1)
        self.assertEqual(res1, "https://cdn.example.com/video/1080p.m3u8?token=jwt123&app=test")

        # Relative variant with its own query string
        rel2 = "video/720p.m3u8?sig=custom_sig"
        res2 = _resolve_relative_url(base, rel2)
        self.assertEqual(res2, "https://cdn.example.com/video/720p.m3u8?sig=custom_sig")

        # Absolute URL on different domain
        rel3 = "https://other.cdn.com/seg.ts"
        res3 = _resolve_relative_url(base, rel3)
        self.assertEqual(res3, "https://other.cdn.com/seg.ts")

    def test_hls_proxy_error_marks_blocked_and_none_playback_url(self):
        """Verify prepare_hls_stream_async reports is_blocked=True and playback_url=None on HTTP 404/errors."""
        from src import hls_proxy
        import urllib.error

        results = []
        def _cb(is_fmp4, session_id, playback_url, total_dur, actual_offset, is_blocked):
            results.append({
                "is_fmp4": is_fmp4,
                "session_id": session_id,
                "playback_url": playback_url,
                "is_blocked": is_blocked
            })

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError("https://dead.stream/m.m3u8", 404, "Not Found", {}, None)
            hls_proxy.prepare_hls_stream_async("https://dead.stream/m.m3u8", callback=_cb)
            # Give background thread time to execute
            time.sleep(0.1)
            # Process GLib idle loop
            ctx = GLib.MainContext.default()
            while ctx.pending():
                ctx.iteration(False)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_blocked"])
        self.assertIsNone(results[0]["playback_url"])

    def test_database_clear_working_stream(self):
        """Verify clear_working_stream removes the failed stream from settings."""
        from src import database

        stream = {
            "url": "https://failing-provider.com/stream.mp4",
            "is_http": True,
            "quality": "1080p",
            "addon_names": ["FailingAddon"]
        }
        database.save_working_stream("tt8888888", stream_info=stream)
        self.assertIsNotNone(database.get_working_stream("tt8888888"))

        # Clear with a non-matching stream: should NOT remove
        other_stream = {"url": "https://other.com/video.mp4"}
        database.clear_working_stream("tt8888888", failed_stream=other_stream)
        self.assertIsNotNone(database.get_working_stream("tt8888888"))

        # Clear with matching stream: SHOULD remove
        database.clear_working_stream("tt8888888", failed_stream=stream)
        self.assertIsNone(database.get_working_stream("tt8888888"))

    def test_failover_discovery_extends_queue_with_working_streams(self):
        """Verify _handle_streams_exhausted_or_discover starts discovery and extends queue instead of terminating."""
        from src.window import CineWindow

        win = MagicMock(spec=CineWindow)
        win.stream_request_id = 1
        win.stream_queue = [{
            "url": "https://dead1.com/v.mp4",
            "is_http": True
        }]
        win.stream_queue_index = 1  # Exhausted
        win.stream_queue_item_id = "tt23724682"
        win.stream_queue_media_type = "movie"
        win.stream_queue_season = None
        win.stream_queue_episode = None
        win.stream_queue_title = "Outcome"
        win.details_box = MagicMock()
        win.details_box.get_first_child.return_value = None
        win._waiting_for_more_streams = False
        win._stream_waiting_watchdog_id = None
        win._extend_stream_queue = MagicMock()

        # Call unbound method
        with patch("src.api.get_torrents_streamed") as mock_streamed:
            CineWindow._handle_streams_exhausted_or_discover(win, request_id=1)
            self.assertTrue(win._waiting_for_more_streams)
            win.show_player_loading.assert_called_once()
            win._start_stream_waiting_watchdog.assert_called_once_with(1)
            time.sleep(0.1)
            mock_streamed.assert_called_once()

    def test_sanitize_stream_url(self):
        """Verify sanitize_stream_url encodes spaces and brackets without double-encoding."""
        from src.window import sanitize_stream_url

        url1 = "https://example.com/Resident Evil (2002) [Hindi BD].mkv?id=123&token=abc"
        res1 = sanitize_stream_url(url1)
        self.assertNotIn(" ", res1)
        self.assertIn("Resident%20Evil", res1)
        self.assertIn("%5BHindi%20BD%5D", res1)
        self.assertIn("id=123&token=abc", res1)

        # Non-HTTP/magnet untouched
        mag = "magnet:?xt=urn:btih:12345&dn=Resident+Evil"
        self.assertEqual(sanitize_stream_url(mag), mag)

    def test_stream_identity_unquoting(self):
        """Verify _stream_identity unquotes URLs so %20 and space match the same identity."""
        from src.window import _stream_identity

        s1 = {"url": "https://worker.dev/Resident%20Evil%20(2002).mkv"}
        s2 = {"url": "https://worker.dev/Resident Evil (2002).mkv"}
        self.assertEqual(_stream_identity(s1), _stream_identity(s2))

    def test_continue_watching_duration_shrink_protection(self):
        """Verify save_continue_watching never shrinks the known media duration when a partial stream reports a shorter length."""
        from src import database

        # 1. Save established movie item with 5629s duration
        item = {
            "id": "tt35538033",
            "title": "Resident Evil",
            "type": "movie",
            "position": 2236.4,
            "duration": 5629.7,
            "progress": 0.397
        }
        database.save_continue_watching(item)
        saved = database.get_continue_watching()[0]
        self.assertAlmostEqual(saved["duration"], 5629.7, places=1)

        # 2. Incoming update from partial stream reporting only 2400s duration and inflated 0.98 progress
        item_update = {
            "id": "tt35538033",
            "title": "Resident Evil",
            "type": "movie",
            "position": 2360.8,
            "duration": 2400.0,
            "progress": 0.983
        }
        database.save_continue_watching(item_update)
        updated = database.get_continue_watching()[0]
        # Duration should be preserved as 5629.7, and progress should be ~0.419, NOT 0.983!
        self.assertAlmostEqual(updated["duration"], 5629.7, places=1)
        self.assertAlmostEqual(updated["progress"], 2360.8 / 5629.7, places=2)
        self.assertLess(updated["progress"], 0.50)

    def test_premature_eof_triggers_failover_and_preserves_position(self):
        """Verify _handle_eof treats premature stream completion (< 88% progress) as interruption and triggers failover."""
        from src.window import CineWindow
        from src import database

        win = MagicMock(spec=CineWindow)
        win._is_playing_trailer.return_value = False
        win.next_ep_dismissed = False
        win.mpv = MagicMock()
        win.mpv.time_pos = 2360.8
        win.mpv.duration = 2400.0  # demuxer truncated
        win._is_hls_proxy_active = False
        win._current_playing_item = {
            "id": "tt35538033",
            "title": "Resident Evil",
            "type": "movie",
            "position": 2360.8,
            "duration": 5629.7,
            "progress": 0.419
        }
        win.stream_queue = [{"url": "https://st1.com"}, {"url": "https://st2.com"}]
        win._try_next_stream_in_queue = MagicMock()
        win._show_toast = MagicMock()
        win._close_player = MagicMock()

        CineWindow._handle_eof(win)

        # Should have called _try_next_stream_in_queue, NOT _close_player
        win._try_next_stream_in_queue.assert_called_once()
        win._close_player.assert_not_called()

        # Database should have the item safely saved with position preserved
        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0]["id"], "tt35538033")
        self.assertAlmostEqual(cw[0]["position"], 2360.8, places=1)


if __name__ == "__main__":
    unittest.main()
