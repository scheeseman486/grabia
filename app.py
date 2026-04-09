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

"""Flask application for Grabia."""

import os
import json
import time
import queue
import hashlib
import threading
import functools
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
import activity
import database as db
import ia_client
from downloader import download_manager
from logger import log, configure as configure_logging

app = Flask(__name__)

def _data_dir():
    """Return the data directory (for DB, secret key, etc.)."""
    return os.environ.get("GRABIA_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))


def _get_secret_key():
    """Load or generate a persistent secret key."""
    key_file = os.path.join(_data_dir(), ".secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r") as f:
            return f.read().strip()
    key = os.urandom(32).hex()
    with open(key_file, "w") as f:
        f.write(key)
    os.chmod(key_file, 0o600)
    return key

app.secret_key = os.environ.get("GRABIA_SECRET_KEY") or os.environ.get("HORNBEAM_SECRET_KEY") or _get_secret_key()


@app.errorhandler(500)
def handle_500(e):
    """Return JSON for API 500 errors instead of Flask's default HTML page."""
    if request.path.startswith("/api/"):
        return jsonify({"error": f"Internal server error: {e}"}), 500
    return e


@app.context_processor
def cache_buster():
    """Add file mtime as cache-busting query param for static assets."""
    def static_url(filename):
        filepath = os.path.join(app.static_folder, filename)
        try:
            mtime = int(os.path.getmtime(filepath))
        except OSError:
            mtime = 0
        return f"/static/{filename}?v={mtime}"
    return dict(static_url=static_url)


# SSE event queue for broadcasting to clients
sse_queues = []
sse_lock = threading.Lock()


def broadcast_sse(event, data):
    """Send an SSE event to all connected clients."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_queues.remove(q)
            # Drain the dead queue to free buffered messages
            try:
                while not q.empty():
                    q.get_nowait()
            except Exception:
                pass


# Hook download manager events into SSE
def on_download_event(event, data):
    broadcast_sse(event, data or {})


download_manager.add_listener(on_download_event)


# --- Auth ---

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not db.is_auth_setup():
            # No password set yet — redirect to setup
            if request.path.startswith("/api/"):
                return jsonify({"error": "Setup required"}), 403
            return redirect(url_for("setup_page"))
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/setup", methods=["GET"])
def setup_page():
    if db.is_auth_setup():
        return redirect(url_for("login_page"))
    return render_template("setup.html")


@app.route("/setup", methods=["POST"])
def setup_submit():
    if db.is_auth_setup():
        return jsonify({"error": "Already configured"}), 400
    data = request.json
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
    db.create_auth(username, password)
    session["authenticated"] = True
    session["username"] = username
    return jsonify({"ok": True})


@app.route("/login", methods=["GET"])
def login_page():
    if not db.is_auth_setup():
        return redirect(url_for("setup_page"))
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.json
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if db.verify_auth(username, password):
        session["authenticated"] = True
        session["username"] = username
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.json
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not new_pw or len(new_pw) < 4:
        return jsonify({"error": "New password must be at least 4 characters"}), 400
    if db.change_password(old_pw, new_pw):
        return jsonify({"ok": True})
    return jsonify({"error": "Current password is incorrect"}), 401


# --- Pages ---

@app.route("/")
@login_required
def index():
    return render_template("index.html")


# --- SSE ---

@app.route("/api/events")
@login_required
def events():
    def stream():
        q = queue.Queue(maxsize=50)
        with sse_lock:
            sse_queues.append(q)
        try:
            # Send initial state
            status = download_manager.get_status()
            yield f"event: status\ndata: {json.dumps(status)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    if msg is None:
                        break  # Poison pill — server is shutting down
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if q in sse_queues:
                    sse_queues.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- Settings API ---

@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    settings = db.get_all_settings()
    # Don't expose IA password in full
    if settings.get("ia_password"):
        settings["ia_password_set"] = True
        settings["ia_password"] = ""
    else:
        settings["ia_password_set"] = False
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
@login_required
def update_settings():
    data = request.json
    allowed = ["ia_email", "ia_password", "download_dir", "processed_dir",
               "max_retries",
               "retry_delay", "bandwidth_limit", "theme", "files_per_page",
               "speed_schedule", "use_http",
               "confirm_reset_order", "confirm_delete_file",
               "confirm_batch_delete_files", "confirm_delete_folders",
               "confirm_delete_processed", "confirm_delete_profile",
               "default_enable_archive", "default_select_all", "sse_update_rate",
               "processing_temp_dir",
               "debug_enabled", "debug_log_file",
               "max_connections_per_node", "max_connections_total"]
    credentials_changed = False
    for key in allowed:
        if key in data:
            # Don't overwrite password with empty string if it was just hidden
            if key == "ia_password" and data[key] == "":
                continue
            db.set_setting(key, data[key])
            if key in ("ia_email", "ia_password"):
                credentials_changed = True
    if credentials_changed:
        ia_client.invalidate_cookie_cache()
    # Update bandwidth limit in running manager
    if "bandwidth_limit" in data:
        download_manager.bandwidth_limit = int(data["bandwidth_limit"])
    # Reconfigure debug logging if changed
    if "debug_enabled" in data or "debug_log_file" in data:
        configure_logging(
            enabled=db.get_setting("debug_enabled", "0") == "1",
            log_file=db.get_setting("debug_log_file", ""),
        )
    broadcast_sse("settings_updated", db.get_all_settings())
    return jsonify({"ok": True})



@app.route("/api/settings/test-credentials", methods=["POST"])
@login_required
def test_ia_credentials():
    """Test IA credentials and return result."""
    ia_email = db.get_setting("ia_email", "")
    ia_password = db.get_setting("ia_password", "")
    if not ia_email or not ia_password:
        return jsonify({"ok": False, "message": "IA email and password must be saved first"}), 400
    success, message = ia_client.test_credentials(ia_email, ia_password)
    return jsonify({"ok": success, "message": message})


# --- Group API ---

@app.route("/api/groups", methods=["GET"])
@login_required
def list_groups():
    return jsonify(db.get_groups())


@app.route("/api/groups", methods=["POST"])
@login_required
def create_group():
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    group_id = db.add_group(name)
    group = db.get_group(group_id)
    broadcast_sse("groups_changed", {})
    return jsonify(group), 201


@app.route("/api/groups/<int:group_id>", methods=["PUT"])
@login_required
def update_group(group_id):
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    db.rename_group(group_id, name)
    broadcast_sse("groups_changed", {})

    # Refresh auto-tags for all archives in this group — group name changed
    try:
        from auto_tagger import _refresh_archive_auto_tags
        archives = db.get_archives()
        for a in archives:
            if a.get("group_id") == group_id:
                _refresh_archive_auto_tags(a["id"])
    except Exception as e:
        log.warning("Auto-tag refresh failed after group %d rename: %s", group_id, e)

    return jsonify({"ok": True})


@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
@login_required
def remove_group(group_id):
    # Collect archives in this group before deletion so we can refresh their tags
    affected_archive_ids = []
    try:
        archives = db.get_archives()
        affected_archive_ids = [a["id"] for a in archives if a.get("group_id") == group_id]
    except Exception:
        pass

    db.delete_group(group_id)
    broadcast_sse("groups_changed", {})

    # Refresh auto-tags — these archives no longer have a group
    if affected_archive_ids:
        try:
            from auto_tagger import _refresh_archive_auto_tags
            for aid in affected_archive_ids:
                _refresh_archive_auto_tags(aid)
        except Exception as e:
            log.warning("Auto-tag refresh failed after group %d deletion: %s", group_id, e)

    return jsonify({"ok": True})


@app.route("/api/groups/reorder", methods=["POST"])
@login_required
def reorder_groups():
    data = request.json
    db.reorder_groups(data.get("order", []))
    broadcast_sse("groups_changed", {})
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/group", methods=["POST"])
@login_required
def set_archive_group(archive_id):
    data = request.json
    group_id = data.get("group_id")  # None to remove from group
    db.set_archive_group(archive_id, group_id)
    archive = db.get_archive(archive_id)
    broadcast_sse("archive_updated", archive)

    # Refresh auto-tags — group assignment changes the group: tag
    try:
        from auto_tagger import _refresh_archive_auto_tags
        _refresh_archive_auto_tags(archive_id)
    except Exception as e:
        log.warning("Auto-tag refresh failed for archive %d after group change: %s", archive_id, e)

    return jsonify(archive)


# --- Archive API ---

@app.route("/api/archives", methods=["GET"])
@login_required
def list_archives():
    archives = db.get_archives()
    return jsonify(archives)


@app.route("/api/archives", methods=["POST"])
@login_required
def add_archive():
    data = request.json
    url_or_id = data.get("url", "").strip()
    if not url_or_id:
        return jsonify({"error": "URL is required"}), 400

    identifier = ia_client.parse_identifier(url_or_id)
    if not identifier:
        return jsonify({"error": "Could not parse identifier from URL"}), 400

    # Check for duplicates
    existing = db.get_archive_by_identifier(identifier)
    if existing:
        return jsonify({"error": f"Archive '{identifier}' already exists"}), 409

    # Run in background so the request returns immediately
    options = {
        "enable": data.get("enable", False),
        "select_all": data.get("select_all", True),
        "group_id": data.get("group_id"),
    }
    threading.Thread(
        target=_add_archive_bg,
        args=(identifier, options),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "identifier": identifier, "status": "fetching"}), 202


def _add_archive_bg(identifier, options):
    """Background worker: fetch IA metadata, create archive, broadcast progress."""
    import activity

    act_job_id = activity.start_job("metadata", archive_id=None)
    activity.log(act_job_id, "info", f"Fetching metadata for {identifier}",
                 detail=identifier)
    activity.flush()

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "phase": "fetching",
        "message": f"Fetching metadata for {identifier}...",
    })

    try:
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        use_http = db.get_setting("use_http", "0") == "1"
        meta = ia_client.fetch_metadata(identifier, ia_email, ia_password, use_http=use_http)
    except Exception as e:
        broadcast_sse("metadata_progress", {
            "identifier": identifier,
            "phase": "error",
            "message": f"Failed to fetch metadata: {e}",
        })
        activity.log(act_job_id, "error", f"Failed to fetch metadata: {e}",
                     detail=identifier)
        activity.flush()
        activity.finish_job(act_job_id, "failed", summary=f"Failed: {e}")
        return

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "phase": "storing",
        "message": f"Storing {meta['files_count']} files...",
    })

    archive_id = db.add_archive(
        identifier=meta["identifier"],
        url=meta["url"],
        title=meta["title"],
        description=meta["description"],
        total_size=meta["total_size"],
        files_count=meta["files_count"],
        metadata_json=meta["metadata"],
        server=meta["server"],
        dir_path=meta["dir"],
    )
    db.add_archive_files(archive_id, meta["files"])

    # Update the activity job now that we have an archive_id
    with db._db() as conn:
        conn.execute("UPDATE activity_jobs SET archive_id = ? WHERE id = ?",
                     (archive_id, act_job_id))
        conn.commit()

    # Auto-tag based on filenames
    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "tagging",
        "message": "Auto-tagging files...",
    })
    try:
        from auto_tagger import auto_tag_archive
        auto_tag_archive(archive_id)
    except Exception as e:
        log.warning("Auto-tag failed for archive %d: %s", archive_id, e)

    # Apply options from the add modal
    if options.get("enable"):
        db.set_archive_download_enabled(archive_id, True)
    if not options.get("select_all", True):
        db.set_all_files_queued(archive_id, False)
    if options.get("group_id"):
        db.set_archive_group(archive_id, options["group_id"])

    archive = db.get_archive(archive_id)
    broadcast_sse("archive_added", archive)

    # Fetch view_archive.php contents inline with progress
    fetched, total_archives = _fetch_archive_contents_inline(
        archive_id, identifier, act_job_id=act_job_id)

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "done",
        "message": f"Added {meta['title'] or identifier} ({meta['files_count']} files)",
    })

    summary_parts = [f"{meta['files_count']} files"]
    if fetched:
        summary_parts.append(f"{fetched} archive(s) inspected")
    activity.log(act_job_id, "success",
                 f"Added {meta['title'] or identifier}: {', '.join(summary_parts)}",
                 archive_id=archive_id)
    activity.flush()
    activity.finish_job(act_job_id, "completed",
                        summary=f"Added: {', '.join(summary_parts)}")


@app.route("/api/archives/<int:archive_id>", methods=["GET"])
@login_required
def get_archive(archive_id):
    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Not found"}), 404
    return jsonify(archive)


@app.route("/api/archives/<int:archive_id>", methods=["DELETE"])
@login_required
def delete_archive(archive_id):
    db.delete_archive(archive_id)
    broadcast_sse("archive_removed", {"id": archive_id})
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/delete-folder", methods=["POST"])
@login_required
def delete_archive_folder(archive_id):
    """Delete the download folder for an archive from disk, reset file statuses."""
    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Not found"}), 404

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
    processed_dir = db.get_processed_dir()
    proc_base = os.path.realpath(os.path.join(processed_dir, archive["identifier"]))

    import shutil
    removed = False
    if os.path.isdir(base_dir) and base_dir.startswith(os.path.realpath(download_dir) + os.sep):
        shutil.rmtree(base_dir)
        removed = True
    if os.path.isdir(proc_base) and proc_base.startswith(os.path.realpath(processed_dir) + os.sep):
        shutil.rmtree(proc_base)
        removed = True

    # Reset all files to pending and clear overlay
    conn = db.get_db()
    conn.execute(
        "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = 0, "
        "processing_error = '', error_message = '', process_queue_status = '' WHERE archive_id = ?",
        (archive_id,),
    )
    conn.execute("DELETE FROM local_files WHERE archive_id = ?", (archive_id,))
    conn.commit()
    conn.close()
    db.recompute_archive_status(archive_id)
    updated = db.get_archive(archive_id)
    broadcast_sse("archive_updated", updated)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/archives/<int:archive_id>/refresh", methods=["POST"])
@login_required
def refresh_archive(archive_id):
    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Not found"}), 404

    # Run in background so the request returns immediately
    threading.Thread(
        target=_refresh_archive_bg,
        args=(archive_id,),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "status": "refreshing"}), 202


def _refresh_archive_bg(archive_id):
    """Background worker: refresh archive metadata and broadcast progress."""
    import activity

    archive = db.get_archive(archive_id)
    if not archive:
        return
    identifier = archive["identifier"]
    title = archive.get("title") or identifier

    act_job_id = activity.start_job("metadata", archive_id=archive_id)
    activity.log(act_job_id, "info", f"Refreshing metadata for {title}",
                 archive_id=archive_id)
    activity.flush()

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "fetching",
        "message": f"Refreshing metadata for {title}...",
    })

    try:
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        use_http = db.get_setting("use_http", "0") == "1"
        meta = ia_client.fetch_metadata(identifier, ia_email, ia_password, use_http=use_http)
    except Exception as e:
        broadcast_sse("metadata_progress", {
            "identifier": identifier,
            "archive_id": archive_id,
            "phase": "error",
            "message": f"Failed to fetch metadata: {e}",
        })
        activity.log(act_job_id, "error", f"Refresh failed: {e}",
                     archive_id=archive_id)
        activity.flush()
        activity.finish_job(act_job_id, "failed", summary=f"Failed: {e}")
        return

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "comparing",
        "message": f"Comparing {len(meta['files'])} files...",
    })

    summary = db.refresh_archive_metadata(archive_id, meta["files"])
    updated = db.get_archive(archive_id)
    broadcast_sse("archive_updated", updated)

    # Fetch view_archive.php contents inline with progress
    fetched, _ = _fetch_archive_contents_inline(
        archive_id, identifier, act_job_id=act_job_id)

    parts = []
    if summary.get("new", 0) > 0:
        parts.append(f"{summary['new']} new")
    if summary.get("removed", 0) > 0:
        parts.append(f"{summary['removed']} removed")
    if summary.get("changed", 0) > 0:
        parts.append(f"{summary['changed']} changed")
    changes_text = ", ".join(parts) if parts else "no changes"

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "done",
        "message": f"Refreshed {title}: {changes_text}",
        "summary": summary,
    })

    summary_msg = f"Refreshed {title}: {changes_text}"
    if fetched:
        summary_msg += f" ({fetched} archive(s) inspected)"
    activity.log(act_job_id, "success" if parts else "info",
                 summary_msg, archive_id=archive_id)
    activity.flush()
    activity.finish_job(act_job_id, "completed",
                        summary=f"Refresh: {changes_text}")


# --- Scan Queue (DB-backed) ---

_scan_cancel = {}  # archive_id -> threading.Event
_scan_options = {}  # archive_id -> {match_by_name: bool, ...}
_scan_lock = threading.Lock()
_scan_wake = threading.Event()  # signalled when new entries are added

_scan_conn = threading.local()  # holds the raw scan DB connection for cleanup

# Track the current archive-level scan context so we know when an archive group starts/ends
_scan_current_archive = {"id": None, "act_job_id": None, "notif_id": None, "summary": None, "total": 0, "processed": 0}


def wake_scan_worker():
    """Signal the scan worker that new entries are available."""
    _scan_wake.set()


def _scan_worker():
    """Background worker that processes scan queue entries from the DB."""
    while True:
        # Check pause state
        if db.get_setting("scan_paused", "0") == "1":
            _scan_wake.wait(timeout=2.0)
            _scan_wake.clear()
            continue

        entry = db.get_next_scan_queue_entry()
        if not entry:
            _scan_wake.wait(timeout=2.0)
            _scan_wake.clear()
            continue

        if not db.claim_scan_queue_entry(entry["id"]):
            continue  # someone else claimed it

        archive_id = entry["archive_id"]
        is_priority = entry["position"] == 0

        try:
            if is_priority:
                _run_single_file_scan(entry)
            else:
                _run_archive_scan_entry(entry)
        except Exception as e:
            db.complete_scan_queue_entry(entry["id"], error_message=str(e))
            log.error("scan", "Scan entry %d failed: %s", entry["id"], e)
            # If this was part of an archive group, check if group is done
            if not is_priority and _scan_current_archive["id"] == archive_id:
                if db.is_archive_scan_complete(archive_id):
                    _finish_archive_scan(archive_id)


_scan_thread = threading.Thread(target=_scan_worker, daemon=True)
_scan_thread.start()


# --- Content Fetch Worker (archive contents discovery) ---

_content_fetch_wake = threading.Event()

ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz"}


def wake_content_fetch_worker():
    """Signal the content fetch worker that new entries are available."""
    _content_fetch_wake.set()


def queue_archive_contents_fetch(archive_id, file_id=None):
    """Queue content fetch tasks for compressed files in an archive.
    If file_id is given, queue only that file. Otherwise queue all
    compressed files that haven't been fetched yet."""
    archive = db.get_archive(archive_id)
    if not archive:
        return 0

    if file_id:
        db.queue_content_fetch(file_id, archive_id, method="remote")
        wake_content_fetch_worker()
        return 1

    files, _ = db.get_archive_files(archive_id)
    count = 0
    for f in files:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in ARCHIVE_EXTENSIONS and f.get("origin", "manifest") == "manifest":
            if not f.get("contents_fetched_at"):
                db.queue_content_fetch(f["id"], archive_id, method="remote")
                count += 1
    if count:
        wake_content_fetch_worker()
    return count


def _fetch_archive_contents_inline(archive_id, identifier, act_job_id=None):
    """Fetch view_archive.php contents for all compressed files in an archive,
    running inline (not via the queue worker) with progress broadcasting.
    Used by the metadata background workers so content discovery is part of
    the same activity job.  Returns (fetched_count, total_count)."""
    import activity as _activity

    archive = db.get_archive(archive_id)
    if not archive:
        return 0, 0

    ia_email = db.get_setting("ia_email", "")
    ia_password = db.get_setting("ia_password", "")
    use_http = db.get_setting("use_http", "0") == "1"

    files, _ = db.get_archive_files(archive_id)
    targets = []
    for f in files:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in ARCHIVE_EXTENSIONS and f.get("origin", "manifest") == "manifest":
            if not f.get("contents_fetched_at"):
                targets.append(f)

    if not targets:
        return 0, 0

    total = len(targets)
    fetched = 0

    broadcast_sse("metadata_progress", {
        "identifier": identifier,
        "archive_id": archive_id,
        "phase": "contents",
        "message": f"Fetching archive contents (0/{total})...",
        "current": 0,
        "total": total,
    })

    for i, f in enumerate(targets):
        # Hash-based staleness: skip if already fetched and hash exists
        if f.get("contents_fetched_at") and f.get("md5"):
            fetched += 1
            continue

        try:
            contents = ia_client.fetch_archive_contents(
                identifier, f["name"],
                server=archive.get("server"), dir_path=archive.get("dir"),
                ia_email=ia_email, ia_password=ia_password,
                use_http=use_http,
            )
        except Exception as e:
            log.warning("content_fetch", "Failed to fetch contents for %s: %s",
                        f["name"], e)
            if act_job_id:
                _activity.log(act_job_id, "warning",
                              f"Could not fetch contents for {f['name']}: {e}",
                              archive_id=archive_id, file_id=f["id"])
            continue

        if contents is not None:
            count = db.add_archive_content_files(archive_id, f["id"], contents)
            db.set_contents_fetched(f["id"])
            fetched += 1
            if count > 0:
                broadcast_sse("archive_updated", {"id": archive_id})
                if act_job_id:
                    _activity.log(act_job_id, "info",
                                  f"Discovered {count} files inside {f['name']}",
                                  archive_id=archive_id, file_id=f["id"])
            # Check for nested archives
            _queue_nested_archive_inspection(archive_id, f["id"])

        broadcast_sse("metadata_progress", {
            "identifier": identifier,
            "archive_id": archive_id,
            "phase": "contents",
            "message": f"Fetching archive contents ({i + 1}/{total})...",
            "current": i + 1,
            "total": total,
        })

    if act_job_id:
        _activity.flush()

    return fetched, total


def _content_fetch_worker():
    """Background worker that fetches/inspects archive contents."""
    while True:
        entry = db.get_next_content_fetch()
        if not entry:
            _content_fetch_wake.wait(timeout=5.0)
            _content_fetch_wake.clear()
            continue

        try:
            _process_content_fetch(entry)
        except Exception as e:
            db.complete_content_fetch(entry["id"], error=str(e))
            log.error("content_fetch", "Content fetch %d failed: %s", entry["id"], e)


def _process_content_fetch(entry):
    """Process a single content fetch queue entry."""
    file_id = entry["file_id"]
    archive_id = entry["archive_id"]
    method = entry["method"]

    f = db.get_file(file_id)
    if not f:
        db.complete_content_fetch(entry["id"], error="File not found")
        return

    archive = db.get_archive(archive_id)
    if not archive:
        db.complete_content_fetch(entry["id"], error="Archive not found")
        return

    contents = None

    if method == "remote":
        # Fetch from IA's view_archive.php
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        use_http = db.get_setting("use_http", "0") == "1"

        # Hash-based staleness check: skip if the file hash hasn't changed
        if f.get("contents_fetched_at") and f.get("md5"):
            # Already fetched and hash available — skip unless forced
            db.complete_content_fetch(entry["id"])
            return

        contents = ia_client.fetch_archive_contents(
            archive["identifier"], f["name"],
            server=archive.get("server"), dir_path=archive.get("dir"),
            ia_email=ia_email, ia_password=ia_password,
            use_http=use_http,
        )
    elif method == "local":
        # Inspect a local archive on disk
        contents = _inspect_local_archive(archive, f)

    if contents is None:
        db.complete_content_fetch(entry["id"], error="Could not fetch contents")
        return

    # Store discovered files as archive_content rows
    count = db.add_archive_content_files(archive_id, file_id, contents)
    db.set_contents_fetched(file_id)
    db.complete_content_fetch(entry["id"])

    if count > 0:
        broadcast_sse("archive_updated", {"id": archive_id})

    # Check for nested archives in the discovered contents and queue local
    # inspection for any that are on disk
    _queue_nested_archive_inspection(archive_id, file_id)


def _inspect_local_archive(archive, file_info):
    """Inspect a local archive on disk to list its contents without extracting."""
    from processors import _list_archive_contents as list_contents

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    processed_dir = db.get_setting("processed_dir", "")
    if not processed_dir:
        processed_dir = os.path.join(os.path.dirname(download_dir), "processed")

    # Try both download and processed directories
    for base in (os.path.join(download_dir, archive["identifier"]),
                 os.path.join(processed_dir, archive["identifier"])):
        file_path = os.path.join(base, file_info["name"])
        if os.path.isfile(file_path):
            names = list_contents(file_path)
            if names is not None:
                # Convert flat name list to structured entries
                import zipfile as _zipfile
                entries = []
                ext = os.path.splitext(file_path)[1].lower()
                try:
                    if ext == ".zip":
                        with _zipfile.ZipFile(file_path, "r") as zf:
                            for info in zf.infolist():
                                if info.is_dir():
                                    continue
                                dt = info.date_time
                                mtime = f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} {dt[3]:02d}:{dt[4]:02d}:{dt[5]:02d}" if dt else ""
                                entries.append({
                                    "name": info.filename,
                                    "size": info.file_size,
                                    "mtime": mtime,
                                    "is_dir": False,
                                })
                    else:
                        # For 7z/rar, we only get names from _list_archive_contents
                        for name in names:
                            entries.append({
                                "name": name,
                                "size": 0,
                                "mtime": "",
                                "is_dir": False,
                            })
                except Exception as e:
                    log.warning("content_fetch", "Failed to inspect %s: %s", file_path, e)
                    return None
                return entries
    return None


