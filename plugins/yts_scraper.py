"""
YTS & Popcorn Scraper and Catalog Plugin for PopcornBox.
Drop this file into PopcornBox's custom plugins directory:
~/.local/share/popcorn-box/custom_plugins/yts_scraper.py

Features:
- Stream Scraping: 720p, 1080p, 4K torrents from YTS mirrors and Popcorn API.
- Catalog Browsing: Popular, Trending, Top Rated, and Last Added (Movies & Series).
- Filter Support: Full genre filtering and keyword search via fusme.link.
"""

import json
import urllib.request
import urllib.parse
import re

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://public.popcorn-tracker.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.si:1337/announce",
    "http://tracker.openbittorrent.com:80/announce",
]

POPULAR_GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Music",
    "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"
]


class Scraper:
    id = "custom.yts"
    name = "YTS (Popcorn)"
    version = "1.2.0"
    description = "Catalogs (Trending, Popular, Top Rated) and torrent streams from YTS and Popcorn Time"
    author = "PopcornBox Community"
    media_types = ["movie", "series"]

    # Catalog sections exposed to PopcornBox Discover and See All grid
    catalog_sections = [
        {"id": "trending", "name": "Trending Movies", "type": "movie", "genres": POPULAR_GENRES},
        {"id": "popular", "name": "Popular Movies", "type": "movie", "genres": POPULAR_GENRES},
        {"id": "top_rated", "name": "Top Rated Movies", "type": "movie", "genres": POPULAR_GENRES},
        {"id": "last_added", "name": "Last Added Movies", "type": "movie", "genres": POPULAR_GENRES},
        {"id": "shows_trending", "name": "Trending Series", "type": "series", "genres": POPULAR_GENRES},
        {"id": "shows_popular", "name": "Popular Series", "type": "series", "genres": POPULAR_GENRES},
    ]

    _YTS_BASES = [
        "https://movies-api.accel.li/api/v2",
        "https://yts.mx/api/v2",
        "https://yts.bz/api/v2",
        "https://yts.gg/api/v2",
    ]

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    def _build_magnet(self, info_hash, title, trackers=None):
        encoded_dn = urllib.parse.quote(title or "Movie")
        trs = trackers or DEFAULT_TRACKERS
        tr_params = "&".join(f"tr={urllib.parse.quote(t)}" for t in trs)
        return f"magnet:?xt=urn:btih:{info_hash}&dn={encoded_dn}&{tr_params}"

    # --- Catalog & Discover Support ---

    def get_catalog_items(self, catalog_id="trending", media_type="movie", genre=None, page=1, query=None, limit=50):
        """Fetch catalog rows or search results from fusme.link."""
        is_series = (media_type in ("series", "show", "shows", "tv")) or str(catalog_id).startswith("shows_")
        endpoint = "shows" if is_series else "movies"
        norm_type = "series" if is_series else "movie"

        if query:
            url = f"https://fusme.link/{endpoint}/{page}?keywords={urllib.parse.quote(query)}&showAll=1&locale=en&contentLocale=en"
        else:
            sort_map = {
                "popular": ("seeds", -1),
                "shows_popular": ("seeds", -1),
                "trending": ("trending", -1),
                "shows_trending": ("trending", -1),
                "top_rated": ("rating", -1),
                "shows_top_rated": ("rating", -1),
                "last_added": ("updated", -1),
            }
            sort, order = sort_map.get(catalog_id, ("trending", -1))
            url = f"https://fusme.link/{endpoint}/{page}?sort={sort}&order={order}&showAll=1&locale=en&contentLocale=en"

            if genre and str(genre).strip().lower() not in ("all", "all genres", "genre", "genres"):
                clean_g = str(genre).strip().lower()
                if clean_g in ("sci-fi", "science-fiction"):
                    clean_g = "science fiction"
                url += f"&genre={urllib.parse.quote(clean_g)}"

        try:
            req = urllib.request.Request(url, headers=self._HEADERS)
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            items = data if isinstance(data, list) else data.get("results", [])
            movies = []
            for m in items:
                imdb_id = m.get("imdb_id") or m.get("imdb") or m.get("id")
                if not imdb_id:
                    continue

                images = m.get("images", {})
                locale_data = m.get("locale", {})
                poster = images.get("poster") or images.get("fanart") or ""
                if locale_data and locale_data.get("poster"):
                    poster = locale_data.get("poster")

                title = m.get("title")
                if locale_data and locale_data.get("title"):
                    title = locale_data.get("title")

                rating_data = m.get("rating", {})
                rating_str = ""
                if isinstance(rating_data, dict) and "percentage" in rating_data:
                    rating_str = f"{rating_data['percentage'] / 10.0:.1f}"

                movies.append({
                    "id": str(imdb_id),
                    "title": str(title or "Untitled"),
                    "year": str(m.get("year", "")),
                    "medium_cover_image": poster,
                    "type": norm_type,
                    "rating": rating_str,
                    "genres": m.get("genres", []),
                })

            return movies
        except Exception:
            return []

    # --- Stream Scraping Support ---

    def get_streams(self, imdb_id, media_type="movie", season=None, episode=None, title=None, year=None, tmdb_id=None):
        if not imdb_id:
            return []

        clean_imdb = str(imdb_id).strip()
        if not clean_imdb.startswith("tt"):
            return []

        # 1. For movies, query YTS REST APIs
        if media_type == "movie":
            for base in self._YTS_BASES:
                url = f"{base}/list_movies.json?query_term={clean_imdb}"
                try:
                    req = urllib.request.Request(url, headers=self._HEADERS)
                    with urllib.request.urlopen(req, timeout=6.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))

                    if not isinstance(data, dict) or data.get("status") != "ok":
                        continue

                    movies = data.get("data", {}).get("movies", [])
                    if not movies:
                        continue

                    movie = movies[0]
                    movie_title = movie.get("title") or title or "Movie"
                    movie_year = movie.get("year") or year or ""
                    torrents = movie.get("torrents", [])
                    streams = []

                    for t in torrents:
                        h = (t.get("hash") or "").strip().lower()
                        if not h:
                            continue

                        quality = t.get("quality", "HD")
                        q_type = t.get("type", "")
                        size = t.get("size", "")
                        seeds = int(t.get("seeds") or 0)

                        details = [quality]
                        if q_type:
                            details.append(q_type.upper())
                        if size:
                            details.append(size)
                        details_str = " | ".join(details)

                        dn = f"{movie_title} ({movie_year}) [{quality}] [YTS]"
                        magnet = self._build_magnet(h, dn)

                        streams.append({
                            "name": f"YTS {quality}",
                            "title": f"{details_str} | 👤 {seeds}",
                            "infoHash": h,
                            "magnet": magnet,
                            "quality": quality,
                            "size": size,
                            "seeders": seeds,
                            "addon_name": "YTS",
                        })

                    if streams:
                        return streams

                except Exception:
                    continue

        # 2. Popcorn Time API (fusme.link) fallback for movies and series
        try:
            actual_media = "show" if media_type in ("series", "show", "tv") else "movie"
            fusme_url = f"https://fusme.link/{actual_media}/{clean_imdb}?locale=en&contentLocale=en"
            req = urllib.request.Request(fusme_url, headers=self._HEADERS)
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            torrents_dict = data.get("torrents", {}).get("en", {})
            m_title = data.get("title") or title or "Item"
            streams = []

            for qual, item in torrents_dict.items():
                if not isinstance(item, dict):
                    continue
                magnet = item.get("url", "")
                h_match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40})", magnet, re.IGNORECASE)
                info_hash = h_match.group(1).lower() if h_match else ""
                if not info_hash and not magnet:
                    continue

                filesize = item.get("filesize") or ""
                seeders = int(item.get("seed") or 0)
                provider = item.get("provider") or "Popcorn"

                streams.append({
                    "name": f"{provider} {qual}",
                    "title": f"{qual} | 👤 {seeders} | {filesize}",
                    "infoHash": info_hash,
                    "magnet": magnet,
                    "quality": qual,
                    "size": filesize,
                    "seeders": seeders,
                    "addon_name": "YTS (Popcorn)",
                })

            if streams:
                return streams
        except Exception:
            pass

        return []
