import unittest
import sys
import tempfile
import os
import subprocess
import io
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
from gi.repository import Gtk, Gio, GLib

for gres_path in [
    Path("/app/share/popcorn-box/popcornbox.gresource"),
    Path("build-dir/files/share/popcorn-box/popcornbox.gresource"),
]:
    if gres_path.exists():
        try:
            res = Gio.Resource.load(str(gres_path))
            Gio.resources_register(res)
            break
        except Exception:
            pass

from src import window, database, http_downloader
from src.download_row import DownloadItemRow

class TestStreamDownload(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_base_dir = database.BASE_DIR
        database.BASE_DIR = Path(self.tmpdir) / "popcorn-box"
        os.makedirs(database.BASE_DIR, exist_ok=True)
        # Clear downloads DB in tmpdir
        database.clear_downloads()

    def tearDown(self):
        import shutil
        database.BASE_DIR = self.orig_base_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_flatpak_sandboxed_no_xdg_download(self):
        """Verify that --filesystem=xdg-download is NOT present in finish-args for complete sandboxing."""
        import json
        manifest_path = "build-aux/flatpak/io.github.fastrizwaan.PopcornBox.json"
        self.assertTrue(os.path.exists(manifest_path), "Manifest file not found")
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
        finish_args = manifest.get("finish-args", [])
        self.assertNotIn("--filesystem=xdg-download", finish_args,
                         "Downloads must be sandboxed inside Flatpak data area; --filesystem=xdg-download should not be in finish-args")

    def test_sandboxed_download_directory(self):
        """Verify that downloads directory is strictly inside PopcornBox sandboxed data dir."""
        dl_dir = http_downloader.get_downloads_dir()
        self.assertTrue(os.path.isdir(dl_dir))
        self.assertEqual(dl_dir, str(database.BASE_DIR / "downloads"))

    @patch("src.window.MovieDetailsPage.__init__", return_value=None)
    def test_download_torrent_routes_to_background_downloader(self, mock_init):
        page = window.MovieDetailsPage()
        page.movie_stub = {"id": "tt1234567", "title": "Test Show", "season": 1, "episode": 2}
        page.media_type = "series"
        page.selected_season = 1
        page.selected_episode = 2
        page.window = MagicMock()

        torrent = {
            "magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=TestShow",
            "file_index": 0,
            "stream_title": "Test Torrent 1080p",
            "is_http": False,
        }

        with patch("src.player.download_magnet_background") as mock_dl_bg, \
             patch("src.database.add_download") as mock_add_dl:
            page._download_stream_item(torrent)
            mock_add_dl.assert_called_once()
            args = mock_add_dl.call_args[0]
            self.assertEqual(args[0], "0123456789abcdef0123456789abcdef01234567")
            self.assertIn("Test Show S01E02", args[1])
            mock_dl_bg.assert_called_once()
            page.window._show_toast.assert_called_once()

    @patch("src.window.MovieDetailsPage.__init__", return_value=None)
    def test_download_http_stream_registers_in_db_and_sandboxed_path(self, mock_init):
        page = window.MovieDetailsPage()
        page.movie_stub = {"id": "tt13187966", "title": "Love Ka Panga", "season": 1, "episode": 1}
        page.media_type = "series"
        page.selected_season = 1
        page.selected_episode = 1
        page.window = MagicMock()

        torrent = {
            "url": "https://video-downloads.googleusercontent.com/test",
            "filename": "4KHDHub Download [Server : 10Gbps] 4KHDHub Download [Server : 10Gbps]",
            "is_http": True,
        }

        with patch("threading.Thread") as mock_thread:
            page._download_stream_item(torrent)
            page.window._show_toast.assert_called_once()
            toast_msg = page.window._show_toast.call_args[0][0]
            self.assertIn("Love Ka Panga S01E01", toast_msg)
            self.assertNotIn("10Gbps", toast_msg)

            # Check download was added to database with is_http=True
            downloads = database.get_downloads()
            self.assertEqual(len(downloads), 1)
            dl = downloads[0]
            self.assertTrue(dl.get("is_http"))
            self.assertEqual(dl.get("name"), "Love Ka Panga S01E01")
            self.assertTrue(dl.get("file_path").startswith(str(database.BASE_DIR / "downloads")))

    @patch("src.window.MovieDetailsPage.__init__", return_value=None)
    def test_download_movie_http_stream_naming(self, mock_init):
        page = window.MovieDetailsPage()
        page.movie_stub = {"id": "tt9999999", "title": "Inception"}
        page.media_type = "movie"
        page.selected_season = None
        page.selected_episode = None
        page.window = MagicMock()

        torrent = {
            "url": "https://cdn.example.com/stream.mkv",
            "is_http": True,
        }

        with patch("threading.Thread") as mock_thread:
            page._download_stream_item(torrent)
            page.window._show_toast.assert_called_once()
            toast_msg = page.window._show_toast.call_args[0][0]
            self.assertIn("Inception.mkv", toast_msg)
            self.assertNotIn("S0", toast_msg)

    @patch("src.window.MovieDetailsPage.__init__", return_value=None)
    def test_download_http_worker_writes_file_successfully(self, mock_init):
        page = window.MovieDetailsPage()
        page.movie_stub = {"id": "tt13187966", "title": "Love Ka Panga", "season": 1, "episode": 1}
        page.media_type = "series"
        page.selected_season = 1
        page.selected_episode = 1
        page.window = MagicMock()

        torrent = {
            "url": "https://cdn.example.com/video.mp4",
            "is_http": True,
        }

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "video/mp4", "Content-Length": "1600"}
        mock_resp.geturl.return_value = "https://cdn.example.com/video.mp4"
        mock_resp.read.side_effect = [b"mock video data " * 100, b""]
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("src.http_downloader.GLib.idle_add") as mock_idle_add:
            page._download_stream_item(torrent)
            import time
            time.sleep(0.15)

            expected_file = os.path.join(http_downloader.get_downloads_dir(), "Love Ka Panga S01E01.mp4")
            self.assertTrue(os.path.exists(expected_file), f"Expected file {expected_file} was not written")
            with open(expected_file, "rb") as f:
                content = f.read()
            self.assertEqual(content, b"mock video data " * 100)

            # Check that success toast callback was posted
            self.assertTrue(mock_idle_add.called)

    def test_populate_downloads_listbox_sections(self):
        """Verify that _populate_downloads_listbox creates separate sections for HTTP Downloads and Torrents."""
        items = [
            {
                "info_hash": "http_12345",
                "name": "Direct Download Item",
                "is_http": True,
                "url": "https://example.com/movie.mp4",
                "file_path": "/path/to/movie.mp4",
            },
            {
                "info_hash": "0123456789abcdef0123456789abcdef01234567",
                "name": "Torrent Item",
                "is_http": False,
                "magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            }
        ]

        listbox = Gtk.ListBox()
        mock_win = MagicMock()
        window.PopcornBoxWindow._populate_downloads_listbox(mock_win, listbox, items)

        # Children in listbox:
        # 1. Section Header: HTTP Downloads
        # 2. Row: Direct Download Item
        # 3. Section Header: Torrents
        # 4. Row: Torrent Item
        children = []
        child = listbox.get_first_child()
        while child:
            children.append(child)
            child = child.get_next_sibling()

        self.assertEqual(len(children), 4)

    def test_download_row_folder_click_uses_xdg_open(self):
        """Verify clicking Open Folder executes xdg-open on sandboxed directory."""
        test_file = os.path.join(http_downloader.get_downloads_dir(), "test_video.mp4")
        with open(test_file, "w") as f:
            f.write("content")

        dl = {
            "info_hash": "http_folder_test",
            "name": "Folder Test",
            "is_http": True,
            "url": "https://example.com/test.mp4",
            "file_path": test_file,
        }

        row = DownloadItemRow(dl)
        with patch("subprocess.Popen") as mock_popen:
            row.on_folder_clicked(None)
            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], "xdg-open")
            self.assertEqual(cmd[1], http_downloader.get_downloads_dir())

    def test_download_row_watch_click_plays_local_file(self):
        """Verify clicking Play Video File on HTTP download plays local file."""
        test_file = os.path.join(http_downloader.get_downloads_dir(), "test_play.mp4")
        with open(test_file, "w") as f:
            f.write("video data")

        dl = {
            "info_hash": "http_play_test",
            "name": "Play Test",
            "is_http": True,
            "url": "https://example.com/play.mp4",
            "file_path": test_file,
        }

        row = DownloadItemRow(dl)
        mock_root = MagicMock()
        row.get_root = MagicMock(return_value=mock_root)

        with patch("src.download_row.idle_add_once") as mock_idle_add_once:
            row.on_watch_clicked(None)
            mock_idle_add_once.assert_called_once()
            args = mock_idle_add_once.call_args[0]
            self.assertEqual(args[0], mock_root._play_stream)
            self.assertEqual(args[1], test_file)
            self.assertEqual(args[2], "Play Test")

    def test_download_row_delete_removes_file_and_db(self):
        """Verify clicking Delete removes the downloaded file and cleans up DB entry."""
        test_file = os.path.join(http_downloader.get_downloads_dir(), "test_del.mp4")
        with open(test_file, "w") as f:
            f.write("delete me")

        database.add_download(
            info_hash="http_del_test",
            name="Delete Test",
            is_http=True,
            file_path=test_file,
        )

        dl = {
            "info_hash": "http_del_test",
            "name": "Delete Test",
            "is_http": True,
            "file_path": test_file,
        }

        row = DownloadItemRow(dl)
        self.assertTrue(os.path.exists(test_file))

        row.on_delete_clicked(None)
        self.assertFalse(os.path.exists(test_file))
        downloads = database.get_downloads()
        self.assertNotIn("http_del_test", [d.get("info_hash") for d in downloads])


if __name__ == "__main__":
    unittest.main()
