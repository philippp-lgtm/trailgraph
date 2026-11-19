"use strict";

// ---- layout ----------------------------------------------------------

const REPULSE_RANGE = 300; // nodes further apart do not repel each other
const MAX_STEP = 40;       // the farthest a node moves in one tick

function tick() {
  if (alpha < 0.005) return;
  const list = [...nodes.values()].filter(visible);
  // Repulsion only reaches REPULSE_RANGE, so a grid of that cell size finds every pair in
  // range by looking at a cell and its forward neighbours: about n * neighbours, not n * n.
  const grid = new Map();
  for (const node of list) {
    const cx = Math.floor(node.x / REPULSE_RANGE), cy = Math.floor(node.y / REPULSE_RANGE);
    const key = cx + "," + cy;
    let cell = grid.get(key);
    if (!cell) grid.set(key, cell = {cx, cy, items: []});
    cell.items.push(node);
  }
  const repel = (a, b) => {
    let dx = b.x - a.x, dy = b.y - a.y;
    let d2 = dx * dx + dy * dy;
    if (d2 > REPULSE_RANGE * REPULSE_RANGE) return;
    if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
    const force = (-900 * alpha) / d2;
    a.vx += dx * force; a.vy += dy * force;
    b.vx -= dx * force; b.vy -= dy * force;
  };
  for (const cell of grid.values()) {
    const items = cell.items;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) repel(items[i], items[j]);
    }
    for (const [ox, oy] of [[1, 0], [-1, 1], [0, 1], [1, 1]]) {
      const other = grid.get((cell.cx + ox) + "," + (cell.cy + oy));
      if (!other) continue;
      for (const a of items) for (const b of other.items) repel(a, b);
    }
  }
  const springs = [];
  const degree = new Map();
  for (const link of links.values()) {
    const a = nodes.get(link.source), b = nodes.get(link.target);
    if (!a || !b || a === b || !visible(a) || !visible(b)) continue;
    springs.push([a, b]);
    degree.set(a, (degree.get(a) || 0) + 1);
    degree.set(b, (degree.get(b) || 0) + 1);
  }
  for (const [a, b] of springs) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const force = ((d - 50) / d) * 0.08 * alpha;
    // Each end takes its share by its edge count: summed over hundreds of edges, the full
    // pull would move a hub past its neighbours and further out on every tick.
    const fa = force / degree.get(a), fb = force / degree.get(b);
    a.vx += dx * fa; a.vy += dy * fa;
    b.vx -= dx * fb; b.vy -= dy * fb;
  }
  for (const node of list) {
    node.vx -= node.x * 0.004 * alpha;
    node.vy -= node.y * 0.004 * alpha;
    if (node.fixed) { node.vx = node.vy = 0; continue; }
    node.vx *= 0.6; node.vy *= 0.6;
    const speed = Math.sqrt(node.vx * node.vx + node.vy * node.vy);
    if (speed > MAX_STEP) { node.vx *= MAX_STEP / speed; node.vy *= MAX_STEP / speed; }
    node.x += node.vx; node.y += node.vy;
  }
  alpha *= 0.985;
}

// ---- drawing ---------------------------------------------------------

function resize() {
  const ratio = devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio;
  canvas.height = canvas.clientHeight * ratio;
}

function toScreen(node) {
  return [canvas.clientWidth / 2 + view.x + node.x * view.k, canvas.clientHeight / 2 + view.y + node.y * view.k];
}

function toWorld(sx, sy) {
  return [(sx - canvas.clientWidth / 2 - view.x) / view.k, (sy - canvas.clientHeight / 2 - view.y) / view.k];
}

