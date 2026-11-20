"use strict";

// Graphs: the last few minutes of every clock, console and the link, drawn on
// canvases with no library, so they work offline. Times use the page's units.
// Relies on app.js for el(), replace(), UNITS and the chosen unit.

const CHARTS = [
  // In groups of three, so each row holds related charts. Time charts start in a unit that suits them;
  // null follows the page's Units setting.
  { id: "offset", group: "Clocks", title: "Clock offset from the leader", kind: "time", signed: true, unit: null },
  { id: "phc", group: "Clocks", title: "PHC offset from system clock", kind: "time", signed: true, unit: null },
  { id: "trim", group: "Clocks", title: "Trim", kind: "ppm", fit: true },
  { id: "latency", group: "Dante devices", title: "Receive latency", kind: "time", wide: true, unit: "ms" },
  { id: "late", group: "Dante devices", title: "Late packets", kind: "rate" },
  { id: "console", group: "Dante devices", title: "Device clocks", kind: "ppm", signed: true },
  { id: "rtt", group: "Link", title: "Link round trip", kind: "time", unit: "ms" },
  { id: "jitter", group: "Link", title: "Link jitter", kind: "time", unit: "us" },
  { id: "loss", group: "Link", title: "Link packet loss", kind: "percent" },
];
const WINDOWS = [[30, "30 s"], [60, "1 min"], [120, "2 min"], [300, "5 min"], [600, "10 min"]];
// Consoles and Clock Boxes draw from separate sets, each ordered so neighbours contrast, so two consoles (or two
// Clock Boxes) never end up in near-identical colours, whatever they're called.
const CONSOLE_COLOURS = ["#9c6ade", "#e07a5f", "#59a14f", "#b07aa1", "#edc948", "#ff9da7"];
const VIRGIL_COLOURS = ["#4c78a8", "#2a9d8f", "#56b4e9", "#8d99ae", "#76b7b2", "#bab0ac"];

let graphWindow = 300;
try { graphWindow = Number(localStorage.getItem("virgil-graph-window")) || 300; } catch { /* private browsing */ }
let graphData = null;
const hover = {};  // chart id: seconds before now under the pointer

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Each time chart's unit: the one chosen for it on this browser, else its own default, else the page's.
const GRAPH_DIGITS = { ns: 0, us: 2, ms: 3 };
const UNIT_ORDER = ["ns", "us", "ms"];

function chartUnit(chart) {
  try {
    const chosen = localStorage.getItem(`virgil-graph-unit-${chart.id}`);
    if (UNITS[chosen]) return chosen;
  } catch { /* private browsing */ }
  return chart.unit || unit;
}

function nextUnit(chart) {
  const following = UNIT_ORDER[(UNIT_ORDER.indexOf(chartUnit(chart)) + 1) % UNIT_ORDER.length];
  try { localStorage.setItem(`virgil-graph-unit-${chart.id}`, following); } catch { /* private browsing */ }
}

