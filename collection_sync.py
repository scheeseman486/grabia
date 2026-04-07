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

"""Symlink-based collection sync engine for Grabia.

Builds a folder of relative symlinks that point back into the downloads
directory, giving users virtual "collections" without duplicating files.

Directory layout (inside the container):
    /grabia/
    ├── downloads/          ← archive subdirectories with real files
    │   ├── xbox-redump-1/
    │   └── snes-roms/
    └── collections/        ← generated symlink trees
        └── Xbox/
            ├── All/        ← flat layout
            │   ├── Aardvark.zip → ../../../downloads/xbox-redump-1/Aardvark.zip
            │   └── Halo.zip     → ../../../downloads/xbox-redump-1/Halo.zip
            └── A-Z/        ← alphabetical layout
                ├── A/
                │   └── Aardvark.zip → ../../../../downloads/xbox-redump-1/Aardvark.zip
                └── H/
                    └── Halo.zip     → ../../../../downloads/xbox-redump-1/Halo.zip

All symlinks use *relative* paths so they resolve identically inside the
Docker container and on the Unraid host (same share), and work over SMB
with Samba's default ``follow symlinks = yes``.
"""

import json
import os
import re
import shutil
from collections import defaultdict

import database as db
from logger import log


# ── Helpers ──────────────────────────────────────────────────────────────

def get_collections_dir():
    """Return the absolute path to the collections root directory.

    Uses the ``collections_dir`` setting if set; otherwise places
    ``collections/`` as a sibling to the download directory.
    """
    explicit = db.get_setting("collections_dir")
    if explicit:
        return explicit
    download_dir = db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))
    return os.path.join(os.path.dirname(download_dir), "collections")


def get_download_dir():
    """Return the absolute path to the downloads root directory."""
    return db.get_download_dir()


def get_processed_dir():
    """Return the absolute path to the processed-files root directory."""
    return db.get_processed_dir()


def _resolve_filename(file_row, flatten=True):
    """Determine the filename that should appear in a collection.

    If the file has been processed and has a ``processed_filename``,
    use that.  Otherwise fall back to the original manifest ``name``.

    If the unit was expanded from ``processed_files_json``, the
    ``_extra_processed_path`` key overrides the normal resolution.

    When ``flatten`` is True (default), returns only the leaf name (no
    subdirectory components).  When False, returns the full relative path
    preserving the original archive directory structure.
    """
    extra = file_row.get("_extra_processed_path")
    if extra:
        raw = extra
    else:
        raw = file_row.get("processed_filename") or file_row["name"]
    return os.path.basename(raw) if flatten else raw


def _resolve_filepath(file_row, download_dir):
    """Return the absolute path to the real file on disk.

    Uses the full relative path (including subdirectories) to locate the
    file, even though ``_resolve_filename`` strips subdirectories for
    display purposes.

    If the unit was expanded from ``processed_files_json``, the
    ``_extra_processed_path`` key overrides the normal resolution.

    For processed files, checks the separate ``processed_dir`` first,
    falling back to the legacy location inside ``download_dir``.
    """
    identifier = file_row["archive_identifier"]
    # Expanded multi-output entry takes precedence
    extra = file_row.get("_extra_processed_path")
    if extra:
        return _resolve_processed_path(identifier, extra, download_dir)
    raw = file_row.get("processed_filename") or file_row["name"]
    if file_row.get("processed_filename"):
        return _resolve_processed_path(identifier, raw, download_dir)
    # Unprocessed file — always in download_dir
    return os.path.join(download_dir, identifier, raw)


def _resolve_processed_path(identifier, rel_path, download_dir):
    """Locate a processed file, checking ``processed_dir`` first.

    Processed outputs may live in either the dedicated processed directory
    (new layout) or alongside downloads (legacy).  This function checks the
    processed dir first and falls back to download_dir so both old and
    migrated archives work transparently.
    """
    processed_dir = get_processed_dir()
    candidate = os.path.join(processed_dir, identifier, rel_path)
    if os.path.exists(candidate):
        return candidate
    # Legacy fallback — processed file alongside downloads
    return os.path.join(download_dir, identifier, rel_path)


def _alphabetical_bucket(filename):
    """Return the single-character bucket for alphabetical layout.

    A-Z for alpha starts, ``#`` for everything else (numbers, symbols).
    """
    first = filename[0].upper() if filename else "#"
    if first.isalpha():
        return first
    return "#"


