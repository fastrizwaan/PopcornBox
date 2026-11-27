"""
Nuvio Plugins Support for PopcornBox.
Handles Nuvio plugin repositories, scraper manifest parsing, code fetching,
and permission-limited JavaScript execution using Deno.
"""

import os
import re
import sys
import json
import hashlib
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
        os.path.join("/app/share/popcorn-box/popcornbox", RUNNER_BUNDLE_FILENAME),
        os.path.join("/usr/share/popcorn-box/popcornbox", RUNNER_BUNDLE_FILENAME),
        os.path.join("/usr/local/share/popcorn-box/popcornbox", RUNNER_BUNDLE_FILENAME),
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
    Find Deno, the JavaScript runtime that provides the required permission boundary.
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
                        _js_runtime_cmd = [c, "run", "--no-config", "--no-prompt", "--allow-net", bundle_path]
                        _runtime_checked = True
                        logger.info(f"Using Deno runtime for Nuvio plugins: {c}")
                        return _js_runtime_cmd
                except Exception as e:
                    logger.debug(f"Failed testing Deno candidate {c}: {e}")

        _runtime_checked = True
        logger.warning("No sandbox-capable Deno runtime found for Nuvio plugins.")
        return None


def is_runtime_available():
    return find_js_runtime() is not None


def resolve_scraper_allowed_hosts(scraper):
    """
    Resolves the network host allowlist for a scraper.
    Returns a list of host strings if host-scoped permissions are defined,
    or None if the scraper requires unconstrained network access (dynamic hosts).
    """
    if not isinstance(scraper, dict):
        return None

    s_id = scraper.get("id")
    hosts = set()

    # 1. Manifest / scraper metadata
    declared = (
        scraper.get("allowed_hosts")
        or scraper.get("allowedDomains")
        or scraper.get("domains")
        or scraper.get("hosts")
    )
    if declared:
        if isinstance(declared, str):
            declared = [h.strip() for h in declared.split(",") if h.strip()]
        if isinstance(declared, (list, tuple, set)):
            hosts.update(str(h).strip().lower() for h in declared if h)

    # 2. Scraper settings override
    if s_id:
        settings = get_scraper_settings(s_id) or {}
        custom_hosts = settings.get("allowed_hosts")
        if custom_hosts:
            if isinstance(custom_hosts, str):
                custom_hosts = [h.strip() for h in custom_hosts.split(",") if h.strip()]
            if isinstance(custom_hosts, (list, tuple, set)):
                hosts.update(str(h).strip().lower() for h in custom_hosts if h)

    if not hosts:
        return None

    # Include default essential metadata hosts
    hosts.add("api.themoviedb.org")

    repo_url = scraper.get("repository_url")
    if repo_url:
        try:
            repo_host = urllib.parse.urlparse(repo_url).hostname
            if repo_host:
                hosts.add(repo_host.lower())
        except Exception:
            pass

    return sorted(hosts)


def _get_scraper_subprocess_env():
    """Return environment dictionary with DoH proxy variables applied when enabled."""
    env = dict(os.environ)
    try:
        from . import doh
        doh_env = doh.get_doh_env()
        if doh_env:
            env.update(doh_env)
    except Exception as e:
        logger.debug(f"Could not configure DoH proxy for scraper runtime: {e}")
    return env


def build_scraper_command(scraper):
    """
    Builds the JS runtime command for executing a scraper, applying host-scoped
    Deno network permissions when allowed hosts are declared or configured.
    """
    base_cmd = find_js_runtime()
    if not base_cmd:
        return None

    cmd = list(base_cmd)
    allowed_hosts = resolve_scraper_allowed_hosts(scraper)

    if allowed_hosts:
        hosts_for_flag = list(allowed_hosts)
        try:
            from . import doh
            doh_env = doh.get_doh_env()
            proxy_url = (doh_env or {}).get("HTTP_PROXY") or ""
            if proxy_url:
                proxy_netloc = urllib.parse.urlparse(proxy_url).netloc
                if proxy_netloc and proxy_netloc not in hosts_for_flag:
                    hosts_for_flag.append(proxy_netloc)
        except Exception:
            pass
        scoped_net_flag = f"--allow-net={','.join(hosts_for_flag)}"
        if "--allow-net" in cmd:
            idx = cmd.index("--allow-net")
            cmd[idx] = scoped_net_flag
        elif not any(isinstance(arg, str) and arg.startswith("--allow-net") for arg in cmd):
            cmd.insert(-1, scoped_net_flag)
        logger.debug(
            f"Executing scraper {scraper.get('name')!r} with host-scoped network permissions: {scoped_net_flag}"
        )
    else:
        logger.debug(
            f"Executing scraper {scraper.get('name')!r} with unconstrained network permissions (--allow-net)"
        )

    return cmd


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


# --- Settings / Preferences & Health State ---

_SCRAPER_HEALTH = {}
_SCRAPER_PERF = {}
_SCRAPER_HEALTH_LOCK = threading.Lock()
_SCRAPER_FAILURE_THRESHOLD = 2
_SCRAPER_COOLDOWN_SECONDS = 120.0


def is_scraper_in_cooldown(scraper_id):
    """Return True if the scraper recently failed repeatedly and is cooling down."""
    if not scraper_id:
        return False
    with _SCRAPER_HEALTH_LOCK:
        entry = _SCRAPER_HEALTH.get(str(scraper_id))
        if not entry:
            return False
        if time.monotonic() < entry.get("cooldown_until", 0.0):
            return True
        if entry.get("cooldown_until", 0.0) > 0.0:
            entry["cooldown_until"] = 0.0
            entry["failures"] = 0
        return False


def clear_scraper_cooldown(scraper_id=None):
    """Clear cooldown state for a specific scraper or all scrapers."""
    with _SCRAPER_HEALTH_LOCK:
        if scraper_id is None:
            _SCRAPER_HEALTH.clear()
            _SCRAPER_PERF.clear()
        else:
            _SCRAPER_HEALTH.pop(str(scraper_id), None)
            _SCRAPER_PERF.pop(str(scraper_id), None)


def _record_scraper_success(scraper_id, elapsed=None, stream_count=0):
    if not scraper_id:
        return
    key = str(scraper_id)
    with _SCRAPER_HEALTH_LOCK:
        _SCRAPER_HEALTH.pop(key, None)
        perf = _SCRAPER_PERF.get(key) or {}
        if elapsed is not None:
            prev_avg = perf.get("avg_elapsed")
            perf["avg_elapsed"] = elapsed if prev_avg is None else (prev_avg * 0.6 + elapsed * 0.4)
        perf["last_streams"] = int(stream_count or 0)
        _SCRAPER_PERF[key] = perf


def _record_scraper_failure(scraper_id, reason):
    if not scraper_id or reason == SCRAPER_ERROR_EMPTY:
        return
    key = str(scraper_id)
    with _SCRAPER_HEALTH_LOCK:
        entry = _SCRAPER_HEALTH.setdefault(key, {"failures": 0, "cooldown_until": 0.0, "last_error": None})
        entry["failures"] = entry.get("failures", 0) + 1
        entry["last_error"] = reason
        if entry["failures"] >= _SCRAPER_FAILURE_THRESHOLD:
            entry["cooldown_until"] = time.monotonic() + _SCRAPER_COOLDOWN_SECONDS
            logger.info(
                "Scraper %r placed in %.0fs cooldown after %d failures (%s)",
                key, _SCRAPER_COOLDOWN_SECONDS, entry["failures"], reason,
            )


