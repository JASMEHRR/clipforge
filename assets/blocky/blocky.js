// ClipForge blocky renderer: original toy-brick characters and sets drawn
// with three.js, one deterministic frame at a time.
//
// blocky.py (Python) opens scene.html in headless Chrome, calls
//   await BLOCKY.load(spec)      build every set, actor and texture once
//   BLOCKY.frame(t)              pose the world for time t (seconds), render,
//                                return the frame as a JPEG data URL
// The same t always gives the same picture (seeded random, no clocks), so a
// re-render is identical and frames can be drawn in any order.
import * as THREE from './vendor/three.module.js';

// ------------------------------------------------------------------ palette

const COLORS = {
  red: '#e53935', blue: '#1e88e5', green: '#43a047', yellow: '#fdd835',
  orange: '#fb8c00', purple: '#8e24aa', pink: '#f06292', white: '#f5f5f5',
  black: '#2b2f33', brown: '#7b4f32', grey: '#90a4ae', teal: '#00897b',
  navy: '#283593', gold: '#ffb300', lime: '#9ccc65', cyan: '#4dd0e1',
};
const SKINS = {
  yellow: '#f6cf3d', light: '#ffd9b8', tan: '#e3a86b', brown: '#a8693f', dark: '#6e4426',
};
const col = (name, fallback = 'grey') => COLORS[name] || COLORS[fallback];

// ------------------------------------------------------------------ helpers

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const clamp01 = (x) => Math.max(0, Math.min(1, x));
const lerp = (a, b, k) => a + (b - a) * k;
const easeInOut = (x) => { x = clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
const easeOut = (x) => 1 - Math.pow(1 - clamp01(x), 3);
const easeOutBack = (x) => { x = clamp01(x); const c = 1.70158; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
const TAU = Math.PI * 2;

const matCache = new Map();
function mat(color, opts = {}) {
  const key = color + JSON.stringify(opts);
  if (!matCache.has(key)) {
    matCache.set(key, new THREE.MeshStandardMaterial({
      color: new THREE.Color(color), roughness: 0.42, metalness: 0.0, ...opts,
    }));
  }
  return matCache.get(key);
}

const geoCache = new Map();
// Box with softly bevelled edges: the moulded-plastic look.
function rboxGeo(w, h, d, b = 0.07) {
  const key = `${w}|${h}|${d}|${b}`;
  if (geoCache.has(key)) return geoCache.get(key);
  b = Math.min(b, w / 4, h / 4, d / 4);
  const W = w - 2 * b, H = h - 2 * b;
  const s = new THREE.Shape();
  const r = Math.min(b, W / 2, H / 2);
  s.moveTo(-W / 2 + r, -H / 2);
  s.lineTo(W / 2 - r, -H / 2);
  s.quadraticCurveTo(W / 2, -H / 2, W / 2, -H / 2 + r);
  s.lineTo(W / 2, H / 2 - r);
  s.quadraticCurveTo(W / 2, H / 2, W / 2 - r, H / 2);
  s.lineTo(-W / 2 + r, H / 2);
  s.quadraticCurveTo(-W / 2, H / 2, -W / 2, H / 2 - r);
  s.lineTo(-W / 2, -H / 2 + r);
  s.quadraticCurveTo(-W / 2, -H / 2, -W / 2 + r, -H / 2);
  const g = new THREE.ExtrudeGeometry(s, {
    depth: Math.max(0.001, d - 2 * b), bevelEnabled: true, bevelThickness: b,
    bevelSize: b, bevelSegments: 2, curveSegments: 3,
  });
  g.center();
  geoCache.set(key, g);
  return g;
}
function boxGeo(w, h, d) {
  const key = `box|${w}|${h}|${d}`;
  if (!geoCache.has(key)) geoCache.set(key, new THREE.BoxGeometry(w, h, d));
  return geoCache.get(key);
}

function mesh(geo, material, x = 0, y = 0, z = 0, parent = null) {
  const m = new THREE.Mesh(geo, material);
  m.position.set(x, y, z);
  m.castShadow = true;
  m.receiveShadow = true;
  if (parent) parent.add(m);
  return m;
}
// Bevelled block in a named or hex colour.
function block(parent, w, h, d, color, x, y, z, opts) {
  return mesh(rboxGeo(w, h, d), mat(color, opts), x, y, z, parent);
}
// Plain block (big set pieces: cheaper, crisp edges).
function slab(parent, w, h, d, color, x, y, z, opts) {
  return mesh(boxGeo(w, h, d), mat(color, opts), x, y, z, parent);
}

// ------------------------------------------------------------------ textures

let MAX_ANISO = 1;
function canvasTex(w, h, draw, repeat = null) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  draw(ctx, w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = MAX_ANISO;
  if (repeat) {
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(repeat[0], repeat[1]);
  }
  return t;
}

function shade(hex, k) {
  const c = new THREE.Color(hex);
  const hsl = {};
  c.getHSL(hsl);
  c.setHSL(hsl.h, hsl.s, clamp01(hsl.l * k));
  return '#' + c.getHexString();
}

// A 2x2-stud tile of baseplate.
function studTex(base, size) {
  return canvasTex(128, 128, (g) => {
    g.fillStyle = base; g.fillRect(0, 0, 128, 128);
    for (const [cx, cy] of [[32, 32], [96, 32], [32, 96], [96, 96]]) {
      g.fillStyle = shade(base, 0.78);
      g.beginPath(); g.arc(cx + 3, cy + 4, 21, 0, TAU); g.fill();
      const gr = g.createRadialGradient(cx - 7, cy - 7, 2, cx, cy, 21);
      gr.addColorStop(0, shade(base, 1.22)); gr.addColorStop(1, base);
      g.fillStyle = gr;
      g.beginPath(); g.arc(cx, cy, 19, 0, TAU); g.fill();
    }
  }, [size / 2, size / 2]);
}

function brickTex(rw, rh) {
  return canvasTex(256, 256, (g) => {
    g.fillStyle = '#d8c7ae'; g.fillRect(0, 0, 256, 256);
    const rows = 8, bw = 64, bh = 32;
    for (let r = 0; r < rows; r++) {
      const off = (r % 2) * 32;
      for (let x = -64; x < 256 + 64; x += bw) {
        const tone = ['#b4432c', '#a93e29', '#bf4d33', '#9f3a26'][(r * 7 + x / 64) & 3];
        g.fillStyle = tone;
        g.fillRect(x + off + 2, r * bh + 2, bw - 4, bh - 4);
      }
    }
  }, [rw, rh]);
}

function plankTex(rw, rh) {
  return canvasTex(256, 256, (g) => {
    for (let r = 0; r < 8; r++) {
      g.fillStyle = ['#c08a55', '#b57f4b', '#c99560', '#ad7744'][r & 3];
      g.fillRect(0, r * 32, 256, 32);
      g.fillStyle = 'rgba(70,40,15,0.35)';
      g.fillRect(0, r * 32, 256, 2);
      g.fillRect(((r * 97) % 200) + 20, r * 32, 2, 32);
    }
  }, [rw, rh]);
}

function tileTex(rw, rh) {
  return canvasTex(128, 128, (g) => {
    g.fillStyle = '#b9c3c9'; g.fillRect(0, 0, 128, 128);
    for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) {
      g.fillStyle = (x + y) % 2 ? '#f4f6f7' : '#e6ecef';
      g.fillRect(x * 32 + 1, y * 32 + 1, 30, 30);
    }
  }, [rw, rh]);
}

function windowGridTex(base, cols, rows) {
  return canvasTex(256, 512, (g) => {
    g.fillStyle = base; g.fillRect(0, 0, 256, 512);
    const cw = 256 / cols, rh = 512 / rows;
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      g.fillStyle = ((r * 5 + c * 3) % 7 === 0) ? '#ffe9a3' : '#9fd3f5';
      g.fillRect(c * cw + cw * 0.18, r * rh + rh * 0.2, cw * 0.64, rh * 0.6);
    }
  });
}

function stripeTex(a, b) {
  return canvasTex(128, 32, (g) => {
    for (let i = 0; i < 8; i++) { g.fillStyle = i % 2 ? a : b; g.fillRect(i * 16, 0, 16, 32); }
  });
}

function fitText(g, text, maxW, maxH, family) {
  let px = maxH;
  for (; px > 10; px -= 2) {
    g.font = `${px}px ${family}`;
    if (g.measureText(text).width <= maxW) break;
  }
  return px;
}

function labelTex(text, w, h, bg, fg, border) {
  return canvasTex(w, h, (g) => {
    g.fillStyle = border || bg; g.fillRect(0, 0, w, h);
    const m = border ? Math.round(h * 0.07) : 0;
    g.fillStyle = bg; g.fillRect(m, m, w - 2 * m, h - 2 * m);
    g.fillStyle = fg;
    g.textAlign = 'center'; g.textBaseline = 'middle';
    const lines = String(text).toUpperCase().split('\n').slice(0, 3);
    const lh = (h - 2 * m) / lines.length;
    for (let i = 0; i < lines.length; i++) {
      fitText(g, lines[i], (w - 2 * m) * 0.9, lh * 0.78, 'Anton, Impact, sans-serif');
      g.fillText(lines[i], w / 2, m + lh * (i + 0.5) + lh * 0.04);
    }
  });
}

function skyTex(top, horizon) {
  const t = canvasTex(4, 512, (g) => {
    const gr = g.createLinearGradient(0, 0, 0, 512);
    gr.addColorStop(0, top); gr.addColorStop(0.62, horizon); gr.addColorStop(1, horizon);
    g.fillStyle = gr; g.fillRect(0, 0, 4, 512);
  });
  return t;
}

// ------------------------------------------------------------------ faces

const FACE_EXPRS = ['neutral', 'smile', 'grin', 'laugh', 'shock', 'sad', 'angry',
  'worried', 'smug', 'cry', 'love', 'cool', 'ko', 'talk'];
const CLOSED_EYES = new Set(['laugh', 'cry', 'ko', 'cool']);
const faceCache = new Map();

function faceTex(expr, blink, extras) {
  const key = `${expr}|${blink ? 1 : 0}|${extras.join(',')}`;
  if (faceCache.has(key)) return faceCache.get(key);
  const t = canvasTex(256, 256, (g) => drawFace(g, expr, blink, extras));
  faceCache.set(key, t);
  return t;
}