def _safe_name(name):
    """Sanitise a name for use as a directory component."""
    # Replace path separators and null bytes
    return re.sub(r'[\x00/\\]', '_', name)


# ── Core sync logic ─────────────────────────────────────────────────────

def _build_file_list(collection):
    """Build the master file list for a collection.

    Returns a list of dicts from ``db.get_collection_files`` — each row
    has all archive_files columns plus ``archive_identifier``.
    """
    return db.get_collection_files(collection["id"])


def _build_media_units(files, download_dir, flatten=True, use_media_units=True):
    """Collapse files sharing a ``media_root`` into single directory units.

    Returns a list of unit dicts:
      - Standalone files: ``{display_name, file_row, is_dir: False}``
      - Media root dirs:  ``{display_name, file_row, is_dir: True,
        target_dir, children: [file_row, ...]}``

    **Critical rule:** processed files are always standalone — ``media_root``
    is ignored when ``processed_filename`` is set, because the processor has
    already collapsed multi-file input into a single output.

    When ``use_media_units`` is False, media_root is ignored entirely and
    all files are treated as standalone.
    When ``flatten`` is False, display names preserve subdirectory structure.
    """
    units = []
    grouped = defaultdict(list)  # (identifier, media_root) → [file_rows]

    for f in files:
        root = f.get("media_root", "")
        is_processed = bool(f.get("processed_filename"))
        if use_media_units and root and not is_processed:
            grouped[(f["archive_identifier"], root)].append(f)
        else:
            # Primary entry (processed_filename or original name)
            units.append({
                "display_name": _resolve_filename(f, flatten=flatten),
                "file_row": f,
                "is_dir": False,
            })
            # Expand additional processed outputs from processed_files_json
            if is_processed and f.get("processed_files_json"):
                primary = f.get("processed_filename", "")
                try:
                    extra_files = json.loads(f["processed_files_json"])
                except (json.JSONDecodeError, TypeError):
                    extra_files = []
                for extra_path in extra_files:
                    if not extra_path or extra_path == primary:
                        continue
                    # Create a shallow copy with override key for path resolution
                    expanded = dict(f)
                    expanded["_extra_processed_path"] = extra_path
                    units.append({
                        "display_name": os.path.basename(extra_path) if flatten else extra_path,
                        "file_row": expanded,
                        "is_dir": False,
                    })

    for (identifier, root), group_files in grouped.items():
        units.append({
            "display_name": os.path.basename(root),
            "file_row": group_files[0],  # representative for archive_identifier etc.
            "is_dir": True,
            "target_dir": os.path.join(download_dir, identifier, root),
            "children": group_files,
        })

    return units


def _build_file_tag_lookup(collection_id, files=None):
    """Build a file_id -> set(tags) lookup for all files in a collection.

    Includes own file tags + inherited archive tags + group tags.
    Also adds a pseudo-tag 'archive:{identifier}' for by_archive grouping.

    If ``files`` is provided, uses that list instead of querying the DB again.
    """
    lookup = defaultdict(set)
    if files is None:
        files = db.get_collection_files(collection_id)
    if not files:
        return lookup

    # Collect all archive IDs and their identifiers
    archive_info = {}  # archive_id -> identifier
    file_ids = []
    for f in files:
        file_ids.append(f["id"])
        aid = f["archive_id"]
        if aid not in archive_info:
            archive_info[aid] = f.get("archive_identifier", "")
        # Add archive pseudo-tag
        lookup[f["id"]].add(f"archive:{f.get('archive_identifier', '')}")

    # Bulk load file tags
    file_tags = db.get_file_tags_bulk(file_ids)
    for fid, tags in file_tags.items():
        for t in tags:
            lookup[fid].add(t["tag"])

    # Inherited archive-level tags
    for aid, ident in archive_info.items():
        archive_tags = db.get_archive_tags(aid)
        atag_set = {t["tag"] for t in archive_tags}
        # Apply archive tags to all files of this archive
        for f in files:
            if f["archive_id"] == aid:
                lookup[f["id"]].update(atag_set)

    return lookup