def _queue_nested_archive_inspection(archive_id, parent_file_id):
    """After discovering contents of an archive, check if any of the contained
    files are themselves archives. If they're on disk, queue local inspection."""
    contained = db.get_archive_content_files(parent_file_id)
    for f in contained:
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in ARCHIVE_EXTENSIONS and not f.get("contents_fetched_at"):
            # Only queue local inspection if the file might be on disk
            if f["download_status"] in ("downloaded", "extracted"):
                db.queue_content_fetch(f["id"], archive_id, method="local")
    wake_content_fetch_worker()


_content_fetch_thread = None

def _start_content_fetch_worker():
    """Start the content fetch background thread.  Called from create_app()
    after the database has been initialised."""
    global _content_fetch_thread
    if _content_fetch_thread is not None:
        return
    _content_fetch_thread = threading.Thread(target=_content_fetch_worker, daemon=True)
    _content_fetch_thread.start()


def _run_single_file_scan(entry):
    """Process a priority single-file rescan (position 0). Runs standalone, not grouped."""
    file_id = entry["file_id"]
    archive_id = entry["archive_id"]
    f = db.get_file(file_id)
    if not f:
        db.complete_scan_queue_entry(entry["id"], error_message="File not found")
        return

    archive = db.get_archive(archive_id)
    if not archive:
        db.complete_scan_queue_entry(entry["id"], error_message="Archive not found")
        return

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    result = _scan_single_file_on_disk(f, base_dir, entry_id=entry["id"], identifier=archive["identifier"])
    db.complete_scan_queue_entry(entry["id"])
    db.recompute_archive_status(archive_id)
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "completed", "entry_id": entry["id"]})


