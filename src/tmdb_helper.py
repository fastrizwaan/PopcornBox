import re
import urllib.parse
from . import database

def resolve_to_imdb_id(imdb_id, media_type, title=None):
    """
    Resolves a TMDB ID to an IMDB ID using multiple fallback strategies.
    If the ID is already an IMDB ID, returns it immediately.
    """
    from .api import _get_cached_request
    
    is_tmdb = str(imdb_id).startswith("tmdb:") or str(imdb_id).startswith("ctmdb.")
    if not is_tmdb:
        return imdb_id
        
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
            addon_url = f"https://tmdb-addon.strem.io/meta/{c_type}/tmdb:{tmdb_id}.json"
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
