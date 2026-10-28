import unittest
import urllib.request
import urllib.parse
from src import hls_proxy

SAMPLE_FMP4_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.000000,
playlist_000.png
#EXTINF:4.000000,
playlist_001.jpeg
#EXTINF:4.000000,
playlist_002.jpg
#EXTINF:4.000000,
playlist_003.png
#EXTINF:4.000000,
playlist_004.jpeg
#EXT-X-ENDLIST
"""

SAMPLE_MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=2628000,RESOLUTION=1280x720,NAME="720p"
https://cdn.example.com/720p/manifest.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5692000,RESOLUTION=1920x1080,NAME="1080p"
https://cdn.example.com/1080p/manifest.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480,NAME="480p"
https://cdn.example.com/480p/manifest.m3u8
"""

class TestHlsProxy(unittest.TestCase):
    def test_is_hls_stream(self):
        self.assertTrue(hls_proxy.is_hls_stream("https://example.com/stream.m3u8"))
        self.assertTrue(hls_proxy.is_hls_stream("https://example.com/hls/manifest.m3u8?token=123"))
        self.assertTrue(hls_proxy.is_hls_stream("https://example.com/hls/video.ts"))
        self.assertFalse(hls_proxy.is_hls_stream("https://example.com/movie.mp4"))
        self.assertFalse(hls_proxy.is_hls_stream("magnet:?xt=urn:btih:123"))
        self.assertFalse(hls_proxy.is_hls_stream(None))

    def test_parse_and_slice_fmp4_session(self):
        session = hls_proxy.HlsSession("test_sess", "https://cdn.example.com/stream/manifest.m3u8")
        
        # Manually parse sample lines
        lines = SAMPLE_FMP4_PLAYLIST.splitlines()
        headers = []
        segments = []
        cum_times = []
        cur_time = 0.0
        i = 0
        in_header = True

        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if line.startswith("#EXT-X-MAP:"):
                session.is_fmp4 = True
                full_map = urllib.parse.urljoin(session.url, "init.mp4")
                line = f'#EXT-X-MAP:URI="{full_map}"'
                headers.append(line)
                i += 1
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                session.target_duration = 4.0
                headers.append(line)
                i += 1
            elif line.startswith("#EXTINF:"):
                in_header = False
                inf_line = line
                dur = 4.0
                i += 1
                if i < len(lines):
                    seg_url = lines[i].strip()
                    full_seg_url = urllib.parse.urljoin(session.url, seg_url)
                    segments.append((dur, inf_line, full_seg_url))
                    cum_times.append(cur_time)
                    cur_time += dur
                    i += 1
            elif in_header:
                if not line.startswith("#EXT-X-ENDLIST"):
                    headers.append(line)
                i += 1
            else:
                i += 1

        session.header_lines = headers
        session.segments = segments
        session.cumulative_times = cum_times
        session.total_duration = cur_time
        session.is_ready = True

        self.assertTrue(session.is_fmp4)
        self.assertEqual(len(session.segments), 5)
        self.assertEqual(session.total_duration, 20.0)

        # Slice at offset 0
        body_0, off_0 = session.slice_manifest(0.0)
        text_0 = body_0.decode("utf-8")
        self.assertEqual(off_0, 0.0)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", text_0)
        self.assertIn("playlist_000.png", text_0)
        self.assertIn("playlist_004.jpeg", text_0)

        # Slice at offset 8.0s (segment 2)
        body_8, off_8 = session.slice_manifest(8.0)
        text_8 = body_8.decode("utf-8")
        self.assertEqual(off_8, 8.0)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:2", text_8)
        self.assertNotIn("playlist_000.png", text_8)
        self.assertNotIn("playlist_001.jpeg", text_8)
        self.assertIn("playlist_002.jpg", text_8)
        self.assertIn("playlist_004.jpeg", text_8)

    def test_extract_best_variant_url(self):
        session = hls_proxy.HlsSession("test_var", "https://cdn.example.com/master.m3u8")
        best = session._extract_best_variant_url(SAMPLE_MASTER_PLAYLIST, "https://cdn.example.com/master.m3u8")
        self.assertEqual(best, "https://cdn.example.com/1080p/manifest.m3u8")

    def test_proxy_server_http_request(self):
        port = hls_proxy.start_proxy_server()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)

        session = hls_proxy.HlsSession("test_http", "https://cdn.example.com/manifest.m3u8")
        session.header_lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4", '#EXT-X-MAP:URI="https://cdn.example.com/init.mp4"']
        session.segments = [
            (4.0, "#EXTINF:4.0,", "https://cdn.example.com/seg0.mp4"),
            (4.0, "#EXTINF:4.0,", "https://cdn.example.com/seg1.mp4"),
        ]
        session.cumulative_times = [0.0, 4.0]
        session.total_duration = 8.0
        session.is_fmp4 = True
        session.is_ready = True
        hls_proxy._sessions["test_http"] = session

        url = hls_proxy.get_sliced_url("test_http", offset=4.0)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/vnd.apple.mpegurl")
            self.assertEqual(resp.headers.get("X-HLS-Total-Duration"), "8.0")
            body = resp.read().decode("utf-8")
            self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", body)
            self.assertNotIn("seg0.mp4", body)
            self.assertIn("seg1.mp4", body)

        hls_proxy.close_session("test_http")
        self.assertNotIn("test_http", hls_proxy._sessions)

if __name__ == "__main__":
    unittest.main()
