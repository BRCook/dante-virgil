"use strict";

// Everything shown here comes from the network (console names, for one), so
// the page is built with textContent and never from HTML strings.

const REFRESH_MS = 2000;
// Seconds without a heartbeat before a member counts as gone quiet.
const QUIET_AFTER = 5;
let status = null;
let session = { logged_in: false, password_set: false };
let lastInvite = null;
// What's been typed into each text field, so a refresh never throws it away.
const drafts = {};
let accountShown = null;

function draft(key, tag, attrs, initial = "") {
  const field = el(tag, attrs);
  field.value = key in drafts ? drafts[key] : initial;
  field.addEventListener("input", () => { drafts[key] = field.value; });
  return field;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function replace(id, ...children) {
  document.getElementById(id).replaceChildren(...children.flat().filter(Boolean));
}

// Figures

// Every time offset on the page is in one unit, chosen in the header and
// remembered by the browser. The precision is the same in each: whole nanoseconds.
const UNITS = {
  ns: { label: "ns", scale: 1, format: new Intl.NumberFormat("en", { maximumFractionDigits: 0 }) },
  us: { label: "µs", scale: 1e3, format: new Intl.NumberFormat("en", { minimumFractionDigits: 3, maximumFractionDigits: 3 }) },
  ms: { label: "ms", scale: 1e6, format: new Intl.NumberFormat("en", { minimumFractionDigits: 6, maximumFractionDigits: 6 }) },
};
let unit = "ns";
try { unit = UNITS[localStorage.getItem("virgil-unit")] ? localStorage.getItem("virgil-unit") : "ns"; } catch { /* private browsing */ }

function magnitude(ns) {
  const u = UNITS[unit];
  return `${u.format.format(Math.abs(ns) / u.scale)} ${u.label}`;
}
const duration = (ns) => `${ns < 0 ? "-" : "+"}${magnitude(ns)}`;

function setUpUnits() {
  const select = document.getElementById("units");
  for (const [key, u] of Object.entries(UNITS)) select.append(el("option", { value: key }, u.label));
  select.value = unit;
  select.addEventListener("change", () => {
    unit = select.value;
    try { localStorage.setItem("virgil-unit", unit); } catch { /* private browsing */ }
    select.blur();
    render();
  });
}
const ppm = (ppb) => `${ppb >= 0 ? "+" : "-"}${Math.abs(ppb / 1000).toFixed(3)} ppm`;

function describeState(state, leader) {
  if (!state) return "not heard from";
  if (state === "follower" && leader) return `following ${leader}`;
  if (state === "sync" && leader) return `syncing to ${leader}`;
  return { leader: "leader", listen: "listening", stopped: "stopped" }[state] || state;
}

// Talking to virgild

async function post(path, body = {}) {
  const reply = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Virgil-Request": "1" },
    body: JSON.stringify(body),
  });
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(data.error || `refused (${reply.status})`);
  return data;
}

let noticeTimer = null;
function notice(message, fault = false) {
  const node = document.getElementById("notice");
  node.textContent = message;
  node.className = fault ? "notice fault" : "notice";
  node.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { node.hidden = true; }, fault ? 8000 : 4000);
}

async function act(path, body, done) {
  try {
    const data = await post(path, body);
    notice(done || data.message || "Done");
    await refresh();
    return data;
  } catch (error) {
    notice(error.message, true);
    if (String(error.message).startsWith("log in")) await loadSession();
    return null;
  }
}

// The parts of the page

function renderAccount() {
  const shown = `${session.password_set}/${session.logged_in}`;
  if (shown === accountShown) return;  // Don't rebuild the login form on every refresh.
  accountShown = shown;
  const canChange = session.logged_in;
  if (!session.password_set) {
    return replace("account", el("span", { class: "hint" },
      "Read-only. To make changes here, run: sudo virgilctl password"));
  }
  if (canChange) {
    return replace("account", el("button", { onclick: logout }, "Log out"));
  }
  const field = el("input", { type: "password", "aria-label": "Password", autocomplete: "current-password",
                              placeholder: "Password", required: true });
  const form = el("form", { class: "account", onsubmit: async (event) => {
    event.preventDefault();
    try {
      await post("/api/v1/login", { password: field.value });
      field.value = "";
      notice("Logged in");
      await loadSession();
    } catch (error) {
      notice(error.message, true);
    }
  } }, field, el("button", { type: "submit", class: "primary" }, "Log in"));
  replace("account", form);
}