def _evaluate_node(node, units, tag_lookup, renames=None):
    """Recursively evaluate a layout node tree.

    Returns a dict of {relative_path: [(display_name, unit), ...]}
    where relative_path is relative to this node's directory.
    """
    import json as _json

    node_type = node["type"]
    sort_mode = node.get("sort_mode", "flat")
    include_untagged = node.get("include_untagged", 1)

    # Parse renames
    try:
        node_renames = _json.loads(node.get("renames_json") or "{}") or {}
    except (ValueError, TypeError):
        node_renames = {}

    mapping = defaultdict(list)

    if node_type == "all":
        # All units go into this directory
        for unit in units:
            mapping[""].append((unit["display_name"], unit))

    elif node_type == "alphabetical":
        # A-Z + # buckets
        for unit in units:
            bucket = _alphabetical_bucket(unit["display_name"])
            mapping[bucket].append((unit["display_name"], unit))

    elif node_type == "tag_parent":
        tag_filter = node.get("tag_filter", "")
        # Group units by the child values of this parent tag
        for unit in units:
            fid = unit["file_row"]["id"]
            tags = tag_lookup.get(fid, set())
            matched = False
            for tag in tags:
                if ":" in tag:
                    parent, child = tag.split(":", 1)
                    if parent == tag_filter:
                        folder_name = node_renames.get(child, child)
                        mapping[_safe_name(folder_name)].append((unit["display_name"], unit))
                        matched = True
                elif tag_filter == "archive":
                    # Special case: archive pseudo-tag uses full value
                    pass  # handled by archive: prefix above
            # Special handling for archive: pseudo-tag
            if tag_filter == "archive":
                ident = unit["file_row"].get("archive_identifier", "unknown")
                folder_name = node_renames.get(ident, ident)
                mapping[_safe_name(folder_name)].append((unit["display_name"], unit))
                matched = True
            if not matched and include_untagged:
                mapping["_untagged"].append((unit["display_name"], unit))

    elif node_type == "tag_value":
        tag_filter = node.get("tag_filter", "")
        for unit in units:
            fid = unit["file_row"]["id"]
            tags = tag_lookup.get(fid, set())
            if tag_filter in tags:
                mapping[""].append((unit["display_name"], unit))

    elif node_type == "custom":
        # Custom node: evaluate children, each gets a subdirectory
        children = node.get("children", [])
        for child_node in children:
            child_name = _safe_name(child_node["name"])
            child_mapping = _evaluate_node(child_node, units, tag_lookup)
            for sub_path, entries in child_mapping.items():
                full_path = os.path.join(child_name, sub_path) if sub_path else child_name
                mapping[full_path].extend(entries)

    return mapping


def _evaluate_node_tree(layout, units, tag_lookup):
    """Evaluate a layout to produce a directory mapping.

    Priority order:
    1. New segment-based path template (if segments exist)
    2. Legacy node tree (if nodes exist)
    3. Legacy type-based mapping (flat/alphabetical/by_archive)

    Returns {relative_dir: [(display_name, unit), ...]}.
    """
    segments = layout.get("segments", [])
    layout_type = layout.get("layout_type") or layout.get("type", "")
    if segments:
        return _evaluate_segments(segments, units, tag_lookup)
    # Segment-type layouts with no segments = empty (user must add filters)
    if layout_type == "segments":
        return {}

    nodes = layout.get("nodes", [])
    if not nodes:
        return _compute_layout_mapping(layout, units)

    # Start with the root node(s)
    mapping = defaultdict(list)
    for root_node in nodes:
        node_mapping = _evaluate_node(root_node, units, tag_lookup)
        for path, entries in node_mapping.items():
            mapping[path].extend(entries)

    return mapping


