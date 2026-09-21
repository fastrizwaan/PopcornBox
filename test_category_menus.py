import unittest
import sys
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
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


class MockMenuButton:
    def __init__(self):
        self._model = None
        self._active = False
        self._callbacks = {}
        self.popup_called = False

    def get_menu_model(self):
        return self._model

    def set_menu_model(self, model):
        self._model = model

    def get_active(self):
        return self._active

    def set_active(self, active):
        self._active = active
        for cb in list(self._callbacks.get("notify::active", [])):
            cb(self, None)

    def popup(self):
        self.popup_called = True
        self.set_active(True)

    def connect(self, signal, callback):
        self._callbacks.setdefault(signal, []).append(callback)

    def disconnect_by_func(self, callback):
        if "notify::active" in self._callbacks and callback in self._callbacks["notify::active"]:
            self._callbacks["notify::active"].remove(callback)


class DummyWindow:
    def __init__(self):
        self._built_menu_models = {}
        self._menus_built = False
        self._menus_building = False
        
        self.discover_active_btn = MockMenuButton()
        self.movies_active_btn = MockMenuButton()
        self.series_active_btn = MockMenuButton()
        self.anime_active_btn = MockMenuButton()
        self.anime_supported = True
        self.media_type_keys = ["movie", "series"]
        self.media_type_labels = ["Movies", "Series"]

    from src.window import CineWindow
    _clean_cat_name = CineWindow._clean_cat_name
    _prepare_menu_data = CineWindow._prepare_menu_data
    _prepare_discover_catalog_data = CineWindow._prepare_discover_catalog_data
    _build_discover_menu = CineWindow._build_discover_menu
    _build_addon_submenu = CineWindow._build_addon_submenu
    _build_full_menu_model = CineWindow._build_full_menu_model
    _init_default_category_menus = CineWindow._init_default_category_menus
    _get_discover_catalog_list = CineWindow._get_discover_catalog_list


