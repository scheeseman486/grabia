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

"""Auto-tagger — extracts structured tags from filenames.

Parses parenthetical tokens from ROM/media filenames and maps them to
normalised tags using TAG_KEY.txt as a lookup table plus dynamic pattern
matching for revisions, versions, dates, etc.
"""

import os
import re
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tag sanitisation
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[/\\<>|*?"]')
_MULTI_UNDERSCORE = re.compile(r'_+')


def sanitise_tag(tag):
    """Normalise and sanitise a tag string.

    - Lowercase
    - Strip leading/trailing whitespace
    - Replace internal whitespace with _
    - Remove filesystem-unsafe characters (except : for parent:child)
    - Collapse multiple underscores
    - Max 64 characters
    - Returns None if empty after sanitisation
    """
    if not tag:
        return None
    tag = tag.strip().lower()
    tag = re.sub(r'\s+', '_', tag)
    tag = _UNSAFE_CHARS.sub('', tag)
    tag = _MULTI_UNDERSCORE.sub('_', tag)
    tag = tag.strip('_')
    if len(tag) > 64:
        tag = tag[:64]
    return tag if tag else None


# ---------------------------------------------------------------------------
# TAG_KEY.txt loader
# ---------------------------------------------------------------------------

_tag_key = None  # cached lookup dict


def load_tag_key(path=None):
    """Load TAG_KEY.txt into a case-insensitive lookup dict.

    Returns dict mapping lowercase pattern -> list of tag strings.
    """
    global _tag_key
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "TAG_KEY.txt")
    lookup = {}
    if not os.path.isfile(path):
        log.warning("TAG_KEY.txt not found at %s", path)
        _tag_key = lookup
        return lookup
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if " > " not in line:
                continue
            pattern_part, tags_part = line.split(" > ", 1)
            pattern = pattern_part.strip().lower()
            tags = [sanitise_tag(t) for t in tags_part.split(",")]
            tags = [t for t in tags if t]
            if pattern and tags:
                lookup[pattern] = tags
    _tag_key = lookup
    return lookup


def _get_tag_key():
    """Return cached tag key, loading if necessary."""
    global _tag_key
    if _tag_key is None:
        load_tag_key()
    return _tag_key


# ---------------------------------------------------------------------------
# Dynamic pattern matchers
# ---------------------------------------------------------------------------

_RE_REV = re.compile(r'^rev\s+([a-z0-9]+)$', re.IGNORECASE)
_RE_VERSION = re.compile(r'^v(\d+(?:\.\d+)+)$', re.IGNORECASE)
_RE_ALT_NUM = re.compile(r'^alt\s+(\d+)$', re.IGNORECASE)
_RE_DATE_DASHED = re.compile(r'^(\d{4})-(\d{2})-([0-9xX]{2})$')
_RE_DATE_COMPACT = re.compile(r'^(\d{4})(\d{2})(\d{2})$')
_RE_DISC = re.compile(r'^dis[ck]\s+(\d+)$', re.IGNORECASE)
_RE_SIDE = re.compile(r'^side\s+([a-z])$', re.IGNORECASE)
_RE_TRACK = re.compile(r'^track\s+(\d+)$', re.IGNORECASE)


def _try_dynamic_match(token):
    """Try dynamic regex patterns on a token. Returns list of tags or None."""
    m = _RE_REV.match(token)
    if m:
        return [f"alt:rev_{m.group(1).lower()}"]

    m = _RE_VERSION.match(token)
    if m:
        return [f"version:{m.group(1)}"]

    m = _RE_ALT_NUM.match(token)
    if m:
        return [f"alt:{m.group(1)}"]

    m = _RE_DATE_DASHED.match(token)
    if m:
        return [f"date:{m.group(0).lower()}"]

    m = _RE_DATE_COMPACT.match(token)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        return [f"date:{year}-{month}-{day}"]

    m = _RE_DISC.match(token)
    if m:
        return [f"disc:{m.group(1)}"]

    m = _RE_SIDE.match(token)
    if m:
        return [f"side:{m.group(1).lower()}"]

    m = _RE_TRACK.match(token)
    if m:
        return [f"track:{m.group(1)}"]

    return None


# ---------------------------------------------------------------------------
# Parenthetical token extraction
# ---------------------------------------------------------------------------

_RE_PARENS = re.compile(r'\(([^)]+)\)')


def _extract_tokens(filename):
    """Extract all parenthesised token groups from a filename.

    Returns a flat list of individual tokens (comma-separated groups are split).
    """
    basename = os.path.splitext(os.path.basename(filename))[0]
    tokens = []
    for m in _RE_PARENS.finditer(basename):
        group = m.group(1)
        # Split comma-separated values: "USA, Europe" -> ["USA", "Europe"]
        for part in group.split(","):
            part = part.strip()
            if part:
                tokens.append(part)
    return tokens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file_tags(filename):
    """Extract structured tags from a filename.

    Parses parenthetical tokens and resolves them against TAG_KEY.txt
    and dynamic patterns. Returns a deduplicated list of tag strings.

    Args:
        filename: File name or path (only basename is used)

    Returns:
        list[str]: Sorted, deduplicated list of tags
    """
    key = _get_tag_key()
    tokens = _extract_tokens(filename)
    tags = set()

    for token in tokens:
        token_lower = token.strip().lower()

        # 1. Exact match in TAG_KEY
        if token_lower in key:
            for t in key[token_lower]:
                tags.add(t)
            continue

        # 2. Dynamic pattern match
        dynamic = _try_dynamic_match(token)
        if dynamic:
            for t in dynamic:
                tags.add(t)
            continue

        # 3. Unknown tag
        unknown = sanitise_tag(token)
        if unknown:
            tags.add(f"unknown:{unknown}")

    return sorted(tags)


def auto_tag_archive(archive_id):
    """Parse all files in an archive and store auto-generated tags.

    - Clears existing auto file tags for all files in the archive
    - Parses each file's name for tags
    - Stores file-level auto tags
    - Recomputes archive-level auto tags (union of all file tags + group tag)
    """
    import database as db

    archive = db.get_archive(archive_id)
    if not archive:
        return

    # Get all files for this archive
    files = db.get_archive_files_all(archive_id)
    if not files:
        return

    # Clear existing auto tags
    db.clear_auto_file_tags_for_archive(archive_id)
    db.clear_auto_archive_tags(archive_id)

    # Parse and store file-level tags
    all_tags = set()
    for f in files:
        if f.get("origin") != "manifest":
            continue
        file_tags = parse_file_tags(f["name"])
        for tag in file_tags:
            db.add_file_tag(f["id"], tag, auto=True)
            all_tags.add(tag)

    # Add group tag at archive level
    if archive.get("group_id"):
        groups = db.get_groups() if hasattr(db, 'get_groups') else []
        group = next((g for g in groups if g["id"] == archive["group_id"]), None)
        if group:
            group_tag = sanitise_tag(f"group:{group['name']}")
            if group_tag:
                db.add_archive_tag(archive_id, group_tag, auto=True)

    # Bubble up file tags to archive level
    for tag in all_tags:
        db.add_archive_tag(archive_id, tag, auto=True)

    log.info("Auto-tagged archive %d: %d unique tags from %d files",
             archive_id, len(all_tags), len(files))
