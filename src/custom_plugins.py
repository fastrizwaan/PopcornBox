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

You can place standalone Python (.py) scraper files in this directory.
PopcornBox will automatically discover, load, and run them when searching for streams.

### Plugin Format:
Every plugin file should define a class with an `id`, `name`, `media_types`, and `get_streams(...)` method.

Example:
```python
class Scraper:
    id = "custom.my_scraper"
    name = "My Scraper"
    version = "1.0.0"
    description = "Scrapes streams for movies and series"
    media_types = ["movie", "series"]

    def get_streams(self, imdb_id, media_type="movie", season=None, episode=None, title=None, year=None, tmdb_id=None):
        streams = []
        # Query your provider API...
        # streams.append({
        #     "name": "Provider 1080p",
        #     "title": "1080p | 👤 150 | 2.1 GB",
        #     "infoHash": "0123456789abcdef0123456789abcdef01234567",
        #     # or "magnet": "magnet:?xt=urn:btih:...",
        #     # or "url": "https://..."
        # })
        return streams
```
"""


class BaseCustomScraper:
    """Base class that custom plugins can inherit from."""
    id: str = "custom.base"
    name: str = "Base Custom Scraper"
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    media_types: list = ["movie", "series"]
    catalog_sections: list = []  # Optional list of dicts: [{"id": "...", "name": "...", "type": "movie", "genres": [...]}]

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


def is_scraper_enabled(scraper_id: str) -> bool:
    """Check if a specific custom scraper is enabled."""
    disabled = database.get_setting("disabled_custom_scrapers", [])
    if not isinstance(disabled, list):
        disabled = []
    return str(scraper_id) not in disabled


def toggle_scraper(scraper_id: str, enabled: bool):
    """Enable or disable a specific custom scraper."""
    disabled = set(database.get_setting("disabled_custom_scrapers", []))
    if enabled:
        disabled.discard(str(scraper_id))
    else:
        disabled.add(str(scraper_id))
    database.set_setting("disabled_custom_scrapers", list(disabled))


def load_all_custom_scrapers(force_reload: bool = False):
    """
    Scans the custom plugins directory, imports all valid .py scraper files,
    and updates the internal registry.
    """
    global _LOADED_SCRAPERS, _LOADED_MODULES
    with _LOAD_LOCK:
        if _LOADED_SCRAPERS and not force_reload:
            return list(_LOADED_SCRAPERS.values())

        plugins_dir = get_custom_plugins_dir()
        if not plugins_dir.exists():
            _LOADED_SCRAPERS = {}
            _LOADED_MODULES = {}
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
            "filename": getattr(s, "_filename", ""),
            "file_path": getattr(s, "_file_path", ""),
        })
    return results


def get_enabled_scrapers_for_type(media_type: str = "movie") -> list:
    """Return all active scraper instances supporting the specified media type."""
    if not is_custom_plugins_enabled():
        return []

    norm_type = "series" if media_type in ["series", "tv", "anime"] else "movie"
    all_scrapers = load_all_custom_scrapers()
    enabled = []
    for s in all_scrapers:
        if not is_scraper_enabled(s.id):
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
    timeout: float = 15.0
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
            "quality": s.get("quality"),
            "size": s.get("size"),
            "seeders": s.get("seeders", s.get("seeds", 0)),
            "behaviorHints": s.get("behaviorHints", {}),
        }
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

    for scraper in load_all_custom_scrapers():
        if not is_scraper_enabled(scraper.id):
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

            genres = list(sec.get("genres") or [])
            catalogs.append({
                "addon_name": s_name,
                "manifest_url": catalog_url(s_id),
                "base_url": catalog_url(s_id),
                "catalog_id": sec_id,
                "catalog_name": sec_name,
                "display_name": f"{s_name} - {sec_name}",
                "genres": genres,
                "extra": [{"name": "genre", "isRequired": False, "options": genres}] if genres else [],
                "extraSupported": ["genre", "skip", "search"] if genres else ["skip", "search"],
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
    timeout: float = 12.0
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
