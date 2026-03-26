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

"""Activity log for Grabia.

Provides a structured, user-facing event log that records scan, processing,
download, and collection events.  Entries are written in batches to avoid
per-file DB round-trips in tight loops.

Usage::

    job_id = activity.start_job("processing", archive_id=1, group_id=None)
    activity.log(job_id, "info", "Processing started", archive_id=1)
    activity.log(job_id, "error", "Failed Aardvark.zip", archive_id=1,
                 file_id=42, detail="chdman exit code 5 ...")
    activity.flush()  # always call at end of job
    activity.finish_job(job_id, "completed", summary="3 converted, 1 failed")
"""

import time
import threading
import database as db


# Buffer for batched inserts — one per thread (processing and scan run
# on separate threads).
_buffers = threading.local()
BATCH_SIZE = 100


def _get_buffer():
    """Return the thread-local buffer, creating it if needed."""
    if not hasattr(_buffers, "entries"):
        _buffers.entries = []
    return _buffers.entries


# ── Jobs ─────────────────────────────────────────────────────────────────

def start_job(category, archive_id=None, group_id=None,
              processing_job_id=None, notification_id=None):
    """Create a new activity job and return its ID.

    Parameters
    ----------
    category : str
        ``'scan'``, ``'processing'``, ``'download'``, or ``'collection'``.
    archive_id : int, optional
        The archive this job relates to.
    group_id : int, optional
        The group this archive belongs to (denormalised for filtering).
    processing_job_id : int, optional
        Links to ``processing_jobs.id`` when this is a processing job.
    notification_id : int, optional
        Links to ``notifications.id`` for the "View Log" link.
    """
    with db._db() as conn:
        cur = conn.execute(
            """INSERT INTO activity_jobs
               (category, archive_id, group_id, processing_job_id,
                notification_id, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'running')""",
            (category, archive_id, group_id, processing_job_id,
             notification_id, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def finish_job(job_id, status="completed", summary=None):
    """Mark a job as completed/failed/cancelled with an optional summary."""
    with db._db() as conn:
        conn.execute(
            """UPDATE activity_jobs
               SET status = ?, completed_at = ?, summary = ?
               WHERE id = ?""",
            (status, time.time(), summary, job_id),
        )
        conn.commit()


def get_job(job_id):
    """Return a single activity job dict, or None."""
    with db._db() as conn:
        row = conn.execute(
            "SELECT * FROM activity_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def update_job_notification(job_id, notification_id):
    """Set the notification_id on an existing job (for when notification
    is created after the job)."""
    with db._db() as conn:
        conn.execute(
            "UPDATE activity_jobs SET notification_id = ? WHERE id = ?",
            (notification_id, job_id),
        )
        conn.commit()


# ── Log entries ──────────────────────────────────────────────────────────

def log(job_id, level, message, archive_id=None, file_id=None,
        detail=None, category=None):
    """Buffer a log entry.  Call :func:`flush` to write to DB.

    Parameters
    ----------
    job_id : int
        The activity job this entry belongs to.
    level : str
        ``'info'``, ``'warning'``, ``'error'``, or ``'success'``.
    message : str
        Short, human-readable description of the event.
    archive_id : int, optional
    file_id : int, optional
    detail : str, optional
        Longer context — stack traces, exit codes, paths, etc.
    category : str, optional
        Override the job's category for this entry (rarely needed).
    """
    buf = _get_buffer()
    buf.append((
        time.time(),    # timestamp
        category,       # category (NULL → resolved from job at query time)
        level,
        job_id,
        archive_id,
        file_id,
        message,
        detail,
    ))
    if len(buf) >= BATCH_SIZE:
        flush()


def flush():
    """Write all buffered log entries to the database in one transaction."""
    buf = _get_buffer()
    if not buf:
        return
    with db._db() as conn:
        conn.executemany(
            """INSERT INTO activity_log
               (timestamp, category, level, job_id, archive_id, file_id,
                message, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            buf,
        )
        conn.commit()
    buf.clear()


# ── Queries ──────────────────────────────────────────────────────────────

def get_log_entries(job_id=None, archive_id=None, group_id=None,
                    category=None, level=None, search=None,
                    limit=200, offset=0):
    """Return activity log entries with optional filters.

    Joins to ``activity_jobs`` to resolve category (when NULL on the entry)
    and to ``archives`` for group filtering.
    """
    conditions = []
    params = []

    if job_id is not None:
        conditions.append("al.job_id = ?")
        params.append(job_id)
    if archive_id is not None:
        conditions.append("al.archive_id = ?")
        params.append(archive_id)
    if group_id is not None:
        conditions.append("a.group_id = ?")
        params.append(group_id)
    if category:
        conditions.append("COALESCE(al.category, aj.category) = ?")
        params.append(category)
    if level:
        if level == "errors":
            conditions.append("al.level IN ('error', 'warning')")
        else:
            conditions.append("al.level = ?")
            params.append(level)
    if search:
        conditions.append("(al.message LIKE ? OR al.detail LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])

    with db._db() as conn:
        rows = conn.execute(
            f"""SELECT al.*,
                       COALESCE(al.category, aj.category) AS resolved_category,
                       a.identifier AS archive_identifier,
                       a.title AS archive_title,
                       a.group_id AS archive_group_id
                FROM activity_log al
                LEFT JOIN activity_jobs aj ON al.job_id = aj.id
                LEFT JOIN archives a ON al.archive_id = a.id
                {where}
                ORDER BY al.timestamp DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_log_count(job_id=None, archive_id=None, group_id=None,
                  category=None, level=None, search=None):
    """Return the total count matching the same filters as get_log_entries."""
    conditions = []
    params = []

    if job_id is not None:
        conditions.append("al.job_id = ?")
        params.append(job_id)
    if archive_id is not None:
        conditions.append("al.archive_id = ?")
        params.append(archive_id)
    if group_id is not None:
        conditions.append("a.group_id = ?")
        params.append(group_id)
    if category:
        conditions.append("COALESCE(al.category, aj.category) = ?")
        params.append(category)
    if level:
        if level == "errors":
            conditions.append("al.level IN ('error', 'warning')")
        else:
            conditions.append("al.level = ?")
            params.append(level)
    if search:
        conditions.append("(al.message LIKE ? OR al.detail LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    with db._db() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS cnt
                FROM activity_log al
                LEFT JOIN activity_jobs aj ON al.job_id = aj.id
                LEFT JOIN archives a ON al.archive_id = a.id
                {where}""",
            params,
        ).fetchone()
        return row["cnt"]


def get_jobs(category=None, archive_id=None, limit=50, offset=0):
    """Return recent activity jobs with archive info."""
    conditions = []
    params = []
    if category:
        conditions.append("aj.category = ?")
        params.append(category)
    if archive_id is not None:
        conditions.append("aj.archive_id = ?")
        params.append(archive_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    with db._db() as conn:
        rows = conn.execute(
            f"""SELECT aj.*,
                       a.identifier AS archive_identifier,
                       a.title AS archive_title
                FROM activity_jobs aj
                LEFT JOIN archives a ON aj.archive_id = a.id
                {where}
                ORDER BY aj.started_at DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


# ── Maintenance ──────────────────────────────────────────────────────────

def prune(max_age_days=30):
    """Delete activity log entries and jobs older than max_age_days."""
    cutoff = time.time() - (max_age_days * 86400)
    with db._db() as conn:
        conn.execute("DELETE FROM activity_log WHERE timestamp < ?", (cutoff,))
        conn.execute(
            "DELETE FROM activity_jobs WHERE completed_at IS NOT NULL AND completed_at < ?",
            (cutoff,),
        )
        conn.commit()
