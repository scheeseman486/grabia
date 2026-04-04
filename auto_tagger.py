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

Parses parenthetical and bracket tokens from ROM/media filenames and maps
them to normalised tags using TAG_KEY.txt as a lookup table.  Both static
mappings and user-editable regex patterns are loaded from TAG_KEY.txt.
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

_tag_key = None       # cached static lookup dict
_tag_patterns = None  # cached list of (compiled_regex, tag_template) tuples

# Regex to parse a pattern line:  /REGEX/FLAGS > tag_template
_RE_PATTERN_LINE = re.compile(r'^/(.+)/([a-zA-Z]*)\s+>\s+(.+)$')

# Regex to find template placeholders like {0}, {1}, {1:lower}
_RE_TEMPLATE_VAR = re.compile(r'\{(\d+)(?::(\w+))?\}')


def _build_regex_flags(flag_str):
    """Convert a flag string like 'i' to re module flags."""
    flags = 0
    for ch in flag_str:
        if ch == 'i':
            flags |= re.IGNORECASE
    return flags


def _expand_template(template, match):
    """Expand a tag template using regex match groups.

    Supports {0} for full match, {1}..{N} for capture groups,
    and modifiers like {1:lower}.
    """
    def replacer(m):
        idx = int(m.group(1))
        modifier = m.group(2)
        try:
            val = match.group(idx) if idx > 0 else match.group(0)
        except IndexError:
            val = ""
        if val is None:
            val = ""
        if modifier == "lower":
            val = val.lower()
        elif modifier == "upper":
            val = val.upper()
        return val

    return _RE_TEMPLATE_VAR.sub(replacer, template)


def load_tag_key(path=None):
    """Load TAG_KEY.txt into a case-insensitive lookup dict and regex patterns.

    Returns dict mapping lowercase pattern -> list of tag strings.
    Also populates the _tag_patterns list with (compiled_re, template) tuples.
    """
    global _tag_key, _tag_patterns
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "TAG_KEY.txt")

    lookup = {}
    patterns = []

    if not os.path.isfile(path):
        log.warning("TAG_KEY.txt not found at %s", path)
        _tag_key = lookup
        _tag_patterns = patterns
        return lookup

    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Check if this is a regex pattern line: /regex/flags > template
            pm = _RE_PATTERN_LINE.match(line)
            if pm:
                regex_str, flags_str, template = pm.group(1), pm.group(2), pm.group(3).strip()
                try:
                    compiled = re.compile(regex_str, _build_regex_flags(flags_str))
                    patterns.append((compiled, template))
                except re.error as e:
                    log.warning("TAG_KEY.txt line %d: bad regex /%s/: %s", lineno, regex_str, e)
                continue

            # Otherwise it's a static mapping: PATTERN > tag1, tag2
            if " > " not in line:
                continue
            pattern_part, tags_part = line.split(" > ", 1)
            pattern = pattern_part.strip().lower()
            tags = [sanitise_tag(t) for t in tags_part.split(",")]
            tags = [t for t in tags if t]
            if pattern and tags:
                lookup[pattern] = tags

    log.info("TAG_KEY loaded: %d static mappings, %d regex patterns", len(lookup), len(patterns))
    _tag_key = lookup
    _tag_patterns = patterns
    return lookup


def _get_tag_key():
    """Return cached tag key, loading if necessary."""
    global _tag_key
    if _tag_key is None:
        load_tag_key()
    return _tag_key


def _get_tag_patterns():
    """Return cached regex patterns, loading if necessary."""
    global _tag_patterns
    if _tag_patterns is None:
        load_tag_key()
    return _tag_patterns


# ---------------------------------------------------------------------------
# Dynamic pattern matching (from TAG_KEY.txt regex rules)
# ---------------------------------------------------------------------------