def _evaluate_segments(segments, units, tag_lookup):
    """Evaluate a segment-based path template.

    Processes segments left-to-right.  Each segment either:
    - Filters the unit set (tag_specific, tag_group, hidden_filter)
    - Splits the unit set into subdirectories (tag_parent, alphabetical)
    - Adds a literal path component (literal)

    Returns {relative_dir: [(display_name, unit), ...]}.
    """
    # Start with all units in a single group at root path ""
    # Groups: list of (path_prefix, [units])
    groups = [("", list(units))]

    for seg in segments:
        stype = seg["segment_type"]
        sval = seg.get("segment_value") or ""
        visible = bool(seg.get("visible", 1))
        include_untagged = bool(seg.get("include_untagged", 0))

        new_groups = []

        if stype == "literal":
            # Add a fixed folder name to the path
            for path, group_units in groups:
                new_path = os.path.join(path, _safe_name(sval)) if visible else path
                new_groups.append((new_path, group_units))

        elif stype == "tag_parent":
            # Expand into child folders for each child value of this parent tag
            for path, group_units in groups:
                buckets = defaultdict(list)
                untagged = []
                for unit in group_units:
                    fid = unit["file_row"]["id"]
                    tags = tag_lookup.get(fid, set())
                    matched = False
                    # Special case: archive pseudo-tag
                    if sval == "archive":
                        ident = unit["file_row"].get("archive_identifier", "unknown")
                        buckets[ident].append(unit)
                        matched = True
                    else:
                        for tag in tags:
                            if ":" in tag:
                                parent, child = tag.split(":", 1)
                                if parent == sval:
                                    buckets[child].append(unit)
                                    matched = True
                    if not matched and include_untagged:
                        untagged.append(unit)
                for child_val, child_units in buckets.items():
                    child_path = os.path.join(path, _safe_name(child_val)) if visible else path
                    new_groups.append((child_path, child_units))
                if untagged:
                    untag_path = os.path.join(path, "_untagged") if visible else path
                    new_groups.append((untag_path, untagged))

        elif stype == "tag_specific":
            # Filter to units matching a specific tag (parent:child or plain tag)
            # Display folder name is the child portion
            for path, group_units in groups:
                filtered = []
                for unit in group_units:
                    fid = unit["file_row"]["id"]
                    tags = tag_lookup.get(fid, set())
                    if sval in tags:
                        filtered.append(unit)
                if filtered:
                    if visible:
                        # Show child portion as folder name
                        folder_name = sval.split(":", 1)[1] if ":" in sval else sval
                        new_path = os.path.join(path, _safe_name(folder_name))
                    else:
                        new_path = path
                    new_groups.append((new_path, filtered))

        elif stype == "tag_group":
            # OR-union of multiple tags. segment_value is JSON array or "+"-separated
            tag_list = _parse_tag_group(sval)
            for path, group_units in groups:
                filtered = []
                for unit in group_units:
                    fid = unit["file_row"]["id"]
                    tags = tag_lookup.get(fid, set())
                    # Match if any tag in the group matches (including parent:child expansion)
                    if _matches_tag_group(tags, tag_list):
                        filtered.append(unit)
                if filtered:
                    if visible:
                        folder_name = "+".join(tag_list)
                        new_path = os.path.join(path, _safe_name(folder_name))
                    else:
                        new_path = path
                    new_groups.append((new_path, filtered))

        elif stype == "hidden_filter":
            # Same as tag_group but always invisible
            tag_list = _parse_tag_group(sval)
            for path, group_units in groups:
                filtered = []
                for unit in group_units:
                    fid = unit["file_row"]["id"]
                    tags = tag_lookup.get(fid, set())
                    if _matches_tag_group(tags, tag_list):
                        filtered.append(unit)
                if filtered:
                    new_groups.append((path, filtered))

        elif stype == "alphabetical":
            # Split into A-Z + # buckets
            for path, group_units in groups:
                buckets = defaultdict(list)
                for unit in group_units:
                    bucket = _alphabetical_bucket(unit["display_name"])
                    buckets[bucket].append(unit)
                for bucket_name, bucket_units in buckets.items():
                    new_path = os.path.join(path, bucket_name) if visible else path
                    new_groups.append((new_path, bucket_units))

        else:
            # Unknown segment type — pass through
            new_groups = groups

        groups = new_groups

    # Convert groups to the standard mapping format
    mapping = defaultdict(list)
    for path, group_units in groups:
        for unit in group_units:
            mapping[path].append((unit["display_name"], unit))

    return mapping


def _parse_tag_group(value):
    """Parse a tag group value — either JSON array or '+'-separated string."""
    if not value:
        return []
    # Try JSON array first
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fall back to "+"-separated
    return [t.strip() for t in value.split("+") if t.strip()]


