"""
CloudStream Plugins & Addons Support for PopcornBox.
Handles CloudStream repository manifests, plugin discovery, SHA-256 package verification,
.cs3 archive inspection, safe atomic package management, and built-in cross-platform
provider adapters (e.g. KickTR and extensible CloudStream providers).
"""

import os
import re
import io
import sys
import json
import time
import ssl
import shutil
import random
import string
import subprocess
import http.cookiejar
import hashlib
import zipfile
import logging
import uuid
import codecs
import base64
import threading
import html as html_lib
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed, wait

from . import database

logger = logging.getLogger("PopcornBox.CloudStream")

_MAIN_PAGE_CACHE = {}
_MAIN_PAGE_CACHE_LOCK = threading.Lock()
_MAIN_PAGE_CACHE_TTL_SECONDS = 300

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

ROUTE_PREFIX = "cloudstream:"

try:
    _SSL_CONTEXT = ssl.create_default_context()
except Exception:
    _SSL_CONTEXT = None


# --- HTTP Fetch Helper ---

def _safe_url(url):
    """
    Ensures URL path and query contain no unescaped spaces or control characters,
    preventing Python 3.12+ urllib.request 'URL can't contain control characters' ValueError.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
        clean_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/:@!$&'()*+,;=-_.~%")
        clean_query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&?/:@!$'()*+,;-_.~%")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, clean_path, clean_query, parts.fragment))
    except Exception:
        return str(url).strip().replace(" ", "%20")


def _convert_raw_git_url(url):
    """Convert raw.githubusercontent.com URLs to cdn.jsdelivr.net mirror (CloudStream reference behavior)."""
    m = re.match(r"^https://raw\.githubusercontent\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)/(.*)$", str(url or ""))
    if not m:
        return None
    user, repo, rest = m.groups()
    parts = rest.split("/", 1)
    branch = parts[0]
    path = parts[1] if len(parts) > 1 else ""
    return f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/{path}"


def _http_get_text(url, headers=None, timeout=12.0):
    if not url:
        return ""
    url = _safe_url(url)
    req_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        cdn_url = _convert_raw_git_url(url)
        if cdn_url:
            try:
                cdn_req = urllib.request.Request(_safe_url(cdn_url), headers=req_headers)
                with urllib.request.urlopen(cdn_req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except Exception:
                pass
        err_str = str(e).upper()
        # Trigger DoH bypass only for SSL/TLS errors or DNS-related network
        # failures (gaierror is wrapped inside URLError by urllib). Avoid
        # triggering for HTTP-level errors (403, 404, 503…) which are
        # genuine server responses and don't benefit from DoH.
        is_ssl_error = "UNEXPECTED_EOF" in err_str or "EOF OCCURRED" in err_str or "SSL" in err_str
        is_dns_error = isinstance(e, urllib.error.URLError) and not isinstance(
            getattr(e, "code", None), int
        )
        if is_ssl_error or is_dns_error:
            try:
                try:
                    from .api import _doh_fetch
                except (ImportError, ValueError):
                    from src.api import _doh_fetch
                doh_data = _doh_fetch(url, headers=headers, timeout=timeout)
                if doh_data is not None:
                    return doh_data.decode("utf-8", errors="replace")
            except Exception:
                pass
        raise


def _http_get_bytes(url, headers=None, timeout=25.0):
    if not url:
        return b""
    url = _safe_url(url)
    req_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.read()
    except Exception as e:
        cdn_url = _convert_raw_git_url(url)
        if cdn_url:
            try:
                cdn_req = urllib.request.Request(_safe_url(cdn_url), headers=req_headers)
                with urllib.request.urlopen(cdn_req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                    return resp.read()
            except Exception:
                pass
        err_str = str(e).upper()
        # Trigger DoH bypass only for SSL/TLS errors or DNS-related network
        # failures (gaierror is wrapped inside URLError by urllib). Avoid
        # triggering for HTTP-level errors (403, 404, 503…) which are
        # genuine server responses and don't benefit from DoH.
        is_ssl_error = "UNEXPECTED_EOF" in err_str or "EOF OCCURRED" in err_str or "SSL" in err_str
        is_dns_error = isinstance(e, urllib.error.URLError) and not isinstance(
            getattr(e, "code", None), int
        )
        if is_ssl_error or is_dns_error:
            try:
                try:
                    from .api import _doh_fetch
                except (ImportError, ValueError):
                    from src.api import _doh_fetch
                doh_data = _doh_fetch(url, headers=headers, timeout=timeout)
                if doh_data is not None:
                    return doh_data
            except Exception:
                pass
        raise


def _resolve_short_link(url, timeout=10.0):
    url = _safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
            if loc:
                return loc.strip()
        raise


# --- URL Normalization & Resolution ---

def normalize_cloudstream_repository_url(raw_url):
    """
    Normalizes user-entered CloudStream repository URLs into a direct repo.json URL.
    Handles cloudstreamrepo://, cs.repo/?..., short links (!XYZ, cutt.ly),
    github repo URLs, and raw github paths.
    """
    if not raw_url:
        raise ValueError("CloudStream repository URL cannot be empty")

    value = raw_url.strip()
    if value.startswith("cloudstreamrepo://"):
        value = value[len("cloudstreamrepo://"):]
    for prefix in ("https://cs.repo/?", "http://cs.repo/?"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.strip()

    # Short links: !code -> py.md/code; or alphanumeric slug -> cutt.ly
    if value.startswith("!"):
        slug = value.lstrip("!")
        try:
            value = _resolve_short_link(f"https://py.md/{slug}")
        except Exception as e:
            logger.debug(f"Failed to resolve py.md short link: {e}")
            value = f"https://py.md/{slug}"
    elif re.match(r"^[a-zA-Z0-9!_-]+$", value) and not value.startswith("http"):
        try:
            value = _resolve_short_link(f"https://cutt.ly/{value}")
        except Exception as e:
            logger.debug(f"Failed to resolve cutt.ly short link: {e}")
            value = f"https://cutt.ly/{value}"

    if not (value.startswith("http://") or value.startswith("https://")):
        value = f"https://{value}"

    value = value.split("#")[0].strip()

    # GitHub normalization
    gh_prefix = "https://github.com/"
    if value.lower().startswith(gh_prefix):
        rest = value[len(gh_prefix):].strip("/")
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 2:
            raise ValueError("Invalid GitHub repository URL")
        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        if len(parts) >= 5 and parts[2] in ("blob", "raw"):
            branch = parts[3]
            subpath = "/".join(parts[4:])
            value = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{subpath}"
        elif len(parts) == 2:
            value = f"https://raw.githubusercontent.com/{owner}/{repo}/master/repo.json"

    # raw.githubusercontent refs/heads stripping
    raw_gh_prefix = "https://raw.githubusercontent.com/"
    if value.lower().startswith(raw_gh_prefix):
        rest = value[len(raw_gh_prefix):].strip("/")
        parts = [p for p in rest.split("/") if p]
        if len(parts) >= 6 and parts[2] == "refs" and parts[3] == "heads":
            branch = parts[4]
            subpath = "/".join(parts[5:])
            value = f"{raw_gh_prefix}{parts[0]}/{parts[1]}/{branch}/{subpath}"

    without_query = value.split("?")[0]
    last_segment = without_query.rstrip("/").split("/")[-1]
    if "." not in last_segment:
        value = value.rstrip("/") + "/repo.json"

    return _normalize_http_url_path(value)


def resolve_cloudstream_url(base_url, candidate):
    """Resolve a possibly relative URL against a base CloudStream URL."""
    value = str(candidate).strip()
    if not value:
        raise ValueError("Candidate URL cannot be blank")

    if value.startswith("https://") or value.startswith("http://"):
        return _normalize_http_url_path(value)
    if value.startswith("//"):
        scheme = base_url.split("://")[0] if "://" in base_url else "https"
        return _normalize_http_url_path(f"{scheme}:{value}")

    base = base_url.split("#")[0]
    scheme_sep = base.find("://")
    if scheme_sep <= 0:
        raise ValueError("Base URL must be absolute")

    slash_pos = base.find("/", scheme_sep + 3)
    scheme_host_end = slash_pos if slash_pos >= 0 else len(base)
    origin = base[:scheme_host_end]

    base_without_query = base.split("?")[0]
    last_slash = base_without_query.rfind("/")
    path_base = base[:max(last_slash, scheme_host_end)]

    if value.startswith("/"):
        resolved = f"{origin}{value}"
    else:
        resolved = f"{path_base}/{value}"
    return _normalize_http_url_path(resolved)


def _normalize_http_url_path(url):
    fragmentless = url.split("#")[0]
    if "?" in fragmentless:
        without_query, query = fragmentless.split("?", 1)
        clean_query = urllib.parse.quote(urllib.parse.unquote(query), safe="=&?/:@!$'()*+,;-_.~%")
        query_suffix = f"?{clean_query}"
    else:
        without_query = fragmentless
        query_suffix = ""

    scheme_sep = without_query.find("://")
    if scheme_sep <= 0:
        return url

    path_start = without_query.find("/", scheme_sep + 3)
    if path_start < 0:
        return without_query + query_suffix

    origin = without_query[:path_start]
    path_str = without_query[path_start + 1:]
    segments = []
    for s in path_str.split("/"):
        if not s or s == ".":
            continue
        if s == "..":
            if segments:
                segments.pop()
        else:
            clean_s = urllib.parse.quote(urllib.parse.unquote(s), safe=":@!$&'()*+,;=-_.~%")
            segments.append(clean_s)

    return f"{origin}/{'/'.join(segments)}{query_suffix}"


# --- Models & Enums ---

class CloudStreamPluginStatus:
    Down = 0
    Ok = 1
    Slow = 2
    BetaOnly = 3
    Unknown = -1

    @classmethod
    def from_wire(cls, val):
        if val == 0:
            return cls.Down
        if val == 1:
            return cls.Ok
        if val == 2:
            return cls.Slow
        if val == 3:
            return cls.BetaOnly
        return cls.Unknown

    @classmethod
    def can_install(cls, status):
        return status != cls.Down


class CloudStreamTvType:
    Movie = "Movie"
    TvSeries = "TvSeries"
    Anime = "Anime"
    AnimeMovie = "AnimeMovie"
    Ova = "Ova"
    Live = "Live"
    Documentary = "Documentary"
    Cartoon = "Cartoon"
    AsianDrama = "AsianDrama"
    Music = "Music"
    Torrent = "Torrent"
    NSFW = "NSFW"
    Other = "Other"

    # Reference mapping from cloudstream/library/.../MainAPI.kt enum class TvType(value: Int?)
    INT_MAP = {
        1: Movie,
        2: AnimeMovie,
        3: TvSeries,
        4: Cartoon,
        5: Anime,
        6: Ova,
        7: Torrent,
        8: Documentary,
        9: AsianDrama,
        10: Live,
        11: NSFW,
        13: Music,
    }

    @classmethod
    def from_wire(cls, val):
        if val is None:
            return cls.Other
        if isinstance(val, int):
            return cls.INT_MAP.get(val, cls.Other)
        if isinstance(val, str):
            v = val.strip().lower()
            if v.isdigit():
                return cls.INT_MAP.get(int(v), cls.Other)
            if v == "movie":
                return cls.Movie
            if v in ("tvseries", "series", "show"):
                return cls.TvSeries
            if v == "anime":
                return cls.Anime
            if v == "animemovie":
                return cls.AnimeMovie
            if v == "ova":
                return cls.Ova
            if v in ("live", "livetv"):
                return cls.Live
            if v == "documentary":
                return cls.Documentary
            if v == "cartoon":
                return cls.Cartoon
            if v in ("asiandrama", "drama"):
                return cls.AsianDrama
            if v == "music":
                return cls.Music
            if v == "torrent":
                return cls.Torrent
            if v in ("nsfw", "18+", "adult", "hentai", "xxx", "erotica"):
                return cls.NSFW
        return cls.Other

    @classmethod
    def to_popcorn_type(cls, tv_type):
        if tv_type in (cls.Movie, cls.AnimeMovie, cls.Documentary):
            return "movie"
        if tv_type in (cls.TvSeries, cls.Anime, cls.Ova, cls.Cartoon, cls.AsianDrama):
            return "series"
        if tv_type in (cls.Live, cls.Music):
            return "tv"
        if tv_type == cls.NSFW:
            return "adult"
        return "movie"


def is_plugin_nsfw(plugin_or_provider):
    """
    Returns True if the plugin or provider contains NSFW / adult content.
    Aligns with CloudStream reference logic (PluginManager.kt line 395).
    """
    if not plugin_or_provider:
        return False

    nsfw_kw = ("nsfw", "11", "adult", "18+", "hentai", "xxx", "erotica", "porn", "ecchi")

    if isinstance(plugin_or_provider, dict):
        raw_types = [str(t).strip().lower() for t in (plugin_or_provider.get("raw_tv_types") or [])]
        parsed_types = [str(t).strip().lower() for t in (plugin_or_provider.get("tv_types") or [])]
        all_types = set(raw_types + parsed_types)
        if any(t in nsfw_kw or any(k in t for k in nsfw_kw) for t in all_types):
            return True
        name = str(plugin_or_provider.get("name") or plugin_or_provider.get("internal_name") or "").lower()
        if any(k in name for k in ("18+", "hentai", "porn", "xxx", "minioppai", "nekopoi")):
            return True
        desc = str(plugin_or_provider.get("description") or "").lower()
        if any(k in desc for k in ("18+ only", "adult content", "hentai anime", "xxx videos", "porn videos")):
            return True
        return False

    # Check CloudStreamProvider instance
    if hasattr(plugin_or_provider, "supported_types"):
        st = [str(t).lower() for t in (plugin_or_provider.supported_types or [])]
        if any(t in nsfw_kw or any(k in t for k in nsfw_kw) for t in st):
            return True
    name = getattr(plugin_or_provider, "name", "").lower()
    if any(k in name for k in ("18+", "hentai", "porn", "xxx", "minioppai", "nekopoi")):
        return True
    return False


def is_plugin_pure_nsfw(plugin_or_provider):
    """
    Returns True if the plugin/provider is strictly adult-only (no non-adult types).
    """
    if not is_plugin_nsfw(plugin_or_provider):
        return False
    if isinstance(plugin_or_provider, dict):
        raw_types = [str(t).strip().lower() for t in (plugin_or_provider.get("raw_tv_types") or [])]
        parsed_types = [str(t).strip().lower() for t in (plugin_or_provider.get("tv_types") or [])]
        all_types = set(raw_types + parsed_types)
        non_nsfw = [
            t for t in all_types
            if t not in ("nsfw", "11", "adult", "18+", "hentai", "xxx", "erotica", "porn", "ecchi", "other", "others")
        ]
        return len(non_nsfw) == 0
    return False


def is_section_nsfw(section):
    """
    Returns True if catalog section is adult/18+ content.
    """
    if not section or not isinstance(section, dict):
        return False
    sec_id = str(section.get("id") or "").lower()
    sec_name = str(section.get("name") or section.get("section") or "").lower()
    keywords = ("18+", "nsfw", "adult", "erotica", "xxx", "hentai", "porn", "ecchi", "softcore")
    return any(k in sec_id or k in sec_name for k in keywords)


def cloudstream_plugin_id(repo_url, internal_name):
    norm_repo = normalize_cloudstream_repository_url(repo_url)
    val = f"{norm_repo}#{internal_name}"
    storage_key = hashlib.sha256(val.encode("utf-8")).hexdigest()
    return val, storage_key


def parse_cloudstream_file_hash(val):
    if not val or not isinstance(val, str):
        return None
    val = val.strip().lower()
    if "-" in val:
        algo, hex_str = val.split("-", 1)
        if algo != "sha256" or len(hex_str) != 64 or not all(c in "0123456789abcdef" for c in hex_str):
            return None
        return {"algorithm": algo, "hex": hex_str, "wire": val}
    if len(val) == 64 and all(c in "0123456789abcdef" for c in val):
        return {"algorithm": "sha256", "hex": val, "wire": f"sha256-{val}"}
    return None


def matches_file_hash(file_bytes, hash_obj):
    if not hash_obj:
        return True
    algo = hash_obj.get("algorithm", "").lower()
    expected_hex = hash_obj.get("hex", "").lower()
    if algo == "sha256":
        actual = hashlib.sha256(file_bytes).hexdigest().lower()
        return actual == expected_hex
    return False


# --- Package Inspection & Safety ---

def inspect_cs3_package(bytes_data):
    """
    Inspects a downloaded .cs3 zip package.
    Ensures presence of manifest.json and guards against zip slip / path traversal.
    Returns (manifest_dict, list_of_entry_names).
    """
    if len(bytes_data) < 22:
        raise ValueError("Invalid .cs3 package: file is too small")

    bio = io.BytesIO(bytes_data)
    try:
        with zipfile.ZipFile(bio, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > 512:
                raise ValueError("Invalid .cs3 package: entry count exceeds safety limit")

            total_uncompressed = 0
            entry_names = []
            for entry in infolist:
                name = entry.filename
                # Path traversal checks
                if not name or name.startswith("/") or name.startswith("\\"):
                    raise ValueError(f"Invalid .cs3 entry path: {name}")
                if re.match(r"^[A-Za-z]:", name):
                    raise ValueError(f"Drive-qualified path in .cs3: {name}")
                if any(part == ".." for part in name.replace("\\", "/").split("/")):
                    raise ValueError(f"Path traversal detected in .cs3: {name}")

                total_uncompressed += entry.file_size
                if total_uncompressed > 100 * 1024 * 1024:  # 100MB limit
                    raise ValueError(".cs3 archive exceeds maximum allowed uncompressed size")

                entry_names.append(name)

            if "manifest.json" not in entry_names:
                raise ValueError(".cs3 package is missing manifest.json")

            manifest_content = zf.read("manifest.json").decode("utf-8", errors="replace")
            manifest_json = json.loads(manifest_content)
            return manifest_json, entry_names
    except zipfile.BadZipFile as e:
        raise ValueError(f"Corrupt or invalid .cs3 ZIP archive: {e}")


def get_packages_dir():
    """Return the Path to the directory where .cs3 packages are saved."""
    base = database.BASE_DIR if hasattr(database, "BASE_DIR") else Path.home() / ".local/share/popcorn-box"
    pkg_dir = Path(base) / "cloudstream_packages"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    return pkg_dir


def save_package_atomically(storage_key, bytes_data):
    pkg_dir = get_packages_dir()
    target = pkg_dir / f"{storage_key}.cs3"
    temp = pkg_dir / f"{storage_key}.cs3.tmp"
    with open(temp, "wb") as f:
        f.write(bytes_data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, target)
    return str(target)


def delete_package(storage_key):
    pkg_dir = get_packages_dir()
    target = pkg_dir / f"{storage_key}.cs3"
    if target.exists():
        try:
            target.unlink()
        except Exception as e:
            logger.warning(f"Could not delete package {target}: {e}")


def package_exists(storage_key):
    pkg_dir = get_packages_dir()
    return (pkg_dir / f"{storage_key}.cs3").is_file()


# --- Repository Manifest & Plugin List Parsing ---

def parse_repository_manifest(raw_url, payload_text):
    """Parses repo.json into a structured manifest dictionary."""
    manifest_url = normalize_cloudstream_repository_url(raw_url)
    dto = json.loads(payload_text)
    manifest_version = dto.get("manifestVersion") or 1
    if manifest_version <= 0:
        raise ValueError("manifestVersion must be positive")

    raw_lists = dto.get("pluginLists") or []
    if not isinstance(raw_lists, list) or not raw_lists:
        raise ValueError("CloudStream repository does not contain any pluginLists")

    resolved_lists = []
    for item in raw_lists:
        if isinstance(item, str) and item.strip():
            u = resolve_cloudstream_url(manifest_url, item.strip())
            if u not in resolved_lists:
                resolved_lists.append(u)

    if not resolved_lists:
        raise ValueError("No valid plugin lists found in repository")

    name = str(dto.get("name") or "").strip()
    if not name:
        name = manifest_url.split("?")[0].rstrip("/").split("/")[-1].removesuffix(".json")

    icon_url = dto.get("iconUrl")
    if icon_url and isinstance(icon_url, str) and icon_url.strip():
        icon_url = resolve_cloudstream_url(manifest_url, icon_url.strip())
    else:
        icon_url = None

    return {
        "manifest_url": manifest_url,
        "name": name,
        "description": dto.get("description"),
        "icon_url": icon_url,
        "manifest_version": manifest_version,
        "plugin_list_urls": resolved_lists,
        "last_updated": time.time(),
        "error_message": None,
    }


def parse_plugin_list(repository_manifest_url, plugin_list_url, payload_text):
    """Parses a plugins.json list into a list of plugin metadata dicts."""
    norm_repo = normalize_cloudstream_repository_url(repository_manifest_url)
    norm_list = resolve_cloudstream_url(norm_repo, plugin_list_url)
    data = json.loads(payload_text)
    if not isinstance(data, list):
        return []

    plugins = []
    for item in data:
        if not isinstance(item, dict):
            continue
        internal_name = str(item.get("internalName") or "").strip()
        if not internal_name:
            continue
        name = str(item.get("name") or "").strip() or internal_name
        package_url = str(item.get("url") or "").strip()
        if not package_url:
            continue
        resolved_pkg_url = resolve_cloudstream_url(norm_list, package_url)

        version = item.get("version")
        if not isinstance(version, int) or version < 0:
            version = 0

        status_wire = item.get("status")
        status = CloudStreamPluginStatus.from_wire(status_wire)

        authors = []
        for a in item.get("authors") or []:
            if isinstance(a, str) and a.strip() and a.strip() not in authors:
                authors.append(a.strip())

        raw_tv_types = []
        for t in item.get("tvTypes") or []:
            t_str = str(t).strip()
            if t_str and t_str not in raw_tv_types:
                raw_tv_types.append(t_str)

        parsed_tv_types = [CloudStreamTvType.from_wire(t) for t in raw_tv_types]

        file_hash = parse_cloudstream_file_hash(item.get("fileHash"))
        file_size = item.get("fileSize") if isinstance(item.get("fileSize"), (int, float)) and item.get("fileSize") >= 0 else None

        icon_url = str(item.get("iconUrl") or "").strip()
        if icon_url:
            icon_url = resolve_cloudstream_url(norm_list, icon_url)
        else:
            icon_url = None

        plugin_id_str, storage_key = cloudstream_plugin_id(norm_repo, internal_name)

        plugins.append({
            "id": plugin_id_str,
            "storage_key": storage_key,
            "internal_name": internal_name,
            "name": name,
            "repository_manifest_url": norm_repo,
            "package_url": resolved_pkg_url,
            "version": version,
            "status": status,
            "authors": authors,
            "description": item.get("description"),
            "file_size": file_size,
            "file_hash": file_hash,
            "language": str(item.get("language") or "").strip().lower() or None,
            "raw_tv_types": raw_tv_types,
            "tv_types": parsed_tv_types,
            "icon_url": icon_url,
            "api_version": item.get("apiVersion") or 1,
            "installed_version": None,
            "enabled": False,
            "verified": False,
            "is_installed": False,
        })
    return plugins


def merge_plugin_lists(lists):
    """Merges multiple lists of plugins, keeping the highest version for duplicate IDs."""
    selected = {}
    for plugin_list in lists:
        for p in plugin_list:
            p_id = p["id"]
            existing = selected.get(p_id)
            if existing is None or p.get("version", 0) > existing.get("version", 0):
                selected[p_id] = p
    return sorted(selected.values(), key=lambda p: (p.get("name", "").lower(), p.get("internal_name", "").lower()))


# --- Route ID Helpers ---

def cloudstream_route_id(provider_id, data):
    p_id = str(provider_id).strip()
    d = str(data).strip()
    return f"{ROUTE_PREFIX}{len(p_id)}:{p_id}{d}"


def parse_cloudstream_route_id(value):
    if not isinstance(value, str) or not value.startswith(ROUTE_PREFIX):
        return None
    payload = value[len(ROUTE_PREFIX):]
    sep = payload.find(":")
    if sep <= 0:
        return None
    try:
        p_len = int(payload[:sep])
    except ValueError:
        return None
    content = payload[sep + 1:]
    if p_len <= 0 or p_len > len(content):
        return None
    provider_id = content[:p_len]
    data = content[p_len:]
    if not provider_id or not data:
        return None
    return provider_id, data


def is_cloudstream_series(title_text="", url_or_data="", catalog_type=""):
    """
    Determines if a title, data link, or catalog section represents an episodic TV series.
    """
    c_type = str(catalog_type or "").strip().lower()
    if c_type in ("series", "tvseries", "tvshow", "tv_series", "show", "anime"):
        return True
    combo = f"{title_text or ''} {url_or_data or ''}".strip()
    if not combo:
        return False
    return bool(re.search(
        r"\b(season\b|series\b|tvshow|s\d{1,2}(?:e\d{1,2})?|episodes?|ep\s*\d+)\b",
        combo,
        re.IGNORECASE,
    ))


def parse_archive_episodes(html):
    """
    Parses an intermediate episode archive page (e.g. mdrive, nexdrive, fxlinks, dflinks, modpro, file-dl)
    and returns a mapping of episode_number (int) -> list of (url, label).
    Supports episode numbers up to 4 digits and HTML tags <h1>-<h6>, <strong>, <b>, <p>, <span>.
    """
    if not html:
        return {}
    episodes = {}

    # Pattern 1: Heading above links (e.g. <h5>EP01...</h5> or -:Episodes: 01:- or <h4>Episode 1</h4> or <p><strong>Episode 150</strong></p>)
    sections = re.split(r"(?i)(?:<(?:h[1-6]|strong|b|p|span)[^>]*>\s*(?:EP|Episode|E)\.?\s*(\d{1,4})|-?:\s*Episodes?:\s*(\d{1,4})\s*:-?)", html)
    if len(sections) > 1:
        i = 1
        while i < len(sections):
            ep_str = sections[i] or sections[i+1]
            block = sections[i+2]
            if ep_str and ep_str.isdigit():
                ep_num = int(ep_str)
                links = re.findall(r"""<a\b[^>]*href=["\x27](https?://[^"\x27]+)["\x27][^>]*>(.*?)</a>""", block, re.I | re.DOTALL)
                for h, t in links:
                    h_clean = h.strip()
                    if any(k in h_clean.lower() for k in [
                        "hubcloud", "fastdl", "nexdrive", "drive", "filebee", "gdflix",
                        "pixeldrain", "only-apis", "dl.", "vcloud", "gofile", "mixdrop",
                        "streamwish", "vidhide", "streamtape", "dood"
                    ]) or any(h_clean.lower().endswith(ext) or f"{ext}?" in h_clean.lower() for ext in (".mkv", ".mp4", ".m3u8", ".mpd")):
                        if not any(existing_h == h_clean for existing_h, _ in episodes.get(ep_num, [])):
                            episodes.setdefault(ep_num, []).append((h_clean, re.sub(r"<[^>]+>", "", t).strip()))
            i += 3

    # Pattern 2: Anchors containing Episode text
    anchor_matches = re.finditer(r"""<a\b[^>]*href=["\x27](https?://[^"\x27]+)["\x27][^>]*>(.*?)</a>""", html, re.I | re.DOTALL)
    for m in anchor_matches:
        h, t = m.group(1).strip(), m.group(2)
        clean_t = re.sub(r"<[^>]+>", " ", t).strip()
        ep_m = re.search(r"\b(?:EP|Episode|E)\.?\s*(\d{1,4})\b", clean_t, re.I)
        if ep_m:
            ep_num = int(ep_m.group(1))
            if any(k in h.lower() for k in [
                "hubcloud", "fastdl", "nexdrive", "drive", "filebee", "gdflix",
                "pixeldrain", "dl.", "thenaukriadda", "fastdlserver", "vcloud",
                "gofile", "mixdrop", "streamwish", "vidhide", "streamtape", "dood"
            ]) or any(h.lower().endswith(ext) or f"{ext}?" in h.lower() for ext in (".mkv", ".mp4", ".m3u8", ".mpd")):
                if not any(existing_h == h for existing_h, _ in episodes.get(ep_num, [])):
                    episodes.setdefault(ep_num, []).append((h, clean_t))

    return episodes


