"use strict";

// ---- data ------------------------------------------------------------

function matches(node) {
  if (!query) return true;
  return [node.label, node.alias, node.type].some(v => v && v.toLowerCase().includes(query));
}

function visible(node) {
  if (!showInactive.checked && node.status !== "active") return false;
  if (hiddenTypes.has(node.type)) return false;
  return !(query && onlyMatches.checked && !matches(node));
}

function typeName(type) {
  return type.includes(":") ? type.slice(type.lastIndexOf(":") + 1) : type;
}

function refilter() {
  let found = 0;
  if (query) for (const node of nodes.values()) if (visible(node) && matches(node)) found++;
  document.getElementById("matches").textContent = query ? `${found} match${found === 1 ? "" : "es"}` : "";
  alpha = Math.max(alpha, 0.3);
  if (autoFit) fit();
}

function buildTypes() {
  const counts = new Map();
  for (const node of nodes.values()) counts.set(node.type, (counts.get(node.type) || 0) + 1);
  const box = document.getElementById("types");
  box.replaceChildren();
  for (const type of [...counts.keys()].sort()) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !hiddenTypes.has(type);
    input.addEventListener("change", () => {
      if (input.checked) hiddenTypes.delete(type); else hiddenTypes.add(type);
      refilter();
    });
    const name = document.createElement("span");
    name.textContent = typeName(type);
    name.title = type;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = counts.get(type);
    label.append(input, name, count);
    box.append(label);
  }
}

function placeNear(node, data) {
  const neighbour = data.links.map(l => nodes.get(l.target)).find(Boolean);
  const angle = Math.random() * 2 * Math.PI;
  const base = neighbour || {x: 0, y: 0};
  node.x = base.x + Math.cos(angle) * 30;
  node.y = base.y + Math.sin(angle) * 30;
}

function upsert(data, newLinks) {
  let node = nodes.get(data.id);
  if (!node) {
    node = {vx: 0, vy: 0, flash: null, fixed: false};
    nodes.set(data.id, node);
    placeNear(Object.assign(node, data), {links: newLinks});
    alpha = Math.max(alpha, 0.3);
    buildTypes();
  }
  Object.assign(node, data);
  for (const [id, link] of links) {
    if (link.source === data.id) links.delete(id);
  }
  for (const link of newLinks) links.set(link.id, link);
  return node;
}

function flash(node, kind) {
  node.flash = {kind, t0: performance.now()};
}

function sessionColor(session) {
  const key = session || "";
  if (!sessions.has(key)) sessions.set(key, SESSION_COLORS[sessions.size % SESSION_COLORS.length]);
  return sessions.get(key);
}

function excite(node, color, amount = 1) {
  node.glow = {e: Math.min(MAX_GLOW, (node.glow ? node.glow.e : 0) + amount), color};
  lit.add(node);
}

function exciteLink(link, color) {
  const glow = litLinks.get(link.id);
  litLinks.set(link.id, {e: Math.min(MAX_GLOW, (glow ? glow.e : 0) + 1), color});
}

// One read as a walk: a pulse along each walked edge, in the order the read names them,
// from the end the read names first (the start of a neighbors call, the row's first node);
// the entities no walked edge reaches spark one after the other.
function walk(known, walked, color) {
  const now = performance.now();
  const order = new Map(known.map((node, index) => [node.id, index]));
  const rank = id => order.has(id) ? order.get(id) : Infinity;
  const step = Math.min(STEP_MS, SPREAD_MS / Math.max(1, walked.length));
  const reached = new Set();
  walked.forEach((link, index) => {
    const forward = rank(link.source) <= rank(link.target);
    const from = nodes.get(forward ? link.source : link.target), to = nodes.get(forward ? link.target : link.source);
    reached.add(from.id); reached.add(to.id);
    if (reducedMotion) { excite(from, color); excite(to, color); exciteLink(link, color); return; }
    pulses.push({link, from, to, t0: now + index * step, color, started: false, arrived: false});
  });
  if (pulses.length > MAX_PULSES) pulses.splice(0, pulses.length - MAX_PULSES);
  const rest = known.filter(node => !reached.has(node.id));
  const gap = Math.min(25, 800 / Math.max(1, rest.length));
  rest.forEach((node, index) => {
    if (reducedMotion) excite(node, color);
    else sparks.push({node, t0: now + index * gap, color});
  });
}

// Advances pulses and sparks to ``now`` and lets every glow fade by the time since the last frame.
function animate(now) {
  const dt = Math.max(0, now - lastFrame);
  lastFrame = now;
  const fade = Math.exp(-dt / GLOW_MS);
  for (const node of lit) {
    node.glow.e *= fade;
    if (node.glow.e < 0.02 || nodes.get(node.id) !== node) { node.glow = null; lit.delete(node); }
  }
  for (const [id, glow] of litLinks) {
    glow.e *= fade;
    if (glow.e < 0.02 || !links.has(id)) litLinks.delete(id);
  }
  sparks = sparks.filter(spark => {
    if (now < spark.t0) return true;
    excite(spark.node, spark.color);
    return false;
  });
  pulses = pulses.filter(pulse => {
    if (now < pulse.t0) return true;
    if (!pulse.started) { pulse.started = true; excite(pulse.from, pulse.color, 0.7); }
    if (now - pulse.t0 < PULSE_MS) return true;
    excite(pulse.to, pulse.color);
    exciteLink(pulse.link, pulse.color);
    return false;
  });
  let peak = 0;
  for (const node of lit) peak = Math.max(peak, node.glow.e);
  const target = Math.min(1, peak);
  ambient += (target - ambient) * (1 - Math.exp(-dt / (target > ambient ? 150 : 1200)));
}

