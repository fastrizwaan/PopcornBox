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
import os
import re
import select
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


def doh_resolve(hostname: str, timeout: float = 1.5) -> list[str]:
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
        endpoints = [
            (f"https://cloudflare-dns.com/dns-query?name={urllib.parse.quote(host_clean)}&type=A", "application/dns-json"),
            (f"https://dns.google/resolve?name={urllib.parse.quote(host_clean)}&type=A", "application/json"),
        ]
        for url, accept_hdr in endpoints:
            req = urllib.request.Request(url, headers={
                "Accept": accept_hdr,
                "User-Agent": "PopcornBox-DoH/1.0"
            })
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for a in data.get("Answer", []):
                        if a.get("type") in (1, 28):  # A or AAAA
                            ip = a.get("data", "").strip()
                            if _is_ip_literal(ip):
                                ips.append(ip)
                if ips:
                    break
            except Exception:
                continue
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
    if not isinstance(host, str):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    host_clean = host.strip().lower().rstrip(".")
    # Fast path: bootstrap hosts resolve directly to bootstrap IPs without external DNS,
    # even when inside doh_resolve itself
    if host_clean in _BOOTSTRAP_HOSTS:
        results = []
        for ip in _BOOTSTRAP_HOSTS[host_clean]:
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

    if getattr(_thread_local, "in_doh", False) or not _enabled:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    if _should_bypass_doh(host):
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


# ---------------------------------------------------------------------------
# Local DoH Forward Proxy Server (for Deno / Scraper Subprocesses)
# ---------------------------------------------------------------------------

_proxy_server = None
_proxy_port = 0
_proxy_lock = threading.Lock()


class _LocalDoHProxyHandler(threading.Thread):
    def __init__(self, client_sock):
        super().__init__(daemon=True)
        self.client = client_sock

    def run(self):
        remote = None
        try:
            req_data = b""
            while b"\r\n\r\n" not in req_data:
                chunk = self.client.recv(4096)
                if not chunk:
                    break
                req_data += chunk
                if len(req_data) > 65536:
                    break
            if not req_data:
                return

            header_part, _, body = req_data.partition(b"\r\n\r\n")
            lines = header_part.split(b"\r\n")
            if not lines:
                return

            request_line = lines[0].decode("latin-1", errors="ignore")
            parts = request_line.split()
            if len(parts) < 2:
                return

            method, target = parts[0], parts[1]

            if method.upper() == "CONNECT":
                # HTTPS tunneling
                host_str, _, port_str = target.partition(":")
                port = int(port_str) if port_str.isdigit() else 443
                try:
                    remote = socket.create_connection((host_str, port), timeout=12)
                except Exception:
                    try:
                        self.client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    except Exception:
                        pass
                    return

                self.client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._pipe(self.client, remote)
            else:
                # Plain HTTP proxying
                parsed = urllib.parse.urlsplit(target)
                host = parsed.hostname
                port = parsed.port or 80
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                try:
                    remote = socket.create_connection((host, port), timeout=12)
                except Exception:
                    try:
                        self.client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    except Exception:
                        pass
                    return

                # Reconstruct HTTP request with relative path
                first_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
                out_headers = [first_line]
                for l in lines[1:]:
                    if l.lower().startswith(b"proxy-connection:"):
                        continue
                    out_headers.append(l + b"\r\n")
                out_headers.append(b"\r\n")
                remote.sendall(b"".join(out_headers) + body)
                self._pipe(self.client, remote)
        except Exception:
            pass
        finally:
            try:
                self.client.close()
            except Exception:
                pass
            if remote:
                try:
                    remote.close()
                except Exception:
                    pass

    @staticmethod
    def _pipe(s1, s2):
        sockets = [s1, s2]
        try:
            while True:
                r, _, _ = select.select(sockets, [], [], 20)
                if not r:
                    break
                for s in r:
                    data = s.recv(32768)
                    if not data:
                        return
                    other = s2 if s is s1 else s1
                    other.sendall(data)
        except Exception:
            pass
        finally:
            for s in sockets:
                try:
                    s.close()
                except Exception:
                    pass


class _LocalDoHProxyServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(64)
        self.running = True
        self.ready_event = threading.Event()

    def run(self):
        self.ready_event.set()
        while self.running:
            try:
                client, _ = self.sock.accept()
                _LocalDoHProxyHandler(client).start()
            except Exception:
                break

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


def get_doh_proxy_url() -> str | None:
    """
    Returns the local DoH forward proxy URL ('http://127.0.0.1:<port>') for external
    processes (such as Deno for Nuvio plugins or yt-dlp) to route through DoH.
    Starts the proxy server lazily on demand if DoH is enabled.
    """
    global _proxy_server, _proxy_port
    if not is_global_doh_enabled():
        return None

    with _proxy_lock:
        if _proxy_server is not None and _proxy_server.is_alive():
            return f"http://127.0.0.1:{_proxy_port}"

        try:
            server = _LocalDoHProxyServer()
            server.start()
            server.ready_event.wait(timeout=2.0)
            _proxy_server = server
            _proxy_port = server.port
            logger.info(f"Local DoH proxy server started on 127.0.0.1:{_proxy_port}")
            return f"http://127.0.0.1:{_proxy_port}"
        except Exception as e:
            logger.error(f"Failed to start local DoH proxy server: {e}")
            return None


def stop_doh_proxy():
    """Stops the local DoH proxy server if running."""
    global _proxy_server, _proxy_port
    with _proxy_lock:
        if _proxy_server is not None:
            try:
                _proxy_server.stop()
            except Exception:
                pass
            _proxy_server = None
            _proxy_port = 0
            logger.info("Local DoH proxy server stopped.")


def get_doh_env() -> dict:
    """
    Returns environment variable dictionary with HTTP_PROXY and HTTPS_PROXY
    configured to use the local DoH proxy if DoH is enabled, or empty dict.
    """
    if not is_global_doh_enabled():
        return {}
    url = get_doh_proxy_url()
    if not url:
        return {}
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "ALL_PROXY": url,
        "all_proxy": url,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }


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
            stop_doh_proxy()


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
