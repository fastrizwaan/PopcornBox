import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["POPCORN_TEST"] = "1"
sys.modules.setdefault("mpv", MagicMock())
sys.modules.setdefault("libtorrent", MagicMock())

_schema_dir = tempfile.TemporaryDirectory()
_schema_src = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_src.exists():
    (Path(_schema_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml").write_text(
        _schema_src.read_text()
    )
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

Adw.init()
_res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if _res_file.exists():
    try:
        Gio.resources_register(Gio.Resource.load(str(_res_file)))
    except Exception:
        pass

from src import api, database, plugins, tmdb_helper
from src.window import PopcornBoxWindow


class TestNuvioCatalogsRobustnessAndUI(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.orig_config_dir = database.CONFIG_DIR
        self.orig_db_file = database.DB_FILE
        database.CONFIG_DIR = Path(self.td.name)
        database.DB_FILE = Path(self.td.name) / "data.json"
        database.reset_json_cache()
        plugins.clear_scraper_cooldown()

    def tearDown(self):
        plugins.clear_scraper_cooldown()
        database.CONFIG_DIR = self.orig_config_dir
        database.DB_FILE = self.orig_db_file
        database.reset_json_cache()
        self.td.cleanup()

    def test_nuvio_get_catalogs_exposes_enabled_providers(self):
        scrapers = [
            {
                "id": "https://repo.test/manifest.json:allanime",
                "raw_id": "allanime",
                "name": "AllAnime",
                "repository_url": "https://repo.test/manifest.json",
                "supported_types": ["movie", "tv"],
                "content_language": ["en", "ja"],
                "enabled": True,
                "code": "module.exports = { getStreams: async () => [] };",
            },
            {
                "id": "https://repo.test/manifest.json:disabled_p",
                "raw_id": "disabled_p",
                "name": "DisabledProv",
                "repository_url": "https://repo.test/manifest.json",
                "supported_types": ["movie"],
                "enabled": False,
                "code": "module.exports = { getStreams: async () => [] };",
            },
        ]
        with patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_all_scrapers", return_value=scrapers):
            cats = plugins.get_catalogs("movie")
            names = {c["addon_name"] for c in cats}
            self.assertIn("AllAnime", names)
            self.assertNotIn("DisabledProv", names)
            self.assertTrue(all(c["manifest_url"].startswith("nuvio://catalog/") for c in cats))
            self.assertEqual(
                plugins.scraper_id_from_catalog_url(cats[0]["manifest_url"]),
                "https://repo.test/manifest.json:allanime",
            )

    def test_nuvio_fetch_catalog_items_legal_tmdb_fallback(self):
        scraper = {
            "id": "https://repo.test/manifest.json:desiflix",
            "name": "DesiFlix",
            "supported_types": ["movie"],
            "content_language": ["hi", "ta"],
            "enabled": True,
            "code": "module.exports = { getStreams: async () => [] };",
        }
        fake_tmdb_resp = {
            "results": [
                {
                    "id": 12345,
                    "title": "Sample Hindi Film",
                    "poster_path": "/poster.jpg",
                    "backdrop_path": "/bg.jpg",
                    "release_date": "2024-05-01",
                    "vote_average": 7.8,
                    "vote_count": 200,
                    "popularity": 99.0,
                    "overview": "A great movie.",
                    "genre_ids": [18],
                    "original_language": "hi",
                }
            ]
        }
        seen_urls = []

        def fake_get(url, **kwargs):
            seen_urls.append(url)
            return fake_tmdb_resp

        with patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_scraper", return_value=scraper), \
             patch.object(tmdb_helper, "_get_cached_request", side_effect=fake_get):
            items = api.fetch_items(
                media_type="movie",
                catalog_id="popular",
                catalog_url=plugins.catalog_url(scraper["id"]),
                page=1,
                limit=20,
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "tmdb:12345")
            self.assertEqual(items[0]["nuvio_provider_name"], "DesiFlix")
            self.assertTrue(any("with_original_language=hi%7Cta" in u for u in seen_urls))

    def test_nuvio_fetch_catalog_items_declared_sandbox_catalog(self):
        scraper = {
            "id": "https://repo.test/manifest.json:catprov",
            "name": "CatProv",
            "supported_types": ["movie"],
            "enabled": True,
            "catalogs": [{"id": "featured", "name": "Featured", "type": "movie"}],
            "code": "module.exports = { getCatalog: async () => [], getStreams: async () => [] };",
        }
        with patch.object(plugins, "is_plugins_enabled", return_value=True), \
             patch.object(plugins, "get_scraper", return_value=scraper), \
             patch.object(
                 plugins,
                 "execute_scraper_catalog",
                 return_value=[{"id": "tmdb:603", "name": "The Matrix", "poster": "https://img.test/m.jpg", "type": "movie"}],
             ):
            items = plugins.fetch_catalog_items(scraper["id"], catalog_id="featured", media_type="movie")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "tmdb:603")
            self.assertEqual(items[0]["title"], "The Matrix")

    def test_execute_scraper_guards_and_cooldown(self):
        scraper = {
            "id": "https://repo.test/manifest.json:broken",
            "name": "Broken",
            "code": "console.log('broken');",
        }
        # Invalid tmdb_id returns early without invoking subprocess
        with patch.object(plugins, "build_scraper_command", return_value=["deno", "run", "bundle.js"]), \
             patch("subprocess.Popen") as mock_popen:
            res = plugins.execute_scraper(scraper, None, "movie")
            self.assertEqual(res, [])
            mock_popen.assert_not_called()

        # Repeated crash places scraper in cooldown
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.communicate.return_value = ("", "crash")
        fake_proc.poll.return_value = 1
        with patch.object(plugins, "build_scraper_command", return_value=["deno", "run", "bundle.js"]), \
             patch("subprocess.Popen", return_value=fake_proc):
            plugins.execute_scraper(scraper, "603", "movie")
            self.assertFalse(plugins.is_scraper_in_cooldown(scraper["id"]))
            plugins.execute_scraper(scraper, "603", "movie")
            self.assertTrue(plugins.is_scraper_in_cooldown(scraper["id"]))

        plugins.clear_scraper_cooldown(scraper["id"])
        self.assertFalse(plugins.is_scraper_in_cooldown(scraper["id"]))

    def test_fetch_repository_data_bom_and_relative_path(self):
        manifest_json = "\ufeff" + json.dumps({
            "name": "Test Repo",
            "version": "1.2.0",
            "scrapers": [
                {"id": "prov1", "name": "Prov 1", "filename": "./providers/prov1.js", "enabled": True}
            ],
        })
        requested = []

        def fake_get_text(url, timeout=15.0):
            requested.append(url)
            if url.endswith("manifest.json"):
                return manifest_json
            return "module.exports = { getStreams: async () => [] };"

        with patch.object(plugins, "_http_get_text", side_effect=fake_get_text):
            repo_info, scrapers = plugins.fetch_repository_data("https://example.com/repo/manifest.json")
            self.assertEqual(repo_info["name"], "Test Repo")
            self.assertEqual(len(scrapers), 1)
            self.assertIn("https://example.com/repo/providers/prov1.js", requested)

    def test_merged_addons_plugins_ui_and_removed_cloudstream(self):
        import xml.etree.ElementTree as ET

        win_tree = ET.parse("src/window.ui")
        win_root = win_tree.getroot()
        win_ids = {el.attrib.get("id") for el in win_root.iter("object") if "id" in el.attrib}
        self.assertIn("page_plugins", win_ids)
        self.assertIn("page_addons_sub", win_ids)
        self.assertIn("page_nuvio_sub", win_ids)
        self.assertIn("page_custom_sub", win_ids)
        self.assertNotIn("page_cloudstream_sub", win_ids)

        def _page_title(root, page_id):
            for obj in root.iter("object"):
                if obj.attrib.get("id") == page_id:
                    for prop in obj.findall("property"):
                        if prop.attrib.get("name") == "title":
                            return prop.text
            return None

        self.assertEqual(_page_title(win_root, "page_addons_sub"), "Addons")
        self.assertEqual(_page_title(win_root, "page_nuvio_sub"), "Plugins")
        self.assertEqual(_page_title(win_root, "page_custom_sub"), "Custom")

        menu_el = next((m for m in win_root.iter("menu") if m.attrib.get("id") == "primary_menu"), None)
        self.assertIsNotNone(menu_el)
        menu_labels = []
        for item in menu_el.iter("item"):
            for attr in item.findall("attribute"):
                if attr.attrib.get("name") == "label":
                    menu_labels.append(attr.text)
        self.assertIn("Addons/Plugins", menu_labels)
        self.assertNotIn("Addons", menu_labels)
        self.assertNotIn("Plugins", menu_labels)

        prefs_tree = ET.parse("src/preferences.ui")
        prefs_ids = {el.attrib.get("id") for el in prefs_tree.getroot().iter("object") if "id" in el.attrib}
        self.assertIn("prefs_nuvio_plugins_row", prefs_ids)
        self.assertIn("prefs_custom_plugins_row", prefs_ids)
        self.assertNotIn("prefs_cloudstream_plugins_row", prefs_ids)

    def test_discover_catalog_dropdown_includes_nuvio_providers(self):
        fake_win = MagicMock()
        fake_win.catalog_dropdown = MagicMock()
        fake_win._syncing_discover_toolbar = False
        fake_cats = [
            {
                "addon_name": "AllAnime",
                "manifest_url": "nuvio://catalog/repo%3Aallanime",
                "catalog_id": "popular",
                "catalog_name": "Popular",
                "type": "movie",
                "_source": "nuvio_plugin",
            }
        ]
        with patch("src.api.database.get_addons", return_value=[]), \
             patch.object(plugins, "get_catalogs", return_value=fake_cats), \
             patch("src.custom_plugins.get_catalogs", return_value=[]):
            PopcornBoxWindow._populate_discover_catalog_dropdown(fake_win, "movie")
            display_names = [it["display_name"] for it in fake_win._discover_catalog_items]
            self.assertIn("AllAnime (Nuvio)", display_names)


if __name__ == "__main__":
    unittest.main()
