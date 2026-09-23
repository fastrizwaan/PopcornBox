import unittest
import sys
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    import subprocess
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database, api
from src.window import CineWindow


class TestAddonUpdates(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "data.json"
        
        # Patch DB path in database module
        self.patch_db = patch.object(database, "DB_FILE", self.db_file)
        self.patch_db.start()
        
        # Reset memory cache & locks in database
        database._json_cache = None
        database._json_cache_valid = False
        database._ensure_db()

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.patch_db.stop()
        self.temp_dir.cleanup()

    def test_normalize_addon_url(self):
        # 1. stremio:// protocol conversion
        url = CineWindow._normalize_addon_url(None, "stremio://v3-cinemeta.strem.io/manifest.json")
        self.assertEqual(url, "https://v3-cinemeta.strem.io/manifest.json")

        # 2. stremio:// without /manifest.json
        url = CineWindow._normalize_addon_url(None, "stremio://v3-cinemeta.strem.io")
        self.assertEqual(url, "https://v3-cinemeta.strem.io/manifest.json")

        # 3. Missing scheme defaults to https:// and appends manifest.json
        url = CineWindow._normalize_addon_url(None, "torrentio.strem.fun")
        self.assertEqual(url, "https://torrentio.strem.fun/manifest.json")

        # 4. http:// URL preserved
        url = CineWindow._normalize_addon_url(None, "http://localhost:7000/manifest.json")
        self.assertEqual(url, "http://localhost:7000/manifest.json")

        # 5. Empty or whitespace
        self.assertEqual(CineWindow._normalize_addon_url(None, ""), "")
        self.assertEqual(CineWindow._normalize_addon_url(None, "   "), "")

    def test_add_and_remove_addon_database(self):
        # Add a custom addon
        custom_addon = {
            "id": "custom.myaddon",
            "name": "My Custom Addon",
            "manifest_url": "https://example.com/manifest.json",
            "types": ["movie", "series"],
            "catalogs": []
        }
        database.add_addon(custom_addon)
        addons = database.get_addons()
        self.assertTrue(any(a["id"] == "custom.myaddon" for a in addons))

        # Remove custom addon by ID
        database.remove_addon(addon_id="custom.myaddon")
        addons = database.get_addons()
        self.assertFalse(any(a["id"] == "custom.myaddon" for a in addons))

        # Remove addon by manifest_url
        database.add_addon(custom_addon)
        addons = database.get_addons()
        self.assertTrue(any(a["id"] == "custom.myaddon" for a in addons))
        database.remove_addon(manifest_url="https://example.com/manifest.json")
        addons = database.get_addons()
        self.assertFalse(any(a["id"] == "custom.myaddon" for a in addons))

    def test_default_addons_not_resurrected_after_removal(self):
        # By default, Cinemeta should be present
        addons = database.get_addons()
        self.assertTrue(any(a["id"] == "cinemeta" for a in addons))

        # Remove Cinemeta
        database.remove_addon(addon_id="cinemeta")
        addons_after = database.get_addons()
        self.assertFalse(any(a["id"] == "cinemeta" for a in addons_after))

        # Verify removed_default_addons setting recorded it
        removed = database.get_setting("removed_default_addons", [])
        self.assertIn("cinemeta", removed)

        # Re-reading database (as on application restart) should NOT re-add cinemeta
        database._json_cache = None
        database._json_cache_valid = False
        reloaded_addons = database.get_addons()
        self.assertFalse(any(a["id"] == "cinemeta" for a in reloaded_addons))

        # If user explicitly installs Cinemeta again, it should be removed from removed_default_addons
        cinemeta_obj = {
            "id": "cinemeta",
            "name": "Cinemeta",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
            "types": ["movie", "series"],
            "catalogs": []
        }
        database.add_addon(cinemeta_obj)
        removed_after_readd = database.get_setting("removed_default_addons", [])
        self.assertNotIn("cinemeta", removed_after_readd)
        self.assertTrue(any(a["id"] == "cinemeta" for a in database.get_addons()))

    def test_cache_invalidation_on_addon_mutation(self):
        # Cache dummy streams in database
        database.save_cached_streams("tt12345", [{"name": "stream1"}])
        self.assertIsNotNone(database.get_cached_streams("tt12345"))

        # Invalidate catalogs cache spy
        with patch.object(api, "invalidate_catalogs_cache") as mock_inval:
            database.add_addon({
                "id": "test.addon",
                "name": "Test",
                "manifest_url": "https://test.addon/manifest.json",
                "types": ["movie"],
                "catalogs": []
            })
            mock_inval.assert_called()
            # Stream cache must be cleared immediately
            self.assertIsNone(database.get_cached_streams("tt12345"))

        # Test on remove_addon as well
        database.save_cached_streams("tt12345", [{"name": "stream2"}])
        with patch.object(api, "invalidate_catalogs_cache") as mock_inval:
            database.remove_addon(addon_id="test.addon")
            mock_inval.assert_called()
            self.assertIsNone(database.get_cached_streams("tt12345"))

    def test_on_addons_changed_flow(self):
        class MockWindow:
            def __init__(self):
                self._discover_views = {"cached": True}
                self._discover_catalog_list_cache = {"cached": True}
                self._catalog_list_cache = {"cached": True}
                self._built_menu_models = {"movie": True}
                self._menus_built = True
                self._menus_building = False
                self._addons_dirty = False
                self.main_stack = MagicMock()
                self.main_stack.get_visible_child_name.return_value = "library"
                self.library_stack = MagicMock()
                self.library_stack.get_visible_child_name.return_value = "discover"
                self.category_btn_stack = MagicMock()
                self.media_type_dropdown = MagicMock()
                self.media_type_dropdown.get_selected.return_value = 0
                self.search_catalog_dropdown = MagicMock()
                self.search_catalog_dropdown.get_selected.return_value = 0
                self.catalog_dropdown = MagicMock()
                self.catalog_dropdown.get_selected.return_value = 0

            _update_supported_media_types = CineWindow._update_supported_media_types
            _update_search_catalog_dropdown = CineWindow._update_search_catalog_dropdown
            _update_catalog_dropdown = CineWindow._update_catalog_dropdown
            _refresh_active_library_view = CineWindow._refresh_active_library_view
            _on_addons_changed = CineWindow._on_addons_changed
            _ensure_all_menus_built = MagicMock()
            _prewarm_discover_views = MagicMock()
            _refresh_discover_page = MagicMock()

        win = MockWindow()
        win._on_addons_changed()

        # Cache should be cleared/refreshed
        self.assertEqual(len(win._discover_views), 0)
        self.assertEqual(len(win._discover_catalog_list_cache), 0)
        self.assertNotIn("cached", win._catalog_list_cache)
        self.assertEqual(len(win._built_menu_models), 0)
        # Menus rebuild and discover prewarm triggered
        win._ensure_all_menus_built.assert_called_once()
        win._prewarm_discover_views.assert_called_once()
        # Discover page refreshed immediately
        win._refresh_discover_page.assert_called_once()


if __name__ == "__main__":
    unittest.main()