def _matches_tag_group(file_tags, tag_list):
    """Check if a file's tags match any tag in a group.

    Supports both exact matches and parent-tag matching:
    - "beta" matches the tag "beta" directly
    - "region" matches any tag starting with "region:" (parent match)
    - "region:japan" matches "region:japan" exactly
    """
    for tag in tag_list:
        if tag in file_tags:
            return True
        # Check if this is a parent tag (matches any child)
        prefix = tag + ":"
        if any(ft.startswith(prefix) for ft in file_tags):
            return True
    return False


def _compute_layout_mapping(layout, units):
    """Compute a mapping of ``{relative_dir: [(display_name, unit), ...]}``
    for a given layout type.

    Accepts media units (from ``_build_media_units``) rather than raw
    file rows.

    ``relative_dir`` is relative to the layout root, e.g. ``""`` for flat,
    ``"A"`` for alphabetical, ``"xbox-redump-1"`` for by_archive.
    """
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
            # Unknown layout type — treat as flat
            mapping[""].append((display_name, unit))

    return mapping


def _resolve_conflicts(mapping):
    """Detect duplicate display names within each directory bucket and
    resolve by prefixing the archive identifier.

    Modifies ``mapping`` in place — entries are ``(display_name, unit)``
    where ``display_name`` may be changed to ``[identifier] name``.

    Works with both standalone files and directory media units.

    Returns the number of conflicts resolved.
    """
    conflicts = 0
    for subdir, entries in mapping.items():
        # Group by display name
        by_name = defaultdict(list)
        for display_name, unit in entries:
            by_name[display_name].append(unit)

        # Rebuild entries, prefixing where there are clashes
        new_entries = []
        for display_name, units in by_name.items():
            if len(units) == 1:
                new_entries.append((display_name, units[0]))
            else:
                conflicts += len(units)
                for unit in units:
                    identifier = unit["file_row"]["archive_identifier"]
                    prefixed = f"[{identifier}] {display_name}"
                    new_entries.append((prefixed, unit))
        mapping[subdir] = new_entries

    return conflicts


def _compute_relative_symlink(link_path, target_path):
    """Compute the relative path from ``link_path`` to ``target_path``.

    Both paths must be absolute.  The result is suitable for
    ``os.symlink(result, link_path)``.
    """
    link_dir = os.path.dirname(link_path)
    return os.path.relpath(target_path, link_dir)


