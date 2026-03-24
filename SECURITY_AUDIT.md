# Grabia Security Audit

**Date:** 2026-03-24
**Scope:** Full application — app.py, database.py, downloader.py, processors.py, ia_client.py, static/js/app.js, logger.py

---

## Critical Findings

### 1. Archive Extraction Path Traversal (processors.py)

**Severity: CRITICAL**

`_extract_zip` (line 223) calls `zipfile.ZipFile.extractall()` with no path validation. A malicious ZIP file containing entries like `../../../etc/cron.d/backdoor` will write files outside the intended extraction directory. The same issue affects `_extract_7z` (line 231) via `py7zr.SevenZipFile.extractall()`.

The downstream consumer `ExtractProcessor.process` (line ~1178) then does `shutil.move(src, dest)` where `rel` comes from the archive's internal listing — again with no check that `dest` stays inside `dest_dir`.

**Impact:** An attacker who can get a crafted archive onto Internet Archive (or who controls the download source) can write arbitrary files anywhere the Grabia process has write permission. This is remote code execution via cron, `.bashrc`, or similar persistence vectors.

**Fix:** Before extracting, iterate `zf.namelist()` / `zf.infolist()` and reject entries containing `..` segments or absolute paths. After extraction, validate every output path with `os.path.realpath()` + `startswith(dest_dir)`. The same check is needed for the `shutil.move()` in `ExtractProcessor`.

---

### 2. Configurable Tool Paths Enable Arbitrary Command Execution (processors.py + app.py)

**Severity: CRITICAL**

The settings endpoint (app.py line 237-243) allows an authenticated user to set `tool_chdman_path`, `tool_maxcso_path`, `tool_7z_path`, and `tool_unrar_path` to arbitrary filesystem paths. These are later passed directly to `subprocess.run()` (processors.py lines 238, 256, 659, 670, 867, etc.).

The only validation in `_find_binary()` (processors.py line 76-79) is `os.path.isfile()` and `os.access(custom, os.X_OK)` — meaning an attacker who gains access to any authenticated session can point a tool path at any executable on the system, like `/bin/bash`, and trigger it via the processing pipeline.

**Impact:** Any authenticated user can execute arbitrary commands as the Grabia process user. In a multi-user or exposed deployment, this is a privilege escalation / RCE vector.

**Fix:** Either remove configurable tool paths entirely (only allow binaries found on `$PATH`), or validate that tool paths resolve to expected binary names (e.g. the basename must be `7z`, `unrar`, etc.) and don't follow symlinks to unexpected locations.

---

### 3. Debug Log File Path Allows Arbitrary File Write (logger.py + app.py)

**Severity: HIGH**

The `debug_log_file` setting (app.py line 243) is user-configurable and flows directly into `logging.FileHandler(log_file)` (logger.py line 78) after only an `os.makedirs()` on its parent directory (line 77). There is no path validation.

An authenticated user can set `debug_log_file` to any writable path (e.g. `/home/user/.bashrc`, a cron directory, or an SSH authorized_keys file), and then trigger debug log output to overwrite or append to that file.

**Impact:** Arbitrary file write as the Grabia process user, leading to code execution.

**Fix:** Validate that the log file path stays within the data directory, or restrict it to a fixed location.

---

### 4. CUE Sheet Parsing Path Traversal (processors.py)

**Severity: HIGH**

`_parse_cue_bins` (line 332-344) extracts `FILE` references from CUE sheets and joins them with `os.path.join(cue_dir, bin_name)` without validating that the result stays within the archive directory. A malicious CUE file referencing `FILE "../../../../etc/shadow" BINARY` would cause the processor to read and potentially operate on files outside the intended scope.

**Fix:** Apply `os.path.realpath()` and validate the result stays within `base_dir`.

---

## High Findings

### 5. No CSRF Protection

**Severity: HIGH**

No CSRF tokens are generated or validated on any endpoint. All state-changing operations (settings changes, archive manipulation, download control, file deletion) are vulnerable to cross-site request forgery.

