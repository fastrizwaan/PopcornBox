"""PopcornBox custom scraper for the Streamo provider implemented in luffy.

Install through PopcornBox's Custom Plugins screen, or copy this file into
~/.local/share/popcorn-box/custom_plugins/.
"""

import ipaddress
import json
import re
import time
import urllib.parse
import urllib.request


class Scraper:
    id = "custom.luffy_streamo"
    name = "Streamo (Luffy)"
    version = "1.0.0"
    description = "Movie and series streams from luffy's Streamo provider."
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

    _BASE_URL = "https://streamo.st"
    _REQUEST_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://streamo.st",
        "Referer": "https://streamo.st/",
    }
    _TOTAL_TIMEOUT = 12.5

    @staticmethod
    def _safe_http_url(url):
        try:
            parsed = urllib.parse.urlsplit(str(url))
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
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
    def _remaining(cls, deadline, cap):
        remaining = deadline - time.monotonic()
        return min(cap, remaining) if remaining > 0 else 0

    @classmethod
    def _json_request(cls, url, deadline, data=None, timeout=3.0):
        remaining = cls._remaining(deadline, timeout)
        if not remaining:
            return {}
        headers = dict(cls._REQUEST_HEADERS)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if not 200 <= response.status < 300:
                    return {}
                body = response.read(2 << 20)
            value = json.loads(body.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @classmethod
    def _fetch_playlist(cls, url, deadline):
        if not cls._safe_http_url(url):
            return "", ""
        remaining = cls._remaining(deadline, 2.0)
        if not remaining:
            return "", ""
        request = urllib.request.Request(url, headers=cls._REQUEST_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if not 200 <= response.status < 300:
                    return "", ""
                body = response.read(4 << 20)
                final_url = response.geturl() or url
            playlist = body.decode("utf-8", errors="replace")
            if not playlist.lstrip().startswith("#EXTM3U"):
                return "", ""
            return final_url, playlist
        except Exception:
            return "", ""

    @staticmethod
    def _first_uri(playlist):
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        return ""

    @staticmethod
    def _tag_uri(playlist, tag):
        for line in playlist.splitlines():
            line = line.strip()
            if not line.startswith(tag) or "METHOD=NONE" in line:
                continue
            match = re.search(r'URI="([^"]+)"', line)
            return match.group(1) if match else ""
        return ""

    @classmethod
    def _probe_media(cls, url, deadline):
        if not cls._safe_http_url(url):
            return False
        remaining = cls._remaining(deadline, 1.25)
        if not remaining:
            return False
        headers = dict(cls._REQUEST_HEADERS)
        headers["Range"] = "bytes=0-65535"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if not 200 <= response.status < 300:
                    return False
                body = response.read(64 << 10)
                content_type = response.headers.get("Content-Type", "").lower()
            sample = body.lstrip()[:256].lower()
            if sample.startswith(b"#extm3u"):
                return True
            return not (sample.startswith(b"<") or "text/html" in content_type or content_type.startswith("image/"))
        except Exception:
            return False

    @classmethod
    def _is_playable_hls(cls, url, deadline):
        playlist_url, playlist = cls._fetch_playlist(url, deadline)
        if not playlist_url:
            return False
        for _ in range(2):
            if "#EXT-X-STREAM-INF" not in playlist:
                break
            variant = cls._first_uri(playlist)
            if not variant:
                return False
            playlist_url, playlist = cls._fetch_playlist(urllib.parse.urljoin(playlist_url, variant), deadline)
            if not playlist_url:
                return False
        if "#EXT-X-STREAM-INF" in playlist:
            return False

        segment = cls._first_uri(playlist)
        if not segment:
            return False
        probes = []
        key_uri = cls._tag_uri(playlist, "#EXT-X-KEY:")
        map_uri = cls._tag_uri(playlist, "#EXT-X-MAP:")
        if key_uri:
            probes.append(urllib.parse.urljoin(playlist_url, key_uri))
        if map_uri:
            probes.append(urllib.parse.urljoin(playlist_url, map_uri))
        probes.append(urllib.parse.urljoin(playlist_url, segment))
        return all(cls._probe_media(uri, deadline) for uri in probes)

    @staticmethod
    def _candidate_refs(providers):
        seen = set()
        candidates = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            name = str(provider.get("name") or "Streamo").strip()
            refs = []
            if provider.get("url"):
                refs.append((str(provider["url"]), "Auto"))
            qualities = provider.get("allQualities")
            if isinstance(qualities, list):
                for item in qualities[:4]:
                    if isinstance(item, dict) and item.get("url"):
                        refs.append((str(item["url"]), str(item.get("quality") or "Auto")))
            for ref, quality in refs:
                if ref and ref not in seen:
                    seen.add(ref)
                    candidates.append((name, ref, quality))
                if len(candidates) >= 12:
                    return candidates
        return candidates

    def get_streams(self, imdb_id, media_type="movie", season=None, episode=None,
                    title=None, year=None, tmdb_id=None):
        """Return the first validated Streamo HLS URL in custom-plugin format."""
        try:
            tmdb = str(tmdb_id or "").strip()
            if not re.fullmatch(r"[0-9]+", tmdb) or int(tmdb) <= 0:
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
            if not re.fullmatch(r"[0-9]+", tmdb) or int(tmdb) <= 0:
                return []
            media_type = str(media_type or "movie").strip().lower()
            if media_type in ("series", "show", "shows", "tv", "anime"):
                upstream_type = "tv"
                season_number, episode_number = int(season), int(episode)
                if season_number < 1 or episode_number < 1:
                    return []
            elif media_type == "movie":
                upstream_type = "movie"
                season_number = episode_number = None
            else:
                return []
        except (TypeError, ValueError, OverflowError):
            return []

        deadline = time.monotonic() + self._TOTAL_TIMEOUT
        payload = {"tmdbId": int(tmdb), "type": upstream_type}
        if upstream_type == "tv":
            payload.update({"season": season_number, "episode": episode_number})
        response = self._json_request(
            self._BASE_URL + "/api/streaming/guest-token",
            deadline,
            data=json.dumps(payload).encode("utf-8"),
            timeout=3.5,
        )
        data = response.get("data")
        if not response.get("success") or not isinstance(data, dict):
            return []
        token = str(data.get("token") or "")
        providers = data.get("providers")
        if not token or not isinstance(providers, list):
            return []

        for provider_name, ref, quality in self._candidate_refs(providers):
            if not self._remaining(deadline, 0.1):
                break
            query = urllib.parse.urlencode({"ref": ref, "stoken": token})
            resolved = self._json_request(self._BASE_URL + "/api/streaming/resolve?" + query, deadline)
            stream_url = str(resolved.get("url") or "").strip()
            if stream_url.startswith("/"):
                stream_url = self._BASE_URL + stream_url
            if not self._safe_http_url(stream_url):
                continue
            if not self._is_playable_hls(stream_url, deadline):
                continue

            quality = quality or "Auto"
            return [{
                "name": f"Streamo {quality}",
                "title": f"{quality} | {provider_name}",
                "url": stream_url,
                "quality": quality,
                "behaviorHints": {"proxyHeaders": {"request": dict(self._REQUEST_HEADERS)}},
            }]
        return []
