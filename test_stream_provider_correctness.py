"""
Targeted tests for stream provider correctness:
  - TMDB ID correctness for Nuvio plugin scrapers
  - Stale-cache eviction when TMDB ID changes
  - Duplicate scraper deduplication (code-identical vs different)
  - Wrong-movie-year filter (movies only, TV unaffected)
  - execute_scraper error classification
  - Deno/Node runtime detection reporting
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Schema bootstrap (required before importing any src module that uses GLib)
# ---------------------------------------------------------------------------
sys.modules.setdefault("mpv", MagicMock())

_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    _dest = Path(_schema_dir.name) / _schema_path.name
    _dest.write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

from src import api, database, plugins

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scraper(name, code, repo_url, raw_id=None, enabled=True, types=None):
    rid = raw_id or name.lower().replace(" ", "_")
    return {
        "id": f"{repo_url.lower()}:{rid}",
        "raw_id": rid,
        "repository_url": repo_url,
        "name": name,
        "supported_types": types or ["movie", "tv"],
        "enabled": enabled,
        "code": code,
        "_duplicate_provider_names": [],
    }


SIMPLE_JS = "async function getStreams(){return [];} module.exports={getStreams};"
SCRAPER_ID_JS = "const SCRAPER_ID='abc'; async function getStreams(){return [];} module.exports={getStreams};"


# ===========================================================================
# 1. Deduplication tests
# ===========================================================================

class TestDeduplicateScrapers(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        patcher = patch.object(database, "DB_FILE", Path(self.td.name) / "db.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        database._json_cache = None
        database._json_cache_valid = False

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.td.cleanup()

    # --- identical code from two repos → collapsed to one ---
    def test_identical_code_collapsed(self):
        code = "async function getStreams(id){return [{url:'http://x/a.mp4',name:'P'}];} module.exports={getStreams};"
        s1 = _make_scraper("Alpha", code, "https://repo1.test/manifest.json")
        s2 = _make_scraper("Alpha", code, "https://repo2.test/manifest.json")
        result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 1, "Identical code from two repos must be collapsed")

    # --- duplicate label is preserved ---
    def test_duplicate_label_preserved(self):
        code = "async function getStreams(id){return [{url:'http://x/b.mp4',name:'P'}];} module.exports={getStreams};"
        s1 = _make_scraper("ProvA", code, "https://repo1.test/manifest.json")
        s2 = _make_scraper("ProvB", code, "https://repo2.test/manifest.json")
        result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 1)
        surviving = result[0]
        # Both labels must be traceable
        all_names = [surviving["name"]] + surviving.get("_duplicate_provider_names", [])
        self.assertIn("ProvA", all_names)
        self.assertIn("ProvB", all_names)

    # --- different code from two repos → kept independent ---
    def test_different_code_kept_independent(self):
        s1 = _make_scraper("Delta", "async function getStreams(){return [{url:'http://v1/a'}];} module.exports={getStreams};", "https://repo1.test/manifest.json")
        s2 = _make_scraper("Delta", "async function getStreams(){return [{url:'http://v2/a'}];} module.exports={getStreams};", "https://repo2.test/manifest.json")
        result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 2, "Scrapers with different code must run independently")

    # --- SCRAPER_ID in code → always kept independent ---
    def test_scraper_id_in_code_always_independent(self):
        code = SCRAPER_ID_JS  # same code but references SCRAPER_ID
        s1 = _make_scraper("Echo", code, "https://repo1.test/manifest.json")
        s2 = _make_scraper("Echo", code, "https://repo2.test/manifest.json")
        result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 2, "SCRAPER_ID scrapers must remain independent even if code is identical")

    # --- empty code → kept independent ---
    def test_empty_code_kept_independent(self):
        s1 = _make_scraper("Foxtrot", "", "https://repo1.test/manifest.json")
        s2 = _make_scraper("Foxtrot", "", "https://repo2.test/manifest.json")
        result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 2)

    # --- same code but different settings → kept independent ---
    def test_different_settings_kept_independent(self):
        code = "async function getStreams(){return [];} module.exports={getStreams};"
        s1 = _make_scraper("Golf", code, "https://repo1.test/manifest.json", raw_id="golf")
        s2 = _make_scraper("Golf", code, "https://repo2.test/manifest.json", raw_id="golf2")
        # Give s2 different user settings
        with patch.object(database, "get_setting", side_effect=lambda key, default=None: (
            {f"{s1['id']}": {}, f"{s2['id']}": {"quality": "4k"}} if key == "plugins_scraper_settings" else default
        )):
            result = plugins.deduplicate_scrapers([s1, s2])
        self.assertEqual(len(result), 2, "Different settings → scrapers must run independently")

    # --- disabled duplicate does not block enabled copy ---
    def test_disabled_duplicate_not_in_result_of_enabled_filter(self):
        code = "async function getStreams(){return [];} module.exports={getStreams};"
        s1 = _make_scraper("Hotel", code, "https://repo1.test/manifest.json", enabled=True)
        s2 = _make_scraper("Hotel", code, "https://repo2.test/manifest.json", enabled=False)
        # Only enabled scrapers for type 'movie'
        with patch.object(plugins, "get_all_scrapers", return_value=[s1, s2]):
            enabled = plugins.get_enabled_scrapers_for_type("movie")
        # The disabled one must not appear
        result = plugins.deduplicate_scrapers(enabled)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["enabled"])


# ===========================================================================
# 2. execute_scraper error classification tests
# ===========================================================================

class TestScraperErrorClassification(unittest.TestCase):

    def _base_scraper(self, code=None):
        return {
            "id": "test-repo:test",
            "name": "TestScraper",
            "code": SIMPLE_JS if code is None else code,
            "supported_types": ["movie", "tv"],
            "enabled": True,
            "_duplicate_provider_names": [],
        }

    def test_no_runtime_returns_no_runtime_error(self):
        with patch.object(plugins, "find_js_runtime", return_value=None):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie")
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_NO_RUNTIME)

    def test_no_code_returns_no_code_error(self):
        s = self._base_scraper(code="")
        with patch.object(plugins, "find_js_runtime", return_value=["node"]):
            result = plugins.execute_scraper(s, tmdb_id=603, media_type="movie")
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_NO_CODE)

    def test_timeout_returns_timeout_error(self):
        import subprocess as sp

        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            proc.communicate.side_effect = sp.TimeoutExpired(cmd="node", timeout=1)
            proc.kill = MagicMock()
            proc.wait = MagicMock()
            return proc

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie", timeout=1)
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_TIMEOUT)

    def test_crash_returns_crash_error(self):
        import subprocess as sp

        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            proc.communicate.return_value = ("", "SyntaxError: unexpected token")
            proc.returncode = 1
            return proc

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie")
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_CRASH)

    def test_empty_result_returns_empty_error(self):
        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            proc.communicate.return_value = ("[]", "")
            proc.returncode = 0
            return proc

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie")
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_EMPTY)

    def test_invalid_json_returns_invalid_json_error(self):
        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            proc.communicate.return_value = ("this is not json at all", "")
            proc.returncode = 0
            return proc

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie")
        self.assertEqual(result, [])
        self.assertEqual(getattr(result, "_scraper_error", None), plugins.SCRAPER_ERROR_INVALID_JSON)

    def test_valid_result_has_no_error(self):
        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            payload = json.dumps([{"url": "https://cdn.test/movie.mp4", "name": "FastProvider", "title": "1080p"}])
            proc.communicate.return_value = (payload, "")
            proc.returncode = 0
            return proc

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(self._base_scraper(), tmdb_id=603, media_type="movie")
        self.assertTrue(len(result) >= 1)
        self.assertIsNone(getattr(result, "_scraper_error", None))


# ===========================================================================
# 3. TMDB ID correctness for plugin scrapers
# ===========================================================================

class TestPluginTmdbIdCorrectness(unittest.TestCase):
    """Verify that execute_scraper stamps plugin_scraper_tmdb_id on results."""

    def _make_popen(self, streams):
        payload = json.dumps(streams)
        def fake_popen(*args, **kwargs):
            proc = MagicMock()
            proc.communicate.return_value = (payload, "")
            proc.returncode = 0
            return proc
        return fake_popen

    def test_plugin_scraper_tmdb_id_set_on_result(self):
        scraper = {
            "id": "repo:test",
            "name": "TMDB-ID Test",
            "code": SIMPLE_JS,
            "supported_types": ["movie"],
            "enabled": True,
        }
        fake_streams = [{"url": "https://stream.test/vid.mp4", "name": "X", "title": "1080p"}]
        fake_popen = self._make_popen(fake_streams)

        with patch.object(plugins, "find_js_runtime", return_value=["node", "bundle.js"]), \
             patch("subprocess.Popen", side_effect=fake_popen):
            result = plugins.execute_scraper(scraper, tmdb_id="12345", media_type="movie")

        self.assertTrue(len(result) >= 1)
        for stream in result:
            self.assertEqual(
                stream.get("plugin_scraper_tmdb_id"), "12345",
                "plugin_scraper_tmdb_id must equal the queried TMDB ID"
            )

    def test_plugin_scraper_tmdb_id_propagated_through_process_raw_streams(self):
        """process_raw_streams must carry plugin_scraper_tmdb_id into the parsed entry."""
        raw = [{
            "url": "https://stream.test/vid.mp4",
            "name": "Provider",
            "title": "1080p quality",
            "is_nuvio_plugin": True,
            "plugin_scraper_id": "repo:test",
            "plugin_scraper_tmdb_id": "99999",
        }]
        parsed = api.process_raw_streams(raw)
        self.assertTrue(len(parsed) >= 1)
        self.assertEqual(parsed[0].get("plugin_scraper_tmdb_id"), "99999")


# ===========================================================================
# 4. Stale cached plugin streams filtered when TMDB ID changes
# ===========================================================================

class TestStaleCachePluginTmdbFilter(unittest.TestCase):
    """Cached Nuvio plugin streams from a different TMDB ID must be evicted."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        patcher = patch.object(database, "DB_FILE", Path(self.td.name) / "db.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        database._json_cache = None
        database._json_cache_valid = False
        api.clear_stream_url_cache()

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.td.cleanup()

    def _make_cached_plugin_stream(self, tmdb_id, url, addon_name="TestPlugin"):
        return {
            "hash": hashlib.md5(url.encode()).hexdigest(),
            "url": url,
            "is_http": True,
            "is_nuvio_plugin": True,
            "plugin_scraper_id": "repo:test",
            "plugin_scraper_tmdb_id": str(tmdb_id),
            "addon_name": addon_name,
            "addon_names": [addon_name],
            "quality": "1080p",
            "stream_title": "1080p",
            "saved_at": __import__("time").time(),
        }

    def test_wrong_tmdb_id_plugin_stream_evicted_from_cache(self):
        """A cached plugin stream for TMDB 11111 must not appear when requesting TMDB 22222."""
        wrong_stream = self._make_cached_plugin_stream(tmdb_id="11111", url="https://wrong.test/movie.mp4")
        correct_stream = self._make_cached_plugin_stream(tmdb_id="22222", url="https://correct.test/movie.mp4")

        cache_key = api.get_stream_cache_key("tt0000001", "movie")
        database.save_cached_streams_full(
            cache_key, cache_key + ":source:direct", cache_key + ":source:torrent",
            [wrong_stream, correct_stream],
        )

        result_streams = []
        done = __import__("threading").Event()

        def callback(streams, is_cached=False, is_complete=False):
            result_streams.extend(streams)
            if is_complete:
                done.set()

        with patch.object(database, "get_addons", return_value=[]), \
             patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_enabled_scrapers_for_type", return_value=[]):
            # Request with known_tmdb_id=22222 — should drop the wrong-tmdb stream
            api.get_torrents_streamed(
                "tt0000001",
                media_type="movie",
                callback=callback,
                known_tmdb_id="22222",
                force_refresh=False,
            )
        done.wait(timeout=5)

        returned_tmdb_ids = {s.get("plugin_scraper_tmdb_id") for s in result_streams}
        self.assertNotIn(
            "11111",
            returned_tmdb_ids,
            "Stream with wrong TMDB ID must be filtered out of cached results"
        )
        self.assertIn(
            "22222",
            returned_tmdb_ids,
            "Stream with correct TMDB ID must remain in cached results"
        )

    def test_correct_tmdb_id_plugin_stream_kept(self):
        """A cached plugin stream with the correct TMDB ID must still be served."""
        correct_stream = self._make_cached_plugin_stream(tmdb_id="22222", url="https://correct.test/movie.mp4")

        cache_key = api.get_stream_cache_key("tt0000002", "movie")
        database.save_cached_streams_full(
            cache_key, cache_key + ":source:direct", cache_key + ":source:torrent",
            [correct_stream],
        )

        result_streams = []
        done = __import__("threading").Event()

        def callback(streams, is_cached=False, is_complete=False):
            result_streams.extend(streams)
            if is_complete:
                done.set()

        with patch.object(database, "get_addons", return_value=[]), \
             patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_enabled_scrapers_for_type", return_value=[]):
            api.get_torrents_streamed(
                "tt0000002",
                media_type="movie",
                callback=callback,
                known_tmdb_id="22222",
                force_refresh=False,
            )
        done.wait(timeout=5)

        self.assertTrue(
            any(s.get("plugin_scraper_tmdb_id") == "22222" for s in result_streams),
            "Correct TMDB stream must be served from cache"
        )

    def test_plugin_stream_without_tmdb_id_not_filtered(self):
        """Cached plugin streams that pre-date this feature (no tmdb_id field) must still appear."""
        legacy_stream = {
            "hash": hashlib.md5(b"legacy").hexdigest(),
            "url": "https://legacy.test/movie.mp4",
            "is_http": True,
            "is_nuvio_plugin": True,
            "addon_name": "LegacyPlugin",
            "addon_names": ["LegacyPlugin"],
            "quality": "720p",
            "stream_title": "720p",
            "saved_at": __import__("time").time(),
            # No plugin_scraper_tmdb_id field
        }

        cache_key = api.get_stream_cache_key("tt0000003", "movie")
        database.save_cached_streams_full(
            cache_key, cache_key + ":source:direct", cache_key + ":source:torrent",
            [legacy_stream],
        )

        result_streams = []
        done = __import__("threading").Event()

        def callback(streams, is_cached=False, is_complete=False):
            result_streams.extend(streams)
            if is_complete:
                done.set()

        with patch.object(database, "get_addons", return_value=[]), \
             patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_enabled_scrapers_for_type", return_value=[]):
            api.get_torrents_streamed(
                "tt0000003",
                media_type="movie",
                callback=callback,
                known_tmdb_id="33333",
                force_refresh=False,
            )
        done.wait(timeout=5)

        self.assertTrue(
            any(s.get("url") == "https://legacy.test/movie.mp4" for s in result_streams),
            "Legacy plugin stream without plugin_scraper_tmdb_id must not be filtered"
        )


