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
        err_str = str(e).upper()
        if "UNEXPECTED_EOF" in err_str or "EOF OCCURRED" in err_str or "SSL" in err_str or isinstance(e, urllib.error.URLError):
            try:
                from .api import _doh_fetch
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
        err_str = str(e).upper()
        if "UNEXPECTED_EOF" in err_str or "EOF OCCURRED" in err_str or "SSL" in err_str or isinstance(e, urllib.error.URLError):
            try:
                from .api import _doh_fetch
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
    Other = "Other"

    @classmethod
    def from_wire(cls, val):
        if not val or not isinstance(val, str):
            return cls.Other
        v = val.strip().lower()
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
        if v == "asiandrama":
            return cls.AsianDrama
        if v == "music":
            return cls.Music
        if v == "torrent":
            return cls.Torrent
        return cls.Other

    @classmethod
    def to_popcorn_type(cls, tv_type):
        if tv_type in (cls.Movie, cls.AnimeMovie, cls.Documentary):
            return "movie"
        if tv_type in (cls.TvSeries, cls.Anime, cls.Ova, cls.Cartoon, cls.AsianDrama):
            return "series"
        if tv_type in (cls.Live, cls.Music):
            return "tv"
        return "movie"


def cloudstream_plugin_id(repo_url, internal_name):
    norm_repo = normalize_cloudstream_repository_url(repo_url)
    val = f"{norm_repo}#{internal_name}"
    storage_key = hashlib.sha256(val.encode("utf-8")).hexdigest()
    return val, storage_key


def parse_cloudstream_file_hash(val):
    if not val or not isinstance(val, str):
        return None
    val = val.strip().lower()
    if "-" not in val:
        return None
    algo, hex_str = val.split("-", 1)
    if algo != "sha256" or len(hex_str) != 64 or not all(c in "0123456789abcdef" for c in hex_str):
        return None
    return {"algorithm": algo, "hex": hex_str, "wire": val}


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
            if isinstance(t, str) and t.strip():
                raw_tv_types.append(t.strip())

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
    """
    if not html:
        return {}
    episodes = {}

    # Pattern 1: Heading above links (e.g. <h5>EP01...</h5> or -:Episodes: 01:- or <h4>Episode 1</h4>)
    sections = re.split(r"(?i)(?:<h[3-6][^>]*>\s*(?:EP|Episode)\s*(\d{1,2})|-?:\s*Episodes?:\s*(\d{1,2})\s*:-?)", html)
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
                    if any(k in h_clean.lower() for k in ["hubcloud", "fastdl", "nexdrive", "drive", "filebee", "gdflix", "pixeldrain", "only-apis", "dl.", "vcloud"]):
                        if not any(existing_h == h_clean for existing_h, _ in episodes.get(ep_num, [])):
                            episodes.setdefault(ep_num, []).append((h_clean, re.sub(r"<[^>]+>", "", t).strip()))
            i += 3

    # Pattern 2: Anchors containing Episode text
    anchor_matches = re.finditer(r"""<a\b[^>]*href=["\x27](https?://[^"\x27]+)["\x27][^>]*>(.*?)</a>""", html, re.I | re.DOTALL)
    for m in anchor_matches:
        h, t = m.group(1).strip(), m.group(2)
        clean_t = re.sub(r"<[^>]+>", " ", t).strip()
        ep_m = re.search(r"\b(?:EP|Episode)\.?\s*(\d{1,2})\b", clean_t, re.I)
        if ep_m:
            ep_num = int(ep_m.group(1))
            if any(k in h.lower() for k in ["hubcloud", "fastdl", "nexdrive", "drive", "filebee", "gdflix", "pixeldrain", "dl.", "thenaukriadda", "fastdlserver", "vcloud"]):
                if not any(existing_h == h for existing_h, _ in episodes.get(ep_num, [])):
                    episodes.setdefault(ep_num, []).append((h, clean_t))

    return episodes


# --- Built-in CloudStream Provider Architecture ---

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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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


class YoutubeCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/recloudstream/extensions/builds/YoutubeProvider.cs3#YoutubeProvider"
    name = "YoutubeProvider"
    supported_types = ["other", "live", "clip"]

    def search(self, query):
        if not query or not query.strip():
            return []

        # 1. Try yt-dlp first for reliable search
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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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

        # 2. Invidious fallback
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
            "referer": "https://www.youtube.com/",
            "headers": {"Referer": "https://www.youtube.com/"},
            "subtitles": [],
            "is_hls": False,
            "is_dash": False,
        }]


# --- Dynamic Domains Provider Helper ---

_DYNAMIC_DOMAINS_CACHE = {}
_DYNAMIC_DOMAINS_CACHE_TIME = 0

def get_dynamic_domain(key, default=None):
    """Resolves active domain for CloudStream scrapers with fallback and 1-hour cache."""
    global _DYNAMIC_DOMAINS_CACHE, _DYNAMIC_DOMAINS_CACHE_TIME
    now = time.time()
    if not _DYNAMIC_DOMAINS_CACHE or (now - _DYNAMIC_DOMAINS_CACHE_TIME) > 3600:
        for url in [
            "https://raw.githubusercontent.com/phisher98/TVVVV/refs/heads/main/domains.json",
            "https://raw.githubusercontent.com/SaurabhKaperwan/Utils/refs/heads/main/urls.json",
        ]:
            try:
                txt = _http_get_text(url, timeout=3.0)
                data = json.loads(txt)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, str) and v.startswith("http"):
                            _DYNAMIC_DOMAINS_CACHE[k.lower()] = v.rstrip("/")
                    _DYNAMIC_DOMAINS_CACHE_TIME = now
                    break
            except Exception:
                pass
    clean_k = key.lower()
    return _DYNAMIC_DOMAINS_CACHE.get(clean_k, default)


class BollyflixCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#Bollyflix"
    name = "Bollyflix"
    supported_types = ["movie", "series", "tv", "other"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://bollyflix.church"

    def _get_base_url(self):
        return get_dynamic_domain("bollyflix", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"Bollyflix catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        articles = re.findall(r"<article[^>]*>.*?</article>", page_html, re.DOTALL | re.IGNORECASE)
        for art in articles:
            link_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', art, re.I)
            if not link_m:
                continue
            href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_m.group(1)).strip())
            if href in seen:
                continue

            img_m = re.search(r'<img\b[^>]*>', art, re.I)
            img_tag = img_m.group(0) if img_m else ""
            poster = None
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            t_candidates = []
            h_m = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', art, re.I | re.DOTALL)
            if h_m:
                t_candidates.append(h_m.group(1))
            t_m = re.search(r'<a\b[^>]*title=["\']([^"\']+)["\']', art, re.I)
            if t_m:
                t_candidates.append(t_m.group(1))
            if img_m:
                alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
                if alt_m:
                    t_candidates.append(alt_m.group(1))

            title = ""
            for cand in t_candidates:
                c = html_lib.unescape(re.sub(r'<[^>]+>', ' ', cand))
                c = re.sub(r'^Download\s+', '', c, flags=re.I).strip(' .-|\t\r\n')
                c = re.sub(r'\s+', ' ', c)
                if c:
                    title = c
                    break
            if not title:
                continue
            seen.add(href)
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"bollyflix:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        encoded = urllib.parse.quote(query.strip())
        url = f"{base}/?s={encoded}"
        try:
            html = _http_get_text(url, timeout=8.0)
            articles = re.findall(r"<article[^>]*>.*?</article>", html, re.DOTALL | re.IGNORECASE)
            items = []
            seen = set()
            for art in articles:
                link_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', art, re.I)
                if not link_m:
                    continue
                href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_m.group(1)).strip())
                if href in seen:
                    continue
                seen.add(href)

                img_m = re.search(r'<img\b[^>]*>', art, re.I)
                img_tag = img_m.group(0) if img_m else ""
                poster = None
                for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                    pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                    if pm and not pm.group(1).startswith("data:"):
                        poster = html_lib.unescape(pm.group(1)).strip()
                        break

                t_m = re.search(r'<a\b[^>]*title=["\']([^"\']+)["\']', art, re.I)
                h_m = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', art, re.I | re.DOTALL)
                raw_title = t_m.group(1) if t_m else (h_m.group(1) if h_m else "")
                clean_title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
                clean_title = re.sub(r"^Download\s+", "", clean_title, flags=re.I).strip()
                if not clean_title:
                    continue
                y_match = re.search(r"\((\d{4})\)", clean_title)
                year = int(y_match.group(1)) if y_match else None
                is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", clean_title, re.I))

                items.append({
                    "provider_id": self.id,
                    "data": f"bollyflix:{href}",
                    "name": clean_title,
                    "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                    "poster_url": poster,
                    "background_url": poster,
                    "description": clean_title,
                    "year": year,
                })
            return items
        except Exception as e:
            logger.debug(f"Bollyflix search failed: {e}")
            return []

    def load(self, data_str):
        href = data_str.split("bollyflix:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "Bollyflix Movie"
            img_match = re.search(r"<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html, re.I)
            if not img_match:
                img_match = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)[\"']", html, re.I)
            poster = html_lib.unescape(img_match.group(1)).strip() if img_match else None
        except Exception:
            title = "Bollyflix Movie"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:fxlinks|linksmod|elinks)[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href
                    })
                    with urllib.request.urlopen(m_req, timeout=5.0) as mr:
                        m_html = mr.read().decode("utf-8", errors="ignore")
                    archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"Bollyflix archive episode parse error: {e}")

            if archive_eps:
                ep_numbers = sorted(list(archive_eps.keys()))
            else:
                ep_match = re.search(r"(?:Ep|Episode)\.?\s*(\d+)", title, re.I)
                max_ep = int(ep_match.group(1)) if ep_match else 1
                ep_numbers = list(range(1, max(1, max_ep) + 1))

            for ep_num in ep_numbers:
                episodes.append({
                    "name": f"Episode {ep_num}",
                    "season": season_num,
                    "episode": ep_num,
                    "data": f"bollyflix:{href}#s={season_num}&e={ep_num}",
                    "poster_url": poster,
                    "description": f"Season {season_num} Episode {ep_num}",
                })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["Bollyflix"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("bollyflix:")[-1].strip()
        target_season = int(season) if season is not None else None
        target_episode = int(episode) if episode is not None else None
        if "#" in href:
            base_href, fragment = href.split("#", 1)
            href = base_href.strip()
            s_m = re.search(r"\bs=(\d+)", fragment)
            e_m = re.search(r"\be=(\d+)", fragment)
            if s_m and target_season is None: target_season = int(s_m.group(1))
            if e_m and target_episode is None: target_episode = int(e_m.group(1))

        try:
            html = _http_get_text(href, timeout=8.0)
            links = re.findall(r"<a\s+[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.I | re.DOTALL)
            sources = []
            for target_url, text in links:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if any(k in target_url for k in ["fastdlserver", "gdflix", "gdlink", "sidexfee", "drive", "hubcloud", "download", "link", "linksmod", "fxlinks"]):
                    quality_str = ""
                    for q in ["2160p", "1080p", "720p", "480p"]:
                        if q in clean_text.lower() or q in target_url.lower():
                            quality_str = q
                            break

                    # If fxlinks / archive page, extract specific episode links
                    if ("fxlinks." in target_url or "/elinks/" in target_url) and target_episode is not None:
                        try:
                            l_req = urllib.request.Request(target_url, headers={
                                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": href
                            })
                            with urllib.request.urlopen(l_req, timeout=6.0) as lr:
                                l_html = lr.read().decode("utf-8", errors="ignore")
                            arc_eps = parse_archive_episodes(l_html)
                            for ep_target_url, _ in arc_eps.get(target_episode, []):
                                if "hubcloud." in ep_target_url:
                                    hc_sources = _extract_hubcloud_sources(ep_target_url, referer=target_url)
                                    for hs in hc_sources:
                                        ep_pfx = f" S{target_season}E{target_episode}" if target_season and target_episode else ""
                                        sources.append({
                                            "name": f"Bollyflix{ep_pfx} {quality_str} ({hs.get('name')})".strip(),
                                            "url": hs.get("url"),
                                            "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                                            "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                                            "is_hls": ".m3u8" in hs.get("url", ""),
                                            "is_dash": False,
                                        })
                                elif "fastdlserver" in ep_target_url or "dl." in ep_target_url:
                                    resolved = _resolve_short_link(ep_target_url, timeout=5.0)
                                    if resolved and resolved != ep_target_url:
                                        ep_pfx = f" S{target_season}E{target_episode}" if target_season and target_episode else ""
                                        sources.append({
                                            "name": f"Bollyflix{ep_pfx} {quality_str} (FastDL)".strip(),
                                            "url": resolved,
                                            "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                                            "headers": {"User-Agent": "Mozilla/5.0"},
                                            "is_hls": ".m3u8" in resolved,
                                            "is_dash": False,
                                        })
                        except Exception as e:
                            logger.debug(f"Bollyflix fxlinks episode extraction error: {e}")

                    # If linksmod page, extract direct locker links (gofile, megaup, mixdrop, etc.)
                    elif "linksmod." in target_url and "/view/" in target_url:
                        try:
                            l_req = urllib.request.Request(target_url, headers={
                                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": href
                            })
                            with urllib.request.urlopen(l_req, timeout=6.0) as lr:
                                l_html = lr.read().decode("utf-8", errors="ignore")
                            sub_links = re.findall(r'<a[^>]+href=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>', l_html, re.DOTALL)
                            for sub_h, sub_t in sub_links:
                                if any(dom in sub_h.lower() for dom in ["gofile.io", "megaup.net", "mixdrop"]):
                                    sources.append({
                                        "name": f"Bollyflix {quality_str} ({sub_h.split('/')[2]})".strip(),
                                        "url": sub_h,
                                        "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                                        "headers": {"User-Agent": "Mozilla/5.0"},
                                        "is_hls": ".m3u8" in sub_h,
                                        "is_dash": False,
                                    })
                        except Exception as e:
                            logger.debug(f"Bollyflix linksmod resolution failed: {e}")

                    # HubCloud extract
                    elif "hubcloud." in target_url:
                        hc_sources = _extract_hubcloud_sources(target_url, referer=href)
                        for hs in hc_sources:
                            sources.append({
                                "name": f"Bollyflix {quality_str} ({hs.get('name')})".strip(),
                                "url": hs.get("url"),
                                "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                                "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                                "is_hls": ".m3u8" in hs.get("url", ""),
                                "is_dash": False,
                            })

                    sources.append({
                        "name": f"Bollyflix {quality_str} ({clean_text})" if quality_str else f"Bollyflix ({clean_text})",
                        "url": target_url,
                        "subtitles": [],
                        "is_hls": ".m3u8" in target_url,
                        "is_dash": False,
                    })
            if sources:
                return sources
        except Exception as e:
            logger.debug(f"Bollyflix load_links failed: {e}")
        return [{
            "name": "Bollyflix Source",
            "url": href,
            "subtitles": [],
            "is_hls": False,
            "is_dash": False,
        }]


# --- Shared Helpers for Link Extraction ---

def _extract_hubcdn_direct(hubcdn_url, referer=None):
    """
    Extracts direct Google Video stream from a hubcdn.* link.
    """
    try:
        req = urllib.request.Request(hubcdn_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
            req = urllib.request.Request(cur, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
            try:
                res = opener.open(req, timeout=5.0)
                break
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        break
                    cur = loc
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
    Resolves a hubdrive, hubcloud, nexdrive, or fastdl link down to direct playable video streams.
    """
    sources = []
    try:
        # Step 0: NexDrive / FastDL / VCloud resolution
        if any(k in url for k in ["nexdrive.", "fastdl.", "vcloud."]):
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
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
                    "headers": {"User-Agent": "Mozilla/5.0"},
                    "is_http": True,
                })
                return sources

        # Step 1: If HubDrive, extract HubCloud link
        if "hubdrive." in url:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

        # Step 2: On hubcloud.pw/drive/..., extract hubcloud.php link
        if "hubcloud." in url and "hubcloud.php" not in url:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": referer or url
            })
            with urllib.request.urlopen(req, timeout=8.0) as r:
                html = r.read().decode("utf-8", errors="ignore")
            m = re.search(r'<a[^>]+href=[\"\'](https?://[^\"\']*hubcloud\.php[^\"\']*)[\"\']', html)
            if m:
                url = m.group(1)
                referer = url
            else:
                return []

        # Step 3: On hubcloud.php page, extract direct video streams
        if "hubcloud.php" in url:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": referer or url
            })
            with urllib.request.urlopen(req, timeout=8.0) as r:
                html = r.read().decode("utf-8", errors="ignore")
            dl_matches = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
            for dl_href, dl_text in dl_matches:
                dl_clean = re.sub(r'<[^>]+>', '', dl_text).strip()
                if any(k in dl_clean.lower() or k in dl_href.lower() for k in [".mkv", ".mp4", "download file", "server :", "pixel"]):
                    final_url = dl_href
                    if "hubcloud.pw" in dl_href:
                        resolved = _resolve_hubcloud_redirect(dl_href)
                        if resolved and resolved.startswith("http"):
                            final_url = resolved
                    sources.append({
                        "name": dl_clean,
                        "url": final_url,
                        "headers": {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Referer": url}
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
    Extracts HubCloud, HubDrive, and direct streaming links from an hblinks page.
    """
    sources = []
    try:
        html = _http_get_text(target_url, headers={"Referer": referer or target_url}, timeout=8.0)
        hc_links = re.findall(
            r'href\s*=\s*[\"\'](https?://[^\"\']*(?:hubcloud|hubdrive|hubcdn|gdflix|vcloud|nexdrive|fastdl)[^\"\']*)[\"\']',
            html,
            re.IGNORECASE,
        )
        for h in hc_links:
            if "hubcdn." in h:
                d = _extract_hubcdn_direct(h, referer=target_url)
                if d:
                    sources.append({"name": "HubCDN Direct", "url": d, "headers": {"Referer": h}})
            else:
                sources.extend(_extract_hubcloud_sources(h, referer=target_url))
    except Exception as e:
        logger.debug(f"Hblinks extraction error for {target_url}: {e}")
    return sources


class HDhub4uCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#HDhub4u"
    name = "HDhub4u"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://new12.hdhub4u.cl"

    def _get_base_url(self):
        return get_dynamic_domain("hdhub4u", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"HDhub4u catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen_hrefs = set()
        # Themes can mix figure/article cards, release list items, and bare
        # title links on the same page. Collect each shape, then deduplicate by
        # destination instead of letting one unrelated figure hide the lists.
        blocks = []
        for pattern in (
            r"<figure\b[^>]*>.*?</figure>",
            r"<article\b[^>]*>.*?</article>",
            r"<li\b[^>]*>.*?</li>",
            r"<a\b[^>]*>.*?</a\s*>",
        ):
            blocks.extend(re.findall(pattern, page_html, re.IGNORECASE | re.DOTALL))

        for block in blocks:
            link_match = re.search(r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>", block, re.IGNORECASE)
            if not link_match:
                continue
            href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_match.group(1)).strip())
            parsed_href = urllib.parse.urlsplit(href)
            if parsed_href.scheme not in ("http", "https") or not parsed_href.netloc:
                continue
            if href in seen_hrefs:
                continue

            img_match = re.search(r"<(?:img|source)\b[^>]*>", block, re.IGNORECASE | re.DOTALL)
            img_tag = img_match.group(0) if img_match else ""
            poster_match = None
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                poster_match = re.search(
                    rf"\b{attr}\s*=\s*[\"']([^\"']+)[\"']",
                    img_tag,
                    re.IGNORECASE,
                )
                if poster_match:
                    break
            poster = html_lib.unescape(poster_match.group(1)).strip() if poster_match else None

            title_candidates = []
            heading_match = re.search(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", block, re.IGNORECASE | re.DOTALL)
            if heading_match:
                title_candidates.append(heading_match.group(1))
            title_match = re.search(r"<a\b[^>]*title\s*=\s*[\"']([^\"']+)[\"']", block, re.IGNORECASE)
            if title_match:
                title_candidates.append(title_match.group(1))
            alt_match = re.search(r"\balt\s*=\s*[\"']([^\"']+)[\"']", img_tag, re.IGNORECASE)
            if alt_match:
                title_candidates.append(alt_match.group(1))
            text_match = re.search(r"<a\b[^>]*>(.*?)</a\s*>", block, re.IGNORECASE | re.DOTALL)
            if text_match:
                title_candidates.append(text_match.group(1))

            title = ""
            for candidate in title_candidates:
                candidate = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", candidate, flags=re.IGNORECASE | re.DOTALL)
                candidate = html_lib.unescape(re.sub(r"<[^>]+>", " ", candidate))
                candidate = re.sub(r"\s+", " ", candidate).strip(" .-|\t\r\n")
                if candidate and candidate.lower() not in {"home", "latest releases", "latest movies", "latest series", "search"}:
                    title = candidate
                    break
            if not title:
                continue

            year_match = re.search(r"\b((?:18|19|20)\d{2})\b", title)
            is_series = bool(
                re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.IGNORECASE)
            )
            # Ignore navigation links while retaining poster cards with no
            # year in their label.
            if not poster and not year_match and not is_series:
                continue
            seen_hrefs.add(href)
            year = int(year_match.group(1)) if year_match else None
            poster_url = urllib.parse.urljoin(f"{base}/", poster) if poster else None
            item = {
                "provider_id": self.id,
                "data": f"hdhub4u:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster_url,
                "background_url": poster_url,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        logger.debug(
            f"HDhub4u catalog page {page} parsed {len(movies)} movie(s) and {len(series)} series"
        )
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        items = []

        # 1. Pingora Typesense API search
        try:
            encoded = urllib.parse.quote(clean_q)
            api_url = (
                f"https://search.pingora.fyi/collections/post/documents/search?"
                f"q={encoded}&query_by=post_title,category,stars,director,imdb_id&"
                f"query_by_weights=4,2,2,2,4&sort_by=sort_by_date:desc&limit=15&"
                f"highlight_fields=none&use_cache=true&page=1"
            )
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"{base}/search.html?q={encoded}",
                "Origin": base,
            })
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                for hit in data.get("hits", []):
                    doc = hit.get("document", {})
                    p_title = doc.get("post_title") or ""
                    permalink = doc.get("permalink") or ""
                    if not permalink and doc.get("slug"):
                        permalink = f"{base}/{doc['slug'].lstrip('/')}"
                    elif permalink.startswith("/"):
                        permalink = f"{base}{permalink}"
                    elif not permalink.startswith("http"):
                        permalink = f"{base}/{permalink.lstrip('/')}"

                    if not permalink.startswith(base):
                        path = urllib.parse.urlparse(permalink).path
                        permalink = f"{base}{path}"

                    y_match = re.search(r"\b((?:18|19|20)\d{2})\b", p_title)
                    year = int(y_match.group(1)) if y_match else None
                    thumb = doc.get("post_thumbnail")
                    is_series = bool(
                        re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", p_title, re.IGNORECASE)
                    )

                    items.append({
                        "provider_id": self.id,
                        "data": f"hdhub4u:{permalink}",
                        "name": p_title,
                        "imdb_code": doc.get("imdb_id"),
                        "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                        "poster_url": thumb,
                        "background_url": thumb,
                        "description": p_title,
                        "year": year,
                    })
        except Exception as e:
            logger.debug(f"HDhub4u pingora search failed: {e}")

        # 2. Fallback to HTML search if pingora returned 0 items
        if not items:
            try:
                encoded = urllib.parse.quote(clean_q)
                html_url = f"{base}/?s={encoded}"
                html = _http_get_text(html_url, timeout=8.0)
                blocks = []
                for pattern in (
                    r"<figure\b[^>]*>.*?</figure>",
                    r"<article\b[^>]*>.*?</article>",
                    r"<li\b[^>]*>.*?</li>",
                    r"<a\b[^>]*>.*?</a\s*>",
                ):
                    blocks.extend(re.findall(pattern, html, re.IGNORECASE | re.DOTALL))
                seen_results = set()
                for block in blocks:
                    link_match = re.search(
                        r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>",
                        block,
                        re.IGNORECASE,
                    )
                    if not link_match:
                        continue
                    full_href = urllib.parse.urljoin(base + "/", html_lib.unescape(link_match.group(1)).strip())
                    if full_href in seen_results:
                        continue
                    img_match = re.search(r"<(?:img|source)\b[^>]*>", block, re.IGNORECASE | re.DOTALL)
                    img_tag = img_match.group(0) if img_match else ""
                    title_candidates = []
                    for candidate_pattern in (
                        r"<a\b[^>]*title\s*=\s*[\"']([^\"']+)[\"']",
                        r"\balt\s*=\s*[\"']([^\"']+)[\"']",
                        r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>",
                        r"<a\b[^>]*>(.*?)</a\s*>",
                    ):
                        candidate_match = re.search(candidate_pattern, block, re.IGNORECASE | re.DOTALL)
                        if candidate_match:
                            title_candidates.append(candidate_match.group(1))
                    clean_title = ""
                    for candidate in title_candidates:
                        candidate = html_lib.unescape(re.sub(r"<[^>]+>", " ", candidate))
                        candidate = re.sub(r"\s+", " ", candidate).strip(" .-|\t\r\n")
                        if candidate and candidate.lower() not in {"home", "search", "latest releases"}:
                            clean_title = candidate
                            break
                    if not clean_title:
                        continue
                    y_match = re.search(r"\b((?:18|19|20)\d{2})\b", clean_title)
                    year = int(y_match.group(1)) if y_match else None
                    is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", clean_title, re.IGNORECASE))
                    poster_match = None
                    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                        poster_match = re.search(rf"\b{attr}\s*=\s*[\"']([^\"']+)[\"']", img_tag, re.IGNORECASE)
                        if poster_match:
                            break
                    if not poster_match and not year and not is_series:
                        continue
                    poster = urllib.parse.urljoin(base + "/", html_lib.unescape(poster_match.group(1))) if poster_match else None
                    seen_results.add(full_href)

                    items.append({
                        "provider_id": self.id,
                        "data": f"hdhub4u:{full_href}",
                        "name": clean_title,
                        "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                        "poster_url": poster,
                        "background_url": poster,
                        "description": clean_title,
                        "year": year,
                    })
            except Exception as e:
                logger.debug(f"HDhub4u HTML fallback search failed: {e}")

        return items

    def load(self, data_str):
        href = data_str.split("hdhub4u:")[-1].strip()
        base = self._get_base_url()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = html_lib.unescape(re.sub(r"<[^>]+>", " ", t_match.group(1))).strip() if t_match else "HDhub4u Video"
            img_match = re.search(r"<img[^>]+src=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html)
            poster = urllib.parse.urljoin(base + "/", html_lib.unescape(img_match.group(1))) if img_match else None
            episodes = []
            seen_episodes = set()
            page_season_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", title, re.IGNORECASE)
            page_season = int(page_season_match.group(1) or page_season_match.group(2)) if page_season_match else None

            for anchor in re.finditer(
                r"<a\b([^>]*)href\s*=\s*[\"']([^\"']+)[\"']([^>]*)>(.*?)</a\s*>",
                html,
                re.IGNORECASE | re.DOTALL,
            ):
                attrs = anchor.group(1) + " " + anchor.group(3)
                episode_name = html_lib.unescape(re.sub(r"<[^>]+>", " ", anchor.group(4)))
                episode_name = re.sub(r"\s+", " ", episode_name).strip()
                title_attr = re.search(r"\btitle\s*=\s*[\"']([^\"']+)[\"']", attrs, re.IGNORECASE)
                if title_attr:
                    episode_name = html_lib.unescape(title_attr.group(1)).strip() or episode_name

                episode_match = re.search(
                    r"\bS(\d{1,2})\s*[-_. ]*E(\d{1,2})\b|\b(\d{1,2})x(\d{1,2})\b",
                    episode_name,
                    re.IGNORECASE,
                )
                if episode_match and episode_match.group(1) is not None:
                    season_num, episode_num = int(episode_match.group(1)), int(episode_match.group(2))
                elif episode_match and episode_match.group(3) is not None:
                    season_num, episode_num = int(episode_match.group(3)), int(episode_match.group(4))
                else:
                    long_episode = re.search(
                        r"\bSeason\s*(\d{1,2}).*?Episode\s*(\d{1,2})\b",
                        episode_name,
                        re.IGNORECASE,
                    )
                    short_episode = re.search(r"\bEpisode\s*(\d{1,2})\b", episode_name, re.IGNORECASE)
                    if long_episode:
                        season_num, episode_num = int(long_episode.group(1)), int(long_episode.group(2))
                    elif short_episode and page_season is not None:
                        season_num, episode_num = page_season, int(short_episode.group(1))
                    else:
                        continue

                episode_href = urllib.parse.urljoin(href, html_lib.unescape(anchor.group(2)).strip())
                if urllib.parse.urlsplit(episode_href).netloc.casefold() != urllib.parse.urlsplit(base).netloc.casefold():
                    continue
                episode_key = (season_num, episode_num, episode_href)
                if episode_key in seen_episodes:
                    continue
                seen_episodes.add(episode_key)
                episodes.append({
                    "name": episode_name or f"S{season_num:02d}E{episode_num:02d}",
                    "season": season_num,
                    "episode": episode_num,
                    "data": f"hdhub4u:{episode_href}",
                    "poster_url": poster,
                })

            # Some search results point directly at an episode page, with no
            # separate episode anchor on that page.
            direct_episode = re.search(
                r"\bS(\d{1,2})\s*[-_. ]*E(\d{1,2})\b|\b(\d{1,2})x(\d{1,2})\b",
                title,
                re.IGNORECASE,
            )
            if not episodes and direct_episode:
                if direct_episode.group(1) is not None:
                    season_num, episode_num = int(direct_episode.group(1)), int(direct_episode.group(2))
                else:
                    season_num, episode_num = int(direct_episode.group(3)), int(direct_episode.group(4))
                episodes.append({
                    "name": title,
                    "season": season_num,
                    "episode": episode_num,
                    "data": data_str,
                    "poster_url": poster,
                })
        except Exception:
            title = "HDhub4u Video"
            poster = None
            episodes = []

        is_series = bool(
            episodes or re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.IGNORECASE)
        )

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["HDhub4u"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("hdhub4u:")[-1].strip()
        base = self._get_base_url()
        sources = []
        try:
            html = _http_get_text(href, headers={
                "Referer": f"{base.rstrip('/')}/",
                "Accept-Language": "en-US,en;q=0.9",
            }, timeout=12.0)

            links = re.findall(
                r'<a\b[^>]*href\s*=\s*[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a\s*>',
                html,
                re.IGNORECASE | re.DOTALL,
            )
            # The page theme sometimes embeds server URLs in onclick/data
            # values rather than a visible anchor href.
            known_url_re = re.compile(
                r"https?://[^\s\"'<>]+(?:hubcdn|hubdrive|hubcloud|playdrive|nexdrive|fastdl|vcloud|gdflix|greenmotors|hblinks|greenmountmotors)[^\s\"'<>]*",
                re.IGNORECASE,
            )
            links.extend((match.group(0), "") for match in known_url_re.finditer(html))

            seen_links = set()
            seen_sources = set()
            tasks = []
            for raw_url, raw_text in links:
                target_url = urllib.parse.urljoin(href, html_lib.unescape(str(raw_url or "")).strip())
                if not target_url.startswith(("http://", "https://")) or target_url in seen_links:
                    continue
                seen_links.add(target_url)
                lower_url = target_url.lower()
                clean_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(raw_text or ""))).strip()

                quality_str = ""
                for q in ["2160p", "4k", "1080p", "720p", "480p"]:
                    if q in clean_text.lower() or q in lower_url:
                        quality_str = q.upper()
                        break
                quality_num = int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720

                # Some pages now expose a direct media URL alongside server
                # buttons. Keep those playable links without another extractor.
                if any(lower_url.endswith(ext) or f"{ext}?" in lower_url for ext in (".m3u8", ".mp4", ".mpd", ".mkv")):
                    if target_url not in seen_sources:
                        sources.append({
                            "name": f"HDhub4u {quality_str or clean_text or 'Direct'}",
                            "url": target_url,
                            "quality": quality_num,
                            "headers": {"User-Agent": "Mozilla/5.0", "Referer": href},
                            "is_hls": ".m3u8" in lower_url,
                        })
                        seen_sources.add(target_url)
                    continue

                if (
                    any(h in lower_url for h in ("greenmotors.", "greenmountmotors.", "hblinks.", "hubdrive.", "hubcloud.", "hubcdn.", "nexdrive.", "fastdl.", "vcloud.", "playdrive.", "gdflix."))
                    or ("?id=" in lower_url and "hdhub" not in lower_url)
                ):
                    tasks.append((target_url, raw_text, clean_text, quality_str, quality_num))

            def _extract_worker(task):
                t_url, r_text, c_text, q_str, q_num = task
                l_url = t_url.lower()
                extracted = []
                try:
                    if any(h in l_url for h in ("greenmotors.", "greenmountmotors.")) or ("?id=" in l_url and "hdhub" not in l_url):
                        dest = _resolve_hdhub4u_short_link(t_url)
                        if dest and "hblinks." in dest.lower():
                            extracted = _extract_hblinks_sources(dest, referer=t_url)
                        elif dest and any(h in dest.lower() for h in ("hubcloud.", "hubdrive.", "nexdrive.")):
                            extracted = _extract_hubcloud_sources(dest, referer=t_url)
                        elif dest and any(dest.lower().endswith(ext) or f"{ext}?" in dest.lower() for ext in (".m3u8", ".mp4", ".mpd", ".mkv")):
                            extracted = [{"name": "Direct Stream", "url": dest, "headers": {"Referer": t_url}}]
                    elif "hblinks." in l_url:
                        extracted = _extract_hblinks_sources(t_url, referer=href)
                    elif "hubcdn." in l_url:
                        direct_video = _extract_hubcdn_direct(t_url, referer=href)
                        if direct_video:
                            extracted.append({"name": "HubCDN Direct", "url": direct_video, "headers": {"Referer": t_url}})
                    elif any(host in l_url for host in ("hubdrive.", "hubcloud.", "nexdrive.", "fastdl.", "vcloud.")):
                        extracted = _extract_hubcloud_sources(t_url, referer=href)
                    elif any(host in l_url for host in ("playdrive.", "gdflix.")):
                        try:
                            resolved_url = _resolve_short_link(t_url, timeout=8.0)
                        except Exception:
                            resolved_url = t_url
                        if resolved_url and resolved_url != t_url:
                            resolved_lower = resolved_url.lower()
                            if "hubcdn." in resolved_lower:
                                direct_video = _extract_hubcdn_direct(resolved_url, referer=t_url)
                                if direct_video:
                                    extracted = [{"name": "HubCDN Direct", "url": direct_video, "headers": {"Referer": resolved_url}}]
                            elif any(host in resolved_lower for host in ("hubdrive.", "hubcloud.", "nexdrive.", "fastdl.", "vcloud.")):
                                extracted = _extract_hubcloud_sources(resolved_url, referer=t_url)
                            elif any(resolved_lower.endswith(ext) or f"{ext}?" in resolved_lower for ext in (".m3u8", ".mp4", ".mpd", ".mkv")):
                                extracted = [{"name": "PlayDrive Direct", "url": resolved_url, "headers": {"Referer": t_url}}]
                except Exception as e:
                    logger.debug(f"HDhub4u link extraction error for {t_url}: {e}")
                return q_str, q_num, extracted

            if tasks:
                with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
                    results = list(ex.map(_extract_worker, tasks))

                for q_str, q_num, extracted in results:
                    for entry in extracted or []:
                        source_url = str(entry.get("url") or "").strip()
                        if not source_url or source_url in seen_sources:
                            continue
                        sources.append({
                            "name": f"HDhub4u {q_str} ({entry.get('name') or 'Direct'})".strip(),
                            "url": source_url,
                            "quality": q_num,
                            "headers": entry.get("headers") or {"User-Agent": "Mozilla/5.0", "Referer": href},
                            "is_hls": ".m3u8" in source_url.lower(),
                        })
                        seen_sources.add(source_url)

            if sources:
                logger.debug(f"HDhub4u load_links resolved {len(sources)} playable source(s) from {len(seen_links)} page links")
                return sources
            logger.debug(f"HDhub4u load_links found no supported servers on {href}")
        except Exception as e:
            logger.debug(f"HDhub4u load_links failed: {e}")

        return []


class MoviesDriveCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#MoviesDrive"
    name = "MoviesDrive"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://new1.moviesdrive.cyou"

    def _get_base_url(self):
        return get_dynamic_domain("moviesdrive", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"MoviesDrive catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        card_pattern = r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<meta[^>]*>\s*)*<div class="poster-card"[^>]*>(.*?)</div>\s*</div>\s*</a>'
        matches = re.findall(card_pattern, page_html, re.DOTALL | re.IGNORECASE)
        for href, card_html in matches:
            if href in seen:
                continue
            seen.add(href)
            poster = None
            img_m = re.search(r'<img\b[^>]*>', card_html, re.I)
            img_tag = img_m.group(0) if img_m else ""
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            title = ""
            p_title_m = re.search(r'<p\b[^>]*class=["\']poster-title["\'][^>]*>(.*?)</p>', card_html, re.I | re.DOTALL)
            alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
            if p_title_m:
                title = html_lib.unescape(re.sub(r'<[^>]+>', ' ', p_title_m.group(1))).strip()
            elif alt_m:
                title = html_lib.unescape(alt_m.group(1)).strip()
            title = re.sub(r'\s+', ' ', title).strip()
            if not title:
                continue
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"moviesdrive:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        items = []

        # 1. API search via search.php
        try:
            api_url = f"{base}/search.php?q={urllib.parse.quote(clean_q)}&page=1"
            json_text = _http_get_text(api_url, timeout=6.0)
            data = json.loads(json_text)
            for hit in data.get("hits", []):
                doc = hit.get("document", {})
                p_title = doc.get("title") or doc.get("post_title") or ""
                slug = doc.get("slug") or doc.get("permalink") or ""
                if not p_title or not slug:
                    continue
                full_href = slug if slug.startswith("http") else f"{base}/{slug.lstrip('/')}"
                y_match = re.search(r"\((\d{4})\)", p_title)
                year = int(y_match.group(1)) if y_match else None
                items.append({
                    "provider_id": self.id,
                    "data": f"moviesdrive:{full_href}",
                    "name": p_title,
                    "type": CloudStreamTvType.Movie if "season" not in p_title.lower() else CloudStreamTvType.TvSeries,
                    "poster_url": doc.get("post_thumbnail"),
                    "background_url": doc.get("post_thumbnail"),
                    "description": p_title,
                    "year": year,
                })
        except Exception as e:
            logger.debug(f"MoviesDrive search.php failed: {e}")

        # 2. Fallback to HTML search if search.php returned nothing
        if not items:
            encoded = urllib.parse.quote(clean_q)
            url = f"{base}/?s={encoded}"
            try:
                html = _http_get_text(url, timeout=6.0)
                matches = re.findall(
                    r"<article[^>]*>.*?<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>",
                    html,
                    re.DOTALL
                )
                if not matches:
                    matches = re.findall(
                        r"<h[23][^>]*>.*?<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>",
                        html,
                        re.DOTALL
                    )
                for href, raw_title in matches:
                    clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                    if not clean_title or "moviesdrive" in clean_title.lower() or "${title}" in clean_title:
                        continue
                    y_match = re.search(r"\((\d{4})\)", clean_title)
                    year = int(y_match.group(1)) if y_match else None
                    full_href = href if href.startswith("http") else f"{base}/{href.lstrip('/')}"
                    items.append({
                        "provider_id": self.id,
                        "data": f"moviesdrive:{full_href}",
                        "name": clean_title,
                        "type": CloudStreamTvType.Movie,
                        "poster_url": None,
                        "background_url": None,
                        "description": clean_title,
                        "year": year,
                    })
            except Exception as e:
                logger.debug(f"MoviesDrive search failed: {e}")

        return items

    def load(self, data_str):
        href = data_str.split("moviesdrive:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "MoviesDrive Movie"
            img_match = re.search(r"<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html, re.I)
            if not img_match:
                img_match = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)[\"']", html, re.I)
            poster = html_lib.unescape(img_match.group(1)).strip() if img_match else None
        except Exception:
            title = "MoviesDrive Movie"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*mdrive\.[^"\']*/archive/\d+/[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href
                    })
                    with urllib.request.urlopen(m_req, timeout=5.0) as mr:
                        m_html = mr.read().decode("utf-8", errors="ignore")
                    archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"MoviesDrive archive episode parse error: {e}")

            if archive_eps:
                ep_numbers = sorted(list(archive_eps.keys()))
            else:
                ep_match = re.search(r"(?:Ep|Episode)\.?\s*(\d+)", title, re.I)
                max_ep = int(ep_match.group(1)) if ep_match else 1
                ep_numbers = list(range(1, max(1, max_ep) + 1))

            for ep_num in ep_numbers:
                episodes.append({
                    "name": f"Episode {ep_num}",
                    "season": season_num,
                    "episode": ep_num,
                    "data": f"moviesdrive:{href}#s={season_num}&e={ep_num}",
                    "poster_url": poster,
                    "description": f"Season {season_num} Episode {ep_num}",
                })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["MoviesDrive"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("moviesdrive:")[-1].strip()
        target_season = int(season) if season is not None else None
        target_episode = int(episode) if episode is not None else None
        if "#" in href:
            base_href, fragment = href.split("#", 1)
            href = base_href.strip()
            s_m = re.search(r"\bs=(\d+)", fragment)
            e_m = re.search(r"\be=(\d+)", fragment)
            if s_m and target_season is None: target_season = int(s_m.group(1))
            if e_m and target_episode is None: target_episode = int(e_m.group(1))

        sources = []
        try:
            html = _http_get_text(href, timeout=8.0)
            links = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
            for target_url, text in links:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                quality_str = ""
                for q in ["2160p", "4k", "1080p", "720p", "480p"]:
                    if q in clean_text.lower() or q in target_url.lower():
                        quality_str = q.upper()
                        break

                # 1. mdrive.lol archive links -> HubCloud
                if "mdrive." in target_url and "/archive/" in target_url:
                    try:
                        m_req = urllib.request.Request(target_url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Referer": href
                        })
                        with urllib.request.urlopen(m_req, timeout=7.0) as mr:
                            m_html = mr.read().decode("utf-8", errors="ignore")

                        if target_episode is not None:
                            archive_eps = parse_archive_episodes(m_html)
                            ep_entries = archive_eps.get(target_episode, [])
                            hc_links = [h for h, _ in ep_entries if any(k in h for k in ["hubcloud.", "hubdrive.", "nexdrive.", "fastdl.", "vcloud."])]
                        else:
                            hc_links = re.findall(r'<a[^>]+href=[\"\'](https?://hubcloud\.[^\"\']+)[\"\']', m_html)

                        for hc_link in hc_links:
                            hc_res = _extract_hubcloud_sources(hc_link, referer=target_url)
                            for hs in hc_res:
                                ep_prefix = f" S{target_season}E{target_episode}" if target_season and target_episode else ""
                                sources.append({
                                    "name": f"MoviesDrive{ep_prefix} {quality_str} ({hs.get('name')})".strip(),
                                    "url": hs.get("url"),
                                    "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                                    "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                                    "is_hls": ".m3u8" in hs.get("url", ""),
                                })
                    except Exception as e:
                        logger.debug(f"MoviesDrive mdrive extraction failed: {e}")

                # 2. HubCloud, NexDrive, FastDL, VCloud links
                elif any(k in target_url for k in ["hubcloud.", "nexdrive.", "fastdl.", "vcloud.", "hubdrive."]):
                    hc_res = _extract_hubcloud_sources(target_url, referer=href)
                    for hs in hc_res:
                        sources.append({
                            "name": f"MoviesDrive {quality_str} ({hs.get('name')})".strip(),
                            "url": hs.get("url"),
                            "quality": int(re.sub(r"\D", "", quality_str)) if re.sub(r"\D", "", quality_str).isdigit() else 720,
                            "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                            "is_hls": ".m3u8" in hs.get("url", ""),
                        })

            if sources:
                return sources
        except Exception as e:
            logger.debug(f"MoviesDrive load_links failed: {e}")
        return []


class MoviesmodCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#Moviesmod"
    name = "Moviesmod"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://moviesmod.tw"

    def _get_base_url(self):
        return get_dynamic_domain("moviesmod", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"Moviesmod catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        articles = re.findall(r"<article[^>]*>.*?</article>", page_html, re.DOTALL | re.IGNORECASE)
        for art in articles:
            link_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', art, re.I)
            if not link_m:
                continue
            href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_m.group(1)).strip())
            if href in seen:
                continue

            img_m = re.search(r'<img\b[^>]*>', art, re.I)
            img_tag = img_m.group(0) if img_m else ""
            poster = None
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            t_candidates = []
            h_m = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', art, re.I | re.DOTALL)
            if h_m:
                t_candidates.append(h_m.group(1))
            t_m = re.search(r'<a\b[^>]*title=["\']([^"\']+)["\']', art, re.I)
            if t_m:
                t_candidates.append(t_m.group(1))
            if img_m:
                alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
                if alt_m:
                    t_candidates.append(alt_m.group(1))

            title = ""
            for cand in t_candidates:
                c = html_lib.unescape(re.sub(r'<[^>]+>', ' ', cand))
                c = re.sub(r'^Download\s+', '', c, flags=re.I).strip(' .-|\t\r\n')
                c = re.sub(r'\s+', ' ', c)
                if c:
                    title = c
                    break
            if not title:
                continue
            seen.add(href)
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"moviesmod:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        encoded = urllib.parse.quote(clean_q)
        url = f"{base}/?s={encoded}"
        try:
            html = _http_get_text(url, timeout=8.0)
            articles = re.findall(r"<article[^>]*>.*?</article>", html, re.DOTALL | re.IGNORECASE)
            items = []
            seen = set()
            for art in articles:
                link_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', art, re.I)
                if not link_m:
                    continue
                href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_m.group(1)).strip())
                if href in seen:
                    continue
                seen.add(href)

                img_m = re.search(r'<img\b[^>]*>', art, re.I)
                img_tag = img_m.group(0) if img_m else ""
                poster = None
                for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                    pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                    if pm and not pm.group(1).startswith("data:"):
                        poster = html_lib.unescape(pm.group(1)).strip()
                        break

                t_m = re.search(r'<a\b[^>]*title=["\']([^"\']+)["\']', art, re.I)
                h_m = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', art, re.I | re.DOTALL)
                raw_title = t_m.group(1) if t_m else (h_m.group(1) if h_m else "")
                clean_title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
                clean_title = re.sub(r"^Download\s+", "", clean_title, flags=re.I).strip()
                if not clean_title:
                    continue
                y_match = re.search(r"\((\d{4})\)", clean_title)
                year = int(y_match.group(1)) if y_match else None
                is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", clean_title, re.I))

                items.append({
                    "provider_id": self.id,
                    "data": f"moviesmod:{href}",
                    "name": clean_title,
                    "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                    "poster_url": poster,
                    "background_url": poster,
                    "description": clean_title,
                    "year": year,
                })
            return items
        except Exception as e:
            logger.debug(f"Moviesmod search failed: {e}")
            return []

    def load(self, data_str):
        href = data_str.split("moviesmod:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "Moviesmod Movie"
            img_match = re.search(r"<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html, re.I)
            if not img_match:
                img_match = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)[\"']", html, re.I)
            poster = html_lib.unescape(img_match.group(1)).strip() if img_match else None
        except Exception:
            title = "Moviesmod Movie"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:episodes\.modpro|archives/\d+)[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href
                    })
                    with urllib.request.urlopen(m_req, timeout=5.0) as mr:
                        m_html = mr.read().decode("utf-8", errors="ignore")
                    archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"Moviesmod archive episode parse error: {e}")

            if archive_eps:
                ep_numbers = sorted(list(archive_eps.keys()))
            else:
                ep_match = re.search(r"(?:Ep|Episode)\.?\s*(\d+)", title, re.I)
                max_ep = int(ep_match.group(1)) if ep_match else 1
                ep_numbers = list(range(1, max(1, max_ep) + 1))

            for ep_num in ep_numbers:
                episodes.append({
                    "name": f"Episode {ep_num}",
                    "season": season_num,
                    "episode": ep_num,
                    "data": f"moviesmod:{href}#s={season_num}&e={ep_num}",
                    "poster_url": poster,
                    "description": f"Season {season_num} Episode {ep_num}",
                })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["Moviesmod"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("moviesmod:")[-1].strip()
        target_season = int(season) if season is not None else None
        target_episode = int(episode) if episode is not None else None
        if "#" in href:
            base_href, fragment = href.split("#", 1)
            href = base_href.strip()
            s_m = re.search(r"\bs=(\d+)", fragment)
            e_m = re.search(r"\be=(\d+)", fragment)
            if s_m and target_season is None: target_season = int(s_m.group(1))
            if e_m and target_episode is None: target_episode = int(e_m.group(1))

        sources = []
        try:
            html = _http_get_text(href, timeout=8.0)
            links = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
            for target_url, text in links:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if "episodes.modpro" in target_url or "/archives/" in target_url:
                    if target_episode is not None:
                        try:
                            m_req = urllib.request.Request(target_url, headers={
                                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": href
                            })
                            with urllib.request.urlopen(m_req, timeout=6.0) as mr:
                                m_html = mr.read().decode("utf-8", errors="ignore")
                            arc_eps = parse_archive_episodes(m_html)
                            for ep_target, _ in arc_eps.get(target_episode, []):
                                if "hubcloud." in ep_target:
                                    hc_res = _extract_hubcloud_sources(ep_target, referer=target_url)
                                    for hs in hc_res:
                                        ep_pfx = f" S{target_season}E{target_episode}" if target_season and target_episode else ""
                                        sources.append({
                                            "name": f"Moviesmod{ep_pfx} ({hs.get('name')})",
                                            "url": hs.get("url"),
                                            "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                                            "is_hls": ".m3u8" in hs.get("url", ""),
                                        })
                        except Exception as e:
                            logger.debug(f"Moviesmod archive ep extraction error: {e}")
                elif "hubcloud." in target_url:
                    hc_res = _extract_hubcloud_sources(target_url, referer=href)
                    for hs in hc_res:
                        sources.append({
                            "name": f"Moviesmod ({hs.get('name')})",
                            "url": hs.get("url"),
                            "headers": hs.get("headers") or {"User-Agent": "Mozilla/5.0"},
                            "is_hls": ".m3u8" in hs.get("url", ""),
                        })
            if sources:
                return sources
        except Exception as e:
            logger.debug(f"Moviesmod load_links failed: {e}")
        return []


class VegaMoviesCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#VegaMovies"
    name = "VegaMovies"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://vegamovies.lease"

    def _get_base_url(self):
        return get_dynamic_domain("vegamovies", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"VegaMovies catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        card_pattern = r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<meta[^>]*>\s*)*<div class="poster-card"[^>]*>(.*?)</div>\s*</div>\s*</a>'
        matches = re.findall(card_pattern, page_html, re.DOTALL | re.IGNORECASE)
        for href, card_html in matches:
            if href in seen:
                continue
            seen.add(href)
            poster = None
            img_m = re.search(r'<img\b[^>]*>', card_html, re.I)
            img_tag = img_m.group(0) if img_m else ""
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            title = ""
            p_title_m = re.search(r'<p\b[^>]*class=["\']poster-title["\'][^>]*>(.*?)</p>', card_html, re.I | re.DOTALL)
            alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
            if p_title_m:
                title = html_lib.unescape(re.sub(r'<[^>]+>', ' ', p_title_m.group(1))).strip()
            elif alt_m:
                title = html_lib.unescape(alt_m.group(1)).strip()
            title = re.sub(r'\s+', ' ', title).strip()
            if not title:
                continue
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"vegamovies:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        items = []

        # 1. API search via search.php
        try:
            api_url = f"{base}/search.php?q={urllib.parse.quote(clean_q)}&page=1"
            json_text = _http_get_text(api_url, timeout=6.0)
            data = json.loads(json_text)
            for hit in data.get("hits", []):
                doc = hit.get("document", {})
                p_title = doc.get("title") or doc.get("post_title") or ""
                slug = doc.get("slug") or doc.get("permalink") or ""
                if not p_title or not slug:
                    continue
                full_href = slug if slug.startswith("http") else f"{base}/{slug.lstrip('/')}"
                y_match = re.search(r"\((\d{4})\)", p_title)
                year = int(y_match.group(1)) if y_match else None
                items.append({
                    "provider_id": self.id,
                    "data": f"vegamovies:{full_href}",
                    "name": p_title,
                    "type": CloudStreamTvType.Movie if "season" not in p_title.lower() else CloudStreamTvType.TvSeries,
                    "poster_url": doc.get("post_thumbnail"),
                    "background_url": doc.get("post_thumbnail"),
                    "description": p_title,
                    "year": year,
                })
        except Exception as e:
            logger.debug(f"VegaMovies search.php failed: {e}")

        # 2. Fallback to HTML search
        if not items:
            encoded = urllib.parse.quote(clean_q)
            url = f"{base}/?s={encoded}"
            try:
                html = _http_get_text(url, timeout=6.0)
                matches = re.findall(
                    r"<article[^>]*>.*?<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>",
                    html,
                    re.DOTALL
                )
                for href, raw_title in matches:
                    clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                    if "${title}" in clean_title or not clean_title:
                        continue
                    y_match = re.search(r"\((\d{4})\)", clean_title)
                    year = int(y_match.group(1)) if y_match else None
                    items.append({
                        "provider_id": self.id,
                        "data": f"vegamovies:{href}",
                        "name": clean_title,
                        "type": CloudStreamTvType.Movie,
                        "poster_url": None,
                        "background_url": None,
                        "description": clean_title,
                        "year": year,
                    })
            except Exception as e:
                logger.debug(f"VegaMovies search failed: {e}")

        return items

    def load(self, data_str):
        href = data_str.split("vegamovies:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "VegaMovies Movie"
            img_match = re.search(r"<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html, re.I)
            if not img_match:
                img_match = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)[\"']", html, re.I)
            poster = html_lib.unescape(img_match.group(1)).strip() if img_match else None
        except Exception:
            title = "VegaMovies Movie"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:nexdrive|fastdl|vcloud)[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href
                    })
                    with urllib.request.urlopen(m_req, timeout=5.0) as mr:
                        m_html = mr.read().decode("utf-8", errors="ignore")
                    archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"VegaMovies archive episode parse error: {e}")

            if archive_eps:
                ep_numbers = sorted(list(archive_eps.keys()))
            else:
                ep_match = re.search(r"(?:Ep|Episode)\.?\s*(\d+)", title, re.I)
                max_ep = int(ep_match.group(1)) if ep_match else 1
                ep_numbers = list(range(1, max(1, max_ep) + 1))

            for ep_num in ep_numbers:
                episodes.append({
                    "name": f"Episode {ep_num}",
                    "season": season_num,
                    "episode": ep_num,
                    "data": f"vegamovies:{href}#s={season_num}&e={ep_num}",
                    "poster_url": poster,
                    "description": f"Season {season_num} Episode {ep_num}",
                })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["VegaMovies"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("vegamovies:")[-1].strip()
        target_season = int(season) if season is not None else None
        target_episode = int(episode) if episode is not None else None
        if "#" in href:
            base_href, fragment = href.split("#", 1)
            href = base_href.strip()
            s_m = re.search(r"\bs=(\d+)", fragment)
            e_m = re.search(r"\be=(\d+)", fragment)
            if s_m and target_season is None: target_season = int(s_m.group(1))
            if e_m and target_episode is None: target_episode = int(e_m.group(1))

        sources = []
        try:
            html = _http_get_text(href, timeout=8.0)
            links = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
            for target_url, text in links:
                if any(k in target_url for k in ["nexdrive.", "fastdl.", "vcloud.", "hubcloud.", "hubdrive."]):
                    if target_episode is not None and "nexdrive." in target_url:
                        try:
                            m_req = urllib.request.Request(target_url, headers={
                                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": href
                            })
                            with urllib.request.urlopen(m_req, timeout=6.0) as mr:
                                m_html = mr.read().decode("utf-8", errors="ignore")
                            arc_eps = parse_archive_episodes(m_html)
                            for ep_target, _ in arc_eps.get(target_episode, []):
                                hc_res = _extract_hubcloud_sources(ep_target, referer=target_url)
                                for hs in (hc_res or []):
                                    ep_pfx = f" S{target_season}E{target_episode}" if target_season and target_episode else ""
                                    hs["name"] = f"VegaMovies{ep_pfx} ({hs.get('name', 'Direct')})"
                                    sources.append(hs)
                        except Exception as e:
                            logger.debug(f"VegaMovies nexdrive ep extraction error: {e}")
                    else:
                        hc_res = _extract_hubcloud_sources(target_url, referer=href)
                        if hc_res:
                            sources.extend(hc_res)
            if sources:
                return sources
        except Exception as e:
            logger.debug(f"VegaMovies load_links failed: {e}")
        return []


DEFAULT_TRACKERS = [
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://torrent.gresille.org:80/announce",
    "udp://p4p.arenabg.com:1337",
    "udp://tracker.leechers-paradise.org:6969",
]


class YTSCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#YTS"
    name = "YTS"
    supported_types = ["movie", "torrent", "all"]
    catalog_sections = (
        {"id": "popular-movies", "name": "Popular Movies", "section": "Popular Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "top-rated", "name": "Top Rated", "section": "Top Rated", "type": "movie", "supports_pagination": True},
    )

    _BASE_URLS = [
        "https://movies-api.accel.li/api/v2",
        "https://yts.mx/api/v2",
        "https://yts.bz/api/v2",
        "https://yts.gg/api/v2",
    ]

    _TRACKERS = DEFAULT_TRACKERS

    def _request_json(self, endpoint, params=None):
        query_str = urllib.parse.urlencode(params or {}) if params else ""
        for base in self._BASE_URLS:
            url = f"{base}/{endpoint.lstrip('/')}"
            if query_str:
                url = f"{url}?{query_str}"
            try:
                text = _http_get_text(url, timeout=7.0)
                if not text:
                    continue
                data = json.loads(text)
                if isinstance(data, dict) and data.get("status") == "ok":
                    return data
            except Exception as e:
                logger.debug(f"YTS API {url} failed: {e}")
                continue
        return None

    def get_streams_for_media(self, title, media_type, year=None, season=None, episode=None, imdb_id=None):
        norm_type = media_type.lower() if media_type else "movie"
        if norm_type not in ("movie", "all"):
            return []
        items = self.search(title, imdb_id=imdb_id)
        if not items:
            return []
        best_data = None
        target_clean = re.sub(r"[^\w\s]", "", (title or "").lower()).strip()
        for it in items:
            it_name = re.sub(r"[^\w\s]", "", (it.get("name") or "").lower()).strip()
            if target_clean and (target_clean in it_name or it_name in target_clean):
                best_data = it.get("data")
                break
        if not best_data and items:
            best_data = items[0].get("data")
        if best_data:
            return self.load_links(best_data)
        return []

    def search(self, query, imdb_id=None):
        if not query and not imdb_id:
            return []

        target_term = ""
        if imdb_id and str(imdb_id).startswith("tt"):
            target_term = str(imdb_id).strip()
        elif query and str(query).startswith("tt"):
            target_term = str(query).strip()
        else:
            target_term = str(query).strip()

        data = self._request_json("list_movies.json", {"query_term": target_term, "limit": 20})
        # If searching by IMDb ID returned no results and query is different, fallback to title query
        if (not data or not data.get("data", {}).get("movies")) and query and target_term != query.strip():
            data = self._request_json("list_movies.json", {"query_term": query.strip(), "limit": 20})

        if not data or not isinstance(data.get("data"), dict):
            return []

        movies = data["data"].get("movies") or []
        results = []
        for m in movies:
            if not isinstance(m, dict):
                continue
            title = m.get("title_long") or m.get("title") or "Unknown"
            year = m.get("year")
            rating = m.get("rating")
            rating_pct = int(float(rating) * 10) if rating else None
            cover = m.get("large_cover_image") or m.get("medium_cover_image")
            if cover and "://yts.gg/assets/images/" in cover:
                cover = cover.replace("://yts.gg/assets/images/", "://img.yts.gg/assets/images/")
            movie_id = m.get("id")

            payload = {
                "id": movie_id,
                "imdb_code": m.get("imdb_code"),
                "title": title,
                "year": year,
                "cover": cover,
                "summary": m.get("summary") or m.get("description_full"),
                "torrents": m.get("torrents") or [],
            }
            results.append({
                "name": title,
                "url": m.get("url") or f"https://yts.mx/movies/{m.get('slug', movie_id)}",
                "data": json.dumps(payload),
                "imdb_code": m.get("imdb_code"),
                "poster_url": cover,
                "year": year,
                "rating_percent": rating_pct,
                "type": CloudStreamTvType.Movie,
            })
        return results

    def load(self, data_str):
        payload = None
        try:
            if isinstance(data_str, str) and data_str.strip().startswith("{"):
                payload = json.loads(data_str)
        except Exception:
            payload = None

        if not payload:
            movie_id = None
            if str(data_str).isdigit():
                movie_id = str(data_str)
            elif "yts:" in str(data_str):
                movie_id = str(data_str).split("yts:")[1].split(":")[0]
            if movie_id:
                resp = self._request_json("movie_details.json", {"movie_id": movie_id, "with_images": "true"})
                if resp and isinstance(resp.get("data"), dict):
                    m = resp["data"].get("movie") or {}
                    cover_img = m.get("large_cover_image") or m.get("medium_cover_image")
                    if cover_img and "://yts.gg/assets/images/" in cover_img:
                        cover_img = cover_img.replace("://yts.gg/assets/images/", "://img.yts.gg/assets/images/")
                    payload = {
                        "id": m.get("id"),
                        "imdb_code": m.get("imdb_code"),
                        "title": m.get("title_long") or m.get("title") or "Unknown",
                        "year": m.get("year"),
                        "cover": cover_img,
                        "summary": m.get("description_full") or m.get("summary"),
                        "torrents": m.get("torrents") or [],
                    }

        if not payload:
            return GenericCloudStreamProvider(self.id, self.name).load(data_str)

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": payload.get("title", self.name),
            "type": CloudStreamTvType.Movie,
            "poster_url": payload.get("cover"),
            "background_url": None,
            "description": payload.get("summary") or f"YTS movie: {payload.get('title')}",
            "year": payload.get("year"),
            "rating_percent": None,
            "tags": ["Movie", "Torrent"],
            "episodes": [],
        }

    def load_links(self, data_str, season=None, episode=None):
        payload = None
        try:
            if isinstance(data_str, str) and data_str.strip().startswith("{"):
                payload = json.loads(data_str)
        except Exception:
            payload = None

        torrents = []
        movie_title = "Movie"
        if payload and isinstance(payload, dict):
            torrents = payload.get("torrents") or []
            movie_title = payload.get("title") or "Movie"
        else:
            movie_id = None
            if str(data_str).isdigit():
                movie_id = str(data_str)
            elif "yts:" in str(data_str):
                movie_id = str(data_str).split("yts:")[1].split(":")[0]
            if movie_id:
                resp = self._request_json("movie_details.json", {"movie_id": movie_id})
                if resp and isinstance(resp.get("data"), dict):
                    m = resp["data"].get("movie") or {}
                    torrents = m.get("torrents") or []
                    movie_title = m.get("title_long") or m.get("title") or movie_title

        if not torrents:
            return []

        tracker_query = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in self._TRACKERS)
        sources = []
        for t in torrents:
            if not isinstance(t, dict):
                continue
            h = str(t.get("hash") or "").strip().lower()
            if not h:
                continue
            quality_raw = str(t.get("quality") or "1080p").strip()
            codec = str(t.get("video_codec") or "x264").strip()
            t_type = str(t.get("type") or "bluray").strip()

            encoded_dn = urllib.parse.quote(f"{movie_title} [{quality_raw}] [YTS]")
            magnet = f"magnet:?xt=urn:btih:{h}&dn={encoded_dn}{tracker_query}"

            q_digits = re.sub(r"\D", "", quality_raw)
            quality_num = int(q_digits) if q_digits else 1080

            details_parts = [p.upper() for p in [codec, t_type] if p]
            name_parts = ["YTS", quality_raw] + details_parts
            stream_name = " ".join(name_parts)

            seeds = 0
            try:
                seeds = int(t.get("seeds") or 0)
            except (ValueError, TypeError):
                seeds = 0

            peers = 0
            try:
                peers = int(t.get("peers") or 0)
            except (ValueError, TypeError):
                peers = 0

            sources.append({
                "name": stream_name,
                "url": magnet,
                "magnet": magnet,
                "quality": quality_num,
                "is_torrent": True,
                "is_http": False,
                "info_hash": h,
                "infoHash": h,
                "hash": h,
                "seeds": seeds,
                "seeders": seeds,
                "peers": peers,
                "size": t.get("size"),
                "size_bytes": t.get("size_bytes"),
            })
        return sources

    def get_main_page(self, page=1):
        sections = []
        for title, sort in [("Popular Movies", "download_count"), ("Latest Movies", "date_added"), ("Top Rated", "rating")]:
            try:
                resp = self._request_json("list_movies.json", {"sort_by": sort, "limit": 20, "page": page})
                if resp and isinstance(resp.get("data"), dict):
                    movies = resp["data"].get("movies") or []
                    items = []
                    for m in movies:
                        t = m.get("title_long") or m.get("title")
                        cover = m.get("large_cover_image") or m.get("medium_cover_image")
                        if cover and "://yts.gg/assets/images/" in cover:
                            cover = cover.replace("://yts.gg/assets/images/", "://img.yts.gg/assets/images/")
                        payload = {
                            "id": m.get("id"),
                            "imdb_code": m.get("imdb_code"),
                            "title": t,
                            "year": m.get("year"),
                            "cover": cover,
                            "summary": m.get("summary"),
                            "torrents": m.get("torrents") or [],
                        }
                        items.append({
                            "name": t,
                            "url": m.get("url"),
                            "data": json.dumps(payload),
                            "imdb_code": m.get("imdb_code"),
                            "poster_url": cover,
                            "year": m.get("year"),
                            "rating_percent": int(float(m.get("rating") or 0) * 10),
                            "type": CloudStreamTvType.Movie,
                        })
                    if items:
                        sections.append((title, items))
            except Exception as e:
                logger.debug(f"YTS main page '{title}' failed: {e}")
        return sections


class TorrentioCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/DieGon7771/ItaliaInStreaming/builds/repo.json#Torrentio"
    name = "Torrentio"
    supported_types = ["movie", "series", "anime", "torrent", "all"]

    def _resolve_imdb_id(self, query, year=None):
        if str(query).startswith("tt") and query[2:].split(":")[0].isdigit():
            return query
        try:
            encoded = urllib.parse.quote(query.strip())
            url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={encoded}.json"
            txt = _http_get_text(url, timeout=5.0)
            data = json.loads(txt)
            for m in data.get("metas", []):
                m_id = m.get("imdb_id") or m.get("id")
                if m_id and str(m_id).startswith("tt"):
                    return m_id
        except Exception:
            pass
        return None

    def get_streams_for_media(self, title, media_type, year=None, season=None, episode=None, imdb_id=None):
        target_imdb = imdb_id if (imdb_id and str(imdb_id).startswith("tt")) else self._resolve_imdb_id(title, year=year)
        if not target_imdb:
            return []

        norm_type = media_type.lower() if media_type else "movie"
        if norm_type in ("series", "tv", "anime") and season is not None and episode is not None:
            endpoint = f"https://torrentio.strem.fun/stream/series/{target_imdb}:{season}:{episode}.json"
        else:
            endpoint = f"https://torrentio.strem.fun/stream/movie/{target_imdb}.json"

        try:
            txt = _http_get_text(endpoint, timeout=8.0)
            data = json.loads(txt)
        except Exception as e:
            logger.debug(f"Torrentio request to {endpoint} failed: {e}")
            return []

        sources = []
        for s in data.get("streams", []):
            info_hash = s.get("infoHash")
            if not info_hash:
                continue
            s_name = s.get("name") or "Torrentio"
            s_title = s.get("title") or ""
            file_idx = s.get("fileIdx", 0)

            seeds = None
            size = None
            seeds_m = re.search(r"👤\s*(\d+)", s_title)
            if seeds_m:
                try:
                    seeds = int(seeds_m.group(1))
                except ValueError:
                    pass
            size_m = re.search(r"💾\s*([\d.]+\s*[KMGT]B)", s_title)
            if size_m:
                size = size_m.group(1)

            first_line = s_title.splitlines()[0] if s_title else s_name
            tracker_query = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in DEFAULT_TRACKERS)
            magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(first_line)}{tracker_query}"
            q_str = "1080p"
            combined = f"{s_name} {s_title}".lower()
            if "2160p" in combined or "4k" in combined:
                q_str = "2160p"
            elif "1080p" in combined:
                q_str = "1080p"
            elif "720p" in combined:
                q_str = "720p"
            elif "480p" in combined:
                q_str = "480p"

            sources.append({
                "name": f"Torrentio {q_str}",
                "title": s_title,
                "url": magnet,
                "magnet": magnet,
                "info_hash": info_hash,
                "infoHash": info_hash,
                "file_idx": file_idx,
                "fileIdx": file_idx,
                "is_torrent": True,
                "is_http": False,
                "seeds": seeds,
                "seeders": seeds,
                "size": size,
                "quality": q_str,
                "sources": [f"tracker:{t}" for t in DEFAULT_TRACKERS],
                "behavior_hints": s.get("behaviorHints") or {}
            })
        return sources

    def search(self, query, imdb_id=None):
        target_imdb = imdb_id if (imdb_id and str(imdb_id).startswith("tt")) else self._resolve_imdb_id(query)
        if not target_imdb:
            return []
        return [{
            "provider_id": self.id,
            "data": json.dumps({"imdb_id": target_imdb, "title": query}),
            "name": f"{query} (Torrentio)",
            "type": CloudStreamTvType.Movie,
            "poster_url": None,
            "background_url": None,
            "description": f"Torrents for {query} via Torrentio",
            "year": None,
            "rating_percent": None,
            "tags": ["Torrentio", "Torrent"],
            "episodes": [],
        }]

    def load(self, data_str):
        try:
            data = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("{") else {"imdb_id": data_str}
        except Exception:
            data = {"imdb_id": data_str}
        imdb_id = data.get("imdb_id") or data_str
        episodes = []
        for s in range(1, 20):
            for e in range(1, 40):
                episodes.append({
                    "season": s,
                    "episode": e,
                    "data": json.dumps({"imdb_id": imdb_id, "season": s, "episode": e, "type": "series"}),
                    "name": f"Season {s} Episode {e}"
                })
        return {
            "provider_id": self.id,
            "data": data_str,
            "name": data.get("title") or "Torrentio Media",
            "type": CloudStreamTvType.Movie,
            "poster_url": None,
            "background_url": None,
            "description": "Torrentio BitTorrent Streams",
            "year": None,
            "rating_percent": None,
            "tags": ["Torrentio", "Torrent"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        try:
            data = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("{") else {"imdb_id": data_str}
        except Exception:
            data = {"imdb_id": data_str}
        imdb_id = data.get("imdb_id")
        season = data.get("season")
        episode = data.get("episode")
        media_type = data.get("type", "movie")
        return self.get_streams_for_media(
            title="", media_type=media_type, season=season, episode=episode, imdb_id=imdb_id
        )


_STOP_WORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "at", "by", "from"}


def _is_strict_title_match(query, candidate):
    q_clean = re.sub(r"[^\w\s]", "", (query or "").lower()).strip()
    c_clean = re.sub(r"[^\w\s]", "", (candidate or "").lower()).strip()
    if not q_clean or not c_clean:
        return False
    if q_clean == c_clean:
        return True
    q_words = [w for w in q_clean.split() if w not in _STOP_WORDS]
    if not q_words:
        q_words = q_clean.split()
    c_words = [w for w in c_clean.split() if w not in _STOP_WORDS]
    if not c_words:
        c_words = c_clean.split()
    q_set = set(q_words)
    c_set = set(c_words)
    if len(q_set) <= 2:
        return q_set.issubset(c_set)
    return (len(q_set.intersection(c_set)) / len(q_set)) >= 0.8


class CNCVerseCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CNC Verse"
    name = "CNC Verse"
    supported_types = ["movie", "series", "tv", "all"]

    _cached_cookie = None
    _cached_cookie_time = 0
    _cached_api_base = None
    _cached_api_base_time = 0
    _cached_usertoken = None
    _cached_usertoken_time = 0
    _stream_cache = {}

    def _get_api_base(self):
        now = time.time()
        if self._cached_api_base and (now - self._cached_api_base_time) < 86400:
            return self._cached_api_base
        base = "https://tv.imgcdn.kim"
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.Gatu v1.0",
                "Accept": "application/json, text/plain, */*",
            }
            req = urllib.request.Request("https://mobiledetects.com/checknewtv.php", headers=headers)
            with urllib.request.urlopen(req, timeout=4.0) as r:
                res = json.loads(r.read().decode("utf-8"))
            if res.get("token_hash"):
                decoded = base64.b64decode(res["token_hash"]).decode("utf-8").strip()
                if decoded.startswith("http"):
                    base = decoded
        except Exception:
            pass
        self._cached_api_base = base
        self._cached_api_base_time = now
        return self._cached_api_base

    def _get_cookie(self):
        now = time.time()
        if self._cached_cookie and (now - self._cached_cookie_time) < 86400:
            return self._cached_cookie

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://net22.cc",
            "Referer": "https://net22.cc/verify2",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        data = urllib.parse.urlencode({"g-recaptcha-response": str(uuid.uuid4())}).encode("utf-8")
        req = urllib.request.Request("https://net52.cc/verify.php", data=data, headers=headers)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            r = opener.open(req, timeout=6.0)
            cookies = r.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            cookies = e.headers.get_all("Set-Cookie") or []
        except Exception:
            cookies = []

        for c in cookies:
            if "t_hash_t=" in c:
                val = c.split("t_hash_t=")[1].split(";")[0]
                self._cached_cookie = f"t_hash_t={val}"
                self._cached_cookie_time = now
                return self._cached_cookie
        return ""

    def _get_usertoken(self):
        now = time.time()
        if self._cached_usertoken and (now - self._cached_usertoken_time) < 600:
            return self._cached_usertoken
        api_base = self._get_api_base()
        try:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Connection": "Keep-Alive",
                "Expires": "0",
                "Otp": "111111",
                "Pragma": "no-cache",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.Gatu v1.0",
            }
            req = urllib.request.Request(f"{api_base}/newtv/otp.php", headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as r:
                res = json.loads(r.read().decode("utf-8"))
            token = res.get("usertoken")
            if token:
                self._cached_usertoken = token
                self._cached_usertoken_time = now
                return self._cached_usertoken
        except Exception as e:
            logger.debug(f"CNC Verse OTP token fetch failed: {e}")
        return ""

    def _get_headers(self, ott="nf"):
        cookie = self._get_cookie()
        full_cookie = f"{cookie}; hd=on; ott={ott}" if cookie else f"hd=on; ott={ott}"
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.GatuNewTV v1.0",
            "Referer": "https://net52.cc/home",
            "Cookie": full_cookie,
            "X-Requested-With": "NetmirrorNewTV v1.0",
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Ott": ott,
        }

    def _get_player_headers(self, ott="nf"):
        headers = self._get_headers(ott)
        headers["Referer"] = "https://net52.cc/"
        usertoken = self._get_usertoken()
        if usertoken:
            headers["Usertoken"] = usertoken
        return headers

    def search(self, query):
        if not query or not query.strip():
            return []
        clean_q = query.strip()
        t = int(time.time())
        items = []
        for ott, path in [("pv", "/mobile/pv/search.php"), ("nf", "/mobile/search.php"), ("hs", "/mobile/hs/search.php")]:
            url = f"https://net52.cc{path}?s={urllib.parse.quote(clean_q)}&t={t}"
            try:
                req = urllib.request.Request(url, headers=self._get_headers(ott))
                with urllib.request.urlopen(req, timeout=5.0) as r:
                    res = json.loads(r.read().decode("utf-8"))
                if res.get("status") == "n" or res.get("head") == "Top Searches":
                    continue
                for item in res.get("searchResult", []):
                    item_id = item.get("id")
                    item_title = item.get("t")
                    if item_id and item_title:
                        items.append({
                            "provider_id": self.id,
                            "data": json.dumps({"id": str(item_id), "title": item_title, "ott": ott, "year": item.get("y")}),
                            "name": f"{item_title} ({item.get('y') or 'CNC Verse'})",
                            "type": CloudStreamTvType.Movie if ott == "pv" else CloudStreamTvType.TvSeries,
                            "poster_url": None,
                            "background_url": None,
                            "description": f"{item_title} on CNC Verse",
                            "year": int(item.get("y")) if str(item.get("y", "")).isdigit() else None,
                            "rating_percent": None,
                            "tags": ["CNC Verse", ott.upper()],
                            "episodes": [],
                        })
                if items:
                    break
            except Exception as e:
                logger.debug(f"CNC Verse search failed for {ott}: {e}")
        return items

    def get_streams_for_media(self, title, media_type, year=None, season=None, episode=None, imdb_id=None):
        if not title or not title.strip():
            return []
        clean_title = title.strip()
        norm_type = media_type.lower() if media_type else "movie"

        cache_key = (clean_title.lower(), norm_type, str(year or ""), season, episode)
        now = time.time()
        cached = self._stream_cache.get(cache_key)
        if cached:
            ts, cached_streams = cached
            if (now - ts) < 1800:
                return cached_streams

        t = int(time.time())
        api_base = self._get_api_base()

        ott_candidates = [
            ("nf", "/mobile/search.php", "/mobile/post.php", "/mobile/episodes.php"),
            ("pv", "/mobile/pv/search.php", "/mobile/pv/post.php", "/mobile/pv/episodes.php"),
            ("hs", "/mobile/hs/search.php", "/mobile/hs/post.php", "/mobile/hs/episodes.php")
        ]

        norm_query = re.sub(r"[^\w\s]", "", clean_title.lower())
        query_words = set(norm_query.split())

        for ott, s_path, post_path, ep_path in ott_candidates:
            url = f"https://net52.cc{s_path}?s={urllib.parse.quote(clean_title)}&t={t}"
            try:
                req = urllib.request.Request(url, headers=self._get_headers(ott))
                with urllib.request.urlopen(req, timeout=5.0) as r:
                    res = json.loads(r.read().decode("utf-8"))
                if res.get("status") == "n" or res.get("head") == "Top Searches":
                    continue
                search_results = res.get("searchResult") or []
                if not search_results:
                    continue

                target_id = None
                for cand in search_results:
                    cand_raw = cand.get("t") or ""
                    if cand_raw and _is_strict_title_match(clean_title, cand_raw):
                        target_id = cand.get("id")
                        break

                if not target_id:
                    continue

                if norm_type == "movie":
                    p_url = f"{api_base}/newtv/player.php?id={target_id}"
                    p_req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
                    with urllib.request.urlopen(p_req, timeout=5.0) as pr:
                        p_data = json.loads(pr.read().decode("utf-8"))
                    vlink = p_data.get("video_link")
                    if vlink and ("?in=" not in vlink or "stop abusing" in vlink):
                        # Retry once with refreshed token
                        self._cached_usertoken = None
                        try:
                            p_req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
                            with urllib.request.urlopen(p_req, timeout=5.0) as pr:
                                p_data = json.loads(pr.read().decode("utf-8"))
                            vlink = p_data.get("video_link")
                        except Exception:
                            pass
                    if vlink and vlink.startswith("http") and "?in=" in vlink and "stop abusing" not in vlink:
                        res_streams = [{
                            "name": f"CNC Verse [{ott.upper()} 1080p HLS]",
                            "title": f"{clean_title} · {ott.upper()} Multi-Audio HLS",
                            "url": vlink,
                            "is_hls": True,
                            "is_http": True,
                            "is_torrent": False,
                            "quality": "1080p",
                            "headers": {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.GatuNewTV v1.0",
                                "Referer": "https://net52.cc/"
                            }
                        }]
                        self._stream_cache[cache_key] = (now, res_streams)
                        return res_streams
                else:
                    post_url = f"https://net52.cc{post_path}?id={target_id}&t={t}"
                    post_req = urllib.request.Request(post_url, headers=self._get_headers(ott))
                    with urllib.request.urlopen(post_req, timeout=5.0) as pr:
                        p_data = json.loads(pr.read().decode("utf-8"))

                    target_ep_id = None
                    target_s = season if season is not None else 1
                    target_e = episode if episode is not None else 1

                    seasons = p_data.get("season") or []
                    for s_obj in seasons:
                        if str(s_obj.get("s")) == str(target_s):
                            season_id = s_obj.get("id")
                            ep_url = f"https://net52.cc{ep_path}?s={season_id}&t={t}"
                            ep_req = urllib.request.Request(ep_url, headers=self._get_headers(ott))
                            with urllib.request.urlopen(ep_req, timeout=5.0) as er:
                                ep_data = json.loads(er.read().decode("utf-8"))
                            for ep in ep_data.get("episodes") or []:
                                ep_num = str(ep.get("ep") or "").replace("E", "")
                                if ep_num == str(target_e):
                                    target_ep_id = ep.get("id")
                                    break
                            break

                    if not target_ep_id and p_data.get("episodes"):
                        for ep in p_data.get("episodes"):
                            if ep:
                                ep_num = str(ep.get("ep") or "").replace("E", "")
                                if ep_num == str(target_e):
                                    target_ep_id = ep.get("id")
                                    break

                    if target_ep_id:
                        p_url = f"{api_base}/newtv/player.php?id={target_ep_id}"
                        p_req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
                        with urllib.request.urlopen(p_req, timeout=5.0) as pr:
                            p_data = json.loads(pr.read().decode("utf-8"))
                        vlink = p_data.get("video_link")
                        if vlink and ("?in=" not in vlink or "stop abusing" in vlink):
                            self._cached_usertoken = None
                            try:
                                p_req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
                                with urllib.request.urlopen(p_req, timeout=5.0) as pr:
                                    p_data = json.loads(pr.read().decode("utf-8"))
                                vlink = p_data.get("video_link")
                            except Exception:
                                pass
                        if vlink and vlink.startswith("http") and "?in=" in vlink and "stop abusing" not in vlink:
                            res_streams = [{
                                "name": f"CNC Verse [{ott.upper()} S{target_s:02d}E{target_e:02d} HLS]",
                                "title": f"{clean_title} S{target_s:02d}E{target_e:02d} · {ott.upper()} Multi-Audio HLS",
                                "url": vlink,
                                "is_hls": True,
                                "is_http": True,
                                "is_torrent": False,
                                "quality": "1080p",
                                "headers": {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.GatuNewTV v1.0",
                                    "Referer": "https://net52.cc/"
                                }
                            }]
                            self._stream_cache[cache_key] = (now, res_streams)
                            return res_streams
            except Exception as e:
                logger.debug(f"CNC Verse stream resolution failed for {ott}: {e}")

        self._stream_cache[cache_key] = (now, [])
        return []

    def load(self, data_str):
        try:
            data = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("{") else {"id": data_str}
        except Exception:
            data = {"id": data_str}
        item_id = data.get("id")
        ott = data.get("ott", "nf")
        title = data.get("title") or "CNC Verse Media"
        episodes = []
        if item_id:
            try:
                t = int(time.time())
                ep_url = f"https://net52.cc/mobile/{ott}/episodes.php?s={item_id}&series=1&t={t}"
                req = urllib.request.Request(ep_url, headers=self._get_headers(ott))
                with urllib.request.urlopen(req, timeout=4.0) as r:
                    ep_data = json.loads(r.read().decode("utf-8"))
                for s_key, ep_list in (ep_data.get("episodes") or {}).items():
                    s_num = int(s_key) if str(s_key).isdigit() else 1
                    for ep in (ep_list or []):
                        ep_id = ep.get("id")
                        e_num = int(ep.get("ep", 1)) if str(ep.get("ep", "")).isdigit() else 1
                        if ep_id:
                            episodes.append({
                                "data": json.dumps({"id": str(ep_id), "ott": ott, "is_episode": True}),
                                "name": ep.get("t") or f"Episode {e_num}",
                                "season": s_num,
                                "episode": e_num,
                            })
            except Exception:
                pass

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if episodes else CloudStreamTvType.Movie,
            "poster_url": None,
            "background_url": None,
            "description": f"{title} on CNC Verse",
            "year": data.get("year"),
            "rating_percent": None,
            "tags": ["CNC Verse", ott.upper()],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        try:
            data = json.loads(data_str) if isinstance(data_str, str) and data_str.startswith("{") else {"id": data_str}
        except Exception:
            data = {"id": data_str}
        item_id = data.get("id")
        ott = data.get("ott", "nf")
        if not item_id or data.get("is_series"):
            return []
        api_base = self._get_api_base()
        p_url = f"{api_base}/newtv/player.php?id={item_id}"
        try:
            req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
            with urllib.request.urlopen(req, timeout=5.0) as pr:
                p_data = json.loads(pr.read().decode("utf-8"))
            vlink = p_data.get("video_link")
            if vlink and ("?in=" not in vlink or "stop abusing" in vlink):
                self._cached_usertoken = None
                req = urllib.request.Request(p_url, headers=self._get_player_headers(ott))
                with urllib.request.urlopen(req, timeout=5.0) as pr:
                    p_data = json.loads(pr.read().decode("utf-8"))
                vlink = p_data.get("video_link")

            if vlink and vlink.startswith("http") and "?in=" in vlink and "stop abusing" not in vlink:
                return [{
                    "name": f"CNC Verse [{ott.upper()} 1080p HLS]",
                    "title": f"CNC Verse · {ott.upper()} Multi-Audio HLS",
                    "url": vlink,
                    "is_hls": True,
                    "is_http": True,
                    "is_torrent": False,
                    "quality": "1080p",
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0 /OS.GatuNewTV v1.0",
                        "Referer": "https://net52.cc/"
                    }
                }]
        except Exception:
            pass
        return []


class DudeFilmsCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#DudeFilms"
    name = "DudeFilms"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://new1.dudefilms.church"

    def _get_base_url(self):
        return get_dynamic_domain("dudefilms", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"DudeFilms catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        posts = re.findall(r'<div\b[^>]*class=["\'][^"\']*simple-grid-grid-post[^"\']*["\'][^>]*>(.*?)</div>\s*</div>\s*</div>', page_html, re.DOTALL | re.I)
        for post in posts:
            link_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', post, re.I)
            if not link_m:
                continue
            href = urllib.parse.urljoin(f"{base}/", html_lib.unescape(link_m.group(1)).strip())
            if href in seen:
                continue
            img_m = re.search(r'<img\b[^>]*>', post, re.I)
            img_tag = img_m.group(0) if img_m else ""
            poster = None
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            title = ""
            alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
            t_m = re.search(r'<a\b[^>]*title=["\']([^"\']+)["\']', post, re.I)
            h_m = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>', post, re.I | re.DOTALL)
            if t_m:
                title = t_m.group(1)
            elif h_m:
                title = re.sub(r'<[^>]+>', ' ', h_m.group(1))
            elif alt_m:
                title = alt_m.group(1)

            title = html_lib.unescape(title).strip()
            title = re.sub(r'^(?:permanent link to|download)\s+', '', title, flags=re.I).strip()
            title = re.sub(r'\s+', ' ', title)
            if not title:
                continue
            seen.add(href)
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"dudefilms:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        url = f"{base}/?s={urllib.parse.quote(clean_q)}"
        try:
            html = _http_get_text(url, timeout=6.0)
            matches = re.findall(r'<h[23][^>]*><a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>([^<]+)</a>', html)
            items = []
            for href, raw_title in matches:
                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                y_match = re.search(r"\((\d{4})\)", clean_title)
                year = int(y_match.group(1)) if y_match else None
                items.append({
                    "provider_id": self.id,
                    "data": f"dudefilms:{href}",
                    "name": clean_title,
                    "type": CloudStreamTvType.Movie,
                    "poster_url": None,
                    "background_url": None,
                    "description": clean_title,
                    "year": year,
                    "rating_percent": None,
                    "tags": ["DudeFilms"],
                    "episodes": [],
                })
            return items
        except Exception as e:
            logger.debug(f"DudeFilms search failed: {e}")
            return []

    def load(self, data_str):
        href = data_str.split("dudefilms:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "DudeFilms Media"
            img_m = re.search(r'<img\b[^>]*class=["\'][^"\']*wp-post-image[^"\']*["\'][^>]*>', html, re.I)
            if not img_m:
                img_m = re.search(r'<img\b[^>]*>', html, re.I)
            img_tag = img_m.group(0) if img_m else ""
            poster = None
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break
        except Exception:
            title = "DudeFilms Media"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:dflinks|archives/\d+)[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href,
                    })
                    with urllib.request.urlopen(m_req, timeout=6.0) as resp:
                        m_html = resp.read().decode("utf-8", errors="ignore")
                        archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"DudeFilms archive fetch failed: {e}")

            if archive_eps:
                for ep_num in sorted(archive_eps.keys()):
                    episodes.append({
                        "name": f"Episode {ep_num}",
                        "season": season_num,
                        "episode": ep_num,
                        "data": f"dudefilms:{href}#s={season_num}&e={ep_num}",
                        "poster_url": poster,
                    })
            else:
                ep_matches = re.finditer(r'<a[^>]+href=["\']([^\"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL)
                seen_ep = set()
                for em in ep_matches:
                    e_text = re.sub(r'<[^>]+>', ' ', em.group(2)).strip()
                    m = re.search(r'\b(?:Episode|EP|E)\s*(\d{1,3})\b', e_text, re.I)
                    if m:
                        ep_no = int(m.group(1))
                        if ep_no not in seen_ep:
                            seen_ep.add(ep_no)
                            episodes.append({
                                "name": f"Episode {ep_no}",
                                "season": season_num,
                                "episode": ep_no,
                                "data": f"dudefilms:{href}#s={season_num}&e={ep_no}",
                                "poster_url": poster,
                            })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if (is_series or episodes) else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["DudeFilms"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("dudefilms:")[-1].strip()
        target_episode = episode
        if "#" in href:
            frag = href.split("#", 1)[1]
            href = href.split("#", 1)[0]
            if not target_episode:
                em = re.search(r"\be=(\d+)\b", frag)
                if em:
                    target_episode = int(em.group(1))

        sources = []
        try:
            html = _http_get_text(href, timeout=8.0)
            target_links = []
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:dflinks|archives/\d+)[^"\']*)["\']', html)
            if archive_links:
                for arch_url in archive_links[:2]:
                    try:
                        m_req = urllib.request.Request(arch_url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Referer": href,
                        })
                        with urllib.request.urlopen(m_req, timeout=6.0) as resp:
                            m_html = resp.read().decode("utf-8", errors="ignore")
                            arch_eps = parse_archive_episodes(m_html)
                            if target_episode and target_episode in arch_eps:
                                target_links.extend(arch_eps[target_episode])
                            elif not target_episode and arch_eps:
                                for ep_items in arch_eps.values():
                                    target_links.extend(ep_items)
                    except Exception:
                        pass

            if not target_links:
                links = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
                for t_url, t_text in links:
                    if target_episode:
                        em = re.search(r'\b(?:Episode|EP|E)\s*(\d{1,3})\b', t_text, re.I)
                        if em and int(em.group(1)) != target_episode:
                            continue
                    target_links.append((t_url, t_text))

            seen_urls = set()
            for target_url, text in target_links:
                if target_url in seen_urls:
                    continue
                seen_urls.add(target_url)
                if any(k in target_url.lower() for k in ["dflinks.", "hubcloud.", "hubdrive.", "gdflix."]):
                    try:
                        if "dflinks." in target_url:
                            sub_html = _http_get_text(target_url, timeout=5.0)
                            hc_m = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\']', sub_html)
                            for hc_link in hc_m:
                                if "hubcloud." in hc_link or "hubdrive." in hc_link:
                                    hc_res = _extract_hubcloud_sources(hc_link, referer=target_url)
                                    if hc_res:
                                        sources.extend(hc_res)
                        else:
                            hc_res = _extract_hubcloud_sources(target_url, referer=href)
                            if hc_res:
                                sources.extend(hc_res)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"DudeFilms load_links failed: {e}")
        return sources


class ZinkmoviesCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#Zinkmovies"
    name = "Zinkmovies"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    DEFAULT_BASE = "https://new2.zinkmovies.tattoo"

    def _get_base_url(self):
        return get_dynamic_domain("zinkmovies", self.DEFAULT_BASE)

    def get_main_page(self, page=1):
        base = self._get_base_url().rstrip("/")
        page_url = f"{base}/" if int(page or 1) <= 1 else f"{base}/page/{int(page)}/"
        try:
            page_html = _http_get_text(page_url, timeout=8.0)
        except Exception as e:
            logger.debug(f"Zinkmovies catalog page {page} failed: {e}")
            return []

        movies, series = [], []
        seen = set()
        card_pattern = r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>\s*<div class="poster-card"[^>]*>(.*?)</div>\s*</div>\s*</a>'
        matches = re.findall(card_pattern, page_html, re.DOTALL | re.IGNORECASE)
        for href, card_html in matches:
            if href in seen:
                continue
            seen.add(href)
            poster = None
            img_m = re.search(r'<img\b[^>]*>', card_html, re.I)
            img_tag = img_m.group(0) if img_m else ""
            for attr in ("data-src", "data-lazy-src", "data-original", "src"):
                pm = re.search(rf'\b{attr}\s*=\s*["\']([^"\']+)["\']', img_tag, re.I)
                if pm and not pm.group(1).startswith("data:"):
                    poster = html_lib.unescape(pm.group(1)).strip()
                    break

            title = ""
            p_title_m = re.search(r'<p\b[^>]*class=["\']poster-title["\'][^>]*>(.*?)</p>', card_html, re.I | re.DOTALL)
            alt_m = re.search(r'\balt=["\']([^"\']+)["\']', img_tag, re.I)
            if p_title_m:
                title = html_lib.unescape(re.sub(r'<[^>]+>', ' ', p_title_m.group(1))).strip()
            elif alt_m:
                title = html_lib.unescape(alt_m.group(1)).strip()
            title = re.sub(r'\s+', ' ', title).strip()
            if not title:
                continue
            y_m = re.search(r"\b((?:19|20)\d{2})\b", title)
            year = int(y_m.group(1)) if y_m else None
            is_series = bool(re.search(r"\bseason\b|\bseries\b|\bS\d{1,2}(?:E\d{1,2})?\b", title, re.I))
            item = {
                "provider_id": self.id,
                "data": f"zinkmovies:{href}",
                "name": title,
                "type": CloudStreamTvType.TvSeries if is_series else CloudStreamTvType.Movie,
                "poster_url": poster,
                "background_url": poster,
                "description": title,
                "year": year,
            }
            (series if is_series else movies).append(item)

        sections = []
        if movies:
            sections.append(("Latest Movies", movies))
        if series:
            sections.append(("Latest Series", series))
        return sections

    def search(self, query):
        if not query or not query.strip():
            return []
        base = self._get_base_url()
        clean_q = re.sub(r"[^\w\s-]", " ", query).strip()
        url = f"{base}/?s={urllib.parse.quote(clean_q)}"
        try:
            html = _http_get_text(url, timeout=6.0)
            matches = re.findall(r'<a\s+[^>]*href=[\"\'](https?://[^\"\']+)[\"\']\s+title=[\"\']([^\"\']+)[\"\']', html)
            if not matches:
                matches = re.findall(r'<a\s+title=[\"\']([^\"\']+)[\"\']\s+href=[\"\'](https?://[^\"\']+)[\"\']', html)
                matches = [(m[1], m[0]) for m in matches]
            items = []
            for href, raw_title in matches:
                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                y_match = re.search(r"\((\d{4})\)", clean_title)
                year = int(y_match.group(1)) if y_match else None
                items.append({
                    "provider_id": self.id,
                    "data": f"zinkmovies:{href}",
                    "name": clean_title,
                    "type": CloudStreamTvType.Movie,
                    "poster_url": None,
                    "background_url": None,
                    "description": clean_title,
                    "year": year,
                    "rating_percent": None,
                    "tags": ["Zinkmovies"],
                    "episodes": [],
                })
            return items
        except Exception as e:
            logger.debug(f"Zinkmovies search failed: {e}")
            return []

    def load(self, data_str):
        href = data_str.split("zinkmovies:")[-1].strip()
        if "#" in href:
            href = href.split("#")[0].strip()
        try:
            html = _http_get_text(href, timeout=8.0)
            t_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", t_match.group(1)).strip() if t_match else "Zinkmovies Media"
            img_match = re.search(r"<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*class=[\"\'][^\"\']*wp-post-image", html, re.I)
            if not img_match:
                img_match = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)[\"']", html, re.I)
            poster = html_lib.unescape(img_match.group(1)).strip() if img_match else None
        except Exception:
            title = "Zinkmovies Media"
            poster = None
            html = ""

        is_series = is_cloudstream_series(title, href)
        episodes = []
        if is_series and html:
            s_match = re.search(r"\bseason\s*(\d{1,2})\b|\bS(\d{1,2})\b", f"{title} {href}", re.I)
            season_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:file-dl|archives/\d+)[^"\']*)["\']', html)
            archive_eps = {}
            if archive_links:
                try:
                    m_req = urllib.request.Request(archive_links[0], headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": href,
                    })
                    with urllib.request.urlopen(m_req, timeout=6.0) as resp:
                        m_html = resp.read().decode("utf-8", errors="ignore")
                        archive_eps = parse_archive_episodes(m_html)
                except Exception as e:
                    logger.debug(f"Zinkmovies archive fetch failed: {e}")

            if archive_eps:
                for ep_num in sorted(archive_eps.keys()):
                    episodes.append({
                        "name": f"Episode {ep_num}",
                        "season": season_num,
                        "episode": ep_num,
                        "data": f"zinkmovies:{href}#s={season_num}&e={ep_num}",
                        "poster_url": poster,
                    })
            else:
                ep_matches = re.finditer(r'<a[^>]+href=["\']([^\"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL)
                seen_ep = set()
                for em in ep_matches:
                    e_text = re.sub(r'<[^>]+>', ' ', em.group(2)).strip()
                    m = re.search(r'\b(?:Episode|EP|E)\s*(\d{1,3})\b', e_text, re.I)
                    if m:
                        ep_no = int(m.group(1))
                        if ep_no not in seen_ep:
                            seen_ep.add(ep_no)
                            episodes.append({
                                "name": f"Episode {ep_no}",
                                "season": season_num,
                                "episode": ep_no,
                                "data": f"zinkmovies:{href}#s={season_num}&e={ep_no}",
                                "poster_url": poster,
                            })

        return {
            "provider_id": self.id,
            "data": data_str,
            "name": title,
            "type": CloudStreamTvType.TvSeries if (is_series or episodes) else CloudStreamTvType.Movie,
            "poster_url": poster,
            "background_url": poster,
            "description": title,
            "year": None,
            "rating_percent": None,
            "tags": ["Zinkmovies"],
            "episodes": episodes,
        }

    def load_links(self, data_str, season=None, episode=None):
        href = data_str.split("zinkmovies:")[-1].strip()
        target_episode = episode
        if "#" in href:
            frag = href.split("#", 1)[1]
            href = href.split("#", 1)[0]
            if not target_episode:
                em = re.search(r"\be=(\d+)\b", frag)
                if em:
                    target_episode = int(em.group(1))

        sources = []
        try:
            html = _http_get_text(href, timeout=8.0)
            target_links = []
            archive_links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*(?:file-dl|archives/\d+)[^"\']*)["\']', html)
            if archive_links:
                for arch_url in archive_links[:2]:
                    try:
                        m_req = urllib.request.Request(arch_url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Referer": href,
                        })
                        with urllib.request.urlopen(m_req, timeout=6.0) as resp:
                            m_html = resp.read().decode("utf-8", errors="ignore")
                            arch_eps = parse_archive_episodes(m_html)
                            if target_episode and target_episode in arch_eps:
                                target_links.extend(arch_eps[target_episode])
                            elif not target_episode and arch_eps:
                                for ep_items in arch_eps.values():
                                    target_links.extend(ep_items)
                    except Exception:
                        pass

            if not target_links:
                links = re.findall(r'<a[^>]+href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>', html, re.DOTALL)
                for t_url, t_text in links:
                    if target_episode:
                        em = re.search(r'\b(?:Episode|EP|E)\s*(\d{1,3})\b', t_text, re.I)
                        if em and int(em.group(1)) != target_episode:
                            continue
                    target_links.append((t_url, t_text))

            seen_urls = set()
            for target_url, text in target_links:
                if target_url in seen_urls:
                    continue
                seen_urls.add(target_url)
                if any(k in target_url.lower() for k in ["file-dl", "hubcloud.", "hubdrive.", "gdflix.", "vcloud."]):
                    hc_res = _extract_hubcloud_sources(target_url, referer=href)
                    if hc_res:
                        sources.extend(hc_res)
        except Exception as e:
            logger.debug(f"Zinkmovies load_links failed: {e}")
        return sources