# --- Built-in CloudStream Provider Architecture ---

CLOUDSTREAM_POPULAR_GENRES = [
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Music",
    "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"
]


class CloudStreamProvider:
    id = ""
    name = ""
    supported_types = ["live"]
    # Discover sections exposed by this adapter's get_main_page(). Each entry
    # names a stable catalog ID, the returned section title, and its media type.
    catalog_sections = ()

    def get_main_page(self, page=1):
        """Returns list of (section_title: str, items: list[dict])."""
        return []

    def search(self, query):
        """Returns list of dicts: provider_id, data, name, type, poster_url, background_url, description, year."""
        return []

    def load(self, data):
        """Returns metadata dict: provider_id, data, name, type, poster_url, background_url, description, year, rating_percent, tags, episodes."""
        raise NotImplementedError

    def load_links(self, data):
        """Returns list of stream dicts: name, url, quality, referer, headers, subtitles, is_hls, is_dash."""
        raise NotImplementedError


class KickTRCloudStreamProvider(CloudStreamProvider):
    """
    Turkish Kick.com Live Stream provider adapter, corresponding directly
    to Kraptor's KickTR CloudStream plugin.
    """
    id = "https://raw.githubusercontent.com/Kraptor123/cs-kraptor/master/repo.json#KickTR"
    name = "KickTR"
    supported_types = ["live", "tv"]
    catalog_sections = (
        {"id": "featured-tr", "name": "Öne çıkan Türkçe yayınlar", "section": "Öne çıkan Türkçe yayınlar", "type": "tv"},
        {"id": "top-tr", "name": "En çok izlenen Türkçe yayınlar", "section": "En çok izlenen Türkçe yayınlar", "type": "tv"},
        {"id": "chat-tr", "name": "Türkçe sohbet yayınları", "section": "Türkçe sohbet yayınları", "type": "tv"},
        {"id": "gaming-tr", "name": "Türkçe FPS yayınları", "section": "Türkçe FPS yayınları", "type": "tv"},
    )

    API_ORIGINS = ["https://kick.com", "https://web.kick.com"]
    REQUEST_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Origin": "https://kick.com",
        "Referer": "https://kick.com/",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    }

    def _fetch_api(self, path):
        for origin in self.API_ORIGINS:
            url = path if path.startswith("http") else origin + path
            try:
                text = _http_get_text(url, headers=self.REQUEST_HEADERS, timeout=10.0)
                data = json.loads(text)
                if isinstance(data, dict) and "blocked" in str(data.get("error", "")).lower():
                    continue
                return data
            except Exception as e:
                logger.debug(f"KickTR fetch attempt failed on {url}: {e}")
                continue
        raise RuntimeError(f"Kick API request failed: {path}")

    def get_main_page(self, page=1):
        if page > 1:
            return []
        categories = [
            ("Öne çıkan Türkçe yayınlar", "/api/v1/livestreams/featured?language=tr&limit=96"),
            ("En çok izlenen Türkçe yayınlar", "/api/v1/livestreams?sort=viewer_count_desc&language=tr&limit=96"),
            ("Türkçe sohbet yayınları", "/api/v1/livestreams?limit=96&sort=viewer_count_desc&language=tr&category_id=15"),
            ("Türkçe FPS yayınları", "/api/v1/livestreams?sort=featured&language=tr&tag=fps&limit=96"),
        ]
        results = []
        for title, path in categories:
            try:
                data = self._fetch_api(path)
                items = self._parse_livestreams(data)
                if items:
                    results.append((title, items))
            except Exception as e:
                logger.debug(f"KickTR main page category '{title}' failed: {e}")
        return results

    def search(self, query):
        if not query or not query.strip():
            return []
        encoded = urllib.parse.quote(query.strip())
        data = self._fetch_api(f"/api/search?searched_word={encoded}")
        channels = []
        if isinstance(data, dict):
            channels = data.get("channels") or data.get("data") or []
        elif isinstance(data, list):
            channels = data

        items = []
        for ch in channels:
            item = self._channel_to_search_item(ch)
            if item and item not in items:
                items.append(item)
        return items

    def load(self, data_str):
        slug = self._to_slug(data_str)
        encoded = urllib.parse.quote(slug)
        resp = self._fetch_api(f"/api/v2/channels/{encoded}")
        if not isinstance(resp, dict):
            raise RuntimeError(f"Invalid Kick channel response for {slug}")

        user = resp.get("user") or {}
        livestream = resp.get("livestream") or {}
        display_name = (
            user.get("username")
            or resp.get("username")
            or resp.get("slug")
            or slug
        )
        title = livestream.get("session_title")
        poster = (
            self._image_url(resp, "profile_picture", "avatar", "profile_pic")
            or self._image_url(user, "profile_picture", "avatar", "profile_pic")
        )
        background = (
            self._image_url(resp, "banner_image", "offline_banner_image", "thumbnail")
            or self._image_url(livestream, "thumbnail")
        )
        desc_parts = [p for p in [title, user.get("bio")] if p]
        description = "\n".join(desc_parts) if desc_parts else None

        cat = livestream.get("categories") or {}
        cat_name = cat.get("name") if isinstance(cat, dict) else None

        return {
            "provider_id": self.id,
            "data": f"kick:channel:{slug}",
            "name": display_name,
            "type": CloudStreamTvType.Live,
            "poster_url": poster,
            "background_url": background,
            "description": description,
            "year": None,
            "rating_percent": None,
            "tags": [cat_name] if cat_name else [],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        slug = self._to_slug(data_str)
        encoded = urllib.parse.quote(slug)
        channel_page = f"https://kick.com/{slug}"

        resp = self._fetch_api(f"/api/v2/channels/{encoded}")
        playback_url = self._extract_playback_url(resp)
        if not playback_url:
            # Fallback
            try:
                alt = self._fetch_api(f"/api/v1/stream/{encoded}")
                playback_url = self._extract_playback_url(alt)
            except Exception:
                pass

        if not playback_url:
            raise RuntimeError("Kick yayını çevrimdışı veya oynatma bağlantısı bulunamadı (Stream offline)")

        is_hls = ".m3u8" in playback_url.split("?")[0].lower()
        return [{
            "name": "Kick Live HLS",
            "url": playback_url,
            "referer": channel_page,
            "headers": {
                "Referer": channel_page,
                "Origin": "https://kick.com",
                "User-Agent": self.REQUEST_HEADERS["User-Agent"],
            },
            "subtitles": [],
            "is_hls": is_hls,
            "is_dash": False,
        }]

    def _to_slug(self, data_str):
        s = str(data_str).strip()
        if "kick:channel:" in s:
            s = s.split("kick:channel:")[-1]
        elif "kick.com/" in s:
            s = s.split("kick.com/")[-1].split("/")[0]
        s = s.split("?")[0].strip().lower()
        if not s:
            raise ValueError("Kick channel slug is missing")
        return s

    def _extract_playback_url(self, obj):
        if not isinstance(obj, dict):
            return None
        for k in ("playback_url", "playbackUrl", "source", "url"):
            val = obj.get(k)
            if isinstance(val, str) and (val.startswith("https://") or val.startswith("http://")):
                return val
        for v in obj.values():
            if isinstance(v, dict):
                res = self._extract_playback_url(v)
                if res:
                    return res
        return None

    def _image_url(self, obj, *keys):
        if not isinstance(obj, dict):
            return None
        for k in keys:
            val = obj.get(k)
            if isinstance(val, str) and val.strip():
                return val.replace("{width}", "640").replace("{height}", "360")
            elif isinstance(val, dict):
                u = val.get("url") or val.get("src")
                if isinstance(u, str) and u.strip():
                    return u.replace("{width}", "640").replace("{height}", "360")
        return None

    def _parse_livestreams(self, data):
        streams = []
        if isinstance(data, dict):
            streams = data.get("livestreams") or data.get("data") or []
        elif isinstance(data, list):
            streams = data
        items = []
        for s in streams:
            item = self._livestream_to_search_item(s)
            if item and item not in items:
                items.append(item)
        return items

    def _livestream_to_search_item(self, stream):
        if not isinstance(stream, dict):
            return None
        channel = stream.get("channel") or {}
        user = channel.get("user") or stream.get("user") or {}
        slug = channel.get("slug") or stream.get("slug") or (user.get("username", "").lower())
        if not slug:
            return None
        name = user.get("username") or channel.get("username") or slug
        title = stream.get("session_title")
        cat = stream.get("categories") or stream.get("category") or {}
        cat_name = cat.get("name") if isinstance(cat, dict) else None
        viewers = stream.get("viewer_count")
        desc_parts = [p for p in [title, cat_name, f"{viewers} izleyici" if viewers else None] if p]
        desc = " · ".join(desc_parts) if desc_parts else None
        poster = (
            self._image_url(stream, "thumbnail")
            or self._image_url(channel, "profile_picture", "avatar")
            or self._image_url(user, "profile_picture", "avatar")
        )
        return {
            "provider_id": self.id,
            "data": f"kick:channel:{slug}",
            "name": name,
            "type": CloudStreamTvType.Live,
            "poster_url": poster,
            "background_url": self._image_url(stream, "thumbnail"),
            "description": desc,
            "year": None,
        }

    def _channel_to_search_item(self, channel):
        if not isinstance(channel, dict):
            return None
        user = channel.get("user") or {}
        slug = channel.get("slug") or (user.get("username", "").lower())
        if not slug:
            return None
        name = user.get("username") or channel.get("username") or slug
        livestream = channel.get("livestream") or {}
        poster = (
            self._image_url(channel, "profile_picture", "avatar")
            or self._image_url(user, "profile_picture", "avatar")
            or self._image_url(livestream, "thumbnail")
        )
        return {
            "provider_id": self.id,
            "data": f"kick:channel:{slug}",
            "name": name,
            "type": CloudStreamTvType.Live,
            "poster_url": poster,
            "background_url": self._image_url(livestream, "thumbnail"),
            "description": livestream.get("session_title") or user.get("bio"),
            "year": None,
        }


class KickGlobalCloudStreamProvider(KickTRCloudStreamProvider):
    """Global Kick.com Live Stream provider adapter."""
    id = "cloudstream:built-in:KickGlobal"
    name = "Kick (Global)"
    supported_types = ["live", "tv"]
    catalog_sections = (
        {"id": "top", "name": "Top Livestreams", "section": "Top Livestreams", "type": "tv"},
        {"id": "featured", "name": "Featured Streams", "section": "Featured Streams", "type": "tv"},
        {"id": "chat", "name": "Just Chatting", "section": "Just Chatting", "type": "tv"},
        {"id": "gaming", "name": "Gaming Streams", "section": "Gaming Streams", "type": "tv"},
    )
    REQUEST_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://kick.com",
        "Referer": "https://kick.com/",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    }

    def get_main_page(self, page=1):
        if page > 1:
            return []
        categories = [
            ("Top Livestreams", "/api/v1/livestreams?sort=viewer_count_desc&limit=96"),
            ("Featured Streams", "/api/v1/livestreams/featured?limit=96"),
            ("Just Chatting", "/api/v1/livestreams?limit=96&sort=viewer_count_desc&category_id=15"),
            ("Gaming Streams", "/api/v1/livestreams?sort=featured&tag=fps&limit=96"),
        ]
        results = []
        for title, path in categories:
            try:
                data = self._fetch_api(path)
                items = self._parse_livestreams(data)
                if items:
                    results.append((title, items))
            except Exception as e:
                logger.debug(f"KickGlobal category '{title}' failed: {e}")
        return results


