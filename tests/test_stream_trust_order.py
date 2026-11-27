import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("mpv", MagicMock())  # window imports mpv; not needed for sorting

# Same setup as the other window tests: compile the GSettings schema and load resources.
_schema_dir = tempfile.TemporaryDirectory()
_schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if _schema_path.exists():
    (Path(_schema_dir.name) / _schema_path.name).write_text(_schema_path.read_text())
    subprocess.run(["glib-compile-schemas", _schema_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = _schema_dir.name

from gi.repository import Gio  # noqa: E402
_res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if _res_file.exists():
    try:
        Gio.resources_register(Gio.Resource.load(str(_res_file)))
    except Exception:
        pass

from src import api


def _streams():
    return [
        # Stremio addon torrent: best quality and most seeds, but title-searched.
        {"name": "Torrentio", "title": "Love.2015.2160p.BluRay", "infoHash": "a" * 40,
         "seeders": 900, "addon_names": ["SomeAddon"]},
        # Nuvio plugin direct stream, 1080p.
        {"name": "NuvioThing 1080p", "title": "Love 1080p", "url": "https://n.example/love.m3u8",
         "is_http": True, "is_nuvio_plugin": True, "addon_names": ["NuvioThing"]},
        # Custom plugin that looks up strictly by IMDb/TMDB ID, only 720p.
        {"name": "IMDbPlay Luna 720p", "title": "Love\n720p | HLS", "url": "https://i.example/love.m3u8",
         "is_http": True, "is_custom_plugin": True, "id_lookup": True, "addon_names": ["IMDbPlay"]},
        # The catalog provider whose card was opened, only 480p.
        {"name": "9kMovies 480p", "title": "9kMovies 480p", "url": "https://k.example/love-480.mp4",
         "is_http": True, "is_custom_plugin": True, "addon_names": ["9kMovies"]},
    ]


def _providers(streams):
    return [s["addon_names"][0] for s in streams]


class TestStreamTrustOrder(unittest.TestCase):
    def test_pipeline_ranks_catalog_then_id_based_then_others(self):
        ranked = api.process_raw_streams(_streams(), title="Love", preferred_provider="9kMovies")
        self.assertEqual(_providers(ranked)[:2], ["9kMovies", "IMDbPlay"])
        self.assertEqual(set(_providers(ranked)[2:]), {"SomeAddon", "NuvioThing"})

    def test_id_based_first_without_catalog_card(self):
        ranked = api.process_raw_streams(_streams(), title="Love")
        self.assertEqual(_providers(ranked)[0], "IMDbPlay")

    def test_details_page_sort_keeps_trust_order(self):
        from src.window import stream_sort_key_all
        ranked = api.process_raw_streams(_streams(), title="Love", preferred_provider="9kMovies")
        resorted = sorted(reversed(ranked), key=stream_sort_key_all, reverse=True)
        self.assertEqual(_providers(resorted)[:2], ["9kMovies", "IMDbPlay"])

    def test_id_based_stream_without_title_in_label_is_kept(self):
        """An ID-found stream labelled only "Auto | Cinejoy" must not be dropped by the title-text check."""
        s = {"name": "Cinejoy Auto", "title": "Auto | Cinejoy", "url": "https://h.example/hls/x/1/master.m3u8",
             "is_http": True, "is_custom_plugin": True, "addon_name": "Cinejoy (Luffy)", "addon_names": ["Cinejoy (Luffy)"]}
        self.assertEqual(api.strict_stream_title_mismatch(s, "M3GAN 2.0"), api.MATCH_MISMATCH)
        self.assertEqual(api.strict_stream_title_mismatch(dict(s, id_lookup=True), "M3GAN 2.0"), api.MATCH_UNKNOWN)

    def test_remembered_stream_still_wins(self):
        from src.window import stream_sort_key_all
        ranked = api.process_raw_streams(_streams(), title="Love", preferred_provider="9kMovies")
        remembered = next(s for s in ranked if s["addon_names"][0] == "SomeAddon")
        resorted = sorted(ranked, key=lambda t: stream_sort_key_all(t, rem_st=remembered), reverse=True)
        self.assertEqual(_providers(resorted)[0], "SomeAddon")

    def test_auto_picked_stream_is_not_pinned(self):
        """A stream auto-selected before the catalog's own streams arrived must not stay on top."""
        from types import SimpleNamespace
        from src.window import MovieDetailsPage, stream_sort_key_all
        ranked = api.process_raw_streams(_streams(), title="Love", preferred_provider="9kMovies")
        early_pick = next(s for s in ranked if s["addon_names"][0] == "NuvioThing")
        page = SimpleNamespace(remembered_working_stream=None, selected_torrent=early_pick,
                               _user_explicitly_selected_stream=False, window=None)
        rem_st = MovieDetailsPage._pinned_stream(page)
        self.assertIsNone(rem_st)
        resorted = sorted(ranked, key=lambda t: stream_sort_key_all(t, rem_st=rem_st), reverse=True)
        self.assertEqual(_providers(resorted)[0], "9kMovies")

    def test_user_picked_and_remembered_streams_are_pinned(self):
        from types import SimpleNamespace
        from src.window import MovieDetailsPage
        picked, remembered, global_rem = {"url": "a"}, {"url": "b"}, {"url": "c"}
        page = SimpleNamespace(remembered_working_stream=None, selected_torrent=picked,
                               _user_explicitly_selected_stream=True, window=None)
        self.assertIs(MovieDetailsPage._pinned_stream(page), picked)
        page.remembered_working_stream = remembered
        self.assertIs(MovieDetailsPage._pinned_stream(page), remembered)
        page = SimpleNamespace(remembered_working_stream=None, selected_torrent=picked,
                               _user_explicitly_selected_stream=False,
                               window=SimpleNamespace(remembered_working_stream=global_rem, selected_torrent=picked))
        self.assertIs(MovieDetailsPage._pinned_stream(page), global_rem)


def _tiered_streams():
    return [
        # Title-searched scraper: best quality, no title evidence.
        {"name": "2160p", "title": "2160p", "url": "https://s.example/v/9a8b7c6d5e4f3a2b.m3u8",
         "is_http": True, "is_nuvio_plugin": True, "addon_names": ["TitleScraper"]},
        # Title-searched scraper whose release name contains the full title.
        {"name": "Namer 720p", "title": "Love.2015.720p.WEB", "url": "https://n.example/v/1b2c3d4e5f6a7b8c.m3u8",
         "is_http": True, "is_nuvio_plugin": True, "addon_names": ["Namer"]},
        # Stremio addon queried by IMDb ID, no catalog of its own.
        {"name": "Torrentio", "title": "Love.2015.480p", "infoHash": "b" * 40, "seeders": 10,
         "id_source": "imdb", "addon_names": ["IdAddon"]},
        # Stremio addon with an IMDb-keyed catalog, queried by IMDb ID.
        {"name": "CatAddon 480p", "title": "480p", "url": "https://c.example/v/2c3d4e5f6a7b8c9d.mp4",
         "is_http": True, "id_source": "imdb", "catalog_backed": True, "addon_names": ["CatAddon"]},
        # Custom plugin that searches by title (no lookup = "id"), only 360p.
        {"name": "Custom 360p", "title": "Love 360p", "url": "https://x.example/v/3d4e5f6a7b8c9d0e.mp4",
         "is_http": True, "is_custom_plugin": True, "addon_names": ["MyCustom"]},
    ]


class TestStreamTrustTiers(unittest.TestCase):
    def test_full_tier_order(self):
        ranked = api.process_raw_streams(_tiered_streams(), title="Love", expected_year="2015")
        # The title-searching custom plugin stays below every ID-based result;
        # it only leads the other full-title matches.
        self.assertEqual(_providers(ranked), ["CatAddon", "IdAddon", "MyCustom", "Namer", "TitleScraper"])

    def test_id_based_custom_plugin_leads_id_results(self):
        streams = _tiered_streams() + [
            {"name": "IdCustom 360p", "title": "360p", "url": "https://y.example/v/4e5f6a7b8c9d0e1f.mp4",
             "is_http": True, "is_custom_plugin": True, "id_lookup": True, "addon_names": ["IdCustom"]},
        ]
        ranked = api.process_raw_streams(streams, title="Love", expected_year="2015")
        self.assertEqual(_providers(ranked), ["IdCustom", "CatAddon", "IdAddon", "MyCustom", "Namer", "TitleScraper"])

    def test_opened_catalog_card_leads(self):
        ranked = api.process_raw_streams(_tiered_streams(), title="Love", expected_year="2015", preferred_provider="TitleScraper")
        self.assertEqual(_providers(ranked)[0], "TitleScraper")

    def test_without_custom_plugins_id_results_lead(self):
        streams = [s for s in _tiered_streams() if not s.get("is_custom_plugin")]
        ranked = api.process_raw_streams(streams, title="Love", expected_year="2015")
        self.assertEqual(_providers(ranked), ["CatAddon", "IdAddon", "Namer", "TitleScraper"])

    def test_details_page_sort_keeps_tiers(self):
        from src.window import stream_sort_key_all
        ranked = api.process_raw_streams(_tiered_streams(), title="Love", expected_year="2015")
        resorted = sorted(reversed(ranked), key=stream_sort_key_all, reverse=True)
        self.assertEqual(_providers(resorted), _providers(ranked))

    def test_duplicate_found_by_id_inherits_id_trust(self):
        title_hit = {"name": "T", "title": "Love.2015.1080p", "infoHash": "c" * 40, "seeders": 5,
                     "addon_names": ["TitleScraper"]}
        id_hit = dict(title_hit, addon_names=["IdAddon"], id_source="imdb")
        ranked = api.process_raw_streams([title_hit, id_hit], title="Love")
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["id_source"], "imdb")

    def test_canonical_id_kind(self):
        self.assertEqual(api.canonical_id_kind("tt0123456"), "imdb")
        self.assertEqual(api.canonical_id_kind("tmdb:550"), "tmdb")
        self.assertEqual(api.canonical_id_kind("kitsu:1:2"), "kitsu")
        self.assertEqual(api.canonical_id_kind("bolly:m:https://x"), "")
        self.assertEqual(api.canonical_id_kind(""), "")

    def test_addon_has_id_backed_catalog(self):
        base = {"catalogs": [{"type": "movie", "id": "top"}], "idPrefixes": ["tt"]}
        self.assertTrue(api.addon_has_id_backed_catalog(base))
        self.assertFalse(api.addon_has_id_backed_catalog(dict(base, catalogs=[])))
        self.assertFalse(api.addon_has_id_backed_catalog(dict(base, idPrefixes=["dsx:"])))
        search_only = dict(base, catalogs=[{"type": "movie", "id": "s", "extra": [{"name": "search", "isRequired": True}]}])
        self.assertFalse(api.addon_has_id_backed_catalog(search_only))
        by_resource = {"catalogs": [{"type": "movie", "id": "top"}],
                       "resources": [{"name": "catalog", "idPrefixes": ["tmdb:"]}, "stream"]}
        self.assertTrue(api.addon_has_id_backed_catalog(by_resource))

    def test_full_title_match(self):
        self.assertTrue(api.stream_full_title_match({"stream_title": "Love.2015.1080p"}, "Love"))
        self.assertFalse(api.stream_full_title_match({"stream_title": "Auto | 1080p"}, "Love"))
        self.assertFalse(api.stream_full_title_match({"stream_title": "Crazy Stupid Love 2011"}, "Love"))


class TestWidescreenQuality(unittest.TestCase):
    def test_cropped_heights_map_to_standard_tiers(self):
        cases = {"Cinejoy 1600p": "4K", "Cinejoy 800p": "1080p", "Cinejoy 534p": "720p",
                 "Cinejoy 266p": "360p", "3840x1600": "4K", "1920x800": "1080p",
                 "Movie.2160p": "4K", "Blade Runner 2049 1080p": "1080p"}
        for text, expected in cases.items():
            self.assertEqual(api._extract_quality(text)[0], expected, text)
        self.assertEqual(api._extract_quality("x265 10bit h264"), (None, 0))

    def test_details_page_bucket(self):
        from src.window import _normalize_stream_quality
        self.assertEqual(_normalize_stream_quality({"quality": "1600p"}), "4K")
        self.assertEqual(_normalize_stream_quality({"quality": "800p"}), "1080p")


if __name__ == "__main__":
    unittest.main()
