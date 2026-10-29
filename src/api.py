import json
import urllib.request
import urllib.parse
import urllib.error
import os
import time
import hashlib
import logging
import socket
import ssl
import gzip
import zlib
import base64
from . import database
from .tmdb_helper import resolve_to_imdb_id, resolve_all_provider_ids, TMDB_GENRES, fetch_tmdb_standalone_metadata
import concurrent.futures
import threading
import queue
import contextlib
import re

def _create_ssl_context():
    try:
        # Keep the platform CA store and hostname verification enabled. Add-on
        # URLs are user supplied, so accepting arbitrary certificates would
        # allow a network attacker to replace metadata and stream responses.
        return ssl.create_default_context()
    except Exception:
        return None

_SSL_CONTEXT = _create_ssl_context()


# ---------------------------------------------------------------------------
# DNS-over-HTTPS (DoH) bypass for ISP-poisoned domains
# ---------------------------------------------------------------------------
# Some ISPs (notably in India) poison DNS responses so that blocked domains
# resolve to an interception IP.  That IP either returns an HTTP block page
# or, for HTTPS, immediately tears down the TLS handshake, producing an
# ``SSL: UNEXPECTED_EOF_WHILE_READING`` error.  The actual server (often
# behind Cloudflare) is perfectly reachable when we connect to the real IP
# with the correct SNI.
#
# The helpers below resolve hostnames via Google's DoH JSON endpoint and,
# when the result differs from what the system resolver returns, fetch the
# resource by connecting directly to the DoH-resolved IP.
# ---------------------------------------------------------------------------

import http.client as _http_client

# hostname -> (real_ips_tuple, monotonic_timestamp)
_DOH_CACHE = {}
_DOH_CACHE_LOCK = threading.Lock()
_DOH_CACHE_TTL = 600  # 10 minutes

# Hosts where DoH resolution already returned the same IP as the system
# resolver, i.e. DNS is not poisoned for that host.  No point retrying DoH
# for these -- they fail for a genuine server reason.
_DOH_NOT_POISONED = set()
_DOH_NOT_POISONED_LOCK = threading.Lock()


def _doh_resolve(hostname):
    """Resolve *hostname* via Google DNS-over-HTTPS, returning a list of IPs.

    Returns an empty list on any failure so callers can fall through.  Results
    are cached for ``_DOH_CACHE_TTL`` seconds.
    """
    now = time.monotonic()
    with _DOH_CACHE_LOCK:
        entry = _DOH_CACHE.get(hostname)
        if entry and now - entry[1] < _DOH_CACHE_TTL:
            return list(entry[0])
    try:
        doh_url = f"https://dns.google/resolve?name={urllib.parse.quote(hostname)}&type=A"
        req = urllib.request.Request(doh_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4, context=_SSL_CONTEXT) as resp:
            data = json.loads(resp.read())
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        if ips:
            with _DOH_CACHE_LOCK:
                _DOH_CACHE[hostname] = (tuple(ips), now)
        return ips
    except Exception:
        return []


def _is_dns_poisoned(hostname):
    """Return True if the system resolver likely gives a poisoned IP.

    CDNs like Cloudflare, BunnyCDN and Akamai serve different IPs based on
    location, so a simple "DoH IP ≠ system IP" check would false-positive on
    almost every CDN host.  Instead, we check whether the system-resolved IP
    actually has a working TLS stack for this hostname.  A poisoned IP will
    fail the TLS handshake because the interception server does not have a
    valid certificate for the hostname — or, more commonly, it simply drops
    the connection during the handshake (the ``UNEXPECTED_EOF`` error).

    A passing TLS handshake proves DNS is fine (regardless of whether the IP
    matches DoH); a failing one combined with a *different* DoH answer is a
    strong signal of poisoning.

    Caches negative results so the DoH path is never attempted for hosts
    whose DNS is clean.
    """
    with _DOH_NOT_POISONED_LOCK:
        if hostname in _DOH_NOT_POISONED:
            return False

    # Quick TLS probe to the system-resolved IP.  Use a short timeout:
    # a poisoned IP either closes the connection immediately or hangs, and
    # a healthy server completes the handshake in well under a second.
    try:
        system_ip = socket.gethostbyname(hostname)
    except Exception:
        return False

    tls_ok = False
    try:
        raw = socket.create_connection((system_ip, 443), timeout=2)
        try:
            ctx = _SSL_CONTEXT or ssl.create_default_context()
            ssock = ctx.wrap_socket(raw, server_hostname=hostname)
            ssock.close()
            tls_ok = True
        except Exception:
            raw.close()
    except Exception:
        pass

    if tls_ok:
        with _DOH_NOT_POISONED_LOCK:
            if len(_DOH_NOT_POISONED) < 2048:
                _DOH_NOT_POISONED.add(hostname)
        return False

    # TLS handshake failed.  See whether DoH gives a different IP.
    doh_ips = _doh_resolve(hostname)
    if not doh_ips:
        return False
    return system_ip not in doh_ips


def _doh_fetch(url, timeout=5.0):
    """Fetch *url* by connecting directly to a DoH-resolved IP.

    Returns the response body bytes, or *None* on failure.  Only useful when
    ``_is_dns_poisoned()`` has confirmed the system DNS is lying.
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if not path:
        path = "/"

    doh_ips = _doh_resolve(hostname)
    if not doh_ips:
        return None

    for ip in doh_ips[:2]:
        try:
            raw = socket.create_connection((ip, port), timeout=timeout)
            try:
                ctx = _SSL_CONTEXT or ssl.create_default_context()
                ssock = ctx.wrap_socket(raw, server_hostname=hostname)
            except Exception:
                raw.close()
                continue
            try:
                request_line = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {hostname}\r\n"
                    f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/120.0.0.0 Safari/537.36\r\n"
                    f"Accept: */*\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                )
                ssock.sendall(request_line.encode())

                chunks = []
                while True:
                    chunk = ssock.recv(16384)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            finally:
                ssock.close()

            # Separate headers from body.
            sep = data.find(b"\r\n\r\n")
            if sep < 0:
                continue
            header_block = data[:sep].decode("utf-8", errors="replace")
            body = data[sep + 4:]

            status_line = header_block.split("\r\n", 1)[0]
            # e.g. "HTTP/1.1 200 OK"
            parts = status_line.split(None, 2)
            if len(parts) < 2:
                continue
            status_code = int(parts[1])
            if status_code != 200:
                continue

            # Handle chunked transfer encoding.
            if "transfer-encoding: chunked" in header_block.lower():
                body = _decode_chunked(body)
                if body is None:
                    continue

            if body:
                return body
        except Exception:
            continue
    return None


def _decode_chunked(data):
    """Decode an HTTP chunked transfer-encoded body. Returns None on error."""
    result = []
    pos = 0
    while pos < len(data):
        # Find the chunk size line.
        nl = data.find(b"\r\n", pos)
        if nl < 0:
            break
        size_str = data[pos:nl].decode("ascii", errors="replace").strip()
        if not size_str:
            pos = nl + 2
            continue
        try:
            chunk_size = int(size_str.split(";", 1)[0], 16)
        except ValueError:
            return None
        if chunk_size == 0:
            break
        start = nl + 2
        end = start + chunk_size
        if end > len(data):
            result.append(data[start:])
            break
        result.append(data[start:end])
        pos = end + 2  # skip trailing \r\n
    return b"".join(result) if result else None



DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.bittorrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://uploads.gamebase.info:6969/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "udp://tracker.1337x.org:80/announce",
    "udp://tracker.pomf.se:80/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.srv00.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "udp://tracker.dler.com:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://evan.im:6969/announce",
    "udp://bittorrent-tracker.e-n-c-r-y-p-t.net:1337/announce",
    "udp://tracker.opentorrent.top:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.ilibr.org:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.dump.cl:6969/announce",
    "http://tracker.waaa.moe:6969/announce",
    "udp://tracker.bluefrog.pw:2710/announce",
    "udp://tracker.aruku.ovh:8081/announce",
    "udp://anime-tracker.aruku.kro.kr:8081/announce",
    "udp://mail.segso.net:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
    "https://tracker.leechshield.link:443/announce",
    "http://wegkxfcivgx.ydns.eu:80/announce",
    "https://t.213891.xyz:443/announce",
    "udp://tracker.gmi.gd:6969/announce",
    "udp://tracker.teambelgium.net:6969/announce",
    "http://tracker.xn--djrq4gl4hvoi.top:80/announce",
    "http://tracker.dhitechnical.com:6969/announce",
    "udp://tracker.wildkat.net:6969/announce",
    "udp://torrentclub.online:1984/announce",
    "http://bt1.archive.org:6969/announce",
    "http://bt2.archive.org:6969/announce",
    "udp://t.overflow.biz:6969/announce",
    "http://tracker.renfei.net:8080/announce",
    "https://tracker.zhuqiy.com:443/announce",
    "udp://open.stealth.si:80/announce"
]

if os.environ.get("FLATPAK_ID"):
    BASE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "popcorn-box")
else:
    BASE_DIR = os.path.expanduser("~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box")
CACHE_DIR = os.path.join(BASE_DIR, 'api')
os.makedirs(CACHE_DIR, exist_ok=True)

# Card metadata -- the catalog pages, meta responses and poster lookups that
# fill the grids -- is cached forever. A title's year, genres, countries and
# picture change rarely enough that asking TMDb or an addon for the same JSON
# twice is pure cost for the provider and pure latency for the user, so these
# readers pass no expiry at all. `METADATA_CACHE_HOURS` is the name the call
# sites use for that promise; `None` means "never expires" on every read path
# (memory cache, disk cache and the SQLite caches).
# Everything that genuinely changes keeps an explicit TTL of 24 h or less:
# streams, subtitles, trailers, searches, IPTV playlists and addon manifests.
METADATA_CACHE_HOURS = None

# Disk cache eviction: cache files are kept until the directory actually runs
# out of its size budget. Freshness is decided by each reader's max_age_hours at
# read time, so nothing here needs to delete a file merely because it is old --
# and deleting metadata files would send the app back to the servers for the
# same cards and posters. When the ceiling is reached, the least recently *read*
# entries go first (atime with relatime moves once a day, which is far finer
# grain than an eviction that rare), so the tiles the user keeps browsing
# outlive one-off requests. Prune at most once per hour.
_DISK_CACHE_MAX_BYTES = 1024 * 1024 * 1024
_DISK_CACHE_PRUNE_LOCK = threading.Lock()
_DISK_CACHE_LAST_PRUNE = 0.0

def _prune_disk_cache_if_needed(now=None):
    global _DISK_CACHE_LAST_PRUNE
    now = time.time() if now is None else now
    if not _DISK_CACHE_PRUNE_LOCK.acquire(blocking=False):
        return
    try:
        if now - _DISK_CACHE_LAST_PRUNE < 3600:
            return
        _DISK_CACHE_LAST_PRUNE = now
        try:
            entries = []
            total = 0
            for name in os.listdir(CACHE_DIR):
                path = os.path.join(CACHE_DIR, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                total += st.st_size
                entries.append((max(st.st_atime, st.st_mtime), st.st_size, path))
            if total > _DISK_CACHE_MAX_BYTES:
                entries.sort()
                for mtime, size, path in entries:
                    if total <= _DISK_CACHE_MAX_BYTES:
                        break
                    try:
                        os.remove(path)
                        total -= size
                    except OSError:
                        break
        except Exception as e:
            logging.debug(f"Disk cache prune failed: {e}")
    finally:
        _DISK_CACHE_PRUNE_LOCK.release()

_MEM_CACHE = {}
_MEM_CACHE_LOCK = threading.Lock()
_MEM_CACHE_MAX_ITEMS = 1000

_OFFLINE_HOSTS = {}
_OFFLINE_HOST_FAIL_COUNT = {}
_OFFLINE_HOSTS_LOCK = threading.Lock()
_OFFLINE_HOST_COOLDOWN = 60 # 1 minute circuit breaker for offline addons
_OFFLINE_HOST_MAX_FAILS = 2 # Trip breaker only after consecutive failures

def _remember_bounded_host_state(mapping, host, value, limit=512):
    """Update an insertion-ordered host map while bounding retained entries."""
    mapping.pop(host, None)
    mapping[host] = value
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))

# ---------------------------------------------------------------------------
# Provider Protection System (Rate Limiting, Cooldowns, & Concurrency Control)
# ---------------------------------------------------------------------------
_PROVIDER_PROTECTION_LOCK = threading.RLock()
# One lock per provider host. Fixed stripe sets collided often enough that a
# slow request to one addon could block an unrelated host for seconds and get
# it skipped entirely ("host slot unavailable"), so keys are the hosts directly.
_PROVIDER_HOST_LOCKS = {}             # host -> threading.Semaphore (window size per host)
_PROVIDER_LAST_REQUEST_TIME = {}      # host -> float (timestamp)
_PROVIDER_COOLDOWNS = {}              # host -> float (cooldown expiry timestamp)
_PROVIDER_COOLDOWN_REASONS = {}       # host -> str
_PROVIDER_STATE_LAST_PRUNE = 0.0
_STREAM_URL_CACHE = {}                # url -> (data, expiry_timestamp, write_generation)
_STREAM_URL_CACHE_LOCK = threading.Lock()
_STREAM_URL_CACHE_TTL = 900           # 15 minutes cache for identical stream URLs
_STREAM_URL_CACHE_GENERATION = 0
_REFRESH_RESULT_CACHE = {}            # (id, type, season, episode, provider) -> (data, expiry)
_REFRESH_RESULT_CACHE_LOCK = threading.Lock()
_REFRESH_RESULT_TTL = 60              # successful refresh results
_REFRESH_EMPTY_TTL = 30               # provider returned nothing; avoid retry storms
_REQUEST_INFLIGHT = {}
_REQUEST_INFLIGHT_LOCK = threading.Lock()

# Catalog and metadata URLs are the bursty ones: one discover page can ask a
# dozen different URLs of the same addon, and a TMDb country filter fans out to
# eight. They get a politeness window of their own: at most
# _CATALOG_HOST_PERMITS requests in flight per host, never closer together than
# _CATALOG_MIN_INTERVAL, and closed entirely while the host is cooling down or
# has just been marked offline. That is a fraction of the rate a browser would
# generate, while a fully serial window would be worse: an addon that takes a
# second per response would serve a six-row batch past the deadline the
# discover loader gives it, and the row would be missing rather than late.
# Streams keep the stricter one-at-a-time window, and both classes pace
# themselves on the same per-host clock, so one host never sees the two rates
# added together.
_CATALOG_MIN_INTERVAL = 0.25
_CATALOG_SLOT_WAIT = 4.0
_CATALOG_HOST_PERMITS = 3
_CATALOG_429_COOLDOWN = 120

# Posters and backdrops are the widest fan the app opens: one flowbox page can
# ask ten image workers for pictures that mostly live on two or three CDN hosts.
# A host serves two images at a time, never closer together than
# _IMAGE_MIN_INTERVAL, and queued waiters get a generous budget because a narrow
# window makes the wait for the last tile of a page proportional to the whole
# page's latency rather than to a burst. Two rather than one is the point where
# a page of posters finishes in reasonable time without the CDN seeing a
# burst -- waiting still happens on the image pool's own threads, never on the
# GTK one.
_IMAGE_MIN_INTERVAL = 0.06
_IMAGE_SLOT_WAIT = 15.0
_IMAGE_HOST_PERMITS = 2

def _retry_after_seconds(headers, default=300, floor=30, cap=1800):
    """Back-off seconds for an HTTP 429, from a delta-seconds or HTTP-date header."""
    raw = None
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        raw = None
    if not raw:
        return default
    secs = None
    try:
        secs = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        try:
            import email.utils
            parsed = email.utils.parsedate_to_datetime(str(raw).strip())
            secs = int(parsed.timestamp() - time.time())
        except Exception:
            secs = None
    if secs is None:
        return default
    return max(floor, min(secs, cap))

class _CancelCheckAsEvent:
    """Adapts an `is_cancelled` callable to the abort_event API the host slots take."""
    def __init__(self, fn):
        self._fn = fn

    def is_set(self):
        try:
            return bool(self._fn())
        except Exception:
            return False

def _prune_provider_state_locked(now):
    """Discard expired per-host timestamps and cooldowns periodically."""
    global _PROVIDER_STATE_LAST_PRUNE
    if now - _PROVIDER_STATE_LAST_PRUNE < 60:
        return
    _PROVIDER_STATE_LAST_PRUNE = now
    if len(_PROVIDER_LAST_REQUEST_TIME) > 256:
        stale_hosts = [host for host, ts in _PROVIDER_LAST_REQUEST_TIME.items() if now - ts > 3600]
        for host in stale_hosts:
            _PROVIDER_LAST_REQUEST_TIME.pop(host, None)
    expired_hosts = [host for host, expiry in _PROVIDER_COOLDOWNS.items() if expiry <= now]
    for host in expired_hosts:
        _PROVIDER_COOLDOWNS.pop(host, None)
        _PROVIDER_COOLDOWN_REASONS.pop(host, None)

def _get_provider_host(url_or_manifest):
    if not url_or_manifest:
        return ""
    try:
        parsed = urllib.parse.urlparse(str(url_or_manifest))
        host = parsed.netloc.lower()
        return host if host else str(url_or_manifest).lower()
    except Exception:
        return str(url_or_manifest).lower()

def is_provider_in_cooldown(url_or_manifest):
    host = _get_provider_host(url_or_manifest)
    if not host:
        return False
    with _PROVIDER_PROTECTION_LOCK:
        exp = _PROVIDER_COOLDOWNS.get(host, 0)
        now = time.time()
        if exp > now:
            return True
        elif host in _PROVIDER_COOLDOWNS:
            del _PROVIDER_COOLDOWNS[host]
            _PROVIDER_COOLDOWN_REASONS.pop(host, None)
    return False

def get_provider_cooldown_remaining(url_or_manifest):
    host = _get_provider_host(url_or_manifest)
    if not host:
        return 0
    with _PROVIDER_PROTECTION_LOCK:
        exp = _PROVIDER_COOLDOWNS.get(host, 0)
        return max(0, int(exp - time.time()))

def set_provider_cooldown(url_or_manifest, duration_seconds=300, reason="Rate limit"):
    host = _get_provider_host(url_or_manifest)
    if not host:
        return
    with _PROVIDER_PROTECTION_LOCK:
        _prune_provider_state_locked(time.time())
        duration = max(10, int(duration_seconds))
        _PROVIDER_COOLDOWNS[host] = time.time() + duration
        _PROVIDER_COOLDOWN_REASONS[host] = str(reason)
    logging.warning(f"[Provider Protection] Backing off from provider '{host}' for {duration}s. Reason: {reason}")

def clear_provider_cooldown(url_or_manifest=None):
    with _PROVIDER_PROTECTION_LOCK:
        if url_or_manifest:
            host = _get_provider_host(url_or_manifest)
            _PROVIDER_COOLDOWNS.pop(host, None)
            _PROVIDER_COOLDOWN_REASONS.pop(host, None)
        else:
            _PROVIDER_COOLDOWNS.clear()
            _PROVIDER_COOLDOWN_REASONS.clear()

EXPIRING_CREDENTIAL_QUERY_KEYS = {
    "accesskey", "accesssignature", "accesssig", "access_token", "accesstoken",
    "auth", "authkey", "authsig", "authsignature", "auth_token", "authtoken",
    "e", "exp", "expiration", "expire", "expires", "expiresat", "expiresin",
    "expires_in", "expiry", "hmac", "jwt", "keypairid", "policy", "sig",
    "signature", "signed", "st", "t", "token", "wsipauth", "wssecret"
}

EXPIRING_CREDENTIAL_FRAGMENTS = ("token", "signature", "expires", "expiry")

def has_likely_expiring_playback_credentials(url):
    """Return True if the URL contains query parameters indicating short-lived tokens/signatures."""
    if not url or not isinstance(url, str) or "?" not in url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        params = {key.lower(): values for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        for key in params.keys():
            k_lower = key.strip().lower()
            if not k_lower:
                continue
            k_compact = k_lower.replace("-", "").replace("_", "").replace(".", "")
            if k_lower in EXPIRING_CREDENTIAL_QUERY_KEYS or k_compact in EXPIRING_CREDENTIAL_QUERY_KEYS:
                return True
            if any(frag in k_lower or frag in k_compact for frag in EXPIRING_CREDENTIAL_FRAGMENTS):
                return True
    except Exception:
        pass
    return False

def get_stream_url_expiry_timestamp(url, saved_at=None):
    """
    Extract the numeric expiration epoch timestamp from an HTTP stream URL if present in query parameters.
    Returns float timestamp (seconds since epoch) or None.
    """
    if not url or not isinstance(url, str) or "?" not in url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        # Look for direct timestamp parameters
        ts_keys = ("exp", "expires", "expires_at", "expiresat", "expiry", "expiration", "e", "until", "valid_until")
        for k in ts_keys:
            val_list = params.get(k)
            if not val_list or not val_list[0]:
                continue
            try:
                val = float(val_list[0].strip())
                if 1000000000 <= val <= 3000000000:
                    return val
                elif val > 3000000000:
                    return val / 1000.0
            except (ValueError, TypeError):
                continue
        # Look for relative duration parameter (e.g. expires_in=7200)
        rel_keys = ("expires_in", "expiresin", "ttl", "x-amz-expires")
        for k in rel_keys:
            val_list = params.get(k)
            if not val_list or not val_list[0]:
                continue
            try:
                rel_val = float(val_list[0].strip())
                if 0 < rel_val < 864000:  # Up to 10 days
                    if k == "x-amz-expires" and params.get("x-amz-date"):
                        try:
                            import datetime
                            issued_at = datetime.datetime.strptime(
                                params["x-amz-date"][0].strip(), "%Y%m%dT%H%M%SZ"
                            ).replace(tzinfo=datetime.timezone.utc).timestamp()
                            return issued_at + rel_val
                        except (ValueError, TypeError, IndexError):
                            pass
                    base_ts = float(saved_at) if saved_at else time.time()
                    return base_ts + rel_val
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return None

def is_stream_url_expired(url, buffer_seconds=180, saved_at=None):
    """
    Determine if a stream URL has expired or will expire within buffer_seconds.
    If the URL has expiring credentials with a known expiry timestamp, checks if now + buffer_seconds >= expiry.
    If the URL has expiring credentials but no timestamp, checks if saved_at is older than 1 hour.
    """
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    
    expiry_ts = get_stream_url_expiry_timestamp(url, saved_at=saved_at)
    now = time.time()
    if expiry_ts is not None:
        return (now + buffer_seconds) >= expiry_ts
    
    # If it has signed/token credentials but no explicit timestamp, treat as ephemeral (1 hour default max TTL)
    if has_likely_expiring_playback_credentials(url):
        if saved_at:
            try:
                age = now - float(saved_at)
                if age > 3600:  # 1 hour
                    return True
            except (ValueError, TypeError):
                return True
    return False

def stream_matches_binge_group(stream, target_binge_group):
    """Return True if the stream matches the target bingeGroup from behaviorHints."""
    if not stream or not target_binge_group or not isinstance(stream, dict):
        return False
    bh = stream.get("behaviorHints")
    stream_bg = stream.get("binge_group") or (bh.get("bingeGroup") if isinstance(bh, dict) else None)
    if not stream_bg or not isinstance(stream_bg, str):
        return False
    return stream_bg.strip().lower() == str(target_binge_group).strip().lower()

def check_stream_rate_limit_warning(stream):
    """
    Check if a stream entry returned by an addon is actually a rate-limit/ban notice.
    Returns the warning string if detected, or None.
    """
    if not isinstance(stream, dict):
        return None

    # Check behaviorHints (e.g. bingeGroup: "fenixflix-ratelimit")
    bh = stream.get("behaviorHints")
    if isinstance(bh, dict):
        bg = str(bh.get("bingeGroup") or "").lower()
        if "ratelimit" in bg or "rate-limit" in bg or "banned" in bg:
            return str(stream.get("title") or stream.get("name") or bg).strip()

    name = str(stream.get("name") or "")
    title = str(stream.get("title") or stream.get("stream_title") or "")
    desc = str(stream.get("description") or "")
    filename = str(stream.get("filename") or "")
    combined = f"{name} {title} {desc} {filename}".lower()

    warning_signatures = [
        "muitas requisições",
        "muitas requisicoes",
        "aguarde 5 minutos",
        "aguarde alguns minutos",
        "rate limit exceeded",
        "rate limit reached",
        "rate-limit",
        "too many requests",
        "slow down",
        "temporarily banned",
        "temporarily blocked",
        "(banned)",
        "ip banned",
        "ip blocked",
    ]
    for sig in warning_signatures:
        if sig in combined:
            return (title or name or desc or sig).strip()

    has_media = bool(stream.get("url") or stream.get("infoHash") or stream.get("magnet") or stream.get("ytId"))
    if not has_media and any(w in combined for w in ["banned", "bloqueado", "aguarde", "limit"]):
        return (title or name or "Rate limit message").strip()

    return None

def extract_trailer_id(m):
    """
    Extract YouTube trailer ID or URL from any metadata dict or catalog item.
    Handles 'trailer' (str/dict), 'trailerStreams' (list), 'trailers' (list),
    'ytId' / 'youtube_id', and trailer entries within 'videos'.
    """
    if not m or not isinstance(m, dict):
        return None

    # 1. Direct trailer field
    tr = m.get("trailer")
    if tr:
        if isinstance(tr, str) and tr.strip():
            clean = tr.strip()
            if clean.lower() not in ("none", "null", "false", ""):
                return clean
        elif isinstance(tr, dict):
            sub = tr.get("source") or tr.get("ytId") or tr.get("url")
            if sub and str(sub).strip():
                return str(sub).strip()

    # 2. trailerStreams list (standard Cinemeta format)
    trailer_streams = m.get("trailerStreams")
    if trailer_streams and isinstance(trailer_streams, list):
        for ts in trailer_streams:
            if isinstance(ts, dict):
                yt_id = ts.get("ytId") or ts.get("url")
                if yt_id and str(yt_id).strip():
                    return str(yt_id).strip()
            elif isinstance(ts, str) and ts.strip():
                return ts.strip()

    # 3. trailers list (Cinemeta / Stremio format)
    trailers = m.get("trailers")
    if trailers and isinstance(trailers, list):
        for t in trailers:
            if isinstance(t, dict):
                source = t.get("source") or t.get("ytId") or t.get("url")
                if source and str(source).strip():
                    return str(source).strip()
            elif isinstance(t, str) and t.strip():
                return t.strip()

    # 4. Direct ytId or youtube_id
    direct_yt = m.get("ytId") or m.get("youtube_id")
    if direct_yt and isinstance(direct_yt, str) and direct_yt.strip():
        return direct_yt.strip()

    # 5. Check videos list for a trailer entry
    videos = m.get("videos")
    if videos and isinstance(videos, list):
        for v in videos:
            if isinstance(v, dict):
                is_tr = (
                    v.get("trailer") is True
                    or str(v.get("type", "")).lower() == "trailer"
                    or "trailer" in str(v.get("title", "")).lower()
                )
                if is_tr:
                    vid_id = v.get("ytId") or v.get("source") or v.get("id") or v.get("url")
                    if vid_id and str(vid_id).strip():
                        s_vid = str(vid_id).strip()
                        if s_vid.startswith("yt_id:"):
                            return s_vid.split("yt_id:", 1)[1]
                        return s_vid

    return None

def _get_cached_stream_url(url):
    if not url:
        return None
    with _STREAM_URL_CACHE_LOCK:
        entry = _STREAM_URL_CACHE.get(url)
        if entry:
            data, exp = entry[:2]
            if time.time() < exp:
                return data
            del _STREAM_URL_CACHE[url]
    return None

def _save_cached_stream_url(url, streams, ttl=_STREAM_URL_CACHE_TTL):
    global _STREAM_URL_CACHE_GENERATION
    if not url or streams is None:
        return
    # Never let an endpoint response cache outlive the media credentials it
    # contains. Signed URLs can expire much sooner than our normal cache TTL.
    now = time.time()
    effective_ttl = max(0.0, float(ttl))
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream.setdefault("saved_at", now)
        stream_url = stream.get("url") or stream.get("externalUrl") or ""
        expiry = stream.get("expires_at")
        try:
            expiry = float(expiry) if expiry is not None else None
        except (TypeError, ValueError):
            expiry = None
        if expiry is None:
            expiry = get_stream_url_expiry_timestamp(stream_url, saved_at=stream.get("saved_at") or now)
        if expiry is not None:
            effective_ttl = min(effective_ttl, expiry - now - 180)
        elif stream.get("has_expiring_credentials") or has_likely_expiring_playback_credentials(stream_url):
            effective_ttl = min(effective_ttl, 3600)
    if effective_ttl <= 0:
        return
    with _STREAM_URL_CACHE_LOCK:
        if len(_STREAM_URL_CACHE) > 500:
            now = time.time()
            expired = [k for k, v in list(_STREAM_URL_CACHE.items()) if v[1] <= now]
            for k in expired:
                del _STREAM_URL_CACHE[k]
            while len(_STREAM_URL_CACHE) > 400:
                evicted = next(iter(_STREAM_URL_CACHE))
                _STREAM_URL_CACHE.pop(evicted, None)
        _STREAM_URL_CACHE_GENERATION += 1
        _STREAM_URL_CACHE[url] = (streams, now + effective_ttl, _STREAM_URL_CACHE_GENERATION)

def clear_stream_url_cache():
    """Drop cached stream endpoint responses, including negative (empty) entries."""
    global _STREAM_URL_CACHE_GENERATION
    with _STREAM_URL_CACHE_LOCK:
        _STREAM_URL_CACHE.clear()
        _STREAM_URL_CACHE_GENERATION += 1

@contextlib.contextmanager
def _acquire_provider_slot(host, min_interval=0.4, timeout=4.0, abort_event=None,
                           max_wait=None, may_wait=True, permits=1, slot_key=None):
    """
    Acquires a concurrency slot for a provider host:
    - Never more than `permits` in-flight requests per host (1 by default).
    - At least min_interval (400ms) spacing between successive requests.
    - Aborts cleanly on cooldown or abort_event.

    `max_wait` overrides how long a caller queues for the host. `may_wait=False`
    is for callers that must not block at all (the GTK main thread): the host
    window gets a single non-blocking attempt and unmet spacing denies the slot
    instead of sleeping, so pacing never turns into a frozen UI.

    `slot_key` puts this caller in a different concurrency window while still
    pacing on the host's single clock; catalog and stream traffic use separate
    windows so a page of slow catalog rows cannot starve a stream request, and
    neither one doubles the request rate the other is already making.
    """
    if not host:
        yield True
        return

    window_key = slot_key or host
    with _PROVIDER_PROTECTION_LOCK:
        now = time.time()
        _prune_provider_state_locked(now)
        lock = _PROVIDER_HOST_LOCKS.get(window_key)
        if lock is None:
            # A 1-permit semaphore behaves exactly like a lock, so the strict
            # stream window and the wider catalog window share one mechanism.
            lock = threading.Semaphore(max(1, int(permits)))
            _PROVIDER_HOST_LOCKS[window_key] = lock

    start_wait = time.time()
    acquired = False
    if max_wait is None:
        max_wait = min(float(timeout), 4.0) if timeout else 4.0
    max_wait = float(max_wait) if may_wait else 0.0
    while True:
        if abort_event and abort_event.is_set():
            yield False
            return
        if is_provider_in_cooldown(host):
            yield False
            return
        if lock.acquire(timeout=0.08 if max_wait > 0 else 0.0):
            acquired = True
            break
        if time.time() - start_wait >= max_wait:
            break

    if not acquired:
        yield False
        return

    did_request = False
    try:
        if abort_event and abort_event.is_set():
            yield False
            return
        if is_provider_in_cooldown(host):
            yield False
            return

        # Claim the next slot for this host while holding the state lock, and
        # claim the *earliest legal start time* rather than "now": two siblings
        # that merely read the last-request timestamp would both see an idle
        # host and start in the same instant, which is exactly the burst this
        # window exists to prevent. A queue of polite requests therefore starts
        # min_interval apart instead of piling up on one free slot.
        with _PROVIDER_PROTECTION_LOCK:
            now = time.time()
            next_allowed = max(now, _PROVIDER_LAST_REQUEST_TIME.get(host, 0) + min_interval)
            if may_wait:
                _PROVIDER_LAST_REQUEST_TIME[host] = next_allowed
            wait_for = next_allowed - now

        if wait_for > 0:
            if not may_wait:
                # A caller that cannot pace the host has to let someone else who
                # can fetch it; skipping the spacing would defeat the window.
                yield False
                return
            while time.time() < next_allowed:
                if abort_event and abort_event.is_set():
                    yield False
                    return
                time.sleep(min(0.04, max(0.001, next_allowed - time.time())))

        did_request = True
        yield True
    finally:
        # Only stamp the spacing timestamp when a request was actually made;
        # aborted/cooldown paths must not delay the next real request.
        if did_request:
            with _PROVIDER_PROTECTION_LOCK:
                _PROVIDER_LAST_REQUEST_TIME[host] = max(
                    _PROVIDER_LAST_REQUEST_TIME.get(host, 0), time.time())
        lock.release()

@contextlib.contextmanager
def _acquire_catalog_window(host, abort_event=None, may_wait=True):
    """The polite window catalog, metadata and manifest traffic fetches through.

    Separate from the stream window on purpose: a discover batch of six rows must
    not have to finish one HTTP round trip at a time (it would miss the loader's
    deadline and show no row at all), but it is still bounded to
    `_CATALOG_HOST_PERMITS` concurrent requests per host, paced on the same clock
    the streams path paces on.
    """
    with _acquire_provider_slot(
        host,
        min_interval=_CATALOG_MIN_INTERVAL,
        max_wait=_CATALOG_SLOT_WAIT if may_wait else 0.0,
        may_wait=may_wait,
        abort_event=abort_event,
        permits=_CATALOG_HOST_PERMITS,
        slot_key=f"{host}#catalog",
    ) as open_window:
        yield open_window

@contextlib.contextmanager
def _acquire_image_window(host, is_cancelled=None, may_wait=True):
    """The polite window poster and backdrop bytes fetch through.

    Bounded like the catalog window and paced on the same per-host clock, with a
    shorter spacing because image hosts are CDNs and a blank tile is visible.
    A caller that is denied serves whatever it already cached and moves on; it
    must not retry in a loop, which is what turns one scroll into a burst.
    """
    abort_event = _CancelCheckAsEvent(is_cancelled) if is_cancelled else None
    with _acquire_provider_slot(
        host,
        min_interval=_IMAGE_MIN_INTERVAL,
        max_wait=_IMAGE_SLOT_WAIT if may_wait else 0.0,
        may_wait=may_wait,
        abort_event=abort_event,
        permits=_IMAGE_HOST_PERMITS,
        slot_key=f"{host}#image",
    ) as open_window:
        yield open_window

def _get_stream_provider_options():
    """Read bounded timeout/retry preferences, with safe defaults."""
    timeout_seconds = 20
    retries = 1
    try:
        from .preferences import settings
        timeout_seconds = settings.get_double("stream-provider-timeout")
        retries = settings.get_double("stream-provider-retries")
    except Exception:
        pass
    return max(5, min(int(timeout_seconds), 30)), max(0, min(int(retries), 2))


def _wait_before_provider_retry(attempt, abort_event=None):
    delay = min(1.5 * (2 ** attempt), 4.0)
    print(f"[STREAM DEBUG] retry backoff attempt={attempt + 1} delay={delay:.1f}s")
    if abort_event:
        return not abort_event.wait(delay)
    time.sleep(delay)
    return True


def _request_addon_streams(addon, url, timeout=None, abort_event=None, retries=None, _attempt=0, bypass_url_cache=False):
    """
    Politely requests stream data from an addon URL with:
    - Host-level concurrency limit (max 1 concurrent in-flight request per host)
    - Minimum inter-request delay (600ms spacing per host)
    - Active cooldown check (e.g. from 402, 403, 429 or provider ban/wait warnings)
    - HTTP 429 Retry-After handling
    - Detection and filtering of provider rate-limit/ban messages
    - Short-term stream URL in-memory caching (including negative caching for empty responses)
    """
    default_timeout, default_retries = _get_stream_provider_options()
    timeout = default_timeout if timeout is None else max(1, min(int(timeout), 30))
    retries = default_retries if retries is None else max(0, min(int(retries), 2))
    if not url or (abort_event and abort_event.is_set()):
        return []

    manifest_url = addon.get("manifest_url", "")
    host = _get_provider_host(manifest_url) or _get_provider_host(url)
    addon_name = addon.get("name", "Unknown")
    with _STREAM_URL_CACHE_LOCK:
        entry = _STREAM_URL_CACHE.get(url)
        url_cache_start_version = entry[2] if entry and len(entry) > 2 else 0

    # Check the exact endpoint cache before provider cooldown. A still-valid
    # response is safe to reuse and avoids touching a cooling provider.
    # Explicit refreshes skip it and request revalidation from the add-on.
    if not bypass_url_cache:
        cached_streams = _get_cached_stream_url(url)
        if cached_streams is not None:
            print(f"[STREAM DEBUG] request URL cache hit addon={addon_name!r} cards={len(cached_streams)}")
            return cached_streams

    if is_provider_in_cooldown(host):
        rem = get_provider_cooldown_remaining(host)
        print(f"[STREAM DEBUG] request skipped addon={addon_name!r} host={host!r}: cooldown {rem}s")
        logging.info(f"[Provider Protection] Addon '{addon.get('name')}' ({host}) is in cooldown for {rem}s. Skipping request.")
        return []

    # 3. Acquire host slot (concurrency limit + polite delay of 600ms)
    with _acquire_provider_slot(host, min_interval=0.6, timeout=timeout, abort_event=abort_event) as acquired:
        if not acquired:
            print(f"[STREAM DEBUG] request skipped addon={addon_name!r} host={host!r}: host slot unavailable or cancelled")
            return []

        # Re-check the cache once inside the slot: another thread may have
        # completed the identical request while this one waited for the slot.
        if not bypass_url_cache:
            cached_streams = _get_cached_stream_url(url)
            if cached_streams is not None:
                print(f"[STREAM DEBUG] request URL cache hit after slot addon={addon_name!r} cards={len(cached_streams)}")
                return cached_streams
        else:
            # A forced refresh must bypass an old response, but concurrent
            # forced refreshes for the same endpoint should share the response
            # fetched by the request that reached the host slot first.
            with _STREAM_URL_CACHE_LOCK:
                entry = _STREAM_URL_CACHE.get(url)
                current_version = entry[2] if entry and len(entry) > 2 else 0
                if entry and current_version > url_cache_start_version and time.time() < entry[1]:
                    print(f"[STREAM DEBUG] concurrent forced refresh coalesced addon={addon_name!r}")
                    return entry[0]

        retry_requested = False
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            if bypass_url_cache:
                headers['Cache-Control'] = 'no-cache'
                headers['Pragma'] = 'no-cache'
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
                print(f"[STREAM DEBUG] HTTP response addon={addon_name!r} status={getattr(response, 'status', 'unknown')}")
                data_str = response.read().decode('utf-8')
            if not data_str or not data_str.strip():
                print(f"[STREAM DEBUG] empty response body addon={addon_name!r}")
                _save_cached_stream_url(url, [], 600)
                return []
            data = json.loads(data_str)
            _record_addon_success(manifest_url)

            raw_streams = data.get("streams", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            print(f"[STREAM DEBUG] decoded response addon={addon_name!r} raw_cards={len(raw_streams)}")
            clean_streams = []
            warning_msg = None

            for s in raw_streams:
                if isinstance(s, dict):
                    warn = check_stream_rate_limit_warning(s)
                    if warn:
                        warning_msg = warn
                        continue
                    clean_streams.append(s)
                else:
                    clean_streams.append(s)

            if warning_msg:
                set_provider_cooldown(host, duration_seconds=300, reason=warning_msg)
                logging.warning(f"[Provider Protection] Provider '{addon.get('name')}' returned rate-limit notice: '{warning_msg}'. Cooldown set for 300s.")

            # Negative caching: cache empty responses too so we don't spam addons that index 0 streams for this item
            _save_cached_stream_url(url, clean_streams, 900 if clean_streams else 600)

            return clean_streams

        except urllib.error.HTTPError as e:
            code = e.code
            if code == 429:
                cooldown_secs = _retry_after_seconds(getattr(e, "headers", None))
                set_provider_cooldown(host, duration_seconds=cooldown_secs, reason="HTTP 429 Too Many Requests")
                print(f"[STREAM DEBUG] HTTP 429 addon={addon_name!r} cooldown_seconds={cooldown_secs}")
                logging.warning(f"[Provider Protection] Provider '{addon.get('name')}' ({host}) returned HTTP 429. Cooldown set for {cooldown_secs}s.")
                if not (abort_event and abort_event.is_set()):
                    _record_addon_failure(manifest_url)
                retry_requested = False
            elif code == 402:
                # 402 Payment Required / Deployment Disabled (e.g. Vercel quota limit exceeded)
                set_provider_cooldown(host, duration_seconds=900, reason="HTTP 402 Payment Required (Quota Exceeded)")
                print(f"[STREAM DEBUG] HTTP 402 addon={addon_name!r} quota exceeded, cooldown 900s")
                logging.warning(f"[Provider Protection] Provider '{addon.get('name')}' ({host}) returned HTTP 402. Cooldown set for 900s.")
                if not (abort_event and abort_event.is_set()):
                    _record_addon_failure(manifest_url)
                retry_requested = False
            elif code == 403:
                # 403 Forbidden / Cloudflare challenge / IP blocked
                set_provider_cooldown(host, duration_seconds=600, reason="HTTP 403 Forbidden (Blocked)")
                print(f"[STREAM DEBUG] HTTP 403 addon={addon_name!r} blocked, cooldown 600s")
                logging.warning(f"[Provider Protection] Provider '{addon.get('name')}' ({host}) returned HTTP 403. Cooldown set for 600s.")
                if not (abort_event and abort_event.is_set()):
                    _record_addon_failure(manifest_url)
                retry_requested = False
            elif code in (502, 503, 504):
                print(f"[STREAM DEBUG] HTTP {code} addon={addon_name!r}")
                if _attempt < retries and _wait_before_provider_retry(_attempt, abort_event):
                    retry_requested = True
                else:
                    set_provider_cooldown(host, duration_seconds=180, reason=f"HTTP {code} Server Overload")
                    if not (abort_event and abort_event.is_set()):
                        _record_addon_failure(manifest_url)
            elif code >= 500:
                print(f"[STREAM DEBUG] HTTP {code} addon={addon_name!r}")
                if _attempt < retries and _wait_before_provider_retry(_attempt, abort_event):
                    retry_requested = True
                elif not (abort_event and abort_event.is_set()):
                    _record_addon_failure(manifest_url)
            else:
                print(f"[STREAM DEBUG] HTTP {code} addon={addon_name!r}")
                if not (abort_event and abort_event.is_set()):
                    _record_addon_failure(manifest_url)
            try:
                e.close()
            except Exception:
                pass
        except urllib.error.URLError as e:
            # DoH bypass for ISP-poisoned DNS (SSL handshake torn down by
            # the interception IP).
            reason_str = str(getattr(e, "reason", "")).lower()
            if host and ("unexpected_eof" in reason_str or "ssl" in reason_str):
                try:
                    parsed_host = urllib.parse.urlparse(url).hostname
                    if parsed_host and _is_dns_poisoned(parsed_host):
                        doh_body = _doh_fetch(url, timeout=timeout)
                        if doh_body:
                            data_str = doh_body.decode("utf-8")
                            if data_str and data_str.strip():
                                data = json.loads(data_str)
                                _record_addon_success(manifest_url)
                                raw_streams = data.get("streams", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                                clean_streams = [s for s in raw_streams if isinstance(s, dict) and not check_stream_rate_limit_warning(s)]
                                _save_cached_stream_url(url, clean_streams, 900 if clean_streams else 600)
                                print(f"[STREAM DEBUG] DoH bypass success addon={addon_name!r} streams={len(clean_streams)}")
                                return clean_streams
                except Exception:
                    pass
            print(f"[STREAM DEBUG] URL error addon={addon_name!r}: {e.reason}")
            logging.debug(f"Error fetching from addon {addon.get('name')}: {e.reason}")
            if _attempt < retries and _wait_before_provider_retry(_attempt, abort_event):
                retry_requested = True
            elif not (abort_event and abort_event.is_set()):
                _record_addon_failure(manifest_url)
        except TimeoutError:
            print(f"[STREAM DEBUG] timeout addon={addon_name!r} timeout={timeout}s")
            logging.debug(f"Timeout fetching from addon {addon.get('name')}")
            # Do NOT immediately retry after a long timeout - server is already unresponsive
            set_provider_cooldown(host, duration_seconds=120, reason="Request Timeout (Unresponsive)")
            if not (abort_event and abort_event.is_set()):
                _record_addon_failure(manifest_url)
        except Exception as e:
            print(f"[STREAM DEBUG] request error addon={addon_name!r}: {type(e).__name__}: {e}")
            err_str = str(e).lower()
            logging.debug(f"Error fetching from addon {addon.get('name')}: {e}")
            transient_error = any(kw in err_str for kw in ["timed out", "timeout", "connection refused", "connection reset", "no route", "name or service"])
            if transient_error and _attempt < retries and _wait_before_provider_retry(_attempt, abort_event):
                retry_requested = True
            elif not (abort_event and abort_event.is_set()) and transient_error:
                _record_addon_failure(manifest_url)

    # Retry after releasing the host slot: the slot lock is not reentrant, so a
    # recursive call made from inside the `with` block would wait for itself
    # until timeout and silently drop the retry.
    if retry_requested:
        print(f"[STREAM DEBUG] retrying addon={addon_name!r} (attempt {_attempt + 1})")
        return _request_addon_streams(addon, url, timeout, abort_event, retries, _attempt + 1, bypass_url_cache)
    return []


def _is_host_marked_offline(host, now=None):
    """True while a host's circuit breaker is tripped from recent failures."""
    if not host:
        return False
    with _OFFLINE_HOSTS_LOCK:
        last_fail = _OFFLINE_HOSTS.get(host)
    return last_fail is not None and (now or time.time()) - last_fail < _OFFLINE_HOST_COOLDOWN


