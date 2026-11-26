import unittest
from unittest.mock import patch, MagicMock
import json

from src import api


class TestSeriesMetadata(unittest.TestCase):

    def test_is_valid_meta_rejects_id_placeholders(self):
        # Raw ID title with no description and no videos should be rejected
        self.assertFalse(api.is_valid_meta({
            "id": "tt43439241",
            "title": "tt43439241",
            "description": "No synopsis available.",
            "videos": [],
            "type": "series"
        }))
        self.assertFalse(api.is_valid_meta({
            "id": "tt43439241",
            "title": "TT43439241",
            "description": "",
            "videos": [],
            "type": "series"
        }))
        # Radio stream placeholder should be rejected
        self.assertFalse(api.is_valid_meta({
            "id": "tt43439241",
            "title": "tt43439241",
            "description": "Stream tt43439241 live - Indian FM Radio HD on Stremio.",
            "videos": [],
            "type": "series"
        }))

    def test_is_valid_meta_accepts_rich_metadata(self):
        # Real title and real synopsis
        self.assertTrue(api.is_valid_meta({
            "id": "tt43439241",
            "title": "Headline",
            "description": "Dhaka, 2006. When a massive money-laundering exposé is wiped from existence...",
            "videos": [],
            "type": "series"
        }))
        # Real title and videos
        self.assertTrue(api.is_valid_meta({
            "id": "tt43439241",
            "title": "Headline",
            "description": "No synopsis available.",
            "videos": [{"season": 1, "episode": 1, "title": "Pilot"}],
            "type": "series"
        }))

    def test_merge_metadata(self):
        base = {
            "id": "tt43439241",
            "title": "tt43439241",
            "description": "No synopsis available.",
            "videos": [
                {"id": "tt43439241:1:1", "season": 1, "episode": 1, "title": "Almost Famous"},
                {"id": "tt43439241:1:2", "season": 1, "episode": 2, "title": "The Insiders"}
            ],
            "imdbRating": "6.9",
            "type": "series"
        }
        extra = {
            "id": "tt43439241",
            "title": "Headline",
            "description": "Dhaka, 2006. An investigative journalist uncovers...",
            "medium_cover_image": "https://banglaplex.biz/poster.jpg",
            "background": "https://banglaplex.biz/bg.jpg",
            "genres": ["Drama", "Bengali Web Series"],
            "year": "2026",
            "type": "series"
        }
        merged = api.merge_metadata(base, extra)
        self.assertEqual(merged["title"], "Headline")
        self.assertEqual(merged["description"], "Dhaka, 2006. An investigative journalist uncovers...")
        self.assertEqual(len(merged["videos"]), 2)
        self.assertEqual(merged["medium_cover_image"], "https://banglaplex.biz/poster.jpg")
        self.assertEqual(merged["imdbRating"], "6.9")
        self.assertEqual(merged["year"], "2026")

    def test_series_fallback_video_synthesis(self):
        # Test that _save_and_return_meta creates Episode 1 if a series has 0 videos
        meta = {
            "id": "tt43695931",
            "title": "Dahan",
            "description": "Can a person's memories remain alive...",
            "type": "series",
            "videos": []
        }
        res = api._save_and_return_meta(meta, "tt43695931", media_type="series", title="Dahan")
        self.assertEqual(len(res["videos"]), 1)
        v0 = res["videos"][0]
        self.assertEqual(v0["season"], 1)
        self.assertEqual(v0["episode"], 1)
        self.assertEqual(v0["id"], "tt43695931:1:1")
        self.assertEqual(v0["title"], "Dahan")


if __name__ == "__main__":
    unittest.main()