function drawFlash(x, y, node, now) {
  const t = (now - node.flash.t0) / FLASH_MS;
  if (t >= 1) { node.flash = null; return; }
  const color = colors[node.flash.kind];
  const grow = reducedMotion ? 0 : t * 10;
  ctx.save();
  ctx.globalAlpha = 1 - t;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.setLineDash(node.flash.kind === "read" ? [4, 3] : []);
  ctx.beginPath();
  ctx.arc(x, y, node.r + 5 + grow, 0, 2 * Math.PI);
  ctx.stroke();
  if (node.flash.kind === "created") {
    ctx.beginPath();
    ctx.arc(x, y, node.r + 10 + grow * 1.6, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.globalAlpha = 0.35 * (1 - t);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, node.r + 4, 0, 2 * Math.PI);
  ctx.fill();
  ctx.restore();
}

function halo(x, y, inner, outer, color, strength) {
  const gradient = ctx.createRadialGradient(x, y, inner, x, y, outer);
  gradient.addColorStop(0, rgba(color, 0.85 * strength));
  gradient.addColorStop(1, rgba(color, 0));
  ctx.fillStyle = gradient;
  ctx.beginPath();
  ctx.arc(x, y, outer, 0, 2 * Math.PI);
  ctx.fill();
}

function segment(ax, ay, bx, by) {
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.stroke();
}

// Glowing edges and the pulses on their way, added up on a dark background.
function drawWalks(now) {
  ctx.save();
  ctx.globalCompositeOperation = isDark ? "lighter" : "source-over";
  for (const [id, glow] of litLinks) {
    const link = links.get(id);
    const a = nodes.get(link.source), b = nodes.get(link.target);
    if (!a || !b || !visible(a) || !visible(b)) continue;
    const [ax, ay] = toScreen(a), [bx, by] = toScreen(b);
    const strength = Math.min(1, glow.e), color = colors[glow.color];
    ctx.lineWidth = 6;
    ctx.strokeStyle = rgba(color, 0.22 * strength);
    segment(ax, ay, bx, by);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = rgba(color, 0.9 * strength);
    segment(ax, ay, bx, by);
  }
  for (const pulse of pulses) {
    if (!pulse.started || !visible(pulse.from) || !visible(pulse.to)) continue;
    const t = Math.min(1, (now - pulse.t0) / PULSE_MS);
    const [ax, ay] = toScreen(pulse.from), [bx, by] = toScreen(pulse.to);
    const hx = ax + (bx - ax) * t, hy = ay + (by - ay) * t, color = colors[pulse.color];
    if (Math.hypot(hx - ax, hy - ay) > 1) {
      const trail = ctx.createLinearGradient(ax, ay, hx, hy);
      trail.addColorStop(0, rgba(color, 0));
      trail.addColorStop(1, rgba(color, 0.95));
      ctx.lineWidth = 2;
      ctx.strokeStyle = trail;
      segment(ax, ay, hx, hy);
    }
    halo(hx, hy, 1, 9, color, 1);
  }
  ctx.restore();
}

// Glowing entities on top of the others: a halo that grows with the glow, then a lit core.
function drawGlows() {
  ctx.save();
  for (const node of lit) {
    if (!visible(node)) continue;
    const [x, y] = toScreen(node);
    const strength = Math.min(1, node.glow.e), color = colors[node.glow.color];
    ctx.globalCompositeOperation = isDark ? "lighter" : "source-over";
    halo(x, y, node.r * 0.6, node.r + 6 + 16 * strength, color, strength);
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = rgba(color, 0.35 + 0.65 * strength);
    ctx.beginPath();
    ctx.arc(x, y, node.r, 0, 2 * Math.PI);
    ctx.fill();
    if (node.glow.e > 1 && isDark) {
      ctx.fillStyle = rgba("#ffffff", 0.8 * (node.glow.e - 1));
      ctx.beginPath();
      ctx.arc(x, y, node.r * 0.5, 0, 2 * Math.PI);
      ctx.fill();
    }
  }
  ctx.restore();
}

// How much the dots shrink so that a crowded view stays readable: 1 while the dots on screen
// have room, less when zoomed out far enough that they would cover each other and the edges
// between them. Judged by the mean gap between the dots in the area they take on screen.
function dotScale() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  let count = 0, x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const node of nodes.values()) {
    if (!visible(node)) continue;
    const [x, y] = toScreen(node);
    if (x < 0 || y < 0 || x > w || y > h) continue;
    count++;
    x0 = Math.min(x0, x); x1 = Math.max(x1, x);
    y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  if (count < 2) return 1;
  const gap = Math.sqrt(Math.max(x1 - x0, 1) * Math.max(y1 - y0, 1) / count);
  return Math.max(DOT_MIN, Math.min(RADIUS, DOT_SHARE * gap)) / RADIUS;
}

