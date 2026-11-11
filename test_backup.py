import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

# Ensure testing flag is set before imports
os.environ["POPCORN_TEST"] = "1"

from src import database, cloudstream, backup


class TestBackupRestore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # Point database and cloudstream to temp directory
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_db_file = database.DB_FILE
        self.temp_config_dir = Path(self.temp_dir.name) / "config"
        self.temp_config_dir.mkdir(parents=True, exist_ok=True)
        database.CONFIG_DIR = self.temp_config_dir
        database.DB_FILE = self.temp_config_dir / "data.json"

        # Point packages dir to temp
        self.temp_pkg_dir = Path(self.temp_dir.name) / "packages"
        self.temp_pkg_dir.mkdir(parents=True, exist_ok=True)
        self.orig_pkg_dir_func = cloudstream.get_packages_dir
        cloudstream.get_packages_dir = lambda: self.temp_pkg_dir

    def tearDown(self):
        database.CONFIG_DIR = self.orig_config_dir
        database.DB_FILE = self.orig_db_file
        cloudstream.get_packages_dir = self.orig_pkg_dir_func

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

        sample_cs_repo = {
            "name": "CS Repo",
            "manifest_url": "https://cs.example.com/repo.json",
            "url": "https://cs.example.com/repo.json"
        }
        database.save_cloudstream_repository(sample_cs_repo)

        sample_cs_plugin = {
            "id": "cs_provider_1",
            "name": "CS Provider 1",
            "storage_key": "test_cs_pkg_123",
            "repository_manifest_url": "https://cs.example.com/repo.json",
            "is_installed": True,
            "enabled": True,
            "version": 10,
            "installed_version": 10,
        }
        database.save_cloudstream_plugins([sample_cs_plugin])

        # Create dummy .cs3 file
        dummy_pkg_file = self.temp_pkg_dir / "test_cs_pkg_123.cs3"
        dummy_pkg_file.write_bytes(b"DUMMY_CS3_CONTENT_FOR_BACKUP")

        # 2. Create ZIP backup
        backup_zip = Path(self.temp_dir.name) / "test_backup.zip"
        success, msg, summary = backup.create_backup(backup_zip)
        self.assertTrue(success, msg)
        self.assertTrue(backup_zip.is_file())
        self.assertEqual(summary["addons_count"], len(database.get_addons()))
        self.assertEqual(summary["nuvio_repos_count"], 1)
        self.assertEqual(summary["cloudstream_repos_count"], 1)
        self.assertEqual(summary["cloudstream_installed_count"], 1)

        # Inspect zip contents
        with zipfile.ZipFile(backup_zip, "r") as zf:
            self.assertIn("backup.json", zf.namelist())
            self.assertIn("cloudstream_packages/test_cs_pkg_123.cs3", zf.namelist())
            self.assertEqual(zf.read("cloudstream_packages/test_cs_pkg_123.cs3"), b"DUMMY_CS3_CONTENT_FOR_BACKUP")

        # 3. Clear database & packages to simulate fresh install
        database.set_setting("plugins_scraper_settings", {})
        database.remove_plugin_repository("https://nuvio.example.com/manifest.json")
        database.remove_cloudstream_repository("https://cs.example.com/repo.json")
        if dummy_pkg_file.exists():
            dummy_pkg_file.unlink()

        self.assertEqual(len(database.get_plugin_repositories()), 0)
        self.assertEqual(len(database.get_cloudstream_repositories()), 0)
        self.assertFalse(dummy_pkg_file.exists())

        # 4. Restore ZIP backup
        res_success, res_msg, res_summary = backup.restore_backup(backup_zip)
        self.assertTrue(res_success, res_msg)
        self.assertEqual(res_summary["nuvio_repos_count"], 1)
        self.assertEqual(res_summary["cloudstream_repos_count"], 1)
        self.assertEqual(res_summary["cloudstream_installed_count"], 1)
        self.assertEqual(res_summary["extracted_packages_count"], 1)

        # 5. Verify restored state
        restored_addons = [a for a in database.get_addons() if a.get("id") == "community.testaddon"]
        self.assertEqual(len(restored_addons), 1)

        restored_nuvio = database.get_plugin_repositories()
        self.assertEqual(len(restored_nuvio), 1)
        self.assertEqual(restored_nuvio[0]["manifest_url"], "https://nuvio.example.com/manifest.json")

        restored_scraper_settings = database.get_setting("plugins_scraper_settings", {})
        self.assertEqual(restored_scraper_settings.get("scraper1", {}).get("api_key"), "secret123")

        restored_cs_repos = database.get_cloudstream_repositories()
        self.assertEqual(len(restored_cs_repos), 1)
        self.assertEqual(restored_cs_repos[0]["manifest_url"], "https://cs.example.com/repo.json")

        restored_cs_plugins = database.get_cloudstream_plugins()
        self.assertEqual(len(restored_cs_plugins), 1)
        self.assertTrue(restored_cs_plugins[0].get("is_installed"))
        self.assertTrue(restored_cs_plugins[0].get("enabled"))

        # Verify .cs3 package was extracted
        self.assertTrue(dummy_pkg_file.is_file())
        self.assertEqual(dummy_pkg_file.read_bytes(), b"DUMMY_CS3_CONTENT_FOR_BACKUP")

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


if __name__ == "__main__":
    unittest.main()
