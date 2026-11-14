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

from src import database, window


class TestResumeAudioSubtitle(unittest.TestCase):
    def setUp(self):
        self.orig_read_db = database._read_db
        self.orig_write_db = database._write_db
        self.fake_db = {
            "continue_watching": [],
            "settings": {},
            "history": [],
        }
        database._read_db = lambda: self.fake_db
        database._write_db = lambda data: True

    def tearDown(self):
        database._read_db = self.orig_read_db
        database._write_db = self.orig_write_db

    def test_get_continue_watching_item_by_any_id(self):
        """Verify continue watching item is found by imdb_id, id, or alias_ids."""
        cw_item = {
            "id": "tmdb:12345",
            "imdb_id": "tt9876543",
            "alias_ids": ["cloudstream:abc", "slug:the-movie"],
            "title": "The Movie",
            "position": 1500.0,
            "duration": 6000.0,
            "progress": 0.25,
            "audio_track_lang": "eng",
            "audio_track_title": "English 5.1",
            "sub_track_lang": "spa",
            "sub_visibility": "yes",
        }
        database.save_continue_watching(cw_item)

        # Lookup by tmdb id
        found1 = database.get_continue_watching_item("tmdb:12345")
        self.assertIsNotNone(found1)
        self.assertEqual(found1["position"], 1500.0)

        # Lookup by imdb id
        found2 = database.get_continue_watching_item("tt9876543")
        self.assertIsNotNone(found2)
        self.assertEqual(found2["title"], "The Movie")

        # Lookup by alias
        found3 = database.get_continue_watching_item("cloudstream:abc")
        self.assertIsNotNone(found3)
        self.assertEqual(found3["position"], 1500.0)

        # Lookup with list of IDs
        found4 = database.get_continue_watching_item(["other:id", "slug:the-movie"])
        self.assertIsNotNone(found4)
        self.assertEqual(found4["position"], 1500.0)

    def test_save_continue_watching_protects_established_position(self):
        """An unseeked startup tick (< 5s) must not overwrite established progress (> 10s)."""
        cw_item = {
            "id": "tt11111",
            "title": "Test Film",
            "position": 1200.0,
            "duration": 5000.0,
            "progress": 0.24,
        }
        database.save_continue_watching(cw_item)

        # Simulated startup progress tick with unseeked position 0.1s
        stale_tick = {
            "id": "tt11111",
            "title": "Test Film",
            "position": 0.1,
            "duration": 5000.0,
            "progress": 0.00002,
        }
        database.save_continue_watching(stale_tick)

        item = database.get_continue_watching_item("tt11111")
        self.assertEqual(item["position"], 1200.0)
        self.assertEqual(item["progress"], 0.24)

        # Legitimate progress update beyond 10s or with force_reset
        advanced_tick = {
            "id": "tt11111",
            "title": "Test Film",
            "position": 1450.0,
            "duration": 5000.0,
            "progress": 0.29,
        }
        database.save_continue_watching(advanced_tick)
        item2 = database.get_continue_watching_item("tt11111")
        self.assertEqual(item2["position"], 1450.0)

    def test_audio_and_subtitle_preference_persistence(self):
        """Audio and subtitle preferences are retained in continue_watching and settings."""
        cw_item = {
            "id": "tt22222",
            "title": "Anime Show",
            "season": 1,
            "episode": 3,
            "position": 500.0,
            "duration": 1400.0,
            "progress": 0.35,
            "audio_track_id": 2,
            "audio_track_lang": "jpn",
            "audio_track_title": "Japanese Original",
            "sub_track_id": 4,
            "sub_track_lang": "eng",
            "sub_track_title": "English Full",
            "sub_visibility": "yes",
            "sub_disabled": False,
        }
        database.save_continue_watching(cw_item)

        item = database.get_continue_watching_item("tt22222")
        self.assertEqual(item.get("audio_track_lang"), "jpn")
        self.assertEqual(item.get("sub_track_lang"), "eng")
        self.assertEqual(item.get("sub_visibility"), "yes")

        # Update without explicit track fields preserves existing preferences
        update_tick = {
            "id": "tt22222",
            "title": "Anime Show",
            "season": 1,
            "episode": 3,
            "position": 600.0,
            "duration": 1400.0,
            "progress": 0.42,
        }
        database.save_continue_watching(update_tick)
        item_updated = database.get_continue_watching_item("tt22222")
        self.assertEqual(item_updated["position"], 600.0)
        self.assertEqual(item_updated.get("audio_track_lang"), "jpn")
        self.assertEqual(item_updated.get("sub_track_lang"), "eng")

    def test_restore_audio_and_subtitle_preferences(self):
        """_restore_audio_and_subtitle_preferences matches track list and applies aid / sid."""
        win = MagicMock()
        win._is_playing_trailer.return_value = False
        win.mpv.idle_active = False
        win.mpv.aid = 1
        win.mpv.sid = "no"
        win.mpv.__getitem__.side_effect = lambda k: "no" if k == "sub-visibility" else None
        win._current_playing_item = {
            "id": "tt33333",
            "audio_track_lang": "fra",
            "audio_track_title": "French Audio",
            "sub_track_lang": "eng",
            "sub_track_title": "English Subtitles",
            "sub_visibility": "yes",
            "sub_disabled": False,
        }

        # Mock track list from MPV
        track_list = [
            {"type": "video", "id": 1},
            {"type": "audio", "id": 1, "lang": "eng", "title": "English 2.0"},
            {"type": "audio", "id": 2, "lang": "fra", "title": "French Audio 5.1"},
            {"type": "sub", "id": 1, "lang": "spa", "title": "Spanish"},
            {"type": "sub", "id": 2, "lang": "eng", "title": "English Subtitles"},
        ]
        win.mpv.track_list = track_list

        # Bind the real method from CineWindow
        win._playback_preference_keys = window.CineWindow._playback_preference_keys.__get__(win, window.CineWindow)
        win._get_saved_audio_preference = window.CineWindow._get_saved_audio_preference.__get__(win, window.CineWindow)
        win._get_saved_subtitle_preference = window.CineWindow._get_saved_subtitle_preference.__get__(win, window.CineWindow)
        win._restore_audio_and_subtitle_preferences = window.CineWindow._restore_audio_and_subtitle_preferences.__get__(win, window.CineWindow)

        win._restore_audio_and_subtitle_preferences(track_list)

        # French audio (id 2) should be selected
        self.assertEqual(win.mpv.aid, 2)
        # English subtitle (id 2) should be selected and visible
        self.assertEqual(win.mpv.sid, 2)
        win.mpv.__setitem__.assert_any_call("sub-visibility", "yes")

    def test_restore_disabled_subtitles_preference(self):
        """When user previously disabled subtitles, restoration keeps them disabled."""
        win = MagicMock()
        win._is_playing_trailer.return_value = False
        win.mpv.idle_active = False
        win.mpv.sid = 2
        win._current_playing_item = {
            "id": "tt44444",
            "sub_visibility": "no",
            "sub_disabled": True,
        }
        track_list = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English"},
        ]
        win.mpv.track_list = track_list

        win._playback_preference_keys = window.CineWindow._playback_preference_keys.__get__(win, window.CineWindow)
        win._get_saved_audio_preference = lambda item: None
        win._get_saved_subtitle_preference = window.CineWindow._get_saved_subtitle_preference.__get__(win, window.CineWindow)
        win._restore_audio_and_subtitle_preferences = window.CineWindow._restore_audio_and_subtitle_preferences.__get__(win, window.CineWindow)

        win._restore_audio_and_subtitle_preferences(track_list)

        self.assertEqual(win.mpv.sid, "no")
        win.mpv.__setitem__.assert_any_call("sub-visibility", "no")
        self.assertTrue(win._subtitles_configured_for_file)

    def test_clear_saved_external_subtitle_accepts_item_context(self):
        """_clear_saved_external_subtitle supports both item and item_context kwargs."""
        win = MagicMock()
        win._playback_preference_keys = window.CineWindow._playback_preference_keys.__get__(win, window.CineWindow)
        win._external_subtitle_setting_keys = window.CineWindow._external_subtitle_setting_keys.__get__(win, window.CineWindow)
        win._clear_saved_external_subtitle = window.CineWindow._clear_saved_external_subtitle.__get__(win, window.CineWindow)

        item = {"id": "tt55555", "season": 1, "episode": 2}
        # Calling with item_context should not raise TypeError
        win._clear_saved_external_subtitle(item_context=item)
        # Calling with item should not raise TypeError
        win._clear_saved_external_subtitle(item=item)
        # Calling with no arguments should not raise TypeError
        win._clear_saved_external_subtitle()


if __name__ == "__main__":
    unittest.main()
