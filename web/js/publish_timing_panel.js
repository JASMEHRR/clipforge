/* "Schedule intelligence" card for the Analytics tab: publish_timing.py's
 * learned-hour ranking, gate status, changelog, and owner controls
 * (auto-learn toggle, pin/ban, reset). Reads only local files server-side —
 * mounts independently of whether YouTube/Analytics is connected, so it
 * shows real gate status ("0/15 uploads") even before that's set up. */

import { api } from "./api.js";
import { barsSVG } from "./charts.js";
import { confirmDialog, el, toast } from "./ui.js";

function fmtHour(h) {
  return `${String(h).padStart(2, "0")}:00`;
}

export function mountPublishTiming(container) {
  const state = { data: null };

  const enabledToggle = el("input", { type: "checkbox" });
  const enabledLabel = el("label", { class: "opt-toggle" }, "Learn posting times automatically",
    el("span", { class: "switch" }, enabledToggle, el("span", { class: "knob" })));
  const recomputeBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Recompute now");
  const resetBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Reset learning");
  const gateLine = el("p", { style: "margin:0;font-weight:500" });
  const noteLine = el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-xs)" });
  const chartWrap = el("div");
  const tableWrap = el("div", { class: "an-table" });
  const changelogWrap = el("div", { style: "display:grid;gap:4px" });

  container.append(
    el("div", { class: "field-inline", style: "justify-content:space-between" },
      el("h2", { class: "t-title", style: "margin:0" }, "Schedule intelligence"),
      el("div", { class: "field-inline" }, enabledLabel, recomputeBtn, resetBtn)),
    gateLine, noteLine, chartWrap,
    el("h3", { class: "t-label", style: "margin:12px 0 0" }, "Hour ranking"),
    tableWrap,
    el("h3", { class: "t-label", style: "margin:12px 0 0" }, "Recent changes"),
    changelogWrap);

  enabledToggle.addEventListener("change", async () => {
    const next = enabledToggle.checked;
    try {
      await api.put("/api/analytics/publish-timing/enabled", { enabled: next });
      await refresh();
    } catch (e) {
      enabledToggle.checked = !next;
      toast(e.message, "is-error");
    }
  });

  recomputeBtn.addEventListener("click", async () => {
    recomputeBtn.disabled = true;
    try {
      state.data = await api.post("/api/analytics/publish-timing/recompute");
      render();
      toast("Recomputed.", "is-ok");
    } catch (e) { toast(e.message, "is-error"); }
    recomputeBtn.disabled = false;
  });

  resetBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Reset learning?",
      body: "This forgets every learned hour score and the change log. Your "
        + "enabled/pinned/banned settings are kept. This can't be undone.",
      confirmLabel: "Reset",
    });
    if (!ok) return;
    try {
      await api.post("/api/analytics/publish-timing/reset");
      toast("Learning reset.", "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
  });

  function pinBanBtn(hour, kind, active) {
    const btn = el("button", {
      class: `btn btn-sm ${active ? "btn-primary" : "btn-ghost"}`, type: "button",
      "aria-label": `${kind === "pin" ? "Pin" : "Ban"} ${fmtHour(hour)}`,
    }, kind === "pin" ? "Pin" : "Ban");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api.post(`/api/analytics/publish-timing/${kind}`, { hour });
        await refresh();
      } catch (e) { toast(e.message, "is-error"); btn.disabled = false; }
    });
    return btn;
  }

  function rankingRow(r, pinned, banned) {
    const isPinned = pinned.includes(r.hour);
    const isBanned = banned.includes(r.hour);
    return el("div", { class: "an-row" },
      el("div", { class: "t-mono" }, fmtHour(r.hour)),
      el("div", {}, r.score.toFixed(2)),
      el("div", {}, `${r.sample_count} clip${r.sample_count === 1 ? "" : "s"}`),
      el("div", {},
        el("span", { class: `badge ${r.trusted ? "badge-ok" : ""}` },
          r.trusted ? "Trusted" : "Exploring")),
      el("div", { class: "field-inline" },
        pinBanBtn(r.hour, "pin", isPinned), pinBanBtn(r.hour, "ban", isBanned)));
  }

  function changelogRow(entry) {
    return el("div", { class: "t-dim", style: "font-size:var(--text-sm)" },
      el("span", { class: "t-mono" },
        entry.at ? new Date(entry.at).toLocaleString([], {
          day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
        }) : ""),
      " — ", entry.message);
  }

  function render() {
    const d = state.data;
    if (!d) return;
    enabledToggle.checked = d.enabled;

    gateLine.textContent = d.using_learned_hours
      ? `Learning from your own upload history — using learned hours `
        + `(${d.total_uploads}/${d.min_total_uploads} uploads gated).`
      : `Not enough data yet — using configured slots `
        + `(${d.total_uploads}/${d.min_total_uploads} uploads gated).`;
    noteLine.textContent = d.note;

    if (d.ranking.length) {
      chartWrap.replaceChildren(barsSVG(
        d.ranking.map((r) => ({ label: fmtHour(r.hour), value: Math.max(0, r.score) }))));
    } else {
      chartWrap.replaceChildren(el("p", { class: "t-dim", style: "margin:0" },
        "No scored hours yet."));
    }

    tableWrap.replaceChildren(
      el("div", { class: "an-row an-row-head" },
        el("div", {}, "Hour"), el("div", {}, "Score"), el("div", {}, "Samples"),
        el("div", {}, "Status"), el("div", {}, "")),
      ...(d.ranking.length
        ? d.ranking.map((r) => rankingRow(r, d.pinned_hours, d.banned_hours))
        : [el("p", { class: "t-dim", style: "margin:8px 0" }, "Nothing scored yet.")]));

    changelogWrap.replaceChildren(...(d.changelog.length
      ? d.changelog.map(changelogRow)
      : [el("p", { class: "t-dim", style: "margin:0" }, "No changes logged yet.")]));
  }

  async function refresh() {
    try {
      state.data = await api.get("/api/analytics/publish-timing");
    } catch (e) {
      gateLine.textContent = "";
      noteLine.textContent = e.message;
      return;
    }
    render();
  }

  refresh();
  return { refresh };
}
