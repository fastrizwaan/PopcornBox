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


if __name__ == "__main__":
    unittest.main()