async function logout() {
  await fetch("/api/v1/logout", { method: "POST", credentials: "same-origin" });
  notice("Logged out");
  await loadSession();
}

function collectWarnings() {
  const warnings = [];
  const ptp = status.ptp || {};
  for (const w of ptp.warnings || []) warnings.push({ text: w, fault: true });
  for (const d of status.dante_devices || []) for (const w of d.warnings || []) warnings.push({ text: w });
  const auto = status.auto_trim || {};
  if (auto.paused) warnings.push({ text: `Auto-trim paused: ${auto.paused}` });
  if (status.netaudio && status.netaudio.running && status.netaudio.reachable === false) {
    warnings.push({ text: "Can't reach netaudio's daemon, so consoles aren't being monitored" });
  }
  for (const m of (status.group || {}).members || []) {
    if (m.heard_ago === null || m.heard_ago > QUIET_AFTER) warnings.push({ text: `${m.name} isn't being heard from`, fault: true });
    for (const w of m.ptp_warnings || []) warnings.push({ text: `${m.name}: ${w}`, fault: true });
    for (const d of m.dante_devices || []) for (const w of d.warnings || []) warnings.push({ text: `${m.name}: ${w}` });
  }
  return warnings;
}

function consoleRows(devices) {
  if (!devices || devices.length === 0) return null;
  return el("ul", { class: "consoles" }, devices.map((d) => {
    const following = d.follows_this_virgil;
    const offset = d.clock_offset_ppb;
    const settling = d.servo && d.servo !== "synchronized";
    const state = !following ? "fault"
      : (settling || (offset !== null && Math.abs(offset) > 1000) ? "amber" : "ok");
    return el("li", { class: "console" },
      el("span", { class: "console-name", title: [d.model, d.servo && `servo ${d.servo}`].filter(Boolean).join(", ") },
        el("span", { class: `dot ${state}` }), d.name),
      el("span", {}, offset === null || offset === undefined ? (following ? "locked" : "not following") : ppm(offset)));
  }));
}

function clockFacts(clock) {
  if (!clock) return [];
  const facts = [];
  if (clock.phc_offset_ns !== undefined) {
    facts.push(`PHC ${duration(clock.phc_offset_ns)} from system clock, ${clock.phc_locked ? "locked" : "settling"}`);
  } else if (clock.phc_trim_ppm !== undefined) {
    facts.push(`PHC running free, trim ${clock.phc_trim_ppm.toFixed(3)} ppm`);
  }
  return facts;
}

function virgils() {
  // This Virgil and its members, as one list: name, state, leader, clock, consoles.
  const own = {
    name: status.name, self: true, state: status.state, leader: status.leader, clock: status.clock,
    dante_devices: status.dante_devices, heard_ago: 0,
  };
  const members = ((status.group || {}).members || []).map((m) => ({ ...m, self: false }));
  return [own, ...members];
}

