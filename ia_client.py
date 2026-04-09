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

"""Internet Archive API client.

Authentication: IA downloads of restricted items require cookie-based auth.
Cookies are obtained via the xauthn endpoint using email/password.
The 'op' parameter MUST be in the query string, not the POST body.
"""

import re
import time
import logging
import threading
import requests

log = logging.getLogger(__name__)


def parse_identifier(url_or_id):
    """Extract IA identifier from a URL or bare identifier string."""
    url_or_id = url_or_id.strip().rstrip("/")
    m = re.search(r"archive\.org/details/([^/?#]+)", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"archive\.org/download/([^/?#]+)", url_or_id)
    if m:
        return m.group(1)
    if "/" not in url_or_id and "." not in url_or_id:
        return url_or_id
    return None


# --- Cookie cache ---
_cookie_cache = {
    "cookies": {},
    "timestamp": 0,
}
_cookie_lock = threading.Lock()
_COOKIE_TTL = 3600  # 1 hour


def get_download_cookies(ia_email, ia_password):
    """Get IA download cookies, using cache when possible.
    Returns (cookies_dict, error_string_or_None).
    """
    if not ia_email or not ia_password:
        return {}, None

    with _cookie_lock:
        now = time.time()
        if _cookie_cache["cookies"] and now - _cookie_cache["timestamp"] < _COOKIE_TTL:
            return dict(_cookie_cache["cookies"]), None

    # Cache miss — authenticate fresh
    cookies, error = _login(ia_email, ia_password)
    if cookies:
        with _cookie_lock:
            _cookie_cache["cookies"] = dict(cookies)
            _cookie_cache["timestamp"] = time.time()

    return cookies, error


def invalidate_cookie_cache():
    """Clear the cookie cache (e.g. after credentials change)."""
    with _cookie_lock:
        _cookie_cache["cookies"] = {}
        _cookie_cache["timestamp"] = 0


def fetch_metadata(identifier, ia_email=None, ia_password=None, use_http=False):
    """Fetch metadata for an IA item. Returns dict with item info and files list."""
    scheme = "http" if use_http else "https"
    url = f"{scheme}://archive.org/metadata/{identifier}"
    cookies, _ = get_download_cookies(ia_email or "", ia_password or "")

    resp = requests.get(url, cookies=cookies, timeout=30)
    try:
        resp.raise_for_status()
        data = resp.json()
    finally:
        resp.close()

    if not data or "metadata" not in data:
        raise ValueError(f"Item '{identifier}' not found or has no metadata.")

    metadata = data.get("metadata", {})
    files = data.get("files", [])
    server = data.get("server", "")
    d1 = data.get("d1", "")
    d2 = data.get("d2", "")
    dir_path = data.get("dir", "")

    title = metadata.get("title", identifier)
    description = metadata.get("description", "")
    if isinstance(description, list):
        description = " ".join(description)

    total_size = sum(int(f.get("size", 0) or 0) for f in files)

    return {
        "identifier": identifier,
        "url": f"https://archive.org/details/{identifier}",
        "title": title,
        "description": description,
        "total_size": total_size,
        "files_count": len(files),
        "files": files,
        "server": server or d1 or d2,
        "dir": dir_path,
        "metadata": metadata,
    }


def fetch_archive_contents(identifier, filename, server=None, dir_path=None,
                           ia_email=None, ia_password=None, use_http=False):
    """Fetch the file listing inside a compressed archive from IA's view_archive.php.

    Returns list of dicts:
        [{"name": "sonic cd.bin", "size": 640000, "mtime": "1996-12-24 17:32:00", "is_dir": False}, ...]

    Only yields the first level of contents — nested archives (zip-in-zip)
    are discovered later via local inspection.
    """
    cookies, _ = get_download_cookies(ia_email or "", ia_password or "")
    scheme = "http" if use_http else "https"

    # Prefer direct URL via server/dir (avoids redirect)
    if server and dir_path:
        url = f"{scheme}://{server}/view_archive.php?archive={dir_path}/{filename}"
    else:
        # Canonical URL with trailing slash triggers redirect to view_archive.php
        url = f"{scheme}://archive.org/download/{identifier}/{requests.utils.quote(filename)}/"

    try:
        resp = requests.get(url, cookies=cookies, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    except requests.exceptions.RequestException as e:
        log.warning("Failed to fetch archive contents for %s/%s: %s", identifier, filename, e)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass

    return _parse_view_archive_html(html)


def _parse_view_archive_html(html):
    """Parse the HTML response from view_archive.php into structured file entries."""
    entries = []
    # view_archive.php outputs lines like:
    #   <a href="...">filename</a>  2024-01-15 12:30:00  12345
    # Some entries may show directory markers (trailing /)
    import re as _re

    # Match each table row or line containing a link + optional timestamp + size
    # Pattern handles both table-based and pre-formatted text layouts
    for match in _re.finditer(
        r'<a\s[^>]*>([^<]+)</a>\s*'           # link text (filename)
        r'(?:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+)?'  # optional timestamp
        r'(\d+)?',                              # optional size in bytes
        html,
    ):
        name = match.group(1).strip()
        mtime = match.group(2) or ""
        size_str = match.group(3)

        # Skip parent directory link
        if name in (".", "..", "../"):
            continue

        is_dir = name.endswith("/")
        if is_dir:
            name = name.rstrip("/")

        entries.append({
            "name": name,
            "size": int(size_str) if size_str else 0,
            "mtime": mtime.strip(),
            "is_dir": is_dir,
        })

    return entries


def get_download_url(identifier, filename, server=None, dir_path=None, use_http=False):
    """Construct a download URL for a specific file."""
    scheme = "http" if use_http else "https"
    return f"{scheme}://archive.org/download/{identifier}/{requests.utils.quote(filename)}"


def test_credentials(ia_email, ia_password):
    """Test IA credentials. Returns (success: bool, message: str)."""
    if not ia_email or not ia_password:
        return False, "Email and password are required"

    cookies, error = _login(ia_email, ia_password)
    if cookies:
        with _cookie_lock:
            _cookie_cache["cookies"] = dict(cookies)
            _cookie_cache["timestamp"] = time.time()
        return True, "Authentication successful"

    return False, error or "Authentication failed"


def _login(email, password):
    """Authenticate with IA via xauthn. Returns (cookies_dict, error_or_None)."""
    try:
        # IMPORTANT: op must be in query string, not POST body
        resp = requests.post(
            "https://archive.org/services/xauthn/?op=login",
            data={"email": email, "password": password},
            timeout=15,
        )

        try:
            data = resp.json()
        finally:
            resp.close()

        if data.get("success"):
            values = data.get("values", {})
            cookies_data = values.get("cookies", {})
            cookies = {
                "logged-in-user": cookies_data.get("logged-in-user", ""),
                "logged-in-sig": cookies_data.get("logged-in-sig", ""),
            }
            if cookies["logged-in-user"] and cookies["logged-in-sig"]:
                log.info("IA authentication successful for %s", email)
                return cookies, None
            else:
                msg = "Auth response missing cookie values"
                log.warning(msg)
                return {}, msg

        error_msg = data.get("values", {}).get("reason",
                    data.get("error", "Unknown auth error"))
        log.warning("IA auth failed for %s: %s", email, error_msg)
        return {}, f"Login failed: {error_msg}"

    except requests.exceptions.Timeout:
        return {}, "IA auth request timed out"
    except requests.exceptions.ConnectionError:
        return {}, "Could not connect to archive.org"
    except Exception as e:
        return {}, f"Auth error: {str(e)}"