function drawFace(g, expr, blink, extras) {
  const ink = '#1d1d1f';
  g.lineCap = 'round'; g.lineJoin = 'round';
  g.strokeStyle = ink; g.fillStyle = ink;
  const EL = [88, 112], ER = [168, 112];
  const line = (pts, w = 10) => {
    g.lineWidth = w; g.beginPath(); g.moveTo(...pts[0]);
    for (const p of pts.slice(1)) g.lineTo(...p);
    g.stroke();
  };
  const eye = ([x, y], rx = 15, ry = 24, half = false) => {
    g.fillStyle = ink; g.beginPath();
    if (half) g.ellipse(x, y + 4, rx, ry * 0.7, 0, 0, Math.PI);
    else g.ellipse(x, y, rx, ry, 0, 0, TAU);
    g.fill();
    if (!half) {
      g.fillStyle = '#ffffff';
      g.beginPath(); g.arc(x - rx * 0.3, y - ry * 0.38, rx * 0.36, 0, TAU); g.fill();
    }
  };
  const closedEye = ([x, y], up = true) => {
    g.lineWidth = 9; g.beginPath();
    if (up) g.arc(x, y + 10, 16, Math.PI * 1.15, Math.PI * 1.85);
    else g.arc(x, y - 6, 16, Math.PI * 0.15, Math.PI * 0.85);
    g.stroke();
  };
  const heart = ([x, y], s) => {
    g.fillStyle = '#e8344e'; g.beginPath();
    g.moveTo(x, y + s * 0.9);
    g.bezierCurveTo(x - s * 1.4, y - s * 0.1, x - s * 0.7, y - s * 1.2, x, y - s * 0.4);
    g.bezierCurveTo(x + s * 0.7, y - s * 1.2, x + s * 1.4, y - s * 0.1, x, y + s * 0.9);
    g.fill();
  };
  const mouthArc = (cx, cy, r, a0, a1, w = 10) => {
    g.lineWidth = w; g.beginPath(); g.arc(cx, cy, r, a0, a1); g.stroke();
  };
  const openMouth = (cx, cy, rx, ry) => {
    g.fillStyle = ink; g.beginPath(); g.ellipse(cx, cy, rx, ry, 0, 0, TAU); g.fill();
    g.fillStyle = '#e25d6b'; g.beginPath(); g.ellipse(cx, cy + ry * 0.45, rx * 0.6, ry * 0.4, 0, 0, TAU); g.fill();
  };
  const dMouth = (cx, cy, w, h) => {
    g.fillStyle = ink; g.beginPath();
    g.moveTo(cx - w / 2, cy);
    g.quadraticCurveTo(cx, cy + h * 1.6, cx + w / 2, cy);
    g.closePath(); g.fill();
    g.fillStyle = '#ffffff'; g.fillRect(cx - w * 0.36, cy + 2, w * 0.72, h * 0.22);
    g.fillStyle = '#e25d6b'; g.beginPath(); g.ellipse(cx, cy + h * 0.62, w * 0.2, h * 0.16, 0, 0, TAU); g.fill();
  };

  // eyes
  const shut = blink && !CLOSED_EYES.has(expr);
  if (shut) { closedEye(EL, false); closedEye(ER, false); }
  else if (expr === 'laugh') { closedEye(EL); closedEye(ER); }
  else if (expr === 'cry') { closedEye(EL, false); closedEye(ER, false); }
  else if (expr === 'ko') {
    for (const [x, y] of [EL, ER]) { line([[x - 13, y - 13], [x + 13, y + 13]], 8); line([[x + 13, y - 13], [x - 13, y + 13]], 8); }
  } else if (expr === 'love') { heart(EL, 22); heart(ER, 22); }
  else if (expr === 'cool') {
    g.fillStyle = ink; g.fillRect(52, 92, 152, 12);
    g.beginPath(); g.roundRect(56, 96, 64, 40, 10); g.fill();
    g.beginPath(); g.roundRect(136, 96, 64, 40, 10); g.fill();
    g.fillStyle = 'rgba(255,255,255,0.5)'; g.fillRect(66, 104, 18, 6); g.fillRect(146, 104, 18, 6);
  } else if (expr === 'shock') { eye(EL, 19, 30); eye(ER, 19, 30); }
  else if (expr === 'smug') { eye(EL, 15, 24, true); eye(ER, 15, 24, true); }
  else { eye(EL); eye(ER); }

  // brows
  if (expr === 'angry') { line([[64, 70], [108, 86]], 10); line([[148, 86], [192, 70]], 10); }
  else if (expr === 'sad' || expr === 'worried' || expr === 'cry') { line([[68, 82], [106, 68]], 9); line([[150, 68], [188, 82]], 9); }
  else if (expr === 'shock') { line([[70, 62], [104, 54]], 9); line([[152, 54], [186, 62]], 9); }
  else if (expr === 'smug') { line([[70, 84], [106, 84]], 9); line([[150, 70], [186, 62]], 9); }

  // tears
  if (expr === 'cry') {
    g.fillStyle = 'rgba(80,190,245,0.92)';
    g.fillRect(74, 118, 16, 110); g.fillRect(166, 118, 16, 110);
  }

  // mouth
  switch (expr) {
    case 'smile': mouthArc(128, 150, 52, Math.PI * 0.18, Math.PI * 0.82, 11); break;
    case 'grin': case 'love': dMouth(128, 168, 104, 44); break;
    case 'laugh': dMouth(128, 160, 116, 56); break;
    case 'shock': openMouth(128, 196, 24, 32); break;
    case 'talk': openMouth(128, 186, 26, 18); break;
    case 'cry': openMouth(128, 192, 44, 26); break;
    case 'sad': mouthArc(128, 222, 42, Math.PI * 1.22, Math.PI * 1.78, 10); break;
    case 'angry': mouthArc(128, 214, 38, Math.PI * 1.25, Math.PI * 1.75, 10); break;
    case 'worried':
      g.lineWidth = 9; g.beginPath(); g.moveTo(92, 190);
      g.quadraticCurveTo(110, 176, 128, 190); g.quadraticCurveTo(146, 204, 164, 190); g.stroke();
      break;
    case 'smug':
      g.lineWidth = 10; g.beginPath(); g.moveTo(98, 186); g.quadraticCurveTo(140, 196, 168, 166); g.stroke();
      break;
    case 'cool': mouthArc(128, 150, 46, Math.PI * 0.22, Math.PI * 0.78, 10); break;
    case 'ko': openMouth(128, 192, 18, 12); break;
    default: mouthArc(128, 158, 38, Math.PI * 0.22, Math.PI * 0.78, 10);
  }

  // extras drawn on the face
  if (extras.includes('glasses') && expr !== 'cool') {
    g.strokeStyle = '#3a2a1a'; g.lineWidth = 6;
    g.beginPath(); g.arc(EL[0], EL[1], 30, 0, TAU); g.stroke();
    g.beginPath(); g.arc(ER[0], ER[1], 30, 0, TAU); g.stroke();
    line([[EL[0] + 30, EL[1]], [ER[0] - 30, ER[1]]], 6);
    g.strokeStyle = ink;
  }
  if (extras.includes('mustache')) {
    g.fillStyle = '#4a3020';
    g.beginPath(); g.ellipse(108, 150, 24, 10, -0.2, 0, TAU); g.fill();
    g.beginPath(); g.ellipse(148, 150, 24, 10, 0.2, 0, TAU); g.fill();
  }
  if (extras.includes('blush')) {
    g.fillStyle = 'rgba(240,110,130,0.45)';
    g.beginPath(); g.ellipse(58, 158, 20, 11, 0, 0, TAU); g.fill();
    g.beginPath(); g.ellipse(198, 158, 20, 11, 0, 0, TAU); g.fill();
  }
}

// ------------------------------------------------------------------ props

function makeProp(kind) {
  const g = new THREE.Group();
  switch (kind) {
    case 'phone':
      block(g, 0.55, 0.95, 0.12, '#222428', 0, 0, 0);
      slab(g, 0.46, 0.8, 0.02, '#58b8f0', 0, 0, 0.07, { emissive: new THREE.Color('#1c5f8a') });
      break;
    case 'pizza': {
      const m = mesh(new THREE.CylinderGeometry(0.75, 0.75, 0.12, 3), mat('#f2c14e'), 0, 0, 0, g);
      m.rotation.x = Math.PI / 2;
      for (const [x, y] of [[0.05, 0.15], [-0.15, -0.15], [0.2, -0.2]]) {
        const p = mesh(new THREE.CylinderGeometry(0.11, 0.11, 0.04, 10), mat('#c0392b'), x, y, 0.08, g);
        p.rotation.x = Math.PI / 2;
      }
      break;
    }
    case 'book':
      block(g, 0.8, 1.0, 0.24, '#1e88e5', 0, 0, 0);
      slab(g, 0.72, 0.92, 0.2, '#fafafa', 0.06, 0, 0);
      break;
    case 'money':
      for (let i = 0; i < 3; i++) block(g, 0.95, 0.45, 0.16, '#3cb371', 0, i * 0.18 - 0.18, 0);
      slab(g, 0.25, 0.47, 0.5, '#f5f5f5', 0, 0, 0);
      break;
    case 'gift':
      block(g, 0.9, 0.9, 0.9, '#e53935', 0, 0, 0);
      slab(g, 0.2, 0.92, 0.92, '#fdd835', 0, 0, 0);
      slab(g, 0.92, 0.92, 0.2, '#fdd835', 0, 0, 0);
      break;
    case 'ball':
      mesh(new THREE.SphereGeometry(0.48, 24, 16), mat('#ef5350'), 0, 0, 0, g);
      break;
    case 'trophy':
      mesh(new THREE.CylinderGeometry(0.42, 0.2, 0.6, 16), mat('#ffc107', { metalness: 0.6, roughness: 0.25 }), 0, 0.25, 0, g);
      block(g, 0.5, 0.2, 0.5, '#6d4c41', 0, -0.25, 0);
      break;
    case 'icecream': {
      const cone = mesh(new THREE.ConeGeometry(0.3, 0.8, 12), mat('#d9a25f'), 0, -0.15, 0, g);
      cone.rotation.x = Math.PI;
      mesh(new THREE.SphereGeometry(0.34, 16, 12), mat('#f48fb1'), 0, 0.38, 0, g);
      break;
    }
    case 'burger':
      mesh(new THREE.CylinderGeometry(0.45, 0.45, 0.22, 16), mat('#d39a4c'), 0, 0.22, 0, g);
      mesh(new THREE.CylinderGeometry(0.47, 0.47, 0.14, 16), mat('#5d3a1a'), 0, 0.04, 0, g);
      mesh(new THREE.CylinderGeometry(0.5, 0.5, 0.05, 16), mat('#7cb342'), 0, -0.05, 0, g);
      mesh(new THREE.CylinderGeometry(0.45, 0.45, 0.16, 16), mat('#d39a4c'), 0, -0.15, 0, g);
      break;
    default:
      return null;
  }
  return g;
}

