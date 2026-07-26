import json
import urllib.request
import urllib.parse
import urllib.error
import os
import time
import hashlib
import logging
from . import database
from .tmdb_helper import resolve_to_imdb_id
import concurrent.futures

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "udp://tracker.dler.com:6969/announce",
    "http://tracker.bt4g.com:2095/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "http://ipv4announce.sktorrent.eu:6969/announce",
    "http://tracker.mywaifu.best:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://evan.im:6969/announce",
    "udp://bittorrent-tracker.e-n-c-r-y-p-t.net:1337/announce",
    "udp://martin-gebhardt.eu:25/announce",
    "udp://tracker.opentorrent.top:6969/announce",
    "udp://ns575949.ip-51-222-82.net:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "https://tracker.manager.v6.navy:443/announce",
    "https://tracker.7471.top:443/announce",
    "udp://open.ftorrent.com:443/announce",
    "https://tracker.anibt.net:443/announce",
    "https://orgtgju.org:443/announce",
    "https://banananetwork.qzz.io:443/announce",
    "https://021912.xyz:443/announce",
    "https://ht.therarbg.to:443/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.ilibr.org:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
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
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data_str = response.read().decode('utf-8')
        data = json.loads(data_str)
        # Save to cache
        with open(cache_file, 'w', encoding='utf-8') as f:
            f.write(data_str)
        return data
    except urllib.error.HTTPError as e:
        print(f"HTTP Error fetching items from {url}: {e}")
        e.close()
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

def get_available_catalogs(c_type="movie"):
    from . import database
    catalogs = []
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    
    for addon in addons:
        addon_name = addon.get("name", "Unknown Addon")
        manifest_url = addon.get("manifest_url", "")
        if not manifest_url or manifest_url.startswith("builtin:"):
            continue
            
        base_url = manifest_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"): base_url += "/"
            
        addon_catalogs = addon.get("catalogs", [])
        for cat in addon_catalogs:
            if cat.get("type") == c_type:
                genres = []
                extra = cat.get("extra") or []
                for ex in extra:
                    if isinstance(ex, dict) and ex.get("name") == "genre":
                        genres = ex.get("options", [])
                        
                cat_name = cat.get("name") or ""
                cat_id = cat.get("id", "")
                
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
                    "type": c_type
                })
    return catalogs

def _get_search_catalogs_for_addon(addon, c_type, cache_only=False):
    catalogs = addon.get("catalogs", [])
    if not catalogs:
        m_url = addon.get("manifest_url", "")
        if m_url and not m_url.startswith("builtin:"):
            try:
                manifest_data = _get_cached_request(m_url, max_age_hours=168, cache_only=cache_only, timeout=3)
                if manifest_data and "catalogs" in manifest_data:
                    catalogs = manifest_data["catalogs"]
            except Exception:
                pass

    search_cats = []
    for cat in catalogs:
        cat_type = cat.get("type")
        if cat_type != c_type:
            continue
            
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        extra = cat.get("extra", [])
        extra_sup = cat.get("extraSupported", [])
        
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
    c_type = media_type
    skip = (page - 1) * 50

    if query:
        import concurrent.futures
        items = []
        seen_ids = set()
        
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
                search_url = f"{base_url}catalog/{c_type}/{cat_id}/search={urllib.parse.quote(query)}.json"
                data = _get_cached_request(search_url, max_age_hours=2, cache_only=cache_only, timeout=4)
                if data and isinstance(data.get("metas"), list):
                    addon_items.extend(data["metas"])
            return addon_items

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            future_to_addon = {executor.submit(fetch_addon_search, addon): addon for addon in database.get_addons()}
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=12):
                    try:
                        addon_items = future.result()
                        new_batch = []
                        q_lower = query.lower()
                        for m in addon_items:
                            title = m.get("name", "")
                            desc = m.get("description", "")
                            if q_lower not in title.lower() and q_lower not in desc.lower():
                                continue
                                
                            imdb_id = m.get("imdb_id") or m.get("id")
                            if imdb_id and imdb_id not in seen_ids:
                                seen_ids.add(imdb_id)
                                item_obj = {
                                    "id": imdb_id,
                                    "title": title,
                                    "year": str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else "",
                                    "medium_cover_image": m.get("poster", ""),
                                    "type": media_type
                                }
                                items.append(item_obj)
                                new_batch.append(item_obj)
                        if new_batch and on_item_found:
                            on_item_found(new_batch)
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                pass
                    
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
                if ch.get("country") == country_code:
                    movies.append({
                        "id": ch.get("id"),
                        "title": ch.get("name"),
                        "year": "",
                        "medium_cover_image": ch.get("logo", ""),
                        "type": "tv"
                    })
            if genre and genre != "All":
                movies = [m for m in movies if genre.lower() in str(ch.get("categories", [])).lower()]
            return movies[skip:skip+100]

        base_url = catalog_url
        if "manifest.json" in base_url:
            base_url = base_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"):
            base_url += "/"
            
        url = f"{base_url}catalog/{c_type}/{catalog_id}"
        
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
        if data and isinstance(data.get("metas"), list):
            movies = []
            for m in data["metas"]:
                imdb_id = m.get("imdb_id") or m.get("id")
                poster = m.get("poster", "")
                title = m.get("name", "")
                year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""
                movies.append({
                    "id": imdb_id,
                    "title": title,
                    "year": year,
                    "medium_cover_image": poster,
                    "type": media_type
                })
            return movies

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

