# Overlay Database Plan

## Problem

The `archive_files` table currently serves two purposes: mirroring the Internet Archive manifest and tracking Grabia's local processing state. Processing columns (`processing_status`, `processed_filename`, `processor_type`, `processing_error`, `processed_files_json`) are bolted onto manifest rows, which conflates "this original file has been processed" with the processed outputs themselves. Locally imported files also live in `archive_files` alongside manifest entries, muddying the source-of-truth for what came from IA vs what Grabia created.

## Design Principle

**`archive_files` is a read-only mirror of the IA manifest.** It only contains rows with `origin = 'manifest'`. Grabia never inserts, deletes, or modifies these records beyond syncing download state with the remote manifest.

**Everything Grabia creates goes in the overlay.** Processed outputs, locally imported files, and any future user-created file associations live in a separate `local_files` table.

**The on-disk layout mirrors the database separation.** `downloads/` contains only IA originals. `processed/` is a flat overlay with the same `identifier/` structure — processed and local files sit directly in `processed/{identifier}/`, no nested `.processed` subfolders.

## Schema Changes

### `archive_files` — Cleaned Up

Remove processing columns entirely. Rename statuses for clarity.

**Columns retained (unchanged):**
- `id`, `archive_id`, `name`, `size`, `md5`, `sha1`, `format`, `source`, `mtime`
- `downloaded_bytes`, `error_message`, `retry_count`
- `change_status`, `change_detail`
- `queue_position`, `downloaded`
- `media_root`

**Columns modified:**
- `download_status` — rename value `completed` → `downloaded`
- `origin` — always `'manifest'` (no more `'scan'`/`'local'` values)

