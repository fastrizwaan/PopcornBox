"""Tests for src/seek_proxy.py, the caching proxy for hosts that ignore Range."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_cache_home = tempfile.mkdtemp(prefix="popcornbox-seek-proxy-test-")
os.environ["XDG_CACHE_HOME"] = _cache_home

from src import seek_proxy  # noqa: E402

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, every offset recognisable
MKV_PAYLOAD = b"\x1a\x45\xdf\xa3" + PAYLOAD[4:] + b"\x01" * (40 * 1024 * 1024)


class _Upstream(BaseHTTPRequestHandler):
    body = PAYLOAD
    honour_range = False
    gate = None  # threading.Event; when set, the body is sent in two halves

    def do_GET(self):
        body = type(self).body
        rng = self.headers.get("Range")
        if type(self).honour_range and rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            body = body[start:]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            gate = type(self).gate
            if gate is not None:
                half = len(body) // 2
                self.wfile.write(body[:half])
                self.wfile.flush()
                gate.wait(10)
                self.wfile.write(body[half:])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/video.mkv"


def _get(url, rng=None):
    req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), resp.read()


class TestSeekProxy(unittest.TestCase):
    def setUp(self):
        self.handler = type("Upstream", (_Upstream,), {})
        self.server, self.url = _serve(self.handler)

    def tearDown(self):
        seek_proxy.close_all()
        self.server.shutdown()
        self.server.server_close()

    def test_range_supporting_host_is_not_proxied(self):
        self.handler.honour_range = True
        result, url = seek_proxy.prepare(self.url, {})
        self.assertEqual(result, seek_proxy.RESULT_NOT_NEEDED)
        self.assertIsNone(url)

    def test_range_ignoring_host_becomes_seekable(self):
        result, url = seek_proxy.prepare(self.url, {})
        self.assertEqual(result, seek_proxy.RESULT_PROXIED)
        self.assertTrue(seek_proxy.is_proxy_url(url))

        status, headers, body = _get(url, "bytes=1000-1999")
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 1000-1999/{len(PAYLOAD)}")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(body, PAYLOAD[1000:2000])

        status, _headers, body = _get(url, "bytes=-10")
        self.assertEqual((status, body), (206, PAYLOAD[-10:]))

        status, _headers, body = _get(url)
        self.assertEqual((status, body), (200, PAYLOAD))

    def test_read_past_download_waits_for_data(self):
        self.handler.gate = threading.Event()
        _result, url = seek_proxy.prepare(self.url, {})
        session = next(iter(seek_proxy._sessions.values()))
        self.assertLess(session.downloaded, len(PAYLOAD))
        threading.Timer(0.3, self.handler.gate.set).start()
        _status, _headers, body = _get(url, f"bytes={len(PAYLOAD) - 100}-")
        self.assertEqual(body, PAYLOAD[-100:])

    def test_mkv_tail_jump_gets_zeros_instead_of_waiting(self):
        self.handler.body = MKV_PAYLOAD
        self.handler.gate = threading.Event()  # never set: the tail never arrives
        try:
            _result, url = seek_proxy.prepare(self.url, {})
            session = next(iter(seek_proxy._sessions.values()))
            # wait until the first chunk (with the EBML magic) is cached
            self.assertTrue(session.wait_for(0))
            self.assertTrue(session.is_mkv)
            tail = len(MKV_PAYLOAD) - 4096
            _status, _headers, body = _get(url, f"bytes={tail}-")
            self.assertEqual(body, bytes(4096))
        finally:
            self.handler.gate.set()

    def test_close_all_removes_cache_file(self):
        _result, _url = seek_proxy.prepare(self.url, {})
        session = next(iter(seek_proxy._sessions.values()))
        self.assertTrue(os.path.exists(session.path))
        seek_proxy.close_all()
        self.assertFalse(os.path.exists(session.path))
        self.assertEqual(seek_proxy._sessions, {})

    def test_unknown_size_is_not_proxied(self):
        class NoLength(_Upstream):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 100)
        server, url = _serve(NoLength)
        try:
            self.assertEqual(seek_proxy.prepare(url, {})[0], seek_proxy.RESULT_UNSUPPORTED)
        finally:
            server.shutdown()
            server.server_close()

    def test_empty_header_values_are_not_sent(self):
        seen = {}

        class Recorder(_Upstream):
            def do_GET(self):
                seen.update(self.headers)
                super().do_GET()
        server, url = _serve(Recorder)
        try:
            seek_proxy.prepare(url, {"User-Agent": "", "Referer": "", "X-Token": "abc"})
            self.assertNotIn("Referer", seen)
            self.assertEqual(seen.get("X-Token"), "abc")
            self.assertNotIn("Mozilla", seen.get("User-Agent", ""))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
