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
    return db.get_setting("download_dir", os.path.expanduser("~/ia-downloads"))


def _resolve_filename(file_row):
    """Determine the filename that should appear in a collection.

    If the file has been processed and has a ``processed_filename``,
    use that.  Otherwise fall back to the original manifest ``name``.

    Returns only the leaf name (no subdirectory components) — the library
    flattens all archive directory structures into a single level.
    """
    raw = file_row.get("processed_filename") or file_row["name"]
    return os.path.basename(raw)


def _resolve_filepath(file_row, download_dir):
    """Return the absolute path to the real file on disk.

    Uses the full relative path (including subdirectories) to locate the
    file, even though ``_resolve_filename`` strips subdirectories for
    display purposes.
    """
    identifier = file_row["archive_identifier"]
    # Use full path for target resolution, not the flattened display name
    raw = file_row.get("processed_filename") or file_row["name"]
    return os.path.join(download_dir, identifier, raw)


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


def _build_media_units(files, download_dir):
    """Collapse files sharing a ``media_root`` into single directory units.

    Returns a list of unit dicts:
      - Standalone files: ``{display_name, file_row, is_dir: False}``
      - Media root dirs:  ``{display_name, file_row, is_dir: True,
        target_dir, children: [file_row, ...]}``

    **Critical rule:** processed files are always standalone — ``media_root``
    is ignored when ``processed_filename`` is set, because the processor has
    already collapsed multi-file input into a single output.
    """
    units = []
    grouped = defaultdict(list)  # (identifier, media_root) → [file_rows]

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
            "children": group_files,
        })

    return units


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

    files = _build_file_list(collection)
    units = _build_media_units(files, download_dir)

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

        # Compute desired symlinks for this layout
        mapping = _compute_layout_mapping(layout, units)
        layout_stats["conflicts"] = _resolve_conflicts(mapping)

        # Build set of desired symlink paths (absolute) → (target_path, is_dir)
        desired = {}  # link_path → (target_path, is_dir)
        for subdir, entries in mapping.items():
            if subdir:
                link_parent = os.path.join(layout_dir, _safe_name(subdir))
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

    files = _build_file_list(collection)
    units = _build_media_units(files, download_dir)

    rows = []

    for layout in layouts:
        mapping = _compute_layout_mapping(layout, units)
        _resolve_conflicts(mapping)

        rows.append({
            "type": "layout_header",
            "depth": 0,
            "name": layout["name"],
            "layout_type": layout["type"],
        })

        # Sort buckets: alphabetical order for bucket names
        for subdir in sorted(mapping.keys()):
            entries = mapping[subdir]
            if subdir:
                rows.append({
                    "type": "bucket_header",
                    "depth": 1,
                    "name": subdir,
                })
                entry_depth = 2
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
                    })
                else:
                    rows.append({
                        "type": "file",
                        "depth": entry_depth,
                        "display_name": display_name,
                        "is_dir": False,
                        "archive_identifier": unit["file_row"]["archive_identifier"],
                        "size": unit["file_row"].get("size", 0),
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
