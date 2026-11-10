import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

from gi.repository import Gio
res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    _dest = Path(_schema_dir.name) / _schema_path.name
    _dest.write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

from src import api, database, window


class TestAllTabAndProviders(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"

        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        database.reset_json_cache()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_quality_normalize_includes_all(self):
        self.assertEqual(window._normalize_stream_quality("All"), "All")
        self.assertEqual(window._normalize_stream_quality("1080p"), "1080p")
        self.assertEqual(window._normalize_stream_quality("4K"), "4K")

    def test_is_torrent_and_is_direct_stream_classification(self):
        # 1. Magnet link
        s_magnet = {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"}
        self.assertTrue(api.is_torrent_stream(s_magnet))
        self.assertFalse(api.is_direct_stream(s_magnet))
        self.assertTrue(window.is_torrent_stream(s_magnet))
        self.assertFalse(window.is_direct_stream(s_magnet))

        # 2. Torrent with infoHash
        s_hash = {"infoHash": "0123456789abcdef0123456789abcdef01234567", "url": "https://debrid.com/stream"}
        self.assertTrue(api.is_torrent_stream(s_hash))
        self.assertFalse(api.is_direct_stream(s_hash))

        # 3. .torrent file url
        s_torrent_file = {"url": "https://yts.mx/torrent/download/12345.torrent"}
        self.assertTrue(api.is_torrent_stream(s_torrent_file))
        self.assertFalse(api.is_direct_stream(s_torrent_file))

        # 4. Pure direct HTTP / HLS stream
        s_http = {"url": "https://cdn.example.com/stream.m3u8", "is_http": True}
        self.assertFalse(api.is_torrent_stream(s_http))
        self.assertTrue(api.is_direct_stream(s_http))

        # 5. Direct DASH stream
        s_dash = {"url": "https://cdn.example.com/manifest.mpd", "stream_type": "dash"}
        self.assertFalse(api.is_torrent_stream(s_dash))
        self.assertTrue(api.is_direct_stream(s_dash))

    def test_all_tab_quality_grouping_and_sorting(self):
        torrents = [
            {"addon_name": "Torrentio", "quality": "4K", "is_http": False, "is_torrent": True, "seeders": 100, "url": "magnet:?xt=urn:btih:1111"},
            {"addon_name": "CineStream", "quality": "1080p", "is_http": True, "is_torrent": False, "url": "https://example.com/1080.mp4"},
            {"addon_name": "YTS", "quality": "720p", "is_http": False, "is_torrent": True, "seeders": 50, "url": "magnet:?xt=urn:btih:2222"},
            {"addon_name": "CNC Verse", "quality": "480p", "is_http": True, "is_torrent": False, "url": "https://example.com/480.m3u8"},
        ]

        quality_groups = {"All": list(torrents), "4K": [], "1080p": [], "720p": [], "More": []}
        for t in torrents:
            q_norm = window._normalize_stream_quality(t)
            if q_norm in quality_groups and q_norm != "All":
                quality_groups[q_norm].append(t)
            else:
                quality_groups["More"].append(t)

        self.assertEqual(len(quality_groups["All"]), 4)
        self.assertEqual(len(quality_groups["4K"]), 1)
        self.assertEqual(len(quality_groups["1080p"]), 1)
        self.assertEqual(len(quality_groups["720p"]), 1)
        self.assertEqual(len(quality_groups["More"]), 1)

    def test_source_and_quality_filtering_combinations(self):
        sample_streams = [
            {"addon_name": "Torrentio", "quality": "4K", "is_torrent": True, "is_http": False, "url": "magnet:?xt=urn:btih:1"},
            {"addon_name": "Torrentio", "quality": "1080p", "is_torrent": True, "is_http": False, "url": "magnet:?xt=urn:btih:2"},
            {"addon_name": "YTS", "quality": "720p", "is_torrent": True, "is_http": False, "url": "magnet:?xt=urn:btih:3"},
            {"addon_name": "CNC Verse", "quality": "4K", "is_torrent": False, "is_http": True, "url": "https://example.com/4k.m3u8"},
            {"addon_name": "CineStream", "quality": "1080p", "is_torrent": False, "is_http": True, "url": "https://example.com/1080.m3u8"},
            {"addon_name": "DudeFilms", "quality": "720p", "is_torrent": False, "is_http": True, "url": "https://example.com/720.m3u8"},
        ]

        # 1. All + All: All torrents and all direct streams across all resolutions
        all_all = [t for t in sample_streams]
        self.assertEqual(len(all_all), 6)
        self.assertEqual(len([t for t in all_all if window.is_torrent_stream(t)]), 3)
        self.assertEqual(len([t for t in all_all if window.is_direct_stream(t)]), 3)

        # 2. Direct + All: Only direct streams across all resolutions, NO torrents
        direct_all = [t for t in sample_streams if window.is_direct_stream(t)]
        self.assertEqual(len(direct_all), 3)
        self.assertTrue(all(window.is_direct_stream(t) for t in direct_all))
        self.assertFalse(any(window.is_torrent_stream(t) for t in direct_all))

        # 3. Torrents + All: Only torrents across all resolutions, NO direct streams
        torrents_all = [t for t in sample_streams if window.is_torrent_stream(t)]
        self.assertEqual(len(torrents_all), 3)
        self.assertTrue(all(window.is_torrent_stream(t) for t in torrents_all))
        self.assertFalse(any(window.is_direct_stream(t) for t in torrents_all))

        # 4. All + 1080p: Both torrent and direct streams for 1080p
        all_1080 = [t for t in sample_streams if window._normalize_stream_quality(t) == "1080p"]
        self.assertEqual(len(all_1080), 2)
        self.assertEqual(len([t for t in all_1080 if window.is_torrent_stream(t)]), 1)
        self.assertEqual(len([t for t in all_1080 if window.is_direct_stream(t)]), 1)

        # 5. Direct + 1080p: Only direct streams for 1080p
        direct_1080 = [t for t in sample_streams if window.is_direct_stream(t) and window._normalize_stream_quality(t) == "1080p"]
        self.assertEqual(len(direct_1080), 1)
        self.assertEqual(direct_1080[0]["addon_name"], "CineStream")

        # 6. Torrents + 1080p: Only torrent streams for 1080p
        torrents_1080 = [t for t in sample_streams if window.is_torrent_stream(t) and window._normalize_stream_quality(t) == "1080p"]
        self.assertEqual(len(torrents_1080), 1)
        self.assertEqual(torrents_1080[0]["addon_name"], "Torrentio")

    def test_provider_matches_helper(self):
        s1 = {"addon_name": "Torrentio", "addon_names": ["Torrentio"]}
        s2 = {"addon_name": "CNC Verse"}
        s3 = {"addon_names": ["CineStream"]}

        self.assertTrue(window._stream_matches_provider(s1, "Torrentio"))
        self.assertTrue(window._stream_matches_provider(s2, "CNC Verse"))
        self.assertTrue(window._stream_matches_provider(s3, "CineStream"))
        self.assertFalse(window._stream_matches_provider(s1, "UnknownProvider"))

    def test_api_cache_merge_for_all_query(self):
        direct_streams = [{"url": "https://direct.mp4", "addon_name": "CineStream", "is_http": True, "is_torrent": False}]
        torrent_streams = [{"url": "magnet:?xt=urn:btih:abc", "addon_name": "Torrentio", "is_http": False, "is_torrent": True}]

        key = "tt_test_movie:movie"
        d_key = key + ":source:direct"
        t_key = key + ":source:torrent"

        database.save_cached_streams(d_key, direct_streams)
        database.save_cached_streams(t_key, torrent_streams)

        # Call get_torrents_streamed with source_scope=None (All)
        with patch("src.api.plugins.is_plugins_enabled", return_value=False), \
             patch("src.api.database.get_addons", return_value=[]), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False):
            res = api.get_torrents_streamed("tt_test_movie", media_type="movie", source_scope=None)

        self.assertEqual(len(res), 2)
        urls = [s.get("url") for s in res]
        self.assertIn("https://direct.mp4", urls)
        self.assertIn("magnet:?xt=urn:btih:abc", urls)


if __name__ == "__main__":
    unittest.main()
