/* Avatar Host tab: the avatar workspace IS the page. Pick an avatar image and
 * a clip (clip choosing lives in a popup), generate/edit the intro+outro
 * scripts (with per-voice audio previews of the real text), pick voice/side/
 * size, render with a dot-matrix + honest per-stage ETA, then approve/reject/
 * re-render. Reuses the same /ws/runs/{id} progress mechanism as rerender; the
 * active run is persisted so a page refresh reattaches instead of looking idle. */

import { api, uploadFile, watchRun } from "./api.js";
import { createDotMatrix } from "./dots.js";
import { pickAvatarImage, pickClip, pickVoice } from "./pickers.js";
import { clipVideo, el, field, fmtClock, thumbButton, toast } from "./ui.js";

const WORDS_PER_SECOND = 2.5; // rough TTS-pace heuristic, not measured
const ACTIVE_KEY = "clipforge.avatar.activeRun";

// avatar stage sub-item name (from avatar.py) -> our ETA stage key
const STAGE_ITEM = {
  tts: "voice synthesis (TTS)",
  lipsync_intro: "lip-sync (intro)",
  lipsync_outro: "lip-sync (outro)",
  composite: "compositing (ffmpeg)",
};

const wordCount = (t) => (t || "").trim().split(/\s+/).filter(Boolean).length;
const fmtLeft = (s) => {
  s = Math.max(0, Math.round(s));
  return s >= 90 ? `${Math.round(s / 60)} min` : `${s}s`;
};

