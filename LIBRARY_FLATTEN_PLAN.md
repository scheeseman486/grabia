# Library Symlink Flattening Plan

## Problem

Archive file structures vary:

1. **Flat** — all files in the root of the archive identifier directory (ideal case)
2. **Subfoldered** — files organised into subdirectories by region, format, etc.
3. **Nested archives** — zip files with their own internal folder structures (handled at processing stage, out of scope here)

Currently, `_resolve_filename()` returns the full relative path from the archive (e.g. `USA/Game.chd`), and this path — including subdirectories — gets embedded directly into the symlink path inside the collection layout. So an alphabetical layout might produce:

```
A-Z/U/USA/Game.chd → ../../downloads/redump-psx/USA/Game.chd
```

Instead of the desired:

```
A-Z/G/Game.chd → ../../downloads/redump-psx/USA/Game.chd
```

Additionally, some "files" are actually **multi-file media units**: a folder containing CUE+BIN pairs, multiple discs, manuals, artwork, etc. These should be symlinked as a directory, not as individual files.

## Design

### 1. Media unit detection (database layer)

Add a column `media_root` to `archive_files`:

```
media_root TEXT DEFAULT ''
```

This stores the path prefix (relative to the archive directory) that represents the folder containing this file as part of a multi-file media unit. When empty, the file is its own standalone media unit.

**Critical rule: `media_root` only applies to unprocessed files.** Files with `processing_status = 'processed'` are always treated as standalone units regardless of their `media_root` value. Processing inherently produces a single output file (e.g. `.chd`, `.cso`, `.bigpimg`) that is its own unit — the processor has already collapsed multi-file input into one output. This means `_build_media_units()` ignores `media_root` for any file where `processed_filename` is set.

Examples for an archive `jaguar-cd`:

| name | media_root | processed_filename | treatment |
|------|-----------|-------------------|-----------|
| `Tempest 2000/Tempest 2000 (Track 01).bin` | `Tempest 2000` | *(empty)* | Grouped into "Tempest 2000" directory symlink |
| `Tempest 2000/Tempest 2000.cue` | `Tempest 2000` | *(empty)* | Grouped into "Tempest 2000" directory symlink |
| `Tempest 2000/Tempest 2000.cue` | `Tempest 2000` | `Tempest 2000.chd` | Standalone — processing overrides media_root |
| `Baldies.cue` | *(empty)* | *(empty)* | Standalone file symlink |
| `Baldies (Track 01).bin` | *(empty)* | *(empty)* | Standalone (CUE is the media unit entry point) |

When `media_root` is set and the file is unprocessed, the collection sync groups all files sharing the same `media_root` and creates a single symlink **to the directory** rather than individual file symlinks.

### 2. Auto-detection during scan (preferred) or manual override

**Auto-detection heuristics** (applied during file scan, stored in `media_root`):

- If a folder contains a `.cue` file plus one or more `.bin` files matching it, mark all of them with `media_root = folder_path`.
- If a folder contains a `.gdi` file plus track files, same treatment.
- If a folder contains exactly one playable media file or zip/7z plus metadata (`.txt`, `.nfo`, `.jpg`, `.png`, `.pdf`, `.xml`), mark them as a unit.
- Single files at any depth with no siblings in the same folder are standalone units.

**Manual override** via the archive file list context menu:

- A "Group as media unit" action — select multiple files, mark them with a shared `media_root`.
- A "Split media unit" action to clear `media_root` on selected files.

These context menu actions set a database flag only. They do **not** change the visual presentation of the file list — the archive file list remains file-centric (see §6 below for the UI separation rationale). This is deliberately different from the "Processed Files" expand/collapse behaviour: processed files have a parent-child visual relationship in the file list because they represent a transformation of the original file. Media units are an organisational concept that only manifests in the library output.

### 3. Collection sync changes (collection_sync.py)

#### `_resolve_filename()` changes

Currently returns the full path including subdirectories. Change to return **only the leaf name**:

```python
def _resolve_filename(file_row):
    raw = file_row.get("processed_filename") or file_row["name"]
    # Strip subdirectory structure — library flattens to leaf names
    return os.path.basename(raw)
```

