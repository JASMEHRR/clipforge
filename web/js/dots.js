/* Dot-matrix progress — ClipForge's signature element.
 *
 * Canvas-rendered grid of dots that fills left-to-right as work progresses.
 * Full variant: the pipeline progress board. Mini variant: static virality
 * fill on clip cards. One bold device; everything around it stays quiet.
 *
 *   const dm = createDotMatrix(canvas, { variant: "progress" });
 *   dm.update({ fraction: 0.4, state: "running" });   // idle|running|done|error
 *   dm.destroy();
 */

const COLORS = {
  idle: "#2a2a2c",
  lit: "#ededea",
  head: "#d71921",
  error: "#d71921",
};

const VARIANTS = {
  progress: { cols: 48, rows: 12, pitch: 10, dot: 6 },
  mini: { cols: 10, rows: 3, pitch: 7, dot: 4 },
};

export function createDotMatrix(canvas, opts = {}) {
  const v = { ...VARIANTS[opts.variant || "progress"], ...opts };
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = v.cols * v.pitch;
  const h = v.rows * v.pitch;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
  canvas.setAttribute("role", "img");
  ctx.scale(dpr, dpr);

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const total = v.cols * v.rows;
  let state = { fraction: 0, state: "idle" };
  let raf = 0;
  let settle = 0;          // done-settle animation progress 0..1
  let errorFlash = 0;      // frames remaining of the error flash

  function dotColor(i, t) {
    const col = i % v.cols;
    const row = (i / v.cols) | 0;
    // fill advances column-by-column so it reads as one sweep, not rows
    const idx = col * v.rows + row;
    const lit = idx < Math.round(state.fraction * total);
    if (state.state === "error") {
      return errorFlash > 0 && lit ? COLORS.error : lit ? COLORS.lit : COLORS.idle;
    }
    if (!lit) return COLORS.idle;
    if (state.state === "running") {
      const headCol = Math.min(v.cols - 1, Math.floor(state.fraction * v.cols));
      if (col === headCol || col === headCol - 1) {
        const p = 0.55 + 0.45 * Math.sin(t / 260 + row * 0.9);
        return blend(COLORS.head, COLORS.lit, 1 - p);
      }
      // subtle per-dot life in the filled region so it reads alive, not a bar
      if (!reduced) {
        const n = 0.85 + 0.15 * Math.sin(t / 900 + i * 2.399);
        return alpha(COLORS.lit, n);
      }
    }
    return COLORS.lit;
  }

  function draw(t) {
    ctx.clearRect(0, 0, w, h);
    const r = v.dot / 2;
    for (let i = 0; i < total; i++) {
      const col = i % v.cols;
      const row = (i / v.cols) | 0;
      let scale = 1;
      if (state.state === "done" && settle < 1 && !reduced) {
        // one settle wave sweeping across, then everything rests lit
        const d = Math.abs(col / v.cols - settle);
        scale = 1 + Math.max(0, 0.5 - d * 6);
      }
      ctx.fillStyle = dotColor(i, t);
      ctx.beginPath();
      ctx.arc(col * v.pitch + v.pitch / 2, row * v.pitch + v.pitch / 2,
              r * scale, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function frame(t) {
    if (state.state === "done" && settle < 1) settle = Math.min(1, settle + 0.02);
    if (errorFlash > 0) errorFlash--;
    draw(t);
    const animating = state.state === "running"
      || (state.state === "done" && settle < 1)
      || errorFlash > 0;
    raf = animating && !reduced ? requestAnimationFrame(frame) : 0;
  }

  function kick() {
    if (!raf) raf = requestAnimationFrame(frame);
  }

  return {
    update(next) {
      const was = state.state;
      state = { ...state, ...next };
      if (state.state === "done") state.fraction = 1;
      if (state.state === "done" && was !== "done") settle = 0;
      if (state.state === "error" && was !== "error") errorFlash = 36;
      canvas.setAttribute("aria-label",
        `Progress ${Math.round(state.fraction * 100)} percent`);
      draw(performance.now());
      kick();
    },
    destroy() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    },
  };
}

/* Mini variant for clip cards: static fill = score/100. */
export function miniScore(canvas, score) {
  const dm = createDotMatrix(canvas, { variant: "mini" });
  dm.update({ fraction: Math.max(0, Math.min(1, score / 100)), state: "idle" });
  canvas.setAttribute("aria-label", `Score ${score} out of 100`);
  return dm;
}

// -- tiny color helpers (hex in, rgba out) ----------------------------------
function hex(c) {
  return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16),
          parseInt(c.slice(5, 7), 16)];
}
function blend(a, b, t) {
  const [ar, ag, ab] = hex(a), [br, bg, bb] = hex(b);
  return `rgb(${Math.round(ar + (br - ar) * t)},${Math.round(ag + (bg - ag) * t)},${Math.round(ab + (bb - ab) * t)})`;
}
function alpha(c, a) {
  const [r, g, b] = hex(c);
  return `rgba(${r},${g},${b},${a})`;
}
