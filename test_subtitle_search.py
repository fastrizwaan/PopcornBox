import unittest
import sys
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.modules["mpv"] = MagicMock()

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

import json
import io

from src import api
from src.subtitle_search_dialog import SubtitleSearchDialog, SUPPORTED_LANGUAGES


class TestSubtitleSearch(unittest.TestCase):

    def test_get_language_name(self):
        self.assertEqual(api.get_language_name("eng"), "English")
        self.assertEqual(api.get_language_name("en"), "English")
        self.assertEqual(api.get_language_name("spa"), "Spanish")
        self.assertEqual(api.get_language_name("es"), "Spanish")
        self.assertEqual(api.get_language_name("pob"), "Portuguese (BR)")
        self.assertEqual(api.get_language_name("pt-br"), "Portuguese (BR)")
        self.assertEqual(api.get_language_name("hin"), "Hindi")
        self.assertEqual(api.get_language_name("ara"), "Arabic")
        self.assertEqual(api.get_language_name("fra"), "French")
        self.assertEqual(api.get_language_name("cze"), "Czech")
        self.assertEqual(api.get_language_name("xyz123"), "XYZ123")
        self.assertEqual(api.get_language_name(""), "Unknown")

    @patch("urllib.request.urlopen")
    def test_search_subtitles_movie(self, mock_urlopen):
        sample_response = {
            "subtitles": [
                {
                    "id": "1001",
                    "url": "https://subs.strem.io/file1.srt",
                    "lang": "eng",
                    "subtitleFileName": "Inception.2010.1080p.srt",
                    "movieReleaseName": "Inception 2010 BluRay",
                    "fpsMilli": 23976,
                    "format": "SRT"
                },
                {
                    "id": "1002",
                    "url": "https://subs.strem.io/file2.srt",
                    "lang": "spa",
                    "subtitleFileName": "Inception.Spanish.srt",
                    "movieReleaseName": "Inception Spanish",
                    "fpsMilli": 24000,
                    "format": "SRT"
                },
                # Duplicate url to test deduplication
                {
                    "id": "1003",
                    "url": "https://subs.strem.io/file1.srt",
                    "lang": "eng",
                    "subtitleFileName": "Inception.Duplicate.srt",
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(sample_response).encode("utf-8")
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        results = api.search_subtitles_online(
            query="Inception",
            imdb_id="tt1375666",
            media_type="movie"
        )

        self.assertEqual(len(results), 2)
        r0 = results[0]
        self.assertEqual(r0["lang"], "eng")
        self.assertEqual(r0["lang_name"], "English")
        self.assertEqual(r0["file_name"], "Inception.2010.1080p.srt")
        self.assertEqual(r0["release_name"], "Inception 2010 BluRay")
        self.assertEqual(r0["fps"], "23.976 fps")

        r1 = results[1]
        self.assertEqual(r1["lang"], "spa")
        self.assertEqual(r1["lang_name"], "Spanish")

    @patch("urllib.request.urlopen")
    def test_search_subtitles_series_and_cinemeta_resolution(self, mock_urlopen):
        cinemeta_response = {
            "metas": [
                {
                    "id": "tt0903747",
                    "name": "Breaking Bad",
                    "type": "series"
                }
            ]
        }
        subtitles_response = {
            "subtitles": [
                {
                    "id": "2001",
                    "url": "https://subs.strem.io/bb_s1e1.srt",
                    "lang": "eng",
                    "subtitleFileName": "Breaking.Bad.S01E01.srt",
                    "movieReleaseName": "Breaking Bad S01E01 Pilot",
                    "fpsMilli": 23976
                }
            ]
        }

        def mock_urlopen_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            resp.headers = {}
            resp.__enter__.return_value = resp
            if "cinemeta" in url:
                resp.read.return_value = json.dumps(cinemeta_response).encode("utf-8")
            else:
                self.assertIn("series/tt0903747:1:1.json", url)
                resp.read.return_value = json.dumps(subtitles_response).encode("utf-8")
            return resp

        mock_urlopen.side_effect = mock_urlopen_side_effect

        results = api.search_subtitles_online(
            query="Breaking Bad",
            imdb_id=None,
            media_type="series",
            season=1,
            episode=1
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_name"], "Breaking.Bad.S01E01.srt")
        self.assertEqual(results[0]["lang_name"], "English")

    def test_subtitle_dialog_filter(self):
        # Test client-side filtering logic
        mock_win = MagicMock()
        dialog = SubtitleSearchDialog(
            window=mock_win,
            title="Test Movie",
            imdb_id="tt1234567",
            media_type="movie"
        )

        test_data = [
            {"id": "1", "url": "http://sub1", "lang": "eng", "lang_name": "English", "release_name": "Release 1", "file_name": "1.srt", "source": "OpenSubtitles", "fps": "", "format": "SRT"},
            {"id": "2", "url": "http://sub2", "lang": "spa", "lang_name": "Spanish", "release_name": "Release 2", "file_name": "2.srt", "source": "OpenSubtitles", "fps": "", "format": "SRT"},
            {"id": "3", "url": "http://sub3", "lang": "fre", "lang_name": "French", "release_name": "Release 3", "file_name": "3.srt", "source": "OpenSubtitles", "fps": "", "format": "SRT"},
        ]
        dialog._all_subtitles = test_data

        # 1. Filter: All Languages (index 0)
        dialog.lang_dropdown.set_selected(0)
        dialog._apply_filter()
        self.assertEqual(dialog.stack.get_visible_child_name(), "results")
        self.assertIn("3 subtitle(s)", dialog.lbl_count.get_text())

        # 2. Filter: English (index 1)
        dialog.lang_dropdown.set_selected(1)
        dialog._apply_filter()
        self.assertEqual(dialog.stack.get_visible_child_name(), "results")
        self.assertIn("1 subtitle(s)", dialog.lbl_count.get_text())
        self.assertIn("English", dialog.lbl_count.get_text())

        # 3. Filter: German (index 4) -> 0 results
        dialog.lang_dropdown.set_selected(4)
        dialog._apply_filter()
        self.assertEqual(dialog.stack.get_visible_child_name(), "empty")
        self.assertEqual(dialog.lbl_count.get_text(), "0 subtitles found")

    def test_window_track_menus_has_search_subtitle(self):
        from src.window import CineWindow
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        mock_win = MagicMock(spec=CineWindow)
        mock_win.subtitles_menu = Gio.Menu()
        mock_win.audio_tracks_menu = Gio.Menu()
        mock_win.video_tracks_menu = Gio.Menu()
        mock_win.video_tracks_menu_btn = MagicMock()
        mock_win.subtitles_menu_btn = MagicMock()
        mock_win.audio_tracks_menu_btn = MagicMock()

        # Run _update_track_menus unbound on mock_win
        CineWindow._update_track_menus(mock_win, [])

        # Check items in subtitles_menu
        n_items = mock_win.subtitles_menu.get_n_items()
        self.assertGreaterEqual(n_items, 2)

        item0_label = mock_win.subtitles_menu.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_LABEL).get_string()
        item0_action = mock_win.subtitles_menu.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_ACTION).get_string()
        self.assertEqual(item0_label, "Add Subtitle Track")
        self.assertEqual(item0_action, "win.add-sub-tracks")

        item1_label = mock_win.subtitles_menu.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_LABEL).get_string()
        item1_action = mock_win.subtitles_menu.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_ACTION).get_string()
        self.assertEqual(item1_label, "Search Subtitle")
        self.assertEqual(item1_action, "win.search-subtitles")

    def test_is_sdh_track(self):
        # Explicit SDH / CC / Hearing Impaired titles & flags
        self.assertTrue(api.is_sdh_track({"title": "English [SDH]"}))
        self.assertTrue(api.is_sdh_track({"title": "English (SDH)"}))
        self.assertTrue(api.is_sdh_track({"title": "SDH"}))
        self.assertTrue(api.is_sdh_track({"title": "English [CC]"}))
        self.assertTrue(api.is_sdh_track({"title": "English (CC)"}))
        self.assertTrue(api.is_sdh_track({"title": "English [HI]"}))
        self.assertTrue(api.is_sdh_track({"title": "English (Hearing Impaired)"}))
        self.assertTrue(api.is_sdh_track({"title": "Closed Captions"}))
        self.assertTrue(api.is_sdh_track({"hearing-impaired": True}))
        self.assertTrue(api.is_sdh_track({"hearing_impaired": True}))
        self.assertTrue(api.is_sdh_track({"subtitleFileName": "Movie.2024.SDH.srt"}))
        self.assertTrue(api.is_sdh_track({"lang": "en-sdh"}))

        # Non-SDH titles
        self.assertFalse(api.is_sdh_track({"title": "English"}))
        self.assertFalse(api.is_sdh_track({"title": "Spanish"}))
        self.assertFalse(api.is_sdh_track({"title": "Director Commentary"}))
        self.assertFalse(api.is_sdh_track({"title": "Signs & Songs"}))

    def test_find_best_subtitle_track_prefers_sdh(self):
        tracks = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English (Standard)", "external": False},
            {"type": "sub", "id": 2, "lang": "eng", "title": "English [SDH]", "external": False},
        ]
        best = api.find_best_subtitle_track(tracks, preferred_langs=["en"], only_embedded=True)
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], 2)

    def test_find_best_subtitle_track_language_order(self):
        tracks = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English [SDH]", "external": False},
            {"type": "sub", "id": 2, "lang": "spa", "title": "Spanish [SDH]", "external": False},
        ]
        # Spanish is 1st preference -> should pick Spanish SDH
        best_spa = api.find_best_subtitle_track(tracks, preferred_langs=["es", "en"], only_embedded=True)
        self.assertEqual(best_spa["id"], 2)

        # English is 1st preference -> should pick English SDH
        best_eng = api.find_best_subtitle_track(tracks, preferred_langs=["en", "es"], only_embedded=True)
        self.assertEqual(best_eng["id"], 1)

    def test_find_best_subtitle_track_only_embedded(self):
        tracks = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "External English [SDH]", "external": True},
        ]
        best = api.find_best_subtitle_track(tracks, preferred_langs=["en"], only_embedded=True)
        self.assertIsNone(best)

        # When only_embedded is False, external is allowed
        best_ext = api.find_best_subtitle_track(tracks, preferred_langs=["en"], only_embedded=False)
        self.assertIsNotNone(best_ext)
        self.assertEqual(best_ext["id"], 1)

    def test_has_embedded_subtitles(self):
        self.assertFalse(api.has_embedded_subtitles([]))
        self.assertFalse(api.has_embedded_subtitles([
            {"type": "audio", "id": 1, "external": False},
            {"type": "sub", "id": 2, "external": True},
        ]))
        self.assertTrue(api.has_embedded_subtitles([
            {"type": "audio", "id": 1, "external": False},
            {"type": "sub", "id": 2, "external": False},
        ]))

    def test_smart_subtitles_skips_download_when_embedded_present(self):
        from src.window import CineWindow
        mock_win = MagicMock(spec=CineWindow)
        mock_win._is_playing_trailer.return_value = False
        mock_win._subtitles_configured_for_file = False
        mock_win._pending_subtitle_request = {
            "imdb_id": "tt1234567",
            "media_type": "movie",
            "season": None,
            "episode": None,
            "stream_subtitles": None,
            "stream_title": "Test Movie"
        }
        mock_win.mpv = MagicMock()
        mock_win.mpv.idle_active = False
        mock_win.mpv.track_list = [
            {"type": "video", "id": 1, "external": False},
            {"type": "audio", "id": 2, "external": False},
            {"type": "sub", "id": 3, "lang": "eng", "title": "English", "external": False},
            {"type": "sub", "id": 4, "lang": "eng", "title": "English [SDH]", "external": False},
        ]

        # Call _check_and_configure_subtitles unbound
        CineWindow._check_and_configure_subtitles(mock_win)

        # Verify that track 4 (SDH) was selected
        self.assertEqual(mock_win.mpv.sid, 4)
        self.assertTrue(mock_win._subtitles_configured_for_file)
        self.assertIsNone(mock_win._pending_subtitle_request)
        # Verify fetch_and_add_subtitles was NOT called!
        mock_win.fetch_and_add_subtitles.assert_not_called()

    def test_smart_subtitles_downloads_when_no_embedded_present(self):
        from src.window import CineWindow
        mock_win = MagicMock(spec=CineWindow)
        mock_win._is_playing_trailer.return_value = False
        mock_win._subtitles_configured_for_file = False
        mock_win._pending_subtitle_request = {
            "imdb_id": "tt1234567",
            "media_type": "movie",
            "season": None,
            "episode": None,
            "stream_subtitles": None,
            "stream_title": "Test Movie"
        }
        mock_win.mpv = MagicMock()
        mock_win.mpv.idle_active = False
        # Media has NO embedded subtitle tracks
        mock_win.mpv.track_list = [
            {"type": "video", "id": 1, "external": False},
            {"type": "audio", "id": 2, "external": False},
        ]

        # Call _check_and_configure_subtitles unbound
        CineWindow._check_and_configure_subtitles(mock_win)

        # Verify fetch_and_add_subtitles WAS called with pending request!
        mock_win.fetch_and_add_subtitles.assert_called_once_with(
            imdb_id="tt1234567",
            media_type="movie",
            season=None,
            episode=None,
            stream_subtitles=None,
            stream_title="Test Movie"
        )
        self.assertTrue(mock_win._subtitles_configured_for_file)
        self.assertIsNone(mock_win._pending_subtitle_request)

    def test_track_list_observer_auto_selects_sdh_and_clears_pending_download(self):
        from src.window import CineWindow
        mock_win = MagicMock(spec=CineWindow)
        mock_win._is_playing_trailer.return_value = False
        mock_win._subtitles_configured_for_file = False
        mock_win._pending_subtitle_request = {
            "imdb_id": "tt1234567",
            "media_type": "movie"
        }
        mock_win.mpv = MagicMock()
        mock_win.mpv.idle_active = False

        track_list = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English", "external": False},
            {"type": "sub", "id": 2, "lang": "eng", "title": "English [SDH]", "external": False},
        ]

        CineWindow._on_track_list_for_subtitles(mock_win, track_list)

        self.assertEqual(mock_win.mpv.sid, 2)
        self.assertTrue(mock_win._subtitles_configured_for_file)
        self.assertIsNone(mock_win._pending_subtitle_request)

    def test_smart_subtitles_falls_back_to_regular_when_no_sdh(self):
        from src.window import CineWindow
        mock_win = MagicMock(spec=CineWindow)
        mock_win._is_playing_trailer.return_value = False
        mock_win._subtitles_configured_for_file = False
        mock_win._pending_subtitle_request = {
            "imdb_id": "tt1234567",
            "media_type": "movie"
        }
        mock_win.mpv = MagicMock()
        mock_win.mpv.idle_active = False
        # Media has regular English embedded subtitles, no SDH
        mock_win.mpv.track_list = [
            {"type": "sub", "id": 1, "lang": "eng", "title": "English", "external": False},
        ]

        CineWindow._check_and_configure_subtitles(mock_win)

        self.assertEqual(mock_win.mpv.sid, 1)
        self.assertTrue(mock_win._subtitles_configured_for_file)
        self.assertIsNone(mock_win._pending_subtitle_request)
        mock_win.fetch_and_add_subtitles.assert_not_called()

    @patch("src.database.get_cached_subtitles")
    @patch("src.database.save_cached_subtitles")
    @patch("urllib.request.urlopen")
    def test_get_subtitles_ranks_sdh_first(self, mock_urlopen, mock_save_cache, mock_get_cache):
        mock_get_cache.return_value = None
        sample_api_response = {
            "subtitles": [
                {
                    "id": "1",
                    "url": "http://sub-regular.srt",
                    "lang": "en",
                    "subtitleFileName": "Movie.2024.1080p.srt",
                },
                {
                    "id": "2",
                    "url": "http://sub-sdh.srt",
                    "lang": "en",
                    "subtitleFileName": "Movie.2024.1080p.SDH.srt",
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(sample_api_response).encode("utf-8")
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        subs = api.get_subtitles(imdb_id="tt9999999")
        self.assertGreaterEqual(len(subs), 2)
        # First returned subtitle should be the SDH subtitle!
        self.assertEqual(subs[0]["id"], "2")
        self.assertIn("SDH", subs[0]["subtitleFileName"])


if __name__ == "__main__":
    unittest.main()