// ------------------------------------------------------------------ actors

const SHAPE_WIDTH = { person: 2.6, house: 6.2, car: 3.4, phone: 2.4, fridge: 3.0, tree: 3.6, cake: 3.2, dog: 2.0, cat: 1.8 };

class Actor {
  constructor(c, index) {
    this.c = c;
    this.index = index;
    this.kind = c.kind === 'object' ? (c.shape || 'house') : 'person';
    if (!(this.kind in SHAPE_WIDTH)) this.kind = 'house';
    const size = { baby: 0.5, kid: 0.78, adult: 1.0, giant: 2.4 }[c.size] || 1.0;
    this.scale = this.kind === 'person' ? size : Math.min(size, 2.0);
    this.width = SHAPE_WIDTH[this.kind] * this.scale;
    this.extras = (c.extras || []).filter((e) => ['glasses', 'mustache', 'blush', 'tie'].includes(e));
    this.root = new THREE.Group();     // placement (x, z) + facing
    this.body = new THREE.Group();     // pose: hop, tilt, squash
    this.root.add(this.body);
    this.parts = {};
    this.pivots = {};
    this.faceMesh = null;
    this.headY = 4.6;
    this.height = 5.4;
    if (this.kind === 'person') this.buildPerson();
    else this.buildObject();
    this.body.scale.setScalar(this.scale);
    this.restPos = {};
    for (const [k, p] of Object.entries(this.pivots)) {
      this.restPos[k] = { pos: p.position.clone(), rot: p.rotation.clone() };
    }
  }

  pivot(name, x, y, z, parent = this.body) {
    const p = new THREE.Group();
    p.position.set(x, y, z);
    parent.add(p);
    this.pivots[name] = p;
    return p;
  }

  addFace(parent, w, h, x, y, z) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h),
      new THREE.MeshStandardMaterial({ map: faceTex('neutral', false, this.extras), transparent: true, roughness: 0.7, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2 }));
    m.position.set(x, y, z);
    m.renderOrder = 2;
    parent.add(m);
    this.faceMesh = m;
  }

  buildPerson() {
    const c = this.c;
    const shirt = col(c.shirt, 'blue'), pants = col(c.pants, 'navy');
    const skin = SKINS[c.skin] || SKINS.yellow;
    const hairC = col(c.hair_color, 'brown');
    const hatC = col(c.hat_color, 'red');
    // legs (pivot at hip)
    for (const [name, x] of [['legA', -0.5], ['legB', 0.5]]) {
      const p = this.pivot(name, x, 2.0, 0);
      block(p, 0.98, 2.0, 1.0, pants, 0, -1.0, 0);
      block(p, 1.0, 0.32, 1.06, shade(pants, 0.55), 0, -1.86, 0.02);
    }
    // torso
    const torso = block(this.body, 2.0, 2.0, 1.0, shirt, 0, 3.0, 0);
    this.parts.torso = torso;
    if (this.extras.includes('tie')) {
      slab(this.body, 0.32, 1.3, 0.06, '#c62828', 0, 3.25, 0.52);
      slab(this.body, 0.42, 0.26, 0.07, '#b71c1c', 0, 3.86, 0.52);
    }
    // arms (pivot at shoulder)
    for (const [name, x] of [['armA', -1.5], ['armB', 1.5]]) {
      const p = this.pivot(name, x, 3.82, 0);
      block(p, 0.96, 1.45, 0.96, shirt, 0, -0.6, 0);
      block(p, 0.92, 0.55, 0.92, skin, 0, -1.6, 0);
      this.parts[name] = p;
    }
    // head (pivot at neck)
    const head = this.pivot('head', 0, 4.0, 0);
    block(head, 1.32, 1.22, 1.22, skin, 0, 0.64, 0, { roughness: 0.5 });
    this.addFace(head, 1.18, 1.1, 0, 0.62, 0.612);
    // hair / hats
    const hat = new THREE.Group();
    head.add(hat);
    this.pivots.hat = hat;
    switch (c.hair) {
      case 'short':
        block(hat, 1.42, 0.34, 1.32, hairC, 0, 1.3, -0.02);
        block(hat, 1.42, 0.9, 0.3, hairC, 0, 0.95, -0.52);
        break;
      case 'long':
        block(hat, 1.44, 0.36, 1.34, hairC, 0, 1.3, -0.02);
        block(hat, 1.46, 1.75, 0.34, hairC, 0, 0.5, -0.5);
        block(hat, 0.2, 1.0, 1.1, hairC, -0.74, 0.85, -0.05);
        block(hat, 0.2, 1.0, 1.1, hairC, 0.74, 0.85, -0.05);
        break;
      case 'bun':
        block(hat, 1.42, 0.34, 1.32, hairC, 0, 1.3, -0.02);
        block(hat, 0.62, 0.55, 0.62, hairC, 0, 1.68, -0.25);
        break;
      case 'spiky':
        block(hat, 1.42, 0.3, 1.32, hairC, 0, 1.3, -0.02);
        for (const [x, z, r] of [[-0.4, 0.2, 0.3], [0.1, 0.25, -0.2], [0.45, -0.1, 0.35], [-0.2, -0.35, -0.3]]) {
          const s = block(hat, 0.42, 0.55, 0.42, hairC, x, 1.6, z);
          s.rotation.set(r, 0.6, r * 0.7);
        }
        break;
      case 'cap':
        block(hat, 1.46, 0.42, 1.38, hatC, 0, 1.34, -0.02);
        block(hat, 1.34, 0.12, 0.72, hatC, 0, 1.16, 0.86);
        break;
      case 'tophat':
        block(hat, 1.9, 0.14, 1.8, '#1f1f24', 0, 1.3, 0);
        block(hat, 1.22, 1.3, 1.22, '#1f1f24', 0, 2.0, 0);
        slab(hat, 1.24, 0.22, 1.24, hatC, 0, 1.5, 0);
        break;
      case 'crown':
        block(hat, 1.4, 0.3, 1.3, '#ffc107', 0, 1.36, 0, { metalness: 0.5, roughness: 0.3 });
        for (const x of [-0.5, 0, 0.5]) block(hat, 0.3, 0.38, 0.3, '#ffc107', x, 1.66, 0.48, { metalness: 0.5, roughness: 0.3 });
        break;
      case 'bow':
        block(hat, 1.42, 0.34, 1.32, hairC, 0, 1.3, -0.02);
        block(hat, 1.42, 1.0, 0.3, hairC, 0, 0.9, -0.52);
        block(hat, 0.5, 0.36, 0.3, '#ec407a', -0.32, 1.52, 0.2);
        block(hat, 0.5, 0.36, 0.3, '#ec407a', 0.32, 1.52, 0.2);
        break;
      default:
        break;
    }
    this.parts.hat = hat;
    this.hasHat = hat.children.length > 0;
    this.handY = -1.72;
    this.headY = 4.64;
    this.height = c.hair === 'tophat' ? 7.0 : 5.6;
  }

  buildObject() {
    const c = this.c;
    const main = col(c.shirt, 'red'), second = col(c.pants, 'brown');
    const legsFor = (x1, x2, y, len, color) => {
      for (const [name, x] of [['legA', x1], ['legB', x2]]) {
        const p = this.pivot(name, x, y, 0);
        block(p, 0.62, len, 0.62, color, 0, -len / 2, 0);
        block(p, 0.8, 0.26, 1.0, shade(color, 0.6), 0, -len + 0.13, 0.12);
      }
    };
    const armsFor = (x1, x2, y, len, color) => {
      for (const [name, x] of [['armA', x1], ['armB', x2]]) {
        const p = this.pivot(name, x, y, 0);
        block(p, 0.42, len, 0.42, color, 0, -len / 2, 0);
        block(p, 0.56, 0.42, 0.56, '#f5f5f5', 0, -len, 0);
        this.parts[name] = p;
      }
      this.handY = -len;
    };
    const B = this.pivot('head', 0, 0, 0);   // the whole object nods as a "head"
    switch (this.kind) {
      case 'house': {
        legsFor(-1.4, 1.4, 1.8, 1.8, '#8d6e63');
        block(B, 5.0, 4.2, 4.4, main, 0, 3.9, 0);
        for (let i = 0; i < 3; i++) block(B, 5.6 - i * 1.6, 0.75, 4.8, second, 0, 6.35 + i * 0.75, 0);
        block(B, 0.8, 1.4, 0.8, '#8d6e63', 1.5, 7.4, -1.0);
        block(B, 1.2, 1.9, 0.16, '#6d4c41', 0, 2.75, 2.22);
        slab(B, 0.16, 0.16, 0.12, '#ffd54f', 0.38, 2.75, 2.32);
        this.addFace(B, 3.6, 3.0, 0, 4.4, 2.215);
        armsFor(-2.7, 2.7, 4.2, 1.7, main);
        this.headY = 4.6; this.height = 8.2;
        break;
      }
      case 'car': {
        block(B, 2.8, 1.5, 5.0, main, 0, 1.35, 0);
        block(B, 2.5, 1.2, 2.6, main, 0, 2.6, -0.4);
        slab(B, 2.3, 0.9, 2.62, '#9fd3f5', 0, 2.65, -0.4);
        for (const [x, z] of [[-1.45, 1.6], [1.45, 1.6], [-1.45, -1.6], [1.45, -1.6]]) {
          const w = mesh(new THREE.CylinderGeometry(0.62, 0.62, 0.45, 20), mat('#222'), x, 0.62, z, B);
          w.rotation.z = Math.PI / 2;
        }
        this.addFace(B, 2.3, 1.3, 0, 1.45, 2.52);
        this.headY = 2.2; this.height = 3.4;
        break;
      }
      case 'phone': {
        legsFor(-0.5, 0.5, 1.4, 1.4, '#37474f');
        block(B, 2.0, 3.6, 0.42, '#263238', 0, 3.2, 0);
        slab(B, 1.76, 3.3, 0.05, '#4fc3f7', 0, 3.2, 0.2, { emissive: new THREE.Color('#0d4a6b') });
        this.addFace(B, 1.6, 1.6, 0, 3.6, 0.235);
        armsFor(-1.2, 1.2, 3.6, 1.4, '#37474f');
        this.headY = 3.6; this.height = 5.2;
        break;
      }
      case 'fridge': {
        legsFor(-0.8, 0.8, 1.3, 1.3, '#78909c');
        block(B, 2.6, 4.8, 2.3, '#eceff1', 0, 3.7, 0);
        slab(B, 2.5, 0.06, 0.04, '#b0bec5', 0, 4.6, 1.16);
        slab(B, 0.16, 0.9, 0.2, '#90a4ae', 1.0, 5.4, 1.24);
        this.addFace(B, 2.1, 1.9, 0, 3.4, 1.16);
        armsFor(-1.5, 1.5, 4.6, 1.6, '#cfd8dc');
        this.headY = 4.0; this.height = 6.3;
        break;
      }
      case 'tree': {
        legsFor(-0.6, 0.6, 1.2, 1.2, '#6d4c41');
        block(B, 1.3, 3.0, 1.3, '#795548', 0, 2.7, 0);
        block(B, 3.6, 2.6, 3.4, main === COLORS.red ? '#43a047' : main, 0, 5.3, 0);
        block(B, 2.4, 1.6, 2.4, main === COLORS.red ? '#4caf50' : shade(main, 1.15), 0, 7.2, 0);
        this.addFace(B, 2.6, 2.0, 0, 5.2, 1.715);
        armsFor(-0.85, 0.85, 3.6, 1.2, '#6d4c41');
        this.headY = 5.2; this.height = 8.0;
        break;
      }
      case 'cake': {
        legsFor(-0.8, 0.8, 1.1, 1.1, '#8d6e63');
        mesh(new THREE.CylinderGeometry(1.6, 1.6, 1.5, 32), mat('#f8bbd0'), 0, 1.85, 0, B);
        mesh(new THREE.CylinderGeometry(1.15, 1.15, 1.1, 32), mat('#fff8e1'), 0, 3.15, 0, B);
        mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.7, 8), mat('#42a5f5'), 0, 4.05, 0, B);
        mesh(new THREE.SphereGeometry(0.16, 10, 8), mat('#ffca28', { emissive: new THREE.Color('#ff8f00') }), 0, 4.5, 0, B);
        this.addFace(B, 1.7, 1.3, 0, 1.95, 1.62);
        this.headY = 2.6; this.height = 4.8;
        break;
      }
      case 'dog':
      case 'cat': {
        const cat = this.kind === 'cat';
        const fur = main, spot = second;
        for (const [name, x, z] of [['legA', -0.45, 0.8], ['legB', 0.45, 0.8], ['legC', -0.45, -0.8], ['legD', 0.45, -0.8]]) {
          const p = this.pivot(name, x, 1.1, z);
          block(p, 0.5, 1.1, 0.5, fur, 0, -0.55, 0);
        }
        block(B, 1.5, 1.2, 2.4, fur, 0, 1.6, 0);
        const head = this.pivot('face', 0, 2.5, 1.2, B);
        block(head, 1.4, 1.2, 1.2, fur, 0, 0, 0);
        if (cat) {
          for (const x of [-0.45, 0.45]) { const e = block(head, 0.36, 0.5, 0.2, spot, x, 0.75, 0); e.rotation.z = x > 0 ? -0.3 : 0.3; }
        } else {
          for (const x of [-0.72, 0.72]) block(head, 0.22, 0.9, 0.6, spot, x, 0.1, -0.1);
          block(head, 0.7, 0.45, 0.4, shade(fur, 1.1), 0, -0.25, 0.7);
          block(head, 0.26, 0.2, 0.12, '#222', 0, -0.08, 0.92);
        }
        this.addFace(head, 1.2, 1.0, 0, 0.05, cat ? 0.612 : 0.612);
        const tail = this.pivot('tail', 0, 2.0, -1.2, B);
        block(tail, 0.3, 0.3, 1.0, spot, 0, 0.2, -0.45).rotation.x = cat ? -0.9 : -0.5;
        this.headY = 2.5; this.height = 3.4;
        break;
      }
      default:
        break;
    }
  }

  setFace(expr, blink) {
    if (!this.faceMesh) return;
    const t = faceTex(FACE_EXPRS.includes(expr) ? expr : 'neutral', blink, this.extras);
    if (this.faceMesh.material.map !== t) this.faceMesh.material.map = t;
  }

  resetPose() {
    this.body.position.set(0, 0, 0);
    this.body.rotation.set(0, 0, 0);
    this.body.scale.setScalar(this.scale);
    for (const [k, p] of Object.entries(this.pivots)) {
      p.position.copy(this.restPos[k].pos);
      p.rotation.copy(this.restPos[k].rot);
      p.visible = true;
    }
  }
}

