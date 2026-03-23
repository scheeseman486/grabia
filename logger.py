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

"""Centralised debug logging for Grabia.

Usage:
    from logger import log
    log.debug("scan", "Checking file %s", filename)

Logging is controlled via the 'debug_enabled' and 'debug_log_file' settings
in the database.  When disabled, all log calls are no-ops.
"""

import logging
import os
import sys
import threading

_logger = logging.getLogger("grabia")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

_console_handler = None
_file_handler = None
_lock = threading.Lock()
_enabled = False


def _format():
    return logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def configure(enabled=False, log_file=""):
    """Reconfigure logging based on settings.  Called on startup and when
    settings change."""
    global _console_handler, _file_handler, _enabled

    with _lock:
        _enabled = enabled

        # Remove existing handlers
        for h in list(_logger.handlers):
            _logger.removeHandler(h)
            h.close()
        _console_handler = None
        _file_handler = None

        if not enabled:
            return

        # Console handler (always when enabled)
        _console_handler = logging.StreamHandler(sys.stderr)
        _console_handler.setFormatter(_format())
        _logger.addHandler(_console_handler)

        # Optional file handler
        if log_file:
            log_file = os.path.expanduser(log_file)
            try:
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                _file_handler = logging.FileHandler(log_file, encoding="utf-8")
                _file_handler.setFormatter(_format())
                _logger.addHandler(_file_handler)
            except OSError as e:
                _logger.warning("Could not open log file %s: %s", log_file, e)


class _Log:
    """Lightweight wrapper that prefixes messages with a category tag."""

    def debug(self, category, msg, *args):
        if _enabled:
            _logger.debug("[%s] " + msg, category, *args)

    def info(self, category, msg, *args):
        if _enabled:
            _logger.info("[%s] " + msg, category, *args)

    def warning(self, category, msg, *args):
        if _enabled:
            _logger.warning("[%s] " + msg, category, *args)

    def error(self, category, msg, *args):
        if _enabled:
            _logger.error("[%s] " + msg, category, *args)


log = _Log()
