import re
import urllib.parse
from . import database

def resolve_to_imdb_id(imdb_id, media_type, title=None):
    """
    Resolves a TMDB ID or raw ID to an IMDB ID using multiple fallback strategies.
    If the ID is already an IMDB ID, returns it clean (tt...).
    """
    if isinstance(imdb_id, list):
        return [resolve_to_imdb_id(i, media_type, title) for i in imdb_id]
        
    if not imdb_id:
        return None

    str_id = str(imdb_id).strip()
    if str_id.startswith("tt"):
        return str_id.split(":")[0]

    from .api import _get_cached_request
    
    is_tmdb = str_id.startswith("tmdb:") or str_id.startswith("ctmdb.") or str_id.isdigit()
    if not is_tmdb:
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
    """
    import concurrent.futures
    ids = set()
    if isinstance(item_id, list):
        for i in item_id:
            if i: ids.add(str(i))
    elif item_id:
        ids.add(str(item_id))

    def task_imdb():
        res = resolve_to_imdb_id(item_id, media_type, title)
        if isinstance(res, list): return res
        return [res] if res else []

    def task_dsf():
        has_dsf = any(i.startswith("dsf:") for i in ids)
        if not has_dsf and title and title != "Loading...":
            try:
                from .api import _get_cached_request
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://desiflix.stremioaddon.workers.dev/catalog/{c_type}/desiflix/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("id")
                        if m_id and str(m_id).startswith("dsf:"):
                            return [str(m_id)]
            except Exception:
                pass
        return []

    def task_tt():
        has_tt = any(i.startswith("tt") for i in ids)
        if not has_tt and title and title != "Loading...":
            try:
                from .api import _get_cached_request
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("imdb_id") or m.get("id", "")
                        if str(m_id).startswith("tt"):
                            return [str(m_id)]
            except Exception:
                pass
        return []

    def task_tmdb(current_ids):
        res = []
        has_tmdb = any(i.startswith("tmdb:") for i in current_ids)
        if not has_tmdb:
            tt_id = next((i for i in current_ids if str(i).startswith("tt")), None)
            root_tt_id = tt_id.split(":")[0] if tt_id else None
            if root_tt_id:
                try:
                    from .api import _get_cached_request
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    meta_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{root_tt_id}.json"
                    meta_data = _get_cached_request(meta_url, max_age_hours=168)
                    if meta_data and "meta" in meta_data:
                        t_id = meta_data["meta"].get("id")
                        if t_id and str(t_id).startswith("tmdb:"):
                            res.append(str(t_id))
                except Exception:
                    pass
            if not res and title and title != "Loading...":
                try:
                    from .api import _get_cached_request
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    search_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/catalog/{c_type}/tmdb.top/search={urllib.parse.quote(title)}.json"
                    search_data = _get_cached_request(search_url, max_age_hours=168)
                    if search_data and "metas" in search_data:
                        for m in search_data["metas"]:
                            m_id = m.get("id")
                            if m_id and str(m_id).startswith("tmdb:"):
                                res.append(str(m_id))
                                break
                except Exception:
                    pass
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f1 = executor.submit(task_imdb)
        f2 = executor.submit(task_dsf)
        f3 = executor.submit(task_tt)

        for f in [f1, f2, f3]:
            try:
                for r in f.result(timeout=1.5):
                    if r: ids.add(str(r))
            except Exception:
                pass

        if not any(i.startswith("tmdb:") for i in ids):
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
