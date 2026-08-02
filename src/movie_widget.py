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
    global _image_pool, _disk_pool
    try:
        _image_pool.shutdown(wait=False, cancel_futures=True)
        _disk_pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        _image_pool.shutdown(wait=False)
        _disk_pool.shutdown(wait=False)
    _image_pool = ThreadPoolExecutor(max_workers=4)
    _disk_pool = ThreadPoolExecutor(max_workers=4)
    FAILED_IMAGE_URLS.clear()

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

FAILED_IMAGE_URLS = set()

def load_image_into_picture(url, picture_widget, width=None, height=None, on_error=None):
    if not url or not isinstance(url, str): return
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    
    if "media-amazon.com" in url or "ssl-images-amazon.com" in url:
        import re
        url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', url)
    
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(IMAGE_CACHE_DIR, url_hash)

    if url in FAILED_IMAGE_URLS and not (os.path.exists(cache_file) and os.path.getsize(cache_file) > 0):
        if on_error: GLib.idle_add(on_error)
        return

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
                for attempt in range(2):
                    try:
                        with urllib.request.urlopen(req, timeout=6) as response:
                            data = response.read()
                            if data:
                                with open(cache_file, 'wb') as f:
                                    f.write(data)
                                FAILED_IMAGE_URLS.discard(url)
                            break
                    except Exception as e:
                        if attempt == 1:
                            FAILED_IMAGE_URLS.add(url)
                        else:
                            import time
                            time.sleep(0.5)
                
            if not data:
                if on_error: GLib.idle_add(on_error)
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
            else:
                if on_error: GLib.idle_add(on_error)
        except Exception as e:
            print(f"Failed to load image {url}: {e}")
            if on_error: GLib.idle_add(on_error)
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        try:
            _disk_pool.submit(fetch_image)
        except RuntimeError:
            pass  # Pool was shut down between reference capture and submit
    else:
        try:
            _image_pool.submit(fetch_image)
        except RuntimeError:
            pass  # Pool was shut down between reference capture and submit

def _apply_pixbuf(picture_widget, pixbuf):
    try:
        if not picture_widget or not pixbuf:
            print("[IMAGE APPLY WARNING] picture_widget or pixbuf is None.")
            return False
        picture_widget.set_can_shrink(True)
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture_widget.set_paintable(texture)
    except Exception as e:
        print(f"[IMAGE APPLY ERROR] Failed to paint texture: {e}")
    return False


def fetch_fallback_poster(item_id, item_type, poster_widget, title=None, width=130, height=195):
    if not item_id: return
    try:
        import urllib.request
        from .api import _get_cached_request
        
        # FIRST FALLBACK: Use IMDb autocomplete API to get the highest quality poster directly
        if str(item_id).startswith("tt"):
            first_char = str(item_id)[0].lower()
            imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{item_id}.json"
            req = urllib.request.Request(imdb_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            with urllib.request.urlopen(req, timeout=4) as response:
                import json
                data = json.loads(response.read().decode('utf-8', errors='ignore'))
                if data and "d" in data and len(data["d"]) > 0:
                    for item in data["d"]:
                        if item.get("id") == item_id and "i" in item and "imageUrl" in item["i"]:
                            poster_url = item["i"]["imageUrl"]
                            import re
                            poster_url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX500_.jpg', poster_url)
                            print(f"[IMDb API] Successfully fetched fallback catalog poster for {item_id}: {poster_url}")
                            try:
                                from .database import get_cached_metadata, save_cached_metadata
                                existing = get_cached_metadata(item_id, item_type) or {}
                                existing["medium_cover_image"] = poster_url
                                save_cached_metadata(item_id, item_type, existing)
                            except Exception:
                                pass
                            GLib.idle_add(load_image_into_picture, poster_url, poster_widget, width, height)
                            return
    except Exception as e:
        pass
        
    try:
        from .api import _get_cached_request
        if str(item_id).startswith("tt"):
            url = f"https://v3-cinemeta.strem.io/meta/{item_type}/{item_id}.json"
            meta_data = _get_cached_request(url, max_age_hours=168)
            if meta_data and "meta" in meta_data and meta_data["meta"].get("poster"):
                poster_url = meta_data["meta"]["poster"]
                try:
                    from .database import get_cached_metadata, save_cached_metadata
                    existing = get_cached_metadata(item_id, item_type) or {}
                    existing["medium_cover_image"] = poster_url
                    save_cached_metadata(item_id, item_type, existing)
                except Exception:
                    pass
                GLib.idle_add(load_image_into_picture, poster_url, poster_widget, width, height)
                return
        
        c_type = "series" if item_type in ["series", "anime", "tv"] else "movie"
        url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{item_id}.json"
        try:
            tmdb_data = _get_cached_request(url, max_age_hours=168, timeout=4)
            if tmdb_data and "meta" in tmdb_data and tmdb_data["meta"].get("poster"):
                poster_url = tmdb_data["meta"]["poster"]
                try:
                    from .database import get_cached_metadata, save_cached_metadata
                    existing = get_cached_metadata(item_id, item_type) or {}
                    existing["medium_cover_image"] = poster_url
                    save_cached_metadata(item_id, item_type, existing)
                except Exception:
                    pass
                GLib.idle_add(load_image_into_picture, poster_url, poster_widget, width, height)
                return
        except Exception:
            pass
    except Exception as e:
        pass

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
            remove_btn.add_css_class("card-remove-btn")
            remove_btn.add_css_class("osd")
            remove_btn.add_css_class("circular")
            remove_btn.set_halign(Gtk.Align.END)
            remove_btn.set_valign(Gtk.Align.START)
            remove_btn.set_margin_top(6)
            remove_btn.set_margin_end(6)
            remove_btn.set_tooltip_text("Remove")
            remove_btn.set_visible(False)
            remove_btn.connect("clicked", lambda btn: on_remove_clicked(self.movie_data, self))
            self.overlay.add_overlay(remove_btn)
            self.remove_btn_ref = remove_btn
            
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *args: remove_btn.set_visible(True))
            hover.connect("leave", lambda *args: remove_btn.set_visible(False))
            self.add_controller(hover)
            
        icon_container.append(self.overlay)
        self.append(icon_container)
        
        item_id = movie_data.get("imdb_id") or movie_data.get("id")
        item_type = movie_data.get("type", "movie")
        
        poster_url = None
        has_cached_poster = False
        if item_id:
            try:
                from .database import get_cached_metadata
                cached = get_cached_metadata(item_id, item_type)
                if cached and cached.get("medium_cover_image"):
                    poster_url = cached.get("medium_cover_image")
                    has_cached_poster = True
            except Exception:
                pass
                
        if not poster_url:
            poster_url = extract_image_url(movie_data)
        
        def trigger_fallback():
            try:
                _image_pool.submit(fetch_fallback_poster, item_id, item_type, self.poster_image, movie_data.get("title") or movie_data.get("name"), 130, 195)
            except RuntimeError:
                pass

        if poster_url:
            load_image_into_picture(poster_url, self.poster_image, width=130, height=195, on_error=trigger_fallback)
        else:
            trigger_fallback()
            
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
