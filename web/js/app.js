/* ClipForge frontend — hash-routed views over the FastAPI backend.
 * Screens: home (submit), run (live progress), results (clip gallery).
 * All copy is plain language; server sentences are shown verbatim. */

import { api, uploadFile, watchRun } from "./api.js";
import { createDotMatrix, miniScore } from "./dots.js";

const view = document.getElementById("view");
let cleanup = null; // per-view teardown (websockets, dot-matrix rafs)

// ------------------------------------------------------------- helpers ----

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function toast(message, kind = "") {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = el("div", { class: `toast ${kind}`, role: "status" }, message);
  document.body.append(t);
  setTimeout(() => t.remove(), 5000);
}

function fmtClock(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return "—";
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// -------------------------------------------------------------- router ----

const routes = {
  "": renderHome,
  run: renderRun,
  results: renderResults,
};

function navigate() {
  if (cleanup) { cleanup(); cleanup = null; }
  const [, name = "", arg = ""] = location.hash.split("/");
  view.replaceChildren();
  (routes[name.replace("#", "")] || renderHome)(decodeURIComponent(arg));
}
window.addEventListener("hashchange", navigate);

// ---------------------------------------------------------------- home ----

function renderHome() {
  let filePath = "";   // server path returned by /api/uploads
  let fileName = "";

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

  // options (collapsed by default; defaults mean nobody has to open this)
  const clipCount = el("input", {
    class: "range", type: "range", min: "0", max: "12", value: "0",
    "aria-label": "How many clips",
  });
  const clipCountOut = el("span", { class: "t-mono t-dim" }, "Automatic");
  clipCount.addEventListener("input", () => {
    clipCountOut.textContent = clipCount.value === "0" ? "Automatic" : clipCount.value;
  });
  const aspectSel = el("select", { class: "select", "aria-label": "Clip shape" },
    el("option", { value: "9:16", selected: "" }, "Tall — for Shorts, Reels, TikTok"),
    el("option", { value: "1:1" }, "Square"),
    el("option", { value: "16:9" }, "Widescreen"));
  const presetSel = el("select", { class: "select", "aria-label": "Caption style" });
  const musicSel = el("select", { class: "select", "aria-label": "Background music" },
    el("option", { value: "" }, "No music"),
    el("option", { value: "auto" }, "Pick music for me"),
    el("option", { value: "random" }, "Surprise me"));
  const ctaInput = el("input", {
    class: "input", type: "text", maxlength: "60",
    placeholder: "e.g. Follow for more",
  });
  const styleToggle = toggle(true, "Polish clip timing");
  const viralToggle = toggle(true, "Look for big moments");

  function toggle(checked, label) {
    const input = el("input", { type: "checkbox" });
    input.checked = checked;
    return {
      input,
      node: el("label", { class: "opt-toggle" }, label,
        el("span", { class: "switch" }, input, el("span", { class: "knob" }))),
    };
  }

  const field = (label, control, extra) =>
    el("div", { class: "field" },
      el("label", {}, label),
      extra ? el("div", { style: "display:flex;gap:12px;align-items:center" },
        control, extra) : control);

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
          field("Clip shape", aspectSel),
          field("Caption style", presetSel),
          field("Background music", musicSel),
          field("Ending message", ctaInput),
          el("div", { class: "field" },
            el("label", {}, "Extras"), styleToggle.node, viralToggle.node))),
      el("div", {}, makeBtn)),
    filePicker));

  // populate selects (offline endpoints; failures leave usable defaults)
  api.get("/api/presets").then((p) => {
    for (const name of p.presets) {
      presetSel.append(el("option",
        name === p.default ? { value: name, selected: "" } : { value: name },
        name.replace(/-/g, " ")));
    }
  }).catch(() => presetSel.append(el("option", { value: "" }, "Standard")));
  api.get("/api/music").then((m) => {
    for (const t of m.tracks) {
      musicSel.append(el("option", { value: t.id }, t.title || t.id));
    }
  }).catch(() => {});

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
    fileName = file.name;
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
          onclick: () => { filePath = ""; fileName = ""; fileChipWrap.replaceChildren(); },
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
        target_count: Number(clipCount.value) || null,
        aspect: aspectSel.value,
        preset: presetSel.value || null,
        music: musicSel.value,
        cta_text: ctaInput.value.trim(),
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

// ------------------------------------------------------------ progress ----

function renderRun(runId) {
  const stageTitle = el("h1", { class: "t-display" }, "Getting ready");
  const detail = el("p", { class: "t-dim", style: "margin:0;min-height:1.5em" }, " ");
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
    detail.textContent = (running && (running.message || running.current_file)) || " ";
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
      detail.textContent = " ";
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
      cancelBtn.textContent = "Back";
      cancelBtn.disabled = false;
      cancelBtn.onclick = () => { location.hash = "#/"; };
    },
  });

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Stopping…";
    try {
      await api.post(`/api/runs/${runId}/cancel`);
    } catch (e) {
      toast(e.message, "is-error");
      cancelBtn.disabled = false;
      cancelBtn.textContent = "Stop";
    }
  });

  // page refresh mid-run: the registry may already hold a snapshot
  api.get(`/api/runs/${runId}`).then((st) => {
    if (st.snapshot) apply(st.snapshot);
    if (st.state === "done") location.hash = `#/results/${encodeURIComponent(runId)}`;
  }).catch(() => {
    toast("That run isn't active anymore — check your finished clips.");
    location.hash = "#/";
  });

  cleanup = () => { stop(); dm.destroy(); };
}

// ------------------------------------------------------------- results ----

async function renderResults(jobName) {
  const grid = el("div", { class: "clip-grid" },
    ...Array.from({ length: 3 }, () =>
      el("div", { class: "skeleton skeleton-card" })));
  const sub = el("p", { class: "t-dim", style: "margin:4px 0 0" }, "Loading…");

  view.append(el("section", { class: "screen" },
    el("div", { class: "results-head" },
      el("div", {},
        el("div", { class: "t-label" }, "Your clips"),
        el("h1", { class: "t-display" }, "Ready to post"),
        sub),
      el("div", { style: "display:flex;gap:12px" },
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
    .sort((a, b) => (b.kept === true) - (a.kept === true) || a.index - b.index);
  const src = clips[0]?.source_name || job.source || "";
  sub.textContent = clips.length
    ? `${clips.filter((c) => c.kept).length} clips from ${src}`
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

  const srcStart = clip.original_source_start_s ?? clip.start;
  const srcEnd = clip.original_source_end_s ?? clip.end;

  const keptBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" });
  const card = el("article", {
    class: `card card-hover clip-card ${clip.kept ? "" : "is-discarded"}`,
  });
  const setKeptUi = (kept) => {
    keptBtn.textContent = kept ? "Discard" : "Keep";
    card.classList.toggle("is-discarded", !kept);
  };
  setKeptUi(clip.kept !== false);
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

  card.append(
    el("video", { controls: "", preload: "metadata", src: fileUrl("final.mp4") }),
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
          class: "btn btn-sm", href: fileUrl("final.mp4"),
          download: `clip_${nn}.mp4`,
        }, "Download"),
        keptBtn)));
  return card;
}

// ---------------------------------------------------------------- boot ----

navigate();
