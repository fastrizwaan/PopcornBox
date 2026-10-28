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
        # Test that non-android googlevideo URLs or expired URLs are rejected
        now = time.time()
        # Non-android URL
        url_non_android = "https://rr7---sn-5jucgv5qc5oq-itqe7.googlevideo.com/videoplayback?expire=9999999999&cver=web"
        with database._cache_db_lock:
            conn = database._get_cache_db()
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO trailer_stream_cache (youtube_id, stream_url, user_agent, updated_at) VALUES (?, ?, ?, ?)",
                        ("test_non_android", url_non_android, "Mozilla", now))
            conn.commit()

        self.assertIsNone(database.get_cached_trailer_stream("test_non_android"))

        # Expired android URL
        url_expired_android = f"https://rr7---sn-5jucgv5qc5oq-itqe7.googlevideo.com/videoplayback?expire={int(now - 100)}&cver=android"
        with database._cache_db_lock:
            cur.execute("INSERT OR REPLACE INTO trailer_stream_cache (youtube_id, stream_url, user_agent, updated_at) VALUES (?, ?, ?, ?)",
                        ("test_expired", url_expired_android, "Mozilla", now))
            conn.commit()

        self.assertIsNone(database.get_cached_trailer_stream("test_expired"))

        # Valid android URL
        url_valid_android = f"https://rr7---sn-5jucgv5qc5oq-itqe7.googlevideo.com/videoplayback?expire={int(now + 10000)}&cver=android"
        with database._cache_db_lock:
            cur.execute("INSERT OR REPLACE INTO trailer_stream_cache (youtube_id, stream_url, user_agent, updated_at) VALUES (?, ?, ?, ?)",
                        ("test_valid", url_valid_android, "Mozilla", now))
            conn.commit()

        cached = database.get_cached_trailer_stream("test_valid")
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], url_valid_android)

    @patch("src.database.get_cached_trailer_stream", return_value=None)
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_player_resolve_youtube_url_cmd_args(self, mock_which, mock_run, mock_get_cached):
        mock_which.return_value = "/usr/bin/yt-dlp"
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = '{"url": "https://example.com/stream.mp4", "http_headers": {"User-Agent": "UA"}}'
        mock_run.return_value = mock_res

        v_url, a_url, ua = player.resolve_youtube_url("uncached_test_id")
        self.assertEqual(v_url, "https://example.com/stream.mp4")
        self.assertIsNone(a_url)
        self.assertEqual(ua, "UA")

        # Verify command arguments passed to yt-dlp
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn("--extractor-args", cmd)
        ext_idx = cmd.index("--extractor-args")
        self.assertEqual(cmd[ext_idx + 1], "youtube:player_client=android")
        self.assertIn("-f", cmd)
        f_idx = cmd.index("-f")
        self.assertEqual(cmd[f_idx + 1], "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best")

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
