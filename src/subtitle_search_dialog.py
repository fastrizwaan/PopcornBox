import os
import time
import logging
import threading
from gettext import gettext as _

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, GLib, Pango

from . import api

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = [
    ("all", _("All Languages")),
    ("eng", "English"),
    ("spa", "Spanish"),
    ("fre", "French"),
    ("ger", "German"),
    ("ita", "Italian"),
    ("por", "Portuguese"),
    ("pob", "Portuguese (BR)"),
    ("ara", "Arabic"),
    ("hin", "Hindi"),
    ("chi", "Chinese"),
    ("jpn", "Japanese"),
    ("kor", "Korean"),
    ("rus", "Russian"),
    ("tur", "Turkish"),
    ("pol", "Polish"),
    ("dut", "Dutch"),
    ("cze", "Czech"),
    ("ell", "Greek"),
    ("swe", "Swedish"),
    ("ind", "Indonesian"),
    ("mal", "Malayalam"),
    ("tam", "Tamil"),
    ("tel", "Telugu"),
    ("kan", "Kannada"),
    ("ben", "Bengali"),
    ("pan", "Punjabi"),
    ("urd", "Urdu"),
    ("per", "Persian"),
    ("rum", "Romanian"),
    ("hun", "Hungarian"),
    ("ukr", "Ukrainian"),
    ("heb", "Hebrew"),
    ("tha", "Thai"),
    ("vie", "Vietnamese"),
    ("dan", "Danish"),
    ("fin", "Finnish"),
    ("nor", "Norwegian"),
]


