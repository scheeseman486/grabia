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

"""Background processing worker for post-download file conversion.

Uses a SQLite-backed job queue so jobs survive server restarts.
"""

import json
import os
import threading
import time

import activity
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

_cancel_events = {}          # archive_id -> threading.Event
_processing_lock = threading.Lock()
_sse_broadcaster = None      # set by init_processing_worker()
_wake_event = threading.Event()  # signalled when a new job is enqueued


def init_processing_worker(broadcast_fn):
    """Start the background processing worker thread."""
    global _sse_broadcaster
    _sse_broadcaster = broadcast_fn
    t = threading.Thread(target=_worker_loop, daemon=True, name="processing-worker")
    t.start()


def queue_archive_processing(archive_id, profile_id, file_ids=None, options_override=None):
    """Queue processing for an archive via the DB.

    Args:
        archive_id: archive to process
        profile_id: processing profile to use
        file_ids: specific file IDs to process (None = all eligible)
        options_override: dict of option overrides for this run

    Returns:
        (ok, info) — ok is True on success, info is pending count or error string
    """
    # Check if there's already an active job for this archive
    existing = db.get_active_processing_job_for_archive(archive_id)
    if existing:
        return False, "Processing already queued for this archive"

    # Set up cancel event BEFORE creating the DB job so the worker can
    # find it even if it picks up the job immediately.
    with _processing_lock:
        evt = threading.Event()
        _cancel_events[archive_id] = evt

    job_id = db.create_processing_job(archive_id, profile_id, file_ids, options_override)

    # Create an activity job to track this processing run
    archive = db.get_archive(archive_id)
    group_id = archive["group_id"] if archive else None
    act_job_id = activity.start_job(
        "processing", archive_id=archive_id, group_id=group_id,
        processing_job_id=job_id,
    )

    pending = db.count_pending_processing_jobs()

    # Create a persistent notification for the queued job
    archive_name = archive["title"] or archive["identifier"] if archive else f"Archive #{archive_id}"
    if pending > 1:
        notif_id = db.create_notification(
            f'Processing "{archive_name}": queued (position {pending})',
            type="info", progress=0, processing_archive_id=archive_id,
            job_id=act_job_id,
        )
    else:
        notif_id = db.create_notification(
            f'Processing "{archive_name}": starting...',
            type="info", progress=0, processing_archive_id=archive_id,
            job_id=act_job_id,
        )

    # Link the notification back to the activity job
    activity.update_job_notification(act_job_id, notif_id)

    _broadcast("notification_created", db.get_notification(notif_id))

    # Wake the worker thread
    _wake_event.set()

    return True, pending > 1


def cancel_archive_processing(archive_id):
    """Cancel processing for an archive and remove its notification."""
    cancelled = False

    with _processing_lock:
        evt = _cancel_events.get(archive_id)
        if evt:
            evt.set()
            cancelled = True

    # Also cancel via DB — handles pending jobs and running jobs that
    # somehow lack a cancel event (e.g. after crash recovery).
    if not cancelled:
        job = db.get_active_processing_job_for_archive(archive_id)
        if job:
            db.cancel_processing_job(job["id"])
            cancelled = True

    # Always remove the notification on cancel
    if cancelled:
        notif = db.find_notification_by_processing(archive_id)
        if notif:
            db.delete_notification(notif["id"])
            _broadcast("notification_dismissed", {"id": notif["id"]})

        # Close any running activity job for this archive.
        # The processing loop may also call finish_job when it detects the
        # cancel event, but that's harmless (just an extra UPDATE).
        _cancel_activity_job(archive_id)

    return cancelled


def _cancel_activity_job(archive_id):
    """Find and close the running activity job for a cancelled archive."""
    try:
        with db._db() as conn:
            row = conn.execute(
                """SELECT id FROM activity_jobs
                   WHERE category = 'processing' AND archive_id = ?
                         AND status = 'running'
                   ORDER BY id DESC LIMIT 1""",
                (archive_id,),
            ).fetchone()
            if row:
                activity.log(row["id"], "warning", "Cancelled by user",
                             archive_id=archive_id)
                activity.flush()
                activity.finish_job(row["id"], "cancelled", summary="Cancelled by user")
    except Exception:
        pass


def is_processing(archive_id):
    """Check if an archive is currently queued or being processed."""
    with _processing_lock:
        if archive_id in _cancel_events:
            return True
    job = db.get_active_processing_job_for_archive(archive_id)
    return job is not None


def _broadcast(event, data):
    if _sse_broadcaster:
        _sse_broadcaster(event, data)


# ---------------------------------------------------------------------------
# Worker loop — polls DB for pending jobs
# ---------------------------------------------------------------------------