def sort_scrapers_by_health(scrapers):
    """Order scrapers so previously fast, stream-producing scrapers run before slow/failing ones."""
    items = list(scrapers or [])
    if len(items) <= 1:
        return items
    with _SCRAPER_HEALTH_LOCK:
        health_snap = {k: dict(v) for k, v in _SCRAPER_HEALTH.items()}
        perf_snap = {k: dict(v) for k, v in _SCRAPER_PERF.items()}

    def _priority(idx_and_scraper):
        idx, scraper = idx_and_scraper
        s_id = str((scraper or {}).get("id") or "")
        health = health_snap.get(s_id)
        if health and health.get("failures", 0) > 0:
            return (3, float(health.get("failures", 0)), idx)
        perf = perf_snap.get(s_id)
        if not perf:
            return (1, 0.0, idx)
        if perf.get("last_streams", 0) > 0:
            return (0, float(perf.get("avg_elapsed") or 0.0), idx)
        return (2, float(perf.get("avg_elapsed") or 0.0), idx)

    return [s for _, s in sorted(enumerate(items), key=_priority)]


def is_plugins_enabled():
    return bool(database.get_setting("plugins_enabled", True))


def set_plugins_enabled(enabled):
    database.set_setting("plugins_enabled", bool(enabled))
    clear_scraper_cooldown()
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


def plugin_content_id(video_id, season=None, episode=None):
    """Extract normalized plugin content ID matching NuvioDesktop PluginContentIds."""
    trimmed = "" if video_id is None else str(video_id).strip()
    if not trimmed:
        return trimmed
    without_prefix = trimmed
    lower_trimmed = trimmed.lower()
    if lower_trimmed.startswith("tmdb:"):
        without_prefix = trimmed[5:]
    elif lower_trimmed.startswith("tmdb/"):
        without_prefix = trimmed[5:]
    if season is not None and episode is not None:
        episode_suffix = f":{season}:{episode}"
        if without_prefix.endswith(episode_suffix):
            without_prefix = without_prefix[: -len(episode_suffix)]
    base_id = without_prefix.split("/", 1)[0].strip()
    return base_id or trimmed


def js_plugin_media_type(media_type, supported_types, season=None, episode=None):
    """Map normalized media type to JS runtime mediaType matching NuvioDesktop."""
    norm = "" if media_type is None else str(media_type).strip().lower()
    supported = {str(t).strip().lower() for t in (supported_types or []) if t}
    if norm == "tv" and season is not None and episode is not None and "series" in supported:
        return "series"
    return media_type


def scraper_supports_type(scraper, media_type):
    supported = [normalize_media_type(t) for t in scraper.get("supported_types", ["movie", "tv"])]
    return normalize_media_type(media_type) in supported


def scraper_is_anime_only(scraper):
    """Identify anime-focused scrapers that should not serve general TV series."""
    if not isinstance(scraper, dict):
        return False
    raw_types = scraper.get("supported_types") or ["movie", "tv"]
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    types = {str(value).strip().casefold() for value in raw_types if value}
    if "all" in types:
        return False
    anime_types = {"anime", "ova"}
    general_series_types = {"series", "tv", "show", "tvshow", "tv_show"}
    if types & anime_types and not types & general_series_types:
        return True

    identity = " ".join(
        str(scraper.get(key) or "")
        for key in ("name", "raw_id", "id", "description")
    ).casefold()
    anime_identity_markers = (
        "anime", "anilist", "myanimelist", "hianime", "animepahe", "animekai",
    )
    general_scope_markers = (
        "all media", "all genres", "general tv", "general series", "movies and tv",
        "movies, series", "all movies and series",
    )
    return (
        any(marker in identity for marker in anime_identity_markers)
        and not any(marker in identity for marker in general_scope_markers)
    )


def get_enabled_scrapers_for_type(media_type):
    """Return all enabled scrapers that support the given media type."""
    if not is_plugins_enabled():
        return []
    target_type = normalize_media_type(media_type)
    return [
        s for s in get_all_scrapers()
        if s.get("enabled", True) and scraper_supports_type(s, target_type)
    ]


def deduplicate_scrapers(scrapers):
    """Collapse byte-identical scraper copies while retaining their labels.

    Plugin repositories often republish the same scraper. Running each copy
    launches another Deno process and sends the same provider request. Only
    collapse copies when their code and saved settings match, and the code
    does not depend on its repository-specific SCRAPER_ID.

    Scrapers that differ only in name (but share identical code and settings)
    are collapsed into a single execution. The duplicate names are stored in
    ``_duplicate_provider_names`` on the surviving entry so the UI can still
    show all labels.

    Scrapers whose code contains the literal token ``SCRAPER_ID`` are always
    kept independent because their behaviour depends on the per-repo ID baked
    into the code.
    """
    unique = []
    signatures = {}
    group_by_repository = is_group_by_repository()
    repository_names = {
        str(repo.get("manifest_url") or ""): str(repo.get("name") or "")
        for repo in get_installed_repositories()
    } if group_by_repository else {}
    try:
        settings_by_id = database.get_setting("plugins_scraper_settings", {}) or {}
    except Exception:
        settings_by_id = {}

    def _provider_label(scraper):
        name = str(scraper.get("name") or "Plugin")
        if group_by_repository:
            return repository_names.get(str(scraper.get("repository_url") or "")) or name
        return name

    for scraper in scrapers or []:
        code = str(scraper.get("code") or "")
        scraper_id = scraper.get("id")
        # Keep scrapers that use their own ID in code independent: the token
        # SCRAPER_ID is a placeholder the runner replaces with the actual
        # repository-scoped ID, so two copies are behaviorally different.
        if not code or re.search(r"\bSCRAPER_ID\b", code):
            unique.append(scraper)
            continue
        try:
            settings = settings_by_id.get(scraper_id, {})
            settings_json = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            unique.append(scraper)
            continue

        signature = hashlib.sha256((code + "\0" + settings_json).encode("utf-8")).hexdigest()
        canonical = signatures.get(signature)
        if canonical is None:
            canonical = dict(scraper)
            canonical["_duplicate_provider_names"] = []
            signatures[signature] = canonical
            unique.append(canonical)
            continue

        # This scraper is byte-for-byte identical to one already queued.
        # Record its label so the UI can attribute results to both repos.
        duplicate_label = _provider_label(scraper)
        labels = canonical["_duplicate_provider_names"]
        primary_label = _provider_label(canonical)
        if duplicate_label and duplicate_label != primary_label and duplicate_label not in labels:
            labels.append(duplicate_label)
        logger.debug(
            "Duplicate scraper collapsed: %r (from %r) is identical to %r (from %r); "
            "one Deno process will run for both.",
            scraper.get("name"),
            scraper.get("repository_url"),
            canonical.get("name"),
            canonical.get("repository_url"),
        )

    return unique


def toggle_scraper(scraper_id, enabled):
    """Enable or disable a specific scraper."""
    clear_scraper_cooldown(scraper_id)
    database.toggle_plugin_scraper(scraper_id, bool(enabled))


# --- HTTP Helpers ---

def _normalize_manifest_resources(raw_resources):
    """Normalize Nuvio/Stremio manifest resources entries to lowercase names."""
    out = []
    if isinstance(raw_resources, (list, tuple)):
        for item in raw_resources:
            if isinstance(item, str):
                name = item.strip().lower()
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("id") or "").strip().lower()
            else:
                name = ""
            if name and name not in out:
                out.append(name)
    return out


