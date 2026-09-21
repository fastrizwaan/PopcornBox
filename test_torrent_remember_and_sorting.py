import unittest
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys

class TestTorrentRememberAndSorting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Register gresource and compile gschema into a temp directory
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        from gi.repository import Gio
        res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
        if res_file.exists():
            try:
                res = Gio.Resource.load(str(res_file))
                Gio.resources_register(res)
            except Exception:
                pass

        sys.modules["mpv"] = MagicMock()

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

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
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database
        database.reset_json_cache()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_find_matching_file_index_episodes(self):
        from src import api
        files = [
            {"path": "Goof.Troop.S01E01.mkv", "size": 1000},
            {"path": "Goof.Troop.S01E02-Good.Neighbor.Goof.mkv", "size": 1000},
            {"path": "Goof.Troop.S01E03.mkv", "size": 1000},
        ]
        # Match S1E2
        idx = api.find_matching_file_index(files, season=1, episode=2, strict=True)
        self.assertEqual(idx, 1)

        # Match by title
        idx_title = api.find_matching_file_index(files, season=1, episode=2, title="Good Neighbor Goof", strict=True)
        self.assertEqual(idx_title, 1)

        # Non-existent episode with strict=True returns None
        idx_none = api.find_matching_file_index(files, season=1, episode=99, strict=True)
        self.assertIsNone(idx_none)

    def test_find_matching_file_index_collection(self):
        from src import api
        files = [
            {"path": "The Lord of the Rings 1 - The Fellowship of the Ring (2001).mkv", "size": 5000},
            {"path": "The Lord of the Rings 2 - The Two Towers (2002).mkv", "size": 5000},
            {"path": "The Lord of the Rings 3 - The Return of the King (2003).mkv", "size": 5000},
        ]
        # Match Part 2 (The Two Towers)
        idx_towers = api.find_matching_file_index(files, episode=2, title="The Two Towers", strict=True)
        self.assertEqual(idx_towers, 1)

        # Match Part 3 by title
        idx_king = api.find_matching_file_index(files, episode=3, title="The Return of the King", strict=True)
        self.assertEqual(idx_king, 2)

    def test_zero_seed_torrent_deprioritized(self):
        from src import api
        # Mock stream data similar to the user's screenshot
        stream_zero_seed = {
            "name": "Torrentio",
            "title": "Goof.Troop.S01E02.720p.WEB.H264-SHIIIT\n👤 0 💾 712.82 MB ⚙️ ThePirateBay",
            "stream_title": "Goof.Troop.S01E02.720p.WEB.H264-SHIIIT",
            "url": "magnet:?xt=urn:btih:0000000000000000000000000000000000000001",
            "is_http": False,
            "seeders": 0
        }
        stream_season_pack = {
            "name": "TorrentClaw",
            "title": "TorrentClaw\n👤 13\n720p - Web-dl - Complete Season 1 - 36.7 GB",
            "stream_title": "Goof Troop 1992 Season 1 Complete 720p WEB-DL x264 [i_c]",
            "url": "magnet:?xt=urn:btih:0000000000000000000000000000000000000002",
            "is_http": False,
            "seeders": 13
        }

        # In sorting key:
        # Season pack has seeds=13 (has_seeds=1, ep_m=1)
        # Zero seed torrent has seeds=0 (has_seeds=0, ep_m=2)
        # Because has_seeds is the primary key, season pack MUST rank above zero seed stream!
        def _sort_key(t):
            is_http = 1 if t.get('is_http') else 0
            seeds = int(t.get('seeders') or 0)
            has_seeds = 1 if (is_http or seeds > 0) else 0
            ep_m = api.match_stream_to_episode(t, 1, 2)
            return (has_seeds, ep_m, seeds)

        streams = [stream_zero_seed, stream_season_pack]
        streams.sort(key=_sort_key, reverse=True)

        self.assertEqual(streams[0], stream_season_pack)
        self.assertEqual(streams[1], stream_zero_seed)

    def test_database_pack_remembering_and_file_caching(self):
        from src import database
        item_id = "tt0103429" # Goof Troop
        info_hash = "abcdef1234567890abcdef1234567890abcdef12"
        files = [
            {"index": 0, "path": "Goof Troop S01E01.mkv", "size": 1000},
            {"index": 1, "path": "Goof Troop S01E02.mkv", "size": 1000},
            {"index": 2, "path": "Goof Troop S01E03.mkv", "size": 1000},
        ]
        database.save_torrent_files(info_hash, files)
        cached = database.get_torrent_files(info_hash)
        self.assertEqual(len(cached), 3)
        self.assertEqual(cached[1]["path"], "Goof Troop S01E02.mkv")

        # Save season pack torrent
        pack_stream = {
            "hash": info_hash,
            "url": f"magnet:?xt=urn:btih:{info_hash}",
            "stream_title": "Goof Troop Season 1 Complete",
            "is_http": False,
            "seeders": 15,
            "file_index": 0
        }
        database.save_series_pack_torrent(item_id, pack_stream, season=1)

        # Lookup working stream for Season 1 Episode 2
        working = database.get_working_stream(item_id, season=1, episode=2)
        self.assertIsNotNone(working)
        self.assertEqual(working.get("hash"), info_hash)
        # file_index must be updated to index 1 (Episode 2)
        self.assertEqual(working.get("file_index"), 1)
        self.assertEqual(working.get("filename"), "Goof Troop S01E02.mkv")

    def test_stream_cache_key_and_extract_quality(self):
        from src import api
        key_series = api.get_stream_cache_key("tt0103429", "series", season=1, episode=2)
        self.assertEqual(key_series, "tt0103429:S1:E2")

        key_movie = api.get_stream_cache_key("tt0103429", "movie")
        self.assertEqual(key_movie, "tt0103429:movie")

        q_label, q_val = api._extract_quality("Goof.Troop.S01E02.1080p.WEB.H264")
        self.assertEqual(q_label, "1080p")
        self.assertEqual(q_val, 3)

if __name__ == "__main__":
    unittest.main()