export function mountAvatar(container) {
  const state = {
    clip: null, jobName: "", index: 0,
    voices: [], engine: "", voice: "",
    side: "left", scale: 0.42, avatarImage: null, avatarImages: [],
    pendingReattach: null,
  };

  const avatarField = el("div", { class: "avatar-picker" });
  const previewWrap = el("div", { class: "avatar-preview-wrap", style: "display:none" });
  const clipSlot = el("div", {});
  const formWrap = el("div", { style: "display:none;gap:12px;grid-template-columns:1fr" });
  container.append(
    el("p", { class: "t-dim", style: "margin:0" },
      "Pick an avatar, choose a clip, and host it."),
    avatarField, previewWrap, clipSlot, formWrap);

  // mount-scoped render machinery (shared across clip re-selections)
  let dm = null;             // dot-matrix (recreated per clip form)
  let activeWatch = null;    // watchRun stop fn
  let ticker = null;         // 1s elapsed repaint interval
  const stopWatch = () => { if (activeWatch) { activeWatch(); activeWatch = null; } };
  const stopTicker = () => { if (ticker) { clearInterval(ticker); ticker = null; } };
  const clearActive = () => { try { localStorage.removeItem(ACTIVE_KEY); } catch { /* ignore */ } };
  const saveActive = (runId, estimate) => {
    try {
      localStorage.setItem(ACTIVE_KEY, JSON.stringify({
        run_id: runId, jobName: state.jobName, index: state.index, estimate }));
    } catch { /* storage disabled — reattach just won't work */ }
  };

  // drag-and-drop a PNG anywhere onto the avatar field. Set up once.
  avatarField.addEventListener("dragover", (e) => {
    e.preventDefault(); avatarField.classList.add("is-drop");
  });
  avatarField.addEventListener("dragleave", () => avatarField.classList.remove("is-drop"));
  avatarField.addEventListener("drop", (e) => {
    e.preventDefault(); avatarField.classList.remove("is-drop");
    const file = e.dataTransfer.files[0];
    if (file) uploadAvatar(file);
  });

  async function uploadAvatar(file) {
    try {
      const { path } = await uploadFile("/api/uploads/avatar_image", file);
      toast("Avatar added.", "is-ok");
      state.avatarImage = path;
      await refreshAvatarImages();
    } catch (e) { toast(e.message, "is-error"); }
  }

  function avatarThumbUrl(path) {
    if (!path) return null;
    return `/api/avatar/images/${encodeURIComponent(path.split("/").pop())}`;
  }

  function renderAvatarField() {
    const tiles = state.avatarImages.map((img) => {
      const tile = el("button", {
        class: `avatar-tile ${img.path === state.avatarImage ? "is-selected" : ""}`,
        type: "button", title: img.name, "data-avatar-pick": img.path,
      }, el("img", { src: img.url, alt: img.name }));
      tile.addEventListener("click", () => {
        state.avatarImage = img.path;
        renderAvatarField();
        buildPreviewFrame();
      });
      return tile;
    });

    const fileIn = el("input", { type: "file", accept: ".png", class: "visually-hidden" });
    fileIn.addEventListener("change", () => {
      if (fileIn.files[0]) uploadAvatar(fileIn.files[0]);
    });
    const uploadTile = el("label", { class: "avatar-tile avatar-upload", title: "Upload a PNG" },
      el("span", { class: "avatar-plus" }, "+"),
      el("span", { class: "t-dim", style: "font-size:var(--text-xs)" }, "Upload"),
      fileIn);

    const browseBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Browse all");
    browseBtn.addEventListener("click", async () => {
      const picked = await pickAvatarImage(state.avatarImage);
      if (picked === null) return;
      state.avatarImage = picked;
      await refreshAvatarImages();
    });

    avatarField.replaceChildren(
      el("div", { class: "avatar-strip" }, ...tiles, uploadTile),
      el("div", { class: "avatar-drop-hint t-dim" },
        state.avatarImages.length ? "Drop a transparent .png here, or "
                                  : "No saved avatars yet — drop a transparent .png here, or ",
        browseBtn));
  }

  async function loadVoices() {
    try {
      const voices = await api.get("/api/avatar/voices");
      state.voices = voices.voices || [];
      state.engine = voices.engine || "";
    } catch (e) { toast(e.message, "is-error"); }
  }

  async function refreshAvatarImages() {
    try {
      const data = await api.get("/api/avatar/images");
      state.avatarImages = data.images || [];
      if (!state.avatarImage) state.avatarImage = data.last_used || null;
    } catch { state.avatarImages = []; }
    renderAvatarField();
    buildPreviewFrame();
  }

  // ------------------------------------------------- chosen-clip slot / popup

  function renderClipSlot() {
    if (!state.clip) {
      const btn = el("button", { class: "btn btn-primary", type: "button" }, "Choose clip");
      btn.addEventListener("click", chooseClip);
      clipSlot.replaceChildren(field("Clip to host", btn));
      return;
    }
    const changeBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Change");
    changeBtn.addEventListener("click", chooseClip);
    const card = el("div", { class: "avatar-clip-card" },
      el("span", { "data-no-pick": "" },
        thumbButton(state.clip.video_url, state.clip.title, "avatar-clip-thumb")),
      el("div", { class: "avatar-clip-meta" },
        el("div", { class: "uq-title" }, state.clip.title),
        el("div", { class: "t-dim t-mono", style: "font-size:var(--text-xs)" }, state.clip.job)),
      changeBtn);
    clipSlot.replaceChildren(field("Clip to host", card));
  }

  async function chooseClip() {
    const clip = await pickClip(state.clip ? state.clip.key : "");
    if (!clip) return;
    await selectClip(clip);
    renderClipSlot();
  }

  // ------------------------------------------------------------- preview frame

  let previewOverlay = null;

  function buildPreviewFrame() {
    if (!state.avatarImage && !state.clip) {
      previewWrap.style.display = "none";
      return;
    }
    previewWrap.style.display = "";
    previewOverlay = el("img", { class: "avatar-preview-overlay" });
    previewOverlay.hidden = true;
    const base = state.clip
      ? clipVideo(state.clip.video_url, { muted: "", class: "avatar-preview-clip" })
      : el("div", { class: "avatar-preview-dummy" });
    const caption = state.clip
      ? null
      : el("div", { class: "t-dim", style: "font-size:var(--text-xs);margin-top:6px" },
          "Preview on a blank 9:16 frame — pick a clip to see it on real footage.");
    previewWrap.replaceChildren(...[
      el("div", { class: "avatar-preview-frame" }, base, previewOverlay),
      caption,
    ].filter(Boolean));
    updateOverlay();
  }

  function updateOverlay() {
    if (!previewOverlay) return;
    const avatarUrl = avatarThumbUrl(state.avatarImage);
    if (!avatarUrl) { previewOverlay.hidden = true; return; }
    const side = state.side === "right" ? "right" : "left";
    const widthPct = Math.round(state.scale * 100);
    if (previewOverlay.src !== new URL(avatarUrl, location.href).href) {
      previewOverlay.src = avatarUrl;
    }
    previewOverlay.style.cssText = `width:${widthPct}%;${side}:0`;
    previewOverlay.hidden = false;
  }

  async function selectClip(c) {
    const m = /^output\/([^/]+)\/clip_(\d+)$/.exec(c.key);
    if (!m) { toast("Couldn't resolve that clip's job/index.", "is-error"); return; }
    state.clip = c;
    state.jobName = m[1];
    state.index = Number(m[2]);
    buildPreviewFrame();
    formWrap.style.display = "";
    formWrap.replaceChildren(el("div", { class: "skeleton", style: "height:120px" }));
    let script;
    try {
      script = await api.post(
        `/api/jobs/${encodeURIComponent(state.jobName)}/clips/${state.index}/avatar/script`);
    } catch (e) { formWrap.replaceChildren(el("p", { class: "t-dim" }, e.message)); return; }
    buildForm(script);
  }

  function wordStats(text) {
    const words = wordCount(text);
    const secs = words / WORDS_PER_SECOND;
    return `${words} word${words === 1 ? "" : "s"} · ~${secs.toFixed(1)}s`;
  }

  function scriptField(label, initial, regenerateFn, previewFn) {
    const ta = el("textarea", { rows: "3" });
    ta.value = initial || "";
    const stats = el("span", { class: "t-dim", style: "font-size:var(--text-xs)" },
      wordStats(ta.value));
    ta.addEventListener("input", () => { stats.textContent = wordStats(ta.value); });

    const playBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "▶ Hear");
    playBtn.addEventListener("click", async () => {
      const text = (ta.value || "").trim();
      if (!text) { toast("Nothing to say yet.", "is-error"); return; }
      playBtn.disabled = true;
      const orig = playBtn.textContent;
      playBtn.textContent = "generating…";
      try { await previewFn(text); }
      catch (e) { if (e.name !== "AbortError") toast(e.message, "is-error"); }
      finally { playBtn.textContent = orig; playBtn.disabled = false; }
    });

    const regenBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
      "Regenerate");
    regenBtn.addEventListener("click", async () => {
      regenBtn.disabled = true;
      try {
        const fresh = await regenerateFn();
        ta.value = fresh;
        stats.textContent = wordStats(ta.value);
        toast(`${label} regenerated.`, "is-ok");
      } catch (e) {
        toast(e.message, "is-error");
      } finally {
        regenBtn.disabled = false;
      }
    });
    return { ta, node: field(label, ta,
      el("div", { class: "field-inline" }, stats, playBtn, regenBtn)) };
  }

  function buildForm(script) {
    // switching clips: tear down any in-flight render UI for the old clip
    stopWatch(); stopTicker();
    if (dm) { dm.destroy(); dm = null; }

    let estimate = null;   // {stages:[{key,name,est_s}], total_s, has_history}
    let lastItems = {};
    let renderStart = 0;   // Date.now() anchor, aligned to server elapsed
    const previewAudio = new Audio();

    async function regenerate(field_) {
      const fresh = await api.post(
        `/api/jobs/${encodeURIComponent(state.jobName)}/clips/${state.index}/avatar/script/regenerate`);
      return fresh[field_] || "";
    }
    async function previewScript(text) {
      if (!state.voice) throw new Error("Pick a voice first.");
      const { url } = await api.post("/api/avatar/voice-preview",
        { voice: state.voice, text });
      previewAudio.src = url;
      await previewAudio.play();
    }

    const intro = scriptField("Intro script", script.intro,
      () => regenerate("intro"), previewScript);
    const outro = scriptField("Outro script", script.outro,
      () => regenerate("outro"), previewScript);

    const transcriptDetails = el("details", {},
      el("summary", {}, "Source transcript"),
      el("p", { class: "t-dim", style: "font-size:var(--text-xs)" },
        script.transcript || "(no transcript available)"));

    state.voice = state.voice || state.voices[0]?.id || "";
    const voiceLabel = () => {
      const v = state.voices.find((x) => x.id === state.voice);
      return v ? v.label : "Choose voice";
    };
    const voiceBtn = el("button", { class: "btn picker-btn", type: "button" }, voiceLabel());
    voiceBtn.addEventListener("click", async () => {
      const picked = await pickVoice(state.voice, state.voices, () => intro.ta.value);
      if (picked === null) return;
      state.voice = picked;
      voiceBtn.textContent = voiceLabel();
    });

    const sideSel = el("select", {},
      el("option", { value: "left" }, "Left"),
      el("option", { value: "right" }, "Right"));
    sideSel.value = state.side;
    sideSel.addEventListener("change", () => {
      state.side = sideSel.value; updateOverlay();
    });

    const sizeIn = el("input", { type: "range", min: "0.25", max: "0.6", step: "0.01" });
    sizeIn.value = String(state.scale);
    sizeIn.addEventListener("input", () => {
      state.scale = Number(sizeIn.value); updateOverlay();
    });

    const engineRow = state.engine
      ? field("Engine", el("p", { class: "t-dim", style: "margin:0" }, state.engine))
      : null;

    const progCanvas = el("canvas");
    const etaLine = el("div", { class: "avatar-eta t-mono", style: "font-size:var(--text-xs)" });
    const stageList = el("div", { class: "avatar-stagelist" });
    const roughNote = el("div", { class: "t-dim", style: "font-size:var(--text-xs)" });
    const progWrap = el("div", { class: "avatar-progress", style: "display:none" },
      progCanvas, etaLine, stageList, roughNote);
    const resultWrap = el("div");
    const renderBtn = el("button", { class: "btn btn-primary", type: "button" }, "Render");

    // ---- progress painting (honest ETA from server estimate + elapsed) ----
    const currentElapsed = () => (renderStart ? (Date.now() - renderStart) / 1000 : 0);

    function paintProgress() {
      const elapsedS = currentElapsed();
      const total = estimate ? estimate.total_s : null;
      let frac = total ? Math.min(0.98, elapsedS / total) : 0;
      if (estimate && total) {
        const doneEst = estimate.stages
          .filter((s) => (lastItems[STAGE_ITEM[s.key]] || 0) >= 1)
          .reduce((a, s) => a + s.est_s, 0);
        frac = Math.max(frac, Math.min(0.98, doneEst / total));
      }
      if (dm) dm.update({ fraction: frac, state: "running" });

      const active = estimate
        ? estimate.stages.find((s) => (STAGE_ITEM[s.key] in lastItems)
              && (lastItems[STAGE_ITEM[s.key]] || 0) < 1)
          || estimate.stages.find((s) => (lastItems[STAGE_ITEM[s.key]] || 0) < 1)
        : null;
      const activeName = active ? active.name : "working";

      let remain;
      if (total == null) remain = "estimating…";
      else {
        const r = total - elapsedS;
        remain = r > 1 ? `~${fmtLeft(r)} left` : "running long — still working";
      }
      etaLine.textContent = `${fmtClock(elapsedS)} · ${activeName} · ${remain}`;

      if (estimate) {
        stageList.replaceChildren(...estimate.stages.map((s) => {
          const v = lastItems[STAGE_ITEM[s.key]] || 0;
          const done = v >= 1;
          const act = (STAGE_ITEM[s.key] in lastItems) && !done;
          const mark = done ? "✓" : act ? "…" : "·";
          return el("div", { class: `avatar-stage-row t-dim${done ? " is-done" : ""}` },
            `${mark} ${s.name} — ~${fmtLeft(s.est_s)}`);
        }));
      }
      roughNote.textContent = (estimate && !estimate.has_history)
        ? "First render estimates are rough — they improve after each run." : "";
    }

    function applySnapshot(snap) {
      const st = (snap && snap.stages || []).find((s) => s.key === "avatar");
      if (st) {
        lastItems = st.items || {};
        if (st.elapsed > 0) renderStart = Date.now() - st.elapsed * 1000;
      }
      paintProgress();
    }

    function showResult() {
      etaLine.textContent = "Done.";
      resultWrap.replaceChildren(
        clipVideo(`/api/files/${encodeURIComponent(state.jobName)}/clip_${String(state.index).padStart(2, "0")}/final.mp4?t=${Date.now()}`),
        approvalRow());
    }

    function approvalRow() {
      const approveBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" }, "Approve");
      const rejectBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Reject");
      const rerenderBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Re-render");
      const act = async (approval) => {
        approveBtn.disabled = rejectBtn.disabled = rerenderBtn.disabled = true;
        try {
          await api.put(
            `/api/jobs/${encodeURIComponent(state.jobName)}/clips/${state.index}/approval`,
            { approval });
          toast(approval === "approved"
            ? "Approved — it can now be scheduled."
            : "Rejected — it won't be uploaded.", "is-ok");
        } catch (e) {
          toast(e.message, "is-error");
        } finally {
          approveBtn.disabled = rejectBtn.disabled = rerenderBtn.disabled = false;
        }
      };
      approveBtn.addEventListener("click", () => act("approved"));
      rejectBtn.addEventListener("click", () => act("rejected"));
      rerenderBtn.addEventListener("click", () => {
        resultWrap.replaceChildren();
        progWrap.style.display = "none";
        renderBtn.disabled = false;
      });
      return el("div", { class: "field-inline" }, approveBtn, rejectBtn, rerenderBtn);
    }

    function attachRun(runId) {
      stopWatch();
      activeWatch = watchRun(runId, {
        onSnapshot: applySnapshot,
        onDone: () => {
          if (dm) dm.update({ state: "done" });
          stopTicker(); clearActive(); showResult();
          renderBtn.disabled = false;
          toast("Avatar applied — review below.", "is-ok");
        },
        onCancelled: () => { stopTicker(); clearActive(); renderBtn.disabled = false; },
        onError: (message) => {
          if (dm) dm.update({ state: "error" });
          stopTicker(); clearActive();
          etaLine.textContent = message;
          toast(message, "is-error");
          renderBtn.disabled = false;
        },
      });
    }

    renderBtn.addEventListener("click", async () => {
      if (!state.avatarImage) {
        toast("Choose an avatar image before rendering.", "is-error");
        return;
      }
      const audioS = (wordCount(intro.ta.value) + wordCount(outro.ta.value)) / WORDS_PER_SECOND;
      try {
        estimate = await api.get(`/api/avatar/render-estimate?audio_s=${audioS.toFixed(1)}`);
      } catch { estimate = null; }

      renderBtn.disabled = true;
      resultWrap.replaceChildren();
      progWrap.style.display = "";
      if (!dm) dm = createDotMatrix(progCanvas, { variant: "progress" });
      dm.update({ fraction: 0, state: "running" });
      renderStart = Date.now();
      lastItems = {};
      stopTicker(); ticker = setInterval(paintProgress, 1000); paintProgress();

      try {
        const { run_id } = await api.post(
          `/api/jobs/${encodeURIComponent(state.jobName)}/clips/${state.index}/avatar/render`, {
            intro_script: intro.ta.value, outro_script: outro.ta.value,
            voice: state.voice, side: state.side, avatar_scale: state.scale,
            avatar_image: state.avatarImage || undefined,
          });
        saveActive(run_id, estimate);
        attachRun(run_id);
      } catch (e) {
        toast(e.message, "is-error");
        stopTicker();
        progWrap.style.display = "none";
        renderBtn.disabled = false;
      }
    });

    formWrap.replaceChildren(...[
      intro.node, outro.node, transcriptDetails,
      field("Voice", voiceBtn),
      field("Avatar side", sideSel),
      field("Avatar size", sizeIn),
      engineRow,
      renderBtn, progWrap, resultWrap,
    ].filter(Boolean));

    // reattach to an in-flight (or just-finished) render after a page refresh
    if (state.pendingReattach) {
      const pr = state.pendingReattach;
      state.pendingReattach = null;
      estimate = pr.estimate || estimate;
      if (pr.status.state === "running") {
        renderBtn.disabled = true;
        progWrap.style.display = "";
        dm = createDotMatrix(progCanvas, { variant: "progress" });
        stopTicker(); ticker = setInterval(paintProgress, 1000);
        attachRun(pr.run_id);
        if (pr.status.snapshot) applySnapshot(pr.status.snapshot);
        else paintProgress();
      } else if (pr.status.state === "done") {
        progWrap.style.display = "";
        dm = createDotMatrix(progCanvas, { variant: "progress" });
        dm.update({ state: "done" });
        showResult();
        clearActive();
      } else {
        clearActive();
      }
    }
  }

  // -------------------------------------------------------------- reattach

  async function maybeReattach() {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null"); }
    catch { saved = null; }
    if (!saved || !saved.run_id) return;
    let status;
    try { status = await api.get(`/api/runs/${encodeURIComponent(saved.run_id)}`); }
    catch { clearActive(); return; }   // 404 — run is gone
    if (status.state !== "running" && status.state !== "done") { clearActive(); return; }
    let data;
    try { data = await api.get("/api/clips/all"); } catch { return; }
    const clip = (data.clips || []).find((c) => {
      const m = /^output\/([^/]+)\/clip_(\d+)$/.exec(c.key);
      return m && m[1] === saved.jobName && Number(m[2]) === saved.index;
    });
    if (!clip) { clearActive(); return; }
    state.pendingReattach = { run_id: saved.run_id, estimate: saved.estimate, status };
    await selectClip(clip);
    renderClipSlot();
  }

  // ------------------------------------------------------------------ init

  renderClipSlot();
  loadVoices().then(maybeReattach);
  refreshAvatarImages();

  return () => {
    stopWatch();
    stopTicker();
    if (dm) { dm.destroy(); dm = null; }
    document.querySelectorAll("dialog.dialog,dialog.dialog-preview")
      .forEach((d) => { try { d.close(); } catch { /* already closed */ } });
  };
}
