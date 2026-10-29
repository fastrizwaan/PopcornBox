import sys
import unittest
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.modules["mpv"] = MagicMock()

from src import api


class TestStreamHeaders(unittest.TestCase):
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

    def test_extract_stream_headers_proxy_headers_request(self):
        stream = {
            "url": "https://wal5as0it0.sssrr.org/sora/123/abc",
            "behaviorHints": {
                "bingeGroup": "bpx|headline",
                "filename": "Headline.720p.mp4",
                "proxyHeaders": {
                    "request": {
                        "Referer": "https://abyssplayer.com/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
                    }
                },
                "notWebReady": True
            }
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://abyssplayer.com/")
        self.assertIn("Chrome/126.0.0.0", headers.get("User-Agent"))

    def test_extract_stream_headers_direct_headers(self):
        stream = {
            "url": "https://example.com/video.mp4",
            "behaviorHints": {
                "headers": {
                    "Referer": "https://source.com/",
                    "Origin": "https://source.com"
                }
            }
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://source.com/")
        self.assertEqual(headers.get("Origin"), "https://source.com")

    def test_extract_stream_headers_query_params(self):
        stream = {
            "url": "https://example.com/video.m3u8?Referer=https%3A%2F%2Fiptv.com&User-Agent=CustomUA",
            "behaviorHints": {}
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://iptv.com")
        self.assertEqual(headers.get("User-Agent"), "CustomUA")

    def test_extract_stream_headers_empty_fallback(self):
        self.assertEqual(api.extract_stream_headers(None), {})
        self.assertEqual(api.extract_stream_headers({}), {})

    def test_play_stream_configures_mpv_headers(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        headers = {
            "Referer": "https://abyssplayer.com/",
            "User-Agent": "Mozilla/5.0 Chrome/126",
            "Origin": "https://abyssplayer.com"
        }
        CineWindow._play_stream(
            win,
            url="https://wal5as0it0.sssrr.org/sora/123/abc",
            title="Headline",
            headers=headers
        )
        self.assertEqual(mpv_props.get("referrer"), "https://abyssplayer.com/")
        self.assertEqual(mpv_props.get("user-agent"), "Mozilla/5.0 Chrome/126")
        self.assertFalse(mpv_props.get("ytdl"))
        http_fields = mpv_props.get("http-header-fields", [])
        # Referer and User-Agent are NOT duplicated in http_fields to avoid Cloudflare 400 Bad Request
        self.assertFalse(any("Referer:" in f for f in http_fields))
        self.assertFalse(any("User-Agent:" in f for f in http_fields))
        self.assertTrue(any("Origin: https://abyssplayer.com" in f for f in http_fields))

    def test_play_stream_without_user_agent_leaves_mpv_user_agent_empty(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        # When no User-Agent is explicitly requested, MPV should not be forced with Chrome UA
        CineWindow._play_stream(
            win,
            url="https://hcdn3.hakunaymatata.com/resource/test.mp4",
            title="Direct Stream",
            headers={}
        )
        self.assertEqual(mpv_props.get("user-agent"), "")
        self.assertEqual(mpv_props.get("referrer"), "")
        self.assertFalse(mpv_props.get("ytdl"))

    def test_ping_stream_url_preserves_clean_headers(self):
        stream = {
            "url": "https://hcdn3.hakunaymatata.com/resource/test.mp4",
            "behaviorHints": {"notWebReady": False}
        }
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            res = api._ping_stream_url(dict(stream))
            self.assertTrue(res.get("is_working"))
            # Ensure ffprobe was NOT passed a hardcoded Mozilla User-Agent header
            call_cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(call_cmd)
            self.assertNotIn("Mozilla", cmd_str)

    def test_play_stream_does_not_leak_previous_stream_headers(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        # Simulate previous playing item with Videasy proxyHeaders
        win._current_playing_item = {
            "selected_torrent": {
                "url": "https://videasy.net/hls/test.m3u8",
                "behaviorHints": {
                    "proxyHeaders": {
                        "request": {
                            "Referer": "https://vidy.st/",
                            "User-Agent": "Mozilla/5.0 Chrome/137"
                        }
                    }
                }
            }
        }

        # Playing a new stream with empty headers
        CineWindow._play_stream(
            win,
            url="https://bcdn.hakunaymatata.com/resource/f4452e26cf89de5e091c20c4108d6cbb.mp4?sign=test",
            title="Now Apocalypse",
            headers={}
        )
        self.assertEqual(mpv_props.get("user-agent"), "")
        self.assertEqual(mpv_props.get("referrer"), "")

    def test_hakunaymatata_strips_mozilla_user_agent(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        CineWindow._play_stream(
            win,
            url="https://bcdn.hakunaymatata.com/resource/test.mp4",
            title="Hakuna Stream",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}
        )
        self.assertEqual(mpv_props.get("user-agent"), "")

    def test_streams_match_different_quality_does_not_match(self):
        s1 = {
            "hash": "325896c673cc290e7928ac3763ec5764",
            "url": "https://bcdn.hakunaymatata.com/1080.mp4",
            "quality": "1080p",
            "filename": "⚡ Fast Load",
            "addon_names": ["GGplay"]
        }
        s2 = {
            "hash": "178244607ae3342b7fbe2ddf2107ba5a",
            "url": "https://bcdn.hakunaymatata.com/720.mp4",
            "quality": "720p",
            "filename": "⚡ Fast Load",
            "addon_names": ["GGplay"]
        }
        self.assertFalse(api.streams_match(s1, s2))

    def test_play_stream_configures_30s_timeout_and_probe_size(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        CineWindow._play_stream(
            win,
            url="https://bcdn.hakunaymatata.com/resource/test.mp4",
            title="Hakuna Stream",
            headers={}
        )
        self.assertEqual(mpv_props.get("network-timeout"), 30.0)
        self.assertEqual(mpv_props.get("demuxer-lavf-o"), "")

    def test_play_stream_sets_vod_buffer_headroom(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False
        win._current_playing_item = None

        CineWindow._play_stream(
            win,
            url="https://bcdn.hakunaymatata.com/resource/test.mp4",
            title="Slow Host",
            headers={}
        )
        self.assertEqual(mpv_props.get("demuxer-readahead-secs"), 60)
        self.assertEqual(mpv_props.get("demuxer-max-bytes"), 256 * 1024 * 1024)
        self.assertEqual(mpv_props.get("demuxer-max-back-bytes"), 64 * 1024 * 1024)
        self.assertEqual(mpv_props.get("cache-pause-wait"), 15.0)
        self.assertEqual(mpv_props.get("cache-pause-initial"), "yes")
        self.assertEqual(mpv_props.get("cache-pause"), "yes")
        self.assertEqual(mpv_props.get("cache"), "yes")

    def test_live_channel_playback_resets_vod_buffer_gate(self):
        from src.window import CineWindow
        from src import hls_proxy
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False
        win.stream_request_id = 1
        win._current_playing_item = None

        def mock_prepare_async(url, headers=None, seek_target=0, callback=None):
            callback(False, None, url, 0.0, 0.0)

        with patch.object(hls_proxy, "prepare_hls_stream_async", side_effect=mock_prepare_async):
            CineWindow._play_stream(
                win,
                url="https://iptv.example.com/live/channel.m3u8",
                title="Live Channel",
                headers={},
                media_type="channel"
            )
        self.assertEqual(mpv_props.get("demuxer-readahead-secs"), 10)
        self.assertEqual(mpv_props.get("demuxer-max-bytes"), 64 * 1024 * 1024)
        self.assertEqual(mpv_props.get("demuxer-max-back-bytes"), 16 * 1024 * 1024)
        self.assertEqual(mpv_props.get("cache-pause-wait"), 1.0)
        self.assertEqual(mpv_props.get("cache-pause-initial"), "no")

    def test_progress_adjusted_debounced_seek(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win._seek_debounce_id = None
        win._is_hls_proxy_active = False
        adj = MagicMock()
        adj.props.value = 120.0

        seek_cb = None
        def mock_timeout(ms, cb):
            nonlocal seek_cb
            seek_cb = cb
            return 888

        with patch.object(GLib, "timeout_add", side_effect=mock_timeout):
            CineWindow._on_progress_adjusted(win, adj)
            self.assertEqual(win._seek_debounce_id, 888)
            self.assertIsNotNone(seek_cb)
            seek_cb()
            win.mpv.command_async.assert_called_once_with("seek", 120.0, "absolute+keyframes")

    def test_progress_adjusted_hls_proxy_seek(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win._seek_debounce_id = None
        win._is_hls_proxy_active = True
        win._hls_session_id = "test_sess"
        adj = MagicMock()
        adj.props.value = 350.0

        seek_cb = None
        def mock_timeout(ms, cb):
            nonlocal seek_cb
            seek_cb = cb
            return 777

        with patch.object(GLib, "timeout_add", side_effect=mock_timeout):
            CineWindow._on_progress_adjusted(win, adj)
            self.assertEqual(win._seek_debounce_id, 777)
            self.assertIsNotNone(seek_cb)
            with patch.object(win, "_seek_hls_proxy") as mock_seek_hls:
                seek_cb()
                mock_seek_hls.assert_called_once_with(350.0)

    def test_stream_loading_watchdog_lifecycle(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win._stream_loading_watchdog_id = None
        win.stream_request_id = 42

        # Start watchdog
        with patch.object(GLib, "timeout_add_seconds", return_value=999) as mock_timeout:
            CineWindow._start_stream_loading_watchdog(win, 42)
            mock_timeout.assert_called_once_with(30, win._on_stream_loading_timeout, 42)
            self.assertEqual(win._stream_loading_watchdog_id, 999)

        # Cancel watchdog
        with patch.object(GLib, "source_remove") as mock_remove:
            CineWindow._cancel_stream_loading_watchdog(win)
            mock_remove.assert_called_once_with(999)
            self.assertIsNone(win._stream_loading_watchdog_id)

    def test_stream_loading_watchdog_timeout_triggers_failover(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win.stream_request_id = 5
        win.is_loading_stream = True
        win.stream_queue = [{"url": "http://example.com/1"}, {"url": "http://example.com/2"}]
        win.stream_queue_index = 0

        res = CineWindow._on_stream_loading_timeout(win, 5)
        self.assertEqual(res, GLib.SOURCE_REMOVE)
        win._try_next_stream_in_queue.assert_called_once()

    def test_trailer_loading_watchdog_lifecycle(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win._trailer_loading_watchdog_id = None
        win.stream_request_id = 42

        with patch.object(GLib, "timeout_add_seconds", return_value=999) as mock_timeout:
            CineWindow._start_trailer_loading_watchdog(win, 42)
            mock_timeout.assert_called_once_with(30, win._on_trailer_loading_timeout, 42)
            self.assertEqual(win._trailer_loading_watchdog_id, 999)

        with patch.object(GLib, "source_remove") as mock_remove:
            CineWindow._cancel_trailer_loading_watchdog(win)
            mock_remove.assert_called_once_with(999)
            self.assertIsNone(win._trailer_loading_watchdog_id)

    def test_trailer_loading_watchdog_retries_safe_mode_once(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win.stream_request_id = 5
        win.is_loading_stream = True
        win._youtube_safe_mode_active = False
        win._current_playing_item = {
            "is_trailer": True,
            "stream_url": "https://www.youtube.com/watch?v=l91Km49W9qI",
        }
        win._try_youtube_safe_mode.return_value = True

        res = CineWindow._on_trailer_loading_timeout(win, 5)
        self.assertEqual(res, GLib.SOURCE_REMOVE)
        win._try_youtube_safe_mode.assert_called_once()
        win._start_trailer_loading_watchdog.assert_called_once_with(5)
        win._handle_youtube_failure.assert_not_called()

    def test_trailer_loading_watchdog_fails_after_safe_mode_timeout(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win.stream_request_id = 5
        win.is_loading_stream = True
        win._youtube_safe_mode_active = True
        win._current_playing_item = {
            "is_trailer": True,
            "stream_url": "https://www.youtube.com/watch?v=l91Km49W9qI",
        }

        res = CineWindow._on_trailer_loading_timeout(win, 5)
        self.assertEqual(res, GLib.SOURCE_REMOVE)
        win._try_youtube_safe_mode.assert_not_called()
        win._start_trailer_loading_watchdog.assert_not_called()
        win._handle_youtube_failure.assert_called_once()
        self.assertIs(win._handle_youtube_failure.call_args.args[0], True)
        self.assertIn("timed out", win._handle_youtube_failure.call_args.kwargs["message"].lower())

    def test_trailer_loading_watchdog_ignores_started_playback(self):
        from src.window import CineWindow
        from gi.repository import GLib
        win = MagicMock()
        win.stream_request_id = 5
        win.is_loading_stream = False
        win._current_playing_item = {
            "is_trailer": True,
            "stream_url": "https://www.youtube.com/watch?v=l91Km49W9qI",
        }

        res = CineWindow._on_trailer_loading_timeout(win, 5)
        self.assertEqual(res, GLib.SOURCE_REMOVE)
        win._try_youtube_safe_mode.assert_not_called()
        win._handle_youtube_failure.assert_not_called()

    def test_play_stream_arms_trailer_watchdog(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        page = MagicMock()
        page.is_channel = False
        win.details_box = MagicMock()
        win.details_box.get_first_child.return_value = page
        win._is_playing_trailer.return_value = True
        win.stream_request_id = 7

        CineWindow._play_stream(
            win,
            url="https://www.youtube.com/watch?v=l91Km49W9qI",
            title="Some Movie (Trailer)",
            headers={},
        )

        win._start_trailer_loading_watchdog.assert_called_once_with(7)


    def test_hls_stream_loads_without_start_offset(self):
        from src.window import CineWindow
        from src import hls_proxy
        win = MagicMock()
        win.main_stack = MagicMock()
        win._is_playing_trailer.return_value = False
        win.stream_request_id = 1

        hls_url = "https://ggplay-gf.vercel.app/hls/manifest.m3u8?url=https%3A%2F%2Fnebula.bright67.online%2Fplaylist.jpg"
        mock_prepared_url = "http://127.0.0.1:9999/hls/sess123/playlist.m3u8?offset=96.00"

        def mock_prepare_async(url, headers, seek_target, callback):
            # Directly execute callback
            callback(True, "sess123", mock_prepared_url, 7200.0, 96.0)

        with patch.object(hls_proxy, "prepare_hls_stream_async", side_effect=mock_prepare_async) as mock_prep:
            CineWindow._play_stream(
                win,
                url=hls_url,
                title="Project Hail Mary",
                headers={},
                start_time=96.0
            )
            mock_prep.assert_called_once()
            win.mpv.loadfile.assert_called_once_with(mock_prepared_url, "replace")
            self.assertTrue(win._is_hls_proxy_active)
            self.assertEqual(win._hls_time_offset, 96.0)
            self.assertEqual(win._hls_total_duration, 7200.0)

    def test_mp4_stream_loads_with_start_offset(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        win._is_playing_trailer.return_value = False

        mp4_url = "https://bcdn.hakunaymatata.com/resource/video.mp4"
        CineWindow._play_stream(
            win,
            url=mp4_url,
            title="Project Hail Mary",
            headers={},
            start_time=96.0
        )
        # Verify non-HLS streams pass start offset
        win.mpv.command.assert_called_once_with("loadfile", mp4_url, "replace", -1, "start=96.00")
        self.assertIsNone(win._seek_after_loaded)


if __name__ == "__main__":
    unittest.main()