_RE_JS_INSPECT_INDICATOR = re.compile(
    r"\b(?:getCatalog|getHome|getCatalogs|onSettings|manifest)\b|\.(?:catalog|getCatalog|getHome|onSettings|manifest)\b"
)
_RE_CATALOG_DEF = re.compile(
    r"(?:(?:async\s+)?function\s+(?P<fname>getCatalog|getHome)\s*\([^)]*\)|"
    r"\b(?P<aname>getCatalog|getHome|catalog)\s*[:=]\s*(?:async\s*)?(?:function\b[^{]*|\([^)]*\)\s*=>|[A-Za-z0-9_$]+\s*=>))"
)
_RE_ON_SETTINGS_DEF = re.compile(r"\bonSettings\s*[:=(]|\bexports\.onSettings\s*=")
_RE_STATIC_RESOURCES = re.compile(r"\bresources\s*[:=]\s*\[([^\]]{1,200})\]")
_RE_STUB_ASYNC_BODY = re.compile(
    r'^\{\s*(?:["\']use strict["\'];\s*)?return\s*__async\s*\([^,]+,[^,]+,function\s*\*\s*\([^)]*\)\s*\{\s*return\s*(?:\[\s*\]|\{\s*\}|\{\s*(?:metas|items|results|catalog|data)\s*:\s*\[\s*\]\s*\}|null|undefined|void\s+0)\s*;?\s*\}\s*\)\s*;?\s*\}$'
)
_RE_STUB_SYNC_BODY = re.compile(
    r'^\{\s*(?:["\']use strict["\'];\s*)?return\s*(?:\[\s*\]|\{\s*\}|\{\s*(?:metas|items|results|catalog|data)\s*:\s*\[\s*\]\s*\}|null|undefined|void\s+0)\s*;?\s*\}$'
)
_RE_STUB_ARROW_EXPR = re.compile(r'^(?:\(\s*)?(?:\[\s*\]|\{\s*\}|null|undefined|void\s+0)(?:\s*\))?\s*(?:[;,}\n]|$)')


