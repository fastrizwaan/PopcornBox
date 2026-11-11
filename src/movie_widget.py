import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, GObject, GLib, Gdk, GdkPixbuf, Gio, Pango
import urllib.request
import urllib.error
import urllib.parse
import os
import time
import weakref
import hashlib
import json
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from .utils import debug_log
from . import database

def _get_cached_request(*args, **kwargs):
    from .api import _get_cached_request as req
    return req(*args, **kwargs)

# Poster and backdrop URLs come from meta responses, and a title's picture does
# not change often enough to re-fetch. Mirrors api.METADATA_CACHE_HOURS, restated
# here because importing api at module level would be circular.
METADATA_CACHE_HOURS = None

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "popcorn-box")
else:
    flatpak_cache = os.path.expanduser("~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box")
    if os.path.exists(flatpak_cache):
        BASE_DIR = flatpak_cache
    else:
        BASE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "popcorn-box")
IMAGE_CACHE_DIR = os.path.join(BASE_DIR, 'images')
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

_image_pool = ThreadPoolExecutor(max_workers=10)
_disk_pool = ThreadPoolExecutor(max_workers=2)
_meta_fallback_pool = ThreadPoolExecutor(max_workers=8)

import atexit
atexit.register(_image_pool.shutdown, wait=False, cancel_futures=True)
atexit.register(_disk_pool.shutdown, wait=False, cancel_futures=True)
atexit.register(_meta_fallback_pool.shutdown, wait=False, cancel_futures=True)

_MEMORY_PIXBUF_CACHE = OrderedDict()
_MEMORY_PIXBUF_LOCK = threading.Lock()
_MEMORY_PIXBUF_CACHE_BYTES = 0
_MAX_MEMORY_PIXBUF_CACHE_BYTES = 32 * 1024 * 1024


def _get_memory_cached_pixbuf(key):
    with _MEMORY_PIXBUF_LOCK:
        entry = _MEMORY_PIXBUF_CACHE.get(key)
        if entry is None:
            return None
        _MEMORY_PIXBUF_CACHE.move_to_end(key)
        return entry[0]


def _cache_memory_pixbuf(key, pixbuf):
    global _MEMORY_PIXBUF_CACHE_BYTES
    try:
        byte_size = int(pixbuf.get_rowstride()) * int(pixbuf.get_height())
    except Exception:
        return
    if byte_size <= 0 or byte_size > _MAX_MEMORY_PIXBUF_CACHE_BYTES:
        return

    with _MEMORY_PIXBUF_LOCK:
        old_entry = _MEMORY_PIXBUF_CACHE.pop(key, None)
        if old_entry is not None:
            _MEMORY_PIXBUF_CACHE_BYTES -= old_entry[1]
        while (
            _MEMORY_PIXBUF_CACHE
            and _MEMORY_PIXBUF_CACHE_BYTES + byte_size > _MAX_MEMORY_PIXBUF_CACHE_BYTES
        ):
            _, (_, evicted_bytes) = _MEMORY_PIXBUF_CACHE.popitem(last=False)
            _MEMORY_PIXBUF_CACHE_BYTES -= evicted_bytes
        _MEMORY_PIXBUF_CACHE[key] = (pixbuf, byte_size)
        _MEMORY_PIXBUF_CACHE_BYTES += byte_size

# url -> Event for image downloads currently in progress. Grids frequently
# show the same poster in several rows; without this, each card downloaded it.
_IMAGE_INFLIGHT = {}
_IMAGE_INFLIGHT_LOCK = threading.Lock()

_FAILED_IMAGE_LOCK = threading.Lock()
# url -> unix time of the last failed download. A permanent ban is what made
# blank tiles stay blank for the rest of the session: one CDN timeout during a
# fast scroll marked a poster dead forever, even though the same URL fetched fine
# a minute later. Failures now expire, and the repair tick below re-tries the
# tiles that are still empty.
FAILED_IMAGE_URLS = {}
_FAILED_IMAGE_RETRY_SECONDS = 30 * 60

def _is_image_failed(url):
    now = time.time()
    with _FAILED_IMAGE_LOCK:
        failed_at = FAILED_IMAGE_URLS.get(url)
        if failed_at is None:
            return False
        if now - failed_at >= _FAILED_IMAGE_RETRY_SECONDS:
            FAILED_IMAGE_URLS.pop(url, None)
            return False
        return True

def _mark_image_failed(url):
    with _FAILED_IMAGE_LOCK:
        if len(FAILED_IMAGE_URLS) >= 4096:
            oldest = min(FAILED_IMAGE_URLS, key=FAILED_IMAGE_URLS.get)
            FAILED_IMAGE_URLS.pop(oldest, None)
        FAILED_IMAGE_URLS[url] = time.time()

def _mark_image_success(url):
    with _FAILED_IMAGE_LOCK:
        FAILED_IMAGE_URLS.pop(url, None)

def _clear_failed_images():
    with _FAILED_IMAGE_LOCK:
        FAILED_IMAGE_URLS.clear()

# Tiles that came back empty -- the host's image window closed, the CDN timed
# out, the card had no poster URL yet. They are remembered here and the tick
# below re-issues the fetch a while later, so a card is not stuck blank until
# the user happens to rebuild the grid. Entries hold weak references: a card the
# flowbox has already thrown away is dropped, not kept alive by its own repair.
_BLANK_TILES = []
_BLANK_TILES_LOCK = threading.Lock()
_MAX_BLANK_TILES = 400
_BLANK_TILE_REPAIR_SECONDS = 20
_BLANK_TILE_MAX_TRIES = 3
_BLANK_TILE_TICK_ARMED = False

def _cancel_blank_tile_repairs():
    global _BLANK_TILE_TICK_ARMED
    with _BLANK_TILES_LOCK:
        _BLANK_TILES.clear()
        _BLANK_TILE_TICK_ARMED = False

def _requeue_blank_tile(entry):
    """Put a tile back for a later tick without charging it an attempt."""
    tile = entry["pic"]()
    with _BLANK_TILES_LOCK:
        if tile is None or len(_BLANK_TILES) >= _MAX_BLANK_TILES:
            return
        if any(existing["pic"]() is tile for existing in _BLANK_TILES):
            return
        _BLANK_TILES.append(entry)

# A card that is not on the screen costs no download. While another page has the
# screen - movie details, the player, another tab - its tiles' requests are parked
# here instead of occupying the two image permits of their host, and the watch
# below re-issues them the moment the tile becomes visible again. That is the
# pause/resume the navigation needs, per tile, so no page switch can strand a
# card: an aborted or parked picture is finished when the user comes back.
def _tile_is_on_screen(picture_widget):
    """True unless this tile is attached to a window whose page is off screen.

    A widget still being built has no root yet, and is treated as visible: the
    grid it belongs to is on its way to the screen. Only an attached tile that is
    hidden - another tab, movie details, the player - is parked.
    """
    try:
        if picture_widget.get_root() is None:
            return True
        return bool(picture_widget.get_mapped())
    except Exception:
        return True

