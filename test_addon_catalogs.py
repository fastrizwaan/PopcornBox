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


class TestAddonCatalogs(unittest.TestCase):

    def test_stremio_addons_manifest_not_in_media_types(self):
        """stremio-addons.net only has addon_catalog resource and must NOT leak into media types (movies/series/discover)."""
        stremio_addons_manifest = {
            "id": "community.stremio.stremio-addons-dot-net",
            "name": "stremio-addons.net",
            "description": "Provides the Community Stremio Addons catalog from stremio-addons.net.",
            "types": ["movie", "series", "channel", "tv"],
            "resources": ["addon_catalog"],
            "catalogs": [],
            "addonCatalogs": [
                {
                    "type": "all",
                    "id": "stremio-addons.net",
                    "name": "stremio-addons.net"
                }
            ],
            "enabled": True
        }

        # has_catalog_resource must be False because its only resource is addon_catalog
        self.assertFalse(api.has_catalog_resource(stremio_addons_manifest))
        self.assertFalse(api.has_stream_resource(stremio_addons_manifest))
        self.assertFalse(api.has_meta_resource(stremio_addons_manifest))

        # Test _update_supported_media_types ignores this addon
        win = CineWindow.__new__(CineWindow)
        win.media_type_keys = []
        win.media_type_labels = []

        with patch("src.database.get_addons", return_value=[stremio_addons_manifest]):
            win._update_supported_media_types()
            # Since no media addons exist, tv channels / custom types must NOT be added
            self.assertFalse(getattr(win, "tv_supported", False))
            self.assertFalse(getattr(win, "anime_supported", False))

    def test_get_addon_directory_catalogs_includes_stremio_addons(self):
        """get_addon_directory_catalogs returns Official, Community, STREMIO-ADDONS.NET, and Installed."""
        catalogs = api.get_addon_directory_catalogs()
        cat_ids = [c["id"] for c in catalogs]
        self.assertIn("community", cat_ids)
        self.assertIn("official", cat_ids)
        self.assertIn("stremio-addons.net", cat_ids)
        self.assertIn("installed", cat_ids)

        # STREMIO-ADDONS.NET URL points to the addon_catalog endpoint
        stremio_net = next(c for c in catalogs if c["id"] == "stremio-addons.net")
        self.assertEqual(stremio_net["url"], "https://stremio-addons.net/api/addon_catalog/all/stremio-addons.net.json")
        self.assertEqual(stremio_net["manifest_url"], "https://stremio-addons.net/api/manifest.json")

    def test_fetch_addon_directory_catalog_parsing(self):
        """fetch_addon_directory_catalog properly parses normalized addon entries."""
        sample_catalog_data = {
            "addons": [
                {
                    "transportUrl": "https://example.com/kkphim/manifest.json",
                    "manifest": {
                        "id": "org.kkphim.stremio",
                        "name": "KKPhim",
                        "version": "1.0.0",
                        "description": "Xem phim tu KKPhim tren Stremio",
                        "types": ["movie", "series"],
                        "resources": ["catalog", "meta", "stream"]
                    }
                },
                {
                    "transportUrl": "https://example.com/subsync/manifest.json",
                    "manifest": {
                        "id": "community.subsync",
                        "name": "SubSync",
                        "version": "0.2.0",
                        "description": "Fixes subtitles that drift out of sync",
                        "types": ["movie", "series"],
                        "resources": ["subtitles"]
                    }
                }
            ]
        }

        with patch("src.api._get_cached_request", return_value=sample_catalog_data):
            results = api.fetch_addon_directory_catalog("https://example.com/addon_catalog.json")
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["name"], "KKPhim")
            self.assertEqual(results[0]["transport_url"], "https://example.com/kkphim/manifest.json")
            self.assertEqual(results[0]["types"], ["movie", "series"])
            self.assertEqual(results[1]["name"], "SubSync")

    def test_addon_filter_func_matching(self):
        """_addon_filter_func correctly filters catalog rows by search text and media type."""
        win = CineWindow.__new__(CineWindow)
        win._addon_types_list = [
            ("all", "All"),
            ("movie", "Movies"),
            ("series", "Series"),
            ("anime", "Anime"),
            ("subtitles", "Subtitles"),
            ("other", "Other"),
        ]

        mock_search = MagicMock()
        mock_search.get_text.return_value = ""
        win.addon_search_entry = mock_search

        mock_type_dropdown = MagicMock()
        mock_type_dropdown.get_selected.return_value = 0  # "all"
        win.addon_type_dropdown = mock_type_dropdown

        # Create mock catalog row
        row = MagicMock()
        row._is_catalog_row = True
        row._addon_name = "heroes"
        row._addon_desc = "tokusatsu universe kamen rider"
        row._addon_types = ["heroes"]
        row._addon_id = "community.heroes.tokusatsu"
        row._addon_data = {"resources": ["catalog", "meta", "stream"]}

        # All matches
        self.assertTrue(win._addon_filter_func(row))

        # Search match
        mock_search.get_text.return_value = "kamen"
        self.assertTrue(win._addon_filter_func(row))

        # Search non-match
        mock_search.get_text.return_value = "nonexistent"
        self.assertFalse(win._addon_filter_func(row))

        # Type filter test
        mock_search.get_text.return_value = ""
        mock_type_dropdown.get_selected.return_value = 1  # "movie"
        self.assertFalse(win._addon_filter_func(row))  # "heroes" type is not movie

        mock_type_dropdown.get_selected.return_value = 5  # "other"
        self.assertTrue(win._addon_filter_func(row))  # "heroes" is other

    def test_load_remote_image_import_and_signature(self):
        """load_remote_image can be imported from movie_widget and supports both argument orders."""
        from src.movie_widget import load_remote_image, load_image_into_picture
        self.assertTrue(callable(load_remote_image))
        self.assertTrue(callable(load_image_into_picture))

        # Test load_remote_image handles both argument orders
        mock_pic = MagicMock()
        with patch("src.movie_widget.load_image_into_picture") as mock_load:
            load_remote_image(mock_pic, "https://example.com/logo.png", width=56, height=56)
            mock_load.assert_called_with(
                url="https://example.com/logo.png",
                picture_widget=mock_pic,
                width=56,
                height=56,
                on_error=None,
                is_priority=False,
                crop=True
            )

        with patch("src.movie_widget.load_image_into_picture") as mock_load:
            load_remote_image("https://example.com/logo.png", mock_pic, width=56, height=56)
            mock_load.assert_called_with(
                url="https://example.com/logo.png",
                picture_widget=mock_pic,
                width=56,
                height=56,
                on_error=None,
                is_priority=False,
                crop=True
            )


if __name__ == "__main__":
    unittest.main()