def fetch_movie_details(imdb_id, media_type="movie", title=None, use_cache=True):
    if use_cache and imdb_id:
        cached = database.get_cached_metadata(imdb_id)
        if cached and is_valid_meta(cached):
            return cached

    c_type = "series" if media_type in ["series", "anime"] else media_type
    
    if media_type == "tv":
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
                    
def fetch_movie_details(imdb_id, media_type="movie", title=None, use_cache=True):
    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
    
    # Resolve TMDB ids to IMDB format if needed
    imdb_id = resolve_to_imdb_id(imdb_id, media_type, title)

    if use_cache:
        cached = database.get_cached_metadata(imdb_id)
        if cached and is_valid_meta(cached):
            return cached

    def fetch_addon_meta(addon):
        if not addon.get("enabled", True): return None
        m_url = addon.get("manifest_url", "")
        if not m_url: return None
        
        if addon.get("id") == "local.iptv-org":
            return None

        if m_url.startswith("builtin:"): return None
        
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
                
        if addon_types is not None and not any(t in addon_types for t in [c_type, media_type, "anime" if media_type == "anime" else ""]):
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
        
        meta_type = "anime" if (media_type == "anime" or str(imdb_id).startswith("kitsu:")) else c_type
        meta_url = f"{base_url}meta/{meta_type}/{imdb_id}.json"
        data = _get_cached_request(meta_url, max_age_hours=168)
        if (not data or not data.get("meta")) and meta_type != c_type:
            meta_url = f"{base_url}meta/{c_type}/{imdb_id}.json"
            data = _get_cached_request(meta_url, max_age_hours=168)
        
        if data and data.get("meta"):
            cm = data["meta"]
            t_val = cm.get("name", "")
            if not t_val or "error getting meta" in t_val.lower() or t_val.lower().startswith("error"):
                return None

            videos = []
            for v in cm.get("videos", []):
                videos.append({
                    "season": v.get("season", 1),
                    "episode": v.get("episode", 1),
                    "title": v.get("title", ""),
                    "overview": v.get("overview", "")
                })
            
            true_id = cm.get("imdb_id") or imdb_id
            if str(true_id).startswith("tmdb:") or str(true_id).startswith("ctmdb."):
                m_title = cm.get("name")
                if m_title:
                    try:
                        import urllib.parse
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
                        
            res_dict = {
                "id": true_id,
                "title": cm.get("name", ""),
                "year": str(cm.get("releaseInfo", "")).split("-")[0] if cm.get("releaseInfo") else "",
                "medium_cover_image": cm.get("poster", ""),
                "background": cm.get("background", ""),
                "description": cm.get("description", "No synopsis available."),
                "runtime": cm.get("runtime", ""),
                "genre": ", ".join(cm.get("genres", [])),
                "imdbRating": str(cm.get("imdbRating", "")),
                "trailer": cm.get("trailers", [{"source": ""}])[0].get("source") if cm.get("trailers") else None,
                "videos": videos,
                "cast": cm.get("cast", [])
            }
            return res_dict
            
        return None

    addons = database.get_addons()
    cinemeta_addon = next((a for a in addons if a.get("id") == "cinemeta" or "cinemeta" in a.get("manifest_url", "").lower()), None)
    if cinemeta_addon:
        cinemeta_res = fetch_addon_meta(cinemeta_addon)
        if cinemeta_res and is_valid_meta(cinemeta_res):
            database.save_cached_metadata(imdb_id, media_type, cinemeta_res)
            if cinemeta_res.get("id") and cinemeta_res.get("id") != imdb_id:
                database.save_cached_metadata(cinemeta_res.get("id"), media_type, cinemeta_res)
            return cinemeta_res

    other_addons = [a for a in addons if a != cinemeta_addon]
    if other_addons:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(other_addons), 8)) as executor:
            future_to_addon = {executor.submit(fetch_addon_meta, addon): addon for addon in other_addons}
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=15):
                    res = future.result()
                    if res and is_valid_meta(res):
                        database.save_cached_metadata(imdb_id, media_type, res)
                        if res.get("id") and res.get("id") != imdb_id:
                            database.save_cached_metadata(res.get("id"), media_type, res)
                        return res
            except concurrent.futures.TimeoutError:
                pass

    if title and title != "Loading...":
        return {
            "id": imdb_id,
            "title": title,
            "year": "",
            "medium_cover_image": "",
            "background": "",
            "description": "Synopsis temporarily unavailable.",
            "runtime": "",
            "genre": "",
            "imdbRating": "",
            "trailer": None,
            "videos": []
        }

    return {}

