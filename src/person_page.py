# person_page.py
#
# Copyright 2026 Asif Ali Rizvan
#
# SPDX-License-Identifier: GPL-3.0-or-later

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, GLib, Pango, Gio
import threading
import urllib.parse
from gettext import gettext as _
from .movie_widget import MovieWidget, load_image_into_picture, cancel_pending_image_downloads
from .tmdb_helper import fetch_person_details
from .utils import open_uri


class PersonPage(Gtk.Overlay):
    """
    Full-page view displaying an actor or creator's profile, biography, birth info,
    and an interactive, responsive filmography grid with category filtering.
    """

    def __init__(self, window, person_id, person_name="", photo_url=None, on_back=None, **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.person_id = person_id
        self.person_name = person_name or ""
        self.photo_url = photo_url
        self.on_back = on_back
        self.person_data = {
            "id": person_id,
            "name": person_name,
            "photo_url": photo_url
        }

        self._bio_expanded = False
        self._destroyed = False
        self._current_filter = "all"
        self._all_filmography = []

        # Ambient backdrop
        self.backdrop_pic = Gtk.Picture()
        self.backdrop_pic.set_can_shrink(True)
        self.backdrop_pic.set_opacity(0.18)
        self.backdrop_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.backdrop_pic.set_hexpand(True)
        self.backdrop_pic.set_vexpand(True)
        self.set_child(self.backdrop_pic)

        # Main vertical layout container
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_overlay(self.main_box)

        # HeaderBar matching PopcornBox's details styling
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")
        self.header_bar.add_css_class("details-headerbar")
        self.header_bar.set_show_end_title_buttons(True)

        self.header_title = Adw.WindowTitle(title=self.person_name or _("Person Details"))
        self.header_bar.set_title_widget(self.header_title)

        # Back Button
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.set_tooltip_text(_("Back"))
        back_btn.add_css_class("flat")
        back_btn.connect("clicked", lambda b: self._on_back_clicked())
        self.header_bar.pack_start(back_btn)

        # Reload Button
        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.reload_btn.set_tooltip_text(_("Reload Details"))
        self.reload_btn.add_css_class("flat")
        self.reload_btn.connect("clicked", lambda b: self.reload_details())
        self.header_bar.pack_start(self.reload_btn)

        # Window Menu Button
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text(_("Menu"))
        menu_btn.add_css_class("flat")
        if self.window and hasattr(self.window, "primary_menu_btn") and self.window.primary_menu_btn:
            menu_model = self.window.primary_menu_btn.get_menu_model()
            if menu_model and isinstance(menu_model, Gio.MenuModel):
                menu_btn.set_menu_model(menu_model)
        self.header_bar.pack_end(menu_btn)

        self.main_box.append(self.header_bar)

        # Scrolled content container for fluid resizing
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.main_box.append(self.scrolled)

        # Main content box with generous responsive padding
        self.content_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.content_vbox.set_margin_start(28)
        self.content_vbox.set_margin_end(28)
        self.content_vbox.set_margin_top(16)
        self.content_vbox.set_margin_bottom(32)
        self.content_vbox.set_hexpand(True)
        self.scrolled.set_child(self.content_vbox)

        # Loading Spinner Box
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.set_halign(Gtk.Align.CENTER)
        self.loading_box.set_vexpand(True)
        self.loading_box.set_margin_top(80)
        self.loading_box.set_margin_bottom(80)

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(44, 44)
        self.spinner.start()
        self.loading_box.append(self.spinner)

        self.loading_label = Gtk.Label(label=_("Loading filmography for {}...").format(self.person_name) if self.person_name else _("Loading details..."))
        self.loading_label.add_css_class("dim-label")
        self.loading_box.append(self.loading_label)

        self.content_vbox.append(self.loading_box)

        # Profile Hero Box
        self.hero_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=26)
        self.hero_box.set_visible(False)
        self.hero_box.add_css_class("person-hero-box")
        self.content_vbox.append(self.hero_box)

        # Headshot Picture Frame
        self.portrait_frame = Gtk.Box()
        self.portrait_frame.add_css_class("person-portrait-frame")
        self.portrait_frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.portrait_frame.set_valign(Gtk.Align.START)

        self.portrait_pic = Gtk.Picture()
        self.portrait_pic.set_size_request(180, 260)
        self.portrait_pic.set_can_shrink(True)
        self.portrait_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.portrait_pic.add_css_class("person-portrait")
        self.portrait_frame.append(self.portrait_pic)
        self.hero_box.append(self.portrait_frame)

        # Person Info Column
        self.info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.info_vbox.set_hexpand(True)
        self.info_vbox.set_valign(Gtk.Align.START)
        self.hero_box.append(self.info_vbox)

        # Name row with action buttons
        title_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_hbox.set_valign(Gtk.Align.CENTER)

        self.name_label = Gtk.Label(label=self.person_name)
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.add_css_class("title-1")
        self.name_label.set_wrap(True)
        title_hbox.append(self.name_label)

        self.g_btn = Gtk.Button(icon_name="goa-account-google-symbolic")
        self.g_btn.set_tooltip_text(_("Search Online"))
        self.g_btn.add_css_class("flat")
        self.g_btn.add_css_class("circular")
        self.g_btn.connect("clicked", self._on_google_search)
        title_hbox.append(self.g_btn)

        self.info_vbox.append(title_hbox)

        # Meta row (Known for, Birth/Death info)
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

        self.bio_toggle_btn = Gtk.Button(label=_("Read more"))
        self.bio_toggle_btn.set_halign(Gtk.Align.START)
        self.bio_toggle_btn.add_css_class("flat")
        self.bio_toggle_btn.add_css_class("person-bio-toggle")
        self.bio_toggle_btn.set_visible(False)
        self.bio_toggle_btn.connect("clicked", self._on_toggle_bio)
        self.info_vbox.append(self.bio_toggle_btn)

        # Filmography section
        self.filmo_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.filmo_section.set_visible(False)
        self.content_vbox.append(self.filmo_section)

        # Filmography Header + Filter pills row
        filmo_header_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        filmo_header_hbox.set_valign(Gtk.Align.CENTER)

        self.filmo_header = Gtk.Label(label=_("Filmography"))
        self.filmo_header.set_halign(Gtk.Align.START)
        self.filmo_header.add_css_class("title-2")
        filmo_header_hbox.append(self.filmo_header)

        # Filter Toggles Box
        self.filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.filter_box.add_css_class("linked")
        self.filter_box.set_halign(Gtk.Align.START)

        self.filter_all_btn = Gtk.ToggleButton(label=_("All"), active=True)
        self.filter_all_btn.add_css_class("person-filter-btn")
        self.filter_all_btn.connect("toggled", lambda b: self._on_filter_changed("all", b))
        self.filter_box.append(self.filter_all_btn)

        self.filter_movies_btn = Gtk.ToggleButton(label=_("Movies"), group=self.filter_all_btn)
        self.filter_movies_btn.add_css_class("person-filter-btn")
        self.filter_movies_btn.connect("toggled", lambda b: self._on_filter_changed("movie", b))
        self.filter_box.append(self.filter_movies_btn)

        self.filter_series_btn = Gtk.ToggleButton(label=_("Series"), group=self.filter_all_btn)
        self.filter_series_btn.add_css_class("person-filter-btn")
        self.filter_series_btn.connect("toggled", lambda b: self._on_filter_changed("series", b))
        self.filter_box.append(self.filter_series_btn)

        filmo_header_hbox.append(self.filter_box)
        self.filmo_section.append(filmo_header_hbox)

        # Responsive FlowBox Filmography Grid
        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flowbox.set_homogeneous(False)
        self.flowbox.set_max_children_per_line(12)
        self.flowbox.set_min_children_per_line(2)
        self.flowbox.set_column_spacing(16)
        self.flowbox.set_row_spacing(20)
        self.flowbox.set_halign(Gtk.Align.FILL)
        self.flowbox.set_hexpand(True)
        self.filmo_section.append(self.flowbox)

        # Initial photo preview if available
        if self.photo_url:
            load_image_into_picture(self.photo_url, self.portrait_pic, width=180, height=260)
            load_image_into_picture(self.photo_url, self.backdrop_pic, width=1280, height=720, crop=False)

        # Fetch details asynchronously
        self._fetch_thread = threading.Thread(target=self._fetch_details_thread, daemon=True)
        self._fetch_thread.start()

    def _on_back_clicked(self):
        self.destroy_page()
        if self.on_back:
            self.on_back()

    def _on_google_search(self, btn):
        query = f'"{self.person_name}" actor filmography' if self.person_name else "filmography"
        open_uri(f"https://www.google.com/search?q={urllib.parse.quote(query)}", self.window)

    def reload_details(self):
        self.loading_box.set_visible(True)
        self.spinner.start()
        self.hero_box.set_visible(False)
        self.filmo_section.set_visible(False)
        threading.Thread(target=self._fetch_details_thread, daemon=True).start()

    def _on_toggle_bio(self, btn):
        self._bio_expanded = not self._bio_expanded
        if self._bio_expanded:
            self.bio_label.set_lines(0)
            self.bio_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            btn.set_label(_("Read less"))
        else:
            self.bio_label.set_lines(4)
            self.bio_label.set_ellipsize(Pango.EllipsizeMode.END)
            btn.set_label(_("Read more"))

    def _fetch_details_thread(self):
        data = fetch_person_details(self.person_id)
        if not self._destroyed:
            GLib.idle_add(self._populate_ui, data)

    def _populate_ui(self, data):
        if self._destroyed:
            return False

        self.spinner.stop()
        self.loading_box.set_visible(False)

        if not data:
            self.loading_label.set_text(_("Could not load details for {}.").format(self.person_name or "this person"))
            self.loading_box.set_visible(True)
            return False

        # Populate name & window title
        name = data.get("name") or self.person_name
        self.person_name = name
        self.header_title.set_title(name)
        self.name_label.set_text(name)

        photo = data.get("photo") or self.photo_url
        if photo:
            self.photo_url = photo
            self.person_data["photo_url"] = photo
            load_image_into_picture(photo, self.portrait_pic, width=180, height=260)
            load_image_into_picture(photo, self.backdrop_pic, width=1280, height=720, crop=False)

        # Build meta details string
        meta_items = []
        known_for = data.get("known_for")
        if known_for:
            meta_items.append(known_for)

        birthday = data.get("birthday")
        birthplace = data.get("place_of_birth")
        deathday = data.get("deathday")

        if birthday and birthplace:
            meta_items.append(_("Born {} in {}").format(birthday, birthplace))
        elif birthday:
            meta_items.append(_("Born {}").format(birthday))
        elif birthplace:
            meta_items.append(_("Born in {}").format(birthplace))

        if deathday:
            meta_items.append(_("Died {}").format(deathday))

        self.meta_label.set_text(" • ".join(meta_items))

        # Biography
        bio = data.get("biography") or ""
        if bio:
            self.bio_label.set_text(bio)
            self.bio_label.set_visible(True)
            if len(bio) > 280:
                self.bio_toggle_btn.set_visible(True)
            else:
                self.bio_toggle_btn.set_visible(False)
        else:
            self.bio_label.set_text(_("No biography available."))
            self.bio_label.set_visible(True)
            self.bio_toggle_btn.set_visible(False)

        self.hero_box.set_visible(True)

        # Filmography
        self._all_filmography = data.get("filmography") or []
        movie_count = sum(1 for m in self._all_filmography if m.get("type") == "movie")
        series_count = sum(1 for m in self._all_filmography if m.get("type") in ["series", "tv", "tv_series"])
        total_count = len(self._all_filmography)

        self.filmo_header.set_text(f"{_('Filmography')} ({total_count})")
        self.filter_all_btn.set_label(f"{_('All')} ({total_count})")
        self.filter_movies_btn.set_label(f"{_('Movies')} ({movie_count})")
        self.filter_series_btn.set_label(f"{_('Series')} ({series_count})")

        self._render_filtered_filmography()
        self.filmo_section.set_visible(True)
        return False

    def _on_filter_changed(self, filter_type, btn):
        if not btn.get_active():
            return
        if self._current_filter == filter_type:
            return
        self._current_filter = filter_type
        self._render_filtered_filmography()

    def _render_filtered_filmography(self):
        while child := self.flowbox.get_first_child():
            self.flowbox.remove(child)

        items_to_show = []
        for item in self._all_filmography:
            mtype = item.get("type") or "movie"
            if self._current_filter == "all":
                items_to_show.append(item)
            elif self._current_filter == "movie" and mtype == "movie":
                items_to_show.append(item)
            elif self._current_filter == "series" and mtype in ["series", "tv", "tv_series"]:
                items_to_show.append(item)

        for item in items_to_show:
            card = MovieWidget(item, self._on_film_clicked)
            self.flowbox.append(card)

    def _on_film_clicked(self, movie_data):
        if self.window and hasattr(self.window, "_on_movie_clicked"):
            self.window._on_movie_clicked(movie_data)

    def destroy_page(self):
        self._destroyed = True
        cancel_pending_image_downloads()
