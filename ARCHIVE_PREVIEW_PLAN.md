# Archive Contents Preview & Pipeline Refactor

## Overview

Integrate Internet Archive's `view_archive.php` endpoint to fetch and display the contents of compressed files (zip, 7z) **before download**, and refactor the processing pipeline to split extraction from transformation. This turns compressed archives into browsable, nested file trees in the Grabia UI and aligns the database model so preview contents and extracted contents are a 1:1 match.

---

## 1. IA's `view_archive.php` — What We Know

### Endpoint

Lives on IA storage nodes. Not officially documented, but stable and used by the IA web UI itself.

**Listing contents:**
```
https://{server}/view_archive.php?archive={dir}/{filename}
```
Or via the canonical download URL with a trailing slash:
```
https://archive.org/download/{identifier}/{archive_file}/
→ 302 → view_archive.php on the correct storage node
```

**Extracting a single file:**
```
https://archive.org/download/{identifier}/{archive_file}/{inner_path}
→ 302 → view_archive.php?archive=...&file={inner_path}
→ raw file content
```

### Response Format

- **HTML only** — `&output=json` does not work.
- File entries appear as `<a>` tags with adjacent text for timestamp and size.
- Each entry has: filename (link), timestamp (`YYYY-MM-DD HH:MM:SS`), size in bytes.
- Directories indicated by trailing `/`.
- Does **not** support recursive queries — cannot list contents of a zip inside a zip remotely.

### Metadata API (`/metadata/{identifier}`)

Returns `filecount` on compressed files (e.g., `"filecount": "21"`) but **not** the inner file listing. `view_archive.php` is the only source for the actual inventory.

### What We Have

`ia_client.py` already stores `server` and `dir` from the metadata response — exactly the components needed to construct `view_archive.php` URLs without the redirect round-trip.

### Supported Archive Types

Confirmed: `.zip`, `.7z`. Likely also: `.rar`, `.tar`, `.tar.gz`/`.tgz`. Needs testing.

### Hashes

`view_archive.php` does **not** return file hashes. The IA metadata API provides hashes for top-level item files only, not for files inside archives. File matching for scan operations will need to rely on filename + size, or compute hashes after extraction.

### Nested Archives (zip-in-zip)

`view_archive.php` does **not** support recursive queries. To discover the contents of a nested archive (e.g., `sonic cd (usa).zip` inside `mega cd.zip`), we need the outer archive on disk. However, this does **not** require extraction — Python's `zipfile` and `py7zr` libraries can list the contents of a compressed file without extracting it. The strategy is:

1. **Before download:** `view_archive.php` gives us the first level only (e.g., `mega cd.zip` contains `sonic cd (usa).zip`).
2. **After download:** When the outer archive lands on disk, inspect it locally to discover nested archive contents. Use `zipfile.ZipFile.namelist()` / `py7zr.SevenZipFile.getnames()` to peek inside inner archives without writing anything to disk.
3. **After extraction:** If a nested zip is extracted to disk, inspect it the same way to discover its contents.

This means the file tree fills in progressively: first level from IA, deeper levels as files arrive on disk.

---

## 2. Data Model — Archives as Folders

### Core Concept

Compressed files are treated as **virtual folders** in the file tree. Their contents are first-class `archive_files` rows that happen to have a parent file. This means:

```
v mega cd.zip                        ← archive_files row (origin='manifest')
  v sonic cd (usa).zip               ← archive_files row (origin='archive_content', parent_file_id → mega cd.zip)
    | sonic cd (usa).bin             ← archive_files row (origin='archive_content', parent_file_id → sonic cd.zip)
    | sonic cd (usa).cue             ← archive_files row (origin='archive_content', parent_file_id → sonic cd.zip)
```

### New Column: `parent_file_id`

Add to `archive_files`:

```sql
ALTER TABLE archive_files ADD COLUMN parent_file_id INTEGER DEFAULT NULL
    REFERENCES archive_files(id) ON DELETE CASCADE;
```

