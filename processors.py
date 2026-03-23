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
import tempfile
import zipfile

from logger import log

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

def _find_binary(name, setting_key=None):
    """Find an external binary by name.  Checks settings override first,
    then falls back to PATH lookup via shutil.which."""
    import database as db
    if setting_key:
        custom = db.get_setting(setting_key, "")
        if custom and os.path.isfile(custom) and os.access(custom, os.X_OK):
            log.debug("tools", "%s: using custom path %s", name, custom)
            return custom
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
        result = subprocess.run(
            [path, version_flag],
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or result.stderr or "").strip()
        return output.split("\n")[0] if output else "unknown"
    except Exception:
        return None


def detect_tools():
    """Detect all external tools and return status dict."""
    tools = {}
    for name, setting_key, version_flag in [
        ("chdman", "tool_chdman_path", "--help"),
        ("maxcso", "tool_maxcso_path", "--version"),
        ("7z", "tool_7z_path", "--help"),
        ("unrar", "tool_unrar_path", "--version"),
    ]:
        path = _find_binary(name, setting_key)
        version = None
        if path:
            version = _get_binary_version(path, version_flag)
        tools[name] = {"path": path, "version": version, "available": path is not None}
    return tools


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

# Extensions we consider disc images
DISC_IMAGE_EXTS = {".iso", ".bin", ".cue", ".img", ".gdi", ".mdf", ".mds"}


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
        zf.extractall(dest_dir)
        return zf.namelist()


