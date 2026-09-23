import json
import os
import sys
import threading
import sqlite3
import time
from pathlib import Path

_db_lock = threading.RLock()
_cache_db_lock = threading.RLock()
_db_corrupted = False

# In-memory cache for data.json to avoid repeated file reads
_json_cache = None
_json_cache_valid = False

# Persistent SQLite connection (reused across all cache calls)
_cache_conn = None
_cache_db_initialized = False

def _is_testing():
    return (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or bool(os.environ.get("POPCORN_TEST"))
    )

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "popcorn-box"
else:
    flatpak_data = Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box"
    if flatpak_data.exists() and not _is_testing():
        BASE_DIR = flatpak_data
    else:
        BASE_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "popcorn-box"

CONFIG_DIR = BASE_DIR / "config"
os.makedirs(CONFIG_DIR, exist_ok=True)

DB_FILE = CONFIG_DIR / "data.json"

HISTORY_LIMIT = 100

DEFAULT_ADDONS = [
    {
        "id": "cinemeta",
        "name": "Cinemeta",
        "version": "3.0.0",
        "description": "Provides movies and series catalogs from IMDb.",
        "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
        "enabled": True,
        "resources": ["catalog", "meta"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [
            {"type": "movie", "id": "top", "genres": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western"], "extra": [{"name": "genre", "options": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western"]}, {"name": "search"}, {"name": "skip"}], "name": "Popular"},
            {"type": "movie", "id": "year", "genres": ["2026","2025","2024","2023","2022","2021","2020","2019","2018","2017","2016","2015","2014","2013","2012","2011","2010","2009","2008","2007","2006","2005","2004","2003","2002","2001","2000","1999","1998","1997","1996","1995","1994","1993","1992","1991","1990","1989","1988","1987","1986","1985","1984","1983","1982","1981","1980","1979","1978","1977","1976","1975","1974","1973","1972","1971","1970","1969","1968","1967","1966","1965","1964","1963","1962","1961","1960","1959","1958","1957","1956","1955","1954","1953","1952","1951","1950","1949","1948","1947","1946","1945","1944","1943","1942","1941","1940","1939","1938","1937","1936","1935","1934","1933","1932","1931","1930","1929","1928","1927","1926","1925","1924","1923","1922","1921","1920"], "extra": [{"name": "genre", "options": ["2026","2025","2024","2023","2022","2021","2020","2019","2018","2017","2016","2015","2014","2013","2012","2011","2010","2009","2008","2007","2006","2005","2004","2003","2002","2001","2000","1999","1998","1997","1996","1995","1994","1993","1992","1991","1990","1989","1988","1987","1986","1985","1984","1983","1982","1981","1980","1979","1978","1977","1976","1975","1974","1973","1972","1971","1970","1969","1968","1967","1966","1965","1964","1963","1962","1961","1960","1959","1958","1957","1956","1955","1954","1953","1952","1951","1950","1949","1948","1947","1946","1945","1944","1943","1942","1941","1940","1939","1938","1937","1936","1935","1934","1933","1932","1931","1930","1929","1928","1927","1926","1925","1924","1923","1922","1921","1920"]}, {"name": "skip"}], "name": "New"},
            {"type": "movie", "id": "imdbRating", "genres": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western"], "extra": [{"name": "genre", "options": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western"]}, {"name": "skip"}], "name": "Featured"},
            {"type": "series", "id": "top", "genres": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western","Reality-TV","Talk-Show","Game-Show"], "extra": [{"name": "genre", "options": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western","Reality-TV","Talk-Show","Game-Show"]}, {"name": "search"}, {"name": "skip"}], "name": "Popular"},
            {"type": "series", "id": "year", "genres": ["2026","2025","2024","2023","2022","2021","2020","2019","2018","2017","2016","2015","2014","2013","2012","2011","2010","2009","2008","2007","2006","2005","2004","2003","2002","2001","2000","1999","1998","1997","1996","1995","1994","1993","1992","1991","1990","1989","1988","1987","1986","1985","1984","1983","1982","1981","1980","1979","1978","1977","1976","1975","1974","1973","1972","1971","1970","1969","1968","1967","1966","1965","1964","1963","1962","1961","1960"], "extra": [{"name": "genre", "options": ["2026","2025","2024","2023","2022","2021","2020","2019","2018","2017","2016","2015","2014","2013","2012","2011","2010","2009","2008","2007","2006","2005","2004","2003","2002","2001","2000","1999","1998","1997","1996","1995","1994","1993","1992","1991","1990","1989","1988","1987","1986","1985","1984","1983","1982","1981","1980","1979","1978","1977","1976","1975","1974","1973","1972","1971","1970","1969","1968","1967","1966","1965","1964","1963","1962","1961","1960"]}, {"name": "skip"}], "name": "New"},
            {"type": "series", "id": "imdbRating", "genres": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western","Reality-TV","Talk-Show","Game-Show"], "extra": [{"name": "genre", "options": ["Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy","History","Horror","Mystery","Romance","Sci-Fi","Sport","Thriller","War","Western","Reality-TV","Talk-Show","Game-Show"]}, {"name": "skip"}], "name": "Featured"}
        ]
    },
    {
        "id": "anime-kitsu",
        "name": "Anime Kitsu",
        "version": "1.0.0",
        "description": "Provides anime catalogs from Kitsu.",
        "manifest_url": "https://anime-kitsu.strem.fun/manifest.json",
        "enabled": True,
        "resources": ["catalog", "meta"],
        "types": ["anime", "series", "movie"],
        "idPrefixes": ["kitsu:"],
        "catalogs": [
            {
                "type": "anime",
                "id": "kitsu-anime-trending",
                "name": "Trending",
                "genres": ["Action","Adventure","Cars","Comedy","Dementia","Demons","Drama","Ecchi","Fantasy","Game","Harem","Historical","Horror","Josei","Kids","Magic","Martial Arts","Mecha","Military","Music","Mystery","Parody","Police","Psychological","Romance","Samurai","School","Sci-Fi","Seinen","Shoujo","Shounen","Slice of Life","Space","Sports","Super Power","Supernatural","Thriller","Vampire"],
                "extra": [{"name": "genre", "options": ["Action","Adventure","Cars","Comedy","Dementia","Demons","Drama","Ecchi","Fantasy","Game","Harem","Historical","Horror","Josei","Kids","Magic","Martial Arts","Mecha","Military","Music","Mystery","Parody","Police","Psychological","Romance","Samurai","School","Sci-Fi","Seinen","Shoujo","Shounen","Slice of Life","Space","Sports","Super Power","Supernatural","Thriller","Vampire"]}, {"name": "search"}, {"name": "skip"}]
            },
            {
                "type": "anime",
                "id": "kitsu-anime-top",
                "name": "Top Rated",
                "genres": ["Action","Adventure","Cars","Comedy","Dementia","Demons","Drama","Ecchi","Fantasy","Game","Harem","Historical","Horror","Josei","Kids","Magic","Martial Arts","Mecha","Military","Music","Mystery","Parody","Police","Psychological","Romance","Samurai","School","Sci-Fi","Seinen","Shoujo","Shounen","Slice of Life","Space","Sports","Super Power","Supernatural","Thriller","Vampire"],
                "extra": [{"name": "genre", "options": ["Action","Adventure","Cars","Comedy","Dementia","Demons","Drama","Ecchi","Fantasy","Game","Harem","Historical","Horror","Josei","Kids","Magic","Martial Arts","Mecha","Military","Music","Mystery","Parody","Police","Psychological","Romance","Samurai","School","Sci-Fi","Seinen","Shoujo","Shounen","Slice of Life","Space","Sports","Super Power","Supernatural","Thriller","Vampire"]}, {"name": "skip"}]
            }
        ]
    },
    {
        "id": "local.iptv-org",
        "name": "IPTV Org TV Channels",
        "version": "1.0.0",
        "description": "Free worldwide live TV channels.",
        "manifest_url": "https://iptv-org.github.io/manifest.json",
        "enabled": True,
        "resources": ["catalog", "meta", "stream"],
        "types": ["tv", "channel", "tvchannel"],
        "idPrefixes": ["iptv:"],
        "catalogs": [
            {"type": "tv", "id": "US", "name": "USA TV Channels"},
            {"type": "tv", "id": "UK", "name": "UK TV Channels"},
            {"type": "tv", "id": "IN", "name": "India TV Channels"},
            {"type": "tv", "id": "CA", "name": "Canada TV Channels"},
            {"type": "tv", "id": "ALL", "name": "All World Channels"}
        ]
    }
]

