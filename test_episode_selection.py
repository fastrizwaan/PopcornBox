import unittest
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys

class TestEpisodeSelection(unittest.TestCase):
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
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_episode_matching_s1e5_vs_s1e4(self):
        from src import api
        stream_e4 = {
            "title": "Jaadugar.A.Witch.in.Mongolia.S01E04.Otchigin.1080p.AMZN.WEB-DL.Multi.DDP2.0.H.264-4kHd-Hub.Com.mkv",
            "is_http": True
        }
        stream_e5 = {
            "title": "Jaadugar.A.Witch.in.Mongolia.S01E05.Episode.5.1080p.AMZN.WEB-DL.Multi.DDP2.0.H.264-4kHd-Hub.Com.mkv",
            "is_http": True
        }

        # For Episode 5: E4 must mismatch, E5 must match
        self.assertEqual(api.match_stream_to_episode(stream_e4, 1, 5), api.MATCH_MISMATCH)
        self.assertEqual(api.match_stream_to_episode(stream_e5, 1, 5), api.MATCH_EXACT)

        # For Episode 4: E4 must match, E5 must mismatch
        self.assertEqual(api.match_stream_to_episode(stream_e4, 1, 4), api.MATCH_EXACT)
        self.assertEqual(api.match_stream_to_episode(stream_e5, 1, 4), api.MATCH_MISMATCH)

    def test_episode_selection_and_background_reload_persistence(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
        from src.window import MovieDetailsPage
        from src import database

        videos = [
            {"id": "tt39281887:1:1", "season": 1, "episode": 1, "title": "Episode 1"},
            {"id": "tt39281887:1:2", "season": 1, "episode": 2, "title": "Episode 2"},
            {"id": "tt39281887:1:3", "season": 1, "episode": 3, "title": "Episode 3"},
            {"id": "tt39281887:1:4", "season": 1, "episode": 4, "title": "Episode 4"},
            {"id": "tt39281887:1:5", "season": 1, "episode": 5, "title": "Episode 5"},
        ]
        details = {
            "id": "tt39281887",
            "title": "Jaadugar: A Witch in Mongolia",
            "type": "series",
            "videos": videos,
            "medium_cover_image": "https://example.com/poster.jpg"
        }

        # User clicked Jaadugar from Continue Watching where episode 4 was previously watched
        stub = {
            "id": "tt39281887",
            "title": "Jaadugar: A Witch in Mongolia",
            "type": "series",
            "season": 1,
            "episode": 4,
            "position": 100.0,
            "duration": 1200.0
        }
        database.save_continue_watching(stub)

        # Mock window object
        mock_win = MagicMock()
        mock_win.active_details_page = None

        with patch.object(MovieDetailsPage, "load_details_async"), \
             patch.object(MovieDetailsPage, "fetch_torrents_async"):
            page = MovieDetailsPage(stub, mock_win)
            page.build_ui(details)

            # Step 1: Initial state after opening from continue watching -> episode 4
            self.assertEqual(page.selected_episode, 4)
            self.assertIn("S1:E4", page.continue_label.get_text())

            # Step 2: User selects Episode 5 via dropdown
            # Episode dropdown index 4 corresponds to Episode 5
            page.episode_dropdown.set_selected(4)
            self.assertEqual(page.selected_episode, 5)
            self.assertEqual(page.movie_stub["episode"], 5)
            self.assertIn("S1:E5", page.continue_label.get_text())

            # Step 3: Background load_details_async finishes and calls build_ui(details) again!
            # It MUST NOT reset the selected episode back to 4!
            page.build_ui(details)

            self.assertEqual(page.selected_episode, 5, "Episode should remain 5 after background build_ui")
            self.assertEqual(page.movie_stub["episode"], 5, "movie_stub episode should remain 5")
            self.assertIn("S1:E5", page.continue_label.get_text(), "Continue button should still show S1:E5")
            self.assertEqual(page.episode_dropdown.get_selected(), 4, "Dropdown index should remain 4 (episode 5)")

if __name__ == "__main__":
    unittest.main()
