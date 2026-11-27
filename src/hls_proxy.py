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
from concurrent.futures import ThreadPoolExecutor
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


_H264_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
_VARIANT_PROBE_BYTES = 256 * 1024
_VARIANT_PROBE_TIMEOUT = 5.0
_VARIANT_PROBE_MAX = 6


class _BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u(self, n: int) -> int:
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]  # IndexError on truncated data is handled by caller
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("invalid Exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _h264_sps_height(sps: bytes) -> int:
    """Return the cropped frame height from an H.264 SPS NAL payload (after the NAL header byte)."""
    rbsp = re.sub(b"\x00\x00\x03", b"\x00\x00", bytes(sps))
    r = _BitReader(rbsp)
    profile_idc = r.u(8)
    r.u(16)  # constraint flags + level_idc
    r.ue()  # seq_parameter_set_id
    chroma_format_idc = 1
    if profile_idc in _H264_HIGH_PROFILES:
        chroma_format_idc = r.ue()
        if chroma_format_idc == 3:
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):  # seq_scaling_matrix_present_flag
            for i in range(8 if chroma_format_idc != 3 else 12):
                if r.u(1):
                    last, nxt = 8, 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt != 0:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt if nxt != 0 else last
    r.ue()  # log2_max_frame_num_minus4
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()  # max_num_ref_frames
    r.u(1)
    r.ue()  # pic_width_in_mbs_minus1
    height_map_units = r.ue() + 1
    frame_mbs_only = r.u(1)
    if not frame_mbs_only:
        r.u(1)
    r.u(1)
    height = (2 - frame_mbs_only) * height_map_units * 16
    if r.u(1):  # frame_cropping_flag
        r.ue()
        r.ue()
        crop_top = r.ue()
        crop_bottom = r.ue()
        unit_y = (2 - frame_mbs_only) * (2 if chroma_format_idc == 1 else 1)
        height -= (crop_top + crop_bottom) * unit_y
    return height


def _mpegts_video_height(data: bytes) -> int:
    """Return the H.264 frame height found in the first SPS of an MPEG-TS chunk, or 0."""
    offset = _find_mpegts_sync_offset(data)
    if len(data) < offset + 188 or data[offset] != 0x47:
        return 0
    streams = {}
    for pos in range(offset, len(data) - 187, 188):
        if data[pos] != 0x47:
            break
        pid = ((data[pos + 1] & 0x1F) << 8) | data[pos + 2]
        afc = (data[pos + 3] >> 4) & 3
        start = pos + 4
        if afc & 2:
            start += 1 + data[pos + 4]
        if afc & 1 and start < pos + 188:
            streams.setdefault(pid, bytearray()).extend(data[start:pos + 188])
    for payload in streams.values():
        idx = payload.find(b"\x00\x00\x01")
        while idx != -1 and idx + 4 < len(payload):
            if payload[idx + 3] & 0x1F == 7:
                end = payload.find(b"\x00\x00\x01", idx + 4)
                nal = payload[idx + 4:end if end != -1 else len(payload)]
                try:
                    height = _h264_sps_height(nal)
                except (IndexError, ValueError):
                    height = 0
                if 64 <= height <= 4320:
                    return height
            idx = payload.find(b"\x00\x00\x01", idx + 3)
    return 0


def _parse_master_variants(master_text: str) -> list[dict]:
    """Return the #EXT-X-STREAM-INF entries of a master playlist with their line indices."""
    lines = master_text.splitlines()
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
            j += 1
        if j >= len(lines):
            continue
        bw = re.search(r"(?<![-A-Z])BANDWIDTH=(\d+)", line)
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        variants.append({
            "inf_index": i,
            "uri": lines[j].strip(),
            "bandwidth": int(bw.group(1)) if bw else 0,
            "width": int(res.group(1)) if res else 0,
            "height": int(res.group(2)) if res else 0,
        })
    return variants


