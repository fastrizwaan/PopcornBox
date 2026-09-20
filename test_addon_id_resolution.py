import unittest
from unittest.mock import patch, MagicMock


class TestAddonIdResolution(unittest.TestCase):
    def test_non_tmdb_addon_ids_preserved(self):
        """Verify non-TMDB addon IDs (like tubi:578912, kitsu:123, dsf:456) are preserved
        and not falsely queried against TMDB by integer ID."""
        from src.tmdb_helper import resolve_to_imdb_id

        # Tubi content ID 578912 is 'I, Robot'. TMDB 578912 is 'La Fracture'.
        # It must NOT resolve to La Fracture (tt5138104).
        res_tubi = resolve_to_imdb_id("tubi:578912", "movie", title="I, Robot")
        self.assertEqual(res_tubi, "tubi:578912")

        res_kitsu = resolve_to_imdb_id("kitsu:12345", "series", title="Some Anime")
        self.assertEqual(res_kitsu, "kitsu:12345")

        res_dsf = resolve_to_imdb_id("dsf:9876", "movie", title="Some Show")
        self.assertEqual(res_dsf, "dsf:9876")

    def test_imdb_ids_preserved(self):
        """Verify existing IMDB IDs (tt...) are cleanly returned."""
        from src.tmdb_helper import resolve_to_imdb_id
        res = resolve_to_imdb_id("tt0343818", "movie")
        self.assertEqual(res, "tt0343818")

        res_with_colon = resolve_to_imdb_id("tt0343818:1:1", "series")
        self.assertEqual(res_with_colon, "tt0343818")

    def test_tmdb_ids_resolved(self):
        """Verify genuine TMDB IDs are resolved."""
        from src.tmdb_helper import resolve_to_imdb_id

        mock_tmdb_data = {"external_ids": {"imdb_id": "tt0343818"}}
        with patch("src.tmdb_helper.get_tmdb_api_key", return_value="fake_key"), \
             patch("src.tmdb_helper._get_cached_request", return_value=mock_tmdb_data):
            self.assertEqual(resolve_to_imdb_id("tmdb:123", "movie"), "tt0343818")
            self.assertEqual(resolve_to_imdb_id("123", "movie"), "tt0343818")
            self.assertEqual(resolve_to_imdb_id("bolly:m:123", "movie"), "tt0343818")
            self.assertEqual(resolve_to_imdb_id("hub:s:123", "series"), "tt0343818")


if __name__ == "__main__":
    unittest.main()
