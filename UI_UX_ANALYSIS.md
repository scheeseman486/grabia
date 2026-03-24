# Grabia UI/UX Analysis

## 1. Design Inconsistencies

### 1.1 Button Naming and Terminology

The application mixes terminology for similar concepts. "Select" was historically used for both UI checkbox selection (batch actions) and download queue membership. The column rename to `queued` resolves the backend, but the UI still has residual confusion:

- The "select all" checkbox in the file table header controls batch checkbox selection, while queue add/remove buttons control download queue membership. These are visually similar (both are per-row interactive controls) but do completely different things.
- Archive batch actions bar says "selected" (checkbox count), while file queue buttons say "Add to queue" / "Remove from queue." Consistent, but only if the user understands the two systems.
- Settings has "Download all files" checkbox (on add), which sets the initial queue state. The word "download" here means "queue for download," but elsewhere "download" means the active transfer.

### 1.2 Destructive Action Confirmation

Destructive actions use three different confirmation patterns:

- **Custom modals:** Delete archive, reset queue order, delete group. These have consistent styling with Cancel/Confirm buttons.
- **Browser `confirm()` dialogs:** Delete file, batch delete files, delete download folders. These look completely different from the custom modals and can't be styled.
- **No confirmation at all:** Stop downloads, cancel processing, clear change highlights, reorder operations.

This should be unified. All destructive actions should use custom modals with consistent danger styling.

### 1.3 Feedback and Loading States

Operations provide wildly different levels of feedback:

- **Good feedback:** Adding an archive shows a loading spinner with text. Scan progress shows percentage in notification panel.
- **Minimal feedback:** Play/pause/stop buttons have no transition state. Bandwidth limit changes are silently debounced with no confirmation. Settings save closes the panel with no "saved" toast.
- **No feedback:** Drag-to-reorder sends API calls silently. Move-to-group closes modal with no confirmation. File rename restores original text on failure with no error shown.

### 1.4 Notification Duplication

Every `addNotification()` call creates both a persistent entry in the notification panel AND a toast popup. The user sees the same message twice in two different places. The toast auto-dismisses after 5 seconds, but during rapid operations (batch retry, batch scan) the screen can flood with overlapping toasts while the same messages accumulate in the panel.

### 1.5 Settings Page vs Modal Pattern

Settings is a full-screen page with its own topbar, while every other overlay (add archive, delete confirmation, process archive, etc.) is a centered modal dialog. This makes settings feel like navigating away from the app rather than adjusting a panel. The user loses visual context of their archive list.

### 1.6 Status Color Semantics

File and archive statuses use color-coded badges, but the color meanings aren't documented anywhere in the UI. Users must learn through experience that green means completed, orange means downloading, red means failed, etc. There's also no legend or tooltip explaining the colors.

Additionally, `.archive-status.downloading` and `.archive-status.completed` share identical CSS (both use `--accent`), so downloading and completed archives are visually indistinguishable by color alone—only the text label differs.

---

## 2. User Flow Problems

### 2.1 Queue vs Download Confusion

The relationship between queuing and downloading isn't obvious:

1. User adds an archive. Files default to "queued."
2. User must separately enable the archive for download (toggle in archive list).
3. User must then click Play to start the download manager.
4. If the user clicks Play but nothing is enabled, nothing happens silently.

There's no guidance for new users about this three-step activation. An empty state or onboarding hint would help.

### 2.2 Pagination Breaks Reordering

In Download Order mode, users can drag files to reorder priority. But pagination limits this to the current page. If a user wants to move file #1 to position #75, they can't—drag only works within the visible page, and the move-up/move-down buttons move one position at a time. There's no way to jump a file to a specific position or drag across pages.

### 2.3 Sort Mode Changes Layout Dramatically

Switching to/from "Download Order" sort rebuilds the entire table header (adds/removes grip handles, priority columns, queue divider). This is disorienting if done accidentally. The sort dropdown doesn't preview what will change, and switching always resets to page 1.

### 2.4 Processing Modal Dead End

If no processing profiles exist, the Process Archive modal shows "No profiles — create one in Settings" as a disabled dropdown option. But the user can't navigate to Settings from within the modal. They must close the modal, open Settings, create a profile, navigate back, and re-open the modal.

### 2.5 Batch Operations Give No Per-Item Feedback

Batch scan, batch retry, and batch delete all show a single summary count ("Queued scan for 5 archive(s)"). The user doesn't know which items succeeded or failed, and for batch delete, the selection is cleared immediately after the operation with no way to see what was affected.

### 2.6 SSE Update Lag

Global progress updates are throttled to 2-second intervals, and file list refreshes during scans happen every 3 seconds. This means a file can complete downloading but the progress bar and file list don't reflect it for several seconds. The user sees file progress jump from, say, 80% directly to "completed" when the next refresh cycle hits.

### 2.7 No Recovery from Connection Loss

If the SSE connection drops (server restart, network hiccup), the app silently reconnects after 3 seconds. But during the gap, the UI is stale with no indication. There's no "connection lost" banner or "reconnecting..." state. The user might think downloads are frozen when really they just can't see the updates.

---

## 3. Missing Features

### 3.1 Global Retry

There's "Retry All" per-archive but no global "Retry all failed files across all archives." For a user managing dozens of archives, clicking Retry All on each one individually is tedious.

