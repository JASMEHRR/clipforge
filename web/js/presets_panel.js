/* Editing presets: list, create/edit form, live caption preview.
 * A preset bundles caption style, aspect, SFX, music, speed ramps, punch-in
 * zooms, keyword pop-ins, transitions and watermark; runs and channel
 * auto-pull pick a preset by name. Thin client over /api/edit-presets. */

import { api } from "./api.js";
import { confirmDialog, el, field, toast, toggle } from "./ui.js";

const ASPECTS = ["9:16", "1:1", "16:9"];
const ANIMATIONS = ["karaoke", "fade", "box"];
const TRANSITIONS = ["cut", "whip", "zoom"];

function select(options, value) {
  const s = el("select", {},
    ...options.map(([v, label]) => el("option", { value: v }, label)));
  s.value = value ?? options[0][0];
  return s;
}

function input(value, attrs = {}) {
  const i = el("input", { type: "text", ...attrs });
  i.value = value ?? "";
  return i;
}

export function mountPresets(container) {
  const state = { presets: [], captionPresets: [], tracks: [], editing: null };
  const listWrap = el("div", { style: "display:grid;gap:8px" });
  const editorWrap = el("div");
  const newBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" },
    "New preset");
  newBtn.addEventListener("click", () => openEditor(null));

  container.append(
    el("div", { class: "field-inline", style: "justify-content:space-between" },
      el("h2", { class: "t-title", style: "margin:0" }, "Editing presets"),
      newBtn),
    el("p", { class: "t-dim", style: "margin:0" },
      "A preset bundles caption style, aspect ratio, sound effects, music, "
      + "pacing effects and watermark. Pick one when you queue a video; "
      + "approved channels can use one automatically."),
    listWrap, editorWrap);

  function presetRow(p) {
    const editBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Edit");
    editBtn.addEventListener("click", () => openEditor(p));
    const delBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Delete");
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete "${p.name}"?`,
        body: "Channels using this preset fall back to run defaults.",
      });
      if (!ok) return;
      try {
        await api.del(`/api/edit-presets/${encodeURIComponent(p.name)}`);
        toast("Preset deleted.", "is-ok");
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
    });
    const bits = [];
    if (p.aspect) bits.push(p.aspect);
    if (p.caption?.preset) bits.push(p.caption.preset);
    if (p.music?.track) bits.push(`music: ${p.music.track}`);
    if (p.sfx?.enabled) bits.push("sfx");
    if (p.speed_ramps?.enabled) bits.push("speed ramps");
    if (p.transition && p.transition !== "cut") bits.push(p.transition);
    return el("div", { class: "an-row" },
      el("div", { style: "font-weight:600" }, p.name),
      el("div", { class: "t-dim" }, bits.join(" · ") || "defaults"),
      el("div", { class: "field-inline" }, editBtn, delBtn));
  }

  function openEditor(p) {
    const isNew = !p;
    p = p || {};
    const name = input(p.name, { placeholder: "e.g. Gaming punchy" });
    if (!isNew) name.disabled = true; // name = identity; delete + recreate to rename
    const capPreset = select(
      state.captionPresets.map((n) => [n, n]), p.caption?.preset);
    const animation = select(ANIMATIONS.map((a) => [a, a]), p.caption?.animation || "karaoke");
    const highlight = input(p.caption?.highlight_hex, { type: "color" });
    if (!p.caption?.highlight_hex) highlight.value = "#39FF14";
    const aspect = select(ASPECTS.map((a) => [a, a]), p.aspect || "9:16");
    const ctaText = input(p.cta_text, { placeholder: "Follow for more!" });

    const sfxOn = toggle(!!p.sfx?.enabled, "Sound effects (whoosh at cuts, pop on emphasis)");
    const sfxVol = input(String(p.sfx?.volume_db ?? -10), { type: "number", step: "1" });
    const musicTrack = select(
      [["", "No music"], ["auto", "Auto (mood match)"],
       ...state.tracks.map((t) => [t.id, t.title])], p.music?.track || "");
    const musicVol = input(String(p.music?.volume_db ?? -18), { type: "number", step: "1" });

    const rampsOn = toggle(!!p.speed_ramps?.enabled, "Speed up low-energy moments");
    const rampRate = input(String(p.speed_ramps?.rate ?? 1.5),
      { type: "number", step: "0.1", min: "1", max: "2" });
    const punchMode = select(
      [["off", "Off"], ["emphasis", "On emphasis"], ["interval", "Regular interval"]],
      p.punch_in?.mode || "off");
    const punchAmount = input(String(p.punch_in?.amount_pct ?? 7),
      { type: "number", step: "1", min: "1", max: "25" });
    const transition = select(TRANSITIONS.map((t) => [t, t]), p.transition || "cut");

    const wmMode = select([["off", "Off"], ["text", "Text"], ["image", "Logo"]],
      p.watermark?.mode || "off");
    const wmText = input(p.watermark?.text, { placeholder: "clipped by @you" });

    const popinsBox = el("textarea", {
      rows: "3",
      placeholder: "keyword = assets/popins/fire.png (one per line)",
    });
    popinsBox.value = (p.popins || [])
      .map((x) => `${x.keyword} = ${x.asset}`).join("\n");

    const previewImg = el("img", {
      style: "max-width:100%;border-radius:8px;display:none", alt: "Caption preview",
    });
    const previewBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Preview captions");
    const saveBtn = el("button", { class: "btn btn-primary", type: "button" },
      isNew ? "Create preset" : "Save changes");
    const cancelBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
    cancelBtn.addEventListener("click", () => editorWrap.replaceChildren());

    function collect() {
      const data = { name: name.value.trim() };
      data.caption = {
        preset: capPreset.value,
        animation: animation.value,
        highlight_hex: highlight.value,
      };
      data.aspect = aspect.value;
      if (ctaText.value.trim()) data.cta_text = ctaText.value.trim();
      data.sfx = { enabled: sfxOn.input.checked, volume_db: Number(sfxVol.value) };
      if (musicTrack.value) {
        data.music = { track: musicTrack.value, volume_db: Number(musicVol.value) };
      }
      data.speed_ramps = { enabled: rampsOn.input.checked, rate: Number(rampRate.value) };
      if (punchMode.value !== "off") {
        data.punch_in = { mode: punchMode.value, amount_pct: Number(punchAmount.value) };
      }
      data.transition = transition.value;
      if (wmMode.value !== "off") {
        data.watermark = { mode: wmMode.value, text: wmText.value.trim() };
      }
      const popins = popinsBox.value.split("\n")
        .map((line) => line.split("="))
        .filter((parts) => parts.length === 2 && parts[0].trim() && parts[1].trim())
        .map(([k, v]) => ({ keyword: k.trim(), asset: v.trim() }));
      if (popins.length) data.popins = popins;
      return data;
    }

    previewBtn.addEventListener("click", async () => {
      previewBtn.disabled = true;
      try {
        const res = await fetch("/api/edit-presets/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preset: collect() }),
        });
        if (!res.ok) throw new Error((await res.json()).detail || "Preview failed");
        previewImg.src = URL.createObjectURL(await res.blob());
        previewImg.style.display = "block";
      } catch (e) { toast(e.message, "is-error"); }
      previewBtn.disabled = false;
    });

    saveBtn.addEventListener("click", async () => {
      const data = collect();
      if (!data.name) { toast("Give the preset a name.", "is-error"); return; }
      saveBtn.disabled = true;
      try {
        await api.post("/api/edit-presets", { preset: data });
        toast(isNew ? "Preset created." : "Preset saved.", "is-ok");
        editorWrap.replaceChildren();
        await refresh();
      } catch (e) { toast(e.message, "is-error"); }
      saveBtn.disabled = false;
    });

    editorWrap.replaceChildren(el("div", { class: "card", style: "display:grid;gap:12px" },
      el("h3", { class: "t-label", style: "margin:0" },
        isNew ? "New preset" : `Edit "${p.name}"`),
      field("Name", name),
      el("div", { class: "field-inline" },
        field("Caption style", capPreset),
        field("Word animation", animation),
        field("Highlight color", highlight)),
      el("div", { class: "field-inline" },
        field("Aspect ratio", aspect),
        field("Transition between cuts", transition)),
      field("CTA text", ctaText),
      el("div", { class: "field-inline" }, sfxOn.node, field("SFX volume (dB)", sfxVol)),
      el("div", { class: "field-inline" },
        field("Background music", musicTrack),
        field("Music volume (dB)", musicVol)),
      el("div", { class: "field-inline" },
        rampsOn.node, field("Speed factor", rampRate)),
      el("div", { class: "field-inline" },
        field("Punch-in zooms", punchMode),
        field("Zoom amount (%)", punchAmount)),
      el("div", { class: "field-inline" }, field("Watermark", wmMode), field("Watermark text", wmText)),
      field("Keyword pop-ins", popinsBox),
      el("div", { class: "field-inline" }, previewBtn),
      previewImg,
      el("div", { class: "field-inline" }, saveBtn, cancelBtn)));
  }

  async function refresh() {
    try {
      const [presets, caps, music] = await Promise.all([
        api.get("/api/edit-presets"),
        api.get("/api/presets"),
        api.get("/api/music"),
      ]);
      state.presets = presets.presets;
      state.captionPresets = caps.presets;
      state.tracks = music.tracks;
    } catch (e) {
      listWrap.replaceChildren(el("p", { class: "t-dim" }, e.message));
      return;
    }
    listWrap.replaceChildren(...(state.presets.length
      ? state.presets.map(presetRow)
      : [el("p", { class: "t-dim", style: "margin:0" },
          "No presets yet — create one to bundle your editing style.")]));
  }

  refresh();
  return { refresh };
}
