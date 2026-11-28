"""
HTTP / Direct Stream Downloader for PopcornBox.
Handles downloading direct HTTP/HTTPS/HLS video streams to PopcornBox's sandboxed
downloads directory in background threads, with progress tracking, pause/resume,
and cancellation.
"""

import os
import re
import sys
import time
import shutil
import hashlib
import logging
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from gi.repository import GLib

from . import database
from . import api

logger = logging.getLogger("PopcornBox.HttpDownloader")

_downloads_lock = threading.Lock()
_active_downloads = {}


def get_downloads_dir():
    """Return the sandboxed downloads directory inside PopcornBox's data dir."""
    d = os.path.join(str(database.BASE_DIR), "downloads")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def get_http_download_stats(info_hash):
    """
    Return statistics for an HTTP download matching the shape expected by DownloadItemRow.
    Returns:
        dict with keys: name, filePath, downloaded, totalLength, progress,
        downloadSpeed, uploadSpeed, status ("downloading"|"finished"|"paused"|"failed"), error
        or None if not found.
    """
    with _downloads_lock:
        task = _active_downloads.get(info_hash)
        if task:
            tot = task.get("total_length", 0) or 0
            dl = task.get("downloaded", 0) or 0
            prog = (dl / tot) if tot > 0 else 0.0
            return {
                "name": task.get("name"),
                "filePath": task.get("file_path"),
                "downloaded": dl,
                "totalLength": tot,
                "progress": prog,
                "downloadSpeed": task.get("speed", 0.0),
                "uploadSpeed": 0.0,
                "status": task.get("status", "downloading"),
                "error": task.get("error"),
            }

    # If not active in memory, check database and disk
    for d in database.get_downloads():
        if d.get("info_hash") == info_hash:
            file_path = d.get("file_path")
            title = d.get("name") or "Download"
            if file_path and os.path.exists(file_path):
                size = os.path.getsize(file_path)
                return {
                    "name": title,
                    "filePath": file_path,
                    "downloaded": size,
                    "totalLength": size,
                    "progress": 1.0,
                    "downloadSpeed": 0.0,
                    "uploadSpeed": 0.0,
                    "status": "finished",
                    "error": None,
                }
            elif file_path and os.path.exists(file_path + ".part"):
                size = os.path.getsize(file_path + ".part")
                tot = d.get("total_bytes") or 0
                prog = (size / tot) if tot > 0 else 0.0
                return {
                    "name": title,
                    "filePath": file_path + ".part",
                    "downloaded": size,
                    "totalLength": tot,
                    "progress": prog,
                    "downloadSpeed": 0.0,
                    "uploadSpeed": 0.0,
                    "status": "paused",
                    "error": None,
                }
            else:
                return {
                    "name": title,
                    "filePath": file_path or "",
                    "downloaded": d.get("downloaded_bytes", 0),
                    "totalLength": d.get("total_bytes", 0),
                    "progress": 0.0,
                    "downloadSpeed": 0.0,
                    "uploadSpeed": 0.0,
                    "status": d.get("status") or "not_downloaded",
                    "error": None,
                }
    return None


def sanitize_stream_url(url):
    """Sanitize stream URL by removing surrounding quotes and unescaping HTML entities."""
    if not url or not isinstance(url, str):
        return url
    url = url.strip().strip("'\"")
    url = url.replace("&amp;", "&")
    return url


def generate_clean_filename(torrent, clean_title, url):
    """Compute a safe, sanitized filename with proper video extension."""
    raw_fn = str((torrent.get("filename") if isinstance(torrent, dict) else "") or "").strip()
    raw_base, raw_ext = os.path.splitext(raw_fn)
    raw_ext = raw_ext.lower()
    valid_exts = (".mp4", ".mkv", ".webm", ".avi", ".ts", ".mov", ".m4v")

    if raw_ext in valid_exts and len(raw_fn) > len(raw_ext) and not any(tag in raw_fn for tag in ("Download [", "Server :", "10Gbps")):
        filename = raw_fn
    else:
        url_lower = (url or "").lower()
        torrent_title = str(torrent.get("stream_title", "") if isinstance(torrent, dict) else "")
        if ".mkv" in url_lower or "mkv" in raw_fn.lower() or "mkv" in torrent_title.lower():
            ext = ".mkv"
        elif ".webm" in url_lower:
            ext = ".webm"
        elif ".ts" in url_lower:
            ext = ".ts"
        elif ".m3u8" in url_lower:
            ext = ".mp4"
        else:
            ext = ".mp4"
        filename = f"{clean_title}{ext}"

    filename = re.sub(r'[\\/*?:"<>|]', "", filename).strip() or "video.mp4"
    return filename


