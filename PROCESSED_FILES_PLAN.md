# Processed Output → Re-processable Files Plan

## Design Constraints

**`archive_files` is a mirror of the Internet Archive manifest.** It must always reflect what IA says exists. Processed outputs, discovered files, and any other local artifacts must live in a separate layer so that the manifest view is never polluted.

This means no inserting child rows into `archive_files`. All processing output tracking goes into its own table.

**Downloads and processed files are physically separated on disk.** Downloaded files live under `download_dir/identifier/`, and processed outputs live under `processed_dir/identifier/`. This keeps the download directory a clean mirror of IA and makes it easy to manage storage independently (e.g. different volumes for originals vs. converted files).

### Directory Layout (implemented)

```
/grabia/
├── downloads/              ← pure IA downloads, untouched
│   ├── xbox-redump-1/
│   │   └── game.zip       ← original from Internet Archive
│   └── snes-roms/
│       └── game.sfc
├── processed/              ← all processing outputs go here
│   ├── xbox-redump-1/
│   │   └── game.chd       ← converted from game.zip
│   └── snes-roms/
│       └── game.sfc.zip   ← compressed
└── collections/            ← generated symlink trees
    └── Xbox/
        └── All/
            └── game.chd → ../../../processed/xbox-redump-1/game.chd
```

- `processed_dir` setting: configurable in Settings → Downloads, defaults to a `processed/` sibling of `download_dir`.
- All path resolution (`_resolve_processed_file`, `_resolve_filepath`) checks `processed_dir` first, falls back to `download_dir` for legacy compatibility.
- Migration tool in Settings → Debug → Maintenance moves existing processed files from download dirs to the processed dir.

## The Problem

When a processor (typically `extract`) runs on `game.zip`, it can produce multiple output files — a dozen `.bin` files, several `.iso` images, etc. These are stored in `processed_files_json` on the source `archive_files` row. Today they're dead ends: they appear in the library as symlinks but cannot be fed back through the processing pipeline. A user who extracts a batch of zips and then wants to convert each extracted `.bin` to `.chd` has no path to do so within Grabia.

## Current Architecture

`archive_files` rows represent source files from the IA manifest. Processing metadata is bolted onto the same row:

| Column | Role |
|---|---|
| `name` | Original filename from the manifest |
| `processing_status` | `''` → `queued` → `processing` → `processed` / `failed` / `skipped` |
| `processed_filename` | Primary output path relative to archive download dir |
| `processed_files_json` | JSON array of ALL output paths (including primary) |
| `processor_type` | Which processor produced the output |

Problems with this:

- Only one `processing_status` per row — no concept of "step 1 done, step 2 pending."
- `get_processable_files()` filters on `origin = 'manifest'` — outputs can't re-enter the pipeline.
- Multi-output files are a JSON blob with no individual status, size, or error tracking.
- Library sync needs special expansion logic to turn JSON entries into symlinks.

---

## Design: Separate `processed_outputs` Table

### New table: `processed_outputs`

```sql
CREATE TABLE processed_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL,

    -- Lineage: what produced this file
    source_file_id INTEGER,          -- FK → archive_files.id (NULL for outputs of other outputs)
    source_output_id INTEGER,        -- FK → processed_outputs.id (NULL for first-gen outputs)
    -- Exactly one of source_file_id / source_output_id is set.

    -- File identity
    name TEXT NOT NULL,              -- relative path within archive download dir (e.g. "extracted/file1.bin")
    size INTEGER NOT NULL DEFAULT 0, -- bytes, read from disk at insertion time

    -- Processing state (same lifecycle as archive_files)
    processing_status TEXT NOT NULL DEFAULT '',
    processed_filename TEXT NOT NULL DEFAULT '',
    processed_files_json TEXT NOT NULL DEFAULT '',
    processor_type TEXT NOT NULL DEFAULT '',
    processing_error TEXT NOT NULL DEFAULT '',

    -- Metadata
    generation INTEGER NOT NULL DEFAULT 1,  -- 1 = direct output of manifest file, 2 = output of output, etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
    FOREIGN KEY (source_file_id) REFERENCES archive_files(id) ON DELETE CASCADE,
    FOREIGN KEY (source_output_id) REFERENCES processed_outputs(id) ON DELETE CASCADE
);
```

### How it works

**Step 1 — Extraction (or any multi-output processor):**

1. Processor runs on archive_file A (`game.zip`), produces `file1.bin`, `file2.bin`, `file3.bin`.
2. `archive_files` row A is updated as today: `processing_status = 'processed'`, `processed_filename` and `processed_files_json` set.
3. **New:** For each output path, insert a `processed_outputs` row:
   - `source_file_id = A.id`
   - `name = "extracted/file1.bin"` (relative path)
   - `size` = read from disk
   - `generation = 1`
   - `processing_status = ''` (eligible for further processing)