class CineStreamCloudStreamProvider(CloudStreamProvider):
    id = "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#CineStream"
    name = "CineStream"
    supported_types = ["movie", "series", "tv", "all"]
    catalog_sections = (
        {"id": "latest-movies", "name": "Latest Movies", "section": "Latest Movies", "type": "movie", "supports_pagination": True},
        {"id": "latest-series", "name": "Latest Series", "section": "Latest Series", "type": "series", "supports_pagination": True},
    )

    def get_main_page(self, page=1):
        sections_map = {"Latest Movies": [], "Latest Series": []}
        seen_titles = set()
        for prov in (HDhub4uCloudStreamProvider(), BollyflixCloudStreamProvider(), MoviesDriveCloudStreamProvider(), VegaMoviesCloudStreamProvider()):
            try:
                for sec_title, items in prov.get_main_page(page=page):
                    target_list = sections_map.get(sec_title)
                    if target_list is None:
                        continue
                    for it in items:
                        t_clean = (it.get("name") or "").strip().lower()
                        if t_clean in seen_titles:
                            continue
                        seen_titles.add(t_clean)
                        target_list.append(it)
            except Exception:
                pass
        res = []
        if sections_map["Latest Movies"]:
            res.append(("Latest Movies", sections_map["Latest Movies"]))
        if sections_map["Latest Series"]:
            res.append(("Latest Series", sections_map["Latest Series"]))
        return res

    def get_streams_for_media(self, title, media_type="movie", year=None, season=None, episode=None, imdb_id=None):
        streams = []
        for p in [YTSCloudStreamProvider(), TorrentioCloudStreamProvider(), CNCVerseCloudStreamProvider(), HDhub4uCloudStreamProvider(), DudeFilmsCloudStreamProvider(), ZinkmoviesCloudStreamProvider(), VegaMoviesCloudStreamProvider(), MoviesDriveCloudStreamProvider()]:
            if hasattr(p, "get_streams_for_media"):
                try:
                    res = p.get_streams_for_media(
                        title=title, media_type=media_type, year=year, season=season, episode=episode, imdb_id=imdb_id
                    )
                    if res:
                        streams.extend(res)
                except Exception:
                    pass
        return streams

    def search(self, query):
        results = []
        for p in [HDhub4uCloudStreamProvider(), BollyflixCloudStreamProvider(), MoviesDriveCloudStreamProvider(), DudeFilmsCloudStreamProvider(), ZinkmoviesCloudStreamProvider(), VegaMoviesCloudStreamProvider()]:
            try:
                res = p.search(query)
                if res:
                    results.extend(res)
            except Exception:
                pass
        return results

    def load(self, data_str):
        if "hdhub4u:" in data_str:
            return HDhub4uCloudStreamProvider().load(data_str)
        if "bollyflix:" in data_str:
            return BollyflixCloudStreamProvider().load(data_str)
        if "moviesdrive:" in data_str:
            return MoviesDriveCloudStreamProvider().load(data_str)
        if "vegamovies:" in data_str:
            return VegaMoviesCloudStreamProvider().load(data_str)
        if "moviesmod:" in data_str:
            return MoviesmodCloudStreamProvider().load(data_str)
        if "dudefilms:" in data_str:
            return DudeFilmsCloudStreamProvider().load(data_str)
        if "zinkmovies:" in data_str:
            return ZinkmoviesCloudStreamProvider().load(data_str)
        if "yts:" in data_str or (isinstance(data_str, str) and ("imdb_code" in data_str or "torrents" in data_str)):
            return YTSCloudStreamProvider().load(data_str)
        if isinstance(data_str, str) and "imdb_id" in data_str:
            return TorrentioCloudStreamProvider().load(data_str)
        if isinstance(data_str, str) and ("ott" in data_str or "net52" in data_str):
            return CNCVerseCloudStreamProvider().load(data_str)
        return GenericCloudStreamProvider(self.id, self.name).load(data_str)

    def load_links(self, data_str, season=None, episode=None):
        if "hdhub4u:" in data_str:
            return HDhub4uCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "bollyflix:" in data_str:
            return BollyflixCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "moviesdrive:" in data_str:
            return MoviesDriveCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "vegamovies:" in data_str:
            return VegaMoviesCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "moviesmod:" in data_str:
            return MoviesmodCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "dudefilms:" in data_str:
            return DudeFilmsCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "zinkmovies:" in data_str:
            return ZinkmoviesCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if "yts:" in data_str or (isinstance(data_str, str) and ("imdb_code" in data_str or "torrents" in data_str)):
            return YTSCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if isinstance(data_str, str) and "imdb_id" in data_str:
            return TorrentioCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        if isinstance(data_str, str) and ("ott" in data_str or "net52" in data_str):
            return CNCVerseCloudStreamProvider().load_links(data_str, season=season, episode=episode)
        return []