def sync_collection(collection_id):
    """Synchronise symlinks for one collection.

    1. Build master file list from DB (all archives × file scope).
    2. For each layout, compute directory structure and desired symlinks.
    3. Create missing symlinks, remove stale ones.
    4. Remove layout dirs that no longer exist in the collection config.

    Returns a stats dict::

        {
            "collection_id": int,
            "collection_name": str,
            "layouts": {
                "layout_name": {
                    "created": int,
                    "removed": int,
                    "unchanged": int,
                    "conflicts": int,
                    "errors": [],
                }
            },
            "total_created": int,
            "total_removed": int,
            "total_errors": int,
        }
    """
    collection = db.get_collection(collection_id)
    if not collection:
        return {"error": f"Collection {collection_id} not found"}

    collections_dir = get_collections_dir()
    download_dir = get_download_dir()
    coll_name = _safe_name(collection["name"])
    coll_dir = os.path.join(collections_dir, coll_name)

    layouts = db.get_collection_layouts(collection_id)
    if not layouts:
        return {"error": "Collection has no layouts configured"}

    flatten = bool(collection.get("flatten", 1))
    use_media_units = bool(collection.get("use_media_units", 1))

    files = _build_file_list(collection)
    units = _build_media_units(files, download_dir, flatten=flatten,
                               use_media_units=use_media_units)

    # Build tag lookup for node-based layouts
    tag_lookup = _build_file_tag_lookup(collection_id, files=files)

    stats = {
        "collection_id": collection_id,
        "collection_name": collection["name"],
        "layouts": {},
        "total_created": 0,
        "total_removed": 0,
        "total_errors": 0,
    }

    active_layout_dirs = set()

    for layout in layouts:
        layout_name = _safe_name(layout["name"])
        layout_dir = os.path.join(coll_dir, layout_name)
        active_layout_dirs.add(layout_name)

        layout_stats = {
            "created": 0,
            "removed": 0,
            "unchanged": 0,
            "conflicts": 0,
            "errors": [],
        }

        # Compute desired symlinks for this layout (node-based or legacy)
        mapping = _evaluate_node_tree(layout, units, tag_lookup)
        layout_stats["conflicts"] = _resolve_conflicts(mapping)

        # Build set of desired symlink paths (absolute) → (target_path, is_dir)
        desired = {}  # link_path → (target_path, is_dir)
        for subdir, entries in mapping.items():
            if subdir:
                # Apply _safe_name to each path component (nested paths may contain /)
                safe_parts = [_safe_name(p) for p in subdir.replace("\\", "/").split("/") if p]
                link_parent = os.path.join(layout_dir, *safe_parts) if safe_parts else layout_dir
            else:
                link_parent = layout_dir

            for display_name, unit in entries:
                link_path = os.path.join(link_parent, display_name)
                if unit["is_dir"]:
                    target_path = unit["target_dir"]
                else:
                    target_path = _resolve_filepath(unit["file_row"], download_dir)
                desired[link_path] = (target_path, unit["is_dir"])

        # Collect existing symlinks under this layout dir
        # Check both files and directories — directory symlinks appear in dirnames
        existing = set()
        if os.path.isdir(layout_dir):
            for dirpath, dirnames, filenames in os.walk(layout_dir):
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    if os.path.islink(full):
                        existing.add(full)
                # Directory symlinks: os.walk lists them in dirnames but
                # won't recurse into them (they're symlinks).  Check each.
                for dname in list(dirnames):
                    full = os.path.join(dirpath, dname)
                    if os.path.islink(full):
                        existing.add(full)
                        # Don't recurse into symlinked dirs
                        dirnames.remove(dname)

        # Remove stale symlinks (exist on disk but not in desired set)
        for link_path in existing - set(desired.keys()):
            try:
                os.unlink(link_path)
                layout_stats["removed"] += 1
            except OSError as e:
                layout_stats["errors"].append(f"Remove {link_path}: {e}")

        # Create or update symlinks
        for link_path, (target_path, is_dir) in desired.items():
            rel_target = _compute_relative_symlink(link_path, target_path)

            if os.path.islink(link_path):
                # Check if it already points to the right place
                current = os.readlink(link_path)
                if current == rel_target:
                    layout_stats["unchanged"] += 1
                    continue
                # Wrong target — remove and recreate
                try:
                    os.unlink(link_path)
                except OSError as e:
                    layout_stats["errors"].append(f"Update {link_path}: {e}")
                    continue

            # Ensure parent directory exists
            parent = os.path.dirname(link_path)
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                layout_stats["errors"].append(f"Mkdir {parent}: {e}")
                continue

            # Verify the target actually exists before linking
            if is_dir:
                if not os.path.isdir(target_path):
                    layout_stats["errors"].append(
                        f"Target dir missing: {target_path} (skipping symlink)"
                    )
                    continue
            else:
                if not os.path.isfile(target_path):
                    layout_stats["errors"].append(
                        f"Target missing: {target_path} (skipping symlink)"
                    )
                    continue

            try:
                os.symlink(rel_target, link_path)
                layout_stats["created"] += 1
            except OSError as e:
                layout_stats["errors"].append(f"Symlink {link_path}: {e}")

        # Clean up empty directories left after removal
        if os.path.isdir(layout_dir):
            _remove_empty_dirs(layout_dir)

        stats["layouts"][layout["name"]] = layout_stats
        stats["total_created"] += layout_stats["created"]
        stats["total_removed"] += layout_stats["removed"]
        stats["total_errors"] += len(layout_stats["errors"])

    # Remove layout directories that are no longer in the collection config
    if os.path.isdir(coll_dir):
        for entry in os.listdir(coll_dir):
            entry_path = os.path.join(coll_dir, entry)
            if os.path.isdir(entry_path) and entry not in active_layout_dirs:
                try:
                    shutil.rmtree(entry_path)
                    log.info("collections", "Removed stale layout dir: %s", entry_path)
                except OSError as e:
                    stats["total_errors"] += 1
                    log.error("collections", "Failed to remove stale layout dir %s: %s", entry_path, e)

    # If collection dir is now completely empty, remove it
    if os.path.isdir(coll_dir) and not os.listdir(coll_dir):
        try:
            os.rmdir(coll_dir)
        except OSError:
            pass

    log.info(
        "collections",
        "Synced collection '%s': %d created, %d removed, %d errors",
        collection["name"],
        stats["total_created"],
        stats["total_removed"],
        stats["total_errors"],
    )

    return stats


