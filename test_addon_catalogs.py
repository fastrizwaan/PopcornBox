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


    def test_catalog_item_with_missing_title_hydrated_from_cache(self):
        """When an item in a catalog has no name/title, it is hydrated from metadata_cache."""
        # Pre-seed metadata_cache
        database.save_cached_metadata("tt27165187", "movie", {
            "id": "tt27165187",
            "title": "The End of Oak Street",
            "year": "2026",
            "medium_cover_image": "https://example.com/poster.jpg"
        })

        # Test fetch_items hydration
        raw_addon_data = {
            "metas": [
                {"id": "tt27165187", "type": "movie"}  # No name, title, or poster
            ]
        }
        with patch("src.api._get_cached_request", return_value=raw_addon_data):
            items = api.fetch_items(catalog_url="https://example.com/manifest.json", catalog_id="top")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "The End of Oak Street")
            self.assertEqual(items[0]["year"], "2026")
            self.assertEqual(items[0]["medium_cover_image"], "https://example.com/poster.jpg")

    def test_movie_widget_resolves_missing_title_dynamically(self):
        """MovieWidget initializes with cached title if available, or updates dynamically via _apply_resolved_meta."""
        from src.movie_widget import MovieWidget
        database.save_cached_metadata("tt27165187", "movie", {
            "id": "tt27165187",
            "title": "The End of Oak Street",
            "year": "2026",
            "medium_cover_image": "https://example.com/poster.jpg"
        })

        # Item initially without title
        item_data = {"id": "tt27165187", "type": "movie"}
        widget = MovieWidget(item_data, lambda *a: None)
        # Should be hydrated immediately on init
        self.assertEqual(item_data.get("title"), "The End of Oak Street")
        self.assertEqual(widget.title_label.get_label(), "The End of Oak Street")
        self.assertIsNotNone(widget.year_label)
        self.assertEqual(widget.year_label.get_label(), "2026")

        # Test _apply_resolved_meta dynamic update
        fresh_item = {"id": "tt99999999", "type": "movie"}
        fresh_widget = MovieWidget(fresh_item, lambda *a: None)
        self.assertEqual(fresh_widget.title_label.get_label(), "Unknown")
        fresh_widget._apply_resolved_meta("Dynamic Movie", "2027", "https://example.com/dyn.jpg")
        self.assertEqual(fresh_widget.title_label.get_label(), "Dynamic Movie")
        self.assertEqual(fresh_item.get("title"), "Dynamic Movie")
        self.assertEqual(fresh_item.get("year"), "2027")

    def test_fallback_poster_generation_cancellation(self):
        """fetch_fallback_poster respects task_gen and aborts immediately if generation is stale."""
        from src.movie_widget import fetch_fallback_poster, cancel_pending_image_downloads, _IMAGE_GENERATION_ID
        callback_called = []
        def on_resolved(title, year, poster):
            callback_called.append((title, year, poster))

        # Save an inactive gen_id
        stale_gen = _IMAGE_GENERATION_ID
        cancel_pending_image_downloads()  # increments _IMAGE_GENERATION_ID

        # Call with stale_gen
        fetch_fallback_poster("tt88888888", "movie", None, on_meta_resolved=on_resolved, task_gen=stale_gen)
        # Should not resolve or call callbacks
        self.assertEqual(len(callback_called), 0)

    def test_hydrate_missing_catalog_items_fast_cache_pass(self):
        """hydrate_missing_catalog_items updates items in-place instantly from metadata_cache."""
        database.save_cached_metadata("tt77777777", "movie", {
            "id": "tt77777777",
            "title": "Instant Movie",
            "year": "2028",
            "medium_cover_image": "https://example.com/instant.jpg"
        })
        items = [{"id": "tt77777777", "type": "movie", "title": ""}]
        updated = api.hydrate_missing_catalog_items(items, media_type="movie")
        self.assertTrue(updated)
        self.assertEqual(items[0]["title"], "Instant Movie")
        self.assertEqual(items[0]["year"], "2028")
        self.assertEqual(items[0]["medium_cover_image"], "https://example.com/instant.jpg")

    def test_catalog_cache_never_stores_or_returns_empty(self):
        """Empty lists must not be persisted to catalog_cache, and existing empty entries return None."""
        key = "test_empty_catalog_key"
        database.save_cached_catalog(key, [])
        self.assertIsNone(database.get_cached_catalog(key))

        # Test valid list is saved and retrieved
        database.save_cached_catalog(key, [{"id": "tt123", "title": "Test"}])
        cached = database.get_cached_catalog(key)
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 1)

    def test_fetch_items_resolves_case_insensitive_catalog_type(self):
        """fetch_items matches catalog types regardless of casing (e.g. 101genres vs 101Genres)."""
        mock_addon = {
            "manifest_url": "https://example.com/101/manifest.json",
            "catalogs": [
                {"id": "top_genres", "name": "Top", "type": "101Genres"},
                {"id": "top_genres", "name": "Top", "type": "movie"}
            ]
        }
        with patch("src.database.get_addons", return_value=[mock_addon]):
            with patch("src.api._get_cached_request") as mock_req:
                mock_req.return_value = {"metas": [{"id": "tt999", "name": "Catalog Item"}]}
                items = api.fetch_items(
                    media_type="101genres",
                    catalog_id="top_genres",
                    catalog_url="https://example.com/101/manifest.json"
                )
                self.assertIsNotNone(items)
                self.assertEqual(len(items), 1)
                # Verify the URL was constructed with the manifest's casing "101Genres"
                called_url = mock_req.call_args[0][0]
                self.assertIn("/catalog/101Genres/top_genres.json", called_url)


if __name__ == "__main__":
    unittest.main()



