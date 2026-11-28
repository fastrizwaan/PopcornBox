import gzip
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from src import api, hls_proxy, http_downloader


class TestStreamUrlSchemeBoundary(unittest.TestCase):
    def test_remote_file_and_non_http_urls_are_rejected(self):
        streams = api.process_raw_streams([
            {"url": "file:///etc/passwd.m3u8", "stream_type": "hls"},
            {"url": "../../private/movie.mp4", "stream_type": "direct"},
            {"url": "data:text/plain,secret", "stream_type": "direct"},
            {"url": "https:///missing-host/movie.mp4", "stream_type": "direct"},
            {"url": "https://[invalid/movie.mp4", "stream_type": "direct"},
        ])
        self.assertEqual(streams, [])

    def test_http_and_magnet_stream_urls_are_retained(self):
        streams = api.process_raw_streams([
            {"url": "https://cdn.example/movie.m3u8", "stream_type": "hls"},
            {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"},
        ])
        self.assertEqual(len(streams), 2)

    def test_subtitle_download_refuses_local_schemes(self):
        with patch("urllib.request.urlopen") as urlopen:
            for url in ("file:///etc/passwd", "data:text/plain,x", "ftp://host/sub.srt", "/etc/passwd"):
                self.assertIsNone(api.download_subtitle(url, "x"))
            urlopen.assert_not_called()

    def _subtitle_response(self, body, encoding=""):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.side_effect = lambda n=-1: body if n is None or n < 0 else body[:n]
        resp.headers = {"Content-Encoding": encoding}
        return resp

    def test_subtitle_download_rejects_gzip_bomb_and_keeps_normal_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(api.database, "CONFIG_DIR", tmp):
            bomb = gzip.compress(b"\0" * (api._SUBTITLE_MAX_DECODED_BYTES + 1024))
            with patch("urllib.request.urlopen", return_value=self._subtitle_response(bomb, "gzip")):
                self.assertIsNone(api.download_subtitle("https://subs.example/bomb.srt", "bomb"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "subtitles", "bomb.srt")))

            body = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
            with patch("urllib.request.urlopen", return_value=self._subtitle_response(gzip.compress(body), "gzip")):
                path = api.download_subtitle("https://subs.example/ok.srt", "ok")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), body)

    def test_subtitle_download_rejects_oversized_body(self):
        big = b"a" * (api._SUBTITLE_MAX_DOWNLOAD_BYTES + 10)
        with tempfile.TemporaryDirectory() as tmp, patch.object(api.database, "CONFIG_DIR", tmp):
            with patch("urllib.request.urlopen", return_value=self._subtitle_response(big)):
                self.assertIsNone(api.download_subtitle("https://subs.example/big.srt", "big"))

    def test_http_download_refuses_local_schemes(self):
        with patch.object(http_downloader.database, "add_download") as add_download, \
                patch.object(http_downloader, "_start_worker_thread") as start:
            for url in ("file:///etc/shadow", "/home/user/.ssh/id_rsa", "ftp://h/x.mkv"):
                self.assertIsNone(http_downloader.start_http_download(url, clean_title="x"))
            self.assertFalse(http_downloader.resume_http_download(
                {"info_hash": "h", "url": "file:///etc/shadow", "file_path": "/tmp/x"}))
            add_download.assert_not_called()
            start.assert_not_called()

    def test_hls_manifest_uris_cannot_open_local_files(self):
        for url in ("file:///etc/passwd", "data:,x", "ftp://h/seg.ts", "http:///nohost"):
            with self.assertRaises(urllib.error.URLError):
                hls_proxy._remote_request(url)
        self.assertEqual(hls_proxy._remote_request("https://cdn.example/a.ts").full_url,
                         "https://cdn.example/a.ts")

        session = hls_proxy.HlsSession("s", "https://cdn.example/master.m3u8")
        master = "\n".join([
            "#EXTM3U",
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="Local",LANGUAGE="en",URI="file:///etc/passwd"',
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="Remote",LANGUAGE="en",URI="subs/en.vtt"',
        ])
        subs = session._extract_subtitles(master, "https://cdn.example/master.m3u8", {})
        self.assertEqual([s["title"] for s in subs], ["Remote"])
        self.assertTrue(subs[0]["url"].startswith("https://cdn.example/"))

    def test_capped_decompression(self):
        import zlib
        small = gzip.compress(b"x" * 1000)
        self.assertEqual(api._decompress_capped(small, 16 + zlib.MAX_WBITS, 1000), b"x" * 1000)
        with self.assertRaises(ValueError):
            api._decompress_capped(small, 16 + zlib.MAX_WBITS, 999)
        raw = zlib.compress(b"y" * 5000)
        with self.assertRaises(ValueError):
            api._decompress_capped(raw, zlib.MAX_WBITS, 4096)


if __name__ == "__main__":
    unittest.main()
