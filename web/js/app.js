/* ClipForge frontend — hash-routed views over the FastAPI backend.
 * Screens: home (submit), run (live progress), results, edit (per-clip),
 * queue (batch), youtube (upload center), settings, history.
 * All copy is plain language; server sentences are shown verbatim. */

import { api, uploadFile, watchRun } from "./api.js";
import { barsSVG, sparklineSVG } from "./charts.js";
import { createDotMatrix, miniScore } from "./dots.js";
import { clipVideo, confirmDialog, el, field, fmtBytes, fmtClock, isKept,
  toast, toggle } from "./ui.js";
import {
  pickFont, pickMusic, pickPosition, pickPreset, pickProfile, pickShape,
  pickSubsMode,
} from "./pickers.js";
import { mountUploadQueue } from "./upload_queue.js";
import { mountLibrary } from "./library.js";
import { mountPublishTiming } from "./publish_timing_panel.js";
import { mountPresets } from "./presets_panel.js";
import { mountChannels } from "./channels_panel.js";
import { mountPostQueue } from "./post_queue_panel.js";

const view = document.getElementById("view");
let cleanup = null; // per-view teardown (websockets, dot-matrix rafs, timers)

// -------------------------------------------------------------- router ----

const routes = {
  "": renderHome,
  run: renderRun,
  activity: renderActivity,
  results: renderResults,
  edit: renderEditor,
  queue: renderQueue,
  library: renderLibrary,
  youtube: renderYouTube,
  analytics: renderAnalytics,
  presets: renderPresets,
  channels: renderChannels,
  settings: renderSettings,
  history: renderHistory,
};

function navigate() {
  if (cleanup) { cleanup(); cleanup = null; }
  const [, name = "", ...rest] = location.hash.split("/");
  document.querySelectorAll(".topbar nav a").forEach((a) => {
    a.toggleAttribute("aria-current",
      a.getAttribute("href") === `#/${name}` || (!name && a.hash === "#/"));
  });
  view.replaceChildren();
  (routes[name] || renderHome)(...rest.map(decodeURIComponent));
}
window.addEventListener("hashchange", navigate);

/* A picker-backed form row: button shows the current choice. */
function pickerRow(label, initialText, open) {
  const btn = el("button", { class: "btn picker-btn", type: "button" },
    initialText);
  btn.addEventListener("click", open);
  return { node: field(label, btn), btn };
}

// ---------------------------------------------------------------- home ----