class SubtitleSearchDialog(Adw.Dialog):
    """
    Dialog allowing users to search OpenSubtitles and installed subtitle addons,
    filter by language, and insert subtitles directly into MPV.
    """

    def __init__(self, window, title="", imdb_id=None, media_type="movie", season=None, episode=None, **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.initial_title = str(title or "").strip()
        self.initial_imdb_id = imdb_id
        self.initial_media_type = media_type or "movie"
        self.initial_season = season
        self.initial_episode = episode

        self._all_subtitles = []
        self._search_id = 0
        self._is_searching = False

        self.set_title(_("Search Subtitles"))
        self.set_content_width(700)
        self.set_content_height(560)

        # Toolbar View
        self.toolbar_view = Adw.ToolbarView()
        self.set_child(self.toolbar_view)

        # HeaderBar
        self.header_bar = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(
            title=_("Search Subtitles"),
            subtitle=self.initial_title if self.initial_title else _("OpenSubtitles")
        )
        self.header_bar.set_title_widget(self.window_title)
        self.toolbar_view.add_top_bar(self.header_bar)

        # Main vertical container
        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.main_vbox.set_margin_start(16)
        self.main_vbox.set_margin_end(16)
        self.main_vbox.set_margin_top(12)
        self.main_vbox.set_margin_bottom(16)
        self.toolbar_view.set_content(self.main_vbox)

        # Search Controls Row 1: Title Query & Search button
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search movie or series title..."))
        self.search_entry.set_hexpand(True)
        self.search_entry.set_text(self.initial_title)
        self.search_entry.connect("activate", lambda *_: self._start_search())

        self.btn_search = Gtk.Button(label=_("Search"), css_classes=["suggested-action"])
        self.btn_search.connect("clicked", lambda *_: self._start_search())

        search_box.append(self.search_entry)
        search_box.append(self.btn_search)
        self.main_vbox.append(search_box)

        # Search Controls Row 2: Type, Season, Episode, Language filter
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        # Media type toggle
        self.type_dropdown = Gtk.DropDown.new_from_strings([_("Movie"), _("Series")])
        is_series = self.initial_media_type in ["series", "tv", "anime"] or self.initial_season is not None
        self.type_dropdown.set_selected(1 if is_series else 0)
        self.type_dropdown.connect("notify::selected", self._on_type_changed)
        filter_box.append(self.type_dropdown)

        # Season / Episode box
        self.series_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_s = Gtk.Label(label=_("S:"), css_classes=["dim-label"])
        self.season_spin = Gtk.SpinButton.new_with_range(1, 99, 1)
        self.season_spin.set_value(int(self.initial_season or 1))

        lbl_e = Gtk.Label(label=_("E:"), css_classes=["dim-label"])
        self.episode_spin = Gtk.SpinButton.new_with_range(1, 999, 1)
        self.episode_spin.set_value(int(self.initial_episode or 1))

        self.series_box.append(lbl_s)
        self.series_box.append(self.season_spin)
        self.series_box.append(lbl_e)
        self.series_box.append(self.episode_spin)
        self.series_box.set_visible(is_series)
        filter_box.append(self.series_box)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        filter_box.append(spacer)

        # Language dropdown
        lbl_lang = Gtk.Label(label=_("Language:"), css_classes=["dim-label"])
        filter_box.append(lbl_lang)

        lang_labels = [label for _, label in SUPPORTED_LANGUAGES]
        self.lang_dropdown = Gtk.DropDown.new_from_strings(lang_labels)
        self.lang_dropdown.set_selected(0)
        self.lang_dropdown.connect("notify::selected", lambda *_: self._apply_filter())
        filter_box.append(self.lang_dropdown)

        self.main_vbox.append(filter_box)

        # Count / status bar
        self.status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.lbl_count = Gtk.Label(label="", xalign=0, css_classes=["dim-label", "caption"])
        self.status_bar.append(self.lbl_count)
        self.main_vbox.append(self.status_bar)

        # Content Stack: loading, empty, results
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # 1. Loading page
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner()
        self.spinner.set_size_request(40, 40)
        self.lbl_loading = Gtk.Label(label=_("Searching OpenSubtitles..."), css_classes=["title-4", "dim-label"])
        loading_box.append(self.spinner)
        loading_box.append(self.lbl_loading)
        self.stack.add_named(loading_box, "loading")

        # 2. Empty page
        self.empty_page = Adw.StatusPage()
        self.empty_page.set_icon_name("edit-find-symbolic")
        self.empty_page.set_title(_("No Subtitles Found"))
        self.empty_page.set_description(_("Try a different search title, season, episode, or language filter."))
        self.stack.add_named(self.empty_page, "empty")

        # 3. Results page
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.scrolled.set_child(self.list_box)
        self.stack.add_named(self.scrolled, "results")

        self.main_vbox.append(self.stack)

        # Initial state & auto-trigger search if we have title or imdb_id
        if self.initial_title or self.initial_imdb_id:
            GLib.idle_add(self._start_search)
        else:
            self.stack.set_visible_child_name("empty")

    def _on_type_changed(self, dropdown, _param):
        is_series = dropdown.get_selected() == 1
        self.series_box.set_visible(is_series)

    def _start_search(self):
        query = self.search_entry.get_text().strip()
        is_series = self.type_dropdown.get_selected() == 1
        media_type = "series" if is_series else "movie"
        season = int(self.season_spin.get_value()) if is_series else None
        episode = int(self.episode_spin.get_value()) if is_series else None

        # Determine whether to use initial IMDb ID
        imdb_id = self.initial_imdb_id if (self.initial_imdb_id and query == self.initial_title) else None

        self._search_id += 1
        current_id = self._search_id
        self._is_searching = True

        self.stack.set_visible_child_name("loading")
        self.btn_search.set_sensitive(False)
        self.lbl_count.set_text("")
        if query:
            self.window_title.set_subtitle(query)

        def worker():
            logger.info(f"[SUB_SEARCH] Searching query='{query}', imdb_id='{imdb_id}', type='{media_type}', S{season}E{episode}")
            try:
                subs = api.search_subtitles_online(
                    query=query,
                    imdb_id=imdb_id,
                    media_type=media_type,
                    season=season,
                    episode=episode
                )
            except Exception as e:
                logger.error(f"[SUB_SEARCH] Error in search_subtitles_online: {e}")
                subs = []

            def on_finish():
                if current_id != self._search_id:
                    return False
                self._is_searching = False
                self.btn_search.set_sensitive(True)
                self._all_subtitles = subs
                self._apply_filter()
                return False

            GLib.idle_add(on_finish)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_filter(self):
        selected_idx = self.lang_dropdown.get_selected()
        selected_code = SUPPORTED_LANGUAGES[selected_idx][0] if selected_idx < len(SUPPORTED_LANGUAGES) else "all"

        filtered = []
        for s in self._all_subtitles:
            lang_code = str(s.get("lang", "")).lower()
            if selected_code == "all":
                filtered.append(s)
            else:
                if lang_code == selected_code or lang_code.startswith(selected_code) or selected_code.startswith(lang_code):
                    filtered.append(s)
                elif selected_code == "eng" and lang_code in ["en", "eng", "english"]:
                    filtered.append(s)
                elif selected_code == "spa" and lang_code in ["es", "spa", "esp", "spanish"]:
                    filtered.append(s)
                elif selected_code == "por" and lang_code in ["pt", "por", "portuguese"]:
                    filtered.append(s)
                elif selected_code == "pob" and lang_code in ["pob", "pt-br"]:
                    filtered.append(s)

        # Populate list box
        self.list_box.remove_all()

        if not filtered:
            self.stack.set_visible_child_name("empty")
            self.lbl_count.set_text(_("0 subtitles found"))
            return

        self.stack.set_visible_child_name("results")
        count_text = _("Showing {} subtitle(s)").format(len(filtered))
        if selected_code != "all":
            lang_name = SUPPORTED_LANGUAGES[selected_idx][1]
            count_text += f" ({lang_name})"
        self.lbl_count.set_text(count_text)

        for item in filtered:
            row = self._create_row(item)
            self.list_box.append(row)

    def _create_row(self, item):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_margin_top(8)
        hbox.set_margin_bottom(8)
        hbox.set_margin_start(12)
        hbox.set_margin_end(12)

        # Language badge
        lang_code = item.get("lang", "und").upper()[:4]
        badge = Gtk.Label(label=lang_code)
        badge.add_css_class("caption-heading")
        badge.add_css_class("badge")
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_size_request(42, -1)
        hbox.append(badge)

        # Middle column: Release title and metadata
        mid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mid_box.set_hexpand(True)

        name = item.get("release_name") or item.get("file_name") or _("Subtitle")
        title_lbl = Gtk.Label(label=name, xalign=0)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_tooltip_text(name)
        mid_box.append(title_lbl)

        # Subtitle details: Language name, source, format, fps
        parts = [item.get("lang_name", "")]
        if item.get("source"):
            parts.append(item.get("source"))
        if item.get("fps"):
            parts.append(item.get("fps"))
        if item.get("format"):
            parts.append(item.get("format"))

        details_str = " • ".join([p for p in parts if p])
        details_lbl = Gtk.Label(label=details_str, xalign=0, css_classes=["dim-label", "caption"])
        mid_box.append(details_lbl)
        hbox.append(mid_box)

        # Right: Apply button
        btn_apply = Gtk.Button(label=_("Apply"), css_classes=["pill", "suggested-action"], valign=Gtk.Align.CENTER)
        btn_apply.connect("clicked", lambda b, it=item: self._download_and_apply(it, b))
        hbox.append(btn_apply)

        row.set_child(hbox)
        return row

    def _download_and_apply(self, item, btn):
        btn.set_sensitive(False)
        btn.set_label(_("Applying..."))

        url = item.get("url")
        if not url:
            btn.set_sensitive(True)
            btn.set_label(_("Apply"))
            return

        lang = item.get("lang", "en")
        raw_name = item.get("release_name") or item.get("file_name") or "sub"
        clean_name = "".join([c for c in raw_name if c.isalnum() or c in (" ", "-", "_", ".")]).rstrip()

        def download_worker():
            filename = f"opensub_{int(time.time())}_{lang}_{clean_name[:40]}.srt"
            file_path = api.download_subtitle(url, filename)

            def on_download_done():
                if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    try:
                        # In MPV: add subtitle, select it, and turn on sub-visibility
                        track_title = f"OpenSubtitles ({lang.upper()}) - {clean_name[:35]}"
                        self.window.mpv.command("sub-add", file_path, "select", track_title, lang)
                        self.window.mpv["sub-visibility"] = "yes"
                        self.window._show_toast(_("Loaded subtitle: {} ({})").format(clean_name[:30], lang.upper()))
                        logger.info(f"[SUB_SEARCH] Successfully injected subtitle track: {track_title} from {file_path}")
                        self.close()
                    except Exception as e:
                        logger.error(f"[SUB_SEARCH] Error loading subtitle in MPV: {e}")
                        btn.set_sensitive(True)
                        btn.set_label(_("Failed"))
                else:
                    logger.warning(f"[SUB_SEARCH] Failed to download subtitle from {url}")
                    btn.set_sensitive(True)
                    btn.set_label(_("Failed - Retry"))
                    self.window._show_toast(_("Failed to download subtitle file"))
                return False

            GLib.idle_add(on_download_done)

        threading.Thread(target=download_worker, daemon=True).start()
