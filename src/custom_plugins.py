"""
Custom Drop-in Python Plugins Support for PopcornBox.
Provides a safe, decoupled architecture for loading user-supplied Python scrapers
from the local user directory without bundling proprietary or legal-risk scrapers
in the core application.
"""

import os
import sys
import json
import logging
import inspect
import shutil
import subprocess
import threading
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from . import database

logger = logging.getLogger("PopcornBox.CustomPlugins")

_LOAD_LOCK = threading.Lock()
_LOADED_SCRAPERS = {}  # {scraper_id: scraper_instance}
_LOADED_MODULES = {}   # {filename: module}
_SCRAPERS_LOADED = False
_LOADED_BASE_DIR = None
_LOADED_COUNT = 0
_PRELOAD_INFLIGHT = False

RUNNER_FILENAME = "custom_plugins_runner.py"


def get_runner_path() -> str:
    """Return the absolute path to the isolated custom_plugins_runner.py script."""
    module_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(module_dir, RUNNER_FILENAME)
    if os.path.exists(candidate):
        return candidate

    alt_paths = [
        os.path.join("/app/share/popcorn-box/popcornbox", RUNNER_FILENAME),
        os.path.join("/usr/share/popcorn-box/popcornbox", RUNNER_FILENAME),
        os.path.join("/usr/local/share/popcorn-box/popcornbox", RUNNER_FILENAME),
        os.path.join("/app/share/popcorn-box/cine", RUNNER_FILENAME),
        os.path.join("/usr/share/popcorn-box/cine", RUNNER_FILENAME),
        os.path.join("/usr/local/share/popcorn-box/cine", RUNNER_FILENAME),
    ]
    for p in alt_paths:
        if os.path.exists(p):
            return p

    return candidate

