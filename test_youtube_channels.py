import unittest
from unittest.mock import patch
from src import player, api, database


class TestYouTubeChannels(unittest.TestCase):
    def test_extract_youtube_id(self):
        # yt_id:channel:video format
        self.assertEqual(
            player.extract_youtube_id("yt_id:UCq-Fj5jknLsUf-MWSy4_brA:yJg-Y5byMMw"),
            "yJg-Y5byMMw"
        )
        # yt_id:video format
        self.assertEqual(
            player.extract_youtube_id("yt_id:yJg-Y5byMMw"),
            "yJg-Y5byMMw"
        )
        # 11-char ID directly
        self.assertEqual(
            player.extract_youtube_id("yJg-Y5byMMw"),
            "yJg-Y5byMMw"
        )
        # Standard YouTube URLs
        self.assertEqual(
            player.extract_youtube_id("https://www.youtube.com/watch?v=yJg-Y5byMMw"),
            "yJg-Y5byMMw"
        )
        self.assertEqual(
            player.extract_youtube_id("https://youtu.be/yJg-Y5byMMw"),
            "yJg-Y5byMMw"
        )

    def test_get_torrents_streamed_for_channel_video(self):
        video_id = "yt_id:UCbCmjCuTUZos6Inko4u57UQ:TvfIAaGu1KA"
        streams = api.get_torrents_streamed(video_id, title="12 Days of Christmas")
        self.assertEqual(len(streams), 1)
        stream = streams[0]
        self.assertEqual(stream.get("ytId"), "TvfIAaGu1KA")
        self.assertEqual(stream.get("url"), "https://www.youtube.com/watch?v=TvfIAaGu1KA")
        self.assertEqual(stream.get("name"), "YouTube")
        self.assertEqual(stream.get("title"), "12 Days of Christmas")
        self.assertTrue(stream.get("is_http"))

    def test_channel_details_preserves_all_videos(self):
        mock_meta = {
            "meta": {
                "id": "yt_id:channel123",
                "name": "Cool Channel",
                "poster": "https://example.com/poster.jpg",
                "videos": [
                    {"id": "yt_id:channel123:vid1", "title": "First Video", "publishedAt": "2023-01-01"},
                    {"id": "yt_id:channel123:vid2", "title": "Second Video", "publishedAt": "2023-01-02"},
                    {"id": "yt_id:channel123:vid3", "title": "Third Video", "publishedAt": "2023-01-03"},
                ]
            }
        }
        yt_addon = {
            "id": "com.linvo.stremiochannels",
            "name": "YouTube",
            "manifest_url": "https://v3-channels.strem.io/manifest.json",
            "resources": ["catalog", "meta"],
            "types": ["channel"],
            "idPrefixes": ["yt_id:"],
            "enabled": True
        }
        def fake_get_cached(url, **kwargs):
            if "/meta/" in url:
                return mock_meta
            return None

        with patch.object(database, "get_addons", return_value=[yt_addon]):
            with patch.object(api, "_get_cached_request", side_effect=fake_get_cached):
                details = api.fetch_movie_details("yt_id:channel123", media_type="channel", use_cache=False)
                self.assertIsNotNone(details)
                videos = details.get("videos", [])
                self.assertEqual(len(videos), 3)
                self.assertEqual(videos[0]["id"], "yt_id:channel123:vid1")
                self.assertEqual(videos[1]["id"], "yt_id:channel123:vid2")
                self.assertEqual(videos[2]["id"], "yt_id:channel123:vid3")
                # Verify distinct video episode numbers are assigned
                ep_nums = [v["episode"] for v in videos]
                self.assertEqual(ep_nums, [1, 2, 3])
                # Verify video titles match
                self.assertEqual(videos[0]["title"], "First Video")
                self.assertEqual(videos[1]["title"], "Second Video")
                self.assertEqual(videos[2]["title"], "Third Video")


if __name__ == "__main__":
    unittest.main()
