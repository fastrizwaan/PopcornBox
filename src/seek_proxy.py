"""
Local caching proxy that makes non-seekable HTTP streams seekable.

Some hosts (for example Google's video-downloads CDN) ignore the HTTP Range
header and always answer 200 with the whole body. mpv then marks the stream as
not seekable: seeking fails, and switching audio tracks cannot refresh the new
track's packets.

This proxy downloads such a stream once, front to back, into a temporary cache
file, and serves it to mpv on 127.0.0.1 with full Range support. Reads of
bytes that are already downloaded return immediately; reads past the download
position wait until the download gets there.
"""

import atexit
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gi.repository import GLib

logger = logging.getLogger("PopcornBox.SeekProxy")

_CHUNK = 256 * 1024
_FREE_SPACE_MARGIN = 512 * 1024 * 1024
_MAX_RETRIES = 5
# Matroska keeps its seek index (Cues) and often its Tags at the end of the
# file. Waiting for those would stall the first seek until the whole file is
# cached, so a jump into the tail that is this far ahead is answered with zero
# bytes. mpv finds no Cues element there and builds its index from the
# clusters it reads instead. Failing the request outright is worse: a dropped
# connection makes ffmpeg retry for about 11 seconds, and an HTTP error leaves
# its read position out of step so the next seek lands at EOF.
_MKV_FAR_AHEAD = 8 * 1024 * 1024
_MKV_TAIL = 32 * 1024 * 1024
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Outcomes of prepare_async(); the callback receives one of these.
RESULT_PROXIED = "proxied"          # playback_url is a local seekable URL
RESULT_NOT_NEEDED = "not-needed"    # upstream honours Range already
RESULT_UNSUPPORTED = "unsupported"  # no size, too big for the disk, or an error


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "popcorn-box", "seek-cache")


def _request(url: str, headers: dict, start: int = 0, timeout: float = 20.0):
    headers = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = _DEFAULT_USER_AGENT
    # An empty value means "send no such header" (some hosts reject browser
    # User-Agents), so drop those.
    req_headers = {k: v for k, v in headers.items() if k and v}
    req_headers["Range"] = f"bytes={start}-"
    # Identity encoding keeps byte offsets equal to file offsets.
    req_headers["Accept-Encoding"] = "identity"
    return urllib.request.urlopen(urllib.request.Request(url, headers=req_headers), timeout=timeout)


def _content_range_start(resp) -> int | None:
    m = re.match(r"bytes\s+(\d+)-", resp.headers.get("Content-Range") or "")
    return int(m.group(1)) if m else None


class SeekSession:
    def __init__(self, session_id: str, url: str, headers: dict, size: int, content_type: str, path: str):
        self.id = session_id
        self.url = url
        self.headers = dict(headers or {})
        self.size = size
        self.content_type = content_type or "application/octet-stream"
        self.path = path
        self.downloaded = 0
        self.error = None
        self.closed = False
        self.is_mkv = False
        self.cond = threading.Condition()
        self._wfd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        self._rfd = os.open(path, os.O_RDONLY)

    @property
    def complete(self) -> bool:
        return self.downloaded >= self.size

    def start(self, first_response):
        threading.Thread(target=self._download, args=(first_response,), daemon=True,
                         name=f"SeekProxyDownload-{self.id}").start()

    def _download(self, resp):
        retries = 0
        try:
            while not self.closed and not self.complete:
                try:
                    if resp is None:
                        resp = _request(self.url, self.headers, self.downloaded)
                    # A 200 restarts at byte 0, so skip what is already cached.
                    skip = self.downloaded if resp.status == 200 else 0
                    if resp.status == 206 and _content_range_start(resp) != self.downloaded:
                        raise IOError(f"unexpected Content-Range {resp.headers.get('Content-Range')!r}")
                    while not self.closed and not self.complete:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            raise IOError("upstream closed early")
                        if skip:
                            drop = min(skip, len(chunk))
                            skip -= drop
                            chunk = chunk[drop:]
                            if not chunk:
                                continue
                        chunk = chunk[: self.size - self.downloaded]
                        # File descriptors are only used under the lock, so
                        # close() can never free one mid-write for reuse.
                        with self.cond:
                            if self.closed:
                                break
                            if self.downloaded == 0:
                                self.is_mkv = chunk.startswith(_EBML_MAGIC)
                            os.pwrite(self._wfd, chunk, self.downloaded)
                            self.downloaded += len(chunk)
                            self.cond.notify_all()
                        retries = 0
                except Exception as e:
                    if self.closed:
                        break
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise
                    logger.info(f"[SeekProxy] {self.id}: download interrupted at {self.downloaded} ({e}); "
                                f"retry {retries}/{_MAX_RETRIES}")
                    time.sleep(min(2 ** retries, 10))
                finally:
                    try:
                        if resp is not None:
                            resp.close()
                    except Exception:
                        pass
                    resp = None
            if self.complete:
                logger.info(f"[SeekProxy] {self.id}: download complete ({self.size} bytes)")
        except Exception as e:
            logger.warning(f"[SeekProxy] {self.id}: download failed at {self.downloaded}/{self.size}: {e}")
            with self.cond:
                self.error = str(e) or type(e).__name__
                self.cond.notify_all()

    def refuses(self, offset: int) -> bool:
        """True for a Matroska tail read that would wait for most of the file."""
        return (self.is_mkv and not self.complete
                and offset > self.downloaded + _MKV_FAR_AHEAD
                and offset >= self.size - _MKV_TAIL)

    def wait_for(self, offset: int) -> bool:
        """Block until byte `offset` is cached. False if it never will be."""
        with self.cond:
            while self.downloaded <= offset:
                if self.closed or self.error:
                    return False
                self.cond.wait(1.0)
            return True

    def read(self, offset: int, length: int) -> bytes:
        with self.cond:
            avail = min(self.downloaded, self.size) - offset
            if self.closed or avail <= 0:
                return b""
            return os.pread(self._rfd, min(length, avail), offset)

    def close(self):
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
            for fd in (self._wfd, self._rfd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class _SeekProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _session(self):
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "seek":
            return _sessions.get(parts[1])
        return None

    def _parse_range(self, size: int):
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if not m or (not m.group(1) and not m.group(2)):
            return None
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start = max(0, size - int(m.group(2)))
            end = size - 1
        return start, min(end, size - 1)

    def _send_headers(self, session):
        size = session.size
        rng = self._parse_range(size)
        if rng and rng[0] >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        start, end = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", session.content_type)
        self.send_header("Accept-Ranges", "bytes")
        # Each response is one read stream; ffmpeg must not reuse the socket.
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        return start, end

    def do_HEAD(self):
        session = self._session()
        if not session or session.closed:
            self.send_error(404)
            return
        self._send_headers(session)

    def do_GET(self):
        session = self._session()
        if not session or session.closed:
            self.send_error(404)
            return
        span = self._send_headers(session)
        if not span:
            return
        pos, end = span
        logger.debug(f"[SeekProxy] {session.id}: GET {pos}-{end} (cached {session.downloaded}/{session.size})")
        try:
            if session.refuses(pos):
                zeros = bytes(_CHUNK)
                while pos <= end:
                    n = min(_CHUNK, end - pos + 1)
                    self.wfile.write(zeros[:n])
                    pos += n
            while pos <= end:
                if not session.wait_for(pos):
                    break  # closing the connection tells mpv the read failed
                data = session.read(pos, min(_CHUNK, end - pos + 1))
                if not data:
                    break
                self.wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.debug(f"[SeekProxy] {session.id}: client left at {pos} ({type(e).__name__})")
        self.close_connection = True

    def log_message(self, *args):
        pass


_lock = threading.Lock()
_server = None
_port = None
_sessions: dict[str, SeekSession] = {}
_cleaned = False


def _ensure_server() -> int:
    global _server, _port, _cleaned
    with _lock:
        if not _cleaned:
            # Remove cache files left behind by a crash.
            shutil.rmtree(_cache_dir(), ignore_errors=True)
            _cleaned = True
        if _server is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _SeekProxyHandler)
            server.daemon_threads = True
            _port = server.server_address[1]
            _server = server
            threading.Thread(target=server.serve_forever, daemon=True, name="SeekProxyServer").start()
            logger.info(f"[SeekProxy] listening on 127.0.0.1:{_port}")
        return _port