def _resolve_processed_file(identifier, rel_path, base_dir):
    """Locate a processed file.

    Checks processed_dir/{identifier}/{rel_path} first, then falls back
    to base_dir/{rel_path} (download dir).
    """
    processed_dir = db.get_processed_dir()
    candidate = os.path.join(processed_dir, identifier, rel_path)
    if os.path.exists(candidate):
        return candidate
    return os.path.join(base_dir, rel_path)


def _get_processed_base(identifier):
    """Return the per-archive processed output directory."""
    return os.path.realpath(os.path.join(db.get_processed_dir(), identifier))


def _scan_single_file_on_disk(f, base_dir, entry_id=None, identifier=None):
    """Check a single manifest file against disk. Updates DB. Returns result status string.
    If entry_id is provided, broadcasts file-level progress SSE for the hash phase.
    ``identifier`` is the archive identifier used for processed-dir lookups."""
    file_id = f["id"]
    name = f["name"]
    local_path = os.path.realpath(os.path.join(base_dir, name))
    if not local_path.startswith(base_dir + os.sep) and local_path != base_dir:
        return "skipped"

    # Check if file has processed outputs in overlay
    proc_queue = f.get("process_queue_status") or ""
    has_overlay_outputs = bool(db.get_processed_outputs(file_id))

    if has_overlay_outputs or proc_queue == "failed":
        if has_overlay_outputs:
            # Processed output exists in overlay — check if original is still on disk
            if not os.path.isfile(local_path):
                # Original deleted but processed files remain
                with db._db() as conn:
                    conn.execute(
                        "UPDATE archive_files SET download_status = 'downloaded', "
                        "downloaded = 0, queue_position = NULL WHERE id = ?",
                        (file_id,),
                    )
                    conn.commit()
            else:
                # Original still on disk — ensure status is downloaded
                db.set_file_download_status(file_id, "downloaded",
                    downloaded_bytes=f.get("downloaded_bytes") or f.get("size", 0))
            return "matched"

        # Failed processing with no overlay outputs — clear stale state
        with db._db() as conn:
            conn.execute(
                "UPDATE archive_files SET processing_error = '', process_queue_status = '' WHERE id = ?",
                (file_id,),
            )
            conn.commit()

    if not os.path.isfile(local_path):
        # Check if this file already has processed outputs in the overlay
        if has_overlay_outputs:
            db.set_file_download_status(file_id, "downloaded",
                downloaded_bytes=f.get("downloaded_bytes") or 0)
            return "matched"
        db.set_file_download_status(file_id, "pending", downloaded_bytes=0)
        return "missing"

    local_size = os.path.getsize(local_path)
    expected_size = f["size"]
    expected_md5 = f["md5"]

    if f["download_status"] == "downloaded":
        return "matched"

    has_size = expected_size > 0
    has_md5 = bool(expected_md5) and has_size

    if not has_size and not has_md5:
        db.set_file_download_status(file_id, "conflict",
            error_message=f"Cannot verify: no size/hash in manifest (local file is {local_size} bytes)")
        return "conflict"

    if has_size and local_size != expected_size:
        if local_size < expected_size:
            db.set_file_download_status(file_id, "pending", downloaded_bytes=local_size,
                error_message="Partial download detected by scan")
            if db.get_file(file_id).get("queue_position") is None:
                db.set_file_queue_position(file_id)
            return "partial"
        else:
            db.set_file_download_status(file_id, "conflict",
                error_message=f"Size mismatch: local {local_size} vs expected {expected_size}")
            return "conflict"

    if has_md5:
        md5 = hashlib.md5()
        hashed_bytes = 0
        last_progress = 0.0
        try:
            with open(local_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(131072), b""):
                    md5.update(chunk)
                    hashed_bytes += len(chunk)
                    if entry_id is not None:
                        now = time.monotonic()
                        if now - last_progress >= 0.5 or hashed_bytes >= local_size:
                            last_progress = now
                            broadcast_sse("scan_file_progress", {
                                "entry_id": entry_id,
                                "file_id": file_id,
                                "phase": "hashing",
                                "bytes_done": hashed_bytes,
                                "bytes_total": local_size,
                            })
            if md5.hexdigest() != expected_md5:
                db.set_file_download_status(file_id, "conflict",
                    error_message=f"MD5 mismatch: local {md5.hexdigest()} vs expected {expected_md5}")
                return "conflict"
        except OSError as e:
            db.set_file_download_status(file_id, "conflict", error_message=f"Read error: {e}")
            return "conflict"

    db.set_file_download_status(file_id, "downloaded", downloaded_bytes=local_size)
    return "matched"


def _start_archive_scan(archive_id):
    """Initialize tracking for a new archive scan group."""
    ctx = _scan_current_archive
    archive = db.get_archive(archive_id)
    if not archive:
        return False

    archive_name = archive["title"] or archive["identifier"]
    group_id = archive.get("group_id")

    # Create activity job
    act_job_id = activity.start_job("scan", archive_id=archive_id, group_id=group_id)

    # Log the scan queuing event so it shows up in the Activity Log
    activity.log(act_job_id, "info",
                 f"Scanning \"{archive_name}\"",
                 archive_id=archive_id)
    activity.flush()

    # Flash notification for scan start
    notif_id = db.create_notification(
        f'Scanning "{archive_name}"...',
        type="info", job_id=act_job_id,
    )
    broadcast_sse("notification_created", db.get_notification(notif_id))
    activity.update_job_notification(act_job_id, notif_id)

    # Clean slate for this archive — remove old scan-origin files and local overlay, reset conflicts
    with db._db() as conn:
        conn.execute(
            "DELETE FROM archive_files WHERE archive_id = ? AND origin = 'scan'",
            (archive_id,),
        )
        conn.execute(
            "DELETE FROM local_files WHERE archive_id = ? AND origin = 'local'",
            (archive_id,),
        )
        conn.execute(
            "UPDATE archive_files SET download_status = 'pending', error_message = '' "
            "WHERE archive_id = ? AND download_status IN ('conflict', 'unknown')",
            (archive_id,),
        )
        conn.commit()

    # Count total entries for this archive in the queue
    total = db.count_pending_scan_entries(archive_id) + 1  # +1 for the one already claimed

    scan_opts = _scan_options.get(archive_id, {})
    ctx["id"] = archive_id
    ctx["act_job_id"] = act_job_id
    ctx["notif_id"] = notif_id
    ctx["summary"] = {"matched": 0, "conflict": 0, "unknown": 0, "missing": 0, "partial": 0, "auto_matched": 0}
    ctx["total"] = total
    ctx["processed"] = 0
    ctx["last_progress"] = 0.0
    ctx["last_notif"] = 0.0
    ctx["match_by_name"] = scan_opts.get("match_by_name", False)

    activity.log(act_job_id, "info",
                 f"Scan started: {total} manifest files to verify",
                 archive_id=archive_id)
    activity.flush()
    broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "verify", "current": 0, "total": total})
    return True


def _run_archive_scan_entry(entry):
    """Process one scan queue entry as part of an archive group."""
    ctx = _scan_current_archive
    archive_id = entry["archive_id"]
    file_id = entry["file_id"]

    # Check cancellation
    cancel_evt = _scan_cancel.get(archive_id)
    if cancel_evt and cancel_evt.is_set():
        db.complete_scan_queue_entry(entry["id"], error_message="Cancelled")
        return

    # Start new archive group if needed
    if ctx["id"] != archive_id:
        # Finish previous archive if any
        if ctx["id"] is not None:
            _finish_archive_scan(ctx["id"])
        if not _start_archive_scan(archive_id):
            db.complete_scan_queue_entry(entry["id"], error_message="Archive not found")
            return

    # Use cached archive/base_dir for the current group to avoid per-file DB lookups
    if "archive" not in ctx or ctx["archive"]["id"] != archive_id:
        archive = db.get_archive(archive_id)
        if not archive:
            db.complete_scan_queue_entry(entry["id"], error_message="Archive not found")
            return
        download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
        ctx["archive"] = archive
        ctx["base_dir"] = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    base_dir = ctx["base_dir"]

    f = db.get_file(file_id)
    if not f:
        db.complete_scan_queue_entry(entry["id"], error_message="File not found")
        ctx["processed"] += 1
        _update_scan_progress(archive_id)
        if db.is_archive_scan_complete(archive_id):
            _finish_archive_scan(archive_id)
        return

    result = _scan_single_file_on_disk(f, base_dir, entry_id=entry["id"],
                                       identifier=ctx["archive"]["identifier"])

    # Update summary
    if result in ctx["summary"]:
        ctx["summary"][result] += 1
    elif result == "matched":
        ctx["summary"]["matched"] += 1

    ctx["processed"] += 1
    db.complete_scan_queue_entry(entry["id"])

    _update_scan_progress(archive_id)

    # Check if this archive group is done
    if db.is_archive_scan_complete(archive_id):
        _finish_archive_scan(archive_id)


def _update_scan_progress(archive_id):
    """Send throttled progress updates for the current archive scan."""
    ctx = _scan_current_archive
    if ctx["id"] != archive_id:
        return
    # Cache the update rate for the duration of the scan group
    if "update_rate" not in ctx:
        ctx["update_rate"] = int(db.get_setting("sse_update_rate", "500")) / 1000.0
    update_rate = ctx["update_rate"]
    now = time.monotonic()
    done = ctx["processed"] == ctx["total"]

    if now - ctx["last_progress"] >= update_rate or done:
        broadcast_sse("scan_progress", {
            "archive_id": archive_id, "phase": "verify",
            "current": ctx["processed"], "total": ctx["total"],
        })
        ctx["last_progress"] = now

    # Progress is reported via SSE scan_progress events only (no notification updates)