This single change is the core of the "flattening" — symlinks always use the leaf filename regardless of where the file sits in the archive's directory tree. The symlink _target_ still uses the full path, so the real file is found correctly.

#### Media unit grouping

New function `_build_media_units()` collapses files into units before layout computation. Inserted between `_build_file_list()` and `_compute_layout_mapping()`:

```python
def _build_media_units(files, download_dir):
    """Collapse files sharing a media_root into a single directory unit.

    Returns a list of unit dicts:
      - Standalone files: {display_name, file_row, is_dir=False}
      - Media root dirs:  {display_name, file_row (representative), is_dir=True, target_dir}

    Processed files are always standalone — media_root is ignored when
    processed_filename is set, because the processor has already collapsed
    multi-file input into a single output.
    """
    units = []
    grouped = defaultdict(list)

    for f in files:
        root = f.get("media_root", "")
        is_processed = bool(f.get("processed_filename"))
        if root and not is_processed:
            grouped[(f["archive_identifier"], root)].append(f)
        else:
            units.append({
                "display_name": _resolve_filename(f),
                "file_row": f,
                "is_dir": False,
            })

    for (identifier, root), group_files in grouped.items():
        units.append({
            "display_name": os.path.basename(root),
            "file_row": group_files[0],  # representative for archive_identifier etc.
            "is_dir": True,
            "target_dir": os.path.join(download_dir, identifier, root),
        })

    return units
```

#### Updated `_compute_layout_mapping()`

Change signature to accept media units instead of raw file rows:

```python
def _compute_layout_mapping(layout, units):
    """Compute {relative_dir: [(display_name, unit), ...]} for a layout type."""
    layout_type = layout["type"]
    mapping = defaultdict(list)

    for unit in units:
        display_name = unit["display_name"]
        if layout_type == "flat":
            mapping[""].append((display_name, unit))
        elif layout_type == "alphabetical":
            bucket = _alphabetical_bucket(display_name)
            mapping[bucket].append((display_name, unit))
        elif layout_type == "by_archive":
            mapping[unit["file_row"]["archive_identifier"]].append((display_name, unit))
        else:
            mapping[""].append((display_name, unit))

    return mapping
```

#### Symlink creation changes in `sync_collection()`

The symlink creation loop needs to handle both files and directories:

```python
for display_name, unit in entries:
    link_path = os.path.join(link_parent, display_name)

    if unit["is_dir"]:
        target_path = unit["target_dir"]
    else:
        target_path = _resolve_filepath(unit["file_row"], download_dir)

    desired[link_path] = (target_path, unit["is_dir"])
```

For standalone files: `os.symlink(rel_target, link_path)` — same as today.
For media unit directories: `os.symlink(rel_target, link_path)` — a directory symlink pointing to the media root folder.

The target existence check changes: use `os.path.isfile()` for files, `os.path.isdir()` for directory units.

The stale symlink scan also changes: `os.walk()` currently only looks at `filenames`, but directory symlinks appear in `dirnames` instead. Need to check both.

### 4. Processing stage integration

Processors already flatten structure during conversion (e.g. extract from archive → single `.chd` in the archive root). No changes needed there — the `processed_filename` is already a flat leaf name.

The scan stage is where `media_root` gets populated. When `app.py` runs a file scan for an archive, it analyses the directory tree and applies the auto-detection heuristics listed above. This happens once; users can override via the UI afterward.

### 5. Migration

- Add `media_root` column to `archive_files` (default empty string).
- Existing collections continue to work — empty `media_root` means all files are standalone, and `os.path.basename()` on filenames is the only behaviour change (flattening).
- Users re-sync their collections to get the flattened structure.

### 6. UI: Two views for two purposes

This is the key design decision that resolves the tension between media units and the existing processed-files UI.

#### Problem: visual conflict

The archive file list already has an expand/collapse pattern for processed files — clicking a processed file reveals the original source files beneath it. Media units could use a similar visual grouping, but this would create confusion: what does a grouped row mean? Is it a processing result or a media unit? The two concepts are orthogonal — a file can be both part of a media unit AND have a processed output.

#### Solution: separation of concerns