**Columns removed** (migrate data first, then stop writing to them; SQLite can't drop columns easily so they stay in the schema but are ignored):
- `processing_status`
- `processed_filename`
- `processor_type`
- `processing_error`
- `processed_files_json`

### New: `local_files` (The Overlay)

```sql
CREATE TABLE IF NOT EXISTS local_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL,
    source_file_id INTEGER DEFAULT NULL,   -- NULL for imports, FK to archive_files for processed outputs
    name TEXT NOT NULL,                     -- relative path within processed/{identifier}/ (e.g. "GameName.chd")
    size INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'local',   -- 'processed' or 'local'
    processor_type TEXT NOT NULL DEFAULT '',
    processing_job_id INTEGER DEFAULT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
    FOREIGN KEY (source_file_id) REFERENCES archive_files(id) ON DELETE SET NULL,
    FOREIGN KEY (processing_job_id) REFERENCES processing_jobs(id) ON DELETE SET NULL
);

CREATE INDEX idx_lf_archive ON local_files(archive_id);
CREATE INDEX idx_lf_source ON local_files(source_file_id);
CREATE INDEX idx_lf_origin ON local_files(origin);
```

**Key relationships:**
- A processed output has `source_file_id` pointing to the original manifest file, and `origin = 'processed'`
- A locally imported file has `source_file_id = NULL` and `origin = 'local'`
- One source file can produce multiple processed outputs (1:N)
- A local file belongs to an archive but has no IA manifest counterpart

### Processing Queue State

The transient processing lifecycle (queued → processing → done/failed) still needs to be tracked somewhere. Since this is about queue state, not file identity, it stays on `archive_files` as a lightweight column:

```sql
-- New column on archive_files (replaces processing_status)
process_queue_status TEXT NOT NULL DEFAULT ''
-- Values: '' (idle), 'queued', 'processing', 'failed', 'skipped'
-- 'processed' is NO LONGER a status — it's implied by local_files rows
```

When processing completes successfully, `process_queue_status` is cleared back to `''` and the output is recorded as a `local_files` row. The "has been processed" question becomes: `SELECT 1 FROM local_files WHERE source_file_id = ? AND origin = 'processed'`.

## Tags (Virtual, Not Stored)

Status-derived tags are computed at query time in the segment evaluator, not stored in the tags table. This eliminates the sync problem entirely.

| Tag | Derived From |
|-----|-------------|
| `original` | Row exists in `archive_files` (always true for manifest files) |
| `downloaded` | `archive_files.download_status = 'downloaded'` |
| `processed` | Has rows in `local_files WHERE origin = 'processed'` |
| `local` | Row in `local_files WHERE origin = 'local'` |
| `archive:<id>` | `archives.identifier` (as now) |
| `group:<name>` | Group membership (as now) |

Content-derived tags (parsed from filenames, user-assigned tags) remain stored in `file_tags` / `archive_tags` as they are now.

## File on Disk Layout

The on-disk layout mirrors the database separation. `downloads/` is the IA manifest. `processed/` is a flat overlay.

```
downloads/                              ← IA manifest mirror (archive_files)
  xbox-redump-1/
    Game (USA).zip                      ← original, tracked in archive_files
    Another Game (USA).zip

processed/                              ← flat overlay (local_files)
  xbox-redump-1/
    Game (USA).chd                      ← processed output from Game (USA).zip
    Game (USA).cue                      ← additional output from same source
    Imported Game.iso                   ← locally imported, no source_file_id

collections/                            ← generated symlink trees
  Xbox/
    Game (USA).chd → ../../processed/xbox-redump-1/Game (USA).chd
```

No nested `.processed` subfolders. Processed outputs sit directly in `processed/{identifier}/` alongside any local imports for that archive. This keeps the overlay structure simple and predictable — the same flat layout as `downloads/`.

## Migration

### Database Migration (automatic on startup)

#### Step 1: Create `local_files` table

Run the `CREATE TABLE` above.

#### Step 2: Migrate processed outputs

For every `archive_files` row where `processing_status = 'processed'`:

1. Insert the primary output into `local_files`:
   ```sql
   INSERT INTO local_files (archive_id, source_file_id, name, origin, processor_type, created_at)
   VALUES (af.archive_id, af.id, af.processed_filename, 'processed', af.processor_type, <now>)
   ```

2. Parse `processed_files_json` and insert additional outputs:
   ```sql
   -- For each entry in the JSON array that differs from processed_filename:
   INSERT INTO local_files (archive_id, source_file_id, name, origin, processor_type, created_at)
   VALUES (af.archive_id, af.id, <json_entry>, 'processed', af.processor_type, <now>)
   ```

The `name` values stored are the final flat relative paths (just the filename, no `.processed/` prefix). If current paths contain `.processed/` subfolder prefixes, strip them during migration.

#### Step 3: Migrate local files

For every `archive_files` row where `origin = 'scan'` (currently meaning locally found files):

```sql
INSERT INTO local_files (archive_id, source_file_id, name, size, origin, created_at)
VALUES (af.archive_id, NULL, af.name, af.size, 'local', <now>)
```

Then delete these rows from `archive_files` (they don't belong in the manifest mirror).

#### Step 4: Rename statuses

```sql
UPDATE archive_files SET download_status = 'downloaded' WHERE download_status = 'completed';
UPDATE archive_files SET origin = 'manifest' WHERE origin = 'scan';
```

#### Step 5: Add process_queue_status

```sql
ALTER TABLE archive_files ADD COLUMN process_queue_status TEXT NOT NULL DEFAULT '';
-- Copy transient states only (not 'processed' which is now in local_files)
UPDATE archive_files SET process_queue_status = processing_status
    WHERE processing_status IN ('queued', 'processing', 'failed', 'skipped');
```

#### Step 6: Stop writing to old columns

Code changes ensure nothing writes to `processing_status`, `processed_filename`, etc. on `archive_files` anymore. The columns remain in the schema but are dead.

### File Migration (user-triggered via Settings)

The existing "Migrate to .processed Folders" button in Settings → Debug is repurposed to flatten the on-disk layout. The new migration:

1. Scans `processed/{identifier}/` directories for `.processed` subfolders
2. Moves all files out of `.processed` subfolders into the parent `{identifier}/` directory
3. Removes the now-empty `.processed` subfolders
4. Updates `local_files.name` to reflect the flattened paths
5. Also moves any processed files still in `downloads/` into `processed/` (legacy cleanup)

This is idempotent — files already in the flat layout are skipped. The button label changes to "Flatten Processed Files" or similar.

**Path resolution during transition:** `_resolve_processed_path()` checks for the file in the flat layout first (`processed/{identifier}/{name}`), then falls back to the `.processed` subfolder layout (`processed/{identifier}/{source}.processed/{name}`), then legacy location (`downloads/{identifier}/{name}`). This means everything works before, during, and after file migration.

## Impact on Existing Systems

### Processing Worker (`processing_worker.py`)

**Before:** `set_file_processing_status(file_id, "processed", processed_filename=..., processed_files=...)`

**After:**
1. `set_file_process_queue_status(file_id, "")` — clear queue state
2. `add_local_file(archive_id, source_file_id=file_id, name=output_name, origin="processed", ...)` — for each output file

Processor output_dir remains `processed/{identifier}/` — outputs land directly there, no `.processed` subfolder created.

### Collection Sync (`collection_sync.py`)

**`get_collection_files()`** becomes a UNION query:

```sql
-- Original manifest files (downloaded)
SELECT af.id, af.archive_id, af.name, af.size, 'original' AS file_type,
       a.identifier AS archive_identifier
FROM archive_files af
JOIN archives a ON af.archive_id = a.id
WHERE af.download_status = 'downloaded'

UNION ALL

-- Processed outputs and local imports
SELECT lf.id, lf.archive_id, lf.name, lf.size,
       lf.origin AS file_type,
       a.identifier AS archive_identifier
FROM local_files lf
JOIN archives a ON lf.archive_id = a.id
```

The `file_type` column feeds into virtual tag computation: `original`, `processed`, or `local`.

**`_build_file_tag_lookup()`** adds virtual tags based on `file_type` instead of checking status columns.

**`_resolve_filepath()`** checks `file_type`:
- `original` → `download_dir/identifier/name`
- `processed` or `local` → `processed_dir/identifier/name` (with fallback chain for legacy layouts)

### File List API (`app.py`)

**`get_archive_files()`** joins with `local_files` to show processing state:

```sql
SELECT af.*,
       EXISTS(SELECT 1 FROM local_files lf
              WHERE lf.source_file_id = af.id AND lf.origin = 'processed') AS has_processed,
       (SELECT GROUP_CONCAT(lf.name, '|') FROM local_files lf
        WHERE lf.source_file_id = af.id AND lf.origin = 'processed') AS processed_outputs
FROM archive_files af
WHERE af.archive_id = ?
```

The effective status expression simplifies:

```sql
CASE
    WHEN process_queue_status = 'processing' THEN 'processing'
    WHEN process_queue_status = 'queued' THEN 'queued'
    WHEN process_queue_status = 'failed' THEN 'failed'
    WHEN process_queue_status = 'skipped' THEN 'skipped'
    WHEN EXISTS(SELECT 1 FROM local_files WHERE source_file_id = af.id AND origin = 'processed') THEN 'processed'
    WHEN queue_position IS NULL AND download_status = 'pending' THEN 'skipped'
    ELSE download_status
END
```

### Archive Progress (`database.py`)

`recompute_archive_status()` and `get_archive_progress()` currently count `processing_status = 'processed'` toward "completed" files. This changes to:

```sql
SUM(CASE WHEN af.download_status = 'downloaded'
         OR EXISTS(SELECT 1 FROM local_files lf
                   WHERE lf.source_file_id = af.id AND lf.origin = 'processed')
    THEN 1 ELSE 0 END) AS completed
```

### Auto-Tagger

File-level auto tags (`processed`, `original`) become unnecessary since these are now virtual tags derived at query time. The auto-tagger only needs to handle content-derived tags (filename parsing) and archive-level tags (`archive:`, `group:`).

### Scan System

The scan currently creates `origin = 'scan'` rows in `archive_files` for files found on disk that aren't in the manifest. Post-migration, these would be inserted into `local_files` with `origin = 'local'` instead.

## Order of Implementation

1. Create `local_files` table + indexes
2. Write DB migration (Steps 2-5 above, runs automatically on startup)
3. Update `processing_worker.py` to write to `local_files` and output directly to `processed/{identifier}/`
4. Update `database.py` queries (progress, status, file list)
5. Update `collection_sync.py` (file list, tag lookup, path resolution with fallback chain)
6. Update `app.py` endpoints
7. Rewrite the file migration endpoint to flatten `.processed` subfolders and move legacy files from `downloads/` to `processed/`
8. Update frontend file list display
9. Update auto-tagger to remove status-based tags
10. Update scan system to write local files to overlay
