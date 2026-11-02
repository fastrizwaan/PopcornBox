"""
Nuvio Plugins Support for PopcornBox.
Handles Nuvio plugin repositories, scraper manifest parsing, code fetching,
and sandboxed JavaScript execution using Deno (or Node.js fallback).
"""

import os
import re
import sys
import json
import time
import shutil
import urllib.request
import urllib.error
import urllib.parse
import ssl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from . import database
from . import tmdb_helper

logger = logging.getLogger("PopcornBox.Plugins")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
RUNNER_BUNDLE_FILENAME = "nuvio_runner.bundle.js"

try:
    _SSL_CONTEXT = ssl.create_default_context()
except Exception:
    _SSL_CONTEXT = None

_js_runtime_cmd = None
_runtime_checked = False
_runtime_lock = threading.Lock()


def get_runner_bundle_path():
    """Return the absolute path to the bundled nuvio runner JS file."""
    # Look in the same directory as this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(module_dir, RUNNER_BUNDLE_FILENAME)
    if os.path.exists(candidate):
        return candidate

    # Look in system data dir (for installed Flatpak / system installs)
    alt_paths = [
        os.path.join("/app/share/popcorn-box/cine", RUNNER_BUNDLE_FILENAME),
        os.path.join("/usr/share/popcorn-box/cine", RUNNER_BUNDLE_FILENAME),
        os.path.join("/usr/local/share/popcorn-box/cine", RUNNER_BUNDLE_FILENAME),
    ]
    for p in alt_paths:
        if os.path.exists(p):
            return p

    return candidate


def find_js_runtime():
    """
    Find an available JavaScript runtime (Deno preferred, Node.js fallback).
    Returns a list of command arguments (e.g. ['deno', 'run', '--no-config', '--allow-net']).
    """
    global _js_runtime_cmd, _runtime_checked
    with _runtime_lock:
        if _runtime_checked:
            return _js_runtime_cmd

        bundle_path = get_runner_bundle_path()

        # Candidates for Deno
        deno_candidates = [
            "/app/bin/deno",
            shutil.which("deno"),
            os.path.expanduser("~/.deno/bin/deno"),
            "/usr/bin/deno",
            "/usr/local/bin/deno",
        ]
        for c in deno_candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                try:
                    res = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=3)
                    if res.returncode == 0:
                        _js_runtime_cmd = [c, "run", "--no-config", "--allow-net", bundle_path]
                        _runtime_checked = True
                        logger.info(f"Using Deno runtime for Nuvio plugins: {c}")
                        return _js_runtime_cmd
                except Exception as e:
                    logger.debug(f"Failed testing Deno candidate {c}: {e}")

        # Fallback to Node.js
        node_candidates = [
            "/app/bin/node",
            shutil.which("node"),
            shutil.which("nodejs"),
            "/usr/bin/node",
            "/usr/local/bin/node",
        ]
        for c in node_candidates:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                try:
                    res = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=3)
                    if res.returncode == 0:
                        _js_runtime_cmd = [c, bundle_path]
                        _runtime_checked = True
                        logger.info(f"Using Node.js runtime for Nuvio plugins: {c}")
                        return _js_runtime_cmd
                except Exception as e:
                    logger.debug(f"Failed testing Node candidate {c}: {e}")

        _runtime_checked = True
        logger.warning("No Deno or Node.js runtime found on the system for Nuvio plugins.")
        return None


def is_runtime_available():
    return find_js_runtime() is not None


# --- URL Normalization ---

def normalize_manifest_url(raw_url):
    """
    Normalizes a user-entered URL into a direct plugin manifest.json URL.
    Handles raw github URLs, github repo URLs, etc.
    """
    if not raw_url:
        raise ValueError("URL cannot be empty")
    url = raw_url.strip()

    # github.com/user/repo -> raw.githubusercontent.com/user/repo/refs/heads/main/manifest.json
    gh_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if gh_match:
        user, repo = gh_match.group(1), gh_match.group(2)
        return f"https://raw.githubusercontent.com/{user}/{repo}/refs/heads/main/manifest.json"

    # github.com/user/repo/blob/.../manifest.json -> raw.githubusercontent.com/user/repo/.../manifest.json
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    # If URL does not end in .json and has no filename, append manifest.json
    parsed = urllib.parse.urlparse(url)
    if not parsed.path.endswith(".json"):
        path = parsed.path.rstrip("/") + "/manifest.json"
        url = urllib.parse.urlunparse(parsed._replace(path=path))

    return url


