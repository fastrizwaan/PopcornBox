import unittest
from unittest.mock import MagicMock, patch
import json

from src import api, database


class TestCatalogOriginAndAdultFiltering(unittest.TestCase):
    def setUp(self):
        # Reset DB addons in-memory mock or test harness
        self.mock_addons = [
            {
                "id": "torrentclaw",
                "name": "TorrentClaw",
                "manifest_url": "https://torrentclaw.com/api/stremio/manifest.json",
                "enabled": True,
                "catalogs_enabled": True,
                "streams_enabled": True,
                "resources": [
                    {"name": "catalog", "types": ["movie", "series"]},
                    {"name": "stream", "types": ["movie", "series"]}
                ],
                "catalogs": [
                    {"id": "tc-recent", "name": "Recent", "type": "movie"},
                    {"id": "tc-popular", "name": "Popular", "type": "movie"}
                ]
            },
            {
                "id": "cinemeta",
                "name": "Cinemeta",
                "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
                "enabled": True,
                "catalogs_enabled": True,
                "streams_enabled": True,
                "resources": [
                    {"name": "catalog", "types": ["movie", "series"]},
                    {"name": "meta", "types": ["movie", "series"]}
                ],
                "catalogs": [
                    {"id": "top", "name": "Popular", "type": "movie"}
                ]
            }
        ]

    def test_addon_independent_catalog_and_stream_toggles(self):
        """Test disabling catalogs independently from streams and vice versa."""
        addon = dict(self.mock_addons[0])
        self.assertTrue(api.has_catalog_resource(addon))
        self.assertTrue(api.has_stream_resource(addon))

        # Disable only catalogs
        addon["catalogs_enabled"] = False
        addon["streams_enabled"] = True
        self.assertFalse(api.has_catalog_resource(addon))
        self.assertTrue(api.has_stream_resource(addon))
        self.assertEqual(api.get_addon_catalogs(addon), [])

        # Disable only streams
        addon["catalogs_enabled"] = True
        addon["streams_enabled"] = False
        self.assertTrue(api.has_catalog_resource(addon))
        self.assertFalse(api.has_stream_resource(addon))
        self.assertNotEqual(api.get_addon_catalogs(addon), [])

        # Disable both
        addon["catalogs_enabled"] = False
        addon["streams_enabled"] = False
        self.assertFalse(api.has_catalog_resource(addon))
        self.assertFalse(api.has_stream_resource(addon))

    def test_adult_detection_keywords_and_certifications(self):
        """Test detection of explicit/adult titles, keywords, and certifications."""
        # Keyword matches
        self.assertTrue(api.is_adult_item({"name": "Perverted Stories 9"}))
        self.assertTrue(api.is_adult_item({"title": "Perfectly Trimmed Bush"}))
        self.assertTrue(api.is_adult_item({"name": "The Porn Queens"}))
        self.assertTrue(api.is_adult_item({"name": "Redtastic: Porn girls collection"}))
        self.assertTrue(api.is_adult_item({"name": "Muttsuri Do Sukebe Ro Gibo Shimai no Honshitsu Minuite Sex Zanmai"}))
        self.assertTrue(api.is_adult_item({"name": "The Sexual Torture Chamber of Doctor Josefel"}))
        self.assertTrue(api.is_adult_item({"name": "Shishunki Sex"}))
        self.assertTrue(api.is_adult_item({"name": "Asses Galore 3: Pure Evil"}))
        self.assertTrue(api.is_adult_item({"name": "Bikini Car Wash Massacre"}))
        self.assertTrue(api.is_adult_item({"name": "Deep Inside Missy"}))

        # Adult flag
        self.assertTrue(api.is_adult_item({"title": "Bad Mama", "adult": True}))
        self.assertTrue(api.is_adult_item({"title": "Playtime", "adult": True}))
        self.assertTrue(api.is_adult_item({"title": "Some Show", "isAdult": True}))

        # Certifications
        self.assertTrue(api.is_adult_item({"title": "Movie", "certification": "NC-17"}))
        self.assertTrue(api.is_adult_item({"title": "Movie", "certification": "XXX"}))
        self.assertTrue(api.is_adult_item({"title": "Movie", "certification": "R18+"}))
        self.assertTrue(api.is_adult_item({"title": "Movie", "contentRating": "18"}))

        # Mainstream titles must NOT be flagged as adult
        self.assertFalse(api.is_adult_item({"title": "The Dark Knight", "genres": ["Action", "Crime"]}))
        self.assertFalse(api.is_adult_item({"title": "Sex and the City", "genres": ["Comedy", "Romance"]}))
        self.assertFalse(api.is_adult_item({"title": "Sex Education", "genres": ["Comedy", "Drama"]}))
        self.assertFalse(api.is_adult_item({"title": "Alaskan Bush People", "genres": ["Documentary"]}))
        self.assertFalse(api.is_adult_item({"title": "The Essex Serpent", "genres": ["Drama"]}))

    def test_database_cached_adult_metadata_check(self):
        """Test that if SQLite metadata cache has adult flag, is_adult_item catches it even with benign title."""
        with patch.object(database, "get_cached_metadata", return_value={"title": "Innocent Title", "adult": True}):
            self.assertTrue(api.is_adult_item({"id": "tt1234567", "name": "Innocent Title"}))

    def test_cached_catalog_adult_filtering(self):
        """Test that get_cached_catalog filters out adult items when is_adult_content_hidden is True."""
        raw_items = [
            {"id": "tt0111161", "title": "The Shawshank Redemption"},
            {"id": "tt0315225", "title": "Asses Galore 3: Pure Evil"}
        ]
        with patch.object(database, "is_adult_content_hidden", return_value=True):
            database.save_cached_catalog("test:key", raw_items)
            cached = database.get_cached_catalog("test:key")
            self.assertIsNotNone(cached)
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0]["title"], "The Shawshank Redemption")

    @patch("src.api._get_cached_request")
    @patch("src.database.get_addons")
    def test_catalog_items_tagged_with_origin_catalog_and_addon(self, mock_get_addons, mock_req):
        """Test that catalog items fetched via fetch_items have addon_name and catalog_name attached."""
        mock_get_addons.return_value = self.mock_addons
        mock_req.return_value = {
            "metas": [
                {
                    "id": "tt0111161",
                    "name": "The Shawshank Redemption",
                    "poster": "https://example.com/poster.jpg",
                    "type": "movie"
                }
            ]
        }

        items = api.fetch_items(
            media_type="movie",
            catalog_id="tc-recent",
            catalog_url="https://torrentclaw.com/api/stremio/manifest.json",
            page=1
        )

        self.assertIsNotNone(items)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.get("addon_name"), "TorrentClaw")
        self.assertEqual(item.get("catalog_name"), "Recent")
        self.assertEqual(item.get("catalog_id"), "tc-recent")
        self.assertEqual(item.get("manifest_url"), "https://torrentclaw.com/api/stremio/manifest.json")


if __name__ == "__main__":
    unittest.main()
