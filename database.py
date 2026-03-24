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
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash

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
            selected INTEGER NOT NULL DEFAULT 1,
            download_status TEXT NOT NULL DEFAULT 'pending',
            downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            change_status TEXT NOT NULL DEFAULT '',
            change_detail TEXT NOT NULL DEFAULT '',
            download_priority INTEGER NOT NULL DEFAULT 0,
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

    try:
        conn.execute("SELECT download_priority FROM archive_files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE archive_files ADD COLUMN download_priority INTEGER NOT NULL DEFAULT 0")
        # Initialise priorities by current name order within each archive
        conn.execute("""
            UPDATE archive_files SET download_priority = (
                SELECT COUNT(*) FROM archive_files af2
                WHERE af2.archive_id = archive_files.archive_id
                  AND af2.name < archive_files.name
            )
        """)

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

    # Fix any scan-inserted rows incorrectly tagged as 'manifest'.
    # Real IA files always have at least one metadata field populated;
    # scan-inserted files have md5, sha1, format, source, and mtime all empty.
    conn.execute("""
        UPDATE archive_files SET origin = 'scan'
        WHERE origin = 'manifest'
          AND md5 = '' AND sha1 = '' AND format = '' AND source = '' AND mtime = ''
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
            WHERE archive_id = ? AND selected = 1 AND origin = 'manifest'
        """, (aid,)).fetchone()
        if counts["total"] > 0:
            if counts["completed"] == counts["total"]:
                conn.execute("UPDATE archives SET status = 'completed' WHERE id = ?", (aid,))
            elif counts["completed"] + counts["failed"] + counts["conflict"] == counts["total"]:
                conn.execute("UPDATE archives SET status = 'partial' WHERE id = ?", (aid,))
            else:
                conn.execute("UPDATE archives SET status = 'idle' WHERE id = ?", (aid,))

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
            # Start priority after existing files (atomic within transaction)
            max_pri = conn.execute(
                "SELECT COALESCE(MAX(download_priority), -1) FROM archive_files WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()[0]
            for i, f in enumerate(files):
                conn.execute(
                    """INSERT OR IGNORE INTO archive_files
                       (archive_id, name, size, md5, sha1, format, source, mtime, selected, download_priority)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (archive_id, f["name"], int(f.get("size", 0) or 0),
                     f.get("md5", ""), f.get("sha1", ""), f.get("format", ""),
                     f.get("source", ""), f.get("mtime", ""), max_pri + 1 + i),
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
               SUM(CASE WHEN selected = 1 AND download_status = 'completed' THEN 1 ELSE 0 END) AS completed_files,
               SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected_files,
               SUM(CASE WHEN selected = 1 THEN size ELSE 0 END) AS selected_size,
               SUM(CASE WHEN selected = 1 THEN downloaded_bytes ELSE 0 END) AS downloaded_bytes
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
            SELECT SUM(CASE WHEN selected = 1 AND download_status = 'completed' THEN 1 ELSE 0 END) AS completed_files,
                   SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected_files,
                   SUM(CASE WHEN selected = 1 THEN size ELSE 0 END) AS selected_size,
                   SUM(CASE WHEN selected = 1 THEN downloaded_bytes ELSE 0 END) AS downloaded_bytes
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
            WHERE archive_id = ? AND selected = 1 AND origin = 'manifest'
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
    "priority": ("download_priority", "ASC"),
}

# Effective status mirrors the JS formatFileStatus logic:
# processing_status takes priority, then selected+download_status determines "skipped"
_EFFECTIVE_STATUS_EXPR = """CASE
    WHEN processing_status = 'completed' THEN 'processed'
    WHEN processing_status = 'extracted' THEN 'extracted'
    WHEN processing_status = 'processing' THEN 'processing'
    WHEN processing_status = 'queued' THEN 'proc_queued'
    WHEN processing_status = 'failed' THEN 'proc_failed'
    WHEN processing_status = 'skipped' THEN 'proc_skipped'
    WHEN selected = 0 AND download_status = 'pending' THEN 'skipped'
    ELSE download_status
END"""


def get_archive_files(archive_id, page=1, per_page=50, sort="name", sort_dir=None, search=""):
    with _db() as conn:
        offset = (page - 1) * per_page
        col, default_dir = _FILE_SORT_MAP.get(sort, _FILE_SORT_MAP["name"])
        direction = sort_dir.upper() if sort_dir in ("asc", "desc") else default_dir
        if sort == "priority":
            order = f"selected DESC, download_priority {direction}"
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
            f"SELECT * FROM archive_files WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def reorder_archive_files(file_ids):
    """Reorder files so their download_priority values match the given ID order.

    Reads the current priorities of the supplied files, sorts those priority
    slots, then assigns them back in the new order.  Files NOT in the list
    keep their priorities untouched, so pagination is safe.
    """
    if not file_ids:
        return
    with _db() as conn:
        placeholders = ",".join("?" * len(file_ids))
        rows = conn.execute(
            f"SELECT id, download_priority FROM archive_files WHERE id IN ({placeholders})",
            file_ids,
        ).fetchall()
        # Map id -> current priority
        pri_map = {r["id"]: r["download_priority"] for r in rows}
        # Collect the priority slots in their current sorted order
        slots = sorted(pri_map[fid] for fid in file_ids if fid in pri_map)
        # Assign slots in the new order
        for slot, fid in zip(slots, file_ids):
            if fid in pri_map:
                conn.execute("UPDATE archive_files SET download_priority = ? WHERE id = ?", (slot, fid))
        conn.commit()


def reset_file_priorities(archive_id):
    """Reset download_priority for all files in an archive to alphabetical name order."""
    with _db() as conn:
        conn.execute("""
            UPDATE archive_files SET download_priority = (
                SELECT COUNT(*) FROM archive_files af2
                WHERE af2.archive_id = archive_files.archive_id
                  AND af2.name < archive_files.name
            ) WHERE archive_id = ?
        """, (archive_id,))
        conn.commit()


def count_unselected_files(archive_id):
    with _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE archive_id = ? AND selected = 0",
            (archive_id,),
        ).fetchone()[0]
        return count


def set_file_selected(file_id, selected):
    with _db() as conn:
        conn.execute("UPDATE archive_files SET selected = ? WHERE id = ?", (1 if selected else 0, file_id))
        conn.commit()


def set_all_files_selected(archive_id, selected):
    with _db() as conn:
        conn.execute("UPDATE archive_files SET selected = ? WHERE archive_id = ?", (1 if selected else 0, archive_id))
        conn.commit()


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
        params.append(file_id)
        conn.execute(f"UPDATE archive_files SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()


def increment_file_retry(file_id):
    with _db() as conn:
        conn.execute("UPDATE archive_files SET retry_count = retry_count + 1 WHERE id = ?", (file_id,))
        conn.commit()


def get_next_download_file():
    """Get the next file to download: from the highest-priority enabled archive, first pending file,
    then failed files that haven't exhausted retries."""
    with _db() as conn:
        max_retries = int(get_setting("max_retries") or 3)
        row = conn.execute("""
            SELECT af.*, a.identifier, a.server, a.dir, a.id as archive_id
            FROM archive_files af
            JOIN archives a ON af.archive_id = a.id
            WHERE a.download_enabled = 1
              AND af.selected = 1
              AND (af.download_status = 'pending'
                   OR (af.download_status = 'failed' AND af.retry_count < ?))
            ORDER BY a.position ASC,
                     CASE af.download_status WHEN 'pending' THEN 0 ELSE 1 END,
                     af.download_priority ASC
            LIMIT 1
        """, (max_retries,)).fetchone()
        return dict(row) if row else None


def get_download_progress():
    """Get overall download progress stats."""
    with _db() as conn:
        stats = {}
        row = conn.execute("""
            SELECT
                COUNT(*) as total_files,
                SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) as completed_files,
                SUM(CASE WHEN download_status = 'downloading' THEN 1 ELSE 0 END) as active_files,
                SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) as failed_files,
                SUM(CASE WHEN selected = 1 AND download_status IN ('pending', 'failed') THEN 1 ELSE 0 END) as queued_files,
                SUM(size) as total_size,
                SUM(downloaded_bytes) as downloaded_bytes
            FROM archive_files af
            JOIN archives a ON af.archive_id = a.id
            WHERE a.download_enabled = 1 AND af.selected = 1
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
        for name, new_f in incoming.items():
            if name not in existing:
                conn.execute(
                    """INSERT INTO archive_files
                       (archive_id, name, size, md5, sha1, format, source, mtime,
                        selected, change_status, change_detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'new', 'Newly added to Internet Archive since last check')""",
                    (archive_id, name, int(new_f.get("size", 0) or 0),
                     new_f.get("md5", ""), new_f.get("sha1", ""), new_f.get("format", ""),
                     new_f.get("source", ""), new_f.get("mtime", "")),
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
            "WHERE archive_id = ? AND processing_status IN ('completed', 'extracted')",
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


def get_processable_files(archive_id, processor_types=None):
    """Get files eligible for processing: completed downloads, not already processed.
    Optionally filter by file extensions matching processor input types."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM archive_files
               WHERE archive_id = ? AND download_status = 'completed'
                 AND processing_status IN ('', 'failed')
                 AND origin = 'manifest'
               ORDER BY download_priority ASC""",
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