**Step 2 — Re-processing (e.g. bin → chd):**

1. User selects outputs in the UI and queues them for CHD conversion.
2. Processing worker picks up each `processed_outputs` row (same lifecycle: `queued` → `processing` → `processed`).
3. Result: the output row gets its own `processed_filename = "extracted/file1.chd"`.
4. If the processor produces multiple outputs, new `processed_outputs` rows are inserted with `source_output_id` pointing back — `generation = 2`.

**Arbitrary chaining:** The pattern repeats. Each generation's outputs can be re-processed into the next. The lineage tree is always traceable via `source_file_id` / `source_output_id`.

### What stays the same

- `archive_files` is untouched structurally. It continues to mirror the IA manifest exactly.
- The existing `processed_filename` / `processed_files_json` columns on `archive_files` remain and continue to work as they do today — they record what the first processing pass produced.
- Existing processors return the same result dict. The worker just has an additional step of inserting `processed_outputs` rows.

### What changes

**Processing worker (`processing_worker.py`):**
- After a successful process, insert `processed_outputs` rows for each output file.
- New code path: when processing a `processed_outputs` row (not an `archive_files` row), the queue entry references `output_id` instead of `file_id`. The worker resolves the file path from `processed_outputs.name` instead of `archive_files.name`.

**Processing queue (`processing_queue` table):**
- Add nullable `output_id INTEGER` column (FK → `processed_outputs.id`).
- Queue entries have either `file_id` (processing a manifest file) or `output_id` (processing a previous output). Never both.

**`get_processable_files()` equivalent for outputs:**
- New function: `get_processable_outputs(archive_id)` — returns `processed_outputs` rows where `processing_status IN ('', 'failed')`.

**Library sync (`collection_sync.py`):**
- `get_collection_files()` gains a companion: `get_collection_outputs(collection_id)` that queries `processed_outputs` for the same archives/scope.
- `_build_media_units()` consumes both lists. Outputs replace their parent's entry when present (if file A was extracted and the bins were then converted to chds, the library shows the chds — not the bins, not the zip).
- The `processed_files_json` expansion logic in `_build_media_units()` becomes a fallback for archives that haven't been re-scanned since the migration (backwards compat).

**File list UI:**
- Processed outputs appear as expandable children under their parent manifest file.
- Each output shows its own processing status, size, and can be individually selected for further processing.
- Clear visual distinction: manifest files are the top-level rows; outputs are indented beneath them.

**Unknown file detection (`_finish_archive_scan()`):**
- Must include `processed_outputs.name` values in the known-files set so they aren't flagged as unknowns.

### Library sync: which file wins?

When multiple generations of outputs exist for the same source file, the library needs to pick which one to symlink. The rule is simple: **use the deepest processed generation that has `processing_status = 'processed'`.**

Example chain: `game.zip` → `game.bin` (gen 1, processed) → `game.chd` (gen 2, processed)

- Library symlinks `game.chd` (gen 2 — the final output).
- If gen 2 failed or is still pending, falls back to `game.bin` (gen 1).
- If gen 1 also failed, falls back to the original `game.zip` behavior (using `processed_filename` on the `archive_files` row).

This "deepest successful output" resolution happens in `_build_media_units()` by first grouping outputs by their root `archive_files` source, then selecting the leaf nodes of the lineage tree.

### Deletion cascading

- Deleting a `processed_outputs` row deletes the file from disk and cascades to all downstream rows (children with `source_output_id` pointing to it).
- Resetting processing on an `archive_files` row deletes all `processed_outputs` with `source_file_id` matching it (and their descendants via cascade).
- Re-scanning an archive: existing `processed_outputs` rows are preserved unless the parent `archive_files` row is removed (IA manifest changed).

### Migration

1. Create the `processed_outputs` table.
2. Add `output_id` column to `processing_queue`.
3. For each `archive_files` row that has `processed_files_json`:
   - Parse the JSON array.
   - Insert a `processed_outputs` row for each entry with `generation = 1`, `processing_status = ''`, size read from disk.
   - The `processed_files_json` column is left intact for backwards compatibility (library sync falls back to it if outputs table is empty for a given file).

---

## Implementation Order

1. **Schema:** Create `processed_outputs` table, add `output_id` to `processing_queue`.
2. **Worker — insertion:** After processing an `archive_files` row, insert output rows.
3. **Worker — output processing:** Support queue entries with `output_id`, resolve paths from `processed_outputs`.
4. **Database functions:** `get_processable_outputs()`, `get_archive_outputs()`, deletion/reset cascading.
5. **Library sync:** Query outputs alongside files, implement deepest-generation resolution.
6. **File list UI:** Show outputs as indented children, with processing actions.
7. **Migration:** Backfill `processed_outputs` from existing `processed_files_json` data.
8. **Cleanup:** Once migration is stable, `processed_files_json` expansion in `_build_media_units()` can be removed (or kept as fallback).
