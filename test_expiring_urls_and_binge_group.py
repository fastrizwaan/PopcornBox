import unittest
import time
from unittest.mock import MagicMock

class TestExpiringUrlsAndBingeGroup(unittest.TestCase):
    def test_has_likely_expiring_playback_credentials(self):
        from src.api import has_likely_expiring_playback_credentials

        # Positive tests (various signed/expiring parameters)
        url1 = "https://ggplay-gf.vercel.app/hls/manifest.m3u8?url=https%3A%2F%2Fnebula.bright67.online&referer=https%3A%2F%2Fcinejoy.pk%2F&exp=1793180253&sig=e58c86c5e7ee7aea5d19fbb92e58fc28"
        self.assertTrue(has_likely_expiring_playback_credentials(url1))

        url2 = "https://cdn.example.com/video.mp4?token=abcdef123456"
        self.assertTrue(has_likely_expiring_playback_credentials(url2))

        url3 = "https://debrid.example.com/stream/file.mkv?expires=1793180000&signature=xyz"
        self.assertTrue(has_likely_expiring_playback_credentials(url3))

        url4 = "https://stream.provider.com/playlist.m3u8?auth_token=jwt.token.here"
        self.assertTrue(has_likely_expiring_playback_credentials(url4))

        url5 = "https://host.com/live.m3u8?wssecret=abc&wstime=123"
        self.assertTrue(has_likely_expiring_playback_credentials(url5))

        # Negative tests (clean/direct static URLs)
        self.assertFalse(has_likely_expiring_playback_credentials("https://example.com/movie.mp4"))
        self.assertFalse(has_likely_expiring_playback_credentials("https://example.com/stream.m3u8?resolution=1080p&lang=en"))
        self.assertFalse(has_likely_expiring_playback_credentials("magnet:?xt=urn:btih:abcdef"))
        self.assertFalse(has_likely_expiring_playback_credentials(""))
        self.assertFalse(has_likely_expiring_playback_credentials(None))

    def test_get_stream_url_expiry_timestamp(self):
        from src.api import get_stream_url_expiry_timestamp

        url_exp = "https://example.com/stream.m3u8?exp=1793180253&sig=12345"
        self.assertEqual(get_stream_url_expiry_timestamp(url_exp), 1793180253.0)

        url_ms = "https://example.com/stream.m3u8?expires=1793180253000"
        self.assertEqual(get_stream_url_expiry_timestamp(url_ms), 1793180253.0)

        url_rel = "https://example.com/stream.m3u8?expires_in=3600"
        now = 1700000000.0
        self.assertEqual(get_stream_url_expiry_timestamp(url_rel, saved_at=now), 1700003600.0)

        url_none = "https://example.com/stream.m3u8?foo=bar"
        self.assertIsNone(get_stream_url_expiry_timestamp(url_none))

    def test_is_stream_url_expired(self):
        from src.api import is_stream_url_expired

        now = time.time()
        # Expired in past
        past_url = f"https://example.com/stream.m3u8?exp={int(now - 100)}&sig=abc"
        self.assertTrue(is_stream_url_expired(past_url, buffer_seconds=0))

        # Expiring within buffer seconds (e.g. expires in 60s, buffer is 120s)
        soon_url = f"https://example.com/stream.m3u8?exp={int(now + 60)}&sig=abc"
        self.assertTrue(is_stream_url_expired(soon_url, buffer_seconds=120))

        # Far in future (not expired)
        future_url = f"https://example.com/stream.m3u8?exp={int(now + 3600)}&sig=abc"
        self.assertFalse(is_stream_url_expired(future_url, buffer_seconds=120))

        # Token without exp timestamp, older than 1 hour
        token_url = "https://example.com/stream.m3u8?token=abcdef"
        self.assertTrue(is_stream_url_expired(token_url, saved_at=now - 4000))
        self.assertFalse(is_stream_url_expired(token_url, saved_at=now - 300))

    def test_database_is_stream_stale(self):
        from src import database

        now = time.time()
        # Non-stale stream
        fresh_stream = {
            "url": "https://example.com/direct.mp4",
            "is_http": True,
            "saved_at": now - 300
        }
        self.assertFalse(database.is_stream_stale(fresh_stream))

        # Expired by embedded URL exp parameter
        expired_url_stream = {
            "url": f"https://example.com/stream.m3u8?exp={int(now - 10)}&sig=xyz",
            "is_http": True,
            "saved_at": now - 60  # Saved only 1 min ago, but URL exp timestamp is past!
        }
        self.assertTrue(database.is_stream_stale(expired_url_stream))

        # Stale by age > max_age_seconds
        old_stream = {
            "url": "https://example.com/direct.mp4",
            "is_http": True,
            "saved_at": now - 20000
        }
        self.assertTrue(database.is_stream_stale(old_stream))

        # Torrent stream does not expire by time
        torrent_stream = {
            "url": "magnet:?xt=urn:btih:12345",
            "hash": "12345",
            "is_http": False,
            "saved_at": now - 50000
        }
        self.assertFalse(database.is_stream_stale(torrent_stream))

    def test_database_normalize_stream_for_storage(self):
        from src import database

        stream = {
            "url": "https://example.com/stream.m3u8?exp=1793180253&sig=abc",
            "is_http": True,
            "behaviorHints": {
                "bingeGroup": "torrentio|1080p|qxr",
                "filename": "show.s01e01.mkv"
            }
        }
        norm = database._normalize_stream_for_storage(stream)
        self.assertIsNotNone(norm)
        self.assertEqual(norm.get("binge_group"), "torrentio|1080p|qxr")
        self.assertTrue(norm.get("has_expiring_credentials"))
        self.assertEqual(norm.get("expires_at"), 1793180253.0)

    def test_stream_matches_binge_group(self):
        from src.api import stream_matches_binge_group

        stream1 = {
            "binge_group": "torrentio|1080p|qxr",
            "title": "Episode 1"
        }
        self.assertTrue(stream_matches_binge_group(stream1, "torrentio|1080p|qxr"))
        self.assertTrue(stream_matches_binge_group(stream1, "  TORRENTIO|1080P|QXR  "))
        self.assertFalse(stream_matches_binge_group(stream1, "watchnext|720p"))

        stream2 = {
            "behaviorHints": {
                "bingeGroup": "watchnext|720p"
            }
        }
        self.assertTrue(stream_matches_binge_group(stream2, "watchnext|720p"))
        self.assertFalse(stream_matches_binge_group(stream2, "torrentio|1080p|qxr"))

    def test_find_best_matching_stream_with_binge_group(self):
        from src.api import find_best_matching_stream

        target = {
            "addon_name": "Torrentio",
            "quality": "1080p",
            "binge_group": "torrentio|1080p|qxr",
            "filename": "show.s01e01.1080p.qxr.mkv",
            "is_http": True
        }

        candidates = [
            {
                "addon_name": "Torrentio",
                "quality": "720p",
                "binge_group": "torrentio|720p|other",
                "filename": "show.s01e02.720p.mkv",
                "is_http": True
            },
            {
                "addon_name": "Torrentio",
                "quality": "1080p",
                "binge_group": "torrentio|1080p|qxr",
                "filename": "show.s01e02.1080p.qxr.mkv",
                "is_http": True
            },
            {
                "addon_name": "WatchNext",
                "quality": "1080p",
                "binge_group": "watchnext|1080p",
                "filename": "show.s01e02.1080p.mkv",
                "is_http": True
            }
        ]

        matched = find_best_matching_stream(target, candidates)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["binge_group"], "torrentio|1080p|qxr")
        self.assertEqual(matched["filename"], "show.s01e02.1080p.qxr.mkv")

if __name__ == "__main__":
    unittest.main()
