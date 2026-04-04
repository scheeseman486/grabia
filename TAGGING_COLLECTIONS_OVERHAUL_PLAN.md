# Tagging & Collections Overhaul Plan

## Current State

**Tagging:** Archive-level only, fully manual. Tags are plain strings stored in `archive_tags` table. No file-level tags. No auto-tagging from filenames or metadata.

**Collections:** Three layout types (flat, alphabetical, by_archive). Layouts are separate cards on the detail page. Archives listed below. Preview virtual scroll renders a flat list of layout_header → bucket_header → file/dir_unit rows. No tag-based sorting folders.

---

## Part 1: Tagging Overhaul

### 1.1 Auto-Tag Engine

Create `auto_tagger.py` — a standalone module that parses structured tags from filenames and archive metadata.

**Tag key file:** `TAG_KEY.txt` (already created) contains the static mapping of parenthetical tokens to structured tags. The engine loads this at startup and caches it as a lookup dict.

**Parsing logic:**

1. Extract all parenthesised tokens from a filename: regex `\(([^)]+)\)` on the basename
2. For each token, split on `, ` (comma-space) to handle multi-value groups like `(USA, Europe)`
3. For each sub-token, trim whitespace and attempt resolution:
   - Exact match against TAG_KEY lookup (case-insensitive) → use mapped tag(s)
   - Regex patterns for dynamic tags:
     - `Rev [A-Z0-9]+` → `alt:rev_{value}` (lowercased)
     - `v\d+\.\d+` → `version:{value}`
     - `Alt \d+` → `alt:{number}`
     - `\d{4}-\d{2}-[\dxX]{2}` → `date:{value}`
     - `\d{8}` → `date:{formatted}` (YYYY-MM-DD)
     - `Disc \d+` / `Disk \d+` → `disc:{number}`
   - No match → `unknown:{token_lowercased_underscored}`
4. Deduplicate tags per file

**Tag sources for files:**
- Parsed from the file's own filename (parenthetical tokens)

