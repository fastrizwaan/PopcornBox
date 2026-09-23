import re
import urllib.parse
import json
import base64
import concurrent.futures
from . import database

def _get_cached_request(*args, **kwargs):
    from .api import _get_cached_request as req
    return req(*args, **kwargs)


DEFAULT_TMDB_API_KEY = "68e094699525b18a70bab2f86b1fa706"

def get_tmdb_api_key():
    import os
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    try:
        for addon in database.get_addons():
            m_url = addon.get("manifest_url", "")
            if "tmdb" in m_url.lower():
                match = re.search(r'/([a-fA-F0-9]{32})/', m_url)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return DEFAULT_TMDB_API_KEY

def resolve_to_imdb_id(imdb_id, media_type, title=None):
    """
    Resolves a TMDB ID or raw ID to an IMDB ID using multiple fallback strategies.
    If the ID is already an IMDB ID, returns it clean (tt...).
    """
    if isinstance(imdb_id, list):
        return [resolve_to_imdb_id(i, media_type, title) for i in imdb_id]
        
    if not imdb_id and not title:
        return None

    if imdb_id:
        str_id = str(imdb_id).strip()
        if str_id.startswith("ctmdb."):
            return str_id
        if str_id.startswith("tt"):
            return str_id.split(":")[0]
        if str_id.startswith("tpb_ctl:"):
            try:
                raw_b64 = str_id.split("tpb_ctl:", 1)[1]
                payload = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
                p_url = payload.get("poster", "")
                tt_match = re.search(r'\b(tt\d{7,8})\b', p_url)
                if tt_match:
                    return tt_match.group(1)
            except Exception:
                pass
        tt_match = re.search(r'\b(tt\d{7,8})\b', str_id)
        if tt_match:
            return tt_match.group(1)

    is_tmdb = False
    if imdb_id:
        str_id = str(imdb_id).strip()
        if str_id.startswith("bolly:") or str_id.startswith("hub:"):
            is_tmdb = True
            if ":s:" in str_id:
                media_type = "series"
            elif ":m:" in str_id:
                media_type = "movie"
        else:
            is_tmdb = str_id.startswith("tmdb:") or str_id.isdigit()
        if not is_tmdb:
            return str_id
        
    resolved_id = None
    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
    
    # 1. Try official TMDB API (only for genuine TMDB IDs)
    if is_tmdb:
        try:
            tmdb_api_key = get_tmdb_api_key()
            if tmdb_api_key:
                tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
                tmdb_type = "tv" if c_type == "series" else "movie"
                tmdb_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={tmdb_api_key}&append_to_response=external_ids"
                
                tmdb_data = _get_cached_request(tmdb_url, max_age_hours=168)
                if tmdb_data and "external_ids" in tmdb_data:
                    resolved_id = tmdb_data["external_ids"].get("imdb_id")
        except Exception as e:
            pass
            
        # 2. Try the public Stremio TMDB addon
        if not resolved_id:
            try:
                tmdb_id = str(imdb_id).split(":")[-1] if ":" in str(imdb_id) else str(imdb_id).split(".")[-1]
                addon_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/tmdb:{tmdb_id}.json"
                addon_data = _get_cached_request(addon_url, max_age_hours=168)
                if addon_data and "meta" in addon_data:
                    resolved_id = addon_data["meta"].get("imdb_id")
            except Exception:
                pass

    # 3. Fallback to Cinemeta Search by title (safe substring matching)
    if not resolved_id and title and title != "Loading...":
        try:
            search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
            search_data = _get_cached_request(search_url, max_age_hours=168)
            if search_data and "metas" in search_data:
                for m in search_data["metas"]:
                    m_id = m.get("imdb_id") or m.get("id", "")
                    m_name = str(m.get("name", "")).lower()
                    t_lower = str(title).lower()
                    # Safe check: title contains or is contained in the match, and id is IMDB
                    if str(m_id).startswith("tt") and (m_name in t_lower or t_lower in m_name):
                        resolved_id = m_id
                        break
        except Exception:
            pass
            
    return resolved_id or imdb_id

