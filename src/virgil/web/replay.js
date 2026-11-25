"use strict";
// Replaying a saved log (see virgil.replay): the page shows a moment of the session through its usual sections, as
// if it were live, with a timeline across the top to move through it. While replaying, the page's own refreshes
// pause (app.js and graphs.js check `replaying`), and it treats you as logged out, so nothing can be changed.

const REPLAY_SPEEDS = [1, 10, 60];
const REPLAY_STALE = 10;  // seconds: a status reading older than this at the moment means a Virgil wasn't logging
let replayTimer = null;

async function startReplay(name) {
  if (replaying) exitReplay();
  try {
    await post("/api/v1/replay", { name });
  } catch (error) {
    notice(`Couldn't replay ${name}: ${error.message}`, true);
    return;
  }
  replaying = { name, preparing: true, phase: "starting", t: 0, speed: 1, playing: false, busy: false, wanted: null,
    liveSession: session, own: status && status.name };
  renderReplay();
  window.scrollTo({ top: 0, behavior: "smooth" });
  waitForReplay(name);
}

async function waitForReplay(name) {
  while (replaying && replaying.name === name) {
    let state;
    try {
      const reply = await fetch(`/api/v1/replay?name=${encodeURIComponent(name)}`, { credentials: "same-origin", cache: "no-store" });
      state = await reply.json();
    } catch {
      state = { state: "preparing", phase: "starting" };
    }
    if (!replaying || replaying.name !== name) return;
    if (state.state === "ready") {
      const o = state.overview;
      Object.assign(replaying, { preparing: false, overview: o, t: Math.min(o.from + graphWindow, o.to) });
      session = { ...session, logged_in: false };  // read-only while replaying
      renderReplay();
      seekReplay(replaying.t);
      return;
    }
    if (state.state !== "preparing") {
      notice(`Couldn't replay ${name}: ${state.error || "it isn't prepared"}`, true);
      exitReplay();
      return;
    }
    replaying.phase = state.phase;
    renderReplay();
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
  }
}

function exitReplay() {
  if (!replaying) return;
  if (replayTimer) clearInterval(replayTimer);
  replayTimer = null;
  session = replaying.liveSession;
  replaying = null;
  document.getElementById("replay").hidden = true;
  replace("replay");
  forgetRendered();
  refresh();
  loadGraphs();
}

function seekReplay(t) {
  if (!replaying || !replaying.overview) return;
  const o = replaying.overview;
  replaying.t = Math.max(o.from, Math.min(o.to, t));
  drawReplayStrip();
  updateReplayTime();
  if (replaying.busy) {
    replaying.wanted = replaying.t;  // the latest wins; fetched when the one in flight is done
  } else {
    replayFetch(replaying.t);
  }
}

// Called by graphs.js when its window changes while replaying: the same moment, with the new window.
function replayLoad() {
  if (replaying && replaying.overview) seekReplay(replaying.t);
}

async function replayFetch(t) {
  const name = replaying.name;
  replaying.busy = true;
  let moment = null;
  try {
    const query = `name=${encodeURIComponent(name)}&t=${t}&window=${graphWindow}`;
    const reply = await fetch(`/api/v1/replay/at?${query}`, { credentials: "same-origin", cache: "no-store" });
    if (reply.ok) moment = await reply.json();
  } catch { /* try again with the next move */ }
  if (!replaying || replaying.name !== name) return;
  replaying.busy = false;
  if (moment) showMoment(moment);
  if (replaying.wanted !== null) {
    const next = replaying.wanted;
    replaying.wanted = null;
    if (next !== t) replayFetch(next);
  }
}

