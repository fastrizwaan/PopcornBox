import re
import urllib.parse
import json
import base64
import concurrent.futures
from . import database

def _get_cached_request(*args, **kwargs):
    from .api import _get_cached_request as req
    return req(*args, **kwargs)


def resolve_to_imdb_id(imdb_id, media_type, title=None):
    """
    Resolves a TMDB ID or raw ID to an IMDB ID using multiple fallback strategies.
    If the ID is already an IMDB ID, returns it clean (tt...).
    """
    if isinstance(imdb_id, list):
        return [resolve_to_imdb_id(i, media_type, title) for i in imdb_id]
        
    if not imdb_id and not title:
        return None

    if imdb_id:
        str_id = str(imdb_id).strip()
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

    is_tmdb = False
    if imdb_id:
        str_id = str(imdb_id).strip()
        if str_id.startswith("bolly:") or str_id.startswith("hub:"):
            is_tmdb = True
            if ":s:" in str_id:
                media_type = "series"
            elif ":m:" in str_id:
                media_type = "movie"
        else:
            is_tmdb = str_id.startswith("tmdb:") or str_id.startswith("ctmdb.") or str_id.isdigit()
        if not is_tmdb and not title:
            return str_id
        
    resolved_id = None
    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
    
    # 1. Try official TMDB API if user has an addon with the API key
    try:
        tmdb_api_key = None
        for addon in database.get_addons():
            m_url = addon.get("manifest_url", "")
            if "tmdb" in m_url.lower():
                match = re.search(r'/([a-fA-F0-9]{32})/', m_url)
                if match:
                    tmdb_api_key = match.group(1)
                    break
                    
        if tmdb_api_key:
            tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
            tmdb_type = "tv" if c_type == "series" else "movie"
            tmdb_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={tmdb_api_key}&append_to_response=external_ids"
            
            tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168)
            if tmdb_data and "external_ids" in tmdb_data:
                resolved_id = tmdb_data["external_ids"].get("imdb_id")
    except Exception as e:
        pass
        
    # 2. Try the public Stremio TMDB addon (requires no API key)
    if not resolved_id:
        try:
            tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
            addon_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/tmdb:{tmdb_id}.json"
            addon_data = _get_cached_request(addon_url, max_age_hours=168)
            if addon_data and "meta" in addon_data:
                resolved_id = addon_data["meta"].get("imdb_id")
        except Exception:
            pass

    # 3. Fallback to Cinemeta Search by title (safe substring matching)
    if not resolved_id and title and title != "Loading...":
        try:
            search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
            search_data = _get_cached_request(search_url, max_age_hours=168)
            if search_data and "metas" in search_data:
                for m in search_data["metas"]:
                    m_id = m.get("imdb_id") or m.get("id", "")
                    m_name = str(m.get("name", "")).lower()
                    t_lower = str(title).lower()
                    # Safe check: title contains or is contained in the match, and id is IMDB
                    if str(m_id).startswith("tt") and (m_name in t_lower or t_lower in m_name):
                        resolved_id = m_id
                        break
        except Exception:
            pass
            
    return resolved_id or imdb_id

def resolve_all_provider_ids(item_id, media_type="movie", title=None):
    """
    Returns a list of all distinct provider IDs (IMDB tt..., TMDB, DesiFlix dsf:...)
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

    needs_dsf = any(p.startswith("dsf") for p in needed_prefixes)
    needs_tmdb = any(p.startswith("tmdb") for p in needed_prefixes)
    has_tt = any(str(i).startswith("tt") for i in ids)

    # If we already have tt... and don't need dsf or tmdb, return immediately!
    if has_tt and not needs_dsf and not needs_tmdb:
        return list(ids)

    def task_imdb():
        res = resolve_to_imdb_id(item_id, media_type, title)
        if isinstance(res, list): return res
        return [res] if res else []

    def task_dsf():
        if not needs_dsf:
            return []
        has_dsf = any(str(i).startswith("dsf:") for i in ids)
        if not has_dsf and title and title != "Loading...":
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://desiflix.stremioaddon.workers.dev/catalog/{c_type}/desiflix/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("id")
                        if m_id and str(m_id).startswith("dsf:"):
                            return [str(m_id)]
            except Exception:
                pass
        return []

    def task_tt():
        has_tt_curr = any(str(i).startswith("tt") for i in ids)
        if not has_tt_curr and title and title != "Loading...":
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("imdb_id") or m.get("id", "")
                        if str(m_id).startswith("tt"):
                            return [str(m_id)]
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
                    meta_data = _get_cached_request(meta_url, max_age_hours=168, timeout=2.5)
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
                    search_data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                    if search_data and "metas" in search_data:
                        for m in search_data["metas"]:
                            m_id = m.get("id")
                            if m_id and str(m_id).startswith("tmdb:"):
                                res.append(str(m_id))
                                break
                except Exception:
                    pass
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(task_imdb)
        f2 = executor.submit(task_dsf)
        f3 = executor.submit(task_tt)

        for f in [f1, f2, f3]:
            try:
                for r in f.result(timeout=1.5):
                    if r: ids.add(str(r))
            except Exception:
                pass

        if needs_tmdb and not any(str(i).startswith("tmdb:") for i in ids):
            f4 = executor.submit(task_tmdb, list(ids))
            try:
                for r in f4.result(timeout=1.5):
                    if r: ids.add(str(r))
            except Exception:
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
