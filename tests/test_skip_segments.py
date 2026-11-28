import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    import subprocess
    dest = Path(schema_temp_dir.name) / schema_path.name
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio

res_file = Path("build-dir/files/share/popcorn-box/popcornbox.gresource")
if res_file.exists():
    try:
        Gio.resources_register(Gio.Resource.load(str(res_file)))
    except Exception:
        pass

from src import skip_segments
from src.window import PopcornBoxWindow


def _fake_http(responses):
    """Patch _get_json with a URL-substring -> payload table."""
    def fake(url, timeout, headers=None):
        for needle, payload in responses.items():
            if needle in url:
                return payload
        return None
    return patch.object(skip_segments, "_get_json", side_effect=fake)


class TestSkipSegments(unittest.TestCase):

    def setUp(self):
        skip_segments._cache.clear()

    def test_parse_item_ids(self):
        self.assertEqual(skip_segments.parse_item_ids("tt0903747:1:2")["season"], 1)
        self.assertEqual(skip_segments.parse_item_ids("tt0903747:1:2")["episode"], 2)
        self.assertEqual(skip_segments.parse_item_ids("tmdb:1396")["tmdb"], "1396")
        k = skip_segments.parse_item_ids("kitsu:42:5")
        self.assertEqual((k["kitsu"], k["episode"]), ("42", 5))
        self.assertIsNone(skip_segments.parse_item_ids("yt_id:abc")["imdb"])

    def test_introdb_series_with_theintrodb_filling_gaps(self):
        responses = {
            "api.introdb.app": {
                "intro": {"start_sec": 314.5, "end_sec": 331.0},
                "recap": None,
                "outro": None,
            },
            "api.theintrodb.org": {
                "intro": [{"start_ms": 300000, "end_ms": 330000}],
                "credits": [{"start_ms": 2848424, "end_ms": None}],
            },
        }
        with _fake_http(responses):
            segs = skip_segments.fetch_segments(["tt0903747"], "series", 1, 2)
        self.assertEqual([(s["type"], s["provider"]) for s in segs],
                         [("intro", "introdb"), ("outro", "theintrodb")])
        self.assertIsNone(segs[1]["end"])  # credits run to end of file

    def test_movie_credits_stop_before_post_credits_scene(self):
        responses = {
            "api.introdb.app": {
                "outro": {"start_sec": 6000, "end_sec": 6500},
                "post_credits": {"start_sec": 6300, "end_sec": 6400},
            },
        }
        with _fake_http(responses):
            segs = skip_segments.fetch_segments(["tt1375666"], "movie")
        self.assertEqual(segs, [{"start": 6000.0, "end": 6300.0, "type": "outro", "provider": "introdb"}])

    def test_theintrodb_null_start_means_zero(self):
        with _fake_http({"api.theintrodb.org": {"intro": [{"start_ms": None, "end_ms": 38000}]}}):
            segs = skip_segments.fetch_segments(["tmdb:27205"], "movie")
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 38.0)

    def test_kitsu_maps_to_mal_for_aniskip(self):
        responses = {
            "kitsu.app": {"data": [{"attributes": {"externalId": "1"}}]},
            "api.aniskip.com/v2/skip-times/1/3": {
                "found": True,
                "results": [{"interval": {"startTime": 57.4, "endTime": 145.7}, "skipType": "op"}],
            },
        }
        with _fake_http(responses):
            segs = skip_segments.fetch_segments(["kitsu:1"], "anime", None, 3)
        self.assertEqual([(s["type"], s["start"]) for s in segs], [("op", 57.4)])

    def test_series_without_episode_is_not_looked_up_as_movie(self):
        with _fake_http({"api.introdb.app": {"outro": {"start_sec": 1, "end_sec": 9}}}) as m:
            self.assertEqual(skip_segments.fetch_segments(["tt0903747"], "series", 1, None), [])
        m.assert_not_called()

    def test_chapters(self):
        chapters = [
            {"time": 0, "title": "Prologue"},
            {"time": 60, "title": "Opening"},
            {"time": 150, "title": "Part A"},
            {"time": 900, "title": "Intro"},  # ignored: an OP chapter exists
            {"time": 930, "title": "Part B"},
            {"time": 1300, "title": "ED"},
            {"time": 1390, "title": "Preview"},
        ]
        segs = skip_segments.from_chapters(chapters, 1420)
        self.assertEqual([(s["type"], s["start"], s["end"]) for s in segs],
                         [("intro", 60.0, 150.0), ("outro", 1300.0, 1390.0)])
        # Too long to be an opening (over 4 minutes)
        self.assertEqual(skip_segments.from_chapters([{"time": 0, "title": "Opening"}, {"time": 600, "title": "A"}], 900), [])

    def test_chapters_take_priority_over_api(self):
        chap = [{"start": 60.0, "end": 150.0, "type": "intro", "provider": "chapters"}]
        api = [{"start": 58.0, "end": 149.0, "type": "intro", "provider": "introdb"},
               {"start": 1300.0, "end": None, "type": "outro", "provider": "introdb"}]
        merged = skip_segments.merge_by_priority(chap, api)
        self.assertEqual([s["provider"] for s in merged], ["chapters", "introdb"])

    def test_active_segment(self):
        segs = [{"start": 10.0, "end": 40.0, "type": "intro", "provider": "x"},
                {"start": 1300.0, "end": None, "type": "outro", "provider": "x"}]
        self.assertIsNone(skip_segments.active_segment(segs, 5.0, 1400))
        self.assertEqual(skip_segments.active_segment(segs, 10.0, 1400)["type"], "intro")
        self.assertIsNone(skip_segments.active_segment(segs, 39.8, 1400))  # nothing left to skip
        self.assertEqual(skip_segments.active_segment(segs, 1350.0, 1400)["type"], "outro")
        self.assertIsNone(skip_segments.active_segment(segs, 1350.0, None))  # open end, unknown duration