def _watch_tile_for_visibility(picture_widget, url, width, height, crop):
    picture_widget._popcornbox_pending_image = (url, width, height, crop)
    if getattr(picture_widget, "_popcornbox_visibility_watched", False):
        return
    try:
        # A hidden page's children get `map` when it is shown again. GTK4 does not
        # emit notify::mapped for that switch, so this is the signal to listen to.
        picture_widget.connect("map", _on_tile_back_on_screen)
        picture_widget._popcornbox_visibility_watched = True
    except Exception:
        pass

def _on_tile_back_on_screen(picture_widget, *_args):
    """Come back for the picture this tile still owes, once the page is drawn.

    Showing a page maps every card on it at once, so the re-issue waits for an
    idle turn rather than running a cache stat and a submit per tile inside the
    map cascade.
    """
    try:
        GLib.idle_add(_reissue_parked_tile, picture_widget)
    except Exception:
        pass

def _reissue_parked_tile(picture_widget):
    try:
        if not _tile_is_on_screen(picture_widget):
            return False
        pending = getattr(picture_widget, "_popcornbox_pending_image", None)
        if not pending:
            return False
        url, width, height, crop = pending
        if url and getattr(picture_widget, "_popcornbox_image_url", None) != url:
            return False
        if picture_widget.get_paintable() is not None:
            return False
        # A fresh visit gets a fresh bounded budget from the repair queue.
        picture_widget._popcornbox_blank_attempts = 0
        load_image_into_picture(url, picture_widget, width=width, height=height, crop=crop)
    except Exception:
        pass
    return False

def _note_blank_tile(picture_widget, url, width, height, crop, unresolved=None):
    """Queue one more polite attempt at a tile that is still showing nothing.

    The attempt count lives on the tile itself, so however many paths report the
    same failure a card is retried a bounded number of times in total. `url` is
    the picture to re-ask for; `unresolved` is the card whose poster URL is still
    unknown, which the tick sends through the metadata fallback instead.
    """
    global _BLANK_TILE_TICK_ARMED
    if picture_widget is None or (not url and not unresolved):
        return
    if url and getattr(picture_widget, "_popcornbox_image_url", None) != url:
        return
    attempts = getattr(picture_widget, "_popcornbox_blank_attempts", 0)
    if not isinstance(attempts, int):
        attempts = 0
    if _tile_is_on_screen(picture_widget):
        # Only a tile the user can see spends one of its tries; a tile parked
        # because another page has the screen is finished later for free.
        attempts += 1
        if attempts > _BLANK_TILE_MAX_TRIES:
            return
    picture_widget._popcornbox_blank_attempts = attempts
    if unresolved:
        # The tick only re-asks a tile that still carries this card's stamp, so
        # the identity is written where the queue entry is created.
        picture_widget._popcornbox_blank_item_id = str(unresolved[0])
    with _BLANK_TILES_LOCK:
        if len(_BLANK_TILES) >= _MAX_BLANK_TILES:
            return
        if any(entry["pic"]() is picture_widget for entry in _BLANK_TILES):
            return
        _BLANK_TILES.append({
            "pic": weakref.ref(picture_widget),
            "url": url,
            "width": width,
            "height": height,
            "crop": crop,
            "unresolved": unresolved,
        })
        armed = _BLANK_TILE_TICK_ARMED
        _BLANK_TILE_TICK_ARMED = True
    if not armed:
        try:
            GLib.timeout_add_seconds(_BLANK_TILE_REPAIR_SECONDS, _blank_tile_repair_tick)
        except Exception:
            with _BLANK_TILES_LOCK:
                _BLANK_TILE_TICK_ARMED = False

def _blank_tile_repair_tick():
    """Re-issue the oldest blank tiles, a slice at a time, from the main loop.

    Only queue bookkeeping and one submit per tile happen here; the fetch itself
    still runs on the image or metadata pool, behind the polite windows.
    """
    global _BLANK_TILE_TICK_ARMED
    with _BLANK_TILES_LOCK:
        batch, _BLANK_TILES[:] = _BLANK_TILES[:8], _BLANK_TILES[8:]
    for entry in batch:
        picture = entry["pic"]()
        # A card the flowbox already threw away, or one that filled itself in the
        # meantime: nothing to do, and it leaves the queue for good.
        if picture is None:
            continue
        try:
            if picture.get_paintable() is not None:
                continue
        except Exception:
            continue
        if not _tile_is_on_screen(picture):
            # Another page has the screen: hold the entry, charge nothing, and
            # keep the image permits free for the page the user is looking at.
            _requeue_blank_tile(entry)
            continue
        unresolved = entry.get("unresolved")
        if unresolved is not None:
            item_id, item_type, title, task_gen = unresolved
            if task_gen is not None and not is_image_generation_active(task_gen):
                continue
            # The tile stamps the card it is waiting for; a recycled tile belongs
            # to somebody else now and must not be painted with this poster.
            if getattr(picture, "_popcornbox_blank_item_id", None) != str(item_id):
                continue
            try:
                _meta_fallback_pool.submit(
                    fetch_fallback_poster, item_id, item_type, picture, title,
                    entry["width"], entry["height"], None, task_gen,
                )
            except RuntimeError:
                pass
            # It notes itself again if the lookup still finds nothing.
            continue
        # A poster still inside its failure window: leave it blank for now.
        if _is_image_failed(entry["url"]):
            continue
        if getattr(picture, "_popcornbox_image_url", None) != entry["url"]:
            continue
        try:
            load_image_into_picture(
                entry["url"], picture,
                width=entry["width"], height=entry["height"], crop=entry["crop"],
            )
        except Exception:
            pass
        # A retry that comes back empty notes itself again; adding it here too
        # would let one tile hold two queue slots.
    with _BLANK_TILES_LOCK:
        keep_running = bool(_BLANK_TILES)
        _BLANK_TILE_TICK_ARMED = keep_running
    return keep_running

_IMAGE_GEN_LOCK = threading.Lock()
_IMAGE_GENERATION_ID = 0

def cancel_pending_image_downloads():
    global _IMAGE_GENERATION_ID
    with _IMAGE_GEN_LOCK:
        _IMAGE_GENERATION_ID += 1
    _clear_failed_images()
    _cancel_blank_tile_repairs()

def is_image_generation_active(gen_id):
    with _IMAGE_GEN_LOCK:
        return gen_id == _IMAGE_GENERATION_ID

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