**Archive detail page** (existing) = file-centric view.
Shows individual files, their download/processing status, and the parent-child relationship between processed outputs and their source files. Media units are **invisible** here — `media_root` is a database flag with no visual representation in the file list. The "Group as media unit" / "Split media unit" context menu actions work on the selection, but the files continue to appear as individual rows.

**Collection detail page** (new) = media-centric preview.
Shows what the library layouts will actually produce. This is a read-only preview of the flattened, organised output — the structure a user will see when browsing their collection via file manager or Kodi/RetroArch.

#### Collection preview design

The collection detail page currently shows which archives are included and the layout configuration. Add a **preview panel** below the layout list that shows the computed output for each layout:

```
Collection: PlayStation
├── Layout: A-Z (alphabetical)
│   ├── A/
│   │   ├── Ape Escape.chd          ← standalone processed file
│   │   └── Armored Core/           ← directory symlink (media unit)
│   │       ├── Armored Core.cue
│   │       ├── Armored Core (Track 01).bin
│   │       └── Armored Core (Track 02).bin
│   ├── B/
│   │   └── Brave Fencer Musashi.chd
│   ...
└── Layout: All (flat)
    ├── Ape Escape.chd
    ├── Armored Core/
    ├── Brave Fencer Musashi.chd
    ...
```

#### Rendering: reuse the virtual scroll file list

The archive file list's virtual scroll infrastructure — row recycling, scroll-position tracking, viewport slicing — is directly applicable to the collection preview. A collection spanning multiple large archives could easily contain tens of thousands of entries; a naive DOM tree would choke the same way a non-virtualised file list would.

The preview list reuses the same pattern:

- **Flat row model**: The API returns the preview as a flat array of rows, each tagged with a `depth` and `type` (`bucket_header`, `file`, `dir_unit`). Bucket headers (e.g. "A/", "B/") are rows like any other, just styled differently — no nested DOM. This mirrors how the file list handles processed-file expand/collapse: child rows are just extra rows inserted into the flat array.
- **Virtual scroll container**: Same `overflow-y: auto` container with a spacer div sized to `totalRows × rowHeight`. Only the visible slice of rows is rendered into the DOM, rebuilt on scroll. The existing `_buildVisibleRows()` / `requestAnimationFrame` throttle pattern applies directly.
- **Row builder**: `buildPreviewRow(unit)` instead of `buildFileRow(f)`. Each row shows an icon (file or folder), the display name, and optionally the source archive identifier. Bucket headers get a distinct style (bold, background tint). Directory units show a folder icon and can be expanded inline to show their constituent files (fetched from the same preview response — the API includes child files for each directory unit).
- **Shared CSS**: Row height, hover state, alternating row colours, and selection highlight classes are shared between the file list and preview list. The preview adds a depth-based left indent (`padding-left: depth × 20px`) for bucket nesting.

This means the preview handles large collections (30,000+ entries across dozens of archives) without layout or memory issues, using the same proven approach as the archive file list.

Implementation:

- New API endpoint: `GET /api/collections/<id>/preview` — runs the same logic as `sync_collection()` but returns the computed mapping as a flat row array instead of creating symlinks. Calls `_build_file_list()` → `_build_media_units()` → `_compute_layout_mapping()` → `_resolve_conflicts()`, then serialises to `[{type, depth, display_name, is_dir, archive_identifier, children?}, ...]`.
- The preview shows:
  - Each layout as a collapsible section header row
  - For alphabetical: bucket header rows with their contents beneath
  - For flat: entries directly under the layout header
  - For by_archive: archive name header rows with entries beneath
  - Each entry shows the display name, whether it's a file or directory unit, and which archive it comes from
  - Conflict resolution prefixes (e.g. `[redump-psx-usa] Game.chd`) are visible in the preview
- The preview updates automatically when archives are added/removed from the collection or layouts change
- No editing capability — it's purely informational. To change what appears, users modify archives/file-scope/layouts, or use "Group/Split" in the archive file list

#### Data flow

```
Archive file list (file-centric)
    │
    │  "Group as media unit" context menu
    │  → sets media_root in DB (no visual change)
    │
    ▼
get_collection_files()
    │
    ▼
_build_media_units()        ← collapses media_root groups; skips processed files
    │
    ▼
_compute_layout_mapping()   ← flattened display_names, alphabetical/flat/by_archive
    │
    ▼
_resolve_conflicts()        ← prefix duplicates with archive identifier
    │
    ├──→ Collection preview (JSON for UI)
    └──→ sync_collection() (creates symlinks on disk)
```