class MegaProviderCloudStreamProvider(CloudStreamProvider):
    """
    MegaProvider meta-plugin adapter.
    Syncs and registers all community CloudStream repositories from repos-db.json.
    """
    id = "https://raw.githubusercontent.com/self-similarity/MegaRepo/builds/repo.json#MegaProvider"
    name = "MegaProvider"
    supported_types = ["other"]

    REPOS_DB_URL = "https://raw.githubusercontent.com/recloudstream/cs-repos/master/repos-db.json"

    def sync_repositories(self, max_workers=10):
        """
        Fetches repos-db.json and imports all repositories in parallel.
        Returns (added_count, total_count, errors).
        """
        try:
            text = _http_get_text(self.REPOS_DB_URL, timeout=10.0)
            entries = json.loads(text)
        except Exception as e:
            logger.error(f"Failed to fetch repos-db.json: {e}")
            return 0, 0, [str(e)]

        urls = []
        for e in entries:
            u = e.get("url") if isinstance(e, dict) else e
            if isinstance(u, str) and u.startswith("http"):
                if "MegaRepo" not in u and u not in urls:
                    urls.append(u)

        added = 0
        errors = []

        def _worker(repo_url):
            try:
                ok, msg, info = add_repository(repo_url)
                return ok, repo_url, msg
            except Exception as ex:
                return False, repo_url, str(ex)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_worker, u) for u in urls]
            for fut in as_completed(futures):
                ok, u, msg = fut.result()
                if ok:
                    added += 1
                else:
                    errors.append((u, msg))

        return added, len(urls), errors

    def search(self, query):
        return []

    def load(self, data_str):
        return {
            "provider_id": self.id,
            "data": data_str,
            "name": "MegaProvider Repository Sync",
            "type": CloudStreamTvType.Other,
            "poster_url": None,
            "background_url": None,
            "description": "CloudStream MegaProvider meta-installer repository manager.",
            "year": 2026,
            "rating_percent": None,
            "tags": ["Tools", "Repositories"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        return []


class TwitchCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/recloudstream/extensions/builds/TwitchProvider.cs3#TwitchProvider"
    name = "TwitchProvider"
    supported_types = ["live", "tv"]

    def search(self, query):
        if not query or not query.strip():
            return []
        channel = query.strip().lower().replace(" ", "")
        return [{
            "provider_id": self.id,
            "data": f"twitch:channel:{channel}",
            "name": f"Twitch: {channel}",
            "type": CloudStreamTvType.Live,
            "poster_url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-640x360.jpg",
            "background_url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-1920x1080.jpg",
            "description": f"Live stream on twitch.tv/{channel}",
            "year": None,
        }]

    def load(self, data_str):
        channel = data_str.split(":")[-1].strip().lower()
        return {
            "provider_id": self.id,
            "data": data_str,
            "name": f"Twitch: {channel}",
            "type": CloudStreamTvType.Live,
            "poster_url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-640x360.jpg",
            "background_url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-1920x1080.jpg",
            "description": f"Watch {channel} live on Twitch",
            "year": None,
            "rating_percent": None,
            "tags": ["Twitch", "Gaming", "Live"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        channel = data_str.split(":")[-1].strip().lower()
        stream_url = f"https://twitch.tv/{channel}"
        return [{
            "name": f"Twitch Live ({channel})",
            "url": stream_url,
            "referer": "https://www.twitch.tv/",
            "headers": {"Referer": "https://www.twitch.tv/"},
            "subtitles": [],
            "is_hls": True,
            "is_dash": False,
        }]


def _extract_dailymotion_id(data_or_url):
    s = str(data_or_url or "").strip()
    if not s:
        return ""
    m = re.search(r"(?:/(?:embed/)?video/|dm:)([a-zA-Z0-9]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]+", s):
        return s
    return s.split("/")[-1].split(":")[-1].split("?")[0].strip()


class DailymotionCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/recloudstream/extensions/builds/DailymotionProvider.cs3#DailymotionProvider"
    name = "DailymotionProvider"
    supported_types = ["movie", "series", "tv", "other", "all"]
    catalog_sections = (
        {"id": "popular", "name": "Popular", "section": "Popular", "type": "movie", "supports_pagination": True},
    )

    def get_main_page(self, page=1):
        url = f"https://api.dailymotion.com/videos?fields=id,title,thumbnail_360_url,description&limit=26&page={page}"
        try:
            txt = _http_get_text(url, timeout=10.0)
            data = json.loads(txt)
            items = []
            for v in data.get("list", []):
                vid = v.get("id")
                if not vid:
                    continue
                items.append({
                    "provider_id": self.id,
                    "data": f"dm:{vid}",
                    "name": v.get("title") or "Dailymotion Video",
                    "type": CloudStreamTvType.Other,
                    "poster_url": v.get("thumbnail_360_url"),
                    "background_url": v.get("thumbnail_360_url"),
                    "description": v.get("description"),
                    "year": None,
                })
            return [("Popular", items)] if items else []
        except Exception as e:
            logger.debug(f"Dailymotion get_main_page failed: {e}")
            return []

    def search(self, query):
        if not query or not query.strip():
            return []
        encoded = urllib.parse.quote(query.strip())
        url = f"https://api.dailymotion.com/videos?search={encoded}&limit=20&fields=id,title,description,thumbnail_360_url"
        try:
            txt = _http_get_text(url, timeout=10.0)
            data = json.loads(txt)
            items = []
            for v in data.get("list", []):
                vid = v.get("id")
                if not vid:
                    continue
                items.append({
                    "provider_id": self.id,
                    "data": f"dm:{vid}",
                    "name": v.get("title") or "Dailymotion Video",
                    "type": CloudStreamTvType.Other,
                    "poster_url": v.get("thumbnail_360_url"),
                    "background_url": v.get("thumbnail_360_url"),
                    "description": v.get("description"),
                    "year": None,
                })
            return items
        except Exception as e:
            logger.debug(f"Dailymotion search failed: {e}")
            return []

    def load(self, data_str):
        vid = _extract_dailymotion_id(data_str)
        url = f"https://api.dailymotion.com/video/{vid}?fields=id,title,description,thumbnail_720_url"
        try:
            txt = _http_get_text(url, timeout=10.0)
            v = json.loads(txt)
            title = v.get("title") or f"Dailymotion {vid}"
            poster = v.get("thumbnail_720_url")
            desc = v.get("description")
        except Exception:
            title = f"Dailymotion {vid}"
            poster = None
            desc = None

        return {
            "provider_id": self.id,
            "data": f"dm:{vid}" if vid else data_str,
            "name": title,
            "type": CloudStreamTvType.Other,
            "poster_url": poster,
            "background_url": poster,
            "description": desc,
            "year": None,
            "rating_percent": None,
            "tags": ["Dailymotion"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        vid = _extract_dailymotion_id(data_str)
        if not vid:
            return []

        # 1. Try yt-dlp first for robust extraction of direct stream and subtitles
        yt_dlp_bin = shutil.which("yt-dlp")
        if not yt_dlp_bin:
            for p in ["/app/bin/yt-dlp", "/usr/bin/yt-dlp", "/usr/local/bin/yt-dlp"]:
                if os.path.exists(p):
                    yt_dlp_bin = p
                    break
        if yt_dlp_bin:
            try:
                cmd = [
                    yt_dlp_bin,
                    "--no-warnings",
                    "--no-playlist",
                    "-j",
                    f"https://www.dailymotion.com/video/{vid}",
                ]
                run_env = dict(os.environ)
                try:
                    from . import doh
                    run_env.update(doh.get_doh_env())
                except Exception:
                    pass
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=run_env)
                if proc.returncode == 0 and proc.stdout.strip():
                    info = json.loads(proc.stdout)
                    stream_url = info.get("url")
                    if not stream_url and info.get("formats"):
                        stream_url = info["formats"][-1].get("url")
                    if stream_url:
                        subs = []
                        for lang, s_list in (info.get("subtitles") or {}).items():
                            for item in s_list:
                                if item.get("url"):
                                    subs.append({
                                        "language": lang,
                                        "url": item["url"],
                                    })
                        return [{
                            "name": f"Dailymotion [{info.get('resolution') or 'HLS'}]",
                            "title": info.get("title") or f"Dailymotion {vid}",
                            "url": stream_url,
                            "is_hls": ".m3u8" in stream_url,
                            "is_dash": ".mpd" in stream_url,
                            "is_http": True,
                            "is_torrent": False,
                            "quality": "1080p",
                            "subtitles": subs,
                            "headers": {
                                "User-Agent": DEFAULT_USER_AGENT,
                                "Referer": "https://www.dailymotion.com/",
                            },
                        }]
            except Exception as e:
                logger.debug(f"yt-dlp extraction for Dailymotion {vid} failed: {e}")

        # 2. Fallback to Dailymotion player metadata API
        try:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            meta_url = f"https://www.dailymotion.com/player/metadata/video/{vid}"
            req = urllib.request.Request(meta_url, headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": "https://www.dailymotion.com/",
            })
            resp = opener.open(req, timeout=10)
            meta_data = json.loads(resp.read().decode("utf-8"))
            qualities = meta_data.get("qualities", {})
            auto_streams = qualities.get("auto", [])
            if auto_streams and auto_streams[0].get("url"):
                master_url = auto_streams[0]["url"]
                req_m = urllib.request.Request(master_url, headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Referer": "https://www.dailymotion.com/",
                })
                resp_m = opener.open(req_m, timeout=10)
                m_text = resp_m.read().decode("utf-8")
                sub_urls = [l for l in m_text.splitlines() if l.startswith("http")]
                target_url = sub_urls[0] if sub_urls else master_url

                subs = []
                sub_raw = meta_data.get("subtitles", {}).get("data")
                if isinstance(sub_raw, dict):
                    for lang_code, sub_info in sub_raw.items():
                        if isinstance(sub_info, dict):
                            urls = sub_info.get("urls") or ([sub_info.get("url")] if sub_info.get("url") else [])
                            if urls:
                                subs.append({
                                    "language": sub_info.get("label") or lang_code,
                                    "url": urls[0],
                                })
                elif isinstance(sub_raw, list):
                    for item in sub_raw:
                        if isinstance(item, dict):
                            urls = item.get("urls") or ([item.get("url")] if item.get("url") else [])
                            if urls:
                                subs.append({
                                    "language": item.get("label") or item.get("language") or "Subtitle",
                                    "url": urls[0],
                                })
                return [{
                    "name": "Dailymotion [HLS]",
                    "title": meta_data.get("title") or f"Dailymotion {vid}",
                    "url": target_url,
                    "is_hls": True,
                    "is_dash": False,
                    "is_http": True,
                    "is_torrent": False,
                    "quality": "1080p",
                    "subtitles": subs,
                    "headers": {
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Referer": "https://www.dailymotion.com/",
                    },
                }]
        except Exception as e:
            logger.debug(f"Dailymotion player metadata extraction failed: {e}")

        # 3. Final fallback: raw video URL with Referer headers
        return [{
            "name": "Dailymotion Video",
            "url": f"https://www.dailymotion.com/video/{vid}",
            "referer": "https://www.dailymotion.com/",
            "headers": {"Referer": "https://www.dailymotion.com/"},
            "subtitles": [],
            "is_hls": True,
            "is_dash": False,
        }]


class InternetArchiveCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/recloudstream/extensions/builds/InternetArchiveProvider.cs3#InternetArchiveProvider"
    name = "InternetArchiveProvider"
    supported_types = ["movie", "series", "tv", "other"]
    catalog_sections = (
        {"id": "featured-movies", "name": "Featured Movies", "section": "Featured Movies", "type": "movie", "supports_pagination": True},
    )

    def get_main_page(self, page=1):
        p = int(page or 1)
        url = f"https://archive.org/advancedsearch.php?q=mediatype:(movies)&fl[]=identifier,title,description,year&sort[]=downloads+desc&rows=25&page={p}&output=json"
        try:
            txt = _http_get_text(url, timeout=10.0)
            data = json.loads(txt)
            docs = data.get("response", {}).get("docs", [])
            items = []
            for d in docs:
                ident = d.get("identifier")
                if not ident:
                    continue
                items.append({
                    "provider_id": self.id,
                    "data": f"ia:{ident}",
                    "name": d.get("title") or ident,
                    "type": CloudStreamTvType.Movie,
                    "poster_url": f"https://archive.org/services/img/{ident}",
                    "background_url": f"https://archive.org/services/img/{ident}",
                    "description": d.get("description"),
                    "year": int(d["year"]) if str(d.get("year", "")).isdigit() else None,
                })
            return [("Featured Movies", items)] if items else []
        except Exception as e:
            logger.debug(f"InternetArchive get_main_page failed: {e}")
            return []

    def search(self, query):
        if not query or not query.strip():
            return []
        encoded = urllib.parse.quote(query.strip())
        url = f"https://archive.org/advancedsearch.php?q={encoded}+AND+mediatype:(movies)&fl[]=identifier,title,description,year&rows=20&output=json"
        try:
            txt = _http_get_text(url, timeout=10.0)
            data = json.loads(txt)
            docs = data.get("response", {}).get("docs", [])
            items = []
            for d in docs:
                ident = d.get("identifier")
                if not ident:
                    continue
                items.append({
                    "provider_id": self.id,
                    "data": f"ia:{ident}",
                    "name": d.get("title") or ident,
                    "type": CloudStreamTvType.Movie,
                    "poster_url": f"https://archive.org/services/img/{ident}",
                    "background_url": f"https://archive.org/services/img/{ident}",
                    "description": d.get("description"),
                    "year": int(d["year"]) if str(d.get("year", "")).isdigit() else None,
                })
            return items
        except Exception as e:
            logger.debug(f"InternetArchive search failed: {e}")
            return []

    def load(self, data_str):
        ident = data_str.split(":")[-1].strip()
        url = f"https://archive.org/metadata/{ident}"
        try:
            txt = _http_get_text(url, timeout=10.0)
            meta = json.loads(txt).get("metadata", {})
            title = meta.get("title") or ident
            desc = meta.get("description")
            year = int(meta["year"]) if str(meta.get("year", "")).isdigit() else None
        except Exception:
            title = ident
            desc = None
            year = None

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.Movie,
            "poster_url": f"https://archive.org/services/img/{ident}",
            "background_url": f"https://archive.org/services/img/{ident}",
            "description": desc,
            "year": year,
            "rating_percent": None,
            "tags": ["Archive.org"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        ident = data_str.split(":")[-1].strip()
        url = f"https://archive.org/metadata/{ident}"
        try:
            txt = _http_get_text(url, timeout=10.0)
            files = json.loads(txt).get("files", [])
            sources = []
            for f in files:
                fname = f.get("name", "")
                if fname.lower().endswith((".mp4", ".mkv", ".webm", ".m3u8")):
                    f_url = f"https://archive.org/download/{ident}/{fname}"
                    sources.append({
                        "name": f.get("format") or fname,
                        "url": f_url,
                        "subtitles": [],
                        "is_hls": fname.lower().endswith(".m3u8"),
                        "is_dash": False,
                    })
            if sources:
                return sources
        except Exception as e:
            logger.debug(f"InternetArchive load_links failed: {e}")

        return []


def _extract_youtube_id(data_or_url):
    if not data_or_url:
        return ""
    s = str(data_or_url).strip()
    if s.startswith("yt:"):
        s = s[3:].strip()
    if re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", s)
    if m:
        return m.group(1)
    return ""


def _is_youtube_title_match(target_title, candidate_title, min_ratio=0.75):
    """Strictly validates that candidate_title contains the target_title words.
    Prevents YouTube's fuzzy search from returning unrelated videos (e.g. 'Campus beats'
    or 'MrBeast' when querying 'betas')."""
    if not target_title or not candidate_title:
        return False

    stop_words = {
        "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
        "with", "by", "from", "as", "is", "it", "its", "part", "vol", "volume"
    }

    t_raw_words = re.findall(r"\b[a-z0-9]+\b", str(target_title).lower())
    t_words = [w for w in t_raw_words if w not in stop_words]
    if not t_words:
        t_words = t_raw_words
    if not t_words:
        return False

    c_text = str(candidate_title).lower()
    c_words = set(re.findall(r"\b[a-z0-9]+\b", c_text))

    clean_target_phrase = " ".join(t_words)
    if clean_target_phrase and re.search(r"\b" + re.escape(clean_target_phrase) + r"\b", c_text):
        if len(t_words) == 1:
            allowed_prefixes = {
                "the", "a", "an", "series", "serie", "show", "watch", "stream",
                "season", "episode", "full", "hd", "4k", "official"
            }
            tw = t_words[0]
            pat = (
                rf"(?:^|[\s\-_:\|\(\)\[\]•]+)([a-z0-9]+)?\s*({re.escape(tw)}s?|{re.escape(tw[:-1])}s?)\b"
                if tw.endswith("s")
                else rf"(?:^|[\s\-_:\|\(\)\[\]•]+)([a-z0-9]+)?\s*({re.escape(tw)}s?)\b"
            )
            matches = list(re.finditer(pat, c_text))
            if matches:
                return any((not m.group(1)) or (m.group(1) in allowed_prefixes) for m in matches)
        return True

    def _word_matches(tw):
        if tw in c_words:
            return True
        if len(tw) >= 4 and tw.endswith("s") and tw[:-1] in c_words:
            return True
        if len(tw) >= 3 and (tw + "s") in c_words:
            return True
        return False

    matched = sum(1 for w in t_words if _word_matches(w))

    if len(t_words) == 1:
        if matched != 1:
            return False
        allowed_prefixes = {
            "the", "a", "an", "series", "serie", "show", "watch", "stream",
            "season", "episode", "full", "hd", "4k", "official"
        }
        tw = t_words[0]
        pat = (
            rf"(?:^|[\s\-_:\|\(\)\[\]•]+)([a-z0-9]+)?\s*({re.escape(tw)}s?|{re.escape(tw[:-1])}s?)\b"
            if tw.endswith("s")
            else rf"(?:^|[\s\-_:\|\(\)\[\]•]+)([a-z0-9]+)?\s*({re.escape(tw)}s?)\b"
        )
        matches = list(re.finditer(pat, c_text))
        if matches:
            return any((not m.group(1)) or (m.group(1) in allowed_prefixes) for m in matches)
        return True

    if len(t_words) == 2:
        return matched == 2

    return (matched / len(t_words)) >= min_ratio and matched >= 2


class YoutubeCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/recloudstream/extensions/builds/YoutubeProvider.cs3#YoutubeProvider"
    name = "YoutubeProvider"
    supported_types = ["movie", "series", "tvseries", "documentary", "other", "live", "clip", "all"]

    @classmethod
    def _search_innertube(cls, query, timeout=6.0):
        if not query or not query.strip():
            return []
        url = "https://www.youtube.com/youtubei/v1/search"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20240101.00.00",
                    "hl": "en",
                    "gl": "US",
                }
            },
            "query": query.strip(),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            },
        )
        results = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                contents = data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
                for s in contents:
                    for it in s.get("itemSectionRenderer", {}).get("contents", []):
                        vr = it.get("videoRenderer")
                        if not vr:
                            continue
                        vid = vr.get("videoId")
                        if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                            continue
                        title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
                        length = vr.get("lengthText", {}).get("simpleText") or "".join(r.get("text", "") for r in vr.get("lengthText", {}).get("runs", []))
                        channel = "".join(r.get("text", "") for r in vr.get("ownerText", {}).get("runs", []))
                        views = vr.get("viewCountText", {}).get("simpleText") or "".join(r.get("text", "") for r in vr.get("viewCountText", {}).get("runs", []))
                        badges = [b.get("metadataBadgeRenderer", {}).get("label") for b in vr.get("badges", [])]
                        thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
                        poster = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                        # Parse duration seconds
                        dur = 0
                        parts = length.split(":")
                        try:
                            if len(parts) == 3: dur = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                            elif len(parts) == 2: dur = int(parts[0])*60 + int(parts[1])
                            elif len(parts) == 1: dur = int(parts[0])
                        except Exception:
                            pass
                        results.append({
                            "id": vid,
                            "title": title,
                            "length_str": length,
                            "duration": dur,
                            "channel": channel,
                            "views": views,
                            "badges": badges,
                            "poster": poster,
                        })
        except Exception as e:
            logger.debug(f"YouTube InnerTube search failed for '{query}': {e}")
        return results

    def search(self, query):
        if not query or not query.strip():
            return []

        clean_q = str(query).strip()
        # Query with quotes first to prevent YouTube's search engine from returning fuzzy typos/recommendations
        inner_items = self._search_innertube(f'"{clean_q}" -review -trailer')
        if not inner_items:
            inner_items = self._search_innertube(f'"{clean_q}"')
        if not inner_items:
            inner_items = self._search_innertube(clean_q)

        # Filter strictly by title match and exclude review/trailer keywords
        clean_q_lower = clean_q.lower()
        filtered = [
            v for v in inner_items
            if _is_youtube_title_match(clean_q, v.get("title", ""))
            and not (re.search(r"\breview[a-z]*\b", v.get("title", "").lower()) and "review" not in clean_q_lower)
            and not (re.search(r"\b(?:trailer|teaser)\b", v.get("title", "").lower()) and not any(k in clean_q_lower for k in ("trailer", "teaser")))
        ]

        if filtered:
            return [
                {
                    "provider_id": self.id,
                    "data": f"yt:{v['id']}",
                    "name": v.get("title") or "YouTube Video",
                    "type": CloudStreamTvType.Other,
                    "poster_url": v.get("poster") or f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg",
                    "background_url": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg",
                    "description": f"{v.get('channel', 'YouTube')} • {v.get('length_str', '')}",
                    "year": None,
                }
                for v in filtered
            ]

        # 2. Try yt-dlp
        yt_dlp_bin = shutil.which("yt-dlp")
        if not yt_dlp_bin:
            for p in ["/app/bin/yt-dlp", "/usr/bin/yt-dlp", "/usr/local/bin/yt-dlp"]:
                if os.path.exists(p):
                    yt_dlp_bin = p
                    break
        if yt_dlp_bin:
            try:
                cmd = [
                    yt_dlp_bin,
                    "--no-warnings",
                    "--flat-playlist",
                    "--dump-single-json",
                    f"ytsearch20:{query.strip()}",
                ]
                run_env = dict(os.environ)
                try:
                    from . import doh
                    run_env.update(doh.get_doh_env())
                except Exception:
                    pass
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=run_env)
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    entries = data.get("entries") or []
                    items = []
                    for e in entries:
                        vid = e.get("id")
                        if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                            continue
                        thumbs = e.get("thumbnails") or []
                        poster = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                        items.append({
                            "provider_id": self.id,
                            "data": f"yt:{vid}",
                            "name": e.get("title") or "YouTube Video",
                            "type": CloudStreamTvType.Other,
                            "poster_url": poster,
                            "background_url": poster,
                            "description": e.get("description"),
                            "year": None,
                        })
                    if items:
                        return items
            except Exception as e:
                logger.debug(f"yt-dlp search for YouTube failed: {e}")

        # 3. Invidious fallback
        encoded = urllib.parse.quote(query.strip())
        for inv_host in ["https://inv.nadeko.net", "https://invidious.nerdvpn.de", "https://yewtu.be"]:
            url = f"{inv_host}/api/v1/search?q={encoded}&type=video"
            try:
                txt = _http_get_text(url, timeout=5.0)
                data = json.loads(txt)
                items = []
                for v in data[:20]:
                    vid = v.get("videoId")
                    if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                        continue
                    thumbs = v.get("videoThumbnails") or []
                    poster = thumbs[0].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                    items.append({
                        "provider_id": self.id,
                        "data": f"yt:{vid}",
                        "name": v.get("title") or "YouTube Video",
                        "type": CloudStreamTvType.Other,
                        "poster_url": poster,
                        "background_url": poster,
                        "description": v.get("description"),
                        "year": None,
                    })
                if items:
                    return items
            except Exception:
                continue

        return []

    def get_streams_for_media(self, title, media_type="movie", year=None, season=None, episode=None, imdb_id=None):
        """Searches YouTube specifically for full movies or TV series episodes."""
        if not title or not str(title).strip():
            return []

        clean_title = str(title).strip()
        is_series = (str(media_type or "").lower() in ("series", "show", "shows", "tv", "anime")) or (season is not None and episode is not None)

        queries = []
        if is_series:
            s_num = int(season) if season is not None else 1
            e_num = int(episode) if episode is not None else 1
            queries = [
                f'"{clean_title}" S{s_num:02d}E{e_num:02d} -review -trailer',
                f'"{clean_title}" Season {s_num} Episode {e_num} full episode -review',
                f'"{clean_title}" Episode {e_num} full -review',
                f'"{clean_title}" S{s_num:02d}E{e_num:02d}',
                f"{clean_title} S{s_num:02d}E{e_num:02d}",
            ]
        else:
            if year:
                queries.append(f'"{clean_title}" {year} full movie -review -trailer')
            queries.append(f'"{clean_title}" full movie -review -trailer')
            queries.append(f'"{clean_title}" full length -review')
            if year:
                queries.append(f'"{clean_title}" {year} full movie')
            queries.append(f'"{clean_title}" full movie')
            queries.append(f'"{clean_title}" full length')
            if year:
                queries.append(f"{clean_title} {year} full movie")
            queries.append(f"{clean_title} full movie")

        neg_movie = [
            "trailer", "trailers", "teaser", "teasers", "preview", "previews", "promo",
            "review", "reviews", "reviewer", "reviewers", "reviewing", "reviewed",
            "reaction", "reactions", "recap", "recaps", "explained", "explanation", "explanations",
            "ending explained", "kill count", "breakdown", "breakdowns", "easter egg", "easter eggs",
            "scene", "scenes", "clip", "clips", "soundtrack", "soundtracks", "ost",
            "interview", "interviews", "behind the scenes", "b-roll", "analysis", "cast then and now",
            "gameplay", "walkthrough", "theme song", "first impression", "first impressions",
            "worth the watch", "thoughts on", "critique", "critiques", "discussion", "discussions",
            "spoiler", "spoilers", "bloopers", "deleted scene", "deleted scenes",
            "supercut", "supercuts", "after show", "aftershow", "talk", "talks",
            "lyric", "lyrics", "lyrical", "audio song", "video song"
        ]
        neg_series = [
            "trailer", "trailers", "teaser", "teasers", "preview", "previews", "promo",
            "review", "reviews", "reviewer", "reviewers", "reviewing", "reviewed",
            "reaction", "reactions", "recap", "recaps", "explained", "explanation", "explanations",
            "ending explained", "interview", "interviews", "behind the scenes",
            "b-roll", "breakdown", "breakdowns", "analysis", "theme song", "first impression", "first impressions",
            "scene", "scenes", "clip", "clips", "soundtrack", "soundtracks", "ost", "kill count",
            "worth the watch", "thoughts on", "critique", "critiques", "discussion", "discussions",
            "spoiler", "spoilers", "bloopers", "deleted scene", "deleted scenes",
            "supercut", "supercuts", "after show", "aftershow", "talk", "talks",
            "lyric", "lyrics", "lyrical", "audio song", "video song"
        ]

        seen_ids = set()
        candidates = []
        for q in queries:
            vids = self._search_innertube(q)
            for v in vids:
                if v["id"] not in seen_ids:
                    seen_ids.add(v["id"])
                    candidates.append(v)
            if len(candidates) >= 15:
                break

        scored = []
        if is_series:
            s_num = int(season) if season is not None else 1
            e_num = int(episode) if episode is not None else 1
            for v in candidates:
                vt = v["title"]
                vt_lower = vt.lower()
                dur = v["duration"]
                if 0 < dur < 180:
                    continue
                # STRICT TITLE CHECK
                if not _is_youtube_title_match(clean_title, vt):
                    continue
                # Explicit review filter
                if re.search(r"\breview[a-z]*\b", vt_lower) and "review" not in clean_title.lower():
                    continue
                if any(re.search(r"\b" + re.escape(n) + r"\b", vt_lower) for n in neg_series if n not in clean_title.lower()):
                    continue
                ep_pattern = rf"(?:ep|episode|part|e)[\s\._-]*0*{e_num}\b"
                other_ep = rf"(?:ep|episode|e)[\s\._-]*0*(\d+)\b"
                found_eps = [int(x) for x in re.findall(other_ep, vt_lower) if x.isdigit()]
                if not re.search(ep_pattern, vt_lower) and found_eps and e_num not in found_eps:
                    continue
                score = 50 if re.search(ep_pattern, vt_lower) else 10
                if re.search(rf"(?:s|season)[\s\._-]*0*{s_num}\b", vt_lower):
                    score += 30
                if any(k in vt_lower for k in ["full episode", "full ep", "complete episode"]):
                    score += 20
                if dur >= 600:
                    score += 15
                scored.append((score, v))
        else:
            for v in candidates:
                vt = v["title"]
                vt_lower = vt.lower()
                dur = v["duration"]
                if 0 < dur < 1800:
                    continue
                # STRICT TITLE CHECK
                if not _is_youtube_title_match(clean_title, vt):
                    continue
                # Explicit review filter
                if re.search(r"\breview[a-z]*\b", vt_lower) and "review" not in clean_title.lower():
                    continue
                has_neg = any(re.search(r"\b" + re.escape(n) + r"\b", vt_lower) for n in neg_movie if n not in clean_title.lower())
                if has_neg:
                    continue
                t_words = set(re.findall(r"\b[a-z0-9]+\b", clean_title.lower())) - {"a", "an", "the", "and", "or", "of", "in", "to"}
                v_words = set(re.findall(r"\b[a-z0-9]+\b", vt_lower))
                overlap = len(t_words.intersection(v_words))
                score = int(40 * (overlap / len(t_words))) if t_words else 0
                if year and str(year) in vt_lower:
                    score += 25
                if any(k in vt_lower for k in ["full movie", "full length", "complete movie"]):
                    score += 20
                if dur >= 3600:
                    score += 20
                scored.append((score, v))

        scored.sort(key=lambda x: -x[0])
        sources = []
        for _score, v in scored[:8]:
            quality = "1080p"
            if any("4k" in str(b).lower() for b in v.get("badges", [])) or "4k" in v["title"].lower():
                quality = "4K"
            elif "720" in v["title"].lower():
                quality = "720p"

            length_str = v.get("length_str") or "Full Video"
            channel = v.get("channel") or "YouTube"
            sources.append({
                "name": f"YouTube [{quality}]",
                "title": f"YouTube • {channel} • {length_str}\n{v['title']}",
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "quality": quality,
                "is_youtube": True,
                "headers": {"Referer": "https://www.youtube.com/"},
                "subtitles": [],
                "is_hls": False,
                "is_dash": False,
            })
        return sources

    def load(self, data_str):
        vid = _extract_youtube_id(data_str)
        if not vid:
            return {
                "provider_id": self.id,
                "data": data_str,
                "name": "Invalid YouTube Video",
                "type": CloudStreamTvType.Other,
                "poster_url": None,
                "background_url": None,
                "description": None,
                "year": None,
                "rating_percent": None,
                "tags": ["YouTube"],
                "episodes": [],
            }
        return {
            "provider_id": self.id,
            "data": f"yt:{vid}",
            "name": f"YouTube Video ({vid})",
            "type": CloudStreamTvType.Other,
            "poster_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "background_url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
            "description": f"https://www.youtube.com/watch?v={vid}",
            "year": None,
            "rating_percent": None,
            "tags": ["YouTube"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        vid = _extract_youtube_id(data_str)
        if not vid:
            return []

        # Try resolving direct stream URL through player.resolve_youtube_url or yt-dlp
        try:
            from .player import resolve_youtube_url
            v_url, a_url, ua = resolve_youtube_url(vid)
            if v_url:
                headers = {"User-Agent": ua} if ua else {"Referer": "https://www.youtube.com/"}
                return [{
                    "name": "YouTube [Direct]",
                    "title": f"YouTube Video ({vid})",
                    "url": v_url,
                    "audio_url": a_url,
                    "is_hls": ".m3u8" in v_url,
                    "is_dash": ".mpd" in v_url,
                    "is_http": True,
                    "is_torrent": False,
                    "quality": "1080p",
                    "headers": headers,
                    "subtitles": [],
                }]
        except Exception as e:
            logger.debug(f"Direct resolve_youtube_url for {vid} failed: {e}")

        # Fallback to standard YouTube watch URL for MPV ytdl hook
        return [{
            "name": "YouTube",
            "title": f"YouTube Video ({vid})",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "is_youtube": True,
            "referer": "https://www.youtube.com/",
            "headers": {"Referer": "https://www.youtube.com/"},
            "subtitles": [],
            "is_hls": False,
            "is_dash": False,
        }]


# --- Dynamic Domains Provider Helper ---

_DYNAMIC_DOMAINS_CACHE = {}
_DYNAMIC_DOMAINS_CACHE_TIME = 0
_DYNAMIC_DOMAINS_CACHE_LOCK = threading.Lock()

def get_dynamic_domain(key, default=None):
    """Resolves active domain for CloudStream scrapers with fallback and 1-hour cache.

    Thread-safe: concurrent callers on first load will all fetch, but the dict
    update is atomic enough for CPython (last writer wins, all values are valid).
    The lock prevents multiple threads from issuing redundant network requests
    simultaneously while the cache is cold.
    """
    global _DYNAMIC_DOMAINS_CACHE, _DYNAMIC_DOMAINS_CACHE_TIME
    now = time.time()
    # Fast path: cache is warm, no lock needed for read
    if _DYNAMIC_DOMAINS_CACHE and (now - _DYNAMIC_DOMAINS_CACHE_TIME) <= 3600:
        return _DYNAMIC_DOMAINS_CACHE.get(key.lower(), default)

    with _DYNAMIC_DOMAINS_CACHE_LOCK:
        # Re-check under lock in case another thread already refreshed
        if _DYNAMIC_DOMAINS_CACHE and (now - _DYNAMIC_DOMAINS_CACHE_TIME) <= 3600:
            return _DYNAMIC_DOMAINS_CACHE.get(key.lower(), default)

        for url in [
            "https://raw.githubusercontent.com/phisher98/TVVVV/refs/heads/main/domains.json",
            "https://raw.githubusercontent.com/SaurabhKaperwan/Utils/refs/heads/main/urls.json",
        ]:
            try:
                txt = _http_get_text(url, timeout=3.0)
                data = json.loads(txt)
                if isinstance(data, dict):
                    new_cache = {}
                    for k, v in data.items():
                        if isinstance(v, str) and v.startswith("http"):
                            new_cache[k.lower()] = v.rstrip("/")
                    _DYNAMIC_DOMAINS_CACHE = new_cache
                    _DYNAMIC_DOMAINS_CACHE_TIME = now
                    break
            except Exception:
                pass
    clean_k = key.lower()
    return _DYNAMIC_DOMAINS_CACHE.get(clean_k, default)


class JsUnpacker:
    """
    De-obfuscates JavaScript packed with Dean Edwards P.A.C.K.E.R.
    Ported from cloudstream/library/.../utils/JsUnpacker.kt.
    Supports both instance usage (unpacker.detect(), unpacker.unpack())
    and static/class usage (JsUnpacker.detect(js), JsUnpacker.unpack(js)).
    """
    def __init__(self, packed_js=""):
        self.packed_js = packed_js or ""

    def detect(self, text=None):
        if isinstance(self, str) and text is None:
            target = self
        elif hasattr(self, "packed_js"):
            target = self.packed_js if text is None else str(text or "")
        else:
            target = str(text or "")
        js = re.sub(r"\s+", "", target)
        return bool(re.search(r"eval\(function\(p,a,c,k,e,[rd]", js))

    def unpack(self, text=None):
        if isinstance(self, str) and text is None:
            target = self
        elif hasattr(self, "packed_js"):
            target = self.packed_js if text is None else str(text or "")
        else:
            target = str(text or "")
        if not target:
            return ""
        pattern = r"}\s*\('(.*?)',\s*(\d+|[a-zA-Z0-9]+),\s*(\d+),\s*'([^']*)'\.split\('\|'\)"
        match = re.search(pattern, target, re.DOTALL)
        if not match:
            pattern_alt = r"}\s*\(\s*\"(.*?)\"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\"(.*?)\"\.split\('\|'\)"
            match = re.search(pattern_alt, target, re.DOTALL)
        if not match:
            return ""

        payload, radix_str, count_str, symtab_str = match.groups()
        try:
            radix = int(radix_str)
        except Exception:
            radix = 36
        symtab = symtab_str.split("|")
        ALPHABET_62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

        def _unbase(word):
            if radix <= 36:
                try:
                    return int(word, radix)
                except ValueError:
                    return None
            ret = 0
            for i, c in enumerate(reversed(word)):
                val = ALPHABET_62.find(c)
                if val < 0 or val >= radix:
                    return None
                ret += val * (radix ** i)
            return ret

        def _replace(m):
            w = m.group(0)
            idx = _unbase(w)
            if idx is not None and 0 <= idx < len(symtab) and symtab[idx]:
                return symtab[idx]
            return w

        return re.sub(r"\b[a-zA-Z0-9_]+\b", _replace, payload)


_PACKED_REGEX = re.compile(r"eval\(function\(p,a,c,k,e,.*?\)\)", re.DOTALL)

def get_packed(string):
    """Finds packed JS string within HTML or script tag."""
    if not string:
        return None
    m = _PACKED_REGEX.search(string)
    return m.group(0) if m else None

def get_and_unpack(string):
    """Extracts packed JS from HTML/script and unpacks it if found."""
    if not string:
        return ""
    packed = get_packed(string)
    if packed:
        unpacked = JsUnpacker(packed).unpack()
        if unpacked:
            return unpacked
    unpacker = JsUnpacker(string)
    if unpacker.detect():
        unpacked = unpacker.unpack()
        if unpacked:
            return unpacked
    return string


def extract_jwplayer_streams(script_or_html, source_name="Stream", main_url="", headers=None):
    """
    Extracts stream links and playlists from JWPlayer scripts and JSON configurations.
    Ported from cloudstream/library/.../extractors/helper/JWPlayerHelper.kt.
    """
    if not script_or_html:
        return []
    req_headers = dict(headers or {})
    if main_url and "Referer" not in req_headers:
        req_headers["Referer"] = main_url
    if "User-Agent" not in req_headers:
        req_headers["User-Agent"] = DEFAULT_USER_AGENT

    streams = []
    seen_urls = set()

    # 1. Direct master.m3u8 or master.txt URLs
    m3u8_matches = re.findall(r'[:=]\s*["\'](https?://[^"\'\s]+(?:\.m3u8|master\.txt)[^"\'\s]*)["\']', script_or_html)
    if not m3u8_matches:
        m3u8_matches = re.findall(r'["\'](https?://[^"\'\s]+(?:\.m3u8|master\.txt)[^"\'\s]*)["\']', script_or_html)
    for m3u8_url in m3u8_matches:
        clean_url = m3u8_url.replace("\\/", "/")
        if clean_url not in seen_urls:
            seen_urls.add(clean_url)
            streams.append({
                "name": source_name,
                "url": clean_url,
                "headers": req_headers,
                "is_hls": True,
                "is_dash": False,
            })

    # 2. sources: [{file: "..."}] or file: "..."
    file_matches = re.findall(r'["\']?file["\']?\s*:\s*["\'](https?://[^"\'\s]+)["\']', script_or_html)
    for f_url in file_matches:
        clean_url = f_url.replace("\\/", "/")
        if clean_url not in seen_urls:
            seen_urls.add(clean_url)
            streams.append({
                "name": source_name,
                "url": clean_url,
                "headers": req_headers,
                "is_hls": ".m3u8" in clean_url or ".txt" in clean_url,
                "is_dash": ".mpd" in clean_url,
            })

    return streams


def extract_pixeldrain_stream(url):
    """
    Extracts direct downloadable/playable stream from a PixelDrain URL.
    Reference: cloudstream/library/.../extractors/PixelDrainExtractor.kt
    """
    if not url:
        return []
    m = re.search(r"pixeldrain\.(?:com|dev)/u/([a-zA-Z0-9_-]+)", url)
    if not m:
        m = re.search(r"/api/file/([a-zA-Z0-9_-]+)", url)
    if m:
        file_id = m.group(1)
        direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
        return [{
            "name": "PixelDrain",
            "url": direct_url,
            "headers": {
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": url,
            },
            "is_hls": False,
            "is_dash": False,
        }]
    return []


def extract_doodstream(url):
    """
    Extracts direct playable stream from DoodStream and its mirrors.
    Reference: cloudstream/library/.../extractors/DoodExtractor.kt
    """
    if not url:
        return []
    embed_url = re.sub(r"/d/([a-zA-Z0-9_-]+)", r"/e/\1", url)
    try:
        html = _http_get_text(embed_url, headers={"Referer": url}, timeout=7.0)
        m_md5 = re.search(r"/pass_md5/([^'\"]+)", html)
        if not m_md5:
            return []
        token = m_md5.group(1)
        parsed = urllib.parse.urlsplit(embed_url)
        md5_url = f"{parsed.scheme}://{parsed.netloc}/pass_md5/{token}"
        md5_resp = _http_get_text(md5_url, headers={"Referer": embed_url}, timeout=7.0).strip()
        if not md5_resp:
            return []
        rand_str = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        final_stream_url = f"{md5_resp}{rand_str}?token={token}"
        return [{
            "name": "DoodStream",
            "url": final_stream_url,
            "headers": {
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": "https://dood.la/",
            },
            "is_hls": False,
            "is_dash": False,
        }]
    except Exception as e:
        logger.debug(f"DoodStream extraction failed for {url}: {e}")
        return []


def extract_streamtape(url):
    """
    Extracts direct playable stream from StreamTape.
    Reference: cloudstream/library/.../extractors/StreamTape.kt
    """
    if not url:
        return []
    try:
        html = _http_get_text(url, headers={"Referer": url}, timeout=7.0)
        m = re.search(r"botlink'\)\.innerHTML\s*=\s*(.*?);", html)
        if m:
            parts = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
            extracted_path = "".join(parts)
            if extracted_path:
                if not extracted_path.startswith("http"):
                    stream_url = f"https:{extracted_path}&stream=1" if extracted_path.startswith("//") else f"https://streamtape.com{extracted_path}&stream=1"
                else:
                    stream_url = f"{extracted_path}&stream=1"
                return [{
                    "name": "StreamTape",
                    "url": stream_url,
                    "headers": {
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Referer": url,
                    },
                    "is_hls": False,
                    "is_dash": False,
                }]
    except Exception as e:
        logger.debug(f"StreamTape extraction failed for {url}: {e}")
        return []


def extract_mixdrop(url):
    """
    Extracts direct playable stream from MixDrop.
    Reference: cloudstream/library/.../extractors/MixDrop.kt
    """
    if not url:
        return []
    embed_url = re.sub(r"/f/([a-zA-Z0-9_-]+)", r"/e/\1", url)
    try:
        html = _http_get_text(embed_url, headers={"Referer": url}, timeout=7.0)
        unpacked = get_and_unpack(html)
        m = re.search(r'wurl.*?=.*?"(.*?)";', unpacked)
        if not m:
            m = re.search(r'wurl.*?=.*?["\']([^"\']+)["\'];?', unpacked)
        if m:
            raw_link = m.group(1)
            stream_url = f"https:{raw_link}" if raw_link.startswith("//") else (f"https://{raw_link}" if not raw_link.startswith("http") else raw_link)
            return [{
                "name": "MixDrop",
                "url": stream_url,
                "headers": {
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Referer": embed_url,
                },
                "is_hls": False,
                "is_dash": False,
            }]
        return []
    except Exception as e:
        logger.debug(f"MixDrop extraction failed for {url}: {e}")
        return []


def extract_streamwish(url):
    """
    Extracts direct stream from StreamWish and its mirrors.
    Reference: cloudstream/library/.../extractors/StreamWishExtractor.kt
    """
    if not url:
        return []
    embed_url = re.sub(r"/(?:f|d)/([a-zA-Z0-9_-]+)", r"/e/\1", url)
    try:
        parsed = urllib.parse.urlsplit(embed_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": f"{origin}/",
            "Origin": origin,
        }
        html = _http_get_text(embed_url, headers=headers, timeout=7.0)
        unpacked = get_and_unpack(html)
        return extract_jwplayer_streams(unpacked or html, source_name="StreamWish", main_url=origin, headers=headers)
    except Exception as e:
        logger.debug(f"StreamWish extraction failed for {url}: {e}")
        return []


def extract_vidhide(url):
    """
    Extracts direct stream from VidHide, FileLions, and mirrors.
    Reference: cloudstream/library/.../extractors/VidHidePro.kt
    """
    if not url:
        return []
    embed_url = re.sub(r"/(?:d|download|file|f)/([a-zA-Z0-9_-]+)", r"/v/\1", url)
    try:
        parsed = urllib.parse.urlsplit(embed_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": f"{origin}/",
            "Origin": origin,
        }
        html = _http_get_text(embed_url, headers=headers, timeout=7.0)
        unpacked = get_and_unpack(html)
        return extract_jwplayer_streams(unpacked or html, source_name="VidHide", main_url=origin, headers=headers)
    except Exception as e:
        logger.debug(f"VidHide extraction failed for {url}: {e}")
        return []


def extract_filemoon(url):
    """
    Extracts direct stream from FileMoon.
    Reference: cloudstream/library/.../extractors/Filemoon.kt
    """
    if not url:
        return []
    try:
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": url,
        }
        html = _http_get_text(url, headers=headers, timeout=7.0)
        m_iframe = re.search(r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']', html)
        if m_iframe:
            iframe_url = m_iframe.group(1)
            html = _http_get_text(iframe_url, headers={"Referer": url, "User-Agent": DEFAULT_USER_AGENT}, timeout=7.0)
        unpacked = get_and_unpack(html)
        return extract_jwplayer_streams(unpacked or html, source_name="FileMoon", main_url=origin, headers=headers)
    except Exception as e:
        logger.debug(f"FileMoon extraction failed for {url}: {e}")
        return []


def extract_fastream(url):
    """
    Extracts direct stream from Fastream.
    Reference: cloudstream/library/.../extractors/Fastream.kt
    """
    if not url:
        return []
    try:
        html = _http_get_text(url, headers={"Referer": url}, timeout=7.0)
        unpacked = get_and_unpack(html)
        return extract_jwplayer_streams(unpacked or html, source_name="Fastream", main_url=url)
    except Exception as e:
        logger.debug(f"Fastream extraction failed for {url}: {e}")
        return []


def extract_gofile(url):
    """
    Extracts direct downloadable stream from Gofile.io.
    Reference: cloudstream/library/.../extractors/Gofile.kt
    """
    if not url:
        return []
    m = re.search(r"gofile\.io/(?:d/|\?c=)([a-zA-Z0-9_-]+)", url)
    if not m:
        return []
    content_id = m.group(1)
    try:
        req = urllib.request.Request(
            "https://api.gofile.io/accounts",
            data=b"{}",
            headers={"User-Agent": DEFAULT_USER_AGENT, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5.0) as r:
            acc_data = json.loads(r.read().decode("utf-8", errors="ignore"))
        token = acc_data.get("data", {}).get("token")
        if not token:
            return []

        c_req = urllib.request.Request(
            f"https://api.gofile.io/contents/{content_id}",
            headers={"User-Agent": DEFAULT_USER_AGENT, "Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(c_req, timeout=5.0) as r:
            c_data = json.loads(r.read().decode("utf-8", errors="ignore"))
        children = c_data.get("data", {}).get("children", {})
        streams = []
        for child_id, file_info in children.items():
            if file_info.get("type") == "file" and file_info.get("link"):
                streams.append({
                    "name": f"Gofile ({file_info.get('name', 'File')})",
                    "url": file_info["link"],
                    "headers": {
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Cookie": f"accountToken={token}",
                    },
                    "is_hls": False,
                    "is_dash": False,
                })
        return streams
    except Exception as e:
        logger.debug(f"Gofile extraction failed for {url}: {e}")
        return []


def extract_voe(url):
    """
    Extracts direct streams from VOE and its mirrors.
    Reference: cloudstream/library/.../extractors/Voe.kt
    """
    if not url:
        return []
    try:
        html = _http_get_text(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": url}, timeout=8.0)
        m_redir = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"];", html)
        if m_redir:
            redir_url = m_redir.group(1).strip()
            html = _http_get_text(redir_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": url}, timeout=8.0)

        # 1. Try modern encrypted JSON payload (F7)
        raw_payload = None
        m_script = re.search(r'<script[^>]*type=[\'"]application/json[\'"][^>]*>([\s\S]*?)</script>', html)
        if m_script:
            script_text = m_script.group(1).strip()
            if '["' in script_text and '"]' in script_text:
                raw_payload = script_text.split('["', 1)[1].rsplit('"]', 1)[0].strip()
            else:
                raw_payload = script_text.strip(' "[]\'')
        if not raw_payload:
            m_payload = re.search(r'\[\"([a-zA-Z0-9+/=_%@^\?*!#~&]{30,})\"\]', html)
            if m_payload:
                raw_payload = m_payload.group(1).strip()

        if raw_payload:
            try:
                v_rot = []
                for c in raw_payload:
                    if "A" <= c <= "Z":
                        v_rot.append(chr((ord(c) - ord("A") + 13) % 26 + ord("A")))
                    elif "a" <= c <= "z":
                        v_rot.append(chr((ord(c) - ord("a") + 13) % 26 + ord("a")))
                    else:
                        v_rot.append(c)
                v_s = "".join(v_rot)
                for pat in ("@$", "^^", "~@", "%?", "*~", "!!", "#&"):
                    v_s = v_s.replace(pat, "_")
                v_s = v_s.replace("_", "")
                v_dec = base64.b64decode(v_s).decode("utf-8", errors="ignore")
                v_shifted = "".join(chr(ord(c) - 3) for c in v_dec)
                v_rev = v_shifted[::-1]
                v_final = base64.b64decode(v_rev).decode("utf-8", errors="ignore")
                data = json.loads(v_final)
                streams = []
                if data.get("source"):
                    streams.append({
                        "name": "VOE [HLS]",
                        "url": data["source"],
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": url},
                        "is_hls": True,
                        "is_dash": False,
                    })
                if data.get("direct_access_url"):
                    streams.append({
                        "name": "VOE [MP4]",
                        "url": data["direct_access_url"],
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": url},
                        "is_hls": False,
                        "is_dash": False,
                    })
                if streams:
                    return streams
            except Exception as ex:
                logger.debug(f"VOE F7 decryption failed: {ex}")

        # 2. Fallback to direct m3u8 or mp4 URLs in script
        m_hls = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', html)
        if m_hls:
            return [{
                "name": "VOE [HLS]",
                "url": m_hls.group(1).replace("\\/", "/"),
                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": url},
                "is_hls": True,
                "is_dash": False,
            }]
        m_mp4 = re.search(r'["\'](https?://[^"\'\s]+\.mp4[^"\'\s]*)["\']', html)
        if m_mp4:
            return [{
                "name": "VOE [MP4]",
                "url": m_mp4.group(1).replace("\\/", "/"),
                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": url},
                "is_hls": False,
                "is_dash": False,
            }]
    except Exception as e:
        logger.debug(f"VOE extraction failed for {url}: {e}")
    return []


def extract_mp4upload(url):
    """
    Extracts direct MP4 stream from Mp4Upload.
    Reference: cloudstream/library/.../extractors/Mp4Upload.kt
    """
    if not url:
        return []
    embed_url = url
    m = re.search(r"mp4upload\.com/(?:embed-|)([a-zA-Z0-9]+)", url)
    if m:
        embed_url = f"https://www.mp4upload.com/embed-{m.group(1)}.html"
    try:
        html = _http_get_text(embed_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": url}, timeout=7.0)
        unpacked = get_and_unpack(html)
        m_src = re.search(r'player\.src\(["\'](https?://[^"\']+)["\']\)', unpacked)
        if not m_src:
            m_src = re.search(r'player\.src\([\w\W]*?src:\s*["\'](https?://[^"\']+)["\']', unpacked)
        if m_src:
            return [{
                "name": "Mp4Upload",
                "url": m_src.group(1),
                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": embed_url},
                "is_hls": False,
                "is_dash": False,
            }]
    except Exception as e:
        logger.debug(f"Mp4Upload extraction failed for {url}: {e}")
    return []


def extract_vidoza(url):
    """
    Extracts direct streams from Vidoza and its mirrors (Videzz).
    Reference: cloudstream/library/.../extractors/Vidoza.kt
    """
    if not url:
        return []
    try:
        html = _http_get_text(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": url}, timeout=8.0)
        # 1. Match sourcesCode: [...] or sourcesCode = [...]
        m = re.search(r'sourcesCode\s*[:=]\s*(\[[^\]]+\])', html)
        if m:
            raw_arr = m.group(1)
            clean_arr = raw_arr
            for key in ("src", "type", "label", "res"):
                clean_arr = re.sub(r'([{\[,\s])\s*' + key + r'\s*:', "\\g<1>\"" + key + "\":", clean_arr)
            clean_arr = re.sub(r',\s*([}\]])', r'\1', clean_arr)
            try:
                video_data = json.loads(clean_arr)
                streams = []
                for item in video_data:
                    src = item.get("src")
                    if src:
                        res = item.get("res") or item.get("label") or ""
                        streams.append({
                            "name": f"Vidoza [{res}p]" if (res and not str(res).endswith("p") and str(res).isdigit()) else (f"Vidoza [{res}]" if res else "Vidoza"),
                            "url": src,
                            "headers": {"User-Agent": DEFAULT_USER_AGENT},
                            "is_hls": ".m3u8" in src,
                            "is_dash": False,
                        })
                if streams:
                    return streams
            except Exception as je:
                logger.debug(f"Vidoza JSON parse error: {je}")

        # 2. Fallback to direct src: "https://...mp4" regex
        m_src = re.findall(r'src\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', html)
        if m_src:
            return [{
                "name": "Vidoza [MP4]",
                "url": s,
                "headers": {"User-Agent": DEFAULT_USER_AGENT},
                "is_hls": False,
                "is_dash": False,
            } for s in set(m_src)]
    except Exception as e:
        logger.debug(f"Vidoza extraction failed for {url}: {e}")
    return []


def _extract_hubcdn_direct(hubcdn_url, referer=None):
    """
    Extracts direct Google Video stream from a hubcdn.* link.
    """
    try:
        req = urllib.request.Request(hubcdn_url, headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": referer or hubcdn_url
        })
        with urllib.request.urlopen(req, timeout=8.0) as r:
            html = r.read().decode("utf-8", errors="ignore")
        m = re.search(r'var\s+reurl\s*=\s*[\"\']([^\"\']+)[\"\']', html)
        if m and "?r=" in m.group(1):
            b64 = m.group(1).split("?r=")[1].split("&")[0]
            dec = base64.b64decode(b64).decode("utf-8", errors="ignore")
            if "link=" in dec:
                return dec.split("link=")[1].split("&")[0]
    except Exception as e:
        logger.debug(f"HubCDN direct link extraction failed: {e}")
    return None


def _resolve_hubcloud_redirect(url):
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        cur = url
        for _ in range(5):
            req = urllib.request.Request(cur, headers={"User-Agent": DEFAULT_USER_AGENT})
            try:
                res = opener.open(req, timeout=5.0)
                hx = res.headers.get("hx-redirect")
                if hx:
                    cur = urllib.parse.urljoin(cur, hx.strip())
                    continue
                break
            except urllib.error.HTTPError as e:
                hx = e.headers.get("hx-redirect")
                if hx:
                    cur = urllib.parse.urljoin(cur, hx.strip())
                    continue
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        break
                    cur = urllib.parse.urljoin(cur, loc.strip())
                    if "link=" in cur:
                        parsed = urllib.parse.urlparse(cur)
                        qs = urllib.parse.parse_qs(parsed.query)
                        if "link" in qs:
                            return qs["link"][0]
                else:
                    break
        return cur
    except Exception:
        return url


def _extract_hubcloud_sources(url, referer=None):
    """
    Resolves a hubdrive, hubcloud, nexdrive, fastdl, or vcloud link down to direct playable video streams.
    Reference: cloudstream/library/.../extractors/HubCloud.kt
    """
    sources = []
    try:
        # Step 0: NexDrive / FastDL / VCloud resolution
        if any(k in url.lower() for k in ["nexdrive.", "fastdl.", "vcloud."]):
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": referer or url
            })
            with urllib.request.urlopen(req, timeout=6.0) as r:
                html = r.read().decode("utf-8", errors="ignore")
            if "nexdrive." in url:
                m_fast = re.search(r'href=[\"\'](https?://fastdl\.[^\"\']+)[\"\']', html)
                if m_fast:
                    return _extract_hubcloud_sources(m_fast.group(1), referer=url)
            m_link = re.search(r'link=(https?://[^\"\'\s;]+)', html)
            if m_link:
                direct_video = m_link.group(1)
                sources.append({
                    "name": "Direct Google CDN",
                    "url": direct_video,
                    "headers": {"User-Agent": DEFAULT_USER_AGENT},
                    "is_http": True,
                    "is_hls": False,
                    "is_dash": False,
                })
                return sources

        # Step 1: If HubDrive, extract HubCloud link
        if "hubdrive." in url.lower():
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": referer or url
            })
            with urllib.request.urlopen(req, timeout=8.0) as r:
                html = r.read().decode("utf-8", errors="ignore")
            m = re.search(r'<a[^>]+href=[\"\'](https?://hubcloud\.[^\"\']+)[\"\']', html)
            if m:
                url = m.group(1)
                referer = url
            else:
                return []

        # Step 2: On landing page, navigate to download page
        if "hubcloud." in url.lower() and "hubcloud.php" not in url.lower():
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": referer or url
            })
            with urllib.request.urlopen(req, timeout=8.0) as r:
                html = r.read().decode("utf-8", errors="ignore")
            m = re.search(r'<a[^>]+id=[\"\']download[\"\'][^>]+href=[\"\']([^\"\']+)[\"\']', html, re.I)
            if not m:
                m = re.search(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]+id=[\"\']download[\"\']', html, re.I)
            if not m:
                m = re.search(r'<a[^>]+href=[\"\'](https?://[^\"\']*hubcloud\.php[^\"\']*)[\"\']', html, re.I)
            if not m:
                m = re.search(r'<a[^>]+href=[\"\']([^\"\']*(?:/download/|/drive/|hubcloud\.php)[^\"\']*)[\"\']', html, re.I)
            if m:
                found_href = m.group(1).strip()
                url = urllib.parse.urljoin(url, found_href)
                referer = url
            else:
                return []

        # Step 3: Extract direct video streams / download buttons
        req = urllib.request.Request(url, headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": referer or url
        })
        with urllib.request.urlopen(req, timeout=8.0) as r:
            html = r.read().decode("utf-8", errors="ignore")

        dl_matches = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
        for dl_href, dl_text in dl_matches:
            dl_clean = re.sub(r'<[^>]+>', '', dl_text).strip()
            dl_lower = dl_clean.lower()
            href_lower = dl_href.lower()

            # 3a. PixelDrain
            if "pixeldra" in dl_lower or "pixel" in dl_lower or "pixeldrain." in href_lower:
                pd_streams = extract_pixeldrain_stream(dl_href)
                if pd_streams:
                    sources.extend(pd_streams)
                    continue

            # 3b. BuzzServer
            if "buzzserver" in dl_lower:
                buzz_link = f"{dl_href.rstrip('/')}/download"
                resolved_buzz = _resolve_hubcloud_redirect(buzz_link)
                if resolved_buzz and resolved_buzz != buzz_link and resolved_buzz.startswith("http"):
                    sources.append({
                        "name": f"{dl_clean} (BuzzServer)",
                        "url": resolved_buzz,
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": dl_href},
                        "is_hls": ".m3u8" in resolved_buzz,
                        "is_dash": False,
                    })
                continue

            # 3c. HubCDN
            if "hubcdn." in href_lower:
                direct_cdn = _extract_hubcdn_direct(dl_href, referer=url)
                if direct_cdn:
                    sources.append({
                        "name": dl_clean or "HubCDN Direct",
                        "url": direct_cdn,
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": dl_href},
                        "is_hls": False,
                        "is_dash": False,
                    })
                continue

            # 3d. Direct streams & servers
            if any(k in dl_lower or k in href_lower for k in [
                ".mkv", ".mp4", "download file", "server :", "fsl server", "s3 server", "fslv2", "mega server", "fast cloud"
            ]):
                final_url = dl_href
                if "hubcloud." in dl_href.lower():
                    resolved = _resolve_hubcloud_redirect(dl_href)
                    if resolved and resolved.startswith("http"):
                        final_url = resolved
                if final_url and (final_url.startswith("http://") or final_url.startswith("https://") or final_url.startswith("magnet:")):
                    sources.append({
                        "name": dl_clean,
                        "url": final_url,
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": url},
                        "is_hls": ".m3u8" in final_url,
                        "is_dash": False,
                    })
    except Exception as e:
        logger.debug(f"HubCloud extraction error for {url}: {e}")
    return sources