def is_proxy_url(url) -> bool:
    return isinstance(url, str) and url.startswith("http://127.0.0.1:") and "/seek/" in url


def close_all():
    """Stop every download and delete its cache file."""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


atexit.register(close_all)


def prepare(url: str, headers: dict = None):
    """Probe `url` and, if it ignores Range, start caching it.

    Blocking; call it off the main thread. Returns (result, playback_url).
    """
    headers = headers or {}
    try:
        resp = _request(url, headers, 0, timeout=10.0)
    except urllib.error.HTTPError as e:
        logger.info(f"[SeekProxy] upstream HTTP {e.code}; not proxying")
        return RESULT_UNSUPPORTED, None
    except Exception as e:
        logger.info(f"[SeekProxy] upstream unreachable ({e}); not proxying")
        return RESULT_UNSUPPORTED, None

    keep = False
    try:
        if resp.status == 206:
            return RESULT_NOT_NEEDED, None
        length = resp.headers.get("Content-Length") or ""
        encoding = (resp.headers.get("Content-Encoding") or "identity").lower()
        if resp.status != 200 or not length.isdigit() or int(length) <= 0 or encoding != "identity":
            logger.info(f"[SeekProxy] upstream status={resp.status} length={length!r} encoding={encoding}; not proxying")
            return RESULT_UNSUPPORTED, None
        size = int(length)
        cache_dir = _cache_dir()
        port = _ensure_server()
        os.makedirs(cache_dir, exist_ok=True)
        free = shutil.disk_usage(cache_dir).free
        if free < size + _FREE_SPACE_MARGIN:
            logger.warning(f"[SeekProxy] {size} byte stream does not fit in {free} free bytes; not proxying")
            return RESULT_UNSUPPORTED, None

        close_all()  # one stream at a time
        session_id = hashlib.sha1(f"{url}|{time.time()}".encode()).hexdigest()[:16]
        session = SeekSession(session_id, url, headers, size, resp.headers.get("Content-Type"),
                              os.path.join(cache_dir, f"{session_id}.part"))
        with _lock:
            _sessions[session_id] = session
        session.start(resp)
        keep = True
        name = os.path.basename(urllib.parse.urlparse(url).path) or "stream"
        name = urllib.parse.quote(name[-80:], safe="._-")
        logger.info(f"[SeekProxy] caching {size} byte stream as session {session_id}")
        return RESULT_PROXIED, f"http://127.0.0.1:{port}/seek/{session_id}/{name}"
    except Exception as e:
        logger.warning(f"[SeekProxy] could not start caching proxy: {e}")
        return RESULT_UNSUPPORTED, None
    finally:
        if not keep:
            try:
                resp.close()
            except Exception:
                pass


def prepare_async(url: str, headers: dict = None, callback=None):
    """prepare() on a worker thread; callback(result, playback_url) runs on
    the GLib main loop."""
    def _worker():
        result, playback_url = prepare(url, headers)
        if callback:
            GLib.idle_add(lambda: callback(result, playback_url) and False)

    threading.Thread(target=_worker, daemon=True, name="SeekProxyPrepare").start()