function renderHome() {
  let filePath = "";

  // per-run options (empty string = leave the app's defaults untouched)
  const o = {
    count: 0, aspect: "9:16", preset: "", font: "", music: "",
    musicVol: -18, cta: "", highlight: "", pacing: "", clipMin: "",
    clipMax: "", subsMode: "", profile: "", wmMode: "text", wmText: "",
    wmImage: "", wmImageName: "", wmPos: "bottom-right",
  };

  const urlInput = el("input", {
    class: "input input-hero", type: "text",
    placeholder: "Paste a YouTube or video link",
    "aria-label": "Video link",
  });
  const fileChipWrap = el("div");
  const browseBtn = el("button", { class: "btn", type: "button" }, "Choose a file");
  const dropHint = el("div", { class: "drop-hint" },
    "or drop a video file anywhere on this page", browseBtn);
  const filePicker = el("input", {
    class: "visually-hidden", type: "file",
    accept: "video/*,.mp4,.mov,.mkv,.webm", "aria-hidden": "true", tabindex: "-1",
  });
  browseBtn.addEventListener("click", () => filePicker.click());
  filePicker.addEventListener("change", () => {
    if (filePicker.files[0]) takeFile(filePicker.files[0]);
  });

  // ---- basics ----
  const clipCount = el("input", {
    class: "range", type: "range", min: "0", max: "12", value: "0",
    "aria-label": "How many clips",
  });
  const clipCountOut = el("span", { class: "t-mono t-dim" }, "Automatic");
  clipCount.addEventListener("input", () => {
    o.count = Number(clipCount.value);
    clipCountOut.textContent = o.count === 0 ? "Automatic" : String(o.count);
  });

  const shapeRow = pickerRow("Clip shape", "Tall — Shorts, Reels, TikTok",
    async () => {
      const v = await pickShape(o.aspect);
      if (v !== null) {
        o.aspect = v;
        shapeRow.btn.textContent = {
          "9:16": "Tall — Shorts, Reels, TikTok",
          "1:1": "Square", "16:9": "Widescreen",
        }[v] || v;
      }
    });

  const musicRow = pickerRow("Background music", "No music", async () => {
    const v = await pickMusic(o.music);
    if (v !== null) {
      o.music = v;
      musicRow.btn.textContent =
        v === "" ? "No music" : v === "auto" ? "Pick for me"
          : v === "random" ? "Surprise me" : v;
    }
  });
  const musicVol = el("input", {
    class: "range", type: "range", min: "-40", max: "-10", value: "-18",
    "aria-label": "Music loudness",
  });
  musicVol.addEventListener("input", () => { o.musicVol = Number(musicVol.value); });

  const lenMin = el("input", { class: "input", type: "number", min: "5",
                               max: "180", placeholder: "30" });
  const lenMax = el("input", { class: "input", type: "number", min: "5",
                               max: "180", placeholder: "60" });
  lenMin.addEventListener("input", () => { o.clipMin = lenMin.value; });
  lenMax.addEventListener("input", () => { o.clipMax = lenMax.value; });

  const pacingSel = el("select", { class: "select", "aria-label": "Pacing" },
    el("option", { value: "", selected: "" }, "Leave as-is"),
    el("option", { value: "0.15" }, "Relaxed — keep natural pauses"),
    el("option", { value: "0.5" }, "Balanced"),
    el("option", { value: "0.85" }, "Snappy — tight cuts"));
  pacingSel.addEventListener("change", () => { o.pacing = pacingSel.value; });

  // ---- style & branding ----
  const presetRow = pickerRow("Caption style", "Style's default", async () => {
    const v = await pickPreset(o.preset, o.font);
    if (v !== null) {
      o.preset = v;
      presetRow.btn.textContent = v.replace(/-/g, " ");
    }
  });
  const fontRow = pickerRow("Caption font", "Style's own font", async () => {
    const v = await pickFont(o.font, o.preset);
    if (v !== null) {
      o.font = v;
      fontRow.btn.textContent = v || "Style's own font";
    }
  });

  const hiToggle = toggle(false, "Custom highlight color");
  const hiColor = el("input", { class: "color-input", type: "color",
                                value: "#ffd230", "aria-label": "Highlight color" });
  hiColor.style.display = "none";
  hiToggle.input.addEventListener("change", () => {
    hiColor.style.display = hiToggle.input.checked ? "" : "none";
    o.highlight = hiToggle.input.checked ? hiColor.value : "";
  });
  hiColor.addEventListener("input", () => { o.highlight = hiColor.value; });

  const ctaInput = el("input", {
    class: "input", type: "text", maxlength: "60",
    placeholder: "e.g. Follow for more",
  });
  ctaInput.addEventListener("input", () => { o.cta = ctaInput.value.trim(); });

  // watermark
  const wmText = el("input", { class: "input", type: "text", maxlength: "40",
                               placeholder: "@yourhandle" });
  wmText.addEventListener("input", () => { o.wmText = wmText.value.trim(); });
  const wmLogoBtn = el("button", { class: "btn", type: "button" }, "Choose a logo");
  const wmLogoIn = el("input", {
    class: "visually-hidden", type: "file", accept: "image/png,image/webp",
    "aria-hidden": "true", tabindex: "-1",
  });
  wmLogoBtn.addEventListener("click", () => wmLogoIn.click());
  wmLogoIn.addEventListener("change", async () => {
    const f = wmLogoIn.files[0];
    if (!f) return;
    try {
      const { path } = await uploadFile("/api/uploads/logo", f);
      o.wmImage = path;
      o.wmImageName = f.name;
      wmLogoBtn.textContent = f.name;
    } catch (e) { toast(e.message, "is-error"); }
  });
  const wmRows = { text: field("Watermark text", wmText),
                   image: field("Logo image", wmLogoBtn) };
  const wmPosRow = pickerRow("Watermark position", "Bottom right", async () => {
    const v = await pickPosition(o.wmPos);
    if (v !== null) {
      o.wmPos = v;
      wmPosRow.btn.textContent =
        v.replace("-", " ").replace(/^\w/, (c) => c.toUpperCase());
    }
  });
  const wmSeg = segmented([["off", "None"], ["text", "Text"], ["image", "Logo"]],
    o.wmMode, (v) => {
      o.wmMode = v;
      wmRows.text.style.display = v === "text" ? "" : "none";
      wmRows.image.style.display = v === "image" ? "" : "none";
      wmPosRow.node.style.display = v === "off" ? "none" : "";
    });
  wmRows.image.style.display = "none";

  const subsRow = pickerRow("If the video already has subtitles",
    "Decide for me", async () => {
      const v = await pickSubsMode(o.subsMode);
      if (v !== null) {
        o.subsMode = v;
        subsRow.btn.textContent = {
          "": "Decide for me", replace: "Swap them out",
          keep: "Keep the originals", ignore: "Caption anyway",
        }[v];
      }
    });
  const profileRow = pickerRow("Editing style", "Standard", async () => {
    const v = await pickProfile(o.profile);
    if (v !== null) {
      o.profile = v;
      profileRow.btn.textContent = v || "Standard";
    }
  });

  const styleToggle = toggle(true, "Polish clip timing");
  const viralToggle = toggle(true, "Look for big moments");

  const makeBtn = el("button", { class: "btn btn-primary btn-lg", type: "button" },
    "Make clips");
  makeBtn.addEventListener("click", start);
  urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });

  view.append(el("section", { class: "screen" },
    el("div", { class: "hero" },
      el("div", { class: "t-label" }, "New clips"),
      el("h1", { class: "t-display" }, "Turn one video into short clips"),
      el("p", { class: "hero-sub" },
        "Paste a link or drop a file. ClipForge finds the best moments, ",
        "crops them for phones, and adds captions."),
      el("div", { class: "source-row" }, urlInput, dropHint, fileChipWrap),
      el("details", { class: "opts" },
        el("summary", {}, "Options"),
        el("div", { class: "opts-body" },
          field("How many clips", clipCount, clipCountOut),
          shapeRow.node,
          musicRow.node,
          field("Music loudness", musicVol),
          field("Clip length (seconds)",
            el("div", { class: "field-inline" }, lenMin, "to", lenMax)),
          field("Pacing", pacingSel))),
      el("details", { class: "opts" },
        el("summary", {}, "Style & branding"),
        el("div", { class: "opts-body" },
          presetRow.node,
          fontRow.node,
          field("Ending message", ctaInput),
          el("div", { class: "field" }, el("label", {}, "Highlight"),
            hiToggle.node, hiColor),
          field("Watermark", wmSeg),
          wmRows.text, wmRows.image, wmPosRow.node,
          subsRow.node,
          profileRow.node,
          el("div", { class: "field" }, el("label", {}, "Extras"),
            styleToggle.node, viralToggle.node))),
      el("div", {}, makeBtn)),
    filePicker, wmLogoIn));

  // drag & drop over the whole page
  const onDragOver = (e) => { e.preventDefault(); document.body.classList.add("dragging"); };
  const onDragLeave = (e) => {
    if (!e.relatedTarget) document.body.classList.remove("dragging");
  };
  const onDrop = (e) => {
    e.preventDefault();
    document.body.classList.remove("dragging");
    if (e.dataTransfer.files[0]) takeFile(e.dataTransfer.files[0]);
  };
  document.addEventListener("dragover", onDragOver);
  document.addEventListener("dragleave", onDragLeave);
  document.addEventListener("drop", onDrop);
  cleanup = () => {
    document.removeEventListener("dragover", onDragOver);
    document.removeEventListener("dragleave", onDragLeave);
    document.removeEventListener("drop", onDrop);
    document.body.classList.remove("dragging");
  };

  async function takeFile(file) {
    filePath = "";
    const status = el("span", { class: "file-chip" }, `Uploading ${file.name}… 0%`);
    fileChipWrap.replaceChildren(status);
    try {
      const { path } = await uploadFile("/api/uploads", file, (f) => {
        status.textContent = `Uploading ${file.name}… ${Math.round(f * 100)}%`;
      });
      filePath = path;
      urlInput.value = "";
      fileChipWrap.replaceChildren(el("span", { class: "file-chip" },
        `\u{1F3AC} ${file.name}`,
        el("button", {
          class: "btn btn-ghost btn-sm", type: "button",
          "aria-label": "Remove file",
          onclick: () => { filePath = ""; fileChipWrap.replaceChildren(); },
        }, "✕")));
    } catch (e) {
      fileChipWrap.replaceChildren();
      toast(e.message, "is-error");
    }
  }

  async function start() {
    const source = filePath || urlInput.value.trim();
    if (!source) {
      toast("Paste a link or choose a video file first.", "is-error");
      urlInput.focus();
      return;
    }
    makeBtn.disabled = true;
    makeBtn.textContent = "Starting…";
    try {
      const { run_id } = await api.post("/api/runs", {
        source,
        target_count: o.count || null,
        aspect: o.aspect,
        preset: o.preset || null,
        music: o.music,
        music_volume_db: o.musicVol,
        cta_text: o.cta,
        highlight_hex: o.highlight,
        pacing: o.pacing,
        clip_min: o.clipMin,
        clip_max: o.clipMax,
        subs_mode: o.subsMode || null,
        style_profile: o.profile || null,
        watermark_mode: o.wmMode,
        watermark_text: o.wmText,
        watermark_image: o.wmMode === "image" ? o.wmImage : "",
        watermark_position: o.wmPos,
        font_family: o.font,
        style_refine: styleToggle.input.checked,
        viral: viralToggle.input.checked,
      });
      location.hash = `#/run/${encodeURIComponent(run_id)}`;
    } catch (e) {
      toast(e.message, "is-error");
      makeBtn.disabled = false;
      makeBtn.textContent = "Make clips";
    }
  }
}

/* Small segmented control (radio group styled as buttons). */
function segmented(items, current, onChange) {
  const wrap = el("div", { class: "seg", role: "radiogroup" });
  for (const [value, label] of items) {
    const b = el("button", {
      class: `seg-item ${value === current ? "is-on" : ""}`,
      type: "button", role: "radio",
      "aria-checked": value === current ? "true" : "false",
    }, label);
    b.addEventListener("click", () => {
      wrap.querySelectorAll(".seg-item").forEach((x) => {
        x.classList.remove("is-on");
        x.setAttribute("aria-checked", "false");
      });
      b.classList.add("is-on");
      b.setAttribute("aria-checked", "true");
      onChange(value);
    });
    wrap.append(b);
  }
  return wrap;
}

// ------------------------------------------------------------ progress ----

