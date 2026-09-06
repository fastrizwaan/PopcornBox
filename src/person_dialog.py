# person_dialog.py
#
# Copyright 2026 Asif Ali Rizvan
#
# SPDX-License-Identifier: GPL-3.0-or-later

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, GLib, Pango
import threading
from .movie_widget import MovieWidget, load_image_into_picture
from .tmdb_helper import fetch_person_details


class PersonDetailDialog(Adw.Dialog):
    """
    Dialog displaying an actor or creator's profile, biography, birth info,
    and a filmography grid of movies/series they have acted in or directed.
    """

    def __init__(self, window, person_id, person_name="", photo_url=None, **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.person_id = person_id
        self.person_name = person_name
        self.photo_url = photo_url
        self._bio_expanded = False

        self.set_title(person_name or "Person Details")
        self.set_content_width(840)
        self.set_content_height(680)

        # Toolbar view for standard Libadwaita dialog layout
        self.toolbar_view = Adw.ToolbarView()
        self.set_child(self.toolbar_view)

        # HeaderBar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")
        self.toolbar_view.add_top_bar(self.header_bar)

        # Scrolled content container
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.toolbar_view.set_content(self.scrolled)

        # Main content box
        self.content_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.content_vbox.set_margin_start(24)
        self.content_vbox.set_margin_end(24)
        self.content_vbox.set_margin_top(16)
        self.content_vbox.set_margin_bottom(24)
        self.scrolled.set_child(self.content_vbox)

        # Loading Spinner Box
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.set_halign(Gtk.Align.CENTER)
        self.loading_box.set_vexpand(True)
        self.loading_box.set_margin_top(60)
        self.loading_box.set_margin_bottom(60)

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(40, 40)
        self.spinner.start()
        self.loading_box.append(self.spinner)

        self.loading_label = Gtk.Label(label=f"Loading filmography for {person_name}...")
        self.loading_label.add_css_class("dim-label")
        self.loading_box.append(self.loading_label)

        self.content_vbox.append(self.loading_box)

        # Profile Hero Box
        self.hero_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        self.hero_box.set_visible(False)
        self.hero_box.add_css_class("person-hero-box")
        self.content_vbox.append(self.hero_box)

        # Headshot Picture
        self.portrait_pic = Gtk.Picture()
        self.portrait_pic.set_size_request(130, 185)
        self.portrait_pic.set_can_shrink(True)
        self.portrait_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.portrait_pic.add_css_class("person-portrait")

        self.portrait_frame = Gtk.Box()
        self.portrait_frame.add_css_class("person-portrait-frame")
        self.portrait_frame.append(self.portrait_pic)
        self.hero_box.append(self.portrait_frame)

        # Person Info column
        self.info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.info_vbox.set_hexpand(True)
        self.hero_box.append(self.info_vbox)

        self.name_label = Gtk.Label(label=person_name)
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.add_css_class("title-1")
        self.name_label.set_wrap(True)
        self.info_vbox.append(self.name_label)

        self.meta_label = Gtk.Label(label="")
        self.meta_label.set_halign(Gtk.Align.START)
        self.meta_label.add_css_class("dim-label")
        self.meta_label.set_wrap(True)
        self.info_vbox.append(self.meta_label)

        # Biography section
        self.bio_label = Gtk.Label(label="")
        self.bio_label.set_halign(Gtk.Align.START)
        self.bio_label.set_xalign(0.0)
        self.bio_label.set_wrap(True)
        self.bio_label.set_lines(4)
        self.bio_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.bio_label.add_css_class("person-bio-label")
        self.info_vbox.append(self.bio_label)

        self.bio_toggle_btn = Gtk.Button(label="Read more")
        self.bio_toggle_btn.set_halign(Gtk.Align.START)
        self.bio_toggle_btn.add_css_class("flat")
        self.bio_toggle_btn.add_css_class("person-bio-toggle")
        self.bio_toggle_btn.set_visible(False)
        self.bio_toggle_btn.connect("clicked", self._on_toggle_bio)
        self.info_vbox.append(self.bio_toggle_btn)

        # Separator
        self.sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.sep.set_margin_top(6)
        self.sep.set_margin_bottom(6)
        self.sep.set_visible(False)
        self.content_vbox.append(self.sep)

        # Filmography Section Header
        self.filmo_header = Gtk.Label(label="Filmography")
        self.filmo_header.set_halign(Gtk.Align.START)
        self.filmo_header.add_css_class("title-3")
        self.filmo_header.set_visible(False)
        self.content_vbox.append(self.filmo_header)

        # Filmography Grid
        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flowbox.set_homogeneous(False)
        self.flowbox.set_max_children_per_line(12)
        self.flowbox.set_min_children_per_line(2)
        self.flowbox.set_column_spacing(14)
        self.flowbox.set_row_spacing(18)
        self.flowbox.set_halign(Gtk.Align.FILL)
        self.flowbox.set_hexpand(True)
        self.flowbox.set_visible(False)
        self.content_vbox.append(self.flowbox)

        # Initial photo preview if available
        if self.photo_url:
            load_image_into_picture(self.photo_url, self.portrait_pic, width=130, height=185)

        # Fetch details asynchronously
        threading.Thread(target=self._fetch_details_thread, daemon=True).start()

    def _on_toggle_bio(self, btn):
        self._bio_expanded = not self._bio_expanded
        if self._bio_expanded:
            self.bio_label.set_lines(0)
            self.bio_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            btn.set_label("Read less")
        else:
            self.bio_label.set_lines(4)
            self.bio_label.set_ellipsize(Pango.EllipsizeMode.END)
            btn.set_label("Read more")

    def _fetch_details_thread(self):
        data = fetch_person_details(self.person_id)
        GLib.idle_add(self._populate_ui, data)

    def _populate_ui(self, data):
        self.spinner.stop()
        self.loading_box.set_visible(False)

        if not data:
            self.loading_label.set_text(f"Could not load details for {self.person_name}.")
            self.loading_box.set_visible(True)
            return False

        # Populate name & hero
        name = data.get("name") or self.person_name
        self.set_title(name)
        self.name_label.set_text(name)

        photo = data.get("photo") or self.photo_url
        if photo:
            load_image_into_picture(photo, self.portrait_pic, width=130, height=185)

        # Build meta string
        meta_items = []
        known_for = data.get("known_for")
        if known_for:
            meta_items.append(known_for)

        birthday = data.get("birthday")
        birthplace = data.get("place_of_birth")
        deathday = data.get("deathday")

        if birthday and birthplace:
            meta_items.append(f"Born {birthday} in {birthplace}")
        elif birthday:
            meta_items.append(f"Born {birthday}")
        elif birthplace:
            meta_items.append(f"Born in {birthplace}")

        if deathday:
            meta_items.append(f"Died {deathday}")

        self.meta_label.set_text(" • ".join(meta_items))

        # Biography
        bio = data.get("biography") or ""
        if bio:
            self.bio_label.set_text(bio)
            self.bio_label.set_visible(True)
            if len(bio) > 280:
                self.bio_toggle_btn.set_visible(True)
        else:
            self.bio_label.set_text("No biography available.")
            self.bio_label.set_visible(True)

        self.hero_box.set_visible(True)
        self.sep.set_visible(True)

        # Filmography
        filmography = data.get("filmography") or []
        count = len(filmography)
        self.filmo_header.set_text(f"Filmography ({count})")
        self.filmo_header.set_visible(True)

        # Clear existing flowbox items
        while child := self.flowbox.get_first_child():
            self.flowbox.remove(child)

        for item in filmography:
            card = MovieWidget(item, self._on_film_clicked)
            self.flowbox.append(card)

        self.flowbox.set_visible(True)
        return False

    def _on_film_clicked(self, movie_data):
        self.close()
        if hasattr(self.window, "_on_movie_clicked"):
            self.window._on_movie_clicked(movie_data)
