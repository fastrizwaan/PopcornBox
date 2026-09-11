import json
import urllib.request
import urllib.parse
import urllib.error
import os
import time
import hashlib
import logging
import ssl
import gzip
import zlib
import base64
from . import database
from .tmdb_helper import resolve_to_imdb_id, resolve_all_provider_ids
import concurrent.futures
import threading
import re

def _create_ssl_context():
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except Exception:
        return None

_SSL_CONTEXT = _create_ssl_context()


DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.bittorrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://uploads.gamebase.info:6969/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "udp://tracker.1337x.org:80/announce",
    "udp://tracker.pomf.se:80/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.srv00.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "udp://tracker.dler.com:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://evan.im:6969/announce",
    "udp://bittorrent-tracker.e-n-c-r-y-p-t.net:1337/announce",
    "udp://tracker.opentorrent.top:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.ilibr.org:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.dump.cl:6969/announce",
    "http://tracker.waaa.moe:6969/announce",
    "udp://tracker.bluefrog.pw:2710/announce",
    "udp://tracker.aruku.ovh:8081/announce",
    "udp://anime-tracker.aruku.kro.kr:8081/announce",
    "udp://mail.segso.net:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
    "https://tracker.leechshield.link:443/announce",
    "http://wegkxfcivgx.ydns.eu:80/announce",
    "https://t.213891.xyz:443/announce",
    "udp://tracker.gmi.gd:6969/announce",
    "udp://tracker.teambelgium.net:6969/announce",
    "http://tracker.xn--djrq4gl4hvoi.top:80/announce",
    "http://tracker.dhitechnical.com:6969/announce",
    "udp://tracker.wildkat.net:6969/announce",
    "udp://torrentclub.online:1984/announce",
    "http://bt1.archive.org:6969/announce",
    "http://bt2.archive.org:6969/announce",
    "udp://t.overflow.biz:6969/announce",
    "http://tracker.renfei.net:8080/announce",
    "https://tracker.zhuqiy.com:443/announce",
    "udp://open.stealth.si:80/announce"
]

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "popcorn-box")
else:
    BASE_DIR = os.path.expanduser("~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box")
CACHE_DIR = os.path.join(BASE_DIR, 'api')
os.makedirs(CACHE_DIR, exist_ok=True)

_MEM_CACHE = {}
_MEM_CACHE_LOCK = threading.Lock()
_MEM_CACHE_MAX_ITEMS = 1000

_OFFLINE_HOSTS = {}
_OFFLINE_HOSTS_LOCK = threading.Lock()
_OFFLINE_HOST_COOLDOWN = 300 # 5 minutes circuit breaker for offline addons

