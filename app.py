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
        q = queue.Queue(maxsize=200)
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
    allowed = ["ia_email", "ia_password", "download_dir", "max_retries",
               "retry_delay", "bandwidth_limit", "theme", "files_per_page",
               "speed_schedule", "use_http",
               "confirm_reset_order", "confirm_delete_file",
               "confirm_batch_delete_files", "confirm_delete_folders",
               "confirm_delete_processed", "confirm_delete_profile",
               "default_enable_archive", "default_select_all", "sse_update_rate",
               "processing_temp_dir",
               "debug_enabled", "debug_log_file"]
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
    return jsonify({"ok": True})


@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
@login_required
def remove_group(group_id):
    db.delete_group(group_id)
    broadcast_sse("groups_changed", {})
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

    try:
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        use_http = db.get_setting("use_http", "0") == "1"
        meta = ia_client.fetch_metadata(identifier, ia_email, ia_password, use_http=use_http)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch metadata: {str(e)}"}), 502

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

    # Apply options from the add modal
    enable = data.get("enable", False)
    select_all = data.get("select_all", True)
    group_id = data.get("group_id")
    if enable:
        db.set_archive_download_enabled(archive_id, True)
    if not select_all:
        db.set_all_files_queued(archive_id, False)
    if group_id:
        db.set_archive_group(archive_id, group_id)

    archive = db.get_archive(archive_id)
    broadcast_sse("archive_added", archive)
    return jsonify(archive), 201


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

    import shutil
    removed = False
    if os.path.isdir(base_dir) and base_dir.startswith(os.path.realpath(download_dir) + os.sep):
        shutil.rmtree(base_dir)
        removed = True

    # Reset all files to pending
    conn = db.get_db()
    conn.execute(
        "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = 0, "
        "processing_status = '', processed_filename = '', processed_files_json = '', "
        "processing_error = '', error_message = '' WHERE archive_id = ?",
        (archive_id,),
    )
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

    try:
        ia_email = db.get_setting("ia_email", "")
        ia_password = db.get_setting("ia_password", "")
        use_http = db.get_setting("use_http", "0") == "1"
        meta = ia_client.fetch_metadata(archive["identifier"], ia_email, ia_password, use_http=use_http)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch metadata: {str(e)}"}), 502

    summary = db.refresh_archive_metadata(archive_id, meta["files"])
    updated = db.get_archive(archive_id)
    broadcast_sse("archive_updated", updated)
    return jsonify({"ok": True, "summary": summary})


# --- Scan Queue ---

_scan_queue = queue.Queue()
_scan_cancel = {}  # archive_id -> threading.Event
_scan_lock = threading.Lock()


def _scan_worker():
    """Background worker that processes scan jobs sequentially."""
    while True:
        archive_id = _scan_queue.get()
        try:
            _run_scan(archive_id)
        except Exception as e:
            # Update notification with error
            scan_notif = db.find_notification_by_scan(archive_id)
            if scan_notif:
                db.update_notification(scan_notif["id"], message=f'Scan failed: {e}', type="error", progress=None)
                broadcast_sse("notification_updated", db.get_notification(scan_notif["id"]))
            # Try to find and close the activity job for this scan
            try:
                with db._db() as conn:
                    row = conn.execute(
                        "SELECT id FROM activity_jobs WHERE category = 'scan' AND archive_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                        (archive_id,),
                    ).fetchone()
                    if row:
                        activity.log(row["id"], "error", f"Scan crashed: {e}",
                                     archive_id=archive_id, detail=str(e))
                        activity.flush()
                        activity.finish_job(row["id"], "failed", summary=str(e))
            except Exception:
                pass
            broadcast_sse("scan_progress", {
                "archive_id": archive_id, "phase": "error",
                "error": str(e),
            })
        finally:
            with _scan_lock:
                _scan_cancel.pop(archive_id, None)
            _scan_queue.task_done()


_scan_thread = threading.Thread(target=_scan_worker, daemon=True)
_scan_thread.start()