**Tag sources for archives:**
- Group tags: `group:{group_name}` (from the archive's group membership)
- All unique file-level tags bubbled up to archive level (union of all file tags)

**Tag inheritance:**
- Files inherit their archive's group tag (`group:*`)
- Files inherit any user-added archive-level tags
- Archives inherit their group's tag
- Inheritance is computed at query time, not stored redundantly

**Functions:**
- `parse_file_tags(filename) -> list[str]` — extract tags from a single filename
- `auto_tag_archive(archive_id)` — parse all files in an archive, store file-level auto-tags, recompute archive auto-tags
- `load_tag_key(path) -> dict` — load TAG_KEY.txt into lookup dict

### 1.2 Database Changes

**New table: `file_tags`**
```sql
CREATE TABLE IF NOT EXISTS file_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,  -- 1 = auto-generated, 0 = user-added
    UNIQUE(file_id, tag),
    FOREIGN KEY (file_id) REFERENCES archive_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_tags_file ON file_tags(file_id);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag);
```

**Modify `archive_tags`** — add `auto` column:
```sql
ALTER TABLE archive_tags ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;
```

Auto-generated tags (`auto=1`) cannot be removed by the user. User-added tags (`auto=0`) can be removed. Auto tags are regenerated on scan/rescan; user tags are preserved.

**New DB functions:**
- `add_file_tag(file_id, tag, auto=False)` — insert file tag
- `remove_file_tag(file_id, tag)` — remove only if `auto=0`
- `get_file_tags(file_id)` — return all tags for a file (with `auto` flag)
- `get_file_tags_bulk(file_ids)` — bulk fetch for efficiency
- `clear_auto_file_tags(file_id)` — remove all auto tags for a file (before re-parse)
- `clear_auto_archive_tags(archive_id)` — remove all auto archive tags (before re-compute)
- `get_all_file_tags()` — return all unique file tags with counts
- `get_files_by_tag(archive_id, tag)` — return files in an archive matching a tag

### 1.3 API Changes

**New endpoints:**
- `GET /api/files/<file_id>/tags` — return file tags (auto + user)
- `POST /api/files/<file_id>/tags` — add user tag to file
- `DELETE /api/files/<file_id>/tags/<tag>` — remove user tag (refuses if auto)
- `GET /api/tags/all` — return all tags (archive + file level) with counts and hierarchy
- `POST /api/archives/<archive_id>/auto-tag` — trigger auto-tagging for an archive

**Modify existing:**
- `GET /api/archives/<archive_id>/tags` — include `auto` flag on each tag
- `DELETE /api/archives/<archive_id>/tags/<tag>` — refuse if `auto=1`

### 1.4 Auto-Tag Trigger Points

- **On scan completion:** After an archive scan finishes (new files discovered), run `auto_tag_archive(archive_id)` for the scanned archive.
- **On archive add:** After an archive is first added and its file list is populated, run auto-tagging.
- **Manual re-tag:** User can trigger re-tagging from archive detail (button or menu). This clears auto tags and re-parses.
- **On file rename detection:** If a scan detects renamed files, re-tag those files.

### 1.5 Tag UI Changes

**Archive detail — archive-level tags (`archive-tags-row`):**
- Auto tags render without an × button, visually distinct (e.g. slightly muted background, no remove affordance)
- User-added tags render with an × button to remove
- Tag input remains the same — typed tags are always `auto=0`
- Tags display with parent:child formatting: parent in muted text, child in normal text (e.g. `region:` in grey, `japan` in white)

**Archive detail — file-level tags:**
- On double-clicking a file row, the expanded detail panel (currently shows PROCESSED OUTPUT section) gains a new section above it: **Tags**
- Shows the file's tags (inherited + own), with inherited tags visually marked (e.g. italic or muted label "inherited")
- Auto tags shown without × button
- User tags shown with × button
- An "Add tag…" text input at the end for adding user tags to the file
- Tags are comma-separated in the input, sanitised on submit (trim leading spaces, split on comma)

### 1.6 Tag Sanitisation Rules

Applied everywhere tags are created (UI input, auto-tagger, API):
- Lowercase the entire tag
- Trim leading/trailing whitespace
- Replace internal whitespace sequences with single `_`
- Strip characters that would cause filesystem conflicts: `/`, `\`, `:` is allowed (it's the parent:child separator), `<`, `>`, `|`, `*`, `?`, `"`
- Collapse multiple `_` into single `_`
- Max length: 64 characters
- Empty string after sanitisation → reject

---

## Part 2: Collections Overhaul

### 2.1 Layout System Redesign

Replace the current flat layout type system with a recursive folder tree model.

**Current layout types:** `flat`, `alphabetical`, `by_archive`
**New model:** Each layout is a tree of **folder nodes**. A layout has a single root node.

**New table: `collection_layout_nodes`**
```sql
CREATE TABLE IF NOT EXISTS collection_layout_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layout_id INTEGER NOT NULL,
    parent_id INTEGER DEFAULT NULL,  -- NULL = root node
    position INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,              -- display name (user-renamable)
    type TEXT NOT NULL,              -- 'all', 'alphabetical', 'tag_parent', 'tag_value', 'custom'
    tag_filter TEXT DEFAULT NULL,    -- for tag_parent: the parent tag (e.g. 'region')
                                    -- for tag_value: the full tag (e.g. 'region:japan')
    sort_mode TEXT NOT NULL DEFAULT 'flat',  -- 'flat' or 'alphabetical'
    FOREIGN KEY (layout_id) REFERENCES collection_layouts(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES collection_layout_nodes(id) ON DELETE CASCADE
);
```

**Node types:**

| Type | Behaviour | Children |
|------|-----------|----------|
| `all` | Shows all files (respecting parent scope). Default root. | None (leaf) |
| `alphabetical` | Auto-generates A-Z + # sub-folders | Auto-generated (not stored) |
| `tag_parent` | Auto-populates with one child folder per child tag value. E.g. `tag_filter='region'` creates folders for japan, usa, europe, etc. based on files that actually have those tags. | Auto-generated from tag values found in the collection's files |
| `tag_value` | Shows files matching a specific tag. E.g. `tag_filter='region:japan'` | None (leaf) |
| `custom` | User-defined folder. Contains other nodes (tag_parent, tag_value, custom, etc.). No automatic file population — only shows files from children. | User-configured children |

**Each node has `sort_mode`:** `flat` (all files in one list) or `alphabetical` (A-Z sub-buckets within that node's files).

**Each node has a `name`** that defaults to a sensible value (the tag value, "All", "A-Z", etc.) but can be renamed by the user. Names are sanitised for filesystem safety.

**Migration path:**
- Existing `flat` layouts → root node type `all`, sort_mode `flat`
- Existing `alphabetical` layouts → root node type `alphabetical`
- Existing `by_archive` layouts → root node type `tag_parent` with `tag_filter='archive'` (special built-in pseudo-tag)

### 2.2 Layout Tree Evaluation (collection_sync.py)

The sync engine walks the node tree recursively to build the directory structure.

```
evaluate_node(node, available_files) -> dict of { relative_path: [files] }
```

- **`all` node:** Assigns all `available_files` to this node's directory. Apply `sort_mode`.
- **`alphabetical` node:** Splits `available_files` into A-Z + # buckets, each as a subdirectory.
- **`tag_parent` node:** Groups `available_files` by values of the parent tag. E.g. for `region`, creates subdirs `japan/`, `usa/`, etc. Files with no matching tag go into an `_untagged/` folder (optional, configurable). Each auto-generated subfolder respects the node's `sort_mode`.
- **`tag_value` node:** Filters `available_files` to only those with the matching tag. Apply `sort_mode`.
- **`custom` node:** Doesn't directly contain files. Its children are evaluated, each getting a subdirectory. Files not matched by any child node can optionally go into an `_other/` folder.

**File tag resolution for layout evaluation:**
Files need their tags resolved at sync time. This means:
1. Load all file tags for the collection's archives (bulk query)
2. Compute inherited tags (archive-level + group-level)
3. Build a `file_id -> set(tags)` lookup
4. Pass this lookup into the tree evaluation

### 2.3 Collection Detail Page Redesign (`page-collection-detail`)

**Overall layout change:**

```
┌─────────────────────────────────────────────────────────┐
│ ← Back    Collection Name                               │
│ 1,234 files • processed • auto-tag: xbox                │
│                                                         │
│ [Sync] [Edit] [Delete]                                  │
│                                                         │
│ ▸ Archives (12)                    [Add Archives]       │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ (collapsed by default, expands to show archive list)│ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Layout: [▼ All > Flat          ]   [+ Add Layout] [⚙]  │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ ▸ japan/                              (142 files)   │ │
│ │ ▸ usa/                                (891 files)   │ │
│ │ ▸ europe/                             (234 files)   │ │
│ │   _untagged/                           (12 files)   │ │
│ │                                                     │ │
│ │         (virtual scroll preview tree)               │ │
│ │                                                     │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Sync status: Last synced 2 minutes ago — 1,234 links    │
└─────────────────────────────────────────────────────────┘
```

**Key changes:**

1. **Archives section** — moved to top, collapsed by default. A collapsible `<details>`-style dropdown showing "▸ Archives (12)". Clicking expands to reveal the archive list with add/remove controls. This de-emphasises archives since most users set them once.

2. **Layout selector** — replaces the layout cards section. A single dropdown (`<select>`) listing all layouts for this collection. Changing selection loads that layout's preview. Next to it:
   - [+ Add Layout] button — opens layout creation flow
   - [⚙ Edit Layout] button — opens layout tree editor for the selected layout

3. **Preview tree** — the virtual scroll area is expanded to show the full nested folder structure of the currently selected layout. Folders are collapsed by default. Expanding a folder shows its contents (sub-folders and files). The currently selected layout's folders and files are highlighted. The tree uses indentation to show nesting depth.

### 2.4 Layout Tree Editor

When the user clicks the ⚙ button next to the layout dropdown, a modal or inline editor opens for configuring the layout's folder tree.

**Editor UI:**

```
Layout: "By Region"

├── 📁 all (All Files)                    [flat ▼]  [✕]
│   Sort: [flat ▼]
│
├── 📁 region (tag: region)               [flat ▼]  [✕]
│   │  Auto-populates: japan/, usa/, europe/, ...
│   │  Sort: [flat ▼]  ☐ Include untagged folder
│   │
│   └── Rename: japan → "Japan"
│       Rename: usa → "United States"
│
├── 📁 Custom Folder                      [flat ▼]  [✕]
│   └── [+ Add sub-folder]
│
└── [+ Add folder]
    Type: [All ▼] [Alphabetical ▼] [By Tag ▼] [Custom ▼]
    Tag: [region ▼]  (shown if "By Tag" selected)
```

**Editor actions:**
- Add a folder node (choose type, configure tag if applicable)
- Remove a folder node
- Rename a folder node
- Change sort mode (flat / alphabetical)
- Reorder folder nodes (drag or up/down buttons)
- For `tag_parent` nodes: optionally rename auto-generated child folders
- For `tag_parent` nodes: toggle "Include untagged" folder

**Rename storage:** Tag-based folder renames are stored in a JSON column on the node:
```sql
ALTER TABLE collection_layout_nodes ADD COLUMN renames_json TEXT DEFAULT NULL;
-- e.g. {"japan": "Japan", "usa": "United States"}
```

### 2.5 Preview Tree Enhancements

The virtual scroll preview tree needs to support:

1. **Nested folders** — rows can be folder nodes at various depths, with proper indentation
2. **Expand/collapse** — clicking a folder toggles its children's visibility
3. **File counts** — each folder shows its file count in parentheses
4. **Highlighting** — files and folders belonging to the currently selected layout are visually distinguished
5. **Folder icons** — closed folder ▸, open folder ▾, file icon 📄

**Row types for the preview:**
- `folder` — a directory node (expandable, shows child count)
- `file` — a file entry (leaf, shows name + archive + size)

**Data structure from API:**
```json
{
  "tree": [
    {
      "type": "folder",
      "name": "japan",
      "display_name": "Japan",
      "path": "japan",
      "file_count": 142,
      "children": [
        { "type": "file", "name": "game.chd", "archive": "romset-1", "size": 524288000 },
        ...
      ]
    },
    ...
  ],
  "total_files": 1234
}
```

The virtual scroll flattens this tree based on expanded state, similar to the current `cpExpandedDirs` approach but supporting arbitrary nesting depth.

---

## Part 3: Implementation Order

### Phase 1 — Tagging Foundation (no UI changes yet)
1. Create `auto_tagger.py` with `parse_file_tags()` and `load_tag_key()`
2. Add `file_tags` table and `auto` column to `archive_tags`
3. Add DB functions for file tags (add, remove, get, bulk get, clear auto)
4. Add API endpoints for file tags
5. Wire auto-tagger into scan completion and archive add flows
6. Write tests for tag parsing (parenthetical extraction, multi-value split, key lookup, dynamic patterns)

### Phase 2 — Tag UI
7. Update archive tag rendering to distinguish auto vs user tags (× button only on user tags)
8. Add file-level tag display to the file detail expand panel (above PROCESSED OUTPUT)
9. Add file tag input field
10. Add tag parent:child visual formatting (muted parent prefix)
11. Add inherited tag display (archive tags shown on files with "inherited" label)

### Phase 3 — Layout Tree Model
12. Create `collection_layout_nodes` table
13. Migrate existing layouts to node-based model (flat → all node, alphabetical → alphabetical node, by_archive → tag_parent with archive pseudo-tag)
14. Update `collection_sync.py` to evaluate node trees recursively
15. Update preview API to return nested tree structure
16. Add DB functions for node CRUD (add, update, delete, reorder)
17. Add API endpoints for node management

### Phase 4 — Collection Detail Page Redesign
18. Restructure `page-collection-detail` HTML:
    - Move archives to collapsible section at top
    - Replace layout cards with dropdown selector
    - Add layout edit button
19. Update collection detail JS:
    - Collapsible archives section
    - Layout dropdown population and selection
    - Preview tree rendering with nested folders
20. Build layout tree editor (modal or inline panel)
21. Update virtual scroll to handle arbitrary nesting depth
22. Add folder rename UI for tag-based nodes

### Phase 5 — Polish & Integration
23. Tag-based collection auto-population: collections can filter by file-level tags (not just archive-level auto_tag)
24. Sync engine integration testing with nested layouts
25. Performance testing with large collections (10k+ files)
26. CSS styling pass for new tag visuals and collection layout
27. Handle edge cases: empty tag folders, deeply nested trees, filesystem path length limits

---

## Design Constraints

- `archive_files` table remains a pure IA mirror — tags are stored in separate tables, never on `archive_files`
- Auto-generated tags are never editable/removable by users — only user-added tags have the × button
- Tag sanitisation is applied consistently everywhere (UI, API, auto-tagger)
- Layout folder names are sanitised for filesystem safety (no `/\:*?"<>|`)
- Symlinks remain relative for Docker/Unraid compatibility
- Virtual scroll performance must handle 50k+ file collections
- TAG_KEY.txt is the single source of truth for auto-tag mappings; users can edit it to customise