README_CONTENT = """# PopcornBox Custom Plugins Directory

Put standalone Python (.py) scraper files in this folder. PopcornBox discovers
them automatically and uses them for streams and/or browsable catalogs.
Files whose names start with "_" or "." are ignored.

Each plugin can be switched on/off in Preferences > Custom Plugins, and its
Stream and Catalog roles can be toggled separately.


## 1. Rules every plugin must follow

- ONE FILE, FULLY STANDALONE. Import only the Python standard library.
  Never import sibling files in this folder, PopcornBox modules (src, api,
  tmdb_helper, ...) or third-party packages. Copy any helper you need into
  the file itself.
- Define a class named `Scraper` (shown below).
- Never call sys.exit(), never block forever: every network call needs a
  timeout (5-15 s). Plugins run in a separate process with a time limit.
- No side effects at import time other than defining constants/classes
  (no network requests while the file is being imported).
- Never ask for, store or send user credentials.


## 2. The plugin contract

class Scraper:
    id = "custom.example"          # unique, starts with "custom."
    name = "Example"               # shown in the UI
    version = "1.0.0"
    description = "Movies and series from example.com"
    author = "Your name"
    media_types = ["movie", "series"]
    capabilities = ["stream", "catalog"]   # or just ["stream"] / ["catalog"]
    lookup = "id"        # ONLY if get_streams looks titles up strictly by
                         # IMDb/TMDB ID. Such streams are listed right after
                         # the opened catalog's own streams, above Stremio
                         # addons / Nuvio plugins. Leave it out otherwise.

    # Browsable sections (only if "catalog" is a capability). Mirror the
    # website's real navigation menu. Offer "genres"/"years" ONLY for
    # sections where the site really has genre/year pages; list the site's
    # own values. Leave them out otherwise.
    catalog_sections = [
        {"id": "latest-movies", "name": "All Movies", "type": "movie",
         "genres": ["Action", "Comedy"], "years": ["2026", "2025", "2011-2015"],
         "supports_pagination": True},
        {"id": "netflix", "name": "Netflix", "type": "series"},
        {"id": "adult", "name": "18+", "type": "movie", "is_adult": True},
    ]

    def get_catalog_items(self, catalog_id, media_type="movie", genre=None,
                          page=1, query=None, limit=50, year=None,
                          extra_filters=None, **kwargs):
        # Return a list of catalog items (format in section 3).
        # query = search text (search the site); page starts at 1.
        # genre / year come from the section's own lists; ignore values the
        # section does not offer instead of guessing.
        return []

    def get_streams(self, imdb_id, media_type="movie", season=None,
                    episode=None, title=None, year=None, tmdb_id=None,
                    **kwargs):
        # imdb_id is EITHER a real IMDb ID ("tt1234567") OR one of this
        # plugin's own catalog IDs ("example:https://example.com/post/").
        # For series, season and episode are integers.
        # Return a list of streams (format in section 3).
        return []


## 3. Data formats

Catalog item (from get_catalog_items):
    {
        "id": "example:https://example.com/some-movie-2025/",
        "title": "Some Movie",            # clean title, no release tags
        "year": "2025",
        "type": "movie",                  # or "series"
        "medium_cover_image": "https://.../poster.jpg",
        "background": "https://.../backdrop.jpg",   # optional
        "genres": ["Action"],                       # optional
        "rating": "7.1",                            # optional
    }
  The "id" MUST be "<prefix>:<page url>" where <prefix> is the plugin id
  without "custom." (or the plugin name, lowercase, no spaces). That is how
  PopcornBox sends the card back to this plugin's get_streams().

Stream (from get_streams):
    {
        "name": "Example 1080p HubCloud",       # provider + quality + server
        "title": "Some Movie (2025) 1080p | 2.1 GB",
        "url": "https://cdn.example.com/file.mkv",   # direct HTTP/HLS link
        # or "infoHash": "<40 hex chars>"  /  "magnet": "magnet:?xt=..."
        "quality": "1080p",                # 2160p / 1080p / 720p / 480p
        "size": "2.1 GB",                  # optional
        "headers": {"Referer": "https://example.com/"},  # if the CDN needs them
        "is_hls": False,                   # True for .m3u8
        "subtitles": [],                   # optional
    }
  Always set "quality" from the site's own label for THAT file (the button
  text or the heading above it, e.g. "720p Download Links [1.4GB]"). Never
  default every link to one quality.


## 4. Accuracy and safety (most important)

A wrong match can play an unrelated or even adult film in front of a family.
When in doubt, return NOTHING rather than a guess.

- IDs ONLY. A plugin may serve streams for a title that is not its own
  catalog card only when it can PROVE the match by IMDb/TMDB ID: e.g. the
  post page links imdb.com/title/<the requested imdb_id>. Reject pages that
  list several different IMDb IDs (collections).
- Never pick a stream source by title text, title + year, or title + season.
  Real example: "Love" (2016 series) matched "Love" (2015), an explicit film,
  because title and year looked close. Many films share a name.
- If the website has no IMDb/TMDB IDs, the plugin serves ONLY its own catalog
  cards (ids "<prefix>:<url>") and returns [] for everything else.
- Stream list order in PopcornBox: (1) the opened catalog card's own plugin,
  (2) plugins with lookup = "id", (3) Stremio addons, Nuvio plugins and the
  rest. A wrong match from a title-searching source therefore sinks down.
- Exact title matching is still useful for the catalog SEARCH box, never for
  choosing which film's streams to play.
- Adult content: never return adult pages (18+, Ullu, HotShots, erotic,
  XXX, ...) for a normal title. Check the post name, URL and the post's own
  category tags, not the site menu. Mark adult catalog sections with
  "is_adult": True.
- Sites change domains often. Keep the base URL in one constant and follow
  redirects; if the site publishes its current domain somewhere, read it.


## 5. Testing

- In PopcornBox: Preferences > Custom Plugins > Refresh, then press "Test"
  on your plugin. Open one of its catalog cards and check the streams.
- From a terminal (inside this folder):
      python3 -c "import importlib.util as u; s=u.spec_from_file_location('p','example_scraper.py'); m=u.module_from_spec(s); s.loader.exec_module(m); sc=m.Scraper(); print(sc.get_catalog_items(sc.catalog_sections[0]['id'])[:2]); print(sc.get_streams('tt1375666', 'movie', title='Inception', year='2010'))"
- Check: correct qualities, no wrong films (try a short common title such as
  "Love" with a year), no adult results for normal titles, page 2 works.


## 6. AI prompt: create a plugin for a website

Copy everything between the lines into an AI assistant, replacing the
<...> placeholders. Paste this whole README above it so the AI has the
contract, or attach the file.

------------------------------------------------------------------------
You are writing a PopcornBox custom scraper plugin: one standalone Python 3
file that follows the "PopcornBox Custom Plugins" README I pasted above,
exactly (class Scraper, attributes, method signatures, data formats).

Website: <https://example.com>
Plugin id / name: <custom.example> / <Example>
Capabilities wanted: <stream and catalog | stream only | catalog only>
Content: <movies / series / both>, language(s): <e.g. Hindi, English>

Do this:
1. Study the website first: its navigation menu (categories, OTT platforms,
   languages, quality/size lists), genre and year pages, the search URL, the
   listing-page markup, the post-page markup and where the download/watch
   links are. Fetch real pages; do not guess URLs.
2. catalog_sections: mirror the real menu. Add "genres"/"years" only to
   sections that have real genre/year pages, using the site's own values.
   Support pagination (/page/N/ or the site's scheme) and search.
3. get_catalog_items: return items in the documented format with clean
   titles, years, posters, type, and id = "<prefix>:<post url>".
4. get_streams:
   - If imdb_id is this plugin's own "<prefix>:<url>", open that post.
   - Otherwise (imdb_id is "tt..."), search the site by title and accept a
     result ONLY if the post's own IMDb link equals imdb_id. Never accept a
     result by title, year or season text alone. If the site shows no
     IMDb/TMDB IDs at all, return [] for anything that is not this plugin's
     own catalog id.
   - Follow the post's download/watch buttons through any link lockers or
     redirectors to the final file hosts, and extract direct playable
     URLs (HTTP, HLS) or torrents. Implement only hosts that work without
     a captcha or login; skip the rest quietly.
   - Label every stream with the quality and size shown for THAT file.
5. Robustness: stdlib only (urllib, re, json, html, ...), a browser-like
   User-Agent, timeouts on every request, try/except around each source so
   one broken mirror does not break the rest, and the base URL in a single
   constant.
6. Give me the complete file, then a short list of the site URLs you
   verified and anything that could not be supported.
------------------------------------------------------------------------
"""


