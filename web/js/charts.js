/* Two small inline-SVG chart builders for the Analytics tab. No dependency —
 * both charts share the same axis/gridline drawing, hence one file. Colors
 * come from tokens.css (--chart-1, --chart-grid); both charts are
 * single-series, so no multi-hue palette exists yet. */

const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function baseSvg(width, height) {
  return svgEl("svg", {
    class: "an-chart", viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none", role: "img",
  });
}

function drawGridlines(svg, { width, padding, innerH, lines = 3 }) {
  for (let i = 0; i <= lines; i++) {
    const gy = padding + (innerH / lines) * i;
    svg.append(svgEl("line", {
      x1: padding, x2: width - padding, y1: gy, y2: gy,
      stroke: "var(--chart-grid)", "stroke-width": 1,
    }));
  }
}

/* points: [{x_label, y}], drawn as an even-spaced line. */
export function sparklineSVG(points, { width = 480, height = 160, padding = 24 } = {}) {
  const svg = baseSvg(width, height);
  if (!points.length) return svg;

  const ys = points.map((p) => p.y);
  const yMin = Math.min(0, ...ys);
  const yMax = Math.max(...ys, 1);
  const innerW = width - padding * 2;
  const innerH = height - padding * 2;
  const sx = (i) => padding + (points.length === 1 ? innerW / 2
    : (i / (points.length - 1)) * innerW);
  const sy = (y) => padding + innerH - ((y - yMin) / (yMax - yMin || 1)) * innerH;

  drawGridlines(svg, { width, padding, innerH });

  const d = points.map((p, i) => `${i === 0 ? "M" : "L"} ${sx(i)} ${sy(p.y)}`).join(" ");
  svg.append(svgEl("path", {
    d, fill: "none", stroke: "var(--chart-1)", "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));
  points.forEach((p, i) => {
    svg.append(svgEl("circle", { cx: sx(i), cy: sy(p.y), r: 2.5, fill: "var(--chart-1)" }));
  });
  return svg;
}

/* bars: [{label, value}], drawn as a simple bar chart. */
export function barsSVG(bars, { width = 480, height = 160, padding = 24 } = {}) {
  const svg = baseSvg(width, height);
  if (!bars.length) return svg;

  const values = bars.map((b) => b.value);
  const vMax = Math.max(...values, 1);
  const innerW = width - padding * 2;
  const innerH = height - padding * 2;
  const gap = 4;
  const barW = Math.max(2, innerW / bars.length - gap);

  drawGridlines(svg, { width, padding, innerH });

  bars.forEach((b, i) => {
    const x = padding + i * (barW + gap);
    const h = (b.value / vMax) * innerH;
    svg.append(svgEl("rect", {
      x, y: padding + innerH - h, width: barW, height: Math.max(0, h),
      fill: "var(--chart-1)", rx: 2,
    }));
  });
  return svg;
}
