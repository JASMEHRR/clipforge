/* Posting queue + per-account quota + week calendar.
 * The queue is priority-ordered (new channel uploads > channel top hits >
 * manual) and drag-reorderable; each destination account has a "max posts
 * per day" stepper that takes effect immediately. */

import { api } from "./api.js";
import { el, toast } from "./ui.js";

const SOURCE_LABEL = {
  channel_new: "new upload",
  channel_top: "top performer",
  manual: "manual",
};

export function mountPostQueue(container) {
  const accountsWrap = el("div", { style: "display:grid;gap:8px" });
  const queueWrap = el("div", { style: "display:grid;gap:4px" });
  const calendarWrap = el("div");
  const drainBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    "Send due clips now");

  drainBtn.addEventListener("click", async () => {
    drainBtn.disabled = true;
    try {
      const r = await api.post("/api/queue/posting/drain");
      toast(`${r.uploaded} clip${r.uploaded === 1 ? "" : "s"} scheduled.`,
        "is-ok");
      await refresh();
    } catch (e) { toast(e.message, "is-error"); }
    drainBtn.disabled = false;
  });

  container.append(
    el("div", { class: "field-inline", style: "justify-content:space-between" },
      el("h2", { class: "t-title", style: "margin:0" }, "Posting queue"),
      drainBtn),
    accountsWrap,
    el("h3", { class: "t-label", style: "margin:8px 0 0" },
      "Queue (drag to reorder)"),
    queueWrap,
    el("h3", { class: "t-label", style: "margin:8px 0 0" }, "This week"),
    calendarWrap);

  function accountRow(a) {
    const stepper = el("input", {
      type: "number", min: "0", max: "50", style: "width:70px",
    });
    stepper.value = a.max_per_day;
    stepper.addEventListener("change", async () => {
      try {
        await api.patch(`/api/accounts/${encodeURIComponent(a.account)}/quota`,
          { max_per_day: Number(stepper.value) });
        toast(`${a.account}: ${stepper.value}/day.`, "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    return el("div", { class: "an-row" },
      el("div", { style: "font-weight:600" }, a.account,
        a.authorized ? "" : el("span", { class: "badge", style: "margin-left:8px" },
          "not connected")),
      el("div", { class: "t-dim" },
        `${a.uploads_today}/${a.max_per_day} today · `
        + `${a.can_schedule_now} more can go out`),
      el("div", { class: "field-inline" },
        el("span", { class: "t-dim" }, "per day"), stepper));
  }

  function queueRow(entry, order) {
    const row = el("div", {
      class: "an-row", draggable: "true", "data-key": entry.clip_key,
      style: "cursor:grab",
    },
      el("div", { class: "t-mono", style: "font-size:var(--text-xs)" },
        entry.clip_key.split("/").slice(-2).join("/")),
      el("div", { class: "t-dim" },
        `${SOURCE_LABEL[entry.source] || entry.source} · ${entry.account}`),
      el("div", {}, removeBtn(entry.clip_key)));
    row.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.setData("text/plain", entry.clip_key);
      ev.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragover", (ev) => ev.preventDefault());
    row.addEventListener("drop", async (ev) => {
      ev.preventDefault();
      const dragged = ev.dataTransfer.getData("text/plain");
      if (!dragged || dragged === entry.clip_key) return;
      const keys = order.filter((k) => k !== dragged);
      keys.splice(keys.indexOf(entry.clip_key), 0, dragged);
      try {
        await api.post("/api/queue/posting/reorder", { clip_keys: keys });
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    return row;
  }

  function removeBtn(key) {
    const b = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Remove");
    b.addEventListener("click", async () => {
      try {
        await api.del(`/api/queue/posting/${key}`);
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    return b;
  }

  function calendarGrid(cal) {
    const grid = el("div", {
      style: "display:grid;grid-template-columns:repeat(7,1fr);gap:6px",
    });
    for (const day of cal.days) {
      const d = new Date(day.date + "T00:00:00");
      grid.append(el("div", {
        class: "card card-flat",
        style: "padding:8px;min-height:84px;display:grid;gap:4px;align-content:start",
      },
        el("div", { class: "t-label" },
          d.toLocaleDateString([], { weekday: "short", day: "numeric" })),
        ...day.posts.map((p) => el("div", {
          class: "t-dim", style: "font-size:var(--text-xs)", title: p.title,
        }, `${p.time} ${p.title.slice(0, 22)}`)),
        day.posts.length === 0
          ? el("div", { class: "t-dim", style: "font-size:var(--text-xs)" }, "—")
          : ""));
    }
    return grid;
  }

  async function refresh() {
    let q, cal;
    try {
      [q, cal] = await Promise.all([
        api.get("/api/queue/posting"),
        api.get("/api/queue/calendar"),
      ]);
    } catch (e) {
      accountsWrap.replaceChildren(el("p", { class: "t-dim" }, e.message));
      return;
    }
    accountsWrap.replaceChildren(...q.accounts.map(accountRow));
    const order = q.queue.map((e) => e.clip_key);
    queueWrap.replaceChildren(...(q.queue.length
      ? q.queue.map((e) => queueRow(e, order))
      : [el("p", { class: "t-dim", style: "margin:0" },
          "Nothing queued — approved clips land here before publishing.")]));
    calendarWrap.replaceChildren(calendarGrid(cal));
  }

  refresh();
  return { refresh };
}
