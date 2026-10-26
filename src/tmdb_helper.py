import re
import urllib.parse
import json
import base64
import threading
import concurrent.futures
import logging
from . import database

def _get_cached_request(*args, **kwargs):
    from .api import _get_cached_request as req
    return req(*args, **kwargs)

# Every response in this module describes a card (TMDb ids, credits, genres,
# companies, collections, seasons) or a poster, so all of them are cached
# forever. Mirrors api.METADATA_CACHE_HOURS, restated here because importing
# api at module level would be circular.
METADATA_CACHE_HOURS = None


DEFAULT_TMDB_API_KEY = "68e094699525b18a70bab2f86b1fa706"

# Extra per-language discover pages fetched for multi-cinema countries (India,
# Pakistan, Nigeria). Kept small because every entry costs one request per media
# type; each result is cached forever, so the cost is paid once per filter.
_MAX_COUNTRY_LANG_PAGES = 3

# How deep one filter may browse. Each page is one request per media type and
# api.py paces them through the polite catalog window, so the cap is what keeps a
# long scroll from turning into a load test on TMDb: 20 pages is at most 40
# requests per filter, each cached forever and only paid for as the user really
# scrolls. Five pages were too tight - a discover row renders up to 36 cards, so
# the grid stopped growing at about 180 cards.
_MAX_DISCOVER_PAGES = 20
# TMDb caps Discover at 500 pages. Network catalogs can be much deeper than the
# other filtered feeds, so let the user keep scrolling up to the API's limit.
_MAX_NETWORK_PAGES = 500
# TMDb's search endpoint allows up to 500 pages. Search pages are requested only
# as the user scrolls, so keep the provider's full searchable depth available.
_MAX_TITLE_SEARCH_PAGES = 500

_TMDB_API_KEY_CACHE = {"key": None, "expires": 0.0}
_TMDB_API_KEY_LOCK = threading.Lock()

_RESOLVER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6)
import atexit
atexit.register(_RESOLVER_EXECUTOR.shutdown, wait=False, cancel_futures=True)

def get_tmdb_api_key():
    import os
    import time as _time
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    # Called by nearly every fetch helper; the addon list only changes when
    # the user edits addons, so a short TTL cache avoids a DB scan + regex
    # on every metadata request.
    now = _time.time()
    with _TMDB_API_KEY_LOCK:
        if _TMDB_API_KEY_CACHE["key"] and now < _TMDB_API_KEY_CACHE["expires"]:
            return _TMDB_API_KEY_CACHE["key"]
        try:
            for addon in database.get_addons():
                m_url = addon.get("manifest_url", "")
                if "tmdb" in m_url.lower():
                    match = re.search(r'/([a-fA-F0-9]{32})/', m_url)
                    if match:
                        _TMDB_API_KEY_CACHE["key"] = match.group(1)
                        _TMDB_API_KEY_CACHE["expires"] = now + 60
                        return match.group(1)
        except Exception:
            pass
        return DEFAULT_TMDB_API_KEY

_PROVIDER_B64_ID_RE = re.compile(r"^dsx:[a-z]+:([A-Za-z0-9+/_=-]+)")
_TMDB_META_ADDON = "https://94c8cb9f702d-tmdb-addon.baby-beamup.club"

def _decode_provider_item_id(item_id):
    """Decode provider-specific IDs that embed metadata as base64 JSON.

    DesiFlix IDs look like ``dsx:c:<base64>`` (catalog) or ``dsx:e:<base64>:S:E``
    (episode). The payload holds the TMDB key (``#m<id>`` for movies, ``#t<id>``
    for series), a title and, for episodes, the season/episode pair. Without
    decoding, no IMDb/TMDB ID exists for the item, so addons that only accept
    ``tt``/``tmdb:`` (Torrentio, TorrentsDB, Pipe, ...) are never queried.
    """
    if not item_id:
        return None
    match = _PROVIDER_B64_ID_RE.match(str(item_id))
    if not match:
        return None
    b64 = match.group(1)
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_").decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    info = {}
    key_match = re.match(r"^#([a-z])(\d+)$", str(payload.get("k") or ""))
    if key_match:
        info["tmdb_id"] = key_match.group(2)
        info["tmdb_type"] = "movie" if key_match.group(1) == "m" else "tv"

    title = payload.get("t") or payload.get("o")
    if isinstance(title, str) and title.strip():
        info["title"] = title.strip()

    seasons = payload.get("se")
    if isinstance(seasons, (list, tuple)) and len(seasons) >= 2:
        try:
            info["season"] = int(seasons[0])
            info["episode"] = int(seasons[1])
        except (TypeError, ValueError):
            pass

    kind = str(payload.get("s") or "").lower()
    if info.get("season") is not None or kind in ("web", "series", "tv"):
        info["media_type"] = "series"
    elif info.get("tmdb_type") == "movie":
        info["media_type"] = "movie"

    return info or None

def normalize_title_variants(title):
    """Generate title variants for resilient searching across IMDb and TMDB.
    Handles concatenated words/camelCase ('KeTop' -> 'Ke Top'),
    'percent' <-> '%', and digit/word boundaries.
    """
    if not title:
        return []
    title = str(title).strip()
    variants = [title]
    # Split camelCase and joined words like 'KeTop' -> 'Ke Top'
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', title)
    spaced = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', spaced)
    spaced = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', spaced)
    spaced = re.sub(r'\s+', ' ', spaced).strip()
    if spaced not in variants:
        variants.append(spaced)
    for base in [title, spaced]:
        percent_sub = re.sub(r'\bpercent\b', '%', base, flags=re.I).strip()
        if percent_sub not in variants:
            variants.append(percent_sub)
        word_percent = re.sub(r'%', 'percent', base).strip()
        if word_percent not in variants:
            variants.append(word_percent)
    return variants

def is_similar_title(t1, t2):
    """Check if two titles refer to the same media item, avoiding false positives."""
    if not t1 or not t2:
        return False
    w1 = set(re.findall(r'\w+', str(t1).lower()))
    w2 = set(re.findall(r'\w+', str(t2).lower()))
    if not w1 or not w2:
        return False
    # Stopwords in English / Hindi / common serial markers
    stop = {"the", "a", "an", "and", "of", "in", "to", "ke", "ka", "ki", "hai", "se", "ek", "percent"}
    w1_sig = w1 - stop
    w2_sig = w2 - stop
    if not w1_sig or not w2_sig:
        return bool(w1 & w2)
    overlap = len(w1_sig & w2_sig)
    return (overlap / min(len(w1_sig), len(w2_sig))) >= 0.6

def _tmdb_id_to_imdb(tmdb_id, tmdb_type="movie"):
    """Map a TMDB numeric ID to an IMDb ``tt`` ID."""
    if not tmdb_id:
        return None
    tmdb_type = "tv" if tmdb_type == "tv" else "movie"
    api_key = get_tmdb_api_key()
    if api_key:
        try:
            find_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={api_key}&append_to_response=external_ids"
            data = _get_cached_request(find_url, max_age_hours=METADATA_CACHE_HOURS, timeout=3.0)
            imdb = (data or {}).get("imdb_id") or ((data or {}).get("external_ids") or {}).get("imdb_id")
            if imdb and str(imdb).startswith("tt"):
                return str(imdb)
        except Exception:
            pass
    try:
        c_type = "series" if tmdb_type == "tv" else "movie"
        addon_url = f"{_TMDB_META_ADDON}/meta/{c_type}/tmdb:{tmdb_id}.json"
        data = _get_cached_request(addon_url, max_age_hours=METADATA_CACHE_HOURS, timeout=3.0)
        imdb = ((data or {}).get("meta") or {}).get("imdb_id")
        if imdb and str(imdb).startswith("tt"):
            return str(imdb)
    except Exception:
        pass
    return None