def _extract_js_brace_block(code, open_idx, limit=350):
    """Extract a short balanced { ... } block for static stub detection."""
    if open_idx < 0 or open_idx >= len(code) or code[open_idx] != "{":
        return ""
    depth = 0
    end_limit = min(len(code), open_idx + limit)
    for idx in range(open_idx, end_limit):
        ch = code[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[open_idx:idx + 1]
    return ""


def _static_inspect_js(code):
    """Inspect JS source without spawning Deno to classify handlers and empty stubs."""
    text = str(code or "")
    matches = list(_RE_CATALOG_DEF.finditer(text))
    has_catalog = bool(matches)
    all_stubs = False
    if matches:
        all_stubs = True
        for m in matches:
            tail = text[m.end():].lstrip()
            if tail.startswith("{"):
                block = _extract_js_brace_block(text, text.find("{", m.end()))
                compact = re.sub(r"\s+", " ", block).strip()
                if not (_RE_STUB_ASYNC_BODY.match(compact) or _RE_STUB_SYNC_BODY.match(compact)):
                    all_stubs = False
                    break
            else:
                compact_tail = re.sub(r"\s+", " ", tail[:80]).strip()
                if not _RE_STUB_ARROW_EXPR.match(compact_tail):
                    all_stubs = False
                    break

    resources = []
    for rm in _RE_STATIC_RESOURCES.finditer(text):
        found = [tok.strip().lower() for tok in re.findall(r'["\']([a-zA-Z0-9_-]+)["\']', rm.group(1))]
        if found:
            resources = found
            break

    return {
        "hasGetStreams": "getStreams" in text,
        "hasGetCatalog": has_catalog,
        "hasSearch": bool(re.search(r"\bsearch\s*[:=(]", text)),
        "hasOnSettings": bool(_RE_ON_SETTINGS_DEF.search(text)),
        "isCatalogStub": bool(has_catalog and all_stubs),
        "isSearchStub": False,
        "resources": resources,
        "catalogs": [],
    }


def _run_scraper_process(scraper, payload, timeout=15.0, is_cancelled=None):
    """Execute the Deno runner bundle with *payload* and return (returncode, stdout, stderr)."""
    cmd = build_scraper_command(scraper)
    if not cmd:
        return None, "", "No runtime available"

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_get_scraper_subprocess_env(),
        )
        input_data = json.dumps(payload)
        if is_cancelled is None:
            stdout, stderr = proc.communicate(input_data, timeout=timeout)
        else:
            deadline = time.monotonic() + max(0.0, float(timeout))
            first_call = True
            while True:
                if is_cancelled():
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    return -1, stdout or "", stderr or "Cancelled"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stdout, stderr = proc.communicate(
                        input_data if first_call else None,
                        timeout=min(0.1, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    first_call = False
        return proc.returncode, stdout or "", stderr or ""
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass


def inspect_scraper_capabilities(scraper, timeout=6.0):
    """
    Determine real exported handlers, manifest resources, and stub status for a scraper.
    Uses sandboxed Deno evaluation when indicators are present, falling back to static JS analysis.
    """
    code = str((scraper or {}).get("code") or "")
    static_caps = _static_inspect_js(code)
    if not code:
        return static_caps

    has_indicator = bool(
        _RE_JS_INSPECT_INDICATOR.search(code)
        or (scraper.get("catalogs") or [])
        or (scraper.get("resources") or [])
        or scraper.get("has_settings")
    )
    if not has_indicator or not is_runtime_available():
        return static_caps

    payload = {
        "action": "inspect",
        "code": code,
        "scraperId": scraper.get("id") or scraper.get("raw_id") or "inspect",
        "settings": get_scraper_settings(scraper.get("id")) if scraper.get("id") else {},
        "tmdbApiKey": tmdb_helper.DEFAULT_TMDB_API_KEY,
    }
    try:
        rc, stdout, _ = _run_scraper_process(scraper, payload, timeout=timeout)
        if rc == 0 and stdout.strip():
            for line in reversed([ln.strip() for ln in stdout.strip().split("\n") if ln.strip()]):
                if line.startswith("{") and line.endswith("}"):
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and not parsed.get("error"):
                        return {
                            "exports": parsed.get("exports") or [],
                            "hasGetStreams": bool(parsed.get("hasGetStreams")),
                            "hasGetCatalog": bool(parsed.get("hasGetCatalog")),
                            "hasSearch": bool(parsed.get("hasSearch")),
                            "hasOnSettings": bool(parsed.get("hasOnSettings")),
                            "isCatalogStub": bool(parsed.get("isCatalogStub")),
                            "isSearchStub": bool(parsed.get("isSearchStub")),
                            "resources": _normalize_manifest_resources(parsed.get("resources")),
                            "catalogs": parsed.get("catalogs") if isinstance(parsed.get("catalogs"), list) else [],
                        }
    except Exception as e:
        logger.debug(f"Capability inspection fallback for {scraper.get('name')!r}: {e}")
    return static_caps


def scraper_has_catalog(scraper):
    """
    Return True only when a Nuvio scraper genuinely implements a non-stub catalog
    and does not restrict its resources to stream-only.
    """
    if not isinstance(scraper, dict):
        return False
    code = str(scraper.get("code") or "")
    if not code.strip():
        return False

    manifest_resources = _normalize_manifest_resources(scraper.get("resources"))
    if manifest_resources and "catalog" not in manifest_resources and "home" not in manifest_resources:
        return False

    caps = scraper.get("capabilities")
    if isinstance(caps, dict) and "hasGetCatalog" in caps:
        cap_resources = _normalize_manifest_resources(caps.get("resources"))
        if cap_resources and "catalog" not in cap_resources and "home" not in cap_resources:
            return False
        if caps.get("isCatalogStub"):
            return False
        return bool(caps.get("hasGetCatalog"))

    static_caps = _static_inspect_js(code)
    cap_resources = static_caps.get("resources") or []
    if cap_resources and "catalog" not in cap_resources and "home" not in cap_resources:
        return False
    if static_caps.get("isCatalogStub"):
        return False
    return bool(static_caps.get("hasGetCatalog"))


def scraper_has_search(scraper):
    """Return True only when a Nuvio scraper exports a search handler."""
    if not isinstance(scraper, dict):
        return False
    code = str(scraper.get("code") or "")
    if not code.strip():
        return False

    caps = scraper.get("capabilities")
    if isinstance(caps, dict) and "hasSearch" in caps:
        return bool(caps.get("hasSearch")) and not bool(caps.get("isSearchStub"))
    return bool(_static_inspect_js(code).get("hasSearch"))


def scraper_has_settings(scraper):
    """Return True if a scraper genuinely defines an `onSettings` handler."""
    if not isinstance(scraper, dict):
        return False
    caps = scraper.get("capabilities")
    if isinstance(caps, dict) and "hasOnSettings" in caps:
        return bool(caps["hasOnSettings"])
    code = str(scraper.get("code") or "")
    if code.strip():
        return bool(_RE_ON_SETTINGS_DEF.search(code))
    return bool(scraper.get("has_settings"))


def is_torrent_capable_scraper(scraper):
    """Return True if a Nuvio scraper is declared or known to yield torrent/magnet streams."""
    if not isinstance(scraper, dict):
        return False
    formats = {str(f).strip().lower() for f in (scraper.get("formats") or []) if f}
    if formats & {"torrent", "magnet", "p2p"}:
        return True
    ident = " ".join([
        str(scraper.get("raw_id") or ""),
        str(scraper.get("name") or ""),
        str(scraper.get("description") or ""),
    ]).lower()
    if any(tok in ident for tok in ("torrent", "magnet", "p2p", "nyaa", "1337x", "yts", "eztv", "jackett", "prowlarr")):
        return True
    code = str(scraper.get("code") or "")
    if code and re.search(r'\binfoHash\s*:|[\'"]magnet:\?', code):
        return True
    return False


def _http_get_text(url, timeout=15.0):
    """Fetch text resource with host-paced windowing, User-Agent, and SSL context."""
    if not url:
        return ""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    )
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host:
        on_gtk_main = False
        if threading.current_thread() is threading.main_thread():
            try:
                from gi.repository import GLib
                on_gtk_main = GLib.main_depth() > 0
            except Exception:
                on_gtk_main = False
        from . import api
        with api._acquire_catalog_window(host, may_wait=not on_gtk_main) as allowed:
            if not allowed:
                raise urllib.error.URLError(f"Host window denied for {host}")
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                return resp.read().decode("utf-8", errors="replace")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# --- Repository Installation & Refresh ---

def fetch_repository_data(manifest_url):
    """
    Fetches the repository manifest and all its scraper code files.
    Returns (repo_info, list_of_scrapers).
    """
    raw_json = _http_get_text(manifest_url, timeout=12.0).lstrip("\ufeff").strip()
    manifest = json.loads(raw_json)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid plugin repository manifest format (expected JSON object)")

    repo_name = str(manifest.get("name") or "Unnamed Plugin Repository").strip()
    repo_version = str(manifest.get("version") or "1.0.0").strip()
    repo_desc = manifest.get("description")
    raw_scrapers = (
        manifest.get("scrapers")
        or manifest.get("providers")
        or manifest.get("plugins")
        or []
    )

    if not raw_scrapers or not isinstance(raw_scrapers, list):
        raise ValueError(f"Plugin repository '{repo_name}' contains no scrapers")

    base_url = manifest_url.split("?")[0]
    if base_url.endswith("/manifest.json"):
        base_url = base_url[:-len("/manifest.json")]
    base_url = base_url.rstrip("/")

    # Fetch scraper code bounded to catalog host window concurrency
    scrapers = []
    def _fetch_one_scraper(info):
        if not isinstance(info, dict) or not scraper_supported_on_this_platform(info):
            return None
        s_id = str(info.get("id") or info.get("raw_id") or "").strip()
        if not s_id:
            return None
        filename = str(info.get("filename") or info.get("url") or info.get("script") or "").strip()
        if not filename:
            return None

        if filename.startswith("http://") or filename.startswith("https://"):
            code_url = filename
        else:
            rel_path = filename.lstrip("./").lstrip("/")
            code_url = f"{base_url}/{rel_path}"

        try:
            code = _http_get_text(code_url, timeout=15.0)
        except Exception as e:
            logger.warning(f"Failed to fetch scraper code for {s_id} from {code_url}: {e}")
            return None

        if not code or not code.strip():
            logger.warning(f"Empty scraper code for {s_id} from {code_url}")
            return None

        declared_catalogs = info.get("catalogs")
        if not isinstance(declared_catalogs, list):
            declared_catalogs = []

        resources = _normalize_manifest_resources(info.get("resources"))
        scraper_id = f"{manifest_url.lower()}:{s_id}"
        entry = {
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
            "allowed_hosts": info.get("allowed_hosts") or info.get("allowedDomains") or info.get("domains") or info.get("hosts") or [],
            "resources": resources,
            "catalogs": declared_catalogs,
            "code": code,
        }
        caps = inspect_scraper_capabilities(entry)
        entry["capabilities"] = caps
        entry["has_settings"] = scraper_has_settings(entry)
        if not entry["resources"] and caps.get("resources"):
            entry["resources"] = caps["resources"]
        if not entry["catalogs"] and caps.get("catalogs") and scraper_has_catalog(entry):
            entry["catalogs"] = caps["catalogs"]
        elif entry["catalogs"] and not scraper_has_catalog(entry):
            entry["catalogs"] = []
        return entry

    with ThreadPoolExecutor(max_workers=min(len(raw_scrapers), 3)) as ex:
        futures = [ex.submit(_fetch_one_scraper, s) for s in raw_scrapers]
        for f in as_completed(futures):
            try:
                res = f.result()
                if res:
                    scrapers.append(res)
            except Exception as e:
                logger.warning(f"Unexpected error fetching scraper entry: {e}")

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
    clear_scraper_cooldown()
    database.save_plugin_repository(repo_info)
    database.save_plugin_scrapers(scrapers, replace_for_repo=manifest_url)

    logger.info(f"Installed plugin repository '{repo_info['name']}' with {len(scrapers)} scrapers")
    return True, f"Installed {repo_info['name']} ({len(scrapers)} providers)", repo_info


def remove_repository(manifest_url):
    """Remove an installed repository and its scrapers."""
    clear_scraper_cooldown()
    database.remove_plugin_repository(manifest_url)
    logger.info(f"Removed plugin repository {manifest_url}")


def refresh_repository(manifest_url):
    """
    Refresh a repository and its scrapers while preserving user toggle states.
    Returns (success: bool, message: str, repo_info: dict | None).
    """
    clear_scraper_cooldown()
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


# Nuvio refreshes installed repositories every 6 h so scraper code fixes reach
# users without a manual refresh (PLUGIN_REPOSITORY_REFRESH_INTERVAL_MS).
REPOSITORY_REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
_AUTO_REFRESH_LOCK = threading.Lock()


def is_repository_refresh_due(repo, now=None):
    try:
        last = float((repo or {}).get("last_updated") or 0)
    except (TypeError, ValueError):
        last = 0.0
    now = time.time() if now is None else now
    return last <= 0 or now - last >= REPOSITORY_REFRESH_INTERVAL_SECONDS


def refresh_due_repositories(is_cancelled=None):
    """
    Refresh installed repositories whose last update is older than 6 h.
    Runs one repository at a time on the calling (background) thread so the
    request pacing in `_http_get_text` is respected. Returns refreshed count.
    """
    if not is_plugins_enabled():
        return 0
    if not _AUTO_REFRESH_LOCK.acquire(blocking=False):
        return 0  # a refresh pass is already running
    refreshed = 0
    try:
        for repo in get_installed_repositories():
            if is_cancelled and is_cancelled():
                break
            manifest_url = (repo or {}).get("manifest_url")
            if not manifest_url or not is_repository_refresh_due(repo):
                continue
            ok, _msg, _info = refresh_repository(manifest_url)
            if ok:
                refreshed += 1
    finally:
        _AUTO_REFRESH_LOCK.release()
    return refreshed


# --- Scraper Execution ---


# Sentinel values for execute_scraper error classification.
SCRAPER_ERROR_NO_RUNTIME = "no_runtime"
SCRAPER_ERROR_NO_CODE = "no_code"
SCRAPER_ERROR_TIMEOUT = "timeout"
SCRAPER_ERROR_CRASH = "crash"
SCRAPER_ERROR_INVALID_JSON = "invalid_json"
SCRAPER_ERROR_EMPTY = "empty"


_RE_SCRAPER_SELECTED_LOG = re.compile(
    r'(?:Selected|Found\s+post|Matched(?:\s+(?:title|post|media))?|Using(?:\s+first)?\s+result)\s*:\s*'
    r'(?:["\']([^"\']+)["\']|(.+?)(?:\s*\(\s*id\s*:[^)]*\))?\s*$)',
    re.IGNORECASE,
)


def _extract_scraper_selected_title(lines: list[str]) -> str | None:
    """Extract an explicitly logged selected release/post title from scraper stdout logs."""
    for line in reversed(lines):
        if (line.startswith("[") and line.endswith("]")) or (line.startswith("{") and line.endswith("}")):
            continue
        m = _RE_SCRAPER_SELECTED_LOG.search(line)
        if m:
            candidate = (m.group(1) or m.group(2) or "").strip()
            if candidate:
                return candidate
    return None


def execute_scraper(scraper, tmdb_id, media_type, season=None, episode=None, timeout=25, expected_title=None):
    """
    Executes a single scraper JS code in the sandboxed runner.
    Returns a list of raw stream dictionaries.

    On failure, returns an empty list.  Callers that need to distinguish
    failure reasons can inspect the ``_scraper_error`` attribute attached to
    the returned list object.  Possible values:

      - ``SCRAPER_ERROR_NO_RUNTIME`` – sandbox-capable Deno not found on the system
      - ``SCRAPER_ERROR_NO_CODE``    – scraper has no downloaded code
      - ``SCRAPER_ERROR_TIMEOUT``    – JS process exceeded *timeout* seconds
      - ``SCRAPER_ERROR_CRASH``      – runner exited with non-zero status
      - ``SCRAPER_ERROR_INVALID_JSON`` – runner produced unparseable output
      - ``SCRAPER_ERROR_EMPTY``      – runner returned a valid empty array

    A normal non-empty result list has ``_scraper_error = None``.
    """
    def _err(reason, streams=None):
        # Use a tagged list subclass so the caller can distinguish failure
        # reasons without breaking `isinstance(result, list)` checks.
        class _ErrorList(list):
            pass

        result = _ErrorList(streams or [])
        result._scraper_error = reason
        return result

    cmd = build_scraper_command(scraper)
    if not cmd:
        logger.warning(
            "Cannot execute scraper %r: No sandbox-capable Deno runtime found on this system.",
            scraper.get("name"),
        )
        return _err(SCRAPER_ERROR_NO_RUNTIME)

    code = scraper.get("code")
    if not code:
        logger.warning(
            "Scraper %r has no downloaded code; refresh its repository.",
            scraper.get("name"),
        )
        return _err(SCRAPER_ERROR_NO_CODE)

    tmdb_id_str = plugin_content_id(tmdb_id, season=season, episode=episode)
    if not tmdb_id_str or tmdb_id_str.lower() in ("none", "null"):
        return _err(SCRAPER_ERROR_EMPTY)

    norm_type = normalize_media_type(media_type)
    js_media_type = js_plugin_media_type(
        norm_type,
        scraper.get("supported_types"),
        season=season,
        episode=episode,
    )
    # Scraper repositories are third-party code. Keep user-configured keys in
    # the application and pass only the public built-in key for compatibility.
    tmdb_key = tmdb_helper.DEFAULT_TMDB_API_KEY

    payload = {
        "action": "getStreams",
        "code": code,
        "scraperId": scraper.get("id"),
        "tmdbId": tmdb_id_str,
        "mediaType": js_media_type,
        "season": season,
        "episode": episode,
        "settings": get_scraper_settings(scraper.get("id")),
        "tmdbApiKey": tmdb_key,
    }

    def _unwrap_items(parsed):
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("streams", "results", "data"):
                val = parsed.get(key)
                if isinstance(val, list):
                    return val
        return None

    proc = None
    scraper_id = scraper.get("id")
    started_at = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_get_scraper_subprocess_env(),
        )
        input_data = json.dumps(payload)
        stdout, stderr = proc.communicate(input_data, timeout=timeout)

        if proc.returncode != 0:
            if stderr:
                logger.warning(
                    "Scraper %r exited with code %d. stderr: %s",
                    scraper.get("name"),
                    proc.returncode,
                    stderr[:300],
                )
            else:
                logger.warning(
                    "Scraper %r exited with code %d (no stderr).",
                    scraper.get("name"),
                    proc.returncode,
                )
            if not stdout.strip():
                _record_scraper_failure(scraper_id, SCRAPER_ERROR_CRASH)
                return _err(SCRAPER_ERROR_CRASH)

        # The runner outputs the JSON array on the last line of stdout.
        lines = [line.strip() for line in stdout.strip().split("\n") if line.strip()]
        if not lines:
            logger.debug(
                "Scraper %r produced no output for TMDB %s (%s).",
                scraper.get("name"),
                tmdb_id,
                norm_type,
            )
            _record_scraper_success(scraper_id, elapsed=time.monotonic() - started_at, stream_count=0)
            return _err(SCRAPER_ERROR_EMPTY)

        # Find the last valid JSON line.
        selected_title = _extract_scraper_selected_title(lines)

        def _raw_items_include_torrent(items):
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_url = item.get("url")
                if isinstance(raw_url, dict):
                    raw_url = raw_url.get("url")
                url_text = str(raw_url or "").strip().lower()
                if (
                    item.get("infoHash")
                    or item.get("info_hash")
                    or item.get("magnet")
                    or str(item.get("stream_type") or "").lower() == "torrent"
                    or url_text.startswith("magnet:")
                    or url_text.split("?", 1)[0].endswith(".torrent")
                    or (item.get("is_http") is False and (item.get("seeders") or item.get("seeds")))
                ):
                    return True
            return False

        for line in reversed(lines):
            if (line.startswith("[") and line.endswith("]")) or (line.startswith("{") and line.endswith("}")):
                try:
                    items = _unwrap_items(json.loads(line))
                    if items is not None:
                        if (
                            expected_title
                            and selected_title
                            and items
                            and not is_torrent_capable_scraper(scraper)
                        ):
                            from . import api
                            selected_title_matches = api.strict_stream_title_matches(
                                expected_title,
                                selected_title,
                                extra_ignored_words={
                                    get_scraper_provider_name(scraper),
                                    scraper.get("name") or "",
                                },
                            )
                            if not selected_title_matches:
                                logger.warning(
                                    "Discarding mismatched direct results from scraper %r: selected %r for expected %r",
                                    scraper.get("name"),
                                    selected_title,
                                    expected_title,
                                )
                                items = [item for item in items if _raw_items_include_torrent([item])]
                                if not items:
                                    return _err(SCRAPER_ERROR_EMPTY)
                        formatted = _format_scraper_results(items, scraper, queried_tmdb_id=tmdb_id)
                        _record_scraper_success(
                            scraper_id,
                            elapsed=time.monotonic() - started_at,
                            stream_count=len(formatted),
                        )
                        if not formatted:
                            return _err(SCRAPER_ERROR_EMPTY, formatted)
                        return formatted
                except json.JSONDecodeError:
                    pass

        # Fallback: try to parse entire stdout as JSON.
        try:
            items = _unwrap_items(json.loads(stdout.strip()))
            if items is not None:
                formatted = _format_scraper_results(items, scraper, queried_tmdb_id=tmdb_id)
                _record_scraper_success(
                    scraper_id,
                    elapsed=time.monotonic() - started_at,
                    stream_count=len(formatted),
                )
                if not formatted:
                    return _err(SCRAPER_ERROR_EMPTY, formatted)
                return formatted
        except json.JSONDecodeError:
            pass

        logger.warning(
            "Scraper %r returned output that could not be parsed as a JSON array. "
            "First 200 chars: %r",
            scraper.get("name"),
            stdout[:200],
        )
        _record_scraper_failure(scraper_id, SCRAPER_ERROR_INVALID_JSON)
        return _err(SCRAPER_ERROR_INVALID_JSON)

    except subprocess.TimeoutExpired:
        try:
            if proc:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass
        logger.info(
            "Scraper %r timed out after %ds for TMDB %s (%s).",
            scraper.get("name"),
            timeout,
            tmdb_id,
            norm_type,
        )
        _record_scraper_failure(scraper_id, SCRAPER_ERROR_TIMEOUT)
        return _err(SCRAPER_ERROR_TIMEOUT)
    except Exception as e:
        logger.warning(
            "Unexpected error executing scraper %r: %s: %s",
            scraper.get("name"),
            type(e).__name__,
            e,
        )
        _record_scraper_failure(scraper_id, SCRAPER_ERROR_CRASH)
        return _err(SCRAPER_ERROR_CRASH)
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass


