import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

class TestContinueWatching(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"
        
        # Patch DB_FILE in database module
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

    def test_save_and_advance_episode(self):
        from src import database
        
        # 1. Start watching episode 3
        ep3 = {
            "id": "tt39281887",
            "title": "Jaadugar: A Witch in Mongolia",
            "type": "series",
            "season": 1,
            "episode": 3,
            "position": 1200.0,
            "progress": 0.95,
            "duration": 1260.0,
            "stream_title": "Jaadugar: A Witch in Mongolia (S01E03)"
        }
        database.save_continue_watching(ep3)
        
        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0]["episode"], 3)
        self.assertEqual(cw[0]["progress"], 0.95)

        # 2. Advance to episode 4 with pos=0, prog=0
        ep4 = {
            "id": "tt39281887",
            "title": "Jaadugar: A Witch in Mongolia",
            "type": "series",
            "season": 1,
            "episode": 4,
            "position": 0.0,
            "progress": 0.0,
            "duration": 0.0,
            "stream_title": "Jaadugar: A Witch in Mongolia (S01E04)"
        }
        database.save_continue_watching(ep4)

        # Verify episode 4 did NOT inherit the 0.95 progress from episode 3
        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0]["episode"], 4)
        self.assertEqual(cw[0]["progress"], 0.0)
        self.assertEqual(cw[0]["position"], 0.0)

    def test_auto_heal_from_history(self):
        from src import database

        # Setup history and settings where episode 4 was reached but continue_watching was cleared
        db = {
            "continue_watching": [],
            "removed_continue_watching": ["tt39281887"],  # Polluted by old 92% bug
            "history": [
                {
                    "id": "tt39281887",
                    "title": "Jaadugar: A Witch in Mongolia",
                    "type": "series"
                }
            ],
            "settings": {
                "last_s_tt39281887": 1,
                "last_ep_tt39281887_1": 4
            }
        }
        database._write_db(db)
        database._json_cache_valid = False

        # get_continue_watching should auto-heal Jaadugar to S1:E4 and un-blacklist it
        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0]["id"], "tt39281887")
        self.assertEqual(cw[0]["season"], 1)
        self.assertEqual(cw[0]["episode"], 4)

        # Check that DB was updated
        db_after = database._read_db()
        self.assertNotIn("tt39281887", db_after.get("removed_continue_watching", []))
        self.assertEqual(len(db_after.get("continue_watching", [])), 1)

    def test_user_explicit_dismissal(self):
        from src import database

        ep = {
            "id": "tt12345",
            "title": "Sample Show",
            "type": "series",
            "season": 1,
            "episode": 2
        }
        database.save_continue_watching(ep)
        self.assertEqual(len(database.get_continue_watching()), 1)

        # Explicit dismissal by user clicking 'X'
        database.remove_continue_watching("tt12345", blacklist=True, user_action=True)
        self.assertEqual(len(database.get_continue_watching()), 0)

        # Watching again should clear dismissal
        database.save_continue_watching(ep)
        self.assertEqual(len(database.get_continue_watching()), 1)

if __name__ == "__main__":
    unittest.main()