def resolve_to_imdb_id(imdb_id, media_type, title=None):
    """
    Resolves a TMDB ID or raw ID to an IMDB ID using multiple fallback strategies.
    If the ID is already an IMDB ID, returns it clean (tt...).
    """
    if isinstance(imdb_id, list):
        return [resolve_to_imdb_id(i, media_type, title) for i in imdb_id]
        
    if not imdb_id and not title:
        return None

    orig_id = str(imdb_id).strip() if imdb_id else None

    if imdb_id:
        str_id = str(imdb_id).strip()
        if str_id.startswith("ctmdb."):
            return str_id
        if str_id.startswith("tt"):
            return str_id.split(":")[0]
        if str_id.startswith("tpb_ctl:"):
            try:
                raw_b64 = str_id.split("tpb_ctl:", 1)[1]
                payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
                p_url = payload.get("poster", "")
                tt_match = re.search(r'\b(tt\d{7,8})\b', p_url)
                if tt_match:
                    return tt_match.group(1)
            except Exception:
                pass
        tt_match = re.search(r'\b(tt\d{7,8})\b', str_id)
        if tt_match:
            return tt_match.group(1)

    custom_info = _decode_provider_item_id(imdb_id)
    if custom_info:
        media_type = custom_info.get("media_type") or media_type
        if custom_info.get("tmdb_id"):
            resolved = _tmdb_id_to_imdb(custom_info["tmdb_id"], custom_info.get("tmdb_type"))
            if resolved:
                return resolved
        title = title or custom_info.get("title")

    is_tmdb = False
    if imdb_id and not custom_info:
        str_id = str(imdb_id).strip()
        if str_id.startswith("bolly:") or str_id.startswith("hub:"):
            is_tmdb = True
            if ":s:" in str_id:
                media_type = "series"
            elif ":m:" in str_id:
                media_type = "movie"
        else:
            is_tmdb = str_id.startswith("tmdb:") or str_id.isdigit()
        if not is_tmdb:
            return str_id
        
    resolved_id = None
    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
    
    # 1. Try official TMDB API (only for genuine TMDB IDs)
    if is_tmdb and imdb_id:
        try:
            tmdb_api_key = get_tmdb_api_key()
            if tmdb_api_key:
                tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
                tmdb_type = "tv" if c_type == "series" else "movie"
                tmdb_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={tmdb_api_key}&append_to_response=external_ids"
                
                tmdb_data = _get_cached_request(tmdb_url, max_age_hours=METADATA_CACHE_HOURS)
                if tmdb_data and "external_ids" in tmdb_data:
                    resolved_id = tmdb_data["external_ids"].get("imdb_id")
        except Exception:
            pass
            
        # 2. Try the public Stremio TMDB addon
        if not resolved_id:
            try:
                tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
                addon_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/tmdb:{tmdb_id}.json"
                addon_data = _get_cached_request(addon_url, max_age_hours=METADATA_CACHE_HOURS)
                if addon_data and "meta" in addon_data:
                    resolved_id = addon_data["meta"].get("imdb_id")
            except Exception:
                pass

    # 3. Fallback: Search by title across Cinemeta, IMDb suggestions, and TMDB
    if not resolved_id and title and title != "Loading...":
        variants = normalize_title_variants(title)

        # 3a. Cinemeta search
        for v in variants:
            try:
                search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(v)}.json"
                search_data = _get_cached_request(search_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.0)
                if search_data and "metas" in search_data:
                    for m in search_data["metas"]:
                        m_id = m.get("imdb_id") or m.get("id", "")
                        m_name = m.get("name", "")
                        if str(m_id).startswith("tt") and is_similar_title(v, m_name):
                            resolved_id = m_id
                            break
                if resolved_id:
                    break
            except Exception:
                pass

        # 3b. IMDb suggestion API
        if not resolved_id:
            for v in variants:
                try:
                    clean = re.sub(r'[^a-zA-Z0-9]', '_', v).lower()
                    clean = re.sub(r'_+', '_', clean).strip('_')
                    if not clean:
                        continue
                    first_char = clean[0]
                    imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(clean)}.json"
                    req = urllib.request.Request(imdb_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    with urllib.request.urlopen(req, timeout=2.5) as response:
                        s_data = json.loads(response.read().decode('utf-8', errors='ignore'))
                        if s_data and "d" in s_data:
                            for item in s_data["d"]:
                                m_id = item.get("id", "")
                                m_name = item.get("l", "")
                                if str(m_id).startswith("tt") and is_similar_title(v, m_name):
                                    resolved_id = m_id
                                    break
                    if resolved_id:
                        break
                except Exception:
                    pass

        # 3c. TMDB search
        if not resolved_id:
            api_key = get_tmdb_api_key()
            if api_key:
                tmdb_media_type = "tv" if c_type == "series" else "movie"
                for v in variants:
                    try:
                        tmdb_search_url = f"https://api.themoviedb.org/3/search/{tmdb_media_type}?api_key={api_key}&query={urllib.parse.quote(v)}"
                        t_data = _get_cached_request(tmdb_search_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
                        if t_data and t_data.get("results"):
                            for res0 in t_data["results"]:
                                r_name = res0.get("name") or res0.get("title") or ""
                                if is_similar_title(v, r_name):
                                    t_id = res0.get("id")
                                    if t_id:
                                        resolved_id = _tmdb_id_to_imdb(str(t_id), tmdb_media_type) or f"tmdb:{t_id}"
                                        break
                        if resolved_id:
                            break
                    except Exception:
                        pass
            
    return resolved_id or orig_id

def resolve_all_provider_ids(item_id, media_type="movie", title=None):
    """
    Returns a list of all distinct provider IDs (IMDB tt..., TMDB, DesiFlix dsx:...)
    for the given media item so all stream addons (Torrentio, Castle, DesiFlix, etc.) can be queried.
    Optimized to only perform network lookups if at least one installed stream addon requires that ID prefix.
    """
    ids = set()
    if isinstance(item_id, list):
        for i in item_id:
            if i: ids.add(str(i))
    elif item_id:
        ids.add(str(item_id))

    # Add TMDB format if item starts with bolly: or hub:
    for single_id in list(ids):
        str_s = str(single_id)
        if str_s.startswith("bolly:") or str_s.startswith("hub:"):
            tmdb_num = str_s.split(":")[-1]
            if tmdb_num.isdigit():
                ids.add(f"tmdb:{tmdb_num}")
                if ":s:" in str_s:
                    media_type = "series"
                elif ":m:" in str_s:
                    media_type = "movie"

    # Decode provider-encoded IDs (e.g. DesiFlix dsx:c:<base64 JSON>) so the
    # embedded TMDB id can be offered to addons that do not understand dsx:.
    for single_id in list(ids):
        info = _decode_provider_item_id(single_id)
        if not info:
            continue
        if info.get("tmdb_id"):
            ids.add(f"tmdb:{info['tmdb_id']}")
        if info.get("media_type"):
            media_type = info["media_type"]
        if not title and info.get("title"):
            title = info["title"]

    # Determine what prefixes are actually needed by installed stream addons
    installed_addons = [a for a in database.get_addons() if a.get("enabled", True) and not a.get("manifest_url", "").startswith("builtin://")]
    needed_prefixes = set()
    for a in installed_addons:
        prefixes = a.get("idPrefixes")
        if prefixes:
            for p in prefixes:
                needed_prefixes.add(str(p).lower())
        else:
            # Addon with no idPrefixes might support any prefix or default tt
            needed_prefixes.add("tt")

    needs_tmdb = any(p.startswith("tmdb") for p in needed_prefixes)
    has_tt = any(str(i).startswith("tt") for i in ids)

    # If we already have tt... and don't need tmdb, return immediately!
    if has_tt and not needs_tmdb:
        return list(ids)

    def task_imdb():
        res = resolve_to_imdb_id(item_id, media_type, title)
        if isinstance(res, list): return res
        return [res] if res else []

    def _title_match(metas, t_lower):
        # Prefer an exact (case-insensitive) title match so "Escape" does not
        # resolve to "Escape Room"; only then accept a contained match.
        contained = None
        for m in metas:
            m_id = m.get("imdb_id") or m.get("id", "")
            if not str(m_id).startswith("tt"):
                continue
            m_name = str(m.get("name", "")).strip().lower()
            if not m_name:
                continue
            if m_name == t_lower:
                return str(m_id)
            if contained is None and (m_name in t_lower or t_lower in m_name):
                contained = str(m_id)
        return contained

    def task_tt():
        has_tt_curr = any(str(i).startswith("tt") for i in ids)
        if not has_tt_curr and title and title != "Loading...":
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=METADATA_CACHE_HOURS, timeout=3.0)
                if data and "metas" in data:
                    matched = _title_match(data["metas"], str(title).strip().lower())
                    if matched:
                        return [matched]
            except Exception:
                pass
        return []

    def task_tmdb(current_ids):
        if not needs_tmdb:
            return []
        res = []
        has_tmdb = any(str(i).startswith("tmdb:") for i in current_ids)
        if not has_tmdb:
            tt_id = next((i for i in current_ids if str(i).startswith("tt")), None)
            root_tt_id = str(tt_id).split(":")[0] if tt_id else None
            if root_tt_id:
                try:
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    meta_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{root_tt_id}.json"
                    meta_data = _get_cached_request(meta_url, max_age_hours=METADATA_CACHE_HOURS, timeout=3.0)
                    if meta_data and "meta" in meta_data:
                        t_id = meta_data["meta"].get("id")
                        if t_id and str(t_id).startswith("tmdb:"):
                            res.append(str(t_id))
                except Exception:
                    pass
            if not res and title and title != "Loading...":
                try:
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    search_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/catalog/{c_type}/tmdb.top/search={urllib.parse.quote(title)}.json"
                    search_data = _get_cached_request(search_url, max_age_hours=METADATA_CACHE_HOURS, timeout=3.0)
                    if search_data and "metas" in search_data:
                        for m in search_data["metas"]:
                            m_id = m.get("id")
                            if m_id and str(m_id).startswith("tmdb:"):
                                res.append(str(m_id))
                                break
                except Exception:
                    pass
        return res

    try:
        futures = [_RESOLVER_EXECUTOR.submit(task_imdb), _RESOLVER_EXECUTOR.submit(task_tt)]
        if needs_tmdb and not any(str(i).startswith("tmdb:") for i in ids):
            futures.append(_RESOLVER_EXECUTOR.submit(task_tmdb, list(ids)))
        # One bounded wait instead of a short per-future timeout: the HTTP
        # requests themselves allow up to 3s, so a 1.5s result() timeout used
        # to silently discard slow but successful lookups.
        done, _pending = concurrent.futures.wait(futures, timeout=5.0)
        # Apply the exact resolver first: the Cinemeta title search is only a
        # fallback and must not add a different tt id ("Escape" -> "Escape
        # Room") when the item's own id already resolved cleanly.
        for f in futures:
            if f not in done and not f.done():
                continue
            try:
                results = f.result()
            except Exception:
                continue
            for r in results or []:
                if not r:
                    continue
                if f is futures[1] and any(str(i).startswith("tt") for i in ids):
                    continue
                ids.add(str(r))
    except RuntimeError:
        pass

    result_list = []
    if isinstance(item_id, list):
        for i in item_id:
            if str(i) in ids and str(i) not in result_list:
                result_list.append(str(i))
    elif item_id and str(item_id) in ids:
        result_list.append(str(item_id))

    for i in ids:
        if i not in result_list:
            result_list.append(i)

    return result_list


def resolve_to_tmdb_id(item_id, media_type="movie", title=None):
    """
    Resolves an item_id (IMDB tt..., raw digits, tmdb:..., ctmdb....) or title to a numeric TMDB ID.
    Returns string of digits (e.g. '550') or None.
    """
    if not item_id and not title:
        return None

    str_id = str(item_id or "").strip()
    if str_id.startswith("tmdb:"):
        num = str_id.split("tmdb:")[-1].split(":")[0]
        if num.isdigit():
            return num
    if str_id.startswith("ctmdb."):
        num = str_id.split(".")[-1]
        if num.isdigit():
            return num
    if (str_id.startswith("bolly:") or str_id.startswith("hub:")) and ":" in str_id:
        num = str_id.split(":")[-1]
        if num.isdigit():
            return num
    if str_id.isdigit():
        return str_id

    custom_info = _decode_provider_item_id(item_id)
    if custom_info:
        if custom_info.get("tmdb_id"):
            return str(custom_info["tmdb_id"])
        if not title and custom_info.get("title"):
            title = custom_info["title"]
        if custom_info.get("media_type"):
            media_type = custom_info["media_type"]

    api_key = get_tmdb_api_key()
    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"

    # If it's an IMDB id (tt...), use TMDB /find endpoint
    clean_tt = None
    if str_id.startswith("tt"):
        clean_tt = str_id.split(":")[0]
    else:
        tt_match = re.search(r'\b(tt\d{7,8})\b', str_id)
        if tt_match:
            clean_tt = tt_match.group(1)

    if clean_tt and api_key:
        try:
            find_url = f"https://api.themoviedb.org/3/find/{clean_tt}?api_key={api_key}&external_source=imdb_id"
            data = _get_cached_request(find_url, max_age_hours=168)
            if data:
                if c_type == "tv" and data.get("tv_results"):
                    return str(data["tv_results"][0]["id"])
                elif data.get("movie_results"):
                    return str(data["movie_results"][0]["id"])
                elif data.get("tv_results"):
                    return str(data["tv_results"][0]["id"])
                elif data.get("id"):
                    return str(data["id"])
        except Exception:
            pass

    # Search by title as fallback with title variants
    if title and title != "Loading..." and api_key:
        variants = normalize_title_variants(title)
        for v in variants:
            try:
                search_url = f"https://api.themoviedb.org/3/search/{c_type}?api_key={api_key}&query={urllib.parse.quote(v)}"
                data = _get_cached_request(search_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
                if data and data.get("results"):
                    for res0 in data["results"]:
                        r_name = res0.get("name") or res0.get("title") or ""
                        if is_similar_title(v, r_name):
                            return str(res0["id"])
                    # If no similar title matched among results, accept first result if it starts with the variant
                    res0 = data["results"][0]
                    r_name = (res0.get("name") or res0.get("title") or "").lower()
                    if v.lower() in r_name or r_name in v.lower():
                        return str(res0["id"])
            except Exception:
                pass

    return None


def fetch_credits_and_companies(item_id, media_type="movie", title=None):
    """
    Fetches cast members, crew (directors, creators, writers), production companies,
    and networks for the given media item from TMDB.
    Returns:
      {
        "cast": [{"id": int, "name": str, "character": str, "photo": str}, ...],
        "crew": [{"id": int, "name": str, "job": str, "photo": str}, ...],
        "production_companies": [{"id": int, "name": str, "logo": str}, ...],
        "networks": [{"id": int, "name": str, "logo": str}, ...]
      }
    """
    empty_result = {
        "cast": [],
        "crew": [],
        "production_companies": [],
        "networks": []
    }

    tmdb_id = resolve_to_tmdb_id(item_id, media_type, title)
    if not tmdb_id:
        return empty_result

    api_key = get_tmdb_api_key()
    if not api_key:
        return empty_result

    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"

    try:
        if c_type == "tv":
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&append_to_response=aggregate_credits,credits"
            data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS)
            if not data:
                return empty_result

            # 1. Crew (Creators, Directors, Writers, EPs)
            crew = []
            seen_crew = set()
            # TV creators
            for creator in data.get("created_by", []):
                cid = creator.get("id")
                cname = creator.get("name", "").strip()
                if cid and cname and cid not in seen_crew:
                    seen_crew.add(cid)
                    prof = creator.get("profile_path")
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": "Creator",
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # Additional TV crew from aggregate or standard credits
            raw_crew = data.get("aggregate_credits", {}).get("crew", []) or data.get("credits", {}).get("crew", [])

            def get_tv_crew_info(m):
                jobs = m.get("jobs", [])
                if jobs and isinstance(jobs, list):
                    for target_job in ["Director", "Writer", "Executive Producer"]:
                        for j in jobs:
                            if j.get("job") == target_job:
                                return target_job, j.get("episode_count", 0)
                    return jobs[0].get("job", "Crew"), jobs[0].get("episode_count", 0)
                return m.get("job", "Crew"), m.get("total_episode_count", 0) or 1

            other_crew = []
            for m in raw_crew:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_crew:
                    continue
                job_name, ep_count = get_tv_crew_info(m)
                if job_name in ["Director", "Writer", "Executive Producer"]:
                    other_crew.append((cid, cname, job_name, ep_count, m.get("profile_path")))

            # Sort Directors by episode count descending, then Writers, then Executive Producers
            rank_map = {"Director": 0, "Writer": 1, "Executive Producer": 2}
            other_crew.sort(key=lambda x: (rank_map.get(x[2], 99), -x[3]))

            for cid, cname, job_name, ep_count, prof in other_crew[:25]:
                if cid not in seen_crew:
                    seen_crew.add(cid)
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": job_name,
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # 2. Cast
            cast = []
            seen_cast = set()
            raw_cast = data.get("aggregate_credits", {}).get("cast", []) or data.get("credits", {}).get("cast", [])
            for m in raw_cast:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_cast:
                    continue
                seen_cast.add(cid)
                char = ""
                if "roles" in m and isinstance(m["roles"], list) and m["roles"]:
                    char = m["roles"][0].get("character", "").strip()
                elif "character" in m:
                    char = (m.get("character") or "").strip()
                prof = m.get("profile_path")
                cast.append({
                    "id": cid,
                    "name": cname,
                    "character": char,
                    "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                })
                if len(cast) >= 50:
                    break

            # 3. Production Companies
            production_companies = []
            for c in data.get("production_companies", []):
                cname = c.get("name", "").strip()
                if not cname: continue
                logo = c.get("logo_path")
                production_companies.append({
                    "id": c.get("id"),
                    "name": cname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "company"
                })

            # 4. Networks
            networks = []
            for n in data.get("networks", []):
                nname = n.get("name", "").strip()
                if not nname: continue
                logo = n.get("logo_path")
                networks.append({
                    "id": n.get("id"),
                    "name": nname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "network"
                })

            return {
                "cast": cast,
                "crew": crew,
                "production_companies": production_companies,
                "networks": networks
            }

        else:
            # Movie credits
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&append_to_response=credits"
            data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS)
            if not data:
                return empty_result

            # 1. Crew (Directors, Writers, Producers)
            crew = []
            seen_crew = set()
            raw_crew = data.get("credits", {}).get("crew", [])

            movie_rank = {"Director": 0, "Writer": 1, "Screenplay": 2, "Producer": 3, "Executive Producer": 4}
            sorted_movie_crew = [m for m in raw_crew if m.get("job") in movie_rank]
            sorted_movie_crew.sort(key=lambda m: (movie_rank.get(m.get("job"), 99), -m.get("popularity", 0)))

            for m in sorted_movie_crew[:25]:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                job = m.get("job") or ""
                if cid and cname and cid not in seen_crew:
                    seen_crew.add(cid)
                    prof = m.get("profile_path")
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": job,
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # 2. Cast
            cast = []
            seen_cast = set()
            raw_cast = data.get("credits", {}).get("cast", [])
            for m in raw_cast:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_cast:
                    continue
                seen_cast.add(cid)
                char = (m.get("character") or "").strip()
                prof = m.get("profile_path")
                cast.append({
                    "id": cid,
                    "name": cname,
                    "character": char,
                    "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                })
                if len(cast) >= 40:
                    break

            # 3. Production Companies
            production_companies = []
            for c in data.get("production_companies", []):
                cname = c.get("name", "").strip()
                if not cname: continue
                logo = c.get("logo_path")
                production_companies.append({
                    "id": c.get("id"),
                    "name": cname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "company"
                })

            return {
                "cast": cast,
                "crew": crew,
                "production_companies": production_companies,
                "networks": []
            }

    except Exception as e:
        print(f"[TMDB] Failed to fetch credits/companies for {item_id}: {e}")
        return empty_result


def fetch_person_details(person_id):
    """
    Fetches person details (bio, photo, birthday, place of birth, known for)
    and full filmography (movies and TV shows) from TMDB.
    Returns dictionary with person info and sorted filmography list.
    """
    if not person_id:
        return None

    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        url = f"https://api.themoviedb.org/3/person/{person_id}?api_key={api_key}&append_to_response=combined_credits"
        data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS)
        if not data:
            return None

        # Build combined credits list
        combined = data.get("combined_credits", {})
        raw_cast = combined.get("cast", [])
        raw_crew = combined.get("crew", [])

        filmography = []
        seen_media = set()

        # Add cast items
        for item in raw_cast:
            mid = item.get("id")
            mtype = item.get("media_type") or "movie"
            unique_key = f"{mtype}:{mid}"
            if not mid or unique_key in seen_media:
                continue
            seen_media.add(unique_key)
            title = item.get("title") or item.get("name") or "Unknown"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{item['poster_path']}" if item.get("poster_path") else ""
            filmography.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "series" if mtype == "tv" else "movie",
                "media_type": "series" if mtype == "tv" else "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "character": (item.get("character") or "").strip(),
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date
            })

        # Add crew items (e.g. Director, Creator, Writer) if not already present
        for item in raw_crew:
            mid = item.get("id")
            mtype = item.get("media_type") or "movie"
            unique_key = f"{mtype}:{mid}"
            job = item.get("job") or ""
            if job not in ["Director", "Creator", "Writer", "Screenplay"]:
                continue
            if not mid or unique_key in seen_media:
                continue
            seen_media.add(unique_key)
            title = item.get("title") or item.get("name") or "Unknown"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{item['poster_path']}" if item.get("poster_path") else ""
            filmography.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "series" if mtype == "tv" else "movie",
                "media_type": "series" if mtype == "tv" else "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "character": job,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date
            })

        # Sort filmography: primarily by popularity/vote_count and release_date
        filmography.sort(key=lambda x: (x.get("vote_count", 0) > 10, x.get("popularity", 0.0)), reverse=True)

        prof = data.get("profile_path")
        return {
            "id": data.get("id"),
            "name": data.get("name", ""),
            "biography": (data.get("biography") or "").strip(),
            "birthday": data.get("birthday"),
            "deathday": data.get("deathday"),
            "place_of_birth": data.get("place_of_birth"),
            "known_for": data.get("known_for_department") or "Acting",
            "photo": f"https://image.tmdb.org/t/p/w300{prof}" if prof else None,
            "filmography": filmography
        }
    except Exception as e:
        print(f"[TMDB] Failed to fetch person details for {person_id}: {e}")
        return None


