import unittest
import re
from src import api, database

class TestEpisodeMatching(unittest.TestCase):
    def setUp(self):
        self.raw_streams = [
            {
                "name": "4KHDHub HubDrive 1080P",
                "title": "Jaadugar.A.Witch.in.Mongolia.S01E01.All.That.Is.In.the.Heavens.and.All.That.Is.In.the.Earth.1080p.REPACK.AMZN.WEB-DL.Multi.DDP2.0.H.264-4kHd-Hub.Com.mkv\nSize: 1.66 GB",
                "url": "https://example.com/e1.mkv",
                "size": "1.66 GB",
                "is_http": True,
            },
            {
                "name": "4KHDHub HubDrive 1080P",
                "title": "Jaadugar.A.Witch.in.Mongolia.S01E02.The.Roses.That.Bloom.m.In.Safar.1080p.AMZN.WEB-DL.Multi.DDP2.0.H.265-4kHd-Hub.Com.mkv\nSize: 1.35 GB",
                "url": "https://example.com/e2.mkv",
                "size": "1.35 GB",
                "is_http": True,
            },
            {
                "name": "4KHDHub HubDrive 1080P",
                "title": "Jaadugar.A.Witch.in.Mongolia.S01E01.All.That.Is.In.the.Heavens.and.All.That.Is.In.the.Earth.1080p.REPACK.AMZN.WEB-DL.Multi.DDP2.0.H.265-4kHd-Hub.Com.mkv\nSize: 1.25 GB",
                "url": "https://example.com/e1_v2.mkv",
                "size": "1.25 GB",
                "is_http": True,
            },
            {
                "name": "4KHDHub 1080p",
                "title": "[FSL] [ 1.64 GB] Jaadugar.A.Witch.in.Mongolia.S01E03.An.Undying.Flame.1080p.AMZN.WEB-DL.Multi.DDP2.0.H.264-4kHdHub.Co",
                "url": "https://example.com/e3_fsl.mkv",
                "size": "1.64 GB",
                "is_http": True,
            },
            {
                "name": "4KHDHub 1080p",
                "title": "[PixelDrain] [ 1.64 GB] Jaadugar.A.Witch.in.Mongolia.S01E03.An.Undying.Flame.1080p.AMZN.WEB-DL.Multi.DDP2.0.H.264-4kHdHub.Co",
                "url": "https://example.com/e3_pixeldrain.mkv",
                "size": "1.64 GB",
                "is_http": True,
            }
        ]

    def test_exact_screenshot_classification(self):
        # Target: Season 1, Episode 3
        self.assertEqual(api.match_stream_to_episode(self.raw_streams[0], 1, 3), api.MATCH_MISMATCH)
        self.assertEqual(api.match_stream_to_episode(self.raw_streams[1], 1, 3), api.MATCH_MISMATCH)
        self.assertEqual(api.match_stream_to_episode(self.raw_streams[2], 1, 3), api.MATCH_MISMATCH)
        self.assertEqual(api.match_stream_to_episode(self.raw_streams[3], 1, 3, "An Undying Flame"), api.MATCH_EXACT)
        self.assertEqual(api.match_stream_to_episode(self.raw_streams[4], 1, 3, "An Undying Flame"), api.MATCH_EXACT)

    def test_process_raw_streams_filtering_for_s1e3(self):
        # When querying S1E3, S01E01 and S01E02 must be completely excluded!
        res = api.process_raw_streams(self.raw_streams, season=1, episode=3, ep_title="An Undying Flame")
        self.assertEqual(len(res), 2)
        for s in res:
            self.assertIn("S01E03", s["stream_title"])
            self.assertEqual(s["ep_match"], api.MATCH_EXACT)

    def test_process_raw_streams_filtering_for_s1e1(self):
        # When querying S1E1, S01E02 and S01E03 must be excluded!
        res = api.process_raw_streams(self.raw_streams, season=1, episode=1)
        self.assertEqual(len(res), 2)
        for s in res:
            self.assertIn("S01E01", s["stream_title"])
            self.assertEqual(s["ep_match"], api.MATCH_EXACT)

    def test_process_raw_streams_filtering_for_s1e2(self):
        # When querying S1E2, S01E01 and S01E03 must be excluded!
        res = api.process_raw_streams(self.raw_streams, season=1, episode=2)
        self.assertEqual(len(res), 1)
        self.assertIn("S01E02", res[0]["stream_title"])
        self.assertEqual(res[0]["ep_match"], api.MATCH_EXACT)

    def test_episode_ranges(self):
        range_stream = {"stream_title": "Jaadugar.S01E01-E04.1080p.mkv"}
        # S1E3 is inside 1-4
        self.assertEqual(api.match_stream_to_episode(range_stream, 1, 3), api.MATCH_EXACT)
        # S1E5 is outside 1-4
        self.assertEqual(api.match_stream_to_episode(range_stream, 1, 5), api.MATCH_MISMATCH)

    def test_anime_and_standalone(self):
        anime_ep3 = {"stream_title": "[Subs] Jaadugar - 03 [1080p].mkv"}
        self.assertEqual(api.match_stream_to_episode(anime_ep3, 1, 3), api.MATCH_EXACT)
        self.assertEqual(api.match_stream_to_episode(anime_ep3, 1, 1), api.MATCH_MISMATCH)

        ep_standalone = {"stream_title": "Jaadugar Episode 03 1080p"}
        self.assertEqual(api.match_stream_to_episode(ep_standalone, 1, 3), api.MATCH_EXACT)
        self.assertEqual(api.match_stream_to_episode(ep_standalone, 1, 2), api.MATCH_MISMATCH)

    def test_resolution_avoidance(self):
        dim_stream = {"stream_title": "Jaadugar 1920x1080 WEB-DL"}
        # Should not think 1920 is season or 1080 is episode
        self.assertEqual(api.match_stream_to_episode(dim_stream, 1, 3), api.MATCH_UNKNOWN)

    def test_database_working_stream_rejection_of_wrong_episode(self):
        # If database has a poisoned stream for S1E1, it must not be returned when querying S1E3
        poisoned = {
            "stream_title": "Jaadugar.S01E01.1080p.mkv",
            "url": "https://example.com/e1.mkv",
            "is_http": True
        }
        database.save_working_stream("test_show_123", season=1, episode=1, stream_info=poisoned)
        # Direct query for S1E3 should NOT return the S1E1 stream
        self.assertIsNone(database.get_working_stream("test_show_123", season=1, episode=3))

if __name__ == "__main__":
    unittest.main()