def load_image_into_picture(url, picture_widget, width=None, height=None, on_error=None, is_priority=False, crop=True):
    if not url or not isinstance(url, str): return
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    
    if "media-amazon.com" in url or "ssl-images-amazon.com" in url:
        import re
        url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', url)
    
    setattr(picture_widget, "_popcornbox_image_url", url)

    if not is_priority:
        # Remember what this tile owes and finish it when it is next on screen;
        # while its page is hidden, no download and no decode is spent on it.
        _watch_tile_for_visibility(picture_widget, url, width, height, crop)
        if not _tile_is_on_screen(picture_widget):
            return

    # 1. Fast in-memory pixbuf cache check
    mem_key = (url, width, height, crop)
    cached_pixbuf = _get_memory_cached_pixbuf(mem_key)
    if cached_pixbuf:
        GLib.idle_add(_apply_pixbuf, picture_widget, cached_pixbuf, url)
        return

    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(IMAGE_CACHE_DIR, url_hash)

    if _is_image_failed(url) and not (os.path.exists(cache_file) and os.path.getsize(cache_file) > 0):
        if on_error and getattr(picture_widget, "_popcornbox_image_url", None) == url:
            GLib.idle_add(on_error)
        return

    with _IMAGE_GEN_LOCK:
        task_gen = _IMAGE_GENERATION_ID

    def fetch_image():
        if not is_priority and not is_image_generation_active(task_gen):
            return
        try:
            data = None
            if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
                with open(cache_file, 'rb') as f:
                    data = f.read()
            else:
                if not is_priority and not is_image_generation_active(task_gen):
                    return
                if os.path.exists(cache_file):
                    try: os.remove(cache_file)
                    except Exception: pass

                # Only one worker may download a given URL; the others wait
                # and reuse the cache file it writes.
                owner = False
                inflight = None
                with _IMAGE_INFLIGHT_LOCK:
                    inflight = _IMAGE_INFLIGHT.get(url)
                    if inflight is None:
                        inflight = threading.Event()
                        _IMAGE_INFLIGHT[url] = inflight
                        owner = True

                if not owner:
                    inflight.wait(timeout=8.0)
                    cached_pixbuf = _get_memory_cached_pixbuf(mem_key)
                    if cached_pixbuf:
                        if is_priority or is_image_generation_active(task_gen):
                            GLib.idle_add(_apply_pixbuf, picture_widget, cached_pixbuf, url)
                        return
                    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
                        try:
                            with open(cache_file, 'rb') as f:
                                data = f.read()
                        except OSError:
                            data = None
                else:
                    try:
                        def _polite_image_get(target_url=url):
                            """One paced image GET through the host's window.
                            None means the window was closed, not that the image
                            is missing: the tile stays empty and no failure is
                            recorded, so a later scroll can still fetch it."""
                            from .api import _SSL_CONTEXT, _acquire_image_window, _get_provider_host
                            req = urllib.request.Request(
                                target_url,
                                headers={
                                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                                    'Referer': f"{urllib.parse.urlsplit(target_url).scheme}://{urllib.parse.urlsplit(target_url).netloc}/",
                                },
                            )
                            with _acquire_image_window(
                                _get_provider_host(target_url),
                                is_cancelled=lambda: not is_priority and not is_image_generation_active(task_gen),
                            ) as window_open:
                                if not window_open:
                                    return None  # callers below queue a retry
                                with urllib.request.urlopen(req, timeout=3.5, context=_SSL_CONTEXT) as response:
                                    return response.read()

                        def _cache_image_bytes(image_data):
                            if not image_data:
                                return False
                            temp_file = f"{cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                            try:
                                with open(temp_file, 'wb') as f:
                                    f.write(image_data)
                                os.replace(temp_file, cache_file)
                            except Exception:
                                try:
                                    if os.path.exists(temp_file):
                                        os.remove(temp_file)
                                except Exception:
                                    pass
                            _mark_image_success(url)
                            return True

                        for attempt in range(2):
                            if not is_priority and not is_image_generation_active(task_gen):
                                return
                            try:
                                fetched = _polite_image_get()
                                if fetched is None:
                                    _note_blank_tile(picture_widget, url, width, height, crop)
                                    return
                                data = fetched
                                if data:
                                    _cache_image_bytes(data)
                                break
                            except urllib.error.HTTPError as e:
                                try:
                                    e.close()
                                except Exception:
                                    pass
                                if e.code == 429:
                                    # The CDN told this host to slow down for the whole
                                    # app: cool it so the other tiles stop queueing for it.
                                    from .api import set_provider_cooldown, _retry_after_seconds
                                    set_provider_cooldown(
                                        url,
                                        duration_seconds=_retry_after_seconds(e.headers, default=120, floor=30),
                                        reason="HTTP 429 on an image request",
                                    )
                                    _mark_image_failed(url)
                                    return
                                if attempt == 1:
                                    _mark_image_failed(url)
                                else:
                                    import time
                                    time.sleep(0.3)
                            except (urllib.error.URLError, TimeoutError, OSError) as url_err:
                                # Some image hosts occasionally close the TLS
                                # handshake early. Treat that like a transient
                                # connection failure and retry once instead of
                                # failing the whole thumbnail immediately.
                                if attempt == 1:
                                    # Final attempt failed. A TLS EOF can happen
                                    # even when ordinary DNS appears healthy, so
                                    # try the verified DoH route for that case
                                    # as well as for known poisoned DNS.
                                    try:
                                        from .api import _is_dns_poisoned, _doh_fetch
                                        parsed_host = urllib.parse.urlparse(url).hostname
                                        tls_eof = "UNEXPECTED_EOF" in str(url_err).upper() or "EOF OCCURRED" in str(url_err).upper()
                                        if parsed_host and (tls_eof or _is_dns_poisoned(parsed_host)):
                                            doh_data = _doh_fetch(url, timeout=5.0)
                                            if _cache_image_bytes(doh_data):
                                                data = doh_data
                                    except Exception:
                                        pass
                                    if not data:
                                        parsed_url = urllib.parse.urlsplit(url)
                                        host = (parsed_url.hostname or "").lower()
                                        yts_hosts = ("img.yts.gg", "yts.gg", "yts.mx", "yts.bz")
                                        if host in yts_hosts or any(host.endswith("." + h) for h in yts_hosts):
                                            for alternate_host in yts_hosts:
                                                if alternate_host == host:
                                                    continue
                                                if not is_priority and not is_image_generation_active(task_gen):
                                                    return
                                                alternate_url = urllib.parse.urlunsplit(parsed_url._replace(netloc=alternate_host))
                                                try:
                                                    alternate_data = _polite_image_get(alternate_url)
                                                except (urllib.error.URLError, TimeoutError, OSError):
                                                    try:
                                                        alternate_data = _doh_fetch(alternate_url, timeout=5.0)
                                                    except Exception:
                                                        alternate_data = None
                                                if _cache_image_bytes(alternate_data):
                                                    data = alternate_data
                                                    break
                                    if data:
                                        break
                                    if not data:
                                        _mark_image_failed(url)
                                        raise url_err
                                else:
                                    import time
                                    time.sleep(0.3)
                    finally:
                        with _IMAGE_INFLIGHT_LOCK:
                            if _IMAGE_INFLIGHT.get(url) is inflight:
                                del _IMAGE_INFLIGHT[url]
                        inflight.set()
                
            if not is_priority and not is_image_generation_active(task_gen):
                return

            if not data:
                _note_blank_tile(picture_widget, url, width, height, crop)
                if on_error and getattr(picture_widget, "_popcornbox_image_url", None) == url:
                    GLib.idle_add(on_error)
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
                
            if not is_priority and not is_image_generation_active(task_gen):
                return

            if pixbuf:
                if width and height:
                    orig_w = pixbuf.get_width()
                    orig_h = pixbuf.get_height()
                    if orig_w > 0 and orig_h > 0:
                        if crop:
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
                        else:
                            scale = min(width / orig_w, height / orig_h)
                            new_w = max(1, int(orig_w * scale))
                            new_h = max(1, int(orig_h * scale))
                            pixbuf = pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
                elif width or height:
                    orig_w = pixbuf.get_width()
                    orig_h = pixbuf.get_height()
                    if orig_w > 0 and orig_h > 0:
                        if width:
                            scale = width / orig_w
                            new_w = width
                            new_h = max(1, int(orig_h * scale))
                        else:
                            scale = height / orig_h
                            new_w = max(1, int(orig_w * scale))
                            new_h = height
                        pixbuf = pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)

                _cache_memory_pixbuf(mem_key, pixbuf)

                if is_priority or is_image_generation_active(task_gen):
                    GLib.idle_add(_apply_pixbuf, picture_widget, pixbuf, url)
            else:
                _note_blank_tile(picture_widget, url, width, height, crop)
                if on_error and getattr(picture_widget, "_popcornbox_image_url", None) == url:
                    GLib.idle_add(on_error)
        except Exception as e:
            print(f"Failed to load image {url}: {e}")
            _note_blank_tile(picture_widget, url, width, height, crop)
            if on_error and getattr(picture_widget, "_popcornbox_image_url", None) == url:
                GLib.idle_add(on_error)
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

