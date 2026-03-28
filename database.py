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

"""SQLite database layer for Grabia."""

import sqlite3
import os
import json
import time
import threading
import logging
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger(__name__)

# Per-queue locks used by compaction and get_next_*() functions to prevent
# workers from picking a stale position mid-compaction.
_download_queue_lock = threading.Lock()
_processing_queue_lock = threading.Lock()
_scan_queue_lock = threading.Lock()

_DATA_DIR = os.environ.get("GRABIA_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DATA_DIR, "grabia.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db():
    """Context manager that ensures the connection is always closed."""
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            total_size INTEGER NOT NULL DEFAULT 0,
            files_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 0,
            download_enabled INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle',
            added_at REAL NOT NULL,
            server TEXT NOT NULL DEFAULT '',
            dir TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS archive_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            md5 TEXT NOT NULL DEFAULT '',
            sha1 TEXT NOT NULL DEFAULT '',
            format TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            mtime TEXT NOT NULL DEFAULT '',
            queued INTEGER NOT NULL DEFAULT 1,
            download_status TEXT NOT NULL DEFAULT 'pending',
            downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            change_status TEXT NOT NULL DEFAULT '',
            change_detail TEXT NOT NULL DEFAULT '',
            queue_position INTEGER DEFAULT NULL,
            downloaded INTEGER NOT NULL DEFAULT 0,
            download_batch_id INTEGER DEFAULT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
            UNIQUE(archive_id, name)
        );

        CREATE TABLE IF NOT EXISTS download_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            timestamp REAL NOT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
            FOREIGN KEY (file_id) REFERENCES archive_files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS processing_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            processor_type TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'info',
            created_at REAL NOT NULL,
            progress REAL DEFAULT NULL,
            scan_archive_id INTEGER DEFAULT NULL,
            processing_archive_id INTEGER DEFAULT NULL,
            adding_archive INTEGER NOT NULL DEFAULT 0,
            dismissed INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS processing_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            file_ids_json TEXT DEFAULT NULL,
            options_override_json TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT NOT NULL DEFAULT '',
            started_at REAL DEFAULT NULL,
            completed_at REAL DEFAULT NULL,
            created_at REAL NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id) REFERENCES processing_profiles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            file_scope TEXT NOT NULL DEFAULT 'processed',
            auto_tag TEXT DEFAULT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            UNIQUE(archive_id, tag),
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS collection_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER NOT NULL,
            archive_id INTEGER NOT NULL,
            UNIQUE(collection_id, archive_id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS collection_layouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'flat',
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS activity_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            archive_id INTEGER DEFAULT NULL,
            group_id INTEGER DEFAULT NULL,
            processing_job_id INTEGER DEFAULT NULL,
            notification_id INTEGER DEFAULT NULL,
            started_at REAL NOT NULL,
            completed_at REAL DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            category TEXT DEFAULT NULL,
            level TEXT NOT NULL,
            job_id INTEGER DEFAULT NULL,
            archive_id INTEGER DEFAULT NULL,
            file_id INTEGER DEFAULT NULL,
            message TEXT NOT NULL,
            detail TEXT DEFAULT NULL,
            FOREIGN KEY (job_id) REFERENCES activity_jobs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_activity_log_job ON activity_log(job_id);
        CREATE INDEX IF NOT EXISTS idx_activity_log_archive ON activity_log(archive_id);
        CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log(timestamp);

        -- Queue overhaul: generic batch tracking for all queue types
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_type TEXT NOT NULL,
            archive_id INTEGER DEFAULT NULL,
            created_at REAL NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        -- Queue overhaul: file-level processing queue (child of processing_jobs)
        CREATE TABLE IF NOT EXISTS processing_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES processing_jobs(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES archive_files(id) ON DELETE CASCADE,
            archive_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            options_json TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            position INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_processing_queue_status ON processing_queue(status, position);

        -- Queue overhaul: file-level scan queue (replaces Python queue.Queue)
        CREATE TABLE IF NOT EXISTS scan_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES archive_files(id) ON DELETE CASCADE,
            archive_id INTEGER NOT NULL,
            batch_id INTEGER DEFAULT NULL REFERENCES batches(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            position INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scan_queue_status ON scan_queue(status, position);
    """)

    # Default settings
    defaults = {
        "ia_email": "",
        "ia_password": "",
        "download_dir": os.path.expanduser("~/ia-downloads"),
        "max_retries": "3",
        "retry_delay": "5",
        "bandwidth_limit": "0",
        "theme": "dark",
        "files_per_page": "50",
        "sse_update_rate": "500",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    # Migrations for existing databases
    try:
        conn.execute("SELECT change_status FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN change_status TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE archive_files ADD COLUMN change_detail TEXT NOT NULL DEFAULT ''")

    # download_priority column is deprecated (replaced by queue_position).
    # Existing databases may still have it — that's fine, it's simply unused.
    # We no longer create it for new databases.

    try:
        conn.execute("SELECT group_id FROM archives LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archives ADD COLUMN group_id INTEGER DEFAULT NULL REFERENCES archive_groups(id) ON DELETE SET NULL")

    try:
        conn.execute("SELECT origin FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN origin TEXT NOT NULL DEFAULT 'manifest'")

    # Processing pipeline columns on archive_files
    try:
        conn.execute("SELECT processing_status FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN processing_status TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE archive_files ADD COLUMN processed_filename TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE archive_files ADD COLUMN processor_type TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE archive_files ADD COLUMN processing_error TEXT NOT NULL DEFAULT ''")

    # Multi-file processing output tracking (e.g. extraction)
    try:
        conn.execute("SELECT processed_files_json FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN processed_files_json TEXT NOT NULL DEFAULT ''")

    # Processing profile FK on archives
    try:
        conn.execute("SELECT processing_profile_id FROM archives LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archives ADD COLUMN processing_profile_id INTEGER DEFAULT NULL REFERENCES processing_profiles(id) ON DELETE SET NULL")

    # Rename 'selected' column to 'queued' (clearer intent, avoids confusion
    # with UI checkbox selection).  ALTER TABLE ... RENAME COLUMN is safe and
    # non-destructive — it keeps all data, indexes, and constraints intact.
    try:
        conn.execute("SELECT queued FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        # Column still has the old name — rename it
        try:
            conn.execute("ALTER TABLE archive_files RENAME COLUMN selected TO queued")
        except sqlite3.OperationalError:
            pass  # Shouldn't happen, but don't break startup

    # Add job_id to notifications for "View Log" linkage
    try:
        conn.execute("SELECT job_id FROM notifications LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE notifications ADD COLUMN job_id INTEGER DEFAULT NULL")

    # --- Queue overhaul migrations ---

    # Add queue_position column (replaces queued + download_priority)
    try:
        conn.execute("SELECT queue_position FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN queue_position INTEGER DEFAULT NULL")
        # Migrate from old queued + download_priority system if the columns exist
        has_download_priority = False
        try:
            conn.execute("SELECT download_priority FROM archive_files LIMIT 1")
            has_download_priority = True
        except sqlite3.OperationalError:
            pass
        if has_download_priority:
            conn.execute("""
                UPDATE archive_files SET queue_position = (
                    SELECT rn FROM (
                        SELECT af.id,
                               ROW_NUMBER() OVER (
                                   ORDER BY a.position ASC, af.download_priority ASC
                               ) AS rn
                        FROM archive_files af
                        JOIN archives a ON af.archive_id = a.id
                        WHERE af.queued = 1
                          AND af.download_status NOT IN ('completed', 'conflict')
                    ) AS t
                    WHERE t.id = archive_files.id
                )
                WHERE queued = 1
                  AND download_status NOT IN ('completed', 'conflict')
            """)
            log.info("Migrated queued files to queue_position column")

    # Add downloaded boolean column
    try:
        conn.execute("SELECT downloaded FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN downloaded INTEGER NOT NULL DEFAULT 0")
        # Migrate: set downloaded=1 for all completed files
        conn.execute("UPDATE archive_files SET downloaded = 1 WHERE download_status = 'completed'")
        log.info("Migrated downloaded column from download_status")

    # Add download_batch_id column
    try:
        conn.execute("SELECT download_batch_id FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN download_batch_id INTEGER DEFAULT NULL")

    # Add manifest cache columns to archives
    try:
        conn.execute("SELECT manifest_json FROM archives LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archives ADD COLUMN manifest_json TEXT DEFAULT NULL")
        conn.execute("ALTER TABLE archives ADD COLUMN manifest_fetched_at REAL DEFAULT NULL")

    # Queue state settings (download_state, processing_paused, scan_paused)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('download_state', 'stopped')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('processing_paused', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('scan_paused', '0')")

    # Fix any scan-inserted rows incorrectly tagged as 'manifest'.
    # Real IA files always have at least one metadata field populated;
    # scan-inserted files have md5, sha1, format, source, and mtime all empty.
    conn.execute("""
        UPDATE archive_files SET origin = 'scan'
        WHERE origin = 'manifest'
          AND md5 = '' AND sha1 = '' AND format = '' AND source = '' AND mtime = ''
    """)

    # Dequeue files that have already completed or hit a conflict — they should
    # not remain queued since they don't need downloading.
    conn.execute("""
        UPDATE archive_files SET queued = 0
        WHERE queued = 1 AND download_status IN ('completed', 'conflict')
    """)
    # Also clear queue_position for completed/conflict files
    conn.execute("""
        UPDATE archive_files SET queue_position = NULL
        WHERE queue_position IS NOT NULL AND download_status IN ('completed', 'conflict')
    """)

    # Fix archives stuck on 'downloading' or 'queued' from a previous session.
    # On startup the downloader is not running, so no files can be in-flight.
    # Recalculate status for any archive marked 'downloading' or 'queued'.
    stuck = conn.execute(
        "SELECT id FROM archives WHERE status IN ('downloading', 'queued')"
    ).fetchall()
    for row in stuck:
        aid = row["id"]
        counts = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN download_status = 'conflict' THEN 1 ELSE 0 END) as conflict
            FROM archive_files
            WHERE archive_id = ? AND (queue_position IS NOT NULL OR download_status IN ('completed', 'conflict'))
        """, (aid,)).fetchone()
        if counts["total"] == 0:
            conn.execute("UPDATE archives SET status = 'idle' WHERE id = ?", (aid,))
        elif counts["completed"] == counts["total"]:
            conn.execute("UPDATE archives SET status = 'completed' WHERE id = ?", (aid,))
        elif counts["completed"] + counts["failed"] + counts["conflict"] == counts["total"]:
            conn.execute("UPDATE archives SET status = 'partial' WHERE id = ?", (aid,))
        else:
            conn.execute("UPDATE archives SET status = 'idle' WHERE id = ?", (aid,))

    # Reset scan queue entries stuck from a crash
    conn.execute("UPDATE scan_queue SET status = 'pending' WHERE status = 'running'")
    # Reset processing queue entries stuck from a crash
    conn.execute("UPDATE processing_queue SET status = 'pending' WHERE status = 'running'")

    # Reset interrupted processing jobs back to pending so the worker picks them up.
    # Jobs stuck in 'running' mean the server crashed mid-processing.
    conn.execute("UPDATE processing_jobs SET status = 'pending', started_at = NULL WHERE status = 'running'")

    # Reset archive_files stuck in processing states from a crash.
    # Files marked 'queued' or 'processing' need to be reset so they can be
    # re-queued when the recovered job runs.
    conn.execute("""
        UPDATE archive_files SET processing_status = '', processing_error = ''
        WHERE processing_status IN ('queued', 'processing')
    """)

    # Migrate legacy processing statuses: 'completed' and 'extracted' → 'processed'
    conn.execute("""
        UPDATE archive_files SET processing_status = 'processed'
        WHERE processing_status IN ('completed', 'extracted')
    """)

    # Clean up stale in-progress notifications (scan/processing that were mid-flight)
    conn.execute("""
        DELETE FROM notifications
        WHERE dismissed = 0
          AND (scan_archive_id IS NOT NULL OR processing_archive_id IS NOT NULL OR adding_archive = 1)
          AND progress IS NOT NULL
    """)

    conn.commit()
    conn.close()


# --- Settings ---

def get_setting(key, default=None):
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def get_all_settings():
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def set_setting(key, value):
    with _db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, str(value), str(value)),
        )
        conn.commit()


