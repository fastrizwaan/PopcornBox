import json
import urllib.request
import urllib.parse
import urllib.error
import os
import time
import hashlib
import logging
import ssl
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

def _get_cached_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=5):
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, url_hash)
    
    if headers is None:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    # Check if cache exists and is fresh
    if os.path.exists(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.debug(f"Cache corrupted, falling back to fetch: {e}")
                
    if cache_only:
        return None
        
    # Fetch from network
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
            data_str = response.read().decode('utf-8')
        data = json.loads(data_str)
        # Save to cache atomically (temp file + rename)
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
        print(f"HTTP Error {e.code} fetching from {url}")
        try:
            e.close()
        except Exception:
            pass
    except urllib.error.URLError as e:
        print(f"URL/SSL Error fetching from {url}: {e.reason}")
    except Exception as e:
        print(f"Error fetching items from {url}: {e}")
        
    # Return stale cache if network fails
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
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

_ADDON_ONLINE_STATUS = {}
_ADDON_ONLINE_LOCK = threading.Lock()

def set_addon_online_status(manifest_url, is_online):
    if not manifest_url: return
    with _ADDON_ONLINE_LOCK:
        _ADDON_ONLINE_STATUS[manifest_url] = is_online

def is_addon_online(manifest_url):
    if not manifest_url or manifest_url.startswith("builtin:"):
        return True
    with _ADDON_ONLINE_LOCK:
        if manifest_url in _ADDON_ONLINE_STATUS:
            return _ADDON_ONLINE_STATUS[manifest_url]
    return True

def get_available_catalogs(c_type="movie"):
    from . import database
    catalogs = []
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    
    for addon in addons:
        addon_name = addon.get("name", "Unknown Addon")
        manifest_url = addon.get("manifest_url", "")
        if not manifest_url or manifest_url.startswith("builtin:"):
            continue
            
        if not is_addon_online(manifest_url):
            continue

        base_url = manifest_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"): base_url += "/"
            
        addon_catalogs = addon.get("catalogs", [])
        for cat in addon_catalogs:
            cat_type = cat.get("type")
            cat_name = cat.get("name") or ""
            cat_id = cat.get("id", "")

            matched = is_type_match(cat_type, c_type)
            if not matched and c_type == "anime":
                if "anime" in str(cat_type).lower() or "anime" in cat_name.lower() or "anime" in cat_id.lower() or "anime" in addon_name.lower():
                    matched = True

            if matched:
                genres = []
                extra = cat.get("extra") or []
                for ex in extra:
                    if isinstance(ex, dict) and ex.get("name") == "genre":
                        genres = ex.get("options", [])
                        
                if not cat_name or cat_name.lower() == "catalog":
                    display_name = f"{addon_name} - {cat_id}"
                else:
                    display_name = f"{addon_name} - {cat_name}" if addon_name.lower() not in cat_name.lower() else cat_name
                    if "tpbctlg" in display_name.lower():
                         display_name = f"{display_name} ({cat_id})"
                
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
    return catalogs

def _get_search_catalogs_for_addon(addon, c_type, cache_only=False):
    catalogs = addon.get("catalogs", [])
    if not catalogs:
        m_url = addon.get("manifest_url", "")
        if m_url and not m_url.startswith("builtin:"):
            try:
                manifest_data = _get_cached_request(m_url, max_age_hours=168, cache_only=cache_only, timeout=3)
                if manifest_data:
                    catalogs = manifest_data.get("catalogs", [])
            except Exception:
                pass

    search_cats = []
    for cat in catalogs:
        cat_type = cat.get("type")
        if cat_type and not is_type_match(cat_type, c_type):
            continue
            
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        extra = cat.get("extra") or []
        extra_sup = cat.get("extraSupported") or []
        
        is_search = False
        if addon.get("id") == "cinemeta" and cat_id == "top":
            is_search = True
        elif "search" in str(cat_id).lower() or "search" in str(cat_name).lower():
            is_search = True
        elif "search" in extra_sup:
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
            if cat.get("type") == c_type:
                search_cats.append(cat.get("id"))
                
    return search_cats


def fetch_items(media_type="movie", query="", genre="", catalog_id="top", catalog_url=None, limit=50, page=1, cache_only=False, on_item_found=None, target_manifest_url=None, target_catalog_id=None):
    c_type = "series" if media_type == "series" else media_type
    skip = (page - 1) * 50

    if query:
        import concurrent.futures
        items = []
        seen_ids = set()
        seen_titles = {}

        
        def fetch_addon_search(addon):
            if not addon.get("enabled", True): return []
            m_url = addon.get("manifest_url", "")
            if not m_url or m_url.startswith("builtin:"): return []
            
            if target_manifest_url and m_url != target_manifest_url:
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
                search_url = f"{base_url}catalog/{c_type}/{urllib.parse.quote(str(cat_id), safe=':')}/search={urllib.parse.quote(query)}.json"
                data = _get_cached_request(search_url, max_age_hours=2, cache_only=cache_only, timeout=3)
                if data and isinstance(data.get("metas"), list):
                    addon_items.extend(data["metas"])
                    if addon_items: 
                        break # Break early if we found results for this addon
            return addon_items

        addons_to_search = [a for a in database.get_addons() if not target_manifest_url or a.get("manifest_url") == target_manifest_url]
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        future_to_addon = {executor.submit(fetch_addon_search, addon): addon for addon in addons_to_search}
        try:
            for future in concurrent.futures.as_completed(future_to_addon, timeout=8):
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
                for cat in a.get("catalogs", []):
                    cat_type = cat.get("type")
                    if str(cat.get("id")) == str(catalog_id) and is_type_match(cat_type, c_type):
                        actual_cat_type = cat_type or c_type
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
            
        data = _get_cached_request(url, max_age_hours=2, cache_only=cache_only)
        if data is None:
            return None
            
        if isinstance(data.get("metas"), list):
            from .movie_widget import extract_image_url
            movies = []
            for m in data["metas"]:
                if database.is_adult_content_hidden() and is_adult_item(m):
                    continue
                imdb_id = m.get("imdb_id") or m.get("id")
                poster = extract_image_url(m)
                title = m.get("name", "")
                year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""
                movies.append({
                    "id": imdb_id,
                    "title": title,
                    "year": year,
                    "medium_cover_image": poster,
                    "poster": poster,
                    "type": m.get("type") or media_type
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
    return True

def has_meta_resource(addon):
    """Return True if the addon explicitly supports metadata ('meta') resource."""
    if not isinstance(addon, dict): return False
    m_url = addon.get("manifest_url", "")
    if addon.get("id") == "cinemeta" or "cinemeta" in m_url.lower():
        return True
    resources = addon.get("resources")
    if resources is None:
        return True
    for r in resources:
        name = r.get("name") if isinstance(r, dict) else r
        if name == "meta":
            return True
    return False

def has_stream_resource(addon):
    """Return True if the addon explicitly supports stream ('stream') resource."""
    if not isinstance(addon, dict): return False
    m_url = addon.get("manifest_url", "")
    if addon.get("id") == "cinemeta" or "cinemeta" in m_url.lower():
        return False  # Cinemeta is metadata/catalog only
    if addon.get("id") == "local.iptv-org":
        return True
    resources = addon.get("resources")
    if resources is None:
        return True
    for r in resources:
        name = r.get("name") if isinstance(r, dict) else r
        if name == "stream":
            return True
    return False

def _save_and_return_meta(res, imdb_id, media_type="movie", title=None, poster=None):
    if not res or not isinstance(res, dict):
        return res

    # Preserve existing valid poster from parameter or database cache before replacing with addon poster
    existing = database.get_cached_metadata(imdb_id)
    existing_poster = poster or (existing.get("medium_cover_image") if existing else None)

    if existing_poster:
        res["medium_cover_image"] = existing_poster
        print(f"[CACHE PROTECT] Preserved existing poster for {imdb_id}: {existing_poster}")
    else:
        current_poster = res.get("medium_cover_image", "")
        if not current_poster and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
            # 1. Try TMDB addon first
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                tmdb_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
                tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168, timeout=4)
                if tmdb_data and "meta" in tmdb_data and tmdb_data["meta"].get("poster"):
                    res["medium_cover_image"] = tmdb_data["meta"]["poster"]
                    if tmdb_data["meta"].get("background"):
                        res["background"] = tmdb_data["meta"]["background"]
                    print(f"[TMDB Fallback] Successfully updated poster for {imdb_id}: {res['medium_cover_image']}")
            except Exception as e:
                print(f"[TMDB Fallback] Failed for {imdb_id}: {e}")

            # 2. Try IMDb API by title search
            if not res.get("medium_cover_image") and title:
                try:
                    clean_title = re.sub(r'[^a-zA-Z0-9]', '_', title).lower()
                    first_char = clean_title[0] if clean_title else "t"
                    imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(clean_title)}.json"
                    req = urllib.request.Request(imdb_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
                    with urllib.request.urlopen(req, timeout=4) as response:
                        data = json.loads(response.read().decode('utf-8', errors='ignore'))
                        if data and "d" in data:
                            for item in data["d"]:
                                if "i" in item and "imageUrl" in item["i"]:
                                    p_url = item["i"]["imageUrl"]
                                    p_url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', p_url)
                                    res["medium_cover_image"] = p_url
                                    if not res.get("background"):
                                        res["background"] = p_url
                                    print(f"[IMDb API] Successfully fetched poster for {imdb_id}: {p_url}")
                                    break
                except Exception as e:
                    print(f"[IMDb API] Failed for {imdb_id}: {e}")

    if existing and existing.get("background") and not res.get("background"):
        res["background"] = existing["background"]

    database.save_cached_metadata(imdb_id, media_type, res)
    if res.get("id") and res.get("id") != imdb_id:
        database.save_cached_metadata(res.get("id"), media_type, res)
    return res

def fetch_movie_details(imdb_id, media_type="movie", title=None, use_cache=True, poster=None):
    if isinstance(imdb_id, list):
        if not imdb_id: return {}
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

    c_type = media_type

    def fetch_addon_meta(addon_orig):
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
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
                
        matched_type = c_type
        if addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, c_type)), None)
            if type_match:
                matched_type = type_match
            else:
                has_cat_match = any(is_type_match(cat.get("type"), c_type) for cat in addon.get("catalogs", []))
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
        cinemeta_res = fetch_addon_meta(cinemeta_addon)
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

    return _save_and_return_meta({
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
        "videos": []
    }, imdb_id, media_type, title, poster=poster)

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