# --- Settings / Preferences ---

def is_plugins_enabled():
    return bool(database.get_setting("plugins_enabled", True))


def set_plugins_enabled(enabled):
    database.set_setting("plugins_enabled", bool(enabled))
    # Cached stream rows embed the provider labels from the previous mode.
    # Drop them so disabling plugins cannot keep their cards alive and
    # re-enabling gets a clean, grouped fetch.
    database.clear_all_cached_streams_async()


def is_group_by_repository():
    return bool(database.get_setting("plugins_group_by_repository", False))


def set_group_by_repository(enabled):
    database.set_setting("plugins_group_by_repository", bool(enabled))
    database.clear_all_cached_streams_async()


def get_scraper_settings(scraper_id):
    settings_map = database.get_setting("plugins_scraper_settings", {}) or {}
    return settings_map.get(scraper_id, {})


def save_scraper_settings(scraper_id, settings):
    settings_map = dict(database.get_setting("plugins_scraper_settings", {}) or {})
    settings_map[scraper_id] = settings
    database.set_setting("plugins_scraper_settings", settings_map)


# --- Repository and Scraper Queries ---

def get_installed_repositories():
    """Return all installed plugin repositories."""
    return database.get_plugin_repositories()


def get_all_scrapers():
    """Return all installed plugin scrapers."""
    return database.get_plugin_scrapers()


def get_scrapers_for_repository(manifest_url):
    return [s for s in get_all_scrapers() if s.get("repository_url") == manifest_url]


def get_repository_name(manifest_url, default=None):
    """Return a repository's display name for one of its scrapers."""
    if not manifest_url:
        return default
    for repo in get_installed_repositories():
        if repo.get("manifest_url") == manifest_url:
            return repo.get("name") or default
    return default


def get_scraper_provider_name(scraper):
    """Return the provider label shown in Streams (repo name when grouping)."""
    name = (scraper.get("name") if isinstance(scraper, dict) else None) or "Plugin"
    if is_group_by_repository():
        return get_repository_name(scraper.get("repository_url"), name)
    return name



def get_scraper(scraper_id):
    for s in get_all_scrapers():
        if s.get("id") == scraper_id:
            return s
    return None


def get_platform_tags():
    """Platform tags a Nuvio manifest uses to gate scraper availability."""
    tags = {"desktop", "jvm"}
    if sys.platform.startswith("linux"):
        tags.add("linux")
    elif sys.platform == "darwin":
        tags.add("macos")
    elif sys.platform.startswith("win"):
        tags.add("windows")
    return tags


def scraper_supported_on_this_platform(info):
    """Match Nuvio's supportedPlatforms/disabledPlatforms manifest filters."""
    if not isinstance(info, dict):
        return False
    supported = {str(t).strip().lower() for t in (info.get("supportedPlatforms") or []) if t}
    disabled = {str(t).strip().lower() for t in (info.get("disabledPlatforms") or []) if t}
    tags = get_platform_tags()
    if supported and tags.isdisjoint(supported):
        return False
    if not tags.isdisjoint(disabled):
        return False
    return True


def normalize_media_type(media_type):
    if not media_type:
        return "movie"
    m = str(media_type).lower().strip()
    if m in ("series", "show", "tvshow", "tv_show", "anime", "tv"):
        return "tv"
    return "movie"


def scraper_supports_type(scraper, media_type):
    supported = [normalize_media_type(t) for t in scraper.get("supported_types", ["movie", "tv"])]
    return normalize_media_type(media_type) in supported


def get_enabled_scrapers_for_type(media_type):
    """Return all enabled scrapers that support the given media type."""
    if not is_plugins_enabled():
        return []
    target_type = normalize_media_type(media_type)
    return [
        s for s in get_all_scrapers()
        if s.get("enabled", True) and scraper_supports_type(s, target_type)
    ]


def toggle_scraper(scraper_id, enabled):
    """Enable or disable a specific scraper."""
    database.toggle_plugin_scraper(scraper_id, bool(enabled))


# --- HTTP Helpers ---

def _http_get_text(url, timeout=15.0):
    """Fetch text resource directly with proper User-Agent and SSL context."""
    if not url:
        return ""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# --- Repository Installation & Refresh ---

