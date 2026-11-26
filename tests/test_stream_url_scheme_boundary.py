import unittest

from src import api


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


if __name__ == "__main__":
    unittest.main()