// Sets node.r: the base radius, or with scaling a radius relative to the most connected visible node;
// either one shrinks with dotScale() when the view is crowded.
function updateRadii() {
  const degree = new Map();
  if (scaleNodes.checked) {
    for (const link of links.values()) {
      const a = nodes.get(link.source), b = nodes.get(link.target);
      if (!a || !b || !visible(a) || !visible(b)) continue;
      degree.set(a.id, (degree.get(a.id) || 0) + 1);
      degree.set(b.id, (degree.get(b.id) || 0) + 1);
    }
  }
  let max = 1;
  for (const value of degree.values()) if (value > max) max = value;
  const scale = dotScale();
  for (const node of nodes.values()) {
    node.r = scale * (RADIUS + (MAX_RADIUS - RADIUS) * Math.sqrt((degree.get(node.id) || 0) / max));
  }
}

function draw(now) {
  updateRadii();
  const ratio = devicePixelRatio || 1;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const base = 1 - 0.45 * ambient;  // the rest steps back while reads glow
  ctx.lineWidth = 1;
  ctx.strokeStyle = colors.link;
  ctx.globalAlpha = base;
  ctx.beginPath();
  for (const link of links.values()) {
    const a = nodes.get(link.source), b = nodes.get(link.target);
    if (!a || !b || !visible(a) || !visible(b)) continue;
    const [ax, ay] = toScreen(a), [bx, by] = toScreen(b);
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
  for (const link of [hoverLink, selected && selected.kind === "link" ? links.get(selected.id) : null]) {
    if (!link) continue;
    const a = nodes.get(link.source), b = nodes.get(link.target);
    if (!a || !b || !visible(a) || !visible(b)) continue;
    const [ax, ay] = toScreen(a), [bx, by] = toScreen(b);
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = colors["text-secondary"];
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
  }
  drawWalks(now);
  ctx.lineWidth = 1;
  const labelled = [];
  let count = 0;
  for (const node of nodes.values()) if (visible(node)) count++;
  const showAllLabels = view.k >= 1.4 || count <= LABEL_ALL;
  for (const node of nodes.values()) {
    if (!visible(node)) continue;
    const [x, y] = toScreen(node);
    const dim = query && !matches(node);
    if (node.flash) drawFlash(x, y, node, now);
    ctx.globalAlpha = (dim ? 0.15 : 1) * base;
    ctx.beginPath();
    ctx.arc(x, y, node.r, 0, 2 * Math.PI);
    if (node.status === "active") {
      ctx.fillStyle = colors.node;
      ctx.fill();
    } else if (node.r < 3) {  // too small for a ring: a plain dot in the ring's color
      ctx.fillStyle = colors["node-inactive"];
      ctx.fill();
    } else {
      ctx.fillStyle = colors["surface-1"];
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = colors["node-inactive"];
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    if (selected && selected.kind === "node" && selected.id === node.id) {
      ctx.beginPath();
      ctx.arc(x, y, node.r + 4, 0, 2 * Math.PI);
      ctx.lineWidth = 2;
      ctx.strokeStyle = colors["text-primary"];
      ctx.stroke();
    }
    const shown = query ? !dim : showAllLabels;
    const glowing = node.glow && lit.size <= LABEL_LIT;
    if (shown || glowing || node === hover || node.flash || (selected && selected.id === node.id)) labelled.push([x, y, node]);
  }
  drawGlows();
  ctx.font = "12px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  ctx.textBaseline = "middle";
  for (const [x, y, node] of labelled) {
    const text = node.label.length > 40 ? node.label.slice(0, 39) + "…" : node.label;
    ctx.lineWidth = 3;
    ctx.strokeStyle = colors["surface-1"];
    ctx.strokeText(text, x + node.r + 4, y);
    ctx.fillStyle = node.status === "active" ? colors["text-primary"] : colors["text-secondary"];
    ctx.fillText(text, x + node.r + 4, y);
  }
}

function fit() {
  const list = [...nodes.values()].filter(visible);
  if (!list.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const node of list) {
    x0 = Math.min(x0, node.x); x1 = Math.max(x1, node.x);
    y0 = Math.min(y0, node.y); y1 = Math.max(y1, node.y);
  }
  const pad = 60, labelRoom = 120;
  const k = Math.min(
    (canvas.clientWidth - 2 * pad - labelRoom) / Math.max(x1 - x0, 1),
    (canvas.clientHeight - 2 * pad) / Math.max(y1 - y0, 1),
    2,
  );
  view.k = Math.max(MIN_ZOOM, k);
  view.x = -((x0 + x1) / 2) * view.k - labelRoom / 2;
  view.y = -((y0 + y1) / 2) * view.k;
}

function frame(now) {
  animate(now);
  tick();
  if (autoFit && alpha >= 0.005) fit();
  draw(now);
  requestAnimationFrame(frame);
}