_INVISIBLE_CHARS_RE = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]+")


def _format_scraper_results(raw_items, scraper, queried_tmdb_id=None):
    """Convert raw scraper output into PopcornBox stream dictionary entries."""
    formatted = []
    provider_name = get_scraper_provider_name(scraper)
    provider_names = [provider_name]
    for duplicate_name in scraper.get("_duplicate_provider_names", []):
        if duplicate_name and duplicate_name not in provider_names:
            provider_names.append(duplicate_name)
    default_name = scraper.get("name") or "Plugin"

    def _text(value):
        if value is None:
            return ""
        return _INVISIBLE_CHARS_RE.sub("", str(value)).strip()

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        url = item.get("url")
        if isinstance(url, dict):
            url = url.get("url")
        url = _text(url)
        magnet = _text(item.get("magnet"))
        if not url and magnet:
            url = magnet
        external_url = _text(item.get("externalUrl"))
        info_hash = _text(item.get("infoHash"))
        if not info_hash and magnet.lower().startswith("magnet:"):
            m_hash = re.search(r"xt=urn:btih:([A-Za-z0-9]{32,40})", magnet, re.I)
            if m_hash:
                info_hash = m_hash.group(1)
        yt_id = _text(item.get("ytId"))
        # Nuvio scrapers may return direct URLs, magnet URIs, torrent hashes,
        # external links, or YouTube IDs. Accept every playable source shape here.
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

        behavior_hints = item.get("behaviorHints")
        if not isinstance(behavior_hints, dict):
            behavior_hints = {}
        else:
            behavior_hints = dict(behavior_hints)

        headers = item.get("headers")
        if isinstance(headers, dict):
            headers = dict(headers)
        else:
            headers = {}
        if not headers:
            bh_req = (behavior_hints.get("proxyHeaders") or {}).get("request") if isinstance(behavior_hints.get("proxyHeaders"), dict) else None
            if isinstance(bh_req, dict) and bh_req:
                headers = dict(bh_req)
            elif isinstance(behavior_hints.get("headers"), dict) and behavior_hints.get("headers"):
                headers = dict(behavior_hints["headers"])

        raw_subtitles = item.get("subtitles")
        subtitles = []
        if isinstance(raw_subtitles, list):
            for sub in raw_subtitles:
                if isinstance(sub, str) and sub.strip():
                    subtitles.append({"url": sub.strip(), "lang": "und", "language": "und"})
                elif isinstance(sub, dict):
                    sub_url = _text(sub.get("url") or sub.get("src"))
                    if not sub_url:
                        continue
                    sub_lang = _text(sub.get("lang") or sub.get("language") or sub.get("srclang") or "und")
                    sub_label = _text(sub.get("label") or sub.get("name") or sub.get("title") or sub_lang)
                    s_entry = dict(sub)
                    s_entry["url"] = sub_url
                    s_entry.setdefault("lang", sub_lang)
                    s_entry.setdefault("language", sub_lang)
                    if sub_label:
                        s_entry.setdefault("label", sub_label)
                        s_entry.setdefault("name", sub_label)
                    subtitles.append(s_entry)

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

        raw_filename = _text(item.get("filename"))
        if raw_filename and not behavior_hints.get("filename"):
            behavior_hints["filename"] = raw_filename
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
            "addon_names": provider_names,
            "is_nuvio_plugin": True,
            "plugin_scraper_id": scraper.get("id"),
            # Record the TMDB ID that was used to query this scraper so the
            # stream layer can discard results cached under a different item.
            "plugin_scraper_tmdb_id": str(queried_tmdb_id) if queried_tmdb_id is not None else None,
        })

    return formatted


