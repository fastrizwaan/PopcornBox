import unittest
import sys
import tempfile
import os
import json
import time
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
from src.window import CineWindow, _filter_items_from_available_addons


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
        api.reset_addon_health_cache()
        api.clear_provider_cooldown()
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

    def test_addon_declares_resource_ignores_health_and_toggles(self):
        addon = {
            "id": "test.disabled",
            "manifest_url": "https://disabled.example/manifest.json",
            "resources": ["catalog", "stream", "meta"],
            "enabled": False,
            "catalogs_enabled": False,
            "streams_enabled": False,
        }
        self.assertTrue(api.addon_declares_resource(addon, "catalog"))
        self.assertTrue(api.addon_declares_resource(addon, "stream"))
        self.assertFalse(api.addon_declares_resource(addon, "subtitles"))

        api.set_addon_online_status(addon["manifest_url"], False)
        self.assertTrue(api.addon_declares_resource(addon, "stream"))

        # Addon-directory entries only expose addon_catalog and are not media addons.
        self.assertFalse(api.addon_declares_resource({"resources": ["addon_catalog"]}, "catalog"))
        self.assertFalse(api.addon_declares_resource({"resources": ["addon_catalog"]}, "stream"))

        # Legacy addons without a resources list keep their inferred capabilities.
        legacy = {"catalogs": [{"type": "movie", "id": "top"}]}
        self.assertTrue(api.addon_declares_resource(legacy, "catalog"))
        self.assertTrue(api.addon_declares_resource(legacy, "stream"))

    def test_remove_addon_refreshes_ui_immediately(self):
        addon = {
            "id": "test.remove-now",
            "name": "Remove Now",
            "manifest_url": "https://remove-now.example/manifest.json",
            "resources": ["stream"],
            "types": ["movie"],
            "catalogs": [],
        }
        database.add_addon(dict(addon))
        api.set_addon_online_status(addon["manifest_url"], False)

        class MockWindow:
            def __init__(self):
                self._update_addon_catalog_dropdown = MagicMock()
                self._populate_addons = MagicMock()
                self._on_addons_changed = MagicMock()
                self._show_toast = MagicMock()

            _remove_addon = CineWindow._remove_addon

        win = MockWindow()
        win._remove_addon(addon)

        self.assertFalse(any(a["id"] == addon["id"] for a in database.get_addons()))
        win._update_addon_catalog_dropdown.assert_called_once()
        win._populate_addons.assert_called_once_with()
        win._on_addons_changed.assert_called_once()
        win._show_toast.assert_called_once()
        self.assertNotIn(addon["manifest_url"], api._ADDON_ONLINE_STATUS)

    def test_apply_addon_status_labels_online_and_offline(self):
        status_label = MagicMock()

        CineWindow._apply_addon_status(None, status_label, False, "Temporarily unavailable")
        status_label.set_label.assert_called_with("🔴")
        status_label.set_tooltip_text.assert_called_with("Temporarily unavailable")

        status_label.reset_mock()

        CineWindow._apply_addon_status(None, status_label, True, "Online")
        status_label.set_label.assert_called_with("🟢")
        status_label.set_tooltip_text.assert_called_with("Online")

    def test_apply_addon_health_results_refreshes_registered_rows(self):
        url = "https://row.example/manifest.json"
        status_label = MagicMock()
        win = MagicMock()
        win._addon_status_widgets = {url: status_label}

        CineWindow._apply_addon_health_results(win, {url: {"online": True}})
        win._apply_addon_status.assert_called_once_with(status_label, True, "Online")

        win._apply_addon_status.reset_mock()
        CineWindow._apply_addon_health_results(win, {url: {"online": False}})
        win._apply_addon_status.assert_called_once_with(status_label, False, "Offline")

        # Rows that are no longer on the page are ignored.
        win._apply_addon_status.reset_mock()
        CineWindow._apply_addon_health_results(win, {"https://gone.example/manifest.json": {"online": True}})
        win._apply_addon_status.assert_not_called()

    def test_filter_items_from_available_addons(self):
        database.add_addon({
            "id": "probe.enabled",
            "name": "Probe Enabled",
            "manifest_url": "https://enabled.example/manifest.json",
            "types": ["movie"],
            "catalogs": [],
        })
        database.add_addon({
            "id": "probe.disabled",
            "name": "Probe Disabled",
            "manifest_url": "https://disabled.example/manifest.json",
            "types": ["movie"],
            "catalogs": [],
        })
        database.set_addon_enabled("probe.disabled", False)

        items = [
            {"id": "tt1", "title": "Enabled source", "manifest_url": "https://enabled.example/manifest.json"},
            {"id": "tt2", "title": "Disabled source", "manifest_url": "https://disabled.example/manifest.json"},
            {"id": "tt3", "title": "Removed source", "manifest_url": "https://removed.example/manifest.json"},
            {"id": "tt4", "title": "Local file"},
            {"id": "tt5", "title": "Builtin catalog", "manifest_url": "builtin:popular"},
            "raw-non-dict-item",
        ]
        kept = _filter_items_from_available_addons(items)
        self.assertEqual(
            [i["id"] if isinstance(i, dict) else i for i in kept],
            ["tt1", "tt4", "tt5", "raw-non-dict-item"],
        )
        self.assertEqual(_filter_items_from_available_addons([]), [])
        self.assertEqual(_filter_items_from_available_addons(None), [])

    def test_set_addon_online_status_overrides_stale_health(self):
        url = "https://stale.example/manifest.json"
        database.update_addon_health(url, online=False, latency_ms=0, error="timeout")
        with api._ADDON_ONLINE_LOCK:
            api._IN_MEMORY_ADDON_HEALTH[url] = {
                "online": False,
                "last_checked": time.time(),
                "latency_ms": 0,
                "error": "timeout",
            }
        self.assertFalse(api.is_addon_online(url))

        # Re-enabling the addon is an explicit retry; it must clear the stale
        # offline verdict instead of staying hidden for the 2h health window.
        api.set_addon_online_status(url, True)

        self.assertTrue(api.is_addon_online(url))
        persisted = database.get_addon_health()[url]
        self.assertTrue(persisted["online"])
        self.assertLessEqual(abs(persisted["last_checked"] - time.time()), 60)

        api.set_addon_online_status(url, False)
        self.assertFalse(api.is_addon_online(url))

    def test_refresh_active_library_view_follows_visible_page(self):
        class MockWindow:
            def __init__(self, lib_page):
                self.library_stack = MagicMock()
                self.library_stack.get_visible_child_name.return_value = lib_page
                self.category_btn_stack = MagicMock()
                self.unique_check = MagicMock()
                self.unique_check.get_active.return_value = True
                self.library_unique_check = MagicMock()
                self.library_unique_check.get_active.return_value = True
                self._refresh_unique_combined_view = MagicMock()
                self._refresh_discover_page = MagicMock()
                self._addons_dirty = True
                self._current_discover_type = "all"
                self._current_discover_addon = None
                self.media_type_keys = ["all", "movie_series", "movie", "series"]
                self.anime_supported = False

            _refresh_active_library_view = CineWindow._refresh_active_library_view

        win = MockWindow("discover")
        win._refresh_active_library_view()
        # The persisted Unique checkbox must not hijack a visible Discover page.
        win._refresh_unique_combined_view.assert_not_called()
        win._refresh_discover_page.assert_called_once_with(
            filter_media_type="all", filter_addon_url=None, force_refresh=True
        )
        self.assertFalse(win._addons_dirty)

        win = MockWindow("unique")
        win._refresh_active_library_view()
        win._refresh_unique_combined_view.assert_called_once()
        win._refresh_discover_page.assert_not_called()
        self.assertFalse(win._addons_dirty)

    def test_on_addons_changed_suppresses_dropdown_reloads(self):
        class MockWindow:
            def __init__(self, prior):
                self._syncing_discover_toolbar = prior
                self._observed = {}
                self._discover_views = {}
                self._discover_catalog_list_cache = {}
                self._catalog_list_cache = {}
                self._built_menu_models = {}
                self._menus_built = True
                self._menus_building = False
                self._addons_dirty = False
                self.main_stack = MagicMock()
                self.main_stack.get_visible_child_name.return_value = "addons"
                self.details_box = None
                self._ensure_all_menus_built = MagicMock()
                self._prewarm_discover_views = MagicMock()
                self._refresh_active_library_view = MagicMock()

            def _update_supported_media_types(self):
                pass

            def _update_search_catalog_dropdown(self):
                self._observed["search"] = self._syncing_discover_toolbar

            def _update_catalog_dropdown(self):
                self._observed["catalog"] = self._syncing_discover_toolbar

            def _populate_discover_catalog_dropdown(self, cur_t):
                self._observed["discover_catalog"] = self._syncing_discover_toolbar

            def _get_current_discover_type(self):
                return "all"

            _on_addons_changed = CineWindow._on_addons_changed

        for prior in (False, True):
            win = MockWindow(prior)
            CineWindow._on_addons_changed(win)

            # Dropdown model rebuilds must not emit reload signals that switch
            # the hidden library to the Unique view while the user is elsewhere.
            self.assertTrue(win._observed["search"])
            self.assertTrue(win._observed["catalog"])
            self.assertTrue(win._observed["discover_catalog"])
            self.assertIs(win._syncing_discover_toolbar, prior)
            self.assertTrue(win._addons_dirty)
            win._refresh_active_library_view.assert_not_called()

    def test_continue_watching_row_filters_disabled_addons(self):
        url = "https://probe.cw.example/manifest.json"
        database.add_addon({
            "id": "probe.cw",
            "name": "Probe CW",
            "manifest_url": url,
            "types": ["movie"],
            "catalogs": [],
        })
        database.save_continue_watching({
            "id": "tt99001",
            "title": "Probe In Progress",
            "type": "movie",
            "manifest_url": url,
            "last_watched": 10,
        })
        database.save_continue_watching({
            "id": "tt99002",
            "title": "Local In Progress",
            "type": "movie",
            "last_watched": 20,
        })

        class FakeCard(Gtk.Box):
            def __init__(self, item, *args, **kwargs):
                super().__init__()
                self.item = item

        def build_window():
            win = MagicMock()
            win.category_btn_stack.get_visible_child_name.return_value = "home"
            target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            win.discover_box = target_box
            win._discover_views = {"home|all|all|all|all|all|Popular": {"box": target_box}}
            win._discover_row_card_metrics.return_value = (15, 130)
            win._discover_result_matches_item.return_value = True
            win._fit_discover_row_card = MagicMock()
            win._start_background_continue_watching_refresh = MagicMock()
            win._cw_initial_layout_check_scheduled = True
            return win, target_box

        def count_cards(target_box):
            child = target_box.get_first_child()
            scroll = None
            while child:
                if getattr(child, "_is_cw_scroll", False):
                    scroll = child
                    break
                child = child.get_next_sibling()
            self.assertIsNotNone(scroll)
            row = scroll.get_child()
            if hasattr(row, "get_child"):
                row = row.get_child()
            count = 0
            card = row.get_first_child()
            while card:
                count += 1
                card = card.get_next_sibling()
            return count

        with patch("src.movie_widget.ContinueWatchingWidget", FakeCard):
            win, target_box = build_window()
            CineWindow._update_continue_watching_section(win)
            self.assertEqual(count_cards(target_box), 2)

            database.set_addon_enabled("probe.cw", False)
            CineWindow._update_continue_watching_section(win)
            self.assertEqual(count_cards(target_box), 1)

    def test_local_continue_watching_page_filters_disabled_addons(self):
        url = "https://probe.local.example/manifest.json"
        database.add_addon({
            "id": "probe.local",
            "name": "Probe Local",
            "manifest_url": url,
            "types": ["movie"],
            "catalogs": [],
        })
        database.set_addon_enabled("probe.local", False)
        database.save_continue_watching({
            "id": "tt88001",
            "title": "Disabled Source",
            "type": "movie",
            "manifest_url": url,
            "last_watched": 10,
        })
        database.save_continue_watching({
            "id": "tt88002",
            "title": "Local Source",
            "type": "movie",
            "last_watched": 20,
        })

        class FakeCard(Gtk.Box):
            def __init__(self, item, *args, **kwargs):
                super().__init__()
                self.item = item

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win = MagicMock()
        win.continue_watching_box = container
        win.local_media_type_filter = "all"
        win.cw_search_entry = None

        with patch("src.movie_widget.ContinueWatchingWidget", FakeCard):
            CineWindow._populate_local_db_page(win, "continue_watching")

        flowbox = None
        child = container.get_first_child()
        while child:
            if isinstance(child, Gtk.FlowBox):
                flowbox = child
                break
            child = child.get_next_sibling()
        self.assertIsNotNone(flowbox)

        titles = []
        child = flowbox.get_first_child()
        while child:
            card = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            titles.append(card.item["title"])
            child = child.get_next_sibling()
        self.assertEqual(titles, ["Local Source"])

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