def _rewrite_variant_attributes(master_text: str, variants: list[dict], actual_heights: list[int]) -> str:
    """
    Rewrite RESOLUTION/BANDWIDTH so the variant ranking matches the measured
    video heights. Players pick the highest BANDWIDTH, so the advertised
    bandwidths are redistributed in order of real height. Returns the original
    text when the variant a player would pick is already the sharpest one, so
    harmless mislabels lower down never force the app to reload the stream.
    """
    by_real = sorted(range(len(variants)), key=lambda k: actual_heights[k], reverse=True)
    by_bw = sorted(range(len(variants)), key=lambda k: variants[k]["bandwidth"], reverse=True)
    if actual_heights[by_bw[0]] == actual_heights[by_real[0]]:
        return master_text

    bandwidths = sorted((v["bandwidth"] for v in variants), reverse=True)
    lines = master_text.splitlines()
    for rank, k in enumerate(by_real):
        v = variants[k]
        height = actual_heights[k]
        line = lines[v["inf_index"]]
        if v["bandwidth"]:
            line = re.sub(r"(?<![-A-Z])BANDWIDTH=\d+", f"BANDWIDTH={bandwidths[rank]}", line, count=1)
        if v["height"]:
            width = round(v["width"] * height / v["height"]) if v["width"] else round(height * 16 / 9)
            width += width & 1
            line = re.sub(r"RESOLUTION=\d+x\d+", f"RESOLUTION={width}x{height}", line, count=1)
        lines[v["inf_index"]] = line
    return "\n".join(lines)


_VARIANT_PIN_KEY = "pb-variant"


def split_variant_pin(url):
    """
    Split a quality pin off a stream URL. Providers that list each quality as
    its own stream append `#pb-variant=<variant playlist name>` to the master
    URL; the fragment never reaches the network. Returns (clean_url, pin|None).
    """
    if not isinstance(url, str) or "#" not in url:
        return url, None
    base, _sep, fragment = url.partition("#")
    params = urllib.parse.parse_qs(fragment, keep_blank_values=False)
    pin = (params.get(_VARIANT_PIN_KEY) or [None])[0]
    if not pin:
        return url, None
    return base, pin


def _keep_only_variant(master_text: str, variant_pin: str) -> str | None:
    """Return the master with only the #EXT-X-STREAM-INF entry whose URI matches the pin."""
    variants = _parse_master_variants(master_text)
    if not variants:
        return None
    pin = variant_pin.split("?", 1)[0]

    def _matches(uri):
        path = uri.split("?", 1)[0]
        return path == pin or path.rsplit("/", 1)[-1] == pin.rsplit("/", 1)[-1]

    keep = [v for v in variants if _matches(v["uri"])]
    if len(keep) != 1:
        return None
    lines = master_text.splitlines()
    drop = set()
    for v in variants:
        if v is keep[0]:
            continue
        drop.add(v["inf_index"])
        j = v["inf_index"] + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
            j += 1
        drop.add(j)
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


