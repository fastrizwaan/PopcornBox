import json
import os
import sys
import threading
import sqlite3
import time
import logging
import re
import atexit
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
    }
]

def _ensure_db():
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True)
    if not DB_FILE.exists():
        # Atomic initial creation: a crash mid-write must not leave a truncated
        # data.json that would latch the corruption path on next start.
        temp_file = DB_FILE.with_name(f"data.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump({"favorites": [], "watched": [], "history": [], "continue_watching": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}, f, indent=4)
            temp_file.replace(DB_FILE)
        except Exception:
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            raise

def _sanitize_for_json(obj, depth=0):
    """Recursively clean objects for json serialization, stripping callables and non-serializables."""
    if depth > 30:
        return None
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if callable(v) or k == "_on_hydrated":
                continue
            clean_v = _sanitize_for_json(v, depth + 1)
            if clean_v is not None or v is None:
                clean[str(k)] = clean_v
        return clean
    elif isinstance(obj, (list, tuple, set)):
        clean_list = []
        for item in obj:
            if callable(item):
                continue
            clean_item = _sanitize_for_json(item, depth + 1)
            clean_list.append(clean_item)
        return clean_list
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    elif hasattr(obj, "__str__"):
        return str(obj)
    return None

def _extract_all_item_ids(item_or_id):
    """Extract all potential string identifiers (id, imdb_id, item_id, tmdb_id, aliases, base IDs)."""
    ids = set()
    if isinstance(item_or_id, dict):
        for k in ("id", "imdb_id", "item_id", "tmdb_id"):
            val = item_or_id.get(k)
            if val is not None and str(val).strip():
                ids.add(str(val).strip())
        aliases = item_or_id.get("alias_ids")
        if isinstance(aliases, (list, tuple, set)):
            ids.update(str(x).strip() for x in aliases if x is not None and str(x).strip())
    elif item_or_id is not None:
        s_val = str(item_or_id).strip()
        if s_val:
            ids.add(s_val)

    # Strip prefixes / episode parts (e.g. 'tt12345:1:2' -> 'tt12345', 'cinemeta:tt12345' -> 'tt12345')
    base_ids = set()
    for i in list(ids):
        if ":" in i:
            parts = i.split(":")
            for part in parts:
                if part.startswith("tt") or part.isdigit():
                    base_ids.add(part)
            base_ids.add(parts[0])
    ids.update(base_ids)
    return ids

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
                data["addons"] = list(DEFAULT_ADDONS)
            else:
                # Remove unbundled default addons (Anime Kitsu and IPTV Org)
                unbundled_ids = {"anime-kitsu", "local.iptv-org"}
                initial_count = len(data["addons"])
                data["addons"] = [
                    a for a in data["addons"]
                    if a.get("id") not in unbundled_ids
                    and "anime-kitsu.strem.fun" not in str(a.get("manifest_url", "")).lower()
                    and "iptv-org.github.io" not in str(a.get("manifest_url", "")).lower()
                ]
                migrated = (len(data["addons"]) != initial_count)
                if migrated:
                    removed_defs = set(data.get("settings", {}).get("removed_default_addons", []))
                    removed_defs.update(unbundled_ids)
                    data.setdefault("settings", {})["removed_default_addons"] = list(removed_defs)
                    try:
                        with _cache_db_lock:
                            conn = _get_cache_db()
                            c = conn.cursor()
                            c.execute("DELETE FROM addon_catalog_cache WHERE manifest_url LIKE '%anime-kitsu%' OR manifest_url LIKE '%iptv-org%'")
                            c.execute("DELETE FROM catalog_cache WHERE catalog_url LIKE '%anime-kitsu%' OR catalog_url LIKE '%iptv-org%'")
                            conn.commit()
                    except Exception:
                        pass

                # Ensure default/bundled addons are present in the user database unless intentionally removed
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
                else:
                    data["addons"] = unique_addons

                # Ensure all addons have valid types, resources, catalogs conforming to Stremio spec
                for a in data.get("addons", []):
                    if not isinstance(a.get("catalogs"), list):
                        a["catalogs"] = []
                        migrated = True
                    if not isinstance(a.get("resources"), list) or not a["resources"]:
                        a["resources"] = ["catalog", "meta"] if a["catalogs"] else ["stream"]
                        migrated = True
                    raw_types = a.get("types")
                    if not isinstance(raw_types, list) or not raw_types:
                        found_types = set()
                        for cat in a.get("catalogs", []):
                            if isinstance(cat, dict) and cat.get("type"):
                                found_types.add(str(cat["type"]))
                        if found_types:
                            a["types"] = sorted(list(found_types))
                        elif any((isinstance(r, str) and r == "stream") or (isinstance(r, dict) and r.get("name") == "stream") for r in a.get("resources", [])):
                            a["types"] = ["movie", "series", "anime", "other"]
                        else:
                            a["types"] = ["movie", "series"]
                        migrated = True

                if migrated:
                    _write_db(data)
            _json_cache = data
            _json_cache_valid = True
            return data
        except json.JSONDecodeError as e:
            logging.error(f"Database file is corrupted: {e}. Attempting recovery from backup or defaults.")
            try:
                bak = DB_FILE.with_suffix(".corrupt.bak")
                if DB_FILE.exists():
                    DB_FILE.rename(bak)
            except Exception:
                pass
            _ensure_db()
            try:
                with open(DB_FILE, "r") as f:
                    _json_cache = json.load(f)
                    _json_cache_valid = True
                    return _json_cache
            except Exception:
                pass
            return {"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}
        except Exception as e:
            # Transient I/O problems (permissions, disk full, FUSE hiccups) must
            # not permanently disable writes for the whole session; only real
            # corruption latches _db_corrupted above.
            logging.warning(f"Transient database read failure: {e}")
            return {"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}

def reset_json_cache():
    global _json_cache, _json_cache_valid
    # Synchronize with readers/writers: _read_db checks _json_cache_valid and
    # returns _json_cache under _db_lock, so invalidation must hold it too.
    with _db_lock:
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
        unique_suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
        temp_file = DB_FILE.with_name(f"data{unique_suffix}")
        try:
            clean_data = _sanitize_for_json(data)
            with open(temp_file, "w") as f:
                json.dump(clean_data, f, indent=4, default=str)
                # Flush to disk before the rename: without fsync a crash can
                # persist the rename while the contents are still unwritten,
                # leaving a truncated data.json behind.
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(DB_FILE)
            # Update in-memory cache with the data we just wrote
            _json_cache = clean_data
            _json_cache_valid = True
            return True
        except Exception as e:
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            _json_cache_valid = False
            logging.error(f"Error writing database: {e}")
            return False

# --- Favorites ---

def get_favorites():
    return _read_db().get("favorites", [])

def add_favorite(item, sync_to_remote=True):
    if not item or not isinstance(item, dict):
        return
    clean_item = {k: v for k, v in item.items() if not callable(v) and k != "_on_hydrated"}
    target_ids = _extract_all_item_ids(clean_item)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        favs = db.setdefault("favorites", [])
        if not any(_extract_all_item_ids(f).intersection(target_ids) for f in favs):
            favs.insert(0, clean_item)
            settings = db.setdefault("settings", {})
            removed_favs = set(settings.get("removed_favorite_ids", []))
            for tid in target_ids:
                removed_favs.discard(tid)
            settings["removed_favorite_ids"] = list(removed_favs)
            _write_db(db)
    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_sync_enabled():
                stremio_sync.sync_in_background()
        except Exception as e:
            logging.warning(f"Failed to trigger remote sync: {e}")

def remove_favorite(item_or_id, sync_to_remote=True):
    target_ids = _extract_all_item_ids(item_or_id)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        db["favorites"] = [
            f for f in db.get("favorites", [])
            if not _extract_all_item_ids(f).intersection(target_ids)
        ]
        settings = db.setdefault("settings", {})
        # Keep tombstones in removal order (oldest first) so trimming to the
        # cap evicts the oldest, not a random entry.
        removed_favs = [str(v) for v in settings.get("removed_favorite_ids", []) if v]
        for tid in target_ids:
            if tid in removed_favs:
                removed_favs.remove(tid)
            removed_favs.append(tid)
        settings["removed_favorite_ids"] = removed_favs[-200:]
        _write_db(db)
    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_sync_enabled():
                stremio_sync.sync_in_background()
        except Exception as e:
            logging.warning(f"Failed to trigger remote sync: {e}")

def is_favorite(item_or_id):
    target_ids = _extract_all_item_ids(item_or_id)
    if not target_ids:
        return False
    return any(
        bool(_extract_all_item_ids(f).intersection(target_ids))
        for f in _read_db().get("favorites", [])
    )

# --- Watched ---

def get_watched():
    return _read_db().get("watched", [])

def add_watched(item):
    if not item or not isinstance(item, dict):
        return
    clean_item = {k: v for k, v in item.items() if not callable(v) and k != "_on_hydrated"}
    target_ids = _extract_all_item_ids(clean_item)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        watched = db.setdefault("watched", [])
        watched = [w for w in watched if not _extract_all_item_ids(w).intersection(target_ids)]
        watched.insert(0, clean_item)
        db["watched"] = watched
        _write_db(db)

def remove_watched(item_or_id):
    target_ids = _extract_all_item_ids(item_or_id)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        db["watched"] = [
            w for w in db.get("watched", [])
            if not _extract_all_item_ids(w).intersection(target_ids)
        ]
        _write_db(db)

def is_watched(item_or_id):
    target_ids = _extract_all_item_ids(item_or_id)
    if not target_ids:
        return False
    return any(
        bool(_extract_all_item_ids(w).intersection(target_ids))
        for w in _read_db().get("watched", [])
    )

# --- History ---

def get_history():
    return _read_db().get("history", [])

def add_history(item, sync_to_remote=True):
    """Add item to top of history, moving it if already present. Capped at HISTORY_LIMIT."""
    if not item or not isinstance(item, dict):
        return
    clean_item = {k: v for k, v in item.items() if not callable(v) and k != "_on_hydrated"}
    target_ids = _extract_all_item_ids(clean_item)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        history = db.setdefault("history", [])
        if "last_watched" not in clean_item:
            import time as _t
            clean_item["last_watched"] = int(_t.time())
        # Remove existing entry if present (so it moves to the top)
        history = [h for h in history if not _extract_all_item_ids(h).intersection(target_ids)]
        history.insert(0, clean_item)
        settings = db.setdefault("settings", {})
        removed_hist = set(settings.get("removed_history_ids", []))
        for tid in target_ids:
            removed_hist.discard(tid)
        settings["removed_history_ids"] = list(removed_hist)
        # Enforce cap
        db["history"] = history[:HISTORY_LIMIT]
        _write_db(db)
    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_sync_enabled():
                stremio_sync.scrobble_playback(clean_item)
        except Exception as e:
            logging.warning(f"Failed to scrobble playback: {e}")

def clear_history():
    with _db_lock:
        db = _read_db()
        db["history"] = []
        _write_db(db)

def add_history_batch(items, sync_to_remote=True):
    """Add multiple items to history efficiently in a single database transaction."""
    if not items:
        return
    clean_items = []
    for it in items:
        if isinstance(it, dict):
            c_it = {k: v for k, v in it.items() if not callable(v) and k != "_on_hydrated"}
            clean_items.append(c_it)
    if not clean_items:
        return
    with _db_lock:
        db = _read_db()
        history = list(db.get("history", []))
        item_ids_to_add = set()
        for it in clean_items:
            item_ids_to_add.update(_extract_all_item_ids(it))
        # Filter out existing occurrences
        history = [h for h in history if not _extract_all_item_ids(h).intersection(item_ids_to_add)]
        # Insert new items at top, sorted by last_watched descending
        sorted_new = sorted(clean_items, key=lambda x: int(x.get("last_watched", 0)), reverse=True)
        combined = sorted_new + history
        db["history"] = combined[:HISTORY_LIMIT]
        _write_db(db)

def remove_history(item_or_id, sync_to_remote=True):
    target_ids = _extract_all_item_ids(item_or_id)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        db["history"] = [
            h for h in db.get("history", [])
            if not _extract_all_item_ids(h).intersection(target_ids)
        ]
        settings = db.setdefault("settings", {})
        removed_hist = [str(v) for v in settings.get("removed_history_ids", []) if v]
        for tid in target_ids:
            if tid in removed_hist:
                removed_hist.remove(tid)
            removed_hist.append(tid)
        settings["removed_history_ids"] = removed_hist[-200:]
        _write_db(db)

# --- Continue Watching ---

def _continue_watching_item_ids(item):
    """Return the string IDs that identify a Continue Watching item."""
    if not isinstance(item, dict):
        return set()
    ids = {str(value) for value in (item.get("id"), item.get("imdb_id")) if value}
    aliases = item.get("alias_ids")
    if isinstance(aliases, (list, tuple, set)):
        ids.update(str(value) for value in aliases if value)
    return ids


def _user_dismissed_continue_watching_ids(db=None):
    """Read dismissals from canonical DB fields and older settings locations."""
    db = db if isinstance(db, dict) else _read_db()
    settings = db.get("settings", {})
    ids = set()
    root_values = db.get("user_dismissed_continue_watching", [])
    settings_values = settings.get("user_dismissed_continue_watching", [])
    for values in (root_values, settings_values):
        if not isinstance(values, (list, tuple, set)):
            continue
        for value in values:
            if value:
                ids.add(str(value))
    return ids


def get_continue_watching_dismissals():
    """Return IDs suppressed from Continue Watching, including legacy tombstones."""
    db = _read_db()
    ids = _user_dismissed_continue_watching_ids(db)
    ids.update(str(value) for value in db.get("removed_continue_watching", []) if value)
    ids.update(str(value) for value in db.get("settings", {}).get("removed_continue_watching", []) if value)
    return ids


def get_continue_watching():
    """Return list of in-progress and played items ordered by last_watched descending."""
    with _db_lock:
        db = _read_db()
        cw_items = list(db.get("continue_watching", []))
        user_dismissed = _user_dismissed_continue_watching_ids(db)
        history_items = list(db.get("history", []))
        settings_snapshot = dict(db.get("settings", {}))
    
    seen_ids = set()
    valid_cw = []
    for item in cw_items:
        item_ids = _continue_watching_item_ids(item)
        i_id = item.get("id") or item.get("imdb_id")
        if i_id and not item_ids.intersection(seen_ids) and not item_ids.intersection(user_dismissed):
            seen_ids.update(item_ids)
            valid_cw.append(item)

    # Auto-heal: If any series in history has saved in-progress episode progress
    # and is not in valid_cw, include it so it is never lost
    now = time.time()
    healed_items = []
    import re
    for idx, h in enumerate(history_items):
        h_id = h.get("id") or h.get("imdb_id")
        history_ids = _continue_watching_item_ids(h)
        if not h_id or history_ids.intersection(seen_ids) or history_ids.intersection(user_dismissed):
            continue
        if h.get("type") in ["series", "anime", "tv"]:
            s_val = settings_snapshot.get(f"last_s_{h_id}", 1) or 1
            e_val = settings_snapshot.get(f"last_ep_{h_id}_{s_val}", None)
            # Legacy last-season/episode settings were also written by merely
            # changing the details-page dropdown. Treat them as resume history
            # only when playback saved a working stream for that exact episode.
            has_playback_evidence = bool(
                e_val is not None
                and settings_snapshot.get(f"working_stream_{h_id}_{s_val}_{e_val}")
            )
            if has_playback_evidence:
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
            rem_list = db_curr.get("removed_continue_watching", [])
            # Re-validate dismissals against the fresh snapshot: the user may
            # have dismissed a healed series between the first read and now.
            curr_dismissed = _user_dismissed_continue_watching_ids(db_curr)
            rem_list_ids = set(str(v) for v in rem_list if v)
            for hi in healed_items:
                hi_id = str(hi.get("id") or hi.get("imdb_id"))
                hi_ids = _continue_watching_item_ids(hi)
                if hi_ids.intersection(curr_dismissed):
                    continue
                rem_list_ids.discard(hi_id)
                if not any(str(c.get("id") or c.get("imdb_id")) == hi_id for c in curr_cw):
                    curr_cw.insert(0, hi)
            db_curr["removed_continue_watching"] = [str(v) for v in rem_list if str(v) in rem_list_ids]
            db_curr["continue_watching"] = curr_cw[:100]
            _write_db(db_curr)

    return sorted(valid_cw, key=lambda x: x.get("last_watched", 0), reverse=True)

def get_continue_watching_item(item_id):
    """Get continue watching item for a given item_id/imdb_id if available."""
    if not item_id:
        return None
    db = _read_db()
    cw_items = list(db.get("continue_watching", []))
    user_dismissed = _user_dismissed_continue_watching_ids(db)
    ids_to_match = [str(x) for x in item_id if x] if isinstance(item_id, (list, set, tuple)) else [str(item_id)]
    if set(ids_to_match).intersection(user_dismissed):
        return None
            
    for item in cw_items:
        if _continue_watching_item_ids(item).intersection(user_dismissed):
            continue
        i_id = item.get("id") or item.get("imdb_id")
        i_aliases = [str(i_id)] if i_id else []
        if isinstance(item.get("alias_ids"), list):
            i_aliases.extend([str(x) for x in item["alias_ids"] if x])
        if any(a in ids_to_match for a in i_aliases):
            return item
            
    # Check history auto-heal on demand
    for h in db.get("history", []):
        if _continue_watching_item_ids(h).intersection(user_dismissed):
            continue
        h_id = h.get("id") or h.get("imdb_id")
        if h_id and any(str(h_id) == str(m) for m in ids_to_match) and h.get("type") in ["series", "anime", "tv"]:
            s_val = db.get("settings", {}).get(f"last_s_{h_id}", 1) or 1
            e_val = db.get("settings", {}).get(f"last_ep_{h_id}_{s_val}", None)
            settings = db.get("settings", {})
            has_playback_evidence = bool(
                e_val is not None
                and settings.get(f"working_stream_{h_id}_{s_val}_{e_val}")
            )
            if has_playback_evidence:
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

def save_continue_watching(item, sync_to_remote=True):
    """Save or update an in-progress item in continue_watching."""
    if not item or not isinstance(item, dict):
        return
    item_id = item.get("id") or item.get("imdb_id")
    if not item_id:
        return
    updated_item = None
    with _db_lock:
        db = _read_db()
        cw = db.setdefault("continue_watching", [])
        
        ids_to_match = _continue_watching_item_ids(item)

        # A remote sync can overlap a local card dismissal. Do not let an
        # already-running pull recreate the dismissed item; a local playback
        # save (sync_to_remote=True) is the explicit way to resume it.
        if not sync_to_remote and set(ids_to_match).intersection(_user_dismissed_continue_watching_ids(db)):
            return

        # Remove any matching IDs from removed_continue_watching and user_dismissed_continue_watching
        removed_set = set(str(value) for value in db.get("removed_continue_watching", []) if value)
        user_dismissed = _user_dismissed_continue_watching_ids(db)
        for mid in ids_to_match:
            removed_set.discard(mid)
            user_dismissed.discard(mid)
        db["removed_continue_watching"] = list(removed_set)
        db["user_dismissed_continue_watching"] = list(user_dismissed)
        settings = db.get("settings", {})
        legacy_removed = settings.get("removed_continue_watching", [])
        if isinstance(legacy_removed, (list, tuple, set)):
            settings["removed_continue_watching"] = [str(value) for value in legacy_removed if str(value) not in ids_to_match]
        legacy_dismissed = settings.get("user_dismissed_continue_watching", [])
        if isinstance(legacy_dismissed, (list, tuple, set)):
            settings["user_dismissed_continue_watching"] = [str(value) for value in legacy_dismissed if str(value) not in ids_to_match]

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

    if updated_item and sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            stremio_sync.scrobble_playback(updated_item)
        except Exception:
            pass

def remove_continue_watching(item_id, blacklist=True, user_action=False, sync_to_remote=True):
    """Remove an item from continue_watching."""
    if not item_id:
        return
    target_ids = _extract_all_item_ids(item_id)
    if not target_ids:
        return
    with _db_lock:
        db = _read_db()
        cw = db.get("continue_watching", [])
        matching = [e for e in cw if target_ids.intersection(_continue_watching_item_ids(e))]
        db["continue_watching"] = [e for e in cw if not target_ids.intersection(_continue_watching_item_ids(e))]
        dismissed_ids = set(target_ids)
        for entry in matching:
            dismissed_ids.update(_continue_watching_item_ids(entry))
        if blacklist:
            removed_list = [str(value) for value in db.get("removed_continue_watching", []) if value]
            for mid in dismissed_ids:
                if mid not in removed_list:
                    removed_list.append(mid)
            db["removed_continue_watching"] = removed_list[-200:]
        if user_action:
            user_dismissed = list(_user_dismissed_continue_watching_ids(db))
            for mid in dismissed_ids:
                if mid not in user_dismissed:
                    user_dismissed.append(mid)
            db["user_dismissed_continue_watching"] = user_dismissed[-200:]
        _write_db(db)
    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_sync_enabled():
                stremio_sync.sync_in_background()
        except Exception as e:
            logging.warning(f"Failed to trigger remote sync: {e}")

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
    clear_all_cached_catalogs()
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

def get_addon_health():
    """Return dictionary of {manifest_url: {'online': bool, 'last_checked': float, 'latency_ms': int, 'error': str}}"""
    return get_setting("addon_health", {}) or {}

def update_addon_health(url, online=True, latency_ms=0, error=None):
    """Update health status for a single addon."""
    if not url:
        return
    with _db_lock:
        db = _read_db()
        settings = db.setdefault("settings", {})
        health = settings.setdefault("addon_health", {})
        cutoff = time.time() - 7 * 86400
        expired = [k for k, v in list(health.items()) if isinstance(v, dict) and v.get("last_checked", 0) < cutoff]
        for k in expired:
            health.pop(k, None)
        while len(health) > 200:
            health.pop(next(iter(health)), None)
        health[str(url)] = {
            "online": bool(online),
            "last_checked": time.time(),
            "latency_ms": int(latency_ms),
            "error": str(error) if error else None
        }
        _write_db(db)

def set_all_addons_health(health_dict):
    """Batch persist health status for multiple addons."""
    if not isinstance(health_dict, dict) or not health_dict:
        return
    with _db_lock:
        db = _read_db()
        settings = db.setdefault("settings", {})
        health = settings.setdefault("addon_health", {})
        health.update(health_dict)
        cutoff = time.time() - 7 * 86400
        expired = [k for k, v in list(health.items()) if isinstance(v, dict) and v.get("last_checked", 0) < cutoff]
        for k in expired:
            health.pop(k, None)
        while len(health) > 200:
            health.pop(next(iter(health)), None)
        _write_db(db)

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
        tf_cache.pop(str(info_hash).lower(), None)
        tf_cache[str(info_hash).lower()] = clean_files
        while len(tf_cache) > 200:
            tf_cache.pop(next(iter(tf_cache)), None)
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
    should_flush = False
    with _db_lock:
        _progress_buffer[key] = position
        while len(_progress_buffer) > 1000:
            _progress_buffer.pop(next(iter(_progress_buffer)), None)
        now = time.time()
        if now - _progress_last_flush >= _PROGRESS_FLUSH_INTERVAL:
            _progress_last_flush = now
            should_flush = True
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

def add_addon(addon, sync_to_remote=True):
    # Ensure mandatory fields are initialized conforming to Stremio spec
    if not isinstance(addon.get("catalogs"), list):
        addon["catalogs"] = []
    if not isinstance(addon.get("resources"), list) or not addon["resources"]:
        addon["resources"] = ["catalog", "meta"] if addon["catalogs"] else ["stream"]
    raw_types = addon.get("types")
    if not isinstance(raw_types, list) or not raw_types:
        found_types = set()
        for cat in addon.get("catalogs", []):
            if isinstance(cat, dict) and cat.get("type"):
                found_types.add(str(cat["type"]))
        if found_types:
            addon["types"] = sorted(list(found_types))
        elif any((isinstance(r, str) and r == "stream") or (isinstance(r, dict) and r.get("name") == "stream") for r in addon.get("resources", [])):
            addon["types"] = ["movie", "series", "anime", "other"]
        else:
            addon["types"] = ["movie", "series"]

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
        # Also remove from user removed tracking
        user_removed_ids = set(settings.get("removed_addon_ids", []))
        user_removed_urls = set(settings.get("removed_addon_urls", []))
        if addon.get("id"):
            user_removed_ids.discard(addon.get("id"))
        if addon.get("manifest_url"):
            user_removed_urls.discard(addon.get("manifest_url"))
        settings["removed_addon_ids"] = list(user_removed_ids)
        settings["removed_addon_urls"] = list(user_removed_urls)
        _write_db(db)
    clear_all_cached_streams()
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass
    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_addon_sync_enabled():
                stremio_sync.sync_addons_in_background()
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

def remove_addon(addon_id=None, manifest_url=None, sync_to_remote=True):
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

        # Track user removed addons so remote sync doesn't resurrect them
        # (keep removal order so trimming evicts the oldest tombstone)
        user_removed_ids = [str(v) for v in settings.get("removed_addon_ids", []) if v]
        user_removed_urls = [str(v) for v in settings.get("removed_addon_urls", []) if v]
        for rid in removed_ids:
            rid = str(rid)
            if rid not in user_removed_ids:
                user_removed_ids.append(rid)
        for rurl in removed_urls:
            rurl = str(rurl)
            if rurl not in user_removed_urls:
                user_removed_urls.append(rurl)
        settings["removed_addon_ids"] = user_removed_ids[-200:]
        settings["removed_addon_urls"] = user_removed_urls[-200:]
        
        _write_db(db)
    
    for m_url in removed_urls:
        clear_cached_catalog_for_url(m_url)
    clear_all_cached_streams()
    
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

    if sync_to_remote and not _is_testing():
        try:
            from . import stremio_sync
            if stremio_sync.is_addon_sync_enabled():
                stremio_sync.sync_addons_in_background()
        except Exception:
            pass

def clear_cached_catalog_for_url(manifest_url):
    if not manifest_url:
        return
    try:
        with _MEM_CATALOG_LOCK:
            keys_to_del = [k for k in _MEM_CATALOG_CACHE if manifest_url in str(k)]
            for k in keys_to_del:
                _MEM_CATALOG_CACHE.pop(k, None)
    except Exception:
        pass
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

def set_addon_enabled(addon_id, enabled, manifest_url=None):
    target_url = manifest_url
    with _db_lock:
        db = _read_db()
        for a in db.get("addons", []):
            if (addon_id and a.get("id") == addon_id) or (target_url and a.get("manifest_url") == target_url):
                a["enabled"] = bool(enabled)
                target_url = a.get("manifest_url") or target_url
                break
        _write_db(db)
    clear_all_cached_streams()
    if target_url:
        clear_cached_catalog_for_url(target_url)
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

def set_addon_catalogs_enabled(addon_id, enabled, manifest_url=None):
    target_url = manifest_url
    with _db_lock:
        db = _read_db()
        for a in db.get("addons", []):
            if (addon_id and a.get("id") == addon_id) or (target_url and a.get("manifest_url") == target_url):
                a["catalogs_enabled"] = bool(enabled)
                target_url = a.get("manifest_url") or target_url
                break
        _write_db(db)
    if target_url:
        clear_cached_catalog_for_url(target_url)
    try:
        from . import api
        api.invalidate_catalogs_cache()
    except Exception:
        pass

def set_addon_streams_enabled(addon_id, enabled, manifest_url=None):
    with _db_lock:
        db = _read_db()
        for a in db.get("addons", []):
            if (addon_id and a.get("id") == addon_id) or (manifest_url and a.get("manifest_url") == manifest_url):
                a["streams_enabled"] = bool(enabled)
                break
        _write_db(db)
    clear_all_cached_streams()

def is_addon_catalogs_enabled(addon):
    if not addon or not addon.get("enabled", True):
        return False
    return bool(addon.get("catalogs_enabled", True))

def is_addon_streams_enabled(addon):
    if not addon or not addon.get("enabled", True):
        return False
    return bool(addon.get("streams_enabled", True))

# --- SQLite Metadata & Stream Cache ---

def _get_cache_db():
    """Return persistent SQLite connection, initializing schema once."""
    global _cache_conn, _cache_db_initialized
    if _cache_conn is not None and _cache_db_initialized:
        return _cache_conn
    with _cache_db_lock:
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
        # Migration: Purge any contaminated rate-limit / banned messages from cache
        try:
            _cache_conn.execute("DELETE FROM stream_cache WHERE data LIKE '%ratelimit%' OR data LIKE '%Aguarde 5 Minutos%' OR data LIKE '%(Banned)%'")
            _cache_conn.commit()
        except Exception:
            pass
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
        _cache_conn.execute("""
            CREATE TABLE IF NOT EXISTS item_genres (
                item_id TEXT,
                media_type TEXT,
                genre TEXT,
                year TEXT,
                PRIMARY KEY (item_id, genre)
            )
        """)
        try:
            _cache_conn.execute("CREATE INDEX IF NOT EXISTS idx_item_genres_type ON item_genres(media_type, genre)")
            _cache_conn.execute("CREATE INDEX IF NOT EXISTS idx_item_genres_genre ON item_genres(genre)")
            _cache_conn.execute("CREATE INDEX IF NOT EXISTS idx_item_genres_genre_nocase ON item_genres(genre COLLATE NOCASE, media_type)")
            _cache_conn.execute("CREATE INDEX IF NOT EXISTS idx_item_genres_year ON item_genres(year)")
            _cache_conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_year ON metadata_cache(json_extract(data, '$.year'))")
        except Exception:
            pass

    # Migration: Backfill item_genres from metadata_cache if empty
    try:
        cur = _cache_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM item_genres")
        row = cur.fetchone()
        if row and row[0] == 0:
            _cache_conn.execute("""
                INSERT OR IGNORE INTO item_genres (item_id, media_type, genre, year)
                SELECT m.id, m.media_type, trim(j.value), json_extract(m.data, '$.year')
                FROM metadata_cache m, json_each(m.data, '$.genres') j
                WHERE j.value IS NOT NULL AND trim(j.value) != ''
            """)
            _cache_conn.commit()
    except Exception:
        pass

    # Migration: Clean adult items and non-clean genres from item_genres
    try:
        _cache_conn.execute("CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, val TEXT)")
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'clean_item_genres_v3'")
        if not cur.fetchone():
            from .api import is_adult_item
            cur.execute("SELECT id, data FROM metadata_cache")
            adult_ids = []
            for r in cur.fetchall():
                try:
                    d = json.loads(r[1])
                    if is_adult_item(d):
                        adult_ids.append(r[0])
                except Exception:
                    pass
            for aid in adult_ids:
                cur.execute("DELETE FROM item_genres WHERE item_id = ?", (str(aid),))

            cur.execute("SELECT DISTINCT genre FROM item_genres WHERE genre != ''")
            to_delete = []
            for r in cur.fetchall():
                if not is_clean_genre(r[0]):
                    to_delete.append(r[0])
            for g in to_delete:
                cur.execute("DELETE FROM item_genres WHERE genre = ?", (g,))
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('clean_item_genres_v3', '1')")
            _cache_conn.commit()
    except Exception:
        pass

    # Migration: Invalidate catalog_cache entries where series catalogs were corrupted with movies
    try:
        _cache_conn.execute("CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, val TEXT)")
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'series_catalog_fix_v1'")
        if not cur.fetchone():
            _cache_conn.execute("DELETE FROM catalog_cache WHERE cache_key LIKE '%:series%' OR cache_key LIKE '%:series:%'")
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('series_catalog_fix_v1', '1')")
        cur.execute("SELECT val FROM cache_meta WHERE key = 'clean_empty_catalog_cache_v1'")
        if not cur.fetchone():
            _cache_conn.execute("DELETE FROM catalog_cache WHERE data = '[]' OR data = '' OR data IS NULL")
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('clean_empty_catalog_cache_v1', '1')")
        cur.execute("SELECT val FROM cache_meta WHERE key = 'clean_item_years_v2'")
        if not cur.fetchone():
            cur.execute("SELECT item_id, genre, year FROM item_genres WHERE year IS NOT NULL AND (length(year) != 4 OR year NOT GLOB '[1-2][0-9][0-9][0-9]')")
            bad_rows = cur.fetchall()
            for iid, g, yr in bad_rows:
                import re
                clean_y = ""
                m = re.search(r'\b(19\d\d|20\d\d)\b', str(yr or ""))
                if m:
                    clean_y = m.group(1)
                elif len(str(yr or "")) >= 4 and str(yr)[:4].isdigit():
                    clean_y = str(yr)[:4]
                if clean_y:
                    cur.execute("UPDATE item_genres SET year = ? WHERE item_id = ? AND genre = ?", (clean_y, iid, g))
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('clean_item_years_v2', '1')")
            _cache_conn.commit()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'strict_imdb_genres_v2'")
        if not cur.fetchone():
            cur.execute("SELECT DISTINCT genre FROM item_genres WHERE genre != ''")
            to_delete = []
            to_update = []
            for r in cur.fetchall():
                orig = r[0]
                norm = normalize_imdb_genre(orig)
                if not norm:
                    to_delete.append(orig)
                elif norm != orig:
                    to_update.append((norm, orig))
            for g in to_delete:
                cur.execute("DELETE FROM item_genres WHERE genre = ?", (g,))
            for norm, orig in to_update:
                cur.execute("UPDATE OR IGNORE item_genres SET genre = ? WHERE genre = ?", (norm, orig))
                cur.execute("DELETE FROM item_genres WHERE genre = ?", (orig,))
            # Evict catalog cache to clear poisoned rows
            _cache_conn.execute("DELETE FROM catalog_cache")
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('strict_imdb_genres_v2', '1')")
            _cache_conn.commit()
    except Exception:
        pass

    # Migration: Backfill genres from catalog_cache into item_genres and metadata_cache
    try:
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'backfill_catalog_genres_to_db_v1'")
        if not cur.fetchone():
            cur.execute("SELECT cache_key, data FROM catalog_cache WHERE data IS NOT NULL AND data != '' AND data != '[]'")
            rows = cur.fetchall()
            meta_batch = []
            genre_batch = []
            seen_ids = set()
            now_t = time.time()
            for ckey, data_str in rows:
                try:
                    cat_items = json.loads(data_str)
                    if isinstance(cat_items, list):
                        for it in cat_items:
                            if not isinstance(it, dict):
                                continue
                            iid = it.get("id") or it.get("imdb_id")
                            if not iid:
                                continue
                            mtype = it.get("type") or "movie"
                            yr = str(it.get("year") or "")
                            genres = it.get("genres") or []
                            if isinstance(genres, str):
                                genres = [genres]
                            if iid not in seen_ids:
                                seen_ids.add(iid)
                                meta_batch.append((str(iid), str(mtype), json.dumps(it), now_t))
                            for g in genres:
                                norm = normalize_imdb_genre(str(g).strip(), media_type=mtype)
                                if norm:
                                    genre_batch.append((str(iid), str(mtype), norm, yr))
                except Exception:
                    pass
            if meta_batch:
                _cache_conn.executemany("INSERT OR IGNORE INTO metadata_cache VALUES (?, ?, ?, ?)", meta_batch)
            if genre_batch:
                _cache_conn.executemany("INSERT OR IGNORE INTO item_genres VALUES (?, ?, ?, ?)", genre_batch)
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('backfill_catalog_genres_to_db_v1', '1')")
            _cache_conn.commit()
    except Exception as e:
        print(f"Error in backfill_catalog_genres migration: {e}")

    # Rebuild missing genre index rows from all cached metadata. Older app
    # versions only indexed string-valued `genres`, which missed TMDB IDs and
    # Stremio `{id, name}` genre objects.
    try:
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'genre_index_metadata_v2'")
        if not cur.fetchone():
            cur.execute("SELECT id, media_type, data FROM metadata_cache WHERE data IS NOT NULL AND data != ''")
            genre_rows = []
            alias_metadata_rows = []
            for item_id, media_type, data_json in cur.fetchall():
                try:
                    details = json.loads(data_json)
                except Exception:
                    continue
                if not isinstance(details, dict):
                    continue
                year = extract_clean_year(details)
                indexed_ids = {str(item_id)}
                for alias_id in [details.get("imdb_id")] + list(details.get("alias_ids") or []):
                    if alias_id:
                        indexed_ids.add(str(alias_id))
                for alias_id in indexed_ids:
                    if alias_id != str(item_id):
                        alias_metadata_rows.append((alias_id, str(media_type or "movie"), data_json, time.time()))
                for genre in _extract_metadata_genres(details, media_type):
                    for alias_id in indexed_ids:
                        genre_rows.append((alias_id, str(media_type or "movie"), genre, year))
            if alias_metadata_rows:
                _cache_conn.executemany(
                    "INSERT OR IGNORE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
                    alias_metadata_rows,
                )
            if genre_rows:
                _cache_conn.executemany("INSERT OR IGNORE INTO item_genres VALUES (?, ?, ?, ?)", genre_rows)
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('genre_index_metadata_v2', '1')")
            _cache_conn.commit()
    except Exception as e:
        print(f"Error rebuilding metadata genre index: {e}")

    # Migration: Backfill language genres into item_genres from metadata_cache
    try:
        cur = _cache_conn.cursor()
        cur.execute("SELECT val FROM cache_meta WHERE key = 'backfill_languages_to_item_genres_v2'")
        if not cur.fetchone():
            cur.execute("""
                SELECT id, media_type, data FROM metadata_cache
                WHERE json_extract(data, '$.language') IS NOT NULL
                   OR json_extract(data, '$.original_language') IS NOT NULL
                   OR json_extract(data, '$.spoken_languages') IS NOT NULL
                   OR json_extract(data, '$.country') LIKE '%India%'
            """)
            rows = cur.fetchall()
            lang_batch = []
            for item_id, media_type, data_json in rows:
                if not data_json:
                    continue
                try:
                    d = json.loads(data_json)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                clean_yr = extract_clean_year(d)
                mtype = media_type or d.get("type") or "movie"
                for g in _extract_metadata_genres(d, mtype):
                    if is_language_genre(g) and g.lower() != "english":
                        lang_batch.append((str(item_id), str(mtype), g, clean_yr))
            if lang_batch:
                _cache_conn.executemany("""
                    INSERT INTO item_genres (item_id, media_type, genre, year)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(item_id, genre) DO UPDATE SET
                        year = CASE WHEN excluded.year != '' THEN excluded.year ELSE item_genres.year END,
                        media_type = excluded.media_type
                """, lang_batch)
            _cache_conn.execute("INSERT OR REPLACE INTO cache_meta (key, val) VALUES ('backfill_languages_to_item_genres_v2', '1')")
            _cache_conn.commit()
    except Exception as e:
        print(f"Error in backfill_languages migration: {e}")

    _cache_conn.commit()
    _cache_db_initialized = True
    return _cache_conn

def close_cache_db():
    """Cleanly close persistent SQLite cache database connection."""
    global _cache_conn, _cache_db_initialized
    with _cache_db_lock:
        if _cache_conn is not None:
            try:
                _cache_conn.close()
            except Exception:
                pass
            _cache_conn = None
            _cache_db_initialized = False

atexit.register(close_cache_db)

_genre_listeners = []
_genre_listeners_lock = threading.Lock()

def register_genre_listener(callback):
    """Register a callback to be notified when new genres are added to the database."""
    with _genre_listeners_lock:
        if callback not in _genre_listeners:
            _genre_listeners.append(callback)

def unregister_genre_listener(callback):
    """Unregister a genre update callback."""
    with _genre_listeners_lock:
        if callback in _genre_listeners:
            _genre_listeners.remove(callback)

def _notify_genre_listeners():
    with _genre_listeners_lock:
        callbacks = list(_genre_listeners)
    for cb in callbacks:
        try:
            cb()
        except Exception as e:
            print(f"Error in genre listener callback: {e}")

def extract_clean_year(val_or_dict):
    """
    Extract a canonical 4-digit year string (e.g. '2019') from a string, integer,
    or item dictionary checking 'year', 'releaseInfo', and 'released'.
    """
    if not val_or_dict:
        return ""
    if isinstance(val_or_dict, dict):
        y = str(val_or_dict.get("year") or "").strip()
        if not y or not any(c.isdigit() for c in y):
            y = str(val_or_dict.get("releaseInfo") or "").strip()
        if not y or not any(c.isdigit() for c in y):
            y = str(val_or_dict.get("released") or "").strip()
    else:
        y = str(val_or_dict).strip()

    if not y:
        return ""
    import re
    m = re.search(r'\b(19\d\d|20\d\d)\b', y)
    if m:
        return m.group(1)
    if len(y) >= 4 and y[:4].isdigit():
        return y[:4]
    return ""

def get_cached_metadata(item_id, media_type=None):
    if not item_id:
        return None
    if isinstance(item_id, (list, tuple, set)):
        for single_id in item_id:
            res = get_cached_metadata(single_id, media_type)
            if res:
                return res
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

def hydrate_items_from_metadata(items, media_type=None):
    """
    Fast batch-hydrate catalog items in-place using SQLite metadata_cache and item_genres.
    Takes a list of item dicts and enriches any items missing genres/year/title/cover/desc/lang.
    Returns the number of items that were updated.
    """
    if not items or not isinstance(items, list):
        return 0

    to_hydrate = []
    id_to_items = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = it.get("id") or it.get("imdb_id")
        if not iid:
            continue
        # Ensure clean year is extracted from releaseInfo / released
        clean_yr = extract_clean_year(it)
        if clean_yr and not it.get("year"):
            it["year"] = clean_yr

        has_genres = bool(it.get("genres"))
        has_year = bool(it.get("year"))
        has_desc = bool(it.get("description") or it.get("overview"))
        has_cover = bool(it.get("medium_cover_image") or it.get("poster"))
        has_title = bool(it.get("title") and it.get("title") != "Unknown")
        has_lang = bool(it.get("language"))

        if not (has_genres and has_year and has_desc and has_cover and has_title and has_lang):
            candidates = [str(iid)]
            if it.get("imdb_id") and str(it["imdb_id"]) not in candidates:
                candidates.append(str(it["imdb_id"]))
            for aid in (it.get("alias_ids") or []):
                if aid and str(aid) not in candidates:
                    candidates.append(str(aid))
            for cid in candidates:
                id_to_items.setdefault(cid, []).append(it)
            to_hydrate.append(it)

    if not id_to_items:
        return 0

    updated_items = set()

    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            all_ids = list(id_to_items.keys())

            # 1. Batch query metadata_cache in chunks of 500
            for i in range(0, len(all_ids), 500):
                chunk = all_ids[i:i+500]
                placeholders = ",".join("?" for _ in chunk)
                cursor.execute(f"SELECT id, data FROM metadata_cache WHERE id IN ({placeholders})", chunk)
                for row_id, data_str in cursor.fetchall():
                    if not data_str:
                        continue
                    try:
                        meta = json.loads(data_str)
                    except Exception:
                        continue
                    if not isinstance(meta, dict):
                        continue

                    target_items = id_to_items.get(str(row_id), [])
                    for it in target_items:
                        it_updated = False
                        if (not it.get("title") or it.get("title") == "Unknown") and (meta.get("title") or meta.get("name")):
                            t = meta.get("title") or meta.get("name")
                            if t and t != "Unknown":
                                it["title"] = t
                                it_updated = True
                        if not it.get("medium_cover_image") and not it.get("poster"):
                            cov = meta.get("medium_cover_image") or meta.get("poster")
                            if cov:
                                it["medium_cover_image"] = cov
                                it["poster"] = cov
                                it_updated = True
                        if not it.get("genres") and meta.get("genres"):
                            it["genres"] = list(meta["genres"])
                            it_updated = True
                        c_yr = extract_clean_year(meta) or meta.get("year")
                        if not it.get("year") and c_yr:
                            it["year"] = str(c_yr)
                            it_updated = True
                        desc = meta.get("description") or meta.get("overview")
                        if not it.get("description") and desc:
                            it["description"] = desc
                            it_updated = True
                        if not it.get("imdbRating") and meta.get("imdbRating"):
                            it["imdbRating"] = str(meta["imdbRating"])
                            it_updated = True
                        if not it.get("runtime") and meta.get("runtime"):
                            it["runtime"] = str(meta["runtime"])
                            it_updated = True
                        meta_lang = meta.get("language") or meta.get("original_language")
                        if meta_lang:
                            norm_lang = normalize_imdb_genre(str(meta_lang))
                            if norm_lang:
                                if not it.get("language"):
                                    it["language"] = norm_lang
                                    it_updated = True
                        if not it.get("background") and meta.get("background"):
                            it["background"] = meta["background"]
                            it_updated = True
                        if not it.get("cast") and meta.get("cast"):
                            it["cast"] = list(meta["cast"])
                            it_updated = True
                        if not it.get("videos") and meta.get("videos"):
                            it["videos"] = meta["videos"]
                            it_updated = True
                        if meta.get("adult") or meta.get("isAdult"):
                            it["adult"] = True
                        if it_updated:
                            updated_items.add(id(it))

            # 2. For items still missing genres, year, or language, check item_genres table
            still_need_genres = [it for it in to_hydrate if not it.get("genres") or not it.get("year") or not it.get("language")]
            if still_need_genres:
                need_ids = []
                need_map = {}
                for it in still_need_genres:
                    iid = str(it.get("id") or it.get("imdb_id") or "")
                    if iid:
                        need_ids.append(iid)
                        need_map.setdefault(iid, []).append(it)
                if need_ids:
                    for i in range(0, len(need_ids), 500):
                        chunk = need_ids[i:i+500]
                        placeholders = ",".join("?" for _ in chunk)
                        cursor.execute(f"SELECT item_id, genre, year FROM item_genres WHERE item_id IN ({placeholders})", chunk)
                        for row_iid, genre_val, yr_val in cursor.fetchall():
                            for it in need_map.get(str(row_iid), []):
                                if genre_val and genre_val.strip():
                                    g_clean = genre_val.strip()
                                    if is_language_genre(g_clean):
                                        if not it.get("language"):
                                            it["language"] = g_clean
                                            updated_items.add(id(it))
                                    else:
                                        if "genres" not in it or not it["genres"]:
                                            it["genres"] = [g_clean]
                                            updated_items.add(id(it))
                                        elif g_clean not in it["genres"]:
                                            it["genres"].append(g_clean)
                                            updated_items.add(id(it))
                                if yr_val and yr_val.strip() and not it.get("year"):
                                    it["year"] = yr_val.strip()
                                    updated_items.add(id(it))

    except Exception as e:
        print(f"Error in hydrate_items_from_metadata: {e}")

    return len(updated_items)

def save_cached_metadata(item_id, media_type, details):
    if not item_id or not details:
        return
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            now = time.time()
            if isinstance(details, dict):
                # Merge with existing record in metadata_cache so rich fields are never lost
                try:
                    cursor.execute("SELECT data FROM metadata_cache WHERE id = ?", (str(item_id),))
                    row = cursor.fetchone()
                    if row and row[0]:
                        existing_meta = json.loads(row[0])
                        if isinstance(existing_meta, dict):
                            for k, v in existing_meta.items():
                                if k not in details or not details[k]:
                                    details[k] = v
                except Exception:
                    pass

                if details.get("name") and not details.get("title"):
                    details["title"] = details["name"]
                if details.get("title") and not details.get("name"):
                    details["name"] = details["title"]
                if details.get("poster") and not details.get("medium_cover_image"):
                    details["medium_cover_image"] = details["poster"]
                if details.get("medium_cover_image") and not details.get("poster"):
                    details["poster"] = details["medium_cover_image"]
                clean_yr = extract_clean_year(details)
                if clean_yr and not details.get("year"):
                    details["year"] = clean_yr
            data_json = json.dumps(details)
            cursor.execute(
                "INSERT OR REPLACE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
                (str(item_id), str(media_type or "movie"), data_json, now)
            )
            imdb_id = details.get("imdb_id") if isinstance(details, dict) else None
            if imdb_id and str(imdb_id) != str(item_id):
                cursor.execute(
                    "INSERT OR REPLACE INTO metadata_cache (id, media_type, data, updated_at) VALUES (?, ?, ?, ?)",
                    (str(imdb_id), str(media_type or "movie"), data_json, now)
                )

            # Extract and store genres and year into item_genres table
            from .api import is_adult_item
            is_adult = is_adult_item(details) if details else False

            genres = _extract_metadata_genres(details, media_type)

            year_val = extract_clean_year(details)

            has_new = False
            if not is_adult:
                ids_to_index = [str(item_id)]
                if imdb_id and str(imdb_id) not in ids_to_index:
                    ids_to_index.append(str(imdb_id))
                for alias_id in details.get("alias_ids") or []:
                    if alias_id and str(alias_id) not in ids_to_index:
                        ids_to_index.append(str(alias_id))

                for iid in ids_to_index:
                    if genres:
                        for norm_g in genres:
                            cursor.execute(
                                """
                                INSERT INTO item_genres (item_id, media_type, genre, year)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(item_id, genre) DO UPDATE SET
                                    year = CASE WHEN excluded.year != '' THEN excluded.year ELSE item_genres.year END,
                                    media_type = excluded.media_type
                                """,
                                (iid, str(media_type or "movie"), norm_g, year_val)
                            )
                            if cursor.rowcount > 0:
                                has_new = True
                        # Remove empty genre placeholder if real genres now exist
                        cursor.execute("DELETE FROM item_genres WHERE item_id = ? AND genre = ''", (iid,))

                    item_lang = details.get("language")
                    if item_lang:
                        for l_sub in str(item_lang).split(","):
                            l_sub = l_sub.strip()
                            if l_sub and l_sub.lower() != "english":
                                norm_lang = normalize_imdb_genre(l_sub, media_type=media_type)
                                if norm_lang:
                                    cursor.execute(
                                        """
                                        INSERT INTO item_genres (item_id, media_type, genre, year)
                                        VALUES (?, ?, ?, ?)
                                        ON CONFLICT(item_id, genre) DO UPDATE SET
                                            year = CASE WHEN excluded.year != '' THEN excluded.year ELSE item_genres.year END,
                                            media_type = excluded.media_type
                                        """,
                                        (iid, str(media_type or "movie"), norm_lang, year_val)
                                    )
                                    if cursor.rowcount > 0:
                                        has_new = True

                    if not genres and year_val:
                        cursor.execute(
                            """
                            INSERT INTO item_genres (item_id, media_type, genre, year)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(item_id, genre) DO UPDATE SET
                                year = CASE WHEN excluded.year != '' THEN excluded.year ELSE item_genres.year END,
                                media_type = excluded.media_type
                            """,
                            (iid, str(media_type or "movie"), "", year_val)
                        )
                        if cursor.rowcount > 0:
                            has_new = True

            # Update matching items inside in-memory catalog cache (_MEM_CATALOG_CACHE)
            try:
                with _MEM_CATALOG_LOCK:
                    for _ckey, (c_items, _ctime) in _MEM_CATALOG_CACHE.items():
                        if isinstance(c_items, list):
                            for cit in c_items:
                                if isinstance(cit, dict):
                                    cit_id = cit.get("id") or cit.get("imdb_id")
                                    if cit_id and str(cit_id) == str(item_id):
                                        if genres and not cit.get("genres"):
                                            cit["genres"] = list(genres)
                                        if year_val and not cit.get("year"):
                                            cit["year"] = str(year_val)
                                        if details.get("title") and (not cit.get("title") or cit.get("title") == "Unknown"):
                                            cit["title"] = details["title"]
                                        if (details.get("medium_cover_image") or details.get("poster")) and not cit.get("medium_cover_image"):
                                            cov = details.get("medium_cover_image") or details.get("poster")
                                            cit["medium_cover_image"] = cov
                                            cit["poster"] = cov
                                        if (details.get("description") or details.get("overview")) and not cit.get("description"):
                                            cit["description"] = details.get("description") or details.get("overview")
                                        if details.get("language") and not cit.get("language"):
                                            cit["language"] = details["language"]
            except Exception:
                pass


            conn.commit()
            if has_new:
                invalidate_dropdown_caches()
                _notify_genre_listeners()
    except Exception as e:
        print(f"Error saving metadata cache: {e}")

IMDB_MOVIE_GENRES = [
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime",
    "Documentary", "Drama", "Family", "Fantasy", "Film-Noir", "History",
    "Horror", "Music", "Musical", "Mystery", "Romance", "Sci-Fi",
    "Short", "Sport", "Thriller", "War", "Western"
]

IMDB_SERIES_GENRES = [
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime",
    "Documentary", "Drama", "Family", "Fantasy", "Film-Noir", "Game-Show",
    "History", "Horror", "Music", "Musical", "Mystery", "News",
    "Reality-TV", "Romance", "Sci-Fi", "Short", "Sport", "Talk-Show",
    "Thriller", "War", "Western"
]

DEFAULT_ANIME_GENRES = [
    "Action", "Adventure", "Cars", "Comedy", "Dementia", "Demons", "Drama",
    "Fantasy", "Game", "Harem", "Historical", "Horror", "Josei", "Kids",
    "Magic", "Martial Arts", "Mecha", "Military", "Music", "Mystery",
    "Parody", "Police", "Psychological", "Romance", "Samurai", "School",
    "Sci-Fi", "Seinen", "Shoujo", "Shounen", "Slice of Life", "Space",
    "Sports", "Super Power", "Supernatural", "Thriller", "Vampire"
]

IMDB_ALL_GENRES = sorted(
    list(set(IMDB_MOVIE_GENRES) | set(IMDB_SERIES_GENRES)),
    key=lambda s: s.lower()
)

DEFAULT_MOVIE_GENRES = IMDB_MOVIE_GENRES
DEFAULT_SERIES_GENRES = IMDB_SERIES_GENRES

GENRE_ALIASES = {
    "science fiction": "Sci-Fi",
    "sci fi": "Sci-Fi",
    "sci-fi": "Sci-Fi",
    "scifi": "Sci-Fi",
    "talk show": "Talk-Show",
    "talk-show": "Talk-Show",
    "talk": "Talk-Show",
    "game show": "Game-Show",
    "game-show": "Game-Show",
    "reality tv": "Reality-TV",
    "reality-tv": "Reality-TV",
    "reality": "Reality-TV",
    "film noir": "Film-Noir",
    "film-noir": "Film-Noir",
}

POPULAR_LANGUAGE_GENRES = [
    "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Bengali", "Punjabi",
    "Marathi", "Gujarati", "Urdu",
    "Korean", "Japanese", "Chinese",
    "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
    "Arabic", "Turkish", "Persian"
]

ALL_LANGUAGE_GENRES = [
    "Arabic", "Bengali", "Chinese", "Czech", "Danish", "Dutch", "English",
    "Finnish", "French", "German", "Greek", "Gujarati", "Hebrew", "Hindi",
    "Hungarian", "Indonesian", "Italian", "Japanese", "Kannada", "Korean",
    "Malayalam", "Marathi", "Norwegian", "Persian", "Polish", "Portuguese",
    "Punjabi", "Romanian", "Russian", "Spanish", "Swedish", "Tamil",
    "Telugu", "Thai", "Turkish", "Ukrainian", "Urdu", "Vietnamese"
]

LANGUAGE_CODE_MAP = {
    "hi": "Hindi", "hin": "Hindi",
    "ta": "Tamil", "tam": "Tamil",
    "te": "Telugu", "tel": "Telugu",
    "ml": "Malayalam", "mal": "Malayalam",
    "kn": "Kannada", "kan": "Kannada",
    "bn": "Bengali", "ben": "Bengali",
    "pa": "Punjabi", "pan": "Punjabi",
    "mr": "Marathi", "mar": "Marathi",
    "gu": "Gujarati", "guj": "Gujarati",
    "ur": "Urdu", "urd": "Urdu",
    "ko": "Korean", "kor": "Korean",
    "ja": "Japanese", "jpn": "Japanese",
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese",
    "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fra": "French", "fre": "French",
    "de": "German", "deu": "German", "ger": "German",
    "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian",
    "ar": "Arabic", "ara": "Arabic",
    "tr": "Turkish", "tur": "Turkish",
    "fa": "Persian", "fas": "Persian", "per": "Persian",
    "th": "Thai", "tha": "Thai",
    "id": "Indonesian", "ind": "Indonesian",
    "vi": "Vietnamese", "vie": "Vietnamese",
    "pl": "Polish", "pol": "Polish",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch",
    "sv": "Swedish", "swe": "Swedish",
    "da": "Danish", "dan": "Danish",
    "no": "Norwegian", "nor": "Norwegian",
    "fi": "Finnish", "fin": "Finnish",
    "el": "Greek", "ell": "Greek", "gre": "Greek",
    "he": "Hebrew", "heb": "Hebrew",
    "cs": "Czech", "ces": "Czech", "cze": "Czech",
    "hu": "Hungarian", "hun": "Hungarian",
    "ro": "Romanian", "ron": "Romanian", "rum": "Romanian",
    "uk": "Ukrainian", "ukr": "Ukrainian",
    "en": "English", "eng": "English"
}

LANGUAGE_NAME_TO_CODE = {
    v.lower(): k for k, v in LANGUAGE_CODE_MAP.items() if len(k) == 2
}

def is_language_genre(g):
    if not g or not isinstance(g, str):
        return False
    gl = g.strip().lower()
    if gl in LANGUAGE_CODE_MAP:
        return True
    return any(lg.lower() == gl for lg in ALL_LANGUAGE_GENRES)

def get_language_code(g):
    if not g or not isinstance(g, str):
        return None
    gl = g.strip().lower()
    if len(gl) == 2 and gl in LANGUAGE_CODE_MAP:
        return gl
    return LANGUAGE_NAME_TO_CODE.get(gl)

def normalize_imdb_genre(g, media_type=None):
    """
    Validate and normalize genre against canonical IMDb genres and canonical language genres.
    Returns canonical genre or language genre string, or None if invalid/non-canonical.
    """
    if not g or not isinstance(g, str):
        return None
    g = g.strip()
    if not g or len(g) > 30:
        return None
    gl = g.lower()

    from .api import ADULT_GENRES_TAGS, ADULT_KEYWORD_REGEX
    if gl in ADULT_GENRES_TAGS or ADULT_KEYWORD_REGEX.search(gl):
        return None

    if gl in GENRE_ALIASES:
        return GENRE_ALIASES[gl]

    # Check language codes / names (e.g. 'hi' -> 'Hindi', 'hindi' -> 'Hindi')
    if gl in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[gl]
    for lg in ALL_LANGUAGE_GENRES:
        if lg.lower() == gl:
            return lg

    mt = (media_type or "").lower()
    if mt == "anime":
        for ag in DEFAULT_ANIME_GENRES:
            if ag.lower() == gl:
                return ag
        for ig in IMDB_ALL_GENRES:
            if ig.lower() == gl:
                return ig
        return None

    target_list = IMDB_MOVIE_GENRES if mt == "movie" else (IMDB_SERIES_GENRES if mt in ("series", "tvshow") else IMDB_ALL_GENRES)
    for ig in target_list:
        if ig.lower() == gl:
            return ig

    for ig in IMDB_ALL_GENRES:
        if ig.lower() == gl:
            return ig

    return None

def _extract_metadata_genres(details, media_type=None):
    """Return canonical genre labels from common metadata and Stremio shapes."""
    if not isinstance(details, dict):
        return []
    from .tmdb_helper import TMDB_GENRES

    raw = details.get("genres") or details.get("genre") or details.get("genre_ids") or []
    values = raw if isinstance(raw, list) else [raw]
    genres = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("id")
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            value = TMDB_GENRES.get(int(value))
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            normalized = normalize_imdb_genre(part.strip(), media_type=media_type)
            if normalized:
                genres.add(normalized)

    # Extract language signals into canonical language genres
    lang_raw = []
    for k in ("language", "languages", "original_language", "spoken_languages"):
        v = details.get(k)
        if v:
            if isinstance(v, list):
                lang_raw.extend(v)
            else:
                lang_raw.append(v)
    for l_val in lang_raw:
        if isinstance(l_val, dict):
            l_val = l_val.get("english_name") or l_val.get("name") or l_val.get("iso_639_1")
        if not isinstance(l_val, str):
            continue
        for part in l_val.split(","):
            normalized = normalize_imdb_genre(part.strip(), media_type=media_type)
            if normalized and is_language_genre(normalized):
                genres.add(normalized)

    # Country signal fallback (e.g. India -> Hindi if no Indian language present)
    country_val = details.get("country")
    if country_val and isinstance(country_val, str) and "india" in country_val.lower():
        indian_langs = {"Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Bengali", "Punjabi", "Marathi", "Gujarati", "Urdu"}
        if not (genres & indian_langs):
            genres.add("Hindi")

    return sorted(genres)

def is_clean_genre(g, media_type=None):
    """Validate that genre is a strict standard IMDb genre."""
    return normalize_imdb_genre(g, media_type=media_type) is not None

def get_stored_genres(media_type=None):
    """Return distinct clean IMDb genres stored in item_genres."""
    genres = set()
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            if media_type and media_type not in ("all", "discover"):
                cursor.execute(
                    "SELECT DISTINCT genre FROM item_genres WHERE media_type = ? AND genre != ''",
                    (str(media_type),)
                )
            else:
                cursor.execute("SELECT DISTINCT genre FROM item_genres WHERE genre != ''")
            for row in cursor.fetchall():
                if row and row[0]:
                    norm = normalize_imdb_genre(row[0].strip(), media_type=media_type)
                    if norm:
                        genres.add(norm)
    except Exception as e:
        print(f"Error reading stored genres: {e}")
    return sorted(list(genres), key=lambda s: s.lower())

def get_cached_items_by_genre(genre, media_type=None, year=None, sort_mode=None, limit=60, offset=0, language=None):
    """Retrieve catalog item dicts from SQLite cache matching a specific genre and optional media_type/year/sort_mode/language."""
    if not genre or genre in ("All", "All Genres", "all"):
        if language and language not in ("All", "All Languages", "all"):
            return get_cached_items_by_genre(language, media_type=media_type, year=year, sort_mode=sort_mode, limit=limit, offset=offset)
        if year:
            return get_cached_items_by_year(year, media_type=media_type, sort_mode=sort_mode, limit=limit, offset=offset)
        return []
    items = []
    seen_ids = set()
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            query = """
                SELECT m.data
                FROM item_genres ig
                JOIN metadata_cache m ON ig.item_id = m.id
                WHERE ig.genre = ? COLLATE NOCASE
            """
            params = [genre.strip()]
            if language and language not in ("All", "All Languages", "all") and language.strip().lower() != genre.strip().lower():
                query += " AND m.id IN (SELECT item_id FROM item_genres WHERE genre = ? COLLATE NOCASE)"
                params.append(language.strip())
            if media_type and media_type not in ("all", "discover"):
                mt = str(media_type).lower()
                if mt == "movie":
                    query += " AND (ig.media_type IN ('movie', 'collections') OR ig.media_type IS NULL OR ig.media_type = '')"
                elif mt == "series":
                    query += " AND (ig.media_type IN ('series', 'tv', 'show') OR ig.media_type IS NULL OR ig.media_type = '')"
                elif mt == "anime":
                    query += " AND (ig.media_type = 'anime' OR ig.media_type IS NULL OR ig.media_type = '')"
                else:
                    query += " AND ig.media_type = ?"
                    params.append(mt)
            if year:
                yr_str = str(year).strip()
                query += " AND (ig.year LIKE ? OR json_extract(m.data, '$.year') LIKE ?)"
                params.extend([f"{yr_str}%", f"{yr_str}%"])

            # Deduplicate rows that point to the same underlying IMDb/TMDB
            # item. Keep this after all WHERE predicates; placing it in the
            # initial query string makes subsequent media/year filters invalid.
            query += """
                GROUP BY COALESCE(
                    NULLIF(json_extract(m.data, '$.id'), ''),
                    NULLIF(json_extract(m.data, '$.imdb_id'), ''),
                    m.id
                )
            """

            if sort_mode == "Recent":
                query += """
                    ORDER BY CASE WHEN ig.year GLOB '[1-2][0-9][0-9][0-9]' THEN ig.year ELSE '0000' END DESC,
                             COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                             m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            else:
                query += """
                    ORDER BY COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                             m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            params.extend([limit, offset])
            cursor.execute(query, params)
            for row in cursor.fetchall():
                if not row or not row[0]:
                    continue
                try:
                    it = json.loads(row[0])
                    if isinstance(it, dict):
                        iid = it.get("id") or it.get("imdb_id")
                        if iid and iid not in seen_ids:
                            seen_ids.add(iid)
                            items.append(it)
                except Exception:
                    pass
    except Exception as e:
        print(f"Error fetching cached items by genre: {e}")
    if items:
        hydrate_items_from_metadata(items, media_type=media_type)
    return items

def get_cached_items_by_year(year, media_type=None, genre=None, sort_mode=None, limit=80, offset=0, language=None):
    """Retrieve catalog item dicts from SQLite cache matching a specific release year and optional media_type/genre/language."""
    if not year or str(year) in ("All Years", "All", "all", "None"):
        return []
    yr_str = str(year).strip()
    if not (yr_str.isdigit() and len(yr_str) == 4):
        return []
    items = []
    seen_ids = set()
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()

            effective_genre = genre
            if (not effective_genre or effective_genre in ("All", "All Genres", "all")) and language and language not in ("All", "All Languages", "all"):
                effective_genre = language

            if effective_genre and str(effective_genre) not in ("All", "All Genres", "all"):
                query = """
                    SELECT m.data
                    FROM item_genres ig
                    JOIN metadata_cache m ON ig.item_id = m.id
                    WHERE lower(ig.genre) = lower(?)
                      AND (ig.year = ? OR json_extract(m.data, '$.year') = ? OR json_extract(m.data, '$.year') = ?)
                """
                params = [effective_genre.strip(), yr_str, yr_str, int(yr_str)]
                if language and language not in ("All", "All Languages", "all") and language.strip().lower() != effective_genre.strip().lower():
                    query += " AND m.id IN (SELECT item_id FROM item_genres WHERE genre = ? COLLATE NOCASE)"
                    params.append(language.strip())
                if media_type and media_type not in ("all", "discover"):
                    mt = str(media_type).lower()
                    if mt == "movie":
                        query += " AND (ig.media_type IN ('movie', 'collections') OR ig.media_type IS NULL OR ig.media_type = '')"
                    elif mt == "series":
                        query += " AND (ig.media_type IN ('series', 'tv', 'show') OR ig.media_type IS NULL OR ig.media_type = '')"
                    elif mt == "anime":
                        query += " AND (ig.media_type = 'anime' OR ig.media_type IS NULL OR ig.media_type = '')"
                    else:
                        query += " AND ig.media_type = ?"
                        params.append(mt)
            else:
                query = """
                    SELECT m.data
                    FROM metadata_cache m
                    WHERE (json_extract(m.data, '$.year') = ? OR json_extract(m.data, '$.year') = ?
                           OR m.id IN (SELECT item_id FROM item_genres WHERE year = ?))
                """
                params = [yr_str, int(yr_str), yr_str]
                if language and language not in ("All", "All Languages", "all"):
                    query += " AND m.id IN (SELECT item_id FROM item_genres WHERE genre = ? COLLATE NOCASE)"
                    params.append(language.strip())
                if media_type and media_type not in ("all", "discover"):
                    mt = str(media_type).lower()
                    if mt == "movie":
                        query += " AND (m.media_type IN ('movie', 'collections') OR m.media_type IS NULL OR m.media_type = '')"
                    elif mt == "series":
                        query += " AND (m.media_type IN ('series', 'tv', 'show') OR m.media_type IS NULL OR m.media_type = '')"
                    elif mt == "anime":
                        query += " AND (m.media_type = 'anime' OR m.media_type IS NULL OR m.media_type = '')"
                    else:
                        query += " AND m.media_type = ?"
                        params.append(mt)

            if sort_mode == "Recent":
                query += """
                    ORDER BY COALESCE(json_extract(m.data, '$.year'), 0) DESC,
                             COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                             m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            else:
                query += """
                    ORDER BY COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                             m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            params.extend([limit, offset])
            cursor.execute(query, params)
            for row in cursor.fetchall():
                if not row or not row[0]:
                    continue
                try:
                    it = json.loads(row[0])
                    if isinstance(it, dict):
                        iid = it.get("id") or it.get("imdb_id")
                        if iid and iid not in seen_ids:
                            seen_ids.add(iid)
                            items.append(it)
                except Exception:
                    pass
    except Exception as e:
        print(f"Error fetching cached items by year: {e}")
    if items:
        hydrate_items_from_metadata(items, media_type=media_type)
    return items

def get_cached_items_by_search(query, media_type=None, genre=None, year=None, sort_mode=None, limit=60, offset=0, title_only=False, language=None):
    """Retrieve catalog item dicts from SQLite cache matching a search keyword and optional media_type/genre/year/language."""
    if not query or not str(query).strip():
        return []
    q_str = str(query).strip().lower()
    items = []
    seen_ids = set()
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            if title_only:
                search_condition = "(lower(json_extract(m.data, '$.title')) LIKE ? OR lower(json_extract(m.data, '$.name')) LIKE ?)"
                cond_params = [f"%{q_str}%", f"%{q_str}%"]
            else:
                search_condition = """(
                    lower(json_extract(m.data, '$.title')) LIKE ?
                    OR lower(json_extract(m.data, '$.name')) LIKE ?
                    OR lower(json_extract(m.data, '$.description')) LIKE ?
                    OR lower(json_extract(m.data, '$.overview')) LIKE ?
                    OR lower(json_extract(m.data, '$.genres')) LIKE ?
                )"""
                cond_params = [f"%{q_str}%"] * 5

            effective_genre = genre
            if (not effective_genre or effective_genre in ("All", "All Genres", "all")) and language and language not in ("All", "All Languages", "all"):
                effective_genre = language

            if effective_genre and str(effective_genre) not in ("All", "All Genres", "all"):
                sql = f"""
                    SELECT m.data
                    FROM item_genres ig
                    JOIN metadata_cache m ON ig.item_id = m.id
                    WHERE lower(ig.genre) = lower(?)
                      AND {search_condition}
                """
                params = [effective_genre.strip()] + cond_params
                if language and language not in ("All", "All Languages", "all") and language.strip().lower() != effective_genre.strip().lower():
                    sql += " AND m.id IN (SELECT item_id FROM item_genres WHERE lower(genre) = lower(?))"
                    params.append(str(language).strip())
            else:
                sql = f"""
                    SELECT m.data
                    FROM metadata_cache m
                    WHERE {search_condition}
                """
                params = list(cond_params)
                if language and language not in ("All", "All Languages", "all"):
                    sql += " AND m.id IN (SELECT item_id FROM item_genres WHERE lower(genre) = lower(?))"
                    params.append(str(language).strip())

            if media_type and media_type not in ("all", "discover"):
                mt = str(media_type).lower()
                if mt == "movie":
                    sql += " AND (m.media_type IN ('movie', 'collections') OR m.media_type IS NULL OR m.media_type = '')"
                elif mt == "series":
                    sql += " AND (m.media_type IN ('series', 'tv', 'show') OR m.media_type IS NULL OR m.media_type = '')"
                elif mt == "anime":
                    sql += " AND (m.media_type = 'anime' OR m.media_type IS NULL OR m.media_type = '')"
                else:
                    sql += " AND m.media_type = ?"
                    params.append(mt)

            if year and str(year) not in ("All Years", "All", "all", "None"):
                yr_str = str(year).strip()
                sql += " AND (json_extract(m.data, '$.year') LIKE ?)"
                params.append(f"{yr_str}%")

            if sort_mode == "Recent":
                sql += """
                    ORDER BY 
                        CASE WHEN lower(json_extract(m.data, '$.title')) LIKE ? OR lower(json_extract(m.data, '$.name')) LIKE ? THEN 0 ELSE 1 END,
                        COALESCE(json_extract(m.data, '$.year'), 0) DESC,
                        COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                        m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            else:
                sql += """
                    ORDER BY 
                        CASE WHEN lower(json_extract(m.data, '$.title')) LIKE ? OR lower(json_extract(m.data, '$.name')) LIKE ? THEN 0 ELSE 1 END,
                        COALESCE(json_extract(m.data, '$.rating'), json_extract(m.data, '$.imdbRating'), 0) DESC,
                        m.updated_at DESC
                    LIMIT ? OFFSET ?
                """
            params.extend([f"%{q_str}%", f"%{q_str}%", limit, offset])
            cursor.execute(sql, params)
            for row in cursor.fetchall():
                if not row or not row[0]:
                    continue
                try:
                    it = json.loads(row[0])
                    if isinstance(it, dict):
                        iid = it.get("id") or it.get("imdb_id")
                        if iid and iid not in seen_ids:
                            title = str(it.get("title") or it.get("name") or "").lower()
                            if title_only:
                                if q_str in title:
                                    seen_ids.add(iid)
                                    items.append(it)
                            else:
                                desc = str(it.get("description") or it.get("overview") or "").lower()
                                genres = str(it.get("genres") or "").lower()
                                if q_str in title or q_str in desc or q_str in genres:
                                    seen_ids.add(iid)
                                    items.append(it)
                except Exception:
                    pass
    except Exception as e:
        print(f"Error fetching cached items by search: {e}")
    if items:
        hydrate_items_from_metadata(items, media_type=media_type)
    return items

def get_stored_years(media_type=None):
    """Return distinct 4-digit release years stored in item_genres (not exceeding current year)."""
    years = set()
    try:
        import datetime
        current_year = datetime.datetime.now().year
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            query = "SELECT DISTINCT year FROM item_genres WHERE year IS NOT NULL AND length(year) = 4 AND year GLOB '[1-2][0-9][0-9][0-9]' AND CAST(year AS INTEGER) <= ?"
            params = [current_year]
            if media_type and media_type not in ("all", "discover"):
                if str(media_type).lower() in ("movie_series", "movies_series", "movie+series", "movies+series"):
                    query += " AND media_type IN ('movie', 'series')"
                else:
                    query += " AND media_type = ?"
                    params.append(str(media_type))
            cursor.execute(query, tuple(params))
            for row in cursor.fetchall():
                if row and row[0]:
                    y = row[0].strip()
                    if y.isdigit() and len(y) == 4 and int(y) <= current_year:
                        years.add(y)
    except Exception as e:
        print(f"Error reading stored years: {e}")
    return sorted(list(years), reverse=True)

_DROPDOWN_CACHE_LOCK = threading.Lock()
_GENRES_CACHE = {}
_SORT_OPTIONS_CACHE = {}
_YEARS_CACHE = {}

def invalidate_dropdown_caches():
    with _DROPDOWN_CACHE_LOCK:
        _GENRES_CACHE.clear()
        _SORT_OPTIONS_CACHE.clear()
        _YEARS_CACHE.clear()

def get_available_years(media_type=None):
    """Return distinct release years (stored + recent years) in descending order, capped at current year."""
    mt = (media_type or "all").lower()
    with _DROPDOWN_CACHE_LOCK:
        if mt in _YEARS_CACHE:
            return list(_YEARS_CACHE[mt])
    import datetime
    current_year = datetime.datetime.now().year
    stored_years = set(get_stored_years(media_type=None if mt in ("all", "discover") else mt))
    stored_years = {y for y in stored_years if y.isdigit() and int(y) <= current_year}
    for y in range(current_year, 2014, -1):
        stored_years.add(str(y))
    res = sorted(list(stored_years), reverse=True)
    with _DROPDOWN_CACHE_LOCK:
        _YEARS_CACHE[mt] = list(res)
    return res

def get_available_sort_options(media_type=None):
    """Return options for the Sort/Year dropdown: Popular, Featured, Recent, followed by years."""
    mt = (media_type or "all").lower()
    with _DROPDOWN_CACHE_LOCK:
        if mt in _SORT_OPTIONS_CACHE:
            return list(_SORT_OPTIONS_CACHE[mt])
    opts = ["Popular", "Featured", "Recent"] + get_available_years(media_type)
    with _DROPDOWN_CACHE_LOCK:
        _SORT_OPTIONS_CACHE[mt] = list(opts)
    return opts

def get_available_languages():
    """Return canonical languages present in the local metadata DB.

    Falls back to the canonical language set so the language filter is always
    usable, even before any item metadata has been cached locally.
    """
    languages = set(ALL_LANGUAGE_GENRES)

    def add_language(value):
        if isinstance(value, dict):
            value = value.get("name") or value.get("id")
        if isinstance(value, list):
            for part in value:
                add_language(part)
            return
        if not isinstance(value, str):
            return
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            normalized = normalize_imdb_genre(part)
            if normalized is None and "-" in part:
                normalized = normalize_imdb_genre(part.split("-", 1)[0])
            if normalized and is_language_genre(normalized):
                languages.add(normalized)

    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT genre FROM item_genres WHERE genre != ''")
            for (genre,) in cursor.fetchall():
                add_language(genre)

            # English is intentionally not duplicated into item_genres by the
            # metadata writer, so also read language fields from cached items.
            cursor.execute("SELECT DISTINCT json_extract(data, '$.language') FROM metadata_cache WHERE json_valid(data)")
            for (language,) in cursor.fetchall():
                add_language(language)
    except Exception as e:
        print(f"Error reading available languages: {e}")

    return sorted(languages, key=lambda language: (language.lower() != "english", language.lower()))

def get_available_genres(media_type=None, include_years=False):
    """
    Get strict canonical narrative IMDb genres for media_type ('movie', 'series', 'anime', or 'all').
    Languages are excluded and provided separately via get_available_languages().
    """
    mt = (media_type or "all").lower()
    if mt == "anime":
        genres = list(DEFAULT_ANIME_GENRES)
    elif mt in ("series", "tvshow"):
        genres = list(IMDB_SERIES_GENRES)
    elif mt == "movie":
        genres = list(IMDB_MOVIE_GENRES)
    else:  # all / discover
        genres = sorted(list(set(IMDB_MOVIE_GENRES + IMDB_SERIES_GENRES)))

    sorted_genres = sorted(list(set(genres)), key=lambda s: s.lower())

    if include_years:
        return sorted_genres, get_available_years(media_type)
    return sorted_genres

def get_cached_streams(cache_key, max_age_hours=24, return_age=False):
    if not cache_key:
        return (None, None) if return_age else None
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data, updated_at FROM stream_cache WHERE cache_key = ?", (str(cache_key),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[1]
                try:
                    age = max(0.0, time.time() - float(updated_at)) if updated_at is not None else float("inf")
                except (ValueError, TypeError):
                    age = float("inf")
                if age / 3600 < max_age_hours:
                    raw_streams = json.loads(row[0])
                    if isinstance(raw_streams, list):
                        clean = []
                        has_warning = False
                        for s in raw_streams:
                            if isinstance(s, dict) and updated_at is not None:
                                s.setdefault("saved_at", updated_at)
                            s_str = str(s).lower()
                            if "ratelimit" in s_str or "muitas requisi" in s_str or "aguarde 5 minutos" in s_str or "(banned)" in s_str:
                                has_warning = True
                                continue
                            clean.append(s)
                        if has_warning:
                            if clean:
                                cursor.execute("UPDATE stream_cache SET data = ? WHERE cache_key = ?", (json.dumps(clean), str(cache_key)))
                            else:
                                cursor.execute("DELETE FROM stream_cache WHERE cache_key = ?", (str(cache_key),))
                            conn.commit()
                            res = clean if clean else None
                            return (res, age) if return_age else res
                        return (clean, age) if return_age else clean
                    return (raw_streams, age) if return_age else raw_streams
    except Exception as e:
        print(f"Error reading stream cache: {e}")
    return (None, None) if return_age else None

def save_cached_streams(cache_key, streams):
    if not cache_key or streams is None:
        return
    try:
        now = time.time()
        for s in streams:
            if isinstance(s, dict):
                s.setdefault("saved_at", now)
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO stream_cache (cache_key, data, updated_at) VALUES (?, ?, ?)",
                (str(cache_key), json.dumps(streams), now)
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
                # Invalidate stale entries without user_agent or non-android/expired googlevideo streams
                if "googlevideo.com" in row[0].lower():
                    if not ua:
                        return None
                    try:
                        import urllib.parse
                        parsed = urllib.parse.urlparse(row[0])
                        qs = urllib.parse.parse_qs(parsed.query)
                        exp = qs.get("expire", [None])[0]
                        if exp and float(exp) <= time.time():
                            return None
                        cver = qs.get("cver", [""])[0]
                        if "android" not in cver.lower():
                            return None
                    except Exception:
                        pass
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
    found_data = None
    with _MEM_CATALOG_LOCK:
        if cache_key in _MEM_CATALOG_CACHE:
            data, ts = _MEM_CATALOG_CACHE[cache_key]
            if (now - ts) / 3600 < max_age_hours:
                found_data = data
            else:
                del _MEM_CATALOG_CACHE[cache_key]

    if found_data is not None:
        if is_adult_content_hidden():
            from . import api
            return [it for it in found_data if not api.is_adult_item(it)]
        return found_data

    # 2. SQLite cache check
    try:
        raw_json = None
        updated_at = None
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data, updated_at FROM catalog_cache WHERE cache_key = ?", (str(cache_key),))
            row = cursor.fetchone()
            if row and row[0]:
                updated_at = row[1]
                if (now - updated_at) / 3600 < max_age_hours:
                    raw_json = row[0]

        if raw_json:
            data = json.loads(raw_json)
            if not data:
                return None
            try:
                hydrate_items_from_metadata(data)
            except Exception:
                pass
            with _MEM_CATALOG_LOCK:
                while len(_MEM_CATALOG_CACHE) >= 500:
                    _MEM_CATALOG_CACHE.pop(next(iter(_MEM_CATALOG_CACHE)), None)
                _MEM_CATALOG_CACHE[cache_key] = (data, updated_at)
            if is_adult_content_hidden():
                from . import api
                return [it for it in data if not api.is_adult_item(it)]
            return data
    except Exception as e:
        print(f"Error reading catalog cache: {e}")
    return None

def save_cached_catalog(cache_key, items):
    if not cache_key or not items:
        return
    if is_adult_content_hidden():
        from . import api
        items = [it for it in items if not api.is_adult_item(it)]

    # Pre-enrich items with any existing metadata from metadata_cache
    try:
        hydrate_items_from_metadata(items)
    except Exception:
        pass

    now = time.time()
    with _MEM_CATALOG_LOCK:
        while len(_MEM_CATALOG_CACHE) >= 500:
            _MEM_CATALOG_CACHE.pop(next(iter(_MEM_CATALOG_CACHE)), None)
        _MEM_CATALOG_CACHE[cache_key] = (items, now)

    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            # Index items and genres into metadata_cache and item_genres for instant DB genre filtering
            try:
                meta_batch = []
                genre_batch = []
                enriched_count = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    iid = it.get("id") or it.get("imdb_id")
                    if not iid:
                        continue
                    mtype = it.get("type") or "movie"
                    clean_yr = extract_clean_year(it)
                    if clean_yr and not it.get("year"):
                        it["year"] = clean_yr
                    genres = _extract_metadata_genres(it, mtype)

                    # If this catalog item lacks genres or year, check if metadata_cache already has rich data
                    if not genres or not clean_yr:
                        try:
                            candidates = [str(iid)]
                            if it.get("imdb_id") and str(it["imdb_id"]) not in candidates:
                                candidates.append(str(it["imdb_id"]))
                            for aid in (it.get("alias_ids") or []):
                                if str(aid) not in candidates:
                                    candidates.append(str(aid))

                            for cid in candidates:
                                cur = cursor.execute("SELECT data FROM metadata_cache WHERE id = ?", (cid,))
                                row = cur.fetchone()
                                if row and row[0]:
                                    d = json.loads(row[0])
                                    item_enriched = False
                                    if not genres:
                                        genres = _extract_metadata_genres(d, mtype)
                                    if genres:
                                        it["genres"] = genres
                                        item_enriched = True
                                    if not clean_yr:
                                        d_yr = extract_clean_year(d)
                                        if d_yr:
                                            clean_yr = d_yr
                                            it["year"] = clean_yr
                                            item_enriched = True
                                    desc = d.get("description") or d.get("overview")
                                    if desc and not it.get("description"):
                                        it["description"] = desc
                                        item_enriched = True
                                    if not it.get("imdbRating") and d.get("imdbRating"):
                                        it["imdbRating"] = str(d["imdbRating"])
                                    if not it.get("runtime") and d.get("runtime"):
                                        it["runtime"] = str(d["runtime"])
                                    if not it.get("background") and d.get("background"):
                                        it["background"] = d["background"]
                                    if not it.get("cast") and d.get("cast"):
                                        it["cast"] = list(d["cast"])
                                    if not it.get("videos") and d.get("videos"):
                                        it["videos"] = d["videos"]
                                    if item_enriched:
                                        enriched_count += 1
                                    break
                        except Exception:
                            pass

                    ids_to_batch = [str(iid)]
                    if it.get("imdb_id") and str(it["imdb_id"]) not in ids_to_batch:
                        ids_to_batch.append(str(it["imdb_id"]))
                    for alias_id in it.get("alias_ids") or []:
                        if alias_id and str(alias_id) not in ids_to_batch:
                            ids_to_batch.append(str(alias_id))

                    for bid in ids_to_batch:
                        meta_batch.append((bid, str(mtype), json.dumps(it), now))
                        for norm in genres:
                            genre_batch.append((bid, str(mtype), norm, clean_yr))
                        if not genres and clean_yr:
                            genre_batch.append((bid, str(mtype), "", clean_yr))

                if meta_batch:
                    cursor.executemany("INSERT OR REPLACE INTO metadata_cache VALUES (?, ?, ?, ?)", meta_batch)
                if genre_batch:
                    cursor.executemany("""
                        INSERT INTO item_genres (item_id, media_type, genre, year)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(item_id, genre) DO UPDATE SET
                            year = CASE WHEN excluded.year != '' THEN excluded.year ELSE item_genres.year END,
                            media_type = excluded.media_type
                    """, genre_batch)
                    invalidate_dropdown_caches()
                    _notify_genre_listeners()

                # Save fully enriched items JSON into catalog_cache
                cursor.execute(
                    "INSERT OR REPLACE INTO catalog_cache (cache_key, data, updated_at) VALUES (?, ?, ?)",
                    (str(cache_key), json.dumps(items), now)
                )

                if enriched_count > 0:
                    print(f"[METADATA-HYDRATE] Enriched {enriched_count} items from metadata_cache for catalog '{cache_key}'")
            except Exception:
                pass
            conn.commit()
    except Exception as e:
        print(f"Error saving catalog cache: {e}")

def clear_all_cached_catalogs():
    with _MEM_CATALOG_LOCK:
        _MEM_CATALOG_CACHE.clear()
    try:
        with _cache_db_lock:
            conn = _get_cache_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM catalog_cache")
            conn.commit()
    except Exception as e:
        print(f"Error clearing all cached catalogs: {e}")



