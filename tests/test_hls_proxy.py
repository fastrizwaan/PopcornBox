import unittest
import io
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
    def test_playlist_reader_rejects_oversized_body(self):
        class OversizedResponse:
            def __init__(self):
                self.remaining = hls_proxy.MAX_PLAYLIST_BYTES + 1

            def read(self, count):
                amount = min(count, self.remaining)
                self.remaining -= amount
                return b"x" * amount

        with self.assertRaises(ValueError):
            hls_proxy._read_playlist_text(OversizedResponse())

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

    def test_master_playlist_with_separate_audio_bypasses_proxy(self):
        from unittest.mock import patch, MagicMock
        master_with_audio = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,URI="audio-en/playlist.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6577000,AUDIO="audio"
1080p/playlist.m3u8
"""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = io.BytesIO(master_with_audio.encode("utf-8")).read
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_resp):
            session = hls_proxy.HlsSession("test_audio", "https://cdn.example.com/master.m3u8")
            session.fetch_and_parse()
            self.assertFalse(session.is_fmp4)
            self.assertTrue(session.is_ready)

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

    def test_extract_subtitles_from_master(self):
        from unittest.mock import patch, MagicMock
        master_with_subs = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English  [CC]",DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,LANGUAGE="en-us.[CC]",URI="subs/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Spanish",DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,LANGUAGE="es",URI="https://cdn.example.com/subs/es.vtt"
#EXT-X-STREAM-INF:BANDWIDTH=4998400,RESOLUTION=1920x1080,SUBTITLES="subs"
1080p.m3u8
"""
        sub_m3u8_content = """#EXTM3U
#EXT-X-TARGETDURATION:99999
#EXTINF:99999.0,
en-us.[CC].vtt
#EXT-X-ENDLIST
"""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = io.BytesIO(sub_m3u8_content.encode("utf-8")).read
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_resp):
            session = hls_proxy.HlsSession("test_subs", "https://cdn.example.com/hls/master.m3u8")
            subs = session._extract_subtitles(master_with_subs, "https://cdn.example.com/hls/master.m3u8", {})
            self.assertEqual(len(subs), 2)
            self.assertEqual(subs[0]["title"], "English  [CC]")
            self.assertEqual(subs[0]["lang"], "en-us")
            self.assertEqual(subs[0]["url"], "https://cdn.example.com/hls/subs/en-us.%5BCC%5D.vtt")
            self.assertEqual(subs[1]["title"], "Spanish")
            self.assertEqual(subs[1]["lang"], "es")
            self.assertEqual(subs[1]["url"], "https://cdn.example.com/subs/es.vtt")

    def test_strip_subtitles_from_master(self):
        master_with_both = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English  [CC]",URI="subs/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4998400,RESOLUTION=1920x1080,AUDIO="audio",SUBTITLES="subs"