def _read_cached_response_json(cache_file):
    """Return a stored response of any age, or None when there is nothing to serve."""
    if not cache_file or not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.debug(f"Failed to read cached response {cache_file}: {e}")
        return None


def _get_cached_request_uncollapsed(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, is_cancelled=None):
    if not url:
        return None
    now = time.time()
    # 1. Fast in-memory cache check
    with _MEM_CACHE_LOCK:
        if url in _MEM_CACHE:
            cached_data, cached_time = _MEM_CACHE[url]
            if max_age_hours is None or (now - cached_time) < (max_age_hours * 3600):
                return cached_data
            else:
                del _MEM_CACHE[url]

    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, url_hash)
    
    if headers is None:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    # 2. Check if disk cache exists and is fresh
    if os.path.exists(cache_file):
        age_hours = (now - os.path.getmtime(cache_file)) / 3600
        if max_age_hours is None or age_hours < max_age_hours:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("meta") == []:
                        try:
                            os.remove(cache_file)
                        except Exception:
                            pass
                        data = None
                    if data is not None:
                        with _MEM_CACHE_LOCK:
                            while len(_MEM_CACHE) >= _MEM_CACHE_MAX_ITEMS:
                                _MEM_CACHE.pop(next(iter(_MEM_CACHE)), None)
                            _MEM_CACHE[url] = (data, now)
                        return data
            except Exception as e:
                logging.debug(f"Cache corrupted, falling back to fetch: {e}")
                
    if cache_only:
        return None

    # Circuit breaker: fast-fail if host is known to be offline/down. Hosts are
    # keyed the way the provider-protection state keys them (lower-cased netloc)
    # so the breaker, the cooldowns and the polite fetch window all agree on
    # which host a URL belongs to.
    parsed_host = _get_provider_host(url)

    if parsed_host:
        last_fail = None
        with _OFFLINE_HOSTS_LOCK:
            if len(_OFFLINE_HOSTS) > 128:
                expired_hosts = [h for h, failed_at in _OFFLINE_HOSTS.items()
                                 if now - failed_at >= _OFFLINE_HOST_COOLDOWN]
                for expired_host in expired_hosts:
                    _OFFLINE_HOSTS.pop(expired_host, None)
                    _OFFLINE_HOST_FAIL_COUNT.pop(expired_host, None)
            last_fail = _OFFLINE_HOSTS.get(parsed_host)
        in_cooldown = last_fail is not None and (now - last_fail < _OFFLINE_HOST_COOLDOWN)
        if in_cooldown:
            # Read the cache outside the lock: file I/O must not stall every
            # other host's request behind this one.
            return _read_cached_response_json(cache_file)

    # 3. Fetch from network, but only through the per-host politeness window:
    # bounded concurrency per host, paced starts, and closed outright while the
    # host is cooling down or has just been marked offline. A refused window is
    # answered from disk -- stale rows are a delay, another burst at a host that
    # already said "slow down" is how a client gets banned for all its users.
    on_main_thread = threading.current_thread() is threading.main_thread()

    def _window_closed():
        # A cancelled batch must not queue behind requests it no longer needs,
        # and neither must a host whose breaker tripped while we waited.
        if is_cancelled and is_cancelled():
            return True
        return _is_host_marked_offline(parsed_host)

    with _acquire_catalog_window(
        parsed_host,
        abort_event=_CancelCheckAsEvent(_window_closed),
        may_wait=not on_main_thread,
    ) as host_slot_open:
        if not host_slot_open:
            logging.debug(f"Polite fetch window closed for {parsed_host or url}; serving the cached response")
            return _read_cached_response_json(cache_file)

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
                data_str = response.read().decode('utf-8')
            if not data_str or not data_str.strip():
                return None
            data = json.loads(data_str)
            if isinstance(data, dict) and data.get("meta") == []:
                return None

            # Host is healthy: remove from offline list
            if parsed_host:
                with _OFFLINE_HOSTS_LOCK:
                    _OFFLINE_HOSTS.pop(parsed_host, None)
                    _OFFLINE_HOST_FAIL_COUNT.pop(parsed_host, None)

            # Save to memory cache
            with _MEM_CACHE_LOCK:
                while len(_MEM_CACHE) >= _MEM_CACHE_MAX_ITEMS:
                    _MEM_CACHE.pop(next(iter(_MEM_CACHE)), None)
                _MEM_CACHE[url] = (data, now)

            # Save to disk cache atomically (temp file + rename)
            temp_file = None
            try:
                _prune_disk_cache_if_needed(now)
                temp_file = f"{cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.write(data_str)
                os.replace(temp_file, cache_file)
            except Exception:
                try:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception:
                    pass
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # The host set the back-off; honour it on this path too, not just
                # for streams, and cap it so one header cannot stall the catalog
                # for longer than half an hour.
                cooldown_secs = _retry_after_seconds(getattr(e, "headers", None),
                                                     default=_CATALOG_429_COOLDOWN, floor=30)
                set_provider_cooldown(url, duration_seconds=cooldown_secs,
                                      reason="HTTP 429 Too Many Requests")
                logging.warning(f"[Provider Protection] {parsed_host} returned HTTP 429 for a catalog request. Backing off {cooldown_secs}s.")
            elif parsed_host and e.code in [500, 502, 503, 504]:
                with _OFFLINE_HOSTS_LOCK:
                    cnt = _OFFLINE_HOST_FAIL_COUNT.get(parsed_host, 0) + 1
                    _remember_bounded_host_state(_OFFLINE_HOST_FAIL_COUNT, parsed_host, cnt)
                    if cnt >= _OFFLINE_HOST_MAX_FAILS:
                        _remember_bounded_host_state(_OFFLINE_HOSTS, parsed_host, time.time())
            logging.debug(f"HTTP Error {e.code} fetching from {url}")
            try:
                if hasattr(e, "close"):
                    e.close()
                if hasattr(e, "fp") and e.fp:
                    e.fp.close()
            except Exception:
                pass
        except urllib.error.URLError as e:
            # If the error is an SSL handshake failure, check whether the
            # ISP is poisoning DNS for this host and retry via DoH.
            reason_str = str(getattr(e, "reason", "")).lower()
            if parsed_host and ("unexpected_eof" in reason_str or "ssl" in reason_str):
                try:
                    if _is_dns_poisoned(parsed_host):
                        doh_body = _doh_fetch(url, timeout=timeout)
                        if doh_body:
                            data_str = doh_body.decode("utf-8")
                            if data_str and data_str.strip():
                                data = json.loads(data_str)
                                if not (isinstance(data, dict) and data.get("meta") == []):
                                    with _OFFLINE_HOSTS_LOCK:
                                        _OFFLINE_HOSTS.pop(parsed_host, None)
                                        _OFFLINE_HOST_FAIL_COUNT.pop(parsed_host, None)
                                    with _MEM_CACHE_LOCK:
                                        while len(_MEM_CACHE) >= _MEM_CACHE_MAX_ITEMS:
                                            _MEM_CACHE.pop(next(iter(_MEM_CACHE)), None)
                                        _MEM_CACHE[url] = (data, now)
                                    temp_file = None
                                    try:
                                        _prune_disk_cache_if_needed(now)
                                        temp_file = f"{cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                                        with open(temp_file, 'w', encoding='utf-8') as f:
                                            f.write(data_str)
                                        os.replace(temp_file, cache_file)
                                    except Exception:
                                        try:
                                            if temp_file and os.path.exists(temp_file):
                                                os.remove(temp_file)
                                        except Exception:
                                            pass
                                    return data
                except Exception:
                    pass
            if parsed_host:
                is_timeout = isinstance(e.reason, (socket.timeout, TimeoutError)) or "timed out" in str(e.reason).lower()
                with _OFFLINE_HOSTS_LOCK:
                    if is_timeout:
                        cnt = _OFFLINE_HOST_FAIL_COUNT.get(parsed_host, 0) + 1
                        _remember_bounded_host_state(_OFFLINE_HOST_FAIL_COUNT, parsed_host, cnt)
                        if cnt >= _OFFLINE_HOST_MAX_FAILS:
                            _remember_bounded_host_state(_OFFLINE_HOSTS, parsed_host, time.time())
                    else:
                        _remember_bounded_host_state(_OFFLINE_HOSTS, parsed_host, time.time())
            logging.debug(f"URL/SSL Error fetching from {url}: {e.reason}")
        except json.JSONDecodeError as e:
            logging.debug(f"JSON decode error from {url}: {e}")
        except Exception as e:
            if parsed_host:
                with _OFFLINE_HOSTS_LOCK:
                    cnt = _OFFLINE_HOST_FAIL_COUNT.get(parsed_host, 0) + 1
                    _remember_bounded_host_state(_OFFLINE_HOST_FAIL_COUNT, parsed_host, cnt)
                    if cnt >= _OFFLINE_HOST_MAX_FAILS:
                        _remember_bounded_host_state(_OFFLINE_HOSTS, parsed_host, time.time())
            logging.debug(f"Error fetching items from {url}: {e}")
        
    # Return stale cache if network fails. Do not put it into _MEM_CACHE:
    # the data may be arbitrarily old, and stamping it with `now` would make
    # every later caller treat it as fresh for a full max_age_hours window.
    return _read_cached_response_json(cache_file)


def _get_cached_request(url, max_age_hours=2, headers=None, cache_only=False, timeout=3.0, is_cancelled=None):
    """Fetch a URL once when concurrent callers miss the same cache entry."""
    if not url or cache_only:
        return _get_cached_request_uncollapsed(
            url, max_age_hours=max_age_hours, headers=headers,
            cache_only=cache_only, timeout=timeout, is_cancelled=is_cancelled,
        )

    now = time.time()
    with _MEM_CACHE_LOCK:
        cached = _MEM_CACHE.get(url)
        if cached and (max_age_hours is None or now - cached[1] < max_age_hours * 3600):
            return cached[0]

    with _REQUEST_INFLIGHT_LOCK:
        pending = _REQUEST_INFLIGHT.get(url)
        is_owner = pending is None
        if is_owner:
            pending = {"event": threading.Event(), "result": None}
            _REQUEST_INFLIGHT[url] = pending

    if not is_owner:
        wait_deadline = time.time() + max(0.1, timeout) + 1.0
        while not pending["event"].is_set():
            if is_cancelled and is_cancelled():
                return None
            remaining = wait_deadline - time.time()
            if remaining <= 0:
                with _MEM_CACHE_LOCK:
                    cached = _MEM_CACHE.get(url)
                if cached and (max_age_hours is None or time.time() - cached[1] < max_age_hours * 3600):
                    return cached[0]
                return None
            pending["event"].wait(timeout=min(0.2, remaining))
        return pending["result"]

    result = None
    try:
        if is_cancelled and is_cancelled():
            return None
        result = _get_cached_request_uncollapsed(
            url, max_age_hours=max_age_hours, headers=headers,
            cache_only=False, timeout=timeout, is_cancelled=is_cancelled,
        )
        return result
    finally:
        with _REQUEST_INFLIGHT_LOCK:
            pending["result"] = result
            if _REQUEST_INFLIGHT.get(url) is pending:
                del _REQUEST_INFLIGHT[url]
            pending["event"].set()

def fetch_genre_counts(media_type="movie"):
    return {}

def is_type_match(type1, type2):
    if not type1 or not type2:
        return False
    t1 = str(type1).lower().strip()
    t2 = str(type2).lower().strip()
    if t1 == t2:
        return True
    if t1 in ("movie_series", "movies_series", "movie+series", "movies+series"):
        return is_type_match(t2, "movie") or is_type_match(t2, "series")
    if t2 in ("movie_series", "movies_series", "movie+series", "movies+series"):
        return is_type_match(t1, "movie") or is_type_match(t1, "series")
    movie_group = {"movie", "movies", "film", "films"}
    if t1 in movie_group and t2 in movie_group:
        return True
    series_group = {"series", "tvshow", "tv_series", "tvshows"}
    if t1 in series_group and t2 in series_group:
        return True
    tv_group = {"tv", "channel", "tvchannel", "channels"}
    if t1 in tv_group and t2 in tv_group:
        return True
    music_group = {"music", "radio"}
    if t1 in music_group and t2 in music_group:
        return True
    return False

def is_catalog_type_match(cat, media_type, addon_name=""):
    """
    Check if a catalog object matches the requested media_type ('movie', 'series', 'anime', 'tv', etc.).
    Supports addons with custom catalog types (e.g. 'HdHub', 'BollywoodX') where the catalog name or id
    identifies the media category.
    """
    if not cat or not media_type or media_type in ("all", "all_types"):
        return True
    tgt = str(media_type).strip().lower()
    if tgt in ("movie_series", "movies_series", "movie+series", "movies+series"):
        return is_catalog_type_match(cat, "movie", addon_name) or is_catalog_type_match(cat, "series", addon_name)
    c_type = str(cat.get("type") or "").strip().lower()
    if is_type_match(c_type, tgt):
        return True

    c_id = str(cat.get("id") or "").strip().lower()
    c_name = str(cat.get("name") or c_id).strip().lower()
    a_name = str(addon_name or "").strip().lower()

    if tgt == "anime":
        return "anime" in c_type or "anime" in c_name or "anime" in c_id or "anime" in a_name
    elif tgt == "movie":
        if is_type_match(c_type, "series") or is_type_match(c_type, "tv"):
            return False
        return "movie" in c_type or "movie" in c_name or "movie" in c_id or "film" in c_name or "film" in c_id
    elif tgt == "series":
        if is_type_match(c_type, "movie"):
            return False
        # A catalog declared as `tv` is a live TV/channel catalog, not a TV
        # series. Only explicit series metadata or labels belong here.
        if is_type_match(c_type, "tv"):
            return False
        return "series" in c_type or "series" in c_name or "series" in c_id
    elif tgt in ("tv", "channel"):
        return is_type_match(c_type, "tv") or "tv" in c_name or "channel" in c_name or "iptv" in a_name

    return False

def is_item_type_compatible(item, expected_type, cached_meta=None):
    """
    Check if an item matches the expected media type ('movie', 'series', 'anime', etc.).
    Strictly ensures TV series do not leak into movies, and movies do not leak into series.
    """
    if not item or not expected_type or expected_type in ("all", "all_types"):
        return True

    expected = str(expected_type).strip().lower()
    if expected in ("movie_series", "movies_series", "movie+series", "movies+series"):
        itype = item.get("type") or item.get("media_type")
        if not itype and cached_meta:
            itype = cached_meta.get("type") or cached_meta.get("media_type")
        if not itype:
            if item.get("seriesInfo") or item.get("seasons") or item.get("episode"):
                itype = "series"
        if not itype:
            return True
        itype = str(itype).strip().lower()
        if itype in ("movie", "movies", "film", "films", "series", "tvshow", "tv_series", "tvshows"):
            return True
        if itype in ("tv", "channel", "tvchannel", "channels", "live", "music", "radio", "podcast"):
            return False
        return is_type_match(itype, "movie") or is_type_match(itype, "series")
    if expected in ("collections", "other", "catalog", "channels", "channel", "live", "tv", "music", "radio", "podcast"):
        return True

    itype = item.get("type") or item.get("media_type")
    if not itype and cached_meta:
        itype = cached_meta.get("type") or cached_meta.get("media_type")

    if not itype:
        from . import database
        item_id = item.get("id") or item.get("imdb_id")
        if item_id:
            cm = cached_meta or database.get_cached_metadata(item_id)
            if cm:
                itype = cm.get("type") or cm.get("media_type")
                if not itype:
                    if cm.get("seriesInfo") or cm.get("seasons"):
                        itype = "series"
                    elif cm.get("videos"):
                        vids = cm.get("videos")
                        if isinstance(vids, list) and any(isinstance(v, dict) and (v.get("season") or v.get("episode")) for v in vids):
                            itype = "series"

    if not itype:
        if item.get("seriesInfo") or item.get("seasons") or item.get("episode"):
            itype = "series"

    if not itype:
        return True

    itype = str(itype).strip().lower()

    if expected == "movie":
        if itype in ("series", "tvshow", "tv_series", "tvshows"):
            return False
        return True
    elif expected == "series":
        if itype in ("movie", "movies", "film", "films"):
            return False
        return True
    elif expected == "anime":
        return True
    elif expected not in ("movie", "series", "anime", "tv", "channel"):
        return True

    return is_type_match(itype, expected)

ADULT_GENRES_TAGS = {
    "adult", "erotica", "erotic", "ecchi", "hentai", "softcore", "pinku eiga",
    "pink film", "sex", "sexuality", "sexual", "porn", "porno", "pornography", "pornographic",
    "nudity", "explicit nudity", "nsfw", "sploitation", "bdsm", "sm", "r18", "r-18",
    "blowjob", "brazzers", "creampie", "cum in mouth", "deep throat",
    "doggystyle", "face sitting", "facial", "gangbang", "handjob",
    "masturbation", "milf", "dilf", "pov", "squirt", "threesome", "foursome", "big dick",
    "bubble butt", "cowgirl", "mofos", "missionary", "piss", "pussy licking",
    "pussy fingering", "pervert", "perverted", "sukebe", "jav", "uncensored"
}

ADULT_RATINGS = {
    "18", "18+", "R18", "R-18", "R18+", "NC-17", "XXX", "XX", "X",
    "RX", "AV", "ADULT", "PORN", "R21", "OVER 18", "A", "AO", "FSK 18", "18A",
    "UNRATED (ADULT)", "NOT RATED (ADULT)", "RESTRICTED 18"
}

ADULT_KEYWORD_REGEX = re.compile(
    r'\b('
    r'18\+|xxx+|hentai|ecchi|erotica?|erotism|softcore|pinku\s+eiga|pink\s+film|'
    r'sex\s+(?:video|tape|movie|film|scenes|act|zanmai)|sexual|sexuality|'
    r'jav|adult\s+(?:movie|film|video|content|only)|nude|nudity|naked|topless|bare\s+breast|uncensored|'
    r'hatsuj[ôo]|dirty\s+(?:couple|movie)|horny\s+couple|groper(?:\s+train)?|'
    r'futariecch?i|incha\s+couple|blowjob|brazzers|bangbros|creampie|cum|cumshot|'
    r'deepthroat|deep\s+throat|doggystyle|masturbat(?:ion|e)|milf|dilf|pov|squirt(?:ing)?|'
    r'threesome|foursome|pussy|cock|penis|vagina|anal\s+sex|tits|boobs|'
    r'pervert(?:ed|s|ion)?|porno?(?:graphy|graphic|s)?|sukebe|chikan|shishunki\s+sex|'
    r'swinger[s]?|playboy|penthouse|hustler|mofos|gangbang|orgy|striptease|stripper|'
    r'fetish|voyeur(?:ism)?|bdsm|trimmed\s+bush|hairy\s+bush|rape|'
    r'asses?|deep\s+inside|pure\s+evil|pure\s+pink|bikini\s+car\s+wash|sexploitation'
    r')\b',
    re.IGNORECASE
)

def is_adult_item(item, check_cache=True):
    if not item or not isinstance(item, dict):
        return False

    if item.get("adult") is True or item.get("isAdult") is True:
        return True

    rating = str(item.get("certification") or item.get("contentRating") or item.get("rating") or item.get("mpaa") or "").strip().upper()
    if rating in ADULT_RATINGS or any(r in rating for r in ["R18", "NC-17", "XXX", "18+", "ADULT", "PORN"]):
        return True

    genres = item.get("genres") or item.get("genre") or []
    if isinstance(genres, str):
        genre_list = [g.strip().lower() for g in genres.split(",")]
    elif isinstance(genres, list):
        genre_list = [str(g).strip().lower() for g in genres]
    else:
        genre_list = []

    for g in genre_list:
        if g in ADULT_GENRES_TAGS:
            return True

    title = str(item.get("name") or item.get("title") or "")
    desc = str(item.get("description") or item.get("overview") or "")

    if ADULT_KEYWORD_REGEX.search(title) or ADULT_KEYWORD_REGEX.search(desc):
        return True

    genres_str = " ".join(genre_list)
    if ADULT_KEYWORD_REGEX.search(genres_str):
        return True

    # Fast check of local SQLite metadata cache for already resolved adult flags/metadata
    if check_cache:
        item_id = item.get("id") or item.get("imdb_id")
        if item_id:
            try:
                cached = database.get_cached_metadata(item_id)
                if cached and isinstance(cached, dict):
                    return is_adult_item(cached, check_cache=False)
            except Exception:
                pass

    return False


# Session-level addon circuit breaker.
# Tracks consecutive timeout/error failures per manifest URL for this app session.
# An addon is skipped once it has failed _ADDON_FAIL_THRESHOLD times in a row.
# Status is NOT persisted to disk and resets on app restart.
_ADDON_SESSION_FAILURES = {}   # manifest_url -> consecutive_fail_count
_ADDON_SESSION_BLOCKED = set() # manifest_urls blocked for this session
_ADDON_ONLINE_LOCK = threading.Lock()
# Also keep the legacy dict for the addon manager UI compatibility
_ADDON_ONLINE_STATUS = {}

_ADDON_FAIL_THRESHOLD = 5  # Block after this many consecutive timeout/connection errors

def set_addon_online_status(manifest_url, is_online):
    """Called by addon manager UI to override session status."""
    if not manifest_url:
        return
    with _ADDON_ONLINE_LOCK:
        _ADDON_ONLINE_STATUS[manifest_url] = is_online
        if is_online:
            # UI explicitly says online — clear session block
            _ADDON_SESSION_BLOCKED.discard(manifest_url)
            _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        else:
            _ADDON_SESSION_BLOCKED.add(manifest_url)
    invalidate_catalogs_cache()

def _record_addon_failure(manifest_url):
    """Record a network failure for an addon. Blocks it after threshold failures."""
    if not manifest_url or manifest_url.startswith("builtin:"):
        return
    newly_blocked = False
    with _ADDON_ONLINE_LOCK:
        count = _ADDON_SESSION_FAILURES.get(manifest_url, 0) + 1
        _ADDON_SESSION_FAILURES[manifest_url] = count
        if count >= _ADDON_FAIL_THRESHOLD:
            newly_blocked = manifest_url not in _ADDON_SESSION_BLOCKED
            _ADDON_SESSION_BLOCKED.add(manifest_url)
            if newly_blocked:
                logging.info(f"[Addon] Blocked for this session after {count} failures: {manifest_url}")
    if newly_blocked:
        invalidate_catalogs_cache()

def _record_addon_success(manifest_url):
    """Clear failure count on a successful addon response."""
    if not manifest_url or manifest_url.startswith("builtin:"):
        return
    with _ADDON_ONLINE_LOCK:
        _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        _ADDON_SESSION_BLOCKED.discard(manifest_url)

def reset_addon_session_status(manifest_url=None):
    """Reset session block for a specific addon (or all addons) — called on manual reload."""
    with _ADDON_ONLINE_LOCK:
        if manifest_url:
            _ADDON_SESSION_BLOCKED.discard(manifest_url)
            _ADDON_SESSION_FAILURES.pop(manifest_url, None)
        else:
            _ADDON_SESSION_BLOCKED.clear()
            _ADDON_SESSION_FAILURES.clear()
    invalidate_catalogs_cache()

_IN_MEMORY_ADDON_HEALTH = {}
_IN_MEMORY_HEALTH_INIT = False

def reset_addon_health_cache():
    """Reset in-memory addon health and status cache."""
    global _IN_MEMORY_HEALTH_INIT
    with _ADDON_ONLINE_LOCK:
        _IN_MEMORY_ADDON_HEALTH.clear()
        _IN_MEMORY_HEALTH_INIT = False
        _ADDON_ONLINE_STATUS.clear()
        _ADDON_SESSION_BLOCKED.clear()
        _ADDON_SESSION_FAILURES.clear()
    invalidate_catalogs_cache()


def _init_in_memory_addon_health():
    global _IN_MEMORY_ADDON_HEALTH, _IN_MEMORY_HEALTH_INIT
    try:
        from . import database
        health = database.get_addon_health()
        if isinstance(health, dict):
            with _ADDON_ONLINE_LOCK:
                _IN_MEMORY_ADDON_HEALTH.update(health)
                _IN_MEMORY_HEALTH_INIT = True
    except Exception:
        pass

def is_addon_online(manifest_url):
    if not manifest_url or manifest_url.startswith("builtin:"):
        return True
    with _ADDON_ONLINE_LOCK:
        # Check explicit UI override first
        if manifest_url in _ADDON_ONLINE_STATUS and not _ADDON_ONLINE_STATUS[manifest_url]:
            return False
        # Check session circuit breaker
        if manifest_url in _ADDON_SESSION_BLOCKED:
            return False
        # Check in-memory health cache
        entry = _IN_MEMORY_ADDON_HEALTH.get(manifest_url)
        if entry is not None:
            if not entry.get("online", True):
                last_checked = float(entry.get("last_checked", 0))
                if time.time() - last_checked < 7200:
                    return False
            return True

    # If not in cache yet, load from database once and update in-memory cache
    try:
        from . import database
        health = database.get_addon_health() or {}
        with _ADDON_ONLINE_LOCK:
            _IN_MEMORY_ADDON_HEALTH.update(health)
            entry = _IN_MEMORY_ADDON_HEALTH.get(manifest_url)
            if entry and not entry.get("online", True):
                last_checked = float(entry.get("last_checked", 0))
                if time.time() - last_checked < 7200:
                    return False
    except Exception:
        pass

    return True

def check_addon_manifest_online(manifest_url, timeout=3.0):
    """Return True if the addon manifest is reachable, reusing the URL cache.

    Opening the addons page used to issue a fresh request per addon every
    time; a cached manifest is just as good for the status indicator.
    """
    if not manifest_url or manifest_url.startswith("builtin:"):
        return True
    try:
        data = _get_cached_request(manifest_url, max_age_hours=24, timeout=timeout)
        return isinstance(data, dict) and bool(data.get("id"))
    except Exception:
        return False

def check_addons_health_background(callback=None):
    """
    Check connectivity of all enabled remote addons in a background daemon thread.
    Updates addon health cache and session blocked sets.
    Calls callback(results) upon completion on GLib main thread.
    """
    def _worker():
        from . import database
        addons = [a for a in database.get_addons() if a.get("enabled", True)]
        remote_addons = [a for a in addons if a.get("manifest_url") and not a.get("manifest_url", "").startswith("builtin:")]
        if not remote_addons:
            if callback:
                try:
                    import gi
                    gi.require_version("GLib", "2.0")
                    from gi.repository import GLib
                    GLib.idle_add(callback, {})
                except Exception:
                    callback({})
            return

        import concurrent.futures
        results = {}

        def _ping_addon(addon):
            url = addon.get("manifest_url", "")
            t_start = time.time()
            try:
                manifest_data = _get_cached_request(url, max_age_hours=24, timeout=2.5)
                elapsed_ms = int((time.time() - t_start) * 1000)
                if manifest_data and isinstance(manifest_data, dict) and manifest_data.get("id"):
                    # Success: verify if manifest has catalogs or resources to sync
                    if manifest_data.get("catalogs") and not addon.get("catalogs"):
                        addon["catalogs"] = manifest_data["catalogs"]
                        try: database.update_addon_catalogs(url, manifest_data["catalogs"])
                        except Exception: pass
                    if manifest_data.get("resources") and not addon.get("resources"):
                        addon["resources"] = manifest_data["resources"]
                        try: database.add_addon(addon, sync_to_remote=False)
                        except Exception: pass
                    return (url, True, elapsed_ms, None)
                else:
                    return (url, False, elapsed_ms, "Invalid manifest JSON")
            except Exception as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                return (url, False, elapsed_ms, str(e))

        # Keep the startup burst small: addons often share a host, and 20
        # parallel manifest fetches can trip their rate limits.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(remote_addons))) as pool:
            futures = [pool.submit(_ping_addon, a) for a in remote_addons]
            for f in concurrent.futures.as_completed(futures):
                try:
                    url, ok, latency, err = f.result()
                    results[url] = {
                        "online": ok,
                        "last_checked": time.time(),
                        "latency_ms": latency,
                        "error": err
                    }
                    if not ok:
                        _record_addon_failure(url)
                    else:
                        _record_addon_success(url)
                except Exception:
                    pass

        with _ADDON_ONLINE_LOCK:
            _IN_MEMORY_ADDON_HEALTH.update(results)

        # Persist health results in database
        try:
            database.set_all_addons_health(results)
        except Exception as e:
            logging.debug(f"Failed to persist addon health: {e}")

        if callback:
            try:
                import gi
                gi.require_version("GLib", "2.0")
                from gi.repository import GLib
                GLib.idle_add(callback, results)
            except Exception:
                callback(results)

    thread = threading.Thread(target=_worker, daemon=True, name="AddonHealthChecker")
    thread.start()
    return thread