def get_scraper_on_settings(scraper_id, timeout=10.0):
    """
    Query a scraper's `onSettings` handler in the sandboxed runner to retrieve
    its dynamic configuration UI fields. Returns a list of field dicts or [].
    """
    scraper = get_scraper(scraper_id)
    if not scraper or not scraper.get("code") or not is_runtime_available():
        return []
    payload = {
        "action": "onSettings",
        "code": scraper.get("code"),
        "scraperId": scraper.get("id"),
        "settings": get_scraper_settings(scraper.get("id")),
        "tmdbApiKey": tmdb_helper.DEFAULT_TMDB_API_KEY,
    }
    try:
        rc, stdout, _ = _run_scraper_process(scraper, payload, timeout=timeout)
        if rc == 0 and stdout.strip():
            for line in reversed([ln.strip() for ln in stdout.strip().split("\n") if ln.strip()]):
                if (line.startswith("[") and line.endswith("]")) or (line.startswith("{") and line.endswith("}")):
                    parsed = json.loads(line)
                    if isinstance(parsed, list):
                        return [x for x in parsed if isinstance(x, dict)]
                    if isinstance(parsed, dict):
                        for key in ("fields", "settings", "data", "items"):
                            val = parsed.get(key)
                            if isinstance(val, list):
                                return [x for x in val if isinstance(x, dict)]
    except Exception as e:
        logger.debug(f"Failed retrieving onSettings for {scraper.get('name')!r}: {e}")
    return []


def _pick_test_target(scraper):
    """Select an appropriate test (tmdb_id, media_type, season, episode) for *scraper*."""
    raw_types = [str(t).strip().lower() for t in (scraper.get("supported_types") or ["movie", "tv"])]
    norm_types = [normalize_media_type(t) for t in raw_types] or ["movie", "tv"]
    langs = {str(l).strip().lower() for l in (scraper.get("content_language") or []) if l}
    ident = " ".join([
        str(scraper.get("name") or ""),
        str(scraper.get("raw_id") or ""),
        str(scraper.get("description") or ""),
    ]).lower()

    has_movie = "movie" in norm_types
    has_tv = "tv" in norm_types

    is_anime = "anime" in raw_types or "anime" in ident or (langs == {"ja"} and has_tv)
    if is_anime:
        if has_tv:
            return "1429", "tv", 1, 1  # Attack on Titan S01E01
        return "129", "movie", None, None  # Spirited Away

    is_kdrama = (langs == {"ko"}) or "kdrama" in ident or "k-drama" in ident or "korean" in ident
    if is_kdrama:
        if has_tv:
            return "93405", "tv", 1, 1  # Squid Game S01E01
        return "496243", "movie", None, None  # Parasite

    is_indian = bool(langs & {"hi", "ta", "te", "ml", "bn"}) and not ("en" in langs)
    if not is_indian:
        is_indian = any(tok in ident for tok in ("hindi", "desi", "bollywood", "einthusan", "gram"))
    if is_indian:
        if has_movie:
            return "20453", "movie", None, None  # 3 Idiots
        return "79026", "tv", 1, 1  # Mirzapur S01E01

    if has_movie:
        return "603", "movie", None, None  # The Matrix
    return "1399", "tv", 1, 1  # Game of Thrones S01E01