def _get_cached_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0):
    if not url:
        return None
    now = time.time()
    # 1. Fast in-memory cache check
    with _MEM_CACHE_LOCK:
        if url in _MEM_CACHE:
            cached_data, cached_time = _MEM_CACHE[url]
            if (now - cached_time) < (max_age_hours * 3600):
                return cached_data
            else:
                del _MEM_CACHE[url]

    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, url_hash)
    
    if headers is None:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    # 2. Check if disk cache exists and is fresh
    if os.path.exists(cache_file):
        age_hours = (now - os.path.getmtime(cache_file)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("meta") == []:
                        try:
                            os.remove(cache_file)
                        except Exception:
                            pass
                        data = None
                    if data is not None:
                        with _MEM_CACHE_LOCK:
                            if len(_MEM_CACHE) > _MEM_CACHE_MAX_ITEMS:
                                _MEM_CACHE.clear()
                            _MEM_CACHE[url] = (data, now)
                        return data
            except Exception as e:
                logging.debug(f"Cache corrupted, falling back to fetch: {e}")
                
    if cache_only:
        return None

    # Circuit breaker: fast-fail if host is known to be offline/down
    parsed_host = ""
    try:
        parsed_host = urllib.parse.urlparse(url).netloc
    except Exception:
        pass

    if parsed_host:
        with _OFFLINE_HOSTS_LOCK:
            last_fail = _OFFLINE_HOSTS.get(parsed_host)
            if last_fail and (now - last_fail < _OFFLINE_HOST_COOLDOWN):
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as f:
                            return json.load(f)
                    except Exception:
                        pass
                return None
        
    # 3. Fetch from network
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
            data_str = response.read().decode('utf-8')
        if not data_str or not data_str.strip():
            return None
        data = json.loads(data_str)
        if isinstance(data, dict) and data.get("meta") == []:
            return None
        
        # Host is healthy: remove from offline list
        if parsed_host:
            with _OFFLINE_HOSTS_LOCK:
                _OFFLINE_HOSTS.pop(parsed_host, None)

        # Save to memory cache
        with _MEM_CACHE_LOCK:
            if len(_MEM_CACHE) > _MEM_CACHE_MAX_ITEMS:
                _MEM_CACHE.clear()
            _MEM_CACHE[url] = (data, now)

        # Save to disk cache atomically (temp file + rename)
        try:
            temp_file = f"{cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(data_str)
            os.replace(temp_file, cache_file)
        except Exception:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass
        return data
    except urllib.error.HTTPError as e:
        if parsed_host:
            with _OFFLINE_HOSTS_LOCK:
                _OFFLINE_HOSTS[parsed_host] = time.time()
        logging.debug(f"HTTP Error {e.code} fetching from {url}")
        try:
            e.close()
        except Exception:
            pass
    except urllib.error.URLError as e:
        if parsed_host:
            with _OFFLINE_HOSTS_LOCK:
                _OFFLINE_HOSTS[parsed_host] = time.time()
        logging.debug(f"URL/SSL Error fetching from {url}: {e.reason}")
    except json.JSONDecodeError as e:
        logging.debug(f"JSON decode error from {url}: {e}")
    except Exception as e:
        if parsed_host:
            with _OFFLINE_HOSTS_LOCK:
                _OFFLINE_HOSTS[parsed_host] = time.time()
        logging.debug(f"Error fetching items from {url}: {e}")
        
    # Return stale cache if network fails
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                with _MEM_CACHE_LOCK:
                    _MEM_CACHE[url] = (data, now)
                return data
        except Exception as e:
            logging.debug(f"Failed to read stale cache: {e}")
    return None

def fetch_genre_counts(media_type="movie"):
    return {}

def is_type_match(type1, type2):
    if not type1 or not type2:
        return False
    t1 = str(type1).lower().strip()
    t2 = str(type2).lower().strip()
    if t1 == t2:
        return True
    series_group = {"series", "tvshow", "tv_series"}
    if t1 in series_group and t2 in series_group:
        return True
    tv_group = {"tv", "channel", "tvchannel"}
    if t1 in tv_group and t2 in tv_group:
        return True
    music_group = {"music", "radio"}
    if t1 in music_group and t2 in music_group:
        return True
    return False

ADULT_GENRES_TAGS = {
    "adult", "erotica", "ecchi", "hentai", "softcore", "pinku eiga",
    "pink film", "sex", "sexuality", "porn", "pornography", "nudity",
    "explicit nudity", "nsfw", "sploitation", "bdsm", "sm", "r18", "r-18"
}

ADULT_RATINGS = {
    "18", "18+", "R18", "R-18", "R18+", "NC-17", "XXX", "X", "TV-MA",
    "RX", "AV", "ADULT", "PORN", "R21", "OVER 18", "A"
}

ADULT_KEYWORD_REGEX = re.compile(
    r'\b(18\+|xxx|hentai|ecchi|erotica|softcore|pinku eiga|pink film|sex video|jav|adult movie|nude|nudity|hatsuj[ôo]|dirty couple exchange|horny couple|groper train|futariecci|futari ecchi|incha couple)\b',
    re.IGNORECASE
)

def is_adult_item(item):
    if not item or not isinstance(item, dict):
        return False

    if item.get("adult") is True or item.get("isAdult") is True:
        return True

    rating = str(item.get("certification") or item.get("contentRating") or item.get("rating") or "").strip().upper()
    if rating in ADULT_RATINGS or any(r in rating for r in ["R18", "NC-17", "XXX", "18+"]):
        return True

    genres = item.get("genres") or item.get("genre") or []
    if isinstance(genres, str):
        genre_list = [g.strip().lower() for g in genres.split(",")]
    elif isinstance(genres, list):
        genre_list = [str(g).strip().lower() for g in genres]
    else:
        genre_list = []

    for g in genre_list:
        if g in ADULT_GENRES_TAGS:
            return True

    title = str(item.get("name") or item.get("title") or "")
    desc = str(item.get("description") or item.get("overview") or "")

    if ADULT_KEYWORD_REGEX.search(title) or ADULT_KEYWORD_REGEX.search(desc):
        return True

    genres_str = " ".join(genre_list)
    if ADULT_KEYWORD_REGEX.search(genres_str):
        return True

    return False

# Session-level addon circuit breaker.
# Tracks consecutive timeout/error failures per manifest URL for this app session.
# An addon is skipped once it has failed _ADDON_FAIL_THRESHOLD times in a row.
# Status is NOT persisted to disk and resets on app restart.
_ADDON_SESSION_FAILURES = {}   # manifest_url -> consecutive_fail_count
_ADDON_SESSION_BLOCKED = set() # manifest_urls blocked for this session
_ADDON_ONLINE_LOCK = threading.Lock()
# Also keep the legacy dict for the addon manager UI compatibility
_ADDON_ONLINE_STATUS = {}

_ADDON_FAIL_THRESHOLD = 2  # Block after this many consecutive timeout/connection errors

def set_addon_online_status(manifest_url, is_online):
    """Called by addon manager UI to override session status."""
    if not manifest_url:
        return
    with _ADDON_ONLINE_LOCK:
        _ADDON_ONLINE_STATUS[manifest_url] = is_online
        if is_online:
            # UI explicitly says online — clear session block
            _ADDON_SESSION_BLOCKED.discard(manifest_url)
            _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        else:
            _ADDON_SESSION_BLOCKED.add(manifest_url)
    invalidate_catalogs_cache()

def _record_addon_failure(manifest_url):
    """Record a network failure for an addon. Blocks it after threshold failures."""
    if not manifest_url or manifest_url.startswith("builtin:"):
        return
    with _ADDON_ONLINE_LOCK:
        count = _ADDON_SESSION_FAILURES.get(manifest_url, 0) + 1
        _ADDON_SESSION_FAILURES[manifest_url] = count
        if count >= _ADDON_FAIL_THRESHOLD:
            _ADDON_SESSION_BLOCKED.add(manifest_url)
            logging.info(f"[Addon] Blocked for this session after {count} failures: {manifest_url}")

def _record_addon_success(manifest_url):
    """Clear failure count on a successful addon response."""
    if not manifest_url or manifest_url.startswith("builtin:"):
        return
    with _ADDON_ONLINE_LOCK:
        _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        _ADDON_SESSION_BLOCKED.discard(manifest_url)

def reset_addon_session_status(manifest_url=None):
    """Reset session block for a specific addon (or all addons) — called on manual reload."""
    with _ADDON_ONLINE_LOCK:
        if manifest_url:
            _ADDON_SESSION_BLOCKED.discard(manifest_url)
            _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        else:
            _ADDON_SESSION_BLOCKED.clear()
            _ADDON_SESSION_FAILURES.clear()
    invalidate_catalogs_cache()

def is_addon_online(manifest_url):
    if not manifest_url or manifest_url.startswith("builtin:"):
        return True
    with _ADDON_ONLINE_LOCK:
        # Check explicit UI override first
        if manifest_url in _ADDON_ONLINE_STATUS and not _ADDON_ONLINE_STATUS[manifest_url]:
            return False
        # Check session circuit breaker
        if manifest_url in _ADDON_SESSION_BLOCKED:
            return False
    return True

def get_session_blocked_addons():
    """Return set of manifest URLs blocked for this session (for UI display)."""
    with _ADDON_ONLINE_LOCK:
        return set(_ADDON_SESSION_BLOCKED)

def get_addon_catalogs(addon, cache_only=False):
    """Return the catalogs for an addon. If catalogs is empty, attempts to fetch
    from cached/remote manifest and persists to database."""
    if not addon or not isinstance(addon, dict):
        return []
    catalogs = addon.get("catalogs")
    if catalogs:
        return catalogs
        
    m_url = addon.get("manifest_url", "")
    if not m_url or m_url.startswith("builtin:"):
        return []
        
    try:
        manifest_data = _get_cached_request(m_url, max_age_hours=168, cache_only=cache_only, timeout=3.5)
        if manifest_data and isinstance(manifest_data.get("catalogs"), list) and manifest_data["catalogs"]:
            found_cats = manifest_data["catalogs"]
            addon["catalogs"] = found_cats
            try:
                from . import database
                database.update_addon_catalogs(m_url, found_cats)
            except Exception:
                pass
            return found_cats
    except Exception:
        pass
        
    return []

def addon_has_resource(addon, resource_name, media_type=None, item_id=None):
    """
    Check if an addon supports a given resource ('catalog', 'meta', 'stream', 'subtitles'),
    optionally checking for compatibility with media_type ('movie', 'series', 'anime', 'tv', etc.)
    and item_id (e.g. 'tt...', 'kitsu:...', 'tmdb:...', 'dsf:...', 'iptv:...').
    """
    if not isinstance(addon, dict):
        return False
    if not addon.get("enabled", True):
        return False
        
    manifest_url = addon.get("manifest_url", "")
    if manifest_url and not is_addon_online(manifest_url):
        return False
        
    addon_id = str(addon.get("id", "")).lower()
    
    # Specific known built-in / default addon behavior overrides
    if addon_id == "cinemeta" or "cinemeta" in manifest_url.lower():
        if resource_name in ["catalog", "meta"]:
            if media_type and not is_type_match(media_type, "movie") and not is_type_match(media_type, "series"):
                return False
            if item_id and not str(item_id).startswith("tt"):
                return False
            return True
        return False  # Cinemeta does NOT provide streams or subtitles
        
    if addon_id == "anime-kitsu" or "anime-kitsu" in manifest_url.lower():
        if resource_name in ["catalog", "meta"]:
            if media_type and not (is_type_match(media_type, "anime") or is_type_match(media_type, "series") or is_type_match(media_type, "movie")):
                return False
            if item_id and resource_name == "meta" and not str(item_id).startswith("kitsu:"):
                return False
            return True
        return False  # Anime Kitsu does NOT provide streams or subtitles

    if addon_id == "local.iptv-org" or "iptv-org" in manifest_url.lower():
        if media_type and not is_type_match(media_type, "tv"):
            return False
        return True

    # Check resources field in manifest
    resources = addon.get("resources")
    
    # If resources is missing or None, infer based on catalogs or defaults
    if resources is None:
        if resource_name == "catalog" and get_addon_catalogs(addon, cache_only=True):
            resources = ["catalog"]
        elif resource_name == "stream":
            resources = ["stream"]
        else:
            resources = ["stream", "meta", "catalog"]

    has_res = False
    for r in resources:
        if isinstance(r, str):
            if r.lower() == resource_name.lower():
                has_res = True
                break
        elif isinstance(r, dict):
            if str(r.get("name", "")).lower() == resource_name.lower():
                # Check resource-level types
                r_types = r.get("types")
                if r_types is not None and media_type:
                    type_ok = any(is_type_match(t, media_type) for t in r_types)
                    if not type_ok and media_type == "anime":
                        type_ok = any("anime" in str(t).lower() for t in r_types)
                    if not type_ok:
                        continue  # This resource entry does not match media_type
                        
                # Check resource-level idPrefixes
                r_prefixes = r.get("idPrefixes")
                if r_prefixes is not None and item_id:
                    if not any(str(item_id).startswith(p) for p in r_prefixes):
                        continue  # This resource entry does not match item_id prefix
                        
                has_res = True
                break

    if not has_res:
        return False

    # Check top-level addon types
    addon_types = addon.get("types")
    if addon_types is not None and media_type:
        type_match = any(is_type_match(t, media_type) for t in addon_types)
        if not type_match and media_type == "anime":
            type_match = any("anime" in str(t).lower() for t in addon_types) or any("anime" in str(c.get("type", "")).lower() for c in get_addon_catalogs(addon, cache_only=True))
        if not type_match:
            # Fallback check catalogs if catalog resource
            if resource_name == "catalog":
                type_match = any(is_type_match(c.get("type"), media_type) for c in get_addon_catalogs(addon, cache_only=True))
            if not type_match:
                return False

    # Check top-level addon idPrefixes
    addon_prefixes = addon.get("idPrefixes")
    if addon_prefixes is not None and item_id:
        if not any(str(item_id).startswith(p) for p in addon_prefixes):
            return False

    return True

def has_meta_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports metadata ('meta') resource for this item."""
    return addon_has_resource(addon, "meta", media_type=media_type, item_id=item_id)

def has_stream_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports stream ('stream') resource for this item."""
    return addon_has_resource(addon, "stream", media_type=media_type, item_id=item_id)

def get_stream_addons(media_type=None, item_id=None):
    """Return all enabled addons that provide streams/torrents for this media type and item."""
    from . import database
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    return [
        a for a in addons 
        if not a.get("manifest_url", "").startswith("builtin://") 
        and has_stream_resource(a, media_type=media_type, item_id=item_id)
    ]

def has_stream_addons(media_type=None, item_id=None):
    """Return True if there is at least one enabled stream/torrent addon for this media type and item."""
    from . import database
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    for a in addons:
        if not a.get("manifest_url", "").startswith("builtin://"):
            if item_id and str(item_id).startswith("tt"):
                if has_stream_resource(a, media_type=media_type, item_id=item_id):
                    return True
            elif has_stream_resource(a, media_type=media_type):
                return True
    return False

def has_catalog_resource(addon, media_type=None):
    """Return True if the addon supports catalog ('catalog') resource for this media type."""
    return addon_has_resource(addon, "catalog", media_type=media_type)

def has_subtitles_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports subtitles ('subtitles') resource for this item."""
    return addon_has_resource(addon, "subtitles", media_type=media_type, item_id=item_id)

def is_catalog_browsable(cat):
    """
    Return True if the catalog can be browsed directly without search or other required parameters.
    Catalogs with isRequired=True for 'search' (or in extraRequired) are search-only catalogs.
    """
    if not isinstance(cat, dict):
        return False
    extra_req = cat.get("extraRequired") or []
    if "search" in extra_req:
        return False
    extra = cat.get("extra") or []
    for ex in extra:
        if isinstance(ex, dict):
            name = ex.get("name", "")
            is_req = ex.get("isRequired", False)
            if is_req:
                if name == "search":
                    return False
                if name != "genre" and not ex.get("options"):
                    return False
    return True

_AVAILABLE_CATALOGS_CACHE = {}
_AVAILABLE_CATALOGS_LOCK = threading.Lock()

def invalidate_catalogs_cache():
    with _AVAILABLE_CATALOGS_LOCK:
        _AVAILABLE_CATALOGS_CACHE.clear()

def get_available_catalogs(c_type="movie"):
    with _AVAILABLE_CATALOGS_LOCK:
        if c_type in _AVAILABLE_CATALOGS_CACHE:
            return list(_AVAILABLE_CATALOGS_CACHE[c_type])

    from . import database
    catalogs = []
    addons = [a for a in database.get_addons() if has_catalog_resource(a, media_type=c_type)]
    
    for addon in addons:
        addon_name = addon.get("name", "Unknown Addon")
        manifest_url = addon.get("manifest_url", "")
        if not manifest_url or manifest_url.startswith("builtin:"):
            continue
            
        base_url = manifest_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"): base_url += "/"
            
        addon_catalogs = get_addon_catalogs(addon, cache_only=False)
        for cat in addon_catalogs:
            if not is_catalog_browsable(cat):
                continue

            cat_type = cat.get("type")
            cat_name = cat.get("name") or ""
            cat_id = cat.get("id", "")

            matched = is_type_match(cat_type, c_type)
            if not matched and c_type == "anime":
                if "anime" in str(cat_type).lower() or "anime" in cat_name.lower() or "anime" in cat_id.lower() or "anime" in addon_name.lower():
                    matched = True
            elif not matched and c_type == "movie":
                if "movie" in str(cat_type).lower() or "movie" in cat_name.lower() or "movie" in str(cat_id).lower() or "film" in cat_name.lower():
                    matched = True
            elif not matched and c_type == "series":
                if "series" in str(cat_type).lower() or "series" in cat_name.lower() or "series" in str(cat_id).lower() or "tv" in str(cat_id).lower():
                    matched = True

            if matched:
                genres = []
                extra = cat.get("extra") or []
                for ex in extra:
                    if isinstance(ex, dict) and ex.get("name") == "genre":
                        genres = ex.get("options", [])
                        
                if not cat_name or cat_name.lower() == "catalog":
                    if str(cat_id).lower() in ["tpbctlg-movies", "tpbctlg-series"]:
                        display_name = f"{addon_name} - Popular"
                    else:
                        display_name = f"{addon_name} - {cat_id}"
                else:
                    display_name = f"{addon_name} - {cat_name}" if addon_name.lower() not in cat_name.lower() else cat_name
                
                catalogs.append({
                    "addon_name": addon_name,
                    "manifest_url": manifest_url,
                    "base_url": base_url,
                    "catalog_id": cat.get("id"),
                    "catalog_name": cat_name,
                    "display_name": display_name,
                    "genres": genres,
                    "type": cat_type or c_type
                })
    if c_type == "anime":
        def get_anime_priority(cat):
            n = (str(cat.get("addon_name")) + " " + str(cat.get("display_name"))).lower()
            if "animestream" in n: return 0
            if "anime kitsu" in n or "kitsu" in n: return 1
            if "onlyanimes" in n: return 2
            if "anime" in n: return 3
            return 4
        catalogs.sort(key=get_anime_priority)

    with _AVAILABLE_CATALOGS_LOCK:
        _AVAILABLE_CATALOGS_CACHE[c_type] = catalogs

    return catalogs


def _get_search_catalogs_for_addon(addon, c_type, cache_only=False):
    if not has_catalog_resource(addon, media_type=c_type):
        return []
        
    catalogs = get_addon_catalogs(addon, cache_only=cache_only)
    if not catalogs:
        return []

    search_cats = []
    for cat in catalogs:
        cat_type = cat.get("type")
        if cat_type and not is_type_match(cat_type, c_type):
            if not (c_type == "anime" and "anime" in str(cat_type).lower()):
                continue
            
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        extra = cat.get("extra") or []
        extra_sup = cat.get("extraSupported") or []
        extra_req = cat.get("extraRequired") or []
        
        is_search = False
        if addon.get("id") == "cinemeta" and cat_id == "top":
            is_search = True
        elif "search" in str(cat_id).lower() or "search" in str(cat_name).lower():
            is_search = True
        elif "search" in extra_sup or "search" in extra_req:
            is_search = True
        else:
            for ex in extra:
                if ex == "search":
                    is_search = True
                    break
                elif isinstance(ex, dict) and ex.get("name") == "search":
                    is_search = True
                    break
                    
        if is_search and cat_id not in search_cats:
            search_cats.append(cat_id)
            
    if not search_cats and addon.get("id") in ["org.stremio.tmdb", "org.cinetorrent", "com.stremio.indianStreamCatalog"]:
        for cat in catalogs:
            if cat.get("type") == c_type or is_type_match(cat.get("type"), c_type):
                search_cats.append(cat.get("id"))
                
    return search_cats


def fetch_items(media_type="movie", query="", genre="", catalog_id="top", catalog_url=None, limit=50, page=1, cache_only=False, on_item_found=None, target_manifest_url=None, target_catalog_id=None, is_cancelled=None):
    c_type = "series" if media_type == "series" else media_type
    skip = (page - 1) * 50

    if query:
        import concurrent.futures
        items = []
        seen_ids = set()
        seen_titles = {}

        def fetch_addon_search(addon):
            if is_cancelled and is_cancelled():
                return []
            if not addon.get("enabled", True): return []
            m_url = addon.get("manifest_url", "")
            if not m_url or m_url.startswith("builtin:"): return []
            
            if target_manifest_url and m_url != target_manifest_url:
                return []
                
            if not has_catalog_resource(addon, media_type=c_type):
                return []
            
            base_url = m_url.rsplit("manifest.json", 1)[0]
            if not base_url.endswith("/"): base_url += "/"
            
            if target_catalog_id:
                search_catalogs = [target_catalog_id]
            else:
                search_catalogs = _get_search_catalogs_for_addon(addon, c_type, cache_only=cache_only)
                
            if not search_catalogs:
                return []
                
            addon_items = []
            for cat_id in search_catalogs:
                if is_cancelled and is_cancelled():
                    break
                search_url = f"{base_url}catalog/{c_type}/{urllib.parse.quote(str(cat_id), safe=':')}/search={urllib.parse.quote(query)}.json"
                data = _get_cached_request(search_url, max_age_hours=2, cache_only=cache_only, timeout=3.5)
                if data and isinstance(data.get("metas"), list):
                    addon_items.extend(data["metas"])
                    if addon_items: 
                        break # Break early if we found results for this addon
            return addon_items

        addons_to_search = [
            a for a in database.get_addons() 
            if (not target_manifest_url or a.get("manifest_url") == target_manifest_url) 
            and has_catalog_resource(a, media_type=c_type)
        ]
        if not addons_to_search:
            return []

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(addons_to_search), 6))
        future_to_addon = {executor.submit(fetch_addon_search, addon): addon for addon in addons_to_search}
        try:
            for future in concurrent.futures.as_completed(future_to_addon, timeout=7):
                if is_cancelled and is_cancelled():
                    break
                try:
                    addon_items = future.result()
                    new_batch = []
                    q_lower = query.lower()
                    for m in addon_items:
                        if database.is_adult_content_hidden() and is_adult_item(m):
                            continue
                        title = m.get("name") or ""
                        desc = m.get("description") or ""
                        if q_lower not in str(title).lower() and q_lower not in str(desc).lower():
                            continue

                        imdb_id = m.get("imdb_id") or m.get("id")
                        if not imdb_id or imdb_id in seen_ids:
                            continue

                        title_lower = title.lower().strip()
                        year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""

                        matched_item = None
                        if title_lower in seen_titles:
                            for existing in seen_titles[title_lower]:
                                if existing["year"] == year or not existing["year"] or not year:
                                    matched_item = existing
                                    break

                        if matched_item:
                            if imdb_id not in matched_item.get("alias_ids", []):
                                if str(imdb_id).startswith("tt") and not str(matched_item["id"]).startswith("tt"):
                                    matched_item.setdefault("alias_ids", []).insert(0, imdb_id)
                                    matched_item["id"] = imdb_id
                                    if m.get("type"):
                                        matched_item["type"] = m.get("type")
                                else:
                                    matched_item.setdefault("alias_ids", []).append(imdb_id)
                                seen_ids.add(imdb_id)
                            if not matched_item["year"] and year:
                                matched_item["year"] = year
                            continue

                        seen_ids.add(imdb_id)
                        poster_url = m.get("poster") or m.get("medium_cover_image") or m.get("logo") or m.get("banner") or m.get("background") or m.get("icon") or m.get("thumbnail") or ""
                        if poster_url and poster_url.startswith("//"):
                            poster_url = "https:" + poster_url
                        item_obj = {
                            "id": imdb_id,
                            "alias_ids": [imdb_id],
                            "title": title,
                            "year": year,
                            "medium_cover_image": poster_url,
                            "poster": poster_url,
                            "type": m.get("type") or media_type
                        }
                        seen_titles.setdefault(title_lower, []).append(item_obj)
                        items.append(item_obj)
                        new_batch.append(item_obj)
                    if new_batch and on_item_found:
                        if not (is_cancelled and is_cancelled()):
                            on_item_found(new_batch)
                except Exception:
                    pass
        except concurrent.futures.TimeoutError:
            for future in future_to_addon:
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
                    
        return items

    if catalog_url:
        is_iptv_org = False
        for a in database.get_addons():
            if a.get("manifest_url") == catalog_url and a.get("id") == "local.iptv-org":
                is_iptv_org = True
                break
                
        if is_iptv_org:
            channels_data = _get_cached_request("https://iptv-org.github.io/api/channels.json", max_age_hours=24)
            if not channels_data:
                return []
            
            country_code = catalog_id.upper()
            movies = []
            for ch in channels_data:
                if country_code == "ALL" or ch.get("country") == country_code:
                    movies.append({
                        "id": ch.get("id"),
                        "title": ch.get("name"),
                        "year": ch.get("country", ""),
                        "medium_cover_image": ch.get("logo", ""),
                        "type": "tv"
                    })
            if genre and genre != "All":
                movies = [m for m in movies if genre.lower() in str(m.get("categories", [])).lower()]
            return movies[skip:skip+100]

        base_url = catalog_url
        if "manifest.json" in base_url:
            base_url = base_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"):
            base_url += "/"
            
        actual_cat_type = c_type
        for a in database.get_addons():
            m_url = a.get("manifest_url", "")
            if m_url and (m_url == catalog_url or catalog_url.startswith(m_url.rsplit("manifest.json", 1)[0])):
                candidates = [cat for cat in get_addon_catalogs(a, cache_only=True) if str(cat.get("id")) == str(catalog_id)]
                if len(candidates) == 1:
                    actual_cat_type = candidates[0].get("type") or c_type
                elif len(candidates) > 1:
                    exact = next((cat.get("type") for cat in candidates if cat.get("type") == c_type), None)
                    if exact:
                        actual_cat_type = exact
                    else:
                        compat = next((cat.get("type") for cat in candidates if is_type_match(cat.get("type"), c_type)), None)
                        actual_cat_type = compat or c_type
                break

        url = f"{base_url}catalog/{actual_cat_type}/{catalog_id}"
        
        extras = []
        if genre and genre != "All":
            extras.append(f"genre={urllib.parse.quote(genre)}")
        if skip > 0:
            extras.append(f"skip={skip}")
            
        if extras:
            url += "/" + "&".join(extras) + ".json"
        else:
            url += ".json"
            
        data = _get_cached_request(url, max_age_hours=2, cache_only=cache_only, timeout=2.5)
        if data is None:
            return None
            
        if isinstance(data.get("metas"), list):
            from .movie_widget import extract_image_url
            movies = []
            for m in data["metas"]:
                if database.is_adult_content_hidden() and is_adult_item(m):
                    continue
                imdb_id = m.get("imdb_id") or m.get("id")
                if not imdb_id or str(imdb_id).startswith("hub:upsell") or imdb_id == "upsell" or str(m.get("name", "")).lower() == "unlock every stream":
                    continue
                poster = extract_image_url(m)
                title = m.get("name", "")
                year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""
                item_type = m.get("type") or media_type
                if str(imdb_id).startswith("bolly:m:") or str(imdb_id).startswith("hub:m:"):
                    item_type = "movie"
                elif str(imdb_id).startswith("bolly:s:") or str(imdb_id).startswith("hub:s:"):
                    item_type = "series"

                alias_ids = [imdb_id] if imdb_id else []

                if str(imdb_id).startswith("bolly:") or str(imdb_id).startswith("hub:"):
                    tmdb_num = str(imdb_id).split(":")[-1]
                    if tmdb_num.isdigit():
                        alias_ids.append(f"tmdb:{tmdb_num}")

                if str(imdb_id).startswith("tpb_ctl:"):
                    try:
                        import base64, json
                        raw_b64 = str(imdb_id).split("tpb_ctl:", 1)[1]
                        payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
                        if payload.get("extra", {}).get("type"):
                            item_type = payload["extra"]["type"]
                        p_val = payload.get("poster")
                        if p_val:
                            if not poster: poster = p_val
                            tt_m = re.search(r'\b(tt\d{7,8})\b', p_val)
                            if tt_m and tt_m.group(1) not in alias_ids:
                                alias_ids.append(tt_m.group(1))
                    except Exception:
                        pass
                elif poster:
                    tt_m = re.search(r'\b(tt\d{7,8})\b', poster)
                    if tt_m and tt_m.group(1) not in alias_ids:
                        alias_ids.append(tt_m.group(1))

                movies.append({
                    "id": imdb_id,
                    "alias_ids": alias_ids,
                    "title": title,
                    "year": year,
                    "medium_cover_image": poster,
                    "poster": poster,
                    "type": item_type
                })
            return movies
        return []

    return []

def is_valid_meta(res):
    if not res or not isinstance(res, dict):
        return False
    title = str(res.get("title", "")).strip()
    if not title:
        return False
    lower_title = title.lower()
    if "error getting meta" in lower_title or lower_title.startswith("error ") or lower_title == "failed to load details":
        return False
    if lower_title == "media item" and not res.get("description"):
        return False
    if "synopsis temporarily unavailable" in str(res.get("description", "")).lower() and not res.get("videos"):
        return False
    if str(res.get("id", "")).startswith("ctmdb.") and not res.get("videos"):
        return False
    return True

def _save_and_return_meta(res, imdb_id, media_type="movie", title=None, poster=None):
    if not res or not isinstance(res, dict):
        return res

    # Preserve existing valid poster from parameter or database cache before replacing with addon poster
    existing = database.get_cached_metadata(imdb_id)
    existing_poster = poster or (existing.get("medium_cover_image") if existing else None)

    if existing_poster:
        res["medium_cover_image"] = existing_poster
    else:
        current_poster = res.get("medium_cover_image", "")
        if not current_poster and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
            # Run fallback poster fetchers in parallel with short timeout
            def _fetch_tmdb_poster():
                try:
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    tmdb_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
                    tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168, timeout=2.5)
                    if tmdb_data and "meta" in tmdb_data and tmdb_data["meta"].get("poster"):
                        return tmdb_data["meta"]["poster"], tmdb_data["meta"].get("background")
                except Exception:
                    pass
                return None, None

            def _fetch_imdb_poster():
                if not title:
                    return None, None
                try:
                    clean_title = re.sub(r'[^a-zA-Z0-9]', '_', title).lower()
                    first_char = clean_title[0] if clean_title else "t"
                    imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(clean_title)}.json"
                    req = urllib.request.Request(imdb_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    with urllib.request.urlopen(req, timeout=2.5, context=_SSL_CONTEXT) as response:
                        data = json.loads(response.read().decode('utf-8', errors='ignore'))
                        if data and "d" in data:
                            for item in data["d"]:
                                if "i" in item and "imageUrl" in item["i"]:
                                    p_url = item["i"]["imageUrl"]
                                    p_url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', p_url)
                                    return p_url, p_url
                except Exception:
                    pass
                return None, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f_tmdb = executor.submit(_fetch_tmdb_poster)
                f_imdb = executor.submit(_fetch_imdb_poster)
                for f in [f_tmdb, f_imdb]:
                    try:
                        p, b = f.result(timeout=2.5)
                        if p and not res.get("medium_cover_image"):
                            res["medium_cover_image"] = p
                            if b and not res.get("background"):
                                res["background"] = b
                            break
                    except Exception:
                        pass

    if existing and existing.get("trailer") and not res.get("trailer"):
        res["trailer"] = existing["trailer"]

    if not res.get("trailer") and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
        try:
            c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
            tmdb_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
            tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168, timeout=2.5)
            if tmdb_data and "meta" in tmdb_data:
                tm_meta = tmdb_data["meta"]
                tr_id = tm_meta.get("trailer")
                if not tr_id:
                    for ts in tm_meta.get("trailerStreams", []):
                        if isinstance(ts, dict) and ts.get("ytId"):
                            tr_id = ts.get("ytId")
                            break
                if not tr_id:
                    for t in tm_meta.get("trailers", []):
                        if isinstance(t, dict):
                            tr_id = t.get("source") or t.get("ytId")
                        elif isinstance(t, str):
                            tr_id = t
                        if tr_id: break
                if tr_id:
                    res["trailer"] = tr_id
        except Exception:
            pass

    if existing and existing.get("background") and not res.get("background"):
        res["background"] = existing["background"]

    if is_valid_meta(res):
        database.save_cached_metadata(imdb_id, media_type, res)
        if res.get("id") and res.get("id") != imdb_id:
            database.save_cached_metadata(res.get("id"), media_type, res)
    return res

def fetch_movie_details(imdb_id, media_type="movie", title=None, use_cache=True, poster=None):
    if isinstance(imdb_id, list):
        if not imdb_id: return {}
        col_id = next((i for i in imdb_id if str(i).startswith('ctmdb.')), None)
        if col_id:
            primary_id = col_id
        else:
            primary_id = next((i for i in imdb_id if str(i).startswith('tt')), imdb_id[0])
        return fetch_movie_details(primary_id, media_type, title, use_cache, poster)

    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else (title or "Radio Stream")
        item_genre = parts[2] if len(parts) > 2 else ""
        item_poster = parts[3] if len(parts) > 3 and parts[3] else (poster or "")
        
        res = {
            "id": imdb_id,
            "title": item_title,
            "year": "",
            "medium_cover_image": item_poster,
            "background": "",
            "description": f"Live Radio / Stream ({item_genre})" if item_genre else "Live Radio / Stream",
            "runtime": "Live",
            "genre": item_genre,
            "imdbRating": "",
            "trailer": None,
            "videos": []
        }
        database.save_cached_metadata(imdb_id, media_type, res)
        return res

    if str(imdb_id).startswith("tpb_ctl:"):
        try:
            import base64, json
            raw_b64 = str(imdb_id).split("tpb_ctl:", 1)[1]
            payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
            if payload.get("extra", {}).get("type"):
                media_type = payload["extra"]["type"]
            p_val = payload.get("poster")
            if p_val:
                if not poster: poster = p_val
                tt_m = re.search(r'\b(tt\d{7,8})\b', p_val)
                if tt_m:
                    imdb_id = tt_m.group(1)
            if not title and payload.get("parsedName"):
                title = payload.get("parsedName")
        except Exception:
            pass

    if poster and not (str(imdb_id).startswith("tt") or str(imdb_id).startswith("tmdb:") or str(imdb_id).startswith("ctmdb.")):
        tt_m = re.search(r'\b(tt\d{7,8})\b', str(poster))
        if tt_m:
            imdb_id = tt_m.group(1)

    # Normalize media_type and c_type to standard formats
    str_imdb = str(imdb_id or "")
    if str_imdb.startswith("bolly:s:") or str_imdb.startswith("hub:s:") or ":s:" in str_imdb:
        media_type = "series"
    elif str_imdb.startswith("bolly:m:") or str_imdb.startswith("hub:m:") or ":m:" in str_imdb:
        media_type = "movie"
    elif str_imdb.startswith("ctmdb.") or media_type == "collections":
        media_type = "collections"
    elif media_type in ["series", "tvshow", "tv_series"]:
        media_type = "series"
    elif media_type in ["tv", "channel", "tvchannel"]:
        media_type = "tv"
    elif media_type in ["music", "radio"]:
        media_type = "music"
    elif media_type not in ["movie", "series", "anime", "tv", "channel", "tvchannel", "music", "radio", "collections"]:
        media_type = "movie"

    # Resolve TMDB ids to IMDB format if needed
    imdb_id = resolve_to_imdb_id(imdb_id, media_type, title)

    if use_cache and imdb_id:
        cached = database.get_cached_metadata(imdb_id)
        if cached and is_valid_meta(cached):
            if poster and not cached.get("medium_cover_image"):
                cached["medium_cover_image"] = poster
                database.save_cached_metadata(imdb_id, media_type, cached)
            elif poster and cached.get("medium_cover_image"):
                cached["medium_cover_image"] = poster
            return cached

    if media_type in ["tv", "channel", "tvchannel"]:
        for addon in database.get_addons():
            if addon.get("id") == "local.iptv-org":
                channels_data = _get_cached_request("https://iptv-org.github.io/api/channels.json", max_age_hours=24)
                ch = next((c for c in channels_data if c.get("id") == imdb_id), None) if channels_data else None
                if ch:
                    res = {
                        "id": ch.get("id"),
                        "title": ch.get("name"),
                        "year": "",
                        "medium_cover_image": ch.get("logo", ""),
                        "background": "",
                        "description": f"Live TV Channel from {ch.get('country')}. Categories: {', '.join(ch.get('categories', []))}",
                        "runtime": "Live",
                        "genre": ", ".join(ch.get("categories", [])),
                        "imdbRating": "",
                        "trailer": None,
                        "videos": []
                    }
                    database.save_cached_metadata(imdb_id, media_type, res)
                    return res

    c_type = "series" if media_type in ["series", "anime"] else ("tv" if media_type in ["tv", "channel", "tvchannel"] else ("music" if media_type in ["music", "radio"] else "movie"))

    def fetch_addon_meta(addon_orig, req_type=None):
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        target_m_type = req_type or c_type
        if not addon.get("enabled", True): return None
        m_url = addon.get("manifest_url", "")
        if not m_url or m_url.startswith("builtin:"): return None
        if addon.get("id") == "local.iptv-org": return None
        
        resources = addon.get("resources")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if resources is None or addon_types is None or ("idPrefixes" not in addon):
            try:
                manifest_data = _get_cached_request(m_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass
                
        matched_type = target_m_type
        if str(imdb_id).startswith("ctmdb."):
            matched_type = "movie"
        elif addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, target_m_type)), None)
            if type_match:
                matched_type = type_match
            else:
                has_cat_match = any(is_type_match(cat.get("type"), target_m_type) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(imdb_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return None
            
        if addon_prefixes is not None:
            if not any(str(imdb_id).startswith(p) for p in addon_prefixes):
                return None
                
        if resources is not None:
            has_meta = False
            for r in resources:
                if isinstance(r, str) and r == "meta":
                    has_meta = True
                elif isinstance(r, dict) and r.get("name") == "meta":
                    has_meta = True
            if not has_meta:
                return None
        
        base_url = m_url.rsplit("manifest.json", 1)[0] if "manifest.json" in m_url else m_url
        if not base_url.endswith("/"): base_url += "/"
        
        meta_url = f"{base_url}meta/{matched_type}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
        data = _get_cached_request(meta_url, max_age_hours=168)
        if not data and "v3-cinemeta.strem.io" in meta_url:
            alt_url = meta_url.replace("v3-cinemeta.strem.io", "cinemeta-live.strem.io")
            data = _get_cached_request(alt_url, max_age_hours=168)
        
        if data and data.get("meta"):
            cm = data["meta"]
            t_val = cm.get("name", "")
            if not t_val or "error getting meta" in t_val.lower() or t_val.lower().startswith("error"):
                return None

            videos = []
            for v in cm.get("videos", []):
                videos.append({
                    "id": v.get("id", ""),
                    "season": v.get("season", 1),
                    "episode": v.get("episode", 1),
                    "title": v.get("title", ""),
                    "overview": v.get("overview", ""),
                    "released": v.get("released", ""),
                    "thumbnail": v.get("thumbnail", "")
                })
            
            true_id = cm.get("imdb_id") or imdb_id
            if str(true_id).startswith("tmdb:") and not str(true_id).startswith("ctmdb.") and not cm.get("videos"):
                m_title = cm.get("name")
                if m_title:
                    try:
                        search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(m_title)}.json"
                        search_data = _get_cached_request(search_url, max_age_hours=168)
                        if search_data and "metas" in search_data:
                            for m in search_data["metas"]:
                                m_id = m.get("imdb_id") or m.get("id", "")
                                if str(m_id).startswith("tt") and str(m.get("name", "")).lower() == str(m_title).lower():
                                    if str(m.get("releaseInfo", "")).split("-")[0] == str(cm.get("releaseInfo", "")).split("-")[0]:
                                        true_id = m_id
                                        break
                                    elif not cm.get("releaseInfo"):
                                        true_id = m_id
                                        break
                    except Exception:
                        pass
                        
            extracted_trailer = None
            trailer_streams = cm.get("trailerStreams", [])
            if trailer_streams and isinstance(trailer_streams, list):
                for ts in trailer_streams:
                    if isinstance(ts, dict) and ts.get("ytId"):
                        extracted_trailer = ts.get("ytId")
                        break
            
            if not extracted_trailer:
                trailers = cm.get("trailers", [])
                if trailers and isinstance(trailers, list):
                    for t in trailers:
                        if isinstance(t, dict):
                            if t.get("source"): extracted_trailer = t.get("source")
                            elif t.get("ytId"): extracted_trailer = t.get("ytId")
                        elif isinstance(t, str):
                            extracted_trailer = t
                        if extracted_trailer: break
                        
            if not extracted_trailer and isinstance(cm.get("trailer"), str):
                extracted_trailer = cm.get("trailer")
                
            cert = cm.get("certification") or cm.get("contentRating") or cm.get("rating") or cm.get("mpaa") or cm.get("ageRating") or cm.get("certification_badge") or ""
            if isinstance(cert, list): cert = ", ".join(cert)

            res_dict = {
                "id": true_id,
                "title": cm.get("name", ""),
                "year": str(cm.get("releaseInfo", "")).split("-")[0] if cm.get("releaseInfo") else "",
                "medium_cover_image": cm.get("poster", ""),
                "background": cm.get("background", ""),
                "description": cm.get("description", "No synopsis available."),
                "runtime": cm.get("runtime", ""),
                "genre": ", ".join(cm.get("genres", [])) if isinstance(cm.get("genres"), list) else str(cm.get("genres", "")),
                "certification": str(cert).strip(),
                "imdbRating": str(cm.get("imdbRating", "")),
                "trailer": extracted_trailer,
                "videos": videos,
                "cast": cm.get("cast", []),
                "genres": cm.get("genres", []),
                "type": "collections" if str(imdb_id).startswith("ctmdb.") else (cm.get("type") or matched_type),
                "adult": cm.get("adult") or cm.get("isAdult") or False
            }
            if database.is_adult_content_hidden() and is_adult_item(res_dict):
                print(f"[SAFE SEARCH] Adult content blocked: {res_dict.get('title')}")
                return None
            return res_dict
            
        return None

    addons = database.get_addons()
    cinemeta_addon = next((a for a in addons if a.get("id") == "cinemeta" or "cinemeta" in a.get("manifest_url", "").lower()), None)
    
    cinemeta_res = None
    if cinemeta_addon:
        cinemeta_res = fetch_addon_meta(cinemeta_addon, c_type)
        if (not cinemeta_res or not is_valid_meta(cinemeta_res)) and str(imdb_id).startswith("tt"):
            alt_type = "movie" if c_type in ["series", "anime", "tv"] else "series"
            alt_res = fetch_addon_meta(cinemeta_addon, alt_type)
            if alt_res and is_valid_meta(alt_res):
                cinemeta_res = alt_res
                media_type = alt_type
        if cinemeta_res and is_valid_meta(cinemeta_res):
            return _save_and_return_meta(cinemeta_res, imdb_id, media_type, title, poster=poster)

    # If Cinemeta failed, fallback to other metadata-supporting addons
    other_addons = [a for a in addons if a != cinemeta_addon and has_meta_resource(a)]
    if other_addons:
        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(other_addons), 4))
        future_to_addon = {executor.submit(fetch_addon_meta, addon): addon for addon in other_addons}
        try:
            for future in concurrent.futures.as_completed(future_to_addon, timeout=3.0):
                res = future.result()
                if res and is_valid_meta(res):
                    if cinemeta_res and not cinemeta_res.get("trailer") and res.get("trailer"):
                        cinemeta_res["trailer"] = res.get("trailer")
                        if not cinemeta_res.get("background") and res.get("background"):
                            cinemeta_res["background"] = res.get("background")
                        return _save_and_return_meta(cinemeta_res, imdb_id, media_type, title, poster=poster)

                    if not cinemeta_res:
                        return _save_and_return_meta(res, imdb_id, media_type, title, poster=poster)
        except concurrent.futures.TimeoutError:
            for future in future_to_addon:
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    if cinemeta_res:
        return _save_and_return_meta(cinemeta_res, imdb_id, media_type, title, poster=poster)

    if str(imdb_id).startswith("ctmdb.") or media_type == "collections":
        try:
            from .tmdb_helper import fetch_collection_details
            col_res = fetch_collection_details(imdb_id, title=title, poster=poster)
            if col_res and is_valid_meta(col_res):
                return _save_and_return_meta(col_res, imdb_id, "collections", title, poster=poster)
        except Exception as e:
            print(f"[COLLECTIONS] Failed to fetch TMDB collection details for {imdb_id}: {e}")

    return {
        "id": imdb_id,
        "title": title or "Media Item",
        "year": "",
        "medium_cover_image": poster or "",
        "background": "",
        "description": "Synopsis temporarily unavailable.",
        "runtime": "",
        "genre": "",
        "imdbRating": "",
        "trailer": None,
        "videos": [],
        "type": media_type
    }

def fetch_trailer_link_fast(imdb_id, media_type="movie", abort_event=None):
    """
    Quickly query trailer-capable addons (Streailer, TMDB) for a trailer ytId.
    Returns a ytId string on success, or None if nothing found within ~3 seconds.
    Designed to be called in a background thread right after page open.
    """
    if not imdb_id:
        return None

    primary_id = imdb_id[0] if isinstance(imdb_id, list) else imdb_id

    def _aborted():
        return abort_event is not None and abort_event.is_set()

    # 1. Check cached metadata first — trailer may already be known
    try:
        cached = database.get_cached_metadata(primary_id)
        if cached and cached.get("trailer"):
            return cached["trailer"]
    except Exception:
        pass

    if _aborted():
        return None

    c_type = "series" if media_type in ["series", "anime"] else "movie"

    # 2. Query TMDB addon (fast, usually <1s cached)
    try:
        tmdb_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{urllib.parse.quote(str(primary_id), safe=':')}.json"
        tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168, timeout=2.5)
        if tmdb_data and "meta" in tmdb_data:
            tm_meta = tmdb_data["meta"]
            tr_id = tm_meta.get("trailer")
            if not tr_id:
                for ts in tm_meta.get("trailerStreams", []):
                    if isinstance(ts, dict) and ts.get("ytId"):
                        tr_id = ts.get("ytId")
                        break
            if not tr_id:
                for t in tm_meta.get("trailers", []):
                    if isinstance(t, dict):
                        tr_id = t.get("source") or t.get("ytId")
                    elif isinstance(t, str):
                        tr_id = t
                    if tr_id:
                        break
            if tr_id:
                return tr_id
    except Exception:
        pass

    if _aborted():
        return None

    # 3. Query Streailer and other trailer-capable stream addons
    try:
        all_addons = database.get_addons()
        trailer_addons = [
            a for a in all_addons
            if a.get("enabled", True)
            and not a.get("manifest_url", "").startswith("builtin:")
            and any(kw in a.get("manifest_url", "").lower() or kw in a.get("name", "").lower()
                    for kw in ["streailer", "trailer"])
        ]

        def _fetch_addon_streams(addon):
            if _aborted():
                return None
            try:
                m_url = addon.get("manifest_url", "")
                base_url = m_url.rsplit("manifest.json", 1)[0]
                if not base_url.endswith("/"):
                    base_url += "/"
                stream_url = f"{base_url}stream/{c_type}/{urllib.parse.quote(str(primary_id), safe=':')}.json"
                data = _get_cached_request(stream_url, max_age_hours=2, timeout=3.0)
                if data and isinstance(data.get("streams"), list):
                    for s in data["streams"]:
                        if isinstance(s, dict):
                            yt = s.get("ytId")
                            if yt:
                                return yt
                            bh = s.get("behaviorHints", {})
                            if isinstance(bh, dict) and bh.get("bingeGroup") == "trailer":
                                url = s.get("url", "")
                                if "youtube.com" in url or "youtu.be" in url:
                                    return url
            except Exception:
                pass
            return None

        if trailer_addons:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(trailer_addons), 4)) as executor:
                futures = {executor.submit(_fetch_addon_streams, a): a for a in trailer_addons}
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=3.5):
                        if _aborted():
                            break
                        result = future.result()
                        if result:
                            return result
                except concurrent.futures.TimeoutError:
                    pass
    except Exception:
        pass

    return None


def find_episode_file_index(files, season, episode, strict=False):
    import re
    patterns = [
        rf"s{season:02d}e{episode:02d}",
        rf"s{season}e{episode}",
        rf"{season}x{episode:02d}",
        rf"{season}x{episode}",
        rf"ep(?:isode)?\s*{episode:02d}\b",
        rf"ep(?:isode)?\s*{episode}\b",
        rf"\b{episode:02d}\b"
    ]
    
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if not any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']): continue
        
        for p in patterns[:4]:
            if re.search(p, fname): return idx
                
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if not any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']): continue
            
        for p in patterns[4:]:
            if re.search(p, fname): return idx
                
    if strict:
        return None
        
    video_files = []
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']):
            size_list = f.get("size")
            size = size_list[0] if isinstance(size_list, list) else (int(size_list) if size_list is not None else 0)
            video_files.append((idx, size))
    if video_files:
        return max(video_files, key=lambda x: x[1])[0]
        
    return None

def get_stream_cache_key(imdb_id, media_type="movie", season=None, episode=None):
    primary = imdb_id[0] if isinstance(imdb_id, list) else imdb_id
    if season is not None and episode is not None:
        return f"{primary}:S{season}:E{episode}"
    return f"{primary}:{media_type}"

def _extract_quality(text):
    """Extract video quality label and numeric rank from text. Hoisted to avoid re-creation per stream."""
    t = str(text).lower()
    if "2160p" in t or re.search(r'\b4k\b', t): return "4K", 4
    if "1080p" in t or re.search(r'\b1080\b', t): return "1080p", 3
    if "720p" in t or re.search(r'\b720\b', t): return "720p", 2
    if "480p" in t or re.search(r'\b480\b', t): return "480p", 1
    if "360p" in t or re.search(r'\b360\b', t): return "360p", 0
    return None, 0

_RE_SIZE = re.compile(
    r'(?:💾|📦|[Ss]ize[:\s]*|\b)([\d.]+)\s*([TtGgMmKk][iI]?[Bb])(?!\s*(?:[pP][sS]|/[sS]|[iI][tT][sS]?|[bB][pP][sS]))\b'
)
_RE_BITRATE = re.compile(
    r'~?([\d.]+)\s*([MmKkGg]bps|[MmKkGg]b/s|[MmKkGg]bit/s)\b', re.IGNORECASE
)
_RE_SEED = re.compile(r'(?:👤|👥|[Ss]eeders?[:\s]*)\s*(\d+)')

MATCH_EXACT = 2
MATCH_SEASON_PACK = 1
MATCH_UNKNOWN = 0
MATCH_MISMATCH = -1

_RE_SXX_EXX = re.compile(
    r'\bS(\d{1,2})[\s._-]*E(\d{1,3})(?:[\s._-]*(?:E|[-~])(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_SEASON_EPISODE = re.compile(
    r'\bseason[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep)[\s._-]*(\d{1,3})(?:[\s._-]*(?:-|to)[\s._-]*(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_NXMM = re.compile(
    r'(?<!\d)(\d{1,2})x(\d{1,3})(?:[\s._-]*[-~][\s._-]*(\d{1,3}))?(?!\d|p\b)',
    re.IGNORECASE
)
_RE_STANDALONE_EP = re.compile(
    r'\b(?:episode|ep)[\s._-]*(\d{1,3})(?:[\s._-]*(?:-|to)[\s._-]*(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_ANIME_EP = re.compile(
    r'(?:^|[\s_.-])-\s*(\d{1,3})(?:v\d)?(?:[\s_.-]*(?:-|~)\s*(\d{1,3}))?(?:[\s_.-]+(?:\[|\(|\d{3,4}p|web|bd|dvd|flac|aac|x264|x265|hevc|mkv|mp4)|\.mkv|\.mp4|$)',
    re.IGNORECASE
)
_RE_SEASON_PACK = re.compile(
    r'\b(?:season[\s._-]*(\d{1,2})|s(\d{1,2}))\b(?!\s*e\d)',
    re.IGNORECASE
)

def extract_stream_episode_info(text):
    """
    Extracts (season, episode_start, episode_end, is_season_pack) from text.
    Returns: (int or None, int or None, int or None, bool)
    """
    if not text:
        return None, None, None, False
    t = str(text)

    # 1. SxxExx
    m = _RE_SXX_EXX.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        return s, ep1, ep2, False

    # 2. Season X Episode Y
    m = _RE_SEASON_EPISODE.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        return s, ep1, ep2, False

    # 3. NxMM (e.g. 1x03)
    m = _RE_NXMM.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        if s < 100 and ep1 < 1000:
            return s, ep1, ep2, False

    # 4. Standalone Ep/Episode
    m = _RE_STANDALONE_EP.search(t)
    if m:
        ep1 = int(m.group(1))
        ep2 = int(m.group(2)) if m.group(2) else None
        m_s = _RE_SEASON_PACK.search(t)
        s = int(m_s.group(1) or m_s.group(2)) if m_s else None
        return s, ep1, ep2, False

    # 5. Anime-style delimiter: " - 03 "
    m = _RE_ANIME_EP.search(t)
    if m:
        ep1 = int(m.group(1))
        ep2 = int(m.group(2)) if m.group(2) else None
        m_s = _RE_SEASON_PACK.search(t)
        s = int(m_s.group(1) or m_s.group(2)) if m_s else None
        return s, ep1, ep2, False

    # 6. Season Pack
    m = _RE_SEASON_PACK.search(t)
    if m:
        s = int(m.group(1) or m.group(2))
        return s, None, None, True

    return None, None, None, False

def get_stream_search_text(stream):
    """Collect all searchable text lines/filenames for a stream."""
    if not isinstance(stream, dict):
        return str(stream or "")
    parts = []
    for k in ("filename", "stream_title", "title", "name"):
        v = stream.get(k)
        if v and isinstance(v, str):
            parts.append(v)
    bh = stream.get("behaviorHints")
    if isinstance(bh, dict):
        for k in ("filename", "videoFilename"):
            v = bh.get(k)
            if v and isinstance(v, str):
                parts.append(v)
    raw_url = stream.get("url") or stream.get("externalUrl")
    if raw_url and isinstance(raw_url, str):
        try:
            unquoted = urllib.parse.unquote(raw_url.split("?")[0].split("#")[0])
            last_segment = unquoted.rstrip("/").rsplit("/", 1)[-1]
            if any(last_segment.lower().endswith(ext) for ext in [".mkv", ".mp4", ".avi", ".webm", ".m4v"]):
                parts.append(last_segment)
        except Exception:
            pass
    return " \n ".join(parts)

def match_stream_to_episode(stream, target_season, target_episode, ep_title=None, series_title=None):
    """
    Evaluates whether a stream corresponds to the requested season and episode.
    Returns:
        MATCH_EXACT (2): Explicit match for target season and episode.
        MATCH_SEASON_PACK (1): Valid season pack containing target season (torrents only).
        MATCH_UNKNOWN (0): Ambiguous stream with no season or episode indicator.
        MATCH_MISMATCH (-1): Explicit indicator of a different episode or different season.
    """
    if target_season is None and target_episode is None:
        return MATCH_UNKNOWN

    try:
        t_season = int(target_season) if target_season is not None else 1
    except (ValueError, TypeError):
        t_season = 1

    try:
        t_episode = int(target_episode) if target_episode is not None else None
    except (ValueError, TypeError):
        t_episode = None

    if t_episode is None:
        return MATCH_UNKNOWN

    text = get_stream_search_text(stream)
    if not text.strip():
        return MATCH_UNKNOWN

    s, ep_start, ep_end, is_pack = extract_stream_episode_info(text)

    # 1. Season mismatch check
    if s is not None and s != t_season:
        return MATCH_MISMATCH

    # 2. Season pack match
    if is_pack:
        is_http = stream.get("is_http") if isinstance(stream, dict) else False
        if is_http:
            return MATCH_UNKNOWN
        return MATCH_SEASON_PACK

    # 3. Explicit episode number matched
    if ep_start is not None:
        if ep_end is not None:
            if ep_start <= t_episode <= ep_end:
                return MATCH_EXACT
            else:
                return MATCH_MISMATCH
        else:
            if ep_start == t_episode:
                return MATCH_EXACT
            else:
                return MATCH_MISMATCH

    # 4. Episode title match (fallback when filename has no SxxExx but has episode title)
    if ep_title and isinstance(ep_title, str):
        clean_ep_title = ep_title.strip()
        if len(clean_ep_title) >= 4 and not re.match(r'^(?:episode|ep|part)\s*\d+$', clean_ep_title, re.IGNORECASE):
            norm_title = re.sub(r'[^a-zA-Z0-9]+', ' ', clean_ep_title).lower().strip()
            norm_text = re.sub(r'[^a-zA-Z0-9]+', ' ', text).lower()
            if norm_title and norm_title in norm_text:
                return MATCH_EXACT

    return MATCH_UNKNOWN

def process_raw_streams(all_streams, season=None, episode=None, ep_title=None):
    if not all_streams:
        return []
    valid_streams = []
    seen_keys = {}  # {dedup_key: index in valid_streams} for O(1) duplicate lookup
    for s in all_streams:
        yt_id = str(s.get("ytId") or "").strip()
        raw_stream_url = str(s.get("url") or "")
        external_url = str(s.get("externalUrl") or "")
        info_hash = str(s.get("infoHash") or "").lower()
        
        if yt_id and not raw_stream_url and not external_url:
            raw_stream_url = f"https://www.youtube.com/watch?v={yt_id}"

        is_external = bool(external_url and not raw_stream_url)
        stream_url = raw_stream_url or external_url
        
        is_http = False
        if stream_url.startswith("http://") or stream_url.startswith("https://"):
            if not stream_url.split('?')[0].endswith(".torrent"):
                is_http = True
        elif stream_url.startswith("magnet:") and not info_hash:
            match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', stream_url, re.IGNORECASE)
            if match:
                info_hash = match.group(1).lower()
            
        if not info_hash and not stream_url:
            continue

        raw_id = info_hash if not is_http else hashlib.md5(stream_url.encode()).hexdigest()
        
        desc_str = s.get("title") or s.get("description") or ""
        name_str = s.get("name") or ""
        
        desc_clean = " • ".join([line.strip() for line in desc_str.splitlines() if line.strip()])
        name_clean = " • ".join([line.strip() for line in name_str.splitlines() if line.strip()])
        
        if desc_clean and name_clean and name_clean not in desc_clean:
            full_title = f"{name_clean} - {desc_clean}"
        else:
            full_title = desc_clean or name_clean
            
        title_str = desc_str
        name_and_title = (name_str + " " + title_str)

        dedup_key = f"{raw_id}:{full_title}"
        if dedup_key in seen_keys:
            vs = valid_streams[seen_keys[dedup_key]]
            if s.get("addon_name") and s["addon_name"] not in vs["addon_names"]:
                vs["addon_names"].append(s["addon_name"])
            continue
        seen_keys[dedup_key] = len(valid_streams)

        quality, q_val = _extract_quality(name_str)
        if not quality:
            quality, q_val = _extract_quality(title_str)
        if not quality:
            quality, q_val = "Unknown", 0
        
        combined_text = f"{title_str} {name_str}"
        size = ""
        size_gb = 0.0
        size_matches = list(_RE_SIZE.finditer(combined_text))
        if size_matches:
            explicit_match = next((m for m in size_matches if any(p in m.group(0) for p in ['💾', '📦', 'ize', 'ize:'])), None)
            chosen_match = explicit_match or size_matches[-1]
            unit = chosen_match.group(2).upper()
            try:
                val = float(chosen_match.group(1))
                size = f"{chosen_match.group(1)} {unit}"
                if "TB" in unit:
                    size_gb = val * 1024.0
                elif "GB" in unit:
                    size_gb = val
                elif "MB" in unit:
                    size_gb = val / 1024.0
                elif "KB" in unit:
                    size_gb = val / (1024.0 * 1024.0)
            except ValueError:
                pass
            
        bitrate = ""
        bitrate_match = _RE_BITRATE.search(combined_text)
        if bitrate_match:
            bitrate = f"{bitrate_match.group(1)} {bitrate_match.group(2)}"
            
        seeders = 0
        seed_match = _RE_SEED.search(title_str)
        if seed_match:
            try:
                seeders = int(seed_match.group(1))
            except ValueError:
                pass
            
        behavior_hints = s.get("behaviorHints", {})
        filename = behavior_hints.get("filename") or behavior_hints.get("videoFilename")
        if not filename:
            filename = title_str.split('\n')[0] if '\n' in title_str else ""
        if not filename:
            filename = name_and_title.replace('/', '_')

        is_trailer = bool(
            yt_id
            or s.get("is_trailer")
            or (isinstance(behavior_hints, dict) and behavior_hints.get("bingeGroup") == "trailer")
            or any("trailer" in str(a).lower() or "streailer" in str(a).lower() for a in [s.get("addon_name", ""), s.get("name", ""), s.get("title", "")])
            or "youtube.com" in stream_url.lower() or "youtu.be" in stream_url.lower()
        )
            
        stream_entry = {
            "hash": raw_id,
            "url": stream_url,
            "sources": s.get("sources") or [],
            "externalUrl": external_url,
            "is_external": is_external,
            "is_http": is_http,
            "is_trailer": is_trailer,
            "ytId": yt_id or s.get("ytId", ""),
            "quality": quality,
            "q_val": q_val,
            "size": size,
            "size_gb": size_gb,
            "bitrate": bitrate,
            "seeders": seeders,
            "title": s.get("name") or "",
            "stream_title": full_title,
            "file_index": s.get("fileIdx"),
            "filename": filename,
            "behaviorHints": behavior_hints,
            "addon_names": [s.get("addon_name")] if s.get("addon_name") else []
        }

        if season is not None and episode is not None:
            ep_match = match_stream_to_episode(stream_entry, season, episode, ep_title)
            # Filter out streams that explicitly belong to a different episode or season!
            if ep_match == MATCH_MISMATCH:
                continue
            stream_entry["ep_match"] = ep_match
        else:
            stream_entry["ep_match"] = MATCH_UNKNOWN

        valid_streams.append(stream_entry)
    
    def _rank_key(x):
        ep_m = x.get("ep_match", 0)
        q_val = x.get("q_val", 0)
        size_gb = float(x.get("size_gb") or 0.0)
        seeders = x.get("seeders", 0) if not x.get("is_http") else 100
        # q_val: 4=4K, 3=1080p, 2=720p, 1=480p, 0=360p/Unknown
        is_1080p_optimal = (q_val == 3 and ((0 < size_gb <= 4.5) or size_gb == 0.0))
        is_720p_optimal = (q_val == 2 and ((0 < size_gb <= 2.5) or size_gb == 0.0))
        if is_1080p_optimal:
            p_tier = 4
        elif is_720p_optimal:
            p_tier = 3
        elif q_val == 3:
            p_tier = 2
        elif q_val == 4 or q_val == 2:
            p_tier = 1
        else:
            p_tier = 0
        return (ep_m, p_tier, seeders, q_val, size_gb)

    valid_streams.sort(key=_rank_key, reverse=True)
    return valid_streams

def get_torrents(imdb_id, media_type="movie", season=None, episode=None, use_cache=True):
    if not imdb_id:
        return []
        
    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else "Live Stream"
        return [{
            "url": stream_url,
            "name": "Live Stream",
            "title": item_title,
            "behaviorHints": {"filename": "stream.mp3"}
        }]
        
    cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    if use_cache:
        cached = database.get_cached_streams(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

    actual_media = media_type
    
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    if not addons:
        return []
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://") and has_stream_resource(a, media_type=actual_media, item_id=imdb_id)]
    
    def fetch_from_addon(addon_orig):
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        resources = addon.get("resources")
        manifest_url = addon.get("manifest_url", "")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if addon.get("id") == "local.iptv-org":
            streams_data = _get_cached_request("https://iptv-org.github.io/api/streams.json", max_age_hours=24)
            strms = [s for s in streams_data if s.get("channel") == imdb_id] if streams_data else []
            valid_strms = []
            for s in strms:
                height = s.get("height", "")
                res_str = f"{height}p" if height else "Live"
                valid_strms.append({
                    "url": s.get("url"),
                    "name": "IPTV-Org",
                    "title": f"Resolution: {res_str}",
                    "behaviorHints": {"filename": "live.m3u8"}
                })
            return addon.get("name", "Unknown"), valid_strms

        if (resources is None or addon_types is None or "idPrefixes" not in addon) and manifest_url:
            try:
                manifest_data = _get_cached_request(manifest_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass

        if addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, actual_media)), None)
            if not type_match:
                has_cat_match = any(is_type_match(cat.get("type"), actual_media) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(imdb_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return addon.get("name", "Unknown"), []

        if addon_prefixes is not None:
            if not any(str(imdb_id).startswith(p) for p in addon_prefixes):
                return addon.get("name", "Unknown"), []
                
        if not has_stream_resource(addon, media_type=actual_media, item_id=imdb_id):
            return addon.get("name", "Unknown"), []
                
        if "manifest.json" in manifest_url:
            base_url = manifest_url.rsplit('manifest.json', 1)[0]
        else:
            base_url = manifest_url
        if not base_url.endswith('/'):
            base_url += '/'
            
        if actual_media == "series" and season is not None and episode is not None:
            url = f"{base_url}stream/series/{urllib.parse.quote(str(imdb_id), safe=':')}:{season}:{episode}.json"
        else:
            url = f"{base_url}stream/{actual_media}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
            
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
            with urllib.request.urlopen(req, timeout=12, context=_SSL_CONTEXT) as response:
                data = json.loads(response.read().decode('utf-8'))
            _record_addon_success(manifest_url)
            if isinstance(data, dict):
                return addon.get("name", "Unknown"), data.get("streams", [])
            elif isinstance(data, list):
                return addon.get("name", "Unknown"), data
            return addon.get("name", "Unknown"), []
        except urllib.error.HTTPError as e:
            code = e.code
            logging.debug(f"HTTP Error {code} fetching from addon {addon.get('name')}")
            if code >= 500:
                _record_addon_failure(manifest_url)
            else:
                _record_addon_success(manifest_url)
            try:
                e.close()
            except Exception:
                pass
            return addon.get("name", "Unknown"), []
        except urllib.error.URLError as e:
            print(f"Error fetching from addon {addon.get('name')}: {e.reason}")
            _record_addon_failure(manifest_url)
            return addon.get("name", "Unknown"), []
        except Exception as e:
            err_str = str(e).lower()
            print(f"Error fetching from addon {addon.get('name')}: {e}")
            if any(kw in err_str for kw in ["timed out", "timeout", "connection refused", "connection reset", "no route", "name or service"]):
                _record_addon_failure(manifest_url)
            return addon.get("name", "Unknown"), []
            
    all_streams = []
    num_workers = min(len(stremio_addons), 20)
    if num_workers > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {executor.submit(fetch_from_addon, addon): addon for addon in stremio_addons}
                
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=15):
                    try:
                        addon_name, streams = future.result()
                        if streams:
                            for s in streams:
                                s["addon_name"] = addon_name
                                all_streams.append(s)
                    except Exception as e:
                        print(f"Error in addon future: {e}")
            except concurrent.futures.TimeoutError:
                print("Timeout fetching streams from some addons")
            
    valid_streams = process_raw_streams(all_streams, season=season, episode=episode)
    if valid_streams:
        database.save_cached_streams(cache_key, valid_streams)
    return valid_streams

# Pre-cache ffprobe availability once at module level
import shutil as _shutil
import subprocess as _subprocess
_has_ffprobe = bool(_shutil.which('ffprobe'))

def _ping_stream_url(stream):
    url = stream.get("url")
    if not url or not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        return stream
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    bh = stream.get("behaviorHints", {})
    if isinstance(bh, dict) and "headers" in bh:
        for k, v in bh.get("headers", {}).items():
            headers[k] = v

    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        for k_target in ["User-Agent", "Referer", "Origin", "Cookie"]:
            for p_key, p_val in params.items():
                if p_key.lower() == k_target.lower() and p_val:
                    headers[k_target] = p_val[0]
    except Exception:
        pass

    for attempt in range(2):
        if _has_ffprobe:
            try:
                cmd = ['ffprobe', '-v', 'error', '-threads', '1', '-analyzeduration', '1000000', '-probesize', '1000000']
                headers_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()])
                if headers_str:
                    cmd.extend(['-headers', headers_str])
                cmd.extend(['-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', url])

                res = _subprocess.run(cmd, capture_output=True, timeout=3.5)
                if res.returncode == 0:
                    stream["is_working"] = True
                    return stream
            except Exception:
                pass

        # Fallback to urllib if ffprobe fails (or isn't available)
        try:
            req = urllib.request.Request(url, headers=headers, method='HEAD')
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CONTEXT) as resp:
                if resp.status < 400:
                    stream["is_working"] = True
                    return stream
        except Exception:
            pass

        try:
            req = urllib.request.Request(url, headers=dict(headers, Range='bytes=0-100'))
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CONTEXT) as resp:
                if resp.status < 400:
                    stream["is_working"] = True
                    return stream
        except Exception:
            pass

        if attempt < 1:
            time.sleep(0.3)

    stream["is_working"] = False
    return stream

def ping_and_filter_streams(streams):
    if not streams:
        return []
    http_streams = [s for s in streams if s.get("url") and (s["url"].startswith("http://") or s["url"].startswith("https://"))]
    if not http_streams:
        return streams
        
    num_workers = min(len(http_streams), 12)
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        results = list(executor.map(_ping_stream_url, http_streams))
        
    working_urls = {s["url"] for s in results if s.get("is_working") is True}
        
    filtered = []
    for s in streams:
        url = s.get("url")
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            filtered.append(s)
        elif url in working_urls:
            filtered.append(s)
    return filtered

def get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None, callback=None, title=None, abort_event=None, ep_title=None):
    if not imdb_id:
        if callback: callback([], is_cached=False, is_complete=True)
        return []

    if abort_event and abort_event.is_set():
        if callback: callback([], is_cached=False, is_complete=True)
        return []

    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else (title or "Live Stream")
        stream_obj = [{
            "url": stream_url,
            "name": "Live Stream",
            "title": item_title,
            "behaviorHints": {"filename": "stream.mp3"}
        }]
        if callback: callback(stream_obj, is_cached=False, is_complete=True)
        return stream_obj

    # Check cache immediately using the provided ID
    cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    cached = database.get_cached_streams(cache_key, max_age_hours=24)
    if cached and callback:
        callback(cached, is_cached=True, is_complete=False)

    actual_media = media_type
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    if not addons:
        if callback: callback(cached or [], is_cached=False, is_complete=True)
        return cached or []
        
    all_stream_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://") and has_stream_resource(a, media_type=actual_media)]
    if not all_stream_addons:
        if callback: callback(cached or [], is_cached=False, is_complete=True)
        return cached or []

    def fetch_from_addon(addon_orig, cur_id):
        if abort_event and abort_event.is_set():
            return addon_orig.get("name", "Unknown"), []
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        resources = addon.get("resources")
        manifest_url = addon.get("manifest_url", "")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if addon.get("id") == "local.iptv-org":
            streams_data = _get_cached_request("https://iptv-org.github.io/api/streams.json", max_age_hours=24)
            strms = [s for s in streams_data if s.get("channel") == cur_id] if streams_data else []
            valid_strms = []
            for s in strms:
                height = s.get("height", "")
                res_str = f"{height}p" if height else "Live"
                valid_strms.append({
                    "url": s.get("url"),
                    "name": "IPTV-Org",
                    "title": f"Resolution: {res_str}",
                    "behaviorHints": {"filename": "live.m3u8"}
                })
            return addon.get("name", "Unknown"), valid_strms

        if (resources is None or addon_types is None or "idPrefixes" not in addon) and manifest_url:
            try:
                manifest_data = _get_cached_request(manifest_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass
                
        matched_media_type = actual_media
        if addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, actual_media)), None)
            if type_match:
                matched_media_type = type_match
            else:
                has_cat_match = any(is_type_match(cat.get("type"), actual_media) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(cur_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return addon.get("name", "Unknown"), []
            
        if addon_prefixes is not None:
            if not any(str(cur_id).startswith(p) for p in addon_prefixes):
                return addon.get("name", "Unknown"), []
                
        if not has_stream_resource(addon, media_type=actual_media, item_id=cur_id):
            return addon.get("name", "Unknown"), []
                
        if "manifest.json" in manifest_url:
            base_url = manifest_url.rsplit('manifest.json', 1)[0]
        else:
            base_url = manifest_url
        if not base_url.endswith('/'):
            base_url += '/'
            
        clean_cur_id = str(cur_id)
        if ":" in clean_cur_id:
            parts = clean_cur_id.split(":")
            if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
                clean_cur_id = ":".join(parts[:-2])
            elif len(parts) == 2 and parts[-1].isdigit() and parts[0].startswith("tt"):
                clean_cur_id = parts[0]

        resource_type = matched_media_type if matched_media_type in ["series", "tv", "anime", "tvshow"] else "series"
        if (matched_media_type in ["series", "tv", "anime", "tvshow"] or actual_media in ["series", "anime", "tv"]) and season is not None and episode is not None:
            url = f"{base_url}stream/{resource_type}/{urllib.parse.quote(clean_cur_id, safe=':')}:{season}:{episode}.json"
        else:
            url = f"{base_url}stream/{matched_media_type}/{urllib.parse.quote(str(cur_id), safe=':')}.json"
            
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
            with urllib.request.urlopen(req, timeout=8, context=_SSL_CONTEXT) as response:
                data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, dict):
                return addon.get("name", "Unknown"), data.get("streams", [])
            elif isinstance(data, list):
                return addon.get("name", "Unknown"), data
            return addon.get("name", "Unknown"), []
        except urllib.error.HTTPError as e:
            code = e.code
            logging.debug(f"HTTP Error {code} fetching from addon {addon.get('name')}: {url}")
            # 4xx means the addon is up but doesn't have this content — don't block it
            # 5xx / connection issues means the server is down
            if code >= 500:
                _record_addon_failure(manifest_url)
            else:
                _record_addon_success(manifest_url)  # 404 etc means addon is alive
            try:
                e.close()
            except Exception:
                pass
            return addon.get("name", "Unknown"), []
        except urllib.error.URLError as e:
            reason = str(e.reason)
            print(f"Error fetching from addon {addon.get('name')}: {reason}")
            _record_addon_failure(manifest_url)
            return addon.get("name", "Unknown"), []
        except TimeoutError as e:
            print(f"Timeout fetching from addon {addon.get('name')}")
            _record_addon_failure(manifest_url)
            return addon.get("name", "Unknown"), []
        except Exception as e:
            err_str = str(e).lower()
            print(f"Error fetching from addon {addon.get('name')}: {e}")
            # Only penalise on connection/timeout errors, not data parse issues
            if any(kw in err_str for kw in ["timed out", "timeout", "connection refused", "connection reset", "no route", "name or service"]):
                _record_addon_failure(manifest_url)
            return addon.get("name", "Unknown"), []

    all_raw_streams = []
    if cached:
        for c in cached:
            # Reconstitute the raw stream structure carefully.
            raw = {
                "infoHash": c.get("hash"),
                "url": c.get("url"),
                "ytId": c.get("ytId"),
                "is_trailer": c.get("is_trailer"),
                "name": c.get("title") or "",
                "title": c.get("stream_title") or "",
                "fileIdx": c.get("file_index"),
                "behaviorHints": c.get("behaviorHints") or ({"filename": c.get("filename")} if c.get("filename") else {}),
                "addon_name": c.get("addon_names")[0] if c.get("addon_names") else "Cache"
            }
            all_raw_streams.append(raw)

    final_streams = process_raw_streams(all_raw_streams, season=season, episode=episode, ep_title=ep_title)

    import concurrent.futures
    num_workers = min(len(all_stream_addons), 8) if all_stream_addons else 1
    if num_workers > 0 and all_stream_addons:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {}
            queried_addon_ids = set()
            
            # Submit immediately for any addon that directly supports initial imdb_id
            for addon in all_stream_addons:
                if has_stream_resource(addon, media_type=actual_media, item_id=imdb_id):
                    future_to_addon[executor.submit(fetch_from_addon, addon, imdb_id)] = (addon, imdb_id)
                    queried_addon_ids.add((addon.get("manifest_url", ""), str(imdb_id)))
                
            # Fire off ID resolution in the background
            def resolve_and_submit():
                from .tmdb_helper import resolve_all_provider_ids
                ids_to_fetch = resolve_all_provider_ids(imdb_id, media_type, title)
                return [i for i in ids_to_fetch if i != imdb_id]
                
            resolve_future = executor.submit(resolve_and_submit)
            
            futures = set(future_to_addon.keys())
            futures.add(resolve_future)
            
            while futures:
                if abort_event and abort_event.is_set():
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    return []
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED, timeout=12)
                if abort_event and abort_event.is_set():
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    return []
                if not done:
                    print("Timeout fetching streams")
                    break
                    
                for future in done:
                    if future == resolve_future:
                        try:
                            resolved_ids = future.result()
                            if resolved_ids:
                                for addon in all_stream_addons:
                                    for cur_id in resolved_ids:
                                        addon_key = (addon.get("manifest_url", ""), str(cur_id))
                                        if addon_key not in queried_addon_ids and has_stream_resource(addon, media_type=actual_media, item_id=cur_id):
                                            new_fut = executor.submit(fetch_from_addon, addon, cur_id)
                                            future_to_addon[new_fut] = (addon, cur_id)
                                            queried_addon_ids.add(addon_key)
                                            futures.add(new_fut)
                        except Exception as e:
                            print(f"Error resolving IDs: {e}")
                    else:
                        try:
                            addon_orig_info = future_to_addon.get(future, (None, None))
                            addon_obj = addon_orig_info[0] if addon_orig_info else None
                            addon_name, streams = future.result()
                            if streams:
                                # Successful response — clear any previous failure count
                                if addon_obj:
                                    _record_addon_success(addon_obj.get("manifest_url", ""))
                                for s in streams:
                                    s["addon_name"] = addon_name
                                    all_raw_streams.append(s)
                                if callback:
                                    current_parsed = process_raw_streams(list(all_raw_streams), season=season, episode=episode, ep_title=ep_title)
                                    callback(current_parsed, is_cached=False, is_complete=False)
                        except Exception as e:
                            print(f"Error in addon future: {e}")

    final_streams = process_raw_streams(all_raw_streams, season=season, episode=episode, ep_title=ep_title)

    if final_streams:
        database.save_cached_streams(cache_key, final_streams)
    elif all_stream_addons:
        database.delete_cached_streams(cache_key)

    if callback:
        callback(final_streams, is_cached=False, is_complete=True)

    return final_streams

def get_subtitles(imdb_id, media_type="movie", season=None, episode=None, stream_subtitles=None, title=None):
    all_subs = []
    if stream_subtitles and isinstance(stream_subtitles, list):
        for s in stream_subtitles:
            if isinstance(s, dict) and s.get("url"):
                all_subs.append(s)

    resolved_imdb = resolve_to_imdb_id(imdb_id, media_type, title=title) if (imdb_id or title) else None
    
    actual_media = "series" if media_type in ["series", "tv", "anime"] else media_type
    if season is not None and episode is not None:
        actual_media = "series"

    cache_key = f"subs_{resolved_imdb or imdb_id}_{actual_media}_{season}_{episode}"
    if not stream_subtitles:
        cached = database.get_cached_subtitles(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

    if resolved_imdb and str(resolved_imdb).startswith("tt"):
        clean_imdb = str(resolved_imdb).split(":")[0]
        if actual_media == "series" and season is not None and episode is not None:
            sub_path = f"series/{clean_imdb}:{int(season)}:{int(episode)}.json"
        else:
            sub_path = f"movie/{clean_imdb}.json"
            
        urls_to_try = [
            f"https://opensubtitles-v3.strem.io/subtitles/{sub_path}",
            f"https://subtitles.strem.io/subtitles/{sub_path}",
            f"https://opensubtitles.strem.io/subtitles/{sub_path}"
        ]
        try:
            installed_addons = [a for a in database.get_addons() if has_subtitles_resource(a, media_type=actual_media, item_id=resolved_imdb)]
            for addon in installed_addons:
                base_url = addon.get("url", "") or addon.get("manifest_url", "")
                base_url = base_url.rsplit("/manifest.json", 1)[0]
                if base_url:
                    u = f"{base_url}/subtitles/{sub_path}"
                    if u not in urls_to_try:
                        urls_to_try.append(u)
        except Exception:
            pass

        def _fetch_sub_url(sub_url):
            try:
                req = urllib.request.Request(
                    sub_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept-Encoding': 'gzip, deflate'
                    }
                )
                with urllib.request.urlopen(req, timeout=3.5, context=_SSL_CONTEXT) as response:
                    raw_data = response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    if encoding == "gzip" or raw_data.startswith(b"\x1f\x8b"):
                        import gzip
                        try:
                            raw_data = gzip.decompress(raw_data)
                        except Exception:
                            pass
                    data = json.loads(raw_data.decode('utf-8'))
                    return data.get("subtitles", [])
            except Exception as e:
                logging.debug(f"Error fetching subtitles from {sub_url}: {e}")
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls_to_try), 6)) as executor:
            fut_to_url = {executor.submit(_fetch_sub_url, u): u for u in urls_to_try}
            try:
                for fut in concurrent.futures.as_completed(fut_to_url, timeout=4.0):
                    try:
                        items = fut.result()
                        if items:
                            all_subs.extend(items)
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                pass

    # Deduplicate subtitles by URL to prevent loading identical tracks multiple times
    seen_urls = set()
    unique_subs = []
    for s in all_subs:
        url = s.get("url")
        if url:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_subs.append(s)
        else:
            unique_subs.append(s)
    all_subs = unique_subs

    pref_langs_str = ""
    try:
        import gi
        gi.require_version('Gio', '2.0')
        from gi.repository import Gio
        schema_source = Gio.SettingsSchemaSource.get_default()
        if schema_source and schema_source.lookup("io.github.fastrizwaan.PopcornBox", True):
            settings = Gio.Settings.new("io.github.fastrizwaan.PopcornBox")
            pref_langs_str = settings.get_string("subtitle-languages")
    except Exception:
        pass
        
    if not pref_langs_str:
        pref_langs_str = database.get_setting("subtitle-languages", "") or database.get_setting("subtitle_languages", "")
        
    raw_langs = [l.strip().lower() for l in pref_langs_str.replace(";", ",").split(',') if l.strip()]
    if not raw_langs:
        raw_langs = ["en", "eng", "english"]
        
    language_map = {
        "en": ["en", "eng", "english"], "eng": ["en", "eng", "english"], "english": ["en", "eng", "english"],
        "es": ["es", "spa", "esp", "spanish"], "spa": ["es", "spa", "esp", "spanish"], "spanish": ["es", "spa", "esp", "spanish"],
        "hi": ["hi", "hin", "hindi"], "hin": ["hi", "hin", "hindi"], "hindi": ["hi", "hin", "hindi"],
        "pt": ["pt", "por", "pob", "portuguese"], "por": ["pt", "por", "pob", "portuguese"], "portuguese": ["pt", "por", "pob", "portuguese"],
        "fr": ["fr", "fre", "fra", "french"], "fre": ["fr", "fre", "fra", "french"], "french": ["fr", "fre", "fra", "french"],
        "de": ["de", "ger", "deu", "german"], "ger": ["de", "ger", "deu", "german"], "german": ["de", "ger", "deu", "german"],
        "it": ["it", "ita", "italian"], "ita": ["it", "ita", "italian"], "italian": ["it", "ita", "italian"],
        "ru": ["ru", "rus", "russian"], "rus": ["ru", "rus", "russian"], "russian": ["ru", "rus", "russian"],
        "ar": ["ar", "ara", "arabic"], "ara": ["ar", "ara", "arabic"], "arabic": ["ar", "ara", "arabic"],
        "tr": ["tr", "tur", "turkish"], "tur": ["tr", "tur", "turkish"], "turkish": ["tr", "tur", "turkish"],
        "zh": ["zh", "chi", "zho", "chinese"], "chi": ["zh", "chi", "zho", "chinese"], "chinese": ["zh", "chi", "zho", "chinese"],
        "ja": ["ja", "jpn", "japanese"], "jpn": ["ja", "jpn", "japanese"], "japanese": ["ja", "jpn", "japanese"],
        "ko": ["ko", "kor", "korean"], "kor": ["ko", "kor", "korean"], "korean": ["ko", "kor", "korean"],
        "id": ["id", "ind", "indonesian"], "ind": ["id", "ind", "indonesian"], "indonesian": ["id", "ind", "indonesian"],
        "ml": ["ml", "mal", "malayalam"], "mal": ["ml", "mal", "malayalam"], "malayalam": ["ml", "mal", "malayalam"],
        "ta": ["ta", "tam", "tamil"], "tam": ["ta", "tam", "tamil"], "tamil": ["ta", "tam", "tamil"],
        "te": ["te", "tel", "telugu"], "tel": ["te", "tel", "telugu"], "telugu": ["te", "tel", "telugu"],
        "kn": ["kn", "kan", "kannada"], "kan": ["kn", "kan", "kannada"], "kannada": ["kn", "kan", "kannada"],
        "bn": ["bn", "ben", "bengali"], "ben": ["bn", "ben", "bengali"], "bengali": ["bn", "ben", "bengali"],
        "pa": ["pa", "pan", "punjabi"], "pan": ["pa", "pan", "punjabi"], "punjabi": ["pa", "pan", "punjabi"],
        "ur": ["ur", "urd", "urdu"], "urd": ["ur", "urd", "urdu"], "urdu": ["ur", "urd", "urdu"],
        "fa": ["fa", "per", "fas", "persian"], "per": ["fa", "per", "fas", "persian"], "persian": ["fa", "per", "fas", "persian"],
        "pl": ["pl", "pol", "polish"], "pol": ["pl", "pol", "polish"], "polish": ["pl", "pol", "polish"],
        "nl": ["nl", "dut", "nld", "dutch"], "dut": ["nl", "dut", "nld", "dutch"], "dutch": ["nl", "dut", "nld", "dutch"]
    }

    rank_sets = []
    for pl in raw_langs:
        clean_pl = pl.split("-")[0].split("_")[0]
        codes = language_map.get(clean_pl, language_map.get(pl, [clean_pl, pl]))
        rank_sets.append(set(codes))

    matched_subs = []
    for s in all_subs:
        raw_sub_lang = str(s.get("lang", "")).lower()
        sub_lang_clean = raw_sub_lang.split("-")[0].split("_")[0]
        matched_rank = 999
        for idx, rset in enumerate(rank_sets):
            if raw_sub_lang in rset or sub_lang_clean in rset or any(raw_sub_lang.startswith(p) for p in rset) or any(p in raw_sub_lang for p in rset):
                matched_rank = idx
                break
        if matched_rank < 999:
            matched_subs.append((matched_rank, s))
            
    if not matched_subs and all_subs:
        if all_subs:
            database.save_cached_subtitles(cache_key, all_subs)
        return all_subs

    matched_subs.sort(key=lambda x: x[0])
    res_list = [item[1] for item in matched_subs]
    if res_list:
        database.save_cached_subtitles(cache_key, res_list)
    return res_list

def download_subtitle(sub_url, filename):
    sub_dir = os.path.join(database.CONFIG_DIR, "subtitles")
    os.makedirs(sub_dir, exist_ok=True)
        
    if not filename.endswith('.srt') and not filename.endswith('.vtt'):
        filename += '.srt'
        
    filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
    file_path = os.path.join(sub_dir, filename)
    
    try:
        req = urllib.request.Request(
            sub_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Encoding': 'gzip, deflate'
            }
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as response:
            data = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
                import gzip
                try:
                    data = gzip.decompress(data)
                except Exception as ge:
                    logging.debug(f"Gzip decompression error: {ge}")
            elif encoding == "deflate":
                import zlib
                try:
                    data = zlib.decompress(data)
                except Exception:
                    pass
            with open(file_path, 'wb') as f:
                f.write(data)
        return file_path
    except urllib.error.HTTPError as e:
        logging.debug(f"HTTP Error {e.code} downloading subtitle")
        try:
            e.close()
        except Exception:
            pass
        return None
    except Exception as e:
        logging.debug(f"Error downloading subtitle: {e}")
        return None

def download_subtitle_to_path(sub_url, file_path):
    dir_name = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    base_name = "".join([c for c in base_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '(', ')', '[', ']')]).rstrip()
    
    file_path = os.path.join(dir_name, base_name)
    os.makedirs(dir_name, exist_ok=True)
    
    try:
        req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=10) as response:
            with open(file_path, 'wb') as f:
                f.write(response.read())
        return file_path
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} downloading subtitle")
        e.close()
        return None
    except Exception as e:
        print(f"Error downloading subtitle: {e}")
        return None

def build_magnet(hash_string, title, sources=None):
    title = title or ""
    encoded_title = urllib.parse.quote(title)
    trackers = []
    seen = set()
    if sources and isinstance(sources, list):
        for src in sources:
            if not isinstance(src, str):
                continue
            t = src.strip()
            if t.startswith("tracker:"):
                t = t[len("tracker:"):].strip()
            elif t.startswith("dht:"):
                continue
            if t and (t.startswith("udp://") or t.startswith("http://") or t.startswith("https://")):
                if t not in seen:
                    seen.add(t)
                    trackers.append(t)
    for dt in DEFAULT_TRACKERS:
        if dt not in seen:
            seen.add(dt)
            trackers.append(dt)
    tracker_str = "&tr=".join([urllib.parse.quote(t, safe="") for t in trackers])
    return f"magnet:?xt=urn:btih:{hash_string}&dn={encoded_title}&tr={tracker_str}"

def add_trackers_to_magnet(magnet, sources=None):
    if not magnet or not magnet.startswith("magnet:?"):
        return magnet
    new_trackers = []
    seen = set()
    if sources and isinstance(sources, list):
        for src in sources:
            if not isinstance(src, str):
                continue
            t = src.strip()
            if t.startswith("tracker:"):
                t = t[len("tracker:"):].strip()
            elif t.startswith("dht:"):
                continue
            if t and (t.startswith("udp://") or t.startswith("http://") or t.startswith("https://")):
                if t not in seen:
                    seen.add(t)
                    new_trackers.append(t)
    for dt in DEFAULT_TRACKERS:
        if dt not in seen:
            seen.add(dt)
            new_trackers.append(dt)
    for t in new_trackers:
        enc = urllib.parse.quote(t, safe="")
        if enc not in magnet and t not in magnet:
            magnet += f"&tr={enc}"
    return magnet

def is_stremio_server_running(host="127.0.0.1", port=11470, timeout=0.3):
    """Check if Stremio Streaming Server is active on local machine."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        res = sock.connect_ex((host, port))
        sock.close()
        return res == 0
    except Exception:
        return False

def build_stremio_stream_url(info_hash, file_index=None, sources=None, host="127.0.0.1", port=11470):
    f_idx = file_index if file_index is not None and str(file_index).isdigit() and int(file_index) >= 0 else -1
    base_url = f"http://{host}:{port}/{info_hash}/{f_idx}"
    query_parts = []
    if sources and isinstance(sources, list):
        for s in sources:
            if not isinstance(s, str) or not s.strip():
                continue
            tr = s.strip()
            if tr.startswith("tracker:"):
                query_parts.append(f"tr={urllib.parse.quote(tr, safe='')}")
            elif tr.startswith("dht:"):
                query_parts.append(f"dht={urllib.parse.quote(tr[4:], safe='')}")
            elif tr.startswith(("udp://", "http://", "https://")):
                query_parts.append(f"tr={urllib.parse.quote('tracker:' + tr, safe='')}")
    if query_parts:
        base_url += "?" + "&".join(query_parts)
    return base_url

