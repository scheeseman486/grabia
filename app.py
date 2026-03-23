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
               "speed_schedule", "use_http", "confirm_reset_order",
               "default_enable_archive", "default_select_all", "sse_update_rate",
               "tool_chdman_path", "tool_maxcso_path", "tool_7z_path",
               "tool_unrar_path", "processing_temp_dir",
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
        db.set_all_files_selected(archive_id, False)
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

    # Read update rate from settings (milliseconds -> seconds)
    update_rate = int(db.get_setting("sse_update_rate", "500")) / 1000.0

    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    base_dir = os.path.realpath(os.path.join(download_dir, archive["identifier"]))

    if not os.path.isdir(base_dir):
        broadcast_sse("scan_progress", {
            "archive_id": archive_id, "phase": "error",
            "error": f"Download folder not found: {base_dir}",
        })
        return

    def _cancelled():
        return cancel_evt and cancel_evt.is_set()

    def _abort():
        broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "cancelled", "current": processed, "total": total_manifest})
        conn.close()

    # Time-based progress throttle
    last_progress = [0.0]  # mutable for closure

    def _progress():
        now = time.monotonic()
        if now - last_progress[0] >= update_rate or processed == total_manifest:
            broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "verify", "current": processed, "total": total_manifest})
            last_progress[0] = now

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

    broadcast_sse("scan_progress", {"archive_id": archive_id, "phase": "verify", "current": 0, "total": total_manifest})

    for name, info in manifest.items():
        if _cancelled():
            _flush_writes()
            _abort()
            return

        # Check if a previously processed/extracted file still exists on disk
        if info.get("processing_status") in ("completed", "extracted"):
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
                        "processing_status = 'completed', processed_filename = ?, "
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
                    "UPDATE archive_files SET download_status = 'pending', downloaded_bytes = ?, selected = 1, "
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
            "UPDATE archive_files SET download_status = 'completed', downloaded_bytes = ?, selected = 1 WHERE id = ?",
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
                selected, download_status, downloaded_bytes, error_message, download_priority, origin)
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
        "selected = 1, error_message = '' WHERE id = ?",
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
        db.set_archive_status(archive_id, "queued")
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
    page = request.args.get("page", 1, type=int)
    sort = request.args.get("sort", "name")
    sort_dir = request.args.get("sort_dir", "")
    search = request.args.get("search", "").strip()
    per_page = int(db.get_setting("files_per_page", "50"))
    files, total = db.get_archive_files(archive_id, page, per_page, sort=sort, sort_dir=sort_dir, search=search)
    unselected = db.count_unselected_files(archive_id)
    progress = db.get_archive_progress(archive_id)
    return jsonify({
        "files": files,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "all_selected": unselected == 0,
        "progress": progress,
    })


@app.route("/api/files/<int:file_id>/select", methods=["POST"])
@login_required
def toggle_file_select(file_id):
    data = request.json
    db.set_file_selected(file_id, data.get("selected", True))
    return jsonify({"ok": True})


@app.route("/api/archives/<int:archive_id>/files/select-all", methods=["POST"])
@login_required
def select_all_files(archive_id):
    data = request.json
    db.set_all_files_selected(archive_id, data.get("selected", True))
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
    download_manager.start()
    return jsonify({"state": download_manager.state})


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
