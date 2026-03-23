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

"""Background processing worker for post-download file conversion."""

import os
import queue
import threading
import time

import database as db
from logger import log
from processors import (
    get_processor,
    ProcessingError,
    ProcessingCancelled,
)


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------

_processing_queue = queue.Queue()
_cancel_events = {}          # archive_id -> threading.Event
_processing_lock = threading.Lock()
_sse_broadcaster = None      # set by init_processing_worker()


def init_processing_worker(broadcast_fn):
    """Start the background processing worker thread."""
    global _sse_broadcaster
    _sse_broadcaster = broadcast_fn
    t = threading.Thread(target=_worker_loop, daemon=True, name="processing-worker")
    t.start()


def queue_archive_processing(archive_id, profile_id, file_ids=None, options_override=None):
    """Queue processing for an archive.

    Args:
        archive_id: archive to process
        profile_id: processing profile to use
        file_ids: specific file IDs to process (None = all eligible)
        options_override: dict of option overrides for this run
    """
    with _processing_lock:
        if archive_id in _cancel_events:
            return False, "Processing already queued for this archive"
        evt = threading.Event()
        _cancel_events[archive_id] = evt

    pending = _processing_queue.qsize()
    _processing_queue.put({
        "archive_id": archive_id,
        "profile_id": profile_id,
        "file_ids": file_ids,
        "options_override": options_override,
    })
    return True, pending > 0


def cancel_archive_processing(archive_id):
    """Cancel processing for an archive."""
    with _processing_lock:
        evt = _cancel_events.get(archive_id)
        if evt:
            evt.set()
            return True
    return False


def is_processing(archive_id):
    """Check if an archive is currently queued or being processed."""
    with _processing_lock:
        return archive_id in _cancel_events


def _broadcast(event, data):
    if _sse_broadcaster:
        _sse_broadcaster(event, data)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _worker_loop():
    while True:
        job = _processing_queue.get()
        try:
            _run_processing(job)
        except Exception as e:
            _broadcast("processing_progress", {
                "archive_id": job["archive_id"],
                "phase": "error",
                "error": str(e),
            })
        finally:
            with _processing_lock:
                _cancel_events.pop(job["archive_id"], None)
            _processing_queue.task_done()