1080p.m3u8
"""
        session = hls_proxy.HlsSession("test_strip", "https://cdn.example.com/hls/master.m3u8")
        sanitized = session._strip_subtitles_from_master(master_with_both, "https://cdn.example.com/hls/master.m3u8")
        self.assertNotIn("TYPE=SUBTITLES", sanitized)
        self.assertNotIn('SUBTITLES="subs"', sanitized)
        self.assertIn('TYPE=AUDIO', sanitized)
        self.assertIn('URI="https://cdn.example.com/hls/audio/en.m3u8"', sanitized)
        self.assertIn('https://cdn.example.com/hls/1080p.m3u8', sanitized)

    def test_master_playlist_with_subtitles_and_audio_uses_master_proxy(self):
        from unittest.mock import patch, MagicMock
        master_with_both = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="https://cdn.example.com/sub.vtt"
#EXT-X-STREAM-INF:BANDWIDTH=4998400,RESOLUTION=1920x1080,AUDIO="audio",SUBTITLES="subs"
1080p.m3u8
"""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = io.BytesIO(master_with_both.encode("utf-8")).read
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_resp):
            session = hls_proxy.HlsSession("test_master_proxy", "https://cdn.example.com/hls/master.m3u8")
            session.fetch_and_parse()
            self.assertTrue(session.is_master_proxy)
            self.assertEqual(len(session.extracted_subtitles), 1)
            self.assertNotIn("TYPE=SUBTITLES", session.master_sanitized_text)

        # Test serving the sanitized master proxy
        hls_proxy._sessions["test_master_proxy"] = session
        master_proxy_url = hls_proxy.get_master_proxy_url("test_master_proxy")
        req = urllib.request.Request(master_proxy_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/vnd.apple.mpegurl")
            body = resp.read().decode("utf-8")
            self.assertNotIn("TYPE=SUBTITLES", body)
            self.assertIn("TYPE=AUDIO", body)
        hls_proxy.close_session("test_master_proxy")


# SPS payloads (after the NAL header byte) captured from a real provider whose
# master playlist labels a 640x360 decoy as 1920x1080.
SPS_1080 = bytes.fromhex("0028acca501e0089f9701100000303e90000bb80e440001312d00002faf0a4c30078c18cb0")
SPS_360 = bytes.fromhex("001eacd940a02ff97016a02020280000030008000003019078b16cb0")
SPS_720 = bytes.fromhex("001facca5014016ec04400000fa40002ee038980006acfc0003567e930c01e30632c")


def _make_ts(sps: bytes, prefix: bytes = b"") -> bytes:
    """Build two MPEG-TS packets whose video PES carries the given H.264 SPS."""
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05\x21\x00\x01\x00\x01"
    es = b"\x00\x00\x00\x01\x09\xf0" + b"\x00\x00\x00\x01\x67" + sps + b"\x00\x00\x00\x01\x68\xeb\xe3\xcb"
    payload = (pes + es).ljust(184, b"\xff")
    first = b"\x47\x41\x00\x10" + payload
    second = b"\x47\x01\x00\x11" + b"\xff" * 184
    return prefix + first + second


DECOY_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",LANGUAGE="eng",NAME="English",DEFAULT=YES,URI="track_english.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,AUDIO="aac",RESOLUTION=1920x1080
tiles.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="aac",DEFAULT=YES,RESOLUTION=1280x720
video_720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,AUDIO="aac",RESOLUTION=854x480
video_480p.m3u8
"""


class TestHlsVariantCorrection(unittest.TestCase):
    def test_sps_height(self):
        self.assertEqual(hls_proxy._h264_sps_height(SPS_1080), 1080)
        self.assertEqual(hls_proxy._h264_sps_height(SPS_360), 360)
        self.assertEqual(hls_proxy._h264_sps_height(SPS_720), 720)

    def test_mpegts_video_height(self):
        self.assertEqual(hls_proxy._mpegts_video_height(_make_ts(SPS_1080)), 1080)
        # Disguised segment with a fake PNG header in front of the TS packets
        self.assertEqual(hls_proxy._mpegts_video_height(_make_ts(SPS_360, b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)), 360)
        self.assertEqual(hls_proxy._mpegts_video_height(b"not a transport stream" * 20), 0)

    def test_rewrite_reranks_decoy(self):
        variants = hls_proxy._parse_master_variants(DECOY_MASTER)
        text = hls_proxy._rewrite_variant_attributes(DECOY_MASTER, variants, [360, 1080, 720])
        ranked = hls_proxy._parse_master_variants(text)
        best = max(ranked, key=lambda v: v["bandwidth"])
        self.assertEqual(best["uri"], "video_720p.m3u8")
        self.assertEqual((best["width"], best["height"]), (1920, 1080))
        by_uri = {v["uri"]: v for v in ranked}
        self.assertEqual(by_uri["tiles.m3u8"]["bandwidth"], 800000)
        self.assertEqual((by_uri["tiles.m3u8"]["width"], by_uri["tiles.m3u8"]["height"]), (640, 360))

    def test_rewrite_keeps_correct_master(self):
        variants = hls_proxy._parse_master_variants(DECOY_MASTER)
        self.assertEqual(hls_proxy._rewrite_variant_attributes(DECOY_MASTER, variants, [1080, 720, 480]), DECOY_MASTER)

    def _fake_cdn(self, segments):
        from unittest.mock import MagicMock

        def urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            name = url.rsplit("/", 1)[-1].split("?", 1)[0]
            if name == "master.m3u8":
                body = DECOY_MASTER.encode()
            elif name.endswith(".m3u8") and name[:-5] in segments:
                body = f"#EXTM3U\n#EXTINF:6.0,\n{name[:-5]}/00000.png\n#EXT-X-ENDLIST\n".encode()
            elif name == "00000.png":
                body = segments[url.rsplit("/", 2)[-2]]
            else:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            resp = MagicMock()
            resp.read.side_effect = io.BytesIO(body).read
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        return urlopen

    def test_fetch_and_parse_corrects_decoy_variant(self):
        from unittest.mock import patch
        segments = {"tiles": _make_ts(SPS_360), "video_720p": _make_ts(SPS_1080), "video_480p": _make_ts(SPS_720)}
        with patch("urllib.request.urlopen", side_effect=self._fake_cdn(segments)):
            session = hls_proxy.HlsSession("decoy", "https://cdn.example.com/media/master.m3u8?api_key=k")
            session.fetch_and_parse()
        self.assertTrue(session.is_master_proxy)
        self.assertTrue(session.variants_corrected)
        best = max(hls_proxy._parse_master_variants(session.master_sanitized_text), key=lambda v: v["bandwidth"])
        self.assertEqual(best["uri"], "https://cdn.example.com/media/video_720p.m3u8?api_key=k")
        hls_proxy._sessions["decoy"] = session
        self.assertTrue(hls_proxy.session_has_corrected_variants("decoy"))
        hls_proxy.close_session("decoy")

    def test_fetch_and_parse_passes_through_when_labels_are_right(self):
        from unittest.mock import patch
        segments = {"tiles": _make_ts(SPS_1080), "video_720p": _make_ts(SPS_720), "video_480p": _make_ts(SPS_360)}
        with patch("urllib.request.urlopen", side_effect=self._fake_cdn(segments)):
            session = hls_proxy.HlsSession("honest", "https://cdn.example.com/media/master.m3u8")
            session.fetch_and_parse()
        self.assertFalse(session.is_master_proxy)
        self.assertFalse(session.variants_corrected)

    def test_split_variant_pin(self):
        url = "https://cdn.example.com/media/master.m3u8?api_key=k"
        self.assertEqual(hls_proxy.split_variant_pin(url), (url, None))
        self.assertEqual(hls_proxy.split_variant_pin(url + "#pb-variant=video_720p.m3u8"), (url, "video_720p.m3u8"))
        # Unrelated fragments are left alone
        self.assertEqual(hls_proxy.split_variant_pin(url + "#t=10"), (url + "#t=10", None))

    def test_keep_only_variant_with_same_named_variants(self):
        master = "\n".join([
            "#EXTM3U",
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="EN",DEFAULT=YES,URI="audio-en/playlist.m3u8"',
            '#EXT-X-STREAM-INF:BANDWIDTH=853000,RESOLUTION=640x360,AUDIO="audio"', "360p/playlist.m3u8",
            '#EXT-X-STREAM-INF:BANDWIDTH=8128000,RESOLUTION=1920x1080,AUDIO="audio"', "1080p/playlist.m3u8",
            '#EXT-X-STREAM-INF:BANDWIDTH=23328000,RESOLUTION=3840x2160,AUDIO="audio"', "2160p/playlist.m3u8",
        ])
        text = hls_proxy._keep_only_variant(master, "1080p/playlist.m3u8")
        self.assertIsNotNone(text)
        self.assertIn("1080p/playlist.m3u8", text)
        self.assertNotIn("360p/playlist.m3u8", text)
        self.assertNotIn("2160p/playlist.m3u8", text)
        self.assertIn("audio-en/playlist.m3u8", text)  # audio rendition kept

    def test_keep_only_variant(self):
        text = hls_proxy._keep_only_variant(DECOY_MASTER, "video_720p.m3u8")
        variants = hls_proxy._parse_master_variants(text)
        self.assertEqual([v["uri"] for v in variants], ["video_720p.m3u8"])
        self.assertIn("TYPE=AUDIO", text)
        self.assertIsNone(hls_proxy._keep_only_variant(DECOY_MASTER, "missing.m3u8"))

    def test_fetch_and_parse_with_pin_serves_single_variant(self):
        from unittest.mock import patch
        segments = {"tiles": _make_ts(SPS_360), "video_720p": _make_ts(SPS_1080), "video_480p": _make_ts(SPS_720)}
        with patch("urllib.request.urlopen", side_effect=self._fake_cdn(segments)):
            session = hls_proxy.HlsSession(
                "pinned", "https://cdn.example.com/media/master.m3u8?api_key=k#pb-variant=video_480p.m3u8"
            )
            session.fetch_and_parse()
        self.assertTrue(session.is_master_proxy)
        self.assertTrue(session.variant_pinned)
        self.assertEqual(session.url, "https://cdn.example.com/media/master.m3u8?api_key=k")
        uris = [v["uri"] for v in hls_proxy._parse_master_variants(session.master_sanitized_text)]
        self.assertEqual(uris, ["https://cdn.example.com/media/video_480p.m3u8?api_key=k"])
        hls_proxy._sessions["pinned"] = session
        self.assertTrue(hls_proxy.session_has_corrected_variants("pinned"))
        hls_proxy.close_session("pinned")

    def test_disguised_multi_audio_master_routes_every_playlist(self):
        import http.client
        from unittest.mock import patch
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 62
        segments = {
            "tiles": _make_ts(SPS_1080, png), "video_720p": _make_ts(SPS_720, png),
            "video_480p": _make_ts(SPS_360, png), "track_english": _make_ts(SPS_360, png),
        }
        with patch("urllib.request.urlopen", side_effect=self._fake_cdn(segments)):
            session = hls_proxy.HlsSession("routed", "https://cdn.example.com/media/master.m3u8")
            session.fetch_and_parse()
            self.assertTrue(session.is_master_proxy)
            self.assertEqual(session.default_audio_language, "eng")
            # 3 variants + 1 audio rendition, all pointing at the local proxy
            self.assertEqual(len(session.routed_media), 4)
            self.assertNotIn("cdn.example.com", session.master_sanitized_text)
            self.assertIn("/hls/routed/media/0.m3u8", session.master_sanitized_text)
            hls_proxy._sessions["routed"] = session
            self.assertTrue(hls_proxy.session_has_corrected_variants("routed"))
            self.assertEqual(hls_proxy.session_default_audio_language("routed"), "eng")

            port = hls_proxy.start_proxy_server()
            audio_index = next(i for i, u in session.routed_media.items() if "track_english" in u)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/hls/routed/media/{audio_index}.m3u8")
            body = conn.getresponse().read().decode()
            self.assertIn(f"/hls/routed-m{audio_index}/seg/0.ts", body)
            conn.request("GET", f"/hls/routed-m{audio_index}/seg/0.ts")
            seg = conn.getresponse().read()
            self.assertEqual(seg[:1], b"\x47")  # fake PNG header stripped
            conn.close()

        hls_proxy.close_session("routed")
        self.assertNotIn(f"routed-m{audio_index}", hls_proxy._sessions)

    def test_probe_failure_leaves_master_untouched(self):
        from unittest.mock import patch
        segments = {"tiles": _make_ts(SPS_360), "video_720p": b"garbage" * 50, "video_480p": _make_ts(SPS_720)}
        with patch("urllib.request.urlopen", side_effect=self._fake_cdn(segments)):
            session = hls_proxy.HlsSession("unknown", "https://cdn.example.com/media/master.m3u8")
            session.fetch_and_parse()
        self.assertFalse(session.variants_corrected)


if __name__ == "__main__":
    unittest.main()
