import unittest
import time
import json
import threading
import concurrent.futures
from unittest.mock import patch, MagicMock
import urllib.error
import ssl

from src import api
from src import database
from src import stremio_sync

class TestProviderProtection(unittest.TestCase):
    def setUp(self):
        api.clear_provider_cooldown()
        with api._STREAM_URL_CACHE_LOCK:
            api._STREAM_URL_CACHE.clear()

    def tearDown(self):
        api.clear_provider_cooldown()

    def test_api_tls_context_verifies_certificates_and_hostnames(self):
        context = api._create_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(stremio_sync._SSL_CONTEXT.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(stremio_sync._SSL_CONTEXT.check_hostname)

    def test_cached_request_coalesces_concurrent_misses(self):
        barrier = threading.Barrier(3)
        results = []
        calls = []

        def fetch_once(url, **kwargs):
            calls.append(url)
            time.sleep(0.1)
            return {"ok": True}

        def request():
            barrier.wait()
            results.append(api._get_cached_request("https://single-flight.test/resource"))

        with patch.object(api, "_get_cached_request_uncollapsed", side_effect=fetch_once):
            threads = [threading.Thread(target=request) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(calls, ["https://single-flight.test/resource"])
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_provider_host_lock_registry_is_bounded(self):
        self.assertEqual(len(api._PROVIDER_LOCK_STRIPES), 128)

    def test_check_stream_rate_limit_warning_hydra(self):
        """Verifies detection of Hydra Torrents and other provider rate-limit/ban notice streams."""
        # Exact payload structure seen in user's report
        banned_stream = {
            "name": "Hydra Torrents",
            "title": "Hydra Torrents - Aguarde 5 Minutos Muitas requisições (Banned)",
            "url": "https://husky-denny-fenixflixaddon-ec8e842b.koyeb.app/stream/51389",
            "behaviorHints": {
                "bingeGroup": "fenixflix-ratelimit"
            }
        }
        warn = api.check_stream_rate_limit_warning(banned_stream)
        self.assertIsNotNone(warn)
        self.assertTrue("ratelimit" in warn.lower() or "aguarde" in warn.lower() or "banned" in warn.lower())

    def test_check_stream_rate_limit_warning_generic(self):
        """Verifies detection of generic English rate-limit notices (Torrentio, MediaFusion, etc.)."""
        torrentio_limit = {
            "name": "Torrentio",
            "title": "Rate limit exceeded, try again in 5 minutes",
            "url": "https://example.com/dummy"
        }
        self.assertIsNotNone(api.check_stream_rate_limit_warning(torrentio_limit))

        clean_stream = {
            "name": "Hydra Torrents",
            "title": "Avatar 2009 1080p BluRay",
            "infoHash": "abcdef1234567890abcdef1234567890abcdef12"
        }
        self.assertIsNone(api.check_stream_rate_limit_warning(clean_stream))

    def test_provider_cooldown_lifecycle(self):
        """Verifies setting, checking, remaining duration, and clearing of provider cooldowns."""
        host = "hydra-torrent.noe359866.workers.dev"
        self.assertFalse(api.is_provider_in_cooldown(host))

        api.set_provider_cooldown(host, duration_seconds=300, reason="Test 5 min ban")
        self.assertTrue(api.is_provider_in_cooldown(host))
        self.assertGreater(api.get_provider_cooldown_remaining(host), 250)

        # Extraction from full URL
        full_url = f"https://{host}/manifest.json"
        self.assertTrue(api.is_provider_in_cooldown(full_url))

        api.clear_provider_cooldown(host)
        self.assertFalse(api.is_provider_in_cooldown(host))

    def test_acquire_provider_slot_concurrency_and_cooldown(self):
        """Ensures at most 1 concurrent in-flight request per host and respects cooldown."""
        host = "api.testprovider.com"
        
        # When in cooldown, acquire immediately yields False
        api.set_provider_cooldown(host, duration_seconds=60)
        with api._acquire_provider_slot(host, min_interval=0.1, timeout=0.5) as acquired:
            self.assertFalse(acquired)
        api.clear_provider_cooldown(host)

        # Successful acquisition
        with api._acquire_provider_slot(host, min_interval=0.05, timeout=1.0) as acquired:
            self.assertTrue(acquired)

    def test_request_addon_streams_http_429_triggers_cooldown(self):
        """HTTP 429 response automatically triggers cooldown and returns empty streams without crashing."""
        addon = {
            "name": "Hydra Torrents",
            "manifest_url": "https://hydra-torrent.noe359866.workers.dev/manifest.json"
        }
        url = "https://hydra-torrent.noe359866.workers.dev/stream/movie/tt1234567.json"

        # Mock urllib to raise HTTPError 429 with Retry-After: 120
        mock_headers = MagicMock()
        mock_headers.get.side_effect = lambda k, default=None: "120" if k == "Retry-After" else default
        http_err = urllib.error.HTTPError(url, 429, "Too Many Requests", mock_headers, None)

        with patch("urllib.request.urlopen", side_effect=http_err):
            streams = api._request_addon_streams(addon, url, timeout=2)
            self.assertEqual(streams, [])

        # Host must now be in cooldown for at least 100 seconds
        host = "hydra-torrent.noe359866.workers.dev"
        self.assertTrue(api.is_provider_in_cooldown(host))
        self.assertGreaterEqual(api.get_provider_cooldown_remaining(host), 100)

    def test_request_addon_streams_warning_stream_triggers_cooldown_and_filters(self):
        """When an addon returns a ban warning stream, it is filtered out and cooldown is set."""
        addon = {
            "name": "Hydra Torrents",
            "manifest_url": "https://hydra-torrent.noe359866.workers.dev/manifest.json"
        }
        url = "https://hydra-torrent.noe359866.workers.dev/stream/movie/tt1234567.json"

        payload = {
            "streams": [
                {
                    "name": "Hydra Torrents",
                    "title": "Hydra Torrents - Aguarde 5 Minutos Muitas requisições (Banned)",
                    "behaviorHints": {"bingeGroup": "fenixflix-ratelimit"}
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            streams = api._request_addon_streams(addon, url, timeout=2)
            # The warning stream must be filtered out!
            self.assertEqual(streams, [])

        # Addon must now be placed in 5-minute cooldown
        host = "hydra-torrent.noe359866.workers.dev"
        self.assertTrue(api.is_provider_in_cooldown(host))
        self.assertGreaterEqual(api.get_provider_cooldown_remaining(host), 250)

    def test_process_raw_streams_filters_rate_limit_notices(self):
        """process_raw_streams drops any warning streams even if present in raw list."""
        raw = [
            {
                "name": "Hydra Torrents",
                "title": "Hydra Torrents - Aguarde 5 Minutos Muitas requisições (Banned)",
                "behaviorHints": {"bingeGroup": "fenixflix-ratelimit"},
                "url": "https://dummy.stream"
            },
            {
                "name": "Good Addon",
                "title": "Movie 1080p",
                "infoHash": "1111111111111111111111111111111111111111"
            }
        ]
        processed = api.process_raw_streams(raw)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["hash"], "1111111111111111111111111111111111111111")

    def test_get_torrents_streamed_cache_first(self):
        """When cached streams exist and force_refresh is False, return cached without network calls."""
        cached_data = [{
            "hash": "2222222222222222222222222222222222222222",
            "title": "Cached Movie Stream",
            "addon_names": ["CachedAddon"]
        }]
        with patch("src.database.get_cached_streams", return_value=cached_data), \
             patch("src.api._request_addon_streams") as mock_req:
            results = api.get_torrents_streamed("tt9999999", media_type="movie", force_refresh=False)
            self.assertEqual(results, cached_data)
            mock_req.assert_not_called()

    def test_get_torrents_streamed_deduplicates_addon_queries(self):
        """An addon supporting both primary and secondary IDs is only queried once."""
        addon = {
            "name": "MultiID Addon",
            "manifest_url": "https://multi-id.test/manifest.json",
            "enabled": True,
            "streams_enabled": True,
            "resources": ["stream"],
            "types": ["movie"],
            "idPrefixes": ["tt", "tmdb:"]
        }
        with patch("src.database.get_cached_streams", return_value=None), \
             patch("src.database.get_addons", return_value=[addon]), \
             patch("src.api._request_addon_streams", return_value=[]) as mock_req, \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=["tt1234567", "tmdb:76543"]):
            api.get_torrents_streamed("tt1234567", media_type="movie", force_refresh=True)
            # Must only be called once, for tt1234567! Not twice!
            self.assertEqual(mock_req.call_count, 1)

    def test_get_torrents_streamed_normalizes_alias_id_list(self):
        """Stream addon URLs must use a concrete ID when the caller passes aliases."""
        addon = {
            "name": "Alias Provider",
            "manifest_url": "https://alias-provider.test/manifest.json",
            "enabled": True,
            "resources": ["stream"],
            "types": ["movie"],
            "idPrefixes": ["tt"],
        }
        with patch("src.database.get_cached_streams", return_value=None), \
             patch("src.database.get_addons", return_value=[addon]), \
             patch("src.api._request_addon_streams", return_value=[] ) as request_streams, \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]):
            api.get_torrents_streamed(["tt1234567", "tt7654321"], "movie", force_refresh=True)

        self.assertEqual(request_streams.call_count, 1)
        requested_url = request_streams.call_args.args[1]
        self.assertIn("tt1234567", requested_url)
        self.assertNotIn("[", requested_url)

    def test_stream_fetch_wait_budget_covers_provider_slot_and_request_timeout(self):
        """The collector must wait longer than provider-slot plus HTTP timeouts."""
        addon = {
            "name": "Slow Provider",
            "manifest_url": "https://slow-provider.test/manifest.json",
            "enabled": True,
            "streams_enabled": True,
            "resources": ["stream"],
            "types": ["movie"],
            "idPrefixes": ["tt"],
        }
        observed_timeouts = []
        real_wait = concurrent.futures.wait

        def capture_wait(futures, *args, **kwargs):
            observed_timeouts.append(kwargs.get("timeout"))
            return real_wait(futures, *args, **kwargs)

        with patch("src.database.get_cached_streams", return_value=None), \
             patch("src.database.get_addons", return_value=[addon]), \
             patch("src.api._request_addon_streams", return_value=[]), \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]), \
             patch("concurrent.futures.wait", side_effect=capture_wait):
            api.get_torrents_streamed("tt1234567", "movie", force_refresh=True)

        self.assertTrue(observed_timeouts)
        self.assertGreaterEqual(observed_timeouts[0], 10)

if __name__ == "__main__":
    unittest.main()
