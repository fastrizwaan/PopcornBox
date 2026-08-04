import os
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, GLib, Pango, Gdk
from . import player
from . import database
from .utils import open_uri

class DownloadItemRow(Gtk.Box):
    def __init__(self, download):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.add_css_class('dl-row')
        
        self.download = download
        self.info_hash = download.get("info_hash") or ""
        self.magnet = download.get("magnet") or ""
        self.file_index = download.get("file_index")
        
        self.display_title = download.get("name") or "Unknown"
        self.actual_filename = download.get("name") or ""
        
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_hexpand(True)
        
        self.title_label = Gtk.Label(label=self.display_title)
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.add_css_class('title-2')
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(self.title_label)
        
        self.progress_bar = Gtk.ProgressBar()
        vbox.append(self.progress_bar)
        
        self.status_label = Gtk.Label(label="Checking...")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.add_css_class('dim-label')
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(self.status_label)
        
        self.append(vbox)
        
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_valign(Gtk.Align.CENTER)
        
        self.popover = Gtk.Popover()
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pop_box.set_margin_top(8)
        pop_box.set_margin_bottom(8)
        pop_box.set_margin_start(8)
        pop_box.set_margin_end(8)
        
        self.play_btn = Gtk.Button(label="Resume Download", icon_name="go-down-symbolic")
        self.play_btn.set_tooltip_text("Resume Download")
        self.play_btn.set_has_frame(False)
        self.play_btn.set_halign(Gtk.Align.START)
        self.play_btn.connect("clicked", self.on_play_clicked)
        pop_box.append(self.play_btn)

        self.stop_btn = Gtk.Button(label="Pause Download", icon_name="media-playback-pause-symbolic")
        self.stop_btn.set_tooltip_text("Pause Download")
        self.stop_btn.set_has_frame(False)
        self.stop_btn.set_halign(Gtk.Align.START)
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        pop_box.append(self.stop_btn)
        
        self.watch_btn = Gtk.Button(label="Play Video File", icon_name="media-playback-start-symbolic")
        self.watch_btn.set_tooltip_text("Play Video File")
        self.watch_btn.set_has_frame(False)
        self.watch_btn.set_halign(Gtk.Align.START)
        self.watch_btn.connect("clicked", self.on_watch_clicked)
        pop_box.append(self.watch_btn)
        
        folder_btn = Gtk.Button(label="Open Folder", icon_name="folder-symbolic")
        folder_btn.set_tooltip_text("Open Folder")
        folder_btn.set_has_frame(False)
        folder_btn.set_halign(Gtk.Align.START)
        folder_btn.connect("clicked", self.on_folder_clicked)
        pop_box.append(folder_btn)
        
        copy_btn = Gtk.Button(label="Copy Magnet Link", icon_name="edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy Magnet Link")
        copy_btn.set_has_frame(False)
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.connect("clicked", self.on_copy_clicked)
        pop_box.append(copy_btn)
        
        source_btn = Gtk.Button(label="Go to Source", icon_name="go-home-symbolic")
        source_btn.set_tooltip_text("Go to Source")
        source_btn.set_has_frame(False)
        source_btn.set_halign(Gtk.Align.START)
        item_id = download.get("item_id")
        media_type = download.get("media_type")
        if not item_id or not media_type:
            source_btn.set_sensitive(False)
            source_btn.set_tooltip_text("Source information missing for this download.")
        else:
            source_btn.connect("clicked", lambda btn: self.on_source_clicked(item_id, media_type))
        pop_box.append(source_btn)
        
        del_btn = Gtk.Button(label="Delete", icon_name="user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.set_has_frame(False)
        del_btn.set_halign(Gtk.Align.START)
        del_btn.add_css_class('destructive-action')
        del_btn.connect("clicked", self.on_delete_clicked)
        pop_box.append(del_btn)
        
        self.popover.set_child(pop_box)
        
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("view-more-symbolic")
        menu_btn.set_popover(self.popover)
        menu_btn.set_tooltip_text("Options")
        action_box.append(menu_btn)
        
        self.append(action_box)
        
        self.is_active = True
        self.poll_id = GLib.timeout_add(1000, self.update_status)
        self.update_status()
        
        # In GTK4, widgets emit 'destroy' when disposed
        self.connect("destroy", self.on_destroy)
        
    def on_destroy(self, widget=None):
        self.is_active = False
        if hasattr(self, 'poll_id'):
            GLib.source_remove(self.poll_id)
            del self.poll_id
            
    def update_status(self):
        if not self.is_active:
            return False
            
        suffix = ""
        season = self.download.get("season")
        episode = self.download.get("episode")
        if season is not None and episode is not None:
            suffix += f" | S{int(season):02d}E{int(episode):02d}"
            
        stats = player.get_engine_stats(self.info_hash)
        if stats and stats.get("name") and stats.get("name") != "Unknown":
            engine_name = stats.get("name")
            if engine_name != self.display_title and not engine_name.startswith("http"):
                self.display_title = engine_name
                self.title_label.set_text(self.display_title)
                
        display_filename = self.actual_filename
        if stats and stats.get("filePath"):
            display_filename = os.path.basename(stats.get("filePath"))
            
        if display_filename and display_filename != "Unknown" and display_filename != self.display_title:
            suffix += f" | {display_filename}"
            
        if stats:
            dl = stats.get("downloaded", 0)
            tot = stats.get("totalLength", 0)
            prog = stats.get("progress", 0)
            self.progress_bar.set_fraction(prog)
            speed_dl = stats.get("downloadSpeed", 0) / 1024
            speed_ul = stats.get("uploadSpeed", 0) / 1024
            
            status_text = f"{prog*100:.1f}%"
            if speed_dl > 0 or speed_ul > 0:
                status_text += f" - D: {speed_dl:.1f} KiB/s | U: {speed_ul:.1f} KiB/s"
            elif prog < 1.0:
                status_text += f" - Connecting..."
                
            if tot > 0:
                status_text += f" ({dl/(1024*1024):.1f} MB / {tot/(1024*1024*1024):.2f} GB)"
                
            status_desc = stats.get("status", "")
            if ("seeding" in status_desc.lower() or "finished" in status_desc.lower()) and prog >= 1.0:
                status_text = f"Seeding - {status_text}"
                
            ratio = stats.get("ratio", 0)
            peers = stats.get("activePeers", 0)
            seeds = stats.get("seeds", 0)
            
            if ratio > 0:
                status_text += f" | Ratio: {ratio:.2f}"
            if peers > 0 or seeds > 0:
                status_text += f" | Peers: {peers} / Seeds: {seeds}"
                
            self.status_label.set_text(status_text + suffix)
            
            self.play_btn.set_visible(False)
            self.stop_btn.set_visible(True)
        else:
            self.play_btn.set_visible(True)
            self.stop_btn.set_visible(False)
            
            path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
            if os.path.exists(path):
                self.status_label.set_text("Paused" + suffix)
            else:
                self.status_label.set_text("Not Downloaded" + suffix)
                self.progress_bar.set_fraction(0.0)
                
        return True
        
    def on_play_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.download_magnet_background(
            self.magnet,
            file_index=self.file_index,
            item_id=self.download.get("item_id"),
            media_type=self.download.get("media_type")
        )
        self.update_status()
        
    def on_watch_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        
        window = self.get_root()
        if not hasattr(window, '_play_stream'):
            return
            
        title = self.download.get("name", "Downloaded Media")
        if hasattr(window, 'show_player_loading'):
            window.show_player_loading("Starting stream...", title)
            
        def progress_callback(stats):
            url = stats.get("url")
            if url and hasattr(window, '_play_stream'):
                window._play_stream(url, title)
            elif isinstance(stats, dict) and hasattr(window, 'format_stream_stats'):
                text = window.format_stream_stats(stats)
                if hasattr(window, 'update_player_loading'):
                    window.update_player_loading(text)
                
        player.play_magnet(
            self.magnet, 
            progress_callback=progress_callback,
            file_index=self.file_index, 
            item_id=self.download.get("item_id"), 
            media_type=self.download.get("media_type", "movie"),
            season=self.download.get("season"),
            episode=self.download.get("episode")
        )
        
    def on_stop_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.stop_engine_explicit(self.info_hash)
        self.update_status()
        
    def on_folder_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
        os.makedirs(path, exist_ok=True)
        open_uri(GLib.filename_to_uri(path, None), self.get_root())
            
    def on_copy_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(self.magnet)
        
    def on_delete_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.stop_engine_explicit(self.info_hash)
        database.remove_download(self.info_hash)
        path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
        import shutil
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        
        # Remove from ListBox
        parent = self.get_parent()
        if parent:
            if isinstance(parent, Gtk.ListBoxRow):
                # When appended to ListBox, it wraps in a ListBoxRow
                grandparent = parent.get_parent()
                if grandparent:
                    grandparent.remove(parent)
            else:
                parent.remove(self)
        self.on_destroy(self)

    def on_source_clicked(self, item_id, media_type):
        if hasattr(self, 'popover'): self.popover.popdown()
        # Find the main app window
        widget = self.get_root()
        if hasattr(widget, '_on_movie_clicked'):
            stub = {"id": item_id, "type": media_type, "title": "Loading...", "medium_cover_image": ""}
            widget._on_movie_clicked(stub)