// ------------------------------------------------------------------ poses

// One shot's action for one actor at u seconds into a shot of `dur` seconds.
function poseActor(a, act, u, dur, seed) {
  a.resetPose();
  const P = a.pivots, b = a.body, s = a.scale;
  const swing = (amp, hz, ph = 0) => amp * Math.sin(TAU * hz * u + ph);
  const legs = (amp, hz) => {
    if (P.legA) P.legA.rotation.x = swing(amp, hz);
    if (P.legB) P.legB.rotation.x = -swing(amp, hz);
    if (P.legC) P.legC.rotation.x = -swing(amp, hz);
    if (P.legD) P.legD.rotation.x = swing(amp, hz);
  };
  const arms = (amp, hz) => {
    if (P.armA) P.armA.rotation.x = -swing(amp, hz);
    if (P.armB) P.armB.rotation.x = swing(amp, hz);
  };
  const breathe = () => { b.position.y = Math.abs(swing(0.03, 0.8, seed)) * s; if (P.head) P.head.rotation.y = swing(0.12, 0.23, seed); };
  const armsUp = (k, flare = 2.6) => {
    if (P.armA) P.armA.rotation.z = -flare * k;
    if (P.armB) P.armB.rotation.z = flare * k;
  };
  const p = clamp01(u / Math.max(0.01, dur));
  let move = { dx: 0, dz: 0, ry: 0 };
  switch (act.action) {
    case 'walk':
      legs(0.62, 1.6); arms(0.55, 1.6);
      b.position.y = Math.abs(swing(0.12, 3.2)) * s;
      move.dz = 3.2 * p;
      break;
    case 'run':
      legs(0.95, 2.6); arms(0.95, 2.6);
      b.position.y = Math.abs(swing(0.28, 5.2)) * s;
      b.rotation.x = 0.16;
      move.dz = 7.0 * p;
      break;
    case 'sneak':
      legs(0.35, 0.9); if (P.armA) P.armA.rotation.x = -1.2; if (P.armB) P.armB.rotation.x = -1.2;
      b.position.y = -0.25 * s; b.rotation.x = 0.22;
      move.dx = -2.6 + 5.2 * p; move.ry = Math.sin(TAU * 0.5 * u) * 0.5;
      break;
    case 'jump': {
      const h = Math.abs(Math.sin(Math.PI * 1.6 * u));
      b.position.y = 1.6 * h * s;
      armsUp(0.25 + 0.6 * h, 2.5);
      if (P.legA) { P.legA.rotation.x = -0.4 * h; P.legB.rotation.x = 0.3 * h; }
      break;
    }
    case 'celebrate': {
      const h = Math.abs(Math.sin(Math.PI * 1.8 * u));
      b.position.y = 1.2 * h * s;
      armsUp(1, 2.7 + 0.2 * Math.sin(TAU * 3 * u));
      b.rotation.y = 0.3 * Math.sin(TAU * 0.9 * u);
      break;
    }
    case 'wave':
      breathe();
      if (P.armB) { P.armB.rotation.z = 2.6 + 0.4 * Math.sin(TAU * 2.6 * u); }
      if (P.head) P.head.rotation.z = -0.08;
      break;
    case 'dance':
      b.position.y = Math.abs(swing(0.35, 2.2)) * s;
      b.rotation.y = swing(0.5, 0.55);
      if (P.armA) { P.armA.rotation.z = -1.4 - swing(0.9, 1.1); }
      if (P.armB) { P.armB.rotation.z = 1.4 - swing(0.9, 1.1); }
      legs(0.35, 2.2);
      if (P.head) P.head.rotation.z = swing(0.18, 1.1);
      break;
    case 'shock': {
      const k = easeOutBack(u / 0.35);
      armsUp(clamp01(k), 2.3);
      b.position.y = Math.max(0, Math.sin(Math.PI * clamp01(u / 0.4))) * 0.7 * s;
      move.dz = -0.7 * easeOut(u / 0.4);
      b.scale.setScalar(s * (1 + 0.12 * Math.max(0, 1 - u / 0.3)));
      if (P.head) P.head.rotation.x = -0.15 * clamp01(k);
      break;
    }
    case 'cry':
      if (P.head) P.head.rotation.x = 0.3;
      b.position.x = swing(0.05, 7) * s;
      if (P.armA) { P.armA.rotation.x = -2.2; P.armA.rotation.z = 0.5; }
      if (P.armB) { P.armB.rotation.x = -2.2; P.armB.rotation.z = -0.5; }
      break;
    case 'fall': {
      const k = easeOut(u / 0.45);
      b.rotation.z = (Math.PI / 2) * k;
      b.position.y = 1.0 * k * s + Math.abs(Math.sin(Math.PI * clamp01((u - 0.45) / 0.25))) * 0.25 * s * (u > 0.45 ? 1 : 0);
      armsUp(0.4 * k, 2.4);
      break;
    }
    case 'spin':
      b.rotation.y = TAU * 1.5 * easeInOut(p);
      armsUp(0.55, 2.0);
      b.position.y = Math.sin(Math.PI * p) * 0.6 * s;
      break;
    case 'point':
      breathe();
      if (P.armB) P.armB.rotation.x = -Math.PI / 2 * easeOut(u / 0.3);
      if (P.head) P.head.rotation.x = swing(0.08, 2.0);
      break;
    case 'sit':
      b.position.y = -1.0 * s;
      if (P.legA) { P.legA.rotation.x = -Math.PI / 2; P.legB.rotation.x = -Math.PI / 2; }
      if (P.armA) { P.armA.rotation.x = -0.7; P.armB.rotation.x = -0.7; }
      if (P.head) P.head.rotation.y = swing(0.15, 0.3, seed);
      break;
    case 'shrink':
      b.scale.setScalar(s * lerp(1, 0.28, easeInOut(u / Math.min(1.2, dur))));
      armsUp(0.3, 2.0);
      break;
    case 'grow':
      b.scale.setScalar(s * lerp(1, 2.3, easeOutBack(u / Math.min(1.2, dur))));
      armsUp(0.5, 2.0);
      break;
    case 'float':
      b.position.y = (1.4 + 0.35 * Math.sin(TAU * 0.6 * u)) * s * easeOut(u / 0.8);
      armsUp(0.55, 2.0);
      if (P.legA) { P.legA.rotation.x = 0.25; P.legB.rotation.x = -0.15; }
      break;
    case 'talk':
      breathe();
      if (P.armB) P.armB.rotation.x = -0.6 - swing(0.25, 1.3);
      if (P.armB) P.armB.rotation.z = 0.25;
      break;
    case 'lose_part':
      breathe();
      b.position.x = swing(0.03, 9) * s * (u > (act.lose_t ?? 0.4 * dur) ? 1 : 0);
      break;
    default: // idle
      breathe();
      if (P.armA) P.armA.rotation.z = -0.06 - swing(0.04, 0.8, seed);
      if (P.armB) P.armB.rotation.z = 0.06 + swing(0.04, 0.8, seed);
      break;
  }
  if (P.tail) P.tail.rotation.y = Math.sin(TAU * 2.2 * u) * 0.6;
  return move;
}