class BaseCustomScraper:
    """Base class that custom plugins can inherit from."""
    id: str = "custom.base"
    name: str = "Base Custom Scraper"
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    media_types: list = ["movie", "series"]
    # Optional browsable sections. Each dict: {"id", "name", "type": "movie"|"series",
    # "genres": [...], "years": [...], "supports_pagination": bool}. Genre/year
    # filters are only offered for sections that list their options.
    catalog_sections: list = []
    # Optional: "id" if get_streams looks titles up strictly by IMDb/TMDB ID (never by
    # title text). Such streams are listed above title-searched sources.
    lookup: str = None
    # Optional: restrict what the plugin is used for, e.g. ["stream"] or ["catalog"].
    # When omitted, capabilities are detected from get_streams / catalog_sections.
    capabilities: list = None

    def get_streams(self, imdb_id, media_type="movie", season=None, episode=None, title=None, year=None, tmdb_id=None):
        """
        Fetch streams for the given item.
        Returns a list of dicts with keys:
          - 'name': str (e.g. 'YTS 1080p')
          - 'title': str (e.g. '1080p.BluRay | 👤 1420 | 1.85 GB')
          - 'infoHash': str (hex hash) OR 'magnet': str OR 'url': str
          - 'fileIdx': optional int
        """
        raise NotImplementedError

    def get_catalog_items(self, catalog_id, media_type="movie", genre=None, page=1, query=None):
        """
        Optional catalog fetch method. Returns a list of dicts:
        [
            {
                "id": "tt1234567",
                "title": "Movie Title",
                "year": "2024",
                "medium_cover_image": "https://...",
                "type": "movie"  # or "series"
            }
        ]
        """
        return []


def get_custom_plugins_dir() -> Path:
    """Return the Path to the directory where user-supplied .py plugins live."""
    base = getattr(database, "BASE_DIR", None)
    if not base:
        base = Path.home() / ".local/share/popcorn-box"
    plugins_dir = Path(base) / "custom_plugins"
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        readme = plugins_dir / "README.txt"
        if not readme.exists():
            readme.write_text(README_CONTENT, encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to create custom plugins directory {plugins_dir}: {e}")
    return plugins_dir


def is_custom_plugins_enabled() -> bool:
    """Return whether custom plugins are globally enabled."""
    return bool(database.get_setting("custom_plugins_enabled", True))


def set_custom_plugins_enabled(enabled: bool):
    """Enable or disable custom plugins globally."""
    database.set_setting("custom_plugins_enabled", bool(enabled))
    database.clear_all_cached_streams_async()


def is_scraper_enabled(scraper_id: str) -> bool:
    """Check if a specific custom scraper is enabled."""
    disabled = database.get_setting("disabled_custom_scrapers", [])
    if not isinstance(disabled, list):
        disabled = []
    return str(scraper_id) not in disabled


CAPABILITY_STREAM = "stream"
CAPABILITY_CATALOG = "catalog"
ALL_CAPABILITIES = (CAPABILITY_STREAM, CAPABILITY_CATALOG)


def get_scraper_capabilities(scraper) -> list[str]:
    """Return the capabilities ("stream", "catalog") a loaded scraper provides.

    An explicit `capabilities` attribute wins; otherwise a plugin is a stream
    source when it implements get_streams, and a catalog source when it declares
    non-empty catalog_sections and implements get_catalog_items.
    """
    declared = getattr(scraper, "capabilities", None)
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, (list, tuple, set)):
        wanted = {str(c).strip().lower() for c in declared}
        return [c for c in ALL_CAPABILITIES if c in wanted]

    caps = []
    get_streams = getattr(type(scraper), "get_streams", None)
    if callable(get_streams) and get_streams is not BaseCustomScraper.get_streams:
        caps.append(CAPABILITY_STREAM)
    sections = getattr(scraper, "catalog_sections", None)
    if sections and isinstance(sections, (list, tuple)) and callable(getattr(scraper, "get_catalog_items", None)):
        caps.append(CAPABILITY_CATALOG)
    return caps


def _disabled_capabilities() -> dict:
    value = database.get_setting("disabled_custom_scraper_capabilities", {})
    return value if isinstance(value, dict) else {}


def is_capability_enabled(scraper_id: str, capability: str) -> bool:
    """Check whether a scraper's stream or catalog role is switched on."""
    return capability not in (_disabled_capabilities().get(str(scraper_id)) or [])


def _supported_capabilities(scraper_id: str) -> list[str]:
    scraper = _LOADED_SCRAPERS.get(str(scraper_id))
    return get_scraper_capabilities(scraper) if scraper is not None else list(ALL_CAPABILITIES)


def _set_disabled_capabilities(scraper_id: str, disabled: set):
    all_disabled = dict(_disabled_capabilities())
    if disabled:
        all_disabled[str(scraper_id)] = sorted(disabled)
    else:
        all_disabled.pop(str(scraper_id), None)
    database.set_setting("disabled_custom_scraper_capabilities", all_disabled)


def toggle_capability(scraper_id: str, capability: str, enabled: bool) -> bool:
    """Enable or disable a scraper's stream or catalog role independently.

    Switching off the last capability a plugin supports disables the whole
    plugin instead (its capabilities are reset, so re-enabling the plugin
    brings every role back). Returns whether the plugin is still enabled.
    """
    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"Unknown plugin capability: {capability}")
    disabled = set(_disabled_capabilities().get(str(scraper_id)) or [])
    if enabled:
        disabled.discard(capability)
    else:
        disabled.add(capability)

    supported = set(_supported_capabilities(scraper_id))
    if supported and supported <= disabled:
        _set_disabled_capabilities(scraper_id, set())
        toggle_scraper(scraper_id, False)
        return False

    _set_disabled_capabilities(scraper_id, disabled)
    if capability == CAPABILITY_STREAM:
        database.clear_all_cached_streams_async()
    return is_scraper_enabled(scraper_id)