Both the preview API and the sync function share the same pipeline up to `_resolve_conflicts()`. The preview serialises the result; sync creates symlinks.

### 7. `file_scope` interaction with media units

The collection's `file_scope` setting (`processed`, `downloaded`, or `both`) determines which files enter the pipeline:

- **`processed`**: Only files with `processing_status = 'processed'`. Since processed files are always standalone (§1), media units never appear in this scope. This is the common case for collections of converted media (CHD, CSO, etc.).
- **`downloaded`**: Only manifest files with `download_status = 'completed'`. Media units apply fully here — unprocessed CUE+BIN sets get grouped.
- **`both`**: Processed files take precedence (standalone), and remaining downloaded files may form media units. This is the interesting case: if a CUE+BIN set has been processed into a CHD, the CHD appears as a standalone entry and the original CUE+BIN files are excluded (the `get_collection_files()` query already handles this by preferring processed files). If only some files in a media unit have been processed, the processed ones appear standalone and the remaining unprocessed ones still group into the media unit directory.

Edge case for `both` scope: a partial processing scenario where some files in a `media_root` group are processed and some aren't. The processed files become standalone; the remaining unprocessed files that share the same `media_root` still form a (smaller) directory unit. This could look odd — a directory that's missing some of its expected contents. The preview makes this visible so users can decide whether to process the remaining files or adjust the media unit grouping.

### 8. Conflict resolution with media units

`_resolve_conflicts()` already handles duplicate display names by prefixing with the archive identifier. With media units, the same logic applies: if two archives both have a "Tempest 2000" directory unit, they become `[jaguar-cd-usa] Tempest 2000` and `[jaguar-cd-eur] Tempest 2000`.

The conflict resolution operates on `display_name` which is `os.path.basename(media_root)` for directory units — so it works identically to file conflicts.

## Scope

### In scope
- `media_root` column + migration
- `_resolve_filename()` returning leaf name only
- `_build_media_units()` function with processed-file exclusion
- Updated `_compute_layout_mapping()` accepting units
- Directory symlink support in `sync_collection()`
- Stale symlink detection for both file and directory symlinks
- Auto-detection heuristics during scan
- "Group as media unit" / "Split" context menu actions (DB flag only, no visual grouping)
- Collection preview API endpoint
- Collection preview UI panel

### Out of scope
- Changes to how processors handle archives (already works)
- Changes to download directory structure (preserved as-is)
- Nested archive extraction (already handled by processors)
- Visual grouping of media units in the archive file list (deliberately excluded)
- Changes to processed-file expand/collapse UI

## File changes

| File | Change |
|------|--------|
| `database.py` | Add `media_root` column + migration; `set_media_root()`, `get_media_units()`; `get_collection_preview()` helper |
| `app.py` | Scan endpoint populates `media_root`; new API: manual media unit grouping, collection preview |
| `collection_sync.py` | `_resolve_filename()` returns basename; new `_build_media_units()`; updated `_compute_layout_mapping()`; directory symlink support; new `preview_collection()` (shared pipeline, JSON output) |
| `static/js/app.js` | Context menu entries for "Group as media unit" / "Split media unit"; collection preview panel rendering |
| `templates/index.html` | Collection detail: preview panel container |
| `static/css/style.css` | Preview panel tree styles |
| `processors.py` | No changes needed |

## Implementation order

1. **Database migration** — add `media_root` column. Zero-risk, additive only.
2. **`_resolve_filename()` basename change** — immediate flattening benefit with no media unit complexity. Collections re-sync to pick up the change.
3. **`_build_media_units()` + updated layout mapping** — media unit grouping in the sync pipeline. Directory symlink creation.
4. **Auto-detection heuristics** — populate `media_root` during scan.
5. **Preview API + UI** — collection detail preview showing computed output.
6. **Context menu actions** — "Group as media unit" / "Split" manual overrides.

Steps 1-2 can ship independently and immediately improve the library for subfoldered archives. Steps 3-6 build on each other but can be incremental.