def fetch_repository_data(manifest_url):
    """
    Fetches the repository manifest and all its scraper code files.
    Returns (repo_info, list_of_scrapers).
    """
    raw_json = _http_get_text(manifest_url, timeout=12.0)
    manifest = json.loads(raw_json)

    repo_name = str(manifest.get("name") or "Unnamed Plugin Repository").strip()
    repo_version = str(manifest.get("version") or "1.0.0").strip()
    repo_desc = manifest.get("description")
    raw_scrapers = manifest.get("scrapers", [])

    if not raw_scrapers:
        raise ValueError(f"Plugin repository '{repo_name}' contains no scrapers")

    base_url = manifest_url.split("?")[0]
    if base_url.endswith("/manifest.json"):
        base_url = base_url[:-len("/manifest.json")]
    base_url = base_url.rstrip("/")

    # Fetch scraper code in parallel
    scrapers = []
    def _fetch_one_scraper(info):
        if not isinstance(info, dict) or not scraper_supported_on_this_platform(info):
            return None
        s_id = str(info.get("id") or "").strip()
        if not s_id:
            return None
        filename = str(info.get("filename") or "").strip()
        if not filename:
            return None

        if filename.startswith("http://") or filename.startswith("https://"):
            code_url = filename
        else:
            code_url = f"{base_url}/{filename.lstrip('/')}"

        try:
            code = _http_get_text(code_url, timeout=15.0)
        except Exception as e:
            logger.warning(f"Failed to fetch scraper code for {s_id} from {code_url}: {e}")
            return None

        scraper_id = f"{manifest_url.lower()}:{s_id}"
        return {
            "id": scraper_id,
            "raw_id": s_id,
            "repository_url": manifest_url,
            "name": str(info.get("name") or s_id).strip(),
            "description": str(info.get("description") or "").strip(),
            "version": str(info.get("version") or "1.0.0").strip(),
            "filename": filename,
            "supported_types": info.get("supportedTypes") or ["movie", "tv"],
            "enabled": bool(info.get("enabled", True)),
            "manifest_enabled": bool(info.get("enabled", True)),
            "has_settings": bool(info.get("hasSettings", False)),
            "logo": info.get("logo"),
            "content_language": info.get("contentLanguage") or [],
            "formats": info.get("formats") or info.get("supportedFormats") or [],
            "supported_platforms": info.get("supportedPlatforms") or [],
            "disabled_platforms": info.get("disabledPlatforms") or [],
            "code": code,
        }

    with ThreadPoolExecutor(max_workers=min(len(raw_scrapers), 8)) as ex:
        futures = [ex.submit(_fetch_one_scraper, s) for s in raw_scrapers]
        for f in as_completed(futures):
            res = f.result()
            if res:
                scrapers.append(res)

    repo_info = {
        "manifest_url": manifest_url,
        "name": repo_name,
        "version": repo_version,
        "description": repo_desc,
        "scraper_count": len(scrapers),
        "last_updated": time.time(),
        "error_message": None,
    }

    return repo_info, scrapers


def install_repository(raw_url):
    """
    Synchronously install a repository by URL.
    Returns (success: bool, message: str, repo_info: dict | None).
    """
    try:
        manifest_url = normalize_manifest_url(raw_url)
    except Exception as e:
        return False, f"Invalid repository URL: {e}", None

    existing = [r for r in get_installed_repositories() if r.get("manifest_url") == manifest_url]
    if existing:
        return False, "Plugin repository is already installed", existing[0]

    try:
        repo_info, scrapers = fetch_repository_data(manifest_url)
    except Exception as e:
        logger.error(f"Failed to install plugin repository {manifest_url}: {e}")
        return False, f"Failed to install repository: {e}", None

    # Save to database
    database.save_plugin_repository(repo_info)
    database.save_plugin_scrapers(scrapers, replace_for_repo=manifest_url)

    logger.info(f"Installed plugin repository '{repo_info['name']}' with {len(scrapers)} scrapers")
    return True, f"Installed {repo_info['name']} ({len(scrapers)} providers)", repo_info


def remove_repository(manifest_url):
    """Remove an installed repository and its scrapers."""
    database.remove_plugin_repository(manifest_url)
    logger.info(f"Removed plugin repository {manifest_url}")


