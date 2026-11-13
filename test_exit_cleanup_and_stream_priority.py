import os
import shutil
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
res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database, player, window


class TestExitCleanupAndStreamPriority(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_download_base = player.DOWNLOAD_BASE
        player.DOWNLOAD_BASE = os.path.join(self.temp_dir, "torrents")
        os.makedirs(player.DOWNLOAD_BASE, exist_ok=True)

        self.cache_dir = os.path.join(self.temp_dir, "cache")
        os.makedirs(os.path.join(self.cache_dir, "api"), exist_ok=True)
        os.makedirs(os.path.join(self.cache_dir, "images"), exist_ok=True)

        player._cleanup_performed = False

    def tearDown(self):
        player.DOWNLOAD_BASE = self.orig_download_base
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        player._cleanup_performed = False

    def test_cleanup_torrents_and_cache_on_exit(self):
        # Create dummy torrent files
        torrent_hash_dir = os.path.join(player.DOWNLOAD_BASE, "abc123hash")
        os.makedirs(torrent_hash_dir, exist_ok=True)
        with open(os.path.join(torrent_hash_dir, "video.mkv"), "wb") as f:
            f.write(b"data" * 100)

        # Create dummy cache files
        with open(os.path.join(self.cache_dir, "api", "cached_resp.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.cache_dir, "images", "poster.jpg"), "wb") as f:
            f.write(b"image")

        with patch("src.player.database._is_testing", return_value=False), \
             patch("src.player.database.clear_downloads") as mock_clear_dl, \
             patch("src.player.database.clear_all_cached_streams_async") as mock_clear_streams:

            # Patch cache_dirs in player to point to our test cache_dir
            with patch("src.api.BASE_DIR", self.cache_dir):
                player.cleanup_torrents_and_cache_on_exit()

        # Torrents folder must be emptied
        self.assertEqual(len(os.listdir(player.DOWNLOAD_BASE)), 0)
        self.assertFalse(os.path.exists(torrent_hash_dir))

        # Cache folder contents must be emptied (with empty api and images dirs recreated)
        self.assertEqual(len(os.listdir(os.path.join(self.cache_dir, "api"))), 0)
        self.assertEqual(len(os.listdir(os.path.join(self.cache_dir, "images"))), 0)

        mock_clear_dl.assert_called_once()
        mock_clear_streams.assert_called_once()

    def test_stream_priority_tiers(self):
        # 1. 1080p Direct
        s_1080_direct = {"quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        self.assertEqual(window.get_stream_priority_tier(s_1080_direct), 120)

        # 2. 1080p Torrent > 50 seeds
        s_1080_torrent_high = {"quality": "1080p", "is_torrent": True, "seeders": 85, "url": "magnet:?xt=urn:btih:1"}
        self.assertEqual(window.get_stream_priority_tier(s_1080_torrent_high), 110)

        # 3. 720p Direct
        s_720_direct = {"quality": "720p", "is_http": True, "url": "https://direct.com/720"}
        self.assertEqual(window.get_stream_priority_tier(s_720_direct), 100)

        # 4. 720p Torrent > 50 seeds
        s_720_torrent_high = {"quality": "720p", "is_torrent": True, "seeders": 60, "url": "magnet:?xt=urn:btih:2"}
        self.assertEqual(window.get_stream_priority_tier(s_720_torrent_high), 90)

        # 5. More Direct (e.g. 480p)
        s_more_direct = {"quality": "480p", "is_http": True, "url": "https://direct.com/480"}
        self.assertEqual(window.get_stream_priority_tier(s_more_direct), 80)

        # 6. More Torrent > 50 seeds
        s_more_torrent_high = {"quality": "480p", "is_torrent": True, "seeders": 95, "url": "magnet:?xt=urn:btih:3"}
        self.assertEqual(window.get_stream_priority_tier(s_more_torrent_high), 70)

        # 7. 4K Direct
        s_4k_direct = {"quality": "4K", "is_http": True, "url": "https://direct.com/4k"}
        self.assertEqual(window.get_stream_priority_tier(s_4k_direct), 60)

        # 8. 4K Torrent > 50 seeds
        s_4k_torrent_high = {"quality": "4K", "is_torrent": True, "seeders": 150, "url": "magnet:?xt=urn:btih:4"}
        self.assertEqual(window.get_stream_priority_tier(s_4k_torrent_high), 50)

        # 9. 1080p Torrent <= 50 seeds
        s_1080_torrent_low = {"quality": "1080p", "is_torrent": True, "seeders": 25, "url": "magnet:?xt=urn:btih:5"}
        self.assertEqual(window.get_stream_priority_tier(s_1080_torrent_low), 40)

        # 10. 720p Torrent <= 50 seeds
        s_720_torrent_low = {"quality": "720p", "is_torrent": True, "seeders": 15, "url": "magnet:?xt=urn:btih:6"}
        self.assertEqual(window.get_stream_priority_tier(s_720_torrent_low), 30)

        # 11. More Torrent <= 50 seeds
        s_more_torrent_low = {"quality": "480p", "is_torrent": True, "seeders": 5, "url": "magnet:?xt=urn:btih:7"}
        self.assertEqual(window.get_stream_priority_tier(s_more_torrent_low), 20)

        # 12. 4K Torrent <= 50 seeds
        s_4k_torrent_low = {"quality": "4K", "is_torrent": True, "seeders": 20, "url": "magnet:?xt=urn:btih:8"}
        self.assertEqual(window.get_stream_priority_tier(s_4k_torrent_low), 15)

    def test_sorting_sequence_matches_user_specification(self):
        """Sequence: 1080p -> direct then torrents with >50 seeds; next try 720p -> direct then torrents with >50 seeds, and same for more, then try 4k"""
        s_4k_torrent_200 = {"name": "4K Torrent 200s", "quality": "4K", "is_torrent": True, "seeders": 200, "url": "magnet:?xt=urn:btih:4k"}
        s_1080_direct = {"name": "1080p Direct", "quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        s_1080_torrent_75 = {"name": "1080p Torrent 75s", "quality": "1080p", "is_torrent": True, "seeders": 75, "url": "magnet:?xt=urn:btih:1080"}
        s_720_direct = {"name": "720p Direct", "quality": "720p", "is_http": True, "url": "https://direct.com/720"}
        s_720_torrent_60 = {"name": "720p Torrent 60s", "quality": "720p", "is_torrent": True, "seeders": 60, "url": "magnet:?xt=urn:btih:720"}
        s_more_direct = {"name": "More Direct", "quality": "480p", "is_http": True, "url": "https://direct.com/480"}
        s_more_torrent_90 = {"name": "More Torrent 90s", "quality": "480p", "is_torrent": True, "seeders": 90, "url": "magnet:?xt=urn:btih:480"}
        s_4k_direct = {"name": "4K Direct", "quality": "4K", "is_http": True, "url": "https://direct.com/4k"}
        s_1080_torrent_10 = {"name": "1080p Torrent 10s", "quality": "1080p", "is_torrent": True, "seeders": 10, "url": "magnet:?xt=urn:btih:1080low"}

        streams = [
            s_4k_torrent_200,
            s_more_torrent_90,
            s_720_torrent_60,
            s_1080_torrent_10,
            s_4k_direct,
            s_720_direct,
            s_1080_torrent_75,
            s_more_direct,
            s_1080_direct,
        ]

        def sort_key(t):
            tier = window.get_stream_priority_tier(t)
            seeds = int(t.get("seeders") or 0)
            return (tier, seeds)

        sorted_streams = sorted(streams, key=sort_key, reverse=True)

        expected_order = [
            s_1080_direct,       # 1. 1080p direct (tier 120)
            s_1080_torrent_75,   # 2. 1080p torrent >50s (tier 110)
            s_720_direct,        # 3. 720p direct (tier 100)
            s_720_torrent_60,    # 4. 720p torrent >50s (tier 90)
            s_more_direct,       # 5. More direct (tier 80)
            s_more_torrent_90,   # 6. More torrent >50s (tier 70)
            s_4k_direct,         # 7. 4K direct (tier 60)
            s_4k_torrent_200,    # 8. 4K torrent >50s (tier 50)
            s_1080_torrent_10,   # 9. 1080p torrent <=50s (tier 40)
        ]

        self.assertEqual(sorted_streams, expected_order)

    def test_on_watch_clicked_prioritizes_1080p_direct_when_all_selected(self):
        page = MagicMock()
        page.media_type = "movie"
        page.is_channel = False
        page.source_idx = 0
        page.user_selected_quality = "All"
        page._user_explicitly_selected_stream = False
        page.movie_stub = {"id": "tt12345", "title": "Test Movie"}
        page.selected_season = None
        page.selected_episode = None
        page.selected_video = None
        page.provider_dropdown.get_model.return_value.get_string.return_value = "All Providers"
        page.provider_dropdown.get_model.return_value.get_n_items.return_value = 1
        page.provider_dropdown.get_selected.return_value = 0

        s_4k_torrent = {"name": "4K Torrent", "quality": "4K", "is_torrent": True, "seeders": 100, "url": "magnet:?xt=urn:btih:4k"}
        s_1080_direct = {"name": "1080p Direct", "quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        page.current_t_list = [s_4k_torrent, s_1080_direct]
        page.selected_torrent = s_4k_torrent

        # Simulate on_watch_clicked execution logic on page
        window.MovieDetailsPage.on_watch_clicked(page, None)

        self.assertEqual(page.selected_torrent, s_1080_direct)

    def test_on_watch_clicked_honors_explicit_user_selection(self):
        page = MagicMock()
        page.media_type = "movie"
        page.is_channel = False
        page.source_idx = 0
        page.user_selected_quality = "All"
        page._user_explicitly_selected_stream = True
        page.movie_stub = {"id": "tt12345", "title": "Test Movie"}
        page.selected_season = None
        page.selected_episode = None
        page.selected_video = None
        page.provider_dropdown.get_model.return_value.get_string.return_value = "All Providers"
        page.provider_dropdown.get_model.return_value.get_n_items.return_value = 1
        page.provider_dropdown.get_selected.return_value = 0

        s_4k_torrent = {"name": "4K Torrent", "quality": "4K", "is_torrent": True, "seeders": 100, "url": "magnet:?xt=urn:btih:4k"}
        s_1080_direct = {"name": "1080p Direct", "quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        page.current_t_list = [s_4k_torrent, s_1080_direct]
        page.selected_torrent = s_4k_torrent

        # Simulate on_watch_clicked execution logic on page
        window.MovieDetailsPage.on_watch_clicked(page, None)

        # Since user explicitly selected s_4k_torrent, it must NOT be overridden
        self.assertEqual(page.selected_torrent, s_4k_torrent)


if __name__ == "__main__":
    unittest.main()
