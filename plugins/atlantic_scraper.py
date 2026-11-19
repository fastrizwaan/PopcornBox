"""PopcornBox custom scraper for Atlantic (pal.hls.lol).

Provides direct HLS streams for movies and series from Atlantic aggregations.
Drop this file into ~/.local/share/popcorn-box/custom_plugins/ or
~/.var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box/custom_plugins/.
"""

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class Scraper:
    id = "custom.atlantic"
    name = "Atlantic"
    version = "1.0.0"
    description = "Direct HLS movie and TV streams from Atlantic (pal.hls.lol / embedsun)."
    author = "PopcornBox Community"
    media_types = ["movie", "series"]

    catalog_sections = [
        {"id": "popular_movies", "name": "Popular Movies", "type": "movie"},
        {"id": "new_movies", "name": "New Movies", "type": "movie"},
        {"id": "popular_series", "name": "Popular Series", "type": "series"},
        {"id": "new_series", "name": "New Series", "type": "series"},
    ]

    def get_catalog_items(
        self,
        catalog_id="popular_movies",
        media_type="movie",
        genre=None,
        page=1,
        query=None,
        limit=50,
    ):
        try:
            try:
                from popcornbox import tmdb_helper
            except ImportError:
                from src import tmdb_helper

            cid = str(catalog_id or "").lower()
            mtype = (
                "series"
                if ("series" in cid or media_type in ("series", "show", "tv"))
                else "movie"
            )
            tmdb_cat = "new" if "new" in cid else "popular"
            items, _ = tmdb_helper.fetch_default_tmdb_catalog_page(
                tmdb_cat, media_type=mtype, page=page, limit=limit
            )
            return items or []
        except Exception:
            return []

    _BASE_API = "https://pal.hls.lol/fs/embed"
    _REQUEST_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Origin": "https://atlantic.bz",
        "Referer": "https://atlantic.bz/",
    }
    _STREAM_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Referer": "https://embedsun.cc/",
    }
    _TOTAL_TIMEOUT = 12.0

    @staticmethod
    def _safe_http_url(url):
        try:
            parsed = urllib.parse.urlsplit(str(url))
            host = (parsed.hostname or "").lower()
            if (
                parsed.scheme not in ("http", "https")
                or not host
                or parsed.username
                or parsed.password
            ):
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
    def _fetch_page(cls, url, deadline, headers=None, timeout_cap=3.5):
        if not cls._safe_http_url(url):
            return ""
        remaining = cls._remaining(deadline, timeout_cap)
        if not remaining:
            return ""
        req_headers = dict(headers or cls._REQUEST_HEADERS)
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                if not 200 <= resp.status < 300:
                    return ""
                body = resp.read(2 << 20)
                return body.decode("utf-8", errors="replace")
        except Exception:
            return ""

    @classmethod
    def _detect_quality(cls, master_url, deadline):
        """Probe the master.m3u8 playlist to determine video resolution quality."""
        remaining = cls._remaining(deadline, 2.0)
        if not remaining:
            return "Auto"
        req = urllib.request.Request(master_url, headers=cls._STREAM_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                if not 200 <= resp.status < 300:
                    return "Auto"
                content = resp.read(64 << 10).decode("utf-8", errors="replace")
            widths = [int(w) for w in re.findall(r"RESOLUTION=(\d+)x\d+", content)]
            heights = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", content)]
            max_w = max(widths) if widths else 0
            max_h = max(heights) if heights else 0
            if max_w >= 3800 or max_h >= 2100:
                return "4K"
            if max_w >= 1900 or max_h >= 1000:
                return "1080p"
            if max_w >= 1200 or max_h >= 700:
                return "720p"
            if max_w >= 700 or max_h >= 400:
                return "480p"
            return "Auto"
        except Exception:
            return "Auto"

    def get_streams(
        self,
        imdb_id=None,
        media_type="movie",
        season=None,
        episode=None,
        title=None,
        year=None,
        tmdb_id=None,
    ):
        """Fetch working HLS streams for movies or TV episodes from Atlantic."""
        try:
            deadline = time.monotonic() + self._TOTAL_TIMEOUT
            media_type = str(media_type or "movie").strip().lower()
            is_tv = media_type in ("series", "show", "shows", "tv", "anime")
            upstream_type = "tv" if is_tv else "movie"

            season_num = int(season or 1) if is_tv else None
            episode_num = int(episode or 1) if is_tv else None

            # Resolve title & year if not provided
            if not title or not year:
                try:
                    try:
                        from popcornbox import tmdb_helper
                    except ImportError:
                        from src import tmdb_helper

                    key = tmdb_helper.get_tmdb_api_key()
                    if key:
                        if imdb_id and str(imdb_id).startswith("tt"):
                            find_url = (
                                f"https://api.themoviedb.org/3/find/{imdb_id}?"
                                f"api_key={key}&external_source=imdb_id"
                            )
                            find_html = self._fetch_page(find_url, deadline, timeout_cap=2.5)
                            if find_html:
                                data = json.loads(find_html)
                                res_list = data.get("tv_results" if is_tv else "movie_results") or []
                                if not res_list:
                                    res_list = data.get("movie_results" if is_tv else "tv_results") or []
                                if res_list:
                                    res = res_list[0]
                                    title = title or res.get("title") or res.get("name")
                                    date_str = res.get("release_date") or res.get("first_air_date") or ""
                                    year = year or (date_str[:4] if len(date_str) >= 4 else None)
                        elif tmdb_id and str(tmdb_id).isdigit():
                            kind = "tv" if is_tv else "movie"
                            detail_url = f"https://api.themoviedb.org/3/{kind}/{tmdb_id}?api_key={key}"
                            detail_html = self._fetch_page(detail_url, deadline, timeout_cap=2.5)
                            if detail_html:
                                res = json.loads(detail_html)
                                title = title or res.get("title") or res.get("name")
                                date_str = res.get("release_date") or res.get("first_air_date") or ""
                                year = year or (date_str[:4] if len(date_str) >= 4 else None)
                except Exception:
                    pass

            if not title:
                return []

            # Prepare API query
            params = {
                "title": str(title).strip(),
                "type": upstream_type,
            }
            if year:
                params["year"] = str(year).strip()
            if is_tv:
                params["seasonId"] = season_num
                params["episodeId"] = episode_num

            api_url = f"{self._BASE_API}?{urllib.parse.urlencode(params)}"
            resp_text = self._fetch_page(
                api_url, deadline, headers=self._REQUEST_HEADERS, timeout_cap=3.5
            )
            if not resp_text:
                return []

            try:
                data = json.loads(resp_text)
            except Exception:
                return []

            if not data.get("found"):
                return []

            embeds = data.get("embeds")
            if not isinstance(embeds, list) or not embeds:
                return []

            streams = []
            seen_urls = set()

            for embed in embeds:
                if not self._remaining(deadline, 0.5):
                    break
                if not isinstance(embed, dict):
                    continue
                embed_name = str(embed.get("name") or "FileSuN").strip()
                embed_url = str(embed.get("url") or "").strip()
                if not self._safe_http_url(embed_url):
                    continue

                # Fetch embed page HTML
                embed_html = self._fetch_page(
                    embed_url,
                    deadline,
                    headers={
                        "User-Agent": self._REQUEST_HEADERS["User-Agent"],
                        "Referer": "https://atlantic.bz/",
                    },
                    timeout_cap=3.0,
                )
                if not embed_html:
                    continue

                # Extract master.m3u8 playlist URL
                m3u8_url = None
                m = re.search(r'sources:\s*\[\{\s*file:\s*[\'"]([^\'"]+)[\'"]', embed_html)
                if m and ".m3u8" in m.group(1):
                    m3u8_url = m.group(1)
                else:
                    m2 = re.search(
                        r'https?://[^\s\'"<>\\\\]+master\.m3u8[^\s\'"<>\\\\]*', embed_html
                    )
                    if m2:
                        m3u8_url = m2.group(0)

                if not m3u8_url or not self._safe_http_url(m3u8_url) or m3u8_url in seen_urls:
                    continue

                seen_urls.add(m3u8_url)

                # Extract subtitles if present
                subtitles = []
                for sub_match in re.finditer(
                    r'[\'"](https?://[^\s\'"<>\\\\]+\.(?:vtt|srt)[^\s\'"<>\\\\]*)[\'"]',
                    embed_html,
                ):
                    sub_url = sub_match.group(1)
                    if self._safe_http_url(sub_url):
                        lang_m = re.search(r"_([A-Za-z]+)\.(?:vtt|srt)", sub_url)
                        lang = lang_m.group(1) if lang_m else "Sub"
                        subtitles.append(
                            {"id": lang.lower()[:3], "url": sub_url, "lang": lang}
                        )

                # Determine resolution quality
                quality = self._detect_quality(m3u8_url, deadline)

                streams.append({
                    "name": f"Atlantic {quality}",
                    "title": f"{quality} | Atlantic ({embed_name})",
                    "url": m3u8_url,
                    "quality": quality,
                    "behaviorHints": {
                        "proxyHeaders": {
                            "request": dict(self._STREAM_HEADERS)
                        }
                    },
                    "subtitles": subtitles,
                })

                if len(streams) >= 3:
                    break

            return streams
        except Exception:
            return []
