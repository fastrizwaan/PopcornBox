# stremio_sync.py
#
# Copyright 2026 Mohammed Asif Ali Rizvan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Stremio Account Synchronization Service for PopcornBox.

Provides authentication and two-way synchronization of Watchlist,
Favorites, and Continue Watching across Stremio, Nuvio, and PopcornBox
via the official Stremio Datastore API (https://api.strem.io).
"""

import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import database

logger = logging.getLogger(__name__)

STREMIO_API_URL = "https://api.strem.io/api"
LOGIN_URL = f"{STREMIO_API_URL}/login"
GET_USER_URL = f"{STREMIO_API_URL}/getUser"
LOGOUT_URL = f"{STREMIO_API_URL}/logout"
DATASTORE_GET_URL = f"{STREMIO_API_URL}/datastoreGet"
DATASTORE_PUT_URL = f"{STREMIO_API_URL}/datastorePut"
ADDON_COLLECTION_GET_URL = f"{STREMIO_API_URL}/addonCollectionGet"
ADDON_COLLECTION_SET_URL = f"{STREMIO_API_URL}/addonCollectionSet"
COLLECTION_NAME = "libraryItem"
DEFAULT_TIMEOUT = 8.0

_SSL_CONTEXT = None
try:
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE
    _SSL_CONTEXT = _ctx
except Exception:
    _SSL_CONTEXT = None

_sync_lock = threading.Lock()
_is_syncing = False
_sync_listeners = []
_listeners_lock = threading.Lock()


class StremioSyncError(Exception):
    """Base exception for Stremio synchronization errors."""
    pass


class StremioAuthError(StremioSyncError):
    """Exception raised when authentication fails or session expires."""
    pass


def register_sync_listener(callback):
    """Register a callback to be called whenever synchronization finishes."""
    with _listeners_lock:
        if callback not in _sync_listeners:
            _sync_listeners.append(callback)


def unregister_sync_listener(callback):
    """Unregister a synchronization listener."""
    with _listeners_lock:
        if callback in _sync_listeners:
            _sync_listeners.remove(callback)


def _notify_sync_listeners(success, summary):
    """Notify all registered listeners on GLib idle thread if available, or direct."""
    with _listeners_lock:
        callbacks = list(_sync_listeners)

    for cb in callbacks:
        try:
            cb(success, summary)
        except Exception as e:
            logger.warning(f"Error notifying sync listener: {e}")


def _post_json(url, payload, timeout=DEFAULT_TIMEOUT):
    """Send an HTTP POST request with a JSON payload and return parsed JSON."""
    data_bytes = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "PopcornBox/2.1 (Linux; Flatpak) StremioSync/1.0",
    }
    req = urllib.request.Request(url, data=data_bytes, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
            resp_body = response.read().decode("utf-8")
            if not resp_body:
                return {}
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        err_msg = f"HTTP {e.code}: {e.reason}"
        try:
            err_body = e.read().decode("utf-8")
            err_data = json.loads(err_body)
            if isinstance(err_data, dict) and "error" in err_data:
                err_detail = err_data["error"]
                if isinstance(err_detail, dict):
                    err_msg = err_detail.get("message") or err_msg
                elif isinstance(err_detail, str):
                    err_msg = err_detail
        except Exception:
            pass
        raise StremioSyncError(err_msg) from e
    except urllib.error.URLError as e:
        raise StremioSyncError(f"Network error: {e.reason}") from e
    except TimeoutError as e:
        raise StremioSyncError("Request timed out") from e
    except json.JSONDecodeError as e:
        raise StremioSyncError("Invalid JSON received from server") from e
    except Exception as e:
        raise StremioSyncError(str(e)) from e


def iso_to_epoch(iso_str):
    """Parse an ISO 8601 string into Unix epoch seconds (float)."""
    if not iso_str or not isinstance(iso_str, str):
        return 0.0
    try:
        cleaned = iso_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except Exception:
        # Fallback regex parsing for milliseconds or slight deviations
        try:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", iso_str)
            if m:
                year, mon, day, hr, mn, sc = map(int, m.groups())
                dt = datetime(year, mon, day, hr, mn, sc, tzinfo=timezone.utc)
                return dt.timestamp()
        except Exception:
            pass
    return 0.0


def epoch_to_iso(epoch_seconds):
    """Convert Unix epoch seconds to an ISO 8601 UTC string."""
    try:
        ts = float(epoch_seconds) if epoch_seconds else time.time()
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --- Authentication & Account Management ---

def is_authenticated():
    """Return True if an authKey is saved in settings."""
    return bool(database.get_setting("stremio_auth_key"))


def is_sync_enabled():
    """Return True if Stremio sync is enabled and an authKey exists."""
    return is_authenticated() and bool(database.get_setting("stremio_sync_enabled", True))


def is_addon_sync_enabled():
    """Return True if Stremio addon synchronization is enabled and an authKey exists."""
    return is_authenticated() and bool(database.get_setting("stremio_sync_addons_enabled", True))


def get_user_email():
    """Return the saved Stremio user email."""
    return database.get_setting("stremio_user_email", "")


def get_last_synced_time():
    """Return the Unix timestamp of the last successful sync, or 0.0."""
    return float(database.get_setting("stremio_last_synced", 0.0) or 0.0)


def login(email, password):
    """
    Authenticate with Stremio using email and password.
    Returns dict: {'authKey': ..., 'email': ...} on success, raises StremioAuthError on failure.
    """
    email_clean = (email or "").strip()
    if not email_clean or not password:
        raise StremioAuthError("Email and password are required")

    payload = {"email": email_clean, "password": password, "type": "Login"}
    data = _post_json(LOGIN_URL, payload)

    if isinstance(data, dict):
        if "error" in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise StremioAuthError(msg or "Authentication failed")

        result = data.get("result")
        if isinstance(result, dict) and "authKey" in result:
            auth_key = result["authKey"]
            user = result.get("user") or {}
            user_email = user.get("email") or email_clean

            database.set_setting("stremio_auth_key", auth_key)
            database.set_setting("stremio_user_email", user_email)
            database.set_setting("stremio_sync_enabled", True)
            database.set_setting("stremio_sync_addons_enabled", True)
            return {"authKey": auth_key, "email": user_email}

    raise StremioAuthError("Invalid response received from Stremio login endpoint")


def login_with_auth_key(auth_key):
    """
    Validate and save a Stremio session using an existing authKey directly.
    Returns dict: {'authKey': ..., 'email': ...} on success, raises StremioAuthError on failure.
    """
    key_clean = (auth_key or "").strip()
    if not key_clean:
        raise StremioAuthError("AuthKey cannot be empty")

    payload = {"type": "GetUser", "authKey": key_clean}
    data = _post_json(GET_USER_URL, payload)

    if isinstance(data, dict):
        if "error" in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise StremioAuthError(msg or "Session verification failed")

        result = data.get("result")
        if isinstance(result, dict):
            user_email = result.get("email") or "Stremio User"
            database.set_setting("stremio_auth_key", key_clean)
            database.set_setting("stremio_user_email", user_email)
            database.set_setting("stremio_sync_enabled", True)
            database.set_setting("stremio_sync_addons_enabled", True)
            return {"authKey": key_clean, "email": user_email}

    raise StremioAuthError("Invalid session or session expired")


def logout():
    """Log out of the Stremio account, invalidating the session and clearing credentials."""
    auth_key = database.get_setting("stremio_auth_key")
    if auth_key:
        try:
            _post_json(LOGOUT_URL, {"type": "Logout", "authKey": auth_key}, timeout=3.0)
        except Exception as e:
            logger.debug(f"Best-effort Stremio logout error: {e}")

    database.set_setting("stremio_auth_key", None)
    database.set_setting("stremio_user_email", None)
    database.set_setting("stremio_sync_enabled", False)
    database.set_setting("stremio_sync_addons_enabled", False)
    database.set_setting("stremio_last_synced", None)


# --- Model Mapping & Transformers ---

def remote_to_continue_watching_item(remote_item, existing_local=None):
    """Convert a Stremio libraryItem object into a PopcornBox continue_watching item."""
    state = remote_item.get("state") or {}
    r_id = remote_item.get("_id") or ""
    name = remote_item.get("name") or (existing_local.get("title") if existing_local else "") or "Unknown"

    last_w = iso_to_epoch(state.get("lastWatched") or remote_item.get("_mtime"))
    if last_w <= 0.0:
        last_w = time.time()

    pos = float(state.get("timeOffset") or 0.0)
    dur = float(state.get("duration") or 0.0)
    progress = (pos / dur) if dur > 0 else 0.0
    progress = min(1.0, max(0.0, progress))

    # Parse season and episode from video_id (e.g. 'tt0944947:1:2') or state
    season = None
    episode = None
    vid = state.get("video_id") or ""
    if ":" in vid:
        parts = vid.split(":")
        if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
            season = int(parts[-2])
            episode = int(parts[-1])
        elif len(parts) == 2 and parts[-1].isdigit():
            episode = int(parts[-1])

    if season is None and state.get("season") is not None:
        try:
            season = int(state["season"])
        except Exception:
            pass
    if episode is None and state.get("episode") is not None:
        try:
            episode = int(state["episode"])
        except Exception:
            pass

    cw_item = dict(existing_local or {})
    cw_item.update({
        "id": r_id,
        "imdb_id": r_id if r_id.startswith("tt") else (existing_local.get("imdb_id") if existing_local else r_id),
        "title": name,
        "name": name,
        "type": remote_item.get("type") or (existing_local.get("type") if existing_local else "movie"),
        "poster": remote_item.get("poster") or (existing_local.get("poster") if existing_local else ""),
        "year": remote_item.get("year") or (existing_local.get("year") if existing_local else ""),
        "position": pos,
        "duration": dur,
        "progress": progress,
        "last_watched": int(last_w),
    })

    if season is not None:
        cw_item["season"] = season
    if episode is not None:
        cw_item["episode"] = episode

    clean_name = re.sub(r"\s*\(S\d+E\d+\)", "", name).strip()
    if season is not None and episode is not None:
        cw_item["title"] = clean_name
        cw_item["stream_title"] = f"{clean_name} (S{season:02d}E{episode:02d})"
    else:
        cw_item["title"] = clean_name

    return cw_item


def remote_to_favorite_item(remote_item):
    """Convert a Stremio libraryItem into a PopcornBox favorite item."""
    r_id = remote_item.get("_id") or ""
    name = remote_item.get("name") or "Unknown"
    return {
        "id": r_id,
        "imdb_id": r_id if r_id.startswith("tt") else r_id,
        "title": name,
        "name": name,
        "type": remote_item.get("type", "movie"),
        "poster": remote_item.get("poster", ""),
        "year": remote_item.get("year", ""),
    }


def local_to_stremio_library_item(item, is_favorite=False, in_continue_watching=False):
    """Convert a PopcornBox item (CW or favorite) into a Stremio libraryItem dictionary."""
    item_id = str(item.get("id") or item.get("imdb_id") or "")
    if not item_id:
        return None

    last_w = item.get("last_watched") or time.time()
    iso_time = epoch_to_iso(last_w)

    pos = int(round(float(item.get("position") or 0.0)))
    dur = int(round(float(item.get("duration") or 0.0)))

    season = item.get("season")
    episode = item.get("episode")
    if season is not None and episode is not None:
        vid = f"{item_id}:{season}:{episode}"
    else:
        vid = item_id

    name = item.get("title") or item.get("name") or "Unknown"
    clean_name = re.sub(r"\s*\(S\d+E\d+\)", "", name).strip()

    state = {
        "timeOffset": pos,
        "duration": dur,
        "video_id": vid,
        "lastWatched": iso_time,
        "timesWatched": 1 if pos > 0 else 0,
        "flaggedWatching": 1 if in_continue_watching else 0,
        "overallTimeOffset": pos,
    }
    if season is not None:
        state["season"] = season
    if episode is not None:
        state["episode"] = episode

    return {
        "_id": item_id,
        "name": clean_name,
        "type": item.get("type", "movie"),
        "poster": item.get("poster", "") or "",
        "posterShape": "poster",
        "year": str(item.get("year") or "") if item.get("year") else "",
        "state": state,
        "_mtime": iso_time,
        "temp": not is_favorite,
        "removed": False,
    }


# --- Two-Way Synchronization Engine ---

def sync_library(force=False):
    """
    Perform a full 2-way sync with Stremio datastore:
    1. Fetch remote libraryItems via datastoreGet
    2. Merge remote Continue Watching & Favorites into local DB (newer timestamp wins)
    3. Push local changes to Stremio via datastorePut
    """
    global _is_syncing
    if not is_sync_enabled() and not force:
        logger.debug("Stremio sync is disabled or unauthenticated")
        return {"status": "skipped", "reason": "Sync disabled"}

    auth_key = database.get_setting("stremio_auth_key")
    if not auth_key:
        return {"status": "skipped", "reason": "No auth key"}

    with _sync_lock:
        if _is_syncing:
            return {"status": "in_progress"}
        _is_syncing = True

    pulled_cw = 0
    pulled_fav = 0
    pushed_count = 0

    try:
        # Step 1: Pull remote datastore
        req_payload = {
            "authKey": auth_key,
            "collection": COLLECTION_NAME,
            "all": True,
        }
        res = _post_json(DATASTORE_GET_URL, req_payload)
        remote_items = res.get("result", []) if isinstance(res, dict) else []
        if not isinstance(remote_items, list):
            remote_items = []

        # Index remote items by _id
        remote_by_id = {}
        for r in remote_items:
            rid = r.get("_id")
            if rid:
                remote_by_id[str(rid)] = r

        # Step 2: Merge Remote -> Local
        local_cw_list = database.get_continue_watching()
        local_cw_by_id = {}
        for c in local_cw_list:
            cid = str(c.get("id") or c.get("imdb_id") or "")
            if cid:
                local_cw_by_id[cid] = c

        local_favs = database.get_favorites()
        local_fav_ids = {str(f.get("id") or f.get("imdb_id") or "") for f in local_favs if (f.get("id") or f.get("imdb_id"))}

        # Check remote items
        for rid, r_item in remote_by_id.items():
            if r_item.get("removed") is True or r_item.get("_deleted") is True:
                continue

            r_state = r_item.get("state") or {}
            r_ts = iso_to_epoch(r_state.get("lastWatched") or r_item.get("_mtime"))
            is_cw = bool(r_state.get("flaggedWatching") == 1 or (r_state.get("timeOffset", 0) > 0))

            # Merge Continue Watching
            if is_cw:
                local_existing = local_cw_by_id.get(rid)
                local_ts = float(local_existing.get("last_watched", 0.0)) if local_existing else 0.0

                if not local_existing or r_ts > local_ts:
                    cw_item = remote_to_continue_watching_item(r_item, existing_local=local_existing)
                    database.save_continue_watching(cw_item, sync_to_remote=False)
                    local_cw_by_id[rid] = cw_item
                    pulled_cw += 1

            # Merge Favorites
            is_library = (r_item.get("temp") is False)
            if is_library and rid not in local_fav_ids:
                fav_item = remote_to_favorite_item(r_item)
                database.add_favorite(fav_item, sync_to_remote=False)
                local_fav_ids.add(rid)
                pulled_fav += 1

        # Step 3: Local -> Remote (Push changes)
        changes_to_push = []

        # Push newer or missing local CW items
        fresh_local_cw = database.get_continue_watching()
        for l_cw in fresh_local_cw:
            lid = str(l_cw.get("id") or l_cw.get("imdb_id") or "")
            if not lid:
                continue
            r_match = remote_by_id.get(lid)
            l_ts = float(l_cw.get("last_watched", 0.0))

            should_push = False
            if not r_match:
                should_push = True
            else:
                r_ts = iso_to_epoch((r_match.get("state") or {}).get("lastWatched") or r_match.get("_mtime"))
                if l_ts > r_ts:
                    should_push = True

            if should_push:
                stremio_item = local_to_stremio_library_item(
                    l_cw,
                    is_favorite=(lid in local_fav_ids),
                    in_continue_watching=True,
                )
                if stremio_item:
                    changes_to_push.append(stremio_item)

        # Push local favorites that are not marked as library on remote
        fresh_local_favs = database.get_favorites()
        for l_fav in fresh_local_favs:
            fid = str(l_fav.get("id") or l_fav.get("imdb_id") or "")
            if not fid:
                continue
            r_match = remote_by_id.get(fid)
            if not r_match or r_match.get("temp") is True or r_match.get("removed") is True:
                cw_match = local_cw_by_id.get(fid)
                stremio_item = local_to_stremio_library_item(
                    cw_match or l_fav,
                    is_favorite=True,
                    in_continue_watching=bool(cw_match),
                )
                if stremio_item:
                    changes_to_push.append(stremio_item)

        # Step 4: Dispatch push if changes exist
        if changes_to_push:
            put_payload = {
                "authKey": auth_key,
                "collection": COLLECTION_NAME,
                "changes": changes_to_push,
            }
            _post_json(DATASTORE_PUT_URL, put_payload)
            pushed_count = len(changes_to_push)

        now_synced = time.time()
        database.set_setting("stremio_last_synced", now_synced)
        summary = {
            "status": "success",
            "pulled_cw": pulled_cw,
            "pulled_fav": pulled_fav,
            "pushed": pushed_count,
            "timestamp": now_synced,
        }
        _notify_sync_listeners(True, summary)
        return summary

    except Exception as e:
        logger.warning(f"Stremio sync error: {e}")
        _notify_sync_listeners(False, {"status": "error", "message": str(e)})
        raise
    finally:
        with _sync_lock:
            _is_syncing = False


def sanitize_stremio_manifest(m):
    """
    Ensure an addon manifest conforms strictly to Stremio Addon Protocol v3.
    Guarantees required fields: id, name, version, types, resources, catalogs.
    Strips internal PopcornBox runtime keys.
    """
    if not isinstance(m, dict):
        return {}

    clean = dict(m)

    # Strip internal PopcornBox runtime/cache keys
    internal_keys = [
        "manifest_url", "manifestUrl", "transportUrl", "transportName",
        "enabled", "catalogs_enabled", "streams_enabled",
        "origin_catalog", "origin_addon", "is_installed", "flags",
        "sync_to_remote"
    ]
    for k in internal_keys:
        clean.pop(k, None)

    # 1. ID
    clean_id = str(clean.get("id") or "").strip()
    if not clean_id:
        clean_id = "unknown.addon"
    clean["id"] = clean_id

    # 2. Name
    clean_name = str(clean.get("name") or "").strip()
    if not clean_name:
        clean_name = clean_id
    clean["name"] = clean_name

    # 3. Version (semver string required by stremio-core)
    clean_version = str(clean.get("version") or "").strip()
    if not clean_version:
        clean_version = "1.0.0"
    clean["version"] = clean_version

    # 4. Description
    if "description" in clean and clean["description"] is not None:
        clean["description"] = str(clean["description"])
    else:
        clean["description"] = ""

    # 5. Catalogs (must be a list)
    catalogs = clean.get("catalogs")
    if not isinstance(catalogs, list):
        catalogs = []
    clean["catalogs"] = catalogs

    # 6. Resources (must be a non-empty list of strings or dicts)
    resources = clean.get("resources")
    if not isinstance(resources, list) or len(resources) == 0:
        if clean["catalogs"]:
            resources = ["catalog", "meta"]
        else:
            resources = ["stream"]
    clean["resources"] = resources

    # 7. Types (MANDATORY non-empty list of strings for stremio-core serde)
    raw_types = clean.get("types")
    types_list = []
    if isinstance(raw_types, list):
        types_list = [str(t).strip() for t in raw_types if t and str(t).strip()]
    elif isinstance(raw_types, str) and raw_types.strip():
        types_list = [raw_types.strip()]

    if not types_list:
        found_types = set()
        for cat in clean["catalogs"]:
            if isinstance(cat, dict) and cat.get("type"):
                found_types.add(str(cat["type"]))
        for res in clean["resources"]:
            if isinstance(res, dict) and isinstance(res.get("types"), list):
                for t in res["types"]:
                    if t:
                        found_types.add(str(t))
        if found_types:
            types_list = sorted(list(found_types))
        elif any((isinstance(r, str) and r == "stream") or (isinstance(r, dict) and r.get("name") == "stream") for r in clean["resources"]):
            types_list = ["movie", "series", "anime", "other"]
        else:
            types_list = ["movie", "series"]

    clean["types"] = types_list
    return clean


def _is_manifest_dirty(raw_m, clean_m):
    """Check if raw manifest was missing required fields or had internal keys."""
    if not isinstance(raw_m, dict):
        return True
    if raw_m.get("types") != clean_m.get("types"):
        return True
    if raw_m.get("resources") != clean_m.get("resources"):
        return True
    if raw_m.get("catalogs") != clean_m.get("catalogs"):
        return True
    if any(k in raw_m for k in ["manifest_url", "enabled", "catalogs_enabled", "streams_enabled"]):
        return True
    return False


_addon_sync_lock = threading.Lock()
_is_syncing_addons = False


def sync_addons(force=False):
    """
    Two-way synchronize installed addons between PopcornBox and Stremio:
    1. Fetch remote addon collection from Stremio (addonCollectionGet).
    2. Add any missing remote addons to PopcornBox.
    3. Push any PopcornBox addons missing on remote to Stremio (addonCollectionSet).
    """
    global _is_syncing_addons
    if not is_addon_sync_enabled() and not force:
        logger.debug("Stremio addon sync is disabled or unauthenticated")
        return {"status": "skipped", "reason": "Addon sync disabled"}

    auth_key = database.get_setting("stremio_auth_key")
    if not auth_key:
        return {"status": "skipped", "reason": "No auth key"}

    with _addon_sync_lock:
        if _is_syncing_addons:
            return {"status": "in_progress"}
        _is_syncing_addons = True

    pulled_count = 0
    pushed_count = 0

    try:
        # Step 1: Pull remote addons
        req_payload = {
            "type": "AddonCollectionGet",
            "authKey": auth_key,
            "update": True,
        }
        res = _post_json(ADDON_COLLECTION_GET_URL, req_payload)
        raw_res = res.get("result") if isinstance(res, dict) else None
        if isinstance(raw_res, dict):
            remote_addons = raw_res.get("addons", [])
        elif isinstance(raw_res, list):
            remote_addons = raw_res
        else:
            remote_addons = []

        local_addons = database.get_addons()
        local_by_url = {}
        local_by_id = {}
        for a in local_addons:
            u = a.get("manifest_url")
            aid = a.get("id")
            if u:
                local_by_url[u] = a
            if aid:
                local_by_id[aid] = a

        user_removed_ids = set(database.get_setting("removed_addon_ids", []))
        user_removed_urls = set(database.get_setting("removed_addon_urls", []))

        # Step 2: Merge Remote -> Local
        remote_urls = set()
        remote_ids = set()
        for r in remote_addons:
            m = r.get("manifest") or {}
            transport_url = r.get("transportUrl") or m.get("manifest_url") or ""
            rid = m.get("id") or r.get("id")
            if transport_url:
                remote_urls.add(transport_url)
            if rid:
                remote_ids.add(rid)

            if transport_url in user_removed_urls or (rid and rid in user_removed_ids):
                continue

            in_local = (transport_url and transport_url in local_by_url) or (rid and rid in local_by_id)
            if not in_local and transport_url:
                clean_addon = sanitize_stremio_manifest(m)
                clean_addon["manifest_url"] = transport_url
                clean_addon["enabled"] = True
                database.add_addon(clean_addon, sync_to_remote=False)
                local_by_url[transport_url] = clean_addon
                if rid:
                    local_by_id[rid] = clean_addon
                pulled_count += 1

        # Step 3: Push local addons missing on remote or heal malformed manifests
        fresh_local_addons = database.get_addons()
        addons_to_push = []
        needs_push = False
        seen_pushed_urls = set()

        for r in remote_addons:
            m = r.get("manifest") or {}
            t_url = r.get("transportUrl") or m.get("manifest_url") or ""
            rid = m.get("id") or r.get("id")
            if not t_url or t_url in seen_pushed_urls:
                continue

            if t_url in user_removed_urls or (rid and rid in user_removed_ids):
                needs_push = True
                continue

            clean_m = sanitize_stremio_manifest(m)
            # If local has richer catalogs/types for this addon, merge them
            loc = local_by_url.get(t_url) or (local_by_id.get(rid) if rid else None)
            if loc:
                if not clean_m.get("catalogs") and loc.get("catalogs"):
                    clean_m["catalogs"] = loc["catalogs"]
                if clean_m.get("types") == ["movie", "series"] and loc.get("types") and loc.get("types") != ["movie", "series"]:
                    clean_m["types"] = loc["types"]

            if _is_manifest_dirty(m, clean_m):
                needs_push = True

            flags = r.get("flags")
            if not isinstance(flags, dict):
                flags = {"official": False, "protected": False}

            addons_to_push.append({
                "transportUrl": t_url,
                "transportName": r.get("transportName") or "http",
                "manifest": clean_m,
                "flags": flags
            })
            seen_pushed_urls.add(t_url)

        for l_addon in fresh_local_addons:
            m_url = l_addon.get("manifest_url")
            l_id = l_addon.get("id")
            if not m_url or m_url in seen_pushed_urls:
                continue
            if m_url in user_removed_urls or (l_id and l_id in user_removed_ids):
                continue
            needs_push = True
            clean_m = sanitize_stremio_manifest(l_addon)
            addons_to_push.append({
                "transportUrl": m_url,
                "transportName": "http",
                "manifest": clean_m,
                "flags": {"official": False, "protected": False}
            })
            seen_pushed_urls.add(m_url)

        if needs_push:
            set_payload = {
                "type": "AddonCollectionSet",
                "authKey": auth_key,
                "addons": addons_to_push,
            }
            _post_json(ADDON_COLLECTION_SET_URL, set_payload)
            pushed_count = len(addons_to_push)

        summary = {
            "status": "success",
            "pulled_addons": pulled_count,
            "pushed_addons": pushed_count,
            "timestamp": time.time(),
        }
        return summary
    except Exception as e:
        logger.warning(f"Stremio addon sync error: {e}")
        raise
    finally:
        with _addon_sync_lock:
            _is_syncing_addons = False


def sync_addons_in_background(callback=None):
    """Run sync_addons in a background daemon thread."""
    def _worker():
        try:
            res = sync_addons()
            if callback:
                try:
                    import gi
                    gi.require_version("GLib", "2.0")
                    from gi.repository import GLib
                    GLib.idle_add(callback, res, None)
                except Exception:
                    callback(res, None)
        except Exception as err:
            if callback:
                try:
                    import gi
                    gi.require_version("GLib", "2.0")
                    from gi.repository import GLib
                    GLib.idle_add(callback, None, err)
                except Exception:
                    callback(None, err)

    thread = threading.Thread(target=_worker, daemon=True, name="StremioAddonSyncWorker")
    thread.start()
    return thread


def sync_in_background(callback=None):
    """Run full sync (library and addons if enabled) in a background daemon thread."""
    def _worker():
        lib_res = None
        addon_res = None
        first_err = None
        try:
            if is_sync_enabled():
                lib_res = sync_library()
        except Exception as err:
            first_err = err
            logger.warning(f"Library sync error: {err}")

        try:
            if is_addon_sync_enabled():
                addon_res = sync_addons()
        except Exception as err:
            if not first_err:
                first_err = err
            logger.warning(f"Addon sync error: {err}")

        combined = {
            "status": "success" if not first_err else "partial_error",
            "library": lib_res,
            "addons": addon_res,
        }
        if callback:
            try:
                import gi
                gi.require_version("GLib", "2.0")
                from gi.repository import GLib
                GLib.idle_add(callback, combined, first_err)
            except Exception:
                callback(combined, first_err)

    thread = threading.Thread(target=_worker, daemon=True, name="StremioSyncWorker")
    thread.start()
    return thread


# --- Realtime Playback Scrobbling ---

_scrobble_lock = threading.Lock()
_last_scrobbles = {}


def scrobble_playback(item):
    """
    Push playback state to Stremio asynchronously when a video updates/stops.
    Never blocks the GTK main thread. Debounces rapid duplicate updates.
    """
    if not is_sync_enabled():
        return

    auth_key = database.get_setting("stremio_auth_key")
    if not auth_key:
        return

    item_id = str(item.get("id") or item.get("imdb_id") or "")
    if not item_id:
        return

    pos = float(item.get("position") or 0.0)
    dur = float(item.get("duration") or 0.0)
    season = item.get("season")
    episode = item.get("episode")
    now = time.time()

    # Debounce identical progress calls within 5 seconds
    key = f"{item_id}:{season}:{episode}"
    with _scrobble_lock:
        last_t, last_pos = _last_scrobbles.get(key, (0, -999))
        if (now - last_t < 4.0) and abs(pos - last_pos) < 2.0:
            return
        _last_scrobbles[key] = (now, pos)

    is_fav = database.is_favorite(item_id)
    lib_item = local_to_stremio_library_item(
        item,
        is_favorite=is_fav,
        in_continue_watching=True,
    )
    if not lib_item:
        return

    def _push_worker():
        try:
            payload = {
                "authKey": auth_key,
                "collection": COLLECTION_NAME,
                "changes": [lib_item],
            }
            _post_json(DATASTORE_PUT_URL, payload, timeout=5.0)
            logger.debug(f"Scrobbled '{key}' to Stremio at {pos:.1f}s")
        except Exception as e:
            logger.debug(f"Failed to scrobble playback to Stremio: {e}")

    threading.Thread(target=_push_worker, daemon=True, name="StremioScrobbleWorker").start()