def get_session_blocked_addons():
    """Return set of manifest URLs blocked for this session (for UI display)."""
    with _ADDON_ONLINE_LOCK:
        return set(_ADDON_SESSION_BLOCKED)

def get_addon_catalogs(addon, cache_only=False):
    """Return the catalogs for an addon. If catalogs is empty, attempts to fetch
    from cached/remote manifest and persists to database."""
    if not addon or not isinstance(addon, dict):
        return []
    if not addon.get("catalogs_enabled", True):
        return []
    catalogs = addon.get("catalogs")
    if catalogs:
        return catalogs
        
    m_url = addon.get("manifest_url", "")
    if not m_url or m_url.startswith("builtin:"):
        return []
        
    try:
        manifest_data = _get_cached_request(m_url, max_age_hours=168, cache_only=cache_only, timeout=3.5)
        if manifest_data and isinstance(manifest_data.get("catalogs"), list) and manifest_data["catalogs"]:
            found_cats = manifest_data["catalogs"]
            addon["catalogs"] = found_cats
            try:
                from . import database
                database.update_addon_catalogs(m_url, found_cats)
            except Exception:
                pass
            return found_cats
    except Exception:
        pass
        
    return []

def get_addon_directory_catalogs():
    """Return list of available addon directory catalog sources."""
    sources = [
        {
            "id": "community",
            "name": "Community Addons",
            "url": "https://v3-cinemeta.strem.io/addon_catalog/all/community.json",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json"
        },
        {
            "id": "official",
            "name": "Official Addons",
            "url": "https://v3-cinemeta.strem.io/addon_catalog/all/official.json",
            "manifest_url": "https://v3-cinemeta.strem.io/manifest.json"
        },
        {
            "id": "stremio-addons.net",
            "name": "STREMIO-ADDONS.NET",
            "url": "https://stremio-addons.net/api/addon_catalog/all/stremio-addons.net.json",
            "manifest_url": "https://stremio-addons.net/api/manifest.json"
        }
    ]

    try:
        from . import database
        installed_addons = database.get_addons()
    except Exception:
        installed_addons = []

    installed_manifest_urls = {a.get("manifest_url") for a in installed_addons if a.get("manifest_url")}
    installed_ids = {a.get("id") for a in installed_addons if a.get("id")}

    seen_ids = {s["id"] for s in sources}
    for addon in installed_addons:
        addon_cats = addon.get("addonCatalogs") or []
        m_url = addon.get("manifest_url", "")
        if not addon_cats and "addon_catalog" in addon.get("resources", []):
            addon_cats = [{"type": "all", "id": addon.get("id"), "name": addon.get("name")}]

        for cat in addon_cats:
            cat_id = cat.get("id") or addon.get("id")
            if cat_id in seen_ids:
                continue
            seen_ids.add(cat_id)
            c_type = cat.get("type", "all")
            c_name = cat.get("name") or addon.get("name") or cat_id

            base = m_url.rsplit("/manifest.json", 1)[0] if "/manifest.json" in m_url else m_url.rstrip("/")
            cat_url = f"{base}/addon_catalog/{c_type}/{cat_id}.json"
            sources.append({
                "id": cat_id,
                "name": c_name,
                "url": cat_url,
                "manifest_url": m_url,
                "installed": True
            })

    for s in sources:
        m_url = s.get("manifest_url")
        s_id = s.get("id")
        if m_url in installed_manifest_urls or s_id in installed_ids:
            s["installed"] = True

    sources.append({
        "id": "installed",
        "name": "Installed",
        "is_installed_view": True
    })
    return sources

def fetch_addon_directory_catalog(catalog_url, cache_only=False):
    """Fetch and parse an addon directory catalog (returns list of normalized addon item dicts)."""
    if not catalog_url:
        return []
    try:
        data = _get_cached_request(catalog_url, max_age_hours=12, cache_only=cache_only, timeout=8.0)
        if not data and not cache_only:
            try:
                import urllib.request, json
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'application/json'
                }
                req = urllib.request.Request(catalog_url, headers=headers)
                with urllib.request.urlopen(req, timeout=8.0, context=_SSL_CONTEXT) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
            except Exception:
                pass
        if not data or not isinstance(data, dict):
            return []
        addons = data.get("addons", [])
        results = []
        for item in addons:
            if not isinstance(item, dict):
                continue
            manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
            transport_url = item.get("transportUrl") or manifest.get("manifest_url") or item.get("manifest_url")
            if not transport_url:
                continue
            addon_id = manifest.get("id") or item.get("id")
            name = manifest.get("name") or addon_id or "Addon"
            version = manifest.get("version", "1.0.0")
            description = manifest.get("description", "")
            types = manifest.get("types") or []
            logo = manifest.get("logo") or manifest.get("icon")
            bh = manifest.get("behaviorHints") or {}
            configurable = bh.get("configurable", False) or "/configure" in transport_url
            configuration_required = bh.get("configurationRequired", False)

            results.append({
                "id": addon_id,
                "name": name,
                "version": version,
                "description": description,
                "types": types,
                "logo": logo,
                "transport_url": transport_url,
                "configurable": configurable,
                "configuration_required": configuration_required,
                "manifest": manifest
            })
        return results
    except Exception as e:
        logging.error(f"Error fetching addon directory catalog {catalog_url}: {e}")
        return []


def addon_has_resource(addon, resource_name, media_type=None, item_id=None):
    """
    Check if an addon supports a given resource ('catalog', 'meta', 'stream', 'subtitles'),
    optionally checking for compatibility with media_type ('movie', 'series', 'anime', 'tv', etc.)
    and item_id (e.g. 'tt...', 'kitsu:...', 'tmdb:...', 'dsf:...', 'iptv:...').
    """
    if not isinstance(addon, dict):
        return False
    if not addon.get("enabled", True):
        return False
    if media_type and str(media_type).strip().lower() in ("movie_series", "movies_series", "movie+series", "movies+series"):
        return addon_has_resource(addon, resource_name, media_type="movie", item_id=item_id) or addon_has_resource(addon, resource_name, media_type="series", item_id=item_id)
        
    manifest_url = addon.get("manifest_url", "")
    if manifest_url and not is_addon_online(manifest_url):
        return False
        
    addon_id = str(addon.get("id", "")).lower()
    
    # Specific known built-in / default addon behavior overrides
    if addon_id == "cinemeta" or "cinemeta" in manifest_url.lower():
        if resource_name in ["catalog", "meta"]:
            if media_type and not is_type_match(media_type, "movie") and not is_type_match(media_type, "series"):
                return False
            if item_id and not str(item_id).startswith("tt"):
                return False
            return True
        return False  # Cinemeta does NOT provide streams or subtitles
        
    if addon_id == "anime-kitsu" or "anime-kitsu" in manifest_url.lower():
        if resource_name in ["catalog", "meta"]:
            if media_type and not (is_type_match(media_type, "anime") or is_type_match(media_type, "series") or is_type_match(media_type, "movie")):
                return False
            if item_id and resource_name == "meta" and not str(item_id).startswith("kitsu:"):
                return False
            return True
        return False  # Anime Kitsu does NOT provide streams or subtitles

    if addon_id == "local.iptv-org" or "iptv-org" in manifest_url.lower():
        if media_type and not is_type_match(media_type, "tv"):
            return False
        return True

    # Check resources field in manifest
    resources = addon.get("resources")

    # Addons that only declare "addon_catalog" are addon-directory providers (e.g. stremio-addons.net).
    # They must NEVER be treated as having catalog/meta/stream resources.
    if resources is not None:
        resource_names = set(
            (r if isinstance(r, str) else r.get("name", "")).lower()
            for r in resources
        )
        if resource_names and resource_names <= {"addon_catalog"}:
            return False

    # If resources is missing or None, infer based on catalogs or defaults
    if resources is None:
        if resource_name == "catalog" and get_addon_catalogs(addon, cache_only=True):
            resources = ["catalog"]
        elif resource_name == "stream":
            resources = ["stream"]
        else:
            resources = ["stream", "meta", "catalog"]

    has_res = False
    for r in resources:
        if isinstance(r, str):
            if r.lower() == resource_name.lower():
                has_res = True
                break
        elif isinstance(r, dict):
            if str(r.get("name", "")).lower() == resource_name.lower():
                # Check resource-level types
                r_types = r.get("types")
                if r_types is not None and media_type:
                    type_ok = any(is_type_match(t, media_type) for t in r_types)
                    if not type_ok and media_type == "anime":
                        type_ok = any("anime" in str(t).lower() for t in r_types)
                    if not type_ok:
                        continue  # This resource entry does not match media_type
                        
                # Check resource-level idPrefixes
                r_prefixes = r.get("idPrefixes")
                if r_prefixes is not None and item_id:
                    if not any(str(item_id).startswith(p) for p in r_prefixes):
                        continue  # This resource entry does not match item_id prefix
                        
                has_res = True
                break

    if not has_res:
        return False

    # Check top-level addon types
    addon_types = addon.get("types")
    if addon_types is not None and media_type:
        type_match = any(is_type_match(t, media_type) for t in addon_types)
        if not type_match and media_type == "anime":
            type_match = any("anime" in str(t).lower() for t in addon_types) or any("anime" in str(c.get("type", "")).lower() for c in get_addon_catalogs(addon, cache_only=True))
        if not type_match:
            # Fallback check catalogs if catalog resource
            if resource_name == "catalog":
                type_match = any(is_type_match(c.get("type"), media_type) for c in get_addon_catalogs(addon, cache_only=True))
            if not type_match:
                return False

    # Check top-level addon idPrefixes
    addon_prefixes = addon.get("idPrefixes")
    if addon_prefixes is not None and item_id:
        if not any(str(item_id).startswith(p) for p in addon_prefixes):
            return False

    return True

def has_meta_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports metadata ('meta') resource for this item."""
    return addon_has_resource(addon, "meta", media_type=media_type, item_id=item_id)

def has_stream_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports stream ('stream') resource for this item."""
    if not addon.get("streams_enabled", True):
        return False
    return addon_has_resource(addon, "stream", media_type=media_type, item_id=item_id)

def is_stream_provider(addon, media_type=None, item_id=None):
    """
    Return True if the addon provides media playback streams (direct http or torrent).
    Excludes catalog-only addons (Cinemeta, TMDB Catalogs, CyberFlix, etc.) and non-stream addons.
    """
    if not isinstance(addon, dict) or not addon.get("enabled", True) or not addon.get("streams_enabled", True):
        return False
    manifest_url = addon.get("manifest_url", "")
    if manifest_url.startswith("builtin:"):
        return False
    addon_id = str(addon.get("id", "")).lower()
    # Explicit exclusions for known catalog-only addons
    if addon_id in ["cinemeta", "anime-kitsu", "tmdb-addon", "com.linvo.cinemeta"] or "cinemeta" in manifest_url.lower():
        return False
    if "tmdb" in addon_id and "catalog" in addon_id:
        return False
    return has_stream_resource(addon, media_type=media_type, item_id=item_id)

def is_catalog_provider(addon, media_type=None):
    """
    Return True if the addon provides catalogs for browsing.
    Must have 'catalog' resource and at least one browsable catalog.
    Media providers (stream-only addons without catalogs) return False.
    """
    if not isinstance(addon, dict) or not addon.get("enabled", True) or not addon.get("catalogs_enabled", True):
        return False
    if not has_catalog_resource(addon, media_type=media_type):
        return False
    catalogs = get_addon_catalogs(addon, cache_only=True)
    if not catalogs:
        return False
    if media_type:
        addon_name = addon.get("name", "")
        if any(is_catalog_type_match(c, media_type, addon_name) for c in catalogs):
            return True
        addon_types = addon.get("types") or []
        if any(is_type_match(t, media_type) for t in addon_types):
            return True
        return False
    return True

def is_search_provider(addon, media_type=None):
    """
    Return True if the addon provides catalogs that support keyword search.
    Pure media providers (torrent/direct stream without search catalog) return False.
    """
    if not isinstance(addon, dict) or not addon.get("enabled", True) or not addon.get("catalogs_enabled", True):
        return False
    if not has_catalog_resource(addon, media_type=media_type):
        return False
    c_type = "series" if media_type == "series" else (media_type or "movie")
    search_cats = _get_search_catalogs_for_addon(addon, c_type, cache_only=True)
    return len(search_cats) > 0

def get_catalog_addons(media_type=None, only_working=True):
    """Return enabled addons that provide browsable catalogs."""
    from . import database
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    return [
        a for a in addons 
        if is_catalog_provider(a, media_type=media_type)
        and (not only_working or is_addon_online(a.get("manifest_url", "")))
    ]

def get_search_addons(media_type=None, only_working=True):
    """Return enabled addons that provide search catalogs."""
    from . import database
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    return [
        a for a in addons 
        if is_search_provider(a, media_type=media_type)
        and (not only_working or is_addon_online(a.get("manifest_url", "")))
    ]

def get_stream_addons(media_type=None, item_id=None, only_working=True):
    """Return enabled media providers that provide streams/torrents for this item."""
    from . import database
    addons = [a for a in database.get_addons() if a.get("enabled", True) and a.get("streams_enabled", True)]
    return [
        a for a in addons 
        if not a.get("manifest_url", "").startswith("builtin://") 
        and is_stream_provider(a, media_type=media_type, item_id=item_id)
        and (not only_working or is_addon_online(a.get("manifest_url", "")))
    ]

def has_stream_addons(media_type=None, item_id=None, only_working=True):
    """Return True if there is at least one enabled stream/torrent addon for this media type and item."""
    return len(get_stream_addons(media_type=media_type, item_id=item_id, only_working=only_working)) > 0

def addon_declares_resource(addon, resource_name):
    """Check an addon's declared resources without health or toggle gating.

    The addon manager must keep showing a stream/catalog toggle for addons that
    are disabled or temporarily offline, otherwise their capabilities vanish
    from the row and cannot be re-enabled.
    """
    if not isinstance(addon, dict):
        return False
    resource_name = str(resource_name or "").lower()
    resources = addon.get("resources")
    if resources is None:
        if resource_name == "catalog":
            return bool(addon.get("catalogs"))
        return resource_name in ("stream", "meta")
    declared = set()
    for resource in resources:
        if isinstance(resource, str):
            declared.add(resource.lower())
        elif isinstance(resource, dict):
            declared.add(str(resource.get("name", "")).lower())
    # Addon-directory entries expose only "addon_catalog" and are not media addons.
    if declared and declared <= {"addon_catalog"}:
        return False
    return resource_name in declared

