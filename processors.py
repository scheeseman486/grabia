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

"""Post-download file processors (CHD, CISO, etc.)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from logger import log


# ---------------------------------------------------------------------------
# Memory-aware thread scaling for chdman
# ---------------------------------------------------------------------------

# -- chdman memory model (empirically measured, chdman 0.286) --
#
# The FLAC codec is responsible for virtually ALL of chdman's memory usage.
# With FLAC in the codec set, peak anonymous memory is ~2.7× the input file
# size (e.g. 20 GB for a 7.3 GB DVD ISO).  Without FLAC, memory usage is
# <100 MB regardless of file size or thread count.
#
# Measured on DVD ISOs:
#
#   Codec set             MK (4.5 GB)    DW6 (7.3 GB)   Output size
#   lzma,zlib,huff,flac   11,915 MB      20,418 MB      56.7%
#   lzma,zlib,huff            —              81 MB       ~58%
#   lzma alone                46 MB           —          57.9%
#   zstd alone                45 MB           —          58.4%
#   flac alone            11,921 MB           —          60.4%
#
# Dropping FLAC costs only ~1-2% compression ratio but reduces memory
# from ~20 GB to ~80 MB.  The default DVD compression is therefore
# lzma,zlib,huff (no FLAC).  FLAC-inclusive compression is available
# as the "maximum" preset for users with sufficient RAM.
#
_CHDMAN_FLAC_MEMORY_RATIO = 2.8   # peak_anonymous ≈ input_size × this (with FLAC)
_CHDMAN_MEMORY_HEADROOM_MB = 700  # reserve for Grabia, Flask, OS, other containers


def _get_disc_data_size(input_path):
    """Return total data size in bytes for a disc image input path.

    For .cue files, parses the sheet and sums the referenced BIN files.
    For .gdi files, sums the referenced track files.
    For everything else (.iso, .bin, .img), returns the file size directly.
    """
    if not input_path:
        return 0
    ext = os.path.splitext(input_path)[1].lower()
    input_dir = os.path.dirname(input_path) or "."

    if ext == ".cue":
        bins = _parse_cue_bins(input_path, input_dir)
        if bins:
            total = 0
            for b in bins:
                try:
                    total += os.path.getsize(b)
                except OSError:
                    pass
            return total
        # No bins found — fall through to direct size

    if ext == ".gdi":
        total = 0
        try:
            with open(input_path, "r", errors="replace") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        track_name = parts[4].strip('"')
                        track_path = os.path.join(input_dir, track_name)
                        try:
                            total += os.path.getsize(track_path)
                        except OSError:
                            pass
        except OSError:
            pass
        if total > 0:
            return total
        # No tracks found — fall through to direct size

    try:
        return os.path.getsize(input_path)
    except OSError:
        return 0


def _can_use_flac(disc_type, input_path):
    """Check whether there's enough memory to use FLAC compression.

    FLAC is the only chdman codec that needs significant RAM (~2.8× file size).
    Without FLAC, chdman uses <100 MB regardless of file size.

    Returns True if FLAC is safe to use, False if memory is insufficient
    (caller should fall back to non-FLAC codecs).
    """
    file_size_mb = _get_disc_data_size(input_path) / (1024 * 1024)

    estimated_peak = int(file_size_mb * _CHDMAN_FLAC_MEMORY_RATIO)
    needed = estimated_peak + _CHDMAN_MEMORY_HEADROOM_MB

    available_mb = _get_available_memory_mb()
    if available_mb is None:
        log.debug("proc", "chdman FLAC check: cannot detect memory limits; "
                  "estimated peak for %.0fMB %s file = %dMB, skipping FLAC",
                  file_size_mb, disc_type.upper(), estimated_peak)
        return False  # Can't verify — be safe, skip FLAC

    if available_mb >= needed:
        log.debug("proc", "chdman FLAC check: %.0fMB %s file, estimated peak "
                  "%dMB, available %dMB — FLAC OK",
                  file_size_mb, disc_type.upper(), estimated_peak, available_mb)
        return True

    log.debug("proc", "chdman FLAC check: %.0fMB %s file, estimated peak "
              "%dMB, available %dMB — dropping FLAC (insufficient memory)",
              file_size_mb, disc_type.upper(), estimated_peak, available_mb)
    return False


def _get_chdman_threads(user_setting, disc_type="dvd", input_path=None):
    """Return the number of chdman threads to use.

    Since chdman's peak memory is file-size-dominated (not thread-dependent),
    thread count is chosen purely for CPU efficiency.  Memory checking is
    handled separately by _can_use_flac().

    When *user_setting* is 0 (auto), use half the available CPUs (good
    balance of speed vs leaving resources for Grabia/OS).
    When it's a positive integer, honour it directly.

    Returns an int >= 1.
    """
    if user_setting and int(user_setting) > 0:
        return int(user_setting)

    import multiprocessing
    cpu_count = multiprocessing.cpu_count()
    # Use half the CPUs, minimum 1, maximum 16 (diminishing returns beyond)
    threads = max(1, min(cpu_count // 2, 16))
    log.info("proc", "chdman threads: %d (cpus=%d)", threads, cpu_count)
    return threads


def _get_available_memory_mb():
    """Return available memory in MB, checking cgroup limits then system memory.

    Returns None if memory cannot be determined.
    """
    # 1. Check cgroup v2 memory limit (Docker with modern kernels)
    cgroup_limit = None
    try:
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            val = f.read().strip()
            if val != "max":
                cgroup_limit = int(val) // (1024 * 1024)
                log.debug("proc", "cgroup v2 memory.max: %dMB", cgroup_limit)
            else:
                log.debug("proc", "cgroup v2 memory.max: unlimited")
    except OSError:
        log.debug("proc", "cgroup v2 memory.max: not available")
    except ValueError:
        pass

    # 2. Check cgroup v1 memory limit (older Docker / Unraid)
    if cgroup_limit is None:
        try:
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                val = int(f.read().strip())
                # Very large values mean "no limit set"
                if val < 2**62:
                    cgroup_limit = val // (1024 * 1024)
                    log.debug("proc", "cgroup v1 memory.limit_in_bytes: %dMB", cgroup_limit)
                else:
                    log.debug("proc", "cgroup v1 memory.limit_in_bytes: unlimited (%d)", val)
        except OSError:
            log.debug("proc", "cgroup v1 memory.limit_in_bytes: not available")
        except ValueError:
            pass

    # 3. Get current memory usage within the cgroup
    cgroup_usage = None
    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            cgroup_usage = int(f.read().strip()) // (1024 * 1024)
    except (OSError, ValueError):
        pass
    if cgroup_usage is None:
        try:
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
                cgroup_usage = int(f.read().strip()) // (1024 * 1024)
        except (OSError, ValueError):
            pass

    if cgroup_limit is not None:
        available = cgroup_limit - (cgroup_usage or 0)
        log.debug("proc", "cgroup memory: limit=%dMB, usage=%sMB, available=%dMB",
                  cgroup_limit, cgroup_usage or "?", available)
        return max(available, 0)

    # 4. Fall back to system MemAvailable from /proc/meminfo
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB -> MB
    except (OSError, ValueError):
        pass

    return None


def _safe_relpath(rel, dest_dir):
    """Validate that a relative path stays within dest_dir when joined.

    Returns the normalised relative path, or None if it would escape.
    Rejects absolute paths, '..' traversal, and null bytes.
    """
    if not rel or "\x00" in rel:
        return None
    # Normalise: collapse redundant separators and resolve ..
    normed = os.path.normpath(rel)
    # Reject absolute paths or any leading .. that escapes the root
    if os.path.isabs(normed) or normed.startswith(".."):
        return None
    # Double-check with realpath against the actual destination
    full = os.path.realpath(os.path.join(dest_dir, normed))
    dest_real = os.path.realpath(dest_dir)
    if not (full == dest_real or full.startswith(dest_real + os.sep)):
        return None
    return normed

# Optional extraction libraries
try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False


# ---------------------------------------------------------------------------
# Registry of available processor types
# ---------------------------------------------------------------------------

_PROCESSOR_REGISTRY = {}


def register_processor(cls):
    """Class decorator to register a processor type."""
    _PROCESSOR_REGISTRY[cls.type_id] = cls
    return cls


def get_processor_types():
    """Return dict of type_id -> {label, description, options_schema}."""
    return {
        tid: {
            "label": cls.label,
            "description": cls.description,
            "options_schema": cls.options_schema + _COMMON_OPTIONS,
            "input_extensions": cls.input_extensions,
        }
        for tid, cls in _PROCESSOR_REGISTRY.items()
    }


def get_processor(type_id):
    """Return processor class for the given type_id, or None."""
    return _PROCESSOR_REGISTRY.get(type_id)


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

def _find_binary(name):
    """Find an external binary by name on $PATH via shutil.which."""
    path = shutil.which(name)
    if path:
        log.debug("tools", "%s: found on PATH at %s", name, path)
    else:
        log.debug("tools", "%s: not found", name)
    return path


def _get_binary_version(path, version_flag="--version"):
    """Run a binary with a version flag and capture the first line of output."""
    if not path:
        return None
    try:
        cmd = [path, version_flag] if version_flag else [path]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or result.stderr or "").strip()
        return output.split("\n")[0] if output else "unknown"
    except Exception:
        return None


def detect_tools():
    """Detect all external tools and return status dict."""
    tools = {}
    for name, version_flag in [
        ("chdman", "--help"),
        ("maxcso", "--version"),
        ("7z", "--help"),
        ("unrar", None),
    ]:
        path = _find_binary(name)
        version = None
        if path:
            version = _get_binary_version(path, version_flag)
        tools[name] = {"path": path, "version": version, "available": path is not None}

    # shitman: check multiple binary names (compiled binary, Python script, generic)
    shitman_path = _find_shitman()
    tools["shitman"] = {
        "path": shitman_path,
        "version": _get_binary_version(shitman_path, "--help") if shitman_path else None,
        "available": shitman_path is not None,
    }

    return tools


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

# Extensions we consider disc images
DISC_IMAGE_EXTS = {".iso", ".bin", ".cue", ".img", ".gdi", ".mdf", ".mds"}


def _list_archive_contents(archive_path):
    """List file names inside an archive without extracting.
    Returns a list of relative paths, or None if listing is not supported."""
    ext = os.path.splitext(archive_path)[1].lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                # Filter out directory entries
                return [n for n in zf.namelist() if not n.endswith("/")]
        elif ext == ".7z":
            if HAS_PY7ZR:
                with py7zr.SevenZipFile(archive_path, "r") as sz:
                    return [n for n in sz.getnames() if not n.endswith("/")]
            bin_path = _find_binary("7z")
            if bin_path:
                result = subprocess.run(
                    [bin_path, "l", "-slt", archive_path],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    # Parse -slt output: blocks of "Path = ...\nFolder = ..." etc.
                    # First Path entry is the archive itself, skip it.
                    files = []
                    current_path = None
                    current_is_dir = False
                    for line in result.stdout.splitlines():
                        if line.startswith("Path = "):
                            # Save previous entry
                            if current_path is not None and not current_is_dir:
                                files.append(current_path)
                            current_path = line[7:]
                            current_is_dir = False
                        elif line.startswith("Folder = +"):
                            current_is_dir = True
                    # Save last entry
                    if current_path is not None and not current_is_dir:
                        files.append(current_path)
                    # Skip first entry (archive path itself)
                    return files[1:] if len(files) > 1 else files
            return None
        elif ext == ".rar":
            bin_path = _find_binary("unrar")
            if bin_path:
                result = subprocess.run(
                    [bin_path, "lb", archive_path],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    return [l for l in result.stdout.strip().splitlines() if l]
            return None
        else:
            return None
    except Exception as e:
        log.debug("extract", "Failed to list contents of %s: %s", os.path.basename(archive_path), e)
        return None


def _archive_has_extensions(archive_path, extensions):
    """Check if an archive contains any files with the given extensions,
    without extracting. Returns True/False, or None if listing failed
    (caller should fall back to extracting)."""
    contents = _list_archive_contents(archive_path)
    if contents is None:
        return None
    log.debug("extract", "Peeked at %s: %d files", os.path.basename(archive_path), len(contents))
    for name in contents:
        if os.path.splitext(name)[1].lower() in extensions:
            return True
    log.debug("extract", "No matching extensions %s in %s, skipping extraction",
              sorted(extensions), os.path.basename(archive_path))
    return False


def _extract_archive(archive_path, dest_dir, cancel_check=None):
    """Extract a compressed archive (zip, 7z, rar) to dest_dir.
    Returns list of extracted file paths relative to dest_dir."""
    ext = os.path.splitext(archive_path)[1].lower()
    log.debug("extract", "Extracting %s (format: %s)", os.path.basename(archive_path), ext)

    if ext == ".zip":
        files = _extract_zip(archive_path, dest_dir)
    elif ext == ".7z":
        files = _extract_7z(archive_path, dest_dir)
    elif ext == ".rar":
        files = _extract_rar(archive_path, dest_dir)
    else:
        raise ProcessingError(f"Unsupported archive format: {ext}")
    log.debug("extract", "Extracted %d files from %s", len(files), os.path.basename(archive_path))
    for f in files:
        log.debug("extract", "  %s", f)
    return files


def _extract_zip(path, dest_dir):
    with zipfile.ZipFile(path, "r") as zf:
        safe_names = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            checked = _safe_relpath(info.filename, dest_dir)
            if checked is None:
                log.warning("extract", "Skipping unsafe zip entry: %s", info.filename)
                continue
            safe_names.append(info)
        for info in safe_names:
            zf.extract(info, dest_dir)
        return [_safe_relpath(i.filename, dest_dir) for i in safe_names]


def _extract_7z(path, dest_dir):
    if HAS_PY7ZR:
        with py7zr.SevenZipFile(path, "r") as sz:
            # Validate names before extracting
            unsafe = [n for n in sz.getnames() if _safe_relpath(n, dest_dir) is None]
            for u in unsafe:
                log.warning("extract", "Skipping unsafe 7z entry: %s", u)
            sz.extractall(dest_dir)
            # Return only safe entries, and remove any unsafe files that were extracted
            safe = []
            for n in sz.getnames():
                checked = _safe_relpath(n, dest_dir)
                if checked is not None:
                    safe.append(checked)
            return safe
    # Fallback to 7z binary
    bin_path = _find_binary("7z")
    if not bin_path:
        raise ProcessingError("No 7z extraction tool available (install py7zr or 7z)")
    result = subprocess.run(
        [bin_path, "x", "-y", f"-o{dest_dir}", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise ProcessingError(f"7z extraction failed: {(result.stderr or '')[:500]}")
    # Walk extracted files — only return safe paths
    extracted = []
    for root, _, files in os.walk(dest_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), dest_dir)
            if _safe_relpath(rel, dest_dir) is not None:
                extracted.append(rel)
    return extracted


def _extract_rar(path, dest_dir):
    bin_path = _find_binary("unrar")
    if not bin_path:
        raise ProcessingError("No RAR extraction tool available (install unrar)")
    result = subprocess.run(
        [bin_path, "x", "-y", "-o+", path, dest_dir + os.sep],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise ProcessingError(f"unrar extraction failed: {(result.stderr or '')[:500]}")
    # Walk extracted files — only return safe paths
    extracted = []
    for root, _, files in os.walk(dest_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), dest_dir)
            if _safe_relpath(rel, dest_dir) is not None:
                extracted.append(rel)
    return extracted


def find_disc_images(file_list, base_dir):
    """Given a list of relative paths, find disc image sets.
    Returns list of dicts: {type: 'cue'|'iso'|'gdi', files: [abs paths], name: base name}"""
    found = []
    seen_bases = set()

    # Priority 1: CUE sheets (implies BIN+CUE set)
    for rel in file_list:
        if rel.lower().endswith(".cue"):
            base = os.path.splitext(os.path.basename(rel))[0]
            if base.lower() in seen_bases:
                continue
            seen_bases.add(base.lower())
            cue_path = os.path.join(base_dir, rel)
            # Collect the CUE and any referenced BIN files
            bins = _parse_cue_bins(cue_path, base_dir)
            found.append({
                "type": "cue",
                "files": [cue_path] + bins,
                "cue_path": cue_path,
                "name": base,
            })

    # Priority 2: GDI files (Dreamcast)
    for rel in file_list:
        if rel.lower().endswith(".gdi"):
            base = os.path.splitext(os.path.basename(rel))[0]
            if base.lower() in seen_bases:
                continue
            seen_bases.add(base.lower())
            gdi_path = os.path.join(base_dir, rel)
            found.append({
                "type": "gdi",
                "files": [gdi_path],
                "gdi_path": gdi_path,
                "name": base,
            })

    # Priority 3: Standalone ISO files
    for rel in file_list:
        ext = os.path.splitext(rel)[1].lower()
        if ext in (".iso", ".img"):
            base = os.path.splitext(os.path.basename(rel))[0]
            if base.lower() in seen_bases:
                continue
            seen_bases.add(base.lower())
            iso_path = os.path.join(base_dir, rel)
            found.append({
                "type": "iso",
                "files": [iso_path],
                "iso_path": iso_path,
                "name": base,
            })

    if found:
        log.debug("disc", "Found %d disc image(s):", len(found))
        for d in found:
            log.debug("disc", "  %s [%s] — %d file(s)", d["name"], d["type"], len(d["files"]))
    else:
        log.debug("disc", "No disc images found in %d files", len(file_list))
    return found


def _parse_cue_bins(cue_path, base_dir):
    """Parse a CUE sheet and return absolute paths to referenced BIN/data files."""
    bins = []
    cue_dir = os.path.dirname(cue_path)
    base_real = os.path.realpath(base_dir)
    try:
        with open(cue_path, "r", errors="replace") as f:
            for line in f:
                m = re.match(r'^\s*FILE\s+"?([^"]+)"?\s+', line, re.IGNORECASE)
                if m:
                    bin_name = m.group(1)
                    bin_path = os.path.realpath(os.path.join(cue_dir, bin_name))
                    # Ensure the resolved path stays within the archive base directory
                    if not bin_path.startswith(base_real + os.sep):
                        log.warning("disc", "Skipping CUE reference outside archive dir: %s", bin_name)
                        continue
                    if os.path.isfile(bin_path):
                        bins.append(bin_path)
    except OSError:
        pass
    return bins


# ---------------------------------------------------------------------------
# Disc type detection (CD vs DVD)
# ---------------------------------------------------------------------------

# CD-ROM max capacity ~800 MB (overburn).  Anything larger is almost
# certainly a DVD (single-layer 4.7 GB, dual-layer 8.5 GB).
_CD_MAX_BYTES = 870_000_000  # generous upper bound for overburned CDs


def detect_disc_type(image_path, disc_info=None):
    """Detect whether a disc image is CD or DVD.

    Args:
        image_path: path to an ISO/IMG file, or a CUE/GDI file
        disc_info: optional dict from find_disc_images() with 'type' key

    Returns:
        "cd" or "dvd"
    """
    ext = os.path.splitext(image_path)[1].lower()
    fname = os.path.basename(image_path)

    # CUE/BIN and GDI are inherently CD formats
    if ext in (".cue", ".gdi"):
        log.debug("disc", "%s: detected as CD (format: %s)", fname, ext)
        return "cd"
    if disc_info and disc_info.get("type") in ("cue", "gdi"):
        log.debug("disc", "%s: detected as CD (disc_info type: %s)", fname, disc_info["type"])
        return "cd"
    # .bin without a CUE is treated as raw CD sector dump
    if ext == ".bin":
        log.debug("disc", "%s: detected as CD (standalone .bin)", fname)
        return "cd"

    # For ISO/IMG: inspect the file
    try:
        file_size = os.path.getsize(image_path)
    except OSError:
        log.debug("disc", "%s: cannot stat file, defaulting to DVD", fname)
        return "dvd"  # safe fallback for large/unknown

    size_mb = file_size / (1024 * 1024)
    log.debug("disc", "%s: size %.1f MB, checking sector layout", fname, size_mb)

    # Check for raw CD sectors (2352 bytes/sector).  The ISO 9660 Primary
    # Volume Descriptor sits at logical sector 16.  For 2352-byte sectors
    # the PVD magic "CD001" appears at offset 16*2352 + 16 = 37648.
    # For standard 2048-byte sectors it's at 16*2048 + 1 = 32769.
    try:
        with open(image_path, "rb") as f:
            # Check 2352-byte sector layout first (raw CD)
            f.seek(16 * 2352 + 16)
            if f.read(5) == b"CD001":
                log.debug("disc", "%s: detected as CD (2352-byte raw sectors)", fname)
                return "cd"
            # Check standard 2048-byte sector layout
            f.seek(16 * 2048 + 1)
            if f.read(5) == b"CD001":
                # Valid ISO 9660 — use size heuristic
                if file_size <= _CD_MAX_BYTES:
                    log.debug("disc", "%s: detected as CD (ISO 9660, %.1f MB <= threshold)", fname, size_mb)
                    return "cd"
                log.debug("disc", "%s: detected as DVD (ISO 9660, %.1f MB > threshold)", fname, size_mb)
                return "dvd"
    except OSError:
        pass

    # Fallback: size-only heuristic
    if file_size <= _CD_MAX_BYTES:
        return "cd"
    return "dvd"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProcessingError(Exception):
    """Raised when a processing step fails."""
    pass


class ProcessingCancelled(Exception):
    """Raised when processing is cancelled by the user."""
    pass


# ---------------------------------------------------------------------------
# Base processor
# ---------------------------------------------------------------------------

_COMMON_OPTIONS = [
    {
        "key": "delete_original",
        "label": "Delete original after processing",
        "type": "select",
        "default": "yes",
        "choices": [
            {"value": "yes", "label": "Yes"},
            {"value": "no", "label": "No — keep original file"},
        ],
    },
]


class BaseProcessor:
    """Base class for file processors."""

    type_id = None      # unique identifier, e.g. "chd_cd"
    label = None        # human-readable name
    description = None  # short description
    input_extensions = []  # file extensions this processor can handle
    options_schema = []    # list of {key, label, type, default, choices?}

    def __init__(self, options=None, cancel_check=None, progress_callback=None):
        self.options = options or {}
        self._cancel_check = cancel_check or (lambda: False)
        self._progress = progress_callback or (lambda **kw: None)

    def _check_cancel(self):
        if self._cancel_check():
            raise ProcessingCancelled("Processing cancelled")

    # Regex for chdman progress lines like "Compressing, 45.3% complete..."
    # or "Verifying, 100.0% complete..."
    _CHDMAN_PCT_RE = re.compile(r'(\d+(?:\.\d+)?)%\s*complete')

    def _run_chdman_with_progress(self, cmd, phase="converting", timeout=7200):
        """Run a chdman command, parsing stderr for progress percentages.

        Reports progress via self._progress(phase=phase, pct=XX.X) at most
        once per second. Returns (returncode, stderr_text).
        """
        import time
        self._check_cancel()
        log.debug("proc", "Running: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr_lines = []
        last_report = 0
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
                m = self._CHDMAN_PCT_RE.search(line)
                if m:
                    now = time.monotonic()
                    if now - last_report >= 1.0:
                        last_report = now
                        self._progress(phase=phase, pct=float(m.group(1)))
                        self._check_cancel()
            proc.wait(timeout=timeout)
        except ProcessingCancelled:
            proc.kill()
            proc.wait(timeout=5)
            raise
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise ProcessingError(f"chdman timed out after {timeout}s")
        return proc.returncode, "".join(stderr_lines)

    def can_process(self, filename):
        """Check if this processor can handle the given filename."""
        ext = os.path.splitext(filename)[1].lower()
        return ext in self.input_extensions

    def get_temp_dir(self, file_path):
        """Create a temp directory for processing, respecting user settings.

        Priority: 1) processing_temp_dir setting, 2) TMPDIR env var
        (set to /tempstorage in the Docker image), 3) alongside the file.
        """
        import database as db_mod
        temp_base = db_mod.get_setting("processing_temp_dir", "")
        if temp_base and os.path.isdir(temp_base):
            return tempfile.mkdtemp(prefix="grabia_proc_", dir=temp_base)
        # Honour TMPDIR (set to /tempstorage in the Docker image) so that
        # extraction temp files land on a real disk, not the container overlay.
        env_tmp = os.environ.get("TMPDIR", "")
        if env_tmp and os.path.isdir(env_tmp):
            return tempfile.mkdtemp(prefix="grabia_proc_", dir=env_tmp)
        # Last resort: temp dir alongside the file
        return tempfile.mkdtemp(prefix="grabia_proc_", dir=os.path.dirname(file_path))

    def process(self, file_path, download_dir):
        """Process a downloaded file.

        Args:
            file_path: absolute path to the downloaded file
            download_dir: base download directory for the archive

        Returns:
            dict with:
                processed_filename: new filename relative to download_dir (or original if unchanged)
                files_created: list of absolute paths to new files
                files_to_delete: list of absolute paths to remove
                skipped: True if file was not processable (e.g. not an archive containing disc images)
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CHD CD Processor
# ---------------------------------------------------------------------------