def test_scraper(scraper_id):
    """
    Tests a scraper against a matching title for its supported types/languages.
    Returns (success: bool, stream_count: int, error_msg: str | None).
    """
    scraper = get_scraper(scraper_id)
    if not scraper:
        return False, 0, "Provider not found"
    if not is_runtime_available():
        return False, 0, "No sandbox-capable Deno runtime is available to run this provider"
    if not scraper.get("code"):
        return False, 0, "This provider has no downloaded scraper code; refresh its repository"

    clear_scraper_cooldown(scraper_id)
    tmdb_id, media_type, season, episode = _pick_test_target(scraper)

    try:
        streams = execute_scraper(scraper, tmdb_id, media_type, season=season, episode=episode, timeout=20)
        err = getattr(streams, "_scraper_error", None)
        if err in (SCRAPER_ERROR_CRASH, SCRAPER_ERROR_TIMEOUT, SCRAPER_ERROR_INVALID_JSON, SCRAPER_ERROR_NO_RUNTIME, SCRAPER_ERROR_NO_CODE):
            return False, 0, f"Scraper test failed ({err})"
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

    scrapers = [
        s for s in deduplicate_scrapers(get_enabled_scrapers_for_type(media_type))
        if not is_scraper_in_cooldown(s.get("id"))
    ]
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


# --- Catalog Integration (Plugin-Declared Only; Zero False Positives) ---

_NUVIO_CATALOG_PREFIX = "nuvio://catalog/"


def catalog_url(scraper_id: str) -> str:
    """Return the virtual catalog URI for an enabled Nuvio plugin provider."""
    return f"{_NUVIO_CATALOG_PREFIX}{urllib.parse.quote(str(scraper_id or ''), safe='')}"


def scraper_id_from_catalog_url(raw_url: str) -> str:
    """Extract the scraper ID from a nuvio://catalog/<id> URI."""
    if not isinstance(raw_url, str) or not raw_url.startswith(_NUVIO_CATALOG_PREFIX):
        return ""
    encoded = raw_url[len(_NUVIO_CATALOG_PREFIX):].split("/", 1)[0]
    return urllib.parse.unquote(encoded)


def _catalog_type_matches(scraper, media_type: str) -> bool:
    norm = str(media_type or "all").strip().lower()
    if norm in ("", "all"):
        return True
    if norm == "movie_series":
        return scraper_supports_type(scraper, "movie") or scraper_supports_type(scraper, "tv")
    if norm == "anime":
        raw_types = [str(t).lower() for t in (scraper.get("supported_types") or [])]
        langs = [str(l).lower() for l in (scraper.get("content_language") or [])]
        name_l = str(scraper.get("name") or "").lower()
        return "anime" in raw_types or "anime" in name_l or ("ja" in langs and "tv" in raw_types)
    return scraper_supports_type(scraper, norm)


def _scraper_app_media_type(scraper, requested_type: str = "all") -> str:
    req = str(requested_type or "all").strip().lower()
    if req in ("movie", "series", "anime"):
        return req
    raw_types = [str(t).lower() for t in (scraper.get("supported_types") or ["movie", "tv"])]
    if "anime" in raw_types and "movie" not in raw_types and "tv" not in raw_types:
        return "anime"
    if "movie" in raw_types and ("tv" in raw_types or "series" in raw_types):
        return "all"
    if "tv" in raw_types or "series" in raw_types:
        return "series"
    return "movie"


def _resolve_scraper_catalog_entries(scraper):
    """Return raw declared catalog dicts from manifest or JS-inspected capabilities."""
    declared = scraper.get("catalogs")
    if isinstance(declared, list) and declared:
        return declared
    caps = scraper.get("capabilities")
    if isinstance(caps, dict) and isinstance(caps.get("catalogs"), list) and caps.get("catalogs"):
        return caps["catalogs"]
    return []


def get_catalogs(media_type: str = "all") -> list[dict]:
    """
    Return browsable virtual catalogs for enabled Nuvio plugins with real,
    non-stub catalog handlers. Search-only providers are exposed separately.
    """
    if not is_plugins_enabled():
        return []

    norm_type = str(media_type or "all").strip().lower()
    catalogs = []
    seen = set()

    for scraper in get_all_scrapers():
        if not isinstance(scraper, dict) or not scraper.get("enabled", True):
            continue
        has_catalog = scraper_has_catalog(scraper)
        has_search = scraper_has_search(scraper)
        if not has_catalog:
            continue
        if not _catalog_type_matches(scraper, norm_type):
            continue

        s_id = str(scraper.get("id") or "").strip()
        if not s_id:
            continue
        s_name = str(scraper.get("name") or s_id).strip()
        m_url = catalog_url(s_id)
        app_type = _scraper_app_media_type(scraper, norm_type)
        default_genres = database.get_available_genres(app_type if app_type != "all" else "movie")

        declared = _resolve_scraper_catalog_entries(scraper)
        sections = []
        if declared:
            for entry in declared:
                if not isinstance(entry, dict):
                    continue
                c_id = str(entry.get("id") or entry.get("catalog_id") or "").strip()
                c_name = str(entry.get("name") or entry.get("catalog_name") or c_id.title()).strip()
                c_type = str(entry.get("type") or app_type).strip().lower()
                if c_type == "tv":
                    c_type = "series"
                if norm_type in ("movie", "series", "anime") and c_type not in ("", "all", norm_type):
                    if not (norm_type == "movie_series" and c_type in ("movie", "series")):
                        continue
                if not c_id:
                    continue
                sec_genres = entry.get("genres")
                if not isinstance(sec_genres, list):
                    sec_genres = default_genres
                sec_extra = entry.get("extraSupported")
                if not isinstance(sec_extra, list):
                    sec_extra = ["genre", "year", "skip"]
                    if has_search:
                        sec_extra.append("search")
                elif not has_search:
                    sec_extra = [
                        extra for extra in sec_extra
                        if str(
                            (extra.get("name") if isinstance(extra, dict) else extra) or ""
                        ).strip().casefold() != "search"
                    ]
                sections.append((c_id, c_name, c_type or app_type, sec_genres, sec_extra))
        else:
            # Plugins without sections still get a virtual search target. Only
            # advertise search when the runtime can invoke an actual handler.
            sec_extra = ["genre", "year", "skip"]
            if has_search:
                sec_extra.append("search")
            sections.append(("popular", "Catalog", app_type, default_genres, sec_extra))

        for cid, cname, sec_type, sec_genres, sec_extra in sections:
            key = (s_id, cid, sec_type)
            if key in seen:
                continue
            seen.add(key)
            catalogs.append({
                "addon_name": s_name,
                "repository_url": scraper.get("repository_url") or "",
                "manifest_url": m_url,
                "plugin_id": s_id,
                "catalog_id": cid,
                "catalog_name": cname,
                "display_name": f"{s_name} - {cname}",
                "genres": list(sec_genres),
                "extra": [
                    {"name": "genre", "isRequired": False, "options": list(sec_genres)},
                    {"name": "year", "isRequired": False},
                ],
                "extraSupported": list(sec_extra),
                "type": sec_type,
                "supports_pagination": True,
                "_source": "nuvio_plugin",
            })

    return catalogs


