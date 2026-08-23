# window.py
#
# Copyright 2025 Diego Povliuk
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import re
import gi
import mpv
import ctypes
from typing import cast
from gettext import gettext as _
from urllib.parse import urlparse
from time import time
import shlex
import hashlib

from .save_session import (
    save_last_playlist_file,
    restore_last_playlist,
    is_same_playlist,
)

from .utils import (
    logger,
    debug_log,
    get_mouse_bindings,
    parse_nonrepeat_bindings,
    is_local_path,
    get_gpu_vendor,
    format_time,
    get_display_param,
    open_uri,
    idle_add_once,
    timeout_add_once,
    timeout_add_seconds_once,
    display,
    has_host_permission,
    PrimaryClick,
    SecondaryClick,
    MBTN_MAP,
    KEY_REMAP,
    SUB_EXTS,
    SCREENSHOT_DIR,
    CONFIG_DIR,
    INPUT_CONF,
    WATCH_HISTORY_JSONL,
)

from .history import HistoryDialog
from .options import OptionsMenuButton
from .playlist import Playlist, PlaylistItemObj
from .preferences import settings, sync_mpv_with_settings
from .shortcuts import INTERNAL_BINDINGS, populate_shortcuts_dialog_mpv
from .mpris import MPRIS
import threading
import concurrent.futures
from . import database
from .api import fetch_items, fetch_movie_details, get_torrents_streamed
from .movie_widget import MovieWidget, ContinueWatchingWidget

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import Adw, Gio, Gdk, GLib, Gtk, GObject, Pango

def _streams_match(s1, s2):
    if not s1 or not s2: return False
    if s1 is s2: return True
    
    is_http1 = bool(s1.get("is_http") or (isinstance(s1.get("url"), str) and s1.get("url", "").startswith(("http://", "https://")) and not s1.get("hash") and not s1.get("infoHash")))
    is_http2 = bool(s2.get("is_http") or (isinstance(s2.get("url"), str) and s2.get("url", "").startswith(("http://", "https://")) and not s2.get("hash") and not s2.get("infoHash")))
    if is_http1 != is_http2:
        return False

    u1, u2 = s1.get("url") or "", s2.get("url") or ""
    if u1 and u2 and u1.startswith("http") and u2.startswith("http"):
        return u1 == u2
    h1 = (s1.get("hash") or s1.get("infoHash") or "").lower()
    h2 = (s2.get("hash") or s2.get("infoHash") or "").lower()
    if h1 and h2 and h1 == h2:
        idx1 = s1.get("file_index") if s1.get("file_index") is not None else s1.get("fileIdx")
        idx2 = s2.get("file_index") if s2.get("file_index") is not None else s2.get("fileIdx")
        if idx1 is None or idx2 is None or idx1 == idx2:
            return True
    m1 = s1.get("magnet") or ""
    m2 = s2.get("magnet") or ""
    if m1 and m2 and m1 == m2:
        return True
    t1 = (s1.get("stream_title") or s1.get("title") or s1.get("filename") or "").strip()
    t2 = (s2.get("stream_title") or s2.get("title") or s2.get("filename") or "").strip()
    if t1 and t2 and t1 == t2 and not h1 and not h2 and not u1 and not u2:
        return True
    return False

def _find_stream_index(stream, stream_list):
    if not stream or not stream_list: return 0
    for idx, item in enumerate(stream_list):
        if _streams_match(stream, item):
            return idx
    return 0

def _find_stream_index_exact(stream, stream_list):
    if not stream or not stream_list: return -1
    for idx, item in enumerate(stream_list):
        if _streams_match(stream, item):
            return idx
    return -1

libegl = ctypes.CDLL("libEGL.so.1")
egl_get_proc_address = libegl.eglGetProcAddress
egl_get_proc_address.restype = ctypes.c_void_p
egl_get_proc_address.argtypes = [ctypes.c_char_p]

GL_FRAMEBUFFER_BINDING = 0x8CA6
libgl = ctypes.CDLL("libGL.so.1")
glGetIntegerv = libgl.glGetIntegerv
glGetIntegerv.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]

gtk_setts: Gtk.Settings | None = Gtk.Settings.get_default()

DEFAULT_WIDTH, DEFAULT_HEIGHT = 1120, 630