function showMoment(moment) {
  const statuses = moment.statuses || {};
  const own = statuses[replaying.own] || Object.values(statuses)[0] || {};
  replaying.quiet = Object.entries(statuses).filter(([, s]) => s.age === null || s.age > REPLAY_STALE).map(([n]) => n);
  if (own.status) {
    // The Events section, as it was then: everything up to the moment, newest first.
    const events = (replaying.overview.events || []).filter((e) => e.t <= moment.t && e.virgil === (own.status.name || e.virgil))
      .reverse().slice(0, 50);
    status = { ...own.status, events };
  }
  graphData = { window: moment.window, series: moment.series };
  render();
  renderGraphs();
  updateReplayTime();
}

function updateReplayTime() {
  const label = document.getElementById("replay-time");
  if (!label || !replaying || !replaying.overview) return;
  label.textContent = new Date(replaying.t * 1000).toLocaleString([], { hour12: false });
  const quiet = document.getElementById("replay-quiet");
  if (quiet) {
    quiet.textContent = (replaying.quiet || []).length
      ? `${replaying.quiet.join(" and ")} ${replaying.quiet.length === 1 ? "wasn't" : "weren't"} logging at this moment.` : "";
  }
  const play = document.getElementById("replay-play");
  if (play) play.textContent = replaying.playing ? "Pause" : "Play";
}

function setPlaying(playing) {
  replaying.playing = playing;
  if (replayTimer) clearInterval(replayTimer);
  replayTimer = playing ? setInterval(() => {
    if (!replaying || !replaying.overview) return;
    if (replaying.t >= replaying.overview.to) {
      setPlaying(false);
      return;
    }
    seekReplay(replaying.t + replaying.speed);
  }, 1000) : null;
  updateReplayTime();
}

