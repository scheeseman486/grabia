# Grabia Race Condition Analysis

## Architecture Context

Grabia has three concurrent execution contexts:

1. **Flask request threads** — multiple concurrent HTTP handlers (Werkzeug thread pool)
2. **Downloader thread** — single background thread doing downloads, writing DB and emitting SSE events
3. **Scan worker thread** — single background thread for archive scanning
4. **Browser** — single-threaded JS, but SSE events interleave with async API responses

SQLite is used with WAL mode, which allows concurrent readers but only one writer at a time. Each function in `database.py` opens and closes its own connection — there's no connection pooling or shared transactions.

---

## TIER 1 — Bugs That Likely Cause Visible Issues

### 1. `loadFiles()` responses arriving out of order (frontend)

**What happens:** `loadFiles()` is called from 15+ sources (SSE events, pagination, sort clicks, queue toggles, batch operations). Each call fires an async `fetch()`. If two calls overlap, the first response might arrive after the second, and `renderFiles()` will render stale data over fresh data.

**User-visible symptom:** After clicking a sort header or paginating, the file list briefly shows correct data then snaps back to old data. Or SSE-triggered refreshes overwrite a user-initiated page change.

**Fix:** Add a request generation counter. Increment it on every `loadFiles()` call, capture it before the `await`, and after the response check if it's still current — discard stale responses.

```javascript
let loadFilesGeneration = 0;

async function loadFiles() {
    if (!currentArchiveId) return;
    const gen = ++loadFilesGeneration;
    const data = await api("GET", `/api/archives/${currentArchiveId}/files?...`);
    if (gen !== loadFilesGeneration) return; // Stale response, discard
    renderFiles(data);
}
```

### 2. `currentArchiveId` changes while `loadFiles()` is in flight (frontend)

**What happens:** User opens archive A, `loadFiles()` fires. User quickly switches to archive B. Response for archive A arrives and `renderFiles()` displays A's files under B's header.

**User-visible symptom:** Wrong files displayed for the selected archive.

**Fix:** Same generation counter as above — capture `currentArchiveId` before the fetch, verify it hasn't changed after.

### 3. SSE `updateFileRow()` races with `renderFiles()` (frontend)

**What happens:** SSE `file_progress` event calls `updateFileRow()` which directly mutates the DOM. If `loadFiles()` is rebuilding the table at the same time (clearing `innerHTML` then appending rows), `updateFileRow()` either finds no row (silent no-op, losing the update) or briefly shows stale data that gets overwritten.

**User-visible symptom:** Download percentage briefly freezes or jumps backwards during page refreshes.

**Fix:** The generation counter fix from #1 also helps here. Additionally, `updateFileRow()` could be a no-op while a `loadFiles()` is in flight, since the fresh data will include the latest status anyway.

### 4. `recompute_archive_status()` TOCTOU (database.py)

**What happens:** Reads file counts, then writes archive status in a separate statement. Between the read and write, the downloader thread or a Flask request can change file statuses, making the computed status stale.

**User-visible symptom:** Archive shows "completed" when files are still pending, or "idle" when files are queued.

**Fix:** Wrap the read and write in a single transaction with `BEGIN IMMEDIATE`:

```python
def recompute_archive_status(archive_id, fallback=None):
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT COUNT(*)...").fetchone()
        # ... compute status ...
        conn.execute("UPDATE archives SET status = ? WHERE id = ?", (status, archive_id))
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()
```

### 5. Downloader `state` mutations are unprotected (downloader.py)

**What happens:** `self.state` is read and written from both Flask threads (via `start()`, `pause()`, `stop()`, `bandwidth_limit` setter) and the download thread (bandwidth limiting loop). No lock protects these accesses.

**User-visible symptom:** Rare, but could cause the downloader to get stuck in a wrong state — e.g., showing "paused" while actually running, or failing to resume.

**Fix:** All reads and writes of `self.state` should be under `self._lock`. The `bandwidth_limit` setter, `start()`, `pause()`, and `stop()` methods should acquire the lock. The download thread's bandwidth limiting code already has some lock usage but needs to extend it to state changes.

### 6. File rename/delete racing with active download (app.py)

**What happens:** A Flask endpoint renames or deletes a file on disk while the downloader thread is actively writing to that same file.

**User-visible symptom:** Download fails with an IO error, or renamed file ends up incomplete because the downloader kept writing to the old path.

**Fix:** Check `download_manager.skip_current_file(file_id)` before file operations, and wait briefly for the download to actually stop. The skip mechanism already exists — it just needs to be called in rename and delete endpoints too, not just the dequeue endpoint.

---

## TIER 2 — Bugs That Could Cause Issues Under Load

### 7. `_listeners` list modified during iteration (downloader.py)

**What happens:** `add_listener()` appends to `self._listeners` while `_notify()` iterates it from the download thread. If the SSE endpoint's generator is garbage-collected at the right moment, `remove_listener()` modifies the list during iteration.

**Fix:** Use a lock around `_listeners` access, or copy the list before iterating:

```python
def _notify(self, event, data=None):
    for cb in list(self._listeners):  # Iterate a snapshot
        try:
            cb(event, data)
        except Exception:
            pass
```

### 8. `bandwidth_limit` setter data race (downloader.py)