def _scraper_serves(scraper, capability: str) -> bool:
    s_id = str(getattr(scraper, "id", ""))
    return (
        is_scraper_enabled(s_id)
        and capability in get_scraper_capabilities(scraper)
        and is_capability_enabled(s_id, capability)
    )


def toggle_scraper(scraper_id: str, enabled: bool):
    """Enable or disable a specific custom scraper."""
    disabled = set(database.get_setting("disabled_custom_scrapers", []))
    if enabled:
        disabled.discard(str(scraper_id))
        # A plugin switched on with every role off would do nothing; restore them.
        supported = set(_supported_capabilities(scraper_id))
        if supported and supported <= set(_disabled_capabilities().get(str(scraper_id)) or []):
            _set_disabled_capabilities(scraper_id, set())
    else:
        disabled.add(str(scraper_id))
    database.set_setting("disabled_custom_scrapers", list(disabled))
    database.clear_all_cached_streams_async()


def _is_scrapers_cache_ready(cur_base_dir: str) -> bool:
    """Return True if scrapers for cur_base_dir are loaded and not externally cleared."""
    if not _SCRAPERS_LOADED or _LOADED_BASE_DIR != cur_base_dir:
        return False
    if _LOADED_COUNT > 0 and not _LOADED_SCRAPERS:
        return False
    return True


_LOADER_STATE_LOCK = threading.Lock()
_LOADER_THREAD = None
_LOADER_PENDING_FORCE = None  # None: no request queued; False: load; True: forced reload
_LOADER_CALLBACKS = []


_LOAD_LISTENERS = []


def add_load_listener(listener):
    """Call `listener(scrapers)` after every background (re)load, e.g. so the UI can
    rebuild catalog lists that were built before plugins finished loading."""
    if listener not in _LOAD_LISTENERS:
        _LOAD_LISTENERS.append(listener)


def _loader_worker():
    """Serve queued (re)load requests one at a time on the single loader thread."""
    global _LOADER_THREAD, _LOADER_PENDING_FORCE, _PRELOAD_INFLIGHT
    while True:
        with _LOADER_STATE_LOCK:
            force = _LOADER_PENDING_FORCE
            callbacks = list(_LOADER_CALLBACKS)
            _LOADER_PENDING_FORCE = None
            _LOADER_CALLBACKS.clear()
            if force is None:
                _LOADER_THREAD = None
                _PRELOAD_INFLIGHT = False
                return
        scrapers = []
        try:
            scrapers = load_all_custom_scrapers(force_reload=force)
            logger.info(f"Custom plugins {'reloaded' if force else 'loaded'}: {len(scrapers)} scraper(s)")
        except Exception as e:
            logger.warning(f"Background custom plugin {'reload' if force else 'load'} failed: {e}")
        for callback in callbacks + list(_LOAD_LISTENERS):
            try:
                callback(scrapers)
            except Exception as e:
                logger.debug(f"Custom plugin reload callback failed: {e}")


def reload_custom_scrapers_async(force: bool = True, callback=None):
    """
    (Re)load custom plugins on one dedicated background thread, never the
    caller's. Only one loader thread exists at a time: requests made while it
    runs are merged into a single follow-up pass (forced if any request was),
    and every callback runs on the loader thread after that pass with the
    loaded scrapers, so GTK callers must hop back with GLib.idle_add.
    """
    global _LOADER_THREAD, _LOADER_PENDING_FORCE, _PRELOAD_INFLIGHT
    with _LOADER_STATE_LOCK:
        _LOADER_PENDING_FORCE = bool(force) or bool(_LOADER_PENDING_FORCE)
        if callback is not None:
            _LOADER_CALLBACKS.append(callback)
        _PRELOAD_INFLIGHT = True
        if _LOADER_THREAD is not None:
            return
        _LOADER_THREAD = threading.Thread(target=_loader_worker, daemon=True, name="CustomPluginsLoader")
        _LOADER_THREAD.start()


def preload_custom_scrapers_async():
    """Preload custom scrapers on the background loader thread without blocking the UI."""
    cur_base_dir = str(getattr(database, "BASE_DIR", "") or "")
    if _is_scrapers_cache_ready(cur_base_dir) or _PRELOAD_INFLIGHT:
        return
    reload_custom_scrapers_async(force=False)


def wait_for_custom_scrapers(timeout: float = 30.0) -> list:
    """
    Return the loaded scrapers, letting the loader thread do any loading and
    waiting up to `timeout` for it. Background threads use this so plugin files
    are only ever probed and imported on the single loader thread.
    """
    cur_base_dir = str(getattr(database, "BASE_DIR", "") or "")
    if _is_scrapers_cache_ready(cur_base_dir):
        return list(_LOADED_SCRAPERS.values())
    done = threading.Event()
    reload_custom_scrapers_async(force=False, callback=lambda _scrapers: done.set())
    if not done.wait(timeout):
        logger.warning(f"Timed out after {timeout}s waiting for custom plugins to load")
    return list(_LOADED_SCRAPERS.values()) if _LOADED_BASE_DIR == cur_base_dir else []


def _get_scrapers_for_caller() -> list:
    """Return loaded scrapers; never block the GTK main thread on subprocess probes."""
    cur_base_dir = str(getattr(database, "BASE_DIR", "") or "")
    if _is_scrapers_cache_ready(cur_base_dir):
        return list(_LOADED_SCRAPERS.values())
    if database._is_testing():
        return load_all_custom_scrapers()
    if threading.current_thread() is threading.main_thread():
        preload_custom_scrapers_async()
        return list(_LOADED_SCRAPERS.values()) if _LOADED_BASE_DIR == cur_base_dir else []
    return wait_for_custom_scrapers()