def _apply_pixbuf(picture_widget, pixbuf, requested_url=None):
    try:
        if not picture_widget or not pixbuf:
            return False
        if requested_url and getattr(picture_widget, "_popcornbox_image_url", None) != requested_url:
            return False
        picture_widget.set_can_shrink(True)
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture_widget.set_paintable(texture)
        # Painted: this tile no longer owes a picture, so the visibility watch
        # has nothing to re-issue when the page is shown again.
        picture_widget._popcornbox_pending_image = None
    except Exception as e:
        print(f"[IMAGE APPLY ERROR] Failed to paint texture: {e}")
    return False


def load_remote_image(first, second=None, width=None, height=None, on_error=None, is_priority=False, crop=True):
    """Helper to load a remote image into a Gtk.Picture widget.
    Supports either (url, picture_widget) or (picture_widget, url) argument order.
    """
    if isinstance(first, str):
        url, picture_widget = first, second
    else:
        picture_widget, url = first, second
    return load_image_into_picture(
        url=url,
        picture_widget=picture_widget,
        width=width,
        height=height,
        on_error=on_error,
        is_priority=is_priority,
        crop=crop,
    )


def format_card_rating(movie_data):
    """Format rating value for display on movie cards (e.g., '★ 7.5')."""
    if not isinstance(movie_data, dict):
        return ""
    val = (
        movie_data.get("rating")
        or movie_data.get("imdbRating")
        or movie_data.get("vote_average")
        or movie_data.get("tmdb_rating")
    )
    if val is None:
        return ""
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("n/a", "none", "null", "0", "0.0"):
        return ""
    try:
        clean = val_str.replace("★", "").replace("⭐", "").strip()
        f_val = float(clean)
        if 0.0 < f_val <= 10.0:
            return f"★ {f_val:.1f}"
        if 10.0 < f_val <= 100.0:
            return f"★ {f_val / 10.0:.1f}"
    except (ValueError, TypeError):
        pass
    return ""


def _dispatch_meta_resolved(callback, title, year, poster, is_adult=False, rating=None):
    """Safely invoke meta-resolved callbacks supporting varying argument counts."""
    if not callable(callback):
        return
    import inspect
    try:
        sig = inspect.signature(callback)
        param_count = len(sig.parameters)
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if has_varargs or param_count >= 5:
            callback(title, year, poster, is_adult, rating)
        elif param_count == 4:
            callback(title, year, poster, is_adult)
        else:
            callback(title, year, poster)
    except Exception:
        try:
            callback(title, year, poster, is_adult, rating)
        except TypeError:
            try:
                callback(title, year, poster, is_adult)
            except TypeError:
                callback(title, year, poster)