def probe_addon_manifest_online(manifest_url, timeout=4.0):
    """Live manifest probe used by the addon manager's explicit status check.

    Unlike check_addon_manifest_online(), this never answers from the manifest
    cache: a stale entry made dead providers look healthy and cleared their
    session block.
    """
    if not manifest_url or manifest_url.startswith("builtin:"):
        return True
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        request = urllib.request.Request(manifest_url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return isinstance(data, dict) and bool(data.get("id"))
    except Exception as exc:
        logging.debug(f"[Addon] Live manifest probe failed for {manifest_url}: {exc}")
        return False

def has_catalog_resource(addon, media_type=None):
    """Return True if the addon supports catalog ('catalog') resource for this media type."""
    if not addon.get("catalogs_enabled", True):
        return False
    return addon_has_resource(addon, "catalog", media_type=media_type)

def has_subtitles_resource(addon, media_type=None, item_id=None):
    """Return True if the addon supports subtitles ('subtitles') resource for this item."""
    return addon_has_resource(addon, "subtitles", media_type=media_type, item_id=item_id)

def is_catalog_browsable(cat):
    """
    Return True if the catalog can be browsed directly without search or other required parameters.
    Catalogs with isRequired=True for 'search' (or in extraRequired) are search-only catalogs.
    """
    if not isinstance(cat, dict):
        return False
    extra_req = cat.get("extraRequired") or []
    if "search" in extra_req:
        return False
    extra = cat.get("extra") or []
    for ex in extra:
        if isinstance(ex, dict):
            name = ex.get("name", "")
            is_req = ex.get("isRequired", False)
            if is_req:
                if name == "search":
                    return False
                if name != "genre" and not ex.get("options"):
                    return False
    return True

def does_genre_match_filter(candidate_genre, target_genre):
    """
    Checks if a candidate genre (e.g. from catalog options or item genres)
    matches the target filter genre, supporting aliases, numeric IDs, and compound genres like 'Action & Adventure'.
    """
    if not candidate_genre or not target_genre:
        return False

    cand_str = str(candidate_genre).strip().lower()
    tgt = str(target_genre).strip().lower()

    # Translate numeric TMDB genre IDs if candidate is numeric
    if cand_str.isdigit():
        gid = int(cand_str)
        if gid in TMDB_GENRES:
            cand_str = TMDB_GENRES[gid].lower()
            if gid == 10759 and tgt in ("action", "adventure"):
                return True
            elif gid == 10765 and tgt in ("sci-fi", "science fiction", "fantasy"):
                return True
            elif gid == 10762 and tgt in ("kids", "family"):
                return True
            elif gid == 10768 and tgt in ("war", "politics"):
                return True

    if cand_str == tgt:
        return True

    # Language code and name mapping
    from . import database
    if database.is_language_genre(tgt):
        tgt_code = database.get_language_code(tgt)
        cand_code = database.get_language_code(cand_str)
        if tgt_code and cand_code and tgt_code == cand_code:
            return True
        if tgt_code and cand_str == tgt_code:
            return True
        if cand_code and tgt == cand_code:
            return True

    # Compound and alias mappings
    if tgt in ("action & adventure", "action and adventure"):
        return "action" in cand_str or "adventure" in cand_str
    if tgt in ("sci-fi & fantasy", "sci-fi and fantasy", "science fiction & fantasy"):
        return "sci-fi" in cand_str or "science fiction" in cand_str or "fantasy" in cand_str
    if tgt in ("war & politics", "war and politics"):
        return "war" in cand_str or "politics" in cand_str
    if cand_str in ("action & adventure", "action and adventure"):
        return "action" in tgt or "adventure" in tgt
    if cand_str in ("sci-fi & fantasy", "sci-fi and fantasy", "science fiction & fantasy"):
        return "sci-fi" in tgt or "science fiction" in tgt or "fantasy" in tgt
    if cand_str in ("war & politics", "war and politics"):
        return "war" in tgt or "politics" in tgt
    if tgt == "action":
        return "action" in cand_str
    if tgt == "adventure":
        return "adventure" in cand_str
    if tgt in ("animation", "anime"):
        return "animation" in cand_str or "anime" in cand_str or "animated" in cand_str
    if tgt in ("sci-fi", "science fiction"):
        return "sci-fi" in cand_str or "science fiction" in cand_str
    if tgt in ("reality", "reality-tv"):
        return "reality" in cand_str
    if tgt in ("talk", "talk-show"):
        return "talk" in cand_str
    if tgt in ("tv movie", "tv-movie"):
        return "tv movie" in cand_str or "tv-movie" in cand_str
    if tgt in ("game-show", "game show"):
        return "game-show" in cand_str or "game show" in cand_str
    if tgt in ("film-noir", "film noir", "noir"):
        return "film-noir" in cand_str or "film noir" in cand_str or "noir" in cand_str
    if tgt in ("family", "kids", "children"):
        return "family" in cand_str or "kids" in cand_str or "children" in cand_str
    if tgt in ("romance", "romantic"):
        return "romance" in cand_str or "romantic" in cand_str
    if tgt in ("music", "musical"):
        return "music" in cand_str or "musical" in cand_str
    if tgt in ("history", "historical"):
        return "history" in cand_str or "historical" in cand_str
    if tgt in ("biography", "biographical"):
        return "biography" in cand_str or "biographical" in cand_str
    if tgt == "comedy":
        return "comedy" in cand_str
    if tgt == "drama":
        return "drama" in cand_str
    if tgt == "horror":
        return "horror" in cand_str
    if tgt == "thriller":
        return "thriller" in cand_str or "suspense" in cand_str
    if tgt == "mystery":
        return "mystery" in cand_str
    if tgt == "crime":
        return "crime" in cand_str
    if tgt == "documentary":
        return "documentary" in cand_str or "docuseries" in cand_str
    if tgt == "fantasy":
        return "fantasy" in cand_str
    if tgt == "war":
        return "war" in cand_str
    if tgt == "western":
        return "western" in cand_str

    # Word boundary match as fallback
    if re.search(r'\b' + re.escape(tgt) + r'\b', cand_str):
        return True
    return False

def catalog_supports_pagination(cat):
    """Return whether a catalog declares support for Stremio's skip extra."""
    if not isinstance(cat, dict):
        return False
    for key in ("extra", "extraSupported", "extraRequired"):
        extras = cat.get(key) or []
        if not isinstance(extras, (list, tuple, set)):
            continue
        for extra in extras:
            name = extra.get("name") if isinstance(extra, dict) else extra
            if str(name or "").strip().lower() == "skip":
                return True
    return False


def catalog_supports_genre(cat, genre):
    """
    Check if a Stremio catalog supports filtering by the specified genre.
    Returns True if:
    - genre is empty/None/All (no genre filtering requested)
    - The catalog defines genre filtering and the genre matches supported options (case-insensitive)
    """
    if not genre or genre in ("All", "All Genres", "all"):
        return True
    if not cat:
        return False
    g_norm = str(genre).strip().lower()

    from . import database
    is_lang = database.is_language_genre(g_norm)
    is_yr = g_norm.isdigit() and len(g_norm) == 4

    # 1. Check legacy/direct "genres" field on catalog object
    cat_genres = cat.get("genres")
    if cat_genres and isinstance(cat_genres, list) and len(cat_genres) > 0:
        for cg in cat_genres:
            if does_genre_match_filter(cg, g_norm):
                return True

    # 2. Check "extra" declarations
    extra = cat.get("extra") or []
    had_restricted_opts = False
    for ex in extra:
        ex_name = ex.get("name") if isinstance(ex, dict) else ex
        if is_yr and ex_name not in ("year", "genre"):
            continue
        if is_lang and ex_name not in ("language", "genre"):
            continue
        if not is_yr and not is_lang and ex_name != "genre":
            continue

        opts = ex.get("options") if isinstance(ex, dict) else None
        if opts and isinstance(opts, list) and len(opts) > 0:
            had_restricted_opts = True
            for opt in opts:
                if does_genre_match_filter(opt, g_norm):
                    return True
            continue
        elif is_yr and ex_name == "year":
            return True
        elif not is_lang and not is_yr:
            # Unrestricted genre extra accepts arbitrary narrative genres
            return True

    if had_restricted_opts:
        return False

    # 3. Check "extraSupported"
    extra_sup = cat.get("extraSupported") or []
    if is_yr and "year" in extra_sup:
        return True
    if not is_lang and not is_yr and "genre" in extra_sup:
        return True

    # 4. Check if catalog ID or Name is dedicated to this language or year
    if is_lang:
        if catalog_is_dedicated_to_language(cat, g_norm):
            return True
    elif is_yr:
        cat_id = str(cat.get("id") or cat.get("catalog_id") or "").strip().lower()
        if cat_id in ("year", "years", "acc85513", "tmdb.year"):
            return True

    return False

def catalog_is_dedicated_to_language(cat, language):
    """True when a catalog's own id or name says it is this language's section.

    Manifests commonly declare genre options for a section ("Hindi Movies"
    offering Action/Drama), and that declaration makes the options check veto
    the catalog; its id or name is then the only proof it serves the language.
    """
    if not cat or not language:
        return False
    from . import database
    g_norm = str(language).strip().lower()
    if not database.is_language_genre(g_norm):
        return False
    lang_code = database.get_language_code(g_norm)

    def slug_matches(text):
        # Ids and titles are slugs ("tamilmv-now", "bollywood-hits"); only words
        # long enough to be unambiguous may match by prefix, so a TV catalog
        # keyed by country code ("tvgarden:gu") is not read as Gujarati.
        for tok in re.split(r'[^a-z0-9]+', text):
            for needle in (g_norm, lang_code):
                if needle and len(needle) >= 4 and (tok == needle or tok.startswith(needle)):
                    return True
        return False

    cat_id = str(cat.get("id") or cat.get("catalog_id") or "").strip().lower()
    cat_name = str(cat.get("name") or cat.get("catalog_name") or cat.get("display_name") or "").strip().lower()
    # TV-style catalogs are keyed by country code ("tvgarden:gu" named Guam), and
    # those letters collide with language codes (gu = Gujarati). A catalog named
    # for the country the code belongs to is not a language section.
    if lang_code and str(database.get_country_iso_code(cat_name) or "").lower() == str(lang_code).lower():
        return False
    if cat_id and (cat_id == g_norm or (lang_code and cat_id == lang_code)
                   or (lang_code and f"-{lang_code}-" in cat_id)
                   or (lang_code and cat_id.startswith(f"{lang_code}-"))):
        return True
    if cat_id and slug_matches(cat_id):
        return True
    return bool(cat_name) and (bool(re.search(r'\b' + re.escape(g_norm) + r'\b', cat_name)) or slug_matches(cat_name))

def catalog_supports_filter(cat, genre):
    """Whether a catalog can be asked for this filter, by declaration or by its own name.

    Non-language filters behave exactly like catalog_supports_genre; languages gain
    the id/name match, which the options veto can otherwise hide.
    """
    return catalog_supports_genre(cat, genre) or catalog_is_dedicated_to_language(cat, genre)

def country_language_names(country):
    """Canonical language genres (Hindi, Tamil, …) in which this country's titles are made."""
    from . import database
    names = []
    for code in database.get_country_languages(country):
        name = database.LANGUAGE_CODE_MAP.get(code)
        if name and name not in names:
            names.append(name)
    return names

def catalog_language_for_country(cat, country):
    """The language a catalog serves on behalf of a country, or None.

    Stremio manifests almost never declare countries, but they do expose sections
    per cinema (Hindi, Tamil, Telugu…), so a country filter is answered through
    those sections instead of dropping the catalog.
    """
    if not cat or not country:
        return None
    langs = country_language_names(country)
    # A section named for one language serves that one; a catalog that merely
    # lists languages among its options is the weaker claim.
    for lang in langs:
        if catalog_is_dedicated_to_language(cat, lang):
            return lang
    for lang in langs:
        if catalog_supports_genre(cat, lang):
            return lang
    return None

def filter_items_by_genre(items, genre, media_type=None):
    """
    Strictly filter items by the specified IMDb genre or language genre and media type.
    Items with known genres or language (from response or cache) that do NOT match are excluded.
    Items incompatible with media_type are excluded.
    """
    if not items:
        return []

    from . import database

    # If no genre filtering requested, still enforce media_type compatibility if provided
    if not genre or genre in ("All", "All Genres", "all"):
        if media_type and media_type != "all":
            return [it for it in items if is_item_type_compatible(it, media_type)]
        return items or []

    g_norm = str(genre).strip().lower()

    # If the filter is a 4-digit year, filter by release year, NOT narrative genre
    if g_norm.isdigit() and len(g_norm) == 4:
        filtered = []
        for item in items:
            if media_type and media_type not in ("all", "discover"):
                if not is_item_type_compatible(item, media_type):
                    continue
            y = database.extract_clean_year(item)
            if not y:
                item_id = item.get("id") or item.get("imdb_id")
                if item_id:
                    c_meta = database.get_cached_metadata(item_id, media_type or item.get("type"))
                    if c_meta:
                        y = database.extract_clean_year(c_meta)
            if not y or y == g_norm:
                filtered.append(item)
        return filtered

    filtered = []
    is_lang_filter = database.is_language_genre(g_norm)

    for item in items:
        # Prefer the catalog response first. SQLite metadata is a fallback for
        # items whose response does not include genre/language information.
        has_item_genres = bool(item.get("genres") or item.get("genre") or item.get("genre_ids"))
        has_item_lang = bool(item.get("language"))
        cached_meta = None
        item_id = item.get("id") or item.get("imdb_id")
        metadata_checked = False

        # Extract and normalize all genre signals
        item_genres = set()

        def _add_genre_entry(v):
            if not v:
                return
            if isinstance(v, (int, float)):
                gid = int(v)
                if gid in TMDB_GENRES:
                    item_genres.add(TMDB_GENRES[gid].lower())
                    if gid == 10759:
                        item_genres.add("action")
                        item_genres.add("adventure")
                    elif gid == 10765:
                        item_genres.add("sci-fi")
                        item_genres.add("fantasy")
                    elif gid == 10762:
                        item_genres.add("kids")
                        item_genres.add("family")
                    elif gid == 10768:
                        item_genres.add("war")
                        item_genres.add("politics")
                return
            if isinstance(v, dict):
                if v.get("name"):
                    _add_genre_entry(v["name"])
                if v.get("id"):
                    _add_genre_entry(v["id"])
                return
            s = str(v).strip().lower()
            if not s:
                return
            if s.isdigit():
                gid = int(s)
                if gid in TMDB_GENRES:
                    item_genres.add(TMDB_GENRES[gid].lower())
                    if gid == 10759:
                        item_genres.add("action")
                        item_genres.add("adventure")
                    elif gid == 10765:
                        item_genres.add("sci-fi")
                        item_genres.add("fantasy")
                    elif gid == 10762:
                        item_genres.add("kids")
                        item_genres.add("family")
                    elif gid == 10768:
                        item_genres.add("war")
                        item_genres.add("politics")
                return
            for sub in re.split(r'[,/|]+', s):
                sub_clean = sub.strip()
                if sub_clean:
                    # Normalize language names/codes separately so values
                    # such as "en-US" compare as the canonical English label.
                    lang_value = sub_clean.split("-", 1)[0] if "-" in sub_clean else sub_clean
                    normalized_lang = database.normalize_imdb_genre(lang_value)
                    if normalized_lang and database.is_language_genre(normalized_lang):
                        item_genres.add(normalized_lang.lower())
                    else:
                        item_genres.add(sub_clean)

        for src in (item,):
            if not src or not isinstance(src, dict):
                continue
            for k in ("genres", "genre", "genre_ids"):
                raw = src.get(k)
                if isinstance(raw, list):
                    for elem in raw:
                        _add_genre_entry(elem)
                elif raw is not None:
                    _add_genre_entry(raw)
            for k in ("language", "languages", "original_language", "spoken_languages"):
                raw = src.get(k)
                if isinstance(raw, list):
                    for elem in raw:
                        _add_genre_entry(elem)
                elif raw is not None:
                    _add_genre_entry(raw)
            if src.get("country"):
                c_str = str(src.get("country")).lower()
                if "india" in c_str:
                    _add_genre_entry("Hindi")
                elif "japan" in c_str:
                    _add_genre_entry("Japanese")
                elif "korea" in c_str:
                    _add_genre_entry("Korean")

        # Consult cached metadata only when the item response cannot answer
        # the genre/language question. This avoids one SQLite lookup for every
        # fully described item while preserving the DB fallback.
        if (not item_genres or (is_lang_filter and not has_item_lang)) and item_id:
            metadata_checked = True
            metadata_ids = [item_id]
            seen_metadata_ids = {str(item_id)}
            for candidate_id in [item.get("imdb_id")] + list(item.get("alias_ids") or []):
                if candidate_id and str(candidate_id) not in seen_metadata_ids:
                    seen_metadata_ids.add(str(candidate_id))
                    metadata_ids.append(candidate_id)
            cached_meta = database.get_cached_metadata(metadata_ids, media_type or item.get("type"))
            if cached_meta and isinstance(cached_meta, dict):
                for key in ("genres", "genre", "genre_ids", "language", "original_language", "spoken_languages"):
                    raw = cached_meta.get(key)
                    if isinstance(raw, list):
                        for elem in raw:
                            _add_genre_entry(elem)
                    elif raw is not None:
                        _add_genre_entry(raw)
                if cached_meta.get("country"):
                    c_str = str(cached_meta.get("country")).lower()
                    if "india" in c_str:
                        _add_genre_entry("Hindi")
                    elif "japan" in c_str:
                        _add_genre_entry("Japanese")
                    elif "korea" in c_str:
                        _add_genre_entry("Korean")
        # Check media type after the optional metadata fallback so an item
        # without a type does not trigger a second metadata lookup internally.
        if media_type and media_type != "all" and not (metadata_checked and not cached_meta and not (item.get("type") or item.get("media_type"))):
            if not is_item_type_compatible(item, media_type, cached_meta=cached_meta):
                continue

        # Check if type is anime
        itype = str(item.get("type") or (cached_meta.get("type") if cached_meta else "")).strip().lower()
        if itype == "anime":
            item_genres.add("anime")
            item_genres.add("animation")
            item_genres.add("japanese")

        if is_lang_filter:
            known_languages = set()
            for value in item_genres:
                normalized = database.normalize_imdb_genre(value)
                if normalized and database.is_language_genre(normalized):
                    known_languages.add(normalized.lower())
            if not known_languages:
                title = (item.get("title") or item.get("name") or "")
                if re.search(r'[\u0900-\u097F]', title):
                    known_languages.add("hindi")
                elif re.search(r'[\u0B80-\u0BFF]', title):
                    known_languages.add("tamil")
                elif re.search(r'[\u0C00-\u0C7F]', title):
                    known_languages.add("telugu")
                elif re.search(r'[\u0C80-\u0CFF]', title):
                    known_languages.add("kannada")
                elif re.search(r'[\u0D00-\u0D7F]', title):
                    known_languages.add("malayalam")
                elif re.search(r'[\u3040-\u30FF\u31F0-\u31FF]', title):
                    known_languages.add("japanese")
                elif re.search(r'[\uAC00-\uD7AF]', title):
                    known_languages.add("korean")
                # Latin-script titles do not prove that a title is English:
                # many Hindi and other-language titles are transliterated. Keep
                # unknown-language items instead of guessing that they are English.
            if not known_languages or any(does_genre_match_filter(language, g_norm) for language in known_languages):
                filtered.append(item)
            continue

        if item_genres:
            if any(does_genre_match_filter(ig, g_norm) for ig in item_genres):
                filtered.append(item)
            # If item has known genres and did NOT match, discard it!
        else:
            # If genres are not yet known, keep the item (its media type is already verified)
            filtered.append(item)

    return filtered

def hydrate_network_metadata(items, media_type=None, timeout=20.0):
    """Wait briefly for provider metadata on this visible batch before filtering it."""
    if not database.METADATA_HYDRATION_ENABLED or not items:
        return
    pending = {}
    by_id = {}
    by_type = {}
    for item in items:
        if not isinstance(item, dict) or item.get("networks"):
            continue
        if str(item.get("type") or media_type or "").lower() not in ("series", "anime", "tv", "tvshow"):
            continue
        candidates = [item.get("imdb_id"), item.get("id")]
        item_id = next((str(value).strip() for value in candidates if is_imdb_id(value)), None)
        if item_id:
            by_id.setdefault(item_id, []).append(item)
            item_type = str(item.get("type") or media_type or "movie").lower()
            by_type.setdefault(item_type, []).append(item)
    if not by_id:
        return

    for item_id, matched_items in by_id.items():
        done = threading.Event()

        def _on_ready(_title=None, _year=None, _poster=None, event=done, targets=matched_items, hydration_id=item_id):
            cached = database.get_cached_metadata(hydration_id)
            if cached:
                for target in targets:
                    for field in ("networks", "production_companies"):
                        if not target.get(field) and cached.get(field):
                            target[field] = list(cached[field])
            event.set()

        pending[item_id] = (done, _on_ready)
        register_hydration_listener(item_id, _on_ready)

    try:
        for item_type, typed_items in by_type.items():
            hydrate_missing_catalog_items(typed_items, media_type=item_type)
        for item_id, (event, _callback) in pending.items():
            cached = database.get_cached_metadata(item_id)
            if cached and (cached.get("networks") or cached.get("production_companies")):
                for target in by_id[item_id]:
                    for field in ("networks", "production_companies"):
                        if not target.get(field) and cached.get(field):
                            target[field] = list(cached[field])
                event.set()

        deadline = time.monotonic() + max(0.0, float(timeout))
        for event, _callback in pending.values():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event.wait(remaining)
    finally:
        for item_id, (_event, callback) in pending.items():
            unregister_hydration_listener(item_id, callback)


def filter_items_by_network(items, network, media_type=None, metadata_timeout=20.0):
    """Filter catalog items by TMDB TV network; production studios are not networks."""
    if not network or str(network).casefold() in ("all", "all networks"):
        return items
    hydrate_network_metadata(items, media_type=media_type, timeout=metadata_timeout)
    target = str(network).strip().casefold()
    aliases = {
        "amazon prime": {"amazon prime", "amazon prime video"},
        "amazon prime video": {"amazon prime", "amazon prime video"},
        "prime video": {"amazon prime video", "prime video"},
        "hbo": {"hbo", "home box office"},
        "max": {"hbo max", "max"},
        "apple": {"apple tv+", "apple tv plus"},
        "apple tv+": {"apple tv+", "apple tv plus"},
        "netflix": {"netflix", "netflix originals"},
        "disney+": {"disney", "walt disney"},
        "paramount+": {"paramount"},
        "hulu": {"hulu"},
        "peacock": {"peacock", "nbcuniversal"},
    }
    accepted = aliases.get(target, {target})
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        names = set()
        for entry in item.get("networks", []) or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name:
                names.add(str(name).strip().casefold())
        if names & accepted or any(any(alias in name for alias in accepted) for name in names):
            result.append(item)
    return result


def filter_items_by_country(items, country, media_type=None):
    """
    Strictly filter items by the specified country and media type.
    Items with known country from response or cache that do NOT match are excluded.
    Items incompatible with media_type are excluded.
    """
    if not items:
        return []

    from . import database

    if not country or country in ("All", "All Countries", "all"):
        if media_type and media_type != "all":
            return [it for it in items if is_item_type_compatible(it, media_type)]
        return items or []

    c_norm = database.normalize_country_name(country).lower()
    filtered = []

    # Providers label most non-US titles by language only, so an item's language
    # stands in for its country when no country field exists. A country such as
    # India spans ten cinemas, hence the per-country language lists in database.
    def _add_language_guess(val):
        if not val:
            return
        for guessed in database.get_countries_for_language(val):
            item_countries.add(guessed)

    for item in items:
        if not isinstance(item, dict):
            continue
        if media_type and media_type not in ("all", "discover"):
            if not is_item_type_compatible(item, media_type):
                continue

        item_countries = set()

        def _add_country_val(val):
            if not val:
                return
            if isinstance(val, str):
                for part in val.split(","):
                    norm = database.normalize_country_name(part).lower()
                    if norm:
                        item_countries.add(norm)
            elif isinstance(val, list):
                for elem in val:
                    if isinstance(elem, str):
                        for part in elem.split(","):
                            norm = database.normalize_country_name(part).lower()
                            if norm:
                                item_countries.add(norm)
                    elif isinstance(elem, dict):
                        c_name = elem.get("name") or elem.get("iso_3166_1")
                        if c_name:
                            norm = database.normalize_country_name(c_name).lower()
                            if norm:
                                item_countries.add(norm)

        for k in ("country", "countries", "origin_country", "production_countries"):
            _add_country_val(item.get(k))

        if not item_countries:
            _add_language_guess(item.get("language") or item.get("original_language"))

        item_id = item.get("id") or item.get("imdb_id")
        if not item_countries and item_id:
            c_meta = database.get_cached_metadata(item_id, media_type or item.get("type"))
            if c_meta and isinstance(c_meta, dict):
                for k in ("country", "countries", "origin_country", "production_countries"):
                    _add_country_val(c_meta.get(k))
                if not item_countries:
                    _add_language_guess(c_meta.get("language") or c_meta.get("original_language"))

        if not item_countries and item_id:
            try:
                with database._cache_db_lock:
                    conn = database._get_cache_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT country FROM item_countries WHERE item_id = ?", (str(item_id),))
                    for row in cursor.fetchall():
                        if row and row[0]:
                            norm = database.normalize_country_name(row[0]).lower()
                            if norm:
                                item_countries.add(norm)
            except Exception:
                pass

        if c_norm in item_countries or any(c_norm in x or x in c_norm for x in item_countries):
            filtered.append(item)

    return filtered

_AVAILABLE_CATALOGS_CACHE = {}
_AVAILABLE_CATALOGS_LOCK = threading.Lock()

def invalidate_catalogs_cache():
    with _AVAILABLE_CATALOGS_LOCK:
        _AVAILABLE_CATALOGS_CACHE.clear()

def get_available_catalogs(c_type="movie", cache_only=True):
    with _AVAILABLE_CATALOGS_LOCK:
        if c_type in _AVAILABLE_CATALOGS_CACHE:
            return list(_AVAILABLE_CATALOGS_CACHE[c_type])

    from . import database
    catalogs = []
    addons = [a for a in database.get_addons() if has_catalog_resource(a, media_type=c_type)]
    
    for addon in addons:
        addon_name = addon.get("name", "Unknown Addon")
        manifest_url = addon.get("manifest_url", "")
        if not manifest_url or manifest_url.startswith("builtin:"):
            continue
            
        base_url = manifest_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"): base_url += "/"
            
        addon_catalogs = get_addon_catalogs(addon, cache_only=cache_only)
        for cat in addon_catalogs:
            if not is_catalog_browsable(cat):
                continue

            cat_type = cat.get("type")
            cat_name = cat.get("name") or ""
            cat_id = cat.get("id", "")

            matched = is_catalog_type_match(cat, c_type, addon_name)

            if matched:
                genres = list(cat.get("genres") or [])
                extra = cat.get("extra") or []
                for ex in extra:
                    if isinstance(ex, dict) and ex.get("name") == "genre":
                        opts = ex.get("options", [])
                        if opts:
                            genres = list(opts)
                        break
                        
                if not cat_name or cat_name.lower() == "catalog":
                    if str(cat_id).lower() in ["tpbctlg-movies", "tpbctlg-series"]:
                        display_name = f"{addon_name} - Popular"
                    else:
                        display_name = f"{addon_name} - {cat_id}"
                else:
                    display_name = f"{addon_name} - {cat_name}" if addon_name.lower() not in cat_name.lower() else cat_name
                
                catalogs.append({
                    "addon_name": addon_name,
                    "manifest_url": manifest_url,
                    "base_url": base_url,
                    "catalog_id": cat.get("id"),
                    "catalog_name": cat_name,
                    "display_name": display_name,
                    "genres": genres,
                    "extra": extra,
                    "extraSupported": cat.get("extraSupported", []),
                    "type": cat_type or c_type
                })
    if c_type == "anime":
        def get_anime_priority(cat):
            n = (str(cat.get("addon_name")) + " " + str(cat.get("display_name"))).lower()
            if "animestream" in n: return 0
            if "anime kitsu" in n or "kitsu" in n: return 1
            if "onlyanimes" in n: return 2
            if "anime" in n: return 3
            return 4
        catalogs.sort(key=get_anime_priority)

    with _AVAILABLE_CATALOGS_LOCK:
        _AVAILABLE_CATALOGS_CACHE[c_type] = catalogs

    return catalogs


def _get_search_catalogs_for_addon(addon, c_type, cache_only=False):
    if not has_catalog_resource(addon, media_type=c_type):
        return []
        
    catalogs = get_addon_catalogs(addon, cache_only=cache_only)
    if not catalogs:
        return []

    search_cats = []
    for cat in catalogs:
        cat_type = cat.get("type")
        if cat_type and not is_type_match(cat_type, c_type):
            if not (c_type == "anime" and "anime" in str(cat_type).lower()):
                continue
            
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        extra = cat.get("extra") or []
        extra_sup = cat.get("extraSupported") or []
        extra_req = cat.get("extraRequired") or []
        
        is_search = False
        if addon.get("id") == "cinemeta" and cat_id == "top":
            is_search = True
        elif "search" in str(cat_id).lower() or "search" in str(cat_name).lower():
            is_search = True
        elif "search" in extra_sup or "search" in extra_req:
            is_search = True
        else:
            for ex in extra:
                if ex == "search":
                    is_search = True
                    break
                elif isinstance(ex, dict) and ex.get("name") == "search":
                    is_search = True
                    break
                    
        if is_search and cat_id not in search_cats:
            search_cats.append(cat_id)
            
    if not search_cats and addon.get("id") in ["org.stremio.tmdb", "org.cinetorrent", "com.stremio.indianStreamCatalog"]:
        for cat in catalogs:
            if cat.get("type") == c_type or is_type_match(cat.get("type"), c_type):
                search_cats.append(cat.get("id"))
                
    return search_cats


def get_available_search_catalogs(c_type="movie", only_working=True):
    """Return list of catalogs that actually support keyword search."""
    from . import database
    catalogs = []
    addons = get_search_addons(media_type=c_type, only_working=only_working)
    if not addons and only_working:
        addons = get_search_addons(media_type=c_type, only_working=False)

    for addon in addons:
        addon_name = addon.get("name", "Unknown Addon")
        manifest_url = addon.get("manifest_url", "")
        if not manifest_url or manifest_url.startswith("builtin:"):
            continue
        search_cat_ids = set(_get_search_catalogs_for_addon(addon, c_type, cache_only=True))
        addon_catalogs = get_addon_catalogs(addon, cache_only=True)
        for cat in addon_catalogs:
            cat_id = cat.get("id", "")
            if cat_id in search_cat_ids:
                cat_name = cat.get("name") or cat_id
                catalogs.append({
                    "addon_name": addon_name,
                    "manifest_url": manifest_url,
                    "catalog_id": cat_id,
                    "catalog_name": cat_name,
                    "display_name": f"{addon_name} - {cat_name}",
                    "media_type": c_type,
                    "supports_pagination": catalog_supports_pagination(cat),
                })
    return catalogs


def fetch_items(media_type="movie", query="", genre="", catalog_id="top", catalog_url=None, limit=50, page=1, cache_only=False, on_item_found=None, target_manifest_url=None, target_catalog_id=None, is_cancelled=None, timeout=3.5, title_only=False):
    from . import database
    c_type = "series" if media_type == "series" else media_type
    page_limit = limit if (limit and limit > 0) else 50
    skip = (page - 1) * page_limit

    if not target_manifest_url and catalog_url:
        target_manifest_url = catalog_url
    if not target_catalog_id and catalog_id and catalog_id != "top":
        target_catalog_id = catalog_id

    if query:
        import concurrent.futures
        items = []
        seen_ids = set()
        seen_titles = {}

        def fetch_addon_search(addon):
            if is_cancelled and is_cancelled():
                return []
            if not addon.get("enabled", True): return []
            m_url = addon.get("manifest_url", "")
            if not m_url or m_url.startswith("builtin:"): return []
            
            if target_manifest_url and m_url != target_manifest_url:
                return []
                
            if not target_manifest_url and not is_search_provider(addon, media_type=c_type):
                return []
            if not target_manifest_url and not is_addon_online(m_url):
                return []
            
            base_url = m_url.rsplit("manifest.json", 1)[0]
            if not base_url.endswith("/"): base_url += "/"
            
            if target_catalog_id:
                search_catalogs = [target_catalog_id]
            else:
                search_catalogs = _get_search_catalogs_for_addon(addon, c_type, cache_only=cache_only)
                if not search_catalogs and target_manifest_url:
                    search_catalogs = [catalog_id or "top"]
                
            if not search_catalogs:
                return []
                
            addon_items = []
            for cat_id in search_catalogs:
                if is_cancelled and is_cancelled():
                    break
                if page > 1:
                    skip_val = (page - 1) * page_limit
                    search_url = f"{base_url}catalog/{c_type}/{urllib.parse.quote(str(cat_id), safe=':')}/search={urllib.parse.quote(query)}/skip={skip_val}.json"
                else:
                    search_url = f"{base_url}catalog/{c_type}/{urllib.parse.quote(str(cat_id), safe=':')}/search={urllib.parse.quote(query)}.json"
                fetch_kw = {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
                data = _get_cached_request(search_url, max_age_hours=2, cache_only=cache_only, timeout=3.5, **fetch_kw)
                if not data and page > 1:
                    alt_url = f"{base_url}catalog/{c_type}/{urllib.parse.quote(str(cat_id), safe=':')}/search={urllib.parse.quote(query)}.json?skip={skip_val}"
                    data = _get_cached_request(alt_url, max_age_hours=2, cache_only=cache_only, timeout=3.5, **fetch_kw)
                if data and isinstance(data.get("metas"), list):
                    addon_items.extend(data["metas"])
                    if addon_items: 
                        break # Break early if we found results for this addon
            return addon_items

        if target_manifest_url:
            addons_to_search = [
                a for a in database.get_addons() 
                if a.get("manifest_url") == target_manifest_url 
                and is_addon_online(a.get("manifest_url", ""))
            ]
            if not addons_to_search:
                addons_to_search = [
                    a for a in database.get_addons()
                    if a.get("manifest_url") == target_manifest_url and a.get("enabled", True)
                ]
            if not addons_to_search:
                addons_to_search = [{
                    "manifest_url": target_manifest_url,
                    "name": "Addon",
                    "enabled": True,
                    "catalogs": [{"type": c_type, "id": target_catalog_id or "top", "extra": ["search"]}]
                }]
        else:
            addons_to_search = get_search_addons(media_type=c_type, only_working=True)
            if not addons_to_search:
                addons_to_search = get_search_addons(media_type=c_type, only_working=False)
        if not addons_to_search:
            return []

        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(addons_to_search), 8))
            future_to_addon = {executor.submit(fetch_addon_search, addon): addon for addon in addons_to_search}
        except RuntimeError:
            return []
        try:
            # Allow room for the primary search URL and the alternate skip URL
            # used by providers that encode pagination differently.
            for future in concurrent.futures.as_completed(future_to_addon, timeout=max(7.0, timeout * 3)):
                if is_cancelled and is_cancelled():
                    break
                try:
                    addon_items = future.result()
                    new_batch = []
                    q_lower = query.lower()
                    cur_addon = future_to_addon.get(future) or {}
                    cur_addon_name = cur_addon.get("name", "")
                    cur_manifest_url = cur_addon.get("manifest_url", "")
                    for m in addon_items:
                        if database.is_adult_content_hidden() and is_adult_item(m):
                            continue
                        title = m.get("name") or m.get("title") or ""
                        desc = m.get("description") or m.get("overview") or ""
                        if title_only:
                            if q_lower not in str(title).lower():
                                continue
                        else:
                            if q_lower not in str(title).lower() and q_lower not in str(desc).lower():
                                continue

                        imdb_id = m.get("imdb_id") or m.get("id")
                        if not imdb_id or imdb_id in seen_ids:
                            continue

                        title_lower = title.lower().strip()
                        year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""

                        matched_item = None
                        if title_lower in seen_titles:
                            for existing in seen_titles[title_lower]:
                                if existing["year"] == year or not existing["year"] or not year:
                                    matched_item = existing
                                    break

                        if matched_item:
                            if imdb_id not in matched_item.get("alias_ids", []):
                                if str(imdb_id).startswith("tt") and not str(matched_item["id"]).startswith("tt"):
                                    matched_item.setdefault("alias_ids", []).insert(0, imdb_id)
                                    matched_item["id"] = imdb_id
                                    if m.get("type"):
                                        matched_item["type"] = m.get("type")
                                else:
                                    matched_item.setdefault("alias_ids", []).append(imdb_id)
                                seen_ids.add(imdb_id)
                            if not matched_item["year"] and year:
                                matched_item["year"] = year
                            continue

                        seen_ids.add(imdb_id)
                        poster_url = m.get("poster") or m.get("medium_cover_image") or m.get("logo") or m.get("banner") or m.get("background") or m.get("icon") or m.get("thumbnail") or ""
                        if poster_url and poster_url.startswith("//"):
                            poster_url = "https:" + poster_url
                        item_desc = m.get("description") or m.get("overview") or ""
                        item_rating = str(m.get("imdbRating") or m.get("rating") or m.get("vote_average") or "")
                        item_runtime = str(m.get("runtime") or "")
                        item_bg = m.get("background") or m.get("backdrop") or ""
                        item_cast = list(m.get("cast") or [])
                        item_videos = m.get("videos") or []
                        item_trailer = extract_trailer_id(m)

                        item_obj = {
                            "id": imdb_id,
                            "alias_ids": [imdb_id],
                            "title": title,
                            "year": year,
                            "medium_cover_image": poster_url,
                            "poster": poster_url,
                            "type": m.get("type") or media_type,
                            "genres": m.get("genres") or [],
                            "addon_name": cur_addon_name,
                            "catalog_name": "Search",
                            "catalog_id": target_catalog_id or "search",
                            "manifest_url": cur_manifest_url
                        }
                        if item_desc:
                            item_obj["description"] = item_desc
                            item_obj["overview"] = item_desc
                        if item_rating:
                            item_obj["imdbRating"] = item_rating
                        if item_runtime:
                            item_obj["runtime"] = item_runtime
                        if item_bg:
                            item_obj["background"] = item_bg
                        if item_cast:
                            item_obj["cast"] = item_cast
                        if item_videos:
                            item_obj["videos"] = item_videos
                        if item_trailer:
                            item_obj["trailer"] = item_trailer

                        if item_desc and imdb_id:
                            database.save_cached_metadata(imdb_id, item_obj["type"], item_obj)
                        seen_titles.setdefault(title_lower, []).append(item_obj)
                        items.append(item_obj)
                        new_batch.append(item_obj)
                    if new_batch:
                        database.hydrate_items_from_metadata(new_batch, media_type=c_type)
                        hydrate_missing_catalog_items(new_batch, media_type=c_type)
                    if new_batch and on_item_found:
                        if not (is_cancelled and is_cancelled()):
                            on_item_found(new_batch)
                except Exception:
                    pass
        except concurrent.futures.TimeoutError:
            for future in future_to_addon:
                future.cancel()
        except RuntimeError as e:
            if "shutdown" in str(e).lower() or "interpreter" in str(e).lower():
                return []
            raise
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
                    
        if items:
            database.hydrate_items_from_metadata(items, media_type=c_type)
            hydrate_missing_catalog_items(items, media_type=c_type)
        return items

    if catalog_url:
        is_iptv_org = False
        for a in database.get_addons():
            if a.get("manifest_url") == catalog_url and a.get("id") == "local.iptv-org":
                is_iptv_org = True
                break
                
        if is_iptv_org:
            channels_data = _get_cached_request("https://iptv-org.github.io/api/channels.json", max_age_hours=24)
            if not channels_data:
                return []
            
            country_code = catalog_id.upper()
            movies = []
            for ch in channels_data:
                if country_code == "ALL" or ch.get("country") == country_code:
                    movies.append({
                        "id": ch.get("id"),
                        "title": ch.get("name"),
                        "year": ch.get("country", ""),
                        "medium_cover_image": ch.get("logo", ""),
                        "type": "tv"
                    })
            if genre and genre != "All":
                movies = [m for m in movies if genre.lower() in str(m.get("categories", [])).lower()]
            return movies[skip:skip+100]

        base_url = catalog_url
        if "manifest.json" in base_url:
            base_url = base_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"):
            base_url += "/"
            
        actual_cat_type = c_type
        cat_obj = None
        for a in database.get_addons():
            m_url = a.get("manifest_url", "")
            if m_url and (m_url == catalog_url or catalog_url.startswith(m_url.rsplit("manifest.json", 1)[0])):
                addon_cats = get_addon_catalogs(a, cache_only=False)
                candidates = [cat for cat in addon_cats if str(cat.get("id")) == str(catalog_id)]
                if len(candidates) == 1:
                    cat_obj = candidates[0]
                    actual_cat_type = candidates[0].get("type") or c_type
                elif len(candidates) > 1:
                    exact = next((cat for cat in candidates if str(cat.get("type") or "").lower() == c_type.lower()), None)
                    if exact:
                        cat_obj = exact
                        actual_cat_type = exact.get("type") or c_type
                    else:
                        compat = next((cat for cat in candidates if is_type_match(cat.get("type"), c_type)), None)
                        cat_obj = compat or candidates[0]
                        actual_cat_type = cat_obj.get("type") or c_type
                elif not candidates and addon_cats:
                    for cat in addon_cats:
                        ct = cat.get("type")
                        if ct and ct.lower() == c_type.lower():
                            cat_obj = cat
                            actual_cat_type = ct
                            break
                break

        server_genre = None
        server_supports_skip = False
        target_extra = None
        g_str = str(genre).strip() if genre else ""
        is_yr = bool(g_str.isdigit() and len(g_str) == 4)

        if cat_obj:
            extra = cat_obj.get("extra") or []
            year_extra = None
            genre_extra = None
            lang_extra = None
            for ex in extra:
                ex_dict = ex if isinstance(ex, dict) else {"name": ex}
                ex_name = ex_dict.get("name")
                if ex_name == "skip":
                    server_supports_skip = True
                elif ex_name == "year":
                    year_extra = ex_dict
                elif ex_name == "genre":
                    genre_extra = ex_dict
                elif ex_name == "language":
                    lang_extra = ex_dict
            if catalog_supports_pagination(cat_obj):
                server_supports_skip = True

            is_lang = database.is_language_genre(g_str.lower())
            if is_yr:
                target_extra = year_extra or genre_extra
            elif is_lang:
                target_extra = lang_extra or genre_extra
            else:
                target_extra = genre_extra

            if genre and genre != "All":
                cat_genres = cat_obj.get("genres") or []
                matched_opt = next((cg for cg in cat_genres if does_genre_match_filter(cg, genre)), None)
                if matched_opt:
                    server_genre = matched_opt
                elif target_extra is not None:
                    opts = target_extra.get("options") or []
                    if opts:
                        server_genre = next((opt for opt in opts if does_genre_match_filter(opt, genre)), None)
                    else:
                        server_genre = g_str
                elif "genre" in (cat_obj.get("extraSupported") or []) or "year" in (cat_obj.get("extraSupported") or []):
                    server_genre = g_str
        else:
            server_supports_skip = True
            if genre and genre != "All":
                server_genre = g_str

        # If catalog does not support pagination and a subsequent page was requested, return empty
        if skip > 0 and cat_obj and not server_supports_skip:
            return []

        if is_cancelled and is_cancelled():
            return []

        url = f"{base_url}catalog/{actual_cat_type}/{catalog_id}"
        
        extras = []
        if genre and genre != "All" and server_genre:
            extra_param_name = target_extra.get("name") if target_extra else ("year" if is_yr else "genre")
            extras.append(f"{extra_param_name}={urllib.parse.quote(server_genre)}")
        if skip > 0 and server_supports_skip:
            extras.append(f"skip={skip}")
            
        if extras:
            url += "/" + "&".join(extras) + ".json"
        else:
            url += ".json"

        if is_cancelled and is_cancelled():
            return []
            
        fetch_kw = {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
        data = _get_cached_request(url, max_age_hours=2, cache_only=cache_only, timeout=timeout, **fetch_kw)
        if data is None or (is_cancelled and is_cancelled()):
            if data is None and catalog_url and not (is_cancelled and is_cancelled()):
                _record_addon_failure(catalog_url)
            return None if data is None else []

        if catalog_url:
            _record_addon_success(catalog_url)
            
        found_addon_name = ""
        found_catalog_name = str(catalog_id).title()
        for a in database.get_addons():
            m_url = a.get("manifest_url", "")
            if m_url and (m_url == catalog_url or catalog_url.startswith(m_url.rsplit("manifest.json", 1)[0])):
                found_addon_name = a.get("name", "")
                addon_cats = get_addon_catalogs(a, cache_only=True)
                for cat in (addon_cats or []):
                    if str(cat.get("id")) == str(catalog_id) and cat.get("name"):
                        found_catalog_name = cat.get("name")
                        break
                break

        if isinstance(data.get("metas"), list):
            from .movie_widget import extract_image_url
            movies = []
            for m in data["metas"]:
                if database.is_adult_content_hidden() and is_adult_item(m):
                    continue
                imdb_id = m.get("imdb_id") or m.get("id")
                if not imdb_id or str(imdb_id).startswith("hub:upsell") or imdb_id == "upsell" or str(m.get("name", "")).lower() == "unlock every stream":
                    continue
                poster = extract_image_url(m)
                title = m.get("name") or m.get("title", "")
                year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else (str(m.get("year", "")).strip() if m.get("year") else "")
                item_type = m.get("type") or media_type
                if (not title or title == "Unknown" or not poster) and imdb_id:
                    cached_m = database.get_cached_metadata(imdb_id, item_type)
                    if cached_m:
                        if not title or title == "Unknown":
                            title = cached_m.get("title") or cached_m.get("name", "")
                        if not poster:
                            poster = cached_m.get("medium_cover_image") or cached_m.get("poster", "")
                        if not year:
                            year = str(cached_m.get("year", ""))
                if str(imdb_id).startswith("bolly:m:") or str(imdb_id).startswith("hub:m:"):
                    item_type = "movie"
                elif str(imdb_id).startswith("bolly:s:") or str(imdb_id).startswith("hub:s:"):
                    item_type = "series"

                alias_ids = [imdb_id] if imdb_id else []

                if str(imdb_id).startswith("bolly:") or str(imdb_id).startswith("hub:"):
                    tmdb_num = str(imdb_id).split(":")[-1]
                    if tmdb_num.isdigit():
                        alias_ids.append(f"tmdb:{tmdb_num}")

                if str(imdb_id).startswith("tpb_ctl:"):
                    try:
                        import base64, json
                        raw_b64 = str(imdb_id).split("tpb_ctl:", 1)[1]
                        payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
                        if payload.get("extra", {}).get("type"):
                            item_type = payload["extra"]["type"]
                        p_val = payload.get("poster")
                        if p_val:
                            if not poster: poster = p_val
                            tt_m = re.search(r'\b(tt\d{7,8})\b', p_val)
                            if tt_m and tt_m.group(1) not in alias_ids:
                                alias_ids.append(tt_m.group(1))
                    except Exception:
                        pass
                elif poster:
                    tt_m = re.search(r'\b(tt\d{7,8})\b', poster)
                    if tt_m and tt_m.group(1) not in alias_ids:
                        alias_ids.append(tt_m.group(1))

                item_genres = m.get("genres") or m.get("genre_ids") or (m.get("genre") if isinstance(m.get("genre"), list) else ([m.get("genre")] if m.get("genre") else []))
                item_cat_name = m.get("catalog_name") or found_catalog_name
                item_addon_name = m.get("addon_name") or found_addon_name

                item_desc = m.get("description") or m.get("overview") or ""
                item_rating = str(m.get("imdbRating") or m.get("rating") or m.get("vote_average") or "")
                item_runtime = str(m.get("runtime") or "")
                item_bg = m.get("background") or m.get("backdrop") or ""
                item_cast = list(m.get("cast") or [])
                item_videos = m.get("videos") or []
                item_trailer = extract_trailer_id(m)

                item_dict = {
                    "id": imdb_id,
                    "alias_ids": alias_ids,
                    "title": title,
                    "year": year,
                    "medium_cover_image": poster,
                    "poster": poster,
                    "type": item_type,
                    "genres": item_genres,
                    "addon_name": item_addon_name,
                    "catalog_name": item_cat_name,
                    "catalog_id": catalog_id,
                    "manifest_url": catalog_url
                }
                if item_desc:
                    item_dict["description"] = item_desc
                    item_dict["overview"] = item_desc
                if item_rating:
                    item_dict["imdbRating"] = item_rating
                if item_runtime:
                    item_dict["runtime"] = item_runtime
                if item_bg:
                    item_dict["background"] = item_bg
                if item_cast:
                    item_dict["cast"] = item_cast
                if item_videos:
                    item_dict["videos"] = item_videos
                if item_trailer:
                    item_dict["trailer"] = item_trailer
                if imdb_id:
                    database.save_cached_metadata(imdb_id, item_type, item_dict)

                movies.append(item_dict)
            hydrate_missing_catalog_items(movies, media_type=media_type)
            if genre and genre != "All" and str(catalog_id) not in ("year", "years", "acc85513", "tmdb.year"):
                movies = filter_items_by_genre(movies, genre, media_type=media_type)
            if database.is_adult_content_hidden():
                movies = [m for m in movies if not is_adult_item(m)]
            if movies and page_limit and page_limit > 0:
                if not server_supports_skip:
                    movies = movies[skip : skip + page_limit]
                elif len(movies) > page_limit:
                    movies = movies[:page_limit]
            return movies
        return []

    return []

# --- Polite Background Metadata Hydration Queue ---
_HYDRATION_QUEUE = queue.Queue(maxsize=10000)
_HYDRATION_BACKFILL_QUEUE = queue.Queue(maxsize=10000)
_HYDRATION_SEEN = set()
_HYDRATION_LOCK = threading.Lock()
_HYDRATION_THREAD = None
_HYDRATION_BOOTSTRAP_DONE = False
_HYDRATION_LISTENERS_LOCK = threading.Lock()
_HYDRATION_LISTENERS = {}

def register_hydration_listener(item_id, callback):
    """Register a UI callback to be notified when item_id metadata hydration completes."""
    if not database.METADATA_HYDRATION_ENABLED or not item_id or not callable(callback):
        return
    iid = str(item_id).strip()
    with _HYDRATION_LISTENERS_LOCK:
        _HYDRATION_LISTENERS.setdefault(iid, []).append(callback)

def unregister_hydration_listener(item_id, callback):
    """Unregister a previously registered UI callback."""
    if not item_id:
        return
    iid = str(item_id).strip()
    with _HYDRATION_LISTENERS_LOCK:
        listeners = _HYDRATION_LISTENERS.get(iid, [])
        if callback in listeners:
            try:
                listeners.remove(callback)
            except ValueError:
                pass
        if not listeners and iid in _HYDRATION_LISTENERS:
            del _HYDRATION_LISTENERS[iid]

def _notify_hydration_listeners(item_id, item_dict):
    """Notify all listeners that hydration has completed, popping any legacy _on_hydrated function."""
    if not item_id:
        return
    iid = str(item_id).strip()
    with _HYDRATION_LISTENERS_LOCK:
        listeners = list(_HYDRATION_LISTENERS.pop(iid, []))

    if isinstance(item_dict, dict):
        legacy_cb = item_dict.pop("_on_hydrated", None)
        if callable(legacy_cb) and legacy_cb not in listeners:
            listeners.append(legacy_cb)

    if listeners and isinstance(item_dict, dict):
        title = item_dict.get("title")
        year = item_dict.get("year")
        poster = item_dict.get("medium_cover_image") or item_dict.get("poster")
        for cb in listeners:
            if callable(cb):
                try:
                    from gi.repository import GLib
                    GLib.idle_add(cb, title, year, poster)
                except Exception:
                    pass

_IMDB_ID_RE = re.compile(r"^tt\d{6,10}$")

def is_imdb_id(value):
    """True only for well-formed IMDb IDs (tt + digits).

    Loose ``startswith("tt")`` checks used to send catalogue pseudo-IDs such as
    ``tt_movie_1`` or ``tt_<slug>`` to Cinemeta, wasting provider requests on
    IDs that can never resolve.
    """
    if not value:
        return False
    return bool(_IMDB_ID_RE.match(str(value).strip()))


def _promote_metadata_backfill(key):
    """Move a queued catch-up lookup ahead of background work when a view needs it."""
    with _HYDRATION_BACKFILL_QUEUE.mutex:
        for index, job in enumerate(_HYDRATION_BACKFILL_QUEUE.queue):
            if (str(job[0]), str(job[1])) != key:
                continue
            del _HYDRATION_BACKFILL_QUEUE.queue[index]
            _HYDRATION_BACKFILL_QUEUE.unfinished_tasks -= 1
            if _HYDRATION_BACKFILL_QUEUE.unfinished_tasks == 0:
                _HYDRATION_BACKFILL_QUEUE.all_tasks_done.notify_all()
            promoted_job = (*job[:4], _HYDRATION_QUEUE)
            try:
                _HYDRATION_QUEUE.put_nowait(promoted_job)
            except queue.Full:
                _HYDRATION_BACKFILL_QUEUE.queue.append(job)
                _HYDRATION_BACKFILL_QUEUE.unfinished_tasks += 1
                _HYDRATION_BACKFILL_QUEUE.not_empty.notify()
                return False
            return True
    return False

def queue_metadata_hydration(item_id, media_type="movie", item_dict=None, force_refresh=False):
    """Queue an item for polite, rate-limited background metadata hydration from Cinemeta."""
    if not database.METADATA_HYDRATION_ENABLED or not is_imdb_id(item_id):
        return False
    global _HYDRATION_THREAD, _HYDRATION_BOOTSTRAP_DONE
    with _HYDRATION_LOCK:
        bootstrap = not _HYDRATION_BOOTSTRAP_DONE
        _HYDRATION_BOOTSTRAP_DONE = True
    if bootstrap:
        try:
            for stale_id, stale_type in database.get_metadata_needing_hydration(limit=10000, min_age_days=0):
                live_item = item_dict if str(stale_id) == str(item_id) else None
                queue_metadata_hydration(stale_id, stale_type, item_dict=live_item, force_refresh=True)
        except Exception:
            logging.debug("Could not enqueue startup metadata catch-up", exc_info=True)
    iid = str(item_id).strip()
    m_type = str(media_type or "movie").strip().lower()
    key = (iid, m_type)
    backfill = bool(force_refresh and item_dict is None)
    target_queue = _HYDRATION_BACKFILL_QUEUE if backfill else _HYDRATION_QUEUE
    with _HYDRATION_LOCK:
        if key in _HYDRATION_SEEN:
            if not backfill:
                _promote_metadata_backfill(key)
            return False
        _HYDRATION_SEEN.add(key)
        if target_queue.full():
            try:
                dropped = target_queue.get_nowait()
                dropped_id, dropped_type = dropped[0], dropped[1]
                _HYDRATION_SEEN.discard((str(dropped_id), str(dropped_type)))
                target_queue.task_done()
            except Exception:
                pass
        target_queue.put((iid, m_type, item_dict, bool(force_refresh), target_queue))
        if _HYDRATION_THREAD is None or not _HYDRATION_THREAD.is_alive():
            _HYDRATION_THREAD = threading.Thread(target=_run_metadata_hydration_worker, daemon=True, name="MetadataHydrator")
            _HYDRATION_THREAD.start()
    return True

def _run_metadata_hydration_worker():
    """Background daemon worker that hydrates metadata politely at <=2.5 req/sec."""
    while True:
        try:
            try:
                hydration_job = _HYDRATION_QUEUE.get_nowait()
                job_queue = _HYDRATION_QUEUE
            except queue.Empty:
                job_queue = _HYDRATION_BACKFILL_QUEUE
                hydration_job = job_queue.get(timeout=30)
            if len(hydration_job) == 3:
                item_id, m_type, item_dict = hydration_job
                force_refresh = False
            else:
                item_id, m_type, item_dict, force_refresh = hydration_job[:4]
                job_queue = hydration_job[4] if len(hydration_job) > 4 else job_queue
        except (queue.Empty, Exception):
            break

        try:
            # 1. Fast check if already cached in SQLite (another thread or previous view populated it)
            cached = database.get_cached_metadata(item_id, m_type)
            networks_checked = float(cached.get("networks_checked_at") or 0) if cached else 0
            needs_network_retry = (
                bool(cached)
                and m_type in ("series", "anime", "tv", "tvshow")
                and not cached.get("networks")
                and time.time() - networks_checked > 7 * 24 * 3600
            )
            cached_cover = (cached.get("medium_cover_image") or cached.get("poster")) if cached else None
            cached_title = (cached.get("title") or cached.get("name")) if cached else None
            cached_complete = bool(cached and cached_title and cached.get("genres") and cached.get("year") and (cached.get("description") or cached.get("overview")) and cached_cover and cached.get("language"))
            metadata_checked = float(cached.get("metadata_checked_at") or 0) if cached else 0
            needs_metadata_retry = bool(cached) and not cached_complete and time.time() - metadata_checked > 7 * 24 * 3600
            if cached and not force_refresh and (cached_complete or not needs_metadata_retry) and not needs_network_retry:
                if item_dict is not None and isinstance(item_dict, dict):
                    if not item_dict.get("genres") and cached.get("genres"):
                        item_dict["genres"] = list(cached["genres"])
                    if not item_dict.get("year") and cached.get("year"):
                        item_dict["year"] = str(cached["year"])
                    desc = cached.get("description") or cached.get("overview")
                    if desc and not item_dict.get("description"):
                        item_dict["description"] = desc
                    if not item_dict.get("imdbRating") and cached.get("imdbRating"):
                        item_dict["imdbRating"] = str(cached["imdbRating"])
                    if not item_dict.get("runtime") and cached.get("runtime"):
                        item_dict["runtime"] = str(cached["runtime"])
                    if not item_dict.get("background") and cached.get("background"):
                        item_dict["background"] = cached["background"]
                    if not item_dict.get("cast") and cached.get("cast"):
                        item_dict["cast"] = list(cached["cast"])
                    for field in ("networks", "production_companies"):
                        if not item_dict.get(field) and cached.get(field):
                            item_dict[field] = list(cached[field])
                    if not item_dict.get("videos") and cached.get("videos"):
                        item_dict["videos"] = cached["videos"]
                    # Covers belong here too: this branch is what lets a card that
                    # arrived with no poster URL pick one up from a metadata row some
                    # other view already fetched, without a request.
                    cov = cached.get("medium_cover_image") or cached.get("poster")
                    if cov and not (item_dict.get("medium_cover_image") or item_dict.get("poster")):
                        item_dict["medium_cover_image"] = cov
                        item_dict["poster"] = cov
                    _notify_hydration_listeners(item_id, item_dict)
                continue

            # 2. Check provider cooldown to avoid hitting a rate-limited host
            if is_provider_in_cooldown("v3-cinemeta.strem.io") or is_provider_in_cooldown("cinemeta.strem.io"):
                time.sleep(5)
                continue

            # 3. Polite rate-limiting: 350ms delay between calls = ~2.8 requests/second max
            time.sleep(0.35)

            # 4. Fetch metadata from Cinemeta (keep forever)
            c_url = f"https://v3-cinemeta.strem.io/meta/{m_type}/{item_id}.json"
            refresh_age = 0 if force_refresh else 168
            meta_res = _get_cached_request(c_url, max_age_hours=refresh_age, timeout=3.0)
            if not meta_res or not isinstance(meta_res.get("meta"), dict):
                alt_url = f"https://cinemeta-live.strem.io/meta/{m_type}/{item_id}.json"
                meta_res = _get_cached_request(alt_url, max_age_hours=refresh_age, timeout=3.0)

            if meta_res and isinstance(meta_res.get("meta"), dict):
                meta_dict = meta_res["meta"]
                meta_dict["metadata_checked_at"] = time.time()
                title_str = meta_dict.get("name") or meta_dict.get("title") or item_id
                clean_year_val = database.extract_clean_year(meta_dict)
                if clean_year_val and not meta_dict.get("year"):
                    meta_dict["year"] = clean_year_val
                genres_list = meta_dict.get("genres") or []

                # Fallback to TMDB if Cinemeta has no genres or no language or no country
                if (
                    not genres_list
                    or not meta_dict.get("language")
                    or not meta_dict.get("country")
                    or not (meta_dict.get("networks") or meta_dict.get("production_companies"))
                    or needs_network_retry
                ):
                    try:
                        from . import tmdb_helper
                        tmdb_data = tmdb_helper.fetch_title_language_and_genres(item_id, media_type=m_type, title=title_str, force_refresh=force_refresh)
                        if tmdb_data:
                            if not genres_list and tmdb_data.get("genres"):
                                meta_dict["genres"] = tmdb_data["genres"]
                                genres_list = tmdb_data["genres"]
                            if tmdb_data.get("language_name"):
                                lang_name = tmdb_data["language_name"]
                                if not meta_dict.get("language"):
                                    meta_dict["language"] = lang_name
                            if tmdb_data.get("country"):
                                if not meta_dict.get("country"):
                                    meta_dict["country"] = tmdb_data["country"]
                            if tmdb_data.get("countries"):
                                meta_dict["countries"] = tmdb_data["countries"]
                            if tmdb_data.get("production_companies"):
                                meta_dict["production_companies"] = tmdb_data["production_companies"]
                            if tmdb_data.get("networks"):
                                meta_dict["networks"] = tmdb_data["networks"]
                            if tmdb_data is not None:
                                meta_dict["networks_checked_at"] = time.time()
                            if not clean_year_val and tmdb_data.get("release_date"):
                                clean_year_val = database.extract_clean_year(tmdb_data["release_date"])
                                if clean_year_val:
                                    meta_dict["year"] = clean_year_val
                    except Exception:
                        pass

                clean_tr = extract_trailer_id(meta_dict)
                if clean_tr:
                    meta_dict["trailer"] = clean_tr

                # Save into SQLite metadata_cache and item_genres
                database.save_cached_metadata(item_id, m_type, meta_dict)
                orig_id = item_dict.get("id") if item_dict else None
                if orig_id and str(orig_id) != str(item_id):
                    database.save_cached_metadata(orig_id, m_type, meta_dict)
                print(f"[METADATA-HYDRATE] Cinemeta fetched: '{title_str}' ({item_id}) -> Year: {clean_year_val}, Genres: {genres_list} (Saved to SQLite)")

                # In-memory hydration of live card
                if item_dict is not None and isinstance(item_dict, dict):
                    if genres_list:
                        if not item_dict.get("genres"):
                            item_dict["genres"] = list(genres_list)
                        else:
                            for g in genres_list:
                                if g not in item_dict["genres"]:
                                    item_dict["genres"].append(g)
                    if meta_dict.get("language"):
                        item_dict["language"] = meta_dict["language"]
                    if meta_dict.get("country") and not item_dict.get("country"):
                        item_dict["country"] = meta_dict["country"]
                    clean_yr = clean_year_val or meta_dict.get("year")
                    if clean_yr and not item_dict.get("year"):
                        item_dict["year"] = str(clean_yr)
                    if meta_dict.get("name") and (not item_dict.get("title") or item_dict.get("title") == "Unknown"):
                        item_dict["title"] = meta_dict["name"]
                    if meta_dict.get("poster") and not item_dict.get("medium_cover_image"):
                        item_dict["medium_cover_image"] = meta_dict["poster"]
                        item_dict["poster"] = meta_dict["poster"]
                    desc = meta_dict.get("description") or meta_dict.get("overview")
                    if desc and not item_dict.get("description"):
                        item_dict["description"] = desc
                    if not item_dict.get("imdbRating") and meta_dict.get("imdbRating"):
                        item_dict["imdbRating"] = str(meta_dict["imdbRating"])
                    if not item_dict.get("runtime") and meta_dict.get("runtime"):
                        item_dict["runtime"] = str(meta_dict["runtime"])
                    if not item_dict.get("background") and meta_dict.get("background"):
                        item_dict["background"] = meta_dict["background"]
                    if not item_dict.get("cast") and meta_dict.get("cast"):
                        item_dict["cast"] = list(meta_dict["cast"])
                    for field in ("networks", "production_companies"):
                        if not item_dict.get(field) and meta_dict.get(field):
                            item_dict[field] = list(meta_dict[field])
                    if not item_dict.get("videos") and meta_dict.get("videos"):
                        item_dict["videos"] = meta_dict["videos"]
                    if clean_tr and not item_dict.get("trailer"):
                        item_dict["trailer"] = clean_tr
                    elif not item_dict.get("trailer") and meta_dict.get("trailer"):
                        item_dict["trailer"] = meta_dict["trailer"]

                    _notify_hydration_listeners(item_id, item_dict)
        except Exception as e:
            logging.debug(f"Metadata hydration error for {item_id}: {e}")
        finally:
            # Do not permanently suppress an item after a transient failure or
            # a provider response that was still incomplete. A later catalog
            # visit can retry it; successful results short-circuit from SQLite.
            with _HYDRATION_LOCK:
                _HYDRATION_SEEN.discard((str(item_id), str(m_type)))
            _notify_hydration_listeners(item_id, item_dict if isinstance(item_dict, dict) else {})
            try:
                job_queue.task_done()
            except Exception:
                pass

def hydrate_missing_catalog_items(items, media_type="movie"):
    """Batch-hydrates any catalog items missing titles/posters/genres/year using metadata_cache and background hydrator."""
    if not database.METADATA_HYDRATION_ENABLED or not items:
        return False

    updated = False
    missing_titles = []
    missing_metadata = []
    local_hydrated_count = 0
    # 0. Immediate local DB cache pass (sub-millisecond SQLite WAL lookup) for ALL items
    try:
        fast_count = database.hydrate_items_from_metadata(items, media_type=media_type)
        if fast_count > 0:
            updated = True
            local_hydrated_count += fast_count
    except Exception:
        pass

    for m in items:
        if not isinstance(m, dict):
            continue
        m_id = m.get("id") or m.get("imdb_id")
        if not m_id:
            continue
        m_type = m.get("type") or media_type

        # Ensure clean year is extracted from releaseInfo / released
        clean_yr = database.extract_clean_year(m)
        if clean_yr and not m.get("year"):
            m["year"] = clean_yr

        raw_title = str(m.get("title") or m.get("name") or "").strip()
        is_missing_title = not raw_title or raw_title == "Unknown"
        if not m.get("title") and m.get("name"):
            m["title"] = m["name"]
        is_missing_genres = not m.get("genres")
        is_missing_year = not m.get("year")
        is_missing_cover = not m.get("medium_cover_image") and not m.get("poster")
        is_missing_desc = not m.get("description") and not m.get("overview")
        is_missing_language = not m.get("language")
        is_missing_networks = m_type in ("series", "anime", "tv", "tvshow") and not m.get("networks")

        candidates = [str(m_id)]
        if m.get("imdb_id") and str(m["imdb_id"]) not in candidates:
            candidates.append(str(m["imdb_id"]))
        for aid in (m.get("alias_ids") or []):
            if str(aid) not in candidates:
                candidates.append(str(aid))

        has_known_key = any(str(c).startswith(("tt", "tmdb:", "kitsu:", "ctmdb.")) for c in candidates)
        if not has_known_key:
            continue

        if is_missing_title or is_missing_genres or is_missing_year or is_missing_cover or is_missing_desc or is_missing_language or is_missing_networks:
            c_meta = None
            for cid in candidates:
                c_meta = database.get_cached_metadata(cid, m_type)
                if c_meta and isinstance(c_meta, dict):
                    break
            if c_meta and isinstance(c_meta, dict):
                item_hydrated = False
                if is_missing_title and (c_meta.get("title") or c_meta.get("name")):
                    t_val = c_meta.get("title") or c_meta.get("name", "")
                    if t_val and t_val != "Unknown":
                        m["title"] = t_val
                        is_missing_title = False
                        updated = True
                        item_hydrated = True
                if is_missing_cover:
                    cov = c_meta.get("medium_cover_image") or c_meta.get("poster")
                    if cov:
                        m["medium_cover_image"] = cov
                        m["poster"] = cov
                        updated = True
                        item_hydrated = True
                if is_missing_genres and c_meta.get("genres"):
                    m["genres"] = list(c_meta["genres"])
                    is_missing_genres = False
                    updated = True
                    item_hydrated = True
                c_lang = c_meta.get("language") or c_meta.get("original_language")
                if c_lang:
                    norm_lang = database.normalize_imdb_genre(str(c_lang))
                    if norm_lang:
                        if is_missing_language:
                            m["language"] = norm_lang
                            is_missing_language = False
                            updated = True
                            item_hydrated = True
                c_country = c_meta.get("country")
                if c_country and not m.get("country"):
                    m["country"] = c_country
                    updated = True
                    item_hydrated = True
                c_yr = database.extract_clean_year(c_meta)
                if is_missing_year and c_yr:
                    m["year"] = str(c_yr)
                    is_missing_year = False
                    updated = True
                    item_hydrated = True
                desc = c_meta.get("description") or c_meta.get("overview")
                if desc and not m.get("description"):
                    m["description"] = desc
                    updated = True
                    item_hydrated = True
                if not m.get("imdbRating") and c_meta.get("imdbRating"):
                    m["imdbRating"] = str(c_meta["imdbRating"])
                    updated = True
                if not m.get("runtime") and c_meta.get("runtime"):
                    m["runtime"] = str(c_meta["runtime"])
                    updated = True
                if not m.get("background") and c_meta.get("background"):
                    m["background"] = c_meta["background"]
                    updated = True
                if not m.get("cast") and c_meta.get("cast"):
                    m["cast"] = list(c_meta["cast"])
                    updated = True
                if not m.get("videos") and c_meta.get("videos"):
                    m["videos"] = c_meta["videos"]
                    updated = True
                for field in ("networks", "production_companies"):
                    if not m.get(field) and c_meta.get(field):
                        m[field] = list(c_meta[field])
                        if field == "networks":
                            is_missing_networks = False
                        updated = True
                if c_meta.get("adult") or c_meta.get("isAdult"):
                    m["adult"] = True
                if item_hydrated:
                    local_hydrated_count += 1

        tt_id = None
        for cid in candidates:
            if is_imdb_id(cid):
                tt_id = str(cid)
                break

        if is_missing_title:
            missing_titles.append(m)
        if (is_missing_title or is_missing_genres or is_missing_year or is_missing_desc or is_missing_cover or is_missing_language or is_missing_networks) and tt_id:
            missing_metadata.append((m, tt_id))

    if local_hydrated_count > 0:
        print(f"[METADATA-HYDRATE] Local SQLite cache hit: hydrated {local_hydrated_count} items with genres/year (0ms, 0 network)")

    # Queue any items that still lack genres or year to polite background hydrator
    if missing_metadata:
        print(f"[METADATA-HYDRATE] Queued {len(missing_metadata)} items for background Cinemeta hydration (queue: {_HYDRATION_QUEUE.qsize() + len(missing_metadata)})")
        for m_item, tt_id in missing_metadata:
            m_type = m_item.get("type") or media_type
            queue_metadata_hydration(tt_id, m_type, item_dict=m_item)

    still_missing = missing_titles
    if not still_missing:
        if database.is_adult_content_hidden():
            items[:] = [m for m in items if not is_adult_item(m)]
        return updated

    import concurrent.futures
    def _hydrate_single_item(m_item):
        m_id = m_item.get("id")
        m_type = m_item.get("type") or media_type

        # 1. Try Cinemeta direct URL (~20-40ms, keep forever)
        if is_imdb_id(m_id):
            c_url = f"https://v3-cinemeta.strem.io/meta/{m_type}/{m_id}.json"
            cm = _get_cached_request(c_url, max_age_hours=METADATA_CACHE_HOURS, timeout=1.5)
            if not cm:
                alt_url = f"https://cinemeta-live.strem.io/meta/{m_type}/{m_id}.json"
                cm = _get_cached_request(alt_url, max_age_hours=METADATA_CACHE_HOURS, timeout=1.5)
            if cm and isinstance(cm.get("meta"), dict):
                meta_dict = cm["meta"]
                if meta_dict.get("name"):
                    m_item["title"] = meta_dict["name"]
                if not m_item.get("medium_cover_image") and meta_dict.get("poster"):
                    m_item["medium_cover_image"] = meta_dict["poster"]
                    m_item["poster"] = meta_dict["poster"]
                if not m_item.get("year") and meta_dict.get("year"):
                    m_item["year"] = str(meta_dict["year"])
                if meta_dict.get("description") and not m_item.get("description"):
                    m_item["description"] = meta_dict["description"]
                if meta_dict.get("imdbRating") and not m_item.get("imdbRating"):
                    m_item["imdbRating"] = str(meta_dict["imdbRating"])
                if meta_dict.get("runtime") and not m_item.get("runtime"):
                    m_item["runtime"] = str(meta_dict["runtime"])
                if meta_dict.get("background") and not m_item.get("background"):
                    m_item["background"] = meta_dict["background"]
                if meta_dict.get("adult") or meta_dict.get("isAdult"):
                    m_item["adult"] = True
                if meta_dict.get("certification"):
                    m_item["certification"] = meta_dict["certification"]
                if meta_dict.get("country") and not m_item.get("country"):
                    m_item["country"] = meta_dict["country"]

        # 2. If still missing, try fetch_movie_details
        if not m_item.get("title") or m_item.get("title") == "Unknown":
            det = fetch_movie_details(m_id, m_type, use_cache=True)
            if det and det.get("title") and det.get("title") != "Media Item":
                m_item["title"] = det["title"]
                if not m_item.get("medium_cover_image") and det.get("medium_cover_image"):
                    m_item["medium_cover_image"] = det["medium_cover_image"]
                    m_item["poster"] = det["medium_cover_image"]
                if not m_item.get("year") and det.get("year"):
                    m_item["year"] = str(det["year"])
                if det.get("description") and not m_item.get("description"):
                    m_item["description"] = det["description"]
                if det.get("imdbRating") and not m_item.get("imdbRating"):
                    m_item["imdbRating"] = str(det["imdbRating"])
                if det.get("runtime") and not m_item.get("runtime"):
                    m_item["runtime"] = str(det["runtime"])
                if det.get("background") and not m_item.get("background"):
                    m_item["background"] = det["background"]
                if det.get("adult") or det.get("isAdult"):
                    m_item["adult"] = True
                if det.get("certification"):
                    m_item["certification"] = det["certification"]
                if det.get("country") and not m_item.get("country"):
                    m_item["country"] = det["country"]

        # Fast TMDb check for adult flag if still unknown and IMDb ID
        if database.is_adult_content_hidden() and not m_item.get("adult") and is_imdb_id(m_id):
            try:
                from . import tmdb_helper
                t_info = tmdb_helper.fetch_title_language_and_genres(m_id, m_type, m_item.get("title"))
                if t_info and t_info.get("adult"):
                    m_item["adult"] = True
            except Exception:
                pass

        # Save to metadata_cache so subsequent views are instant
        if m_item.get("title") and m_item.get("title") != "Unknown":
            try:
                cached_rec = database.get_cached_metadata(m_id, m_type) or {}
                cached_rec["title"] = m_item["title"]
                if m_item.get("medium_cover_image"):
                    cached_rec["medium_cover_image"] = m_item["medium_cover_image"]
                    cached_rec["poster"] = m_item["medium_cover_image"]
                if m_item.get("year"):
                    cached_rec["year"] = m_item["year"]
                if m_item.get("description"):
                    cached_rec["description"] = m_item["description"]
                if m_item.get("imdbRating"):
                    cached_rec["imdbRating"] = m_item["imdbRating"]
                if m_item.get("runtime"):
                    cached_rec["runtime"] = m_item["runtime"]
                if m_item.get("background"):
                    cached_rec["background"] = m_item["background"]
                if m_item.get("adult"):
                    cached_rec["adult"] = True
                if m_item.get("certification"):
                    cached_rec["certification"] = m_item["certification"]
                if m_item.get("country"):
                    cached_rec["country"] = m_item["country"]
                cached_rec["id"] = m_id
                database.save_cached_metadata(m_id, m_type, cached_rec)
            except Exception:
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(still_missing), 12)) as ex:
        list(ex.map(_hydrate_single_item, still_missing))

    if database.is_adult_content_hidden():
        items[:] = [m for m in items if not is_adult_item(m)]

    return True

