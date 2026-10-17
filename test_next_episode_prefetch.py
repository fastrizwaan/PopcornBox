import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from gi.repository import Gio

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database
from src.window import CineWindow


VIDEOS = [
    {"id": "tt1234567:1:1", "season": 1, "episode": 1, "title": "Pilot"},
    {"id": "tt1234567:1:2", "season": 1, "episode": 2, "title": "Second"},
    {"id": "tt1234567:2:1", "season": 2, "episode": 1, "title": "Premiere"},
]


class FakeBox:
    def __init__(self):
        self.children = []

    def get_first_child(self):
        return self.children[0] if self.children else None

    def append(self, child):
        self.children.append(child)


class FakeStack:
    def __init__(self, initial="player"):
        self.visible_child_name = initial

    def get_visible_child_name(self):
        return self.visible_child_name

    def set_visible_child_name(self, name):
        self.visible_child_name = name


class FakeDropdown:
    def __init__(self, selected=0):
        self.selected = selected

    def get_selected(self):
        return self.selected

    def set_selected(self, idx):
        self.selected = idx


class FakeSeriesPage:
    """Minimal stand-in for MovieDetailsPage while an episode is playing."""

    def __init__(self, season=1, episode=1, media_type="series"):
        self.is_channel = False
        self.media_type = media_type
        self.selected_season = season
        self.selected_episode = episode
        self.selected_video = None
        self.selected_torrent = None
        self.remembered_working_stream = None
        self._auto_play_next = False
        self.movie_details = {"imdb_id": "tt1234567", "title": "Test Show"}
        self.movie_stub = {"id": "tt1234567", "title": "Test Show"}
        self.current_episodes = [{"episode": 1}, {"episode": 2}]
        self.season_dropdown = FakeDropdown()
        self.episode_dropdown = FakeDropdown()


