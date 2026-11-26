import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    _dest = Path(_schema_dir.name) / _schema_path.name
    _dest.write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

from gi.repository import Gio
res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import api, window


class TestYouTubeStreamSorting(unittest.TestCase):
    def test_is_youtube_stream_detection(self):
        """Verify is_youtube_stream detects various YouTube stream formats and rejects non-YouTube streams."""
        # 1. ytId present
        self.assertTrue(api.is_youtube_stream({"ytId": "dQw4w9WgXcQ"}))
        self.assertTrue(api.is_youtube_stream({"youtube_id": "dQw4w9WgXcQ"}))

        # 2. explicit flag
        self.assertTrue(api.is_youtube_stream({"is_youtube": True}))

        # 3. custom plugin
        self.assertTrue(api.is_youtube_stream({"custom_plugin_id": "custom.youtube"}))

        # 4. YouTube watch / share URLs
        self.assertTrue(api.is_youtube_stream({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}))
        self.assertTrue(api.is_youtube_stream({"url": "https://youtu.be/dQw4w9WgXcQ"}))
        self.assertTrue(api.is_youtube_stream({"stream_url": "https://m.youtube.com/watch?v=xyz"}))
        self.assertTrue(api.is_youtube_stream({"externalUrl": "https://www.youtube.com/watch?v=abc"}))
        self.assertTrue(api.is_youtube_stream({"url": "https://rr1---sn-abc.googlevideo.com/videoplayback?id=123"}))

        # 5. Addon names
        self.assertTrue(api.is_youtube_stream({"addon_name": "YouTube"}))
        self.assertTrue(api.is_youtube_stream({"addon_name": "YoutubeProvider"}))
        self.assertTrue(api.is_youtube_stream({"addon_names": ["YouTube", "Other"]}))
        self.assertTrue(api.is_youtube_stream({"addon_name": "YouTube Movies"}))

        # 6. Stream name format
        self.assertTrue(api.is_youtube_stream({"name": "YouTube [1080p]"}))
        self.assertTrue(api.is_youtube_stream({"name": "YouTube • Channel • 1:20:00"}))

        # 7. Non-YouTube streams must NOT be identified as YouTube
        self.assertFalse(api.is_youtube_stream({
            "name": "Torrentio\n1080p",
            "title": "YouTube: The Documentary (2020) 1080p",
            "is_torrent": True,
            "infoHash": "abcdef1234567890",
            "seeders": 150
        }))
        self.assertFalse(api.is_youtube_stream({
            "name": "VidSrc [1080p]",
            "url": "https://vidsrc.stream/play.mp4",
            "is_http": True,
            "addon_name": "VidSrc"
        }))
        self.assertFalse(api.is_youtube_stream({
            "name": "Local Torrent",
            "infoHash": "11223344556677889900",
            "is_torrent": True
        }))

    def test_process_raw_streams_ranks_youtube_below_other_streams(self):
        """Verify that process_raw_streams in api.py puts YouTube streams below other streams."""
        raw_streams = [
            # YouTube stream (1080p)
            {
                "name": "YouTube [1080p]",
                "title": "Full Movie on YouTube\nNight of the Living Dead",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "quality": "1080p",
                "is_http": True,
                "addon_name": "YouTube",
                "custom_plugin_id": "custom.youtube"
            },
            # 720p Torrent with 15 seeds
            {
                "name": "Torrentio 720p",
                "title": "Night.of.the.Living.Dead.720p.WEB-DL",
                "infoHash": "720a" * 10,
                "quality": "720p",
                "seeders": 15,
                "is_torrent": True
            },
            # 1080p Torrent with 120 seeds
            {
                "name": "Torrentio 1080p",
                "title": "Night.of.the.Living.Dead.1080p.BluRay",
                "infoHash": "1080a" * 10,
                "quality": "1080p",
                "seeders": 120,
                "is_torrent": True
            },
            # Direct HTTP stream from another provider (1080p)
            {
                "name": "Nuvio Direct [1080p]",
                "title": "Night of the Living Dead 1080p Direct",
                "url": "https://provider.stream/video.mp4",
                "quality": "1080p",
                "is_http": True,
                "addon_name": "NuvioDirect"
            },
            # 4K Torrent with 50 seeds
            {
                "name": "Torrentio 4K",
                "title": "Night.of.the.Living.Dead.2160p.UHD",
                "infoHash": "4ka" * 10,
                "quality": "4K",
                "seeders": 50,
                "is_torrent": True
            },
            # Dead torrent with 0 seeds
            {
                "name": "Torrentio Dead",
                "title": "Night.of.the.Living.Dead.1080p.Dead",
                "infoHash": "deada" * 10,
                "quality": "1080p",
                "seeders": 0,
                "is_torrent": True
            },
            # Another YouTube stream (720p)
            {
                "name": "YouTube [720p]",
                "title": "Movie Clip on YouTube\nNight of the Living Dead",
                "url": "https://www.youtube.com/watch?v=another720",
                "quality": "720p",
                "is_http": True,
                "addon_name": "YouTube"
            }
        ]

        parsed = api.process_raw_streams(raw_streams)

        # YouTube streams must be at the very bottom, below all other stream results
        names = [s.get("title") or s.get("name") for s in parsed]
        yt_indices = [i for i, s in enumerate(parsed) if s.get("is_youtube") or api.is_youtube_stream(s)]
        non_yt_indices = [i for i, s in enumerate(parsed) if not (s.get("is_youtube") or api.is_youtube_stream(s))]

        # Verify that all non-YouTube streams come before all YouTube streams
        max_non_yt = max(non_yt_indices)
        min_yt = min(yt_indices)
        self.assertLess(max_non_yt, min_yt, f"Non-YouTube streams must come before YouTube streams: {names}")

        # Verify that between the YouTube streams, 1080p is ranked before 720p
        yt_streams = [s for s in parsed if s.get("is_youtube") or api.is_youtube_stream(s)]
        self.assertEqual(yt_streams[0].get("title") or yt_streams[0].get("name"), "YouTube [1080p]")
        self.assertEqual(yt_streams[1].get("title") or yt_streams[1].get("name"), "YouTube [720p]")

    def test_stream_sort_key_all_ranks_youtube_below_other_streams(self):
        """Verify stream_sort_key_all puts YouTube streams below other direct streams and torrents."""
        s_direct_1080 = {"name": "1080p Direct", "quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        s_torrent_1080_high = {"name": "1080p Torrent 150s", "quality": "1080p", "is_torrent": True, "seeders": 150, "url": "magnet:?xt=urn:btih:1080a"}
        s_torrent_1080_low = {"name": "1080p Torrent 20s", "quality": "1080p", "is_torrent": True, "seeders": 20, "url": "magnet:?xt=urn:btih:1080b"}
        s_torrent_dead = {"name": "1080p Torrent 0s", "quality": "1080p", "is_torrent": True, "seeders": 0, "url": "magnet:?xt=urn:btih:dead"}
        s_youtube_1080 = {
            "name": "YouTube [1080p]",
            "quality": "1080p",
            "is_http": True,
            "url": "https://www.youtube.com/watch?v=123",
            "addon_name": "YouTube",
            "custom_plugin_id": "custom.youtube"
        }
        s_youtube_720 = {
            "name": "YouTube [720p]",
            "quality": "720p",
            "is_http": True,
            "url": "https://www.youtube.com/watch?v=456",
            "addon_name": "YouTube"
        }

        all_streams = [
            s_youtube_720,
            s_torrent_1080_low,
            s_youtube_1080,
            s_direct_1080,
            s_torrent_dead,
            s_torrent_1080_high,
        ]

        sorted_streams = sorted(all_streams, key=window.stream_sort_key_all, reverse=True)

        # Non-YouTube streams must come first
        self.assertIn(sorted_streams[0], [s_direct_1080, s_torrent_1080_high])
        self.assertIn(sorted_streams[1], [s_direct_1080, s_torrent_1080_high])
        self.assertEqual(sorted_streams[2], s_torrent_1080_low)
        self.assertEqual(sorted_streams[3], s_torrent_dead)

        # YouTube streams must be at the bottom, with 1080p above 720p
        self.assertEqual(sorted_streams[4], s_youtube_1080)
        self.assertEqual(sorted_streams[5], s_youtube_720)

    def test_remembered_stream_takes_precedence_over_sorting(self):
        """If user explicitly selected and remembered a YouTube stream, it stays top."""
        s_torrent = {"name": "1080p Torrent 200s", "quality": "1080p", "is_torrent": True, "seeders": 200, "url": "magnet:?xt=urn:btih:1080"}
        s_youtube = {
            "name": "YouTube [1080p]",
            "quality": "1080p",
            "is_http": True,
            "url": "https://www.youtube.com/watch?v=remembered_vid",
            "addon_name": "YouTube"
        }

        # Normal sorting: torrent is ahead of YouTube
        normal_sorted = sorted([s_youtube, s_torrent], key=window.stream_sort_key_all, reverse=True)
        self.assertEqual(normal_sorted[0], s_torrent)

        # Remembered YouTube stream: ranks first
        rem_sorted = sorted(
            [s_torrent, s_youtube],
            key=lambda t: window.stream_sort_key_all(t, rem_st=s_youtube),
            reverse=True
        )
        self.assertEqual(rem_sorted[0], s_youtube)

    def test_live_check_sorting_keeps_youtube_at_bottom(self):
        """⚡ Check Direct sorting must keep YouTube streams below non-YouTube direct streams."""
        streams = [
            {"name": "YouTube [1080p]", "is_http": True, "ping_status": True, "url": "https://www.youtube.com/watch?v=yt1"},
            {"name": "Provider Direct [1080p]", "is_http": True, "ping_status": True, "url": "https://direct.com/video.mp4"},
            {"name": "Provider Direct 2 [720p]", "is_http": True, "ping_status": None, "url": "https://direct2.com/video.mp4"},
        ]

        streams.sort(key=lambda x: (
            1 if window.is_youtube_stream(x) else 0,
            0 if (x.get('is_http') and x.get('ping_status') is True) else (1 if x.get('is_http') and x.get('ping_status') is None else (2 if x.get('is_http') else 3))
        ))

        # Provider Direct (working) -> Provider Direct 2 (unknown) -> YouTube (even though ping_status is True)
        self.assertEqual(streams[0]["name"], "Provider Direct [1080p]")
        self.assertEqual(streams[1]["name"], "Provider Direct 2 [720p]")
        self.assertEqual(streams[2]["name"], "YouTube [1080p]")


if __name__ == "__main__":
    unittest.main()