def load_all_custom_scrapers(force_reload: bool = False):
    """
    Scans the custom plugins directory, imports all valid .py scraper files,
    and updates the internal registry.
    """
    global _LOADED_SCRAPERS, _LOADED_MODULES, _SCRAPERS_LOADED, _LOADED_BASE_DIR, _LOADED_COUNT
    # Serve a loaded registry without waiting on _LOAD_LOCK, so stream lookups
    # keep using the current plugins while a background reload is probing files.
    if not force_reload and _is_scrapers_cache_ready(str(getattr(database, "BASE_DIR", "") or "")):
        return list(_LOADED_SCRAPERS.values())
    with _LOAD_LOCK:
        cur_base_dir = str(getattr(database, "BASE_DIR", "") or "")
        if not force_reload and _is_scrapers_cache_ready(cur_base_dir):
            return list(_LOADED_SCRAPERS.values())

        plugins_dir = get_custom_plugins_dir()
        if not plugins_dir.exists():
            _LOADED_SCRAPERS = {}
            _LOADED_MODULES = {}
            _SCRAPERS_LOADED = True
            _LOADED_BASE_DIR = cur_base_dir
            _LOADED_COUNT = 0
            return []

        new_scrapers = {}
        new_modules = {}

        py_files = sorted(plugins_dir.glob("*.py"))
        for filepath in py_files:
            filename = filepath.name
            if filename.startswith("_") or filename.startswith("."):
                continue

            # Run isolated probe in subprocess first to protect against top-level exits/crashes
            runner_path = get_runner_path()
            if runner_path and os.path.exists(runner_path):
                try:
                    probe = subprocess.run(
                        [sys.executable, "-s", runner_path],
                        input=json.dumps({"action": "inspect", "file_path": str(filepath)}),
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                    )
                    if probe.returncode != 0:
                        logger.error(
                            f"Plugin probe failed for {filename} (exit code {probe.returncode}): {probe.stderr[:200]}"
                        )
                        continue
                except Exception as probe_err:
                    logger.error(f"Failed to probe custom plugin {filename}: {probe_err}")
                    continue

            module_name = f"popcornbox_custom_plugin_{filepath.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, filepath)
                if not spec or not spec.loader:
                    logger.warning(f"Could not create module spec for {filename}")
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                new_modules[filename] = module

                # Inspect module for scraper classes
                found = False
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        inspect.isclass(attr)
                        and attr is not BaseCustomScraper
                        and (
                            issubclass(attr, BaseCustomScraper)
                            or hasattr(attr, "get_streams")
                            or getattr(attr, "is_scraper", False)
                            or attr.__name__ == "Scraper"
                        )
                    ):
                        try:
                            instance = attr()
                            s_id = str(getattr(instance, "id", None) or f"custom.{filepath.stem}")
                            instance.id = s_id
                            instance._filename = filename
                            instance._file_path = str(filepath)
                            instance._class_name = attr_name
                            if not getattr(instance, "name", None):
                                instance.name = filepath.stem.replace("_", " ").title()
                            if not getattr(instance, "media_types", None):
                                instance.media_types = ["movie", "series"]
                            if not getattr(instance, "version", None):
                                instance.version = "1.0.0"

                            new_scrapers[s_id] = instance
                            found = True
                            logger.info(f"Loaded custom scraper '{instance.name}' ({s_id}) from {filename}")
                        except Exception as inst_err:
                            logger.error(f"Failed to instantiate scraper class {attr_name} in {filename}: {inst_err}")

                if not found:
                    logger.debug(f"No scraper class found in {filename}")

            except Exception as e:
                logger.error(f"Error loading custom plugin {filename}: {e}", exc_info=True)

        _LOADED_SCRAPERS = new_scrapers
        _LOADED_MODULES = new_modules
        _SCRAPERS_LOADED = True
        _LOADED_BASE_DIR = cur_base_dir
        _LOADED_COUNT = len(new_scrapers)
        return list(_LOADED_SCRAPERS.values())


def get_all_custom_scrapers() -> list[dict]:
    """
    Return a list of metadata dicts describing all loaded custom scrapers
    for UI presentation and management.
    """
    scrapers = load_all_custom_scrapers()
    results = []
    for s in scrapers:
        s_id = str(s.id)
        results.append({
            "id": s_id,
            "name": getattr(s, "name", s_id),
            "version": getattr(s, "version", "1.0.0"),
            "description": getattr(s, "description", ""),
            "author": getattr(s, "author", ""),
            "media_types": list(getattr(s, "media_types", ["movie", "series"])),
            "enabled": is_scraper_enabled(s_id),
            "capabilities": get_scraper_capabilities(s),
            "stream_enabled": is_capability_enabled(s_id, CAPABILITY_STREAM),
            "catalog_enabled": is_capability_enabled(s_id, CAPABILITY_CATALOG),
            "filename": getattr(s, "_filename", ""),
            "file_path": getattr(s, "_file_path", ""),
        })
    return results


