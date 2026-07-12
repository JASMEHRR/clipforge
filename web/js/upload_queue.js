/* Manual "Upload now" batch control for the YouTube screen's Upload queue:
 * pick clips by top-score count or by hand, confirm exactly what will go
 * out, watch live per-clip progress, see a result summary. Reuses the same
 * eligibility/scoring/exclude state as the auto-upload scheduler — this is
 * an additional manual trigger into it, not a second selection system. */

import { api } from "./api.js";
import { clipVideo, el, fmtClock, openClipPreview, toast } from "./ui.js";

/* A compact clip thumbnail that opens a full click-to-play preview. Shared by
 * the queue rows and the confirm dialog so preview behaves the same in both. */
function thumbButton(src, title, cls) {
  return el("button", { class: "uq-thumb-btn", type: "button",
                        "aria-label": `Play preview of ${title}`,
                        onclick: () => openClipPreview(src, title) },
    clipVideo(src, { class: cls, controls: null, muted: "" }),
    el("span", { class: "uq-play", "aria-hidden": "true" }, "▶"));
}

const STATUS_LABEL = {
  pending: "Waiting", uploading: "Uploading…", done: "Done", failed: "Failed",
};
const STATUS_BADGE = {
  pending: "", uploading: "badge-live", done: "badge-ok", failed: "badge-warn",
};

export function mountUploadQueue(container) {
  const state = { candidates: [], pending: [], requireApproval: false,
                  selected: new Set(), endWatermark: null };
  let openDialog = null;   // tracked so navigating away can force-close it

  const countIn = el("input", { class: "input t-mono", type: "number",
                                min: "0", step: "1", style: "width:80px" });
  const countBtn = el("button", { class: "btn btn-primary", type: "button" },
    "Upload now");
  const selBtn = el("button", { class: "btn", type: "button" }, "Upload selected now");
  const rowsWrap = el("div", { class: "uq-rows" });
  const emptyMsg = el("p", { class: "t-dim", style: "margin:0" },
    "No clips are waiting to publish right now.");
  const uploadedWrap = el("div", { class: "uq-rows" });
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
    approvalsSection,
    el("div", { class: "uq-section-label t-label" }, "Queue — waiting to publish"),
    el("div", { class: "uq-controls" },
      el("div", { class: "field-inline" },
        el("span", { class: "t-label" }, "Upload"), countIn,
        el("span", { class: "t-dim" }, "of the best clips now"), countBtn),
      selBtn),
    rowsWrap,
    el("div", { class: "uq-section-label t-label", style: "margin-top:8px" },
      "Uploaded — already sent to YouTube"),
    uploadedWrap);

  countBtn.addEventListener("click", () => openConfirm("top", Number(countIn.value) || 0));
  selBtn.addEventListener("click", () => openConfirm("manual", 0, [...state.selected]));
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

  async function refresh() {
    rowsWrap.replaceChildren(el("div", { class: "skeleton", style: "height:60px" }));
    let data, approvals;
    try {
      [data, approvals] = await Promise.all([
        api.get("/api/youtube/queue"), api.get("/api/youtube/approvals")]);
    } catch (e) {
      rowsWrap.replaceChildren(el("p", { class: "t-dim", style: "margin:0" }, e.message));
      return;
    }
    state.candidates = data.candidates;
    state.pending = approvals.items || [];
    state.requireApproval = !!data.require_approval;
    state.uploaded = data.uploaded || [];
    state.endWatermark = data.end_watermark || null;
    state.selected = new Set([...state.selected].filter(
      (k) => data.candidates.some((c) => c.key === k)));
    const remaining = Math.max(0, (data.max_per_day ?? 3) - (data.uploads_today ?? 0));
    countIn.value = String(Math.min(remaining || 1, data.candidates.length));
    countIn.max = String(data.candidates.length);
    render();
  }

  function render() {
    selBtn.disabled = state.selected.size === 0;
    countBtn.disabled = state.candidates.length === 0;
    rowsWrap.replaceChildren(
      ...(state.candidates.length ? state.candidates.map(candidateRow)
        : [emptyMsg]));
    approvalsSection.style.display = state.pending.length ? "" : "none";
    approveAllBtn.textContent = `Approve all (${state.pending.length})`;
    approvalsWrap.replaceChildren(...state.pending.map(approvalRow));
    renderUploaded();
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

  function renderUploaded() {
    const rows = state.uploaded || [];
    if (!rows.length) {
      uploadedWrap.replaceChildren(el("p", { class: "t-dim", style: "margin:0" },
        "Nothing uploaded yet."));
      return;
    }
    uploadedWrap.replaceChildren(...rows.map((u) =>
      el("div", { class: "uq-uploaded-row" },
        el("div", { class: "uq-meta" },
          el("div", { class: "uq-title" }, u.title),
          el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
            u.uploaded_at ? new Date(u.uploaded_at).toLocaleDateString() : "",
            u.score != null ? ` · score ${u.score}` : "")),
        u.video_id
          ? el("a", { class: "t-mono", style: "font-size:var(--text-xs)",
                      href: u.url, target: "_blank", rel: "noopener" },
              "youtu.be ↗")
          : el("span", { class: "t-dim" }, "scheduled"))));
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
      selBtn.disabled = state.selected.size === 0;
    };
    check.addEventListener("change", () => setSel(check.checked));
    return el("div", { class: "uq-row" },
      check,
      thumbButton(c.video_url, c.title, "uq-thumb"),
      el("div", { class: "uq-meta", onclick: () => setSel(!check.checked) },
        el("div", { class: "uq-title" }, c.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          c.source_name, c.duration != null ? ` · ${fmtClock(c.duration)}` : "",
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
        }, c.band) : null));
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