def _try_dynamic_match(token):
    """Try regex patterns from TAG_KEY.txt on a token.

    Returns list of tags or None.
    """
    patterns = _get_tag_patterns()
    for compiled_re, template in patterns:
        m = compiled_re.match(token)
        if m:
            expanded = _expand_template(template, m)
            tag = sanitise_tag(expanded)
            if tag:
                return [tag]
    return None


# ---------------------------------------------------------------------------
# Token extraction from filenames
# ---------------------------------------------------------------------------

# Match both parentheses (...) and square brackets [...]
_RE_PARENS = re.compile(r'\(([^)]+)\)')
_RE_BRACKETS = re.compile(r'\[([^\]]+)\]')


def _extract_tokens(filename):
    """Extract all parenthesised and bracketed token groups from a filename.

    Returns a flat list of individual tokens (comma-separated groups are split).
    """
    basename = os.path.splitext(os.path.basename(filename))[0]
    tokens = []

    # Parenthesised tokens: (USA, Europe) -> ["USA", "Europe"]
    for m in _RE_PARENS.finditer(basename):
        group = m.group(1)
        for part in group.split(","):
            part = part.strip()
            if part:
                tokens.append(part)

    # Square bracket tokens: [b] [!] [T+Eng] -> ["b", "!", "T+Eng"]
    for m in _RE_BRACKETS.finditer(basename):
        group = m.group(1)
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

    Parses parenthetical and bracket tokens and resolves them against
    TAG_KEY.txt static mappings and regex patterns.
    Returns a deduplicated list of tag strings.

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

        # 1. Exact match in static TAG_KEY
        if token_lower in key:
            for t in key[token_lower]:
                tags.add(t)
            continue

        # 2. Regex pattern match (from TAG_KEY.txt [Patterns] section)
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
    tagged_count = 0
    for f in files:
        if f.get("origin") != "manifest":
            continue
        file_tags = parse_file_tags(f["name"])
        for tag in file_tags:
            db.add_file_tag(f["id"], tag, auto=True)
        tagged_count += 1

    # Archive-level auto tags: only group membership, not file tag bubbling.
    # File tags belong to files — archive tags should describe the archive itself.
    if archive.get("group_id"):
        groups = db.get_groups() if hasattr(db, 'get_groups') else []
        group = next((g for g in groups if g["id"] == archive["group_id"]), None)
        if group:
            group_tag = sanitise_tag(f"group:{group['name']}")
            if group_tag:
                db.add_archive_tag(archive_id, group_tag, auto=True)

    log.info("Auto-tagged archive %d: %d files tagged",
             archive_id, tagged_count)


def auto_tag_files(file_ids):
    """Re-tag specific files by ID. Clears their auto tags and re-parses.

    Also refreshes archive-level auto tags for affected archives.
    Returns the number of files tagged.
    """
    import database as db

    if not file_ids:
        return 0

    affected_archives = set()
    tagged = 0

    for fid in file_ids:
        f = db.get_file(fid)
        if not f or f.get("origin") != "manifest":
            continue
        db.clear_auto_file_tags(fid)
        file_tags = parse_file_tags(f["name"])
        for tag in file_tags:
            db.add_file_tag(fid, tag, auto=True)
        affected_archives.add(f["archive_id"])
        tagged += 1

    # Recompute archive-level auto tags for each affected archive
    for aid in affected_archives:
        _refresh_archive_auto_tags(aid)

    return tagged


def _refresh_archive_auto_tags(archive_id):
    """Recompute archive-level auto tags (group tag only, no file tag bubbling)."""
    import database as db

    db.clear_auto_archive_tags(archive_id)

    archive = db.get_archive(archive_id)
    if not archive:
        return

    # Only group tag at archive level
    if archive.get("group_id"):
        groups = db.get_groups() if hasattr(db, 'get_groups') else []
        group = next((g for g in groups if g["id"] == archive["group_id"]), None)
        if group:
            group_tag = sanitise_tag(f"group:{group['name']}")
            if group_tag:
                db.add_archive_tag(archive_id, group_tag, auto=True)
