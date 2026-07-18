/* Preview-first picker modals (native <dialog> + the .dialog/.pick classes
 * from the design system). Every picker returns a Promise that resolves to
 * the chosen value, or null when dismissed. */

import { api, uploadFile } from "./api.js";
import { createDotMatrix } from "./dots.js";
import { clipVideo, el, toast } from "./ui.js";

/* Generic option-grid modal. options: [{value, label, desc?, img?, node?}].
 * `footer` (optional node) renders under the grid — for upload buttons.
 * Clicks inside [data-no-pick] elements don't select (play buttons). */
export function pick({ title, options, current, footer, onClose }) {
  return new Promise((resolve) => {
    let result = null;
    const dlg = el("dialog", { class: "dialog" });
    const grid = el("div", { class: "dialog-grid" });
    for (const opt of options) {
      const btn = el("button", {
        class: `pick ${opt.value === current ? "is-selected" : ""}`,
        type: "button",
      },
        opt.img ? el("img", { src: opt.img, loading: "lazy", alt: "" }) : null,
        opt.node || null,
        el("span", { class: "pick-name" }, opt.label),
        opt.desc ? el("span", { class: "pick-desc" }, opt.desc) : null);
      btn.addEventListener("click", (e) => {
        if (e.target.closest("[data-no-pick]")) return;
        result = opt;
        dlg.close();
      });
      grid.append(btn);
    }
    const closeBtn = el("button", {
      class: "btn btn-ghost btn-sm", type: "button", "aria-label": "Close",
      onclick: () => dlg.close(),
    }, "Close");
    dlg.append(
      el("div", { class: "dialog-head" },
        el("h2", { class: "t-title" }, title), closeBtn),
      el("div", { class: "dialog-body" }, grid,
        footer ? el("div", { class: "dialog-foot" }, footer) : null));
    dlg.addEventListener("close", () => {
      dlg.remove();
      if (onClose) onClose();
      resolve(result ? result.value : null);
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

/* Caption style — real rendered frames from /api/preview. */
export async function pickPreset(current, font) {
  const p = await api.get("/api/presets");
  const q = font ? `&font=${encodeURIComponent(font)}` : "";
  return pick({
    title: "Caption style",
    current: current || p.default,
    options: p.presets.map((name) => ({
      value: name,
      label: name.replace(/-/g, " "),
      img: `/api/preview?preset=${encodeURIComponent(name)}${q}`,
    })),
  });
}

/* Caption font — real burns of the current style in each font, plus an
 * "Add a font" upload that registers and re-opens the gallery. */
export async function pickFont(current, preset) {
  const [fonts, presets] = await Promise.all([
    api.get("/api/fonts"), api.get("/api/presets")]);
  const style = preset || presets.default;
  const fileIn = el("input", {
    class: "visually-hidden", type: "file", accept: ".ttf,.otf",
    "aria-hidden": "true", tabindex: "-1",
  });
  const addBtn = el("button", { class: "btn btn-sm", type: "button" },
    "Add a font (.ttf or .otf)");
  addBtn.addEventListener("click", () => fileIn.click());

  const options = [{ value: "", label: "Style's own font",
                     desc: "Whatever the caption style uses" }];
  const seen = new Set();
  for (const f of fonts.fonts) {
    if (seen.has(f.family)) continue;
    seen.add(f.family);
    options.push({
      value: f.family,
      label: f.family,
      desc: f.source === "user" ? "Added by you" : "",
      img: `/api/preview?preset=${encodeURIComponent(style)}`
        + `&font=${encodeURIComponent(f.family)}`,
    });
  }

  return new Promise((resolve) => {
    // after an upload the dialog closes and re-opens with the new font; the
    // re-opened picker's choice must win, not the close-with-null underneath
    let reopened = false;
    fileIn.addEventListener("change", async () => {
      const file = fileIn.files[0];
      if (!file) return;
      try {
        const { family } = await uploadFile("/api/uploads/font", file);
        toast(`Added ${family}.`, "is-ok");
        reopened = true;
        document.querySelector("dialog.dialog")?.close();
        resolve(await pickFont(family, style));   // re-open with it selected
      } catch (e) {
        toast(e.message, "is-error");
      }
    });
    pick({
      title: "Caption font", options, current: current || "",
      footer: el("div", {}, addBtn, fileIn),
    }).then((v) => { if (!reopened) resolve(v); });
  });
}

/* Background music — play/pause per track on a shared audio element. */
export async function pickMusic(current) {
  const m = await api.get("/api/music");
  const audio = new Audio();
  let playingBtn = null;
  const playButton = (id) => {
    const b = el("button", {
      class: "btn btn-ghost btn-sm", type: "button", "data-no-pick": "",
      "aria-label": "Play this track",
    }, "▶ Listen");
    b.addEventListener("click", () => {
      if (playingBtn === b && !audio.paused) {
        audio.pause();
        b.textContent = "▶ Listen";
        return;
      }
      if (playingBtn) playingBtn.textContent = "▶ Listen";
      audio.src = `/api/music/${encodeURIComponent(id)}/audio`;
      audio.play().catch(() => toast(
        "Couldn't play that track — check your internet connection.",
        "is-error"));
      b.textContent = "❚❚ Stop";
      playingBtn = b;
    });
    return el("span", { "data-no-pick": "" }, b);
  };
  const options = [
    { value: "", label: "No music", desc: "Just the original sound" },
    { value: "auto", label: "Pick for me",
      desc: "A track that fits each clip's mood" },
    { value: "random", label: "Surprise me", desc: "A random track per run" },
    ...m.tracks.map((t) => ({
      value: t.id,
      label: t.title || t.id,
      desc: (t.moods || []).join(" · "),
      node: playButton(t.id),
    })),
  ];
  return pick({
    title: "Background music", options, current: current || "",
    onClose: () => { audio.pause(); audio.src = ""; },
  });
}

/* Watermark position — the five real anchor spots, shown on a phone frame. */
const POSITIONS = [
  ["top-left", "Top left"], ["top-right", "Top right"],
  ["center", "Middle"],
  ["bottom-left", "Bottom left"], ["bottom-right", "Bottom right"],
];
export function pickPosition(current) {
  return pick({
    title: "Watermark position",
    current: current || "bottom-right",
    options: POSITIONS.map(([value, label]) => ({
      value, label,
      node: el("span", { class: "pos-demo" },
        el("span", { class: `pos-dot pos-${value}` })),
    })),
  });
}

/* Output shape. */
const SHAPES = [
  ["9:16", "Tall", "Shorts, Reels, TikTok", 9 / 16],
  ["1:1", "Square", "Feed posts", 1],
  ["16:9", "Widescreen", "YouTube, presentations", 16 / 9],
];
export function pickShape(current) {
  return pick({
    title: "Clip shape",
    current: current || "9:16",
    options: SHAPES.map(([value, label, desc, ratio]) => ({
      value, label, desc,
      node: el("span", { class: "shape-demo-wrap" },
        el("span", { class: "shape-demo", style: `aspect-ratio:${ratio}` })),
    })),
  });
}

/* What to do when the video already has subtitles burned in. */
const SUBS_MODES = [
  ["", "Decide for me", "Looks at each clip and picks the safest option"],
  ["replace", "Swap them out", "Hide the old subtitles, add fresh captions"],
  ["keep", "Keep the originals", "Leave the video's own subtitles as they are"],
  ["ignore", "Caption anyway", "Add captions even if the video has its own"],
];
export function pickSubsMode(current) {
  return pick({
    title: "If the video already has subtitles",
    current: current || "",
    options: SUBS_MODES.map(([value, label, desc]) => ({ value, label, desc })),
  });
}

/* Style profile — summaries from profiles/*.json. */
export async function pickProfile(current) {
  const p = await api.get("/api/profiles");
  return pick({
    title: "Editing style",
    current: current || "",
    options: [
      { value: "", label: "Standard",
        desc: "ClipForge's built-in editing style" },
      ...p.profiles.filter((x) => x.name !== "default").map((x) => ({
        value: x.name,
        label: x.name,
        desc: x.description || "Learned from your reference videos",
      })),
    ],
  });
}
