"use strict";

// Graphs: the last few minutes of every clock, console and the link, drawn on
// canvases with no library, so they work offline. Times use the page's units.
// Relies on app.js for el(), replace(), UNITS and the chosen unit.

const CHARTS = [
  { id: "offset", title: "Clock offset from the leader", kind: "time", signed: true },
  { id: "phc", title: "PHC offset from system clock", kind: "time", signed: true },
  { id: "latency", title: "Receive latency (worst flow)", kind: "time" },
  { id: "late", title: "Late packets", kind: "rate" },
  { id: "console", title: "Console clocks", kind: "ppm", signed: true },
  { id: "trim", title: "Trim", kind: "ppm", fit: true },
  { id: "rtt", title: "Link round trip", kind: "time" },
  { id: "jitter", title: "Link jitter", kind: "time" },
  { id: "loss", title: "Link packet loss", kind: "percent" },
];
const WINDOWS = [[30, "30 s"], [60, "1 min"], [120, "2 min"], [300, "5 min"], [600, "10 min"]];
const SERIES_COLOURS = ["#4c78a8", "#2a9d8f", "#9c6ade", "#e07a5f", "#56b4e9", "#8d99ae", "#b07aa1", "#59a14f"];

let graphWindow = 300;
try { graphWindow = Number(localStorage.getItem("virgil-graph-window")) || 300; } catch { /* private browsing */ }
let graphData = null;
const hover = {};  // chart id: seconds before now under the pointer

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function formatValue(kind, value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  if (kind === "time") return `${value < 0 ? "-" : ""}${magnitude(value)}`;
  if (kind === "ppm") return `${value >= 0 ? "+" : ""}${value.toFixed(3)} ppm`;
  if (kind === "percent") return `${value.toFixed(1)}%`;
  return `${value.toFixed(value < 10 ? 1 : 0)}/s`;
}

function scaled(kind, value) {
  // Values on the axis, in the chosen time unit for times.
  return kind === "time" ? value / UNITS[unit].scale : value;
}

function niceStep(span) {
  const raw = span / 4;
  const power = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / power;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * power;
}

function agoLabel(seconds) {
  const s = Math.round(-seconds);
  if (s === 0) return "now";
  return s >= 60 ? `-${Math.floor(s / 60)}m${s % 60 ? `${s % 60}s` : ""}` : `-${s}s`;
}

function drawChart(canvas, chart, series) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const g = canvas.getContext("2d");
  g.scale(ratio, ratio);
  g.clearRect(0, 0, width, height);
  const left = 64, right = 10, top = 8, bottom = 22;
  const plotW = width - left - right, plotH = height - top - bottom;

  let low = Infinity, high = -Infinity;
  for (const s of series) for (const [, v] of s.points) {
    const y = scaled(chart.kind, v);
    low = Math.min(low, y); high = Math.max(high, y);
  }
  if (!Number.isFinite(low)) return;
  if (chart.signed) { const m = Math.max(Math.abs(low), Math.abs(high)) || 1; low = -m; high = m; }
  else if (chart.fit) { const pad = Math.max((high - low) * 0.2, 0.05); low -= pad; high += pad; }
  else { low = Math.min(0, low); if (high === low) high = low + 1; }
  const step = niceStep(high - low);
  low = Math.floor(low / step) * step;
  high = Math.ceil(high / step) * step;
  const x = (t) => left + ((t + graphWindow) / graphWindow) * plotW;
  const y = (v) => top + (1 - (v - low) / (high - low)) * plotH;

  g.font = "11px system-ui, sans-serif";
  g.fillStyle = cssVar("--muted");
  g.strokeStyle = cssVar("--line");
  g.lineWidth = 1;
  g.textAlign = "right";
  g.textBaseline = "middle";
  for (let v = low; v <= high + step / 2; v += step) {
    g.beginPath(); g.moveTo(left, y(v)); g.lineTo(left + plotW, y(v)); g.stroke();
    const digits = chart.kind === "time" ? Math.max(0, -Math.floor(Math.log10(step))) : (chart.kind === "ppm" ? 2 : 1);
    const label = new Intl.NumberFormat("en", { maximumFractionDigits: Math.min(digits, 6) }).format(v);
    g.fillText(label, left - 6, y(v));
  }
  g.textAlign = "center";
  g.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const t = -graphWindow + (graphWindow * i) / 4;
    g.fillText(agoLabel(t), x(t), top + plotH + 6);
  }
  g.save();
  g.beginPath(); g.rect(left, top, plotW, plotH); g.clip();
  series.forEach((s) => {
    g.strokeStyle = s.colour;
    g.lineWidth = s.setting ? 1.2 : 1.6;
    g.setLineDash(s.setting ? [5, 4] : []);
    g.beginPath();
    s.points.forEach(([t, v], i) => (i ? g.lineTo(x(t), y(scaled(chart.kind, v))) : g.moveTo(x(t), y(scaled(chart.kind, v)))));
    g.stroke();
  });
  g.restore();
  g.setLineDash([]);
  if (hover[chart.id] !== undefined) {
    g.strokeStyle = cssVar("--muted");
    g.beginPath(); g.moveTo(x(hover[chart.id]), top); g.lineTo(x(hover[chart.id]), top + plotH); g.stroke();
  }
  canvas.dataset.left = left; canvas.dataset.plotW = plotW;
}