def is_valid_meta(res):
    if not res or not isinstance(res, dict):
        return False
    title = str(res.get("title") or res.get("name") or "").strip()
    if not title:
        return False
    lower_title = title.lower()
    if "error getting meta" in lower_title or lower_title.startswith("error ") or lower_title == "failed to load details":
        return False
    if lower_title == "media item" and not res.get("description"):
        return False
    if "synopsis temporarily unavailable" in str(res.get("description", "")).lower() and not res.get("videos"):
        return False
    if str(res.get("id", "")).startswith("ctmdb.") and not res.get("videos"):
        return False

    # Check if title is literally just the raw item ID (e.g. "tt43439241" or "bpx-headline")
    lower_id = str(res.get("id", "")).lower()
    if lower_title == lower_id or re.match(r'^(tt\d{7,10}|bpx-[\w-]+)$', lower_title):
        has_real_desc = bool(res.get("description")) and res.get("description") not in (
            "No synopsis available.", "Synopsis temporarily unavailable.", "HD"
        )
        has_videos = bool(res.get("videos"))
        if not (has_real_desc or has_videos):
            return False

    # Reject junk descriptions that are just dummy radio/placeholder streams
    desc = str(res.get("description", ""))
    if desc.startswith("Stream tt") and "live - Indian FM Radio" in desc:
        return False

    # Incomplete stubs lacking substantive details (no real synopsis/overview, no runtime, no genres, no cast, and no videos) are invalid
    desc_val = str(res.get("description") or res.get("overview") or "").strip()
    has_real_desc = bool(desc_val) and desc_val not in (
        "No synopsis available.", "Synopsis temporarily unavailable.", "HD"
    )
    has_runtime = bool(res.get("runtime"))
    has_genres = bool(res.get("genre") or res.get("genres"))
    has_cast = bool(res.get("cast"))
    has_videos = bool(res.get("videos"))
    if not (has_real_desc or has_runtime or has_genres or has_cast or has_videos):
        return False

    return True

def merge_metadata(base, extra):
    """Merge metadata from multiple providers (e.g. Cinemeta episodes + Addon synopsis/poster/genres)."""
    if not base:
        return extra
    if not extra:
        return base
    merged = dict(base)

    b_title = str(base.get("title", "")).strip()
    e_title = str(extra.get("title", "")).strip()
    if (not b_title or b_title.lower() == str(base.get("id", "")).lower() or re.match(r'^(tt\d{7,10}|bpx-[\w-]+)$', b_title.lower())) and e_title:
        merged["title"] = e_title

    b_desc = str(base.get("description", "")).strip()
    e_desc = str(extra.get("description", "")).strip()
    if (not b_desc or b_desc in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD")) and e_desc and e_desc not in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD"):
        merged["description"] = e_desc

    if not merged.get("videos") and extra.get("videos"):
        merged["videos"] = extra["videos"]

    if not merged.get("medium_cover_image") and extra.get("medium_cover_image"):
        merged["medium_cover_image"] = extra["medium_cover_image"]
    if not merged.get("background") and extra.get("background"):
        merged["background"] = extra["background"]

    if (not merged.get("genre") or not merged.get("genres")) and (extra.get("genre") or extra.get("genres")):
        if extra.get("genre"): merged["genre"] = extra["genre"]
        if extra.get("genres"): merged["genres"] = extra["genres"]

    if not merged.get("year") and extra.get("year"):
        merged["year"] = extra["year"]
    if not merged.get("imdbRating") and extra.get("imdbRating"):
        merged["imdbRating"] = extra["imdbRating"]
    if not merged.get("certification") and extra.get("certification"):
        merged["certification"] = extra["certification"]
    if not merged.get("adult") and (extra.get("adult") or extra.get("isAdult")):
        merged["adult"] = True
    if not merged.get("trailer") and extra.get("trailer"):
        merged["trailer"] = extra["trailer"]
    if not merged.get("director") and extra.get("director"):
        merged["director"] = extra["director"]
    if not merged.get("language") and extra.get("language"):
        merged["language"] = extra["language"]
    if not merged.get("language_code") and extra.get("language_code"):
        merged["language_code"] = extra["language_code"]

    return merged

def enrich_metadata_with_language_and_genres(res, imdb_id, media_type="movie", title=None):
    """
    Enriches metadata dictionary with title language and canonical genres from TMDB.
    For non-English titles, the canonical language name (e.g. 'Hindi') is prepended to genres
    matching IMDb title page badge conventions.
    """
    if not res or not isinstance(res, dict):
        return res

    item_id = res.get("id") or imdb_id
    item_title = res.get("title") or title
    try:
        from . import tmdb_helper
        lang_info = tmdb_helper.fetch_title_language_and_genres(item_id, media_type, item_title)
        if lang_info:
            if lang_info.get("adult"):
                res["adult"] = True
            lang_name = lang_info.get("language_name")
            lang_code = lang_info.get("language_code")
            if lang_name:
                res["language"] = lang_name
            if lang_code:
                res["language_code"] = lang_code

            # Combine existing genres with TMDB genres
            existing_genres = res.get("genres") or []
            if isinstance(existing_genres, str):
                existing_genres = [g.strip() for g in existing_genres.split(",") if g.strip()]
            elif not isinstance(existing_genres, list):
                existing_genres = []

            seen_lower = set(g.lower() for g in existing_genres)
            for tg in lang_info.get("genres", []):
                if tg and tg.lower() not in seen_lower:
                    existing_genres.append(tg)
                    seen_lower.add(tg.lower())

            # For non-English titles, prepend canonical language genre matching IMDb chips
            if lang_name and str(lang_name).strip().lower() != "english":
                clean_lang = str(lang_name).strip()
                if clean_lang.lower() not in seen_lower:
                    existing_genres.insert(0, clean_lang)
                    seen_lower.add(clean_lang.lower())
                elif existing_genres and existing_genres[0].lower() != clean_lang.lower():
                    existing_genres = [g for g in existing_genres if g.lower() != clean_lang.lower()]
                    existing_genres.insert(0, clean_lang)

            if existing_genres:
                res["genres"] = existing_genres
                res["genre"] = ", ".join(existing_genres)
    except Exception as e:
        print(f"[METADATA] Failed to enrich language and genres for {item_id}: {e}")

    return res

