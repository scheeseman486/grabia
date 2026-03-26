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
    let currentSort = "priority";
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
    let archiveSort = "default";
    // Groups collapsed by default; store *expanded* group IDs in localStorage
    let expandedGroups = new Set(
        JSON.parse(localStorage.getItem("grabia_expanded_groups") || "[]")
    );

    function saveExpandedGroups() {
        localStorage.setItem("grabia_expanded_groups", JSON.stringify([...expandedGroups]));
    }
    let realBandwidth = -1; // tracks the actual backend bandwidth setting
    let lastProgressRefresh = 0; // timestamp of last throttled progress refresh

    // --- Virtual Scroll State ---
    const VS_ROW_HEIGHT = 37;       // px per normal file row
    const VS_OVERSCAN = 15;         // extra rows rendered above/below viewport
    let vsFiles = [];               // all file data from last fetch
    let vsAllQueued = false;        // whether all files are currently queued
    let vsExpandedIds = new Set();  // file IDs with expanded detail rows
    let vsScrollRAF = null;         // requestAnimationFrame handle for scroll
    let vsLastRange = null;         // { start, end } of last rendered range

    // --- Notifications ---
    let notifications = [];
    let notifIdCounter = 0;

    function addNotification(message, type = "info") {
        // Create a server-side notification; add from response immediately
        // (SSE notification_created will be deduped by ID check)
        api("POST", "/api/notifications", { message, type }).then((notif) => {
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
        api("POST", "/api/notifications/clear").catch(() => {});
        notifications = notifications.filter((n) =>
            n.scan_archive_id != null || n.processing_archive_id != null || (n.adding_archive && n.adding_archive !== 0)
        );
        renderNotifBadge();
        renderNotifList();
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
            div.className = "notif-item notif-" + n.type;
            // Support both server (created_at as unix timestamp) and legacy (time as Date)
            const notifTime = n.created_at ? new Date(n.created_at * 1000) : (n.time || new Date());
            const ago = formatTimeAgo(notifTime);
            const hasProgress = n.progress !== undefined && n.progress !== null;
            let progressHtml = "";
            if (hasProgress) {
                if (n.progress >= 0) {
                    progressHtml = `<div class="notif-progress-track"><div class="notif-progress-fill" style="width:${n.progress}%"></div></div>`;
                } else {
                    progressHtml = `<div class="notif-progress-track"><div class="notif-progress-fill indeterminate"></div></div>`;
                }
            }
            // Active notifications (with progress) show cancel, not dismiss
            const isActive = hasProgress || (n.adding_archive && n.adding_archive !== 0);
            const cancelHtml = n.scan_archive_id
                ? `<button class="notif-cancel" data-cancel-type="scan" data-cancel-archive="${n.scan_archive_id}">Cancel</button>`
                : n.processing_archive_id
                ? `<button class="notif-cancel" data-cancel-type="process" data-cancel-archive="${n.processing_archive_id}">Cancel</button>`
                : "";
            const viewLogHtml = n.job_id
                ? `<button class="notif-view-log" data-job-id="${n.job_id}">View Log</button>`
                : "";
            const dismissHtml = isActive ? "" : `
                <button class="notif-dismiss" data-notif-id="${n.id}" title="Dismiss">
                    <svg viewBox="0 0 24 24" width="12" height="12"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
                </button>`;
            div.innerHTML = `
                <div class="notif-content">
                    <span class="notif-message">${escapeHtml(n.message)}</span>
                    ${progressHtml}
                    <span class="notif-time-row">
                        <span class="notif-time">${ago}</span>
                        ${viewLogHtml}
                        ${cancelHtml}
                    </span>
                </div>
                ${dismissHtml}
            `;
            const dismissBtn = div.querySelector(".notif-dismiss");
            if (dismissBtn) dismissBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                removeNotification(n.id);
            });
            const cancelBtn = div.querySelector(".notif-cancel");
            if (cancelBtn) {
                cancelBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const archiveId = parseInt(cancelBtn.dataset.cancelArchive);
                    if (cancelBtn.dataset.cancelType === "process") {
                        cancelProcessing(archiveId);
                    } else {
                        cancelScan(archiveId);
                    }
                });
            }
            const viewLogBtn = div.querySelector(".notif-view-log");
            if (viewLogBtn) {
                viewLogBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const jobId = parseInt(viewLogBtn.dataset.jobId);
                    $("#notif-popup").classList.remove("open");
                    openActivityLog({ job_id: jobId });
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

    function toggleNotifPopup() {
        const popup = document.querySelector("#notif-popup");
        popup.classList.toggle("open");
    }

    // --- DOM refs ---
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    const pageHome = $("#page-home");
    const pageDetail = $("#page-detail");
    const archiveListEl = $("#archive-list");
    const archiveListWrap = archiveListEl.closest(".archive-list-wrap");
    const emptyState = $("#empty-state");
    const fileListEl = $("#file-list");
    const queueStatusDot = $("#queue-status-dot");
    const speedDisplay = $("#speed-display");
    const sparkCanvas = $("#speed-sparkline");
    const sparkCtx = sparkCanvas.getContext("2d");
    const globalProgress = $("#global-progress");
    const progressFill = $("#progress-fill");
    const progressText = $("#progress-text");

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
                currentDownloadInfo = null;
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
            speedDisplay.textContent = formatSpeed(data.speed);
            pushSpeed(data.speed || 0);
            // Update queue display progress
            if (currentDownloadInfo && currentDownloadInfo.file_id === data.file_id) {
                currentDownloadInfo.downloaded = data.downloaded;
                currentDownloadInfo.size = data.size;
                const pct = data.size > 0 ? Math.min(100, data.downloaded / data.size * 100) : 0;
                queueDisplayFill.style.width = pct.toFixed(1) + "%";
            }
            throttledProgressRefresh();
        });

        es.addEventListener("file_complete", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "completed" });
            lastProgressRefresh = 0; // force immediate refresh
            throttledProgressRefresh();
            refreshQueueCount();
            if (queueDropdownOpen) loadQueueDropdown();
        });

        es.addEventListener("file_error", (e) => {
            const data = JSON.parse(e.data);
            const fname = data.filename || "Unknown file";
            const archive = data.identifier || "";
            const detail = data.error || "Unknown error";
            addNotification(`Download error: ${fname}${archive ? " (" + archive + ")" : ""} — ${detail}`, "error");
        });

        es.addEventListener("file_failed", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "failed" });
            lastProgressRefresh = 0;
            throttledProgressRefresh();
            const fname = data.filename || "Unknown file";
            const archive = data.identifier || "";
            addNotification(`Download failed: ${fname}${archive ? " (" + archive + ")" : ""} — retries exhausted`, "error");
        });

        es.addEventListener("file_start", () => {
            refreshStatus();
        });

        es.addEventListener("scan_progress", (e) => {
            const data = JSON.parse(e.data);
            updateScanProgress(data);
        });

        es.addEventListener("processing_progress", (e) => {
            const data = JSON.parse(e.data);
            updateProcessingProgress(data);
            // Refresh activity log if visible and job finished/cancelled
            if (data.phase === "done" || data.phase === "cancelled" || data.phase === "error") {
                _refreshActivityIfVisible();
            }
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
            notifications = notifications.filter(n =>
                n.scan_archive_id != null || n.processing_archive_id != null || (n.adding_archive && n.adding_archive !== 0)
            );
            renderNotifBadge();
            renderNotifList();
        });

        es.addEventListener("archive_added", () => { refreshArchives(); refreshQueueCount(); });
        es.addEventListener("archive_updated", () => { refreshArchives(); refreshQueueCount(); });
        es.addEventListener("archive_removed", () => { refreshArchives(); refreshQueueCount(); });
        es.addEventListener("archives_reordered", () => refreshArchives());
        es.addEventListener("groups_changed", () => refreshGroups());
        es.addEventListener("settings_updated", (e) => {
            const s = JSON.parse(e.data);
            if (s.theme) applyTheme(s.theme);
            updateLockIndicator(s.use_http === "1");
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
        if (dlState === "running" && data.current_file && data.current_speed) {
            speedDisplay.textContent = formatSpeed(data.current_speed);
        } else {
            speedDisplay.textContent = "";
        }
        // Track current download for queue display
        if (data.current_file) {
            currentDownloadInfo = data.current_file;
        } else if (dlState === "stopped") {
            currentDownloadInfo = null;
        }
        updateQueueDisplayText();
        updateGlobalProgress(data.progress);
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
    const queueDisplayFill = $("#queue-display-fill");
    const queueDropdown = $("#queue-dropdown");
    const queueDropdownList = $("#queue-dropdown-list");
    const queueDropdownCount = $("#queue-dropdown-count");
    let queueDropdownOpen = false;
    let currentDownloadInfo = null; // {file_id, filename, identifier, archive_id, size, downloaded}
    let lastQueueCount = 0;

    function updateQueueDisplayText() {
        // State class
        queueDisplay.className = queueDisplay.className.replace(/\bstate-\S+/g, "");
        queueDisplay.classList.add("state-" + dlState);
        if (queueDisplay.classList.contains("active")) queueDisplay.classList.add("active");

        if (currentDownloadInfo && (dlState === "running" || dlState === "paused")) {
            queueDisplayText.textContent = currentDownloadInfo.filename;
            queueDisplay.title = `${dlState === "running" ? "Downloading" : "Paused"}: ${currentDownloadInfo.filename} (${currentDownloadInfo.identifier})`;
            // Progress fill
            const pct = currentDownloadInfo.size > 0
                ? Math.min(100, (currentDownloadInfo.downloaded || 0) / currentDownloadInfo.size * 100)
                : 0;
            queueDisplayFill.style.width = pct.toFixed(1) + "%";
        } else {
            queueDisplayFill.style.width = "0";
            if (lastQueueCount > 0) {
                queueDisplayText.textContent = lastQueueCount + (lastQueueCount === 1 ? " file queued" : " files queued");
                queueDisplay.title = lastQueueCount + " pending";
            } else {
                queueDisplayText.textContent = "Queue is empty";
                queueDisplay.title = "View download queue";
            }
        }
    }

    async function refreshQueueCount() {
        try {
            const items = await api("GET", "/api/download/queue");
            lastQueueCount = items.length;
        } catch (_) {}
        updateQueueDisplayText();
    }

    async function loadQueueDropdown() {
        try {
            const items = await api("GET", "/api/download/queue");
            queueDropdownCount.textContent = items.length + (items.length === 1 ? " file" : " files");
            if (items.length === 0) {
                queueDropdownList.innerHTML = '<li class="queue-dropdown-empty">Queue is empty</li>';
                return;
            }
            queueDropdownList.innerHTML = "";
            items.forEach((f, i) => {
                const li = document.createElement("li");
                li.className = "queue-item" + (f.download_status === "downloading" ? " downloading" : "");
                li.innerHTML = `
                    <span class="queue-item-index">${i + 1}</span>
                    <span class="queue-item-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
                    <span class="queue-item-archive" title="${escapeHtml(f.title || f.identifier)}">${escapeHtml(f.title || f.identifier)}</span>
                    <span class="queue-item-size">${formatBytes(f.size)}</span>
                `;
                li.addEventListener("click", () => {
                    closeQueueDropdown();
                    navigateToFile(f.archive_id, f.id);
                });
                queueDropdownList.appendChild(li);
            });
        } catch (e) { /* ignore */ }
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

    function flashElement(el, times = 3) {
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

    function updateGlobalProgress(progress) {
        if (!progress || progress.total_files === 0) {
            globalProgress.style.display = "none";
            return;
        }
        globalProgress.style.display = "flex";
        const pct = progress.total_size > 0
            ? Math.min(100, (progress.downloaded_bytes / progress.total_size) * 100)
            : 0;
        progressFill.style.width = pct.toFixed(1) + "%";
        progressText.textContent =
            `${progress.completed_files}/${progress.total_files} files \u2022 ` +
            `${formatBytes(progress.downloaded_bytes)} / ${formatBytes(progress.total_size)} \u2022 ` +
            `${pct.toFixed(1)}%`;
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
        if (p.downloaded_bytes > 0 || p.completed_files > 0 || p.selected_files > 0) {
            const pct = p.selected_size > 0 ? ` \u2022 ${((p.downloaded_bytes / p.selected_size) * 100).toFixed(1)}%` : "";
            prog.textContent = `${p.completed_files}/${p.selected_files} files \u2022 ${formatBytes(p.downloaded_bytes)} / ${formatBytes(p.selected_size)}${pct}`;
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
        const bar = $("#archive-batch-actions");
        if (!bar) return;
        if (selectedArchiveIds.size > 0) {
            bar.style.display = "";
            $("#archive-batch-count").textContent = selectedArchiveIds.size + (selectedArchiveIds.size === 1 ? " archive selected" : " archives selected");
        } else {
            bar.style.display = "none";
        }
    }

    function buildArchiveItem(a, idx, listScope) {
        const li = document.createElement("li");
        li.className = "archive-item";
        li.dataset.id = a.id;
        li.draggable = true;
        li.innerHTML = `
            <div class="archive-grip" title="Drag to reorder">
                <div class="grip-dots"><span></span><span></span></div>
                <div class="grip-dots"><span></span><span></span></div>
                <div class="grip-dots"><span></span><span></span></div>
            </div>
            ${a.download_enabled
                ? `<button class="queue-toggle queue-remove" data-action="queue-remove" title="Remove from queue"><svg viewBox="0 0 16 16" width="14" height="14"><rect x="3" y="7" width="10" height="2" rx="1" fill="currentColor"/></svg></button>`
                : `<button class="queue-toggle queue-add" data-action="queue-add" title="Add to queue"><svg viewBox="0 0 16 16" width="14" height="14"><path d="M8 3a1 1 0 011 1v3h3a1 1 0 110 2H9v3a1 1 0 11-2 0V9H4a1 1 0 110-2h3V4a1 1 0 011-1z" fill="currentColor"/></svg></button>`
            }
            <div class="archive-checkbox">
                <input type="checkbox" ${selectedArchiveIds.has(a.id) ? "checked" : ""} data-action="select">
            </div>
            <div class="archive-info" data-action="open">
                <div class="archive-title">${escapeHtml(a.title || a.identifier)}</div>
                <div class="archive-meta">
                    <span>${a.files_count} files</span>
                    <span>${formatBytes(a.total_size)}</span>
                    <span>${a.identifier}</span>
                </div>
                ${a.selected_files > 0 ? `<div class="archive-progress-meta">${a.completed_files}/${a.selected_files} files \u2022 ${formatBytes(a.downloaded_bytes)} / ${formatBytes(a.selected_size)}${a.selected_size > 0 ? ` \u2022 ${((a.downloaded_bytes / a.selected_size) * 100).toFixed(1)}%` : ""}</div>` : ""}
            </div>
            <span class="archive-status ${a.status}">${a.status}</span>
            <div class="archive-actions">
                <button data-action="retry" title="Retry failed files" class="retry" style="display:${a.status === 'partial' || a.status === 'failed' ? 'flex' : 'none'}">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>
                </button>
                <button data-action="move-group" title="Move to group">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z" fill="currentColor"/></svg>
                </button>
                <button data-action="move-up" title="Move up" ${idx === 0 || listScope[idx - 1].download_enabled !== a.download_enabled ? "disabled" : ""}>
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" fill="currentColor"/></svg>
                </button>
                <button data-action="move-down" title="Move down" ${idx === listScope.length - 1 || listScope[idx + 1].download_enabled !== a.download_enabled ? "disabled" : ""}>
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"/></svg>
                </button>
                <button data-action="delete" class="delete" title="Remove">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" fill="currentColor"/></svg>
                </button>
            </div>
        `;

        // Event delegation
        li.addEventListener("click", (e) => {
            const action = e.target.closest("[data-action]")?.dataset.action;
            if (action === "queue-add") {
                toggleArchiveDownload(a.id, true);
            } else if (action === "queue-remove") {
                toggleArchiveDownload(a.id, false);
            } else if (action === "select") {
                const cb = li.querySelector("[data-action=select]");
                if (cb.checked) selectedArchiveIds.add(a.id);
                else selectedArchiveIds.delete(a.id);
                updateArchiveBatchActions();
            } else if (action === "open") {
                openArchiveDetail(a.id);
            } else if (action === "move-up") {
                moveArchive(archives.indexOf(a), archives.indexOf(a) - 1);
            } else if (action === "move-down") {
                moveArchive(archives.indexOf(a), archives.indexOf(a) + 1);
            } else if (action === "retry") {
                retryArchive(a.id);
            } else if (action === "delete") {
                confirmDelete(a);
            } else if (action === "move-group") {
                openMoveToGroup(a);
            }
        });

        // Drag and drop
        li.addEventListener("dragstart", (e) => {
            isDragging = true;
            dragSrcId = a.id;
            dragSrcGroupId = null; // not a group drag
            li.classList.add("dragging");
            e.dataTransfer.effectAllowed = "move";
        });
        li.addEventListener("dragend", () => {
            isDragging = false;
            li.classList.remove("dragging");
            $$(".archive-item").forEach((el) => el.classList.remove("drag-over"));
            $$(".group-header").forEach((el) => el.classList.remove("drag-over", "drag-over-group"));
            $$(".ungroup-drop-zone").forEach((el) => el.classList.remove("drag-over-group"));
            if (renderArchiveListPending) {
                renderArchiveListPending = false;
                renderArchiveList();
            }
        });
        li.addEventListener("dragover", (e) => {
            if (dragSrcGroupId !== null) return; // don't accept group drags on archives
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            li.classList.add("drag-over");
        });
        li.addEventListener("dragleave", () => li.classList.remove("drag-over"));
        li.addEventListener("drop", (e) => {
            e.preventDefault();
            li.classList.remove("drag-over");
            if (dragSrcGroupId !== null) return; // ignore group drops
            if (dragSrcId !== null && dragSrcId !== a.id) {
                const src = archives.find((x) => x.id === dragSrcId);
                if (src) {
                    // If dragged into a different group, move to that group first
                    if (src.group_id !== a.group_id) {
                        api("POST", `/api/archives/${dragSrcId}/group`, { group_id: a.group_id });
                    } else if (src.download_enabled === a.download_enabled) {
                        // Same group — reorder within it
                        const order = archives.map((x) => x.id);
                        const fromIdx = order.indexOf(dragSrcId);
                        const toIdx = order.indexOf(a.id);
                        order.splice(fromIdx, 1);
                        order.splice(toIdx, 0, dragSrcId);
                        api("POST", "/api/archives/reorder", { order });
                    }
                }
                dragSrcId = null;
            }
        });

        return li;
    }

    function renderArchiveList() {
        // Suppress re-renders during drag to avoid breaking the DOM mid-drag
        if (isDragging) {
            renderArchiveListPending = true;
            return;
        }
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

        // Apply sort — non-default sort flattens group structure
        const isCustomSort = archiveSort !== "default";
        if (isCustomSort) {
            filtered = [...filtered];
            const statusOrder = { downloading: 0, queued: 1, partial: 2, failed: 3, completed: 4, idle: 5 };
            switch (archiveSort) {
                case "title":
                    filtered.sort((a, b) => (a.title || a.identifier).localeCompare(b.title || b.identifier));
                    break;
                case "size":
                    filtered.sort((a, b) => (b.total_size || 0) - (a.total_size || 0));
                    break;
                case "files":
                    filtered.sort((a, b) => (b.files_count || 0) - (a.files_count || 0));
                    break;
                case "status":
                    filtered.sort((a, b) => (statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99));
                    break;
                case "added":
                    filtered.sort((a, b) => (b.added_at || "").localeCompare(a.added_at || ""));
                    break;
                case "progress":
                    filtered.sort((a, b) => {
                        const pa = a.selected_size > 0 ? a.downloaded_bytes / a.selected_size : 0;
                        const pb = b.selected_size > 0 ? b.downloaded_bytes / b.selected_size : 0;
                        return pb - pa;
                    });
                    break;
            }
            // Flat render — no groups
            filtered.forEach((a, idx) => {
                archiveListEl.appendChild(buildArchiveItem(a, idx, filtered));
            });
            updateArchiveBatchActions();
            return;
        }

        // Default queue-order render with groups
        // Render groups first
        const filteredSet = new Set(filtered.map((a) => a.id));
        groups.forEach((g, gIdx) => {
            const groupArchives = archives.filter((a) => a.group_id === g.id && filteredSet.has(a.id));
            // Hide empty groups when searching
            if (query && groupArchives.length === 0) return;
            const collapsed = !expandedGroups.has(g.id);

            const header = document.createElement("li");
            header.className = "group-header" + (collapsed ? " collapsed" : "");
            header.dataset.groupId = g.id;
            header.draggable = true;
            header.innerHTML = `
                <div class="group-header-left">
                    <div class="group-grip" title="Drag to reorder group">
                        <div class="grip-dots"><span></span><span></span></div>
                        <div class="grip-dots"><span></span><span></span></div>
                        <div class="grip-dots"><span></span><span></span></div>
                    </div>
                    <svg class="group-chevron" viewBox="0 0 24 24" width="14" height="14"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"/></svg>
                    <svg class="group-icon" viewBox="0 0 24 24" width="16" height="16"><path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 12H4V8h16v10z" fill="currentColor"/></svg>
                    <span class="group-name">${escapeHtml(g.name)}</span>
                    <span class="group-count">${groupArchives.length}</span>
                </div>
                <div class="group-actions">
                    <button data-group-action="move-up" title="Move group up" ${gIdx === 0 ? "disabled" : ""}>
                        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" fill="currentColor"/></svg>
                    </button>
                    <button data-group-action="move-down" title="Move group down" ${gIdx === groups.length - 1 ? "disabled" : ""}>
                        <svg viewBox="0 0 24 24" width="14" height="14"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"/></svg>
                    </button>
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
                else if (action === "move-up") moveGroup(gIdx, gIdx - 1);
                else if (action === "move-down") moveGroup(gIdx, gIdx + 1);
                else if (!e.target.closest(".group-actions") && !e.target.closest(".group-grip")) {
                    // Toggle collapse
                    if (expandedGroups.has(g.id)) expandedGroups.delete(g.id);
                    else expandedGroups.add(g.id);
                    saveExpandedGroups();
                    renderArchiveList();
                }
            });

            // Group drag-and-drop
            header.addEventListener("dragstart", (e) => {
                isDragging = true;
                dragSrcGroupId = g.id;
                dragSrcId = null;
                header.classList.add("dragging");
                e.dataTransfer.effectAllowed = "move";
            });
            header.addEventListener("dragend", () => {
                isDragging = false;
                header.classList.remove("dragging");
                $$(".group-header").forEach((el) => el.classList.remove("drag-over"));
                if (renderArchiveListPending) {
                    renderArchiveListPending = false;
                    renderArchiveList();
                }
            });
            header.addEventListener("dragover", (e) => {
                if (dragSrcGroupId !== null) {
                    // Group-to-group reorder
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "move";
                    header.classList.add("drag-over");
                } else if (dragSrcId !== null) {
                    // Archive being dragged onto a group header
                    const src = archives.find((x) => x.id === dragSrcId);
                    if (src && src.group_id !== g.id) {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                        header.classList.add("drag-over-group");
                    }
                }
            });
            header.addEventListener("dragleave", () => {
                header.classList.remove("drag-over");
                header.classList.remove("drag-over-group");
            });
            header.addEventListener("drop", (e) => {
                e.preventDefault();
                header.classList.remove("drag-over");
                header.classList.remove("drag-over-group");
                if (dragSrcGroupId !== null && dragSrcGroupId !== g.id) {
                    // Group reorder
                    const order = groups.map((x) => x.id);
                    const fromIdx = order.indexOf(dragSrcGroupId);
                    const toIdx = order.indexOf(g.id);
                    order.splice(fromIdx, 1);
                    order.splice(toIdx, 0, dragSrcGroupId);
                    api("POST", "/api/groups/reorder", { order });
                    dragSrcGroupId = null;
                } else if (dragSrcId !== null) {
                    // Archive dropped onto group header — move into this group
                    const src = archives.find((x) => x.id === dragSrcId);
                    if (src && src.group_id !== g.id) {
                        api("POST", `/api/archives/${dragSrcId}/group`, { group_id: g.id });
                    }
                    dragSrcId = null;
                }
            });

            archiveListEl.appendChild(header);

            // Group's archives (hidden if collapsed)
            if (!collapsed) {
                let groupDividerInserted = false;
                const groupQueued = groupArchives.filter((a) => a.download_enabled);
                groupArchives.forEach((a, idx) => {
                    if (!groupDividerInserted && groupQueued.length > 0 && !a.download_enabled) {
                        groupDividerInserted = true;
                        const div = document.createElement("li");
                        div.className = "archive-queue-divider in-group";
                        div.innerHTML = '<div class="queue-divider"><span>Not queued</span></div>';
                        archiveListEl.appendChild(div);
                    }
                    const li = buildArchiveItem(a, idx, groupArchives);
                    li.classList.add("in-group");
                    archiveListEl.appendChild(li);
                });
            }
        });

        // Ungroup drop zone — always present when groups exist
        const looseArchives = archives.filter((a) => !a.group_id && filteredSet.has(a.id));
        if (groups.length > 0) {
            const dropZone = document.createElement("li");
            dropZone.className = "ungroup-drop-zone";
            dropZone.innerHTML = '<div class="ungroup-divider"><span>Ungrouped</span></div>';
            dropZone.addEventListener("dragover", (e) => {
                if (dragSrcId !== null) {
                    const src = archives.find((x) => x.id === dragSrcId);
                    if (src && src.group_id !== null) {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                        dropZone.classList.add("drag-over-group");
                    }
                }
            });
            dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over-group"));
            dropZone.addEventListener("drop", (e) => {
                e.preventDefault();
                dropZone.classList.remove("drag-over-group");
                if (dragSrcId !== null) {
                    const src = archives.find((x) => x.id === dragSrcId);
                    if (src && src.group_id !== null) {
                        api("POST", `/api/archives/${dragSrcId}/group`, { group_id: null });
                    }
                    dragSrcId = null;
                }
            });
            archiveListEl.appendChild(dropZone);
        }

        // Ungrouped archives
        let looseDividerInserted = false;
        const looseQueued = looseArchives.filter((a) => a.download_enabled);
        looseArchives.forEach((a, idx) => {
            if (!looseDividerInserted && looseQueued.length > 0 && !a.download_enabled) {
                looseDividerInserted = true;
                const div = document.createElement("li");
                div.className = "archive-queue-divider";
                div.innerHTML = '<div class="queue-divider"><span>Not queued</span></div>';
                archiveListEl.appendChild(div);
            }
            archiveListEl.appendChild(buildArchiveItem(a, idx, looseArchives));
        });

        updateArchiveBatchActions();
    }

    async function toggleArchiveDownload(id, enabled) {
        await api("POST", `/api/archives/${id}/download`, { enabled });
        await refreshArchives();
        refreshStatus();
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
        try {
            await api("POST", `/api/archives/${currentArchiveId}/scan`);
            // Server creates the notification and broadcasts via SSE
            updateScanButton();
        } catch (e) {
            if (e.message && e.message.includes("already queued")) {
                addNotification(`Scan "${archiveName}": already queued`, "info");
            } else {
                addNotification(`Scan "${archiveName}" failed: ` + e.message, "error");
            }
        }
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
        const active = currentArchiveId && notifications.some(n => n.scan_archive_id === currentArchiveId && n.progress !== null);
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

        currentSort = "priority";
        currentSortDir = "";
        fileSearchQuery = "";
        selectedFileIds.clear();
        vsFiles = [];
        vsLastRange = null;
        vsExpandedIds.clear();
        $("#file-sort").value = "priority";
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
            renderFiles(data);
            if (data.progress) updateDetailProgressFromData(data.progress);
        } catch (e) {
            if (gen !== loadFilesGen || archiveId !== currentArchiveId) return;
            vsFiles = [];
            fileListEl.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--danger)">${escapeHtml(e.message)}</td></tr>`;
        }
    }

    const SORT_COL_MAP = { "col-name": "name", "col-size": "size", "col-modified": "modified", "col-status": "status" };
    const SORT_DEFAULTS = { name: "asc", size: "desc", modified: "desc", status: "asc", priority: "asc" };

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
        const all = vsFiles.length > 0 && vsFiles.every(f => selectedFileIds.has(f.id));
        $("#select-all-files").checked = all;
        updateBatchActions();
    }

    function updateBatchActions() {
        const bar = $("#batch-actions");
        if (!bar) return;
        if (selectedFileIds.size > 0) {
            bar.style.display = "";
            $("#batch-count").textContent = selectedFileIds.size + (selectedFileIds.size === 1 ? " file selected" : " files selected");
            // Update Queue/Unqueue button label based on selected files' queue state
            const queueBtn = $("#batch-queue");
            if (queueBtn) {
                const allQueued = [...selectedFileIds].every(id => {
                    const f = vsFiles.find(f => f.id === id);
                    return f && f.queued;
                });
                queueBtn.textContent = allQueued ? "Unqueue" : "Queue";
            }
        } else {
            bar.style.display = "none";
        }
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
        thead.innerHTML += '<th class="col-check"><input type="checkbox" id="select-all-files" title="Select / deselect all"></th>';
        thead.innerHTML += '<th class="col-name">Name</th>';
        thead.innerHTML += '<th class="col-size">Size</th>';
        thead.innerHTML += '<th class="col-modified">Modified</th>';
        thead.innerHTML += '<th class="col-status">Status</th>';
        if (isPriority) {
            thead.innerHTML += '<th class="col-priority"></th>';
        }
        // Show/hide reset order button based on sort mode
        const resetBtn = $("#btn-reset-queue-order");
        if (resetBtn) resetBtn.style.display = isPriority ? "" : "none";
        // Re-attach handlers
        $("#select-all-files").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
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
        return currentSort === "priority" ? 9 : 7;
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

    // Build a "virtual row" descriptor for each logical row (files + divider).
    // Returns an array of { type, file?, idx? } objects.
    function vsGetRowDescriptors(files, isPriority) {
        const rows = [];
        const hasQueued = isPriority && files.some(f => f.queued);
        const hasUnqueued = isPriority && files.some(f => !f.queued);
        const needsDivider = hasQueued && hasUnqueued;
        let dividerInserted = false;
        for (let i = 0; i < files.length; i++) {
            const f = files[i];
            if (needsDivider && !dividerInserted && !f.queued) {
                dividerInserted = true;
                rows.push({ type: "divider" });
            }
            rows.push({ type: "file", file: f, idx: i });
        }
        return rows;
    }

    // Build a single file <tr> element with all cells and event listeners.
    function buildFileRow(f, isPriority, queuedFiles, lastQueuedIdx) {
        const tr = document.createElement("tr");
        tr.dataset.fileId = f.id;

        if (f.change_status) tr.className = "file-row-" + f.change_status;

        const changeIcon = f.change_status
            ? `<span class="change-info ${f.change_status}" aria-label="${escapeHtml(f.change_detail)}">` +
              `<span class="change-tooltip">${escapeHtml(f.change_detail)}</span>` +
              (f.change_status === "new" ? "+" : f.change_status === "removed" ? "\u2212" : "\u0394") +
              `</span>`
            : "";

        let html = "";
        const isUnknown = f.download_status === "unknown";

        // Grip column
        if (isPriority) {
            html += (f.queued && !isUnknown) ? buildGripCell() : '<td class="col-grip"></td>';
        }

        const procStatus = f.processing_status || "";
        const sourceDeleted = (f.download_status === "pending" && procStatus === "processed");
        const hasProcessedOutput = (procStatus === "processed");
        const hideQueue = isUnknown || (hasProcessedOutput && !sourceDeleted);
        if (hideQueue) {
            html += '<td class="col-queue"></td>';
        } else if (f.queued) {
            html += `<td class="col-queue"><button class="queue-toggle queue-remove" data-queue-id="${f.id}" title="Remove from queue">` +
                `<svg viewBox="0 0 16 16" width="14" height="14"><rect x="3" y="7" width="10" height="2" rx="1" fill="currentColor"/></svg></button></td>`;
        } else {
            html += `<td class="col-queue"><button class="queue-toggle queue-add" data-queue-id="${f.id}" title="Add to queue">` +
                `<svg viewBox="0 0 16 16" width="14" height="14"><path d="M8 3a1 1 0 011 1v3h3a1 1 0 110 2H9v3a1 1 0 11-2 0V9H4a1 1 0 110-2h3V4a1 1 0 011-1z" fill="currentColor"/></svg></button></td>`;
        }

        html += `<td class="col-check"><input type="checkbox" ${selectedFileIds.has(f.id) ? "checked" : ""} data-select-id="${f.id}"></td>`;

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
        html += `<td class="col-name"><div class="file-name-wrap">` +
            unknownGrip +
            renderFileName(f.name, sourceDeleted ? "file-name-deleted" : "") + changeIcon +
            `<span class="file-actions">` +
            renameBtn + processBtn + rescanBtn + deleteBtn +
            `</span></div></td>`;

        html += `<td class="col-size" style="text-align:right">${formatBytes(f.size)}</td>`;
        html += `<td class="col-modified">${formatDate(f.mtime)}</td>`;
        const displayStatus = formatFileStatus(f);
        const isSkipped = !f.queued && f.download_status === "pending";
        const statusClass = procStatus === "processed" ? "processed"
            : procStatus === "failed" ? "proc-failed"
            : procStatus === "processing" || procStatus === "queued" ? "proc-active"
            : isSkipped ? "skipped"
            : f.download_status;
        const hasError = ((f.download_status === "failed" || f.download_status === "conflict" || f.download_status === "unknown") && f.error_message)
            || (procStatus === "failed" && f.processing_error);
        const isConflict = f.download_status === "conflict";
        const errorMsg = (procStatus === "failed" && f.processing_error) ? f.processing_error : f.error_message;
        html += `<td class="col-status">` +
            `<span class="file-status ${statusClass}" ${hasError ? `title="${escapeHtml(errorMsg)}"` : ""}>${displayStatus}</span>` +
            (hasError && isConflict
                ? `<span class="file-error-hint clickable" data-conflict-file='${JSON.stringify({id: f.id, name: f.name, size: f.size, error: f.error_message})}' title="Click to resolve conflict">&#9432;</span>`
                : hasError ? `<span class="file-error-hint" title="${escapeHtml(f.error_message)}">&#9432;</span>` : "") +
            (f.download_status === "failed" ? `<button class="retry-file-btn" data-retry-file="${f.id}" title="Retry this file">&#x21bb;</button>` : "") +
            `</td>`;

        if (isPriority) {
            if (f.queued && !isUnknown) {
                const selIdx = queuedFiles.indexOf(f);
                html += buildPriorityCell(f.id, selIdx === 0, selIdx === lastQueuedIdx);
            } else {
                html += '<td class="col-priority"></td>';
            }
        }

        tr.innerHTML = html;

        // --- Event listeners ---
        const selectCb = tr.querySelector("input[type=checkbox][data-select-id]");
        if (selectCb) {
            selectCb.addEventListener("change", (e) => {
                const fid = parseInt(e.target.dataset.selectId);
                if (e.target.checked) selectedFileIds.add(fid);
                else selectedFileIds.delete(fid);
                syncSelectAll();
            });
        }
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
        if (isPriority && f.queued && !isUnknown) {
            attachPriorityDrag(tr, f.id);
            const upBtn = tr.querySelector(`[data-move-up="${f.id}"]`);
            const downBtn = tr.querySelector(`[data-move-down="${f.id}"]`);
            if (upBtn) upBtn.addEventListener("click", () => moveFile(f.id, -1));
            if (downBtn) downBtn.addEventListener("click", () => moveFile(f.id, 1));
        }
        if (procStatus === "processed") {
            tr.classList.add("processed-expandable");
            tr.addEventListener("click", (e) => {
                if (e.target.closest("input, button, .file-error-hint, .file-actions")) return;
                toggleProcessedDetail(tr, f, isPriority);
            });
        }

        // Unknown files: draggable source for assign-as-output
        if (isUnknown) {
            attachUnknownDrag(tr, f.id);
        }
        // Completed, processed, and skipped files: drop targets for unknown files
        const canReceiveOutput = f.download_status === "completed" || hasProcessedOutput || isSkipped;
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
        const queuedFiles = isPriority ? vsFiles.filter(f => f.queued && f.download_status !== "unknown") : [];
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
            } else {
                const tr = buildFileRow(desc.file, isPriority, queuedFiles, lastQueuedIdx);
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

        // Sync the select-all header checkbox
        const allChecked = files.length > 0 && files.every((f) => selectedFileIds.has(f.id));
        $("#select-all-files").checked = allChecked;
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

    function formatFileStatus(f) {
        // Show processing status first — it takes priority over download status
        if (f.processing_status && f.processing_status !== "") {
            if (f.processing_status === "processed") return "processed";
            if (f.processing_status === "processing") return "processing...";
            if (f.processing_status === "queued") return "proc. queued";
            if (f.processing_status === "failed") return "proc. failed";
            if (f.processing_status === "skipped") return "proc. skipped";
        }
        if (!f.queued && f.download_status === "pending") {
            if (f.downloaded_bytes > 0 && f.size > 0) {
                const pct = ((f.downloaded_bytes / f.size) * 100).toFixed(1);
                return `${pct}%`;
            }
            return "skipped";
        }
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

    async function toggleProcessedDetail(tr, f, isPriority) {
        // If already expanded, collapse
        const existing = tr.nextElementSibling;
        if (existing && existing.classList.contains("processed-detail-row")) {
            existing.remove();
            tr.classList.remove("expanded");
            return;
        }

        // Fetch the actual on-disk tree from the server
        let tree = [];
        try {
            const data = await api("GET", `/api/files/${f.id}/processed-tree`);
            tree = data.tree || [];
        } catch (e) {
            // Fallback to DB data
            let outputFiles = [];
            if (f.processed_files_json) {
                try { outputFiles = JSON.parse(f.processed_files_json); } catch (e2) {}
            }
            if (outputFiles.length === 0 && f.processed_filename) {
                outputFiles = [f.processed_filename];
            }
            tree = outputFiles.map((p) => ({ name: p, path: p, type: "file", size: 0 }));
        }

        if (tree.length === 0) return;

        const colCount = getColspan();
        const detailTr = document.createElement("tr");
        detailTr.classList.add("processed-detail-row");

        function buildTreeHtml(nodes, depth) {
            let html = `<ul class="processed-tree" style="padding-left:${depth > 0 ? 16 : 0}px">`;
            for (const node of nodes) {
                const icon = node.type === "dir"
                    ? `<svg class="ptree-icon" viewBox="0 0 16 16" width="13" height="13"><path d="M14 4H8L6.5 2.5h-5l-.5.5v10l.5.5h13l.5-.5V4.5L14 4zM13 12H2V4h4l1.5 1.5H13V12z" fill="currentColor"/></svg>`
                    : `<svg class="ptree-icon" viewBox="0 0 16 16" width="13" height="13"><path d="M3 1h6l4 4v10H3V1zm6 0v4h4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
                const sizeStr = node.size != null && node.size > 0 ? `<span class="ptree-size">${formatBytes(node.size)}</span>` : `<span class="ptree-size"></span>`;
                const mtimeStr = node.mtime ? `<span class="ptree-mtime">${new Date(node.mtime * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}</span>` : `<span class="ptree-mtime"></span>`;
                const nameHtml = renderPtreeName(node.name);
                html += `<li class="ptree-node" data-path="${escapeHtml(node.path)}" data-type="${node.type}" data-name="${escapeHtml(node.name)}">` +
                    `<div class="ptree-row">${icon}${nameHtml}` +
                    `<span class="ptree-actions">` +
                    `<button class="ptree-btn" data-ptree-action="rename" title="Rename"><svg viewBox="0 0 16 16" width="11" height="11"><path d="M12.15 2.85a1.2 1.2 0 00-1.7 0L3.5 9.8l-.8 3.5 3.5-.8 6.95-6.95a1.2 1.2 0 000-1.7z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg></button>` +
                    `<button class="ptree-btn ptree-btn-danger" data-ptree-action="delete" title="Delete"><svg viewBox="0 0 16 16" width="11" height="11"><path d="M5.5 2h5M3 4h10M6 4v8m4-8v8M4.5 4l.5 9h6l.5-9" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button>` +
                    `</span>${sizeStr}${mtimeStr}</div>`;
                if (node.type === "dir" && node.children && node.children.length > 0) {
                    html += buildTreeHtml(node.children, depth + 1);
                }
                html += "</li>";
            }
            html += "</ul>";
            return html;
        }

        let cellHtml = `<div class="processed-tree-wrap">` +
            `<div class="processed-tree-header">` +
            `<span class="processed-tree-label">Processed output</span>` +
            `<button class="ptree-delete-all" data-file-id="${f.id}" title="Delete all processed files">Delete all</button>` +
            `</div>` +
            buildTreeHtml(tree, 0) +
            `</div>`;
        detailTr.innerHTML = `<td colspan="${colCount}" class="processed-detail-cell">${cellHtml}</td>`;

        // Attach handlers
        detailTr.querySelectorAll(".ptree-btn").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const li = btn.closest(".ptree-node");
                const path = li.dataset.path;
                const name = li.dataset.name;
                const action = btn.dataset.ptreeAction;
                if (action === "delete") {
                    confirmAction("confirm_delete_processed", "Delete Processed File", `Delete &ldquo;${escapeHtml(name)}&rdquo;?`, () => {
                        api("POST", `/api/files/${f.id}/delete-processed`, { filename: path }).then(() => {
                            addNotification(`Deleted "${name}"`, "info");
                            detailTr.remove();
                            tr.classList.remove("expanded");
                            loadFiles();
                        }).catch((e2) => addNotification("Delete failed: " + e2.message, "error"));
                    }, { confirmText: "Delete" });
                } else if (action === "rename") {
                    startProcessedRename(li, f.id, path, name);
                }
            });
        });

        // Delete all button
        const deleteAllBtn = detailTr.querySelector(".ptree-delete-all");
        if (deleteAllBtn) {
            deleteAllBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                confirmAction("confirm_delete_processed", "Delete All Processed Files", "Delete all processed output files for this item?", () => {
                    api("POST", `/api/files/${f.id}/delete-processed`, { delete_all: true }).then(() => {
                        addNotification("Deleted all processed files", "info");
                        detailTr.remove();
                        tr.classList.remove("expanded");
                        loadFiles();
                    }).catch((e2) => addNotification("Delete failed: " + e2.message, "error"));
                }, { confirmText: "Delete All" });
            });
        }

        tr.after(detailTr);
        tr.classList.add("expanded");
        applyTruncationTooltips(detailTr);
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
            return f && f.queued;
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

    function updateFileRow(fileId, updates) {
        // Update in-memory data so virtual scroll re-renders stay current
        const vsFile = vsFiles.find(f => f.id === fileId);
        if (vsFile) {
            if (updates.download_status) vsFile.download_status = updates.download_status;
            if (updates.downloaded_bytes !== undefined) vsFile.downloaded_bytes = updates.downloaded_bytes;
            if (updates.size !== undefined) vsFile.size = updates.size;
        }
        // Update the visible DOM row if present
        const tr = fileListEl.querySelector(`tr[data-file-id="${fileId}"]`);
        if (!tr) return;
        const statusCell = tr.querySelector(".file-status");
        if (statusCell && updates.download_status) {
            statusCell.className = "file-status " + updates.download_status;
            if (updates.download_status === "downloading" && updates.size > 0) {
                const pct = ((updates.downloaded_bytes / updates.size) * 100).toFixed(1);
                statusCell.textContent = pct + "%";
            } else {
                statusCell.textContent = updates.download_status;
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
                adding_archive: true,
                progress: -1,
            });
            serverNotifId = nRes.id;
        } catch (_) {
            // Fallback: local-only notification
            const nid = "local-" + (++notifIdCounter);
            notifications.unshift({ id: nid, message: `Adding "${label}": fetching metadata...`, type: "info", created_at: Date.now() / 1000, progress: -1, adding_archive: true });
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
                adding_archive: true,
                progress: 0,
            });
            serverNotifId = nRes.id;
        } catch (_) {
            const nid = "local-" + (++notifIdCounter);
            notifications.unshift({ id: nid, message: `Batch add: 0/${total}`, type: "info", created_at: Date.now() / 1000, progress: 0, adding_archive: true });
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
                api("PATCH", "/api/notifications/" + serverNotifId, { message: `Batch add: ${done}/${total}`, progress: pct }).catch(() => {});
            }
            // Also update local copy
            const active = notifications.find(n => n.id === serverNotifId);
            if (active) {
                active.progress = pct;
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
            max_retries: $("#set-max-retries").value,
            retry_delay: $("#set-retry-delay").value,
            sse_update_rate: $("#set-sse-update-rate").value,
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
            $("#set-max-retries").value = s.max_retries || "3";
            $("#set-retry-delay").value = s.retry_delay || "5";
            $("#set-sse-update-rate").value = s.sse_update_rate || "500";
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
            max_retries: $("#set-max-retries").value,
            retry_delay: $("#set-retry-delay").value,
            sse_update_rate: $("#set-sse-update-rate").value,
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

    // --- Select All Files ---

    function toggleSelectAll(checked) {
        if (!currentArchiveId) return;
        // Update all files in memory (not just visible rows)
        if (checked) {
            vsFiles.forEach(f => selectedFileIds.add(f.id));
        } else {
            selectedFileIds.clear();
        }
        // Update visible checkboxes
        const boxes = fileListEl.querySelectorAll("input[type=checkbox][data-select-id]");
        boxes.forEach(cb => { cb.checked = checked; });
        updateBatchActions();
    }

    // --- Confirmation System ---

    // Keys and their default-enabled state (true = warn by default)
    const CONFIRM_KEYS = {
        confirm_reset_order:         { label: "Warn before resetting download order",              default: true },
        confirm_delete_file:         { label: "Warn before deleting a file",                       default: true },
        confirm_batch_delete_files:  { label: "Warn before batch-deleting files",                  default: true },
        confirm_delete_folders:      { label: "Warn before deleting download folders",             default: true },
        confirm_delete_processed:    { label: "Warn before deleting processed output files",       default: true },
        confirm_delete_profile:      { label: "Warn before deleting a processing profile",         default: true },
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

    // --- Reset Download Order ---

    function confirmResetOrder() {
        if (!currentArchiveId) return;
        confirmAction(
            "confirm_reset_order",
            "Reset Queue Order",
            "This will reset the download priority of all files in this archive back to alphabetical order by filename. Any custom ordering you have set will be lost.",
            doResetOrder,
            { confirmText: "Reset Queue Order" }
        );
    }

    async function doResetOrder() {
        if (!currentArchiveId) return;
        await api("POST", `/api/archives/${currentArchiveId}/files/reset-order`);
        await loadFiles();
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
        $("#process-auto-future").checked = false;
        $("#modal-process-archive").classList.add("open");
    }

    async function confirmProcessArchive() {
        const profileId = parseInt($("#process-profile-select").value);
        const options = collectOptions($("#process-profile-options"));
        const autoProcess = $("#process-auto-future").checked;
        const fileIds = pendingBatchProcessIds || undefined;
        pendingBatchProcessIds = null;

        // Batch archive processing
        const archiveIds = pendingBatchArchiveProcessIds || (currentArchiveId ? [currentArchiveId] : []);
        pendingBatchArchiveProcessIds = null;

        if (!profileId || archiveIds.length === 0) return;

        let queued = 0;
        for (const aid of archiveIds) {
            try {
                const body = { profile_id: profileId, options, auto_process: autoProcess };
                if (fileIds) body.file_ids = fileIds;
                const resp = await api("POST", `/api/archives/${aid}/process`, body);
                if (resp.queued) queued++;
            } catch (e) {
                addNotification(`Processing failed for archive ${aid}: ${e.message}`, "error");
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
        const collsEl = $("#archive-collections-list");
        tagsEl.innerHTML = "";
        collsEl.innerHTML = "";
        try {
            const [tags, colls] = await Promise.all([
                api("GET", `/api/archives/${archiveId}/tags`),
                api("GET", `/api/archives/${archiveId}/collections`),
            ]);
            renderArchiveTags(tags, archiveId);
            renderArchiveCollections(colls);
        } catch (e) {
            // Silently fail — tags are optional
        }
    }

    function renderArchiveTags(tags, archiveId) {
        const el = $("#archive-tags");
        el.innerHTML = "";
        for (const tag of tags) {
            const chip = document.createElement("span");
            chip.className = "tag-chip";
            chip.innerHTML = `${esc(tag)} <button class="tag-remove" data-tag="${esc(tag)}">&times;</button>`;
            el.appendChild(chip);
        }
        // Remove tag handler
        el.querySelectorAll(".tag-remove").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const tag = btn.dataset.tag;
                await api("DELETE", `/api/archives/${archiveId}/tags/${encodeURIComponent(tag)}`);
                loadArchiveTagsAndCollections(archiveId);
            });
        });
    }

    function renderArchiveCollections(colls) {
        const el = $("#archive-collections-list");
        el.innerHTML = "";
        if (colls.length === 0) {
            el.innerHTML = '<span class="no-collections">None</span>';
            return;
        }
        for (const c of colls) {
            const chip = document.createElement("span");
            chip.className = "collection-chip";
            chip.textContent = c.name;
            chip.addEventListener("click", () => openCollectionDetail(c.id));
            el.appendChild(chip);
        }
    }

    // ── Collections ────────────────────────────────────────────────────────

    let collections = [];
    let currentCollectionId = null;
    let editingCollectionId = null;
    let editingLayoutId = null;

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

    // ── Activity Log ──────────────────────────────────────────────
    let activityOffset = 0;
    const ACTIVITY_PAGE_SIZE = 100;
    let activityJobFilter = null;  // set when navigating from a notification

    async function openActivityLog(opts) {
        opts = opts || {};
        activityOffset = 0;
        activityJobFilter = opts.job_id || null;

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

        await populateActivityArchiveFilter();
        await loadActivityJobs(opts.job_id || null);
        await loadActivityLog();
        showPage("page-activity");
    }

    async function loadActivityJobs(highlightJobId) {
        const banner = $("#activity-job-banner");
        try {
            const data = await api("GET", "/api/activity/jobs?limit=10");
            const jobs = data.jobs || [];
            if (jobs.length === 0) {
                banner.style.display = "none";
                return;
            }
            // If a specific job is highlighted, show it prominently
            // Otherwise show active (running) jobs, or the most recent completed one
            let displayJobs;
            if (highlightJobId) {
                activityJobFilter = highlightJobId;
                displayJobs = jobs.filter(j => j.id === highlightJobId);
                if (displayJobs.length === 0) {
                    // Fetch it directly
                    try {
                        const j = await api("GET", `/api/activity/jobs/${highlightJobId}`);
                        if (j) displayJobs = [j];
                    } catch (_) {}
                }
            } else {
                // Show running jobs first, then most recent
                const running = jobs.filter(j => j.status === "running");
                displayJobs = running.length > 0 ? running : jobs.slice(0, 3);
            }
            if (!displayJobs || displayJobs.length === 0) {
                banner.style.display = "none";
                return;
            }
            banner.innerHTML = displayJobs.map(j => {
                const started = new Date(j.started_at * 1000).toLocaleString();
                const statusCls = j.status || "running";
                const isFiltered = activityJobFilter === j.id;
                const filterCls = isFiltered ? " job-card-active" : "";
                const archiveName = j.archive_title || j.archive_identifier
                    ? ` — ${esc(j.archive_title || j.archive_identifier)}`
                    : "";
                return `<div class="job-card${filterCls}" data-job-id="${j.id}">
                    <span class="job-category">${esc(j.category)}</span>
                    <span class="job-status ${statusCls}">${esc(j.status)}</span>
                    <span class="job-time">${started}${archiveName}</span>
                    ${j.summary ? `<span class="job-summary">${esc(j.summary)}</span>` : ""}
                </div>`;
            }).join("");
            banner.style.display = "";

            // Click on a job card to filter by it
            banner.querySelectorAll(".job-card").forEach(el => {
                el.addEventListener("click", () => {
                    const jid = parseInt(el.dataset.jobId);
                    if (activityJobFilter === jid) {
                        // Toggle off
                        activityJobFilter = null;
                        el.classList.remove("job-card-active");
                    } else {
                        activityJobFilter = jid;
                        banner.querySelectorAll(".job-card").forEach(c => c.classList.remove("job-card-active"));
                        el.classList.add("job-card-active");
                    }
                    activityOffset = 0;
                    loadActivityLog();
                });
            });
        } catch (_) {
            banner.style.display = "none";
        }
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
        if (activityJobFilter) {
            params.set("job_id", activityJobFilter);
        } else {
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
        }
        params.set("limit", ACTIVITY_PAGE_SIZE);
        params.set("offset", activityOffset);

        try {
            const data = await api("GET", `/api/activity/log?${params}`);
            renderActivityLog(data.entries, data.total);
        } catch (err) {
            $("#activity-log-list").innerHTML = `<div class="activity-log-wrap"><div class="activity-empty">Failed to load activity log</div></div>`;
        }
    }

    function renderActivityLog(entries, total) {
        const list = $("#activity-log-list");
        if (!entries || entries.length === 0) {
            list.innerHTML = `<div class="activity-log-wrap"><div class="activity-empty">No activity log entries</div></div>`;
            $("#activity-pagination").innerHTML = "";
            return;
        }
        const rows = entries.map(e => {
            const dt = new Date(e.timestamp * 1000);
            const time = dt.toLocaleString();
            const cat = e.resolved_category || e.category || "";
            const lvl = e.level || "info";
            const archiveLink = e.archive_identifier
                ? ` — <span class="entry-archive" data-id="${e.archive_id}">${esc(e.archive_title || e.archive_identifier)}</span>`
                : "";
            const detail = e.detail
                ? `<div class="entry-detail">${esc(e.detail)}</div>`
                : "";
            return `<tr>
                <td class="col-time"><span class="entry-time">${esc(time)}</span></td>
                <td class="col-category"><span class="entry-category">${esc(cat)}</span></td>
                <td class="col-level"><span class="entry-level level-${lvl}">${esc(lvl)}</span></td>
                <td><span class="entry-message">${esc(e.message)}${archiveLink}</span>${detail}</td>
            </tr>`;
        }).join("");
        list.innerHTML = `<div class="activity-log-wrap"><table class="activity-table">
            <thead><tr>
                <th class="col-time">Time</th>
                <th class="col-category">Category</th>
                <th class="col-level">Level</th>
                <th>Message</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;

        // Click on archive links
        list.querySelectorAll(".entry-archive").forEach(el => {
            el.addEventListener("click", () => {
                const id = parseInt(el.dataset.id);
                if (id) openArchiveDetail(id);
            });
        });

        // Pagination
        const pag = $("#activity-pagination");
        const totalPages = Math.ceil(total / ACTIVITY_PAGE_SIZE);
        const currentPage = Math.floor(activityOffset / ACTIVITY_PAGE_SIZE) + 1;
        pag.innerHTML = `
            <button ${activityOffset === 0 ? "disabled" : ""} id="activity-prev">Previous</button>
            <span class="page-info">Page ${currentPage} of ${totalPages} (${total} entries)</span>
            <button ${activityOffset + ACTIVITY_PAGE_SIZE >= total ? "disabled" : ""} id="activity-next">Next</button>
        `;
        if (activityOffset > 0) {
            $("#activity-prev").addEventListener("click", () => {
                activityOffset = Math.max(0, activityOffset - ACTIVITY_PAGE_SIZE);
                loadActivityLog();
            });
        }
        if (activityOffset + ACTIVITY_PAGE_SIZE < total) {
            $("#activity-next").addEventListener("click", () => {
                activityOffset += ACTIVITY_PAGE_SIZE;
                loadActivityLog();
            });
        }
    }

    function clearActivityFilters() {
        $("#activity-filter-category").value = "";
        $("#activity-filter-level").value = "";
        $("#activity-filter-group").value = "";
        $("#activity-filter-archive").value = "";
        $("#activity-filter-search").value = "";
        activityJobFilter = null;
        activityOffset = 0;
        loadActivityJobs(null);
        loadActivityLog();
    }

    function _refreshActivityIfVisible() {
        if ($("#page-activity").classList.contains("active")) {
            loadActivityJobs(activityJobFilter);
            loadActivityLog();
        }
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
            const layoutInfo = (coll.layouts || []).map((l) => `${l.name} (${l.type})`).join(", ") || "No layouts";
            card.innerHTML = `
                <div class="collection-card-header">
                    <h3 class="collection-card-title">${esc(coll.name)}</h3>
                    <span class="collection-card-scope">${esc(coll.file_scope)}</span>
                </div>
                <div class="collection-card-meta">
                    <span>${coll.archive_count} archive${coll.archive_count !== 1 ? "s" : ""}</span>
                    <span>${coll.file_count} file${coll.file_count !== 1 ? "s" : ""}</span>
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
        } catch (e) {
            alert("Failed to load collection: " + e.message);
        }
    }

    function renderCollectionDetail(coll) {
        $("#collection-detail-title").textContent = coll.name;
        const scopeLabel = { processed: "Processed files", downloaded: "Downloaded files", both: "All files" }[coll.file_scope] || coll.file_scope;
        let meta = `${coll.file_count} files \u2022 ${scopeLabel}`;
        if (coll.auto_tag) meta += ` \u2022 auto-tag: ${coll.auto_tag}`;
        $("#collection-detail-meta").textContent = meta;

        // Layouts
        const layoutsEl = $("#collection-layouts");
        layoutsEl.innerHTML = "";
        const layouts = coll.layouts || [];
        if (layouts.length === 0) {
            layoutsEl.innerHTML = '<p class="empty-hint">No layouts configured. Add a layout to define how files are organized.</p>';
        } else {
            for (const layout of layouts) {
                const div = document.createElement("div");
                div.className = "layout-card";
                const typeLabel = { flat: "Flat", alphabetical: "Alphabetical (A\u2013Z)", by_archive: "By Archive" }[layout.type] || layout.type;
                div.innerHTML = `
                    <div class="layout-card-info">
                        <strong>${esc(layout.name)}</strong>
                        <span class="layout-type-badge">${esc(typeLabel)}</span>
                    </div>
                    <div class="layout-card-actions">
                        <button class="action-btn action-btn-sm" data-edit-layout="${layout.id}" data-name="${esc(layout.name)}" data-type="${layout.type}">Edit</button>
                        <button class="action-btn action-btn-sm batch-btn-danger" data-delete-layout="${layout.id}">Delete</button>
                    </div>
                `;
                layoutsEl.appendChild(div);
            }
            layoutsEl.addEventListener("click", (e) => {
                const editBtn = e.target.closest("[data-edit-layout]");
                if (editBtn) {
                    editingLayoutId = parseInt(editBtn.dataset.editLayout);
                    openLayoutModal(editBtn.dataset.name, editBtn.dataset.type);
                    return;
                }
                const delBtn = e.target.closest("[data-delete-layout]");
                if (delBtn) {
                    deleteLayout(parseInt(delBtn.dataset.deleteLayout));
                }
            });
        }

        // Archives
        const archivesEl = $("#collection-archives");
        archivesEl.innerHTML = "";
        const collArchives = coll.archives || [];
        if (collArchives.length === 0) {
            archivesEl.innerHTML = '<p class="empty-hint">No archives in this collection. Click "Add Archives" to get started.</p>';
        } else {
            for (const a of collArchives) {
                const div = document.createElement("div");
                div.className = "collection-archive-item";
                div.innerHTML = `
                    <div class="collection-archive-info">
                        <strong>${esc(a.title || a.identifier)}</strong>
                        <span class="collection-archive-meta">${a.identifier} \u2022 ${a.file_count} files${!a.manual ? " \u2022 auto-tag" : ""}</span>
                    </div>
                    ${a.manual ? `<button class="action-btn action-btn-sm batch-btn-danger" data-remove-archive="${a.id}">Remove</button>` : ""}
                `;
                archivesEl.appendChild(div);
            }
            archivesEl.addEventListener("click", (e) => {
                const btn = e.target.closest("[data-remove-archive]");
                if (btn) removeArchiveFromCollection(parseInt(btn.dataset.removeArchive));
            });
        }
    }

    function closeCollectionDetail() {
        currentCollectionId = null;
        openCollections();
    }

    // --- Collection CRUD Modals ---

    function openCollectionModal(coll = null) {
        editingCollectionId = coll ? coll.id : null;
        $("#modal-collection-title").textContent = coll ? "Edit Collection" : "New Collection";
        $("#collection-name-input").value = coll ? coll.name : "";
        $("#collection-scope-input").value = coll ? coll.file_scope : "processed";
        $("#collection-autotag-input").value = coll ? (coll.auto_tag || "") : "";
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
        const body = {
            name,
            file_scope: $("#collection-scope-input").value,
            auto_tag: $("#collection-autotag-input").value.trim(),
        };
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

    // --- Add Archives Modal ---

    function openAddArchivesModal() {
        if (!currentCollectionId) return;
        const listEl = $("#add-archives-list");
        const searchEl = $("#add-archives-search");
        searchEl.value = "";
        listEl.innerHTML = "";

        function render(filter = "") {
            listEl.innerHTML = "";
            const lf = filter.toLowerCase();
            // Get the collection's current archive IDs from the rendered detail
            const currentArchiveEls = $$("#collection-archives [data-remove-archive]");
            const currentIds = new Set();
            currentArchiveEls.forEach((el) => currentIds.add(parseInt(el.dataset.removeArchive)));

            for (const a of archives) {
                if (lf && !(a.identifier || "").toLowerCase().includes(lf) && !(a.title || "").toLowerCase().includes(lf)) continue;
                const inColl = currentIds.has(a.id);
                const div = document.createElement("div");
                div.className = "add-archive-item" + (inColl ? " in-collection" : "");
                div.innerHTML = `
                    <div class="add-archive-info">
                        <strong>${esc(a.title || a.identifier)}</strong>
                        <span>${a.identifier}</span>
                    </div>
                    ${inColl
                        ? '<span class="add-archive-badge">Added</span>'
                        : `<button class="action-btn action-btn-sm primary" data-add-archive="${a.id}">Add</button>`
                    }
                `;
                listEl.appendChild(div);
            }
        }

        render();
        searchEl.addEventListener("input", () => render(searchEl.value));
        listEl.addEventListener("click", async (e) => {
            const btn = e.target.closest("[data-add-archive]");
            if (!btn) return;
            const aid = parseInt(btn.dataset.addArchive);
            try {
                await api("POST", `/api/collections/${currentCollectionId}/archives`, { archive_id: aid });
                btn.replaceWith(Object.assign(document.createElement("span"), { className: "add-archive-badge", textContent: "Added" }));
                btn.closest(".add-archive-item").classList.add("in-collection");
                // Refresh detail in background
                openCollectionDetail(currentCollectionId);
            } catch (e) {
                alert(e.message);
            }
        });
        $("#modal-add-archives").classList.add("open");
    }

    function closeAddArchivesModal() {
        $("#modal-add-archives").classList.remove("open");
    }

    async function removeArchiveFromCollection(archiveId) {
        if (!currentCollectionId) return;
        try {
            await api("DELETE", `/api/collections/${currentCollectionId}/archives/${archiveId}`);
            openCollectionDetail(currentCollectionId);
        } catch (e) {
            alert(e.message);
        }
    }

    // --- Layout Modal ---

    function openLayoutModal(name = "", type = "flat") {
        const isEdit = !!editingLayoutId;
        $("#modal-layout-title").textContent = isEdit ? "Edit Layout" : "Add Layout";
        $("#layout-name-input").value = name;
        $("#layout-type-input").value = type;
        $("#layout-modal-error").textContent = "";
        $("#modal-add-layout").classList.add("open");
        $("#layout-name-input").focus();
    }

    function closeLayoutModal() {
        $("#modal-add-layout").classList.remove("open");
        editingLayoutId = null;
    }

    async function saveLayout() {
        if (!currentCollectionId) return;
        const name = $("#layout-name-input").value.trim();
        if (!name) {
            $("#layout-modal-error").textContent = "Name is required.";
            return;
        }
        const body = { name, type: $("#layout-type-input").value };
        try {
            if (editingLayoutId) {
                await api("PUT", `/api/collections/${currentCollectionId}/layouts/${editingLayoutId}`, body);
            } else {
                await api("POST", `/api/collections/${currentCollectionId}/layouts`, body);
            }
            closeLayoutModal();
            openCollectionDetail(currentCollectionId);
        } catch (e) {
            $("#layout-modal-error").textContent = e.message;
        }
    }

    async function deleteLayout(layoutId) {
        if (!currentCollectionId) return;
        if (!confirm("Delete this layout?")) return;
        try {
            await api("DELETE", `/api/collections/${currentCollectionId}/layouts/${layoutId}`);
            openCollectionDetail(currentCollectionId);
        } catch (e) {
            alert(e.message);
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
        // Activity Log
        $("#btn-activity").addEventListener("click", () => openActivityLog());
        $("#activity-filter-apply").addEventListener("click", () => { activityOffset = 0; loadActivityLog(); });
        $("#activity-filter-clear").addEventListener("click", clearActivityFilters);
        $("#activity-filter-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { activityOffset = 0; loadActivityLog(); } });
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
        $("#btn-add-archives-to-collection").addEventListener("click", openAddArchivesModal);
        $("#btn-add-archives-cancel").addEventListener("click", closeAddArchivesModal);
        $("#btn-add-layout").addEventListener("click", () => { editingLayoutId = null; openLayoutModal(); });
        $("#btn-layout-modal-cancel").addEventListener("click", closeLayoutModal);
        $("#btn-layout-modal-save").addEventListener("click", saveLayout);
        $("#layout-name-input").addEventListener("keydown", (e) => { if (e.key === "Enter") saveLayout(); });
        $("#btn-delete-collection").addEventListener("click", deleteCurrentCollection);
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

        // Archive batch actions
        $("#archive-batch-scan").addEventListener("click", archiveBatchScan);
        $("#archive-batch-process").addEventListener("click", archiveBatchProcess);
        $("#archive-batch-retry").addEventListener("click", archiveBatchRetry);
        $("#archive-batch-delete-folders").addEventListener("click", archiveBatchDeleteFolders);

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

        // Reset download order
        $("#btn-reset-queue-order").addEventListener("click", confirmResetOrder);

        // Virtual scroll for file table
        $(".file-table-wrap").addEventListener("scroll", () => {
            if (vsScrollRAF) return;
            vsScrollRAF = requestAnimationFrame(() => {
                vsScrollRAF = null;
                vsRenderVisible();
            });
        });

        // Detail
        $("#btn-back").addEventListener("click", closeDetail);
        $("#select-all-files").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
        $("#btn-retry-all").addEventListener("click", () => { if (currentArchiveId) retryArchive(currentArchiveId); });
        $("#btn-refresh-meta").addEventListener("click", refreshMetadata);
        $("#btn-scan-files").addEventListener("click", scanExistingFiles);
        $("#btn-process-all-files").addEventListener("click", openProcessArchiveModal);
        $("#btn-clear-changes").addEventListener("click", clearChanges);

        // Batch actions
        $("#batch-queue").addEventListener("click", batchQueueFiles);
        $("#batch-scan").addEventListener("click", batchScanFiles);
        $("#batch-process").addEventListener("click", batchProcessFiles);
        $("#batch-retry").addEventListener("click", batchRetryFiles);
        $("#batch-delete").addEventListener("click", batchDeleteFiles);

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

        // Close modals / settings on Escape
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
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