def _worker_loop():
    while True:
        job = db.get_next_processing_job()
        if not job:
            # No work — wait for a wake signal or poll every 5s
            _wake_event.wait(timeout=5)
            _wake_event.clear()
            continue

        if not db.claim_processing_job(job["id"]):
            # Another thread/instance claimed it (shouldn't happen with single worker)
            continue

        # Ensure a cancel event exists for this archive so cancel requests work.
        # queue_archive_processing() creates one for new jobs, but recovered
        # jobs (from a crash) won't have one.
        with _processing_lock:
            if job["archive_id"] not in _cancel_events:
                _cancel_events[job["archive_id"]] = threading.Event()

        try:
            _run_processing(job)
            db.complete_processing_job(job["id"])
        except Exception as e:
            log.error("worker", "Processing job %d failed: %s", job["id"], e)
            db.complete_processing_job(job["id"], error_message=str(e))
            _broadcast("processing_progress", {
                "archive_id": job["archive_id"],
                "phase": "error",
                "error": str(e),
            })
        finally:
            with _processing_lock:
                _cancel_events.pop(job["archive_id"], None)


def _find_activity_job(processing_job_id):
    """Look up the activity job linked to a processing job."""
    with db._db() as conn:
        row = conn.execute(
            "SELECT id FROM activity_jobs WHERE processing_job_id = ?",
            (processing_job_id,),
        ).fetchone()
        return row["id"] if row else None