def resolve_all_provider_ids(item_id, media_type="movie", title=None):
    """
    Returns a list of all distinct provider IDs (IMDB tt..., TMDB, DesiFlix dsf:...)
    for the given media item so all stream addons (Torrentio, Castle, DesiFlix, etc.) can be queried.
    Optimized to only perform network lookups if at least one installed stream addon requires that ID prefix.
    """
    ids = set()
    if isinstance(item_id, list):
        for i in item_id:
            if i: ids.add(str(i))
    elif item_id:
        ids.add(str(item_id))

    # Add TMDB format if item starts with bolly: or hub:
    for single_id in list(ids):
        str_s = str(single_id)
        if str_s.startswith("bolly:") or str_s.startswith("hub:"):
            tmdb_num = str_s.split(":")[-1]
            if tmdb_num.isdigit():
                ids.add(f"tmdb:{tmdb_num}")
                if ":s:" in str_s:
                    media_type = "series"
                elif ":m:" in str_s:
                    media_type = "movie"

    # Determine what prefixes are actually needed by installed stream addons
    installed_addons = [a for a in database.get_addons() if a.get("enabled", True) and not a.get("manifest_url", "").startswith("builtin://")]
    needed_prefixes = set()
    for a in installed_addons:
        prefixes = a.get("idPrefixes")
        if prefixes:
            for p in prefixes:
                needed_prefixes.add(str(p).lower())
        else:
            # Addon with no idPrefixes might support any prefix or default tt
            needed_prefixes.add("tt")

    needs_dsf = any(p.startswith("dsf") for p in needed_prefixes)
    needs_tmdb = any(p.startswith("tmdb") for p in needed_prefixes)
    has_tt = any(str(i).startswith("tt") for i in ids)

    # If we already have tt... and don't need dsf or tmdb, return immediately!
    if has_tt and not needs_dsf and not needs_tmdb:
        return list(ids)

    def task_imdb():
        res = resolve_to_imdb_id(item_id, media_type, title)
        if isinstance(res, list): return res
        return [res] if res else []

    def task_dsf():
        if not needs_dsf:
            return []
        has_dsf = any(str(i).startswith("dsf:") for i in ids)
        if not has_dsf and title and title != "Loading...":
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://desiflix.stremioaddon.workers.dev/catalog/{c_type}/desiflix/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("id")
                        if m_id and str(m_id).startswith("dsf:"):
                            return [str(m_id)]
            except Exception:
                pass
        return []

    def task_tt():
        has_tt_curr = any(str(i).startswith("tt") for i in ids)
        if not has_tt_curr and title and title != "Loading...":
            try:
                c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                search_url = f"https://v3-cinemeta.strem.io/catalog/{c_type}/top/search={urllib.parse.quote(title)}.json"
                data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                if data and "metas" in data:
                    for m in data["metas"]:
                        m_id = m.get("imdb_id") or m.get("id", "")
                        if str(m_id).startswith("tt"):
                            return [str(m_id)]
            except Exception:
                pass
        return []

    def task_tmdb(current_ids):
        if not needs_tmdb:
            return []
        res = []
        has_tmdb = any(str(i).startswith("tmdb:") for i in current_ids)
        if not has_tmdb:
            tt_id = next((i for i in current_ids if str(i).startswith("tt")), None)
            root_tt_id = str(tt_id).split(":")[0] if tt_id else None
            if root_tt_id:
                try:
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    meta_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/meta/{c_type}/{root_tt_id}.json"
                    meta_data = _get_cached_request(meta_url, max_age_hours=168, timeout=2.5)
                    if meta_data and "meta" in meta_data:
                        t_id = meta_data["meta"].get("id")
                        if t_id and str(t_id).startswith("tmdb:"):
                            res.append(str(t_id))
                except Exception:
                    pass
            if not res and title and title != "Loading...":
                try:
                    c_type = "series" if media_type in ["series", "anime", "tv"] else "movie"
                    search_url = f"https://94c8cb9f702d-tmdb-addon.baby-beamup.club/catalog/{c_type}/tmdb.top/search={urllib.parse.quote(title)}.json"
                    search_data = _get_cached_request(search_url, max_age_hours=168, timeout=2.5)
                    if search_data and "metas" in search_data:
                        for m in search_data["metas"]:
                            m_id = m.get("id")
                            if m_id and str(m_id).startswith("tmdb:"):
                                res.append(str(m_id))
                                break
                except Exception:
                    pass
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(task_imdb)
        f2 = executor.submit(task_dsf)
        f3 = executor.submit(task_tt)

        for f in [f1, f2, f3]:
            try:
                for r in f.result(timeout=1.5):
                    if r: ids.add(str(r))
            except Exception:
                pass

        if needs_tmdb and not any(str(i).startswith("tmdb:") for i in ids):
            f4 = executor.submit(task_tmdb, list(ids))
            try:
                for r in f4.result(timeout=1.5):
                    if r: ids.add(str(r))
            except Exception:
                pass

    result_list = []
    if isinstance(item_id, list):
        for i in item_id:
            if str(i) in ids and str(i) not in result_list:
                result_list.append(str(i))
    elif item_id and str(item_id) in ids:
        result_list.append(str(item_id))

    for i in ids:
        if i not in result_list:
            result_list.append(i)

    return result_list