class HlsSession:
    """Represents a parsed HLS VOD playlist session."""

    def __init__(self, session_id: str, url: str, headers: dict = None, variant_pin: str = None):
        self.session_id = session_id
        url, url_pin = split_variant_pin(url)
        self.variant_pin = variant_pin or url_pin
        self.variant_pinned = False
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
        self.variants_corrected = False
        # Masters whose segments carry fake image headers get every variant and
        # rendition routed through child sessions: index -> upstream playlist URL.
        self.routed_media = {}
        self.default_audio_language = ""
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
                # MPV picks the variant with the highest BANDWIDTH. Some providers
                # mislabel a low-resolution decoy as the top variant, so measure
                # the real heights and re-rank before MPV sees the master.
                pinned = _keep_only_variant(text, self.variant_pin) if self.variant_pin else None
                if pinned:
                    text = pinned
                    self.variant_pinned = True
                else:
                    corrected = self._correct_mislabeled_variants(text, current_url, req_headers)
                    if corrected != text:
                        text = corrected
                        self.variants_corrected = True
                default_audio = re.search(
                    r'#EXT-X-MEDIA:(?=[^\n]*TYPE=AUDIO)(?=[^\n]*DEFAULT=YES)[^\n]*LANGUAGE="([^"]+)"', text
                )
                self.default_audio_language = default_audio.group(1) if default_audio else ""
                disguised = self._master_segments_disguised(text, current_url, req_headers)
                if self.extracted_subtitles or self.variants_corrected or self.variant_pinned or disguised:
                    self.master_sanitized_text = self._strip_subtitles_from_master(text, current_url)
                    if disguised:
                        self.master_sanitized_text = self._route_master_media(self.master_sanitized_text)
                    self.is_master_proxy = True
                    self.is_fmp4 = False
                    self.is_ready = True
                    logger.info(
                        f"[HLSProxy] Serving sanitized master (subtitles={len(self.extracted_subtitles)}, "
                        f"variants_corrected={self.variants_corrected}, pinned={self.variant_pin if self.variant_pinned else None}, "
                        f"routed_media={len(self.routed_media)}): {current_url}"
                    )
                    return self
                else:
                    logger.info(f"[HLSProxy] Master playlist has separate audio tracks (#EXT-X-MEDIA:TYPE=AUDIO), passing through for native MPV playback: {current_url}")
                    self.is_fmp4 = False
                    self.is_ready = True
                    return self

            variant_url = ""
            if self.variant_pin:
                pinned = _keep_only_variant(text, self.variant_pin)
                if pinned:
                    variant_url = self._extract_best_variant_url(pinned, current_url)
                    self.variant_pinned = bool(variant_url)
            if not variant_url:
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

    def _master_segments_disguised(self, master_text: str, base_url: str, req_headers: dict) -> bool:
        """True when the first variant's segments hide MPEG-TS behind a fake image header."""
        variants = _parse_master_variants(master_text)
        if not variants:
            return False
        try:
            variant_url = _resolve_relative_url(base_url, variants[0]["uri"])
            req = urllib.request.Request(variant_url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=_VARIANT_PROBE_TIMEOUT) as resp:
                playlist = _read_playlist_text(resp)
            if "#EXT-X-MAP" in playlist:
                return False
            first_seg = next((l.strip() for l in playlist.splitlines() if l.strip() and not l.startswith("#")), "")
            if not first_seg:
                return False
            probe_headers = dict(req_headers)
            probe_headers["Range"] = "bytes=0-1023"
            req = urllib.request.Request(_resolve_relative_url(variant_url, first_seg), headers=probe_headers)
            with urllib.request.urlopen(req, timeout=_VARIANT_PROBE_TIMEOUT) as resp:
                sample = resp.read(1024)
            return _find_mpegts_sync_offset(sample) > 0
        except Exception as e:
            logger.debug(f"[HLSProxy] Disguised-segment probe of master skipped ({e})")
            return False

    def _route_master_media(self, master_text: str) -> str:
        """
        Point every variant and rendition of an (absolute-URI) master at local
        /media/<n>.m3u8 endpoints. Each is served by a child session whose
        segments go through the header-stripping segment proxy, so MPV can play
        multi-audio masters whose segments are disguised as images.
        """
        port = start_proxy_port()
        out = []
        expect_variant_uri = False

        def route(url):
            index = len(self.routed_media)
            self.routed_media[index] = url
            return f"http://127.0.0.1:{port}/hls/{self.session_id}/media/{index}.m3u8"

        for line in master_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#EXT-X-MEDIA:"):
                m = re.search(r'URI="([^"]+)"', stripped)
                if m and m.group(1).startswith(("http://", "https://")):
                    line = stripped.replace(f'URI="{m.group(1)}"', f'URI="{route(m.group(1))}"')
            elif stripped.startswith("#EXT-X-STREAM-INF:"):
                expect_variant_uri = True
            elif expect_variant_uri and stripped and not stripped.startswith("#"):
                if stripped.startswith(("http://", "https://")):
                    line = route(stripped)
                expect_variant_uri = False
            out.append(line)
        return "\n".join(out)

    def media_child(self, index: int):
        """Return (creating on first use) the child session for a routed media playlist."""
        child_id = f"{self.session_id}-m{index}"
        child = _sessions.get(child_id)
        if child is None and index in self.routed_media:
            child = HlsSession(child_id, self.routed_media[index], headers=self.headers).fetch_and_parse(timeout=10.0)
            _sessions[child_id] = child
        return child

    def _probe_variant_height(self, variant_url: str, req_headers: dict) -> int:
        """Return the real video height of a variant's first MPEG-TS segment, or 0 if unknown."""
        req = urllib.request.Request(variant_url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=_VARIANT_PROBE_TIMEOUT) as resp:
            playlist = _read_playlist_text(resp)
        if "#EXT-X-MAP" in playlist or re.search(r"#EXT-X-KEY:(?![^\n]*METHOD=NONE)", playlist):
            return 0  # fMP4 or encrypted segments cannot be read as plain TS
        first_seg = next(
            (l.strip() for l in playlist.splitlines() if l.strip() and not l.startswith("#")), ""
        )
        if not first_seg:
            return 0
        seg_url = _resolve_relative_url(variant_url, first_seg)
        seg_headers = dict(req_headers)
        seg_headers["Range"] = f"bytes=0-{_VARIANT_PROBE_BYTES - 1}"
        req = urllib.request.Request(seg_url, headers=seg_headers)
        with urllib.request.urlopen(req, timeout=_VARIANT_PROBE_TIMEOUT) as resp:
            data = resp.read(_VARIANT_PROBE_BYTES)
        return _mpegts_video_height(data)

    def _correct_mislabeled_variants(self, master_text: str, base_url: str, req_headers: dict) -> str:
        variants = _parse_master_variants(master_text)
        if not 2 <= len(variants) <= _VARIANT_PROBE_MAX:
            return master_text
        urls = [_resolve_relative_url(base_url, v["uri"]) for v in variants]
        heights = [0] * len(variants)
        pool = ThreadPoolExecutor(max_workers=len(variants), thread_name_prefix="HlsVariantProbe")
        try:
            futures = [pool.submit(self._probe_variant_height, u, req_headers) for u in urls]
            deadline = time.monotonic() + _VARIANT_PROBE_TIMEOUT * 2
            for k, fut in enumerate(futures):
                try:
                    heights[k] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
                except Exception as e:
                    logger.debug(f"[HLSProxy] Variant probe failed for {urls[k][:100]}: {e}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not all(heights):
            return master_text
        corrected = _rewrite_variant_attributes(master_text, variants, heights)
        if corrected != master_text:
            summary = ", ".join(
                f"{v['uri'].rsplit('/', 1)[-1][:40]} {v['height'] or '?'}p->{h}p" for v, h in zip(variants, heights)
            )
            logger.info(f"[HLSProxy] Corrected mislabeled HLS variants: {summary}")
        return corrected

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

                if len(parts) >= 4 and parts[2] == "media" and session.routed_media:
                    try:
                        child = session.media_child(int(parts[3].split(".", 1)[0]))
                    except Exception as e:
                        logger.warning(f"[HLSProxy] Routed media playlist failed: {e}")
                        child = None
                    if child is None or not child.segments:
                        self.send_response(502 if child is None else 404)
                        self.end_headers()
                        return
                    body, _actual = child.slice_manifest(0.0, local_port=_proxy_port)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
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
    # Child sessions of a routed master (see HlsSession.media_child)
    for child_id in [k for k in list(_sessions) if k.startswith(f"{session_id}-m")]:
        _sessions.pop(child_id, None)


def is_segment_proxy_session(session_id: str | None) -> bool:
    """Return True if the active HLS proxy session rewrites/strips segments."""
    if not session_id:
        return False
    session = _sessions.get(session_id)
    return bool(session and session.is_segment_proxy)


def session_has_corrected_variants(session_id: str | None) -> bool:
    """Return True if the session's master differs in its variants from the original
    (mislabeled variants re-ranked, or a single quality pinned)."""
    session = _sessions.get(session_id) if session_id else None
    return bool(session and (session.variants_corrected or session.variant_pinned or session.routed_media))


def session_default_audio_language(session_id: str | None) -> str:
    """
    Language of the master's DEFAULT audio rendition for routed masters. Their
    video segments often carry an extra, untagged audio track that MPV would
    otherwise prefer over the declared renditions.
    """
    session = _sessions.get(session_id) if session_id else None
    if session and session.routed_media:
        return session.default_audio_language
    return ""


def prepare_hls_stream_async(url: str, headers: dict = None, seek_target: float = 0.0, callback = None, variant_pin: str = None):
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
            session = HlsSession(session_id, url, headers=headers, variant_pin=variant_pin).fetch_and_parse(timeout=10.0)
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
