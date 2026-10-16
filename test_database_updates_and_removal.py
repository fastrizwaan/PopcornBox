import os
import sys
import unittest
import tempfile
import json
from pathlib import Path

# Setup clean testing environment
os.environ["POPCORN_TEST"] = "1"
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src import database, api, movie_widget

class TestDatabaseUpdatesAndRemoval(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_file = database.DB_FILE
        self.orig_config_dir = database.CONFIG_DIR
        database.CONFIG_DIR = Path(self.tmp_dir.name)
        database.DB_FILE = database.CONFIG_DIR / "data.json"
        database.reset_json_cache()

    def tearDown(self):
        database.CONFIG_DIR = self.orig_config_dir
        database.DB_FILE = self.orig_db_file
        database.reset_json_cache()
        self.tmp_dir.cleanup()

    def test_write_db_sanitizes_callables_and_functions(self):
        """Verify that _write_db never crashes with TypeError when non-serializable objects exist."""
        bad_data = {
            "favorites": [{"id": "fav1", "title": "Fav 1", "_on_hydrated": lambda *a: None}],
            "history": [{"id": "hist1", "title": "Hist 1", "func": lambda: True}],
            "watched": [],
            "continue_watching": [],
            "downloads": [],
            "settings": {},
            "addons": database.DEFAULT_ADDONS
        }
        res = database._write_db(bad_data)
        self.assertTrue(res, "_write_db must succeed even when callables are present")

        # Verify on disk that function was stripped and json is valid
        with open(database.DB_FILE, "r") as f:
            disk_data = json.load(f)
        self.assertNotIn("_on_hydrated", disk_data["favorites"][0])
        self.assertNotIn("func", disk_data["history"][0])
        self.assertEqual(disk_data["favorites"][0]["id"], "fav1")

    def test_movie_widget_does_not_mutate_movie_data_with_functions(self):
        """Verify that MovieWidget no longer attaches Python function callbacks to movie_data."""
        item = {"id": "tt1234567", "title": "Widget Test Movie"}
        widget = movie_widget.MovieWidget(item, lambda *a: None, on_remove_clicked=lambda *a: None)
        self.assertNotIn("_on_hydrated", item)
        self.assertNotIn("_on_hydrated", widget.movie_data)

    def test_add_and_remove_favorite_persists_across_reload(self):
        """Verify adding and removing favorites updates the database and persists across cache resets."""
        item = {
            "id": "tt9990001",
            "imdb_id": "tt9990001",
            "title": "Favorite Movie",
            "type": "movie"
        }
        database.add_favorite(item, sync_to_remote=False)
        self.assertTrue(database.is_favorite("tt9990001"))
        self.assertTrue(database.is_favorite(item))

        # Reset cache to simulate app restart
        database.reset_json_cache()
        self.assertTrue(database.is_favorite("tt9990001"))

        # Remove by dict
        database.remove_favorite(item, sync_to_remote=False)
        self.assertFalse(database.is_favorite("tt9990001"))

        # Verify on disk
        database.reset_json_cache()
        self.assertFalse(database.is_favorite("tt9990001"))
        self.assertEqual(len(database.get_favorites()), 0)

    def test_remove_favorite_with_id_variant(self):
        """Verify remove_favorite matches when an addon ID variant is passed."""
        item = {
            "id": "cinemeta:tt9990002",
            "imdb_id": "tt9990002",
            "title": "Variant Fav",
            "type": "movie"
        }
        database.add_favorite(item, sync_to_remote=False)
        self.assertTrue(database.is_favorite("tt9990002"))

        # Remove using only the base IMDB ID string
        database.remove_favorite("tt9990002", sync_to_remote=False)
        self.assertFalse(database.is_favorite("tt9990002"))
        database.reset_json_cache()
        self.assertEqual(len(database.get_favorites()), 0)

    def test_add_and_remove_history_persists_across_reload(self):
        """Verify adding and removing history updates the database and persists across reloads."""
        item = {
            "id": "tt8880001",
            "title": "Watched Movie",
            "type": "movie"
        }
        database.add_history(item, sync_to_remote=False)
        self.assertEqual(len(database.get_history()), 1)

        database.reset_json_cache()
        self.assertEqual(len(database.get_history()), 1)

        # Remove by string
        database.remove_history("tt8880001", sync_to_remote=False)
        self.assertEqual(len(database.get_history()), 0)

        # Confirm persisted on disk
        database.reset_json_cache()
        self.assertEqual(len(database.get_history()), 0)

    def test_add_and_remove_watched_persists_across_reload(self):
        """Verify adding and removing watched items updates the database and persists across reloads."""
        item = {
            "id": "tt7770001",
            "imdb_id": "tt7770001",
            "title": "Seen Movie",
            "type": "movie"
        }
        database.add_watched(item)
        self.assertTrue(database.is_watched("tt7770001"))
        self.assertTrue(database.is_watched(item))

        database.reset_json_cache()
        self.assertTrue(database.is_watched("tt7770001"))

        # Remove by dict
        database.remove_watched(item)
        self.assertFalse(database.is_watched("tt7770001"))

        database.reset_json_cache()
        self.assertFalse(database.is_watched("tt7770001"))
        self.assertEqual(len(database.get_watched()), 0)

    def test_remove_continue_watching_accepts_dict(self):
        """Verify remove_continue_watching accepts item_data dictionary as well as string."""
        item = {
            "id": "tt6660001",
            "imdb_id": "tt6660001",
            "title": "CW Show",
            "type": "series",
            "season": 1,
            "episode": 2
        }
        database.save_continue_watching(item, sync_to_remote=False)
        self.assertEqual(len(database.get_continue_watching()), 1)

        # Remove by passing the item dictionary directly
        database.remove_continue_watching(item, blacklist=True, user_action=True, sync_to_remote=False)
        self.assertEqual(len(database.get_continue_watching()), 0)

        database.reset_json_cache()
        self.assertEqual(len(database.get_continue_watching()), 0)

if __name__ == "__main__":
    unittest.main()