class TestNextEpisodePrefetch(unittest.TestCase):
    def setUp(self):
        self.win = CineWindow.__new__(CineWindow)
        self.win.details_box = FakeBox()
        self.win.main_stack = FakeStack("player")
        self.win.mpv = MagicMock()
        self.win.mpv.duration = 600.0
        self.win.mpv.time_pos = 500.0
        self.win.mpv.media_title = "Test Show"
        self.win.mpv.path = "/tmp/stream"
        self.win.prev_prog_time = None
        self.win.show_remaining = False
        self.win.video_progress_adj = MagicMock()
        self.win.time_elapsed_label = MagicMock()
        self.win._current_playing_item = None
        self.win._show_toast = MagicMock()
        self.win.show_player_loading = MagicMock()
        self.win.play_stream_with_failover = MagicMock()
        self.win._update_continue_watching_section = MagicMock()
        self.win.stream_queue = []
        self.win.stream_queue_index = 0
        self.win.preferred_direct_provider = None
        self.win.user_selected_quality = None
        self.win._get_videos_for_current_page = lambda page: list(VIDEOS)

        self._db_patches = [
            patch.object(database, "set_setting"),
            patch.object(database, "save_continue_watching"),
            patch.object(database, "get_continue_watching_item", return_value=None),
            patch.object(database, "save_working_stream"),
            patch.object(database, "save_series_pack_torrent"),
        ]
        for patcher in self._db_patches:
            patcher.start()
        self.addCleanup(self._stop_db_patches)

    def _stop_db_patches(self):
        for patcher in self._db_patches:
            patcher.stop()

    def _start_playing(self, season=1, episode=1):
        page = FakeSeriesPage(season=season, episode=episode)
        self.win.details_box = FakeBox()
        self.win.details_box.append(page)
        self.win._current_playing_item = {
            "id": "tt1234567",
            "imdb_id": "tt1234567",
            "title": "Test Show",
            "type": "series",
            "season": season,
            "episode": episode,
            "stream_url": "https://cdn.example/e1.mp4",
        }
        return page

    def _join_prefetch_threads(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            workers = [t for t in threading.enumerate() if t.name == "next-episode-prefetch"]
            if not workers:
                return
            for worker in workers:
                worker.join(timeout=max(0.01, deadline - time.time()))

    def _direct_card(self, url="https://cdn.example/e2.mp4", **extra):
        card = {
            "url": url,
            "is_http": True,
            "quality": "1080p",
            "stream_title": "Test Show S01E02 1080p",
            "addon_names": ["ProviderA"],
            "saved_at": time.time(),
        }
        card.update(extra)
        return card

    def test_prefetch_skipped_when_auto_play_next_disabled(self):
        self._start_playing()
        with patch("src.window.settings.get_boolean", return_value=False), \
             patch("src.api.get_torrents_streamed") as mock_fetch:
            self.assertFalse(self.win._prefetch_next_episode_streams())
        mock_fetch.assert_not_called()
        self.assertIsNone(getattr(self.win, "_next_ep_prefetched", None))

    def test_prefetch_fetches_direct_streams_for_next_episode(self):
        self._start_playing(season=1, episode=1)
        direct = self._direct_card()
        torrent = {"hash": "abc123", "infoHash": "abc123", "quality": "1080p", "seeders": 12}
        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed", return_value=[direct, torrent]) as mock_fetch:
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()

        mock_fetch.assert_called_once()
        self.assertEqual(mock_fetch.call_args.args[0], "tt1234567")
        self.assertEqual(mock_fetch.call_args.kwargs.get("season"), 1)
        self.assertEqual(mock_fetch.call_args.kwargs.get("episode"), 2)

        staged = self.win._take_prefetched_next_episode_streams(1, 2)
        self.assertIsNotNone(staged)
        self.assertEqual([s["url"] for s in staged], ["https://cdn.example/e2.mp4"])
        self.assertTrue(all(s.get("is_http") for s in staged))

    def test_prefetch_rolls_over_to_next_season(self):
        self._start_playing(season=1, episode=2)
        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed", return_value=[]) as mock_fetch:
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()
        self.assertEqual(mock_fetch.call_args.kwargs.get("season"), 2)
        self.assertEqual(mock_fetch.call_args.kwargs.get("episode"), 1)

    def test_prefetch_not_repeated_while_current_episode_is_warm(self):
        self._start_playing(season=1, episode=1)
        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed", return_value=[self._direct_card()]) as mock_fetch:
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self.assertFalse(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()
        self.assertEqual(mock_fetch.call_count, 1)

    def test_prefetch_refreshes_when_the_episode_is_about_to_end(self):
        self._start_playing(season=1, episode=1)
        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed", return_value=[self._direct_card()]):
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()
            # Simulate the near-end refresh window: the staged pre-fetch is old
            # enough that the direct links should be resolved again.
            self.win._next_ep_prefetch_at = time.time() - (self.win._NEXT_EPISODE_PREFETCH_MIN_INTERVAL + 5)
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()

    def test_cancelled_prefetch_does_not_stage_results(self):
        self._start_playing(season=1, episode=1)

        def cancel_then_return(*args, **kwargs):
            self.win._cancel_next_episode_prefetch()
            return [self._direct_card()]

        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed", side_effect=cancel_then_return):
            self.assertTrue(self.win._prefetch_next_episode_streams())
            self._join_prefetch_threads()
        self.assertIsNone(self.win._next_ep_prefetched)

    def test_cancel_next_episode_prefetch_sets_abort_and_clears_stage(self):
        abort = threading.Event()
        self.win._next_ep_prefetch_abort = abort
        self.win._next_ep_prefetched = {"key": "tt1234567:S1:E2", "season": 1, "episode": 2, "streams": [self._direct_card()]}
        self.win._cancel_next_episode_prefetch()
        self.assertTrue(abort.is_set())
        self.assertIsNone(self.win._next_ep_prefetched)
        self.assertIsNone(self.win._next_ep_prefetch_abort)

    def test_next_episode_starts_from_prefetched_direct_stream(self):
        page = self._start_playing(season=1, episode=1)
        direct = self._direct_card()
        self.win._next_ep_prefetched = {
            "key": "tt1234567:S1:E2",
            "season": 1,
            "episode": 2,
            "streams": [direct],
        }
        with patch("src.window.settings.get_boolean", return_value=True):
            self.assertTrue(self.win._try_play_next_episode())

        self.win.play_stream_with_failover.assert_called_once()
        queue = self.win.play_stream_with_failover.call_args.args[0]
        self.assertEqual(queue[0]["url"], "https://cdn.example/e2.mp4")
        kwargs = self.win.play_stream_with_failover.call_args.kwargs
        self.assertEqual(kwargs.get("season"), 1)
        self.assertEqual(kwargs.get("episode"), 2)
        self.assertEqual(page.selected_torrent["url"], "https://cdn.example/e2.mp4")
        self.assertFalse(page._auto_play_next)
        # A direct link is not a torrent, so it must not be remembered as a pack.
        database.save_series_pack_torrent.assert_not_called()
        database.save_working_stream.assert_not_called()

    def test_stale_prefetched_direct_stream_is_ignored(self):
        page = self._start_playing(season=1, episode=1)
        stale = self._direct_card(saved_at=time.time() - 6 * 3600)
        self.win._next_ep_prefetched = {
            "key": "tt1234567:S1:E2",
            "season": 1,
            "episode": 2,
            "streams": [stale],
        }
        with patch("src.window.settings.get_boolean", return_value=True):
            self.assertTrue(self.win._try_play_next_episode())

        self.win.play_stream_with_failover.assert_not_called()
        self.assertTrue(page._auto_play_next)
        self.assertIsNone(self.win._next_ep_prefetched)

    def test_prefetch_ignored_for_non_episodic_media(self):
        page = self._start_playing(season=1, episode=1)
        page.media_type = "movie"
        with patch("src.window.settings.get_boolean", return_value=True), \
             patch("src.api.get_torrents_streamed") as mock_fetch:
            self.assertFalse(self.win._prefetch_next_episode_streams())
        mock_fetch.assert_not_called()

    def test_progress_tick_prefetches_within_lead_window_only(self):
        self._start_playing(season=1, episode=1)
        self.win._prefetch_next_episode_streams = MagicMock()
        with patch("src.window.settings.get_boolean", return_value=True):
            self.win._update_progress(500.0)  # 100s remaining -> inside the lead window
        self.win._prefetch_next_episode_streams.assert_called_once()

        self.win._prefetch_next_episode_streams.reset_mock()
        with patch("src.window.settings.get_boolean", return_value=True):
            self.win._update_progress(100.0)  # 500s remaining -> too early
        self.win._prefetch_next_episode_streams.assert_not_called()

    def test_progress_tick_skips_prefetch_when_prompt_dismissed(self):
        self._start_playing(season=1, episode=1)
        self.win.next_ep_dismissed = True
        self.win._prefetch_next_episode_streams = MagicMock()
        with patch("src.window.settings.get_boolean", return_value=True):
            self.win._update_progress(500.0)
        self.win._prefetch_next_episode_streams.assert_not_called()

    def test_play_stream_triggers_next_episode_prefetch(self):
        win = MagicMock()
        win._is_playing_trailer.return_value = False
        CineWindow._play_stream(win, url="https://cdn.example/e1.mp4", title="Test Show")
        win._prefetch_next_episode_streams.assert_called_once()

    def test_rank_direct_streams_prioritizes_preferred_provider(self):
        self._start_playing(season=1, episode=1)
        self.win.preferred_direct_provider = "ProviderB"
        stream_a = self._direct_card(url="https://cdn.example/a.mp4", addon_names=["ProviderA"], quality="1080p")
        stream_b = self._direct_card(url="https://cdn.example/b.mp4", addon_names=["ProviderB"], quality="1080p")
        ranked = self.win._rank_direct_streams_for_playback([stream_a, stream_b])
        self.assertEqual(ranked[0]["url"], "https://cdn.example/b.mp4")

    def test_rank_direct_streams_falls_back_to_current_playing_item_provider(self):
        self._start_playing(season=1, episode=1)
        self.win.preferred_direct_provider = None
        self.win._current_playing_item["provider"] = "ProviderB"
        stream_a = self._direct_card(url="https://cdn.example/a.mp4", addon_names=["ProviderA"])
        stream_b = self._direct_card(url="https://cdn.example/b.mp4", addon_names=["ProviderB"])
        ranked = self.win._rank_direct_streams_for_playback([stream_a, stream_b])
        self.assertEqual(ranked[0]["url"], "https://cdn.example/b.mp4")

    def test_try_play_next_episode_carries_over_provider(self):
        page = self._start_playing(season=1, episode=1)
        direct_a = self._direct_card(url="https://cdn.example/e2_a.mp4", addon_names=["ProviderA"])
        direct_b = self._direct_card(url="https://cdn.example/e2_b.mp4", addon_names=["ProviderB"])
        self.win.preferred_direct_provider = "ProviderB"
        self.win._next_ep_prefetched = {
            "key": "tt1234567:S1:E2",
            "season": 1,
            "episode": 2,
            "streams": [direct_b, direct_a],
        }
        with patch("src.window.settings.get_boolean", return_value=True):
            self.assertTrue(self.win._try_play_next_episode())
        self.assertEqual(self.win.preferred_direct_provider, "ProviderB")
        self.assertEqual(getattr(page, "preferred_direct_provider", None), "ProviderB")


if __name__ == "__main__":
    unittest.main()
