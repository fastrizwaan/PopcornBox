import unittest
from unittest.mock import patch, MagicMock
from datetime import date
from src import tmdb_helper, api, database


class TestFutureDatesFilter(unittest.TestCase):
    def test_is_future_item(self):
        today_str = "2026-11-05"
        current_year = 2026

        # Future date in next year
        self.assertTrue(tmdb_helper.is_future_item(
            {"release_date": "2027-05-14", "year": "2027"},
            today_str=today_str, current_year=current_year
        ))

        # Future date later in the current year
        self.assertTrue(tmdb_helper.is_future_item(
            {"release_date": "2026-12-25", "year": "2026"},
            today_str=today_str, current_year=current_year
        ))

        # Released today is not future
        self.assertFalse(tmdb_helper.is_future_item(
            {"release_date": "2026-11-05", "year": "2026"},
            today_str=today_str, current_year=current_year
        ))

        # Released in past is not future
        self.assertFalse(tmdb_helper.is_future_item(
            {"release_date": "2026-10-15", "year": "2026"},
            today_str=today_str, current_year=current_year
        ))

        # Series first aired in past but continues into future
        self.assertFalse(tmdb_helper.is_future_item(
            {"first_air_date": "2024-03-01", "year": "2024–2027"},
            today_str=today_str, current_year=current_year
        ))

        # Series first airs in future
        self.assertTrue(tmdb_helper.is_future_item(
            {"first_air_date": "2027-01-01", "year": "2027–"},
            today_str=today_str, current_year=current_year
        ))

        # Item with only future year
        self.assertTrue(tmdb_helper.is_future_item(
            {"year": "2027"},
            today_str=today_str, current_year=current_year
        ))

        # Item with only current year
        self.assertFalse(tmdb_helper.is_future_item(
            {"year": "2026"},
            today_str=today_str, current_year=current_year
        ))

        # Item with only past year
        self.assertFalse(tmdb_helper.is_future_item(
            {"year": "2022"},
            today_str=today_str, current_year=current_year
        ))

    def test_country_catalog_excludes_future_items(self):
        today = date.today().isoformat()
        mock_payload = {
            "page": 1,
            "total_pages": 1,
            "results": [
                {
                    "id": 1001,
                    "title": "Past Movie",
                    "poster_path": "/past.jpg",
                    "release_date": "2026-01-01"
                },
                {
                    "id": 1002,
                    "title": "Future Movie 2027",
                    "poster_path": "/future.jpg",
                    "release_date": "2027-05-14"
                },
                {
                    "id": 1003,
                    "title": "Future Movie 2026 Later",
                    "poster_path": "/future_later.jpg",
                    "release_date": "2026-12-31"
                }
            ]
        }
        with patch("src.tmdb_helper._get_cached_request") as mock_req, \
             patch("src.tmdb_helper.get_tmdb_api_key", return_value="fake_key"):
            mock_req.return_value = mock_payload
            items, _ = tmdb_helper.fetch_country_catalog_page("United States", media_type="movie", limit=10)

            # Check that URL passed primary_release_date.lte
            called_url = mock_req.call_args[0][0]
            self.assertIn(f"primary_release_date.lte={today}", called_url)

            # Check that future movies were filtered out
            titles = [it["title"] for it in items]
            self.assertIn("Past Movie", titles)
            self.assertNotIn("Future Movie 2027", titles)
            if today < "2026-12-31":
                self.assertNotIn("Future Movie 2026 Later", titles)

    def test_database_cached_catalog_filters_future_items(self):
        items = [
            {"id": "tt1", "title": "Good Movie", "release_date": "2025-01-01", "year": "2025"},
            {"id": "tt2", "title": "Future Movie", "release_date": "2027-01-01", "year": "2027"},
        ]
        cache_key = "test:future_filter_catalog"
        database.save_cached_catalog(cache_key, items)

        retrieved = database.get_cached_catalog(cache_key)
        self.assertIsNotNone(retrieved)
        retrieved_titles = [it["title"] for it in retrieved]
        self.assertIn("Good Movie", retrieved_titles)
        self.assertNotIn("Future Movie", retrieved_titles)


if __name__ == "__main__":
    unittest.main()
