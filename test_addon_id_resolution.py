import base64
import json
import unittest
from unittest.mock import patch, MagicMock


def _make_dsx(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "dsx:c:" + base64.b64encode(raw).decode("ascii").rstrip("=")


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

    def test_provider_encoded_ids_are_decoded(self):
        """DesiFlix dsx: IDs carry the TMDB key inside base64 JSON."""
        from src.tmdb_helper import _decode_provider_item_id

        movie_id = _make_dsx({"s": "movie", "k": "#m1164892", "t": "Escape"})
        movie = _decode_provider_item_id(movie_id)
        self.assertEqual(movie["tmdb_id"], "1164892")
        self.assertEqual(movie["tmdb_type"], "movie")
        self.assertEqual(movie["title"], "Escape")
        self.assertEqual(movie["media_type"], "movie")

        series_id = _make_dsx({
            "s": "web", "k": "#t86248", "t": "Upload",
            "u": "https://example.test/ser/86248", "se": [2, 3],
        })
        series = _decode_provider_item_id(series_id + ":S2:E3")
        self.assertEqual(series["tmdb_id"], "86248")
        self.assertEqual(series["tmdb_type"], "tv")
        self.assertEqual(series["media_type"], "series")
        self.assertEqual((series["season"], series["episode"]), (2, 3))

        self.assertIsNone(_decode_provider_item_id("tt0111161"))
        self.assertIsNone(_decode_provider_item_id("dsx:c:not-base64"))

    def test_provider_encoded_id_resolves_for_torrent_addons(self):
        """A dsx: item must yield tt/tmdb ids so tt-only torrent providers run."""
        from src.tmdb_helper import resolve_all_provider_ids

        item_id = _make_dsx({"s": "movie", "k": "#m1164892", "t": "Escape"})
        addons = [
            {"manifest_url": "https://torrent.test/manifest.json", "idPrefixes": ["tt"], "enabled": True},
            {"manifest_url": "https://desiflix.test/manifest.json", "idPrefixes": ["dsx:", "tt", "tmdb:"], "enabled": True},
        ]

        def fake_request(url, *args, **kwargs):
            if "api.themoviedb.org" in url:
                return {"external_ids": {"imdb_id": "tt26451068"}}
            if "cinemeta" in url:
                return {"metas": [
                    {"id": "tt99999999", "name": "Escape Room"},
                    {"id": "tt26451068", "name": "Escape"},
                ]}
            return {}

        with patch("src.tmdb_helper.database.get_addons", return_value=addons), \
             patch("src.tmdb_helper.get_tmdb_api_key", return_value="fake_key"), \
             patch("src.tmdb_helper._get_cached_request", side_effect=fake_request):
            ids = resolve_all_provider_ids(item_id, "movie", "Escape")

        self.assertIn("tt26451068", ids)
        self.assertIn("tmdb:1164892", ids)
        # The title-search fallback must not inject an unrelated exact-name match.
        self.assertNotIn("tt99999999", ids)

    def test_title_fallback_prefers_exact_match(self):
        """Unknown custom prefixes still resolve via Cinemeta's exact title."""
        from src.tmdb_helper import resolve_all_provider_ids

        addons = [{"manifest_url": "https://torrent.test/manifest.json", "idPrefixes": ["tt"], "enabled": True}]

        def fake_request(url, *args, **kwargs):
            if "cinemeta" in url:
                return {"metas": [
                    {"id": "tt99999999", "name": "Escape Room"},
                    {"id": "tt26451068", "name": "Escape"},
                ]}
            return {}

        with patch("src.tmdb_helper.database.get_addons", return_value=addons), \
             patch("src.tmdb_helper.get_tmdb_api_key", return_value="fake_key"), \
             patch("src.tmdb_helper._get_cached_request", side_effect=fake_request):
            ids = resolve_all_provider_ids("bpx-12345", "movie", "Escape")

        self.assertIn("tt26451068", ids)
        self.assertNotIn("tt99999999", ids)


if __name__ == "__main__":
    unittest.main()
