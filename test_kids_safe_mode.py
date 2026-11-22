import unittest
import os
import sys
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("mpv", MagicMock())

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database, api, tmdb_helper


class TestKidsSafeMode(unittest.TestCase):
    def setUp(self):
        # Save original settings
        self.orig_kids_mode = database.is_kids_safe_mode()
        self.orig_adult_hidden = database.get_setting("hide_adult_content", True)

    def tearDown(self):
        # Restore original settings
        database.set_kids_safe_mode(self.orig_kids_mode)
        database.set_adult_content_hidden(self.orig_adult_hidden)

    def test_mature_certifications_classification(self):
        # Mature certifications (should return True)
        mature_certs = [
            "R", "NC-17", "NC17", "X", "XXX", "TV-MA", "TVMA",
            "16", "16+", "18", "18+", "21+", "FSK 16", "FSK 18",
            "FSK-16", "FSK-18", "MA 15+", "MA15+", "R18+", "R 18+",
            "15", "15+", "M18", "R21", "Cat III", "Adults Only",
            "AO", "Age 16+", "Age 18+", "Rated R for violence"
        ]
        for cert in mature_certs:
            self.assertTrue(
                api.is_mature_certification(cert),
                f"Expected '{cert}' to be classified as mature certification"
            )

        # Safe certifications for kids / under 16 (should return False)
        safe_certs = [
            "G", "PG", "PG-13", "PG13", "TV-Y", "TV-Y7", "TV-G",
            "TV-PG", "TV-14", "TV14", "U", "UA", "UA 13+", "12",
            "12+", "12A", "FSK 6", "FSK 12", "FSK-12", "6+", "7+",
            "10+", "13+", "14+", "General", "Passed", "Approved"
        ]
        for cert in safe_certs:
            self.assertFalse(
                api.is_mature_certification(cert),
                f"Expected '{cert}' to be classified as safe certification"
            )

    def test_database_kids_safe_mode_toggle(self):
        # Enable Kids Safe Mode
        database.set_kids_safe_mode(True)
        self.assertTrue(database.is_kids_safe_mode())
        # Adult content must also automatically report as hidden
        self.assertTrue(database.is_adult_content_hidden())

        # Disable Kids Safe Mode
        database.set_kids_safe_mode(False)
        self.assertFalse(database.is_kids_safe_mode())

    def test_is_mature_item(self):
        # Item with mature certification
        r_movie = {"id": "tt1234567", "title": "Deadpool", "certification": "R"}
        self.assertTrue(api.is_mature_item(r_movie, check_cache=False))

        # Item with TV-MA certification
        ma_show = {"id": "tt2345678", "title": "Game of Thrones", "contentRating": "TV-MA"}
        self.assertTrue(api.is_mature_item(ma_show, check_cache=False))

        # Item with mature genre
        ecchi_anime = {"id": "tt3456789", "title": "Anime Show", "genres": ["Action", "Ecchi"]}
        self.assertTrue(api.is_mature_item(ecchi_anime, check_cache=False))

        # Item with adult flag
        adult_item = {"id": "tt4567890", "title": "Some Film", "adult": True}
        self.assertTrue(api.is_mature_item(adult_item, check_cache=False))

        # Item with PG-13 certification (Safe for under 16)
        pg13_movie = {"id": "tt5678901", "title": "Avengers: Endgame", "certification": "PG-13"}
        self.assertFalse(api.is_mature_item(pg13_movie, check_cache=False))

        # Item with TV-14 certification (Safe for under 16)
        tv14_show = {"id": "tt6789012", "title": "Stranger Things", "certification": "TV-14"}
        self.assertFalse(api.is_mature_item(tv14_show, check_cache=False))

        # Item with G / TV-Y certification (Kids safe)
        kids_movie = {"id": "tt7890123", "title": "Finding Nemo", "certification": "G"}
        self.assertFalse(api.is_mature_item(kids_movie, check_cache=False))

    def test_is_adult_item_delegation_in_kids_mode(self):
        r_item = {"id": "tt9999999", "title": "Gladiator", "certification": "R"}

        # When Kids Safe Mode is OFF, standard R-rated movie is not considered adult (porn)
        database.set_kids_safe_mode(False)
        self.assertFalse(api.is_adult_item(r_item, check_cache=False))

        # When Kids Safe Mode is ON, standard R-rated movie IS filtered
        database.set_kids_safe_mode(True)
        self.assertTrue(api.is_adult_item(r_item, check_cache=False))

        # PG-13 item remains accessible even in Kids Safe Mode
        pg13_item = {"id": "tt8888888", "title": "Spider-Man", "certification": "PG-13"}
        self.assertFalse(api.is_adult_item(pg13_item, check_cache=False))

    def test_extract_tmdb_certification(self):
        # Movie release dates structure where US is PG-13 but worldwide has mature rating (e.g. Argentina +16)
        batman_tmdb = {
            "release_dates": {
                "results": [
                    {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
                    {"iso_3166_1": "AR", "release_dates": [{"certification": "+16"}]},
                    {"iso_3166_1": "GB", "release_dates": [{"certification": "15"}]}
                ]
            }
        }
        cert = tmdb_helper.extract_tmdb_certification(batman_tmdb, c_type="movie")
        self.assertTrue(api.is_mature_certification(cert), f"Expected worldwide mature cert, got '{cert}'")

        # Movie release dates structure
        movie_tmdb = {
            "release_dates": {
                "results": [
                    {"iso_3166_1": "FR", "release_dates": [{"certification": "12"}]},
                    {"iso_3166_1": "US", "release_dates": [{"certification": "R"}]}
                ]
            }
        }
        self.assertEqual(tmdb_helper.extract_tmdb_certification(movie_tmdb, c_type="movie"), "R")

        # TV content ratings structure
        tv_tmdb = {
            "content_ratings": {
                "results": [
                    {"iso_3166_1": "GB", "rating": "15"},
                    {"iso_3166_1": "US", "rating": "TV-MA"}
                ]
            }
        }
        self.assertEqual(tmdb_helper.extract_tmdb_certification(tv_tmdb, c_type="tv"), "15")

        # Family / Safe TV content rating
        family_tv = {
            "content_ratings": {
                "results": [
                    {"iso_3166_1": "US", "rating": "TV-Y"}
                ]
            }
        }
        self.assertEqual(tmdb_helper.extract_tmdb_certification(family_tv, c_type="tv"), "TV-Y")

        # Kids animated movie (Swapped 2026) with Macau 'A', India 'U/A 7+', and US 'PG'
        swapped_tmdb = {
            "release_dates": {
                "results": [
                    {"iso_3166_1": "AR", "release_dates": [{"certification": "ATP"}]},
                    {"iso_3166_1": "IN", "release_dates": [{"certification": "U/A 7+"}]},
                    {"iso_3166_1": "MO", "release_dates": [{"certification": "A"}]},
                    {"iso_3166_1": "US", "release_dates": [{"certification": "PG"}]},
                    {"iso_3166_1": "GB", "release_dates": [{"certification": "PG"}]}
                ]
            }
        }
        # India user preference: should return U/A 7+, not Macau's 'A'
        self.assertEqual(tmdb_helper.extract_tmdb_certification(swapped_tmdb, c_type="movie", preferred_country="IN"), "U/A 7+")
        # US user preference: should return PG, not Macau's 'A'
        self.assertEqual(tmdb_helper.extract_tmdb_certification(swapped_tmdb, c_type="movie", preferred_country="US"), "PG")

    def test_cbfc_and_latin_ratings_safety(self):
        # Indian CBFC kids safe ratings
        self.assertTrue(api.is_kids_safe_certification("U/A 7+"))
        self.assertTrue(api.is_kids_safe_certification("U/A 13+"))
        self.assertTrue(api.is_kids_safe_certification("U/A"))
        self.assertTrue(api.is_kids_safe_certification("UA"))
        self.assertTrue(api.is_kids_safe_certification("U"))
        self.assertFalse(api.is_mature_certification("U/A 7+"))
        self.assertFalse(api.is_mature_certification("U/A"))
        self.assertFalse(api.is_mature_certification("UA"))

        # Bare 'A' (Apto in Spain/Mexico/Macau) is not an adult or mature rating by default
        self.assertFalse(api.is_mature_certification("A"))
        self.assertNotIn("A", api.ADULT_RATINGS)

        # Kids animated movie with 'A' or 'U/A 7+' is never adult or mature
        swapped_movie = {
            "id": "tt29552248",
            "title": "Swapped",
            "genres": ["Adventure", "Animation", "Family", "Fantasy"],
            "certification": "U/A 7+"
        }
        self.assertFalse(api.is_adult_item(swapped_movie, check_cache=False))
        self.assertFalse(api.is_mature_item(swapped_movie, check_cache=False))

        swapped_legacy_a = {
            "id": "tt29552248",
            "title": "Swapped",
            "genres": ["Adventure", "Animation", "Family", "Fantasy"],
            "certification": "A"
        }
        self.assertFalse(api.is_adult_item(swapped_legacy_a, check_cache=False))
        self.assertFalse(api.is_mature_item(swapped_legacy_a, check_cache=False))

        # Indian mature movie with CBFC 'A' and action/crime (non-kids) genres IS mature
        indian_adult_film = {
            "id": "tt781732",
            "title": "Animal",
            "country": "India",
            "language": "Hindi",
            "genres": ["Action", "Crime", "Drama"],
            "certification": "A"
        }
        self.assertTrue(api.is_mature_item(indian_adult_film, check_cache=False))

    def test_cinemeta_fallback_and_type_auto_correction(self):
        from unittest.mock import patch

        # 1. Test Cinemeta fallback when TMDB returns None (must NOT raise UnboundLocalError)
        with patch("src.api.fetch_tmdb_standalone_metadata", return_value=None):
            try:
                res = api.fetch_movie_details(
                    "tt8421566", media_type="movie", title="Japani Toy", prefer_tmdb=True, use_cache=False, expected_year="2018"
                )
            except UnboundLocalError as e:
                self.fail(f"fetch_movie_details raised UnboundLocalError: {e}")

        # 2. Test TMDb standalone metadata auto-corrects media_type from movie to series for TV series IMDb ID
        meta = tmdb_helper.fetch_tmdb_standalone_metadata(
            "tt8421566", media_type="movie", title="Japani Toy", year="2018"
        )
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("type"), "series")
        self.assertEqual(meta.get("title"), "Japani Toy")
        self.assertGreater(len(meta.get("videos", [])), 0)

    def test_database_hydrate_certifications(self):
        database.set_kids_safe_mode(True)
        database.save_cached_metadata("tt_test_hydrate", "movie", {
            "title": "Hydrate Test",
            "certification": "R",
            "contentRating": "TV-MA",
            "year": "2024"
        })
        items = [{"id": "tt_test_hydrate", "title": "Hydrate Test"}]
        count = database.hydrate_items_from_metadata(items)
        self.assertEqual(count, 1)
        self.assertEqual(items[0].get("certification"), "R")
        self.assertEqual(items[0].get("contentRating"), "TV-MA")

    def test_movie_widget_mature_filtering_on_init(self):
        from src.movie_widget import MovieWidget
        database.set_kids_safe_mode(True)

        # Mature item should be hidden on init
        mature_card = MovieWidget({"id": "tt1111111", "title": "Deadpool", "certification": "R"}, lambda x: None)
        self.assertFalse(mature_card.get_visible())

        # Safe item should remain visible
        safe_card = MovieWidget({"id": "tt2222222", "title": "Finding Nemo", "certification": "G"}, lambda x: None)
        self.assertTrue(safe_card.get_visible())

        # Item with mature rating in SQLite cache should be hidden even if init dict is shallow
        database.save_cached_metadata("tt_sqlite_mature", "movie", {
            "title": "Mature Film",
            "certification": "18+"
        })
        cached_mature_card = MovieWidget({"id": "tt_sqlite_mature", "title": "Shallow Title"}, lambda x: None)
        self.assertFalse(cached_mature_card.get_visible())

    def test_movie_widget_hydration_hiding(self):
        from src.movie_widget import MovieWidget
        database.set_kids_safe_mode(True)

        # Shallow item initially visible
        card = MovieWidget({"id": "tt3333333", "title": "Unknown Rating"}, lambda x: None)
        self.assertTrue(card.get_visible())

        # Applying resolved metadata marked as adult hides the card
        card._apply_resolved_meta("Unknown Rating", "2025", None, is_adult=True)
        self.assertFalse(card.get_visible())

    def test_details_page_lockdown_when_filtered(self):
        from src.window import MovieDetailsPage
        database.set_kids_safe_mode(True)

        page = MovieDetailsPage(
            {"id": "tt_locked_mature", "title": "Mature Movie", "certification": "R", "type": "movie"},
            on_back=lambda: None
        )

        # Verify page entered filtered mode
        self.assertTrue(getattr(page, "_is_content_filtered", False))

        # Verify playback buttons and backdrop are hidden
        self.assertFalse(page.continue_btn.get_visible())
        self.assertFalse(page.continue_btn.get_sensitive())
        self.assertFalse(page.trailer_btn.get_visible())
        self.assertFalse(page.backdrop_pic.get_visible())

        # Verify streams are empty and blocked
        self.assertEqual(len(page.torrents), 0)
        self.assertEqual(len(page.current_t_list), 0)

        # Calling fetch_torrents_async must immediately return without fetching
        page.fetch_torrents_async(force=True)
        self.assertEqual(len(page.torrents), 0)

        # Calling on_watch_clicked must not initiate playback
        page.on_watch_clicked()
        self.assertEqual(len(page.torrents), 0)

    def test_prefilter_kids_safe_items(self):
        database.set_kids_safe_mode(True)
        items = [
            {"id": "tt1", "title": "Deadpool", "certification": "R"},
            {"id": "tt2", "title": "Frozen", "certification": "PG"},
            {"id": "tt3", "title": "Stranger Things", "certification": "TV-14"},
            {"id": "tt4", "title": "MobLand", "contentRating": "TV-MA"},
            {"id": "tt5", "title": "Unrated Horror", "genres": ["Horror", "Mystery"]},
            {"id": "tt6", "title": "Finding Dory", "certification": "G"}
        ]
        filtered = api.prefilter_kids_safe_items(items, media_type="movie")
        titles = [it["title"] for it in filtered]
        self.assertIn("Frozen", titles)
        self.assertIn("Stranger Things", titles)
        self.assertIn("Finding Dory", titles)
        self.assertNotIn("Deadpool", titles)
        self.assertNotIn("MobLand", titles)
        self.assertNotIn("Unrated Horror", titles)

    def test_kids_safe_certification_helper(self):
        self.assertTrue(api.is_kids_safe_certification("G"))
        self.assertTrue(api.is_kids_safe_certification("PG"))
        self.assertTrue(api.is_kids_safe_certification("PG-13"))
        self.assertTrue(api.is_kids_safe_certification("TV-14"))
        self.assertTrue(api.is_kids_safe_certification("TV-PG"))
        self.assertTrue(api.is_kids_safe_certification("TV-Y"))
        self.assertFalse(api.is_kids_safe_certification("R"))
        self.assertFalse(api.is_kids_safe_certification("TV-MA"))
        self.assertFalse(api.is_kids_safe_certification("18+"))
        self.assertFalse(api.is_kids_safe_certification("16"))
        self.assertFalse(api.is_kids_safe_certification(""))


if __name__ == "__main__":
    unittest.main()
