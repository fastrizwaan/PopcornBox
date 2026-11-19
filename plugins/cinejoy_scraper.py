"""PopcornBox custom scraper for the Cinejoy provider implemented in luffy.

This provider's upstream protocol uses a WASM request encoder and AES-GCM
responses. Install the optional Python packages with:
    python3 -m pip install wasmtime cryptography
Then import this file through PopcornBox's Custom Plugins screen, or copy it to
~/.local/share/popcorn-box/custom_plugins/.
"""

import concurrent.futures
import hashlib
import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import wasmtime
except ImportError:
    wasmtime = None

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class Scraper:
    id = "custom.luffy_cinejoy"
    name = "Cinejoy (Luffy)"
    version = "1.0.0"
    description = "Movie and series streams from luffy's Cinejoy provider (requires wasmtime and cryptography)."
    author = "PopcornBox Community"
    media_types = ["movie", "series"]

    catalog_sections = [
        {"id": "popular_movies", "name": "Popular Movies", "type": "movie"},
        {"id": "new_movies", "name": "New Movies", "type": "movie"},
        {"id": "popular_series", "name": "Popular Series", "type": "series"},
        {"id": "new_series", "name": "New Series", "type": "series"},
    ]

    def get_catalog_items(self, catalog_id="popular_movies", media_type="movie", genre=None, page=1, query=None, limit=50):
        try:
            try:
                from popcornbox import tmdb_helper
            except ImportError:
                from src import tmdb_helper
            cid = str(catalog_id or "").lower()
            mtype = "series" if ("series" in cid or media_type in ("series", "show", "tv")) else "movie"
            tmdb_cat = "new" if "new" in cid else "popular"
            items, _ = tmdb_helper.fetch_default_tmdb_catalog_page(tmdb_cat, media_type=mtype, page=page, limit=limit)
            return items or []
        except Exception:
            return []

    _SITE_URL = "https://cinejoy.pk"
    _API_URL = "https://api.wing.st"
    _WASM_SHA256 = "40c923580779e2a850fc5ab3f0046be565ed717883cf4547ed3a87de2450bdcf"
    _VERIFIED_SERVERS = {"light", "nebula", "sirius", "solara", "lisbon"}
    _HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://cinejoy.pk",
        "Referer": "https://cinejoy.pk/",
    }
    _TOTAL_TIMEOUT = 13.0

    def __init__(self):
        self._wasm_bytes = None

    @classmethod
    def _timeout(cls, deadline, cap=3.0):
        remaining = deadline - time.monotonic()
        return min(cap, remaining) if remaining > 0 else 0

    @staticmethod
    def _safe_https_url(raw_url):
        try:
            parsed = urllib.parse.urlsplit(str(raw_url))
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not host or parsed.username or parsed.password:
                return False
            if host == "localhost" or host.endswith((".local", ".internal")):
                return False
            try:
                ipaddress.ip_address(host)
                return False
            except ValueError:
                return "." in host
        except (TypeError, ValueError):
            return False

    @classmethod
    def _fetch(cls, url, deadline, method="GET", body=None, content_type=None,
               limit=4 << 20, timeout_cap=3.0):
        if not cls._safe_https_url(url):
            return None, None, None, None
        timeout = cls._timeout(deadline, timeout_cap)
        if not timeout:
            return None, None, None, None
        headers = dict(cls._HEADERS)
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return None, None, None, None
                data = response.read(limit)
                return data, response.geturl(), response.headers.get("Content-Type", ""), response.status
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None, None, None, None

    @classmethod
    def _json(cls, url, deadline, method="GET", body=None, content_type=None, cap=3.0):
        raw, _, _, _ = cls._fetch(
            url, deadline, method=method, body=body, content_type=content_type,
            limit=2 << 20, timeout_cap=cap,
        )
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _get_wasm(self, deadline):
        if self._wasm_bytes is not None:
            return self._wasm_bytes
        body, _, _, _ = self._fetch(self._API_URL + "/crush.wasm", deadline, limit=4 << 20)
        if not body or hashlib.sha256(body).hexdigest() != self._WASM_SHA256:
            return None
        self._wasm_bytes = body
        return body

    @staticmethod
    def _seal_request(wasm_bytes, path, payload):
        if wasmtime is None:
            return None
        try:
            body = json.dumps({"path": path, "payload": payload}, separators=(",", ":")).encode("utf-8")
            engine = wasmtime.Engine()
            module = wasmtime.Module(engine, wasm_bytes)
            if module.imports:
                return None
            store = wasmtime.Store(engine)
            instance = wasmtime.Instance(store, module, [])
            exports = instance.exports(store)
            memory = exports["memory"]
            alloc = exports["alloc"]
            seal = exports["seal_request"]

            seed = os.urandom(44)
            capacity = len(body) + 512
            body_ptr = int(alloc(store, len(body)))
            seed_ptr = int(alloc(store, len(seed)))
            output_ptr = int(alloc(store, capacity))
            if not body_ptr or not seed_ptr or not output_ptr:
                return None
            memory.write(store, body, start=body_ptr)
            memory.write(store, seed, start=seed_ptr)
            length = int(seal(store, body_ptr, len(body), seed_ptr, len(seed), output_ptr, capacity))
            if length <= 98 or length > capacity:
                return None
            return bytes(memory.read(store, start=output_ptr, stop=output_ptr + length))
        except Exception:
            return None

    @staticmethod
    def _aad(key_id, ephemeral_public):
        return b"lumen-gate-v2" + bytes((0, 2, key_id)) + ephemeral_public

    @classmethod
    def _decrypt(cls, sealed, wire):
        if AESGCM is None or len(sealed) <= 98 or len(wire) < 28:
            return None
        response_key = sealed[:32]
        key_id = sealed[32]
        ephemeral_public = sealed[33:98]
        try:
            plaintext = AESGCM(response_key).decrypt(
                wire[:12], wire[12:], cls._aad(key_id, ephemeral_public)
            )
            envelope = json.loads(plaintext.decode("utf-8"))
            return envelope if isinstance(envelope, dict) else None
        except Exception:
            return None

    @classmethod
    def _extract_candidates(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("stream"), list):
            return []
        candidates = []
        for stream in data["stream"]:
            if not isinstance(stream, dict):
                continue
            playlist = str(stream.get("playlist") or "")
            if playlist.startswith("https://"):
                candidates.append((playlist, "Auto"))
                continue
            qualities = stream.get("qualities")
            if stream.get("type") == "file" and isinstance(qualities, dict):
                def quality_key(item):
                    key = str(item[0])
                    return int(key) if key.isdigit() else -1
                for quality, item in sorted(qualities.items(), key=quality_key, reverse=True):
                    if isinstance(item, dict) and item.get("url"):
                        candidates.append((str(item["url"]), str(quality) + "p"))
        return candidates

    @classmethod
    def _fetch_playlist(cls, url, deadline):
        body, final_url, _, _ = cls._fetch(url, deadline, limit=4 << 20, timeout_cap=2.0)
        if body is None:
            return "", ""
        text = body.decode("utf-8", errors="replace")
        if not text.lstrip().startswith("#EXTM3U"):
            return "", ""
        return final_url or url, text

    @staticmethod
    def _first_uri(playlist):
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        return ""

    @classmethod
    def _is_playable(cls, raw_url, deadline):
        current = raw_url
        for _ in range(3):
            current, playlist = cls._fetch_playlist(current, deadline)
            if not current:
                return False
            media = cls._first_uri(playlist)
            if not media:
                return False
            if "#EXT-X-STREAM-INF:" in playlist:
                current = urllib.parse.urljoin(current, media)
                continue
            segment_url = urllib.parse.urljoin(current, media)
            segment, _, content_type, _ = cls._fetch(
                segment_url, deadline, limit=256 << 10, timeout_cap=2.0
            )
            if segment is None or len(segment) < 188:
                return False
            sample = segment[:100].decode("utf-8", errors="ignore").lower().lstrip()
            if sample.startswith(("<!doctype", "<html", "{")):
                return False
            if content_type and "text/html" in content_type.lower():
                return False
            # Check for actual image format signatures (JPEG: FF D8 FF, PNG: 89 50 4E 47, GIF: GIF8)
            # rather than trusting content_type header, since CDNs often mask media segments with image/ headers.
            if segment.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")):
                return False
            return True
        return False

    @classmethod
    def _try_server(cls, wasm, server_name, media_kind, params, deadline):
        sealed = cls._seal_request(wasm, "/" + server_name + "/" + media_kind, params)
        if sealed is None:
            return []
        response_body, _, _, _ = cls._fetch(
            cls._API_URL + "/g", deadline, method="POST", body=sealed[98:],
            content_type="text/plain;charset=UTF-8", limit=4 << 20, timeout_cap=3.0,
        )
        if response_body is None:
            return []
        envelope = cls._decrypt(sealed, response_body)
        if not isinstance(envelope, dict) or not 200 <= int(envelope.get("status") or 0) < 300:
            return []
        data = envelope.get("data")
        candidates = cls._extract_candidates(data)
        if not candidates and isinstance(data, dict):
            # Some hosts return an embed identifier that must be sent back in a
            # second sealed request to reveal the final playlist.
            streams = data.get("stream")
            if isinstance(streams, list):
                for stream in streams[:2]:
                    if not isinstance(stream, dict) or not stream.get("id"):
                        continue
                    embed = stream["id"]
                    if not isinstance(embed, (str, int, float)):
                        continue
                    nested_params = dict(params)
                    nested_params["embed"] = embed
                    nested = cls._seal_request(
                        wasm, "/" + server_name + "/" + media_kind, nested_params
                    )
                    if nested is None:
                        continue
                    nested_body, _, _, _ = cls._fetch(
                        cls._API_URL + "/g", deadline, method="POST", body=nested[98:],
                        content_type="text/plain;charset=UTF-8", limit=4 << 20, timeout_cap=2.0,
                    )
                    if nested_body is None:
                        continue
                    nested_envelope = cls._decrypt(nested, nested_body)
                    if isinstance(nested_envelope, dict):
                        candidates.extend(cls._extract_candidates(nested_envelope.get("data")))
                    if candidates:
                        break
        for url, quality in candidates:
            if cls._safe_https_url(url) and cls._is_playable(url, deadline):
                return [(url, quality)]
        return []

    def get_streams(self, imdb_id, media_type="movie", season=None, episode=None,
                    title=None, year=None, tmdb_id=None):
        """Resolve the first validated Cinejoy HLS stream in plugin format."""
        if wasmtime is None or AESGCM is None:
            return []
        try:
            tmdb = str(tmdb_id or "").strip()
            if not tmdb.isdigit() or int(tmdb) <= 0:
                if imdb_id:
                    try:
                        try:
                            from popcornbox import tmdb_helper
                        except ImportError:
                            from src import tmdb_helper
                        resolved = tmdb_helper.resolve_to_tmdb_id(imdb_id, media_type, title, year=year)
                        if resolved and str(resolved).isdigit():
                            tmdb = str(resolved).strip()
                    except Exception:
                        pass
            if not tmdb.isdigit() or int(tmdb) <= 0:
                return []
            media_type = str(media_type or "movie").strip().lower()
            if media_type in ("series", "show", "shows", "tv", "anime"):
                kind = "series"
                season_number, episode_number = int(season), int(episode)
                if season_number < 1 or episode_number < 1:
                    return []
            elif media_type == "movie":
                kind = "movie"
                season_number = episode_number = None
            else:
                return []
        except (TypeError, ValueError, OverflowError):
            return []

        deadline = time.monotonic() + self._TOTAL_TIMEOUT
        wasm = self._get_wasm(deadline)
        if not wasm:
            return []
        catalog = self._json(self._API_URL + "/servers", deadline, cap=2.5).get("servers")
        if not isinstance(catalog, list):
            return []
        servers = [
            str(item.get("name")) for item in catalog
            if isinstance(item, dict)
            and str(item.get("status") or "").lower() == "ok"
            and str(item.get("name") or "").lower() in self._VERIFIED_SERVERS
        ]
        if not servers:
            return []

        params = {"tmdb": tmdb}
        if kind == "series":
            params.update({"season": str(season_number), "episode": str(episode_number)})
        if title:
            params["title"] = str(title)
        if year:
            params["year"] = str(year)

        # The Go provider races the verified servers; mirror that so a slow
        # source does not prevent a faster verified source from being returned.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(servers)))
        futures = [
            pool.submit(self._try_server, wasm, server, kind, params, deadline)
            for server in servers
        ]
        result = []
        try:
            wait_for = max(0.1, deadline - time.monotonic())
            for future in concurrent.futures.as_completed(futures, timeout=wait_for):
                try:
                    result = future.result() or []
                except Exception:
                    result = []
                if result:
                    break
        except concurrent.futures.TimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

        if not result:
            return []
        url, quality = result[0]
        return [{
            "name": "Cinejoy " + quality,
            "title": quality + " | Cinejoy",
            "url": url,
            "quality": quality,
            "behaviorHints": {"proxyHeaders": {"request": dict(self._HEADERS)}},
        }]