def _detect_media_units(archive_id):
    """Auto-detect multi-file media units for an archive after scan completes.

    Analyses file names by directory and applies heuristics:
    - Folder that exclusively contains a CUE+BIN set → media unit
    - Folder that exclusively contains a GDI + track set → media unit
    - Folder with one playable file + metadata only → media unit
    - Directories with many unrelated files (flat collections) → left alone

    Only sets media_root on files that don't already have one (preserves manual
    overrides) and only on unprocessed files (processed files are always standalone).

    Key constraint: heuristics only apply to small directories that look like a
    single piece of media (max ~50 files).  Large directories containing many
    standalone games are never treated as a media unit.
    """
    with db._db() as conn:
        # Reset all media_root values so re-scan re-evaluates from scratch.
        # Manual overrides are lost, but the user can re-apply them.
        conn.execute(
            "UPDATE archive_files SET media_root = '' WHERE archive_id = ? AND media_root != ''",
            (archive_id,),
        )
        conn.commit()

        rows = conn.execute(
            """SELECT id, name, process_queue_status, media_root
               FROM archive_files
               WHERE archive_id = ? AND origin = 'manifest'""",
            (archive_id,),
        ).fetchall()

    if not rows:
        return

    # Group files by their parent directory
    from collections import defaultdict
    by_dir = defaultdict(list)  # dir_path → [(file_id, basename, process_queue_status, media_root)]
    for r in rows:
        name = r["name"]
        parent = os.path.dirname(name)
        by_dir[parent].append((r["id"], os.path.basename(name), r["process_queue_status"], r["media_root"]))

    METADATA_EXTS = {".txt", ".nfo", ".jpg", ".jpeg", ".png", ".pdf", ".xml",
                     ".htm", ".html", ".bmp", ".gif", ".svg"}
    # Max files in a directory to consider it a media unit candidate.
    # Real media units (CUE+BIN, multi-disc) rarely exceed this.
    # Flat collections of games will have hundreds/thousands and be skipped.
    MAX_MEDIA_UNIT_FILES = 50
    updates = []  # (file_id, media_root)

    for dir_path, files in by_dir.items():
        if not dir_path:
            # Root-level files — check for CUE+BIN pairs at the root
            _detect_cue_bin_pairs(files, "", updates)
            continue

        # Skip files already assigned or processed
        eligible = [(fid, bn, ps, mr) for fid, bn, ps, mr in files
                    if not mr and ps != "processed"]
        if not eligible:
            continue

        # Large directories are flat collections, not media units
        if len(files) > MAX_MEDIA_UNIT_FILES:
            # Still check for CUE+BIN pairs within the directory
            _detect_cue_bin_pairs(files, dir_path, updates)
            continue

        exts = {os.path.splitext(bn)[1].lower() for _, bn, _, _ in files}

        # Heuristic 1: CUE+BIN set — the directory must be predominantly
        # CUE/BIN files, not a mix with many other types
        cue_bin_exts = {".cue", ".bin"}
        has_cue = ".cue" in exts
        has_bin = ".bin" in exts
        if has_cue and has_bin:
            cue_bin_count = sum(1 for _, bn, _, _ in files
                               if os.path.splitext(bn)[1].lower() in cue_bin_exts)
            other_count = len(files) - cue_bin_count
            # Only treat as media unit if CUE/BIN files are the majority
            # (allow a few metadata/artwork files alongside)
            meta_count = sum(1 for _, bn, _, _ in files
                            if os.path.splitext(bn)[1].lower() in METADATA_EXTS)
            if other_count <= meta_count + 1:  # at most 1 non-CUE/BIN/metadata file
                for fid, bn, ps, mr in eligible:
                    updates.append((fid, dir_path))
                continue

        # Heuristic 2: GDI + track files — same logic
        has_gdi = ".gdi" in exts
        has_raw = any(e in (".raw", ".bin") for e in exts)
        if has_gdi and has_raw:
            gdi_track_exts = {".gdi", ".raw", ".bin"}
            gdi_count = sum(1 for _, bn, _, _ in files
                           if os.path.splitext(bn)[1].lower() in gdi_track_exts)
            other_count = len(files) - gdi_count
            meta_count = sum(1 for _, bn, _, _ in files
                            if os.path.splitext(bn)[1].lower() in METADATA_EXTS)
            if other_count <= meta_count + 1:
                for fid, bn, ps, mr in eligible:
                    updates.append((fid, dir_path))
                continue

        # Heuristic 3: one playable file + metadata only
        non_meta = [(fid, bn) for fid, bn, ps, mr in eligible
                    if os.path.splitext(bn)[1].lower() not in METADATA_EXTS]
        meta_only = [(fid, bn) for fid, bn, ps, mr in eligible
                     if os.path.splitext(bn)[1].lower() in METADATA_EXTS]
        if len(non_meta) == 1 and meta_only:
            for fid, bn, ps, mr in eligible:
                updates.append((fid, dir_path))
            continue

    if updates:
        db.set_media_root_bulk(archive_id, updates)
        log.info("scan", "Auto-detected %d media unit file(s) in %s", len(updates), archive_id)


def _detect_cue_bin_pairs(files, dir_path, updates):
    """Detect CUE+BIN pairs among root-level files and group them."""
    import re
    from collections import defaultdict
    cue_files = {}  # stem_lower → (fid, basename)
    bin_files = defaultdict(list)  # stem_lower → [(fid, basename)]

    for fid, bn, ps, mr in files:
        if mr or ps == "processed":
            continue
        ext = os.path.splitext(bn)[1].lower()
        stem = os.path.splitext(bn)[0].lower()
        if ext == ".cue":
            cue_files[stem] = (fid, bn)
        elif ext == ".bin":
            # BIN files often have " (Track XX)" suffix — strip it to match CUE
            clean = stem
            track_match = re.match(r"^(.+?)\s*\(track\s*\d+\)$", stem, re.IGNORECASE)
            if track_match:
                clean = track_match.group(1).strip().lower()
            bin_files[clean].append((fid, bn))

    # For each CUE with matching BINs, group them under a synthetic media_root
    for stem, (cue_fid, cue_bn) in cue_files.items():
        matching_bins = bin_files.get(stem, [])
        if matching_bins:
            # Use the CUE name (without extension) as the media_root
            media_root = os.path.join(dir_path, os.path.splitext(cue_bn)[0]) if dir_path else os.path.splitext(cue_bn)[0]
            updates.append((cue_fid, media_root))
            for bin_fid, _ in matching_bins:
                updates.append((bin_fid, media_root))


def _finish_archive_scan(archive_id):
    """Complete an archive scan group: unknown file discovery, summary, cleanup."""
    ctx = _scan_current_archive
    if ctx["id"] != archive_id:
        return

    act_job_id = ctx["act_job_id"]
    notif_id = ctx["notif_id"]
    summary = ctx["summary"]

    archive = db.get_archive(archive_id)
    if not archive:
        ctx["id"] = None
        return

    archive_name = archive["title"] or archive["identifier"]
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    # Check cancellation
    cancel_evt = _scan_cancel.get(archive_id)
    if cancel_evt and cancel_evt.is_set():
        activity.log(act_job_id, "warning", "Scan cancelled by user", archive_id=archive_id)
        activity.flush()
        activity.finish_job(act_job_id, "cancelled",
                            summary=f"Cancelled at {ctx['processed']}/{ctx['total']}")
        db.update_notification(notif_id, message=f'Scan cancelled at {ctx["processed"]}/{ctx["total"]}', type="warning")
        broadcast_sse("notification_updated", db.get_notification(notif_id))
        broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "cancelled",
                                        "current": ctx["processed"], "total": ctx["total"]})
        with _scan_lock:
            _scan_cancel.pop(archive_id, None)
        _scan_options.pop(archive_id, None)
        ctx["id"] = None
        db.clear_completed_scan_entries(archive_id)
        return

    # Scan for unknown files on disk
    match_by_name = ctx.get("match_by_name", False)
    if os.path.isdir(base_dir):
        broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "disk", "current": 0, "total": 0})

        # Build manifest and processed name sets
        with db._db() as conn:
            manifest_rows = conn.execute(
                "SELECT id, name FROM archive_files WHERE archive_id = ? AND origin = 'manifest'",
                (archive_id,),
            ).fetchall()
            manifest_names = {r["name"] for r in manifest_rows}

            # Also include contained file names (archive_content) for recognition
            contained_rows = conn.execute(
                "SELECT id, name FROM archive_files WHERE archive_id = ? AND origin = 'archive_content'",
                (archive_id,),
            ).fetchall()
            contained_names = {r["name"]: r["id"] for r in contained_rows}

            # Build stem→file_id lookup for match-by-name (stem = name without extension)
            # Includes subdirectory path: "subdir/Game (USA)" maps to file_id
            stem_to_manifest = {}
            if match_by_name:
                for r in manifest_rows:
                    stem = os.path.splitext(r["name"])[0]
                    # Only keep first match per stem (closest manifest entry)
                    if stem not in stem_to_manifest:
                        stem_to_manifest[stem] = r["id"]
                # Also include contained files in stem matching
                for r in contained_rows:
                    stem = os.path.splitext(r["name"])[0]
                    if stem not in stem_to_manifest:
                        stem_to_manifest[stem] = r["id"]

        processed_names = db.get_all_processed_files(archive_id)

        unknown_files = []
        auto_matched = []  # (rel_name, local_size, manifest_file_id)
        for root, _dirs, files in os.walk(base_dir):
            if cancel_evt and cancel_evt.is_set():
                break
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, base_dir)
                if rel in manifest_names or rel in processed_names:
                    continue
                # Recognize contained files found on disk (post-extraction)
                if rel in contained_names:
                    # Mark the contained file as extracted
                    cfid = contained_names[rel]
                    with db._db() as _conn:
                        _conn.execute(
                            "UPDATE archive_files SET download_status = 'extracted' WHERE id = ? AND download_status IN ('contained', 'contained_queued')",
                            (cfid,),
                        )
                        _conn.commit()
                    continue

                # Try stem-matching: does this file's stem match a manifest entry?
                if match_by_name:
                    rel_stem = os.path.splitext(rel)[0]
                    manifest_fid = stem_to_manifest.get(rel_stem)
                    if manifest_fid is not None:
                        local_size = 0
                        try:
                            local_size = os.path.getsize(full)
                        except OSError:
                            pass
                        auto_matched.append((rel, local_size, manifest_fid))
                        continue

                unknown_files.append(rel)
                summary["unknown"] += 1

        # Insert auto-matched files into the overlay as processed outputs
        if auto_matched:
            import time as _time
            with db._db() as conn:
                for rel_name, local_size, source_fid in auto_matched:
                    clean_name = rel_name
                    conn.execute(
                        """INSERT OR IGNORE INTO local_files
                           (archive_id, source_file_id, name, size, origin, processor_type, created_at)
                           VALUES (?, ?, ?, ?, 'processed', '', ?)""",
                        (archive_id, source_fid, clean_name, local_size, _time.time()),
                    )
                    # Also ensure the source manifest file is marked downloaded
                    conn.execute(
                        "UPDATE archive_files SET download_status = 'downloaded' "
                        "WHERE id = ? AND download_status NOT IN ('downloaded', 'downloading')",
                        (source_fid,),
                    )
                conn.commit()
            summary["auto_matched"] = len(auto_matched)
            log.info("scan", "%d files auto-matched by filename stem", len(auto_matched))

        if unknown_files:
            with db._db() as conn:
                for rel_name in unknown_files:
                    local_path = os.path.join(base_dir, rel_name)
                    local_size = 0
                    try:
                        local_size = os.path.getsize(local_path)
                    except OSError:
                        pass
                    conn.execute(
                        """INSERT OR IGNORE INTO archive_files
                           (archive_id, name, size, md5, sha1, format, source, mtime,
                            download_status, downloaded_bytes, error_message, origin)
                           VALUES (?, ?, ?, '', '', '', '', '', 'unknown', ?, 'File found on disk but not in archive manifest', 'scan')""",
                        (archive_id, rel_name, local_size, local_size),
                    )
                conn.commit()
            log.debug("scan", "%d unknown files found on disk", len(unknown_files))

    # Auto-detect media units from directory structure
    _detect_media_units(archive_id)

    db.recompute_archive_file_count(archive_id)
    db.recompute_archive_status(archive_id)
    updated = db.get_archive(archive_id)
    log.info("scan", "Scan complete for %s: %s", archive["identifier"], summary)

    # Update notification with final summary
    parts = []
    if summary["matched"] > 0:
        parts.append(f'{summary["matched"]} matched')
    if summary["partial"] > 0:
        parts.append(f'{summary["partial"]} partial')
    if summary["conflict"] > 0:
        parts.append(f'{summary["conflict"]} conflict')
    if summary.get("auto_matched", 0) > 0:
        parts.append(f'{summary["auto_matched"]} auto-matched')
    if summary["unknown"] > 0:
        parts.append(f'{summary["unknown"]} unknown')
    if summary["missing"] > 0:
        parts.append(f'{summary["missing"]} not on disk')
    result_msg = ", ".join(parts) if parts else "no files found on disk"
    ntype = "success" if summary["conflict"] == 0 and summary["missing"] == 0 else "warning"
    db.update_notification(notif_id, message=f'Scan "{archive_name}": {result_msg}', type=ntype)
    broadcast_sse("notification_updated", db.get_notification(notif_id))

    if summary["conflict"] > 0:
        activity.log(act_job_id, "warning", f'{summary["conflict"]} file(s) have conflicts',
                     archive_id=archive_id)
    if summary["missing"] > 0:
        activity.log(act_job_id, "warning", f'{summary["missing"]} file(s) not found on disk',
                     archive_id=archive_id)
    if summary["partial"] > 0:
        activity.log(act_job_id, "info", f'{summary["partial"]} partial download(s) re-queued',
                     archive_id=archive_id)
    if summary.get("auto_matched", 0) > 0:
        activity.log(act_job_id, "info", f'{summary["auto_matched"]} file(s) auto-matched by filename',
                     archive_id=archive_id)
    if summary["unknown"] > 0:
        activity.log(act_job_id, "info", f'{summary["unknown"]} unknown file(s) found on disk',
                     archive_id=archive_id)

    activity.log(act_job_id, "success" if ntype == "success" else "warning",
                 f"Scan complete: {result_msg}", archive_id=archive_id)
    activity.flush()
    activity.finish_job(act_job_id, "completed", summary=result_msg)

    broadcast_sse("scan_progress", {
        "archive_id": archive_id, "phase": "done",
        "current": ctx["total"], "total": ctx["total"],
        "summary": summary,
    })
    broadcast_sse("archive_updated", updated)

    # Auto-tag after scan completes
    try:
        from auto_tagger import auto_tag_archive
        auto_tag_archive(archive_id)
        log.info("Auto-tagged archive %d after scan", archive_id)
    except Exception as e:
        log.warning("Auto-tag failed for archive %d: %s", archive_id, e)

    # Clean up
    with _scan_lock:
        _scan_cancel.pop(archive_id, None)
    _scan_options.pop(archive_id, None)
    db.clear_completed_scan_entries(archive_id)
    ctx["id"] = None