def _save_and_return_meta(res, imdb_id, media_type="movie", title=None, poster=None):
    if not res or not isinstance(res, dict):
        return res

    # Preserve existing valid poster from parameter or database cache before replacing with addon poster
    existing = database.get_cached_metadata(imdb_id)
    existing_poster = poster or (existing.get("medium_cover_image") if existing else None)

    if existing_poster:
        res["medium_cover_image"] = existing_poster
    else:
        current_poster = res.get("medium_cover_image", "")
        if not current_poster and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
            effective_title = title or res.get("title") or res.get("name")
            # Run fallback poster fetchers in parallel with short timeout
            def _fetch_tmdb_poster():
                try:
                    c_type = "tv" if media_type in ["series", "anime", "tv"] else "movie"
                    from .tmdb_helper import resolve_to_tmdb_id, get_tmdb_api_key
                    t_id = resolve_to_tmdb_id(imdb_id, media_type=media_type, title=effective_title)
                    if t_id:
                        api_key = get_tmdb_api_key()
                        if api_key:
                            url = f"https://api.themoviedb.org/3/{c_type}/{t_id}?api_key={api_key}"
                            t_data = _get_cached_request(url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
                            if t_data and t_data.get("poster_path"):
                                p_url = f"https://image.tmdb.org/t/p/w500{t_data['poster_path']}"
                                b_url = f"https://image.tmdb.org/t/p/w1280{t_data['backdrop_path']}" if t_data.get("backdrop_path") else None
                                return p_url, b_url
                        tmdb_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/tmdb:{t_id}.json"
                        tmdb_data = _get_cached_request(tmdb_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
                        if tmdb_data and "meta" in tmdb_data and tmdb_data["meta"].get("poster"):
                            return tmdb_data["meta"]["poster"], tmdb_data["meta"].get("background")
                except Exception:
                    pass
                return None, None

            def _fetch_imdb_poster():
                if not effective_title:
                    return None, None
                try:
                    from .tmdb_helper import normalize_title_variants, is_similar_title
                    variants = normalize_title_variants(effective_title)
                    for v in variants:
                        clean_title = re.sub(r'[^a-zA-Z0-9]', '_', v).lower()
                        clean_title = re.sub(r'_+', '_', clean_title).strip('_')
                        if not clean_title:
                            continue
                        first_char = clean_title[0]
                        imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(clean_title)}.json"
                        req = urllib.request.Request(imdb_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                        with urllib.request.urlopen(req, timeout=2.5, context=_SSL_CONTEXT) as response:
                            data = json.loads(response.read().decode('utf-8', errors='ignore'))
                            if data and "d" in data:
                                for item in data["d"]:
                                    if "i" in item and "imageUrl" in item["i"]:
                                        m_name = item.get("l", "")
                                        if is_similar_title(v, m_name):
                                            p_url = item["i"]["imageUrl"]
                                            p_url = re.sub(r'\._V1_.*?\.(jpg|png)', r'._V1_UX400_.jpg', p_url)
                                            return p_url, p_url
                except urllib.error.HTTPError as e:
                    try:
                        e.close()
                    except Exception:
                        pass
                except Exception:
                    pass
                return None, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f_tmdb = executor.submit(_fetch_tmdb_poster)
                f_imdb = executor.submit(_fetch_imdb_poster)
                for f in [f_tmdb, f_imdb]:
                    try:
                        p, b = f.result(timeout=2.5)
                        if p and not res.get("medium_cover_image"):
                            res["medium_cover_image"] = p
                            if b and not res.get("background"):
                                res["background"] = b
                            break
                    except Exception:
                        pass

    if not res.get("trailer"):
        res_tr = extract_trailer_id(res) or (extract_trailer_id(existing) if existing else None)
        if res_tr:
            res["trailer"] = res_tr

    if not res.get("trailer") and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
        try:
            c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
            cm_url = f"https://v3-cinemeta.strem.io/meta/{c_type}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
            cm_data = _get_cached_request(cm_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
            if cm_data and isinstance(cm_data.get("meta"), dict):
                tr_id = extract_trailer_id(cm_data["meta"])
                if tr_id:
                    res["trailer"] = tr_id
        except Exception:
            pass

    if existing and existing.get("background") and not res.get("background"):
        res["background"] = existing["background"]

    # For series/anime/tv: if no videos were returned by any source, synthesize a fallback S1:E1
    if res.get("type") in ["series", "anime", "tv"] and not res.get("videos"):
        ep_title = res.get("title") or title or "Episode 1"
        res["videos"] = [{
            "id": f"{imdb_id}:1:1",
            "season": 1,
            "episode": 1,
            "number": 1,
            "title": ep_title,
            "name": ep_title,
            "overview": res.get("description") if res.get("description") not in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD") else "",
            "thumbnail": res.get("medium_cover_image") or ""
        }]

    desc = str(res.get("description", "")).strip()
    if not desc or desc in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD") or not res.get("background") or not res.get("runtime"):
        try:
            tmdb_extra = fetch_tmdb_standalone_metadata(imdb_id, media_type=media_type, title=title, poster=poster)
            if tmdb_extra:
                res = merge_metadata(res, tmdb_extra)
        except Exception:
            pass

    if not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
        res = enrich_metadata_with_language_and_genres(res, imdb_id, media_type, title)

    if is_valid_meta(res):
        database.save_cached_metadata(imdb_id, media_type, res)
        if res.get("id") and res.get("id") != imdb_id:
            database.save_cached_metadata(res.get("id"), media_type, res)
    return res

def fetch_movie_details(imdb_id, media_type="movie", title=None, use_cache=True, poster=None):
    if isinstance(imdb_id, list):
        if not imdb_id: return {}
        col_id = next((i for i in imdb_id if str(i).startswith('ctmdb.')), None)
        if col_id:
            primary_id = col_id
        else:
            primary_id = next((i for i in imdb_id if str(i).startswith('tt')), imdb_id[0])
        return fetch_movie_details(primary_id, media_type, title, use_cache, poster)

    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else (title or "Radio Stream")
        item_genre = parts[2] if len(parts) > 2 else ""
        item_poster = parts[3] if len(parts) > 3 and parts[3] else (poster or "")
        
        res = {
            "id": imdb_id,
            "title": item_title,
            "year": "",
            "medium_cover_image": item_poster,
            "background": "",
            "description": f"Live Radio / Stream ({item_genre})" if item_genre else "Live Radio / Stream",
            "runtime": "Live",
            "genre": item_genre,
            "imdbRating": "",
            "trailer": None,
            "videos": []
        }
        database.save_cached_metadata(imdb_id, media_type, res)
        return res

    if str(imdb_id).startswith("tpb_ctl:"):
        try:
            import base64, json
            raw_b64 = str(imdb_id).split("tpb_ctl:", 1)[1]
            payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
            if payload.get("extra", {}).get("type"):
                media_type = payload["extra"]["type"]
            p_val = payload.get("poster")
            if p_val:
                if not poster: poster = p_val
                tt_m = re.search(r'\b(tt\d{7,8})\b', p_val)
                if tt_m:
                    imdb_id = tt_m.group(1)
            if not title and payload.get("parsedName"):
                title = payload.get("parsedName")
        except Exception:
            pass

    if poster and not (str(imdb_id).startswith("tt") or str(imdb_id).startswith("tmdb:") or str(imdb_id).startswith("ctmdb.")):
        tt_m = re.search(r'\b(tt\d{7,8})\b', str(poster))
        if tt_m:
            imdb_id = tt_m.group(1)

    # Normalize media_type and c_type to standard formats
    str_imdb = str(imdb_id or "")
    if str_imdb.startswith("bolly:s:") or str_imdb.startswith("hub:s:") or ":s:" in str_imdb:
        media_type = "series"
    elif str_imdb.startswith("bolly:m:") or str_imdb.startswith("hub:m:") or ":m:" in str_imdb:
        media_type = "movie"
    elif str_imdb.startswith("ctmdb.") or media_type == "collections":
        media_type = "collections"
    elif media_type in ["series", "tvshow", "tv_series"]:
        media_type = "series"
    elif media_type in ["tv", "channel", "tvchannel"]:
        media_type = "tv"
    elif media_type in ["music", "radio"]:
        media_type = "music"
    elif media_type not in ["movie", "series", "anime", "tv", "channel", "tvchannel", "music", "radio", "collections"]:
        media_type = "movie"

    orig_item_id = str(imdb_id or "").strip()
    is_custom_provider = any(orig_item_id.startswith(p) for p in ["dsx:", "bpx-", "bolly:", "hub:", "tpb_ctl:"])

    # Resolve TMDB ids to IMDB format if needed
    resolved_imdb = resolve_to_imdb_id(imdb_id, media_type, title)
    if is_custom_provider:
        canonical_id = str(resolved_imdb) if resolved_imdb and (str(resolved_imdb).startswith("tt") or str(resolved_imdb).startswith("tmdb:")) and str(resolved_imdb) != orig_item_id else None
        imdb_id = orig_item_id
    else:
        imdb_id = resolved_imdb
        canonical_id = None

    cached = database.get_cached_metadata(imdb_id) if imdb_id else None
    if not cached and canonical_id:
        cached = database.get_cached_metadata(canonical_id)
    if use_cache and cached and is_valid_meta(cached):
        desc = cached.get("description") or cached.get("overview")
        has_real_desc = bool(desc) and desc not in ("No synopsis available.", "Synopsis temporarily unavailable.", "HD")
        has_videos = bool(cached.get("videos")) if media_type in ["series", "anime", "tv"] else True
        if has_real_desc and has_videos:
            if not cached.get("trailer"):
                cached_tr = extract_trailer_id(cached)
                if cached_tr:
                    cached["trailer"] = cached_tr
                    database.save_cached_metadata(imdb_id, media_type, cached)
            if not cached.get("language") and not (str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://")):
                cached = enrich_metadata_with_language_and_genres(cached, imdb_id, media_type, title)
                database.save_cached_metadata(imdb_id, media_type, cached)
            if poster and not cached.get("medium_cover_image"):
                cached["medium_cover_image"] = poster
                database.save_cached_metadata(imdb_id, media_type, cached)
            elif poster and cached.get("medium_cover_image"):
                cached["medium_cover_image"] = poster
            return cached

    if media_type in ["tv", "channel", "tvchannel"]:
        for addon in database.get_addons():
            if addon.get("id") == "local.iptv-org":
                channels_data = _get_cached_request("https://iptv-org.github.io/api/channels.json", max_age_hours=24)
                ch = next((c for c in channels_data if isinstance(c, dict) and c.get("id") == imdb_id), None) if isinstance(channels_data, list) else None
                if ch:
                    res = {
                        "id": ch.get("id"),
                        "title": ch.get("name"),
                        "year": "",
                        "medium_cover_image": ch.get("logo", ""),
                        "background": "",
                        "description": f"Live TV Channel from {ch.get('country')}. Categories: {', '.join(ch.get('categories', []))}",
                        "runtime": "Live",
                        "genre": ", ".join(ch.get("categories", [])),
                        "imdbRating": "",
                        "trailer": None,
                        "videos": []
                    }
                    database.save_cached_metadata(imdb_id, media_type, res)
                    return res

    c_type = "series" if media_type in ["series", "anime"] else ("tv" if media_type in ["tv", "channel", "tvchannel"] else ("music" if media_type in ["music", "radio"] else "movie"))

    def fetch_addon_meta(addon_orig, req_type=None, use_cache=True, query_id=None):
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        target_m_type = req_type or c_type
        if not addon.get("enabled", True): return None
        m_url = addon.get("manifest_url", "")
        if not m_url or m_url.startswith("builtin:"): return None
        if addon.get("id") == "local.iptv-org": return None
        
        resources = addon.get("resources")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if resources is None or addon_types is None or ("idPrefixes" not in addon):
            try:
                manifest_data = _get_cached_request(m_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass
                
        matched_type = target_m_type
        target_id = query_id or imdb_id
        if str(target_id).startswith("ctmdb."):
            matched_type = "movie"
        elif addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, target_m_type)), None)
            if type_match:
                matched_type = type_match
            else:
                has_cat_match = any(is_type_match(cat.get("type"), target_m_type) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(target_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return None
            
        if addon_prefixes is not None and not query_id:
            if not any(str(target_id).startswith(p) for p in addon_prefixes):
                if canonical_id and any(str(canonical_id).startswith(p) for p in addon_prefixes):
                    target_id = canonical_id
                else:
                    return None
        elif addon_prefixes is not None and query_id:
            if not any(str(target_id).startswith(p) for p in addon_prefixes):
                return None
                
        if resources is not None:
            has_meta = False
            for r in resources:
                if isinstance(r, str) and r == "meta":
                    has_meta = True
                elif isinstance(r, dict) and r.get("name") == "meta":
                    has_meta = True
            if not has_meta:
                return None
        
        base_url = m_url.rsplit("manifest.json", 1)[0] if "manifest.json" in m_url else m_url
        if not base_url.endswith("/"): base_url += "/"
        
        meta_url = f"{base_url}meta/{matched_type}/{urllib.parse.quote(str(target_id), safe=':')}.json"
        meta_hours = METADATA_CACHE_HOURS if use_cache else 0
        data = _get_cached_request(meta_url, max_age_hours=meta_hours)
        if not data and "v3-cinemeta.strem.io" in meta_url:
            alt_url = meta_url.replace("v3-cinemeta.strem.io", "cinemeta-live.strem.io")
            data = _get_cached_request(alt_url, max_age_hours=meta_hours)
        
        if data and data.get("meta"):
            cm = data["meta"]
            t_val = cm.get("name", "") or title or ""
            if not t_val and cm.get("videos"):
                t_val = str(target_id)
            if not t_val or "error getting meta" in t_val.lower() or t_val.lower().startswith("error"):
                return None

            is_channel_meta = (
                matched_type in ["channel", "channels"]
                or str(target_id).startswith("yt_id:")
                or not any(v.get("episode") is not None or v.get("number") is not None for v in cm.get("videos", []))
            )
            videos = []
            for idx, v in enumerate(cm.get("videos", [])):
                if is_channel_meta:
                    ep_num = v.get("episode") or v.get("number") or (idx + 1)
                    ep_title = v.get("title") or v.get("name") or f"Video {idx + 1}"
                else:
                    ep_num = v.get("episode") or v.get("number") or (idx + 1)
                    ep_title = v.get("title") or v.get("name") or f"Episode {ep_num}"
                videos.append({
                    "id": v.get("id", ""),
                    "season": v.get("season", 1),
                    "episode": ep_num,
                    "number": ep_num,
                    "title": ep_title,
                    "name": ep_title,
                    "overview": v.get("overview", ""),
                    "released": v.get("released") or v.get("publishedAt") or v.get("firstAired", ""),
                    "thumbnail": v.get("thumbnail") or v.get("thumbnailHigh") or v.get("thumbnailMax", "")
                })
            
            true_id = cm.get("imdb_id") or target_id
            if str(true_id).startswith("tmdb:") and not str(true_id).startswith("ctmdb.") and not cm.get("videos"):
                m_title = cm.get("name")
                if m_title:
                    try:
                        search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(m_title)}.json"
                        search_data = _get_cached_request(search_url, max_age_hours=METADATA_CACHE_HOURS)
                        if search_data and "metas" in search_data:
                            for m in search_data["metas"]:
                                m_id = m.get("imdb_id") or m.get("id", "")
                                if str(m_id).startswith("tt") and str(m.get("name", "")).lower() == str(m_title).lower():
                                    if str(m.get("releaseInfo", "")).split("-")[0] == str(cm.get("releaseInfo", "")).split("-")[0]:
                                        true_id = m_id
                                        break
                                    elif not cm.get("releaseInfo"):
                                        true_id = m_id
                                        break
                    except Exception:
                        pass
                        
            extracted_trailer = extract_trailer_id(cm)
            cert = cm.get("certification") or cm.get("contentRating") or cm.get("rating") or cm.get("mpaa") or cm.get("ageRating") or cm.get("certification_badge") or ""
            if isinstance(cert, list): cert = ", ".join(cert)

            res_dict = {
                "id": true_id,
                "title": cm.get("name") or title or (t_val if cm.get("videos") else ""),
                "year": str(cm.get("releaseInfo", "")).split("-")[0] if cm.get("releaseInfo") else "",
                "medium_cover_image": cm.get("poster") or cm.get("thumbnail", ""),
                "background": cm.get("background", ""),
                "description": cm.get("description", "No synopsis available."),
                "runtime": cm.get("runtime", ""),
                "genre": ", ".join(cm.get("genres", [])) if isinstance(cm.get("genres"), list) else str(cm.get("genres", "")),
                "certification": str(cert).strip(),
                "imdbRating": str(cm.get("imdbRating", "")),
                "trailer": extracted_trailer,
                "videos": videos,
                "cast": cm.get("cast", []),
                "genres": cm.get("genres", []),
                "type": "collections" if str(target_id).startswith("ctmdb.") else (cm.get("type") or matched_type),
                "adult": cm.get("adult") or cm.get("isAdult") or False
            }
            if database.is_adult_content_hidden() and is_adult_item(res_dict):
                print(f"[SAFE SEARCH] Adult content blocked: {res_dict.get('title')}")
                return None
            return res_dict
            
        return None

    addons = database.get_addons()
    cinemeta_addon = next((a for a in addons if a.get("id") == "cinemeta" or "cinemeta" in a.get("manifest_url", "").lower()), None)
    
    cinemeta_res = None
    cinemeta_query_id = canonical_id or (imdb_id if str(imdb_id).startswith("tt") else None)
    if cinemeta_addon and cinemeta_query_id:
        cinemeta_res = fetch_addon_meta(cinemeta_addon, c_type, use_cache=use_cache, query_id=cinemeta_query_id)
        if (not cinemeta_res or not is_valid_meta(cinemeta_res)) and str(cinemeta_query_id).startswith("tt"):
            alt_type = "movie" if c_type in ["series", "anime", "tv"] else "series"
            alt_res = fetch_addon_meta(cinemeta_addon, alt_type, use_cache=use_cache, query_id=cinemeta_query_id)
            if alt_res and is_valid_meta(alt_res):
                cinemeta_res = alt_res
                media_type = alt_type

    # Determine if cinemeta_res has complete metadata
    is_cinemeta_complete = False
    if cinemeta_res and is_valid_meta(cinemeta_res) and not is_custom_provider:
        has_desc = bool(cinemeta_res.get("description")) and cinemeta_res.get("description") not in (
            "No synopsis available.", "Synopsis temporarily unavailable.", "HD"
        )
        has_videos = bool(cinemeta_res.get("videos")) if c_type in ["series", "anime", "tv"] else True
        has_poster = bool(cinemeta_res.get("medium_cover_image") or poster)
        has_genres = bool(cinemeta_res.get("genre") or cinemeta_res.get("genres"))
        if has_desc and has_videos and has_poster and has_genres:
            is_cinemeta_complete = True

    if is_cinemeta_complete:
        return _save_and_return_meta(cinemeta_res, imdb_id, media_type, title, poster=poster)

    # If Cinemeta failed or was incomplete, fallback to / enrich from other metadata-supporting addons
    if is_custom_provider:
        other_addons = [a for a in addons if a != cinemeta_addon and has_meta_resource(a, media_type=c_type, item_id=orig_item_id)]
    else:
        other_addons = [a for a in addons if a != cinemeta_addon and has_meta_resource(a, media_type=c_type, item_id=imdb_id)]
    if other_addons:
        def _addon_meta_priority(a):
            score = 0
            prefixes = a.get("idPrefixes") or []
            m_url = a.get("manifest_url", "").lower()
            a_id = a.get("id", "").lower()
            if any(str(imdb_id).startswith(p) for p in prefixes):
                score += 30
            elif canonical_id and any(str(canonical_id).startswith(p) for p in prefixes):
                score += 20
            if "bpx-" in str(imdb_id) and "banglaplex" in m_url:
                score += 50
            elif "banglaplex" in m_url:
                score += 25
            elif "tmdb" in a_id or "tmdb" in m_url:
                score += 15
            if "javpie" in m_url or "mediafusion" in m_url:
                score -= 30
            return -score

        other_addons.sort(key=_addon_meta_priority)

        import concurrent.futures
        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(other_addons), 8))
            future_to_addon = {executor.submit(fetch_addon_meta, addon, use_cache=use_cache): addon for addon in other_addons}
        except RuntimeError:
            return cinemeta_res or {}
        best_addon_res = None
        try:
            for future in concurrent.futures.as_completed(future_to_addon, timeout=4.0):
                res = future.result()
                if res and is_valid_meta(res):
                    if not best_addon_res:
                        best_addon_res = res
                    else:
                        best_addon_res = merge_metadata(best_addon_res, res)

                    has_desc = bool(best_addon_res.get("description")) and best_addon_res.get("description") not in (
                        "No synopsis available.", "Synopsis temporarily unavailable.", "HD"
                    )
                    has_vids = bool(best_addon_res.get("videos")) if c_type in ["series", "anime", "tv"] else True
                    if cinemeta_res:
                        merged_check = merge_metadata(best_addon_res, cinemeta_res)
                        if has_desc and (merged_check.get("videos") or c_type not in ["series", "anime", "tv"]):
                            break
                    elif has_desc and has_vids:
                        break
        except concurrent.futures.TimeoutError:
            for future in future_to_addon:
                future.cancel()
        except RuntimeError as e:
            if "shutdown" in str(e).lower() or "interpreter" in str(e).lower():
                return cinemeta_res or {}
            raise
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        if best_addon_res:
            final_res = merge_metadata(best_addon_res, cinemeta_res) if cinemeta_res else best_addon_res
            final_res["id"] = orig_item_id
            if canonical_id and not final_res.get("imdb_id"):
                final_res["imdb_id"] = canonical_id
            return _save_and_return_meta(final_res, imdb_id, media_type, title, poster=poster)

    if cinemeta_res:
        cinemeta_res["id"] = orig_item_id
        if canonical_id and not cinemeta_res.get("imdb_id"):
            cinemeta_res["imdb_id"] = canonical_id
        return _save_and_return_meta(cinemeta_res, imdb_id, media_type, title, poster=poster)

    if str(imdb_id).startswith("ctmdb.") or media_type == "collections":
        try:
            from .tmdb_helper import fetch_collection_details
            col_res = fetch_collection_details(imdb_id, title=title, poster=poster)
            if col_res and is_valid_meta(col_res):
                return _save_and_return_meta(col_res, imdb_id, "collections", title, poster=poster)
        except Exception as e:
            print(f"[COLLECTIONS] Failed to fetch TMDB collection details for {imdb_id}: {e}")

    # Robust TMDB fallback before returning generic unavailable placeholder
    try:
        tmdb_res = fetch_tmdb_standalone_metadata(canonical_id or imdb_id, media_type=c_type, title=title, poster=poster)
        if tmdb_res and is_valid_meta(tmdb_res):
            tmdb_res["id"] = orig_item_id
            if canonical_id and not tmdb_res.get("imdb_id"):
                tmdb_res["imdb_id"] = canonical_id
            return _save_and_return_meta(tmdb_res, imdb_id, media_type, title, poster=poster)
    except Exception:
        pass

    if cached:
        return cached

    return {
        "id": imdb_id,
        "title": title or "Media Item",
        "year": "",
        "medium_cover_image": poster or "",
        "background": "",
        "description": "Synopsis temporarily unavailable.",
        "runtime": "",
        "genre": "",
        "imdbRating": "",
        "trailer": None,
        "videos": [],
        "type": media_type
    }

def fetch_trailer_link_fast(imdb_id, media_type="movie", abort_event=None):
    """
    Quickly query trailer-capable addons (Streailer, TMDB) for a trailer ytId.
    Returns a ytId string on success, or None if nothing found within ~3 seconds.
    Designed to be called in a background thread right after page open.
    """
    if not imdb_id:
        return None

    primary_id = imdb_id[0] if isinstance(imdb_id, list) else imdb_id

    def _aborted():
        return abort_event is not None and abort_event.is_set()

    # 1. Check cached metadata first — trailer may already be known
    try:
        cached = database.get_cached_metadata(primary_id)
        if cached:
            tr = extract_trailer_id(cached)
            if tr:
                return tr
    except Exception:
        pass

    if _aborted():
        return None

    c_type = "series" if media_type in ["series", "anime"] else "movie"

    # 2. Query Cinemeta metadata (authoritative & fast, usually <300ms)
    try:
        cm_url = f"https://v3-cinemeta.strem.io/meta/{c_type}/{urllib.parse.quote(str(primary_id), safe=':')}.json"
        cm_data = _get_cached_request(cm_url, max_age_hours=METADATA_CACHE_HOURS, timeout=2.5)
        if cm_data and isinstance(cm_data.get("meta"), dict):
            tr_id = extract_trailer_id(cm_data["meta"])
            if tr_id:
                try:
                    cached = database.get_cached_metadata(primary_id)
                    if cached and not cached.get("trailer"):
                        cached["trailer"] = tr_id
                        database.save_cached_metadata(primary_id, media_type, cached)
                except Exception:
                    pass
                return tr_id
    except Exception:
        pass

    if _aborted():
        return None

    # 3. Query Streailer and other trailer-capable stream addons
    try:
        all_addons = database.get_addons()
        trailer_addons = [
            a for a in all_addons
            if a.get("enabled", True)
            and not a.get("manifest_url", "").startswith("builtin:")
            and any(kw in a.get("manifest_url", "").lower() or kw in a.get("name", "").lower()
                    for kw in ["streailer", "trailer"])
        ]

        def _fetch_addon_streams(addon):
            if _aborted():
                return None
            try:
                m_url = addon.get("manifest_url", "")
                base_url = m_url.rsplit("manifest.json", 1)[0]
                if not base_url.endswith("/"):
                    base_url += "/"
                stream_url = f"{base_url}stream/{c_type}/{urllib.parse.quote(str(primary_id), safe=':')}.json"
                data = _get_cached_request(stream_url, max_age_hours=2, timeout=3.0)
                if data and isinstance(data.get("streams"), list):
                    for s in data["streams"]:
                        if isinstance(s, dict):
                            yt = s.get("ytId")
                            if yt:
                                return yt
                            bh = s.get("behaviorHints", {})
                            if isinstance(bh, dict) and bh.get("bingeGroup") == "trailer":
                                url = s.get("url", "")
                                if "youtube.com" in url or "youtu.be" in url:
                                    return url
            except Exception:
                pass
            return None

        if trailer_addons:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(trailer_addons), 4)) as executor:
                futures = {executor.submit(_fetch_addon_streams, a): a for a in trailer_addons}
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=3.5):
                        if _aborted():
                            break
                        result = future.result()
                        if result:
                            return result
                except concurrent.futures.TimeoutError:
                    pass
    except Exception:
        pass

    return None


def find_matching_file_index(files, season=None, episode=None, title=None, strict=False):
    import re
    if not files:
        return None

    def _get_fname(f):
        for key in ("path", "name", "filename"):
            val = f.get(key)
            if val:
                return val[0].lower() if isinstance(val, list) else str(val).lower()
        return ""

    video_exts = ('.mkv', '.mp4', '.avi', '.m4v', '.webm', '.ts')

    # 1. If season and episode are specified, try standard SxxExx and episode patterns
    if season is not None and episode is not None:
        try:
            s_num = int(season)
            ep_num = int(episode)
            patterns_priority = [
                rf"s{s_num:02d}e{ep_num:02d}\b",
                rf"s{s_num}e{ep_num}\b",
                rf"{s_num}x{ep_num:02d}\b",
                rf"{s_num}x{ep_num}\b",
            ]
            patterns_fallback = [
                rf"ep(?:isode)?[\s._-]*{ep_num:02d}\b",
                rf"ep(?:isode)?[\s._-]*{ep_num}\b",
                rf"(?:^|[\s._\-\[/]){ep_num:02d}(?:[\s._\-\]\/]|$)",
            ]
            for idx, f in enumerate(files):
                fname = _get_fname(f)
                if not any(fname.endswith(ext) for ext in video_exts): continue
                for p in patterns_priority:
                    if re.search(p, fname): return idx

            for idx, f in enumerate(files):
                fname = _get_fname(f)
                if not any(fname.endswith(ext) for ext in video_exts): continue
                for p in patterns_fallback:
                    if re.search(p, fname): return idx
        except (ValueError, TypeError):
            pass

    # 2. If episode title is provided (or collection movie title), match significant words
    if title and isinstance(title, str):
        clean_title = re.sub(r'[\(\)\[\]{}._\-:,!]', ' ', title).lower().strip()
        stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with", "part", "episode", "movie", "collection"}
        tokens = [w for w in clean_title.split() if len(w) >= 3 and w not in stop_words]
        if tokens:
            best_idx = None
            best_matches = 0
            for idx, f in enumerate(files):
                fname = _get_fname(f)
                if not any(fname.endswith(ext) for ext in video_exts): continue
                clean_fname = re.sub(r'[\(\)\[\]{}._\-:,!]', ' ', fname).lower()
                matches = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", clean_fname))
                if matches > best_matches and (matches >= len(tokens) or matches >= 2):
                    best_matches = matches
                    best_idx = idx
            if best_idx is not None:
                return best_idx

    # 3. If only episode (part) number is provided (e.g. anime or collection part)
    if episode is not None:
        try:
            ep_num = int(episode)
            ep_patterns = [
                rf"\bpart[\s._-]*{ep_num}\b",
                rf"\bep(?:isode)?[\s._-]*{ep_num:02d}\b",
                rf"\bep(?:isode)?[\s._-]*{ep_num}\b",
                rf"(?:^|[\s._\-\[/]){ep_num:02d}(?:[\s._\-\]\/]|$)",
            ]
            for idx, f in enumerate(files):
                fname = _get_fname(f)
                if not any(fname.endswith(ext) for ext in video_exts): continue
                for p in ep_patterns:
                    if re.search(p, fname): return idx
        except (ValueError, TypeError):
            pass

    if strict:
        return None

    # Fallback: largest video file
    video_files = []
    for idx, f in enumerate(files):
        fname = _get_fname(f)
        if any(fname.endswith(ext) for ext in video_exts):
            size_list = f.get("size")
            size = size_list[0] if isinstance(size_list, list) else (int(size_list) if size_list is not None else 0)
            video_files.append((idx, size))
    if video_files:
        return max(video_files, key=lambda x: x[1])[0]

    return None

def find_episode_file_index(files, season, episode, strict=False):
    return find_matching_file_index(files, season=season, episode=episode, strict=strict)

def get_stream_cache_key(imdb_id, media_type="movie", season=None, episode=None):
    primary = imdb_id[0] if isinstance(imdb_id, list) else imdb_id
    primary = str(primary or "")
    if season is not None and episode is not None:
        if ":" in primary:
            parts = primary.split(":")
            if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
                primary = ":".join(parts[:-2])
            elif len(parts) == 2 and parts[-1].isdigit() and parts[0].startswith("tt"):
                primary = parts[0]
        return f"{primary}:S{season}:E{episode}"
    return f"{primary}:{media_type}"

def _extract_quality(text):
    """Extract video quality label and numeric rank from text. Hoisted to avoid re-creation per stream."""
    import re
    t = str(text).lower()
    if "2160p" in t or re.search(r'\b4k\b', t): return "4K", 4
    if "1080p" in t or re.search(r'\b1080\b', t): return "1080p", 3
    if "720p" in t or re.search(r'\b720\b', t): return "720p", 2
    if "480p" in t or re.search(r'\b480\b', t): return "480p", 1
    if "360p" in t or re.search(r'\b360\b', t): return "360p", 0
    return None, 0

_RE_SIZE = re.compile(
    r'(?:💾|📦|[Ss]ize[:\s]*|\b)([\d.]+)\s*([TtGgMmKk][iI]?[Bb])(?!\s*(?:[pP][sS]|/[sS]|[iI][tT][sS]?|[bB][pP][sS]))\b'
)
_RE_BITRATE = re.compile(
    r'~?([\d.]+)\s*([MmKkGg]bps|[MmKkGg]b/s|[MmKkGg]bit/s)\b', re.IGNORECASE
)
_RE_SEED = re.compile(r'(?:👤|👥|\b[Ss]eeders?[:\s]*|\b[Ss]eeds?[:\s]*)\s*(\d+)')

MATCH_EXACT = 2
MATCH_SEASON_PACK = 1
MATCH_UNKNOWN = 0
MATCH_MISMATCH = -1

_RE_SXX_EXX = re.compile(
    r'\bS(\d{1,2})[\s._-]*E(\d{1,3})(?:[\s._-]*(?:E|[-~])(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_SEASON_EPISODE = re.compile(
    r'\bseason[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep)[\s._-]*(\d{1,3})(?:[\s._-]*(?:-|to)[\s._-]*(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_NXMM = re.compile(
    r'(?<!\d)(\d{1,2})x(\d{1,3})(?:[\s._-]*[-~][\s._-]*(\d{1,3}))?(?!\d|p\b)',
    re.IGNORECASE
)
_RE_STANDALONE_EP = re.compile(
    r'\b(?:episode|ep)[\s._-]*(\d{1,3})(?:[\s._-]*(?:-|to)[\s._-]*(\d{1,3}))?\b',
    re.IGNORECASE
)
_RE_ANIME_EP = re.compile(
    r'(?:^|[\s_.-])-\s*(\d{1,3})(?:v\d)?(?:[\s_.-]*(?:-|~)\s*(\d{1,3}))?(?:[\s_.-]+(?:\[|\(|\d{3,4}p|web|bd|dvd|flac|aac|x264|x265|hevc|mkv|mp4)|\.mkv|\.mp4|$)',
    re.IGNORECASE
)
_RE_SEASON_PACK = re.compile(
    r'\b(?:season[\s._-]*(\d{1,2})|s(\d{1,2}))\b(?!\s*e\d)',
    re.IGNORECASE
)

def extract_stream_episode_info(text):
    """
    Extracts (season, episode_start, episode_end, is_season_pack) from text.
    Returns: (int or None, int or None, int or None, bool)
    """
    if not text:
        return None, None, None, False
    t = str(text)

    # 1. SxxExx
    m = _RE_SXX_EXX.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        return s, ep1, ep2, False

    # 2. Season X Episode Y
    m = _RE_SEASON_EPISODE.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        return s, ep1, ep2, False

    # 3. NxMM (e.g. 1x03)
    m = _RE_NXMM.search(t)
    if m:
        s = int(m.group(1))
        ep1 = int(m.group(2))
        ep2 = int(m.group(3)) if m.group(3) else None
        if s < 100 and ep1 < 1000:
            return s, ep1, ep2, False

    # 4. Standalone Ep/Episode
    m = _RE_STANDALONE_EP.search(t)
    if m:
        ep1 = int(m.group(1))
        ep2 = int(m.group(2)) if m.group(2) else None
        m_s = _RE_SEASON_PACK.search(t)
        s = int(m_s.group(1) or m_s.group(2)) if m_s else None
        return s, ep1, ep2, False

    # 5. Anime-style delimiter: " - 03 "
    m = _RE_ANIME_EP.search(t)
    if m:
        ep1 = int(m.group(1))
        ep2 = int(m.group(2)) if m.group(2) else None
        m_s = _RE_SEASON_PACK.search(t)
        s = int(m_s.group(1) or m_s.group(2)) if m_s else None
        return s, ep1, ep2, False

    # 6. Season Pack
    m = _RE_SEASON_PACK.search(t)
    if m:
        s = int(m.group(1) or m.group(2))
        return s, None, None, True

    return None, None, None, False

def get_stream_search_text(stream):
    """Collect all searchable text lines/filenames for a stream."""
    if not isinstance(stream, dict):
        return str(stream or "")
    parts = []
    for k in ("filename", "stream_title", "title", "name"):
        v = stream.get(k)
        if v and isinstance(v, str):
            parts.append(v)
    bh = stream.get("behaviorHints")
    if isinstance(bh, dict):
        for k in ("filename", "videoFilename"):
            v = bh.get(k)
            if v and isinstance(v, str):
                parts.append(v)
    raw_url = stream.get("url") or stream.get("externalUrl")
    if raw_url and isinstance(raw_url, str):
        try:
            unquoted = urllib.parse.unquote(raw_url.split("?")[0].split("#")[0])
            last_segment = unquoted.rstrip("/").rsplit("/", 1)[-1]
            if any(last_segment.lower().endswith(ext) for ext in [".mkv", ".mp4", ".avi", ".webm", ".m4v"]):
                parts.append(last_segment)
        except Exception:
            pass
    return " \n ".join(parts)

def match_stream_to_episode(stream, target_season, target_episode, ep_title=None, series_title=None):
    """
    Evaluates whether a stream corresponds to the requested season and episode.
    Returns:
        MATCH_EXACT (2): Explicit match for target season and episode.
        MATCH_SEASON_PACK (1): Valid season pack containing target season (torrents only).
        MATCH_UNKNOWN (0): Ambiguous stream with no season or episode indicator.
        MATCH_MISMATCH (-1): Explicit indicator of a different episode or different season.
    """
    if target_season is None and target_episode is None:
        return MATCH_UNKNOWN

    try:
        t_season = int(target_season) if target_season is not None else 1
    except (ValueError, TypeError):
        t_season = 1

    try:
        t_episode = int(target_episode) if target_episode is not None else None
    except (ValueError, TypeError):
        t_episode = None

    if t_episode is None:
        return MATCH_UNKNOWN

    text = get_stream_search_text(stream)
    if not text.strip():
        return MATCH_UNKNOWN

    s, ep_start, ep_end, is_pack = extract_stream_episode_info(text)

    # 1. Season mismatch check
    if s is not None and s != t_season:
        return MATCH_MISMATCH

    # 2. Season pack match
    if is_pack:
        is_http = stream.get("is_http") if isinstance(stream, dict) else False
        if is_http:
            return MATCH_UNKNOWN
        return MATCH_SEASON_PACK

    # 3. Explicit episode number matched
    if ep_start is not None:
        if ep_end is not None:
            if ep_start <= t_episode <= ep_end:
                return MATCH_EXACT
            else:
                return MATCH_MISMATCH
        else:
            if ep_start == t_episode:
                return MATCH_EXACT
            else:
                return MATCH_MISMATCH

    # 4. Episode title match (fallback when filename has no SxxExx but has episode title)
    if ep_title and isinstance(ep_title, str):
        clean_ep_title = ep_title.strip()
        if len(clean_ep_title) >= 4 and not re.match(r'^(?:episode|ep|part)\s*\d+$', clean_ep_title, re.IGNORECASE):
            norm_title = re.sub(r'[^a-zA-Z0-9]+', ' ', clean_ep_title).lower().strip()
            norm_text = re.sub(r'[^a-zA-Z0-9]+', ' ', text).lower()
            if norm_title and norm_title in norm_text:
                return MATCH_EXACT

    return MATCH_UNKNOWN

def process_raw_streams(all_streams, season=None, episode=None, ep_title=None):
    if not all_streams:
        return []
    valid_streams = []
    seen_keys = {}  # {dedup_key: index in valid_streams} for O(1) duplicate lookup
    for s in all_streams:
        if check_stream_rate_limit_warning(s):
            continue
        yt_id = str(s.get("ytId") or "").strip()
        raw_stream_url = str(s.get("url") or "")
        external_url = str(s.get("externalUrl") or "")
        info_hash = str(s.get("infoHash") or "").lower()
        source_addon_names = []
        raw_addon_names = s.get("addon_names") or []
        if isinstance(raw_addon_names, str):
            raw_addon_names = [raw_addon_names]
        if isinstance(raw_addon_names, (list, tuple, set)):
            source_addon_names.extend(str(name).strip() for name in raw_addon_names if name)
        if s.get("addon_name"):
            source_addon_names.append(str(s.get("addon_name")).strip())
        source_addon_names = list(dict.fromkeys(name for name in source_addon_names if name))
        
        if yt_id and not raw_stream_url and not external_url:
            raw_stream_url = f"https://www.youtube.com/watch?v={yt_id}"

        is_external = bool(external_url and not raw_stream_url)
        stream_url = raw_stream_url or external_url
        
        is_http = False
        if stream_url.startswith("http://") or stream_url.startswith("https://"):
            if not stream_url.split('?')[0].endswith(".torrent"):
                is_http = True
        elif stream_url.startswith("magnet:") and not info_hash:
            match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', stream_url, re.IGNORECASE)
            if match:
                info_hash = match.group(1).lower()
            
        if not info_hash and not stream_url:
            continue

        raw_id = info_hash if not is_http else hashlib.md5(stream_url.encode()).hexdigest()
        
        desc_str = s.get("title") or s.get("description") or ""
        name_str = s.get("name") or ""
        
        desc_clean = " • ".join([line.strip() for line in desc_str.splitlines() if line.strip()])
        name_clean = " • ".join([line.strip() for line in name_str.splitlines() if line.strip()])
        
        if desc_clean and name_clean and name_clean not in desc_clean:
            full_title = f"{name_clean} - {desc_clean}"
        else:
            full_title = desc_clean or name_clean
            
        title_str = desc_str
        name_and_title = (name_str + " " + title_str)

        dedup_key = f"{raw_id}:{full_title}"
        if dedup_key in seen_keys:
            idx = seen_keys[dedup_key]
            if idx is not None and 0 <= idx < len(valid_streams):
                vs = valid_streams[idx]
                for addon_name in source_addon_names:
                    if addon_name not in vs.get("addon_names", []):
                        vs.setdefault("addon_names", []).append(addon_name)
                if s.get("sources"):
                    existing_sources = vs.setdefault("sources", [])
                    for src in s["sources"]:
                        if src and src not in existing_sources:
                            existing_sources.append(src)
            continue

        quality, q_val = _extract_quality(name_str)
        if not quality:
            quality, q_val = _extract_quality(title_str)
        if not quality:
            quality, q_val = "Unknown", 0
        
        combined_text = f"{title_str} {name_str}"
        size = ""
        size_gb = 0.0
        size_matches = list(_RE_SIZE.finditer(combined_text))
        if size_matches:
            explicit_match = next((m for m in size_matches if any(p in m.group(0) for p in ['💾', '📦', 'ize', 'ize:'])), None)
            chosen_match = explicit_match or size_matches[-1]
            unit = chosen_match.group(2).upper()
            try:
                val = float(chosen_match.group(1))
                size = f"{chosen_match.group(1)} {unit}"
                if "TB" in unit:
                    size_gb = val * 1024.0
                elif "GB" in unit:
                    size_gb = val
                elif "MB" in unit:
                    size_gb = val / 1024.0
                elif "KB" in unit:
                    size_gb = val / (1024.0 * 1024.0)
            except ValueError:
                pass
            
        bitrate = ""
        bitrate_match = _RE_BITRATE.search(combined_text)
        if bitrate_match:
            bitrate = f"{bitrate_match.group(1)} {bitrate_match.group(2)}"
            
        seeders = 0
        if s.get("seeders") is not None:
            try:
                seeders = int(s.get("seeders"))
            except (ValueError, TypeError):
                seeders = 0
        elif s.get("seeds") is not None:
            try:
                seeders = int(s.get("seeds"))
            except (ValueError, TypeError):
                seeders = 0
        if seeders == 0:
            seed_match = _RE_SEED.search(title_str)
            if seed_match:
                try:
                    seeders = int(seed_match.group(1))
                except ValueError:
                    pass

        behavior_hints = s.get("behaviorHints", {})
        filename = behavior_hints.get("filename") or behavior_hints.get("videoFilename")
        if not filename:
            filename = title_str.split('\n')[0] if '\n' in title_str else ""
        if not filename:
            filename = name_and_title.replace('/', '_')

        binge_group = behavior_hints.get("bingeGroup") if isinstance(behavior_hints, dict) else None
        if not binge_group:
            binge_group = s.get("binge_group")
        saved_at = s.get("saved_at") or time.time()
        expires_at = s.get("expires_at") or get_stream_url_expiry_timestamp(stream_url, saved_at=saved_at)
        has_expiring_credentials = bool(
            s.get("has_expiring_credentials")
            or has_likely_expiring_playback_credentials(stream_url)
        )

        is_trailer = bool(
            yt_id
            or s.get("is_trailer")
            or (isinstance(behavior_hints, dict) and behavior_hints.get("bingeGroup") == "trailer")
            or any("trailer" in str(a).lower() or "streailer" in str(a).lower() for a in [s.get("addon_name", ""), s.get("name", ""), s.get("title", "")])
            or "youtube.com" in stream_url.lower() or "youtu.be" in stream_url.lower()
        )
            
        stream_entry = {
            "hash": raw_id,
            "infoHash": info_hash or (raw_id if not is_http else ""),
            "url": stream_url,
            "sources": s.get("sources") or [],
            "externalUrl": external_url,
            "is_external": is_external,
            "is_http": is_http,
            "is_trailer": is_trailer,
            "ytId": yt_id or s.get("ytId", ""),
            "quality": quality,
            "q_val": q_val,
            "size": size,
            "size_gb": size_gb,
            "bitrate": bitrate,
            "seeders": seeders,
            "title": s.get("name") or "",
            "stream_title": full_title,
            "file_index": s.get("fileIdx"),
            "filename": filename,
            "behaviorHints": behavior_hints,
            "binge_group": binge_group,
            "saved_at": saved_at,
            "expires_at": expires_at,
            "has_expiring_credentials": has_expiring_credentials,
            "addon_name": source_addon_names[0] if source_addon_names else "",
            "addon_names": source_addon_names,
        }

        if season is not None and episode is not None:
            ep_match = match_stream_to_episode(stream_entry, season, episode, ep_title)
            # Filter out streams that explicitly belong to a different episode or season!
            if ep_match == MATCH_MISMATCH:
                seen_keys[dedup_key] = None
                continue
            stream_entry["ep_match"] = ep_match
        else:
            stream_entry["ep_match"] = MATCH_UNKNOWN

        seen_keys[dedup_key] = len(valid_streams)
        valid_streams.append(stream_entry)
    
    def _rank_key(x):
        is_http = 1 if x.get("is_http") else 0
        seeds = int(x.get("seeders") or 0)
        has_seeds = 1 if (is_http or seeds > 0) else 0
        ep_m = x.get("ep_match", 0)
        q_val = x.get("q_val", 0)
        size_gb = float(x.get("size_gb") or 0.0)
        seeders = seeds if not is_http else 100
        # q_val: 4=4K, 3=1080p, 2=720p, 1=480p, 0=360p/Unknown
        is_1080p_optimal = (q_val == 3 and ((0 < size_gb <= 4.5) or size_gb == 0.0))
        is_720p_optimal = (q_val == 2 and ((0 < size_gb <= 2.5) or size_gb == 0.0))
        if is_1080p_optimal:
            p_tier = 4
        elif is_720p_optimal:
            p_tier = 3
        elif q_val == 3:
            p_tier = 2
        elif q_val == 4 or q_val == 2:
            p_tier = 1
        else:
            p_tier = 0
        return (has_seeds, ep_m, p_tier, seeders, q_val, size_gb)

    valid_streams.sort(key=_rank_key, reverse=True)
    return valid_streams

def stream_matches_provider(stream, provider_name):
    if not provider_name or not isinstance(stream, dict):
        return False
    p_lower = str(provider_name).strip().lower()
    for an in stream.get("addon_names", []):
        if p_lower in str(an).strip().lower() or str(an).strip().lower() in p_lower:
            return True
    an_single = stream.get("addon_name")
    if an_single and (p_lower in str(an_single).strip().lower() or str(an_single).strip().lower() in p_lower):
        return True
    return False

def streams_match(s1, s2):
    if not s1 or not s2: return False
    if s1 is s2: return True

    is_http1 = bool(s1.get("is_http") or (isinstance(s1.get("url"), str) and s1.get("url", "").startswith(("http://", "https://")) and not s1.get("hash") and not s1.get("infoHash")))
    is_http2 = bool(s2.get("is_http") or (isinstance(s2.get("url"), str) and s2.get("url", "").startswith(("http://", "https://")) and not s2.get("hash") and not s2.get("infoHash")))
    if is_http1 != is_http2:
        return False

    u1, u2 = s1.get("url") or "", s2.get("url") or ""
    if u1 and u2 and u1 == u2:
        return True

    # Same URL path excluding query parameters / session tokens
    if u1 and u2 and u1.startswith("http") and u2.startswith("http"):
        p1 = u1.split("?")[0].rstrip("/")
        p2 = u2.split("?")[0].rstrip("/")
        if p1 == p2 and len(p1) > 20:
            return True

    # Conflicting distinct qualities cannot match
    q1 = (s1.get("quality") or "").strip().lower()
    q2 = (s2.get("quality") or "").strip().lower()
    if q1 and q2 and q1 not in ("unknown", "") and q2 not in ("unknown", "") and q1 != q2:
        return False

    h1 = (s1.get("hash") or s1.get("infoHash") or "").lower()
    h2 = (s2.get("hash") or s2.get("infoHash") or "").lower()
    if h1 and h2 and h1 != h2 and not (u1 and u2 and u1 == u2):
        return False

    if h1 and h2 and h1 == h2:
        idx1 = s1.get("file_index") if s1.get("file_index") is not None else s1.get("fileIdx")
        idx2 = s2.get("file_index") if s2.get("file_index") is not None else s2.get("fileIdx")
        if idx1 is None or idx2 is None or idx1 == idx2:
            return True

    m1 = s1.get("magnet") or ""
    m2 = s2.get("magnet") or ""
    if m1 and m2 and m1 == m2:
        return True

    f1 = (s1.get("filename") or "").strip().lower()
    f2 = (s2.get("filename") or "").strip().lower()
    t1 = (s1.get("stream_title") or s1.get("title") or "").strip().lower()
    t2 = (s2.get("stream_title") or s2.get("title") or "").strip().lower()

    # Provider match
    prov1 = s1.get("addon_names") or ([s1.get("addon_name")] if s1.get("addon_name") else [])
    prov2 = s2.get("addon_names") or ([s2.get("addon_name")] if s2.get("addon_name") else [])
    common_prov = any(stream_matches_provider(s2, p) for p in prov1) or any(stream_matches_provider(s1, p) for p in prov2)

    # Same provider + same filename or title or quality
    if common_prov:
        if f1 and f2 and f1 == f2:
            return True
        if t1 and t2 and t1 == t2:
            return True
        q1 = (s1.get("quality") or "").strip().lower()
        q2 = (s2.get("quality") or "").strip().lower()
        if q1 and q2 and q1 == q2 and (f1 == f2 or t1 == t2 or (t1 and t2 and (t1 in t2 or t2 in t1))):
            return True

    # Same specific filename across streams
    if f1 and f2 and f1 == f2 and len(f1) > 5 and not f1.startswith("stream."):
        return True

    # Same title and no conflicting hashes/urls
    if t1 and t2 and t1 == t2 and not h1 and not h2:
        q1 = (s1.get("quality") or "").strip().lower()
        q2 = (s2.get("quality") or "").strip().lower()
        if not q1 or not q2 or q1 == q2:
            return True

    return False