While the JSON `Content-Type` requirement provides a partial mitigation (browsers won't send `application/json` cross-origin without CORS preflight), this is bypassable via Flash, certain browser extensions, and `navigator.sendBeacon()` with a Blob.

**Fix:** Add Flask-WTF or a custom CSRF token scheme. At minimum, validate the `Origin` or `Referer` header on state-changing requests.

---

### 6. No Login Rate Limiting

**Severity: HIGH**

The login endpoint (app.py line 146) has no rate limiting, lockout, or delay mechanism. An attacker can make unlimited password guesses at network speed. Combined with the 4-character minimum password policy, brute-force is feasible.

**Fix:** Add exponential backoff or account lockout after N failed attempts. Consider Flask-Limiter.

---

### 7. IA Credentials Stored in Plaintext in Database

**Severity: HIGH**

Internet Archive email and password are stored as plaintext in the `settings` table (app.py lines 237-250, database.py `set_setting`). While the Grabia password is properly hashed via Werkzeug's `generate_password_hash`, the IA credentials that grant access to the user's Internet Archive account are stored in the clear.

The GET settings endpoint (line 224-227) redacts the password in API responses, but the database itself is unprotected — anyone with file access to `grabia.db` gets the IA credentials.

**Fix:** Encrypt IA credentials at rest using a key derived from the app secret, or use a proper secrets manager.

---

### 8. Session Cookie Security Not Configured

**Severity: HIGH (when exposed to network)**

No session cookie flags are explicitly set:
- `SESSION_COOKIE_SECURE` is not set (defaults False) — cookies sent over HTTP
- `SESSION_COOKIE_SAMESITE` is not set (defaults to Flask's `"Lax"` in recent versions, but this should be explicit)
- `SESSION_COOKIE_HTTPONLY` relies on Flask default (True, which is fine)
- `PERMANENT_SESSION_LIFETIME` is not configured — permanent sessions use Flask's default of 31 days

**Fix:** Set `SESSION_COOKIE_SECURE = True` when running behind HTTPS, set `SESSION_COOKIE_SAMESITE = "Lax"` or `"Strict"` explicitly, and configure a reasonable session lifetime.

---

### 9. Unclosed Database Connection in delete_file Endpoint (app.py)

**Severity: MEDIUM** (reliability, not security per se — but contributes to DoS surface)

The `delete_file` endpoint (app.py lines 1044-1051) opens a raw `db.get_db()` connection without using the `_db()` context manager, so if an exception occurs between `get_db()` and `conn.close()`, the connection leaks. Same issue in `delete_processed_file` (lines 1172-1179, 1205-1226).

**Fix:** Use `with _db() as conn:` or refactor these into database.py functions.

---

## Medium Findings

### 10. Download Directory Accepts Arbitrary Paths (app.py)

The `download_dir` setting accepts any path without validation. An authenticated user could set it to a sensitive system directory, causing subsequent downloads to write files into that directory. While the downloaded content comes from Internet Archive (not directly user-controlled), filenames within IA items could be crafted to overwrite sensitive files.

**Fix:** Validate that `download_dir` is within an allowed parent directory, or at minimum ensure it doesn't point to system directories.

---

### 11. Filename Validation Relies Solely on realpath Containment

File rename (app.py line 985), delete (line 1011), and processed-rename (line 1256) all use the `os.path.realpath(path).startswith(base_dir + os.sep)` pattern. While this is generally effective, it has edge cases: if `base_dir` is a symlink, `realpath` resolves it, and the `startswith` check may pass or fail depending on the symlink's target. Additionally, null bytes in filenames on certain systems could truncate the path check.

**Fix:** Also reject filenames containing `..`, null bytes, and path separators as a defense-in-depth measure. Add explicit `\0` checks.

---

### 12. SSE Endpoint Has No Connection Limit

The SSE endpoint (app.py line 188) creates a new `queue.Queue(maxsize=200)` for every connected client with no limit on concurrent connections. An attacker with a valid session could open hundreds of SSE connections to exhaust server memory.

**Fix:** Add a per-session or per-IP limit on concurrent SSE connections.

---

### 13. Integer Parsing Without Error Handling (app.py)

`int(data["bandwidth_limit"])` (line 257) and `int(data.get("limit", -1))` (line 1457) will throw `ValueError` on non-numeric input, returning a 500 to the client. While not exploitable, it reveals implementation details in error responses.

**Fix:** Wrap in try/except and return a 400.

---

### 14. Symlink Attacks in Extraction (processors.py)

No code in processors.py checks for symlinks. Malicious archives can contain symlinks that point outside the extraction directory. After extraction, subsequent file operations follow symlinks, potentially reading or writing arbitrary locations.

**Fix:** After extraction, walk the output directory and reject or remove any symlinks. Use `os.lstat()` instead of `os.stat()` where appropriate.

---

## Low Findings

### 15. Weak Password Policy

4-character minimum (app.py lines 129, 171) is very permissive. No complexity requirements.

### 16. No Audit Logging

Login attempts, setting changes, file deletions, and processing operations are not logged to an audit trail. Failed login attempts are silently discarded.

### 17. Error Messages May Leak Information

HTTP errors from IA downloads (app.py/downloader.py) are stored in the database and displayed to the user, potentially revealing internal network topology or server details.

### 18. Single-User Authentication Model

The auth table is hardcoded to a single row (`id = 1`). No role separation, no multi-user support, no ability to revoke individual sessions.

### 19. JSON.stringify in HTML Data Attribute (app.js line 1525)

`data-conflict-file='${JSON.stringify({...})}'` embeds `f.error_message` without HTML-escaping the JSON string. While JSON.stringify provides some protection, a carefully crafted error message containing single quotes could break out of the attribute. Defense-in-depth recommends HTML-escaping the JSON string.

### 20. XSS via IA Metadata (Theoretical)

If Internet Archive's API were compromised and returned malicious metadata (titles, descriptions), the server trusts this data. The frontend properly escapes via `escapeHtml()` in almost all cases, so actual exploitation would require finding an unescaped interpolation point. Current coverage appears complete, but this is worth noting as a trust boundary.

---

## Architecture Observations

**What's done well:**
- Path traversal checks using `os.path.realpath()` + `startswith()` are applied consistently across file operations in app.py and downloader.py
- The frontend consistently uses `escapeHtml()` for user-controlled content in innerHTML
- No `shell=True` in any subprocess call
- Secret key generation uses `os.urandom(32)` with proper file permissions
- IA password is redacted in API responses
- SQL parameterization is used for values (the `?` placeholders)
- Subprocess calls use list-form arguments (immune to shell injection)

**Systemic concerns:**
- The application assumes all authenticated users are fully trusted (tool paths, download dir, log file path). This is fine for single-user local use but dangerous if ever exposed to a network with multiple users.
- The processors subsystem is the weakest link — it processes untrusted archive contents (from IA) with insufficient sandboxing. The extraction path traversal (#1) is the single most dangerous finding.
- Database connections opened directly via `db.get_db()` in app.py (outside the `_db()` context manager) are a maintenance risk — any new code following this pattern will leak connections on exceptions.
