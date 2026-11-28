"""Skip Intro / Recap / Outro segment lookup.

Sources, in priority order per category (opening / recap / ending):
1. Container chapters named like "Opening", "ED", "Recap", "Credits"
2. IntroDB         (api.introdb.app, IMDb ids)
3. TheIntroDB      (api.theintrodb.org, IMDb or TMDb ids)
4. AniSkip         (api.aniskip.com, MyAnimeList ids; Kitsu ids are mapped)

A segment is a dict {"start": float, "end": float | None, "type": str,
"provider": str}. "end" None means the segment runs to the end of the file.
"""

import json
import logging
import re
import threading
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

INTRODB_BASE = "https://api.introdb.app"
THEINTRODB_BASE = "https://api.theintrodb.org/v2"
ANISKIP_BASE = "https://api.aniskip.com/v2"
KITSU_BASE = "https://kitsu.app/api/edge"

_USER_AGENT = "PopcornBox"
_cache: dict = {}
_cache_lock = threading.Lock()

# --- categories ----------------------------------------------------------

def segment_category(seg_type):
    t = str(seg_type or "").lower()
    if t in ("intro", "op", "mixed-op", "opening"):
        return "opening"
    if t in ("outro", "ed", "mixed-ed", "credits", "ending"):
        return "ending"
    if t == "recap":
        return "recap"
    return None


def merge_by_priority(*provider_results):
    """First provider to supply a category wins that category."""
    chosen = {}
    for result in provider_results:
        for seg in result or []:
            cat = segment_category(seg.get("type"))
            if cat and cat not in chosen:
                chosen[cat] = seg
    return sorted(chosen.values(), key=lambda s: s["start"])


def _segment(start, end, seg_type, provider):
    try:
        start = float(start or 0.0)
        end = float(end) if end is not None else None
    except (TypeError, ValueError):
        return None
    if start < 0 or (end is not None and end <= start):
        return None
    return {"start": start, "end": end, "type": seg_type, "provider": provider}


# --- chapters ------------------------------------------------------------

_OPENING_RE = re.compile(r"^\s*(op(ening)?)(\s*(credits|theme))?\s*\d*\s*$", re.I)
_ENDING_RE = re.compile(r"^\s*(ed|ending)(\s*(credits|theme))?\s*\d*\s*$", re.I)
_CREDITS_RE = re.compile(r"^\s*(end|closing)?\s*credits\s*$", re.I)
_RECAP_RE = re.compile(r"^\s*(recap|previously(\s+on.*)?)\s*$", re.I)
_INTRO_RE = re.compile(r"^\s*intro\s*$", re.I)
_POST_CREDITS_RE = re.compile(r"(post|mid|after)[\s-]credits", re.I)
_CHAPTER_MIN_SEC = 5.0
_CHAPTER_MAX_SEC = 240.0


def _classify_chapter(title):
    if not title:
        return None
    if _POST_CREDITS_RE.search(title):
        return None
    if _OPENING_RE.match(title):
        return "opening"
    if _ENDING_RE.match(title) or _CREDITS_RE.match(title):
        return "outro"
    if _RECAP_RE.match(title):
        return "recap"
    if _INTRO_RE.match(title):
        return "intro-title"
    return None


def from_chapters(chapters, duration):
    """Derive segments from MPV chapter-list entries ({"time", "title"})."""
    chapters = sorted(
        (c for c in (chapters or []) if isinstance(c, dict) and c.get("time") is not None),
        key=lambda c: float(c["time"]),
    )
    if len(chapters) < 2:
        return []
    classified = []
    for i, ch in enumerate(chapters):
        start = float(ch["time"])
        end = float(chapters[i + 1]["time"]) if i + 1 < len(chapters) else (float(duration) if duration else None)
        classified.append((start, end, _classify_chapter(ch.get("title"))))
    has_opening = any(kind == "opening" for _, _, kind in classified)
    out = []
    for start, end, kind in classified:
        if kind == "intro-title":
            # "Intro" counts as the opening only when no OP chapter exists.
            kind = None if has_opening else "opening"
        if kind is None or end is None:
            continue
        if not (_CHAPTER_MIN_SEC <= end - start <= _CHAPTER_MAX_SEC):
            continue
        seg = _segment(start, end, "intro" if kind == "opening" else kind, "chapters")
        if seg:
            out.append(seg)
    return out