_RE_SIZE = re.compile(r'([\d.]+)\s*([GgMm][Bb])')
_RE_SEED = re.compile(r'(?:👤|👥|[Ss]eeders?[:\s]*)\s*(\d+)')

def process_raw_streams(all_streams):
    if not all_streams:
        return []
    valid_streams = []
    seen_keys = {}  # {dedup_key: index in valid_streams} for O(1) duplicate lookup
    for s in all_streams:
        stream_url = str(s.get("url") or s.get("externalUrl") or "")
        info_hash = str(s.get("infoHash") or "").lower()
        
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
        
        size = ""
        size_gb = 0.0
        size_match = _RE_SIZE.search(title_str)
        if size_match:
            unit = size_match.group(2).upper()
            try:
                val = float(size_match.group(1))
                size = f"{size_match.group(1)} {unit}"
                size_gb = val / 1024.0 if unit == "MB" else val
            except ValueError:
                pass
            
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
            
        valid_streams.append({
            "hash": raw_id,
            "url": stream_url,
            "is_http": is_http,
            "quality": quality,
            "q_val": q_val,
            "size": size,
            "size_gb": size_gb,
            "seeders": seeders,
            "title": s.get("name") or "",
            "stream_title": full_title,
            "file_index": s.get("fileIdx"),
            "filename": filename,
            "behaviorHints": behavior_hints,
            "addon_names": [s.get("addon_name")] if s.get("addon_name") else []
        })
    
    valid_streams.sort(key=lambda x: (x.get("seeders", 0), x.get("q_val", 0), x.get("size_gb", 0.0)), reverse=True)
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
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://") and has_stream_resource(a)]
    
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
                # Allow if a catalog or idPrefix matches as fallback
                has_cat_match = any(is_type_match(cat.get("type"), actual_media) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(imdb_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return addon.get("name", "Unknown"), []

        if addon_prefixes is not None:
            if not any(str(imdb_id).startswith(p) for p in addon_prefixes):
                return addon.get("name", "Unknown"), []
                
        if resources is not None:
            has_stream = False
            for r in resources:
                if isinstance(r, str) and r == "stream":
                    has_stream = True
                elif isinstance(r, dict) and r.get("name") == "stream":
                    has_stream = True
            if not has_stream:
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
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, dict):
                return addon.get("name", "Unknown"), data.get("streams", [])
            elif isinstance(data, list):
                return addon.get("name", "Unknown"), data
            return addon.get("name", "Unknown"), []
        except urllib.error.HTTPError as e:
            print(f"HTTP Error {e.code} fetching from addon {addon.get('name')}")
            e.close()
            return addon.get("name", "Unknown"), []
        except Exception as e:
            print(f"Error fetching from addon {addon.get('name')}: {e}")
            return addon.get("name", "Unknown"), []
            
    all_streams = []
    num_workers = min(len(stremio_addons), 30)
    if num_workers > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {executor.submit(fetch_from_addon, addon): addon for addon in stremio_addons}
                
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=20):
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
            
    valid_streams = process_raw_streams(all_streams)
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

