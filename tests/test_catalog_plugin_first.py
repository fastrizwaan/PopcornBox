"""The opened custom catalog's own plugin is asked first and listed on top."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    (Path(_schema_dir.name) / _schema_path.name).write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

from src import api


class _CatalogPlugin:
    id = "custom.cinejoy"
    name = "Cinejoy"


ADDON = {
    "name": "BigAddon",
    "manifest_url": "https://big.addon/manifest.json",
    "resources": [{"name": "stream", "types": ["movie"]}],
    "types": ["movie"],
    "idPrefixes": ["tt"],
    "enabled": True,
}


class TestCatalogPluginFirst(unittest.TestCase):
    def _run(self, **kwargs):
        events = []
        plugin = _CatalogPlugin()

        def fake_custom(scraper, imdb_id=None, **kw):
            events.append(("plugin", imdb_id))
            return [{"name": "Cinejoy 720p", "title": "Heart 720p", "url": "https://cj.example/v/0a1b2c3d4e5f.m3u8",
                     "is_http": True, "is_custom_plugin": True, "addon_name": "Cinejoy", "addon_names": ["Cinejoy"]}]

        def fake_req(addon, url, **kw):
            events.append(("addon", url))
            return [{"name": "2160p", "title": "Heart.2026.2160p", "url": "https://big.example/v/Heart.2026.2160p.mkv", "is_http": True}]

        def callback(streams, is_cached=False, is_complete=False):
            events.append(("callback", [s.get("addon_names", [""])[0] for s in streams], is_complete))

        with patch("src.api.database.get_addons", return_value=[ADDON]), \
             patch("src.api.database.get_cached_streams", return_value=None), \
             patch("src.plugins.is_plugins_enabled", return_value=False), \
             patch("src.tmdb_helper.resolve_all_provider_ids", return_value=[]), \
             patch("src.tmdb_helper.resolve_to_tmdb_id", return_value=None), \
             patch("src.api.database.save_cached_streams_full", side_effect=lambda *a, **k: a[3]), \
             patch.object(api, "fetch_items", return_value=[]), \
             patch("src.custom_plugins.is_custom_plugins_enabled", return_value=True), \
             patch("src.custom_plugins.get_enabled_scrapers_for_type", return_value=[plugin]), \
             patch("src.custom_plugins.execute_custom_scraper", side_effect=fake_custom), \
             patch("src.api._request_addon_streams", side_effect=fake_req):
            results = api.get_torrents_streamed("tt7654321", media_type="movie", title="Heart",
                                                force_refresh=True, callback=callback, **kwargs)
        return events, results

    def test_imdb_id_card_from_custom_catalog(self):
        events, results = self._run(catalog_plugin_id="custom.cinejoy")
        kinds = [e[0] for e in events]
        # The plugin runs, and its streams reach the UI, before the addon is asked.
        self.assertEqual(kinds[0], "plugin")
        self.assertLess(kinds.index("callback"), kinds.index("addon"))
        self.assertEqual(events[kinds.index("callback")][1], ["Cinejoy"])
        # Asked once only, and ranked first despite the addon's better quality.
        self.assertEqual(kinds.count("plugin"), 1)
        self.assertEqual(results[0]["addon_names"][0], "Cinejoy")
        self.assertTrue(results[0]["from_opened_catalog"])
        self.assertEqual(len(results), 2)

    def test_plain_open_does_not_promote_plugin(self):
        events, results = self._run()
        self.assertFalse(any(s.get("from_opened_catalog") for s in results))


if __name__ == "__main__":
    unittest.main()