function renderRun(runId) {
  const stageTitle = el("h1", { class: "t-display" }, "Getting ready");
  const detail = el("p", { class: "t-dim", style: "margin:0;min-height:1.5em" }, " ");
  const pct = el("span", { class: "t-hero-num" }, "0");
  const eta = el("span", { class: "t-mono" }, "");
  const canvas = el("canvas");
  const stageList = el("div", { class: "stage-list" });
  const cancelBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Stop");

  view.append(el("section", { class: "screen" },
    el("div", { class: "progress-wrap" },
      el("div", { class: "t-label" }, "Making clips"),
      stageTitle,
      el("div", {}, pct, el("span", { class: "t-title t-dim" }, "%")),
      canvas,
      el("div", { class: "progress-readout" }, eta),
      detail,
      stageList,
      cancelBtn)));

  const dm = createDotMatrix(canvas, { variant: "progress" });
  dm.update({ fraction: 0, state: "running" });

  const apply = (snap) => {
    const stages = snap.stages || [];
    const running = stages.filter((s) => s.state === "running").at(-1);
    const lastDone = stages.filter((s) => s.state === "done").at(-1);
    const current = running || lastDone;
    if (current) stageTitle.textContent = current.label;
    detail.textContent = (running && (running.message || running.current_file)) || " ";
    pct.textContent = String(Math.round((snap.overall || 0) * 100));
    eta.textContent = snap.overall_eta != null
      ? `about ${fmtClock(snap.overall_eta)} left · ${fmtClock(snap.elapsed)} elapsed`
      : `${fmtClock(snap.elapsed)} elapsed`;
    dm.update({ fraction: snap.overall || 0, state: "running" });
    stageList.replaceChildren(...stages
      .filter((s) => s.state !== "skipped" && s.key !== "done")
      .map((s) => el("div", {
        class: `stage-row ${s.state === "running" ? "is-running"
          : s.state === "done" ? "is-done" : ""}`,
      }, s.label)));
  };

  const stop = watchRun(runId, {
    onSnapshot: apply,
    onDone: () => {
      dm.update({ state: "done" });
      stageTitle.textContent = "Done";
      detail.textContent = " ";
      setTimeout(() => { location.hash = `#/results/${encodeURIComponent(runId)}`; }, 900);
    },
    onCancelled: () => {
      toast("Stopped. Finished clips are kept.");
      location.hash = "#/";
    },
    onError: (message) => {
      dm.update({ state: "error" });
      stageTitle.textContent = "That didn't work";
      detail.textContent = message;
      cancelBtn.removeEventListener("click", onCancel);
      cancelBtn.textContent = "Back";
      cancelBtn.disabled = false;
      cancelBtn.onclick = () => { location.hash = "#/"; };
    },
  });

  async function onCancel() {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Stopping…";
    try {
      await api.post(`/api/runs/${runId}/cancel`);
    } catch (e) {
      toast(e.message, "is-error");
      cancelBtn.disabled = false;
      cancelBtn.textContent = "Stop";
    }
  }
  cancelBtn.addEventListener("click", onCancel);

  // page refresh mid-run: the registry may already hold a snapshot
  api.get(`/api/runs/${runId}`).then((st) => {
    if (st.snapshot) apply(st.snapshot);
    if (st.state === "done") location.hash = `#/results/${encodeURIComponent(runId)}`;
  }).catch(async () => {
    // registry entry gone (finished run + app restart) — the clips may
    // still be on disk; land on results instead of stranding the user
    try {
      await api.get(`/api/jobs/${encodeURIComponent(runId)}`);
      location.hash = `#/results/${encodeURIComponent(runId)}`;
    } catch {
      toast("That run isn't active anymore — check your finished clips.");
      location.hash = "#/";
    }
  });

  cleanup = () => { stop(); dm.destroy(); };
}

// ------------------------------------------------------------ activity ----

function renderActivity() {
  const list = el("div", { class: "activity-list" });
  const empty = el("p", { class: "t-dim", style: "margin:0" },
    "No runs yet this session. Start one from New clips — it keeps going even "
    + "if you switch tabs or close this page.");

  const row = (r) => {
    const pct = Math.round((r.overall || 0) * 100);
    const label = r.state === "running" ? "Working"
      : r.state === "done" ? "Done"
      : r.state === "error" ? "Didn't work"
      : r.state === "cancelled" ? "Stopped" : r.state;
    const badge = el("span", {
      class: `badge ${r.state === "done" ? "badge-ok"
        : r.state === "error" ? "badge-warn"
        : r.state === "running" ? "badge-live" : ""}`,
    }, label);
    // running -> live progress view; anything finished -> its clips
    const href = r.state === "running"
      ? `#/run/${encodeURIComponent(r.run_id)}`
      : `#/results/${encodeURIComponent(r.run_id)}`;
    const sub = r.state === "running" ? (r.stage || "Working…")
      : r.state === "error" ? (r.error || "Didn't finish")
      : (r.stage || "");
    return el("a", { class: "activity-row", href },
      el("div", { class: "activity-meta" },
        el("div", { class: "activity-title t-mono" }, r.run_id),
        el("div", { class: "t-dim", style: "font-size:var(--text-xs)" }, sub)),
      el("div", { class: "activity-bar" },
        el("div", {
          class: `activity-fill ${r.state === "error" ? "is-error" : ""}`,
          style: `width:${r.state === "running" ? pct : 100}%`,
        })),
      el("div", { class: "activity-side" }, badge,
        el("span", { class: "t-mono t-dim" },
          r.state === "running" ? `${pct}%` : "")));
  };

  const refresh = async () => {
    let data;
    try { data = await api.get("/api/runs"); }
    catch { return; }  // server briefly unreachable — next tick retries
    list.replaceChildren(...(data.runs.length ? data.runs.map(row) : [empty]));
  };

  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Activity"),
        el("h1", { class: "t-display" }, "Runs in progress"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "Clip-making runs on the app, not this page — a run keeps going if "
          + "you switch tabs, minimize, or close the window. Reopen here to "
          + "check on it."))),
    el("div", { class: "card card-flat" }, list)));

  refresh();
  const timer = setInterval(refresh, 2000);
  cleanup = () => clearInterval(timer);
}

// ------------------------------------------------------------- results ----

async function renderResults(jobName) {
  const grid = el("div", { class: "clip-grid" },
    ...Array.from({ length: 3 }, () =>
      el("div", { class: "skeleton skeleton-card" })));
  const sub = el("p", { class: "t-dim", style: "margin:4px 0 0" }, "Loading…");
  const ytBtn = el("button", { class: "btn", type: "button" },
    "Schedule to YouTube");
  ytBtn.addEventListener("click", async () => {
    ytBtn.disabled = true;
    ytBtn.textContent = "Sending…";
    try {
      const r = await api.post("/api/youtube/upload", { job_name: jobName });
      toast(`Sent ${r.uploaded.length} clip(s) to YouTube.`, "is-ok");
    } catch (e) {
      toast(e.message, "is-error");
    }
    ytBtn.disabled = false;
    ytBtn.textContent = "Schedule to YouTube";
  });

  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Your clips"),
        el("h1", { class: "t-display" }, "Ready to post"),
        sub),
      el("div", { style: "display:flex;gap:12px;flex-wrap:wrap" },
        ytBtn,
        el("a", { class: "btn", href: `/api/jobs/${encodeURIComponent(jobName)}/zip` },
          "Download everything"),
        el("a", { class: "btn btn-ghost", href: "#/" }, "Make more clips"))),
    grid));

  const dots = [];
  cleanup = () => dots.forEach((d) => d.destroy());

  let job;
  try {
    job = await api.get(`/api/jobs/${encodeURIComponent(jobName)}`);
  } catch (e) {
    sub.textContent = e.message;
    grid.replaceChildren();
    return;
  }

  const clips = [...(job.clips || [])]
    .sort((a, b) => isKept(b) - isKept(a) || a.index - b.index);
  const src = clips[0]?.source_name || job.source || "";
  sub.textContent = clips.length
    ? `${clips.filter(isKept).length} clips from ${src}`
    : "";

  if (!clips.length) {
    grid.replaceChildren(el("div", { class: "card" },
      el("p", { class: "t-dim", style: "margin:0" },
        "No clips came out of this video — try a longer one with clear speech.")));
    return;
  }

  grid.replaceChildren(...clips.map((clip) => clipCard(jobName, clip, dots)));
}

