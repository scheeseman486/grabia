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

    # Populate processing_queue with file-level entries
    _populate_processing_queue(job_id, archive_id, profile_id, file_ids, options_override)

    # Create an activity job to track this processing run
    archive = db.get_archive(archive_id)
    group_id = archive["group_id"] if archive else None
    act_job_id = activity.start_job(
        "processing", archive_id=archive_id, group_id=group_id,
        processing_job_id=job_id,
    )

    pending = db.count_pending_processing_jobs()

    # Log the queuing event so it shows up in the Activity Log
    archive_name = archive["title"] or archive["identifier"] if archive else f"Archive #{archive_id}"
    activity.log(act_job_id, "info",
                 f"Queued \"{archive_name}\" for processing",
                 archive_id=archive_id)
    activity.flush()

    # Flash notification for processing queued
    notif_id = db.create_notification(
        f'Queued "{archive_name}" for processing',
        type="info", job_id=act_job_id,
    )

    # Link the notification back to the activity job
    activity.update_job_notification(act_job_id, notif_id)

    _broadcast("notification_created", db.get_notification(notif_id))

    # Wake the worker thread
    _wake_event.set()

    return True, pending > 1


def cancel_current_processing():
    """Cancel whatever is currently being processed (if anything).
    Used by the 'Cancel and Remove All' action."""
    with _processing_lock:
        for archive_id, evt in _cancel_events.items():
            evt.set()


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
    job = db.get_active_processing_job_for_archive(archive_id)
    if job:
        db.cancel_processing_job(job["id"])
        # Cancel all pending queue entries for this job so they don't get
        # picked up one-by-one by the worker loop.
        _cancel_job_queue_entries(job["id"], archive_id)
        cancelled = True

    if cancelled:
        # Flash notification for cancellation
        db.create_notification(f'Processing cancelled', type="warning")
        # Close any running activity job for this archive.
        _cancel_activity_job(archive_id)
        # Clean up cancel event
        with _processing_lock:
            _cancel_events.pop(archive_id, None)

    return cancelled


def _cancel_job_queue_entries(job_id, archive_id):
    """Cancel all pending queue entries for a job and reset their file statuses."""
    with db._db() as conn:
        # Get pending entries to reset file statuses
        pending = conn.execute(
            "SELECT file_id FROM processing_queue WHERE job_id = ? AND status = 'pending'",
            (job_id,),
        ).fetchall()
        for row in pending:
            db.set_file_processing_status(row["file_id"], "cancelled", error="Cancelled")
        # Bulk cancel queue entries
        conn.execute(
            "UPDATE processing_queue SET status = 'cancelled', updated_at = ? WHERE job_id = ? AND status = 'pending'",
            (time.time(), job_id),
        )
        conn.commit()
    # Notify frontend to remove them from queue display
    _broadcast("queue_update", {
        "queue_type": "processing", "action": "removed",
        "data": {"cancelled": len(pending)},
    })


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


def _populate_processing_queue(job_id, archive_id, profile_id, file_ids, options_override):
    """Pre-populate processing_queue entries for a job so they appear in the queue page."""
    from processors import get_processor

    profile = db.get_processing_profile(profile_id)
    if not profile:
        return

    processor_cls = get_processor(profile["processor_type"])
    if not processor_cls:
        return

    # Get eligible files (same logic as _run_processing)
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

    # Filter to compatible files
    input_exts = set(processor_cls.input_extensions)
    files = [f for f in files if os.path.splitext(f["name"])[1].lower() in input_exts]

    if not files:
        return

    # Build entries
    options_json = {**(json.loads(profile.get("options_json", "{}")) if profile.get("options_json") else {}),
                    **(options_override or {})}
    entries = [(f["id"], archive_id, profile_id, options_json) for f in files]
    db.add_processing_queue_entries_batch(job_id, entries)

    # Mark all files as queued so the file list shows correct status immediately
    for f in files:
        db.set_file_processing_status(f["id"], "queued", processor_type=profile["processor_type"])

    _broadcast("queue_changed", {"queue_type": "processing", "count": len(entries), "archive_id": archive_id})


def _broadcast(event, data):
    if _sse_broadcaster:
        _sse_broadcaster(event, data)


# ---------------------------------------------------------------------------
# Worker loop — polls DB for pending jobs
# ---------------------------------------------------------------------------

