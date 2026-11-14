import socket
import unittest
from unittest.mock import patch, MagicMock
import json

from src import doh


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


if __name__ == "__main__":
    unittest.main()