def fetch_company_details(entity_id, entity_type="company"):
    """
    Fetches details for a production company or TV network from TMDB,
    along with a curated catalog of movies and series produced or broadcast by that entity.
    """
    if not entity_id:
        return None
    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        is_network = str(entity_type).lower() in ["network", "networks", "tv_network"]

        info_data = None
        if is_network:
            info_url = f"https://api.themoviedb.org/3/network/{entity_id}?api_key={api_key}"
            info_data = _get_cached_request(info_url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
            if not info_data:
                info_data = _get_cached_request(f"https://api.themoviedb.org/3/company/{entity_id}?api_key={api_key}", max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
                if info_data:
                    is_network = False
        else:
            info_url = f"https://api.themoviedb.org/3/company/{entity_id}?api_key={api_key}"
            info_data = _get_cached_request(info_url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
            if not info_data:
                info_data = _get_cached_request(f"https://api.themoviedb.org/3/network/{entity_id}?api_key={api_key}", max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
                if info_data:
                    is_network = True

        movie_url = f"https://api.themoviedb.org/3/discover/movie?api_key={api_key}&with_companies={entity_id}&sort_by=popularity.desc&page=1"
        if is_network:
            tv_url = f"https://api.themoviedb.org/3/discover/tv?api_key={api_key}&with_networks={entity_id}&sort_by=popularity.desc&page=1"
        else:
            tv_url = f"https://api.themoviedb.org/3/discover/tv?api_key={api_key}&with_companies={entity_id}&sort_by=popularity.desc&page=1"

        raw_movies = {}
        raw_tv = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                fut_movies = executor.submit(
                    _get_cached_request, movie_url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
                fut_tv = executor.submit(
                    _get_cached_request, tv_url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0)
                raw_movies = fut_movies.result() or {}
                raw_tv = fut_tv.result() or {}
        except RuntimeError:
            pass

        movies = []
        for item in raw_movies.get("results", []):
            mid = item.get("id")
            poster_path = item.get("poster_path")
            if not mid or not poster_path:
                continue
            title = item.get("title") or item.get("original_title") or "Unknown"
            rel_date = item.get("release_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            movies.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "movie",
                "media_type": "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date,
                "overview": (item.get("overview") or "").strip()
            })

        series = []
        for item in raw_tv.get("results", []):
            sid = item.get("id")
            poster_path = item.get("poster_path")
            if not sid or not poster_path:
                continue
            title = item.get("name") or item.get("original_name") or "Unknown"
            rel_date = item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            series.append({
                "id": f"tmdb:{sid}",
                "raw_id": sid,
                "title": title,
                "name": title,
                "type": "series",
                "media_type": "series",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date,
                "overview": (item.get("overview") or "").strip()
            })

        all_titles = movies + series
        all_titles.sort(key=lambda x: (x.get("vote_count", 0) > 10, x.get("popularity", 0.0)), reverse=True)

        logo_path = info_data.get("logo_path") if info_data else None
        return {
            "id": entity_id,
            "type": "network" if is_network else "company",
            "name": (info_data.get("name") if info_data else "") or "",
            "description": (info_data.get("description") if info_data else "") or "",
            "headquarters": (info_data.get("headquarters") if info_data else "") or "",
            "origin_country": (info_data.get("origin_country") if info_data else "") or "",
            "homepage": (info_data.get("homepage") if info_data else "") or "",
            "logo": f"https://image.tmdb.org/t/p/w300{logo_path}" if logo_path else None,
            "movies": movies,
            "series": series,
            "all_titles": all_titles
        }
    except Exception as e:
        print(f"[TMDB] Failed to fetch company details for {entity_id} ({entity_type}): {e}")
        return None


_NETWORK_COMPANY_SEARCH_NAMES = {
    "amazon prime": ("Amazon Studios", "Amazon MGM Studios"),
    "amazon prime video": ("Amazon Studios", "Amazon MGM Studios"),
    "apple tv+": ("Apple Studios", "Apple Original Films"),
    "disney+": ("Walt Disney Pictures", "Disney"),
    "hbo": ("HBO Films", "HBO"),
    "bbc": ("BBC Films", "BBC Studios"),
    "amazon mx player": ("Amazon MX Player", "Amazon MiniTV"),
    "mx player": ("MX Player",),
    "eros now": ("Eros Now", "Eros International"),
    "chaupal": ("Chaupal", "Bosna Digital Entertainment"),
}

# TMDb network IDs avoid ambiguous text matching for the Network dropdown.
_NETWORK_TMDB_IDS = {
    "netflix": 213,
    "amazon prime": 1024,
    "amazon prime video": 1024,
    "hbo": 49,
    "apple tv+": 2552,
    "disney+": 2739,
    "hulu": 453,
    "paramount+": 4330,
    "peacock": 3353,
    "bbc": 4,
    "showtime": 67,
    "starz": 318,
    "amazon mx player": 7774,
    "mx player": 2964,
    "eros now": 2716,
}

_NETWORK_COMPANY_IDS = {
    "netflix": 178464,
    "amazon prime": 20580,
    "amazon prime video": 20580,
    "hbo": 7429,
    "apple tv+": 194232,
    "disney+": 2,
}

# Indian OTT catalogs are availability catalogs, not production-network catalogs.
# Resolve these names against TMDb's regional watch-provider lists so provider IDs
# can change without confusing an OTT service with a TV network or studio.
_INDIA_WATCH_PROVIDER_ALIASES = {
    "jiohotstar": ("JioHotstar", "Jio Hotstar", "Disney+ Hotstar", "Hotstar"),
    "zee5": ("ZEE5", "Zee5"),
    "sonyliv": ("SonyLIV", "Sony Liv", "SonyLIV Amazon Channel"),
    "sun nxt": ("Sun Nxt", "Sun NXT"),
    "aha": ("aha", "aha Video"),
    "hoichoi": ("hoichoi", "Hoichoi"),
    "lionsgate play": ("Lionsgate Play",),
    "manoramamax": ("ManoramaMAX", "Manorama Max"),
    "chaupal": ("Chaupal",),
    "hungama play": ("Hungama Play", "Hungama"),
}


def _tmdb_india_watch_provider_id(network_name, media_type):
    """Resolve an Indian OTT provider by current TMDb name for movie or TV."""
    aliases = _INDIA_WATCH_PROVIDER_ALIASES.get(str(network_name or "").casefold().strip())
    if not aliases:
        return None
    api_key = get_tmdb_api_key()
    if not api_key:
        return None
    kind = "tv" if str(media_type or "").lower() in ("tv", "series", "anime", "tvshow") else "movie"
    params = urllib.parse.urlencode({"api_key": api_key, "watch_region": "IN", "language": "en-US"})
    payload = _get_cached_request(
        f"https://api.themoviedb.org/3/watch/providers/{kind}?{params}",
        max_age_hours=24,
        timeout=5.0,
    ) or {}
    wanted = {str(alias).casefold().strip() for alias in aliases}
    for provider in payload.get("results") or []:
        name = str(provider.get("provider_name") or "").casefold().strip()
        if name in wanted and provider.get("provider_id"):
            return int(provider["provider_id"])
    return None


def is_india_watch_provider(network_name):
    """Whether a dropdown option is resolved as an India OTT availability provider."""
    return str(network_name or "").casefold().strip() in _INDIA_WATCH_PROVIDER_ALIASES


def _search_tmdb_entity_id(kind, query):
    api_key = get_tmdb_api_key()
    if not api_key or not query:
        return None
    params = urllib.parse.urlencode({"api_key": api_key, "query": query})
    payload = _get_cached_request(
        f"https://api.themoviedb.org/3/search/{kind}?{params}",
        max_age_hours=METADATA_CACHE_HOURS,
        timeout=5.0,
    ) or {}
    results = payload.get("results") or []
    query_folded = query.casefold().strip()
    for result in results:
        name = str(result.get("name") or "").casefold().strip()
        if name == query_folded and result.get("id"):
            return int(result["id"])
    return int(results[0]["id"]) if results and results[0].get("id") else None


def fetch_network_catalog_page(network_name, media_type="all", page=1, limit=36,
                              genre=None, year=None, language=None, country=None,
                              sort_mode=None):
    """Fetch one network or Indian OTT provider's TMDb catalog page.

    Broadcast networks use TV-network IDs and movie-company IDs. Indian OTT
    services use availability providers for region IN. Returns ``(items, pages)``.
    """
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    if page > _MAX_NETWORK_PAGES:
        return [], 0

    api_key = get_tmdb_api_key()
    if not api_key or not network_name:
        return [], 0

    name = str(network_name).strip()
    folded = name.casefold()
    aliases = _NETWORK_COMPANY_SEARCH_NAMES.get(folded, (name,))
    network_id = _NETWORK_TMDB_IDS.get(folded)
    company_id = _NETWORK_COMPANY_IDS.get(folded)
    use_watch_provider = is_india_watch_provider(name)
    if not company_id and (not use_watch_provider or folded in _NETWORK_COMPANY_SEARCH_NAMES):
        for company_name in aliases:
            company_id = _search_tmdb_entity_id("company", company_name)
            if company_id:
                break

    mt = str(media_type or "all").strip().lower()
    if mt in ("all", "movie_series", "movie+series", "movies_series"):
        kinds = ("movie", "tv")
    elif mt in ("movie", "movies", "film", "films"):
        kinds = ("movie",)
    elif mt in ("series", "show", "shows", "tv", "tvshow", "tvshows", "anime"):
        kinds = ("tv",)
    else:
        return [], 0

    items = []
    total_pages = 0
    for kind in kinds:
        watch_provider_id = (
            _tmdb_india_watch_provider_id(name, kind)
            if use_watch_provider else None
        )
        use_provider_for_kind = bool(use_watch_provider and watch_provider_id)
        # TV titles can be linked either to an actual network or to a company.
        # Movies only have production-company credits in Discover.
        company_filter = company_id
        tv_network_filter = network_id if kind == "tv" else None
        if not use_provider_for_kind and kind == "tv" and not tv_network_filter and not company_filter:
            continue
        if not use_provider_for_kind and kind == "movie" and not company_filter:
            continue
        params = {
            "api_key": api_key,
            "sort_by": (("first_air_date.desc" if kind == "tv" else "primary_release_date.desc") if sort_mode and str(sort_mode).lower() in ("recent", "new", "latest") else "popularity.desc"),
            "include_adult": "false",
            "page": page,
        }
        if use_provider_for_kind:
            params.update({
                "with_watch_providers": watch_provider_id,
                "watch_region": "IN",
            })
        else:
            if kind == "tv" and tv_network_filter:
                params["with_networks"] = tv_network_filter
            else:
                params["with_companies"] = company_filter
        genre_id = _tmdb_genre_id(genre, kind) if genre else None
        if genre_id:
            params["with_genres"] = genre_id
        if year:
            params["first_air_date_year" if kind == "tv" else "primary_release_year"] = year
        if language:
            lang_code = database.get_language_code(language)
            if lang_code:
                params["with_original_language"] = lang_code
        if country:
            country_code = database.get_country_iso_code(country)
            if country_code:
                params["with_origin_country"] = country_code
        payload = _get_cached_request(
            f"https://api.themoviedb.org/3/discover/{kind}?{urllib.parse.urlencode(params)}",
            max_age_hours=24 if use_watch_provider else METADATA_CACHE_HOURS,
            timeout=5.0,
        ) or {}
        total_pages = max(total_pages, int(payload.get("total_pages") or 0))
        for entry in payload.get("results") or []:
            tmdb_id = entry.get("id")
            poster_path = entry.get("poster_path")
            if not tmdb_id or not poster_path:
                continue
            title = entry.get("title") or entry.get("name") or entry.get("original_title") or entry.get("original_name") or "Unknown"
            released = entry.get("release_date") or entry.get("first_air_date") or ""
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            items.append({
                "id": f"tmdb:{tmdb_id}",
                "raw_id": tmdb_id,
                "title": title,
                "name": title,
                "type": "movie" if kind == "movie" else "series",
                "media_type": "movie" if kind == "movie" else "series",
                "poster": poster,
                "medium_cover_image": poster,
                "year": released[:4] if len(released) >= 4 else "",
                "rating": round(float(entry.get("vote_average") or 0), 1),
                "vote_count": entry.get("vote_count", 0),
                "popularity": entry.get("popularity", 0.0),
                "release_date": released,
                "overview": (entry.get("overview") or "").strip(),
                "networks": ([{"id": network_id, "name": name}] if kind == "tv" and network_id and not use_provider_for_kind else []),
                "watch_providers": ([{"provider_id": watch_provider_id, "provider_name": name, "region": "IN"}] if use_provider_for_kind else []),
            })
    items.sort(key=lambda item: item.get("popularity", 0), reverse=True)
    return items[:max(1, int(limit or 36))], min(total_pages, _MAX_NETWORK_PAGES)

_TMDB_MOVIE_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War",
    37: "Western",
}

_TMDB_TV_GENRES = {
    10759: "Action", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 9648: "Mystery", 10765: "Sci-Fi",
    10766: "Soap", 10767: "Talk-Show", 10768: "War", 10762: "Family",
    10763: "News", 10764: "Reality-TV", 37: "Western",
}


def _tmdb_genre_id(genre_name, kind):
    if not genre_name:
        return None
    target = str(genre_name).strip().lower()
    table = _TMDB_MOVIE_GENRES if kind == "movie" else _TMDB_TV_GENRES
    for gid, name in table.items():
        if name.lower() == target:
            return gid
    if kind == "tv" and target == "science fiction":
        return 10765
    return None


def fetch_country_catalog(country, media_type="all", genre=None, year=None,
                          sort_mode=None, language=None, limit=36, page=1):
    """Fetch country-specific titles from TMDb's discover endpoint.

    Country filtering in the catalog UI is metadata-based, and most Stremio
    addons cannot filter by country at all. TMDb can, via
    ``with_origin_country``. Returns item dicts shaped like the ones produced
    by ``fetch_company_details``, or ``[]`` when the country has no known ISO
    code or the requests fail.
    """
    items, _total_pages = fetch_country_catalog_page(
        country=country, media_type=media_type, genre=genre, year=year,
        sort_mode=sort_mode, language=language, limit=limit, page=page,
    )
    return items


def fetch_country_catalog_page(country=None, media_type="all", genre=None, year=None,
                               sort_mode=None, language=None, limit=50, page=1,
                               exclude_ids=None):
    """One TMDb discover page for a country and/or an original language.

    Returns ``(items, total_pages)``. The deeper pages are what turn a country or
    language selection into a real browse instead of a single page of results,
    and each one costs a request per media type, so the caller only asks for the
    next page when the user reaches the bottom.
    """
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    if page > _MAX_DISCOVER_PAGES:
        return [], 0

    iso = database.get_country_iso_code(country) if country else None
    api_key = get_tmdb_api_key()
    if not api_key:
        return [], 0

    mt = str(media_type or "all").strip().lower()
    if mt in ("all", "discover", "none", "", "movie_series", "movies_series", "movie+series", "movies+series"):
        kinds = ["movie", "tv"]
    elif mt in ("movie", "movies", "film", "films"):
        kinds = ["movie"]
    elif mt in ("series", "show", "shows", "tvshow", "tvshows"):
        kinds = ["tv"]
    else:
        return [], 0

    lang_code = database.get_language_code(language) if language else None
    if genre and not lang_code and database.is_language_genre(genre):
        lang_code = database.get_language_code(genre)
        genre = None
    if not iso and not lang_code:
        return [], 0

    country_name = (database.normalize_country_name(country) or str(country).strip()) if country else None
    language_name = language or (database.LANGUAGE_CODE_MAP.get(lang_code) if lang_code else None)
    skip_ids = set(exclude_ids or ())

    def _sort_by(kind):
        if sort_mode == "Recent":
            return "primary_release_date.desc" if kind == "movie" else "first_air_date.desc"
        if sort_mode == "Featured":
            return "vote_average.desc"
        return "popularity.desc"

    def _url(kind, origin_lang=None):
        params = {
            "api_key": api_key,
            "sort_by": _sort_by(kind),
            "include_adult": "false",
            "page": page,
        }
        if iso and not origin_lang:
            params["with_origin_country"] = iso
            if lang_code:
                params["with_original_language"] = lang_code
        else:
            # Language-only request: with_origin_country AND with_original_language
            # would drop the titles that omit the country field entirely, which is
            # exactly the gap this fan-out exists to fill.
            params["with_original_language"] = origin_lang or lang_code
        gid = _tmdb_genre_id(genre, kind)
        if gid:
            params["with_genres"] = gid
        if year and str(year).strip().isdigit():
            params["primary_release_year" if kind == "movie" else "first_air_date_year"] = str(year).strip()
        if sort_mode == "Featured":
            params["vote_count.gte"] = 100
        return f"https://api.themoviedb.org/3/discover/{kind}?" + urllib.parse.urlencode(params)

    # Countries whose cinema spans several languages (India, Pakistan, Nigeria)
    # label titles per language instead of per origin country, so a handful of
    # extra language-wide pages is what actually fills the grid. Cached forever
    # upstream, and each filter combination only pays for it once. Deeper pages
    # stay on the primary query so scrolling never multiplies the requests.
    fanout_langs = [] if (lang_code or page > 1 or not iso) else database.get_fetch_languages(country)[:_MAX_COUNTRY_LANG_PAGES]
    requests = [(kind, _url(kind)) for kind in kinds]
    requests += [(kind, _url(kind, lang)) for lang in fanout_langs for kind in kinds]

    # Cached pages answer first and cost nothing. Read every URL through the
    # cache before asking for anything, then fetch only the pages this filter does
    # not already have: a filter the user has browsed fills the row from disk
    # without opening a single request. A row's own order stays the popularity
    # sort at the end of this function - "cached before network" is a promise
    # about when results reach the UI, made in window.py's delivery slices, not
    # one that should out-rank what TMDb says is most relevant.
    cached_pages = []
    missing = []
    for kind, url in requests:
        try:
            page_data = _get_cached_request(
                url, max_age_hours=METADATA_CACHE_HOURS, cache_only=True, timeout=5.0)
        except Exception:
            page_data = None
        if page_data:
            cached_pages.append((kind, page_data))
        else:
            missing.append((kind, url))

    fetched_pages = []
    if missing:
        try:
            # TMDb answers one filter with up to eight discover URLs. api.py lets a
            # host keep three requests in flight and paces their starts, so the pool
            # is sized to that window: enough to finish the fan-out inside the
            # discover batch deadline, never more than the window allows, and the
            # two primary URLs are submitted first so they take the first slots.
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(missing))) as executor:
                futures = [
                    (kind, executor.submit(
                        _get_cached_request, url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0))
                    for kind, url in missing]
                for kind, future in futures:
                    try:
                        fetched_pages.append((kind, future.result() or {}))
                    except Exception:
                        fetched_pages.append((kind, {}))
        except RuntimeError:
            pass

    raw = {}
    for kind, page_data in cached_pages + fetched_pages:
        raw.setdefault(kind, []).append(page_data)

    total_pages = 0
    for pages in raw.values():
        for page_data in pages:
            try:
                total_pages = max(total_pages, int((page_data or {}).get("total_pages") or 0))
            except (TypeError, ValueError):
                continue
    total_pages = min(total_pages, _MAX_DISCOVER_PAGES)

    items = []
    seen_ids = set()
    for kind in kinds:
        app_type = "movie" if kind == "movie" else "series"
        for page_data in (raw.get(kind) or []):
            for entry in (page_data or {}).get("results", []) or []:
                mid = entry.get("id")
                poster_path = entry.get("poster_path")
                if not mid or not poster_path or mid in seen_ids:
                    continue
                if skip_ids and f"tmdb:{mid}" in skip_ids:
                    continue
                seen_ids.add(mid)
                title = (
                    entry.get("title") or entry.get("name")
                    or entry.get("original_title") or entry.get("original_name")
                    or "Unknown"
                )
                rel_date = entry.get("release_date") or entry.get("first_air_date") or ""
                year_str = rel_date[:4] if len(rel_date) >= 4 else ""
                poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
                backdrop_path = entry.get("backdrop_path")
                genre_ids = [g for g in (entry.get("genre_ids") or []) if g in TMDB_GENRES]
                vote_average = entry.get("vote_average")
                items.append({
                    "id": f"tmdb:{mid}",
                    "raw_id": mid,
                    "title": title,
                    "name": title,
                    "type": app_type,
                    "media_type": app_type,
                    "poster": poster,
                    "medium_cover_image": poster,
                    "background": f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else "",
                    "year": year_str,
                    "rating": round(float(vote_average), 1) if vote_average else None,
                    "vote_count": entry.get("vote_count", 0),
                    "popularity": entry.get("popularity", 0.0),
                    "release_date": rel_date,
                    "overview": (entry.get("overview") or "").strip(),
                    "country": country_name or "",
                    "countries": [country_name] if country_name else [],
                    "language": database.LANGUAGE_CODE_MAP.get(str(entry.get("original_language") or "").lower()) or language_name or "",
                    "genres": [TMDB_GENRES[g] for g in genre_ids],
                    "genre_ids": genre_ids,
                    "original_language": entry.get("original_language") or "",
                })

    items.sort(key=lambda it: (it.get("popularity", 0.0), it.get("vote_count", 0)), reverse=True)
    return items[:limit], total_pages


def fetch_title_search_page(query, media_type="all", page=1, limit=50):
    """Fetch one cache-first TMDb title-search page for movies and/or series.

    Search results are converted to the app's regular card shape. The caller
    controls paging so network requests happen only as the user scrolls.
    """
    query = str(query or "").strip()
    if not query:
        return [], 0
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    if page > _MAX_TITLE_SEARCH_PAGES:
        return [], 0

    mt = str(media_type or "all").strip().lower()
    if mt in ("all", "discover", "none", "", "movie_series", "movies_series", "movie+series", "movies+series"):
        kinds = [("movie", "movie"), ("tv", "series")]
    elif mt in ("movie", "movies", "film", "films"):
        kinds = [("movie", "movie")]
    elif mt in ("series", "show", "shows", "tvshow", "tvshows"):
        kinds = [("tv", "series")]
    else:
        return [], 0

    api_key = get_tmdb_api_key()
    if not api_key:
        return [], 0
    urls = []
    for kind, app_type in kinds:
        params = {"api_key": api_key, "query": query, "page": page, "include_adult": "false"}
        urls.append((kind, app_type, "https://api.themoviedb.org/3/search/"
                     f"{kind}?{urllib.parse.urlencode(params)}"))

    # Search the response cache first, then request only missing URLs.
    cached, missing = [], []
    for kind, app_type, url in urls:
        try:
            data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS,
                                       cache_only=True, timeout=5.0)
        except Exception:
            data = None
        if data:
            cached.append((kind, app_type, data))
        else:
            missing.append((kind, app_type, url))

    fetched = []
    if missing:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(missing)) as executor:
                futures = [(kind, app_type, executor.submit(
                    _get_cached_request, url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0))
                    for kind, app_type, url in missing]
                for kind, app_type, future in futures:
                    try:
                        fetched.append((kind, app_type, future.result() or {}))
                    except Exception:
                        fetched.append((kind, app_type, {}))
        except RuntimeError:
            pass

    # TMDb's website uses a unified title search. Its multi-search can return
    # fuzzy matches that the separate movie/TV endpoints miss (for example,
    # "trublood" finding "True Blood"). Keep the focused endpoints as the
    # primary path, then try the same unified search only when they yield no
    # usable movie or TV cards.
    has_usable_title = any(
        entry.get("id") and entry.get("poster_path")
        for _kind, _app_type, data in cached + fetched
        for entry in (data or {}).get("results", []) or []
        if _kind != "multi" or entry.get("media_type") in ("movie", "tv")
    )
    if not has_usable_title and mt in ("all", "discover", "none", "", "movie_series", "movies_series", "movie+series", "movies+series"):
        params = {"api_key": api_key, "query": query, "page": page, "include_adult": "false"}
        multi_url = "https://api.themoviedb.org/3/search/multi?" + urllib.parse.urlencode(params)
        try:
            multi_data = _get_cached_request(
                multi_url, max_age_hours=METADATA_CACHE_HOURS,
                cache_only=True, timeout=5.0,
            )
            if not multi_data:
                multi_data = _get_cached_request(
                    multi_url, max_age_hours=METADATA_CACHE_HOURS, timeout=5.0,
                )
        except Exception:
            multi_data = None
        if multi_data:
            fetched.append(("multi", "", multi_data))

    total_pages = 0
    items, seen_ids = [], set()
    for _kind, app_type, data in cached + fetched:
        try:
            total_pages = max(total_pages, int((data or {}).get("total_pages") or 0))
        except (TypeError, ValueError):
            pass
        for entry in (data or {}).get("results", []) or []:
            if _kind == "multi":
                media_kind = entry.get("media_type")
                if media_kind not in ("movie", "tv"):
                    continue
                app_type = "movie" if media_kind == "movie" else "series"
            tmdb_id = entry.get("id")
            poster_path = entry.get("poster_path")
            if not tmdb_id or not poster_path:
                continue
            item_id = f"tmdb:{tmdb_id}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            title = (entry.get("title") or entry.get("name") or
                     entry.get("original_title") or entry.get("original_name") or "Unknown")
            release_date = entry.get("release_date") or entry.get("first_air_date") or ""
            backdrop_path = entry.get("backdrop_path")
            genre_ids = [gid for gid in (entry.get("genre_ids") or []) if gid in TMDB_GENRES]
            rating = entry.get("vote_average")
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            items.append({
                "id": item_id, "raw_id": tmdb_id, "title": title, "name": title,
                "type": app_type, "media_type": app_type,
                "poster": poster, "medium_cover_image": poster,
                "background": f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else "",
                "year": release_date[:4] if len(release_date) >= 4 else "",
                "rating": round(float(rating), 1) if rating else None,
                "vote_count": entry.get("vote_count", 0), "popularity": entry.get("popularity", 0.0),
                "release_date": release_date, "overview": (entry.get("overview") or "").strip(),
                "genres": [TMDB_GENRES[gid] for gid in genre_ids], "genre_ids": genre_ids,
                "original_language": entry.get("original_language") or "",
                "language": database.LANGUAGE_CODE_MAP.get(str(entry.get("original_language") or "").lower(), ""),
            })

    return items[:limit], min(total_pages, _MAX_TITLE_SEARCH_PAGES)

TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action", 10762: "Family", 10763: "News", 10764: "Reality-TV",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk-Show", 10768: "War"
}

def fetch_collection_details(cid, title=None, poster=None):
    """
    Fetches full movie collection details (parts, overview, posters, ratings)
    directly from the TMDB API when collection addon metadata is missing or empty.
    """
    clean_id = str(cid).replace("ctmdb.", "").strip()
    api_key = get_tmdb_api_key()
    if not api_key:
        return None
    url = f"https://api.themoviedb.org/3/collection/{clean_id}?api_key={api_key}"
    data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS)
    if not data or not isinstance(data, dict) or not data.get("name"):
        return None

    parts = list(data.get("parts", []))
    parts.sort(key=lambda p: p.get("release_date") or "9999-99-99")

    def resolve_part(p_tuple):
        idx, p = p_tuple
        pid = p.get("id")
        t = p.get("title") or f"Part {idx+1}"
        imdb_id = resolve_to_imdb_id(f"tmdb:{pid}", "movie", t) or f"tmdb:{pid}"
        return idx, imdb_id

    resolved_ids = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(parts), 5) or 1) as executor:
        try:
            for idx, i_id in executor.map(resolve_part, enumerate(parts), timeout=10.0):
                resolved_ids[idx] = i_id
        except Exception as e:
            logging.debug(f"Timeout or error resolving parts: {e}")

    videos = []
    genre_names = set()
    total_vote = 0.0
    vote_count = 0

    for idx, p in enumerate(parts):
        t = p.get("title") or f"Part {idx+1}"
        rel = p.get("release_date") or ""
        overview = p.get("overview") or ""
        vote = p.get("vote_average", 0.0)
        if vote > 0:
            total_vote += vote
            vote_count += 1
        for gid in p.get("genre_ids", []):
            if gid in TMDB_GENRES:
                genre_names.add(TMDB_GENRES[gid])

        b_path = p.get("backdrop_path") or p.get("poster_path")
        thumb = f"https://image.tmdb.org/t/p/w500{b_path}" if b_path else ""
        part_id = p.get("id")

        videos.append({
            "id": resolved_ids.get(idx, f"tmdb:{part_id}"),
            "season": 1,
            "episode": idx + 1,
            "title": t,
            "overview": f"[IMDB: {vote:.1f}⭐] {overview}" if vote > 0 else overview,
            "released": rel,
            "thumbnail": thumb
        })

    avg_rating = f"{total_vote / vote_count:.1f}" if vote_count > 0 else ""
    p_path = data.get("poster_path")
    cov = f"https://image.tmdb.org/t/p/w500{p_path}" if p_path else (poster or "")
    bg_path = data.get("backdrop_path")
    backdrop = f"https://image.tmdb.org/t/p/original{bg_path}" if bg_path else ""
    year = parts[0].get("release_date", "")[:4] if parts and parts[0].get("release_date") else ""

    return {
        "id": f"ctmdb.{clean_id}",
        "title": data.get("name") or title or "Collection",
        "year": year,
        "medium_cover_image": cov,
        "background": backdrop,
        "description": data.get("overview") or "No synopsis available.",
        "runtime": "",
        "genre": ", ".join(sorted(genre_names)) if genre_names else "Collection",
        "imdbRating": avg_rating,
        "trailer": None,
        "videos": videos,
        "cast": [],
        "genres": sorted(list(genre_names)),
        "type": "collections",
        "is_collection": True
    }