# --- HTTP ----------------------------------------------------------------

def _get_json(url, timeout, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        ssl_ctx = None
        try:
            from .api import _SSL_CONTEXT as ssl_ctx
        except Exception:
            pass
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[Skip] {url} failed: {e}")
        return None


# --- providers -----------------------------------------------------------

def _introdb_segment(raw, seg_type):
    if not isinstance(raw, dict):
        return None
    start = raw.get("start_sec")
    if start is None and raw.get("start_ms") is not None:
        start = raw["start_ms"] / 1000.0
    end = raw.get("end_sec")
    if end is None and raw.get("end_ms") is not None:
        end = raw["end_ms"] / 1000.0
    if start is None or end is None:
        return None
    return _segment(start, end, seg_type, "introdb")


def _trim_credits_before_post_credits(credits, post):
    """Credit skipping must stop before a post-credits scene."""
    if not credits or not post:
        return credits
    c_end = credits["end"] if credits["end"] is not None else float("inf")
    if post["start"] < c_end and (post["end"] is None or post["end"] > credits["start"]):
        if post["start"] <= credits["start"]:
            return None
        credits = dict(credits, end=post["start"])
    return credits


def fetch_introdb(imdb_id, season=None, episode=None, is_movie=False, timeout=6.0):
    if not imdb_id:
        return []
    if is_movie:
        q = {"imdb_id": imdb_id, "is_movie": "true"}
    else:
        q = {"imdb_id": imdb_id, "season": season, "episode": episode}
    data = _get_json(f"{INTRODB_BASE}/segments?{urllib.parse.urlencode(q)}", timeout)
    if not isinstance(data, dict):
        return []
    intro = _introdb_segment(data.get("intro"), "intro")
    recap = _introdb_segment(data.get("recap"), "recap")
    outro = _introdb_segment(data.get("outro"), "outro")
    post = _introdb_segment(data.get("post_credits"), "post-credits")
    outro = _trim_credits_before_post_credits(outro, post)
    return [s for s in (intro, recap, outro) if s]


def _theintrodb_segments(entries, seg_type):
    out = []
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            continue
        start = raw.get("start_ms")
        end = raw.get("end_ms")
        seg = _segment(
            (start / 1000.0) if start is not None else 0.0,
            (end / 1000.0) if end is not None else None,
            seg_type,
            "theintrodb",
        )
        if seg:
            out.append(seg)
    return out


def fetch_theintrodb(imdb_id=None, tmdb_id=None, season=None, episode=None, is_movie=False, timeout=6.0):
    q = {}
    if imdb_id:
        q["imdb_id"] = imdb_id
    elif tmdb_id:
        q["tmdb_id"] = tmdb_id
    else:
        return []
    if not is_movie:
        q["season"] = season
        q["episode"] = episode
    data = _get_json(f"{THEINTRODB_BASE}/media?{urllib.parse.urlencode(q)}", timeout)
    if not isinstance(data, dict):
        return []
    out = []
    for key, seg_type in (("intro", "intro"), ("recap", "recap"), ("credits", "outro")):
        segs = _theintrodb_segments(data.get(key), seg_type)
        if segs:
            out.append(segs[0])
    return out


def kitsu_to_mal(kitsu_id, timeout=6.0):
    cache_key = f"kitsu-mal:{kitsu_id}"
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]
    q = urllib.parse.urlencode({"filter[externalSite]": "myanimelist/anime"})
    # Kitsu's JSON:API rejects "Accept: application/json" with 406.
    data = _get_json(f"{KITSU_BASE}/anime/{kitsu_id}/mappings?{q}", timeout,
                     headers={"Accept": "application/vnd.api+json"})
    mal_id = None
    for m in (data or {}).get("data", []) if isinstance(data, dict) else []:
        ext = (m.get("attributes") or {}).get("externalId")
        if ext and str(ext).isdigit():
            mal_id = str(ext)
            break
    with _cache_lock:
        _cache[cache_key] = mal_id
    return mal_id


def fetch_aniskip(mal_id, episode, timeout=6.0):
    if not mal_id or episode is None:
        return []
    types = "types=op&types=ed&types=recap&types=mixed-op&types=mixed-ed"
    data = _get_json(f"{ANISKIP_BASE}/skip-times/{mal_id}/{int(episode)}?{types}&episodeLength=0", timeout)
    if not isinstance(data, dict) or not data.get("found"):
        return []
    out = []
    for r in data.get("results") or []:
        iv = (r or {}).get("interval") or {}
        seg = _segment(iv.get("startTime"), iv.get("endTime"), r.get("skipType"), "aniskip")
        if seg:
            out.append(seg)
    return out