def _build_job_context(job):
    """Load and validate all resources needed to process files for a job.

    Returns a context dict, or None if the job can't run (profile/archive
    missing, etc.).  Caller is responsible for failing/skipping the job.
    """
    archive_id = job["archive_id"]
    profile_id = job["profile_id"]
    options_override = json.loads(job["options_override_json"]) if job.get("options_override_json") else {}

    act_job_id = _find_activity_job(job["id"])

    archive = db.get_archive(archive_id)
    if not archive:
        if act_job_id:
            activity.log(act_job_id, "error", "Archive not found", archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary="Archive not found")
        db.complete_processing_job(job["id"], error_message="Archive not found")
        return None

    archive_name = archive["title"] or archive["identifier"]

    profile = db.get_processing_profile(profile_id)
    if not profile:
        msg = "Processing profile not found"
        if act_job_id:
            activity.log(act_job_id, "error", msg, archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary=msg)
        _broadcast("processing_progress", {
            "archive_id": archive_id, "phase": "error", "error": msg,
        })
        db.complete_processing_job(job["id"], error_message=msg)
        return None

    processor_cls = get_processor(profile["processor_type"])
    if not processor_cls:
        msg = f"Unknown processor type: {profile['processor_type']}"
        if act_job_id:
            activity.log(act_job_id, "error", msg, archive_id=archive_id)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary=msg)
        _broadcast("processing_progress", {
            "archive_id": archive_id, "phase": "error", "error": msg,
        })
        db.complete_processing_job(job["id"], error_message=msg)
        return None

    profile_options = json.loads(profile.get("options_json", "{}"))
    merged_options = {**profile_options, **options_override}

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    archive_dir = os.path.join(download_dir, archive["identifier"])

    return {
        "job": job,
        "job_id": job["id"],
        "archive_id": archive_id,
        "archive_name": archive_name,
        "profile": profile,
        "processor_cls": processor_cls,
        "merged_options": merged_options,
        "archive_dir": archive_dir,
        "act_job_id": act_job_id,
    }


def _process_single_entry(entry, ctx):
    """Process one processing_queue entry using the cached job context."""
    job_id = ctx["job_id"]
    archive_id = ctx["archive_id"]
    act_job_id = ctx["act_job_id"]
    merged_options = ctx["merged_options"]
    processor_cls = ctx["processor_cls"]
    profile = ctx["profile"]
    archive_dir = ctx["archive_dir"]

    file_id = entry["file_id"]
    file_info = db.get_file(file_id)
    if not file_info:
        db.complete_processing_queue_entry(entry["id"], error_message="File not found")
        _broadcast("queue_update", {
            "queue_type": "processing", "action": "completed",
            "entry_id": entry["id"],
        })
        return

    filename = file_info["name"]
    file_path = os.path.join(archive_dir, filename)

    # Claim the queue entry
    if not db.claim_processing_queue_entry(entry["id"]):
        return  # Already claimed
    _broadcast("queue_update", {
        "queue_type": "processing", "action": "status_changed",
        "entry_id": entry["id"], "status": "running",
    })

    db.set_file_processing_status(file_id, "processing")

    cancel_evt = _cancel_events.get(archive_id)

    def _cancelled():
        return cancel_evt and cancel_evt.is_set()

    def progress_cb(**kwargs):
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "file_id": file_id,
            "filename": filename,
            **kwargs,
        })

    try:
        log.debug("worker", "Processing file: %s (entry %d)", filename, entry["id"])
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
            db.set_file_processing_status(file_id, "skipped", error=reason)
            if act_job_id:
                activity.log(act_job_id, "info", f"Skipped: {filename}",
                             archive_id=archive_id, file_id=file_id,
                             detail=reason)
        else:
            delete_original = merged_options.get("delete_original", "yes") == "yes"
            if delete_original:
                for to_delete in result.get("files_to_delete", []):
                    try:
                        os.remove(to_delete)
                    except OSError:
                        pass
                with db._db() as conn:
                    conn.execute(
                        "UPDATE archive_files SET downloaded = 0 WHERE id = ?",
                        (file_id,),
                    )
                    conn.commit()

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

        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "file_id": file_id,
            "filename": filename,
            "phase": "file_done",
            "skipped": result.get("skipped", False),
        })

        db.complete_processing_queue_entry(entry["id"])
        _broadcast("queue_update", {
            "queue_type": "processing", "action": "completed",
            "entry_id": entry["id"],
        })

    except ProcessingCancelled:
        db.set_file_processing_status(file_id, "cancelled", error="Cancelled")
        db.cancel_processing_queue_entry(entry["id"])
        if act_job_id:
            activity.log(act_job_id, "warning",
                         f"Cancelled during: {filename}",
                         archive_id=archive_id, file_id=file_id)
            activity.flush()
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "phase": "cancelled",
        })

    except (ProcessingError, Exception) as e:
        is_proc_err = isinstance(e, ProcessingError)
        if is_proc_err:
            log.error("worker", "Failed %s: %s", filename, e)
        db.set_file_processing_status(file_id, "failed", error=str(e))
        db.complete_processing_queue_entry(entry["id"], error_message=str(e))
        _broadcast("queue_update", {
            "queue_type": "processing", "action": "status_changed",
            "entry_id": entry["id"], "status": "failed",
        })
        if act_job_id:
            activity.log(act_job_id, "error", f"Failed: {filename}",
                         archive_id=archive_id, file_id=file_id,
                         detail=str(e))
            activity.flush()
        _broadcast("processing_progress", {
            "archive_id": archive_id,
            "file_id": file_id,
            "filename": filename,
            "phase": "file_error",
            "error": str(e),
        })


