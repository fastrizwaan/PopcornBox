import datetime
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

mpv_mock = MagicMock()
mpv_mock.ShutdownError = type("ShutdownError", (Exception,), {})
sys.modules["mpv"] = mpv_mock

from gi.repository import Gio, Gdk, GLib


class TestRuntimeDefectFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
        if not res_file.exists():
            res_file = Path("cine.gresource")
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
        self.addCleanup(self.temp_dir.cleanup)

        from src import api
        with api._REFRESH_RESULT_CACHE_LOCK:
            api._REFRESH_RESULT_CACHE.clear()

    def tearDown(self):
        from src import player
        with player._engines_lock:
            player._engines.clear()
            player._launching_engines.clear()
            player._progress_poll_gen.clear()
            player._launch_cancel_gen.clear()
            player._streaming_hash = None

    def _drain_main_loop(self):
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass

    # ------------------------------------------------------------------
    # player.py: one progress poller per engine playback
    # ------------------------------------------------------------------
    def test_progress_poller_supersedes_previous_instance(self):
        from src import player

        info_hash = "f" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True

        stats_idents = []
        first_seen = threading.Event()
        second_poller_seen = threading.Event()

        def stats():
            ident = threading.get_ident()
            stats_idents.append(ident)
            if len(stats_idents) == 1:
                first_seen.set()
            elif ident != stats_idents[0]:
                second_poller_seen.set()
            return {"status": "Downloading"}

        engine.stats.side_effect = stats

        with player._engines_lock:
            player._engines[info_hash] = engine
            player._streaming_hash = info_hash

        release = threading.Event()

        def fake_sleep(_seconds):
            release.wait(timeout=2)
            time.sleep(0.001)

        fake_time = MagicMock()
        fake_time.sleep.side_effect = fake_sleep

        try:
            with patch.object(player, "time", fake_time):
                player._start_progress_poller(info_hash, None)
                self.assertTrue(first_seen.wait(2), "first poller never polled")

                player._start_progress_poller(info_hash, None)
                self.assertTrue(second_poller_seen.wait(2), "second poller never polled")

                first_ident = stats_idents[0]
                release.set()

                # Let any superseded poller wake up and take another iteration.
                time.sleep(0.15)
                self.assertEqual(
                    stats_idents.count(first_ident), 1,
                    "superseded poller kept polling the same engine"
                )
        finally:
            engine.is_alive.return_value = False
            release.set()
            with player._engines_lock:
                player._engines.pop(info_hash, None)
                player._progress_poll_gen.pop(info_hash, None)
                if player._streaming_hash == info_hash:
                    player._streaming_hash = None

    def test_progress_poller_stops_when_generation_cleared(self):
        from src import player

        info_hash = "e" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True
        seen = threading.Event()

        def stats():
            seen.set()
            return {"status": "Downloading"}

        engine.stats.side_effect = stats

        with player._engines_lock:
            player._engines[info_hash] = engine
            player._streaming_hash = info_hash

        release = threading.Event()

        def fake_sleep(_seconds):
            release.wait(timeout=2)
            time.sleep(0.001)

        fake_time = MagicMock()
        fake_time.sleep.side_effect = fake_sleep

        try:
            with patch.object(player, "time", fake_time):
                player._start_progress_poller(info_hash, None)
                self.assertTrue(seen.wait(2))
                with player._engines_lock:
                    player._progress_poll_gen.pop(info_hash, None)
                release.set()
                before = engine.stats.call_count
                time.sleep(0.1)
                self.assertEqual(engine.stats.call_count, before)
        finally:
            engine.is_alive.return_value = False
            release.set()
            with player._engines_lock:
                player._engines.pop(info_hash, None)
                player._progress_poll_gen.pop(info_hash, None)
                if player._streaming_hash == info_hash:
                    player._streaming_hash = None

    # ------------------------------------------------------------------
    # player.py: launch failures must fail over instead of hanging
    # ------------------------------------------------------------------
    class _InlineThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            return None

    def _run_play_magnet_with_inline_threads(self, magnet, callback):
        from src import player

        with patch.object(player.threading, "Thread", self._InlineThread), \
             patch("src.libtorrent_stream.info_hash_from_magnet", return_value="d" * 40):
            player.play_magnet(magnet, progress_callback=callback)

    def test_launch_constructor_failure_reports_ready_false(self):
        from src import player

        callbacks = []
        with patch.object(player, "TorrentStreamEngine", side_effect=RuntimeError("engine boom")):
            self._run_play_magnet_with_inline_threads(
                "magnet:?xt=urn:btih:" + "d" * 40,
                lambda stats: callbacks.append(stats),
            )
        self._drain_main_loop()

        self.assertTrue(callbacks)
        self.assertEqual(callbacks[-1].get("status"), "Error: engine boom")
        self.assertFalse(callbacks[-1].get("ready", True))
        self.assertIsNone(player._streaming_hash)

    def test_launch_start_failure_cleans_up_partial_engine(self):
        from src import player

        info_hash = "d" * 40
        engine = MagicMock()
        engine.start.side_effect = RuntimeError("start boom")

        callbacks = []
        with patch.object(player, "TorrentStreamEngine", return_value=engine), \
             patch.object(player.database, "set_download_paused"):
            self._run_play_magnet_with_inline_threads(
                "magnet:?xt=urn:btih:" + info_hash,
                lambda stats: callbacks.append(stats),
            )
        self._drain_main_loop()

        self.assertTrue(callbacks)
        self.assertEqual(callbacks[-1].get("status"), "Error: start boom")
        self.assertFalse(callbacks[-1].get("ready", True))
        self.assertNotIn(info_hash, player._engines)
        self.assertIsNone(player._streaming_hash)
        engine.stop.assert_called()

    # ------------------------------------------------------------------
    # download_row.py: delete must wait for the engine to release files
    # ------------------------------------------------------------------
    def test_download_delete_waits_for_engine_stop_before_rmtree(self):
        from src import download_row

        stopped = threading.Event()
        rmtree_called = threading.Event()
        order = []

        class FakeStopThread:
            def join(self, timeout=None):
                stopped.set()
                order.append("join")

        def fake_rmtree(path, ignore_errors=False):
            self.assertTrue(stopped.is_set(), "rmtree ran before engine stop completed")
            order.append("rmtree")
            rmtree_called.set()

        def fake_wait_launch(info_hash, timeout=15.0):
            self.assertTrue(stopped.is_set(), "waited for launch before engine stop")
            self.assertEqual(info_hash, "c" * 40)
            order.append("wait_launch")
            return True

        row = SimpleNamespace(
            info_hash="c" * 40,
            on_destroy=MagicMock(),
            get_parent=MagicMock(return_value=None),
        )

        with patch.object(download_row.player, "stop_engine_explicit", return_value=FakeStopThread()), \
             patch.object(download_row.player, "wait_for_pending_launch", side_effect=fake_wait_launch), \
             patch.object(download_row.database, "remove_download") as mock_remove, \
             patch.object(download_row.shutil, "rmtree", side_effect=fake_rmtree):
            download_row.DownloadItemRow.on_delete_clicked(row, None)
            self.assertTrue(rmtree_called.wait(2), "download files were never deleted")

        mock_remove.assert_called_once_with("c" * 40)
        self.assertEqual(order, ["join", "wait_launch", "rmtree"])
        row.on_destroy.assert_called_once()

    # ------------------------------------------------------------------
    # api.py: auto-heal must bypass the short-lived URL cache
    # ------------------------------------------------------------------
    def test_refresh_stream_requests_url_cache_bypass(self):
        from src import api

        old_stream = {
            "url": "https://debrid.example/old",
            "is_http": True,
            "addon_names": ["Torrentio"],
            "quality": "1080p",
        }
        fresh = {
            "url": "https://debrid.example/new",
            "is_http": True,
            "addon_names": ["Torrentio"],
            "quality": "1080p",
        }

        with patch.object(api, "get_torrents", return_value=[fresh]) as mock_get:
            refreshed, candidates = api.refresh_stream(
                "tt1234567", media_type="movie", old_stream=old_stream, provider="Torrentio"
            )

        self.assertEqual(refreshed["url"], fresh["url"])
        self.assertTrue(refreshed.get("_refreshed"))
        self.assertTrue(mock_get.call_args.kwargs.get("bypass_url_cache"))

    def test_refresh_stream_memoizes_provider_fetch_for_failed_batch(self):
        from src import api

        old_stream = {
            "url": "https://memo.example/old",
            "is_http": True,
            "addon_names": ["MemoProvider"],
            "filename": "Show.S02E01.1080p.mkv",
            "quality": "1080p",
        }
        fresh = {
            "url": "https://memo.example/new",
            "is_http": True,
            "addon_names": ["MemoProvider"],
            "filename": "Show.S02E01.1080p.mkv",
            "quality": "1080p",
        }

        with patch.object(api, "get_torrents", return_value=[fresh]) as mock_get:
            api.refresh_stream(
                "tt5550001", media_type="series", season=2, episode=1,
                old_stream=old_stream, provider="MemoProvider"
            )
            api.refresh_stream(
                "tt5550001", media_type="series", season=2, episode=1,
                old_stream=old_stream, provider="MemoProvider"
            )
            # A different episode must not reuse the memoized candidate list.
            api.refresh_stream(
                "tt5550001", media_type="series", season=2, episode=2,
                old_stream=old_stream, provider="MemoProvider"
            )

        self.assertEqual(mock_get.call_count, 2)

    def test_request_addon_streams_skips_url_cache_when_bypassed(self):
        from src import api

        addon = {"name": "BypassAddon", "manifest_url": "https://bypass-host.example/manifest.json"}
        url = "https://bypass-host.example/stream/movie/tt1234567.json"
        payload = json.dumps({"streams": [{"url": "https://fresh.example/stream"}]}).encode()

        response = MagicMock()
        response.read.return_value = payload
        response.__enter__ = lambda s: s
        response.__exit__ = lambda s, *a: False

        with patch.object(api, "_get_cached_stream_url", return_value=[{"url": "https://stale.example"}]) as mock_cache, \
             patch.object(api, "_save_cached_stream_url"), \
             patch.object(api, "is_provider_in_cooldown", return_value=False), \
             patch.object(api, "_record_addon_success"), \
             patch.object(api.urllib.request, "urlopen", return_value=response):
            result = api._request_addon_streams(addon, url, timeout=5, bypass_url_cache=True)

        self.assertEqual(result, [{"url": "https://fresh.example/stream"}])
        mock_cache.assert_not_called()

    # ------------------------------------------------------------------
    # main.py: skip sidecar files inside subdirectories
    # ------------------------------------------------------------------
    def test_find_first_file_skips_sidecars_in_subdirectory(self):
        from src.main import CineApplication

        folder = Path(self.temp_dir.name) / "movie-folder"
        folder.mkdir()
        (folder / "a_notes.nfo").write_text("metadata")
        video = folder / "b_movie.mkv"
        video.write_bytes(b"0")

        class DummyApp:
            def find_first_file(self, gfile, visited=None):
                return CineApplication.find_first_file(self, gfile, visited)

        found = DummyApp().find_first_file(Gio.File.new_for_path(str(folder)))
        self.assertEqual(found, str(video))

    # ------------------------------------------------------------------
    # playlist.py: unreadable dropped files must not wedge the spinner
    # ------------------------------------------------------------------
    def test_drop_unreadable_file_still_hides_spinner(self):
        from src.playlist import Playlist

        page = SimpleNamespace(mpv=MagicMock(), spinner=MagicMock())
        missing = Gio.File.new_for_path(str(Path(self.temp_dir.name) / "missing.mkv"))
        file_list = Gdk.FileList.new_from_list([missing])

        Playlist._on_drop(page, None, file_list, 0, 0)

        page.spinner.set_visible.assert_called_once_with(False)

    # ------------------------------------------------------------------
    # history.py: delete removes every same-day duplicate
    # ------------------------------------------------------------------
    def test_history_delete_removes_all_same_day_duplicates(self):
        from src.history import HistoryDialog

        hist_path = Path(self.temp_dir.name) / "history.jsonl"
        path = "/videos/sample.mkv"
        day = datetime.datetime(2026, 10, 20, 12, 0, 0)
        day_key = day.strftime("%Y-%m-%d")
        newer_same_day = day + datetime.timedelta(hours=1)
        previous_day = day - datetime.timedelta(days=1)

        lines = [
            json.dumps({"path": path, "time": day.timestamp()}),
            json.dumps({"path": path, "time": newer_same_day.timestamp()}),
            json.dumps({"path": path, "time": previous_day.timestamp()}),
            "not-json",
            "null",
        ]
        hist_path.write_text("\n".join(lines) + "\n")

        page = HistoryDialog.__new__(HistoryDialog)
        page._hist_path = str(hist_path)
        page._groups = {}
        row = MagicMock()
        row.get_index.return_value = 0

        HistoryDialog._rm_entry_from_hist(page, row, path, day_key)

        remaining = [line for line in hist_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(remaining), 3)
        parsed = [json.loads(line) for line in remaining if line not in ("not-json", "null")]
        self.assertEqual(len(parsed), 1)
        self.assertAlmostEqual(parsed[0]["time"], previous_day.timestamp())
        self.assertIn("not-json", remaining)
        self.assertIn("null", remaining)

    def test_history_populate_skips_malformed_entries_and_keeps_title_plain(self):
        from src import history

        hist_path = Path(self.temp_dir.name) / "history.jsonl"
        lines = [
            "null",
            "[1, 2]",
            json.dumps({"path": "/videos/overflow.mkv", "time": 10 ** 30}),
            json.dumps({"path": "/videos/A & B.mkv", "time": 1700000000}),
        ]
        hist_path.write_text("\n".join(lines) + "\n")

        page = history.HistoryDialog.__new__(history.HistoryDialog)
        page._hist_path = str(hist_path)
        page._groups = {}
        page._load_all_added = False
        page.spinner = MagicMock()
        page.clear_btn = MagicMock()
        page.placeholder_img = MagicMock()
        page.history_prefs_page = MagicMock()
        page._load_all_group = MagicMock()
        page._load_btn = MagicMock()

        created_rows = []

        def make_row(**kwargs):
            created_rows.append(kwargs)
            return MagicMock()

        with patch.object(history.Adw, "PreferencesGroup", MagicMock()), \
             patch.object(history.Adw, "ActionRow", side_effect=make_row), \
             patch.object(history.Gtk, "Button", MagicMock()), \
             patch.object(history.Gtk, "GestureClick", MagicMock()), \
             patch.object(history, "idle_add_once",
                          lambda fn, *a, **k: fn(*a, **k)):
            history.HistoryDialog._populate_history(page)

        self.assertEqual(len(created_rows), 1, "malformed history lines must be skipped")
        self.assertEqual(created_rows[0]["title"], "A & B")
        self.assertNotIn("&amp;", created_rows[0]["title"])
        # libadwaita parses row titles as markup by default and drops titles
        # containing a bare "&", so history must explicitly disable markup.
        self.assertFalse(created_rows[0]["use_markup"])


    # ------------------------------------------------------------------
    # window.py: failed refresh must not make a stale link look fresh
    # ------------------------------------------------------------------
    def test_continue_watching_failed_refresh_does_not_mark_stream_fresh(self):
        from src.window import CineWindow

        win = CineWindow.__new__(CineWindow)
        win.show_player_loading = MagicMock()
        win._finish_continue_watching_play = MagicMock()

        stale_stream = {
            "url": "https://old.example/stale.mkv",
            "addon_name": "prov",
            "addon_names": ["prov"],
            "is_http": True,
            "saved_at": 1.0,
        }
        item = {"id": "tt9", "type": "movie", "title": "Stale", "provider": "prov"}

        refresh_done = threading.Event()
        idle_attempted = threading.Event()

        def fake_refresh(*args, **kwargs):
            refresh_done.set()
            return stale_stream, []

        def fake_idle_add(callback, *args):
            try:
                callback(*args)
            finally:
                idle_attempted.set()
            return 0

        with patch("src.database.get_working_stream", return_value=stale_stream), \
             patch("src.database.is_stream_stale", return_value=True), \
             patch("src.database.save_working_stream") as save_working, \
             patch("src.database.save_continue_watching") as save_cw, \
             patch("src.api.refresh_stream", side_effect=fake_refresh), \
             patch("src.window.GLib.idle_add", side_effect=fake_idle_add):
            CineWindow._on_continue_watching_clicked(win, item)
            self.assertTrue(refresh_done.wait(2))
            self.assertTrue(idle_attempted.wait(2))

        save_working.assert_not_called()
        save_cw.assert_not_called()
        win._finish_continue_watching_play.assert_called_once()

    def test_cw_background_refresh_claims_running_flag_before_thread_start(self):
        from src.window import CineWindow

        win = CineWindow.__new__(CineWindow)
        win._last_cw_bg_refresh_time = 0

        with patch("src.window.threading.Thread") as thread_cls:
            CineWindow._start_background_continue_watching_refresh(win, [])
            CineWindow._start_background_continue_watching_refresh(win, [])

        self.assertEqual(thread_cls.call_count, 1)
        self.assertTrue(win._cw_bg_refresh_running)

    def test_stream_link_changed_helper(self):
        from src.window import _stream_link_changed

        self.assertTrue(_stream_link_changed({"url": "https://new"}, {"url": "https://old"}))
        self.assertFalse(_stream_link_changed({"url": "https://old"}, {"url": "https://old"}))
        self.assertFalse(_stream_link_changed(None, {"url": "https://old"}))

    # ------------------------------------------------------------------
    # player.py: background launch must not clobber a registered engine
    # ------------------------------------------------------------------
    def test_background_launch_does_not_replace_registered_engine(self):
        from src import player

        info_hash = "e" * 40
        foreground = MagicMock()
        foreground.is_alive.return_value = True
        bg_engine = MagicMock()

        def fake_start():
            with player._engines_lock:
                player._engines[info_hash] = foreground

        bg_engine.start.side_effect = fake_start

        with patch.object(player, "TorrentStreamEngine", return_value=bg_engine), \
             patch.object(player.threading, "Thread", self._InlineThread), \
             patch("src.libtorrent_stream.info_hash_from_magnet", return_value=info_hash), \
             patch.object(player.database, "set_download_paused"):
            player.download_magnet_background("magnet:?xt=urn:btih:" + info_hash)

        self.assertIs(player._engines.get(info_hash), foreground)
        bg_engine.stop.assert_called()

    def test_background_launch_aborts_when_explicitly_stopped_mid_start(self):
        from src import player

        info_hash = "a" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True

        def fake_start():
            # The user pauses or deletes the download while it is starting.
            player.stop_engine_explicit(info_hash)

        engine.start.side_effect = fake_start

        with patch.object(player, "TorrentStreamEngine", return_value=engine), \
             patch.object(player.threading, "Thread", self._InlineThread), \
             patch("src.libtorrent_stream.info_hash_from_magnet", return_value=info_hash), \
             patch.object(player.database, "set_download_paused") as mock_paused:
            player.download_magnet_background("magnet:?xt=urn:btih:" + info_hash)

        self.assertNotIn(info_hash, player._engines)
        self.assertNotIn(info_hash, player._launching_engines)
        engine.stop.assert_called()
        mock_paused.assert_any_call(info_hash, True)
        self.assertFalse(
            any(c.args == (info_hash, False) for c in mock_paused.call_args_list),
            "cancelled launch unpaused the download again",
        )

    def test_play_launch_aborts_when_explicitly_stopped_mid_start(self):
        from src import player

        # _run_play_magnet_with_inline_threads() stubs the magnet parser to
        # report this hash, so the engine and the stop must use it too.
        info_hash = "d" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True

        def fake_start():
            player.stop_engine_explicit(info_hash)

        engine.start.side_effect = fake_start

        with patch.object(player, "TorrentStreamEngine", return_value=engine), \
             patch.object(player.database, "set_download_paused") as mock_paused:
            self._run_play_magnet_with_inline_threads(
                "magnet:?xt=urn:btih:" + info_hash,
                lambda stats: None,
            )
        self._drain_main_loop()

        self.assertNotIn(info_hash, player._engines)
        self.assertIsNone(player._streaming_hash)
        engine.stop.assert_called()
        self.assertFalse(
            any(c.args == (info_hash, False) for c in mock_paused.call_args_list),
            "cancelled playback launch unpaused the download again",
        )

    def test_background_auto_pause_skips_engine_reused_for_playback(self):
        from src import player

        info_hash = "c" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True
        engine.stats.return_value = {"progress": 1.0, "ratio": 2.0}

        def fake_sleep(_seconds):
            # Playback starts reusing the background engine while the launcher
            # waits for fast-resume data.
            with player._engines_lock:
                player._streaming_hash = info_hash

        with patch.object(player, "TorrentStreamEngine", return_value=engine), \
             patch.object(player.threading, "Thread", self._InlineThread), \
             patch("src.libtorrent_stream.info_hash_from_magnet", return_value=info_hash), \
             patch.object(player.time, "sleep", side_effect=fake_sleep), \
             patch.object(player.database, "set_download_paused") as mock_paused:
            player.download_magnet_background("magnet:?xt=urn:btih:" + info_hash)

        self.assertIs(player._engines.get(info_hash), engine)
        engine.stop.assert_not_called()
        mock_paused.assert_any_call(info_hash, False)
        self.assertFalse(
            any(c.args == (info_hash, True) for c in mock_paused.call_args_list),
            "auto-pause stopped an engine that playback had reused",
        )

    def test_details_stop_does_not_deadlock_on_engine_lock(self):
        from src import player
        from src.window import MovieDetailsPage

        info_hash = "f" * 40
        engine = MagicMock()
        engine.is_alive.return_value = True
        engine.item_id = "tt123"

        page = MovieDetailsPage.__new__(MovieDetailsPage)
        page.window = SimpleNamespace(mpv=MagicMock())
        page.movie_stub = {"id": "tt123"}

        with player._engines_lock:
            player._engines[info_hash] = engine

        done = threading.Event()

        def run():
            try:
                MovieDetailsPage.on_stop_clicked(page)
            finally:
                done.set()

        with patch.object(player.database, "set_download_paused"):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(
                done.wait(3),
                "on_stop_clicked deadlocked while holding _engines_lock",
            )

        self.assertNotIn(info_hash, player._engines)
        engine.stop.assert_called()

    # ------------------------------------------------------------------
    # person_page.py: stale detail responses must not overwrite a reload
    # ------------------------------------------------------------------
    def test_person_page_discards_stale_details_response(self):
        from src import person_page

        page = person_page.PersonPage.__new__(person_page.PersonPage)
        page.person_id = "42"
        page._destroyed = False
        page._fetch_gen = 2
        page._populate_ui = MagicMock()

        with patch.object(person_page, "fetch_person_details", return_value={"name": "X"}), \
             patch.object(person_page.GLib, "idle_add") as mock_idle:
            person_page.PersonPage._fetch_details_thread(page, 1)

        mock_idle.assert_not_called()
        page._populate_ui.assert_not_called()

    def test_person_page_applies_current_details_response(self):
        from src import person_page

        page = person_page.PersonPage.__new__(person_page.PersonPage)
        page.person_id = "42"
        page._destroyed = False
        page._fetch_gen = 3
        page._populate_ui = MagicMock()

        with patch.object(person_page, "fetch_person_details", return_value={"name": "X"}), \
             patch.object(person_page.GLib, "idle_add") as mock_idle:
            person_page.PersonPage._fetch_details_thread(page, 3)

        mock_idle.assert_called_once_with(page._populate_ui, {"name": "X"}, 3)

    def test_person_page_reload_starts_new_fetch_generation(self):
        from src import person_page

        page = person_page.PersonPage.__new__(person_page.PersonPage)
        page._fetch_gen = 5

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kwargs):
                self.args = args

            def start(self):
                pass

        with patch.object(person_page.threading, "Thread", FakeThread):
            person_page.PersonPage._start_fetch(page)

        self.assertEqual(page._fetch_gen, 6)
        self.assertEqual(page._fetch_thread.args, (6,))

    # ------------------------------------------------------------------
    # subtitle_search_dialog.py: unknown languages must not match a filter
    # ------------------------------------------------------------------
    def test_subtitle_filter_excludes_unknown_language(self):
        from src import subtitle_search_dialog as ssd

        page = ssd.SubtitleSearchDialog.__new__(ssd.SubtitleSearchDialog)
        page._all_subtitles = [
            {"lang": "", "release_name": "unknown"},
            {"lang": "eng", "release_name": "english"},
            {"lang": "spa", "release_name": "spanish"},
        ]
        page.lang_dropdown = MagicMock()
        page.lang_dropdown.get_selected.return_value = 1  # English
        page.list_box = MagicMock()
        page.stack = MagicMock()
        page.lbl_count = MagicMock()
        page._create_row = MagicMock(return_value=MagicMock())

        ssd.SubtitleSearchDialog._apply_filter(page)

        page._create_row.assert_called_once_with(page._all_subtitles[1])
        self.assertEqual(page.list_box.append.call_count, 1)
        page.stack.set_visible_child_name.assert_called_with("results")

    # ------------------------------------------------------------------
    # window.py: a superseded continue-watching refresh must not play
    # ------------------------------------------------------------------
    def test_continue_watching_slow_refresh_cannot_hijack_newer_click(self):
        from src.window import CineWindow

        win = CineWindow.__new__(CineWindow)
        win.show_player_loading = MagicMock()
        win._finish_continue_watching_play = MagicMock()

        refresh_started = threading.Event()
        release_refresh = threading.Event()
        refresh_done = threading.Event()
        idle_attempted = threading.Event()

        def fake_refresh(*args, **kwargs):
            refresh_started.set()
            release_refresh.wait(5)
            refresh_done.set()
            return ({"url": "https://fresh.example/a.mkv",
                     "addon_name": "prov", "is_http": True}, [])

        def fake_idle_add(callback, *args):
            try:
                callback(*args)
            finally:
                idle_attempted.set()
            return 0

        item_a = {
            "id": "tt1", "type": "movie", "title": "A", "provider": "prov",
        }
        item_b = {"id": "tt2", "type": "movie", "title": "B"}

        with patch("src.database.get_working_stream", return_value=None), \
             patch("src.database.is_stream_stale",
                   side_effect=lambda s: (s or {}).get("id") == "tt1"), \
             patch("src.database.save_working_stream"), \
             patch("src.database.save_continue_watching"), \
             patch("src.api.refresh_stream", side_effect=fake_refresh), \
             patch("src.window.GLib.idle_add", side_effect=fake_idle_add):
            CineWindow._on_continue_watching_clicked(win, item_a)
            self.assertTrue(refresh_started.wait(2))

            CineWindow._on_continue_watching_clicked(win, item_b)
            self.assertEqual(win._finish_continue_watching_play.call_count, 1)
            self.assertIs(win._finish_continue_watching_play.call_args[0][0], item_b)

            release_refresh.set()
            self.assertTrue(refresh_done.wait(2))
            self.assertTrue(idle_attempted.wait(2))

        win._finish_continue_watching_play.assert_called_once()
        self.assertIs(win._finish_continue_watching_play.call_args[0][0], item_b)

    # ------------------------------------------------------------------
    # movie_widget.py: concurrent cards must share one image download
    # ------------------------------------------------------------------
    def test_concurrent_image_loads_download_once(self):
        from src import movie_widget

        unique_url = "https://img.example.test/" + os.urandom(8).hex() + ".png"
        cache_file = os.path.join(
            movie_widget.IMAGE_CACHE_DIR,
            hashlib.md5(unique_url.encode()).hexdigest(),
        )
        self.addCleanup(lambda: os.path.exists(cache_file) and os.remove(cache_file))

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return png

        calls = []
        entered = threading.Event()
        release = threading.Event()
        applied = []
        applied_event = threading.Event()

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(req.full_url)
            entered.set()
            release.wait(3)
            return FakeResponse()

        def fake_apply(widget, pixbuf, requested_url=None):
            applied.append(widget)
            if len(applied) >= 2:
                applied_event.set()

        def immediate_idle(callback, *args, **kwargs):
            callback(*args, **kwargs)
            return 0

        with patch("src.movie_widget.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("src.movie_widget._apply_pixbuf", side_effect=fake_apply), \
             patch("src.movie_widget.GLib.idle_add", side_effect=immediate_idle):
            movie_widget.load_image_into_picture(unique_url, MagicMock(), width=1, height=1)
            movie_widget.load_image_into_picture(unique_url, MagicMock(), width=1, height=1)

            self.assertTrue(entered.wait(2))
            time.sleep(0.2)
            release.set()
            self.assertTrue(applied_event.wait(5))

        self.assertEqual(calls, [unique_url])
        self.assertEqual(len(applied), 2)

    # ------------------------------------------------------------------
    # api.py / window.py: addon status checks must not bypass the cache
    # ------------------------------------------------------------------
    def test_check_addon_manifest_online_uses_cached_manifest(self):
        from src import api

        self.assertTrue(api.check_addon_manifest_online("builtin:something"))

        with patch.object(api, "_get_cached_request", return_value={"id": "org.test.addon"}) as req:
            self.assertTrue(
                api.check_addon_manifest_online("https://addon.example/manifest.json")
            )
            req.assert_called_once()
            self.assertEqual(req.call_args.kwargs.get("max_age_hours"), 24)

        with patch.object(api, "_get_cached_request", return_value=None):
            self.assertFalse(
                api.check_addon_manifest_online("https://addon.example/manifest.json")
            )

    # ------------------------------------------------------------------
    # mpris.py: idle player must not advertise "Playing"
    # ------------------------------------------------------------------
    def test_mpris_reports_stopped_while_idle(self):
        from src import mpris

        mpv_state = SimpleNamespace(idle_active=True, pause=False)
        app = SimpleNamespace(
            props=SimpleNamespace(
                active_window=SimpleNamespace(mpv=mpv_state)
            )
        )
        player = mpris.MPRIS.__new__(mpris.MPRIS)
        player._app = app

        self.assertEqual(player._playback_status(), "Stopped")
        mpv_state.idle_active = False
        self.assertEqual(player._playback_status(), "Playing")
        mpv_state.pause = True
        self.assertEqual(player._playback_status(), "Paused")


if __name__ == "__main__":
    unittest.main()
