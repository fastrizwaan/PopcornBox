# src/backup.py
#
# Backup and Restore configuration manager for PopcornBox.
# Backs up and restores:
#   - Stremio Addons (installed addons, custom URLs, manifests, default addon state)
#   - Nuvio Plugins (repositories, scrapers, settings, enabled state)
#   - CloudStream (repositories, plugins, installed/enabled status, and .cs3 packages)
#   - Key playback/scraper settings (timeout, retries, adult filtering)

import datetime
import json
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

logger = logging.getLogger("PopcornBox.Backup")

BACKUP_FORMAT_NAME = "popcornbox_backup"
BACKUP_FORMAT_VERSION = 1


def get_default_backup_filename():
    """Generate a default backup file name with current timestamp."""
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"PopcornBox_Backup_{now_str}.zip"


def create_backup_data():
    """
    Gather all addons, plugins, CloudStream repositories, and settings
    into a clean dictionary.
    """
    from . import database

    addons = database.get_addons()
    nuvio_repos = database.get_plugin_repositories()
    nuvio_scrapers = database.get_plugin_scrapers()
    nuvio_settings = database.get_setting("plugins_scraper_settings", {}) or {}
    nuvio_enabled = database.get_setting("plugins_enabled", True)
    nuvio_group_by = database.get_setting("plugins_group_by_repository", False)

    cs_repos = database.get_cloudstream_repositories()
    cs_plugins = database.get_cloudstream_plugins()
    cs_enabled = database.get_setting("cloudstream_enabled", True)
    cs_sec_accepted = database.get_setting("cloudstream_security_accepted", True)

    installed_cs_ids = [p["id"] for p in cs_plugins if p.get("is_installed")]
    enabled_cs_ids = [p["id"] for p in cs_plugins if p.get("enabled")]

    settings_to_backup = {
        "hide_adult_content": database.get_setting("hide_adult_content", False),
        "stream_provider_timeout": database.get_setting("stream_provider_timeout", 20),
        "stream_provider_retries": database.get_setting("stream_provider_retries", 1),
        "removed_default_addons": database.get_setting("removed_default_addons", []),
        "show_movies_button": database.get_setting("show_movies_button", True),
        "show_series_button": database.get_setting("show_series_button", True),
        "show_anime_button": database.get_setting("show_anime_button", True),
    }

    payload = {
        "format": BACKUP_FORMAT_NAME,
        "version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "app_version": "2.3",
        "addons": addons,
        "nuvio": {
            "enabled": bool(nuvio_enabled),
            "group_by_repository": bool(nuvio_group_by),
            "repositories": nuvio_repos,
            "scrapers": nuvio_scrapers,
            "scraper_settings": nuvio_settings,
        },
        "cloudstream": {
            "enabled": bool(cs_enabled),
            "security_accepted": bool(cs_sec_accepted),
            "repositories": cs_repos,
            "plugins": cs_plugins,
            "installed_plugin_ids": installed_cs_ids,
            "enabled_plugin_ids": enabled_cs_ids,
        },
        "settings": settings_to_backup,
    }
    return payload


def create_backup(output_path):
    """
    Create a backup archive (.zip) or JSON file at output_path.
    If output_path ends with .zip, packages .cs3 files into the archive as well.
    Returns (success: bool, message: str, summary: dict).
    """
    from . import cloudstream

    try:
        data = create_backup_data()
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        is_zip = str(output_path).lower().endswith(".zip")
        if is_zip:
            pkg_dir = cloudstream.get_packages_dir()
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                # Add JSON metadata
                json_str = json.dumps(data, indent=2, ensure_ascii=False)
                zf.writestr("backup.json", json_str)

                # Add installed CloudStream .cs3 packages
                cs_plugins = data.get("cloudstream", {}).get("plugins", [])
                included_pkgs = 0
                if pkg_dir.exists():
                    for p in cs_plugins:
                        if not p.get("is_installed"):
                            continue
                        sk = p.get("storage_key")
                        if not sk:
                            continue
                        pkg_file = pkg_dir / f"{sk}.cs3"
                        if pkg_file.is_file():
                            zf.write(pkg_file, arcname=f"cloudstream_packages/{sk}.cs3")
                            included_pkgs += 1

            logger.info(f"Created ZIP backup at {output_path} with {included_pkgs} packages")
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Created JSON backup at {output_path}")

        summary = {
            "addons_count": len(data.get("addons", [])),
            "nuvio_repos_count": len(data.get("nuvio", {}).get("repositories", [])),
            "nuvio_scrapers_count": len(data.get("nuvio", {}).get("scrapers", [])),
            "cloudstream_repos_count": len(data.get("cloudstream", {}).get("repositories", [])),
            "cloudstream_installed_count": len(data.get("cloudstream", {}).get("installed_plugin_ids", [])),
            "path": str(output_path),
            "is_zip": is_zip,
        }
        msg = (
            f"Backup created: {summary['addons_count']} addons, "
            f"{summary['nuvio_repos_count']} Nuvio repos, "
            f"{summary['cloudstream_repos_count']} CloudStream repos "
            f"({summary['cloudstream_installed_count']} installed)"
        )
        return True, msg, summary
    except Exception as e:
        logger.error(f"Failed to create backup: {e}", exc_info=True)
        return False, f"Backup failed: {e}", {}