class GenericCloudStreamProvider(CloudStreamProvider):
    def __init__(self, provider_id, name="CloudStream Provider"):
        self.id = provider_id
        self.name = name
        self.supported_types = ["movie", "series", "live", "tv", "other"]

    def search(self, query):
        return []

    def load(self, data_str):
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
        if str(data_str).startswith("http://") or str(data_str).startswith("https://"):
            return [{
                "name": self.name,
                "url": data_str,
                "is_hls": ".m3u8" in data_str,
                "is_dash": ".mpd" in data_str,
            }]
        return []


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
    for p in _PROVIDERS.values():
        if p.id.lower() == clean_id:
            return p

    target_name = clean_id.split("#", 1)[-1] if "#" in clean_id else clean_id
    for p in _PROVIDERS.values():
        p_name = p.name.strip().lower()
        if p_name == target_name or p_name == clean_id:
            return p
        if p.id.lower().endswith(f"#{target_name}"):
            return p

    for p in _PROVIDERS.values():
        p_name = p.name.strip().lower()
        if p_name in target_name or target_name in p_name:
            return p

    # Fallback to Generic provider so every provider has an active instance
    p_name = provider_id.split("#", 1)[1] if "#" in provider_id else provider_id
    plugin = get_plugin(provider_id)
    if plugin:
        p_name = plugin.get("name") or p_name
    return GenericCloudStreamProvider(provider_id, name=p_name)

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
register_provider(BollyflixCloudStreamProvider())
register_provider(HDhub4uCloudStreamProvider())
register_provider(MoviesDriveCloudStreamProvider())
register_provider(MoviesmodCloudStreamProvider())
register_provider(VegaMoviesCloudStreamProvider())
register_provider(YTSCloudStreamProvider())