def _run_processing(job):
    archive_id = job["archive_id"]
    profile_id = job["profile_id"]
    file_ids = job.get("file_ids")
    options_override = job.get("options_override") or {}

    cancel_evt = _cancel_events.get(archive_id)

    def _cancelled():
        return cancel_evt and cancel_evt.is_set()

    log.info("worker", "Processing job started: archive=%d, profile=%d", archive_id, profile_id)

    # Load profile
    profile = db.get_processing_profile(profile_id)
    if not profile:
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "error",
            "error": "Processing profile not found",
        })
        return

    # Load archive
    archive = db.get_archive(archive_id)
    if not archive:
        return

    # Get processor class
    import json
    processor_cls = get_processor(profile["processor_type"])
    if not processor_cls:
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "error",
            "error": f"Unknown processor type: {profile['processor_type']}",
        })
        return

    # Merge profile options with overrides
    profile_options = json.loads(profile.get("options_json", "{}"))
    merged_options = {**profile_options, **options_override}
    log.debug("worker", "Profile: %s, processor: %s, options: %s",
              profile["name"], profile["processor_type"], merged_options)

    # Get download directory
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    archive_dir = os.path.join(download_dir, archive["identifier"])

    # Get eligible files
    if file_ids:
        # Process specific files
        conn = db.get_db()
        placeholders = ",".join("?" * len(file_ids))
        rows = conn.execute(
            f"SELECT * FROM archive_files WHERE id IN ({placeholders}) AND archive_id = ?",
            file_ids + [archive_id],
        ).fetchall()
        conn.close()
        files = [dict(r) for r in rows]
    else:
        files = db.get_processable_files(archive_id)

    # Filter to files the processor can handle
    input_exts = set(processor_cls.input_extensions)
    pre_filter = len(files)
    rejected = [f for f in files if os.path.splitext(f["name"])[1].lower() not in input_exts]
    files = [f for f in files if os.path.splitext(f["name"])[1].lower() in input_exts]
    log.debug("worker", "File filter: %d eligible, %d matched extensions %s, %d rejected",
              pre_filter, len(files), sorted(input_exts), len(rejected))
    for r in rejected:
        ext = os.path.splitext(r["name"])[1].lower()
        log.debug("worker", "  Rejected: %s (ext: %s)", r["name"], ext)

    if not files:
        log.info("worker", "No files to process after filtering — done")
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "done",
            "summary": {"processed": 0, "skipped": 0, "failed": 0},
        })
        return

    # Mark all as queued
    for f in files:
        db.set_file_processing_status(f["id"], "queued", processor_type=profile["processor_type"])

    _broadcast("processing_progress", {
        "archive_id": archive_id,
        "phase": "starting",
        "total": len(files),
    })

    summary = {"processed": 0, "skipped": 0, "failed": 0}

    for i, file_info in enumerate(files):
        if _cancelled():
            # Mark remaining as un-queued
            for remaining in files[i:]:
                db.set_file_processing_status(remaining["id"], "", error="Cancelled")
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "phase": "cancelled",
                "summary": summary,
            })
            return

        file_id = file_info["id"]
        filename = file_info["name"]
        file_path = os.path.join(archive_dir, filename)

        db.set_file_processing_status(file_id, "processing")

        def progress_cb(**kwargs):
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "file_id": file_id,
                "filename": filename,
                "current": i + 1,
                "total": len(files),
                **kwargs,
            })

        try:
            log.debug("worker", "Processing file %d/%d: %s", i + 1, len(files), filename)
            processor = processor_cls(
                options=merged_options,
                cancel_check=_cancelled,
                progress_callback=progress_cb,
            )

            if not os.path.isfile(file_path):
                raise ProcessingError(f"File not found: {file_path}")

            result = processor.process(file_path, archive_dir)

            if result.get("skipped"):
                log.info("worker", "Skipped %s: %s", filename, result.get("reason", "Not processable"))
                db.set_file_processing_status(
                    file_id, "skipped",
                    error=result.get("reason", "Not processable"),
                )
                summary["skipped"] += 1
            else:
                # Delete original files if option allows
                delete_original = merged_options.get("delete_original", "yes") == "yes"
                if delete_original:
                    for to_delete in result.get("files_to_delete", []):
                        try:
                            os.remove(to_delete)
                        except OSError:
                            pass

                # Use "extracted" status when original is kept, "completed" when deleted
                proc_status = "completed" if delete_original else "extracted"
                log.info("worker", "%s %s -> %s", proc_status.capitalize(), filename, result["processed_filename"])
                db.set_file_processing_status(
                    file_id, proc_status,
                    processed_filename=result["processed_filename"],
                    processor_type=profile["processor_type"],
                    processed_files=result.get("processed_files"),
                )
                summary["processed"] += 1

            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "file_id": file_id,
                "filename": filename,
                "phase": "file_done",
                "current": i + 1,
                "total": len(files),
                "skipped": result.get("skipped", False),
            })

        except ProcessingCancelled:
            db.set_file_processing_status(file_id, "", error="Cancelled")
            for remaining in files[i + 1:]:
                db.set_file_processing_status(remaining["id"], "", error="Cancelled")
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "phase": "cancelled",
                "summary": summary,
            })
            return

        except ProcessingError as e:
            log.error("worker", "Failed %s: %s", filename, e)
            db.set_file_processing_status(file_id, "failed", error=str(e))
            summary["failed"] += 1
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "file_id": file_id,
                "filename": filename,
                "phase": "file_error",
                "error": str(e),
                "current": i + 1,
                "total": len(files),
            })

        except Exception as e:
            db.set_file_processing_status(file_id, "failed", error=str(e))
            summary["failed"] += 1
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "file_id": file_id,
                "filename": filename,
                "phase": "file_error",
                "error": str(e),
                "current": i + 1,
                "total": len(files),
            })

    log.info("worker", "Processing complete for archive %d: %d processed, %d skipped, %d failed",
             archive_id, summary["processed"], summary["skipped"], summary["failed"])
    _broadcast("processing_progress", {
        "archive_id": archive_id,
        "phase": "done",
        "summary": summary,
    })
