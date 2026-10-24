"""Cards that came back without a picture must repair themselves.

A poster can fail for reasons that have nothing to do with the title: the CDN
timed out once, the host's polite image window ran out of patience during a fast
scroll, or the card arrived with no poster URL at all and a later lookup found
one. Before this, every one of those left a permanently empty tile -- a failed
URL was banned for the life of the process and nothing re-asked for the picture.
"""
import unittest
import os
import sys
import shutil
import tempfile
import time
import weakref
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["mpv"] = MagicMock()

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    dest = Path(schema_temp_dir.name) / schema_path.name
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        Gio.resources_register(Gio.Resource.load(str(res_file)))
    except Exception:
        pass

os.environ["POPCORN_TEST"] = "1"
from src import api, database, movie_widget


class _FakePicture:
    """The parts of a Gtk.Picture the repair queue looks at, without a display.

    `mapped` stands for the tile's page being on screen and `attached` for it
    already living inside a window: a tile in a hidden stack page is attached and
    unmapped, which is the one case that parks a download.
    """

    def __init__(self, url, painted=False, mapped=True, attached=True):
        self._popcornbox_image_url = url
        self._painted = painted
        self._mapped = mapped
        self._attached = attached
        self.handlers = {}

    def get_paintable(self):
        return object() if self._painted else None

    def get_mapped(self):
        return self._mapped

    def get_root(self):
        return object() if self._attached else None

    def connect(self, name, callback):
        self.handlers[name] = callback

    def set_can_shrink(self, *args):
        pass

    def set_paintable(self, texture):
        self._painted = texture is not None

    def fire(self, name):
        self.handlers[name](self)