# --- Archives ---

def add_archive(identifier, url, title, description, total_size, files_count, metadata_json, server, dir_path):
    with _db() as conn:
        conn.execute(
            """INSERT INTO archives (identifier, url, title, description, total_size, files_count,
               metadata_json, position, download_enabled, status, added_at, server, dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM archives), 0, 'idle', ?, ?, ?)""",
            (identifier, url, title, description, total_size, files_count,
             json.dumps(metadata_json), time.time(), server, dir_path),
        )
        archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return archive_id


def add_archive_files(archive_id, files):
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Get global max queue_position for new files that will be queued
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(queue_position), 0) FROM archive_files WHERE queue_position IS NOT NULL"
            ).fetchone()[0]
            for i, f in enumerate(files):
                new_pos = max_pos + 1 + i
                conn.execute(
                    """INSERT OR IGNORE INTO archive_files
                       (archive_id, name, size, md5, sha1, format, source, mtime,
                        queued, queue_position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (archive_id, f["name"], int(f.get("size", 0) or 0),
                     f.get("md5", ""), f.get("sha1", ""), f.get("format", ""),
                     f.get("source", ""), f.get("mtime", ""), new_pos),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _enrich_archives_with_progress(archives, conn):
    """Add download progress stats to a list of archive dicts."""
    if not archives:
        return archives
    ids = [a["id"] for a in archives]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT archive_id,
               SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) AS completed_files,
               SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN 1 ELSE 0 END) AS selected_files,
               SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN size ELSE 0 END) AS selected_size,
               SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN downloaded_bytes ELSE 0 END) AS downloaded_bytes
        FROM archive_files WHERE archive_id IN ({placeholders})
        GROUP BY archive_id
    """, ids).fetchall()
    prog = {r["archive_id"]: dict(r) for r in rows}
    for a in archives:
        p = prog.get(a["id"], {})
        a["completed_files"] = p.get("completed_files", 0)
        a["selected_files"] = p.get("selected_files", 0)
        a["selected_size"] = p.get("selected_size", 0)
        a["downloaded_bytes"] = p.get("downloaded_bytes", 0)
    return archives


def get_archive_progress(archive_id):
    """Return download progress stats for a single archive."""
    with _db() as conn:
        row = conn.execute("""
            SELECT SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) AS completed_files,
                   SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN 1 ELSE 0 END) AS selected_files,
                   SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN size ELSE 0 END) AS selected_size,
                   SUM(CASE WHEN queue_position IS NOT NULL OR download_status = 'completed' THEN downloaded_bytes ELSE 0 END) AS downloaded_bytes
            FROM archive_files WHERE archive_id = ?
        """, (archive_id,)).fetchone()
        if row:
            return dict(row)
        return {"completed_files": 0, "selected_files": 0, "selected_size": 0, "downloaded_bytes": 0}


def get_archives():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM archives ORDER BY download_enabled DESC, position ASC").fetchall()
        archives = [dict(r) for r in rows]
        _enrich_archives_with_progress(archives, conn)
        # Ensure group_id is always present (for older DBs before migration runs mid-session)
        for a in archives:
            a.setdefault("group_id", None)
        return archives


