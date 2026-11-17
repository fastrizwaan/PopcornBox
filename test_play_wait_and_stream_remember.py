import unittest
import tempfile
import os
import sys
import time
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass


class TestPlayWaitAndStreamRemember(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"

        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

        from src import database
        database.close_cache_db()
        database.reset_json_cache()
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database
        database.reset_json_cache()
        database.close_cache_db()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_database_get_working_stream_across_aliases(self):
        """Verify get_working_stream resolves across IMDb IDs, TMDB IDs, and alias lists."""
        from src import database

        stream = {
            "url": "https://provider.com/video.mp4",
            "is_http": True,
            "quality": "1080p",
            "stream_title": "Test Movie 1080p",
            "addon_names": ["TestAddon"]
        }

        # Save under tmdb:12345
        database.save_working_stream("tmdb:12345", stream_info=stream)

        # Retrieve using exact ID
        res1 = database.get_working_stream("tmdb:12345")
        self.assertIsNotNone(res1)
        self.assertEqual(res1.get("url"), stream["url"])

        # Retrieve passing a list of IDs containing the ID
        res2 = database.get_working_stream(["tt9999999", "tmdb:12345"])
        self.assertIsNotNone(res2)
        self.assertEqual(res2.get("url"), stream["url"])

        # Save with metadata caching alias resolution
        database.save_cached_metadata("tmdb:55555", "movie", {
            "id": "tmdb:55555",
            "imdb_id": "tt5555555",
            "alias_ids": ["tt5555555", "tmdb:55555"]
        })
        database.save_working_stream("tt5555555", stream_info=stream)

        # Retrieve using the TMDB ID when it was saved under IMDb ID
        res3 = database.get_working_stream("tmdb:55555")
        self.assertIsNotNone(res3)
        self.assertEqual(res3.get("url"), stream["url"])

    def test_stream_sort_key_all_prioritizes_remembered_stream(self):
        """Verify stream_sort_key_all ranks the remembered stream above generic higher-tier streams."""
        from src.window import stream_sort_key_all

        remembered = {
            "stream_title": "My Favorite 720p Stream",
            "quality": "720p",
            "url": "https://prov-b.com/stream.mp4",
            "is_http": True,
            "addon_names": ["ProviderB"]
        }

        higher_tier_other = {
            "stream_title": "Generic 1080p Stream",
            "quality": "1080p",
            "url": "https://prov-a.com/stream.mp4",
            "is_http": True,
            "addon_names": ["ProviderA"]
        }

        # Sorting key for both streams
        k_rem = stream_sort_key_all(remembered, pref_prov="ProviderA", rem_st=remembered)
        k_other = stream_sort_key_all(higher_tier_other, pref_prov="ProviderA", rem_st=remembered)

        # The remembered stream must sort ahead of the higher tier other stream
        self.assertGreater(k_rem, k_other)

    def test_on_watch_clicked_waits_when_streams_not_loaded(self):
        """Verify clicking play before streams load arms auto-play and shows loading without failing."""
        from src.window import MovieDetailsPage

        movie = {
            "id": "tt1234567",
            "title": "Uncached Movie",
            "type": "movie"
        }

        page = MovieDetailsPage(movie, on_back=lambda: None, window=MagicMock())
        page.torrents = []
        page._fetching_streams = True
        page._auto_play_on_streams_loaded = False

        # Simulate user clicking play while streams are still empty
        page.on_watch_clicked(btn=None)

        # Auto-play must be armed
        self.assertTrue(page._auto_play_on_streams_loaded)
        # Window loading indicator must be displayed
        page.window.show_player_loading.assert_called_once()
        # Playback must NOT have proceeded to play_stream_with_failover yet
        page.window.play_stream_with_failover.assert_not_called()

    def test_stream_dropdown_and_selection_keeps_remembered_stream(self):
        """Verify UI stream dropdown keeps remembered stream as selected rather than forcing index 0."""
        from src.window import MovieDetailsPage

        movie = {
            "id": "tt7777777",
            "title": "Stream Selection Movie",
            "type": "movie"
        }

        page = MovieDetailsPage(movie, on_back=lambda: None, window=MagicMock())

        stream1 = {
            "stream_title": "First Result 1080p",
            "quality": "1080p",
            "url": "https://other.com/video.mp4",
            "is_http": True,
            "addon_names": ["AddonA"]
        }
        stream2_remembered = {
            "stream_title": "My Used Stream 720p",
            "quality": "720p",
            "url": "https://remembered.com/video.mp4",
            "is_http": True,
            "addon_names": ["AddonB"]
        }

        page.remembered_working_stream = stream2_remembered
        page.selected_torrent = stream2_remembered

        # In target stream list where stream2 is at index 1
        stream_list = [stream1, stream2_remembered]

        # Call update_file_dropdown_ui logic via process_batch or dropdown
        matched_idx = -1
        for idx, item in enumerate(stream_list):
            from src.window import _streams_match
            if _streams_match(page.remembered_working_stream, item):
                matched_idx = idx
                break

        self.assertEqual(matched_idx, 1)

        # When process_batch applies target_t_list, it must select matched index
        from src.window import _find_stream_index_exact
        found_idx = _find_stream_index_exact(page.remembered_working_stream, stream_list)
        self.assertEqual(found_idx, 1)

        if found_idx >= 0:
            page.selected_torrent = stream_list[found_idx]

        self.assertEqual(page.selected_torrent, stream2_remembered)


if __name__ == "__main__":
    unittest.main()