def get_enabled_scrapers_for_type(media_type: str = "movie") -> list:
    """Return all active scraper instances supporting the specified media type."""
    if not is_custom_plugins_enabled():
        return []

    norm_type = "series" if media_type in ["series", "tv", "anime"] else "movie"
    all_scrapers = _get_scrapers_for_caller()
    enabled = []
    for s in all_scrapers:
        if not _scraper_serves(s, CAPABILITY_STREAM):
            continue
        m_types = [t.lower() for t in getattr(s, "media_types", ["movie", "series"])]
        if norm_type in m_types or "all" in m_types or media_type in m_types:
            enabled.append(s)
    return enabled


def execute_custom_scraper(
    scraper,
    imdb_id: str,
    media_type: str = "movie",
    season: int = None,
    episode: int = None,
    title: str = None,
    year: str = None,
    tmdb_id: str = None,
    timeout: float = 25.0
) -> list[dict]:
    """
    Safely executes a custom scraper's get_streams method with a timeout guard
    and formats returned items into PopcornBox stream dictionary objects.
    """
    scraper_name = getattr(scraper, "name", "Custom Plugin")
    scraper_id = getattr(scraper, "id", "custom.unknown")

    if not tmdb_id and imdb_id:
        try:
            from . import tmdb_helper
            resolved = tmdb_helper.resolve_to_tmdb_id(imdb_id, media_type, title, year=year)
            if resolved:
                tmdb_id = str(resolved).strip()
        except Exception:
            pass

    file_path = getattr(scraper, "_file_path", None)
    class_name = getattr(scraper, "_class_name", None)
    runner_path = get_runner_path()

    raw_streams = None

    # Preferred out-of-process execution for file-backed custom plugins
    if file_path and os.path.isfile(file_path) and runner_path and os.path.isfile(runner_path):
        payload = {
            "action": "get_streams",
            "file_path": str(file_path),
            "class_name": class_name,
            "kwargs": {
                "imdb_id": imdb_id,
                "media_type": media_type,
                "season": season,
                "episode": episode,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
            },
        }
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-s", runner_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(json.dumps(payload), timeout=timeout)
            if proc.returncode != 0:
                logger.warning(
                    f"Custom scraper '{scraper_name}' process exited with code {proc.returncode}. stderr: {stderr[:300]}"
                )
                raw_streams = []
            else:
                data = json.loads(stdout)
                if isinstance(data, dict) and "error" in data:
                    logger.warning(f"Custom scraper '{scraper_name}' error: {data['error']}")
                    raw_streams = []
                elif isinstance(data, list):
                    raw_streams = data
                else:
                    raw_streams = []
        except subprocess.TimeoutExpired:
            if proc:
                try:
                    proc.kill()
                    proc.communicate(timeout=1.0)
                except Exception:
                    pass
            logger.warning(f"Custom scraper '{scraper_name}' timed out after {timeout}s (subprocess terminated)")
            return []
        except Exception as e:
            logger.error(f"Error executing custom scraper '{scraper_name}' in subprocess: {e}")
            raw_streams = []

    # Fallback to in-process execution (e.g. for dynamic or mock in-memory scraper instances in tests)
    if raw_streams is None:
        def _call():
            sig = inspect.signature(scraper.get_streams)
            kwargs = {}
            params = sig.parameters
            if "imdb_id" in params:
                kwargs["imdb_id"] = imdb_id
            if "media_type" in params:
                kwargs["media_type"] = media_type
            if "season" in params:
                kwargs["season"] = season
            if "episode" in params:
                kwargs["episode"] = episode
            if "title" in params:
                kwargs["title"] = title
            if "year" in params:
                kwargs["year"] = year
            if "tmdb_id" in params:
                kwargs["tmdb_id"] = tmdb_id

            if not kwargs and len(params) >= 1:
                return scraper.get_streams(imdb_id)
            return scraper.get_streams(**kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                raw_streams = future.result(timeout=timeout) or []
            except TimeoutError:
                logger.warning(f"Custom scraper '{scraper_name}' timed out after {timeout}s")
                return []
            except Exception as e:
                logger.error(f"Custom scraper '{scraper_name}' raised an exception: {e}")
                return []

    if not isinstance(raw_streams, list):
        return []

    formatted_streams = []
    for s in raw_streams:
        if not isinstance(s, dict):
            continue

        info_hash = str(s.get("infoHash") or s.get("info_hash") or s.get("hash") or "").strip().lower()
        magnet = str(s.get("magnet") or s.get("url") if str(s.get("url", "")).startswith("magnet:") else "")
        url = str(s.get("url") or "")

        # A stream must have either a torrent hash/magnet or an HTTP video URL
        if not info_hash and not magnet and not url:
            continue

        strm_name = s.get("name") or scraper_name
        title_str = s.get("title") or strm_name

        card = {
            "name": strm_name,
            "title": title_str,
            "infoHash": info_hash,
            "magnet": magnet,
            "url": url,
            "fileIdx": s.get("fileIdx", s.get("file_index")),
            "addon_name": scraper_name,
            "addon_names": [scraper_name],
            "is_custom_plugin": True,
            "custom_plugin_id": scraper_id,
            # Plugins that look titles up strictly by IMDb/TMDB ID declare lookup = "id";
            # their streams are ranked above title-searched sources.
            "id_lookup": str(getattr(scraper, "lookup", "") or "").lower() == "id",
            "quality": s.get("quality"),
            "size": s.get("size"),
            "seeders": s.get("seeders", s.get("seeds", 0)),
            "behaviorHints": s.get("behaviorHints", {}),
            "is_hls": bool(s.get("is_hls", False)),
            "is_dash": bool(s.get("is_dash", False)),
        }
        if isinstance(s.get("headers"), dict) and s["headers"]:
            card["headers"] = dict(s["headers"])
        for opt_key in ("subtitles", "has_expiring_credentials", "created_at", "expires_at", "binge_group", "stream_type"):
            if s.get(opt_key) is not None:
                card[opt_key] = s[opt_key]
        formatted_streams.append(card)

    return formatted_streams


def test_custom_scraper(scraper_id: str) -> tuple[bool, int, str]:
    """
    Tests a custom scraper with a known media item (Inception or Game of Thrones).
    Returns (success: bool, count: int, message: str).
    """
    scrapers = {s.id: s for s in load_all_custom_scrapers()}
    scraper = scrapers.get(scraper_id)
    if not scraper:
        return False, 0, f"Scraper '{scraper_id}' not found"

    m_types = [t.lower() for t in getattr(scraper, "media_types", ["movie"])]
    is_series = "series" in m_types and "movie" not in m_types

    try:
        if is_series:
            # Test Game of Thrones S01E01
            streams = execute_custom_scraper(
                scraper,
                imdb_id="tt0944947",
                media_type="series",
                season=1,
                episode=1,
                title="Game of Thrones",
                year="2011",
                tmdb_id="1399",
                timeout=20.0
            )
        else:
            # Test Inception (2010)
            streams = execute_custom_scraper(
                scraper,
                imdb_id="tt1375666",
                media_type="movie",
                title="Inception",
                year="2010",
                tmdb_id="27205",
                timeout=20.0
            )

        count = len(streams)
        return True, count, f"Found {count} stream(s)"
    except Exception as e:
        return False, 0, str(e)


def import_plugin_file(src_path: str) -> tuple[bool, str]:
    """
    Copies a .py file from src_path into the custom plugins folder and reloads plugins.
    Returns (success: bool, message: str).
    """
    src = Path(src_path)
    if not src.is_file() or src.suffix.lower() != ".py":
        return False, "File must be a valid .py Python script."

    target_dir = get_custom_plugins_dir()
    target_path = target_dir / src.name

    try:
        shutil.copy2(src, target_path)
        load_all_custom_scrapers(force_reload=True)
        return True, f"Successfully imported '{src.name}'"
    except Exception as e:
        logger.error(f"Failed to import plugin file {src_path}: {e}")
        return False, str(e)


def delete_plugin_file(filename_or_id: str) -> tuple[bool, str]:
    """
    Removes a plugin file from the custom plugins directory.
    Returns (success: bool, message: str).
    """
    target_dir = get_custom_plugins_dir()
    
    # Try by exact filename
    target_file = target_dir / filename_or_id
    if not target_file.is_file():
        # Try matching by scraper id
        for s in load_all_custom_scrapers():
            if s.id == filename_or_id and getattr(s, "_filename", None):
                target_file = target_dir / s._filename
                break

    if not target_file.is_file():
        return False, f"Plugin file '{filename_or_id}' not found."

    try:
        target_file.unlink()
        load_all_custom_scrapers(force_reload=True)
        return True, f"Deleted '{target_file.name}'"
    except Exception as e:
        logger.error(f"Failed to delete plugin file {target_file}: {e}")
        return False, str(e)


def open_plugins_directory(parent_window=None):
    """Opens the custom plugins directory in the user's default desktop file manager."""
    plugins_dir = get_custom_plugins_dir()
    try:
        from gi.repository import Gio, Gtk
        gfile = Gio.File.new_for_path(str(plugins_dir))
        launcher = Gtk.FileLauncher.new(gfile)
        launcher.launch(parent_window, None, None)
        return
    except Exception:
        pass

    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(plugins_dir)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(plugins_dir)])
        elif sys.platform == "win32":
            os.startfile(str(plugins_dir))
    except Exception as e:
        logger.error(f"Could not open plugins directory: {e}")