@app.route("/api/archives/<int:archive_id>/scan", methods=["POST"])
@login_required
def scan_existing_files(archive_id):
    """Queue a scan of the local download folder for files matching this archive's manifest."""
    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    match_by_name = bool(data.get("match_by_name", False))

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
    if not os.path.isdir(base_dir):
        return jsonify({"error": f"Download folder not found: {base_dir}"}), 404

    with _scan_lock:
        if archive_id in _scan_cancel:
            return jsonify({"error": "Scan already queued for this archive"}), 409
        evt = threading.Event()
        _scan_cancel[archive_id] = evt

    # Store scan options keyed by archive_id so the worker can read them
    _scan_options[archive_id] = {"match_by_name": match_by_name}

    try:
        # Get all manifest files for this archive
        with db._db() as conn:
            file_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM archive_files WHERE archive_id = ? AND origin = 'manifest'",
                (archive_id,),
            ).fetchall()]

        if not file_ids:
            with _scan_lock:
                _scan_cancel.pop(archive_id, None)
            _scan_options.pop(archive_id, None)
            return jsonify({"error": "No manifest files to scan"}), 400

        # Add entries to scan_queue
        db.add_scan_queue_entries_batch(archive_id, file_ids)

        archive_name = archive["title"] or archive["identifier"]

        # Flash notification for scan queued
        notif_id = db.create_notification(
            f'Scanning "{archive_name}" ({len(file_ids)} files)',
            type="info",
        )
        broadcast_sse("notification_created", db.get_notification(notif_id))
        broadcast_sse("queue_changed", {"queue_type": "scan", "count": len(file_ids), "archive_id": archive_id})
        wake_scan_worker()
        return jsonify({"ok": True, "queued": len(file_ids)})
    except Exception as e:
        # Clean up _scan_cancel so this archive isn't permanently blocked
        with _scan_lock:
            _scan_cancel.pop(archive_id, None)
        import traceback
        tb = traceback.format_exc()
        log.error("scan", "Scan endpoint failed for archive %d: %s\n%s", archive_id, e, tb)
        return jsonify({"error": f"Scan failed: {e}"}), 500


@app.route("/api/archives/<int:archive_id>/scan/cancel", methods=["POST"])
@login_required
def cancel_scan(archive_id):
    """Cancel a running or queued scan."""
    with _scan_lock:
        evt = _scan_cancel.get(archive_id)
        if evt:
            evt.set()
            return jsonify({"ok": True})
    return jsonify({"error": "No active scan for this archive"}), 404


