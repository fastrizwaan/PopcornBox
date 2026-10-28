"""
HLS Slicing Proxy Server for PopcornBox.

Solves FFmpeg / MPV buffer underrun bug on fragmented MP4 (fMP4) HLS streams.
When FFmpeg seeks in fMP4 HLS streams with #EXT-X-MAP, FFmpeg jumps directly to
a media fragment without reloading the init segment (init.mp4), resulting in
corrupt NAL units, decoder starvation, and unrecoverable buffer underruns.

This module runs a lightweight local HTTP proxy on 127.0.0.1 that parses HLS
playlists, detects fMP4 streams, caches segment URLs, and generates dynamically
sliced playlists on demand starting at any seek offset. MPV opens the sliced
playlist as a clean new stream with #EXT-X-MAP at the beginning, allowing instant
resuming and seeking in 1-2 seconds without freezing.
"""

import bisect
import hashlib
import logging
import re
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from gi.repository import GLib

logger = logging.getLogger("PopcornBox.HLSProxy")


class HlsSession:
    """Represents a parsed HLS VOD playlist session."""

    def __init__(self, session_id: str, url: str, headers: dict = None):
        self.session_id = session_id
        self.url = url
        self.headers = dict(headers or {})
        self.header_lines = []
        self.segments = []  # list of (duration, inf_line, resolved_seg_url)
        self.cumulative_times = []  # list of float start times
        self.total_duration = 0.0
        self.is_fmp4 = False
        self.is_vod = True
        self.target_duration = 4.0
        self.media_sequence = 0
        self.is_ready = False
        self.created_at = time.time()

    def fetch_and_parse(self, timeout: float = 15.0):
        req_headers = dict(self.headers)
        if not any(k.lower() == "user-agent" for k in req_headers):
            req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

        text = None
        current_url = self.url
        for attempt in range(2):
            try:
                req = urllib.request.Request(current_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="ignore")
                break
            except urllib.error.HTTPError as he:
                if he.code in (402, 429):
                    # Payment required / Quota exceeded / Too Many Requests: do not retry!
                    from . import api
                    api.set_provider_cooldown(current_url, duration_seconds=900, reason=f"HTTP {he.code} Payment Required / Quota Exceeded")
                    raise
                elif he.code == 428 and any(k.lower() == "user-agent" for k in req_headers):
                    req_headers = {k: v for k, v in req_headers.items() if k.lower() != "user-agent"}
                elif he.code in (401, 403) and not any(k.lower() == "user-agent" for k in req_headers):
                    req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                else:
                    if he.code == 403:
                        from . import api
                        api.set_provider_cooldown(current_url, duration_seconds=600, reason="HTTP 403 Forbidden (Blocked)")
                    raise

        if not text:
            raise RuntimeError(f"Empty playlist response for {self.url}")

        # Check if master playlist (#EXT-X-STREAM-INF)
        if "#EXT-X-STREAM-INF" in text:
            variant_url = self._extract_best_variant_url(text, current_url)
            if variant_url:
                logger.info(f"[HLSProxy] Master playlist detected, following variant: {variant_url}")
                current_url = variant_url
                self.url = current_url
                req = urllib.request.Request(current_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="ignore")

        lines = text.splitlines()
        headers = []
        segments = []
        cum_times = []
        cur_time = 0.0
        i = 0
        in_header = True

        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            if line.startswith("#EXT-X-MAP:"):
                self.is_fmp4 = True
                # Resolve relative URI in #EXT-X-MAP:URI="..."
                m_match = re.search(r'URI="([^"]+)"', line)
                if m_match:
                    sub_url = m_match.group(1)
                    full_map = urllib.parse.urljoin(current_url, sub_url)
                    line = line.replace(f'URI="{sub_url}"', f'URI="{full_map}"')
                headers.append(line)
                i += 1
            elif line.startswith("#EXT-X-KEY:"):
                # Resolve relative URI in #EXT-X-KEY:...,URI="..."
                m_match = re.search(r'URI="([^"]+)"', line)
                if m_match:
                    sub_url = m_match.group(1)
                    full_key = urllib.parse.urljoin(current_url, sub_url)
                    line = line.replace(f'URI="{sub_url}"', f'URI="{full_key}"')
                headers.append(line)
                i += 1
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    self.target_duration = float(line.split(":")[1])
                except Exception:
                    pass
                headers.append(line)
                i += 1
            elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    self.media_sequence = int(line.split(":")[1])
                except Exception:
                    pass
                i += 1
            elif line.startswith("#EXTINF:"):
                in_header = False
                inf_line = line
                try:
                    dur_str = line[8:].split(",", 1)[0].strip()
                    dur = float(dur_str)
                except Exception:
                    dur = self.target_duration or 4.0
                i += 1
                # Skip any intermediate comment lines before segment URI
                while i < len(lines) and lines[i].strip().startswith("#") and not lines[i].strip().startswith("#EXTINF:"):
                    i += 1
                if i < len(lines):
                    seg_url = lines[i].strip()
                    full_seg_url = urllib.parse.urljoin(current_url, seg_url)
                    segments.append((dur, inf_line, full_seg_url))
                    cum_times.append(cur_time)
                    cur_time += dur
                    i += 1
            elif in_header:
                if not line.startswith("#EXT-X-ENDLIST"):
                    headers.append(line)
                i += 1
            else:
                i += 1

        self.header_lines = headers
        self.segments = segments
        self.cumulative_times = cum_times
        self.total_duration = cur_time
        self.is_ready = True
        return self

    def _extract_best_variant_url(self, master_text: str, base_url: str) -> str:
        lines = master_text.splitlines()
        variants = []
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF:"):
                bw = 0
                res = 0
                bw_m = re.search(r"BANDWIDTH=(\d+)", l)
                if bw_m:
                    bw = int(bw_m.group(1))
                res_m = re.search(r"RESOLUTION=\d+x(\d+)", l)
                if res_m:
                    res = int(res_m.group(1))
                if i + 1 < len(lines):
                    v_url = lines[i + 1].strip()
                    if v_url and not v_url.startswith("#"):
                        full_v_url = urllib.parse.urljoin(base_url, v_url)
                        variants.append((res, bw, full_v_url))

        if variants:
            # Sort by resolution, then bandwidth descending
            variants.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return variants[0][2]
        return ""

    def slice_manifest(self, offset: float) -> tuple[bytes, float]:
        if not self.segments or offset <= 0:
            start_idx = 0
            actual_offset = 0.0
        else:
            idx = bisect.bisect_right(self.cumulative_times, offset) - 1
            start_idx = max(0, min(idx, len(self.segments) - 1))
            actual_offset = self.cumulative_times[start_idx]

        sliced = self.segments[start_idx:]
        out = []
        for h in self.header_lines:
            out.append(h)
            if h.startswith("#EXT-X-TARGETDURATION:"):
                out.append(f"#EXT-X-MEDIA-SEQUENCE:{self.media_sequence + start_idx}")
        for _dur, inf_line, full_url in sliced:
            out.append(inf_line)
            out.append(full_url)
        out.append("#EXT-X-ENDLIST")
        return "\n".join(out).encode("utf-8"), actual_offset


