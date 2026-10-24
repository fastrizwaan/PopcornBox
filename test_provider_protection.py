import unittest
import time
import json
import contextlib
import threading
import concurrent.futures
from unittest.mock import patch, MagicMock
import urllib.error
import ssl
import os
import hashlib
import subprocess
import tempfile
from pathlib import Path

# src.preferences instantiates Gio.Settings at import time, so the schema must
# be compiled and pointed at before importing any src module.
_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    _dest = Path(_schema_dir.name) / _schema_path.name
    _dest.write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

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

        # Other background workers (metadata hydration, details pages left
        # running by earlier tests) may hit this patched helper too; only
        # the calls for this test's URL matter.
        single_flight_calls = [u for u in calls if u.startswith("https://single-flight.test/")]
        self.assertEqual(single_flight_calls, ["https://single-flight.test/resource"])
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_provider_host_locks_are_independent(self):
        """A slow request to one host must not block or skip another provider."""
        slow_host = "slow-provider.test"
        other_host = "other-provider.test"
        entered = threading.Event()
        release = threading.Event()

        def hold_slow_host():
            with api._acquire_provider_slot(slow_host, min_interval=0.0, timeout=1.0) as ok:
                self.assertTrue(ok)
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_slow_host)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            # Same host is serialized and waits, then gives up.
            with api._acquire_provider_slot(slow_host, min_interval=0.0, timeout=0.3) as same_host_ok:
                self.assertFalse(same_host_ok)

            start = time.time()
            with api._acquire_provider_slot(other_host, min_interval=0.0, timeout=0.5) as other_ok:
                self.assertTrue(other_ok)
            self.assertLess(time.time() - start, 0.5)
        finally:
            release.set()
            thread.join(timeout=5)

        with api._acquire_provider_slot(slow_host, min_interval=0.0, timeout=0.5) as ok_after_release:
            self.assertTrue(ok_after_release)

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

    def test_request_addon_streams_retry_releases_host_slot_first(self):
        """A retry must not recurse while the host slot is held; the slot lock
        is not reentrant, so the retry would time out and be dropped."""
        addon = {"name": "Retry Addon", "manifest_url": "https://retry.test/manifest.json"}
        url = "https://retry.test/stream/movie/tt1.json"

        depth = {"active": 0, "max": 0}

        @contextlib.contextmanager
        def fake_slot(host, min_interval=0.4, timeout=4.0, abort_event=None):
            depth["active"] += 1
            depth["max"] = max(depth["max"], depth["active"])
            try:
                yield True
            finally:
                depth["active"] -= 1

        attempts = {"count": 0}
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps(
            {"streams": [{"url": "https://cdn.test/video"}]}
        ).encode("utf-8")
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.status = 200

        def fake_urlopen(req, timeout=None, context=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise urllib.error.HTTPError(req.full_url, 503, "unavailable", None, None)
            return ok_resp

        with patch.object(api, "_acquire_provider_slot", fake_slot), \
             patch.object(api, "_wait_before_provider_retry", lambda *a, **k: True), \
             patch.object(api, "_save_cached_stream_url", lambda *a: None), \
             patch.object(api, "_record_addon_success", lambda *a: None), \
             patch.object(api, "_record_addon_failure", lambda *a: None), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            streams = api._request_addon_streams(addon, url, timeout=2, retries=1)

        self.assertEqual(len(streams), 1)
        self.assertEqual(attempts["count"], 2)
        self.assertLessEqual(depth["max"], 1)

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

class TestPoliteCatalogFetch(unittest.TestCase):
    """Catalog, metadata and image requests must each go through a per-host
    window: paced, bounded in concurrency, closed while the host backs off, and
    answered from cache or left blank rather than by bursting."""

    def setUp(self):
        api.clear_provider_cooldown()
        self._cache_dir = tempfile.TemporaryDirectory()
        self._orig_cache_dir = api.CACHE_DIR
        api.CACHE_DIR = self._cache_dir.name
        with api._MEM_CACHE_LOCK:
            api._MEM_CACHE.clear()
        for host in list(api._PROVIDER_HOST_LOCKS):
            api._PROVIDER_HOST_LOCKS.pop(host, None)
        api._PROVIDER_LAST_REQUEST_TIME.clear()

    def tearDown(self):
        api.CACHE_DIR = self._orig_cache_dir
        self._cache_dir.cleanup()
        api.clear_provider_cooldown()
        with api._MEM_CACHE_LOCK:
            api._MEM_CACHE.clear()

    def _store_cached_response(self, url, payload, age_hours=240):
        """Write a disk cache entry that is older than any max_age_hours in use."""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        path = os.path.join(api.CACHE_DIR, url_hash)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
        return path

    @staticmethod
    def _response(payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    @staticmethod
    def _requested_urls(urlopen_mock):
        """URLs actually passed to the patched urlopen.

        `urllib.request.urlopen` is process-wide, so daemon threads left over
        from other suites can call it while a test is patching it. Assertions
        must be about this test's URLs, not about the call count.
        """
        urls = []
        for call in urlopen_mock.call_args_list:
            request = call.args[0] if call.args else None
            urls.append(str(getattr(request, "full_url", request)))
        return urls

    @contextlib.contextmanager
    def _host_window_held(self, host, slots=None, window="catalog"):
        """Occupy a host's polite window so the code under test has to queue."""
        acquire = (api._acquire_catalog_window if window == "catalog"
                   else api._acquire_image_window)
        slots = (api._CATALOG_HOST_PERMITS if window == "catalog"
                 else api._IMAGE_HOST_PERMITS) if slots is None else slots
        entered = threading.Semaphore(0)
        release = threading.Event()

        def hold():
            with acquire(host) as ok:
                if ok:
                    entered.release()
                    release.wait(timeout=10)

        holders = [threading.Thread(target=hold) for _ in range(slots)]
        for holder in holders:
            holder.start()
        for _ in range(slots):
            self.assertTrue(entered.acquire(timeout=5))
        try:
            yield
        finally:
            release.set()
            for holder in holders:
                holder.join(timeout=10)

    @staticmethod
    def _fetch_off_main_thread(url, **kwargs):
        """Run a fetch where the polite window is allowed to wait, and time it."""
        out = {}

        def run():
            started = time.time()
            out["data"] = api._get_cached_request(url, **kwargs)
            out["elapsed"] = time.time() - started

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=20)
        return out

    def _assert_not_fetched(self, urlopen_mock, prefix):
        hits = [u for u in self._requested_urls(urlopen_mock) if u.startswith(prefix)]
        self.assertEqual(hits, [], "host was requested while its polite window was closed")

    def test_catalog_requests_to_one_host_are_paced_and_bounded(self):
        host = "paced-catalog.test"
        prefix = f"https://{host}/"
        urls = [f"{prefix}catalog/{i}.json" for i in range(4)]
        state = {"active": 0, "max_active": 0, "starts": []}
        state_lock = threading.Lock()

        def fake_urlopen(req, timeout=None, context=None):
            # Background workers left over from earlier suites hit the patched
            # urlopen too; only this test's host says anything about pacing.
            if not str(req.full_url).startswith(prefix):
                return self._response({"ok": str(req.full_url)})
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["starts"].append(time.time())
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
            return self._response({"ok": str(req.full_url)})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            threads = [threading.Thread(target=api._get_cached_request, args=(u, 24)) for u in urls]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        self.assertEqual(len(state["starts"]), 4)
        self.assertLessEqual(state["max_active"], api._CATALOG_HOST_PERMITS)
        # Never closer together than the catalog pacing interval, whichever
        # order the threads happened to start in.
        gaps = [b - a for a, b in zip(sorted(state["starts"]), sorted(state["starts"][1:]))]
        self.assertTrue(all(gap >= api._CATALOG_MIN_INTERVAL - 0.02 for gap in gaps), gaps)

    def test_catalog_window_overlaps_slow_requests_up_to_its_permits(self):
        """Slow responses may overlap, but only as many as the window allows."""
        host = "slow-response-catalog.test"
        prefix = f"https://{host}/"
        urls = [f"{prefix}catalog/{i}.json" for i in range(api._CATALOG_HOST_PERMITS + 1)]
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def fake_urlopen(req, timeout=None, context=None):
            if not str(req.full_url).startswith(prefix):
                return self._response({"ok": str(req.full_url)})
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            # Long enough that the next paced request starts while this one is
            # still running, which is what makes the bound observable.
            time.sleep(0.6)
            with state_lock:
                state["active"] -= 1
            return self._response({"ok": str(req.full_url)})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            threads = [threading.Thread(target=api._get_cached_request, args=(u, 24)) for u in urls]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        self.assertEqual(state["max_active"], api._CATALOG_HOST_PERMITS)

    def test_host_window_never_exceeds_its_permits(self):
        host = "bounded-window.test"
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def claim():
            with api._acquire_provider_slot(host, min_interval=0.0, timeout=5.0, permits=2) as ok:
                if not ok:
                    return
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                time.sleep(0.1)
                with state_lock:
                    state["active"] -= 1

        threads = [threading.Thread(target=claim) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(state["max_active"], 2)

    def test_catalog_request_serves_stale_cache_while_host_is_in_cooldown(self):
        host = "cooling-catalog.test"
        url = f"https://{host}/catalog/top.json"
        self._store_cached_response(url, {"cached": "while cooling"})
        api.set_provider_cooldown(url, duration_seconds=120, reason="test 429")

        with patch("urllib.request.urlopen") as urlopen:
            data = api._get_cached_request(url, max_age_hours=1, timeout=0.5)

        self._assert_not_fetched(urlopen, f"https://{host}/")
        self.assertEqual(data, {"cached": "while cooling"})

    def test_catalog_http_429_sets_a_backoff_the_whole_app_honours(self):
        host = "limit-catalog.test"
        first = f"https://{host}/catalog/a.json"
        second = f"https://{host}/catalog/b.json"
        headers = MagicMock()
        headers.get.side_effect = lambda k, default=None: "120" if k == "Retry-After" else default
        err = urllib.error.HTTPError(first, 429, "Too Many Requests", headers, None)

        with patch("urllib.request.urlopen", side_effect=err) as urlopen:
            self.assertIsNone(api._get_cached_request(first, max_age_hours=24, timeout=0.5))
            # The next URL of the same host must not be tried either: the host
            # asked for a break and the window is closed for all of it.
            self.assertIsNone(api._get_cached_request(second, max_age_hours=24, timeout=0.5))

        self.assertEqual(self._requested_urls(urlopen), [first])
        self.assertTrue(api.is_provider_in_cooldown(host))
        self.assertGreaterEqual(api.get_provider_cooldown_remaining(host), 100)

    def test_denied_host_window_falls_back_to_the_disk_cache(self):
        """Queuing past the polite budget answers from cache instead of bursting."""
        host = "busy-catalog.test"
        url = f"https://{host}/catalog/top.json"
        self._store_cached_response(url, {"cached": "while busy"})

        with self._host_window_held(host), \
             patch.object(api, "_CATALOG_SLOT_WAIT", 0.2), \
             patch("urllib.request.urlopen") as urlopen:
            result = self._fetch_off_main_thread(url, max_age_hours=1, timeout=0.5)

        self._assert_not_fetched(urlopen, f"https://{host}/")
        self.assertEqual(result["data"], {"cached": "while busy"})

    def test_main_thread_never_queues_for_a_host_window(self):
        """The GTK thread must not sleep for pacing: it gets cache or nothing."""
        host = "ui-thread-catalog.test"
        url = f"https://{host}/catalog/top.json"
        self._store_cached_response(url, {"cached": "for the ui thread"})

        with self._host_window_held(host), patch("urllib.request.urlopen") as urlopen:
            started = time.time()
            data = api._get_cached_request(url, max_age_hours=1, timeout=0.5)
            elapsed = time.time() - started

        self._assert_not_fetched(urlopen, f"https://{host}/")
        self.assertLess(elapsed, 0.5)
        self.assertEqual(data, {"cached": "for the ui thread"})

    def test_cancelled_fetch_stops_queuing_for_the_host_window(self):
        """A batch the UI no longer wants must not sit in another host's queue."""
        host = "cancelled-catalog.test"
        url = f"https://{host}/catalog/top.json"

        with self._host_window_held(host), \
             patch.object(api, "_CATALOG_SLOT_WAIT", 5.0), \
             patch("urllib.request.urlopen") as urlopen:
            result = self._fetch_off_main_thread(
                url, max_age_hours=24, timeout=0.5, is_cancelled=lambda: True)

        self._assert_not_fetched(urlopen, f"https://{host}/")
        self.assertIsNone(result["data"])
        self.assertLess(result["elapsed"], 1.0)

    def test_offline_host_marked_while_queuing_releases_the_waiter(self):
        """A host that trips its circuit breaker must not keep siblings waiting."""
        host = "dying-catalog.test"
        url = f"https://{host}/catalog/top.json"

        try:
            with self._host_window_held(host), \
                 patch.object(api, "_CATALOG_SLOT_WAIT", 5.0), \
                 patch("urllib.request.urlopen") as urlopen:
                api._OFFLINE_HOSTS[host] = time.time()
                result = self._fetch_off_main_thread(url, max_age_hours=24, timeout=0.5)
        finally:
            api._OFFLINE_HOSTS.pop(host, None)
            api._OFFLINE_HOST_FAIL_COUNT.pop(host, None)

        self._assert_not_fetched(urlopen, f"https://{host}/")
        self.assertIsNone(result["data"])
        self.assertLess(result["elapsed"], 1.0)

    def test_image_fetches_to_one_host_are_bounded_and_paced(self):
        """A flowbox page hands ten image workers pictures that mostly live on two
        or three CDN hosts; a host gets two of them at a time, paced."""
        host = "paced-posters.test"
        state = {"active": 0, "max_active": 0, "starts": []}
        state_lock = threading.Lock()

        def claim():
            with api._acquire_image_window(host) as ok:
                if not ok:
                    return
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                    state["starts"].append(time.time())
                time.sleep(0.08)
                with state_lock:
                    state["active"] -= 1

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(len(state["starts"]), 8)
        self.assertEqual(api._IMAGE_HOST_PERMITS, 2,
                         "one permit serialised a page of posters to ~0.8s each")
        self.assertEqual(state["max_active"], api._IMAGE_HOST_PERMITS)
        gaps = [b - a for a, b in zip(sorted(state["starts"]), sorted(state["starts"][1:]))]
        self.assertTrue(all(gap >= api._IMAGE_MIN_INTERVAL - 0.02 for gap in gaps), gaps)

    def test_image_window_gives_up_after_its_wait_budget(self):
        """A poster that cannot reach a slot in time stays blank instead of
        pinning an image worker behind a slow CDN."""
        host = "busy-posters.test"
        outcome = []

        with self._host_window_held(host, window="image"), \
             patch.object(api, "_IMAGE_SLOT_WAIT", 0.3):
            started = time.time()

            def wait_for_slot():
                with api._acquire_image_window(host) as ok:
                    outcome.append((ok, time.time() - started))

            waiter = threading.Thread(target=wait_for_slot)
            waiter.start()
            waiter.join(timeout=5)

        self.assertEqual(len(outcome), 1)
        opened, elapsed = outcome[0]
        self.assertFalse(opened, "image window stayed open past its wait budget")
        self.assertLess(elapsed, 2.0)

class TestCardCacheLifetime(unittest.TestCase):
    """Card metadata is kept forever so the same titles are never asked of a
    provider twice; responses that genuinely change (streams, subtitles) still
    age out on their own TTL."""

    def setUp(self):
        api.clear_provider_cooldown()
        self._cache_dir = tempfile.TemporaryDirectory()
        self._orig_cache_dir = api.CACHE_DIR
        api.CACHE_DIR = self._cache_dir.name
        with api._MEM_CACHE_LOCK:
            api._MEM_CACHE.clear()
        self._orig_last_prune = api._DISK_CACHE_LAST_PRUNE
        self._orig_max_bytes = api._DISK_CACHE_MAX_BYTES
        api._DISK_CACHE_LAST_PRUNE = 0.0

    def tearDown(self):
        api.CACHE_DIR = self._orig_cache_dir
        api._DISK_CACHE_LAST_PRUNE = self._orig_last_prune
        api._DISK_CACHE_MAX_BYTES = self._orig_max_bytes
        self._cache_dir.cleanup()
        with api._MEM_CACHE_LOCK:
            api._MEM_CACHE.clear()

    def _store(self, url, payload, age_hours=400 * 24, read_hours_ago=None):
        """A disk cache entry of any age, optionally read at a given time."""
        path = os.path.join(api.CACHE_DIR, hashlib.md5(url.encode()).hexdigest())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        wrote = time.time() - age_hours * 3600
        read = time.time() - (read_hours_ago if read_hours_ago is not None else age_hours) * 3600
        os.utime(path, (read, wrote))
        return path

    def test_card_metadata_has_one_expiry_rule_and_it_is_no_expiry(self):
        self.assertIsNone(api.METADATA_CACHE_HOURS)
        self.assertIsNone(database.CATALOG_CACHE_HOURS)

    def test_a_year_old_card_response_is_served_without_asking_again(self):
        url = "https://card-metadata.test/meta/tt0111161.json"
        self._store(url, {"meta": {"id": "tt0111161", "name": "The Shawshank Redemption"}})
        asked = []

        def fake_urlopen(req, timeout=None, context=None):
            asked.append(str(req.full_url))
            raise AssertionError("a cached card was fetched again")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = api._get_cached_request(url, max_age_hours=api.METADATA_CACHE_HOURS)

        self.assertEqual((data or {}).get("meta", {}).get("id"), "tt0111161")
        self.assertEqual(asked, [])

    def test_stream_and_subtitle_reads_still_age_out(self):
        """The forever rule must not leak into responses that change: an hour-old
        stream list has to be re-fetched, not served for a year."""
        url = "https://streams-change.test/stream/movie/tt0111161.json"
        self._store(url, {"streams": [{"name": "last month"}]})
        fresh = api._get_cached_request(url, max_age_hours=24, cache_only=True)
        self.assertIsNone(fresh, "a stream response was served long past its TTL")

    def test_the_pruner_keeps_entries_of_any_age_inside_the_size_budget(self):
        paths = [self._store(f"https://keep.test/{i}.json", {"i": i}, age_hours=400 * 24)
                 for i in range(3)]
        api._prune_disk_cache_if_needed(time.time())
        for path in paths:
            self.assertTrue(os.path.exists(path), "old card metadata was deleted for being old")

    def test_the_pruner_trims_the_least_recently_read_when_over_budget(self):
        stale = self._store("https://budget.test/stale.json",
                            {"a": "x" * 200}, read_hours_ago=900)
        current = self._store("https://budget.test/current.json", {"b": 1}, read_hours_ago=1)
        api._DISK_CACHE_MAX_BYTES = os.path.getsize(stale)
        api._prune_disk_cache_if_needed(time.time())
        self.assertFalse(os.path.exists(stale), "the budget evicted a recently read card")
        self.assertTrue(os.path.exists(current))

if __name__ == "__main__":
    unittest.main()
