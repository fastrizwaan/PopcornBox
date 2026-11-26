import unittest
from unittest.mock import patch, MagicMock
import time

class TestAddonCategorizationAndHealth(unittest.TestCase):
    def setUp(self):
        from src import database
        self.orig_get_addons = database.get_addons
        self.orig_get_addon_health = database.get_addon_health
        self.orig_get_setting = database.get_setting
        self.orig_set_setting = database.set_setting

        self.fake_db = {
            "settings": {
                "addon_health": {
                    "https://online-catalog.com/manifest.json": {
                        "online": True,
                        "last_checked": time.time(),
                        "latency_ms": 120,
                        "error": None
                    },
                    "https://dead-addon.com/manifest.json": {
                        "online": False,
                        "last_checked": time.time(),
                        "latency_ms": 2500,
                        "error": "HTTP 404"
                    }
                }
            }
        }

    def test_database_addon_health_crud(self):
        from src import database
        with patch.object(database, '_read_db', return_value=self.fake_db), \
             patch.object(database, '_write_db') as mock_write:
            
            health = database.get_addon_health()
            self.assertTrue(health["https://online-catalog.com/manifest.json"]["online"])
            self.assertFalse(health["https://dead-addon.com/manifest.json"]["online"])

            database.update_addon_health("https://new-addon.com/manifest.json", online=True, latency_ms=45)
            self.assertTrue(mock_write.called)
            updated_health = database.get_addon_health()
            self.assertIn("https://new-addon.com/manifest.json", updated_health)
            self.assertTrue(updated_health["https://new-addon.com/manifest.json"]["online"])

    def test_addon_categorization_providers(self):
        from src import api

        catalog_addon = {
            "id": "org.cinemeta",
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "streams_enabled": True,
            "resources": ["catalog", "meta"],
            "catalogs": [
                {"type": "movie", "id": "top", "name": "Top Movies", "extra": ["search", "genre"]}
            ]
        }

        stream_only_addon = {
            "id": "org.torrentio",
            "name": "Torrentio",
            "manifest_url": "https://torrentio.strem.fun/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "streams_enabled": True,
            "resources": ["stream"],
            "catalogs": []
        }

        # Catalog Provider Checks
        self.assertTrue(api.is_catalog_provider(catalog_addon, media_type="movie"))
        self.assertFalse(api.is_catalog_provider(stream_only_addon, media_type="movie"))

        # Search Provider Checks
        self.assertTrue(api.is_search_provider(catalog_addon, media_type="movie"))
        self.assertFalse(api.is_search_provider(stream_only_addon, media_type="movie"))

        # Stream Provider Checks (Cinemeta excluded, Torrentio included)
        self.assertFalse(api.is_stream_provider(catalog_addon, media_type="movie"))
        self.assertTrue(api.is_stream_provider(stream_only_addon, media_type="movie"))

    def test_is_addon_online_with_health_cache(self):
        from src import api, database
        with patch.object(database, 'get_addon_health', return_value=self.fake_db["settings"]["addon_health"]):
            self.assertTrue(api.is_addon_online("https://online-catalog.com/manifest.json"))
            self.assertFalse(api.is_addon_online("https://dead-addon.com/manifest.json"))

    def test_previous_session_offline_verdict_is_not_trusted(self):
        from src import api, database
        url = "https://previous-session.example/manifest.json"
        stale = {
            "online": False,
            "last_checked": api._ADDON_HEALTH_SESSION_START - 60,
            "latency_ms": 0,
            "error": "timeout",
        }
        try:
            with api._ADDON_ONLINE_LOCK:
                api._IN_MEMORY_ADDON_HEALTH[url] = stale
            with patch.object(database, 'get_addon_health', return_value={url: stale}):
                # A failure recorded before this launch describes a network
                # state we have not re-verified yet.
                self.assertTrue(api.is_addon_online(url))

            fresh = dict(stale, last_checked=time.time())
            with api._ADDON_ONLINE_LOCK:
                api._IN_MEMORY_ADDON_HEALTH[url] = fresh
            self.assertFalse(api.is_addon_online(url))
        finally:
            with api._ADDON_ONLINE_LOCK:
                api._IN_MEMORY_ADDON_HEALTH.pop(url, None)

    def test_health_check_retries_transient_probe_failures(self):
        from src import api, database
        url = "https://retry.example/manifest.json"
        addon = {"id": "retry.test", "manifest_url": url, "enabled": True}
        manifest = {"id": "retry.test", "resources": ["stream"]}
        attempts = []

        def fake_live_probe(probe_url, timeout=4.0):
            attempts.append(probe_url)
            return None if len(attempts) == 1 else manifest

        try:
            with patch.object(database, "get_addons", return_value=[addon]), \
                 patch.object(api, "_cached_addon_manifest", return_value=None), \
                 patch.object(api, "_fetch_addon_manifest_live", side_effect=fake_live_probe), \
                 patch.object(api, "_ADDON_HEALTH_RETRY_DELAYS", (0.0, 0.0)), \
                 patch.object(database, "set_all_addons_health") as set_health, \
                 patch.object(database, "add_addon"), \
                 patch.object(database, "update_addon_catalogs"):
                thread = api.check_addons_health_background()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertEqual(attempts, [url, url])
            self.assertTrue(set_health.called)
            with api._ADDON_ONLINE_LOCK:
                self.assertTrue(api._IN_MEMORY_ADDON_HEALTH[url]["online"])
        finally:
            with api._ADDON_ONLINE_LOCK:
                api._IN_MEMORY_ADDON_HEALTH.pop(url, None)

    def test_health_check_keeps_statuses_when_every_probe_fails(self):
        from src import api, database
        addons = [
            {"id": "down.one", "manifest_url": "https://down-one.example/manifest.json", "enabled": True},
            {"id": "down.two", "manifest_url": "https://down-two.example/manifest.json", "enabled": True},
        ]
        try:
            with patch.object(database, "get_addons", return_value=addons), \
                 patch.object(api, "_cached_addon_manifest", return_value=None), \
                 patch.object(api, "_fetch_addon_manifest_live", return_value=None), \
                 patch.object(api, "_ADDON_HEALTH_RETRY_DELAYS", (0.0, 0.0)), \
                 patch.object(database, "set_all_addons_health") as set_health:
                thread = api.check_addons_health_background()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            # A network outage must not paint every addon offline for two hours.
            self.assertFalse(set_health.called)
            with api._ADDON_ONLINE_LOCK:
                for addon in addons:
                    self.assertNotIn(addon["manifest_url"], api._IN_MEMORY_ADDON_HEALTH)
        finally:
            with api._ADDON_ONLINE_LOCK:
                for addon in addons:
                    api._IN_MEMORY_ADDON_HEALTH.pop(addon["manifest_url"], None)

    def test_get_available_search_catalogs(self):
        from src import api, database
        catalog_addon = {
            "id": "org.cinemeta",
            "name": "Cinemeta",
            "manifest_url": "https://online-catalog.com/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "resources": ["catalog"],
            "catalogs": [
                {"type": "movie", "id": "top", "name": "Top Movies", "extra": ["search"]}
            ]
        }
        stream_addon = {
            "id": "org.torrentio",
            "name": "Torrentio",
            "manifest_url": "https://online-catalog.com/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "resources": ["stream"],
            "catalogs": []
        }

        with patch.object(database, 'get_addons', return_value=[catalog_addon, stream_addon]), \
             patch.object(database, 'get_addon_health', return_value=self.fake_db["settings"]["addon_health"]):
            
            search_cats = api.get_available_search_catalogs("movie", only_working=True)
            self.assertEqual(len(search_cats), 1)
            self.assertEqual(search_cats[0]["catalog_id"], "top")
            self.assertEqual(search_cats[0]["addon_name"], "Cinemeta")

    def test_search_avoids_stream_only_addons(self):
        from src import api, database
        catalog_addon = {
            "id": "org.cinemeta",
            "name": "Cinemeta",
            "manifest_url": "https://online-catalog.com/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "resources": ["catalog"],
            "catalogs": [
                {"type": "movie", "id": "top", "name": "Top", "extra": ["search"]}
            ]
        }
        stream_addon = {
            "id": "org.torrentio",
            "name": "Torrentio",
            "manifest_url": "https://stream-addon.com/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "resources": ["stream"],
            "catalogs": []
        }

        queried_urls = []
        def mock_get_cached(url, **kwargs):
            queried_urls.append(url)
            if "search=" in url:
                return {"metas": [{"id": "tt123", "name": "Test Movie", "type": "movie"}]}
            return None

        with patch.object(database, 'get_addons', return_value=[catalog_addon, stream_addon]), \
             patch.object(database, 'get_addon_health', return_value=self.fake_db["settings"]["addon_health"]), \
             patch.object(api, '_get_cached_request', side_effect=mock_get_cached):
            
            results = api.fetch_items(media_type="movie", query="Test", cache_only=True)
            self.assertTrue(any("online-catalog.com" in u for u in queried_urls))
            self.assertFalse(any("stream-addon.com" in u for u in queried_urls))

    def test_stream_fetch_avoids_catalog_only_addons(self):
        from src import api, database
        catalog_addon = {
            "id": "org.cinemeta",
            "name": "Cinemeta",
            "manifest_url": "https://online-catalog.com/manifest.json",
            "enabled": True,
            "streams_enabled": True,
            "resources": ["catalog", "meta"],
            "catalogs": [{"type": "movie", "id": "top"}]
        }
        stream_addon = {
            "id": "org.torrentio",
            "name": "Torrentio",
            "manifest_url": "https://online-catalog.com/manifest.json",
            "enabled": True,
            "streams_enabled": True,
            "resources": ["stream"],
            "catalogs": []
        }

        with patch.object(database, 'get_addons', return_value=[catalog_addon, stream_addon]), \
             patch.object(database, 'get_addon_health', return_value=self.fake_db["settings"]["addon_health"]), \
             patch.object(database, 'get_cached_streams', return_value=None):
            
            stream_addons = api.get_stream_addons(media_type="movie", item_id="tt1234567", only_working=True)
            self.assertEqual(len(stream_addons), 1)
            self.assertEqual(stream_addons[0]["name"], "Torrentio")

if __name__ == '__main__':
    unittest.main()