@app.route("/api/files/<int:file_id>/force-resume", methods=["POST"])
@login_required
def force_resume_file(file_id):
    """Force a conflict file back to pending state so the downloader will resume it."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT id, archive_id, name, size, download_status FROM archive_files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "File not found"}), 404
    if row["download_status"] != "conflict":
        return jsonify({"error": "File is not in conflict state"}), 400

    # Get actual file size on disk
    archive = db.get_archive(row["archive_id"])
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    local_path = os.path.join(download_dir, archive["identifier"], row["name"])
    local_size = 0
    if os.path.isfile(local_path):
        local_size = os.path.getsize(local_path)

    conn.execute(
        "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = ?, "
        "error_message = '' WHERE id = ?",
        (local_size, file_id),
    )
    conn.commit()
    conn.close()

    db.recompute_archive_status(row["archive_id"])
    return jsonify({"ok": True, "downloaded_bytes": local_size, "size": row["size"]})


@app.route("/api/archives/<int:archive_id>/clear-changes", methods=["POST"])
@login_required
def clear_changes(archive_id):
    db.clear_change_statuses(archive_id)
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/download", methods=["POST"])
@login_required
def toggle_download(archive_id):
    data = request.json
    enabled = data.get("enabled", False)
    db.set_archive_download_enabled(archive_id, enabled)
    if enabled:
        db.recompute_archive_status(archive_id, fallback="queued")
    else:
        db.set_archive_status(archive_id, "idle")
    archive = db.get_archive(archive_id)
    broadcast_sse("archive_updated", archive)
    return jsonify(archive)


# --- Archive Files API ---

@app.route("/api/archives/<int:archive_id>/progress", methods=["GET"])
@login_required
def archive_progress(archive_id):
    return jsonify(db.get_archive_progress(archive_id))


@app.route("/api/archives/<int:archive_id>/files", methods=["GET"])
@login_required
def list_archive_files(archive_id):
    sort = request.args.get("sort", "name")
    sort_dir = request.args.get("sort_dir", "")
    search = request.args.get("search", "").strip()
    files, total = db.get_archive_files(archive_id, sort=sort, sort_dir=sort_dir, search=search)
    unqueued = db.count_unqueued_files(archive_id)
    progress = db.get_archive_progress(archive_id)
    return jsonify({
        "files": files,
        "total": total,
        "all_queued": unqueued == 0,
        "progress": progress,
    })


@app.route("/api/files/<int:file_id>/queue", methods=["POST"])
@login_required
def toggle_file_queue(file_id):
    data = request.json
    queued = data.get("queued", True)
    db.set_file_queued(file_id, queued)
    # If dequeuing, cancel the download if this file is currently downloading
    if not queued:
        download_manager.skip_current_file(file_id)
    return jsonify({"ok": True})


# Keep old endpoint as alias for backwards compatibility
@app.route("/api/files/<int:file_id>/select", methods=["POST"])
@login_required
def toggle_file_select_compat(file_id):
    data = request.json
    queued = data.get("queued", data.get("selected", True))
    db.set_file_queued(file_id, queued)
    if not queued:
        download_manager.skip_current_file(file_id)
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/files/queue-all", methods=["POST"])
@login_required
def queue_all_files(archive_id):
    data = request.json
    queued = data.get("queued", True)
    if queued:
        added, skipped = db.set_all_files_queued(archive_id, True)
        if added > 0:
            db.compact_download_queue()
            broadcast_sse("queue_changed", {"queue_type": "download", "count": added})
        return jsonify({"ok": True, "added": added, "skipped": skipped})
    else:
        db.set_all_files_queued(archive_id, False)
        return jsonify({"ok": True})


# Keep old endpoint as alias for backwards compatibility
@app.route("/api/archives/<int:archive_id>/files/select-all", methods=["POST"])
@login_required
def select_all_files_compat(archive_id):
    data = request.json
    queued = data.get("queued", data.get("selected", True))
    if queued:
        added, skipped = db.set_all_files_queued(archive_id, True)
        if added > 0:
            db.compact_download_queue()
            broadcast_sse("queue_changed", {"queue_type": "download", "count": added})
        return jsonify({"ok": True, "added": added, "skipped": skipped})
    else:
        db.set_all_files_queued(archive_id, False)
        return jsonify({"ok": True})



@app.route("/api/files/<int:file_id>/rename", methods=["POST"])
@login_required
def rename_file(file_id):
    data = request.json
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Name is required"}), 400

    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    old_name = f["name"]
    if new_name == old_name:
        return jsonify({"ok": True})

    # Cancel active download if this file is being downloaded
    download_manager.skip_current_file(file_id)

    # Rename on disk if the file exists
    archive = db.get_archive(f["archive_id"])
    if archive:
        download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
        base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
        old_path = os.path.join(base_dir, old_name)
        new_path = os.path.join(base_dir, new_name)
        # Safety: ensure paths stay within archive dir
        if os.path.realpath(new_path).startswith(base_dir + os.sep) and os.path.isfile(old_path):
            os.rename(old_path, new_path)

    db.rename_file(file_id, new_name)
    return jsonify({"ok": True})


@app.route("/api/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    data = request.json or {}
    remove_from_db = data.get("remove_from_db", False)

    # Cancel active download if this file is being downloaded
    download_manager.skip_current_file(file_id)

    # Delete from disk if it exists
    deleted_from_disk = False
    archive = db.get_archive(f["archive_id"])
    if archive:
        download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
        base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
        local_path = os.path.realpath(os.path.join(base_dir, f["name"]))
        if local_path.startswith(base_dir + os.sep) and os.path.isfile(local_path):
            os.remove(local_path)
            deleted_from_disk = True

    if remove_from_db:
        # Unknown/scan-origin files: remove entirely
        db.delete_files([file_id])
        db.recompute_archive_file_count(f["archive_id"])
    else:
        # Manifest files: keep in DB but reset download status
        db.set_file_download_status(file_id, "pending", downloaded_bytes=0, error_message="")
        # Check overlay for processed outputs
        has_outputs = bool(db.get_processed_outputs(file_id))
        if not has_outputs:
            # No overlay outputs — clear stale processing state
            conn = db.get_db()
            conn.execute(
                "UPDATE archive_files SET processing_error = '', process_queue_status = '' WHERE id = ?",
                (file_id,),
            )
            conn.commit()
            conn.close()
    db.recompute_archive_status(f["archive_id"])
    return jsonify({"ok": True, "deleted_from_disk": deleted_from_disk})


@app.route("/api/files/<int:file_id>/processed-tree", methods=["GET"])
@login_required
def get_processed_tree(file_id):
    """Return the on-disk tree of processed output files for a source file."""
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    archive = db.get_archive(f["archive_id"])
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    ident = archive["identifier"]
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, ident))

    # Build the set of root paths to scan from the overlay table
    overlay_outputs = db.get_processed_outputs(file_id)
    root_paths = [lf["name"].rstrip("/").rstrip(os.sep) for lf in overlay_outputs]

    # Build tree from disk (verify each entry actually exists)
    # Check processed_dir first, fall back to download_dir (legacy location)
    def _scan_path(rel_path):
        abs_path = os.path.realpath(_resolve_processed_file(ident, rel_path, base_dir))
        # Security check: must stay within either base_dir or processed base
        proc_base = _get_processed_base(ident)
        if not (abs_path.startswith(base_dir + os.sep) or abs_path.startswith(proc_base + os.sep)):
            return None
        if os.path.isfile(abs_path):
            stat = os.stat(abs_path)
            return {"name": os.path.basename(rel_path), "path": rel_path, "type": "file",
                    "size": stat.st_size, "mtime": int(stat.st_mtime)}
        elif os.path.isdir(abs_path):
            children = []
            try:
                for entry in sorted(os.listdir(abs_path)):
                    child = _scan_path(os.path.join(rel_path, entry))
                    if child:
                        children.append(child)
            except OSError:
                pass
            stat = os.stat(abs_path)
            return {"name": os.path.basename(rel_path), "path": rel_path, "type": "dir",
                    "children": children, "mtime": int(stat.st_mtime)}
        return None

    tree = []
    seen = set()
    for p in root_paths:
        if p in seen:
            continue
        seen.add(p)
        node = _scan_path(p)
        if node:
            tree.append(node)

    return jsonify({"tree": tree})


# --- Archive Contents (contained files) ---

@app.route("/api/files/<int:file_id>/contents", methods=["GET"])
@login_required
def get_file_contents(file_id):
    """Get the contained files inside an archive file."""
    contents = db.get_archive_content_files(file_id)
    return jsonify({"contents": contents, "total": len(contents)})


@app.route("/api/files/<int:file_id>/fetch-contents", methods=["POST"])
@login_required
def fetch_file_contents(file_id):
    """Manually trigger fetching/inspecting contents of an archive file."""
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    method = "local" if f["download_status"] in ("downloaded", "extracted") else "remote"
    db.queue_content_fetch(file_id, f["archive_id"], method=method)
    wake_content_fetch_worker()
    return jsonify({"ok": True, "method": method})


@app.route("/api/archives/<int:archive_id>/contained-files", methods=["GET"])
@login_required
def get_contained_files(archive_id):
    """Get all archive_content files for an archive."""
    files = db.get_all_contained_files(archive_id)
    return jsonify({"files": files, "total": len(files)})


@app.route("/api/files/<int:target_file_id>/assign-output", methods=["POST"])
@login_required
def assign_output(target_file_id):
    """Assign an unknown file as processed output of a target file."""
    data = request.json
    unknown_file_id = data.get("unknown_file_id")
    if not unknown_file_id:
        return jsonify({"error": "Missing unknown_file_id"}), 400
    ok, err = db.assign_as_processed_output(target_file_id, unknown_file_id)
    if not ok:
        return jsonify({"error": err}), 400
    # Recompute archive status
    target = db.get_file(target_file_id)
    if target:
        db.recompute_archive_status(target["archive_id"])
        broadcast_sse("archive_updated", db.get_archive(target["archive_id"]))
    return jsonify({"ok": True})


@app.route("/api/files/<int:file_id>/delete-processed", methods=["POST"])
@login_required
def delete_processed_file(file_id):
    """Delete one or all processed output files from disk."""
    import shutil
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    data = request.json or {}
    filename = data.get("filename", "")
    delete_all = data.get("delete_all", False)

    archive = db.get_archive(f["archive_id"])
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    ident = archive["identifier"]
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, ident))
    proc_base = _get_processed_base(ident)

    def _safe_delete(rel):
        """Resolve and delete a processed file from whichever dir it lives in."""
        abs_p = os.path.realpath(_resolve_processed_file(ident, rel, base_dir))
        if not (abs_p.startswith(base_dir + os.sep) or abs_p.startswith(proc_base + os.sep)):
            return
        if os.path.isfile(abs_p):
            os.remove(abs_p)
        elif os.path.isdir(abs_p):
            shutil.rmtree(abs_p)

    if delete_all:
        # Delete every processed output for this file
        overlay_outputs = db.get_processed_outputs(file_id)
        for lf in overlay_outputs:
            _safe_delete(lf["name"].rstrip("/").rstrip(os.sep))
        # Clear overlay rows and processing state
        db.delete_local_files_for_source(file_id)
        db.set_file_process_queue_status(file_id, "")
    elif filename:
        # Delete a single processed output
        _safe_delete(filename.rstrip("/").rstrip(os.sep))
        # Remove the overlay row for this specific output
        db.delete_local_file_by_name(file_id, filename.rstrip("/").rstrip(os.sep))

    return jsonify({"ok": True})


@app.route("/api/files/<int:file_id>/rename-processed", methods=["POST"])
@login_required
def rename_processed_file(file_id):
    """Rename a processed output file on disk and update the DB record."""
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    data = request.json or {}
    old_path = data.get("old_path", "").strip()
    new_name = data.get("new_name", "").strip()
    if not old_path or not new_name:
        return jsonify({"error": "old_path and new_name are required"}), 400

    archive = db.get_archive(f["archive_id"])
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    ident = archive["identifier"]
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, ident))
    proc_base = _get_processed_base(ident)

    old_abs = os.path.realpath(_resolve_processed_file(ident, old_path, base_dir))
    # Construct new path in the same directory where the old file was found
    new_rel = os.path.join(os.path.dirname(old_path), new_name)
    containing_dir = os.path.dirname(old_abs)
    new_abs = os.path.realpath(os.path.join(containing_dir, new_name))

    if not (old_abs.startswith(base_dir + os.sep) or old_abs.startswith(proc_base + os.sep)):
        return jsonify({"error": "Invalid path"}), 400
    if not (new_abs.startswith(base_dir + os.sep) or new_abs.startswith(proc_base + os.sep)):
        return jsonify({"error": "Invalid path"}), 400

    if (os.path.isfile(old_abs) or os.path.isdir(old_abs)):
        os.rename(old_abs, new_abs)

    # Update the overlay row name to match
    old_clean = old_path.rstrip("/").rstrip(os.sep)
    new_clean = new_rel.rstrip("/").rstrip(os.sep)
    with db._db() as oconn:
        oconn.execute(
            "UPDATE local_files SET name = ? WHERE source_file_id = ? AND name = ?",
            (new_clean, file_id, old_clean),
        )
        oconn.commit()

    return jsonify({"ok": True, "new_path": new_rel})


@app.route("/api/archives/<int:archive_id>/files/batch-delete", methods=["POST"])
@login_required
def batch_delete_files(archive_id):
    data = request.json
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "No files specified"}), 400

    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    # Delete files from disk
    for fid in file_ids:
        f = db.get_file(fid)
        if f and f["archive_id"] == archive_id:
            local_path = os.path.realpath(os.path.join(base_dir, f["name"]))
            if local_path.startswith(base_dir + os.sep) and os.path.isfile(local_path):
                os.remove(local_path)

    count = db.delete_files(file_ids)
    db.recompute_archive_file_count(archive_id)
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/archives/<int:archive_id>/files/batch-retry", methods=["POST"])
@login_required
def batch_retry_files(archive_id):
    data = request.json
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "No files specified"}), 400
    count = db.reset_failed_files_by_ids(file_ids)
    if count > 0:
        db.recompute_archive_status(archive_id, fallback="queued")
    return jsonify({"ok": True, "reset_count": count})


@app.route("/api/files/<int:file_id>/scan", methods=["POST"])
@login_required
def scan_single_file(file_id):
    """Queue a single file for priority rescan (position 0)."""
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    archive = db.get_archive(f["archive_id"])
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    db.add_scan_queue_entry(file_id, f["archive_id"], priority=True)
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "added", "file_id": file_id})
    wake_scan_worker()
    return jsonify({"ok": True, "queued": True, "name": f["name"]})


@app.route("/api/archives/<int:archive_id>/retry", methods=["POST"])
@login_required
def retry_failed_files(archive_id):
    count = db.reset_failed_files(archive_id)
    if count > 0:
        db.set_archive_status(archive_id, "queued")
    archive = db.get_archive(archive_id)
    broadcast_sse("archive_updated", archive)
    return jsonify({"ok": True, "reset_count": count})


@app.route("/api/files/<int:file_id>/retry", methods=["POST"])
@login_required
def retry_single_file(file_id):
    db.reset_failed_file(file_id)
    return jsonify({"ok": True})


# --- Download Control API ---

@app.route("/api/download/start", methods=["POST"])
@login_required
def start_download():
    has_work = db.get_next_download_file() is not None
    if not has_work:
        return jsonify({"state": download_manager.state, "has_work": False})
    download_manager.start()
    db.set_download_state("running")
    return jsonify({"state": download_manager.state, "has_work": True})


@app.route("/api/download/pause", methods=["POST"])
@login_required
def pause_download():
    download_manager.pause()
    db.set_download_state("paused")
    return jsonify({"state": download_manager.state})


@app.route("/api/download/stop", methods=["POST"])
@login_required
def stop_download():
    download_manager.stop()
    db.set_download_state("stopped")
    return jsonify({"state": download_manager.state})


@app.route("/api/download/status", methods=["GET"])
@login_required
def download_status():
    return jsonify(download_manager.get_status())


@app.route("/api/download/queue", methods=["GET"])
@login_required
def download_queue():
    limit = request.args.get("limit", 5000, type=int)
    return jsonify(db.get_download_queue(limit))


@app.route("/api/download/queue/clear", methods=["POST"])
@login_required
def clear_download_queue():
    """Remove all pending files from the download queue."""
    count = db.clear_download_queue()
    broadcast_sse("queue_update", {"queue_type": "download", "action": "removed",
                                    "data": {"cleared": count}})
    return jsonify({"ok": True, "cleared": count})


@app.route("/api/processing/queue/clear", methods=["POST"])
@login_required
def clear_processing_queue():
    """Remove all pending entries from the processing queue (does not cancel active work)."""
    count = db.cancel_all_pending_processing()
    broadcast_sse("queue_update", {"queue_type": "processing", "action": "removed",
                                    "data": {"cleared": count}})
    return jsonify({"ok": True, "cleared": count})


@app.route("/api/scan/queue/clear", methods=["POST"])
@login_required
def clear_scan_queue():
    """Remove all pending entries from the scan queue (does not cancel active work)."""
    count = db.cancel_all_pending_scans()
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "removed",
                                    "data": {"cleared": count}})
    return jsonify({"ok": True, "cleared": count})


@app.route("/api/processing/queue/remove", methods=["POST"])
@login_required
def remove_processing_queue_entries():
    """Remove specific pending entries from the processing queue by ID."""
    data = request.json
    entry_ids = data.get("entry_ids", [])
    if not entry_ids:
        return jsonify({"ok": False, "error": "No entry_ids provided"}), 400
    count = db.cancel_processing_entries(entry_ids)
    broadcast_sse("queue_update", {"queue_type": "processing", "action": "removed",
                                    "data": {"entry_ids": entry_ids, "removed": count}})
    return jsonify({"ok": True, "removed": count})


@app.route("/api/scan/queue/remove", methods=["POST"])
@login_required
def remove_scan_queue_entries():
    """Remove specific pending entries from the scan queue by ID."""
    data = request.json
    entry_ids = data.get("entry_ids", [])
    if not entry_ids:
        return jsonify({"ok": False, "error": "No entry_ids provided"}), 400
    count = db.cancel_scan_entries(entry_ids)
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "removed",
                                    "data": {"entry_ids": entry_ids, "removed": count}})
    return jsonify({"ok": True, "removed": count})


@app.route("/api/download/bandwidth", methods=["POST"])
@login_required
def set_bandwidth():
    data = request.json
    limit = int(data.get("limit", -1))  # -1 = unlimited, 0 = paused, >0 = throttle
    download_manager.bandwidth_limit = limit
    return jsonify({"bandwidth_limit": download_manager.bandwidth_limit})


# --- Queue Overhaul API ---

@app.route("/api/queues/counts", methods=["GET"])
@login_required
def queue_counts():
    """Return total queue counts for the topbar badge (seeded on page load)."""
    return jsonify(db.get_queue_counts())


@app.route("/api/download/queue/reorder", methods=["POST"])
@login_required
def reorder_download_queue():
    """Move file(s) to a new position in the download queue.

    Accepts either single-item ``{file_id, position}`` or multi-item
    ``{file_ids: [...], position}`` to move several items at once.
    """
    data = request.json
    file_ids = data.get("file_ids")
    if not file_ids:
        fid = data.get("file_id")
        if fid is not None:
            file_ids = [fid]
    new_position = data.get("position")
    if not file_ids or new_position is None:
        return jsonify({"error": "file_id(s) and position required"}), 400
    for fid in file_ids:
        db.reorder_download_queue(fid, new_position)
        new_position += 1  # stack consecutively after the target
    broadcast_sse("queue_update", {"queue_type": "download", "action": "reordered", "file_ids": file_ids})
    return jsonify({"ok": True})


@app.route("/api/processing/queue", methods=["GET"])
@login_required
def get_processing_queue_endpoint():
    """Return the file-level processing queue."""
    limit = request.args.get("limit", 5000, type=int)
    return jsonify(db.get_processing_queue(limit))


@app.route("/api/processing/queue/reorder", methods=["POST"])
@login_required
def reorder_processing_queue():
    """Move processing queue entry/entries to a new position."""
    data = request.json
    entry_ids = data.get("entry_ids")
    if not entry_ids:
        eid = data.get("entry_id")
        if eid is not None:
            entry_ids = [eid]
    new_position = data.get("position")
    if not entry_ids or new_position is None:
        return jsonify({"error": "entry_id(s) and position required"}), 400
    for eid in entry_ids:
        db.reorder_processing_queue(eid, new_position)
        new_position += 1
    broadcast_sse("queue_update", {"queue_type": "processing", "action": "reordered", "file_ids": entry_ids})
    return jsonify({"ok": True})


@app.route("/api/processing/pause", methods=["POST"])
@login_required
def pause_processing():
    data = request.json or {}
    paused = data.get("paused", True)
    db.set_processing_paused(paused)
    broadcast_sse("queue_update", {"queue_type": "processing", "action": "status_changed",
                                    "data": {"paused": paused}})
    return jsonify({"ok": True, "paused": paused})


@app.route("/api/processing/cancel", methods=["POST"])
@login_required
def cancel_all_processing():
    """Cancel current processing and remove all pending entries."""
    from processing_worker import cancel_current_processing
    cancel_current_processing()
    count = db.cancel_all_pending_processing()
    broadcast_sse("queue_update", {"queue_type": "processing", "action": "removed",
                                    "data": {"cancelled": count}})
    return jsonify({"ok": True, "cancelled": count})


@app.route("/api/scan/queue", methods=["GET"])
@login_required
def get_scan_queue_endpoint():
    """Return the file-level scan queue."""
    limit = request.args.get("limit", 5000, type=int)
    return jsonify(db.get_scan_queue(limit))


@app.route("/api/scan/queue/reorder", methods=["POST"])
@login_required
def reorder_scan_queue():
    data = request.json
    entry_id = data.get("entry_id")
    new_position = data.get("position")
    if entry_id is None or new_position is None:
        return jsonify({"error": "entry_id and position required"}), 400
    db.reorder_scan_queue(entry_id, new_position)
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "reordered", "file_ids": [entry_id]})
    return jsonify({"ok": True})


@app.route("/api/scan/pause", methods=["POST"])
@login_required
def pause_scanning():
    data = request.json or {}
    paused = data.get("paused", True)
    db.set_scan_paused(paused)
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "status_changed",
                                    "data": {"paused": paused}})
    return jsonify({"ok": True, "paused": paused})


@app.route("/api/scan/cancel", methods=["POST"])
@login_required
def cancel_all_scans():
    """Cancel all pending scan queue entries."""
    count = db.cancel_all_pending_scans()
    broadcast_sse("queue_update", {"queue_type": "scan", "action": "removed",
                                    "data": {"cancelled": count}})
    return jsonify({"ok": True, "cancelled": count})


# --- Processing API ---

@app.route("/api/processing/profiles", methods=["GET"])
@login_required
def list_processing_profiles():
    profiles = db.get_processing_profiles()
    import json as _json
    for p in profiles:
        p["options"] = _json.loads(p.get("options_json", "{}"))
        # Parse step options too
        for step in p.get("steps", []):
            step["options"] = _json.loads(step.get("options_json", "{}"))
    return jsonify(profiles)


@app.route("/api/processing/profiles", methods=["POST"])
@login_required
def create_processing_profile():
    data = request.json
    name = data.get("name", "").strip()
    processor_type = data.get("processor_type", "")
    options = data.get("options", {})
    steps = data.get("steps")
    if not name:
        return jsonify({"error": "Name is required"}), 400
    from processors import get_processor_types
    valid_types = get_processor_types()
    if processor_type and processor_type not in valid_types:
        return jsonify({"error": f"Unknown processor type: {processor_type}"}), 400
    # If steps provided, validate each
    if steps:
        for s in steps:
            if s.get("processor_type") not in valid_types:
                return jsonify({"error": f"Unknown processor type in step: {s.get('processor_type')}"}), 400
    profile_id = db.add_processing_profile(name, processor_type or (steps[0]["processor_type"] if steps else ""), options)
    # Set pipeline steps if provided
    if steps:
        db.set_profile_steps(profile_id, steps)
    elif processor_type:
        # Create single step from legacy fields
        db.set_profile_steps(profile_id, [{"processor_type": processor_type, "options": options}])
    return jsonify({"ok": True, "id": profile_id})


@app.route("/api/processing/profiles/<int:profile_id>", methods=["PUT"])
@login_required
def update_processing_profile_endpoint(profile_id):
    data = request.json
    db.update_processing_profile(
        profile_id,
        name=data.get("name"),
        processor_type=data.get("processor_type"),
        options=data.get("options"),
    )
    # Update pipeline steps if provided
    steps = data.get("steps")
    if steps is not None:
        db.set_profile_steps(profile_id, steps)
    return jsonify({"ok": True})


@app.route("/api/processing/profiles/<int:profile_id>", methods=["DELETE"])
@login_required
def delete_processing_profile_endpoint(profile_id):
    db.delete_processing_profile(profile_id)
    return jsonify({"ok": True})


@app.route("/api/processing/types", methods=["GET"])
@login_required
def list_processor_types():
    from processors import get_processor_types
    return jsonify(get_processor_types())


@app.route("/api/processing/tools", methods=["GET"])
@login_required
def detect_processing_tools():
    from processors import detect_tools
    return jsonify(detect_tools())


@app.route("/api/archives/<int:archive_id>/process", methods=["POST"])
@login_required
def process_archive(archive_id):
    data = request.json or {}
    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id is required"}), 400
    file_ids = data.get("file_ids")
    options_override = data.get("options", {})
    auto_process = data.get("auto_process", False)

    # Optionally set auto-processing on this archive
    if auto_process:
        db.set_archive_processing_profile(archive_id, profile_id)

    from processing_worker import queue_archive_processing
    ok, queued = queue_archive_processing(archive_id, profile_id, file_ids, options_override)
    if not ok:
        return jsonify({"error": queued}), 409
    return jsonify({"ok": True, "queued": queued})


@app.route("/api/archives/<int:archive_id>/auto-process", methods=["POST"])
@login_required
def set_auto_process(archive_id):
    """Set or clear the auto-process profile for an archive."""
    data = request.json or {}
    profile_id = data.get("profile_id")  # None to disable
    db.set_archive_processing_profile(archive_id, profile_id)
    broadcast_sse("archive_updated", {"id": archive_id, "processing_profile_id": profile_id})
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/process/cancel", methods=["POST"])
@login_required
def cancel_processing(archive_id):
    from processing_worker import cancel_archive_processing
    if cancel_archive_processing(archive_id):
        return jsonify({"ok": True})
    return jsonify({"error": "No active processing for this archive"}), 404


@app.route("/api/archives/<int:archive_id>/processable", methods=["GET"])
@login_required
def get_processable(archive_id):
    """Return count and list of files eligible for processing."""
    files = db.get_processable_files(archive_id)
    return jsonify({
        "count": len(files),
        "files": [{"id": f["id"], "name": f["name"], "size": f["size"]} for f in files],
    })


@app.route("/api/files/<int:file_id>/process", methods=["POST"])
@login_required
def process_single_file(file_id):
    """Queue processing for a single file."""
    data = request.json or {}
    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id is required"}), 400
    conn = db.get_db()
    row = conn.execute("SELECT archive_id FROM archive_files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "File not found"}), 404
    from processing_worker import queue_archive_processing
    ok, queued = queue_archive_processing(row["archive_id"], profile_id, [file_id], data.get("options"))
    if not ok:
        return jsonify({"error": queued}), 409
    return jsonify({"ok": True, "queued": queued})


# --- Notifications ---

@app.route("/api/notifications", methods=["GET"])
@login_required
def list_notifications():
    """Return all active (non-dismissed) notifications."""
    notifs = db.get_notifications(include_dismissed=False)
    return jsonify(notifs)


@app.route("/api/notifications", methods=["POST"])
@login_required
def create_notification():
    """Create a new notification (from frontend)."""
    data = request.json or {}
    message = data.get("message", "")
    ntype = data.get("type", "info")
    file_id = data.get("file_id")
    archive_id = data.get("archive_id")
    if not message:
        return jsonify({"error": "message is required"}), 400
    notif_id = db.create_notification(message, type=ntype, file_id=file_id, archive_id=archive_id)
    notif = db.get_notification(notif_id)
    broadcast_sse("notification_created", notif)
    return jsonify(notif), 201


@app.route("/api/notifications/<int:notif_id>", methods=["PATCH"])
@login_required
def update_notification_endpoint(notif_id):
    """Update a notification's fields."""
    data = request.json or {}
    kwargs = {}
    if "message" in data:
        kwargs["message"] = data["message"]
    if "type" in data:
        kwargs["type"] = data["type"]
    if "dismissed" in data:
        kwargs["dismissed"] = data["dismissed"]
    if kwargs:
        db.update_notification(notif_id, **kwargs)
        notif = db.get_notification(notif_id)
        if notif:
            broadcast_sse("notification_updated", notif)
    return jsonify({"ok": True})


