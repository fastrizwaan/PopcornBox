# src/backup.py
#
# Backup and Restore configuration manager for PopcornBox.
# Backs up and restores:
#   - Stremio Addons (installed addons, custom URLs, manifests, default addon state)
#   - Nuvio Plugins (repositories, scrapers, settings, enabled state)
#   - Library data: Favorites, Watched items, Watch history, Continue Watching
#   - Downloads queue and history
#   - User playback preferences, stream info, and application settings

import datetime
import json
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger("PopcornBox.Backup")

BACKUP_FORMAT_NAME = "popcornbox_backup"
BACKUP_FORMAT_VERSION = 2


def get_default_backup_filename():
    """Generate a default backup file name with current timestamp."""
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"PopcornBox_Backup_{now_str}.zip"


def create_backup_data():
    """
    Gather all addons, plugins, library data (favorites,
    watched, history, continue watching), downloads, and settings into a clean dictionary.
    """
    from . import database

    addons = database.get_addons()
    nuvio_repos = database.get_plugin_repositories()
    nuvio_scrapers = database.get_plugin_scrapers()
    nuvio_settings = database.get_setting("plugins_scraper_settings", {}) or {}
    nuvio_enabled = database.get_setting("plugins_enabled", True)
    nuvio_group_by = database.get_setting("plugins_group_by_repository", False)

    favorites = database.get_favorites()
    watched = database.get_watched()
    history = database.get_history()
    continue_watching = database.get_continue_watching()
    dismissed_cw = list(database.get_continue_watching_dismissals())
    downloads = database.get_downloads()

    # Get all settings excluding sensitive keys (like Stremio session auth tokens)
    all_settings = database.get_all_settings()
    sensitive_keys = {"stremio_auth_key"}
    settings_to_backup = {k: v for k, v in all_settings.items() if k not in sensitive_keys}

    # Ensure baseline settings keys are explicitly present with defaults
    baseline_defaults = {
        "hide_adult_content": False,
        "stream_provider_timeout": 20,
        "stream_provider_retries": 1,
        "removed_default_addons": [],
        "show_movies_button": True,
        "show_series_button": True,
        "show_anime_button": True,
    }
    for k, v in baseline_defaults.items():
        if k not in settings_to_backup:
            settings_to_backup[k] = database.get_setting(k, v)

    payload = {
        "format": BACKUP_FORMAT_NAME,
        "version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "app_version": "2.5",
        "addons": addons,
        "nuvio": {
            "enabled": bool(nuvio_enabled),
            "group_by_repository": bool(nuvio_group_by),
            "repositories": nuvio_repos,
            "scrapers": nuvio_scrapers,
            "scraper_settings": nuvio_settings,
        },
        "favorites": favorites,
        "watched": watched,
        "history": history,
        "continue_watching": continue_watching,
        "user_dismissed_continue_watching": dismissed_cw,
        "downloads": downloads,
        "settings": settings_to_backup,
    }
    return payload


def create_backup(output_path):
    """
    Create a backup archive (.zip) or JSON file at output_path.
    Returns (success: bool, message: str, summary: dict).
    """
    try:
        data = create_backup_data()
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        is_zip = str(output_path).lower().endswith(".zip")
        if is_zip:
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                json_str = json.dumps(data, indent=2, ensure_ascii=False)
                zf.writestr("backup.json", json_str)
            logger.info(f"Created ZIP backup at {output_path}")
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Created JSON backup at {output_path}")

        summary = {
            "addons_count": len(data.get("addons", [])),
            "nuvio_repos_count": len(data.get("nuvio", {}).get("repositories", [])),
            "nuvio_scrapers_count": len(data.get("nuvio", {}).get("scrapers", [])),
            "favorites_count": len(data.get("favorites", [])),
            "watched_count": len(data.get("watched", [])),
            "history_count": len(data.get("history", [])),
            "continue_watching_count": len(data.get("continue_watching", [])),
            "downloads_count": len(data.get("downloads", [])),
            "path": str(output_path),
            "is_zip": is_zip,
        }

        parts = [f"{summary['addons_count']} addons"]
        if summary['favorites_count']:
            parts.append(f"{summary['favorites_count']} favorites")
        if summary['continue_watching_count']:
            parts.append(f"{summary['continue_watching_count']} continue watching")
        if summary['history_count']:
            parts.append(f"{summary['history_count']} history")
        if summary['watched_count']:
            parts.append(f"{summary['watched_count']} watched")
        if summary['nuvio_repos_count']:
            parts.append(f"{summary['nuvio_repos_count']} Nuvio repos")
        msg = f"Backup created: {', '.join(parts)}"
        return True, msg, summary
    except Exception as e:
        logger.error(f"Failed to create backup: {e}", exc_info=True)
        return False, f"Backup failed: {e}", {}