function rgba(hex, a) {
  const value = parseInt(hex.replace("#", ""), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${Math.max(0, Math.min(1, a))})`;
}

// The query of a route of the shown graph; every route is relative, so a proxy may serve
// the page below a path prefix.
function api(path, extra) {
  const params = new URLSearchParams(extra || {});
  if (current) params.set("graph", current);
  params.set("t", token);
  return `${path}?${params}`;
}

async function problem(response) {
  try {
    const body = await response.json();
    if (body && body.message) return body.message;
  } catch (error) { /* not JSON */ }
  return "HTTP " + response.status;
}

async function loadGraph(gen) {
  loading = true;
  let data;
  try {
    const response = await fetch(api("graph"), {cache: "no-store"});
    if (!response.ok) throw new Error(await problem(response));
    data = await response.json();
  } catch (error) {
    if (gen === generation) loading = false;
    throw error;
  }
  if (gen !== generation) return;
  try {
    const old = nodes;
    nodes = new Map();
    links = new Map();
    lit = new Set();
    litLinks = new Map();
    pulses = [];
    sparks = [];
    for (const item of data.nodes) {
      const node = Object.assign({vx: 0, vy: 0, flash: null, fixed: false}, old.get(item.id) || {}, item, {glow: null});
      if (node.x === undefined) {
        const angle = Math.random() * 2 * Math.PI, radius = 40 + Math.random() * 20 * Math.sqrt(data.nodes.length);
        node.x = Math.cos(angle) * radius;
        node.y = Math.sin(angle) * radius;
      }
      nodes.set(item.id, node);
    }
    for (const link of data.links) links.set(link.id, link);
    seq = data.seq;
    currentId = data.graph.id;
    renderGraphs();
    document.title = data.graph.name + " · graph view";
    document.getElementById("title").textContent = data.graph.name;
    document.getElementById("reads").textContent = data.signal_reads ? "" : "(off: [activity] signal_reads)";
    alpha = old.size ? Math.max(alpha, 0.3) : 1;
    if (focusTypes) {
      hiddenTypes = new Set(data.types.filter(type => !focusTypes.has(type)));
      focusTypes = null;
    }
    updateCounts();
    buildTypes();
    refilter();
  } finally {
    loading = false;
  }
  const queued = pending;
  pending = [];
  queued.forEach(handle);
}

function updateCounts() {
  let active = 0;
  for (const node of nodes.values()) if (node.status === "active") active++;
  document.getElementById("counts").textContent =
    `${active} active · ${nodes.size - active} inactive · ${links.size} edges`;
}

function handle(event) {
  if (loading) { pending.push(event); return; }
  if (event.kind === "created" || event.kind === "updated") {
    if (event.seq <= seq) return;
    const node = upsert(event.node, event.links);
    flash(node, event.kind);
    excite(node, event.kind);
    record(event.kind, node.label, event.actor, node.id);
    updateCounts();
    if (selected && (selected.id === node.id || selectedSource() === node.id)) showSelection();
  } else if (event.kind === "read") {
    const color = sessionColor(event.session);
    const known = event.ids.map(id => nodes.get(id)).filter(Boolean);
    const walked = (event.edges || []).map(id => links.get(id)).filter(l => l && nodes.has(l.source) && nodes.has(l.target));
    walk(known, walked, color);
    if (known.length || walked.length) {
      const what = known.length === 1 ? known[0].label : `${known.length} entities`;
      const along = walked.length ? `, ${walked.length} edge${walked.length === 1 ? "" : "s"}` : "";
      const who = [event.actor, event.session ? `session ${event.session}` : null].filter(Boolean).join(" · ");
      record("read", `${event.command || "read"}: ${what}${along}`, who, known.length === 1 ? known[0].id : null, color);
    }
  } else if (event.kind === "reload") {
    // A write too big to send revision by revision, a connection that fell behind or a new
    // ontology: the graph is loaded again; events up to its seq are in it already.
    const gen = generation;
    if (event.reason === "large_tx") {
      record("updated", `${event.revisions} revisions, graph reloaded`, event.actor, null);
    }
    loadGraph(gen).catch(error => { if (gen === generation) failGraph(error); });
  } else if (event.kind === "error") {
    setStatus(`error: ${event.message}`, false);
  }
}

function record(kind, label, actor, id, color) {
  const item = document.createElement("li");
  item.className = "event";
  const dot = kind === "created" ? `<circle class="dot created" cx="7" cy="7" r="2"/>`
    : kind === "read" ? `<circle class="dot read" cx="7" cy="7" r="3"/>` : "";
  item.innerHTML = `<svg class="mark" viewBox="0 0 14 14" aria-hidden="true"><circle class="ring ${kind}" cx="7" cy="7" r="5"/>${dot}</svg><div class="body"><div class="what"></div><div class="when"></div></div>`;
  if (color) item.querySelector("svg").style.setProperty("--read", colors[color]);
  item.querySelector(".what").textContent = kind === "read" ? label : `${kind}: ${label}`;
  item.querySelector(".when").textContent = new Date().toLocaleTimeString() + (actor ? ` · ${actor}` : "");
  if (id) {
    item.classList.add("clickable");
    item.addEventListener("click", () => select({kind: "node", id}));
  }
  eventList.prepend(item);
  while (eventList.children.length > MAX_EVENTS) eventList.lastChild.remove();
}

