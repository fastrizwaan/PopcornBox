# company_dialog.py
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
from .tmdb_helper import fetch_company_details


class CompanyDetailDialog(Adw.Dialog):
    """
    Dialog displaying a production company or TV network's profile, logo,
    headquarters/country info, description, and an interactive catalog grid
    of movies and series produced or broadcast by that entity.
    """

    def __init__(self, window, entity_id, entity_name="", logo_url=None, entity_type="company", **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.entity_id = entity_id
        self.entity_name = entity_name or ("Network Details" if entity_type == "network" else "Company Details")
        self.logo_url = logo_url
        self.entity_type = entity_type
        self._desc_expanded = False
        self._current_filter = "all"
        self._catalog_data = None

        self.set_title(self.entity_name)
        self.set_content_width(860)
        self.set_content_height(700)

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

        self.loading_label = Gtk.Label(label=f"Loading catalog for {self.entity_name}...")
        self.loading_label.add_css_class("dim-label")
        self.loading_box.append(self.loading_label)

        self.content_vbox.append(self.loading_box)

        # Hero Box
        self.hero_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        self.hero_box.set_visible(False)
        self.hero_box.add_css_class("company-dialog-hero")
        self.content_vbox.append(self.hero_box)

        # Logo Frame (White container card)
        self.logo_frame = Gtk.Box()
        self.logo_frame.add_css_class("company-dialog-logo-frame")
        self.logo_frame.set_halign(Gtk.Align.START)
        self.logo_frame.set_valign(Gtk.Align.START)

        self.logo_pic = Gtk.Picture()
        self.logo_pic.set_size_request(130, 46)
        self.logo_pic.set_can_shrink(True)
        self.logo_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.logo_pic.add_css_class("company-dialog-logo-pic")
        self.logo_frame.append(self.logo_pic)
        self.hero_box.append(self.logo_frame)

        # Info column
        self.info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.info_vbox.set_hexpand(True)
        self.hero_box.append(self.info_vbox)

        self.name_label = Gtk.Label(label=self.entity_name)
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.add_css_class("title-1")
        self.name_label.set_wrap(True)
        self.info_vbox.append(self.name_label)

        self.meta_label = Gtk.Label(label="")
        self.meta_label.set_halign(Gtk.Align.START)
        self.meta_label.add_css_class("dim-label")
        self.meta_label.set_wrap(True)
        self.info_vbox.append(self.meta_label)

        # Description
        self.desc_label = Gtk.Label(label="")
        self.desc_label.set_halign(Gtk.Align.START)
        self.desc_label.set_xalign(0.0)
        self.desc_label.set_wrap(True)
        self.desc_label.set_lines(4)
        self.desc_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.desc_label.set_visible(False)
        self.info_vbox.append(self.desc_label)

        self.desc_toggle_btn = Gtk.Button(label="Read more")
        self.desc_toggle_btn.set_halign(Gtk.Align.START)
        self.desc_toggle_btn.add_css_class("flat")
        self.desc_toggle_btn.set_visible(False)
        self.desc_toggle_btn.connect("clicked", self._on_toggle_desc)
        self.info_vbox.append(self.desc_toggle_btn)

        # Separator
        self.sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.sep.set_margin_top(4)
        self.sep.set_margin_bottom(4)
        self.sep.set_visible(False)
        self.content_vbox.append(self.sep)

        # Controls Header Box (Title & Filter Pills)
        self.controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.controls_box.set_visible(False)
        self.content_vbox.append(self.controls_box)

        self.catalog_header = Gtk.Label(label="Catalog")
        self.catalog_header.set_halign(Gtk.Align.START)
        self.catalog_header.add_css_class("title-3")
        self.catalog_header.set_hexpand(True)
        self.controls_box.append(self.catalog_header)

        # Filter Segmented Bar
        self.filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.filter_box.add_css_class("linked")
        self.filter_box.set_halign(Gtk.Align.END)
        self.filter_box.set_visible(False)

        self.filter_all_btn = Gtk.ToggleButton(label="All")
        self.filter_all_btn.set_active(True)
        self.filter_all_btn.connect("toggled", lambda b: self._on_filter_changed(b, "all"))
        self.filter_box.append(self.filter_all_btn)

        self.filter_movies_btn = Gtk.ToggleButton(label="Movies")
        self.filter_movies_btn.connect("toggled", lambda b: self._on_filter_changed(b, "movies"))
        self.filter_box.append(self.filter_movies_btn)

        self.filter_series_btn = Gtk.ToggleButton(label="Series")
        self.filter_series_btn.connect("toggled", lambda b: self._on_filter_changed(b, "series"))
        self.filter_box.append(self.filter_series_btn)

        self.controls_box.append(self.filter_box)

        # FlowBox Grid
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

        # Initial preview of logo if provided
        if self.logo_url:
            load_image_into_picture(self.logo_url, self.logo_pic, width=140, height=48, crop=False)

        # Start background fetch
        threading.Thread(target=self._fetch_details_thread, daemon=True).start()

    def _on_toggle_desc(self, btn):
        self._desc_expanded = not self._desc_expanded
        if self._desc_expanded:
            self.desc_label.set_lines(0)
            self.desc_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            btn.set_label("Read less")
        else:
            self.desc_label.set_lines(4)
            self.desc_label.set_ellipsize(Pango.EllipsizeMode.END)
            btn.set_label("Read more")

    def _fetch_details_thread(self):
        data = fetch_company_details(self.entity_id, self.entity_type)
        GLib.idle_add(self._populate_ui, data)

    def _populate_ui(self, data):
        self.spinner.stop()
        self.loading_box.set_visible(False)

        if not data:
            self.loading_label.set_text(f"Could not load catalog for {self.entity_name}.")
            self.loading_box.set_visible(True)
            return False

        self._catalog_data = data

        name = data.get("name") or self.entity_name
        self.set_title(name)
        self.name_label.set_text(name)

        logo = data.get("logo") or self.logo_url
        if logo:
            load_image_into_picture(logo, self.logo_pic, width=140, height=48, crop=False)
        else:
            # Fallback to initials if no logo available
            if self.logo_pic.get_parent() == self.logo_frame:
                self.logo_frame.remove(self.logo_pic)
            fallback_label = Gtk.Label(label=name)
            fallback_label.set_lines(2)
            fallback_label.set_ellipsize(Pango.EllipsizeMode.END)
            fallback_label.add_css_class("company-fallback-label")
            self.logo_frame.append(fallback_label)

        # Meta tags
        meta_items = []
        is_network = data.get("type") == "network"
        meta_items.append("TV Network" if is_network else "Production Company")

        country = data.get("origin_country")
        if country:
            meta_items.append(country)

        hq = data.get("headquarters")
        if hq:
            meta_items.append(hq)

        self.meta_label.set_text(" • ".join(meta_items))

        # Description
        desc = data.get("description") or ""
        if desc:
            self.desc_label.set_text(desc)
            self.desc_label.set_visible(True)
            if len(desc) > 250:
                self.desc_toggle_btn.set_visible(True)

        self.hero_box.set_visible(True)
        self.sep.set_visible(True)
        self.controls_box.set_visible(True)

        # Setup filter buttons if both movies and series exist
        movies = data.get("movies", [])
        series = data.get("series", [])
        all_titles = data.get("all_titles", [])

        if movies and series:
            self.filter_all_btn.set_label(f"All ({len(all_titles)})")
            self.filter_movies_btn.set_label(f"Movies ({len(movies)})")
            self.filter_series_btn.set_label(f"Series ({len(series)})")
            self.filter_box.set_visible(True)
        else:
            self.filter_box.set_visible(False)

        self._render_items(all_titles)
        return False

    def _on_filter_changed(self, button, filter_type):
        if not button.get_active():
            return
        if self._current_filter == filter_type:
            return

        self._current_filter = filter_type

        # Keep toggle buttons mutually exclusive
        if filter_type != "all" and self.filter_all_btn.get_active():
            self.filter_all_btn.set_active(False)
        if filter_type != "movies" and self.filter_movies_btn.get_active():
            self.filter_movies_btn.set_active(False)
        if filter_type != "series" and self.filter_series_btn.get_active():
            self.filter_series_btn.set_active(False)

        if not self._catalog_data:
            return

        if filter_type == "movies":
            items = self._catalog_data.get("movies", [])
        elif filter_type == "series":
            items = self._catalog_data.get("series", [])
        else:
            items = self._catalog_data.get("all_titles", [])

        self._render_items(items)

    def _render_items(self, items):
        count = len(items)
        filter_label = "Movies" if self._current_filter == "movies" else ("Series" if self._current_filter == "series" else "Catalog")
        self.catalog_header.set_text(f"{filter_label} ({count})")

        while child := self.flowbox.get_first_child():
            self.flowbox.remove(child)

        for item in items:
            card = MovieWidget(item, self._on_title_clicked)
            self.flowbox.append(card)

        self.flowbox.set_visible(True)

    def _on_title_clicked(self, movie_data):
        self.close()
        if hasattr(self.window, "_on_movie_clicked"):
            self.window._on_movie_clicked(movie_data)