def _ensure_db():
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True)
    if not DB_FILE.exists():
        with open(DB_FILE, "w") as f:
            json.dump({"favorites": [], "watched": [], "history": [], "continue_watching": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}, f, indent=4)

def _read_db():
    global _db_corrupted, _json_cache, _json_cache_valid
    with _db_lock:
        if _json_cache_valid and _json_cache is not None:
            return _json_cache
        _ensure_db()
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
            # Migrate older databases
            if "history" not in data:
                data["history"] = []
            if "continue_watching" not in data:
                data["continue_watching"] = []
            if "downloads" not in data:
                data["downloads"] = []
            if "settings" not in data:
                data["settings"] = {}
            if "hide_adult_content" not in data["settings"]:
                data["settings"]["hide_adult_content"] = True
            if "addons" not in data:
                data["addons"] = DEFAULT_ADDONS
            else:
                # Ensure default/bundled addons are present in the user database unless intentionally removed
                migrated = False
                removed_defaults = set(data.get("settings", {}).get("removed_default_addons", []))
                for default_addon in DEFAULT_ADDONS:
                    if default_addon.get("id") in removed_defaults:
                        continue
                    found = False
                    for a in data["addons"]:
                        if a.get("id") == default_addon["id"]:
                            found = True
                            # Migrate missing resources/types/idPrefixes to existing default addons
                            if "resources" in default_addon and "resources" not in a:
                                a["resources"] = default_addon["resources"]
                                migrated = True
                            if "types" in default_addon and "types" not in a:
                                a["types"] = default_addon["types"]
                                migrated = True
                            if "idPrefixes" in default_addon and "idPrefixes" not in a:
                                a["idPrefixes"] = default_addon["idPrefixes"]
                                migrated = True

                            # Migrate missing catalogs to existing addons
                            if "catalogs" in default_addon and "catalogs" not in a:
                                a["catalogs"] = default_addon["catalogs"]
                                migrated = True
                                
                            # Migrate Cinemeta to have genres, 'year' catalog, and updated 'Featured' name
                            if a.get("id") == "cinemeta" and "catalogs" in a:
                                has_extra = any("extra" in cat for cat in a["catalogs"])
                                has_year = any(cat.get("id") == "year" for cat in a["catalogs"])
                                has_old_imdb_name = any(cat.get("id") == "imdbRating" and cat.get("name") == "IMDb Rating" for cat in a["catalogs"])
                                if not has_extra or not has_year or has_old_imdb_name:
                                    a["catalogs"] = default_addon["catalogs"]
                                    migrated = True
                            # Migrate dead TMDB url
                            if a.get("id") == "org.stremio.tmdb" and ("tmdb.strem.fun" in a.get("manifest_url", "") or "tmdb-addon.strem.io" in a.get("manifest_url", "")):
                                a["manifest_url"] = "https://94c8cb9f702d-tmdb-addon.baby-beamup.club/manifest.json"
                                migrated = True
                            break
                    if not found:
                        data["addons"].append(default_addon)
                        migrated = True

                # Migrate HdHub catalogs if missing or empty
                for a in data["addons"]:
                    if (a.get("id") == "com.stremio.HdHub" or "hdhub" in str(a.get("manifest_url", "")).lower()) and not a.get("catalogs"):
                        a["catalogs"] = [
                            {
                                "type": "HdHub",
                                "id": "hdhub-featured-movies",
                                "name": "Featured Movies",
                                "extra": [{"name": "search", "isRequired": False}],
                                "extraSupported": ["search"]
                            },
                            {
                                "type": "HdHub",
                                "id": "hdhub-featured-series",
                                "name": "Featured Series",
                                "extra": [{"name": "search", "isRequired": False}],
                                "extraSupported": ["search"]
                            },
                            {
                                "type": "HdHub",
                                "id": "hdhub-popular-movies",
                                "name": "Popular Movies",
                                "extra": [{"name": "search", "isRequired": False}],
                                "extraSupported": ["search"]
                            },
                            {
                                "type": "HdHub",
                                "id": "hdhub-popular-series",
                                "name": "Popular Series",
                                "extra": [{"name": "search", "isRequired": False}],
                                "extraSupported": ["search"]
                            }
                        ]
                        migrated = True

                # Ensure stream-only addons do not retain stale catalogs
                for a in data["addons"]:
                    if a.get("id") == "com.penguplay" and a.get("catalogs"):
                        a["catalogs"] = []
                        migrated = True

                # Deduplicate addons by ID or name
                unique_addons = []
                seen_addon_ids = set()
                seen_addon_names = set()
                for a in data["addons"]:
                    aid = a.get("id")
                    aname = a.get("name")
                    if aid and aid in seen_addon_ids:
                        continue
                    if aname and aname in seen_addon_names and ("tmdb" in str(aid).lower() or "tmdb" in str(aname).lower()):
                        continue
                    if aid:
                        seen_addon_ids.add(aid)
                    if aname:
                        seen_addon_names.add(aname)
                    unique_addons.append(a)
                if len(unique_addons) != len(data["addons"]):
                    data["addons"] = unique_addons
                    migrated = True

                if migrated:
                    _write_db(data)
            _json_cache = data
            _json_cache_valid = True
            return data
        except Exception as e:
            print(f"Failed to read database: {e}. Refusing future writes to prevent corruption.")
            _db_corrupted = True
            return {"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}

def reset_json_cache():
    global _json_cache, _json_cache_valid
    _json_cache = None
    _json_cache_valid = False

def _write_db(data):
    global _json_cache, _json_cache_valid
    if _db_corrupted:
        print("Database read failed previously. Refusing to write to avoid overwriting with defaults.")
        return False
    if _is_testing() and ("io.github.fastrizwaan.PopcornBox" in str(DB_FILE) or str(DB_FILE) == str(Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box/config/data.json")):
        print(f"[SECURITY] Blocked unittests from writing to user database: {DB_FILE}")
        return False
    with _db_lock:
        _ensure_db()
        temp_file = DB_FILE.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=4)
            temp_file.replace(DB_FILE)
            # Update in-memory cache with the data we just wrote
            _json_cache = data
            _json_cache_valid = True
            return True
        except Exception as e:
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            _json_cache_valid = False
            print(f"Error writing database: {e}")
            return False

# --- Favorites ---

def get_favorites():
    return _read_db().get("favorites", [])

def add_favorite(item):
    with _db_lock:
        db = _read_db()
        if not any(f.get("id") == item.get("id") for f in db.get("favorites", [])):
            db.setdefault("favorites", []).insert(0, item)
            _write_db(db)

def remove_favorite(item_id):
    with _db_lock:
        db = _read_db()
        db["favorites"] = [f for f in db.get("favorites", []) if f.get("id") != item_id and f.get("imdb_id") != item_id]
        _write_db(db)

def is_favorite(item_id):
    return any(f.get("id") == item_id or f.get("imdb_id") == item_id for f in _read_db().get("favorites", []))

# --- Watched ---

def get_watched():
    return _read_db().get("watched", [])

def add_watched(item):
    with _db_lock:
        db = _read_db()
        watched = db.setdefault("watched", [])
        item_id = item.get("id") or item.get("imdb_id")
        watched = [w for w in watched if w.get("id") != item_id and w.get("imdb_id") != item_id]
        watched.insert(0, item)
        db["watched"] = watched
        _write_db(db)

def remove_watched(item_id):
    with _db_lock:
        db = _read_db()
        db["watched"] = [f for f in db.get("watched", []) if f.get("id") != item_id and f.get("imdb_id") != item_id]
        _write_db(db)

def is_watched(item_id):
    return any(f.get("id") == item_id or f.get("imdb_id") == item_id for f in _read_db().get("watched", []))

# --- History ---

def get_history():
    return _read_db().get("history", [])

def add_history(item):
    """Add item to top of history, moving it if already present. Capped at HISTORY_LIMIT."""
    with _db_lock:
        db = _read_db()
        history = db.setdefault("history", [])
        item_id = item.get("id") or item.get("imdb_id")
        # Remove existing entry if present (so it moves to the top)
        history = [h for h in history if h.get("id") != item_id and h.get("imdb_id") != item_id]
        history.insert(0, item)
        # Enforce cap
        db["history"] = history[:HISTORY_LIMIT]
        _write_db(db)

def clear_history():
    with _db_lock:
        db = _read_db()
        db["history"] = []
        _write_db(db)

def remove_history(item_id):
    with _db_lock:
        db = _read_db()
        db["history"] = [h for h in db.get("history", []) if h.get("id") != item_id and h.get("imdb_id") != item_id]
        _write_db(db)

# --- Continue Watching ---

def get_continue_watching():
    """Return list of in-progress and played items ordered by last_watched descending."""
    db = _read_db()
    cw_items = list(db.get("continue_watching", []))
    user_dismissed = set(db.get("user_dismissed_continue_watching", []))
    
    seen_ids = set()
    valid_cw = []
    for item in cw_items:
        i_id = item.get("id") or item.get("imdb_id")
        if i_id and str(i_id) not in seen_ids and str(i_id) not in user_dismissed:
            seen_ids.add(str(i_id))
            valid_cw.append(item)

    # Auto-heal: If any series in history has saved in-progress episode progress
    # and is not in valid_cw, include it so it is never lost
    history_items = db.get("history", [])
    now = time.time()
    healed_items = []
    import re
    for idx, h in enumerate(history_items):
        h_id = h.get("id") or h.get("imdb_id")
        if not h_id or str(h_id) in seen_ids or str(h_id) in user_dismissed:
            continue
        if h.get("type") in ["series", "anime", "tv"]:
            s_val = db.get("settings", {}).get(f"last_s_{h_id}", 1) or 1
            e_val = db.get("settings", {}).get(f"last_ep_{h_id}_{s_val}", None)
            if e_val is None:
                if db.get("settings", {}).get(f"working_stream_{h_id}_{s_val}_1"):
                    e_val = 1
            if e_val is not None:
                seen_ids.add(str(h_id))
                item_copy = dict(h)
                item_copy["season"] = s_val
                item_copy["episode"] = e_val
                item_copy.setdefault("progress", 0.0)
                item_copy.setdefault("position", 0.0)
                base_t = item_copy.get("title") or item_copy.get("name") or "Unknown"
                base_t = re.sub(r'\s*\(S\d+E\d+\)', '', base_t).strip()
                item_copy["title"] = base_t
                item_copy["stream_title"] = f"{base_t} (S{s_val:02d}E{e_val:02d})"
                lw = h.get("last_watched") or int(now - idx * 60)
                item_copy["last_watched"] = lw
                valid_cw.append(item_copy)
                healed_items.append(item_copy)

    if healed_items:
        with _db_lock:
            db_curr = _read_db()
            curr_cw = db_curr.setdefault("continue_watching", [])
            rem_list = set(db_curr.get("removed_continue_watching", []))
            for hi in healed_items:
                hi_id = str(hi.get("id") or hi.get("imdb_id"))
                rem_list.discard(hi_id)
                if not any(str(c.get("id") or c.get("imdb_id")) == hi_id for c in curr_cw):
                    curr_cw.insert(0, hi)
            db_curr["removed_continue_watching"] = list(rem_list)
            db_curr["continue_watching"] = curr_cw[:100]
            _write_db(db_curr)

    return sorted(valid_cw, key=lambda x: x.get("last_watched", 0), reverse=True)

def get_continue_watching_item(item_id):
    """Get continue watching item for a given item_id/imdb_id if available."""
    if not item_id:
        return None
    db = _read_db()
    cw_items = list(db.get("continue_watching", []))
    ids_to_match = [str(x) for x in item_id if x] if isinstance(item_id, (list, set, tuple)) else [str(item_id)]
            
    for item in cw_items:
        i_id = item.get("id") or item.get("imdb_id")
        i_aliases = [str(i_id)] if i_id else []
        if isinstance(item.get("alias_ids"), list):
            i_aliases.extend([str(x) for x in item["alias_ids"] if x])
        if any(a in ids_to_match for a in i_aliases):
            return item
            
    # Check history auto-heal on demand
    for h in db.get("history", []):
        h_id = h.get("id") or h.get("imdb_id")
        if h_id and any(str(h_id) == str(m) for m in ids_to_match) and h.get("type") in ["series", "anime", "tv"]:
            s_val = db.get("settings", {}).get(f"last_s_{h_id}", 1) or 1
            e_val = db.get("settings", {}).get(f"last_ep_{h_id}_{s_val}", None)
            if e_val is None and db.get("settings", {}).get(f"working_stream_{h_id}_{s_val}_1"):
                e_val = 1
            if e_val is not None:
                item_copy = dict(h)
                item_copy["season"] = s_val
                item_copy["episode"] = e_val
                item_copy.setdefault("progress", 0.0)
                item_copy.setdefault("position", 0.0)
                import re
                base_t = item_copy.get("title") or item_copy.get("name") or "Unknown"
                base_t = re.sub(r'\s*\(S\d+E\d+\)', '', base_t).strip()
                item_copy["title"] = base_t
                item_copy["stream_title"] = f"{base_t} (S{s_val:02d}E{e_val:02d})"
                return item_copy

    return None

def save_continue_watching(item):
    """Save or update an in-progress item in continue_watching."""
    if not item or not isinstance(item, dict):
        return
    item_id = item.get("id") or item.get("imdb_id")
    if not item_id:
        return
    with _db_lock:
        db = _read_db()
        cw = db.setdefault("continue_watching", [])
        
        ids_to_match = [str(item_id)]
        if isinstance(item.get("alias_ids"), list):
            ids_to_match.extend([str(x) for x in item["alias_ids"] if x])
        if item.get("id"):
            ids_to_match.append(str(item["id"]))
        if item.get("imdb_id"):
            ids_to_match.append(str(item["imdb_id"]))

        # Remove any matching IDs from removed_continue_watching and user_dismissed_continue_watching
        removed_set = set(db.get("removed_continue_watching", []))
        user_dismissed = set(db.get("user_dismissed_continue_watching", []))
        for mid in ids_to_match:
            removed_set.discard(mid)
            user_dismissed.discard(mid)
        db["removed_continue_watching"] = list(removed_set)
        db["user_dismissed_continue_watching"] = list(user_dismissed)

        existing = None
        new_cw = []
        for entry in cw:
            e_id = entry.get("id") or entry.get("imdb_id")
            e_aliases = [str(e_id)] if e_id else []
            if isinstance(entry.get("alias_ids"), list):
                e_aliases.extend([str(x) for x in entry["alias_ids"] if x])
            if any(a in ids_to_match for a in e_aliases):
                existing = entry
            else:
                new_cw.append(entry)

        existing_pos = float(existing.get("position") or 0.0) if existing else 0.0
        existing_prog = float(existing.get("progress") or 0.0) if existing else 0.0
        existing_dur = float(existing.get("duration") or 0.0) if existing else 0.0

        updated_item = dict(existing or {})
        updated_item.update(item)

        # Check if this is the same episode or a new episode
        same_ep = True
        if existing and (item.get("season") is not None or item.get("episode") is not None or existing.get("season") is not None or existing.get("episode") is not None):
            same_ep = (
                str(existing.get("season")) == str(item.get("season")) and 
                str(existing.get("episode")) == str(item.get("episode"))
            )

        new_pos = float(item.get("position") or 0.0)
        new_prog = float(item.get("progress") or 0.0)
        if same_ep:
            if new_pos <= 0.0 and existing_pos > 0.0:
                updated_item["position"] = existing_pos
            if new_prog <= 0.01 and existing_prog > 0.01:
                updated_item["progress"] = existing_prog
            if not updated_item.get("duration") and existing_dur > 0:
                updated_item["duration"] = existing_dur
        else:
            # Different episode: start fresh at the new episode's position and progress
            updated_item["position"] = new_pos
            updated_item["progress"] = new_prog
            if new_pos <= 0.0:
                updated_item.pop("duration", None)
            if existing and existing.get("episode") != item.get("episode"):
                if not item.get("stream_url"):
                    updated_item.pop("stream_url", None)
                if not item.get("stream_queue"):
                    updated_item.pop("stream_queue", None)
                if not item.get("selected_torrent"):
                    updated_item.pop("selected_torrent", None)

        if "provider" not in updated_item:
            st = updated_item.get("selected_torrent") or {}
            anames = st.get("addon_names") or ([st.get("addon_name")] if st.get("addon_name") else [])
            if anames and anames[0]:
                updated_item["provider"] = anames[0]

        if "last_watched" not in item:
            import time
            updated_item["last_watched"] = int(time.time())

        new_cw.insert(0, updated_item)
        db["continue_watching"] = new_cw[:100]
        _write_db(db)

def remove_continue_watching(item_id, blacklist=True, user_action=False):
    """Remove an item from continue_watching."""
    if not item_id:
        return
    with _db_lock:
        db = _read_db()
        cw = db.get("continue_watching", [])
        db["continue_watching"] = [
            e for e in cw 
            if e.get("id") != item_id and e.get("imdb_id") != item_id
        ]
        if blacklist:
            removed_set = set(db.get("removed_continue_watching", []))
            removed_set.add(str(item_id))
            db["removed_continue_watching"] = list(removed_set)[-200:]
        if user_action:
            user_dismissed = set(db.get("user_dismissed_continue_watching", []))
            user_dismissed.add(str(item_id))
            db["user_dismissed_continue_watching"] = list(user_dismissed)[-200:]
        _write_db(db)

# --- Downloads ---

def get_downloads():
    return _read_db().get("downloads", [])

def add_download(info_hash, name, magnet, file_index=None, item_id=None, media_type=None, season=None, episode=None):
    with _db_lock:
        db = _read_db()
        downloads = db.setdefault("downloads", [])
        
        for d in downloads:
            if d.get("info_hash") == info_hash:
                if file_index is not None:
                    d["file_index"] = file_index
                if name and name != "Fetching metadata...":
                    d["name"] = name
                if item_id is not None:
                    d["item_id"] = item_id
                if media_type is not None:
                    d["media_type"] = media_type
                if season is not None:
                    d["season"] = season
                if episode is not None:
                    d["episode"] = episode
                
                # Move the updated download to the top of the list
                downloads.remove(d)
                downloads.insert(0, d)
                
                _write_db(db)
                return

        downloads.insert(0, {
            "info_hash": info_hash,
            "name": name,
            "magnet": magnet,
            "paused": False,
            "file_index": file_index,
            "item_id": item_id,
            "media_type": media_type,
            "season": season,
            "episode": episode
        })
        _write_db(db)

def set_download_paused(info_hash, paused):
    with _db_lock:
        db = _read_db()
        for d in db.get("downloads", []):
            if d.get("info_hash") == info_hash:
                d["paused"] = paused
        _write_db(db)

def set_download_finished(info_hash, finished):
    with _db_lock:
        db = _read_db()
        for d in db.get("downloads", []):
            if d.get("info_hash") == info_hash:
                d["finished"] = finished
        _write_db(db)

def update_download_stats(info_hash, all_time_upload, all_time_download):
    """Persist cumulative upload/download bytes so ratio survives app restarts."""
    with _db_lock:
        db = _read_db()
        for d in db.get("downloads", []):
            if d.get("info_hash") == info_hash:
                # Only update if the new values are strictly larger (never go backwards)
                if all_time_upload > d.get("all_time_upload", 0):
                    d["all_time_upload"] = int(all_time_upload)
                if all_time_download > d.get("all_time_download", 0):
                    d["all_time_download"] = int(all_time_download)
                _write_db(db)
                return

def remove_download(info_hash):
    with _db_lock:
        db = _read_db()
        downloads = db.get("downloads", [])
        db["downloads"] = [d for d in downloads if d.get("info_hash") != info_hash]
        _write_db(db)

# --- Settings ---

def get_setting(key, default=None):
    return _read_db().get("settings", {}).get(key, default)

def set_setting(key, value):
    with _db_lock:
        db = _read_db()
        settings = db.setdefault("settings", {})
        settings[key] = value
        _write_db(db)

def is_adult_content_hidden():
    return get_setting("hide_adult_content", True)

def set_adult_content_hidden(enabled):
    set_setting("hide_adult_content", bool(enabled))

def _normalize_stream_for_storage(stream):
    if not stream or not isinstance(stream, dict):
        return None
    res = {}
    for k in ["url", "magnet", "hash", "infoHash", "sources", "file_index", "fileIdx", "quality", "q_val", "size", "size_gb", "stream_title", "title", "filename", "is_http", "is_external", "ytId", "externalUrl", "addon_names", "addon_name", "behaviorHints", "subtitles", "saved_at"]:
        if k in stream and stream[k] is not None:
            res[k] = stream[k]
    if "addon_name" in stream and "addon_names" not in res:
        res["addon_names"] = [stream["addon_name"]]
    elif res.get("addon_names") and not res.get("addon_name"):
        res["addon_name"] = res["addon_names"][0]
    return res

def is_stream_stale(stream_or_item, max_age_seconds=14400):
    """Check if an HTTP stream link is stale/expired.
    
    Direct HTTP streams from addons/scrapers typically expire after a few hours
    (e.g., debrid links, tokens). Torrent/magnet links do not expire by time.
    """
    if not stream_or_item or not isinstance(stream_or_item, dict):
        return False
    
    url = stream_or_item.get("stream_url") or stream_or_item.get("url") or ""
    is_http = bool(stream_or_item.get("is_http") or (isinstance(url, str) and url.startswith(("http://", "https://")) and not stream_or_item.get("hash") and not stream_or_item.get("infoHash")))
    if not is_http:
        return False
    
    saved_at = stream_or_item.get("saved_at")
    last_refreshed = stream_or_item.get("last_refreshed")
    last_watched = stream_or_item.get("last_watched")
    
    timestamp = last_refreshed or saved_at or last_watched
    if not timestamp:
        return True
    
    try:
        import time as _time
        age = _time.time() - float(timestamp)
        return age > max_age_seconds
    except (ValueError, TypeError):
        return True

def save_torrent_files(info_hash, files):
    """Cache the list of files inside a torrent so episode/file mapping is remembered."""
    if not info_hash or not files:
        return
    with _db_lock:
        db = _read_db()
        tf_cache = db.setdefault("torrent_files_cache", {})
        clean_files = [
            {
                "index": int(f.get("index", i)),
                "path": str(f.get("path") or f.get("name") or ""),
                "name": str(f.get("path") or f.get("name") or ""),
                "size": int(f.get("size", 0))
            }
            for i, f in enumerate(files)
        ]
        tf_cache[str(info_hash).lower()] = clean_files
        if len(tf_cache) > 200:
            oldest = list(tf_cache.keys())[:50]
            for k in oldest:
                del tf_cache[k]
        _write_db(db)

def get_torrent_files(info_hash):
    """Retrieve cached files for a torrent by info_hash."""
    if not info_hash:
        return []
    tf_cache = _read_db().get("torrent_files_cache", {})
    return tf_cache.get(str(info_hash).lower(), [])

def save_series_pack_torrent(item_id, stream_info, season=None):
    """Remember the active multi-episode / season pack torrent for a series or collection."""
    if not item_id or not stream_info:
        return
    norm = _normalize_stream_for_storage(stream_info)
    if not norm:
        return
    import time as _time
    if "saved_at" not in norm:
        norm["saved_at"] = _time.time()
    set_setting(f"series_pack_torrent_{item_id}", norm)
    if season is not None:
        set_setting(f"season_pack_torrent_{item_id}_{season}", norm)

def get_series_pack_torrent(item_id, season=None):
    """Get the remembered multi-episode / season pack torrent for a series or collection."""
    if not item_id:
        return None
    if season is not None:
        saved = get_setting(f"season_pack_torrent_{item_id}_{season}", None)
        if saved:
            return saved
    return get_setting(f"series_pack_torrent_{item_id}", None)

def save_working_stream(item_id, season=None, episode=None, stream_info=None):
    """Save the working stream/torrent for a movie or series episode."""
    if not item_id or not stream_info:
        return
    norm = _normalize_stream_for_storage(stream_info)
    if not norm:
        return
    import time as _time
    if "saved_at" not in norm:
        norm["saved_at"] = _time.time()
    s_key = f"{season}" if season is not None else ""
    e_key = f"{episode}" if episode is not None else ""
    key = f"working_stream_{item_id}_{s_key}_{e_key}"
    set_setting(key, norm)

def get_working_stream(item_id, season=None, episode=None):
    """Get the remembered working stream/torrent for a movie or series episode."""
    if not item_id:
        return None

    def _is_stream_valid_for_ep(st):
        if season is None or episode is None or not st:
            return True
        try:
            from . import api
            return api.match_stream_to_episode(st, season, episode) != api.MATCH_MISMATCH
        except Exception:
            return True

    s_key = f"{season}" if season is not None else ""
    e_key = f"{episode}" if episode is not None else ""
    key = f"working_stream_{item_id}_{s_key}_{e_key}"
    saved = get_setting(key, None)
    if saved and _is_stream_valid_for_ep(saved):
        return saved
    if s_key or e_key:
        general_saved = get_setting(f"working_stream_{item_id}__", None)
        if general_saved and _is_stream_valid_for_ep(general_saved):
            return general_saved

    # Check remembered season/series pack torrent
    pack_torrent = get_series_pack_torrent(item_id, season=season)
    if pack_torrent and not pack_torrent.get("is_http"):
        info_hash = pack_torrent.get("hash")
        cached_files = get_torrent_files(info_hash) if info_hash else []
        if cached_files and season is not None and episode is not None:
            from . import api
            ep_idx = api.find_matching_file_index(cached_files, season=season, episode=episode, strict=True)
            if ep_idx is not None:
                cand = dict(pack_torrent)
                cand["file_index"] = ep_idx
                cand["fileIdx"] = ep_idx
                if ep_idx < len(cached_files):
                    cand["filename"] = cached_files[ep_idx]["path"]
                return cand
        elif _is_stream_valid_for_ep(pack_torrent):
            cand = dict(pack_torrent)
            cand["file_index"] = None
            cand["fileIdx"] = None
            return cand

    cw = get_continue_watching_item(item_id)
    if cw:
        cand = None
        if cw.get("selected_torrent"):
            cand = _normalize_stream_for_storage(cw["selected_torrent"])
        elif cw.get("magnet") or cw.get("stream_url") or cw.get("hash"):
            cand = {
                "url": cw.get("stream_url") or cw.get("magnet"),
                "magnet": cw.get("magnet"),
                "hash": cw.get("hash"),
                "file_index": cw.get("file_index"),
                "stream_title": cw.get("stream_title") or cw.get("title"),
                "is_http": bool(cw.get("stream_url") and not str(cw.get("stream_url")).startswith("magnet:")),
            }
        if cand and not cand.get("is_http"):
            info_hash = cand.get("hash")
            cached_files = get_torrent_files(info_hash) if info_hash else []
            if cached_files and season is not None and episode is not None:
                from . import api
                ep_idx = api.find_matching_file_index(cached_files, season=season, episode=episode, strict=True)
                if ep_idx is not None:
                    cand_copy = dict(cand)
                    cand_copy["file_index"] = ep_idx
                    cand_copy["fileIdx"] = ep_idx
                    if ep_idx < len(cached_files):
                        cand_copy["filename"] = cached_files[ep_idx]["path"]
                    return cand_copy
            elif _is_stream_valid_for_ep(cand):
                cand_copy = dict(cand)
                cand_copy["file_index"] = None
                cand_copy["fileIdx"] = None
                return cand_copy
        elif cand and _is_stream_valid_for_ep(cand):
            if season is not None and cw.get("season") is not None and str(cw.get("season")) != str(season):
                return None
            if episode is not None and cw.get("episode") is not None and str(cw.get("episode")) != str(episode):
                return None
            return cand
    return None


# --- Progress ---

_progress_buffer = {}
_progress_last_flush = 0
_PROGRESS_FLUSH_INTERVAL = 10  # seconds

def save_progress(key, position):
    """Buffer progress in memory, flush to disk at most every 10 seconds."""
    global _progress_last_flush
    with _db_lock:
        _progress_buffer[key] = position
        now = time.time()
        should_flush = now - _progress_last_flush >= _PROGRESS_FLUSH_INTERVAL
    if should_flush:
        flush_progress()

def flush_progress():
    """Write all buffered progress to disk immediately."""
    global _progress_last_flush
    with _db_lock:
        if not _progress_buffer:
            return
        db = _read_db()
        progress = db.setdefault("progress", {})
        progress.update(_progress_buffer)
        if _write_db(db):
            _progress_buffer.clear()
            _progress_last_flush = time.time()

def get_progress(key):
    # Check in-memory buffer first (most recent), then disk
    with _db_lock:
        if key in _progress_buffer:
            return _progress_buffer[key]
        return _read_db().get("progress", {}).get(key, 0)


# --- Addons ---

def get_addons():
    return _read_db().get("addons", [])

def add_addon(addon):
    with _db_lock:
        db = _read_db()
        addons = db.setdefault("addons", [])
        # Remove existing addon with same ID or manifest_url
        addons = [a for a in addons if a.get("id") != addon.get("id") and a.get("manifest_url") != addon.get("manifest_url")]
        addons.append(addon)
        db["addons"] = addons
        # If this default addon was previously removed, unmark it
        settings = db.setdefault("settings", {})
        removed_defaults = settings.get("removed_default_addons", [])
        if addon.get("id") in removed_defaults:
            settings["removed_default_addons"] = [x for x in removed_defaults if x != addon.get("id")]
        _write_db(db)
    clear_all_cached_streams()
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

def update_addon_catalogs(manifest_url, catalogs):
    """Update an addon's catalogs list if it was empty or missing."""
    if not manifest_url or not catalogs:
        return False
    with _db_lock:
        db = _read_db()
        updated = False
        for a in db.get("addons", []):
            if a.get("manifest_url") == manifest_url:
                if not a.get("catalogs"):
                    a["catalogs"] = list(catalogs)
                    updated = True
                break
        if updated:
            _write_db(db)
            return True
    return False

def remove_addon(addon_id=None, manifest_url=None):
    if not addon_id and not manifest_url:
        return
    with _db_lock:
        db = _read_db()
        addons = db.get("addons", [])
        to_remove = []
        for a in addons:
            match_id = addon_id and str(a.get("id")) == str(addon_id)
            match_url = manifest_url and a.get("manifest_url") == manifest_url
            if match_id or match_url:
                to_remove.append(a)
        
        removed_ids = {a.get("id") for a in to_remove if a.get("id")}
        removed_urls = {a.get("manifest_url") for a in to_remove if a.get("manifest_url")}
        if addon_id:
            removed_ids.add(addon_id)
        if manifest_url:
            removed_urls.add(manifest_url)
            
        db["addons"] = [a for a in addons if a.get("id") not in removed_ids and a.get("manifest_url") not in removed_urls]
        
        # Track removed default addons so they aren't resurrected on startup
        settings = db.setdefault("settings", {})
        removed_defaults = set(settings.get("removed_default_addons", []))
        for default_addon in DEFAULT_ADDONS:
            if default_addon.get("id") in removed_ids:
                removed_defaults.add(default_addon.get("id"))
        settings["removed_default_addons"] = list(removed_defaults)
        
        _write_db(db)
    
    for m_url in removed_urls:
        clear_cached_catalog_for_url(m_url)
    clear_all_cached_streams()
    
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

def clear_cached_catalog_for_url(manifest_url):
    if not manifest_url:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM catalog_cache WHERE cache_key LIKE ?", (f"%{manifest_url}%",))
            conn.commit()
    except Exception as e:
        print(f"Error clearing catalog cache for {manifest_url}: {e}")

def clear_all_cached_streams():
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stream_cache")
            conn.commit()
    except Exception as e:
        print(f"Error clearing all cached streams: {e}")

def set_addon_enabled(addon_id, enabled):
    with _db_lock:
        db = _read_db()
        for a in db.get("addons", []):
            if a.get("id") == addon_id:
                a["enabled"] = enabled
        _write_db(db)
    clear_all_cached_streams()
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

# --- SQLite Metadata & Stream Cache ---

def _get_cache_db():
    """Return persistent SQLite connection, initializing schema once."""
    global _cache_conn, _cache_db_initialized
    if _cache_conn is not None and _cache_db_initialized:
        return _cache_conn
    _ensure_db()
    db_path = CONFIG_DIR / "cache.db"
    _cache_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _cache_conn.execute("PRAGMA journal_mode=WAL;")
    _cache_conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_cache (
            id TEXT PRIMARY KEY,
            media_type TEXT,
            data TEXT,
            updated_at REAL
        )
    """)
    _cache_conn.execute("""
        CREATE TABLE IF NOT EXISTS stream_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT,
            updated_at REAL
        )
    """)
    _cache_conn.execute("""
        CREATE TABLE IF NOT EXISTS trailer_stream_cache (
            youtube_id TEXT PRIMARY KEY,
            stream_url TEXT,
            user_agent TEXT DEFAULT '',
            updated_at REAL
        )
    """)
    # Migration: add user_agent column if missing
    try:
        _cache_conn.execute("ALTER TABLE trailer_stream_cache ADD COLUMN user_agent TEXT DEFAULT ''")
    except Exception:
        pass
    _cache_conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT,
            updated_at REAL
        )
    """)
    _cache_conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT,
            updated_at REAL
        )
    """)
    # Migration: Invalidate catalog_cache entries where series catalogs were corrupted with movies
    try:
        _cache_conn.execute("CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, val TEXT)")
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'series_catalog_fix_v1'")
        if not cur.fetchone():
            _cache_conn.execute("DELETE FROM catalog_cache WHERE cache_key LIKE '%:series%' OR cache_key LIKE '%:series:%'")
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('series_catalog_fix_v1', '1')")
    except Exception:
        pass
    _cache_conn.commit()
    _cache_db_initialized = True
    return _cache_conn

def get_cached_metadata(item_id, media_type=None):
    if not item_id:
        return None
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM metadata_cache WHERE id = ?", (str(item_id),))
            row = cursor.fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                if str(item_id).startswith("ctmdb."):
                    if not data.get("videos") or len(data.get("videos", [])) == 0 or str(data.get("title", "")).lower() == "media item":
                        return None
                return data
    except Exception as e:
        print(f"Error reading metadata cache: {e}")
    return None

def save_cached_metadata(item_id, media_type, details):
    if not item_id or not details:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
                (str(item_id), str(media_type or "movie"), json.dumps(details), time.time())
            )
            conn.commit()
    except Exception as e:
        print(f"Error saving metadata cache: {e}")

def get_cached_streams(cache_key, max_age_hours=24):
    if not cache_key:
        return None
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data, updated_at FROM stream_cache WHERE cache_key = ?", (str(cache_key),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[1]
                if (time.time() - updated_at) / 3600 < max_age_hours:
                    return json.loads(row[0])
    except Exception as e:
        print(f"Error reading stream cache: {e}")
    return None

def save_cached_streams(cache_key, streams):
    if not cache_key or streams is None:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO stream_cache (cache_key, data, updated_at) VALUES (?, ?, ?)",
                (str(cache_key), json.dumps(streams), time.time())
            )
            conn.commit()
    except Exception as e:
        print(f"Error saving stream cache: {e}")

def delete_cached_metadata(item_id):
    if not item_id:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM metadata_cache WHERE id = ?", (str(item_id),))
            conn.commit()
    except Exception as e:
        print(f"Error deleting cached metadata: {e}")

def delete_cached_streams(cache_key):
    if not cache_key:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stream_cache WHERE cache_key = ?", (str(cache_key),))
            conn.commit()
    except Exception as e:
        print(f"Error deleting cached streams: {e}")

def get_cached_trailer_stream(youtube_id, max_age_hours=24):
    """Returns (stream_url, user_agent) tuple or None."""
    if not youtube_id:
        return None
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT stream_url, user_agent, updated_at FROM trailer_stream_cache WHERE youtube_id = ?", (str(youtube_id),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[2]
                ua = row[1] or ""
                # Invalidate stale entries without user_agent (pre-migration cache)
                if not ua and "googlevideo.com" in row[0].lower():
                    return None
                if (time.time() - updated_at) / 3600 < max_age_hours:
                    return (row[0], ua)
    except Exception as e:
        print(f"Error reading trailer stream cache: {e}")
    return None

def save_cached_trailer_stream(youtube_id, stream_url, user_agent=""):
    if not youtube_id or not stream_url:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO trailer_stream_cache (youtube_id, stream_url, user_agent, updated_at) VALUES (?, ?, ?, ?)",
                (str(youtube_id), str(stream_url), str(user_agent or ""), time.time())
            )
            conn.commit()
    except Exception as e:
        print(f"Error saving trailer stream cache: {e}")

def get_cached_subtitles(cache_key, max_age_hours=24):
    if not cache_key:
        return None
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data, updated_at FROM subtitle_cache WHERE cache_key = ?", (str(cache_key),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[1]
                if (time.time() - updated_at) / 3600 < max_age_hours:
                    return json.loads(row[0])
    except Exception as e:
        print(f"Error reading subtitle cache: {e}")
    return None

def save_cached_subtitles(cache_key, subtitles):
    if not cache_key or subtitles is None:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO subtitle_cache (cache_key, data, updated_at) VALUES (?, ?, ?)",
                (str(cache_key), json.dumps(subtitles), time.time())
            )
            conn.commit()
    except Exception as e:
        print(f"Error saving subtitle cache: {e}")

def delete_cached_subtitles(cache_key):
    if not cache_key:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subtitle_cache WHERE cache_key = ?", (str(cache_key),))
            conn.commit()
    except Exception as e:
        print(f"Error deleting cached subtitles: {e}")

_MEM_CATALOG_CACHE = {}
_MEM_CATALOG_LOCK = threading.Lock()

def get_cached_catalog(cache_key, max_age_hours=24):
    if not cache_key:
        return None
    now = time.time()
    # 1. Fast in-memory check
    with _MEM_CATALOG_LOCK:
        if cache_key in _MEM_CATALOG_CACHE:
            data, ts = _MEM_CATALOG_CACHE[cache_key]
            if (now - ts) / 3600 < max_age_hours:
                return data
            else:
                del _MEM_CATALOG_CACHE[cache_key]

    # 2. SQLite cache check
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data, updated_at FROM catalog_cache WHERE cache_key = ?", (str(cache_key),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[1]
                if (now - updated_at) / 3600 < max_age_hours:
                    data = json.loads(row[0])
                    with _MEM_CATALOG_LOCK:
                        if len(_MEM_CATALOG_CACHE) > 500:
                            _MEM_CATALOG_CACHE.clear()
                        _MEM_CATALOG_CACHE[cache_key] = (data, updated_at)
                    return data
    except Exception as e:
        print(f"Error reading catalog cache: {e}")
    return None

def save_cached_catalog(cache_key, items):
    if not cache_key or items is None:
        return
    now = time.time()
    with _MEM_CATALOG_LOCK:
        if len(_MEM_CATALOG_CACHE) > 500:
            _MEM_CATALOG_CACHE.clear()
        _MEM_CATALOG_CACHE[cache_key] = (items, now)

    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO catalog_cache (cache_key, data, updated_at) VALUES (?, ?, ?)",
                (str(cache_key), json.dumps(items), now)
            )
            conn.commit()
    except Exception as e:
        print(f"Error saving catalog cache: {e}")