function clipCard(jobName, clip, dots) {
  const nn = String(clip.index).padStart(2, "0");
  const fileUrl = (name) =>
    `/api/files/${encodeURIComponent(jobName)}/clip_${nn}/${name}`;

  const score = clip.virality?.score;
  const band = clip.virality?.band;
  const scoreRow = el("div", { class: "clip-score" });
  if (score != null) {
    const mini = el("canvas");
    scoreRow.append(mini,
      el("span", { class: "t-mono" }, String(score)),
      band ? el("span", {
        class: `badge ${band === "Strong" ? "badge-ok"
          : band === "Weak" ? "badge-warn" : ""}`,
      }, band) : null);
    dots.push(miniScore(mini, score));
  }
  if (clip.niche) scoreRow.append(el("span", { class: "badge" }, clip.niche));

  const srcStart = clip.original_source_start_s ?? clip.start;
  const srcEnd = clip.original_source_end_s ?? clip.end;

  const keptBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" });
  const card = el("article", { class: "card card-hover clip-card" });
  const setKeptUi = (kept) => {
    keptBtn.textContent = kept ? "Discard" : "Keep";
    card.classList.toggle("is-discarded", !kept);
  };
  setKeptUi(isKept(clip));
  keptBtn.addEventListener("click", async () => {
    const next = card.classList.contains("is-discarded");
    keptBtn.disabled = true;
    try {
      await api.put(
        `/api/jobs/${encodeURIComponent(jobName)}/clips/${clip.index}/kept`,
        { kept: next });
      setKeptUi(next);
    } catch (e) {
      toast(e.message, "is-error");
    }
    keptBtn.disabled = false;
  });

  const exclInput = el("input", { type: "checkbox" });
  exclInput.checked = !clip.upload_excluded;
  exclInput.addEventListener("change", async () => {
    try {
      await api.put(
        `/api/jobs/${encodeURIComponent(jobName)}/clips/${clip.index}/exclude`,
        { exclude: !exclInput.checked });
    } catch (e) {
      exclInput.checked = !exclInput.checked;
      toast(e.message, "is-error");
    }
  });

  const key = `output/${jobName}/clip_${nn}`;
  const delBtn = el("button", { class: "btn btn-danger btn-sm", type: "button" },
    "Delete");
  delBtn.addEventListener("click", async () => {
    const size = clip.bytes ? ` (${fmtBytes(clip.bytes)})` : "";
    const approvedPending = clip.approval === "approved";
    const ok = await confirmDialog({
      title: "Delete this clip?",
      body: `This removes the clip's files from disk${size}. `
        + (approvedPending
          ? "It's approved and waiting to upload — deleting it now cancels that. "
          : "")
        + "This can't be undone.",
    });
    if (!ok) return;
    delBtn.disabled = true;
    try {
      const r = await api.del("/api/clips", { keys: [key] });
      if (r.deleted) {
        toast(`Deleted — freed ${fmtBytes(r.reclaimed_bytes)}.`, "is-ok");
        card.remove();
      } else {
        toast(r.results[0]?.status === "uploading"
          ? "That clip is uploading right now — try again once it's done."
          : "Couldn't delete that clip.", "is-error");
        delBtn.disabled = false;
      }
    } catch (e) { toast(e.message, "is-error"); delBtn.disabled = false; }
  });

  card.append(
    clipVideo(fileUrl("final.mp4"), { preload: isKept(clip) ? "metadata" : "none" }),
    el("div", { class: "clip-body" },
      el("h2", { class: "clip-title" }, clip.metadata?.title || `Clip ${clip.index + 1}`),
      el("div", { class: "clip-facts" },
        el("span", { class: "t-mono" }, fmtClock(clip.duration)),
        srcStart != null
          ? el("span", { class: "t-mono t-dim" },
              `from ${fmtClock(srcStart)}–${fmtClock(srcEnd)}`)
          : null),
      scoreRow,
      el("div", { class: "clip-actions" },
        el("label", { class: "opt-toggle grow" }, "Auto-upload",
          el("span", { class: "switch" }, exclInput, el("span", { class: "knob" }))),
        el("a", {
          class: "btn btn-sm",
          href: `#/edit/${encodeURIComponent(jobName)}/${clip.index}`,
        }, "Edit"),
        el("a", {
          class: "btn btn-sm", href: fileUrl("final.mp4"),
          download: `clip_${nn}.mp4`,
        }, "Download"),
        keptBtn, delBtn)));
  return card;
}

// -------------------------------------------------------------- editor ----