def refresh_repository(manifest_url):
    """
    Refresh a repository and its scrapers while preserving user toggle states.
    Returns (success: bool, message: str, repo_info: dict | None).
    """
    try:
        repo_info, new_scrapers = fetch_repository_data(manifest_url)
    except Exception as e:
        logger.error(f"Failed to refresh plugin repository {manifest_url}: {e}")
        # Mark error on existing repo
        for r in get_installed_repositories():
            if r.get("manifest_url") == manifest_url:
                r["error_message"] = str(e)
                database.save_plugin_repository(r)
                break
        return False, f"Failed to refresh: {e}", None

    # Preserve previous user enabled states; default to manifest enabled for new scrapers
    prev_scrapers = {s["id"]: s for s in get_all_scrapers()}
    for s in new_scrapers:
        old = prev_scrapers.get(s["id"])
        if old is not None:
            s["enabled"] = bool(old.get("enabled", s.get("manifest_enabled", True)))
        else:
            s["enabled"] = bool(s.get("manifest_enabled", True))

    database.save_plugin_repository(repo_info)
    database.save_plugin_scrapers(new_scrapers, replace_for_repo=manifest_url)

    logger.info(f"Refreshed plugin repository '{repo_info['name']}' ({len(new_scrapers)} scrapers)")
    return True, f"Refreshed {repo_info['name']}", repo_info


# --- Scraper Execution ---

def execute_scraper(scraper, tmdb_id, media_type, season=None, episode=None, timeout=25):
    """
    Executes a single scraper JS code in the sandboxed runner.
    Returns a list of raw stream dictionaries.
    """
    cmd = find_js_runtime()
    if not cmd:
        logger.warning("Cannot execute scraper: No JS runtime (Deno/Node.js) found.")
        return []

    code = scraper.get("code")
    if not code:
        logger.warning(f"Scraper {scraper.get('id')} has no code")
        return []

    norm_type = normalize_media_type(media_type)
    tmdb_key = tmdb_helper.get_tmdb_api_key() or "b030404650f279792a8d3287232358e3"

    payload = {
        "action": "getStreams",
        "code": code,
        "scraperId": scraper.get("id"),
        "tmdbId": str(tmdb_id),
        "mediaType": norm_type,
        "season": season,
        "episode": episode,
        "settings": get_scraper_settings(scraper.get("id")),
        "tmdbApiKey": tmdb_key,
    }

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        input_data = json.dumps(payload)
        stdout, stderr = proc.communicate(input_data, timeout=timeout)
        if proc.returncode != 0 and stderr:
            logger.debug(f"Scraper {scraper.get('name')} runner stderr: {stderr[:200]}")

        # The runner outputs the JSON array on the last line of stdout
        lines = [line.strip() for line in stdout.strip().split("\n") if line.strip()]
        if not lines:
            return []

        # Find the last valid JSON array line
        for line in reversed(lines):
            if line.startswith("[") and line.endswith("]"):
                try:
                    items = json.loads(line)
                    if isinstance(items, list):
                        return _format_scraper_results(items, scraper)
                except Exception:
                    pass

        # Fallback: parse entire stdout
        items = json.loads(stdout.strip())
        if isinstance(items, list):
            return _format_scraper_results(items, scraper)
        return []

    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        logger.info(f"Scraper {scraper.get('name')} timed out after {timeout}s")
        return []
    except Exception as e:
        logger.warning(f"Error executing scraper {scraper.get('name')}: {e}")
        return []


