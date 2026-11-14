"""
Tests for explicit stream selection and failover isolation:
- Exact stream identity matching in _find_stream_index_exact
- Failover queue candidate isolation (direct streams only fail over to direct streams; torrents to torrents)
- _extend_stream_queue boundary enforcement (never mix torrents into direct stream queue)
- GDFlix extractor filtering of unplayable Googleusercontent links and support for cflare direct streams
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Bootstrap schema & mock mpv before importing window/src
sys.modules.setdefault("mpv", MagicMock())

_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    _dest = Path(_schema_dir.name) / _schema_path.name
    _dest.write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

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

from src import window, cloudstream, api


class TestStreamSelectionAndFailover(unittest.TestCase):

    def test_find_stream_index_exact_distinguishes_same_provider_streams(self):
        """Streams with same provider, title and quality but different URLs must have distinct indices."""
        stream_a = {
            "name": "Bollyflix 1080p (Cloud Resume)",
            "url": "https://workers.dev/link1",
            "stream_title": "Ramayana: Rise of a Legend",
            "quality": "1080p",
            "addon_name": "Bollyflix",
            "is_http": True,
        }
        stream_b = {
            "name": "Bollyflix 1080p (Quick Download)",
            "url": "https://cloudpaglu.site/link2",
            "stream_title": "Ramayana: Rise of a Legend",
            "quality": "1080p",
            "addon_name": "Bollyflix",
            "is_http": True,
        }
        stream_list = [stream_a, stream_b]

        self.assertEqual(window._find_stream_index_exact(stream_a, stream_list), 0)
        self.assertEqual(window._find_stream_index_exact(stream_b, stream_list), 1)

    def test_extend_stream_queue_isolation(self):
        """_extend_stream_queue must not append torrents to an active direct stream queue."""
        mock_win = MagicMock()
        mock_win.stream_queue = [
            {
                "url": "https://example.com/direct1.mp4",
                "is_http": True,
                "name": "Direct Stream 1",
            }
        ]
        mock_win.stream_queue_index = 0
        mock_win.stream_request_id = 1
        mock_win._current_playing_item = {}

        incoming = [
            {"magnet": "magnet:?xt=urn:btih:1111111111111111111111111111111111111111", "name": "Torrent 1", "seeders": 10},
            {"url": "https://example.com/direct2.mp4", "is_http": True, "name": "Direct Stream 2"},
        ]

        # Call the actual method
        window.CineWindow._extend_stream_queue(mock_win, incoming)

        # Only direct stream 2 should have been appended; Torrent 1 must be rejected
        self.assertEqual(len(mock_win.stream_queue), 2)
        self.assertEqual(mock_win.stream_queue[1]["url"], "https://example.com/direct2.mp4")
        self.assertNotIn("magnet", mock_win.stream_queue[1])

    def test_extend_stream_queue_torrent_isolation(self):
        """_extend_stream_queue must not append direct streams to an active torrent queue."""
        mock_win = MagicMock()
        mock_win.stream_queue = [
            {
                "magnet": "magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
                "name": "Torrent 1",
                "seeders": 5,
            }
        ]
        mock_win.stream_queue_index = 0
        mock_win.stream_request_id = 1
        mock_win._current_playing_item = {}

        incoming = [
            {"url": "https://example.com/direct.mp4", "is_http": True, "name": "Direct Stream"},
            {"magnet": "magnet:?xt=urn:btih:3333333333333333333333333333333333333333", "name": "Torrent 2", "seeders": 20},
        ]

        window.CineWindow._extend_stream_queue(mock_win, incoming)

        self.assertEqual(len(mock_win.stream_queue), 2)
        self.assertIn("magnet", mock_win.stream_queue[1])
        self.assertEqual(mock_win.stream_queue[1]["magnet"], "magnet:?xt=urn:btih:3333333333333333333333333333333333333333")

    def test_gdflix_filters_unplayable_googleusercontent(self):
        """GDFlix extractor must discard video-downloads.googleusercontent.com links that return 400."""
        html_page = """
        <html>
        <head><title>File</title></head>
        <body>
        Name : Ramayana Rise Of A Legend (2026) 1080p
        <a href="https://dl.example.com/short">Instant Download</a>
        </body>
        </html>
        """
        with patch("src.cloudstream._http_get_text", return_value=html_page), \
             patch("src.cloudstream._resolve_short_link", return_value="https://video-downloads.googleusercontent.com/ADGPM2mTfRZIvQQb4QHiwAb0j9fZyJg5E1NmGaMzeCXurNIDaJf3Er"):
            sources = cloudstream._extract_gdflix_sources("https://gdflix.dev/file/abc")
            self.assertEqual(len(sources), 0)

    def test_gdflix_extracts_cflare_direct_streams(self):
        """GDFlix extractor must extract Cloud Resume and Quick Download links from /cflare/ subpages."""
        main_page = """
        <html>
        <body>
        Name : Ramayana Rise Of A Legend (2026) 1080p
        <a href="/cflare/123/file">FAST CLOUD / ZIPDISK</a>
        </body>
        </html>
        """
        cf_page = """
        <html>
        <body>
        <a href="https://cloud-dl.workers.dev/stream123">Cloud Resume Download</a>
        <a href="https://quick.cloudpaglu.site/stream456">Quick Download (Ready)</a>
        </body>
        </html>
        """
        def fake_http_get(url, **kwargs):
            if "/cflare/" in url:
                return cf_page
            return main_page

        with patch("src.cloudstream._http_get_text", side_effect=fake_http_get):
            sources = cloudstream._extract_gdflix_sources("https://gdflix.dev/file/abc")
            self.assertEqual(len(sources), 2)
            self.assertTrue(any("Cloud Resume" in s["name"] for s in sources))
            self.assertTrue(any("Quick Download" in s["name"] for s in sources))
            self.assertTrue(any("workers.dev" in s["url"] for s in sources))


if __name__ == "__main__":
    unittest.main()