function renderChain() {
  const all = virgils();
  const leaders = all.filter((v) => v.state === "leader");
  const others = all.filter((v) => v.state !== "leader");
  const order = [...leaders, ...others];
  const parts = [];
  order.forEach((v, index) => {
    if (index > 0) {
      const clock = v.clock || {};
      const following = v.state === "follower" || v.state === "sync";
      const silent = v.heard_ago === null || v.heard_ago > QUIET_AFTER;
      parts.push(el("div", { class: `link${silent || !following ? " fault" : ""}`, "aria-hidden": "true" },
        el("span", { class: "link-figure" },
          following && clock.system_offset_ns !== undefined ? duration(clock.system_offset_ns) : "—"),
        el("span", { class: "link-line" }),
        el("span", { class: "link-detail" },
          following && clock.rms_offset_ns !== undefined ? `RMS ${magnitude(clock.rms_offset_ns)}`
            : (silent ? "not heard" : describeState(v.state, v.leader)))));
    }
    const silent = !v.self && (v.heard_ago === null || v.heard_ago > QUIET_AFTER);
    parts.push(el("article", { class: `node${v.state === "leader" ? " leader" : ""}${silent ? " silent" : ""}` },
      el("p", { class: "node-name" }, v.name, v.self ? " (this Virgil)" : ""),
      el("span", { class: "node-role" }, describeState(v.state, v.leader === status.name ? "this Virgil" : v.leader)),
      el("div", { class: "node-facts" }, clockFacts(v.clock).map((f) => el("p", {}, f))),
      consoleRows(v.dante_devices)));
  });
  replace("chain", parts);
}

function renderThisVirgil() {
  const can = session.logged_in;
  const managing = !status.observing;
  const clock = status.clock || {};
  const ptp = status.ptp || {};
  const auto = status.auto_trim || {};
  const parts = [];

  const facts = [];
  if (clock.source) {
    const source = clock.source.startsWith("PHC") ? "PHC" : (status.leader || clock.source);
    facts.push(`System clock following ${source}: ${duration(clock.system_offset_ns)}` +
      (clock.rms_offset_ns !== undefined ? `, RMS ${magnitude(clock.rms_offset_ns)}` : ""));
  }
  facts.push(...clockFacts(clock));
  if (ptp.interface && ptp.link_up === false) facts.push(`PTP interface ${ptp.interface} has no link`);
  else if (ptp.identity) facts.push(`PTP grandmaster ${ptp.identity}`);
  if (status.waiting_for_network) facts.push("Waiting for the management network before electing");
  if (status.observing) facts.push("Observing only: roles are set by hand with virgilctl");
  parts.push(el("div", { class: "facts" }, facts.map((f) => el("p", {}, f))));
  if (!can) {
    parts.push(el("p", { class: "hint" }, session.password_set ? "Log in to make changes."
      : "Changes are off until a password is set: sudo virgilctl password"));
  }

  const actions = el("div", { class: "row" });
  const netaudio = status.netaudio;
  if (netaudio && netaudio.ui_port) {
    actions.append(el("a", { class: "button", href: `https://${location.hostname}:${netaudio.ui_port}/`,
      target: "_blank", rel: "noopener noreferrer",
      title: can ? "netaudio, on this Virgil" : "Log in first: netaudio can change the consoles' settings" },
      "Open netaudio"));
  }
  if (status.state === "follower") {
    actions.append(el("button", { class: "primary", disabled: !can || !managing,
      onclick: () => act("/api/v1/lead") }, "Become leader"));
  }
  if (status.state === "stopped") {
    actions.append(el("button", { disabled: !can || !managing, onclick: () => act("/api/v1/start") },
      "Start timing"));
  } else {
    actions.append(el("button", { disabled: !can || !managing, onclick: () => {
      if (confirm(`Stop ${status.name}'s timing services? Its console will lose its clock.`)) act("/api/v1/stop");
    } }, "Stop timing"));
  }
  parts.push(actions);

  parts.push(el("h3", {}, "Trim"));
  const trimField = draft("trim", "input", { type: "number", step: "0.1", "aria-label": "Trim in ppm",
    disabled: !can }, ((status.trim_ppb || 0) / 1000).toFixed(3));
  parts.push(el("div", { class: "row" },
    el("label", {}, trimField, " ppm"),
    el("button", { disabled: !can, onclick: () => {
      const value = Number(trimField.value);
      if (!Number.isFinite(value)) return;
      act("/api/v1/trim", { ppm: value }, `Trim set to ${value.toFixed(3)} ppm`)
        .then((data) => { if (data) delete drafts.trim; });
    } }, "Set trim"),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: !!auto.enabled, disabled: !can,
        onchange: (event) => act("/api/v1/auto-trim", { enabled: event.target.checked },
          event.target.checked ? "Auto-trim on" : "Auto-trim off") }),
      "Adjust automatically")));
  const last = auto.last;
  parts.push(el("p", { class: "hint" }, status.state !== "leader" ? "The trim is only used while this Virgil leads."
    : auto.paused ? `Auto-trim paused: ${auto.paused}`
    : last ? `Last automatic change: ${ppm(last.from_ppb)} to ${ppm(last.to_ppb)}, with consoles at ${ppm(last.consoles_ppb)}`
    : "No automatic changes yet."));

  parts.push(el("h3", {}, "Consoles"));
  const devices = status.dante_devices || [];
  if (devices.length === 0) {
    parts.push(el("p", { class: "hint" }, ptp.link_up === false ? "Nothing is plugged into the PTP interface."
      : "No Dante devices heard on the PTP interface yet."));
  } else {
    parts.push(devicesTable(devices, status.device_settings || {}, can));
    for (const d of devices.filter((d) => !d.mac)) {
      parts.push(el("p", { class: "hint" },
        `Virgil doesn't know ${d.name}'s MAC address yet, so its settings can't be enforced.`));
    }
  }
  parts.push(...interfacesSection(can));
  replace("this-virgil", parts);
}

