import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

# Ensure testing flag is set before imports
os.environ["POPCORN_TEST"] = "1"

from src import database, backup


class TestBackupRestore(unittest.TestCase):
    def setUp(self):
        database.reset_json_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # Point database to temp directory
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_db_file = database.DB_FILE
        self.temp_config_dir = Path(self.temp_dir.name) / "config"
        self.temp_config_dir.mkdir(parents=True, exist_ok=True)
        database.CONFIG_DIR = self.temp_config_dir
        database.DB_FILE = self.temp_config_dir / "data.json"
        database.reset_json_cache()


    def tearDown(self):
        database.reset_json_cache()
        database.CONFIG_DIR = self.orig_config_dir
        database.DB_FILE = self.orig_db_file
        database.reset_json_cache()

    def test_backup_and_restore_zip(self):
        # 1. Setup sample data
        sample_addon = {
            "id": "community.testaddon",
            "name": "Test Stremio Addon",
            "version": "1.0.0",
            "manifest_url": "https://example.com/manifest.json",
            "resources": ["stream"],
            "types": ["movie", "series"],
            "catalogs": []
        }
        database.add_addon(sample_addon, sync_to_remote=False)

        sample_nuvio_repo = {
            "name": "Nuvio Repo",
            "manifest_url": "https://nuvio.example.com/manifest.json"
        }
        database.save_plugin_repository(sample_nuvio_repo)

        sample_scraper = {
            "id": "scraper1",
            "name": "Test Scraper",
            "repository_url": "https://nuvio.example.com/manifest.json",
            "enabled": True,
            "code": "console.log('hi')"
        }
        database.save_plugin_scrapers([sample_scraper])
        database.set_setting("plugins_scraper_settings", {"scraper1": {"api_key": "secret123"}})

        # 2. Create ZIP backup
        backup_zip = Path(self.temp_dir.name) / "test_backup.zip"
        success, msg, summary = backup.create_backup(backup_zip)
        self.assertTrue(success, msg)
        self.assertTrue(backup_zip.is_file())
        self.assertEqual(summary["addons_count"], len(database.get_addons()))
        self.assertEqual(summary["nuvio_repos_count"], 1)
        self.assertNotIn("cloudstream_repos_count", summary)

        # Inspect zip contents
        with zipfile.ZipFile(backup_zip, "r") as zf:
            self.assertIn("backup.json", zf.namelist())

        # 3. Clear database to simulate fresh install
        database.set_setting("plugins_scraper_settings", {})
        database.remove_plugin_repository("https://nuvio.example.com/manifest.json")

        self.assertEqual(len(database.get_plugin_repositories()), 0)

        # 4. Restore ZIP backup
        res_success, res_msg, res_summary = backup.restore_backup(backup_zip)
        self.assertTrue(res_success, res_msg)
        self.assertEqual(res_summary["nuvio_repos_count"], 1)

        # 5. Verify restored state
        restored_addons = [a for a in database.get_addons() if a.get("id") == "community.testaddon"]
        self.assertEqual(len(restored_addons), 1)

        restored_nuvio = database.get_plugin_repositories()
        self.assertEqual(len(restored_nuvio), 1)
        self.assertEqual(restored_nuvio[0]["manifest_url"], "https://nuvio.example.com/manifest.json")

        restored_scraper_settings = database.get_setting("plugins_scraper_settings", {})
        self.assertEqual(restored_scraper_settings.get("scraper1", {}).get("api_key"), "secret123")


    def test_backup_and_restore_json(self):
        sample_addon = {
            "id": "community.jsonaddon",
            "name": "JSON Addon",
            "manifest_url": "https://example.com/json_manifest.json",
        }
        database.add_addon(sample_addon, sync_to_remote=False)

        backup_json = Path(self.temp_dir.name) / "test_backup.json"
        success, msg, summary = backup.create_backup(backup_json)
        self.assertTrue(success, msg)
        self.assertTrue(backup_json.is_file())

        with open(backup_json) as f:
            data = json.load(f)
        self.assertEqual(data.get("format"), "popcornbox_backup")

        # Test restoring JSON
        res_success, res_msg, res_summary = backup.restore_backup(backup_json)
        self.assertTrue(res_success, res_msg)
        restored = [a for a in database.get_addons() if a.get("id") == "community.jsonaddon"]
        self.assertEqual(len(restored), 1)

    def test_backup_and_restore_library_data(self):
        # 1. Setup sample library data: Favorites, Watched, History, Continue Watching, Downloads
        sample_fav = {
            "id": "tt1000001",
            "imdb_id": "tt1000001",
            "title": "Favorite Movie",
            "type": "movie",
            "poster": "https://example.com/fav.jpg"
        }
        database.add_favorite(sample_fav, sync_to_remote=False)

        sample_watched = {
            "id": "tt1000002",
            "imdb_id": "tt1000002",
            "title": "Watched Movie",
            "type": "movie",
        }
        database.add_watched(sample_watched)

        sample_hist1 = {
            "id": "tt1000003",
            "imdb_id": "tt1000003",
            "title": "History Show 1",
            "type": "series",
            "last_watched": 1700000000
        }
        sample_hist2 = {
            "id": "tt1000004",
            "imdb_id": "tt1000004",
            "title": "History Show 2",
            "type": "series",
            "last_watched": 1700001000
        }
        database.add_history(sample_hist1, sync_to_remote=False)
        database.add_history(sample_hist2, sync_to_remote=False)

        sample_cw = {
            "id": "tt1000005",
            "imdb_id": "tt1000005",
            "title": "In Progress Series",
            "type": "series",
            "season": 2,
            "episode": 3,
            "position": 540.5,
            "duration": 1800.0,
            "progress": 0.3,
            "last_watched": 1700002000
        }
        database.save_continue_watching(sample_cw, sync_to_remote=False)

        # Add a dismissed continue watching item
        database.add_continue_watching_dismissals_batch(["tt1000006"])

        # Add a download
        database.add_download(
            info_hash="aabbccddeeff00112233",
            name="Sample Download Video",
            magnet="magnet:?xt=urn:btih:aabbccddeeff00112233",
            item_id="tt1000007",
            status="finished",
            total_bytes=1048576,
            downloaded_bytes=1048576
        )

        # Set playback preferences in settings
        database.save_playback_preferences(
            item_id="tt1000005",
            season=2,
            episode=3,
            stream_info={"title": "1080p Torrent", "url": "magnet:?xt=urn:btih:fake123"},
            settings_values={"last_s_tt1000005": 2, "last_ep_tt1000005_2": 3}
        )
        database.set_setting("audio_languages", "eng,fra")

        # 2. Create backup
        backup_zip = Path(self.temp_dir.name) / "test_library_backup.zip"
        success, msg, summary = backup.create_backup(backup_zip)
        self.assertTrue(success, msg)
        self.assertTrue(backup_zip.is_file())

        self.assertEqual(summary["favorites_count"], 1)
        self.assertEqual(summary["watched_count"], 1)
        self.assertEqual(summary["history_count"], 2)
        self.assertEqual(summary["continue_watching_count"], 1)
        self.assertEqual(summary["downloads_count"], 1)

        # 3. Wipe current database
        database.clear_history()
        database.clear_downloads()
        with database._db_lock:
            db = database._read_db()
            db["favorites"] = []
            db["watched"] = []
            db["continue_watching"] = []
            db["user_dismissed_continue_watching"] = []
            db["settings"] = {}
            database._write_db(db)

        self.assertEqual(len(database.get_favorites()), 0)
        self.assertEqual(len(database.get_watched()), 0)
        self.assertEqual(len(database.get_history()), 0)
        self.assertEqual(len(database.get_continue_watching()), 0)
        self.assertEqual(len(database.get_downloads()), 0)
        self.assertFalse(database.is_favorite("tt1000001"))
        self.assertFalse(database.is_watched("tt1000002"))

        # 4. Restore backup
        res_success, res_msg, res_summary = backup.restore_backup(backup_zip)
        self.assertTrue(res_success, res_msg)
        self.assertEqual(res_summary["favorites_count"], 1)
        self.assertEqual(res_summary["watched_count"], 1)
        self.assertEqual(res_summary["history_count"], 2)
        self.assertEqual(res_summary["continue_watching_count"], 1)
        self.assertEqual(res_summary["downloads_count"], 1)

        # 5. Verify all restored items and state
        self.assertTrue(database.is_favorite("tt1000001"))
        restored_favs = database.get_favorites()
        self.assertEqual(len(restored_favs), 1)
        self.assertEqual(restored_favs[0]["title"], "Favorite Movie")

        self.assertTrue(database.is_watched("tt1000002"))
        restored_watched = database.get_watched()
        self.assertEqual(len(restored_watched), 1)
        self.assertEqual(restored_watched[0]["title"], "Watched Movie")

        restored_hist = database.get_history()
        self.assertEqual(len(restored_hist), 2)
        # Should be ordered by last_watched descending
        self.assertEqual(restored_hist[0]["id"], "tt1000004")
        self.assertEqual(restored_hist[1]["id"], "tt1000003")

        restored_cw = database.get_continue_watching()
        self.assertEqual(len(restored_cw), 1)
        self.assertEqual(restored_cw[0]["id"], "tt1000005")
        self.assertEqual(restored_cw[0]["season"], 2)
        self.assertEqual(restored_cw[0]["episode"], 3)
        self.assertAlmostEqual(restored_cw[0]["progress"], 0.3)
        self.assertAlmostEqual(restored_cw[0]["position"], 540.5)

        dismissals = database.get_continue_watching_dismissals()
        self.assertIn("tt1000006", dismissals)

        restored_downloads = database.get_downloads()
        self.assertEqual(len(restored_downloads), 1)
        self.assertEqual(restored_downloads[0]["info_hash"], "aabbccddeeff00112233")

        # Verify playback settings restored
        self.assertEqual(database.get_setting("last_s_tt1000005"), 2)
        self.assertEqual(database.get_setting("last_ep_tt1000005_2"), 3)
        self.assertEqual(database.get_setting("audio_languages"), "eng,fra")

    def test_restore_merges_without_duplicates(self):
        # Setup existing local items
        existing_fav = {"id": "tt2000001", "title": "Existing Fav", "type": "movie"}
        database.add_favorite(existing_fav, sync_to_remote=False)

        existing_cw = {"id": "tt2000002", "title": "Local CW", "position": 100.0, "last_watched": 500}
        database.save_continue_watching(existing_cw, sync_to_remote=False)

        # Create a backup containing a duplicate of tt2000001, a new favorite tt2000003,
        # and an updated continue watching for tt2000002 with newer last_watched
        backup_payload = {
            "format": "popcornbox_backup",
            "version": 2,
            "addons": [],
            "favorites": [
                {"id": "tt2000001", "title": "Existing Fav (backup duplicate)", "type": "movie"},
                {"id": "tt2000003", "title": "New Fav from Backup", "type": "movie"}
            ],
            "continue_watching": [
                {"id": "tt2000002", "title": "Local CW Updated", "position": 250.0, "last_watched": 900}
            ],
            "settings": {}
        }
        test_json = Path(self.temp_dir.name) / "test_merge.json"
        test_json.write_text(json.dumps(backup_payload))

        res_success, res_msg, res_summary = backup.restore_backup(test_json)
        self.assertTrue(res_success)

        favs = database.get_favorites()
        fav_ids = [f["id"] for f in favs]
        self.assertIn("tt2000001", fav_ids)
        self.assertIn("tt2000003", fav_ids)
        self.assertEqual(len(favs), 2)  # No duplicates!

        cw = database.get_continue_watching()
        self.assertEqual(len(cw), 1)
        self.assertEqual(cw[0]["id"], "tt2000002")
        self.assertEqual(cw[0]["position"], 250.0)  # Updated to newer position

    def test_legacy_backup_restore_compatibility(self):
        # Test restoring a backup from older version that lacked library keys
        legacy_payload = {
            "format": "popcornbox_backup",
            "version": 1,
            "addons": [{"id": "addon.legacy", "name": "Legacy Addon", "manifest_url": "https://example.com/m.json"}],
            "settings": {"stream_provider_timeout": 30}
        }
        legacy_json = Path(self.temp_dir.name) / "test_legacy.json"
        legacy_json.write_text(json.dumps(legacy_payload))

        success, msg, summary = backup.restore_backup(legacy_json)
        self.assertTrue(success)
        self.assertEqual(summary["addons_count"], 1)
        self.assertEqual(summary["favorites_count"], 0)
        self.assertEqual(summary["continue_watching_count"], 0)
        self.assertEqual(database.get_setting("stream_provider_timeout"), 30)


    def test_restore_ignores_legacy_cloudstream_backup_data(self):
        """Backups made before CloudStream support was removed still restore; their CloudStream data is skipped."""
        legacy_payload = {
            "addons": [],
            "cloudstream": {
                "repositories": [{"name": "CS Repo", "manifest_url": "https://cs.example.com/repo.json"}],
                "plugins": [{"id": "cs_provider_1", "storage_key": "cs_pkg", "is_installed": True}],
            },
        }
        legacy_zip = Path(self.temp_dir.name) / "legacy_backup.zip"
        with zipfile.ZipFile(legacy_zip, "w") as zf:
            zf.writestr("backup.json", json.dumps(legacy_payload))
            zf.writestr("cloudstream_packages/cs_pkg.cs3", b"LEGACY_PACKAGE")

        success, msg, summary = backup.restore_backup(legacy_zip)
        self.assertTrue(success, msg)
        self.assertNotIn("cloudstream_repos_count", summary)
        self.assertFalse(hasattr(database, "get_cloudstream_repositories"))

if __name__ == "__main__":
    unittest.main()
