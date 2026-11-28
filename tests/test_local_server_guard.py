import http.client
import unittest

from src import hls_proxy, seek_proxy
from src.local_server import is_loopback_host_header, new_session_id


def _get(port, path, host):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        resp = conn.getresponse()
        headers = dict(resp.getheaders())
        resp.read()
        return resp.status, headers
    finally:
        conn.close()


class TestLoopbackHostHeader(unittest.TestCase):
    def test_loopback_names_are_accepted(self):
        for host in ("127.0.0.1:8080", "localhost:8080", "[::1]:8080", "127.0.0.1", "LOCALHOST", "::1", None, ""):
            self.assertTrue(is_loopback_host_header(host, 8080), host)

    def test_rebinding_and_wrong_port_are_rejected(self):
        for host in ("evil.example:8080", "evil.example", "127.0.0.1.evil.example:8080",
                     "127.0.0.1:9999", "[::1]x", "[::1", "localhost:abc"):
            self.assertFalse(is_loopback_host_header(host, 8080), host)

    def test_session_ids_are_unguessable_tokens(self):
        ids = {new_session_id() for _ in range(64)}
        self.assertEqual(len(ids), 64)
        self.assertTrue(all(len(i) == 32 for i in ids))


class TestLoopbackServersRejectForeignHost(unittest.TestCase):
    def test_hls_proxy(self):
        port = hls_proxy.start_proxy_server()
        status, _ = _get(port, "/hls/nope/playlist.m3u8", "attacker.example")
        self.assertEqual(status, 403)
        status, headers = _get(port, "/hls/nope/playlist.m3u8", f"127.0.0.1:{port}")
        self.assertEqual(status, 404)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_seek_proxy(self):
        port = seek_proxy._ensure_server()
        status, _ = _get(port, "/seek/nope/file.mkv", f"rebind.example:{port}")
        self.assertEqual(status, 403)
        status, _ = _get(port, "/seek/nope/file.mkv", f"localhost:{port}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