class TestCategoryMenus(unittest.TestCase):
    def test_init_default_category_menus(self):
        win = DummyWindow()
        win._build_discover_menu = MagicMock()
        win._schedule_deferred_menu_build = MagicMock()
        
        win._init_default_category_menus()
        
        # Verify placeholder models are assigned
        for btn in [win.movies_active_btn, win.series_active_btn, win.anime_active_btn]:
            self.assertIsNotNone(btn.get_menu_model())
            # Initial placeholder has 1 item ("★ All ...")
            self.assertEqual(btn.get_menu_model().get_n_items(), 1)
            # Verify NO notify::active listener is attached (no on-click population)
            self.assertNotIn("notify::active", btn._callbacks)
        win._schedule_deferred_menu_build.assert_called_once_with(500)

    def test_streamlined_menu_structure(self):
        """Verify menus do not contain nested genre submenus (catalogs are direct items)."""
        win = DummyWindow()
        addon_cat_items = [
            {"name": "Popular", "target": "movie|https://addon/manifest.json|top|All", "cat_id": "top"},
            {"name": "Featured", "target": "movie|https://addon/manifest.json|featured|All", "cat_id": "featured"}
        ]
        addon_menu = win._build_addon_submenu("TestAddon", "https://addon/manifest.json", "movie", addon_cat_items)
        
        # Addon menu should have:
        # 1. "★ All TestAddon Movies"
        # 2. "Popular"
        # 3. "Featured"
        self.assertEqual(addon_menu.get_n_items(), 3)
        
        # Full model
        full_model = win._build_full_menu_model("movie", [("TestAddon", "https://addon/manifest.json", addon_cat_items)])
        # Full model has:
        # 1. "★ All Movies (All Catalogs)"
        # 2. "TestAddon" (submenu)
        self.assertEqual(full_model.get_n_items(), 2)

    def test_pre_attach_inactive_buttons(self):
        """Simulate apply_all attach_step on inactive buttons."""
        win = DummyWindow()
        btn = win.movies_active_btn
        self.assertFalse(btn.get_active())
        
        new_model = Gio.Menu.new()
        new_model.append("Item 1", "app.item1")
        
        # In apply_all:
        if not btn.get_active():
            btn.set_menu_model(new_model)
            
        self.assertEqual(btn.get_menu_model(), new_model)
        # When user clicks, button is already loaded!
        self.assertEqual(btn.get_menu_model().get_n_items(), 1)

    def test_pre_attach_when_popover_active_defers_to_close(self):
        """If popover happens to be active when apply_all runs, defer until closed."""
        win = DummyWindow()
        btn = win.series_active_btn
        btn.set_active(True) # currently popped up
        
        initial_model = Gio.Menu.new()
        btn.set_menu_model(initial_model)
        
        new_model = Gio.Menu.new()
        new_model.append("Full Menu Item", "app.full")
        
        # Replicate apply_all logic for active button
        if btn.get_active():
            def on_deactivate(b, pspec):
                if not b.get_active():
                    b.disconnect_by_func(on_deactivate)
                    b.set_menu_model(new_model)
            btn.connect("notify::active", on_deactivate)
        
        # Model should NOT change while active
        self.assertEqual(btn.get_menu_model(), initial_model)
        
        # User closes popover
        btn.set_active(False)
        # Now model is smoothly updated
        self.assertEqual(btn.get_menu_model(), new_model)

    def test_open_catalog_grid_does_not_clobber_with_cinemeta(self):
        """Verify _open_catalog_grid preserves the selected catalog and does not reset to Cinemeta."""
        win = MagicMock()
        win.media_type_keys = ["movie", "series"]
        win._catalog_list_cache = {}
        win._syncing_dropdowns = False

        cinemeta_cat = {"catalog_id": "top", "manifest_url": "https://cinemeta/manifest.json", "display_name": "Cinemeta - Popular"}
        tmdb_cat = {"catalog_id": "tmdb.top", "manifest_url": "https://tmdb/manifest.json", "display_name": "TMDB - Popular"}
        available = [cinemeta_cat, tmdb_cat]

        from src.window import CineWindow
        with patch("src.api.get_available_catalogs", return_value=available):
            CineWindow._open_catalog_grid(win, "movie", tmdb_cat, "TMDB - Popular")

        # Must be TMDB, not Cinemeta
        self.assertEqual(win.current_catalog["catalog_id"], "tmdb.top")
        self.assertEqual(win.current_catalog["manifest_url"], "https://tmdb/manifest.json")
        win.catalog_dropdown.set_selected.assert_called_with(1)
        win.discover_grid_title.set_text.assert_called_with("TMDB - Popular")
        win._refresh_content.assert_called_once()

    def test_discover_menu_placeholder_structure(self):
        """Verify Discover menu places 'Catalog' submenu directly below 'All Types'."""
        win = DummyWindow()
        menu = win._build_discover_menu()
        self.assertIsNotNone(menu)
        # Structure:
        # 0: "All Types"
        # 1: "Catalog" (submenu)
        # 2: "Movies"
        # 3: "Series"
        self.assertEqual(menu.get_n_items(), 4)
        
        # Item 0: All Types
        self.assertEqual(menu.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "All Types")
        self.assertEqual(menu.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_ACTION).get_string(), "win.select-discover-type")
        self.assertEqual(menu.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_TARGET).get_string(), "all")

        # Item 1: Catalog submenu
        self.assertEqual(menu.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "Catalog")
        sub = menu.get_item_link(1, Gio.MENU_LINK_SUBMENU)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.get_n_items(), 1)
        self.assertEqual(sub.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "★ All Catalogs")

        # Items 2 and 3: Media types
        self.assertEqual(menu.get_item_attribute_value(2, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "Movies")
        self.assertEqual(menu.get_item_attribute_value(3, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "Series")

    def test_discover_menu_with_catalog_addons(self):
        """Verify Discover menu with catalog addons populates the Catalog submenu with correct action/target."""
        win = DummyWindow()
        catalog_addons = [
            ("Cinemeta", "https://v3-cinemeta.strem.io/manifest.json"),
            ("Anime Kitsu", "https://anime-kitsu.strem.fun/manifest.json")
        ]
        menu = win._build_discover_menu(catalog_addons=catalog_addons)
        self.assertIsNotNone(menu)
        self.assertEqual(menu.get_n_items(), 4)

        # Inspect Catalog submenu
        sub = menu.get_item_link(1, Gio.MENU_LINK_SUBMENU)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.get_n_items(), 2)

        # Cinemeta item
        self.assertEqual(sub.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "Cinemeta")
        self.assertEqual(sub.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_ACTION).get_string(), "win.select-addon-discover")
        self.assertEqual(
            sub.get_item_attribute_value(0, Gio.MENU_ATTRIBUTE_TARGET).get_string(),
            "all|https://v3-cinemeta.strem.io/manifest.json|Cinemeta"
        )

        # Anime Kitsu item
        self.assertEqual(sub.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_LABEL).get_string(), "Anime Kitsu")
        self.assertEqual(sub.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_ACTION).get_string(), "win.select-addon-discover")
        self.assertEqual(
            sub.get_item_attribute_value(1, Gio.MENU_ATTRIBUTE_TARGET).get_string(),
            "all|https://anime-kitsu.strem.fun/manifest.json|Anime Kitsu"
        )

    def test_discover_catalog_row_titles_multi_type(self):
        """Verify row titles for multi-type catalogs include media type when filter_media_type is 'all'."""
        win = DummyWindow()
        m_url = "https://mock-addon/manifest.json"
        mock_addon = {
            "name": "MockAddon",
            "manifest_url": m_url,
            "enabled": True,
            "catalogs": [
                {"id": "popular", "name": "Popular", "type": "movie"},
                {"id": "popular", "name": "Popular", "type": "series"},
            ]
        }
        with patch("src.database.get_addons", return_value=[mock_addon]), \
             patch("src.api.is_addon_online", return_value=True), \
             patch("src.api.has_catalog_resource", return_value=True), \
             patch("src.api.get_addon_catalogs", return_value=mock_addon["catalogs"]), \
             patch("src.api.is_catalog_browsable", return_value=True):
            rows = win._get_discover_catalog_list(filter_media_type="all", filter_addon_url=m_url)

        self.assertEqual(len(rows), 2)
        titles = [r["title"] for r in rows]
        self.assertIn("Popular - Movies", titles)
        self.assertIn("Popular - Series", titles)


if __name__ == "__main__":
    unittest.main()