class TestSkipSegmentButton(unittest.TestCase):
    """Exercise the window's button logic on a stand-in object."""

    def _window(self, segments, has_next=False):
        W = PopcornBoxWindow
        visible = {"v": False}
        label = SimpleNamespace(text="", get_text=lambda: label.text, set_text=lambda t: setattr(label, "text", t))
        seeks = []
        w = SimpleNamespace(
            _skip_api_segments=segments, _skip_dismissed=set(), _skip_active=None,
            _skip_chapter_cache=(None, []), chapters=[], skip_segment_label=label,
            _is_hls_proxy_active=False,
            mpv=SimpleNamespace(duration=1400.0, command_async=lambda *a: seeks.append(a)),
            next_clicked=[],
        )
        w._next_episode_revealer = lambda: None
        w._skip_segments_enabled = lambda: True
        w._has_next_episode = lambda: has_next
        w._set_skip_segment_visible = lambda v: visible.__setitem__("v", v)
        w._on_next_ep_play_clicked = lambda: w.next_clicked.append(True)
        w._current_skip_segments = lambda d: W._current_skip_segments(w, d)
        w.update = lambda t: W._update_skip_segment_button(w, t, 1400.0)
        w.click = lambda: W._on_skip_segment_clicked(w)
        return w, visible, label, seeks

    def test_intro_button_shows_and_seeks_to_end(self):
        w, visible, label, seeks = self._window([{"start": 10.0, "end": 40.0, "type": "intro", "provider": "introdb"}])
        w.update(5.0)
        self.assertFalse(visible["v"])
        w.update(12.0)
        self.assertTrue(visible["v"])
        self.assertEqual(label.text, "Skip Intro")
        w.click()
        self.assertEqual(seeks, [("seek", 40.0, "absolute")])
        self.assertFalse(visible["v"])
        w.update(13.0)  # seek still in flight: stays hidden
        self.assertFalse(visible["v"])
        w.update(41.0)  # seek landed past the intro
        self.assertFalse(visible["v"])
        w.update(20.0)  # user seeks back into the intro
        self.assertTrue(visible["v"])
        self.assertEqual(label.text, "Skip Intro")

    def test_outro_plays_next_episode_when_available(self):
        w, visible, label, seeks = self._window([{"start": 1300.0, "end": None, "type": "outro", "provider": "theintrodb"}], has_next=True)
        w.update(1310.0)
        self.assertEqual(label.text, "Next Episode")
        w.click()
        self.assertEqual(w.next_clicked, [True])
        self.assertEqual(seeks, [])

    def test_open_ended_credits_without_next_episode_seek_near_end(self):
        w, visible, label, seeks = self._window([{"start": 1300.0, "end": None, "type": "outro", "provider": "theintrodb"}])
        w.update(1310.0)
        self.assertEqual(label.text, "Skip Credits")
        w.click()
        self.assertEqual(seeks, [("seek", 1399.0, "absolute")])


if __name__ == "__main__":
    unittest.main()