def find_episode_file_index(files, season, episode):
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
    if season is not None and episode is not None:
        return f"{imdb_id}:S{season}:E{episode}"
    return f"{imdb_id}:{media_type}"

def process_raw_streams(all_streams):
    if not all_streams:
        return []
    import re
    valid_streams = []
    seen_hashes = set()
    for s in all_streams:
        is_http = bool(s.get("url"))
        if not s.get("infoHash") and not is_http:
            continue
            
        stream_id = s.get("infoHash", "").lower() if not is_http else hashlib.md5(s["url"].encode()).hexdigest()
        if stream_id in seen_hashes:
            for vs in valid_streams:
                if vs["hash"].lower() == stream_id:
                    if s.get("addon_name") and s["addon_name"] not in vs["addon_names"]:
                        vs["addon_names"].append(s["addon_name"])
                    break
            continue
        seen_hashes.add(stream_id)
        
        desc_str = s.get("title") or s.get("description") or ""
        name_str = s.get("name") or ""
        
        if desc_str and name_str and name_str not in desc_str:
            full_title = f"{name_str}\n{desc_str}"
        else:
            full_title = desc_str or name_str
            
        title_str = desc_str
        name_and_title = (name_str + " " + title_str)
        
        quality = "Unknown"
        q_val = 0
        lower_name = name_and_title.lower()
        
        if "2160p" in lower_name:
            quality = "4K"
            q_val = 4
        elif "1080p" in lower_name: 
            quality = "1080p"
            q_val = 3
        elif "720p" in lower_name: 
            quality = "720p"
            q_val = 2
        elif "480p" in lower_name:
            quality = "480p"
            q_val = 1
        elif re.search(r'\b4k\b', lower_name): 
            quality = "4K"
            q_val = 4
        
        size = ""
        size_match = re.search(r'([\d.]+)\s*(GB|MB)', title_str, re.IGNORECASE)
        if size_match:
            size = f"{size_match.group(1)} {size_match.group(2).upper()}"
            
        seeders = 0
        seed_match = re.search(r'👤\s*(\d+)', title_str)
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
            "hash": stream_id,
            "url": s.get("url"),
            "is_http": is_http,
            "quality": quality,
            "q_val": q_val,
            "size": size,
            "seeders": seeders,
            "title": s.get("name") or "",
            "stream_title": full_title,
            "file_index": s.get("fileIdx"),
            "filename": filename,
            "addon_names": [s.get("addon_name")] if s.get("addon_name") else []
        })
    
    valid_streams.sort(key=lambda x: (x["q_val"], x["seeders"]), reverse=True)
    return valid_streams