# --- entry point ---------------------------------------------------------

def parse_item_ids(item_id, season=None, episode=None):
    """Split a PopcornBox/Stremio id into lookup ids.

    Returns dict with imdb, tmdb, mal, kitsu, season, episode.
    """
    ids = {"imdb": None, "tmdb": None, "mal": None, "kitsu": None, "season": season, "episode": episode}
    s = str(item_id or "").strip()
    parts = s.split(":")
    head = parts[0].lower() if parts else ""
    if re.fullmatch(r"tt\d+", parts[0] if parts else ""):
        ids["imdb"] = parts[0]
        rest = parts[1:]
    elif head in ("tmdb", "mal", "kitsu") and len(parts) >= 2 and parts[1].isdigit():
        ids[head] = parts[1]
        rest = parts[2:]
    else:
        return ids
    # Stremio-style ids embed the episode: tt123:1:2, kitsu:42:5
    nums = [int(p) for p in rest if p.isdigit()]
    if head in ("mal", "kitsu"):
        if ids["episode"] is None and nums:
            ids["episode"] = nums[-1]
    elif len(nums) >= 2:
        if ids["season"] is None:
            ids["season"] = nums[0]
        if ids["episode"] is None:
            ids["episode"] = nums[1]
    return ids


def fetch_segments(item_ids, media_type=None, season=None, episode=None, timeout=6.0):
    """Look up API skip segments for the item being played.

    item_ids: candidate ids (e.g. [item["imdb_id"], item["id"]]); the first
    usable one of each kind is used. Blocking; call from a worker thread.
    """
    ids = {"imdb": None, "tmdb": None, "mal": None, "kitsu": None, "season": season, "episode": episode}
    for cand in item_ids or []:
        parsed = parse_item_ids(cand, season, episode)
        for k, v in parsed.items():
            if ids.get(k) is None and v is not None:
                ids[k] = v
    season, episode = ids["season"], ids["episode"]
    mt = str(media_type or "").lower()
    if mt == "movie":
        is_movie = True
    elif mt in ("series", "tv", "anime"):
        is_movie = False
    else:
        is_movie = episode is None and not (ids["mal"] or ids["kitsu"])
    if not is_movie and episode is None:
        return []
    if not is_movie and season is None and not (ids["mal"] or ids["kitsu"]):
        season = 1

    key = json.dumps([ids["imdb"], ids["tmdb"], ids["mal"], ids["kitsu"], season, episode, is_movie])
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    results = {}

    def run(name, fn, *args, **kwargs):
        try:
            results[name] = fn(*args, **kwargs)
        except Exception as e:
            logger.debug(f"[Skip] {name} lookup failed: {e}")
            results[name] = []

    jobs = []
    if ids["imdb"]:
        jobs.append(("introdb", fetch_introdb, (ids["imdb"], season, episode, is_movie, timeout)))
    if ids["imdb"] or ids["tmdb"]:
        jobs.append(("theintrodb", fetch_theintrodb, (ids["imdb"], ids["tmdb"], season, episode, is_movie, timeout)))
    if not is_movie and (ids["mal"] or ids["kitsu"]):
        def aniskip_job():
            mal = ids["mal"] or kitsu_to_mal(ids["kitsu"], timeout)
            return fetch_aniskip(mal, episode, timeout)
        jobs.append(("aniskip", aniskip_job, ()))

    threads = [threading.Thread(target=run, args=(name, fn, *args), daemon=True) for name, fn, args in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout * 2 + 1)

    merged = merge_by_priority(results.get("introdb"), results.get("theintrodb"), results.get("aniskip"))
    with _cache_lock:
        _cache[key] = merged
    return merged


def active_segment(segments, position, duration=None, lead_out=0.5):
    """The segment containing `position`, or None.

    Ends at `lead_out` seconds before its end so the button disappears once
    there is nothing left to skip.
    """
    for seg in segments or []:
        end = seg.get("end")
        if end is None:
            end = duration
        if end is None:
            continue
        if seg["start"] <= position < end - lead_out:
            return seg
    return None
