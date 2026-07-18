/* Approved channels & auto-pull: add/edit channels (permission source and
 * creator credit are optional but recommended), pause/resume, manual
 * "check now", per-channel source pool. */

import { api } from "./api.js";
import { confirmDialog, el, field, toast } from "./ui.js";

function input(value, attrs = {}) {
  const i = el("input", { type: "text", ...attrs });
  i.value = value ?? "";
  return i;
}

export function mountChannels(container) {
  const state = { channels: [], presets: [], accounts: [] };

  // <select> of connected YouTube channels (upload accounts). Reused by the
  // add form and each channel row's "change destination" control.
  function accountSelect(current) {
    const sel = el("select", {}, ...(state.accounts.length
      ? state.accounts.map((a) => el("option", { value: a.account },
          a.account + (a.authorized ? "" : " (not connected)")))
      : [el("option", { value: "default" }, "default")]));
    sel.value = current || "default";
    return sel;
  }
  const listWrap = el("div", { style: "display:grid;gap:8px" });
  const formWrap = el("div");
  const poolWrap = el("div");
  const addBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" },
    "Add channel");
  const pollBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Check now");

  addBtn.addEventListener("click", () => openForm());
  pollBtn.addEventListener("click", async () => {
    pollBtn.disabled = true;
    try {
      const r = await api.post("/api/channels/poll");
      const errs = Object.keys(r.errors || {}).length;
      toast(`${r.added} new video${r.added === 1 ? "" : "s"} pulled`
        + (errs ? ` (${errs} channel${errs === 1 ? "" : "s"} failed)` : "."),
        errs ? "is-error" : "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    pollBtn.disabled = false;
  });

  container.append(
    el("div", { class: "field-inline", style: "justify-content:space-between" },
      el("h2", { class: "t-title", style: "margin:0" }, "Approved channels"),
      el("div", { class: "field-inline" }, pollBtn, addBtn)),
    el("p", { class: "t-dim", style: "margin:0" },
      "Only channels you have permission to clip (clipping program or the "
      + "creator's approval). New uploads and top performers are pulled "
      + "hourly and processed with the channel's preset."),
    listWrap, formWrap, poolWrap);

  function channelRow(c) {
    const pauseBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      c.paused ? "Resume" : "Pause");
    pauseBtn.addEventListener("click", async () => {
      try {
        await api.patch(`/api/channels/${c.id}`, { paused: !c.paused });
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    const poolBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Pool");
    poolBtn.addEventListener("click", () => showPool(c));
    // change which YouTube channel this source's clips upload to
    const acctSel = accountSelect(c.account);
    acctSel.addEventListener("change", async () => {
      try {
        await api.patch(`/api/channels/${c.id}`, { account: acctSel.value });
        toast(`Uploads to "${acctSel.value}".`, "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    const delBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Remove");
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Remove "${c.name}"?`,
        body: "Already-processed videos stay deduplicated; the channel just "
          + "stops being pulled.",
        confirmLabel: "Remove",
      });
      if (!ok) return;
      try {
        await api.del(`/api/channels/${c.id}`);
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    return el("div", { class: "an-row" },
      el("div", {},
        el("div", { style: "font-weight:600" }, c.name,
          c.paused ? el("span", { class: "badge", style: "margin-left:8px" }, "Paused") : ""),
        el("div", { class: "t-dim", style: "font-size:var(--text-xs)" },
          `permission: ${c.permission_source}`)),
      el("div", { class: "t-dim" },
        `${c.videos_pulled} pulled · ${c.pending} queued · `
        + `${c.processed} done${c.failed ? ` · ${c.failed} failed` : ""}`
        + (c.default_preset ? ` · preset: ${c.default_preset}` : "")),
      el("div", { class: "field-inline" },
        el("span", { class: "t-dim", style: "font-size:var(--text-xs)" }, "Uploads to"),
        acctSel),
      el("div", { class: "field-inline" }, pauseBtn, poolBtn, delBtn));
  }

  function openForm() {
    const url = input("", { placeholder: "https://www.youtube.com/@creator" });
    const name = input("", { placeholder: "Creator name (optional)" });
    const permission = input("", {
      placeholder: "e.g. clipping program or creator DM — link or note (optional)",
    });
    const credit = input("", { placeholder: "e.g. clips from @creator (optional)" });
    const preset = el("select", {},
      el("option", { value: "" }, "No preset (run defaults)"),
      ...state.presets.map((p) => el("option", { value: p.name }, p.name)));
    const topN = input("10", { type: "number", min: "1", max: "50" });
    const account = accountSelect("default");
    const saveBtn = el("button", { class: "btn btn-primary", type: "button" }, "Add channel");
    const cancelBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
    cancelBtn.addEventListener("click", () => formWrap.replaceChildren());

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      try {
        await api.post("/api/channels", {
          url: url.value.trim(),
          name: name.value.trim(),
          permission_source: permission.value.trim(),
          credit_text: credit.value.trim(),
          default_preset: preset.value,
          top_n: Number(topN.value) || 10,
          account: account.value,
        });
        toast("Channel added.", "is-ok");
        formWrap.replaceChildren();
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
      saveBtn.disabled = false;
    });

    formWrap.replaceChildren(el("div", { class: "card", style: "display:grid;gap:12px" },
      el("h3", { class: "t-label", style: "margin:0" }, "Add approved channel"),
      field("Channel link", url),
      field("Name", name),
      field("Permission source (optional)", permission),
      field("Creator credit for descriptions (optional)", credit),
      el("div", { class: "field-inline" },
        field("Default preset", preset),
        field("Top videos to pull", topN)),
      field("Upload to which YouTube channel", account),
      el("div", { class: "field-inline" }, saveBtn, cancelBtn)));
  }

  async function showPool(c) {
    let pool;
    try {
      pool = (await api.get(`/api/channels/${c.id}/pool`)).pool;
    } catch (e) { toast(e.message, "is-error"); return; }
    poolWrap.replaceChildren(el("div", { class: "card", style: "display:grid;gap:8px" },
      el("h3", { class: "t-label", style: "margin:0" }, `Source pool — ${c.name}`),
      ...(pool.length ? pool.map((v) => el("div", { class: "an-row" },
        el("div", {},
          el("a", { href: v.url, target: "_blank", rel: "noopener" },
            v.title || v.video_id)),
        el("div", { class: "t-dim" },
          `${v.source}${v.views != null ? ` · ${v.views.toLocaleString()} views` : ""}`),
        el("div", {},
          el("span", { class: `badge ${v.status === "processed" ? "badge-ok" : ""}` },
            v.status),
          v.error ? el("span", { class: "t-dim", style: "margin-left:8px" }, v.error) : "")))
        : [el("p", { class: "t-dim", style: "margin:0" },
            "Nothing pulled yet — hit “Check now”.")])));
  }

  async function refresh() {
    try {
      const [ch, pr, acc] = await Promise.all([
        api.get("/api/channels"),
        api.get("/api/edit-presets"),
        api.get("/api/accounts"),
      ]);
      state.channels = ch.channels;
      state.presets = pr.presets;
      state.accounts = acc.accounts || [];
    } catch (e) {
      listWrap.replaceChildren(el("p", { class: "t-dim" }, e.message));
      return;
    }
    listWrap.replaceChildren(...(state.channels.length
      ? state.channels.map(channelRow)
      : [el("p", { class: "t-dim", style: "margin:0" },
          "No channels yet. Add a creator you have permission to clip.")]));
  }

  refresh();
  return { refresh };
}