torrentio_prov = TorrentioCloudStreamProvider()
register_provider(torrentio_prov)
_PROVIDERS["Torrentio"] = torrentio_prov

cncverse_prov = CNCVerseCloudStreamProvider()
register_provider(cncverse_prov)
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CNC Verse Mobile"] = cncverse_prov
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CastleTvProvider"] = cncverse_prov
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#MovieBoxProviderIN"] = cncverse_prov
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#MovieBoxProvider"] = cncverse_prov
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#Rtally"] = cncverse_prov
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#HDOProvider"] = cncverse_prov
_PROVIDERS["CNC Verse"] = cncverse_prov
_PROVIDERS["CNC Verse Mobile"] = cncverse_prov
_PROVIDERS["CastleTvProvider"] = cncverse_prov
_PROVIDERS["MovieBoxProviderIN"] = cncverse_prov
_PROVIDERS["MovieBoxProvider"] = cncverse_prov
_PROVIDERS["Rtally"] = cncverse_prov
_PROVIDERS["HDOProvider"] = cncverse_prov

vegamovies_prov = VegaMoviesCloudStreamProvider()
register_provider(vegamovies_prov)
_PROVIDERS["https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#VegaMoviesProvider"] = vegamovies_prov
_PROVIDERS["VegaMoviesProvider"] = vegamovies_prov
_PROVIDERS["VegaMovies"] = vegamovies_prov

moviesdrive_prov = MoviesDriveCloudStreamProvider()
register_provider(moviesdrive_prov)
_PROVIDERS["https://raw.githubusercontent.com/redowan99/Redowan-CloudStream/master/repo.json#TheMoviesFlix"] = moviesdrive_prov
_PROVIDERS["TheMoviesFlix"] = moviesdrive_prov
_PROVIDERS["MoviesDrive"] = moviesdrive_prov

dudefilms_prov = DudeFilmsCloudStreamProvider()
register_provider(dudefilms_prov)
_PROVIDERS["https://raw.githubusercontent.com/redowan99/Redowan-CloudStream/master/repo.json#9kMovies"] = dudefilms_prov
_PROVIDERS["9kMovies"] = dudefilms_prov
_PROVIDERS["DudeFilms"] = dudefilms_prov

zinkmovies_prov = ZinkmoviesCloudStreamProvider()
register_provider(zinkmovies_prov)
_PROVIDERS["Zinkmovies"] = zinkmovies_prov

register_provider(CineStreamCloudStreamProvider())


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
    """Return browsable sections declared by enabled, runnable CloudStream adapters."""
    if not is_cloudstream_enabled():
        return []

    catalogs = []
    seen = set()
    for plugin in get_all_plugins():
        if not plugin.get("enabled") or not is_provider_runnable(plugin):
            continue
        plugin_id = str(plugin.get("id") or "").strip()
        provider = get_provider(plugin_id)
        if not plugin_id or not provider:
            continue
        for section in getattr(provider, "catalog_sections", ()) or ():
            if not isinstance(section, dict):
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
            catalogs.append({
                "addon_name": provider_display_name,
                "manifest_url": catalog_url(plugin_id),
                "base_url": catalog_url(plugin_id),
                "catalog_id": catalog_id,
                "catalog_name": catalog_name,
                "display_name": f"{provider_display_name} - {catalog_name}",
                "genres": list(section.get("genres") or []),
                "extra": [],
                "extraSupported": [],
                "type": catalog_type,
                "supports_pagination": bool(section.get("supports_pagination", False)),
                "_source": "cloudstream",
                "cloudstream_provider_id": plugin_id,
                "cloudstream_section": section_name,
            })
    return catalogs


def _get_provider_main_page(provider, page):
    cache_key = (str(provider.id), int(page or 1))
    now = time.time()
    with _MAIN_PAGE_CACHE_LOCK:
        cached = _MAIN_PAGE_CACHE.get(cache_key)
        if cached and now - cached[0] < _MAIN_PAGE_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            try:
                sections = provider.get_main_page(page=page)
            except TypeError:
                sections = provider.get_main_page()
            sections = list(sections or [])
        except Exception as e:
            logger.debug(f"CloudStream catalog fetch failed provider={provider.name!r}: {e}")
            sections = []
        _MAIN_PAGE_CACHE[cache_key] = (now, sections)
        return sections


def fetch_catalog_items(provider_id, catalog_id, page=1, limit=50, is_cancelled=None):
    """Load one declared CloudStream section and normalize it to Discover cards."""
    if is_cancelled and is_cancelled():
        return []
    plugin = get_plugin(provider_id)
    if not is_cloudstream_enabled() or not plugin or not plugin.get("enabled") or not is_provider_runnable(plugin):
        return []
    provider = get_provider(provider_id)
    section = next((
        item for item in (getattr(provider, "catalog_sections", ()) or [])
        if isinstance(item, dict) and str(item.get("id") or "") == str(catalog_id)
    ), None)
    if not section:
        return []

    wanted_section = str(section.get("section") or section.get("name") or "").strip().casefold()
    raw_items = []
    for entry in _get_provider_main_page(provider, page):
        if is_cancelled and is_cancelled():
            return []
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            section_name, items = str(entry[0] or "").strip().casefold(), entry[1]
        elif isinstance(entry, dict):
            section_name = str(entry.get("title") or entry.get("name") or "").strip().casefold()
            items = entry.get("items") or entry.get("results") or []
        else:
            continue
        if section_name == wanted_section:
            raw_items = items if isinstance(items, list) else []
            break

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
        else:
            card["alias_ids"] = [card["id"]]
        for key in ("genres", "description", "overview", "imdbRating", "runtime", "background", "videos", "trailer"):
            if item.get(key) is not None:
                card[key] = item[key]
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

def install_plugin(plugin_id):
    """
    Downloads and installs a CloudStream .cs3 package, verifies SHA-256 and ZIP safety.
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

        # Size check
        expected_size = plugin.get("file_size")
        if expected_size and len(data) != expected_size:
            logger.warning(f"Package size mismatch for {plugin.get('name')}: expected {expected_size}, got {len(data)}")

        # ZIP inspection & safety checks
        inspect_cs3_package(data)

        # SHA-256 Verification
        expected_hash = plugin.get("file_hash")
        verified = matches_file_hash(data, expected_hash)
        if expected_hash and not verified:
            return False, "SHA-256 checksum mismatch on downloaded package"

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
    database.toggle_cloudstream_plugin(plugin_id, bool(enabled))


def bulk_install_and_enable(plugin_ids):
    """Install and enable multiple plugins in parallel."""
    installed = 0
    enabled = 0
    errors = []
    for pid in plugin_ids:
        plugin = get_plugin(pid)
        if not plugin:
            continue
        if not plugin.get("is_installed") or plugin.get("version", 0) > (plugin.get("installed_version") or 0):
            ok, msg = install_plugin(pid)
            if ok:
                installed += 1
            else:
                errors.append(f"{plugin.get('name')}: {msg}")
                continue
        toggle_plugin(pid, True)
        enabled += 1
    return {
        "installed": installed,
        "enabled": enabled,
        "errors": errors,
    }


# --- PopcornBox Media Models Conversion ---

def clean_cloudstream_title(title_text):
    """
    Cleans release tags, audio formats, codec info, and full movie suffixes
    from CloudStream catalog titles for clean UI display.
    Returns (clean_title, year).
    """
    if not title_text:
        return "", None
    text = html_lib.unescape(str(title_text)).strip()
    text = re.sub(r"^(?:download|permanent link to|watch|stream)\s+", "", text, flags=re.IGNORECASE).strip()
    year_match = re.search(r"\b((?:19|20)\d{2})\b", text)
    year = year_match.group(1) if year_match else None

    split_match = re.split(
        r"\s*(?:\[[^\]]*\]|\{[^\}]*\}|\|.*$|\b(?:unrated|uncut|extended(?:\s+edition)?|director'?s\s+cut|theatrical(?:\s+cut)?|limited|proper|repack|remaster(?:ed)?|anniversary\s+edition|special\s+edition|blu[ .-]?ray|brrip|web[ .-]?dl|webrip|hdrip|hq-hdrip|dvdrip|hdtv|hdtc|hq-hdtc|ds4k|imax|x26[45]|(?:2160|1080|720|480)p|4k|dual audio|multi audio|full movie|full series|primevideo|netflix|amzn|disney\+?|hulu)\b)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    base = split_match[0].strip(" .-_:|")
    clean = base
    while True:
        prev = clean
        clean = re.sub(
            r"[\s(\[]*(?:(?:19|20)\d{2}|Season\s*\d+(?:\s*[-–&,]\s*\d+)?|S\d{1,2}(?:\s*[-–&,]\s*S?\d{1,2})?|Complete\s*Series|All\s*Episodes?)[)\]]*\s*$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip(" .-_:|")
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
        "description": item.get("description") or "",
        "is_cloudstream": True,
        "cloudstream_provider_id": item["provider_id"],
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

    videos = []
    if not episodes:
        videos.append({
            "id": route_id,
            "title": "Canlı Yayın" if raw_type == CloudStreamTvType.Live else (clean_name or raw_name),
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
    }


def playback_source_to_stream_dict(source, provider_name, provider_id):
    """Converts a CloudStreamPlaybackSource dict into a PopcornBox stream dictionary for MPV."""
    raw_url = str(source.get("url") or source.get("magnet") or "").strip()
    if not raw_url:
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
    title_parts = [p for p in [name, quality_str] if p and p not in name]
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
        return [playback_source_to_stream_dict(s, p_name, provider_id) for s in sources]
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

    enabled_plugins = [
        p for p in get_all_plugins()
        if p.get("enabled") and is_provider_runnable(p)
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
            return [search_item_to_card_dict(it) for it in items]
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
    title, media_type, year=None, season=None, episode=None, timeout=12.0, imdb_id=None, catalog_title=None
):
    """
    Searches enabled CloudStream providers for a standard movie or episode,
    resolves matching streams, and returns them for PopcornBox's streams list.
    """
    if not is_cloudstream_enabled() or not title or not title.strip():
        return []

    enabled_plugins = [
        p for p in get_all_plugins()
        if p.get("enabled") and is_provider_runnable(p)
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
                ]
                candidate_items = typed_items or list(search_items)
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
                        # fallback, as the reference client does.
                        if requested_year and candidate_year == requested_year:
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
                        and not (requested_year and requested_year in (match_year, loaded_year))
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
                        for ep in loaded.get("episodes") or []:
                            if (
                                _episode_number(ep.get("season")) == _episode_number(season)
                                and _episode_number(ep.get("episode")) == _episode_number(episode)
                            ):
                                link_data = ep.get("data")
                                break
                        if not link_data:
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