class MovieDetailsPage(Gtk.Overlay):
    def __init__(self, movie, on_back, window=None):
        super().__init__()
        self.movie_stub = movie
        self.window = window
        self.media_type = movie.get("type", "movie")
        
        item_raw_id = str(self.movie_stub.get("id", ""))
        poster_raw = str(self.movie_stub.get("medium_cover_image") or self.movie_stub.get("poster") or "")
        
        if item_raw_id.startswith("bolly:s:") or item_raw_id.startswith("hub:s:") or ":s:" in item_raw_id:
            self.media_type = "series"
        elif item_raw_id.startswith("bolly:m:") or item_raw_id.startswith("hub:m:") or ":m:" in item_raw_id:
            self.media_type = "movie"
        elif self.media_type in ["series", "tvshow", "tv_series"]:
            self.media_type = "series"
        elif self.media_type in ["tv", "channel", "tvchannel"]:
            self.media_type = "tv"
        elif self.media_type in ["music", "radio"]:
            self.media_type = "music"
        elif self.media_type not in ["movie", "series", "anime", "tv", "channel", "tvchannel", "music", "radio"]:
            self.media_type = "movie"

        if item_raw_id.startswith("tpb_ctl:"):
            try:
                import base64, json
                raw_b64 = item_raw_id.split("tpb_ctl:", 1)[1]
                payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
                if payload.get("extra", {}).get("type"):
                    self.media_type = payload["extra"]["type"]
                if payload.get("poster") and not poster_raw:
                    poster_raw = payload["poster"]
            except Exception:
                pass
        if poster_raw and not item_raw_id.startswith("tt"):
            tt_m = re.search(r'\b(tt\d{7,8})\b', poster_raw)
            if tt_m:
                alias_ids = self.movie_stub.get("alias_ids") or []
                if tt_m.group(1) not in alias_ids:
                    self.movie_stub.setdefault("alias_ids", []).append(tt_m.group(1))

        self.selected_season = None
        self.selected_episode = None
        self.torrents = []
        self._last_torrents_hash = None  # For change detection to skip redundant UI rebuilds
        self.videos = []
        self.seasons = []
        self.current_episodes = []
        self.selected_torrent = None
        self.quality_buttons = []
        self._restoring_state = False
        self._destroyed = False
        self._last_played_magnet = None
        self._last_played_file_index = None
        self._auto_play_next = False
        self._auto_play_on_streams_loaded = False
        self._details_fetch_id = 0
        self._fetch_gen = 0
        self._last_fetch_key = None
        # Trailer state
        self._trailer_signal_gen = 0       # Bumped on each safe trailer btn reconnect
        self._resolved_trailer_url = None  # Pre-resolved direct stream URL for instant playback
        self._trailer_fast_abort = None    # threading.Event to cancel fast-fetch on navigation
        

        self.remembered_working_stream = None
        from . import database
        item_id = self.movie_stub.get("alias_ids") or self.movie_stub.get("id") or self.movie_stub.get("imdb_id")
        primary_id = item_id[0] if isinstance(item_id, list) else item_id
        if primary_id:
            self.remembered_working_stream = database.get_working_stream(primary_id, self.selected_season, self.selected_episode)
            if self.remembered_working_stream:
                self.selected_torrent = self.remembered_working_stream
        
        self.backdrop_pic = Gtk.Picture()
        self.backdrop_pic.set_can_shrink(True)
        self.backdrop_pic.set_opacity(0.3)
        self.backdrop_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.backdrop_pic.set_hexpand(True)
        self.backdrop_pic.set_vexpand(True)
        self.set_child(self.backdrop_pic)
        
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_overlay(self.main_box)
        
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")
        self.header_bar.add_css_class("details-headerbar")
        self.header_bar.set_show_end_title_buttons(True)

        self.header_title = Adw.WindowTitle(title="PopcornBox")
        self.header_bar.set_title_widget(self.header_title)

        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.set_tooltip_text("Back")
        back_btn.add_css_class("flat")
        def on_back_clicked(btn):
            on_back()
        back_btn.connect("clicked", on_back_clicked)
        self.header_bar.pack_start(back_btn)

        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.reload_btn.set_tooltip_text("Reload Details & Streams")
        self.reload_btn.add_css_class("flat")
        def on_reload(btn):
            from . import database, api
            item_id = self.movie_stub.get("alias_ids") or self.movie_stub.get("id")
            primary_id = item_id[0] if isinstance(item_id, list) else item_id
            existing = database.get_cached_metadata(primary_id)
            saved_poster = None
            saved_bg = None
            if existing:
                p = existing.get("medium_cover_image", "")
                b = existing.get("background", "")
                if p: saved_poster = p
                if b: saved_bg = b
            database.delete_cached_metadata(primary_id)
            if saved_poster or saved_bg:
                stub = existing.copy() if existing else {}
                if saved_poster: stub["medium_cover_image"] = saved_poster
                if saved_bg: stub["background"] = saved_bg
                stub["id"] = primary_id
                database.save_cached_metadata(primary_id, self.media_type, stub)
            cache_key = api.get_stream_cache_key(primary_id, self.media_type, getattr(self, 'selected_season', None), getattr(self, 'selected_episode', None))
            database.delete_cached_streams(cache_key)
            self._ui_built = False
            self.load_details_async(force_refresh=True)
            self.fetch_torrents_async(force=True)
        self.reload_btn.connect("clicked", on_reload)
        self.header_bar.pack_start(self.reload_btn)


        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text("Menu")
        menu_btn.add_css_class("flat")
        if self.window and hasattr(self.window, "primary_menu_btn") and self.window.primary_menu_btn:
            menu_model = self.window.primary_menu_btn.get_menu_model()
            if menu_model:
                menu_btn.set_menu_model(menu_model)
        self.header_bar.pack_end(menu_btn)

        self.main_box.append(self.header_bar)

        # Split Layout Container (Left: Overview, Right: Fixed Sidebar)
        self.split_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.split_hbox.set_margin_start(24)
        self.split_hbox.set_margin_end(24)
        self.split_hbox.set_margin_top(12)
        self.split_hbox.set_margin_bottom(24)
        self.split_hbox.set_vexpand(True)
        self.split_hbox.set_hexpand(True)
        self.main_box.append(self.split_hbox)

        # Left Overview Column inside ScrolledWindow
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_hexpand(True)
        left_scroll.set_vexpand(True)
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.info_vbox.set_margin_end(12)
        left_scroll.set_child(self.info_vbox)

        left_meta_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        
        self.poster = Gtk.Picture()
        self.poster.set_can_shrink(True)
        self.poster.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.poster.set_size_request(220, 330)
        self.poster.set_halign(Gtk.Align.START)
        self.poster.set_margin_top(12)

        meta_detail_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        meta_detail_vbox.set_hexpand(True)

        title_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_hbox.set_valign(Gtk.Align.START)
        
        title_str = self.movie_stub.get("title", "")
        self.title_label = Gtk.Label(label=title_str if title_str else "Loading...")
        self.title_label.add_css_class("title-1")
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_wrap(True)
        title_hbox.append(self.title_label)

        action_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_hbox.set_valign(Gtk.Align.CENTER)

        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_btn.set_tooltip_text("Copy Title")
        self.copy_btn.add_css_class("flat")
        self.copy_btn.add_css_class("circular")
        action_hbox.append(self.copy_btn)

        self.detail_fav_btn = Gtk.Button(icon_name="non-starred-symbolic")
        self.detail_fav_btn.set_tooltip_text("Add to Favorites")
        self.detail_fav_btn.add_css_class("flat")
        self.detail_fav_btn.add_css_class("circular")
        action_hbox.append(self.detail_fav_btn)

        self.detail_seen_btn = Gtk.Button(icon_name="eye-closed-symbolic")
        self.detail_seen_btn.set_tooltip_text("Mark as Seen")
        self.detail_seen_btn.add_css_class("flat")
        self.detail_seen_btn.add_css_class("circular")
        action_hbox.append(self.detail_seen_btn)

        self.g_btn = Gtk.Button(icon_name="goa-account-google-symbolic")
        self.g_btn.set_tooltip_text("Search Online")
        self.g_btn.add_css_class("flat")
        self.g_btn.add_css_class("circular")
        action_hbox.append(self.g_btn)

        self.continue_btn = Gtk.Button()
        self.continue_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.continue_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        self.continue_label = Gtk.Label(label="Continue")
        self.continue_box.append(self.continue_icon)
        self.continue_box.append(self.continue_label)
        self.continue_btn.set_child(self.continue_box)
        self.continue_btn.add_css_class("continue-details-btn")
        self.continue_btn.set_tooltip_text("Continue Watching")
        self.continue_btn.set_visible(False)
        action_hbox.append(self.continue_btn)
        self._continue_btn_hid = None

        self.trailer_btn = Gtk.Button()
        self.trailer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.trailer_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        self.trailer_spinner = Gtk.Spinner()
        self.trailer_spinner.set_visible(False)
        self.trailer_label = Gtk.Label(label="Trailer")
        self.trailer_box.append(self.trailer_icon)
        self.trailer_box.append(self.trailer_spinner)
        self.trailer_box.append(self.trailer_label)
        self.trailer_btn.set_child(self.trailer_box)
        self.trailer_btn.add_css_class("trailer-btn")
        self.trailer_btn.set_tooltip_text("Watch Trailer")
        action_hbox.append(self.trailer_btn)

        title_flowbox = Gtk.FlowBox()
        title_flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        title_flowbox.set_column_spacing(6)
        title_flowbox.set_row_spacing(8)
        title_flowbox.set_valign(Gtk.Align.START)
        title_flowbox.set_halign(Gtk.Align.START)
        
        # In a FlowBox, children wrap based on their natural sizes.
        # We append both the title and action boxes to it.
        title_flowbox.append(title_hbox)
        title_flowbox.append(action_hbox)
        
        meta_detail_vbox.append(title_flowbox)

        meta_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta_hbox.set_valign(Gtk.Align.CENTER)

        year_str = self.movie_stub.get("year", "")
        self.meta_label = Gtk.Label(label=str(year_str) if year_str else "")
        self.meta_label.set_halign(Gtk.Align.START)
        self.meta_label.set_wrap(True)
        self.meta_label.add_css_class("dim-label")
        meta_hbox.append(self.meta_label)

        self.meta_dot = Gtk.Label(label="•")
        self.meta_dot.add_css_class("dim-label")
        meta_hbox.append(self.meta_dot)

        self.imdb_btn = Gtk.Button(label="IMDb 0.0")
        self.imdb_btn.add_css_class("flat")
        meta_hbox.append(self.imdb_btn)

        meta_detail_vbox.append(meta_hbox)

        self.desc_label = Gtk.Label(label="")
        self.desc_label.set_wrap(True)
        self.desc_label.set_halign(Gtk.Align.START)
        self.desc_label.set_max_width_chars(80)
        self.desc_label.set_margin_top(4)
        meta_detail_vbox.append(self.desc_label)

        self.cast_label = Gtk.Label(label="")
        self.cast_label.set_wrap(True)
        self.cast_label.set_halign(Gtk.Align.START)
        self.cast_label.set_max_width_chars(80)
        self.cast_label.add_css_class("dim-label")
        self.cast_label.set_visible(False)
        meta_detail_vbox.append(self.cast_label)

        action_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        action_row2.set_margin_top(4)

        self.play_next_check = Gtk.CheckButton(label="Play Next Ep")
        self.play_next_check.set_valign(Gtk.Align.CENTER)
        from .preferences import settings
        settings.bind("auto-play-next", self.play_next_check, "active", Gio.SettingsBindFlags.DEFAULT)
        self.play_next_check.set_visible(self.media_type in ["series", "anime"])
        action_row2.append(self.play_next_check)

        meta_detail_vbox.append(action_row2)
        meta_detail_vbox.append(self.poster)

        left_meta_hbox.append(meta_detail_vbox)
        self.info_vbox.append(left_meta_hbox)
        self.split_hbox.append(left_scroll)

        # Right Column (Fixed Glassmorphism Sidebar)
        self.sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.sidebar_box.set_size_request(420, -1)
        self.sidebar_box.set_hexpand(False)
        self.sidebar_box.set_vexpand(True)
        self.sidebar_box.add_css_class("details-sidebar")
        self.split_hbox.append(self.sidebar_box)

        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.sidebar_stack.set_transition_duration(250)
        self.sidebar_box.append(self.sidebar_stack)

        # Page 1: Episodes Browser
        self.episodes_page_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.sidebar_stack.add_named(self.episodes_page_vbox, "episodes")

        # Season Nav Box
        self.season_nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.season_nav_box.add_css_class("season-nav-box")

        self.prev_season_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.prev_season_btn.set_tooltip_text("Previous Season")
        self.prev_season_btn.add_css_class("circular")
        self.prev_season_btn.add_css_class("flat")
        self.prev_season_btn.connect("clicked", lambda b: self._change_season_delta(-1))
        self.season_nav_box.append(self.prev_season_btn)

        self.season_dropdown = Gtk.DropDown.new_from_strings([])
        self.season_dropdown.set_valign(Gtk.Align.CENTER)
        self.season_dropdown.set_hexpand(True)
        self.season_nav_box.append(self.season_dropdown)

        self.next_season_btn = Gtk.Button(icon_name="go-next-symbolic")
        self.next_season_btn.set_tooltip_text("Next Season")
        self.next_season_btn.add_css_class("circular")
        self.next_season_btn.add_css_class("flat")
        self.next_season_btn.connect("clicked", lambda b: self._change_season_delta(1))
        self.season_nav_box.append(self.next_season_btn)

        self.episodes_page_vbox.append(self.season_nav_box)

        # Search Entry for Episodes
        self.ep_search_entry = Gtk.SearchEntry()
        self.ep_search_entry.set_placeholder_text("Search videos / episodes...")
        self.ep_search_entry.add_css_class("ep-search-entry")
        self.ep_search_entry.connect("search-changed", lambda e: self.render_episodes_list())
        self.episodes_page_vbox.append(self.ep_search_entry)

        # Episodes List Scrolled Box
        episodes_scroll = Gtk.ScrolledWindow()
        episodes_scroll.set_vexpand(True)
        episodes_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.episodes_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        episodes_scroll.set_child(self.episodes_list_box)
        self.episodes_page_vbox.append(episodes_scroll)

        self.streams_page_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.sidebar_stack.add_named(self.streams_page_vbox, "streams")

        # Set default visible stack child based on initial media_type
        if self.media_type in ["series", "anime"]:
            self.sidebar_stack.set_visible_child_name("episodes")
        else:
            self.sidebar_stack.set_visible_child_name("streams")

        # Streams Header
        self.stream_header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.stream_header_box.set_valign(Gtk.Align.CENTER)

        self.stream_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.stream_back_btn.set_tooltip_text("Back to Episodes" if self.media_type in ["series", "anime"] else "Back to Movies")
        self.stream_back_btn.add_css_class("circular")
        self.stream_back_btn.add_css_class("flat")
        def on_stream_back_clicked(b):
            self._user_navigated_to_streams = False
            if hasattr(self, 'stream_ep_title_label'):
                self.stream_ep_title_label.set_visible(False)
            self.sidebar_stack.set_visible_child_name("episodes")
        self.stream_back_btn.connect("clicked", on_stream_back_clicked)
        self.stream_header_box.append(self.stream_back_btn)

        self.streams_page_vbox.append(self.stream_header_box)

        self.stream_ep_title_label = Gtk.Label(label="")
        self.stream_ep_title_label.add_css_class("stream-ep-title")
        self.stream_ep_title_label.set_halign(Gtk.Align.START)
        self.stream_ep_title_label.set_wrap(True)
        self.stream_ep_title_label.set_visible(False)
        self.streams_page_vbox.append(self.stream_ep_title_label)

        # Source Filter Toggle Box (All / Direct / Torrents)
        self.source_segmented_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.source_segmented_box.add_css_class("linked")
        self.source_segmented_box.set_hexpand(True)
        self.source_segmented_box.set_halign(Gtk.Align.START)

        self.source_all_btn = Gtk.ToggleButton(label="All")
        self.source_direct_btn = Gtk.ToggleButton(label="⚡ Direct")
        self.source_torrent_btn = Gtk.ToggleButton(label="🧲 Torrents")
        self.source_direct_btn.set_group(self.source_all_btn)
        self.source_torrent_btn.set_group(self.source_all_btn)

        self.source_segmented_box.append(self.source_all_btn)
        self.source_segmented_box.append(self.source_direct_btn)
        self.source_segmented_box.append(self.source_torrent_btn)

        from . import database
        saved_source = database.get_setting("default_source_idx", 0)
        try:
            self.source_idx = int(saved_source)
        except Exception:
            self.source_idx = 0

        self._ignore_source_toggle_signals = True
        if self.source_idx == 1:
            self.source_direct_btn.set_active(True)
        elif self.source_idx == 2:
            self.source_torrent_btn.set_active(True)
        else:
            self.source_all_btn.set_active(True)
        self._ignore_source_toggle_signals = False

        def on_source_toggled(btn):
            if getattr(self, '_ignore_source_toggle_signals', False):
                return
            if self.source_all_btn.get_active(): self.source_idx = 0
            elif self.source_direct_btn.get_active(): self.source_idx = 1
            elif self.source_torrent_btn.get_active(): self.source_idx = 2
            
            from . import database
            database.set_setting("default_source_idx", self.source_idx)
            
            if hasattr(self, 'check_live_btn'):
                self.check_live_btn.set_visible(self.source_idx == 1)

            self.update_quality_dropdown()

        self.source_all_btn.connect("toggled", on_source_toggled)
        self.source_direct_btn.connect("toggled", on_source_toggled)
        self.source_torrent_btn.connect("toggled", on_source_toggled)

        self.stream_header_box.append(self.source_segmented_box)

        # Provider Filter Dropdown
        self.provider_dropdown = Gtk.DropDown.new_from_strings(["All Providers"])
        self.provider_dropdown.set_valign(Gtk.Align.CENTER)

        def on_provider_changed(dd, pspec):
            if getattr(self, '_ignore_provider_signals', False):
                return

            model = self.provider_dropdown.get_model()
            sel_idx = self.provider_dropdown.get_selected()
            if model and sel_idx != Gtk.INVALID_LIST_POSITION and sel_idx < model.get_n_items():
                sel_prov = model.get_string(sel_idx)
                if sel_prov != "All Providers":
                    matching = [t for t in getattr(self, 'torrents', []) if sel_prov in t.get("addon_names", [])]
                    if matching:
                        all_http = all(t.get('is_http') for t in matching)
                        all_torrent = all(not t.get('is_http') for t in matching)
                        if all_http and getattr(self, 'source_idx', 0) != 1:
                            self._ignore_source_toggle_signals = True
                            try:
                                self.source_idx = 1
                                self.source_direct_btn.set_active(True)
                                if hasattr(self, 'check_live_btn'):
                                    self.check_live_btn.set_visible(True)
                            finally:
                                self._ignore_source_toggle_signals = False
                        elif all_torrent and getattr(self, 'source_idx', 0) != 2:
                            self._ignore_source_toggle_signals = True
                            try:
                                self.source_idx = 2
                                self.source_torrent_btn.set_active(True)
                                if hasattr(self, 'check_live_btn'):
                                    self.check_live_btn.set_visible(False)
                            finally:
                                self._ignore_source_toggle_signals = False

            self.update_quality_dropdown()

        self.provider_dropdown.connect("notify::selected", on_provider_changed)
        self.stream_header_box.append(self.provider_dropdown)

        # Quality Row Box (Quality Pills & Check Live Button)
        self.quality_row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.quality_row_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.quality_row_hbox.set_valign(Gtk.Align.CENTER)

        self.quality_button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.quality_button_box.add_css_class("linked")
        self.quality_row_hbox.append(self.quality_button_box)

        self.check_live_btn = Gtk.Button(label="⚡ Check Direct")
        self.check_live_btn.add_css_class("pill")
        self.check_live_btn.set_valign(Gtk.Align.CENTER)
        self.check_live_btn.set_size_request(-1, 32)
        self.check_live_btn.set_visible(False)
        self.check_live_btn.set_tooltip_text("Verify live availability of direct HTTP stream links")
        self.check_live_btn.connect("clicked", lambda b: self.run_live_stream_check())
        self.quality_row_hbox.append(self.check_live_btn)

        self.quality_row_box.append(self.quality_row_hbox)

        self.streams_page_vbox.append(self.quality_row_box)

        # Dropdown compatibility placeholders
        self.file_dropdown = Gtk.DropDown.new_from_strings([])
        self.episode_dropdown = Gtk.DropDown.new_from_strings([])

        # Streams List Scrolled Box
        streams_scroll = Gtk.ScrolledWindow()
        streams_scroll.set_vexpand(True)
        streams_scroll.set_overlay_scrolling(False)
        streams_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.streams_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.streams_list_box.set_margin_end(8)
        self.streams_list_box.set_margin_start(2)
        streams_scroll.set_child(self.streams_list_box)
        self.streams_page_vbox.append(streams_scroll)

        # Stream Action Buttons
        self.row4_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        watch_label = "PLAY STREAM" if self.media_type in ["music", "radio", "live"] else "WATCH IT NOW"
        self.watch_btn = Gtk.Button(label=watch_label)
        self.watch_btn.add_css_class("suggested-action")
        self.watch_btn.add_css_class("pill")
        self.watch_btn.set_hexpand(True)
        self.watch_btn.connect("clicked", self.on_watch_clicked)
        self.row4_box.append(self.watch_btn)

        self.download_btn = Gtk.Button(label="Download")
        self.download_btn.add_css_class("pill")
        self.download_btn.connect("clicked", self.on_download_clicked)
        self.row4_box.append(self.download_btn)

        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.add_css_class("pill")
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        self.stop_btn.set_visible(False)
        self.row4_box.append(self.stop_btn)

        self.streams_page_vbox.append(self.row4_box)

        self.progress_label = Gtk.Label(label="")
        self.progress_label.set_halign(Gtk.Align.START)
        self.progress_label.add_css_class("dim-label")
        self.streams_page_vbox.append(self.progress_label)

        # Set default stack page based on media_type / collections
        is_multi_item = (
            self.media_type in ["series", "anime"]
            or str(self.movie_stub.get("id", "")).startswith("ctmdb.")
            or self.movie_stub.get("type") == "collections"
            or "collection" in (self.movie_stub.get("title") or self.movie_stub.get("name") or "").lower()
        )
        if is_multi_item:
            self.sidebar_stack.set_visible_child_name("episodes")
            self.stream_back_btn.set_visible(True)
        else:
            self.sidebar_stack.set_visible_child_name("streams")
            self.stream_back_btn.set_visible(False)

        # Initial Metadata Loading
        self.update_continue_btn()
        item_id = self.movie_stub.get("alias_ids") or self.movie_stub.get("id")
        primary_id = item_id[0] if isinstance(item_id, list) else item_id
        cached_details = database.get_cached_metadata(primary_id)
        if cached_details:
            self.build_ui(cached_details)
            self.load_details_async(force_refresh=False)
        else:
            self.load_details_async(force_refresh=True)

    def _change_season_delta(self, delta):
        if not hasattr(self, 'seasons') or not self.seasons: return
        curr_idx = self.season_dropdown.get_selected()
        if curr_idx == Gtk.INVALID_LIST_POSITION: curr_idx = 0
        new_idx = curr_idx + delta
        if 0 <= new_idx < len(self.seasons):
            self.season_dropdown.set_selected(new_idx)

    def render_episodes_list(self):
        while child := self.episodes_list_box.get_first_child():
            self.episodes_list_box.remove(child)

        if not hasattr(self, 'current_episodes') or not self.current_episodes:
            empty_lbl = Gtk.Label(label="No items available")
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(16)
            self.episodes_list_box.append(empty_lbl)
            return

        query = self.ep_search_entry.get_text().strip().lower() if hasattr(self, 'ep_search_entry') else ""
        from .movie_widget import load_image_into_picture
        from . import database
        item_id = self.movie_stub.get("id")
        backdrop_cover = (self.movie_details.get("background") or self.movie_details.get("medium_cover_image")) if hasattr(self, 'movie_details') else None

        filtered_eps = []
        for ep in self.current_episodes:
            ep_num = ep.get("episode", 1)
            ep_title = ep.get("title") or ep.get("name") or (f"Episode {ep_num}" if self.media_type in ["series", "anime"] else f"Movie {ep_num}")
            overview = ep.get("overview") or ""
            if query:
                match_q = (query in ep_title.lower() or query in str(ep_num) or query in overview.lower())
                if not match_q: continue
            filtered_eps.append(ep)

        if not filtered_eps:
            no_match_text = "No matching episodes found" if self.media_type in ["series", "anime"] else "No matching movies found"
            empty_lbl = Gtk.Label(label=no_match_text)
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(16)
            self.episodes_list_box.append(empty_lbl)
            return

        self._ep_render_gen = getattr(self, '_ep_render_gen', 0) + 1
        render_gen = self._ep_render_gen

        def build_ep_card(ep):
            ep_num = ep.get("episode", 1)
            ep_title = ep.get("title") or ep.get("name") or (f"Episode {ep_num}" if self.media_type in ["series", "anime"] else f"Movie {ep_num}")
            overview = ep.get("overview") or ""
            released = ep.get("released") or ""

            btn = Gtk.Button()
            btn.add_css_class("ep-card-row")
            if getattr(self, 'selected_episode', None) == ep_num:
                btn.add_css_class("selected")

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            thumb_pic = Gtk.Picture()
            thumb_pic.set_can_shrink(True)
            thumb_pic.set_size_request(130, 75)
            thumb_pic.set_content_fit(Gtk.ContentFit.COVER)
            thumb_pic.add_css_class("ep-card-thumb")

            thumb_url = ep.get("thumbnail") or backdrop_cover
            if thumb_url:
                load_image_into_picture(thumb_url, thumb_pic, width=130, height=75)

            hbox.append(thumb_pic)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.set_hexpand(True)
            vbox.set_valign(Gtk.Align.CENTER)

            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            card_title_text = f"Ep {ep_num}. {ep_title}" if self.media_type in ["series", "anime"] else f"{ep_num}. {ep_title}"
            ep_name_lbl = Gtk.Label(label=card_title_text)
            ep_name_lbl.add_css_class("ep-card-title")
            ep_name_lbl.set_halign(Gtk.Align.START)
            ep_name_lbl.set_wrap(True)
            ep_name_lbl.set_hexpand(True)
            title_row.append(ep_name_lbl)

            vid_id = ep.get("id") or f"{item_id}_S{getattr(self, 'selected_season', 1)}E{ep_num}"
            if database.is_watched(vid_id) or database.is_watched(f"{item_id}_S{getattr(self, 'selected_season', 1)}E{ep_num}"):
                w_badge = Gtk.Label(label="👁 Watched")
                w_badge.add_css_class("ep-watched-badge")
                title_row.append(w_badge)

            vbox.append(title_row)

            if released:
                date_lbl = Gtk.Label(label=str(released).split('T')[0])
                date_lbl.add_css_class("ep-card-date")
                date_lbl.set_halign(Gtk.Align.START)
                vbox.append(date_lbl)

            if overview:
                short_ov = overview[:120] + "..." if len(overview) > 120 else overview
                ov_lbl = Gtk.Label(label=short_ov)
                ov_lbl.add_css_class("ep-card-overview")
                ov_lbl.set_halign(Gtk.Align.START)
                ov_lbl.set_wrap(True)
                vbox.append(ov_lbl)

            hbox.append(vbox)
            btn.set_child(hbox)

            def make_ep_cb(episode_item, episode_num):
                def cb(b):
                    self._user_navigated_to_streams = True
                    self.selected_video = episode_item
                    self.selected_episode = episode_num
                    s_val = getattr(self, 'selected_season', 1)
                    if item_id:
                        database.set_setting(f"last_ep_{item_id}_{s_val}", episode_num)
                    if hasattr(self, 'stream_ep_title_label'):
                        if self.media_type in ["series", "anime"]:
                            self.stream_ep_title_label.set_text(f"▶ S{s_val}E{episode_num}: {ep_title}")
                        else:
                            self.stream_ep_title_label.set_text(f"▶ {ep_title}")
                        self.stream_ep_title_label.set_visible(True)
                    self.sidebar_stack.set_visible_child_name("streams")
                    self.render_episodes_list()
                    self.fetch_torrents_async()
                return cb

            btn.connect("clicked", make_ep_cb(ep, ep_num))
            return btn

        batch_size = 10
        def append_batch(start_idx):
            if render_gen != getattr(self, '_ep_render_gen', 0) or getattr(self, '_destroyed', False):
                return False
            end_idx = min(start_idx + batch_size, len(filtered_eps))
            for i in range(start_idx, end_idx):
                card = build_ep_card(filtered_eps[i])
                self.episodes_list_box.append(card)
            if end_idx < len(filtered_eps):
                GLib.idle_add(append_batch, end_idx)
            return False

        append_batch(0)

    def run_live_stream_check(self):
        t_list = getattr(self, 'current_t_list', []) or []
        http_streams = [t for t in t_list if t.get('is_http')]
        if not http_streams:
            if hasattr(self, 'progress_label') and self.progress_label:
                self.progress_label.set_text("No direct streams available to check.")
            return

        if hasattr(self, 'check_live_btn'):
            self.check_live_btn.set_sensitive(False)
            self.check_live_btn.set_label("⏳ Checking...")

        if hasattr(self, '_live_check_abort_event'):
            self._live_check_abort_event.set()
        self._live_check_abort_event = threading.Event()
        abort_event = self._live_check_abort_event

        def live_check():
            from . import api
            import concurrent.futures

            def check_stream(t):
                if abort_event.is_set(): return
                res = api._ping_stream_url(dict(t))
                t['ping_status'] = res.get('is_working', False)

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(check_stream, t) for t in http_streams]
                concurrent.futures.wait(futures)

            if abort_event.is_set() or not self._is_current_details_page():
                return

            def on_complete():
                if hasattr(self, 'check_live_btn'):
                    self.check_live_btn.set_sensitive(True)
                    self.check_live_btn.set_label("⚡ Check Direct")
                t_list.sort(key=lambda x: (0 if (x.get('is_http') and x.get('ping_status') is True) else (1 if x.get('is_http') and x.get('ping_status') is None else (2 if x.get('is_http') else 3))))
                self.render_streams_list()
                return False

            GLib.idle_add(on_complete)

        threading.Thread(target=live_check, daemon=True).start()

    def update_provider_dropdown_model(self):
        if not hasattr(self, 'provider_dropdown'): return
        all_addons = set()
        source_idx = getattr(self, 'source_idx', 0)
        
        streams = getattr(self, 'torrents', [])
        if source_idx == 1:
            streams = [t for t in streams if t.get('is_http')]
        elif source_idx == 2:
            streams = [t for t in streams if not t.get('is_http')]

        for t in streams:
            for a in t.get("addon_names", []):
                if a: all_addons.add(a)
        provider_strings = ["All Providers"] + sorted(list(all_addons))
        
        curr_model = self.provider_dropdown.get_model()
        curr_strings = []
        if curr_model:
            for i in range(curr_model.get_n_items()):
                curr_strings.append(curr_model.get_string(i))
                
        if curr_strings != provider_strings:
            curr_sel = self.provider_dropdown.get_selected()
            curr_name = curr_strings[curr_sel] if (curr_strings and curr_sel < len(curr_strings)) else "All Providers"
            
            self._ignore_provider_signals = True
            try:
                self.provider_dropdown.set_model(Gtk.StringList.new(provider_strings))
                if curr_name in provider_strings:
                    self.provider_dropdown.set_selected(provider_strings.index(curr_name))
                else:
                    self.provider_dropdown.set_selected(0)
            finally:
                self._ignore_provider_signals = False

    def render_streams_list(self):
        while child := self.streams_list_box.get_first_child():
            self.streams_list_box.remove(child)

        t_list = getattr(self, 'current_t_list', []) or []

        sel_prov_name = "All Providers"
        if hasattr(self, 'provider_dropdown'):
            model = self.provider_dropdown.get_model()
            sel_idx = self.provider_dropdown.get_selected()
            if model and sel_idx != Gtk.INVALID_LIST_POSITION and sel_idx < model.get_n_items():
                sel_prov_name = model.get_string(sel_idx)

        filtered_streams = t_list
        source_idx = getattr(self, 'source_idx', 0)
        if source_idx == 1:
            filtered_streams = [t for t in filtered_streams if t.get('is_http')]
        elif source_idx == 2:
            filtered_streams = [t for t in filtered_streams if not t.get('is_http')]

        if sel_prov_name != "All Providers":
            filtered_streams = [t for t in filtered_streams if sel_prov_name in t.get("addon_names", [])]

        if not filtered_streams:
            no_s_lbl = Gtk.Label(label="No streams match active provider/source filters")
            no_s_lbl.add_css_class("dim-label")
            no_s_lbl.set_margin_top(16)
            self.streams_list_box.append(no_s_lbl)
            return

        if len(filtered_streams) > 40:
            filtered_streams = filtered_streams[:40]

        for idx, t in enumerate(filtered_streams):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.add_css_class("stream-card-row")

            is_selected = getattr(self, 'selected_torrent', None) and _streams_match(t, self.selected_torrent)
            if not getattr(self, 'selected_torrent', None) and idx == 0:
                is_selected = True
                self.selected_torrent = t

            if is_selected:
                row_box.add_css_class("selected")

            badge_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            badge_vbox.set_valign(Gtk.Align.CENTER)

            if t.get("size"):
                sz_lbl = Gtk.Label(label=t.get("size"))
                sz_lbl.add_css_class("size-badge")
                badge_vbox.append(sz_lbl)
            elif t.get("bitrate"):
                br_lbl = Gtk.Label(label=t.get("bitrate"))
                br_lbl.add_css_class("size-badge")
                badge_vbox.append(br_lbl)

            q_str = t.get("quality", "1080p")
            q_lbl = Gtk.Label(label=q_str)
            q_lbl.add_css_class("quality-pill")
            if "4K" in q_str or "2160" in q_str: q_lbl.add_css_class("q-4k")
            elif "1080" in q_str: q_lbl.add_css_class("q-1080p")
            else: q_lbl.add_css_class("q-720p")
            badge_vbox.append(q_lbl)

            addons_list = t.get("addon_names", [])
            if addons_list:
                a_lbl = Gtk.Label(label=addons_list[0])
                a_lbl.add_css_class("addon-badge")
                badge_vbox.append(a_lbl)

            if not t.get("is_http") and t.get("seeders", 0) > 0:
                seed_lbl = Gtk.Label(label=f"👤 {t.get('seeders')}")
                seed_lbl.add_css_class("seeders-badge")
                badge_vbox.append(seed_lbl)

            row_box.append(badge_vbox)

            mid_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            mid_vbox.set_hexpand(True)

            raw_title = t.get("stream_title", "").strip() or t.get("filename", "") or t.get("title", "")
            ping_status = t.get("ping_status")
            status_prefix = "✅ " if ping_status is True else ("⛔ " if ping_status is False else "")

            title_lbl = Gtk.Label(label=f"{status_prefix}{raw_title.splitlines()[0] if raw_title else 'Stream'}")
            title_lbl.add_css_class("ep-card-title")
            title_lbl.set_halign(Gtk.Align.START)
            title_lbl.set_wrap(True)
            title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            mid_vbox.append(title_lbl)

            meta_parts = []
            if t.get("is_external"):
                meta_parts.append("🌐 Web Stream")
            elif t.get("is_http"):
                meta_parts.append("⚡ Direct Stream")
            else:
                meta_parts.append("🧲 Torrent")

            if t.get("bitrate") and t.get("size"):
                meta_parts.append(f"📶 {t.get('bitrate')}")

            if meta_parts:
                meta_lbl = Gtk.Label(label=" • ".join(meta_parts))
                meta_lbl.add_css_class("ep-card-date")
                meta_lbl.set_halign(Gtk.Align.START)
                mid_vbox.append(meta_lbl)

            row_box.append(mid_vbox)

            actions_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            actions_vbox.set_valign(Gtk.Align.CENTER)
            actions_vbox.set_visible(is_selected)

            play_btn = Gtk.Button()
            play_btn.set_icon_name("media-playback-start-symbolic")
            play_btn.add_css_class("suggested-action")
            play_btn.add_css_class("circular")
            play_btn.set_tooltip_text("Play Stream")

            def make_play_cb(st):
                def on_play_clicked(b):
                    self.selected_torrent = st
                    self.on_watch_clicked(None)
                return on_play_clicked

            play_btn.connect("clicked", make_play_cb(t))
            actions_vbox.append(play_btn)

            dl_btn = Gtk.Button()
            dl_btn.set_icon_name("folder-download-symbolic")
            dl_btn.add_css_class("flat")
            dl_btn.add_css_class("circular")
            dl_btn_tooltip = "Download Direct Stream" if t.get("is_http") else "Download Torrent"
            dl_btn.set_tooltip_text(dl_btn_tooltip)

            def make_dl_cb(st):
                def on_dl_clicked(b):
                    self.selected_torrent = st
                    self._download_stream_item(st)
                return on_dl_clicked

            dl_btn.connect("clicked", make_dl_cb(t))
            actions_vbox.append(dl_btn)

            row_box.append(actions_vbox)
            row_box._actions_vbox = actions_vbox

            row_gesture = Gtk.GestureClick()
            row_gesture.set_button(1)
            row_gesture.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)

            def make_row_click_cb(stream_item, curr_row_box):
                def on_pressed(gesture, n_press, x, y):
                    self.selected_torrent = stream_item
                    if not curr_row_box.has_css_class("selected"):
                        child = self.streams_list_box.get_first_child()
                        while child:
                            child.remove_css_class("selected")
                            if hasattr(child, '_actions_vbox'):
                                child._actions_vbox.set_visible(False)
                            child = child.get_next_sibling()

                        curr_row_box.add_css_class("selected")
                        if hasattr(curr_row_box, '_actions_vbox'):
                            curr_row_box._actions_vbox.set_visible(True)

                    if n_press >= 2:
                        self.on_watch_clicked(None)
                return on_pressed

            row_gesture.connect("pressed", make_row_click_cb(t, row_box))
            row_box.add_controller(row_gesture)
            self.streams_list_box.append(row_box)

    def _is_current_details_page(self):
        if self._destroyed:
            return False
        if not self.window:
            return True
        try:
            current = self.window.details_box.get_first_child()
            return current is self
        except Exception:
            return False

    def load_details_async(self, force_refresh=False):
        item_id = self.movie_stub.get("alias_ids") or self.movie_stub.get("id")
        existing_poster = self.movie_stub.get("medium_cover_image") or self.movie_stub.get("poster")
        self._details_fetch_id += 1
        fetch_id = self._details_fetch_id

        # Cancel any in-flight fast trailer fetch from a previous page
        if self._trailer_fast_abort:
            self._trailer_fast_abort.set()
        self._trailer_fast_abort = threading.Event()
        fast_abort = self._trailer_fast_abort

        print(f"[CARD CLICK Step 6] load_details_async started (force_refresh={force_refresh}) for '{item_id}'")

        # Step 1: Fetch Image & Metadata FIRST
        def fetch():
            from . import api
            details = api.fetch_movie_details(item_id, self.media_type, title=self.movie_stub.get("title"), use_cache=not force_refresh, poster=existing_poster)
            if details:
                poster = details.get("medium_cover_image")
                print(f"[CARD CLICK Step 7] api.fetch_movie_details completed. Poster URL: {poster}")
                def apply_details():
                    if fetch_id == self._details_fetch_id and self._is_current_details_page():
                        # Step 1 UI build (Metadata and Images displayed)
                        self.build_ui(details)
                        # Step 2: Now resolve trailer YouTube URL & direct stream
                        tr = details.get("trailer")
                        if tr:
                            self._apply_trailer_url(tr, fetch_id)
                        else:
                            self._start_fast_trailer_fetch(item_id, fetch_id, fast_abort)
                    return False
                GLib.idle_add(apply_details)
            else:
                print(f"[CARD CLICK Step 7 WARNING] api.fetch_movie_details returned None for '{item_id}'")
                def try_fast_trailer():
                    if fetch_id == self._details_fetch_id and self._is_current_details_page():
                        self._start_fast_trailer_fetch(item_id, fetch_id, fast_abort)
                    return False
                GLib.idle_add(try_fast_trailer)

        threading.Thread(target=fetch, daemon=True).start()

    def _start_fast_trailer_fetch(self, item_id, fetch_id, abort_event):
        """Query trailer-capable addons for a trailer ytId after metadata is loaded."""
        if getattr(self, "trailer_url", None):
            return

        def _fast_fetch():
            from . import api
            yt_id = api.fetch_trailer_link_fast(item_id, self.media_type, abort_event=abort_event)
            if yt_id and not abort_event.is_set():
                GLib.idle_add(self._apply_trailer_url, yt_id, fetch_id)

        threading.Thread(target=_fast_fetch, daemon=True).start()

    def _apply_trailer_url(self, yt_id, fetch_id=None):
        """
        Safely apply a discovered trailer URL to the button and start background stream pre-resolution.
        Guards against stale results from old pages via fetch_id check.
        """
        if not yt_id:
            return False
        if fetch_id is not None and fetch_id != getattr(self, "_details_fetch_id", 0):
            return False  # Stale result from a previous page
        if not self._is_current_details_page():
            return False
        if getattr(self, "_destroyed", False):
            return False

        clean_yt = str(yt_id).strip()
        self.trailer_url = clean_yt

        if hasattr(self, "trailer_btn") and self.trailer_btn:
            self._trailer_signal_gen += 1
            cur_gen = self._trailer_signal_gen

            if hasattr(self, "_trailer_btn_hid") and self._trailer_btn_hid:
                try:
                    self.trailer_btn.disconnect(self._trailer_btn_hid)
                except Exception:
                    pass
                self._trailer_btn_hid = None

            self.reset_trailer_btn_ui()
            self.trailer_btn.set_sensitive(True)

            def _on_trailer_btn_clicked(btn):
                if self._trailer_signal_gen != cur_gen:
                    return
                self.on_trailer_clicked()

            self._trailer_btn_hid = self.trailer_btn.connect("clicked", _on_trailer_btn_clicked)

        return False


        from . import database
        item_id = details.get("id")
        if database.is_favorite(item_id):
            database.remove_favorite(item_id)
            self.detail_fav_btn.set_tooltip_text("Add to Favorites")
            self.detail_fav_btn.set_icon_name("non-starred-symbolic")
        else:
            database.add_favorite({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_fav_btn.set_tooltip_text("Remove from Favorites")
            self.detail_fav_btn.set_icon_name("starred-symbolic")

    def toggle_watched(self, details):
        from . import database
        item_id = details.get("id")
        if database.is_watched(item_id):
            database.remove_watched(item_id)
            self.detail_seen_btn.set_tooltip_text("Mark as Seen")
            self.detail_seen_btn.set_icon_name("eye-closed-symbolic")
        else:
            database.add_watched({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_seen_btn.set_tooltip_text("Marked as Seen")
            self.detail_seen_btn.set_icon_name("eye-open-negative-filled-symbolic")

    def update_continue_btn(self, details=None):
        from . import database, api
        ids_to_check = []
        if self.movie_stub.get("alias_ids"):
            for a_id in self.movie_stub["alias_ids"]:
                if a_id and str(a_id) not in ids_to_check:
                    ids_to_check.append(str(a_id))
        for k in ["id", "imdb_id"]:
            val = self.movie_stub.get(k)
            if val and str(val) not in ids_to_check:
                ids_to_check.append(str(val))
        if details:
            for k in ["id", "imdb_id"]:
                val = details.get(k)
                if val and str(val) not in ids_to_check:
                    ids_to_check.append(str(val))

        primary_id = ids_to_check[0] if ids_to_check else None

        cw_item = database.get_continue_watching_item(ids_to_check)
        if not cw_item and (self.movie_stub.get("stream_queue") or self.movie_stub.get("stream_url") or float(self.movie_stub.get("position") or 0) > 0):
            cw_item = self.movie_stub

        working_stream = getattr(self, "remembered_working_stream", None)
        if not working_stream and primary_id:
            working_stream = database.get_working_stream(primary_id, getattr(self, "selected_season", None), getattr(self, "selected_episode", None))
            if working_stream:
                self.remembered_working_stream = working_stream

        # Disconnect existing click handler if any
        if hasattr(self, '_continue_btn_hid') and self._continue_btn_hid:
            try:
                self.continue_btn.disconnect(self._continue_btn_hid)
            except Exception:
                pass
            self._continue_btn_hid = None

        has_progress = False
        if cw_item:
            pos = float(cw_item.get("position") or 0.0)
            if pos > 0 or cw_item.get("stream_url") or cw_item.get("magnet") or cw_item.get("stream_queue"):
                has_progress = True
            elif working_stream:
                has_progress = True
        elif working_stream:
            has_progress = True

        has_stream_provider = api.has_stream_addons(self.media_type, primary_id)
        has_direct_playable = bool(
            working_stream
            or (cw_item and (cw_item.get("stream_url") or cw_item.get("magnet") or cw_item.get("stream_queue")))
            or (self.movie_stub.get("stream_url") or self.movie_stub.get("magnet"))
            or (details and (details.get("stream_url") or details.get("magnet")))
            or getattr(self, "torrents", None)
        )

        if not has_stream_provider and not has_direct_playable:
            self.continue_btn.set_visible(False)
            return

        sel_season = getattr(self, "selected_season", None)
        sel_episode = getattr(self, "selected_episode", None)

        if self.media_type in ["series", "anime", "tv"]:
            cw_s = (cw_item or {}).get("season")
            cw_e = (cw_item or {}).get("episode")
            cw_pos = float((cw_item or {}).get("position") or 0.0)
            cw_dur = float((cw_item or {}).get("duration") or 0.0)

            s_label = sel_season if sel_season is not None else (cw_s if cw_s is not None else 1)
            e_label = sel_episode if sel_episode is not None else (cw_e if cw_e is not None else 1)

            is_matching_cw = (cw_s is not None and cw_e is not None and cw_s == s_label and cw_e == e_label and cw_pos > 0)
            
            lbl_parts = []
            if is_matching_cw:
                lbl_parts.append("Continue")
                lbl_parts.append(f"S{s_label}:E{e_label}")
                if cw_dur > cw_pos and cw_pos > 0:
                    rem_mins = int((cw_dur - cw_pos) / 60)
                    if rem_mins > 0:
                        lbl_parts.append(f"({rem_mins}m left)")
                self.continue_btn.set_tooltip_text("Continue Watching")
            else:
                lbl_parts.append("Play")
                lbl_parts.append(f"S{s_label}:E{e_label}")
                self.continue_btn.set_tooltip_text(f"Play Season {s_label} Episode {e_label}")

            lbl_text = " ".join(lbl_parts)
            self.continue_label.set_text(lbl_text)
            self.continue_btn.set_visible(True)

            def on_continue_clicked(btn):
                self.on_watch_clicked(btn)

            self._continue_btn_hid = self.continue_btn.connect("clicked", on_continue_clicked)
        else:
            pos = float((cw_item or {}).get("position") or 0.0)
            dur = float((cw_item or {}).get("duration") or 0.0)
            if pos > 0:
                lbl_parts = ["Continue"]
                if dur > pos:
                    rem_mins = int((dur - pos) / 60)
                    if rem_mins > 0:
                        lbl_parts.append(f"({rem_mins}m left)")
                self.continue_label.set_text(" ".join(lbl_parts))
                self.continue_btn.set_tooltip_text("Continue Watching")
            else:
                self.continue_label.set_text("Play")
                self.continue_btn.set_tooltip_text("Play Movie")
            self.continue_btn.set_visible(True)

            def on_play_clicked(btn):
                self.on_watch_clicked(btn)

            self._continue_btn_hid = self.continue_btn.connect("clicked", on_play_clicked)

    def build_ui(self, details):
        if not details: return
        self.movie_details = details
        if details.get("type"):
            self.media_type = details["type"]
        resolved_id = details.get("id") or details.get("imdb_id")
        if resolved_id and str(resolved_id).startswith("tt"):
            self.movie_stub["imdb_id"] = resolved_id
            alias_ids = self.movie_stub.get("alias_ids") or []
            if isinstance(alias_ids, list):
                if resolved_id not in alias_ids:
                    alias_ids.insert(0, resolved_id)
                self.movie_stub["alias_ids"] = alias_ids
        item_id = details.get("id") or self.movie_stub.get("id")
        print(f"[CARD CLICK Step 8] build_ui() running for '{item_id}'")
        if details.get("videos"):
            self.videos = details.get("videos")
        from . import database
        from .movie_widget import load_image_into_picture

        if details.get("background"):
            load_image_into_picture(details.get("background"), self.backdrop_pic, is_priority=True)
            
        cover = self.movie_stub.get("medium_cover_image") or self.movie_stub.get("poster")
        if not cover:
            from . import database
            cached = database.get_cached_metadata(self.movie_stub.get("id"))
            if cached and cached.get("medium_cover_image"):
                cover = cached["medium_cover_image"]
        if not cover:
            cover = details.get("medium_cover_image")
            
        print(f"[CARD CLICK Step 8a] Cover resolved to: '{cover}'")
            
        if cover:
            details["medium_cover_image"] = cover
            if cover != getattr(self, "_loaded_poster_url", None):
                self._loaded_poster_url = cover
                print(f"[CARD CLICK Step 8b] Calling load_image_into_picture for poster: {cover}")
                def on_poster_error():
                    print(f"[CARD CLICK Step 8c ERROR] Poster load failed for '{cover}'. Triggering fetch_fallback_poster.")
                    from .movie_widget import fetch_fallback_poster
                    fetch_fallback_poster(details.get("id") or self.movie_stub.get("id"), self.media_type, self.poster, details.get("title") or self.movie_stub.get("title"))
                load_image_into_picture(cover, self.poster, width=360, height=540, on_error=on_poster_error, is_priority=True)
            
        self.title_label.set_text(details.get("title", ""))

        # Disconnect any previously connected signal handlers before reconnecting
        # to avoid duplicate callbacks when build_ui() is called a second time.
        for attr in ('_copy_btn_hid', '_g_btn_hid', '_imdb_btn_hid',
                     '_fav_btn_hid', '_seen_btn_hid', '_trailer_btn_hid',
                     '_continue_btn_hid', '_ep_dropdown_hid', '_season_dropdown_hid'):
            hid = getattr(self, attr, None)
            if hid:
                try:
                    btn_map = {
                        '_copy_btn_hid': self.copy_btn,
                        '_g_btn_hid': self.g_btn,
                        '_imdb_btn_hid': self.imdb_btn,
                        '_fav_btn_hid': self.detail_fav_btn,
                        '_seen_btn_hid': self.detail_seen_btn,
                        '_trailer_btn_hid': self.trailer_btn,
                        '_continue_btn_hid': self.continue_btn,
                        '_ep_dropdown_hid': self.episode_dropdown,
                        '_season_dropdown_hid': self.season_dropdown,
                    }[attr]
                    btn_map.disconnect(hid)
                except Exception:
                    pass
                setattr(self, attr, None)

        def on_copy_clicked(btn):
            try:
                clipboard = Gdk.Display.get_default().get_clipboard()
                clipboard.set(details.get("title", ""))
            except Exception as e:
                print(f"Failed to copy to clipboard: {e}")
        self._copy_btn_hid = self.copy_btn.connect("clicked", on_copy_clicked)
        
        def on_g_clicked(btn):
            import urllib.parse
            title = details.get("title", "")
            year = details.get("year", "")
            query = f'"{title}" {year} watch online stream' if year else f'"{title}" watch online stream'
            open_uri(f"https://www.google.com/search?q={urllib.parse.quote(query)}", self.window)
        self._g_btn_hid = self.g_btn.connect("clicked", on_g_clicked)
        
        meta_parts = []
        if details.get("year"):
            meta_parts.append(str(details.get("year")))
        if details.get("certification"):
            meta_parts.append(f"[{details.get('certification')}]")
        if details.get("runtime"):
            meta_parts.append(str(details.get("runtime")))
        if details.get("genre"):
            meta_parts.append(str(details.get("genre")))
            
        meta_str = " • ".join(meta_parts) if meta_parts else f"{details.get('year', '')} • {details.get('runtime', '')} • {details.get('genre', '')}"
        self.meta_label.set_text(meta_str)
        
        imdb_id = details.get("imdb_id") or details.get("id")
        imdb_rating = details.get("imdbRating", "")
        if imdb_id and imdb_rating:
            self.imdb_btn.set_label(f"IMDb {imdb_rating}")
            self.imdb_btn.set_visible(True)
            self.meta_dot.set_visible(True)
            def on_imdb_clicked(btn):
                open_uri(f"https://www.imdb.com/title/{imdb_id}/", self.window)
            self._imdb_btn_hid = self.imdb_btn.connect("clicked", on_imdb_clicked)
        elif imdb_id:
            self.imdb_btn.set_label("IMDb")
            self.imdb_btn.set_visible(True)
            self.meta_dot.set_visible(True)
            def on_imdb_clicked(btn):
                open_uri(f"https://www.imdb.com/title/{imdb_id}/", self.window)
            self._imdb_btn_hid = self.imdb_btn.connect("clicked", on_imdb_clicked)
        else:
            self.imdb_btn.set_visible(False)
            self.meta_dot.set_visible(False)
            
        self.desc_label.set_text(details.get("description", ""))
        
        cast_str = ", ".join(details.get("cast", []))
        if cast_str:
            self.cast_label.set_text(f"Cast: {cast_str}")
            self.cast_label.set_visible(True)
            
        item_id = details.get("id")
        if database.is_favorite(item_id):
            self.detail_fav_btn.set_icon_name("starred-symbolic")
            self.detail_fav_btn.set_tooltip_text("Remove from Favorites")
        else:
            self.detail_fav_btn.set_icon_name("non-starred-symbolic")
            self.detail_fav_btn.set_tooltip_text("Add to Favorites")
        self._fav_btn_hid = self.detail_fav_btn.connect("clicked", lambda x: self.toggle_favorite(details))

        if database.is_watched(item_id):
            self.detail_seen_btn.set_icon_name("eye-open-negative-filled-symbolic")
            self.detail_seen_btn.set_tooltip_text("Marked as Seen")
        else:
            self.detail_seen_btn.set_icon_name("eye-closed-symbolic")
            self.detail_seen_btn.set_tooltip_text("Mark as Seen")
        self._seen_btn_hid = self.detail_seen_btn.connect("clicked", lambda x: self.toggle_watched(details))

        self.update_continue_btn(details)

        trailer_url = details.get("trailer")
        if trailer_url:
            self._apply_trailer_url(trailer_url, self._details_fetch_id)
        elif not getattr(self, "trailer_url", None):
            self.trailer_url = None
            self.reset_trailer_btn_ui()
            self.trailer_btn.set_sensitive(False)

        if hasattr(self, 'play_next_check'):
            has_multiple_eps = self.media_type in ["series", "anime"] or (bool(details.get("videos")) and len(details.get("videos", [])) > 1)
            self.play_next_check.set_visible(has_multiple_eps)
            if self.media_type not in ["series", "anime"] and has_multiple_eps:
                self.play_next_check.set_label("Play Next Movie")
            else:
                self.play_next_check.set_label("Play Next Ep")
        
        if details.get("videos") and len(details.get("videos", [])) > 0:
            videos = details.get("videos")
            self.videos = videos
            has_multiple = len(videos) > 1 or self.media_type in ["series", "anime"] or str(self.movie_stub.get("id", "")).startswith("ctmdb.") or self.movie_stub.get("type") == "collections"

            self.stream_back_btn.set_visible(has_multiple)
            self.stream_back_btn.set_tooltip_text("Back to Episodes" if self.media_type in ["series", "anime"] else "Back to Movies")
            if has_multiple and not getattr(self, '_user_navigated_to_streams', False):
                self.sidebar_stack.set_visible_child_name("episodes")
            else:
                self.sidebar_stack.set_visible_child_name("streams")

            if hasattr(self, 'ep_search_entry'):
                if self.media_type in ["series", "anime"]:
                    self.ep_search_entry.set_placeholder_text("Search videos / episodes...")
                else:
                    self.ep_search_entry.set_placeholder_text("Search movies in collection...")

            if hasattr(self, 'row2_box'):
                self.row2_box.set_visible(True)

            seasons = sorted(list(set([v.get("season", 1) for v in videos])))
            self.seasons = seasons

            show_seasons = len(self.seasons) > 1 or self.media_type in ["series", "anime"]
            if hasattr(self, 'season_nav_box'):
                self.season_nav_box.set_visible(show_seasons)
            self.season_dropdown.set_model(Gtk.StringList.new([f"Season {s}" for s in self.seasons]))
            self.season_dropdown.set_visible(show_seasons)
            
            self._ignore_dropdown_changes = False
            item_id = self.movie_stub.get("id")
            from . import database
            
            def on_episode_changed(dropdown, *args):
                if getattr(self, '_ignore_dropdown_changes', False): return
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                if hasattr(self, 'current_episodes') and idx < len(self.current_episodes):
                    self.selected_video = self.current_episodes[idx]
                    self.selected_episode = self.selected_video.get("episode")
                    primary_id = (self.movie_stub.get("alias_ids") or [self.movie_stub.get("id") or self.movie_stub.get("imdb_id")])[0]
                    if primary_id and getattr(self, 'selected_season', None) is not None and self.selected_episode is not None:
                        database.set_setting(f"last_ep_{primary_id}_{self.selected_season}", self.selected_episode)
                    self.remembered_working_stream = database.get_working_stream(primary_id, getattr(self, 'selected_season', None), self.selected_episode)
                    if self.remembered_working_stream:
                        self.selected_torrent = self.remembered_working_stream
                    if hasattr(self, 'stream_ep_title_label'):
                        s_val = getattr(self, 'selected_season', 1)
                        ep_title = self.selected_video.get("title") or self.selected_video.get("name") or f"Episode {self.selected_episode}"
                        if self.media_type in ["series", "anime"]:
                            self.stream_ep_title_label.set_text(f"▶ S{s_val}E{self.selected_episode}: {ep_title}")
                        else:
                            self.stream_ep_title_label.set_text(f"▶ {ep_title}")
                        self.stream_ep_title_label.set_visible(True)
                    self.update_continue_btn()
                    self.render_episodes_list()
                    self.fetch_torrents_async()
                
            self._on_episode_dropdown_changed = on_episode_changed
            self._ep_dropdown_hid = self.episode_dropdown.connect("notify::selected", on_episode_changed)
            
            def on_season_changed(dropdown, *args):
                if getattr(self, '_ignore_dropdown_changes', False): return
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                if idx >= len(self.seasons): return
                s = self.seasons[idx]
                self.selected_season = s
                if item_id:
                    database.set_setting(f"last_s_{item_id}", s)
                eps = [v for v in self.videos if v.get("season", 1) == s]
                unique_eps = []
                seen_eps = set()
                for e in eps:
                    ep_num = e.get("episode", 0)
                    if ep_num not in seen_eps:
                        seen_eps.add(ep_num)
                        unique_eps.append(e)
                unique_eps.sort(key=lambda x: x.get("episode", 0))
                self.current_episodes = unique_eps
                
                if self.media_type in ["series", "anime"]:
                    ep_strings = [f"Ep {e.get('episode')}: {e.get('title') or e.get('name', '')}" for e in unique_eps]
                else:
                    ep_strings = [f"{e.get('episode', idx+1)}. {e.get('title') or e.get('name', '')}" for idx, e in enumerate(unique_eps)]
                
                self._ignore_dropdown_changes = True
                try:
                    self.episode_dropdown.set_model(Gtk.StringList.new(ep_strings))
                    ep_nums = [e.get('episode') for e in unique_eps]
                    
                    saved_ep = self.movie_stub.get("episode") or (database.get_setting(f"last_ep_{item_id}_{s}", None) if item_id else None)
                    if saved_ep in ep_nums:
                        default_ep_idx = ep_nums.index(saved_ep)
                    else:
                        default_ep_idx = ep_nums.index(1) if 1 in ep_nums else 0
                        
                    if default_ep_idx < len(ep_strings):
                        self.episode_dropdown.set_selected(default_ep_idx)
                finally:
                    self._ignore_dropdown_changes = False

                on_episode_changed(self.episode_dropdown)
                self.render_episodes_list()
                
            self._season_dropdown_hid = self.season_dropdown.connect("notify::selected", on_season_changed)
            
            if self.seasons:
                saved_s = self.movie_stub.get("season") or (database.get_setting(f"last_s_{item_id}", None) if item_id else None)
                if saved_s in self.seasons:
                    default_s_idx = self.seasons.index(saved_s)
                else:
                    default_s_idx = self.seasons.index(1) if 1 in self.seasons else 0
                self._ignore_dropdown_changes = True
                self.season_dropdown.set_selected(default_s_idx)
                self._ignore_dropdown_changes = False
                on_season_changed(self.season_dropdown)
        else:
            self.sidebar_stack.set_visible_child_name("streams")
            self.stream_back_btn.set_visible(False)
            if hasattr(self, 'season_nav_box'):
                self.season_nav_box.set_visible(False)
            if hasattr(self, 'row2_box'):
                self.row2_box.set_visible(False)
            self.fetch_torrents_async()

    def destroy_page(self):
        self._destroyed = True
        if hasattr(self, 'stream_abort_event') and self.stream_abort_event:
            self.stream_abort_event.set()
        if hasattr(self, '_trailer_fast_abort') and self._trailer_fast_abort:
            self._trailer_fast_abort.set()

    def fetch_torrents_async(self, force=False):
        selected_video = getattr(self, 'selected_video', None)
        video_id = selected_video.get("id") if (selected_video and isinstance(selected_video, dict)) else None
        
        details_id = getattr(self, "movie_details", {}).get("id") if hasattr(self, "movie_details") and self.movie_details else None
        item_id = video_id or (details_id if (details_id and str(details_id).startswith("tt")) else None) or self.movie_stub.get("alias_ids") or self.movie_stub.get("id")
        
        req_media_type = self.media_type
        if video_id and str(video_id).startswith("tt") and self.media_type not in ["series", "anime"]:
            req_media_type = "movie"

        sel_season = getattr(self, 'selected_season', None) if req_media_type in ["series", "anime"] else None
        sel_episode = getattr(self, 'selected_episode', None) if req_media_type in ["series", "anime"] else None
        
        fetch_key = f"{item_id}_{sel_season}_{sel_episode}"
        if not force and getattr(self, '_last_fetch_key', None) == fetch_key:
            return
        self._last_fetch_key = fetch_key

        if hasattr(self, 'progress_label') and self.progress_label:
            self.progress_label.set_text("Loading streams...")
        
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)

        self._fetch_gen = getattr(self, '_fetch_gen', 0) + 1
        current_gen = self._fetch_gen
        target_title = (selected_video.get("title") if selected_video else None) or self.movie_stub.get("title")
        
        if hasattr(self, 'stream_abort_event') and self.stream_abort_event:
            self.stream_abort_event.set()
        self.stream_abort_event = threading.Event()
        current_abort_event = self.stream_abort_event
        
        batch_timer = [None]
        latest_batch = [None]

        def process_batch():
            batch_timer[0] = None
            if not latest_batch[0]:
                return False
            torrents, is_cached, is_complete = latest_batch[0]
            if not self._is_current_details_page() or getattr(self, '_fetch_gen', 0) != current_gen:
                return False

            if hasattr(self, 'progress_label') and self.progress_label:
                if is_complete:
                    if not torrents: self.progress_label.set_text("No streams available.")
                    else: self.progress_label.set_text("")
                elif is_cached:
                    self.progress_label.set_text("Loaded cached streams...")

            self.torrents = torrents or []
            self.update_quality_dropdown()
            self.update_continue_btn()

            if self.torrents and getattr(self, '_auto_play_on_streams_loaded', False):
                self._auto_play_on_streams_loaded = False
                GLib.idle_add(self.on_watch_clicked, self.watch_btn)
            elif is_complete and not self.torrents and getattr(self, '_auto_play_on_streams_loaded', False):
                self._auto_play_on_streams_loaded = False
                if self.window:
                    self.window.hide_player_loading()
                if hasattr(self, 'progress_label') and self.progress_label:
                    self.progress_label.set_text("No streams available.")
            elif self.torrents and getattr(self, '_auto_play_next', False):
                self._auto_play_next = False
                GLib.idle_add(self.on_watch_clicked, self.watch_btn)
            elif is_complete and not self.torrents and getattr(self, '_auto_play_next', False):
                self._auto_play_next = False
                if self.window:
                    self.window.hide_player_loading()
                if hasattr(self, 'progress_label') and self.progress_label:
                    self.progress_label.set_text("No streams available.")
            return False

        def on_stream_batch(torrents, is_cached=False, is_complete=False):
            if not self._is_current_details_page() or getattr(self, '_fetch_gen', 0) != current_gen:
                return
            latest_batch[0] = (torrents, is_cached, is_complete)
            if is_complete or is_cached:
                if batch_timer[0]:
                    GLib.source_remove(batch_timer[0])
                    batch_timer[0] = None
                process_batch()
            elif not batch_timer[0]:
                batch_timer[0] = GLib.timeout_add(300, process_batch)

        def fetch():
            from . import api
            api.get_torrents_streamed(
                item_id,
                req_media_type,
                sel_season,
                sel_episode,
                callback=lambda t, is_cached=False, is_complete=False: GLib.idle_add(on_stream_batch, t, is_cached, is_complete),
                title=target_title,
                abort_event=current_abort_event
            )
        threading.Thread(target=fetch, daemon=True).start()

    def update_quality_dropdown(self):
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)
            
        self.update_provider_dropdown_model()
            
        all_torrents = getattr(self, 'torrents', []) or []

        # Check if trailer button needs activating from discovered stream addons (e.g. Streailer).
        # Guard with _fetch_gen so stale stream batches can't corrupt a newly loaded page.
        if not getattr(self, 'trailer_url', None) and all_torrents:
            current_fetch_gen = getattr(self, '_fetch_gen', 0)
            found_yt = None
            for t in all_torrents:
                t_yt = t.get("ytId")
                t_url = t.get("url") or ""
                if t_yt:
                    found_yt = t_yt
                    break
                elif "youtube.com" in t_url.lower() or "youtu.be" in t_url.lower():
                    found_yt = t_url
                    break
            if found_yt and getattr(self, '_fetch_gen', 0) == current_fetch_gen:
                # Use _apply_trailer_url for race-safe connection management
                self._apply_trailer_url(found_yt, self._details_fetch_id)

        if not all_torrents:
            self.current_t_list = []
            self.selected_torrent = None
            self.quality_row_box.set_visible(False)
            if hasattr(self, 'row3_box'): self.row3_box.set_visible(False)
            if hasattr(self, 'row4_box'): self.row4_box.set_visible(True)
            self.watch_btn.set_visible(False)
            if hasattr(self, 'download_btn'):
                self.download_btn.set_sensitive(False)
            if hasattr(self, 'search_online_btn'):
                self.search_online_btn.set_visible(True)
                self.search_online_btn.add_css_class("suggested-action")
            return

        source_idx = getattr(self, 'source_idx', 0)
        if source_idx == 1:
            filtered_torrents = [t for t in all_torrents if t.get('is_http')]
        elif source_idx == 2:
            filtered_torrents = [t for t in all_torrents if not t.get('is_http')]
        else:
            filtered_torrents = list(all_torrents)

        sel_prov_name = "All Providers"
        if hasattr(self, 'provider_dropdown'):
            model = self.provider_dropdown.get_model()
            sel_idx = self.provider_dropdown.get_selected()
            if model and sel_idx != Gtk.INVALID_LIST_POSITION and sel_idx < model.get_n_items():
                sel_prov_name = model.get_string(sel_idx)

        if sel_prov_name != "All Providers":
            filtered_torrents = [t for t in filtered_torrents if sel_prov_name in t.get("addon_names", [])]

        if not filtered_torrents:
            self.current_t_list = []
            self.selected_torrent = None
            self.quality_row_box.set_visible(True)
            self.quality_button_box.set_visible(False)
            if hasattr(self, 'row3_box'): self.row3_box.set_visible(True)
            self.file_dropdown.set_model(Gtk.StringList.new(["No streams available for selected source filter"]))
            self.file_dropdown.set_selected(0)
            self.watch_btn.set_visible(False)
            if hasattr(self, 'download_btn'):
                self.download_btn.set_sensitive(False)
            self.render_streams_list()
            return

        self.quality_row_box.set_visible(True)
        self.quality_button_box.set_visible(True)
        if hasattr(self, 'row3_box'): self.row3_box.set_visible(True)
        if hasattr(self, 'row4_box'): self.row4_box.set_visible(False)
        self.watch_btn.set_visible(True)
        self.watch_btn.set_sensitive(True)
        if hasattr(self, 'search_online_btn'):
            self.search_online_btn.set_visible(True)
            self.search_online_btn.remove_css_class("suggested-action")
        
        quality_groups = {"4K": [], "1080p": [], "720p": [], "More": []}
        for t in filtered_torrents:
            q = t.get('quality', 'Unknown').upper()
            if "4K" in q or "2160" in q:
                quality_groups["4K"].append(t)
            elif "1080" in q:
                quality_groups["1080p"].append(t)
            elif "720" in q:
                quality_groups["720p"].append(t)
            else:
                quality_groups["More"].append(t)
            
        self.quality_buttons = []
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)

        def _stream_sort_key_1080p(t):
            size = float(t.get('size_gb') or 0.0)
            is_under_4gb = (0 < size < 4.0) or (size == 0.0)
            p_size = 1 if is_under_4gb else 0
            seeds = int(t.get('seeders') or 0)
            is_http = 1 if t.get('is_http') else 0
            ping_ok = 1 if t.get('ping_status') is True else 0
            return (p_size, ping_ok, is_http, seeds, size)

        def _stream_sort_key_general(t):
            seeds = int(t.get('seeders') or 0)
            is_http = 1 if t.get('is_http') else 0
            ping_ok = 1 if t.get('ping_status') is True else 0
            size = float(t.get('size_gb') or 0.0)
            return (ping_ok, is_http, seeds, size)

        for q_label in ["4K", "1080p", "720p", "More"]:
            if quality_groups[q_label]:
                if q_label == "1080p":
                    quality_groups[q_label].sort(key=_stream_sort_key_1080p, reverse=True)
                else:
                    quality_groups[q_label].sort(key=_stream_sort_key_general, reverse=True)

        def update_file_dropdown_ui(t_list):
            if not t_list:
                self.file_dropdown.set_model(Gtk.StringList.new(["No working streams"]))
                self.selected_torrent = None
                if hasattr(self, 'download_btn'):
                    self.download_btn.set_sensitive(False)
                self.render_streams_list()
                return
            strings = []
            for t in t_list:
                addons_str = ", ".join(t.get("addon_names", []))
                addons_suffix = f" [{addons_str}]" if addons_str else ""
                
                raw_title = t.get('stream_title', '').strip() or t.get('size', 'Unknown Size')
                
                ping_status = t.get('ping_status')
                status_prefix = ""
                if ping_status is True:
                    status_prefix = "✅ "
                elif ping_status is False:
                    status_prefix = "⛔ "
                    
                lines = [line.strip() for line in raw_title.splitlines() if line.strip()]
                seed_str = f" ({t.get('seeders', 0)} seeds)" if not t.get('is_http') and t.get('seeders', 0) > 0 else ""
                suffix = f"{seed_str}{addons_suffix}"
                
                if lines:
                    lines[0] = f"{status_prefix}{lines[0]}{suffix}"
                    full_item_str = "\n".join(lines)
                else:
                    full_item_str = f"{status_prefix}{raw_title}{suffix}"
                strings.append(full_item_str)
            selected_idx = 0
            curr_sel = getattr(self, 'selected_torrent', None)
            if curr_sel and t_list:
                selected_idx = _find_stream_index_exact(curr_sel, t_list)
                if selected_idx < 0:
                    selected_idx = 0
                
            self._programmatic_dropdown_switch = True
            try:
                self.file_dropdown.set_model(Gtk.StringList.new(strings))
                self.file_dropdown.set_selected(selected_idx)
                self.selected_torrent = t_list[selected_idx]
            finally:
                self._programmatic_dropdown_switch = False
            if hasattr(self, 'download_btn'):
                self.download_btn.set_sensitive(True)
                if self.selected_torrent.get("is_http"):
                    self.download_btn.set_tooltip_text("Download Direct Stream (Opens in Browser)")
                else:
                    self.download_btn.set_tooltip_text("Download Torrent")
            self.render_streams_list()
            
        def on_quality_btn_clicked(btn, t_list):
            self.current_t_list = t_list
            self._user_interacted_dropdown = False
            update_file_dropdown_ui(t_list)
            
        saved_label = getattr(self, 'user_selected_quality', None)
        target_btn = None
        target_t_list = None
        
        preferred_order = ["1080p", "720p", "4K", "More"]
        btn_by_label = {}
        
        self._programmatic_quality_switch = True
        try:
            first_quality_btn = None
            for q_label in preferred_order:
                t_list = quality_groups[q_label]
                if t_list:
                    btn = Gtk.ToggleButton(label=q_label)
                    btn.set_size_request(-1, 32)
                    if first_quality_btn is None:
                        first_quality_btn = btn
                    else:
                        btn.set_group(first_quality_btn)
                    
                    def make_click_cb(b, label, tl):
                        def cb(*args):
                            if b.get_active():
                                if not getattr(self, '_programmatic_quality_switch', False):
                                    self.user_selected_quality = label
                                    from . import database
                                    database.set_setting("preferred_quality", label)
                                on_quality_btn_clicked(b, tl)
                        return cb
                        
                    btn.connect("toggled", make_click_cb(btn, q_label, t_list))
                    self.quality_buttons.append(btn)
                    self.quality_button_box.append(btn)
                    btn_by_label[q_label] = (btn, t_list)

            # 1. First priority: Match remembered working stream if available
            if getattr(self, "remembered_working_stream", None):
                for q_label in preferred_order:
                    if q_label in btn_by_label:
                        t_list = quality_groups[q_label]
                        idx = _find_stream_index_exact(self.remembered_working_stream, t_list)
                        if idx >= 0:
                            target_btn, target_t_list = btn_by_label[q_label]
                            self.selected_torrent = t_list[idx]
                            break

            # 2. Second priority: User explicitly selected quality in this session
            if not target_btn and saved_label and saved_label in btn_by_label:
                target_btn, target_t_list = btn_by_label[saved_label]

            # 3. Third priority: Preference rule:
            # Prefer 1080p if size < 4 GB, if not found in 1080p choose 720p, then others
            if not target_btn:
                has_1080p_under_4gb = any(
                    (0.0 < float(t.get('size_gb') or 0.0) < 4.0) or float(t.get('size_gb') or 0.0) == 0.0
                    for t in quality_groups.get('1080p', [])
                )
                if has_1080p_under_4gb and "1080p" in btn_by_label:
                    target_btn, target_t_list = btn_by_label["1080p"]
                elif "720p" in btn_by_label:
                    target_btn, target_t_list = btn_by_label["720p"]
                elif "1080p" in btn_by_label:
                    target_btn, target_t_list = btn_by_label["1080p"]
                elif "4K" in btn_by_label:
                    target_btn, target_t_list = btn_by_label["4K"]
                elif "More" in btn_by_label:
                    target_btn, target_t_list = btn_by_label["More"]

            if target_btn and target_t_list:
                if not target_btn.get_active():
                    target_btn.set_active(True)
                else:
                    on_quality_btn_clicked(target_btn, target_t_list)
        finally:
            self._programmatic_quality_switch = False

    def reset_trailer_btn_ui(self):
        if hasattr(self, 'trailer_spinner'):
            self.trailer_spinner.stop()
            self.trailer_spinner.set_visible(False)
        if hasattr(self, 'trailer_icon'):
            self.trailer_icon.set_visible(True)
        if hasattr(self, 'trailer_btn'):
            has_trailer = bool(getattr(self, 'trailer_url', None))
            self.trailer_btn.set_sensitive(has_trailer)

    def on_trailer_clicked(self, *args, **kwargs):
        trailer_url = None
        for a in args:
            if isinstance(a, str) and a.strip():
                trailer_url = a.strip()
                break
        if not trailer_url:
            trailer_url = getattr(self, "trailer_url", None)
        if not trailer_url and hasattr(self, "movie_details") and self.movie_details:
            trailer_url = self.movie_details.get("trailer")
        if not trailer_url and hasattr(self, "movie_stub") and self.movie_stub:
            trailer_url = self.movie_stub.get("trailer") or self.movie_stub.get("ytId")

        if not trailer_url:
            print("[Trailer] No trailer URL found.")
            return

        print(f"[Trailer] Playing trailer: {trailer_url}")

        stub_title = self.movie_stub.get('name') or self.movie_stub.get('title') if hasattr(self, 'movie_stub') else ""
        details_title = self.movie_details.get('name') or self.movie_details.get('title') if (hasattr(self, 'movie_details') and self.movie_details) else ""
        trailer_title = f"{details_title or stub_title or 'Unknown Title'} (Trailer)"

        if hasattr(self, 'trailer_icon'):
            self.trailer_icon.set_visible(False)
        if hasattr(self, 'trailer_spinner'):
            self.trailer_spinner.set_visible(True)
            self.trailer_spinner.start()
        if hasattr(self, 'trailer_btn'):
            self.trailer_btn.set_sensitive(False)

        if self.window:
            self.window.show_player_loading(_("Loading trailer..."), title=trailer_title)

        def progress_callback(stats):
            if not isinstance(stats, dict): return
            url = stats.get("url")
            if url and self.window:
                headers = {}
                if stats.get("user_agent"):
                    headers["User-Agent"] = stats.get("user_agent")
                audio_url = stats.get("audio_url")
                self.window._play_stream(url, trailer_title, headers=headers, audio_url=audio_url)
                self.reset_trailer_btn_ui()
            elif stats.get("closed") or stats.get("error"):
                self.reset_trailer_btn_ui()
                if self.window:
                    self.window.hide_player_loading()
                    if stats.get("status"):
                        self.window._show_toast(stats.get("status"))
            elif stats.get("status") and self.window:
                self.window.update_player_loading(stats.get("status"))

        from . import player
        player.play_trailer(trailer_url, progress_callback=progress_callback)



    def on_stop_clicked(self, btn):
        if self.window and hasattr(self.window, 'mpv'):
            self.window.mpv.stop()
        from . import player
        item_id = self.movie_stub.get("id")
        with player._engines_lock:
            for h, eng in list(player._engines.items()):
                if eng.is_alive() and eng.item_id == item_id:
                    player.stop_engine_explicit(h)
        self.stop_btn.set_visible(False)
        watch_label = "PLAY STREAM" if self.media_type in ["music", "radio", "live"] else "WATCH IT NOW"
        self.watch_btn.set_label(watch_label)

    def on_watch_clicked(self, btn):
        raw_t_list = getattr(self, 'current_t_list', []) or []
        
        sel_prov_name = "All Providers"
        if hasattr(self, 'provider_dropdown'):
            model = self.provider_dropdown.get_model()
            sel_idx = self.provider_dropdown.get_selected()
            if model and sel_idx != Gtk.INVALID_LIST_POSITION and sel_idx < model.get_n_items():
                sel_prov_name = model.get_string(sel_idx)

        if sel_prov_name != "All Providers":
            raw_t_list = [t for t in raw_t_list if sel_prov_name in t.get("addon_names", [])]

        t_list = raw_t_list
        source_idx = getattr(self, 'source_idx', 0)
        if source_idx == 1:
            t_list = [t for t in t_list if t.get('is_http')]
        elif source_idx == 2:
            t_list = [t for t in t_list if not t.get('is_http')]

        selected = getattr(self, 'selected_torrent', None)
        if selected and any(_streams_match(selected, t) for t in t_list):
            self.selected_torrent = next(t for t in t_list if _streams_match(selected, t))
        else:
            working_stream = getattr(self, "remembered_working_stream", None)
            if not working_stream:
                from . import database
                item_id = (
                    getattr(self, "movie_details", {}).get("imdb_id")
                    or getattr(self, "movie_details", {}).get("id")
                    or self.movie_stub.get("imdb_id")
                    or self.movie_stub.get("id")
                )
                season = getattr(self, "selected_season", None)
                episode = getattr(self, "selected_episode", None)
                working_stream = database.get_working_stream(item_id, season, episode)
                if working_stream:
                    self.remembered_working_stream = working_stream

            if working_stream and isinstance(working_stream, dict) and any(_streams_match(working_stream, t) for t in t_list):
                matching_items = [t for t in t_list if _streams_match(working_stream, t)]
                self.selected_torrent = matching_items[0]
            elif t_list:
                self.selected_torrent = t_list[0]
            elif raw_t_list and source_idx == 0:
                self.selected_torrent = raw_t_list[0]
            else:
                if not getattr(self, 'torrents', None):
                    media_title = self.movie_stub.get("name") or self.movie_stub.get("title", "Unknown Title")
                    self._auto_play_on_streams_loaded = True
                    if hasattr(self, 'progress_label') and self.progress_label:
                        self.progress_label.set_text("Loading streams to play...")
                    if self.window:
                        self.window.show_player_loading("Loading streams to play...", media_title)
                    return
                else:
                    if self.window and hasattr(self.window, '_show_toast'):
                        self.window._show_toast("No streams match active filter.")
                    return

        # Save selected stream as working stream
        from . import database
        primary_id = (
            getattr(self, "movie_details", {}).get("imdb_id")
            or getattr(self, "movie_details", {}).get("id")
            or self.movie_stub.get("imdb_id")
            or self.movie_stub.get("id")
        )
        database.save_working_stream(primary_id, getattr(self, "selected_season", None), getattr(self, "selected_episode", None), self.selected_torrent)
        self.remembered_working_stream = self.selected_torrent

        media_title = self.movie_stub.get("name") or self.movie_stub.get("title", "Unknown Title")
        if self.media_type == "series" and getattr(self, "selected_season", None) is not None:
            try:
                s_int = int(self.selected_season)
                e_int = int(self.selected_episode)
                media_title = f"{media_title} (S{s_int:02d}E{e_int:02d})"
            except (ValueError, TypeError):
                media_title = f"{media_title} (S{self.selected_season}E{self.selected_episode})"
            
        is_direct = bool(self.selected_torrent.get("is_http"))
        if self.selected_torrent.get("is_external") or (self.selected_torrent.get("url") and any(d in str(self.selected_torrent.get("url")).lower() for d in ["vidfast.pro", "vidfast.vc", "vidsrc.", "embed"])):
            ext_url = self.selected_torrent.get("externalUrl") or self.selected_torrent.get("url")
            if ext_url:
                if self.window:
                    self.window._show_toast(_("Opening in web browser..."))
                open_uri(ext_url, self.window if self.window else None)
                return

        all_matching = getattr(self, 'torrents', []) or []
        if sel_prov_name != "All Providers":
            all_matching = [t for t in all_matching if sel_prov_name in t.get("addon_names", [])]
        if source_idx == 1:
            all_matching = [t for t in all_matching if t.get("is_http")]
        elif source_idx == 2:
            all_matching = [t for t in all_matching if not t.get("is_http")]
        elif is_direct:
            all_matching = [t for t in all_matching if t.get("is_http")]
        else:
            all_matching = [t for t in all_matching if not t.get("is_http")]

        queue = [self.selected_torrent]
        for t in t_list:
            if not _streams_match(t, self.selected_torrent):
                queue.append(t)
        for t in all_matching:
            if not any(_streams_match(t, q) for q in queue):
                queue.append(t)
        init_idx = 0

        # Subtitle fetching is handled by play_stream_with_failover — do NOT call it here too
        if self.window and hasattr(self.window, 'play_stream_with_failover'):
            imdb_id = (
                getattr(self, "movie_details", {}).get("imdb_id")
                or getattr(self, "movie_details", {}).get("id")
                or self.movie_stub.get("imdb_id")
                or self.movie_stub.get("id")
            )
            stream_subs = self.selected_torrent.get("subtitles") if hasattr(self, "selected_torrent") and isinstance(self.selected_torrent, dict) else None
            self.window.play_stream_with_failover(
                queue,
                initial_index=init_idx,
                title=media_title,
                previous_page="details",
                season=getattr(self, "selected_season", None),
                episode=getattr(self, "selected_episode", None),
                imdb_id=imdb_id,
                media_type=self.media_type,
                stream_subtitles=stream_subs
            )
        else:
            torrent = self.selected_torrent
            magnet = torrent.get("url") or torrent.get("magnet")
            if not magnet and torrent.get("hash"):
                from . import api
                t_name = torrent.get("stream_title") or torrent.get("filename") or torrent.get("name") or torrent.get("title") or self.movie_stub.get("title", "")
                magnet = api.build_magnet(torrent.get("hash"), t_name)
            file_index = torrent.get("file_index")
            self._start_streaming(magnet, file_index)

    def _download_stream_item(self, torrent):
        if not torrent: return
        url = torrent.get("url") or torrent.get("magnet")
        if not url and torrent.get("hash"):
            from . import api
            t_name = torrent.get("stream_title") or torrent.get("filename") or torrent.get("name") or torrent.get("title") or self.movie_stub.get("title", "")
            url = api.build_magnet(torrent.get("hash"), t_name)
            
        if url:
            try:
                open_uri(url, self.window)
            except Exception as e:
                print("Open URL error:", e)
            if hasattr(self, 'progress_label') and self.progress_label:
                if torrent.get("is_http") or (isinstance(url, str) and url.startswith(("http://", "https://"))):
                    self.progress_label.set_text("Opening direct stream in browser...")
                else:
                    self.progress_label.set_text("Opening stream URL...")

    def _copy_stream_url(self, torrent):
        if not torrent: return
        url = torrent.get("url") or torrent.get("magnet")
        if not url and torrent.get("hash"):
            from . import api
            t_name = torrent.get("stream_title") or torrent.get("filename") or torrent.get("name") or torrent.get("title") or self.movie_stub.get("title", "")
            url = api.build_magnet(torrent.get("hash"), t_name)
            
        if url:
            try:
                display = Gdk.Display.get_default()
                clipboard = display.get_clipboard()
                clipboard.set(url)
                
                toast = Adw.Toast.new("Stream URL copied to clipboard")
                toast.set_timeout(2)
                if hasattr(self.window, 'toast_overlay'):
                    self.window.toast_overlay.add_toast(toast)
                elif hasattr(self, 'toast_overlay'):
                    self.toast_overlay.add_toast(toast)
            except Exception as e:
                print("Copy stream URL error:", e)

    def on_download_clicked(self, btn):
        t_list = getattr(self, 'current_t_list', []) or []
        if not getattr(self, 'selected_torrent', None):
            if t_list:
                self.selected_torrent = t_list[0]
            else:
                if hasattr(self, 'progress_label') and self.progress_label:
                    self.progress_label.set_text("Please select a stream first.")
                return

        self._download_stream_item(self.selected_torrent)

    def _start_streaming(self, magnet, file_index):
        if not magnet: return
        
        media_title = self.movie_stub.get("name") or self.movie_stub.get("title", "Unknown Title")
        if self.media_type == "series" and getattr(self, "selected_season", None) is not None:
            try:
                s_int = int(self.selected_season)
                e_int = int(self.selected_episode)
                media_title = f"{media_title} (S{s_int:02d}E{e_int:02d})"
            except (ValueError, TypeError):
                media_title = f"{media_title} (S{self.selected_season}E{self.selected_episode})"
            
        from . import database
        primary_id = (
            getattr(self, "movie_details", {}).get("imdb_id")
            or getattr(self, "movie_details", {}).get("id")
            or self.movie_stub.get("imdb_id")
            or self.movie_stub.get("id")
        )
        season = getattr(self, "selected_season", None)
        episode = getattr(self, "selected_episode", None)
        st_obj = dict(self.selected_torrent) if getattr(self, "selected_torrent", None) else {
            "magnet": magnet,
            "url": magnet,
            "file_index": file_index,
            "title": media_title,
            "stream_title": media_title,
        }
        # Check if the stream is a trailer (from Streailer, YouTube, or labeled as trailer)
        is_trailer = False
        if st_obj:
            if st_obj.get("ytId") or st_obj.get("behaviorHints", {}).get("bingeGroup") == "trailer":
                is_trailer = True
            if any("trailer" in str(a).lower() or "streailer" in str(a).lower() for a in st_obj.get("addon_names", [])):
                is_trailer = True
            if any(k in str(st_obj.get("title", "")).lower() or k in str(st_obj.get("name", "")).lower() or k in str(st_obj.get("stream_title", "")).lower() for k in ["🎬 trailer", "trailer"]):
                is_trailer = True
            if "youtube.com" in str(magnet).lower() or "youtu.be" in str(magnet).lower():
                is_trailer = True

        if not is_trailer:
            database.save_working_stream(primary_id, season, episode, st_obj)
            self.remembered_working_stream = st_obj

            if self.window:
                import time
                details = getattr(self, "movie_details", {}) or {}
                cover = details.get("medium_cover_image") or self.movie_stub.get("medium_cover_image") or details.get("poster") or self.movie_stub.get("poster")
                self.window._current_playing_item = {
                    "id": primary_id,
                    "imdb_id": primary_id,
                    "title": media_title,
                    "type": self.media_type,
                    "medium_cover_image": cover,
                    "season": season,
                    "episode": episode,
                    "magnet": magnet,
                    "file_index": file_index,
                    "stream_title": media_title,
                    "last_watched": int(time.time()),
                    "progress": 0.01,
                    "position": 0.0,
                    "selected_torrent": st_obj,
                    "is_trailer": False,
                }
                database.save_continue_watching(self.window._current_playing_item)
                self.update_continue_btn()
        elif self.window:
            self.window._current_playing_item = {
                "is_trailer": True,
                "title": media_title,
            }

        if magnet and any(d in magnet.lower() for d in ["vidfast.pro", "vidfast.vc", "vidsrc.", "embed"]):
            if self.window:
                self.window.hide_player_loading()
                self.window._show_toast(_("Opening in web browser..."))
            open_uri(magnet, self.window if self.window else None)
            return

        if magnet.startswith("http://") or magnet.startswith("https://"):
            if self.window:
                self.window.show_player_loading("Opening direct stream...", media_title)
                imdb_id = (
                    getattr(self, "movie_details", {}).get("imdb_id")
                    or getattr(self, "movie_details", {}).get("id")
                    or self.movie_stub.get("imdb_id")
                    or self.movie_stub.get("id")
                )
                season = getattr(self, "selected_season", None)
                episode = getattr(self, "selected_episode", None)
                stream_subs = self.selected_torrent.get("subtitles") if hasattr(self, "selected_torrent") and isinstance(self.selected_torrent, dict) else None
                if hasattr(self.window, "fetch_and_add_subtitles"):
                    self.window.fetch_and_add_subtitles(imdb_id, self.media_type, season, episode, stream_subtitles=stream_subs, stream_title=media_title)
                self.window._play_stream(magnet, media_title)
        else:
            from . import player
            if self.window:
                self.window.show_player_loading("Fetching metadata...", media_title)
                
            def progress_callback(stats):
                url = stats.get("url") if isinstance(stats, dict) else None
                if url and self.window:
                    display_title = media_title
                    if isinstance(stats, dict) and stats.get("filePath"):
                        import os
                        display_title = os.path.basename(stats.get("filePath"))
                    if getattr(self.window, "_current_playing_item", None):
                        self.window._current_playing_item["stream_url"] = url
                    self.window._play_stream(url, display_title)
                    GLib.idle_add(self.stop_btn.set_visible, True)
                    continue_label = "▶ Resume Stream" if self.media_type in ["music", "radio", "live"] else "▶ Continue Watching"
                    GLib.idle_add(self.watch_btn.set_label, continue_label)
                    GLib.idle_add(self.update_continue_btn)
                elif isinstance(stats, dict):
                    status_msg = stats.get("status", "Buffering...")
                    if hasattr(self, 'progress_label') and self.progress_label:
                        self.progress_label.set_text(status_msg)
                        
                    if self.window:
                        full_text = self.window.format_stream_stats(stats)
                        self.window.update_player_loading(full_text)
                        
            player.play_magnet(
                magnet,
                progress_callback=progress_callback,
                file_index=file_index,
                item_id=self.movie_stub.get("id"),
                media_type=self.media_type,
                season=getattr(self, "selected_season", None),
                episode=getattr(self, "selected_episode", None)
            )


@Gtk.Template(resource_path="/io/github/fastrizwaan/PopcornBox/window.ui")
class CineWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CineWindow"

    window_handle: Gtk.WindowHandle = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    main_stack: Adw.ViewStack = Gtk.Template.Child()
    details_box: Gtk.Box = Gtk.Template.Child()
    library_stack: Adw.ViewStack = Gtk.Template.Child()
    header_stack: Gtk.Stack = Gtk.Template.Child()
    category_btn_stack: Gtk.Stack = Gtk.Template.Child()
    discover_active_btn: Gtk.MenuButton = Gtk.Template.Child()
    movies_active_btn: Gtk.MenuButton = Gtk.Template.Child()
    series_active_btn: Gtk.MenuButton = Gtk.Template.Child()
    anime_active_btn: Gtk.MenuButton = Gtk.Template.Child()
    discover_inactive_btn_movies: Gtk.Button = Gtk.Template.Child()
    discover_inactive_btn_series: Gtk.Button = Gtk.Template.Child()
    discover_inactive_btn_anime: Gtk.Button = Gtk.Template.Child()
    movies_inactive_btn_discover: Gtk.Button = Gtk.Template.Child()
    series_inactive_btn_discover: Gtk.Button = Gtk.Template.Child()
    anime_inactive_btn_discover: Gtk.Button = Gtk.Template.Child()
    movies_inactive_btn: Gtk.Button = Gtk.Template.Child()
    series_inactive_btn: Gtk.Button = Gtk.Template.Child()
    movies_inactive_btn_anime: Gtk.Button = Gtk.Template.Child()
    series_inactive_btn_anime: Gtk.Button = Gtk.Template.Child()
    anime_inactive_btn_movies: Gtk.Button = Gtk.Template.Child()
    anime_inactive_btn_series: Gtk.Button = Gtk.Template.Child()
    discover_filter_toggle_btn: Gtk.ToggleButton = Gtk.Template.Child()
    library_window_title: Adw.WindowTitle = Gtk.Template.Child()
    discover_options_revealer: Gtk.Revealer = Gtk.Template.Child()
    discover_scrolled: Gtk.ScrolledWindow = Gtk.Template.Child()
    discover_box: Gtk.Box = Gtk.Template.Child()
    discover_back_box: Gtk.Box = Gtk.Template.Child()
    back_to_discover_btn: Gtk.Button = Gtk.Template.Child()
    discover_grid_title: Gtk.Label = Gtk.Template.Child()
    library_header: Adw.HeaderBar = Gtk.Template.Child()
    search_header: Adw.HeaderBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    catalog_dropdown: Gtk.DropDown = Gtk.Template.Child()
    media_type_dropdown: Gtk.DropDown = Gtk.Template.Child()
    genre_dropdown: Gtk.DropDown = Gtk.Template.Child()
    search_catalog_dropdown: Gtk.DropDown = Gtk.Template.Child()
    content_scrolled: Gtk.ScrolledWindow = Gtk.Template.Child()
    content_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    search_movies_section: Gtk.Box = Gtk.Template.Child()
    search_series_section: Gtk.Box = Gtk.Template.Child()
    search_anime_section: Gtk.Box = Gtk.Template.Child()
    search_tv_section: Gtk.Box = Gtk.Template.Child()
    search_movies_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    search_series_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    search_anime_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    search_tv_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    favorites_box: Gtk.Box = Gtk.Template.Child()
    history_box: Gtk.Box = Gtk.Template.Child()
    watched_box: Gtk.Box = Gtk.Template.Child()
    continue_watching_box: Gtk.Box = Gtk.Template.Child()
    downloads_listbox: Gtk.ListBox = Gtk.Template.Child()
    
    fav_all_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fav_movies_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fav_series_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fav_anime_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fav_tv_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_all_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_movies_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_series_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_anime_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_tv_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_all_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_movies_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_series_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_anime_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_tv_btn: Gtk.ToggleButton = Gtk.Template.Child()
    cw_all_btn: Gtk.ToggleButton = Gtk.Template.Child()
    cw_movies_btn: Gtk.ToggleButton = Gtk.Template.Child()
    cw_series_btn: Gtk.ToggleButton = Gtk.Template.Child()
    cw_anime_btn: Gtk.ToggleButton = Gtk.Template.Child()
    cw_tv_btn: Gtk.ToggleButton = Gtk.Template.Child()
    
    fav_search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    hist_search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    watch_search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    cw_search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    
    cw_active_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fav_active_btn: Gtk.ToggleButton = Gtk.Template.Child()
    hist_active_btn: Gtk.ToggleButton = Gtk.Template.Child()
    watch_active_btn: Gtk.ToggleButton = Gtk.Template.Child()
    down_active_btn: Gtk.ToggleButton = Gtk.Template.Child()
    addon_active_btn: Gtk.ToggleButton = Gtk.Template.Child()

    addon_url_entry: Gtk.Entry = Gtk.Template.Child()
    addons_listbox: Gtk.ListBox = Gtk.Template.Child()
    video_overlay: Gtk.Overlay = Gtk.Template.Child()
    start_page: Adw.StatusPage = Gtk.Template.Child()
    revealer_icon_indicator: Gtk.Revealer = Gtk.Template.Child()
    icon_indicator: Gtk.Image = Gtk.Template.Child()
    title_widget: Adw.WindowTitle = Gtk.Template.Child()
    headerbar: Adw.HeaderBar = Gtk.Template.Child()
    controls_box: Gtk.Box = Gtk.Template.Child()
    controls_wrap_box: Adw.WrapBox = Gtk.Template.Child()
    controls_separator: Gtk.Separator = Gtk.Template.Child()
    audio_only_icon: Gtk.Image = Gtk.Template.Child()
    revealer_ui: Gtk.Revealer = Gtk.Template.Child()
    revealer_drop_indicator: Gtk.Revealer = Gtk.Template.Child()
    drop_label: Gtk.Label = Gtk.Template.Child()
    drop_icon: Gtk.Image = Gtk.Template.Child()
    player_loading_box: Gtk.Box = Gtk.Template.Child()
    player_buffering_label: Gtk.Label = Gtk.Template.Child()
    spinner: Adw.Spinner = Gtk.Template.Child()
    next_episode_revealer: Gtk.Revealer = Gtk.Template.Child()
    next_ep_label: Gtk.Label = Gtk.Template.Child()
    next_ep_play_btn: Gtk.Button = Gtk.Template.Child()
    next_ep_dismiss_btn: Gtk.Button = Gtk.Template.Child()
    context_popover_menu: Gtk.PopoverMenu = Gtk.Template.Child()
    primary_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    previous_btn: Gtk.Button = Gtk.Template.Child()
    play_pause_btn: Gtk.Button = Gtk.Template.Child()
    next_btn: Gtk.Button = Gtk.Template.Child()
    volume_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    mute_toggle_btn: Gtk.ToggleButton = Gtk.Template.Child()
    volume_box: Gtk.Box = Gtk.Template.Child()
    volume_scale: Gtk.Scale = Gtk.Template.Child()
    volume_scale_adj: Gtk.Adjustment = Gtk.Template.Child()
    subtitles_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    subtitles_menu: Gio.Menu = Gtk.Template.Child()
    audio_tracks_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    audio_tracks_menu: Gio.Menu = Gtk.Template.Child()
    video_tracks_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    video_tracks_menu: Gio.Menu = Gtk.Template.Child()
    chapters_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    chapters_menu: Gio.Menu = Gtk.Template.Child()
    options_menu_btn: OptionsMenuButton = Gtk.Template.Child()
    shuffle_toggle_btn: Gtk.ToggleButton = Gtk.Template.Child()
    loop_playlist_btn: Gtk.ToggleButton = Gtk.Template.Child()
    loop_file_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fullscreen_btn: Gtk.Button = Gtk.Template.Child()
    time_elapsed_label: Gtk.Label = Gtk.Template.Child()
    progress_box: Gtk.Box = Gtk.Template.Child()
    vid_progress_scale_box: Gtk.Box = Gtk.Template.Child()
    video_progress_scale: Gtk.Scale = Gtk.Template.Child()
    video_progress_adj: Gtk.Adjustment = Gtk.Template.Child()
    time_total_label: Gtk.Label = Gtk.Template.Child()

    def __init__(self, is_activate=False, **kwargs):
        debug_log("CineWindow.__init__ BEGIN")
        super().__init__(**kwargs)
        debug_log("CineWindow.__init__ super().__init__ DONE (GTK template bound)")
        self.app: Adw.Application = cast(Adw.Application, kwargs.get("application"))
        self.app_mpris: MPRIS = self.app.mpris  # type: ignore

        Gtk.WindowGroup().add_window(self)

        for name in ['content_flowbox', 'search_movies_flowbox', 'search_series_flowbox', 'search_anime_flowbox', 'search_tv_flowbox']:
            if hasattr(self, name):
                fb = getattr(self, name)
                fb.set_valign(Gtk.Align.START)
                fb.set_row_spacing(12)

        self.gl_area: Gtk.GLArea = Gtk.GLArea()
        self.offload: Gtk.GraphicsOffload = Gtk.GraphicsOffload(child=self.gl_area)
        self.offload.set_black_background(True)

        vendor: str | None = get_gpu_vendor(libgl)
        if vendor and "nvidia" in vendor:
            self.offload.set_enabled(Gtk.GraphicsOffloadEnabled.DISABLED)

        self.video_overlay.set_child(self.offload)

        self.visible_dialog: Adw.Dialog | None = None
        self.nav_stack: list = []
        self.playlist_ls: Gio.ListStore = Gio.ListStore.new(PlaylistItemObj)
        self.playlist_debounce_id: int = 0
        self.playlist_prev_pos: int
        self.prev_shuffle: bool = False
        self.playlist_changed: bool = False
        self.has_some_doc_path: bool = False
        self.can_go_prev: bool = False
        
        self.local_media_type_filter = "all"
        self._syncing_local_btns = False

        def on_local_btn_toggled(btn, m_type):
            if self._syncing_local_btns or not btn.get_active():
                return
                
            self._syncing_local_btns = True
            self.local_media_type_filter = m_type
            
            if m_type == "all":
                self.fav_all_btn.set_active(True)
                self.hist_all_btn.set_active(True)
                self.watch_all_btn.set_active(True)
                self.cw_all_btn.set_active(True)
            elif m_type == "movie":
                self.fav_movies_btn.set_active(True)
                self.hist_movies_btn.set_active(True)
                self.watch_movies_btn.set_active(True)
                self.cw_movies_btn.set_active(True)
            elif m_type == "series":
                self.fav_series_btn.set_active(True)
                self.hist_series_btn.set_active(True)
                self.watch_series_btn.set_active(True)
                self.cw_series_btn.set_active(True)
            elif m_type == "anime":
                self.fav_anime_btn.set_active(True)
                self.hist_anime_btn.set_active(True)
                self.watch_anime_btn.set_active(True)
                self.cw_anime_btn.set_active(True)
            elif m_type == "tv":
                self.fav_tv_btn.set_active(True)
                self.hist_tv_btn.set_active(True)
                self.watch_tv_btn.set_active(True)
                self.cw_tv_btn.set_active(True)
                
            self._syncing_local_btns = False
            
            page = self.main_stack.get_visible_child_name()
            if page in ["favorites", "history", "watched", "continue_watching"]:
                self._populate_local_db_page(page)

        self.fav_all_btn.connect("toggled", on_local_btn_toggled, "all")
        self.fav_movies_btn.connect("toggled", on_local_btn_toggled, "movie")
        self.fav_series_btn.connect("toggled", on_local_btn_toggled, "series")
        self.fav_anime_btn.connect("toggled", on_local_btn_toggled, "anime")
        self.fav_tv_btn.connect("toggled", on_local_btn_toggled, "tv")
        self.hist_all_btn.connect("toggled", on_local_btn_toggled, "all")
        self.hist_movies_btn.connect("toggled", on_local_btn_toggled, "movie")
        self.hist_series_btn.connect("toggled", on_local_btn_toggled, "series")
        self.hist_anime_btn.connect("toggled", on_local_btn_toggled, "anime")
        self.hist_tv_btn.connect("toggled", on_local_btn_toggled, "tv")
        self.watch_all_btn.connect("toggled", on_local_btn_toggled, "all")
        self.watch_movies_btn.connect("toggled", on_local_btn_toggled, "movie")
        self.watch_series_btn.connect("toggled", on_local_btn_toggled, "series")
        self.watch_anime_btn.connect("toggled", on_local_btn_toggled, "anime")
        self.watch_tv_btn.connect("toggled", on_local_btn_toggled, "tv")
        self.cw_all_btn.connect("toggled", on_local_btn_toggled, "all")
        self.cw_movies_btn.connect("toggled", on_local_btn_toggled, "movie")
        self.cw_series_btn.connect("toggled", on_local_btn_toggled, "series")
        self.cw_anime_btn.connect("toggled", on_local_btn_toggled, "anime")
        self.cw_tv_btn.connect("toggled", on_local_btn_toggled, "tv")
        
        def on_local_search_changed(entry):
            page = self.main_stack.get_visible_child_name()
            if page in ["favorites", "history", "watched", "continue_watching"]:
                self._populate_local_db_page(page)
                
        for se in [self.fav_search_entry, self.hist_search_entry, self.watch_search_entry, self.cw_search_entry]:
            if se:
                se.connect("search-changed", on_local_search_changed)
                se.connect("changed", on_local_search_changed)
        
        def on_active_btn_toggled(btn):
            if not btn.get_active():
                btn.set_active(True)
                self._back_to_library()
                
        self.cw_active_btn.connect("toggled", on_active_btn_toggled)
        self.fav_active_btn.connect("toggled", on_active_btn_toggled)
        self.hist_active_btn.connect("toggled", on_active_btn_toggled)
        self.watch_active_btn.connect("toggled", on_active_btn_toggled)
        self.down_active_btn.connect("toggled", on_active_btn_toggled)
        self.addon_active_btn.connect("toggled", on_active_btn_toggled)

        self.can_go_next: bool = False
        self.chapters: list = []
        self.curr_chapter_time = None
        self.actions: dict[str, Gio.SimpleAction] = {}
        self.prev_motion_xy: tuple = (0, 0)
        self.hover_time: float = 0.0
        self.show_remaining: bool = settings.get_boolean("show-remaining")
        self.prev_prog_time: float = -1.0
        self.prev_prog_motion_xy: tuple = (0, 0)
        self.inhibit_cookie: int = 0
        self.loaded_path: str = ""
        self.startup: bool = True
        self.space_hold_id: int = 0
        self.space_holding: bool = False
        self.space_pressed: bool = False
        self.click_delay_id: int = 0
        ck_time: int = gtk_setts.props.gtk_double_click_time if gtk_setts else 400
        self.click_time: int = max(200, min(ck_time, 425))
        self.click_holding: bool = False
        self.prev_speed: float = 1.0
        self.wheel_accum_x: float = 0.0
        self.wheel_accum_y: float = 0.0
        self.hide_icon_indicator: bool = True
        self.preview_player: mpv.MPV | None = None
        self.late_preview_id: int = 0
        self.is_local_path: bool = True
        self.last_preview_update: float = 0
        self.last_preview_seek: int = 0
        self.error_count: int = 0
        self.pressed_combos: set[str] = set()
        self.key_state: Gdk.ModifierType = Gdk.ModifierType.NO_MODIFIER_MASK
        self.hide_timeout_id: int = 0
        self.is_fs: bool = False
        self.is_inactive: bool = False
        self.next_ep_dismissed: bool = False
        self.next_ep_auto_triggered: bool = False
        self.stream_queue = []
        self.stream_queue_index = 0
        self.stream_request_id = 0
        self.mpv_ctx: mpv.MpvRenderContext

        self.mpv = mpv.MPV(
            # terminal=True,
            # log_handler=print,
            loglevel="info",
            audio_client_name=_("Cine"),
            screenshot_directory=SCREENSHOT_DIR,
            screenshot_template="cine_%n",
            config=True,
            config_dir=CONFIG_DIR,
            input_default_bindings=False,
            input_vo_keyboard=True,
            load_scripts=True,
            audio_display="embedded-first",
            audio_file_auto="fuzzy",
            sub_auto="fuzzy",
            sub_file_paths="sub:subs:subtitles:Sub:Subs:Subtitles:srt:srts:Srt:Srts",
            sub_border_size=2,
            sub_shadow_offset=0.6,
            sub_border_color="#B6000000",
            sub_shadow_color="#97000000",
            sub_color="#ebebeb",
            sub_use_margins=False,
            sub_font="Adwaita Sans SemiBold",
            osd_font="Adwaita Sans",
            osd_bold=True,
            osd_bar=False,
            osd_blur=1,
            osd_border_size=1.5,
            osd_border_color="#BE000000",
            osd_shadow_color="#1B000000",
            osd_margin_x=66,
            osd_margin_y=66,
            volume_max=150,
            keep_open=True,
            ytdl=True,
            ytdl_format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            ytdl_raw_options="no-playlist=",
            cursor_autohide_fs_only=True,
            directory_filter_types="video,audio",
            autocreate_playlist="filter",
            save_watch_history=True,
            watch_history_path=WATCH_HISTORY_JSONL,
        )

        if self.mpv["window-maximized"] or settings.get_boolean("is-maximized"):
            self.maximize()

        self.conf_hwdec = list(
            filter(lambda x: x != "no", cast(list, self.mpv["hwdec"]))
        )
        self.mpv["vo"] = "libmpv"
        self.mpv["osc"] = "no"
        self.mpv["load-console"] = "no"
        self.mpv.command("change-list", "watch-later-options", "remove", "vid")
        self.mpv.command("change-list", "watch-later-options", "remove", "aid")
        self.mpv.command("change-list", "watch-later-options", "remove", "volume")
        self.mpv.command("change-list", "watch-later-options", "remove", "sub-scale")

        self._setup_actions()
        self._setup_widgets()
        self._setup_observers()

        try:
            self.mpv.command("load-input-conf", f"memory://{INTERNAL_BINDINGS}")
            self.mpv.command("load-input-conf", INPUT_CONF)
        except Exception as e:
            logger.error(f"load-input-conf error: {e}", exc_info=True)

        self.bindings = cast(dict, self.mpv._get_property("input-bindings"))
        self.mouse_bindings: dict = get_mouse_bindings(self.bindings)
        self.nonrepeat_keys = parse_nonrepeat_bindings(self.bindings)

        sync_mpv_with_settings(self)

        if settings.get_boolean("save-session") and is_activate:
            restore_last_playlist(self, self.app, self.mpv)

    def _setup_actions(self):
        self._create_action("clear-and-add", self._on_clear_and_add)
        self._create_action_stateful("select-subtitle", self._on_subtitle_selected, "i")
        self._create_action_stateful("select-audio", self._on_audio_selected, "i")
        self._create_action_stateful("select-video", self._on_video_selected, "i")
        self._create_action_stateful("select-chapter", self._on_chapter_selected, "i")
        self._create_action("add-sub-tracks", self._on_add_sub_dialog)
        self._create_action("add-audio-tracks", self._on_add_audio_dialog)
        self._create_action("add-playlist-files", self._on_add_playlist_dialog)
        self._create_action("open-folder", self._on_open_folder_dialog)
        self._create_action("open-url", self._on_open_url)
        self._create_action("close-player", self._close_player)
        self._create_action("add-addon", self._add_addon)
        self._create_action("add-url", self._on_add_url)
        self._create_action("open-history", lambda *a: self._open_local_page("history"))
        self._create_action("open-watch-history", self._present_history)
        self._create_action("add-playlist-folder", self._on_open_folder_dialog)
        self._create_action("open-playlist-dialog", self._on_open_playlist)
        self._create_action("open-sub-menu", self._on_open_sub_menu)
        self._create_action("open-audio-menu", self._on_open_audio_menu)
        self._create_action("open-chapters-menu", self._on_open_chapters_menu)
        self._create_action("save-session", self._on_save_session)
        self._create_action("open-addons", self._open_addons)
        self._create_action("back-to-library", self._back_to_library)
        self._create_action("import-addons", self._import_addons)
        self._create_action("export-addons", self._export_addons)
        self._create_action(
            "save-session-close", lambda *a: self._on_save_session(close=True)
        )
        self._create_action("clear-session", self._on_clear_session)
        self._create_action("search-addons", self._on_search_addons)
        self._create_action("open-search", self._open_search)
        self._create_action("close-search", self._close_search)
        self._create_action("open-continue-watching", lambda *a: self._open_local_page("continue_watching"))
        self._create_action("open-favorites", lambda *a: self._open_local_page("favorites"))
        self._create_action("open-downloads", lambda *a: self._open_local_page("downloads"))
        self._create_action("open-watched", lambda *a: self._open_local_page("watched"))
        self._create_action("open-player", lambda *a: self.main_stack.set_visible_child_name("player"))

        self.app.set_accels_for_action("win.open-folder", ["<primary>i"])
        self.app.set_accels_for_action("win.open-url", ["<primary>u"])
        self.app.set_accels_for_action("win.add-url", ["<shift><primary>u"])
        self.app.set_accels_for_action("win.open-watch-history", ["<primary>h"])
        self.app.set_accels_for_action("win.add-playlist-folder", ["<shift><primary>i"])
        self.app.set_accels_for_action("win.open-playlist-dialog", ["<primary>p"])
        self.app.set_accels_for_action("win.clear-and-add", ["<primary>o"])
        self.app.set_accels_for_action("win.add-playlist-files", ["<shift><primary>o"])
        self.app.set_accels_for_action("win.open-sub-menu", ["<primary>s"])
        self.app.set_accels_for_action("win.open-audio-menu", ["<primary>a"])
        self.app.set_accels_for_action("win.open-chapters-menu", ["<primary>c"])
        self.app.set_accels_for_action("win.save-session", ["<shift><primary>s"])
        self.app.set_accels_for_action("win.save-session-close", ["<shift>q"])

        self._create_action("quit", lambda *a: self.close())
        self.app.set_accels_for_action("win.quit", ["q", "<primary>w"])

        self._create_action("custom-shortcuts", self._present_shortcuts)
        self.app.set_accels_for_action("win.custom-shortcuts", ["<primary>question"])
        self.app.set_accels_for_action("app.shortcuts", [])

        self._create_action("play-pause", self._on_play_pause_clicked)
        self._create_action("previous", self._on_previous_clicked)
        self._create_action("next", self._on_next_clicked)

    def _present_shortcuts(self, *args):
        builder = Gtk.Builder.new_from_resource(
            "/io/github/fastrizwaan/PopcornBox/shortcuts-dialog.ui"
        )
        self.shortcuts_dialog = cast(
            Adw.ShortcutsDialog,  # pyright: ignore[reportAttributeAccessIssue]
            builder.get_object("shortcuts_dialog"),
        )
        populate_shortcuts_dialog_mpv(self.shortcuts_dialog, self.bindings)
        self.shortcuts_dialog.present(self)

    def _present_history(self, *args):
        history_dialog = HistoryDialog(self)
        history_dialog.present(self)

    def _setup_widgets(self):
        self.set_default_size(DEFAULT_WIDTH, DEFAULT_HEIGHT)

        for widget in [
            self.controls_wrap_box,
            self.volume_box,
            self.volume_scale,
            self.progress_box,
            self.vid_progress_scale_box,
            self.video_progress_scale,
            self.time_elapsed_label,
        ]:
            widget.set_direction(Gtk.TextDirection.LTR)

        self.addons_listbox.set_filter_func(self._addon_filter_func)
        self.addon_url_entry.connect("changed", lambda *a: self.addons_listbox.invalidate_filter())

        self.library_stack.connect("notify::visible-child-name", self._on_library_stack_changed)
        self._on_library_stack_changed()

        max_vol = cast(int, self.mpv.volume_max)
        self.volume_scale_adj.set_upper(max_vol)

        self.mute_handler_id = self.mute_toggle_btn.connect(
            "toggled", lambda btn: setattr(self.mpv, "mute", btn.get_active())
        )

        vol_mid_click = Gtk.GestureClick(button=2)
        vol_mid_click.connect(
            "pressed",
            lambda *a: setattr(self.mpv, "mute", not self.mpv.mute),
        )
        self.volume_menu_btn.add_controller(vol_mid_click)

        self.fullscreen_btn.connect(
            "clicked",
            lambda *a: setattr(self.mpv, "fullscreen", not self.is_fs),
        )

        self.volume_handler_id = self.volume_scale.connect(
            "value-changed",
            lambda *a: setattr(self.mpv, "volume", self.volume_scale_adj.props.value),
        )

        if max_vol > 100:
            self.volume_scale.add_mark(100.0, Gtk.PositionType.BOTTOM, None)

        self.video_progress_adj.connect("value-changed", self._on_progress_adjusted)

        self.time_popover = Gtk.Popover(css_name="time-popover")
        self.time_popover.remove_css_class("background")
        self.time_popover.set_position(Gtk.PositionType.TOP)
        # video_progress_scale can be different heights because of marks, use a box instead
        self.time_popover.set_parent(self.vid_progress_scale_box)
        self.time_popover.set_autohide(False)
        self.time_popover.set_has_arrow(False)

        self.popover_content_box = Gtk.Box()
        self.popover_content_box.props.orientation = Gtk.Orientation.VERTICAL

        self.thumb_preview = Gtk.Picture()
        self.thumb_preview.set_valign(Gtk.Align.START)
        self.thumb_preview.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        self.thumb_preview.set_halign(Gtk.Align.CENTER)
        self.popover_content_box.append(self.thumb_preview)

        self.time_popover_rect = Gdk.Rectangle()
        self.time_popover_label = Gtk.Label()
        self.time_popover_label.set_use_markup(True)
        self.time_popover_label.set_justify(Gtk.Justification.CENTER)
        self.time_popover_label.set_xalign(0.5)
        self.time_popover_label.add_css_class("numeric")
        self.time_popover_label.set_halign(Gtk.Align.CENTER)

        self.popover_content_box.append(self.time_popover_label)
        self.time_popover.set_child(self.popover_content_box)

        self._set_time_margin()

        self.gl_area.connect("realize", self._on_realize_area)
        self.gl_area.connect("render", self._on_render_area)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_event, "keypress")
        key_controller.connect("key-released", self._on_key_event, "keyup")
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(key_controller)

        progress_hover = Gtk.EventControllerMotion()
        progress_hover.connect("motion", self._on_progress_motion)
        progress_hover.connect("leave", lambda *a: self.time_popover.popdown())
        self.video_progress_scale.add_controller(progress_hover)

        prog_mid_click = Gtk.GestureClick(button=2)
        prog_mid_click.connect("pressed", self._go_to_chapter_start)
        self.video_progress_scale.add_controller(prog_mid_click)

        ecs_flags = Gtk.EventControllerScrollFlags

        progress_ecs = Gtk.EventControllerScroll.new(ecs_flags.VERTICAL)
        progress_ecs.connect("scroll", self._on_progress_scroll)
        self.video_progress_scale.add_controller(progress_ecs)

        overlay_ecs = Gtk.EventControllerScroll.new(ecs_flags.BOTH_AXES)
        volume_ecs = Gtk.EventControllerScroll.new(ecs_flags.VERTICAL)
        self.video_overlay.add_controller(overlay_ecs)
        overlay_ecs.connect("scroll", self._on_mouse_scroll)
        self.volume_scale.add_controller(volume_ecs)
        volume_ecs.connect("scroll", self._on_mouse_scroll_volume)

        for btn_num in MBTN_MAP.keys():
            click_gesture = Gtk.GestureClick(button=btn_num)
            click_gesture.connect("pressed", self._on_click_pressed)
            click_gesture.connect("released", self._on_click_released)
            self.video_overlay.add_controller(click_gesture)

        long_press = Gtk.GestureLongPress.new()
        long_press.connect("pressed", self._on_click_hold)
        long_press.connect("end", self._cancel_click_hold)
        long_press.connect("cancelled", self._cancel_click_hold)
        self.window_handle.add_controller(long_press)

        @self._connect("notify::visible-dialog")
        def on_vis_dialog_change(*args):
            if dialog := self.get_visible_dialog():
                self.visible_dialog = dialog
                self.set_cursor_from_name(None)
                self._cancel_click_hold()
                self.space_holding = False
                self._set_space_holding(False)
            else:
                self.visible_dialog = None
            self._hide_ui_timeout()

        @self._connect("notify::is-active")
        def on_is_active_change(*args):
            if self.props.is_active:
                timeout_add_once(200, setattr, self, "is_inactive", False)
            else:
                self._cancel_click_hold()
                self.space_holding = False
                self._set_space_holding(False)
                self.is_inactive = True

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        drop_target.connect("drop", self._on_drop)
        self.video_overlay.add_controller(drop_target)

        self.motion_header_controls = Gtk.EventControllerMotion()
        self.motion_header_controls.connect("motion", self._on_mouse_motion)
        self.revealer_ui.add_controller(self.motion_header_controls)

        self.motion_header = Gtk.EventControllerMotion()
        self.motion_controls = Gtk.EventControllerMotion()
        self.headerbar.add_controller(self.motion_header)
        self.controls_box.add_controller(self.motion_controls)

        self.motion_controls_separator = Gtk.EventControllerMotion()
        self.controls_separator.add_controller(self.motion_controls_separator)

        @self._connect("notify::maximized")
        def on_maximized_change(*args):
            settings.set_boolean("is-maximized", self.is_maximized())

        self.connect("notify::fullscreened", self._set_fs_state)

        buttons = [
            self.primary_menu_btn,
            self.options_menu_btn,
            self.volume_menu_btn,
            self.subtitles_menu_btn,
            self.audio_tracks_menu_btn,
            self.video_tracks_menu_btn,
            self.chapters_menu_btn,
        ]
        for btn in buttons:
            popover = btn.props.popover
            popover.connect("closed", self._hide_ui_timeout)

            if btn == self.primary_menu_btn:

                def on_popv_closed(*args):
                    if is_same_playlist(self.mpv.playlist):
                        self.mpv.write_watch_later_config()

                popover.connect("closed", on_popv_closed)

        # TODO: remove for gnome 51
        # Somehow because the options menu contains other menus popovers inside,
        # when closing it, contains_pointer from header/controls still returns True,
        # even if not hovering; setting Gtk.PropagationLimit.NONE seems to be the only way to fix it
        # also sets Gtk.PropagationLimit.SAME_NATIVE back for the other buttons
        groups = {
            Gtk.PropagationLimit.SAME_NATIVE: [
                self.primary_menu_btn,
                self.volume_menu_btn,
                self.subtitles_menu_btn,
                self.audio_tracks_menu_btn,
                self.video_tracks_menu_btn,
                self.chapters_menu_btn,
            ],
            Gtk.PropagationLimit.NONE: [
                self.options_menu_btn,
            ],
        }
        for limit, buttons in groups.items():
            for btn in buttons:
                btn.connect(
                    "notify::active",
                    lambda *a, lim=limit: (
                        self.motion_header.set_propagation_limit(lim),
                        self.motion_controls.set_propagation_limit(lim),
                    ),
                )

        self._load_catalogs()

    def _set_fs_state(self, _window, _gparam):
        is_fullscreen = self.props.fullscreened

        try:
            if not is_fullscreen:
                self.mpv.fullscreen = is_fullscreen
        except mpv.ShutdownError:
            pass

        if not gtk_setts:
            return

        layout = gtk_setts.get_property("gtk-decoration-layout")

        if is_fullscreen:
            left_side, _colon, _right_side = layout.partition(":")
            layout = "close:" if "close" in left_side else ":close"

        self.headerbar.set_decoration_layout(layout)

    def _show_ui(self):
        self.set_cursor_from_name(None)
        self.revealer_ui.set_reveal_child(True)

    def _hide_ui_timeout(self, *args, s=2):
        if self.hide_timeout_id:
            GLib.source_remove(self.hide_timeout_id)
        self.hide_timeout_id = timeout_add_seconds_once(s, self._hide_ui)

    def _hide_ui(self, *args):
        try:
            self.hide_timeout_id = 0
            controls_hover = self.motion_controls.props.contains_pointer
            header_hover = self.motion_header.props.contains_pointer

            active_or_hover = (
                self.mpv.idle_active
                or header_hover
                or controls_hover
                or self.primary_menu_btn.props.active
                or self.options_menu_btn.props.active
                or self.volume_menu_btn.props.active
                or self.subtitles_menu_btn.props.active
                or self.audio_tracks_menu_btn.props.active
                or self.video_tracks_menu_btn.props.active
                or self.chapters_menu_btn.props.active
                or getattr(self, "is_loading_stream", False)
            )
            if not active_or_hover:
                self.revealer_ui.set_reveal_child(False)
                self.time_popover.popdown()

            if (
                (self.is_fs or not self.mpv["cursor-autohide-fs-only"])
                and not active_or_hover
                and not self.props.dialogs
            ):
                self.set_cursor_from_name("none")
        except mpv.ShutdownError:
            return

    def _on_mouse_motion(self, _controller, x, y):
        if None not in (x, y):
            if (x, y) == self.prev_motion_xy or self.click_holding:
                return

            if self.key_state & Gdk.ModifierType.CONTROL_MASK:
                mpv_x = int(x * self.props.scale_factor)
                mpv_y = int(y * self.props.scale_factor)
                self.mpv.command_async("mouse", mpv_x, mpv_y)

            self.prev_motion_xy = (x, y)
            self._show_ui()
            self._hide_ui_timeout()

    def _update_track_menus(self, track_list):
        self.subtitles_menu.remove_all()
        self.subtitles_menu.append(_("Add Subtitle Track"), "win.add-sub-tracks")

        item_none_sub = Gio.MenuItem.new(_("None"), None)
        item_none_sub.set_action_and_target_value(
            "win.select-subtitle", GLib.Variant("i", 0)
        )
        self.subtitles_menu.append_item(item_none_sub)

        self.audio_tracks_menu.remove_all()
        self.audio_tracks_menu.append(_("Add Audio Track"), "win.add-audio-tracks")

        item_none_audio = Gio.MenuItem.new(_("None"), None)
        item_none_audio.set_action_and_target_value(
            "win.select-audio", GLib.Variant("i", 0)
        )
        self.audio_tracks_menu.append_item(item_none_audio)

        self.video_tracks_menu.remove_all()

        for track in track_list:
            if track["type"] in ("sub", "audio", "video"):
                self._add_track_to_menu(track)

        video_count = len(
            [t for t in track_list if t["type"] == "video" and not t.get("albumart")]
        )
        self.video_tracks_menu_btn.set_visible(video_count > 1)

        def hide_box_first_model_btn(menu_btn):
            """Hide the space before add track label"""
            target = menu_btn.get_popover()
            for _i in range(8):
                if target:
                    target = target.get_first_child()
            if target:
                target.set_visible(False)

        hide_box_first_model_btn(self.subtitles_menu_btn)
        hide_box_first_model_btn(self.audio_tracks_menu_btn)

    def _add_track_to_menu(self, track):
        track_id = int(track.get("id", 0))
        track_type = track.get("type")
        lang = track.get("lang")
        title = track.get("title")

        label_parts = [p for p in (title, lang) if p]
        label = (
            " – ".join(label_parts) if label_parts else (_("Track") + f" {track_id}")
        )

        if track_type == "sub":
            menu = self.subtitles_menu
            action = "win.select-subtitle"
        elif track_type == "audio":
            menu = self.audio_tracks_menu
            action = "win.select-audio"
        else:
            menu = self.video_tracks_menu
            action = "win.select-video"

        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, GLib.Variant("i", track_id))
        menu.append_item(item)

    def _create_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        self.actions[name] = action

    def _create_action_stateful(self, name, callback, target_type):
        if target_type != "i":
            raise TypeError("_create_action_stateful int only")
        action = Gio.SimpleAction.new_stateful(
            name,
            GLib.VariantType.new(target_type),
            GLib.Variant("i", 0),
        )
        action.connect("activate", callback)
        self.add_action(action)
        self.actions[name] = action

    def _on_open_playlist(self, *args):
        if self.mpv.idle_active:
            return
        playlist = Playlist(self)
        playlist.present(self)

    def _on_open_folder_dialog(self, action, *args):
        add_mode = False if action.props.name == "open-folder" else True
        title = _("Add Folder") if add_mode else _("Open Folder")
        dialog = Gtk.FileDialog(title=title)
        curr_path = self.mpv.path

        if isinstance(curr_path, str) and os.path.exists(curr_path):
            folder_path = os.path.dirname(curr_path)
            dialog.set_initial_folder(Gio.File.new_for_path(folder_path))

        def on_open_response(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)

                if not add_mode:
                    self.mpv.stop()
                    self.mpv.pause = False
                    self.shuffle_toggle_btn.set_active(False)

                path = folder.get_path()
                self._clear_stream_failover()
                self.mpv.loadfile(path, "append-play")

            except GLib.Error as e:
                logger.warning(f"Dialog error: {e}")

        dialog.select_folder(self, None, on_open_response)
        return Gdk.EVENT_STOP  # so "<shift><primary>i" doesn't trigger inspector

    def _on_clear_and_add(self, _action, _param):
        self._open_add_dialog(_("Open Files"), "clear-and-add")

    def _on_add_playlist_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Files"), "playlist-add")
        return Gdk.EVENT_STOP

    def _on_add_sub_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Subtitle"), "sub-add")

    def _on_add_audio_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Audio"), "audio-add")

    def _open_add_dialog(self, title, mode):
        filter = Gtk.FileFilter()
        dialog = Gtk.FileDialog(title=title)
        filters_list = Gio.ListStore.new(Gtk.FileFilter)
        filters_list.append(filter)
        dialog.set_filters(filters_list)
        dialog.set_default_filter(filter)

        curr_path = self.mpv.path
        if isinstance(curr_path, str) and os.path.exists(curr_path):
            folder_path = os.path.dirname(curr_path)
            dialog.set_initial_folder(Gio.File.new_for_path(folder_path))

        if mode == "sub-add":
            filter.set_name(_("Subtitles"))
            for sub in SUB_EXTS:
                s = sub.lstrip(".")
                filter.add_suffix(s)
        elif mode == "audio-add":
            filter.set_name(_("Audio"))
            for m in ["video/*", "audio/*"]:
                filter.add_mime_type(m)
        else:
            filter.set_name(_("Media"))
            for m in ["video/*", "audio/*", "image/*"]:
                filter.add_mime_type(m)

        dialog.open_multiple(
            self,
            None,
            lambda d, res: self._on_open_response(d, res, mode),
        )

        if isinstance(self.visible_dialog, Playlist):
            self.visible_dialog.spinner.set_visible(True)

    def _on_search_addons(self, *args):
        from . import api
        dialog = Adw.Window(title="Search Addons", default_width=600, default_height=500)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        dialog.set_content(box)

        search_entry = Gtk.SearchEntry(placeholder_text="Search movies/tv...")
        search_entry.set_hexpand(True)
        search_entry.set_margin_top(10)
        search_entry.set_margin_start(10)
        search_entry.set_margin_end(10)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(list_box)
        scrolled.set_vexpand(True)
        
        box.append(search_entry)
        box.append(scrolled)
        
        def on_search_changed(entry):
            query = entry.get_text()
            if not query: return
            try:
                items = api.fetch_items(query=query)
                list_box.remove_all()
                for item in items:
                    row = Gtk.ListBoxRow()
                    lbl = Gtk.Label(label=f"{item.get('title', 'Unknown')} ({item.get('year', '')})")
                    lbl.set_halign(Gtk.Align.START)
                    lbl.set_margin_top(10)
                    lbl.set_margin_bottom(10)
                    lbl.set_margin_start(10)
                    row.set_child(lbl)
                    row.item_data = item
                    list_box.append(row)
            except Exception as e:
                print(f"Search error: {e}")

        search_entry.connect("search-changed", on_search_changed)
        
        def on_row_activated(lb, row):
            try:
                item = row.item_data
                details = api.fetch_movie_details(item["id"], media_type=item.get("type", "movie"), title=item["title"])
                streams = api.get_torrents_streamed(details["id"], media_type=item.get("type", "movie"), title=item.get("title") or details.get("name"))
                
                direct_stream = None
                for s in streams:
                    if s.get("is_http") and s.get("url"):
                        direct_stream = s["url"]
                        break
                
                if direct_stream:
                    dialog.close()
                    self.mpv.loadfile(direct_stream, "replace")
                    self.mpv.pause = False
                else:
                    print("No direct HTTP stream found for this item.")
            except Exception as e:
                print(f"Playback error: {e}")
                
        list_box.connect("row-activated", on_row_activated)
        dialog.set_transient_for(self)
        dialog.present()

    def _on_open_response(self, dialog, result, mode):
        try:
            files = dialog.open_multiple_finish(result)

            if mode == "clear-and-add":
                self.mpv.stop()
                self.shuffle_toggle_btn.set_active(False)

            for file in files:
                path = file.get_path() or file.get_uri()

                if mode == "sub-add":
                    self.mpv.sub_add(path)
                elif mode == "audio-add":
                    self.mpv.audio_add(path)
                else:
                    self._clear_stream_failover()
                    self.mpv.loadfile(path, "append-play")

            if mode == "clear-and-add":
                self.mpv.pause = False
        except GLib.Error as e:
            logger.warning(f"Dialog error: {e}")
        finally:
            if isinstance(self.visible_dialog, Playlist):
                self.visible_dialog.spinner.set_visible(False)

    def _on_open_sub_menu(self, *args):
        self._show_ui()
        self.subtitles_menu_btn.popup()

    def _on_open_audio_menu(self, *args):
        self._show_ui()
        self.audio_tracks_menu_btn.popup()

    def _on_open_chapters_menu(self, *args):
        if not self.mpv.chapters:
            return
        self._show_ui()
        self.chapters_menu_btn.popup()

    def _on_save_session(self, *args, close=False):
        settings.set_boolean("save-session", True)
        save_last_playlist_file(self.mpv)
        if close:
            self.close()
        else:
            idle_add_once(self._show_toast, _("Session Saved"))

    def _on_clear_session(self, *args):
        from .utils import LAST_PLAYLIST_FILE, CONFIG_DIR
        import os
        import shutil
        try:
            if os.path.exists(LAST_PLAYLIST_FILE):
                os.remove(LAST_PLAYLIST_FILE)
            
            watch_later_dir = os.path.join(CONFIG_DIR, "watch_later")
            if os.path.exists(watch_later_dir):
                shutil.rmtree(watch_later_dir)
                
            self._show_toast(_("Saved session cleared"))
        except Exception as e:
            logger.error(f"Error clearing session: {e}")

    def _on_open_url(self, *args, add=False):
        mode = "append-play" if add else "replace"
        view = Adw.ToolbarView()
        header_bar = Adw.HeaderBar()
        h_title = _("Add URL") if add else _("Open URL")
        header_bar.set_title_widget(Adw.WindowTitle(title=h_title))
        view.add_top_bar(header_bar)

        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )

        view.set_content(content_box)
        entry_row = Adw.EntryRow(title=_("URL"), activates_default=True)
        list_box = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        list_box.append(entry_row)
        content_box.append(list_box)

        btn_open = Gtk.Button(
            label=_("Add") if add else _("Open"),
            css_classes=["pill", "suggested-action"],
            halign=Gtk.Align.CENTER,
            sensitive=False,
        )

        content_box.append(btn_open)
        dialog = Adw.Dialog(content_width=450, child=view, default_widget=btn_open)
        self.url = ""

        def is_valid_input(text):
            url = text.strip()
            parsed = urlparse(url)
            if parsed.scheme in cast(list, self.mpv.protocol_list):
                self.url = url
                return True
            elif os.path.exists(url):
                self.url = url
                return True
            elif url:
                self.url = f"https://{url}"
                return True
            return False

        def on_text_changed(*args):
            is_valid = is_valid_input(entry_row.get_text())
            btn_open.set_sensitive(is_valid)

        entry_row.connect("notify::text", on_text_changed)

        def open_url(*args):
            dialog.close()
            try:
                self._clear_stream_failover()
                self.mpv.loadfile(self.url, mode)
                if mode == "replace":
                    self.mpv.pause = False
                    self.shuffle_toggle_btn.set_active(False)
            except mpv.ShutdownError:
                pass

        def on_clipboard_read(clipboard, result):
            try:
                text = clipboard.read_text_finish(result)
                if text and (parsed := urlparse(text)):
                    if parsed.scheme in cast(list, self.mpv.protocol_list):
                        entry_row.insert_text(text, 0)
            except Exception:
                pass

        if display and (clipboard := display.get_clipboard()):
            clipboard.read_text_async(None, on_clipboard_read)

        btn_open.connect("clicked", open_url)
        dialog.present(self)

    def _on_add_url(self, *args):
        self._on_open_url(add=True)
        return Gdk.EVENT_STOP

    def setup_preview_player(self):
        if not self.is_local_path:
            self.thumb_preview.props.visible = False
            return

        try:
            params = cast(dict, self.mpv.video_params)
            v_width = params.get("w") or 1920
            v_height = params.get("h") or 1080
        except Exception:
            v_width, v_height = 1920, 1080

        if v_width >= v_height:
            # Horizontal or square
            width = 180
            height = int((v_height / v_width) * width)
        else:
            # Vertical
            height = 180
            width = int((v_width / v_height) * height)

        if self.preview_player is None:
            self.preview_player = mpv.MPV(
                vo="null",
                ao="null",
                hwdec=self.mpv.hwdec,
                ytdl=False,
                config=False,
                osc=False,
                terminal=False,
                load_scripts=False,
                msg_level="all=no",
                vd_lavc_threads=2,
                vd_lavc_fast=True,
                vd_lavc_skiploopfilter="all",
                vd_lavc_software_fallback=1,
                sws_scaler="fast-bilinear",
                demuxer_readahead_secs=0,
                demuxer_max_bytes="128KiB",
                hr_seek=False,
                gpu_dumb_mode=True,
                pause=True,
                ovc="rawvideo",
                of="image2",
                ofopts="update=1",
            )

            self.preview_player["load-osd-console"] = "no"
            self.preview_player["load-stats-overlay"] = "no"
            self.preview_player["load-auto-profiles"] = "no"
            self.preview_player["really-quiet"] = "yes"

            @self.preview_player.property_observer("time-pos")
            def pos_observer(_name, pos):
                if pos and pos >= 0:

                    def on_screenshot_ready(_, result):
                        if result is None:
                            self.thumb_preview.props.visible = False
                            return

                        self._apply_preview_texture(result)

                    if self.preview_player:
                        self.preview_player.command_async(
                            "screenshot-raw",
                            callback=on_screenshot_ready,
                        )

        self.preview_player.loadfile(self.mpv.path, "replace")
        self.preview_player["vf"] = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,format=bgra"
        )

    def _update_video_preview(self):
        if (
            self.preview_player is None
            or not self.preview_player.path
            or self.last_preview_seek == int(self.hover_time)
        ):
            return

        self.last_preview_seek = int(self.hover_time)

        try:
            self.preview_player.command_async(
                "seek", self.hover_time, "absolute+keyframes"
            )
        except Exception:
            pass

    def _apply_preview_texture(self, res):
        try:
            self.thumb_preview.props.paintable = Gdk.MemoryTexture.new(
                res["w"],
                res["h"],
                Gdk.MemoryFormat.B8G8R8X8,
                GLib.Bytes.new(res["data"]),
                res["stride"],
            )
        except Exception as e:
            self.thumb_preview.props.visible = False
            logger.error(f"Preview texture error: {e}")

    def _on_progress_motion(self, _controller, x, y):
        if (x, y) == self.prev_prog_motion_xy:
            return

        self.prev_prog_motion_xy = (x, y)

        if self.late_preview_id > 0:
            GLib.source_remove(self.late_preview_id)

        self.late_preview_id = timeout_add_once(120, self._late_update_preview)

        width = self.video_progress_scale.get_width()
        duration = self.video_progress_adj.props.upper
        if width <= 0 or duration <= 0:
            return

        percentage = max(0, min(1, x / width))
        self.hover_time = percentage * duration

        self.curr_chapter_time = None
        curr_chapter = None

        for chapter in self.chapters:
            c_time = chapter.get("time", 0)
            if c_time <= self.hover_time:
                curr_chapter = chapter
                self.curr_chapter_time = c_time
            else:
                break

        time_str = format_time(self.hover_time)
        if curr_chapter:
            title = curr_chapter.get("title", _("Chapter"))
            title = GLib.markup_escape_text(title)
            markup = f"<b>{title}</b>\n{time_str}"
        else:
            markup = f"{time_str}"

        self.time_popover_label.set_markup(markup)

        clamped_x = max(2, min(x, width - 2))
        self.time_popover_rect.x = clamped_x
        self.time_popover_rect.y = 0
        self.time_popover_rect.width = 41
        self.time_popover.set_pointing_to(self.time_popover_rect)
        self.time_popover.popup()

        if not settings.get_boolean("thumbnail-preview") or not self.is_local_path:
            return

        curr_time = time()

        if curr_time - self.last_preview_update > 0.3:
            self.last_preview_update = curr_time
            idle_add_once(self._update_video_preview)

    def _late_update_preview(self):
        """Update preview when the cursor is stopped"""
        self.late_preview_id = 0
        idle_add_once(self._update_video_preview)

    def _go_to_chapter_start(self, *args):
        if self.curr_chapter_time is not None:
            self.mpv.command_async("seek", self.curr_chapter_time, "absolute")

    def _on_progress_scroll(self, controller, _dx, dy):
        event: Gdk.ScrollEvent = controller.get_current_event()

        self.key_state = event.get_modifier_state()

        if self.key_state & Gdk.ModifierType.CONTROL_MASK:
            return True

        direction: Gdk.ScrollDirection = event.get_direction()
        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        step = dy if direction == Gdk.ScrollDirection.SMOOTH else dy * 10

        if is_natural:
            step = -step

        adj = self.video_progress_scale.get_adjustment()
        progress = adj.get_value()
        new_progress = progress - step
        adj.set_value(new_progress)

        return True

    def _update_volume_icon(self):
        volume = cast(float, self.mpv.volume)
        is_muted = self.mpv.mute

        if is_muted or volume == 0:
            icon = "cine-volume-mute-symbolic"
        elif volume < 33:
            icon = "cine-volume-low-symbolic"
        elif volume < 66:
            icon = "cine-volume-mid-symbolic"
        elif volume <= 100.5:
            icon = "cine-volume-max-symbolic"
        else:
            icon = "cine-volume-overamp-symbolic"

        self.volume_menu_btn.props.icon_name = icon

    def _set_time_margin(self):
        self.time_elapsed_label.props.margin_end = 3 if self.show_remaining else 0

    @Gtk.Template.Callback()
    def _toggle_elapsed_remaining(self, _btn):
        self.show_remaining = not self.show_remaining
        settings.set_boolean("show-remaining", self.show_remaining)
        pos = float(self.mpv.time_pos or 0)
        self._update_progress(pos, update_bar=False)
        self._set_time_margin()

    def _update_progress(self, curr_time, update_bar=True):
        curr_time = round(curr_time, 1)

        if update_bar and curr_time == self.prev_prog_time:
            return

        if update_bar:
            self.video_progress_adj.handler_block_by_func(self._on_progress_adjusted)
            self.video_progress_adj.props.value = curr_time
            self.video_progress_adj.handler_unblock_by_func(self._on_progress_adjusted)

        try:
            duration = float(self.mpv.duration or 0)
            remaining = (duration - curr_time) if duration > curr_time else 0

            if self.show_remaining:
                self.time_elapsed_label.props.label = f"-{format_time(remaining)}"
            else:
                self.time_elapsed_label.props.label = format_time(curr_time)

            is_trailer = self._is_playing_trailer()
            auto_play_enabled = settings.get_boolean("auto-play-next")
            if not is_trailer and auto_play_enabled and not getattr(self, "next_ep_dismissed", False) and duration > 30 and remaining <= 30 and remaining > 0 and self._has_next_episode():
                if hasattr(self, "next_episode_revealer"):
                    rem_sec = max(1, int(remaining))
                    if hasattr(self, "next_ep_label"):
                        self.next_ep_label.set_text(f"Next episode in {rem_sec}s")
                    if not self.next_episode_revealer.get_reveal_child():
                        self.next_episode_revealer.set_reveal_child(True)

                if remaining <= 1.0 and not getattr(self, "next_ep_auto_triggered", False):
                    self.next_ep_auto_triggered = True
                    if hasattr(self, "next_episode_revealer"):
                        self.next_episode_revealer.set_reveal_child(False)
                    self._try_play_next_episode()
            else:
                if hasattr(self, "next_episode_revealer") and self.next_episode_revealer.get_reveal_child():
                    self.next_episode_revealer.set_reveal_child(False)

            # Track Continue Watching progress and mark verified working stream
            if curr_time >= 0.1 and getattr(self, "stream_queue", None) and not is_trailer:
                last_marked = getattr(self, "_last_marked_stream_idx", None)
                if last_marked != self.stream_queue_index:
                    self._last_marked_stream_idx = self.stream_queue_index
                    self._mark_current_stream_as_working()

            if getattr(self, "_current_playing_item", None) and not is_trailer:
                prog = min(1.0, max(0.0, curr_time / duration)) if duration > 0 else 0.0
                self._current_playing_item["position"] = curr_time
                self._current_playing_item["duration"] = duration
                self._current_playing_item["progress"] = prog
                
                last_cw_save = getattr(self, "_last_cw_save_time", 0)
                import time
                now = time.time()
                if now - last_cw_save >= 4:
                    self._last_cw_save_time = now
                    from . import database
                    if prog >= 0.92:
                        item_id = self._current_playing_item.get("id") or self._current_playing_item.get("imdb_id")
                        database.remove_continue_watching(item_id)
                    else:
                        database.save_continue_watching(self._current_playing_item)
                    if hasattr(self, "_update_continue_watching_section"):
                        curr_stack = self.main_stack.get_visible_child_name() if hasattr(self, "main_stack") else None
                        if curr_stack != "player":
                            self._update_continue_watching_section()
        except mpv.ShutdownError:
            pass

        self.prev_prog_time = curr_time

    def _update_chapter_marks_and_menu(self, chapters):
        self.video_progress_scale.clear_marks()
        if not chapters:
            self.chapters_menu_btn.set_visible(False)
            return

        for chapter in chapters:
            time_pos = chapter.get("time")
            if time_pos is not None:
                self.video_progress_scale.add_mark(
                    float(time_pos), Gtk.PositionType.TOP, None
                )

        self.chapters_menu_btn.set_visible(True)
        self.chapters_menu.remove_all()

        for i, chapter in enumerate(chapters):
            title = chapter.get("title") or _("Chapter") + f" {i + 1}"
            item = Gio.MenuItem.new(title, None)
            item.set_action_and_target_value("win.select-chapter", GLib.Variant("i", i))
            self.chapters_menu.append_item(item)

    def _navigate_playlist(self, direction: int):
        pos = int(self.mpv.playlist_pos or 0)
        count = int(self.mpv.playlist_count or 0)

        if count > 0:
            self.mpv.playlist_pos = (pos + direction) % count

    @Gtk.Template.Callback()
    def _on_previous_clicked(self, *args):
        self._navigate_playlist(-1)

    @Gtk.Template.Callback()
    def _on_next_clicked(self, *args):
        self._navigate_playlist(+1)

    def _on_subtitle_selected(self, action, parameter):
        track_id = parameter.get_int32()
        if track_id > 0:
            try:
                self.mpv["sub-visibility"] = "yes"
            except Exception:
                pass
            self.mpv.sid = track_id
        else:
            self.mpv.sid = "no"
        action.set_state(parameter)

    def _on_audio_selected(self, action, parameter):
        track_id = parameter.get_int32()
        self.mpv.aid = track_id
        action.set_state(parameter)

    def _on_video_selected(self, action, parameter):
        track_id = parameter.get_int32()
        self.mpv.vid = track_id
        action.set_state(parameter)

    def _on_chapter_selected(self, action, parameter):
        chapter_index = parameter.get_int32()
        self.mpv.chapter = chapter_index
        action.set_state(parameter)

    @Gtk.Template.Callback()
    def _sync_chapter_menu_selected(self, *args):
        if action := self.lookup_action("select-chapter"):
            action.set_state(  # pyright: ignore[reportAttributeAccessIssue]
                GLib.Variant("i", self.mpv.chapter)
            )

    def _update_play_pause_icon(self, paused):
        play = "cine-playback-start-symbolic"
        pause = "cine-playback-pause-symbolic"

        btn_icon = play if paused else pause
        self.play_pause_btn.set_icon_name(btn_icon)

        text = _("Play") if paused else _("Pause")
        self.play_pause_btn.update_property([Gtk.AccessibleProperty.LABEL], [text])

        self.icon_indicator.props.icon_name = pause if paused else play
        self._show_icon_indicator()
        self.app_mpris._update_playback_status(paused)

    def _update_duration(self, duration):
        self.time_total_label.set_text(format_time(duration))

        if duration == 0:
            self.video_progress_scale.set_sensitive(False)
            self.time_popover.popdown()
            return

        self.video_progress_scale.set_sensitive(True)

        self.video_progress_adj.set_upper(duration)

        if duration >= 86400:
            chars = 10
        elif duration >= 3600:
            chars = 7
        elif duration >= 600:
            chars = 6
        else:
            chars = 5

        self.time_elapsed_label.set_width_chars(chars)

    @Gtk.Template.Callback()
    def _on_play_pause_clicked(self, *args):
        self.mpv.pause = not self.mpv.pause

    def _on_progress_adjusted(self, adjustment):
        self.mpv.command_async("seek", adjustment.props.value, "absolute")

    @Gtk.Template.Callback()
    def _on_shuffle_toggled(self, button):
        active = button.props.active

        cmd = "playlist-shuffle" if active else "playlist-unshuffle"
        self.mpv.command(cmd)

        self.app_mpris._update_shuffle(active)
        self.prev_shuffle = not active

        if isinstance(self.visible_dialog, Playlist):
            idle_add_once(self._splice_playlist)

    def _set_loop_state(self, loop, active):
        if loop == "playlist":
            self.mpv.loop_playlist = "inf" if active else "no"
            if active:
                self.mpv.loop_file = "no"
                self.loop_file_btn.set_active(False)
            self._update_playlist_nav_sensitivity()

        elif loop == "file":
            self.mpv.loop_file = "inf" if active else "no"
            if active:
                self.mpv.loop_playlist = "no"
                self.loop_playlist_btn.set_active(False)

    @Gtk.Template.Callback()
    def _on_loop_playlist_toggled(self, button):
        self._set_loop_state("playlist", button.props.active)

    @Gtk.Template.Callback()
    def _on_loop_file_toggled(self, button):
        self._set_loop_state("file", button.props.active)

    def _update_playlist_nav_sensitivity(self):
        try:
            count: int = cast(int, self.mpv.playlist_count) or 0
            pos: int = cast(int, self.mpv.playlist_pos) or 0
            loop_list_enabled: bool = self.mpv.loop_playlist == "inf"

            has_multiple: bool = count > 1

            self.can_go_prev = loop_list_enabled or (has_multiple and pos > 0)
            self.can_go_next = loop_list_enabled or (has_multiple and pos < count - 1)

            self.app_mpris._update_can_prev_next(self.can_go_prev, self.can_go_next)

            self.previous_btn.props.sensitive = self.can_go_prev
            self.next_btn.props.sensitive = self.can_go_next

            self.actions["previous"].props.enabled = self.can_go_prev
            self.actions["next"].props.enabled = self.can_go_next

            self.shuffle_toggle_btn.props.visible = has_multiple
            self.loop_playlist_btn.props.visible = has_multiple
        except mpv.ShutdownError:
            pass

    def _on_drop_enter(self, target, _x, _y):
        self.revealer_drop_indicator.set_reveal_child(True)
        drop = target.get_current_drop()
        formats = drop.get_formats()
        target_type = (
            Gdk.FileList if formats.contain_gtype(Gdk.FileList) else GObject.TYPE_STRING
        )

        def on_read_done(source, result):
            try:
                value = source.read_value_finish(result)

                if isinstance(value, Gdk.FileList):
                    f_name = value.get_files()[0].get_basename() or ""
                    f_name = f_name.lower()
                    is_playing = not self.mpv.idle_active

                    if is_playing and any(f_name.endswith(ext) for ext in SUB_EXTS):
                        self.drop_icon.props.icon_name = "cine-subtitles-symbolic"
                        self.drop_label.props.label = _("Add Subtitle Track")
                        return

                self.drop_icon.props.icon_name = "cine-playback-start-symbolic"
                self.drop_label.props.label = _("Play")

            except GLib.Error as e:
                logger.warning(f"File error path: {self.loaded_path}")
                idle_add_once(self._show_toast, _("File Error") + f": {e.message}")
                self.spinner.set_visible(False)
                return

        drop.read_value_async(target_type, GLib.PRIORITY_DEFAULT, None, on_read_done)
        return True

    def _on_drop_leave(self, _target):
        self.revealer_drop_indicator.set_reveal_child(False)
        self.drop_icon.set_from_icon_name("")
        self.drop_label.set_text("")

    def _on_drop(self, _target, value, _x, _y):
        first_file = True

        if is_same_playlist(self.mpv.playlist):
            self.mpv.write_watch_later_config()

        items: list[Gio.File] | list[str] = []

        if isinstance(value, Gdk.FileList):
            items = value.get_files()
        elif isinstance(value, str):
            items = [value]

        for item in items:
            mode = "replace" if first_file else "append-play"

            if isinstance(item, Gio.File):
                path = item.get_path() or item.get_uri()

                is_url = not is_local_path(path)  # URL Thumbnail

                if is_url:
                    self._clear_stream_failover()
                    self.mpv.loadfile(path, mode)
                    first_file = False
                    continue
                else:
                    try:
                        info = item.query_info(
                            "standard::content-type,standard::type",
                            Gio.FileQueryInfoFlags.NONE,
                            None,
                        )
                    except Exception as e:
                        logger.error(f"Drop error: {e}", exc_info=True)
                        idle_add_once(self._show_toast, str(e))
                        return

                file_type = info.get_file_type()
                mime_type = info.get_content_type() or ""

                if file_type == Gio.FileType.DIRECTORY:
                    self._clear_stream_failover()
                    self.mpv.loadfile(path, mode)
                    first_file = False
                    continue

                name = cast(str, item.get_basename()).lower()
                if name.endswith(SUB_EXTS):
                    if not self.mpv.core_idle:
                        self.mpv.command("sub-add", path, "select")
                    continue

                if mime_type.startswith(("video/", "audio/", "image/")) or is_url:
                    self._clear_stream_failover()
                    self.mpv.loadfile(path, mode)
                    first_file = False

            elif isinstance(item, str):  # URL string
                self._clear_stream_failover()
                self.mpv.loadfile(item, mode)
                first_file = False

            if mode == "replace":
                self.mpv.command_async("set", "pause", "no")

    def _sync_fullscreen(self, mpv_is_fs: bool):
        self.is_fs = mpv_is_fs
        self.fullscreen() if mpv_is_fs else self.unfullscreen()

    def _set_space_holding(self, hold):
        if hold:
            self.space_hold_id = 0
            if self.click_holding:
                return

            # prevent being able to open menus when clicking buttons while holding spacebar
            # because that causes issues with the internal gtk button handling (space activates it)
            # and it becomes impossible to activate anything again in the window with mouse clicks
            # unless a menu popover is opened again (with keyboard enter)
            self.set_can_target(False)

            self.space_holding = True

            try:
                self.mpv.pause = False
                self.prev_speed = cast(float, self.mpv["speed"])
                new_speed = self.prev_speed * 2
                self.mpv["speed"] = new_speed
                self.mpv.show_text(f"{new_speed:g}× ⯈⯈", "100000000")
            except mpv.ShutdownError:
                pass
        else:
            self.set_can_target(True)

            if self.space_hold_id:
                GLib.source_remove(self.space_hold_id)
                self.space_hold_id = 0

            if self.space_pressed:
                self.space_pressed = False
                try:
                    self.mpv["speed"] = self.prev_speed
                    self.mpv.show_text(f"{self.mpv['speed']:g}×")
                except mpv.ShutdownError:
                    pass

    def _on_key_event(self, _controller, keyval, _keycode, state, event_type):
        focused = self.get_focus()
        if isinstance(focused, (Gtk.Editable, Gtk.TextView)):
            return False

        key_name = Gdk.keyval_name(keyval)

        if self.space_holding and event_type == "keyup":
            self._set_space_holding(False)

        if key_name in ("Tab", "ISO_Left_Tab", "Return"):
            self.revealer_ui.set_reveal_child(True)
            self._hide_ui_timeout(s=3)
            self._set_space_holding(False)
            return

        self.key_state = state
        clean_state = state & Gtk.accelerator_get_default_mod_mask()
        accel = Gtk.accelerator_name(keyval, clean_state)
        shortcuts_accel = "<Shift><Control>question"
        if self.app.get_actions_for_accel(accel) or accel == shortcuts_accel:
            self._set_space_holding(False)
            return

        mpv_key = chr(Gdk.keyval_to_unicode(keyval))
        mpv_key = KEY_REMAP.get(key_name, mpv_key)

        mods = []
        if state & Gdk.ModifierType.CONTROL_MASK:
            mods.append("Ctrl")
        if state & Gdk.ModifierType.ALT_MASK:
            mods.append("Alt")
        if state & Gdk.ModifierType.SHIFT_MASK:
            mods.append("Shift")

        combo = "+".join(mods + [mpv_key])

        if event_type == "keypress":
            if combo in self.nonrepeat_keys and combo in self.pressed_combos:
                return True
            self.pressed_combos.add(combo)
        elif event_type == "keyup":
            self.pressed_combos.discard(combo)

        if combo == "SPACE":
            if event_type == "keypress":
                if self.space_pressed:
                    return True

                self.space_pressed = True

                self.space_hold_id = timeout_add_once(
                    500, self._set_space_holding, True
                )
            elif event_type == "keyup":
                if self.space_hold_id:
                    GLib.source_remove(self.space_hold_id)
                    self.space_hold_id = 0

                if not self.space_holding:
                    self.mpv.command_async("keypress", "SPACE")
                    if self.space_pressed:
                        self.space_pressed = False

            self.space_holding = False
            return True

        try:
            self.mpv.command_async(event_type, combo)
            return True
        except mpv.ShutdownError:
            pass

    def _on_click_pressed(self, gesture, _n_press, x, y):
        button = MBTN_MAP.get(gesture.get_button())
        self.left_clk = settings.get_int("left-click")
        self.right_clk = settings.get_int("right-click")

        if not button or self._is_hovering():
            return

        if button == "MBTN_RIGHT" and self.right_clk == SecondaryClick.CONTEXT_MENU:
            if not self.mpv.idle_active:
                rect = Gdk.Rectangle()
                rect.x = x
                rect.y = y
                self.context_popover_menu.set_pointing_to(rect)
                self.context_popover_menu.popup()
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                return

        if button != "MBTN_LEFT":
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        # Back and forward dont trigger _on_click_released when video is playing (??)
        if button in ("MBTN_BACK", "MBTN_FORWARD"):
            self.mpv.command_async("keypress", button)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return

        self._show_ui()
        self._hide_ui_timeout()

    def _on_click_hold(self, gesture, *args):
        try:
            if self.space_holding or self._is_hovering():
                return

            self.click_holding = True
            self.mpv.pause = False
            self.prev_speed = cast(float, self.mpv["speed"])
            new_speed = self.prev_speed * 2
            self.mpv["speed"] = new_speed
            self.mpv.show_text(f"{new_speed:g}× ⯈⯈", "100000000")
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except mpv.ShutdownError:
            pass

    def _on_click_released(self, gesture, n_press, _x, _y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        button = MBTN_MAP.get(gesture.get_button())

        ignored_btn = not button or button in ("MBTN_BACK", "MBTN_FORWARD")
        ignore_left = (
            self.is_inactive
            and button == "MBTN_LEFT"
            and self.left_clk == PrimaryClick.FOCUS_PLAY_PAUSE
        )

        if ignored_btn or ignore_left or self._is_hovering():
            return

        if self.click_delay_id:
            GLib.source_remove(self.click_delay_id)
            self.click_delay_id = 0

        def run_command(cmd):
            try:
                for sub_cmd in cmd.split(";"):
                    args = shlex.split(sub_cmd.strip())
                    self.mpv.command_async(*args)
            except Exception:
                pass

        if n_press == 1 and not self.click_holding:
            cmd_str = str(self.mouse_bindings.get(button))

            if button == "MBTN_LEFT" and self.left_clk != PrimaryClick.BYPASS:

                def click():
                    self.mpv.command_async("cycle", "pause")
                    self.click_delay_id = 0

                self.click_delay_id = timeout_add_once(self.click_time, click)

            elif button == "MBTN_RIGHT" and self.right_clk == SecondaryClick.PLAY_PAUSE:
                self.mpv.command_async("cycle", "pause")

            else:
                run_command(cmd_str)

        elif n_press == 2:
            button_dbl = f"{button}_DBL"
            cmd_str = self.mouse_bindings.get(button_dbl)
            run_command(cmd_str)

    def _cancel_click_hold(self, *args):
        if self.click_holding:
            try:
                self.mpv["speed"] = self.prev_speed
                self.mpv.show_text(f"{self.mpv['speed']:g}×")
            except mpv.ShutdownError:
                pass
            self.click_holding = False

    def _on_mouse_scroll(self, controller, dx, dy):
        event: Gdk.ScrollEvent = controller.get_current_event()

        if event.get_unit() == Gdk.ScrollUnit.SURFACE:  # Touchpad
            # Scale it down so it doesn't fire rapidly
            dx *= 0.1
            dy *= 0.1

        self.wheel_accum_x += dx
        self.wheel_accum_y += dy

        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        UP: str = "WHEEL_DOWN" if is_natural else "WHEEL_UP"
        DOWN: str = "WHEEL_UP" if is_natural else "WHEEL_DOWN"
        LEFT: str = "WHEEL_RIGHT" if is_natural else "WHEEL_LEFT"
        RIGHT: str = "WHEEL_LEFT" if is_natural else "WHEEL_RIGHT"
        wheel: str | None = None

        self.key_state = event.get_modifier_state()

        mods = []
        if self.key_state & Gdk.ModifierType.CONTROL_MASK:
            mods.append("ctrl")
        if self.key_state & Gdk.ModifierType.ALT_MASK:
            mods.append("alt")
        if self.key_state & Gdk.ModifierType.SHIFT_MASK:
            mods.append("shift")

        # Only trigger if scrolled a full 'unit'
        if abs(self.wheel_accum_y) >= 1:
            wheel = UP if self.wheel_accum_y < 0 else DOWN
            self.wheel_accum_y = 0.0
        elif abs(self.wheel_accum_x) >= 1:
            wheel = RIGHT if self.wheel_accum_x > 0 else LEFT
            self.wheel_accum_x = 0.0

        if wheel:
            combo = "+".join(mods + [wheel])
            self.mpv.command_async("keypress", combo)

        return True

    def _on_mouse_scroll_volume(self, controller, _dx, dy):
        event: Gdk.ScrollEvent = controller.get_current_event()
        direction: Gdk.ScrollDirection = event.get_direction()
        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        max_vol = cast(float, self.mpv.volume_max)
        step = dy if direction == Gdk.ScrollDirection.SMOOTH else dy * 5

        if is_natural:
            step = -step

        adj = self.volume_scale.get_adjustment()
        volume = adj.get_value()
        new_vol = int(volume - step)
        new_vol = max(adj.get_lower(), min(new_vol, max_vol))
        adj.set_value(new_vol)

        return True

    def _is_hovering(self):
        controls_hover = self.motion_controls.props.contains_pointer
        header_hover = self.motion_header.props.contains_pointer
        separator_hover = self.motion_controls_separator.props.contains_pointer
        hovering = (controls_hover or header_hover) and not separator_hover
        return hovering

    def _on_realize_area(self, area):
        area.make_current()

        proc_address_fn = mpv.MpvGlGetProcAddressFn(
            lambda _inst, name: egl_get_proc_address(name)
        )

        display_param = get_display_param()

        self.mpv_ctx = mpv.MpvRenderContext(
            self.mpv,
            "opengl",
            opengl_init_params={
                "get_proc_address": proc_address_fn,
            },
            **display_param,
        )

        self.mpv_ctx.update_cb = lambda: idle_add_once(self.gl_area.queue_render)

        self.fbo = ctypes.c_int()

    def _on_render_area(self, area, _context):
        try:
            glGetIntegerv(GL_FRAMEBUFFER_BINDING, self.fbo)

            self.mpv_ctx.render(
                flip_y=True,
                opengl_fbo={
                    "w": area.get_width() * area.props.scale_factor,
                    "h": area.get_height() * area.props.scale_factor,
                    "fbo": self.fbo.value,
                },
            )
            return True
        except Exception as e:
            logger.error(f"Render error: {e}", exc_info=True)
            return False

    def _set_window_size(self, width, height):
        if width <= 0 or height <= 0:
            return

        aspect_ratio = width / height
        base_size = DEFAULT_HEIGHT

        if aspect_ratio < 1:
            new_h = int(base_size / aspect_ratio)
            new_w = base_size
        else:
            new_w = int(base_size * aspect_ratio)
            new_h = base_size

        MAX_W, MAX_H = 1280, 720
        if new_w > MAX_W or new_h > MAX_H:
            scale = min(MAX_W / new_w, MAX_H / new_h)
            new_w = int(new_w * scale)
            new_h = int(new_h * scale)

        self.set_default_size(new_w, new_h)

    def _sync_inhibit(self):
        try:
            should_inhibit = not self.mpv.pause and not self.mpv.idle_active
        except mpv.ShutdownError:
            should_inhibit = False

        if should_inhibit and self.inhibit_cookie == 0:
            self.inhibit_cookie = self.app.inhibit(
                self,
                Gtk.ApplicationInhibitFlags.IDLE,
                "Playing Media",
            )
        elif not should_inhibit and self.inhibit_cookie != 0:
            self.app.uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def _show_icon_indicator(self):
        if self.mpv.idle_active or self.click_delay_id:
            return

        if not self.hide_icon_indicator:
            self.revealer_icon_indicator.set_reveal_child(True)
            timeout_add_once(350, self.revealer_icon_indicator.set_reveal_child, False)

    def do_close_request(self) -> bool:
        try:
            if self.preview_player:
                self.preview_player.terminate()
                self.preview_player = None
            same_playlist = is_same_playlist(self.mpv.playlist)
            save_pos = settings.get_boolean("save-video-position")
            if same_playlist or save_pos:
                self.mpv.quit_watch_later()
            else:
                self.mpv.quit()
            self.mpv.wait_for_shutdown(timeout=3)
        except mpv.ShutdownError:
            pass

        if self.inhibit_cookie:
            self.app.uninhibit(self.inhibit_cookie)

        return False

    def _splice_playlist(self):
        self.playlist_debounce_id = 0
        self.has_some_doc_path = False
        new_items = []
        for idx, item in enumerate(cast(list, self.mpv.playlist)):
            new_items.append(PlaylistItemObj(item, idx))

            if (
                self.has_some_doc_path
                or f"/run/user/{os.getuid()}/doc/" not in item.get("filename")
                or has_host_permission
            ):
                continue
            self.has_some_doc_path = True

        if isinstance(self.visible_dialog, Playlist):
            self.visible_dialog._set_save_btn_playlist()
            self.visible_dialog._set_item_count()

        self.playlist_ls.splice(0, self.playlist_ls.get_n_items(), new_items)
        self.prev_shuffle = self.shuffle_toggle_btn.props.active
        self.playlist_changed = False

    def _show_toast(self, label, timeout=5, force_dismiss=False):
        try:
            toast = Adw.Toast.new(label)
            toast.set_timeout(timeout)
            if hasattr(self, "toast_overlay"):
                self.toast_overlay.dismiss_all()
                self.toast_overlay.add_toast(toast)
                GLib.timeout_add_seconds(timeout, lambda: (toast.dismiss(), False)[1])
        except Exception as e:
            logger.error(f"Error showing toast: {e}")

    def _setup_observers(self):
        @self.mpv.event_callback("start-file")
        def on_start_file(_event):
            idle_add_once(self.spinner.set_visible, True)
            self.loaded_path = str(self.mpv.path)

        @self.mpv.event_callback("playback-restart")
        def on_playback_restart(_event):
            def update():
                self.hide_player_loading()
                self.spinner.set_visible(False)
                if hasattr(self, "gl_area"):
                    self.gl_area.queue_render()
            idle_add_once(update)

        @self.mpv.event_callback("file-loaded")
        def on_files_loaded(_event):
            def update():
                try:
                    self.hide_player_loading()
                    self.spinner.set_visible(False)
                    if hasattr(self, "gl_area"):
                        self.gl_area.queue_render()
                    if hasattr(self, 'details_box') and self.details_box.get_first_child():
                        page = self.details_box.get_first_child()
                        if hasattr(page, 'reset_trailer_btn_ui'):
                            page.reset_trailer_btn_ui()
                    self.is_local_path = is_local_path(self.mpv.path)
                    self.start_page.set_sensitive(True)
                    self._hide_ui_timeout()
                    
                    if hasattr(self, "_try_add_pending_subtitles"):
                        self._try_add_pending_subtitles()

                    if settings.get_boolean("thumbnail-preview"):
                        self.thumb_preview.props.visible = True
                        self.setup_preview_player()
                    else:
                        self.thumb_preview.props.visible = False
                        if self.preview_player:
                            self.preview_player.terminate()
                            self.preview_player = None

                    self.app_mpris._update_metadata()
                except mpv.ShutdownError:
                    pass

            idle_add_once(update)
            timeout_add_seconds_once(5, setattr, self, "error_count", 0)

        @self.mpv.event_callback("end-file")
        def on_end_file(event):
            try:
                curr_pos = self.mpv.playlist_pos
                info = event.as_dict()
                reason = info["reason"]

                if reason == b"error":
                    error = info.get("file_error", b"").decode("utf-8", errors="ignore")
                    
                    # If we are currently loading a new stream and receive 'no audio or video data played' or empty error,
                    # this is an artifact of displacing/replacing the previous stream. Ignore it.
                    if getattr(self, "is_loading_stream", False) and ("no audio or video data played" in error.lower() or "aborted" in error.lower() or not error):
                        logger.debug(f"Ignoring previous stream displacement: {error}")
                        return

                    idle_add_once(self.spinner.set_visible, False)
                    idle_add_once(self.start_page.set_sensitive, True)

                    # Avoid stopping playback on last file/folder error
                    playlist_count = cast(int, self.mpv.playlist_count)
                    if curr_pos == playlist_count - 1:
                        self.mpv.playlist_pos = 0

                    self.error_count += 1
                    is_yt = self.loaded_path and isinstance(self.loaded_path, str) and ("youtube.com" in self.loaded_path.lower() or "youtu.be" in self.loaded_path.lower() or "googlevideo.com" in self.loaded_path.lower())
                    is_trailer = is_yt or bool(getattr(self, "_current_playing_item", {}).get("is_trailer"))
                    is_web = self.loaded_path and isinstance(self.loaded_path, str) and any(d in self.loaded_path.lower() for d in ["vidfast.pro", "vidfast.vc", "vidsrc.", "embed"])
                    if is_trailer:
                        idle_add_once(self.hide_player_loading)
                        if hasattr(self, 'details_box') and self.details_box.get_first_child():
                            page = self.details_box.get_first_child()
                            if hasattr(page, 'reset_trailer_btn_ui'):
                                idle_add_once(page.reset_trailer_btn_ui)
                        idle_add_once(self._show_toast, _("Failed to play trailer"))
                        idle_add_once(self._close_player)
                    elif is_web:
                        idle_add_once(self._show_toast, _("Opening in web browser..."))
                        idle_add_once(open_uri, self.loaded_path, self)
                        idle_add_once(self._close_player)
                    elif getattr(self, "stream_queue", None) and len(self.stream_queue) > 0:
                        idle_add_once(self._try_next_stream_in_queue)
                    else:
                        idle_add_once(self._show_toast, _("File Error") + f": {error}")
                        if self.error_count == 20:
                            self.mpv.stop()
                            self.shuffle_toggle_btn.set_active(False)
                            self.error_count = 0
                elif reason == b"eof":
                    idle_add_once(self.spinner.set_visible, False)
                    idle_add_once(self.start_page.set_sensitive, True)
                    if not self.mpv.keep_open and self.mpv.idle_active and not self.startup:
                        def _handle_eof():
                            if self._is_playing_trailer():
                                self._close_player()
                            elif not self._try_play_next_episode():
                                self._close_player()
                        idle_add_once(_handle_eof)
                else:
                    idle_add_once(self.spinner.set_visible, False)
                    idle_add_once(self.start_page.set_sensitive, True)
                    if not self.mpv.keep_open and self.mpv.idle_active and not self.startup:
                        idle_add_once(self._close_player)
            except mpv.ShutdownError:
                pass

        @self.mpv.property_observer("path")
        def on_path_change(_name, has_file):
            if has_file:
                idle_add_once(self.play_pause_btn.set_sensitive, has_file)

        @self.mpv.property_observer("playlist-count")
        def on_playlist_count_change(_name, _count):
            self.playlist_changed = True
            if isinstance(self.visible_dialog, Playlist):
                if self.playlist_debounce_id > 0:
                    GLib.source_remove(self.playlist_debounce_id)
                    self.playlist_debounce_id = 0
                self.playlist_debounce_id = timeout_add_once(75, self._splice_playlist)
            idle_add_once(self._update_playlist_nav_sensitivity)

        @self.mpv.property_observer("playlist-pos")
        def on_playlist_pos_changed(_name, pos):
            def update_playing_item():
                try:
                    prev_p = self.playlist_prev_pos
                    prev_obj = cast(PlaylistItemObj, self.playlist_ls.get_item(prev_p))
                    curr_obj = cast(PlaylistItemObj, self.playlist_ls.get_item(pos))
                    prev_obj.playing = False
                    curr_obj.playing = True
                except (AttributeError, OverflowError):
                    pass
                finally:
                    self.playlist_prev_pos = pos

            idle_add_once(update_playing_item)

        @self.mpv.property_observer("loop-playlist")
        def on_loop_playlist_change(_name, value):
            def update():
                self.loop_playlist_btn.set_active(value == "inf")
                self._update_playlist_nav_sensitivity()
                self.app_mpris._update_loop()

            idle_add_once(update)

        @self.mpv.property_observer("loop-file")
        def on_loop_file_change(_name, value):
            def update():
                self.loop_file_btn.set_active(value == "inf")
                self.app_mpris._update_loop()

            idle_add_once(update)

        @self.mpv.property_observer("fullscreen")
        def on_fs_change(_name, value):
            def update():
                icon = (
                    "cine-view-restore-symbolic"
                    if value
                    else "cine-view-fullscreen-symbolic"
                )
                text = _("Exit Fullscreen") if value else _("Fullscreen")
                self.fullscreen_btn.set_tooltip_text(text)
                self.fullscreen_btn.set_icon_name(icon)
                self._sync_fullscreen(value)

            idle_add_once(update)
            self._hide_ui_timeout()

        @self.mpv.property_observer("time-pos")
        def on_time_change(_name, value):
            if value and float(value) > 0.05:
                if getattr(self, "is_loading_stream", False):
                    idle_add_once(self.hide_player_loading)
            idle_add_once(self._update_progress, float(value or 0))

        @self.mpv.property_observer("seeking")
        def on_seeking_change(_name, _is_seeking):
            idle_add_once(self.app_mpris._emit_seeked)

        @self.mpv.property_observer("duration")
        def on_duration_change(_name, value):
            idle_add_once(self._update_duration, float(value or 0))

        @self.mpv.property_observer("mute")
        def on_mute_change(_name, muted):
            def update_mute():
                self.mute_toggle_btn.handler_block(self.mute_handler_id)
                self.mute_toggle_btn.set_active(muted)
                self.mute_toggle_btn.handler_unblock(self.mute_handler_id)
                self._update_volume_icon()
                show_icon = None

                try:
                    show_icon = self.mpv._get_property("user-data/show-icon")
                except AttributeError:
                    pass

                if show_icon == "yes":
                    self.icon_indicator.props.icon_name = (
                        self.volume_menu_btn.props.icon_name
                    )
                    self._show_icon_indicator()
                    self.mpv._set_property("user-data/show-icon", None)

            idle_add_once(update_mute)

        @self.mpv.property_observer("volume")
        def on_volume_change(_name, value):
            def update_icon_and_vol_adj():
                vol = int(value)
                # block the signal to not trigger value-changed
                self.volume_scale.handler_block(self.volume_handler_id)
                self.volume_scale_adj.set_value(vol)
                self.volume_scale.handler_unblock(self.volume_handler_id)

                if vol > 0 and self.mpv.mute:
                    self.mpv.mute = False

                if self.volume_menu_btn.props.active:
                    self.mpv.show_text(_("Volume") + f": {vol}%")

                self._update_volume_icon()
                settings.set_int("volume", vol)
                self.app_mpris._update_volume(vol)

            idle_add_once(update_icon_and_vol_adj)

        track_map = {
            "sid": "select-subtitle",
            "aid": "select-audio",
            "vid": "select-video",
        }

        def on_track_change(name, value):
            def set_track():
                action_name = track_map.get(name) or ""
                val = value if isinstance(value, int) else 0
                if action := self.lookup_action(action_name):
                    action.set_state(  # pyright: ignore[reportAttributeAccessIssue]
                        GLib.Variant("i", val)
                    )

            idle_add_once(set_track)

        for prop in track_map.keys():
            self.mpv.property_observer(prop)(on_track_change)

        @self.mpv.property_observer("track-list")
        def on_track_list_change(_name, track_list):
            idle_add_once(self._update_track_menus, track_list)

        @self.mpv.property_observer("playlist-pos")
        def on_pl_pos_change(_name, _value):
            idle_add_once(self._update_playlist_nav_sensitivity)

        @self.mpv.property_observer("chapter-list")
        def on_chapter_list_change(_name, chapters):
            self.chapters = sorted(chapters, key=lambda c: c.get("time", 0))
            idle_add_once(self._update_chapter_marks_and_menu, self.chapters)

        @self.mpv.property_observer("chapter")
        def on_chapter_change(_name, chapter_idx):
            if chapter_idx is not None and self.chapters_menu_btn.get_active():
                idle_add_once(self._sync_chapter_menu_selected)

        @self.mpv.property_observer("pause")
        def on_pause_change(_name, paused):
            if self.mpv.eof_reached:  # allow to replay at eof, requires keep-open
                self.mpv.seek(0, reference="absolute")

            idle_add_once(self._sync_inhibit)
            idle_add_once(self._update_play_pause_icon, paused)

        @self.mpv.property_observer("idle-active")
        def on_idle_change(_name, is_idle):
            def update_state():
                self.actions["open-sub-menu"].set_enabled(not is_idle)
                self.actions["open-audio-menu"].set_enabled(not is_idle)

                self.title_widget.set_visible(not is_idle)
                self.start_page.set_visible(is_idle and not getattr(self, "is_loading_stream", False))
                self.controls_box.set_visible(not is_idle)
                self.gl_area.set_visible(not is_idle or getattr(self, "is_loading_stream", False))

                if is_idle:
                    self.error_count = 0
                    self.revealer_ui.set_reveal_child(True)
                    self.set_title(_("Cine"))
                    self.hide_icon_indicator = True
                    if isinstance(self.visible_dialog, Playlist):
                        self.visible_dialog.close()

                self._sync_inhibit()

            self.startup = False

            idle_add_once(update_state)

        @self.mpv.property_observer("media-title")
        def on_title_change(_name, title):
            def set():
                try:
                    if title == self.mpv.filename:
                        title_no_ext = os.path.splitext(title)[0]
                        self.set_title(title_no_ext)
                        self.title_widget.set_title(title_no_ext)
                    else:
                        self.set_title(title)
                        self.title_widget.set_title(title)
                        pos = abs(cast(int, self.mpv.playlist_pos))
                        if obj := cast(PlaylistItemObj, self.playlist_ls.get_item(pos)):
                            obj.notify("playing")

                    self.hide_icon_indicator = False
                    self.app_mpris._update_props()
                except mpv.ShutdownError:
                    pass

            if title:
                idle_add_once(set)

        @self.mpv.property_observer("sub-scale")
        def on_sub_scale_change(_name, value):
            if self.visible_dialog is None:
                idle_add_once(settings.set_double, "subtitle-scale", value)

        @self.mpv.property_observer("sub-visibility")
        @self.mpv.property_observer("sid")
        def on_sub_vis_change(name, value):
            def set_icon():
                try:
                    sub_on_icon = "cine-subtitles-symbolic"
                    sub_off_icon = "cine-subtitles-off-symbolic"

                    sub_on = (value == "auto" or value) and self.mpv.sid
                    self.subtitles_menu_btn.props.icon_name = (
                        sub_on_icon if sub_on else sub_off_icon
                    )

                    if name != "sub-visibility":
                        return

                    show_icon = None

                    try:
                        show_icon = self.mpv._get_property("user-data/show-icon")
                    except AttributeError:
                        pass

                    if show_icon == "yes":
                        icon = sub_on_icon if sub_on else sub_off_icon
                        self.icon_indicator.props.icon_name = icon
                        self._show_icon_indicator()
                        self.mpv._set_property("user-data/show-icon", None)
                except mpv.ShutdownError:
                    pass

            idle_add_once(set_icon)

        @self.mpv.property_observer("aid")
        def on_aid_change(_name, value):
            def set_icon():
                audio_on = value == "auto" or value
                self.audio_tracks_menu_btn.props.icon_name = (
                    "cine-audio-symbolic" if audio_on else "cine-audio-off-symbolic"
                )

            idle_add_once(set_icon)

        @self.mpv.property_observer("vid")
        def on_vid_change(_name, value):
            idle_add_once(self.audio_only_icon.set_visible, not bool(value))
            if not value:
                # clear the last frame, which sometimes can still be present
                idle_add_once(self.gl_area.queue_render)

        @self.mpv.property_observer("video-zoom")
        def on_zoom_change(_name, value):
            if round(value, 2) == 0.00:
                self.mpv["video-align-x"] = 0
                self.mpv["video-align-y"] = 0

        @self.mpv.property_observer("vo")
        def on_vo_change(_name, vo_list):
            try:
                if vo_list[0].get("name") != "libmpv":
                    self.mpv["vo"] = "libmpv"
            except mpv.ShutdownError:
                pass

        @self.mpv.event_callback("shutdown")
        def on_quit(_event):
            idle_add_once(self.close)

    def _connect(self, signal_name):
        return lambda func: self.connect(signal_name, func)

    def _load_catalogs(self):
        # Initial stack is library
        self.main_stack.set_visible_child_name("library")
        self.library_stack.set_visible_child_name("content")
        
        from . import api
        
        self.all_catalogs = []
        self.current_media_type = "movie"
        self.current_catalog = None
        self.current_genre = None
        
        self.content_page = 1
        self.is_fetching_content = False
        self.content_seen_ids = set()
        
        # Populate search catalog dropdown with all available catalogs
        self._update_search_catalog_dropdown()
            
        def on_search_catalog_changed(dropdown, pspec):
            selected = dropdown.get_selected()
            if selected != Gtk.INVALID_LIST_POSITION:
                database.set_setting("search_catalog_idx", selected)
            if self.search_entry.get_text().strip():
                self._on_search_changed(self.search_entry)
        self.search_catalog_dropdown.connect("notify::selected", on_search_catalog_changed)
        
        # Load addons ONCE and reuse for all checks below
        all_addons = api.database.get_addons()
        
        def _get_supported_types(addons):
            types_found = set()
            for addon in addons:
                if not addon.get("enabled", True): continue
                for t in addon.get("types", []):
                    if t: types_found.add(str(t).lower())
                for cat in addon.get("catalogs", []):
                    if cat.get("type"):
                        types_found.add(str(cat.get("type")).lower())
            return types_found

        supported_types = _get_supported_types(all_addons)
        self.anime_supported = ("anime" in supported_types)
        self.tv_supported = any(t in supported_types for t in ["tv", "channel", "tvchannel"])
        for btn_name in ["anime_inactive_btn_movies", "anime_inactive_btn_series", "anime_inactive_btn_discover"]:
            if hasattr(self, btn_name):
                getattr(self, btn_name).set_visible(self.anime_supported)
        
        # Build dynamic media types based on Stremio addons
        media_type_labels = ["Movies", "Series"]
        media_type_keys = ["movie", "series"]
        
        known_mappings = [
            (["tv", "channel", "tvchannel"], "tv", "TV Channels"),
            (["anime"], "anime", "Anime"),
            (["music", "radio"], "music", "Radio / Music"),
            (["live"], "live", "Live Streams"),
        ]

        added_keys = set(media_type_keys)
        
        for check_list, key_name, label_name in known_mappings:
            if any(t in supported_types for t in check_list):
                if key_name not in added_keys:
                    media_type_labels.append(label_name)
                    media_type_keys.append(key_name)
                    added_keys.add(key_name)

        for t in sorted(supported_types):
            if t not in added_keys and t not in ["movie", "movies", "series", "tv", "channel", "tvchannel", "anime", "music", "radio", "live"]:
                media_type_labels.append(t.title())
                media_type_keys.append(t)
                added_keys.add(t)

        self.media_type_keys = media_type_keys
        self.media_type_labels = media_type_labels
        self.media_type_dropdown.set_model(Gtk.StringList.new(media_type_labels))
        
        # Cache catalog lists per media type to avoid recomputing on every dropdown change
        _catalog_list_cache = {}

        def on_media_type_changed(dropdown, pspec):
            selected = dropdown.get_selected()
            if selected < len(self.media_type_keys):
                self.current_media_type = self.media_type_keys[selected]

            m_type = self.current_media_type

            # Fast path: serve from session cache if available
            if m_type in _catalog_list_cache:
                self.all_catalogs = _catalog_list_cache[m_type]
                cat_names = [c["display_name"] for c in self.all_catalogs]
                if not cat_names:
                    cat_names = ["No Catalogs Found"]
                self.catalog_dropdown.set_model(Gtk.StringList.new(cat_names))
                on_catalog_changed(self.catalog_dropdown, None)
                # Refresh cache in background for next time
                def _refresh_cache():
                    cats = api.get_available_catalogs(m_type)
                    _catalog_list_cache[m_type] = cats
                threading.Thread(target=_refresh_cache, daemon=True).start()
                return

            # Show loading placeholder immediately, then fill async
            self.catalog_dropdown.set_model(Gtk.StringList.new(["Loading..."]))

            def _fetch_catalogs():
                cats = api.get_available_catalogs(m_type)
                _catalog_list_cache[m_type] = cats
                def _apply():
                    if self.current_media_type != m_type:
                        return False  # User switched again before we finished
                    self.all_catalogs = cats
                    cat_names = [c["display_name"] for c in self.all_catalogs]
                    if not cat_names:
                        cat_names = ["No Catalogs Found"]
                    self.catalog_dropdown.set_model(Gtk.StringList.new(cat_names))
                    on_catalog_changed(self.catalog_dropdown, None)
                    return False
                GLib.idle_add(_apply)

            threading.Thread(target=_fetch_catalogs, daemon=True).start()


        self.media_type_dropdown.connect("notify::selected", on_media_type_changed)
        
        def on_catalog_changed(dropdown, pspec):
            selected = dropdown.get_selected()
            if not self.all_catalogs or selected >= len(self.all_catalogs):
                self.current_catalog = None
                return
                
            self.current_catalog = self.all_catalogs[selected]
            genres = self.current_catalog.get("genres", [])
            
            if genres:
                genre_names = ["All"] + genres
                self.genre_dropdown.set_model(Gtk.StringList.new(genre_names))
                self.genre_dropdown.set_visible(True)
            else:
                self.genre_dropdown.set_visible(False)
                self.current_genre = None
                
            if hasattr(self, "library_stack") and self.library_stack.get_visible_child_name() == "content" and not getattr(self, "_suppress_discover_grid_switch", False):
                self.discover_back_box.set_visible(True)
                disp_title = self.current_catalog.get("display_name", "Catalog") if self.current_catalog else "Catalog"
                self.discover_grid_title.set_text(disp_title)

            self._refresh_content()
            
        self.catalog_dropdown.connect("notify::selected", on_catalog_changed)
        
        def on_genre_changed(dropdown, pspec):
            if not self.genre_dropdown.get_visible():
                return
            selected_item = dropdown.get_selected_item()
            if selected_item:
                genre_str = selected_item.get_string()
                self.current_genre = genre_str if genre_str != "All" else None
                self._refresh_content()
                
        self.genre_dropdown.connect("notify::selected", on_genre_changed)

        def on_discover_filter_toggled(button, pspec):
            is_active = button.get_active()
            self.discover_options_revealer.set_reveal_child(is_active)
        if hasattr(self, "discover_filter_toggle_btn"):
            self.discover_filter_toggle_btn.connect("notify::active", on_discover_filter_toggled)

        def on_back_to_discover_clicked(button):
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self.discover_options_revealer.set_reveal_child(False)
            if hasattr(self, "discover_filter_toggle_btn"):
                self.discover_filter_toggle_btn.set_active(False)
            filter_type = getattr(self, "_current_discover_type", "all")
            filter_addon = getattr(self, "_current_discover_addon", None)
            self._refresh_discover_page(filter_media_type=filter_type, filter_addon_url=filter_addon)
        self.back_to_discover_btn.connect("clicked", on_back_to_discover_clicked)
        
        self._init_default_category_menus()

        # Action: Switch to Discover
        action = Gio.SimpleAction.new("switch-to-discover", None)
        def on_switch_discover(action, parameter):
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            self.discover_request_id = getattr(self, "discover_request_id", 0) + 1
            self.category_btn_stack.set_visible_child_name("discover")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._current_discover_type = "all"
            self._current_discover_addon = None
            self._refresh_discover_page(filter_media_type="all")
        action.connect("activate", on_switch_discover)
        self.add_action(action)

        # Action: Select Discover Filter Type
        action = Gio.SimpleAction.new("select-discover-type", GLib.VariantType.new("s"))
        def on_select_discover_type(action, parameter):
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            self.discover_request_id = getattr(self, "discover_request_id", 0) + 1
            m_type = parameter.get_string()
            self.category_btn_stack.set_visible_child_name("discover")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._current_discover_type = m_type
            self._current_discover_addon = None
            self._refresh_discover_page(filter_media_type=m_type)
        action.connect("activate", on_select_discover_type)
        self.add_action(action)

        # Action: Select Addon Discover (multi-row view of all catalogs for an addon)
        action = Gio.SimpleAction.new("select-addon-discover", GLib.VariantType.new("s"))
        def on_select_addon_discover(action, parameter):
            val = parameter.get_string()
            parts = val.split("|", 2)
            if len(parts) == 3:
                from .movie_widget import cancel_pending_image_downloads
                cancel_pending_image_downloads()
                self.discover_request_id = getattr(self, "discover_request_id", 0) + 1
                m_type, m_url, addon_name = parts
                self._current_discover_type = m_type
                self._current_discover_addon = m_url
                show_movies = settings.get_boolean("show-movies-button")
                show_series = settings.get_boolean("show-series-button")
                show_anime = settings.get_boolean("show-anime-button")
                if m_type == "movie" and show_movies:
                    self.category_btn_stack.set_visible_child_name("movies")
                elif m_type == "series" and show_series:
                    self.category_btn_stack.set_visible_child_name("series")
                elif m_type == "anime" and show_anime:
                    self.category_btn_stack.set_visible_child_name("anime")
                else:
                    self.category_btn_stack.set_visible_child_name("discover")
                
                self.discover_back_box.set_visible(False)
                self.library_stack.set_visible_child_name("discover")
                self._refresh_discover_page(filter_media_type=m_type, filter_addon_url=m_url, custom_title=addon_name)
        action.connect("activate", on_select_addon_discover)
        self.add_action(action)

        # Action: Select Catalog & Genre (full grid)
        action = Gio.SimpleAction.new("select-catalog-genre", GLib.VariantType.new("s"))
        def on_select_catalog_genre(action, parameter):
            val = parameter.get_string()
            parts = val.split("|", 3)
            if len(parts) == 4:
                from .movie_widget import cancel_pending_image_downloads
                cancel_pending_image_downloads()
                m_type, m_url, c_id, genre = parts
                self.current_media_type = m_type
                self.current_catalog = {"manifest_url": m_url, "catalog_id": c_id}
                self.current_genre = None if genre == "All" else genre
                
                show_movies = settings.get_boolean("show-movies-button")
                show_series = settings.get_boolean("show-series-button")
                show_anime = settings.get_boolean("show-anime-button")
                if m_type == "movie" and show_movies:
                    self.category_btn_stack.set_visible_child_name("movies")
                elif m_type == "series" and show_series:
                    self.category_btn_stack.set_visible_child_name("series")
                elif m_type == "anime" and show_anime:
                    self.category_btn_stack.set_visible_child_name("anime")
                else:
                    self.category_btn_stack.set_visible_child_name("discover")
                
                from . import api
                cat_info = None
                for c in api.get_available_catalogs(m_type):
                    if c.get("catalog_id") == c_id and c.get("manifest_url") == m_url:
                        cat_info = c
                        break
                
                title = (cat_info.get("display_name") if cat_info else c_id.title()) or "Catalog"
                if self.current_genre:
                    title += f" — {self.current_genre}"
                
                self.discover_grid_title.set_text(title)
                self.discover_back_box.set_visible(True)
                self.library_stack.set_visible_child_name("content")
                self._refresh_content()
        action.connect("activate", on_select_catalog_genre)
        self.add_action(action)

        # Action: Switch to Movies (multi-row view of all movie catalogs)
        action = Gio.SimpleAction.new("switch-to-movies", None)
        def on_switch_movies(action, parameter):
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            # Don't pre-increment: _refresh_discover_page will increment if a rebuild is needed
            self.category_btn_stack.set_visible_child_name("movies")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._current_discover_type = "movie"
            self._current_discover_addon = None
            self._refresh_discover_page(filter_media_type="movie")
        action.connect("activate", on_switch_movies)
        self.add_action(action)
        
        # Action: Switch to Series (multi-row view of all series catalogs)
        action = Gio.SimpleAction.new("switch-to-series", None)
        def on_switch_series(action, parameter):
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            # Don't pre-increment: _refresh_discover_page will increment if a rebuild is needed
            self.category_btn_stack.set_visible_child_name("series")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._current_discover_type = "series"
            self._current_discover_addon = None
            self._refresh_discover_page(filter_media_type="series")
        action.connect("activate", on_switch_series)
        self.add_action(action)

        # Action: Switch to Anime (multi-row view of all anime catalogs)
        action = Gio.SimpleAction.new("switch-to-anime", None)
        def on_switch_anime(action, parameter):
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            # Don't pre-increment: _refresh_discover_page will increment if a rebuild is needed
            self.category_btn_stack.set_visible_child_name("anime")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._current_discover_type = "anime"
            self._current_discover_addon = None
            self._refresh_discover_page(filter_media_type="anime")
        action.connect("activate", on_switch_anime)
        self.add_action(action)

        if hasattr(self, "content_scrolled"):
            adj = self.content_scrolled.get_vadjustment()
            adj.connect("value-changed", self._on_content_scroll)

        if hasattr(self, "discover_scrolled"):
            discover_adj = self.discover_scrolled.get_vadjustment()
            discover_adj.connect("value-changed", self._on_discover_scroll)
            discover_adj.connect("changed", self._on_discover_scroll)
            
        self._discover_views = {}
        self._discover_catalog_list_cache = {}
        debug_log("CineWindow.__init__ calling _populate_addons")
        self._populate_addons()
        
        # Default Launch Option: Discover View and Discover Mode Active
        self._current_discover_type = "all"
        self._current_discover_addon = None
        self.current_media_type = "movie"
        self.current_catalog = {"catalog_id": "top", "manifest_url": "https://v3-cinemeta.strem.io/manifest.json"}
        self.current_genre = None
        self.category_btn_stack.set_visible_child_name("discover")
        self.library_stack.set_visible_child_name("discover")
        debug_log("CineWindow.__init__ calling initial _refresh_discover_page(filter_media_type='all')")
        self._refresh_discover_page(filter_media_type="all")
        
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_key_ctrl = Gtk.EventControllerKey()
        def on_search_key_pressed(controller, keyval, keycode, state):
            if keyval in [Gdk.KEY_Return, Gdk.KEY_KP_Enter]:
                self._on_search_changed(self.search_entry)
                return True
            if keyval == Gdk.KEY_Escape:
                self._close_search()
                return True
            return False
        search_key_ctrl.connect("key-pressed", on_search_key_pressed)
        self.search_entry.add_controller(search_key_ctrl)

        debug_log("CineWindow.__init__ updating category buttons visibility")
        self.update_category_buttons_visibility()
        debug_log("CineWindow.__init__ ALL DONE")
        settings.connect("changed::show-movies-button", lambda *a: self.update_category_buttons_visibility())
        settings.connect("changed::show-series-button", lambda *a: self.update_category_buttons_visibility())
        settings.connect("changed::show-anime-button", lambda *a: self.update_category_buttons_visibility())

    def update_category_buttons_visibility(self):
        show_movies = settings.get_boolean("show-movies-button")
        show_series = settings.get_boolean("show-series-button")
        show_anime = settings.get_boolean("show-anime-button")

        for btn in [
            getattr(self, "movies_active_btn", None),
            getattr(self, "movies_inactive_btn_discover", None),
            getattr(self, "movies_inactive_btn", None),
            getattr(self, "movies_inactive_btn_anime", None),
        ]:
            if btn:
                btn.set_visible(show_movies)

        for btn in [
            getattr(self, "series_active_btn", None),
            getattr(self, "series_inactive_btn_discover", None),
            getattr(self, "series_inactive_btn", None),
            getattr(self, "series_inactive_btn_anime", None),
        ]:
            if btn:
                btn.set_visible(show_series)

        for btn in [
            getattr(self, "anime_active_btn", None),
            getattr(self, "anime_inactive_btn_discover", None),
            getattr(self, "anime_inactive_btn_movies", None),
            getattr(self, "anime_inactive_btn_series", None),
        ]:
            if btn:
                btn.set_visible(show_anime)

        curr_cat = self.category_btn_stack.get_visible_child_name() if hasattr(self, "category_btn_stack") else "discover"
        if (not show_movies and curr_cat == "movies") or \
           (not show_series and curr_cat == "series") or \
           (not show_anime and curr_cat == "anime"):
            self.category_btn_stack.set_visible_child_name("discover")
            self.discover_back_box.set_visible(False)
            self.library_stack.set_visible_child_name("discover")
            self._refresh_discover_page(filter_media_type="all")

    def _refresh_content(self):
        from .movie_widget import cancel_pending_image_downloads
        cancel_pending_image_downloads()
        self.content_request_id = getattr(self, "content_request_id", 0) + 1
        self.is_fetching_content = False
        self.content_page = 1
        self.content_error_count = 0
        self.content_seen_ids.clear()
        self.has_more_content = True
        
        # Clear flowbox
        while self.content_flowbox.get_first_child() is not None:
            self.content_flowbox.remove(self.content_flowbox.get_first_child())
            
        if self.current_catalog:
            self._fetch_content_page()

    def _fetch_content_page(self):
        if not self.current_catalog or not getattr(self, "has_more_content", True):
            return

        if getattr(self, "is_fetching_content", False):
            return

        self.is_fetching_content = True

        current_req_id = getattr(self, "content_request_id", 0)
        page_to_fetch = getattr(self, "content_page", 1)
        media_type = self.current_media_type
        catalog = dict(self.current_catalog)
        genre = self.current_genre

        def fetch():
            from . import api, database
            items = []
            try:
                cat_url = catalog.get("manifest_url")
                cat_id = catalog.get("catalog_id")
                genre_suffix = f":{genre}" if genre else ""
                cache_key = f"discover:{cat_url}:{cat_id}:{media_type}{genre_suffix}:p{page_to_fetch}"

                # Cache-first: show instantly if we have a recent result
                cached = database.get_cached_catalog(cache_key, max_age_hours=6)
                if cached:
                    def apply_cached(cached_items=cached):
                        if current_req_id != getattr(self, "content_request_id", 0):
                            return False
                        self.is_fetching_content = False
                        if cached_items:
                            self.content_error_count = 0
                            if page_to_fetch == 1:
                                self._populate_flowbox(self.content_flowbox, cached_items, self.content_seen_ids)
                                self._schedule_deferred_menu_build(1000)
                            else:
                                self._append_flowbox(self.content_flowbox, cached_items, self.content_seen_ids)
                            self.content_page = page_to_fetch + 1
                        if getattr(self, "has_more_content", True) and hasattr(self, "content_scrolled"):
                            self._on_content_scroll(self.content_scrolled.get_vadjustment())
                        return False
                    GLib.idle_add(apply_cached)
                    # Silent background refresh — update cache only, don't redraw
                    try:
                        fresh = api.fetch_items(
                            media_type=media_type,
                            catalog_id=cat_id,
                            catalog_url=cat_url,
                            genre=genre,
                            page=page_to_fetch
                        )
                        if fresh:
                            database.save_cached_catalog(cache_key, fresh)
                    except Exception:
                        pass
                    return

                # No cache — fetch from network and save
                items = api.fetch_items(
                    media_type=media_type,
                    catalog_id=cat_id,
                    catalog_url=cat_url,
                    genre=genre,
                    page=page_to_fetch
                )
                if items:
                    database.save_cached_catalog(cache_key, items)
            except Exception as e:
                logger.error(f"Error fetching content: {e}")
                items = None

            def apply_results():
                # Race condition guard: ignore if user selected another tab or catalog
                if current_req_id != getattr(self, "content_request_id", 0):
                    return False

                self.is_fetching_content = False

                if items is None:
                    err_count = getattr(self, "content_error_count", 0) + 1
                    self.content_error_count = err_count
                    if err_count >= 2:
                        self.has_more_content = False
                elif items:
                    self.content_error_count = 0
                    is_first_page = (page_to_fetch == 1)
                    if is_first_page:
                        self._populate_flowbox(self.content_flowbox, items, self.content_seen_ids)
                        self._schedule_deferred_menu_build(1000)
                    else:
                        self._append_flowbox(self.content_flowbox, items, self.content_seen_ids)
                    self.content_page = page_to_fetch + 1
                else:
                    self.content_error_count = 0
                    self.has_more_content = False

                if getattr(self, "has_more_content", True) and hasattr(self, "content_scrolled"):
                    self._on_content_scroll(self.content_scrolled.get_vadjustment())
                return False

            GLib.idle_add(apply_results)

        threading.Thread(target=fetch, daemon=True).start()


    def _open_catalog_grid(self, media_type, catalog, title):
        self._suppress_discover_grid_switch = True
        self.current_media_type = media_type
        self.current_catalog = catalog
        self.current_genre = None
        
        # Sync dropdowns
        if hasattr(self, "media_type_keys") and media_type in self.media_type_keys:
            try:
                self.media_type_dropdown.set_selected(self.media_type_keys.index(media_type))
                from . import api
                self.all_catalogs = api.get_available_catalogs(media_type)
                cat_names = [c["display_name"] for c in self.all_catalogs] or ["No Catalogs Found"]
                self.catalog_dropdown.set_model(Gtk.StringList.new(cat_names))
                for idx, c in enumerate(self.all_catalogs):
                    if c.get("catalog_id") == catalog.get("catalog_id") and c.get("manifest_url") == catalog.get("manifest_url"):
                        self.catalog_dropdown.set_selected(idx)
                        break
            except Exception:
                pass
        self._suppress_discover_grid_switch = False
        
        self.discover_grid_title.set_text(title)
        self.discover_back_box.set_visible(True)
        self.library_stack.set_visible_child_name("content")
        self._refresh_content()

    def _on_continue_watching_clicked(self, item_data):
        if not item_data: return
        stream_url = item_data.get("stream_url")
        magnet = item_data.get("magnet")
        title = item_data.get("title") or item_data.get("name") or "Stream"
        stream_queue = item_data.get("stream_queue")
        position = float(item_data.get("position") or 0.0)

        if not magnet and not stream_url and item_data.get("hash"):
            from . import api
            t_name = item_data.get("stream_title") or title
            magnet = api.build_magnet(item_data.get("hash"), t_name)
        p_id = item_data.get("id") or item_data.get("imdb_id")
        season = item_data.get("season")
        episode = item_data.get("episode")
        from . import database
        working_stream = database.get_working_stream(p_id, season, episode) or item_data.get("selected_torrent")

        if working_stream and isinstance(working_stream, dict):
            item_data["selected_torrent"] = working_stream
            if stream_queue and len(stream_queue) > 0:
                stream_queue = [working_stream] + [s for s in stream_queue if s != working_stream and (s.get("url") or s.get("magnet")) != (working_stream.get("url") or working_stream.get("magnet"))]
                item_data["stream_queue"] = stream_queue
                item_data["stream_queue_index"] = 0
            else:
                stream_queue = [working_stream]
                item_data["stream_queue"] = stream_queue
                item_data["stream_queue_index"] = 0
            if working_stream.get("url"):
                stream_url = working_stream["url"]
                item_data["stream_url"] = stream_url
            if working_stream.get("magnet"):
                magnet = working_stream["magnet"]
                item_data["magnet"] = magnet

        self._current_playing_item = dict(item_data)
        
        position = float(item_data.get("position") or 0.0)
        self._pending_seek_position = position if position > 5.0 else None
        
        curr_page = self.main_stack.get_visible_child_name() or "discover"
        prev_page = "details" if curr_page == "details" else "discover"
        
        if stream_queue and len(stream_queue) > 0:
            q_idx = int(item_data.get("stream_queue_index") or 0)
            self.play_stream_with_failover(
                stream_queue,
                initial_index=q_idx,
                title=title,
                previous_page=prev_page,
                season=item_data.get("season"),
                episode=item_data.get("episode"),
                imdb_id=p_id,
                media_type=item_data.get("type", "movie")
            )
        elif magnet or (stream_url and str(stream_url).startswith(("http://", "https://", "magnet:"))):
            target = stream_url or magnet
            if target and any(d in target.lower() for d in ["vidfast.pro", "vidfast.vc", "vidsrc.", "embed"]):
                self.hide_player_loading()
                self._show_toast(_("Opening in web browser..."))
                open_uri(target, self)
                return
            self.previous_page_before_player = prev_page
            self._play_stream(target, title, start_time=self._pending_seek_position)
        else:
            self._on_movie_clicked(item_data)

    def _on_remove_continue_watching(self, item_data, widget):
        from . import database
        item_id = item_data.get("id") or item_data.get("imdb_id")
        if item_id:
            database.remove_continue_watching(item_id)
        parent = widget.get_parent()
        if parent and isinstance(parent, Gtk.FlowBoxChild):
            flowbox = parent.get_parent()
            if flowbox:
                flowbox.remove(parent)
        elif parent:
            parent.remove(widget)
            if not parent.get_first_child():
                grandparent = parent.get_parent()
                if grandparent and isinstance(grandparent, Gtk.ScrolledWindow):
                    box = grandparent.get_parent()
                    prev = grandparent.get_prev_sibling()
                    if box and prev and isinstance(prev, Gtk.Box):
                        box.remove(prev)
                    if box:
                        box.remove(grandparent)

    def _get_discover_catalog_list(self, filter_media_type=None, filter_addon_url=None):
        cache_key = (filter_media_type or "all", filter_addon_url or "")
        if hasattr(self, "_discover_catalog_list_cache") and cache_key in self._discover_catalog_list_cache:
            cached_res = list(self._discover_catalog_list_cache[cache_key])
            debug_log(f"_get_discover_catalog_list CACHE HIT for {cache_key}", f"{len(cached_res)} rows")
            return cached_res

        debug_log(f"_get_discover_catalog_list START for {cache_key}")
        from . import database, api
        addons = database.get_addons()
        rows = []
        
        # Priority order: Cinemeta first, then Anime, TMDB, Bharat Binge, WATCHO, IPTV, etc.
        def addon_sort_key(addon):
            name = addon.get('name', '').lower()
            if 'cinemeta' in name: return 0
            if 'kitsu' in name or 'animestream' in name or 'onlyanimes' in name or 'anime' in name: return 1
            if 'tmdb' in name or 'movie database' in name: return 2
            if 'iptv' in name or 'channel' in name: return 8
            return 5

        sorted_addons = sorted([a for a in addons if a.get('enabled', True)], key=addon_sort_key)
        type_priority = {'movie': 0, 'series': 1, 'anime': 2, 'tv': 3, 'channel': 4, 'live': 5, 'music': 6}
        seen_row_keys = set()

        for addon in sorted_addons:
            addon_name = addon.get('name', 'Addon')
            m_url = addon.get('manifest_url', '')
            if not m_url or m_url.startswith('builtin:'):
                continue
            if not api.is_addon_online(m_url):
                continue
            if filter_addon_url and m_url != filter_addon_url:
                continue
                
            catalogs = addon.get('catalogs', [])
            if not catalogs:
                continue
                
            grouped = {}
            for cat in catalogs:
                if not api.is_catalog_browsable(cat):
                    continue

                c_id = cat.get('id', '')
                c_name = cat.get('name') or c_id
                c_type = cat.get('type') or 'movie'
                
                # Check media type filter if specified
                if filter_media_type and filter_media_type != "all":
                    if not api.is_type_match(c_type, filter_media_type):
                        if filter_media_type == "anime":
                            if "anime" not in str(c_type).lower() and "anime" not in c_name.lower() and "anime" not in str(c_id).lower() and "anime" not in addon_name.lower():
                                continue
                        else:
                            continue
                
                clean_name = c_name
                if addon_name and clean_name.lower().startswith(addon_name.lower()):
                    clean_name = clean_name[len(addon_name):].lstrip(' -|:·')
                if clean_name.lower() in ['tpbctlg-movies', 'tpbctlg-series']:
                    clean_name = 'Popular'
                
                group_key = clean_name.lower().strip()
                if not group_key:
                    group_key = str(c_id).lower().strip()
                    
                if group_key not in grouped:
                    grouped[group_key] = []
                grouped[group_key].append(cat)
                
            for g_key, cat_group in grouped.items():
                sorted_group = sorted(cat_group, key=lambda c: type_priority.get(c.get('type', 'movie'), 99))
                for cat in sorted_group:
                    c_id = cat.get('id')
                    c_name = cat.get('name') or c_id
                    c_type = cat.get('type', 'movie')
                    
                    row_key = (m_url, c_id, c_type)
                    if row_key in seen_row_keys:
                        continue
                    seen_row_keys.add(row_key)
                    
                    type_display = 'Movies' if c_type == 'movie' else ('Series' if c_type == 'series' else ('Anime' if c_type == 'anime' else ('TV Channels' if c_type in ['tv', 'channel'] else c_type.title())))
                    clean_name = c_name
                    if addon_name and clean_name.lower().startswith(addon_name.lower()):
                        clean_name = clean_name[len(addon_name):].lstrip(' -|:·')
                    if clean_name.lower() in ['tpbctlg-movies', 'tpbctlg-series']:
                        clean_name = 'Popular'
                    if not clean_name:
                        clean_name = c_id.title() if c_id else 'Catalog'
                        
                    if filter_addon_url:
                        row_title = f'{clean_name.title()}'
                    elif 'cinemeta' in addon_name.lower():
                        row_title = f'{clean_name.title()} - {type_display}'
                    elif addon_name.lower() in clean_name.lower():
                        row_title = f'{clean_name} ({type_display})'
                    else:
                        row_title = f'{addon_name} - {clean_name} ({type_display})'
                        
                    rows.append({
                        'addon_name': addon_name,
                        'media_type': c_type,
                        'catalog_id': c_id,
                        'catalog_name': c_name,
                        'manifest_url': m_url,
                        'title': row_title
                    })

        # Deduplicate row titles to avoid GTK "duplicate child name" warnings
        seen_titles = {}
        for row in rows:
            title = row['title']
            if title in seen_titles:
                seen_titles[title] += 1
                # Make the duplicate title unique by appending a counter
                row['title'] = f"{title} ({seen_titles[title]})"
            else:
                seen_titles[title] = 1

        if not hasattr(self, "_discover_catalog_list_cache"):
            self._discover_catalog_list_cache = {}
        self._discover_catalog_list_cache[cache_key] = rows
        debug_log(f"_get_discover_catalog_list DONE for {cache_key}", f"Generated {len(rows)} catalog rows")
        return rows

    def _open_continue_watching_grid(self):
        from . import database
        from .movie_widget import ContinueWatchingWidget
        
        while child := self.content_flowbox.get_first_child():
            self.content_flowbox.remove(child)
            
        cw_items = database.get_continue_watching()
        for item in cw_items:
            card = ContinueWatchingWidget(
                item,
                self._on_movie_clicked,
                on_remove_clicked=self._on_remove_continue_watching,
                on_play_clicked=self._on_continue_watching_clicked
            )
            self.content_flowbox.append(card)
            
        self.discover_grid_title.set_text(_("Continue Watching"))
        self.discover_back_box.set_visible(True)
        self.library_stack.set_visible_child_name("content")

    def _update_continue_watching_section(self):
        from . import database
        from .movie_widget import ContinueWatchingWidget
        
        target_box = None
        if hasattr(self, "_discover_views") and "all|all" in self._discover_views:
            target_box = self._discover_views["all|all"]["box"]
        elif hasattr(self, "_current_discover_type") and self._current_discover_type == "all" and hasattr(self, "discover_box"):
            target_box = self.discover_box
            
        if not target_box:
            debug_log("_update_continue_watching_section SKIPPED (no target_box)")
            return
            
        debug_log("_update_continue_watching_section START")
        cw_items = database.get_continue_watching()
        display_cw_items = cw_items[:15]
        debug_log(f"_update_continue_watching_section DB returned {len(cw_items)} items (displaying {len(display_cw_items)})")
        
        # Scan target_box to identify any existing Continue Watching header and scroll widgets
        existing_header = None
        existing_scroll = None
        extra_headers = []
        extra_scrolls = []
        
        child = target_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            if getattr(child, "_is_cw_header", False) or child.has_css_class("continue-watching-header"):
                if existing_header is None:
                    existing_header = child
                else:
                    extra_headers.append(child)
            elif getattr(child, "_is_cw_scroll", False) or child.has_css_class("continue-watching-scroll"):
                if existing_scroll is None:
                    existing_scroll = child
                else:
                    extra_scrolls.append(child)
            child = next_child
            
        # Clean up any duplicate widgets that may have accumulated
        for h in extra_headers:
            target_box.remove(h)
        for s in extra_scrolls:
            target_box.remove(s)
            
        if not display_cw_items:
            if existing_header and existing_header.get_parent() == target_box:
                target_box.remove(existing_header)
            if existing_scroll and existing_scroll.get_parent() == target_box:
                target_box.remove(existing_scroll)
            debug_log("_update_continue_watching_section DONE (no items to display)")
            return

        new_cw_ids = [str(item.get("id") or item.get("imdb_id")) + ":" + str(item.get("last_watched", 0)) for item in display_cw_items]
        if existing_scroll and getattr(existing_scroll, "_cw_item_ids", None) == new_cw_ids and existing_scroll.get_child():
            debug_log("_update_continue_watching_section SKIPPED (already rendered matching cw_item_ids)")
            return
            
        if not existing_header:
            cw_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            cw_header.add_css_class("discover-section-header")
            cw_header.add_css_class("continue-watching-header")
            cw_header._is_cw_header = True
            
            lbl = Gtk.Label(label=_("Continue Watching"), halign=Gtk.Align.START)
            lbl.add_css_class("discover-section-title")
            cw_header.append(lbl)
            
            see_all_btn = Gtk.Button(label=_("See All"))
            see_all_btn.add_css_class("discover-see-all-btn")
            see_all_btn.add_css_class("flat")
            see_all_btn.set_halign(Gtk.Align.END)
            see_all_btn.set_hexpand(True)
            see_all_btn.connect("clicked", lambda *a: self._open_continue_watching_grid())
            cw_header.append(see_all_btn)
            
            target_box.prepend(cw_header)
            existing_header = cw_header
        else:
            cw_header = existing_header
            cw_header.add_css_class("continue-watching-header")
            cw_header._is_cw_header = True

        if not existing_scroll:
            cw_scroll = Gtk.ScrolledWindow()
            cw_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            cw_scroll.set_hexpand(True)
            cw_scroll.add_css_class("discover-row-scroll")
            cw_scroll.add_css_class("continue-watching-scroll")
            cw_scroll._is_cw_scroll = True
            target_box.insert_child_after(cw_scroll, cw_header)
            existing_scroll = cw_scroll
        else:
            cw_scroll = existing_scroll
            cw_scroll.add_css_class("continue-watching-scroll")
            cw_scroll._is_cw_scroll = True

        cw_scroll._cw_item_ids = new_cw_ids
        cw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cw_row.add_css_class("discover-row-box")
        cw_scroll.set_child(cw_row)
        
        # Populate Continue Watching cards
        for item in display_cw_items:
            card = ContinueWatchingWidget(
                item, 
                self._on_movie_clicked,
                on_remove_clicked=self._on_remove_continue_watching,
                on_play_clicked=self._on_continue_watching_clicked
            )
            cw_row.append(card)

        page = self.details_box.get_first_child() if hasattr(self, "details_box") else None
        if page and hasattr(page, "update_continue_btn"):
            page.update_continue_btn()
        debug_log("_update_continue_watching_section DONE")

    def _refresh_discover_page(self, filter_media_type=None, filter_addon_url=None, custom_title=None, force_refresh=False):
        debug_log(f"_refresh_discover_page START (media_type={filter_media_type}, addon={filter_addon_url}, force={force_refresh})")
        from . import database
        
        # Increment request ID to cancel any pending background loaders from previous tab
        self.discover_request_id = getattr(self, "discover_request_id", 0) + 1
        req_id = self.discover_request_id
        self._discover_is_loading_rows = False
        
        self._current_discover_type = filter_media_type or "all"
        self._current_discover_addon = filter_addon_url
        
        if not hasattr(self, "_discover_views"):
            self._discover_views = {}
            
        view_key = f"{self._current_discover_type}|{self._current_discover_addon or 'all'}"
        
        # 1. Instant cache hit: Switch visible view immediately with zero refetch
        if not force_refresh and view_key in self._discover_views:
            debug_log(f"_refresh_discover_page INSTANT CACHE HIT for view '{view_key}'")
            view_data = self._discover_views[view_key]
            target_box = view_data["box"]
            self._discover_catalog_list = view_data["catalog_list"]
            self._discover_next_row_index = view_data["next_row_index"]
            view_data["request_id"] = req_id
            
            if hasattr(self, "discover_scrolled") and self.discover_scrolled.get_child() != target_box:
                self.discover_scrolled.set_child(target_box)
            self.discover_box = target_box
            
            if self._current_discover_type == "all" and not self._current_discover_addon:
                self._update_continue_watching_section()
                
            if hasattr(self, "discover_scrolled"):
                adj = self.discover_scrolled.get_vadjustment()
                val = adj.get_value()
                page_size = adj.get_page_size()
                upper = adj.get_upper()
                if page_size > 0 and (val + page_size >= upper - 800 or upper <= page_size):
                    self._load_next_discover_batch(self.discover_request_id, batch_size=4)
            debug_log(f"_refresh_discover_page DONE (cache hit)")
            return

        debug_log(f"_refresh_discover_page CACHE MISS for view '{view_key}', constructing new view")
        from .movie_widget import cancel_pending_image_downloads
        cancel_pending_image_downloads()
        
        new_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        new_box.set_margin_top(12)
        new_box.set_margin_bottom(24)
        new_box.set_margin_start(16)
        new_box.set_margin_end(16)
        
        if hasattr(self, "discover_scrolled"):
            self.discover_scrolled.set_child(new_box)
        self.discover_box = new_box
        
        # Continue Watching Section (only if filter is "all" or None)
        if not filter_media_type or filter_media_type == "all":
            self._update_continue_watching_section()

        # Setup Sequential Row Loading List
        self._discover_catalog_list = self._get_discover_catalog_list(filter_media_type=filter_media_type, filter_addon_url=filter_addon_url)
        self._discover_next_row_index = 0
        
        self._discover_views[view_key] = {
            "box": new_box,
            "catalog_list": self._discover_catalog_list,
            "next_row_index": 0,
            "request_id": req_id
        }
        
        # Load initial batch of 6 rows to cleanly fill viewport without flooding (lazy load rest on scroll)
        debug_log(f"_refresh_discover_page dispatching initial batch of 6 rows (req_id={req_id})")
        self._load_next_discover_batch(req_id, batch_size=6)
        debug_log(f"_refresh_discover_page DONE")

    def _load_next_discover_batch(self, req_id, batch_size=4):
        if getattr(self, "_discover_is_loading_rows", False):
            debug_log(f"_load_next_discover_batch SKIPPED: already loading rows (req_id={req_id})")
            return
        if req_id != getattr(self, "discover_request_id", 0):
            debug_log(f"_load_next_discover_batch SKIPPED: outdated req_id {req_id} vs current {getattr(self, 'discover_request_id', 0)}")
            return
        if not hasattr(self, "_discover_catalog_list") or self._discover_next_row_index >= len(self._discover_catalog_list):
            debug_log(f"_load_next_discover_batch SKIPPED: all rows loaded ({self._discover_next_row_index}/{len(self._discover_catalog_list) if hasattr(self, '_discover_catalog_list') else 0})")
            return
            
        self._discover_is_loading_rows = True
        
        target_box = getattr(self, "discover_box", None)
        start_idx = self._discover_next_row_index
        end_idx = min(start_idx + batch_size, len(self._discover_catalog_list))
        self._discover_next_row_index = end_idx
        batch_items = self._discover_catalog_list[start_idx:end_idx]
        
        debug_log(f"_load_next_discover_batch START (req_id={req_id}, rows {start_idx}..{end_idx} of {len(self._discover_catalog_list)})", f"Titles: {[b.get('title') for b in batch_items]}")
        
        view_key = f"{getattr(self, '_current_discover_type', 'all')}|{getattr(self, '_current_discover_addon', None) or 'all'}"
        if hasattr(self, "_discover_views") and view_key in self._discover_views:
            self._discover_views[view_key]["next_row_index"] = end_idx

        def worker():
            from . import api, database
            import concurrent.futures
            debug_log(f"discover worker START (req_id={req_id}, batch {start_idx}..{end_idx})")
            try:
                def _fetch_single_row(row_info):
                    """Fetch a single catalog row's items. Runs in parallel."""
                    if req_id != getattr(self, "discover_request_id", 0):
                        return None
                    m_type = row_info.get("media_type", "movie")
                    c_id = row_info.get("catalog_id")
                    m_url = row_info.get("manifest_url")
                    row_title = row_info.get("title", "Catalog")

                    if m_url and not api.is_addon_online(m_url):
                        debug_log(f"discover row SKIPPED (addon offline for session): '{row_title}'")
                        return None

                    cache_key = f"discover:{m_url}:{c_id}:{m_type}"
                    cached = database.get_cached_catalog(cache_key, max_age_hours=48)
                    if cached is not None:
                        debug_log(f"discover row CACHE HIT: '{row_title}'", f"{len(cached)} items")
                        return (row_info, cached)
                    try:
                        debug_log(f"discover row FETCHING from API: '{row_title}' (type={m_type}, id={c_id})")
                        items = api.fetch_items(
                            media_type=m_type,
                            catalog_id=c_id,
                            catalog_url=m_url,
                            page=1,
                            limit=15
                        )
                        debug_log(f"discover row FETCHED from API: '{row_title}'", f"Got {len(items) if items else 0} items")
                        database.save_cached_catalog(cache_key, items if items else [])
                    except Exception as e:
                        logger.error(f"Error fetching discover row {row_title}: {e}")
                        items = None
                        database.save_cached_catalog(cache_key, [])
                    return (row_info, items)

                # Fetch all rows in this batch in parallel
                results = [None] * len(batch_items)
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(batch_items), 6)) as executor:
                    future_to_idx = {executor.submit(_fetch_single_row, ri): idx for idx, ri in enumerate(batch_items)}
                    try:
                        for future in concurrent.futures.as_completed(future_to_idx, timeout=10):
                            if req_id != getattr(self, "discover_request_id", 0):
                                break
                            idx = future_to_idx[future]
                            try:
                                results[idx] = future.result()
                            except Exception:
                                pass
                    except concurrent.futures.TimeoutError:
                        pass

                debug_log(f"discover worker fetched all {len(results)} rows for batch {start_idx}..{end_idx}, queueing _apply_batch_results on GLib idle")

                # Render batch results on the main thread
                def _apply_batch_results():
                    if req_id != getattr(self, "discover_request_id", 0):
                        self._discover_is_loading_rows = False
                        debug_log(f"_apply_batch_results CANCELLED (req_id {req_id} != {getattr(self, 'discover_request_id', 0)})")
                        return False
                        
                    debug_log(f"_apply_batch_results START on main thread for req_id={req_id}")
                    from .movie_widget import MovieWidget
                    
                    for result in results:
                        if result is None:
                            continue
                        row_info, items = result
                        if not items or len(items) == 0:
                            continue

                        m_type = row_info.get("media_type", "movie")
                        c_id = row_info.get("catalog_id")
                        m_url = row_info.get("manifest_url")
                        row_title = row_info.get("title", "Catalog")
                        c_name = row_info.get("catalog_name", c_id)

                        sec_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                        sec_header.add_css_class("discover-section-header")
                        
                        sec_title = Gtk.Label(label=row_title, halign=Gtk.Align.START)
                        sec_title.add_css_class("discover-section-title")
                        sec_header.append(sec_title)
                        
                        see_all_btn = Gtk.Button(label=_("See All"))
                        see_all_btn.add_css_class("discover-see-all-btn")
                        see_all_btn.add_css_class("flat")
                        see_all_btn.set_halign(Gtk.Align.END)
                        see_all_btn.set_hexpand(True)
                        
                        cat_obj = {
                            "catalog_id": c_id,
                            "catalog_name": c_name,
                            "manifest_url": m_url,
                            "display_name": row_title
                        }
                        see_all_btn.connect("clicked", lambda *a, cat_dict=cat_obj, m_type_val=m_type, title_val=row_title: getattr(self, "_open_catalog_grid")(m_type_val, cat_dict, title_val))
                        sec_header.append(see_all_btn)
                        
                        sec_scroll = Gtk.ScrolledWindow()
                        sec_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
                        sec_scroll.set_hexpand(True)
                        sec_scroll.add_css_class("discover-row-scroll")
                        
                        sec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                        sec_row.add_css_class("discover-row-box")
                        
                        for it in items:
                            if not it.get("type"):
                                 it["type"] = m_type
                            card = MovieWidget(it, self._on_movie_clicked)
                            card.set_hexpand(False)
                            sec_row.append(card)
                            
                        sec_scroll.set_child(sec_row)
                        
                        if target_box:
                            target_box.append(sec_header)
                            target_box.append(sec_scroll)
                        debug_log(f"_apply_batch_results appended row: '{row_title}' ({len(items)} cards)")

                    self._discover_is_loading_rows = False
                    debug_log(f"_apply_batch_results FINISHED for req_id={req_id}")

                    # Check if viewport still has unfilled room on extra-large displays
                    if req_id == getattr(self, "discover_request_id", 0) and self._discover_next_row_index < len(self._discover_catalog_list):
                        if hasattr(self, "discover_scrolled"):
                            adj = self.discover_scrolled.get_vadjustment()
                            val = adj.get_value()
                            page_size = adj.get_page_size()
                            upper = adj.get_upper()
                            debug_log(f"check_fill_viewport: val={val:.1f}, page_size={page_size:.1f}, upper={upper:.1f}")
                            if page_size > 0 and (val + page_size >= upper - 800 or upper <= page_size):
                                debug_log(f"check_fill_viewport TRIGGERING NEXT BATCH (upper <= page_size or near bottom)")
                                self._load_next_discover_batch(req_id, batch_size=4)
                    return False

                GLib.idle_add(_apply_batch_results)
            except Exception as e:
                logger.error(f"Error in discover batch worker: {e}")
                def _reset_flag():
                    if req_id == getattr(self, "discover_request_id", 0):
                        self._discover_is_loading_rows = False
                    return False
                GLib.idle_add(_reset_flag)

        threading.Thread(target=worker, daemon=True).start()

    def _on_discover_scroll(self, adj):
        if hasattr(self, "library_stack") and self.library_stack.get_visible_child_name() != "discover":
            return
        if getattr(self, "_discover_is_loading_rows", False):
            return
        if not hasattr(self, "_discover_catalog_list") or getattr(self, "_discover_next_row_index", 0) >= len(self._discover_catalog_list):
            return
            
        val = adj.get_value()
        page_size = adj.get_page_size()
        upper = adj.get_upper()
        
        # Fetch next batch if user scrolled near the bottom (or viewport is unfilled)
        if page_size > 0 and (val + page_size >= upper - 800 or upper <= page_size):
            debug_log(f"_on_discover_scroll TRIGGERING NEXT BATCH: val={val:.1f}, page_size={page_size:.1f}, upper={upper:.1f}")
            self._load_next_discover_batch(getattr(self, "discover_request_id", 0), batch_size=4)

    def _prewarm_discover_views(self):
        """Pre-fetch Movie, Series, and Anime discover catalog data in the background into SQLite cache."""
        debug_log("_prewarm_discover_views scheduling background prewarm thread")
        def bg_prewarm():
            import time
            import concurrent.futures
            time.sleep(1.5)  # Wait for initial app startup and UI rendering to complete
            debug_log("bg_prewarm thread woke up after 1.5s delay")
            from . import api, database

            all_uncached = []
            for m_type in ["movie", "series", "anime"]:
                try:
                    cat_list = self._get_discover_catalog_list(filter_media_type=m_type)
                    if not cat_list:
                        continue
                    for row_info in cat_list[:4]:
                        mt = row_info.get("media_type", m_type)
                        c_id = row_info.get("catalog_id")
                        m_url = row_info.get("manifest_url")
                        cache_key = f"discover:{m_url}:{c_id}:{mt}"
                        if database.get_cached_catalog(cache_key, max_age_hours=48) is None:
                            all_uncached.append((mt, c_id, m_url, cache_key))
                except Exception as e:
                    logger.debug(f"Prewarm error for {m_type}: {e}")

            if not all_uncached:
                debug_log("bg_prewarm: All initial catalogs already cached, nothing to prewarm")
                return

            debug_log(f"bg_prewarm: Fetching {len(all_uncached)} uncached catalog rows")
            def _fetch_one(args):
                mt, c_id, m_url, cache_key = args
                try:
                    items = api.fetch_items(media_type=mt, catalog_id=c_id, catalog_url=m_url, page=1, limit=15)
                    if items:
                        database.save_cached_catalog(cache_key, items)
                except Exception:
                    pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(all_uncached), 6)) as executor:
                list(executor.map(_fetch_one, all_uncached))
            debug_log("bg_prewarm: Completed prewarming all rows")

        threading.Thread(target=bg_prewarm, daemon=True).start()

    def _clean_cat_name(self, cat, addon_name, media_type):
        raw_name = cat.get("catalog_name") or cat.get("catalog_id") or "Catalog"
        display_name = cat.get("display_name") or ""
        
        name = display_name if display_name else raw_name
        if addon_name and name.lower().startswith(addon_name.lower()):
            name = name[len(addon_name):].lstrip(" -|:·")
        if name.lower() in ['tpbctlg-movies', 'tpbctlg-series']:
            name = 'Popular'
            
        words = []
        if media_type == "movie":
            words = ["Movies", "Movie"]
        elif media_type == "series":
            words = ["Series", "TV Shows", "TV Show", "TV"]
        elif media_type == "anime":
            words = ["Anime"]
            
        for w in words:
            name = re.sub(rf'\b{w}\b\s*[·\-:]*\s*', '', name, flags=re.IGNORECASE)
            name = re.sub(rf'\s*[·\-:]*\s*\b{w}\b', '', name, flags=re.IGNORECASE)
            
        name = name.strip(" -|:·")
        if not name:
            cat_id = cat.get("catalog_id", "")
            if cat_id and cat_id.lower() not in ["top", "movie", "series", "anime"]:
                name = cat_id.title()
            else:
                name = (cat.get("catalog_name") or "Main").title()
        return name

    def _prepare_menu_data(self, media_type):
        from . import api
        catalogs = api.get_available_catalogs(media_type)
        
        addons_map = {}
        for cat in catalogs:
            addon_name = cat.get("addon_name") or "Addon"
            m_url = cat.get("manifest_url") or ""
            if not m_url or not api.is_addon_online(m_url):
                continue
            key = (addon_name, m_url)
            if key not in addons_map:
                addons_map[key] = []
            addons_map[key].append(cat)
        result_addons = []

        for (addon_name, m_url), addon_cats in addons_map.items():
            addon_cat_items = []
            for cat in addon_cats:
                cat_id = cat.get("catalog_id", "")
                clean_name = self._clean_cat_name(cat, addon_name, media_type)
                genres = cat.get("genres") or []
                
                if genres:
                    genre_items = []
                    for g in ["All"] + genres:
                        target_str = f"{media_type}|{m_url}|{cat_id}|{g}"
                        genre_items.append((g, target_str))
                        
                    is_single_default = (len(addon_cats) == 1 and clean_name.lower() in ["top", "main", "all", "default", "popular", addon_name.lower()])
                    
                    addon_cat_items.append({
                        "type": "submenu",
                        "name": clean_name,
                        "cat_id": cat_id,
                        "items": genre_items,
                        "is_single_default": is_single_default
                    })
                else:
                    target_str = f"{media_type}|{m_url}|{cat_id}|All"
                    addon_cat_items.append({
                        "type": "item",
                        "name": clean_name,
                        "target": target_str
                    })
                    
            result_addons.append((addon_name, m_url, addon_cat_items))
            
        return result_addons

    def _build_addon_submenu(self, addon_name, m_url, media_type, addon_cat_items):
        """Build a single addon's Gio.Menu from its prepared data. Runs in background thread."""
        addon_menu = Gio.Menu.new()
        
        # 1. Top item: All catalogs from this addon (e.g. "All Cinemeta Movies")
        m_label = "Movies" if media_type == "movie" else ("Series" if media_type == "series" else ("Anime" if media_type == "anime" else media_type.title()))
        all_addon_item = Gio.MenuItem.new(f"★ All {addon_name} {m_label}", None)
        target_str = f"{media_type}|{m_url}|{addon_name}"
        all_addon_item.set_action_and_target_value("win.select-addon-discover", GLib.Variant("s", target_str))
        addon_menu.append_item(all_addon_item)

        # 2. Catalogs and Genres
        if len(addon_cat_items) == 1 and addon_cat_items[0].get("is_single_default"):
            cat_data = addon_cat_items[0]
            if cat_data["type"] == "submenu":
                for label, target_str in cat_data["items"]:
                    item = Gio.MenuItem.new(label, None)
                    item.set_action_and_target_value("win.select-catalog-genre", GLib.Variant("s", target_str))
                    addon_menu.append_item(item)
            else:
                item = Gio.MenuItem.new(cat_data["name"], None)
                item.set_action_and_target_value("win.select-catalog-genre", GLib.Variant("s", cat_data["target"]))
                addon_menu.append_item(item)
        else:
            for cat_idx, cat_data in enumerate(addon_cat_items):
                if cat_data["type"] == "submenu":
                    cat_menu = Gio.Menu.new()
                    for label, target_str in cat_data["items"]:
                        item = Gio.MenuItem.new(label, None)
                        item.set_action_and_target_value("win.select-catalog-genre", GLib.Variant("s", target_str))
                        cat_menu.append_item(item)
                    sub_item = Gio.MenuItem.new_submenu(cat_data["name"], cat_menu)
                    # Assign unique id attribute so GtkPopoverMenu's internal GtkStack never has duplicate child name warnings
                    cat_h = hashlib.md5(f"{media_type}:{m_url}:{cat_data.get('cat_id', '')}:{cat_idx}:{cat_data['name']}".encode()).hexdigest()[:12]
                    sub_item.set_attribute_value("id", GLib.Variant("s", f"cat_{cat_h}"))
                    addon_menu.append_item(sub_item)
                else:
                    item = Gio.MenuItem.new(cat_data["name"], None)
                    item.set_action_and_target_value("win.select-catalog-genre", GLib.Variant("s", cat_data["target"]))
                    addon_menu.append_item(item)
        return addon_menu

    def _build_discover_menu(self):
        if not hasattr(self, "discover_active_btn"):
            return
        if not hasattr(self, "media_type_keys") or not self.media_type_keys:
            return
        if self.discover_active_btn.get_menu_model() is not None:
            return
            
        menu = Gio.Menu.new()
        all_item = Gio.MenuItem.new(_("All Types"), None)
        all_item.set_action_and_target_value("win.select-discover-type", GLib.Variant("s", "all"))
        menu.append_item(all_item)
        
        for key, label in zip(getattr(self, "media_type_keys", []), getattr(self, "media_type_labels", [])):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.select-discover-type", GLib.Variant("s", key))
            menu.append_item(item)
            
        self.discover_active_btn.set_menu_model(menu)

    def _attach_lazy_menu_model(self, m_type, btn):
        if not hasattr(self, "_attached_menu_types"):
            self._attached_menu_types = set()
        if m_type in self._attached_menu_types:
            return
        built_model = getattr(self, "_built_menu_models", {}).get(m_type)
        if built_model and btn:
            btn.set_menu_model(built_model)
            self._attached_menu_types.add(m_type)

    def _init_default_category_menus(self):
        """Immediately assign valid initial menu models and defer background full menu construction."""
        debug_log("_init_default_category_menus START")
        self._build_discover_menu()
        for m_type, btn_name in [("movie", "movies_active_btn"), ("series", "series_active_btn"), ("anime", "anime_active_btn")]:
            btn = getattr(self, btn_name, None)
            if btn and btn.get_menu_model() is None:
                m_label = "Movies" if m_type == "movie" else ("Series" if m_type == "series" else "Anime")
                menu = Gio.Menu.new()
                top_item = Gio.MenuItem.new(f"★ All {m_label} (All Catalogs)", None)
                action_name = "win.switch-to-movies" if m_type == "movie" else ("win.switch-to-series" if m_type == "series" else "win.switch-to-anime")
                top_item.set_action_and_target_value(action_name, None)
                menu.append_item(top_item)
                btn.set_menu_model(menu)
                btn.connect("notify::active", lambda b, pspec, mt=m_type: self._attach_lazy_menu_model(mt, b) if b.get_active() else None)
        debug_log("_init_default_category_menus applied placeholder models, scheduling deferred full menu build")
        self._schedule_deferred_menu_build(1000)

    def _build_full_menu_model(self, media_type, prepared_data):
        """Construct the complete Gio.Menu model in the background thread."""
        m_label = "Movies" if media_type == "movie" else ("Series" if media_type == "series" else ("Anime" if media_type == "anime" else media_type.title()))
        menu = Gio.Menu.new()
        
        top_item = Gio.MenuItem.new(f"★ All {m_label} (All Catalogs)", None)
        if media_type == "movie":
            top_item.set_action_and_target_value("win.switch-to-movies", None)
        elif media_type == "series":
            top_item.set_action_and_target_value("win.switch-to-series", None)
        elif media_type == "anime":
            top_item.set_action_and_target_value("win.switch-to-anime", None)
        menu.append_item(top_item)

        for addon_idx, (addon_name, m_url, addon_cat_items) in enumerate(prepared_data):
            try:
                addon_menu = self._build_addon_submenu(addon_name, m_url, media_type, addon_cat_items)
                addon_sub_item = Gio.MenuItem.new_submenu(addon_name, addon_menu)
                addon_h = hashlib.md5(f"{media_type}:{m_url}:{addon_name}:{addon_idx}".encode()).hexdigest()[:12]
                addon_sub_item.set_attribute_value("id", GLib.Variant("s", f"addon_{addon_h}"))
                menu.append_item(addon_sub_item)
            except Exception as e:
                logger.error(f"Error appending addon menu '{addon_name}': {e}")

        return menu

    def _ensure_all_menus_built(self):
        self._build_discover_menu()
        if getattr(self, "_menus_building", False) or getattr(self, "_menus_built", False):
            debug_log(f"_ensure_all_menus_built SKIPPED (building={getattr(self, '_menus_building', False)}, built={getattr(self, '_menus_built', False)})")
            return
        self._menus_building = True

        btn_map = {
            "movie": getattr(self, "movies_active_btn", None),
            "series": getattr(self, "series_active_btn", None),
        }
        if getattr(self, "anime_supported", False):
            btn_map["anime"] = getattr(self, "anime_active_btn", None)

        media_types = list(btn_map.keys())
        debug_log(f"_ensure_all_menus_built starting bg_prepare thread for media_types: {media_types}")

        def bg_prepare():
            debug_log("bg_prepare menus thread START")
            built_menus = {}
            for m_type in media_types:
                try:
                    data = self._prepare_menu_data(m_type)
                    built_menus[m_type] = self._build_full_menu_model(m_type, data)
                    debug_log(f"bg_prepare built full Gio.Menu model for {m_type} ({len(data)} online addons)")
                except Exception as e:
                    logger.error(f"Error preparing menu data for {m_type}: {e}")
                    built_menus[m_type] = None

            def apply_all():
                debug_log("apply_all menus storing built models (non-blocking)")
                self._built_menu_models = built_menus
                self._menus_built = True
                self._menus_building = False
                debug_log("apply_all menus on main thread DONE (_menus_built = True)")
                return False

            GLib.idle_add(apply_all)

        threading.Thread(target=bg_prepare, daemon=True).start()

    def _schedule_deferred_menu_build(self, delay_ms=500):
        if getattr(self, "_menu_build_scheduled", False) or getattr(self, "_menus_built", False):
            return
        self._menu_build_scheduled = True
        def run_build():
            self._menu_build_scheduled = False
            self._ensure_all_menus_built()
            return False
        GLib.timeout_add(delay_ms, run_build)

    def _on_content_scroll(self, adj):
        if getattr(self, "is_fetching_content", False) or not getattr(self, "has_more_content", True):
            return
        val = adj.get_value()
        page_size = adj.get_page_size()
        upper = adj.get_upper()
        if page_size > 0 and upper > (page_size + 100) and (val + page_size >= upper - 400):
            if getattr(self, "current_catalog", None):
                self._fetch_content_page()

    def _populate_flowbox(self, flowbox, items, seen_ids=None, on_remove_clicked=None):
        while flowbox.get_first_child() is not None:
            flowbox.remove(flowbox.get_first_child())
        if seen_ids is not None:
            seen_ids.clear()
        self._append_flowbox(flowbox, items, seen_ids, on_remove_clicked=on_remove_clicked)

    def _append_flowbox(self, flowbox, items, seen_ids=None, on_remove_clicked=None):
        for item in items:
            item_id = item.get("id") or item.get("imdb_id")
            if seen_ids is not None and item_id:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            flowbox.append(MovieWidget(item, self._on_movie_clicked, on_remove_clicked=on_remove_clicked))

    def _on_movie_clicked(self, movie_data):
        item_id = movie_data.get("id") or movie_data.get("imdb_id")
        title = movie_data.get("title") or movie_data.get("name")
        print(f"[CARD CLICK Step 1] Clicked movie card: '{title}' (id: {item_id})")
        from . import database
        from .movie_widget import cancel_pending_image_downloads
        cancel_pending_image_downloads()
        self.discover_request_id = getattr(self, "discover_request_id", 0) + 1
        print(f"[CARD CLICK Step 2] Canceled pending image downloads for clean navigation.")
        database.add_history(movie_data)
        
        while child := self.details_box.get_first_child():
            if hasattr(child, "destroy_page"):
                child.destroy_page()
            elif hasattr(child, "_destroyed"):
                child._destroyed = True
            if hasattr(child, "_live_check_abort_event"):
                child._live_check_abort_event.set()
            self.details_box.remove(child)
            
        def on_back():
            print(f"[CARD CLICK Nav] Back button clicked. Returning to previous view.")
            if hasattr(page, "destroy_page"):
                page.destroy_page()
            self._go_back()
            
        self._push_current_nav_state()
        print(f"[CARD CLICK Step 3] Creating MovieDetailsPage widget...")
        page = MovieDetailsPage(movie_data, on_back, window=self)
        self.details_box.append(page)
        self.main_stack.set_visible_child_name("details")
        print(f"[CARD CLICK Step 4] Switched main stack to details view.")

    def show_player_loading(self, text="Fetching metadata...", title=None):
        GLib.idle_add(self._show_player_loading_ui, text, title)

    def _show_player_loading_ui(self, text, title=None):
        self.is_loading_stream = True
        self.main_stack.set_visible_child_name("player")
        if hasattr(self, "start_page"):
            self.start_page.set_visible(False)
        if hasattr(self, "gl_area"):
            self.gl_area.set_visible(True)
        if hasattr(self, "player_loading_box"):
            self.player_loading_box.set_visible(True)
        if hasattr(self, "spinner"):
            self.spinner.set_visible(True)
        if hasattr(self, "player_buffering_label"):
            self.player_buffering_label.set_text(text)
            
        if title and hasattr(self, "title_widget"):
            self.title_widget.set_title(title)
            self.title_widget.set_visible(True)
            self._show_ui()

    def format_stream_stats(self, stats):
        if not isinstance(stats, dict):
            return str(stats)
            
        status_msg = stats.get("status") or "Connecting..."
        
        # If it's a simple text dict without torrent metric fields
        if "progress" not in stats and "downloaded" not in stats and "activePeers" not in stats:
            return status_msg
            
        prog = stats.get("progress", 0)
        dl = stats.get("downloaded", 0)
        tot = stats.get("totalLength", 0)
        spd_dl = stats.get("downloadSpeed", 0) / 1024
        spd_ul = stats.get("uploadSpeed", 0) / 1024
        peers = stats.get("activePeers", 0)
        seeds = stats.get("seeds", 0)

        metric_str = f"{prog * 100.0:.1f}%"
        
        if spd_dl > 0 or spd_ul > 0:
            metric_str += f" - D: {spd_dl:.1f} KiB/s | U: {spd_ul:.1f} KiB/s"
        else:
            metric_str += " - Connecting..."
            
        if tot > 0:
            metric_str += f" ({dl / (1024 * 1024):.1f} MB / {tot / (1024 * 1024 * 1024):.2f} GB)"
        elif dl > 0:
            metric_str += f" ({dl / (1024 * 1024):.1f} MB downloaded)"
            
        metric_str += f" | Peers: {peers} / Seeds: {seeds}"
            
        if status_msg and status_msg.lower() not in ["downloading", "buffering..."]:
            return f"{status_msg}\n{metric_str}"
        return metric_str

    def update_player_loading(self, text):
        if hasattr(self, "player_buffering_label"):
            GLib.idle_add(self.player_buffering_label.set_text, text)

    def hide_player_loading(self):
        GLib.idle_add(self._hide_player_loading_ui)

    def _hide_player_loading_ui(self):
        self.is_loading_stream = False
        if hasattr(self, "player_loading_box"):
            self.player_loading_box.set_visible(False)

    def _clear_stream_failover(self):
        self.stream_request_id += 1
        self.stream_queue = []
        self.stream_queue_index = 0

    def _play_stream(self, url, title=None, headers=None, preserve_queue=False, start_time=None, audio_url=None):
        if not preserve_queue:
            self._clear_stream_failover()
        if url and isinstance(url, str) and any(d in url.lower() for d in ["vidfast.pro", "vidfast.vc", "vidsrc.", "embed"]):
            self.hide_player_loading()
            self._show_toast(_("Opening in web browser..."))
            open_uri(url, self)
            return
        from . import player
        if url and isinstance(url, str) and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            player.stop_player()

        is_youtube_raw_url = url and isinstance(url, str) and ("youtube.com" in url.lower() or "youtu.be" in url.lower()) and ("googlevideo.com" not in url.lower())
        is_youtube_trailer = url and isinstance(url, str) and ("googlevideo.com" in url.lower())
        is_youtube = bool(is_youtube_raw_url or is_youtube_trailer or (title and "(trailer)" in str(title).lower()))
        if is_youtube:
            self._current_playing_item = {"is_trailer": True, "title": title, "stream_url": url}
            if is_youtube_raw_url:
                self.show_player_loading(_("Loading trailer..."), title=title)
        else:
            self.hide_player_loading()
            page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
            details = getattr(page, 'movie_details', {}) or {} if page else {}
            stub = getattr(page, 'movie_stub', {}) or {} if page else {}
            p_id = details.get("imdb_id") or details.get("id") or stub.get("imdb_id") or stub.get("id")
            cover = details.get("medium_cover_image") or stub.get("medium_cover_image") or details.get("poster") or stub.get("poster")
            m_type = getattr(page, 'media_type', 'movie') if page else 'movie'
            s_num = getattr(page, 'selected_season', None) if page else None
            e_num = getattr(page, 'selected_episode', None) if page else None

            if not getattr(self, "_current_playing_item", None):
                import time
                self._current_playing_item = {
                    "id": p_id,
                    "imdb_id": p_id,
                    "title": title or stub.get("title") or stub.get("name") or "Stream",
                    "type": m_type,
                    "medium_cover_image": cover,
                    "season": s_num,
                    "episode": e_num,
                    "last_watched": int(time.time()),
                    "progress": 0.01,
                    "position": 0.0,
                }
            else:
                if p_id and not self._current_playing_item.get("id"):
                    self._current_playing_item["id"] = p_id
                    self._current_playing_item["imdb_id"] = p_id
                if cover and not self._current_playing_item.get("medium_cover_image"):
                    self._current_playing_item["medium_cover_image"] = cover
                if m_type and not self._current_playing_item.get("type"):
                    self._current_playing_item["type"] = m_type
                if s_num is not None and self._current_playing_item.get("season") is None:
                    self._current_playing_item["season"] = s_num
                if e_num is not None and self._current_playing_item.get("episode") is None:
                    self._current_playing_item["episode"] = e_num

            self._current_playing_item["stream_url"] = url
            if title:
                self._current_playing_item["stream_title"] = title

            from . import database
            if self._current_playing_item.get("id") or self._current_playing_item.get("imdb_id"):
                database.save_continue_watching(self._current_playing_item)
                if page and hasattr(page, "update_continue_btn"):
                    page.update_continue_btn()

        self.main_stack.set_visible_child_name("player")
        
        if title:
            self.mpv["force-media-title"] = title
            
        all_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        if headers:
            all_headers.update(headers)
            
        if url and isinstance(url, str):
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                for k_target in ["User-Agent", "Referer", "Origin", "Cookie"]:
                    for p_key, p_val in params.items():
                        if p_key.lower() == k_target.lower() and p_val:
                            all_headers[k_target] = p_val[0]
            except Exception:
                pass
                
        user_agent = None
        referrer = None
        custom_headers = {}

        for k, v in all_headers.items():
            if k.lower() == "user-agent":
                user_agent = v
            elif k.lower() == "referer":
                referrer = v
            else:
                custom_headers[k] = v

        if is_youtube_raw_url:
            # Let MPV's ytdl_hook.lua handle raw youtube URLs
            self.show_player_loading(_("Loading trailer..."), title=title)
            try:
                self.mpv["ytdl"] = True
                self.mpv["ytdl-raw-options"] = "no-playlist="
                self.mpv["user-agent"] = ""
                self.mpv["http-header-fields"] = []
                self.mpv["demuxer-lavf-o"] = ""
            except Exception:
                pass
        else:
            self.hide_player_loading()
            if user_agent:
                self.mpv["user-agent"] = user_agent
            else:
                self.mpv["user-agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

            if referrer:
                self.mpv["referrer"] = referrer
            else:
                self.mpv["referrer"] = ""

            if custom_headers:
                header_fields = [f"{k}: {v}" for k, v in custom_headers.items()]
                self.mpv["http-header-fields"] = header_fields
            else:
                self.mpv["http-header-fields"] = []

            try:
                self.mpv["demuxer-lavf-o"] = "probesize=2000000,analyzeduration=2000000"
                self.mpv["demuxer-readahead-secs"] = 30
                self.mpv["demuxer-max-bytes"] = 150 * 1024 * 1024
                self.mpv["demuxer-max-back-bytes"] = 50 * 1024 * 1024
                self.mpv["cache"] = "yes"
                self.mpv["cache-pause"] = "yes"
                self.mpv["cache-pause-wait"] = 1.0
            except Exception:
                pass

        is_playing_tr = self._is_playing_trailer() or bool(is_youtube_trailer or is_youtube_raw_url or (title and "(trailer)" in str(title).lower()))
        self.next_ep_dismissed = is_playing_tr
        self.next_ep_auto_triggered = is_playing_tr
        if hasattr(self, "next_episode_revealer"):
            self.next_episode_revealer.set_reveal_child(False)

        seek_target = start_time
        if seek_target is None and getattr(self, "_pending_seek_position", None):
            seek_target = self._pending_seek_position
            self._pending_seek_position = None
        elif seek_target is None and getattr(self, "_current_playing_item", None):
            pos = float(self._current_playing_item.get("position") or 0.0)
            prog = float(self._current_playing_item.get("progress") or 0.0)
            if pos > 5.0 and prog < 0.92:
                seek_target = pos

        if seek_target and float(seek_target) > 5.0 and not is_youtube:
            try:
                self.mpv.command("loadfile", url, "replace", f"start={float(seek_target):.2f}")
            except Exception:
                self.mpv.loadfile(url, "replace")
        else:
            self.mpv.loadfile(url, "replace")

        if audio_url:
            try:
                self.mpv.command("audio-add", audio_url, "auto")
            except Exception as e:
                logger.error(f"Error adding audio track to trailer: {e}")

        self.mpv.pause = False
        self.is_inactive = False
        if hasattr(self, "gl_area"):
            self.gl_area.queue_render()

    def fetch_and_add_subtitles(self, imdb_id, media_type, season, episode, stream_subtitles=None, stream_title=None):
        """Fetch subtitles in a background thread and inject them into MPV when ready."""
        self.pending_subtitles = []
        self._subtitle_fetch_id = getattr(self, '_subtitle_fetch_id', 0) + 1
        current_fetch_id = self._subtitle_fetch_id
        
        # Parse season/episode from stream title if not provided
        parse_target = str(stream_title or "")
        if (season is None or episode is None) and parse_target:
            import re
            m = re.search(r'[sS](\d+)[eE](\d+)', parse_target)
            if m:
                if season is None: season = int(m.group(1))
                if episode is None: episode = int(m.group(2))
                media_type = "series"
            else:
                m2 = re.search(r'(\d+)x(\d+)', parse_target)
                if m2:
                    if season is None: season = int(m2.group(1))
                    if episode is None: episode = int(m2.group(2))
                    media_type = "series"

        # Extract clean title for Cinemeta search fallback
        extracted_title = None
        if stream_title and not (imdb_id and str(imdb_id).startswith("tt")):
            import re
            t_clean = re.sub(r'[\(\[\{].*?[\)\]\}]', '', stream_title)
            t_clean = re.sub(r'[sS]\d+[eE]\d+.*', '', t_clean)
            t_clean = re.sub(r'\b\d{3,4}p\b.*', '', t_clean)
            t_clean = t_clean.replace(".", " ").replace("_", " ").strip()
            if t_clean:
                extracted_title = t_clean

        logger.info(f"[SUBS] Starting subtitle fetch: imdb_id={imdb_id}, media_type={media_type}, S{season}E{episode}, title={extracted_title}")

        def fetch():
            if current_fetch_id != self._subtitle_fetch_id:
                return  # Another fetch was started, abort this one
            from . import api
            import time
            try:
                subs = api.get_subtitles(
                    imdb_id,
                    media_type,
                    season,
                    episode,
                    stream_subtitles=stream_subtitles,
                    title=extracted_title
                )
            except Exception as e:
                logger.error(f"[SUBS] Exception in get_subtitles: {e}")
                GLib.idle_add(lambda: self._show_toast(_("Subtitle fetch error")))
                return

            if current_fetch_id != self._subtitle_fetch_id:
                return

            if subs:
                logger.info(f"[SUBS] Found {len(subs)} subtitle(s), downloading...")
                downloaded_count = 0
                for idx, s in enumerate(subs):
                    url = s.get("url")
                    lang = s.get("lang", "en")
                    if url:
                        clean_id = str(imdb_id or extracted_title or 'sub').replace(":", "_").replace("/", "_").replace(" ", "_")
                        filename = f"{clean_id}_{int(time.time())}_{idx}_{lang}.srt"
                        path = api.download_subtitle(url, filename)
                        if path:
                            downloaded_count += 1
                            logger.info(f"[SUBS] Downloaded subtitle #{idx}: {lang} -> {path}")
                            def _queue(p=path, l=lang, is_first=(idx == 0)):
                                if current_fetch_id != self._subtitle_fetch_id:
                                    return False
                                if not hasattr(self, 'pending_subtitles'):
                                    self.pending_subtitles = []
                                self.pending_subtitles.append((p, l, is_first))
                                self._try_add_pending_subtitles()
                                return False
                            GLib.idle_add(_queue)
                        else:
                            logger.warning(f"[SUBS] Failed to download subtitle #{idx}: {url}")
                if downloaded_count == 0:
                    GLib.idle_add(lambda: self._show_toast(_("Subtitle download failed")))
            else:
                logger.info(f"[SUBS] No subtitles found for imdb_id={imdb_id}")
                GLib.idle_add(lambda: self._show_toast(_("No external subtitles found")))
                            
        threading.Thread(target=fetch, daemon=True).start()

    def _try_add_pending_subtitles(self):
        """Try to inject pending subtitles into MPV. Retries via timer if MPV isn't ready."""
        if not hasattr(self, 'pending_subtitles') or not self.pending_subtitles:
            return
            
        try:
            is_idle = self.mpv.core_idle
        except Exception:
            is_idle = True

        if is_idle:
            # MPV isn't playing yet — schedule a retry in 1 second
            logger.debug("[SUBS] MPV core_idle=True, scheduling retry in 1s")
            def _retry():
                self._try_add_pending_subtitles()
                return False
            GLib.timeout_add(1000, _retry)
            return
            
        try:
            added_count = 0
            for path, lang, select in list(self.pending_subtitles):
                mode = "select" if select else "auto"
                self.mpv.command("sub-add", path, mode, f"External ({lang})", lang)
                added_count += 1
                logger.info(f"[SUBS] Injected subtitle into MPV: {lang} ({mode}) -> {path}")
            self.pending_subtitles.clear()
            if added_count > 0:
                msg = _("Subtitles loaded") if added_count == 1 else _(f"Subtitles loaded ({added_count})")
                self._show_toast(msg)
                try:
                    self.mpv.show_text(msg)
                    self.mpv["sub-visibility"] = "yes"
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[SUBS] Failed to add pending subtitles: {e}")

    def play_stream_with_failover(self, queue, initial_index=0, title="", previous_page="details", season=None, episode=None, imdb_id=None, media_type=None, stream_subtitles=None):
        if season is not None:
            self.selected_season = season
        if episode is not None:
            self.selected_episode = episode
        self.stream_request_id += 1
        self.stream_queue = list(queue)
        self.stream_queue_index = max(0, min(int(initial_index or 0), len(self.stream_queue) - 1)) if self.stream_queue else 0
        self.stream_queue_title = title
        self.previous_page_before_player = previous_page

        # Resolve IMDb ID from the details page if not passed directly
        if not imdb_id:
            page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
            if page:
                imdb_id = (
                    getattr(page, 'movie_details', {}).get('imdb_id')
                    or getattr(page, 'movie_details', {}).get('id')
                    or getattr(page, 'movie_stub', {}).get('imdb_id')
                    or getattr(page, 'movie_stub', {}).get('id')
                )
        if not media_type:
            page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
            media_type = getattr(page, 'media_type', 'movie') if page else 'movie'

        # Merge stream-level subtitles from the current queue entry
        current_stream = self.stream_queue[self.stream_queue_index] if self.stream_queue and self.stream_queue_index < len(self.stream_queue) else {}
        entry_subs = current_stream.get("subtitles") if isinstance(current_stream, dict) else None
        all_subs = stream_subtitles or entry_subs

        # Record playing item for Continue Watching
        page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
        details = {}
        stub = {}
        if page:
            p_details = getattr(page, 'movie_details', {}) or {}
            p_stub = getattr(page, 'movie_stub', {}) or {}
            p_id = p_details.get("imdb_id") or p_details.get("id") or p_stub.get("imdb_id") or p_stub.get("id")
            if not imdb_id or not p_id or str(p_id) == str(imdb_id):
                details = p_details
                stub = p_stub

        from . import database
        if not details and imdb_id:
            cached_meta = database.get_cached_metadata(imdb_id, media_type)
            if cached_meta:
                details = cached_meta

        cover = details.get("medium_cover_image") or stub.get("medium_cover_image") or details.get("poster") or stub.get("poster")
        item_title = details.get("title") or stub.get("title") or details.get("name") or stub.get("name") or title
        target_id = imdb_id or details.get("id") or stub.get("id")
        existing_cw = database.get_continue_watching_item(target_id) if target_id else None
        saved_pos = float(existing_cw.get("position") or 0.0) if existing_cw else 0.0
        saved_prog = float(existing_cw.get("progress") or 0.01) if existing_cw else 0.01
        saved_dur = float(existing_cw.get("duration") or 0.0) if existing_cw else 0.0

        import time
        self._last_marked_stream_idx = None

        # Check if the current queue stream is a trailer
        is_stream_trailer = bool(
            current_stream.get("is_trailer")
            or current_stream.get("ytId")
            or (isinstance(current_stream, dict) and current_stream.get("behaviorHints", {}).get("bingeGroup") == "trailer")
            or any("trailer" in str(a).lower() or "streailer" in str(a).lower() for a in current_stream.get("addon_names", []))
            or "(trailer)" in str(title).lower()
            or "trailer" in str(current_stream.get("title", "")).lower()
            or "trailer" in str(current_stream.get("name", "")).lower()
            or "trailer" in str(current_stream.get("stream_title", "")).lower()
            or ("youtube.com" in str(current_stream.get("url", "")).lower() or "youtu.be" in str(current_stream.get("url", "")).lower())
        )

        self._current_playing_item = {
            "id": target_id,
            "imdb_id": imdb_id or details.get("imdb_id") or stub.get("imdb_id") or target_id,
            "title": item_title,
            "type": media_type or (getattr(page, 'media_type', 'movie') if page else 'movie'),
            "medium_cover_image": cover,
            "season": season,
            "episode": episode,
            "stream_queue": list(queue) if queue else [],
            "stream_queue_index": self.stream_queue_index,
            "stream_title": title,
            "last_watched": int(time.time()),
            "progress": saved_prog,
            "position": saved_pos,
            "duration": saved_dur,
            "is_trailer": is_stream_trailer,
        }

        if not is_stream_trailer:
            if self._current_playing_item.get("id") or self._current_playing_item.get("imdb_id") or self._current_playing_item.get("title"):
                database.save_continue_watching(self._current_playing_item)
                if hasattr(self, "_update_continue_watching_section"):
                    curr_stack = self.main_stack.get_visible_child_name() if hasattr(self, "main_stack") else None
                    if curr_stack != "player":
                        self._update_continue_watching_section()

            logger.info(f"[SUBS] play_stream_with_failover: imdb_id={imdb_id}, media_type={media_type}, S{season}E{episode}, title={title}")
            self.fetch_and_add_subtitles(imdb_id, media_type, season, episode, stream_subtitles=all_subs, stream_title=title)

        self._play_current_stream_from_queue(self.stream_request_id)

    def _play_current_stream_from_queue(self, request_id=None):
        if request_id is None:
            request_id = getattr(self, "stream_request_id", 0)
        if request_id != getattr(self, "stream_request_id", 0):
            return
        if not getattr(self, 'stream_queue', None) or len(self.stream_queue) == 0:
            return
        if self.stream_queue_index >= len(self.stream_queue):
            self._handle_all_streams_exhausted()
            return

        torrent = self.stream_queue[self.stream_queue_index]
        magnet = torrent.get("url") or torrent.get("magnet") if isinstance(torrent, dict) else None
        hash_val = torrent.get("hash") or torrent.get("infoHash") if isinstance(torrent, dict) else None
        if not magnet and hash_val:
            from . import api
            t_name = torrent.get("filename") or torrent.get("stream_title") or torrent.get("name") or torrent.get("title") or self.stream_queue_title or ""
            magnet = api.build_magnet(hash_val, t_name)

        title_text = self.stream_queue_title or "Stream"
        stream_name = torrent.get("filename") or torrent.get("name") or torrent.get("stream_title") or "" if isinstance(torrent, dict) else ""
        if stream_name:
            display_title = f"{title_text} ({stream_name})"
        else:
            display_title = title_text

        self.show_player_loading(
            f"Connecting to stream ({self.stream_queue_index + 1}/{len(self.stream_queue)})...",
            display_title
        )

        headers = {}
        behavior_hints = torrent.get("behaviorHints", {}) if isinstance(torrent, dict) else {}
        if behavior_hints and "headers" in behavior_hints:
            headers.update(behavior_hints["headers"])
            
        if magnet:
            import urllib.parse
            parsed_url = urllib.parse.urlparse(magnet)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            for key in ["User-Agent", "Referer", "Origin"]:
                if key in query_params and query_params[key]:
                    headers[key] = query_params[key][0]

        if torrent.get("is_external") or (isinstance(torrent, dict) and torrent.get("externalUrl")):
            ext_url = torrent.get("externalUrl") or magnet
            self.hide_player_loading()
            self._show_toast(_("Opening in web browser..."))
            open_uri(ext_url, self)
            return

        is_yt_stream = bool(
            torrent.get("ytId")
            or (magnet and isinstance(magnet, str) and ("youtube.com" in magnet.lower() or "youtu.be" in magnet.lower()) and "googlevideo.com" not in magnet.lower())
        )

        if is_yt_stream:
            yt_target = magnet or torrent.get("ytId")
            from . import player
            clean_id = player.extract_youtube_id(yt_target) or yt_target
            if clean_id and not str(clean_id).startswith("http"):
                clean_target = f"https://www.youtube.com/watch?v={clean_id}"
            else:
                clean_target = yt_target
            self._play_stream(clean_target, display_title, headers=headers, preserve_queue=True)
            return

        if magnet and (magnet.startswith("http://") or magnet.startswith("https://")):
            self._play_stream(magnet, display_title, headers=headers, preserve_queue=True)
        elif magnet:
            from . import player
            file_index = torrent.get("file_index") if isinstance(torrent, dict) else None
            if file_index is None and isinstance(torrent, dict):
                file_index = torrent.get("fileIdx")
            def progress_callback(stats):
                if request_id != getattr(self, "stream_request_id", 0):
                    return
                url = stats.get("url") if isinstance(stats, dict) else None
                if url:
                    t = display_title
                    if isinstance(stats, dict) and stats.get("filePath"):
                        import os
                        t = os.path.basename(stats.get("filePath"))
                    self._play_stream(url, t, preserve_queue=True)
                elif isinstance(stats, dict):
                    text = self.format_stream_stats(stats)
                    self.update_player_loading(text)
            player.play_magnet(magnet, file_index=file_index, progress_callback=progress_callback, item_id=torrent.get("id") if isinstance(torrent, dict) else None, season=getattr(self, "selected_season", None), episode=getattr(self, "selected_episode", None))
        else:
            self._try_next_stream_in_queue()

    def _try_next_stream_in_queue(self):
        request_id = getattr(self, "stream_request_id", 0)
        if not getattr(self, 'stream_queue', None):
            return
        self.stream_queue_index += 1
        if self.stream_queue_index < len(self.stream_queue):
            msg = f"Stream failed. Trying next stream ({self.stream_queue_index + 1}/{len(self.stream_queue)})..."
            logger.info(msg)
            self._show_toast(msg)
            self._play_current_stream_from_queue(request_id)
        else:
            self._handle_all_streams_exhausted()

    def _handle_all_streams_exhausted(self):
        logger.warning("All candidate streams failed or were exhausted.")
        self._show_toast(_("All available streams failed to play."))
        self.hide_player_loading()
        if hasattr(self, 'mpv'):
            try: self.mpv.stop()
            except Exception: pass
        self.stream_queue = []
        self.stream_queue_index = 0
        prev_page = getattr(self, 'previous_page_before_player', 'details')
        if prev_page and prev_page in ['details', 'library', 'favorites', 'history', 'watched', 'downloads', 'addons']:
            self.main_stack.set_visible_child_name(prev_page)
        elif prev_page in ['discover', 'content', 'search_results']:
            self.main_stack.set_visible_child_name('library')
            if hasattr(self, 'library_stack'):
                self.library_stack.set_visible_child_name(prev_page)
        else:
            self.main_stack.set_visible_child_name('details')

    def _mark_current_stream_as_working(self):
        """Save the verified working stream to DB and reorder the queue so it is played first next time."""
        if not getattr(self, "stream_queue", None) or self.stream_queue_index >= len(self.stream_queue):
            return
        working_stream = self.stream_queue[self.stream_queue_index]
        if not working_stream or not isinstance(working_stream, dict):
            return

        from . import database
        p_id = getattr(self, "_current_playing_item", {}).get("id") or getattr(self, "_current_playing_item", {}).get("imdb_id")
        season = getattr(self, "_current_playing_item", {}).get("season")
        episode = getattr(self, "_current_playing_item", {}).get("episode")

        # Save verified working stream to database
        if p_id:
            database.save_working_stream(p_id, season, episode, working_stream)

        # Update details page if open
        page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
        if page:
            page.remembered_working_stream = working_stream
            page.selected_torrent = working_stream
            if hasattr(page, 'update_continue_btn'):
                page.update_continue_btn()

        # Update _current_playing_item with working stream placed at index 0
        if getattr(self, "_current_playing_item", None):
            reordered_queue = [working_stream] + [t for t in self.stream_queue if t != working_stream and (t.get("url") or t.get("magnet")) != (working_stream.get("url") or working_stream.get("magnet"))]
            self.stream_queue = reordered_queue
            self.stream_queue_index = 0
            self._current_playing_item["stream_queue"] = reordered_queue
            self._current_playing_item["stream_queue_index"] = 0
            self._current_playing_item["selected_torrent"] = working_stream
            st_url = working_stream.get("url") or working_stream.get("magnet") or getattr(self, "loaded_path", None)
            if st_url:
                self._current_playing_item["stream_url"] = st_url
            if working_stream.get("magnet"):
                self._current_playing_item["magnet"] = working_stream["magnet"]
            database.save_continue_watching(self._current_playing_item)

    def _on_no_streams_found(self):
        self.hide_player_loading()
        logger.warning("No direct HTTP streams found for this item.")

    def _get_videos_for_current_page(self, page):
        if not page: return []
        videos = getattr(page, 'videos', None) or (getattr(page, 'movie_details', {}) or {}).get("videos") or (getattr(page, 'movie_stub', {}) or {}).get("videos") or []
        if not videos and hasattr(page, 'movie_stub'):
            from . import database
            cached = database.get_cached_metadata(page.movie_stub.get("id"))
            if cached:
                videos = cached.get("videos", [])
        return videos

    def _is_playing_trailer(self):
        playing_item = getattr(self, "_current_playing_item", {}) or {}
        if playing_item.get("is_trailer"):
            return True
        title = str(playing_item.get("title") or playing_item.get("stream_title") or getattr(self.mpv, "media_title", "") or "").lower()
        if "(trailer)" in title or "trailer" in title:
            return True
        stream_url = str(playing_item.get("stream_url") or getattr(self, "loaded_path", "") or getattr(self.mpv, "path", "") or "").lower()
        if "googlevideo.com" in stream_url or "youtube.com" in stream_url or "youtu.be" in stream_url:
            return True
        return False

    def _has_next_episode(self):
        if self._is_playing_trailer():
            return False
        page = self.details_box.get_first_child()
        if not page:
            return False
        videos = self._get_videos_for_current_page(page)
        if not videos or len(videos) <= 1:
            return False
        current_season = getattr(page, 'selected_season', 1) or 1
        current_episode = getattr(page, 'selected_episode', None)
        if current_episode is None:
            return False
        next_ep = current_episode + 1
        eps_in_season = [v for v in videos if v.get("season", 1) == current_season]
        if any(e.get("episode") == next_ep for e in eps_in_season):
            return True
        target_season = current_season + 1
        eps_in_next_season = [v for v in videos if v.get("season", 1) == target_season]
        return bool(eps_in_next_season)

    @Gtk.Template.Callback()
    def _on_next_ep_play_clicked(self, *args):
        self.next_ep_dismissed = True
        if hasattr(self, "next_episode_revealer"):
            self.next_episode_revealer.set_reveal_child(False)
        self._try_play_next_episode()

    @Gtk.Template.Callback()
    def _on_next_ep_dismiss_clicked(self, *args):
        self.next_ep_dismissed = True
        if hasattr(self, "next_episode_revealer"):
            self.next_episode_revealer.set_reveal_child(False)

    def _try_play_next_episode(self):
        if self._is_playing_trailer():
            return False
        self.next_ep_auto_triggered = False
        page = self.details_box.get_first_child()
        if not page:
            return False
        videos = self._get_videos_for_current_page(page)
        if not videos or len(videos) <= 1:
            return False
            
        current_season = getattr(page, 'selected_season', 1) or 1
        current_episode = getattr(page, 'selected_episode', None)
        if current_episode is None:
            return False
            
        next_ep = current_episode + 1
        eps_in_season = [v for v in videos if v.get("season", 1) == current_season]
        found_next_ep = any(e.get("episode") == next_ep for e in eps_in_season)
        
        target_season = current_season
        target_episode = next_ep
        
        if not found_next_ep:
            target_season = current_season + 1
            eps_in_next_season = [v for v in videos if v.get("season", 1) == target_season]
            if not eps_in_next_season:
                return False
            
            target_episode = min((e.get("episode", 1) for e in eps_in_next_season), default=1)
            
        next_v = next((v for v in videos if v.get("season", 1) == target_season and v.get("episode") == target_episode), None)
        next_title = (next_v.get("title") or next_v.get("name")) if next_v else None
        if getattr(page, 'media_type', '') in ["series", "anime", "tv"]:
            msg = f"Playing next: Season {target_season} Episode {target_episode}"
            load_msg = f"Loading Season {target_season} Episode {target_episode}..."
        else:
            msg = f"Playing next: {next_title or f'Part {target_episode}'}"
            load_msg = f"Loading {next_title or f'Part {target_episode}'}..."
            
        self._show_toast(msg)
        self.main_stack.set_visible_child_name("player")
        self.show_player_loading(load_msg)
        if hasattr(self, 'mpv'):
            try: self.mpv.stop()
            except Exception: pass
        
        page._auto_play_next = True
        
        primary_id = (page.movie_stub.get("alias_ids") or [page.movie_stub.get("id") or page.movie_stub.get("imdb_id")])[0]
        if primary_id:
            from . import database
            database.set_setting(f"last_s_{primary_id}", target_season)
            database.set_setting(f"last_ep_{primary_id}_{target_season}", target_episode)
        
        if target_season != current_season:
            seasons = sorted(list(set([v.get("season", 1) for v in videos])))
            if target_season in seasons:
                idx = seasons.index(target_season)
                page.season_dropdown.set_selected(idx)
        else:
            ep_nums = [e.get('episode') for e in page.current_episodes]
            if target_episode in ep_nums:
                idx = ep_nums.index(target_episode)
                prev_idx = page.episode_dropdown.get_selected()
                page.episode_dropdown.set_selected(idx)
                if prev_idx == idx and hasattr(page, '_on_episode_dropdown_changed'):
                    page._on_episode_dropdown_changed(page.episode_dropdown)
                
        return True

    def _close_player(self, *args):
        self.hide_player_loading()
        if hasattr(self, 'details_box') and self.details_box.get_first_child():
            page = self.details_box.get_first_child()
            if hasattr(page, 'reset_trailer_btn_ui'):
                page.reset_trailer_btn_ui()
        if getattr(self, "_current_playing_item", None) and hasattr(self, "mpv"):
            try:
                curr_pos = float(self.mpv.time_pos or 0.0)
                curr_dur = float(self.mpv.duration or 0.0)
                if curr_pos > 0 and getattr(self, "stream_queue", None):
                    self._mark_current_stream_as_working()
                if curr_dur > 0:
                    prog = min(1.0, max(0.0, curr_pos / curr_dur))
                    self._current_playing_item["position"] = curr_pos
                    self._current_playing_item["duration"] = curr_dur
                    self._current_playing_item["progress"] = prog
                    from . import database
                    if prog >= 0.92:
                        item_id = self._current_playing_item.get("id") or self._current_playing_item.get("imdb_id")
                        database.remove_continue_watching(item_id)
                    else:
                        database.save_continue_watching(self._current_playing_item)
            except Exception:
                pass
        if hasattr(self, 'mpv'):
            try: self.mpv.stop()
            except Exception: pass
        from . import player
        player.stop_player()
        if hasattr(self, "_update_continue_watching_section"):
            self._update_continue_watching_section()
        page = self.details_box.get_first_child() if hasattr(self, 'details_box') else None
        if page:
            if hasattr(page, 'update_continue_btn'):
                page.update_continue_btn()
            self.main_stack.set_visible_child_name("details")
        else:
            self.main_stack.set_visible_child_name("library")

    def _update_search_catalog_dropdown(self):
        from . import api, database
        movie_cats = api.get_available_catalogs("movie")
        series_cats = api.get_available_catalogs("series")
        anime_cats = api.get_available_catalogs("anime") if getattr(self, "anime_supported", False) else []
        
        seen_cat_keys = set()
        self.search_catalogs_list = [{"display_name": "All Catalogs", "manifest_url": None, "catalog_id": None}]
        
        for cat in movie_cats + series_cats + anime_cats:
            key = (cat["manifest_url"], cat["catalog_id"])
            if key not in seen_cat_keys:
                seen_cat_keys.add(key)
                self.search_catalogs_list.append({
                    "display_name": cat["display_name"],
                    "manifest_url": cat["manifest_url"],
                    "catalog_id": cat["catalog_id"]
                })
                
        search_cat_names = [c["display_name"] for c in self.search_catalogs_list]
        if hasattr(self, "search_catalog_dropdown"):
            self.search_catalog_dropdown.set_model(Gtk.StringList.new(search_cat_names))
            saved_search_cat = database.get_setting("search_catalog_idx", 0)
            if 0 <= saved_search_cat < len(search_cat_names):
                self.search_catalog_dropdown.set_selected(saved_search_cat)
            else:
                self.search_catalog_dropdown.set_selected(0)

    def _on_addons_changed(self):
        from .movie_widget import cancel_pending_image_downloads
        from . import api
        cancel_pending_image_downloads()
        # Reset session-blocked addons so they get a fresh attempt after the list changes
        api.reset_addon_session_status()

        # 1. Invalidate all cached discover views & catalog lists
        if hasattr(self, "_discover_views"):
            self._discover_views.clear()
        if hasattr(self, "_discover_catalog_list_cache"):
            self._discover_catalog_list_cache.clear()

        # 2. Reset menu build flags and rebuild category & discover menus
        self._menus_built = False
        self._menus_building = False
        if hasattr(self, "discover_active_btn"):
            self.discover_active_btn.set_menu_model(None)
        self._ensure_all_menus_built()
        self._update_search_catalog_dropdown()
        self._prewarm_discover_views()

        # 3. Update active view state (Discover / Movies / Series / Anime / Content grid)
        from . import api, database
        if hasattr(self, "library_stack") and self.library_stack.get_visible_child_name() == "content":
            curr_cat = getattr(self, "current_catalog", None)
            m_type = getattr(self, "current_media_type", "movie")
            available = api.get_available_catalogs(m_type)
            still_exists = any(
                c.get("catalog_id") == curr_cat.get("catalog_id") and c.get("manifest_url") == curr_cat.get("manifest_url")
                for c in available
            ) if curr_cat else False
            
            if not still_exists:
                self.discover_back_box.set_visible(False)
                self.library_stack.set_visible_child_name("discover")
                if hasattr(self, "category_btn_stack"):
                    self.category_btn_stack.set_visible_child_name("discover")
                self._refresh_discover_page(filter_media_type="all")
            else:
                self._refresh_content()
        elif hasattr(self, "library_stack") and self.library_stack.get_visible_child_name() == "discover":
            cur_type = getattr(self, "_current_discover_type", "all")
            cur_addon = getattr(self, "_current_discover_addon", None)
            if cur_addon:
                addon_exists = any(a.get("manifest_url") == cur_addon and a.get("enabled", True) for a in database.get_addons())
                if not addon_exists:
                    cur_addon = None
                    cur_type = "all"
                    if hasattr(self, "category_btn_stack"):
                        self.category_btn_stack.set_visible_child_name("discover")
            self._refresh_discover_page(filter_media_type=cur_type, filter_addon_url=cur_addon)
        else:
            self._refresh_discover_page(filter_media_type="all")

    def _populate_addons(self):
        debug_log("_populate_addons START")
        while self.addons_listbox.get_first_child() is not None:
            self.addons_listbox.remove(self.addons_listbox.get_first_child())
            
        from . import database
        import urllib.request
        addons = database.get_addons()
        debug_log(f"_populate_addons DB returned {len(addons)} addons")
        for addon in addons:
            name_str = GLib.markup_escape_text(addon.get("name", "Unknown") or "Unknown")
            desc_str = GLib.markup_escape_text(addon.get("description", "") or "")
            row = Adw.ActionRow(title=name_str, subtitle=desc_str)
            
            status_label = Gtk.Label(label="⚪", valign=Gtk.Align.CENTER)
            row.add_prefix(status_label)
            
            manifest_url = addon.get("manifest_url", "")
            
            def check_online(url, lbl, aname=addon.get("name", "Unknown")):
                from . import api
                import urllib.request
                if url.startswith("builtin:"):
                    is_on = True
                else:
                    try:
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            is_on = resp.getcode() == 200
                    except Exception:
                        is_on = False
                api.set_addon_online_status(url, is_on)
                if is_on:
                    # Clear session block so this addon is immediately re-queried
                    api.reset_addon_session_status(url)
                debug_log(f"check_online result for '{aname}' ({url})", "🟢 Online" if is_on else "🔴 Offline")
                GLib.idle_add(lbl.set_label, "🟢" if is_on else "🔴")
                
            if manifest_url:
                threading.Thread(target=check_online, args=(manifest_url, status_label), daemon=True).start()
            else:
                status_label.set_label("🔴")
            
            box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
            
            enable_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            enable_switch.set_active(addon.get("enabled", True))
            enable_switch.connect("notify::active", lambda sw, pspec, a=addon: (database.set_addon_enabled(a.get("id"), sw.get_active()), self._on_addons_changed()))
            box.append(enable_switch)
            
            menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
            menu_btn.add_css_class("flat")
            
            popover = Gtk.Popover()
            popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            popover.set_child(popover_box)
            menu_btn.set_popover(popover)
            
            if addon.get("behaviorHints", {}).get("configurable", False) or "/configure" in manifest_url:
                config_btn = Gtk.Button(label=_("Configure"))
                config_btn.add_css_class("flat")
                def on_config(btn, m_url=manifest_url, pop=popover):
                    pop.popdown()
                    c_url = m_url.replace("/manifest.json", "/configure") if m_url.endswith("/manifest.json") else m_url
                    open_uri(c_url, self)
                config_btn.connect("clicked", on_config)
                popover_box.append(config_btn)
            
            copy_btn = Gtk.Button(label=_("Copy URL"))
            copy_btn.add_css_class("flat")
            def on_copy(btn, m_url=manifest_url, pop=popover):
                pop.popdown()
                self.get_clipboard().set(m_url)
                self._show_toast(_("Copied URL to clipboard"))
            copy_btn.connect("clicked", on_copy)
            popover_box.append(copy_btn)
            
            remove_btn = Gtk.Button(label=_("Delete"))
            remove_btn.add_css_class("flat")
            remove_btn.add_css_class("destructive-action")
            remove_btn.connect("clicked", lambda btn, a=addon, pop=popover: (pop.popdown(), self._remove_addon(a)))
            popover_box.append(remove_btn)
            
            box.append(menu_btn)
            
            row.add_suffix(box)
            self.addons_listbox.append(row)
            
    def _add_addon(self, *args):
        url = self.addon_url_entry.get_text()
        if not url: return
        from . import database
        import urllib.request, json
        
        def fetch_and_add():
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    manifest = json.loads(resp.read().decode('utf-8'))
                    manifest["manifest_url"] = url
                    database.add_addon(manifest)
                    GLib.idle_add(self._populate_addons)
                    GLib.idle_add(self._on_addons_changed)
                    GLib.idle_add(self.addon_url_entry.set_text, "")
            except Exception as e:
                logger.error(f"Failed to add addon: {e}")
                def show_force_add_dialog():
                    dialog = Adw.MessageDialog(
                        heading=_("Addon is Offline"),
                        body=_("Failed to connect to the addon or invalid manifest. Do you want to force add it anyway?"),
                        transient_for=self
                    )
                    dialog.add_response("cancel", _("Cancel"))
                    dialog.add_response("add", _("Force Add"))
                    dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
                    
                    def on_response(dlg, response):
                        if response == "add":
                            addon_id = url.replace("https://", "").replace("http://", "").split("/")[0]
                            manifest = {
                                "id": f"offline.{addon_id}",
                                "name": addon_id,
                                "description": "Offline addon (Forced addition)",
                                "manifest_url": url,
                                "types": ["movie", "series", "anime", "tv", "other"],
                                "catalogs": [],
                                "resources": ["stream", "meta", "catalog"]
                            }
                            database.add_addon(manifest)
                            self._populate_addons()
                            self._on_addons_changed()
                            self.addon_url_entry.set_text("")
                    dialog.connect("response", on_response)
                    dialog.present()
                GLib.idle_add(show_force_add_dialog)
                
        threading.Thread(target=fetch_and_add, daemon=True).start()
        
    def _remove_addon(self, addon):
        from . import database
        addon_id = addon.get("id")
        if addon_id:
            database.remove_addon(addon_id)
            self._populate_addons()
            self._on_addons_changed()

    def _addon_filter_func(self, row):
        search_text = self.addon_url_entry.get_text().lower().strip()
        if not search_text or search_text.startswith("http"):
            return True
        title = row.get_title().lower()
        subtitle = row.get_subtitle().lower() if row.get_subtitle() else ""
        return search_text in title or search_text in subtitle

    def _push_current_nav_state(self):
        if not hasattr(self, "nav_stack"):
            self.nav_stack = []
        current = {
            "main_page": self.main_stack.get_visible_child_name() if hasattr(self, "main_stack") else "library",
            "library_page": self.library_stack.get_visible_child_name() if hasattr(self, "library_stack") else "content",
            "category_btn": self.category_btn_stack.get_visible_child_name() if hasattr(self, "category_btn_stack") else "movies"
        }
        if not self.nav_stack or self.nav_stack[-1] != current:
            self.nav_stack.append(current)
            if len(self.nav_stack) > 50:
                self.nav_stack.pop(0)

    def _go_back(self, *args):
        if hasattr(self, "_update_continue_watching_section"):
            self._update_continue_watching_section()
        if hasattr(self, "nav_stack") and self.nav_stack:
            prev = self.nav_stack.pop()
            main_page = prev.get("main_page", "library")
            self.main_stack.set_visible_child_name(main_page)
            if main_page == "library":
                if hasattr(self, "library_stack") and prev.get("library_page"):
                    self.library_stack.set_visible_child_name(prev["library_page"])
                if hasattr(self, "category_btn_stack") and prev.get("category_btn"):
                    self.category_btn_stack.set_visible_child_name(prev["category_btn"])
            elif main_page in ["favorites", "history", "watched", "downloads"]:
                self._populate_local_db_page(main_page)
        else:
            self.main_stack.set_visible_child_name("library")

    def _open_addons(self, *args):
        self._push_current_nav_state()
        self.main_stack.set_visible_child_name("addons")

    def _back_to_library(self, *args):
        self._go_back()

    def _import_addons(self, *args):
        dialog = Gtk.FileDialog(title=_("Import Addons"))
        def on_open_response(dialog, result):
            try:
                file = dialog.open_finish(result)
                if not file: return
                import json
                with open(file.get_path(), "r") as f:
                    addons = json.load(f)
                from . import database
                for a in addons:
                    database.add_addon(a)
                self._populate_addons()
                self._on_addons_changed()
            except Exception as e:
                logger.error(f"Failed to import addons: {e}")
        dialog.open(self, None, on_open_response)

    def _export_addons(self, *args):
        dialog = Gtk.FileDialog(title=_("Export Addons"), initial_name="addons.json")
        def on_save_response(dialog, result):
            try:
                file = dialog.save_finish(result)
                if not file: return
                from . import database
                import json
                addons = database.get_addons()
                with open(file.get_path(), "w") as f:
                    json.dump(addons, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to export addons: {e}")
        dialog.save(self, None, on_save_response)

    def _open_search(self, *args):
        self.header_stack.set_visible_child_name("search_header")
        self.search_entry.grab_focus()

    def _close_search(self, *args):
        self.search_entry.set_text("")
        self.header_stack.set_visible_child_name("library_header")
        self.library_stack.set_visible_child_name("content")

    def _on_search_changed(self, entry):
        if getattr(self, "search_timeout_id", None):
            GLib.source_remove(self.search_timeout_id)
            self.search_timeout_id = None
            
        self._current_search_id = getattr(self, "_current_search_id", 0) + 1
        current_search_id = self._current_search_id
            
        query = entry.get_text().strip()
        if not query:
            self.library_stack.set_visible_child_name("content")
            return
            
        def trigger_search():
            if self._current_search_id != current_search_id:
                return False
            self.search_timeout_id = None
            if hasattr(self, "library_stack") and self.library_stack.get_visible_child_name() != "search_results":
                self._push_current_nav_state()
            self.library_stack.set_visible_child_name("search_results")
            
            from .movie_widget import cancel_pending_image_downloads
            cancel_pending_image_downloads()
            
            for cat in ["movies", "series", "anime", "tv"]:
                section = getattr(self, f"search_{cat}_section", None)
                flowbox = getattr(self, f"search_{cat}_flowbox", None)
                if section:
                    section.set_visible(False)
                if flowbox:
                    while flowbox.get_first_child() is not None:
                        flowbox.remove(flowbox.get_first_child())
                
            selected_idx = self.search_catalog_dropdown.get_selected()
            target_manifest_url = None
            target_catalog_id = None
            if hasattr(self, "search_catalogs_list") and selected_idx < len(self.search_catalogs_list):
                cat_obj = self.search_catalogs_list[selected_idx]
                target_manifest_url = cat_obj["manifest_url"]
                target_catalog_id = cat_obj["catalog_id"]
                
            def do_search_category(media_type, section_attr, flowbox_attr):
                if self._current_search_id != current_search_id:
                    return
                try:
                    def is_cancelled_fn():
                        return self._current_search_id != current_search_id

                    def on_batch(batch):
                        if self._current_search_id == current_search_id and batch:
                            def update_ui():
                                if self._current_search_id != current_search_id:
                                    return False
                                section = getattr(self, section_attr, None)
                                flowbox = getattr(self, flowbox_attr, None)
                                if flowbox and section:
                                    self._append_flowbox(flowbox, batch, None)
                                    section.set_visible(True)
                                return False
                            GLib.idle_add(update_ui)
                    fetch_items(
                        media_type=media_type,
                        query=query,
                        on_item_found=on_batch,
                        target_manifest_url=target_manifest_url,
                        target_catalog_id=target_catalog_id,
                        is_cancelled=is_cancelled_fn
                    )
                except Exception as e:
                    logger.error(f"Search error ({media_type}): {e}")

            if not hasattr(self, "_search_pool"):
                self._search_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
                
            self._search_pool.submit(do_search_category, "movie", "search_movies_section", "search_movies_flowbox")
            self._search_pool.submit(do_search_category, "series", "search_series_section", "search_series_flowbox")
            self._search_pool.submit(do_search_category, "anime", "search_anime_section", "search_anime_flowbox")
            self._search_pool.submit(do_search_category, "tv", "search_tv_section", "search_tv_flowbox")
            return False

        self.search_timeout_id = GLib.timeout_add(500, trigger_search)

    def switch_to_library_page(self, page_name: str):
        self.library_stack.set_visible_child_name(page_name)

    def _on_library_stack_changed(self, *args):
        pass

    def _open_local_page(self, page_name):
        self._push_current_nav_state()
        self.main_stack.set_visible_child_name(page_name)
        self._populate_local_db_page(page_name)
        
    def _populate_local_db_page(self, page_name):
        from . import database
        items = []
        if page_name == "favorites":
            items = database.get_favorites()
        elif page_name == "history":
            items = database.get_history()
        elif page_name == "watched":
            items = database.get_watched()
        elif page_name == "continue_watching":
            items = database.get_continue_watching()
        elif page_name == "downloads":
            items = database.get_downloads()
            self._populate_downloads_listbox(self.downloads_listbox, items)
            return
        container = getattr(self, f"{page_name}_box", None)
        if not container:
            return
            
        while child := container.get_first_child():
            container.remove(child)
            
        m_type = getattr(self, "local_media_type_filter", "all")
        
        prefix_map = {"favorites": "fav", "history": "hist", "watched": "watch", "continue_watching": "cw"}
        prefix = prefix_map.get(page_name, page_name)
        search_entry = getattr(self, f"{prefix}_search_entry", None)
        query = search_entry.get_text().strip().lower() if search_entry else ""
        
        if query:
            items = [
                i for i in items 
                if query in str(i.get("title") or "").lower() 
                or query in str(i.get("name") or "").lower()
                or query in str(i.get("description") or "").lower()
            ]
            
        movies = [i for i in items if i.get("type", "movie") == "movie"]
        series = [i for i in items if i.get("type", "movie") == "series"]
        anime = [i for i in items if i.get("type", "movie") == "anime"]
        tv = [i for i in items if i.get("type", "movie") in ["tv", "channel", "tvchannel"]]
        
        def _on_remove_local_item(item_data, card_widget):
            item_id = item_data.get("id") or item_data.get("imdb_id")
            if not item_id: return
            if page_name == "favorites":
                database.remove_favorite(item_id)
            elif page_name == "history":
                database.remove_history(item_id)
            elif page_name == "watched":
                database.remove_watched(item_id)
            elif page_name == "continue_watching":
                database.remove_continue_watching(item_id)
                if hasattr(self, "_update_continue_watching_section"):
                    self._update_continue_watching_section()
                
            parent = card_widget.get_parent()
            if parent and isinstance(parent, Gtk.FlowBoxChild):
                flowbox = parent.get_parent()
                if flowbox:
                    flowbox.remove(parent)
            elif parent:
                parent.remove(card_widget)

        def _add_section(title, data):
            if not data: return
            lbl = Gtk.Label(label=title, halign=Gtk.Align.START)
            lbl.add_css_class("title-2")
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(12)
            container.append(lbl)
            
            flowbox = Gtk.FlowBox(
                max_children_per_line=20, min_children_per_line=2, 
                selection_mode=Gtk.SelectionMode.NONE, halign=Gtk.Align.START,
                valign=Gtk.Align.START,
                row_spacing=12, column_spacing=2
            )
            if page_name == "continue_watching":
                from .movie_widget import ContinueWatchingWidget
                for item in data:
                    card = ContinueWatchingWidget(
                        item,
                        self._on_movie_clicked,
                        on_remove_clicked=_on_remove_local_item,
                        on_play_clicked=self._on_continue_watching_clicked
                    )
                    flowbox.append(card)
            else:
                self._populate_flowbox(flowbox, data, on_remove_clicked=_on_remove_local_item)
            container.append(flowbox)

        has_sections = False
        if m_type in ["all", "movie"] and movies:
            _add_section(_("Movies"), movies)
            has_sections = True
        if m_type in ["all", "series"] and series:
            _add_section(_("Series"), series)
            has_sections = True
        if m_type in ["all", "anime"] and anime:
            _add_section(_("Anime"), anime)
            has_sections = True
        if m_type in ["all", "tv"] and tv:
            _add_section(_("TV Channels"), tv)
            has_sections = True

        if not has_sections:
            empty_msg = _("No in-progress items to continue watching.") if page_name == "continue_watching" else (_("No watched items yet.") if page_name == "watched" else (_("No favorites yet.") if page_name == "favorites" else _("No history yet.")))
            empty_lbl = Gtk.Label(label=empty_msg)
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(32)
            container.append(empty_lbl)

    def _populate_downloads_listbox(self, listbox, items):
        while child := listbox.get_first_child():
            listbox.remove(child)
            
        if not items:
            empty = Gtk.Label(label="No downloads yet.")
            empty.add_css_class("dim-label")
            empty.set_valign(Gtk.Align.CENTER)
            empty.set_vexpand(True)
            listbox.append(empty)
            return
            
        from .download_row import DownloadItemRow
        for dl in items:
            listbox.append(DownloadItemRow(dl))
