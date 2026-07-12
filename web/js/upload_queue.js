/* Manual "Upload now" batch control for the YouTube screen's Upload queue:
 * pick clips by top-score count or by hand, confirm exactly what will go
 * out, watch live per-clip progress, see a result summary. Reuses the same
 * eligibility/scoring/exclude state as the auto-upload scheduler — this is
 * an additional manual trigger into it, not a second selection system. */

import { api } from "./api.js";
import { confirmDialog, el, fmtBytes, fmtClock, thumbButton,
  toast } from "./ui.js";

const STATUS_LABEL = {
  pending: "Waiting", uploading: "Uploading…", done: "Done", failed: "Failed",
};
const STATUS_BADGE = {
  pending: "", uploading: "badge-live", done: "badge-ok", failed: "badge-warn",
};

export function mountUploadQueue(container) {
  const state = { candidates: [], pending: [], requireApproval: false,
                  selected: new Set(), endWatermark: null, cleanable: 0,
                  scheduled: [], published: [], quota: null, horizon: 3,
                  dryRun: false, zipStatus: null };
  let openDialog = null;   // tracked so navigating away can force-close it

  const dryBanner = el("div", { class: "uq-dryrun", style: "display:none" },
    "Dry run — uploads and schedules are simulated. Nothing reaches YouTube "
    + "(CLIPFORGE_DRY_RUN is set).");
  const zipPromptBanner = el("div", { class: "uq-dryrun", style: "display:none" });

  const countIn = el("input", { class: "input t-mono", type: "number",
                                min: "0", step: "1", style: "width:80px" });
  const countBtn = el("button", { class: "btn btn-primary", type: "button" },
    "Upload now");
  const selBtn = el("button", { class: "btn", type: "button" }, "Upload selected now");
  const delSelBtn = el("button", { class: "btn btn-danger", type: "button" },
    "Delete selected");
  const rowsWrap = el("div", { class: "uq-rows" });
  const emptyMsg = el("p", { class: "t-dim", style: "margin:0" },
    "No clips are waiting to publish right now.");
  const syncBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" },
    "Sync schedule");
  const quotaLine = el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-xs)" });
  const scheduledWrap = el("div", { class: "uq-rows" });
  const publishedWrap = el("div", { class: "uq-rows" });
  const cleanupBtn = el("button", { class: "btn btn-danger btn-sm",
                                    type: "button" }, "Clean up uploaded");
  const archiveBackfillBtn = el("button", { class: "btn btn-ghost btn-sm",
                                            type: "button" }, "Archive older uploads");
  const zipNowBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Zip archive now");
  const approveAllBtn = el("button", { class: "btn", type: "button" },
    "Approve all");
  const approvalsHead = el("div", { class: "uq-controls" },
    el("div", { class: "uq-section-label t-label" },
      "Awaiting approval — review before they can upload"),
    approveAllBtn);
  const approvalsWrap = el("div", { class: "uq-rows" });
  const approvalsSection = el("div", { style: "display:none" },
    approvalsHead, approvalsWrap, el("div", { style: "height:8px" }));

  container.append(
    dryBanner,
    zipPromptBanner,
    approvalsSection,
    el("div", { class: "uq-section-label t-label" }, "Queue — waiting to publish"),
    el("div", { class: "uq-controls" },
      el("div", { class: "field-inline" },
        el("span", { class: "t-label" }, "Upload"), countIn,
        el("span", { class: "t-dim" }, "of the best clips now"), countBtn),
      el("div", { class: "field-inline" }, selBtn, delSelBtn)),
    rowsWrap,
    el("div", { class: "uq-controls", style: "margin-top:8px" },
      el("div", {},
        el("div", { class: "uq-section-label t-label" },
          "Scheduled on YouTube — publishes with the app closed"),
        quotaLine),
      syncBtn),
    scheduledWrap,
    el("div", { class: "uq-controls", style: "margin-top:8px" },
      el("div", { class: "uq-section-label t-label" }, "Published"),
      el("div", { class: "field-inline" }, archiveBackfillBtn, zipNowBtn, cleanupBtn)),
    publishedWrap);

  countBtn.addEventListener("click", () => openConfirm("top", Number(countIn.value) || 0));
  selBtn.addEventListener("click", () => openConfirm("manual", 0, [...state.selected]));

  /* Shared delete path for every "remove from disk" action here. Queue clips
   * are approved and waiting to upload, so deleting them cancels a pending
   * publish — the confirm says so. Keeps the upload log (dedupe intact). */
  async function deleteKeys(keys, { approvedPending = false } = {}) {
    if (!keys.length) return;
    const bytes = state.candidates
      .filter((c) => keys.includes(c.key))
      .reduce((sum, c) => sum + (c.bytes || 0), 0);
    const ok = await confirmDialog({
      title: `Delete ${keys.length} clip${keys.length === 1 ? "" : "s"}?`,
      body: `This removes ${keys.length === 1 ? "its" : "their"} files from disk`
        + (bytes ? ` (${fmtBytes(bytes)})` : "") + ". "
        + (approvedPending
          ? "They're approved and waiting to upload — deleting cancels that. " : "")
        + "This can't be undone.",
    });
    if (!ok) return;
    try {
      const r = await api.del("/api/clips", { keys });
      const busy = r.results.filter((x) => x.status === "uploading").length;
      let msg = `Deleted ${r.deleted} — freed ${fmtBytes(r.reclaimed_bytes)}.`;
      if (busy) msg += ` ${busy} skipped (uploading).`;
      toast(msg, busy ? "is-error" : "is-ok");
      state.selected.clear();
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
  }

  delSelBtn.addEventListener("click", () =>
    deleteKeys([...state.selected], { approvedPending: state.requireApproval }));

  cleanupBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Clean up uploaded clips?",
      body: `This deletes the local files of everything already uploaded to `
        + `YouTube${state.cleanable ? ` (${fmtBytes(state.cleanable)})` : ""}. `
        + "They stay live on YouTube; only your local copies go. "
        + "This can't be undone.",
    });
    if (!ok) return;
    cleanupBtn.disabled = true;
    try {
      const r = await api.post("/api/youtube/cleanup-uploaded");
      toast(`Cleaned up ${r.deleted} clip${r.deleted === 1 ? "" : "s"} — `
        + `freed ${fmtBytes(r.reclaimed_bytes)}.`, "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    cleanupBtn.disabled = false;
  });
  archiveBackfillBtn.addEventListener("click", async () => {
    archiveBackfillBtn.disabled = true;
    try {
      const r = await api.post("/api/archive/backfill");
      toast(r.archived
        ? `Archived ${r.archived} clip${r.archived === 1 ? "" : "s"}.`
        : "Everything is already archived.", "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    archiveBackfillBtn.disabled = false;
  });

  /* Polls a zip-backup job to completion, reporting the result. */
  function pollZipJob(jobId) {
    return new Promise((resolve) => {
      const tick = async () => {
        let st;
        try { st = await api.get(`/api/archive/zip/${jobId}`); }
        catch { setTimeout(tick, 1000); return; }
        if (st.state === "running") { setTimeout(tick, 800); return; }
        if (st.state === "error") {
          toast(st.error || "The backup zip didn't work — try again.", "is-error");
        } else if (st.zipped) {
          toast(`Backed up ${st.zipped} clip${st.zipped === 1 ? "" : "s"} `
            + `into ${st.zip_name}.`, "is-ok");
        } else {
          toast("Nothing new to back up.", "is-ok");
        }
        resolve();
      };
      tick();
    });
  }

  /* Also guarded server-side (archive.create_backup_zip holds a lock), but
   * disabling here too avoids popping a second confirm dialog while a zip
   * job from the other trigger (banner vs. this button) is still running. */
  async function openZipDialog() {
    if (state.zipping) return;
    const delToggle = el("input", { type: "checkbox" });
    const body = el("div", { style: "display:grid;gap:16px" },
      el("p", { class: "t-dim", style: "margin:0" },
        `This zips every archived clip not already backed up `
        + `(${state.zipStatus ? state.zipStatus.since_last_zip : 0} right now) `
        + "into archive/backups/."),
      el("label", { class: "opt-toggle" },
        "Delete the zipped originals from archive/uploaded/ to reclaim space",
        el("span", { class: "switch" }, delToggle, el("span", { class: "knob" }))));
    const ok = await confirmDialog({
      title: "Create backup zip?", body, confirmLabel: "Zip now", danger: false,
    });
    if (!ok) return;
    state.zipping = true;
    zipNowBtn.disabled = true;
    try {
      const { job_id } = await api.post("/api/archive/zip",
        { delete_originals: delToggle.checked });
      await pollZipJob(job_id);
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    state.zipping = false;
    zipNowBtn.disabled = false;
  }
  zipNowBtn.addEventListener("click", openZipDialog);
  approveAllBtn.addEventListener("click", async () => {
    approveAllBtn.disabled = true;
    try {
      const r = await api.post("/api/youtube/approvals/all",
        { approval: "approved" });
      toast(`Approved ${r.updated} clip${r.updated === 1 ? "" : "s"}.`, "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    approveAllBtn.disabled = false;
  });

  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncBtn.textContent = "Scheduling…";
    try {
      const r = await api.post("/api/youtube/sync-schedule");
      toast(r.scheduled
        ? `Scheduled ${r.scheduled} clip${r.scheduled === 1 ? "" : "s"} `
          + "ahead — they'll publish on their own."
        : r.can_schedule_now === 0
          ? "Daily upload quota is used up — try again tomorrow."
          : "Nothing to schedule right now.",
        r.scheduled ? "is-ok" : "is-error");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    syncBtn.textContent = "Sync schedule";
    syncBtn.disabled = false;
  });

  async function refresh() {
    rowsWrap.replaceChildren(el("div", { class: "skeleton", style: "height:60px" }));
    let data, approvals, storage, zipStatus;
    try {
      [data, approvals, storage, zipStatus] = await Promise.all([
        api.get("/api/youtube/queue"), api.get("/api/youtube/approvals"),
        api.get("/api/storage").catch(() => null),
        api.get("/api/archive/zip-status").catch(() => null)]);
    } catch (e) {
      rowsWrap.replaceChildren(el("p", { class: "t-dim", style: "margin:0" }, e.message));
      return;
    }
    state.zipStatus = zipStatus;
    state.candidates = data.candidates;
    state.pending = approvals.items || [];
    state.requireApproval = !!data.require_approval;
    state.uploaded = data.uploaded || [];
    state.scheduled = data.scheduled || [];
    state.published = data.published || [];
    state.quota = data.quota || null;
    state.horizon = data.schedule_ahead_days ?? 3;
    state.dryRun = !!data.dry_run;
    state.cleanable = storage ? storage.cleanable_bytes : 0;
    state.endWatermark = data.end_watermark || null;
    state.selected = new Set([...state.selected].filter(
      (k) => data.candidates.some((c) => c.key === k)));
    const remaining = Math.max(0, (data.max_per_day ?? 3) - (data.uploads_today ?? 0));
    countIn.value = String(Math.min(remaining || 1, data.candidates.length));
    countIn.max = String(data.candidates.length);
    render();
  }

  function render() {
    dryBanner.style.display = state.dryRun ? "" : "none";
    zipNowBtn.disabled = !!state.zipping;
    if (state.zipStatus && state.zipStatus.should_prompt) {
      zipPromptBanner.style.display = "";
      const zipHereBtn = el("button", { class: "btn btn-sm", type: "button",
                                        disabled: state.zipping ? "" : null },
        "Create backup zip");
      zipHereBtn.addEventListener("click", openZipDialog);
      zipPromptBanner.replaceChildren(
        `${state.zipStatus.since_last_zip} clips archived since the last `
        + "backup — ", zipHereBtn);
    } else {
      zipPromptBanner.style.display = "none";
    }
    selBtn.disabled = delSelBtn.disabled = state.selected.size === 0;
    countBtn.disabled = state.candidates.length === 0;
    cleanupBtn.style.display = state.cleanable ? "" : "none";
    cleanupBtn.textContent = state.cleanable
      ? `Clean up uploaded (${fmtBytes(state.cleanable)})` : "Clean up uploaded";
    rowsWrap.replaceChildren(
      ...(state.candidates.length ? state.candidates.map(candidateRow)
        : [emptyMsg]));
    approvalsSection.style.display = state.pending.length ? "" : "none";
    approveAllBtn.textContent = `Approve all (${state.pending.length})`;
    approvalsWrap.replaceChildren(...state.pending.map(approvalRow));
    renderSchedule();
    renderPublished();
  }

  function renderSchedule() {
    const q = state.quota;
    const canNow = q ? q.can_schedule_now : 0;
    // honest quota math: each upload spends real API quota, ~6/day ceiling
    quotaLine.textContent = q
      ? `Can schedule ${canNow} more today `
        + `(${q.uploads_today}/${Math.min(q.uploads_per_day_by_quota, q.max_per_day)} used; `
        + `~${q.quota_per_upload.toLocaleString()} quota units each, `
        + `${q.quota_daily.toLocaleString()}/day). Books up to ${state.horizon} days ahead.`
      : "";
    syncBtn.disabled = canNow === 0 || state.candidates.length === 0;
    scheduledWrap.replaceChildren(...(state.scheduled.length
      ? state.scheduled.map(scheduledRow)
      : [el("p", { class: "t-dim", style: "margin:0" },
          "Nothing scheduled ahead yet. Approve clips, then Sync schedule.")]));
  }

  function renderPublished() {
    publishedWrap.replaceChildren(...(state.published.length
      ? state.published.map(uploadedRow)
      : [el("p", { class: "t-dim", style: "margin:0" }, "Nothing published yet.")]));
  }

  /* key = "output/<job>/clip_NN" — the same per-clip approval endpoint the
   * Results screen uses, addressed by job + clip index. */
  function setApproval(key, approval) {
    const [, job, clip] = key.split("/");
    const index = Number(clip.slice(5));
    return api.put(
      `/api/jobs/${encodeURIComponent(job)}/clips/${index}/approval`,
      { approval });
  }

  function approvalRow(c) {
    const slot = c.proposed_publish_at
      ? new Date(c.proposed_publish_at).toLocaleString([], {
          weekday: "short", day: "numeric", month: "short",
          hour: "numeric", minute: "2-digit" })
      : null;
    const approveBtn = el("button", { class: "btn btn-primary btn-sm",
                                      type: "button" }, "Approve");
    const rejectBtn = el("button", { class: "btn btn-ghost btn-sm",
                                     type: "button" }, "Reject");
    const act = async (approval) => {
      approveBtn.disabled = rejectBtn.disabled = true;
      try {
        await setApproval(c.key, approval);
        toast(approval === "approved"
          ? "Approved — it can now be scheduled."
          : "Rejected — it won't be uploaded.", "is-ok");
        await refresh();
      } catch (e) {
        toast(e.message, "is-error");
        approveBtn.disabled = rejectBtn.disabled = false;
      }
    };
    approveBtn.addEventListener("click", () => act("approved"));
    rejectBtn.addEventListener("click", () => act("rejected"));
    return el("div", { class: "uq-approval-row" },
      thumbButton(c.video_url, c.title, "uq-thumb"),
      el("div", { class: "uq-meta" },
        el("div", { class: "uq-title" }, c.title),
        c.description ? el("div", { class: "t-dim",
                                    style: "font-size:var(--text-xs)" },
          c.description) : null,
        (c.hashtags && c.hashtags.length)
          ? el("div", { class: "t-dim t-mono",
                        style: "font-size:var(--text-xs)" },
              c.hashtags.join(" ")) : null,
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          slot ? `would publish ${slot}` : "",
          c.duration != null ? ` · ${fmtClock(c.duration)}` : "")),
      el("div", { class: "uq-score" },
        el("span", { class: "t-mono" }, String(c.score)),
        c.niche ? el("span", { class: "badge" }, c.niche) : null),
      el("div", { class: "field-inline" }, approveBtn, rejectBtn));
  }

  function deleteLocalBtn(u) {
    if (!(u.on_disk && u.key)) return null;
    const btn = el("button", { class: "btn btn-danger btn-sm", type: "button",
                               "aria-label": `Delete local files of ${u.title}` },
      "Delete local");
    btn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Delete local files?",
        body: `This removes your local copy of "${u.title}". It stays on `
          + "YouTube; only the local files go.",
      });
      if (!ok) return;
      try {
        const r = await api.del("/api/clips", { keys: [u.key] });
        toast(r.deleted ? `Deleted — freed ${fmtBytes(r.reclaimed_bytes)}.`
          : "Couldn't delete that clip.", r.deleted ? "is-ok" : "is-error");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    return btn;
  }

  function ytLink(u) {
    return u.video_id
      ? el("a", { class: "t-mono", style: "font-size:var(--text-xs)",
                  href: u.url, target: "_blank", rel: "noopener" }, "youtu.be ↗")
      : null;
  }

  /* Opens this clip's permanent archive/uploaded/ folder in Explorer. Only
   * shown while a live folder exists — once it's swept into a backup zip
   * (archive_zip set) there's no folder to open, archiveLabel below says
   * where the file lives instead. */
  function openFolderBtn(u) {
    if (!(u.archived && u.video_id) || u.archive_zip) return null;
    const btn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Open folder");
    btn.addEventListener("click", async () => {
      try {
        await api.post(`/api/archive/open/${encodeURIComponent(u.video_id)}`);
      } catch (e) { toast(e.message, "is-error"); }
    });
    return btn;
  }

  function archiveLabel(u) {
    return u.archive_zip
      ? el("span", { class: "t-dim", style: "font-size:var(--text-xs)" },
          `in backup ${u.archive_zip}`)
      : null;
  }

  /* A published (live-on-YouTube) row. */
  function uploadedRow(u) {
    return el("div", { class: "uq-uploaded-row" },
      el("div", { class: "uq-meta" },
        el("div", { class: "uq-title" }, u.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          u.uploaded_at ? new Date(u.uploaded_at).toLocaleDateString() : "",
          u.score != null ? ` · score ${u.score}` : "")),
      el("div", { class: "field-inline" },
        ytLink(u), openFolderBtn(u), archiveLabel(u), deleteLocalBtn(u)));
  }

  /* A scheduled row: booked on YouTube, publishes at its slot. Offers
   * un-schedule (pulls it back before publish; re-uploading costs quota). */
  function scheduledRow(u) {
    const when = u.publish_at
      ? new Date(u.publish_at).toLocaleString([], {
          weekday: "short", day: "numeric", month: "short",
          hour: "numeric", minute: "2-digit" })
      : "soon";
    const unBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Un-schedule");
    unBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Un-schedule this clip?",
        body: `This pulls "${u.title}" back off YouTube before it publishes and `
          + "frees the slot. Re-uploading it later costs API quota again.",
        confirmLabel: "Un-schedule",
      });
      if (!ok) return;
      unBtn.disabled = true;
      try {
        await api.post("/api/youtube/unschedule", { key: u.key });
        toast("Un-scheduled — it's eligible again.", "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); unBtn.disabled = false; }
    });
    return el("div", { class: "uq-uploaded-row" },
      el("div", { class: "uq-meta" },
        el("div", { class: "uq-title" }, u.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          `publishes ${when}`, u.score != null ? ` · score ${u.score}` : "")),
      el("div", { class: "field-inline" },
        el("span", { class: "badge badge-live" }, "Scheduled"),
        ytLink(u), unBtn));
  }

  function candidateRow(c) {
    // Not a <label>: the thumbnail must open a preview on click, so wrapping
    // the whole row in a label (which would toggle the checkbox on any click,
    // the original preview bug) is wrong. The checkbox and the meta area
    // toggle selection explicitly; the thumbnail opens the preview.
    const check = el("input", { type: "checkbox", class: "uq-check",
                                "aria-label": `Select ${c.title}` });
    check.checked = state.selected.has(c.key);
    const setSel = (on) => {
      check.checked = on;
      if (on) state.selected.add(c.key); else state.selected.delete(c.key);
      selBtn.disabled = delSelBtn.disabled = state.selected.size === 0;
    };
    check.addEventListener("change", () => setSel(check.checked));
    const delBtn = el("button", { class: "btn btn-danger btn-sm", type: "button",
                                  "aria-label": `Delete ${c.title}` }, "Delete");
    delBtn.addEventListener("click", () =>
      deleteKeys([c.key], { approvedPending: state.requireApproval }));
    return el("div", { class: "uq-row" },
      check,
      thumbButton(c.video_url, c.title, "uq-thumb"),
      el("div", { class: "uq-meta", onclick: () => setSel(!check.checked) },
        el("div", { class: "uq-title" }, c.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          c.source_name, c.duration != null ? ` · ${fmtClock(c.duration)}` : "",
          c.bytes ? ` · ${fmtBytes(c.bytes)}` : "",
          c.duplicates ? ` · +${c.duplicates} duplicate`
            + `${c.duplicates > 1 ? "s" : ""} collapsed` : "")),
      el("div", { class: "uq-score" },
        el("span", { class: "t-mono" }, String(c.score)),
        c.niche ? el("span", { class: "badge" }, c.niche) : null,
        state.requireApproval
          ? el("span", { class: "badge badge-ok" }, "Approved") : null,
        c.band ? el("span", {
          class: `badge ${c.band === "Strong" ? "badge-ok"
            : c.band === "Weak" ? "badge-warn" : ""}`,
        }, c.band) : null,
        delBtn));
  }

  async function openConfirm(mode, count, keys) {
    let picked;
    try {
      const body = mode === "manual" ? { mode, keys } : { mode, count };
      picked = await api.post("/api/youtube/queue/select", body);
    } catch (e) {
      toast(e.message, "is-error");
      return;
    }
    if (!picked.items.length) {
      toast("Nothing to upload — pick at least one clip.", "is-error");
      return;
    }

    const rows = new Map();
    const list = el("div", { class: "uq-confirm-list" },
      ...picked.items.map((it) => {
        const status = el("span", { class: "badge" }, STATUS_LABEL.pending);
        const link = el("span");
        const row = el("div", { class: "uq-confirm-row" },
          thumbButton(it.video_url, it.title, "uq-confirm-thumb"),
          el("span", { class: "uq-confirm-title" }, it.title),
          el("span", { class: "t-mono" }, String(it.score)),
          status, link);
        rows.set(it.key, { status, link });
        return row;
      }));

    const warning = picked.warning
      ? el("p", { class: "field-error", style: "margin:0" }, picked.warning)
      : null;
    // preview-first: show the branded end card that gets appended at upload
    const wm = state.endWatermark;
    const endCard = wm && wm.enabled
      ? el("div", { class: "uq-endcard" },
          el("img", { class: "uq-endcard-img", src: "/endcard.png",
                      alt: "End card preview" }),
          el("div", {},
            el("div", { class: "t-label" }, "End card added on upload"),
            el("p", { class: "t-dim", style: "margin:4px 0 0" },
              `A "${wm.text}" end card (${wm.duration_s}s) is appended to each `
              + "uploaded video. Your saved clips stay unchanged.")))
      : null;
    const summary = el("p", { class: "t-dim", style: "margin:0" },
      `${picked.items.length} clip(s) will go live right now.`);

    const confirmBtn = el("button", { class: "btn btn-primary", type: "button" },
      "Confirm — upload now");
    const cancelBtn = el("button", { class: "btn btn-ghost", type: "button" },
      "Cancel");
    const closeBtn = el("button", { class: "btn", type: "button" }, "Close");
    closeBtn.style.display = "none";
    const actions = el("div", { class: "field-inline" },
      confirmBtn, cancelBtn, closeBtn);

    const dlg = el("dialog", { class: "dialog" },
      el("div", { class: "dialog-head" },
        el("h2", { class: "t-title" }, "Confirm upload"),
        el("button", {
          class: "btn btn-ghost btn-sm", type: "button", "aria-label": "Close",
          onclick: () => dlg.close(),
        }, "✕")),
      el("div", { class: "dialog-body", style: "display:grid;gap:16px" },
        summary, warning, endCard, list, actions));
    document.body.append(dlg);
    dlg.showModal();
    openDialog = dlg;
    dlg.addEventListener("close", () => {
      dlg.remove();
      if (openDialog === dlg) openDialog = null;
      refresh();
    });
    cancelBtn.addEventListener("click", () => dlg.close());

    confirmBtn.addEventListener("click", async () => {
      confirmBtn.disabled = true;
      cancelBtn.disabled = true;
      let run;
      try {
        const body = mode === "manual" ? { mode, keys } : { mode, count };
        run = await api.post("/api/youtube/queue/upload", body);
      } catch (e) {
        toast(e.message, "is-error");
        confirmBtn.disabled = false;
        cancelBtn.disabled = false;
        return;
      }
      const stop = pollBatch(run.batch_id, (items) => {
        let done = 0, failed = 0;
        for (const it of items) {
          const row = rows.get(it.key);
          if (!row) continue;
          row.status.className = `badge ${STATUS_BADGE[it.status] || ""}`;
          row.status.textContent = STATUS_LABEL[it.status] || it.status;
          if (it.status === "done") {
            done++;
            row.link.replaceChildren(el("a", {
              href: it.url, target: "_blank", rel: "noopener",
              class: "t-mono", style: "font-size:var(--text-xs)",
            }, "youtu.be ↗"));
          } else if (it.status === "failed") {
            failed++;
            row.link.replaceChildren(el("span", {
              class: "t-dim", style: "font-size:var(--text-xs)",
            }, it.error || ""));
          }
        }
        return { done, failed };
      }, ({ done, failed }) => {
        summary.textContent = failed
          ? `${done} uploaded, ${failed} failed.`
          : `${done} uploaded.`;
        toast(failed ? `${done} uploaded, ${failed} failed — see the list.`
                     : `${done} clip(s) are live.`, failed ? "is-error" : "is-ok");
        actions.replaceChildren(closeBtn);
        closeBtn.style.display = "";
      });
      cleanupOnClose(dlg, stop);
    });
  }

  function cleanupOnClose(dlg, stop) {
    dlg.addEventListener("close", stop, { once: true });
  }

  /* Polls batch status until state === 'done'; onTick fires each poll with
   * a {done, failed} tally (from onItems' return), onFinal once at the end.
   * Returns a stop() to cancel polling early (dialog closed mid-batch). */
  function pollBatch(batchId, onItems, onFinal) {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      let st;
      try {
        st = await api.get(`/api/youtube/queue/upload/${batchId}`);
      } catch {
        return; // transient — next tick retries
      }
      const tally = onItems(st.items);
      if (st.state === "done") {
        onFinal(tally);
        return;
      }
      if (!stopped) setTimeout(tick, 1500);
    };
    tick();
    return () => { stopped = true; };
  }

  refresh();
  return {
    refresh,
    dispose: () => { if (openDialog) openDialog.close(); },
  };
}