### 3.2 Archive Search/Filter

The archive list has no search or filter. With many archives, the user must scroll through the entire list to find one. There's file-level search within an archive, but not archive-level search on the main page.

### 3.3 Download Statistics

There's no historical view of download activity—total downloaded today, average speed over time, failed/succeeded ratio, etc. The speed sparkline shows only the current session's instantaneous speed with no persistence.

### 3.4 Keyboard Shortcuts

Only Enter-to-submit in text fields is handled. There are no keyboard shortcuts for common actions: Play/Pause (spacebar?), navigate archives (arrow keys), open archive (Enter), go back (Escape or Backspace), select all (Ctrl+A). A download manager is the kind of app where power users expect keyboard control.

### 3.5 Export/Import

No way to export the archive list (identifiers, queue state, groups) for backup or migration to another instance. The only backup path is copying the SQLite database directly.

### 3.6 Undo

No undo for any operation—delete file, remove from queue, rename, reorder. Every action is immediately committed. Even a simple "undo last action" covering the most recent destructive operation would help.

### 3.7 Bulk Group Assignment

Can only move one archive to a group at a time. With the checkbox batch selection system already in place for archives, a "Move to group" batch action is a natural addition.

### 3.8 Profile Cloning

Processing profiles must be created from scratch each time. No "Duplicate" button to clone an existing profile and tweak it.

### 3.9 Processing Queue Visibility

When files are queued for processing, there's no dedicated view showing the processing queue, order, and progress. The user must check individual archives or watch the notification panel.

### 3.10 Download Scheduling Preview

The speed schedule system allows time-based bandwidth rules, but there's no visual timeline showing when rules are active. A simple weekly heatmap or timeline bar would make it much easier to understand the effective schedule.

---

## 4. CSS and Visual Issues

### 4.1 Hardcoded Colors

Several places use hardcoded hex values instead of CSS variables: `#fff` for white text on buttons (should be a `--text-on-accent` variable), `#c44`/`#e55` for notification cancel buttons (should use `--danger`), `#9b59b6` for change highlights (purple, no variable). These won't adapt properly if theme colors are adjusted.

### 4.2 Responsive Design Gaps

There's only one breakpoint at 700px. No tablet breakpoint (around 1024px), no adjustments for large monitors, and no print styles. The file table in particular would benefit from a responsive treatment—on small screens the columns are cramped, and on very wide screens the table has unnecessary empty space (max-width: 1100px on main content).

### 4.3 Accessibility

- No `:focus-visible` styles anywhere. Keyboard users get no visible focus indicator on buttons and interactive elements.
- No `prefers-reduced-motion` media query. Animations play regardless of user preference.
- Status information conveyed by color alone (no icon or pattern differentiation for colorblind users).
- Drag handles have no keyboard alternative (can use move-up/down buttons, but these aren't keyboard-focused).
- Modals don't trap focus—Tab can escape the modal to background elements.

### 4.4 Typography

Font sizes are all px-based (not rem), so they don't respect browser zoom or user font size preferences. The sizes also don't follow a consistent scale—10px, 11px, 12px, 13px, 14px are all used for slightly different elements without clear hierarchy.

### 4.5 Scrollbar Styling

Only `-webkit-scrollbar` styles are defined (Chrome/Safari). Firefox users get default scrollbars that don't match the theme. Adding `scrollbar-width` and `scrollbar-color` would cover Firefox.

---

## 5. API Consistency Issues

### 5.1 Response Shape Variation

Different endpoints return different shapes for similar operations:

- Toggle operations return `{"ok": true}` with no data
- Archive mutations (toggle download, set group) return the full archive object
- Batch operations return `{"ok": true, "deleted": count}` or `{"reset_count": count}`
- List endpoints return bare arrays (archives, groups) or wrapped objects (files with pagination)

A consistent envelope format would make the frontend simpler and more predictable.

### 5.2 Missing Input Validation

Settings accepts any string for numeric fields (max_retries, retry_delay, bandwidth_limit, files_per_page). Invalid values cause server-side crashes on `int()` conversion rather than returning validation errors. The download directory isn't validated as writable. The debug log file path is validated for containment but not for writeability.

### 5.3 Redundant Endpoints

The backwards-compatible `/select` and `/select-all` endpoints should be removed once the frontend is confirmed to use the new `/queue` and `/queue-all` endpoints. They add confusion and maintenance burden.

---

## 6. Suggested Priorities

### High Impact, Low Effort
- Add archive search/filter to the main page
- Replace `confirm()` dialogs with custom modals
- Add "Saved" toast after settings save
- Add connection-lost banner for SSE disconnection
- Fix notification duplication (choose toast OR panel, not both)
- Add focus-visible styles for keyboard accessibility

### High Impact, Medium Effort
- Add global retry all failed
- Add keyboard shortcuts for play/pause/stop and navigation
- Add per-item feedback for batch operations
- Bulk group assignment from archive batch actions
- Processing profile clone button

### High Impact, Higher Effort
- Archive search and filter with status/group filters
- Download statistics dashboard
- Undo system for recent destructive actions
- Processing queue view
- Speed schedule visual timeline
- Full responsive overhaul with tablet breakpoint