def _format_scraper_results(raw_items, scraper):
    """Convert raw scraper output into PopcornBox stream dictionary entries."""
    formatted = []
    provider_name = get_scraper_provider_name(scraper)
    default_name = scraper.get("name") or "Plugin"

    def _text(value):
        return "" if value is None else str(value).strip()

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        url = item.get("url")
        if isinstance(url, dict):
            url = url.get("url")
        url = _text(url)
        external_url = _text(item.get("externalUrl"))
        info_hash = _text(item.get("infoHash"))
        yt_id = _text(item.get("ytId"))
        # Nuvio scrapers may return direct URLs, torrent hashes, external
        # links, or YouTube IDs. Dropping a hash-only result silently emptied
        # whole providers, so accept every playable source shape here.
        if not any((url, external_url, info_hash, yt_id)):
            continue

        if url and url.startswith(("http://", "https://")):
            url = url.replace(" ", "%20")
        if external_url and external_url.startswith(("http://", "https://")):
            external_url = external_url.replace(" ", "%20")

        name = _text(item.get("name") or item.get("provider")) or default_name
        title = _text(item.get("title"))
        quality = _text(item.get("quality"))
        size = _text(item.get("size"))
        language = _text(item.get("language"))
        headers = item.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        subtitles = item.get("subtitles")
        if not isinstance(subtitles, list):
            subtitles = []
        sources = item.get("sources")
        if not isinstance(sources, list):
            sources = []

        desc_parts = [p for p in [quality, size, language] if p]
        desc_line = " • ".join(desc_parts)
        if desc_line and desc_line not in title:
            if title:
                full_title = f"{title}\n{desc_line}"
            else:
                full_title = desc_line
        else:
            full_title = title or name

        behavior_hints = item.get("behaviorHints")
        if not isinstance(behavior_hints, dict):
            behavior_hints = {}
        else:
            behavior_hints = dict(behavior_hints)
        if headers:
            # Proxy / HTTP headers. Merge with any hints the scraper supplied
            # instead of replacing them outright.
            proxy_headers = behavior_hints.get("proxyHeaders")
            if not isinstance(proxy_headers, dict):
                proxy_headers = {}
            else:
                proxy_headers = dict(proxy_headers)
            request_headers = proxy_headers.get("request")
            if not isinstance(request_headers, dict):
                request_headers = {}
            else:
                request_headers = dict(request_headers)
            request_headers.update(headers)
            proxy_headers["request"] = request_headers
            behavior_hints["proxyHeaders"] = proxy_headers
            hinted_headers = behavior_hints.get("headers")
            if not isinstance(hinted_headers, dict):
                hinted_headers = {}
            else:
                hinted_headers = dict(hinted_headers)
            hinted_headers.update(headers)
            behavior_hints["headers"] = hinted_headers

        formatted.append({
            "name": name,
            "title": full_title,
            "url": url,
            "externalUrl": external_url,
            "ytId": yt_id,
            "quality": quality,
            "size": size,
            "headers": headers,
            "subtitles": subtitles,
            "infoHash": info_hash,
            "fileIdx": item.get("fileIdx"),
            "sources": sources,
            "seeders": item.get("seeders"),
            "peers": item.get("peers"),
            "behaviorHints": behavior_hints,
            "addon_name": provider_name,
            "addon_names": [provider_name],
            "is_nuvio_plugin": True,
            "plugin_scraper_id": scraper.get("id"),
        })

    return formatted


def test_scraper(scraper_id):
    """
    Tests a scraper against TMDB ID 603 (The Matrix) or S01E01.
    Returns (success: bool, stream_count: int, error_msg: str | None).
    """
    scraper = get_scraper(scraper_id)
    if not scraper:
        return False, 0, "Provider not found"

    media_type = "movie" if scraper_supports_type(scraper, "movie") else "tv"
    season = 1 if media_type == "tv" else None
    episode = 1 if media_type == "tv" else None
    tmdb_id = "603"  # The Matrix

    try:
        streams = execute_scraper(scraper, tmdb_id, media_type, season=season, episode=episode, timeout=20)
        return True, len(streams), None
    except Exception as e:
        return False, 0, str(e)


# --- Stream Discovery Integration ---

def fetch_plugin_streams(tmdb_id, media_type, season=None, episode=None, on_stream_batch=None, abort_event=None):
    """
    Executes all enabled plugin scrapers in parallel and collects their streams.
    Calls on_stream_batch(streams) as each scraper finishes so the UI updates live.
    Returns the total list of streams.
    """
    if not is_plugins_enabled():
        return []

    scrapers = get_enabled_scrapers_for_type(media_type)
    if not scrapers:
        return []

    if abort_event and abort_event.is_set():
        return []

    logger.info(f"Querying {len(scrapers)} Nuvio plugin scrapers for TMDB {tmdb_id} ({media_type})")

    all_streams = []
    max_workers = min(len(scrapers), 6)

    def _worker(scraper):
        if abort_event and abort_event.is_set():
            return []
        res = execute_scraper(scraper, tmdb_id, media_type, season=season, episode=episode, timeout=25)
        return res

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(_worker, s): s for s in scrapers}
        for f in as_completed(future_map):
            if abort_event and abort_event.is_set():
                break
            try:
                streams = f.result()
                if streams:
                    all_streams.extend(streams)
                    if on_stream_batch and not (abort_event and abort_event.is_set()):
                        on_stream_batch(streams)
            except Exception as e:
                scraper = future_map.get(f)
                logger.debug(f"Scraper {scraper.get('name') if scraper else 'unknown'} failed: {e}")

    return all_streams