def start_http_download(url, torrent=None, headers=None, clean_title=None, item_id=None, media_type=None, season=None, episode=None, window=None):
    """Initiate an HTTP download and record it in database."""
    url = sanitize_stream_url(url)
    if not url:
        return None
    # Stream URLs come from addons and scrapers; urllib and FFmpeg would also
    # read file:// and other local schemes, so only remote HTTP(S) downloads.
    if not api._is_remote_http_url(url):
        logger.warning("Refusing to download non-HTTP stream URL")
        return None

    if not clean_title:
        clean_title = "video"
        if season is not None and episode is not None:
            try:
                clean_title = f"{clean_title} S{int(season):02d}E{int(episode):02d}"
            except (ValueError, TypeError):
                clean_title = f"{clean_title} S{season}E{episode}"

    filename = generate_clean_filename(torrent or {}, clean_title, url)
    download_dir = get_downloads_dir()

    base_name, ext = os.path.splitext(filename)
    dest_path = os.path.join(download_dir, filename)
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(download_dir, f"{base_name} ({counter}){ext}")
        counter += 1

    info_hash = "http_" + hashlib.sha1((url + dest_path).encode("utf-8")).hexdigest()[:16]
    disp_name = os.path.basename(dest_path)

    database.add_download(
        info_hash=info_hash,
        name=clean_title or disp_name,
        magnet="",
        file_index=None,
        item_id=item_id,
        media_type=media_type,
        season=season,
        episode=episode,
        is_http=True,
        url=url,
        file_path=dest_path,
        status="downloading",
    )

    if window and hasattr(window, "_show_toast"):
        window._show_toast(f"Downloading {disp_name} in background...", timeout=6)

    _start_worker_thread(info_hash, url, dest_path, headers=headers, torrent=torrent, clean_title=clean_title, window=window)
    return info_hash


def _start_worker_thread(info_hash, url, dest_path, headers=None, torrent=None, clean_title=None, window=None):
    all_headers = {}
    if headers:
        all_headers.update(headers)
    elif isinstance(torrent, dict):
        all_headers.update(api.extract_stream_headers(torrent, url=url))

    if isinstance(url, str):
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            for k_target in ["User-Agent", "Referer", "Origin", "Cookie"]:
                for p_key, p_val in params.items():
                    if p_key.lower() == k_target.lower() and p_val:
                        all_headers[k_target] = p_val[0]
        except Exception:
            pass

    if not any(k.lower() == "user-agent" for k in all_headers):
        all_headers["User-Agent"] = api.DEFAULT_BROWSER_USER_AGENT

    abort_event = threading.Event()
    with _downloads_lock:
        _active_downloads[info_hash] = {
            "abort_event": abort_event,
            "url": url,
            "file_path": dest_path,
            "downloaded": 0,
            "total_length": 0,
            "speed": 0.0,
            "status": "downloading",
            "error": None,
            "name": clean_title or os.path.basename(dest_path),
        }

    def _worker():
        task = _active_downloads.get(info_hash)
        part_path = dest_path + ".part"
        target_dest_path = dest_path
        disp_name = os.path.basename(dest_path)
        try:
            ssl_ctx = getattr(api, "_SSL_CONTEXT", None)
            req_headers = dict(all_headers)

            initial_bytes = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if initial_bytes > 0:
                req_headers["Range"] = f"bytes={initial_bytes}-"

            req = urllib.request.Request(url, headers=req_headers)
            try:
                resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
            except urllib.error.HTTPError as he:
                if he.code == 416 and initial_bytes > 0:
                    # Range not satisfiable; reset and retry full download
                    req_headers.pop("Range", None)
                    initial_bytes = 0
                    req = urllib.request.Request(url, headers=req_headers)
                    resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
                elif he.code == 428 and any(k.lower() == 'user-agent' for k in req_headers):
                    req_headers = {k: v for k, v in req_headers.items() if k.lower() != 'user-agent'}
                    req = urllib.request.Request(url, headers=req_headers)
                    resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
                elif he.code in (401, 403):
                    req_headers['User-Agent'] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    req = urllib.request.Request(url, headers=req_headers)
                    resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
                else:
                    raise

            content_type = (resp.headers.get("Content-Type") or "").lower()
            final_url = resp.geturl() or url

            # Align extension with server Content-Type if necessary
            if ("matroska" in content_type or "mkv" in content_type) and target_dest_path.endswith(".mp4"):
                target_dest_path = target_dest_path[:-4] + ".mkv"
                part_path = target_dest_path + ".part"
                disp_name = os.path.basename(target_dest_path)
            elif ("mp4" in content_type) and target_dest_path.endswith(".mkv"):
                target_dest_path = target_dest_path[:-4] + ".mp4"
                part_path = target_dest_path + ".part"
                disp_name = os.path.basename(target_dest_path)

            if task:
                task["file_path"] = target_dest_path

            if ".m3u8" in final_url.lower() or "mpegurl" in content_type:
                resp.close()
                if shutil.which("ffmpeg"):
                    headers_arg = "".join(f"{k}: {v}\r\n" for k, v in req_headers.items() if k.lower() != "range")
                    cmd = [
                        "ffmpeg", "-y",
                        "-protocol_whitelist", "http,https,httpproxy,tcp,tls,crypto",
                        "-headers", headers_arg,
                        "-i", final_url,
                        "-c", "copy",
                        "-bsf:a", "aac_adtstoasc",
                        part_path
                    ]
                    # Keep stderr out of an unread pipe: once the pipe fills,
                    # FFmpeg blocks while the worker waits for it to exit.
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    while proc.poll() is None:
                        if abort_event.is_set():
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                try:
                                    proc.wait(timeout=5)
                                except Exception:
                                    pass
                            break
                        if os.path.exists(part_path) and task:
                            task["downloaded"] = os.path.getsize(part_path)
                        time.sleep(0.5)

                    if abort_event.is_set():
                        if task:
                            task["status"] = "paused"
                        database.update_http_download_status(info_hash, status="paused", file_path=target_dest_path)
                        return

                    if proc.returncode != 0:
                        raise RuntimeError(f"FFmpeg exited with status {proc.returncode}")
                else:
                    raise RuntimeError("HLS (.m3u8) stream requires ffmpeg to download")
            else:
                # Direct HTTP stream
                content_range = resp.headers.get("Content-Range")
                content_length = resp.headers.get("Content-Length")
                total_length = 0
                if content_range:
                    # bytes start-end/total
                    m = re.search(r'/(\d+)', content_range)
                    if m:
                        total_length = int(m.group(1))
                elif content_length:
                    total_length = initial_bytes + int(content_length)

                if task:
                    task["total_length"] = total_length

                mode = "ab" if (initial_bytes > 0 and resp.status == 206) else "wb"
                downloaded = initial_bytes if mode == "ab" else 0

                chunk_size = 256 * 1024
                speed_window_start = time.time()
                bytes_since_window = 0

                with resp, open(part_path, mode) as out_f:
                    while not abort_event.is_set():
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded += len(chunk)
                        bytes_since_window += len(chunk)
                        now = time.time()
                        elapsed = now - speed_window_start
                        if elapsed >= 0.5:
                            if task:
                                task["speed"] = bytes_since_window / elapsed
                                task["downloaded"] = downloaded
                            bytes_since_window = 0
                            speed_window_start = now

                if abort_event.is_set():
                    if task:
                        task["status"] = "paused"
                        task["downloaded"] = downloaded
                    database.update_http_download_status(
                        info_hash,
                        status="paused",
                        file_path=target_dest_path,
                        downloaded_bytes=downloaded,
                        total_bytes=total_length
                    )
                    return

            if os.path.exists(part_path):
                if os.path.exists(target_dest_path):
                    os.remove(target_dest_path)
                os.rename(part_path, target_dest_path)

            final_size = os.path.getsize(target_dest_path) if os.path.exists(target_dest_path) else 0
            if task:
                task["status"] = "finished"
                task["downloaded"] = final_size
                task["total_length"] = final_size
                task["speed"] = 0.0

            database.update_http_download_status(
                info_hash,
                status="finished",
                file_path=target_dest_path,
                downloaded_bytes=final_size,
                total_bytes=final_size
            )

            logger.info(f"HTTP download completed: {target_dest_path}")
            if window and hasattr(window, "_show_toast"):
                GLib.idle_add(lambda: window._show_toast(f"Download complete: {disp_name}", timeout=6))

        except Exception as e:
            logger.error(f"HTTP download failed: {e}")
            if task:
                task["status"] = "failed"
                task["error"] = str(e)
                task["speed"] = 0.0
            database.update_http_download_status(info_hash, status="failed")
            if window and hasattr(window, "_show_toast"):
                err_msg = str(e)
                GLib.idle_add(lambda: window._show_toast(f"Download failed: {err_msg}", timeout=6))

    t = threading.Thread(target=_worker, name=f"HttpDownload-{info_hash}", daemon=True)
    with _downloads_lock:
        if info_hash in _active_downloads:
            _active_downloads[info_hash]["thread"] = t
    t.start()