async function renderEditor(jobName, indexStr) {
  const index = Number(indexStr);
  let job;
  try {
    job = await api.get(`/api/jobs/${encodeURIComponent(jobName)}`);
  } catch (e) {
    toast(e.message, "is-error");
    location.hash = "#/history";
    return;
  }
  const clip = (job.clips || []).find((c) => c.index === index);
  if (!clip) {
    toast("That clip can't be found.", "is-error");
    location.hash = `#/results/${encodeURIComponent(jobName)}`;
    return;
  }

  const nn = String(index).padStart(2, "0");
  const videoUrl = () =>
    `/api/files/${encodeURIComponent(jobName)}/clip_${nn}/final.mp4?t=${Date.now()}`;
  const video = clipVideo(videoUrl());

  const state = { preset: "", styleRefine: true, subsMode: "" };

  const startIn = el("input", { class: "input t-mono", type: "number",
                                step: "0.1", value: clip.start.toFixed(1) });
  const endIn = el("input", { class: "input t-mono", type: "number",
                              step: "0.1", value: clip.end.toFixed(1) });
  const snapBtn = el("button", { class: "btn btn-sm", type: "button" },
    "Snap to sentences");
  snapBtn.addEventListener("click", async () => {
    try {
      const r = await api.get(`/api/jobs/${encodeURIComponent(jobName)}`
        + `/clips/snap?start=${startIn.value}&end=${endIn.value}`);
      startIn.value = r.start.toFixed(1);
      endIn.value = r.end.toFixed(1);
    } catch (e) { toast(e.message, "is-error"); }
  });

  const stylePreview = el("img", { class: "style-preview", alt: "",
                                   src: "/api/preview?preset="
                                     + encodeURIComponent(clip.preset || "") });
  const presetRow = pickerRow("Caption style",
    (clip.preset || "Style's default").replace(/-/g, " "), async () => {
      const v = await pickPreset(state.preset || clip.preset, "");
      if (v !== null) {
        state.preset = v;
        presetRow.btn.textContent = v.replace(/-/g, " ");
        stylePreview.src = `/api/preview?preset=${encodeURIComponent(v)}`;
      }
    });
  const styleToggle = toggle(true, "Polish clip timing");
  const subsRow = pickerRow("If the video already has subtitles",
    "Decide for me", async () => {
      const v = await pickSubsMode(state.subsMode);
      if (v !== null) {
        state.subsMode = v;
        subsRow.btn.textContent = {
          "": "Decide for me", replace: "Swap them out",
          keep: "Keep the originals", ignore: "Caption anyway",
        }[v];
      }
    });

  const title = el("h2", { class: "t-title", style: "margin:0" },
    clip.metadata?.title || `Clip ${index + 1}`);
  const desc = el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-sm)" },
    clip.metadata?.description || "");
  const regenBtn = el("button", { class: "btn btn-sm", type: "button" },
    "Rewrite title & description");
  regenBtn.addEventListener("click", async () => {
    regenBtn.disabled = true;
    try {
      const meta = await api.post(
        `/api/jobs/${encodeURIComponent(jobName)}/clips/${index}/metadata`);
      title.textContent = meta.title || title.textContent;
      desc.textContent = meta.description || desc.textContent;
      toast("Title and description rewritten.", "is-ok");
    } catch (e) { toast(e.message, "is-error"); }
    regenBtn.disabled = false;
  });

  // inline re-render progress
  const progWrap = el("div", { class: "edit-progress" });
  progWrap.style.display = "none";
  const progCanvas = el("canvas");
  const progLabel = el("span", { class: "t-mono t-dim" }, "");
  progWrap.append(progCanvas, progLabel);

  const applyBtn = el("button", { class: "btn btn-primary", type: "button" },
    "Re-make this clip");
  let dm = null;
  let stopWatch = null;
  applyBtn.addEventListener("click", async () => {
    const start = Number(startIn.value);
    const end = Number(endIn.value);
    if (!(end > start)) {
      toast("The end time must be after the start time.", "is-error");
      return;
    }
    applyBtn.disabled = true;
    progWrap.style.display = "";
    if (!dm) dm = createDotMatrix(progCanvas, { variant: "progress" });
    dm.update({ fraction: 0, state: "running" });
    try {
      const { run_id } = await api.post(
        `/api/jobs/${encodeURIComponent(jobName)}/clips/${index}/rerender`, {
          start, end,
          preset: state.preset || null,
          style_refine: styleToggle.input.checked,
          subs_mode: state.subsMode || null,
        });
      stopWatch = watchRun(run_id, {
        onSnapshot: (snap) => {
          dm.update({ fraction: snap.overall || 0, state: "running" });
          const running = (snap.stages || []).filter(
            (s) => s.state === "running").at(-1);
          progLabel.textContent = running ? running.label : "";
        },
        onDone: (result) => {
          dm.update({ state: "done" });
          progLabel.textContent = "Done";
          video.src = videoUrl();
          if (result) {
            startIn.value = Number(result.start).toFixed(1);
            endIn.value = Number(result.end).toFixed(1);
            if (result.metadata?.title) title.textContent = result.metadata.title;
          }
          toast("Clip updated.", "is-ok");
          applyBtn.disabled = false;
        },
        onCancelled: () => { applyBtn.disabled = false; },
        onError: (message) => {
          dm.update({ state: "error" });
          progLabel.textContent = "";
          toast(message, "is-error");
          applyBtn.disabled = false;
        },
      });
    } catch (e) {
      toast(e.message, "is-error");
      progWrap.style.display = "none";
      applyBtn.disabled = false;
    }
  });

  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Edit clip"),
        el("h1", { class: "t-display" }, `Clip ${index + 1}`),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          clip.source_name || job.source || "")),
      el("a", { class: "btn btn-ghost",
                href: `#/results/${encodeURIComponent(jobName)}` },
        "Back to clips")),
    el("div", { class: "editor-grid" },
      el("div", { class: "editor-video" }, video),
      el("div", { class: "editor-controls" },
        el("div", { class: "card card-flat", style: "display:grid;gap:16px" },
          field("Starts at (seconds in the original video)",
            el("div", { class: "field-inline" }, startIn, "to", endIn, snapBtn)),
          presetRow.node,
          stylePreview,
          styleToggle.node,
          subsRow.node,
          el("div", { class: "field-inline" }, applyBtn, progWrap)),
        el("div", { class: "card card-flat", style: "display:grid;gap:12px" },
          title, desc,
          el("p", { class: "t-mono t-dim", style: "margin:0" },
            (clip.metadata?.hashtags || []).join(" ")),
          el("div", {}, regenBtn))))));

  cleanup = () => { if (stopWatch) stopWatch(); if (dm) dm.destroy(); };
}

// -------------------------------------------------------------- library ----

function renderLibrary() {
  const body = el("div", { class: "card card-flat", style: "display:grid;gap:12px" });
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Library"),
        el("h1", { class: "t-display" }, "All clips"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "Every clip on disk, from every run — samples, pending, approved, "
          + "uploaded, everything."))),
    body));

  const lib = mountLibrary(body);
  cleanup = lib.dispose;
}

// --------------------------------------------------------------- queue ----

function renderQueue() {
  const linesIn = el("textarea", {
    class: "textarea", rows: "5",
    placeholder: "One link or file path per line",
  });
  const musicState = { value: "" };
  const musicRow = pickerRow("Background music for these", "No music",
    async () => {
      const v = await pickMusic(musicState.value);
      if (v !== null) {
        musicState.value = v;
        musicRow.btn.textContent =
          v === "" ? "No music" : v === "auto" ? "Pick for me"
            : v === "random" ? "Surprise me" : v;
      }
    });
  const addBtn = el("button", { class: "btn btn-primary", type: "button" },
    "Add to the queue");
  const table = el("div", { class: "queue-table" });
  const inboxToggle = toggle(false, "Watch the inbox folder for new videos");
  inboxToggle.input.addEventListener("change", async () => {
    try {
      const r = await api.put("/api/batch/inbox",
        { enabled: inboxToggle.input.checked });
      toast(r.message || "Saved.");
    } catch (e) {
      inboxToggle.input.checked = !inboxToggle.input.checked;
      toast(e.message, "is-error");
    }
  });

  const renderRows = (rows) => {
    if (!rows.length) {
      table.replaceChildren(el("p", { class: "t-dim", style: "margin:0" },
        "The queue is empty — add a few links above."));
      return;
    }
    table.replaceChildren(...rows.map(([id, source, status, message]) =>
      el("div", { class: "queue-row" },
        el("span", { class: "t-mono t-dim" }, id),
        el("span", { class: "queue-src" }, source),
        el("span", {
          class: `badge ${status === "done" ? "badge-ok"
            : status === "error" ? "badge-warn"
              : status === "running" ? "badge-live" : ""}`,
        }, status === "done" ? "Done" : status === "error" ? "Didn't work"
          : status === "running" ? "Working" : "Waiting"),
        el("span", { class: "t-dim queue-msg" }, message || ""))));
  };
  const refresh = async () => {
    try { renderRows((await api.get("/api/batch")).rows); }
    catch { /* server briefly unreachable — next tick retries */ }
  };

  addBtn.addEventListener("click", async () => {
    const sources = linesIn.value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!sources.length) {
      toast("Add at least one link or file path first.", "is-error");
      return;
    }
    addBtn.disabled = true;
    try {
      const r = await api.post("/api/batch",
        { sources, music: musicState.value });
      toast(`Queued ${r.queued} video(s).`, "is-ok");
      linesIn.value = "";
      renderRows(r.rows);
    } catch (e) { toast(e.message, "is-error"); }
    addBtn.disabled = false;
  });

  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Queue"),
        el("h1", { class: "t-display" }, "Several videos at once"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "They run one after another — leave this open or come back later.")),
      el("a", { class: "btn", href: "/api/batch/zip" }, "Download everything")),
    el("div", { class: "queue-grid" },
      el("div", { class: "card", style: "display:grid;gap:16px" },
        field("Videos to turn into clips", linesIn),
        musicRow.node,
        el("div", {}, addBtn),
        inboxToggle.node),
      el("div", { class: "card card-flat" }, table))));

  refresh();
  const timer = setInterval(refresh, 3000);
  cleanup = () => clearInterval(timer);
}

