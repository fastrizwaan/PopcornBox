import json
import unittest
from unittest.mock import MagicMock, patch

from src import api, database, plugins


class TestWordBasedStreamSearch(unittest.TestCase):
    def test_modern_family_rejects_family_malayalam_and_unrelated_files(self):
        bad_streams = [
            {
                "name": "UHDMovies",
                "title": "🎬 Family (2023) | Malayalam | 1080p",
                "url": "https://cdn.example.com/Family.2023.Malayalam.1080p.WEB-DL.mkv",
            },
            {
                "name": "MovieBox",
                "title": "Family (2023) Malayalam Full Movie",
                "url": "https://cdn.example.com/opaque/12345",
            },
            {
                "name": "HDHub4u",
                "title": "Modern Family S01E01",
                "behaviorHints": {"filename": "Family.2023.Malayalam.1080p.WEB-DL.mkv"},
                "url": "https://cdn.example.com/file/abc",
            },
            {
                "name": "TopCartoons",
                "title": "TopCartoons • Unknown",
                "url": "https://ww.topcartoons.tv/video/Dexters-Laboratory-Ol-McDexter.mp4",
            },
        ]
        for s in bad_streams:
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="Modern Family"),
                api.MATCH_MISMATCH,
                f"Expected MISMATCH for {s}",
            )
            self.assertEqual(
                api.match_stream_to_episode(s, 1, 1, series_title="Modern Family"),
                api.MATCH_MISMATCH,
                f"Expected episode MISMATCH for {s}",
            )

    def test_modern_family_accepts_valid_streams(self):
        good_streams = [
            {
                "name": "1Shows • 480p",
                "title": "🍿 Modern Family (2009) | S1E1\n480p • Multi-Audio",
                "url": "https://pixeldrain.com/api/file/6r3RAeip",
            },
            {
                "name": "Movix VF",
                "title": "🍿 S1 E1 - Pilot | Modern Family\n1080p • Dual-Audio",
                "url": "https://zebi.senpai-stream.club/tv/1421/S1/E1/master.m3u8",
            },
            {
                "name": "DahmerMovies",
                "title": "Modern Family (2009) - S01E01 - Pilot [Bluray-1080p].mkv",
                "url": "https://a.111477.xyz/tvs/Modern%20Family/Season%201/Modern.Family.S01E01.1080p.mkv",
            },
            {
                "name": "UHDMovies",
                "title": "UHDMovies - 1080p [2.5 GB]",
                "url": "https://cdn.example.com/stream/8472920182",
            },
        ]
        for s in good_streams:
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="Modern Family"),
                api.MATCH_UNKNOWN,
                f"Expected compatible for {s}",
            )
            self.assertNotEqual(
                api.match_stream_to_episode(s, 1, 1, series_title="Modern Family"),
                api.MATCH_MISMATCH,
                f"Expected episode not rejected for {s}",
            )

    def test_family_does_not_match_modern_family_or_family_man(self):
        wrong_for_family = [
            {
                "name": "Torrentio",
                "title": "Modern.Family.S01E01.1080p.WEB-DL.mkv",
                "url": "https://cdn.example.com/Modern.Family.S01E01.1080p.WEB-DL.mkv",
            },
            {
                "name": "VidRock",
                "title": "🎬 Modern Family - (2009) | S1E1",
                "url": "https://cdn.example.com/tv/123.m3u8",
            },
            {
                "name": "Stream",
                "title": "Family.Man.S01E01.1080p.WEB-DL.mkv",
                "url": "https://cdn.example.com/Family.Man.S01E01.1080p.WEB-DL.mkv",
            },
            {
                "name": "Stream",
                "title": "Family.Guy.S01E01.1080p.mkv",
                "url": "https://cdn.example.com/Family.Guy.S01E01.1080p.mkv",
            },
        ]
        for s in wrong_for_family:
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="Family"),
                api.MATCH_MISMATCH,
                f"Expected Family to reject {s}",
            )

        right_for_family = {
            "name": "HDHub4u",
            "title": "🎬 Family (2023) | Malayalam | 1080p",
            "url": "https://cdn.example.com/Family.2023.Malayalam.1080p.WEB-DL.mkv",
        }
        self.assertEqual(api.match_stream_to_movie(right_for_family, target_title="Family"), api.MATCH_UNKNOWN)

    def test_episode_matcher_rejects_explicit_full_movie_for_series(self):
        movie_stream = {
            "name": "Bollyflix",
            "title": "Some Show - Hindi Malayalam Movie Full Movie HDRip",
            "url": "https://cdn.example.com/stream/abc",
        }
        self.assertEqual(api.match_stream_to_episode(movie_stream, 1, 1), api.MATCH_MISMATCH)

    def test_plugins_execute_scraper_rejects_mismatched_selected_log(self):
        scraper = {"id": "test:hdhub4u", "name": "HDHub4u", "code": "module.exports={getStreams:()=>[]}"}
        fake_stdout = (
            '[HDHub4u] TMDB Info: "Modern Family" (2009)\n'
            '[HDHub4u] Selected: "Download Family (2023) Malayalam Full Movie" (https://example.com/family)\n'
            + json.dumps([{"name": "HDHub4u", "title": "Modern Family S01E01", "url": "https://example.com/abc"}])
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.poll.return_value = 0
        proc.communicate.return_value = (fake_stdout, "")

        with patch.object(plugins, "build_scraper_command", return_value=["deno", "run", "runner.js"]), \
             patch("subprocess.Popen", return_value=proc):
            res = plugins.execute_scraper(
                scraper, "1421", "tv", season=1, episode=1, expected_title="Modern Family"
            )
        self.assertEqual(res, [])

    def test_fetch_items_uses_whole_word_matching_and_ranking(self):
        fake_addon = {
            "name": "FakeCatalog",
            "manifest_url": "https://fake.example.com/manifest.json",
            "enabled": True,
            "catalogs": [{"type": "series", "id": "top", "extra": ["search"]}],
        }
        metas = [
            {"id": "tt001", "type": "movie", "name": "Family", "description": "A Malayalam drama about a modern family."},
            {"id": "tt002", "type": "movie", "name": "Modernity & Familiar", "description": "Not whole words."},
            {"id": "tt003", "type": "series", "name": "Modern Family", "releaseInfo": "2009", "description": "Mockumentary."},
        ]
        with patch.object(database, "get_addons", return_value=[fake_addon]), \
             patch.object(api, "is_addon_online", return_value=True), \
             patch.object(api, "_get_cached_request", return_value={"metas": metas}), \
             patch.object(database, "hydrate_items_from_metadata"), \
             patch.object(api, "hydrate_missing_catalog_items"), \
             patch.object(database, "save_cached_metadata"):
            title_only_res = api.fetch_items(
                media_type="series",
                query="Modern Family",
                target_manifest_url="https://fake.example.com/manifest.json",
                target_catalog_id="top",
                title_only=True,
            )
            self.assertEqual([r["title"] for r in title_only_res], ["Modern Family"])

            full_res = api.fetch_items(
                media_type="series",
                query="Modern Family",
                target_manifest_url="https://fake.example.com/manifest.json",
                target_catalog_id="top",
                title_only=False,
            )
            # Whole-word exact title match is ranked first; partial substring 'Modernity & Familiar' is excluded
            self.assertEqual(full_res[0]["title"], "Modern Family")
            self.assertNotIn("Modernity & Familiar", [r["title"] for r in full_res])

    def test_database_search_uses_whole_word_matching(self):
        database.save_cached_catalog(
            "unit-test-word-search",
            [
                {"id": "tt88001", "type": "movie", "title": "Romance", "name": "Romance", "description": "Love story"},
                {"id": "tt88002", "type": "movie", "title": "Spider-Man", "name": "Spider-Man", "description": "Action"},
                {"id": "tt88003", "type": "movie", "title": "Man", "name": "Man", "description": "Drama"},
            ],
        )
        res = database.get_cached_items_by_search("Man", title_only=True, limit=50)
        titles = [r.get("title") for r in res]
        self.assertIn("Man", titles)
        self.assertIn("Spider-Man", titles)
        self.assertNotIn("Romance", titles)
        self.assertEqual(titles[0], "Man")

    def test_filter_non_media_html_streams_drops_shorteners_and_keeps_video(self):
        shortener_stream = {
            "name": "UHDMovies",
            "title": "UHDMovies - 1080p",
            "url": "https://en.thenaukriadda.in/?sid=THpveVRFRjJp",
        }
        generic_root_html = {
            "name": "SomeAddon",
            "title": "SomeAddon - 1080p",
            "url": "https://example-redirect.test/?sid=abc123",
        }
        valid_vidrock = {
            "name": "🪨 VidRock",
            "title": "🪨 VidRock | 480p",
            "url": "https://vercetti.org/movie/P6e0z/bigtits.m3u8",
        }
        valid_bare_video = {
            "name": "CDN",
            "title": "CDN - 1080p",
            "url": "https://real-video-cdn.test/?sid=999",
        }

        def _fake_probe(u, headers=None, timeout=2.5):
            return "example-redirect.test" in u

        with patch.object(api, "_probe_url_returns_html", side_effect=_fake_probe):
            kept = api.filter_non_media_html_streams(
                [shortener_stream, generic_root_html, valid_vidrock, valid_bare_video],
                allow_network_probe=True,
            )

        self.assertEqual([s["url"] for s in kept], [valid_vidrock["url"], valid_bare_video["url"]])

    def test_filter_non_media_drops_cf_challenge_wrapper_and_sibling_redirects(self):
        import urllib.error
        bulk_wrapper = {
            "name": "DahmerMovies",
            "title": "DahmerMovies 1080p",
            "url": "https://p.cf-test-cdn.invalid/bulk?u=https%3A%2F%2Fa.cf-test-cdn.invalid%2Fmovies%2FTest.mkv",
        }
        sibling_mkv = {
            "name": "DahmerMovies",
            "title": "DahmerMovies 1080p",
            "url": "https://a.cf-test-cdn.invalid/movies/Test.2026.1080p.mkv",
        }
        pixeldrain_stream = {
            "name": "Pixeldrain",
            "title": "Test.2026.1080p.mkv",
            "url": "https://pixeldrain.dev/api/file/awpMXWe9?download",
        }

        def _fake_urlopen(req, timeout=2.5, context=None):
            u = req.full_url if hasattr(req, "full_url") else str(req)
            if "cf-test-cdn.invalid" in u:
                raise urllib.error.HTTPError(
                    u, 403, "Forbidden", {"cf-mitigated": "challenge", "Content-Type": "text/html"}, None
                )
            raise AssertionError(f"Unexpected probe on {u}")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            kept = api.filter_non_media_html_streams(
                [bulk_wrapper, sibling_mkv, pixeldrain_stream],
                allow_network_probe=True,
            )

        self.assertEqual([s["url"] for s in kept], [pixeldrain_stream["url"]])

    def test_subtitle_configuration_defers_before_file_loaded_and_handles_mpv_minus_12(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        sys.modules.setdefault("mpv", MagicMock())
        if "GSETTINGS_SCHEMA_DIR" not in os.environ:
            _schema_dir = tempfile.TemporaryDirectory()
            self.addCleanup(_schema_dir.cleanup)
            _schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
            if _schema_path.exists():
                _dest = Path(_schema_dir.name) / _schema_path.name
                _dest.write_text(_schema_path.read_text())
                subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
                os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gio, GLib
        res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
        if res_file.exists():
            try:
                Gio.resources_register(Gio.Resource.load(str(res_file)))
            except Exception:
                pass

        from src.window import PopcornBoxWindow

        # 1. _check_and_configure_subtitles must defer cleanly while _mpv_file_loaded is False
        win = MagicMock()
        win._user_selected_subtitle = False
        win._subtitles_configured_for_file = False
        win._is_playing_trailer.return_value = False
        win.mpv = MagicMock()
        win.mpv.idle_active = False
        win._mpv_file_loaded = False
        PopcornBoxWindow._check_and_configure_subtitles(win)
        win._apply_external_subtitle.assert_not_called()
        win.fetch_and_add_subtitles.assert_not_called()

        # 2. When saved_subtitle hits MPV error -12, queue pending_subtitles instead of re-downloading
        win._mpv_file_loaded = True
        win._extracted_hls_subtitles = None
        win._hls_prep_url = None
        win._get_saved_subtitle_preference.return_value = None
        win._get_saved_external_subtitle.return_value = {
            "path": "/tmp/fake_sub.srt",
            "lang": "eng",
            "title": "External (eng)",
        }
        win._apply_external_subtitle.side_effect = SystemError("Error running mpv command", -12)
        with patch("os.path.exists", return_value=True):
            PopcornBoxWindow._check_and_configure_subtitles(win)

        win.fetch_and_add_subtitles.assert_not_called()
        self.assertEqual(win.pending_subtitles, [("/tmp/fake_sub.srt", "eng", True)])
        win._try_add_pending_subtitles.assert_called_once()

        # 3. _try_add_pending_subtitles schedules bounded retry on -12 instead of immediate failure
        win2 = MagicMock()
        win2.mpv = MagicMock()
        win2.mpv.idle_active = False
        win2.mpv.sid = "no"
        win2._mpv_file_loaded = True
        win2._user_selected_subtitle = False
        win2._get_saved_subtitle_preference.return_value = None
        win2._apply_external_subtitle.side_effect = SystemError("Error running mpv command", -12)
        win2.pending_subtitles = [("/tmp/fake_sub.srt", "eng", True)]
        with patch.object(GLib, "timeout_add") as mock_timer:
            PopcornBoxWindow._try_add_pending_subtitles(win2, retry_count=0)
        mock_timer.assert_called_once()

    def test_you_can_see_everything_rejects_see_variants_and_vice_versa(self):
        wrong_streams = [
            {"name": "MovieBox", "title": "🎬 See • 1080p", "url": "https://cdn.example.com/1"},
            {"name": "UHDMovies", "title": "See - 1080p", "url": "https://cdn.example.com/2"},
            {"name": "Torrentio", "title": "See S01E01 1080p", "url": "https://cdn.example.com/3"},
            {"name": "Provider", "title": "See\n1080p • 2.1 GB", "url": "https://cdn.example.com/4"},
            {"name": "HDHub4u", "title": "See.2019.S01E01.1080p.WEB-DL.mkv", "url": "https://cdn.example.com/See.2019.S01E01.1080p.WEB-DL.mkv"},
        ]
        for s in wrong_streams:
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="You Can See Everything"),
                api.MATCH_MISMATCH,
                f"Expected MISMATCH for {s}",
            )

        right_streams = [
            {"name": "🪨 VidRock | 360p | 🌍 [Nova]", "title": "🎬 You Can See Everything - (2026)\n💎 360p", "url": "https://vercetti.org/movie/abc/playlist.m3u8"},
            {"name": "🏰 Castle", "title": "You Can See Everything (2026)\n1080P • 869 MB", "url": "https://img1.hscow.com/hls_mps/abc/index_287.m3u8"},
            {"name": "1Shows", "title": "🍿 You Can See Everything (2026)\n🌟 2160p", "url": "https://example.workers.dev/abc"},
        ]
        for s in right_streams:
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="You Can See Everything"),
                api.MATCH_UNKNOWN,
                f"Expected compatible for {s}",
            )
            # And reverse: searching for 'See' must NOT accept 'You Can See Everything'
            self.assertEqual(
                api.match_stream_to_movie(s, target_title="See"),
                api.MATCH_MISMATCH,
                f"Expected 'See' to reject {s}",
            )

    def test_nuvio_provider_streams_survive_process_raw_streams(self):
        raw_streams = [
            {
                "name": "4KHDHub - HubCloud 10Gbps 2160p",
                "title": "The Matrix (1999) REMASTERED UHD 2160p 10bit HDR BluRay\n💾 12.5 GB • HUBCLOUD",
                "url": "https://pixel.hubcloud.mov/?id=23b18ca583293b84aacf8398",
                "addon_name": "4KHDHub",
                "is_nuvio_plugin": True,
            },
            {
                "name": "Torrentio | 👤 372 | 2160P",
                "title": "🎬 The Matrix - 1999\n🔥 2160p | DV • HDR10 • English • 👥 372",
                "infoHash": "0123456789abcdef0123456789abcdef01234567",
                "addon_name": "Torrentio",
                "is_nuvio_plugin": True,
            },
            {
                "name": "StreamFlix",
                "title": "The Matrix - Premium Quality\n1080p • 2h 16m",
                "url": "https://s3.ap-southeast-1.wasabisys.com/streamflix/movies/1999/thematrix.mkv",
                "addon_name": "StreamFlix",
                "is_nuvio_plugin": True,
            },
            {
                "name": "StreamFlix",
                "title": "The Matrix - Standard Quality\n720p • 2h 16m",
                "url": "https://bb.streamflixserver.site/file/streamflix/movies/1999/thematrix.mkv",
                "addon_name": "StreamFlix",
                "is_nuvio_plugin": True,
            },
            {
                "name": "Purstream | hd | Dual-Audio",
                "title": "🎬 The Matrix - 1999\n🔥 hdp | 🔊 Dual-Audio",
                "url": "https://cdn.example.com/purstream/matrix.m3u8",
                "addon_name": "Purstream",
                "is_nuvio_plugin": True,
            },
        ]
        processed = api.process_raw_streams(raw_streams, title="The Matrix")
        self.assertEqual(len(processed), 5)


if __name__ == "__main__":
    unittest.main()
