import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())


VIDEOS = [
    {"id": "tt0106179:1:1", "season": 1, "episode": 1, "title": "Pilot"},
    {"id": "tt0106179:1:2", "season": 1, "episode": 2, "title": "Deep Throat"},
    {"id": "tt0106179:1:3", "season": 1, "episode": 3, "title": "Squeeze"},
]

SERIES_DETAILS = {
    "id": "tt0106179",
    "title": "The X-Files",
    "type": "series",
    "videos": VIDEOS,
    "medium_cover_image": "https://example.com/poster.jpg",
}


class TestStreamReloadLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        database.close_cache_db()
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database, api
        api.clear_stream_url_cache()
        api.clear_provider_cooldown()
        api.reset_addon_health_cache()
        database.reset_json_cache()
        database.close_cache_db()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def _make_page(self, stub=None):
        from src.window import MovieDetailsPage
        stub = stub or {"id": "tt0106179", "title": "The X-Files", "type": "series"}
        with patch.object(MovieDetailsPage, "load_details_async"), \
             patch.object(MovieDetailsPage, "fetch_torrents_async"):
            return MovieDetailsPage(stub, lambda: None, window=None)

    def test_reload_rearms_aborted_state_and_retries_providers(self):
        from src import api
        from src.window import MovieDetailsPage

        page = self._make_page()
        page.page_abort_event.set()
        page.stream_abort_event = threading.Event()
        page.stream_abort_event.set()
        page._fetching_streams = True
        page._last_fetch_key = "tt0106179_None_None"
        old_page_event = page.page_abort_event
        old_stream_event = page.stream_abort_event

        with patch("src.api.reset_addon_session_status") as reset_session, \
             patch("src.api.clear_provider_cooldown") as clear_cooldown, \
             patch("src.api.clear_stream_url_cache") as clear_urls, \
             patch.object(MovieDetailsPage, "load_details_async") as load_details, \
             patch.object(MovieDetailsPage, "fetch_torrents_async") as fetch_streams:
            page.reload_btn.emit("clicked")

        reset_session.assert_called_once()
        clear_cooldown.assert_called_once()
        clear_urls.assert_called_once()
        load_details.assert_called_once_with(force_refresh=True)
        fetch_streams.assert_called_once_with(force=True)

        self.assertIsNot(page.page_abort_event, old_page_event)
        self.assertFalse(page.page_abort_event.is_set())
        self.assertIsNot(page.stream_abort_event, old_stream_event)
        self.assertFalse(page.stream_abort_event.is_set())
        self.assertFalse(page._fetching_streams)
        self.assertIsNone(page._last_fetch_key)

    def test_handle_addons_changed_drops_stale_streams_and_rearms(self):
        page = self._make_page()
        page.torrents = [{
            "title": "The.X-Files.S01E01.1080p",
            "stream_title": "1080p",
            "is_http": True,
            "addon_names": ["OldProvider"],
            "url": "https://old.example/stream",
        }]
        page.current_t_list = list(page.torrents)
        page.page_abort_event.set()

        page.handle_addons_changed()

        self.assertEqual(page.torrents, [])
        self.assertEqual(page.current_t_list, [])
        self.assertFalse(page.page_abort_event.is_set())

    def test_provider_filter_without_streams_resets_to_all(self):
        from gi.repository import Gtk

        page = self._make_page(stub={"id": "tt0106179", "title": "The X-Files", "type": "movie"})
        page.update_provider_dropdown_model = lambda: None
        page.provider_dropdown.set_model(Gtk.StringList.new(["All Providers", "GGplay"]))
        page.provider_dropdown.set_selected(1)
        page.source_idx = 0
        page._fetching_streams = False
        page.torrents = [{
            "title": "The.X-Files.1080p",
            "stream_title": "1080p",
            "is_http": True,
            "addon_names": ["OtherProvider"],
            "url": "https://other.example/stream",
        }]

        page.update_quality_dropdown()

        self.assertEqual(page.provider_dropdown.get_selected(), 0)
        self.assertTrue(page.current_t_list, "available streams must remain visible")

    def test_provider_filter_kept_when_only_source_filter_hides_streams(self):
        from gi.repository import Gtk

        page = self._make_page(stub={"id": "tt0106179", "title": "The X-Files", "type": "movie"})
        page.update_provider_dropdown_model = lambda: None
        page.provider_dropdown.set_model(Gtk.StringList.new(["All Providers", "GGplay"]))
        page.provider_dropdown.set_selected(1)
        page.source_idx = 1  # Direct filter, provider only has torrent streams
        page._fetching_streams = False
        page.torrents = [{
            "title": "The.X-Files.1080p",
            "is_http": False,
            "magnet": "magnet:?xt=urn:btih:abcdef123456",
            "addon_names": ["GGplay"],
        }]

        page.update_quality_dropdown()

        self.assertEqual(page.provider_dropdown.get_selected(), 1)
        self.assertEqual(page.current_t_list, [])
        self.assertEqual(
            page.file_dropdown.get_model().get_string(0),
            "No streams available for selected source filter",
        )

    def test_empty_provider_result_falls_back_to_all_providers(self):
        from gi.repository import Gtk
        from src.window import MovieDetailsPage

        page = self._make_page(stub={"id": "tt0106179", "title": "The X-Files", "type": "movie"})
        page.update_provider_dropdown_model = lambda: None
        page.provider_dropdown.set_model(Gtk.StringList.new(["All Providers", "GGplay"]))
        page.provider_dropdown.set_selected(1)
        page._fetching_streams = False

        with patch.object(page, "update_quality_dropdown"), \
             patch("src.window.GLib.idle_add") as idle_add:
            page._fallback_to_all_providers()

        self.assertEqual(page.provider_dropdown.get_selected(), 0)
        idle_add.assert_called_once_with(page._refetch_after_provider_fallback)

        with patch.object(MovieDetailsPage, "fetch_torrents_async") as fetch_streams:
            page._refetch_after_provider_fallback()
        fetch_streams.assert_called_once_with(force=True)

    def test_metadata_refresh_keeps_user_selected_episode(self):
        from src import database
        from src.window import MovieDetailsPage

        page = self._make_page()
        database.save_continue_watching({
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series",
            "season": 1,
            "episode": 3,
            "position": 600.0,
            "duration": 2700.0,
        })
        page._user_selected_episode = True
        page.selected_season = 1
        page.selected_episode = 1
        page.selected_video = VIDEOS[0]
        page._preferred_series_season = 1
        page._preferred_series_episode = 1
        page._suppress_stream_reload = True

        page.build_ui(dict(SERIES_DETAILS), is_refresh=True)

        self.assertEqual(page.selected_episode, 1)
        self.assertEqual(page.episode_dropdown.get_selected(), 0)

    def test_metadata_refresh_restores_resume_without_user_selection(self):
        from src import database
        from src.window import MovieDetailsPage

        page = self._make_page()
        database.save_continue_watching({
            "id": "tt0106179",
            "title": "The X-Files",
            "type": "series",
            "season": 1,
            "episode": 3,
            "position": 600.0,
            "duration": 2700.0,
        })
        page.selected_season = 1
        page.selected_episode = 1
        page.selected_video = VIDEOS[0]
        page._preferred_series_season = 1
        page._preferred_series_episode = 1
        page._suppress_stream_reload = True

        with patch.object(MovieDetailsPage, "fetch_torrents_async"):
            page.build_ui(dict(SERIES_DETAILS), is_refresh=True)

        self.assertEqual(page.selected_episode, 3)

    def test_episode_mismatch_schedules_refetch_for_active_episode(self):
        from src.window import MovieDetailsPage

        page = self._make_page()
        page.update_provider_dropdown_model = lambda: None
        page.source_idx = 0
        page._fetching_streams = False
        page.selected_season = 1
        page.selected_episode = 1
        page.selected_video = VIDEOS[0]
        page._stream_item_key = "tt0106179:1:3_1_3"
        page.torrents = [{
            "title": "The.X-Files.S01E03.Squeeze.1080p",
            "stream_title": "1080p",
            "is_http": True,
            "addon_names": ["Provider"],
            "url": "https://provider.example/stream",
        }]

        with patch("src.window.GLib.idle_add") as idle_add:
            page.update_quality_dropdown()

        idle_add.assert_any_call(page._refetch_for_selected_episode)
        self.assertEqual(page.current_t_list, [])
        self.assertEqual(
            page.file_dropdown.get_model().get_string(0),
            "No streams available for selected episode",
        )

        with patch.object(MovieDetailsPage, "fetch_torrents_async") as fetch_streams:
            page._refetch_for_selected_episode()
        fetch_streams.assert_called_once_with(force=False)

        page._stream_item_key = "tt0106179:1:1_1_1"
        with patch.object(MovieDetailsPage, "fetch_torrents_async") as fetch_streams:
            page._refetch_for_selected_episode()
        fetch_streams.assert_not_called()

    def test_clear_stream_url_cache_drops_negative_entries(self):
        from src import api

        url = "https://providers.test/stream/series/tt0106179:1:1.json"
        api._save_cached_stream_url(url, [], 600)
        self.assertEqual(api._get_cached_stream_url(url), [])

        api.clear_stream_url_cache()

        self.assertIsNone(api._get_cached_stream_url(url))

    def test_clear_provider_cooldown_releases_host(self):
        from src import api

        manifest = "https://provider.test/manifest.json"
        api.set_provider_cooldown(manifest, 300, reason="test")
        self.assertTrue(api.is_provider_in_cooldown(manifest))

        api.clear_provider_cooldown()

        self.assertFalse(api.is_provider_in_cooldown(manifest))

    def test_provider_dropdown_omits_offline_and_cooling_providers(self):
        from src import api, database

        addons = [
            {
                "id": "test.online",
                "name": "OnlineProvider",
                "manifest_url": "https://online.provider/manifest.json",
                "resources": ["stream"],
                "types": ["movie"],
                "catalogs": [],
            },
            {
                "id": "test.offline",
                "name": "OfflineProvider",
                "manifest_url": "https://offline.provider/manifest.json",
                "resources": ["stream"],
                "types": ["movie"],
                "catalogs": [],
            },
            {
                "id": "test.cooldown",
                "name": "CooldownProvider",
                "manifest_url": "https://cooldown.provider/manifest.json",
                "resources": ["stream"],
                "types": ["movie"],
                "catalogs": [],
            },
        ]
        for addon in addons:
            database.add_addon(dict(addon))
        api.set_addon_online_status(addons[1]["manifest_url"], False)
        api.set_provider_cooldown(addons[2]["manifest_url"], 300, reason="test")

        page = self._make_page(stub={"id": "tt0106179", "title": "The X-Files", "type": "movie"})
        page.torrents = [{
            "title": "The.X-Files.1080p",
            "is_http": True,
            "addon_names": ["OnlineProvider", "OfflineProvider", "CachedOnlyProvider"],
            "url": "https://online.provider/stream",
        }]

        page.update_provider_dropdown_model()

        model = page.provider_dropdown.get_model()
        strings = [model.get_string(i) for i in range(model.get_n_items())]
        self.assertEqual(strings, ["All Providers", "CachedOnlyProvider", "OnlineProvider"])


if __name__ == "__main__":
    unittest.main()