const ENFORCE = {
  ptpv2: { heading: "Enforce native PTPv2", name: "native PTPv2" },
  aes67: { heading: "Enforce AES67", name: "AES67 mode" },
};
const enforceTip = (name) => `Watches this device, and asks it to turn ${name} back on whenever it's turned off.`;
const onOff = (value) => (value === null || value === undefined ? "Unknown" : value ? "On" : "Off");
const ppmValue = (ppb) => `${ppb >= 0 ? "+" : "-"}${Math.abs(ppb / 1000).toFixed(3)}`;

function devicesTable(devices, settings, can) {
  const header = el("tr", {},
    el("th", { scope: "col", class: "name" }, "Device"),
    ["Model", "Address", "PTPv2", "AES67", "Clock (ppm)"].map((h) => el("th", { scope: "col" }, h)),
    Object.values(ENFORCE).map((e) => el("th", { scope: "col", title: enforceTip(e.name) }, e.heading)));
  const rows = devices.map((d) => {
    const wanted = settings[d.mac] || {};
    const enforce = (setting, on) => {
      const { name, heading } = ENFORCE[setting];
      return el("td", { "data-label": heading }, el("input", {
        type: "checkbox", checked: !!on, disabled: !can || !d.mac, title: enforceTip(name),
        "aria-label": `${heading} on ${d.name}`,
        onchange: (event) => act("/api/v1/device", { mac: d.mac, setting, enabled: event.target.checked },
          event.target.checked ? `Virgil will enforce ${d.name}'s ${name}`
            : `Virgil will no longer enforce ${d.name}'s ${name}`),
      }));
    };
    return el("tr", {},
      el("th", { scope: "row", class: "name" }, d.name),
      el("td", { "data-label": "Model" }, d.model || ""),
      el("td", { "data-label": "Address" }, d.address),
      el("td", { "data-label": "PTPv2" }, onOff(d.ptpv2)),
      el("td", { "data-label": "AES67" }, onOff(d.aes67)),
      el("td", { "data-label": "Clock (ppm)" }, d.clock_offset_ppb === null || d.clock_offset_ppb === undefined
        ? "" : ppmValue(d.clock_offset_ppb)),
      enforce("ptpv2", wanted.native_ptpv2),
      enforce("aes67", wanted.aes67));
  });
  return el("div", { class: "table-wrap" }, el("table", { class: "devices" }, el("thead", {}, header),
    el("tbody", {}, rows)));
}

// Interfaces: fetched when the page opens and after a change, not on every refresh.
let interfaces = null;
async function loadInterfaces() {
  try {
    const reply = await fetch("/api/v1/interfaces", { credentials: "same-origin", cache: "no-store" });
    interfaces = await reply.json();
  } catch {
    interfaces = null;
  }
}