def _extract_7z(path, dest_dir):
    if HAS_PY7ZR:
        with py7zr.SevenZipFile(path, "r") as sz:
            sz.extractall(dest_dir)
            return sz.getnames()
    # Fallback to 7z binary
    bin_path = _find_binary("7z", "tool_7z_path")
    if not bin_path:
        raise ProcessingError("No 7z extraction tool available (install py7zr or 7z)")
    result = subprocess.run(
        [bin_path, "x", "-y", f"-o{dest_dir}", path],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise ProcessingError(f"7z extraction failed: {result.stderr[:500]}")
    # Walk extracted files
    extracted = []
    for root, _, files in os.walk(dest_dir):
        for f in files:
            extracted.append(os.path.relpath(os.path.join(root, f), dest_dir))
    return extracted


def _extract_rar(path, dest_dir):
    bin_path = _find_binary("unrar", "tool_unrar_path")
    if not bin_path:
        raise ProcessingError("No RAR extraction tool available (install unrar)")
    result = subprocess.run(
        [bin_path, "x", "-y", "-o+", path, dest_dir + os.sep],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise ProcessingError(f"unrar extraction failed: {result.stderr[:500]}")
    extracted = []
    for root, _, files in os.walk(dest_dir):
        for f in files:
            extracted.append(os.path.relpath(os.path.join(root, f), dest_dir))
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
    try:
        with open(cue_path, "r", errors="replace") as f:
            for line in f:
                m = re.match(r'^\s*FILE\s+"?([^"]+)"?\s+', line, re.IGNORECASE)
                if m:
                    bin_name = m.group(1)
                    bin_path = os.path.join(cue_dir, bin_name)
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

    def can_process(self, filename):
        """Check if this processor can handle the given filename."""
        ext = os.path.splitext(filename)[1].lower()
        return ext in self.input_extensions

    def get_temp_dir(self, file_path):
        """Create a temp directory for processing, respecting user settings."""
        import database as db_mod
        temp_base = db_mod.get_setting("processing_temp_dir", "")
        if temp_base and os.path.isdir(temp_base):
            return tempfile.mkdtemp(prefix="grabia_proc_", dir=temp_base)
        # Default: temp dir alongside the file
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
                {"value": "cdfl", "label": "FLAC (cdfl)"},
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
        chdman = _find_binary("chdman", "tool_chdman_path")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools or set the path in settings.")

        ext = os.path.splitext(file_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        # If it's already an ISO or BIN, process directly (no extraction needed)
        if ext in (".iso", ".bin"):
            log.debug("proc", "CHD CD: %s is a direct disc image, skipping extraction", fname)
            return self._convert_iso_direct(file_path, download_dir, chdman)

        # It's an archive — extract and find disc images
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
            ext = os.path.splitext(rel)[1].lower()
            if ext in archive_exts:
                nested_path = os.path.join(base_dir, rel)
                nested_dir = os.path.join(base_dir, os.path.splitext(rel)[0])
                os.makedirs(nested_dir, exist_ok=True)
                try:
                    inner = _extract_archive(nested_path, nested_dir, self._cancel_check)
                    for inner_rel in inner:
                        new_files.append(os.path.join(os.path.splitext(rel)[0], inner_rel))
                    os.remove(nested_path)
                    new_files.remove(rel)
                except ProcessingError:
                    pass  # Keep the nested archive as-is
        return new_files

    def _run_chdman_createcd(self, chdman, input_path, output_path):
        """Run chdman createcd with configured options."""
        cmd = [chdman, "createcd", "-i", input_path, "-o", output_path, "-f"]
        compression = self.options.get("compression", "default")
        if compression != "default":
            cmd.extend(["-c", compression])
        num_proc = int(self.options.get("num_processors", 0))
        if num_proc > 0:
            cmd.extend(["-np", str(num_proc)])

        log.debug("proc", "Running: %s", " ".join(cmd))
        self._check_cancel()
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=7200,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log.error("proc", "chdman createcd failed (rc=%d): %s", result.returncode, err[:500])
            raise ProcessingError(f"chdman createcd failed: {err[:500]}")

        log.debug("proc", "chdman createcd succeeded, verifying %s", os.path.basename(output_path))
        # Verify the output
        verify_result = subprocess.run(
            [chdman, "verify", "-i", output_path],
            capture_output=True, text=True, timeout=3600,
        )
        if verify_result.returncode != 0:
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
    _COMPRESSION_PRESETS = {
        "default": (None, None),  # chdman built-in defaults (tries all)
        "lzma":    ("cdlz", "lzma"),
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
                {"value": "default", "label": "Default — CD: cdlz+cdzl+cdfl / DVD: lzma+zlib+huff+flac"},
                {"value": "lzma", "label": "LZMA — CD: cdlz / DVD: lzma (best ratio, slowest)"},
                {"value": "zlib", "label": "Zlib — CD: cdzl / DVD: zlib (balanced)"},
                {"value": "flac", "label": "FLAC — CD: cdfl / DVD: flac (lossless audio, fast)"},
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
        chdman = _find_binary("chdman", "tool_chdman_path")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools or set the path in settings.")

        ext = os.path.splitext(file_path)[1].lower()

        # Standalone disc image (not in an archive)
        if ext in (".iso", ".img", ".bin"):
            disc_type = detect_disc_type(file_path)
            log.debug("proc", "CHD Auto: %s is a direct disc image, detected as %s", fname, disc_type.upper())
            return self._convert_direct(file_path, download_dir, chdman, disc_type)

        # Archive — extract and find disc images
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
            ext = os.path.splitext(rel)[1].lower()
            if ext in archive_exts:
                nested_path = os.path.join(base_dir, rel)
                nested_dir = os.path.join(base_dir, os.path.splitext(rel)[0])
                os.makedirs(nested_dir, exist_ok=True)
                try:
                    inner = _extract_archive(nested_path, nested_dir, self._cancel_check)
                    for inner_rel in inner:
                        new_files.append(os.path.join(os.path.splitext(rel)[0], inner_rel))
                    os.remove(nested_path)
                    new_files.remove(rel)
                except ProcessingError:
                    pass
        return new_files

    def _run_chdman_createcd(self, chdman, input_path, output_path):
        cmd = [chdman, "createcd", "-i", input_path, "-o", output_path, "-f"]
        cmd.extend(self._get_compression_args("cd"))
        num_proc = int(self.options.get("num_processors", 0))
        if num_proc > 0:
            cmd.extend(["-np", str(num_proc)])

        log.debug("proc", "Running: %s", " ".join(cmd))
        self._check_cancel()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log.error("proc", "chdman createcd failed (rc=%d): %s", result.returncode, err[:500])
            raise ProcessingError(f"chdman createcd failed: {err[:500]}")

        self._verify_chd(chdman, output_path)

    def _run_chdman_createdvd(self, chdman, input_path, output_path):
        cmd = [chdman, "createdvd", "-i", input_path, "-o", output_path, "-f"]
        cmd.extend(self._get_compression_args("dvd"))
        num_proc = int(self.options.get("num_processors", 0))
        if num_proc > 0:
            cmd.extend(["-np", str(num_proc)])

        log.debug("proc", "Running: %s", " ".join(cmd))
        self._check_cancel()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log.error("proc", "chdman createdvd failed (rc=%d): %s", result.returncode, err[:500])
            raise ProcessingError(f"chdman createdvd failed: {err[:500]}")

        self._verify_chd(chdman, output_path)

    def _verify_chd(self, chdman, chd_path):
        log.debug("proc", "Verifying %s", os.path.basename(chd_path))
        verify_result = subprocess.run(
            [chdman, "verify", "-i", chd_path],
            capture_output=True, text=True, timeout=7200,
        )
        if verify_result.returncode != 0:
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
                {"value": "default", "label": "Default"},
                {"value": "none", "label": "None"},
                {"value": "lzma", "label": "LZMA"},
                {"value": "zlib", "label": "Zlib"},
                {"value": "huff", "label": "Huffman"},
                {"value": "flac", "label": "FLAC"},
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
        chdman = _find_binary("chdman", "tool_chdman_path")
        if not chdman:
            raise ProcessingError("chdman not found. Install MAME tools or set the path in settings.")

        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".iso":
            return self._convert_iso(file_path, download_dir, chdman)

        # Extract archive
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            # Find ISO files
            isos = [f for f in extracted if os.path.splitext(f)[1].lower() in (".iso", ".img")]
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

    def _run_chdman_createdvd(self, chdman, input_path, output_path):
        cmd = [chdman, "createdvd", "-i", input_path, "-o", output_path, "-f"]
        compression = self.options.get("compression", "default")
        if compression != "default":
            cmd.extend(["-c", compression])
        num_proc = int(self.options.get("num_processors", 0))
        if num_proc > 0:
            cmd.extend(["-np", str(num_proc)])

        self._check_cancel()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise ProcessingError(f"chdman createdvd failed: {err[:500]}")

        verify_result = subprocess.run(
            [chdman, "verify", "-i", output_path],
            capture_output=True, text=True, timeout=7200,
        )
        if verify_result.returncode != 0:
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
        maxcso = _find_binary("maxcso", "tool_maxcso_path")
        if not maxcso:
            raise ProcessingError("maxcso not found. Install maxcso or set the path in settings.")

        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".iso":
            return self._convert_iso(file_path, download_dir, maxcso)

        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            isos = [f for f in extracted if os.path.splitext(f)[1].lower() in (".iso", ".img")]
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
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

        # Extract to a temp location first so we can inspect contents
        temp_dir = self.get_temp_dir(file_path)
        try:
            self._progress(phase="extracting", filename=os.path.basename(file_path))
            self._check_cancel()
            extracted = _extract_archive(file_path, temp_dir, self._cancel_check)
            self._check_cancel()

            if not extracted:
                return {"skipped": True, "reason": "Archive is empty"}

            # Determine output location:
            # Single file  -> extract alongside the archive
            # Multiple files -> extract into a subfolder named after the archive
            if len(extracted) == 1:
                dest_dir = download_dir
            else:
                dest_dir = os.path.join(download_dir, base_name)
                os.makedirs(dest_dir, exist_ok=True)

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

                # Preserve subdirectory structure from the archive
                dest = os.path.join(dest_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                created.append(dest)
                all_relative.append(os.path.relpath(dest, download_dir))

            if not created:
                return {"skipped": True, "reason": "No files extracted"}

            # Primary display name: the folder if multi-file, the file if single
            if len(extracted) == 1:
                processed_filename = all_relative[0]
            else:
                processed_filename = base_name + os.sep

            return {
                "processed_filename": processed_filename,
                "processed_files": all_relative,
                "files_created": created,
                "files_to_delete": [file_path],
                "skipped": False,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