class _HlsProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "hls":
            session_id = parts[1]
            session = _sessions.get(session_id)
            if session:
                qs = urllib.parse.parse_qs(parsed.query)
                offset = 0.0
                try:
                    offset = float(qs.get("offset", [0])[0])
                except (ValueError, TypeError):
                    offset = 0.0
                body, actual_off = session.slice_manifest(offset)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-HLS-Total-Duration", str(session.total_duration))
                self.send_header("X-HLS-Actual-Offset", str(actual_off))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        # Silence standard HTTP access logging to keep console clean
        pass


_server_lock = threading.Lock()
_proxy_server = None
_proxy_port = None
_sessions = {}


def start_proxy_server() -> int:
    """Ensures the local HLS slicing proxy server is running and returns its port."""
    global _proxy_server, _proxy_port
    with _server_lock:
        if _proxy_server is not None and _proxy_port is not None:
            return _proxy_port

        server = ThreadingHTTPServer(("127.0.0.1", 0), _HlsProxyHandler)
        _proxy_port = server.server_address[1]
        _proxy_server = server
        threading.Thread(target=server.serve_forever, daemon=True, name="HlsProxyServer").start()
        logger.info(f"[HLSProxy] Slicing proxy listening on 127.0.0.1:{_proxy_port}")
        return _proxy_port


