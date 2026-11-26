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
res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
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

        # Bind the real method from PopcornBoxWindow
        win._playback_preference_keys = window.PopcornBoxWindow._playback_preference_keys.__get__(win, window.PopcornBoxWindow)
        win._get_saved_audio_preference = window.PopcornBoxWindow._get_saved_audio_preference.__get__(win, window.PopcornBoxWindow)
        win._get_saved_subtitle_preference = window.PopcornBoxWindow._get_saved_subtitle_preference.__get__(win, window.PopcornBoxWindow)
        win._restore_audio_and_subtitle_preferences = window.PopcornBoxWindow._restore_audio_and_subtitle_preferences.__get__(win, window.PopcornBoxWindow)

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

        win._playback_preference_keys = window.PopcornBoxWindow._playback_preference_keys.__get__(win, window.PopcornBoxWindow)
        win._get_saved_audio_preference = lambda item: None
        win._get_saved_subtitle_preference = window.PopcornBoxWindow._get_saved_subtitle_preference.__get__(win, window.PopcornBoxWindow)
        win._restore_audio_and_subtitle_preferences = window.PopcornBoxWindow._restore_audio_and_subtitle_preferences.__get__(win, window.PopcornBoxWindow)

        win._restore_audio_and_subtitle_preferences(track_list)

        self.assertEqual(win.mpv.sid, "no")
        win.mpv.__setitem__.assert_any_call("sub-visibility", "no")
        self.assertTrue(win._subtitles_configured_for_file)

    def test_clear_saved_external_subtitle_accepts_item_context(self):
        """_clear_saved_external_subtitle supports both item and item_context kwargs."""
        win = MagicMock()
        win._playback_preference_keys = window.PopcornBoxWindow._playback_preference_keys.__get__(win, window.PopcornBoxWindow)
        win._external_subtitle_setting_keys = window.PopcornBoxWindow._external_subtitle_setting_keys.__get__(win, window.PopcornBoxWindow)
        win._clear_saved_external_subtitle = window.PopcornBoxWindow._clear_saved_external_subtitle.__get__(win, window.PopcornBoxWindow)

        item = {"id": "tt55555", "season": 1, "episode": 2}
        # Calling with item_context should not raise TypeError
        win._clear_saved_external_subtitle(item_context=item)
        # Calling with item should not raise TypeError
        win._clear_saved_external_subtitle(item=item)
        # Calling with no arguments should not raise TypeError
        win._clear_saved_external_subtitle()

    def test_languages_match(self):
        """api.languages_match matches ISO 639-1, 639-2, English names, and country tags."""
        from src import api
        self.assertTrue(api.languages_match("en", "eng"))
        self.assertTrue(api.languages_match("eng", "English"))
        self.assertTrue(api.languages_match("en-US", "en"))
        self.assertTrue(api.languages_match("hin", "hi"))
        self.assertTrue(api.languages_match("hi", "Hindi"))
        self.assertTrue(api.languages_match("tam", "ta"))
        self.assertTrue(api.languages_match("ta", "Tamil"))
        self.assertFalse(api.languages_match("ta", "en"))
        self.assertFalse(api.languages_match("tam", "hi"))

    def test_find_best_audio_track_priority(self):
        """find_best_audio_track respects the priority order (e.g. en before hi)."""
        from src import api
        tracks = [
            {"type": "audio", "id": 1, "lang": "hin", "title": "Hindi DDP 5.1", "default": True},
            {"type": "audio", "id": 2, "lang": "tam", "title": "Tamil DDP 5.1"},
            {"type": "audio", "id": 3, "lang": "tel", "title": "Telugu DDP 5.1"},
            {"type": "audio", "id": 4, "lang": "eng", "title": "English Atmos 5.1", "default": True},
        ]
        best = api.find_best_audio_track(tracks, preferred_langs=["en", "hi"])
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], 4)
        self.assertEqual(best["lang"], "eng")

    def test_restore_audio_never_selects_unmatched_track_id(self):
        """Saved track_id from an older stream must never select an unrelated language (e.g. Tamil for English)."""
        win = MagicMock()
        win._is_playing_trailer.return_value = False
        win.mpv.idle_active = False
        win.mpv.aid = 1
        # Saved preference had track_id 2 from a previous release (where 2 was English),
        # but in this release, track 2 is Tamil!
        win._current_playing_item = {
            "id": "tt66666",
            "audio_track_id": 2,
            "audio_track_lang": "en",
            "audio_track_title": "English 5.1",
        }
        track_list = [
            {"type": "audio", "id": 1, "lang": "hin", "title": "Hindi DDP 5.1", "default": True},
            {"type": "audio", "id": 2, "lang": "tam", "title": "Tamil DDP 5.1"},
            {"type": "audio", "id": 3, "lang": "tel", "title": "Telugu DDP 5.1"},
            {"type": "audio", "id": 4, "lang": "eng", "title": "English Atmos 5.1", "default": True},
        ]
        win.mpv.track_list = track_list

        win._playback_preference_keys = window.PopcornBoxWindow._playback_preference_keys.__get__(win, window.PopcornBoxWindow)
        win._get_saved_audio_preference = window.PopcornBoxWindow._get_saved_audio_preference.__get__(win, window.PopcornBoxWindow)
        win._get_saved_subtitle_preference = lambda item: None
        win._restore_audio_and_subtitle_preferences = window.PopcornBoxWindow._restore_audio_and_subtitle_preferences.__get__(win, window.PopcornBoxWindow)

        win._restore_audio_and_subtitle_preferences(track_list)

        # Must select Track 4 (English), NEVER Track 2 (Tamil)
        self.assertEqual(win.mpv.aid, 4)

    def test_match_stream_to_movie_rejects_unrelated_substring_match(self):
        """Movie name matching rejects unrelated substring match (e.g. Muskurahatein for Musk)."""
        from src import api
        stream = {
            "title": "🎬CineStream 🧊 1080p • CNCVerse Bridge",
            "url": "https://qrtgf.b-cdn.net/s3/upload/videos/2026/08/[Fibwatch.Com]Muskurahatein.(2017).Hindi.1080P.mkv",
            "behaviorHints": {
                "filename": "Musk.2026.1080p-CineTmdb.mkv",
            }
        }
        res = api.match_stream_to_movie(stream, target_title="Musk", expected_year=2026)
        self.assertEqual(res, api.MATCH_MISMATCH)

    def test_match_stream_to_movie_accepts_valid_stream(self):
        """Valid streams matching the title and year must not be flagged as MATCH_MISMATCH."""
        from src import api
        stream = {
            "title": "🎬Nebula 🧊 1080p",
            "url": "https://content.api.nebula.app/video_episodes/123/manifest.m3u8",
            "behaviorHints": {
                "filename": "Musk.2026.1080p.WEB-DL.English-Nebula"
            }
        }
        res = api.match_stream_to_movie(stream, target_title="Musk", expected_year=2026)
        self.assertEqual(res, api.MATCH_UNKNOWN)

    def test_match_stream_to_movie_accepts_opaque_hash_url(self):
        """Opaque CDN resource hashes must not be treated as conflicting titles."""
        from src import api
        stream = {
            "title": "🎬CineStream 🧊 1080p",
            "url": "https://bcdnxw.hakunaymatata.com/resource/47e474a252957c4090155ac7a2b53068.mp4?sign=abc",
            "behaviorHints": {
                "filename": "Musk.2026.1080p-CineTmdb.mp4"
            }
        }
        res = api.match_stream_to_movie(stream, target_title="Musk", expected_year=2026)
        self.assertEqual(res, api.MATCH_UNKNOWN)

    def test_match_stream_to_movie_does_not_reject_different_year(self):
        """Year matching is removed from stream searches; streams are not discarded."""
        from src import api
        stream = {
            "title": "Avatar.The.Way.of.Water.2022.1080p.mkv",
            "url": "https://example.com/Avatar.The.Way.of.Water.2022.1080p.mkv",
        }
        res = api.match_stream_to_movie(stream, target_title="Avatar", expected_year=2009)
        self.assertEqual(res, api.MATCH_UNKNOWN)

    def test_match_stream_to_movie_accepts_tuner_and_various_stream_formats(self):
        """Streams for Tuner (torrents, international titles, YTS badges, cloudstream) must be accepted."""
        from src import api
        test_streams = [
            # Standard scene torrent
            {
                "title": "Torrentio 2160p",
                "url": "magnet:?xt=urn:btih:90c6258c02ef2151362baf673b39c52a9fe6915c&dn=Tuner.2025.2160p",
            },
            # Spanish release
            {
                "title": "Torrentio 1080p",
                "url": "magnet:?xt=urn:btih:b785ad68e920fd3647eb5c933145e93a6995dd45&dn=El.Afinador.2026",
            },
            # YTS stream badge format (quality | seeds | size)
            {
                "name": "Popcorn 1080p",
                "title": "1080p | 👤 15 | 2.1 GB",
                "magnet": "magnet:?xt=urn:btih:abc&dn=Tuner",
            },
            # Direct Google Drive or Pixeldrain stream
            {
                "title": "Fast Server · 1080p",
                "url": "https://video-downloads.googleusercontent.com/ADGPM2nXoYgoRvR14WJx8xPi0frnqHogr8",
            },
        ]
        for s in test_streams:
            self.assertEqual(api.match_stream_to_movie(s, target_title="Tuner"), api.MATCH_UNKNOWN)

    def test_match_stream_to_movie_accepts_shaun_the_sheep_yts_streams(self):
        """Multi-word title movie (Shaun the Sheep) with YTS badges must be accepted."""
        from src import api
        stream = {
            "name": "Popcorn 1080p",
            "title": "1080p | 👤 15 | 2.1 GB",
            "magnet": "magnet:?xt=urn:btih:def&dn=Shaun+the+Sheep",
        }
        self.assertEqual(
            api.match_stream_to_movie(stream, target_title="Shaun the Sheep: The Beast of Mossy Bottom"),
            api.MATCH_UNKNOWN
        )

    def test_dialogue_preferred_over_sdh_in_subtitles(self):
        """find_best_subtitle_track chooses standard dialogue over SDH / CC roll-up subtitles."""
        from src import api
        tracks = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English [CC]", "hearing-impaired": True},
            {"type": "sub", "id": 2, "lang": "eng", "title": "English", "hearing-impaired": False},
        ]
        best = api.find_best_subtitle_track(tracks, preferred_langs=["en"])
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], 2)

    def test_filter_subtitles_by_language_ranks_dialogue_before_sdh(self):
        """_filter_subtitles_by_language places clean dialogue subtitles ahead of SDH."""
        from src import api
        rank_sets = api._build_language_rank_sets(["en"])
        subs = [
            {"lang": "eng", "title": "English (SDH)", "hearing-impaired": True},
            {"lang": "eng", "title": "English Dialogue", "hearing-impaired": False},
        ]
        filtered = api._filter_subtitles_by_language(subs, rank_sets)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["title"], "English Dialogue")
        self.assertEqual(filtered[1]["title"], "English (SDH)")

    def test_find_best_audio_track_fallback(self):
        """When no tracks match preferred_langs, find_best_audio_track falls back to default/first audio track."""
        from src import api
        tracks = [
            {"type": "audio", "id": 1, "lang": "und", "title": "Main Audio", "default": True, "audio-channels": 6},
            {"type": "audio", "id": 2, "lang": "und", "title": "Commentary", "default": False, "audio-channels": 2},
        ]
        # preferred_langs is 'en' which does not match 'und'
        best = api.find_best_audio_track(tracks, preferred_langs=["en"])
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], 1)

    def test_capture_track_preferences_ignores_idle_aid_false(self):
        """_capture_current_track_preferences does NOT mark audio disabled when mpv.aid is False (idle)."""
        win = MagicMock()
        win.mpv.idle_active = False
        win.mpv.aid = False  # MPV idle / stopped returns False, which is == 0 in raw Python
        win.mpv.sid = False
        win.mpv.track_list = [
            {"type": "audio", "id": 1, "lang": "eng", "title": "English"},
        ]
        win._remember_audio_preference = MagicMock()
        win._remember_subtitle_preference = MagicMock()
        win._capture_current_track_preferences = window.PopcornBoxWindow._capture_current_track_preferences.__get__(win, window.PopcornBoxWindow)

        item = {"id": "tt77777"}
        win._capture_current_track_preferences(item)

        # Must NOT mark audio disabled
        self.assertFalse(item.get("audio_disabled", False))
        win._remember_audio_preference.assert_not_called()

    def test_subtitle_manual_selection_prevents_bounce(self):
        """Manual subtitle selection sets _user_selected_subtitle and prevents _on_track_list_for_subtitles override."""
        win = MagicMock()
        win.mpv.idle_active = False
        win.mpv.track_list = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English 1"},
            {"type": "sub", "id": 2, "lang": "eng", "title": "English 2"},
        ]
        win._playback_preference_keys = window.PopcornBoxWindow._playback_preference_keys.__get__(win, window.PopcornBoxWindow)
        win._remember_subtitle_preference = MagicMock()
        win._clear_saved_external_subtitle = MagicMock()
        win._on_subtitle_selected = window.PopcornBoxWindow._on_subtitle_selected.__get__(win, window.PopcornBoxWindow)
        win._on_track_list_for_subtitles = window.PopcornBoxWindow._on_track_list_for_subtitles.__get__(win, window.PopcornBoxWindow)

        # User selects track 2 (English 2)
        action = MagicMock()
        parameter = MagicMock()
        parameter.get_int32.return_value = 2
        win._on_subtitle_selected(action, parameter)

        self.assertEqual(win.mpv.sid, 2)
        self.assertTrue(win._user_selected_subtitle)
        self.assertTrue(win._subtitles_configured_for_file)

        # Subsequent track_list change must NOT overwrite sid to 1
        win._on_track_list_for_subtitles(win.mpv.track_list)
        self.assertEqual(win.mpv.sid, 2)

    def test_history_dialog_activates_via_play_stream(self):
        """HistoryDialog._on_row_activated delegates to win._play_stream."""
        from src import history
        mock_win = MagicMock()
        diag = MagicMock(spec=history.HistoryDialog)
        diag._win = mock_win
        diag.close = MagicMock()
        history.HistoryDialog._on_row_activated(diag, "/tmp/test_video.mkv")

        mock_win._play_stream.assert_called_once_with("/tmp/test_video.mkv", title="test_video.mkv")
        diag.close.assert_called_once()

    def test_update_track_menus_groups_many_subtitles(self):
        """When media has > 6 subtitle tracks, non-preferred foreign tracks are grouped into an 'Other Languages' submenu."""
        win = MagicMock()
        win.subtitles_menu = Gio.Menu.new()
        win.audio_tracks_menu = Gio.Menu.new()
        win.video_tracks_menu = Gio.Menu.new()
        win.mpv.sid = 1
        win._format_sub_label = window.PopcornBoxWindow._format_sub_label.__get__(win, window.PopcornBoxWindow)
        win._format_audio_label = window.PopcornBoxWindow._format_audio_label.__get__(win, window.PopcornBoxWindow)
        win._format_video_label = window.PopcornBoxWindow._format_video_label.__get__(win, window.PopcornBoxWindow)
        win._update_track_menus = window.PopcornBoxWindow._update_track_menus.__get__(win, window.PopcornBoxWindow)

        # 2 English tracks + 10 foreign tracks
        track_list = [
            {"type": "sub", "id": 1, "lang": "en", "title": "", "selected": True},
            {"type": "sub", "id": 2, "lang": "en", "title": ""},
            {"type": "sub", "id": 3, "lang": "bg", "title": ""},
            {"type": "sub", "id": 4, "lang": "cs", "title": ""},
            {"type": "sub", "id": 5, "lang": "da", "title": ""},
            {"type": "sub", "id": 6, "lang": "de", "title": ""},
            {"type": "sub", "id": 7, "lang": "el", "title": ""},
            {"type": "sub", "id": 8, "lang": "es", "title": ""},
            {"type": "sub", "id": 9, "lang": "fr", "title": ""},
            {"type": "sub", "id": 10, "lang": "he", "title": ""},
            {"type": "sub", "id": 11, "lang": "it", "title": ""},
            {"type": "sub", "id": 12, "lang": "ja", "title": ""},
        ]

        with patch("src.api.get_user_preferred_languages", return_value=["en"]):
            win._update_track_menus(track_list)

        # In subtitles_menu: 2 sections (actions_section, tracks_section)
        self.assertEqual(win.subtitles_menu.get_n_items(), 2)
        # Check tracks_section items: None + 2 English tracks + 1 "Other Languages" submenu item = 4 items
        # Gio.Menu structure: section 1 is at index 1
        tracks_section_model = win.subtitles_menu.get_item_link(1, Gio.MENU_LINK_SECTION)
        self.assertIsNotNone(tracks_section_model)
        # Verify it has 4 items (None + English 1 + English 2 + Other Languages submenu)
        self.assertEqual(tracks_section_model.get_n_items(), 4)

        # Verify the 4th item has a submenu link
        other_submenu = tracks_section_model.get_item_link(3, Gio.MENU_LINK_SUBMENU)
        self.assertIsNotNone(other_submenu)
        # Verify other_submenu contains the remaining 10 foreign language tracks
        self.assertEqual(other_submenu.get_n_items(), 10)

    def test_update_track_menus_few_subtitles_no_submenu(self):
        """When media has <= 6 subtitle tracks, all tracks remain at the primary level without a submenu."""
        win = MagicMock()
        win.subtitles_menu = Gio.Menu.new()
        win.audio_tracks_menu = Gio.Menu.new()
        win.video_tracks_menu = Gio.Menu.new()
        win.mpv.sid = 1
        win._format_sub_label = window.PopcornBoxWindow._format_sub_label.__get__(win, window.PopcornBoxWindow)
        win._format_audio_label = window.PopcornBoxWindow._format_audio_label.__get__(win, window.PopcornBoxWindow)
        win._format_video_label = window.PopcornBoxWindow._format_video_label.__get__(win, window.PopcornBoxWindow)
        win._update_track_menus = window.PopcornBoxWindow._update_track_menus.__get__(win, window.PopcornBoxWindow)

        track_list = [
            {"type": "sub", "id": 1, "lang": "en", "title": ""},
            {"type": "sub", "id": 2, "lang": "fr", "title": ""},
            {"type": "sub", "id": 3, "lang": "es", "title": ""},
        ]

        with patch("src.api.get_user_preferred_languages", return_value=["en"]):
            win._update_track_menus(track_list)

        tracks_section_model = win.subtitles_menu.get_item_link(1, Gio.MENU_LINK_SECTION)
        # None + 3 tracks = 4 items, NO submenu
        self.assertEqual(tracks_section_model.get_n_items(), 4)
        for i in range(tracks_section_model.get_n_items()):
            self.assertIsNone(tracks_section_model.get_item_link(i, Gio.MENU_LINK_SUBMENU))


if __name__ == "__main__":
    unittest.main()
