"""PopcornBox custom scraper for YouTube Movies & Series.

Searches YouTube for full movies, classic films, documentaries, and TV series episodes.
Drop this file into ~/.local/share/popcorn-box/custom_plugins/ or
~/.var/app/io.github.fastrizwaan.PopcornBox/data/popcorn-box/custom_plugins/.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

logger = logging.getLogger("PopcornBox.Plugin.YouTube")

# Stop words to ignore during title token matching
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "is", "it", "its", "part", "vol", "volume"
}

# Curated high-quality classic and public domain movies readily available on YouTube
CLASSIC_MOVIES_CATALOG = [
    {"id": "tt0063350", "title": "Night of the Living Dead", "year": "1968", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMzlmY2VjYjUtMGZlOC00MjE3LTg1Y2UtMGJhMDM2MTNiN2ZiXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.8"},
    {"id": "tt0056923", "title": "Charade", "year": "1963", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMTY3OTkxNjYxMV5BMl5BanBnXkFtZTcwMTc0MDk2Mw@@._V1_SX300.jpg", "type": "movie", "rating": "7.9"},
    {"id": "tt0017925", "title": "The General", "year": "1926", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BNmU4YjY5YmYtMjc2OS00NmE4LTg5MmItNzgxZWI3ZGU4N2JmXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "8.1"},
    {"id": "tt0055830", "title": "Carnival of Souls", "year": "1962", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BYzA2NTllOWMtMWY5Yi00ZmY0LWJjOTAtYTZiYzQ3ZTU3MGQwXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.1"},
    {"id": "tt0032553", "title": "His Girl Friday", "year": "1940", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BY2FhZTZlYmQtZTc4My00ODljLWE3MWMtYzE0NjRhMmI1ZGRkXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.8"},
    {"id": "tt0017136", "title": "Metropolis", "year": "1927", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMTg5NzkwMTU2OV5BMl5BanBnXkFtZTcwMjg4NDg0MQ@@._V1_SX300.jpg", "type": "movie", "rating": "8.3"},
    {"id": "tt0053174", "title": "Plan 9 from Outer Space", "year": "1957", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BN2E4N2JjODEtZTc3Yi00NWVmLTg4MDQtNTdlMjNlMjc3NzI2XkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "3.9"},
    {"id": "tt0052905", "title": "House on Haunted Hill", "year": "1959", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMmMyNGI1MDYtNmZhMy00YjViLTgwZTYtMmY1NTYyZDg2MzE1XkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "6.8"},
    {"id": "tt0013442", "title": "Nosferatu", "year": "1922", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BYjRlZTQ5NjctMGM1Ny00OWVlLTg0MmQtYTg0M2QzZTNiOWJmXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.9"},
    {"id": "tt0058294", "title": "The Last Man on Earth", "year": "1964", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BZTIwYTMxZTctMGRiNS00MzMwLTllZWYtMGUzY2E1NDlmN2VlXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "6.8"},
    {"id": "tt0042409", "title": "D.O.A.", "year": "1949", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BZjY5M2ZkNWUtZDRlMi00Mjc4LTk0MjEtNTRjMDZlMGM5ZTUyXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.3"},
    {"id": "tt0037638", "title": "Detour", "year": "1945", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BN2U5MmM2YzQtZDhhMC00MzM4LThlYzctMTRjMzkzNjc3YWYxXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.3"},
    {"id": "tt0031397", "title": "Gulliver's Travels", "year": "1939", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMGM5Y2Q1ZjMtMzg3Ny00NmYzLWI4NDYtM2ZmMmExZGMzNTFhXkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "6.7"},
    {"id": "tt0027094", "title": "The 39 Steps", "year": "1935", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMjA4Nzg5NzA5Ml5BMl5BanBnXkFtZTgwNTQ2NDQ5MDE@._V1_SX300.jpg", "type": "movie", "rating": "7.6"},
    {"id": "tt0030341", "title": "The Lady Vanishes", "year": "1938", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BYzA2ZDY4MjMtOGZhNi00MmE3LWI2NmMtYTQyYTMzNDYyMmI3XkEyXkFqcGc@._V1_SX300.jpg", "type": "movie", "rating": "7.8"},
    {"id": "tt0054033", "title": "The Little Shop of Horrors", "year": "1960", "medium_cover_image": "https://m.media-amazon.com/images/M/MV5BMjA4ODc4ODAwMV5BMl5BanBnXkFtZTcwMjE5MTYyMQ@@._V1_SX300.jpg", "type": "movie", "rating": "6.3"},
]


class Scraper:
    id = "custom.youtube"
    name = "YouTube"
    version = "1.0.0"
    description = "Searches YouTube for full movies, classic films, documentaries, and TV series episodes."
    author = "PopcornBox Community"
    media_types = ["movie", "series"]

    catalog_sections = [
        {"id": "popular_movies", "name": "Popular Movies", "type": "movie"},
        {"id": "classic_movies", "name": "Classic & Public Domain Movies", "type": "movie"},
        {"id": "documentaries", "name": "Documentaries", "type": "movie"},
        {"id": "popular_series", "name": "Popular Series", "type": "series"},
        {"id": "new_movies", "name": "New Movies", "type": "movie"},
        {"id": "new_series", "name": "New Series", "type": "series"},
    ]

    NEGATIVE_MOVIE_KEYWORDS = [
        "trailer", "teaser", "review", "reaction", "recap", "explained",
        "ending explained", "kill count", "breakdown", "easter egg", "scene",
        "scenes", "clip", "clips", "soundtrack", "ost", "interview",
        "behind the scenes", "b-roll", "analysis", "cast then and now",
        "gameplay", "walkthrough", "theme song"
    ]

    NEGATIVE_SERIES_KEYWORDS = [
        "trailer", "teaser", "preview", "promo", "review", "reaction",
        "recap", "explained", "ending explained", "interview", "behind the scenes",
        "b-roll", "breakdown", "analysis", "theme song"
    ]

    @staticmethod
    def _parse_duration(duration_str):
        if not duration_str:
            return 0
        parts = str(duration_str).strip().split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 1:
                return int(parts[0])
        except (ValueError, TypeError):
            return 0
        return 0

    @classmethod
    def _search_innertube(cls, query, timeout=6.0):
        """Query YouTube InnerTube web search endpoint for real-time video results."""
        if not query or not query.strip():
            return []
        url = "https://www.youtube.com/youtubei/v1/search"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20240101.00.00",
                    "hl": "en",
                    "gl": "US",
                }
            },
            "query": query.strip(),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            },
        )
        results = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                contents = data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
                for s in contents:
                    for it in s.get("itemSectionRenderer", {}).get("contents", []):
                        vr = it.get("videoRenderer")
                        if not vr:
                            continue
                        vid = vr.get("videoId")
                        if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                            continue
                        title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
                        length = vr.get("lengthText", {}).get("simpleText") or "".join(r.get("text", "") for r in vr.get("lengthText", {}).get("runs", []))
                        channel = "".join(r.get("text", "") for r in vr.get("ownerText", {}).get("runs", []))
                        views = vr.get("viewCountText", {}).get("simpleText") or "".join(r.get("text", "") for r in vr.get("viewCountText", {}).get("runs", []))
                        badges = [b.get("metadataBadgeRenderer", {}).get("label") for b in vr.get("badges", [])]
                        thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
                        poster = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                        results.append({
                            "id": vid,
                            "title": title,
                            "length_str": length,
                            "duration": cls._parse_duration(length),
                            "channel": channel,
                            "views": views,
                            "badges": badges,
                            "poster": poster,
                        })
        except Exception as e:
            logger.debug(f"YouTube InnerTube search failed for '{query}': {e}")
        return results

    @classmethod
    def _search_ytdlp(cls, query, timeout=6.0):
        """Fallback search using local yt-dlp binary if available."""
        yt_dlp_bin = shutil.which("yt-dlp")
        if not yt_dlp_bin:
            for p in ["/app/bin/yt-dlp", "/usr/bin/yt-dlp", "/usr/local/bin/yt-dlp", os.path.expanduser("~/.local/bin/yt-dlp")]:
                if os.path.exists(p):
                    yt_dlp_bin = p
                    break
        if not yt_dlp_bin:
            return []

        try:
            cmd = [
                yt_dlp_bin,
                "--no-warnings",
                "--flat-playlist",
                "--dump-single-json",
                "--socket-timeout", str(int(timeout)),
                f"ytsearch15:{query.strip()}",
            ]
            run_env = dict(os.environ)
            try:
                from . import doh
                run_env.update(doh.get_doh_env())
            except Exception:
                pass
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=run_env)
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                entries = data.get("entries") or []
                items = []
                for e in entries:
                    vid = e.get("id")
                    if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                        continue
                    dur = int(e.get("duration") or 0)
                    length_str = f"{dur // 3600}:{(dur % 3600) // 60:02d}:{dur % 60:02d}" if dur >= 3600 else f"{dur // 60}:{dur % 60:02d}"
                    thumbs = e.get("thumbnails") or []
                    poster = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                    items.append({
                        "id": vid,
                        "title": e.get("title") or "YouTube Video",
                        "length_str": length_str,
                        "duration": dur,
                        "channel": e.get("uploader") or e.get("channel") or "YouTube",
                        "views": f"{e.get('view_count', '')} views" if e.get("view_count") else "",
                        "badges": ["1080p"] if (e.get("height") or 0) >= 1080 else [],
                        "poster": poster,
                    })
                return items
        except Exception as e:
            logger.debug(f"yt-dlp fallback search failed for '{query}': {e}")
        return []

    @classmethod
    def _search_invidious(cls, query, timeout=4.0):
        """Last fallback search using Invidious instances."""
        encoded = urllib.parse.quote(query.strip())
        for host in ["https://inv.nadeko.net", "https://invidious.nerdvpn.de", "https://yewtu.be"]:
            url = f"{host}/api/v1/search?q={encoded}&type=video"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = []
                    for v in data[:15]:
                        vid = v.get("videoId")
                        if not vid or not re.fullmatch(r"^[a-zA-Z0-9_-]{11}$", vid):
                            continue
                        dur = int(v.get("lengthSeconds") or 0)
                        length_str = f"{dur // 3600}:{(dur % 3600) // 60:02d}:{dur % 60:02d}" if dur >= 3600 else f"{dur // 60}:{dur % 60:02d}"
                        items.append({
                            "id": vid,
                            "title": v.get("title") or "YouTube Video",
                            "length_str": length_str,
                            "duration": dur,
                            "channel": v.get("author") or "YouTube",
                            "views": f"{v.get('viewCount', '')} views" if v.get("viewCount") else "",
                            "badges": [],
                            "poster": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        })
                    if items:
                        return items
            except Exception:
                continue
        return []

    @classmethod
    def _query_videos(cls, query, timeout=6.0):
        """Performs multi-tiered search: InnerTube -> yt-dlp -> Invidious."""
        items = cls._search_innertube(query, timeout=timeout)
        if not items:
            items = cls._search_ytdlp(query, timeout=timeout)
        if not items:
            items = cls._search_invidious(query, timeout=timeout)
        return items

    # --- Catalog & Discover Support ---

    def get_catalog_items(
        self,
        catalog_id="popular_movies",
        media_type="movie",
        genre=None,
        page=1,
        query=None,
        limit=50,
    ):
        """Fetch catalog items or search results."""
        if query:
            clean_q = str(query).strip()
            is_series = (media_type in ("series", "show", "shows", "tv", "anime")) or "series" in str(catalog_id)
            search_query = f"{clean_q} full episode" if is_series else f"{clean_q} full movie"
            videos = self._query_videos(search_query)
            items = []
            for v in videos[:limit]:
                v_title = v["title"]
                year_match = re.search(r"\b((?:19|20)\d{2})\b", v_title)
                extracted_year = year_match.group(1) if year_match else ""
                clean_name = re.sub(r"\[.*?\]|\(.*?\)|\|.*", "", v_title).strip()
                items.append({
                    "id": f"yt:{v['id']}",
                    "title": clean_name or v_title,
                    "year": extracted_year,
                    "medium_cover_image": v.get("poster") or f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg",
                    "background": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg",
                    "type": "series" if is_series else "movie",
                    "genres": ["YouTube"],
                    "rating": "",
                })
            return items

        cid = str(catalog_id or "").lower()
        if cid == "classic_movies":
            return CLASSIC_MOVIES_CATALOG

        try:
            try:
                from popcornbox import tmdb_helper
            except ImportError:
                from src import tmdb_helper

            mtype = "series" if ("series" in cid or media_type in ("series", "show", "tv", "anime")) else "movie"
            tmdb_cat = "new" if "new" in cid else ("top_rated" if "doc" in cid else "popular")
            items, _ = tmdb_helper.fetch_default_tmdb_catalog_page(
                tmdb_cat, media_type=mtype, page=page, limit=limit
            )
            return items or []
        except Exception:
            return CLASSIC_MOVIES_CATALOG if "movie" in cid else []

    # --- Stream Scraping Support ---

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
        """Searches YouTube for matching full movie or series episode streams."""
        target_title = title
        target_year = year

        # Resolve title from TMDb helper if not passed
        if not target_title and imdb_id:
            try:
                try:
                    from popcornbox import tmdb_helper
                except ImportError:
                    from src import tmdb_helper
                meta = tmdb_helper.fetch_tmdb_standalone_metadata(imdb_id, media_type=media_type)
                if meta and meta.get("title"):
                    target_title = meta["title"]
                    if not target_year and meta.get("year"):
                        target_year = meta["year"]
            except Exception as e:
                logger.debug(f"Could not resolve metadata for {imdb_id}: {e}")

        if not target_title or not str(target_title).strip():
            return []

        clean_title = str(target_title).strip()
        is_series = (media_type in ("series", "show", "shows", "tv", "anime")) or (season is not None and episode is not None)

        raw_items = []
        seen_ids = set()

        if is_series:
            s_num = int(season) if season is not None else 1
            e_num = int(episode) if episode is not None else 1
            queries = [
                f"{clean_title} S{s_num:02d}E{e_num:02d}",
                f"{clean_title} Season {s_num} Episode {e_num} full episode",
                f"{clean_title} Episode {e_num} full",
            ]
            for q in queries:
                items = self._query_videos(q)
                for it in items:
                    if it["id"] not in seen_ids:
                        seen_ids.add(it["id"])
                        raw_items.append(it)
                if len(raw_items) >= 15:
                    break

            scored = []
            for it in raw_items:
                v_title = it["title"].lower()
                dur = it["duration"]
                if 0 < dur < 180:
                    continue

                neg_in_target = {w for w in self.NEGATIVE_SERIES_KEYWORDS if w in clean_title.lower()}
                has_neg = any(
                    re.search(r"\b" + re.escape(neg) + r"\b", v_title)
                    for neg in self.NEGATIVE_SERIES_KEYWORDS
                    if neg not in neg_in_target
                )
                if has_neg:
                    continue

                # Episode check
                ep_pattern = rf"(?:ep|episode|part|e)[\s\._-]*0*{e_num}\b"
                other_ep_pattern = rf"(?:ep|episode|e)[\s\._-]*0*(\d+)\b"
                found_eps = [int(x) for x in re.findall(other_ep_pattern, v_title) if x.isdigit()]
                if not re.search(ep_pattern, v_title) and found_eps and e_num not in found_eps:
                    continue

                # Season check
                s_pattern = rf"(?:s|season)[\s\._-]*0*{s_num}\b"
                other_s_pattern = rf"(?:s|season)[\s\._-]*0*(\d+)\b"
                found_seasons = [int(x) for x in re.findall(other_s_pattern, v_title) if x.isdigit()]
                if not re.search(s_pattern, v_title) and found_seasons and s_num not in found_seasons:
                    continue

                score = 50 if re.search(ep_pattern, v_title) else 10
                if re.search(s_pattern, v_title):
                    score += 30
                if any(k in v_title for k in ["full episode", "full ep", "complete episode"]):
                    score += 20
                if dur >= 600:
                    score += 15
                if any(b in ["4K", "HD"] for b in it.get("badges", [])):
                    score += 10
                scored.append((score, it))

        else:
            # Movie search
            queries = []
            if target_year:
                queries.append(f"{clean_title} {target_year} full movie")
            queries.append(f"{clean_title} full movie")
            queries.append(f'"{clean_title}" full length')
            for q in queries:
                items = self._query_videos(q)
                for it in items:
                    if it["id"] not in seen_ids:
                        seen_ids.add(it["id"])
                        raw_items.append(it)
                if len(raw_items) >= 15:
                    break

            scored = []
            t_words = [w for w in re.findall(r"\b[a-z0-9]+\b", clean_title.lower()) if w not in _STOP_WORDS]
            t_word_set = set(t_words) if t_words else set(re.findall(r"\b[a-z0-9]+\b", clean_title.lower()))

            for it in raw_items:
                v_title = it["title"].lower()
                dur = it["duration"]
                if 0 < dur < 1800:
                    continue

                neg_in_target = {w for w in self.NEGATIVE_MOVIE_KEYWORDS if w in clean_title.lower()}
                has_neg = any(
                    re.search(r"\b" + re.escape(neg) + r"\b", v_title)
                    for neg in self.NEGATIVE_MOVIE_KEYWORDS
                    if neg not in neg_in_target
                )

                v_words = set(re.findall(r"\b[a-z0-9]+\b", v_title))
                overlap = len(t_word_set.intersection(v_words))
                if len(t_word_set) > 0 and (overlap / len(t_word_set)) < 0.35:
                    continue

                score = int(40 * (overlap / len(t_word_set))) if len(t_word_set) > 0 else 0
                if has_neg:
                    if dur >= 3600:
                        score -= 30
                    else:
                        continue

                if target_year and str(target_year) in v_title:
                    score += 25
                elif target_year:
                    other_years = re.findall(r"\b((?:19|20)\d{2})\b", v_title)
                    if other_years and str(target_year) not in other_years:
                        score -= 20

                if any(k in v_title for k in ["full movie", "full length", "complete movie", "entire movie"]):
                    score += 20
                if dur >= 3600:
                    score += 20
                if dur >= 4800:
                    score += 10
                if any(b in ["4K", "HD"] for b in it.get("badges", [])):
                    score += 10

                scored.append((score, it))

        scored.sort(key=lambda x: -x[0])
        streams = []

        for _score, it in scored[:8]:
            quality = "1080p"
            if any("4k" in str(b).lower() for b in it.get("badges", [])) or "4k" in it["title"].lower():
                quality = "4K"
            elif "720" in it["title"].lower():
                quality = "720p"

            fn = (
                f"{clean_title}.S{int(season):02d}E{int(episode):02d}.mp4"
                if is_series and season and episode
                else f"{clean_title} ({target_year or ''}).mp4"
            )
            length_str = it.get("length_str") or "Full Video"
            views_str = it.get("views") or ""
            ch = it.get("channel") or "YouTube"
            title_line = f"YouTube • {ch} • {length_str}"
            if views_str:
                title_line += f" • {views_str}"

            streams.append({
                "name": f"YouTube [{quality}]",
                "title": f"{title_line}\n{it['title']}",
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "ytId": it["id"],
                "quality": quality,
                "size": length_str,
                "is_http": True,
                "is_torrent": False,
                "behaviorHints": {
                    "filename": fn,
                    "notWebReady": False,
                },
            })

        return streams