def is_hls_stream(url: str) -> bool:
    """Check if a URL points to an HLS stream manifest."""
    if not url or not isinstance(url, str):
        return False
    u = url.lower()
    return any(x in u for x in [".m3u8", "/hls/", "manifest.m3u8"])


def get_sliced_url(session_id: str, offset: float = 0.0) -> str:
    """Get the local HTTP URL for a sliced HLS playlist."""
    port = start_proxy_port()
    return f"http://127.0.0.1:{port}/hls/{session_id}/playlist.m3u8?offset={offset:.2f}"


def start_proxy_port() -> int:
    return start_proxy_server()


def close_session(session_id: str):
    """Release session resources when playback ends."""
    if session_id in _sessions:
        try:
            del _sessions[session_id]
        except KeyError:
            pass


def prepare_hls_stream_async(url: str, headers: dict = None, seek_target: float = 0.0, callback = None):
    """
    Asynchronously prepare an HLS stream.
    If the stream is fragmented MP4 (fMP4 with #EXT-X-MAP), parses the manifest
    into memory, registers a proxy session, and yields the sliced local URL.
    Otherwise yields is_fmp4=False so normal MPV playback can proceed.
    
    callback(is_fmp4: bool, session_id: str | None, playback_url: str | None, total_duration: float | None, actual_offset: float, is_blocked: bool = False)
    """
    def _worker():
        try:
            from . import api
            if api.is_provider_in_cooldown(url):
                rem = api.get_provider_cooldown_remaining(url)
                logger.warning(f"[HLSProxy] Skipping HLS stream: host is in cooldown for {rem}s: {url}")
                if callback:
                    GLib.idle_add(callback, False, None, None, None, 0.0, True)
                return

            session_id = hashlib.md5(f"{url}_{time.time()}".encode()).hexdigest()[:12]
            session = HlsSession(session_id, url, headers=headers).fetch_and_parse(timeout=10.0)
            if session.is_fmp4 and session.segments:
                _sessions[session_id] = session
                sliced_url = get_sliced_url(session_id, offset=seek_target)
                logger.info(f"[HLSProxy] Prepared fMP4 HLS session {session_id}: {len(session.segments)} segs, dur={session.total_duration:.1f}s")
                if callback:
                    GLib.idle_add(callback, True, session_id, sliced_url, session.total_duration, seek_target, False)
                return
            else:
                logger.debug(f"[HLSProxy] Stream is not fMP4 or has no segments: {url}")
                if callback:
                    GLib.idle_add(callback, False, None, url, session.total_duration if session.total_duration > 0 else None, 0.0, False)
                return
        except urllib.error.HTTPError as he:
            is_blocked = he.code in (402, 403, 429)
            logger.warning(f"[HLSProxy] HLS stream HTTP error {he.code}: {he.reason} ({url})")
            if callback:
                GLib.idle_add(callback, False, None, None if is_blocked else url, None, 0.0, is_blocked)
        except Exception as e:
            logger.warning(f"[HLSProxy] Failed to prepare HLS stream ({e}), falling back to direct URL")
            if callback:
                GLib.idle_add(callback, False, None, url, None, 0.0, False)

    threading.Thread(target=_worker, daemon=True, name="HlsPrepareWorker").start()
