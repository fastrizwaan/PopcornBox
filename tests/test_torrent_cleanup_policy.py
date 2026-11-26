import unittest
from unittest.mock import patch, MagicMock
import tempfile
import os
import shutil
import threading
import time

from src import player
from src import database

class TestTorrentCleanupPolicy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_download_base = player.DOWNLOAD_BASE
        player.DOWNLOAD_BASE = self.temp_dir

        # Reset player state
        with player._engines_lock:
            player._engines.clear()
            player._launching_engines.clear()
            player._streaming_hash = None

    def tearDown(self):
        player.DOWNLOAD_BASE = self.orig_download_base
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        with player._engines_lock:
            player._engines.clear()
            player._launching_engines.clear()
            player._streaming_hash = None

    @patch("src.player.threading.Thread")
    def test_background_download_only_launches_one_engine_per_hash(self, mock_thread):
        """Rapid duplicate resume requests must share one in-flight engine start."""
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        with patch("src.libtorrent_stream.info_hash_from_magnet", return_value="0123456789abcdef0123456789abcdef01234567"):
            player.download_magnet_background(magnet)
            player.download_magnet_background(magnet)

        mock_thread.assert_called_once()
        with player._engines_lock:
            self.assertIn("0123456789abcdef0123456789abcdef01234567", player._launching_engines)

    def test_metadata_worker_wakes_waiters_after_failure(self):
        from src.libtorrent_stream import TorrentStreamEngine

        engine = object.__new__(TorrentStreamEngine)
        engine.stopped = threading.Event()
        engine.ready_event = threading.Event()
        engine.error = None
        engine._status = MagicMock(side_effect=RuntimeError("metadata status failed"))

        engine._metadata_worker()

        self.assertIsInstance(engine.error, RuntimeError)
        self.assertTrue(engine.ready_event.is_set())

    def test_resume_save_is_serialized(self):
        from src.libtorrent_stream import TorrentStreamEngine

        engine = object.__new__(TorrentStreamEngine)
        engine.handle = MagicMock()
        engine.lt = MagicMock()
        engine._resume_lock = threading.Lock()

        active = 0
        peak = 0
        guard = threading.Lock()

        def fake_save(_timeout):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1

        engine._save_resume_data_locked = fake_save

        threads = [
            threading.Thread(target=engine._save_resume_data, args=(0.05,))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The monitor and stop() can save at the same time; overlapping saves
        # steal each other's libtorrent alerts and corrupt the temp file.
        self.assertEqual(peak, 1)

    @patch("src.database.remove_download")
    @patch("src.database.set_download_finished")
    def test_completed_standalone_torrent_not_deleted_on_back(self, mock_set_finished, mock_remove_download):
        """A completed torrent must NOT be deleted or removed from list when clicking back."""
        info_hash = "abc123completedhash"
        torrent_dir = os.path.join(self.temp_dir, info_hash)
        os.makedirs(torrent_dir, exist_ok=True)
        movie_file = os.path.join(torrent_dir, "Movie.mkv")
        with open(movie_file, "wb") as f:
            f.write(b"0" * 1024)

        mock_engine = MagicMock()
        mock_engine.is_alive.return_value = True
        mock_engine.is_download_only = False
        mock_engine.season = None
        mock_engine.media_type = "movie"
        mock_engine.stats.return_value = {
            "progress": 1.0,
            "filePath": "Movie.mkv",
            "totalLength": 1024,
            "downloaded": 1024
        }
        mock_engine.target = {"index": 0, "path": "Movie.mkv", "size": 1024}
        mock_engine._target_downloaded.return_value = 1024
        mock_engine._files.return_value = [{"index": 0, "path": "Movie.mkv", "size": 1024}]

        with player._engines_lock:
            player._engines[info_hash] = mock_engine
            player._streaming_hash = info_hash

        player.stop_player(remove_torrent=True)

        mock_remove_download.assert_not_called()
        mock_set_finished.assert_called_with(info_hash, True)
        self.assertTrue(os.path.exists(torrent_dir))
        self.assertTrue(os.path.exists(movie_file))

    @patch("src.database.remove_download")
    def test_incomplete_standalone_torrent_is_deleted_on_back(self, mock_remove_download):
        """An incomplete separate torrent (e.g. abandoned movie preview) IS deleted on clicking back."""
        info_hash = "abc123incompletemovie"
        torrent_dir = os.path.join(self.temp_dir, info_hash)
        os.makedirs(torrent_dir, exist_ok=True)

        mock_engine = MagicMock()
        mock_engine.is_alive.return_value = True
        mock_engine.is_download_only = False
        mock_engine.season = None
        mock_engine.media_type = "movie"
        mock_engine.stats.return_value = {
            "progress": 0.05,
            "filePath": "Movie.mkv",
            "totalLength": 100000,
            "downloaded": 5000
        }
        mock_engine.target = {"index": 0, "path": "Movie.mkv", "size": 100000}
        mock_engine._target_downloaded.return_value = 5000
        mock_engine._files.return_value = [{"index": 0, "path": "Movie.mkv", "size": 100000}]

        with player._engines_lock:
            player._engines[info_hash] = mock_engine
            player._streaming_hash = info_hash

        player.stop_player(remove_torrent=True)

        mock_remove_download.assert_called_once_with(info_hash)

    @patch("src.database.remove_download")
    def test_season_pack_with_downloaded_episode_not_deleted_when_next_episode_playing(self, mock_remove_download):
        """In a season pack, episode 1 already downloaded and episode 2 playing must NOT delete episode 1."""
        info_hash = "seasonpackhash12345"
        torrent_dir = os.path.join(self.temp_dir, info_hash)
        os.makedirs(torrent_dir, exist_ok=True)
        ep1_file = os.path.join(torrent_dir, "Show_S01E01.mkv")
        with open(ep1_file, "wb") as f:
            f.write(b"0" * 2048)

        mock_engine = MagicMock()
        mock_engine.is_alive.return_value = True
        mock_engine.is_download_only = False
        mock_engine.season = 1
        mock_engine.episode = 2
        mock_engine.media_type = "series"
        # Episode 2 is currently streaming and incomplete (10%)
        mock_engine.target = {"index": 1, "path": "Show_S01E02.mkv", "size": 2048}
        mock_engine._target_downloaded.return_value = 200
        mock_engine.stats.return_value = {
            "progress": 0.10,
            "filePath": "Show_S01E02.mkv",
            "totalLength": 2048,
            "downloaded": 200
        }
        # Multi-file season pack with Episode 1 and Episode 2
        mock_engine._files.return_value = [
            {"index": 0, "path": "Show_S01E01.mkv", "size": 2048},
            {"index": 1, "path": "Show_S01E02.mkv", "size": 2048}
        ]
        # Episode 1 is 100% completed in file_progress
        mock_engine.handle.file_progress.return_value = [2048, 200]

        with player._engines_lock:
            player._engines[info_hash] = mock_engine
            player._streaming_hash = info_hash

        # User clicks back while Episode 2 is playing
        player.stop_player(remove_torrent=True, is_series=True, season=1)

        # Neither the torrent nor Episode 1 must be deleted!
        mock_remove_download.assert_not_called()
        self.assertTrue(os.path.exists(torrent_dir))
        self.assertTrue(os.path.exists(ep1_file))

    @patch("src.database.remove_download")
    def test_separate_series_episode_not_deleted_on_back(self, mock_remove_download):
        """A series episode that is part of a season must NOT be deleted on back."""
        info_hash = "seriesseparateep2hash"
        torrent_dir = os.path.join(self.temp_dir, info_hash)
        os.makedirs(torrent_dir, exist_ok=True)

        mock_engine = MagicMock()
        mock_engine.is_alive.return_value = True
        mock_engine.is_download_only = False
        mock_engine.season = 1
        mock_engine.episode = 2
        mock_engine.media_type = "series"
        mock_engine.target = {"index": 0, "path": "Ep2.mkv", "size": 5000}
        mock_engine._target_downloaded.return_value = 500
        mock_engine.stats.return_value = {
            "progress": 0.1,
            "filePath": "Ep2.mkv",
            "totalLength": 5000,
            "downloaded": 500
        }
        mock_engine._files.return_value = [{"index": 0, "path": "Ep2.mkv", "size": 5000}]

        with player._engines_lock:
            player._engines[info_hash] = mock_engine
            player._streaming_hash = info_hash

        player.stop_player(remove_torrent=True, is_series=True, season=1)

        mock_remove_download.assert_not_called()

    @patch("src.database.remove_download")
    def test_explicit_download_only_not_deleted(self, mock_remove_download):
        """Torrents queued from Downloads page (is_download_only=True) are never deleted on back."""
        info_hash = "explicitdownloadonlyhash"
        mock_engine = MagicMock()
        mock_engine.is_alive.return_value = True
        mock_engine.is_download_only = True
        mock_engine.stats.return_value = {"progress": 0.4}

        with player._engines_lock:
            player._engines[info_hash] = mock_engine
            player._streaming_hash = info_hash

        player.stop_player(remove_torrent=True)

        mock_remove_download.assert_not_called()

if __name__ == "__main__":
    unittest.main()