class TestBlankImageRepair(unittest.TestCase):
    def setUp(self):
        movie_widget._clear_failed_images()
        movie_widget._cancel_blank_tile_repairs()
        self._glib = patch.object(movie_widget, "GLib", MagicMock())
        self._glib.start()

    def tearDown(self):
        self._glib.stop()
        movie_widget._clear_failed_images()
        movie_widget._cancel_blank_tile_repairs()

    def _drain(self, times=1, fake_load=None):
        """Run repair ticks the way the main loop would."""
        issued = []
        def load(url, picture, width=None, height=None, crop=True, **kwargs):
            issued.append(url)
            if fake_load:
                fake_load(url, picture, width, height, crop)
        with patch.object(movie_widget, "load_image_into_picture", side_effect=load):
            for _ in range(times):
                movie_widget._blank_tile_repair_tick()
        return issued

    def test_a_failed_poster_is_retried_once_its_cooldown_passes(self):
        """One timeout used to blacklist a poster URL for the whole session."""
        url = "https://cdn.test/poster/dead-then-alive.jpg"
        movie_widget._mark_image_failed(url)
        self.assertTrue(movie_widget._is_image_failed(url))
        with movie_widget._FAILED_IMAGE_LOCK:
            movie_widget.FAILED_IMAGE_URLS[url] = (
                time.time() - movie_widget._FAILED_IMAGE_RETRY_SECONDS - 1)
        self.assertFalse(movie_widget._is_image_failed(url),
                         "an expired failure still keeps a tile blank")

    def test_a_successful_download_clears_the_failure(self):
        url = "https://cdn.test/poster/recovered.jpg"
        movie_widget._mark_image_failed(url)
        movie_widget._mark_image_success(url)
        self.assertFalse(movie_widget._is_image_failed(url))

    def test_a_blank_tile_is_fetched_again_later(self):
        url = "https://cdn.test/poster/queued.jpg"
        picture = _FakePicture(url)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        with patch.object(movie_widget, "load_image_into_picture") as load:
            movie_widget._blank_tile_repair_tick()
        self.assertEqual(load.call_args.args[0], url)

    def test_a_tile_that_painted_itself_is_left_alone(self):
        url = "https://cdn.test/poster/filled.jpg"
        picture = _FakePicture(url, painted=True)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        with patch.object(movie_widget, "load_image_into_picture") as load:
            movie_widget._blank_tile_repair_tick()
        load.assert_not_called()

    def test_a_tile_that_moved_on_to_another_picture_is_dropped(self):
        """Tiles are recycled with the cards that held them: a tile now showing a
        different poster must not have the old one re-issued over it."""
        old_url = "https://cdn.test/poster/old.jpg"
        picture = _FakePicture(old_url)
        movie_widget._note_blank_tile(picture, old_url, 130, 195, True)
        picture._popcornbox_image_url = "https://cdn.test/poster/new.jpg"
        issued = self._drain(times=1)
        self.assertEqual(issued, [])
        self.assertEqual(movie_widget._BLANK_TILES, [])

    def test_repairs_are_bounded_and_do_not_burst(self):
        """A dead URL gets a few polite tries spread over minutes, then rests."""
        url = "https://cdn.test/poster/gone.jpg"
        picture = _FakePicture(url)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        tries = []
        def keep_failing(failed_url, tile, width, height, crop):
            # Exactly what the fetch paths do when the tile is still empty.
            movie_widget._note_blank_tile(tile, failed_url, width, height, crop)
            tries.append(failed_url)
        issued = self._drain(times=6, fake_load=keep_failing)
        self.assertEqual(len(issued), movie_widget._BLANK_TILE_MAX_TRIES,
                         "blank tiles are retried more than the bounded number")
        self.assertEqual(tries, [url] * movie_widget._BLANK_TILE_MAX_TRIES)

    def test_a_tile_is_not_queued_twice(self):
        url = "https://cdn.test/poster/once.jpg"
        picture = _FakePicture(url)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        self.assertEqual(len(movie_widget._BLANK_TILES), 1)

        def keep_failing(failed_url, tile, width, height, crop):
            movie_widget._note_blank_tile(tile, failed_url, width, height, crop)
        self._drain(times=1, fake_load=keep_failing)
        self.assertEqual(len(movie_widget._BLANK_TILES), 1)

    def test_a_discarded_card_is_not_kept_alive_by_its_repair(self):
        url = "https://cdn.test/poster/orphan.jpg"
        picture = _FakePicture(url)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        ref = weakref.ref(picture)
        del picture
        issued = self._drain(times=1)
        self.assertEqual(issued, [])
        self.assertEqual(movie_widget._BLANK_TILES, [])
        self.assertIsNone(ref())

    def test_rebuilding_the_grid_clears_the_pending_repairs(self):
        url = "https://cdn.test/poster/stale.jpg"
        picture = _FakePicture(url)
        movie_widget._note_blank_tile(picture, url, 130, 195, True)
        movie_widget.cancel_pending_image_downloads()
        issued = self._drain(times=1)
        self.assertEqual(issued, [])

    def test_a_fallback_that_finds_no_poster_requeues_the_tile(self):
        """Cards that never received a URL are the case no download can report:
        nothing failed, so the lookup itself has to ask for a repair and the tick
        has to run it again once the addon that had nothing may have an answer."""
        tile = _FakePicture("")
        with patch.object(database, "get_cached_metadata", return_value=None), \
             patch.object(api, "_get_cached_request", return_value=None), \
             patch.object(api, "fetch_movie_details", return_value=None), \
             patch("urllib.request.urlopen", side_effect=Exception("offline")):
            movie_widget.fetch_fallback_poster(
                "tt0111161", "movie", tile, "The Shawshank Redemption")
            self.assertEqual(len(movie_widget._BLANK_TILES), 1)
            entry = movie_widget._BLANK_TILES[0]
            self.assertEqual(entry["url"], "")
            self.assertEqual(entry["unresolved"][0], "tt0111161")
            self.assertEqual(tile._popcornbox_blank_item_id, "tt0111161")
            with patch.object(movie_widget._meta_fallback_pool, "submit") as submit:
                movie_widget._blank_tile_repair_tick()

        self.assertEqual(submit.call_count, 1)
        args = submit.call_args.args
        self.assertIs(args[0], movie_widget.fetch_fallback_poster)
        self.assertEqual(args[1], "tt0111161")
        self.assertIs(args[3], tile)

    def test_a_recycled_or_discarded_tile_is_not_re_asked(self):
        """Two guards: a widget the flowbox handed to another card must not be
        painted with this one's poster, and a grid rebuild cancels the ask."""
        recycled = _FakePicture("")
        movie_widget._note_blank_tile(recycled, "", 130, 195, True,
                                      unresolved=("tt0111161", "movie", "Shawshank", None))
        recycled._popcornbox_blank_item_id = "tt0000001"
        stale = _FakePicture("")
        movie_widget._note_blank_tile(
            stale, "", 130, 195, True,
            unresolved=("tt0000002", "movie", "Other", movie_widget._IMAGE_GENERATION_ID + 1))
        stale._popcornbox_blank_item_id = "tt0000002"
        with patch.object(movie_widget._meta_fallback_pool, "submit") as submit:
            movie_widget._blank_tile_repair_tick()
        submit.assert_not_called()
        self.assertEqual(movie_widget._BLANK_TILES, [])

    def test_a_card_gets_the_picture_hydration_found_for_it(self):
        """A tile whose URL 404s must take the poster the hydrator resolved,
        instead of staying empty because its data dict had 'a' cover."""
        broken = "https://cdn.test/poster/broken.jpg"
        resolved = "https://image.tmdb.org/t/p/w300/found.jpg"
        card_data = {
            "id": "tt0111161", "title": "The Shawshank Redemption", "year": "1994",
            "type": "movie", "medium_cover_image": broken, "poster": broken,
        }
        with patch.object(movie_widget, "load_image_into_picture") as load:
            card = movie_widget.MovieWidget(card_data, lambda *a: None)
            card.poster_image._popcornbox_image_url = broken
            self.assertIsNone(card.poster_image.get_paintable())
            card._apply_resolved_meta("The Shawshank Redemption", "1994", resolved)
        self.assertEqual(card.movie_data["medium_cover_image"], resolved)
        self.assertEqual(load.call_args.args[0], resolved)