def get_archive(archive_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
        if not row:
            return None
        archive = dict(row)
        _enrich_archives_with_progress([archive], conn)
        return archive


def get_archive_by_identifier(identifier):
    with _db() as conn:
        row = conn.execute("SELECT * FROM archives WHERE identifier = ?", (identifier,)).fetchone()
        return dict(row) if row else None


def delete_archive(archive_id):
    with _db() as conn:
        conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
        conn.commit()


def update_archive_position(archive_id, new_position):
    with _db() as conn:
        conn.execute("UPDATE archives SET position = ? WHERE id = ?", (new_position, archive_id))
        conn.commit()


def reorder_archives(id_order):
    """id_order is a list of archive IDs in desired order."""
    with _db() as conn:
        for pos, aid in enumerate(id_order):
            conn.execute("UPDATE archives SET position = ? WHERE id = ?", (pos, aid))
        conn.commit()


def recompute_archive_file_count(archive_id):
    """Recompute files_count and total_size for an archive from the archive_files table."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(size), 0) as total FROM archive_files WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
        conn.execute(
            "UPDATE archives SET files_count = ?, total_size = ? WHERE id = ?",
            (row["cnt"], row["total"], archive_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recompute_archive_status(archive_id, fallback=None):
    """Recalculate archive status from its file statuses (used after scan)."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN download_status = 'conflict' THEN 1 ELSE 0 END) as conflict
            FROM archive_files
            WHERE archive_id = ? AND (queue_position IS NOT NULL OR download_status IN ('completed', 'conflict'))
        """, (archive_id,)).fetchone()
        if row["total"] == 0:
            status = "idle"
        elif row["completed"] == row["total"]:
            status = "completed"
        elif row["completed"] + row["failed"] + row["conflict"] == row["total"]:
            status = "partial"
        elif row["completed"] > 0 or row["conflict"] > 0:
            status = "idle"
        elif fallback:
            status = fallback
        else:
            status = None
        if status:
            conn.execute("UPDATE archives SET status = ? WHERE id = ?", (status, archive_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_archive_download_enabled(archive_id, enabled):
    with _db() as conn:
        conn.execute("UPDATE archives SET download_enabled = ? WHERE id = ?", (1 if enabled else 0, archive_id))
        conn.commit()


def set_archive_status(archive_id, status):
    with _db() as conn:
        conn.execute("UPDATE archives SET status = ? WHERE id = ?", (status, archive_id))
        conn.commit()


# --- Archive Files ---

_FILE_SORT_MAP = {
    "name": ("name", "ASC"),
    "size": ("size", "DESC"),
    "modified": ("mtime", "DESC"),
    "status": ("download_status", "ASC"),
    "queue_position": ("queue_position", "ASC"),
}

# Effective status mirrors the JS formatFileStatus logic:
# processing_status takes priority, then queue_position+download_status determines "skipped"
_EFFECTIVE_STATUS_EXPR = """CASE
    WHEN processing_status = 'processed' THEN 'processed'
    WHEN processing_status = 'processing' THEN 'processing'
    WHEN processing_status = 'queued' THEN 'proc_queued'
    WHEN processing_status = 'failed' THEN 'proc_failed'
    WHEN processing_status = 'skipped' THEN 'proc_skipped'
    WHEN queue_position IS NULL AND download_status = 'pending' THEN 'skipped'
    ELSE download_status
END"""


def get_archive_files(archive_id, sort="name", sort_dir=None, search=""):
    with _db() as conn:
        col, default_dir = _FILE_SORT_MAP.get(sort, _FILE_SORT_MAP["name"])
        direction = sort_dir.upper() if sort_dir in ("asc", "desc") else default_dir
        if sort == "queue_position":
            # Read-only queue position sort: queued files first by position, then unqueued
            order = f"(queue_position IS NULL) ASC, queue_position {direction}, name ASC"
        elif sort == "status":
            order = f"({_EFFECTIVE_STATUS_EXPR}) {direction}, name ASC"
        else:
            order = f"{col} {direction}"
            if sort != "name":
                order += ", name ASC"
        where = "archive_id = ?"
        params = [archive_id]
        if search:
            where += " AND name LIKE ?"
            params.append(f"%{search}%")
        total = conn.execute(f"SELECT COUNT(*) FROM archive_files WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM archive_files WHERE {where} ORDER BY {order}",
            params,
        ).fetchall()
        return [dict(r) for r in rows], total


def count_unqueued_files(archive_id):
    with _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE archive_id = ? AND queue_position IS NULL",
            (archive_id,),
        ).fetchone()[0]
        return count


def set_file_queued(file_id, queued):
    """Legacy compat wrapper. Use set_file_queue_position / clear_file_queue_position."""
    if queued:
        set_file_queue_position(file_id)
    else:
        clear_file_queue_position(file_id)


def set_file_queue_position(file_id, batch_id=None):
    """Add a file to the download queue at the end (MAX + 1)."""
    with _db() as conn:
        # Only queue files that still need downloading
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(queue_position), 0) FROM archive_files WHERE queue_position IS NOT NULL"
        ).fetchone()[0]
        updates = ["queue_position = ?", "queued = 1"]
        params = [max_pos + 1]
        if batch_id is not None:
            updates.append("download_batch_id = ?")
            params.append(batch_id)
        params.append(file_id)
        conn.execute(
            f"""UPDATE archive_files SET {', '.join(updates)}
               WHERE id = ? AND download_status NOT IN ('completed', 'conflict')
                 AND processing_status NOT IN ('queued', 'processing', 'processed')""",
            params,
        )
        conn.commit()


def clear_file_queue_position(file_id):
    """Remove a file from the download queue."""
    with _db() as conn:
        # Get the batch_id before clearing, so we can decrement the batch
        row = conn.execute(
            "SELECT download_batch_id FROM archive_files WHERE id = ?", (file_id,)
        ).fetchone()
        conn.execute(
            "UPDATE archive_files SET queue_position = NULL, queued = 0, download_batch_id = NULL WHERE id = ?",
            (file_id,),
        )
        # Decrement the batch file_count if this file was part of a batch
        if row and row["download_batch_id"]:
            conn.execute(
                "UPDATE batches SET file_count = MAX(file_count - 1, 0) WHERE id = ?",
                (row["download_batch_id"],),
            )
        conn.commit()


def clear_download_queue():
    """Remove all pending files from the download queue. Returns count cleared.
    Does not affect files currently downloading (download_status='downloading')."""
    with _db() as conn:
        conn.execute(
            """UPDATE archive_files
               SET queue_position = NULL, queued = 0, download_batch_id = NULL
               WHERE queue_position IS NOT NULL
                 AND download_status NOT IN ('downloading')""",
        )
        count = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return count


def set_all_files_queued(archive_id, queued, batch_id=None):
    """Batch-add or remove all files in an archive from the download queue.
    Returns (added_count, skipped_count) when queuing."""
    with _db() as conn:
        if queued:
            # Get current max position
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(queue_position), 0) FROM archive_files WHERE queue_position IS NOT NULL"
            ).fetchone()[0]
            # Find eligible files (not already queued, not completed, not processing)
            eligible = conn.execute(
                """SELECT id FROM archive_files
                   WHERE archive_id = ? AND queue_position IS NULL
                     AND download_status NOT IN ('completed', 'conflict')
                     AND processing_status NOT IN ('queued', 'processing', 'processed')
                   ORDER BY name ASC""",
                (archive_id,),
            ).fetchall()
            added = 0
            for row in eligible:
                max_pos += 1
                updates = ["queue_position = ?", "queued = 1"]
                params = [max_pos]
                if batch_id is not None:
                    updates.append("download_batch_id = ?")
                    params.append(batch_id)
                params.append(row["id"])
                conn.execute(
                    f"UPDATE archive_files SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                added += 1
            # Count how many were already queued or completed
            already = conn.execute(
                """SELECT COUNT(*) FROM archive_files
                   WHERE archive_id = ? AND (queue_position IS NOT NULL
                     OR download_status IN ('completed', 'conflict')
                     OR processing_status IN ('queued', 'processing', 'processed'))""",
                (archive_id,),
            ).fetchone()[0]
            conn.commit()
            return added, already
        else:
            conn.execute(
                "UPDATE archive_files SET queue_position = NULL, queued = 0, download_batch_id = NULL WHERE archive_id = ?",
                (archive_id,),
            )
            conn.commit()
            return 0, 0


def set_file_download_status(file_id, status, downloaded_bytes=None, error_message=None):
    with _db() as conn:
        updates = ["download_status = ?"]
        params = [status]
        if downloaded_bytes is not None:
            updates.append("downloaded_bytes = ?")
            params.append(downloaded_bytes)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        # Dequeue files that have reached a terminal state
        if status in ("completed", "conflict"):
            updates.append("queued = 0")
            updates.append("queue_position = NULL")
            updates.append("download_batch_id = NULL")
        # Set downloaded flag: 1 when completed, 0 when reset to pending
        if status == "completed":
            updates.append("downloaded = 1")
        elif status == "pending":
            updates.append("downloaded = 0")
        params.append(file_id)
        conn.execute(f"UPDATE archive_files SET {', '.join(updates)} WHERE id = ?", params)
        # If completed, increment batch completed_count
        if status == "completed":
            row = conn.execute(
                "SELECT download_batch_id FROM archive_files WHERE id = ?", (file_id,)
            ).fetchone()
            if row and row["download_batch_id"]:
                conn.execute(
                    "UPDATE batches SET completed_count = completed_count + 1 WHERE id = ?",
                    (row["download_batch_id"],),
                )
                # Check if batch is now complete
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id = ?", (row["download_batch_id"],)
                ).fetchone()
                if batch and batch["completed_count"] >= batch["file_count"]:
                    conn.execute(
                        "UPDATE batches SET status = 'completed' WHERE id = ?",
                        (row["download_batch_id"],),
                    )
        conn.commit()


def increment_file_retry(file_id):
    with _db() as conn:
        conn.execute("UPDATE archive_files SET retry_count = retry_count + 1 WHERE id = ?", (file_id,))
        conn.commit()


def get_next_download_file():
    """Get the next file to download, ordered by queue_position.
    Acquires the download queue lock to prevent races with compaction."""
    with _download_queue_lock:
        with _db() as conn:
            max_retries = int(get_setting("max_retries") or 3)
            row = conn.execute("""
                SELECT af.*, a.identifier, a.server, a.dir, a.id as archive_id
                FROM archive_files af
                JOIN archives a ON af.archive_id = a.id
                WHERE a.download_enabled = 1
                  AND af.queue_position IS NOT NULL
                  AND (af.download_status = 'pending'
                       OR (af.download_status = 'failed' AND af.retry_count < ?))
                ORDER BY af.queue_position ASC
                LIMIT 1
            """, (max_retries,)).fetchone()
            return dict(row) if row else None


def get_download_queue(limit=200):
    """Get the ordered download queue: files in queue_position order."""
    with _db() as conn:
        max_retries = int(get_setting("max_retries") or 3)
        rows = conn.execute("""
            SELECT af.id, af.name, af.size, af.download_status, af.downloaded_bytes,
                   af.queue_position, af.downloaded, a.id as archive_id, a.identifier, a.title
            FROM archive_files af
            JOIN archives a ON af.archive_id = a.id
            WHERE af.queue_position IS NOT NULL
              AND (af.download_status IN ('pending', 'downloading')
                   OR (af.download_status = 'failed' AND af.retry_count < ?))
            ORDER BY af.queue_position ASC
            LIMIT ?
        """, (max_retries, limit)).fetchall()
        return [dict(r) for r in rows]


def get_download_progress():
    """Get overall download progress stats."""
    with _db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total_files,
                SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) as completed_files,
                SUM(CASE WHEN download_status = 'downloading' THEN 1 ELSE 0 END) as active_files,
                SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) as failed_files,
                SUM(CASE WHEN download_status IN ('pending', 'failed') THEN 1 ELSE 0 END) as queued_files,
                SUM(size) as total_size,
                SUM(downloaded_bytes) as downloaded_bytes
            FROM archive_files af
            JOIN archives a ON af.archive_id = a.id
            WHERE a.download_enabled = 1 AND (af.queue_position IS NOT NULL OR af.download_status = 'completed')
        """).fetchone()
        if row:
            return dict(row)
        return {
            "total_files": 0, "completed_files": 0, "active_files": 0,
            "failed_files": 0, "queued_files": 0, "total_size": 0, "downloaded_bytes": 0,
        }