def resolve_to_tmdb_id(item_id, media_type="movie", title=None):
    """
    Resolves an item_id (IMDB tt..., raw digits, tmdb:..., ctmdb....) or title to a numeric TMDB ID.
    Returns string of digits (e.g. '550') or None.
    """
    if not item_id and not title:
        return None

    str_id = str(item_id or "").strip()
    if str_id.startswith("tmdb:"):
        num = str_id.split("tmdb:")[-1].split(":")[0]
        if num.isdigit():
            return num
    if str_id.startswith("ctmdb."):
        num = str_id.split(".")[-1]
        if num.isdigit():
            return num
    if (str_id.startswith("bolly:") or str_id.startswith("hub:")) and ":" in str_id:
        num = str_id.split(":")[-1]
        if num.isdigit():
            return num
    if str_id.isdigit():
        return str_id

    api_key = get_tmdb_api_key()
    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"

    # If it's an IMDB id (tt...), use TMDB /find endpoint
    clean_tt = None
    if str_id.startswith("tt"):
        clean_tt = str_id.split(":")[0]
    else:
        tt_match = re.search(r'\b(tt\d{7,8})\b', str_id)
        if tt_match:
            clean_tt = tt_match.group(1)

    if clean_tt and api_key:
        try:
            find_url = f"https://api.themoviedb.org/3/find/{clean_tt}?api_key={api_key}&external_source=imdb_id"
            data = _get_cached_request(find_url, max_age_hours=168)
            if data:
                if c_type == "tv" and data.get("tv_results"):
                    return str(data["tv_results"][0]["id"])
                elif data.get("movie_results"):
                    return str(data["movie_results"][0]["id"])
                elif data.get("tv_results"):
                    return str(data["tv_results"][0]["id"])
        except Exception:
            pass

    # Search by title as fallback
    if title and title != "Loading..." and api_key:
        try:
            search_url = f"https://api.themoviedb.org/3/search/{c_type}?api_key={api_key}&query={urllib.parse.quote(title)}"
            data = _get_cached_request(search_url, max_age_hours=168)
            if data and data.get("results"):
                return str(data["results"][0]["id"])
        except Exception:
            pass

    return None