def _resolve_hdhub4u_short_link(target_url, timeout=8.0):
    """
    Resolves greenmotors.club / greenmountmotors / intermediate shorteners
    used by HDhub4u to bypass mediator pages and extract destination hblinks URLs.
    """
    try:
        html = _http_get_text(target_url, timeout=timeout)
        m = re.search(r"s\('o','([A-Za-z0-9+/=]+)'", html)
        if not m:
            m = re.search(r"ck\('_wp_http_\d+','([^']+)'", html)
        if m:
            raw = m.group(1)
            d1 = base64.b64decode(raw).decode('utf-8', errors='ignore')
            d2 = base64.b64decode(d1).decode('utf-8', errors='ignore')
            d3 = codecs.decode(d2, 'rot_13')
            d4 = base64.b64decode(d3).decode('utf-8', errors='ignore')
            obj = json.loads(d4)
            o = obj.get("o") or obj.get("data")
            if o:
                dest = base64.b64decode(o).decode('utf-8', errors='ignore')
                return dest
    except Exception as e:
        logger.debug(f"HDhub4u short link resolve error for {target_url}: {e}")
    return None


def _extract_hblinks_sources(target_url, referer=None):
    """
    Extracts HubCloud, HubDrive, PixelDrain, and direct streaming links from an hblinks page.
    """
    sources = []
    try:
        html = _http_get_text(target_url, headers={"Referer": referer or target_url}, timeout=8.0)
        hc_links = re.findall(
            r'href\s*=\s*[\"\'](https?://[^\"\']*(?:hubcloud|hubdrive|hubcdn|gdflix|vcloud|nexdrive|fastdl|pixeldrain|dood|streamtape|streamwish|vidhide)[^\"\']*)[\"\']',
            html,
            re.IGNORECASE,
        )
        for h in hc_links:
            ext_res = extract_video_streams(h, referer=target_url)
            if ext_res:
                sources.extend(ext_res)
    except Exception as e:
        logger.debug(f"Hblinks extraction error for {target_url}: {e}")
    return sources