def reset_downloading_files():
    """Reset any files stuck in 'downloading' state back to 'pending' (e.g., after crash)."""
    with _db() as conn:
        conn.execute("UPDATE archive_files SET download_status = 'pending' WHERE download_status = 'downloading'")
        conn.commit()


def reset_stale_processing():
    """Reset files and jobs stuck in processing states after a crash.

    - Files stuck in 'processing' or 'queued' → reset to '' (unprocessed)
    - Processing jobs stuck in 'running' → mark as 'failed'
    - Stale processing/scan notifications → cleared
    - Activity jobs stuck in 'running' for processing/scan → marked failed
    """
    with _db() as conn:
        # Reset stuck files
        stuck_files = conn.execute(
            "SELECT COUNT(*) as cnt FROM archive_files WHERE processing_status IN ('processing', 'queued')"
        ).fetchone()["cnt"]
        if stuck_files:
            conn.execute(
                "UPDATE archive_files SET processing_status = '', processing_error = 'Reset after crash' "
                "WHERE processing_status IN ('processing', 'queued')"
            )

        # Fail stuck processing jobs
        conn.execute(
            "UPDATE processing_jobs SET status = 'failed', error_message = 'Interrupted by crash/restart', "
            "completed_at = ? WHERE status = 'running'",
            (time.time(),),
        )
        stuck_jobs = conn.execute("SELECT changes()").fetchone()[0]

        # Dismiss ALL stale processing notifications — any notification with
        # processing_archive_id set where the job is no longer running.
        # This catches both jobs stuck in 'running' (just failed above) and
        # jobs that were already marked failed/completed but whose notification
        # was never cleaned up.  Clear the processing_archive_id so that
        # clear_notifications() and the dismiss endpoint can handle them
        # normally going forward.
        conn.execute("""
            UPDATE notifications
            SET dismissed = 1, processing_archive_id = NULL
            WHERE processing_archive_id IS NOT NULL
              AND dismissed = 0
              AND processing_archive_id NOT IN (
                  SELECT archive_id FROM processing_jobs WHERE status IN ('pending', 'running')
              )
        """)
        stale_notifs = conn.execute("SELECT changes()").fetchone()[0]

        # Same for scan notifications
        conn.execute("""
            UPDATE notifications
            SET dismissed = 1, scan_archive_id = NULL
            WHERE scan_archive_id IS NOT NULL
              AND dismissed = 0
        """)

        # Fail stuck activity jobs for processing and scan
        conn.execute(
            "UPDATE activity_jobs SET status = 'failed', completed_at = ?, "
            "summary = 'Interrupted by crash/restart' "
            "WHERE category IN ('processing', 'scan') AND status = 'running'",
            (time.time(),),
        )

        conn.commit()

        if stuck_files or stuck_jobs or stale_notifs:
            log.info("Startup cleanup: %d stuck files, %d stuck jobs, %d stale notifications",
                     stuck_files, stuck_jobs, stale_notifs)


def reset_failed_files(archive_id):
    """Reset all failed files in an archive back to pending with retry count zeroed."""
    with _db() as conn:
        conn.execute(
            "UPDATE archive_files SET download_status = 'pending', retry_count = 0, error_message = '' "
            "WHERE archive_id = ? AND download_status = 'failed'",
            (archive_id,),
        )
        affected = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return affected


def reset_failed_file(file_id):
    """Reset a single failed file back to pending."""
    with _db() as conn:
        conn.execute(
            "UPDATE archive_files SET download_status = 'pending', retry_count = 0, error_message = '' "
            "WHERE id = ? AND download_status = 'failed'",
            (file_id,),
        )
        conn.commit()


def clear_change_statuses(archive_id):
    """Clear all change_status flags for an archive."""
    with _db() as conn:
        conn.execute(
            "UPDATE archive_files SET change_status = '', change_detail = '' WHERE archive_id = ?",
            (archive_id,),
        )
        conn.commit()


def refresh_archive_metadata(archive_id, new_files_list):
    """Compare current files against fresh IA metadata. Updates change_status:
    '' = unchanged, 'new' = added, 'removed' = no longer on IA, 'changed' = same name but different content.
    Returns summary dict."""
    with _db() as conn:

        # Get existing files keyed by name
        rows = conn.execute(
            "SELECT id, name, size, md5, mtime, download_status FROM archive_files WHERE archive_id = ?",
            (archive_id,),
        ).fetchall()
        existing = {r["name"]: dict(r) for r in rows}

        # Build lookup for new files
        incoming = {}
        for f in new_files_list:
            incoming[f["name"]] = f

        summary = {"new": 0, "removed": 0, "changed": 0, "unchanged": 0}

        # First: clear all change statuses
        conn.execute(
            "UPDATE archive_files SET change_status = '', change_detail = '' WHERE archive_id = ?",
            (archive_id,),
        )

        # Check existing files against incoming
        for name, old in existing.items():
            if name not in incoming:
                # File removed from IA
                conn.execute(
                    "UPDATE archive_files SET change_status = 'removed', change_detail = ? WHERE id = ?",
                    ("This file is no longer listed on Internet Archive", old["id"]),
                )
                summary["removed"] += 1
            else:
                new_f = incoming[name]
                new_size = int(new_f.get("size", 0) or 0)
                new_md5 = new_f.get("md5", "")
                new_mtime = new_f.get("mtime", "")
                changes = []
                if new_md5 and old["md5"] and new_md5 != old["md5"]:
                    changes.append("hash changed")
                if new_size != old["size"] and old["size"] > 0 and new_size > 0:
                    changes.append(f"size: {old['size']} \u2192 {new_size} bytes")
                if new_mtime and old["mtime"] and new_mtime != old["mtime"]:
                    changes.append("modification time changed")

                if changes:
                    detail = "File content changed: " + ", ".join(changes)
                    conn.execute(
                        "UPDATE archive_files SET change_status = 'changed', change_detail = ?, "
                        "size = ?, md5 = ?, sha1 = ?, mtime = ? WHERE id = ?",
                        (detail, new_size, new_md5, new_f.get("sha1", ""), new_mtime, old["id"]),
                    )
                    summary["changed"] += 1
                else:
                    summary["unchanged"] += 1

        # Check for new files not in existing
        # Get current max queue_position for newly queued files
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(queue_position), 0) FROM archive_files WHERE queue_position IS NOT NULL"
        ).fetchone()[0]
        new_pos_offset = 0
        for name, new_f in incoming.items():
            if name not in existing:
                new_pos_offset += 1
                conn.execute(
                    """INSERT INTO archive_files
                       (archive_id, name, size, md5, sha1, format, source, mtime,
                        queued, queue_position, change_status, change_detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'new', 'Newly added to Internet Archive since last check')""",
                    (archive_id, name, int(new_f.get("size", 0) or 0),
                     new_f.get("md5", ""), new_f.get("sha1", ""), new_f.get("format", ""),
                     new_f.get("source", ""), new_f.get("mtime", ""),
                     max_pos + new_pos_offset),
                )
                summary["new"] += 1

        # Update archive metadata
        total_size = sum(int(f.get("size", 0) or 0) for f in new_files_list)
        conn.execute(
            "UPDATE archives SET total_size = ?, files_count = ? WHERE id = ?",
            (total_size, len(new_files_list), archive_id),
        )

        conn.commit()
        return summary


# --- Groups ---

def get_groups():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM archive_groups ORDER BY position ASC").fetchall()
        return [dict(r) for r in rows]


