import socket
import unittest
from unittest.mock import patch, MagicMock
import json

from src import doh
from src import api


class TestDoH(unittest.TestCase):
    def setUp(self):
        doh.clear_doh_cache()

    def tearDown(self):
        doh.clear_doh_cache()

    def test_is_ip_literal(self):
        self.assertTrue(doh._is_ip_literal("127.0.0.1"))
        self.assertTrue(doh._is_ip_literal("8.8.8.8"))
        self.assertTrue(doh._is_ip_literal("::1"))
        self.assertTrue(doh._is_ip_literal("2606:4700:4700::1111"))
        self.assertFalse(doh._is_ip_literal("example.com"))
        self.assertFalse(doh._is_ip_literal("the9kmovies.com"))
        self.assertFalse(doh._is_ip_literal(""))

    def test_should_bypass_doh(self):
        self.assertTrue(doh._should_bypass_doh("localhost"))
        self.assertTrue(doh._should_bypass_doh("127.0.0.1"))
        self.assertTrue(doh._should_bypass_doh("::1"))
        self.assertTrue(doh._should_bypass_doh("mydevice.local"))
        self.assertTrue(doh._should_bypass_doh("router.lan"))
        self.assertTrue(doh._should_bypass_doh("cloudflare-dns.com"))
        self.assertTrue(doh._should_bypass_doh("dns.google"))
        self.assertTrue(doh._should_bypass_doh("192.168.1.1"))
        self.assertFalse(doh._should_bypass_doh("the9kmovies.com"))
        self.assertFalse(doh._should_bypass_doh("v3-cinemeta.strem.io"))

    def test_doh_resolve_caching(self):
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = json.dumps({
            "Status": 0,
            "Answer": [
                {"name": "the9kmovies.com", "type": 1, "data": "104.21.57.231"},
                {"name": "the9kmovies.com", "type": 1, "data": "172.67.193.123"},
            ]
        }).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            ips = doh.doh_resolve("the9kmovies.com")
            self.assertEqual(ips, ["104.21.57.231", "172.67.193.123"])
            self.assertEqual(mock_urlopen.call_count, 1)

            # Second call should hit in-memory cache without urlopen
            cached_ips = doh.doh_resolve("the9kmovies.com")
            self.assertEqual(cached_ips, ["104.21.57.231", "172.67.193.123"])
            self.assertEqual(mock_urlopen.call_count, 1)

    def test_doh_resolve_fallback_to_google(self):
        def mock_urlopen_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            resp.__enter__.return_value = resp
            if "cloudflare-dns.com" in url:
                # Cloudflare fails
                raise Exception("Cloudflare DoH timeout")
            elif "dns.google" in url:
                # Google succeeds
                resp.read.return_value = json.dumps({
                    "Status": 0,
                    "Answer": [{"name": "example.org", "type": 1, "data": "93.184.216.34"}]
                }).encode("utf-8")
                return resp
            raise Exception("Unknown host")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_side_effect):
            ips = doh.doh_resolve("example.org")
            self.assertEqual(ips, ["93.184.216.34"])

    def test_doh_resolve_fallback_to_adguard(self):
        def mock_urlopen_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            resp.__enter__.return_value = resp
            if "cloudflare-dns.com" in url or "dns.google" in url:
                raise Exception("Provider failed")
            elif "dns.adguard-dns.com" in url:
                resp.read.return_value = json.dumps({
                    "Status": 0,
                    "Answer": [{"name": "example.org", "type": 1, "data": "93.184.216.34"}]
                }).encode("utf-8")
                return resp
            raise Exception("Unknown host")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_side_effect):
            ips = doh.doh_resolve("example.org")
            self.assertEqual(ips, ["93.184.216.34"])

    def test_doh_resolve_ipv6_aaaa(self):
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = json.dumps({
            "Status": 0,
            "Answer": [
                {"name": "ipv6.example.com", "type": 28, "data": "2606:2800:220:1:248:1893:25c8:1946"},
            ]
        }).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            ips = doh.doh_resolve("ipv6.example.com", record_type="AAAA")
            self.assertEqual(ips, ["2606:2800:220:1:248:1893:25c8:1946"])

    def test_getaddrinfo_hook(self):
        doh.enable_global_doh(True)
        self.assertTrue(doh.is_global_doh_enabled())

        mock_ips = ["93.184.216.34"]
        with patch.object(doh, "doh_resolve", return_value=mock_ips) as mock_res:
            addrs = socket.getaddrinfo("example.org", 80)
            self.assertTrue(len(addrs) >= 1)
            # The sockaddr tuple should have the resolved IP
            sockaddrs = [item[4] for item in addrs]
            self.assertTrue(any(sa[0] == "93.184.216.34" for sa in sockaddrs))
            mock_res.assert_called_with("example.org")

        doh.enable_global_doh(False)
        self.assertFalse(doh.is_global_doh_enabled())

    def test_bootstrap_hosts_resolution(self):
        doh.enable_global_doh(True)
        try:
            # cloudflare-dns.com should resolve to 1.1.1.1 without calling doh_resolve
            with patch.object(doh, "doh_resolve") as mock_resolve:
                addrs = socket.getaddrinfo("cloudflare-dns.com", 443)
                self.assertTrue(len(addrs) >= 1)
                mock_resolve.assert_not_called()
                sockaddrs = [item[4][0] for item in addrs]
                self.assertIn("1.1.1.1", sockaddrs)
        finally:
            doh.enable_global_doh(False)

    def test_doh_resolve_negative_cache(self):
        """Negative (no-IP) results are cached with a short TTL to avoid hammering DoH servers."""
        with patch("urllib.request.urlopen", side_effect=Exception("All providers failed")) as mock_urlopen:
            ips1 = doh.doh_resolve("unreachable-domain-xyz.invalid")
            self.assertEqual(ips1, [])
            call_count_1 = mock_urlopen.call_count

            # Second call within the negative TTL should NOT hit urlopen again
            ips2 = doh.doh_resolve("unreachable-domain-xyz.invalid")
            self.assertEqual(ips2, [])
            self.assertEqual(mock_urlopen.call_count, call_count_1,
                             "Negative result was not cached; DoH was called again unnecessarily")

    def test_doh_resolve_negative_cache_distinct_from_positive(self):
        """Positive cached results use the full 15-min TTL; negative results use the short 60-s TTL."""
        # Verify the TTL constants are distinct
        self.assertGreater(doh._DOH_CACHE_TTL, doh._DOH_NEGATIVE_CACHE_TTL)
        # Verify negative TTL is short enough to be useful but not too short
        self.assertGreaterEqual(doh._DOH_NEGATIVE_CACHE_TTL, 30)
        self.assertLessEqual(doh._DOH_NEGATIVE_CACHE_TTL, 300)

    def test_doh_proxy_url_and_env(self):
        doh.enable_global_doh(False)
        self.assertIsNone(doh.get_doh_proxy_url())
        self.assertEqual(doh.get_doh_env(), {})

        doh.enable_global_doh(True)
        try:
            url = doh.get_doh_proxy_url()
            self.assertIsNotNone(url)
            self.assertTrue(url.startswith("http://127.0.0.1:"))

            env = doh.get_doh_env()
            self.assertEqual(env.get("HTTP_PROXY"), url)
            self.assertEqual(env.get("HTTPS_PROXY"), url)
            self.assertIn("localhost", env.get("NO_PROXY", ""))
        finally:
            doh.enable_global_doh(False)
            self.assertIsNone(doh.get_doh_proxy_url())

    def test_is_dns_poisoned_with_system_nxdomain_and_doh_ok(self):
        """When system DNS returns NXDOMAIN (or gaierror), but DoH resolves the host, it is considered poisoned/blocked."""
        with patch("socket.gethostbyname", side_effect=socket.gaierror("Name or service not known")), \
             patch.object(api, "_doh_resolve", return_value=["104.21.5.1"]):
            self.assertTrue(api._is_dns_poisoned("blocked-by-isp.org"))

    def test_is_dns_poisoned_clean_tls(self):
        """When system DNS resolves and TLS handshake succeeds, host is clean."""
        with patch("socket.gethostbyname", return_value="93.184.216.34"), \
             patch("socket.create_connection") as mock_conn:
            mock_sock = MagicMock()
            mock_conn.return_value = mock_sock
            mock_ctx = MagicMock()
            mock_ssock = MagicMock()
            mock_ctx.wrap_socket.return_value = mock_ssock
            with patch.object(api, "_SSL_CONTEXT", mock_ctx):
                self.assertFalse(api._is_dns_poisoned("clean-domain.org"))
                self.assertIn("clean-domain.org", api._DOH_NOT_POISONED)

    def test_doh_fetch_post_and_decompression(self):
        """_doh_fetch sends POST data with Content-Length and decompresses gzip content."""
        import gzip
        payload = b"test response body from doh"
        gzipped = gzip.compress(payload)

        raw_resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Encoding: gzip\r\n"
            b"\r\n" + gzipped
        )

        mock_sock = MagicMock()
        mock_sock.recv.side_effect = [raw_resp, b""]

        with patch.object(api, "_doh_resolve", return_value=["1.2.3.4"]), \
             patch("socket.create_connection", return_value=mock_sock):
            result = api._doh_fetch("http://example.com/api", method="POST", data='{"query":"abc"}')
            self.assertEqual(result, payload)
            sent_bytes = b"".join(call[0][0] for call in mock_sock.sendall.call_args_list)
            self.assertIn(b"POST /api HTTP/1.1", sent_bytes)
            self.assertIn(b"Content-Length: 15", sent_bytes)


if __name__ == "__main__":
    unittest.main()