def _handle_cancellation(entry, ctx):
    """Cancel a single queue entry when the job's cancel event is set."""
    db.set_file_processing_status(entry["file_id"], "cancelled", error="Cancelled")
    db.cancel_processing_queue_entry(entry["id"])
    _broadcast("queue_update", {
        "queue_type": "processing", "action": "completed",
        "entry_id": entry["id"],
    })


def _finalise_job(job_id, ctx, job_cache):
    """Finalise a job when all its queue entries are done."""
    archive_id = ctx["archive_id"]
    archive_name = ctx["archive_name"]
    act_job_id = ctx["act_job_id"]

    counts = db.count_total_queue_entries_for_job(job_id)

    db.complete_processing_job(job_id)

    # Clean up cancel event
    with _processing_lock:
        _cancel_events.pop(archive_id, None)

    # Remove from context cache
    job_cache.pop(job_id, None)

    # Build summary
    parts = []
    if counts["completed"] > 0:
        parts.append(f'{counts["completed"]} converted')
    if counts["failed"] > 0:
        parts.append(f'{counts["failed"]} failed')
    if counts["cancelled"] > 0:
        parts.append(f'{counts["cancelled"]} cancelled')
    summary_str = ", ".join(parts) if parts else "no eligible files"

    if act_job_id:
        activity.flush()
        status = "cancelled" if counts["cancelled"] > 0 and counts["completed"] == 0 else "completed"
        activity.finish_job(act_job_id, status, summary=summary_str)

    ntype = "warning" if counts["failed"] > 0 or counts["cancelled"] > 0 else "success"
    result_notif_id = db.create_notification(
        f'Processing "{archive_name}": {summary_str}', type=ntype,
    )
    _broadcast("notification_created", db.get_notification(result_notif_id))

    log.info("worker", "Processing job %d complete: %s", job_id, summary_str)

    _broadcast("processing_progress", {
        "archive_id": archive_id,
        "phase": "done",
        "summary": {"processed": counts["completed"], "failed": counts["failed"]},
    })


def _finalise_empty_jobs():
    """Check for running jobs that have no remaining queue entries and finalise them."""
    jobs = db.get_processing_jobs(status="running")
    for job in jobs:
        remaining = db.count_pending_queue_entries_for_job(job["id"])
        if remaining == 0:
            ctx = _build_job_context(job)
            if ctx:
                _finalise_job(job["id"], ctx, {})
            else:
                db.complete_processing_job(job["id"])
                with _processing_lock:
                    _cancel_events.pop(job["archive_id"], None)


