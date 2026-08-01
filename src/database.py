import json
import os
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

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "popcorn-box"
else:
    BASE_DIR = Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box"

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
    }
]

def _ensure_db():
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True)
    if not DB_FILE.exists():
        with open(DB_FILE, "w") as f:
            json.dump({"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}, f, indent=4)

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
            if "downloads" not in data:
                data["downloads"] = []
            if "settings" not in data:
                data["settings"] = {}
            if "addons" not in data:
                data["addons"] = DEFAULT_ADDONS
            else:
                # Ensure all default/bundled addons are present in the user database
                migrated = False
                for default_addon in DEFAULT_ADDONS:
                    found = False
                    for a in data["addons"]:
                        if a.get("id") == default_addon["id"]:
                            found = True
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

def _write_db(data):
    global _json_cache, _json_cache_valid
    if _db_corrupted:
        print("Database read failed previously. Refusing to write to avoid overwriting with defaults.")
        return
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
        except Exception as e:
            _json_cache_valid = False
            print(f"Error writing database: {e}")

# --- Favorites ---

def get_favorites():
    return _read_db().get("favorites", [])

def add_favorite(item):
    db = _read_db()
    if not any(f.get("id") == item.get("id") for f in db.get("favorites", [])):
        db.setdefault("favorites", []).insert(0, item)
        _write_db(db)

def remove_favorite(item_id):
    db = _read_db()
    db["favorites"] = [f for f in db.get("favorites", []) if f.get("id") != item_id and f.get("imdb_id") != item_id]
    _write_db(db)

def is_favorite(item_id):
    return any(f.get("id") == item_id or f.get("imdb_id") == item_id for f in _read_db().get("favorites", []))

# --- Watched ---

def get_watched():
    return _read_db().get("watched", [])

def add_watched(item):
    db = _read_db()
    watched = db.setdefault("watched", [])
    item_id = item.get("id") or item.get("imdb_id")
    watched = [w for w in watched if w.get("id") != item_id and w.get("imdb_id") != item_id]
    watched.insert(0, item)
    db["watched"] = watched
    _write_db(db)

def remove_watched(item_id):
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
    db = _read_db()
    db["history"] = []
    _write_db(db)

def remove_history(item_id):
    db = _read_db()
    db["history"] = [h for h in db.get("history", []) if h.get("id") != item_id and h.get("imdb_id") != item_id]
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
    db = _read_db()
    settings = db.setdefault("settings", {})
    settings[key] = value
    _write_db(db)

# --- Progress ---

_progress_buffer = {}
_progress_last_flush = 0
_PROGRESS_FLUSH_INTERVAL = 10  # seconds

def save_progress(key, position):
    """Buffer progress in memory, flush to disk at most every 10 seconds."""
    global _progress_last_flush
    import time as _time
    _progress_buffer[key] = position
    now = _time.time()
    if now - _progress_last_flush >= _PROGRESS_FLUSH_INTERVAL:
        flush_progress()

def flush_progress():
    """Write all buffered progress to disk immediately."""
    global _progress_last_flush
    import time as _time
    if not _progress_buffer:
        return
    db = _read_db()
    progress = db.setdefault("progress", {})
    progress.update(_progress_buffer)
    _write_db(db)
    _progress_last_flush = _time.time()

def get_progress(key):
    # Check in-memory buffer first (most recent), then disk
    if key in _progress_buffer:
        return _progress_buffer[key]
    return _read_db().get("progress", {}).get(key, 0)


# --- Addons ---

def get_addons():
    return _read_db().get("addons", [])

def add_addon(addon):
    db = _read_db()
    addons = db.setdefault("addons", [])
    # Remove existing addon with same ID or manifest_url
    addons = [a for a in addons if a.get("id") != addon.get("id") and a.get("manifest_url") != addon.get("manifest_url")]
    addons.append(addon)
    db["addons"] = addons
    _write_db(db)

def remove_addon(addon_id):
    db = _read_db()
    db["addons"] = [a for a in db.get("addons", []) if a.get("id") != addon_id]
    _write_db(db)

def set_addon_enabled(addon_id, enabled):
    db = _read_db()
    for a in db.get("addons", []):
        if a.get("id") == addon_id:
            a["enabled"] = enabled
    _write_db(db)

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
                return json.loads(row[0])
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


