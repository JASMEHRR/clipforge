/* All-clips library: every clip on disk, any status, across every job.
 * Reuses the upload-queue's row/checkbox/delete pattern (uq-* CSS, the same
 * DELETE /api/clips endpoint) rather than a second selection system. Renders
 * in chunks with an IntersectionObserver sentinel so 500+ clips stay
 * responsive — no virtual-scroll library, just bounded DOM growth. */

import { api } from "./api.js";
import { confirmDialog, el, fmtBytes, fmtClock, thumbButton, toast } from "./ui.js";

const STATUS_LABEL = {
  sample: "Sample/test", pending: "Pending", approved: "Approved",
  rejected: "Rejected", scheduled: "Scheduled", uploaded: "Uploaded",
};
const STATUS_BADGE = {
  sample: "", pending: "", approved: "badge-ok", rejected: "badge-warn",
  scheduled: "badge-live", uploaded: "badge-ok",
};
const CHUNK = 40;

export function mountLibrary(container) {
  const state = { clips: [], sort: "date", selected: new Set() };
  let items = [];   // flattened render items (dividers + clip rows) for the active sort
  let visible = 0;

  const sortRow = el("div", { class: "field-inline" });
  const delSelBtn = el("button", { class: "btn btn-danger btn-sm", type: "button" },
    "Delete selected");
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Refresh");
  const countLine = el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-xs)" });
  const rowsWrap = el("div", { class: "uq-rows" });
  const sentinel = el("div", { style: "height:1px" });
  const emptyMsg = el("p", { class: "t-dim", style: "margin:0" },
    "No clips found under output/ yet.");

  container.append(
    el("div", { class: "uq-controls" },
      el("div", { class: "field-inline" },
        el("span", { class: "t-label" }, "Sort"), sortRow),
      el("div", { class: "field-inline" }, countLine, delSelBtn, refreshBtn)),
    rowsWrap, sentinel);

  delSelBtn.disabled = true;

  const sortBtn = (label, value) => {
    const b = el("button", {
      class: `btn btn-sm ${state.sort === value ? "btn-primary" : "btn-ghost"}`,
      type: "button",
    }, label);
    b.addEventListener("click", () => { state.sort = value; renderAll(); });
    return b;
  };
  const renderSortRow = () => sortRow.replaceChildren(
    sortBtn("Date", "date"), sortBtn("Virality", "virality"), sortBtn("Size", "size"));

  function buildItems() {
    if (state.sort === "date") {
      const out = [];
      let lastJob = null;
      for (const c of state.clips) {
        if (c.job !== lastJob) {
          out.push({ type: "divider", job: c.job, created: c.created,
                     keys: state.clips.filter((x) => x.job === c.job).map((x) => x.key) });
          lastJob = c.job;
        }
        out.push({ type: "clip", clip: c });
      }
      return out;
    }
    const field = state.sort === "virality" ? "score" : "bytes";
    return [...state.clips]
      .sort((a, b) => (b[field] ?? -1) - (a[field] ?? -1))
      .map((c) => ({ type: "clip", clip: c }));
  }

  function renderAll() {
    renderSortRow();
    items = buildItems();
    rowsWrap.replaceChildren();
    visible = 0;
    growChunk();
  }

  function growChunk() {
    const next = items.slice(visible, visible + CHUNK);
    rowsWrap.append(...next.map(itemNode));
    visible += next.length;
  }

  const observer = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && visible < items.length) growChunk();
  });
  observer.observe(sentinel);

  function setSelDisabled() {
    delSelBtn.disabled = state.selected.size === 0;
    delSelBtn.textContent = state.selected.size
      ? `Delete selected (${state.selected.size})` : "Delete selected";
  }

  function setSel(clip, on) {
    if (on) state.selected.add(clip.key); else state.selected.delete(clip.key);
    setSelDisabled();
  }

  function itemNode(item) {
    if (item.type === "divider") return dividerRow(item);
    return clipRow(item.clip);
  }

  function dividerRow(item) {
    const when = item.job.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
    const date = when
      ? `${when[3]}.${when[2]}.${when[1]} ${when[4]}:${when[5]}` : (item.created || "");
    const allBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Select all in this job");
    allBtn.addEventListener("click", () => {
      for (const k of item.keys) state.selected.add(k);
      setSelDisabled();
      rowsWrap.querySelectorAll(`[data-job="${CSS.escape(item.job)}"] .uq-check`)
        .forEach((cb) => { cb.checked = true; });
    });
    return el("div", { class: "uq-controls", style: "margin-top:12px" },
      el("div", {},
        el("div", { class: "t-mono t-dim" }, date),
        el("div", { class: "t-label" }, item.job)),
      allBtn);
  }

  function clipRow(c) {
    const check = el("input", { type: "checkbox", class: "uq-check",
                                "aria-label": `Select ${c.title}` });
    check.checked = state.selected.has(c.key);
    check.addEventListener("change", () => setSel(c, check.checked));
    const delBtn = el("button", { class: "btn btn-danger btn-sm", type: "button",
                                  "aria-label": `Delete ${c.title}` }, "Delete");
    delBtn.addEventListener("click", () => deleteKeys([c.key]));
    return el("div", { class: "uq-row", "data-job": c.job },
      check,
      thumbButton(c.video_url, c.title, "uq-thumb"),
      el("div", { class: "uq-meta", onclick: () => setSel(c, !check.checked) },
        el("div", { class: "uq-title" }, c.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" },
          c.job, c.duration != null ? ` · ${fmtClock(c.duration)}` : "",
          c.bytes ? ` · ${fmtBytes(c.bytes)}` : "")),
      el("div", { class: "uq-score" },
        c.score != null ? el("span", { class: "t-mono" }, String(c.score)) : null,
        c.niche ? el("span", { class: "badge" }, c.niche) : null,
        el("span", { class: `badge ${STATUS_BADGE[c.status] || ""}` },
          STATUS_LABEL[c.status] || c.status),
        delBtn));
  }

  async function deleteKeys(keys) {
    if (!keys.length) return;
    const picked = state.clips.filter((c) => keys.includes(c.key));
    const bytes = picked.reduce((sum, c) => sum + (c.bytes || 0), 0);
    const risky = picked.some((c) => c.status === "approved" || c.status === "scheduled");
    const ok = await confirmDialog({
      title: `Delete ${keys.length} clip${keys.length === 1 ? "" : "s"}?`,
      body: `This removes ${keys.length === 1 ? "its" : "their"} files from disk`
        + (bytes ? ` (${fmtBytes(bytes)})` : "") + ". "
        + (risky ? "Some are approved or scheduled to upload — deleting them "
          + "cancels that. " : "")
        + "This can't be undone.",
    });
    if (!ok) return;
    try {
      const r = await api.del("/api/clips", { keys });
      const busy = r.results.filter((x) => x.status === "uploading").length;
      let msg = `Deleted ${r.deleted} — freed ${fmtBytes(r.reclaimed_bytes)}.`;
      if (busy) msg += ` ${busy} skipped (uploading).`;
      toast(msg, busy ? "is-error" : "is-ok");
      for (const k of keys) state.selected.delete(k);
      await refresh(true);
    } catch (e) { toast(e.message, "is-error"); }
  }

  delSelBtn.addEventListener("click", () => deleteKeys([...state.selected]));
  refreshBtn.addEventListener("click", () => refresh(true));

  async function refresh(forceRefresh = false) {
    rowsWrap.replaceChildren(el("div", { class: "skeleton", style: "height:60px" }));
    let data;
    try {
      data = await api.get(`/api/clips/all${forceRefresh ? "?refresh=1" : ""}`);
    } catch (e) {
      rowsWrap.replaceChildren(el("p", { class: "t-dim", style: "margin:0" }, e.message));
      return;
    }
    state.clips = data.clips || [];
    state.selected = new Set(
      [...state.selected].filter((k) => state.clips.some((c) => c.key === k)));
    setSelDisabled();
    countLine.textContent =
      `${state.clips.length} clip${state.clips.length === 1 ? "" : "s"} on disk`;
    if (!state.clips.length) {
      rowsWrap.replaceChildren(emptyMsg);
      return;
    }
    renderAll();
  }

  refresh();
  return { refresh, dispose: () => observer.disconnect() };
}
