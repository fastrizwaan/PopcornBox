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

    def test_sorting_individual_resolutions_direct_first(self):
        """When viewing specific resolutions (720p, 1080p, More, 4K), direct streams must come first, then torrents."""
        # 720p
        s_720_direct = {"name": "720p Direct", "quality": "720p", "is_http": True, "url": "https://direct.com/720"}
        s_720_torrent_high = {"name": "720p Torrent 120s", "quality": "720p", "is_torrent": True, "seeders": 120, "url": "magnet:?xt=urn:btih:720a"}
        s_720_torrent_low = {"name": "720p Torrent 15s", "quality": "720p", "is_torrent": True, "seeders": 15, "url": "magnet:?xt=urn:btih:720b"}
        s_720_torrent_dead = {"name": "720p Torrent 0s", "quality": "720p", "is_torrent": True, "seeders": 0, "url": "magnet:?xt=urn:btih:720c"}

        list_720 = [s_720_torrent_dead, s_720_torrent_low, s_720_torrent_high, s_720_direct]
        sorted_720 = sorted(list_720, key=window.stream_sort_key_all, reverse=True)
        self.assertEqual(sorted_720, [s_720_direct, s_720_torrent_high, s_720_torrent_low, s_720_torrent_dead])

        # 1080p
        s_1080_direct = {"name": "1080p Direct", "quality": "1080p", "is_http": True, "url": "https://direct.com/1080"}
        s_1080_torrent_high = {"name": "1080p Torrent 200s", "quality": "1080p", "is_torrent": True, "seeders": 200, "url": "magnet:?xt=urn:btih:1080a"}
        s_1080_torrent_low = {"name": "1080p Torrent 30s", "quality": "1080p", "is_torrent": True, "seeders": 30, "url": "magnet:?xt=urn:btih:1080b"}

        list_1080 = [s_1080_torrent_low, s_1080_torrent_high, s_1080_direct]
        sorted_1080 = sorted(list_1080, key=window.stream_sort_key_all, reverse=True)
        self.assertEqual(sorted_1080, [s_1080_direct, s_1080_torrent_high, s_1080_torrent_low])

        # More (480p)
        s_more_direct = {"name": "480p Direct", "quality": "480p", "is_http": True, "url": "https://direct.com/480"}
        s_more_torrent_high = {"name": "480p Torrent 80s", "quality": "480p", "is_torrent": True, "seeders": 80, "url": "magnet:?xt=urn:btih:480a"}
        s_more_torrent_low = {"name": "480p Torrent 10s", "quality": "480p", "is_torrent": True, "seeders": 10, "url": "magnet:?xt=urn:btih:480b"}

        list_more = [s_more_torrent_low, s_more_torrent_high, s_more_direct]
        sorted_more = sorted(list_more, key=window.stream_sort_key_all, reverse=True)
        self.assertEqual(sorted_more, [s_more_direct, s_more_torrent_high, s_more_torrent_low])

        # 4K
        s_4k_direct = {"name": "4K Direct", "quality": "4K", "is_http": True, "url": "https://direct.com/4k"}
        s_4k_torrent_high = {"name": "4K Torrent 150s", "quality": "4K", "is_torrent": True, "seeders": 150, "url": "magnet:?xt=urn:btih:4ka"}
        s_4k_torrent_low = {"name": "4K Torrent 20s", "quality": "4K", "is_torrent": True, "seeders": 20, "url": "magnet:?xt=urn:btih:4kb"}

        list_4k = [s_4k_torrent_low, s_4k_torrent_high, s_4k_direct]
        sorted_4k = sorted(list_4k, key=window.stream_sort_key_all, reverse=True)
        self.assertEqual(sorted_4k, [s_4k_direct, s_4k_torrent_high, s_4k_torrent_low])

    def test_on_watch_clicked_with_specific_resolution_prefers_direct(self):
        """When resolution is 720p, More, or 4K, clicking play prefers direct stream first then torrent."""
        for q_label, direct_q in [("720p", "720p"), ("More", "480p"), ("4K", "4K")]:
            page = MagicMock()
            page.media_type = "movie"
            page.is_channel = False
            page.source_idx = 0
            page.user_selected_quality = q_label
            page._user_explicitly_selected_stream = False
            page.movie_stub = {"id": "tt12345", "title": "Test Movie"}
            page.selected_season = None
            page.selected_episode = None
            page.selected_video = None
            page.provider_dropdown.get_model.return_value.get_string.return_value = "All Providers"
            page.provider_dropdown.get_model.return_value.get_n_items.return_value = 1
            page.provider_dropdown.get_selected.return_value = 0

            s_torrent = {"name": f"{q_label} Torrent 100s", "quality": direct_q, "is_torrent": True, "seeders": 100, "url": f"magnet:?xt=urn:btih:{q_label}"}
            s_direct = {"name": f"{q_label} Direct", "quality": direct_q, "is_http": True, "url": f"https://direct.com/{q_label}"}
            page.current_t_list = [s_torrent, s_direct]
            page.selected_torrent = s_torrent

            window.MovieDetailsPage.on_watch_clicked(page, None)
            self.assertEqual(page.selected_torrent, s_direct, f"Failed for resolution {q_label}")

    def test_is_torrent_addon(self):
        from src import api
        # Torrent addons
        self.assertTrue(api.is_torrent_addon({"name": "Torrentio", "manifest_url": "https://torrentio.strem.fun/manifest.json"}))
        self.assertTrue(api.is_torrent_addon({"name": "KnightCrawler", "manifest_url": "https://knightcrawler.elfhosted.com/manifest.json"}))
        self.assertTrue(api.is_torrent_addon({"name": "TorrentClaw", "id": "org.torrentclaw"}))
        self.assertTrue(api.is_torrent_addon({"name": "ThePirateBay+", "id": "tpb-addon"}))
        self.assertTrue(api.is_torrent_addon({"name": "1337x", "id": "1337x-torrents"}))
        self.assertTrue(api.is_torrent_addon({"name": "YTS.mx", "description": "Download YTS torrents"}))

        # Direct addons
        self.assertFalse(api.is_torrent_addon({"name": "IPTV-Org", "id": "local.iptv-org", "manifest_url": "https://iptv.org/manifest.json"}))
        self.assertFalse(api.is_torrent_addon({"name": "CyberFlix Catalog", "id": "cyberflix", "manifest_url": "https://cyberflix.strem.io/manifest.json"}))
        self.assertFalse(api.is_torrent_addon({"name": "Cinemeta", "id": "cinemeta", "manifest_url": "https://v3-cinemeta.strem.io/manifest.json"}))

    def test_get_torrents_streamed_direct_providers_prioritized(self):
        """Verify that get_torrents_streamed schedules direct providers first before torrent addons."""
        from src import api
        order_of_calls = []

        direct_addon = {
            "name": "DirectAddon",
            "manifest_url": "https://direct.addon/manifest.json",
            "resources": [{"name": "stream", "types": ["movie"]}],
            "types": ["movie"],
            "idPrefixes": ["tt"],
            "enabled": True,
        }
        torrent_addon = {
            "name": "Torrentio",
            "manifest_url": "https://torrentio.strem.fun/manifest.json",
            "resources": [{"name": "stream", "types": ["movie"]}],
            "types": ["movie"],
            "idPrefixes": ["tt"],
            "enabled": True,
        }

        def mock_fetch_addon(addon, cur_id):
            name = addon.get("name")
            order_of_calls.append(name)
            if name == "DirectAddon":
                return name, [{"url": "https://direct.com/stream.mp4", "name": "Direct Stream", "is_http": True, "quality": "1080p"}]
            else:
                return name, [{"url": "magnet:?xt=urn:btih:123", "name": "Torrent Stream", "is_torrent": True, "seeders": 100, "quality": "1080p"}]

        with patch("src.api.database.get_addons", return_value=[direct_addon, torrent_addon]), \
             patch("src.api.database.get_cached_streams", return_value=None), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]), \
             patch("src.api.database.save_cached_streams_full", side_effect=lambda *args, **kwargs: args[3]), \
             patch.object(api, "fetch_items", return_value=[]):

            # Patch _request_addon_streams inside api
            with patch("src.api._request_addon_streams") as mock_req:
                def fake_req(addon, url, **kwargs):
                    return mock_fetch_addon(addon, "tt12345")[1]
                mock_req.side_effect = fake_req

                results = api.get_torrents_streamed("tt12345", media_type="movie", force_refresh=True)

        # Direct addon must have been queried first!
        self.assertTrue(len(order_of_calls) >= 2)
        self.assertEqual(order_of_calls[0], "DirectAddon")
        self.assertEqual(order_of_calls[1], "Torrentio")
        # Direct stream must be ranked above torrent stream in results
        self.assertTrue(results[0].get("is_http"))

    def test_get_torrents_streamed_scoped_isolation(self):
        """Verify that source_scope='direct' skips torrent addons and source_scope='torrent' skips direct addons."""
        from src import api

        direct_addon = {
            "name": "DirectAddon",
            "manifest_url": "https://direct.addon/manifest.json",
            "resources": [{"name": "stream", "types": ["movie"]}],
            "types": ["movie"],
            "idPrefixes": ["tt"],
            "enabled": True,
        }
        torrent_addon = {
            "name": "Torrentio",
            "manifest_url": "https://torrentio.strem.fun/manifest.json",
            "resources": [{"name": "stream", "types": ["movie"]}],
            "types": ["movie"],
            "idPrefixes": ["tt"],
            "enabled": True,
        }

        # Test source_scope == "direct"
        called_addons = []
        with patch("src.api.database.get_addons", return_value=[direct_addon, torrent_addon]), \
             patch("src.api.database.get_cached_streams", return_value=None), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]), \
             patch("src.api.database.save_cached_streams_full", side_effect=lambda *args, **kwargs: args[3]), \
             patch("src.api.database.save_cached_streams_partial", side_effect=lambda *args, **kwargs: args[2]), \
             patch.object(api, "fetch_items", return_value=[]):

            with patch("src.api._request_addon_streams") as mock_req:
                def fake_req(addon, url, **kwargs):
                    called_addons.append(addon.get("name"))
                    return [{"url": "https://direct.com/stream.mp4", "name": "Direct Stream", "is_http": True, "quality": "1080p"}]
                mock_req.side_effect = fake_req

                api.get_torrents_streamed("tt12345", media_type="movie", source_scope="direct", force_refresh=True)

        self.assertIn("DirectAddon", called_addons)
        self.assertNotIn("Torrentio", called_addons)

        # Test source_scope == "torrent"
        called_addons = []
        with patch("src.api.database.get_addons", return_value=[direct_addon, torrent_addon]), \
             patch("src.api.database.get_cached_streams", return_value=None), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.cloudstream.is_cloudstream_enabled", return_value=False), \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]), \
             patch("src.api.database.save_cached_streams_full", side_effect=lambda *args, **kwargs: args[3]), \
             patch("src.api.database.save_cached_streams_partial", side_effect=lambda *args, **kwargs: args[2]), \
             patch.object(api, "fetch_items", return_value=[]):

            with patch("src.api._request_addon_streams") as mock_req:
                def fake_req(addon, url, **kwargs):
                    called_addons.append(addon.get("name"))
                    return [{"url": "magnet:?xt=urn:btih:123", "name": "Torrent Stream", "is_torrent": True, "seeders": 100, "quality": "1080p"}]
                mock_req.side_effect = fake_req

                api.get_torrents_streamed("tt12345", media_type="movie", source_scope="torrent", force_refresh=True)

        self.assertIn("Torrentio", called_addons)
        self.assertNotIn("DirectAddon", called_addons)

    def test_is_torrent_addon_pipe_and_dynamic_registration(self):
        from src import api
        pipe_addon = {
            "id": "org.boringways.pipe",
            "name": "Pipe",
            "manifest_url": "https://pipe.boringways.workers.dev/eyJsIjpbIkVuZ2xpc2giLCJIaW5kaSJdLCJ0cyI6InNlZWRlcnMifQ/manifest.json",
            "description": "All your streams in one pipe.",
        }
        self.assertTrue(api.is_torrent_addon(pipe_addon))

        custom_addon = {
            "id": "custom.unknown.provider",
            "name": "CustomProvider",
            "manifest_url": "https://unknown.com/manifest.json",
        }
        self.assertFalse(api.is_torrent_addon(custom_addon))

        # Dynamic registration
        api.mark_addon_as_torrent("CustomProvider")
        self.assertTrue(api.is_torrent_addon(custom_addon))

    def test_continue_watching_queue_pruning(self):
        test_db_dir = tempfile.mkdtemp()
        test_db_file = Path(test_db_dir) / "data.json"
        orig_db_file = database.DB_FILE
        try:
            database.DB_FILE = test_db_file
            database.reset_json_cache()

            dummy_queue = [
                {"url": f"http://example.com/{i}", "title": f"Stream {i}", "seeders": i, "subtitles": [{"lang": "en"}] * 10}
                for i in range(50)
            ]
            item = {
                "id": "tt1234567",
                "title": "Test Title",
                "stream_queue": dummy_queue,
                "stream_queue_index": 3,
                "selected_torrent": dummy_queue[3],
                "position": 100.0,
                "duration": 500.0,
            }

            database.save_continue_watching(item, sync_to_remote=False)

            cw = database.get_continue_watching()
            self.assertEqual(len(cw), 1)
            saved_item = cw[0]
            # Stream queue must be capped to at most 2 streams
            self.assertLessEqual(len(saved_item.get("stream_queue", [])), 2)
            self.assertEqual(saved_item.get("stream_queue_index"), 0)
            self.assertEqual(saved_item["stream_queue"][0]["url"], "http://example.com/3")
            # Subtitles should be capped
            self.assertLessEqual(len(saved_item["stream_queue"][0].get("subtitles", [])), 5)
        finally:
            database.DB_FILE = orig_db_file
            database.reset_json_cache()
            shutil.rmtree(test_db_dir, ignore_errors=True)

    def test_database_read_migration_prunes_bloat(self):
        test_db_dir = tempfile.mkdtemp()
        test_db_file = Path(test_db_dir) / "data.json"
        orig_db_file = database.DB_FILE
        try:
            database.DB_FILE = test_db_file
            database.reset_json_cache()

            import json
            dummy_queue = [
                {"url": f"http://example.com/{i}", "title": f"Stream {i}", "seeders": i}
                for i in range(80)
            ]
            bloated_cw = [{
                "id": "tt7654321",
                "title": "Bloated Title",
                "stream_queue": dummy_queue,
                "stream_queue_index": 2,
                "selected_torrent": dummy_queue[2]
            }]
            raw_db = {
                "favorites": [], "watched": [], "history": [],
                "continue_watching": bloated_cw, "downloads": [],
                "settings": {}, "addons": database.DEFAULT_ADDONS
            }
            with open(test_db_file, "w") as f:
                json.dump(raw_db, f, indent=4)

            # _read_db should migrate and prune
            data = database._read_db()
            cw_item = data["continue_watching"][0]
            self.assertLessEqual(len(cw_item["stream_queue"]), 2)
            self.assertEqual(cw_item["stream_queue_index"], 0)
            self.assertEqual(cw_item["stream_queue"][0]["url"], "http://example.com/2")
        finally:
            database.DB_FILE = orig_db_file
            database.reset_json_cache()
            shutil.rmtree(test_db_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