function interfaceLabel(i) {
  // The name, the NIC type where Virgil knows it, and the driver.
  if (!i.physical) return `${i.name} (virtual)`;
  const facts = [i.profile ? i.profile.description : null, i.driver].filter(Boolean);
  return facts.length ? `${i.name} (${facts.join(", ")})` : i.name;
}

function interfaceFacts(i, forPtp) {
  // What's worth knowing about the chosen interface, beside its dropdown.
  if (!i) return [];
  const facts = [];
  if (forPtp) {
    facts.push(i.timestamping === "hardware"
      ? el("span", {}, "Hardware timestamping supported")
      : el("span", { class: "caution" }, "Software timestamping only"));
  }
  if (!i.link_up) facts.push(el("span", { class: "caution" }, "No link"));
  return facts;
}

function interfacesSection(can) {
  const parts = [el("h3", {}, "Interfaces")];
  if (!interfaces) return [...parts, el("p", { class: "hint" }, "Loading…")];
  const byName = (name) => interfaces.interfaces.find((i) => i.name === name);
  const choose = (key, label, options, current) => {
    const forPtp = key === "ptp";
    const select = el("select", { "aria-label": label, disabled: !can });
    if (!current) select.append(el("option", { value: "" }, "Choose…"));
    for (const i of options) select.append(el("option", { value: i.name }, interfaceLabel(i)));
    select.value = key in drafts ? drafts[key] : (current || "");
    const facts = el("span", { class: "select-facts", "aria-live": "polite" }, interfaceFacts(byName(select.value), forPtp));
    select.addEventListener("change", () => {
      drafts[key] = select.value;
      facts.replaceChildren(...interfaceFacts(byName(select.value), forPtp));
    });
    const row = el("div", { class: "row" },
      el("label", { class: "field" }, label, el("span", { class: "select-row" }, select, facts)));
    return { select, row };
  };
  const ptp = choose("ptp", "PTP (faces the console)",
    interfaces.interfaces.filter((i) => i.ptp_capable), interfaces.ptp_interface);
  const mgmt = choose("mgmt", "Management (communicates with other Virgils)",
    interfaces.interfaces, interfaces.mgmt_interface);
  parts.push(
    ptp.row,
    mgmt.row,
    el("p", { class: "hint" }, "Saving restarts Virgil. Changing the PTP interface resets the clock, so any "
      + "connected consoles will re-lock. ", el("strong", {}, "This will interrupt audio.")),
    el("div", { class: "row" }, el("button", { disabled: !can,
      onclick: () => saveInterfaces(ptp.select.value, mgmt.select.value) }, "Save interfaces")));
  return parts;
}

async function saveInterfaces(ptp, mgmt) {
  if (!ptp || !mgmt) return notice("Choose both interfaces first", true);
  if (interfaces && interfaces.ptp_interface && ptp !== interfaces.ptp_interface &&
      !confirm(`Change the PTP interface to ${ptp}? This resets the clock, so any connected consoles will `
        + "re-lock. This will interrupt audio.")) {
    return;
  }
  const body = { ptp_interface: ptp, mgmt_interface: mgmt };
  for (let attempt = 0; attempt < 3; attempt++) {
    const reply = await fetch("/api/v1/interfaces", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Virgil-Request": "1" }, body: JSON.stringify(body),
    });
    const data = await reply.json().catch(() => ({}));
    if (reply.status === 409 && data.needs === "software") {
      if (!confirm(`${data.message}\n\nUse it anyway?`)) return;
      body.accept_software = true;
      continue;
    }
    if (reply.status === 409 && data.needs === "profile") {
      body.accept_profile = confirm(data.message);
      continue;
    }
    if (!reply.ok) return notice(data.error || `refused (${reply.status})`, true);
    delete drafts.ptp;
    delete drafts.mgmt;
    notice(data.message || "Saved");
    if (data.restarting) setTimeout(() => loadInterfaces().then(refresh), 6000);
    return;
  }
}

// A member heard from recently needs no mention; one that's gone quiet does.
function silence(heardAgo) {
  if (heardAgo !== null && heardAgo <= QUIET_AFTER) return null;
  return el("span", { class: "stale" },
    heardAgo === null ? " not heard from" : ` not heard for ${Math.round(heardAgo)} s`);
}