def get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None, callback=None, title=None):
    if not imdb_id:
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
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://") and has_stream_resource(a)]

    def fetch_from_addon(addon_orig, cur_id):
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
            is_custom_id = ":" in str(cur_id) or not str(cur_id).startswith("tt")
            if not is_custom_id and not any(str(cur_id).startswith(p) for p in addon_prefixes):
                return addon.get("name", "Unknown"), []
                
        if resources is not None:
            has_stream = False
            for r in resources:
                if isinstance(r, str) and r == "stream":
                    has_stream = True
                elif isinstance(r, dict) and r.get("name") == "stream":
                    has_stream = True
            if not has_stream:
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

        if matched_media_type == "series" and season is not None and episode is not None:
            url = f"{base_url}stream/series/{urllib.parse.quote(clean_cur_id, safe=':')}:{season}:{episode}.json"
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
            print(f"HTTP Error {e.code} fetching from addon {addon.get('name')}")
            try:
                e.close()
            except Exception:
                pass
            return addon.get("name", "Unknown"), []
        except urllib.error.URLError as e:
            print(f"Error fetching from addon {addon.get('name')}: {e.reason}")
            return addon.get("name", "Unknown"), []
        except Exception as e:
            print(f"Error fetching from addon {addon.get('name')}: {e}")
            return addon.get("name", "Unknown"), []

    all_raw_streams = []
    if cached:
        for c in cached:
            # Reconstitute the raw stream structure carefully.
            raw = {
                "infoHash": c.get("hash"),
                "url": c.get("url"),
                "name": c.get("title") or "",
                "title": c.get("stream_title") or "",
                "fileIdx": c.get("file_index"),
                "behaviorHints": {"filename": c.get("filename")},
                "addon_name": c.get("addon_names")[0] if c.get("addon_names") else "Cache"
            }
            all_raw_streams.append(raw)

    final_streams = process_raw_streams(all_raw_streams)

    import concurrent.futures
    num_workers = min(len(stremio_addons), 8) if stremio_addons else 1
    if num_workers > 0 and stremio_addons:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {}
            
            # Submit immediately for the known item_id
            for addon in stremio_addons:
                future_to_addon[executor.submit(fetch_from_addon, addon, imdb_id)] = (addon, imdb_id)
                
            # Fire off ID resolution in the background
            def resolve_and_submit():
                from .tmdb_helper import resolve_all_provider_ids
                ids_to_fetch = resolve_all_provider_ids(imdb_id, media_type, title)
                return [i for i in ids_to_fetch if i != imdb_id]
                
            resolve_future = executor.submit(resolve_and_submit)
            
            futures = set(future_to_addon.keys())
            futures.add(resolve_future)
            
            while futures:
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED, timeout=12)
                if not done:
                    print("Timeout fetching streams")
                    break
                    
                for future in done:
                    if future == resolve_future:
                        try:
                            resolved_ids = future.result()
                            if resolved_ids:
                                for addon in stremio_addons:
                                    for cur_id in resolved_ids[:1]:
                                        new_fut = executor.submit(fetch_from_addon, addon, cur_id)
                                        future_to_addon[new_fut] = (addon, cur_id)
                                        futures.add(new_fut)
                        except Exception as e:
                            print(f"Error resolving IDs: {e}")
                    else:
                        try:
                            addon_name, streams = future.result()
                            if streams:
                                for s in streams:
                                    s["addon_name"] = addon_name
                                    all_raw_streams.append(s)
                                if callback:
                                    current_parsed = process_raw_streams(all_raw_streams)
                                    callback(current_parsed, is_cached=False, is_complete=False)
                        except Exception as e:
                            print(f"Error in addon future: {e}")

    final_streams = process_raw_streams(all_raw_streams)

    if final_streams:
        # Recompute the cache key for saving just in case resolve found a better primary ID, 
        # but storing it under imdb_id is also completely fine.
        database.save_cached_streams(cache_key, final_streams)
    elif stremio_addons:
        database.delete_cached_streams(cache_key)

    if callback:
        callback(final_streams, is_cached=False, is_complete=True)

    return final_streams