def restore_backup(input_path):
    """
    Restore addons, plugins, library data (favorites,
    watched, history, continue watching), downloads, and settings from
    a backup (.zip or .json).
    Returns (success: bool, message: str, summary: dict).
    """
    from . import database

    in_path = Path(input_path)
    if not in_path.is_file():
        return False, f"File not found: {input_path}", {}

    try:
        payload = None

        if zipfile.is_zipfile(input_path):
            with zipfile.ZipFile(input_path, "r") as zf:
                target_json = None
                for name in zf.namelist():
                    if name.endswith("backup.json") or (name.endswith(".json") and not target_json):
                        target_json = name

                if not target_json:
                    return False, "Invalid backup: no backup.json found inside zip archive", {}

                with zf.open(target_json) as jf:
                    payload = json.loads(jf.read().decode("utf-8"))
        else:
            with open(input_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

        if not payload:
            return False, "Backup file is empty or invalid", {}

        # Handle legacy raw Stremio addons list export
        if isinstance(payload, list):
            payload = {"addons": payload}

        # 1. Restore Addons
        addons = payload.get("addons", [])
        restored_addons = 0
        if isinstance(addons, list):
            for a in addons:
                if isinstance(a, dict) and (a.get("manifest_url") or a.get("id")):
                    database.add_addon(a, sync_to_remote=False)
                    restored_addons += 1

        # 2. Restore Nuvio Plugins
        nuvio = payload.get("nuvio", {})
        restored_nuvio_repos = 0
        restored_nuvio_scrapers = 0
        if isinstance(nuvio, dict):
            for r in nuvio.get("repositories", []):
                if isinstance(r, dict) and r.get("manifest_url"):
                    database.save_plugin_repository(r)
                    restored_nuvio_repos += 1

            scrapers = nuvio.get("scrapers", [])
            if isinstance(scrapers, list) and scrapers:
                database.save_plugin_scrapers(scrapers)
                restored_nuvio_scrapers = len(scrapers)

            scraper_settings = nuvio.get("scraper_settings")
            if isinstance(scraper_settings, dict):
                database.set_setting("plugins_scraper_settings", scraper_settings)

            if "enabled" in nuvio:
                database.set_setting("plugins_enabled", bool(nuvio["enabled"]))
            if "group_by_repository" in nuvio:
                database.set_setting("plugins_group_by_repository", bool(nuvio["group_by_repository"]))

        # 3. Restore settings
        settings = payload.get("settings", {})
        if isinstance(settings, dict):
            safe_settings = {k: v for k, v in settings.items() if k != "stremio_auth_key"}
            database.set_settings_batch(safe_settings)

        # 5. Restore Favorites
        favorites = payload.get("favorites", [])
        restored_favorites = 0
        if isinstance(favorites, list) and favorites:
            restored_favorites = database.add_favorites_batch(favorites, sync_to_remote=False)

        # 6. Restore Watched
        watched = payload.get("watched", [])
        restored_watched = 0
        if isinstance(watched, list) and watched:
            restored_watched = database.add_watched_batch(watched)

        # 7. Restore History
        history = payload.get("history", [])
        restored_history = 0
        if isinstance(history, list) and history:
            restored_history = database.add_history_batch(history, sync_to_remote=False)

        # 8. Restore Continue Watching
        cw = payload.get("continue_watching", [])
        restored_cw = 0
        if isinstance(cw, list) and cw:
            restored_cw = database.add_continue_watching_batch(cw, sync_to_remote=False)

        # 9. Restore user dismissed continue watching
        dismissed = payload.get("user_dismissed_continue_watching", [])
        if isinstance(dismissed, list) and dismissed:
            restored_cw_ids = set()
            for it in (cw if isinstance(cw, list) else []):
                restored_cw_ids.update(database._continue_watching_item_ids(it))
            valid_dismissals = [d for d in dismissed if str(d) not in restored_cw_ids]
            if valid_dismissals:
                database.add_continue_watching_dismissals_batch(valid_dismissals)

        # 10. Restore Downloads
        downloads = payload.get("downloads", [])
        restored_downloads = 0
        if isinstance(downloads, list) and downloads:
            restored_downloads = database.add_downloads_batch(downloads)

        # 11. Clear stream & catalog caches
        database.clear_all_cached_streams_async()
        database.clear_all_cached_catalogs()

        summary = {
            "addons_count": restored_addons,
            "nuvio_repos_count": restored_nuvio_repos,
            "nuvio_scrapers_count": restored_nuvio_scrapers,
            "favorites_count": restored_favorites,
            "watched_count": restored_watched,
            "history_count": restored_history,
            "continue_watching_count": restored_cw,
            "downloads_count": restored_downloads,
        }

        parts = [f"{restored_addons} addons"]
        if restored_favorites:
            parts.append(f"{restored_favorites} favorites")
        if restored_cw:
            parts.append(f"{restored_cw} continue watching")
        if restored_history:
            parts.append(f"{restored_history} history")
        if restored_watched:
            parts.append(f"{restored_watched} watched")
        if restored_nuvio_repos:
            parts.append(f"{restored_nuvio_repos} Nuvio repos")
        msg = f"Restored {', '.join(parts)}"
        logger.info(f"Restore complete: {msg}")
        return True, msg, summary
    except Exception as e:
        logger.error(f"Failed to restore backup: {e}", exc_info=True)
        return False, f"Restore failed: {e}", {}