def _run_processing(job):
    archive_id = job["archive_id"]
    profile_id = job["profile_id"]
    file_ids = json.loads(job["file_ids_json"]) if job.get("file_ids_json") else None
    options_override = json.loads(job["options_override_json"]) if job.get("options_override_json") else {}

    cancel_evt = _cancel_events.get(archive_id)

    def _cancelled():
        return cancel_evt and cancel_evt.is_set()

    # Find the activity job linked to this processing job
    act_job_id = _find_activity_job(job["id"])

    log.info("worker", "Processing job started: archive=%d, profile=%d", archive_id, profile_id)

    archive = db.get_archive(archive_id)
    archive_name = archive["title"] or archive["identifier"] if archive else f"Archive #{archive_id}"

    # Find or create the notification for this job
    notif = db.find_notification_by_processing(archive_id)
    if notif:
        notif_id = notif["id"]
    else:
        notif_id = db.create_notification(
            f'Processing "{archive_name}": starting...',
            type="info", progress=0, processing_archive_id=archive_id,
        )
        _broadcast("notification_created", db.get_notification(notif_id))

    # Load profile
    profile = db.get_processing_profile(profile_id)
    if not profile:
        msg = "Processing profile not found"
        if act_job_id:
            activity.log(act_job_id, "error", msg, archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary=msg)
        if notif_id:
            db.update_notification(notif_id, message=f'Processing "{archive_name}" failed: {msg}', type="error", progress=None)
            _broadcast("notification_updated", db.get_notification(notif_id))
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "error",
            "error": msg,
        })
        return

    if not archive:
        if act_job_id:
            activity.log(act_job_id, "error", "Archive not found", archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary="Archive not found")
        return

    # Get processor class
    processor_cls = get_processor(profile["processor_type"])
    if not processor_cls:
        msg = f"Unknown processor type: {profile['processor_type']}"
        if act_job_id:
            activity.log(act_job_id, "error", msg, archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary=msg)
        if notif_id:
            db.update_notification(notif_id, message=f'Processing "{archive_name}" failed: {msg}', type="error", progress=None)
            _broadcast("notification_updated", db.get_notification(notif_id))
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "error",
            "error": msg,
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
        if act_job_id:
            activity.log(act_job_id, "info", "No eligible files after filtering",
                         archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "completed", summary="No eligible files")
        if notif_id:
            db.update_notification(notif_id, message=f'Processing "{archive_name}": no eligible files', type="info", progress=None)
            _broadcast("notification_updated", db.get_notification(notif_id))
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "done",
            "summary": {"processed": 0, "skipped": 0, "failed": 0},
        })
        return

    # Mark all as queued
    for f in files:
        db.set_file_processing_status(f["id"], "queued", processor_type=profile["processor_type"])

    if notif_id:
        db.update_notification(notif_id, message=f'Processing "{archive_name}": starting...', progress=0)
        _broadcast("notification_updated", db.get_notification(notif_id))

    if act_job_id:
        activity.log(act_job_id, "info",
                     f"Processing started: {len(files)} files with {profile['name']}",
                     archive_id=archive_id)
        activity.flush()

    _broadcast("processing_progress", {
        "archive_id": archive_id,
        "phase": "starting",
        "total": len(files),
    })

    summary = {"processed": 0, "skipped": 0, "failed": 0}

    for i, file_info in enumerate(files):
        if _cancelled():
            for remaining in files[i:]:
                db.set_file_processing_status(remaining["id"], "", error="Cancelled")
            if act_job_id:
                activity.log(act_job_id, "warning", "Processing cancelled by user",
                             archive_id=archive_id)
                activity.flush()
                parts = []
                if summary["processed"]: parts.append(f'{summary["processed"]} converted')
                if summary["skipped"]: parts.append(f'{summary["skipped"]} skipped')
                if summary["failed"]: parts.append(f'{summary["failed"]} failed')
                activity.finish_job(act_job_id, "cancelled",
                                    summary=", ".join(parts) if parts else "Cancelled before processing")
            # Notification is cleaned up by cancel_archive_processing();
            # only delete here if it still exists (e.g. ProcessingCancelled
            # raised by the processor itself without an explicit cancel call).
            if notif_id and db.get_notification(notif_id):
                db.delete_notification(notif_id)
                _broadcast("notification_dismissed", {"id": notif_id})
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

        # Update notification progress
        pct = int((i / len(files)) * 100)
        if notif_id:
            db.update_notification(notif_id, message=f'Processing "{archive_name}": {i + 1}/{len(files)}', progress=pct)
            _broadcast("notification_updated", db.get_notification(notif_id))

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
                reason = result.get("reason", "Not processable")
                log.info("worker", "Skipped %s: %s", filename, reason)
                db.set_file_processing_status(
                    file_id, "skipped",
                    error=reason,
                )
                if act_job_id:
                    activity.log(act_job_id, "info", f"Skipped: {filename}",
                                 archive_id=archive_id, file_id=file_id,
                                 detail=reason)
                summary["skipped"] += 1
            else:
                delete_original = merged_options.get("delete_original", "yes") == "yes"
                if delete_original:
                    for to_delete in result.get("files_to_delete", []):
                        try:
                            os.remove(to_delete)
                        except OSError:
                            pass

                log.info("worker", "Processed %s -> %s", filename, result["processed_filename"])
                db.set_file_processing_status(
                    file_id, "processed",
                    processed_filename=result["processed_filename"],
                    processor_type=profile["processor_type"],
                    processed_files=result.get("processed_files"),
                )
                if act_job_id:
                    activity.log(act_job_id, "success",
                                 f"Converted: {filename} → {result['processed_filename']}",
                                 archive_id=archive_id, file_id=file_id)
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
            if act_job_id:
                activity.log(act_job_id, "warning",
                             f"Cancelled during: {filename}",
                             archive_id=archive_id, file_id=file_id)
                activity.flush()
                parts = []
                if summary["processed"]: parts.append(f'{summary["processed"]} converted')
                if summary["skipped"]: parts.append(f'{summary["skipped"]} skipped')
                if summary["failed"]: parts.append(f'{summary["failed"]} failed')
                activity.finish_job(act_job_id, "cancelled",
                                    summary=", ".join(parts) if parts else "Cancelled")
            # Notification is cleaned up by cancel_archive_processing();
            # only delete here if it still exists (e.g. ProcessingCancelled
            # raised by the processor itself without an explicit cancel call).
            if notif_id and db.get_notification(notif_id):
                db.delete_notification(notif_id)
                _broadcast("notification_dismissed", {"id": notif_id})
            _broadcast("processing_progress", {
                "archive_id": archive_id,
                "phase": "cancelled",
                "summary": summary,
            })
            return

        except ProcessingError as e:
            log.error("worker", "Failed %s: %s", filename, e)
            db.set_file_processing_status(file_id, "failed", error=str(e))
            if act_job_id:
                activity.log(act_job_id, "error", f"Failed: {filename}",
                             archive_id=archive_id, file_id=file_id,
                             detail=str(e))
                activity.flush()  # errors flush immediately for visibility
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
            if act_job_id:
                activity.log(act_job_id, "error", f"Failed: {filename}",
                             archive_id=archive_id, file_id=file_id,
                             detail=str(e))
                activity.flush()  # errors flush immediately for visibility
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

        # Periodic flush so entries are visible during long jobs
        if act_job_id and (i + 1) % 10 == 0:
            activity.flush()

    log.info("worker", "Processing complete for archive %d: %d processed, %d skipped, %d failed",
             archive_id, summary["processed"], summary["skipped"], summary["failed"])

    # Flush activity log and finish the activity job
    if act_job_id:
        parts = []
        if summary["processed"]: parts.append(f'{summary["processed"]} converted')
        if summary["skipped"]: parts.append(f'{summary["skipped"]} skipped')
        if summary["failed"]: parts.append(f'{summary["failed"]} failed')
        summary_str = ", ".join(parts) if parts else "no eligible files"
        status = "completed" if summary["failed"] == 0 else "completed"
        activity.flush()
        activity.finish_job(act_job_id, status, summary=summary_str)

    # Update notification with final result
    if notif_id:
        parts = []
        if summary["processed"] > 0:
            parts.append(f'{summary["processed"]} converted')
        if summary["skipped"] > 0:
            parts.append(f'{summary["skipped"]} skipped')
        if summary["failed"] > 0:
            parts.append(f'{summary["failed"]} failed')
        result_msg = ", ".join(parts) if parts else "no eligible files"
        ntype = "warning" if summary["failed"] > 0 else "success"
        db.update_notification(notif_id, message=f'Processing "{archive_name}": {result_msg}', type=ntype, progress=None)
        _broadcast("notification_updated", db.get_notification(notif_id))

    _broadcast("processing_progress", {
        "archive_id": archive_id,
        "phase": "done",
        "summary": summary,
    })