def get_search_catalogs(media_type: str = "all") -> list[dict]:
    """Return enabled Nuvio search targets without exposing search-only plugins to Discover."""
    if not is_plugins_enabled():
        return []

    norm_type = str(media_type or "all").strip().lower()
    catalogs = []
    seen = set()
    for cat in get_catalogs(media_type):
        extras = cat.get("extraSupported") or []
        if any(str(extra or "").strip().casefold() == "search" for extra in extras):
            key = (cat.get("manifest_url"), cat.get("catalog_id"), cat.get("type"))
            seen.add(key)
            catalogs.append(cat)

    for scraper in get_all_scrapers():
        if not isinstance(scraper, dict) or not scraper.get("enabled", True):
            continue
        if scraper_has_catalog(scraper) or not scraper_has_search(scraper):
            continue
        if not _catalog_type_matches(scraper, norm_type):
            continue

        scraper_id = str(scraper.get("id") or "").strip()
        if not scraper_id:
            continue
        app_type = _scraper_app_media_type(scraper, norm_type)
        declared = _resolve_scraper_catalog_entries(scraper)
        sections = []
        if isinstance(declared, list) and declared:
            for entry in declared:
                if not isinstance(entry, dict):
                    continue
                catalog_id = str(entry.get("id") or entry.get("catalog_id") or "").strip()
                catalog_type = str(entry.get("type") or app_type).strip().lower()
                if catalog_type == "tv":
                    catalog_type = "series"
                if not catalog_id:
                    continue
                if norm_type in ("movie", "series", "anime") and catalog_type not in ("", "all", norm_type):
                    continue
                sections.append((
                    catalog_id,
                    str(entry.get("name") or entry.get("catalog_name") or catalog_id.title()).strip(),
                    catalog_type or app_type,
                ))
        else:
            sections.append(("popular", "Search", app_type))

        for catalog_id, catalog_name, catalog_type in sections:
            key = (catalog_url(scraper_id), catalog_id, catalog_type)
            if key in seen:
                continue
            seen.add(key)
            name = str(scraper.get("name") or scraper_id).strip()
            catalogs.append({
                "addon_name": name,
                "repository_url": scraper.get("repository_url") or "",
                "manifest_url": catalog_url(scraper_id),
                "plugin_id": scraper_id,
                "catalog_id": catalog_id,
                "catalog_name": catalog_name,
                "display_name": f"{name} - {catalog_name}",
                "extra": [{"name": "search", "isRequired": False}],
                "extraSupported": ["search"],
                "type": catalog_type,
                "supports_pagination": True,
                "_source": "nuvio_plugin",
            })

    return catalogs


def execute_scraper_catalog(
    scraper,
    catalog_id="popular",
    media_type="all",
    page=1,
    genre="",
    query="",
    timeout=10.0,
    year="",
    is_cancelled=None,
):
    """
    Query a Nuvio plugin's sandboxed `getCatalog` / `search` handler.
    Returns raw item list or [].
    """
    if not isinstance(scraper, dict) or not scraper.get("code"):
        return []
    if not is_runtime_available():
        return []

    query_text = str(query or "").strip()
    action = "search" if query_text else "getCatalog"
    req_type = str(media_type or "all").strip().lower()
    norm_type = normalize_media_type(req_type if req_type not in ("", "all", "movie_series") else "movie")
    page_num = max(1, int(page or 1))
    payload = {
        "action": action,
        "code": scraper.get("code"),
        "scraperId": scraper.get("id"),
        "catalogId": str(catalog_id or "popular"),
        "mediaType": norm_type,
        "page": page_num,
        "skip": (page_num - 1) * 24,
        "genre": str(genre or ""),
        "year": str(year or ""),
        "query": query_text,
        "settings": get_scraper_settings(scraper.get("id")),
        "tmdbApiKey": tmdb_helper.DEFAULT_TMDB_API_KEY,
    }
    try:
        rc, stdout, _ = _run_scraper_process(
            scraper, payload, timeout=timeout, is_cancelled=is_cancelled
        )
        if rc != 0 or not stdout.strip():
            return []
        for line in reversed([ln.strip() for ln in stdout.strip().split("\n") if ln.strip()]):
            if (line.startswith("[") and line.endswith("]")) or (line.startswith("{") and line.endswith("}")):
                parsed = json.loads(line)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    for key in ("metas", "items", "catalog", "results", "data", "streams"):
                        val = parsed.get(key)
                        if isinstance(val, list):
                            return val
    except Exception as e:
        logger.debug(f"Catalog execution failed for {scraper.get('name')!r}: {e}")
    return []


def _normalize_plugin_catalog_item(raw, media_type, provider_name):
    """Normalize a raw plugin catalog item into PopcornBox's catalog card dictionary."""
    if not isinstance(raw, dict):
        return None
    item_title = str(raw.get("title") or raw.get("name") or "").strip()
    if not item_title:
        return None

    item_id = str(raw.get("id") or "").strip()
    if not item_id and (raw.get("tmdbId") or raw.get("tmdb_id")):
        item_id = f"tmdb:{raw.get('tmdbId') or raw.get('tmdb_id')}"
    if not item_id and (raw.get("imdbId") or raw.get("imdb_id")):
        item_id = str(raw.get("imdbId") or raw.get("imdb_id")).strip()
    if not item_id:
        return None

    if database.is_adult_content_hidden():
        if raw.get("adult"):
            return None
        try:
            from . import api
            if api.is_content_filtered(raw):
                return None
        except Exception:
            pass
    if tmdb_helper.is_future_item(raw):
        return None

    poster = str(
        raw.get("poster")
        or raw.get("image")
        or raw.get("medium_cover_image")
        or raw.get("cover")
        or ""
    ).strip()
    background = str(
        raw.get("background")
        or raw.get("backdrop")
        or raw.get("banner")
        or poster
    ).strip()

    m_t = str(raw.get("type") or raw.get("mediaType") or raw.get("media_type") or media_type or "movie").lower()
    if m_t == "tv":
        m_t = "series"
    elif m_t in ("", "all"):
        m_t = "movie"

    rel_date = str(raw.get("release_date") or raw.get("releaseInfo") or "").strip()
    year_val = str(raw.get("year") or "").strip()
    if not year_val and len(rel_date) >= 4 and rel_date[:4].isdigit():
        year_val = rel_date[:4]

    rating = None
    raw_rating = raw.get("rating") if raw.get("rating") is not None else raw.get("imdbRating")
    if raw_rating not in (None, ""):
        try:
            rating = round(float(raw_rating), 1)
        except (TypeError, ValueError):
            rating = None

    genres = raw.get("genres")
    if not isinstance(genres, list):
        genres = []

    return {
        "id": item_id,
        "tmdb_id": str(raw.get("tmdbId") or raw.get("tmdb_id") or "") or (
            item_id.split(":", 1)[1] if item_id.startswith("tmdb:") else ""
        ),
        "title": item_title,
        "name": item_title,
        "type": m_t,
        "media_type": m_t,
        "poster": poster,
        "medium_cover_image": poster,
        "background": background,
        "year": year_val,
        "release_date": rel_date,
        "rating": rating,
        "imdbRating": f"{rating:.1f}" if rating is not None else "",
        "description": str(raw.get("description") or raw.get("overview") or "").strip(),
        "overview": str(raw.get("overview") or raw.get("description") or "").strip(),
        "genres": [str(g) for g in genres if g],
        "nuvio_provider_name": provider_name,
    }


def fetch_catalog_items(
    scraper_id: str,
    catalog_id: str = "popular",
    media_type: str = "all",
    genre: str = "",
    page: int = 1,
    limit: int = 24,
    query: str = None,
    is_cancelled=None,
    year: str = None,
    extra_filters: dict = None,
) -> list[dict]:
    """
    Fetch catalog items exclusively from an enabled Nuvio plugin's sandboxed
    `getCatalog` / `search` handler. Never falls back to synthetic TMDB rows.
    """
    if is_cancelled and is_cancelled():
        return []
    if not is_plugins_enabled():
        return []

    scraper = get_scraper(scraper_id)
    if not scraper or not scraper.get("enabled", True):
        return []
    query_text = str(query or "").strip()
    if query_text:
        if not scraper_has_search(scraper):
            return []
    elif not scraper_has_catalog(scraper):
        return []

    provider_name = scraper.get("name") or "Nuvio"
    year_val = year or (extra_filters.get("year") if isinstance(extra_filters, dict) else None)
    raw_plugin_items = execute_scraper_catalog(
        scraper,
        catalog_id=catalog_id,
        media_type=media_type,
        page=page,
        genre=genre,
        query=query_text,
        year=str(year_val or ""),
        is_cancelled=is_cancelled,
    )
    if not raw_plugin_items or (is_cancelled and is_cancelled()):
        return []

    normalized = []
    seen_ids = set()
    for raw in raw_plugin_items:
        if is_cancelled and is_cancelled():
            break
        item = _normalize_plugin_catalog_item(raw, media_type, provider_name)
        if item and item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            normalized.append(item)
            if len(normalized) >= limit:
                break

    return normalized
