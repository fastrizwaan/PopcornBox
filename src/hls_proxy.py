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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from gi.repository import GLib

logger = logging.getLogger("PopcornBox.HLSProxy")

MAX_PLAYLIST_BYTES = 8 * 1024 * 1024

_FAKE_IMAGE_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
    b"BM",
    b"RIFF",
)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _build_request_headers(raw_headers: dict = None) -> dict:
    """Return a normalized header dict ensuring a browser User-Agent is present."""
    headers = dict(raw_headers or {})
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = _DEFAULT_USER_AGENT
    return headers


def _find_mpegts_sync_offset(data: bytes) -> int:
    """Return the byte offset of embedded MPEG-TS packets if prepended with a fake header, else 0."""
    if not data or len(data) < 189:
        return 0
    if data.startswith(b"ID3") or data[4:8] in (b"ftyp", b"styp", b"moof", b"sidx"):
        return 0
    has_fake_prefix = data.startswith(_FAKE_IMAGE_PREFIXES)
    # Valid MPEG-TS starts with 0x47 at byte 0 and byte 188 (unless spoofed with GIF8, which starts with 0x47 'G')
    if not has_fake_prefix and data[0] == 0x47 and data[188] == 0x47:
        return 0

    max_scan = min(len(data) - 189, 2048)
    for offset in range(1, max_scan + 1):
        if data[offset] == 0x47 and data[offset + 188] == 0x47:
            if offset + 376 < len(data):
                if data[offset + 376] == 0x47:
                    return offset
            elif has_fake_prefix or offset >= 8:
                return offset
    return 0


def _read_playlist_text(response) -> str:
    """Read a playlist with a hard cap so a remote server cannot exhaust memory."""
    data = bytearray()
    while len(data) <= MAX_PLAYLIST_BYTES:
        chunk = response.read(min(64 * 1024, MAX_PLAYLIST_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > MAX_PLAYLIST_BYTES:
        raise ValueError(f"HLS playlist exceeds {MAX_PLAYLIST_BYTES} bytes")
    return data.decode("utf-8", errors="ignore")


def _sanitize_url(url: str) -> str:
    """Sanitize URL path by percent-encoding spaces and brackets."""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        clean_path = urllib.parse.unquote(parts.path)
        quoted_path = urllib.parse.quote(clean_path, safe="/:@&=+$,;~*()!'")
        clean_query = parts.query.replace(" ", "%20") if parts.query else ""
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, quoted_path, clean_query, parts.fragment))
    except Exception:
        return url.replace(" ", "%20")