class TestCardsPauseWhileOffScreen(unittest.TestCase):
    """The blank grid after a movie-details visit.

    Opening a card stops the background workers, and until now that threw away
    every poster the grid had not finished: the tiles stayed empty because
    nothing asked for them again once the user came back. A card whose page is
    off screen now holds its picture instead of dropping it, and finishes it the
    moment the page is shown again.
    """

    def setUp(self):
        movie_widget._clear_failed_images()
        movie_widget._cancel_blank_tile_repairs()
        self.tmp = tempfile.mkdtemp(prefix="popcorn-tiles-")
        self._cache = patch.object(movie_widget, "IMAGE_CACHE_DIR", self.tmp)
        self._cache.start()
        self._glib = patch.object(movie_widget, "GLib", MagicMock())
        self._glib.start()

    def tearDown(self):
        self._glib.stop()
        self._cache.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        movie_widget._clear_failed_images()
        movie_widget._cancel_blank_tile_repairs()

    def _submits(self):
        return patch.object(movie_widget._image_pool, "submit")

    def _run_idle(self):
        """Run the callbacks the tiles' map signals handed to the main loop."""
        for call in movie_widget.GLib.idle_add.call_args_list:
            args = list(call.args)
            args[0](*args[1:])

    def test_a_card_that_is_not_on_screen_costs_no_download(self):
        poster = "https://cdn.test/poster/parked.jpg"
        tile = _FakePicture("", mapped=False)
        with self._submits() as submit:
            movie_widget.load_image_into_picture(poster, tile, width=130, height=195)
            submit.assert_not_called()
        self.assertEqual(tile._popcornbox_pending_image[0], poster)
        self.assertIn("map", tile.handlers)

    def test_coming_back_from_the_details_page_finishes_the_parked_card(self):
        poster = "https://cdn.test/poster/after-back.jpg"
        tile = _FakePicture("", mapped=False)
        with self._submits() as submit:
            movie_widget.load_image_into_picture(poster, tile, width=130, height=195)
            tile._mapped = True          # Back: the library page is on screen
            tile.fire("map")
            self.assertEqual(submit.call_count, 0,
                             "a page return did its image work inside the map cascade")
            self._run_idle()
            self.assertEqual(submit.call_count, 1)
            self.assertTrue(callable(submit.call_args.args[0]))

    def test_cancelling_for_a_card_click_cannot_strand_a_tile(self):
        """The generation bump still stops the work in flight, but the tile keeps
        what it owes and the way back re-issues it under the new generation."""
        poster = "https://cdn.test/poster/aborted.jpg"
        tile = _FakePicture("", mapped=False)
        with self._submits() as submit:
            movie_widget.load_image_into_picture(poster, tile, width=130, height=195)
            movie_widget.cancel_pending_image_downloads()
            self.assertEqual(submit.call_count, 0)
            tile._mapped = True
            tile.fire("map")
            self._run_idle()
            self.assertEqual(submit.call_count, 1)

    def test_a_grid_still_being_built_downloads_as_usual(self):
        """A tile not attached to a window yet belongs to a view on its way to the
        screen, so parking it would delay every fresh page."""
        poster = "https://cdn.test/poster/being-built.jpg"
        tile = _FakePicture("", mapped=False, attached=False)
        with self._submits() as submit:
            movie_widget.load_image_into_picture(poster, tile, width=130, height=195)
            self.assertEqual(submit.call_count, 1)

    def test_parked_cards_do_not_burn_their_repair_tries(self):
        poster = "https://cdn.test/poster/waiting.jpg"
        tile = _FakePicture(poster, mapped=False)
        movie_widget._note_blank_tile(tile, poster, 130, 195, True)
        with patch.object(movie_widget, "load_image_into_picture") as load:
            movie_widget._blank_tile_repair_tick()
        load.assert_not_called()
        self.assertEqual(tile._popcornbox_blank_attempts, 0)
        self.assertEqual(len(movie_widget._BLANK_TILES), 1)

    def test_a_painted_card_stops_owing_a_picture(self):
        poster = "https://cdn.test/poster/painted.jpg"
        tile = _FakePicture(poster, mapped=False)
        movie_widget._watch_tile_for_visibility(tile, poster, 130, 195, True)
        with patch.object(movie_widget, "Gdk", MagicMock()):
            movie_widget._apply_pixbuf(tile, object(), poster)
        self.assertIsNone(tile._popcornbox_pending_image)
        with self._submits() as submit:
            tile._mapped = True
            tile.fire("map")
            self._run_idle()
            submit.assert_not_called()

if __name__ == "__main__":
    unittest.main()