def get_group(group_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM archive_groups WHERE id = ?", (group_id,)).fetchone()
        return dict(row) if row else None


def add_group(name):
    with _db() as conn:
        conn.execute(
            "INSERT INTO archive_groups (name, position) VALUES (?, (SELECT COALESCE(MAX(position), -1) + 1 FROM archive_groups))",
            (name,),
        )
        group_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return group_id


def rename_group(group_id, name):
    with _db() as conn:
        conn.execute("UPDATE archive_groups SET name = ? WHERE id = ?", (name, group_id))
        conn.commit()


def delete_group(group_id):
    """Delete a group. Archives in the group become ungrouped."""
    with _db() as conn:
        conn.execute("UPDATE archives SET group_id = NULL WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM archive_groups WHERE id = ?", (group_id,))
        conn.commit()


def reorder_groups(id_order):
    """id_order is a list of group IDs in desired order."""
    with _db() as conn:
        for pos, gid in enumerate(id_order):
            conn.execute("UPDATE archive_groups SET position = ? WHERE id = ?", (pos, gid))
        conn.commit()


def set_archive_group(archive_id, group_id):
    """Move an archive into a group (or remove from group if group_id is None)."""
    with _db() as conn:
        conn.execute("UPDATE archives SET group_id = ? WHERE id = ?", (group_id, archive_id))
        conn.commit()


# --- Auth ---

def is_auth_setup():
    """Check if a user/password has been configured."""
    with _db() as conn:
        row = conn.execute("SELECT id FROM auth WHERE id = 1").fetchone()
        return row is not None


def create_auth(username, password):
    """Create or replace the single auth credential."""
    with _db() as conn:
        pw_hash = generate_password_hash(password)
        conn.execute(
            "INSERT INTO auth (id, username, password_hash) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username = ?, password_hash = ?",
            (username, pw_hash, username, pw_hash),
        )
        conn.commit()


def verify_auth(username, password):
    """Verify credentials. Returns True if valid."""
    with _db() as conn:
        row = conn.execute("SELECT username, password_hash FROM auth WHERE id = 1").fetchone()
        if not row:
            return False
        if row["username"] != username:
            return False
        return check_password_hash(row["password_hash"], password)


def change_password(old_password, new_password):
    """Change password. Returns True if old_password was correct and change succeeded."""
    with _db() as conn:
        row = conn.execute("SELECT username, password_hash FROM auth WHERE id = 1").fetchone()
    if not row:
        return False
    if not check_password_hash(row["password_hash"], old_password):
        return False
    with _db() as conn:
        pw_hash = generate_password_hash(new_password)
        conn.execute("UPDATE auth SET password_hash = ? WHERE id = 1", (pw_hash,))
        conn.commit()
    return True


# --- Processing Profiles ---

def get_processing_profiles():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM processing_profiles ORDER BY position ASC").fetchall()
        return [dict(r) for r in rows]


def get_processing_profile(profile_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM processing_profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None


def add_processing_profile(name, processor_type, options=None):
    with _db() as conn:
        conn.execute(
            "INSERT INTO processing_profiles (name, processor_type, options_json, position) VALUES (?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM processing_profiles))",
            (name, processor_type, json.dumps(options or {})),
        )
        profile_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return profile_id


def update_processing_profile(profile_id, name=None, processor_type=None, options=None):
    with _db() as conn:
        updates, params = [], []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if processor_type is not None:
            updates.append("processor_type = ?")
            params.append(processor_type)
        if options is not None:
            updates.append("options_json = ?")
            params.append(json.dumps(options))
        if updates:
            params.append(profile_id)
            conn.execute(f"UPDATE processing_profiles SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()


def delete_processing_profile(profile_id):
    with _db() as conn:
        # Unlink any archives using this profile
        conn.execute("UPDATE archives SET processing_profile_id = NULL WHERE processing_profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM processing_profiles WHERE id = ?", (profile_id,))
        conn.commit()


def set_archive_processing_profile(archive_id, profile_id):
    with _db() as conn:
        conn.execute("UPDATE archives SET processing_profile_id = ? WHERE id = ?", (profile_id, archive_id))
        conn.commit()


# --- Processing Status Helpers ---

def set_file_processing_status(file_id, status, processed_filename=None, processor_type=None, error=None, processed_files=None):
    with _db() as conn:
        updates = ["processing_status = ?"]
        params = [status]
        if processed_filename is not None:
            updates.append("processed_filename = ?")
            params.append(processed_filename)
        if processor_type is not None:
            updates.append("processor_type = ?")
            params.append(processor_type)
        if error is not None:
            updates.append("processing_error = ?")
            params.append(error)
        if processed_files is not None:
            updates.append("processed_files_json = ?")
            params.append(json.dumps(processed_files))
        params.append(file_id)
        conn.execute(f"UPDATE archive_files SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()


def get_all_processed_files(archive_id):
    """Return a set of all output filenames (relative to download dir) produced
    by processing for the given archive.  Includes both single-file outputs
    (processed_filename) and multi-file outputs (processed_files_json)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT processed_filename, processed_files_json FROM archive_files "
            "WHERE archive_id = ? AND processing_status = 'processed'",
            (archive_id,),
        ).fetchall()
        names = set()
        for r in rows:
            pf = r["processed_filename"]
            if pf:
                names.add(pf)
            pj = r["processed_files_json"]
            if pj:
                try:
                    for entry in json.loads(pj):
                        names.add(entry)
                except (json.JSONDecodeError, TypeError):
                    pass
        return names


def get_file(file_id):
    """Get a single archive file by its ID."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM archive_files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def rename_file(file_id, new_name):
    """Rename a file in the database."""
    with _db() as conn:
        conn.execute("UPDATE archive_files SET name = ? WHERE id = ?", (new_name, file_id))
        conn.commit()


def delete_files(file_ids):
    """Delete files from the database by their IDs. Returns the count deleted."""
    if not file_ids:
        return 0
    with _db() as conn:
        placeholders = ",".join("?" * len(file_ids))
        conn.execute(f"DELETE FROM archive_files WHERE id IN ({placeholders})", file_ids)
        affected = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return affected


def reset_failed_files_by_ids(file_ids):
    """Reset specific failed files back to pending."""
    if not file_ids:
        return 0
    with _db() as conn:
        placeholders = ",".join("?" * len(file_ids))
        conn.execute(
            f"UPDATE archive_files SET download_status = 'pending', retry_count = 0, error_message = '' "
            f"WHERE id IN ({placeholders}) AND download_status = 'failed'",
            file_ids,
        )
        affected = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return affected


def assign_as_processed_output(target_file_id, unknown_file_id):
    """Assign an unknown file as processed output of a target file.
    Adds the unknown file's name to the target's processed_files_json,
    sets the target's processing_status to 'processed' if not already,
    and deletes the unknown file record."""
    with _db() as conn:
        unknown = conn.execute("SELECT * FROM archive_files WHERE id = ?", (unknown_file_id,)).fetchone()
        if not unknown or unknown["download_status"] != "unknown":
            return False, "Source file is not an unknown file"
        target = conn.execute("SELECT * FROM archive_files WHERE id = ?", (target_file_id,)).fetchone()
        if not target:
            return False, "Target file not found"
        if unknown["archive_id"] != target["archive_id"]:
            return False, "Files must be in the same archive"

        # Add unknown file's name to target's processed_files_json
        existing = []
        if target["processed_files_json"]:
            try:
                existing = json.loads(target["processed_files_json"])
            except (json.JSONDecodeError, TypeError):
                existing = []
        if unknown["name"] not in existing:
            existing.append(unknown["name"])

        # Update target: set processing_status and processed_files_json
        updates = ["processed_files_json = ?"]
        params = [json.dumps(existing)]
        if target["processing_status"] != "processed":
            updates.append("processing_status = 'processed'")
        if not target["processed_filename"]:
            updates.append("processed_filename = ?")
            params.append(unknown["name"])
        params.append(target_file_id)
        conn.execute(f"UPDATE archive_files SET {', '.join(updates)} WHERE id = ?", params)

        # Delete the unknown file record
        conn.execute("DELETE FROM archive_files WHERE id = ?", (unknown_file_id,))
        conn.commit()
        return True, None


def get_processable_files(archive_id, processor_types=None):
    """Get files eligible for processing: completed downloads, not already processed.
    Optionally filter by file extensions matching processor input types."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM archive_files
               WHERE archive_id = ? AND download_status = 'completed'
                 AND processing_status IN ('', 'failed')
                 AND origin = 'manifest'
               ORDER BY name ASC""",
            (archive_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_processing_queue_files(archive_id):
    """Get files currently queued or being processed for an archive."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM archive_files WHERE archive_id = ? AND processing_status IN ('queued', 'processing')",
            (archive_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Notifications ---

def create_notification(message, type="info", progress=None, scan_archive_id=None,
                        processing_archive_id=None, adding_archive=False, job_id=None):
    """Create a persistent notification and return its ID."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO notifications (message, type, created_at, progress, scan_archive_id,
               processing_archive_id, adding_archive, job_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message, type, time.time(), progress, scan_archive_id,
             processing_archive_id, 1 if adding_archive else 0, job_id),
        )
        nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return nid


def update_notification(notif_id, **kwargs):
    """Update fields on a notification. Supported: message, type, progress, dismissed, job_id."""
    allowed = {"message", "type", "progress", "dismissed", "job_id"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [notif_id]
    with _db() as conn:
        conn.execute(f"UPDATE notifications SET {set_clause} WHERE id = ?", values)
        conn.commit()


def dismiss_notification(notif_id):
    """Mark a notification as dismissed."""
    update_notification(notif_id, dismissed=1)


def delete_notification(notif_id):
    """Permanently delete a notification."""
    with _db() as conn:
        conn.execute("DELETE FROM notifications WHERE id = ?", (notif_id,))
        conn.commit()


def get_notifications(include_dismissed=False):
    """Return notifications, newest first (capped to prevent unbounded results)."""
    with _db() as conn:
        if include_dismissed:
            rows = conn.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 500").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE dismissed = 0 ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]


def get_notification(notif_id):
    """Return a single notification by ID."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        return dict(row) if row else None


def clear_notifications():
    """Dismiss all non-active notifications.

    Clears notifications that have no ongoing operation, plus stale
    processing/scan notifications whose jobs are no longer running.
    """
    with _db() as conn:
        # Clear simple notifications (no linked operation)
        conn.execute("""
            UPDATE notifications SET dismissed = 1
            WHERE dismissed = 0
              AND scan_archive_id IS NULL
              AND processing_archive_id IS NULL
              AND adding_archive = 0
        """)
        # Clear stale processing notifications (job finished/failed/cancelled)
        conn.execute("""
            UPDATE notifications SET dismissed = 1, processing_archive_id = NULL
            WHERE dismissed = 0
              AND processing_archive_id IS NOT NULL
              AND processing_archive_id NOT IN (
                  SELECT archive_id FROM processing_jobs WHERE status IN ('pending', 'running')
              )
        """)
        # Clear stale scan notifications (scans don't have a persistent job table,
        # so any scan notification at clear time is stale)
        conn.execute("""
            UPDATE notifications SET dismissed = 1, scan_archive_id = NULL
            WHERE dismissed = 0
              AND scan_archive_id IS NOT NULL
        """)
        conn.commit()


def prune_notifications(max_age_days=7, max_dismissed=200):
    """Delete old dismissed notifications to prevent unbounded growth.

    Keeps at most *max_dismissed* dismissed notifications and removes any
    dismissed notification older than *max_age_days*.
    """
    import time as _time
    cutoff = _time.time() - (max_age_days * 86400)
    with _db() as conn:
        # Delete old dismissed notifications
        conn.execute(
            "DELETE FROM notifications WHERE dismissed = 1 AND created_at < ?",
            (cutoff,),
        )
        # Cap total dismissed count — keep the newest max_dismissed
        conn.execute("""
            DELETE FROM notifications WHERE dismissed = 1 AND id NOT IN (
                SELECT id FROM notifications WHERE dismissed = 1
                ORDER BY created_at DESC LIMIT ?
            )
        """, (max_dismissed,))
        conn.commit()


def find_notification_by_scan(archive_id):
    """Find the active (non-dismissed) scan notification for an archive."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM notifications WHERE scan_archive_id = ? AND dismissed = 0",
            (archive_id,),
        ).fetchone()
        return dict(row) if row else None


def find_notification_by_processing(archive_id):
    """Find the active (non-dismissed) processing notification for an archive."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM notifications WHERE processing_archive_id = ? AND dismissed = 0",
            (archive_id,),
        ).fetchone()
        return dict(row) if row else None


# --- Processing Jobs ---

def create_processing_job(archive_id, profile_id, file_ids=None, options_override=None):
    """Create a new processing job and return its ID."""
    with _db() as conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM processing_jobs WHERE status = 'pending'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO processing_jobs
               (archive_id, profile_id, file_ids_json, options_override_json, status, created_at, position)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (archive_id, profile_id,
             json.dumps(file_ids) if file_ids else None,
             json.dumps(options_override) if options_override else None,
             time.time(), position),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return job_id


def get_next_processing_job():
    """Get the next pending processing job (FIFO by position)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def claim_processing_job(job_id):
    """Atomically claim a pending job by setting status to 'running'."""
    with _db() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'pending'",
            (time.time(), job_id),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return changed > 0


def complete_processing_job(job_id, error_message=None):
    """Mark a processing job as completed or failed."""
    status = "failed" if error_message else "completed"
    with _db() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (status, error_message or "", time.time(), job_id),
        )
        conn.commit()


def cancel_processing_job(job_id):
    """Cancel a pending or running processing job."""
    with _db() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status = 'cancelled', completed_at = ? WHERE id = ? AND status IN ('pending', 'running')",
            (time.time(), job_id),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return changed > 0


def get_processing_job(job_id):
    """Return a single processing job by ID."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_processing_jobs(status=None):
    """Return processing jobs, optionally filtered by status."""
    with _db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM processing_jobs WHERE status = ? ORDER BY position ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM processing_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_active_processing_job_for_archive(archive_id):
    """Check if there's an active (pending/running) processing job for an archive."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE archive_id = ? AND status IN ('pending', 'running') LIMIT 1",
            (archive_id,),
        ).fetchone()
        return dict(row) if row else None


def count_pending_processing_jobs():
    """Return count of pending processing jobs."""
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM processing_jobs WHERE status = 'pending'").fetchone()
        return row["cnt"]


# ── Collections ──────────────────────────────────────────────────────────

def create_collection(name, file_scope="processed", auto_tag=None):
    """Create a new collection. Returns the new collection dict."""
    with _db() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM collections").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO collections (name, file_scope, auto_tag, position, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, file_scope, auto_tag or None, pos, time.time()),
        )
        conn.commit()
        return get_collection(cur.lastrowid)