def fetch_fallback_poster(
    item_id, item_type, poster_widget, title=None, width=130, height=195,
    on_meta_resolved=None, task_gen=None, is_priority=False,
    expected_year=None, failed_poster_url=None,
):
    if not item_id:
        return
    if task_gen is not None and not is_image_generation_active(task_gen):
        return

    found_title = None
    found_year = None
    found_poster = None
    found_rating = None
    is_adult = False

    failed_host = (urllib.parse.urlsplit(str(failed_poster_url or "")).hostname or "").lower()
    yts_image_domains = ("yts.gg", "yts.mx", "yts.bz")

    def _is_usable_fallback_poster(candidate):
        if not isinstance(candidate, str) or not candidate.startswith(("http://", "https://")):
            return False
        if not failed_poster_url:
            return True
        if candidate == failed_poster_url:
            return False
        candidate_host = (urllib.parse.urlsplit(candidate).hostname or "").lower()
        failed_is_yts = any(failed_host == domain or failed_host.endswith("." + domain) for domain in yts_image_domains)
        candidate_is_yts = any(candidate_host == domain or candidate_host.endswith("." + domain) for domain in yts_image_domains)
        # The YTS mirrors share the same failing image origin for this card, so
        # resolve a TMDB/IMDb poster instead of retrying another YTS hostname.
        return not (failed_is_yts and candidate_is_yts)

    # 0. Check local database cache first (instant SQLite read)
    try:
        existing = database.get_cached_metadata(item_id, item_type)
        if existing and (existing.get("title") or existing.get("medium_cover_image") or existing.get("poster") or existing.get("adult")):
            if existing.get("adult") or existing.get("isAdult"):
                is_adult = True
            cached_title = existing.get("title") or existing.get("name")
            cached_poster = existing.get("medium_cover_image") or existing.get("poster")
            cached_year = str(existing.get("year", "")) if existing.get("year") else ""
            cached_rating = existing.get("rating") or existing.get("imdbRating") or existing.get("vote_average")
            if cached_title and cached_title != "Unknown":
                found_title = cached_title
            if _is_usable_fallback_poster(cached_poster):
                found_poster = cached_poster
            if cached_year:
                found_year = cached_year
            if cached_rating:
                found_rating = cached_rating

            if found_poster and poster_widget and not is_adult:
                GLib.idle_add(load_image_into_picture, found_poster, poster_widget, width, height, None, is_priority)
            if on_meta_resolved and (found_title or found_poster or is_adult or found_rating):
                GLib.idle_add(_dispatch_meta_resolved, on_meta_resolved, found_title, found_year, found_poster, is_adult, found_rating)
            if (found_title and found_poster) or is_adult:
                return
    except Exception:
        pass

    if task_gen is not None and not is_image_generation_active(task_gen):
        return

    from .api import _get_cached_request

    # FIRST FALLBACK: Cinemeta direct (fastest stremio meta provider, ~20-50ms)
    if str(item_id).startswith("tt") and (not found_poster or not found_title or not is_adult):
        try:
            url = f"https://v3-cinemeta.strem.io/meta/{item_type}/{item_id}.json"
            meta_data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS, timeout=1.5)
            if not meta_data:
                alt_url = f"https://cinemeta-live.strem.io/meta/{item_type}/{item_id}.json"
                meta_data = _get_cached_request(alt_url, max_age_hours=METADATA_CACHE_HOURS, timeout=1.5)
            if meta_data and isinstance(meta_data.get("meta"), dict):
                cm = meta_data["meta"]
                from . import api
                if api.is_adult_item(cm):
                    is_adult = True
                elif str(item_id).startswith("tt") and database.is_adult_content_hidden():
                    from . import tmdb_helper
                    t_info = tmdb_helper.fetch_title_language_and_genres(item_id, item_type)
                    if t_info and t_info.get("adult"):
                        is_adult = True
                if not found_poster and _is_usable_fallback_poster(cm.get("poster")):
                    found_poster = cm.get("poster")
                if not found_title and cm.get("name"):
                    found_title = cm.get("name")
                if not found_year and cm.get("year"):
                    found_year = str(cm.get("year"))
                if not found_rating and (cm.get("imdbRating") or cm.get("rating") or cm.get("vote_average")):
                    found_rating = cm.get("imdbRating") or cm.get("rating") or cm.get("vote_average")
        except Exception:
            pass

    if task_gen is not None and not is_image_generation_active(task_gen):
        return

    # SECOND FALLBACK: Full fetch_movie_details (queries installed meta addons)
    if not found_poster or not found_title:
        try:
            from . import api
            details = api.fetch_movie_details(
                item_id,
                item_type,
                title=title,
                use_cache=not bool(failed_poster_url),
                prefer_tmdb=bool(failed_poster_url),
                expected_year=expected_year or found_year,
            )
            if details:
                if api.is_adult_item(details):
                    is_adult = True
                detail_poster = details.get("medium_cover_image") or details.get("poster")
                if not found_poster and _is_usable_fallback_poster(detail_poster):
                    found_poster = detail_poster
                if not found_title:
                    found_title = details.get("title") or details.get("name")
                if not found_year:
                    found_year = str(details.get("year", ""))
                if not found_rating and (details.get("imdbRating") or details.get("rating") or details.get("vote_average")):
                    found_rating = details.get("imdbRating") or details.get("rating") or details.get("vote_average")
        except Exception:
            pass

    # THIRD FALLBACK: Title-based search across IMDb suggestions & TMDB if still no poster
    if not found_poster and (title or found_title):
        search_title = title or found_title
        try:
            from .tmdb_helper import normalize_title_variants, is_similar_title
            from .api import _SSL_CONTEXT
            variants = normalize_title_variants(search_title)
            for v in variants:
                try:
                    clean = re.sub(r'[^a-zA-Z0-9]', '_', v).lower()
                    clean = re.sub(r'_+', '_', clean).strip('_')
                    if not clean:
                        continue
                    first_char = clean[0]
                    imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(clean)}.json"
                    req = urllib.request.Request(imdb_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    with urllib.request.urlopen(req, timeout=2.0, context=_SSL_CONTEXT) as response:
                        s_data = json.loads(response.read().decode('utf-8', errors='ignore'))
                        if s_data and "d" in s_data:
                            for item in s_data["d"]:
                                if item.get("i") and item["i"].get("imageUrl"):
                                    m_name = item.get("l", "")
                                    if is_similar_title(v, m_name):
                                        p_url = item["i"]["imageUrl"]
                                        candidate_poster = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', p_url)
                                        if _is_usable_fallback_poster(candidate_poster):
                                            found_poster = candidate_poster
                                        if not found_title and item.get("l"):
                                            found_title = item["l"]
                                        if not found_year and item.get("y"):
                                            found_year = str(item["y"])
                                        break
                    if found_poster:
                        break
                except Exception:
                    pass
        except Exception:
            pass

    # Save to database cache
    if found_poster or found_title or found_year or found_rating or is_adult:
        try:
            cached = database.get_cached_metadata(item_id, item_type) or {}
            if found_poster:
                cached["medium_cover_image"] = found_poster
                cached["poster"] = found_poster
            if found_title and found_title != "Unknown":
                cached["title"] = found_title
            if found_year:
                cached["year"] = found_year
            if found_rating:
                cached["rating"] = found_rating
            if is_adult:
                cached["adult"] = True
            cached["id"] = item_id
            database.save_cached_metadata(item_id, item_type, cached)
        except Exception:
            pass

    if task_gen is not None and not is_image_generation_active(task_gen):
        return

    if found_poster and poster_widget and not is_adult:
        GLib.idle_add(load_image_into_picture, found_poster, poster_widget, width, height, None, is_priority)
    elif (
        failed_poster_url
        and failed_host in yts_image_domains
        and poster_widget
        and not is_adult
    ):
        # Keep the provider image as a last resort if metadata services are
        # unavailable; this attempt has no error callback, so it cannot loop.
        GLib.idle_add(load_image_into_picture, failed_poster_url, poster_widget, width, height, None, is_priority)

    if on_meta_resolved and (found_title or found_year or found_poster or found_rating or is_adult):
        GLib.idle_add(_dispatch_meta_resolved, on_meta_resolved, found_title, found_year, found_poster, is_adult, found_rating)

    # A card with no poster anywhere in its chain of lookups is the other half of
    # the permanently-blank grid: no URL means no download, so no download can
    # fail and report itself. Queue it as unresolved so the repair tick asks for
    # its metadata again later, when the addon or CDN that had nothing may answer.
    if poster_widget is not None and not found_poster and not is_adult:
        try:
            _note_blank_tile(poster_widget, "", width, height, True,
                             unresolved=(item_id, item_type, title, task_gen))
        except Exception:
            pass

class MovieWidget(Gtk.Box):
    def __init__(self, movie_data, click_callback, on_remove_clicked=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.movie_data = movie_data
        self.click_callback = click_callback
        self.remove_btn_ref = None
        
        self.set_size_request(130, 240)
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
        else:
            icon_container.append(self.overlay)
            self.append(icon_container)
        
        item_id = movie_data.get("imdb_id") or movie_data.get("id")
        if not (isinstance(item_id, str) and re.fullmatch(r"tt\d{7,8}", item_id)):
            # CloudStream catalog cards keep their provider route as the click
            # ID, but some (notably YTS) also provide an IMDb alias. Use that
            # alias for poster recovery so Cinemeta/TMDB can replace an
            # unreachable provider image without changing the card's route.
            aliases = movie_data.get("alias_ids") or []
            imdb_alias = next(
                (str(alias) for alias in aliases if re.fullmatch(r"tt\d{7,8}", str(alias))),
                None,
            )
            if imdb_alias:
                item_id = imdb_alias
        item_type = movie_data.get("type", "movie")
        poster_url = extract_image_url(movie_data)
        if poster_url and "://yts.gg/assets/images/" in poster_url:
            poster_url = poster_url.replace("://yts.gg/assets/images/", "://img.yts.gg/assets/images/")
            movie_data["medium_cover_image"] = poster_url
        provider_poster_url = poster_url
        has_valid_title = bool(
            (movie_data.get("title") and movie_data.get("title") != "Unknown")
            or (movie_data.get("name") and movie_data.get("name") != "Unknown")
        )
        if not movie_data.get("title") and movie_data.get("name"):
            movie_data["title"] = movie_data["name"]

        if movie_data.get("is_cloudstream") or str(movie_data.get("id", "")).startswith("cloudstream:"):
            raw_title = movie_data.get("catalog_title") or movie_data.get("raw_title") or movie_data.get("title") or movie_data.get("name")
            if raw_title:
                movie_data.setdefault("catalog_title", raw_title)
                movie_data.setdefault("raw_title", raw_title)
                from .cloudstream import clean_cloudstream_title
                clean_name, extracted_year = clean_cloudstream_title(raw_title)
                if clean_name:
                    movie_data["title"] = clean_name
                    movie_data["name"] = clean_name
                if extracted_year and not movie_data.get("year"):
                    movie_data["year"] = extracted_year

        # Only check metadata cache if essential info (poster or title) is missing, avoiding main thread SQLite lock contention
        if database.METADATA_HYDRATION_ENABLED and item_id and (not poster_url or not has_valid_title):
            try:
                cached = database.get_cached_metadata(item_id, item_type)
                if cached:
                    c_title = cached.get("title") or cached.get("name")
                    if c_title and c_title != "Unknown" and not has_valid_title:
                        movie_data["title"] = c_title
                        has_valid_title = True
                    c_poster = cached.get("medium_cover_image") or cached.get("poster")
                    if c_poster and "://yts.gg/assets/images/" in c_poster:
                        c_poster = c_poster.replace("://yts.gg/assets/images/", "://img.yts.gg/assets/images/")
                    if c_poster and not poster_url:
                        poster_url = c_poster
                        movie_data["medium_cover_image"] = c_poster
                    c_year = str(cached.get("year", "")) if cached.get("year") else ""
                    if c_year and not movie_data.get("year"):
                        movie_data["year"] = c_year
                    if cached.get("genres") and not movie_data.get("genres"):
                        movie_data["genres"] = list(cached["genres"])
                    c_desc = cached.get("description") or cached.get("overview")
                    if c_desc and not movie_data.get("description"):
                        movie_data["description"] = c_desc
                    if cached.get("imdbRating") and not movie_data.get("imdbRating"):
                        movie_data["imdbRating"] = str(cached["imdbRating"])
                    if cached.get("rating") and not movie_data.get("rating"):
                        movie_data["rating"] = str(cached["rating"])
                    if cached.get("runtime") and not movie_data.get("runtime"):
                        movie_data["runtime"] = str(cached["runtime"])
                    if cached.get("cast") and not movie_data.get("cast"):
                        movie_data["cast"] = list(cached["cast"])
            except Exception:
                pass
            if str(item_id).startswith("tt") and (not has_valid_title or not poster_url):
                try:
                    from . import api
                    api.queue_metadata_hydration(str(item_id), item_type, item_dict=movie_data)
                except Exception:
                    pass

        with _IMAGE_GEN_LOCK:
            task_gen = _IMAGE_GENERATION_ID

        def trigger_fallback():
            try:
                _meta_fallback_pool.submit(
                    fetch_fallback_poster,
                    item_id,
                    item_type,
                    self.poster_image,
                    movie_data.get("title") or movie_data.get("name"),
                    130,
                    195,
                    self._apply_resolved_meta,
                    task_gen,
                    False,
                    movie_data.get("year"),
                    provider_poster_url,
                )
            except RuntimeError:
                pass

        has_title = bool(movie_data.get("title") and movie_data.get("title") != "Unknown")

        if poster_url:
            load_image_into_picture(poster_url, self.poster_image, width=130, height=195, on_error=trigger_fallback)
            if not has_title and item_id:
                trigger_fallback()
        elif item_id:
            trigger_fallback()
            
        title_text = movie_data.get("title") or movie_data.get("name") or "Unknown"
        self.title_label = Gtk.Label(label=title_text)
        self.title_label.set_lines(1)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(1)
        self.title_label.set_hexpand(True)
        self.title_label.set_halign(Gtk.Align.FILL)
        self.title_label.set_xalign(0.0)
        self.title_label.add_css_class("pt-card-title")
        self.append(self.title_label)
        
        year_str = str(movie_data.get("year", "")) or str(movie_data.get("releaseInfo", ""))
        rating_str = format_card_rating(movie_data)

        self.meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.meta_box.set_hexpand(True)
        self.meta_box.set_halign(Gtk.Align.FILL)
        self.meta_box.add_css_class("pt-card-meta-row")

        self.year_label = Gtk.Label(label=year_str)
        self.year_label.set_halign(Gtk.Align.START)
        self.year_label.set_hexpand(True)
        self.year_label.set_xalign(0.0)
        self.year_label.add_css_class("pt-card-year")
        self.year_label.set_visible(bool(year_str))
        self.meta_box.append(self.year_label)

        self.rating_label = Gtk.Label(label=rating_str)
        self.rating_label.set_halign(Gtk.Align.END)
        self.rating_label.set_hexpand(True)
        self.rating_label.set_xalign(1.0)
        self.rating_label.add_css_class("pt-card-rating")
        self.rating_label.set_visible(bool(rating_str))
        self.meta_box.append(self.rating_label)

        self.meta_box.set_visible(bool(year_str or rating_str))
        self.append(self.meta_box)

        cat_name = movie_data.get("catalog_name")
        addon_name = movie_data.get("addon_name")
        tooltip_lines = [title_text]
        raw_catalog_name = movie_data.get("catalog_title") or movie_data.get("raw_title")
        if raw_catalog_name and raw_catalog_name != title_text:
            tooltip_lines.append(f"Release: {raw_catalog_name}")
        sub_meta = []
        if year_str:
            sub_meta.append(f"({year_str})")
        if rating_str:
            sub_meta.append(rating_str)
        if sub_meta:
            tooltip_lines.append(" ".join(sub_meta))
        genres = movie_data.get("genres")
        if genres and isinstance(genres, list):
            tooltip_lines.append(" • ".join(str(g) for g in genres[:3]))
        desc = movie_data.get("description") or movie_data.get("overview")
        if desc and desc not in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD"):
            clean_desc = desc.strip().replace("\r", " ").replace("\n", " ")
            if len(clean_desc) > 180:
                clean_desc = clean_desc[:177] + "..."
            tooltip_lines.append(f"\n{clean_desc}")
        if addon_name and cat_name:
            tooltip_lines.append(f"\nCatalog: {addon_name} · {cat_name}")
        elif addon_name:
            tooltip_lines.append(f"\nAddon: {addon_name}")
        elif cat_name:
            tooltip_lines.append(f"\nCatalog: {cat_name}")
        self.set_tooltip_text("\n".join(tooltip_lines))

        # Keep the callback path available if metadata hydration is enabled.
        def _on_card_hydrated(t, y, p, *args):
            try:
                r = args[1] if len(args) >= 2 else (args[0] if len(args) == 1 else None)
                if not r and item_id:
                    cached_m = database.get_cached_metadata(item_id)
                    if cached_m:
                        r = cached_m.get("imdbRating") or cached_m.get("rating") or cached_m.get("vote_average")
                self._apply_resolved_meta(t, y, p, rating=r)
            except Exception:
                pass
            return False

        if database.METADATA_HYDRATION_ENABLED and item_id:
            try:
                from . import api
                api.register_hydration_listener(item_id, _on_card_hydrated)
                self._hydration_item_id = item_id
                self._hydration_cb = _on_card_hydrated
            except Exception:
                pass
        if isinstance(self.movie_data, dict) and "_on_hydrated" in self.movie_data:
            self.movie_data.pop("_on_hydrated", None)

    def do_unroot(self):
        if getattr(self, "_hydration_item_id", None) and getattr(self, "_hydration_cb", None):
            try:
                from . import api
                api.unregister_hydration_listener(self._hydration_item_id, self._hydration_cb)
            except Exception:
                pass
            self._hydration_item_id = None
            self._hydration_cb = None
        Gtk.Box.do_unroot(self)

    def _apply_resolved_meta(self, title, year, poster_url, is_adult=False, rating=None, *args, **kwargs):
        try:
            from . import api
            if is_adult or (database.is_adult_content_hidden() and api.is_adult_item(self.movie_data)):
                self.set_visible(False)
                return
            if title and title != "Unknown" and (not self.movie_data.get("title") or self.movie_data.get("title") == "Unknown"):
                self.movie_data["title"] = title
                if hasattr(self, "title_label") and self.title_label:
                    self.title_label.set_label(title)
            if year and (not self.movie_data.get("year") or self.movie_data.get("year") != str(year)):
                self.movie_data["year"] = str(year)
                if hasattr(self, "year_label") and self.year_label:
                    self.year_label.set_label(str(year))
                    self.year_label.set_visible(True)
                if hasattr(self, "meta_box") and self.meta_box:
                    self.meta_box.set_visible(True)
            if rating:
                formatted_rating = format_card_rating({"rating": rating})
                if formatted_rating:
                    self.movie_data["rating"] = rating
                    if hasattr(self, "rating_label") and self.rating_label:
                        self.rating_label.set_label(formatted_rating)
                        self.rating_label.set_visible(True)
                    if hasattr(self, "meta_box") and self.meta_box:
                        self.meta_box.set_visible(True)
            # A card can also arrive with a poster URL that simply does not work.
            # If the tile is still blank and fallback found a different picture for
            # the same title, that is the fix the user is waiting on -- paint it.
            # A tile that already shows something is never repainted.
            shown_url = self.movie_data.get("medium_cover_image") or self.movie_data.get("poster")
            tile_is_blank = False
            try:
                tile_is_blank = self.poster_image.get_paintable() is None
            except Exception:
                pass
            if poster_url and poster_url != shown_url and (not shown_url or tile_is_blank):
                self.movie_data["medium_cover_image"] = poster_url
                self.movie_data["poster"] = poster_url
                if hasattr(self, "poster_image") and self.poster_image:
                    load_image_into_picture(poster_url, self.poster_image, width=130, height=195)

            cat_name = self.movie_data.get("catalog_name")
            addon_name = self.movie_data.get("addon_name")
            t_lines = [self.movie_data.get("title", "Unknown")]
            y_val = self.movie_data.get("year")
            r_val = format_card_rating(self.movie_data)
            sub_m = []
            if y_val:
                sub_m.append(f"({y_val})")
            if r_val:
                sub_m.append(r_val)
            if sub_m:
                t_lines.append(" ".join(sub_m))
            genres = self.movie_data.get("genres")
            if genres and isinstance(genres, list):
                t_lines.append(" • ".join(str(g) for g in genres[:3]))
            desc = self.movie_data.get("description") or self.movie_data.get("overview")
            if desc and desc not in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD"):
                clean_desc = desc.strip().replace("\r", " ").replace("\n", " ")
                if len(clean_desc) > 180:
                    clean_desc = clean_desc[:177] + "..."
                t_lines.append(f"\n{clean_desc}")
            if addon_name and cat_name:
                t_lines.append(f"\nCatalog: {addon_name} · {cat_name}")
            elif addon_name:
                t_lines.append(f"\nAddon: {addon_name}")
            elif cat_name:
                t_lines.append(f"\nCatalog: {cat_name}")
            self.set_tooltip_text("\n".join(t_lines))
        except Exception:
            pass

    def _on_card_released(self, gesture, n_press, x, y):
        if n_press > 1:
            return
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


class ContinueWatchingWidget(Gtk.Box):
    def __init__(self, item_data, click_callback, on_remove_clicked=None, on_play_clicked=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.item_data = item_data
        self.click_callback = click_callback
        self.on_play_clicked = on_play_clicked
        self.remove_btn_ref = None
        self.play_btn_ref = None
        
        self.set_size_request(130, 240)
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.CENTER)
        self.add_css_class("pt-card")
        self.add_css_class("continue-card")
        
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
        
        # Center Play Button Overlay
        play_btn = Gtk.Button()
        play_btn.set_can_focus(False)
        play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        play_icon.set_pixel_size(20)
        play_btn.set_child(play_icon)
        play_btn.add_css_class("continue-play-circle")
        play_btn.add_css_class("continue-play-btn")
        play_btn.add_css_class("circular")
        play_btn.add_css_class("osd")
        play_btn.set_halign(Gtk.Align.CENTER)
        play_btn.set_valign(Gtk.Align.CENTER)
        play_btn.set_tooltip_text("Play")
        if on_play_clicked:
            play_btn.connect("clicked", lambda btn: on_play_clicked(self.item_data))
        self.overlay.add_overlay(play_btn)
        self.play_btn_ref = play_btn
        
        # Progress Bar at bottom of poster
        progress_val = float(item_data.get("progress") or 0.0)
        if progress_val <= 0:
            pos = float(item_data.get("position") or 0.0)
            dur = float(item_data.get("duration") or 0.0)
            if dur > 0:
                progress_val = min(1.0, max(0.0, pos / dur))
        
        pbar = Gtk.ProgressBar()
        pbar.set_fraction(max(0.05, min(1.0, progress_val)))
        pbar.set_valign(Gtk.Align.END)
        pbar.set_halign(Gtk.Align.FILL)
        pbar.add_css_class("continue-progress-bar")
        self.overlay.add_overlay(pbar)
        
        # Remove button on hover
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
            remove_btn.connect("clicked", lambda btn: on_remove_clicked(self.item_data, self))
            self.overlay.add_overlay(remove_btn)
            self.remove_btn_ref = remove_btn
            
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *args: remove_btn.set_visible(True))
            hover.connect("leave", lambda *args: remove_btn.set_visible(False))
            self.add_controller(hover)
        
        icon_container.append(self.overlay)
        self.append(icon_container)
        
        # Poster image loading
        item_id = item_data.get("imdb_id") or item_data.get("id")
        item_type = item_data.get("type", "movie")
        poster_url = item_data.get("medium_cover_image") or item_data.get("poster") or extract_image_url(item_data)

        # Check metadata cache only if vital fields are missing
        need_meta = not poster_url or not item_data.get("title") or item_data.get("title") == "Unknown" or not item_data.get("year")
        if item_id and need_meta:
            try:
                cached = database.get_cached_metadata(item_id, item_type)
                if cached:
                    c_title = cached.get("title") or cached.get("name")
                    if c_title and c_title != "Unknown" and (not item_data.get("title") or item_data.get("title") == "Unknown"):
                        item_data["title"] = c_title
                    c_poster = cached.get("medium_cover_image") or cached.get("poster")
                    if c_poster and not poster_url:
                        poster_url = c_poster
                        item_data["medium_cover_image"] = c_poster
                    c_year = str(cached.get("year", "")) if cached.get("year") else ""
                    if c_year and not item_data.get("year"):
                        item_data["year"] = c_year
            except Exception:
                pass

        with _IMAGE_GEN_LOCK:
            task_gen = _IMAGE_GENERATION_ID

        def trigger_fallback():
            try:
                _meta_fallback_pool.submit(
                    fetch_fallback_poster,
                    item_id,
                    item_type,
                    self.poster_image,
                    item_data.get("title") or item_data.get("name"),
                    130,
                    195,
                    self._apply_resolved_meta,
                    task_gen
                )
            except RuntimeError:
                pass

        has_title = bool(item_data.get("title") and item_data.get("title") != "Unknown")

        if poster_url:
            load_image_into_picture(poster_url, self.poster_image, width=130, height=195, on_error=trigger_fallback)
            if not has_title and item_id:
                trigger_fallback()
        elif item_id:
            trigger_fallback()
            
        # Title
        title_text = item_data.get("title") or item_data.get("name") or "Unknown"
        self.title_label = Gtk.Label(label=title_text)
        self.title_label.set_lines(1)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(1)
        self.title_label.set_hexpand(True)
        self.title_label.set_halign(Gtk.Align.FILL)
        self.title_label.set_xalign(0.0)
        self.title_label.add_css_class("pt-card-title")
        self.append(self.title_label)
        
        # Subtitle (Season/Episode or Remaining Time)
        sub_text = ""
        season = item_data.get("season")
        episode = item_data.get("episode")
        if season is not None and episode is not None:
            sub_text = f"S{season}:E{episode}"
        else:
            pos = float(item_data.get("position") or 0.0)
            dur = float(item_data.get("duration") or 0.0)
            if dur > pos and pos > 0:
                rem_mins = int((dur - pos) / 60)
                if rem_mins > 0:
                    sub_text = f"{rem_mins}m left"
            if not sub_text:
                sub_text = str(item_data.get("year", "")) or ""
                
        if sub_text:
            sub_label = Gtk.Label(label=sub_text)
            sub_label.set_halign(Gtk.Align.START)
            sub_label.add_css_class("pt-card-year")
            self.append(sub_label)

    def _apply_resolved_meta(self, title, year, poster_url, is_adult=False, *args, **kwargs):
        try:
            from . import database, api
            if is_adult or (database.is_adult_content_hidden() and api.is_adult_item(self.item_data)):
                self.set_visible(False)
                return
            if title and title != "Unknown" and (not self.item_data.get("title") or self.item_data.get("title") == "Unknown"):
                self.item_data["title"] = title
                if hasattr(self, "title_label") and self.title_label:
                    self.title_label.set_label(title)
            if poster_url:
                self.item_data["medium_cover_image"] = poster_url
                self.item_data["poster"] = poster_url
        except Exception:
            pass

    def _on_card_released(self, gesture, n_press, x, y):
        if n_press > 1:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        target = picked
        while target is not None:
            if target == self.remove_btn_ref or target == self.play_btn_ref:
                return
            if target == self:
                break
            target = target.get_parent()
            
        if self.click_callback:
            self.click_callback(self.item_data)

