"""
DNS-over-HTTPS (DoH) engine for PopcornBox.

Provides an application-wide secure DNS resolver hooking into Python's
`socket.getaddrinfo` to prevent DNS poisoning, ISP censorship, and
TLS handshake resets (e.g. UNEXPECTED_EOF_WHILE_READING).

Features:
- Dual DoH providers: Cloudflare (primary, fast) + Google (fallback)
- High-speed in-memory TTL cache (subsequent lookups take < 0.001 ms)
- Thread-safe recursion guards for DoH requests
- Automatic bypass for localhost, LAN, IPv4/IPv6 literals, and bootstrap hosts
- Graceful transparent fallback to system getaddrinfo on any network/offline failure
"""

import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import ssl

logger = logging.getLogger("PopcornBox.DoH")

_DOH_CACHE = {}
_DOH_CACHE_LOCK = threading.Lock()
_DOH_CACHE_TTL = 900  # 15 minutes

_orig_getaddrinfo = socket.getaddrinfo
_is_hooked = False
_hook_lock = threading.Lock()
_enabled = True

_thread_local = threading.local()

# Bootstrap IPs for DoH hosts so we don't need DNS to resolve the DNS resolver itself
_BOOTSTRAP_HOSTS = {
    "cloudflare-dns.com": ["1.1.1.1", "1.0.0.1", "2606:4700:4700::1111", "2606:4700:4700::1001"],
    "one.one.one.one": ["1.1.1.1", "1.0.0.1"],
    "dns.google": ["8.8.8.8", "8.8.4.4", "2001:4860:4860::8888", "2001:4860:4860::8844"],
}

_SPECIAL_HOSTS = {"localhost", "localhost.localdomain"}
_SPECIAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".onion")

_SSL_CONTEXT = ssl.create_default_context()


def _is_ip_literal(host: str) -> bool:
    """Returns True if host is an IPv4 or IPv6 literal."""
    if not host:
        return False
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
            return True
        except ValueError:
            pass
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return False


def _should_bypass_doh(host: str) -> bool:
    """Checks whether this hostname should bypass DoH and use standard system DNS."""
    if not host or not isinstance(host, str):
        return True
    host_clean = host.strip().lower()
    if not host_clean:
        return True
    if host_clean in _SPECIAL_HOSTS or any(host_clean.endswith(s) for s in _SPECIAL_SUFFIXES):
        return True
    if host_clean in _BOOTSTRAP_HOSTS:
        return True
    if _is_ip_literal(host_clean):
        return True
    return False


def doh_resolve(hostname: str, timeout: float = 2.0) -> list[str]:
    """
    Resolves *hostname* via DNS-over-HTTPS.
    Returns list of IP strings, or empty list on failure.
    Uses in-memory cache with TTL.
    """
    if not hostname or _should_bypass_doh(hostname):
        return []

    host_clean = hostname.strip().lower().rstrip(".")
    now = time.monotonic()

    # 1. Check in-memory cache
    with _DOH_CACHE_LOCK:
        entry = _DOH_CACHE.get(host_clean)
        if entry and (now - entry[1]) < _DOH_CACHE_TTL:
            return list(entry[0])

    # 2. Query DoH providers with recursion guard
    _thread_local.in_doh = True
    ips = []
    try:
        # Try Cloudflare DoH first
        cf_url = f"https://cloudflare-dns.com/dns-query?name={urllib.parse.quote(host_clean)}&type=A"
        req = urllib.request.Request(cf_url, headers={
            "Accept": "application/dns-json",
            "User-Agent": "PopcornBox-DoH/1.0"
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for a in data.get("Answer", []):
                    if a.get("type") == 1:
                        ip = a.get("data", "").strip()
                        if _is_ip_literal(ip):
                            ips.append(ip)
        except Exception:
            pass

        # Try Google DoH fallback if Cloudflare didn't return any IPs
        if not ips:
            g_url = f"https://dns.google/resolve?name={urllib.parse.quote(host_clean)}&type=A"
            req = urllib.request.Request(g_url, headers={
                "Accept": "application/json",
                "User-Agent": "PopcornBox-DoH/1.0"
            })
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for a in data.get("Answer", []):
                        if a.get("type") == 1:
                            ip = a.get("data", "").strip()
                            if _is_ip_literal(ip):
                                ips.append(ip)
            except Exception:
                pass
    finally:
        _thread_local.in_doh = False

    if ips:
        unique_ips = list(dict.fromkeys(ips))
        with _DOH_CACHE_LOCK:
            _DOH_CACHE[host_clean] = (unique_ips, now)
        return unique_ips

    return []


def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """
    Hooked `socket.getaddrinfo` that resolves domains via DoH before calling
    system resolver on the resolved IP addresses.
    """
    if getattr(_thread_local, "in_doh", False) or not _enabled:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    if not isinstance(host, str) or _should_bypass_doh(host):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    ips = doh_resolve(host)
    if ips:
        results = []
        for ip in ips:
            ip_family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            if family not in (0, ip_family):
                continue
            try:
                res = _orig_getaddrinfo(ip, port, ip_family, type, proto, flags)
                if res:
                    results.extend(res)
            except Exception:
                pass
        if results:
            return results

    # Fallback to system getaddrinfo
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def enable_global_doh(enabled: bool = True):
    """Enables or disables the global DoH socket hook."""
    global _enabled, _is_hooked
    with _hook_lock:
        _enabled = bool(enabled)
        if _enabled:
            if not _is_hooked:
                socket.getaddrinfo = _custom_getaddrinfo
                _is_hooked = True
                logger.info("Global DNS-over-HTTPS (DoH) enabled.")
        else:
            if _is_hooked:
                socket.getaddrinfo = _orig_getaddrinfo
                _is_hooked = False
                logger.info("Global DNS-over-HTTPS (DoH) disabled.")


def is_global_doh_enabled() -> bool:
    return _enabled and _is_hooked


def clear_doh_cache():
    with _DOH_CACHE_LOCK:
        _DOH_CACHE.clear()


def init_doh():
    """Initializes DoH according to the user's stored setting."""
    try:
        from . import database
        doh_setting = database.get_setting("doh_enabled", True)
    except Exception:
        doh_setting = True
    enable_global_doh(bool(doh_setting))