def get_collection(collection_id):
    """Return a single collection dict with archive/layout counts, or None."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["archive_count"] = conn.execute(
            "SELECT COUNT(*) FROM collection_archives WHERE collection_id = ?", (collection_id,)
        ).fetchone()[0]
        d["layout_count"] = conn.execute(
            "SELECT COUNT(*) FROM collection_layouts WHERE collection_id = ?", (collection_id,)
        ).fetchone()[0]
        # Count total files across archives in this collection
        d["file_count"] = _count_collection_files(conn, collection_id, d["file_scope"], d["auto_tag"])
        d["layouts"] = [dict(r) for r in conn.execute(
            "SELECT * FROM collection_layouts WHERE collection_id = ? ORDER BY position", (collection_id,)
        ).fetchall()]
        return d


def get_collections():
    """Return all collections with summary info."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM collections ORDER BY position").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            cid = d["id"]
            d["archive_count"] = conn.execute(
                "SELECT COUNT(*) FROM collection_archives WHERE collection_id = ?", (cid,)
            ).fetchone()[0]
            d["layout_count"] = conn.execute(
                "SELECT COUNT(*) FROM collection_layouts WHERE collection_id = ?", (cid,)
            ).fetchone()[0]
            d["file_count"] = _count_collection_files(conn, cid, d["file_scope"], d["auto_tag"])
            d["layouts"] = [dict(r) for r in conn.execute(
                "SELECT * FROM collection_layouts WHERE collection_id = ? ORDER BY position", (cid,)
            ).fetchall()]
            result.append(d)
        return result