def catalog_url(plugin_id: str) -> str:
    """Return the virtual catalog URI for a custom plugin."""
    return f"custom://catalog/{plugin_id}"


def plugin_id_from_catalog_url(raw_url: str) -> str:
    """Extract plugin ID from a custom://catalog/<id> URL."""
    if not isinstance(raw_url, str):
        return ""
    prefix = "custom://catalog/"
    if raw_url.startswith(prefix):
        return raw_url[len(prefix):].split("/")[0]
    return ""


def get_catalogs(media_type: str = "all") -> list[dict]:
    """
    Return browsable catalog sections declared by active custom plugins.
    Supports filtering by media_type ("all", "movie", "series").
    """
    if not is_custom_plugins_enabled():
        return []

    norm_type = str(media_type or "all").lower()
    catalogs = []
    seen = set()

    for scraper in _get_scrapers_for_caller():
        if not _scraper_serves(scraper, CAPABILITY_CATALOG):
            continue

        sections = getattr(scraper, "catalog_sections", None)
        if not sections or not isinstance(sections, (list, tuple)):
            continue

        s_id = str(scraper.id)
        s_name = getattr(scraper, "name", s_id)

        for sec in sections:
            if not isinstance(sec, dict):
                continue

            sec_id = str(sec.get("id") or "").strip()
            sec_name = str(sec.get("name") or sec_id).strip()
            sec_type = str(sec.get("type") or "movie").strip().lower()

            if not sec_id or not sec_name:
                continue

            # Check media_type match
            if norm_type not in ("all", "movie_series"):
                if norm_type in ("movie", "movies") and sec_type not in ("movie", "movies"):
                    continue
                if norm_type in ("series", "show", "shows", "tv") and sec_type not in ("series", "show", "shows", "tv"):
                    continue

            key = (s_id, sec_id, sec_type)
            if key in seen:
                continue
            seen.add(key)

            # Only advertise the filters a section actually declares, with the
            # plugin's own option values (e.g. the site's genre/year menus).
            genres = [str(g) for g in (sec.get("genres") or []) if str(g).strip()]
            years = [str(y) for y in (sec.get("years") or []) if str(y).strip()]
            extra = []
            extra_supported = []
            if genres:
                extra.append({"name": "genre", "isRequired": False, "options": genres})
                extra_supported.append("genre")
            if years:
                extra.append({"name": "year", "isRequired": False, "options": years})
                extra_supported.append("year")
            if sec.get("supports_pagination", True):
                extra_supported.append("skip")
            if sec.get("supports_search", True):
                extra_supported.append("search")
            catalogs.append({
                "addon_name": s_name,
                "manifest_url": catalog_url(s_id),
                "base_url": catalog_url(s_id),
                "catalog_id": sec_id,
                "catalog_name": sec_name,
                "display_name": f"{s_name} - {sec_name}",
                "genres": genres,
                "years": years,
                "extra": extra,
                "extraSupported": extra_supported,
                "type": sec_type,
                "supports_pagination": bool(sec.get("supports_pagination", True)),
                "_source": "custom_plugin",
                "custom_plugin_id": s_id,
            })

    return catalogs