def _worker_loop():
    # Cache of job contexts keyed by job_id, so we don't re-load profile/archive
    # for every single file in the same job.
    _job_ctx = {}  # job_id -> dict

    while True:
        # Check pause state
        if db.get_setting("processing_paused", "0") == "1":
            _wake_event.wait(timeout=2)
            _wake_event.clear()
            continue

        # Pick the next pending entry by global queue position
        entry = db.get_next_processing_queue_entry()
        if not entry:
            # No file-level work.  Check for jobs that have no queue entries
            # (e.g. zero eligible files) so they can be finalised.
            _finalise_empty_jobs()
            _wake_event.wait(timeout=5)
            _wake_event.clear()
            continue

        job_id = entry["job_id"]

        # Ensure the parent job is claimed (running).  It may still be 'pending'
        # if this is the first entry we pick from it.
        job = db.get_processing_job(job_id)
        if not job or job["status"] not in ("pending", "running"):
            # Job was cancelled or removed — skip this entry
            db.cancel_processing_queue_entry(entry["id"])
            _broadcast("queue_update", {
                "queue_type": "processing", "action": "completed",
                "entry_id": entry["id"],
            })
            continue

        if job["status"] == "pending":
            if not db.claim_processing_job(job_id):
                continue
            job = db.get_processing_job(job_id)

        archive_id = job["archive_id"]

        # Ensure a cancel event exists for this archive
        with _processing_lock:
            if archive_id not in _cancel_events:
                _cancel_events[archive_id] = threading.Event()

        # Get or build job context
        ctx = _job_ctx.get(job_id)
        if not ctx:
            ctx = _build_job_context(job)
            if ctx is None:
                # Invalid job (missing profile/archive/processor) — already
                # handled inside _build_job_context which fails the job.
                db.cancel_processing_queue_entry(entry["id"])
                _broadcast("queue_update", {
                    "queue_type": "processing", "action": "completed",
                    "entry_id": entry["id"],
                })
                continue
            _job_ctx[job_id] = ctx

        # Check cancellation before starting
        cancel_evt = _cancel_events.get(archive_id)
        if cancel_evt and cancel_evt.is_set():
            _handle_cancellation(entry, ctx)
            _finalise_job(job_id, ctx, _job_ctx)
            continue

        # Process this single file
        try:
            _process_single_entry(entry, ctx)
        except Exception as e:
            log.error("worker", "Unexpected error processing entry %d: %s", entry["id"], e)
            db.set_file_processing_status(entry["file_id"], "failed", error=str(e))
            db.complete_processing_queue_entry(entry["id"], error_message=str(e))
            _broadcast("queue_update", {
                "queue_type": "processing", "action": "status_changed",
                "entry_id": entry["id"], "status": "failed",
            })
            if ctx["act_job_id"]:
                activity.log(ctx["act_job_id"], "error",
                             f"Failed: {entry.get('file_name', 'unknown')}",
                             archive_id=archive_id, file_id=entry["file_id"],
                             detail=str(e))
                activity.flush()

        # Check if this job is now complete (no more pending entries)
        remaining = db.count_pending_queue_entries_for_job(job_id)
        if remaining == 0:
            _finalise_job(job_id, ctx, _job_ctx)


def _reset_stuck_files(archive_id):
    """Reset files stuck in 'processing' or 'queued' for this archive after a crash."""
    try:
        with db._db() as conn:
            conn.execute(
                "UPDATE archive_files SET processing_status = '', processing_error = 'Interrupted by error' "
                "WHERE archive_id = ? AND processing_status IN ('processing', 'queued')",
                (archive_id,),
            )
            conn.commit()
    except Exception:
        pass


def _fail_activity_job(processing_job_id, error_msg):
    """Close the activity job linked to a processing job as failed."""
    try:
        act_job_id = _find_activity_job(processing_job_id)
        if act_job_id:
            activity.log(act_job_id, "error", f"Processing crashed: {error_msg[:200]}",
                         archive_id=None)
            activity.flush()
            activity.finish_job(act_job_id, "failed", summary=f"Crashed: {error_msg[:100]}")
    except Exception:
        pass


def _dismiss_processing_notification(archive_id, error_msg):
    """Update the processing notification for a crashed job and release it.

    Creates a flash error notification for the failure.
    """
    try:
        archive = db.get_archive(archive_id)
        name = (archive["title"] or archive["identifier"]) if archive else f"Archive #{archive_id}"
        notif_id = db.create_notification(
            f'Processing "{name}" failed: {error_msg[:150]}',
            type="error",
        )
        _broadcast("notification_created", db.get_notification(notif_id))
    except Exception:
        pass


def _find_activity_job(processing_job_id):
    """Look up the activity job linked to a processing job."""
    with db._db() as conn:
        row = conn.execute(
            "SELECT id FROM activity_jobs WHERE processing_job_id = ?",
            (processing_job_id,),
        ).fetchone()
        return row["id"] if row else None


    # _run_processing has been replaced by the entry-at-a-time loop above.
