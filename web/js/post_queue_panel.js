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
  const addWrap = el("div", {});
  let channelsByAccount = {};   // account name -> [source channel names]
  let allChannels = [];         // full channel list (id, name, account)
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
      el("h2", { class: "t-title", style: "margin:0" }, "Your YouTube channels"),
      drainBtn),
    el("p", { class: "t-dim", style: "margin:0" },
      "Each channel connects once with its own Google sign-in. Source channels "
      + "in the Channels tab pick which of these they upload to."),
    accountsWrap,
    addWrap,
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

    // Connect button (per-account OAuth) when this channel isn't signed in yet
    const connectBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" },
      "Connect");
    connectBtn.addEventListener("click", async () => {
      connectBtn.disabled = true;
      connectBtn.textContent = "A Google window is open — finish there…";
      try {
        await api.post(`/api/accounts/${encodeURIComponent(a.account)}/authorize`);
        toast(`"${a.account}" connected.`, "is-ok");
        await refresh();
      } catch (e) {
        toast(e.message, "is-error");
        connectBtn.disabled = false;
        connectBtn.textContent = "Connect";
      }
    });

    const removeAcctBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Remove");
    removeAcctBtn.addEventListener("click", async () => {
      try {
        await api.del(`/api/accounts/${encodeURIComponent(a.account)}`);
        toast(`Removed "${a.account}".`, "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });

    const sources = channelsByAccount[a.account] || [];

    // "link a source channel to this account" — the from-the-YouTube-side half
    // of two-way linking. Lists channels not already feeding this account.
    const linkable = allChannels.filter((c) => (c.account || "default") !== a.account);
    const linkSel = el("select", {},
      el("option", { value: "" }, linkable.length ? "+ Link a source channel" : "no other channels"),
      ...linkable.map((c) => el("option", { value: c.id }, c.name)));
    linkSel.disabled = !linkable.length;
    linkSel.addEventListener("change", async () => {
      if (!linkSel.value) return;
      try {
        await api.patch(`/api/channels/${linkSel.value}`, { account: a.account });
        toast(`Linked to "${a.account}".`, "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });

    const actions = a.authorized
      ? [el("span", { class: "t-dim" }, "per day"), stepper]
      : [connectBtn];
    // /api/queue/posting doesn't send is_default — the built-in account is
    // always literally named "default", so key off that.
    if (a.account !== "default") actions.push(removeAcctBtn);

    return el("div", { class: "an-row" },
      el("div", {},
        el("div", { style: "font-weight:600" }, a.account,
          a.authorized
            ? el("span", { class: "badge badge-ok", style: "margin-left:8px" }, "connected")
            : el("span", { class: "badge", style: "margin-left:8px" }, "not connected")),
        el("div", { class: "t-dim", style: "font-size:var(--text-xs)" },
          sources.length ? `Sources: ${sources.join(", ")}` : "No source channels linked yet"),
        allChannels.length
          ? el("div", { class: "field-inline", style: "margin-top:4px" }, linkSel)
          : ""),
      el("div", { class: "t-dim" },
        `${a.uploads_today}/${a.max_per_day} today · `
        + `${a.can_schedule_now} more can go out`),
      el("div", { class: "field-inline" }, ...actions));
  }

  function addAccountForm() {
    const name = el("input", { type: "text", placeholder: "e.g. gaming-channel" });
    const perDay = el("input", { type: "number", min: "0", max: "50", value: "3",
      style: "width:70px" });
    const save = el("button", { class: "btn btn-primary btn-sm", type: "button" }, "Add");
    const cancel = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Cancel");
    const form = el("div", { class: "card card-flat", style: "display:grid;gap:8px" },
      el("h3", { class: "t-label", style: "margin:0" }, "Add a YouTube channel"),
      el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-xs)" },
        "Give it a short name, then use its Connect button to sign in with that "
        + "channel's Google account."),
      el("div", { class: "field-inline" },
        el("span", { class: "t-dim" }, "Name"), name,
        el("span", { class: "t-dim" }, "posts/day"), perDay),
      el("div", { class: "field-inline" }, save, cancel));
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api.post("/api/accounts",
          { name: name.value.trim(), max_per_day: Number(perDay.value) });
        toast("Channel added — now connect it.", "is-ok");
        addWrap.replaceChildren(addAccountButton());
        await refresh();
      } catch (e) { toast(e.message, "is-error"); save.disabled = false; }
    });
    cancel.addEventListener("click", () => addWrap.replaceChildren(addAccountButton()));
    return form;
  }

  function addAccountButton() {
    const btn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "+ Add a YouTube channel");
    btn.addEventListener("click", () => addWrap.replaceChildren(addAccountForm()));
    return btn;
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
    let q, cal, chans;
    try {
      [q, cal, chans] = await Promise.all([
        api.get("/api/queue/posting"),
        api.get("/api/queue/calendar"),
        api.get("/api/channels").catch(() => ({ channels: [] })),
      ]);
    } catch (e) {
      accountsWrap.replaceChildren(el("p", { class: "t-dim" }, e.message));
      return;
    }
    allChannels = chans.channels || [];
    channelsByAccount = {};
    for (const c of allChannels) {
      (channelsByAccount[c.account || "default"] ||= []).push(c.name);
    }
    accountsWrap.replaceChildren(...q.accounts.map(accountRow));
    if (!addWrap.hasChildNodes()) addWrap.replaceChildren(addAccountButton());
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