def fetch_credits_and_companies(item_id, media_type="movie", title=None):
    """
    Fetches cast members, crew (directors, creators, writers), production companies,
    and networks for the given media item from TMDB.
    Returns:
      {
        "cast": [{"id": int, "name": str, "character": str, "photo": str}, ...],
        "crew": [{"id": int, "name": str, "job": str, "photo": str}, ...],
        "production_companies": [{"id": int, "name": str, "logo": str}, ...],
        "networks": [{"id": int, "name": str, "logo": str}, ...]
      }
    """
    empty_result = {
        "cast": [],
        "crew": [],
        "production_companies": [],
        "networks": []
    }

    tmdb_id = resolve_to_tmdb_id(item_id, media_type, title)
    if not tmdb_id:
        return empty_result

    api_key = get_tmdb_api_key()
    if not api_key:
        return empty_result

    c_type = "tv" if media_type in ["series", "anime", "tv", "tvshow"] else "movie"

    try:
        if c_type == "tv":
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&append_to_response=aggregate_credits,credits"
            data = _get_cached_request(url, max_age_hours=168)
            if not data:
                return empty_result

            # 1. Crew (Creators, Directors, Writers, EPs)
            crew = []
            seen_crew = set()
            # TV creators
            for creator in data.get("created_by", []):
                cid = creator.get("id")
                cname = creator.get("name", "").strip()
                if cid and cname and cid not in seen_crew:
                    seen_crew.add(cid)
                    prof = creator.get("profile_path")
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": "Creator",
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # Additional TV crew from aggregate or standard credits
            raw_crew = data.get("aggregate_credits", {}).get("crew", []) or data.get("credits", {}).get("crew", [])

            def get_tv_crew_info(m):
                jobs = m.get("jobs", [])
                if jobs and isinstance(jobs, list):
                    for target_job in ["Director", "Writer", "Executive Producer"]:
                        for j in jobs:
                            if j.get("job") == target_job:
                                return target_job, j.get("episode_count", 0)
                    return jobs[0].get("job", "Crew"), jobs[0].get("episode_count", 0)
                return m.get("job", "Crew"), m.get("total_episode_count", 0) or 1

            other_crew = []
            for m in raw_crew:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_crew:
                    continue
                job_name, ep_count = get_tv_crew_info(m)
                if job_name in ["Director", "Writer", "Executive Producer"]:
                    other_crew.append((cid, cname, job_name, ep_count, m.get("profile_path")))

            # Sort Directors by episode count descending, then Writers, then Executive Producers
            rank_map = {"Director": 0, "Writer": 1, "Executive Producer": 2}
            other_crew.sort(key=lambda x: (rank_map.get(x[2], 99), -x[3]))

            for cid, cname, job_name, ep_count, prof in other_crew[:25]:
                if cid not in seen_crew:
                    seen_crew.add(cid)
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": job_name,
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # 2. Cast
            cast = []
            seen_cast = set()
            raw_cast = data.get("aggregate_credits", {}).get("cast", []) or data.get("credits", {}).get("cast", [])
            for m in raw_cast:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_cast:
                    continue
                seen_cast.add(cid)
                char = ""
                if "roles" in m and isinstance(m["roles"], list) and m["roles"]:
                    char = m["roles"][0].get("character", "").strip()
                elif "character" in m:
                    char = (m.get("character") or "").strip()
                prof = m.get("profile_path")
                cast.append({
                    "id": cid,
                    "name": cname,
                    "character": char,
                    "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                })
                if len(cast) >= 50:
                    break

            # 3. Production Companies
            production_companies = []
            for c in data.get("production_companies", []):
                cname = c.get("name", "").strip()
                if not cname: continue
                logo = c.get("logo_path")
                production_companies.append({
                    "id": c.get("id"),
                    "name": cname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "company"
                })

            # 4. Networks
            networks = []
            for n in data.get("networks", []):
                nname = n.get("name", "").strip()
                if not nname: continue
                logo = n.get("logo_path")
                networks.append({
                    "id": n.get("id"),
                    "name": nname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "network"
                })

            return {
                "cast": cast,
                "crew": crew,
                "production_companies": production_companies,
                "networks": networks
            }

        else:
            # Movie credits
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={api_key}&append_to_response=credits"
            data = _get_cached_request(url, max_age_hours=168)
            if not data:
                return empty_result

            # 1. Crew (Directors, Writers, Producers)
            crew = []
            seen_crew = set()
            raw_crew = data.get("credits", {}).get("crew", [])

            movie_rank = {"Director": 0, "Writer": 1, "Screenplay": 2, "Producer": 3, "Executive Producer": 4}
            sorted_movie_crew = [m for m in raw_crew if m.get("job") in movie_rank]
            sorted_movie_crew.sort(key=lambda m: (movie_rank.get(m.get("job"), 99), -m.get("popularity", 0)))

            for m in sorted_movie_crew[:25]:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                job = m.get("job") or ""
                if cid and cname and cid not in seen_crew:
                    seen_crew.add(cid)
                    prof = m.get("profile_path")
                    crew.append({
                        "id": cid,
                        "name": cname,
                        "job": job,
                        "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                    })

            # 2. Cast
            cast = []
            seen_cast = set()
            raw_cast = data.get("credits", {}).get("cast", [])
            for m in raw_cast:
                cid = m.get("id")
                cname = m.get("name", "").strip()
                if not cid or not cname or cid in seen_cast:
                    continue
                seen_cast.add(cid)
                char = (m.get("character") or "").strip()
                prof = m.get("profile_path")
                cast.append({
                    "id": cid,
                    "name": cname,
                    "character": char,
                    "photo": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None
                })
                if len(cast) >= 40:
                    break

            # 3. Production Companies
            production_companies = []
            for c in data.get("production_companies", []):
                cname = c.get("name", "").strip()
                if not cname: continue
                logo = c.get("logo_path")
                production_companies.append({
                    "id": c.get("id"),
                    "name": cname,
                    "logo": f"https://image.tmdb.org/t/p/w300{logo}" if logo else None,
                    "type": "company"
                })

            return {
                "cast": cast,
                "crew": crew,
                "production_companies": production_companies,
                "networks": []
            }

    except Exception as e:
        print(f"[TMDB] Failed to fetch credits/companies for {item_id}: {e}")
        return empty_result


def fetch_person_details(person_id):
    """
    Fetches person details (bio, photo, birthday, place of birth, known for)
    and full filmography (movies and TV shows) from TMDB.
    Returns dictionary with person info and sorted filmography list.
    """
    if not person_id:
        return None

    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        url = f"https://api.themoviedb.org/3/person/{person_id}?api_key={api_key}&append_to_response=combined_credits"
        data = _get_cached_request(url, max_age_hours=168)
        if not data:
            return None

        # Build combined credits list
        combined = data.get("combined_credits", {})
        raw_cast = combined.get("cast", [])
        raw_crew = combined.get("crew", [])

        filmography = []
        seen_media = set()

        # Add cast items
        for item in raw_cast:
            mid = item.get("id")
            mtype = item.get("media_type") or "movie"
            unique_key = f"{mtype}:{mid}"
            if not mid or unique_key in seen_media:
                continue
            seen_media.add(unique_key)
            title = item.get("title") or item.get("name") or "Unknown"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{item['poster_path']}" if item.get("poster_path") else ""
            filmography.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "series" if mtype == "tv" else "movie",
                "media_type": "series" if mtype == "tv" else "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "character": (item.get("character") or "").strip(),
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date
            })

        # Add crew items (e.g. Director, Creator, Writer) if not already present
        for item in raw_crew:
            mid = item.get("id")
            mtype = item.get("media_type") or "movie"
            unique_key = f"{mtype}:{mid}"
            job = item.get("job") or ""
            if job not in ["Director", "Creator", "Writer", "Screenplay"]:
                continue
            if not mid or unique_key in seen_media:
                continue
            seen_media.add(unique_key)
            title = item.get("title") or item.get("name") or "Unknown"
            rel_date = item.get("release_date") or item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{item['poster_path']}" if item.get("poster_path") else ""
            filmography.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "series" if mtype == "tv" else "movie",
                "media_type": "series" if mtype == "tv" else "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "character": job,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date
            })

        # Sort filmography: primarily by popularity/vote_count and release_date
        filmography.sort(key=lambda x: (x.get("vote_count", 0) > 10, x.get("popularity", 0.0)), reverse=True)

        prof = data.get("profile_path")
        return {
            "id": data.get("id"),
            "name": data.get("name", ""),
            "biography": (data.get("biography") or "").strip(),
            "birthday": data.get("birthday"),
            "deathday": data.get("deathday"),
            "place_of_birth": data.get("place_of_birth"),
            "known_for": data.get("known_for_department") or "Acting",
            "photo": f"https://image.tmdb.org/t/p/w300{prof}" if prof else None,
            "filmography": filmography
        }
    except Exception as e:
        print(f"[TMDB] Failed to fetch person details for {person_id}: {e}")
        return None


