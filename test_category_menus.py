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
        
        self.movies_active_btn = MockMenuButton()
        self.series_active_btn = MockMenuButton()
        self.anime_active_btn = MockMenuButton()
        self.anime_supported = True

    from src.window import CineWindow
    _clean_cat_name = CineWindow._clean_cat_name
    _prepare_menu_data = CineWindow._prepare_menu_data
    _build_addon_submenu = CineWindow._build_addon_submenu
    _build_full_menu_model = CineWindow._build_full_menu_model
    _init_default_category_menus = CineWindow._init_default_category_menus


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


if __name__ == "__main__":
    unittest.main()