def fetch_title_language_and_genres(item_id, media_type="movie", title=None, force_refresh=False):
    """
    Fetch title language (e.g. 'Hindi') and canonical genres (e.g. ['Thriller', 'Crime', 'Drama'])
    from TMDB.
    Returns:
      {
        "language_code": str,
        "language_name": str,
        "genres": [str, ...]
      } or None
    """
    if not item_id and not title:
        return None

    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    str_id = str(item_id or "").strip()
    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"

    clean_tt = None
    if str_id.startswith("tt"):
        clean_tt = str_id.split(":")[0]
    else:
        tt_match = re.search(r'\b(tt\d{7,8})\b', str_id)
        if tt_match:
            clean_tt = tt_match.group(1)

    result_item = None
    if clean_tt:
        try:
            find_url = f"https://api.themoviedb.org/3/find/{clean_tt}?api_key={api_key}&external_source=imdb_id"
            data = _get_cached_request(find_url, max_age_hours=0 if force_refresh else 168)
            if data:
                if c_type == "tv" and data.get("tv_results"):
                    result_item = data["tv_results"][0]
                elif data.get("movie_results"):
                    result_item = data["movie_results"][0]
                elif data.get("tv_results"):
                    result_item = data["tv_results"][0]
        except Exception:
            pass

    if not result_item:
        tmdb_id = resolve_to_tmdb_id(item_id, media_type, title)
        if tmdb_id:
            try:
                tm_type = "tv" if c_type == "tv" else "movie"
                url = f"https://api.themoviedb.org/3/{tm_type}/{tmdb_id}?api_key={api_key}"
                data = _get_cached_request(url, max_age_hours=0 if force_refresh else 168)
                if data:
                    result_item = data
            except Exception:
                pass

    if not result_item:
        return None

    orig_lang = result_item.get("original_language")
    lang_name = None
    if orig_lang:
        lang_name = database.LANGUAGE_CODE_MAP.get(orig_lang.lower())
        if not lang_name:
            for sl in result_item.get("spoken_languages", []):
                if sl.get("iso_639_1") == orig_lang:
                    lang_name = sl.get("english_name")
                    break

    tmdb_genres = []
    for gid in result_item.get("genre_ids", []):
        gname = TMDB_GENRES.get(gid)
        if gname:
            norm = database.normalize_imdb_genre(gname, media_type=media_type)
            if norm and norm not in tmdb_genres:
                tmdb_genres.append(norm)

    for gdict in result_item.get("genres", []):
        if isinstance(gdict, dict) and gdict.get("name"):
            norm = database.normalize_imdb_genre(gdict["name"], media_type=media_type)
            if norm and norm not in tmdb_genres:
                tmdb_genres.append(norm)

    countries = []
    for oc in result_item.get("origin_country", []):
        norm_c = database.normalize_country_name(oc)
        if norm_c and norm_c not in countries:
            countries.append(norm_c)
    for pc in result_item.get("production_countries", []):
        if isinstance(pc, dict):
            c_name = pc.get("name") or pc.get("iso_3166_1")
            norm_c = database.normalize_country_name(c_name)
            if norm_c and norm_c not in countries:
                countries.append(norm_c)
    primary_country = countries[0] if countries else None

    # /find results contain genre IDs but omit the studio/network fields. Fetch
    # the canonical detail record so Discover can filter by the actual network
    # (for TV) or production company (for films).
    production_companies = []
    networks = []
    tmdb_id = result_item.get("id")
    if tmdb_id:
        try:
            details_url = f"https://api.themoviedb.org/3/{c_type}/{tmdb_id}?api_key={api_key}"
            details = _get_cached_request(details_url, max_age_hours=0 if force_refresh else 168)
            if isinstance(details, dict):
                for company in details.get("production_companies", []):
                    if isinstance(company, dict) and company.get("name"):
                        production_companies.append({"id": company.get("id"), "name": company["name"]})
                for network in details.get("networks", []):
                    if isinstance(network, dict) and network.get("name"):
                        networks.append({"id": network.get("id"), "name": network["name"]})
                if details.get("production_countries"):
                    result_item["production_countries"] = details["production_countries"]
        except Exception:
            pass

    return {
        "language_code": orig_lang,
        "language_name": lang_name,
        "genres": tmdb_genres,
        "country": primary_country,
        "countries": countries,
        "production_companies": production_companies,
        "networks": networks,
        "adult": bool(result_item.get("adult", False)),
        "tmdb_id": result_item.get("id")
    }