def fetch_company_details(entity_id, entity_type="company"):
    """
    Fetches details for a production company or TV network from TMDB,
    along with a curated catalog of movies and series produced or broadcast by that entity.
    """
    if not entity_id:
        return None
    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        is_network = str(entity_type).lower() in ["network", "networks", "tv_network"]

        info_data = None
        if is_network:
            info_url = f"https://api.themoviedb.org/3/network/{entity_id}?api_key={api_key}"
            info_data = _get_cached_request(info_url, max_age_hours=168, timeout=5.0)
            if not info_data:
                info_data = _get_cached_request(f"https://api.themoviedb.org/3/company/{entity_id}?api_key={api_key}", max_age_hours=168, timeout=5.0)
                if info_data:
                    is_network = False
        else:
            info_url = f"https://api.themoviedb.org/3/company/{entity_id}?api_key={api_key}"
            info_data = _get_cached_request(info_url, max_age_hours=168, timeout=5.0)
            if not info_data:
                info_data = _get_cached_request(f"https://api.themoviedb.org/3/network/{entity_id}?api_key={api_key}", max_age_hours=168, timeout=5.0)
                if info_data:
                    is_network = True

        movie_url = f"https://api.themoviedb.org/3/discover/movie?api_key={api_key}&with_companies={entity_id}&sort_by=popularity.desc&page=1"
        if is_network:
            tv_url = f"https://api.themoviedb.org/3/discover/tv?api_key={api_key}&with_networks={entity_id}&sort_by=popularity.desc&page=1"
        else:
            tv_url = f"https://api.themoviedb.org/3/discover/tv?api_key={api_key}&with_companies={entity_id}&sort_by=popularity.desc&page=1"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_movies = executor.submit(_get_cached_request, movie_url, 168, None, False, 5.0)
            fut_tv = executor.submit(_get_cached_request, tv_url, 168, None, False, 5.0)
            raw_movies = fut_movies.result() or {}
            raw_tv = fut_tv.result() or {}

        movies = []
        for item in raw_movies.get("results", []):
            mid = item.get("id")
            poster_path = item.get("poster_path")
            if not mid or not poster_path:
                continue
            title = item.get("title") or item.get("original_title") or "Unknown"
            rel_date = item.get("release_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            movies.append({
                "id": f"tmdb:{mid}",
                "raw_id": mid,
                "title": title,
                "name": title,
                "type": "movie",
                "media_type": "movie",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date,
                "overview": (item.get("overview") or "").strip()
            })

        series = []
        for item in raw_tv.get("results", []):
            sid = item.get("id")
            poster_path = item.get("poster_path")
            if not sid or not poster_path:
                continue
            title = item.get("name") or item.get("original_name") or "Unknown"
            rel_date = item.get("first_air_date") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else ""
            poster = f"https://image.tmdb.org/t/p/w300{poster_path}"
            series.append({
                "id": f"tmdb:{sid}",
                "raw_id": sid,
                "title": title,
                "name": title,
                "type": "series",
                "media_type": "series",
                "poster": poster,
                "medium_cover_image": poster,
                "year": year,
                "rating": round(float(item.get("vote_average", 0)), 1) if item.get("vote_average") else None,
                "vote_count": item.get("vote_count", 0),
                "popularity": item.get("popularity", 0.0),
                "release_date": rel_date,
                "overview": (item.get("overview") or "").strip()
            })

        all_titles = movies + series
        all_titles.sort(key=lambda x: (x.get("vote_count", 0) > 10, x.get("popularity", 0.0)), reverse=True)

        logo_path = info_data.get("logo_path") if info_data else None
        return {
            "id": entity_id,
            "type": "network" if is_network else "company",
            "name": (info_data.get("name") if info_data else "") or "",
            "description": (info_data.get("description") if info_data else "") or "",
            "headquarters": (info_data.get("headquarters") if info_data else "") or "",
            "origin_country": (info_data.get("origin_country") if info_data else "") or "",
            "homepage": (info_data.get("homepage") if info_data else "") or "",
            "logo": f"https://image.tmdb.org/t/p/w300{logo_path}" if logo_path else None,
            "movies": movies,
            "series": series,
            "all_titles": all_titles
        }
    except Exception as e:
        print(f"[TMDB] Failed to fetch company details for {entity_id} ({entity_type}): {e}")
        return None

TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western"
}

def fetch_collection_details(cid, title=None, poster=None):
    """
    Fetches full movie collection details (parts, overview, posters, ratings)
    directly from the TMDB API when collection addon metadata is missing or empty.
    """
    clean_id = str(cid).replace("ctmdb.", "").strip()
    api_key = get_tmdb_api_key()
    if not api_key:
        return None
    url = f"https://api.themoviedb.org/3/collection/{clean_id}?api_key={api_key}"
    data = _get_cached_request(url, max_age_hours=168)
    if not data or not isinstance(data, dict) or not data.get("name"):
        return None

    parts = list(data.get("parts", []))
    parts.sort(key=lambda p: p.get("release_date") or "9999-99-99")

    def resolve_part(p_tuple):
        idx, p = p_tuple
        pid = p.get("id")
        t = p.get("title") or f"Part {idx+1}"
        imdb_id = resolve_to_imdb_id(f"tmdb:{pid}", "movie", t) or f"tmdb:{pid}"
        return idx, imdb_id

    resolved_ids = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(parts), 5) or 1) as executor:
        for idx, i_id in executor.map(resolve_part, enumerate(parts)):
            resolved_ids[idx] = i_id

    videos = []
    genre_names = set()
    total_vote = 0.0
    vote_count = 0

    for idx, p in enumerate(parts):
        t = p.get("title") or f"Part {idx+1}"
        rel = p.get("release_date") or ""
        overview = p.get("overview") or ""
        vote = p.get("vote_average", 0.0)
        if vote > 0:
            total_vote += vote
            vote_count += 1
        for gid in p.get("genre_ids", []):
            if gid in TMDB_GENRES:
                genre_names.add(TMDB_GENRES[gid])

        b_path = p.get("backdrop_path") or p.get("poster_path")
        thumb = f"https://image.tmdb.org/t/p/w500{b_path}" if b_path else ""
        part_id = p.get("id")

        videos.append({
            "id": resolved_ids.get(idx, f"tmdb:{part_id}"),
            "season": 1,
            "episode": idx + 1,
            "title": t,
            "overview": f"[IMDB: {vote:.1f}⭐] {overview}" if vote > 0 else overview,
            "released": rel,
            "thumbnail": thumb
        })

    avg_rating = f"{total_vote / vote_count:.1f}" if vote_count > 0 else ""
    p_path = data.get("poster_path")
    cov = f"https://image.tmdb.org/t/p/w500{p_path}" if p_path else (poster or "")
    bg_path = data.get("backdrop_path")
    backdrop = f"https://image.tmdb.org/t/p/original{bg_path}" if bg_path else ""
    year = parts[0].get("release_date", "")[:4] if parts and parts[0].get("release_date") else ""

    return {
        "id": f"ctmdb.{clean_id}",
        "title": data.get("name") or title or "Collection",
        "year": year,
        "medium_cover_image": cov,
        "background": backdrop,
        "description": data.get("overview") or "No synopsis available.",
        "runtime": "",
        "genre": ", ".join(sorted(genre_names)) if genre_names else "Collection",
        "imdbRating": avg_rating,
        "trailer": None,
        "videos": videos,
        "cast": [],
        "genres": sorted(list(genre_names)),
        "type": "collections",
        "is_collection": True
    }