def update_collection(collection_id, **kwargs):
    """Update collection fields. Accepted keys: name, file_scope, auto_tag, position."""
    allowed = {"name", "file_scope", "auto_tag", "position"}
    updates = []
    params = []
    for key, val in kwargs.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(val)
    if not updates:
        return
    params.append(collection_id)
    with _db() as conn:
        conn.execute(f"UPDATE collections SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()


def delete_collection(collection_id):
    """Delete a collection and all its relationships (CASCADE)."""
    with _db() as conn:
        conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        conn.commit()


def _get_collection_archive_ids(conn, collection_id, auto_tag=None):
    """Return set of archive IDs in a collection (manual + auto-tag)."""
    ids = set()
    for row in conn.execute(
        "SELECT archive_id FROM collection_archives WHERE collection_id = ?", (collection_id,)
    ).fetchall():
        ids.add(row[0])
    if auto_tag:
        for row in conn.execute(
            "SELECT archive_id FROM archive_tags WHERE tag = ?", (auto_tag,)
        ).fetchall():
            ids.add(row[0])
    return ids


def _count_collection_files(conn, collection_id, file_scope, auto_tag):
    """Count files matching the collection's scope across its archives."""
    archive_ids = _get_collection_archive_ids(conn, collection_id, auto_tag)
    if not archive_ids:
        return 0
    placeholders = ",".join("?" * len(archive_ids))
    if file_scope == "processed":
        condition = "processing_status = 'processed'"
    elif file_scope == "downloaded":
        condition = "download_status = 'completed' AND origin = 'manifest'"
    else:  # both
        condition = "(processing_status = 'processed' OR (download_status = 'completed' AND origin = 'manifest'))"
    row = conn.execute(
        f"SELECT COUNT(*) FROM archive_files WHERE archive_id IN ({placeholders}) AND {condition}",
        list(archive_ids),
    ).fetchone()
    return row[0]


def get_collection_files(collection_id):
    """Return all files matching a collection's scope, with archive identifier."""
    with _db() as conn:
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not coll:
            return []
        archive_ids = _get_collection_archive_ids(conn, collection_id, coll["auto_tag"])
        if not archive_ids:
            return []
        placeholders = ",".join("?" * len(archive_ids))
        scope = coll["file_scope"]
        if scope == "processed":
            condition = "af.processing_status = 'processed'"
        elif scope == "downloaded":
            condition = "af.download_status = 'completed' AND af.origin = 'manifest'"
        else:
            condition = "(af.processing_status = 'processed' OR (af.download_status = 'completed' AND af.origin = 'manifest'))"
        rows = conn.execute(
            f"""SELECT af.*, a.identifier AS archive_identifier
                FROM archive_files af
                JOIN archives a ON af.archive_id = a.id
                WHERE af.archive_id IN ({placeholders}) AND {condition}""",
            list(archive_ids),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Collection-Archive relationships ──────────────────────────────────

def add_archive_to_collection(collection_id, archive_id):
    """Add an archive to a collection. Returns True if added, False if already present."""
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO collection_archives (collection_id, archive_id) VALUES (?, ?)",
                (collection_id, archive_id),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_archive_from_collection(collection_id, archive_id):
    """Remove an archive from a collection."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM collection_archives WHERE collection_id = ? AND archive_id = ?",
            (collection_id, archive_id),
        )
        conn.commit()


def get_archives_for_collection(collection_id):
    """Return archives in a collection (manual + auto-tag), with file counts."""
    with _db() as conn:
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not coll:
            return []
        archive_ids = _get_collection_archive_ids(conn, collection_id, coll["auto_tag"])
        if not archive_ids:
            return []
        placeholders = ",".join("?" * len(archive_ids))
        rows = conn.execute(
            f"SELECT * FROM archives WHERE id IN ({placeholders}) ORDER BY identifier",
            list(archive_ids),
        ).fetchall()
        # Check which are manual vs auto-tag
        manual_ids = set(r[0] for r in conn.execute(
            "SELECT archive_id FROM collection_archives WHERE collection_id = ?", (collection_id,)
        ).fetchall())
        result = []
        for row in rows:
            d = dict(row)
            d["manual"] = d["id"] in manual_ids
            d["file_count"] = conn.execute(
                "SELECT COUNT(*) FROM archive_files WHERE archive_id = ?", (d["id"],)
            ).fetchone()[0]
            result.append(d)
        return result


def get_collections_for_archive(archive_id):
    """Return collections that contain this archive (manual membership only)."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT c.* FROM collections c
               JOIN collection_archives ca ON c.id = ca.collection_id
               WHERE ca.archive_id = ?
               ORDER BY c.position""",
            (archive_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Archive Tags ──────────────────────────────────────────────────────

def add_archive_tag(archive_id, tag):
    """Add a tag to an archive. Returns True if added, False if already present."""
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO archive_tags (archive_id, tag) VALUES (?, ?)",
                (archive_id, tag.strip()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_archive_tag(archive_id, tag):
    """Remove a tag from an archive."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM archive_tags WHERE archive_id = ? AND tag = ?",
            (archive_id, tag),
        )
        conn.commit()


def get_archive_tags(archive_id):
    """Return list of tag strings for an archive."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT tag FROM archive_tags WHERE archive_id = ? ORDER BY tag",
            (archive_id,),
        ).fetchall()
        return [r["tag"] for r in rows]


def get_all_tags():
    """Return all unique tags with counts."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) as count FROM archive_tags GROUP BY tag ORDER BY tag"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Collection Layouts ────────────────────────────────────────────────

def add_collection_layout(collection_id, name, layout_type="flat"):
    """Add a layout to a collection. Returns the new layout dict."""
    with _db() as conn:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM collection_layouts WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO collection_layouts (collection_id, name, type, position) VALUES (?, ?, ?, ?)",
            (collection_id, name, layout_type, pos),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM collection_layouts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else None


def update_collection_layout(layout_id, **kwargs):
    """Update layout fields. Accepted keys: name, type, position."""
    allowed = {"name", "type", "position"}
    updates = []
    params = []
    for key, val in kwargs.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(val)
    if not updates:
        return
    params.append(layout_id)
    with _db() as conn:
        conn.execute(f"UPDATE collection_layouts SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()


def delete_collection_layout(layout_id):
    """Delete a layout."""
    with _db() as conn:
        conn.execute("DELETE FROM collection_layouts WHERE id = ?", (layout_id,))
        conn.commit()


def get_collection_layouts(collection_id):
    """Return layouts for a collection."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM collection_layouts WHERE collection_id = ? ORDER BY position",
            (collection_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Queue Overhaul: Batches ──────────────────────────────────────────

def create_batch(queue_type, file_count=0, archive_id=None):
    """Create a generic batch record for notification tracking. Returns batch ID."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO batches (queue_type, archive_id, created_at, file_count, status) VALUES (?, ?, ?, ?, 'pending')",
            (queue_type, archive_id, time.time(), file_count),
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return batch_id


def get_batch(batch_id):
    """Return a single batch record."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


def update_batch(batch_id, **kwargs):
    """Update batch fields. Accepted: file_count, completed_count, status."""
    allowed = {"file_count", "completed_count", "status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [batch_id]
    with _db() as conn:
        conn.execute(f"UPDATE batches SET {set_clause} WHERE id = ?", values)
        conn.commit()


# ── Queue Overhaul: Compaction ───────────────────────────────────────

def compact_download_queue():
    """Renumber all queue_positions as contiguous 1, 2, 3, ...
    Acquires the download queue lock to prevent workers from picking stale positions."""
    with _download_queue_lock:
        with _db() as conn:
            conn.execute("""
                UPDATE archive_files SET queue_position = (
                    SELECT rn FROM (
                        SELECT id, ROW_NUMBER() OVER (ORDER BY queue_position) AS rn
                        FROM archive_files WHERE queue_position IS NOT NULL
                    ) AS t WHERE t.id = archive_files.id
                ) WHERE queue_position IS NOT NULL
            """)
            conn.commit()


def compact_processing_queue():
    """Renumber all processing_queue positions as contiguous 1, 2, 3, ..."""
    with _processing_queue_lock:
        with _db() as conn:
            conn.execute("""
                UPDATE processing_queue SET position = (
                    SELECT rn FROM (
                        SELECT id, ROW_NUMBER() OVER (ORDER BY position) AS rn
                        FROM processing_queue WHERE status = 'pending'
                    ) AS t WHERE t.id = processing_queue.id
                ) WHERE status = 'pending'
            """)
            conn.commit()


def compact_scan_queue():
    """Renumber all scan_queue positions as contiguous 1, 2, 3, ..."""
    with _scan_queue_lock:
        with _db() as conn:
            conn.execute("""
                UPDATE scan_queue SET position = (
                    SELECT rn FROM (
                        SELECT id, ROW_NUMBER() OVER (ORDER BY position) AS rn
                        FROM scan_queue WHERE status = 'pending'
                    ) AS t WHERE t.id = scan_queue.id
                ) WHERE status = 'pending'
            """)
            conn.commit()


# ── Queue Overhaul: Download Queue Reordering ────────────────────────

def reorder_download_queue(file_id, new_position):
    """Move a file to a new queue_position, shifting others as needed."""
    with _download_queue_lock:
        with _db() as conn:
            # Get current position
            row = conn.execute(
                "SELECT queue_position FROM archive_files WHERE id = ? AND queue_position IS NOT NULL",
                (file_id,),
            ).fetchone()
            if not row:
                return
            old_pos = row["queue_position"]
            if old_pos == new_position:
                return
            if new_position < old_pos:
                # Moving up: shift items in [new_pos, old_pos) down by 1
                conn.execute(
                    """UPDATE archive_files SET queue_position = queue_position + 1
                       WHERE queue_position >= ? AND queue_position < ? AND queue_position IS NOT NULL""",
                    (new_position, old_pos),
                )
            else:
                # Moving down: shift items in (old_pos, new_pos] up by 1
                conn.execute(
                    """UPDATE archive_files SET queue_position = queue_position - 1
                       WHERE queue_position > ? AND queue_position <= ? AND queue_position IS NOT NULL""",
                    (old_pos, new_position),
                )
            conn.execute(
                "UPDATE archive_files SET queue_position = ? WHERE id = ?",
                (new_position, file_id),
            )
            conn.commit()


# ── Queue Overhaul: Processing Queue ─────────────────────────────────

def add_processing_queue_entry(job_id, file_id, archive_id, profile_id, options_json=None):
    """Add a file-level entry to the processing queue at the end."""
    with _db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM processing_queue WHERE status = 'pending'"
        ).fetchone()[0]
        now = time.time()
        conn.execute(
            """INSERT INTO processing_queue
               (job_id, file_id, archive_id, profile_id, options_json, status, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (job_id, file_id, archive_id, profile_id,
             json.dumps(options_json) if options_json else None,
             max_pos + 1, now, now),
        )
        conn.commit()


def add_processing_queue_entries_batch(job_id, entries):
    """Batch-add multiple processing queue entries.
    entries is a list of (file_id, archive_id, profile_id, options_json) tuples."""
    with _db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM processing_queue WHERE status = 'pending'"
        ).fetchone()[0]
        now = time.time()
        for i, (file_id, archive_id, profile_id, options_json) in enumerate(entries):
            conn.execute(
                """INSERT INTO processing_queue
                   (job_id, file_id, archive_id, profile_id, options_json, status, position, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (job_id, file_id, archive_id, profile_id,
                 json.dumps(options_json) if options_json else None,
                 max_pos + 1 + i, now, now),
            )
        conn.commit()


def get_next_processing_queue_entry():
    """Get the next pending processing queue entry, ordered by position.
    Acquires the processing queue lock."""
    with _processing_queue_lock:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM processing_queue WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None


def claim_processing_queue_entry(entry_id):
    """Atomically claim a pending processing queue entry."""
    with _db() as conn:
        conn.execute(
            "UPDATE processing_queue SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
            (time.time(), entry_id),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return changed > 0


def complete_processing_queue_entry(entry_id, error_message=None):
    """Mark a processing queue entry as completed or failed."""
    status = "failed" if error_message else "completed"
    with _db() as conn:
        conn.execute(
            "UPDATE processing_queue SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
            (status, error_message or "", time.time(), entry_id),
        )
        conn.commit()


def cancel_processing_queue_entry(entry_id):
    """Cancel a single processing queue entry."""
    with _db() as conn:
        conn.execute(
            "UPDATE processing_queue SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('pending', 'running')",
            (time.time(), entry_id),
        )
        conn.commit()


def cancel_all_pending_processing():
    """Cancel all pending processing queue entries. Returns count cancelled."""
    with _db() as conn:
        conn.execute(
            "UPDATE processing_queue SET status = 'cancelled', updated_at = ? WHERE status = 'pending'",
            (time.time(),),
        )
        count = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return count


def get_processing_queue(limit=200):
    """Get the processing queue entries in position order."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT pq.*, af.name as file_name, af.size as file_size, af.downloaded,
                   a.identifier as archive_identifier, a.title as archive_title,
                   pp.name as profile_name
            FROM processing_queue pq
            JOIN archive_files af ON pq.file_id = af.id
            JOIN archives a ON pq.archive_id = a.id
            LEFT JOIN processing_profiles pp ON pq.profile_id = pp.id
            WHERE pq.status IN ('pending', 'running')
            ORDER BY pq.position ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def reorder_processing_queue(entry_id, new_position):
    """Move a processing queue entry to a new position."""
    with _processing_queue_lock:
        with _db() as conn:
            row = conn.execute(
                "SELECT position FROM processing_queue WHERE id = ? AND status = 'pending'",
                (entry_id,),
            ).fetchone()
            if not row:
                return
            old_pos = row["position"]
            if old_pos == new_position:
                return
            if new_position < old_pos:
                conn.execute(
                    """UPDATE processing_queue SET position = position + 1
                       WHERE position >= ? AND position < ? AND status = 'pending'""",
                    (new_position, old_pos),
                )
            else:
                conn.execute(
                    """UPDATE processing_queue SET position = position - 1
                       WHERE position > ? AND position <= ? AND status = 'pending'""",
                    (old_pos, new_position),
                )
            conn.execute(
                "UPDATE processing_queue SET position = ?, updated_at = ? WHERE id = ?",
                (new_position, time.time(), entry_id),
            )
            conn.commit()


# ── Queue Overhaul: Scan Queue ───────────────────────────────────────

def add_scan_queue_entry(file_id, archive_id, batch_id=None, priority=False):
    """Add a file to the scan queue. If priority=True, insert at position 0."""
    with _db() as conn:
        if priority:
            position = 0
            # Shift existing entries down
            conn.execute(
                "UPDATE scan_queue SET position = position + 1 WHERE status = 'pending'"
            )
        else:
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM scan_queue WHERE status = 'pending'"
            ).fetchone()[0]
            position = max_pos + 1
        conn.execute(
            "INSERT INTO scan_queue (file_id, archive_id, batch_id, status, position, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (file_id, archive_id, batch_id, position, time.time()),
        )
        # Set file status to scan_pending
        conn.execute(
            "UPDATE archive_files SET download_status = 'scan_pending' WHERE id = ? AND download_status NOT IN ('downloading')",
            (file_id,),
        )
        conn.commit()


def add_scan_queue_entries_batch(archive_id, file_ids, batch_id=None):
    """Batch-add files to the scan queue at the end. Returns count added."""
    with _db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM scan_queue WHERE status = 'pending'"
        ).fetchone()[0]
        now = time.time()
        for i, file_id in enumerate(file_ids):
            conn.execute(
                "INSERT INTO scan_queue (file_id, archive_id, batch_id, status, position, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                (file_id, archive_id, batch_id, max_pos + 1 + i, now),
            )
        # Batch-update file statuses
        if file_ids:
            placeholders = ",".join("?" * len(file_ids))
            conn.execute(
                f"UPDATE archive_files SET download_status = 'scan_pending' WHERE id IN ({placeholders}) AND download_status NOT IN ('downloading')",
                file_ids,
            )
        conn.commit()
        return len(file_ids)