def _run_scan(archive_id):
    """Execute the actual scan logic in a background thread."""
    log.info("scan", "Starting scan for archive %d", archive_id)
    cancel_evt = _scan_cancel.get(archive_id)
    archive = db.get_archive(archive_id)
    if not archive:
        log.warning("scan", "Archive %d not found, aborting scan", archive_id)
        return

    archive_name = archive["title"] or archive["identifier"]
    group_id = archive.get("group_id")

    # Create activity job for this scan
    act_job_id = activity.start_job("scan", archive_id=archive_id, group_id=group_id)

    # Create or find existing scan notification
    scan_notif = db.find_notification_by_scan(archive_id)
    if not scan_notif:
        scan_notif_id = db.create_notification(
            f'Scanning "{archive_name}": starting...',
            type="info", progress=0, scan_archive_id=archive_id,
            job_id=act_job_id,
        )
        broadcast_sse("notification_created", db.get_notification(scan_notif_id))
    else:
        scan_notif_id = scan_notif["id"]
        # Link existing notification to the activity job
        db.update_notification(scan_notif_id, job_id=act_job_id)

    activity.update_job_notification(act_job_id, scan_notif_id)

    # Read update rate from settings (milliseconds -> seconds)
    update_rate = int(db.get_setting("sse_update_rate", "500")) / 1000.0

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    if not os.path.isdir(base_dir):
        msg = f"Download folder not found: {base_dir}"
        activity.log(act_job_id, "error", msg, archive_id=archive_id)
        activity.flush()
        activity.finish_job(act_job_id, "failed", summary="Folder not found")
        db.update_notification(scan_notif_id, message=f'Scan "{archive_name}" failed: folder not found', type="error", progress=None)
        broadcast_sse("notification_updated", db.get_notification(scan_notif_id))
        broadcast_sse("scan_progress", {
            "archive_id": archive_id, "phase": "error",
            "error": msg,
        })
        return

    def _cancelled():
        return cancel_evt and cancel_evt.is_set()

    def _abort():
        activity.log(act_job_id, "warning", "Scan cancelled by user",
                     archive_id=archive_id)
        activity.flush()
        activity.finish_job(act_job_id, "cancelled",
                            summary=f"Cancelled at {processed}/{total_manifest}")
        db.delete_notification(scan_notif_id)
        broadcast_sse("notification_dismissed", {"id": scan_notif_id})
        broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "cancelled", "current": processed, "total": total_manifest})
        conn.close()

    # Time-based progress throttle
    last_progress = [0.0]  # mutable for closure
    last_notif_update = [0.0]

    def _progress():
        now = time.monotonic()
        if now - last_progress[0] >= update_rate or processed == total_manifest:
            broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "verify", "current": processed, "total": total_manifest})
            last_progress[0] = now
        # Update persistent notification less frequently (every 2s)
        if now - last_notif_update[0] >= 2.0 or processed == total_manifest:
            pct = int((processed / total_manifest) * 100) if total_manifest > 0 else 0
            db.update_notification(scan_notif_id, message=f'Scanning "{archive_name}": {processed}/{total_manifest} ({pct}%)', progress=pct)
            broadcast_sse("notification_updated", db.get_notification(scan_notif_id))
            last_notif_update[0] = now

    # Clean slate
    conn = db.get_db()
    conn.execute(
        "DELETE FROM archive_files WHERE archive_id = ? AND origin = 'scan'",
        (archive_id,),
    )
    conn.execute(
        "UPDATE archive_files SET download_status = 'pending', error_message = '' "
        "WHERE archive_id = ? AND download_status IN ('conflict', 'unknown')",
        (archive_id,),
    )
    conn.commit()

    # Ground truth
    rows = conn.execute(
        "SELECT id, name, size, md5, download_status, processing_status, processed_filename FROM archive_files "
        "WHERE archive_id = ? AND origin = 'manifest'",
        (archive_id,),
    ).fetchall()
    manifest = {r["name"]: dict(r) for r in rows}
    log.debug("scan", "Manifest has %d files for %s", len(manifest), archive["identifier"])

    summary = {"matched": 0, "conflict": 0, "unknown": 0, "missing": 0, "partial": 0}

    total_manifest = len(manifest)
    processed = 0
    # Batch DB writes: accumulate and commit periodically for live UI updates
    BATCH_SIZE = 25
    pending_writes = []

    def _flush_writes():
        """Commit accumulated DB writes and recompute archive status."""
        if not pending_writes:
            return
        for sql, params in pending_writes:
            conn.execute(sql, params)
        conn.commit()
        pending_writes.clear()
        db.recompute_archive_status(archive_id)

    activity.log(act_job_id, "info",
                 f"Scan started: {total_manifest} manifest files to verify",
                 archive_id=archive_id)

    broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "verify", "current": 0, "total": total_manifest})

    for name, info in manifest.items():
        if _cancelled():
            _flush_writes()
            _abort()
            return

        # Check if a previously processed file still exists on disk
        if info.get("processing_status") == "processed":
            pf = info.get("processed_filename", "")
            pf_path = os.path.join(base_dir, pf) if pf else ""
            # processed_filename can be a file or a directory (folder extraction)
            if pf_path and (os.path.isfile(pf_path) or os.path.isdir(pf_path)):
                log.debug("scan", "%s: processed output %s exists, matched", name, pf)
                summary["matched"] += 1
                processed += 1
                _progress()
                continue
            # Processed output is gone — reset processing state
            log.info("scan", "%s: processed output %s missing, resetting processing state", name, pf)
            pending_writes.append((
                "UPDATE archive_files SET processing_status = '', processed_filename = '', "
                "processed_files_json = '', processor_type = '', processing_error = '' WHERE id = ?",
                (info["id"],),
            ))

        local_path = os.path.realpath(os.path.join(base_dir, name))
        if not local_path.startswith(base_dir + os.sep) and local_path != base_dir:
            continue

        if not os.path.isfile(local_path):
            # Check if a processed version exists (e.g., game.zip → game.chd)
            base_no_ext = os.path.splitext(name)[0]
            found_processed = False
            for proc_ext in (".chd", ".cso"):
                proc_path = os.path.join(base_dir, base_no_ext + proc_ext)
                if os.path.isfile(proc_path):
                    proc_filename = base_no_ext + proc_ext
                    pending_writes.append((
                        "UPDATE archive_files SET download_status = 'completed', "
                        "processing_status = 'processed', processed_filename = ?, "
                        "downloaded_bytes = ? WHERE id = ?",
                        (proc_filename, os.path.getsize(proc_path), info["id"]),
                    ))
                    summary["matched"] += 1
                    found_processed = True
                    break
            if not found_processed:
                pending_writes.append((
                    "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = 0 WHERE id = ?",
                    (info["id"],),
                ))
                summary["missing"] += 1
            processed += 1
            if len(pending_writes) >= BATCH_SIZE:
                _flush_writes()
            _progress()
            continue

        local_size = os.path.getsize(local_path)
        expected_size = info["size"]
        expected_md5 = info["md5"]

        if info["download_status"] == "completed":
            summary["matched"] += 1
            processed += 1
            _progress()
            continue

        has_size = expected_size > 0
        has_md5 = bool(expected_md5) and has_size

        if not has_size and not has_md5:
            pending_writes.append((
                "UPDATE archive_files SET download_status = 'conflict', error_message = ? WHERE id = ?",
                (f"Cannot verify: no size/hash in manifest (local file is {local_size} bytes)", info["id"]),
            ))
            summary["conflict"] += 1
            processed += 1
            if len(pending_writes) >= BATCH_SIZE:
                _flush_writes()
            _progress()
            continue

        # Size check (fast)
        if has_size and local_size != expected_size:
            if local_size < expected_size:
                pending_writes.append((
                    "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = ?, queued = 1, "
                    "error_message = 'Partial download detected by scan' WHERE id = ?",
                    (local_size, info["id"]),
                ))
                summary["partial"] += 1
            else:
                pending_writes.append((
                    "UPDATE archive_files SET download_status = 'conflict', error_message = ? WHERE id = ?",
                    (f"Size mismatch: local {local_size} vs expected {expected_size}", info["id"]),
                ))
                summary["conflict"] += 1
            processed += 1
            if len(pending_writes) >= BATCH_SIZE:
                _flush_writes()
            _progress()
            continue

        # MD5 check — 128KB chunks for better HDD throughput
        if has_md5:
            md5 = hashlib.md5()
            try:
                with open(local_path, "rb") as f:
                    for chunk in iter(lambda: f.read(131072), b""):
                        if _cancelled():
                            _flush_writes()
                            _abort()
                            return
                        md5.update(chunk)
                if md5.hexdigest() != expected_md5:
                    pending_writes.append((
                        "UPDATE archive_files SET download_status = 'conflict', error_message = ? WHERE id = ?",
                        (f"MD5 mismatch: local {md5.hexdigest()} vs expected {expected_md5}", info["id"]),
                    ))
                    summary["conflict"] += 1
                    processed += 1
                    if len(pending_writes) >= BATCH_SIZE:
                        _flush_writes()
                    _progress()
                    continue
            except OSError as e:
                pending_writes.append((
                    "UPDATE archive_files SET download_status = 'conflict', error_message = ? WHERE id = ?",
                    (f"Read error: {e}", info["id"]),
                ))
                summary["conflict"] += 1
                processed += 1
                if len(pending_writes) >= BATCH_SIZE:
                    _flush_writes()
                _progress()
                continue

        pending_writes.append((
            "UPDATE archive_files SET download_status = 'completed', downloaded_bytes = ? WHERE id = ?",
            (local_size, info["id"]),
        ))
        summary["matched"] += 1
        processed += 1
        if len(pending_writes) >= BATCH_SIZE:
            _flush_writes()
        _progress()

    # Flush any remaining writes from verification phase
    _flush_writes()

    # Scan for unknown files on disk
    db.update_notification(scan_notif_id, message=f'Scanning "{archive_name}": checking for unknown files...', progress=-1)
    broadcast_sse("notification_updated", db.get_notification(scan_notif_id))
    broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "disk", "current": 0, "total": 0})

    # Build a set of known processed filenames so we don't flag them as unknown
    processed_names = db.get_all_processed_files(archive_id)

    unknown_files = []
    for root, _dirs, files in os.walk(base_dir):
        if _cancelled():
            _abort()
            return
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, base_dir)
            if rel not in manifest and rel not in processed_names:
                unknown_files.append(rel)
                summary["unknown"] += 1

    # Insert unknown files
    max_pri = conn.execute(
        "SELECT COALESCE(MAX(download_priority), -1) FROM archive_files WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()[0]
    for i, rel_name in enumerate(unknown_files):
        local_path = os.path.join(base_dir, rel_name)
        local_size = 0
        try:
            local_size = os.path.getsize(local_path)
        except OSError:
            pass
        conn.execute(
            """INSERT OR IGNORE INTO archive_files
               (archive_id, name, size, md5, sha1, format, source, mtime,
                queued, download_status, downloaded_bytes, error_message, download_priority, origin)
               VALUES (?, ?, ?, '', '', '', '', '', 0, 'unknown', ?, 'File found on disk but not in archive manifest', ?, 'scan')""",
            (archive_id, rel_name, local_size, local_size, max_pri + 1 + i),
        )

    conn.commit()
    conn.close()

    if unknown_files:
        log.debug("scan", "%d unknown files found on disk", len(unknown_files))

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
    if summary["unknown"] > 0:
        parts.append(f'{summary["unknown"]} unknown')
    if summary["missing"] > 0:
        parts.append(f'{summary["missing"]} not on disk')
    result_msg = ", ".join(parts) if parts else "no files found on disk"
    ntype = "success" if summary["conflict"] == 0 and summary["missing"] == 0 else "warning"
    db.update_notification(scan_notif_id, message=f'Scan "{archive_name}": {result_msg}', type=ntype, progress=None)
    broadcast_sse("notification_updated", db.get_notification(scan_notif_id))

    # Log scan summary to activity log
    if summary["conflict"] > 0:
        activity.log(act_job_id, "warning",
                     f'{summary["conflict"]} file(s) have conflicts',
                     archive_id=archive_id)
    if summary["missing"] > 0:
        activity.log(act_job_id, "warning",
                     f'{summary["missing"]} file(s) not found on disk',
                     archive_id=archive_id)
    if summary["partial"] > 0:
        activity.log(act_job_id, "info",
                     f'{summary["partial"]} partial download(s) re-queued',
                     archive_id=archive_id)
    if summary["unknown"] > 0:
        activity.log(act_job_id, "info",
                     f'{summary["unknown"]} unknown file(s) found on disk',
                     archive_id=archive_id)

    activity.log(act_job_id, "success" if ntype == "success" else "warning",
                 f"Scan complete: {result_msg}", archive_id=archive_id)
    activity.flush()
    activity.finish_job(act_job_id, "completed", summary=result_msg)

    broadcast_sse("scan_progress", {
        "archive_id": archive_id, "phase": "done",
        "current": total_manifest, "total": total_manifest,
        "summary": summary,
    })
    broadcast_sse("archive_updated", updated)


@app.route("/api/archives/<int:archive_id>/scan", methods=["POST"])
@login_required
def scan_existing_files(archive_id):
    """Queue a scan of the local download folder for files matching this archive's manifest."""
    archive = db.get_archive(archive_id)
    if not archive:
        return jsonify({"error": "Not found"}), 404

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
    if not os.path.isdir(base_dir):
        return jsonify({"error": f"Download folder not found: {base_dir}"}), 404

    with _scan_lock:
        if archive_id in _scan_cancel:
            return jsonify({"error": "Scan already queued for this archive"}), 409
        evt = threading.Event()
        _scan_cancel[archive_id] = evt

    pending = _scan_queue.qsize()
    archive_name = archive["title"] or archive["identifier"]

    # Create a persistent notification for the scan
    notif_id = db.create_notification(
        f'Scanning "{archive_name}": queued...' if pending > 0 else f'Scanning "{archive_name}": starting...',
        type="info", progress=-1, scan_archive_id=archive_id,
    )
    broadcast_sse("notification_created", db.get_notification(notif_id))

    _scan_queue.put(archive_id)
    return jsonify({"ok": True, "queued": pending > 0})


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
        "queued = 1, error_message = '' WHERE id = ?",
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


@app.route("/api/archives/reorder", methods=["POST"])
@login_required
def reorder_archives():
    data = request.json
    id_order = data.get("order", [])
    db.reorder_archives(id_order)
    broadcast_sse("archives_reordered", {"order": id_order})
    return jsonify({"ok": True})


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
    db.set_all_files_queued(archive_id, data.get("queued", True))
    return jsonify({"ok": True})


# Keep old endpoint as alias for backwards compatibility
@app.route("/api/archives/<int:archive_id>/files/select-all", methods=["POST"])
@login_required
def select_all_files_compat(archive_id):
    data = request.json
    db.set_all_files_queued(archive_id, data.get("queued", data.get("selected", True)))
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/files/reorder", methods=["POST"])
@login_required
def reorder_files(archive_id):
    data = request.json
    file_ids = data.get("order", [])
    db.reorder_archive_files(file_ids)
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/files/reset-order", methods=["POST"])
@login_required
def reset_file_order(archive_id):
    db.reset_file_priorities(archive_id)
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
        # Only reset processing state if there are no processed outputs remaining on disk
        has_outputs = False
        if f.get("processing_status") == "processed":
            if archive:
                download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
                base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
                # Check processed_filename
                pf = f.get("processed_filename", "")
                if pf:
                    pf_abs = os.path.realpath(os.path.join(base_dir, pf.rstrip("/").rstrip(os.sep)))
                    if pf_abs.startswith(base_dir + os.sep) and (os.path.isfile(pf_abs) or os.path.isdir(pf_abs)):
                        has_outputs = True
                # Check processed_files_json entries
                if not has_outputs and f.get("processed_files_json"):
                    try:
                        for p in json.loads(f["processed_files_json"]):
                            p_abs = os.path.realpath(os.path.join(base_dir, p.rstrip("/").rstrip(os.sep)))
                            if p_abs.startswith(base_dir + os.sep) and (os.path.isfile(p_abs) or os.path.isdir(p_abs)):
                                has_outputs = True
                                break
                    except (json.JSONDecodeError, TypeError):
                        pass
        if not has_outputs:
            conn = db.get_db()
            conn.execute(
                "UPDATE archive_files SET processing_status = '', processed_filename = '', "
                "processed_files_json = '', processing_error = '', processor_type = '' WHERE id = ?",
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

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    # Build the set of root paths to scan.
    # processed_filename is the primary output (a file, or a folder ending with /).
    # processed_files_json lists individual files — but we want the tree from the
    # root entries, not each leaf, so we prefer processed_filename when it's a folder.
    root_paths = []
    pf = (f.get("processed_filename") or "").rstrip("/").rstrip(os.sep)
    if pf:
        root_paths.append(pf)

    # For non-folder primary outputs, also include any entries from processed_files_json
    # that aren't already children of a root
    if f["processed_files_json"]:
        try:
            extra = json.loads(f["processed_files_json"])
            for p in extra:
                p = p.rstrip("/").rstrip(os.sep)
                # Skip if already covered by an existing root (as child)
                if any(p == r or p.startswith(r + "/") or p.startswith(r + os.sep) for r in root_paths):
                    continue
                root_paths.append(p)
        except (json.JSONDecodeError, TypeError):
            pass

    if not root_paths and f.get("processed_filename"):
        root_paths = [f["processed_filename"].rstrip("/").rstrip(os.sep)]

    # Build tree from disk (verify each entry actually exists)
    def _scan_path(rel_path):
        abs_path = os.path.realpath(os.path.join(base_dir, rel_path))
        if not abs_path.startswith(base_dir + os.sep):
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

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    if delete_all:
        # Delete every processed output for this file
        paths_to_delete = []
        if f["processed_files_json"]:
            try:
                paths_to_delete = json.loads(f["processed_files_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        if not paths_to_delete and f.get("processed_filename"):
            paths_to_delete = [f["processed_filename"]]
        for p in paths_to_delete:
            p = p.rstrip("/").rstrip(os.sep)
            abs_p = os.path.realpath(os.path.join(base_dir, p))
            if abs_p.startswith(base_dir + os.sep):
                if os.path.isfile(abs_p):
                    os.remove(abs_p)
                elif os.path.isdir(abs_p):
                    shutil.rmtree(abs_p)
        # Reset all processing state
        conn = db.get_db()
        conn.execute(
            "UPDATE archive_files SET processing_status = '', processed_filename = '', "
            "processed_files_json = '', processing_error = '', processor_type = '' WHERE id = ?",
            (file_id,),
        )
        conn.commit()
        conn.close()
    elif filename:
        # Delete a single processed output
        proc_path = os.path.realpath(os.path.join(base_dir, filename))
        if proc_path.startswith(base_dir + os.sep):
            if os.path.isfile(proc_path):
                os.remove(proc_path)
            elif os.path.isdir(proc_path):
                shutil.rmtree(proc_path)

        # Update processed_files_json to remove the deleted entry
        remaining = []
        if f["processed_files_json"]:
            try:
                all_files = json.loads(f["processed_files_json"])
                remaining = [p for p in all_files if p.rstrip("/").rstrip(os.sep) != filename.rstrip("/").rstrip(os.sep)
                             and not p.startswith(filename.rstrip("/") + "/")
                             and not p.startswith(filename.rstrip(os.sep) + os.sep)]
            except (json.JSONDecodeError, TypeError):
                pass

        # Check if we deleted the processed_filename itself
        pf = (f.get("processed_filename") or "").rstrip("/").rstrip(os.sep)
        deleted_pf = filename.rstrip("/").rstrip(os.sep)
        pf_was_deleted = pf and (pf == deleted_pf or pf.startswith(deleted_pf + "/") or pf.startswith(deleted_pf + os.sep))

        conn = db.get_db()
        if not remaining:
            # No processed files left — clear path fields but keep processing status
            # so the file still shows as processed/extracted (like automated processing
            # that deletes the source file)
            conn.execute(
                "UPDATE archive_files SET processed_filename = '', "
                "processed_files_json = '' WHERE id = ?",
                (file_id,),
            )
        else:
            updates = {"processed_files_json": json.dumps(remaining)}
            if pf_was_deleted:
                # The primary processed_filename was deleted; pick the first remaining as new root
                updates["processed_filename"] = remaining[0]
            parts = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE archive_files SET {parts} WHERE id = ?",
                (*updates.values(), file_id),
            )
        conn.commit()
        conn.close()

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

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    old_abs = os.path.realpath(os.path.join(base_dir, old_path))
    new_rel = os.path.join(os.path.dirname(old_path), new_name)
    new_abs = os.path.realpath(os.path.join(base_dir, new_rel))

    if not old_abs.startswith(base_dir + os.sep) or not new_abs.startswith(base_dir + os.sep):
        return jsonify({"error": "Invalid path"}), 400

    if (os.path.isfile(old_abs) or os.path.isdir(old_abs)):
        os.rename(old_abs, new_abs)

    # Update processed_filename if it matches
    conn = db.get_db()
    pf = f.get("processed_filename", "")
    if pf.rstrip("/").rstrip(os.sep) == old_path.rstrip("/").rstrip(os.sep):
        suffix = "/" if pf.endswith("/") or pf.endswith(os.sep) else ""
        conn.execute("UPDATE archive_files SET processed_filename = ? WHERE id = ?",
                     (new_rel + suffix, file_id))

    # Update processed_files_json entries
    if f["processed_files_json"]:
        try:
            all_files = json.loads(f["processed_files_json"])
            old_stripped = old_path.rstrip("/").rstrip(os.sep)
            updated = []
            for p in all_files:
                ps = p.rstrip("/").rstrip(os.sep)
                if ps == old_stripped:
                    updated.append(new_rel + ("/" if p.endswith("/") else ""))
                elif ps.startswith(old_stripped + "/") or ps.startswith(old_stripped + os.sep):
                    updated.append(new_rel + ps[len(old_stripped):])
                else:
                    updated.append(p)
            conn.execute("UPDATE archive_files SET processed_files_json = ? WHERE id = ?",
                         (json.dumps(updated), file_id))
        except (json.JSONDecodeError, TypeError):
            pass

    conn.commit()
    conn.close()
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
    """Re-scan a single file against what's on disk."""
    f = db.get_file(file_id)
    if not f:
        return jsonify({"error": "File not found"}), 404

    archive = db.get_archive(f["archive_id"])
    if not archive:
        return jsonify({"error": "Archive not found"}), 404

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))
    local_path = os.path.realpath(os.path.join(base_dir, f["name"]))

    if not local_path.startswith(base_dir + os.sep):
        return jsonify({"error": "Invalid path"}), 400

    result = {"status": "missing"}

    if os.path.isfile(local_path):
        local_size = os.path.getsize(local_path)
        expected_size = f["size"]

        if expected_size > 0 and local_size != expected_size:
            if local_size < expected_size:
                db.set_file_download_status(file_id, "pending", downloaded_bytes=local_size, error_message="Partial download detected by scan")
                if not f["queued"]:
                    db.set_file_queued(file_id, True)
                result = {"status": "partial"}
            else:
                db.set_file_download_status(file_id, "conflict", error_message=f"Size mismatch: local {local_size} vs expected {expected_size}")
                result = {"status": "conflict"}
        else:
            # MD5 check if available
            if f["md5"]:
                md5 = hashlib.md5()
                with open(local_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(8192), b""):
                        md5.update(chunk)
                if md5.hexdigest() != f["md5"]:
                    db.set_file_download_status(file_id, "conflict", error_message=f"MD5 mismatch: local {md5.hexdigest()} vs expected {f['md5']}")
                    result = {"status": "conflict"}
                else:
                    db.set_file_download_status(file_id, "completed", downloaded_bytes=local_size)
                    result = {"status": "completed"}
            else:
                db.set_file_download_status(file_id, "completed", downloaded_bytes=local_size)
                result = {"status": "completed"}
    else:
        # Check for processed version
        base_no_ext = os.path.splitext(f["name"])[0]
        for proc_ext in (".chd", ".cso"):
            proc_path = os.path.join(base_dir, base_no_ext + proc_ext)
            if os.path.isfile(proc_path):
                db.set_file_download_status(file_id, "completed", downloaded_bytes=os.path.getsize(proc_path))
                result = {"status": "completed"}
                break
        else:
            db.set_file_download_status(file_id, "pending", downloaded_bytes=0)
            result = {"status": "missing"}

    result["name"] = f["name"]
    db.recompute_archive_status(f["archive_id"])
    return jsonify({"ok": True, **result})


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
    return jsonify({"state": download_manager.state, "has_work": True})


@app.route("/api/download/pause", methods=["POST"])
@login_required
def pause_download():
    download_manager.pause()
    return jsonify({"state": download_manager.state})


@app.route("/api/download/stop", methods=["POST"])
@login_required
def stop_download():
    download_manager.stop()
    return jsonify({"state": download_manager.state})


@app.route("/api/download/status", methods=["GET"])
@login_required
def download_status():
    return jsonify(download_manager.get_status())


@app.route("/api/download/queue", methods=["GET"])
@login_required
def download_queue():
    return jsonify(db.get_download_queue())


@app.route("/api/download/bandwidth", methods=["POST"])
@login_required
def set_bandwidth():
    data = request.json
    limit = int(data.get("limit", -1))  # -1 = unlimited, 0 = paused, >0 = throttle
    download_manager.bandwidth_limit = limit
    return jsonify({"bandwidth_limit": download_manager.bandwidth_limit})


# --- Processing API ---

@app.route("/api/processing/profiles", methods=["GET"])
@login_required
def list_processing_profiles():
    profiles = db.get_processing_profiles()
    import json as _json
    for p in profiles:
        p["options"] = _json.loads(p.get("options_json", "{}"))
    return jsonify(profiles)


@app.route("/api/processing/profiles", methods=["POST"])
@login_required
def create_processing_profile():
    data = request.json
    name = data.get("name", "").strip()
    processor_type = data.get("processor_type", "")
    options = data.get("options", {})
    if not name:
        return jsonify({"error": "Name is required"}), 400
    from processors import get_processor_types
    if processor_type not in get_processor_types():
        return jsonify({"error": f"Unknown processor type: {processor_type}"}), 400
    profile_id = db.add_processing_profile(name, processor_type, options)
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
    if not message:
        return jsonify({"error": "message is required"}), 400
    progress = data.get("progress")
    adding_archive = data.get("adding_archive", False)
    notif_id = db.create_notification(message, type=ntype, progress=progress,
                                       adding_archive=adding_archive)
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
    if "progress" in data:
        kwargs["progress"] = data["progress"]
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
    notif = db.get_notification(notif_id)
    if notif and notif["progress"] is not None:
        return jsonify({"error": "Cannot dismiss an active notification"}), 409
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
    """Create a new collection."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    file_scope = data.get("file_scope", "processed")
    if file_scope not in ("processed", "downloaded", "both"):
        return jsonify({"error": "Invalid file_scope"}), 400
    auto_tag = data.get("auto_tag", "").strip() or None
    try:
        coll = db.create_collection(name, file_scope=file_scope, auto_tag=auto_tag)
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
    coll["archives"] = db.get_archives_for_collection(collection_id)
    return jsonify(coll)


@app.route("/api/collections/<int:collection_id>", methods=["PUT"])
@login_required
def update_collection(collection_id):
    """Update a collection's name, file_scope, or auto_tag."""
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
    if "file_scope" in data:
        if data["file_scope"] not in ("processed", "downloaded", "both"):
            return jsonify({"error": "Invalid file_scope"}), 400
        kwargs["file_scope"] = data["file_scope"]
    if "auto_tag" in data:
        kwargs["auto_tag"] = data["auto_tag"].strip() or None
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


# ── Collection Archives ──────────────────────────────────────────────────

@app.route("/api/collections/<int:collection_id>/archives", methods=["GET"])
@login_required
def get_collection_archives(collection_id):
    """Return archives in a collection."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    return jsonify(db.get_archives_for_collection(collection_id))


@app.route("/api/collections/<int:collection_id>/archives", methods=["POST"])
@login_required
def add_collection_archive(collection_id):
    """Add an archive to a collection."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    data = request.get_json(force=True)
    archive_id = data.get("archive_id")
    if not archive_id:
        return jsonify({"error": "archive_id is required"}), 400
    added = db.add_archive_to_collection(collection_id, archive_id)
    if not added:
        return jsonify({"error": "Archive already in collection"}), 409
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
    return jsonify({"ok": True}), 201


@app.route("/api/collections/<int:collection_id>/archives/<int:archive_id>", methods=["DELETE"])
@login_required
def remove_collection_archive(collection_id, archive_id):
    """Remove an archive from a collection."""
    db.remove_archive_from_collection(collection_id, archive_id)
    updated = db.get_collection(collection_id)
    broadcast_sse("collection_updated", updated)
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
    """Add a layout to a collection."""
    coll = db.get_collection(collection_id)
    if not coll:
        return jsonify({"error": "Collection not found"}), 404
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Layout name is required"}), 400
    layout_type = data.get("type", "flat")
    if layout_type not in ("flat", "alphabetical", "by_archive"):
        return jsonify({"error": "Invalid layout type"}), 400
    layout = db.add_collection_layout(collection_id, name, layout_type=layout_type)
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
    if "type" in data:
        if data["type"] not in ("flat", "alphabetical", "by_archive"):
            return jsonify({"error": "Invalid layout type"}), 400
        kwargs["type"] = data["type"]
    if "position" in data:
        kwargs["position"] = int(data["position"])
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
    broadcast_sse("notification", db.get_notification(notif_id))

    try:
        stats = collection_sync.sync_collection(collection_id)
    except Exception as e:
        db.update_notification(notif_id, message=f"Sync failed: {e}", type="error")
        broadcast_sse("notification", db.get_notification(notif_id))
        return jsonify({"error": str(e)}), 500

    if stats.get("error"):
        db.update_notification(notif_id, message=f"Sync failed: {stats['error']}", type="error")
        broadcast_sse("notification", db.get_notification(notif_id))
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
    broadcast_sse("notification", db.get_notification(notif_id))
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


@app.route("/api/archives/<int:archive_id>/collections", methods=["GET"])
@login_required
def get_archive_collections(archive_id):
    """Return collections that contain this archive."""
    return jsonify(db.get_collections_for_archive(archive_id))


@app.route("/api/collections/settings", methods=["GET"])
@login_required
def get_collections_settings():
    """Return the collections directory path."""
    return jsonify({
        "collections_dir": collection_sync.get_collections_dir(),
        "download_dir": collection_sync.get_download_dir(),
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


# --- Init ---

def create_app():
    db.init_db()
    db.reset_downloading_files()
    # Configure debug logging from saved settings
    configure_logging(
        enabled=db.get_setting("debug_enabled", "0") == "1",
        log_file=db.get_setting("debug_log_file", ""),
    )
    # Start processing worker
    from processing_worker import init_processing_worker
    init_processing_worker(broadcast_sse)
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
