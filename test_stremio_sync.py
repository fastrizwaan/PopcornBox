import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

class TestStremioSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"

        # Patch DB_FILE and CONFIG_DIR
        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

        from src import database
        database._json_cache_valid = False
        database._json_cache = None

    def tearDown(self):
        from src import database
        database.reset_json_cache()
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_iso_timestamp_conversion(self):
        from src import stremio_sync

        iso_str = "2026-10-13T01:30:00.000Z"
        epoch = stremio_sync.iso_to_epoch(iso_str)
        self.assertGreater(epoch, 0)

        converted_back = stremio_sync.epoch_to_iso(epoch)
        self.assertIn("2026-10-13", converted_back)
        self.assertTrue(converted_back.endswith(".000Z"))

        # Invalid/empty inputs
        self.assertEqual(stremio_sync.iso_to_epoch(None), 0.0)
        self.assertEqual(stremio_sync.iso_to_epoch(""), 0.0)
        self.assertEqual(stremio_sync.iso_to_epoch("invalid_date"), 0.0)

    @patch("src.stremio_sync._post_json")
    def test_login_success(self, mock_post):
        from src import stremio_sync, database

        mock_post.return_value = {
            "result": {
                "authKey": "mock_auth_token_12345",
                "user": {"_id": "u123", "email": "tester@example.com"}
            }
        }

        res = stremio_sync.login("tester@example.com", "secretpass")
        self.assertEqual(res["authKey"], "mock_auth_token_12345")
        self.assertEqual(res["email"], "tester@example.com")

        self.assertEqual(database.get_setting("stremio_auth_key"), "mock_auth_token_12345")
        self.assertEqual(database.get_setting("stremio_user_email"), "tester@example.com")
        self.assertTrue(database.get_setting("stremio_sync_enabled"))
        self.assertTrue(stremio_sync.is_authenticated())
        self.assertTrue(stremio_sync.is_sync_enabled())

    @patch("src.stremio_sync._post_json")
    def test_login_failure(self, mock_post):
        from src import stremio_sync

        mock_post.return_value = {
            "error": {"code": 3, "message": "Wrong passphrase"}
        }

        with self.assertRaises(stremio_sync.StremioAuthError) as ctx:
            stremio_sync.login("tester@example.com", "wrongpass")

        self.assertIn("Wrong passphrase", str(ctx.exception))

    @patch("src.stremio_sync._post_json")
    def test_login_with_auth_key(self, mock_post):
        from src import stremio_sync, database

        mock_post.return_value = {
            "result": {"_id": "u456", "email": "keyuser@example.com"}
        }

        res = stremio_sync.login_with_auth_key("custom_key_xyz")
        self.assertEqual(res["authKey"], "custom_key_xyz")
        self.assertEqual(res["email"], "keyuser@example.com")
        self.assertEqual(database.get_setting("stremio_auth_key"), "custom_key_xyz")

    @patch("src.stremio_sync._post_json")
    def test_logout(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "active_key")
        database.set_setting("stremio_user_email", "logout@example.com")
        database.set_setting("stremio_sync_enabled", True)
        database.set_setting("stremio_last_synced", time.time())

        stremio_sync.logout()

        self.assertIsNone(database.get_setting("stremio_auth_key"))
        self.assertIsNone(database.get_setting("stremio_user_email"))
        self.assertFalse(database.get_setting("stremio_sync_enabled"))
        self.assertIsNone(database.get_setting("stremio_last_synced"))
        self.assertFalse(stremio_sync.is_authenticated())

    def test_model_transformers(self):
        from src import stremio_sync

        # Remote -> Continue Watching item (Series)
        remote_series = {
            "_id": "tt0944947",
            "name": "Game of Thrones",
            "type": "series",
            "poster": "https://example.com/got.jpg",
            "year": "2011",
            "state": {
                "timeOffset": 1800,
                "duration": 3600,
                "video_id": "tt0944947:2:4",
                "lastWatched": "2026-10-13T02:00:00.000Z",
                "flaggedWatching": 1
            },
            "_mtime": "2026-10-13T02:00:00.000Z",
            "temp": False
        }

        cw = stremio_sync.remote_to_continue_watching_item(remote_series)
        self.assertEqual(cw["id"], "tt0944947")
        self.assertEqual(cw["season"], 2)
        self.assertEqual(cw["episode"], 4)
        self.assertEqual(cw["position"], 1800.0)
        self.assertEqual(cw["duration"], 3600.0)
        self.assertEqual(cw["progress"], 0.5)
        self.assertEqual(cw["stream_title"], "Game of Thrones (S02E04)")

        # Local CW -> Stremio libraryItem
        local_cw = {
            "id": "tt1375666",
            "title": "Inception",
            "type": "movie",
            "poster": "https://example.com/inception.jpg",
            "year": "2010",
            "position": 5000.0,
            "duration": 8880.0,
            "last_watched": 1760317200
        }

        remote_movie = stremio_sync.local_to_stremio_library_item(local_cw, is_favorite=True, in_continue_watching=True)
        self.assertEqual(remote_movie["_id"], "tt1375666")
        self.assertEqual(remote_movie["name"], "Inception")
        self.assertEqual(remote_movie["state"]["timeOffset"], 5000)
        self.assertEqual(remote_movie["state"]["duration"], 8880)
        self.assertEqual(remote_movie["state"]["flaggedWatching"], 1)
        self.assertFalse(remote_movie["temp"])  # Favorite, so temp is False

    @patch("src.stremio_sync._post_json")
    def test_two_way_sync_library(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "test_auth_key")
        database.set_setting("stremio_sync_enabled", True)

        # Local state initially:
        # 1. Local favorite: Interstellar (not on remote)
        # 2. Local continue watching: Breaking Bad S01E01 (older timestamp)
        database.add_favorite({
            "id": "tt0816692",
            "title": "Interstellar",
            "type": "movie",
            "poster": "https://example.com/interstellar.jpg",
            "year": "2014"
        }, sync_to_remote=False)

        database.save_continue_watching({
            "id": "tt0903747",
            "title": "Breaking Bad",
            "type": "series",
            "season": 1,
            "episode": 1,
            "position": 600.0,
            "duration": 3480.0,
            "last_watched": 1000  # Very old timestamp
        }, sync_to_remote=False)

        # Remote datastore returns:
        # 1. Remote Breaking Bad S01E02 with newer timestamp (should overwrite local CW)
        # 2. Remote favorite: Succession (temp=False, should be added to local favorites)
        mock_remote_library = [
            {
                "_id": "tt0903747",
                "name": "Breaking Bad",
                "type": "series",
                "poster": "https://example.com/bb.jpg",
                "year": "2008",
                "state": {
                    "timeOffset": 1500,
                    "duration": 3480,
                    "video_id": "tt0903747:1:2",
                    "lastWatched": "2026-10-13T05:00:00.000Z",
                    "flaggedWatching": 1
                },
                "_mtime": "2026-10-13T05:00:00.000Z",
                "temp": True
            },
            {
                "_id": "tt7660850",
                "name": "Succession",
                "type": "series",
                "poster": "https://example.com/succession.jpg",
                "year": "2018",
                "state": {
                    "timeOffset": 0,
                    "duration": 0,
                    "flaggedWatching": 0
                },
                "_mtime": "2026-10-13T05:00:00.000Z",
                "temp": False
            }
        ]

        def fake_post_json(url, payload, timeout=8.0):
            if "datastoreGet" in url:
                return {"result": mock_remote_library}
            elif "datastorePut" in url:
                # Capture pushed changes
                self.assertIn("changes", payload)
                return {"result": {"success": True}}
            return {}

        mock_post.side_effect = fake_post_json

        summary = stremio_sync.sync_library()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["pulled_cw"], 1)  # Breaking Bad updated
        self.assertEqual(summary["pulled_fav"], 1)  # Succession added

        # Verify local Continue Watching updated to Episode 2
        cw = database.get_continue_watching()
        bb_entry = next(c for c in cw if c["id"] == "tt0903747")
        self.assertEqual(bb_entry["episode"], 2)
        self.assertEqual(bb_entry["position"], 1500.0)

        # Verify Succession was added to local favorites
        self.assertTrue(database.is_favorite("tt7660850"))
        # Verify Interstellar remains in local favorites
        self.assertTrue(database.is_favorite("tt0816692"))

    @patch("src.stremio_sync._post_json")
    def test_scrobble_playback(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "scrobble_token")
        database.set_setting("stremio_sync_enabled", True)

        scrobble_item = {
            "id": "tt1375666",
            "title": "Inception",
            "type": "movie",
            "position": 3200.0,
            "duration": 8880.0,
            "last_watched": int(time.time())
        }

        stremio_sync.scrobble_playback(scrobble_item)
        # Give daemon thread a brief moment to execute
        time.sleep(0.05)

        self.assertTrue(mock_post.called)
        call_args = mock_post.call_args[0]
        self.assertIn("datastorePut", call_args[0])
        payload = call_args[1]
        self.assertEqual(payload["authKey"], "scrobble_token")
        self.assertEqual(payload["changes"][0]["_id"], "tt1375666")
        self.assertEqual(payload["changes"][0]["state"]["timeOffset"], 3200)

    @patch("src.stremio_sync._post_json")
    def test_sync_addons(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "addon_token")
        database.set_setting("stremio_sync_addons_enabled", True)

        # 1. Local addon present: IPTV Channels
        database.add_addon({
            "id": "local-addon-1",
            "name": "Custom Local Addon",
            "version": "1.0.0",
            "manifest_url": "https://local-custom.com/manifest.json",
            "enabled": True,
            "resources": ["stream"],
            "types": ["movie"]
        }, sync_to_remote=False)

        # 2. Remote addons: Cinemeta and Torrentio
        remote_addons_data = [
            {
                "transportUrl": "https://v3-cinemeta.strem.io/manifest.json",
                "transportName": "http",
                "manifest": {
                    "id": "cinemeta",
                    "name": "Cinemeta",
                    "version": "3.0.0",
                    "resources": ["catalog", "meta"],
                    "types": ["movie", "series"]
                },
                "flags": {"protected": True}
            },
            {
                "transportUrl": "https://torrentio.strem.fun/manifest.json",
                "transportName": "http",
                "manifest": {
                    "id": "torrentio",
                    "name": "Torrentio",
                    "version": "1.0.14",
                    "resources": ["stream"],
                    "types": ["movie", "series"]
                },
                "flags": {}
            }
        ]

        pushed_payloads = []

        def fake_post_json(url, payload, timeout=8.0):
            if "addonCollectionGet" in url:
                return {"result": remote_addons_data}
            elif "addonCollectionSet" in url:
                pushed_payloads.append(payload)
                return {"result": {"success": True}}
            return {}

        mock_post.side_effect = fake_post_json

        summary = stremio_sync.sync_addons()
        self.assertEqual(summary["status"], "success")
        # Torrentio was pulled
        self.assertEqual(summary["pulled_addons"], 1)
        # Custom local addon was pushed
        self.assertGreater(summary["pushed_addons"], 0)

        # Verify Torrentio is installed in local database
        installed = database.get_addons()
        installed_ids = [a.get("id") for a in installed]
        self.assertIn("torrentio", installed_ids)

        # Verify pushed payload includes local-addon-1
        self.assertTrue(len(pushed_payloads) > 0)
        pushed_urls = [a.get("transportUrl") for a in pushed_payloads[0]["addons"]]
        self.assertIn("https://local-custom.com/manifest.json", pushed_urls)
        self.assertIn("https://torrentio.strem.fun/manifest.json", pushed_urls)

    def test_sanitize_stremio_manifest(self):
        from src import stremio_sync

        # Malformed manifest: missing types, resources, catalogs, has internal keys
        raw = {
            "id": "com.test.addon",
            "name": "Test Addon",
            "version": None,
            "manifest_url": "https://test.com/manifest.json",
            "enabled": True,
            "catalogs_enabled": True,
            "types": None,
            "resources": None,
            "catalogs": None
        }

        clean = stremio_sync.sanitize_stremio_manifest(raw)
        self.assertEqual(clean["id"], "com.test.addon")
        self.assertEqual(clean["name"], "Test Addon")
        self.assertEqual(clean["version"], "1.0.0")
        self.assertEqual(clean["catalogs"], [])
        self.assertEqual(clean["resources"], ["stream"])
        self.assertIn("movie", clean["types"])
        self.assertIn("series", clean["types"])
        self.assertNotIn("manifest_url", clean)
        self.assertNotIn("enabled", clean)
        self.assertNotIn("catalogs_enabled", clean)

        # Derives types from catalogs if available
        cat_raw = {
            "id": "com.test.cat",
            "name": "Catalog Addon",
            "catalogs": [{"type": "anime", "id": "top_anime", "name": "Top Anime"}],
            "types": []
        }
        clean_cat = stremio_sync.sanitize_stremio_manifest(cat_raw)
        self.assertEqual(clean_cat["types"], ["anime"])
        self.assertEqual(clean_cat["resources"], ["catalog", "meta"])

    @patch("src.stremio_sync._post_json")
    def test_sync_addons_dict_response_and_healing(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "heal_token")
        database.set_setting("stremio_sync_addons_enabled", True)

        # Remote returns standard Stremio dict structure {"result": {"addons": [...], "lastModified": "..."}}
        # with an addon that has types=None (the exact bug that crashed Stremio)
        remote_data = {
            "result": {
                "addons": [
                    {
                        "transportUrl": "https://elfhosted.com/manifest.json",
                        "transportName": "http",
                        "manifest": {
                            "id": "stremio.addons.mediafusiondev",
                            "name": "MediaFusion DEV",
                            "version": "4.2.14",
                            "types": None,  # Buggy None value
                            "resources": ["catalog"],
                            "catalogs": [{"type": "movie", "id": "mf_mov", "name": "MF Movies"}]
                        },
                        "flags": {"protected": False}
                    }
                ],
                "lastModified": "2026-10-13T07:00:00.000Z"
            }
        }

        pushed_payloads = []

        def fake_post_json(url, payload, timeout=8.0):
            if "addonCollectionGet" in url:
                return remote_data
            elif "addonCollectionSet" in url:
                pushed_payloads.append(payload)
                return {"result": {"success": True}}
            return {}

        mock_post.side_effect = fake_post_json

        summary = stremio_sync.sync_addons()
        self.assertEqual(summary["status"], "success")

        # Verify that because MediaFusion DEV had types=None, it was healed and re-pushed via addonCollectionSet
        self.assertTrue(len(pushed_payloads) > 0)
        pushed_addons = pushed_payloads[0]["addons"]
        mf = next(a for a in pushed_addons if a["transportUrl"] == "https://elfhosted.com/manifest.json")
        self.assertIsNotNone(mf["manifest"].get("types"))
        self.assertIsInstance(mf["manifest"]["types"], list)
        self.assertEqual(mf["manifest"]["types"], ["movie"])

    @patch("src.stremio_sync._post_json")
    def test_sync_addons_user_removed(self, mock_post):
        from src import stremio_sync, database

        database.set_setting("stremio_auth_key", "remove_token")
        database.set_setting("stremio_sync_addons_enabled", True)

        # Mark an addon as removed locally
        database.remove_addon("annoying_addon", manifest_url="https://annoying.com/manifest.json", sync_to_remote=False)

        # Remote still has annoying_addon
        remote_data = {
            "result": {
                "addons": [
                    {
                        "transportUrl": "https://annoying.com/manifest.json",
                        "manifest": {
                            "id": "annoying_addon",
                            "name": "Annoying Addon",
                            "types": ["movie"],
                            "resources": ["stream"],
                            "catalogs": []
                        }
                    }
                ]
            }
        }

        pushed_payloads = []

        def fake_post_json(url, payload, timeout=8.0):
            if "addonCollectionGet" in url:
                return remote_data
            elif "addonCollectionSet" in url:
                pushed_payloads.append(payload)
                return {"result": {"success": True}}
            return {}

        mock_post.side_effect = fake_post_json

        summary = stremio_sync.sync_addons()
        self.assertEqual(summary["status"], "success")

        # Annoying Addon should NOT be installed in local database
        installed_ids = [a.get("id") for a in database.get_addons()]
        self.assertNotIn("annoying_addon", installed_ids)

        # And it should have triggered a push to delete it from remote
        self.assertTrue(len(pushed_payloads) > 0)
        pushed_urls = [a.get("transportUrl") for a in pushed_payloads[0]["addons"]]
        self.assertNotIn("https://annoying.com/manifest.json", pushed_urls)

if __name__ == "__main__":
    unittest.main()