def get_next_scan_queue_entry():
    """Get the next pending scan queue entry, respecting archive non-interleaving.
    Priority rescans (position 0) are exempt from grouping.
    Acquires the scan queue lock."""
    with _scan_queue_lock:
        with _db() as conn:
            # First check for priority rescans (position 0)
            row = conn.execute(
                "SELECT * FROM scan_queue WHERE status = 'pending' AND position = 0 ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row:
                return dict(row)

            # Check if there's a currently running scan — prefer same archive
            running = conn.execute(
                "SELECT archive_id FROM scan_queue WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if running:
                # Continue with same archive
                row = conn.execute(
                    "SELECT * FROM scan_queue WHERE status = 'pending' AND archive_id = ? ORDER BY position ASC LIMIT 1",
                    (running["archive_id"],),
                ).fetchone()
                if row:
                    return dict(row)

            # No running scan or no more entries for current archive — pick next archive
            row = conn.execute(
                "SELECT * FROM scan_queue WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None


def claim_scan_queue_entry(entry_id):
    """Atomically claim a pending scan queue entry."""
    with _db() as conn:
        conn.execute(
            "UPDATE scan_queue SET status = 'running' WHERE id = ? AND status = 'pending'",
            (entry_id,),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return changed > 0


def complete_scan_queue_entry(entry_id, error_message=None):
    """Mark a scan queue entry as completed or failed."""
    status = "failed" if error_message else "completed"
    with _db() as conn:
        conn.execute(
            "UPDATE scan_queue SET status = ? WHERE id = ?",
            (status, entry_id),
        )
        conn.commit()


def cancel_all_pending_scans():
    """Cancel all pending scan queue entries. Returns count cancelled."""
    with _db() as conn:
        conn.execute("UPDATE scan_queue SET status = 'cancelled' WHERE status = 'pending'")
        count = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return count


def get_scan_queue(limit=200):
    """Get the scan queue entries in position order."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT sq.*, af.name as file_name, af.size as file_size, af.downloaded,
                   a.identifier as archive_identifier, a.title as archive_title
            FROM scan_queue sq
            JOIN archive_files af ON sq.file_id = af.id
            JOIN archives a ON sq.archive_id = a.id
            WHERE sq.status IN ('pending', 'running')
            ORDER BY sq.position ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def reorder_scan_queue(entry_id, new_position):
    """Move a scan queue entry to a new position."""
    with _scan_queue_lock:
        with _db() as conn:
            row = conn.execute(
                "SELECT position FROM scan_queue WHERE id = ? AND status = 'pending'",
                (entry_id,),
            ).fetchone()
            if not row:
                return
            old_pos = row["position"]
            if old_pos == new_position:
                return
            if new_position < old_pos:
                conn.execute(
                    """UPDATE scan_queue SET position = position + 1
                       WHERE position >= ? AND position < ? AND status = 'pending'""",
                    (new_position, old_pos),
                )
            else:
                conn.execute(
                    """UPDATE scan_queue SET position = position - 1
                       WHERE position > ? AND position <= ? AND status = 'pending'""",
                    (old_pos, new_position),
                )
            conn.execute(
                "UPDATE scan_queue SET position = ? WHERE id = ?",
                (new_position, entry_id),
            )
            conn.commit()


def is_archive_scan_complete(archive_id):
    """Check if all scan queue entries for an archive are done (none pending/running)."""
    with _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM scan_queue WHERE archive_id = ? AND status IN ('pending', 'running')",
            (archive_id,),
        ).fetchone()[0]
        return count == 0


def count_pending_scan_entries(archive_id):
    """Count pending scan queue entries for an archive."""
    with _db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM scan_queue WHERE archive_id = ? AND status = 'pending'",
            (archive_id,),
        ).fetchone()[0]


def clear_completed_scan_entries(archive_id=None):
    """Remove completed/failed/cancelled scan queue entries. If archive_id given, only that archive."""
    with _db() as conn:
        if archive_id:
            conn.execute(
                "DELETE FROM scan_queue WHERE archive_id = ? AND status IN ('completed', 'failed', 'cancelled')",
                (archive_id,),
            )
        else:
            conn.execute("DELETE FROM scan_queue WHERE status IN ('completed', 'failed', 'cancelled')")
        conn.commit()


# ── Queue Overhaul: Queue Counts ─────────────────────────────────────

def get_queue_counts():
    """Return total queue counts across all three queue types for the topbar badge."""
    with _db() as conn:
        download = conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE queue_position IS NOT NULL"
        ).fetchone()[0]
        processing = conn.execute(
            "SELECT COUNT(*) FROM processing_queue WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        scan = conn.execute(
            "SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        return {"download": download, "processing": processing, "scan": scan}


# ── Queue Overhaul: Download State Persistence ───────────────────────

def get_download_state():
    """Return the persisted download state ('stopped', 'paused', or 'running')."""
    return get_setting("download_state", "stopped")


def set_download_state(state):
    """Persist the download state."""
    assert state in ("stopped", "paused", "running"), f"Invalid download state: {state}"
    set_setting("download_state", state)


def is_processing_paused():
    """Check if processing is paused."""
    return get_setting("processing_paused", "0") == "1"


def set_processing_paused(paused):
    """Set the processing pause state."""
    set_setting("processing_paused", "1" if paused else "0")


def is_scan_paused():
    """Check if scanning is paused."""
    return get_setting("scan_paused", "0") == "1"


def set_scan_paused(paused):
    """Set the scan pause state."""
    set_setting("scan_paused", "1" if paused else "0")