@app.route("/api/notifications/<int:notif_id>", methods=["DELETE"])
@login_required
def dismiss_notification(notif_id):
    """Dismiss (delete) a single notification. Refuses to delete active notifications."""
    db.delete_notification(notif_id)
    broadcast_sse("notification_dismissed", {"id": notif_id})
    return jsonify({"ok": True})


@app.route("/api/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    """Clear all clearable notifications (not active scan/processing/adding)."""
    db.clear_notifications()
    broadcast_sse("notifications_cleared", {})
    return jsonify({"ok": True})


# ── Collections ──────────────────────────────────────────────────────────

import collection_sync


@app.route("/api/collections", methods=["GET"])
@login_required
def get_collections():
    """Return all collections with summary info."""
    return jsonify(db.get_collections())


@app.route("/api/collections", methods=["POST"])
@login_required
def create_collection():
    """Create a new collection (name only — all other options are per-layout)."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        coll = db.create_collection(name)
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "A collection with that name already exists"}), 409
        raise
    broadcast_sse("collection_created", coll)
    return jsonify(coll), 201


@app.route("/api/collections/<int:collection_id>", methods=["GET"])
@login_required
def get_collection(collection_id):
    """Return a single collection with full details."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    return jsonify(coll)


@app.route("/api/collections/<int:collection_id>", methods=["PUT"])
@login_required
def update_collection(collection_id):
    """Update a collection's name or position."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    data = request.get_json(force=True)
    kwargs = {}
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Name cannot be empty"}), 400
        kwargs["name"] = name
    if "position" in data:
        kwargs["position"] = int(data["position"])
    if kwargs:
        try:
            db.update_collection(collection_id, **kwargs)
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"error": "A collection with that name already exists"}), 409
            raise
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
    return jsonify(updated)


@app.route("/api/collections/<int:collection_id>", methods=["DELETE"])
@login_required
def delete_collection(collection_id):
    """Delete a collection and remove its symlink directories."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    collection_sync.delete_collection_files(collection_id)
    db.delete_collection(collection_id)
    broadcast_sse("collection_deleted", {"id": collection_id})
    return jsonify({"ok": True})


@app.route("/api/collections/reorder", methods=["POST"])
@login_required
def reorder_collections():
    """Reorder collections. Expects {"order": [id, id, ...]}."""
    data = request.get_json(force=True)
    order = data.get("order", [])
    for pos, cid in enumerate(order):
        db.update_collection(cid, position=pos)
    broadcast_sse("collections_reordered", {"order": order})
    return jsonify({"ok": True})


# ── Collection Layouts ───────────────────────────────────────────────────

@app.route("/api/collections/<int:collection_id>/layouts", methods=["GET"])
@login_required
def get_collection_layouts(collection_id):
    """Return layouts for a collection."""
    return jsonify(db.get_collection_layouts(collection_id))


@app.route("/api/collections/<int:collection_id>/layouts", methods=["POST"])
@login_required
def add_collection_layout(collection_id):
    """Add a layout to a collection (always segments type)."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Layout name is required"}), 400
    flatten = int(data.get("flatten", 1))
    use_media_units = int(data.get("use_media_units", 1))
    layout = db.add_collection_layout(collection_id, name, flatten=flatten, use_media_units=use_media_units)
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
    return jsonify(layout), 201


@app.route("/api/collections/<int:collection_id>/layouts/<int:layout_id>", methods=["PUT"])
@login_required
def update_collection_layout(collection_id, layout_id):
    """Update a layout's name, type, or position."""
    data = request.get_json(force=True)
    kwargs = {}
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Layout name cannot be empty"}), 400
        kwargs["name"] = name
    if "position" in data:
        kwargs["position"] = int(data["position"])
    if "flatten" in data:
        kwargs["flatten"] = int(data["flatten"])
    if "use_media_units" in data:
        kwargs["use_media_units"] = int(data["use_media_units"])
    if kwargs:
        db.update_collection_layout(layout_id, **kwargs)
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
    return jsonify(db.get_collection_layouts(collection_id))


@app.route("/api/collections/<int:collection_id>/layouts/<int:layout_id>", methods=["DELETE"])
@login_required
def delete_collection_layout(collection_id, layout_id):
    """Delete a layout."""
    db.delete_collection_layout(layout_id)
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
    return jsonify({"ok": True})


# ── Layout Nodes ──────────────────────────────────────────────────────

@app.route("/api/layouts/<int:layout_id>/nodes", methods=["GET"])
@login_required
def get_layout_nodes(layout_id):
    """Return the node tree for a layout."""
    nodes = db.get_layout_nodes(layout_id)
    return jsonify(nodes)


@app.route("/api/layouts/<int:layout_id>/nodes", methods=["POST"])
@login_required
def add_layout_node(layout_id):
    """Add a node to a layout."""
    data = request.get_json(force=True)
    name = data.get("name", "New Folder")
    node_type = data.get("type", "all")
    parent_id = data.get("parent_id")
    tag_filter = data.get("tag_filter")
    sort_mode = data.get("sort_mode", "flat")
    include_untagged = data.get("include_untagged", True)
    renames_json = data.get("renames_json")
    node = db.add_layout_node(
        layout_id, name, node_type, parent_id=parent_id,
        tag_filter=tag_filter, sort_mode=sort_mode,
        include_untagged=include_untagged, renames_json=renames_json,
    )
    return jsonify(node), 201


@app.route("/api/layouts/nodes/<int:node_id>", methods=["PATCH"])
@login_required
def update_layout_node(node_id):
    """Update a layout node."""
    data = request.get_json(force=True)
    db.update_layout_node(node_id, **data)
    return jsonify({"ok": True})


@app.route("/api/layouts/nodes/<int:node_id>", methods=["DELETE"])
@login_required
def delete_layout_node(node_id):
    """Delete a layout node (cascades to children)."""
    db.delete_layout_node(node_id)
    return jsonify({"ok": True})


# ── Layout Segments (new path-based layout system) ────────────────────

@app.route("/api/layouts/<int:layout_id>/segments", methods=["GET"])
@login_required
def get_layout_segments(layout_id):
    """Return the ordered segments for a layout."""
    return jsonify(db.get_layout_segments(layout_id))