def stop_http_download(info_hash):
    """Signal an active HTTP download to pause."""
    with _downloads_lock:
        task = _active_downloads.get(info_hash)
        if task and task.get("abort_event"):
            task["abort_event"].set()
            task["status"] = "paused"
            task["speed"] = 0.0
            return True
    return False


def resume_http_download(download_dict, window=None):
    """Resume a paused or failed HTTP download."""
    info_hash = download_dict.get("info_hash")
    url = download_dict.get("url")
    dest_path = download_dict.get("file_path")
    if not info_hash or not url or not dest_path:
        return False
    if not api._is_remote_http_url(url):
        return False

    with _downloads_lock:
        existing = _active_downloads.get(info_hash)
        if existing and existing.get("status") == "downloading":
            return True

    title = download_dict.get("name") or os.path.basename(dest_path)
    _start_worker_thread(info_hash, url, dest_path, clean_title=title, window=window)
    return True


def delete_http_download(info_hash, file_path=None):
    """Stop download worker if active, remove files, and remove from database."""
    stop_http_download(info_hash)
    with _downloads_lock:
        _active_downloads.pop(info_hash, None)

    if not file_path:
        for d in database.get_downloads():
            if d.get("info_hash") == info_hash:
                file_path = d.get("file_path")
                break

    if file_path:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
            if os.path.exists(file_path + ".part"):
                os.remove(file_path + ".part")
        except Exception as e:
            logger.warning(f"Error removing HTTP download files for {info_hash}: {e}")

    database.remove_download(info_hash)
