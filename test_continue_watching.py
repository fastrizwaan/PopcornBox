import unittest
import tempfile
import os
import sys
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

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

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
                "last_ep_tt39281887_1": 4,
                "working_stream_tt39281887_1_4": {"url": "http://stream.test"}
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

        # A fresh database read, like an app restart, must preserve the dismissal.
        database.reset_json_cache()
        self.assertEqual(len(database.get_continue_watching()), 0)
        self.assertIsNone(database.get_continue_watching_item("tt12345"))

        # Watching again should clear dismissal
        database.save_continue_watching(ep)
        self.assertEqual(len(database.get_continue_watching()), 1)

    def test_user_dismissal_blocks_history_auto_heal_after_reload(self):
        from src import database

        item = {"id": "tt98765", "title": "Dismissed Show", "type": "series", "season": 1, "episode": 3}
        database.save_continue_watching(item)
        database.add_history({"id": "tt98765", "title": "Dismissed Show", "type": "series"}, sync_to_remote=False)
        database.set_setting("last_s_tt98765", 1)
        database.set_setting("last_ep_tt98765_1", 3)
        database.remove_continue_watching("tt98765", blacklist=True, user_action=True, sync_to_remote=False)

        database.reset_json_cache()
        self.assertEqual(database.get_continue_watching(), [])
        self.assertIsNone(database.get_continue_watching_item("tt98765"))

    def test_remove_continue_watching_matches_alias_ids(self):
        from src import database

        item = {"id": "tmdb:123", "imdb_id": "tt12345", "alias_ids": ["tt12345", "tmdb:123"], "title": "Alias Movie"}
        database.save_continue_watching(item)
        database.remove_continue_watching("tt12345", blacklist=True, user_action=True, sync_to_remote=False)

        self.assertEqual(database.get_continue_watching(), [])
        self.assertTrue({"tt12345", "tmdb:123"}.issubset(database.get_continue_watching_dismissals()))

    def test_update_continue_watching_section_shows_more_cards(self):
        from src import database
        from src.window import CineWindow
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        from unittest.mock import MagicMock

        # Populate 30 continue watching items
        for i in range(30):
            database.save_continue_watching({
                "id": f"tt{i:07d}",
                "title": f"Item {i}",
                "type": "movie",
                "last_watched": 1000 + i
            })

        win = MagicMock()
        win.category_btn_stack.get_visible_child_name.return_value = "home"
        target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win.discover_box = target_box
        win._discover_views = {"home|all|all|all|all|all|Popular": {"box": target_box}}
        win._discover_row_card_metrics.return_value = (15, 130)
        win._discover_result_matches_item.return_value = True
        win._fit_discover_row_card = MagicMock()
        win._start_background_continue_watching_refresh = MagicMock()
        win._cw_initial_layout_check_scheduled = True

        CineWindow._update_continue_watching_section(win)

        cw_scroll = None
        c = target_box.get_first_child()
        while c:
            if getattr(c, "_is_cw_scroll", False):
                cw_scroll = c
                break
            c = c.get_next_sibling()

        self.assertIsNotNone(cw_scroll)
        viewport = cw_scroll.get_child()
        cw_row = viewport.get_child() if hasattr(viewport, "get_child") else viewport

        card_count = 0
        card = cw_row.get_first_child()
        while card:
            card_count += 1
            card = card.get_next_sibling()

        # Must display all 30 cards (up to 36 cards) rather than capping at 15
        self.assertEqual(card_count, 30)

if __name__ == "__main__":
    unittest.main()