# ===========================================================================
# 5. Wrong-movie-year filter
# ===========================================================================

class TestWrongYearFilter(unittest.TestCase):
    """_filter_wrong_movie_year discards streams with an explicitly different year (movies only)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        patcher = patch.object(database, "DB_FILE", Path(self.td.name) / "db.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        database._json_cache = None
        database._json_cache_valid = False
        api.clear_stream_url_cache()

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.td.cleanup()

    def _run_filter(self, streams, expected_year, media_type="movie"):
        result_streams = []
        done = __import__("threading").Event()

        cache_key = api.get_stream_cache_key("tt_year_test", media_type)
        database.save_cached_streams_full(
            cache_key, cache_key + ":source:direct", cache_key + ":source:torrent",
            streams,
        )

        def callback(s, is_cached=False, is_complete=False):
            result_streams.extend(s)
            if is_complete:
                done.set()

        with patch.object(database, "get_addons", return_value=[]), \
             patch.object(plugins, "is_plugins_enabled", return_value=False):
            api.get_torrents_streamed(
                "tt_year_test",
                media_type=media_type,
                callback=callback,
                expected_year=expected_year,
                force_refresh=False,
            )
        done.wait(timeout=5)
        return result_streams

    def _make_stream(self, title, url=None, name=None):
        u = url or f"https://cdn.test/{title.replace(' ', '_')}.mp4"
        return {
            "hash": hashlib.md5(u.encode()).hexdigest(),
            "url": u,
            "is_http": True,
            "stream_title": name or title,
            "title": name or title,
            "addon_name": "Provider",
            "addon_names": ["Provider"],
            "quality": "1080p",
            "saved_at": __import__("time").time(),
        }

    def test_wrong_year_in_title_discarded(self):
        good = self._make_stream("Movie 2015 BluRay 1080p", url="https://good.test/m.mp4")
        bad = self._make_stream("Movie 2018 WEB-DL 1080p", url="https://bad.test/m.mp4")
        result = self._run_filter([good, bad], expected_year="2015")
        urls = {s.get("url") for s in result}
        self.assertIn("https://good.test/m.mp4", urls)
        self.assertNotIn("https://bad.test/m.mp4", urls)

    def test_no_year_in_title_not_discarded(self):
        """Streams that omit a year entirely must pass through — do not reject on absence."""
        no_year = self._make_stream("Movie BluRay 1080p x264", url="https://noyear.test/m.mp4")
        result = self._run_filter([no_year], expected_year="2015")
        urls = {s.get("url") for s in result}
        self.assertIn("https://noyear.test/m.mp4", urls, "Stream without a year must not be rejected")

    def test_correct_year_in_title_kept(self):
        good = self._make_stream("Movie (2015) 1080p", url="https://correct.test/m.mp4")
        result = self._run_filter([good], expected_year="2015")
        self.assertEqual(len(result), 1)

    def test_tv_series_not_filtered_by_year(self):
        """Year filter must be a no-op for TV series."""
        s1 = self._make_stream("Show S01E01 2018 720p", url="https://tvshow.test/ep1.mp4")
        s2 = self._make_stream("Show S01E01 2022 720p", url="https://tvshow.test/ep2.mp4")
        result = self._run_filter([s1, s2], expected_year="2018", media_type="series")
        urls = {s.get("url") for s in result}
        # Both must survive because year filter is disabled for series
        self.assertIn("https://tvshow.test/ep1.mp4", urls)
        self.assertIn("https://tvshow.test/ep2.mp4", urls)


# ===========================================================================
# 6. Runtime availability test (safe skip when runtime absent)
# ===========================================================================

class TestRuntimeAvailability(unittest.TestCase):

    def test_find_js_runtime_returns_list_or_none(self):
        # Reset the cached state so we probe fresh
        plugins._runtime_checked = False
        plugins._js_runtime_cmd = None
        result = plugins.find_js_runtime()
        # Result must be either None (no runtime) or a non-empty list
        if result is not None:
            self.assertIsInstance(result, list)
            self.assertGreater(len(result), 0)
        # Reset so other tests get the real cached value
        plugins._runtime_checked = False
        plugins._js_runtime_cmd = None

    def test_is_runtime_available_matches_find_js_runtime(self):
        plugins._runtime_checked = False
        plugins._js_runtime_cmd = None
        runtime = plugins.find_js_runtime()
        expected = runtime is not None
        plugins._runtime_checked = False
        plugins._js_runtime_cmd = None
        self.assertEqual(plugins.is_runtime_available(), expected)
        plugins._runtime_checked = False
        plugins._js_runtime_cmd = None

    @unittest.skipUnless(plugins.is_runtime_available(), "No Deno/Node.js runtime on this system")
    def test_runner_bundle_exists_when_runtime_present(self):
        bundle_path = plugins.get_runner_bundle_path()
        self.assertTrue(
            os.path.exists(bundle_path),
            f"Runner bundle not found at {bundle_path}; Deno/Node is available but runner is missing",
        )


# ===========================================================================
# 7. Stremio addon ID prefix check for media type
# ===========================================================================

class TestStremioAddonIdPrefixCheck(unittest.TestCase):

    def _make_addon(self, name, prefixes, types, resources=None):
        return {
            "id": name.lower(),
            "name": name,
            "manifest_url": f"https://{name.lower()}.test/manifest.json",
            "enabled": True,
            "streams_enabled": True,
            "idPrefixes": prefixes,
            "types": types,
            "resources": resources or [{"name": "stream", "types": types, "idPrefixes": prefixes}],
            "catalogs": [],
        }

    def test_tt_id_accepted_by_addon_with_tt_prefix(self):
        addon = self._make_addon("CinemaAddon", ["tt"], ["movie"])
        self.assertTrue(api.has_stream_resource(addon, media_type="movie", item_id="tt1234567"))

    def test_tmdb_id_rejected_by_addon_with_tt_prefix_only(self):
        addon = self._make_addon("CinemaAddon", ["tt"], ["movie"])
        self.assertFalse(api.has_stream_resource(addon, media_type="movie", item_id="tmdb:9999"))

    def test_tmdb_id_accepted_by_addon_with_tmdb_prefix(self):
        addon = self._make_addon("TmdbAddon", ["tmdb:"], ["movie"])
        self.assertTrue(api.has_stream_resource(addon, media_type="movie", item_id="tmdb:9999"))

    def test_series_addon_rejects_movie_type(self):
        addon = self._make_addon("SeriesAddon", ["tt"], ["series"])
        self.assertFalse(api.has_stream_resource(addon, media_type="movie", item_id="tt1234567"))

    def test_multi_type_addon_accepts_both(self):
        addon = self._make_addon("AllAddon", ["tt"], ["movie", "series"])
        self.assertTrue(api.has_stream_resource(addon, media_type="movie", item_id="tt0000001"))
        self.assertTrue(api.has_stream_resource(addon, media_type="series", item_id="tt0000002"))


if __name__ == "__main__":
    unittest.main()
