# local_server.py
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Request guards shared by PopcornBox's loopback HTTP servers.

The HLS slicing proxy, the seek proxy and the torrent stream server listen on
127.0.0.1 so mpv can read from them. Binding to loopback alone does not keep
web pages out: a site can point its own hostname at 127.0.0.1 (DNS rebinding)
and then read the responses as same-origin. Browsers always send the name they
resolved in the Host header, so accepting only loopback names stops that.
Session paths are additionally keyed by unguessable tokens.
"""

import secrets

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def is_loopback_host_header(host_value, port=None) -> bool:
    """Return True when an HTTP Host header names this loopback server.

    A missing Host header is accepted: HTTP/1.0 clients may omit it and a
    browser never does, so it cannot be used for DNS rebinding.
    """
    if host_value is None:
        return True
    host = str(host_value).strip().lower()
    if not host:
        return True
    if host.startswith("["):
        close = host.find("]")
        if close < 0:
            return False
        name, rest = host[:close + 1], host[close + 1:]
        if rest and not rest.startswith(":"):
            return False
        host_port = rest[1:]
    elif host.count(":") == 1:
        name, host_port = host.split(":", 1)
    else:
        # No port, or a bare IPv6 literal such as "::1".
        name, host_port = host, ""
    if name not in _LOOPBACK_NAMES:
        return False
    if host_port and port is not None:
        try:
            return int(host_port) == int(port)
        except (TypeError, ValueError):
            return False
    return True


def reject_foreign_host(handler) -> bool:
    """Send 403 and return True when *handler*'s request is not for loopback.

    *handler* is a ``BaseHTTPRequestHandler``. Call it first in ``do_GET`` /
    ``do_HEAD`` and return immediately when it answers True.
    """
    try:
        port = handler.server.server_address[1]
    except Exception:
        port = None
    if is_loopback_host_header(handler.headers.get("Host"), port):
        return False
    try:
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.send_header("Connection", "close")
        handler.end_headers()
    except Exception:
        pass
    handler.close_connection = True
    return True


def new_session_id() -> str:
    """Return an unguessable, URL-safe session identifier."""
    return secrets.token_hex(16)