// ------------------------------------------------------------- youtube ----

async function renderYouTube() {
  const body = el("div", { class: "yt-grid" },
    el("div", { class: "skeleton", style: "height:220px" }),
    el("div", { class: "skeleton", style: "height:220px" }));
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "YouTube"),
        el("h1", { class: "t-display" }, "Auto-upload"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "Finished clips get scheduled to your channel at good times of day."))),
    body));

  let st;
  try {
    st = await api.get("/api/youtube/state");
  } catch (e) {
    body.replaceChildren(el("div", { class: "card" },
      el("p", { class: "t-dim", style: "margin:0" }, e.message)));
    return;
  }

  const left = el("div", { class: "card", style: "display:grid;gap:16px" });
  const right = el("div", { class: "card card-flat", style: "display:grid;gap:12px" });
  const postQueueCard = el("div", {
    class: "card uq-span", style: "display:grid;gap:12px",
  });
  body.replaceChildren(left, right, postQueueCard);
  mountPostQueue(postQueueCard);

  if (!st.configured) {
    left.append(
      el("h2", { class: "t-title", style: "margin:0" }, "One-time setup"),
      el("p", { class: "t-dim", style: "margin:0" },
        "ClipForge needs a Google credentials file to talk to your channel. ",
        "It stays on this computer."),
      el("pre", { class: "setup-pre t-mono" },
        (st.setup_instructions || "").replace(/[#*`]/g, "")));
  } else if (!st.authorized) {
    const connectBtn = el("button", { class: "btn btn-primary", type: "button" },
      "Connect to YouTube");
    connectBtn.addEventListener("click", async () => {
      connectBtn.disabled = true;
      connectBtn.textContent = "A Google window is open — finish there…";
      try {
        await api.post("/api/youtube/authorize");
        toast("Connected.", "is-ok");
        navigate();   // re-render with the authorized panel
      } catch (e) {
        toast(e.message, "is-error");
        connectBtn.disabled = false;
        connectBtn.textContent = "Connect to YouTube";
      }
    });
    left.append(
      el("h2", { class: "t-title", style: "margin:0" }, "Connect your channel"),
      el("p", { class: "t-dim", style: "margin:0" },
        "One-time sign-in with Google. Nothing uploads until you turn ",
        "auto-upload on."),
      el("div", {}, connectBtn));
  } else {
    const panel = st.panel || {};
    const autoToggle = toggle(!!panel.auto_enabled, "Upload new clips automatically");
    autoToggle.input.addEventListener("change", async () => {
      try {
        await api.put("/api/youtube/auto",
          { enabled: autoToggle.input.checked });
        toast(autoToggle.input.checked
          ? "On — good clips get scheduled after each run."
          : "Off — nothing uploads on its own.", "is-ok");
      } catch (e) {
        autoToggle.input.checked = !autoToggle.input.checked;
        toast(e.message, "is-error");
      }
    });
    const slot = panel.next_slot_ist
      ? new Date(panel.next_slot_ist).toLocaleString([], {
          weekday: "short", hour: "2-digit", minute: "2-digit" })
      : "—";
    left.append(
      el("div", { class: "field-inline" },
        el("span", { class: "badge badge-ok" }, "Connected"), autoToggle.node),
      el("div", { class: "yt-stats" },
        el("div", {},
          el("div", { class: "t-hero-num" },
            `${panel.uploads_today ?? 0}/${panel.max_per_day ?? 3}`),
          el("div", { class: "t-label" }, "Scheduled today")),
        el("div", {},
          el("div", { class: "t-title" }, slot),
          el("div", { class: "t-label" }, "Next upload slot"))),
      el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-sm)" },
        "Uploads start private and go public at the scheduled time. ",
        "Use a clip's Auto-upload switch to leave it out."));
  }

  const recent = st.panel?.recent || [];
  right.append(el("h2", { class: "t-title", style: "margin:0" }, "Recent uploads"));
  right.append(el("p", { class: "t-dim", style: "margin:0" }, recent.length
    ? "Your uploaded videos are listed in the Uploaded section below, with links."
    : "Nothing uploaded yet — finished clips appear in the Uploaded section below."));

  if (st.authorized) {
    const queueCard = el("div", { class: "card uq-span", style: "display:grid;gap:16px" },
      el("div", {},
        el("h2", { class: "t-title", style: "margin:0" }, "Upload queue"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "Send clips right now instead of waiting for the next slot.")));
    body.append(queueCard);
    const uq = mountUploadQueue(queueCard);
    cleanup = uq.dispose;
  }
}

// ----------------------------------------------------------- analytics ----

function fmtIstHour(h) {
  return `${((h + 11) % 12) + 1} ${h < 12 ? "AM" : "PM"}`;
}

// publish_at is always an ISO string with a fixed +05:30 offset (see
// upload_scheduler.py's IST constant) — read the hour off the string
// directly rather than through Date, which would convert to the browser's
// local timezone and disagree with the backend's IST-based recommendations.
function istHourFromIso(iso) {
  const m = /T(\d{2}):/.exec(iso || "");
  return m ? Number(m[1]) : null;
}

function analyticsRecCard(rec) {
  const evidence = Object.entries(rec.evidence || {})
    .map(([k, v]) => `${k}: ${Array.isArray(v) || typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ");
  const applyBtn = rec.action?.kind === "apply_publish_slot"
    ? el("button", { class: "btn btn-sm", type: "button" },
        `Add ${fmtIstHour(rec.action.hour)} to publish slots`)
    : null;
  if (applyBtn) {
    applyBtn.addEventListener("click", async () => {
      applyBtn.disabled = true;
      try {
        await api.put("/api/analytics/publish-slot", { hour: rec.action.hour });
        toast("Added to your publish slots.", "is-ok");
      } catch (e) {
        toast(e.message, "is-error");
      } finally {
        applyBtn.disabled = false;
      }
    });
  }
  return el("div", { class: "an-rec" },
    el("p", { style: "margin:0" }, rec.message),
    evidence ? el("p", { class: "an-evidence" }, evidence) : null,
    applyBtn);
}

function analyticsVideoHeadRow() {
  return el("div", { class: "an-row an-row-head" },
    el("div", { class: "an-title" }, "Title"),
    el("div", {}, "Views"), el("div", {}, "Avg %"),
    el("div", {}, "Likes"), el("div", {}, "Subs+"));
}

function analyticsVideoRow(v) {
  return el("div", { class: "an-row" },
    el("div", { class: "an-title" }, v.title),
    el("div", {}, v.views),
    el("div", {}, `${v.avg_view_pct}%`),
    el("div", {}, v.likes),
    el("div", {}, v.subs_gained));
}

async function renderPresets() {
  const card = el("div", { class: "card", style: "display:grid;gap:12px" });
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Presets"),
        el("h1", { class: "t-display" }, "Your editing styles"))),
    card));
  mountPresets(card);
}

async function renderChannels() {
  const card = el("div", { class: "card", style: "display:grid;gap:12px" });
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Channels"),
        el("h1", { class: "t-display" }, "Approved channels"))),
    card));
  mountChannels(card);
}

async function renderAnalytics() {
  const body = el("div", { class: "yt-grid" },
    el("div", { class: "skeleton", style: "height:220px" }),
    el("div", { class: "skeleton", style: "height:220px" }));
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Analytics"),
        el("h1", { class: "t-display" }, "How your clips are doing"),
        el("p", { class: "t-dim", style: "margin:4px 0 0" },
          "Numbers and suggestions come only from your own uploaded clips."))),
    body));

  // Reads only local files (upload_log.json, config, its own stats store) —
  // mounted independently so it shows real gate status even before YouTube
  // is connected or if the main analytics fetch below fails.
  const scheduleCard = el("div", { class: "card uq-span", style: "display:grid;gap:12px" });
  mountPublishTiming(scheduleCard);

  let st;
  try {
    st = await api.get("/api/analytics/state");
  } catch (e) {
    body.replaceChildren(scheduleCard,
      el("div", { class: "card" }, el("p", { class: "t-dim", style: "margin:0" }, e.message)));
    return;
  }

  const left = el("div", { class: "card", style: "display:grid;gap:16px" });
  const right = el("div", { class: "card card-flat", style: "display:grid;gap:12px" });
  body.replaceChildren(scheduleCard, left, right);

  if (!st.configured) {
    left.append(
      el("h2", { class: "t-title", style: "margin:0" }, "One-time setup"),
      el("p", { class: "t-dim", style: "margin:0" },
        "Connect YouTube on the YouTube tab first — Analytics uses the same connection."),
      el("pre", { class: "setup-pre t-mono" },
        (st.setup_instructions || "").replace(/[#*`]/g, "")));
    return;
  }
  if (!st.authorized) {
    left.append(
      el("h2", { class: "t-title", style: "margin:0" }, "Connect your channel"),
      el("p", { class: "t-dim", style: "margin:0" },
        "Go to the YouTube tab and connect your channel — Analytics will show up here once you have."));
    return;
  }

  const overview = st.overview || {};
  const videos = st.videos || [];
  const recs = st.recommendations || [];

  const statTile = (label, stats) => el("div", {},
    el("div", { class: "t-hero-num" }, (stats?.views ?? 0).toLocaleString()),
    el("div", { class: "t-label" }, label),
    el("p", { class: "t-dim", style: "margin:4px 0 0;font-size:var(--text-sm)" },
      `${(stats?.avg_view_pct ?? 0)}% avg watch · +${stats?.subs_gained ?? 0} subs`));

  const refreshBtn = el("button", { class: "btn btn-sm", type: "button" }, "Refresh");
  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refreshing…";
    try {
      await api.get("/api/analytics/state?refresh=1");
      navigate();
    } catch (e) {
      toast(e.message, "is-error");
      refreshBtn.disabled = false;
      refreshBtn.textContent = "Refresh";
    }
  });

  left.append(
    el("div", { class: "field-inline" },
      el("h2", { class: "t-title", style: "margin:0" }, "Channel overview"),
      refreshBtn),
    el("div", { class: "yt-stats" },
      statTile("Last 28 days", overview["28d"]),
      statTile("Last 90 days", overview["90d"])),
    el("p", { class: "t-dim", style: "margin:0;font-size:var(--text-xs)" },
      st.fetched_at ? `Updated ${new Date(st.fetched_at).toLocaleString()}` : ""));

  const sorted = [...videos].sort((a, b) =>
    new Date(a.publish_at || 0) - new Date(b.publish_at || 0));
  left.append(
    el("h2", { class: "t-title", style: "margin:0" }, "Views over time"),
    sparklineSVG(sorted.map((v) => ({ y: v.views }))));

  const byHour = {};
  for (const v of videos) {
    const h = istHourFromIso(v.publish_at);
    if (h === null) continue;
    if (!byHour[h]) byHour[h] = [];
    byHour[h].push(v.views);
  }
  const hourBars = Object.keys(byHour).map(Number).sort((a, b) => a - b)
    .map((h) => ({ label: `${h}:00`,
                  value: byHour[h].reduce((a, b) => a + b, 0) / byHour[h].length }));
  left.append(
    el("h2", { class: "t-title", style: "margin:0" }, "Performance by publish hour"),
    barsSVG(hourBars));

  right.append(el("h2", { class: "t-title", style: "margin:0" }, "Recommendations"));
  if (recs.length) recs.forEach((r) => right.append(analyticsRecCard(r)));
  else right.append(el("p", { class: "t-dim", style: "margin:0" },
    "No recommendations yet."));

  const tableCard = el("div", { class: "card uq-span", style: "display:grid;gap:12px" },
    el("h2", { class: "t-title", style: "margin:0" }, "Your uploaded clips"),
    videos.length
      ? el("div", { class: "an-table" },
          analyticsVideoHeadRow(),
          ...videos.map((v) => analyticsVideoRow(v)))
      : el("p", { class: "t-dim", style: "margin:0" },
          "Nothing uploaded through ClipForge yet."));
  body.append(tableCard);
}

