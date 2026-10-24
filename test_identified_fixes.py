import unittest
import sys
import tempfile
import os
import gzip
import zlib
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.modules["mpv"] = MagicMock()

from src import api, database
from src import libtorrent_stream


class TestIdentifiedFixes(unittest.TestCase):

    def test_subtitle_language_names_kor(self):
        """Verify 'kor' is present in SUBTITLE_LANGUAGE_NAMES and resolves to Korean."""
        self.assertIn("kor", api.SUBTITLE_LANGUAGE_NAMES)
        self.assertEqual(api.SUBTITLE_LANGUAGE_NAMES["kor"], "Korean")
        self.assertEqual(api.SUBTITLE_LANGUAGE_NAMES["ko"], "Korean")
        self.assertEqual(api.get_language_name("kor"), "Korean")
        self.assertEqual(api.get_language_name("ko"), "Korean")

    @patch("logging.debug")
    def test_run_metadata_hydration_worker_logging_debug(self, mock_logging_debug):
        """Verify _run_metadata_hydration_worker handles exceptions via logging.debug rather than crashing on NameError."""
        # The queue is module-global and other tests may have left items in it;
        # drain it so the first item processed is deterministically ours.
        while True:
            try:
                api._HYDRATION_QUEUE.get_nowait()
            except Exception:
                break

        # Enqueue an item that causes an exception inside the try block
        with patch.object(database, "get_cached_metadata", side_effect=Exception("DB simulated error")):
            api._HYDRATION_QUEUE.put(("tt_test_error", "movie", {}))
            # Run one iteration of the worker body
            try:
                # Call worker with mock queue that breaks after 1 loop
                orig_get = api._HYDRATION_QUEUE.get
                call_count = [0]
                def get_and_then_break(*args, **kwargs):
                    call_count[0] += 1
                    if call_count[0] > 1:
                        raise KeyboardInterrupt()
                    return orig_get(*args, **kwargs)
                with patch.object(api._HYDRATION_QUEUE, "get", side_effect=get_and_then_break):
                    try:
                        api._run_metadata_hydration_worker()
                    except KeyboardInterrupt:
                        pass
            finally:
                pass
            mock_logging_debug.assert_called()
            logged = [str(call.args[0]) for call in mock_logging_debug.call_args_list if call.args]
            self.assertTrue(
                any("Metadata hydration error for tt_test_error" in message for message in logged),
                f"expected hydration error for tt_test_error, got {logged}",
            )

    @patch.object(database, "add_addon")
    def test_check_addons_health_background_calls_add_addon(self, mock_add_addon):
        """Verify check_addons_health_background calls database.add_addon instead of undefined update_addon."""
        sample_addon = {
            "manifest_url": "https://test.addon/manifest.json",
            "enabled": True,
            "manifest": {"id": "test.addon", "name": "Test Addon"},
        }
        with patch.object(database, "get_addons", return_value=[sample_addon]):
            with patch.object(api, "_get_cached_request", return_value={"id": "test.addon", "resources": ["stream"]}):
                t = api.check_addons_health_background()
                t.join(timeout=2)
                mock_add_addon.assert_called_with(sample_addon, sync_to_remote=False)

    @patch("urllib.request.urlopen")
    def test_download_subtitle_to_path_gzip_decompression(self, mock_urlopen):
        """Verify download_subtitle_to_path decompresses gzip payloads."""
        raw_text = "1\n00:00:01,000 --> 00:00:04,000\nHello World\n"
        compressed = gzip.compress(raw_text.encode("utf-8"))

        mock_resp = MagicMock()
        mock_resp.read.return_value = compressed
        mock_resp.headers.get.return_value = "gzip"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test_sub.srt")
            result = api.download_subtitle_to_path("https://example.com/sub.srt.gz", target)
            self.assertEqual(result, target)
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, raw_text)

    @patch("urllib.request.urlopen")
    def test_download_subtitle_to_path_deflate_decompression(self, mock_urlopen):
        """Verify download_subtitle_to_path decompresses deflate payloads."""
        raw_text = "1\n00:00:01,000 --> 00:00:04,000\nDeflate Test\n"
        compressed = zlib.compress(raw_text.encode("utf-8"))

        mock_resp = MagicMock()
        mock_resp.read.return_value = compressed
        mock_resp.headers.get.return_value = "deflate"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test_deflate.srt")
            result = api.download_subtitle_to_path("https://example.com/sub_deflate.srt", target)
            self.assertEqual(result, target)
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, raw_text)

    def test_ping_stream_url_uses_extract_stream_headers(self):
        """Verify _ping_stream_url incorporates proxyHeaders via extract_stream_headers."""
        stream = {
            "url": "https://stream.example.com/video.mkv",
            "behaviorHints": {
                "proxyHeaders": {
                    "request": {
                        "User-Agent": "CustomAgent/1.0",
                        "Referer": "https://provider.example.com/"
                    }
                }
            }
        }
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch.object(api, "_has_ffprobe", True):
                res = api._ping_stream_url(stream)
                self.assertTrue(res.get("is_working"))
                cmd_called = mock_run.call_args[0][0]
                # Check headers passed to ffprobe
                headers_idx = cmd_called.index("-headers")
                headers_arg = cmd_called[headers_idx + 1]
                self.assertIn("Referer: https://provider.example.com/", headers_arg)
                self.assertIn("User-Agent: CustomAgent/1.0", headers_arg)

    def test_libtorrent_stream_is_buffering_finished_int(self):
        """Verify req_bytes is converted to int in is_buffering_finished."""
        with patch.object(libtorrent_stream.TorrentStreamEngine, "__init__", return_value=None):
            engine = libtorrent_stream.TorrentStreamEngine("magnet:?xt=urn:btih:dummy", "/tmp")
            engine.target = {"size": 150000000}
            engine.handle = MagicMock()
            engine._target_downloaded = MagicMock(return_value=0)
            engine._has_range_downloaded = MagicMock(return_value=True)
            engine._buffered_from_start = MagicMock(return_value=0)

            result = engine.is_buffering_finished()
            self.assertTrue(result)
            # Check arguments to _has_range_downloaded
            start_arg, end_arg = engine._has_range_downloaded.call_args[0]
            self.assertIsInstance(start_arg, int)
            self.assertIsInstance(end_arg, int)

    def test_libtorrent_stream_serve_stream_idx_param(self):
        """Verify serve_stream parses idx query parameter and updates file_index."""
        with patch.object(libtorrent_stream.TorrentStreamEngine, "__init__", return_value=None):
            engine = libtorrent_stream.TorrentStreamEngine("magnet:?xt=urn:btih:dummy", "/tmp")
            engine.error = None
            engine.ready_event = MagicMock()
            engine.ready_event.wait.return_value = True
            engine.stopped = MagicMock()
            engine.lock = MagicMock()
            engine.lock.__enter__ = MagicMock(return_value=None)
            engine.lock.__exit__ = MagicMock(return_value=None)
            engine.file_index = 0
            engine._stream_gen = 0
            engine.target = {"size": 1000, "path": "file.mkv"}
            engine._select_target_file = MagicMock()
            engine._parse_range = MagicMock(return_value=(0, 999, 200))
            engine._write_range = MagicMock()

            request = MagicMock()
            request.path = "/stream?idx=3"
            request.headers = {}

            # Execute serve_stream up to range parse
            try:
                engine.serve_stream(request)
            except Exception:
                pass

            self.assertEqual(engine.file_index, 3)
            engine._select_target_file.assert_called_once()

    def test_read_resume_data_sets_save_path(self):
        """Verify atp.save_path is populated from self.session_dir when loading fastresume data."""
        with patch.object(libtorrent_stream.TorrentStreamEngine, "__init__", return_value=None):
            engine = libtorrent_stream.TorrentStreamEngine("magnet:?xt=urn:btih:dummy", "/tmp")
            mock_lt = MagicMock()
            engine.lt = mock_lt
            engine.magnet_link = "magnet:?xt=urn:btih:dummy"
            engine.resume_path = "/tmp/dummy.fastresume"
            engine.session_dir = "/tmp/download/session123"
            engine.session = MagicMock()
            mock_atp = MagicMock()
            mock_atp.save_path = ""
            mock_lt.read_resume_data.return_value = mock_atp

            with patch("os.path.exists", return_value=True):
                with patch("builtins.open", MagicMock()):
                    engine._add_torrent()

            self.assertEqual(mock_atp.save_path, "/tmp/download/session123")

    def test_extract_metadata_genres_compound_tmdb(self):
        """Verify _extract_metadata_genres expands compound TMDB TV genres (10759, 10765, 10762, 10768)."""
        genres = database._extract_metadata_genres({"genre_ids": [10759, 10765, 10762, 10768]})
        self.assertIn("Action", genres)
        self.assertIn("Adventure", genres)
        self.assertIn("Sci-Fi", genres)
        self.assertIn("Fantasy", genres)
        self.assertIn("Family", genres)
        self.assertIn("War", genres)

    @patch("logging.debug")
    def test_fetch_collection_details_logging_debug(self, mock_logging_debug):
        """Verify fetch_collection_details catches errors without NameError on logger."""
        from src import tmdb_helper
        with patch.object(tmdb_helper, "_get_cached_request", return_value={"name": "Test Collection", "parts": [{"id": 1, "title": "P1"}]}):
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_exec:
                instance = mock_exec.return_value.__enter__.return_value
                instance.map.side_effect = Exception("Simulated collection error")
                details = tmdb_helper.fetch_collection_details(12345)
                self.assertIsNotNone(details)
                mock_logging_debug.assert_called()
                args, _ = mock_logging_debug.call_args
                self.assertIn("Timeout or error resolving parts", args[0])

    def test_movie_widget_apply_resolved_meta_adult_hiding(self):
        """Verify MovieWidget._apply_resolved_meta hides widget if is_adult=True."""
        from src.movie_widget import MovieWidget
        widget = MagicMock(spec=MovieWidget)
        widget.movie_data = {"id": "test_id"}
        widget.set_visible = MagicMock()

        MovieWidget._apply_resolved_meta(widget, "Adult Title", 2024, None, is_adult=True)
        widget.set_visible.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()