// A part that pops off flies on an arc and lands on the ground beside the actor.
const PART_ORDER = ['armB', 'armA', 'hat', 'legB'];
function applyMissing(a, act, u) {
  for (const name of act.missing || []) if (a.pivots[name]) a.pivots[name].visible = false;
  const name = act.lose;
  if (!name || !a.pivots[name]) return;
  const t0 = act.lose_t ?? 0;
  if (u < t0) return;
  const k = clamp01((u - t0) / 0.62);
  const p = a.pivots[name], rest = a.restPos[name];
  const side = rest.pos.x >= 0 ? 1 : -1;
  // the hat hangs off the head pivot: land it on the ground in that pivot's space
  const groundY = 0.35 - (p.parent === a.body ? 0 : p.parent.position.y);
  p.position.set(
    rest.pos.x + side * 2.4 * k,
    lerp(rest.pos.y, groundY, k) + 3.2 * k * (1 - k) * 2,
    rest.pos.z + 0.9 * k,
  );
  // pose of the body hop does not apply to a flying part: undo it
  p.position.y -= a.body.position.y / a.scale;
  p.rotation.set(rest.rot.x + 5.5 * k, 0, rest.rot.z + side * (Math.PI / 2) * easeOut(k) + 2.0 * k * (1 - k));
}

// ------------------------------------------------------------------ sets

function addTree(g, x, z, s = 1, leaf = '#43a047') {
  block(g, 0.9 * s, 2.6 * s, 0.9 * s, '#795548', x, 1.3 * s, z);
  block(g, 3.4 * s, 2.6 * s, 3.4 * s, leaf, x, 3.7 * s, z);
  block(g, 2.2 * s, 1.5 * s, 2.2 * s, shade(leaf, 1.15), x, 5.6 * s, z);
}
function addHouse(g, x, z, wall, roof, w = 7, h = 5.5, d = 6, rot = 0) {
  const hg = new THREE.Group();
  hg.position.set(x, 0, z); hg.rotation.y = rot;
  g.add(hg);
  slab(hg, w, h, d, wall, 0, h / 2, 0);
  for (let i = 0; i < 3; i++) slab(hg, w + 0.8 - i * 2.2, 0.9, d + 0.6, roof, 0, h + 0.45 + i * 0.9, 0);
  slab(hg, 1.5, 2.6, 0.2, '#6d4c41', 0, 1.3, d / 2 + 0.05);
  for (const wx of [-w / 3.2, w / 3.2]) {
    slab(hg, 1.7, 1.5, 0.16, '#f5f5f5', wx, h * 0.62, d / 2 + 0.04);
    slab(hg, 1.4, 1.2, 0.2, '#8fd0f5', wx, h * 0.62, d / 2 + 0.05);
  }
  return hg;
}
function addFence(g, x0, x1, z, color = '#f5f5f5') {
  for (let x = x0; x <= x1; x += 1.2) slab(g, 0.28, 1.3, 0.28, color, x, 0.65, z);
  slab(g, x1 - x0, 0.22, 0.16, color, (x0 + x1) / 2, 0.95, z);
  slab(g, x1 - x0, 0.22, 0.16, color, (x0 + x1) / 2, 0.45, z);
}
function addLamp(g, x, z) {
  slab(g, 0.25, 5.5, 0.25, '#546e7a', x, 2.75, z);
  slab(g, 0.8, 0.5, 0.8, '#fff59d', x, 5.6, z, { emissive: new THREE.Color('#5c5200') });
}
function addSignBoard(g, text, x, y, z, w, h, bg = '#d32f2f', fg = '#ffffff', border = '#fdd835') {
  const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshStandardMaterial({ map: labelTex(text, Math.round(256 * w / h), 256, bg, fg, border), roughness: 0.6 }));
  m.position.set(x, y, z);
  g.add(m);
  return m;
}
function ground(g, color, size = 260) {
  const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshStandardMaterial({ map: studTex(color, size), roughness: 0.55 }));
  m.rotation.x = -Math.PI / 2;
  m.receiveShadow = true;
  g.add(m);
}
function road(g, z, w = 7, along = 'x') {
  const r = slab(g, along === 'x' ? 260 : w, 0.06, along === 'x' ? w : 260, '#4b5257', 0, 0.03, z);
  r.castShadow = false;
  for (let x = -120; x < 120; x += 6) {
    const d = slab(g, along === 'x' ? 2.6 : 0.3, 0.07, along === 'x' ? 0.3 : 2.6, '#fdd835', along === 'x' ? x : 0, 0.04, along === 'x' ? z : x);
    d.castShadow = false;
  }
}

