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

    def test_all_tab_quality_grouping_and_sorting(self):
        torrents = [
            {"addon_name": "Torrentio", "quality": "4K", "is_http": False, "seeders": 100, "url": "magnet:?xt=urn:btih:1111"},
            {"addon_name": "CineStream", "quality": "1080p", "is_http": True, "url": "https://example.com/1080.mp4"},
            {"addon_name": "YTS", "quality": "720p", "is_http": False, "seeders": 50, "url": "magnet:?xt=urn:btih:2222"},
            {"addon_name": "CNC Verse", "quality": "480p", "is_http": True, "url": "https://example.com/480.m3u8"},
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

    def test_provider_matches_helper(self):
        s1 = {"addon_name": "Torrentio", "addon_names": ["Torrentio"]}
        s2 = {"addon_name": "CNC Verse"}
        s3 = {"addon_names": ["CineStream"]}

        self.assertTrue(window._stream_matches_provider(s1, "Torrentio"))
        self.assertTrue(window._stream_matches_provider(s2, "CNC Verse"))
        self.assertTrue(window._stream_matches_provider(s3, "CineStream"))
        self.assertFalse(window._stream_matches_provider(s1, "UnknownProvider"))

    def test_api_cache_merge_for_all_query(self):
        direct_streams = [{"url": "https://direct.mp4", "addon_name": "CineStream", "is_http": True}]
        torrent_streams = [{"url": "magnet:?xt=urn:btih:abc", "addon_name": "Torrentio", "is_http": False}]

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