function renderGroup() {
  const can = session.logged_in;
  const group = status.group;
  const parts = [];
  if (!group) {
    parts.push(el("p", {}, "This Virgil isn't in a group."));
    parts.push(el("div", { class: "row" }, el("button", { disabled: !can,
      onclick: () => act("/api/v1/group/create", {}, "Group created") }, "Create a group")));
    const token = draft("join", "textarea", { "aria-label": "Invite token",
      placeholder: "Paste an invite token from a member", disabled: !can });
    parts.push(el("h3", {}, "Join a group"), token,
      el("div", { class: "row" }, el("button", { class: "primary", disabled: !can, onclick: async () => {
        const data = await act("/api/v1/group/join", { token: token.value.trim() });
        if (data) {
          delete drafts.join;
          notice(`Joined, with ${data.members.join(", ")}`);
        }
      } }, "Join group")));
  } else {
    parts.push(el("ul", { class: "plain" }, group.members.map((m) => el("li", {},
      el("strong", {}, m.name), " ",
      el("span", { class: "hint" },
        describeState(m.state, m.leader === status.name ? "this Virgil" : m.leader) +
        (m.addresses && m.addresses.length ? `, at ${m.addresses.join(", ")}` : "")),
      silence(m.heard_ago)))));
    if (group.members.length === 0) parts.push(el("p", { class: "hint" }, "No other members yet."));
    parts.push(el("div", { class: "row" }, el("button", { disabled: !can, onclick: async () => {
      const data = await act("/api/v1/group/invite", { minutes: 60 }, "Invite made");
      if (data) { lastInvite = data; renderGroup(); }
    } }, "Make an invite")));
    if (lastInvite) {
      const field = el("textarea", { readonly: true, "aria-label": "Invite token" });
      field.value = lastInvite.token;
      const until = new Date(lastInvite.expires * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      parts.push(field, el("div", { class: "row" },
        el("button", { onclick: async () => {
          try { await navigator.clipboard.writeText(lastInvite.token); notice("Copied"); }
          catch { field.select(); notice("Select the token and copy it"); }
        } }, "Copy"),
        el("span", { class: "hint" }, `Works once, until ${until}. Paste it into the new Virgil's page.`)));
    }
  }
  const others = status.other_virgils || [];
  if (others.length) {
    parts.push(el("h3", {}, "Other Virgils nearby"), el("ul", { class: "plain" }, others.map((o) =>
      el("li", {}, el("strong", {}, o.name), " ", el("span", { class: "hint" },
        `${o.group_id ? `in group ${o.group_id.slice(0, 8)}` : "not in a group"}, at ${o.addresses.join(", ")}`)))));
  }
  replace("group", parts);
}

function render() {
  if (!status) return;
  document.getElementById("name").textContent = status.name;
  document.title = `${status.name} · Virgil`;
  const role = document.getElementById("role");
  role.textContent = describeState(status.state, status.leader);
  role.className = status.state === "leader" ? "role leader" : "role";
  replace("warnings", collectWarnings().map((w) => el("p", { class: w.fault ? "warning fault" : "warning" }, w.text)));
  renderAccount();
  renderChain();
  // Don't redraw a form while someone's typing in it.
  const typing = document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (!typing) {
    renderThisVirgil();
    renderGroup();
  }
}

async function refresh() {
  try {
    const reply = await fetch("/api/v1/status", { credentials: "same-origin", cache: "no-store" });
    status = await reply.json();
    render();
  } catch {
    replace("warnings", el("p", { class: "warning fault" }, "Can't reach this Virgil. Is virgild running?"));
  }
}

async function loadSession() {
  try {
    const reply = await fetch("/api/v1/session", { credentials: "same-origin", cache: "no-store" });
    session = await reply.json();
  } catch {
    session = { logged_in: false, password_set: false };
  }
  render();
}

setUpUnits();
loadSession().then(loadInterfaces).then(refresh);
setInterval(refresh, REFRESH_MS);