const SETS = {
  street(g, rnd) {
    ground(g, '#5fb760');
    slab(g, 260, 0.1, 6, '#c9c9c9', 0, 0.05, -1);
    road(g, 6);
    const walls = ['#ffe082', '#90caf9', '#f48fb1', '#a5d6a7', '#fff3e0', '#ce93d8'];
    const roofs = ['#c62828', '#1565c0', '#6d4c41', '#2e7d32', '#455a64'];
    for (let i = -4; i <= 4; i++) {
      addHouse(g, i * 11, -12, walls[(i + 8) % walls.length], roofs[(i + 9) % roofs.length], 7.5, 5.5, 6);
      addTree(g, i * 11 + 5.5, -8.5, 0.8 + rnd() * 0.3);
      addFence(g, i * 11 - 4, i * 11 + 4, -5);
    }
    for (let i = -3; i <= 3; i++) addLamp(g, i * 14 + 7, -3.5);
    for (let i = -4; i <= 4; i++) addHouse(g, i * 12 + 4, 38, walls[(i + 10) % walls.length], roofs[(i + 7) % roofs.length], 8, 5, 6, Math.PI);
  },
  house(g, rnd) {
    ground(g, '#5fb760');
    addHouse(g, 0, -12, '#ffe082', '#c62828', 12, 8, 9);
    slab(g, 3, 0.12, 8, '#bdbdbd', 0, 0.06, -4);
    addFence(g, -16, -4, -3.5);
    addFence(g, 4, 16, -3.5);
    addTree(g, -11, -9, 1.1); addTree(g, 11, -9, 1.0);
    slab(g, 0.3, 1.5, 0.3, '#6d4c41', 6, 0.75, 1);
    block(g, 0.9, 0.7, 1.3, '#1e88e5', 6, 1.8, 1);
    for (let i = 0; i < 9; i++) block(g, 0.5, 0.5, 0.5, ['#f06292', '#fdd835', '#ab47bc'][i % 3], -8 + i * 0.7, 0.25, -6 + (i % 2) * 0.6);
    addHouse(g, -24, -12, '#90caf9', '#1565c0', 8, 5.5, 6);
    addHouse(g, 24, -12, '#a5d6a7', '#6d4c41', 8, 5.5, 6);
  },
  school(g, rnd) {
    ground(g, '#5fb760');
    const W = 30, H = 10, D = 9;
    const bm = new THREE.MeshStandardMaterial({ map: brickTex(W / 4, H / 4), roughness: 0.8 });
    const b = mesh(new THREE.BoxGeometry(W, H, D), bm, 0, H / 2, -15, g);
    slab(g, W + 1, 0.8, D + 1, '#eeeeee', 0, H + 0.4, -15);
    for (let row = 0; row < 2; row++) for (let i = -3; i <= 3; i++) {
      if (row === 0 && i === 0) continue;
      slab(g, 2.4, 2.2, 0.2, '#f5f5f5', i * 4, 3 + row * 4, -10.45);
      slab(g, 2.0, 1.8, 0.24, '#8fd0f5', i * 4, 3 + row * 4, -10.4);
    }
    slab(g, 3.6, 3.6, 0.3, '#5d4037', 0, 1.8, -10.4);
    for (let i = 0; i < 3; i++) slab(g, 7 - i, 0.3, 1.2, '#bdbdbd', 0, 0.15 + i * 0.3, -9.6 + i * -0.5 + 1.4);
    addSignBoard(g, 'SCHOOL', 0, 8.4, -10.3, 9, 1.8, '#1565c0', '#ffffff', '#fdd835');
    slab(g, 0.25, 12, 0.25, '#b0bec5', -12, 6, -6);
    const flag = slab(g, 3, 1.8, 0.08, '#e53935', -10.4, 11, -6);
    flag.userData.flag = true;
    slab(g, 260, 0.08, 7, '#c9c9c9', 0, 0.04, -4);
    // school bus
    const bus = new THREE.Group(); bus.position.set(14, 0, -6); bus.rotation.y = -0.3; g.add(bus);
    block(bus, 9, 3.2, 3, '#fbc02d', 0, 2.2, 0);
    for (let i = 0; i < 4; i++) slab(bus, 1.4, 1.1, 3.05, '#8fd0f5', -3 + i * 2, 2.8, 0);
    for (const x of [-3, 3]) for (const z of [-1.5, 1.5]) { const w = mesh(new THREE.CylinderGeometry(0.7, 0.7, 0.4, 18), mat('#222'), x, 0.7, z, bus); w.rotation.x = Math.PI / 2; }
    addTree(g, -20, -8, 1.1); addTree(g, 22, -12, 1.0);
  },
  park(g, rnd) {
    ground(g, '#62bd62');
    for (let i = 0; i < 14; i++) {
      const a = (i / 14) * TAU, r = 14 + rnd() * 10;
      addTree(g, Math.cos(a) * r, Math.sin(a) * r - 6, 0.8 + rnd() * 0.5, ['#43a047', '#66bb6a', '#2e7d32'][i % 3]);
    }
    const pond = slab(g, 10, 0.12, 6, '#42a5f5', -9, 0.06, -9, { roughness: 0.15 });
    pond.castShadow = false;
    const bench = new THREE.Group(); bench.position.set(6, 0, -5); g.add(bench);
    slab(bench, 4, 0.3, 1.2, '#8d6e63', 0, 1.1, 0);
    slab(bench, 4, 1.0, 0.25, '#8d6e63', 0, 1.8, -0.5);
    for (const x of [-1.7, 1.7]) slab(bench, 0.3, 1.1, 1.0, '#455a64', x, 0.55, 0);
    for (let i = 0; i < 30; i++) {
      const x = (rnd() - 0.5) * 36, z = -3 - rnd() * 14;
      if (Math.abs(x + 9) < 6 && Math.abs(z + 9) < 4) continue;
      block(g, 0.4, 0.4, 0.4, ['#f06292', '#fdd835', '#ab47bc', '#ff7043'][i % 4], x, 0.2, z);
    }
    slab(g, 3, 0.1, 30, '#d7ccc8', 0, 0.05, 0);
  },
  shop(g, rnd) {
    ground(g, '#9e9e9e');
    slab(g, 260, 0.1, 8, '#cfcfcf', 0, 0.05, -2);
    road(g, 7);
    const fronts = [['#ffcc80', 'SHOP'], ['#80cbc4', 'CAFE'], ['#f48fb1', 'TOYS'], ['#b39ddb', 'PIZZA'], ['#a5d6a7', 'MARKET']];
    for (let i = -2; i <= 2; i++) {
      const [c, name] = fronts[(i + 5) % fronts.length];
      const x = i * 10;
      slab(g, 9.6, 7, 6, c, x, 3.5, -10);
      slab(g, 7, 3.6, 0.2, '#8fd0f5', x, 2.4, -6.95);
      slab(g, 1.8, 3.2, 0.25, '#6d4c41', x + 3.4, 1.6, -6.9);
      const aw = new THREE.Mesh(new THREE.BoxGeometry(9, 0.25, 2.2), new THREE.MeshStandardMaterial({ map: stripeTex(['#e53935', '#1e88e5', '#43a047', '#fb8c00', '#8e24aa'][(i + 5) % 5], '#ffffff'), roughness: 0.6 }));
      aw.position.set(x, 4.6, -5.9); aw.rotation.x = 0.3; aw.castShadow = true; g.add(aw);
      addSignBoard(g, name, x, 6.0, -6.88, 6, 1.4, '#ffffff', '#263238', '#263238');
      for (let k = 0; k < 3; k++) {
        block(g, 1.1, 0.8, 1.1, '#a1887f', x - 3 + k * 1.3, 0.4, -5.2);
        for (let q = 0; q < 3; q++) mesh(new THREE.SphereGeometry(0.22, 10, 8), mat(['#e53935', '#fb8c00', '#fdd835'][k]), x - 3.3 + k * 1.3 + q * 0.3, 0.95, -5.2 + (q - 1) * 0.25, g);
      }
    }
  },
  city(g, rnd) {
    ground(g, '#8d8d8d');
    slab(g, 260, 0.1, 8, '#bdbdbd', 0, 0.05, -2);
    road(g, 7);
    const bases = ['#90a4ae', '#b0bec5', '#78909c', '#bcaaa4', '#9fa8da'];
    const texs = bases.map((c) => windowGridTex(c, 4, 10));
    for (let i = -6; i <= 6; i++) {
      const h = 14 + Math.floor(rnd() * 22), w = 8 + Math.floor(rnd() * 3);
      const m = new THREE.MeshStandardMaterial({ map: texs[(i + 12) % texs.length], roughness: 0.5 });
      mesh(new THREE.BoxGeometry(w, h, 8), m, i * 11, h / 2, -14 - rnd() * 6, g);
    }
    for (let i = -6; i <= 6; i++) {
      const h = 18 + Math.floor(rnd() * 20);
      const m = new THREE.MeshStandardMaterial({ map: texs[(i + 13) % texs.length], roughness: 0.5 });
      mesh(new THREE.BoxGeometry(9, h, 8), m, i * 11 + 4, h / 2, 40, g);
    }
    for (let i = -3; i <= 3; i++) addLamp(g, i * 12 + 6, -4.5);
    // a parked car
    const car = new THREE.Group(); car.position.set(-8, 0, 5.5); car.rotation.y = Math.PI / 2; g.add(car);
    block(car, 2.6, 1.3, 4.6, '#e53935', 0, 1.2, 0);
    block(car, 2.3, 1.1, 2.4, '#e53935', 0, 2.3, -0.3);
    slab(car, 2.1, 0.8, 2.42, '#9fd3f5', 0, 2.35, -0.3);
    for (const [x, z] of [[-1.3, 1.5], [1.3, 1.5], [-1.3, -1.5], [1.3, -1.5]]) { const w = mesh(new THREE.CylinderGeometry(0.55, 0.55, 0.4, 18), mat('#222'), x, 0.55, z, car); w.rotation.z = Math.PI / 2; }
  },
  room(g, rnd) {
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ map: plankTex(6, 6), roughness: 0.6 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; g.add(floor);
    slab(g, 40, 32, 0.5, '#ffe0b2', 0, 16, -8, { roughness: 0.9 });
    slab(g, 0.5, 32, 30, '#ffccbc', -13, 16, 5, { roughness: 0.9 });
    slab(g, 0.5, 32, 30, '#ffccbc', 13, 16, 5, { roughness: 0.9 });
    slab(g, 40, 0.6, 0.6, '#ffffff', 0, 0.3, -7.6);
    // window with sky
    slab(g, 6.4, 4.4, 0.3, '#ffffff', -5, 6.5, -7.7);
    slab(g, 5.8, 3.8, 0.34, '#8fd0f5', -5, 6.5, -7.68, { emissive: new THREE.Color('#2f6f99') });
    slab(g, 0.2, 3.8, 0.4, '#ffffff', -5, 6.5, -7.6);
    // sofa
    const sofa = new THREE.Group(); sofa.position.set(5.5, 0, -6); g.add(sofa);
    block(sofa, 7, 1.4, 2.6, '#5c6bc0', 0, 0.9, 0);
    block(sofa, 7, 2.2, 0.8, '#5c6bc0', 0, 2.2, -1.0);
    for (const x of [-3.4, 3.4]) block(sofa, 0.8, 1.9, 2.6, '#3f51b5', x, 1.2, 0);
    for (const x of [-1.6, 1.6]) block(sofa, 2.8, 0.5, 2.0, '#7986cb', x, 1.85, 0.1);
    // rug, lamp, picture
    const rug = slab(g, 10, 0.05, 6, '#ef9a9a', 0, 0.03, 0.5); rug.castShadow = false;
    slab(g, 0.25, 5, 0.25, '#455a64', 10.5, 2.5, -6);
    block(g, 1.6, 1.4, 1.6, '#fff59d', 10.5, 5.4, -6, { emissive: new THREE.Color('#665c00') });
    slab(g, 3, 2.2, 0.2, '#6d4c41', 6, 7.2, -7.7);
    slab(g, 2.5, 1.7, 0.24, '#81c784', 6, 7.2, -7.68);
  },
  classroom(g, rnd) {
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ map: plankTex(6, 6), roughness: 0.6 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; g.add(floor);
    slab(g, 40, 32, 0.5, '#e8f5e9', 0, 16, -8, { roughness: 0.9 });
    slab(g, 0.5, 32, 30, '#dcedc8', -13, 16, 5, { roughness: 0.9 });
    slab(g, 0.5, 32, 30, '#dcedc8', 13, 16, 5, { roughness: 0.9 });
    slab(g, 13, 5.4, 0.3, '#8d6e63', 0, 6.4, -7.7);
    const board = new THREE.Mesh(new THREE.PlaneGeometry(12.2, 4.8), new THREE.MeshStandardMaterial({
      map: canvasTex(512, 200, (c) => {
        c.fillStyle = '#2e5d3a'; c.fillRect(0, 0, 512, 200);
        c.fillStyle = 'rgba(255,255,255,0.88)'; c.font = '52px Anton, Impact, sans-serif'; c.textAlign = 'center';
        c.fillText('2 + 2 = ?', 256, 92);
        c.font = '30px Anton, Impact, sans-serif'; c.fillText('HOMEWORK DUE FRIDAY', 256, 160);
      }), roughness: 0.9,
    }));
    board.position.set(0, 6.4, -7.5); g.add(board);
    for (const x of [-9, 9]) for (const z of [-2, 3]) {
      const d = new THREE.Group(); d.position.set(x, 0, z); g.add(d);
      block(d, 3.2, 0.3, 2, '#ffb74d', 0, 2.2, 0);
      for (const lx of [-1.4, 1.4]) slab(d, 0.2, 2.1, 0.2, '#455a64', lx, 1.05, 0);
    }
    const clock = mesh(new THREE.CylinderGeometry(0.9, 0.9, 0.2, 24), mat('#ffffff'), 9, 9.5, -7.6, g);
    clock.rotation.x = Math.PI / 2;
    slab(g, 0.1, 0.7, 0.05, '#222', 9, 9.75, -7.45);
  },
  kitchen(g, rnd) {
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ map: tileTex(14, 14), roughness: 0.4 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; g.add(floor);
    const wall = new THREE.MeshStandardMaterial({ map: tileTex(10, 4), roughness: 0.5 });
    mesh(new THREE.BoxGeometry(40, 32, 0.5), wall, 0, 16, -8, g);
    slab(g, 0.5, 32, 30, '#fff3e0', -13, 16, 5);
    slab(g, 0.5, 32, 30, '#fff3e0', 13, 16, 5);
    for (let i = -4; i <= 4; i++) {
      block(g, 2.4, 3, 2.4, '#90caf9', i * 2.5, 1.5, -6.6);
      slab(g, 2.5, 0.25, 2.6, '#eceff1', i * 2.5, 3.1, -6.6);
      slab(g, 0.5, 0.12, 0.1, '#546e7a', i * 2.5, 2.4, -5.35);
    }
    for (const x of [-2.5, 0]) { const b = mesh(new THREE.CylinderGeometry(0.45, 0.45, 0.06, 18), mat('#37474f'), x, 3.25, -6.6, g); }
    const pot = mesh(new THREE.CylinderGeometry(0.6, 0.55, 0.8, 18), mat('#b0bec5', { metalness: 0.5, roughness: 0.3 }), 0, 3.65, -6.6, g);
    const table = new THREE.Group(); table.position.set(7, 0, -1.5); g.add(table);
    block(table, 5, 0.3, 3, '#a1887f', 0, 2.5, 0);
    for (const [x, z] of [[-2.2, -1.2], [2.2, -1.2], [-2.2, 1.2], [2.2, 1.2]]) slab(table, 0.3, 2.4, 0.3, '#795548', x, 1.2, z);
    for (const x of [-1.2, 1.2]) mesh(new THREE.CylinderGeometry(0.6, 0.5, 0.08, 20), mat('#ffffff'), x, 2.7, 0, table);
  },
};
const PLACES = Object.keys(SETS);

