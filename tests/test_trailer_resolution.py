import unittest
import time
from unittest.mock import patch, MagicMock

from src import api
from src import database
from src import player


class TestTrailerResolution(unittest.TestCase):

    def test_extract_trailer_id_direct_string(self):
        m = {"trailer": "7gCitSBXW6w"}
        self.assertEqual(api.extract_trailer_id(m), "7gCitSBXW6w")

    def test_extract_trailer_id_direct_dict(self):
        m = {"trailer": {"source": "7gCitSBXW6w", "type": "Trailer"}}
        self.assertEqual(api.extract_trailer_id(m), "7gCitSBXW6w")

    def test_extract_trailer_id_trailer_streams(self):
        # Cinemeta standard format
        m = {
            "name": "Jaadugar: A Witch in Mongolia",
            "trailer": None,
            "trailerStreams": [
                {"title": "Jaadugar", "ytId": "7gCitSBXW6w"},
                {"title": "Jaadugar 2", "ytId": "1oPCiZlXnYQ"}
            ]
        }
        self.assertEqual(api.extract_trailer_id(m), "7gCitSBXW6w")

    def test_extract_trailer_id_trailers_list(self):
        m = {
            "name": "The Uprising",
            "trailer": None,
            "trailers": [
                {"source": "lyhCxZN4Ro8", "type": "Trailer"}
            ]
        }
        self.assertEqual(api.extract_trailer_id(m), "lyhCxZN4Ro8")

    def test_extract_trailer_id_videos_list(self):
        m = {
            "videos": [
                {"title": "Official Trailer", "id": "yt_id:7gCitSBXW6w", "trailer": True}
            ]
        }
        self.assertEqual(api.extract_trailer_id(m), "7gCitSBXW6w")

    def test_extract_trailer_id_empty_or_none(self):
        self.assertIsNone(api.extract_trailer_id(None))
        self.assertIsNone(api.extract_trailer_id({}))
        self.assertIsNone(api.extract_trailer_id({"trailer": "none"}))
        self.assertIsNone(api.extract_trailer_id({"trailer": "null"}))

    @patch("src.database.get_cached_metadata")
    def test_fetch_trailer_link_fast_cached_trailer_streams(self, mock_cached):
        mock_cached.return_value = {
            "title": "Jaadugar",
            "trailerStreams": [{"ytId": "7gCitSBXW6w"}]
        }
        res = api.fetch_trailer_link_fast("tt39281887")
        self.assertEqual(res, "7gCitSBXW6w")

    def test_database_cached_trailer_stream_expiry(self):
        now = time.time()

        def put(key, stored, ua="Mozilla"):
            with database._cache_db_lock:
                conn = database._get_cache_db()
                conn.execute("INSERT OR REPLACE INTO trailer_stream_cache (youtube_id, stream_url, user_agent, updated_at) VALUES (?, ?, ?, ?)",
                             (key, stored, ua, now))
                conn.commit()

        # Legacy (unversioned) entries were low-resolution android streams
        put("test_legacy", f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now + 10000)}&cver=android")
        self.assertIsNone(database.get_cached_trailer_stream("test_legacy"))

        # Expired video URL
        database.save_cached_trailer_stream(
            "test_expired", f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now - 100)}", "Mozilla")
        self.assertIsNone(database.get_cached_trailer_stream("test_expired"))

        # Expired audio URL
        database.save_cached_trailer_stream(
            "test_expired_audio", f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now + 10000)}", "Mozilla",
            audio_url=f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now - 100)}")
        self.assertIsNone(database.get_cached_trailer_stream("test_expired_audio"))

        # Valid separate video + audio
        v = f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now + 10000)}&itag=137"
        a = f"https://rr7---sn-x.googlevideo.com/videoplayback?expire={int(now + 10000)}&itag=140"
        database.save_cached_trailer_stream("test_valid", v, "Mozilla", audio_url=a)
        self.assertEqual(database.get_cached_trailer_stream("test_valid"), (v, a, "Mozilla"))

    @patch("src.database.save_cached_trailer_stream")
    @patch("src.database.get_cached_trailer_stream", return_value=None)
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_player_resolve_youtube_url_cmd_args(self, mock_which, mock_run, mock_get_cached, mock_save):
        mock_which.return_value = "/usr/bin/yt-dlp"
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = (
            '{"format_id": "137+140", "http_headers": {"User-Agent": "UA"}, "requested_formats": ['
            '{"url": "https://example.com/v.mp4", "http_headers": {"User-Agent": "UA"}},'
            '{"url": "https://example.com/a.m4a"}]}'
        )
        mock_run.return_value = mock_res

        v_url, a_url, ua = player.resolve_youtube_url("uncached_test_id")
        self.assertEqual((v_url, a_url, ua), ("https://example.com/v.mp4", "https://example.com/a.m4a", "UA"))
        mock_save.assert_called_once_with("uncached_test_id", v_url, "UA", audio_url=a_url)

        # First attempt uses the HD-capable client and prefers separate DASH streams
        cmd = mock_run.call_args_list[0][0][0]
        ext_idx = cmd.index("--extractor-args")
        self.assertEqual(cmd[ext_idx + 1], "youtube:player_client=visionos")
        f_idx = cmd.index("-f")
        self.assertTrue(cmd[f_idx + 1].startswith("bestvideo[height<=1080]"))
        # TLS verification stays on: the resolved URLs are handed to mpv.
        self.assertNotIn("--no-check-certificates", cmd)

    @patch("src.database.save_cached_trailer_stream")
    @patch("src.database.get_cached_trailer_stream", return_value=None)
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_player_resolve_youtube_url_falls_back_through_clients(self, mock_which, mock_run, mock_get_cached, mock_save):
        mock_which.return_value = "/usr/bin/yt-dlp"
        fail = MagicMock(returncode=1, stdout="", stderr="ERROR: blocked")
        ok = MagicMock(returncode=0, stdout='{"url": "https://example.com/s.mp4", "http_headers": {"User-Agent": "UA"}}', stderr="")
        mock_run.side_effect = [fail, fail, ok]

        self.assertEqual(player.resolve_youtube_url("fallback_id"), ("https://example.com/s.mp4", None, "UA"))
        cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn("youtube:player_client=visionos", cmds[0])
        self.assertNotIn("--extractor-args", cmds[1])  # yt-dlp default clients
        self.assertIn("youtube:player_client=android", cmds[2])

    def test_safe_mode_ytdl_raw_options_quotes_client_list(self):
        opts = player.youtube_safe_mode_ytdl_raw_options()
        value = "youtube:player_client=visionos,default,android"
        self.assertEqual(opts, f"no-playlist=,extractor-args=%{len(value)}%{value}")

    @patch("gi.repository.GLib.idle_add")
    def test_play_trailer_direct_mpv_first(self, mock_idle_add):
        received_stats = []
        def dummy_cb(stats):
            received_stats.append(stats)

        mock_idle_add.side_effect = lambda fn, *args: fn(*args)

        player.play_trailer("dQw4w9WgXcQ", progress_callback=dummy_cb)
        time.sleep(0.05)

        self.assertTrue(len(received_stats) >= 1)
        stat = received_stats[0]
        self.assertEqual(stat.get("url"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertTrue(stat.get("is_trailer"))
        self.assertFalse(stat.get("is_direct"))
        self.assertEqual(stat.get("youtube_id"), "dQw4w9WgXcQ")


if __name__ == "__main__":
    unittest.main()
