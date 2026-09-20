import sys
import unittest
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.modules["mpv"] = MagicMock()

from src import api


class TestStreamHeaders(unittest.TestCase):
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

    def test_extract_stream_headers_proxy_headers_request(self):
        stream = {
            "url": "https://wal5as0it0.sssrr.org/sora/123/abc",
            "behaviorHints": {
                "bingeGroup": "bpx|headline",
                "filename": "Headline.720p.mp4",
                "proxyHeaders": {
                    "request": {
                        "Referer": "https://abyssplayer.com/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
                    }
                },
                "notWebReady": True
            }
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://abyssplayer.com/")
        self.assertIn("Chrome/126.0.0.0", headers.get("User-Agent"))

    def test_extract_stream_headers_direct_headers(self):
        stream = {
            "url": "https://example.com/video.mp4",
            "behaviorHints": {
                "headers": {
                    "Referer": "https://source.com/",
                    "Origin": "https://source.com"
                }
            }
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://source.com/")
        self.assertEqual(headers.get("Origin"), "https://source.com")

    def test_extract_stream_headers_query_params(self):
        stream = {
            "url": "https://example.com/video.m3u8?Referer=https%3A%2F%2Fiptv.com&User-Agent=CustomUA",
            "behaviorHints": {}
        }
        headers = api.extract_stream_headers(stream)
        self.assertEqual(headers.get("Referer"), "https://iptv.com")
        self.assertEqual(headers.get("User-Agent"), "CustomUA")

    def test_extract_stream_headers_empty_fallback(self):
        self.assertEqual(api.extract_stream_headers(None), {})
        self.assertEqual(api.extract_stream_headers({}), {})

    def test_play_stream_configures_mpv_headers(self):
        from src.window import CineWindow
        win = MagicMock()
        win.main_stack = MagicMock()
        mpv_props = {}
        win.mpv.__setitem__.side_effect = lambda k, v: mpv_props.__setitem__(k, v)
        win.mpv.__getitem__.side_effect = lambda k: mpv_props.get(k)
        win._is_playing_trailer.return_value = False

        headers = {
            "Referer": "https://abyssplayer.com/",
            "User-Agent": "Mozilla/5.0 Chrome/126",
            "Origin": "https://abyssplayer.com"
        }
        CineWindow._play_stream(
            win,
            url="https://wal5as0it0.sssrr.org/sora/123/abc",
            title="Headline",
            headers=headers
        )
        self.assertEqual(mpv_props.get("referrer"), "https://abyssplayer.com/")
        self.assertEqual(mpv_props.get("user-agent"), "Mozilla/5.0 Chrome/126")
        self.assertFalse(mpv_props.get("ytdl"))
        http_fields = mpv_props.get("http-header-fields", [])
        # Referer and User-Agent are NOT duplicated in http_fields to avoid Cloudflare 400 Bad Request
        self.assertFalse(any("Referer:" in f for f in http_fields))
        self.assertFalse(any("User-Agent:" in f for f in http_fields))
        self.assertTrue(any("Origin: https://abyssplayer.com" in f for f in http_fields))


if __name__ == "__main__":
    unittest.main()
