import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, GObject, GLib, Gdk, GdkPixbuf, Gio, Pango
import urllib.request
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "popcorn-box")
else:
    BASE_DIR = os.path.expanduser("~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box")
IMAGE_CACHE_DIR = os.path.join(BASE_DIR, 'images')
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

_image_pool = ThreadPoolExecutor(max_workers=4)
_disk_pool = ThreadPoolExecutor(max_workers=4)

def cancel_pending_image_downloads():
    global _image_pool
    try:
        _image_pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        _image_pool.shutdown(wait=False)
    _image_pool = ThreadPoolExecutor(max_workers=4)

def extract_image_url(m):
    if not isinstance(m, dict):
        return ""
    candidates = [
        m.get("medium_cover_image"),
        m.get("poster"),
        m.get("logo"),
        m.get("icon"),
        m.get("thumbnail"),
        m.get("banner"),
        m.get("background"),
        m.get("image"),
        m.get("cover")
    ]
    for url in candidates:
        if isinstance(url, str):
            url = url.strip()
            if url.startswith("//"):
                return "https:" + url
            if url.startswith("http://") or url.startswith("https://"):
                return url
    return ""

def load_image_into_picture(url, picture_widget, width=None, height=None):
    if not url or not isinstance(url, str): return
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(IMAGE_CACHE_DIR, url_hash)

    def fetch_image():
        try:
            data = None
            if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
                with open(cache_file, 'rb') as f:
                    data = f.read()
            else:
                if os.path.exists(cache_file):
                    try: os.remove(cache_file)
                    except Exception: pass
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                )
                for attempt in range(1):
                    try:
                        with urllib.request.urlopen(req, timeout=3) as response:
                            data = response.read()
                            if data:
                                with open(cache_file, 'wb') as f:
                                    f.write(data)
                            break
                    except Exception as e:
                        if attempt == 0:
                            pass
                
            if not data:
                return
                
            loader = GdkPixbuf.PixbufLoader()
            pixbuf = None
            try:
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
            except Exception as e:
                try:
                    loader.close()
                except Exception:
                    pass
                try:
                    if os.path.exists(cache_file):
                        os.remove(cache_file)
                except Exception:
                    pass
                print(f"Failed to decode image {url}: {e}")
                
            if pixbuf:
                if width and height:
                    orig_w = pixbuf.get_width()
                    orig_h = pixbuf.get_height()
                    if orig_w > 0 and orig_h > 0:
                        target_ratio = width / height
                        orig_ratio = orig_w / orig_h
                        if orig_ratio > target_ratio:
                            crop_w = int(orig_h * target_ratio)
                            crop_h = orig_h
                            crop_x = (orig_w - crop_w) // 2
                            crop_y = 0
                        else:
                            crop_w = orig_w
                            crop_h = int(orig_w / target_ratio)
                            crop_x = 0
                            crop_y = (orig_h - crop_h) // 2
                        try:
                            sub_pixbuf = GdkPixbuf.Pixbuf.new_subpixbuf(pixbuf, crop_x, crop_y, crop_w, crop_h)
                            pixbuf = sub_pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
                        except Exception:
                            pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
                GLib.idle_add(_apply_pixbuf, picture_widget, pixbuf)
        except Exception as e:
            print(f"Failed to load image {url}: {e}")
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        _disk_pool.submit(fetch_image)
    else:
        _image_pool.submit(fetch_image)

def _apply_pixbuf(picture_widget, pixbuf):
    try:
        if picture_widget.get_parent() is None:
            return False
        picture_widget.set_can_shrink(True)
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture_widget.set_paintable(texture)
    except Exception:
        pass
    return False


class MovieWidget(Gtk.Box):
    def __init__(self, movie_data, click_callback, on_remove_clicked=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.movie_data = movie_data
        self.click_callback = click_callback
        self.remove_btn_ref = None
        
        self.set_size_request(130, 195)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.add_css_class("pt-card")
        
        click = Gtk.GestureClick()
        click.connect("released", self._on_card_released)
        self.add_controller(click)
        
        icon_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        icon_container.set_hexpand(True)
        icon_container.set_halign(Gtk.Align.CENTER)
        
        self.overlay = Gtk.Overlay()
        self.poster_image = Gtk.Picture()
        self.poster_image.set_can_shrink(True)
        self.poster_image.set_size_request(130, 195)
        self.poster_image.set_content_fit(Gtk.ContentFit.COVER)
        
        self.overlay.set_child(self.poster_image)
        
        if on_remove_clicked:
            remove_btn = Gtk.Button(icon_name="window-close-symbolic")
            remove_btn.set_can_focus(False)
            remove_btn.add_css_class("osd")
            remove_btn.add_css_class("circular")
            remove_btn.set_halign(Gtk.Align.END)
            remove_btn.set_valign(Gtk.Align.START)
            remove_btn.set_margin_top(4)
            remove_btn.set_margin_end(4)
            remove_btn.set_tooltip_text("Remove")
            remove_btn.set_visible(False)
            remove_btn.connect("clicked", lambda btn: on_remove_clicked(self.movie_data, self))
            self.overlay.add_overlay(remove_btn)
            self.remove_btn_ref = remove_btn
            
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *args: remove_btn.set_visible(True))
            hover.connect("leave", lambda *args: remove_btn.set_visible(False))
            icon_container.add_controller(hover)
            
        icon_container.append(self.overlay)
        self.append(icon_container)
        
        poster_url = extract_image_url(movie_data)
        if poster_url:
            load_image_into_picture(poster_url, self.poster_image, width=130, height=195)
            
        title_text = movie_data.get("title") or movie_data.get("name") or "Unknown"
        title_label = Gtk.Label(label=title_text)
        title_label.set_lines(1)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_max_width_chars(1)
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.FILL)
        title_label.set_xalign(0.0)
        title_label.add_css_class("pt-card-title")
        self.append(title_label)
        
        year_str = str(movie_data.get("year", "")) or str(movie_data.get("releaseInfo", ""))
        if year_str:
            year_label = Gtk.Label(label=year_str)
            year_label.set_halign(Gtk.Align.START)
            year_label.add_css_class("pt-card-year")
            self.append(year_label)

    def _on_card_released(self, gesture, n_press, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        target = picked
        while target is not None:
            if target == self.remove_btn_ref:
                return
            if target == self:
                break
            target = target.get_parent()
            
        if self.click_callback:
            self.click_callback(self.movie_data)