**What happens:** The setter writes `self._bandwidth_limit`, `self._schedule_overridden`, `self._paused_by_bandwidth`, and `self.state` without holding `self._lock`. The download thread reads these same variables in the bandwidth limiting loop without the lock.

**Fix:** The setter should acquire `self._lock` for all state mutations.

### 9. Non-atomic position/priority assignment (database.py)

**What happens:** `add_archive()`, `add_archive_files()`, `add_group()`, `add_processing_profile()` all do `SELECT MAX(position)` then `INSERT ... position = max + 1`. Two concurrent inserts get the same position.

**User-visible symptom:** Archives or files appear in wrong order.

**Fix:** Use `INSERT ... position = (SELECT COALESCE(MAX(position), -1) + 1 FROM ...)` as a single atomic statement, or use `BEGIN IMMEDIATE` transactions.

### 10. Database connections not closed on exceptions (database.py)

**What happens:** Every database function does `conn = get_db() ... conn.close()` without try/finally. Any exception between open and close leaks the connection.

**Fix:** Use context managers:

```python
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

# Usage pattern:
with get_db() as conn:
    ...
```

Or at minimum, wrap every function in try/finally.

### 11. Selection state corrupted by concurrent renders (frontend)

**What happens:** `renderFiles()` prunes `selectedFileIds` by removing IDs not in the visible page. If two renders overlap (one from pagination, one from SSE), the first render prunes the selection based on one page's data, then the second render shows a different page with the selection already pruned.

**Fix:** Don't mutate `selectedFileIds` during render. Instead, maintain it as the authoritative set and just let the checkboxes reflect it. Only clear selection explicitly on user actions (page change, archive switch).

---

## TIER 3 — Edge Cases Unlikely to Cause Issues in Practice

### 12. `_skip_file_event` timing (downloader.py)

The event is cleared at the start of `_download_file()` and set from Flask via `skip_current_file()`. There's a small window where the clear happens just after the set, but in practice the Flask endpoint responds after the set, and by then the download loop has moved on.

### 13. Concurrent scan requests for same archive (app.py)

The `_scan_lock` + `_scan_cancel` dict guards against this, but there's a tiny TOCTOU between the check and the insert. In practice, the lock makes this window so small it's negligible.

### 14. `increment_file_retry()` lost updates (database.py)

SQLite's write lock means `retry_count = retry_count + 1` is atomic at the SQL level. Only one writer can execute at a time in WAL mode. This is safe despite appearances.

### 15. Drag-and-drop corruption by SSE re-renders (frontend)

If `renderArchiveList()` fires during a drag operation, the DOM is rebuilt and the drag breaks. This is annoying but not a data integrity issue. Could be fixed by suppressing SSE-triggered re-renders while a drag is in progress.

---

## Recommended Fix Priority

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| **P0** | #1 + #2: loadFiles() generation counter | Small | Eliminates most visible frontend races |
| **P0** | #4: recompute_archive_status atomicity | Small | Fixes stale archive status |
| **P1** | #5: Downloader state under lock | Medium | Prevents stuck downloader |
| **P1** | #6: Skip download before rename/delete | Small | Prevents IO errors |
| **P1** | #10: DB connection leak protection | Medium | Prevents resource exhaustion |
| **P2** | #7: Listener list snapshot | Trivial | Prevents rare crash |
| **P2** | #8: Bandwidth setter lock | Small | Prevents bandwidth glitches |
| **P2** | #9: Atomic position inserts | Small | Prevents ordering bugs |
| **P2** | #11: Selection state preservation | Small | Prevents lost selections |
| **P3** | #3: updateFileRow during render | Trivial | Already mitigated by #1 |
| **P3** | #15: Drag suppression | Small | QoL improvement |

## Implementation Plan — COMPLETED

### Phase 1 — Quick wins (P0) ✓

1. ✓ Added `loadFilesGen` and `refreshArchivesGen` generation counters with stale-response guards in app.js
2. ✓ Wrapped `recompute_archive_status()` and `recompute_archive_file_count()` in `BEGIN IMMEDIATE` transactions in database.py
3. ✓ Added `download_manager.skip_current_file(file_id)` calls in rename and delete endpoints in app.py

### Phase 2 — Lock discipline (P1) ✓

4. ✓ Extended `self._lock` in downloader.py to cover all `self.state` mutations in `start()`, `pause()`, `stop()`, `bandwidth_limit` setter/getter, and the bandwidth loop in `_do_download()`. Notifications are sent outside the lock to prevent deadlocks.
5. ✓ Added `_db()` context manager to database.py, converted ~40 functions to use it for guaranteed connection cleanup. Functions with explicit `BEGIN IMMEDIATE` transactions (`recompute_*`) keep their manual try/except/finally.

### Phase 3 — Polish (P2-P3) ✓

6. ✓ `_notify()` now iterates a snapshot copy of `self._listeners`. `add_listener()`/`remove_listener()` also use the lock.
7. ✓ `add_archive()`, `add_group()`, `add_processing_profile()` use atomic `INSERT ... (SELECT MAX + 1)` subqueries. `add_archive_files()` uses `BEGIN IMMEDIATE` to protect the priority sequence.
8. ✓ Removed `selectedFileIds` pruning from `renderFiles()`. Selection is now cleared only on explicit user actions (archive switch, close detail).
9. ✓ Added `isDragging` guard to `renderArchiveList()` — defers re-renders during drag operations and replays them on `dragend`.