// ------------------------------------------------------------ settings ----

const PROVIDER_LABELS = {
  mock: "Offline — no API key needed",
  gemini: "Google Gemini (free key)",
  groq: "Groq (free key)",
  ollama: "Ollama (runs on this computer)",
  openrouter: "OpenRouter (free key)",
};

async function renderSettings() {
  const wrap = el("div", { class: "settings-grid" },
    el("div", { class: "skeleton", style: "height:320px" }),
    el("div", { class: "skeleton", style: "height:220px" }));
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Settings"),
        el("h1", { class: "t-display" }, "How ClipForge works"))),
    wrap));

  let s, sys, upd, storage;
  try {
    [s, sys, upd, storage] = await Promise.all([
      api.get("/api/settings"), api.get("/api/system"), api.get("/api/update"),
      api.get("/api/storage").catch(() => null)]);
  } catch (e) {
    wrap.replaceChildren(el("div", { class: "card" },
      el("p", { class: "t-dim", style: "margin:0" }, e.message)));
    return;
  }

  const providerSel = el("select", { class: "select" },
    ...Object.entries(PROVIDER_LABELS).map(([v, label]) =>
      el("option", v === s.provider ? { value: v, selected: "" } : { value: v },
        label)));
  const computeSel = el("select", { class: "select" },
    el("option", { value: "auto" }, "Decide for me"),
    el("option", { value: "gpu" }, "Always use the graphics card"),
    el("option", { value: "cpu" }, "Processor only (slower, always works)"));
  computeSel.value = s.compute || "auto";
  const whisperSel = el("select", { class: "select" },
    el("option", { value: "" }, "Automatic"),
    ...["tiny", "base", "small", "medium", "large-v3"].map((m) =>
      el("option", { value: m }, m)));
  whisperSel.value = s.whisper_model || "";
  const gemIn = el("input", { class: "input", type: "text",
                              placeholder: "e.g. gemini-2.0-flash" });
  gemIn.value = s.gemini_model || "";
  const groqIn = el("input", { class: "input", type: "text" });
  groqIn.value = s.groq_model || "";
  const ollamaIn = el("input", { class: "input", type: "text" });
  ollamaIn.value = s.ollama_model || "";
  const nichesIn = el("input", { class: "input", type: "text",
                                 placeholder: "e.g. cooking, travel" });
  nichesIn.value = s.custom_niches || "";
  const approvalToggle = toggle(!!s.require_approval,
    "Require approval before upload");

  const saveBtn = el("button", { class: "btn btn-primary", type: "button" }, "Save");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      await api.put("/api/settings", {
        compute: computeSel.value,
        whisper_model: whisperSel.value,
        provider: providerSel.value,
        gemini_model: gemIn.value,
        groq_model: groqIn.value,
        ollama_model: ollamaIn.value,
        custom_niches: nichesIn.value,
        require_approval: approvalToggle.input.checked,
      });
      toast("Saved — takes effect on the next run.", "is-ok");
    } catch (e) { toast(e.message, "is-error"); }
    saveBtn.disabled = false;
  });

  const accel = { full: "Full speed (graphics card)",
                  partial: "Partly accelerated",
                  cpu: "Processor only" }[sys.acceleration] || sys.acceleration;

  const updRow = el("div", { class: "field-inline" });
  const renderUpdate = (state) => {
    updRow.replaceChildren();
    if (state?.update_available) {
      const installBtn = el("button", { class: "btn btn-primary btn-sm",
                                        type: "button" },
        `Install ${state.latest}`);
      installBtn.addEventListener("click", async () => {
        installBtn.disabled = true;
        installBtn.textContent = "Installing…";
        try {
          const r = await api.post("/api/update/apply");
          toast(r.message || "Updated — restart ClipForge to finish.", "is-ok");
        } catch (e) { toast(e.message, "is-error"); }
        installBtn.disabled = false;
      });
      updRow.append(
        el("span", { class: "badge badge-warn" }, `New: ${state.latest}`),
        installBtn);
    } else {
      const checkBtn = el("button", { class: "btn btn-sm", type: "button" },
        "Check for updates");
      checkBtn.addEventListener("click", async () => {
        checkBtn.disabled = true;
        checkBtn.textContent = "Checking…";
        try {
          const r = await api.post("/api/update/check");
          if (r.state?.update_available) renderUpdate(r.state);
          else toast("You're on the newest version.", "is-ok");
        } catch (e) { toast(e.message, "is-error"); }
        checkBtn.disabled = false;
        checkBtn.textContent = "Check for updates";
      });
      updRow.append(checkBtn);
    }
  };
  renderUpdate(upd.state);

  wrap.replaceChildren(
    el("div", { class: "card", style: "display:grid;gap:16px" },
      el("h2", { class: "t-title", style: "margin:0" }, "Understanding & speed"),
      field("Writing titles and finding moments", providerSel),
      field("Gemini model (blank = default)", gemIn),
      field("Groq model (blank = default)", groqIn),
      field("Ollama model (blank = default)", ollamaIn),
      field("Speed", computeSel),
      field("Speech recognition accuracy", whisperSel),
      field("Custom niches (comma-separated)", nichesIn),
      approvalToggle.node,
      el("div", {}, saveBtn)),
    el("div", { class: "card card-flat", style: "display:grid;gap:12px" },
      el("h2", { class: "t-title", style: "margin:0" }, "This computer"),
      kv("Version", upd.current || sys.version),
      kv("Speed", accel),
      kv("Processor cores", String(sys.cpu_count ?? "—")),
      storage ? kv("Clips on disk", storage.cleanable_bytes
        ? `${fmtBytes(storage.total_bytes)} · ${fmtBytes(storage.cleanable_bytes)} reclaimable`
        : fmtBytes(storage.total_bytes)) : null,
      el("div", { class: "hr", style: "margin:4px 0" }),
      updRow));
}