def fetch_tmdb_standalone_metadata(item_id, media_type="movie", title=None, poster=None):
    """
    Fetch comprehensive metadata directly from TMDB when Cinemeta or other addons fail or return incomplete metadata.
    Returns metadata dict compatible with PopcornBox UI (MovieDetailsPage, database cache).
    """
    if not item_id and not title:
        return None

    api_key = get_tmdb_api_key()
    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"
    tmdb_id = resolve_to_tmdb_id(item_id, media_type, title)

    # If TMDB ID could not be resolved, try public Stremio TMDB addon with item_id
    if not tmdb_id:
        try:
            addon_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{media_type}/{urllib.parse.quote(str(item_id), safe=':')}.json"
            addon_data = _get_cached_request(addon_url, max_age_hours=METADATA_CACHE_HOURS)
            if addon_data and "meta" in addon_data:
                meta = addon_data["meta"]
                return {
                    "id": meta.get("id") or item_id,
                    "title": meta.get("name") or title or "Media Item",
                    "year": str(meta.get("year", "")),
                    "medium_cover_image": meta.get("poster") or poster or "",
                    "background": meta.get("background") or "",
                    "description": meta.get("description") or "No synopsis available.",
                    "runtime": str(meta.get("runtime", "")),
                    "genre": ", ".join(meta.get("genres", [])) if isinstance(meta.get("genres"), list) else (meta.get("genre") or ""),
                    "genres": meta.get("genres", []),
                    "imdbRating": str(meta.get("imdbRating", "")),
                    "trailer": meta.get("trailer"),
                    "videos": meta.get("videos", [])
                }
        except Exception:
            pass
        return None

    # Fetch full TMDB object
    data = None
    if api_key:
        try:
            url = f"https://api.themoviedb.org/3/{c_type}/{tmdb_id}?api_key={api_key}&append_to_response=external_ids,credits,videos,content_ratings,release_dates"
            data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS)
        except Exception:
            pass

    if not data:
        # Fallback to public Stremio TMDB addon
        try:
            addon_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{media_type}/tmdb:{tmdb_id}.json"
            addon_data = _get_cached_request(addon_url, max_age_hours=METADATA_CACHE_HOURS)
            if addon_data and "meta" in addon_data:
                meta = addon_data["meta"]
                return {
                    "id": meta.get("id") or (f"tmdb:{tmdb_id}" if not str(item_id).startswith("tt") else item_id),
                    "title": meta.get("name") or title or "Media Item",
                    "year": str(meta.get("year", "")),
                    "medium_cover_image": meta.get("poster") or poster or "",
                    "background": meta.get("background") or "",
                    "description": meta.get("description") or "No synopsis available.",
                    "runtime": str(meta.get("runtime", "")),
                    "genre": ", ".join(meta.get("genres", [])) if isinstance(meta.get("genres"), list) else (meta.get("genre") or ""),
                    "genres": meta.get("genres", []),
                    "imdbRating": str(meta.get("imdbRating", "")),
                    "trailer": meta.get("trailer"),
                    "videos": meta.get("videos", [])
                }
        except Exception:
            pass
        return None

    # Parse TMDB API response
    imdb_id = data.get("external_ids", {}).get("imdb_id") or (item_id if str(item_id).startswith("tt") else f"tmdb:{tmdb_id}")
    clean_title = data.get("title") or data.get("name") or title or "Media Item"
    overview = data.get("overview") or "No synopsis available."
    rel_date = data.get("release_date") or data.get("first_air_date") or ""
    year = rel_date.split("-")[0] if rel_date else ""
    poster_path = data.get("poster_path")
    backdrop_path = data.get("backdrop_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else (poster or "")
    backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else ""

    vote_avg = data.get("vote_average")
    rating_str = f"{vote_avg:.1f}" if vote_avg else ""

    runtime_min = data.get("runtime")
    if not runtime_min and data.get("episode_run_time"):
        r_list = data.get("episode_run_time")
        if r_list: runtime_min = r_list[0]
    runtime_str = f"{runtime_min} min" if runtime_min else ""

    genre_names = []
    for g in data.get("genres", []):
        if isinstance(g, dict) and g.get("name"):
            norm = database.normalize_imdb_genre(g["name"], media_type=media_type)
            if norm and norm not in genre_names:
                genre_names.append(norm)
            elif g["name"] not in genre_names:
                genre_names.append(g["name"])

    # Cast & Director
    credits = data.get("credits", {})
    cast_list = [c.get("name") for c in credits.get("cast", [])[:10] if c.get("name")]
    director_name = ""
    for cr in credits.get("crew", []):
        if cr.get("job") == "Director":
            director_name = cr.get("name", "")
            break

    # Trailer
    trailer_url = None
    videos_res = data.get("videos", {}).get("results", [])
    for v in videos_res:
        if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser") and v.get("key"):
            trailer_url = f"https://www.youtube.com/watch?v={v['key']}"
            if v.get("type") == "Trailer":
                break

    # Videos / Episodes for Series
    episodes = []
    if c_type == "tv" and data.get("seasons"):
        season_numbers = [s.get("season_number") for s in data.get("seasons", [])
                          if s.get("season_number") is not None and s.get("season_number") >= 1]
        def _fetch_season_episodes(s_num):
            try:
                s_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s_num}?api_key={api_key}"
                s_data = _get_cached_request(s_url, max_age_hours=METADATA_CACHE_HOURS)
                out = []
                if s_data and s_data.get("episodes"):
                    for ep in s_data["episodes"]:
                        ep_num = ep.get("episode_number")
                        out.append({
                            "id": f"{imdb_id}:{s_num}:{ep_num}",
                            "title": ep.get("name") or f"Episode {ep_num}",
                            "name": ep.get("name") or f"Episode {ep_num}",
                            "season": s_num,
                            "episode": ep_num,
                            "overview": ep.get("overview") or "",
                            "released": ep.get("air_date") or "",
                            "thumbnail": f"https://image.tmdb.org/t/p/w500{ep.get('still_path')}" if ep.get("still_path") else ""
                        })
                return s_num, out
            except Exception:
                return s_num, []

        if season_numbers:
            # Seasons are independent requests; fetch them concurrently instead
            # of one TMDB round-trip per season on the UI thread.
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(season_numbers), 6)) as ex:
                results = list(ex.map(_fetch_season_episodes, season_numbers))
            for _s_num, eps in results:
                episodes.extend(eps)

    countries = []
    for oc in data.get("origin_country", []):
        norm_c = database.normalize_country_name(oc)
        if norm_c and norm_c not in countries:
            countries.append(norm_c)
    for pc in data.get("production_countries", []):
        if isinstance(pc, dict):
            c_name = pc.get("name") or pc.get("iso_3166_1")
            norm_c = database.normalize_country_name(c_name)
            if norm_c and norm_c not in countries:
                countries.append(norm_c)
    primary_country = countries[0] if countries else None
    orig_lang = data.get("original_language")
    lang_name = database.LANGUAGE_CODE_MAP.get(orig_lang.lower()) if orig_lang else None

    return {
        "id": imdb_id,
        "title": clean_title,
        "year": year,
        "medium_cover_image": poster_url,
        "background": backdrop_url,
        "description": overview,
        "runtime": runtime_str,
        "genre": ", ".join(genre_names),
        "genres": genre_names,
        "country": primary_country,
        "countries": countries,
        "language": lang_name,
        "imdbRating": rating_str,
        "cast": cast_list,
        "director": director_name,
        "trailer": trailer_url,
        "videos": episodes
    }