function formatTime(chart, ns) {
  const key = chartUnit(chart);
  const digits = GRAPH_DIGITS[key];
  const number = new Intl.NumberFormat("en", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${ns < 0 ? "-" : ""}${number.format(Math.abs(ns) / UNITS[key].scale)} ${UNITS[key].label}`;
}

function formatValue(chart, value) {
  const kind = chart.kind;
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  if (kind === "time") return formatTime(chart, value);
  if (kind === "ppm") return `${value >= 0 ? "+" : ""}${value.toFixed(3)} ppm`;
  if (kind === "percent") return `${value.toFixed(1)}%`;
  return `${value.toFixed(value < 10 ? 1 : 0)}/s`;
}

// The summary of a series over the whole window, worked out by virgild from every reading (not the thinned points
// drawn here): one muted figure after the current value, with what it means on hover.
function summaryParts(chart, summary) {
  if (!summary || typeof summary !== "object") return null;
  const number = (v) => typeof v === "number" && Number.isFinite(v);
  const window = (WINDOWS.find(([seconds]) => seconds === graphWindow) || [0, `${graphWindow} s`])[1];
  const over = `over the last ${window.replace(/^1 min$/, "minute").replace(/^30 s$/, "30 seconds")
    .replace(/ min$/, " minutes")}`;
  switch (summary.kind) {
    case "peak":
      return number(summary.value) ? [`peak ${formatValue(chart, summary.value)}`, `The largest swing either way ${over}`] : null;
    case "max":
      return number(summary.value) ? [`max ${formatValue(chart, summary.value)}`, `The highest ${over}`] : null;
    case "mean":
      return number(summary.value) ? [`mean ${formatValue(chart, summary.value)}`, `The average ${over}`] : null;
    case "median":
      if (!number(summary.value) || !number(summary.spread)) return null;
      return [`median ${formatValue(chart, summary.value)}, spread ${formatValue(chart, summary.spread)}`,
        `Half the readings ${over} were below the median. The spread is how steady it was: the middle 80% of `
        + `readings fell within ${formatValue(chart, summary.spread)} of each other`];
    case "total":
      return number(summary.value)
        ? [`total ${Math.round(summary.value).toLocaleString("en")}`, `Late packets counted ${over}`] : null;
    case "range":
      return number(summary.low) && number(summary.high)
        ? [`range ${formatValue(chart, summary.low)} to ${formatValue(chart, summary.high)}`, `Lowest to highest ${over}`]
        : null;
    default:
      return null;
  }
}

function scaled(chart, value) {
  // Values on the axis, in the chart's own time unit for times.
  return chart.kind === "time" ? value / UNITS[chartUnit(chart)].scale : value;
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
    const y = scaled(chart, v);
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
    // As many decimals as the step between ticks needs, so small values never all read "0" (ppm keeps at least 2).
    const digits = Math.max(chart.kind === "ppm" ? 2 : 0, -Math.floor(Math.log10(step)));
    const label = new Intl.NumberFormat("en", { maximumFractionDigits: Math.min(digits, 6) }).format(v);
    g.fillText(label, left - 6, y(v));
  }
  g.textAlign = "center";
  g.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const t = -graphWindow + (graphWindow * i) / 4;
    // The last label ends at the edge rather than straddling it, so it's never cut off.
    g.textAlign = i === 4 ? "right" : "center";
    g.fillText(agoLabel(t), x(t), top + plotH + 6);
  }
  g.save();
  g.beginPath(); g.rect(left, top, plotW, plotH); g.clip();
  series.forEach((s) => {
    g.strokeStyle = s.colour;
    g.lineWidth = s.style === "line" ? 1.6 : 1.3;
    g.setLineDash({ setting: [6, 4], average: [1.5, 3], line: [] }[s.style]);
    g.beginPath();
    s.points.forEach(([t, v], i) => (i ? g.lineTo(x(t), y(scaled(chart, v))) : g.moveTo(x(t), y(scaled(chart, v)))));
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
  // A console's worst, average and setting lines share its colour, and differ in style.
  const base = (label) => label.replace(/ (setting|worst flow|worst|average)$/, "");
  // A console's series are labelled "<console> (<Clock Box>)"; everything else belongs to a Clock Box or the link.
  const isConsole = (name) => /\(.+\)$/.test(name);
  const names = [...new Set(graphData.series.map((s) => base(s.label)))].sort();
  const consoles = names.filter(isConsole);
  const others = names.filter((n) => !isConsole(n));
  const colourOf = (label) => {
    const name = base(label);
    return isConsole(name) ? CONSOLE_COLOURS[consoles.indexOf(name) % CONSOLE_COLOURS.length]
      : VIRGIL_COLOURS[others.indexOf(name) % VIRGIL_COLOURS.length];
  };
  const styleOf = (key) => (key.startsWith("limit/") ? "setting" : key.startsWith("average/") ? "average" : "line");

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
    // Grouped by console: its worst, then its average, then its setting.
    const styleRank = { line: 0, average: 1, setting: 2 };
    const series = byChart[chart.id].map((s) => ({ ...s, colour: colourOf(s.label), style: styleOf(s.key) }))
      .sort((x, y) => base(x.label).localeCompare(base(y.label)) || styleRank[x.style] - styleRank[y.style]);
    const canvas = el("canvas", { class: "graph", role: "img", "aria-label": chart.title });
    const legend = el("ul", { class: "legend" });
    const fillLegend = () => {
      const at = hover[chart.id];
      const when = at === undefined ? [] : [el("li", { class: "legend-when" }, `at ${agoLabel(at)}`)];
      legend.replaceChildren(...when, ...series.map((s) => {
        const value = at === undefined ? (s.points.length ? s.points[s.points.length - 1][1] : null) : valueAt(s.points, at);
        // The colour is set through the CSSOM: the content security policy blocks inline style attributes.
        const swatch = el("span", { class: `swatch ${s.style}` });
        swatch.style.setProperty("--swatch", s.colour);
        const summary = summaryParts(chart, s.summary);
        return el("li", {}, swatch,
          `${s.label} `, el("span", { class: "legend-value" }, formatValue(chart, value)),
          summary ? el("span", { class: "legend-summary", title: summary[1] }, summary[0]) : null);
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
    // A chart with many lines gets a row to itself, with its legend in columns.
    const wide = chart.wide || series.length > 4;
    const card = el("figure", { class: wide ? "graph-card wide" : "graph-card" },
      el("figcaption", {}, chart.title, ", ", chart.kind === "time"
        ? el("button", {
          class: "unit-switch", type: "button",
          title: "Change this chart's unit", "aria-label": `${chart.title}: in ${UNITS[chartUnit(chart)].label}. Change unit`,
          onclick: () => { nextUnit(chart); renderGraphs(); },
        }, UNITS[chartUnit(chart)].label)
        : el("span", { class: "graph-unit" }, unitLabel(chart.kind))), canvas, legend);
    requestAnimationFrame(() => drawChart(canvas, chart, series));
    return card;
  });
  // One labelled row of charts per group.
  const groups = [];
  shown.forEach((chart, index) => {
    if (!groups.length || groups[groups.length - 1].name !== chart.group) groups.push({ name: chart.group, cards: [] });
    groups[groups.length - 1].cards.push(cards[index]);
  });
  // Each row's charts share its width: as many columns as it has charts (besides full-width ones), at most three.
  const columns = (cards) => Math.min(3, Math.max(1, cards.filter((card) => !card.classList.contains("wide")).length));
  replace("graphs", controls, ...groups.map((group) => el("section", { class: "graph-group" },
    el("h3", {}, group.name), el("div", { class: `graph-grid cols-${columns(group.cards)}` }, group.cards))));
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