// Townspeople in the background: the place looks lived in and the main cast
// has normal-sized people to be compared with ("town dead" was the top fault
// of weak videos). Built into the set group, so they show only with it.
const EXTRA_SPOTS = {
  street: { n: 6, x: [-15, 15], z: [-4.5, -2.6], walk: true },
  house: { n: 3, x: [-13, 13], z: [-5, -3], walk: true },
  school: { n: 7, x: [-14, 14], z: [-8, -3.5], walk: true },
  park: { n: 6, x: [-14, 14], z: [-9, -3], walk: true },
  shop: { n: 6, x: [-15, 15], z: [-5, -2.8], walk: true },
  city: { n: 7, x: [-15, 15], z: [-5, -2.8], walk: true },
  classroom: { n: 5, x: [-11, 11], z: [-5, -3], walk: false },
  kitchen: { n: 0 }, room: { n: 0 },
};
const EXTRA_HAIR = ['short', 'long', 'bun', 'spiky', 'cap', 'none', 'bow'];
const EXTRA_COLORS = Object.keys(COLORS);
const EXTRA_SKINS = Object.keys(SKINS);
function addExtras(g, place, rnd) {
  const spot = EXTRA_SPOTS[place];
  const out = [];
  if (!spot || !spot.n) return out;
  for (let i = 0; i < spot.n; i++) {
    const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
    const a = new Actor({ kind: 'person', shape: 'person', shirt: pick(EXTRA_COLORS), pants: pick(EXTRA_COLORS),
      skin: pick(EXTRA_SKINS), hair: pick(EXTRA_HAIR), hair_color: pick(['brown', 'black', 'yellow', 'orange', 'grey']),
      hat_color: pick(EXTRA_COLORS), size: rnd() < 0.5 ? 'kid' : 'adult', extras: [] }, -1);
    let x = lerp(spot.x[0], spot.x[1], (i + 0.2 + 0.6 * rnd()) / spot.n);
    if (Math.abs(x) < 4.5) x = (x < 0 ? -1 : 1) * (4.5 + rnd() * 2);
    const z = lerp(spot.z[0], spot.z[1], rnd());
    a.root.position.set(x, 0, z);
    g.add(a.root);
    out.push({ a, x0: x, z, walk: spot.walk && rnd() < 0.6, dir: rnd() < 0.5 ? -1 : 1,
      speed: 0.9 + rnd() * 0.7, phase: rnd() * 10, face: pick(['smile', 'neutral', 'grin', 'smile']),
      action: spot.walk ? pick(['idle', 'idle', 'wave', 'talk']) : 'sit' });
  }
  return out;
}
function poseExtras(list, t) {
  for (const e of list) {
    const u = t + e.phase;
    if (e.walk) {
      // stroll back and forth on a 6-unit beat, turning at each end
      const span = 6, cyc = (u * e.speed) % (2 * span);
      const fwd = cyc < span;
      poseActor(e.a, { action: 'walk' }, u, 1e9, e.phase);
      e.a.body.position.z = 0;
      e.a.root.position.set(e.x0 + e.dir * (fwd ? cyc : 2 * span - cyc) - e.dir * span / 2, 0, e.z);
      e.a.root.rotation.y = (fwd ? 1 : -1) * e.dir * Math.PI / 2;
    } else {
      poseActor(e.a, { action: e.action }, u, 1e9, e.phase);
    }
    e.a.setFace(e.face, (u % 3.4) < 0.13);
  }
}

// ------------------------------------------------------------------ cameras

const CAMERAS = ['player', 'wide', 'push_in', 'close_up', 'orbit', 'low_angle', 'top_down', 'pan', 'shake', 'whip', 'reveal'];

function cameraFor(kind, u, dur, T, a, yaw0, sizeK) {
  // T = focus point at the actor's feet, a = actor (null = group), sizeK = framing scale
  const p = clamp01(u / Math.max(0.01, dur));
  // aim at the actor's CURRENT size (shrink/grow) but keep the distance set by
  // its normal size, so a shrinking actor gets smaller in frame, not lost
  const cur = a ? a.body.scale.x : 1;
  const H = a ? a.height * cur : 5.5;
  const headY = a ? a.headY * cur : 4.4;
  const H0 = a ? a.height * a.scale : 5.5;
  const at = (yaw, dist, h) => new THREE.Vector3(T.x + Math.sin(yaw) * dist, T.y + h, T.z + Math.cos(yaw) * dist);
  const look = (y, dx = 0) => new THREE.Vector3(T.x + dx, T.y + y, T.z);
  const k = sizeK;
  switch (kind) {
    case 'player': {
      // raised three-quarter "game camera": the place and everyone in it
      const e = easeInOut(p), kk = Math.min(k, 2.2);
      const side = yaw0 >= 0 ? 1 : -1;
      return [at(yaw0 + side * (0.22 + 0.08 * e), lerp(12.5, 11.3, e) * kk, 6.4 * Math.max(1, kk * 0.8)), look(H0 * 0.42)];
    }
    case 'wide': {
      const d = lerp(12.5, 10.5, easeInOut(p)) * Math.min(k, 2.2);
      return [at(yaw0, d, 5.5 * k), look(H * 0.5)];
    }
    case 'push_in': {
      const e = easeInOut(p);
      return [at(yaw0, lerp(11, 5.8, e) * k, lerp(3.8, 4.2, e) * Math.max(1, k * 0.8)), look(lerp(H * 0.5, H * 0.66, e))];
    }
    case 'close_up': {
      const d = lerp(8.2, 7.2, easeInOut(p)) * Math.max(0.8, H0 / 5.6);
      return [at(yaw0 + 0.12 * Math.sin(p * 2), d, headY + 0.2), look(headY - 0.25)];
    }
    case 'orbit': {
      const y = yaw0 - 0.6 + 1.2 * easeInOut(p);
      return [at(y, 8.5 * k, 3.6 * Math.max(1, k * 0.8)), look(H * 0.55)];
    }
    case 'low_angle': {
      const d = lerp(7, 6, easeInOut(p)) * k;
      return [at(yaw0, d, 0.7), look(headY + 0.4)];
    }
    case 'top_down': {
      const e = easeInOut(p / 0.75);
      const from = new THREE.Vector3(T.x, T.y + 26, T.z + 15);
      const to = at(yaw0, 11 * Math.min(k, 2.2), 6 * Math.min(k, 1.6));
      return [from.lerp(to, e), look(lerp(0, H * 0.5, e))];
    }
    case 'pan': {
      const dx = lerp(-1.8, 1.8, easeInOut(p)) * k;
      const pos = at(yaw0, 9.5 * k, 3.8 * Math.max(1, k * 0.8));
      pos.x += dx;
      return [pos, look(H * 0.52, dx * 0.75)];
    }
    case 'whip': {
      const e = easeOut(u / 0.32);
      return [at(yaw0 + 1.3 * (1 - e), lerp(7.5, 7, p) * k, 3.8), look(H * 0.55)];
    }
    case 'reveal': {
      const e = easeInOut((u - 0.25) / Math.max(0.5, dur - 0.5));
      return [at(yaw0, lerp(5.4, 12, e) * Math.max(1, Math.min(k, 2.2) * lerp(0.3, 1, e)), lerp(headY + 0.2, 6.5, e)), look(lerp(headY - 0.3, H * 0.5, e))];
    }
    case 'shake':
    default: {
      return [at(yaw0, 7.5 * k, 3.8 * Math.max(1, k * 0.8)), look(H * 0.56)];
    }
  }
}

// ------------------------------------------------------------------ engine

const STATE = { extras: {}, spec: null, renderer: null, scene: null, camera: null, sets: {}, actors: [], clouds: [], sun: null, warnings: [], current: -1, signs: [] };

function layout(actors) {
  // focus actor at the front, the rest staggered behind on alternating sides
  const out = [];
  let right = 0, left = 0;
  actors.forEach((a, i) => {
    if (i === 0) { out.push([0, 0]); right = a.width / 2; left = a.width / 2; return; }
    const side = i % 2 === 1 ? 1 : -1;
    const z = -1.2 - 0.8 * Math.floor((i - 1) / 2);
    const gap = 0.9;
    if (side > 0) { out.push([right + a.width / 2 + gap, z]); right += a.width + gap; }
    else { out.push([-(left + a.width / 2 + gap), z]); left += a.width + gap; }
  });
  return out;
}

function enterShot(i) {
  const S = STATE, shot = S.spec.shots[i];
  for (const [name, g] of Object.entries(S.sets)) g.visible = name === shot.place;
  for (const a of S.actors) a.root.visible = false;
  for (const s of S.signs) if (s) s.visible = false;
  const list = shot.actors.map((x) => S.actors[x.cast]);
  const pos = layout(list);
  shot._pos = pos;
  list.forEach((a, k) => {
    a.root.visible = true;
    a.root.position.set(pos[k][0], 0, pos[k][1]);
    a.root.rotation.set(0, k === 0 ? 0 : -Math.sign(pos[k][0]) * 0.35, 0);
  });
  if (shot.sign) {
    const sg = S.signs[i];
    if (sg) {
      sg.visible = true;
      const x = list.length ? (Math.max(...pos.map((p, k) => p[0] + list[k].width / 2)) + 2.6) : 0;
      sg.position.set(x, 0, -1.0);
      sg.rotation.y = -0.25;
    }
  }
  S.current = i;
}

function shotAt(t) {
  const shots = STATE.spec.shots;
  for (let i = 0; i < shots.length; i++) if (t < shots[i].end) return i;
  return shots.length - 1;
}