def get_subtitles(imdb_id, media_type="movie", season=None, episode=None):
    if not imdb_id:
        return []
        
    actual_media = "series" if media_type == "tv" else media_type
    if actual_media == "series" and season is not None and episode is not None:
        sub_path = f"series/{imdb_id}:{season}:{episode}.json"
    else:
        sub_path = f"movie/{imdb_id}.json"
        
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
        
    pref_langs = [l.strip().lower() for l in pref_langs_str.split(',') if l.strip()]
    if not pref_langs:
        pref_langs = ["en", "eng", "english"]
        
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
    for pl in pref_langs:
        codes = language_map.get(pl, [pl])
        rank_sets.append(set(codes))

    urls_to_try = [f"https://opensubtitles-v3.strem.io/subtitles/{sub_path}"]
    try:
        installed_addons = database.get_addons()
        for addon in installed_addons:
            resources = addon.get("resources", [])
            has_subs = False
            for r in resources:
                if isinstance(r, dict) and r.get("name") == "subtitles":
                    has_subs = True
                    break
                elif isinstance(r, str) and r == "subtitles":
                    has_subs = True
                    break
            if has_subs:
                base_url = addon.get("url", "").rsplit("/manifest.json", 1)[0]
                if base_url:
                    urls_to_try.append(f"{base_url}/subtitles/{sub_path}")
    except Exception:
        pass

    all_subs = []
    for sub_url in urls_to_try:
        try:
            req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode('utf-8'))
                items = data.get("subtitles", [])
                all_subs.extend(items)
        except Exception as e:
            print(f"Error fetching subtitles from {sub_url}: {e}")

    matched_subs = []
    for s in all_subs:
        sub_lang = str(s.get("lang", "")).lower()
        matched_rank = 999
        for idx, rset in enumerate(rank_sets):
            if sub_lang in rset or any(sub_lang.startswith(p) for p in rset) or any(p in sub_lang for p in rset):
                matched_rank = idx
                break
        if matched_rank < 999:
            matched_subs.append((matched_rank, s))
            
    if not matched_subs and all_subs:
        # Fallback to returning all available subtitles if preferred language doesn't match
        return all_subs

    matched_subs.sort(key=lambda x: x[0])
    return [item[1] for item in matched_subs]

def download_subtitle(sub_url, filename):
    import gi
    gi.require_version('GLib', '2.0')
    from gi.repository import GLib
    download_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    if not download_dir:
        download_dir = os.path.expanduser("~/Downloads")
        os.makedirs(download_dir, exist_ok=True)
        
    if not filename.endswith('.srt'):
        filename += '.srt'
        
    filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
    file_path = os.path.join(download_dir, filename)
    
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

def build_magnet(hash_string, title):
    title = title or ""
    encoded_title = urllib.parse.quote(title)
    tracker_str = "&tr=".join([urllib.parse.quote(t) for t in DEFAULT_TRACKERS])
    return f"magnet:?xt=urn:btih:{hash_string}&dn={encoded_title}&tr={tracker_str}"
