import unittest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
sys.modules["mpv"] = MagicMock()

import subprocess
from gi.repository import Gtk, Adw, GLib


class TestPersonPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_temp_dir = tempfile.TemporaryDirectory()
        schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
        if schema_path.exists():
            dest = Path(cls.schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
            dest.write_text(schema_path.read_text())
            subprocess.run(["glib-compile-schemas", cls.schema_temp_dir.name], check=True)
            os.environ["GSETTINGS_SCHEMA_DIR"] = cls.schema_temp_dir.name

        from gi.repository import Gio
        res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
        if res_file.exists():
            try:
                res = Gio.Resource.load(str(res_file))
                Gio.resources_register(res)
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls.schema_temp_dir.cleanup()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name) / "popcorn-box" / "config"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "data.json"
        self.cache_db = self.db_dir / "cache.db"

        self.patcher1 = patch("src.database.DB_FILE", self.db_file)
        self.patcher2 = patch("src.database.CONFIG_DIR", self.db_dir)
        self.patcher1.start()
        self.patcher2.start()

    def tearDown(self):
        self.patcher1.stop()
        self.patcher2.stop()
        self.temp_dir.cleanup()

    def test_person_page_init(self):
        from src.person_page import PersonPage
        mock_window = MagicMock()
        mock_on_back = MagicMock()

        with patch("threading.Thread"):
            page = PersonPage(
                window=mock_window,
                person_id="12345",
                person_name="Sandra Lee",
                photo_url="https://example.com/photo.jpg",
                on_back=mock_on_back
            )

        self.assertEqual(page.person_id, "12345")
        self.assertEqual(page.person_name, "Sandra Lee")
        self.assertEqual(page.photo_url, "https://example.com/photo.jpg")
        self.assertEqual(page.person_data["id"], "12345")
        self.assertEqual(page.person_data["name"], "Sandra Lee")
        self.assertIsNotNone(page.flowbox)
        self.assertIsNotNone(page.header_bar)

    def test_person_page_populate_ui(self):
        from src.person_page import PersonPage
        mock_window = MagicMock()
        mock_on_back = MagicMock()

        with patch("threading.Thread"):
            page = PersonPage(
                window=mock_window,
                person_id="12345",
                person_name="Sandra Lee",
                on_back=mock_on_back
            )

        mock_data = {
            "id": 12345,
            "name": "Sandra Lee",
            "biography": "Sandra Lee is an American television chef and author.",
            "birthday": "1966-07-03",
            "place_of_birth": "Santa Monica, California, USA",
            "deathday": None,
            "known_for": "Acting",
            "photo": "https://example.com/sandra.jpg",
            "filmography": [
                {"id": "tmdb:101", "title": "Beat Bobby Flay", "type": "series"},
                {"id": "tmdb:102", "title": "Celebrity Family Feud", "type": "series"},
                {"id": "tmdb:103", "title": "Halloween Baking Championship", "type": "series"},
                {"id": "tmdb:201", "title": "Sandra Lee Special", "type": "movie"}
            ]
        }

        with patch("src.person_page.load_image_into_picture"):
            page._populate_ui(mock_data)

        self.assertEqual(page.name_label.get_text(), "Sandra Lee")
        self.assertIn("Acting", page.meta_label.get_text())
        self.assertIn("1966-07-03", page.meta_label.get_text())
        self.assertIn("Santa Monica", page.meta_label.get_text())
        self.assertEqual(page.bio_label.get_text(), "Sandra Lee is an American television chef and author.")
        self.assertEqual(len(page._all_filmography), 4)
        self.assertEqual(page.filter_all_btn.get_label(), "All (4)")
        self.assertEqual(page.filter_movies_btn.get_label(), "Movies (1)")
        self.assertEqual(page.filter_series_btn.get_label(), "Series (3)")

    def test_person_page_filtering(self):
        from src.person_page import PersonPage
        mock_window = MagicMock()

        with patch("threading.Thread"):
            page = PersonPage(
                window=mock_window,
                person_id="12345",
                person_name="Sandra Lee"
            )

        mock_data = {
            "id": 12345,
            "name": "Sandra Lee",
            "biography": "Short bio",
            "filmography": [
                {"id": "tmdb:101", "title": "Series 1", "type": "series"},
                {"id": "tmdb:201", "title": "Movie 1", "type": "movie"},
                {"id": "tmdb:202", "title": "Movie 2", "type": "movie"}
            ]
        }

        with patch("src.person_page.load_image_into_picture"):
            page._populate_ui(mock_data)

        # Initially 'all' filter has 3 children
        children_count = 0
        child = page.flowbox.get_first_child()
        while child:
            children_count += 1
            child = child.get_next_sibling()
        self.assertEqual(children_count, 3)

        # Filter movies
        page.filter_movies_btn.set_active(True)
        children_count = 0
        child = page.flowbox.get_first_child()
        while child:
            children_count += 1
            child = child.get_next_sibling()
        self.assertEqual(children_count, 2)

        # Filter series
        page.filter_series_btn.set_active(True)
        children_count = 0
        child = page.flowbox.get_first_child()
        while child:
            children_count += 1
            child = child.get_next_sibling()
        self.assertEqual(children_count, 1)

    def test_person_page_back_and_destroy(self):
        from src.person_page import PersonPage
        mock_window = MagicMock()
        mock_on_back = MagicMock()

        with patch("threading.Thread"):
            page = PersonPage(
                window=mock_window,
                person_id="12345",
                person_name="Sandra Lee",
                on_back=mock_on_back
            )

        page._on_back_clicked()
        self.assertTrue(page._destroyed)
        mock_on_back.assert_called_once()

    def test_back_from_person_to_details_instant_reuse(self):
        class MockBox:
            def __init__(self):
                self.children = []
            def get_first_child(self):
                return self.children[0] if self.children else None
            def append(self, child):
                self.children.append(child)
            def remove(self, child):
                if child in self.children:
                    self.children.remove(child)

        from src.window import CineWindow
        win = CineWindow.__new__(CineWindow)
        win.nav_stack = []
        win.main_stack = MagicMock()
        win.library_stack = MagicMock()
        win.category_btn_stack = MagicMock()
        win.details_box = MockBox()
        win.person_box = MockBox()

        # User is on Movie A details
        movie_a = {"id": "tt001", "title": "The Tony Danza Show", "type": "series"}
        mock_details_page_a = MagicMock()
        mock_details_page_a.movie_stub = movie_a
        mock_details_page_a._destroyed = False
        win.details_box.append(mock_details_page_a)

        # Nav stack holds library state
        win.nav_stack.append({"main_page": "library", "library_page": "content", "category_btn": "movies"})

        # User clicks cast member -> open_person_details
        win.main_stack.get_visible_child_name.return_value = "details"
        with patch("src.person_page.PersonPage") as mock_person_page_cls:
            mock_person_page = MagicMock()
            mock_person_page.person_id = 12345
            mock_person_page.person_data = {"id": 12345, "name": "Sandra Lee", "photo_url": None}
            mock_person_page._destroyed = False
            mock_person_page_cls.return_value = mock_person_page
            win.open_person_details(12345, "Sandra Lee", push_history=True)

        # Nav stack now has 2 items, top is details for movie_a
        self.assertEqual(len(win.nav_stack), 2)
        self.assertEqual(win.nav_stack[-1]["main_page"], "details")
        self.assertEqual(win.nav_stack[-1]["movie_data"], movie_a)

        # Movie A is still in details_box and intact!
        self.assertEqual(win.details_box.get_first_child(), mock_details_page_a)
        self.assertFalse(getattr(mock_details_page_a, "_destroyed", False))

        # Current view is person
        win.main_stack.get_visible_child_name.return_value = "person"

        # Now user clicks Back from PersonPage
        with patch.object(win, "_on_movie_clicked") as mock_movie_clicked:
            win._go_back()
            # Must NOT re-create or re-click movie!
            mock_movie_clicked.assert_not_called()
            # Must simply switch stack back to details!
            win.main_stack.set_visible_child_name.assert_called_with("details")
            # Person page must be cleaned up from person_box
            self.assertIsNone(win.person_box.get_first_child())

        # Now on details, len(nav_stack) is 1 (library)
        self.assertEqual(len(win.nav_stack), 1)

    def test_navigation_history_flow(self):
        class MockBox:
            def __init__(self):
                self.children = []
            def get_first_child(self):
                return self.children[0] if self.children else None
            def append(self, child):
                self.children.append(child)
            def remove(self, child):
                if child in self.children:
                    self.children.remove(child)

        from src.window import CineWindow
        win = CineWindow.__new__(CineWindow)
        win.nav_stack = []
        win.main_stack = MagicMock()
        win.library_stack = MagicMock()
        win.category_btn_stack = MagicMock()
        win.details_box = MockBox()
        win.person_box = MockBox()

        # Step 1: User is on library
        win.main_stack.get_visible_child_name.return_value = "library"
        win.library_stack.get_visible_child_name.return_value = "content"
        win.category_btn_stack.get_visible_child_name.return_value = "movies"

        # Step 2: User opens Movie A
        movie_a = {"id": "tt001", "title": "The Tony Danza Show", "type": "series"}
        with patch.object(win, "_on_movie_clicked") as mock_movie_clicked:
            # Simulate _push_current_nav_state before opening Movie A
            win._push_current_nav_state()
            self.assertEqual(len(win.nav_stack), 1)
            self.assertEqual(win.nav_stack[-1]["main_page"], "library")

            # Now on details page for Movie A
            win.main_stack.get_visible_child_name.return_value = "details"
            mock_details_page_a = MagicMock()
            mock_details_page_a.movie_stub = movie_a
            mock_details_page_a._destroyed = False
            win.details_box.append(mock_details_page_a)

            # Step 3: User clicks cast member Sandra Lee -> open_person_details
            with patch("src.person_page.PersonPage") as mock_person_page_cls:
                mock_person_page = MagicMock()
                mock_person_page.person_id = 12345
                mock_person_page.person_data = {"id": 12345, "name": "Sandra Lee", "photo_url": None}
                mock_person_page._destroyed = False
                mock_person_page_cls.return_value = mock_person_page
                win.open_person_details(12345, "Sandra Lee", push_history=True)

            self.assertEqual(len(win.nav_stack), 2)
            self.assertEqual(win.nav_stack[-1]["main_page"], "details")
            self.assertEqual(win.nav_stack[-1]["movie_data"], movie_a)

            # Now on person page for Sandra Lee
            win.main_stack.get_visible_child_name.return_value = "person"

            # Step 4: User clicks a movie card in Sandra Lee's filmography (Movie B: Beat Bobby Flay)
            movie_b = {"id": "tt002", "title": "Beat Bobby Flay", "type": "series"}
            win._push_current_nav_state()
            self.assertEqual(len(win.nav_stack), 3)
            self.assertEqual(win.nav_stack[-1]["main_page"], "person")
            self.assertEqual(win.nav_stack[-1]["person_data"]["name"], "Sandra Lee")

            # Now on details page for Movie B (which replaced details_box child)
            win.main_stack.get_visible_child_name.return_value = "details"
            mock_details_page_b = MagicMock()
            mock_details_page_b.movie_stub = movie_b
            mock_details_page_b._destroyed = False
            while child := win.details_box.get_first_child():
                win.details_box.remove(child)
            win.details_box.append(mock_details_page_b)

            # Step 5: User clicks Back on Movie B -> should restore Sandra Lee's person page
            win._go_back()
            # Since Sandra Lee was intact in person_box, main_stack was set to person directly
            win.main_stack.set_visible_child_name.assert_called_with("person")
            self.assertEqual(len(win.nav_stack), 2)

            # Step 6: User clicks Back on Sandra Lee -> should restore Movie A
            # Since details_box was cleared when Movie B was backed out of, Movie A is recreated via _on_movie_clicked
            win._go_back()
            mock_movie_clicked.assert_called_once_with(movie_a, push_history=False)
            self.assertEqual(len(win.nav_stack), 1)

            # Step 7: User clicks Back on Movie A -> should restore library
            win._go_back()
            win.main_stack.set_visible_child_name.assert_called_with("library")
            self.assertEqual(len(win.nav_stack), 0)


if __name__ == "__main__":
    unittest.main()