def delete_collection_files(collection_id):
    """Remove all symlink directories for a collection from disk.

    Called when a collection is deleted.
    """
    collection = db.get_collection(collection_id)
    if not collection:
        return

    collections_dir = get_collections_dir()
    coll_name = _safe_name(collection["name"])
    coll_dir = os.path.join(collections_dir, coll_name)

    if os.path.isdir(coll_dir):
        try:
            shutil.rmtree(coll_dir)
            log.info("collections", "Deleted collection directory: %s", coll_dir)
        except OSError as e:
            log.error("collections", "Failed to delete collection dir %s: %s", coll_dir, e)


def preview_collection(collection_id):
    """Compute what a collection sync would produce, without touching disk.

    Returns a flat list of rows suitable for the virtual-scroll preview UI::

        [
            {"type": "layout_header", "depth": 0, "name": "A-Z", "layout_type": "alphabetical"},
            {"type": "bucket_header", "depth": 1, "name": "A"},
            {"type": "file", "depth": 2, "display_name": "Ape Escape.chd",
             "is_dir": False, "archive_identifier": "redump-psx"},
            {"type": "dir_unit", "depth": 2, "display_name": "Armored Core",
             "is_dir": True, "archive_identifier": "redump-psx",
             "children": [{"name": "Armored Core.cue"}, ...]},
            ...
        ]

    Shares the same pipeline as ``sync_collection``:
    ``_build_file_list → _build_media_units → _compute_layout_mapping
    → _resolve_conflicts``, then serialises to flat rows.
    """
    collection = db.get_collection(collection_id)
    if not collection:
        return {"error": f"Collection {collection_id} not found"}

    download_dir = get_download_dir()
    layouts = db.get_collection_layouts(collection_id)
    if not layouts:
        return {"rows": [], "total": 0}

    flatten = bool(collection.get("flatten", 1))
    use_media_units = bool(collection.get("use_media_units", 1))

    files = _build_file_list(collection)
    units = _build_media_units(files, download_dir, flatten=flatten,
                               use_media_units=use_media_units)

    # Build tag lookup for node-based layouts
    tag_lookup = _build_file_tag_lookup(collection_id, files=files)

    rows = []

    for layout in layouts:
        lid = layout["id"]
        mapping = _evaluate_node_tree(layout, units, tag_lookup)
        _resolve_conflicts(mapping)

        rows.append({
            "type": "layout_header",
            "depth": 0,
            "name": layout["name"],
            "layout_type": layout["type"],
            "layout_id": lid,
        })

        # Sort buckets: alphabetical order for bucket names
        for subdir in sorted(mapping.keys()):
            entries = mapping[subdir]
            if subdir:
                # Support nested paths: split on / or os.sep for depth
                parts = [p for p in subdir.replace("\\", "/").split("/") if p]
                depth = len(parts)
                rows.append({
                    "type": "bucket_header",
                    "depth": depth,
                    "name": parts[-1],
                    "path": subdir,
                    "layout_id": lid,
                    "file_count": len(entries),
                })
                entry_depth = depth + 1
            else:
                entry_depth = 1

            # Sort entries by display name within each bucket
            for display_name, unit in sorted(entries, key=lambda e: e[0].lower()):
                if unit["is_dir"]:
                    children = []
                    for child in unit.get("children", []):
                        children.append({
                            "name": os.path.basename(child["name"]),
                            "size": child.get("size", 0),
                        })
                    rows.append({
                        "type": "dir_unit",
                        "depth": entry_depth,
                        "display_name": display_name,
                        "is_dir": True,
                        "archive_identifier": unit["file_row"]["archive_identifier"],
                        "children": sorted(children, key=lambda c: c["name"].lower()),
                        "layout_id": lid,
                    })
                else:
                    rows.append({
                        "type": "file",
                        "depth": entry_depth,
                        "display_name": display_name,
                        "is_dir": False,
                        "archive_identifier": unit["file_row"]["archive_identifier"],
                        "size": unit["file_row"].get("size", 0),
                        "layout_id": lid,
                    })

    return {"rows": rows, "total": len(rows)}


def _remove_empty_dirs(root):
    """Walk bottom-up and remove empty directories under ``root``.

    Does not remove ``root`` itself.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        else:
            # Re-check after children may have been removed
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