function makeSign(text) {
  const g = new THREE.Group();
  slab(g, 0.35, 3.0, 0.35, '#6d4c41', -1.6, 1.5, 0);
  slab(g, 0.35, 3.0, 0.35, '#6d4c41', 1.6, 1.5, 0);
  block(g, 4.6, 2.8, 0.3, '#fdd835', 0, 4.1, 0);
  const face = new THREE.Mesh(new THREE.PlaneGeometry(4.3, 2.5), new THREE.MeshStandardMaterial({ map: labelTex(text, 440, 256, '#d32f2f', '#ffffff', null), roughness: 0.6 }));
  face.position.set(0, 4.1, 0.16);
  g.add(face);
  g.userData.width = 4.6; g.userData.height = 5.5;
  return g;
}

async function load(spec) {
  STATE.spec = spec;
  STATE.warnings = [];
  const w = spec.w, h = spec.h;
  try {
    const f = new FontFace('Anton', 'url(fonts/Anton.ttf)');
    await f.load();
    document.fonts.add(f);
  } catch (e) {
    STATE.warnings.push('Anton font failed to load: ' + e);
  }
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(1);
  renderer.setSize(w, h);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  document.body.appendChild(renderer.domElement);
  MAX_ANISO = renderer.capabilities.getMaxAnisotropy();
  STATE.renderer = renderer;

  const scene = new THREE.Scene();
  scene.background = skyTex('#3d9be9', '#cfeaff');
  scene.fog = new THREE.Fog('#cfeaff', 70, 190);
  STATE.scene = scene;
  STATE.camera = new THREE.PerspectiveCamera(52, w / h, 0.1, 600);

  scene.add(new THREE.HemisphereLight('#dff1ff', '#8a7a5a', 1.5));
  const sun = new THREE.DirectionalLight('#fff3dc', 2.6);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  const sc = sun.shadow.camera;
  sc.left = -26; sc.right = 26; sc.top = 26; sc.bottom = -26; sc.near = 1; sc.far = 120;
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun); scene.add(sun.target);
  STATE.sun = sun;

  const rnd = mulberry32(spec.seed || 1);
  const used = new Set(spec.shots.map((s) => s.place));
  for (const name of used) {
    if (!SETS[name]) { STATE.warnings.push(`unknown place ${name}`); continue; }
    const g = new THREE.Group();
    SETS[name](g, rnd);
    STATE.extras[name] = addExtras(g, name, rnd);
    g.visible = false;
    scene.add(g);
    STATE.sets[name] = g;
  }
  // blocky clouds
  for (let i = 0; i < 14; i++) {
    const c = new THREE.Group();
    const n = 3 + Math.floor(rnd() * 3);
    for (let k = 0; k < n; k++) slab(c, 4 + rnd() * 5, 2 + rnd() * 1.5, 4 + rnd() * 3, '#ffffff', k * 3.2 - n * 1.6, rnd() * 1.2, rnd() * 2, { roughness: 1, emissive: new THREE.Color('#5a6670') }).castShadow = false;
    c.position.set((rnd() - 0.5) * 220, 38 + rnd() * 18, -60 - rnd() * 80);
    c.userData.x0 = c.position.x;
    c.userData.speed = 0.4 + rnd() * 0.6;
    scene.add(c);
    STATE.clouds.push(c);
  }
  STATE.actors = spec.cast.map((c, i) => {
    const a = new Actor(c, i);
    a.root.visible = false;
    scene.add(a.root);
    return a;
  });
  // Lost parts stay lost: walk the shots in order and decide which part each
  // lose_part pops off (next one the actor still has) and what is already gone.
  const lost = spec.cast.map(() => []);
  spec.shots.forEach((shot) => shot.actors.forEach((act) => {
    const a = STATE.actors[act.cast];
    act.missing = [...lost[act.cast]];
    act.lose = null;
    if (act.action !== 'lose_part') return;
    const part = PART_ORDER.find((p) => a.pivots[p] && !lost[act.cast].includes(p) && (p !== 'hat' || a.hasHat));
    if (part) { act.lose = part; lost[act.cast].push(part); }
    if (act.lose_t == null) act.lose_t = 0.4 * (shot.end - shot.start);
  }));
  // held props live on each actor's armB pivot (one per actor per prop kind)
  STATE.props = new Map();
  spec.shots.forEach((shot) => shot.actors.forEach((act) => {
    if (!act.prop || act.prop === 'none') return;
    const a = STATE.actors[act.cast];
    const key = `${act.cast}|${act.prop}`;
    if (STATE.props.has(key) || !a.pivots.armB) return;
    const p = makeProp(act.prop);
    if (!p) { STATE.warnings.push(`unknown prop ${act.prop}`); return; }
    p.position.set(0, a.handY - 0.35, 0.55);
    p.visible = false;
    a.pivots.armB.add(p);
    STATE.props.set(key, p);
  }));
  STATE.signs = spec.shots.map((s) => {
    if (!s.sign) return null;
    const g = makeSign(s.sign);
    g.visible = false;
    scene.add(g);
    return g;
  });
  STATE.current = -1;
  return { ok: true, warnings: STATE.warnings, places: PLACES, cameras: CAMERAS, faces: FACE_EXPRS };
}

function frame(t) {
  const S = STATE, spec = S.spec;
  const i = shotAt(t);
  if (i !== S.current) enterShot(i);
  const shot = spec.shots[i];
  const u = t - shot.start, dur = shot.end - shot.start;
  const list = shot.actors.map((x) => S.actors[x.cast]);

  for (const [key, p] of S.props) p.visible = false;
  let lastPop = -1;
  shot.actors.forEach((act, k) => {
    const a = list[k];
    const seed = (act.cast + 1) * 1.37;
    const mv = poseActor(a, act, u, dur, seed);
    a.root.position.x = shot._pos[k][0] + mv.dx;
    a.root.position.z = shot._pos[k][1] + mv.dz;
    a.root.rotation.y = (k === 0 ? 0 : -Math.sign(shot._pos[k][0]) * 0.35) + mv.ry;
    applyMissing(a, act, u);
    // faces: blink every ~3 s; shock for a beat when a part pops off
    let face = act.face || 'neutral';
    if (act.lose && act.lose_t != null && u >= act.lose_t && u < act.lose_t + 0.7) face = 'shock';
    if (act.action === 'talk' && Math.floor(u / 0.12) % 2 === 0) face = 'talk';
    const ph = (u + act.cast * 0.9 + shot.start * 0.37) % 3.1;
    a.setFace(face, ph < 0.13);
    if (act.prop && act.prop !== 'none') {
      const p = S.props.get(`${act.cast}|${act.prop}`);
      if (p && a.pivots.armB && a.pivots.armB.visible) p.visible = true;
    }
    if (act.lose && act.lose_t != null && u >= act.lose_t) lastPop = Math.max(lastPop, act.lose_t);
  });
  for (const c of S.clouds) c.position.x = c.userData.x0 + t * c.userData.speed;
  poseExtras(S.extras[shot.place] || [], t);

  // camera
  let focusA = null, T;
  if (shot.focus === 'sign' && S.signs[i]) {
    T = S.signs[i].position.clone();
  } else if (typeof shot.focus === 'number' && shot.focus >= 0 && shot.focus < list.length) {
    focusA = list[shot.focus];
    T = focusA.root.position.clone();
  } else if (list.length) {
    const xs = list.map((a) => a.root.position.x);
    T = new THREE.Vector3((Math.min(...xs) + Math.max(...xs)) / 2, 0, Math.max(...list.map((a) => a.root.position.z)) - 0.5);
    focusA = list.reduce((m, a) => (a.height * a.scale > m.height * m.scale ? a : m), list[0]);
  } else {
    T = new THREE.Vector3(0, 0, 0);
  }
  let sizeK = 1;
  if (focusA) sizeK = Math.max(1, (focusA.height * focusA.scale) / 5.6);
  // a sign in the shot is part of the joke: frame the actor and the sign together
  const groupShot = !(typeof shot.focus === 'number' && shot.focus >= 0) && shot.focus !== 'sign';
  if (S.signs[i] && groupShot && list.length) {
    const sx = S.signs[i].position.x;
    const lo = Math.min(...list.map((a) => a.root.position.x - a.width / 2));
    const hi = Math.max(sx + 2.3, ...list.map((a) => a.root.position.x + a.width / 2));
    T = new THREE.Vector3((lo + hi) / 2, 0, T.z - 0.3);
    sizeK = Math.max(sizeK, (hi - lo) / 4.0);
  } else if (S.signs[i] && focusA && shot.focus !== 'sign') {
    const sx = S.signs[i].position.x;
    const span = Math.abs(sx - T.x) + focusA.width / 2 + 2.3;
    T = new THREE.Vector3((T.x - focusA.width / 2 + sx + 2.3) / 2, 0, T.z - 0.3);
    sizeK = Math.max(sizeK, span / 4.0);
  }
  if (shot.focus === 'group' || shot.focus == null || shot.focus === -1) {
    const span = list.length ? Math.max(...list.map((a) => a.root.position.x + a.width / 2)) - Math.min(...list.map((a) => a.root.position.x - a.width / 2)) : 3;
    sizeK = Math.max(sizeK, span / 4.2);
  }
  const yaw0 = shot.yaw || 0;
  const [pos, look] = cameraFor(shot.camera, u, dur, T, focusA, yaw0, sizeK);
  // impacts: shake + punch-in after a pop and on 'shake' shots
  let shake = 0, fov = shot.camera === 'close_up' ? 40 : 58;
  if (shot.camera === 'shake') shake = 0.32 * Math.exp(-2.5 * u);
  for (const tp of [...(shot.punch || []), ...(lastPop >= 0 ? [lastPop] : [])]) {
    const d = u - tp;
    if (d >= 0 && d < 0.45) { shake = Math.max(shake, 0.22 * (1 - d / 0.45)); fov -= 7 * Math.sin(Math.PI * Math.min(1, d / 0.45)); }
  }
  if (shake > 0) {
    const n = (x) => Math.sin(x * 47.3) * 0.6 + Math.sin(x * 91.7) * 0.4;
    pos.x += shake * n(t); pos.y += shake * n(t + 3.1); look.x += shake * 0.5 * n(t + 7.7);
  }
  const cam = S.camera;
  cam.fov = fov;
  cam.position.copy(pos);
  cam.lookAt(look);
  cam.updateProjectionMatrix();

  S.sun.position.set(T.x - 14, 28, T.z + 18);
  S.sun.target.position.set(T.x, 0, T.z);
  S.sun.target.updateMatrixWorld();

  S.renderer.render(S.scene, cam);
  return S.renderer.domElement.toDataURL('image/jpeg', spec.jpeg_quality || 0.92);
}

window.BLOCKY = { load, frame, PLACES, CAMERAS, FACE_EXPRS, PART_ORDER };
window.BLOCKY_READY = true;
