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
import gi
import mpv
import ctypes
from typing import cast
from gettext import gettext as _
from urllib.parse import urlparse
from time import time
import shlex

from .save_session import (
    save_last_playlist_file,
    restore_last_playlist,
    is_same_playlist,
)

from .utils import (
    logger,
    get_mouse_bindings,
    parse_nonrepeat_bindings,
    is_local_path,
    get_gpu_vendor,
    format_time,
    get_display_param,
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
from .api import fetch_items, fetch_movie_details, get_torrents_streamed
from .movie_widget import MovieWidget

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import Adw, Gio, Gdk, GLib, Gtk, GObject

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
        self.selected_season = None
        self.selected_episode = None
        self.torrents = []
        self._restoring_state = False
        self._destroyed = False
        self._last_played_magnet = None
        self._last_played_file_index = None
        
        self.backdrop_pic = Gtk.Picture()
        self.backdrop_pic.set_can_shrink(True)
        self.backdrop_pic.set_opacity(0.3)
        self.backdrop_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.backdrop_pic.set_hexpand(True)
        self.backdrop_pic.set_vexpand(True)
        self.set_child(self.backdrop_pic)
        
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_overlay(self.main_box)
        
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header_box.set_margin_start(16)
        header_box.set_margin_top(16)
        
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.set_tooltip_text("Back")
        back_btn.add_css_class("circular")
        back_btn.add_css_class("flat")
        def on_back_clicked(btn):
            on_back()
        back_btn.connect("clicked", on_back_clicked)
        header_box.append(back_btn)
        
        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.reload_btn.set_tooltip_text("Reload Details & Streams")
        self.reload_btn.add_css_class("circular")
        self.reload_btn.add_css_class("flat")
        def on_reload(btn):
            from . import database, api
            item_id = self.movie_stub.get("id")
            database.delete_cached_metadata(item_id)
            cache_key = api.get_stream_cache_key(item_id, self.media_type, getattr(self, 'selected_season', None), getattr(self, 'selected_episode', None))
            database.delete_cached_streams(cache_key)
            self._ui_built = False
            self.load_details_async(force_refresh=True)
            self.fetch_torrents_async()
        self.reload_btn.connect("clicked", on_reload)
        header_box.append(self.reload_btn)
        
        self.main_box.append(header_box)
        
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.content_box.set_margin_start(32)
        self.content_box.set_margin_end(32)
        self.content_box.set_margin_top(16)
        self.content_box.set_margin_bottom(16)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.content_box)
        
        self.main_box.append(scrolled)
        
        self.top_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        
        self.poster = Gtk.Picture()
        self.poster.set_can_shrink(True)
        self.poster.set_size_request(240, 360)
        self.poster.set_valign(Gtk.Align.START)
        self.poster.set_halign(Gtk.Align.START)
        self.poster.set_hexpand(False)
        self.poster.set_content_fit(Gtk.ContentFit.COVER)
        self.top_hbox.append(self.poster)
        
        from .movie_widget import load_image_into_picture
        poster_url = self.movie_stub.get("medium_cover_image")
        if poster_url:
            load_image_into_picture(poster_url, self.poster, width=240, height=360)
            
        self.info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.info_vbox.set_hexpand(True)
        
        title_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_hbox.set_valign(Gtk.Align.CENTER)
        
        title_str = self.movie_stub.get("title", "")
        self.title_label = Gtk.Label(label=title_str if title_str else "Loading...")
        self.title_label.add_css_class("title-1")
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_wrap(True)
        title_hbox.append(self.title_label)
        
        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_btn.set_tooltip_text("Copy Title")
        self.copy_btn.add_css_class("flat")
        self.copy_btn.add_css_class("circular")
        self.copy_btn.set_valign(Gtk.Align.CENTER)
        title_hbox.append(self.copy_btn)
        
        self.g_btn = Gtk.Button(label="Google Search")
        self.g_btn.set_tooltip_text("Search Google")
        self.g_btn.add_css_class("flat")
        self.g_btn.set_valign(Gtk.Align.CENTER)
        title_hbox.append(self.g_btn)
        
        self.info_vbox.append(title_hbox)
        
        meta_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta_hbox.set_valign(Gtk.Align.CENTER)
        
        year_str = self.movie_stub.get("year", "")
        self.meta_label = Gtk.Label(label=str(year_str) if year_str else "")
        self.meta_label.set_halign(Gtk.Align.START)
        self.meta_label.set_wrap(True)
        self.meta_label.add_css_class("dim-label")
        meta_hbox.append(self.meta_label)
        
        self.imdb_btn = Gtk.Button(label="IMDb 0.0")
        self.imdb_btn.add_css_class("flat")
        self.imdb_btn.set_valign(Gtk.Align.CENTER)
        meta_hbox.append(self.imdb_btn)
        
        self.info_vbox.append(meta_hbox)
        
        self.desc_label = Gtk.Label(label="")
        self.desc_label.set_wrap(True)
        self.desc_label.set_halign(Gtk.Align.START)
        self.desc_label.set_max_width_chars(80)
        self.desc_label.set_margin_top(8)
        self.desc_label.set_margin_bottom(8)
        self.info_vbox.append(self.desc_label)
        
        self.cast_label = Gtk.Label(label="")
        self.cast_label.set_wrap(True)
        self.cast_label.set_halign(Gtk.Align.START)
        self.cast_label.set_max_width_chars(80)
        self.cast_label.add_css_class("dim-label")
        self.cast_label.set_visible(False)
        self.info_vbox.append(self.cast_label)
        
        self.row1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row1_box.set_margin_top(8)
        
        self.detail_fav_btn = Gtk.Button(label="♡ Add to Favorites")
        self.detail_fav_btn.add_css_class("pill")
        self.row1_box.append(self.detail_fav_btn)
        
        self.detail_seen_btn = Gtk.Button(label="👁 Not Seen")
        self.detail_seen_btn.add_css_class("pill")
        self.row1_box.append(self.detail_seen_btn)
        
        self.trailer_btn = Gtk.Button()
        trailer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        trailer_box.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        trailer_box.append(Gtk.Label(label="Watch Trailer"))
        self.trailer_btn.set_child(trailer_box)
        self.trailer_btn.add_css_class("pill")
        self.row1_box.append(self.trailer_btn)
        
        self.info_vbox.append(self.row1_box)
        
        self.row2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row2_box.set_margin_top(8)
        
        self.season_dropdown = Gtk.DropDown.new_from_strings([])
        self.season_dropdown.set_valign(Gtk.Align.CENTER)
        self.row2_box.append(self.season_dropdown)
        
        self.episode_dropdown = Gtk.DropDown.new_from_strings([])
        self.episode_dropdown.set_valign(Gtk.Align.CENTER)
        self.row2_box.append(self.episode_dropdown)
        
        self.row2_box.set_visible(False)
        self.info_vbox.append(self.row2_box)
        
        self.quality_button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.quality_button_box.set_valign(Gtk.Align.CENTER)
        self.quality_button_box.set_margin_top(12)
        self.quality_button_box.set_visible(False)
        self.info_vbox.append(self.quality_button_box)
        
        self.row3_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row3_box.set_margin_top(8)
        
        self.file_dropdown = Gtk.DropDown.new_from_strings([])
        self.file_dropdown.set_valign(Gtk.Align.CENTER)
        
        def on_dropdown_changed(dropdown, pspec):
            idx = dropdown.get_selected()
            if hasattr(self, 'current_t_list') and idx != Gtk.INVALID_LIST_POSITION and idx < len(self.current_t_list):
                self.selected_torrent = self.current_t_list[idx]
                
        self.file_dropdown.connect("notify::selected", on_dropdown_changed)
        self.row3_box.append(self.file_dropdown)
        self.row3_box.set_visible(False)
        self.info_vbox.append(self.row3_box)
        
        self.row4_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row4_box.set_margin_top(12)
        
        self.watch_btn = Gtk.Button(label="WATCH IT NOW")
        self.watch_btn.add_css_class("suggested-action")
        self.watch_btn.add_css_class("pill")
        self.watch_btn.set_size_request(160, 42)
        self.watch_btn.connect("clicked", self.on_watch_clicked)
        self.row4_box.append(self.watch_btn)
        
        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.add_css_class("pill")
        self.stop_btn.set_valign(Gtk.Align.CENTER)
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        self.stop_btn.set_visible(False)
        self.row4_box.append(self.stop_btn)
        
        self.row4_box.set_visible(False)
        self.info_vbox.append(self.row4_box)
        
        self.progress_label = Gtk.Label(label="")
        self.progress_label.set_halign(Gtk.Align.START)
        self.info_vbox.append(self.progress_label)
        
        self.top_hbox.append(self.info_vbox)
        self.content_box.append(self.top_hbox)
        
        from . import database
        cached_details = database.get_cached_metadata(self.movie_stub.get("id"))
        if cached_details:
            self.build_ui(cached_details)
            self.load_details_async(force_refresh=False)
        else:
            self.load_details_async(force_refresh=True)

    def load_details_async(self, force_refresh=False):
        def fetch():
            from . import api
            details = api.fetch_movie_details(self.movie_stub.get("id"), self.media_type, title=self.movie_stub.get("title"), use_cache=not force_refresh)
            if details:
                GLib.idle_add(self.build_ui, details)
        threading.Thread(target=fetch, daemon=True).start()

    def toggle_favorite(self, details):
        from . import database
        item_id = details.get("id")
        if database.is_favorite(item_id):
            database.remove_favorite(item_id)
            self.detail_fav_btn.set_label("♡ Add to Favorites")
        else:
            database.add_favorite({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_fav_btn.set_label("♥ Remove from Favorites")

    def toggle_watched(self, details):
        from . import database
        item_id = details.get("id")
        if database.is_watched(item_id):
            database.remove_watched(item_id)
            self.detail_seen_btn.set_label("👁 Not Seen")
        else:
            database.add_watched({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_seen_btn.set_label("👁 Seen")

    def build_ui(self, details):
        if not details: return
        from . import database
        from .movie_widget import load_image_into_picture

        if details.get("background"):
            load_image_into_picture(details.get("background"), self.backdrop_pic)
            
        self.title_label.set_text(details.get("title", ""))
        
        def on_copy_clicked(btn):
            try:
                clipboard = Gdk.Display.get_default().get_clipboard()
                clipboard.set(details.get("title", ""))
            except Exception as e:
                print(f"Failed to copy to clipboard: {e}")
        self.copy_btn.connect("clicked", on_copy_clicked)
        
        def on_g_clicked(btn):
            import urllib.parse, subprocess
            q = urllib.parse.quote(details.get("title", ""))
            subprocess.Popen(["xdg-open", f"https://www.google.com/search?q={q}"])
        self.g_btn.connect("clicked", on_g_clicked)
        
        meta_str = f"{details.get('year', '')} • {details.get('runtime', '')} • {details.get('genre', '')}"
        self.meta_label.set_text(meta_str)
        
        imdb_id = details.get("imdb_id") or details.get("id")
        imdb_rating = details.get("imdbRating", "")
        if imdb_id:
            self.imdb_btn.set_label(f"IMDb {imdb_rating}")
            def on_imdb_clicked(btn):
                import subprocess
                subprocess.Popen(["xdg-open", f"https://www.imdb.com/title/{imdb_id}/"])
            self.imdb_btn.connect("clicked", on_imdb_clicked)
        else:
            self.imdb_btn.set_visible(False)
            
        self.desc_label.set_text(details.get("description", ""))
        
        cast_str = ", ".join(details.get("cast", []))
        if cast_str:
            self.cast_label.set_text(f"Cast: {cast_str}")
            self.cast_label.set_visible(True)
            
        item_id = details.get("id")
        self.detail_fav_btn.set_label("♥ Remove from Favorites" if database.is_favorite(item_id) else "♡ Add to Favorites")
        self.detail_fav_btn.connect("clicked", lambda x: self.toggle_favorite(details))
        
        self.detail_seen_btn.set_label("👁 Seen" if database.is_watched(item_id) else "👁 Not Seen")
        self.detail_seen_btn.connect("clicked", lambda x: self.toggle_watched(details))
        
        self.trailer_btn.connect("clicked", lambda x: self.on_trailer_clicked(details.get("trailer")))
        if not details.get("trailer"): self.trailer_btn.set_sensitive(False)
        
        if self.media_type in ["series", "anime"] and details.get("videos"):
            self.row2_box.set_visible(True)
            videos = details.get("videos")
            seasons = sorted(list(set([v.get("season", 1) for v in videos])))
            self.season_dropdown.set_model(Gtk.StringList.new([f"Season {s}" for s in seasons]))
            
            def on_season_changed(dropdown, *args):
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                s = seasons[idx]
                self.selected_season = s
                eps = [v for v in videos if v.get("season") == s]
                unique_eps = []
                seen_eps = set()
                for e in eps:
                    ep_num = e.get("episode", 0)
                    if ep_num not in seen_eps:
                        seen_eps.add(ep_num)
                        unique_eps.append(e)
                unique_eps.sort(key=lambda x: x.get("episode", 0))
                self.current_episodes = unique_eps
                ep_strings = [f"Ep {e.get('episode')}: {e.get('title') or e.get('name', '')}" for e in unique_eps]
                self.episode_dropdown.set_model(Gtk.StringList.new(ep_strings))
                self.episode_dropdown.set_selected(0)
                
            self.season_dropdown.connect("notify::selected", on_season_changed)
            
            def on_episode_changed(dropdown, *args):
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                ep = self.current_episodes[idx].get("episode")
                self.selected_episode = ep
                self.fetch_torrents_async()
                
            self.episode_dropdown.connect("notify::selected", on_episode_changed)
            if seasons: on_season_changed(self.season_dropdown)
        else:
            self.fetch_torrents_async()

    def fetch_torrents_async(self):
        if hasattr(self, 'progress_label') and self.progress_label:
            self.progress_label.set_text("Loading streams...")
        
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)

        item_id = self.movie_stub.get("id")
        sel_season = getattr(self, 'selected_season', None)
        sel_episode = getattr(self, 'selected_episode', None)
        
        def on_stream_batch(torrents, is_cached=False, is_complete=False):
            if hasattr(self, 'progress_label') and self.progress_label:
                if is_complete:
                    if not torrents: self.progress_label.set_text("No streams available.")
                    else: self.progress_label.set_text("")
                elif is_cached:
                    self.progress_label.set_text("Loaded cached streams...")

            if torrents:
                self.torrents = torrents
                self.update_quality_dropdown()

        def fetch():
            from . import api
            api.get_torrents_streamed(
                item_id,
                self.media_type,
                sel_season,
                sel_episode,
                callback=lambda t, is_cached=False, is_complete=False: GLib.idle_add(on_stream_batch, t, is_cached, is_complete)
            )
        threading.Thread(target=fetch, daemon=True).start()

    def update_quality_dropdown(self):
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)
            
        if not self.torrents:
            self.quality_button_box.set_visible(False)
            self.row3_box.set_visible(False)
            self.row4_box.set_visible(False)
            return
            
        self.quality_button_box.set_visible(True)
        self.row3_box.set_visible(True)
        self.row4_box.set_visible(True)
        self.watch_btn.set_sensitive(True)
        
        quality_groups = {"4K": [], "1080p": [], "720p": [], "More": [], "Direct": []}
        for t in self.torrents:
            if t.get('is_http'):
                quality_groups["Direct"].append(t)
                continue
            q = t.get('quality', 'Unknown').upper()
            if "4K" in q or "2160" in q: quality_groups["4K"].append(t)
            elif "1080" in q: quality_groups["1080p"].append(t)
            elif "720" in q: quality_groups["720p"].append(t)
            else: quality_groups["More"].append(t)
            
        self.quality_buttons = []
        
        def on_quality_btn_clicked(btn, t_list):
            for b in self.quality_buttons: b.remove_css_class('suggested-action')
            btn.add_css_class('suggested-action')
            self.current_t_list = t_list
            self.selected_torrent = t_list[0]
            
            strings = []
            for t in t_list:
                title = t.get('stream_title', '').strip() or t.get('size', 'Unknown Size')
                seed_str = f" ({t.get('seeders', 0)} seeds)" if not t.get('is_http') else ""
                strings.append(f"{title}{seed_str}")
            self.file_dropdown.set_model(Gtk.StringList.new(strings))
            self.file_dropdown.set_selected(0)
            
        default_btn = None
        default_t_list = None
        for q_label in ["4K", "1080p", "720p", "More", "Direct"]:
            t_list = quality_groups[q_label]
            if t_list:
                btn = Gtk.Button(label=q_label)
                btn.add_css_class('pill')
                btn.connect("clicked", lambda b, tl=t_list: on_quality_btn_clicked(b, tl))
                self.quality_buttons.append(btn)
                self.quality_button_box.append(btn)
                if not default_btn:
                    default_btn = btn
                    default_t_list = t_list
                    
        if default_btn:
            on_quality_btn_clicked(default_btn, default_t_list)

    def on_trailer_clicked(self, trailer_url):
        if not trailer_url: return
        from . import player
        def progress_callback(stats):
            url = stats.get("url")
            if url and self.window:
                self.window._play_stream(url)
            elif stats.get("status"):
                if hasattr(self, 'progress_label') and self.progress_label:
                    self.progress_label.set_text(stats.get("status"))
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
        self.watch_btn.set_label("WATCH IT NOW")

    def on_watch_clicked(self, btn):
        if not getattr(self, 'selected_torrent', None):
            if hasattr(self, 'progress_label') and self.progress_label:
                self.progress_label.set_text("Please select a stream first.")
            return
            
        torrent = self.selected_torrent
        magnet = torrent.get("url") or torrent.get("magnet")
        if not magnet and torrent.get("hash"):
            from . import api
            magnet = api.build_magnet(torrent.get("hash"), self.movie_stub.get("title", ""))
            
        file_index = torrent.get("file_index")
        self._start_streaming(magnet, file_index)

    def _start_streaming(self, magnet, file_index):
        if not magnet: return
        if magnet.startswith("http://") or magnet.startswith("https://"):
            if self.window:
                self.window._play_stream(magnet)
        else:
            from . import player
            def progress_callback(stats):
                status = stats.get("status")
                url = stats.get("url")
                if url and self.window:
                    self.window._play_stream(url)
                    GLib.idle_add(self.stop_btn.set_visible, True)
                    GLib.idle_add(self.watch_btn.set_label, "▶ Continue Watching")
                elif status:
                    if hasattr(self, 'progress_label') and self.progress_label:
                        self.progress_label.set_text(status)
                        
            player.play_magnet(
                magnet,
                progress_callback=progress_callback,
                file_index=file_index,
                item_id=self.movie_stub.get("id"),
                media_type=self.media_type,
                season=self.selected_season,
                episode=self.selected_episode
            )


@Gtk.Template(resource_path="/io/github/fastrizwaan/PopcornBox/window.ui")
class CineWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CineWindow"

    window_handle: Gtk.WindowHandle = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    main_stack: Adw.ViewStack = Gtk.Template.Child()
    details_box: Gtk.Box = Gtk.Template.Child()
    library_stack: Adw.ViewStack = Gtk.Template.Child()
    movies_flowbox: Gtk.FlowBox = Gtk.Template.Child()
    series_flowbox: Gtk.FlowBox = Gtk.Template.Child()
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
    spinner: Adw.Spinner = Gtk.Template.Child()
    context_popover_menu: Gtk.PopoverMenu = Gtk.Template.Child()

    open_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
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
        super().__init__(**kwargs)
        self.app: Adw.Application = cast(Adw.Application, kwargs.get("application"))
        self.app_mpris: MPRIS = self.app.mpris  # type: ignore

        Gtk.WindowGroup().add_window(self)

        self.gl_area: Gtk.GLArea = Gtk.GLArea()
        self.offload: Gtk.GraphicsOffload = Gtk.GraphicsOffload(child=self.gl_area)
        self.offload.set_black_background(True)

        vendor: str | None = get_gpu_vendor(libgl)
        if vendor and "nvidia" in vendor:
            self.offload.set_enabled(Gtk.GraphicsOffloadEnabled.DISABLED)

        self.video_overlay.set_child(self.offload)

        self.visible_dialog: Adw.Dialog | None = None
        self.playlist_ls: Gio.ListStore = Gio.ListStore.new(PlaylistItemObj)
        self.playlist_debounce_id: int = 0
        self.playlist_prev_pos: int
        self.prev_shuffle: bool = False
        self.playlist_changed: bool = False
        self.has_some_doc_path: bool = False
        self.can_go_prev: bool = False
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
        self.click_time: int = max(ck_time, min(200, 425))
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
            osd_shadow_offset=0.6,
            osd_border_color="#BE000000",
            osd_shadow_color="#1B000000",
            osd_margin_x=66,
            osd_margin_y=66,
            volume_max=150,
            keep_open=True,
            ytdl=True,
            ytdl_raw_options="yes-playlist=",
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
        self._create_action("open-history", self._present_history)
        self._create_action("add-playlist-folder", self._on_open_folder_dialog)
        self._create_action("open-playlist-dialog", self._on_open_playlist)
        self._create_action("open-sub-menu", self._on_open_sub_menu)
        self._create_action("open-audio-menu", self._on_open_audio_menu)
        self._create_action("open-chapters-menu", self._on_open_chapters_menu)
        self._create_action("save-session", self._on_save_session)
        self._create_action(
            "save-session-close", lambda *a: self._on_save_session(close=True)
        )
        self._create_action("search-addons", self._on_search_addons)

        self.app.set_accels_for_action("win.open-folder", ["<primary>i"])
        self.app.set_accels_for_action("win.open-url", ["<primary>u"])
        self.app.set_accels_for_action("win.add-url", ["<shift><primary>u"])
        self.app.set_accels_for_action("win.open-history", ["<primary>h"])
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
            self.open_menu_btn,
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

            if btn in (self.primary_menu_btn, self.open_menu_btn):

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
                self.open_menu_btn,
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
                or self.open_menu_btn.props.active
                or self.options_menu_btn.props.active
                or self.volume_menu_btn.props.active
                or self.subtitles_menu_btn.props.active
                or self.audio_tracks_menu_btn.props.active
                or self.video_tracks_menu_btn.props.active
                or self.chapters_menu_btn.props.active
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
                streams = api.get_torrents_streamed(details["id"], media_type=item.get("type", "movie"))
                
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
                self.mpv.loadfile(self.url, mode)
                if mode == "replace":
                    self.mpv.pause = False
                    self.shuffle_toggle_btn.set_active(False)
            except mpv.ShutdownError:
                pass

        def on_clipboard_read(clipboard, result):
            text = clipboard.read_text_finish(result)

            if text and (parsed := urlparse(text)):
                if parsed.scheme in cast(list, self.mpv.protocol_list):
                    entry_row.insert_text(text, 0)

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
            if self.show_remaining:
                duration = float(self.mpv.duration or 0)
                remaining = (duration - curr_time) if duration > curr_time else 0
                self.time_elapsed_label.props.label = f"-{format_time(remaining)}"
            else:
                self.time_elapsed_label.props.label = format_time(curr_time)
        except mpv.ShutdownError:
            pass

        self.prev_prog_time = curr_time

    def _update_chapter_marks_and_menu(self, chapters):
        if not chapters:
            self.video_progress_scale.clear_marks()
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
        self.mpv.command("set", "sub-visibility", "yes")
        track_id = parameter.get_int32()
        self.mpv.sid = track_id if track_id > 0 else "no"
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
            loop_list_enabled: bool = self.mpv.loop_playlist is not False

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
                    self.mpv.loadfile(path, mode)
                    first_file = False
                    continue

                name = cast(str, item.get_basename()).lower()
                if name.endswith(SUB_EXTS):
                    if not self.mpv.core_idle:
                        self.mpv.command("sub-add", path, "select")
                    continue

                if mime_type.startswith(("video/", "audio/", "image/")) or is_url:
                    self.mpv.loadfile(path, mode)
                    first_file = False

            elif isinstance(item, str):  # URL string
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
            self.mpv["speed"] = self.prev_speed
            self.mpv.show_text(f"{self.mpv['speed']:g}×")
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
        except Exception as e:
            logger.error(f"Render error: {e}", exc_info=True)
            return

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

    def _show_toast(self, label, force_dismiss=False):
        toast = Adw.Toast(title=label, timeout=2)
        self.toast_overlay.dismiss_all()
        self.toast_overlay.add_toast(toast)
        if force_dismiss:
            timeout_add_seconds_once(2, toast.dismiss)

    def _setup_observers(self):
        @self.mpv.event_callback("start-file")
        def on_start_file(_event):
            idle_add_once(self.spinner.set_visible, True)
            self.loaded_path = str(self.mpv.path)

        @self.mpv.event_callback("file-loaded")
        def on_files_loaded(_event):
            def update():
                try:
                    self.spinner.set_visible(False)
                    self.is_local_path = is_local_path(self.mpv.path)
                    self.start_page.set_sensitive(True)
                    self._hide_ui_timeout()

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
            idle_add_once(self.spinner.set_visible, False)
            idle_add_once(self.start_page.set_sensitive, True)

            try:
                curr_pos = self.mpv.playlist_pos
                info = event.as_dict()
                reason = info["reason"]

                if reason == b"error":
                    # Avoid stopping playback on last file/folder error
                    playlist_count = cast(int, self.mpv.playlist_count)
                    if curr_pos == playlist_count - 1:
                        self.mpv.playlist_pos = 0

                    self.error_count += 1
                    logger.warning(f"File error path: {self.loaded_path}")
                    error = info["file_error"].decode("utf-8")
                    idle_add_once(self._show_toast, _("File Error") + f": {error}")

                    if self.error_count == 20:
                        self.mpv.stop()
                        self.shuffle_toggle_btn.set_active(False)
                        self.error_count = 0
                elif (
                    not self.mpv.keep_open and self.mpv.idle_active and not self.startup
                ):
                    idle_add_once(self.close)
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
                self.start_page.set_visible(is_idle)
                self.controls_box.set_visible(not is_idle)
                self.gl_area.set_visible(not is_idle)

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
        self._populate_addons()
        
        self.movies_page = 1
        self.series_page = 1
        self.is_fetching_movies = False
        self.is_fetching_series = False
        self.movies_seen_ids = set()
        self.series_seen_ids = set()

        if hasattr(self, "movies_scrolled"):
            self.movies_scrolled.get_vadjustment().connect("value-changed", self._on_movies_scroll)
        if hasattr(self, "series_scrolled"):
            self.series_scrolled.get_vadjustment().connect("value-changed", self._on_series_scroll)
        
        def fetch_movies():
            self.is_fetching_movies = True
            try:
                movies = fetch_items(media_type="movie", catalog_id="top", catalog_url="https://v3-cinemeta.strem.io/manifest.json", page=1)
                if movies:
                    GLib.idle_add(self._populate_flowbox, self.movies_flowbox, movies, self.movies_seen_ids)
            except Exception as e:
                logger.error(f"Error fetching movies: {e}")
            finally:
                self.is_fetching_movies = False

        def fetch_series():
            self.is_fetching_series = True
            try:
                series = fetch_items(media_type="series", catalog_id="top", catalog_url="https://v3-cinemeta.strem.io/manifest.json", page=1)
                if series:
                    GLib.idle_add(self._populate_flowbox, self.series_flowbox, series, self.series_seen_ids)
            except Exception as e:
                logger.error(f"Error fetching series: {e}")
            finally:
                self.is_fetching_series = False

        threading.Thread(target=fetch_movies, daemon=True).start()
        threading.Thread(target=fetch_series, daemon=True).start()

    def _on_movies_scroll(self, adj):
        if self.is_fetching_movies:
            return
        if adj.get_value() > 0 and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 400:
            self.is_fetching_movies = True
            next_page = self.movies_page + 1
            def fetch_next():
                try:
                    movies = fetch_items(media_type="movie", catalog_id="top", catalog_url="https://v3-cinemeta.strem.io/manifest.json", page=next_page)
                    if movies:
                        self.movies_page = next_page
                        GLib.idle_add(self._append_flowbox, self.movies_flowbox, movies, self.movies_seen_ids)
                except Exception as e:
                    logger.error(f"Error fetching more movies: {e}")
                finally:
                    self.is_fetching_movies = False
            threading.Thread(target=fetch_next, daemon=True).start()

    def _on_series_scroll(self, adj):
        if self.is_fetching_series:
            return
        if adj.get_value() > 0 and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 400:
            self.is_fetching_series = True
            next_page = self.series_page + 1
            def fetch_next():
                try:
                    series = fetch_items(media_type="series", catalog_id="top", catalog_url="https://v3-cinemeta.strem.io/manifest.json", page=next_page)
                    if series:
                        self.series_page = next_page
                        GLib.idle_add(self._append_flowbox, self.series_flowbox, series, self.series_seen_ids)
                except Exception as e:
                    logger.error(f"Error fetching more series: {e}")
                finally:
                    self.is_fetching_series = False
            threading.Thread(target=fetch_next, daemon=True).start()

    def _populate_flowbox(self, flowbox, items, seen_ids=None):
        while flowbox.get_first_child() is not None:
            flowbox.remove(flowbox.get_first_child())
        if seen_ids is not None:
            seen_ids.clear()
        self._append_flowbox(flowbox, items, seen_ids)

    def _append_flowbox(self, flowbox, items, seen_ids=None):
        for item in items:
            item_id = item.get("id")
            if seen_ids is not None and item_id:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            flowbox.append(MovieWidget(item, self._on_movie_clicked))

    def _on_movie_clicked(self, movie_data):
        while child := self.details_box.get_first_child():
            self.details_box.remove(child)
            
        def on_back():
            self.main_stack.set_visible_child_name("library")
            
        page = MovieDetailsPage(movie_data, on_back, window=self)
        self.details_box.append(page)
        self.main_stack.set_visible_child_name("details")

    def _play_stream(self, url):
        self.spinner.set_visible(False)
        self.main_stack.set_visible_child_name("player")
        
        self.mpv.loadfile(url, "replace")
        self.mpv.pause = False
        self.is_inactive = False

    def _on_no_streams_found(self):
        self.spinner.set_visible(False)
        logger.warning("No direct HTTP streams found for this item.")

    def _close_player(self, *args):
        self.mpv.stop()
        if self.details_box.get_first_child():
            self.main_stack.set_visible_child_name("details")
        else:
            self.main_stack.set_visible_child_name("library")

    def _populate_addons(self):
        while self.addons_listbox.get_first_child() is not None:
            self.addons_listbox.remove(self.addons_listbox.get_first_child())
            
        from . import database
        addons = database.get_addons()
        for addon in addons:
            name_str = GLib.markup_escape_text(addon.get("name", "Unknown") or "Unknown")
            desc_str = GLib.markup_escape_text(addon.get("description", "") or "")
            row = Adw.ActionRow(title=name_str, subtitle=desc_str)
            
            remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove_btn.add_css_class("destructive-action")
            remove_btn.connect("clicked", lambda btn, a=addon: self._remove_addon(a))
            
            row.add_suffix(remove_btn)
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
                    GLib.idle_add(self.addon_url_entry.set_text, "")
            except Exception as e:
                logger.error(f"Failed to add addon: {e}")
                
        threading.Thread(target=fetch_and_add, daemon=True).start()
        
    def _remove_addon(self, addon):
        from . import database
        addon_id = addon.get("id")
        if addon_id:
            database.remove_addon(addon_id)
            self._populate_addons()