// The markers: notes, alerts opening, and the group's events, to jump to.
function replayMarkers() {
  const o = replaying.overview;
  const notes = (o.notes || []).map((n) => ({ t: Date.parse(n.at) / 1000, kind: "note", text: `Note: ${n.text}` }));
  const events = (o.events || []).filter((e) => e.event === "opened" || e.event === "group").map((e) => ({
    t: e.t, kind: e.event === "group" ? "group" : "alert",
    text: e.event === "group" ? `${e.virgil}: ${e.title}` : `${e.label} on ${e.device} (${e.virgil})` }));
  // One marker for the same thing at the same moment (an alert on several of a device's flows, say).
  const seen = new Set();
  return [...notes, ...events].filter((m) => Number.isFinite(m.t)).sort((a, b) => a.t - b.t).filter((m) => {
    const key = `${Math.round(m.t)}|${m.text}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function renderReplay() {
  const box = document.getElementById("replay");
  box.hidden = false;
  if (replaying.preparing) {
    const phase = { unpacking: "unpacking it", indexing: "indexing it" }[replaying.phase] || "starting";
    replace("replay", el("div", { class: "replay-head" },
      el("strong", {}, `Preparing ${replaying.name} for replay: ${phase}…`),
      el("button", { onclick: exitReplay }, "Cancel")),
    el("p", { class: "hint" }, "This happens once for each log, at a low priority; a long log takes a minute or so."));
    return;
  }
  const step = (seconds, label) => el("button", { onclick: () => seekReplay(replaying.t + seconds) }, label);
  const speed = el("select", { "aria-label": "Playback speed", onchange: (event) => { replaying.speed = Number(event.target.value); } },
    ...REPLAY_SPEEDS.map((s) => el("option", { value: String(s), ...(s === replaying.speed ? { selected: "" } : {}) }, `${s}×`)));
  const markers = replayMarkers();
  const jump = el("select", { "aria-label": "Jump to", onchange: (event) => {
    if (event.target.value) seekReplay(Number(event.target.value));
    event.target.value = "";
  } }, el("option", { value: "" }, markers.length ? `Jump to… (${markers.length})` : "Nothing to jump to"),
  ...markers.map((m) => el("option", { value: String(m.t) },
    `${new Date(m.t * 1000).toLocaleTimeString([], { hour12: false })} ${m.text}`)));
  const strip = el("canvas", { id: "replay-strip", class: "replay-strip", "aria-label": "The session: click or drag to move through it" });
  let dragging = false;
  const seekAt = (event) => {
    const box = strip.getBoundingClientRect();
    const o = replaying.overview;
    seekReplay(o.from + ((event.clientX - box.left) / box.width) * (o.to - o.from));
  };
  strip.addEventListener("pointerdown", (event) => { dragging = true; strip.setPointerCapture(event.pointerId); seekAt(event); });
  strip.addEventListener("pointermove", (event) => { if (dragging) seekAt(event); });
  strip.addEventListener("pointerup", () => { dragging = false; });
  replace("replay",
    el("div", { class: "replay-head" },
      el("strong", {}, "Replaying"), el("span", { class: "replay-name" }, replaying.name),
      el("span", { id: "replay-time", class: "replay-time" }),
      el("button", { class: "primary", onclick: exitReplay }, "Back to live")),
    strip,
    el("div", { class: "replay-controls" },
      step(-60, "−1 min"), step(-10, "−10 s"),
      el("button", { id: "replay-play", onclick: () => setPlaying(!replaying.playing) }, "Play"),
      step(10, "+10 s"), step(60, "+1 min"), speed, jump,
      el("span", { id: "replay-quiet", class: "hint" })),
    el("p", { class: "hint" }, "Nothing can be changed while replaying. The strip shows each Dante device's worst-flow "
      + "latency, late packets in red, and notes, alerts and the group's events as marks along the bottom."));
  requestAnimationFrame(drawReplayStrip);
  updateReplayTime();
}

function drawReplayStrip() {
  const canvas = document.getElementById("replay-strip");
  if (!canvas || !replaying || !replaying.overview) return;
  const o = replaying.overview;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const g = canvas.getContext("2d");
  g.scale(ratio, ratio);
  g.clearRect(0, 0, width, height);
  const span = Math.max(1, o.to - o.from);
  const x = (t) => ((t - o.from) / span) * width;
  const plot = height - 12;  // marks along the bottom
  // Worst-flow latency: the worst of every device in each slice, as a filled shape.
  const slices = o.slices || 1;
  const worst = Array.from({ length: slices }, (_, i) => Math.max(0, ...Object.values(o.overview || {})
    .map((d) => d.worst_ns[i] || 0)));
  const sorted = worst.filter((v) => v > 0).sort((a, b) => a - b);
  const top = sorted.length ? sorted[Math.floor(sorted.length * 0.995)] * 1.1 : 1;  // ignore the rare huge spike
  g.fillStyle = cssVar("--line");
  g.beginPath();
  g.moveTo(0, plot);
  worst.forEach((v, i) => g.lineTo((i / slices) * width, plot - Math.min(1, v / top) * (plot - 4)));
  g.lineTo(width, plot);
  g.closePath();
  g.fill();
  // Late packets: a red bar in every slice that had any.
  g.fillStyle = cssVar("--fault");
  for (let i = 0; i < slices; i += 1) {
    if (Object.values(o.overview || {}).some((d) => d.late[i] > 0)) g.fillRect((i / slices) * width, 0, Math.max(2, width / slices), plot);
  }
  // The window the graphs show, and the moment.
  g.fillStyle = "rgba(127, 127, 127, 0.18)";
  g.fillRect(x(replaying.t - graphWindow), 0, x(replaying.t) - x(replaying.t - graphWindow), plot);
  g.fillStyle = cssVar("--ink");
  g.fillRect(x(replaying.t) - 1, 0, 2, height);
  // Marks along the bottom.
  const colours = { note: cssVar("--focus"), alert: cssVar("--amber"), group: cssVar("--muted") };
  for (const m of replayMarkers()) {
    g.fillStyle = colours[m.kind];
    g.fillRect(x(m.t) - 1, plot + 2, 3, 9);
  }
}

window.addEventListener("resize", () => requestAnimationFrame(drawReplayStrip));