@register_processor
class CHDCDProcessor(BaseProcessor):
    type_id = "chd_cd"
    label = "CHD (CD)"
    description = "Convert CD images (BIN/CUE, ISO) inside archives to CHD using chdman createcd"
    input_extensions = [".zip", ".7z", ".rar", ".iso", ".bin"]
    options_schema = [
        {
            "key": "compression",
            "label": "Compression",
            "type": "select",
            "default": "default",
            "choices": [
                {"value": "default", "label": "Default (cdlz + cdzl + cdfl)"},
                {"value": "none", "label": "None"},
                {"value": "cdlz", "label": "LZMA (cdlz)"},
                {"value": "cdzl", "label": "Zlib (cdzl)"},
                {"value": "cdfl", "label": "FLAC (cdfl) — high memory usage"},
            ],
        },
        {
            "key": "num_processors",
            "label": "Threads",
            "type": "number",
            "default": 0,
            "description": "0 = auto (use all cores)",
        },
    ]

    def process(self, file_path, download_dir):
        fname = os.path.basename(file_path)
        log.info("proc", "CHD CD: processing %s", fname)
        chdman = _find_binary("chdman")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools (mame-tools package).")

        ext = os.path.splitext(file_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        # If it's already an ISO or BIN, process directly (no extraction needed)
        if ext in (".iso", ".bin"):
            log.debug("proc", "CHD CD: %s is a direct disc image, skipping extraction", fname)
            return self._convert_iso_direct(file_path, download_dir, chdman)

        # It's an archive — peek first, then extract if worthwhile
        has_images = _archive_has_extensions(file_path, DISC_IMAGE_EXTS | {".zip", ".7z", ".rar"})
        if has_images is False:
            log.info("proc", "CHD CD: %s — no disc images or nested archives found (peeked), skipping", fname)
            return {"skipped": True, "reason": "No disc images found in archive"}

        log.debug("proc", "CHD CD: %s is an archive, extracting to search for disc images", fname)
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            # Look for nested archives and extract those too
            extracted = self._handle_nested(extracted, temp_dir)

            disc_images = find_disc_images(extracted, temp_dir)
            if not disc_images:
                log.info("proc", "CHD CD: %s — no disc images found, skipping", fname)
                return {"skipped": True, "reason": "No disc images found in archive"}

            results = []
            for i, disc in enumerate(disc_images):
                self._check_cancel()
                self._progress(
                    phase="converting",
                    filename=disc["name"],
                    current=i + 1,
                    total=len(disc_images),
                )
                chd_name = disc["name"] + ".chd"
                chd_path = os.path.join(download_dir, chd_name)

                if disc["type"] == "cue":
                    self._run_chdman_createcd(chdman, disc["cue_path"], chd_path)
                elif disc["type"] == "gdi":
                    self._run_chdman_createcd(chdman, disc["gdi_path"], chd_path)
                elif disc["type"] == "iso":
                    self._run_chdman_createcd(chdman, disc["iso_path"], chd_path)

                results.append(chd_path)

            # Determine the primary output filename (relative to download_dir)
            if len(results) == 1:
                processed_filename = os.path.relpath(results[0], download_dir)
            else:
                # Multiple discs — use the first one as primary, they're all tracked
                processed_filename = os.path.relpath(results[0], download_dir)

            return {
                "processed_filename": processed_filename,
                "files_created": results,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            # Clean up temp dir
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_iso_direct(self, iso_path, download_dir, chdman):
        """Convert a standalone ISO/BIN directly to CHD."""
        base_name = os.path.splitext(os.path.basename(iso_path))[0]
        chd_path = os.path.join(download_dir, base_name + ".chd")

        self._progress(phase="converting", filename=os.path.basename(iso_path), current=1, total=1)
        self._run_chdman_createcd(chdman, iso_path, chd_path)

        return {
            "processed_filename": os.path.relpath(chd_path, download_dir),
            "files_created": [chd_path],
            "files_to_delete": [iso_path],
            "skipped": False,
        }

    def _handle_nested(self, file_list, base_dir):
        """Check for nested archives in extracted files and extract them too."""
        archive_exts = {".zip", ".7z", ".rar"}
        new_files = list(file_list)
        for rel in list(file_list):
            if _safe_relpath(rel, base_dir) is None:
                continue
            ext = os.path.splitext(rel)[1].lower()
            if ext in archive_exts:
                nested_path = os.path.join(base_dir, rel)
                nested_dir = os.path.join(base_dir, os.path.splitext(rel)[0])
                if _safe_relpath(os.path.splitext(rel)[0], base_dir) is None:
                    continue
                os.makedirs(nested_dir, exist_ok=True)
                try:
                    inner = _extract_archive(nested_path, nested_dir, self._cancel_check)
                    for inner_rel in inner:
                        combined = os.path.join(os.path.splitext(rel)[0], inner_rel)
                        if _safe_relpath(combined, base_dir) is not None:
                            new_files.append(combined)
                    os.remove(nested_path)
                    new_files.remove(rel)
                except ProcessingError:
                    pass  # Keep the nested archive as-is
        return new_files

    def _run_chdman_createcd(self, chdman, input_path, output_path):
        """Run chdman createcd with configured options."""
        compression = self.options.get("compression", "default")
        wants_flac = compression == "default" or "flac" in compression or "cdfl" in compression
        if wants_flac and not _can_use_flac("cd", input_path):
            # Fall back to non-FLAC CD codecs
            if compression == "default":
                compression = "cdlz,cdzl"
            else:
                # Strip FLAC/cdfl from a custom codec string
                parts = [c for c in compression.split(",") if c not in ("flac", "cdfl")]
                compression = ",".join(parts) if parts else "cdlz"
        cmd = [chdman, "createcd", "-i", input_path, "-o", output_path, "-f"]
        if compression != "default":
            cmd.extend(["-c", compression])
        threads = _get_chdman_threads(self.options.get("num_processors", 0))
        cmd.extend(["-np", str(threads)])

        rc, stderr = self._run_chdman_with_progress(cmd, phase="converting", timeout=7200)
        if rc != 0:
            err = (stderr or "").strip()
            log.error("proc", "chdman createcd failed (rc=%d): %s", rc, err[:500])
            raise ProcessingError(f"chdman createcd failed: {err[:500]}")

        log.debug("proc", "chdman createcd succeeded, verifying %s", os.path.basename(output_path))
        vrc, vstderr = self._run_chdman_with_progress(
            [chdman, "verify", "-i", output_path], phase="verifying", timeout=3600)
        if vrc != 0:
            os.remove(output_path)
            raise ProcessingError("CHD verification failed after conversion")


# ---------------------------------------------------------------------------
# CHD Auto Processor (auto-detects CD vs DVD)
# ---------------------------------------------------------------------------

@register_processor
class CHDAutoProcessor(BaseProcessor):
    type_id = "chd_auto"
    label = "CHD (Auto)"
    description = "Auto-detect CD or DVD and convert to CHD using the appropriate chdman command"
    input_extensions = [".zip", ".7z", ".rar", ".iso", ".bin", ".img"]
    # Maps preset value -> (cd_codecs, dvd_codecs)
    # cd_codecs passed to chdman createcd -c, dvd_codecs to createdvd -c
    #
    # NOTE: FLAC codec causes chdman to allocate ~2.7× the input file size
    # in memory (e.g. 20 GB for a 7.3 GB DVD ISO).  Without FLAC, memory
    # usage is <100 MB regardless of file size.  The "default" preset
    # therefore excludes FLAC; use "maximum" to include it if you have RAM.
    _COMPRESSION_PRESETS = {
        "default": ("cdlz,cdzl,cdfl", "lzma,zlib,huff"),
        "maximum": (None, None),  # chdman built-in (lzma+zlib+huff+flac) — needs ~2.7× file size in RAM
        "lzma":    ("cdlz", "lzma"),
        "zstd":    ("cdzl", "zstd"),
        "zlib":    ("cdzl", "zlib"),
        "flac":    ("cdfl", "flac"),
        "huff":    (None, "huff"),  # Huffman is DVD-only; CD falls back to default
        "none":    ("none", "none"),
    }

    options_schema = [
        {
            "key": "compression",
            "label": "Compression",
            "type": "select",
            "default": "default",
            "choices": [
                {"value": "default", "label": "Default — CD: cdlz+cdzl+cdfl / DVD: lzma+zlib+huff (low memory)"},
                {"value": "maximum", "label": "Maximum — includes FLAC, ~1-2% smaller but needs ~2.7× file size in RAM"},
                {"value": "lzma", "label": "LZMA — CD: cdlz / DVD: lzma (best ratio, slowest)"},
                {"value": "zstd", "label": "Zstd — CD: cdzl / DVD: zstd (fast, good ratio)"},
                {"value": "zlib", "label": "Zlib — CD: cdzl / DVD: zlib (balanced)"},
                {"value": "flac", "label": "FLAC — CD: cdfl / DVD: flac (WARNING: extreme memory usage)"},
                {"value": "huff", "label": "Huffman — DVD: huff (fastest, larger files)"},
                {"value": "none", "label": "None — no compression"},
            ],
        },
        {
            "key": "num_processors",
            "label": "Threads",
            "type": "number",
            "default": 0,
            "description": "0 = auto (use all cores)",
        },
    ]

    def _get_compression_args(self, disc_type):
        """Return the -c flag arguments for the chosen compression preset."""
        preset = self.options.get("compression", "default")
        mapping = self._COMPRESSION_PRESETS.get(preset, (None, None))
        codec = mapping[0] if disc_type == "cd" else mapping[1]
        return ["-c", codec] if codec else []

    def process(self, file_path, download_dir):
        fname = os.path.basename(file_path)
        log.info("proc", "CHD Auto: processing %s", fname)
        chdman = _find_binary("chdman")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools (mame-tools package).")

        ext = os.path.splitext(file_path)[1].lower()

        # Standalone disc image (not in an archive)
        if ext in (".iso", ".img", ".bin"):
            disc_type = detect_disc_type(file_path)
            log.debug("proc", "CHD Auto: %s is a direct disc image, detected as %s", fname, disc_type.upper())
            return self._convert_direct(file_path, download_dir, chdman, disc_type)

        # Archive — peek first, then extract if worthwhile
        has_images = _archive_has_extensions(file_path, DISC_IMAGE_EXTS | {".zip", ".7z", ".rar"})
        if has_images is False:
            log.info("proc", "CHD Auto: %s — no disc images or nested archives found (peeked), skipping", fname)
            return {"skipped": True, "reason": "No disc images found in archive"}

        log.debug("proc", "CHD Auto: %s is an archive, extracting to search for disc images", fname)
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            # Handle nested archives
            extracted = self._handle_nested(extracted, temp_dir)

            disc_images = find_disc_images(extracted, temp_dir)
            if not disc_images:
                log.info("proc", "CHD Auto: %s — no disc images found, skipping", fname)
                return {"skipped": True, "reason": "No disc images found in archive"}

            results = []
            for i, disc in enumerate(disc_images):
                self._check_cancel()
                self._progress(
                    phase="converting",
                    filename=disc["name"],
                    current=i + 1,
                    total=len(disc_images),
                )
                chd_name = disc["name"] + ".chd"
                chd_path = os.path.join(download_dir, chd_name)

                # Detect disc type from the primary image file
                if disc["type"] == "cue":
                    disc_type = "cd"  # CUE is always CD
                    input_path = disc["cue_path"]
                elif disc["type"] == "gdi":
                    disc_type = "cd"  # GDI is always CD
                    input_path = disc["gdi_path"]
                else:
                    input_path = disc["iso_path"]
                    disc_type = detect_disc_type(input_path, disc)

                self._progress(
                    phase="converting",
                    filename=f"{disc['name']} ({disc_type.upper()})",
                    current=i + 1,
                    total=len(disc_images),
                )

                if disc_type == "cd":
                    self._run_chdman_createcd(chdman, input_path, chd_path)
                else:
                    self._run_chdman_createdvd(chdman, input_path, chd_path)

                results.append(chd_path)

            processed_filename = os.path.relpath(results[0], download_dir)
            return {
                "processed_filename": processed_filename,
                "files_created": results,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_direct(self, file_path, download_dir, chdman, disc_type):
        """Convert a standalone disc image directly to CHD."""
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        chd_path = os.path.join(download_dir, base_name + ".chd")

        self._progress(
            phase="converting",
            filename=f"{os.path.basename(file_path)} ({disc_type.upper()})",
            current=1, total=1,
        )

        if disc_type == "cd":
            self._run_chdman_createcd(chdman, file_path, chd_path)
        else:
            self._run_chdman_createdvd(chdman, file_path, chd_path)

        return {
            "processed_filename": os.path.relpath(chd_path, download_dir),
            "files_created": [chd_path],
            "files_to_delete": [file_path],
            "skipped": False,
        }

    def _handle_nested(self, file_list, base_dir):
        """Check for nested archives and extract them."""
        archive_exts = {".zip", ".7z", ".rar"}
        new_files = list(file_list)
        for rel in list(file_list):
            if _safe_relpath(rel, base_dir) is None:
                continue
            ext = os.path.splitext(rel)[1].lower()
            if ext in archive_exts:
                nested_path = os.path.join(base_dir, rel)
                nested_dir = os.path.join(base_dir, os.path.splitext(rel)[0])
                if _safe_relpath(os.path.splitext(rel)[0], base_dir) is None:
                    continue
                os.makedirs(nested_dir, exist_ok=True)
                try:
                    inner = _extract_archive(nested_path, nested_dir, self._cancel_check)
                    for inner_rel in inner:
                        combined = os.path.join(os.path.splitext(rel)[0], inner_rel)
                        if _safe_relpath(combined, base_dir) is not None:
                            new_files.append(combined)
                    os.remove(nested_path)
                    new_files.remove(rel)
                except ProcessingError:
                    pass
        return new_files

    # Fallback codecs when FLAC is dropped due to insufficient memory
    _NO_FLAC_FALLBACK = {
        "maximum": ("cdlz,cdzl,cdfl", "lzma,zlib,huff"),  # same as "default"
        "flac":    ("cdlz", "lzma"),                        # fall back to LZMA
    }

    def _get_compression_args_checked(self, disc_type, input_path):
        """Return -c flag arguments, dropping FLAC if memory is insufficient."""
        preset = self.options.get("compression", "default")
        mapping = self._COMPRESSION_PRESETS.get(preset, (None, None))
        codec = mapping[0] if disc_type == "cd" else mapping[1]

        # Check if FLAC is involved (chdman built-in default includes FLAC)
        wants_flac = codec is None or "flac" in (codec or "") or "cdfl" in (codec or "")
        if wants_flac and not _can_use_flac(disc_type, input_path):
            fallback = self._NO_FLAC_FALLBACK.get(preset)
            if fallback:
                codec = fallback[0] if disc_type == "cd" else fallback[1]
            elif codec:
                # Custom codec string — strip FLAC variants
                parts = [c for c in codec.split(",") if c not in ("flac", "cdfl")]
                codec = ",".join(parts) if parts else ("cdlz" if disc_type == "cd" else "lzma")

        return ["-c", codec] if codec else []

    def _run_chdman_createcd(self, chdman, input_path, output_path):
        cmd = [chdman, "createcd", "-i", input_path, "-o", output_path, "-f"]
        cmd.extend(self._get_compression_args_checked("cd", input_path))
        threads = _get_chdman_threads(self.options.get("num_processors", 0))
        cmd.extend(["-np", str(threads)])

        rc, stderr = self._run_chdman_with_progress(cmd, phase="converting", timeout=7200)
        if rc != 0:
            err = (stderr or "").strip()
            log.error("proc", "chdman createcd failed (rc=%d): %s", rc, err[:500])
            raise ProcessingError(f"chdman createcd failed: {err[:500]}")

        self._verify_chd(chdman, output_path)

    def _run_chdman_createdvd(self, chdman, input_path, output_path):
        cmd = [chdman, "createdvd", "-i", input_path, "-o", output_path, "-f"]
        cmd.extend(self._get_compression_args_checked("dvd", input_path))
        threads = _get_chdman_threads(self.options.get("num_processors", 0))
        cmd.extend(["-np", str(threads)])

        rc, stderr = self._run_chdman_with_progress(cmd, phase="converting", timeout=14400)
        if rc != 0:
            err = (stderr or "").strip()
            log.error("proc", "chdman createdvd failed (rc=%d): %s", rc, err[:500])
            raise ProcessingError(f"chdman createdvd failed: {err[:500]}")

        self._verify_chd(chdman, output_path)

    def _verify_chd(self, chdman, chd_path):
        log.debug("proc", "Verifying %s", os.path.basename(chd_path))
        rc, stderr = self._run_chdman_with_progress(
            [chdman, "verify", "-i", chd_path], phase="verifying", timeout=7200)
        if rc != 0:
            log.error("proc", "CHD verification failed for %s", os.path.basename(chd_path))
            os.remove(chd_path)
            raise ProcessingError("CHD verification failed after conversion")
        log.debug("proc", "CHD verification passed for %s", os.path.basename(chd_path))


# ---------------------------------------------------------------------------
# CHD DVD Processor
# ---------------------------------------------------------------------------

@register_processor
class CHDDVDProcessor(BaseProcessor):
    type_id = "chd_dvd"
    label = "CHD (DVD)"
    description = "Convert DVD/large ISO images inside archives to CHD using chdman createdvd"
    input_extensions = [".zip", ".7z", ".rar", ".iso"]
    options_schema = [
        {
            "key": "compression",
            "label": "Compression",
            "type": "select",
            "default": "default",
            "choices": [
                {"value": "default", "label": "Default — lzma+zlib+huff (low memory)"},
                {"value": "maximum", "label": "Maximum — lzma+zlib+huff+flac (~1-2% smaller, needs ~2.7× file size in RAM)"},
                {"value": "none", "label": "None"},
                {"value": "lzma", "label": "LZMA"},
                {"value": "zstd", "label": "Zstd"},
                {"value": "zlib", "label": "Zlib"},
                {"value": "huff", "label": "Huffman"},
                {"value": "flac", "label": "FLAC (WARNING: extreme memory usage)"},
            ],
        },
        {
            "key": "num_processors",
            "label": "Threads",
            "type": "number",
            "default": 0,
            "description": "0 = auto (use all cores)",
        },
    ]

    def process(self, file_path, download_dir):
        chdman = _find_binary("chdman")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools (mame-tools package).")

        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".iso":
            return self._convert_iso(file_path, download_dir, chdman)

        # Peek at archive contents before extracting
        iso_exts = {".iso", ".img"}
        has_isos = _archive_has_extensions(file_path, iso_exts)
        if has_isos is False:
            return {"skipped": True, "reason": "No ISO images found in archive"}

        # Extract archive
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            # Find ISO files
            isos = [f for f in extracted if os.path.splitext(f)[1].lower() in iso_exts]
            if not isos:
                return {"skipped": True, "reason": "No ISO images found in archive"}

            results = []
            for i, rel in enumerate(isos):
                self._check_cancel()
                iso_path = os.path.join(temp_dir, rel)
                base_name = os.path.splitext(os.path.basename(rel))[0]
                chd_path = os.path.join(download_dir, base_name + ".chd")

                self._progress(phase="converting", filename=base_name, current=i + 1, total=len(isos))
                self._run_chdman_createdvd(chdman, iso_path, chd_path)
                results.append(chd_path)

            processed_filename = os.path.relpath(results[0], download_dir)
            return {
                "processed_filename": processed_filename,
                "files_created": results,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_iso(self, iso_path, download_dir, chdman):
        base_name = os.path.splitext(os.path.basename(iso_path))[0]
        chd_path = os.path.join(download_dir, base_name + ".chd")
        self._progress(phase="converting", filename=os.path.basename(iso_path), current=1, total=1)
        self._run_chdman_createdvd(chdman, iso_path, chd_path)
        return {
            "processed_filename": os.path.relpath(chd_path, download_dir),
            "files_created": [chd_path],
            "files_to_delete": [iso_path],
            "skipped": False,
        }

    # Maps preset names to chdman -c argument (None = chdman built-in default)
    _DVD_COMPRESSION = {
        "default": "lzma,zlib,huff",
        "maximum": None,  # chdman built-in: lzma+zlib+huff+flac
        "zstd": "zstd",
    }

    def _run_chdman_createdvd(self, chdman, input_path, output_path):
        compression = self.options.get("compression", "default")
        wants_flac = compression in ("maximum", "flac") or (
            compression not in self._DVD_COMPRESSION
            and "flac" in compression
        )
        codec = self._DVD_COMPRESSION.get(compression, compression)
        if wants_flac and not _can_use_flac("dvd", input_path):
            # Fall back to non-FLAC codecs
            if compression == "maximum":
                codec = "lzma,zlib,huff"
            elif compression == "flac":
                codec = "lzma"
            elif codec:
                parts = [c for c in codec.split(",") if c != "flac"]
                codec = ",".join(parts) if parts else "lzma"
        cmd = [chdman, "createdvd", "-i", input_path, "-o", output_path, "-f"]
        if codec:
            cmd.extend(["-c", codec])
        threads = _get_chdman_threads(self.options.get("num_processors", 0))
        cmd.extend(["-np", str(threads)])

        rc, stderr = self._run_chdman_with_progress(cmd, phase="converting", timeout=14400)
        if rc != 0:
            err = (stderr or "").strip()
            raise ProcessingError(f"chdman createdvd failed: {err[:500]}")

        vrc, vstderr = self._run_chdman_with_progress(
            [chdman, "verify", "-i", output_path], phase="verifying", timeout=7200)
        if vrc != 0:
            os.remove(output_path)
            raise ProcessingError("CHD verification failed after conversion")


# ---------------------------------------------------------------------------
# CISO Processor
# ---------------------------------------------------------------------------

@register_processor
class CISOProcessor(BaseProcessor):
    type_id = "ciso"
    label = "CISO/CSO"
    description = "Convert ISO images inside archives to compressed CSO using maxcso"
    input_extensions = [".zip", ".7z", ".rar", ".iso"]
    options_schema = [
        {
            "key": "block_size",
            "label": "Block size",
            "type": "select",
            "default": "default",
            "choices": [
                {"value": "default", "label": "Default (2048)"},
                {"value": "2048", "label": "2048"},
                {"value": "4096", "label": "4096"},
                {"value": "8192", "label": "8192"},
            ],
        },
    ]

    def process(self, file_path, download_dir):
        maxcso = _find_binary("maxcso")
        if not maxcso:
            raise ProcessingError("maxcso not found. Install maxcso.")

        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".iso":
            return self._convert_iso(file_path, download_dir, maxcso)

        # Peek at archive contents before extracting
        iso_exts = {".iso", ".img"}
        has_isos = _archive_has_extensions(file_path, iso_exts)
        if has_isos is False:
            return {"skipped": True, "reason": "No ISO images found in archive"}

        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            isos = [f for f in extracted if os.path.splitext(f)[1].lower() in iso_exts]
            if not isos:
                return {"skipped": True, "reason": "No ISO images found in archive"}

            results = []
            for i, rel in enumerate(isos):
                self._check_cancel()
                iso_path = os.path.join(temp_dir, rel)
                base_name = os.path.splitext(os.path.basename(rel))[0]
                cso_path = os.path.join(download_dir, base_name + ".cso")

                self._progress(phase="converting", filename=base_name, current=i + 1, total=len(isos))
                self._run_maxcso(maxcso, iso_path, cso_path)
                results.append(cso_path)

            processed_filename = os.path.relpath(results[0], download_dir)
            return {
                "processed_filename": processed_filename,
                "files_created": results,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_iso(self, iso_path, download_dir, maxcso):
        base_name = os.path.splitext(os.path.basename(iso_path))[0]
        cso_path = os.path.join(download_dir, base_name + ".cso")
        self._progress(phase="converting", filename=os.path.basename(iso_path), current=1, total=1)
        self._run_maxcso(maxcso, iso_path, cso_path)
        return {
            "processed_filename": os.path.relpath(cso_path, download_dir),
            "files_created": [cso_path],
            "files_to_delete": [iso_path],
            "skipped": False,
        }

    def _run_maxcso(self, maxcso, input_path, output_path):
        cmd = [maxcso, input_path, "-o", output_path]
        block_size = self.options.get("block_size", "default")
        if block_size != "default":
            cmd.extend(["--block", block_size])

        self._check_cancel()
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=7200)
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            raise ProcessingError(f"maxcso failed: {err[:500]}")


# ---------------------------------------------------------------------------
# Extract Processor
# ---------------------------------------------------------------------------

@register_processor
class ExtractProcessor(BaseProcessor):
    type_id = "extract"
    label = "Extract"
    description = "Extract archive contents without recompression"
    input_extensions = [".zip", ".7z", ".rar"]
    options_schema = []

    def process(self, file_path, download_dir):
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        # Peek to check for empty archive before extracting
        contents = _list_archive_contents(file_path)
        if contents is not None and len(contents) == 0:
            return {"skipped": True, "reason": "Archive is empty"}

        # Extract to a temp location first so we can inspect contents
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            if not extracted:
                return {"skipped": True, "reason": "Archive is empty"}

            # Output directly into the flat processed directory
            os.makedirs(download_dir, exist_ok=True)

            created = []
            all_relative = []  # paths relative to download_dir for DB tracking
            for i, rel in enumerate(extracted):
                self._check_cancel()
                self._progress(
                    phase="extracting",
                    filename=os.path.basename(rel),
                    current=i + 1,
                    total=len(extracted),
                )
                src = os.path.join(temp_dir, rel)
                if not os.path.isfile(src):
                    continue
                # Reject symlinks — they could point outside the directory
                if os.path.islink(src):
                    log.warning("extract", "Skipping symlink in extraction: %s", rel)
                    continue

                # Flatten into download_dir (use basename only to avoid
                # recreating archive subdirectory structure)
                dest_name = os.path.basename(rel)
                dest = os.path.join(download_dir, dest_name)
                # Handle name collisions by appending a suffix
                if os.path.exists(dest):
                    stem, ext = os.path.splitext(dest_name)
                    n = 1
                    while os.path.exists(dest):
                        dest_name = f"{stem}_{n}{ext}"
                        dest = os.path.join(download_dir, dest_name)
                        n += 1
                shutil.move(src, dest)
                created.append(dest)
                all_relative.append(dest_name)

            if not created:
                return {"skipped": True, "reason": "No files extracted"}

            # Primary output is the first extracted file
            processed_filename = all_relative[0]

            return {
                "processed_filename": processed_filename,
                "processed_files": all_relative if len(all_relative) > 1 else None,
                "files_created": created,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# BigPImage (Jaguar) Processor — uses shitman
# ---------------------------------------------------------------------------

def _find_shitman():
    """Locate the shitman binary or script.

    Checks for the compiled binary (shitman-linux-x64) first, then the
    Python script (shitman.py) which is used in Docker where Python is
    already available.
    """
    for name in ("shitman-linux-x64", "shitman-linux-arm64", "shitman.py", "shitman"):
        path = _find_binary(name)
        if path:
            return path
    return None


@register_processor
class BigPImageProcessor(BaseProcessor):
    type_id = "bigpimg"
    label = "BigPImage (Jaguar)"
    description = "Convert Jaguar CD images (BIN/CUE) to BigPEmu's .bigpimg format using shitman"
    input_extensions = [".zip", ".7z", ".rar", ".cue"]
    options_schema = [
        {
            "key": "subchannel",
            "label": "Subchannel data",
            "type": "select",
            "default": "no",
            "choices": [
                {"value": "no", "label": "No"},
                {"value": "yes", "label": "Yes — preserve subchannel data"},
            ],
        },
        {
            "key": "prepass",
            "label": "Deduplication pre-pass",
            "type": "select",
            "default": "no",
            "choices": [
                {"value": "no", "label": "No"},
                {"value": "yes", "label": "Yes — build sector dictionary"},
            ],
        },
    ]

    def process(self, file_path, download_dir):
        fname = os.path.basename(file_path)
        log.info("proc", "BigPImage: processing %s", fname)
        shitman = _find_shitman()
        if not shitman:
            raise ProcessingError(
                "shitman not found. Place shitman-linux-x64 or shitman.py on PATH."
            )

        ext = os.path.splitext(file_path)[1].lower()

        # Direct CUE file — convert without extraction
        if ext == ".cue":
            return self._convert_cue(shitman, file_path, download_dir)

        # Archive — peek, extract, find CUE files inside
        has_cues = _archive_has_extensions(file_path, {".cue", ".bin"})
        if has_cues is False:
            return {"skipped": True, "reason": "No CUE/BIN disc images found in archive"}

        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=fname)
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            # Find CUE sheets in extracted contents
            disc_images = find_disc_images(extracted, temp_dir)
            cue_discs = [d for d in disc_images if d["type"] == "cue"]
            if not cue_discs:
                return {"skipped": True, "reason": "No CUE/BIN disc images found in archive"}

            results = []
            for i, disc in enumerate(cue_discs):
                self._check_cancel()
                self._progress(
                    phase="converting",
                    filename=disc["name"],
                    current=i + 1,
                    total=len(cue_discs),
                )
                bigp_name = disc["name"] + ".bigpimg"
                bigp_path = os.path.join(download_dir, bigp_name)
                self._run_shitman(shitman, disc["cue_path"], bigp_path)
                results.append(bigp_path)

            processed_filename = os.path.relpath(results[0], download_dir)
            return {
                "processed_filename": processed_filename,
                "files_created": results,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_cue(self, shitman, cue_path, download_dir):
        """Convert a standalone CUE file to BigPImage."""
        base_name = os.path.splitext(os.path.basename(cue_path))[0]
        bigp_path = os.path.join(download_dir, base_name + ".bigpimg")
        self._progress(phase="converting", filename=os.path.basename(cue_path), current=1, total=1)
        self._run_shitman(shitman, cue_path, bigp_path)
        # Collect all BIN files referenced by the CUE for deletion
        cue_dir = os.path.dirname(cue_path) or "."
        bins = _parse_cue_bins(cue_path, cue_dir)
        files_to_delete = [cue_path] + bins
        return {
            "processed_filename": os.path.relpath(bigp_path, download_dir),
            "files_created": [bigp_path],
            "files_to_delete": files_to_delete,
            "skipped": False,
        }

    def _run_shitman(self, shitman, cue_path, output_path):
        """Run shitman to convert a CUE to BigPImage."""
        # Build command — if it's a .py script, run via Python interpreter
        if shitman.endswith(".py"):
            cmd = [sys.executable, shitman]
        else:
            cmd = [shitman]

        cmd.extend([cue_path, "-o", output_path, "--level", "9", "-v"])

        if self.options.get("subchannel") == "yes":
            cmd.append("--subchannel")
        if self.options.get("prepass") == "yes":
            cmd.append("--prepass")

        self._check_cancel()
        log.debug("proc", "Running: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        output_lines = []
        try:
            for line in proc.stdout:
                output_lines.append(line)
                # Parse progress from verbose output: "Processed 10000/50000 sectors..."
                if "Processed " in line and "/" in line:
                    try:
                        parts = line.strip().split("Processed ")[1].split("/")
                        current = int(parts[0])
                        total = int(parts[1].split()[0])
                        if total > 0:
                            pct = min(100, (current / total) * 100)
                            self._progress(phase="converting", pct=pct)
                    except (IndexError, ValueError):
                        pass
                self._check_cancel()
            proc.wait(timeout=7200)
        except ProcessingCancelled:
            proc.kill()
            proc.wait(timeout=5)
            raise
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise ProcessingError("shitman timed out after 7200s")

        if proc.returncode != 0:
            err = "".join(output_lines[-10:]).strip()
            raise ProcessingError(f"shitman failed (rc={proc.returncode}): {err[:500]}")