def restore_backup(input_path):
    """
    Restore addons, plugins, CloudStream repositories, and settings from
    a backup (.zip or .json).
    Returns (success: bool, message: str, summary: dict).
    """
    from . import database, cloudstream

    in_path = Path(input_path)
    if not in_path.is_file():
        return False, f"File not found: {input_path}", {}

    try:
        payload = None
        extracted_pkgs = 0

        if zipfile.is_zipfile(input_path):
            with zipfile.ZipFile(input_path, "r") as zf:
                # Find the backup JSON file in zip
                target_json = None
                for name in zf.namelist():
                    if name.endswith("backup.json") or (name.endswith(".json") and not target_json):
                        target_json = name

                if not target_json:
                    return False, "Invalid backup: no backup.json found inside zip archive", {}

                with zf.open(target_json) as jf:
                    payload = json.loads(jf.read().decode("utf-8"))

                # Extract cloudstream packages
                pkg_dir = cloudstream.get_packages_dir()
                pkg_dir.mkdir(parents=True, exist_ok=True)
                for name in zf.namelist():
                    if name.startswith("cloudstream_packages/") and name.endswith(".cs3"):
                        base_name = os.path.basename(name)
                        if base_name:
                            dest_file = pkg_dir / base_name
                            with zf.open(name) as src_pkg, open(dest_file, "wb") as dst_pkg:
                                shutil.copyfileobj(src_pkg, dst_pkg)
                            extracted_pkgs += 1
            logger.info(f"Restored {extracted_pkgs} CloudStream package(s) from ZIP archive")
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

        # 3. Restore CloudStream
        cs = payload.get("cloudstream", {})
        restored_cs_repos = 0
        restored_cs_plugins = 0
        installed_cs_count = 0
        if isinstance(cs, dict):
            for r in cs.get("repositories", []):
                if isinstance(r, dict) and r.get("manifest_url"):
                    database.save_cloudstream_repository(r)
                    restored_cs_repos += 1

            cs_plugins = cs.get("plugins", [])
            if isinstance(cs_plugins, list) and cs_plugins:
                database.save_cloudstream_plugins(cs_plugins)
                restored_cs_plugins = len(cs_plugins)

            installed_ids = set(cs.get("installed_plugin_ids", []))
            enabled_ids = set(cs.get("enabled_plugin_ids", []))

            # Mark installed / enabled states for restored plugins
            all_db_plugins = database.get_cloudstream_plugins()
            for p in all_db_plugins:
                p_id = p.get("id")
                was_installed = p_id in installed_ids or p.get("is_installed", False)
                was_enabled = p_id in enabled_ids or p.get("enabled", False)
                if was_installed:
                    installed_cs_count += 1
                    sk = p.get("storage_key")
                    has_pkg = cloudstream.package_exists(sk) if sk else False
                    database.update_cloudstream_plugin(p_id, {
                        "is_installed": True,
                        "enabled": bool(was_enabled),
                        "installed_version": p.get("version", p.get("installed_version")),
                    })
                    # If package is missing on disk, download it in background
                    if not has_pkg and p.get("package_url"):
                        try:
                            threading.Thread(
                                target=cloudstream.install_plugin,
                                args=(p_id,),
                                daemon=True,
                                name=f"CS-Restore-Install-{p_id}",
                            ).start()
                        except Exception as e:
                            logger.warning(f"Failed to queue background install for {p_id}: {e}")

            if "enabled" in cs:
                database.set_setting("cloudstream_enabled", bool(cs["enabled"]))
            database.set_setting("cloudstream_security_accepted", True)

        # 4. Restore settings
        settings = payload.get("settings", {})
        if isinstance(settings, dict):
            for k in ("hide_adult_content", "stream_provider_timeout", "stream_provider_retries",
                      "show_movies_button", "show_series_button", "show_anime_button"):
                if k in settings:
                    database.set_setting(k, settings[k])
            if "removed_default_addons" in settings:
                database.set_setting("removed_default_addons", settings["removed_default_addons"])

        # 5. Clear stream & catalog caches
        database.clear_all_cached_streams_async()
        database.clear_all_cached_catalogs()

        summary = {
            "addons_count": restored_addons,
            "nuvio_repos_count": restored_nuvio_repos,
            "nuvio_scrapers_count": restored_nuvio_scrapers,
            "cloudstream_repos_count": restored_cs_repos,
            "cloudstream_installed_count": installed_cs_count,
            "extracted_packages_count": extracted_pkgs,
        }
        msg = (
            f"Restored {restored_addons} addons, {restored_nuvio_repos} Nuvio repos, "
            f"and {restored_cs_repos} CloudStream repos ({installed_cs_count} installed plugins)"
        )
        logger.info(f"Restore complete: {msg}")
        return True, msg, summary
    except Exception as e:
        logger.error(f"Failed to restore backup: {e}", exc_info=True)
        return False, f"Restore failed: {e}", {}