- `NULL` = top-level file (from IA manifest, as today).
- Non-NULL = file lives inside the referenced archive/zip.
- Enables arbitrary nesting (zip inside zip inside zip).

### New Origin Value: `'archive_content'`

Files discovered via `view_archive.php` get `origin = 'archive_content'`. This distinguishes them from `'manifest'` (IA top-level files) and `'local'` (overlay/processed files).

### File Status for Contained Files

A file inside a compressed archive needs a status that reflects **its host's state**, not its own download state. New `download_status` values:

| Status | Meaning |
|--------|---------|
| `'contained'` | File exists inside an archive that has **not** been queued for download. Inert — visible in the tree, but no action taken. |
| `'contained_queued'` | Host archive is queued/downloading — this file will become available after extraction. |
| `'extracted'` | File has been extracted from its host archive and exists on disk. |

**Why `contained`**: "internal" and "compressed" are ambiguous. `contained` clearly says "I am inside something else" without implying anything about compression format. It pairs naturally with `extracted` as its transition state.

When the host file's download status changes, a cascade update sets contained files' statuses:
- Host queued → contained files become `contained_queued`
- Host downloaded → contained files stay `contained_queued` (extraction hasn't happened yet)
- Host extracted (via processing) → contained files become `extracted`
- Host skipped/removed → contained files revert to `contained`

### Tag Inheritance

Tags follow the containment hierarchy: **group → archive → file → contained files → processed outputs**.

When a file is inside a zip, it inherits:
1. The archive's tags (e.g., `platform:playstation`)
2. The host file's tags (e.g., `region:usa`)
3. Its own tags (can be manually added or auto-tagged)

This is already partially supported via `archive_tags` and `file_tags`. The extension is: when resolving tags for a contained file, walk up `parent_file_id` to collect inherited tags. Virtual tag computation (`get_virtual_tags_for_file`) already handles `original`, `downloaded`, `processed`, `local` — add `contained` and `extracted` to this set.

### Extracted & Processed File Relationship

Both extracted files and processed outputs live in `local_files` (the overlay table). `archive_files` is reserved for data sourced from IA (manifest files + archive contents discovered via `view_archive.php` or local inspection). `local_files` is for everything that exists on disk as a result of local operations.

```
archive_files layer (what IA knows about):
v mega cd.zip                                    archive_files (manifest)
  v sonic cd (usa).zip                           archive_files (archive_content)
    | sonic cd (usa).bin                         archive_files (archive_content)
    | sonic cd (usa).cue                         archive_files (archive_content)

local_files layer (what's on disk from local operations):
  v mega cd.zip.extracted
    v sonic cd (usa).zip.extracted
      | sonic cd (usa).bin                       local_files (processor_type='extract', source → zip row)
      v sonic cd (usa).cue                       local_files (processor_type='extract', source → zip row)
        | sonic cd (usa).chd                     local_files (processor_type='chd_cd', source → cue row)
```

The `local_files.source_file_id` foreign key points to the `archive_files` row the file was derived from. For extracted files, this points to the host archive; for processed files, this points to the input file.

On disk, both extracted and processed files live under:
```
processed/{identifier}/
```

### Multi-Input Processing

Some processors (e.g., CHD from bin+cue) use one file as the invocation input (the cue) but logically depend on a sibling (the bin). The current `local_files.source_file_id` only tracks a single source, which loses the bin dependency. A junction table makes all relationships explicit:

```sql
CREATE TABLE local_file_sources (
    local_file_id INTEGER NOT NULL REFERENCES local_files(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES archive_files(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'primary',  -- 'primary' = invocation input, 'dependency' = required sibling
    UNIQUE(local_file_id, source_file_id)
);
```

For the CHD-from-bin+cue example:

| local_file (chd) | source_file | role |
|-------------------|-------------|------|
| sonic cd (usa).chd | sonic cd (usa).cue | `primary` |
| sonic cd (usa).chd | sonic cd (usa).bin | `dependency` |

This replaces the single `source_file_id` FK on `local_files` — the existing column stays for backwards compatibility and quick lookups (always points to the `primary` source), but the junction table is the authoritative record.

Benefits:
- Accurate dependency tracking — deleting the bin flags the chd as having a broken dependency.
- Processors can declare their input/dependency files explicitly in their result dict.
- The UI can show "derived from: cue + bin" rather than just "derived from: cue".

---

## 3. Fetching Archive Contents from IA

### New Function: `ia_client.fetch_archive_contents()`

```python
def fetch_archive_contents(identifier, filename, server=None, dir_path=None):
    """Fetch the file listing inside a compressed archive from IA.
    
    Returns list of dicts:
        [{"name": "sonic cd.bin", "size": 640000, "mtime": "1996-12-24 17:32:00", "is_dir": False}, ...]
    """
```

**Implementation:**
1. Construct URL using `server` and `dir` (avoids redirect): `https://{server}/view_archive.php?archive={dir}/{filename}`
2. Fallback to canonical URL if server/dir unavailable: `https://archive.org/download/{identifier}/{filename}/`
3. Parse HTML response — extract `<a>` tags and adjacent timestamp/size text.
4. Return structured list.

This only yields the **first level** of contents. Nested archives (zip-in-zip) are discovered later via local inspection once the outer archive is downloaded (see Section 1, "Nested Archives").

### When to Fetch / Inspect

| Trigger | Source | Depth |
|---------|--------|-------|
| Archive add / metadata refresh | `view_archive.php` (remote) | First level only — contents of top-level zips |
| Archive file downloaded | Local inspection (`zipfile`/`py7zr`) | Nested levels — peek inside inner archives without extracting |
| File extracted from archive | Local inspection | Same — any newly-on-disk archive is inspected for contents |
| On demand (user expand) | Remote or local, whichever is available | Lazy-load if not yet fetched |

All of these queue as background tasks (see Section 6) since they may be slow for large archives. Progress communicated via the existing notification system.

### Staleness & Hash Check

IA metadata includes hashes (md5, sha1) for top-level files. When refreshing metadata:
1. Compare the stored hash for each zip/7z/rar against the new metadata hash.
2. If the hash matches, skip re-fetching contents — they're identical.
3. If the hash differs (IA item was updated), re-fetch and diff against existing `archive_content` rows: insert new, remove stale, update changed.

This avoids unnecessary `view_archive.php` requests on routine metadata refreshes.

### Storage

Archive contents are stored as `archive_files` rows with `origin='archive_content'` and `parent_file_id` set. This means they participate in all existing queries (collections, tags, search) without special-casing.

The raw HTML response is **not** cached — the structured rows in the database are the cache. A "refresh archive contents" action can re-fetch and diff.

---

## 4. Processing Pipeline Refactor

### Current State

`ExtractProcessor.process()` does everything in one shot:
1. Extract archive to temp dir
2. Move files to processed dir (flattened)
3. Return `files_created` and `files_to_delete`

The processing worker then records all outputs in `local_files` at once. If the user cancels mid-way, nothing is recorded.

### Target State — Split Extract + Transform

The processing pipeline becomes a sequence of discrete **steps**:

#### Step 1: Extract

**Input:** Downloaded archive file (e.g., `mega cd.zip`)
**Output:** Extracted files on disk, database updated with `extracted` status

- Extract contents to `processed/{identifier}/`
- For each extracted file, update the corresponding `archive_files` row:
  - `download_status = 'extracted'`
  - `downloaded = 1`
- If the user cancels here, the extracted files are on disk and tracked in the DB. The user can see them in the file list and manually process them later.

#### Step 2: Transform (optional, per-profile)

**Input:** Extracted file(s) (e.g., `sonic cd (usa).cue` + `.bin`)
**Output:** Processed file (e.g., `sonic cd (usa).chd`)

- Run the configured processor (CHD, CISO, etc.)
- Record output in `local_files` with `source_file_id` → the primary input file
- Insert rows into `local_file_sources` for all inputs: primary (cue) + dependencies (bin)
- The source file's extracted status remains — it's still on disk

#### How This Looks in the Job Queue

A single processing job for `mega cd.zip` with a CHD profile would produce two queue entries:

| Step | Type | Input | Output |
|------|------|-------|--------|
| 1 | `extract` | `mega cd.zip` | `sonic cd (usa).bin`, `sonic cd (usa).cue` |
| 2 | `chd_cd` | `sonic cd (usa).cue` | `sonic cd (usa).chd` |

Step 2 is auto-generated after step 1 completes, based on the processing profile's input extensions matching against the extracted files.

### Processing Profile Changes — Pipeline Profiles

A profile becomes an **ordered list of steps** rather than a single processor. Each step has its own processor type and options. The worker executes them in sequence, feeding outputs from one step as inputs to the next.

#### Schema Change

```sql
-- Replace the flat processor_type/options_json on processing_profiles
-- with a child table of ordered steps.

CREATE TABLE processing_profile_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES processing_profiles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    processor_type TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(profile_id, position)
);
```

The existing `processing_profiles` table keeps `name` and `position` but drops `processor_type` and `options_json` (moved to steps). For backwards compatibility during migration, existing single-step profiles get one row in `processing_profile_steps`.

#### Example

A profile named "Extract → CHD (CD)" would have two steps:

| position | processor_type | options_json |
|----------|---------------|--------------|
| 0 | `extract` | `{}` |
| 1 | `chd_cd` | `{"hunks": 8}` |

#### Worker Execution

When processing a file against a pipeline profile:

1. Start with the input file (e.g., `mega cd.zip`)
2. Execute step 0 (`extract`) → outputs: `sonic cd (usa).bin`, `sonic cd (usa).cue`
3. Filter step 1's input extensions against the outputs → `sonic cd (usa).cue` matches `.cue`
4. Execute step 1 (`chd_cd`) on the matching file → output: `sonic cd (usa).chd`
5. Each step's completion updates the DB before the next step begins

If a step produces outputs that don't match the next step's input extensions, those files are left as-is (extracted but not further processed). This is correct — not everything in a zip needs transformation.

#### UI Plan — Profile Editor

The current profile editor modal shows: name, processor type dropdown, processor-specific options. This changes to a **step list with drag-to-reorder**:

```
┌─────────────────────────────────────────────────┐
│ Edit Profile                                     │
│                                                   │
│ Name: [Extract → CHD (CD)                      ] │
│                                                   │
│ Pipeline Steps:                                   │
│ ┌───────────────────────────────────────────────┐ │
│ │ ≡  1. Extract                            [×]  │ │
│ │    (no options)                                │ │
│ ├───────────────────────────────────────────────┤ │
│ │       ↓ outputs feed into next step           │ │
│ ├───────────────────────────────────────────────┤ │
│ │ ≡  2. CHD (CD)                           [×]  │ │
│ │    Hunks: [8]  Compression: [zstd ▼]          │ │
│ └───────────────────────────────────────────────┘ │
│                                                   │
│ [+ Add Step]                                      │
│                                                   │
│                        [Cancel]  [Save]           │
└─────────────────────────────────────────────────┘
```

**Key interactions:**

- **Add Step** — appends a new step with a processor type dropdown. Selecting the type reveals its options inline.
- **Drag handle (≡)** — reorder steps. Position determines execution order.
- **Remove (×)** — delete a step from the pipeline.
- **Arrow between steps** — visual indicator that outputs flow downward. Clicking it could show which file extensions bridge the two steps.
- **Single-step profiles** — a profile with just one step (e.g., only `chd_cd`) works exactly like today. No extraction, just direct processing.
- **Validation** — warn if consecutive steps have no overlapping extensions (step N outputs nothing that step N+1 accepts).

#### Profile List Display

In the settings profile list, each profile shows its step chain compactly:

```
Extract → CHD (CD)          Extract ▸ CHD (CD)       [Edit] [Delete]
CHD (DVD)                   CHD (DVD)                [Edit] [Delete]
Extract Only                Extract                  [Edit] [Delete]
```

The step chain uses `▸` separators, similar to how the path-builder-visual shows segments.

#### Assigning Profiles to Archives

No change — an archive still gets assigned a single profile ID. The difference is that profile now means "run these N steps in sequence" rather than "run this one processor."

### Cancellation Behaviour

With the split pipeline:
- Cancel after extract completes → extracted files remain on disk and in DB. User sees them in the file list.
- Cancel during extract → partial extraction is cleaned up (as today).
- Cancel during transform → extracted files remain, partially processed output is cleaned up.

This is a significant UX improvement over the current all-or-nothing approach.

---

## 5. File List UI

### Tree Rendering

The existing file list already uses a dropdown to show processed outputs under their source file. This same pattern extends to archive contents:

```
v mega cd.zip                    [download] [process] [expand]
  v sonic cd (usa).zip           [contained] 
    | sonic cd (usa).bin         [contained]
    v sonic cd (usa).cue         [contained]
      | sonic cd (usa).chd       [processed]  ← local_files overlay
```

The expand/collapse is driven by `parent_file_id` — click a zip row to show its children.

### Status Display

| File State | Badge | Colour |
|------------|-------|--------|
| Contained (host not downloaded) | `contained` | grey |
| Contained (host queued) | `queued` | blue |
| Extracted (on disk) | `extracted` | green |
| Processed output | `processed` | purple |
| Strikethrough | File has processed outputs but source file itself is not on disk | dim text |

### Strikethrough Semantics

A file shown with strikethrough means: "this file has been processed (outputs exist), but the source file itself is no longer on disk." This happens when:
- The user deleted the source after processing
- A scan matched a file by filename but the original was never downloaded

The strikethrough is a visual cue, not a database state. It's derived at render time: `has_processed_outputs AND NOT on_disk`.

---

## 6. Scanning & Matching

### Filename-Based Matching

When scanning a folder, matched files should be moved into `{filename}.processed/` to match the convention. The scan worker already matches by filename — this extends to archive contents:

1. Scan finds `sonic cd (usa).chd` on disk
2. Match against `archive_files` where `name = 'sonic cd (usa).cue'` (the expected processor input)
3. Record in `local_files` as processed output of the cue file

### Limitations Without Hashes

`view_archive.php` does not return hashes. For contained files, matching is filename + size only. This is acceptable for the preview use case but means we can't do hash-based integrity verification until after extraction.

### Queue Implications

Fetching archive contents for a large item (hundreds of zips) will take time — one HTTP request per zip. This should be a background task in the scan queue:

1. User adds archive → metadata fetched (fast, single request)
2. Metadata contains zip/7z files → "Fetch archive contents" tasks queued
3. Tasks run in background, one per compressed file
4. Each task calls `fetch_archive_contents()`, inserts `archive_content` rows
5. UI updates progressively as contents are discovered

This fits naturally into the existing scan queue infrastructure. A new task type `'fetch_contents'` alongside the existing `'scan'` type.

---

## 7. Database Migration

### New Columns & Tables

```sql
ALTER TABLE archive_files ADD COLUMN parent_file_id INTEGER DEFAULT NULL
    REFERENCES archive_files(id) ON DELETE CASCADE;

CREATE INDEX idx_af_parent ON archive_files(parent_file_id);

CREATE TABLE local_file_sources (
    local_file_id INTEGER NOT NULL REFERENCES local_files(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES archive_files(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'primary',
    UNIQUE(local_file_id, source_file_id)
);

CREATE INDEX idx_lfs_local ON local_file_sources(local_file_id);
CREATE INDEX idx_lfs_source ON local_file_sources(source_file_id);
```

### New Download Status Values

No schema change needed — `download_status` is already TEXT. Add `'contained'`, `'contained_queued'`, `'extracted'` as valid values alongside existing `'pending'`, `'downloading'`, `'downloaded'`, `'failed'`, `'skipped'`.

### Processing Profile Step Table

```sql
CREATE TABLE processing_profile_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES processing_profiles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    processor_type TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(profile_id, position)
);
```

### Migration for Existing Data

**Processing profiles:** Existing single-processor profiles get one row in `processing_profile_steps` with `position=0`, copying `processor_type` and `options_json` from the parent profile. The columns on `processing_profiles` can then be dropped (or left as dead columns for safety).

**Extracted files:** Existing extract-type `local_files` rows stay in `local_files` — they are already in the correct table. Ensure their `source_file_id` points to the correct archive file and their `processor_type = 'extract'` is preserved. If corresponding `archive_content` rows don't yet exist (because the feature is new), create them from the existing `local_files` entries:
1. For each extract-type `local_files` row, look up the source archive file
2. Create `archive_content` rows in `archive_files` with `parent_file_id` → source, `download_status = 'contained'`
3. The `local_files` rows already track what's on disk; the new `archive_files` rows track what IA says is inside the archive

---

## 8. Implementation Order

### Phase 1: Data Model & Migration
1. Add `parent_file_id` column to `archive_files`
2. Create `local_file_sources` junction table
3. Create `processing_profile_steps` table
4. Migrate existing single-processor profiles → one row each in `processing_profile_steps`
5. Add `archive_content` origin and `contained`/`contained_queued`/`extracted` statuses

### Phase 2: Fetching & Inspection
6. Implement `ia_client.fetch_archive_contents()` (HTML parser for remote)
7. Implement local archive inspector (`zipfile`/`py7zr`/`rarfile` for on-disk archives)
8. Insert archive content rows on metadata fetch (first level, via `view_archive.php`)
9. Hook download completion to trigger local inspection of downloaded archives (discovers nested contents)
10. Hook extraction completion to trigger local inspection of extracted archives
11. Add background queue task type for fetching/inspecting contents
12. Implement hash-based staleness check to skip re-fetching unchanged archives

### Phase 3: Pipeline Split
13. Refactor `ExtractProcessor` to only extract + update DB status
14. Implement pipeline profile step execution: worker walks `processing_profile_steps` in order, feeding outputs from one step as inputs to the next
15. Populate `local_file_sources` with primary + dependency roles from processor results
16. Update cancellation to preserve partial progress (extracted files remain after cancel)

### Phase 4: File List UI & Profile Editor
17. Extend file list rendering to show nested tree via `parent_file_id`
18. Display `contained`/`contained_queued`/`extracted` status badges
19. Reuse existing processed-output dropdown for the nested view
20. Strikethrough rendering for files with outputs but no source on disk
21. Implement pipeline profile step list editor UI (add/remove/reorder steps)
22. Update profile list display to show step chain with `▸` separators

### Phase 5: Tag Inheritance, Collections & Scanning
23. Extend virtual tag computation to walk `parent_file_id` chain
24. Update collection file queries to include/exclude contained files based on status
25. Ensure archive_content files participate in collection preview
26. Extend scan matching to consider contained file names
27. Move matched files into `{filename}.processed/` convention
28. Add "Fetch archive contents" as a scan queue task type

---

## 9. Resolved Questions & Decisions

1. **Rate limiting** — Apply the same rate limiting already used for IA metadata fetches. Throttle `view_archive.php` requests identically.

2. **Archive format coverage** — Stick to **zip, 7z, rar** for now. `unrar` tool is already available. For local inspection: Python `zipfile` for zip, `py7zr` for 7z, `rarfile`/`unrar` for rar.

3. **Staleness** — IA metadata includes hashes for top-level files. If a zip file's hash hasn't changed since last fetch, skip re-fetching its contents — they're presumed identical. Only re-fetch when the hash differs.

4. **Extracted files** — Go into `local_files` (not `archive_files`). This keeps IA-sourced data cleanly separated from local processing artifacts. `archive_files` is for things that exist on IA (manifest + archive_content); `local_files` is for things that exist on disk as a result of local operations (extraction, processing).

5. **Progress notification** — Use the pre-existing notification system. No new UI needed for communicating "fetching contents..." — the same notification banners and progress indicators used for downloads and processing work here.

6. **Uninspectable archives** — Skip and log. Encrypted zips, split archives, and corrupted files are gracefully skipped with a log entry. No error state on the file — it just won't have child rows.