def get_torrents(imdb_id, media_type="movie", season=None, episode=None, use_cache=True):
    if not imdb_id:
        return []
        
    cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    if use_cache:
        cached = database.get_cached_streams(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

    actual_media = media_type if media_type == "tv" else ("series" if media_type in ["series", "anime"] else media_type)
    
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    if not addons:
        return []
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://")]
    
    def fetch_from_addon(addon):
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
                
        if addon_types is not None and actual_media not in addon_types:
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
            url = f"{base_url}stream/series/{imdb_id}:{season}:{episode}.json"
        else:
            url = f"{base_url}stream/{actual_media}/{imdb_id}.json"
            
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

def get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None, callback=None, title=None):
    if not imdb_id:
        if callback: callback([], is_cached=False, is_complete=True)
        return []

    imdb_id = resolve_to_imdb_id(imdb_id, media_type, title)

    cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    cached = database.get_cached_streams(cache_key, max_age_hours=24)
    if cached and callback:
        callback(cached, is_cached=True, is_complete=False)

    actual_media = "anime" if (media_type == "anime" or str(imdb_id).startswith("kitsu:")) else ("series" if media_type in ["series", "tv"] else media_type)
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    if not addons:
        if callback: callback(cached or [], is_cached=False, is_complete=True)
        return cached or []
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://")]

    def fetch_from_addon(addon):
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
                
        if addon_types is not None and actual_media not in addon_types:
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
            url = f"{base_url}stream/series/{imdb_id}:{season}:{episode}.json"
        else:
            url = f"{base_url}stream/{actual_media}/{imdb_id}.json"
            
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

    all_raw_streams = []
    num_workers = min(len(stremio_addons), 30) if stremio_addons else 1
    if num_workers > 0 and stremio_addons:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {executor.submit(fetch_from_addon, addon): addon for addon in stremio_addons}
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=20):
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
            except concurrent.futures.TimeoutError:
                print("Timeout fetching streams from some addons")

    final_streams = process_raw_streams(all_raw_streams)
    if final_streams:
        database.save_cached_streams(cache_key, final_streams)
    elif stremio_addons:
        database.delete_cached_streams(cache_key)

    if callback:
        callback(final_streams, is_cached=False, is_complete=True)

    return final_streams

def get_subtitles(imdb_id, media_type="movie", season=None, episode=None):
    if not imdb_id:
        return []
        
    actual_media = "series" if media_type in ["series", "anime", "tv"] else media_type
    if actual_media == "series" and season is not None and episode is not None:
        url = f"https://opensubtitles-v3.strem.io/subtitles/series/{imdb_id}:{season}:{episode}.json"
    else:
        url = f"https://opensubtitles-v3.strem.io/subtitles/movie/{imdb_id}.json"
        
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            subs = data.get("subtitles", [])
            
            eng_subs = [s for s in subs if s.get("lang", "").lower() in ["eng", "en", "english"]]
            return eng_subs
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} fetching subtitles")
        e.close()
    except Exception as e:
        print(f"Error fetching subtitles: {e}")
        
    return []

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
