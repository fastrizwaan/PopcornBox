#!/usr/bin/env python3
"""
Unit tests for TMDB credits, creator & cast, production companies, and actor filmography.
"""
import unittest
from src.tmdb_helper import (
    resolve_to_tmdb_id,
    fetch_credits_and_companies,
    fetch_person_details,
    fetch_company_details,
    get_tmdb_api_key
)


class TestTmdbCredits(unittest.TestCase):

    def test_get_tmdb_api_key(self):
        key = get_tmdb_api_key()
        self.assertIsNotNone(key)
        self.assertTrue(len(key) >= 16)

    def test_resolve_to_tmdb_id_formats(self):
        # Direct tmdb prefix
        self.assertEqual(resolve_to_tmdb_id("tmdb:550"), "550")
        self.assertEqual(resolve_to_tmdb_id("tmdb:1171145:1:2"), "1171145")
        # Raw numeric
        self.assertEqual(resolve_to_tmdb_id("550"), "550")
        # ctmdb prefix
        self.assertEqual(resolve_to_tmdb_id("ctmdb.550"), "550")
        # bolly prefix
        self.assertEqual(resolve_to_tmdb_id("bolly:m:550"), "550")

    def test_resolve_imdb_to_tmdb_id(self):
        # Movie: Fight Club tt0137523 -> 550
        tmdb_id = resolve_to_tmdb_id("tt0137523", media_type="movie")
        self.assertEqual(tmdb_id, "550")

        # Series: Breaking Bad tt0903747 -> 1396
        tv_id = resolve_to_tmdb_id("tt0903747", media_type="series")
        self.assertEqual(tv_id, "1396")

    def test_fetch_movie_credits_and_production(self):
        # Fight Club
        data = fetch_credits_and_companies("tt0137523", media_type="movie")
        self.assertIsInstance(data, dict)

        # Cast
        cast = data.get("cast", [])
        self.assertTrue(len(cast) > 0)
        cast_names = [c["name"] for c in cast]
        self.assertTrue(any("Edward Norton" in n for n in cast_names))
        self.assertTrue(any("Brad Pitt" in n for n in cast_names))

        # Check character and photo
        edward = next(c for c in cast if "Edward Norton" in c["name"])
        self.assertIsNotNone(edward.get("character"))
        self.assertTrue(edward["photo"].startswith("https://image.tmdb.org/"))

        # Crew (Directors)
        crew = data.get("crew", [])
        self.assertTrue(len(crew) > 0)
        directors = [c for c in crew if c.get("job") == "Director"]
        self.assertTrue(len(directors) > 0)
        self.assertEqual(directors[0]["name"], "David Fincher")

        # Production Companies
        companies = data.get("production_companies", [])
        self.assertTrue(len(companies) > 0)
        company_names = [c["name"] for c in companies]
        self.assertTrue(any("Fox" in name or "Regency" in name for name in company_names))

    def test_fetch_tv_credits_and_networks(self):
        # Breaking Bad
        data = fetch_credits_and_companies("tt0903747", media_type="series")
        self.assertIsInstance(data, dict)

        # Cast
        cast = data.get("cast", [])
        self.assertTrue(len(cast) > 0)
        cast_names = [c["name"] for c in cast]
        self.assertTrue(any("Bryan Cranston" in n for n in cast_names))

        # Check Creator in crew
        crew = data.get("crew", [])
        creators = [c for c in crew if c.get("job") == "Creator"]
        self.assertTrue(len(creators) > 0)
        self.assertEqual(creators[0]["name"], "Vince Gilligan")

        # Networks
        networks = data.get("networks", [])
        self.assertTrue(len(networks) > 0)
        net_names = [n["name"] for n in networks]
        self.assertTrue("AMC" in net_names)
        self.assertIsNotNone(networks[0].get("logo"))
        self.assertEqual(networks[0].get("type"), "network")

    def test_company_type_tags(self):
        # Movie companies
        data = fetch_credits_and_companies("tt0137523", media_type="movie")
        companies = data.get("production_companies", [])
        self.assertTrue(len(companies) > 0)
        self.assertEqual(companies[0].get("type"), "company")

    def test_fetch_person_details_and_filmography(self):
        # Edward Norton (TMDB ID 819)
        person = fetch_person_details(819)
        self.assertIsNotNone(person)
        self.assertEqual(person.get("name"), "Edward Norton")
        self.assertEqual(person.get("known_for"), "Acting")
        self.assertIsNotNone(person.get("biography"))
        self.assertTrue(len(person.get("biography")) > 20)
        self.assertIsNotNone(person.get("birthday"))
        self.assertIsNotNone(person.get("photo"))

        # Filmography
        filmography = person.get("filmography", [])
        self.assertTrue(len(filmography) > 10)
        film_titles = [f.get("title") for f in filmography]
        self.assertTrue("Fight Club" in film_titles or "American History X" in film_titles)

        # Check structure of filmography item
        item = filmography[0]
        self.assertTrue(item["id"].startswith("tmdb:"))
        self.assertIn("type", item)
        self.assertIn("year", item)
        self.assertIn("rating", item)

    def test_fetch_company_details(self):
        # AMC network (TMDB ID 174)
        company = fetch_company_details(174, entity_type="network")
        self.assertIsNotNone(company)
        self.assertEqual(company.get("id"), 174)
        self.assertIn("name", company)
        self.assertIn("movies", company)
        self.assertIn("series", company)
        self.assertIn("all_titles", company)

    def test_contain_scaling_aspect_ratio(self):
        # Verify proportional scaling logic for square logos (e.g. Apple 100x100 into 130x36 badge)
        orig_w, orig_h = 100, 100
        target_w, target_h = 130, 36
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        # Entire 1:1 image scales to 36x36, preserving 1:1 aspect ratio without crop
        self.assertEqual(new_w, 36)
        self.assertEqual(new_h, 36)

        # Verify wide logo (e.g. 300x100 into 130x36 badge)
        orig_w, orig_h = 300, 100
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        self.assertTrue(new_w <= target_w)
        self.assertTrue(new_h <= target_h)
        self.assertAlmostEqual(new_w / new_h, 3.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
