/* Shared DOM helpers used by every view and the pickers. */

export function el(tag, attrs = {}, ...children) {
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

export function toast(message, kind = "") {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = el("div", { class: `toast ${kind}`, role: "status" }, message);
  document.body.append(t);
  setTimeout(() => t.remove(), 5000);
}

export function fmtClock(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return "—";
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// one truthiness rule for the keep flag everywhere: absent = kept
export const isKept = (clip) => clip.kept !== false;

/* A labeled switch. Returns { node, input }. */
export function toggle(checked, label) {
  const input = el("input", { type: "checkbox" });
  input.checked = checked;
  return {
    input,
    node: el("label", { class: "opt-toggle" }, label,
      el("span", { class: "switch" }, input, el("span", { class: "knob" }))),
  };
}

/* A form field: label above control (plus optional inline extra). */
export function field(label, control, extra) {
  return el("div", { class: "field" },
    el("label", {}, label),
    extra ? el("div", { class: "field-inline" }, control, extra) : control);
}