function kv(k, v) {
  return el("div", { class: "kv" },
    el("span", { class: "t-label" }, k),
    el("span", { class: "t-mono" }, v));
}

// ------------------------------------------------------------- history ----

async function renderHistory() {
  const grid = el("div", { class: "history-grid" },
    ...Array.from({ length: 4 }, () =>
      el("div", { class: "skeleton", style: "height:120px" })));
  const filterRow = el("div", { class: "field-inline",
                               style: "flex-wrap:wrap" });
  const backfillBtn = el("button", { class: "btn btn-ghost", type: "button" },
    "Tag older clips");
  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "History"),
        el("h1", { class: "t-display" }, "Past runs")),
      el("div", { class: "field-inline" },
        backfillBtn,
        el("a", { class: "btn btn-ghost", href: "#/" }, "Make new clips"))),
    filterRow, grid));

  let jobs = [];
  let activeNiche = null;

  const jobCard = (j) => {
    const when = j.name.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
    const date = when
      ? `${when[3]}.${when[2]}.${when[1]} ${when[4]}:${when[5]}` : j.created;
    return el("a", {
      class: "card card-hover history-card",
      href: `#/results/${encodeURIComponent(j.name)}`,
    },
      el("div", { class: "t-mono t-dim" }, date),
      el("div", { class: "history-src" }, j.source || j.name),
      el("div", { class: "field-inline" },
        el("span", { class: "badge" }, `${j.kept} of ${j.clip_count} clips kept`),
        ...(j.niches || []).map((n) => el("span", { class: "badge" }, n)),
        j.status && j.status !== "done"
          ? el("span", { class: "badge badge-warn" }, j.status)
          : null));
  };

  const renderGrid = () => {
    const shown = activeNiche
      ? jobs.filter((j) => (j.niches || []).includes(activeNiche)) : jobs;
    if (!shown.length) {
      grid.replaceChildren(el("div", { class: "card" },
        el("p", { class: "t-dim", style: "margin:0" }, activeNiche
          ? "No runs with clips in that niche."
          : "No clips yet — paste a link on the first screen to start.")));
      return;
    }
    grid.replaceChildren(...shown.map(jobCard));
  };

  const renderFilters = () => {
    const niches = [...new Set(jobs.flatMap((j) => j.niches || []))].sort();
    if (!niches.length) { filterRow.replaceChildren(); return; }
    const chip = (label, value) => {
      const b = el("button", {
        class: `btn btn-sm ${activeNiche === value ? "btn-primary" : "btn-ghost"}`,
        type: "button",
      }, label);
      b.addEventListener("click", () => {
        activeNiche = value;
        renderFilters();
        renderGrid();
      });
      return b;
    };
    filterRow.replaceChildren(chip("All", null),
      ...niches.map((n) => chip(n, n)));
  };

  const load = async () => {
    try {
      jobs = (await api.get("/api/jobs")).jobs;
    } catch (e) {
      grid.replaceChildren(el("div", { class: "card" },
        el("p", { class: "t-dim", style: "margin:0" }, e.message)));
      return;
    }
    renderFilters();
    renderGrid();
  };

  backfillBtn.addEventListener("click", async () => {
    backfillBtn.disabled = true;
    backfillBtn.textContent = "Tagging…";
    try {
      const r = await api.post("/api/classify/backfill");
      toast(r.classified
        ? `Tagged ${r.classified} clip${r.classified === 1 ? "" : "s"} by niche.`
        : "Everything is already tagged.", "is-ok");
      await load();
    } catch (e) { toast(e.message, "is-error"); }
    backfillBtn.disabled = false;
    backfillBtn.textContent = "Tag older clips";
  });

  await load();
}

// ---------------------------------------------------------------- boot ----

navigate();

// quiet once-per-launch nudge when a newer version is available
api.get("/api/update").then((u) => {
  if (u.state?.update_available) {
    toast(`A new version (${u.state.latest}) is ready — install it in Settings.`);
  }
}).catch(() => { /* offline or updater unavailable — stay quiet */ });
