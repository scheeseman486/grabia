# Grabia - Internet Archive Download Manager
# Copyright (C) 2026 Sharkcheese
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""Download manager - runs in a background thread, handles sequential downloads with pause/resume/retry."""

import os
import json
import hashlib
import time
import logging
import threading
from datetime import datetime
import requests
import database as db
import ia_client
from logger import log

log = logging.getLogger(__name__)


def get_scheduled_limit():
    """Check speed schedule rules and return the bandwidth limit in bytes/sec, or None if no rule matches.

    Returns:
        int: limit in bytes/sec (0 = pause, positive = throttle), or
        None: no rule matches (means uncapped / -1)
    """
    raw = db.get_setting("speed_schedule", "[]")
    try:
        rules = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not rules:
        return None

    now = datetime.now()
    # Monday=0 ... Sunday=6
    current_day = now.weekday()
    current_minutes = now.hour * 60 + now.minute

    for rule in rules:
        days = rule.get("days", [0, 1, 2, 3, 4, 5, 6])
        if current_day not in days:
            continue
        start_parts = (rule.get("start") or "00:00").split(":")
        end_parts = (rule.get("end") or "23:59").split(":")
        start_min = int(start_parts[0]) * 60 + int(start_parts[1])
        end_min = int(end_parts[0]) * 60 + int(end_parts[1])
        if start_min <= current_minutes <= end_min:
            return rule.get("limit_kbps", 0) * 1024  # Convert KB/s to bytes/s; 0 = pause
    return None


class DownloadManager:
    def __init__(self):
        self.state = "stopped"  # stopped, running, paused
        self._thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused initially
        self._bandwidth_limit = -1  # bytes/sec: -1 = unlimited, 0 = paused, >0 = limit
        self._schedule_overridden = False  # True when user manually changed BW; cleared on schedule transition
        self._paused_by_bandwidth = False  # True when paused because bandwidth was set to 0
        self._last_schedule_limit = None   # Track schedule transitions
        self._current_file_info = None
        self._current_speed = 0
        self._skip_file_event = threading.Event()
        self._lock = threading.Lock()
        self._listeners = []

    def add_listener(self, callback):
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self, event, data=None):
        with self._lock:
            listeners = list(self._listeners)  # Snapshot to avoid mutation during iteration
        for cb in listeners:
            try:
                cb(event, data)
            except Exception:
                pass

    @property
    def bandwidth_limit(self):
        with self._lock:
            return self._bandwidth_limit

    @bandwidth_limit.setter
    def bandwidth_limit(self, value):
        notify_state = None
        with self._lock:
            old = self._bandwidth_limit
            self._bandwidth_limit = int(value)
            if self._bandwidth_limit < -1:
                self._bandwidth_limit = -1
            self._schedule_overridden = True

            # 0 = effectively pause downloads
            if self._bandwidth_limit == 0 and self.state == "running":
                self._pause_event.clear()
                self.state = "paused"
                self._paused_by_bandwidth = True
                notify_state = self.state
            # Non-zero: resume if we were paused by bandwidth
            elif old == 0 and self._bandwidth_limit != 0 and self._paused_by_bandwidth:
                self._pause_event.set()
                self.state = "running"
                self._paused_by_bandwidth = False
                notify_state = self.state

        db.set_setting("bandwidth_limit", str(self._bandwidth_limit))
        if notify_state is not None:
            self._notify("state", notify_state)

    def start(self):
        with self._lock:
            if self.state == "running":
                return  # Already running, nothing to notify
            if self.state == "paused":
                self._pause_event.set()
                self.state = "running"
                self._paused_by_bandwidth = False
            else:
                self._stop_event.clear()
                self._pause_event.set()
                self.state = "running"
                self._paused_by_bandwidth = False
                self._last_schedule_limit = None
                self._thread = threading.Thread(target=self._download_loop, daemon=True)
                self._thread.start()
        self._notify("state", "running")

    def pause(self):
        with self._lock:
            if self.state != "running":
                return
            self._pause_event.clear()
            self.state = "paused"
        self._notify("state", "paused")

    def stop(self):
        with self._lock:
            self.state = "stopped"
            self._stop_event.set()
            self._pause_event.set()  # Unblock if paused so thread can exit
            self._paused_by_bandwidth = False
            self._last_schedule_limit = None
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        with self._lock:
            self._thread = None
            self._current_file_info = None
            self._current_speed = 0
        # Reset any files stuck in downloading state
        db.reset_downloading_files()
        self._notify("state", "stopped")

    def skip_current_file(self, file_id):
        """Skip the currently downloading file if it matches file_id."""
        with self._lock:
            if self._current_file_info and self._current_file_info.get("file_id") == file_id:
                self._skip_file_event.set()
                return True
        return False

    def get_status(self):
        with self._lock:
            return {
                "state": self.state,
                "bandwidth_limit": self._bandwidth_limit,
                "current_file": self._current_file_info,
                "current_speed": self._current_speed,
                "progress": db.get_download_progress(),
            }

    def _download_loop(self):
        while not self._stop_event.is_set():
            # Wait if paused
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            file_info = db.get_next_download_file()
            if not file_info:
                # No more files to download, idle briefly then check again
                time.sleep(2)
                continue

            # If this is a retry of a previously failed file, wait retry_delay first
            if file_info.get("download_status") == "failed":
                retry_delay = int(db.get_setting("retry_delay", "5"))
                for _ in range(retry_delay):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)
                if self._stop_event.is_set():
                    break

            self._download_file(file_info)

        # stop() handles state notification after reset_downloading_files(),
        # so don't notify here to avoid a race where the client refreshes
        # before the DB has been updated.
        self._current_file_info = None

    def _download_file(self, file_info):
        self._skip_file_event.clear()
        file_id = file_info["id"]
        archive_id = file_info["archive_id"]
        identifier = file_info["identifier"]
        filename = file_info["name"]
        expected_size = file_info["size"]
        expected_md5 = file_info["md5"]

        download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))

        # Construct local path preserving directory structure.
        # Sanitise: resolve the path and verify it stays within the
        # intended download directory to prevent path-traversal attacks
        # via malicious filenames in IA metadata (e.g. "../../etc/foo").
        base_dir = os.path.realpath(os.path.join(download_dir, identifier))
        local_path = os.path.realpath(os.path.join(base_dir, filename))
        if not local_path.startswith(base_dir + os.sep) and local_path != base_dir:
            error_msg = f"Blocked path traversal in filename: {filename}"
            log.warning("download", "%s", error_msg)
            db.set_file_download_status(file_id, "failed", error_message=error_msg)
            self._notify("file_error", {"file_id": file_id, "filename": filename, "identifier": identifier, "error": error_msg})
            return
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        use_http = db.get_setting("use_http", "0") == "1"
        url = ia_client.get_download_url(identifier, filename, use_http=use_http)

        # Get IA auth cookies
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        cookies, auth_error = ia_client.get_download_cookies(ia_email, ia_password)
        if auth_error:
            log.warning("download", "IA auth issue for %s: %s", filename, auth_error)

        with self._lock:
            self._current_file_info = {
                "file_id": file_id,
                "archive_id": archive_id,
                "identifier": identifier,
                "filename": filename,
                "size": expected_size,
                "downloaded": 0,
                "status": "downloading",
            }

        log.debug("download", "Starting download: %s (%s bytes)", filename, expected_size)
        db.set_file_download_status(file_id, "downloading")
        db.set_archive_status(archive_id, "downloading")
        self._notify("file_start", {"file_id": file_id, "filename": filename})

        success = False
        try:
            success = self._do_download(url, local_path, file_id, expected_size, expected_md5, cookies)
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else "?"
            if status_code in (403, 401) and auth_error:
                error_msg = f"HTTP {status_code} (auth failed: {auth_error})"
            elif status_code in (403, 401) and not ia_email:
                error_msg = f"HTTP {status_code} — IA credentials not configured (set in Settings)"
            elif status_code in (403, 401):
                error_msg = f"HTTP {status_code} — access denied (check IA credentials in Settings)"
                ia_client.invalidate_cookie_cache()
            else:
                error_msg = f"HTTP {status_code}: {str(e)}"
            log.warning("download", "Failed %s: %s", filename, error_msg)
            db.set_file_download_status(file_id, "failed", error_message=error_msg)
            db.increment_file_retry(file_id)
            self._notify("file_error", {"file_id": file_id, "filename": filename, "identifier": identifier, "error": error_msg})
        except Exception as e:
            error_msg = str(e)
            log.warning("download", "Failed %s: %s", filename, error_msg)
            db.set_file_download_status(file_id, "failed", error_message=error_msg)
            db.increment_file_retry(file_id)
            self._notify("file_error", {"file_id": file_id, "filename": filename, "identifier": identifier, "error": error_msg})

        if success:
            log.debug("download", "Completed: %s", filename)
            db.set_file_download_status(file_id, "completed", downloaded_bytes=expected_size)
            self._notify("file_complete", {"file_id": file_id, "filename": filename, "identifier": identifier})
        elif self._skip_file_event.is_set():
            # File was removed from queue mid-download — reset to pending
            log.debug("download", "Skipped (dequeued): %s", filename)
            db.set_file_download_status(file_id, "pending", downloaded_bytes=0)
            self._skip_file_event.clear()
            self._notify("file_skipped", {"file_id": file_id, "filename": filename, "identifier": identifier})
        elif not self._stop_event.is_set() and not success:
            # Mark failed, increment retry — the download loop will re-pick it
            # if retries remain, after processing other pending files first
            self._notify("file_failed", {"file_id": file_id, "filename": filename, "identifier": identifier})

        # Check if all files in this archive are done
        self._check_archive_completion(archive_id)

        with self._lock:
            self._current_file_info = None
            self._current_speed = 0

    def _do_download(self, url, local_path, file_id, expected_size, expected_md5, cookies):
        """Perform the actual download with resume support and bandwidth limiting."""
        headers = {"User-Agent": "grabia/1.0"}

        # Resume support
        existing_size = 0
        if os.path.exists(local_path):
            existing_size = os.path.getsize(local_path)
            if expected_size > 0 and existing_size >= expected_size:
                # File already complete, verify hash
                if expected_md5 and self._verify_md5(local_path, expected_md5):
                    return True
                else:
                    # Hash mismatch or no hash, re-download
                    existing_size = 0
                    os.remove(local_path)

        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        resp = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60)

        if resp.status_code == 416:
            # Range not satisfiable, file might be complete
            if expected_md5 and self._verify_md5(local_path, expected_md5):
                return True
            existing_size = 0
            if os.path.exists(local_path):
                os.remove(local_path)
            headers.pop("Range", None)
            resp = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60)

        resp.raise_for_status()

        mode = "ab" if existing_size > 0 and resp.status_code == 206 else "wb"
        if mode == "wb":
            existing_size = 0

        downloaded = existing_size
        chunk_size = 8192
        last_update = time.time()
        # Token bucket for bandwidth limiting
        tokens = 0.0
        token_time = time.time()
        # For speed measurement: track bytes in a rolling window
        speed_samples = []

        with open(local_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if self._stop_event.is_set() or self._skip_file_event.is_set():
                    return False

                # Wait if paused
                self._pause_event.wait()
                if self._stop_event.is_set() or self._skip_file_event.is_set():
                    return False

                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    # --- Bandwidth limiting ---
                    # Determine effective limit: schedule takes priority unless manually overridden
                    scheduled = get_scheduled_limit()
                    bw_notify = None

                    with self._lock:
                        if scheduled is not None:
                            # A schedule rule is active
                            if scheduled != self._last_schedule_limit:
                                # Schedule transition: clear manual override, apply new schedule
                                self._last_schedule_limit = scheduled
                                self._schedule_overridden = False
                                bw_notify = {"limit": scheduled, "source": "schedule"}
                            if self._schedule_overridden:
                                limit = self._bandwidth_limit  # User manually overrode
                            else:
                                limit = scheduled
                        else:
                            # No schedule rule active
                            if self._last_schedule_limit is not None:
                                # Transitioning out of schedule
                                self._last_schedule_limit = None
                                self._schedule_overridden = False
                                bw_notify = {"limit": self._bandwidth_limit, "source": "manual"}
                            limit = self._bandwidth_limit

                    if bw_notify is not None:
                        self._notify("bandwidth_update", bw_notify)

                    # 0 = schedule-driven pause (manual 0 already triggers real pause via setter)
                    if limit == 0:
                        # Pause via event so UI reflects it
                        do_pause_notify = False
                        with self._lock:
                            if self.state == "running":
                                self._pause_event.clear()
                                self.state = "paused"
                                self._paused_by_bandwidth = True
                                do_pause_notify = True
                        if do_pause_notify:
                            self._notify("state", "paused")
                        # Block until limit changes
                        while limit == 0 and not self._stop_event.is_set():
                            time.sleep(0.25)
                            with self._lock:
                                # Check for manual override
                                if self._schedule_overridden:
                                    limit = self._bandwidth_limit
                                    break
                            # Re-check schedule
                            new_scheduled = get_scheduled_limit()
                            if new_scheduled != scheduled:
                                with self._lock:
                                    self._last_schedule_limit = new_scheduled
                                    if new_scheduled is not None:
                                        limit = new_scheduled
                                    else:
                                        limit = self._bandwidth_limit
                                self._notify("bandwidth_update", {"limit": limit, "source": "schedule" if new_scheduled is not None else "manual"})
                                break
                        # Resume after schedule-driven pause
                        do_resume_notify = False
                        with self._lock:
                            if limit != 0 and self._paused_by_bandwidth:
                                self._pause_event.set()
                                self.state = "running"
                                self._paused_by_bandwidth = False
                                do_resume_notify = True
                        if do_resume_notify:
                            self._notify("state", "running")
                        tokens = 0.0
                        token_time = time.time()
                        if self._stop_event.is_set():
                            return False

                    # Positive limit: throttle via token bucket
                    if limit > 0:
                        now = time.time()
                        elapsed = now - token_time
                        token_time = now
                        tokens += elapsed * limit
                        if tokens > limit:
                            tokens = limit
                        tokens -= len(chunk)
                        if tokens < 0:
                            sleep_time = -tokens / limit
                            if sleep_time > 0.001:
                                time.sleep(sleep_time)
                            tokens = 0.0
                            token_time = time.time()
                    # limit == -1: unlimited, no throttling

                    # Update progress every 0.5 seconds
                    now = time.time()
                    speed_samples.append((now, len(chunk)))
                    if now - last_update >= 0.5:
                        # Calculate speed from recent samples (last 2 seconds)
                        cutoff = now - 2.0
                        speed_samples = [(t, s) for t, s in speed_samples if t > cutoff]
                        if len(speed_samples) >= 2:
                            window_time = speed_samples[-1][0] - speed_samples[0][0]
                            window_bytes = sum(s for _, s in speed_samples)
                            speed = window_bytes / max(window_time, 0.001)
                        else:
                            speed = 0
                        with self._lock:
                            if self._current_file_info:
                                self._current_file_info["downloaded"] = downloaded
                                self._current_speed = speed
                        db.set_file_download_status(file_id, "downloading", downloaded_bytes=downloaded)
                        self._notify("file_progress", {
                            "file_id": file_id,
                            "downloaded": downloaded,
                            "size": expected_size,
                            "speed": speed,
                        })
                        last_update = now

        # Verify hash if available — skip for files with no size in metadata,
        # as IA regenerates them dynamically and the stored hash is stale
        if expected_md5 and expected_size > 0:
            if not self._verify_md5(local_path, expected_md5):
                os.remove(local_path)
                raise Exception("MD5 hash mismatch after download")

        return True

    def _verify_md5(self, filepath, expected_md5):
        md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest() == expected_md5

    def _check_archive_completion(self, archive_id):
        conn = db.get_db()
        try:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN download_status = 'conflict' THEN 1 ELSE 0 END) as conflict
                FROM archive_files
                WHERE archive_id = ? AND selected = 1
            """, (archive_id,)).fetchone()
        finally:
            conn.close()

        if row["total"] == 0:
            db.set_archive_status(archive_id, "idle")
        elif row["completed"] == row["total"]:
            db.set_archive_status(archive_id, "completed")
        elif row["completed"] + row["failed"] + row["conflict"] == row["total"]:
            db.set_archive_status(archive_id, "partial")
        else:
            # Still has pending files but nothing actively downloading —
            # mark idle so it doesn't stay stuck on "downloading" or "queued"
            db.set_archive_status(archive_id, "idle")


# Singleton
download_manager = DownloadManager()
