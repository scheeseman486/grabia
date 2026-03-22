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
    let currentPage = 1;
    let currentSort = "priority";
    let fileSearchQuery = "";
    let fileSearchTimer = null;
    let dlState = "stopped";
    let dragSrcId = null;
    let dragSrcGroupId = null;
    // Groups collapsed by default; store *expanded* group IDs in localStorage
    let expandedGroups = new Set(
        JSON.parse(localStorage.getItem("grabia_expanded_groups") || "[]")
    );

    function saveExpandedGroups() {
        localStorage.setItem("grabia_expanded_groups", JSON.stringify([...expandedGroups]));
    }
    let realBandwidth = -1; // tracks the actual backend bandwidth setting
    let lastProgressRefresh = 0; // timestamp of last throttled progress refresh

    // --- Notifications ---
    let notifications = [];
    let notifIdCounter = 0;

    function addNotification(message, type = "info") {
        const notif = { id: ++notifIdCounter, message, type, time: new Date() };
        notifications.unshift(notif);
        renderNotifBadge();
        renderNotifList();
        showToast(message, type);
    }

    function removeNotification(id) {
        notifications = notifications.filter((n) => n.id !== id);
        renderNotifBadge();
        renderNotifList();
    }

    function clearAllNotifications() {
        notifications = [];
        renderNotifBadge();
        renderNotifList();
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
            const ago = formatTimeAgo(n.time);
            const hasProgress = n.progress !== undefined;
            let progressHtml = "";
            if (hasProgress) {
                if (n.progress >= 0) {
                    progressHtml = `<div class="notif-progress-track"><div class="notif-progress-fill" style="width:${n.progress}%"></div></div>`;
                } else {
                    progressHtml = `<div class="notif-progress-track"><div class="notif-progress-fill indeterminate"></div></div>`;
                }
            }
            const cancelHtml = n.scanArchiveId
                ? `<button class="notif-cancel" data-cancel-archive="${n.scanArchiveId}">Cancel</button>`
                : "";
            div.innerHTML = `
                <div class="notif-content">
                    <span class="notif-message">${escapeHtml(n.message)}</span>
                    ${progressHtml}
                    <span class="notif-time-row">
                        <span class="notif-time">${ago}</span>
                        ${cancelHtml}
                    </span>
                </div>
                <button class="notif-dismiss" data-notif-id="${n.id}" title="Dismiss">
                    <svg viewBox="0 0 24 24" width="12" height="12"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
                </button>
            `;
            div.querySelector(".notif-dismiss").addEventListener("click", (e) => {
                e.stopPropagation();
                removeNotification(n.id);
            });
            const cancelBtn = div.querySelector(".notif-cancel");
            if (cancelBtn) {
                cancelBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    cancelScan(parseInt(cancelBtn.dataset.cancelArchive));
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

    // --- Scan Progress Notification ---

    // Track progress notification IDs per archive
    let scanNotifs = {}; // archive_id -> notif id
    let scanQueuedNotifs = {}; // archive_id -> notif id for "queued" message
    let scanLastRefresh = {}; // archive_id -> timestamp of last UI refresh
    const SCAN_REFRESH_INTERVAL = 3000; // refresh file list every 3s during scan

    function getArchiveName(archiveId) {
        const a = archives.find((x) => x.id === archiveId);
        return a ? (a.title || a.identifier) : "Archive #" + archiveId;
    }

    function updateScanProgress(data) {
        const { archive_id, phase, current, total } = data;
        const archiveName = getArchiveName(archive_id);

        if (phase === "verify") {
            const pct = total > 0 ? Math.round((current / total) * 100) : 0;
            const msg = `Scanning "${archiveName}": ${current}/${total} (${pct}%)`;
            if (!scanNotifs[archive_id]) {
                // Remove "queued" notification if present
                if (scanQueuedNotifs[archive_id]) {
                    notifications = notifications.filter((n) => n.id !== scanQueuedNotifs[archive_id]);
                    delete scanQueuedNotifs[archive_id];
                }
                // Create progress notification with toast
                const nid = ++notifIdCounter;
                scanNotifs[archive_id] = nid;
                const notif = { id: nid, message: msg, type: "info", time: new Date(), progress: pct, scanArchiveId: archive_id };
                notifications.unshift(notif);
                renderNotifBadge();
                renderNotifList();
                showToast(`Scan started: "${archiveName}"`, "info");
            } else {
                const notif = notifications.find((n) => n.id === scanNotifs[archive_id]);
                if (notif) {
                    notif.message = msg;
                    notif.progress = pct;
                    renderNotifList();
                }
            }
            // Periodically refresh file list & archive sidebar during scan
            const now = Date.now();
            if (!scanLastRefresh[archive_id] || now - scanLastRefresh[archive_id] >= SCAN_REFRESH_INTERVAL) {
                scanLastRefresh[archive_id] = now;
                if (currentArchiveId === archive_id) loadFiles();
                refreshArchives();
            }
        } else if (phase === "disk") {
            const notif = notifications.find((n) => n.id === scanNotifs[archive_id]);
            if (notif) {
                notif.message = `Scanning "${archiveName}": checking for unknown files...`;
                notif.progress = -1;
                renderNotifList();
            }
        } else if (phase === "done") {
            // Remove progress and queued notifications
            if (scanNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanNotifs[archive_id]);
                delete scanNotifs[archive_id];
            }
            if (scanQueuedNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanQueuedNotifs[archive_id]);
                delete scanQueuedNotifs[archive_id];
            }
            delete scanLastRefresh[archive_id];
            updateScanButton();
            // Add final summary notification
            const s = data.summary || {};
            const parts = [];
            if (s.matched > 0) parts.push(`${s.matched} matched`);
            if (s.partial > 0) parts.push(`${s.partial} partial`);
            if (s.conflict > 0) parts.push(`${s.conflict} conflict`);
            if (s.unknown > 0) parts.push(`${s.unknown} unknown`);
            if (s.missing > 0) parts.push(`${s.missing} not on disk`);
            if (parts.length === 0) {
                addNotification(`Scan "${archiveName}": no files found on disk`, "info");
            } else {
                const type = s.conflict > 0 || s.unknown > 0 ? "warning" : "success";
                addNotification(`Scan "${archiveName}": ` + parts.join(", "), type);
            }
            loadFiles();
            refreshArchives();
        } else if (phase === "cancelled") {
            if (scanNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanNotifs[archive_id]);
                delete scanNotifs[archive_id];
            }
            if (scanQueuedNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanQueuedNotifs[archive_id]);
                delete scanQueuedNotifs[archive_id];
            }
            delete scanLastRefresh[archive_id];
            addNotification(`Scan "${archiveName}": cancelled`, "info");
            updateScanButton();
            loadFiles();
            refreshArchives();
        } else if (phase === "error") {
            if (scanNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanNotifs[archive_id]);
                delete scanNotifs[archive_id];
            }
            if (scanQueuedNotifs[archive_id]) {
                notifications = notifications.filter((n) => n.id !== scanQueuedNotifs[archive_id]);
                delete scanQueuedNotifs[archive_id];
            }
            delete scanLastRefresh[archive_id];
            addNotification(`Scan "${archiveName}" failed: ${data.error || "Unknown error"}`, "error");
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
    const emptyState = $("#empty-state");
    const fileListEl = $("#file-list");
    const paginationEl = $("#pagination");
    const statusIndicator = $("#status-indicator");
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

    function connectSSE() {
        const es = new EventSource("/api/events");

        es.addEventListener("status", (e) => {
            const data = JSON.parse(e.data);
            updateStatus(data);
        });

        es.addEventListener("state", (e) => {
            const data = JSON.parse(e.data);
            dlState = data;
            updateControlButtons();
            updateStatusIndicator();
            syncBandwidthToState();
            if (dlState !== "running") {
                speedDisplay.textContent = "";
                clearSparkline();
            }
        });

        es.addEventListener("file_progress", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "downloading", downloaded_bytes: data.downloaded, size: data.size });
            speedDisplay.textContent = formatSpeed(data.speed);
            pushSpeed(data.speed || 0);
            throttledProgressRefresh();
        });

        es.addEventListener("file_complete", (e) => {
            const data = JSON.parse(e.data);
            updateFileRow(data.file_id, { download_status: "completed" });
            lastProgressRefresh = 0; // force immediate refresh
            throttledProgressRefresh();
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

        es.addEventListener("archive_added", () => refreshArchives());
        es.addEventListener("archive_updated", () => refreshArchives());
        es.addEventListener("archive_removed", () => refreshArchives());
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

        es.onerror = () => {
            setTimeout(connectSSE, 3000);
            es.close();
        };
    }

    function updateStatus(data) {
        dlState = data.state;
        updateControlButtons();
        updateStatusIndicator();
        syncBandwidthToState();
        if (dlState === "running" && data.current_file && data.current_speed) {
            speedDisplay.textContent = formatSpeed(data.current_speed);
        } else {
            speedDisplay.textContent = "";
        }
        updateGlobalProgress(data.progress);
    }

    function updateStatusIndicator() {
        statusIndicator.className = "status-indicator " + dlState;
        const text = statusIndicator.querySelector(".status-text");
        text.textContent = dlState.charAt(0).toUpperCase() + dlState.slice(1);
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
        try {
            archives = await api("GET", "/api/archives");
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
            <div class="archive-checkbox" title="Enable download">
                <input type="checkbox" ${a.download_enabled ? "checked" : ""} data-action="toggle-dl">
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
            if (action === "toggle-dl") {
                toggleArchiveDownload(a.id, e.target.checked);
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
            dragSrcId = a.id;
            dragSrcGroupId = null; // not a group drag
            li.classList.add("dragging");
            e.dataTransfer.effectAllowed = "move";
        });
        li.addEventListener("dragend", () => {
            li.classList.remove("dragging");
            $$(".archive-item").forEach((el) => el.classList.remove("drag-over"));
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
                if (src && src.download_enabled === a.download_enabled) {
                    const order = archives.map((x) => x.id);
                    const fromIdx = order.indexOf(dragSrcId);
                    const toIdx = order.indexOf(a.id);
                    order.splice(fromIdx, 1);
                    order.splice(toIdx, 0, dragSrcId);
                    api("POST", "/api/archives/reorder", { order });
                }
                dragSrcId = null;
            }
        });

        return li;
    }

    function renderArchiveList() {
        if (archives.length === 0 && groups.length === 0) {
            emptyState.style.display = "flex";
            archiveListEl.style.display = "none";
            return;
        }
        emptyState.style.display = "none";
        archiveListEl.style.display = "flex";
        archiveListEl.innerHTML = "";

        // Render groups first
        groups.forEach((g, gIdx) => {
            const groupArchives = archives.filter((a) => a.group_id === g.id);
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
                dragSrcGroupId = g.id;
                dragSrcId = null;
                header.classList.add("dragging");
                e.dataTransfer.effectAllowed = "move";
            });
            header.addEventListener("dragend", () => {
                header.classList.remove("dragging");
                $$(".group-header").forEach((el) => el.classList.remove("drag-over"));
            });
            header.addEventListener("dragover", (e) => {
                if (dragSrcGroupId === null) return; // only accept group drags
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                header.classList.add("drag-over");
            });
            header.addEventListener("dragleave", () => header.classList.remove("drag-over"));
            header.addEventListener("drop", (e) => {
                e.preventDefault();
                header.classList.remove("drag-over");
                if (dragSrcGroupId !== null && dragSrcGroupId !== g.id) {
                    const order = groups.map((x) => x.id);
                    const fromIdx = order.indexOf(dragSrcGroupId);
                    const toIdx = order.indexOf(g.id);
                    order.splice(fromIdx, 1);
                    order.splice(toIdx, 0, dragSrcGroupId);
                    api("POST", "/api/groups/reorder", { order });
                    dragSrcGroupId = null;
                }
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

        // Divider between groups and loose archives
        const looseArchives = archives.filter((a) => !a.group_id);
        if (groups.length > 0 && looseArchives.length > 0) {
            const divider = document.createElement("li");
            divider.className = "group-divider";
            archiveListEl.appendChild(divider);
        }

        // Ungrouped archives
        looseArchives.forEach((a, idx) => {
            archiveListEl.appendChild(buildArchiveItem(a, idx, looseArchives));
        });
    }

    async function toggleArchiveDownload(id, enabled) {
        await api("POST", `/api/archives/${id}/download`, { enabled });
        await refreshArchives();
        refreshStatus();
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
            btn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0020 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74A7.93 7.93 0 004 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z" fill="currentColor"></path></svg> Refresh Metadata';
        }
    }

    async function scanExistingFiles() {
        if (!currentArchiveId) return;
        const archiveName = getArchiveName(currentArchiveId);
        try {
            const aid = currentArchiveId;
            await api("POST", `/api/archives/${aid}/scan`);
            // Scan is now queued — track the queued notification so we can remove it when progress starts
            const qid = ++notifIdCounter;
            scanQueuedNotifs[aid] = qid;
            notifications.unshift({ id: qid, message: `Scan "${archiveName}": queued`, type: "info", time: new Date() });
            renderNotifBadge();
            renderNotifList();
            showToast(`Scan "${archiveName}": queued`, "info");
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

    function updateScanButton() {
        const btn = $("#btn-scan-files");
        if (!btn) return;
        const active = currentArchiveId && (scanNotifs[currentArchiveId] || scanQueuedNotifs[currentArchiveId]);
        btn.disabled = !!active;
        btn.style.opacity = active ? "0.5" : "";
        btn.title = active
            ? "Scan already in progress or queued for this archive"
            : "Scan local folder for existing files that match this archive";
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
        currentPage = 1;
        currentSort = "priority";
        fileSearchQuery = "";
        $("#file-sort").value = "priority";
        $("#file-search").value = "";
        pageHome.classList.remove("active");
        pageDetail.classList.add("active");

        const archive = archives.find((a) => a.id === id);
        if (archive) {
            $("#detail-title").textContent = archive.title || archive.identifier;
            $("#detail-meta").textContent = `${archive.files_count} files \u2022 ${formatBytes(archive.total_size)} \u2022 ${archive.identifier}`;
            updateDetailProgress();
        }
        await loadFiles();
        updateScanButton();
    }

    function closeDetail() {
        currentArchiveId = null;
        pageDetail.classList.remove("active");
        pageHome.classList.add("active");
        refreshArchives();
    }

    async function loadFiles() {
        if (!currentArchiveId) return;
        try {
            const searchParam = fileSearchQuery ? `&search=${encodeURIComponent(fileSearchQuery)}` : "";
            const data = await api("GET", `/api/archives/${currentArchiveId}/files?page=${currentPage}&sort=${currentSort}${searchParam}`);
            renderFiles(data);
            if (data.progress) updateDetailProgressFromData(data.progress);
        } catch (e) {
            fileListEl.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--danger)">${escapeHtml(e.message)}</td></tr>`;
        }
    }

    function syncSelectAll() {
        const boxes = fileListEl.querySelectorAll("input[type=checkbox]");
        const all = boxes.length > 0 && Array.from(boxes).every((cb) => cb.checked);
        $("#select-all-files").checked = all;
    }

    // --- File table header (dynamic based on sort mode) ---

    function rebuildTableHeader() {
        const isPriority = currentSort === "priority";
        const thead = fileListEl.closest("table").querySelector("thead tr");
        thead.innerHTML = "";
        if (isPriority) {
            thead.innerHTML += '<th class="col-grip">' +
                '<button class="btn-reset-order" id="btn-reset-order" title="Reset download order to alphabetical">' +
                '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>' +
                '</button></th>';
        }
        thead.innerHTML += '<th class="col-check"><input type="checkbox" id="select-all-files" title="Select / deselect all files"></th>';
        thead.innerHTML += '<th class="col-name">Name</th>';
        thead.innerHTML += '<th class="col-size">Size</th>';
        thead.innerHTML += '<th class="col-modified">Modified</th>';
        thead.innerHTML += '<th class="col-status">Status</th>';
        if (isPriority) {
            thead.innerHTML += '<th class="col-priority"></th>';
        }
        // Re-attach handlers
        $("#select-all-files").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
        const resetBtn = document.getElementById("btn-reset-order");
        if (resetBtn) resetBtn.addEventListener("click", confirmResetOrder);
    }

    function getColspan() {
        return currentSort === "priority" ? 7 : 5;
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

        // Only allow reorder among selected files (the top group in priority mode)
        const thisSelected = rows[idx].querySelector("input[type=checkbox]").checked;
        const swapSelected = rows[swapIdx].querySelector("input[type=checkbox]").checked;
        if (!thisSelected || !swapSelected) return;

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

            // Only rearrange among selected rows
            const rows = Array.from(fileListEl.querySelectorAll("tr[data-file-id]"));
            const selectedRows = rows.filter((r) => r.querySelector("input[type=checkbox]").checked);
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

    // --- Render file list ---

    function renderFiles(data) {
        const { files, total, page, per_page, total_pages, all_selected } = data;
        const isPriority = currentSort === "priority";

        rebuildTableHeader();
        fileListEl.innerHTML = "";

        // Sync the select-all header checkbox with actual state
        $("#select-all-files").checked = !!all_selected;

        if (files.length === 0) {
            fileListEl.innerHTML = `<tr><td colspan="${getColspan()}" style="text-align:center;padding:20px;color:var(--text-muted)">No files found.</td></tr>`;
            paginationEl.innerHTML = "";
            return;
        }

        // In priority mode, identify the boundary between selected and unselected
        // (server already sorts selected DESC, priority ASC)
        const selectedFiles = isPriority ? files.filter((f) => f.selected && f.download_status !== "unknown") : [];
        const lastSelectedIdx = isPriority ? selectedFiles.length - 1 : -1;

        let hasChanges = false;
        files.forEach((f, idx) => {
            const tr = document.createElement("tr");
            tr.dataset.fileId = f.id;

            // Change highlight
            if (f.change_status) {
                hasChanges = true;
                tr.className = "file-row-" + f.change_status;
            }

            const changeIcon = f.change_status
                ? `<span class="change-info ${f.change_status}" aria-label="${escapeHtml(f.change_detail)}">` +
                  `<span class="change-tooltip">${escapeHtml(f.change_detail)}</span>` +
                  (f.change_status === "new" ? "+" : f.change_status === "removed" ? "\u2212" : "\u0394") +
                  `</span>`
                : "";

            let html = "";

            const isUnknown = f.download_status === "unknown";

            // Grip column (priority mode only, selected files only)
            if (isPriority) {
                html += (f.selected && !isUnknown) ? buildGripCell() : '<td class="col-grip"></td>';
            }

            if (isUnknown) {
                html += `<td class="col-check"><input type="checkbox" disabled title="Unknown file — not in archive manifest" data-file-id="${f.id}"></td>`;
            } else {
                html += `<td class="col-check"><input type="checkbox" ${f.selected ? "checked" : ""} data-file-id="${f.id}"></td>`;
            }
            html += `<td class="col-name"><span class="file-name">${escapeHtml(f.name)}</span>${changeIcon}</td>`;
            html += `<td class="col-size" style="text-align:right">${formatBytes(f.size)}</td>`;
            html += `<td class="col-modified">${formatDate(f.mtime)}</td>`;
            const displayStatus = formatFileStatus(f);
            const statusClass = displayStatus === "skipped" ? "skipped" : f.download_status;
            const hasError = (f.download_status === "failed" || f.download_status === "conflict" || f.download_status === "unknown") && f.error_message;
            const isConflict = f.download_status === "conflict";
            html += `<td class="col-status">` +
                `<span class="file-status ${statusClass}" ${hasError ? `title="${escapeHtml(f.error_message)}"` : ""}>${displayStatus}</span>` +
                (hasError && isConflict
                    ? `<span class="file-error-hint clickable" data-conflict-file='${JSON.stringify({id: f.id, name: f.name, size: f.size, error: f.error_message})}' title="Click to resolve conflict">&#9432;</span>`
                    : hasError ? `<span class="file-error-hint" title="${escapeHtml(f.error_message)}">&#9432;</span>` : "") +
                (f.download_status === "failed" ? `<button class="retry-file-btn" data-retry-file="${f.id}" title="Retry this file">&#x21bb;</button>` : "") +
                `</td>`;

            // Priority buttons (priority mode only, selected files only)
            if (isPriority) {
                if (f.selected && !isUnknown) {
                    const selIdx = selectedFiles.indexOf(f);
                    html += buildPriorityCell(f.id, selIdx === 0, selIdx === lastSelectedIdx);
                } else {
                    html += '<td class="col-priority"></td>';
                }
            }

            tr.innerHTML = html;

            // Checkbox handler (skip for unknown files)
            if (!isUnknown) {
                tr.querySelector("input[type=checkbox]").addEventListener("change", (e) => {
                    api("POST", `/api/files/${f.id}/select`, { selected: e.target.checked }).then(() => {
                        if (isPriority) loadFiles(); // re-sort grouping
                    });
                    syncSelectAll();
                });
            }

            // Retry handler
            const retryBtn = tr.querySelector(".retry-file-btn");
            if (retryBtn) {
                retryBtn.addEventListener("click", () => retryFile(f.id));
            }

            // Conflict resolve handler
            const conflictHint = tr.querySelector(".file-error-hint.clickable");
            if (conflictHint) {
                conflictHint.addEventListener("click", () => {
                    const info = JSON.parse(conflictHint.dataset.conflictFile);
                    openForceResume(info);
                });
            }

            // Priority mode: drag & priority button handlers for selected files
            if (isPriority && f.selected && !isUnknown) {
                attachPriorityDrag(tr, f.id);
                const upBtn = tr.querySelector(`[data-move-up="${f.id}"]`);
                const downBtn = tr.querySelector(`[data-move-down="${f.id}"]`);
                if (upBtn) upBtn.addEventListener("click", () => moveFile(f.id, -1));
                if (downBtn) downBtn.addEventListener("click", () => moveFile(f.id, 1));
            }

            fileListEl.appendChild(tr);
        });

        // Pagination
        paginationEl.innerHTML = "";
        if (total_pages > 1) {
            const prevBtn = document.createElement("button");
            prevBtn.textContent = "\u2190 Prev";
            prevBtn.disabled = page <= 1;
            prevBtn.addEventListener("click", () => { currentPage--; loadFiles(); });
            paginationEl.appendChild(prevBtn);

            for (let i = 1; i <= total_pages; i++) {
                if (total_pages > 10 && Math.abs(i - page) > 2 && i !== 1 && i !== total_pages) {
                    if (i === page - 3 || i === page + 3) {
                        const dots = document.createElement("button");
                        dots.textContent = "...";
                        dots.disabled = true;
                        paginationEl.appendChild(dots);
                    }
                    continue;
                }
                const btn = document.createElement("button");
                btn.textContent = i;
                btn.className = i === page ? "active" : "";
                btn.addEventListener("click", () => { currentPage = i; loadFiles(); });
                paginationEl.appendChild(btn);
            }

            const nextBtn = document.createElement("button");
            nextBtn.textContent = "Next \u2192";
            nextBtn.disabled = page >= total_pages;
            nextBtn.addEventListener("click", () => { currentPage++; loadFiles(); });
            paginationEl.appendChild(nextBtn);
        }

        // Show/hide clear highlights button
        $("#btn-clear-changes").style.display = hasChanges ? "" : "none";
    }

    function formatFileStatus(f) {
        if (!f.selected && f.download_status === "pending") {
            return "skipped";
        }
        if (f.download_status === "downloading" && f.size > 0) {
            const pct = ((f.downloaded_bytes / f.size) * 100).toFixed(1);
            return `${pct}%`;
        }
        return f.download_status;
    }

    function updateFileRow(fileId, updates) {
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

    async function addArchive() {
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
            $("#add-error").textContent = "";
            $("#add-loading").style.display = "flex";
            $("#btn-add-confirm").disabled = true;

            let succeeded = 0;
            let failed = 0;
            const errors = [];
            for (let i = 0; i < lines.length; i++) {
                $("#add-loading-text").textContent = `Processing ${i + 1} of ${lines.length}...`;
                try {
                    await api("POST", "/api/archives", {
                        url: lines[i],
                        enable,
                        select_all: selectAll,
                        group_id: groupId,
                    });
                    succeeded++;
                } catch (e) {
                    failed++;
                    errors.push(`${lines[i]}: ${e.message}`);
                }
            }

            $("#add-loading").style.display = "none";
            $("#add-loading-text").textContent = "Fetching metadata...";
            $("#btn-add-confirm").disabled = false;
            await refreshArchives();

            if (failed === 0) {
                addNotification(`Batch add: ${succeeded} archive${succeeded !== 1 ? "s" : ""} added`, "success");
                closeAddModal();
            } else {
                addNotification(`Batch add: ${succeeded} added, ${failed} failed`, "warning");
                $("#add-error").innerHTML = errors.map((e) => escapeHtml(e)).join("<br>");
            }
        } else {
            const url = $("#input-add-url").value.trim();
            if (!url) {
                $("#add-error").textContent = "Please enter a URL or identifier.";
                return;
            }
            $("#add-error").textContent = "";
            $("#add-loading").style.display = "flex";
            $("#btn-add-confirm").disabled = true;

            try {
                await api("POST", "/api/archives", {
                    url,
                    enable,
                    select_all: selectAll,
                    group_id: groupId,
                });
                closeAddModal();
                await refreshArchives();
            } catch (e) {
                $("#add-error").textContent = e.message;
            } finally {
                $("#add-loading").style.display = "none";
                $("#btn-add-confirm").disabled = false;
            }
        }
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
            files_per_page: $("#set-files-per-page").value,
            sse_update_rate: $("#set-sse-update-rate").value,
            theme: $("#set-theme").value,
            use_http: $("#set-use-http").checked,
            confirm_reset_order: $("#set-confirm-reset-order").checked,
            default_enable_archive: $("#set-default-enable-archive").checked,
            default_select_all: $("#set-default-select-all").checked,
            schedule: JSON.stringify(collectScheduleRules()),
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
            $("#set-files-per-page").value = s.files_per_page || "50";
            $("#set-sse-update-rate").value = s.sse_update_rate || "500";
            $("#set-theme").value = s.theme || "dark";
            $("#set-use-http").checked = s.use_http === "1";
            $("#http-warning").style.display = s.use_http === "1" ? "block" : "none";
            $("#set-confirm-reset-order").checked = s.confirm_reset_order !== "0";
            $("#set-default-enable-archive").checked = s.default_enable_archive === "1";
            $("#set-default-select-all").checked = s.default_select_all !== "0";
            $("#set-old-password").value = "";
            $("#set-new-password").value = "";
            $("#pw-change-error").textContent = "";
            // Load schedule rules
            scheduleRules = JSON.parse(s.speed_schedule || "[]");
            renderScheduleRules();
            // Snapshot for dirty tracking
            settingsSnapshot = getSettingsFingerprint();
            $("#btn-settings-save-bottom").disabled = true;
            // Show settings page, hide others
            $$(".page").forEach((p) => p.classList.remove("active"));
            $("#page-settings").classList.add("active");
        } catch (e) {
            alert("Failed to load settings: " + e.message);
        }
    }

    function closeSettings() {
        $("#page-settings").classList.remove("active");
        if (currentArchiveId) {
            $("#page-detail").classList.add("active");
        } else {
            $("#page-home").classList.add("active");
        }
    }

    function switchTab(tabId) {
        $$(".settings-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tabId));
        $$(".settings-panel").forEach((p) => p.classList.toggle("active", p.id === tabId));
    }

    async function saveSettings() {
        const data = {
            ia_email: $("#set-ia-email").value,
            ia_password: $("#set-ia-password").value,
            download_dir: $("#set-download-dir").value,
            max_retries: $("#set-max-retries").value,
            retry_delay: $("#set-retry-delay").value,
            files_per_page: $("#set-files-per-page").value,
            sse_update_rate: $("#set-sse-update-rate").value,
            theme: $("#set-theme").value,
            use_http: $("#set-use-http").checked ? "1" : "0",
            confirm_reset_order: $("#set-confirm-reset-order").checked ? "1" : "0",
            default_enable_archive: $("#set-default-enable-archive").checked ? "1" : "0",
            default_select_all: $("#set-default-select-all").checked ? "1" : "0",
            speed_schedule: JSON.stringify(collectScheduleRules()),
        };
        try {
            await api("POST", "/api/settings", data);
            applyTheme(data.theme);
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

    async function toggleSelectAll(checked) {
        if (!currentArchiveId) return;
        await api("POST", `/api/archives/${currentArchiveId}/files/select-all`, { selected: checked });
        loadFiles();
    }

    // --- Reset Download Order ---

    let confirmResetSetting = true; // loaded from settings on init

    function confirmResetOrder() {
        if (!currentArchiveId) return;
        if (!confirmResetSetting) {
            doResetOrder();
            return;
        }
        $("#reset-order-suppress").checked = false;
        $("#modal-reset-order").classList.add("open");
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

    // --- Init ---

    function init() {
        // Controls
        $("#btn-play").addEventListener("click", () => {
            if (dlState === "paused") {
                // Clear the limiter when resuming from pause
                realBandwidth = -1;
                api("POST", "/api/download/bandwidth", { limit: -1 });
            }
            api("POST", "/api/download/start");
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

        // Reset download order modal
        $("#btn-reset-order-cancel").addEventListener("click", () => $("#modal-reset-order").classList.remove("open"));
        $("#btn-reset-order-confirm").addEventListener("click", () => {
            const suppress = $("#reset-order-suppress").checked;
            $("#modal-reset-order").classList.remove("open");
            if (suppress) {
                confirmResetSetting = false;
                api("POST", "/api/settings", { confirm_reset_order: "0" });
            }
            doResetOrder();
        });

        // Detail
        $("#btn-back").addEventListener("click", closeDetail);
        $("#select-all-files").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
        $("#btn-retry-all").addEventListener("click", () => { if (currentArchiveId) retryArchive(currentArchiveId); });
        $("#btn-refresh-meta").addEventListener("click", refreshMetadata);
        $("#btn-scan-files").addEventListener("click", scanExistingFiles);
        $("#btn-clear-changes").addEventListener("click", clearChanges);
        $("#file-sort").addEventListener("change", (e) => {
            currentSort = e.target.value;
            currentPage = 1;
            loadFiles();
        });
        $("#file-search").addEventListener("input", (e) => {
            clearTimeout(fileSearchTimer);
            fileSearchTimer = setTimeout(() => {
                fileSearchQuery = e.target.value.trim();
                currentPage = 1;
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
        connectSSE();

        // Set initial lock indicator and reset-order confirmation state
        api("GET", "/api/settings").then((s) => {
            updateLockIndicator(s.use_http === "1");
            confirmResetSetting = s.confirm_reset_order !== "0";
        }).catch(() => {});
    }

    document.addEventListener("DOMContentLoaded", init);
})();