@app.route("/api/layouts/<int:layout_id>/segments", methods=["POST"])
@login_required
def add_layout_segment(layout_id):
    """Add a segment to a layout."""
    data = request.get_json(force=True)
    segment_type = data.get("segment_type", "").strip()
    valid_types = ("literal", "tag_parent", "tag_specific", "tag_group",
                   "hidden_filter", "alphabetical")
    if segment_type not in valid_types:
        return jsonify({"error": f"Invalid segment type: {segment_type}"}), 400
    seg = db.add_layout_segment(
        layout_id,
        segment_type=segment_type,
        segment_value=data.get("segment_value"),
        visible=data.get("visible", True),
        include_untagged=data.get("include_untagged", False),
        position=data.get("position"),
    )
    return jsonify(seg), 201


@app.route("/api/layouts/segments/<int:segment_id>", methods=["PATCH"])
@login_required
def update_layout_segment(segment_id):
    """Update a segment's properties."""
    data = request.get_json(force=True)
    db.update_layout_segment(segment_id, **data)
    return jsonify({"ok": True})


@app.route("/api/layouts/segments/<int:segment_id>", methods=["DELETE"])
@login_required
def delete_layout_segment(segment_id):
    """Delete a segment."""
    db.delete_layout_segment(segment_id)
    return jsonify({"ok": True})


@app.route("/api/layouts/<int:layout_id>/segments/reorder", methods=["POST"])
@login_required
def reorder_layout_segments(layout_id):
    """Reorder segments. Body: {"segment_ids": [3, 1, 2]}."""
    data = request.get_json(force=True)
    segment_ids = data.get("segment_ids", [])
    if not segment_ids:
        return jsonify({"error": "segment_ids required"}), 400
    db.reorder_layout_segments(layout_id, segment_ids)
    return jsonify({"ok": True})


# ── Collection Sync ──────────────────────────────────────────────────────

@app.route("/api/collections/<int:collection_id>/sync", methods=["POST"])
@login_required
def sync_collection(collection_id):
    """Trigger a sync for a collection — rebuilds all symlinks."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404

    # Create a notification for the sync
    notif_id = db.create_notification(
        f"Syncing collection '{coll['name']}'...", type="info"
    )
    broadcast_sse("notification_created", db.get_notification(notif_id))

    try:
        stats = collection_sync.sync_collection(collection_id)
    except Exception as e:
        db.update_notification(notif_id, message=f"Sync failed: {e}", type="error")
        broadcast_sse("notification_updated", db.get_notification(notif_id))
        return jsonify({"error": str(e)}), 500

    if stats.get("error"):
        db.update_notification(notif_id, message=f"Sync failed: {stats['error']}", type="error")
        broadcast_sse("notification_updated", db.get_notification(notif_id))
        return jsonify(stats), 400

    # Build summary message
    msg = (
        f"Collection '{coll['name']}' synced: "
        f"{stats['total_created']} created, {stats['total_removed']} removed"
    )
    if stats["total_errors"]:
        msg += f", {stats['total_errors']} errors"
        db.update_notification(notif_id, message=msg, type="warning")
    else:
        db.update_notification(notif_id, message=msg, type="success")
    broadcast_sse("notification_updated", db.get_notification(notif_id))
    broadcast_sse("collection_synced", stats)
    return jsonify(stats)


@app.route("/api/collections/<int:collection_id>/files", methods=["GET"])
@login_required
def get_collection_files(collection_id):
    """Return all files that would be included in this collection."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    files = db.get_collection_files(collection_id)
    return jsonify(files)


@app.route("/api/collections/<int:collection_id>/preview", methods=["GET"])
@login_required
def get_collection_preview(collection_id):
    """Return a flat row array previewing what sync would produce."""
    result = collection_sync.preview_collection(collection_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


# ── Media Units ──────────────────────────────────────────────────────────

@app.route("/api/archives/<int:archive_id>/media-units", methods=["GET"])
@login_required
def get_archive_media_units(archive_id):
    """Return media unit groups for an archive."""
    return jsonify(db.get_media_units(archive_id))


@app.route("/api/files/media-root", methods=["POST"])
@login_required
def set_files_media_root():
    """Set media_root for a list of file IDs.

    Body: {"file_ids": [1, 2, 3], "media_root": "Some Folder"}
    To clear (split): {"file_ids": [1, 2, 3], "media_root": ""}
    """
    data = request.get_json(force=True)
    file_ids = data.get("file_ids", [])
    media_root = data.get("media_root", "")
    if not file_ids:
        return jsonify({"error": "No file_ids provided"}), 400
    count = db.set_media_root(file_ids, media_root)
    return jsonify({"ok": True, "updated": count})


# ── Archive Tags ─────────────────────────────────────────────────────────

@app.route("/api/archives/<int:archive_id>/tags", methods=["GET"])
@login_required
def get_archive_tags(archive_id):
    """Return tags for an archive."""
    return jsonify(db.get_archive_tags(archive_id))


@app.route("/api/archives/<int:archive_id>/tags", methods=["POST"])
@login_required
def add_archive_tag(archive_id):
    """Add a tag to an archive."""
    data = request.get_json(force=True)
    tag = data.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "Tag is required"}), 400
    db.add_archive_tag(archive_id, tag)
    return jsonify(db.get_archive_tags(archive_id))


@app.route("/api/archives/<int:archive_id>/tags/<tag>", methods=["DELETE"])
@login_required
def remove_archive_tag(archive_id, tag):
    """Remove a tag from an archive."""
    db.remove_archive_tag(archive_id, tag)
    return jsonify(db.get_archive_tags(archive_id))


@app.route("/api/tags", methods=["GET"])
@login_required
def get_all_tags():
    """Return all unique tags with usage counts."""
    return jsonify(db.get_all_tags())


@app.route("/api/archives/<int:archive_id>/auto-tag", methods=["POST"])
@login_required
def trigger_auto_tag(archive_id):
    """Trigger auto-tagging for an archive (re-parses all filenames)."""
    from auto_tagger import auto_tag_archive, load_tag_key
    load_tag_key()  # reload TAG_KEY.txt so edits take effect immediately
    auto_tag_archive(archive_id)
    return jsonify({"ok": True, "tags": db.get_archive_tags(archive_id)})


@app.route("/api/files/auto-tag", methods=["POST"])
@login_required
def trigger_auto_tag_files():
    """Trigger auto-tagging for specific files by ID."""
    data = request.get_json(force=True)
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "No file_ids provided"}), 400
    from auto_tagger import auto_tag_files, load_tag_key
    load_tag_key()  # reload TAG_KEY.txt so edits take effect immediately
    tagged = auto_tag_files(file_ids)
    return jsonify({"ok": True, "tagged": tagged})


# ── File Tags ──────────────────────────────────────────────────────

@app.route("/api/files/<int:file_id>/tags", methods=["GET"])
@login_required
def get_file_tags(file_id):
    """Return tags for a file (own + inherited from archive + inherited from parent chain)."""
    own_tags = db.get_file_tags(file_id)
    # Get inherited archive-level tags
    file_info = db.get_file(file_id)
    inherited = []
    own_tag_set = {t["tag"] for t in own_tags}
    if file_info:
        archive_tags = db.get_archive_tags(file_info["archive_id"])
        for at in archive_tags:
            if at["tag"] not in own_tag_set:
                inherited.append({"tag": at["tag"], "auto": True, "inherited": True})
                own_tag_set.add(at["tag"])
        # Walk parent_file_id chain for containment inheritance
        if file_info.get("parent_file_id"):
            parent_tags = db.get_inherited_file_tags(file_id)
            for pt in parent_tags:
                if pt["tag"] not in own_tag_set:
                    inherited.append(pt)
                    own_tag_set.add(pt["tag"])
    return jsonify({"own": own_tags, "inherited": inherited})


@app.route("/api/files/<int:file_id>/tags", methods=["POST"])
@login_required
def add_file_tag(file_id):
    """Add a user tag to a file. Supports comma-separated tags."""
    from auto_tagger import sanitise_tag
    data = request.get_json(force=True)
    raw = data.get("tag", "")
    added = []
    for part in raw.split(","):
        tag = sanitise_tag(part)
        if tag:
            db.add_file_tag(file_id, tag, auto=False)
            added.append(tag)
    return jsonify(db.get_file_tags(file_id))


@app.route("/api/files/<int:file_id>/tags/<path:tag>", methods=["DELETE"])
@login_required
def remove_file_tag(file_id, tag):
    """Remove a user-added tag from a file."""
    db.remove_file_tag(file_id, tag)
    return jsonify(db.get_file_tags(file_id))



@app.route("/api/collections/settings", methods=["GET"])
@login_required
def get_collections_settings():
    """Return the collections directory path."""
    return jsonify({
        "collections_dir": collection_sync.get_collections_dir(),
        "download_dir": collection_sync.get_download_dir(),
        "processed_dir": collection_sync.get_processed_dir(),
    })


# --- Activity Log API ---

@app.route("/api/activity/log", methods=["GET"])
@login_required
def get_activity_log():
    """Return activity log entries with optional filters."""
    job_id = request.args.get("job_id", type=int)
    archive_id = request.args.get("archive_id", type=int)
    group_id = request.args.get("group_id", type=int)
    category = request.args.get("category")
    level = request.args.get("level")
    search = request.args.get("search")
    limit = request.args.get("limit", 200, type=int)
    offset = request.args.get("offset", 0, type=int)
    entries = activity.get_log_entries(
        job_id=job_id, archive_id=archive_id, group_id=group_id,
        category=category, level=level, search=search,
        limit=limit, offset=offset,
    )
    total = activity.get_log_count(
        job_id=job_id, archive_id=archive_id, group_id=group_id,
        category=category, level=level, search=search,
    )
    return jsonify({"entries": entries, "total": total})


@app.route("/api/activity/jobs", methods=["GET"])
@login_required
def get_activity_jobs():
    """Return recent activity jobs."""
    category = request.args.get("category")
    archive_id = request.args.get("archive_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    jobs = activity.get_jobs(
        category=category, archive_id=archive_id,
        limit=limit, offset=offset,
    )
    return jsonify({"jobs": jobs})


@app.route("/api/activity/jobs/<int:job_id>", methods=["GET"])
@login_required
def get_activity_job(job_id):
    """Return a single activity job."""
    j = activity.get_job(job_id)
    if not j:
        return jsonify({"error": "Not found"}), 404
    return jsonify(j)


@app.route("/api/activity/jobs/<int:job_id>", methods=["DELETE"])
@login_required
def delete_activity_job(job_id):
    """Delete an activity job and its log entries."""
    activity.delete_job(job_id)
    return jsonify({"ok": True})


# --- Init ---

def create_app():
    db.init_db()
    db.reset_downloading_files()
    db.reset_stale_processing()
    # Prune old dismissed notifications and stale activity log entries on startup
    try:
        db.prune_notifications(max_age_days=7, max_dismissed=200)
        import activity
        activity.prune(max_age_days=30)
    except Exception:
        pass  # Non-critical — don't block startup
    # Configure debug logging from saved settings
    configure_logging(
        enabled=db.get_setting("debug_enabled", "0") == "1",
        log_file=db.get_setting("debug_log_file", ""),
    )
    # Start processing worker
    from processing_worker import init_processing_worker
    init_processing_worker(broadcast_sse)
    # Start content fetch worker (must be after init_db so tables exist)
    _start_content_fetch_worker()
    # Load saved bandwidth limit (-1 = unlimited, 0 = paused, >0 = throttle)
    # One-time migration: old "0 = unlimited" → new "-1 = unlimited"
    if not db.get_setting("bw_migrated", ""):
        saved_bw = db.get_setting("bandwidth_limit", "-1")
        if saved_bw == "0":
            db.set_setting("bandwidth_limit", "-1")
        db.set_setting("bw_migrated", "1")
    saved_bw = db.get_setting("bandwidth_limit", "-1")
    download_manager.bandwidth_limit = int(saved_bw)
    return app


if __name__ == "__main__":
    import signal
    import socket
    from werkzeug.serving import make_server

    application = create_app()

    port = int(os.environ.get("GRABIA_PORT", 5000))

    # Use SO_REUSEADDR + SO_REUSEPORT to avoid "address already in use" on restart
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    host = os.environ.get("GRABIA_HOST", "127.0.0.1")

    sock.bind((host, port))
    sock.listen(128)

    server = make_server(host, port, application, threaded=True, fd=sock.fileno())

    def _shutdown(signum, frame):
        print("\n * Shutting down Grabia...")
        # Poison all SSE queues so their handler threads can finish
        with sse_lock:
            for q in sse_queues:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        # Run shutdown in a thread — it blocks waiting for serve_forever()
        # to exit, and serve_forever() runs in this (main) thread, so calling
        # shutdown() here directly would deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f" * Serving Grabia on http://{host}:{port}")
    server.serve_forever()
    download_manager.stop()
    sock.close()
    print(" * Stopped.")
