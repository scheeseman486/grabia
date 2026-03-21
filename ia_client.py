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
    resp.raise_for_status()
    data = resp.json()

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

        data = resp.json()

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
