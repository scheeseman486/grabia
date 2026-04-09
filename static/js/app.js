/*
 * Grabia - Internet Archive Download Manager
 * Copyright (C) 2026 Sharkcheese
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 */

/* ===== Grabia - Frontend ===== */

(function () {
    "use strict";

    // --- State ---
    let archives = [];
    let groups = [];
    let currentArchiveId = null;
    let currentSort = "name";
    let currentSortDir = ""; // empty = use backend default
    let fileSearchQuery = "";
    let fileSearchTimer = null;
    let dlState = "stopped";
    let loadFilesGen = 0;
    let refreshArchivesGen = 0;
    let dragSrcId = null;
    let dragSrcGroupId = null;
    let isDragging = false;
    let renderArchiveListPending = false;
    let archiveSearchQuery = "";
    let archiveSearchTimer = null;
    let archiveSort = "title";
    // Groups collapsed by default; store *expanded* group IDs in localStorage
    let expandedGroups = new Set(
        JSON.parse(localStorage.getItem("grabia_expanded_groups") || "[]")
    );

    function saveExpandedGroups() {
        localStorage.setItem("grabia_expanded_groups", JSON.stringify([...expandedGroups]));
    }
    let realBandwidth = -1; // tracks the actual backend bandwidth setting
    let lastProgressRefresh = 0; // timestamp of last throttled progress refresh
    let uiTickTimer = null;  // 1-second periodic UI refresh during active work

    // --- Virtual Scroll State ---
    const VS_ROW_HEIGHT = 37;       // px per normal file row
    const VS_OVERSCAN = 15;         // extra rows rendered above/below viewport
    let vsFiles = [];               // all file data from last fetch
    let vsAllQueued = false;        // whether all files are currently queued
    let vsExpandedIds = new Set();  // file IDs with expanded detail rows
    let vsScrollRAF = null;         // requestAnimationFrame handle for scroll
    let vsLastRange = null;         // { start, end } of last rendered range

    // --- File Tree State ---
    let vsExpandedFolders = new Set();     // expanded folder paths (e.g. "DOOM WADs")
    let vsProcessedCache = {};             // file_id -> processed tree children (lazy loaded)
    let vsRowDescriptorsCache = null;      // cached flattened row descriptors

    // --- Activity Log Virtual Scroll State ---
    const AL_ROW_HEIGHT = 37;       // px per activity log row
    const AL_OVERSCAN = 15;         // extra rows rendered above/below viewport
    let alEntries = [];             // all activity entries from last fetch
    let alScrollRAF = null;         // requestAnimationFrame handle
    let alLastRange = null;         // { start, end } of last rendered range

    // --- Collection Preview Virtual Scroll State ---
    const CP_ROW_HEIGHT = 32;       // px per preview row
    const CP_OVERSCAN = 15;
    let cpRows = [];                // flat preview rows from API
    let cpExpandedDirs = new Set(); // expanded directory unit display_names
    let cpScrollRAF = null;
    let cpLastRange = null;
    let cpLayoutLookup = {};        // layout id -> layout object (with segments)

    // --- Notifications ---
    let notifications = [];
    let notifIdCounter = 0;

    function addNotification(message, type = "info", { file_id, archive_id } = {}) {
        // Create a server-side notification; add from response immediately
        // (SSE notification_created will be deduped by ID check)
        const body = { message, type };
        if (file_id) body.file_id = file_id;
        if (archive_id) body.archive_id = archive_id;
        api("POST", "/api/notifications", body).then((notif) => {
            if (!notifications.find(n => n.id === notif.id)) {
                notifications.unshift(notif);
                renderNotifBadge();
                renderNotifList();
            }
        }).catch(() => {
            // Fallback: add client-side only
            const notif = { id: "local-" + (++notifIdCounter), message, type, created_at: Date.now() / 1000 };
            notifications.unshift(notif);
            renderNotifBadge();
            renderNotifList();
        });
        showToast(message, type);
    }

    function removeNotification(id) {
        // Dismiss on server (skip for local-only IDs)
        if (typeof id === "number") {
            api("DELETE", "/api/notifications/" + id).catch(() => {});
        }
        notifications = notifications.filter((n) => n.id !== id);
        renderNotifBadge();
        renderNotifList();
    }

    function clearAllNotifications() {
        api("POST", "/api/notifications/clear").then(() => {
            // Re-fetch to get the server's authoritative dismissed state,
            // which correctly handles completed scan/processing notifications
            loadNotifications();
        }).catch(() => {});
    }

    function loadNotifications() {
        api("GET", "/api/notifications").then((notifs) => {
            notifications = notifs;
            renderNotifBadge();
            renderNotifList();
        }).catch(() => {});
    }

    function renderNotifBadge() {
        const badge = $("#notif-badge");
        if (notifications.length > 0) {
            badge.textContent = notifications.length > 99 ? "99+" : notifications.length;
            badge.style.display = "";
        } else {
            badge.style.display = "none";
        }
    }

    function renderNotifList() {
        const list = $("#notif-list");
        if (notifications.length === 0) {
            list.innerHTML = '<div class="notif-empty">No notifications</div>';
            return;
        }
        list.innerHTML = "";
        notifications.forEach((n) => {
            const div = document.createElement("div");
            const isClickable = !!(n.file_id);
            div.className = "notif-item notif-" + n.type + (isClickable ? " notif-clickable" : "");
            const notifTime = n.created_at ? new Date(n.created_at * 1000) : (n.time || new Date());
            const ago = formatTimeAgo(notifTime);
            const viewLogHtml = n.job_id
                ? `<button class="notif-view-log" data-job-id="${n.job_id}">View Log</button>`
                : "";
            const goToFileHtml = isClickable
                ? `<button class="notif-view-log notif-goto-file" data-file-id="${n.file_id}">Show in Queue</button>`
                : "";
            div.innerHTML = `
                <div class="notif-content">
                    <span class="notif-message">${escapeHtml(n.message)}</span>
                    <span class="notif-time-row">
                        <span class="notif-time">${ago}</span>
                        ${viewLogHtml}
                        ${goToFileHtml}
                    </span>
                </div>
                <button class="notif-dismiss" data-notif-id="${n.id}" title="Dismiss">
                    <svg viewBox="0 0 24 24" width="12" height="12"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
                </button>
            `;
            const dismissBtn = div.querySelector(".notif-dismiss");
            if (dismissBtn) dismissBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                removeNotification(n.id);
            });
            const viewLogBtn = div.querySelector(".notif-view-log:not(.notif-goto-file)");
            if (viewLogBtn) {
                viewLogBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const jobId = parseInt(viewLogBtn.dataset.jobId);
                    $("#notif-popup").classList.remove("open");
                    openActivityLog({ job_id: jobId });
                });
            }
            const gotoBtn = div.querySelector(".notif-goto-file");
            if (gotoBtn) {
                gotoBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const fileId = parseInt(gotoBtn.dataset.fileId);
                    $("#notif-popup").classList.remove("open");
                    openQueues("download").then(() => {
                        scrollToQueueItemAndFlash("download", { fileId });
                    });
                });
            }
            // Clicking the notification body itself also navigates to the file
            if (isClickable) {
                div.addEventListener("click", () => {
                    const fileId = n.file_id;
                    $("#notif-popup").classList.remove("open");
                    openQueues("download").then(() => {
                        scrollToQueueItemAndFlash("download", { fileId });
                    });
                });
            }
            list.appendChild(div);
        });
    }

    function formatTimeAgo(date) {
        const secs = Math.floor((Date.now() - date.getTime()) / 1000);
        if (secs < 5) return "just now";
        if (secs < 60) return secs + "s ago";
        const mins = Math.floor(secs / 60);
        if (mins < 60) return mins + "m ago";
        const hrs = Math.floor(mins / 60);
        return hrs + "h ago";
    }

    function showToast(message, type = "info") {
        const container = $("#toast-container");
        const toast = document.createElement("div");
        toast.className = "toast toast-" + type;
        toast.innerHTML = `
            <span class="toast-message">${escapeHtml(message)}</span>
            <button class="toast-close" title="Close">
                <svg viewBox="0 0 24 24" width="12" height="12"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
            </button>
        `;
        toast.querySelector(".toast-close").addEventListener("click", () => {
            toast.classList.add("toast-exit");
            setTimeout(() => toast.remove(), 300);
        });
        container.appendChild(toast);
        // Trigger entrance animation
        requestAnimationFrame(() => toast.classList.add("toast-enter"));
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.add("toast-exit");
                setTimeout(() => toast.remove(), 300);
            }
        }, 5000);
    }

    // --- Scan Progress (UI updates only — notifications handled server-side) ---

    let scanLastRefresh = {}; // archive_id -> timestamp of last UI refresh
    const SCAN_REFRESH_INTERVAL = 3000; // refresh file list every 3s during scan

    function getArchiveName(archiveId) {
        const a = archives.find((x) => x.id === archiveId);
        return a ? (a.title || a.identifier) : "Archive #" + archiveId;
    }

    function updateScanProgress(data) {
        const { archive_id, phase } = data;

        if (phase === "verify") {
            // Periodically refresh file list & archive sidebar during scan
            const now = Date.now();
            if (!scanLastRefresh[archive_id] || now - scanLastRefresh[archive_id] >= SCAN_REFRESH_INTERVAL) {
                scanLastRefresh[archive_id] = now;
                if (currentArchiveId === archive_id) loadFiles();
                refreshArchives();
            }
        } else if (phase === "done") {
            delete scanLastRefresh[archive_id];
            updateScanButton();
            loadFiles();
            refreshArchives();
            refreshQueueCount();
        } else if (phase === "cancelled") {
            delete scanLastRefresh[archive_id];
            updateScanButton();
            loadFiles();
            refreshArchives();
        } else if (phase === "error") {
            delete scanLastRefresh[archive_id];
            updateScanButton();
        }
    }

    // Track active scan file hash progress for queue display
    let scanFileProgress = {}; // entry_id -> { bytes_done, bytes_total, phase }

    function updateScanFileProgress(data) {
        const { entry_id, bytes_done, bytes_total, phase } = data;
        scanFileProgress[entry_id] = { bytes_done, bytes_total, phase };

        // Find the row in the scan queue table and update status capsule
        const row = $(`#queue-scan-tbody tr[data-entry-id="${entry_id}"]`);
        if (!row) return;

        let capsule = row.querySelector(".file-status");
        if (!capsule) return;

        const pct = bytes_total > 0 ? Math.min(100, (bytes_done / bytes_total) * 100) : 0;
        capsule.className = "file-status scanning";
        capsule.style.setProperty("--pct", pct.toFixed(1) + "%");
        capsule.textContent = pct < 100 ? `${pct.toFixed(0)}%` : "verifying";
    }

    function toggleNotifPopup() {
        const popup = document.querySelector("#notif-popup");
        popup.classList.toggle("open");
    }

    // --- DOM refs ---
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    // ── Context Menu ────────────────────────────────────────────────

    function showContextMenu(e, items) {
        e.preventDefault();
        const menu = $("#context-menu");
        const ul = $("#context-menu-items");
        ul.innerHTML = "";

        for (const item of items) {
            if (item.separator) {
                const sep = document.createElement("li");
                sep.className = "context-menu-separator";
                ul.appendChild(sep);
                continue;
            }
            const li = document.createElement("li");
            li.className = "context-menu-item"
                + (item.danger ? " danger" : "")
                + (item.disabled ? " disabled" : "");
            li.textContent = item.label;
            if (!item.disabled) {
                li.addEventListener("click", () => {
                    hideContextMenu();
                    item.action();
                });
            }
            ul.appendChild(li);
        }

        menu.style.display = "";

        // Position at cursor, clamped to viewport
        const rect = menu.getBoundingClientRect();
        let x = e.clientX;
        let y = e.clientY;
        if (x + rect.width > window.innerWidth) x = window.innerWidth - rect.width - 4;
        if (y + rect.height > window.innerHeight) y = window.innerHeight - rect.height - 4;
        menu.style.left = x + "px";
        menu.style.top = y + "px";
    }

    function hideContextMenu() {
        $("#context-menu").style.display = "none";
    }

    // Dismiss on click anywhere, Escape, or scroll
    document.addEventListener("click", hideContextMenu);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideContextMenu(); });
    document.addEventListener("scroll", hideContextMenu, true);

    const pageHome = $("#page-home");
    const pageDetail = $("#page-detail");
    const archiveListEl = $("#archive-list");
    const archiveListWrap = archiveListEl.closest(".archive-list-wrap");
    const emptyState = $("#empty-state");
    const fileListEl = $("#file-list");
    // queue-status-dot removed in queue overhaul — replaced by queue-display-badge
    const speedDisplay = $("#speed-display");
    const sparkCanvas = $("#speed-sparkline");
    const sparkCtx = sparkCanvas.getContext("2d");

    // --- Speed sparkline ---

    const SPARK_MAX_POINTS = 30;
    const speedHistory = [];

    function pushSpeed(bps) {
        speedHistory.push(bps || 0);
        if (speedHistory.length > SPARK_MAX_POINTS) speedHistory.shift();
        drawSparkline();
    }

    function clearSparkline() {
        speedHistory.length = 0;
        sparkCtx.clearRect(0, 0, sparkCanvas.width, sparkCanvas.height);
        sparkCanvas.classList.remove("active");
    }

    function drawSparkline() {
        const w = sparkCanvas.width;
        const h = sparkCanvas.height;
        const pts = speedHistory;
        const len = pts.length;

        sparkCtx.clearRect(0, 0, w, h);

        if (len < 2) {
            sparkCanvas.classList.remove("active");
            return;
        }
        sparkCanvas.classList.add("active");

        const max = Math.max(...pts) || 1;
        const pad = 2;
        const plotH = h - pad * 2;
        const step = w / (SPARK_MAX_POINTS - 1);
        const offset = (SPARK_MAX_POINTS - len) * step;

        // Fill
        sparkCtx.beginPath();
        sparkCtx.moveTo(offset, h - pad);
        for (let i = 0; i < len; i++) {
            sparkCtx.lineTo(offset + i * step, h - pad - (pts[i] / max) * plotH);
        }
        sparkCtx.lineTo(offset + (len - 1) * step, h - pad);
        sparkCtx.closePath();
        const accentStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#5b9bf7";
        sparkCtx.fillStyle = accentStyle + "30";
        sparkCtx.fill();

        // Line
        sparkCtx.beginPath();
        for (let i = 0; i < len; i++) {
            const x = offset + i * step;
            const y = h - pad - (pts[i] / max) * plotH;
            if (i === 0) sparkCtx.moveTo(x, y);
            else sparkCtx.lineTo(x, y);
        }
        sparkCtx.strokeStyle = accentStyle;
        sparkCtx.lineWidth = 1.5;
        sparkCtx.lineJoin = "round";
        sparkCtx.stroke();
    }

    // --- Utility ---

    function formatBytes(bytes) {
        if (!bytes || bytes === 0) return "0 B";
        const k = 1024;
        const sizes = ["B", "KB", "MB", "GB", "TB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
    }

    function formatSpeed(bps) {
        if (!bps || bps <= 0) return "";
        return formatBytes(bps) + "/s";
    }

    function formatDate(mtime) {
        if (!mtime) return "-";
        const d = new Date(parseInt(mtime) * 1000);
        if (isNaN(d.getTime())) return mtime;
        return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    /**
     * Render a filename as two spans for Finder-style middle truncation.
     * The head (stem) truncates with ellipsis; the tail (extension) never shrinks.
     * extraClass is optional, e.g. "file-name-deleted".
     */
    function renderFileName(name, extraClass) {
        const escaped = escapeHtml(name);
        const dotIdx = name.lastIndexOf(".");
        const cls = "file-name" + (extraClass ? " " + extraClass : "");
        if (dotIdx > 0 && dotIdx < name.length - 1) {
            const head = escapeHtml(name.substring(0, dotIdx));
            const tail = escapeHtml(name.substring(dotIdx));
            return `<span class="${cls}"><span class="fname-head">${head}</span><span class="fname-tail">${tail}</span></span>`;
        }
        return `<span class="${cls}"><span class="fname-head">${escaped}</span></span>`;
    }

    /** Same middle-truncation for processed tree node names. */
    function renderPtreeName(name) {
        const escaped = escapeHtml(name);
        const dotIdx = name.lastIndexOf(".");
        if (dotIdx > 0 && dotIdx < name.length - 1) {
            const head = escapeHtml(name.substring(0, dotIdx));
            const tail = escapeHtml(name.substring(dotIdx));
            return `<span class="ptree-name"><span class="fname-head">${head}</span><span class="fname-tail">${tail}</span></span>`;
        }
        return `<span class="ptree-name"><span class="fname-head">${escaped}</span></span>`;
    }

    /** Add title tooltip only to filename spans that are actually truncated. */
    function applyTruncationTooltips(container) {
        const selector = ".file-name, .ptree-name";
        for (const el of (container || document).querySelectorAll(selector)) {
            const head = el.querySelector(".fname-head");
            if (head && head.scrollWidth > head.clientWidth) {
                el.title = el.textContent;
            } else {
                el.removeAttribute("title");
            }
        }
    }

    async function api(method, path, body) {
        const opts = { method, headers: {} };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "Request failed");
        return data;
    }

    // --- Theme ---

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        localStorage.setItem("grabia_theme", theme);
    }

    // Apply cached theme immediately to avoid flash
    (function () {
        const cached = localStorage.getItem("grabia_theme");
        if (cached) document.documentElement.setAttribute("data-theme", cached);
    })();

    // --- SSE ---

    const connThrobber = $("#conn-lost-throbber");
    const connOverlay = $("#conn-lost-overlay");
    let connLostTime = 0;
    let connDimTimer = null;
    let connTextTimer = null;

    function onSSEDisconnect() {
        if (connLostTime) return; // already tracking
        connLostTime = Date.now();
        connThrobber.classList.add("visible");
        connDimTimer = setTimeout(() => {
            connOverlay.classList.add("dimmed");
        }, 3000);
        connTextTimer = setTimeout(() => {
            connOverlay.classList.add("show-text");
        }, 10000);
    }

    function onSSEReconnect() {
        connLostTime = 0;
        connThrobber.classList.remove("visible");
        connOverlay.classList.remove("dimmed", "show-text");
        clearTimeout(connDimTimer);
        clearTimeout(connTextTimer);
        connDimTimer = null;
        connTextTimer = null;
        // Refresh everything after reconnect
        refreshStatus();
        refreshArchives();
        refreshGroups();
        refreshQueueCount();
        loadNotifications();
        if (currentArchiveId) loadFiles();
    }

    function connectSSE() {
        const es = new EventSource("/api/events");

        es.addEventListener("status", (e) => {
            const data = JSON.parse(e.data);
            updateStatus(data);
        });

        es.addEventListener("state", (e) => {
            const prevState = dlState;
            const data = JSON.parse(e.data);
            dlState = data;
            updateControlButtons();

            syncBandwidthToState();
            if (dlState !== "running") {
                speedDisplay.textContent = "";
                clearSparkline();
            }
            if (dlState === "stopped") {
                activeDownloads = [];
                currentDownloadInfo = null;
                stopUiTick();
            }
            updateQueueDisplayText();
            // Refresh file list when downloader stops so status resets are visible
            if (prevState === "running" && dlState !== "running" && currentArchiveId) {
                loadFiles();
            }
        });

        es.addEventListener("file_progress", (e) => {
            if (dlState !== "running") return;
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "downloading", downloaded_bytes: data.downloaded, size: data.size });
            // Update the matching entry in activeDownloads (speed/progress displayed by uiTick)
            const dl = activeDownloads.find(d => d.file_id === data.file_id);
            if (dl) {
                dl.downloaded = data.downloaded;
                dl.size = data.size;
                dl.speed = data.speed || 0;
            }
            startUiTick();
            throttledProgressRefresh();
        });

        es.addEventListener("file_complete", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "downloaded", downloaded: 1, queue_position: null });
            // Remove from active downloads (queue_update event handles queue data removal)
            activeDownloads = activeDownloads.filter(d => d.file_id !== data.file_id);
            currentDownloadInfo = activeDownloads.length > 0 ? activeDownloads[0] : null;
            lastProgressRefresh = 0; // force immediate refresh
            throttledProgressRefresh();
            refreshQueueCount();
            refreshOngoingActivity();
            if (queueDropdownOpen) loadQueueDropdown();
            _refreshActivityIfVisible();
        });

        es.addEventListener("file_error", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "failed" });
            // Remove from active downloads (queue_update event handles queue data removal)
            activeDownloads = activeDownloads.filter(d => d.file_id !== data.file_id);
            currentDownloadInfo = activeDownloads.length > 0 ? activeDownloads[0] : null;
            refreshOngoingActivity();
            if (queueDropdownOpen) loadQueueDropdown();
            _refreshActivityIfVisible();
            const fname = data.filename || "Unknown file";
            const archive = data.identifier || "";
            const detail = data.error || "Unknown error";
            addNotification(`Download error: ${fname}${archive ? " (" + archive + ")" : ""} — ${detail}`, "error", { file_id: data.file_id });
        });

        es.addEventListener("file_failed", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "failed" });
            // Remove from active downloads (queue_update event handles queue data removal)
            activeDownloads = activeDownloads.filter(d => d.file_id !== data.file_id);
            currentDownloadInfo = activeDownloads.length > 0 ? activeDownloads[0] : null;
            lastProgressRefresh = 0;
            throttledProgressRefresh();
            refreshOngoingActivity();
            if (queueDropdownOpen) loadQueueDropdown();
            _refreshActivityIfVisible();
            const fname = data.filename || "Unknown file";
            const archive = data.identifier || "";
            addNotification(`Download failed: ${fname}${archive ? " (" + archive + ")" : ""} — retries exhausted`, "error", { file_id: data.file_id });
        });

        es.addEventListener("file_skipped", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, {
                download_status: "pending",
                queue_position: null,
                downloaded_bytes: data.downloaded || 0,
                size: data.size,
            });
            lastProgressRefresh = 0;
            throttledProgressRefresh();
            refreshQueueCount();
        });

        es.addEventListener("file_start", (e) => {
            const data = JSON.parse(e.data);
            // Add to active downloads if not already tracked
            if (data.file_id && !activeDownloads.find(d => d.file_id === data.file_id)) {
                activeDownloads.push({ file_id: data.file_id, filename: data.filename, downloaded: 0, size: 0, speed: 0 });
                currentDownloadInfo = activeDownloads[0];
            }
            // Update file list status immediately
            updateFileRow(data.file_id, { download_status: "downloading", downloaded_bytes: 0 });
            refreshOngoingActivity();
            if (queueDropdownOpen) loadQueueDropdown();
            startUiTick();
            refreshStatus();
        });

        es.addEventListener("scan_progress", (e) => {
            const data = JSON.parse(e.data);
            updateScanProgress(data);
            _updateJobProgress(data);
            // Track ongoing scanning state
            if (data.phase === "done" || data.phase === "cancelled" || data.phase === "error") {
                ongoingScanning = null;
                _refreshActivityIfVisible();
            } else {
                ongoingScanning = {
                    archive_id: data.archive_id,
                    phase: data.phase || "",
                    current: data.current || 0,
                    total: data.total || 0,
                };
                startUiTick();
            }
            refreshOngoingActivity();
        });

        es.addEventListener("scan_file_progress", (e) => {
            const data = JSON.parse(e.data);
            updateScanFileProgress(data);
        });

        es.addEventListener("processing_progress", (e) => {
            const data = JSON.parse(e.data);
            updateProcessingProgress(data);
            _updateJobProgress(data);
            // Track ongoing processing state
            if (data.phase === "done" || data.phase === "cancelled" || data.phase === "error") {
                ongoingProcessing = null;
                _refreshActivityIfVisible();
            } else {
                ongoingProcessing = {
                    archive_id: data.archive_id,
                    filename: data.filename || "",
                    current: data.current || 0,
                    total: data.total || 0,
                    phase: data.phase || "",
                    pct: data.pct,  // per-file tool progress (e.g. chdman %)
                };
                startUiTick();
            }
            refreshOngoingActivity();
        });

        es.addEventListener("notification_created", (e) => {
            const notif = JSON.parse(e.data);
            // Don't add duplicates
            if (!notifications.find(n => n.id === notif.id)) {
                notifications.unshift(notif);
                renderNotifBadge();
                renderNotifList();
            }
        });

        es.addEventListener("notification_updated", (e) => {
            const notif = JSON.parse(e.data);
            const idx = notifications.findIndex(n => n.id === notif.id);
            if (idx >= 0) {
                notifications[idx] = notif;
            } else {
                notifications.unshift(notif);
            }
            renderNotifBadge();
            renderNotifList();
        });

        es.addEventListener("notification_dismissed", (e) => {
            const data = JSON.parse(e.data);
            notifications = notifications.filter(n => n.id !== data.id);
            renderNotifBadge();
            renderNotifList();
        });

        es.addEventListener("notifications_cleared", () => {
            notifications = [];
            renderNotifBadge();
            renderNotifList();
        });

        es.addEventListener("archive_added", () => { refreshArchives(); refreshQueueCount(); });
        es.addEventListener("archive_updated", (e) => {
            refreshArchives(); refreshQueueCount();
            // Sync auto-process controls if viewing the updated archive
            try {
                const data = JSON.parse(e.data);
                if (data.id && data.id === currentArchiveId && data.processing_profile_id !== undefined) {
                    const archive = archives.find(a => a.id === data.id);
                    if (archive) archive.processing_profile_id = data.processing_profile_id;
                    updateAutoProcessControls(currentArchiveId);
                }
            } catch (_) {}
        });
        es.addEventListener("archive_removed", () => { refreshArchives(); refreshQueueCount(); });
        // archives_reordered is no longer sent (archives sorted client-side)
        es.addEventListener("groups_changed", () => refreshGroups());
        const refreshCollectionsIfVisible = async () => {
            await refreshCollections();
            if ($("#page-collections")?.classList.contains("active")) renderCollectionList();
        };
        es.addEventListener("collection_created", refreshCollectionsIfVisible);
        es.addEventListener("collection_updated", refreshCollectionsIfVisible);
        es.addEventListener("collection_deleted", refreshCollectionsIfVisible);
        es.addEventListener("collection_synced", refreshCollectionsIfVisible);
        es.addEventListener("collections_reordered", refreshCollectionsIfVisible);
        es.addEventListener("settings_updated", (e) => {
            const s = JSON.parse(e.data);
            if (s.theme) applyTheme(s.theme);
            updateLockIndicator(s.use_http === "1");
        });

        // ── Queue SSE events ──────────────────────────────────────
        es.addEventListener("queue_changed", (e) => {
            const data = JSON.parse(e.data);
            const queueType = data.queue_type; // "download", "processing", "scan"
            if (queueType && queueStale.hasOwnProperty(queueType)) {
                queueStale[queueType] = true;
                if ($("#page-queues").classList.contains("active") && activeQueueTab === queueType) {
                    loadQueueTab(queueType);
                }
            }
            refreshQueueCounts();
            if (queueType === "download") {
                lastProgressRefresh = 0;
                throttledProgressRefresh();
            }
        });

        es.addEventListener("queue_update", (e) => {
            const data = JSON.parse(e.data);
            const queueType = data.queue_type || data.queue; // normalise key
            if (!queueType || !queueStale.hasOwnProperty(queueType)) { refreshQueueCounts(); return; }

            const action = data.action;
            const isVisible = $("#page-queues").classList.contains("active") && activeQueueTab === queueType;

            // --- In-place array splice/update when possible ---
            if (action === "removed" && data.file_ids) {
                const ids = new Set(data.file_ids.map(Number));
                queueData[queueType] = queueData[queueType].filter(item => {
                    const itemId = item.file_id || item.id;
                    return !ids.has(itemId);
                });
                if (isVisible) renderQueueTable(queueType);
            } else if (action === "completed" || (action === "status_changed" && ["completed", "done", "failed", "cancelled"].includes(data.status))) {
                // Mark item as completing (3-second grey-out)
                const entryId = data.entry_id || data.file_id;
                if (entryId) {
                    const key = `${queueType}:${entryId}`;
                    if (!completingItems.has(key)) {
                        // Update the item status in local data
                        const item = queueData[queueType].find(i => (i.id === entryId || i.file_id === entryId));
                        if (item) item.status = data.status || "completed";
                        completingItems.set(key, setTimeout(() => {
                            completingItems.delete(key);
                            queueData[queueType] = queueData[queueType].filter(i => {
                                const iid = i.id === entryId || i.file_id === entryId;
                                return !iid;
                            });
                            if ($("#page-queues").classList.contains("active") && activeQueueTab === queueType) {
                                renderQueueTable(queueType);
                            }
                            refreshQueueCounts();
                        }, 3000));
                    }
                    if (isVisible) renderQueueTable(queueType);
                } else {
                    queueStale[queueType] = true;
                    if (isVisible) loadQueueTab(queueType);
                }
            } else if (action === "added") {
                // New item added — refetch to get full data
                queueStale[queueType] = true;
                if (isVisible) loadQueueTab(queueType);
            } else if (action === "reordered") {
                // Reorder — refetch to get new positions
                queueStale[queueType] = true;
                if (isVisible) loadQueueTab(queueType);
            } else if (action === "status_changed") {
                // Pause state change — update button appearance
                if (data.paused !== undefined) {
                    if (queueType === "processing") {
                        db_processing_paused = data.paused;
                        updatePauseButton("queue-proc-pause", data.paused);
                    } else if (queueType === "scan") {
                        db_scan_paused = data.paused;
                        updatePauseButton("queue-scan-pause", data.paused);
                    }
                }
                // Non-terminal status change — update in place
                const entryId = data.entry_id || data.file_id;
                if (entryId) {
                    const item = queueData[queueType].find(i => (i.id === entryId || i.file_id === entryId));
                    if (item && data.status) item.status = data.status;
                    if (isVisible) renderQueueTable(queueType);
                } else if (data.paused === undefined) {
                    queueStale[queueType] = true;
                    if (isVisible) loadQueueTab(queueType);
                }
            } else {
                queueStale[queueType] = true;
                if (isVisible) loadQueueTab(queueType);
            }
            refreshQueueCounts();
        });

        es.addEventListener("bandwidth_update", (e) => {
            const data = JSON.parse(e.data);
            // Schedule-driven bandwidth change: update the UI to reflect it
            updateBandwidthUI(data.limit);
        });

        es.onopen = () => {
            if (connLostTime) onSSEReconnect();
        };

        es.onerror = () => {
            es.close();
            onSSEDisconnect();
            setTimeout(connectSSE, 3000);
        };
    }

    function updateStatus(data) {
        dlState = data.state;
        updateControlButtons();
        syncBandwidthToState();
        if (dlState === "running" && data.current_speed) {
            speedDisplay.textContent = formatSpeed(data.current_speed);
        } else {
            speedDisplay.textContent = "";
        }
        // Track active downloads for queue display
        activeDownloads = data.active_downloads || [];
        currentDownloadInfo = activeDownloads.length > 0 ? activeDownloads[0] : null;
        if (dlState === "stopped") {
            activeDownloads = [];
            currentDownloadInfo = null;
        }
        updateQueueDisplayText();
        if (activeDownloads.length > 0 && dlState === "running") startUiTick();
    }


    function updateLockIndicator(insecure) {
        const el = $("#lock-indicator");
        el.classList.toggle("lock-secure", !insecure);
        el.classList.toggle("lock-insecure", !!insecure);
        el.title = insecure ? "Downloads use unencrypted HTTP" : "Downloads use HTTPS";
    }

    function updateControlButtons() {
        const play = $("#btn-play");
        const pause = $("#btn-pause");
        const stop = $("#btn-stop");
        play.classList.toggle("active", dlState === "running");
        pause.classList.toggle("active", dlState === "paused");
        play.disabled = dlState === "running";
        pause.disabled = dlState !== "running";
        stop.disabled = dlState === "stopped";
    }

    // --- Queue Display ---

    const queueDisplay = $("#queue-display");
    const queueDisplayText = $("#queue-display-text");
    const queueDisplayBadge = $("#queue-display-badge");
    const queueDropdown = $("#queue-dropdown");
    let queueDropdownOpen = false;
    let currentDownloadInfo = null; // backwards compat: first active download
    let activeDownloads = []; // array of {file_id, filename, identifier, archive_id, size, downloaded, speed, datanode}
    let lastQueueCount = 0;
    let displayCycleIndex = 0; // which active task to show when cycling

    function _getActiveActivities() {
        const activities = [];
        if (activeDownloads.length > 0 && (dlState === "running" || dlState === "paused")) {
            activities.push("download");
        }
        if (ongoingProcessing && ongoingProcessing.phase !== "done" && ongoingProcessing.phase !== "error" && ongoingProcessing.phase !== "cancelled") {
            activities.push("processing");
        }
        if (ongoingScanning && ongoingScanning.phase !== "done" && ongoingScanning.phase !== "error" && ongoingScanning.phase !== "cancelled") {
            activities.push("scan");
        }
        return activities;
    }

    function cycleQueueDisplay() {
        const activities = _getActiveActivities();
        if (activities.length <= 1) return;
        displayCycleIndex = (displayCycleIndex + 1) % activities.length;
        updateQueueDisplayText();
    }

    function updateQueueDisplayText() {
        // State class
        queueDisplay.className = queueDisplay.className.replace(/\bstate-\S+/g, "");
        queueDisplay.classList.add("state-" + dlState);
        if (queueDisplay.classList.contains("active")) queueDisplay.classList.add("active");

        const activities = _getActiveActivities();
        const otherCount = activities.length > 1 ? activities.length - 1 : 0;

        // Determine which activity to display
        let displayActivity = null;
        if (activities.length > 0) {
            const idx = displayCycleIndex % activities.length;
            displayActivity = activities[idx];
        }

        if (displayActivity === "download") {
            const count = activeDownloads.length;
            const first = currentDownloadInfo || activeDownloads[0];
            if (count > 1) {
                queueDisplayText.textContent = `Downloading ${count} files`;
                queueDisplay.title = activeDownloads.map(d => d.filename).join(", ");
            } else if (first) {
                queueDisplayText.textContent = first.filename;
                queueDisplay.title = `${dlState === "running" ? "Downloading" : "Paused"}: ${first.filename}${first.identifier ? " (" + first.identifier + ")" : ""}`;
            }
        } else if (displayActivity === "processing") {
            const prog = ongoingProcessing.total > 0 ? ` (${ongoingProcessing.current}/${ongoingProcessing.total})` : "";
            queueDisplayText.textContent = `Processing: ${ongoingProcessing.filename}${prog}`;
            queueDisplay.title = `Processing file`;
        } else if (displayActivity === "scan") {
            const prog = ongoingScanning.total > 0 ? ` ${ongoingScanning.current}/${ongoingScanning.total}` : "";
            queueDisplayText.textContent = `Scanning${prog}`;
            queueDisplay.title = `Scanning files`;
        } else {
            const total = (queueCounts.download || 0) + (queueCounts.processing || 0) + (queueCounts.scan || 0);
            if (total > 0) {
                queueDisplayText.textContent = total + (total === 1 ? " item queued" : " items queued");
                queueDisplay.title = total + " pending across queues";
            } else {
                queueDisplayText.textContent = "Idle";
                queueDisplay.title = "View queues";
            }
        }

        // "+N active" cycling indicator
        const cycleEl = $("#queue-display-cycle");
        if (cycleEl) {
            if (otherCount > 0) {
                cycleEl.textContent = `+${otherCount} active`;
                cycleEl.style.display = "";
            } else {
                cycleEl.style.display = "none";
            }
        }

        // Update badge
        const total = (queueCounts.download || 0) + (queueCounts.processing || 0) + (queueCounts.scan || 0);
        if (total > 0) {
            queueDisplayBadge.textContent = total > 999 ? "999+" : total;
            queueDisplayBadge.style.display = "";
        } else {
            queueDisplayBadge.style.display = "none";
        }
    }

    async function refreshQueueCount() {
        await refreshQueueCounts();  // reuse the queue page's count fetch
        updateQueueDisplayText();
    }

    // ── UI Tick ──────────────────────────────────────────────────────
    // A 1-second periodic refresh that keeps speed, queue display, and
    // the Ongoing tab smoothly updated during active downloads/processing.
    // Starts when work begins, stops when idle.

    function startUiTick() {
        if (uiTickTimer) return;
        uiTickTimer = setInterval(uiTick, 1000);
    }

    function stopUiTick() {
        if (uiTickTimer) {
            clearInterval(uiTickTimer);
            uiTickTimer = null;
        }
    }

    function uiTick() {
        // Aggregate speed across all active downloads
        const totalSpeed = activeDownloads.reduce((sum, d) => sum + (d.speed || 0), 0);
        speedDisplay.textContent = formatSpeed(totalSpeed);
        pushSpeed(totalSpeed);

        // Update topbar text (filename, progress, etc.)
        updateQueueDisplayText();

        // Update the Ongoing tab (Activities page) if visible
        if ($("#page-activity").classList.contains("active") && activeActivityTab === "ongoing") {
            refreshOngoingActivity();
        }

        // Update queue dropdown if open
        if (queueDropdownOpen) loadQueueDropdown();

        // Update queue page tables if visible — sync live progress into queue data
        if ($("#page-queues").classList.contains("active")) {
            syncActiveProgressToQueueData();
            // Force re-render of visible rows without re-filtering/re-sorting
            qsLastRange[activeQueueTab] = null;
            qsVsRenderVisible(activeQueueTab);
        }

        // Stop ticking when nothing is active
        const hasWork = (activeDownloads.length > 0 && (dlState === "running" || dlState === "paused"))
            || (ongoingProcessing && !["done", "error", "cancelled"].includes(ongoingProcessing.phase))
            || (ongoingScanning && !["done", "error", "cancelled"].includes(ongoingScanning.phase));
        if (!hasWork) stopUiTick();
    }

    /**
     * Merge real-time progress from activeDownloads into queueData["download"]
     * so queue table re-renders show live percentages.
     */
    function syncActiveProgressToQueueData() {
        if (activeDownloads.length === 0) return;
        const dlQueue = queueData["download"];
        if (!dlQueue) return;
        for (const dl of activeDownloads) {
            const item = dlQueue.find(q => (q.id || q.file_id) == dl.file_id);
            if (item) {
                item.downloaded_bytes = dl.downloaded || 0;
                item.download_status = "downloading";
                if (dl.size > 0) item.size = dl.size;
            }
        }
    }

    function loadQueueDropdown() {
        // Render the ongoing activity summary into the dropdown
        const rows = $("#queue-dropdown-rows");
        const empty = $("#queue-dropdown-empty");
        if (rows && empty) renderOngoingActivity(rows, empty);
    }

    function openQueueDropdown() {
        queueDropdownOpen = true;
        queueDropdown.classList.add("open");
        queueDisplay.classList.add("active");
        loadQueueDropdown();
    }

    function closeQueueDropdown() {
        queueDropdownOpen = false;
        queueDropdown.classList.remove("open");
        queueDisplay.classList.remove("active");
    }

    queueDisplay.addEventListener("click", (e) => {
        e.stopPropagation();
        if (queueDropdownOpen) closeQueueDropdown();
        else openQueueDropdown();
    });
    queueDropdown.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => {
        if (queueDropdownOpen) closeQueueDropdown();
    });
    // "+N active" cycling click
    const cycleBtn = $("#queue-display-cycle");
    if (cycleBtn) {
        cycleBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            cycleQueueDisplay();
        });
    }

    async function navigateToFile(archiveId, fileId) {
        // Open the archive if not already open
        if (currentArchiveId !== archiveId) {
            await openArchiveDetail(archiveId);
        }
        // Wait a tick for virtual scroll to settle, then find and scroll to the file
        requestAnimationFrame(() => {
            scrollToFileAndFlash(fileId);
        });
    }

    function scrollToFileAndFlash(fileId) {
        // Find the file index in vsFiles
        const idx = vsFiles.findIndex((f) => f.id === fileId);
        if (idx === -1) return;

        // Scroll to the row — this triggers vsRenderVisible via the scroll listener
        const wrap = $(".file-table-wrap");
        const targetTop = idx * VS_ROW_HEIGHT;
        wrap.scrollTop = targetTop - wrap.clientHeight / 2 + VS_ROW_HEIGHT / 2;

        // Give virtual scroll time to render, then flash the row
        setTimeout(() => {
            const row = fileListEl.querySelector(`tr[data-file-id="${fileId}"]`);
            if (row) flashElement(row);
        }, 50);
    }

    // Flash state: tracks which elements should be flashing, keyed by a selector string.
    // Survives virtual scroll re-renders because row builders check this set.
    const flashingElements = new Map(); // selector -> { on: bool, intervalId }

    function flashElement(el, times = 3) {
        // Determine a stable selector that survives DOM rebuilds
        const selector = _flashSelector(el);
        if (!selector) {
            // Fallback for non-identifiable elements (e.g. settings sections) — direct DOM flash
            _flashDirect(el, times);
            return;
        }

        // If already flashing this selector, skip
        if (flashingElements.has(selector)) return;

        let flashes = 0;
        const state = { on: true };
        el.classList.add("queue-flash");

        const interval = setInterval(() => {
            flashes++;
            state.on = !state.on;
            // Apply to whichever DOM element currently matches (may be a rebuilt node)
            const current = document.querySelector(selector);
            if (current) current.classList.toggle("queue-flash", state.on);
            if (flashes >= times * 2) {
                clearInterval(interval);
                flashingElements.delete(selector);
                const final = document.querySelector(selector);
                if (final) final.classList.remove("queue-flash");
            }
        }, 200);

        state.intervalId = interval;
        flashingElements.set(selector, state);
    }

    /** Build a selector that can re-find this element after a virtual scroll rebuild. */
    function _flashSelector(el) {
        if (el.dataset?.fileId) return `tr[data-file-id="${el.dataset.fileId}"]`;
        if (el.dataset?.entryId) return `tr[data-entry-id="${el.dataset.entryId}"]`;
        if (el.id) return `#${el.id}`;
        return null;
    }

    /** Check if a row should have the flash class applied at build time. */
    function isFlashing(el) {
        const sel = _flashSelector(el);
        return sel && flashingElements.has(sel) && flashingElements.get(sel).on;
    }

    /** Direct DOM flash for elements that don't survive rebuilds (rare fallback). */
    function _flashDirect(el, times) {
        let flashes = 0;
        const interval = setInterval(() => {
            el.classList.toggle("queue-flash");
            flashes++;
            if (flashes >= times * 2) {
                clearInterval(interval);
                el.classList.remove("queue-flash");
            }
        }, 200);
    }

    async function navigateToProcessingProfiles() {
        await openSettings();
        switchTab("tab-processing");
        // Wait for the tab to render, then scroll to and flash the profiles section
        setTimeout(() => {
            const section = $("#processing-profiles-section");
            if (section) {
                section.scrollIntoView({ behavior: "smooth", block: "center" });
                flashElement(section);
            }
        }, 100);
    }

    async function refreshStatus() {
        try {
            const data = await api("GET", "/api/download/status");
            updateStatus(data);
        } catch (e) { /* ignore */ }
    }

    async function throttledProgressRefresh() {
        const now = Date.now();
        if (now - lastProgressRefresh < 2000) return;
        lastProgressRefresh = now;
        refreshStatus();           // updates global progress bar + text
        refreshArchives();         // updates archive-progress-meta (and detail via updateDetailProgress)
        if (currentArchiveId) {
            try {
                const p = await api("GET", `/api/archives/${currentArchiveId}/progress`);
                updateDetailProgressFromData(p);
            } catch (_) {}
        }
    }

    // --- Archives ---

    async function refreshArchives() {
        const gen = ++refreshArchivesGen;
        try {
            const data = await api("GET", "/api/archives");
            if (gen !== refreshArchivesGen) return; // Stale response
            archives = data;
            renderArchiveList();
            updateDetailProgress();
        } catch (e) { /* ignore */ }
    }

    async function refreshGroups() {
        try {
            groups = await api("GET", "/api/groups");
            renderArchiveList();
        } catch (e) { /* ignore */ }
    }

    function updateDetailProgressFromData(p) {
        const prog = $("#detail-progress-meta");
        const total = p.total_files || 0;
        const completed = p.completed_files || 0;
        const downloaded = p.downloaded_files || completed; // fallback for enriched archives
        const processed = p.processed_files || 0;
        const processedBytes = p.processed_bytes || 0;
        if (total > 0) {
            const pct = Math.round(completed * 100 / total);
            let text = `${downloaded}/${total} files \u2022 ${formatBytes(p.downloaded_bytes || 0)} / ${formatBytes(p.total_size || 0)} \u2022 ${pct}%`;
            if (processed > 0) {
                text += ` (${processed} processed \u2022 ${formatBytes(processedBytes)})`;
            }
            prog.textContent = text;
            prog.style.display = "";
        } else {
            prog.style.display = "none";
        }
    }

    function updateDetailProgress() {
        if (!currentArchiveId) return;
        const archive = archives.find((a) => a.id === currentArchiveId);
        if (!archive) return;
        updateDetailProgressFromData(archive);
    }


    // Client-side archive selection (for batch operations)
    let selectedArchiveIds = new Set();

    function updateArchiveBatchActions() {
        // Update visual selection on archive items
        $$("#archive-list .archive-item").forEach(li => {
            li.classList.toggle("selected", selectedArchiveIds.has(parseInt(li.dataset.id)));
        });
    }

    function handleArchiveSelect(id, idx, e) {
        const visibleArchives = getVisibleArchives();
        if (e.shiftKey && lastClickedArchiveIdx !== null) {
            // Shift+click: range select
            const start = Math.min(lastClickedArchiveIdx, idx);
            const end = Math.max(lastClickedArchiveIdx, idx);
            if (!e.ctrlKey && !e.metaKey) selectedArchiveIds.clear();
            for (let i = start; i <= end; i++) {
                if (visibleArchives[i]) selectedArchiveIds.add(visibleArchives[i].id);
            }
        } else if (e.ctrlKey || e.metaKey) {
            // Ctrl/Cmd+click: toggle individual
            if (selectedArchiveIds.has(id)) selectedArchiveIds.delete(id);
            else selectedArchiveIds.add(id);
            lastClickedArchiveIdx = idx;
        } else {
            // Plain click: select only this one
            selectedArchiveIds.clear();
            selectedArchiveIds.add(id);
            lastClickedArchiveIdx = idx;
        }
        updateArchiveBatchActions();
    }

    function getVisibleArchives() {
        // Return the archives in display order (filtered by search if active)
        const items = $$("#archive-list .archive-item");
        return [...items].map(li => archives.find(a => a.id === parseInt(li.dataset.id))).filter(Boolean);
    }

    let lastClickedArchiveIdx = null; // for shift+click range selection

    function buildArchiveItem(a, idx, listScope) {
        const li = document.createElement("li");
        li.className = "archive-item" + (selectedArchiveIds.has(a.id) ? " selected" : "");
        li.dataset.id = a.id;
        li.innerHTML = `
            ${a.download_enabled
                ? `<button class="queue-toggle queue-remove" data-action="queue-remove" title="Remove from queue"><svg viewBox="0 0 16 16" width="14" height="14"><rect x="3" y="7" width="10" height="2" rx="1" fill="currentColor"/></svg></button>`
                : `<button class="queue-toggle queue-add" data-action="queue-add" title="Add to queue"><svg viewBox="0 0 16 16" width="14" height="14"><path d="M8 3a1 1 0 011 1v3h3a1 1 0 110 2H9v3a1 1 0 11-2 0V9H4a1 1 0 110-2h3V4a1 1 0 011-1z" fill="currentColor"/></svg></button>`
            }
            <div class="archive-info">
                <div class="archive-title">${escapeHtml(a.title || a.identifier)}</div>
                <div class="archive-meta">
                    <span>${a.files_count} files</span>
                    <span>${formatBytes(a.total_size)}</span>
                    <span>${a.identifier}</span>
                </div>
            </div>
            <span class="archive-status ${a.status}"${(a.status === 'partial' || a.status === 'downloading') && a.status_pct != null ? ` style="--pct:${a.status_pct}%"` : ''}>${(a.status === 'partial' || a.status === 'downloading') && a.status_pct != null ? a.status_pct + '%' : a.status}</span>
            <div class="archive-actions">
                <button data-action="retry" title="Retry failed files" class="retry" style="display:${a.status === 'error' ? 'flex' : 'none'}">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>
                </button>
                <button data-action="move-group" title="Move to group">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z" fill="currentColor"/></svg>
                </button>
                <button data-action="delete" class="delete" title="Remove">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" fill="currentColor"/></svg>
                </button>
            </div>
        `;

        // Drag-to-group: make archive items draggable
        li.draggable = true;
        li.addEventListener("dragstart", (e) => {
            if (e.target.closest("button, .archive-actions, .queue-toggle")) {
                e.preventDefault(); return;
            }
            // Drag all selected archives, or just this one
            if (selectedArchiveIds.has(a.id) && selectedArchiveIds.size > 1) {
                dragSrcId = Array.from(selectedArchiveIds);
            } else {
                dragSrcId = [a.id];
            }
            dragSrcGroupId = a.group_id || null;
            isDragging = true;
            li.classList.add("dragging");
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", ""); // required for Firefox
        });
        li.addEventListener("dragend", () => {
            li.classList.remove("dragging");
            isDragging = false;
            dragSrcId = null;
            dragSrcGroupId = null;
            // Remove all drag-over highlights
            $$("#archive-list .drag-over-group").forEach(el => el.classList.remove("drag-over-group"));
        });

        // Double-click to open archive detail
        li.addEventListener("dblclick", (e) => {
            if (e.target.closest("button, .archive-actions, .archive-grip, .queue-toggle")) return;
            openArchiveDetail(a.id);
        });

        // Single click: desktop-style selection (click, shift+click, ctrl+click)
        li.addEventListener("click", (e) => {
            const action = e.target.closest("[data-action]")?.dataset.action;
            if (action === "queue-add") {
                toggleArchiveDownload(a.id, true);
                return;
            } else if (action === "queue-remove") {
                toggleArchiveDownload(a.id, false);
                return;
            } else if (action === "move-up") {
                moveArchive(archives.indexOf(a), archives.indexOf(a) - 1);
                return;
            } else if (action === "move-down") {
                moveArchive(archives.indexOf(a), archives.indexOf(a) + 1);
                return;
            } else if (action === "retry") {
                retryArchive(a.id);
                return;
            } else if (action === "delete") {
                confirmDelete(a);
                return;
            } else if (action === "move-group") {
                openMoveToGroup(a);
                return;
            }

            // Desktop-style selection on the archive-info area
            if (!e.target.closest("button, .archive-actions, .archive-grip, .queue-toggle")) {
                handleArchiveSelect(a.id, idx, e);
            }
        });

        return li;
    }

    function renderArchiveList() {
        const archiveControls = $("#archive-controls");
        const archiveToolbar = $(".archive-toolbar");
        if (archives.length === 0 && groups.length === 0) {
            emptyState.style.display = "flex";
            archiveListWrap.style.display = "none";
            if (archiveControls) archiveControls.style.display = "none";
            if (archiveToolbar) archiveToolbar.style.display = "none";
            return;
        }
        emptyState.style.display = "none";
        archiveListWrap.style.display = "";
        if (archiveControls) archiveControls.style.display = "";
        if (archiveToolbar) archiveToolbar.style.display = "";
        archiveListEl.innerHTML = "";

        // Apply search filter
        const query = archiveSearchQuery.toLowerCase().trim();
        let filtered = query
            ? archives.filter((a) => {
                const title = (a.title || "").toLowerCase();
                const ident = (a.identifier || "").toLowerCase();
                return title.includes(query) || ident.includes(query);
            })
            : archives;

        // Sort within groups: apply selected sort to archives
        const statusOrder = { downloading: 0, queued: 1, partial: 2, error: 3, complete: 4, idle: 5 };
        function sortArchives(list) {
            const sorted = [...list];
            switch (archiveSort) {
                case "size":
                    sorted.sort((a, b) => (b.total_size || 0) - (a.total_size || 0));
                    break;
                case "files":
                    sorted.sort((a, b) => (b.files_count || 0) - (a.files_count || 0));
                    break;
                case "status":
                    sorted.sort((a, b) => (statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99));
                    break;
                case "added":
                    sorted.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
                    break;
                case "title":
                default:
                    sorted.sort((a, b) => (a.title || a.identifier).localeCompare(b.title || b.identifier));
                    break;
            }
            return sorted;
        }

        // Default queue-order render with groups
        // Always show groups alphabetically, with archives sorted within
        const sortedGroups = [...groups].sort((a, b) => a.name.localeCompare(b.name));
        const filteredSet = new Set(filtered.map((a) => a.id));

        // Render ungrouped archives first
        const ungrouped = sortArchives(filtered.filter((a) => !a.group_id));
        ungrouped.forEach((a, idx) => {
            archiveListEl.appendChild(buildArchiveItem(a, idx, ungrouped));
        });

        // Ungroup drop zone — visible only when groups exist, accepts drags to remove from group
        if (sortedGroups.length > 0) {
            const dropZone = document.createElement("li");
            dropZone.className = "ungroup-drop-zone";
            dropZone.innerHTML = '<div class="ungroup-divider">Ungrouped</div>';
            dropZone.addEventListener("dragover", (e) => {
                if (!isDragging) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                dropZone.classList.add("drag-over-group");
            });
            dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over-group"));
            dropZone.addEventListener("drop", async (e) => {
                e.preventDefault();
                dropZone.classList.remove("drag-over-group");
                if (!dragSrcId) return;
                for (const aid of dragSrcId) {
                    try { await api("POST", `/api/archives/${aid}/group`, { group_id: null }); } catch (e) {}
                }
                await refreshArchives();
            });
            archiveListEl.appendChild(dropZone);
        }

        sortedGroups.forEach((g, gIdx) => {
            const groupArchives = sortArchives(archives.filter((a) => a.group_id === g.id && filteredSet.has(a.id)));
            // Hide empty groups when searching
            if (query && groupArchives.length === 0) return;
            const collapsed = !expandedGroups.has(g.id);

            const header = document.createElement("li");
            header.className = "group-header" + (collapsed ? " collapsed" : "");
            header.dataset.groupId = g.id;
            header.innerHTML = `
                <div class="group-header-left">
                    <svg class="group-chevron" viewBox="0 0 24 24" width="14" height="14"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"/></svg>
                    <svg class="group-icon" viewBox="0 0 24 24" width="16" height="16"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z" fill="currentColor"/></svg>
                    <span class="group-name">${escapeHtml(g.name)}</span>
                    <span class="group-count">${groupArchives.length}</span>
                </div>
                <div class="group-actions">
                    <button data-group-action="rename" title="Rename group">
                        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="currentColor"/></svg>
                    </button>
                    <button data-group-action="delete" class="delete" title="Delete group">
                        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" fill="currentColor"/></svg>
                    </button>
                </div>
            `;

            // Click handling
            header.addEventListener("click", (e) => {
                const action = e.target.closest("[data-group-action]")?.dataset.groupAction;
                if (action === "rename") openRenameGroup(g);
                else if (action === "delete") openDeleteGroup(g);
                else if (!e.target.closest(".group-actions")) {
                    // Toggle collapse
                    if (expandedGroups.has(g.id)) expandedGroups.delete(g.id);
                    else expandedGroups.add(g.id);
                    saveExpandedGroups();
                    renderArchiveList();
                }
            });

            // Drop-to-group: accept archive drags
            header.addEventListener("dragover", (e) => {
                if (!isDragging) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                header.classList.add("drag-over-group");
            });
            header.addEventListener("dragleave", () => header.classList.remove("drag-over-group"));
            header.addEventListener("drop", async (e) => {
                e.preventDefault();
                header.classList.remove("drag-over-group");
                if (!dragSrcId) return;
                for (const aid of dragSrcId) {
                    try { await api("POST", `/api/archives/${aid}/group`, { group_id: g.id }); } catch (e) {}
                }
                await refreshArchives();
            });

            archiveListEl.appendChild(header);

            // Group's archives (hidden if collapsed)
            if (!collapsed) {
                groupArchives.forEach((a, idx) => {
                    const li = buildArchiveItem(a, idx, groupArchives);
                    li.classList.add("in-group");
                    archiveListEl.appendChild(li);
                });
            }
        });

        updateArchiveBatchActions();
    }

    async function toggleArchiveDownload(id, enabled) {
        if (enabled) {
            // Use the file-level queue-all endpoint for feedback
            const result = await api("POST", `/api/archives/${id}/files/queue-all`, { queued: true });
            if (result.added !== undefined) {
                const total = result.added + result.skipped;
                if (result.skipped > 0) {
                    addNotification(`Added ${result.added.toLocaleString()} of ${total.toLocaleString()} files to queue (${result.skipped.toLocaleString()} already queued)`, "info");
                } else if (result.added > 0) {
                    addNotification(`Added ${result.added.toLocaleString()} files to queue`, "info");
                } else {
                    addNotification("All files already queued", "info");
                }
            }
        } else {
            await api("POST", `/api/archives/${id}/files/queue-all`, { queued: false });
        }
        // Also toggle the archive download_enabled flag
        await api("POST", `/api/archives/${id}/download`, { enabled });
        await refreshArchives();
        refreshStatus();
        refreshQueueCount();
    }

    // --- Archive-Level Controls (all archives) ---

    async function retryAllArchives() {
        const btn = $("#btn-retry-all-archives");
        btn.disabled = true;
        btn.textContent = "Retrying…";
        try {
            let total = 0;
            for (const a of archives) {
                try {
                    const result = await api("POST", `/api/archives/${a.id}/retry`);
                    total += result.reset_count || 0;
                } catch (e) {}
            }
            addNotification(total > 0 ? `Retried ${total} failed file(s) across all archives` : "No failed files to retry", total > 0 ? "info" : "warning");
        } finally {
            btn.disabled = false;
            btn.textContent = "Retry All Archives";
        }
    }

    async function refreshAllMetadata() {
        const btn = $("#btn-refresh-all-meta");
        btn.disabled = true;
        btn.textContent = "Refreshing…";
        try {
            let changes = 0;
            for (const a of archives) {
                try {
                    const result = await api("POST", `/api/archives/${a.id}/refresh`);
                    const s = result.summary;
                    changes += (s.new || 0) + (s.removed || 0) + (s.changed || 0);
                } catch (e) {}
            }
            addNotification(changes > 0 ? `Metadata refresh: ${changes} change(s) across all archives` : "Metadata refresh: no changes detected", changes > 0 ? "warning" : "info");
            await refreshArchives();
        } finally {
            btn.disabled = false;
            btn.textContent = "Refresh All Metadata";
        }
    }

    async function scanAllArchives() {
        const btn = $("#btn-scan-all-archives");
        btn.disabled = true;
        btn.textContent = "Scanning…";
        try {
            let queued = 0;
            for (const a of archives) {
                try { await api("POST", `/api/archives/${a.id}/scan`); queued++; } catch (e) {}
            }
            addNotification(`Queued scan for ${queued} archive(s)`, "info");
        } finally {
            btn.disabled = false;
            btn.textContent = "Scan For Files In All Archives";
        }
    }

    // --- Archive Batch Actions ---

    async function archiveBatchScan() {
        if (selectedArchiveIds.size === 0) return;
        let queued = 0;
        for (const aid of selectedArchiveIds) {
            try { await api("POST", `/api/archives/${aid}/scan`); queued++; } catch (e) {}
        }
        addNotification(`Queued scan for ${queued} archive(s)`, "info");
        selectedArchiveIds.clear();
        updateArchiveBatchActions();
    }

    async function archiveBatchAutoTag() {
        if (selectedArchiveIds.size === 0) return;
        let tagged = 0;
        for (const aid of selectedArchiveIds) {
            try { await api("POST", `/api/archives/${aid}/auto-tag`); tagged++; } catch (e) {}
        }
        addNotification(`Scanned tags for ${tagged} archive(s)`, "info");
        selectedArchiveIds.clear();
        updateArchiveBatchActions();
        if (currentArchiveId) loadArchiveTagsAndCollections(currentArchiveId);
    }

    let pendingBatchArchiveProcessIds = null;

    async function archiveBatchProcess() {
        if (selectedArchiveIds.size === 0) return;
        // Store archive IDs and open process modal; on confirm, process all
        pendingBatchArchiveProcessIds = Array.from(selectedArchiveIds);
        openProcessArchiveModal();
    }

    async function archiveBatchRetry() {
        if (selectedArchiveIds.size === 0) return;
        let total = 0;
        for (const aid of selectedArchiveIds) {
            try {
                const result = await api("POST", `/api/archives/${aid}/retry`);
                total += result.reset_count || 0;
            } catch (e) {}
        }
        addNotification(`Retried ${total} failed file(s) across ${selectedArchiveIds.size} archive(s)`, "info");
        selectedArchiveIds.clear();
        updateArchiveBatchActions();
        await refreshArchives();
    }

    async function archiveBatchDeleteFolders() {
        if (selectedArchiveIds.size === 0) return;
        const count = selectedArchiveIds.size;
        confirmAction(
            "confirm_delete_folders",
            "Delete Download Folders",
            `Delete download folder(s) for <strong>${count}</strong> selected archive(s)?<br><br>This will remove the downloaded files from disk but keep the archives in the list.`,
            async () => {
                const ids = Array.from(selectedArchiveIds);
                let deleted = 0;
                for (const aid of ids) {
                    try { const r = await api("POST", `/api/archives/${aid}/delete-folder`); if (r.ok) deleted++; } catch (e) {}
                }
                addNotification(`Deleted folders for ${deleted}/${ids.length} archives`, "info");
                selectedArchiveIds.clear();
                renderArchiveList();
            },
            { confirmText: "Delete Folders" }
        );
    }

    // --- Archive List Context Menu ---
    archiveListEl.addEventListener("contextmenu", (e) => {
        const li = e.target.closest(".archive-item");
        if (!li) return;
        e.preventDefault();
        const id = parseInt(li.dataset.id);

        // Modifier key handling: ctrl/shift behave as single-click before opening menu
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
            const visibleArchives = getVisibleArchives();
            const idx = visibleArchives.findIndex(a => a.id === id);
            if (idx !== -1) handleArchiveSelect(id, idx, e);
        } else if (!selectedArchiveIds.has(id)) {
            selectedArchiveIds.clear();
            selectedArchiveIds.add(id);
            updateArchiveBatchActions();
        }

        const n = selectedArchiveIds.size;
        showContextMenu(e, [
            { label: "Scan Existing Files", action: archiveBatchScan },
            { label: "Scan for Tags", action: archiveBatchAutoTag },
            { label: "Process Archive", action: archiveBatchProcess },
            { label: "Retry All Files", action: archiveBatchRetry },
            { separator: true },
            { label: `Delete Folders (${n})`, action: archiveBatchDeleteFolders, danger: true },
            { separator: true },
            { label: "Deselect", action: () => { selectedArchiveIds.clear(); updateArchiveBatchActions(); } },
        ]);
    });

    async function moveArchive(fromIdx, toIdx) {
        if (toIdx < 0 || toIdx >= archives.length) return;
        // Only allow reorder within the same enabled/disabled group
        if (archives[fromIdx].download_enabled !== archives[toIdx].download_enabled) return;
        const order = archives.map((x) => x.id);
        const [moved] = order.splice(fromIdx, 1);
        order.splice(toIdx, 0, moved);
        await api("POST", "/api/archives/reorder", { order });
    }

    async function retryArchive(id) {
        await api("POST", `/api/archives/${id}/retry`);
        await refreshArchives();
        if (currentArchiveId === id) loadFiles();
    }

    async function retryFile(fileId) {
        await api("POST", `/api/files/${fileId}/retry`);
        loadFiles();
    }

    // --- Refresh Metadata ---

    async function refreshMetadata() {
        if (!currentArchiveId) return;
        const btn = $("#btn-refresh-meta");
        btn.disabled = true;
        btn.textContent = "Checking...";

        try {
            const result = await api("POST", `/api/archives/${currentArchiveId}/refresh`);
            const s = result.summary;
            const parts = [];
            if (s.new > 0) parts.push(`${s.new} new`);
            if (s.removed > 0) parts.push(`${s.removed} removed`);
            if (s.changed > 0) parts.push(`${s.changed} changed`);
            if (parts.length === 0) {
                addNotification("Metadata refresh: no changes detected", "info");
            } else {
                addNotification("Metadata refresh: " + parts.join(", "), parts.length > 0 ? "warning" : "info");
            }
            await loadFiles();
            await refreshArchives();
        } catch (e) {
            addNotification("Metadata refresh failed: " + e.message, "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Refresh Archive Metadata";
        }
    }

    async function scanExistingFiles() {
        if (!currentArchiveId) return;
        const archiveName = getArchiveName(currentArchiveId);
        const fileCount = vsFiles.length;
        const msg = `This archive has ${fileCount.toLocaleString()} file${fileCount !== 1 ? "s" : ""}. Add all to scan queue?`
            + `<label style="display:flex;align-items:center;gap:6px;margin-top:10px;font-size:0.92em;cursor:pointer">`
            + `<input type="checkbox" id="scan-match-by-name" checked>`
            + `<span>Match by filename <span style="opacity:0.6">(associate files with same name but different extension, e.g. dog.chd → dog.zip)</span></span></label>`;
        confirmAction("confirm_scan_archive", "Scan Archive", msg, async () => {
            const matchByName = document.getElementById("scan-match-by-name")?.checked ?? false;
            try {
                await api("POST", `/api/archives/${currentArchiveId}/scan`, { match_by_name: matchByName });
                updateScanButton();
            } catch (e) {
                if (e.message && e.message.includes("already queued")) {
                    addNotification(`Scan "${archiveName}": already queued`, "info");
                } else {
                    addNotification(`Scan "${archiveName}" failed: ` + e.message, "error");
                }
            }
        });
    }

    async function cancelScan(archiveId) {
        try {
            await api("POST", `/api/archives/${archiveId}/scan/cancel`);
        } catch (e) {
            // Scan may have already finished
        }
    }

    async function cancelProcessing(archiveId) {
        try {
            await api("POST", `/api/archives/${archiveId}/process/cancel`);
        } catch (e) {
            // Processing may have already finished
        }
    }

    function updateScanButton() {
        const btn = $("#btn-scan-files");
        if (!btn) return;
        // Check for active scan notification from server
        // Scan active state is now tracked via scan queue, not notifications
        const active = false;
        btn.disabled = !!active;
        btn.style.opacity = active ? "0.5" : "";
        btn.title = active
            ? "Scan already in progress or queued for this archive"
            : "Scan local folder for existing files";
    }

    async function clearChanges() {
        if (!currentArchiveId) return;
        await api("POST", `/api/archives/${currentArchiveId}/clear-changes`);
        await loadFiles();
    }

    // --- Force Resume Conflict ---

    let pendingForceResumeId = null;

    function openForceResume(info) {
        pendingForceResumeId = info.id;
        $("#force-resume-info").innerHTML =
            `<strong>${escapeHtml(info.name)}</strong><br>` +
            `Reason: ${escapeHtml(info.error)}`;
        if (info.size > 0) {
            // We don't know the on-disk size from the file list data, but the error message
            // for size mismatches contains it. Show the manifest size for context.
            $("#force-resume-progress").textContent =
                `Expected size: ${formatBytes(info.size)}. ` +
                `Forcing resume will mark this file as pending and the downloader will attempt to resume or re-download it.`;
        } else {
            $("#force-resume-progress").textContent =
                "Forcing resume will mark this file as pending and the downloader will attempt to re-download it.";
        }
        $("#modal-force-resume").classList.add("open");
    }

    async function doForceResume() {
        if (!pendingForceResumeId) return;
        try {
            await api("POST", `/api/files/${pendingForceResumeId}/force-resume`);
            addNotification("Conflict resolved — file queued for download", "success");
            await loadFiles();
            await refreshArchives();
        } catch (e) {
            addNotification("Failed to resolve conflict: " + e.message, "error");
        }
        $("#modal-force-resume").classList.remove("open");
        pendingForceResumeId = null;
    }

    // --- Archive Detail ---

    async function openArchiveDetail(id) {
        currentArchiveId = id;

        currentSort = "name";
        currentSortDir = "";
        fileSearchQuery = "";
        selectedFileIds.clear();
        vsFiles = [];
        vsLastRange = null;
        vsExpandedIds.clear();
        vsExpandedFolders.clear();
        vsProcessedCache = {};
        vsInvalidateDescriptors();
        $("#file-sort").value = "name";
        $("#file-search").value = "";
        $(".file-table-wrap").scrollTop = 0;
        $$(".page").forEach((p) => p.classList.remove("active"));
        pageDetail.classList.add("active");

        const archive = archives.find((a) => a.id === id);
        if (archive) {
            $("#detail-title").textContent = archive.title || archive.identifier;
            $("#detail-meta").textContent = `${archive.files_count} files \u2022 ${formatBytes(archive.total_size)} \u2022 ${archive.identifier}`;
            updateDetailProgress();
        }
        await loadFiles();
        updateScanButton();
        loadArchiveTagsAndCollections(id);
        updateAutoProcessControls(id);
    }

    async function updateAutoProcessControls(archiveId) {
        const toggle = $("#auto-process-toggle");
        const select = $("#auto-process-profile");
        if (!toggle || !select) return;

        const archive = archives.find(a => a.id === archiveId);
        const currentProfileId = archive ? archive.processing_profile_id : null;

        // Populate profile dropdown
        const profiles = await loadProcessingProfiles();
        select.innerHTML = "";
        for (const p of profiles) {
            const o = document.createElement("option");
            o.value = p.id;
            o.textContent = p.name;
            select.appendChild(o);
        }

        if (profiles.length === 0) {
            // No profiles — disable both controls
            toggle.checked = false;
            toggle.disabled = true;
            select.style.display = "none";
            return;
        }

        toggle.disabled = false;
        if (currentProfileId) {
            toggle.checked = true;
            select.value = currentProfileId;
            select.style.display = "";
        } else {
            toggle.checked = false;
            select.style.display = "none";
        }
    }

    function closeDetail() {
        currentArchiveId = null;
        vsFiles = [];
        vsExpandedIds.clear();
        vsLastRange = null;
        selectedFileIds.clear();
        updateBatchActions();
        pageDetail.classList.remove("active");
        pageHome.classList.add("active");
        refreshArchives();
    }

    async function loadFiles() {
        if (!currentArchiveId) return;
        const gen = ++loadFilesGen;
        const archiveId = currentArchiveId;
        try {
            const searchParam = fileSearchQuery ? `&search=${encodeURIComponent(fileSearchQuery)}` : "";
            const dirParam = currentSortDir ? `&sort_dir=${currentSortDir}` : "";
            const data = await api("GET", `/api/archives/${archiveId}/files?sort=${currentSort}${dirParam}${searchParam}`);
            if (gen !== loadFilesGen || archiveId !== currentArchiveId) return; // Stale response
            vsFiles = data.files;
            vsAllQueued = data.all_queued;
            vsExpandedIds.clear();
            vsInvalidateDescriptors();
            renderFiles(data);
            if (data.progress) updateDetailProgressFromData(data.progress);
        } catch (e) {
            if (gen !== loadFilesGen || archiveId !== currentArchiveId) return;
            vsFiles = [];
            fileListEl.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--danger)">${escapeHtml(e.message)}</td></tr>`;
        }
    }

    const SORT_COL_MAP = { "col-name": "name", "col-size": "size", "col-modified": "modified", "col-status": "status" };
    const SORT_DEFAULTS = { name: "asc", name_flat: "asc", size: "desc", modified: "desc", status: "asc", priority: "asc" };

    function updateSortArrows() {
        // Text column headers
        for (const [cls, sort] of Object.entries(SORT_COL_MAP)) {
            const th = $(`.${cls}`);
            if (!th) continue;
            const label = th.textContent.replace(/[\u25B2\u25BC]/g, "").trim();
            if (sort === currentSort) {
                const dir = currentSortDir || SORT_DEFAULTS[sort];
                const arrow = dir === "asc" ? "\u25B2" : "\u25BC";
                th.innerHTML = `${label} <span class="sort-arrow">${arrow}</span>`;
            } else {
                th.textContent = label;
            }
        }
        // Priority column header — just an arrow
        const priTh = $(".col-priority-sort");
        if (priTh) {
            const dir = currentSortDir || SORT_DEFAULTS["priority"];
            const arrow = dir === "asc" ? "\u25B2" : "\u25BC";
            priTh.innerHTML = `<span class="sort-arrow">${arrow}</span>`;
        }
    }

    function onColumnHeaderClick(sortKey) {
        if (currentSort === sortKey) {
            // Toggle direction
            const def = SORT_DEFAULTS[sortKey];
            const cur = currentSortDir || def;
            currentSortDir = cur === "asc" ? "desc" : "asc";
        } else {
            currentSort = sortKey;
            currentSortDir = "";
            $("#file-sort").value = sortKey;
        }

        $(".file-table-wrap").scrollTop = 0;
        loadFiles();
        updateSortArrows();
    }

    // Client-side file selection (independent of download queue)
    let selectedFileIds = new Set();

    function syncSelectAll() {
        updateBatchActions();
        // Update visual selection on visible rows
        updateFileSelectionClasses();
    }

    function updateFileSelectionClasses() {
        fileListEl.querySelectorAll("tr[data-file-id]").forEach(tr => {
            const fid = parseInt(tr.dataset.fileId);
            tr.classList.toggle("selected", selectedFileIds.has(fid));
        });
    }

    function updateBatchActions() {
        // Batch bar removed — just update visual selection classes
        updateFileSelectionClasses();
    }

    // --- File table header (dynamic based on sort mode) ---

    function rebuildTableHeader() {
        const isPriority = currentSort === "priority";
        const thead = fileListEl.closest("table").querySelector("thead tr");
        thead.innerHTML = "";
        if (isPriority) {
            thead.innerHTML += '<th class="col-grip col-priority-sort" title="Download Order"></th>';
        }
        thead.innerHTML += '<th class="col-queue"></th>';
        thead.innerHTML += '<th class="col-name">Name</th>';
        thead.innerHTML += '<th class="col-size">Size</th>';
        thead.innerHTML += '<th class="col-modified">Modified</th>';
        thead.innerHTML += '<th class="col-status">Status</th>';
        if (isPriority) {
            thead.innerHTML += '<th class="col-priority"></th>';
        }
        // Priority column sort header (stack icon)
        const priTh = $(".col-priority-sort");
        if (priTh) {
            priTh.style.cursor = "pointer";
            priTh.addEventListener("click", () => onColumnHeaderClick("priority"));
        }
        // Re-attach column header sort handlers
        for (const [cls, sort] of Object.entries(SORT_COL_MAP)) {
            const th = $(`.${cls}`);
            if (th) {
                th.style.cursor = "pointer";
                th.addEventListener("click", () => onColumnHeaderClick(sort));
            }
        }
        updateSortArrows();
    }

    function getColspan() {
        return currentSort === "priority" ? 8 : 6;
    }

    // --- File drag-and-drop for priority mode ---

    let fileDragSrcId = null;

    function buildGripCell() {
        return `<td class="col-grip"><div class="file-grip" title="Drag to reorder">` +
            `<div class="grip-dots"><span></span><span></span></div>` +
            `<div class="grip-dots"><span></span><span></span></div>` +
            `<div class="grip-dots"><span></span><span></span></div></div></td>`;
    }

    function buildPriorityCell(fileId, isFirst, isLast) {
        return `<td class="col-priority"><div class="file-priority-btns">` +
            `<button data-move-up="${fileId}" title="Move up" ${isFirst ? "disabled" : ""}>` +
            `<svg viewBox="0 0 24 24" width="16" height="16"><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" fill="currentColor"/></svg></button>` +
            `<button data-move-down="${fileId}" title="Move down" ${isLast ? "disabled" : ""}>` +
            `<svg viewBox="0 0 24 24" width="16" height="16"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"/></svg></button>` +
            `</div></td>`;
    }

    async function moveFile(fileId, direction) {
        // Swap this file with its neighbour in the visible list
        const rows = Array.from(fileListEl.querySelectorAll("tr[data-file-id]"));
        const idx = rows.findIndex((r) => r.dataset.fileId == fileId);
        const swapIdx = idx + direction;
        if (swapIdx < 0 || swapIdx >= rows.length) return;

        // Only allow reorder among queued files (the top group in priority mode)
        const thisQueued = !!rows[idx].querySelector(".queue-remove");
        const swapQueued = !!rows[swapIdx].querySelector(".queue-remove");
        if (!thisQueued || !swapQueued) return;

        // Build new order of all file IDs from the current page rows
        const order = rows.map((r) => parseInt(r.dataset.fileId));
        const [moved] = order.splice(idx, 1);
        order.splice(swapIdx, 0, moved);
        await api("POST", `/api/archives/${currentArchiveId}/files/reorder`, { order });
        await loadFiles();
    }

    function attachPriorityDrag(tr, fileId) {
        tr.draggable = true;
        tr.addEventListener("dragstart", (e) => {
            fileDragSrcId = fileId;
            tr.classList.add("file-row-dragging");
            e.dataTransfer.effectAllowed = "move";
        });
        tr.addEventListener("dragend", () => {
            fileDragSrcId = null;
            tr.classList.remove("file-row-dragging");
            fileListEl.querySelectorAll(".file-row-drag-over").forEach((r) => r.classList.remove("file-row-drag-over"));
        });
        tr.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            tr.classList.add("file-row-drag-over");
        });
        tr.addEventListener("dragleave", () => tr.classList.remove("file-row-drag-over"));
        tr.addEventListener("drop", async (e) => {
            e.preventDefault();
            tr.classList.remove("file-row-drag-over");
            if (fileDragSrcId === null || fileDragSrcId === fileId) return;

            // Only rearrange among queued rows
            const rows = Array.from(fileListEl.querySelectorAll("tr[data-file-id]"));
            const selectedRows = rows.filter((r) => !!r.querySelector(".queue-remove"));
            const fromIdx = selectedRows.findIndex((r) => r.dataset.fileId == fileDragSrcId);
            const toIdx = selectedRows.findIndex((r) => r.dataset.fileId == fileId);
            if (fromIdx === -1 || toIdx === -1) return;

            const order = selectedRows.map((r) => parseInt(r.dataset.fileId));
            const [moved] = order.splice(fromIdx, 1);
            order.splice(toIdx, 0, moved);
            await api("POST", `/api/archives/${currentArchiveId}/files/reorder`, { order });
            fileDragSrcId = null;
            await loadFiles();
        });
    }

    // --- Unknown file drag-and-drop (assign as processed output) ---

    let unknownDragSrcId = null;
    let unknownDragActive = false;

    function attachUnknownDrag(tr, fileId) {
        const grip = tr.querySelector(".unknown-grip");
        if (!grip) return;
        grip.draggable = true;
        grip.addEventListener("dragstart", (e) => {
            e.stopPropagation();
            unknownDragSrcId = fileId;
            unknownDragActive = true;
            tr.classList.add("file-row-dragging");
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", String(fileId));
            e.dataTransfer.setDragImage(tr, 0, 0);
        });
        grip.addEventListener("dragend", () => {
            setTimeout(() => {
                unknownDragSrcId = null;
                unknownDragActive = false;
                tr.classList.remove("file-row-dragging");
                fileListEl.querySelectorAll(".file-row-drop-target").forEach((r) => r.classList.remove("file-row-drop-target"));
            }, 0);
        });
    }

    function attachOutputDropTarget(tr, targetFileId) {
        let dragOverCount = 0;
        tr.addEventListener("dragover", (e) => {
            if (unknownDragSrcId === null) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
        });
        tr.addEventListener("dragenter", (e) => {
            if (unknownDragSrcId === null) return;
            e.preventDefault();
            dragOverCount++;
            tr.classList.add("file-row-drop-target");
        });
        tr.addEventListener("dragleave", () => {
            dragOverCount--;
            if (dragOverCount <= 0) {
                dragOverCount = 0;
                tr.classList.remove("file-row-drop-target");
            }
        });
        tr.addEventListener("drop", async (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragOverCount = 0;
            tr.classList.remove("file-row-drop-target");
            const srcId = unknownDragSrcId;
            unknownDragSrcId = null;
            unknownDragActive = false;
            if (srcId === null || srcId === targetFileId) return;
            try {
                await api("POST", `/api/files/${targetFileId}/assign-output`, { unknown_file_id: srcId });
                addNotification("File assigned as processed output", "success");
                loadFiles();
            } catch (err) {
                addNotification("Assign failed: " + err.message, "error");
            }
        });
    }

    // --- Render file list ---

    // --- Virtual-scrolled file list ---

    // ── File tree building ──────────────────────────────────────────────

    /**
     * Build a tree structure from flat file paths.
     * Returns { folders: { name: treeNode }, files: [file, ...] }
     * where treeNode = { name, path, folders, files, fileCount }
     */
    function vsBuildTree(files) {
        const root = { name: "", path: "", folders: {}, files: [], fileCount: 0 };
        for (const f of files) {
            const parts = f.name.replace(/\\/g, "/").split("/");
            let node = root;
            // Walk path segments — last part is the filename
            for (let i = 0; i < parts.length - 1; i++) {
                const seg = parts[i];
                if (!node.folders[seg]) {
                    const folderPath = node.path ? node.path + "/" + seg : seg;
                    node.folders[seg] = { name: seg, path: folderPath, folders: {}, files: [], fileCount: 0 };
                }
                node = node.folders[seg];
            }
            node.files.push(f);
        }
        // Count total files recursively
        function countFiles(n) {
            let c = n.files.length;
            for (const sub of Object.values(n.folders)) c += countFiles(sub);
            n.fileCount = c;
            return c;
        }
        countFiles(root);
        return root;
    }

    /**
     * Flatten tree into row descriptors respecting expand/collapse state.
     * Returns array of:
     *   { type: "folder",    path, name, depth, fileCount, folderType: "archive"|"processed", sourceFileId? }
     *   { type: "file",      file, depth, processedPath?, processedExpanded? }
     *   { type: "divider" }
     *
     * Files with processed outputs become expandable (with chevron + folder icon).
     * Processed children appear directly after the file row when expanded.
     */
    function vsFlattenTree(tree, isPriority) {
        const rows = [];

        // Priority mode: queued/divider/unqueued at root (no tree nesting)
        if (isPriority) {
            const hasQueued = vsFiles.some(f => f.queue_position != null);
            const hasUnqueued = vsFiles.some(f => f.queue_position == null);
            const needsDivider = hasQueued && hasUnqueued;
            let dividerInserted = false;
            for (const f of vsFiles) {
                if (needsDivider && !dividerInserted && f.queue_position == null) {
                    dividerInserted = true;
                    rows.push({ type: "divider" });
                }
                rows.push({ type: "file", file: f, depth: 0 });
            }
            return rows;
        }

        // Normal mode: recursive tree flattening
        function flattenNode(node, depth) {
            // Sort folders alphabetically, then files in current order
            const folderNames = Object.keys(node.folders).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
            for (const fname of folderNames) {
                const sub = node.folders[fname];
                const expanded = vsExpandedFolders.has(sub.path);
                rows.push({
                    type: "folder", path: sub.path, name: sub.name,
                    depth, fileCount: sub.fileCount, folderType: "archive",
                });
                if (expanded) {
                    flattenNode(sub, depth + 1);
                }
            }
            for (const f of node.files) {
                // If this file is processed, mark it as expandable inline
                if (f.has_processed) {
                    const processedPath = "__processed__" + f.id;
                    const expanded = vsExpandedFolders.has(processedPath);
                    rows.push({ type: "file", file: f, depth, processedPath, processedExpanded: expanded });
                    if (expanded && vsProcessedCache[f.id]) {
                        flattenProcessedTree(vsProcessedCache[f.id], depth + 1, rows);
                    }
                } else {
                    rows.push({ type: "file", file: f, depth });
                }
            }
        }

        flattenNode(tree, 0);
        return rows;
    }

    /**
     * Flatten a processed tree (from /api/files/{id}/processed-tree) into rows.
     */
    function flattenProcessedTree(treeNodes, depth, rows) {
        for (const node of treeNodes) {
            if (node.type === "dir") {
                const dirPath = "__pdir__" + node.path;
                const expanded = vsExpandedFolders.has(dirPath);
                rows.push({
                    type: "folder", path: dirPath, name: node.name,
                    depth, fileCount: node.children ? node.children.length : 0,
                    folderType: "processed",
                });
                if (expanded && node.children) {
                    flattenProcessedTree(node.children, depth + 1, rows);
                }
            } else {
                rows.push({
                    type: "pfile", name: node.name, size: node.size,
                    mtime: node.mtime, ppath: node.path, depth,
                });
            }
        }
    }

    /**
     * Build row descriptors from current vsFiles array.
     * Caches result in vsRowDescriptorsCache; invalidated by folder toggle or data reload.
     */
    function vsGetRowDescriptors(files, isPriority) {
        if (vsRowDescriptorsCache) return vsRowDescriptorsCache;
        const tree = vsBuildTree(files);
        const rows = vsFlattenTree(tree, isPriority);
        vsRowDescriptorsCache = rows;
        return rows;
    }

    /** Invalidate cached row descriptors (call after folder toggle, data reload, etc.) */
    function vsInvalidateDescriptors() {
        vsRowDescriptorsCache = null;
        vsLastRange = null;
    }

    // SVG icons for folders
    const FOLDER_ICON_ARCHIVE = `<svg class="folder-icon" viewBox="0 0 16 16" width="14" height="14"><path d="M14 4H8L6.5 2.5h-5l-.5.5v10l.5.5h13l.5-.5V4.5L14 4zM13 12H2V4h4l1.5 1.5H13V12z" fill="currentColor"/></svg>`;
    const FOLDER_ICON_PROCESSED = `<svg class="folder-icon" viewBox="0 0 16 16" width="14" height="14"><path d="M14 4H8L6.5 2.5h-5l-.5.5v10l.5.5h13l.5-.5V4.5L14 4z" fill="currentColor" opacity="0.85"/><path d="M6 8l2 2.5L10 8" fill="none" stroke="var(--bg)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const CHEVRON_RIGHT = `<svg class="folder-chevron" viewBox="0 0 16 16" width="12" height="12"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const CHEVRON_DOWN = `<svg class="folder-chevron" viewBox="0 0 16 16" width="12" height="12"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

    /**
     * Build a folder <tr> element.
     * desc = { type: "folder", path, name, depth, fileCount, folderType, sourceFileId? }
     */
    function buildFolderRow(desc, isPriority) {
        const tr = document.createElement("tr");
        tr.className = "folder-row folder-" + desc.folderType;
        tr.dataset.folderPath = desc.path;
        const expanded = vsExpandedFolders.has(desc.path);
        if (expanded) tr.classList.add("expanded");

        const indent = desc.depth * 20;
        const chevron = expanded ? CHEVRON_DOWN : CHEVRON_RIGHT;
        const icon = desc.folderType === "processed" ? FOLDER_ICON_PROCESSED : FOLDER_ICON_ARCHIVE;
        const countBadge = desc.fileCount >= 0 ? `<span class="folder-count">${desc.fileCount}</span>` : "";
        const nameHtml = escapeHtml(desc.name);

        let html = "";
        if (isPriority) html += '<td class="col-grip"></td>';
        html += '<td class="col-queue"></td>';
        html += `<td class="col-name"><div class="file-name-wrap" style="padding-left:${indent}px">` +
            `<span class="folder-toggle">${chevron}</span>${icon}` +
            `<span class="folder-name">${nameHtml}</span>${countBadge}` +
            `</div></td>`;
        html += '<td class="col-size"></td>';
        html += '<td class="col-modified"></td>';
        html += '<td class="col-status"></td>';
        if (isPriority) html += '<td class="col-priority"></td>';
        tr.innerHTML = html;

        // Click anywhere on the row toggles expand/collapse
        tr.addEventListener("click", async () => {
            if (expanded) {
                vsExpandedFolders.delete(desc.path);
            } else {
                vsExpandedFolders.add(desc.path);
                // Lazy-load processed tree if this is a processed folder
                if (desc.folderType === "processed" && desc.sourceFileId && !vsProcessedCache[desc.sourceFileId]) {
                    try {
                        const data = await api("GET", `/api/files/${desc.sourceFileId}/processed-tree`);
                        vsProcessedCache[desc.sourceFileId] = data.tree || [];
                    } catch (e) {
                        vsProcessedCache[desc.sourceFileId] = [];
                    }
                }
            }
            vsInvalidateDescriptors();
            vsRenderVisible();
        });

        return tr;
    }

    /**
     * Build a processed-file <tr> (processed output of a source file).
     * desc = { type: "pfile", name, size, mtime, ppath, depth }
     */
    function buildProcessedFileRow(desc) {
        const tr = document.createElement("tr");
        tr.className = "pfile-row";
        const indent = desc.depth * 20;
        const fileIcon = `<svg class="pfile-icon" viewBox="0 0 16 16" width="13" height="13"><path d="M3 1h6l4 4v10H3V1zm6 0v4h4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
        const nameHtml = escapeHtml(desc.name);
        let html = '<td class="col-queue"></td>';
        html += `<td class="col-name"><div class="file-name-wrap" style="padding-left:${indent}px">` +
            `${fileIcon}<span class="file-name pfile-name">${nameHtml}</span>` +
            `</div></td>`;
        html += `<td class="col-size" style="text-align:right">${desc.size > 0 ? formatBytes(desc.size) : ""}</td>`;
        html += `<td class="col-modified">${desc.mtime ? formatDate(desc.mtime) : ""}</td>`;
        html += '<td class="col-status"></td>';
        tr.innerHTML = html;
        return tr;
    }

    // Build a single file <tr> element with all cells and event listeners.
    function buildFileRow(f, isPriority, queuedFiles, lastQueuedIdx, depth, desc) {
        const tr = document.createElement("tr");
        tr.dataset.fileId = f.id;
        if (isFlashing(tr)) tr.classList.add("queue-flash");

        // Processed files get the processed-folder styling
        const hasProcessedFolder = desc && desc.processedPath;
        if (hasProcessedFolder) tr.classList.add("folder-processed");

        if (f.change_status) tr.className += (tr.className ? " " : "") + "file-row-" + f.change_status;

        const changeIcon = f.change_status
            ? `<span class="change-info ${f.change_status}" aria-label="${escapeHtml(f.change_detail)}">` +
              `<span class="change-tooltip">${escapeHtml(f.change_detail)}</span>` +
              (f.change_status === "new" ? "+" : f.change_status === "removed" ? "\u2212" : "\u0394") +
              `</span>`
            : "";

        let html = "";
        const isUnknown = f.download_status === "unknown";
        const isQueued = f.queue_position != null;

        // Grip column
        if (isPriority) {
            html += (isQueued && !isUnknown) ? buildGripCell() : '<td class="col-grip"></td>';
        }

        const hasProcessedOutput = !!f.has_processed;
        const procStatus = (f.process_queue_status === "processing" || f.process_queue_status === "queued") ? f.process_queue_status
            : f.process_queue_status === "failed" ? "failed"
            : hasProcessedOutput ? "processed" : "";
        const sourceDeleted = (f.downloaded === 0 && hasProcessedOutput);
        const hideQueue = isUnknown || (hasProcessedOutput && !sourceDeleted);
        if (hideQueue) {
            html += '<td class="col-queue"></td>';
        } else if (isQueued) {
            html += `<td class="col-queue"><button class="queue-toggle queue-remove" data-queue-id="${f.id}" title="Remove from queue">` +
                `<svg viewBox="0 0 16 16" width="14" height="14"><rect x="3" y="7" width="10" height="2" rx="1" fill="currentColor"/></svg></button></td>`;
        } else {
            html += `<td class="col-queue"><button class="queue-toggle queue-add" data-queue-id="${f.id}" title="Add to queue">` +
                `<svg viewBox="0 0 16 16" width="14" height="14"><path d="M8 3a1 1 0 011 1v3h3a1 1 0 110 2H9v3a1 1 0 11-2 0V9H4a1 1 0 110-2h3V4a1 1 0 011-1z" fill="currentColor"/></svg></button></td>`;
        }

        // Selection indicated by row class, no checkbox

        const renameBtn = isUnknown
            ? `<button class="file-action-btn" data-action="rename" data-file-id="${f.id}" data-file-name="${escapeHtml(f.name)}" title="Rename">` +
              `<svg viewBox="0 0 16 16" width="13" height="13"><path d="M12.15 2.85a1.2 1.2 0 00-1.7 0L3.5 9.8l-.8 3.5 3.5-.8 6.95-6.95a1.2 1.2 0 000-1.7z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg></button>`
            : "";
        const mayExistOnDisk = ["completed", "conflict", "failed", "downloading"].includes(f.download_status);
        const showDelete = isUnknown || mayExistOnDisk;
        const deleteTitle = isUnknown ? "Delete file" : "Delete from disk";
        const deleteBtn = showDelete
            ? `<button class="file-action-btn file-action-danger" data-action="delete" data-file-id="${f.id}" data-file-name="${escapeHtml(f.name)}" data-file-origin="${f.origin || 'manifest'}" title="${deleteTitle}">` +
              `<svg viewBox="0 0 16 16" width="13" height="13"><path d="M5.5 2h5M3 4h10M6 4v8m4-8v8M4.5 4l.5 9h6l.5-9" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button>`
            : "";
        const processBtn = mayExistOnDisk || isUnknown
            ? `<button class="file-action-btn" data-action="process" data-file-id="${f.id}" title="Process file">` +
              `<svg viewBox="0 0 16 16" width="13" height="13"><path d="M4 2l9 6-9 6V2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg></button>`
            : "";
        const rescanBtn =
            `<button class="file-action-btn" data-action="rescan" data-file-id="${f.id}" title="Re-scan file">` +
            `<svg viewBox="0 0 16 16" width="13" height="13"><path d="M8 2.5V1L5.5 3.5 8 6V4.5a3.5 3.5 0 11-3.16 5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg></button>`;
        const unknownGrip = isUnknown
            ? `<div class="unknown-grip" title="Drag onto a file to assign as output">` +
              `<div class="grip-dots"><span></span><span></span></div>` +
              `<div class="grip-dots"><span></span><span></span></div>` +
              `<div class="grip-dots"><span></span><span></span></div></div>`
            : "";
        const indent = (depth || 0) * 20;
        // Show only leaf filename when tree view provides folder context
        const displayName = (depth != null && depth > 0) ? f.name.replace(/\\/g, "/").split("/").pop() : f.name;
        // Processed files get a chevron + folder icon prefix
        const procChevron = hasProcessedFolder
            ? `<span class="folder-toggle">${desc.processedExpanded ? CHEVRON_DOWN : CHEVRON_RIGHT}</span>${FOLDER_ICON_PROCESSED}`
            : "";
        html += `<td class="col-name"><div class="file-name-wrap" style="padding-left:${indent}px">` +
            unknownGrip + procChevron +
            renderFileName(displayName, sourceDeleted ? "file-name-deleted" : f.downloaded ? "file-name-downloaded" : "") + changeIcon +
            `<span class="file-actions">` +
            renameBtn + processBtn + rescanBtn + deleteBtn +
            `</span></div></td>`;

        html += `<td class="col-size" style="text-align:right">${formatBytes(f.size)}</td>`;
        html += `<td class="col-modified">${formatDate(f.mtime)}</td>`;
        const displayStatus = formatFileStatus(f);
        const isSkipped = !isQueued && f.download_status === "pending";
        const isPartial = isSkipped && f.downloaded_bytes > 0 && f.size > 0;
        const statusClass = procStatus === "processed" ? "processed"
            : procStatus === "failed" ? "proc-failed"
            : procStatus === "processing" || procStatus === "queued" ? "proc-active"
            : isPartial ? "partial"
            : isSkipped ? "skipped"
            : f.download_status;
        const hasError = ((f.download_status === "failed" || f.download_status === "conflict" || f.download_status === "unknown") && f.error_message)
            || (procStatus === "failed" && f.processing_error);
        const isConflict = f.download_status === "conflict";
        const errorMsg = (procStatus === "failed" && f.processing_error) ? f.processing_error : f.error_message;
        const pctStyle = getStatusPct(f, statusClass);
        html += `<td class="col-status">` +
            `<span class="file-status ${statusClass}"${pctStyle}${hasError ? ` title="${escapeHtml(errorMsg)}"` : ""}>${displayStatus}</span>` +
            (hasError && isConflict
                ? `<span class="file-error-hint clickable" data-conflict-file='${JSON.stringify({id: f.id, name: f.name, size: f.size, error: f.error_message})}' title="Click to resolve conflict">&#9432;</span>`
                : hasError ? `<span class="file-error-hint" title="${escapeHtml(f.error_message)}">&#9432;</span>` : "") +
            (f.download_status === "failed" ? `<button class="retry-file-btn" data-retry-file="${f.id}" title="Retry this file">&#x21bb;</button>` : "") +
            `</td>`;

        if (isPriority) {
            if (isQueued && !isUnknown) {
                const selIdx = queuedFiles.indexOf(f);
                html += buildPriorityCell(f.id, selIdx === 0, selIdx === lastQueuedIdx);
            } else {
                html += '<td class="col-priority"></td>';
            }
        }

        tr.innerHTML = html;

        // --- Event listeners ---

        // Desktop-style selection: click, shift+click, ctrl+click
        if (selectedFileIds.has(f.id)) tr.classList.add("selected");
        tr.addEventListener("click", (e) => {
            if (e.target.closest("button, .file-error-hint, .file-actions, .queue-toggle, .unknown-grip")) return;
            handleFileSelect(f, e);
        });

        const queueBtn = tr.querySelector(".queue-toggle");
        if (queueBtn) {
            queueBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                const fid = parseInt(queueBtn.dataset.queueId);
                const adding = queueBtn.classList.contains("queue-add");
                api("POST", `/api/files/${fid}/queue`, { queued: adding }).then(() => { loadFiles(); refreshQueueCount(); });
            });
        }
        const retryBtn = tr.querySelector(".retry-file-btn");
        if (retryBtn) retryBtn.addEventListener("click", () => retryFile(f.id));
        const conflictHint = tr.querySelector(".file-error-hint.clickable");
        if (conflictHint) {
            conflictHint.addEventListener("click", () => {
                const info = JSON.parse(conflictHint.dataset.conflictFile);
                openForceResume(info);
            });
        }
        tr.querySelectorAll(".file-action-btn").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const action = btn.dataset.action;
                const fid = parseInt(btn.dataset.fileId);
                if (action === "rename") startInlineRename(tr, fid, btn.dataset.fileName);
                else if (action === "process") { pendingBatchProcessIds = [fid]; openProcessArchiveModal(); }
                else if (action === "rescan") rescanFile(fid);
                else if (action === "delete") confirmDeleteFile(fid, btn.dataset.fileName, btn.dataset.fileOrigin);
            });
        });
        if (isPriority && isQueued && !isUnknown) {
            attachPriorityDrag(tr, f.id);
            const upBtn = tr.querySelector(`[data-move-up="${f.id}"]`);
            const downBtn = tr.querySelector(`[data-move-down="${f.id}"]`);
            if (upBtn) upBtn.addEventListener("click", () => moveFile(f.id, -1));
            if (downBtn) downBtn.addEventListener("click", () => moveFile(f.id, 1));
        }
        if (procStatus === "processed") {
            tr.classList.add("processed-expandable");
        }
        // Processed files: single click on chevron/toggle expands processed tree
        if (hasProcessedFolder) {
            const toggleEl = tr.querySelector(".folder-toggle");
            if (toggleEl) {
                toggleEl.style.cursor = "pointer";
                toggleEl.addEventListener("click", async (e) => {
                    e.stopPropagation();
                    if (desc.processedExpanded) {
                        vsExpandedFolders.delete(desc.processedPath);
                    } else {
                        vsExpandedFolders.add(desc.processedPath);
                        // Lazy-load processed tree
                        if (!vsProcessedCache[f.id]) {
                            try {
                                const data = await api("GET", `/api/files/${f.id}/processed-tree`);
                                vsProcessedCache[f.id] = data.tree || [];
                            } catch (err) {
                                vsProcessedCache[f.id] = [];
                            }
                        }
                    }
                    vsInvalidateDescriptors();
                    vsRenderVisible();
                });
            }
        }
        // All manifest files can be expanded to show tags (and processed output if applicable)
        if (f.origin === "manifest" || procStatus === "processed") {
            tr.addEventListener("dblclick", (e) => {
                if (e.target.closest("button, .file-error-hint, .file-actions, .queue-toggle, .folder-toggle")) return;
                toggleFileDetail(tr, f, isPriority);
            });
        }

        // Unknown files: draggable source for assign-as-output
        if (isUnknown) {
            attachUnknownDrag(tr, f.id);
        }
        // Completed, processed, and skipped files: drop targets for unknown files
        const canReceiveOutput = f.download_status === "downloaded" || hasProcessedOutput || isSkipped;
        if (canReceiveOutput && !isUnknown) {
            attachOutputDropTarget(tr, f.id);
        }

        return tr;
    }

    // Render only the visible slice of rows in the virtual-scrolled file table.
    function vsRenderVisible() {
        // Don't rebuild rows during drag — it destroys drop targets
        if (unknownDragActive || fileDragSrcId !== null) return;
        const wrap = $(".file-table-wrap");
        if (!wrap || vsFiles.length === 0) return;

        const isPriority = currentSort === "priority";
        const queuedFiles = isPriority ? vsFiles.filter(f => f.queue_position != null && f.download_status !== "unknown") : [];
        const lastQueuedIdx = queuedFiles.length - 1;
        const descriptors = vsGetRowDescriptors(vsFiles, isPriority);
        const totalRows = descriptors.length;
        const totalHeight = totalRows * VS_ROW_HEIGHT;

        const scrollTop = wrap.scrollTop;
        const viewHeight = wrap.clientHeight;

        let startRow = Math.floor(scrollTop / VS_ROW_HEIGHT) - VS_OVERSCAN;
        let endRow = Math.ceil((scrollTop + viewHeight) / VS_ROW_HEIGHT) + VS_OVERSCAN;
        startRow = Math.max(0, startRow);
        endRow = Math.min(totalRows - 1, endRow);

        // Skip re-render if the range hasn't changed
        if (vsLastRange && vsLastRange.start === startRow && vsLastRange.end === endRow) return;
        vsLastRange = { start: startRow, end: endRow };

        // Clear tbody and add spacer + visible rows + spacer
        const savedScroll = wrap.scrollTop;
        fileListEl.innerHTML = "";

        // Top spacer
        if (startRow > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="${getColspan()}" style="height:${startRow * VS_ROW_HEIGHT}px;padding:0;border:none"></td>`;
            fileListEl.appendChild(spacer);
        }

        // Visible rows
        for (let i = startRow; i <= endRow; i++) {
            const desc = descriptors[i];
            if (desc.type === "divider") {
                const divTr = document.createElement("tr");
                divTr.className = "queue-divider-row";
                divTr.innerHTML = `<td colspan="${getColspan()}"><div class="queue-divider"><span>Not queued</span></div></td>`;
                fileListEl.appendChild(divTr);
            } else if (desc.type === "folder") {
                fileListEl.appendChild(buildFolderRow(desc, isPriority));
            } else if (desc.type === "pfile") {
                fileListEl.appendChild(buildProcessedFileRow(desc));
            } else {
                const tr = buildFileRow(desc.file, isPriority, queuedFiles, lastQueuedIdx, desc.depth, desc);
                fileListEl.appendChild(tr);
            }
        }

        // Bottom spacer
        const bottomSpace = (totalRows - endRow - 1) * VS_ROW_HEIGHT;
        if (bottomSpace > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="${getColspan()}" style="height:${bottomSpace}px;padding:0;border:none"></td>`;
            fileListEl.appendChild(spacer);
        }

        // Restore scroll position (clearing innerHTML can reset it)
        wrap.scrollTop = savedScroll;

        applyTruncationTooltips(fileListEl);
    }

    function renderFiles(data) {
        const { files, total, all_queued } = data;
        const isPriority = currentSort === "priority";

        rebuildTableHeader();
        vsLastRange = null;

        updateBatchActions();

        if (files.length === 0) {
            fileListEl.innerHTML = `<tr><td colspan="${getColspan()}" style="text-align:center;padding:20px;color:var(--text-muted)">No files found.</td></tr>`;
            return;
        }

        // Show/hide clear highlights button
        const hasChanges = files.some(f => !!f.change_status);
        $("#btn-clear-changes").style.display = hasChanges ? "" : "none";

        // File count indicator
        const countEl = $("#vs-file-count");
        if (countEl) countEl.textContent = `${total} file${total !== 1 ? "s" : ""}`;

        // Initial virtual scroll render
        vsRenderVisible();
    }

    /**
     * Return a style attribute string with --pct for progress-bar capsules,
     * or empty string if no progress applies.
     */
    function getStatusPct(f, statusClass) {
        if ((statusClass === "downloading" || statusClass === "partial") && f.size > 0) {
            const pct = Math.min(100, (f.downloaded_bytes || 0) / f.size * 100);
            return ` style="--pct:${pct.toFixed(1)}%"`;
        }
        if (statusClass === "proc-active" && f.processing_total > 0) {
            const pct = Math.min(100, (f.processing_current || 0) / f.processing_total * 100);
            return ` style="--pct:${pct.toFixed(1)}%"`;
        }
        return "";
    }

    function formatFileStatus(f) {
        // Show processing queue status first — it takes priority over download status
        const pqs = f.process_queue_status || "";
        if (pqs === "processing") return "processing...";
        if (pqs === "queued") return "proc. queued";
        if (pqs === "failed") return "proc. failed";
        if (pqs === "skipped") return "proc. skipped";
        // Check overlay for processed state
        if (f.has_processed) return "processed";
        if (f.queue_position == null && f.download_status === "pending") {
            if (f.downloaded_bytes > 0 && f.size > 0) {
                const pct = ((f.downloaded_bytes / f.size) * 100).toFixed(1);
                return `${pct}%`;
            }
            return "skipped";
        }
        if (f.download_status === "scan_pending") return "scanning…";
        if (f.download_status === "downloading" && f.size > 0) {
            const pct = ((f.downloaded_bytes / f.size) * 100).toFixed(1);
            return `${pct}%`;
        }
        if (f.download_status === "pending" && f.downloaded_bytes > 0 && f.size > 0) {
            const pct = ((f.downloaded_bytes / f.size) * 100).toFixed(1);
            return `${pct}%`;
        }
        return f.download_status;
    }

    function buildFileTagsHtml(fileId, ownTags, inheritedTags) {
        let html = `<div class="file-detail-tags" data-file-id="${fileId}">`;
        html += `<div class="file-detail-tags-label">Tags</div>`;
        html += `<div class="file-detail-tags-container">`;
        // Own tags (auto without ×, user with ×)
        for (const t of ownTags) {
            const isAuto = t.auto;
            const cls = "tag-chip" + (isAuto ? " tag-auto" : "");
            html += `<span class="${cls}">${formatTagHtml(t.tag)}`;
            if (!isAuto) {
                html += ` <button class="tag-remove file-tag-remove" data-file-id="${fileId}" data-tag="${esc(t.tag)}">&times;</button>`;
            }
            html += `</span>`;
        }
        // Inherited tags
        for (const t of inheritedTags) {
            html += `<span class="tag-chip tag-inherited" title="Inherited from archive">${formatTagHtml(t.tag)}</span>`;
        }
        html += `<input type="text" class="file-tag-input" data-file-id="${fileId}" placeholder="Add tag…">`;
        html += `</div></div>`;
        return html;
    }

    async function toggleFileDetail(tr, f, isPriority) {
        // If already expanded, collapse
        const existing = tr.nextElementSibling;
        if (existing && existing.classList.contains("file-detail-row")) {
            existing.remove();
            tr.classList.remove("expanded");
            return;
        }

        const colCount = getColspan();
        const detailTr = document.createElement("tr");
        detailTr.classList.add("file-detail-row");

        // Fetch file tags
        let ownTags = [], inheritedTags = [];
        try {
            const tagData = await api("GET", `/api/files/${f.id}/tags`);
            ownTags = tagData.own || [];
            inheritedTags = tagData.inherited || [];
        } catch (e) { /* silent */ }

        // Tags section only — processed output is shown inline via expandable file row
        const cellHtml = buildFileTagsHtml(f.id, ownTags, inheritedTags);

        detailTr.innerHTML = `<td colspan="${colCount}" class="file-detail-cell">${cellHtml}</td>`;

        // File tag remove handlers
        detailTr.querySelectorAll(".file-tag-remove").forEach((btn) => {
            btn.addEventListener("click", async (e) => {
                e.stopPropagation();
                const fileId = btn.dataset.fileId;
                const tag = btn.dataset.tag;
                await api("DELETE", `/api/files/${fileId}/tags/${encodeURIComponent(tag)}`);
                refreshFileTagsInRow(detailTr, fileId);
            });
        });

        // File tag input handler
        const tagInput = detailTr.querySelector(".file-tag-input");
        if (tagInput) {
            tagInput.addEventListener("keydown", async (e) => {
                if (e.key === "Enter") {
                    const val = tagInput.value.trim();
                    if (!val) return;
                    const fileId = tagInput.dataset.fileId;
                    await api("POST", `/api/files/${fileId}/tags`, { tag: val });
                    tagInput.value = "";
                    refreshFileTagsInRow(detailTr, fileId);
                }
            });
        }

        tr.after(detailTr);
        tr.classList.add("expanded");
        applyTruncationTooltips(detailTr);
    }

    async function refreshFileTagsInRow(detailTr, fileId) {
        try {
            const tagData = await api("GET", `/api/files/${fileId}/tags`);
            const container = detailTr.querySelector(`.file-detail-tags[data-file-id="${fileId}"]`);
            if (!container) return;
            container.outerHTML = buildFileTagsHtml(fileId, tagData.own || [], tagData.inherited || []);
            // Re-attach handlers on the new DOM
            const newContainer = detailTr.querySelector(`.file-detail-tags[data-file-id="${fileId}"]`);
            newContainer.querySelectorAll(".file-tag-remove").forEach((btn) => {
                btn.addEventListener("click", async (e) => {
                    e.stopPropagation();
                    await api("DELETE", `/api/files/${btn.dataset.fileId}/tags/${encodeURIComponent(btn.dataset.tag)}`);
                    refreshFileTagsInRow(detailTr, fileId);
                });
            });
            const inp = newContainer.querySelector(".file-tag-input");
            if (inp) {
                inp.addEventListener("keydown", async (e) => {
                    if (e.key === "Enter") {
                        const val = inp.value.trim();
                        if (!val) return;
                        await api("POST", `/api/files/${inp.dataset.fileId}/tags`, { tag: val });
                        inp.value = "";
                        refreshFileTagsInRow(detailTr, fileId);
                    }
                });
            }
        } catch (e) { /* silent */ }
    }

    // Track active rename so only one edit box is open at a time
    let activeRenameCancel = null;

    function cancelActiveRename() {
        if (activeRenameCancel) {
            const fn = activeRenameCancel;
            activeRenameCancel = null;
            fn();
        }
    }

    function startProcessedRename(li, fileId, path, currentName) {
        cancelActiveRename();
        const row = li.querySelector(".ptree-row");
        // Save original child nodes (with their event listeners intact)
        const origChildren = Array.from(row.childNodes).map(n => n.cloneNode ? n : n);
        origChildren.forEach(n => row.removeChild(n));

        row.innerHTML = `<div class="inline-rename">` +
            `<input type="text" class="rename-input" value="${escapeHtml(currentName)}">` +
            `<button class="rename-confirm" title="Confirm"><svg viewBox="0 0 16 16" width="14" height="14"><path d="M3 8l3.5 3.5L13 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>` +
            `<button class="rename-cancel" title="Cancel"><svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>` +
            `</div>`;
        const input = row.querySelector(".rename-input");
        input.focus();
        input.select();

        const restore = () => { row.innerHTML = ""; origChildren.forEach(n => row.appendChild(n)); };

        const doRename = async () => {
            activeRenameCancel = null;
            const newName = input.value.trim();
            if (newName && newName !== currentName) {
                try {
                    await api("POST", `/api/files/${fileId}/rename-processed`, { old_path: path, new_name: newName });
                    loadFiles();
                } catch (e) {
                    addNotification("Rename failed: " + e.message, "error");
                    restore();
                }
            } else {
                restore();
            }
        };
        const doCancel = () => { activeRenameCancel = null; restore(); };
        activeRenameCancel = doCancel;

        row.querySelector(".rename-confirm").addEventListener("click", (e) => { e.stopPropagation(); doRename(); });
        row.querySelector(".rename-cancel").addEventListener("click", (e) => { e.stopPropagation(); doCancel(); });
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); doRename(); }
            else if (e.key === "Escape") { e.preventDefault(); doCancel(); }
        });
        input.addEventListener("click", (e) => e.stopPropagation());
    }

    // --- Inline Rename ---

    function startInlineRename(tr, fileId, currentName) {
        cancelActiveRename();
        const nameCell = tr.querySelector(".col-name");
        const wrap = nameCell.querySelector(".file-name-wrap");
        // Save original child nodes (with their event listeners intact)
        const origChildren = Array.from(wrap.childNodes);
        origChildren.forEach(n => wrap.removeChild(n));

        wrap.innerHTML = `<div class="inline-rename">` +
            `<input type="text" class="rename-input" value="${escapeHtml(currentName)}">` +
            `<button class="rename-confirm" title="Confirm">` +
            `<svg viewBox="0 0 16 16" width="14" height="14"><path d="M3 8l3.5 3.5L13 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>` +
            `<button class="rename-cancel" title="Cancel">` +
            `<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>` +
            `</div>`;
        const input = wrap.querySelector(".rename-input");
        input.focus();
        input.select();

        const restore = () => { wrap.innerHTML = ""; origChildren.forEach(n => wrap.appendChild(n)); };

        const doRename = async () => {
            activeRenameCancel = null;
            const newName = input.value.trim();
            if (newName && newName !== currentName) {
                try {
                    await api("POST", `/api/files/${fileId}/rename`, { name: newName });
                    loadFiles();
                } catch (e) {
                    addNotification("Rename failed: " + e.message, "error");
                    restore();
                }
            } else {
                restore();
            }
        };
        const doCancel = () => { activeRenameCancel = null; restore(); };
        activeRenameCancel = doCancel;

        wrap.querySelector(".rename-confirm").addEventListener("click", (e) => { e.stopPropagation(); doRename(); });
        wrap.querySelector(".rename-cancel").addEventListener("click", (e) => { e.stopPropagation(); doCancel(); });
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); doRename(); }
            else if (e.key === "Escape") { e.preventDefault(); doCancel(); }
        });
        input.addEventListener("click", (e) => e.stopPropagation());
    }

    // --- Re-scan single file ---

    async function rescanFile(fileId) {
        try {
            const result = await api("POST", `/api/files/${fileId}/scan`);
            const name = result.name || "File";
            const messages = {
                completed: `"${name}" found on disk and verified`,
                missing:   `"${name}" not found on disk`,
                partial:   `"${name}" is incomplete — partial download detected`,
                conflict:  `"${name}" found on disk but has a mismatch (size or checksum)`,
            };
            const types = { completed: "success", missing: "warning", partial: "warning", conflict: "warning" };
            addNotification(messages[result.status] || `Scan result: ${result.status}`, types[result.status] || "info");
            loadFiles();
        } catch (e) {
            addNotification("File scan failed: " + e.message, "error");
        }
    }

    // --- Delete single file ---

    function confirmDeleteFile(fileId, fileName, origin) {
        const removeFromDb = (origin === "scan" || origin === "unknown");
        const msg = removeFromDb
            ? `Delete &ldquo;${escapeHtml(fileName)}&rdquo;?<br><br>This will remove the file from disk and from the archive list.`
            : `Delete &ldquo;${escapeHtml(fileName)}&rdquo; from disk?<br><br>The file will be removed from disk but kept in the archive list.`;
        confirmAction("confirm_delete_file", "Delete File", msg, () => {
            api("POST", `/api/files/${fileId}/delete`, { remove_from_db: removeFromDb }).then((r) => {
                if (removeFromDb) {
                    addNotification(`Deleted "${fileName}"`, "info");
                } else if (r.deleted_from_disk) {
                    addNotification(`Deleted "${fileName}" from disk`, "info");
                } else {
                    addNotification(`"${fileName}" was not found on disk`, "warning");
                }
                loadFiles();
                refreshArchives();
            }).catch((e) => addNotification("Delete failed: " + e.message, "error"));
        }, { confirmText: "Delete" });
    }

    // --- Batch actions ---

    async function batchQueueFiles() {
        if (!currentArchiveId || selectedFileIds.size === 0) return;
        const allQueued = [...selectedFileIds].every(id => {
            const f = vsFiles.find(f => f.id === id);
            return f && f.queue_position != null;
        });
        const queued = !allQueued; // toggle: if all queued → unqueue, otherwise queue
        const ids = Array.from(selectedFileIds);
        let done = 0;
        for (const fid of ids) {
            try { await api("POST", `/api/files/${fid}/queue`, { queued }); done++; } catch (e) {}
        }
        addNotification(`${queued ? "Queued" : "Unqueued"} ${done}/${ids.length} files`, "info");
        selectedFileIds.clear();
        loadFiles();
        refreshArchives();
        refreshQueueCount();
    }

    async function batchScanFiles() {
        if (!currentArchiveId || selectedFileIds.size === 0) return;
        const ids = Array.from(selectedFileIds);
        let done = 0;
        for (const fid of ids) {
            try { await api("POST", `/api/files/${fid}/scan`); done++; } catch (e) {}
        }
        addNotification(`Scanned ${done}/${ids.length} files`, "info");
        selectedFileIds.clear();
        loadFiles();
    }

    async function batchAutoTagFiles() {
        if (selectedFileIds.size === 0) return;
        const ids = Array.from(selectedFileIds);
        try {
            const result = await api("POST", "/api/files/auto-tag", { file_ids: ids });
            addNotification(`Scanned tags for ${result.tagged} file(s)`, "info");
        } catch (e) {
            addNotification("Tag scan failed: " + e.message, "error");
        }
        selectedFileIds.clear();
        updateBatchActions();
        updateFileSelectionClasses();
        if (currentArchiveId) loadArchiveTagsAndCollections(currentArchiveId);
    }

    async function batchProcessFiles() {
        if (!currentArchiveId || selectedFileIds.size === 0) return;
        // Open the process modal with file_ids pre-set
        pendingBatchProcessIds = Array.from(selectedFileIds);
        openProcessArchiveModal();
    }

    let pendingBatchProcessIds = null;

    async function batchRetryFiles() {
        if (!currentArchiveId || selectedFileIds.size === 0) return;
        const ids = Array.from(selectedFileIds);
        try {
            const result = await api("POST", `/api/archives/${currentArchiveId}/files/batch-retry`, { file_ids: ids });
            addNotification(`Retried ${result.reset_count} failed files`, "info");
            selectedFileIds.clear();
            loadFiles();
            refreshArchives();
        } catch (e) {
            addNotification("Retry failed: " + e.message, "error");
        }
    }

    async function batchDeleteFiles() {
        if (!currentArchiveId || selectedFileIds.size === 0) return;
        const count = selectedFileIds.size;
        confirmAction(
            "confirm_batch_delete_files",
            "Delete Files",
            `Delete <strong>${count}</strong> selected file(s)?<br><br>This will remove them from the archive list and delete them from disk if present.`,
            async () => {
                const ids = Array.from(selectedFileIds);
                try {
                    const result = await api("POST", `/api/archives/${currentArchiveId}/files/batch-delete`, { file_ids: ids });
                    addNotification(`Deleted ${result.deleted} files`, "info");
                    selectedFileIds.clear();
                    loadFiles();
                    refreshArchives();
                } catch (e) {
                    addNotification("Batch delete failed: " + e.message, "error");
                }
            },
            { confirmText: "Delete Files" }
        );
    }

    async function batchSetMediaRoot(files) {
        // Determine a common parent directory, or use the first file's directory
        const dirs = new Set(files.map(f => {
            const parts = f.name.split("/");
            return parts.length > 1 ? parts.slice(0, -1).join("/") : "";
        }));
        // Use the common directory if there is exactly one; otherwise use the first file's stem
        let mediaRoot;
        if (dirs.size === 1 && [...dirs][0]) {
            mediaRoot = [...dirs][0];
        } else {
            // Use the basename of the first file (without extension) as the unit name
            const first = files[0].name;
            const base = first.split("/").pop();
            const stem = base.replace(/\.[^.]+$/, "");
            mediaRoot = stem;
        }
        try {
            await api("POST", "/api/files/media-root", {
                file_ids: files.map(f => f.id),
                media_root: mediaRoot,
            });
            // Update in-memory
            for (const f of files) {
                const vsf = vsFiles.find(v => v.id === f.id);
                if (vsf) vsf.media_root = mediaRoot;
            }
            addNotification(`Grouped ${files.length} files as media unit "${mediaRoot}"`, "info");
        } catch (e) {
            addNotification("Failed to set media root: " + e.message, "error");
        }
    }

    async function batchClearMediaRoot() {
        const ids = Array.from(selectedFileIds);
        try {
            await api("POST", "/api/files/media-root", { file_ids: ids, media_root: "" });
            for (const id of ids) {
                const vsf = vsFiles.find(v => v.id === id);
                if (vsf) vsf.media_root = "";
            }
            addNotification(`Split ${ids.length} file(s) from media unit`, "info");
        } catch (e) {
            addNotification("Failed to clear media root: " + e.message, "error");
        }
    }

    function updateFileRow(fileId, updates) {
        // Update in-memory data so virtual scroll re-renders stay current
        const vsFile = vsFiles.find(f => f.id === fileId);
        if (vsFile) {
            if (updates.download_status) vsFile.download_status = updates.download_status;
            if (updates.downloaded_bytes !== undefined) vsFile.downloaded_bytes = updates.downloaded_bytes;
            if (updates.size !== undefined) vsFile.size = updates.size;
            if (updates.downloaded !== undefined) vsFile.downloaded = updates.downloaded;
            if (updates.queue_position !== undefined) vsFile.queue_position = updates.queue_position;
        }
        // Update the visible DOM row if present
        const tr = fileListEl.querySelector(`tr[data-file-id="${fileId}"]`);
        if (!tr) return;
        const statusCell = tr.querySelector(".file-status");
        if (statusCell && updates.download_status) {
            const f = vsFile || updates;
            const ds = formatFileStatus(f);
            const isQueued = f.queue_position != null;
            const isSkipped = !isQueued && f.download_status === "pending";
            const isPartial = isSkipped && (f.downloaded_bytes || 0) > 0 && (f.size || 0) > 0;
            const cls = isPartial ? "partial"
                : isSkipped ? "skipped"
                : f.download_status;
            statusCell.className = "file-status " + cls;
            statusCell.textContent = ds;
            if ((cls === "downloading" || cls === "partial") && (f.size || 0) > 0) {
                const pct = Math.min(100, (f.downloaded_bytes || 0) / f.size * 100);
                statusCell.style.setProperty("--pct", pct.toFixed(1) + "%");
            } else {
                statusCell.style.removeProperty("--pct");
            }
        }
    }

    // --- Add Archive ---

    function openAddModal() {
        $("#modal-add").classList.add("open");
        const input = $("#input-add-url");
        const batch = $("#input-add-batch");
        const batchCheck = $("#add-batch-mode");
        input.value = "";
        batch.value = "";
        batchCheck.checked = false;
        toggleBatchMode(false);
        input.focus();
        $("#add-error").textContent = "";
        $("#add-loading").style.display = "none";
        $("#btn-add-confirm").disabled = false;
        // Apply defaults from settings
        api("GET", "/api/settings").then((s) => {
            $("#add-enable-archive").checked = s.default_enable_archive === "1";
            $("#add-select-all-files").checked = s.default_select_all !== "0";
        }).catch(() => {});
        // Populate group dropdown
        const sel = $("#add-group-select");
        sel.innerHTML = '<option value="">None</option>';
        groups.forEach((g) => {
            const opt = document.createElement("option");
            opt.value = g.id;
            opt.textContent = g.name;
            sel.appendChild(opt);
        });
    }

    function toggleBatchMode(on) {
        $("#input-add-url").style.display = on ? "none" : "";
        $("#input-add-batch").style.display = on ? "" : "none";
        $("#add-prompt-single").style.display = on ? "none" : "";
        $("#add-prompt-batch").style.display = on ? "" : "none";
        if (on) {
            $("#input-add-batch").focus();
        } else {
            $("#input-add-url").focus();
        }
    }

    function closeAddModal() {
        $("#modal-add").classList.remove("open");
    }

    function addArchive() {
        const isBatch = $("#add-batch-mode").checked;
        const enable = $("#add-enable-archive").checked;
        const selectAll = $("#add-select-all-files").checked;
        const groupVal = $("#add-group-select").value;
        const groupId = groupVal ? parseInt(groupVal) : null;

        if (isBatch) {
            const lines = $("#input-add-batch").value.split("\n").map((l) => l.trim()).filter((l) => l);
            if (lines.length === 0) {
                $("#add-error").textContent = "Please enter at least one URL or identifier.";
                return;
            }
            closeAddModal();
            addArchiveBatch(lines, enable, selectAll, groupId);
        } else {
            const url = $("#input-add-url").value.trim();
            if (!url) {
                $("#add-error").textContent = "Please enter a URL or identifier.";
                return;
            }
            closeAddModal();
            addArchiveSingle(url, enable, selectAll, groupId);
        }
    }

    async function addArchiveSingle(url, enable, selectAll, groupId) {
        // Create a server-side notification for the adding operation
        const label = url.length > 40 ? url.substring(0, 37) + "..." : url;
        let serverNotifId = null;
        try {
            const nRes = await api("POST", "/api/notifications", {
                message: `Adding "${label}": fetching metadata...`,
                type: "info",
            });
            serverNotifId = nRes.id;
        } catch (_) {
            const nid = "local-" + (++notifIdCounter);
            notifications.unshift({ id: nid, message: `Adding "${label}": fetching metadata...`, type: "info", created_at: Date.now() / 1000 });
            serverNotifId = nid;
            renderNotifBadge();
            renderNotifList();
        }

        try {
            const result = await api("POST", "/api/archives", { url, enable, select_all: selectAll, group_id: groupId });
            // Remove progress notification and add success
            if (typeof serverNotifId === "number") api("DELETE", "/api/notifications/" + serverNotifId).catch(() => {});
            notifications = notifications.filter(n => n.id !== serverNotifId);
            const title = result.title || result.identifier || url;
            addNotification(`Added "${title}"`, "success");
            refreshArchives();
        } catch (e) {
            // Remove progress notification and add error
            if (typeof serverNotifId === "number") api("DELETE", "/api/notifications/" + serverNotifId).catch(() => {});
            notifications = notifications.filter(n => n.id !== serverNotifId);
            addNotification(`Failed to add "${label}": ${e.message}`, "error");
        }
    }

    async function addArchiveBatch(lines, enable, selectAll, groupId) {
        const total = lines.length;
        // Create server-side notification
        let serverNotifId = null;
        try {
            const nRes = await api("POST", "/api/notifications", {
                message: `Batch add: 0/${total}`,
                type: "info",
            });
            serverNotifId = nRes.id;
        } catch (_) {
            const nid = "local-" + (++notifIdCounter);
            notifications.unshift({ id: nid, message: `Batch add: 0/${total}`, type: "info", created_at: Date.now() / 1000 });
            serverNotifId = nid;
            renderNotifBadge();
            renderNotifList();
        }

        let succeeded = 0;
        let failed = 0;
        const errors = [];

        for (let i = 0; i < lines.length; i++) {
            try {
                await api("POST", "/api/archives", { url: lines[i], enable, select_all: selectAll, group_id: groupId });
                succeeded++;
            } catch (e) {
                failed++;
                errors.push(`${lines[i]}: ${e.message}`);
            }
            // Update progress notification on server
            const done = i + 1;
            const pct = Math.round((done / total) * 100);
            if (typeof serverNotifId === "number") {
                api("PATCH", "/api/notifications/" + serverNotifId, { message: `Batch add: ${done}/${total}` }).catch(() => {});
            }
            const active = notifications.find(n => n.id === serverNotifId);
            if (active) {
                active.message = `Batch add: ${done}/${total}`;
                renderNotifList();
            }
        }

        // Remove progress notification and add final result
        if (typeof serverNotifId === "number") api("DELETE", "/api/notifications/" + serverNotifId).catch(() => {});
        notifications = notifications.filter(n => n.id !== serverNotifId);
        if (failed === 0) {
            addNotification(`Batch add: ${succeeded} archive${succeeded !== 1 ? "s" : ""} added`, "success");
        } else {
            addNotification(`Batch add: ${succeeded} added, ${failed} failed`, "warning");
            errors.forEach(err => addNotification(err, "error"));
        }
        refreshArchives();
    }

    // --- Delete Archive ---

    let deleteTarget = null;

    function confirmDelete(archive) {
        deleteTarget = archive;
        $("#delete-name").textContent = archive.title || archive.identifier;
        $("#modal-delete").classList.add("open");
    }

    async function doDelete() {
        if (deleteTarget) {
            await api("DELETE", `/api/archives/${deleteTarget.id}`);
            deleteTarget = null;
            $("#modal-delete").classList.remove("open");
            await refreshArchives();
        }
    }

    // --- Settings (Full-Screen Page) ---

    let scheduleRules = [];
    let settingsSnapshot = "";

    function getSettingsFingerprint() {
        return JSON.stringify({
            ia_email: $("#set-ia-email").value,
            ia_password: $("#set-ia-password").value,
            download_dir: $("#set-download-dir").value,
            processed_dir: $("#set-processed-dir").value,
            max_retries: $("#set-max-retries").value,
            retry_delay: $("#set-retry-delay").value,
            sse_update_rate: $("#set-sse-update-rate").value,
            max_connections_per_node: $("#set-max-connections-per-node").value,
            max_connections_total: $("#set-max-connections-total").value,
            theme: $("#set-theme").value,
            use_http: $("#set-use-http").checked,
            ...Object.fromEntries(Object.keys(CONFIRM_KEYS).map(k => [k, $(`#set-${k.replace(/_/g, "-")}`).checked])),
            default_enable_archive: $("#set-default-enable-archive").checked,
            default_select_all: $("#set-default-select-all").checked,
            schedule: JSON.stringify(collectScheduleRules()),
            processing_temp_dir: $("#set-processing-temp-dir").value,
            debug_enabled: $("#set-debug-enabled").checked,
            debug_log_file: $("#set-debug-log-file").value,
        });
    }

    function checkSettingsDirty() {
        const dirty = getSettingsFingerprint() !== settingsSnapshot;
        $("#btn-settings-save-bottom").disabled = !dirty;
    }

    async function openSettings() {
        try {
            const s = await api("GET", "/api/settings");
            $("#set-ia-email").value = s.ia_email || "";
            $("#set-ia-password").value = "";
            $("#set-ia-pw-hint").textContent = s.ia_password_set ? "(password is set; leave blank to keep)" : "";
            $("#ia-test-result").textContent = "";
            $("#set-download-dir").value = s.download_dir || "";
            $("#set-processed-dir").value = s.processed_dir || "";
            $("#set-max-retries").value = s.max_retries || "3";
            $("#set-retry-delay").value = s.retry_delay || "5";
            $("#set-sse-update-rate").value = s.sse_update_rate || "500";
            $("#set-max-connections-per-node").value = s.max_connections_per_node || "2";
            $("#set-max-connections-total").value = s.max_connections_total || "6";
            $("#set-theme").value = s.theme || "dark";
            $("#set-use-http").checked = s.use_http === "1";
            $("#http-warning").style.display = s.use_http === "1" ? "block" : "none";
            // Confirmation warning checkboxes
            for (const key of Object.keys(CONFIRM_KEYS)) {
                const el = $(`#set-${key.replace(/_/g, "-")}`);
                if (el) el.checked = s[key] !== "0";
            }
            $("#set-default-enable-archive").checked = s.default_enable_archive === "1";
            $("#set-default-select-all").checked = s.default_select_all !== "0";
            $("#set-old-password").value = "";
            $("#set-new-password").value = "";
            $("#pw-change-error").textContent = "";
            $("#set-processing-temp-dir").value = s.processing_temp_dir || "";
            // Debug settings
            $("#set-debug-enabled").checked = s.debug_enabled === "1";
            $("#set-debug-log-file").value = s.debug_log_file || "";
            // Load schedule rules
            scheduleRules = JSON.parse(s.speed_schedule || "[]");
            renderScheduleRules();
            // Snapshot for dirty tracking
            settingsSnapshot = getSettingsFingerprint();
            $("#btn-settings-save-bottom").disabled = true;
            // Track current page before switching to settings
            const activePage = document.querySelector(".page.active");
            pageBeforeSettings = activePage ? activePage.id : null;
            // Show settings page, hide others
            $$(".page").forEach((p) => p.classList.remove("active"));
            $("#page-settings").classList.add("active");
        } catch (e) {
            alert("Failed to load settings: " + e.message);
        }
    }

    let pageBeforeSettings = null;

    function closeSettings() {
        $("#page-settings").classList.remove("active");
        if (pageBeforeSettings) {
            $(`#${pageBeforeSettings}`).classList.add("active");
            pageBeforeSettings = null;
        } else if (currentCollectionId) {
            $("#page-collection-detail").classList.add("active");
        } else if (currentArchiveId) {
            $("#page-detail").classList.add("active");
        } else {
            $("#page-home").classList.add("active");
        }
    }

    function switchTab(tabId) {
        $$(".settings-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tabId));
        $$(".settings-panel").forEach((p) => p.classList.toggle("active", p.id === tabId));
        if (tabId === "tab-processing") {
            detectAndShowTools();
            renderProfilesList();
        }
    }

    async function saveSettings() {
        const data = {
            ia_email: $("#set-ia-email").value,
            ia_password: $("#set-ia-password").value,
            download_dir: $("#set-download-dir").value,
            processed_dir: $("#set-processed-dir").value,
            max_retries: $("#set-max-retries").value,
            retry_delay: $("#set-retry-delay").value,
            sse_update_rate: $("#set-sse-update-rate").value,
            max_connections_per_node: $("#set-max-connections-per-node").value,
            max_connections_total: $("#set-max-connections-total").value,
            theme: $("#set-theme").value,
            use_http: $("#set-use-http").checked ? "1" : "0",
            ...Object.fromEntries(Object.keys(CONFIRM_KEYS).map(k => [k, $(`#set-${k.replace(/_/g, "-")}`).checked ? "1" : "0"])),
            default_enable_archive: $("#set-default-enable-archive").checked ? "1" : "0",
            default_select_all: $("#set-default-select-all").checked ? "1" : "0",
            speed_schedule: JSON.stringify(collectScheduleRules()),
            processing_temp_dir: $("#set-processing-temp-dir").value,
            debug_enabled: $("#set-debug-enabled").checked ? "1" : "0",
            debug_log_file: $("#set-debug-log-file").value,
        };
        try {
            await api("POST", "/api/settings", data);
            applyTheme(data.theme);
            // Sync runtime confirmation settings
            for (const key of Object.keys(CONFIRM_KEYS)) {
                confirmSettings[key] = data[key] === "1";
            }
            closeSettings();
        } catch (e) {
            alert("Failed to save settings: " + e.message);
        }
    }

    async function testCredentials() {
        const resultEl = $("#ia-test-result");
        resultEl.textContent = "Testing...";
        resultEl.className = "ia-test-result";
        try {
            const email = $("#set-ia-email").value;
            const pw = $("#set-ia-password").value;
            if (email || pw) {
                const saveData = { ia_email: email };
                if (pw) saveData.ia_password = pw;
                await api("POST", "/api/settings", saveData);
            }
            const res = await api("POST", "/api/settings/test-credentials");
            resultEl.textContent = res.message || "Success";
            resultEl.className = "ia-test-result " + (res.ok ? "success" : "error");
        } catch (e) {
            resultEl.textContent = e.message || "Test failed";
            resultEl.className = "ia-test-result error";
        }
    }

    // --- Change Password ---

    async function changePassword() {
        const oldPw = $("#set-old-password").value;
        const newPw = $("#set-new-password").value;
        const errEl = $("#pw-change-error");
        errEl.textContent = "";

        if (!oldPw || !newPw) {
            errEl.textContent = "Both fields are required";
            return;
        }
        if (newPw.length < 4) {
            errEl.textContent = "New password must be at least 4 characters";
            return;
        }

        try {
            await api("POST", "/api/auth/change-password", { old_password: oldPw, new_password: newPw });
            $("#set-old-password").value = "";
            $("#set-new-password").value = "";
            errEl.style.color = "var(--success)";
            errEl.textContent = "Password changed successfully";
            setTimeout(() => { errEl.textContent = ""; errEl.style.color = ""; }, 3000);
        } catch (e) {
            errEl.textContent = e.message;
        }
    }

    // --- Speed Schedule ---

    const DAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

    function renderScheduleRules() {
        const container = $("#schedule-rules");
        container.innerHTML = "";
        if (scheduleRules.length === 0) {
            container.innerHTML = '<div class="schedule-empty">No rules configured. Downloads run uncapped unless limited manually in the top bar.</div>';
            return;
        }
        scheduleRules.forEach((rule, idx) => {
            const div = document.createElement("div");
            div.className = "schedule-rule";
            div.dataset.idx = idx;

            const daysHtml = DAY_LABELS.map((d, di) => {
                const active = (rule.days || [0,1,2,3,4,5,6]).includes(di) ? "active" : "";
                return `<button type="button" class="day-toggle ${active}" data-day="${di}">${d}</button>`;
            }).join("");

            div.innerHTML = `
                <label>From <input type="time" class="rule-start" value="${rule.start || '00:00'}"></label>
                <label>To <input type="time" class="rule-end" value="${rule.end || '23:59'}"></label>
                <label><input type="number" class="rule-limit" min="0" step="100" value="${rule.limit_kbps || 0}"> <span class="rule-unit">KB/s</span></label>
                <div class="days-row">${daysHtml}</div>
                <button type="button" class="btn-remove-rule" title="Remove rule">
                    <svg viewBox="0 0 24 24" width="14" height="14"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
                </button>
            `;

            // Day toggles
            div.querySelectorAll(".day-toggle").forEach((btn) => {
                btn.addEventListener("click", () => { btn.classList.toggle("active"); checkSettingsDirty(); });
            });
            // Schedule inputs dirty tracking
            div.querySelectorAll("input").forEach((inp) => {
                inp.addEventListener("input", checkSettingsDirty);
                inp.addEventListener("change", checkSettingsDirty);
            });
            // Remove
            div.querySelector(".btn-remove-rule").addEventListener("click", () => {
                scheduleRules = collectScheduleRules();
                scheduleRules.splice(idx, 1);
                renderScheduleRules();
                checkSettingsDirty();
            });

            container.appendChild(div);
        });
    }

    function addScheduleRule() {
        scheduleRules = collectScheduleRules();
        scheduleRules.push({ start: "00:00", end: "23:59", limit_kbps: 0, days: [0,1,2,3,4,5,6] });
        renderScheduleRules();
        checkSettingsDirty();
    }

    function collectScheduleRules() {
        const rules = [];
        $$(".schedule-rule").forEach((div) => {
            const start = div.querySelector(".rule-start").value;
            const end = div.querySelector(".rule-end").value;
            const limit_kbps = parseInt(div.querySelector(".rule-limit").value) || 0;
            const days = [];
            div.querySelectorAll(".day-toggle.active").forEach((btn) => days.push(parseInt(btn.dataset.day)));
            rules.push({ start, end, limit_kbps, days });
        });
        return rules;
    }

    // --- Desktop-style File Selection ---

    let lastClickedFileIdx = null; // for shift+click range selection

    function handleFileSelect(f, e) {
        const idx = vsFiles.indexOf(f);
        if (e.shiftKey && lastClickedFileIdx !== null) {
            // Shift+click: range select
            const start = Math.min(lastClickedFileIdx, idx);
            const end = Math.max(lastClickedFileIdx, idx);
            if (!e.ctrlKey && !e.metaKey) selectedFileIds.clear();
            for (let i = start; i <= end; i++) {
                selectedFileIds.add(vsFiles[i].id);
            }
        } else if (e.ctrlKey || e.metaKey) {
            // Ctrl/Cmd+click: toggle individual
            if (selectedFileIds.has(f.id)) selectedFileIds.delete(f.id);
            else selectedFileIds.add(f.id);
            lastClickedFileIdx = idx;
        } else {
            // Plain click: select only this one
            selectedFileIds.clear();
            selectedFileIds.add(f.id);
            lastClickedFileIdx = idx;
        }
        syncSelectAll();
    }

    function selectAllFiles() {
        if (!currentArchiveId) return;
        vsFiles.forEach(f => selectedFileIds.add(f.id));
        updateBatchActions();
        updateFileSelectionClasses();
    }

    function deselectAllFiles() {
        selectedFileIds.clear();
        updateBatchActions();
        updateFileSelectionClasses();
    }

    // --- File List Context Menu ---
    fileListEl.addEventListener("contextmenu", (e) => {
        const tr = e.target.closest("tr[data-file-id]");
        if (!tr) return;
        e.preventDefault();
        const fid = parseInt(tr.dataset.fileId);

        // Modifier key handling: ctrl/shift behave as single-click before opening menu
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
            const f = vsFiles.find(f => f.id === fid);
            if (f) handleFileSelect(f, e);
        } else if (!selectedFileIds.has(fid)) {
            selectedFileIds.clear();
            selectedFileIds.add(fid);
            updateBatchActions();
            updateFileSelectionClasses();
        }

        const n = selectedFileIds.size;
        const allQueued = [...selectedFileIds].every(id => {
            const f = vsFiles.find(f => f.id === id);
            return f && f.queue_position != null;
        });

        // Media unit context items
        const selectedFiles = [...selectedFileIds].map(id => vsFiles.find(f => f.id === id)).filter(Boolean);
        const anyHasMediaRoot = selectedFiles.some(f => f.media_root);
        const allHaveMediaRoot = selectedFiles.length > 0 && selectedFiles.every(f => f.media_root);
        const mediaItems = [];
        if (n >= 2 && !allHaveMediaRoot) {
            mediaItems.push({ label: `Group as media unit (${n})`, action: () => batchSetMediaRoot(selectedFiles) });
        }
        if (anyHasMediaRoot) {
            mediaItems.push({ label: "Split media unit", action: () => batchClearMediaRoot() });
        }

        showContextMenu(e, [
            { label: allQueued ? `Unqueue (${n})` : `Queue (${n})`, action: batchQueueFiles },
            { label: "Scan", action: batchScanFiles },
            { label: "Scan for Tags", action: batchAutoTagFiles },
            { label: "Process", action: batchProcessFiles },
            { label: "Retry", action: batchRetryFiles },
            ...(mediaItems.length ? [{ separator: true }, ...mediaItems] : []),
            { separator: true },
            { label: `Delete (${n})`, action: batchDeleteFiles, danger: true },
            { separator: true },
            { label: "Deselect", action: () => { selectedFileIds.clear(); updateBatchActions(); updateFileSelectionClasses(); } },
        ]);
    });

    // --- Confirmation System ---

    // Keys and their default-enabled state (true = warn by default)
    const CONFIRM_KEYS = {
        confirm_delete_file:         { label: "Warn before deleting a file",                       default: true },
        confirm_batch_delete_files:  { label: "Warn before batch-deleting files",                  default: true },
        confirm_delete_folders:      { label: "Warn before deleting download folders",             default: true },
        confirm_delete_processed:    { label: "Warn before deleting processed output files",       default: true },
        confirm_delete_profile:      { label: "Warn before deleting a processing profile",         default: true },
        confirm_cancel_processing:   { label: "Warn before cancelling all processing",             default: true },
        confirm_cancel_scans:        { label: "Warn before cancelling all scans",                  default: true },
        confirm_scan_archive:        { label: "Warn before scanning an entire archive",            default: true },
        confirm_clear_queue:         { label: "Warn before clearing a queue",                      default: true },
    };

    // Runtime state — loaded from settings on init
    let confirmSettings = {};
    for (const k of Object.keys(CONFIRM_KEYS)) confirmSettings[k] = CONFIRM_KEYS[k].default;

    /**
     * Generic confirmation dialog.
     * If the warning for `key` is suppressed, calls onConfirm() immediately.
     * Otherwise shows a styled modal with title, message, suppress checkbox, and Cancel/Confirm buttons.
     * @param {string} key           - Setting key from CONFIRM_KEYS
     * @param {string} title         - Modal heading
     * @param {string} message       - Modal body (HTML allowed)
     * @param {Function} onConfirm   - Called when the user confirms
     * @param {object} [opts]        - Optional: { confirmText, confirmClass }
     */
    function confirmAction(key, title, message, onConfirm, opts = {}) {
        if (!confirmSettings[key]) {
            onConfirm();
            return;
        }
        const modal = $("#modal-confirm-action");
        $("#confirm-action-title").textContent = title;
        $("#confirm-action-message").innerHTML = message;
        $("#confirm-action-suppress").checked = false;
        const confirmBtn = $("#btn-confirm-action-confirm");
        confirmBtn.textContent = opts.confirmText || "Confirm";
        confirmBtn.className = "action-btn " + (opts.confirmClass || "danger");

        // Clean up old listeners by replacing nodes
        const newConfirm = confirmBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirm, confirmBtn);
        const cancelBtn = $("#btn-confirm-action-cancel");
        const newCancel = cancelBtn.cloneNode(true);
        cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);

        newCancel.addEventListener("click", () => modal.classList.remove("open"));
        newConfirm.addEventListener("click", () => {
            modal.classList.remove("open");
            if ($("#confirm-action-suppress").checked) {
                confirmSettings[key] = false;
                api("POST", "/api/settings", { [key]: "0" });
            }
            onConfirm();
        });

        modal.classList.add("open");
    }

    // --- Bandwidth ---

    let bwDebounce = null;

    function showBandwidthUI(limitBytes) {
        // Render the bandwidth controls for a given limit value
        // limitBytes: -1 = unlimited, 0 = paused, >0 = throttle
        const checkbox = $("#bandwidth-enabled");
        const input = $("#bandwidth-input");
        const control = input.closest(".bandwidth-control");

        if (limitBytes < 0) {
            checkbox.checked = false;
            input.disabled = true;
            input.value = "";
            input.placeholder = "uncapped";
            control.classList.add("disabled");
        } else {
            checkbox.checked = true;
            input.disabled = false;
            input.value = Math.round(limitBytes / 1024);
            input.placeholder = "KB/s";
            control.classList.remove("disabled");
        }
    }

    function updateBandwidthUI(limitBytes) {
        // Called when the real bandwidth setting changes (user action or schedule event)
        realBandwidth = limitBytes;
        // If paused, always show 0; otherwise show the real value
        showBandwidthUI(dlState === "paused" ? 0 : realBandwidth);
    }

    function syncBandwidthToState() {
        // Sync the bandwidth display to match the current dlState
        showBandwidthUI(dlState === "paused" ? 0 : realBandwidth);
    }

    function sendBandwidthLimit() {
        const checkbox = $("#bandwidth-enabled");
        const input = $("#bandwidth-input");
        let limit;
        if (!checkbox.checked) {
            limit = -1;
        } else {
            limit = (parseInt(input.value) || 0) * 1024;
        }
        realBandwidth = limit;
        api("POST", "/api/download/bandwidth", { limit });
    }

    function onBandwidthToggle() {
        const checkbox = $("#bandwidth-enabled");
        const input = $("#bandwidth-input");
        const control = input.closest(".bandwidth-control");
        if (checkbox.checked) {
            input.disabled = false;
            input.placeholder = "KB/s";
            control.classList.remove("disabled");
            if (!input.value) input.value = "0";
            input.focus();
        } else {
            input.disabled = true;
            input.value = "";
            input.placeholder = "uncapped";
            control.classList.add("disabled");
        }
        sendBandwidthLimit();
    }

    function onBandwidthInput() {
        clearTimeout(bwDebounce);
        bwDebounce = setTimeout(sendBandwidthLimit, 500);
    }

    // --- Escape ---

    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // --- Group Management ---

    let pendingGroupArchive = null; // archive object for "move to group" modal
    let pendingRenameGroup = null;  // group object for rename modal
    let pendingDeleteGroup = null;  // group object for delete modal

    function openCreateGroup() {
        $("#input-group-name").value = "";
        $("#group-create-error").textContent = "";
        $("#modal-create-group").classList.add("open");
        setTimeout(() => $("#input-group-name").focus(), 50);
    }

    async function doCreateGroup() {
        const name = $("#input-group-name").value.trim();
        if (!name) {
            $("#group-create-error").textContent = "Please enter a group name.";
            return;
        }
        try {
            await api("POST", "/api/groups", { name });
            $("#modal-create-group").classList.remove("open");
            await refreshGroups();
        } catch (e) {
            $("#group-create-error").textContent = e.message || "Failed to create group.";
        }
    }

    function openRenameGroup(g) {
        pendingRenameGroup = g;
        $("#input-group-rename").value = g.name;
        $("#group-rename-error").textContent = "";
        $("#modal-rename-group").classList.add("open");
        setTimeout(() => $("#input-group-rename").focus(), 50);
    }

    async function doRenameGroup() {
        if (!pendingRenameGroup) return;
        const name = $("#input-group-rename").value.trim();
        if (!name) {
            $("#group-rename-error").textContent = "Please enter a name.";
            return;
        }
        try {
            await api("PUT", `/api/groups/${pendingRenameGroup.id}`, { name });
            $("#modal-rename-group").classList.remove("open");
            pendingRenameGroup = null;
            await refreshGroups();
        } catch (e) {
            $("#group-rename-error").textContent = e.message || "Failed to rename group.";
        }
    }

    function openDeleteGroup(g) {
        pendingDeleteGroup = g;
        $("#delete-group-name").textContent = g.name;
        $("#modal-delete-group").classList.add("open");
    }

    async function doDeleteGroup() {
        if (!pendingDeleteGroup) return;
        try {
            await api("DELETE", `/api/groups/${pendingDeleteGroup.id}`);
            $("#modal-delete-group").classList.remove("open");
            pendingDeleteGroup = null;
            await refreshGroups();
            await refreshArchives();
        } catch (e) { /* ignore */ }
    }

    async function moveGroup(fromIdx, toIdx) {
        if (toIdx < 0 || toIdx >= groups.length) return;
        const order = groups.map((x) => x.id);
        const [moved] = order.splice(fromIdx, 1);
        order.splice(toIdx, 0, moved);
        await api("POST", "/api/groups/reorder", { order });
        await refreshGroups();
    }

    function openMoveToGroup(a) {
        pendingGroupArchive = a;
        $("#move-archive-name").textContent = a.title || a.identifier;
        const list = $("#move-group-list");
        list.innerHTML = "";

        // "No group" option
        const noGroup = document.createElement("button");
        noGroup.className = "move-group-option" + (!a.group_id ? " active" : "");
        noGroup.textContent = "No group";
        noGroup.addEventListener("click", () => doMoveToGroup(null));
        list.appendChild(noGroup);

        // Each group
        groups.forEach((g) => {
            const btn = document.createElement("button");
            btn.className = "move-group-option" + (a.group_id === g.id ? " active" : "");
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z" fill="currentColor"/></svg> ${escapeHtml(g.name)}`;
            btn.addEventListener("click", () => doMoveToGroup(g.id));
            list.appendChild(btn);
        });

        $("#modal-move-to-group").classList.add("open");
    }

    async function doMoveToGroup(groupId) {
        if (!pendingGroupArchive) return;
        try {
            await api("POST", `/api/archives/${pendingGroupArchive.id}/group`, { group_id: groupId });
            $("#modal-move-to-group").classList.remove("open");
            pendingGroupArchive = null;
            await refreshArchives();
        } catch (e) { /* ignore */ }
    }

    // --- Processing ---

    let processorTypes = {};  // loaded once from API
    let processingProfiles = []; // cached profiles

    async function loadProcessorTypes() {
        if (Object.keys(processorTypes).length) return;
        try {
            processorTypes = await api("GET", "/api/processing/types");
        } catch (e) { /* ignore */ }
    }

    async function loadProcessingProfiles() {
        try {
            processingProfiles = await api("GET", "/api/processing/profiles");
        } catch (e) {
            processingProfiles = [];
        }
        return processingProfiles;
    }

    function renderProfileOptions(containerEl, typeId, currentOptions) {
        containerEl.innerHTML = "";
        const typeInfo = processorTypes[typeId];
        if (!typeInfo || !typeInfo.options_schema) return;
        for (const opt of typeInfo.options_schema) {
            const label = document.createElement("label");
            label.textContent = opt.label;
            let input;
            if (opt.type === "select" && opt.choices) {
                input = document.createElement("select");
                input.dataset.optKey = opt.key;
                for (const ch of opt.choices) {
                    const o = document.createElement("option");
                    o.value = ch.value;
                    o.textContent = ch.label;
                    if ((currentOptions && currentOptions[opt.key] || opt.default) === ch.value) o.selected = true;
                    input.appendChild(o);
                }
            } else {
                input = document.createElement("input");
                input.type = opt.type === "number" ? "number" : "text";
                input.dataset.optKey = opt.key;
                input.value = (currentOptions && currentOptions[opt.key]) ?? opt.default ?? "";
                if (opt.description) {
                    const sm = document.createElement("small");
                    sm.textContent = opt.description;
                    label.appendChild(input);
                    label.appendChild(sm);
                    containerEl.appendChild(label);
                    continue;
                }
            }
            label.appendChild(input);
            containerEl.appendChild(label);
        }
    }

    function collectOptions(containerEl) {
        const opts = {};
        containerEl.querySelectorAll("[data-opt-key]").forEach(el => {
            opts[el.dataset.optKey] = el.value;
        });
        return opts;
    }

    // --- Processing Profiles in Settings ---

    async function renderProfilesList() {
        await loadProcessorTypes();
        const profiles = await loadProcessingProfiles();
        const list = $("#processing-profiles-list");
        if (!profiles.length) {
            list.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">No profiles yet. Click "Add Profile" to create one.</p>';
            return;
        }
        list.innerHTML = "";
        for (const p of profiles) {
            const row = document.createElement("div");
            row.className = "profile-row";
            const typeLabel = (processorTypes[p.processor_type] || {}).label || p.processor_type;
            row.innerHTML = `
                <div class="profile-info">
                    <strong>${escapeHtml(p.name)}</strong>
                    <small>${escapeHtml(typeLabel)}</small>
                </div>
                <div class="profile-actions">
                    <button class="action-btn small" data-edit-profile="${p.id}">Edit</button>
                    <button class="action-btn small danger" data-delete-profile="${p.id}">Delete</button>
                </div>`;
            list.appendChild(row);
        }
        list.querySelectorAll("[data-edit-profile]").forEach(btn => {
            btn.addEventListener("click", () => openEditProfile(parseInt(btn.dataset.editProfile)));
        });
        list.querySelectorAll("[data-delete-profile]").forEach(btn => {
            btn.addEventListener("click", () => {
                confirmAction("confirm_delete_profile", "Delete Profile", "Delete this processing profile? This cannot be undone.", async () => {
                    await api("DELETE", `/api/processing/profiles/${btn.dataset.deleteProfile}`);
                    renderProfilesList();
                }, { confirmText: "Delete Profile" });
            });
        });
    }

    let editingProfileId = null;

    async function openEditProfile(profileId) {
        await loadProcessorTypes();
        editingProfileId = profileId || null;
        const modal = $("#modal-edit-profile");
        const nameInput = $("#edit-profile-name");
        const typeSelect = $("#edit-profile-type");
        const optionsDiv = $("#edit-profile-options");

        $("#edit-profile-title").textContent = editingProfileId ? "Edit Profile" : "Add Profile";

        // Populate type dropdown
        typeSelect.innerHTML = "";
        for (const [tid, info] of Object.entries(processorTypes)) {
            const o = document.createElement("option");
            o.value = tid;
            o.textContent = info.label;
            typeSelect.appendChild(o);
        }

        if (editingProfileId) {
            const profile = processingProfiles.find(p => p.id === editingProfileId);
            if (profile) {
                nameInput.value = profile.name;
                typeSelect.value = profile.processor_type;
                renderProfileOptions(optionsDiv, profile.processor_type, profile.options);
            }
        } else {
            nameInput.value = "";
            renderProfileOptions(optionsDiv, typeSelect.value, {});
        }

        typeSelect.onchange = () => renderProfileOptions(optionsDiv, typeSelect.value, {});
        modal.classList.add("open");
    }

    async function saveProfile() {
        const name = $("#edit-profile-name").value.trim();
        const processorType = $("#edit-profile-type").value;
        const options = collectOptions($("#edit-profile-options"));
        if (!name) return;

        if (editingProfileId) {
            await api("PUT", `/api/processing/profiles/${editingProfileId}`, { name, processor_type: processorType, options });
        } else {
            await api("POST", "/api/processing/profiles", { name, processor_type: processorType, options });
        }
        $("#modal-edit-profile").classList.remove("open");
        renderProfilesList();
    }

    // --- Tool Detection ---

    async function detectAndShowTools() {
        const container = $("#processing-tools-status");
        if (!container) return;
        container.textContent = "Detecting tools\u2026";
        try {
            const tools = await api("GET", "/api/processing/tools");
            const table = document.createElement("table");
            table.className = "tools-status-table";
            for (const [name, info] of Object.entries(tools)) {
                const tr = document.createElement("tr");
                const tdName = document.createElement("td");
                tdName.textContent = name;
                const tdStatus = document.createElement("td");
                if (info.available) {
                    tdStatus.innerHTML =
                        '<span class="tool-found">Detected</span> <span class="tool-version">' +
                        (info.version || "unknown version") + "</span>";
                } else {
                    tdStatus.innerHTML = '<span class="tool-missing">Not found</span>';
                }
                tr.appendChild(tdName);
                tr.appendChild(tdStatus);
                table.appendChild(tr);
            }
            container.innerHTML = "";
            container.appendChild(table);
        } catch (e) {
            container.textContent = "Failed to detect tools.";
        }
    }

    // --- Process Archive Modal ---

    async function openProcessArchiveModal() {
        if (!currentArchiveId && !pendingBatchArchiveProcessIds) return;
        await loadProcessorTypes();
        const profiles = await loadProcessingProfiles();
        const select = $("#process-profile-select");
        select.innerHTML = "";
        // Remove any previous no-profiles message
        const oldMsg = select.parentNode.querySelector(".process-no-profiles");
        if (oldMsg) oldMsg.remove();
        if (!profiles.length) {
            select.style.display = "none";
            $("#btn-process-confirm").disabled = true;
            // Show inline message with link
            const msg = document.createElement("span");
            msg.className = "process-no-profiles";
            msg.textContent = "No profiles available, ";
            const link = document.createElement("a");
            link.href = "#";
            link.className = "process-create-link";
            link.textContent = "create one in Settings";
            link.addEventListener("click", (e) => {
                e.preventDefault();
                $("#modal-process-archive").classList.remove("open");
                navigateToProcessingProfiles();
            });
            msg.appendChild(link);
            select.parentNode.insertBefore(msg, select);
        } else {
            select.style.display = "";
            for (const p of profiles) {
                const o = document.createElement("option");
                o.value = p.id;
                o.textContent = p.name;
                select.appendChild(o);
            }
            $("#btn-process-confirm").disabled = false;
            // Show options for selected profile
            const onProfileChange = () => {
                const pid = parseInt(select.value);
                const prof = profiles.find(p => p.id === pid);
                if (prof) renderProfileOptions($("#process-profile-options"), prof.processor_type, prof.options);
            };
            select.onchange = onProfileChange;
            onProfileChange();
        }

        // Show eligible file count
        try {
            const data = await api("GET", `/api/archives/${currentArchiveId}/processable`);
            $("#process-eligible-count").textContent = `${data.count} file${data.count !== 1 ? "s" : ""} eligible for processing`;
        } catch (e) {
            $("#process-eligible-count").textContent = "";
        }

        const archiveName = getArchiveName(currentArchiveId);
        $("#process-archive-info").textContent = `Process files in "${archiveName}"`;
        $("#modal-process-archive").classList.add("open");
    }

    async function confirmProcessArchive() {
        const profileId = parseInt($("#process-profile-select").value);
        const options = collectOptions($("#process-profile-options"));
        const fileIds = pendingBatchProcessIds || undefined;
        pendingBatchProcessIds = null;

        // Batch archive processing
        const archiveIds = pendingBatchArchiveProcessIds || (currentArchiveId ? [currentArchiveId] : []);
        pendingBatchArchiveProcessIds = null;

        if (!profileId || archiveIds.length === 0) return;

        let queued = 0;
        for (const aid of archiveIds) {
            try {
                const body = { profile_id: profileId, options };
                if (fileIds) body.file_ids = fileIds;
                const resp = await api("POST", `/api/archives/${aid}/process`, body);
                if (resp.queued) queued++;
            } catch (e) {
                addNotification(`Processing failed for "${getArchiveName(aid)}": ${e.message}`, "error");
            }
        }
        // Notifications for queued processing jobs are now created server-side
        selectedArchiveIds.clear();
        updateArchiveBatchActions();
        $("#modal-process-archive").classList.remove("open");
    }

    // --- Processing SSE Events (UI updates only — notifications handled server-side) ---

    function updateProcessingProgress(data) {
        const { archive_id, phase } = data;

        if (phase === "starting") {
            if (currentArchiveId === archive_id) loadFiles();
        } else if (phase === "file_done") {
            if (currentArchiveId === archive_id) loadFiles();
        } else if (phase === "file_error") {
            if (currentArchiveId === archive_id) loadFiles();
        } else if (phase === "done") {
            if (currentArchiveId === archive_id) loadFiles();
            refreshArchives();
        } else if (phase === "cancelled") {
            if (currentArchiveId === archive_id) loadFiles();
        }
    }

    // ── Archive Tags & Collection Membership ───────────────────────────────

    async function loadArchiveTagsAndCollections(archiveId) {
        const tagsEl = $("#archive-tags");
        tagsEl.innerHTML = "";
        try {
            const tags = await api("GET", `/api/archives/${archiveId}/tags`);
            renderArchiveTags(tags, archiveId);
        } catch (e) {
            // Silently fail — tags are optional
        }
    }

    function formatTagHtml(tagStr) {
        /* Render parent:child tags with muted prefix */
        const idx = tagStr.indexOf(":");
        if (idx > 0) {
            const parent = tagStr.substring(0, idx + 1);
            const child = tagStr.substring(idx + 1);
            return `<span class="tag-parent-prefix">${esc(parent)}</span>${esc(child)}`;
        }
        return esc(tagStr);
    }

    function renderArchiveTags(tags, archiveId) {
        const el = $("#archive-tags");
        el.innerHTML = "";
        for (const item of tags) {
            /* item is { tag: string, auto: bool } */
            const tag = typeof item === "string" ? item : item.tag;
            const isAuto = typeof item === "object" && item.auto;
            const chip = document.createElement("span");
            chip.className = "tag-chip" + (isAuto ? " tag-auto" : "");
            let inner = formatTagHtml(tag);
            if (!isAuto) {
                inner += ` <button class="tag-remove" data-tag="${esc(tag)}">&times;</button>`;
            }
            chip.innerHTML = inner;
            el.appendChild(chip);
        }
        // Remove tag handler (only on user tags)
        el.querySelectorAll(".tag-remove").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const tag = btn.dataset.tag;
                await api("DELETE", `/api/archives/${archiveId}/tags/${encodeURIComponent(tag)}`);
                loadArchiveTagsAndCollections(archiveId);
            });
        });
    }

    // ── Collections ────────────────────────────────────────────────────────

    let collections = [];
    let currentCollectionId = null;
    let editingCollectionId = null;
    // (editingLayoutId removed — layout creation is now direct, no modal)

    async function refreshCollections() {
        try {
            collections = await api("GET", "/api/collections");
        } catch (e) {
            collections = [];
        }
    }

    function showPage(pageId) {
        $$(".page").forEach((p) => p.classList.remove("active"));
        $(`#${pageId}`).classList.add("active");
    }

    // ── Ongoing Activity State ───────────────────────────────────────
    // Tracks what's happening now, shared by Phase 2 (activity page) and Phase 4 (dropdown)
    let ongoingProcessing = null;  // {archive_id, filename, current, total, phase}
    let ongoingScanning = null;    // {archive_id, phase, current, total}

    /**
     * Render the compact ongoing-activity rows.
     * @param {HTMLElement} container  — the element to fill with rows
     * @param {HTMLElement} emptyEl    — the "no active tasks" element to show/hide
     */
    function renderOngoingActivity(container, emptyEl) {
        let html = "";

        // Individual download rows with per-file progress bars
        if (activeDownloads.length > 0 && (dlState === "running" || dlState === "paused")) {
            for (const dl of activeDownloads) {
                const fname = escapeHtml(dl.filename || "Unknown");
                const pct = dl.size > 0 ? Math.min(100, (dl.downloaded || 0) / dl.size * 100) : 0;
                const pctText = dl.size > 0 ? pct.toFixed(1) + "%" : "";
                const speedText = dl.speed ? formatSpeed(dl.speed) : "";
                const sizeText = dl.size > 0 ? formatBytes(dl.size) : "";
                html += `<div class="activity-ongoing-row activity-dl-row" data-navigate="download" data-file-id="${dl.file_id || ""}">`;
                html += `<div class="activity-dl-info">`;
                html += `<span class="activity-dl-name" title="${fname}">${fname}</span>`;
                html += `<span class="activity-dl-stats">${pctText}${sizeText ? " of " + sizeText : ""}${speedText ? " — " + speedText : ""}</span>`;
                html += `</div>`;
                html += `<div class="activity-dl-bar"><div class="activity-dl-fill" style="width:${pct.toFixed(1)}%"></div></div>`;
                html += `</div>`;
            }
        }

        // Processing row
        if (ongoingProcessing && ongoingProcessing.phase !== "done" && ongoingProcessing.phase !== "error" && ongoingProcessing.phase !== "cancelled") {
            const fname = escapeHtml(ongoingProcessing.filename || "");
            const phase = ongoingProcessing.phase || "";
            const hasPct = ongoingProcessing.pct != null;
            const hasFileCount = ongoingProcessing.total > 0;
            // Use tool-level pct (e.g. chdman) when available, otherwise file-level
            const barPct = hasPct ? Math.min(100, ongoingProcessing.pct) : (hasFileCount ? Math.min(100, (ongoingProcessing.current / ongoingProcessing.total) * 100) : 0);
            const progText = hasFileCount ? `${ongoingProcessing.current}/${ongoingProcessing.total}` : "";
            const phaseText = hasPct ? `${phase} ${ongoingProcessing.pct.toFixed(1)}%` : progText;
            html += `<div class="activity-ongoing-row activity-dl-row" data-navigate="processing" data-archive-id="${ongoingProcessing.archive_id || ""}">`;
            html += `<div class="activity-dl-info">`;
            html += `<span class="activity-dl-name"><strong>Processing</strong>${fname ? " — " + fname : ""}</span>`;
            html += `<span class="activity-dl-stats">${phaseText}`;
            if (ongoingProcessing.archive_id) html += ` <button class="activity-cancel-btn" data-cancel="processing" data-archive-id="${ongoingProcessing.archive_id}">Cancel</button>`;
            html += `</span></div>`;
            if (hasPct || hasFileCount) {
                html += `<div class="activity-dl-bar"><div class="activity-dl-fill" style="width:${barPct.toFixed(1)}%"></div></div>`;
            } else {
                html += `<div class="activity-dl-bar"><div class="activity-dl-fill activity-dl-fill-indeterminate"></div></div>`;
            }
            html += `</div>`;
        }

        // Scanning row
        if (ongoingScanning && ongoingScanning.phase !== "done" && ongoingScanning.phase !== "error" && ongoingScanning.phase !== "cancelled") {
            const pct = ongoingScanning.total > 0 ? Math.min(100, (ongoingScanning.current / ongoingScanning.total) * 100) : 0;
            const progText = ongoingScanning.total > 0 ? `${ongoingScanning.current}/${ongoingScanning.total}` : "";
            const phaseText = ongoingScanning.phase || "";
            html += `<div class="activity-ongoing-row activity-dl-row" data-navigate="scan" data-archive-id="${ongoingScanning.archive_id || ""}">`;
            html += `<div class="activity-dl-info">`;
            html += `<span class="activity-dl-name"><strong>Scanning</strong>${phaseText ? " — " + escapeHtml(phaseText) : ""}</span>`;
            html += `<span class="activity-dl-stats">${progText}`;
            if (ongoingScanning.archive_id) html += ` <button class="activity-cancel-btn" data-cancel="scan" data-archive-id="${ongoingScanning.archive_id}">Cancel</button>`;
            html += `</span></div>`;
            if (ongoingScanning.total > 0) {
                html += `<div class="activity-dl-bar"><div class="activity-dl-fill" style="width:${pct.toFixed(1)}%"></div></div>`;
            } else {
                html += `<div class="activity-dl-bar"><div class="activity-dl-fill activity-dl-fill-indeterminate"></div></div>`;
            }
            html += `</div>`;
        }

        container.innerHTML = html;
        emptyEl.style.display = html ? "none" : "";

        // Click handlers to navigate to queue tabs and scroll to the item
        container.querySelectorAll(".activity-ongoing-row").forEach((row) => {
            row.addEventListener("click", async (e) => {
                if (e.target.closest(".activity-cancel-btn")) return;
                const tab = row.dataset.navigate;
                if (!tab) return;
                const fileId = row.dataset.fileId;
                const archiveId = row.dataset.archiveId;
                await openQueues(tab);
                // After queue loads, scroll to and flash the relevant row
                requestAnimationFrame(() => {
                    scrollToQueueItemAndFlash(tab, { fileId, archiveId });
                });
            });
        });
        // Cancel buttons
        container.querySelectorAll(".activity-cancel-btn").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const archiveId = parseInt(btn.dataset.archiveId);
                const type = btn.dataset.cancel;
                if (type === "processing") cancelProcessing(archiveId);
                else if (type === "scan") cancelScan(archiveId);
            });
        });
    }

    function refreshOngoingActivity() {
        const container = $("#activity-ongoing-rows");
        const empty = $("#activity-ongoing-empty");
        if (container && empty) renderOngoingActivity(container, empty);
    }

    // ── Queue Page ──────────────────────────────────────────────────
    let queueCounts = { download: 0, processing: 0, scan: 0 };
    let queueData = { download: [], processing: [], scan: [] };
    let queueStale = { download: true, processing: true, scan: true };
    let activeQueueTab = "download";
    // Track items completing with 3-second grey-out before removal
    // Map of "queueType:entryId" -> setTimeout handle
    let completingItems = new Map();

    async function openQueues(tab) {
        if (tab) activeQueueTab = tab;
        showPage("page-queues");
        switchQueueTab(activeQueueTab);
        await refreshQueueCounts();
        await loadQueueTab(activeQueueTab);
    }

    function switchQueueTab(tab) {
        activeQueueTab = tab;
        $$(".queue-tab").forEach((t) => t.classList.toggle("active", t.dataset.queue === tab));
        $$(".queue-panel").forEach((p) => p.classList.toggle("active", p.id === `queue-panel-${tab}`));
        if (queueStale[tab]) loadQueueTab(tab);
    }

    const PAUSE_ICON = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" fill="currentColor"/></svg>';
    const PLAY_ICON = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';

    function updatePauseButton(btnId, paused) {
        const btn = document.getElementById(btnId);
        if (!btn) return;
        btn.innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
        btn.title = paused
            ? (btnId.includes("proc") ? "Resume processing" : "Resume scanning")
            : (btnId.includes("proc") ? "Pause processing" : "Pause scanning");
        btn.classList.toggle("active", paused);
    }

    async function refreshQueueCounts() {
        try {
            const counts = await api("GET", "/api/queues/counts");
            queueCounts = counts;
            updateQueueBadges();
            // Seed pause states from server
            if (counts.processing_paused !== undefined) {
                db_processing_paused = counts.processing_paused;
                updatePauseButton("queue-proc-pause", db_processing_paused);
            }
            if (counts.scan_paused !== undefined) {
                db_scan_paused = counts.scan_paused;
                updatePauseButton("queue-scan-pause", db_scan_paused);
            }
        } catch (e) { /* ignore */ }
    }

    function updateQueueBadges() {
        const dlBadge = $("#queue-tab-download-badge");
        const procBadge = $("#queue-tab-processing-badge");
        const scanBadge = $("#queue-tab-scan-badge");
        dlBadge.textContent = queueCounts.download > 0 ? `(${queueCounts.download})` : "";
        procBadge.textContent = queueCounts.processing > 0 ? `(${queueCounts.processing})` : "";
        scanBadge.textContent = queueCounts.scan > 0 ? `(${queueCounts.scan})` : "";
    }

    async function loadQueueTab(tab) {
        queueStale[tab] = false;
        const endpoints = {
            download: "/api/download/queue",
            processing: "/api/processing/queue",
            scan: "/api/scan/queue",
        };
        try {
            const data = await api("GET", endpoints[tab]);
            queueData[tab] = data;
            renderQueueTable(tab);
        } catch (e) {
            console.error("Failed to load queue:", e);
        }
    }

    // Queue selection state (separate from file list selection)
    let selectedQueueIds = new Set();
    let lastClickedQueueIdx = null;
    let queueDragSrcIdx = null;
    let queueFilterTimers = {};
    let queueFilterText = { download: "", processing: "", scan: "" };
    let queueSortBy = { download: "position", processing: "position", scan: "position" };
    // Filtered+sorted view of queueData, rebuilt on each render
    let queueView = { download: [], processing: [], scan: [] };
    // Queue virtual scroll state
    const QS_ROW_HEIGHT = 37;
    const QS_OVERSCAN = 15;
    let qsLastRange = { download: null, processing: null, scan: null };
    let qsScrollRAF = { download: null, processing: null, scan: null };

    function getQueueItemId(tab, item) {
        return tab === "download" ? (item.file_id || item.id) : item.id;
    }

    function getQueueItemPos(tab, item) {
        return tab === "download" ? item.queue_position : (item.position ?? item.queue_position ?? 0);
    }

    function handleQueueSelect(tab, idx, e) {
        const view = queueView[tab] || [];
        const item = view[idx];
        if (!item) return;
        const key = getQueueItemId(tab, item);
        if (e.shiftKey && lastClickedQueueIdx !== null) {
            const start = Math.min(lastClickedQueueIdx, idx);
            const end = Math.max(lastClickedQueueIdx, idx);
            if (!e.ctrlKey && !e.metaKey) selectedQueueIds.clear();
            for (let i = start; i <= end; i++) {
                selectedQueueIds.add(getQueueItemId(tab, view[i]));
            }
        } else if (e.ctrlKey || e.metaKey) {
            if (selectedQueueIds.has(key)) selectedQueueIds.delete(key);
            else selectedQueueIds.add(key);
            lastClickedQueueIdx = idx;
        } else {
            selectedQueueIds.clear();
            selectedQueueIds.add(key);
            lastClickedQueueIdx = idx;
        }
        renderQueueTable(tab);
    }

    function filterAndSortQueue(tab) {
        const raw = queueData[tab] || [];
        const filterStr = queueFilterText[tab].toLowerCase();
        const sortKey = queueSortBy[tab];

        // Filter
        let filtered = raw;
        if (filterStr) {
            filtered = raw.filter(item => {
                const fname = (item.file_name || item.name || "").toLowerCase();
                const archive = (item.title || item.archive_title || item.archive_identifier || item.identifier || "").toLowerCase();
                return fname.includes(filterStr) || archive.includes(filterStr);
            });
        }

        // Sort
        const sorted = [...filtered];
        if (sortKey === "position") {
            sorted.sort((a, b) => getQueueItemPos(tab, a) - getQueueItemPos(tab, b));
        } else if (sortKey === "name") {
            sorted.sort((a, b) => (a.file_name || a.name || "").localeCompare(b.file_name || b.name || ""));
        } else if (sortKey === "size") {
            sorted.sort((a, b) => (b.size || b.file_size || 0) - (a.size || a.file_size || 0));
        } else if (sortKey === "status") {
            sorted.sort((a, b) => {
                const sa = tab === "download" ? (a.download_status || "") : (a.status || "");
                const sb = tab === "download" ? (b.download_status || "") : (b.status || "");
                return sa.localeCompare(sb);
            });
        } else if (sortKey === "archive") {
            sorted.sort((a, b) => {
                const la = (a.title || a.archive_title || a.archive_identifier || a.identifier || "").toLowerCase();
                const lb = (b.title || b.archive_title || b.archive_identifier || b.identifier || "").toLowerCase();
                return la.localeCompare(lb);
            });
        }
        queueView[tab] = sorted;
        return sorted;
    }

    function isQueueSortedByPosition(tab) {
        return queueSortBy[tab] === "position";
    }

    function getQueueColspan(tab) {
        return tab === "download" ? 7 : tab === "processing" ? 5 : 3;
    }

    function buildQueueRow(tab, item, i, lastIdx, byPosition) {
        const fname = item.file_name || item.name || "";
        const archiveLabel = item.title || item.archive_title || item.archive_identifier || item.identifier || "";
        const bold = item.downloaded ? "file-name-downloaded" : "";
        const archiveId = item.archive_id;
        const fileId = item.file_id || item.id;
        const entryId = item.id || item.file_id;
        const isCompleting = completingItems.has(`${tab}:${entryId}`);
        const selKey = getQueueItemId(tab, item);

        const tr = document.createElement("tr");
        tr.dataset.archiveId = archiveId;
        if (tab === "download") tr.dataset.fileId = fileId;
        else tr.dataset.entryId = item.id;
        if (isCompleting) tr.className = "queue-completing";
        if (selectedQueueIds.has(selKey)) tr.classList.add("selected");
        if (isFlashing(tr)) tr.classList.add("queue-flash");

        let html = "";

        if (tab === "download") {
            const status = item.download_status || "queued";
            const fileSize = item.size || item.file_size || 0;
            let statusLabel = status;
            let statusPctStyle = "";
            if (status === "downloading" && fileSize > 0) {
                const pct = Math.min(100, ((item.downloaded_bytes || 0) / fileSize) * 100);
                statusLabel = pct.toFixed(1) + "%";
                statusPctStyle = ` style="--pct:${pct.toFixed(1)}%"`;
            }
            if (byPosition) {
                html += buildGripCell();
            } else {
                html += `<td class="col-grip"><span class="queue-pos-num">${getQueueItemPos(tab, item)}</span></td>`;
            }
            html += `<td class="col-queue"><button class="queue-toggle queue-remove" data-queue-id="${fileId}" title="Remove from queue">` +
                `<svg viewBox="0 0 16 16" width="14" height="14"><rect x="3" y="7" width="10" height="2" rx="1" fill="currentColor"/></svg></button></td>`;
            html += `<td class="col-name"><div class="file-name-wrap">${renderFileName(fname, bold)}</div></td>`;
            html += `<td class="col-archive-q" title="${escapeHtml(archiveLabel)}">${escapeHtml(archiveLabel)}</td>`;
            html += `<td class="col-size" style="text-align:right">${formatBytes(fileSize)}</td>`;
            html += `<td class="col-status"><span class="file-status ${status}"${statusPctStyle}>${statusLabel}</span></td>`;
            if (byPosition) {
                html += buildPriorityCell(fileId, i === 0, i === lastIdx);
            } else {
                html += `<td class="col-priority"></td>`;
            }
        } else if (tab === "processing") {
            const status = item.status || "pending";
            const statusClass = (status === "running" || status === "processing") ? "proc-active" : status;
            let statusLabel = status;
            let statusPctStyle = "";
            if ((status === "running" || status === "processing") && ongoingProcessing) {
                // Prefer per-file tool progress (e.g. chdman %) when available
                if (ongoingProcessing.pct != null) {
                    const pct = Math.min(100, ongoingProcessing.pct);
                    const phase = ongoingProcessing.phase || "processing";
                    statusLabel = pct.toFixed(1) + "%";
                    statusPctStyle = ` style="--pct:${pct.toFixed(1)}%" title="${phase}"`;
                } else if (ongoingProcessing.total > 0) {
                    const pct = Math.min(100, (ongoingProcessing.current / ongoingProcessing.total) * 100);
                    statusLabel = pct.toFixed(1) + "%";
                    statusPctStyle = ` style="--pct:${pct.toFixed(1)}%"`;
                }
            }
            if (byPosition) {
                html += buildGripCell();
            } else {
                html += `<td class="col-grip"><span class="queue-pos-num">${getQueueItemPos(tab, item)}</span></td>`;
            }
            html += `<td class="col-name"><div class="file-name-wrap">${renderFileName(fname, bold)}</div></td>`;
            html += `<td class="col-archive-q" title="${escapeHtml(archiveLabel)}">${escapeHtml(archiveLabel)}</td>`;
            html += `<td class="col-profile-q" title="${escapeHtml(item.profile_name || "")}">${escapeHtml(item.profile_name || "")}</td>`;
            html += `<td class="col-status"><span class="file-status ${statusClass}"${statusPctStyle}>${statusLabel}</span></td>`;
        } else {
            const status = item.status || "pending";
            const statusClass = (status === "running" || status === "scanning") ? "scanning" : status;
            let statusLabel = status;
            let statusPctStyle = "";
            if ((status === "running" || status === "scanning") && ongoingScanning && ongoingScanning.total > 0) {
                const pct = Math.min(100, (ongoingScanning.current / ongoingScanning.total) * 100);
                statusLabel = pct.toFixed(1) + "%";
                statusPctStyle = ` style="--pct:${pct.toFixed(1)}%"`;
            }
            html += `<td class="col-name"><div class="file-name-wrap">${renderFileName(fname, bold)}</div></td>`;
            html += `<td class="col-archive-q" title="${escapeHtml(archiveLabel)}">${escapeHtml(archiveLabel)}</td>`;
            html += `<td class="col-status"><span class="file-status ${statusClass}"${statusPctStyle}>${statusLabel}</span></td>`;
        }

        tr.innerHTML = html;

        // Click: select — double-click: navigate
        tr.addEventListener("click", (e) => {
            if (e.target.closest("button, .queue-toggle, .file-priority-btns, .file-grip")) return;
            handleQueueSelect(tab, i, e);
        });
        tr.addEventListener("dblclick", (e) => {
            if (e.target.closest("button, .queue-toggle")) return;
            if (archiveId && fileId) {
                navigateToFile(parseInt(archiveId), parseInt(fileId));
            } else if (archiveId) {
                openArchiveDetail(parseInt(archiveId));
            }
        });

        // Right-click: context menu
        tr.addEventListener("contextmenu", (e) => {
            e.preventDefault();

            // Modifier key handling: ctrl/shift behave as single-click before opening menu
            if (e.ctrlKey || e.metaKey || e.shiftKey) {
                handleQueueSelect(tab, i, e);
            } else if (!selectedQueueIds.has(selKey)) {
                selectedQueueIds.clear();
                selectedQueueIds.add(selKey);
                renderQueueTable(tab);
            }

            const n = selectedQueueIds.size;
            const byPos = isQueueSortedByPosition(tab);
            const items = [
                { label: `Remove from Queue (${n})`, action: () => batchRemoveFromQueue(tab) },
            ];
            if (byPos) {
                items.push({ label: "Move to Top", action: () => batchMoveQueue(tab, "top") });
                items.push({ label: "Move to Bottom", action: () => batchMoveQueue(tab, "bottom") });
            }
            items.push({ separator: true });
            items.push({ label: "Deselect", action: () => { selectedQueueIds.clear(); renderQueueTable(tab); } });

            showContextMenu(e, items);
        });

        // Queue remove toggle (download only)
        const queueBtn = tr.querySelector(".queue-toggle");
        if (queueBtn) {
            queueBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                const fid = parseInt(queueBtn.dataset.queueId);
                api("POST", `/api/files/${fid}/queue`, { queued: false }).then(() => {
                    queueStale.download = true;
                    loadQueueTab("download");
                    refreshQueueCounts();
                });
            });
        }

        // Priority up/down buttons (download only, position sort only)
        if (tab === "download" && byPosition) {
            const upBtn = tr.querySelector(`[data-move-up="${fileId}"]`);
            const downBtn = tr.querySelector(`[data-move-down="${fileId}"]`);
            if (upBtn) upBtn.addEventListener("click", (e) => { e.stopPropagation(); moveQueueItem(tab, i, -1); });
            if (downBtn) downBtn.addEventListener("click", (e) => { e.stopPropagation(); moveQueueItem(tab, i, 1); });
        }

        // Drag-and-drop reordering (only when sorted by position)
        if (byPosition && (tab === "download" || tab === "processing")) {
            attachQueueDrag(tr, tab, i);
        }

        return tr;
    }

    // Called when data or filter/sort changes — recomputes view and triggers virtual render
    function renderQueueTable(tab) {
        const abbr = tab === "download" ? "dl" : tab === "processing" ? "proc" : "scan";
        const empty = $(`#queue-${abbr}-empty`);
        const wrap = $(`#queue-${abbr}-wrap`);

        const view = filterAndSortQueue(tab);

        if (!view || view.length === 0) {
            wrap.style.display = "none";
            empty.style.display = "";
            empty.textContent = queueFilterText[tab]
                ? `No files matching "${queueFilterText[tab]}"`
                : `No files in ${tab} queue`;
            return;
        }
        wrap.style.display = "";
        empty.style.display = "none";

        // Reset virtual scroll range so it fully re-renders
        qsLastRange[tab] = null;
        qsVsRenderVisible(tab);
    }

    /**
     * Scroll to a specific item in a queue tab's virtual scroll and flash it.
     * For download tab, pass fileId; for processing/scan, pass archiveId to match first entry.
     */
    function scrollToQueueItemAndFlash(tab, { fileId, archiveId } = {}) {
        const view = queueView[tab] || [];
        let idx = -1;
        if (tab === "download" && fileId) {
            idx = view.findIndex(item => (item.file_id || item.id) == fileId);
        } else if (archiveId) {
            idx = view.findIndex(item => item.archive_id == archiveId);
        }
        if (idx === -1) return;

        const abbr = tab === "download" ? "dl" : tab === "processing" ? "proc" : "scan";
        const wrap = $(`#queue-${abbr}-wrap`);
        if (!wrap) return;

        // Scroll so the target row is centered
        const targetTop = idx * QS_ROW_HEIGHT;
        wrap.scrollTop = targetTop - wrap.clientHeight / 2 + QS_ROW_HEIGHT / 2;

        // Force re-render at the new scroll position, then flash
        qsLastRange[tab] = null;
        qsVsRenderVisible(tab);

        setTimeout(() => {
            const tbody = $(`#queue-${abbr}-tbody`);
            if (!tbody) return;
            let row;
            if (tab === "download" && fileId) {
                row = tbody.querySelector(`tr[data-file-id="${fileId}"]`);
            } else if (archiveId) {
                row = tbody.querySelector(`tr[data-archive-id="${archiveId}"]`);
            }
            if (row) flashElement(row);
        }, 50);
    }

    // Render only the visible slice of rows in a queue table (virtual scroll)
    function qsVsRenderVisible(tab) {
        // Don't rebuild rows during drag
        if (queueDragSrcIdx !== null) return;

        const abbr = tab === "download" ? "dl" : tab === "processing" ? "proc" : "scan";
        const tbody = $(`#queue-${abbr}-tbody`);
        const wrap = $(`#queue-${abbr}-wrap`);
        const view = queueView[tab];
        if (!wrap || !view || view.length === 0) return;

        const totalRows = view.length;
        const byPosition = isQueueSortedByPosition(tab);
        const lastIdx = totalRows - 1;
        const colspan = getQueueColspan(tab);

        const scrollTop = wrap.scrollTop;
        const viewHeight = wrap.clientHeight;

        let startRow = Math.floor(scrollTop / QS_ROW_HEIGHT) - QS_OVERSCAN;
        let endRow = Math.ceil((scrollTop + viewHeight) / QS_ROW_HEIGHT) + QS_OVERSCAN;
        startRow = Math.max(0, startRow);
        endRow = Math.min(totalRows - 1, endRow);

        // Skip re-render if the range hasn't changed
        if (qsLastRange[tab] && qsLastRange[tab].start === startRow && qsLastRange[tab].end === endRow) return;
        qsLastRange[tab] = { start: startRow, end: endRow };

        const savedScroll = wrap.scrollTop;
        tbody.innerHTML = "";

        // Top spacer
        if (startRow > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="${colspan}" style="height:${startRow * QS_ROW_HEIGHT}px;padding:0;border:none"></td>`;
            tbody.appendChild(spacer);
        }

        // Visible rows
        for (let i = startRow; i <= endRow; i++) {
            const tr = buildQueueRow(tab, view[i], i, lastIdx, byPosition);
            tbody.appendChild(tr);
        }

        // Bottom spacer
        const bottomSpace = (totalRows - endRow - 1) * QS_ROW_HEIGHT;
        if (bottomSpace > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="${colspan}" style="height:${bottomSpace}px;padding:0;border:none"></td>`;
            tbody.appendChild(spacer);
        }

        wrap.scrollTop = savedScroll;
        applyTruncationTooltips(tbody);
    }

    // --- Queue Batch Actions (context menu) ---

    async function batchRemoveFromQueue(tab) {
        if (selectedQueueIds.size === 0) return;
        const ids = Array.from(selectedQueueIds);

        if (tab === "download") {
            // Download queue: unqueue each file
            let done = 0;
            for (const fid of ids) {
                try { await api("POST", `/api/files/${fid}/queue`, { queued: false }); done++; } catch (e) {}
            }
            addNotification(`Removed ${done} file(s) from download queue`, "info");
            queueStale.download = true;
            loadQueueTab("download");
            refreshQueueCounts();
        } else if (tab === "processing") {
            // Processing queue: cancel selected entries
            const result = await api("POST", "/api/processing/queue/remove", { entry_ids: ids });
            addNotification(`Removed ${result.removed || 0} entry/entries from processing queue`, "info");
            queueStale.processing = true;
            loadQueueTab("processing");
            refreshQueueCounts();
        } else if (tab === "scan") {
            // Scan queue: cancel selected entries
            const result = await api("POST", "/api/scan/queue/remove", { entry_ids: ids });
            addNotification(`Removed ${result.removed || 0} entry/entries from scan queue`, "info");
            queueStale.scan = true;
            loadQueueTab("scan");
            refreshQueueCounts();
        }

        selectedQueueIds.clear();
    }

    async function batchMoveQueue(tab, position) {
        if (selectedQueueIds.size === 0) return;
        const ids = Array.from(selectedQueueIds);
        const targetPos = position === "top" ? 0 : 999999999;

        if (tab === "download") {
            await api("POST", "/api/download/queue/reorder", { file_ids: ids, position: targetPos });
        } else if (tab === "processing") {
            await api("POST", "/api/processing/queue/reorder", { entry_ids: ids, position: targetPos });
        }

        loadQueueTab(tab);
    }

    function moveQueueItem(tab, fromIdx, direction) {
        const view = queueView[tab];
        const toIdx = fromIdx + direction;
        if (toIdx < 0 || toIdx >= view.length) return;
        const src = view[fromIdx];
        const dst = view[toIdx];
        if (tab === "download") {
            const newPos = dst.queue_position;
            api("POST", "/api/download/queue/reorder", { file_id: src.id || src.file_id, position: newPos }).then(() => loadQueueTab(tab));
        } else if (tab === "processing") {
            const newPos = dst.position;
            api("POST", "/api/processing/queue/reorder", { entry_id: src.id, position: newPos }).then(() => loadQueueTab(tab));
        }
    }

    function attachQueueDrag(tr, tab, idx) {
        const grip = tr.querySelector(".file-grip");
        if (!grip) return;

        // Only the grip initiates drag — set draggable on mousedown/mouseup
        grip.addEventListener("mousedown", () => { tr.draggable = true; });
        tr.addEventListener("mouseup", () => { tr.draggable = false; });
        tr.addEventListener("mouseleave", () => { if (queueDragSrcIdx === null) tr.draggable = false; });

        tr.addEventListener("dragstart", (e) => {
            const view = queueView[tab];
            const item = view[idx];
            const dragId = getQueueItemId(tab, item);

            // If dragged item is in selection, drag all selected; otherwise drag only this one
            if (selectedQueueIds.size > 0 && selectedQueueIds.has(dragId)) {
                // Dragging the selection
            } else {
                // Single-item drag: replace selection with just this item
                // Don't re-render here — it would destroy the DOM node mid-drag
                selectedQueueIds.clear();
                selectedQueueIds.add(dragId);
            }

            queueDragSrcIdx = idx;
            tr.classList.add("file-row-dragging");
            e.dataTransfer.effectAllowed = "move";
            // Set drag data (required for Firefox)
            e.dataTransfer.setData("text/plain", String(idx));
        });

        tr.addEventListener("dragend", () => {
            queueDragSrcIdx = null;
            tr.draggable = false;
            tr.classList.remove("file-row-dragging");
            const tbody = tr.parentElement;
            if (tbody) tbody.querySelectorAll(".file-row-drag-over").forEach(r => r.classList.remove("file-row-drag-over"));
        });

        tr.addEventListener("dragover", (e) => {
            if (queueDragSrcIdx === null) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            // Clear other drag-over highlights in tbody
            const tbody = tr.parentElement;
            if (tbody) tbody.querySelectorAll(".file-row-drag-over").forEach(r => { if (r !== tr) r.classList.remove("file-row-drag-over"); });
            tr.classList.add("file-row-drag-over");
        });

        tr.addEventListener("dragleave", (e) => {
            // Only remove if actually leaving this row (not entering a child)
            if (!tr.contains(e.relatedTarget)) tr.classList.remove("file-row-drag-over");
        });

        tr.addEventListener("drop", (e) => {
            e.preventDefault();
            tr.classList.remove("file-row-drag-over");
            if (queueDragSrcIdx === null) return;

            const view = queueView[tab];
            const dropTarget = view[idx];
            queueDragSrcIdx = null;

            // Collect items to move: all selected items, in their current queue-position order
            const idsToMove = selectedQueueIds.size > 0 ? [...selectedQueueIds] : [];
            if (idsToMove.length === 0) return;

            // Sort selected items by their current position so they maintain relative order
            const itemsToMove = view.filter(it => idsToMove.includes(getQueueItemId(tab, it)));
            itemsToMove.sort((a, b) => getQueueItemPos(tab, a) - getQueueItemPos(tab, b));

            const targetPos = getQueueItemPos(tab, dropTarget);

            if (tab === "download") {
                const fileIds = itemsToMove.map(it => it.id || it.file_id);
                api("POST", "/api/download/queue/reorder", { file_ids: fileIds, position: targetPos })
                    .then(() => loadQueueTab(tab));
            } else if (tab === "processing") {
                const entryIds = itemsToMove.map(it => it.id);
                api("POST", "/api/processing/queue/reorder", { entry_ids: entryIds, position: targetPos })
                    .then(() => loadQueueTab(tab));
            }
        });
    }

    function initQueuePage() {
        // Tab switching
        $$(".queue-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchQueueTab(tab.dataset.queue));
        });

        // Download controls
        $("#queue-dl-play").addEventListener("click", async () => {
            await api("POST", "/api/download/start");
        });
        $("#queue-dl-pause").addEventListener("click", async () => {
            await api("POST", "/api/download/pause");
        });
        $("#queue-dl-stop").addEventListener("click", async () => {
            await api("POST", "/api/download/stop");
        });

        // Download clear
        $("#queue-dl-clear").addEventListener("click", () => {
            confirmAction("confirm_clear_queue", "Clear Download Queue",
                "Remove all pending files from the download queue? Active downloads are not affected.",
                async () => {
                    const r = await api("POST", "/api/download/queue/clear");
                    addNotification(`Cleared ${r.cleared || 0} files from download queue`, "info");
                    queueStale.download = true;
                    loadQueueTab("download");
                    refreshQueueCounts();
                    refreshArchives();
                });
        });

        // Processing controls
        $("#queue-proc-pause").addEventListener("click", async () => {
            const paused = !db_processing_paused;
            await api("POST", "/api/processing/pause", { paused });
            db_processing_paused = paused;
            updatePauseButton("queue-proc-pause", paused);
        });
        $("#queue-proc-cancel").addEventListener("click", () => {
            confirmAction("confirm_cancel_processing", "Cancel Processing",
                "Cancel the current file and remove all pending entries from the processing queue?",
                async () => {
                    await api("POST", "/api/processing/cancel");
                    queueStale.processing = true;
                    loadQueueTab("processing");
                    refreshQueueCounts();
                });
        });
        $("#queue-proc-clear").addEventListener("click", () => {
            confirmAction("confirm_clear_queue", "Clear Processing Queue",
                "Remove all pending entries from the processing queue? Active processing is not affected.",
                async () => {
                    const r = await api("POST", "/api/processing/queue/clear");
                    addNotification(`Cleared ${r.cleared || 0} entries from processing queue`, "info");
                    queueStale.processing = true;
                    loadQueueTab("processing");
                    refreshQueueCounts();
                });
        });

        // Scan controls
        $("#queue-scan-pause").addEventListener("click", async () => {
            const paused = !db_scan_paused;
            await api("POST", "/api/scan/pause", { paused });
            db_scan_paused = paused;
            updatePauseButton("queue-scan-pause", paused);
        });
        $("#queue-scan-cancel").addEventListener("click", () => {
            confirmAction("confirm_cancel_scans", "Cancel Scans",
                "Cancel all pending scan queue entries?",
                async () => {
                    await api("POST", "/api/scan/cancel");
                    queueStale.scan = true;
                    loadQueueTab("scan");
                    refreshQueueCounts();
                });
        });
        $("#queue-scan-clear").addEventListener("click", () => {
            confirmAction("confirm_clear_queue", "Clear Scan Queue",
                "Remove all pending entries from the scan queue? Active scanning is not affected.",
                async () => {
                    const r = await api("POST", "/api/scan/queue/clear");
                    addNotification(`Cleared ${r.cleared || 0} entries from scan queue`, "info");
                    queueStale.scan = true;
                    loadQueueTab("scan");
                    refreshQueueCounts();
                });
        });

        // Filter inputs (debounced)
        $$(".queue-filter").forEach(input => {
            const qTab = input.dataset.queue;
            input.addEventListener("input", () => {
                clearTimeout(queueFilterTimers[qTab]);
                queueFilterTimers[qTab] = setTimeout(() => {
                    queueFilterText[qTab] = input.value.trim();
                    renderQueueTable(qTab);
                }, 250);
            });
        });

        // Sort dropdowns
        $$(".queue-sort").forEach(sel => {
            const qTab = sel.dataset.queue;
            sel.addEventListener("change", () => {
                queueSortBy[qTab] = sel.value;
                renderQueueTable(qTab);
            });
        });

        // Virtual scroll for queue table wraps
        ["dl", "proc", "scan"].forEach((abbr, idx) => {
            const qTab = ["download", "processing", "scan"][idx];
            const wrap = $(`#queue-${abbr}-wrap`);
            if (wrap) {
                wrap.addEventListener("scroll", () => {
                    if (qsScrollRAF[qTab]) return;
                    qsScrollRAF[qTab] = requestAnimationFrame(() => {
                        qsScrollRAF[qTab] = null;
                        qsVsRenderVisible(qTab);
                    });
                });
            }
        });

        // Seed badge on page load
        refreshQueueCounts();
    }
    let db_processing_paused = false;
    let db_scan_paused = false;

    // ── Activity Log ──────────────────────────────────────────────
    let activeActivityTab = "ongoing";

    function switchActivityTab(tab) {
        activeActivityTab = tab;
        $$("[data-activity-tab]").forEach(btn => btn.classList.toggle("active", btn.dataset.activityTab === tab));
        $("#activity-panel-ongoing").classList.toggle("active", tab === "ongoing");
        $("#activity-panel-log").classList.toggle("active", tab === "log");
        if (tab === "ongoing") refreshOngoingActivity();
        if (tab === "log") {
            loadActivityLog();
        }
    }

    async function openActivityLog(opts) {
        opts = opts || {};

        // Reset filters unless navigating with specific opts
        if (!opts.job_id && !opts.category && !opts.archive_id) {
            $("#activity-filter-category").value = "";
            $("#activity-filter-level").value = "";
            $("#activity-filter-group").value = "";
            $("#activity-filter-archive").value = "";
            $("#activity-filter-search").value = "";
        }
        if (opts.category) $("#activity-filter-category").value = opts.category;
        if (opts.archive_id) $("#activity-filter-archive").value = String(opts.archive_id);

        // If navigating with a job_id or filters, go straight to the Log tab
        const targetTab = (opts.job_id || opts.category || opts.archive_id) ? "log" : "ongoing";

        await populateActivityArchiveFilter();
        if (targetTab === "log") {
            await loadActivityLog();
        }
        refreshOngoingActivity();
        switchActivityTab(targetTab);
        showPage("page-activity");
    }

    function formatDuration(seconds) {
        if (seconds < 60) return `${Math.round(seconds)}s`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return `${h}h ${m}m`;
    }

    async function populateActivityArchiveFilter() {
        const sel = $("#activity-filter-archive");
        const current = sel.value;
        // Keep first option
        while (sel.options.length > 1) sel.remove(1);
        try {
            const data = await api("GET", "/api/archives");
            const archives = data.archives || data;
            for (const a of archives) {
                const opt = document.createElement("option");
                opt.value = a.id;
                opt.textContent = a.title || a.identifier;
                sel.appendChild(opt);
            }
        } catch (_) {}
        sel.value = current;

        // Also populate group filter
        const gSel = $("#activity-filter-group");
        const gCurrent = gSel.value;
        while (gSel.options.length > 1) gSel.remove(1);
        try {
            const groups = await api("GET", "/api/groups");
            for (const g of (groups || [])) {
                const opt = document.createElement("option");
                opt.value = g.id;
                opt.textContent = g.name;
                gSel.appendChild(opt);
            }
        } catch (_) {}
        gSel.value = gCurrent;
    }

    async function loadActivityLog() {
        const params = new URLSearchParams();
        const cat = $("#activity-filter-category").value;
        const lvl = $("#activity-filter-level").value;
        const grp = $("#activity-filter-group").value;
        const arc = $("#activity-filter-archive").value;
        const srch = $("#activity-filter-search").value.trim();
        if (cat) params.set("category", cat);
        if (lvl) params.set("level", lvl);
        if (grp) params.set("group_id", grp);
        if (arc) params.set("archive_id", arc);
        if (srch) params.set("search", srch);
        params.set("limit", 5000);

        try {
            const data = await api("GET", `/api/activity/log?${params}`);
            renderActivityLog(data.entries);
        } catch (err) {
            alEntries = [];
            $("#activity-log-list").innerHTML = `<div class="activity-log-wrap"><div class="activity-empty">Failed to load activity log</div></div>`;
        }
    }

    function renderActivityLog(entries) {
        const list = $("#activity-log-list");
        if (!entries || entries.length === 0) {
            alEntries = [];
            alLastRange = null;
            list.innerHTML = `<div class="activity-log-wrap"><div class="activity-empty">No activity log entries</div></div>`;
            return;
        }
        // Store entries for virtual scrolling
        alEntries = entries;
        alLastRange = null;

        // Set up the table structure with empty tbody
        list.innerHTML = `<div class="activity-log-wrap"><table class="activity-table">
            <thead><tr>
                <th class="col-time">Time</th>
                <th class="col-category">Category</th>
                <th class="col-level">Level</th>
                <th class="col-message">Message</th>
            </tr></thead>
            <tbody id="activity-log-tbody"></tbody>
        </table></div>`;

        // Scroll to top and render visible rows
        list.querySelector(".activity-log-wrap").scrollTop = 0;
        alVsRenderVisible();
    }

    function buildActivityRow(e) {
        const tr = document.createElement("tr");
        const dt = new Date(e.timestamp * 1000);
        const time = dt.toLocaleString();
        const cat = e.resolved_category || e.category || "";
        const lvl = e.level || "info";
        const hasDetail = !!e.detail;
        const archiveName = e.archive_title || e.archive_identifier || "";

        const tdTime = document.createElement("td");
        tdTime.className = "col-time";
        tdTime.innerHTML = `<span class="entry-time">${esc(time)}</span>`;

        const tdCat = document.createElement("td");
        tdCat.className = "col-category";
        tdCat.innerHTML = `<span class="entry-category">${esc(cat)}</span>`;

        const tdLevel = document.createElement("td");
        tdLevel.className = "col-level";
        tdLevel.innerHTML = `<span class="entry-level level-${lvl}">${esc(lvl)}</span>`;

        // Message cell — CSS handles visual truncation via text-overflow: ellipsis.
        // Full text is in the DOM so the browser measures overflow correctly.
        const tdMsg = document.createElement("td");
        tdMsg.className = "col-message";
        let msgHtml = `<span class="entry-message">${esc(e.message)}`;
        if (archiveName) {
            msgHtml += ` — <span class="entry-archive" data-id="${e.archive_id}">${esc(archiveName)}</span>`;
        }
        msgHtml += `</span>`;
        tdMsg.innerHTML = msgHtml;

        // Archive link click handler
        const archiveEl = tdMsg.querySelector(".entry-archive");
        if (archiveEl) {
            archiveEl.addEventListener("click", (ev) => {
                ev.stopPropagation();
                const id = parseInt(archiveEl.dataset.id);
                if (id) openArchiveDetail(id);
            });
        }

        // Click row to expand/collapse full message + detail.
        // Every row gets the handler; on click we check whether there's
        // actually something worth expanding (overflow or detail text).
        tr.addEventListener("click", (ev) => {
            if (ev.target.closest(".entry-archive")) return;
            const existing = tr.nextElementSibling;
            if (existing && existing.classList.contains("al-detail-row")) {
                existing.remove();
                tr.classList.remove("al-expanded");
                return;
            }
            // Only expand if the message is visually clipped or has detail
            const isOverflowing = tdMsg.scrollWidth > tdMsg.clientWidth;
            if (!isOverflowing && !hasDetail) return;

            const detailTr = document.createElement("tr");
            detailTr.className = "al-detail-row";

            let cellHtml = `<div class="al-detail-wrap">`;
            cellHtml += `<div class="al-detail-label">Full message</div>`;
            cellHtml += `<div class="al-detail-message">${esc(e.message)}`;
            if (archiveName) {
                cellHtml += ` — <span class="entry-archive" data-id="${e.archive_id}">${esc(archiveName)}</span>`;
            }
            cellHtml += `</div>`;
            if (hasDetail) {
                cellHtml += `<div class="al-detail-label">Detail</div>`;
                cellHtml += `<div class="al-detail-extra">${esc(e.detail)}</div>`;
            }
            cellHtml += `</div>`;
            detailTr.innerHTML = `<td colspan="4">${cellHtml}</td>`;

            // Archive link in expanded detail
            detailTr.querySelectorAll(".entry-archive").forEach(el => {
                el.addEventListener("click", (ev2) => {
                    ev2.stopPropagation();
                    const id = parseInt(el.dataset.id);
                    if (id) openArchiveDetail(id);
                });
            });

            tr.after(detailTr);
            tr.classList.add("al-expanded");
        });

        tr.appendChild(tdTime);
        tr.appendChild(tdCat);
        tr.appendChild(tdLevel);
        tr.appendChild(tdMsg);
        return tr;
    }

    function alVsRenderVisible() {
        const wrap = document.querySelector("#activity-log-list .activity-log-wrap");
        const tbody = $("#activity-log-tbody");
        if (!wrap || !tbody || alEntries.length === 0) return;

        const totalRows = alEntries.length;
        const scrollTop = wrap.scrollTop;
        const viewHeight = wrap.clientHeight;

        let startRow = Math.floor(scrollTop / AL_ROW_HEIGHT) - AL_OVERSCAN;
        let endRow = Math.ceil((scrollTop + viewHeight) / AL_ROW_HEIGHT) + AL_OVERSCAN;
        startRow = Math.max(0, startRow);
        endRow = Math.min(totalRows - 1, endRow);

        // Skip re-render if range hasn't changed
        if (alLastRange && alLastRange.start === startRow && alLastRange.end === endRow) return;
        alLastRange = { start: startRow, end: endRow };

        const savedScroll = wrap.scrollTop;
        tbody.innerHTML = "";

        // Top spacer
        if (startRow > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="4" style="height:${startRow * AL_ROW_HEIGHT}px;padding:0;border:none"></td>`;
            tbody.appendChild(spacer);
        }

        // Visible rows
        for (let i = startRow; i <= endRow; i++) {
            tbody.appendChild(buildActivityRow(alEntries[i]));
        }

        // Bottom spacer
        const bottomSpace = (totalRows - endRow - 1) * AL_ROW_HEIGHT;
        if (bottomSpace > 0) {
            const spacer = document.createElement("tr");
            spacer.className = "vs-spacer";
            spacer.innerHTML = `<td colspan="4" style="height:${bottomSpace}px;padding:0;border:none"></td>`;
            tbody.appendChild(spacer);
        }

        wrap.scrollTop = savedScroll;
    }

    function clearActivityFilters() {
        $("#activity-filter-category").value = "";
        $("#activity-filter-level").value = "";
        $("#activity-filter-group").value = "";
        $("#activity-filter-archive").value = "";
        $("#activity-filter-search").value = "";
        loadActivityLog();
    }

    function _refreshActivityIfVisible() {
        if ($("#page-activity").classList.contains("active")) {
            if (activeActivityTab === "log") {
                loadActivityLog();
            } else {
                refreshOngoingActivity();
            }
        }
    }

    function _updateJobProgress(data) {
        const { archive_id, phase } = data;
        if (!archive_id) return;

        // Refresh the Ongoing tab when job progress changes
        refreshOngoingActivity();
    }

    async function openCollections() {
        await refreshCollections();
        renderCollectionList();
        showPage("page-collections");
    }

    function openArchiveList() {
        currentCollectionId = null;
        showPage("page-home");
    }

    function closeCollections() {
        currentCollectionId = null;
        showPage("page-home");
    }

    function renderCollectionList() {
        const listEl = $("#collection-list");
        const emptyEl = $("#collections-empty");
        listEl.innerHTML = "";
        if (collections.length === 0) {
            emptyEl.style.display = "";
            return;
        }
        emptyEl.style.display = "none";

        for (const coll of collections) {
            const card = document.createElement("div");
            card.className = "collection-card";
            card.dataset.id = coll.id;
            const layoutInfo = (coll.layouts || []).map((l) => l.name).join(", ") || "No layouts";
            card.innerHTML = `
                <div class="collection-card-header">
                    <h3 class="collection-card-title">${esc(coll.name)}</h3>
                </div>
                <div class="collection-card-meta">
                    <span>${coll.layout_count} layout${coll.layout_count !== 1 ? "s" : ""}</span>
                </div>
                <div class="collection-card-layouts">${esc(layoutInfo)}</div>
            `;
            card.addEventListener("click", () => openCollectionDetail(coll.id));
            listEl.appendChild(card);
        }
    }

    function esc(str) {
        const d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    async function openCollectionDetail(id) {
        currentCollectionId = id;
        try {
            const coll = await api("GET", `/api/collections/${id}`);
            renderCollectionDetail(coll);
            showPage("page-collection-detail");
            // Load preview asynchronously (don't block detail rendering)
            loadCollectionPreview(id);
        } catch (e) {
            alert("Failed to load collection: " + e.message);
        }
    }

    let currentLayoutId = null;
    let currentCollectionLayouts = [];

    function renderCollectionDetail(coll) {
        $("#collection-detail-title").textContent = coll.name;
        const layoutCount = (coll.layouts || []).length;
        $("#collection-detail-meta").textContent = `${layoutCount} layout${layoutCount !== 1 ? "s" : ""}`;

        currentCollectionLayouts = coll.layouts || [];
        // Build lookup: layout id -> layout object (with segments)
        cpLayoutLookup = {};
        for (const layout of currentCollectionLayouts) {
            cpLayoutLookup[layout.id] = layout;
        }
    }

    function closeCollectionDetail() {
        currentCollectionId = null;
        cpRows = [];
        cpExpandedDirs = new Set();
        cpLastRange = null;
        openCollections();
    }

    // --- Collection Preview ---

    async function loadCollectionPreview(collectionId) {
        const section = $("#collection-preview-section");
        const wrap = $("#collection-preview-wrap");
        const hint = $("#collection-preview-hint");
        const listEl = $("#collection-preview-list");

        try {
            const result = await api("GET", `/api/collections/${collectionId}/preview`);
            cpRows = result.rows || [];
            cpExpandedDirs = new Set();
            cpLastRange = null;

            // Ensure every layout has at least one depth-1 bucket_header row
            // so its edit/delete buttons are always reachable.
            const presentLayoutIds = new Set();
            for (const row of cpRows) {
                if (row.type === "bucket_header" && row.depth === 1) {
                    for (const lid of (row.layout_ids || [])) presentLayoutIds.add(lid);
                }
            }
            for (const layout of currentCollectionLayouts) {
                if (!presentLayoutIds.has(layout.id)) {
                    cpRows.unshift({
                        type: "bucket_header",
                        depth: 1,
                        name: layout.name || "Untitled",
                        path: "__layout_" + layout.id,
                        layout_ids: [layout.id],
                        file_count: 0,
                    });
                }
            }

            if (cpRows.length === 0) {
                section.style.display = "none";
                return;
            }

            section.style.display = "";
            hint.textContent = `${cpRows.filter(r => r.type === "file" || r.type === "dir_unit").length} entries`;

            // Build the flat visible rows (expanding dir_units as needed)
            cpRenderVisible();

            // Attach scroll handler
            wrap.onscroll = () => {
                if (cpScrollRAF) return;
                cpScrollRAF = requestAnimationFrame(() => {
                    cpScrollRAF = null;
                    cpRenderVisible();
                });
            };
        } catch (e) {
            section.style.display = "none";
        }
    }

    function cpLayoutVisualHTML(layoutId) {
        /* Render an inline path-builder-visual string for a layout.
           Reuses pbSegmentLabel() logic for segment display names. */
        const layout = cpLayoutLookup[layoutId];
        if (!layout) return esc("Layout #" + layoutId);
        const segs = layout.segments || [];
        if (segs.length === 0) return `<span class="path-segment-root">/</span><span class="path-segment-wildcard">*</span>`;

        const typeIcons = {
            literal: "\uD83D\uDCC1",
            tag_parent: "\uD83C\uDFF7\uFE0F",
            tag_specific: "\uD83C\uDFF7\uFE0F",
            tag_group: "\uD83C\uDFF7\uFE0F+",
            hidden_filter: "\uD83D\uDD12",
            alphabetical: "Az",
        };
        let html = '<span class="path-segment-root">/</span>';
        for (let i = 0; i < segs.length; i++) {
            const seg = segs[i];
            const typeClass = "seg-" + seg.segment_type.replace(/_/g, "-");
            const label = pbSegmentLabel(seg);
            html += `<span class="path-segment-card ${typeClass}${seg.visible ? "" : " seg-hidden"}">`
                + `<span class="seg-type-icon">${typeIcons[seg.segment_type] || "?"}</span>`
                + `<span class="seg-label">${esc(label)}</span></span>`;
            if (i < segs.length - 1) html += '<span class="path-segment-sep">/</span>';
        }
        html += '<span class="path-segment-sep">/</span><span class="path-segment-wildcard">*</span>';
        return html;
    }

    function cpGetVisibleRows() {
        /* Build the flat row list from the unified folder view.
           - Bucket headers are expandable folders (collapsed by default)
           - Dir_units within expanded buckets also expandable
           - Hidden rows (inside collapsed folders) are omitted
           - Each row carries layout_ids[] for highlighting
        */
        const rows = [];
        let skipDepth = null; // when set, skip rows at this depth or deeper

        for (const row of cpRows) {
            // If we're skipping (inside collapsed folder), check depth
            if (skipDepth !== null && row.depth >= skipDepth) continue;
            skipDepth = null; // past the collapsed section

            if (row.type === "bucket_header") {
                const key = `bucket|${row.path || row.name}`;
                const expanded = cpExpandedDirs.has(key);
                rows.push({ ...row, _key: key, _expanded: expanded });
                if (!expanded) skipDepth = row.depth + 1;
            } else if (row.type === "dir_unit") {
                const key = `dir|${row.display_name}|${row.archive_identifier}`;
                const expanded = cpExpandedDirs.has(key);
                rows.push({ ...row, _key: key, _expanded: expanded });
                if (expanded) {
                    for (const child of (row.children || [])) {
                        rows.push({
                            type: "dir_child",
                            depth: row.depth + 1,
                            display_name: child.name,
                            size: child.size || 0,
                            archive_identifier: row.archive_identifier,
                            is_dir: false,
                            layout_ids: row.layout_ids || [],
                        });
                    }
                }
            } else {
                rows.push({ ...row });
            }
        }
        return rows;
    }

    function cpRenderVisible() {
        const wrap = $("#collection-preview-wrap");
        const listEl = $("#collection-preview-list");
        const allRows = cpGetVisibleRows();
        const totalRows = allRows.length;
        const totalHeight = totalRows * CP_ROW_HEIGHT;

        const scrollTop = wrap.scrollTop;
        const viewHeight = wrap.clientHeight;

        let startRow = Math.floor(scrollTop / CP_ROW_HEIGHT) - CP_OVERSCAN;
        let endRow = Math.ceil((scrollTop + viewHeight) / CP_ROW_HEIGHT) + CP_OVERSCAN;
        startRow = Math.max(0, startRow);
        endRow = Math.min(totalRows - 1, endRow);

        if (cpLastRange && cpLastRange.start === startRow && cpLastRange.end === endRow) return;
        cpLastRange = { start: startRow, end: endRow };

        const savedScroll = wrap.scrollTop;
        listEl.innerHTML = "";
        listEl.style.height = totalHeight + "px";
        listEl.style.position = "relative";

        for (let i = startRow; i <= endRow; i++) {
            const row = allRows[i];
            const div = document.createElement("div");
            div.className = "collection-preview-row";
            div.style.position = "absolute";
            div.style.top = (i * CP_ROW_HEIGHT) + "px";
            div.style.left = "0";
            div.style.right = "0";
            div.style.height = CP_ROW_HEIGHT + "px";
            div.style.paddingLeft = (12 + row.depth * 20) + "px";

            if (row.type === "bucket_header") {
                div.classList.add("bucket-header", "cp-folder");
                const arrow = row._expanded ? "\u25BE" : "\u25B8";
                const lids = row.layout_ids || [];
                const isLayoutRoot = row.depth === 1 && lids.length === 1;

                if (isLayoutRoot) {
                    // Layout root: show path-builder-visual + file count + edit/delete
                    const lid = lids[0];
                    const countStr = row.file_count ? `<span class="cp-layout-count">${row.file_count}</span>` : "";
                    div.classList.add("cp-layout-row");
                    div.innerHTML = `<span class="cp-layout-arrow">${arrow}</span>`
                        + `<span class="preview-name cp-layout-visual">${cpLayoutVisualHTML(lid)}</span>`
                        + countStr
                        + `<button class="cp-layout-btn cp-layout-edit" data-layout-id="${lid}" title="Edit layout">`
                        +   `<svg viewBox="0 0 24 24" width="14" height="14"><path d="M19.14 12.94c.04-.31.06-.63.06-.94 0-.31-.02-.63-.06-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.04.31-.06.63-.06.94 0 .31.02.63.06.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1115.6 12 3.611 3.611 0 0112 15.6z" fill="currentColor"/></svg>`
                        + `</button>`
                        + `<button class="cp-layout-btn cp-layout-delete" data-layout-id="${lid}" title="Delete layout">`
                        +   `<svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" fill="currentColor"/></svg>`
                        + `</button>`;

                    // Wire edit/delete buttons (stop propagation so click doesn't toggle expand)
                    div.querySelector(".cp-layout-edit").addEventListener("click", (e) => {
                        e.stopPropagation();
                        currentLayoutId = lid;
                        openPathBuilder(lid);
                    });
                    div.querySelector(".cp-layout-delete").addEventListener("click", (e) => {
                        e.stopPropagation();
                        confirmAction("confirm_delete_layout", "Delete Layout", "Delete this layout?", async () => {
                            await api("DELETE", `/api/collections/${currentCollectionId}/layouts/${lid}`);
                            currentLayoutId = null;
                            openCollectionDetail(currentCollectionId);
                        }, { confirmText: "Delete" });
                    });
                } else {
                    // Sub-bucket: show folder name + count
                    const countStr = row.file_count ? ` <span style="opacity:0.4;font-weight:400">(${row.file_count})</span>` : "";
                    div.innerHTML = `<span class="preview-name">${arrow} ${esc(row.name)}/${countStr}</span>`;
                }

                div.addEventListener("click", () => {
                    if (cpExpandedDirs.has(row._key)) cpExpandedDirs.delete(row._key);
                    else cpExpandedDirs.add(row._key);
                    cpLastRange = null;
                    cpRenderVisible();
                });
            } else if (row.type === "dir_unit") {
                div.classList.add("cp-folder");
                const arrow = row._expanded ? "\u25BE" : "\u25B8";
                div.innerHTML = `
                    <span class="preview-icon">\uD83D\uDCC1</span>
                    <span class="preview-name" style="cursor:pointer">${arrow} ${esc(row.display_name)}/</span>
                    <span class="preview-archive">${esc(row.archive_identifier)}</span>
                `;
                div.addEventListener("click", () => {
                    if (cpExpandedDirs.has(row._key)) cpExpandedDirs.delete(row._key);
                    else cpExpandedDirs.add(row._key);
                    cpLastRange = null;
                    cpRenderVisible();
                });
            } else if (row.type === "dir_child") {
                div.innerHTML = `
                    <span class="preview-icon" style="opacity:0.3">\uD83D\uDCC4</span>
                    <span class="preview-name">${esc(row.display_name)}</span>
                    <span class="preview-size">${formatBytes(row.size || 0)}</span>
                `;
            } else {
                div.innerHTML = `
                    <span class="preview-icon">\uD83D\uDCC4</span>
                    <span class="preview-name">${esc(row.display_name)}</span>
                    <span class="preview-archive">${esc(row.archive_identifier)}</span>
                    <span class="preview-size">${formatBytes(row.size || 0)}</span>
                `;
            }

            listEl.appendChild(div);
        }

        wrap.scrollTop = savedScroll;
    }

    // --- Collection CRUD Modals ---

    function openCollectionModal(coll = null) {
        editingCollectionId = coll ? coll.id : null;
        $("#modal-collection-title").textContent = coll ? "Edit Collection" : "New Collection";
        $("#collection-name-input").value = coll ? coll.name : "";
        $("#collection-modal-error").textContent = "";
        $("#modal-collection").classList.add("open");
        $("#collection-name-input").focus();
    }

    function closeCollectionModal() {
        $("#modal-collection").classList.remove("open");
        editingCollectionId = null;
    }

    async function saveCollection() {
        const name = $("#collection-name-input").value.trim();
        if (!name) {
            $("#collection-modal-error").textContent = "Name is required.";
            return;
        }
        const body = { name };
        try {
            if (editingCollectionId) {
                await api("PUT", `/api/collections/${editingCollectionId}`, body);
            } else {
                const created = await api("POST", "/api/collections", body);
                editingCollectionId = null;
                closeCollectionModal();
                await refreshCollections();
                renderCollectionList();
                openCollectionDetail(created.id);
                return;
            }
            closeCollectionModal();
            if (currentCollectionId) {
                openCollectionDetail(currentCollectionId);
            } else {
                await refreshCollections();
                renderCollectionList();
            }
        } catch (e) {
            $("#collection-modal-error").textContent = e.message;
        }
    }

    async function deleteCurrentCollection() {
        if (!currentCollectionId) return;
        if (!confirm("Delete this collection and remove all its symlinks?")) return;
        try {
            await api("DELETE", `/api/collections/${currentCollectionId}`);
            currentCollectionId = null;
            await refreshCollections();
            renderCollectionList();
            showPage("page-collections");
        } catch (e) {
            alert("Failed to delete: " + e.message);
        }
    }

    // --- Layout Modal ---

    async function addLayoutDirect() {
        if (!currentCollectionId) return;
        try {
            const nextNum = (currentCollectionLayouts.length || 0) + 1;
            const newLayout = await api("POST", `/api/collections/${currentCollectionId}/layouts`, {
                name: `Layout ${nextNum}`,
            });
            await openCollectionDetail(currentCollectionId);
            currentLayoutId = newLayout.id;
            openPathBuilder(newLayout.id);
        } catch (e) {
            alert("Failed to add layout: " + e.message);
        }
    }

    // --- Layout Editor ---

    let leAvailableTags = []; // cached tag list for editor

    async function openLayoutEditor(layoutId) {
        const layout = currentCollectionLayouts.find(l => l.id === layoutId);
        if (!layout) return;
        $("#layout-editor-title").textContent = layout.name;
        $("#modal-layout-editor").classList.add("open");
        // Load available tags for the tag dropdown
        try { leAvailableTags = await api("GET", "/api/tags"); } catch (e) { leAvailableTags = []; }
        populateLeTagDropdown();
        setupLeTypeToggle();
        await refreshLayoutEditorTree(layoutId);
    }

    function populateLeTagDropdown() {
        initTagPicker($("#le-tag-input"), $("#le-tag-suggestions"), leAvailableTags, null);
    }

    function setupLeTypeToggle() {
        const typeSel = $("#le-add-type");
        const tagPicker = $("#le-tag-picker");
        typeSel.onchange = () => {
            const v = typeSel.value;
            tagPicker.style.display = (v === "tag_parent" || v === "tag_value") ? "" : "none";
            if (tagPicker.style.display === "none") $("#le-tag-input").value = "";
        };
        tagPicker.style.display = "none";
    }

    async function refreshLayoutEditorTree(layoutId) {
        const treeEl = $("#layout-editor-tree");
        try {
            const nodes = await api("GET", `/api/layouts/${layoutId}/nodes`);
            treeEl.innerHTML = "";
            if (nodes.length === 0) {
                treeEl.innerHTML = '<p class="empty-hint">No nodes. Add a folder to get started.</p>';
                return;
            }
            renderEditorNodes(treeEl, nodes, layoutId, 0);
        } catch (e) {
            treeEl.innerHTML = `<p class="empty-hint">Error loading nodes: ${esc(e.message)}</p>`;
        }
    }

    function renderEditorNodes(container, nodes, layoutId, depth) {
        for (const node of nodes) {
            const div = document.createElement("div");
            div.className = "layout-node-item";
            div.style.paddingLeft = `${8 + depth * 20}px`;
            const typeLabels = {
                all: "All Files",
                alphabetical: "A\u2013Z",
                tag_parent: `By Tag: ${node.tag_filter || "?"}`,
                tag_value: `Tag: ${node.tag_filter || "?"}`,
                custom: "Custom",
            };
            const sortLabel = node.sort_mode === "alphabetical" ? "A\u2013Z" : "flat";
            const canAddChild = node.type === "custom";
            div.innerHTML = `
                <span class="node-icon">\uD83D\uDCC1</span>
                <span class="node-name" data-node-id="${node.id}" title="Double-click to rename">${esc(node.name)}</span>
                <span class="node-type-badge">${typeLabels[node.type] || node.type}</span>
                <button class="node-sort-toggle" data-node-id="${node.id}" data-sort="${node.sort_mode}" title="Toggle sort mode">${sortLabel}</button>
                ${canAddChild ? `<span class="node-add-child" data-parent-id="${node.id}" title="Add sub-folder">+sub</span>` : ""}
                <div class="node-actions">
                    <button class="action-btn action-btn-sm batch-btn-danger" data-delete-node="${node.id}" title="Delete">&times;</button>
                </div>
            `;
            // Inline rename on double-click
            const nameEl = div.querySelector(`.node-name[data-node-id="${node.id}"]`);
            nameEl.addEventListener("dblclick", () => {
                const input = document.createElement("input");
                input.type = "text";
                input.className = "node-name-input";
                input.value = node.name;
                nameEl.replaceWith(input);
                input.focus();
                input.select();
                const finish = async () => {
                    const newName = input.value.trim();
                    if (newName && newName !== node.name) {
                        await api("PATCH", `/api/layouts/nodes/${node.id}`, { name: newName });
                    }
                    refreshLayoutEditorTree(layoutId);
                };
                input.addEventListener("blur", finish);
                input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = node.name; input.blur(); } });
            });
            // Sort toggle
            div.querySelector(`.node-sort-toggle[data-node-id="${node.id}"]`).addEventListener("click", async () => {
                const newSort = node.sort_mode === "flat" ? "alphabetical" : "flat";
                await api("PATCH", `/api/layouts/nodes/${node.id}`, { sort_mode: newSort });
                refreshLayoutEditorTree(layoutId);
            });
            // Add child (for custom nodes)
            const addChildBtn = div.querySelector(`[data-parent-id="${node.id}"]`);
            if (addChildBtn) {
                addChildBtn.addEventListener("click", () => {
                    leAddNode(layoutId, node.id);
                });
            }
            // Delete
            div.querySelector(`[data-delete-node="${node.id}"]`).addEventListener("click", async () => {
                if (!confirm(`Delete "${node.name}"?`)) return;
                await api("DELETE", `/api/layouts/nodes/${node.id}`);
                refreshLayoutEditorTree(layoutId);
            });
            container.appendChild(div);
            if (node.children && node.children.length > 0) {
                renderEditorNodes(container, node.children, layoutId, depth + 1);
            }
        }
    }

    async function leAddNode(layoutId, parentId) {
        const typeSel = $("#le-add-type");
        const tagInput = $("#le-tag-input");
        const nameInput = $("#le-add-name");
        const nodeType = typeSel.value;
        let tagFilter = null;
        let name = nameInput.value.trim();

        if (nodeType === "tag_parent" || nodeType === "tag_value") {
            tagFilter = tagInput.value.trim();
            if (!tagFilter) { addNotification("Select a tag first", "warning"); return; }
            if (!name) name = tagFilter;
        }
        if (!name) {
            const defaults = { all: "All Files", alphabetical: "A-Z", custom: "Custom" };
            name = defaults[nodeType] || "Folder";
        }
        try {
            await api("POST", `/api/layouts/${layoutId}/nodes`, {
                name, type: nodeType, parent_id: parentId || null, tag_filter: tagFilter,
            });
            nameInput.value = "";
            refreshLayoutEditorTree(layoutId);
        } catch (e) {
            addNotification("Failed to add node: " + e.message, "error");
        }
    }

    // --- Path Builder (new segment-based layout editor) ---

    let pbLayoutId = null;
    let pbSegments = [];

    async function openPathBuilder(layoutId) {
        const layout = currentCollectionLayouts.find(l => l.id === layoutId);
        if (!layout) return;
        pbLayoutId = layoutId;
        $("#modal-path-builder").classList.add("open");

        // Populate layout options
        const flattenEl = $("#pb-flatten-input");
        const mediaEl = $("#pb-media-units-input");
        flattenEl.checked = layout.flatten !== 0;
        mediaEl.checked = layout.use_media_units !== 0;

        // Save on toggle
        const saveOption = async () => {
            if (!currentCollectionId) return;
            await api("PUT", `/api/collections/${currentCollectionId}/layouts/${layoutId}`, {
                flatten: flattenEl.checked ? 1 : 0,
                use_media_units: mediaEl.checked ? 1 : 0,
            });
        };
        flattenEl.onchange = saveOption;
        mediaEl.onchange = saveOption;

        // Load available tags
        try { leAvailableTags = await api("GET", "/api/tags"); } catch (e) { leAvailableTags = []; }
        pbSetupAddBar();
        await pbRefreshSegments();
    }

    // --- Reusable Tag Picker (autocomplete text input) ---

    function initTagPicker(inputEl, suggestionsEl, tags, onSelect) {
        // Build sorted list: parents first, then all tags sorted by count desc
        const parents = new Set();
        for (const t of tags) {
            const idx = t.tag.indexOf(":");
            if (idx > 0) parents.add(t.tag.substring(0, idx));
        }
        const parentList = [...parents].sort().map(p => ({ tag: p, count: tags.filter(t => t.tag.startsWith(p + ":")).reduce((s, t) => s + t.count, 0), isParent: true }));
        const allTags = [...tags].sort((a, b) => b.count - a.count);
        const combined = [...parentList, ...allTags];

        let activeIdx = -1;
        let filtered = [];

        function render(query) {
            query = (query || "").toLowerCase();
            filtered = query
                ? combined.filter(t => t.tag.toLowerCase().includes(query))
                : combined.slice(0, 15);
            suggestionsEl.innerHTML = "";
            if (filtered.length === 0) {
                suggestionsEl.classList.remove("visible");
                return;
            }
            filtered.forEach((t, i) => {
                const div = document.createElement("div");
                div.className = "tag-suggestion" + (i === activeIdx ? " active" : "");
                const label = document.createElement("span");
                label.textContent = t.tag + (t.isParent ? ":*" : "");
                if (t.isParent) {
                    const badge = document.createElement("span");
                    badge.className = "tag-parent-badge";
                    badge.textContent = "(parent)";
                    label.appendChild(badge);
                }
                const count = document.createElement("span");
                count.className = "tag-count";
                count.textContent = t.count;
                div.appendChild(label);
                div.appendChild(count);
                div.addEventListener("mousedown", (e) => {
                    e.preventDefault(); // keep focus on input
                    inputEl.value = t.tag + (t.isParent ? "" : "");
                    suggestionsEl.classList.remove("visible");
                    activeIdx = -1;
                    if (onSelect) onSelect(t);
                });
                suggestionsEl.appendChild(div);
            });
            suggestionsEl.classList.add("visible");
        }

        inputEl.addEventListener("input", () => { activeIdx = -1; render(inputEl.value); });
        inputEl.addEventListener("focus", () => render(inputEl.value));
        inputEl.addEventListener("blur", () => {
            // Delay to allow mousedown on suggestion
            setTimeout(() => suggestionsEl.classList.remove("visible"), 150);
        });
        inputEl.addEventListener("keydown", (e) => {
            if (!suggestionsEl.classList.contains("visible")) return;
            if (e.key === "ArrowDown") {
                e.preventDefault();
                activeIdx = Math.min(activeIdx + 1, filtered.length - 1);
                render(inputEl.value);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                activeIdx = Math.max(activeIdx - 1, 0);
                render(inputEl.value);
            } else if (e.key === "Tab" || e.key === "Enter") {
                if (activeIdx >= 0 && activeIdx < filtered.length) {
                    e.preventDefault();
                    const t = filtered[activeIdx];
                    inputEl.value = t.tag;
                    suggestionsEl.classList.remove("visible");
                    activeIdx = -1;
                    if (onSelect) onSelect(t);
                } else if (filtered.length > 0) {
                    e.preventDefault();
                    const t = filtered[0];
                    inputEl.value = t.tag;
                    suggestionsEl.classList.remove("visible");
                    activeIdx = -1;
                    if (onSelect) onSelect(t);
                }
            } else if (e.key === "Escape") {
                suggestionsEl.classList.remove("visible");
                activeIdx = -1;
            }
        });

        // Initial render (hidden until focus)
        activeIdx = -1;
    }

    function pbSetupAddBar() {
        const typeSel = $("#pb-add-type");
        const tagPicker = $("#pb-tag-picker");
        const tagInput = $("#pb-tag-input");
        const tagSuggestions = $("#pb-tag-suggestions");
        const valInput = $("#pb-add-value");
        const addBtn = $("#btn-pb-add-segment");

        // Init tag picker
        initTagPicker(tagInput, tagSuggestions, leAvailableTags, null);

        typeSel.onchange = () => {
            const v = typeSel.value;
            const needsTag = (v === "tag_parent" || v === "tag_specific" || v === "tag_group" || v === "hidden_filter");
            tagPicker.style.display = needsTag ? "" : "none";
            valInput.style.display = (v === "literal" || v === "tag_group" || v === "hidden_filter") ? "" : "none";
            addBtn.style.display = v ? "" : "none";
            if (v === "literal") { valInput.placeholder = "Folder name"; tagPicker.style.display = "none"; }
            else if (v === "tag_group" || v === "hidden_filter") { valInput.placeholder = "Tags (e.g. beta+proto)"; }
            else { valInput.placeholder = "Value"; }
            tagInput.value = "";
            tagSuggestions.classList.remove("visible");
        };
        typeSel.value = "";
        tagPicker.style.display = "none";
        valInput.style.display = "none";
        addBtn.style.display = "none";

        addBtn.onclick = () => pbAddSegment();
    }

    async function pbAddSegment() {
        const typeSel = $("#pb-add-type");
        const tagInput = $("#pb-tag-input");
        const valInput = $("#pb-add-value");
        const stype = typeSel.value;
        if (!stype) return;

        let sval = null;
        let visible = true;

        if (stype === "literal") {
            sval = valInput.value.trim();
            if (!sval) { addNotification("Enter a folder name", "warning"); return; }
        } else if (stype === "tag_parent" || stype === "tag_specific") {
            sval = tagInput.value.trim();
            if (!sval) { addNotification("Select a tag", "warning"); return; }
        } else if (stype === "tag_group") {
            sval = valInput.value.trim() || tagInput.value.trim();
            if (!sval) { addNotification("Enter tags separated by +", "warning"); return; }
        } else if (stype === "hidden_filter") {
            sval = valInput.value.trim() || tagInput.value.trim();
            if (!sval) { addNotification("Enter tags to filter by", "warning"); return; }
            visible = false;
        } else if (stype === "alphabetical") {
            sval = "A-Z";
        }

        try {
            await api("POST", `/api/layouts/${pbLayoutId}/segments`, {
                segment_type: stype,
                segment_value: sval,
                visible: visible,
            });
            // Reset inputs
            typeSel.value = "";
            $("#pb-tag-picker").style.display = "none";
            tagInput.value = "";
            valInput.style.display = "none";
            valInput.value = "";
            $("#btn-pb-add-segment").style.display = "none";
            await pbRefreshSegments();
        } catch (e) {
            addNotification("Failed to add segment: " + e.message, "error");
        }
    }

    async function pbRefreshSegments() {
        if (!pbLayoutId) return;
        try {
            pbSegments = await api("GET", `/api/layouts/${pbLayoutId}/segments`);
        } catch (e) {
            pbSegments = [];
        }
        pbRenderVisual();
        pbRenderSegmentList();
    }

    function pbRenderVisual() {
        const el = $("#path-builder-visual");
        el.innerHTML = "";
        el.appendChild(Object.assign(document.createElement("span"), { className: "path-segment-root", textContent: "/" }));

        for (let i = 0; i < pbSegments.length; i++) {
            const seg = pbSegments[i];
            const card = document.createElement("span");
            const typeClass = "seg-" + seg.segment_type.replace(/_/g, "-");
            card.className = `path-segment-card ${typeClass}${seg.visible ? "" : " seg-hidden"}`;

            const typeIcons = {
                literal: "\uD83D\uDCC1",
                tag_parent: "\uD83C\uDFF7\uFE0F",
                tag_specific: "\uD83C\uDFF7\uFE0F",
                tag_group: "\uD83C\uDFF7\uFE0F+",
                hidden_filter: "\uD83D\uDD12",
                alphabetical: "Az",
            };
            card.innerHTML = `<span class="seg-type-icon">${typeIcons[seg.segment_type] || "?"}</span>` +
                `<span class="seg-label">${esc(pbSegmentLabel(seg))}</span>` +
                `<button class="seg-remove" data-seg-id="${seg.id}" title="Remove">&times;</button>`;

            card.querySelector(".seg-remove").addEventListener("click", async (e) => {
                e.stopPropagation();
                await api("DELETE", `/api/layouts/segments/${seg.id}`);
                await pbRefreshSegments();
            });

            // Click to toggle visibility
            card.addEventListener("click", async () => {
                await api("PATCH", `/api/layouts/segments/${seg.id}`, { visible: seg.visible ? 0 : 1 });
                await pbRefreshSegments();
            });

            el.appendChild(card);

            if (i < pbSegments.length - 1) {
                el.appendChild(Object.assign(document.createElement("span"), { className: "path-segment-sep", textContent: "/" }));
            }
        }

        el.appendChild(Object.assign(document.createElement("span"), { className: "path-segment-sep", textContent: "/" }));
        el.appendChild(Object.assign(document.createElement("span"), { className: "path-segment-wildcard", textContent: "*" }));
    }

    function pbSegmentLabel(seg) {
        switch (seg.segment_type) {
            case "literal": return seg.segment_value || "folder";
            case "tag_parent": return (seg.segment_value || "?") + ":*";
            case "tag_specific": {
                const v = seg.segment_value || "";
                return v.includes(":") ? v.split(":")[1] : v;
            }
            case "tag_group": return seg.segment_value || "group";
            case "hidden_filter": return seg.segment_value || "filter";
            case "alphabetical": return "A-Z";
            default: return seg.segment_type;
        }
    }

    function pbRenderSegmentList() {
        const el = $("#path-builder-segments");
        el.innerHTML = "";
        if (pbSegments.length === 0) {
            el.innerHTML = '<p class="empty-hint">No segments. Add a segment to build your path.</p>';
            return;
        }
        for (let i = 0; i < pbSegments.length; i++) {
            const seg = pbSegments[i];
            const div = document.createElement("div");
            div.className = "path-segment-detail";

            const typeLabels = {
                literal: "LITERAL",
                tag_parent: "PARENT",
                tag_specific: "TAG",
                tag_group: "GROUP",
                hidden_filter: "HIDDEN",
                alphabetical: "A-Z",
            };

            const visIcon = seg.visible ? "\uD83D\uDC41" : "\uD83D\uDC41\u200D\uD83D\uDDE8";
            const visTitle = seg.visible ? "Visible on disk (click to hide)" : "Hidden filter (click to make visible)";

            div.innerHTML = `
                <span class="seg-pos">${i + 1}</span>
                <span class="seg-type-badge">${typeLabels[seg.segment_type] || seg.segment_type}</span>
                <span class="seg-value">${esc(seg.segment_value || "(none)")}</span>
                <span class="seg-visibility ${seg.visible ? "seg-visible" : ""}" data-seg-id="${seg.id}" title="${visTitle}">${seg.visible ? "\uD83D\uDC41" : "\uD83D\uDE48"}</span>
                <span class="seg-actions">
                    ${i > 0 ? `<button class="seg-action-btn" data-move-up="${seg.id}" title="Move up">\u2191</button>` : ""}
                    ${i < pbSegments.length - 1 ? `<button class="seg-action-btn" data-move-down="${seg.id}" title="Move down">\u2193</button>` : ""}
                    <button class="seg-action-btn seg-btn-danger" data-del-seg="${seg.id}" title="Delete">\u00D7</button>
                </span>
            `;

            // Visibility toggle
            div.querySelector(".seg-visibility").addEventListener("click", async () => {
                await api("PATCH", `/api/layouts/segments/${seg.id}`, { visible: seg.visible ? 0 : 1 });
                await pbRefreshSegments();
            });

            // Move up/down
            const upBtn = div.querySelector(`[data-move-up="${seg.id}"]`);
            if (upBtn) upBtn.addEventListener("click", async () => {
                const ids = pbSegments.map(s => s.id);
                ids.splice(i, 1);
                ids.splice(i - 1, 0, seg.id);
                await api("POST", `/api/layouts/${pbLayoutId}/segments/reorder`, { segment_ids: ids });
                await pbRefreshSegments();
            });
            const downBtn = div.querySelector(`[data-move-down="${seg.id}"]`);
            if (downBtn) downBtn.addEventListener("click", async () => {
                const ids = pbSegments.map(s => s.id);
                ids.splice(i, 1);
                ids.splice(i + 1, 0, seg.id);
                await api("POST", `/api/layouts/${pbLayoutId}/segments/reorder`, { segment_ids: ids });
                await pbRefreshSegments();
            });

            // Delete
            div.querySelector(`[data-del-seg="${seg.id}"]`).addEventListener("click", async () => {
                await api("DELETE", `/api/layouts/segments/${seg.id}`);
                await pbRefreshSegments();
            });

            el.appendChild(div);
        }
    }

    // --- Sync ---

    async function syncCurrentCollection() {
        if (!currentCollectionId) return;
        const btn = $("#btn-sync-collection");
        btn.disabled = true;
        btn.textContent = "Syncing\u2026";
        try {
            const stats = await api("POST", `/api/collections/${currentCollectionId}/sync`);
            let msg = `Sync complete: ${stats.total_created} created, ${stats.total_removed} removed`;
            if (stats.total_errors > 0) msg += `, ${stats.total_errors} errors`;
            // Show sync status
            const statusEl = $("#collection-sync-status");
            const detailsEl = $("#collection-sync-details");
            statusEl.style.display = "";
            let html = `<p>${esc(msg)}</p>`;
            for (const [layoutName, ls] of Object.entries(stats.layouts || {})) {
                html += `<div class="sync-layout-stat"><strong>${esc(layoutName)}</strong>: ${ls.created} created, ${ls.removed} removed, ${ls.unchanged} unchanged`;
                if (ls.conflicts > 0) html += `, ${ls.conflicts} conflicts`;
                if (ls.errors.length > 0) html += `<br><span class="sync-errors">${ls.errors.map(esc).join("<br>")}</span>`;
                html += `</div>`;
            }
            detailsEl.innerHTML = html;
        } catch (e) {
            alert("Sync failed: " + e.message);
        } finally {
            btn.disabled = false;
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0020 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 004 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z" fill="currentColor"/></svg> Sync`;
        }
    }

    // --- Init ---

    function init() {
        // Controls
        $("#btn-play").addEventListener("click", () => {
            if (dlState === "paused") {
                // Clear the limiter when resuming from pause
                realBandwidth = -1;
                api("POST", "/api/download/bandwidth", { limit: -1 });
            }
            api("POST", "/api/download/start").then((r) => {
                if (!r.has_work) {
                    addNotification("Nothing queued for download", "warning");
                }
            });
        });
        $("#btn-pause").addEventListener("click", () => api("POST", "/api/download/pause"));
        $("#btn-stop").addEventListener("click", () => api("POST", "/api/download/stop"));

        // Add
        $("#btn-add").addEventListener("click", openAddModal);
        $("#btn-add-cancel").addEventListener("click", closeAddModal);
        $("#btn-add-confirm").addEventListener("click", addArchive);
        $("#input-add-url").addEventListener("keydown", (e) => { if (e.key === "Enter") addArchive(); });
        $("#add-batch-mode").addEventListener("change", (e) => toggleBatchMode(e.target.checked));

        // Settings
        $("#btn-settings").addEventListener("click", openSettings);
        $("#btn-settings-save-bottom").addEventListener("click", saveSettings);
        $("#btn-settings-back-bottom").addEventListener("click", closeSettings);
        $("#btn-test-credentials").addEventListener("click", testCredentials);
        $("#btn-change-password").addEventListener("click", changePassword);
        // Tab switching
        $$(".settings-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchTab(tab.dataset.tab));
        });
        // Schedule
        $("#btn-add-schedule-rule").addEventListener("click", addScheduleRule);

        // Dirty tracking for settings: listen on all settings inputs
        $("#page-settings").querySelectorAll("input, select").forEach((el) => {
            el.addEventListener("input", checkSettingsDirty);
            el.addEventListener("change", checkSettingsDirty);
        });

        // Toggle HTTP warning visibility
        $("#set-use-http").addEventListener("change", () => {
            $("#http-warning").style.display = $("#set-use-http").checked ? "block" : "none";
        });

        // Logout
        $("#btn-logout").addEventListener("click", async () => {
            await fetch("/logout", { method: "POST" });
            window.location.href = "/login";
        });

        // Delete
        $("#btn-delete-cancel").addEventListener("click", () => { deleteTarget = null; $("#modal-delete").classList.remove("open"); });
        $("#btn-delete-confirm").addEventListener("click", doDelete);

        // Notifications
        $("#btn-notifications").addEventListener("click", (e) => {
            e.stopPropagation();
            toggleNotifPopup();
        });
        $("#notif-popup").addEventListener("click", (e) => e.stopPropagation());
        $("#btn-notif-clear-all").addEventListener("click", clearAllNotifications);
        document.addEventListener("click", () => {
            $("#notif-popup").classList.remove("open");
        });

        // Navigation
        $("#btn-archives").addEventListener("click", openArchiveList);
        // Queues
        $("#btn-queues").addEventListener("click", () => openQueues());
        initQueuePage();
        // Activities
        $("#btn-activity").addEventListener("click", () => openActivityLog());
        $$("#activity-tabs [data-activity-tab]").forEach(btn => {
            btn.addEventListener("click", () => switchActivityTab(btn.dataset.activityTab));
        });
        $("#activity-filter-apply").addEventListener("click", () => loadActivityLog());
        $("#activity-filter-clear").addEventListener("click", clearActivityFilters);
        $("#activity-filter-search").addEventListener("keydown", (e) => { if (e.key === "Enter") loadActivityLog(); });
        // Collections
        $("#btn-collections").addEventListener("click", openCollections);
        $("#btn-create-collection").addEventListener("click", () => openCollectionModal());
        $("#btn-collection-modal-cancel").addEventListener("click", closeCollectionModal);
        $("#btn-collection-modal-save").addEventListener("click", saveCollection);
        $("#collection-name-input").addEventListener("keydown", (e) => { if (e.key === "Enter") saveCollection(); });
        $("#btn-collection-detail-back").addEventListener("click", closeCollectionDetail);
        $("#btn-sync-collection").addEventListener("click", syncCurrentCollection);
        $("#btn-edit-collection").addEventListener("click", async () => {
            if (!currentCollectionId) return;
            const coll = await api("GET", `/api/collections/${currentCollectionId}`);
            openCollectionModal(coll);
        });
        $("#btn-add-layout").addEventListener("click", addLayoutDirect);
        $("#btn-delete-collection").addEventListener("click", deleteCurrentCollection);
        // Layout editor (legacy)
        $("#btn-layout-editor-close").addEventListener("click", () => {
            $("#modal-layout-editor").classList.remove("open");
            if (currentCollectionId) openCollectionDetail(currentCollectionId);
        });
        // Path builder (new)
        $("#btn-path-builder-close").addEventListener("click", () => {
            $("#modal-path-builder").classList.remove("open");
            if (currentCollectionId) openCollectionDetail(currentCollectionId);
        });
        $("#btn-layout-editor-add-node").addEventListener("click", () => {
            if (!currentLayoutId) return;
            leAddNode(currentLayoutId, null);
        });
        // Tags
        $("#archive-tag-input").addEventListener("keydown", async (e) => {
            if (e.key === "Enter" && currentArchiveId) {
                const tag = e.target.value.trim();
                if (!tag) return;
                await api("POST", `/api/archives/${currentArchiveId}/tags`, { tag });
                e.target.value = "";
                loadArchiveTagsAndCollections(currentArchiveId);
            }
        });

        // Archive controls
        $("#btn-retry-all-archives").addEventListener("click", retryAllArchives);
        $("#btn-refresh-all-meta").addEventListener("click", refreshAllMetadata);
        $("#btn-scan-all-archives").addEventListener("click", scanAllArchives);

        // Archive toolbar (search + sort)
        $("#archive-search").addEventListener("input", (e) => {
            clearTimeout(archiveSearchTimer);
            archiveSearchTimer = setTimeout(() => {
                archiveSearchQuery = e.target.value;
                renderArchiveList();
                archiveListWrap.scrollTop = 0;
            }, 200);
        });
        $("#archive-sort").addEventListener("change", (e) => {
            archiveSort = e.target.value;
            renderArchiveList();
            archiveListWrap.scrollTop = 0;
        });

        // Groups
        $("#btn-add-group").addEventListener("click", openCreateGroup);
        $("#btn-group-create-cancel").addEventListener("click", () => $("#modal-create-group").classList.remove("open"));
        $("#btn-group-create-confirm").addEventListener("click", doCreateGroup);
        $("#input-group-name").addEventListener("keydown", (e) => { if (e.key === "Enter") doCreateGroup(); });
        $("#btn-group-rename-cancel").addEventListener("click", () => { pendingRenameGroup = null; $("#modal-rename-group").classList.remove("open"); });
        $("#btn-group-rename-confirm").addEventListener("click", doRenameGroup);
        $("#input-group-rename").addEventListener("keydown", (e) => { if (e.key === "Enter") doRenameGroup(); });
        $("#btn-group-delete-cancel").addEventListener("click", () => { pendingDeleteGroup = null; $("#modal-delete-group").classList.remove("open"); });
        $("#btn-group-delete-confirm").addEventListener("click", doDeleteGroup);
        $("#btn-move-group-cancel").addEventListener("click", () => { pendingGroupArchive = null; $("#modal-move-to-group").classList.remove("open"); });

        // Force resume conflict modal
        $("#btn-force-resume-cancel").addEventListener("click", () => { pendingForceResumeId = null; $("#modal-force-resume").classList.remove("open"); });
        $("#btn-force-resume-confirm").addEventListener("click", doForceResume);

        // Virtual scroll for file table
        $(".file-table-wrap").addEventListener("scroll", () => {
            if (vsScrollRAF) return;
            vsScrollRAF = requestAnimationFrame(() => {
                vsScrollRAF = null;
                vsRenderVisible();
            });
        });

        // Virtual scroll for activity log table
        $("#activity-log-list").addEventListener("scroll", (ev) => {
            if (ev.target.classList.contains("activity-log-wrap")) {
                if (alScrollRAF) return;
                alScrollRAF = requestAnimationFrame(() => {
                    alScrollRAF = null;
                    alVsRenderVisible();
                });
            }
        }, true);

        // Detail
        $("#btn-back").addEventListener("click", closeDetail);
        $("#btn-retry-all").addEventListener("click", () => { if (currentArchiveId) retryArchive(currentArchiveId); });
        $("#btn-refresh-meta").addEventListener("click", refreshMetadata);
        $("#btn-scan-files").addEventListener("click", scanExistingFiles);
        $("#btn-process-all-files").addEventListener("click", openProcessArchiveModal);
        $("#btn-clear-changes").addEventListener("click", clearChanges);

        // Auto-process controls in detail-controls
        $("#auto-process-toggle").addEventListener("change", async () => {
            if (!currentArchiveId) return;
            const checked = $("#auto-process-toggle").checked;
            const select = $("#auto-process-profile");
            if (checked) {
                select.style.display = "";
                const profileId = parseInt(select.value);
                if (profileId) {
                    await api("POST", `/api/archives/${currentArchiveId}/auto-process`, { profile_id: profileId });
                }
            } else {
                select.style.display = "none";
                await api("POST", `/api/archives/${currentArchiveId}/auto-process`, { profile_id: null });
            }
        });
        $("#auto-process-profile").addEventListener("change", async () => {
            if (!currentArchiveId || !$("#auto-process-toggle").checked) return;
            const profileId = parseInt($("#auto-process-profile").value);
            if (profileId) {
                await api("POST", `/api/archives/${currentArchiveId}/auto-process`, { profile_id: profileId });
            }
        });

        // Process Archive modal
        $("#btn-process-cancel").addEventListener("click", () => $("#modal-process-archive").classList.remove("open"));
        $("#btn-process-confirm").addEventListener("click", confirmProcessArchive);

        // Edit Profile modal
        $("#btn-add-profile").addEventListener("click", () => openEditProfile(null));
        $("#btn-edit-profile-cancel").addEventListener("click", () => $("#modal-edit-profile").classList.remove("open"));
        $("#btn-edit-profile-save").addEventListener("click", saveProfile);

        // Tool detection
        if ($("#btn-detect-tools")) {
            $("#btn-detect-tools").addEventListener("click", detectAndShowTools);
        }
        $("#file-sort").addEventListener("change", (e) => {
            currentSort = e.target.value;
            currentSortDir = "";
            $(".file-table-wrap").scrollTop = 0;
            loadFiles();
            updateSortArrows();
        });
        $("#file-search").addEventListener("input", (e) => {
            clearTimeout(fileSearchTimer);
            fileSearchTimer = setTimeout(() => {
                fileSearchQuery = e.target.value.trim();
                $(".file-table-wrap").scrollTop = 0;
                loadFiles();
            }, 250);
        });

        // Bandwidth
        $("#bandwidth-enabled").addEventListener("change", onBandwidthToggle);
        $("#bandwidth-input").addEventListener("input", onBandwidthInput);

        // Close modals on overlay click
        $$(".modal-overlay").forEach((overlay) => {
            overlay.addEventListener("click", (e) => {
                if (e.target === overlay) overlay.classList.remove("open");
            });
        });

        // Keyboard shortcuts: Escape to deselect/close, Ctrl+A to select all
        document.addEventListener("keydown", (e) => {
            // Ctrl+A / Cmd+A: select all (files or archives depending on active page)
            if ((e.ctrlKey || e.metaKey) && e.key === "a" && !e.target.closest("input, textarea, select")) {
                e.preventDefault();
                if (pageDetail.classList.contains("active")) {
                    selectAllFiles();
                } else if (pageHome.classList.contains("active")) {
                    const visible = getVisibleArchives();
                    selectedArchiveIds.clear();
                    visible.forEach(a => selectedArchiveIds.add(a.id));
                    updateArchiveBatchActions();
                }
                return;
            }
            if (e.key === "Escape") {
                // First: clear selection if any
                if (selectedFileIds.size > 0) {
                    deselectAllFiles();
                    return;
                }
                if (selectedArchiveIds.size > 0) {
                    selectedArchiveIds.clear();
                    updateArchiveBatchActions();
                    return;
                }
                // Then: close modals / settings / detail
                $$(".modal-overlay.open").forEach((m) => m.classList.remove("open"));
                if ($("#page-settings").classList.contains("active")) {
                    closeSettings();
                } else if ($("#page-collection-detail").classList.contains("active")) {
                    closeCollectionDetail();
                } else if (pageDetail.classList.contains("active")) {
                    closeDetail();
                }
            }
        });

        // Load theme and bandwidth from settings
        api("GET", "/api/settings").then((s) => {
            if (s.theme) applyTheme(s.theme);
            const bw = parseInt(s.bandwidth_limit);
            updateBandwidthUI(isNaN(bw) ? -1 : bw);
        });

        refreshArchives();
        refreshGroups();
        refreshStatus();
        refreshQueueCount();
        loadNotifications();
        connectSSE();

        // Set initial lock indicator and confirmation settings
        api("GET", "/api/settings").then((s) => {
            updateLockIndicator(s.use_http === "1");
            for (const key of Object.keys(CONFIRM_KEYS)) {
                confirmSettings[key] = s[key] !== "0";
            }
        }).catch(() => {});
    }

    document.addEventListener("DOMContentLoaded", init);
})();