def find_best_matching_stream(target_stream, candidate_streams, preferred_provider=None, preferred_quality=None):
    if not candidate_streams:
        return None
    if not target_stream or not isinstance(target_stream, dict):
        if preferred_provider:
            prov_matches = [c for c in candidate_streams if stream_matches_provider(c, preferred_provider)]
            if prov_matches:
                if preferred_quality:
                    q_matches = [c for c in prov_matches if (c.get("quality") or "").lower() == str(preferred_quality).lower()]
                    if q_matches:
                        return q_matches[0]
                return prov_matches[0]
        return candidate_streams[0]

    # 1. Exact match via streams_match
    for c in candidate_streams:
        if streams_match(target_stream, c):
            return c

    # 2. Score candidates by similarity to target stream
    target_prov = preferred_provider
    if not target_prov:
        an = target_stream.get("addon_names", [])
        target_prov = an[0] if an else target_stream.get("addon_name")

    target_q = (target_stream.get("quality") or preferred_quality or "").strip().lower()
    target_fn = (target_stream.get("filename") or "").strip().lower()
    target_title = (target_stream.get("stream_title") or target_stream.get("title") or "").strip().lower()
    target_size = float(target_stream.get("size_gb") or 0.0)

    best_score = -1
    best_cand = None

    for c in candidate_streams:
        score = 0
        c_prov_match = stream_matches_provider(c, target_prov)
        if c_prov_match:
            score += 100  # Strongly prioritize same provider

        # Binge group matching
        target_bg = target_stream.get("binge_group") or (target_stream.get("behaviorHints") or {}).get("bingeGroup")
        c_bg = c.get("binge_group") or (c.get("behaviorHints") or {}).get("bingeGroup")
        if target_bg and c_bg and str(target_bg).strip().lower() == str(c_bg).strip().lower():
            score += 150  # Strongly prioritize matching binge group

        c_fn = (c.get("filename") or "").strip().lower()
        if target_fn and c_fn and target_fn == c_fn:
            score += 80
        elif target_fn and c_fn and (target_fn in c_fn or c_fn in target_fn):
            score += 40

        c_title = (c.get("stream_title") or c.get("title") or "").strip().lower()
        if target_title and c_title and target_title == c_title:
            score += 60
        elif target_title and c_title and (target_title in c_title or c_title in target_title):
            score += 30

        c_q = (c.get("quality") or "").strip().lower()
        if target_q and c_q and target_q == c_q:
            score += 40

        c_size = float(c.get("size_gb") or 0.0)
        if target_size > 0 and c_size > 0:
            diff = abs(target_size - c_size) / target_size
            if diff < 0.05:
                score += 30
            elif diff < 0.15:
                score += 15

        if bool(target_stream.get("is_http")) == bool(c.get("is_http")):
            score += 20

        if score > best_score:
            best_score = score
            best_cand = c

    if best_cand and (best_score >= 100 or (best_score >= 40 and not target_prov)):
        return best_cand
    elif best_cand and target_prov and stream_matches_provider(best_cand, target_prov):
        return best_cand

    return None

def refresh_stream(imdb_id, media_type="movie", season=None, episode=None, old_stream=None, provider=None):
    """Refresh a stale stream by fetching fresh links from addons and matching the original stream.
    
    Returns:
        (refreshed_stream, candidate_streams)
    """
    if not imdb_id:
        return old_stream, []
    
    target_provider = provider
    if not target_provider and old_stream and isinstance(old_stream, dict):
        anames = old_stream.get("addon_names", [])
        target_provider = anames[0] if anames else old_stream.get("addon_name")
    
    # 1. Fetch fresh streams. Bypass both the SQLite stream cache and the
    # short-lived URL cache: the whole point of auto-heal is to get a freshly
    # signed debrid URL, and the URL cache would otherwise hand back the
    # exact same expired link.
    #
    # A queue can contain many links that all expired together, so reuse the
    # last refresh result briefly instead of re-querying the provider once
    # per failed link.
    cache_key = (
        str(imdb_id),
        media_type,
        season,
        episode,
        (target_provider or "").lower(),
    )
    now = time.time()
    fresh_streams = None
    with _REFRESH_RESULT_CACHE_LOCK:
        cached = _REFRESH_RESULT_CACHE.get(cache_key)
        if cached and cached[1] > now:
            fresh_streams = cached[0]
        elif cached:
            _REFRESH_RESULT_CACHE.pop(cache_key, None)

    if fresh_streams is None:
        fresh_streams = get_torrents(
            imdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            use_cache=False,
            provider=target_provider,
            bypass_url_cache=True
        )
        if not fresh_streams and target_provider:
            fresh_streams = get_torrents(
                imdb_id,
                media_type=media_type,
                season=season,
                episode=episode,
                use_cache=False,
                bypass_url_cache=True
            )
        if fresh_streams:
            ttl = _REFRESH_RESULT_TTL
        else:
            ttl = _REFRESH_EMPTY_TTL
        with _REFRESH_RESULT_CACHE_LOCK:
            _REFRESH_RESULT_CACHE[cache_key] = (fresh_streams or [], now + ttl)
            if len(_REFRESH_RESULT_CACHE) > 50:
                for key in [k for k, v in _REFRESH_RESULT_CACHE.items() if v[1] <= now]:
                    _REFRESH_RESULT_CACHE.pop(key, None)
                while len(_REFRESH_RESULT_CACHE) > 40:
                    _REFRESH_RESULT_CACHE.pop(next(iter(_REFRESH_RESULT_CACHE)))
    
    if not fresh_streams:
        return old_stream, []
    
    pref_q = (old_stream.get("quality") or old_stream.get("stream_quality")) if isinstance(old_stream, dict) else None
    matched = find_best_matching_stream(old_stream, fresh_streams, preferred_provider=target_provider, preferred_quality=pref_q)
    
    if matched:
        if isinstance(old_stream, dict) and old_stream.get("behaviorHints") and not matched.get("behaviorHints"):
            matched["behaviorHints"] = old_stream["behaviorHints"]
        if isinstance(old_stream, dict) and old_stream.get("binge_group") and not matched.get("binge_group"):
            matched["binge_group"] = old_stream["binge_group"]
        matched["_refreshed"] = True
        return matched, fresh_streams
    
    return old_stream, fresh_streams

def extract_stream_headers(stream_entry, url=None):
    """
    Extract HTTP headers required for streaming from behaviorHints (proxyHeaders, headers, requestHeaders)
    or query parameters in the stream URL.
    """
    headers = {}
    if not isinstance(stream_entry, dict) and not url:
        return headers

    stream_dict = stream_entry if isinstance(stream_entry, dict) else {}
    target_url = url or stream_dict.get("url") or stream_dict.get("magnet") or ""

    bh = stream_dict.get("behaviorHints")
    if isinstance(bh, dict):
        # 1. Stremio proxyHeaders: {"request": {"User-Agent": "...", "Referer": "..."}}
        proxy_headers = bh.get("proxyHeaders")
        if isinstance(proxy_headers, dict):
            req_headers = proxy_headers.get("request")
            if isinstance(req_headers, dict):
                headers.update({str(k): str(v) for k, v in req_headers.items()})
            else:
                for k, v in proxy_headers.items():
                    if k != "request" and isinstance(v, (str, int, float)):
                        headers[str(k)] = str(v)

        # 2. direct headers object: {"headers": {"Referer": "..."}}
        if isinstance(bh.get("headers"), dict):
            headers.update({str(k): str(v) for k, v in bh["headers"].items()})

        # 3. requestHeaders: {"requestHeaders": {...}}
        if isinstance(bh.get("requestHeaders"), dict):
            headers.update({str(k): str(v) for k, v in bh["requestHeaders"].items()})

    # Also check top-level stream_dict["headers"] if present
    if isinstance(stream_dict.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in stream_dict["headers"].items()})

    # Query params fallback: ?User-Agent=...&Referer=...
    if target_url and isinstance(target_url, str):
        try:
            import urllib.parse
            parsed_url = urllib.parse.urlparse(target_url)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            for key in ["User-Agent", "Referer", "Origin", "Cookie"]:
                for qk, qv in query_params.items():
                    if qk.lower() == key.lower() and qv:
                        headers[key] = qv[0]
        except Exception:
            pass

    return headers

def get_torrents(imdb_id, media_type="movie", season=None, episode=None, use_cache=True, provider=None, bypass_url_cache=False):
    if not imdb_id:
        return []
        
    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else "Live Stream"
        return [{
            "url": stream_url,
            "name": "Live Stream",
            "title": item_title,
            "behaviorHints": {"filename": "stream.mp3"}
        }]
        
    cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    if use_cache:
        cached = database.get_cached_streams(cache_key, max_age_hours=24)
        if cached:
            cached = [s for s in cached if not check_stream_rate_limit_warning(s)]
            if cached:
                return cached

    actual_media = media_type
    
    stremio_addons = get_stream_addons(media_type=actual_media, item_id=imdb_id, only_working=True)
    if not stremio_addons:
        stremio_addons = get_stream_addons(media_type=actual_media, item_id=imdb_id, only_working=False)
    if not stremio_addons:
        return []
    if provider:
        p_lower = str(provider).strip().lower()
        stremio_addons.sort(key=lambda a: 0 if (p_lower in a.get("name", "").lower() or a.get("name", "").lower() in p_lower) else 1)
    refresh_started_at = time.time()
    
    def fetch_from_addon(addon_orig):
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        resources = addon.get("resources")
        manifest_url = addon.get("manifest_url", "")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if addon.get("id") == "local.iptv-org":
            streams_data = _get_cached_request("https://iptv-org.github.io/api/streams.json", max_age_hours=24)
            strms = [s for s in streams_data if s.get("channel") == imdb_id] if streams_data else []
            valid_strms = []
            for s in strms:
                height = s.get("height", "")
                res_str = f"{height}p" if height else "Live"
                valid_strms.append({
                    "url": s.get("url"),
                    "name": "IPTV-Org",
                    "title": f"Resolution: {res_str}",
                    "behaviorHints": {"filename": "live.m3u8"}
                })
            return addon.get("name", "Unknown"), valid_strms

        if (resources is None or addon_types is None or "idPrefixes" not in addon) and manifest_url:
            try:
                manifest_data = _get_cached_request(manifest_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass

        if addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, actual_media)), None)
            if not type_match:
                has_cat_match = any(is_type_match(cat.get("type"), actual_media) for cat in addon.get("catalogs", []))
                has_prefix_match = addon_prefixes and any(str(imdb_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    return addon.get("name", "Unknown"), []

        if addon_prefixes is not None:
            if not any(str(imdb_id).startswith(p) for p in addon_prefixes):
                return addon.get("name", "Unknown"), []
                
        if not is_stream_provider(addon, media_type=actual_media, item_id=imdb_id):
            return addon.get("name", "Unknown"), []
        if not is_addon_online(manifest_url):
            return addon.get("name", "Unknown"), []
                
        if "manifest.json" in manifest_url:
            base_url = manifest_url.rsplit('manifest.json', 1)[0]
        else:
            base_url = manifest_url
        if not base_url.endswith('/'):
            base_url += '/'
            
        if actual_media == "series" and season is not None and episode is not None:
            clean_id = str(imdb_id)
            if clean_id.startswith(("tt", "tmdb:")):
                if ":" in clean_id:
                    parts = clean_id.split(":")
                    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
                        clean_id = ":".join(parts[:-2])
                    elif len(parts) == 2 and parts[-1].isdigit() and parts[0].startswith("tt"):
                        clean_id = parts[0]
                url = f"{base_url}stream/series/{urllib.parse.quote(clean_id, safe=':')}:{season}:{episode}.json"
            elif clean_id.startswith("kitsu:"):
                parts = clean_id.split(":")
                kitsu_base = ":".join(parts[:2]) if len(parts) >= 2 else clean_id
                url = f"{base_url}stream/series/{urllib.parse.quote(kitsu_base, safe=':')}:{episode}.json"
            else:
                url = f"{base_url}stream/series/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
        else:
            url = f"{base_url}stream/{actual_media}/{urllib.parse.quote(str(imdb_id), safe=':')}.json"
            
        streams = _request_addon_streams(addon, url, timeout=12, bypass_url_cache=bypass_url_cache)
        return addon.get("name", "Unknown"), streams
            
    all_streams = []
    # Keep the legacy force-refresh path aligned with the main discovery path;
    # spawning one worker per installed provider amplified canceled/overlapping
    # refreshes even though requests are paced later at the host boundary.
    num_workers = min(len(stremio_addons), 4)
    if num_workers > 0:
        # Explicit executor: a `with` block would call shutdown(wait=True) on
        # exit and block until every slow addon finishes, defeating the
        # as_completed timeout below.
        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers)
            future_to_addon = {executor.submit(fetch_from_addon, addon): addon for addon in stremio_addons}
        except RuntimeError:
            return []

        try:
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=15):
                    try:
                        addon_name, streams = future.result()
                        if streams:
                            for s in streams:
                                s = dict(s) if isinstance(s, dict) else s
                                s["addon_name"] = addon_name
                                all_streams.append(s)
                    except Exception as e:
                        print(f"Error in addon future: {e}")
            except concurrent.futures.TimeoutError:
                print("Timeout fetching streams from some addons")
        except RuntimeError as e:
            if "shutdown" in str(e).lower() or "interpreter" in str(e).lower():
                return []
            raise
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            
    valid_streams = process_raw_streams(all_streams, season=season, episode=episode)
    if valid_streams:
        database.save_cached_streams_full(
            cache_key, cache_key + ":source:direct", cache_key + ":source:torrent",
            valid_streams, started_at=refresh_started_at,
        )
    return valid_streams

# Pre-cache ffprobe availability once at module level
import shutil as _shutil
import subprocess as _subprocess
_has_ffprobe = bool(_shutil.which('ffprobe'))

def _ping_stream_url(stream):
    url = stream.get("url")
    if not url or not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        return stream
    headers = extract_stream_headers(stream, url)

    for attempt in range(2):
        if _has_ffprobe:
            try:
                cmd = ['ffprobe', '-v', 'error', '-threads', '1', '-analyzeduration', '1000000', '-probesize', '1000000']
                headers_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()])
                if headers_str:
                    cmd.extend(['-headers', headers_str])
                cmd.extend(['-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', url])

                res = _subprocess.run(cmd, capture_output=True, timeout=3.5)
                if res.returncode == 0:
                    stream["is_working"] = True
                    return stream
            except Exception:
                pass

        # Fallback to urllib if ffprobe fails (or isn't available)
        try:
            req = urllib.request.Request(url, headers=headers, method='HEAD')
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CONTEXT) as resp:
                if resp.status < 400:
                    stream["is_working"] = True
                    return stream
        except urllib.error.HTTPError as e:
            is_hakuna = isinstance(url, str) and "hakunaymatata.com" in url.lower()
            if e.code in (401, 403) and not is_hakuna and not any(k.lower() == 'user-agent' for k in headers):
                headers = dict(headers)
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
            elif e.code == 428 and any(k.lower() == 'user-agent' for k in headers):
                headers = {k: v for k, v in headers.items() if k.lower() != 'user-agent'}
            try:
                e.close()
            except Exception:
                pass
        except Exception:
            pass

        try:
            req = urllib.request.Request(url, headers=dict(headers, Range='bytes=0-100'))
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CONTEXT) as resp:
                if resp.status < 400:
                    stream["is_working"] = True
                    return stream
        except urllib.error.HTTPError as e:
            is_hakuna = isinstance(url, str) and "hakunaymatata.com" in url.lower()
            if e.code in (401, 403) and not is_hakuna and not any(k.lower() == 'user-agent' for k in headers):
                headers = dict(headers)
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
            elif e.code == 428 and any(k.lower() == 'user-agent' for k in headers):
                headers = {k: v for k, v in headers.items() if k.lower() != 'user-agent'}
            try:
                e.close()
            except Exception:
                pass
        except Exception:
            pass

        if attempt < 1:
            time.sleep(0.3)

    stream["is_working"] = False
    return stream

def ping_and_filter_streams(streams):
    if not streams:
        return []
    http_streams = [s for s in streams if s.get("url") and (s["url"].startswith("http://") or s["url"].startswith("https://"))]
    if not http_streams:
        return streams
        
    num_workers = min(len(http_streams), 12)
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        results = list(executor.map(_ping_stream_url, http_streams))
        
    working_urls = {s["url"] for s in results if s.get("is_working") is True}
        
    filtered = []
    for s in streams:
        url = s.get("url")
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            filtered.append(s)
        elif url in working_urls:
            filtered.append(s)
    return filtered

def _is_cached_torrent_stream(stream):
    """Identify a torrent result without mistaking its synthetic dedupe hash for a torrent hash."""
    if not isinstance(stream, dict) or stream.get("is_http"):
        return False
    url = str(stream.get("url") or stream.get("stream_url") or "").strip().lower()
    if url.startswith(("http://", "https://")) and not url.split("?", 1)[0].endswith(".torrent"):
        return False
    return bool(
        stream.get("infoHash")
        or url.startswith(("magnet:", "torrent://"))
        or url.split("?", 1)[0].endswith(".torrent")
        or (stream.get("hash") and not url.startswith(("http://", "https://")))
    )


def _stream_has_exact_provider(stream, provider_name):
    if not isinstance(stream, dict) or not provider_name:
        return False
    target = str(provider_name).strip().casefold()
    names = list(stream.get("addon_names") or [])
    if stream.get("addon_name"):
        names.append(stream.get("addon_name"))
    return any(str(name).strip().casefold() == target for name in names if name)


def _stream_matches_source_scope(stream, source_scope):
    if not source_scope:
        return True
    is_direct = bool(stream.get("is_http")) if isinstance(stream, dict) else False
    return is_direct if source_scope == "direct" else not is_direct


def _merge_parsed_streams(primary, secondary):
    """Merge parsed stream lists without duplicating cards, preferring the first list."""
    merged = []
    seen = set()
    for stream in list(primary or []) + list(secondary or []):
        if not isinstance(stream, dict):
            continue
        key = (
            str(stream.get("hash") or stream.get("infoHash") or stream.get("url") or ""),
            str(stream.get("stream_title") or stream.get("title") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(stream)
    return merged


def get_torrents_streamed(imdb_id, media_type="movie", season=None, episode=None, callback=None, title=None, abort_event=None, ep_title=None, force_refresh=False, provider_scope=None, source_scope=None):
    original_id = imdb_id
    # Callers may pass the full alias list used by metadata lookup. Addon
    # stream URLs and prefix checks require a single concrete provider ID.
    if isinstance(imdb_id, (list, tuple)):
        imdb_id = next((candidate for candidate in imdb_id if candidate), None)

    print(
        f"[STREAM DEBUG] API start original_id={original_id!r} normalized_id={imdb_id!r} "
        f"media={media_type!r} season={season!r} episode={episode!r} force={force_refresh}"
    )

    if not imdb_id:
        if callback: callback([], is_cached=False, is_complete=True)
        return []

    if abort_event and abort_event.is_set():
        if callback: callback([], is_cached=False, is_complete=True)
        return []

    if season is not None and episode is not None and isinstance(imdb_id, str) and ":" in imdb_id:
        parts = imdb_id.split(":")
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
            clean_base = ":".join(parts[:-2])
            if clean_base.startswith(("tt", "tmdb:")):
                imdb_id = clean_base
        elif len(parts) == 2 and parts[-1].isdigit() and parts[0].startswith("tt"):
            imdb_id = parts[0]

    if str(imdb_id).startswith("http://") or str(imdb_id).startswith("https://"):
        parts = str(imdb_id).split("||")
        stream_url = parts[0]
        item_title = parts[1] if len(parts) > 1 and parts[1] else (title or "Live Stream")
        stream_obj = [{
            "url": stream_url,
            "name": "Live Stream",
            "title": item_title,
            "behaviorHints": {"filename": "stream.mp3"}
        }]
        if callback: callback(stream_obj, is_cached=False, is_complete=True)
        return stream_obj

    if str(imdb_id).startswith("yt_id:"):
        from .player import extract_youtube_id
        yt_vid = extract_youtube_id(imdb_id)
        if not yt_vid:
            parts = str(imdb_id).split(":")
            yt_vid = parts[-1]
        watch_url = f"https://www.youtube.com/watch?v={yt_vid}"
        stream_title = ep_title or title or "YouTube Stream"
        stream_obj = [{
            "ytId": yt_vid,
            "url": watch_url,
            "name": "YouTube",
            "title": stream_title,
            "addon_name": "YouTube",
            "addon_names": ["YouTube"],
            "is_http": True,
            "quality": "1080p",
            "behaviorHints": {"filename": f"{stream_title}.mp4"}
        }]
        if callback: callback(stream_obj, is_cached=False, is_complete=True)
        return stream_obj

    # A provider-filtered refresh has its own cache namespace. It can reuse
    # matching entries from the global cache for an immediate display, but it
    # must not make the all-provider cache appear freshly refreshed.
    base_cache_key = get_stream_cache_key(imdb_id, media_type, season, episode)
    provider_scope = str(provider_scope).strip() if provider_scope else None
    source_scope = str(source_scope).strip().lower() if source_scope else None
    if source_scope not in ("direct", "torrent"):
        source_scope = None
    provider_cache_suffix = (
        ":provider:" + urllib.parse.quote(provider_scope.casefold(), safe="")
        if provider_scope else ""
    )
    aggregate_cache_key = base_cache_key + provider_cache_suffix
    cache_key = aggregate_cache_key
    direct_source_cache_key = aggregate_cache_key + ":source:direct"
    torrent_source_cache_key = aggregate_cache_key + ":source:torrent"
    cache_res = database.get_cached_streams(aggregate_cache_key, max_age_hours=24, return_age=True)
    if isinstance(cache_res, tuple) and len(cache_res) == 2:
        cached, cache_age = cache_res
    else:
        cached, cache_age = cache_res, 0.0
    cached = list(cached) if isinstance(cached, (list, tuple)) else []

    if provider_scope:
        if cached:
            cached = [s for s in cached if _stream_has_exact_provider(s, provider_scope)]
        global_res = database.get_cached_streams(base_cache_key, max_age_hours=24, return_age=True)
        if isinstance(global_res, tuple) and len(global_res) == 2:
            global_cached, global_age = global_res
        else:
            global_cached, global_age = global_res, 0.0
        global_matches = [
            s for s in (global_cached or [])
            if _stream_has_exact_provider(s, provider_scope)
        ]
        if global_matches and (
            not cached
            or (global_age is not None and (cache_age is None or global_age < cache_age))
        ):
            cached = global_matches
            cache_age = global_age

    if cached:
        cached = [s for s in cached if not check_stream_rate_limit_warning(s)]
    aggregate_cached = list(cached or [])
    aggregate_cache_age = cache_age

    source_cache_key = None
    cache_candidates = aggregate_cached
    if source_scope:
        source_cache_key = aggregate_cache_key + ":source:" + source_scope
        source_res = database.get_cached_streams(source_cache_key, max_age_hours=24, return_age=True)
        if isinstance(source_res, tuple) and len(source_res) == 2:
            source_cached, source_cache_age = source_res
        else:
            source_cached, source_cache_age = source_res, 0.0
        source_cached = [
            s for s in (source_cached or [])
            if _stream_matches_source_scope(s, source_scope)
            and (not provider_scope or _stream_has_exact_provider(s, provider_scope))
            and not check_stream_rate_limit_warning(s)
        ]
        aggregate_candidates = [
            s for s in aggregate_cached
            if _stream_matches_source_scope(s, source_scope)
        ]
        if source_cached and (
            not aggregate_candidates
            or (source_cache_age is not None and (aggregate_cache_age is None or source_cache_age < aggregate_cache_age))
        ):
            cache_candidates = source_cached
            cache_age = source_cache_age
        else:
            cache_candidates = aggregate_candidates
            cache_age = aggregate_cache_age
        cached = [
            s for s in aggregate_cached
            if not _stream_matches_source_scope(s, source_scope)
        ] + list(cache_candidates)

    print(
        f"[STREAM DEBUG] cache key={cache_key!r} cards={len(cached or [])} "
        f"age_seconds={cache_age!r} force={force_refresh} "
        f"provider_scope={provider_scope!r} source_scope={source_scope!r}"
    )

    # Torrent entries can remain useful longer, but direct HTTP links still
    # need their own expiry and age checks even in a mixed result set.
    has_torrents = bool(
        any(_is_cached_torrent_stream(s) for s in cache_candidates)
        if source_scope else any(_is_cached_torrent_stream(s) for s in (cached or []))
    )
    max_fresh_age = 1800 if source_scope == "direct" or (not source_scope and not has_torrents) else 7200
    usable_cached = [
        s for s in (cached or [])
        if _is_cached_torrent_stream(s) or not database.is_stream_stale(s, max_age_seconds=1800)
    ]
    usable_cache_candidates = [
        s for s in (cache_candidates or [])
        if _is_cached_torrent_stream(s) or not database.is_stream_stale(s, max_age_seconds=1800)
    ]
    is_fresh = bool(
        cache_candidates and len(usable_cache_candidates) == len(cache_candidates)
        and not force_refresh
        and cache_age is not None
        and cache_age < max_fresh_age
    )

    if is_fresh:
        print(f"[STREAM DEBUG] cache hit: returning {len(cached or [])} fresh cards without provider requests")
        if callback: callback(usable_cached, is_cached=True, is_complete=True)
        return usable_cached

    if usable_cached and callback:
        print(f"[STREAM DEBUG] delivering {len(usable_cached)} usable cached cards while provider refresh runs")
        callback(usable_cached, is_cached=True, is_complete=False)

    actual_media = media_type
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    print(f"[STREAM DEBUG] enabled addons={len(addons)} media={actual_media!r}")
    if not addons:
        print(f"[STREAM DEBUG] no enabled addons; returning cached cards={len(cached or [])}")
        if callback: callback(usable_cached, is_cached=False, is_complete=True)
        return usable_cached
        
    all_stream_addons = [
        a for a in addons
        if not a.get("manifest_url", "").startswith("builtin://")
        and has_stream_resource(a, media_type=actual_media)
        and (
            not provider_scope
            or str(a.get("name") or "").strip().casefold() == provider_scope.casefold()
        )
    ]
    print(
        f"[STREAM DEBUG] stream-capable addons={len(all_stream_addons)} provider_scope={provider_scope!r} "
        f"names={[a.get('name', 'Unknown') for a in all_stream_addons]}"
    )
    if not all_stream_addons:
        print(f"[STREAM DEBUG] no stream-capable addons; returning cached cards={len(cached or [])}")
        if callback: callback(usable_cached, is_cached=False, is_complete=True)
        return usable_cached

    refresh_started_at = time.time()

    provider_timeout, provider_retries = _get_stream_provider_options()
    retry_backoff_budget = sum(min(1.5 * (2 ** attempt), 4.0) for attempt in range(provider_retries))
    # Each retry reacquires a host slot (bounded to 4s) before making the HTTP
    # request. Use one total deadline for collection, including all attempts.
    provider_collection_timeout = (4.0 + provider_timeout) * (provider_retries + 1) + retry_backoff_budget + 1.0
    print(
        f"[STREAM DEBUG] provider policy timeout={provider_timeout}s retries={provider_retries} "
        f"collection_budget={provider_collection_timeout:.1f}s"
    )

    def fetch_from_addon(addon_orig, cur_id):
        if abort_event and abort_event.is_set():
            print(f"[STREAM DEBUG] addon request cancelled before start addon={addon_orig.get('name', 'Unknown')!r}")
            return addon_orig.get("name", "Unknown"), []
        addon = dict(addon_orig)  # Shallow copy to avoid mutating shared dict in concurrent threads
        resources = addon.get("resources")
        manifest_url = addon.get("manifest_url", "")
        addon_types = addon.get("types")
        addon_prefixes = addon.get("idPrefixes")
        
        if addon.get("id") == "local.iptv-org":
            streams_data = _get_cached_request("https://iptv-org.github.io/api/streams.json", max_age_hours=24)
            strms = [s for s in streams_data if s.get("channel") == cur_id] if streams_data else []
            valid_strms = []
            for s in strms:
                height = s.get("height", "")
                res_str = f"{height}p" if height else "Live"
                valid_strms.append({
                    "url": s.get("url"),
                    "name": "IPTV-Org",
                    "title": f"Resolution: {res_str}",
                    "behaviorHints": {"filename": "live.m3u8"}
                })
            return addon.get("name", "Unknown"), valid_strms

        if (resources is None or addon_types is None or "idPrefixes" not in addon) and manifest_url:
            try:
                manifest_data = _get_cached_request(manifest_url, max_age_hours=168)
                if manifest_data:
                    resources = manifest_data.get("resources", [])
                    addon["resources"] = resources
                    addon_types = manifest_data.get("types", [])
                    addon["types"] = addon_types
                    addon_prefixes = manifest_data.get("idPrefixes")
                    addon["idPrefixes"] = addon_prefixes
            except Exception:
                pass
                
        matched_media_type = actual_media
        if addon_types is not None:
            type_match = next((t for t in addon_types if is_type_match(t, actual_media)), None)
            if type_match:
                matched_media_type = type_match
            else:
                has_cat_match = any(is_type_match(cat.get("type"), actual_media) for cat in (addon.get("catalogs") or []))
                has_prefix_match = addon_prefixes and any(str(cur_id).startswith(p) for p in addon_prefixes)
                if not (has_cat_match or has_prefix_match):
                    print(f"[STREAM DEBUG] addon skipped addon={addon.get('name')!r} id={cur_id!r}: media type mismatch")
                    return addon.get("name", "Unknown"), []
            
        if addon_prefixes is not None:
            if not any(str(cur_id).startswith(p) for p in addon_prefixes):
                print(f"[STREAM DEBUG] addon skipped addon={addon.get('name')!r} id={cur_id!r}: ID prefix mismatch prefixes={addon_prefixes!r}")
                return addon.get("name", "Unknown"), []
                
        if not has_stream_resource(addon, media_type=actual_media, item_id=cur_id):
            print(f"[STREAM DEBUG] addon skipped addon={addon.get('name')!r} id={cur_id!r}: stream resource mismatch")
            return addon.get("name", "Unknown"), []
                
        if "manifest.json" in manifest_url:
            base_url = manifest_url.rsplit('manifest.json', 1)[0]
        else:
            base_url = manifest_url
        if not base_url.endswith('/'):
            base_url += '/'
            
        clean_cur_id = str(cur_id)
        if ":" in clean_cur_id:
            parts = clean_cur_id.split(":")
            if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
                clean_cur_id = ":".join(parts[:-2])
            elif len(parts) == 2 and parts[-1].isdigit() and parts[0].startswith("tt"):
                clean_cur_id = parts[0]

        resource_type = matched_media_type if matched_media_type in ["series", "tv", "anime", "tvshow"] else "series"
        if (matched_media_type in ["series", "tv", "anime", "tvshow"] or actual_media in ["series", "anime", "tv"]) and season is not None and episode is not None:
            if clean_cur_id.startswith(("tt", "tmdb:")):
                url = f"{base_url}stream/{resource_type}/{urllib.parse.quote(clean_cur_id, safe=':')}:{season}:{episode}.json"
            elif clean_cur_id.startswith("kitsu:"):
                parts = clean_cur_id.split(":")
                kitsu_base = ":".join(parts[:2]) if len(parts) >= 2 else clean_cur_id
                url = f"{base_url}stream/{resource_type}/{urllib.parse.quote(kitsu_base, safe=':')}:{episode}.json"
            else:
                # Custom episode ID (e.g. hse-..., hmm-..., htv-..., yt_id:..., etc.)
                url = f"{base_url}stream/{resource_type}/{urllib.parse.quote(str(cur_id), safe=':')}.json"
        else:
            url = f"{base_url}stream/{matched_media_type}/{urllib.parse.quote(str(cur_id), safe=':')}.json"
            
        print(f"[STREAM DEBUG] addon request start addon={addon.get('name')!r} id={cur_id!r} type={matched_media_type!r}")
        # Providers can take longer than a short UI interaction to return a
        # stream list. The old 5s limit caused the first details-page search to
        # fail while an immediate manual retry often succeeded.
        # Omit retries: _request_addon_streams already defaults to the
        # configured stream-provider retry policy.
        streams = _request_addon_streams(
            addon, url, timeout=provider_timeout, abort_event=abort_event,
            bypass_url_cache=force_refresh,
        )
        print(f"[STREAM DEBUG] addon request done addon={addon.get('name')!r} id={cur_id!r} raw_cards={len(streams or [])}")
        return addon.get("name", "Unknown"), streams

    all_raw_streams = []
    if cached and not force_refresh:
        cache_seed_streams = usable_cache_candidates if source_scope else usable_cached
        for c in cache_seed_streams:
            # Reconstitute the raw stream structure carefully.
            raw = {
                "infoHash": c.get("infoHash") or (c.get("hash") if _is_cached_torrent_stream(c) else None),
                "url": c.get("url"),
                "ytId": c.get("ytId"),
                "is_trailer": c.get("is_trailer"),
                "name": c.get("title") or "",
                "title": c.get("stream_title") or "",
                "fileIdx": c.get("file_index"),
                "saved_at": c.get("saved_at"),
                "expires_at": c.get("expires_at"),
                "has_expiring_credentials": c.get("has_expiring_credentials"),
                "binge_group": c.get("binge_group"),
                "addon_names": c.get("addon_names") or [],
                "behaviorHints": c.get("behaviorHints") or ({"filename": c.get("filename")} if c.get("filename") else {}),
                "addon_name": c.get("addon_name") or (c.get("addon_names")[0] if c.get("addon_names") else "Cache")
            }
            all_raw_streams.append(raw)

    final_streams = None
    # When a provider-encoded ID (e.g. dsx:...) resolves to a canonical tt id,
    # the details page may fetch the same item again under that tt id. Mirror
    # the results into its cache key so the second pass does not re-hit every
    # provider.
    alias_cache_ids = []

    import concurrent.futures
    num_workers = min(max(len(all_stream_addons), 2), 4) if all_stream_addons else 1
    if num_workers > 0 and all_stream_addons:
        if abort_event and abort_event.is_set():
            return []
        try:
            # Explicit executor: the `with` form waits for every worker on exit,
            # which nullifies the wait() timeout handled inside the loop.
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers)
        except RuntimeError:
            return []
        future_to_addon = {}
        queried_addon_urls = set()
        collection_deadline = time.monotonic() + provider_collection_timeout

        try:
            # Submit immediately for any addon that directly supports initial imdb_id
            for addon in all_stream_addons:
                if abort_event and abort_event.is_set():
                    return []
                m_url = addon.get("manifest_url", "")
                if has_stream_resource(addon, media_type=actual_media, item_id=imdb_id):
                    print(f"[STREAM DEBUG] direct ID accepted addon={addon.get('name')!r} id={imdb_id!r}")
                    future_to_addon[executor.submit(fetch_from_addon, addon, imdb_id)] = (addon, imdb_id)
                    queried_addon_urls.add(m_url)
                else:
                    print(f"[STREAM DEBUG] direct ID not supported addon={addon.get('name')!r} id={imdb_id!r}")

            # Fire off ID resolution in the background
            def resolve_and_submit():
                from .tmdb_helper import resolve_all_provider_ids
                ids_to_fetch = resolve_all_provider_ids(imdb_id, media_type, title)
                print(f"[STREAM DEBUG] ID resolver id={imdb_id!r} returned {len(ids_to_fetch or [])} IDs: {ids_to_fetch!r}")
                return [i for i in (ids_to_fetch or []) if i != imdb_id]

            scoped_provider_cooling = bool(
                provider_scope
                and all(
                    is_provider_in_cooldown(_get_provider_host(a.get("manifest_url", "")))
                    for a in all_stream_addons
                )
            )
            if scoped_provider_cooling:
                print(f"[STREAM DEBUG] skipping ID resolution: scoped provider {provider_scope!r} is cooling down")

            if not (abort_event and abort_event.is_set()) and not scoped_provider_cooling:
                resolve_future = executor.submit(resolve_and_submit)
            else:
                resolve_future = None

            futures = set(future_to_addon.keys())
            if resolve_future:
                futures.add(resolve_future)

            while futures:
                if abort_event and abort_event.is_set():
                    return []
                # A provider can spend up to 4 seconds waiting for its host
                # slot and another 10 seconds in urlopen. Keep this budget
                # above that combined worst case so the first lookup is not
                # reported empty while its addon workers are still running.
                wait_timeout = max(0.0, collection_deadline - time.monotonic())
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED, timeout=wait_timeout)
                if abort_event and abort_event.is_set():
                    return []
                if not done:
                    print(f"[STREAM DEBUG] provider collection timed out pending={len(futures)}")
                    break

                for future in done:
                    if future == resolve_future:
                        try:
                            resolved_ids = future.result()
                            if resolved_ids:
                                from .tmdb_helper import _decode_provider_item_id
                                is_encoded_id = bool(_decode_provider_item_id(imdb_id))
                                for addon in all_stream_addons:
                                    if abort_event and abort_event.is_set():
                                        return []
                                    m_url = addon.get("manifest_url", "")
                                    # Never query an addon twice for the same media item!
                                    if m_url in queried_addon_urls:
                                        continue
                                    for cur_id in resolved_ids:
                                        if has_stream_resource(addon, media_type=actual_media, item_id=cur_id):
                                            print(f"[STREAM DEBUG] resolved ID accepted addon={addon.get('name')!r} id={cur_id!r}")
                                            new_fut = executor.submit(fetch_from_addon, addon, cur_id)
                                            future_to_addon[new_fut] = (addon, cur_id)
                                            queried_addon_urls.add(m_url)
                                            futures.add(new_fut)
                                            break
                                if is_encoded_id:
                                    canonical_id = next(
                                        (str(i) for i in resolved_ids if str(i).startswith("tt")),
                                        None,
                                    )
                                    if canonical_id and canonical_id != imdb_id and canonical_id not in alias_cache_ids:
                                        alias_cache_ids.append(canonical_id)
                        except Exception as e:
                            print(f"Error resolving IDs: {e}")
                    else:
                        try:
                            addon_orig_info = future_to_addon.get(future, (None, None))
                            addon_obj = addon_orig_info[0] if addon_orig_info else None
                            addon_name, streams = future.result()
                            if streams:
                                # Successful response — clear any previous failure count
                                if addon_obj:
                                    _record_addon_success(addon_obj.get("manifest_url", ""))
                                for s in streams:
                                    if check_stream_rate_limit_warning(s):
                                        continue
                                    s = dict(s) if isinstance(s, dict) else s
                                    s["addon_name"] = addon_name
                                    all_raw_streams.append(s)
                                if callback and not (abort_event and abort_event.is_set()):
                                    current_parsed = process_raw_streams(list(all_raw_streams), season=season, episode=episode, ep_title=ep_title)
                                    print(f"[STREAM DEBUG] incremental results addon={addon_name!r} raw_total={len(all_raw_streams)} parsed_cards={len(current_parsed)}")
                                    if source_scope:
                                        current_scope = [s for s in current_parsed if _stream_matches_source_scope(s, source_scope)]
                                        preserved_scope = [s for s in usable_cached if not _stream_matches_source_scope(s, source_scope)]
                                        callback(preserved_scope + current_scope, is_cached=False, is_complete=False)
                                    else:
                                        callback(current_parsed, is_cached=False, is_complete=False)
                        except Exception as e:
                            print(f"[STREAM DEBUG] addon future failed addon={addon_obj.get('name') if addon_obj else 'Unknown'!r}: {type(e).__name__}: {e}")
        except RuntimeError as e:
            if "shutdown" in str(e).lower() or "interpreter" in str(e).lower():
                return []
            raise
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    # Cancellation invalidates persistence as well as UI callbacks. The HTTP
    # workers may finish in the background, but an obsolete request must not
    # mutate the cache after a newer item/source has been selected.
    if abort_event and abort_event.is_set():
        print(f"[STREAM DEBUG] cache write skipped: request cancelled id={imdb_id!r}")
        return []

    parsed_streams = process_raw_streams(all_raw_streams, season=season, episode=episode, ep_title=ep_title)
    selected_scope_streams = None
    other_scope_streams = []
    if source_scope:
        selected_scope_streams = [s for s in parsed_streams if _stream_matches_source_scope(s, source_scope)]
        # A scoped refresh still asks every provider, so the other source
        # category is refreshed too. Keep it instead of throwing it away:
        # otherwise switching Direct/Torrents afterwards shows an empty list
        # (and provider dropdown) until the user reloads the page.
        other_scope_streams = [s for s in parsed_streams if not _stream_matches_source_scope(s, source_scope)]
        preserved_scope = [s for s in usable_cached if not _stream_matches_source_scope(s, source_scope)]
        merged_other = _merge_parsed_streams(other_scope_streams, preserved_scope)
        final_streams = merged_other + selected_scope_streams if (merged_other or selected_scope_streams) else []
    else:
        final_streams = parsed_streams
    print(f"[STREAM DEBUG] collection finished raw_cards={len(all_raw_streams)} parsed_cards={len(final_streams or [])} cached_fallback={bool(not final_streams and cached)}")

    if final_streams:
        if source_scope:
            if selected_scope_streams:
                if other_scope_streams and not provider_scope:
                    # Both categories came back from this all-provider pass, so
                    # persist a complete result and let the page keep the other
                    # category ready for an instant source switch.
                    merged_streams = database.save_cached_streams_full(
                        cache_key, direct_source_cache_key, torrent_source_cache_key,
                        final_streams, started_at=refresh_started_at, abort_event=abort_event,
                    )
                else:
                    merged_streams = database.save_cached_streams_partial(
                        cache_key, source_cache_key, selected_scope_streams, source_scope,
                        started_at=refresh_started_at, abort_event=abort_event,
                        preserved_streams=usable_cached,
                    )
                if abort_event and abort_event.is_set():
                    return []
                if merged_streams is not None:
                    final_streams = merged_streams
        else:
            merged_streams = database.save_cached_streams_full(
                cache_key, direct_source_cache_key, torrent_source_cache_key,
                final_streams, started_at=refresh_started_at, abort_event=abort_event,
            )
            if abort_event and abort_event.is_set():
                return []
            if merged_streams is not None:
                final_streams = merged_streams
        for alias_id in alias_cache_ids[:1]:
            alias_key = get_stream_cache_key(alias_id, media_type, season, episode) + provider_cache_suffix
            if alias_key != cache_key:
                if source_scope:
                    if selected_scope_streams:
                        if other_scope_streams and not provider_scope:
                            database.save_cached_streams_full(
                                alias_key, alias_key + ":source:direct", alias_key + ":source:torrent",
                                final_streams, started_at=refresh_started_at, abort_event=abort_event,
                            )
                        else:
                            database.save_cached_streams_partial(
                                alias_key, alias_key + ":source:" + source_scope,
                                selected_scope_streams, source_scope,
                                started_at=refresh_started_at, abort_event=abort_event,
                                preserved_streams=usable_cached,
                            )
                else:
                    database.save_cached_streams_full(
                        alias_key, alias_key + ":source:direct", alias_key + ":source:torrent",
                        final_streams, started_at=refresh_started_at, abort_event=abort_event,
                    )
        final_streams = [
            stream for stream in final_streams
            if _is_cached_torrent_stream(stream)
            or not database.is_stream_stale(stream, max_age_seconds=1800)
        ]
        print(f"[STREAM DEBUG] saved {len(final_streams)} cards to cache key={cache_key!r} aliases={alias_cache_ids[:1]!r}")

    result_to_deliver = final_streams if final_streams else usable_cached
    if callback:
        print(f"[STREAM DEBUG] final callback cards={len(result_to_deliver or [])} cached={bool(not final_streams and cached)} complete=True")
        callback(result_to_deliver, is_cached=bool(not final_streams and cached), is_complete=True)

    return result_to_deliver