def _extract_multiup_mirrors(multiup_url, referer=None):
    """
    Extracts playable video streams from a MultiUp mirror page (e.g. multiup.io/en/mirror/...).
    MultiUp aggregates mirrors including MixDrop, PixelDrain, StreamTape, FileLions, GoFile.
    """
    if not multiup_url:
        return []
    clean_url = str(multiup_url).strip()
    sources = []
    try:
        html = _http_get_text(clean_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": referer or ""}, timeout=8.0)
        found_links = set(re.findall(r'href=[\"\'](https?://[^\"\']+)[\"\']', html, re.I))

        for mirror_link in found_links:
            lower_m = mirror_link.lower()
            if any(dom in lower_m for dom in ("mixdrop.", "mdy48tn97.com")):
                md_streams = extract_mixdrop(mirror_link)
                if md_streams:
                    sources.extend(md_streams)
            elif "pixeldrain." in lower_m:
                pd_streams = extract_pixeldrain_stream(mirror_link)
                if pd_streams:
                    sources.extend(pd_streams)
            elif any(dom in lower_m for dom in ("streamtape.", "watchadsontape.")):
                st_streams = extract_streamtape(mirror_link)
                if st_streams:
                    sources.extend(st_streams)
            elif any(dom in lower_m for dom in ("filelions.", "vidhide.")):
                vh_streams = extract_vidhide(mirror_link)
                if vh_streams:
                    sources.extend(vh_streams)
    except Exception as e:
        logger.debug(f"MultiUp mirrors extraction failed for {clean_url}: {e}")
    return sources


def _unlock_linksddr(url, referer=None, timeout=8.0):
    """
    Unlocks linksddr.com / uptobhai.blog intermediate pages with CSRF tokens
    to extract destination file hosters (HubCloud, HubDrive, PixelDrain, GoFile, etc.).
    """
    if not url:
        return []
    clean_url = str(url).strip()
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(_safe_url(clean_url), headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        form_m = re.search(r'<form\b[^>]*>(.*?)</form>', html, re.DOTALL | re.I)
        if not form_m:
            raw_links = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.I)
            return [
                l for l in raw_links
                if l.startswith("http") and not any(skip in l.lower() for skip in [
                    "linksddr.", "uptobhai.", "facebook.", "whatsapp.", "telegram.",
                    "/register", "/login", "/money", "/contact", "/faqs", "/page/"
                ])
            ]

        form_html = form_m.group(1)
        inputs = dict(re.findall(r'<input\b[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', form_html, re.I))
        for val, name in re.findall(r'<input\b[^>]*value=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\']', form_html, re.I):
            inputs.setdefault(name, val)

        action_m = re.search(r'action=["\']([^"\']+)["\']', form_m.group(0), re.I)
        action_url = urllib.parse.urljoin(clean_url, action_m.group(1)) if (action_m and action_m.group(1)) else clean_url

        post_data = urllib.parse.urlencode(inputs).encode("utf-8")
        post_headers = headers.copy()
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"
        post_headers["Referer"] = clean_url
        post_req = urllib.request.Request(_safe_url(action_url), data=post_data, headers=post_headers)
        with opener.open(post_req, timeout=timeout) as resp:
            post_html = resp.read().decode("utf-8", errors="replace")

        raw_links = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', post_html, re.I)
        target_links = []
        for l in raw_links:
            if l.startswith("http") and not any(skip in l.lower() for skip in [
                "linksddr.", "uptobhai.", "facebook.", "whatsapp.", "telegram.",
                "/register", "/login", "/money", "/contact", "/faqs", "/page/"
            ]):
                target_links.append(l)
        return list(dict.fromkeys(target_links))
    except Exception as e:
        logger.debug(f"Failed to unlock linksddr {clean_url}: {e}")
        return []


def _extract_gdflix_sources(url, referer=None):
    """
    Extracts playable video streams from GDFlix/fastdlserver pages.
    Handles 302 redirects (fastdlserver -> gdflix), dynamic domains (new9.gdflix.io),
    and parses direct download links, PixelDrain links, FSL V2, etc.
    """
    if not url:
        return []
    clean_url = str(url).strip()
    target_url = clean_url

    if "fastdlserver" in target_url or "dl." in target_url:
        try:
            loc = _resolve_short_link(target_url, timeout=5.0)
            if loc and loc != target_url:
                target_url = loc
        except Exception as e:
            logger.debug(f"fastdlserver redirect failed for {clean_url}: {e}")

    html = ""
    candidate_urls = [target_url]
    try:
        parsed = urllib.parse.urlparse(target_url)
        dyn_host = get_dynamic_domain("gdflix", "https://new9.gdflix.io").rstrip("/")
        if parsed.netloc and dyn_host:
            alt_url = f"{dyn_host}{parsed.path}"
            if parsed.query:
                alt_url += f"?{parsed.query}"
            if alt_url != target_url:
                candidate_urls.append(alt_url)
    except Exception:
        pass

    for candidate_url in candidate_urls:
        try:
            html = _http_get_text(candidate_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": referer or ""}, timeout=8.0)
            if html:
                break
        except Exception as e:
            logger.debug(f"GDFlix fetch failed for {candidate_url}: {e}")

    if not html:
        return []

    sources = []
    fn_m = re.search(r"Name\s*:\s*([^<]+)", html, re.I)
    file_name = fn_m.group(1).strip() if fn_m else ""
    q_val = 720
    q_m = re.search(r"(\d{3,4})[pP]", file_name)
    if q_m:
        q_val = int(q_m.group(1))

    anchors = re.findall(r'<a\b[^>]*href=["\']([^\"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.DOTALL)
    for link, text in anchors:
        link = link.strip()
        t_clean = re.sub(r"<[^>]+>", "", text).strip()
        t_upper = t_clean.upper()

        if "PIXELDRA" in link.upper():
            pd_sources = extract_pixeldrain_stream(link)
            for s in pd_sources:
                s["quality"] = q_val
                s["name"] = "GDFlix [PixelDrain]"
                sources.append(s)
        elif any(k in t_upper for k in ("DIRECT DL", "DIRECT SERVER", "FSL V2", "CLOUD DOWNLOAD [R2]")):
            dl_url = urllib.parse.urljoin(target_url, link)
            if dl_url.startswith(("http://", "https://")):
                sources.append({
                    "name": f"GDFlix [{t_clean}]",
                    "url": dl_url,
                    "quality": q_val,
                    "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": target_url},
                    "is_hls": ".m3u8" in dl_url,
                    "is_dash": False,
                })
        elif "/cflare/" in link or "FAST CLOUD" in t_upper or "ZIPDISK" in t_upper:
            try:
                cf_target = urllib.parse.urljoin(candidate_url, link)
                cf_html = _http_get_text(cf_target, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": candidate_url}, timeout=6.0)
                cf_anchors = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', cf_html, re.I | re.DOTALL)
                for c_href, c_text in cf_anchors:
                    c_clean = re.sub(r"<[^>]+>", "", c_text).strip()
                    c_upper = c_clean.upper()
                    if any(k in c_upper for k in ("CLOUD RESUME", "QUICK DOWNLOAD", "DIRECT DOWNLOAD")) or any(k in c_href.lower() for k in ("workers.dev", "cloudpaglu", "r2.dev")):
                        full_cf_url = urllib.parse.urljoin(cf_target, c_href)
                        if full_cf_url.startswith(("http://", "https://")):
                            sources.append({
                                "name": f"GDFlix [{c_clean}]",
                                "url": full_cf_url,
                                "quality": q_val,
                                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": cf_target},
                                "is_hls": ".m3u8" in full_cf_url,
                                "is_dash": False,
                            })
            except Exception as e:
                logger.debug(f"cflare extraction failed: {e}")
        elif "INSTANT DL" in t_upper or "INSTANT DOWNLOAD" in t_upper:
            try:
                loc = _resolve_short_link(link, timeout=5.0)
                if loc:
                    m_url = re.search(r"url=([^&]+)", loc)
                    real_dl = urllib.parse.unquote(m_url.group(1)) if m_url else loc
                    if "pages.dev/?url=" in real_dl:
                        m_inner = re.search(r"url=([^&]+)", real_dl)
                        if m_inner:
                            real_dl = urllib.parse.unquote(m_inner.group(1))
                    if real_dl.startswith(("http://", "https://")) and "pages.dev" not in real_dl.lower():
                        sources.append({
                            "name": "GDFlix [Instant Download]",
                            "url": real_dl,
                            "quality": q_val,
                            "headers": {"User-Agent": DEFAULT_USER_AGENT},
                            "is_hls": ".m3u8" in real_dl,
                            "is_dash": False,
                            "has_expiring_credentials": True,
                            "created_at": time.time(),
                        })
            except Exception:
                pass
        elif "gofile" in t_upper or "multiup" in link.lower() or "/realtime/multiup.php" in link.lower():
            try:
                mu_url = urllib.parse.urljoin(target_url, link)
                mu_html = _http_get_text(mu_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": target_url}, timeout=6.0)
                m_fast = re.search(r'href=["\'](/download-fast/[^"\']+)["\']', mu_html, re.I)
                if m_fast:
                    dl_fast = urllib.parse.urljoin("https://multiup.io/", m_fast.group(1))
                    sources.append({
                        "name": "GDFlix [MultiUp Fast]",
                        "url": dl_fast,
                        "quality": q_val,
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://multiup.io/"},
                        "is_hls": False,
                        "is_dash": False,
                    })
            except Exception as e:
                logger.debug(f"GDFlix MultiUp extraction failed: {e}")
    return sources


def _extract_multicloudlinks_sources(url, referer=None):
    """
    Extracts playable streams from MultiCloudLinks pages (e.g. new3.multicloudlinks.com/view/...).
    Parses direct download / Turbo download / watch online player sources.
    """
    if not url:
        return []
    clean_url = str(url).strip()
    sources = []
    try:
        html = _http_get_text(clean_url, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": referer or ""}, timeout=8.0)
        m_stream = re.search(r'const\s+streamSrc\s*=\s*["\']([^"\']+)["\']', html)
        if m_stream:
            sources.append({
                "name": "MultiCloud [Direct Stream]",
                "url": m_stream.group(1),
                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": clean_url},
                "is_hls": ".m3u8" in m_stream.group(1),
                "is_dash": False,
            })

        player_m = re.search(r'href=["\'](https?://[^"\']*multicloudlinks\.com/player\.php[^\'"]*)["\']', html)
        if player_m:
            try:
                p_html = _http_get_text(player_m.group(1), headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": clean_url}, timeout=6.0)
                pm_stream = re.search(r'const\s+streamSrc\s*=\s*["\']([^"\']+)["\']', p_html)
                if pm_stream:
                    sources.append({
                        "name": "MultiCloud [Watch]",
                        "url": pm_stream.group(1),
                        "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": player_m.group(1)},
                        "is_hls": ".m3u8" in pm_stream.group(1),
                        "is_dash": False,
                    })
            except Exception:
                pass

        links = re.findall(r'<a\b[^>]*href=["\'](https?://[^\"\']+)["\'][^>]*>(.*?)</a>', html, re.I)
        for h, t in links:
            t_clean = re.sub(r'<[^>]+>', '', t).strip()
            if "pixeldra" in h.lower() or "pixeldrain" in t_clean.lower():
                pd = extract_video_streams(h, referer=clean_url)
                if pd:
                    sources.extend(pd)
            elif "turbo download" in t_clean.lower() or "direct download" in t_clean.lower():
                sources.append({
                    "name": f"MultiCloud [{t_clean}]",
                    "url": h,
                    "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": clean_url},
                    "is_hls": ".m3u8" in h,
                    "is_dash": False,
                })
    except Exception as e:
        logger.debug(f"MultiCloudLinks extraction failed for {clean_url}: {e}")
    return sources


def extract_video_streams(url, referer=None):
    """
    Dispatches a URL to the matching CloudStream extractor.
    Reference: cloudstream/library/.../utils/ExtractorApi.kt loadExtractor
    Returns list of stream dicts, or [] if unsupported.
    """
    if not url or not isinstance(url, str):
        return []
    clean_u = url.strip()
    lower_u = clean_u.lower()

    # 1. Direct stream files (.m3u8, .mp4, .mkv, .mpd)
    # Exclude file locker pages that have .mkv/.mp4 in their page path
    is_locker_page = any(dom in lower_u for dom in (
        "megaup.", "1fichier.", "dailyuploads.", "rapidgator.", "linksmod.", "multiup.", "gofile.io",
        "dropgalaxy.", "uploadmaza.", "vikingfile."
    ))
    path_without_query = lower_u.split("?")[0]
    if not is_locker_page and any(path_without_query.endswith(ext) for ext in (".m3u8", ".mp4", ".mkv", ".mpd")):
        return [{
            "name": "Direct Stream",
            "url": clean_u,
            "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": referer or clean_u},
            "is_hls": path_without_query.endswith(".m3u8"),
            "is_dash": path_without_query.endswith(".mpd"),
        }]

    # 1b. MultiUp mirror pages
    if "multiup." in lower_u:
        return _extract_multiup_mirrors(clean_u, referer=referer)

    # 1c. GDFlix / FastDL / GDLink
    if any(k in lower_u for k in ("gdflix.", "gdlink.")):
        return _extract_gdflix_sources(clean_u, referer=referer)

    # 1d. MultiCloudLinks
    if "multicloudlinks." in lower_u:
        return _extract_multicloudlinks_sources(clean_u, referer=referer)

    # 2. PixelDrain
    if "pixeldrain." in lower_u:
        return extract_pixeldrain_stream(clean_u)

    # 3. HubCDN
    if "hubcdn." in lower_u:
        d = _extract_hubcdn_direct(clean_u, referer=referer)
        if d:
            return [{
                "name": "HubCDN Direct",
                "url": d,
                "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": clean_u},
                "is_hls": False,
                "is_dash": False,
            }]

    # 4. HubCloud / HubDrive / NexDrive / FastDL / VCloud
    if any(k in lower_u for k in ("hubcloud.", "hubdrive.", "nexdrive.", "fastdl.", "vcloud.")):
        return _extract_hubcloud_sources(clean_u, referer=referer)

    # 5. DoodStream
    if any(k in lower_u for k in ("dood.", "doodstream.", "dooood.", "ds2play.", "dsvplay.")):
        return extract_doodstream(clean_u)

    # 6. StreamTape
    if any(k in lower_u for k in ("streamtape.", "watchadsontape.", "shavetape.")):
        return extract_streamtape(clean_u)

    # 7. MixDrop
    if any(k in lower_u for k in ("mixdrop.", "mdy48tn97.com")):
        return extract_mixdrop(clean_u)

    # 8. StreamWish & mirrors
    if any(k in lower_u for k in (
        "streamwish.", "mwish.", "dwish.", "embedwish.", "wishembed.",
        "wishfast.", "sfastwish.", "strwish.", "asnwish.", "cdnwish.", "wishonly.", "playerwish."
    )):
        return extract_streamwish(clean_u)

    # 9. VidHide & mirrors
    if any(k in lower_u for k in (
        "vidhide.", "vidhidepro.", "vidhidehub.", "vidhidevip.", "vidhidepre.",
        "filelions.", "smoothpre.", "dhtpre.", "peytonepre."
    )):
        return extract_vidhide(clean_u)

    # 10. FileMoon
    if "filemoon." in lower_u:
        return extract_filemoon(clean_u)

    # 11. Fastream
    if "fastream." in lower_u:
        return extract_fastream(clean_u)

    # 12. Gofile
    if "gofile.io" in lower_u:
        return extract_gofile(clean_u)

    # 13. VOE & mirrors
    if any(k in lower_u for k in (
        "voe.", "voe-network.", "audaciousdefaulthouse.", "tubelessceliolymph.",
        "simpulumlamerop.", "urochsunloath.", "nathanfromsubject.",
        "yip.su", "metagnathtuggers.", "donaldlineelse.", "charlestoughrace."
    )):
        return extract_voe(clean_u)

    # 14. Mp4Upload
    if "mp4upload." in lower_u:
        return extract_mp4upload(clean_u)

    # 15. Vidoza & mirrors
    if any(k in lower_u for k in ("vidoza.", "videzz.")):
        return extract_vidoza(clean_u)

    return []


_STOP_WORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "at", "by", "from"}
_RELEASE_TAG_WORDS = {
    "movie", "film", "series", "web", "drama", "show", "shows", "tv", "part", "season",
    "complete", "episodes", "episode", "hindi", "english", "dual", "multi", "audio", "dubbed",
    "subbed", "hdrip", "webrip", "bluray", "dvdrip", "remastered", "extended", "unrated"
}


def matches_all_query_terms(query, candidate_title):
    """
    Returns True if ALL significant query terms are present in candidate_title
    (case-insensitive, equivalent to piping multiple grep -i commands:
    e.g. query 'American Greed' requires candidate to match 'American' AND 'Greed').
    """
    if not query or not candidate_title:
        return False
    q_clean, _ = clean_cloudstream_title(query)
    c_clean, _ = clean_cloudstream_title(candidate_title)

    raw_q_words = re.findall(r"\w+", (q_clean or query).lower())
    q_words = [w for w in raw_q_words if w not in _STOP_WORDS and len(w) >= 2]
    if not q_words:
        q_words = [w for w in raw_q_words if len(w) >= 1]
    if not q_words:
        return False

    c_text = (c_clean or candidate_title).lower()
    c_norm = re.sub(r"[^\w\s]", " ", c_text)

    for word in q_words:
        pattern = r"\b" + re.escape(word) + r"\b"
        if not re.search(pattern, c_norm):
            if len(word) >= 4 and word in c_norm:
                continue
            return False

    return True


def _is_strict_title_match(query, candidate):
    if not query or not candidate:
        return False
    q_clean, _ = clean_cloudstream_title(query)
    c_clean, _ = clean_cloudstream_title(candidate)

    q_norm = re.sub(r"[^\w\s]", "", (q_clean or query).lower()).strip()
    c_norm = re.sub(r"[^\w\s]", "", (c_clean or candidate).lower()).strip()
    if not q_norm or not c_norm:
        return False
    if q_norm == c_norm:
        return True

    for text in (candidate, c_clean):
        if not text:
            continue
        for sep in (" - ", " : ", " | ", " – ", " — "):
            if sep in text:
                prefix = text.split(sep, 1)[0].strip()
                p_clean, _ = clean_cloudstream_title(prefix)
                prefix_norm = re.sub(r"[^\w\s]", "", (p_clean or prefix).lower()).strip()
                if prefix_norm == q_norm:
                    return True

    for text in (query, q_clean):
        if not text:
            continue
        for sep in (" - ", " : ", " | ", " – ", " — "):
            if sep in text:
                q_prefix = text.split(sep, 1)[0].strip()
                qp_clean, _ = clean_cloudstream_title(q_prefix)
                qp_norm = re.sub(r"[^\w\s]", "", (qp_clean or q_prefix).lower()).strip()
                if qp_norm == c_norm:
                    return True

    q_words = [w for w in q_norm.split() if w not in _STOP_WORDS] or q_norm.split()
    c_words = [w for w in c_norm.split() if w not in _STOP_WORDS] or c_norm.split()

    q_filtered = [w for w in q_words if w not in _RELEASE_TAG_WORDS] or q_words
    c_filtered = [w for w in c_words if w not in _RELEASE_TAG_WORDS] or c_words

    q_set = set(q_filtered)
    c_set = set(c_filtered)

    if not matches_all_query_terms(query, candidate):
        return False

    if q_set == c_set:
        return True

    overlap = q_set.intersection(c_set)
    if not overlap:
        return False

    if len(q_set) == 1 or len(c_set) == 1:
        return q_set == c_set

    q_ratio = len(overlap) / len(q_set)
    c_ratio = len(overlap) / len(c_set)

    return q_ratio >= 0.75 and c_ratio >= 0.75


CLOUDSTREAM_URL_SKIP_DOMAINS = {
    "github.com", "raw.githubusercontent.com", "github.io", "gitlab.com",
    "pages.dev", "workers.dev", "vercel.app", "netlify.app",
    "google.com", "gstatic.com", "android.com", "cloudflare.com",
    "w3.org", "schema.org", "apache.org", "jsdelivr.net",
    "themoviedb.org", "tmdb.org", "strem.io", "cinemeta.strem.io",
    "buymeacoffee.com", "ko-fi.com", "patreon.com", "paypal.com",
    "t.me", "telegram.org", "discord.gg", "discord.com",
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
    "i.ibb.co", "postimg.cc", "imgur.com", "cloudfront.net", "short.gy",
    "dpdns.org", "crashedserverpanel.xyz", "wyzie.ru",
}


def find_dex_base_url(dex_bytes):
    """
    Extracts the provider's active streaming base URL from Android DEX bytecode.
    First checks Dalvik bytecode assignment to mainUrl (iput-object / sput-object),
    then falls back to frequency scanning of candidate URLs excluding repos/trackers/CDNs.
    """
    if not dex_bytes or len(dex_bytes) < 0x70 or not dex_bytes.startswith(b"dex\n"):
        return None
    try:
        import struct
        string_ids_size, string_ids_off = struct.unpack_from("<II", dex_bytes, 0x38)
        field_ids_size, field_ids_off = struct.unpack_from("<II", dex_bytes, 0x50)

        def _get_str(idx):
            if idx >= string_ids_size:
                return ""
            str_data_off, = struct.unpack_from("<I", dex_bytes, string_ids_off + idx * 4)
            pos = str_data_off
            val, shift = 0, 0
            while True:
                b = dex_bytes[pos]
                pos += 1
                val |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            end = dex_bytes.find(b"\x00", pos)
            return dex_bytes[pos:end].decode("utf-8", errors="replace") if end != -1 else ""

        # 1. Exact field assignment to mainUrl
        main_url_str_id = None
        for i in range(string_ids_size):
            if _get_str(i) == "mainUrl":
                main_url_str_id = i
                break

        if main_url_str_id is not None:
            main_url_field_ids = set()
            for i in range(field_ids_size):
                c_idx, t_idx, n_idx = struct.unpack_from("<HHI", dex_bytes, field_ids_off + i * 8)
                if n_idx == main_url_str_id:
                    main_url_field_ids.add(i)

            for fid in main_url_field_ids:
                fid_bytes = struct.pack("<H", fid)
                pos = 0
                while True:
                    idx = dex_bytes.find(b"\x5b", pos)
                    if idx == -1:
                        break
                    if idx + 4 <= len(dex_bytes) and dex_bytes[idx+2:idx+4] == fid_bytes:
                        window = dex_bytes[max(0, idx-20):idx]
                        for w in range(len(window)-4, -1, -2):
                            if window[w] == 0x1a:
                                str_idx, = struct.unpack_from("<H", window, w+2)
                                s = _get_str(str_idx)
                                if s.startswith("http://") or s.startswith("https://"):
                                    p = urllib.parse.urlparse(s)
                                    if p.netloc and not any(skip in p.netloc.lower() for skip in CLOUDSTREAM_URL_SKIP_DOMAINS):
                                        return f"{p.scheme}://{p.netloc}"
                            elif window[w] == 0x1b and w+6 <= len(window):
                                str_idx, = struct.unpack_from("<I", window, w+2)
                                s = _get_str(str_idx)
                                if s.startswith("http://") or s.startswith("https://"):
                                    p = urllib.parse.urlparse(s)
                                    if p.netloc and not any(skip in p.netloc.lower() for skip in CLOUDSTREAM_URL_SKIP_DOMAINS):
                                        return f"{p.scheme}://{p.netloc}"
                    pos = idx + 1

        # 2. Heuristic scan of all URL strings in DEX
        all_strs = []
        for i in range(string_ids_size):
            s = _get_str(i)
            if s.startswith("http://") or s.startswith("https://"):
                all_strs.append(s)

        candidates = []
        for s in all_strs:
            try:
                p = urllib.parse.urlparse(s)
                h = p.netloc.lower()
                if h and not any(skip in h for skip in CLOUDSTREAM_URL_SKIP_DOMAINS):
                    candidates.append(f"{p.scheme}://{p.netloc}")
            except Exception:
                pass
        if candidates:
            return max(set(candidates), key=candidates.count)
    except Exception as e:
        logger.debug(f"find_dex_base_url error: {e}")
    return None


class GenericCloudStreamProvider(CloudStreamProvider):
    def __init__(self, provider_id, name="CloudStream Provider", base_url=None, supported_types=None):
        self.id = provider_id
        self.name = name
        self.supported_types = list(supported_types) if supported_types else ["movie", "series", "live", "tv", "other"]
        self._base_url = base_url

    def get_base_url(self):
        if getattr(self, "_base_url", None):
            return self._base_url

        plugin = get_plugin(self.id)
        if plugin:
            # 1. Inspect package classes.dex if installed
            storage_key = plugin.get("storage_key")
            if storage_key:
                pkg_dirs = [get_packages_dir()]
                home = Path.home()
                pkg_dirs.append(home / ".var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box/cloudstream_packages")
                pkg_dirs.append(home / ".local/share/popcorn-box/cloudstream_packages")
                for pdir in pkg_dirs:
                    pkg_file = os.path.join(pdir, f"{storage_key}.cs3")
                    if os.path.exists(pkg_file) and zipfile.is_zipfile(pkg_file):
                        try:
                            with zipfile.ZipFile(pkg_file) as z:
                                if "classes.dex" in z.namelist():
                                    dex = z.read("classes.dex")
                                    base = find_dex_base_url(dex)
                                    if base:
                                        self._base_url = base
                                        return self._base_url
                        except Exception:
                            pass
                        break

            # 2. Check icon_url as fallback
            icon = plugin.get("icon_url", "")
            if icon:
                p = urllib.parse.urlparse(icon)
                if p.scheme in ("http", "https") and p.netloc and not any(skip in p.netloc.lower() for skip in CLOUDSTREAM_URL_SKIP_DOMAINS):
                    self._base_url = f"{p.scheme}://{p.netloc}"
                    return self._base_url

        self._base_url = None
        return None

    @property
    def catalog_sections(self):
        base = self.get_base_url()
        if not base:
            return ()
        plugin = get_plugin(self.id)
        raw_types = [str(t).lower() for t in ((plugin.get("tv_types") if plugin else None) or self.supported_types or [])]
        sections = []
        has_movie = any(t in raw_types for t in ("movie", "animemovie", "documentary"))
        has_series = any(t in raw_types for t in ("tvseries", "series", "cartoon", "asiandrama"))
        has_anime = any(t in raw_types for t in ("anime", "ova"))
        has_live = any(t in raw_types for t in ("live", "tv"))

        if has_movie:
            sections.append({
                "id": f"{self.id}#latest-movies",
                "name": "Latest Movies",
                "section": "Latest Movies",
                "type": "movie",
                "supports_pagination": True,
            })
        if has_series:
            sections.append({
                "id": f"{self.id}#latest-series",
                "name": "Latest Series",
                "section": "Latest Series",
                "type": "series",
                "supports_pagination": True,
            })
        if has_anime and not has_series:
            sections.append({
                "id": f"{self.id}#latest-anime",
                "name": "Latest Anime",
                "section": "Latest Anime",
                "type": "anime",
                "supports_pagination": True,
            })
        if has_live and not sections:
            sections.append({
                "id": f"{self.id}#live",
                "name": "Live TV",
                "section": "Live TV",
                "type": "tv",
                "supports_pagination": False,
            })
        if not sections:
            sections.append({
                "id": f"{self.id}#latest",
                "name": "Latest",
                "section": "Latest",
                "type": "movie",
                "supports_pagination": True,
            })
        return tuple(sections)

    def get_main_page(self, page=1):
        base = self.get_base_url()
        if not base:
            return []
        base = base.rstrip("/")
        urls_to_try = []
        if page and int(page) > 1:
            urls_to_try.extend([
                f"{base}/page/{page}/",
                f"{base}/?page={page}",
                f"{base}/?paged={page}",
            ])
        urls_to_try.append(f"{base}/")

        for u in urls_to_try:
            try:
                html = _http_get_text(u, timeout=10.0)
                cards = self._parse_cards(html, base)
                if cards:
                    return [("Latest", cards)]
            except Exception as e:
                logger.debug(f"{self.name} get_main_page failed on {u}: {e}")
        return []

    def _parse_cards(self, page_html, base):
        items = []
        if not page_html:
            return items
        seen_hrefs = set()
        pattern = (
            r"<(?:article|figure|li|div)\b[^>]*?"
            r"(?:class=[\x27\x22][^\x27\x22]*(?:card|post|item|poster|movie|entry|video|film|thumb)[^\x27\x22]*[\x27\x22]|"
            r"id=[\x27\x22][^\x27\x22]*(?:post|movie|item)[^\x27\x22]*[\x27\x22])"
            r"[^>]*>(.*?)</(?:article|figure|li|div)>"
        )
        blocks = re.findall(pattern, page_html, re.I | re.DOTALL)
        if not blocks:
            blocks = re.findall(r"(<(?:h[1-6]|a)\b[^>]*>.*?<img\b[^>]*>.*?</(?:h[1-6]|a)>)", page_html, re.I | re.DOTALL)

        for block in blocks:
            l_m = re.search(r"<a\b[^>]*href=[\x27\x22]([^\x27\x22]+)[\x27\x22][^>]*>", block, re.I)
            if not l_m:
                continue
            href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(l_m.group(1)).strip())
            if href in seen_hrefs or not href.startswith("http"):
                continue
            seen_hrefs.add(href)
            t_m = re.search(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", block, re.I | re.DOTALL)
            if not t_m:
                t_m = re.search(r"<a\b[^>]*title=[\x27\x22]([^\x27\x22]+)[\x27\x22]", block, re.I)
            if not t_m:
                t_m = re.search(r"<img\b[^>]*alt=[\x27\x22]([^\x27\x22]+)[\x27\x22]", block, re.I)
            title = html_lib.unescape(re.sub(r"<[^>]+>", " ", t_m.group(1))).strip() if t_m else ""
            if not title or len(title) < 2:
                continue
            img_m = re.search(r"<img\b[^>]*src=[\x27\x22]([^\x27\x22]+)[\x27\x22]", block, re.I)
            if not img_m:
                img_m = re.search(r"<img\b[^>]*data-(?:src|original|lazy-src)=[\x27\x22]([^\x27\x22]+)[\x27\x22]", block, re.I)
            poster = urllib.parse.urljoin(f"{base}/", img_m.group(1).strip()) if img_m else None
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}\b", title, re.I))
            items.append({
                "provider_id": self.id,
                "data": f"generic:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            })
        return items

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self.get_base_url()
        if not base:
            return []
        base = base.rstrip("/")
        q_enc = urllib.parse.quote(str(query).strip())
        search_urls = [
            f"{base}/?s={q_enc}",
            f"{base}/search/{q_enc}",
            f"{base}/search/{q_enc}/",
        ]
        for search_url in search_urls:
            try:
                page_html = _http_get_text(search_url, timeout=10.0)
                cards = self._parse_cards(page_html, base)
                if cards:
                    return cards
            except Exception as e:
                logger.debug(f"{self.name} search failed on {search_url}: {e}")
        return []

    def load(self, data_str):
        clean_url = str(data_str).replace("generic:", "").strip()
        if clean_url.startswith("http://") or clean_url.startswith("https://"):
            try:
                page_html = _http_get_text(clean_url, timeout=10.0)
            except Exception:
                page_html = ""
            title = ""
            og_t = re.search(r'<meta\b[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', page_html, re.I)
            if og_t:
                title = html_lib.unescape(og_t.group(1)).strip()
            if not title:
                h_m = re.search(r'<h1\b[^>]*>(.*?)</h1>', page_html, re.I | re.DOTALL)
                if h_m:
                    title = re.sub(r'<[^>]+>', '', h_m.group(1)).strip()

            poster = None
            og_img = re.search(r'<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', page_html, re.I)
            if og_img:
                poster = html_lib.unescape(og_img.group(1)).strip()

            desc = ""
            og_d = re.search(r'<meta\b[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', page_html, re.I)
            if og_d:
                desc = html_lib.unescape(og_d.group(1)).strip()

            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))

            return {
                "provider_id": self.id,
                "data": data_str,
                "name": title or self.name,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": None,
                "description": desc or f"CloudStream provider {self.name}",
                "year": year,
                "rating_percent": None,
                "tags": [],
                "episodes": [],
            }

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": self.name,
            "type": CloudStreamTvType.Other,
            "poster_url": None,
            "background_url": None,
            "description": f"CloudStream provider {self.name}",
            "year": None,
            "rating_percent": None,
            "tags": [],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        data_s = str(data_str).replace("generic:", "").strip()
        if not (data_s.startswith("http://") or data_s.startswith("https://")):
            return []

        # 1. Direct stream or direct extractor
        try:
            extracted = extract_video_streams(data_s)
            if extracted:
                return extracted
        except Exception:
            pass

        # 2. Inspect movie page for direct stream links or video extractors
        try:
            page_html = _http_get_text(data_s, timeout=8.0)
        except Exception:
            page_html = ""

        if not page_html:
            return [{
                "name": self.name,
                "url": data_s,
                "is_hls": ".m3u8" in data_s,
                "is_dash": ".mpd" in data_s,
            }]

        all_links = re.findall(r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>', page_html, re.I)

        # Check direct video extractors on page
        for h in all_links[:25]:
            if any(k in h.lower() for k in [
                "filepress", "gdflix", "multicloud", "hubcloud", "hubdrive", "fastdl",
                "pixeldrain", "gofile", "streamwish", "vidhide", "mixdrop", "dood",
                "streamtape", "filemoon", "fastream", "voe", "mp4upload", "vidoza",
                ".m3u8", ".mp4"
            ]):
                try:
                    st = extract_video_streams(h, referer=data_s)
                    if st:
                        return st
                except Exception:
                    pass

        # 3. Check intermediate download/stream link pages
        candidate_intermediates = []
        for h in all_links:
            clean_h = urllib.parse.urljoin(data_s, h.strip())
            h_lower = clean_h.lower()
            if any(k in h_lower for k in [
                "/links/", "/download/", "/file/", "/archives/", "savelinks.", "fastdl.", "hubcloud."
            ]):
                if clean_h not in candidate_intermediates and clean_h != data_s:
                    candidate_intermediates.append(clean_h)

        for inter_url in candidate_intermediates[:4]:
            try:
                inter_html = _http_get_text(inter_url, timeout=8.0)
                inter_links = re.findall(r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>', inter_html, re.I)
                for ih in inter_links:
                    ih_clean = urllib.parse.urljoin(inter_url, ih.strip())
                    if any(k in ih_clean.lower() for k in [
                        "filepress", "gdflix", "multicloud", "hubcloud", "hubdrive", "fastdl",
                        "pixeldrain", "gofile", "streamwish", "vidhide", "mixdrop", "dood",
                        "streamtape", "filemoon", "fastream", "voe", "mp4upload", "vidoza",
                        ".m3u8", ".mp4"
                    ]):
                        st = extract_video_streams(ih_clean, referer=inter_url)
                        if st:
                            return st
            except Exception as e:
                logger.debug(f"Intermediate link extraction failed on {inter_url}: {e}")

        return [{
            "name": self.name,
            "url": data_s,
            "is_hls": ".m3u8" in data_s,
            "is_dash": ".mpd" in data_s,
        }]


# --- Provider Registry ---

_PROVIDERS = {}

def register_provider(provider_instance):
    _PROVIDERS[provider_instance.id] = provider_instance

def get_provider(provider_id):
    if not provider_id:
        return None
    if provider_id in _PROVIDERS:
        return _PROVIDERS[provider_id]
    
    clean_id = str(provider_id).strip().lower()
    for k, p in _PROVIDERS.items():
        if k.lower() == clean_id or (hasattr(p, "id") and p.id.lower() == clean_id):
            return p

    target_name = clean_id.split("#", 1)[-1] if "#" in clean_id else clean_id
    norm_target = target_name.replace("fourk", "4k")
    norm_target2 = target_name.replace("4k", "fourk")

    for k, p in _PROVIDERS.items():
        k_lower = k.lower()
        p_name = p.name.strip().lower() if hasattr(p, "name") else ""
        if (
            k_lower in (target_name, clean_id, norm_target, norm_target2)
            or p_name in (target_name, clean_id, norm_target, norm_target2)
            or (hasattr(p, "id") and p.id.lower().endswith(f"#{target_name}"))
            or (hasattr(p, "id") and p.id.lower().endswith(f"#{norm_target}"))
            or (hasattr(p, "id") and p.id.lower().endswith(f"#{norm_target2}"))
        ):
            return p

    for p in _PROVIDERS.values():
        p_name = p.name.strip().lower() if hasattr(p, "name") else ""
        if p_name and (p_name in target_name or target_name in p_name):
            return p

    # Fallback to Generic provider so every provider has an active instance
    p_name = provider_id.split("#", 1)[1] if "#" in provider_id else provider_id
    plugin = get_plugin(provider_id)
    p_types = None
    if plugin:
        p_name = plugin.get("name") or p_name
        p_types = plugin.get("tv_types") or plugin.get("raw_tv_types")
    return GenericCloudStreamProvider(provider_id, name=p_name, supported_types=p_types)

def get_all_registered_providers():
    return list(_PROVIDERS.values())

# Register built-in providers
register_provider(KickTRCloudStreamProvider())
register_provider(KickGlobalCloudStreamProvider())
register_provider(MegaProviderCloudStreamProvider())
register_provider(TwitchCloudStreamProvider())

dailymotion_prov = DailymotionCloudStreamProvider()
register_provider(dailymotion_prov)
_PROVIDERS["DailymotionProvider"] = dailymotion_prov
_PROVIDERS["Dailymotion"] = dailymotion_prov
_PROVIDERS["https://raw.githubusercontent.com/recloudstream/extensions/master/repo.json#DailymotionProvider"] = dailymotion_prov
_PROVIDERS["https://raw.githubusercontent.com/Luna712/Luna712-CloudStream-Extensions/master/repo.json#DailymotionProvider"] = dailymotion_prov

register_provider(InternetArchiveCloudStreamProvider())

youtube_prov = YoutubeCloudStreamProvider()
register_provider(youtube_prov)
_PROVIDERS["YoutubeProvider"] = youtube_prov
_PROVIDERS["YouTube"] = youtube_prov
_PROVIDERS["https://raw.githubusercontent.com/recloudstream/extensions/master/repo.json#YoutubeProvider"] = youtube_prov

# --- Settings & State in Database ---

def is_cloudstream_enabled():
    return bool(database.get_setting("cloudstream_enabled", True))

def set_cloudstream_enabled(enabled):
    database.set_setting("cloudstream_enabled", bool(enabled))
    database.clear_all_cached_streams_async()

def is_security_warning_accepted():
    return bool(database.get_setting("cloudstream_security_accepted", False))

def accept_security_warning():
    database.set_setting("cloudstream_security_accepted", True)

def get_installed_repositories():
    return database.get_cloudstream_repositories()

def get_all_plugins():
    return database.get_cloudstream_plugins()

def get_plugin(plugin_id):
    for p in get_all_plugins():
        if p.get("id") == plugin_id:
            return p
    return None

def is_provider_runnable(plugin):
    """
    Check if a plugin has a compatible runtime/adapter.
    Returns True only if an active Python provider adapter is registered.
    (GenericCloudStreamProvider has no Python scraper implementation and returns False).
    """
    if not isinstance(plugin, dict):
        return False
    p_id = plugin.get("id")
    if not p_id:
        return False
    prov = get_provider(p_id)
    if not prov or type(prov) is GenericCloudStreamProvider:
        return False
    return True


def is_provider_usable(plugin):
    """
    Check if a plugin can actively be used for searches and stream fetching.
    Returns True if an active Python scraper adapter is registered, or if the
    plugin is installed and its provider has a discoverable base URL.
    """
    if is_provider_runnable(plugin):
        return True
    if not isinstance(plugin, dict) or not plugin.get("is_installed"):
        return False
    p_id = plugin.get("id")
    if not p_id:
        return False
    prov = get_provider(p_id)
    if prov and hasattr(prov, "get_base_url") and prov.get_base_url():
        return True
    return False


_PROVIDER_HEALTH = {}
_PROVIDER_HEALTH_LOCK = threading.Lock()
_PROVIDER_OFFLINE_TTL = 300.0  # 5 minutes


def is_provider_online(provider_id):
    """
    Check if a CloudStream provider is currently online and functional.
    Returns False if the provider has no runnable or usable adapter,
    or if it was marked offline recently (< 5 minutes).
    """
    if not provider_id:
        return False
    prov = get_provider(provider_id)
    if not prov:
        return False
    if type(prov) is GenericCloudStreamProvider:
        if not getattr(prov, "get_base_url", None) or not prov.get_base_url():
            return False

    with _PROVIDER_HEALTH_LOCK:
        entry = _PROVIDER_HEALTH.get(str(provider_id))
        if entry is None and hasattr(prov, "id") and prov.id:
            entry = _PROVIDER_HEALTH.get(str(prov.id))
        if entry is not None:
            is_online, ts = entry
            if not is_online:
                if (time.time() - ts) < _PROVIDER_OFFLINE_TTL:
                    return False
                _PROVIDER_HEALTH.pop(str(provider_id), None)
                if hasattr(prov, "id") and prov.id:
                    _PROVIDER_HEALTH.pop(str(prov.id), None)
    return True


def mark_provider_status(provider_id, is_online, error=None):
    """
    Record provider health status (online=True or online=False).
    """
    if not provider_id:
        return
    with _PROVIDER_HEALTH_LOCK:
        _PROVIDER_HEALTH[str(provider_id)] = (bool(is_online), time.time())
        try:
            prov = get_provider(provider_id)
            if prov and hasattr(prov, "id") and prov.id and str(prov.id) != str(provider_id):
                _PROVIDER_HEALTH[str(prov.id)] = (bool(is_online), time.time())
        except Exception:
            pass


def _catalog_type_matches(catalog_type, media_type):
    catalog_type = str(catalog_type or "").strip().lower()
    media_type = str(media_type or "all").strip().lower()
    if media_type == "movie_series":
        return catalog_type in ("movie", "movies", "series", "anime")
    if media_type in ("", "all", "discover"):
        return True
    if catalog_type == media_type:
        return True
    return media_type == "series" and catalog_type == "anime"


def catalog_url(provider_id):
    """Build an opaque URL for a CloudStream catalog in the shared Discover flow."""
    return "cloudstream://catalog/" + urllib.parse.quote(str(provider_id or ""), safe="")


def provider_id_from_catalog_url(value):
    prefix = "cloudstream://catalog/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        return urllib.parse.unquote(value[len(prefix):]) or None
    except Exception:
        return None


def get_catalogs(media_type="all"):
    """Return browsable sections declared by enabled, runnable or usable CloudStream adapters."""
    if not is_cloudstream_enabled():
        return []

    hide_adult = False
    try:
        hide_adult = database.is_adult_content_hidden()
    except Exception:
        pass

    catalogs = []
    seen = set()
    for plugin in get_all_plugins():
        if not plugin.get("enabled") or not is_provider_usable(plugin):
            continue
        if hide_adult and is_plugin_nsfw(plugin):
            continue
        plugin_id = str(plugin.get("id") or "").strip()
        provider = get_provider(plugin_id)
        if not plugin_id or not provider:
            continue
        if not is_provider_online(plugin_id):
            continue
        for section in getattr(provider, "catalog_sections", ()) or ():
            if not isinstance(section, dict):
                continue
            if hide_adult and is_section_nsfw(section):
                continue
            catalog_id = str(section.get("id") or "").strip()
            catalog_name = str(section.get("name") or section.get("section") or catalog_id).strip()
            section_name = str(section.get("section") or catalog_name).strip()
            catalog_type = str(section.get("type") or "movie").strip().lower()
            key = (provider.id, catalog_id)
            if not catalog_id or not catalog_name or key in seen:
                continue
            if not _catalog_type_matches(catalog_type, media_type):
                continue
            seen.add(key)
            provider_name = str(plugin.get("name") or provider.name or "CloudStream").strip()
            base_p_name = re.sub(r"\s*\((?:cloudstream|cs)\)$", "", provider_name, flags=re.IGNORECASE).strip()
            provider_display_name = f"{base_p_name} (CloudStream)"
            genres = list(section.get("genres") or [])
            years = list(section.get("years") or [])
            supports_year = bool(section.get("supports_year", bool(years)))
            extra = []
            extra_supported = ["skip", "search"]
            if genres:
                extra.append({"name": "genre", "isRequired": False, "options": genres})
                extra_supported.append("genre")
            if supports_year:
                if not years:
                    years = CLOUDSTREAM_POPULAR_YEARS
                extra.append({"name": "year", "isRequired": False, "options": years})
                extra_supported.append("year")

            catalogs.append({
                "addon_name": provider_display_name,
                "manifest_url": catalog_url(plugin_id),
                "base_url": catalog_url(plugin_id),
                "catalog_id": catalog_id,
                "catalog_name": catalog_name,
                "display_name": f"{provider_display_name} - {catalog_name}",
                "genres": genres,
                "years": years,
                "extra": extra,
                "extraSupported": extra_supported,
                "type": catalog_type,
                "supports_pagination": bool(section.get("supports_pagination", False)),
                "_source": "cloudstream",
                "cloudstream_provider_id": plugin_id,
                "cloudstream_section": section_name,
            })
    return catalogs


def clear_main_page_cache():
    """Clear cached provider main pages and catalog sections."""
    with _MAIN_PAGE_CACHE_LOCK:
        _MAIN_PAGE_CACHE.clear()


def on_adult_content_toggled(is_hidden):
    """
    Handle preference toggle for 'Hide 18+ / Explicit Content'.
    Purges main page caches and disables any enabled NSFW plugins.
    """
    clear_main_page_cache()
    if is_hidden:
        for p in get_all_plugins():
            if p.get("enabled") and is_plugin_nsfw(p):
                logger.info(f"Disabling NSFW CloudStream provider '{p.get('name')}' because Hide 18+ is enabled")
                database.toggle_cloudstream_plugin(p["id"], False)


_MAIN_PAGE_INFLIGHT = {}
_MAIN_PAGE_INFLIGHT_LOCK = threading.Lock()


def _get_provider_main_page(provider, page):
    cache_key = (str(provider.id), int(page or 1))
    now = time.time()
    with _MAIN_PAGE_CACHE_LOCK:
        cached = _MAIN_PAGE_CACHE.get(cache_key)
        if cached and now - cached[0] < _MAIN_PAGE_CACHE_TTL_SECONDS:
            return cached[1]

    wait_for_flight = False
    with _MAIN_PAGE_INFLIGHT_LOCK:
        if cache_key in _MAIN_PAGE_INFLIGHT:
            ev = _MAIN_PAGE_INFLIGHT[cache_key]
            wait_for_flight = True
        else:
            ev = threading.Event()
            _MAIN_PAGE_INFLIGHT[cache_key] = ev

    if wait_for_flight:
        ev.wait(timeout=10.0)
        with _MAIN_PAGE_CACHE_LOCK:
            cached = _MAIN_PAGE_CACHE.get(cache_key)
            if cached:
                return cached[1]
        return []

    try:
        try:
            sections = provider.get_main_page(page=page)
        except TypeError:
            sections = provider.get_main_page()
        sections = list(sections or [])
    except Exception as e:
        logger.debug(f"CloudStream catalog fetch failed provider={provider.name!r}: {e}")
        sections = []
    finally:
        with _MAIN_PAGE_INFLIGHT_LOCK:
            _MAIN_PAGE_INFLIGHT.pop(cache_key, None)
        ev.set()

    total_items = 0
    for sec in sections:
        if isinstance(sec, (tuple, list)) and len(sec) >= 2:
            it = sec[1]
        elif isinstance(sec, dict):
            it = sec.get("items") or sec.get("results") or []
        else:
            it = []
        if isinstance(it, list):
            total_items += len(it)

    if total_items > 0:
        mark_provider_status(provider.id, True)
        with _MAIN_PAGE_CACHE_LOCK:
            _MAIN_PAGE_CACHE[cache_key] = (now, sections)
    else:
        mark_provider_status(provider.id, False)
        with _MAIN_PAGE_CACHE_LOCK:
            _MAIN_PAGE_CACHE[cache_key] = (now - _MAIN_PAGE_CACHE_TTL_SECONDS + 15.0, [])

    return sections


def fetch_catalog_items(provider_id, catalog_id, page=1, limit=50, is_cancelled=None, genre=None, query=None, year=None, extra_filters=None):
    """Load one declared CloudStream section and normalize it to Discover cards."""
    if is_cancelled and is_cancelled():
        return []
    plugin = get_plugin(provider_id)
    if not is_cloudstream_enabled() or not plugin or not plugin.get("enabled") or not is_provider_usable(plugin):
        return []

    hide_adult = False
    try:
        hide_adult = database.is_adult_content_hidden()
    except Exception:
        pass
    if hide_adult and is_plugin_nsfw(plugin):
        return []

    provider = get_provider(provider_id)
    if not provider:
        return []

    section = next((
        item for item in (getattr(provider, "catalog_sections", ()) or [])
        if isinstance(item, dict) and str(item.get("id") or "") == str(catalog_id)
    ), None)
    if not section:
        norm_cid = str(catalog_id or "").lower().replace("-", "_")
        section = next((
            item for item in (getattr(provider, "catalog_sections", ()) or [])
            if isinstance(item, dict) and str(item.get("id") or "").lower().replace("-", "_") == norm_cid
        ), None)
    if not section:
        return []
    if hide_adult and is_section_nsfw(section):
        return []

    sec_type = str(section.get("type") or "movie")
    wanted_section = str(section.get("section") or section.get("name") or "").strip().casefold()
    raw_items = []

    if not year and isinstance(extra_filters, dict):
        year = extra_filters.get("year")
    if not genre and isinstance(extra_filters, dict):
        genre = extra_filters.get("genre")

    # 1. Direct provider catalog support (with genre, year, and search query support)
    if hasattr(provider, "get_catalog_items"):
        try:
            try:
                raw_items = provider.get_catalog_items(
                    catalog_id=catalog_id,
                    media_type=sec_type,
                    genre=genre,
                    page=page,
                    query=query,
                    limit=limit,
                    year=year,
                    extra_filters=extra_filters,
                ) or []
            except TypeError:
                raw_items = provider.get_catalog_items(
                    catalog_id=catalog_id,
                    media_type=sec_type,
                    genre=genre,
                    page=page,
                    query=query,
                    limit=limit,
                ) or []
        except Exception as e:
            logger.debug(f"Provider {provider.name} get_catalog_items failed: {e}")
            raw_items = []

    # 2. Fallback to main page parsing if not handled
    if not raw_items and not query and (not genre or genre in ("All", "All Genres", "Genre", "all")):
        all_main_entries = _get_provider_main_page(provider, page)
        for entry in all_main_entries:
            if is_cancelled and is_cancelled():
                return []
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                section_name, items = str(entry[0] or "").strip().casefold(), entry[1]
            elif isinstance(entry, dict):
                section_name = str(entry.get("title") or entry.get("name") or "").strip().casefold()
                items = entry.get("items") or entry.get("results") or []
            else:
                continue
            if section_name == wanted_section or (wanted_section and (wanted_section in section_name or section_name in wanted_section)):
                raw_items = items if isinstance(items, list) else []
                if section_name == wanted_section:
                    break
        if not raw_items and all_main_entries and wanted_section in ("featured", "all", "popular", "home", "default", "main"):
            first_entry = all_main_entries[0]
            if isinstance(first_entry, (tuple, list)) and len(first_entry) >= 2 and isinstance(first_entry[1], list):
                raw_items = first_entry[1]
            elif isinstance(first_entry, dict) and isinstance(first_entry.get("items") or first_entry.get("results"), list):
                raw_items = first_entry.get("items") or first_entry.get("results")

    # 3. Client-side genre filtering if raw_items were extracted from main page
    if raw_items and genre and str(genre).strip().lower() not in ("all", "all genres", "genre", "genres"):
        g_clean = str(genre).strip().lower()
        if any(it.get("genres") for it in raw_items if isinstance(it, dict)):
            filtered = []
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                it_genres = [str(x).strip().lower() for x in (it.get("genres") or [])]
                if any(g_clean in g or g in g_clean for g in it_genres):
                    filtered.append(it)
            if filtered:
                raw_items = filtered

    # 4. Client-side year filtering fallback if raw_items were extracted without direct provider filtering
    if raw_items and year and str(year).strip().lower() not in ("all", "all years", "year", "years"):
        y_str = str(year).strip()
        filtered_by_year = [
            it for it in raw_items
            if isinstance(it, dict) and (
                str(it.get("year") or "") == y_str
                or y_str in str(it.get("name") or "")
                or y_str in str(it.get("title") or "")
            )
        ]
        if filtered_by_year:
            raw_items = filtered_by_year

    cards = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        data = item.get("data") or item.get("url") or item.get("id")
        if not data:
            continue
        sec_type = str(section.get("type") or "movie")
        raw_type = CloudStreamTvType.from_wire(str(item.get("type") or sec_type))
        item_title = item.get("name") or item.get("title") or "Stream"
        if sec_type in ("series", "anime") or is_cloudstream_series(item_title, str(data), CloudStreamTvType.to_popcorn_type(raw_type)):
            raw_type = CloudStreamTvType.TvSeries
        normalized = dict(item)
        normalized["provider_id"] = item.get("provider_id") or provider_id
        normalized["data"] = str(data)
        normalized["name"] = item_title
        normalized["type"] = raw_type
        card = search_item_to_card_dict(normalized)

        if hide_adult:
            try:
                from . import api
                if api.is_adult_item(card) or api.is_adult_item(normalized):
                    continue
            except Exception:
                pass
        if raw_type == CloudStreamTvType.TvSeries:
            card["type"] = "series"
        elif raw_type == CloudStreamTvType.Other:
            card["type"] = sec_type
        card["catalog_name"] = section.get("name") or catalog_id
        card["catalog_id"] = catalog_id
        card["addon_name"] = plugin.get("name") or provider.name
        card["manifest_url"] = catalog_url(provider_id)
        imdb_code = str(item.get("imdb_code") or item.get("imdb_id") or "").strip()
        if imdb_code and re.fullmatch(r"tt\d{7,8}", imdb_code):
            card["alias_ids"] = [imdb_code, card["id"]]
            card["imdb_id"] = imdb_code
        else:
            card["alias_ids"] = [card["id"]]
        for key in ("genres", "description", "overview", "imdbRating", "runtime", "background", "videos", "trailer"):
            if item.get(key) is not None:
                card[key] = item[key]
        year_str = str(item.get("year") or card.get("year") or "")
        if year_str:
            card["year"] = year_str
            card["releaseInfo"] = year_str
        elif card.get("releaseInfo"):
            card["year"] = str(card.get("releaseInfo"))
        cards.append(card)

    page_limit = int(limit) if isinstance(limit, int) and limit > 0 else 50
    cards = cards[:page_limit]
    logger.debug(
        f"CloudStream catalog provider={provider.name!r} catalog={catalog_id!r} "
        f"matched {len(raw_items)} source item(s), returned {len(cards)} card(s)"
    )
    if cards and is_cancelled and is_cancelled():
        return []
    return cards


# --- Repository Installation & Refresh ---

def fetch_repository_data(raw_url):
    """
    Fetches repo.json and all plugin list files for a CloudStream repo.
    Returns (repo_info, list_of_plugins).
    """
    manifest_url = normalize_cloudstream_repository_url(raw_url)
    manifest_text = _http_get_text(manifest_url, timeout=12.0)
    repo_manifest = parse_repository_manifest(manifest_url, manifest_text)

    all_lists = []
    for list_url in repo_manifest.get("plugin_list_urls", []):
        try:
            list_text = _http_get_text(list_url, timeout=12.0)
            plugins = parse_plugin_list(manifest_url, list_url, list_text)
            if plugins:
                all_lists.append(plugins)
        except Exception as e:
            logger.warning(f"Failed to fetch plugin list from {list_url}: {e}")

    if not all_lists:
        raise ValueError("Could not load any plugin lists from repository")

    merged_plugins = merge_plugin_lists(all_lists)
    repo_manifest["plugin_count"] = len(merged_plugins)
    return repo_manifest, merged_plugins


def add_repository(raw_url):
    """
    Add a CloudStream repository by URL.
    Returns (success: bool, message: str, repo_info: dict | None).
    """
    try:
        manifest_url = normalize_cloudstream_repository_url(raw_url)
    except Exception as e:
        return False, f"Invalid repository URL: {e}", None

    existing = [r for r in get_installed_repositories() if r.get("manifest_url") == manifest_url]
    if existing:
        return False, "CloudStream repository is already installed", existing[0]

    try:
        repo_info, plugins = fetch_repository_data(manifest_url)
    except Exception as e:
        logger.error(f"Failed to install CloudStream repository {manifest_url}: {e}")
        return False, f"Failed to load CloudStream repository: {e}", None

    database.save_cloudstream_repository(repo_info)
    database.save_cloudstream_plugins(plugins, replace_for_repo=manifest_url)

    logger.info(f"Installed CloudStream repository '{repo_info['name']}' with {len(plugins)} plugins")
    return True, f"Installed {repo_info['name']} ({len(plugins)} plugins)", repo_info


def refresh_repository(manifest_url):
    """
    Refresh a CloudStream repository while preserving user install/toggle states.
    Returns (success: bool, message: str, repo_info: dict | None).
    """
    try:
        repo_info, new_plugins = fetch_repository_data(manifest_url)
    except Exception as e:
        logger.error(f"Failed to refresh CloudStream repository {manifest_url}: {e}")
        return False, f"Failed to refresh: {e}", None

    prev_plugins = {p["id"]: p for p in get_all_plugins()}
    for p in new_plugins:
        old = prev_plugins.get(p["id"])
        if old:
            p["enabled"] = bool(old.get("enabled", False))
            p["installed_version"] = old.get("installed_version")
            p["is_installed"] = bool(old.get("is_installed", False))
            p["verified"] = bool(old.get("verified", False))

    database.save_cloudstream_repository(repo_info)
    database.save_cloudstream_plugins(new_plugins, replace_for_repo=manifest_url)

    logger.info(f"Refreshed CloudStream repository '{repo_info['name']}' ({len(new_plugins)} plugins)")
    return True, f"Refreshed {repo_info['name']}", repo_info


def remove_repository(manifest_url):
    """Remove repository and delete its downloaded packages."""
    plugins = [p for p in get_all_plugins() if p.get("repository_manifest_url") == manifest_url]
    for p in plugins:
        delete_package(p["storage_key"])
    database.remove_cloudstream_repository(manifest_url)
    logger.info(f"Removed CloudStream repository {manifest_url}")


# --- Plugin Package Management ---

def install_plugin(plugin_id, auto_refreshed=False):
    """
    Downloads and installs a CloudStream .cs3 package, verifies SHA-256 and ZIP safety.
    If a checksum mismatch occurs due to upstream repository updates, automatically
    refreshes the parent repository and retries once.
    Returns (success: bool, message: str).
    """
    plugin = get_plugin(plugin_id)
    if not plugin:
        return False, "Plugin not found"

    pkg_url = plugin.get("package_url")
    if not pkg_url:
        return False, "Plugin package URL is missing"
    pkg_url = _safe_url(pkg_url)

    try:
        data = _http_get_bytes(pkg_url, timeout=25.0)
        if not data:
            return False, "Downloaded package is empty"

        # ZIP inspection & safety checks
        inspect_cs3_package(data)

        # SHA-256 Verification
        expected_hash = plugin.get("file_hash")
        verified = matches_file_hash(data, expected_hash)
        if expected_hash and not verified:
            # Check if upstream repository was updated with a new version/hash
            repo_url = plugin.get("repository_manifest_url")
            if repo_url and not auto_refreshed:
                logger.info(f"SHA-256 mismatch for {plugin.get('name')}. Auto-refreshing repository '{repo_url}' to fetch latest hashes...")
                ok, _, _ = refresh_repository(repo_url)
                if ok:
                    return install_plugin(plugin_id, auto_refreshed=True)
            return False, "SHA-256 checksum mismatch on downloaded package"

        # Size check
        expected_size = plugin.get("file_size")
        if expected_size and len(data) != expected_size:
            logger.warning(f"Package size mismatch for {plugin.get('name')}: expected {expected_size}, got {len(data)}")

        # Atomically save
        save_package_atomically(plugin["storage_key"], data)

        database.update_cloudstream_plugin(plugin_id, {
            "installed_version": plugin.get("version", 0),
            "is_installed": True,
            "verified": verified,
            "enabled": True,
        })
        logger.info(f"Installed CloudStream plugin '{plugin.get('name')}' (v{plugin.get('version')})")
        return True, f"Installed {plugin.get('name')}"
    except Exception as e:
        logger.error(f"Failed to install CloudStream plugin {plugin.get('name')}: {e}")
        return False, f"Installation failed: {e}"


def uninstall_plugin(plugin_id):
    plugin = get_plugin(plugin_id)
    if not plugin:
        return False, "Plugin not found"
    delete_package(plugin["storage_key"])
    database.update_cloudstream_plugin(plugin_id, {
        "installed_version": None,
        "is_installed": False,
        "verified": False,
        "enabled": False,
    })
    logger.info(f"Uninstalled CloudStream plugin '{plugin.get('name')}'")
    return True, f"Uninstalled {plugin.get('name')}"


def toggle_plugin(plugin_id, enabled):
    if enabled:
        try:
            if database.is_adult_content_hidden():
                p = get_plugin(plugin_id)
                if is_plugin_nsfw(p):
                    logger.info(f"Refusing to enable NSFW CloudStream plugin '{plugin_id}': Hide 18+ is active")
                    database.toggle_cloudstream_plugin(plugin_id, False)
                    return False
        except Exception:
            pass
    database.toggle_cloudstream_plugin(plugin_id, bool(enabled))
    return True


def bulk_install_and_enable(plugin_ids, only_compatible=True):
    """Install and enable multiple plugins in parallel."""
    installed = 0
    enabled = 0
    errors = []
    hide_adult = False
    try:
        hide_adult = database.is_adult_content_hidden()
    except Exception:
        pass

    targets = []
    for pid in plugin_ids:
        plugin = get_plugin(pid)
        if not plugin:
            continue
        if only_compatible and not is_provider_runnable(plugin):
            continue
        if hide_adult and is_plugin_nsfw(plugin):
            logger.debug(f"Skipping NSFW plugin {plugin.get('name')} during bulk install (Hide 18+ active)")
            continue
        targets.append(plugin)

    def _process_one(plugin):
        pid = plugin["id"]
        inst = False
        if not plugin.get("is_installed") or plugin.get("version", 0) > (plugin.get("installed_version") or 0):
            ok, msg = install_plugin(pid)
            if not ok:
                return False, False, f"{plugin.get('name')}: {msg}"
            inst = True
        toggled = toggle_plugin(pid, True)
        return inst, toggled, None

    if len(targets) <= 1:
        for p in targets:
            inst, toggled, err = _process_one(p)
            if inst:
                installed += 1
            if toggled:
                enabled += 1
            if err:
                errors.append(err)
    else:
        max_workers = min(len(targets), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_one, p): p for p in targets}
            for fut in as_completed(futures):
                try:
                    inst, toggled, err = fut.result()
                    if inst:
                        installed += 1
                    if toggled:
                        enabled += 1
                    if err:
                        errors.append(err)
                except Exception as e:
                    errors.append(str(e))

    return {
        "installed": installed,
        "enabled": enabled,
        "errors": errors,
    }


# --- PopcornBox Media Models Conversion ---

def clean_cloudstream_title(title_text):
    """
    Cleans release tags, audio formats, codec info, language/movie suffixes,
    and site branding from CloudStream catalog titles for clean UI display.
    Returns (clean_title, year).
    """
    if not title_text:
        return "", None
    text = html_lib.unescape(str(title_text)).strip()
    text = re.sub(r"^(?:download|permanent link to|watch|stream|view)\s+", "", text, flags=re.IGNORECASE).strip()
    # Strip site branding suffix (e.g. ~ BollyFlix, - HDHub4u, ~ MoviesMod)
    text = re.sub(
        r"\s*[-~|@]\s*(?:bollyflix|hdhub4u|moviesmod|moviesdrive|vegamovies|dudefilms|zinkmovies|cinestream|katmoviehd|luxmovies|topmovies|extrapost|filmyfly|filmyzilla|desiremovies|mkvking|allmovieshub|rogmovies|toonstream).*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", text)
    year = year_match.group(1) if year_match else None

    split_match = re.split(
        r"\s*(?:\[[^\]]*\]|\{[^\}]*\}|\|.*$|\b(?:unrated|uncut|extended(?:\s+edition)?|director'?s\s+cut|theatrical(?:\s+cut)?|limited|proper|repack|remaster(?:ed)?|anniversary\s+edition|special\s+edition|blu[ .-]?ray|brrip|web[ .-]?dl|webrip|hdrip|hq-hdrip|dvdrip|hdtv|hdtc|hq-hdtc|ds4k|imax|predvd|hdcam|camrip|dvd|x26[45]|hevc|h26[45]|avc|esubs?|multi\s*subs?|atmos|eac3|dts|aac|ac3|(?:2160|1080|720|480)p|4k|dual\s*audio|multi\s*audio|full\s*movie|full\s*series|primevideo|netflix|amzn|disney\+?|hulu|zee5|sonyliv|hotstar|jiocinema|(?:hindi|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|english|korean|japanese|chinese|spanish|french|german|russian|italian|south)\s+(?:movie|film|series|web\s*series|drama|dubbed))\b)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    base = split_match[0].strip(" .-_:|~()[]{}")
    clean = base
    while True:
        prev = clean
        clean = re.sub(
            r"[\s(\[]*(?:(?:19|20)\d{2}|Season\s*\d+(?:\s*[-–&,]\s*\d+)?|S\d{1,2}(?:\s*[-–&,]\s*S?\d{1,2})?|Complete\s*(?:Series)?|All\s*Episodes?|Episodes?\s*\d+(?:\s*[-–&,]\s*\d+)?)[)\]]*\s*$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip(" .-_:|~()[]{}")
        clean = re.sub(
            r"[\s(\[]*(?:(?:hindi|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|english|korean|japanese|chinese|spanish|french|german|russian|italian|south)\s+(?:movie|film|series|web\s*series|drama|dubbed)|dual\s*audio|multi\s*audio|dubbed|subbed)[)\]]*\s*$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip(" .-_:|~()[]{}")
        if clean == prev:
            break
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or base or text, year


def search_item_to_card_dict(item):
    """Converts a CloudStream search item into a standard PopcornBox movie card dict."""
    route_id = cloudstream_route_id(item["provider_id"], item["data"])
    raw_type = item.get("type", CloudStreamTvType.Other)
    media_type = CloudStreamTvType.to_popcorn_type(raw_type)

    raw_name = item.get("name") or "Stream"
    clean_name, extracted_year = clean_cloudstream_title(raw_name)
    year_val = str(item.get("year") or extracted_year or "")

    if media_type != "series" and is_cloudstream_series(raw_name, item.get("data", ""), media_type):
        media_type = "series"

    is_adult = (
        raw_type == CloudStreamTvType.NSFW
        or media_type == "adult"
        or any(k in str(raw_name).lower() for k in ("18+", "nsfw", "xxx", "porn", "hentai", "erotica"))
    )

    card = {
        "id": route_id,
        "imdb_id": route_id,
        "title": clean_name or raw_name,
        "name": clean_name or raw_name,
        "catalog_title": raw_name,
        "raw_title": raw_name,
        "original_title": raw_name,
        "type": media_type,
        "medium_cover_image": item.get("poster_url"),
        "poster": item.get("poster_url"),
        "background": item.get("background_url"),
        "year": year_val,
        "releaseInfo": year_val,
        "description": item.get("description") or "",
        "is_cloudstream": True,
        "cloudstream_provider_id": item["provider_id"],
        "adult": is_adult,
        "isAdult": is_adult,
    }
    imdb_code = str(item.get("imdb_code") or item.get("imdb_id") or "").strip()
    card["alias_ids"] = [imdb_code, route_id] if re.fullmatch(r"tt\d{7,8}", imdb_code) else [route_id]
    return card


def load_item_to_details_dict(loaded):
    """Converts a CloudStreamLoadItem into a PopcornBox details dictionary for MovieDetailsPage."""
    route_id = cloudstream_route_id(loaded["provider_id"], loaded["data"])
    raw_type = loaded.get("type", CloudStreamTvType.Other)
    episodes = loaded.get("episodes") or []
    raw_name = loaded.get("name") or "Stream"
    clean_name, extracted_year = clean_cloudstream_title(raw_name)
    year_val = str(loaded.get("year") or extracted_year or "")

    is_series = bool(episodes) or raw_type == CloudStreamTvType.TvSeries or is_cloudstream_series(raw_name, loaded.get("data", ""), CloudStreamTvType.to_popcorn_type(raw_type))
    media_type = "series" if is_series else CloudStreamTvType.to_popcorn_type(raw_type)

    is_adult = (
        raw_type == CloudStreamTvType.NSFW
        or media_type == "adult"
        or any(k in str(raw_name).lower() for k in ("18+", "nsfw", "xxx", "porn", "hentai", "erotica"))
    )

    videos = []
    if not episodes:
        videos.append({
            "id": route_id,
            "title": "Live Stream" if raw_type == CloudStreamTvType.Live else (clean_name or raw_name),
            "season": 1 if is_series else None,
            "episode": 1 if is_series else None,
            "thumbnail": loaded.get("background_url") or loaded.get("poster_url"),
            "overview": loaded.get("description"),
            "available": True,
        })
    else:
        for ep in episodes:
            ep_route = cloudstream_route_id(loaded["provider_id"], ep.get("data", ""))
            s_val = ep.get("season")
            if s_val is None and is_series:
                s_val = 1
            e_val = ep.get("episode")
            if e_val is None and is_series:
                e_val = 1
            videos.append({
                "id": ep_route,
                "title": ep.get("name") or f"Episode {e_val or 1}",
                "season": s_val,
                "episode": e_val,
                "thumbnail": ep.get("poster_url") or loaded.get("poster_url"),
                "overview": ep.get("description"),
                "available": True,
            })

    rating_pct = loaded.get("rating_percent")
    rating_str = f"{rating_pct / 10:.1f}".rstrip("0").rstrip(".") if rating_pct is not None else ""

    return {
        "id": route_id,
        "imdb_id": route_id,
        "title": clean_name or raw_name,
        "name": clean_name or raw_name,
        "catalog_title": raw_name,
        "raw_title": raw_name,
        "original_title": raw_name,
        "type": media_type,
        "medium_cover_image": loaded.get("poster_url"),
        "poster": loaded.get("poster_url"),
        "background": loaded.get("background_url"),
        "description": loaded.get("description") or "",
        "year": year_val,
        "runtime": "Live" if raw_type == CloudStreamTvType.Live else "",
        "genre": ", ".join(loaded.get("tags") or []),
        "imdbRating": rating_str,
        "videos": videos,
        "is_cloudstream": True,
        "cloudstream_provider_id": loaded["provider_id"],
        "adult": is_adult,
        "isAdult": is_adult,
    }


def playback_source_to_stream_dict(source, provider_name, provider_id):
    """Converts a CloudStreamPlaybackSource dict into a PopcornBox stream dictionary for MPV."""
    raw_url = str(source.get("url") or source.get("magnet") or "").strip()
    if not raw_url:
        return None
    if not (raw_url.startswith("http://") or raw_url.startswith("https://") or raw_url.startswith("magnet:")):
        logger.debug(f"[CloudStream] Dropped non-HTTP/non-magnet stream URL: {raw_url}")
        return None
    raw_url_lower = raw_url.lower()
    if any(k in raw_url_lower for k in ("/movies-by-quality/", "/movies-by-size/", "/category/", "/how-to-download/", "/tag/", "/genres/", "/join/")):
        logger.warning(f"[CloudStream] Dropped navigation/category URL mistakenly passed as stream: {raw_url}")
        return None
    if ("tv.imgcdn.kim" in raw_url or "netmirror" in raw_url) and ("?in=" not in raw_url or "stop abusing" in raw_url.lower()):
        logger.warning(f"[CloudStream] Dropped FreeCDN honeypot/unsigned stream: {raw_url}")
        return None
    if "youtube.com" in raw_url or "youtu.be" in raw_url:
        m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", raw_url)
        if not m or " " in raw_url or "%20" in raw_url:
            logger.warning(f"[CloudStream] Dropped invalid YouTube stream: {raw_url}")
            return None
    headers = dict(source.get("headers") or {})
    if "|" in raw_url:
        clean_url, pipe_headers = raw_url.split("|", 1)
        clean_url = clean_url.strip()
        for pair in pipe_headers.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                headers[k.strip()] = v.strip()
    else:
        clean_url = raw_url

    referer = source.get("referer")
    if referer and "Referer" not in headers:
        headers["Referer"] = referer

    quality = source.get("quality")
    quality_str = f"{quality}p" if quality in (144, 240, 360, 480, 720, 1080, 1440, 2160) else (str(quality) if quality else "")

    name = source.get("name") or provider_name
    explicit_title = source.get("stream_title") or source.get("title")
    if explicit_title:
        full_title = explicit_title
    else:
        title_parts = [p for p in [name, quality_str] if p and p.lower() not in name.lower()]
        full_title = " · ".join(title_parts) if title_parts else name

    is_torrent = bool(
        source.get("is_torrent")
        or clean_url.startswith("magnet:")
        or clean_url.endswith(".torrent")
        or source.get("info_hash")
        or source.get("infoHash")
        or source.get("hash")
    )
    is_hls = not is_torrent and (source.get("is_hls") or ".m3u8" in clean_url.split("?")[0].lower() or "hls" in clean_url.lower())
    is_dash = not is_torrent and (source.get("is_dash") or ".mpd" in clean_url.split("?")[0].lower())

    info_hash = str(source.get("info_hash") or source.get("infoHash") or source.get("hash") or "").lower()
    if is_torrent and not info_hash and clean_url.startswith("magnet:"):
        from .libtorrent_stream import info_hash_from_magnet
        info_hash = str(info_hash_from_magnet(clean_url) or "").lower()

    subtitles = []
    for s in source.get("subtitles") or []:
        subtitles.append({
            "url": s.get("url"),
            "lang": s.get("language") or "en",
            "name": s.get("name") or s.get("language"),
            "headers": s.get("headers"),
        })

    behavior_hints = {}
    if headers:
        behavior_hints["notWebReady"] = True
        behavior_hints["proxyHeaders"] = {"request": headers}
        behavior_hints["headers"] = headers

    sources_list = list(source.get("sources") or [])
    if is_torrent and clean_url.startswith("magnet:"):
        if "&tr=" not in clean_url:
            tracker_query = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in DEFAULT_TRACKERS)
            clean_url = f"{clean_url}{tracker_query}"
        if not sources_list:
            sources_list = [f"tracker:{t}" for t in DEFAULT_TRACKERS]

    stream_dict = {
        "name": name,
        "title": full_title,
        "stream_title": full_title,
        "url": clean_url,
        "quality": quality_str,
        "headers": headers,
        "subtitles": subtitles,
        "sources": sources_list,
        "behaviorHints": behavior_hints,
        "addon_name": provider_name,
        "addon_names": [provider_name],
        "is_cloudstream": True,
        "is_torrent": bool(is_torrent),
        "is_http": not is_torrent,
        "stream_type": "torrent" if is_torrent else ("hls" if is_hls else ("dash" if is_dash else "direct")),
        "cloudstream_provider_id": provider_id,
        "has_expiring_credentials": bool(source.get("has_expiring_credentials")),
        "saved_at": source.get("saved_at") or source.get("created_at") or time.time(),
    }

    if is_torrent:
        stream_dict["is_torrent"] = True
        stream_dict["magnet"] = clean_url if clean_url.startswith("magnet:") else (f"magnet:?xt=urn:btih:{info_hash}" if info_hash else "")
        stream_dict["infoHash"] = info_hash
        stream_dict["hash"] = info_hash
        if source.get("seeds") is not None or source.get("seeders") is not None:
            stream_dict["seeders"] = source.get("seeds") or source.get("seeders")
        if source.get("peers") is not None:
            stream_dict["peers"] = source.get("peers")
        if source.get("size") is not None:
            stream_dict["size"] = source.get("size")
        if source.get("size_bytes") is not None:
            stream_dict["size_bytes"] = source.get("size_bytes")
        if source.get("file_idx") is not None or source.get("fileIdx") is not None:
            f_idx = source.get("file_idx") if source.get("file_idx") is not None else source.get("fileIdx")
            stream_dict["file_idx"] = f_idx
            stream_dict["fileIdx"] = f_idx

    return stream_dict


# --- High Level Operations: Details & Streams Resolution ---

def get_metadata_for_route(route_id):
    """
    Called when opening a MovieDetailsPage for a cloudstream: route.
    Calls the provider's load(data) and returns details dict.
    """
    parsed = parse_cloudstream_route_id(route_id)
    if not parsed:
        return None
    provider_id, data = parsed
    provider = get_provider(provider_id)
    if not provider:
        logger.warning(f"No CloudStream provider adapter found for {provider_id}")
        return None

    try:
        loaded = provider.load(data)
        return load_item_to_details_dict(loaded)
    except Exception as e:
        logger.error(f"CloudStream provider.load failed for {route_id}: {e}")
        return None


def get_streams_for_route(route_id, season=None, episode=None):
    """
    Resolves streams for a cloudstream: route.
    Calls the provider's load_links(data, season=season, episode=episode) and returns list of stream dicts.
    """
    parsed = parse_cloudstream_route_id(route_id)
    if not parsed:
        return []
    provider_id, data = parsed
    provider = get_provider(provider_id)
    if not provider:
        return []

    plugin = get_plugin(provider_id)
    p_name = plugin.get("name") if plugin else provider.name

    try:
        import inspect
        sig = inspect.signature(provider.load_links)
        if "season" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            sources = provider.load_links(data, season=season, episode=episode)
        else:
            sources = provider.load_links(data)
        streams = []
        for s in (sources or []):
            st = playback_source_to_stream_dict(s, p_name, provider_id)
            if st:
                streams.append(st)
        return streams
    except Exception as e:
        logger.error(f"CloudStream load_links failed for {route_id}: {e}")
        return []


def search_all_enabled_providers(query, timeout=10.0):
    """
    Searches all enabled, runnable CloudStream providers in parallel.
    Returns list of movie card dicts.
    """
    if not is_cloudstream_enabled() or not query or not query.strip():
        return []

    hide_adult = False
    try:
        hide_adult = database.is_adult_content_hidden()
    except Exception:
        pass

    enabled_plugins = [
        p for p in get_all_plugins()
        if p.get("enabled") and is_provider_usable(p) and not (hide_adult and is_plugin_nsfw(p))
    ]
    if not enabled_plugins:
        return []

    seen_impls = set()
    deduped_plugins = []
    for p in enabled_plugins:
        provider = get_provider(p.get("id"))
        if not provider:
            continue
        impl_key = getattr(provider, "id", None) or id(provider)
        if impl_key not in seen_impls:
            seen_impls.add(impl_key)
            deduped_plugins.append(p)
    enabled_plugins = deduped_plugins

    results = []

    def _search_worker(plugin):
        provider = get_provider(plugin["id"])
        if not provider:
            return []
        try:
            items = provider.search(query.strip())
            cards = [
                search_item_to_card_dict(it) for it in items
                if matches_all_query_terms(query, it.get("name") or "")
            ]
            if hide_adult:
                try:
                    from . import api
                    cards = [c for c in cards if not api.is_adult_item(c)]
                except Exception:
                    pass
            return cards
        except Exception as e:
            logger.debug(f"CloudStream search on {provider.name} failed: {e}")
            return []

    ex = ThreadPoolExecutor(max_workers=min(len(enabled_plugins), 16))
    try:
        futures = [ex.submit(_search_worker, p) for p in enabled_plugins]
        done, _ = concurrent.futures.wait(futures, timeout=timeout)
        for f in done:
            try:
                cards = f.result()
                if cards:
                    results.extend(cards)
            except Exception:
                pass
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)

    return results


def fetch_cloudstream_streams_for_media(
    title, media_type, year=None, season=None, episode=None, timeout=12.0, imdb_id=None, catalog_title=None, exclude_route=None
):
    """
    Searches enabled CloudStream providers for a standard movie or episode,
    resolves matching streams, and returns them for PopcornBox's streams list.
    """
    if not is_cloudstream_enabled() or not title or not title.strip():
        return []

    hide_adult = False
    try:
        hide_adult = database.is_adult_content_hidden()
    except Exception:
        pass
    if hide_adult:
        try:
            from . import api
            if api.is_adult_item({"name": title, "title": title, "genres": []}):
                return []
        except Exception:
            pass

    enabled_plugins = [
        p for p in get_all_plugins()
        if p.get("enabled") and is_provider_usable(p) and not (hide_adult and is_plugin_nsfw(p))
    ]
    if not enabled_plugins:
        return []

    excluded_provider_id = None
    excluded_provider_name = None
    if exclude_route:
        parsed_route = parse_cloudstream_route_id(exclude_route)
        if parsed_route:
            excluded_provider_id = parsed_route[0]
            ex_prov = get_provider(excluded_provider_id)
            if ex_prov:
                excluded_provider_name = getattr(ex_prov, "name", None)

    seen_impls = set()
    deduped_plugins = []
    for p in enabled_plugins:
        provider = get_provider(p.get("id"))
        if not provider:
            continue
        impl_key = getattr(provider, "id", None) or id(provider)
        if impl_key not in seen_impls:
            seen_impls.add(impl_key)
            deduped_plugins.append(p)
    enabled_plugins = deduped_plugins

    all_streams = []

    def _normalized_type(value):
        value = str(value or "").strip()
        if not value:
            return None
        types = {
            CloudStreamTvType.Movie.lower(): "movie",
            CloudStreamTvType.AnimeMovie.lower(): "movie",
            CloudStreamTvType.Documentary.lower(): "movie",
            CloudStreamTvType.TvSeries.lower(): "series",
            CloudStreamTvType.Anime.lower(): "series",
            CloudStreamTvType.Ova.lower(): "series",
            CloudStreamTvType.Cartoon.lower(): "series",
            CloudStreamTvType.AsianDrama.lower(): "series",
            CloudStreamTvType.Live.lower(): "tv",
            CloudStreamTvType.Music.lower(): "tv",
            "anime": "series",
            "ova": "series",
            "cartoon": "series",
            "asiandrama": "series",
            "animemovie": "movie",
            "documentary": "movie",
            "other": "other",
            "show": "series",
            "tvseries": "series",
        }
        return types.get(value.lower(), value.lower())

    requested_type = str(media_type or "movie").strip().lower()
    norm_type = _normalized_type(requested_type) or "movie"

    def _year(value, fallback_text=""):
        match = re.search(r"\b((?:18|19|20)\d{2})\b", str(value or ""))
        if not match:
            match = re.search(r"\b((?:18|19|20)\d{2})\b", str(fallback_text or ""))
        return match.group(1) if match else None

    requested_year = _year(year, title)

    def _worker(plugin):
        provider = get_provider(plugin["id"])
        if not provider:
            return []
        if excluded_provider_id and (
            plugin.get("id") == excluded_provider_id
            or getattr(provider, "id", None) == excluded_provider_id
            or (excluded_provider_name and (plugin.get("name") == excluded_provider_name or getattr(provider, "name", None) == excluded_provider_name))
        ):
            return []
        # Check if provider supports this media type
        if getattr(provider, "id", "") == MegaProviderCloudStreamProvider.id or isinstance(provider, MegaProviderCloudStreamProvider):
            return []
        supported_types = {str(value).lower() for value in (provider.supported_types or [])}
        if (
            norm_type in ("movie", "series")
            and requested_type not in supported_types
            and norm_type not in supported_types
            and "all" not in supported_types
        ):
            return []

        try:
            p_name = plugin.get("name") or provider.name
            if hasattr(provider, "get_streams_for_media"):
                try:
                    direct_sources = provider.get_streams_for_media(
                        title=title.strip(),
                        media_type=requested_type,
                        year=year,
                        season=season,
                        episode=episode,
                        imdb_id=imdb_id,
                    )
                    if direct_sources:
                        out = []
                        for s in direct_sources:
                            sd = playback_source_to_stream_dict(s, p_name, plugin["id"])
                            if sd:
                                out.append(sd)
                        if out:
                            logger.debug(f"CloudStream direct lookup returned {len(out)} stream(s) provider={p_name}")
                            return out
                except Exception as e:
                    logger.debug(f"Direct stream fetch on {provider.name} failed: {e}")
            if not hasattr(provider, "search"):
                return []

            import inspect
            try:
                search_parameters = inspect.signature(provider.search).parameters
                accepts_imdb_id = "imdb_id" in search_parameters
            except (TypeError, ValueError):
                accepts_imdb_id = False

            queries = [title.strip()]
            if catalog_title and str(catalog_title).strip() and str(catalog_title).strip() != title.strip():
                clean_cat, clean_yr = clean_cloudstream_title(str(catalog_title).strip())
                if clean_cat and clean_cat not in queries:
                    queries.append(clean_cat)
                if str(catalog_title).strip() not in queries:
                    queries.append(str(catalog_title).strip())
            if requested_year:
                queries.append(f"{title.strip()} {requested_year}")
            if norm_type == "series" and season is not None and episode is not None:
                try:
                    queries.append(f"{title.strip()} S{int(season):02d}E{int(episode):02d}")
                except (TypeError, ValueError):
                    pass
            attempted_data = set()

            for query in dict.fromkeys(queries):
                try:
                    search_items = (
                        provider.search(query, imdb_id=imdb_id)
                        if accepts_imdb_id
                        else provider.search(query)
                    ) or []
                except Exception as e:
                    logger.debug(f"CloudStream search failed provider={p_name!r} query={query!r}: {e}")
                    continue
                if not search_items:
                    continue

                typed_items = [
                    item for item in search_items
                    if isinstance(item, dict)
                    and (
                        not _normalized_type(item.get("type"))
                        or _normalized_type(item.get("type")) in (norm_type, "other")
                    )
                    and (
                        matches_all_query_terms(title, item.get("name") or "")
                        or (catalog_title and matches_all_query_terms(str(catalog_title), item.get("name") or ""))
                        or (imdb_id and str(imdb_id).startswith("tt") and str(imdb_id) in str(item.get("data") or ""))
                    )
                ]
                candidate_items = typed_items if norm_type in ("movie", "series") else (typed_items or list(search_items))
                scored = []
                ranked_fallbacks = []
                for order, item in enumerate(candidate_items):
                    if not isinstance(item, dict) or not item.get("data"):
                        continue
                    data_key = str(item["data"])
                    if data_key in attempted_data:
                        continue
                    candidate_name = str(item.get("name") or "").strip()
                    candidate_year = _year(item.get("year"), candidate_name)
                    if requested_year and candidate_year and candidate_year != requested_year:
                        continue
                    has_imdb_match = bool(
                        imdb_id
                        and str(imdb_id).startswith("tt")
                        and str(imdb_id) in data_key
                    )
                    title_matches = _is_strict_title_match(title, candidate_name) or (
                        catalog_title and _is_strict_title_match(str(catalog_title), candidate_name)
                    )
                    if not title_matches and not has_imdb_match:
                        # CloudStream search results are already ranked by the
                        # provider. Keep year-verified localized titles as a
                        # fallback only when there is genuine title word overlap.
                        if requested_year and candidate_year == requested_year:
                            if matches_all_query_terms(title, candidate_name):
                                ranked_fallbacks.append((order, item))
                        continue

                    score = 100 if has_imdb_match else (60 if candidate_name.casefold() == title.casefold() else 40)
                    if candidate_year == requested_year and requested_year:
                        score += 25
                    scored.append((score, order, item))

                scored.sort(key=lambda candidate: (-candidate[0], candidate[1]))
                matches = [item for _score, _order, item in scored[:3]]
                if not matches:
                    matches = [item for _order, item in ranked_fallbacks[:3]]

                for match in matches:
                    match_data = str(match.get("data") or "")
                    if not match_data:
                        continue
                    attempted_data.add(match_data)
                    try:
                        loaded = provider.load(match_data)
                    except Exception as e:
                        logger.debug(f"CloudStream load failed provider={p_name!r}: {e}")
                        continue
                    if not isinstance(loaded, dict):
                        continue

                    loaded_type = _normalized_type(loaded.get("type"))
                    if loaded_type and loaded_type not in (norm_type, "other"):
                        continue
                    loaded_name = str(loaded.get("name") or "").strip()
                    loaded_year = _year(loaded.get("year"), loaded_name)
                    match_year = _year(match.get("year"), match.get("name"))
                    if requested_year and any(
                        resolved_year and resolved_year != requested_year
                        for resolved_year in (match_year, loaded_year)
                    ):
                        continue
                    if (
                        loaded_name
                        and not _is_strict_title_match(title, loaded_name)
                        and not (catalog_title and _is_strict_title_match(str(catalog_title), loaded_name))
                        and not (imdb_id and str(imdb_id).startswith("tt") and str(imdb_id) in match_data)
                    ):
                        continue

                    if norm_type == "series":
                        def _episode_number(value):
                            try:
                                return int(value)
                            except (TypeError, ValueError):
                                return None

                        link_data = None
                        req_s = _episode_number(season)
                        req_e = _episode_number(episode)
                        for ep in loaded.get("episodes") or []:
                            ep_s = _episode_number(ep.get("season"))
                            ep_e = _episode_number(ep.get("episode"))
                            s_matches = (ep_s == req_s) or (req_s in (None, 1) and ep_s is None) or (req_s is None and ep_s == 1)
                            e_matches = (ep_e == req_e) or (req_e is None)
                            if s_matches and e_matches:
                                link_data = ep.get("data")
                                break
                        if not link_data:
                            if not loaded.get("episodes"):
                                link_data = loaded.get("data") or match_data
                            else:
                                logger.debug(
                                    f"CloudStream: no matching episode S{season}E{episode} in {provider.name}"
                                )
                                continue
                    else:
                        link_data = loaded.get("data") or match_data

                    if not link_data:
                        continue
                    try:
                        import inspect
                        sig = inspect.signature(provider.load_links)
                        if "season" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                            sources = provider.load_links(link_data, season=season, episode=episode) or []
                        else:
                            sources = provider.load_links(link_data) or []
                    except Exception as e:
                        logger.debug(f"CloudStream loadLinks failed provider={p_name!r}: {e}")
                        continue
                    out = [
                        stream for source in sources
                        if (stream := playback_source_to_stream_dict(source, p_name, plugin["id"]))
                    ]
                    if out:
                        logger.debug(f"CloudStream resolved {len(out)} stream(s) provider={p_name}")
                        return out

            return []
        except Exception as e:
            logger.debug(f"CloudStream matching on {provider.name} failed: {e}")
            return []

    ex = ThreadPoolExecutor(max_workers=min(len(enabled_plugins), 16))
    try:
        futures = [ex.submit(_worker, p) for p in enabled_plugins]
        done, _ = concurrent.futures.wait(futures, timeout=timeout)
        for f in done:
            try:
                res = f.result()
                if res:
                    all_streams.extend(res)
            except Exception:
                pass
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)

    return all_streams