def _resolve_relative_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL against a base URL, preserving query parameters/tokens when appropriate."""
    full_url = urllib.parse.urljoin(base_url, relative_url)
    parsed_base = urllib.parse.urlparse(base_url)
    parsed_rel = urllib.parse.urlparse(relative_url)
    if parsed_base.query and not parsed_rel.query and not parsed_rel.netloc:
        delimiter = "&" if "?" in full_url else "?"
        full_url = f"{full_url}{delimiter}{parsed_base.query}"
    return _sanitize_url(full_url)


class HlsSession:
    """Represents a parsed HLS VOD playlist session."""

    def __init__(self, session_id: str, url: str, headers: dict = None):
        self.session_id = session_id
        self.url = _sanitize_url(url)
        self.headers = dict(headers or {})
        self.header_lines = []
        self.segments = []  # list of (duration, inf_line, resolved_seg_url)
        self.cumulative_times = []  # list of float start times
        self.total_duration = 0.0
        self.is_fmp4 = False
        self.is_segment_proxy = False
        self._seg_sync_offset = 0
        self.is_vod = True
        self.target_duration = 4.0
        self.media_sequence = 0
        self.is_ready = False
        self.is_master_proxy = False
        self.master_sanitized_text = ""
        self.extracted_subtitles = []
        self.variant_url = ""
        self.created_at = time.time()

    def fetch_and_parse(self, timeout: float = 15.0):
        req_headers = _build_request_headers(self.headers)

        text = None
        current_url = self.url
        for attempt in range(2):
            try:
                req = urllib.request.Request(current_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = _read_playlist_text(resp)
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
            if "#EXT-X-MEDIA:" in text and re.search(r'#EXT-X-MEDIA:[^\n\r]*TYPE=SUBTITLES', text, re.IGNORECASE):
                self.extracted_subtitles = self._extract_subtitles(text, current_url, req_headers)
                logger.info(f"[HLSProxy] Extracted {len(self.extracted_subtitles)} subtitles from master playlist")

            # If the master playlist defines separate audio tracks (#EXT-X-MEDIA:TYPE=AUDIO),
            # following only a video variant strips the audio entirely and results in silent video.
            # Direct MPV playback handles multi-rendition master playlists with separate audio natively.
            if "#EXT-X-MEDIA:" in text and re.search(r'#EXT-X-MEDIA:[^\n\r]*TYPE=AUDIO', text, re.IGNORECASE):
                if self.extracted_subtitles:
                    self.master_sanitized_text = self._strip_subtitles_from_master(text, current_url)
                    self.is_master_proxy = True
                    self.is_fmp4 = False
                    self.is_ready = True
                    logger.info(f"[HLSProxy] Master playlist has separate audio and subtitles; serving sanitized master without subtitles: {current_url}")
                    return self
                else:
                    logger.info(f"[HLSProxy] Master playlist has separate audio tracks (#EXT-X-MEDIA:TYPE=AUDIO), passing through for native MPV playback: {current_url}")
                    self.is_fmp4 = False
                    self.is_ready = True
                    return self

            variant_url = self._extract_best_variant_url(text, current_url)
            if variant_url:
                logger.info(f"[HLSProxy] Master playlist detected, following variant: {variant_url}")
                current_url = variant_url
                self.variant_url = variant_url
                self.url = current_url
                req = urllib.request.Request(current_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = _read_playlist_text(resp)

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
                    full_map = _resolve_relative_url(current_url, sub_url)
                    line = line.replace(f'URI="{sub_url}"', f'URI="{full_map}"')
                headers.append(line)
                i += 1
            elif line.startswith("#EXT-X-KEY:"):
                # Resolve relative URI in #EXT-X-KEY:...,URI="..."
                m_match = re.search(r'URI="([^"]+)"', line)
                if m_match:
                    sub_url = m_match.group(1)
                    full_key = _resolve_relative_url(current_url, sub_url)
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
                    full_seg_url = _resolve_relative_url(current_url, seg_url)
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
        is_encrypted = any(
            h.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in h
            for h in headers
        )
        if not self.is_fmp4 and not is_encrypted and segments:
            self._probe_segment_header(req_headers, timeout=min(timeout, 5.0))
        return self

    def _probe_segment_header(self, req_headers: dict, timeout: float = 5.0):
        """Probe the first segment for fake binary/image headers before MPEG-TS sync bytes."""
        if not self.segments:
            return
        first_url = self.segments[0][2]
        if not first_url or not first_url.startswith(("http://", "https://")):
            return
        probe_headers = dict(req_headers)
        probe_headers["Range"] = "bytes=0-1023"
        try:
            req = urllib.request.Request(first_url, headers=probe_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sample = resp.read(1024)
            offset = _find_mpegts_sync_offset(sample)
            if offset > 0:
                self.is_segment_proxy = True
                self._seg_sync_offset = offset
                logger.info(
                    f"[HLSProxy] Detected {offset}-byte disguised header before MPEG-TS in {first_url[:80]}; using segment proxy"
                )
        except Exception as e:
            logger.debug(f"[HLSProxy] Segment header probe skipped ({e})")

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
                        full_v_url = _resolve_relative_url(base_url, v_url)
                        variants.append((res, bw, full_v_url))

        if variants:
            # Sort by resolution, then bandwidth descending
            variants.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return variants[0][2]
        return ""

    def _extract_subtitles(self, master_text: str, base_url: str, req_headers: dict) -> list[dict]:
        lines = master_text.splitlines()
        subtitles = []
        track_id = 1
        for line in lines:
            line = line.strip()
            if not line.startswith("#EXT-X-MEDIA:") or not re.search(r"TYPE=SUBTITLES", line, re.IGNORECASE):
                continue

            uri_m = re.search(r'URI="([^"]+)"', line)
            if not uri_m:
                continue
            sub_uri = _resolve_relative_url(base_url, uri_m.group(1))

            name_m = re.search(r'NAME="([^"]+)"', line)
            name = name_m.group(1) if name_m else "Subtitles"

            lang_m = re.search(r'LANGUAGE="([^"]+)"', line)
            lang = lang_m.group(1) if lang_m else ""

            def_m = re.search(r'DEFAULT=(YES|NO)', line, re.IGNORECASE)
            is_default = bool(def_m and def_m.group(1).upper() == "YES")

            auto_m = re.search(r'AUTOSELECT=(YES|NO)', line, re.IGNORECASE)
            is_autoselect = bool(auto_m and auto_m.group(1).upper() == "YES")

            forced_m = re.search(r'FORCED=(YES|NO)', line, re.IGNORECASE)
            is_forced = bool(forced_m and forced_m.group(1).upper() == "YES")

            clean_lang = re.sub(r'[\.\[].*', '', lang).strip() or lang

            resolved_vtt = sub_uri
            clean_path = sub_uri.split("?", 1)[0].lower()
            if clean_path.endswith(".m3u8") or ".m3u8" in sub_uri.lower():
                try:
                    s_req = urllib.request.Request(sub_uri, headers=req_headers)
                    with urllib.request.urlopen(s_req, timeout=5.0) as resp:
                        s_text = _read_playlist_text(resp)
                    for s_line in s_text.splitlines():
                        s_line = s_line.strip()
                        if not s_line or s_line.startswith("#"):
                            continue
                        resolved_vtt = _resolve_relative_url(sub_uri, s_line)
                        break
                except Exception as e:
                    logger.warning(f"[HLSProxy] Could not fetch inner subtitle playlist {sub_uri}: {e}")
                    resolved_vtt = sub_uri

            subtitles.append({
                "type": "sub",
                "id": track_id,
                "url": resolved_vtt,
                "title": name,
                "lang": clean_lang,
                "raw_lang": lang,
                "default": is_default,
                "autoselect": is_autoselect,
                "forced": is_forced,
                "external": True,
            })
            track_id += 1
        return subtitles

    def _strip_subtitles_from_master(self, master_text: str, base_url: str) -> str:
        lines = master_text.splitlines()
        out = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                out.append("")
                i += 1
                continue
            if line.startswith("#EXT-X-MEDIA:"):
                if re.search(r"TYPE=SUBTITLES", line, re.IGNORECASE):
                    i += 1
                    continue
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    resolved = _resolve_relative_url(base_url, m.group(1))
                    line = line.replace(f'URI="{m.group(1)}"', f'URI="{resolved}"')
                out.append(line)
                i += 1
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                line = re.sub(r',?\s*SUBTITLES="[^"]*"', '', line)
                line = re.sub(r'SUBTITLES="[^"]*",?\s*', '', line)
                out.append(line)
                i += 1
                if i < len(lines):
                    var_line = lines[i].strip()
                    if var_line and not var_line.startswith("#"):
                        resolved_var = _resolve_relative_url(base_url, var_line)
                        out.append(resolved_var)
                        i += 1
                continue
            out.append(line)
            i += 1
        return "\n".join(out)

    def slice_manifest(self, offset: float, local_port: int = None) -> tuple[bytes, float]:
        if not self.segments or offset <= 0:
            start_idx = 0
            actual_offset = 0.0
        else:
            idx = bisect.bisect_right(self.cumulative_times, offset) - 1
            start_idx = max(0, min(idx, len(self.segments) - 1))
            actual_offset = self.cumulative_times[start_idx]

        sliced = self.segments[start_idx:]
        out = []
        sequence_tag = f"#EXT-X-MEDIA-SEQUENCE:{self.media_sequence + start_idx}"
        sequence_written = False
        for h in self.header_lines:
            if h.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                if not sequence_written:
                    out.append(sequence_tag)
                    sequence_written = True
                continue

            out.append(h)

        if not sequence_written:
            target_duration_index = next(
                (
                    index
                    for index, line in enumerate(out)
                    if line.startswith("#EXT-X-TARGETDURATION:")
                ),
                None,
            )
            sequence_index = (
                target_duration_index + 1
                if target_duration_index is not None
                else (1 if out and out[0] == "#EXTM3U" else 0)
            )
            out.insert(sequence_index, sequence_tag)
        for idx, (_dur, inf_line, full_url) in enumerate(sliced):
            out.append(inf_line)
            if self.is_segment_proxy and local_port:
                seg_idx = start_idx + idx
                out.append(f"http://127.0.0.1:{local_port}/hls/{self.session_id}/seg/{seg_idx}.ts")
            else:
                out.append(full_url)
        out.append("#EXT-X-ENDLIST")
        return "\n".join(out).encode("utf-8"), actual_offset


class _HlsProxyHandler(BaseHTTPRequestHandler):
    def _serve_stripped_segment(self, session: HlsSession, seg_idx: int):
        """Fetch an upstream segment and strip any prepended non-TS header before streaming."""
        if seg_idx < 0 or seg_idx >= len(session.segments):
            self.send_response(404)
            self.end_headers()
            return

        seg_url = session.segments[seg_idx][2]
        req_headers = _build_request_headers(session.headers)
        req = urllib.request.Request(seg_url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                first_chunk = bytearray()
                min_probe = max(1024, (session._seg_sync_offset or 0) + 512)
                while len(first_chunk) < min_probe:
                    piece = resp.read(65536)
                    if not piece:
                        break
                    first_chunk.extend(piece)

                raw_first = bytes(first_chunk)
                strip_off = _find_mpegts_sync_offset(raw_first)
                payload = raw_first[strip_off:] if strip_off > 0 else raw_first

                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                upstream_len = resp.headers.get("Content-Length")
                if upstream_len and upstream_len.isdigit():
                    adj_len = max(len(payload), int(upstream_len) - strip_off)
                    self.send_header("Content-Length", str(adj_len))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                if payload:
                    self.wfile.write(payload)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except urllib.error.HTTPError as he:
            logger.warning(f"[HLSProxy] Upstream segment HTTP {he.code}: {seg_url[:80]}")
            self.send_response(he.code if 400 <= he.code < 600 else 502)
            self.end_headers()
        except Exception as e:
            logger.debug(f"[HLSProxy] Segment proxy fetch error ({e}): {seg_url[:80]}")
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "hls":
            session_id = parts[1]
            session = _sessions.get(session_id)
            if session:
                if len(parts) >= 4 and parts[2] == "seg":
                    try:
                        seg_idx = int(parts[3].split(".", 1)[0])
                    except (ValueError, TypeError):
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._serve_stripped_segment(session, seg_idx)
                    return

                if session.is_master_proxy:
                    body = session.master_sanitized_text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                qs = urllib.parse.parse_qs(parsed.query)
                offset = 0.0
                try:
                    offset = float(qs.get("offset", [0])[0])
                except (ValueError, TypeError):
                    offset = 0.0
                body, actual_off = session.slice_manifest(offset, local_port=_proxy_port)
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


def get_master_proxy_url(session_id: str) -> str:
    """Get the local HTTP URL for a sanitized HLS master playlist."""
    port = start_proxy_port()
    return f"http://127.0.0.1:{port}/hls/{session_id}/master.m3u8"


def start_proxy_port() -> int:
    return start_proxy_server()


def close_session(session_id: str):
    """Release session resources when playback ends."""
    if session_id in _sessions:
        try:
            del _sessions[session_id]
        except KeyError:
            pass


def is_segment_proxy_session(session_id: str | None) -> bool:
    """Return True if the active HLS proxy session rewrites/strips segments."""
    if not session_id:
        return False
    session = _sessions.get(session_id)
    return bool(session and session.is_segment_proxy)


def prepare_hls_stream_async(url: str, headers: dict = None, seek_target: float = 0.0, callback = None):
    """
    Asynchronously prepare an HLS stream.
    If the stream is fragmented MP4 (fMP4 with #EXT-X-MAP), parses the manifest
    into memory, registers a proxy session, and yields the sliced local URL.
    Otherwise yields is_fmp4=False so normal MPV playback can proceed.
    
    callback(is_fmp4: bool, session_id: str | None, playback_url: str | None, total_duration: float | None, actual_offset: float, is_blocked: bool = False, extracted_subtitles: list | None = None)
    """
    def _worker():
        try:
            from . import api
            if api.is_provider_in_cooldown(url):
                rem = api.get_provider_cooldown_remaining(url)
                logger.warning(f"[HLSProxy] Skipping HLS stream: host is in cooldown for {rem}s: {url}")
                if callback:
                    GLib.idle_add(callback, False, None, None, None, 0.0, True, None)
                return

            session_id = hashlib.md5(f"{url}_{time.time()}".encode()).hexdigest()[:12]
            session = HlsSession(session_id, url, headers=headers).fetch_and_parse(timeout=10.0)
            if session.is_master_proxy:
                _sessions[session_id] = session
                master_url = get_master_proxy_url(session_id)
                logger.info(f"[HLSProxy] Prepared sanitized master HLS session {session_id}: {master_url}")
                if callback:
                    GLib.idle_add(callback, False, session_id, master_url, None, 0.0, False, session.extracted_subtitles)
                return
            elif (session.is_fmp4 or session.is_segment_proxy) and session.segments:
                _sessions[session_id] = session
                sliced_url = get_sliced_url(session_id, offset=seek_target)
                kind = "fMP4" if session.is_fmp4 else "segment-proxy"
                logger.info(f"[HLSProxy] Prepared {kind} HLS session {session_id}: {len(session.segments)} segs, dur={session.total_duration:.1f}s")
                if callback:
                    GLib.idle_add(callback, True, session_id, sliced_url, session.total_duration, seek_target, False, session.extracted_subtitles)
                return
            else:
                effective_playback_url = session.url or url
                logger.debug(f"[HLSProxy] Stream is not fMP4 or has no segments: {effective_playback_url}")
                if callback:
                    GLib.idle_add(callback, False, None, effective_playback_url, session.total_duration if session.total_duration > 0 else None, 0.0, False, session.extracted_subtitles)
                return
        except urllib.error.HTTPError as he:
            logger.warning(f"[HLSProxy] HLS stream HTTP error {he.code}: {he.reason} ({url})")
            if callback:
                # Any HTTP 4xx or 5xx error means the manifest cannot be fetched; do not pass dead URL to MPV
                GLib.idle_add(callback, False, None, None, None, 0.0, True, None)
        except urllib.error.URLError as ue:
            logger.warning(f"[HLSProxy] Failed to connect to HLS stream ({ue.reason}) ({url})")
            if callback:
                # Host not found, connection refused, or network unreachable
                GLib.idle_add(callback, False, None, None, None, 0.0, True, None)
        except Exception as e:
            logger.warning(f"[HLSProxy] Failed to prepare HLS stream ({e}), falling back to direct URL")
            if callback:
                GLib.idle_add(callback, False, None, url, None, 0.0, False, None)

    threading.Thread(target=_worker, daemon=True, name="HlsPrepareWorker").start()