SUBTITLE_LANGUAGE_MAP = {
    "en": ["en", "eng", "english"], "eng": ["en", "eng", "english"], "english": ["en", "eng", "english"],
    "es": ["es", "spa", "esp", "spanish"], "spa": ["es", "spa", "esp", "spanish"], "spanish": ["es", "spa", "esp", "spanish"],
    "hi": ["hi", "hin", "hindi"], "hin": ["hi", "hin", "hindi"], "hindi": ["hi", "hin", "hindi"],
    "pt": ["pt", "por", "pob", "portuguese"], "por": ["pt", "por", "pob", "portuguese"], "portuguese": ["pt", "por", "pob", "portuguese"],
    "fr": ["fr", "fre", "fra", "french"], "fre": ["fr", "fre", "fra", "french"], "french": ["fr", "fre", "fra", "french"],
    "de": ["de", "ger", "deu", "german"], "ger": ["de", "ger", "deu", "german"], "german": ["de", "ger", "deu", "german"],
    "it": ["it", "ita", "italian"], "ita": ["it", "ita", "italian"], "italian": ["it", "ita", "italian"],
    "ru": ["ru", "rus", "russian"], "rus": ["ru", "rus", "russian"], "russian": ["ru", "rus", "russian"],
    "ar": ["ar", "ara", "arabic"], "ara": ["ar", "ara", "arabic"], "arabic": ["ar", "ara", "arabic"],
    "tr": ["tr", "tur", "turkish"], "tur": ["tr", "tur", "turkish"], "turkish": ["tr", "tur", "turkish"],
    "zh": ["zh", "chi", "zho", "chinese"], "chi": ["zh", "chi", "zho", "chinese"], "chinese": ["zh", "chi", "zho", "chinese"],
    "ja": ["ja", "jpn", "japanese"], "jpn": ["ja", "jpn", "japanese"], "japanese": ["ja", "jpn", "japanese"],
    "ko": ["ko", "kor", "korean"], "kor": ["ko", "kor", "korean"], "korean": ["ko", "kor", "korean"],
    "id": ["id", "ind", "indonesian"], "ind": ["id", "ind", "indonesian"], "indonesian": ["id", "ind", "indonesian"],
    "ml": ["ml", "mal", "malayalam"], "mal": ["ml", "mal", "malayalam"], "malayalam": ["ml", "mal", "malayalam"],
    "ta": ["ta", "tam", "tamil"], "tam": ["ta", "tam", "tamil"], "tamil": ["ta", "tam", "tamil"],
    "te": ["te", "tel", "telugu"], "tel": ["te", "tel", "telugu"], "telugu": ["te", "tel", "telugu"],
    "kn": ["kn", "kan", "kannada"], "kan": ["kn", "kan", "kannada"], "kannada": ["kn", "kan", "kannada"],
    "bn": ["bn", "ben", "bengali"], "ben": ["bn", "ben", "bengali"], "bengali": ["bn", "ben", "bengali"],
    "pa": ["pa", "pan", "punjabi"], "pan": ["pa", "pan", "punjabi"], "punjabi": ["pa", "pan", "punjabi"],
    "ur": ["ur", "urd", "urdu"], "urd": ["ur", "urd", "urdu"], "urdu": ["ur", "urd", "urdu"],
    "fa": ["fa", "per", "fas", "persian"], "per": ["fa", "per", "fas", "persian"], "persian": ["fa", "per", "fas", "persian"],
    "pl": ["pl", "pol", "polish"], "pol": ["pl", "pol", "polish"], "polish": ["pl", "pol", "polish"],
    "nl": ["nl", "dut", "nld", "dutch"], "dut": ["nl", "dut", "nld", "dutch"], "dutch": ["nl", "dut", "nld", "dutch"]
}

SUBTITLE_LANGUAGE_NAMES = {
    "eng": "English", "en": "English", "english": "English",
    "spa": "Spanish", "es": "Spanish", "esp": "Spanish", "spanish": "Spanish",
    "fre": "French", "fra": "French", "fr": "French", "french": "French",
    "ger": "German", "deu": "German", "de": "German", "german": "German",
    "ita": "Italian", "it": "Italian", "italian": "Italian",
    "por": "Portuguese", "pt": "Portuguese", "portuguese": "Portuguese",
    "pob": "Portuguese (BR)", "pt-br": "Portuguese (BR)",
    "rus": "Russian", "ru": "Russian", "russian": "Russian",
    "ara": "Arabic", "ar": "Arabic", "arabic": "Arabic",
    "hin": "Hindi", "hi": "Hindi", "hindi": "Hindi",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "chinese": "Chinese",
    "jpn": "Japanese", "ja": "Japanese", "japanese": "Japanese",
    "kor": "Korean", "ko": "Korean", "korean": "Korean",
    "tur": "Turkish", "tr": "Turkish", "turkish": "Turkish",
    "pol": "Polish", "pl": "Polish", "polish": "Polish",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch", "dutch": "Dutch",
    "cze": "Czech", "ces": "Czech", "cs": "Czech", "czech": "Czech",
    "ell": "Greek", "gre": "Greek", "el": "Greek", "greek": "Greek",
    "swe": "Swedish", "sv": "Swedish", "swedish": "Swedish",
    "ind": "Indonesian", "id": "Indonesian", "indonesian": "Indonesian",
    "mal": "Malayalam", "ml": "Malayalam", "malayalam": "Malayalam",
    "tam": "Tamil", "ta": "Tamil", "tamil": "Tamil",
    "tel": "Telugu", "te": "Telugu", "telugu": "Telugu",
    "kan": "Kannada", "kn": "Kannada", "kannada": "Kannada",
    "ben": "Bengali", "bn": "Bengali", "bengali": "Bengali",
    "pan": "Punjabi", "pa": "Punjabi", "punjabi": "Punjabi",
    "urd": "Urdu", "ur": "Urdu", "urdu": "Urdu",
    "per": "Persian", "fas": "Persian", "fa": "Persian", "persian": "Persian",
    "rum": "Romanian", "ron": "Romanian", "ro": "Romanian", "romanian": "Romanian",
    "hun": "Hungarian", "hu": "Hungarian", "hungarian": "Hungarian",
    "ukr": "Ukrainian", "uk": "Ukrainian", "ukrainian": "Ukrainian",
    "heb": "Hebrew", "he": "Hebrew", "hebrew": "Hebrew",
    "tha": "Thai", "th": "Thai", "thai": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese", "vietnamese": "Vietnamese",
    "dan": "Danish", "da": "Danish", "danish": "Danish",
    "fin": "Finnish", "fi": "Finnish", "finnish": "Finnish",
    "nor": "Norwegian", "no": "Norwegian", "norwegian": "Norwegian",
    "nob": "Norwegian Bokmål",
    "hrv": "Croatian", "hr": "Croatian", "croatian": "Croatian",
    "srp": "Serbian", "sr": "Serbian", "serbian": "Serbian",
    "bul": "Bulgarian", "bg": "Bulgarian", "bulgarian": "Bulgarian",
    "slv": "Slovenian", "sl": "Slovenian", "slovenian": "Slovenian",
    "slk": "Slovak", "sk": "Slovak", "slovak": "Slovak",
    "est": "Estonian", "et": "Estonian", "estonian": "Estonian",
    "lav": "Latvian", "lv": "Latvian", "latvian": "Latvian",
    "lit": "Lithuanian", "lt": "Lithuanian", "lithuanian": "Lithuanian",
}

def get_user_preferred_languages():
    """Retrieve user preferred subtitle languages string from GSettings or database."""
    pref_langs_str = ""
    try:
        import gi
        gi.require_version('Gio', '2.0')
        from gi.repository import Gio
        schema_source = Gio.SettingsSchemaSource.get_default()
        if schema_source and schema_source.lookup("io.github.fastrizwaan.PopcornBox", True):
            settings = Gio.Settings.new("io.github.fastrizwaan.PopcornBox")
            pref_langs_str = settings.get_string("subtitle-languages")
    except Exception:
        pass
        
    if not pref_langs_str:
        pref_langs_str = database.get_setting("subtitle-languages", "") or database.get_setting("subtitle_languages", "")

    return pref_langs_str or ""

def is_sdh_track(track):
    """Determine if a subtitle track or subtitle item is SDH / CC / Hearing Impaired."""
    if not isinstance(track, dict):
        return False
    if track.get("hearing-impaired") or track.get("hearing_impaired") or track.get("hearingImpaired") or track.get("hi") is True or track.get("sdh") is True:
        return True
    title = str(track.get("title") or "")
    lang = str(track.get("lang") or "")
    filename = str(track.get("subtitleFileName") or track.get("file_name") or track.get("release_name") or track.get("movieReleaseName") or track.get("filename") or "")
    combined = f"{title} {filename}".lower()
    
    if re.search(r"\b(sdh|cc|hoh)\b", combined):
        return True
    if re.search(r"\[\s*(sdh|cc|hi|hoh)\s*\]", combined):
        return True
    if re.search(r"\(\s*(sdh|cc|hi|hoh)\s*\)", combined):
        return True
    if any(k in combined for k in ["hearing impaired", "hearing-impaired", "hard of hearing", "closed caption", "closed captions"]):
        return True
    if "sdh" in lang.lower() or "cc" in lang.lower():
        return True
    return False

def has_embedded_subtitles(tracks):
    """Check if any embedded (non-external) subtitle track exists in the given tracks list."""
    if not tracks:
        return False
    return any(
        isinstance(t, dict) and t.get("type") == "sub" and not t.get("external")
        for t in tracks
    )

def find_best_subtitle_track(tracks, preferred_langs=None, only_embedded=False):
    """
    Find the best subtitle track from tracks according to:
    1. Language preference order (from user settings or argument)
    2. SDH priority (SDH preferred over non-SDH in the same language)
    3. Full dialogue over forced subtitles
    4. Default track flag
    5. Track ID order
    Returns the best track dict, or None if no acceptable track is found.
    """
    if not tracks:
        return None

    if preferred_langs is None:
        user_pref = get_user_preferred_languages()
        raw_langs = [l.strip().lower() for l in user_pref.replace(";", ",").split(",") if l.strip()]
        if not raw_langs:
            raw_langs = ["en", "eng", "english"]
    elif isinstance(preferred_langs, str):
        raw_langs = [l.strip().lower() for l in preferred_langs.replace(";", ",").split(",") if l.strip()]
        if not raw_langs:
            raw_langs = ["en", "eng", "english"]
    else:
        raw_langs = [str(l).strip().lower() for l in preferred_langs if str(l).strip()]
        if not raw_langs:
            raw_langs = ["en", "eng", "english"]

    rank_sets = []
    for pl in raw_langs:
        clean_pl = pl.split("-")[0].split("_")[0]
        codes = list(SUBTITLE_LANGUAGE_MAP.get(clean_pl, SUBTITLE_LANGUAGE_MAP.get(pl, [clean_pl, pl])))
        names = [SUBTITLE_LANGUAGE_NAMES.get(c, "").lower() for c in codes]
        s = set(codes) | set(n for n in names if n)
        rank_sets.append(s)

    candidates = []
    for track in tracks:
        if not isinstance(track, dict) or track.get("type") != "sub":
            continue
        if only_embedded and track.get("external"):
            continue

        raw_sub_lang = str(track.get("lang") or "").lower().strip()
        sub_lang_clean = raw_sub_lang.split("-")[0].split("_")[0]
        track_title = str(track.get("title") or "").lower()
        sdh = is_sdh_track(track)
        is_forced = bool(track.get("forced"))
        is_default = bool(track.get("default"))
        track_id = int(track.get("id", 0))

        matched_rank = 999
        for idx, rset in enumerate(rank_sets):
            if raw_sub_lang in rset or sub_lang_clean in rset:
                matched_rank = idx
                break
            for item in rset:
                if len(item) >= 3 and re.search(r"\b" + re.escape(item) + r"\b", track_title):
                    matched_rank = idx
                    break
                elif len(item) == 2 and re.search(r"[\(\[\{\s]" + re.escape(item) + r"[\)\]\}\s]", f" {track_title} "):
                    matched_rank = idx
                    break
            if matched_rank < 999:
                break

        # Fallback for undetermined / missing language tags
        if matched_rank == 999 and (not raw_sub_lang or raw_sub_lang in ["und", "unk", "unknown"]):
            if sdh:
                matched_rank = 500
            elif is_default:
                matched_rank = 501
            else:
                matched_rank = 502

        if matched_rank < 999:
            score = (matched_rank, 0 if sdh else 1, 1 if is_forced else 0, 0 if is_default else 1, track_id)
            candidates.append((score, track))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

def get_subtitles(imdb_id, media_type="movie", season=None, episode=None, stream_subtitles=None, title=None):
    all_subs = []
    if stream_subtitles and isinstance(stream_subtitles, list):
        for s in stream_subtitles:
            if isinstance(s, dict) and s.get("url"):
                all_subs.append(s)

    resolved_imdb = resolve_to_imdb_id(imdb_id, media_type, title=title) if (imdb_id or title) else None
    
    actual_media = "series" if media_type in ["series", "tv", "anime"] else media_type
    if season is not None and episode is not None:
        actual_media = "series"

    cache_key = f"subs_{resolved_imdb or imdb_id}_{actual_media}_{season}_{episode}"
    if not stream_subtitles:
        cached = database.get_cached_subtitles(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

    if resolved_imdb and str(resolved_imdb).startswith("tt"):
        clean_imdb = str(resolved_imdb).split(":")[0]
        if actual_media == "series" and season is not None and episode is not None:
            sub_path = f"series/{clean_imdb}:{int(season)}:{int(episode)}.json"
        else:
            sub_path = f"movie/{clean_imdb}.json"
            
        urls_to_try = [
            f"https://opensubtitles-v3.strem.io/subtitles/{sub_path}",
            f"https://subtitles.strem.io/subtitles/{sub_path}",
            f"https://opensubtitles.strem.io/subtitles/{sub_path}"
        ]
        try:
            installed_addons = [a for a in database.get_addons() if has_subtitles_resource(a, media_type=actual_media, item_id=resolved_imdb)]
            for addon in installed_addons:
                base_url = addon.get("url", "") or addon.get("manifest_url", "")
                base_url = base_url.rsplit("/manifest.json", 1)[0]
                if base_url:
                    u = f"{base_url}/subtitles/{sub_path}"
                    if u not in urls_to_try:
                        urls_to_try.append(u)
        except Exception:
            pass

        def _fetch_sub_url(sub_url):
            try:
                req = urllib.request.Request(
                    sub_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept-Encoding': 'gzip, deflate'
                    }
                )
                with urllib.request.urlopen(req, timeout=3.5, context=_SSL_CONTEXT) as response:
                    raw_data = response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    if encoding == "gzip" or raw_data.startswith(b"\x1f\x8b"):
                        import gzip
                        try:
                            raw_data = gzip.decompress(raw_data)
                        except Exception:
                            pass
                    data = json.loads(raw_data.decode('utf-8'))
                    return data.get("subtitles", [])
            except urllib.error.HTTPError as e:
                try:
                    e.close()
                except Exception:
                    pass
                logging.debug(f"HTTP error {e.code} fetching subtitles from {sub_url}")
                return []
            except Exception as e:
                logging.debug(f"Error fetching subtitles from {sub_url}: {e}")
                return []

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls_to_try), 6)) as executor:
                fut_to_url = {executor.submit(_fetch_sub_url, u): u for u in urls_to_try}
                try:
                    for fut in concurrent.futures.as_completed(fut_to_url, timeout=4.0):
                        try:
                            items = fut.result()
                            if items:
                                all_subs.extend(items)
                        except Exception:
                            pass
                except concurrent.futures.TimeoutError:
                    pass
        except RuntimeError as e:
            if "shutdown" not in str(e).lower() and "interpreter" not in str(e).lower():
                raise

    # Deduplicate subtitles by URL to prevent loading identical tracks multiple times
    seen_urls = set()
    unique_subs = []
    for s in all_subs:
        url = s.get("url")
        if url:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_subs.append(s)
        else:
            unique_subs.append(s)
    all_subs = unique_subs

    pref_langs_str = get_user_preferred_languages()
    raw_langs = [l.strip().lower() for l in pref_langs_str.replace(";", ",").split(',') if l.strip()]
    if not raw_langs:
        raw_langs = ["en", "eng", "english"]

    rank_sets = []
    for pl in raw_langs:
        clean_pl = pl.split("-")[0].split("_")[0]
        codes = SUBTITLE_LANGUAGE_MAP.get(clean_pl, SUBTITLE_LANGUAGE_MAP.get(pl, [clean_pl, pl]))
        rank_sets.append(set(codes))

    matched_subs = []
    for s in all_subs:
        raw_sub_lang = str(s.get("lang", "")).lower()
        sub_lang_clean = raw_sub_lang.split("-")[0].split("_")[0]
        matched_rank = 999
        for idx, rset in enumerate(rank_sets):
            if raw_sub_lang in rset or sub_lang_clean in rset or any(raw_sub_lang.startswith(p) for p in rset) or any(p in raw_sub_lang for p in rset):
                matched_rank = idx
                break
        if matched_rank < 999:
            sdh_score = 0 if is_sdh_track(s) else 1
            matched_subs.append((matched_rank, sdh_score, s))
            
    if not matched_subs and all_subs:
        if all_subs:
            database.save_cached_subtitles(cache_key, all_subs)
        return all_subs

    matched_subs.sort(key=lambda x: (x[0], x[1]))
    res_list = [item[2] for item in matched_subs]
    if res_list:
        database.save_cached_subtitles(cache_key, res_list)
    return res_list


def get_language_name(code):
    if not code:
        return "Unknown"
    c = str(code).lower().strip().split("-")[0].split("_")[0]
    return SUBTITLE_LANGUAGE_NAMES.get(str(code).lower().strip(), SUBTITLE_LANGUAGE_NAMES.get(c, str(code).upper()))

def search_subtitles_online(query="", imdb_id=None, media_type="movie", season=None, episode=None):
    """
    Search OpenSubtitles and installed subtitle addons for subtitles without language filtering.
    Returns a list of subtitle dicts with metadata:
    {
        'id': ...,
        'url': ...,
        'lang': ...,
        'lang_name': ...,
        'file_name': ...,
        'release_name': ...,
        'fps': ...,
        'format': ...,
        'source': ...,
    }
    """
    clean_imdb = None
    if imdb_id and str(imdb_id).startswith("tt"):
        clean_imdb = str(imdb_id).split(":")[0]
    elif imdb_id:
        clean_imdb = resolve_to_imdb_id(imdb_id, media_type, title=query)

    actual_media = "series" if media_type in ["series", "tv", "anime"] else (media_type or "movie")
    if season is not None and episode is not None:
        actual_media = "series"

    # If we still do not have a valid IMDb ID (tt...), search Cinemeta by query
    if (not clean_imdb or not str(clean_imdb).startswith("tt")) and query:
        clean_query = re.sub(r'[\(\[\{].*?[\)\]\}]', '', str(query))
        clean_query = re.sub(r'\b(19\d\d|20\d\d)\b.*', '', clean_query)
        clean_query = re.sub(r'[sS]\d+[eE]\d+.*', '', clean_query)
        clean_query = re.sub(r'\b(480p|720p|1080p|2160p|4k|bluray|web-dl|webrip|x264|x265|hevc)\b.*', '', clean_query, flags=re.IGNORECASE)
        clean_query = clean_query.replace(".", " ").replace("_", " ").strip()
        if not clean_query:
            clean_query = str(query).strip()

        search_types = [actual_media, "series" if actual_media == "movie" else "movie"]
        for stype in search_types:
            try:
                c_url = f"https://v3-cinemeta.strem.io/catalog/{stype}/top/search={urllib.parse.quote(clean_query)}.json"
                req = urllib.request.Request(c_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                with urllib.request.urlopen(req, timeout=4.0, context=_SSL_CONTEXT) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    metas = data.get("metas", [])
                    if metas and metas[0].get("id", "").startswith("tt"):
                        clean_imdb = metas[0]["id"]
                        if stype == "series":
                            actual_media = "series"
                        break
            except urllib.error.HTTPError as e:
                try:
                    e.close()
                except Exception:
                    pass
                logging.debug(f"Cinemeta subtitle search HTTP error: {e}")
            except Exception as e:
                logging.debug(f"Cinemeta subtitle search error: {e}")

    if not clean_imdb or not str(clean_imdb).startswith("tt"):
        logging.warning(f"Could not resolve valid IMDb ID for subtitles query='{query}', imdb_id='{imdb_id}'")
        return []

    clean_imdb = str(clean_imdb).split(":")[0]
    if actual_media == "series" and season is not None and episode is not None:
        sub_path = f"series/{clean_imdb}:{int(season)}:{int(episode)}.json"
    else:
        sub_path = f"movie/{clean_imdb}.json"

    urls_to_try = [
        ("OpenSubtitles v3", f"https://opensubtitles-v3.strem.io/subtitles/{sub_path}"),
        ("OpenSubtitles", f"https://subtitles.strem.io/subtitles/{sub_path}"),
        ("OpenSubtitles", f"https://opensubtitles.strem.io/subtitles/{sub_path}")
    ]

    try:
        installed_addons = [a for a in database.get_addons() if has_subtitles_resource(a, media_type=actual_media, item_id=clean_imdb)]
        for addon in installed_addons:
            base_url = addon.get("url", "") or addon.get("manifest_url", "")
            base_url = base_url.rsplit("/manifest.json", 1)[0]
            addon_name = addon.get("name", "Addon")
            if base_url:
                u = f"{base_url}/subtitles/{sub_path}"
                if not any(existing_url == u for _, existing_url in urls_to_try):
                    urls_to_try.append((addon_name, u))
    except Exception:
        pass

    def _fetch_sub_url(source_name, sub_url):
        try:
            req = urllib.request.Request(
                sub_url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept-Encoding': 'gzip, deflate'
                }
            )
            with urllib.request.urlopen(req, timeout=4.5, context=_SSL_CONTEXT) as response:
                raw_data = response.read()
                encoding = response.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip" or raw_data.startswith(b"\x1f\x8b"):
                    import gzip
                    try:
                        raw_data = gzip.decompress(raw_data)
                    except Exception:
                        pass
                data = json.loads(raw_data.decode('utf-8'))
                items = data.get("subtitles", [])
                for it in items:
                    if isinstance(it, dict):
                        it["_source"] = source_name
                return items
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:
                pass
            logging.debug(f"HTTP error {e.code} fetching subtitles from {sub_url}")
            return []
        except Exception as e:
            logging.debug(f"Error fetching subtitles from {sub_url}: {e}")
            return []

    raw_subs = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls_to_try), 6)) as executor:
            fut_to_info = {executor.submit(_fetch_sub_url, s_name, u): (s_name, u) for s_name, u in urls_to_try}
            try:
                for fut in concurrent.futures.as_completed(fut_to_info, timeout=5.0):
                    try:
                        items = fut.result()
                        if items:
                            raw_subs.extend(items)
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                pass
    except RuntimeError as e:
        if "shutdown" not in str(e).lower() and "interpreter" not in str(e).lower():
            raise

    seen_urls = set()
    results = []
    for s in raw_subs:
        url = s.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        lang_code = s.get("lang") or "und"
        file_name = s.get("subtitleFileName") or ""
        release_name = s.get("movieReleaseName") or s.get("name") or file_name or "Subtitle"
        fps = s.get("fpsMilli")
        fps_str = f"{fps / 1000:.3f}".rstrip('0').rstrip('.') + " fps" if fps else ""
        source = s.get("_source", "OpenSubtitles")

        results.append({
            "id": s.get("id", str(len(results) + 1)),
            "url": url,
            "lang": lang_code,
            "lang_name": get_language_name(lang_code),
            "file_name": file_name,
            "release_name": release_name,
            "fps": fps_str,
            "format": s.get("format", "SRT"),
            "source": source,
        })

    return results

def download_subtitle(sub_url, filename):
    sub_dir = os.path.join(database.CONFIG_DIR, "subtitles")
    os.makedirs(sub_dir, exist_ok=True)
        
    if not filename.endswith('.srt') and not filename.endswith('.vtt'):
        filename += '.srt'
        
    filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
    file_path = os.path.join(sub_dir, filename)
    
    try:
        req = urllib.request.Request(
            sub_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Encoding': 'gzip, deflate'
            }
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as response:
            data = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
                import gzip
                try:
                    data = gzip.decompress(data)
                except Exception as ge:
                    logging.debug(f"Gzip decompression error: {ge}")
            elif encoding == "deflate":
                import zlib
                try:
                    data = zlib.decompress(data)
                except Exception:
                    pass
            # Write via temp file + rename so a crash never leaves a partial
            # .srt that a later playback would load as a valid subtitle.
            temp_path = f"{file_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with open(temp_path, 'wb') as f:
                    f.write(data)
                os.replace(temp_path, file_path)
            except Exception:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                raise
        return file_path
    except urllib.error.HTTPError as e:
        logging.debug(f"HTTP Error {e.code} downloading subtitle")
        try:
            e.close()
        except Exception:
            pass
        return None
    except Exception as e:
        logging.debug(f"Error downloading subtitle: {e}")
        return None

def download_subtitle_to_path(sub_url, file_path):
    dir_name = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    base_name = "".join([c for c in base_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '(', ')', '[', ']')]).rstrip()
    
    file_path = os.path.join(dir_name, base_name)
    os.makedirs(dir_name, exist_ok=True)
    
    try:
        req = urllib.request.Request(
            sub_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Encoding': 'gzip, deflate'
            }
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as response:
            payload = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip" or payload.startswith(b"\x1f\x8b"):
                try:
                    payload = gzip.decompress(payload)
                except Exception as ge:
                    logging.debug(f"Gzip decompression error: {ge}")
            elif encoding == "deflate":
                try:
                    payload = zlib.decompress(payload)
                except Exception:
                    pass
        temp_path = f"{file_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(temp_path, 'wb') as f:
                f.write(payload)
            os.replace(temp_path, file_path)
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            raise
        return file_path
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} downloading subtitle")
        try:
            e.close()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"Error downloading subtitle: {e}")
        return None

def build_magnet(hash_string, title, sources=None):
    title = title or ""
    encoded_title = urllib.parse.quote(title)
    trackers = []
    seen = set()
    if sources and isinstance(sources, list):
        for src in sources:
            if not isinstance(src, str):
                continue
            t = src.strip()
            if t.startswith("tracker:"):
                t = t[len("tracker:"):].strip()
            elif t.startswith("dht:"):
                continue
            if t and (t.startswith("udp://") or t.startswith("http://") or t.startswith("https://")):
                if t not in seen:
                    seen.add(t)
                    trackers.append(t)
    for dt in DEFAULT_TRACKERS:
        if dt not in seen:
            seen.add(dt)
            trackers.append(dt)
    tracker_str = "&tr=".join([urllib.parse.quote(t, safe="") for t in trackers])
    return f"magnet:?xt=urn:btih:{hash_string}&dn={encoded_title}&tr={tracker_str}"

def add_trackers_to_magnet(magnet, sources=None):
    if not magnet or not magnet.startswith("magnet:?"):
        return magnet
    new_trackers = []
    seen = set()
    if sources and isinstance(sources, list):
        for src in sources:
            if not isinstance(src, str):
                continue
            t = src.strip()
            if t.startswith("tracker:"):
                t = t[len("tracker:"):].strip()
            elif t.startswith("dht:"):
                continue
            if t and (t.startswith("udp://") or t.startswith("http://") or t.startswith("https://")):
                if t not in seen:
                    seen.add(t)
                    new_trackers.append(t)
    for dt in DEFAULT_TRACKERS:
        if dt not in seen:
            seen.add(dt)
            new_trackers.append(dt)
    for t in new_trackers:
        enc = urllib.parse.quote(t, safe="")
        if enc not in magnet and t not in magnet:
            magnet += f"&tr={enc}"
    return magnet

def is_stremio_server_running(host="127.0.0.1", port=11470, timeout=0.3):
    """Check if Stremio Streaming Server is active on local machine."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        res = sock.connect_ex((host, port))
        sock.close()
        return res == 0
    except Exception:
        return False

def build_stremio_stream_url(info_hash, file_index=None, sources=None, host="127.0.0.1", port=11470):
    f_idx = file_index if file_index is not None and str(file_index).isdigit() and int(file_index) >= 0 else -1
    base_url = f"http://{host}:{port}/{info_hash}/{f_idx}"
    query_parts = []
    if sources and isinstance(sources, list):
        for s in sources:
            if not isinstance(s, str) or not s.strip():
                continue
            tr = s.strip()
            if tr.startswith("tracker:"):
                query_parts.append(f"tr={urllib.parse.quote(tr, safe='')}")
            elif tr.startswith("dht:"):
                query_parts.append(f"dht={urllib.parse.quote(tr[4:], safe='')}")
            elif tr.startswith(("udp://", "http://", "https://")):
                query_parts.append(f"tr={urllib.parse.quote('tracker:' + tr, safe='')}")
    if query_parts:
        base_url += "?" + "&".join(query_parts)
    return base_url