function valueAt(points, t) {
  let best = null;
  for (const point of points) if (best === null || Math.abs(point[0] - t) < Math.abs(best[0] - t)) best = point;
  return best && Math.abs(best[0] - t) <= graphWindow / 50 ? best[1] : null;
}

function unitLabel(kind) {
  return { time: UNITS[unit].label, ppm: "ppm", percent: "%", rate: "per second" }[kind];
}

function renderGraphs() {
  const host = document.getElementById("graphs");
  if (!graphData) return replace("graphs", el("p", { class: "hint" }, "Loading…"));
  const byChart = {};
  graphData.series.forEach((s) => (byChart[s.chart] = byChart[s.chart] || []).push(s));
  // Each series keeps its colour by name across charts and refreshes.
  const names = [...new Set(graphData.series.map((s) => s.label.replace(/ setting$/, "")))].sort();
  const colourOf = (label) => SERIES_COLOURS[names.indexOf(label.replace(/ setting$/, "")) % SERIES_COLOURS.length];

  const controls = el("div", { class: "row graph-windows", role: "group", "aria-label": "Graph window" },
    WINDOWS.map(([seconds, label]) => el("button", {
      class: seconds === graphWindow ? "primary" : "", "aria-pressed": seconds === graphWindow ? "true" : "false",
      onclick: () => {
        graphWindow = seconds;
        try { localStorage.setItem("virgil-graph-window", String(seconds)); } catch { /* private browsing */ }
        loadGraphs();
      },
    }, label)));
  const shown = CHARTS.filter((c) => byChart[c.id]);
  if (!shown.length) {
    return replace("graphs", controls, el("p", { class: "hint" }, "Nothing to graph yet: figures appear within a few seconds."));
  }
  const cards = shown.map((chart) => {
    const series = byChart[chart.id].map((s) => ({ ...s, colour: colourOf(s.label), setting: s.key.startsWith("limit/") }));
    const canvas = el("canvas", { class: "graph", role: "img", "aria-label": chart.title });
    const legend = el("ul", { class: "legend" });
    const fillLegend = () => {
      const at = hover[chart.id];
      const when = at === undefined ? [] : [el("li", { class: "legend-when" }, `at ${agoLabel(at)}`)];
      legend.replaceChildren(...when, ...series.map((s) => {
        const value = at === undefined ? (s.points.length ? s.points[s.points.length - 1][1] : null) : valueAt(s.points, at);
        return el("li", {}, el("span", { class: `swatch${s.setting ? " dashed" : ""}`, style: `--swatch:${s.colour}` }),
          `${s.label} `, el("span", { class: "legend-value" }, formatValue(chart.kind, value)));
      }));
    };
    canvas.addEventListener("pointermove", (event) => {
      const rect = canvas.getBoundingClientRect();
      const fraction = (event.clientX - rect.left - Number(canvas.dataset.left)) / Number(canvas.dataset.plotW);
      if (fraction < 0 || fraction > 1) return;
      hover[chart.id] = -graphWindow + fraction * graphWindow;
      drawChart(canvas, chart, series); fillLegend();
    });
    canvas.addEventListener("pointerleave", () => { delete hover[chart.id]; drawChart(canvas, chart, series); fillLegend(); });
    fillLegend();
    const card = el("figure", { class: "graph-card" },
      el("figcaption", {}, chart.title, el("span", { class: "graph-unit" }, `, ${unitLabel(chart.kind)}`)), canvas, legend);
    requestAnimationFrame(() => drawChart(canvas, chart, series));
    return card;
  });
  replace("graphs", controls, el("div", { class: "graph-grid" }, cards));
}

async function loadGraphs() {
  try {
    const reply = await fetch(`/api/v1/graphs?window=${graphWindow}`, { credentials: "same-origin", cache: "no-store" });
    graphData = await reply.json();
  } catch {
    return;
  }
  // Don't redraw under a pointer that's reading values, or a selection being copied.
  const hovering = Object.keys(hover).length > 0;
  if (!hovering && !(window.getSelection && !window.getSelection().isCollapsed)) renderGraphs();
}

loadGraphs();
setInterval(loadGraphs, 2000);
document.getElementById("units").addEventListener("change", () => renderGraphs());
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderGraphs());
window.addEventListener("resize", () => renderGraphs());