def fetch_catalog_items(
    plugin_id: str,
    catalog_id: str,
    genre: str = None,
    page: int = 1,
    limit: int = 50,
    query: str = None,
    is_cancelled = None,
    timeout: float = 12.0,
    year: str = None,
    extra_filters: dict = None,
) -> list[dict]:
    """
    Fetch catalog or search items from a custom plugin.
    Runs with a timeout guard and returns standardized PopcornBox items.
    """
    if not is_custom_plugins_enabled() or not is_scraper_enabled(plugin_id):
        return []

    scrapers = {s.id: s for s in load_all_custom_scrapers()}
    scraper = scrapers.get(plugin_id)
    if not scraper or not hasattr(scraper, "get_catalog_items"):
        return []
    if not _scraper_serves(scraper, CAPABILITY_CATALOG):
        return []

    if is_cancelled and is_cancelled():
        return []

    file_path = getattr(scraper, "_file_path", None)
    class_name = getattr(scraper, "_class_name", None)
    runner_path = get_runner_path()

    items = None

    if file_path and os.path.isfile(file_path) and runner_path and os.path.isfile(runner_path):
        payload = {
            "action": "get_catalog_items",
            "file_path": str(file_path),
            "class_name": class_name,
            "kwargs": {
                "catalog_id": catalog_id,
                "genre": genre,
                "page": page,
                "query": query,
                "limit": limit,
                "year": year,
                "extra_filters": extra_filters,
            },
        }
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-s", runner_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(json.dumps(payload), timeout=timeout)
            if proc.returncode != 0:
                logger.warning(
                    f"Custom plugin '{plugin_id}' catalog '{catalog_id}' process exited with code {proc.returncode}: {stderr[:300]}"
                )
                items = []
            else:
                data = json.loads(stdout)
                if isinstance(data, dict) and "error" in data:
                    logger.warning(f"Custom plugin '{plugin_id}' catalog error: {data['error']}")
                    items = []
                elif isinstance(data, list):
                    items = data
                else:
                    items = []
        except subprocess.TimeoutExpired:
            if proc:
                try:
                    proc.kill()
                    proc.communicate(timeout=1.0)
                except Exception:
                    pass
            logger.warning(
                f"Custom plugin '{plugin_id}' catalog '{catalog_id}' timed out after {timeout}s (subprocess terminated)"
            )
            return []
        except Exception as e:
            logger.error(f"Error fetching catalog from '{plugin_id}' in subprocess: {e}")
            items = []

    if items is None:
        def _call():
            sig = inspect.signature(scraper.get_catalog_items)
            kwargs = {}
            params = sig.parameters
            if "catalog_id" in params:
                kwargs["catalog_id"] = catalog_id
            if "genre" in params:
                kwargs["genre"] = genre
            if "page" in params:
                kwargs["page"] = page
            if "query" in params:
                kwargs["query"] = query
            if "limit" in params:
                kwargs["limit"] = limit
            if "year" in params:
                kwargs["year"] = year
            if "extra_filters" in params:
                kwargs["extra_filters"] = extra_filters

            if not kwargs and len(params) >= 1:
                return scraper.get_catalog_items(catalog_id)
            return scraper.get_catalog_items(**kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                items = future.result(timeout=timeout) or []
            except TimeoutError:
                logger.warning(f"Custom plugin '{plugin_id}' catalog '{catalog_id}' timed out after {timeout}s")
                return []
            except Exception as e:
                logger.error(f"Custom plugin '{plugin_id}' catalog error: {e}")
                return []

    if not isinstance(items, list):
        return []

    formatted_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_id = it.get("id") or it.get("imdb_id")
        title = it.get("title")
        if not item_id or not title:
            continue

        formatted_items.append({
            "id": str(item_id),
            "title": str(title),
            "year": str(it.get("year", "")),
            "medium_cover_image": it.get("medium_cover_image") or it.get("poster", ""),
            "background": it.get("background") or it.get("backdrop", ""),
            "type": it.get("type", "movie"),
            "genres": it.get("genres", []),
            "rating": str(it.get("rating", "")),
        })

    return formatted_items
