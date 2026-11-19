"use strict";

// ---- interaction -----------------------------------------------------

const TAP_MS = 300;       // a double tap: two short taps within this time
const TAP_MOVE = 10;      // a tap moves less than this many pixels
const TAP_REACH = 22;     // a finger hits a node within this many pixels, a mouse within 12

// Two fingers on a touch screen pinch to zoom around their midpoint and move to pan; a double
// tap fits. Registered before the surface's own pointer handlers, which leave every pointer
// alone while ``active`` and follow only the pointer their drag started with. ``on.start()``: a
// second finger landed, drop the drag; ``on.move(x, y, factor, dx, dy)``: two fingers moved;
// ``on.rest(id, x, y)``: one finger stays and pans on; ``on.fit()``: a double tap.
function touchGestures(box, on) {
  const points = new Map();  // pointer id -> {x, y, x0, y0, t0} of each finger on the box
  let last = null;           // midpoint and distance of the two fingers at the last move
  let pinched = false;       // this touch has had two fingers: no tap when it ends
  let lastTap = null;
  const at = event => {
    const rect = box.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  };
  const pair = () => {
    const [a, b] = points.values();
    return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, d: Math.max(1, Math.hypot(b.x - a.x, b.y - a.y))};
  };
  box.addEventListener("pointerdown", event => {
    if (event.pointerType !== "touch") return;
    const [x, y] = at(event);
    points.set(event.pointerId, {x, y, x0: x, y0: y, t0: event.timeStamp});
    box.setPointerCapture(event.pointerId);
    if (points.size === 2) { pinched = true; last = pair(); on.start(); }
  });
  box.addEventListener("pointermove", event => {
    const point = points.get(event.pointerId);
    if (!point) return;
    [point.x, point.y] = at(event);
    if (points.size < 2) return;
    const now = pair();
    on.move(now.x, now.y, now.d / last.d, now.x - last.x, now.y - last.y);
    last = now;
  });
  const end = event => {
    const point = points.get(event.pointerId);
    if (!point) return;
    points.delete(event.pointerId);
    if (points.size >= 2) { last = pair(); return; }
    if (points.size === 1) {
      if (pinched) { const [[id, rest]] = points; on.rest(id, rest.x, rest.y); }
      return;
    }
    const tap = !pinched && event.type === "pointerup" && event.timeStamp - point.t0 < TAP_MS
      && Math.hypot(point.x - point.x0, point.y - point.y0) < TAP_MOVE;
    pinched = false;
    if (!tap) { lastTap = null; return; }
    if (lastTap && event.timeStamp - lastTap.t < TAP_MS && Math.hypot(point.x - lastTap.x, point.y - lastTap.y) < 30) {
      lastTap = null;
      on.fit();
    } else lastTap = {x: point.x, y: point.y, t: event.timeStamp};
  };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
  box.addEventListener("gesturestart", event => event.preventDefault());  // Safari's own page zoom
  return {get active() { return points.size >= 2; }};
}

function nodeAt(sx, sy, touch) {
  let best = null, bestD = Infinity;
  for (const node of nodes.values()) {
    if (!visible(node)) continue;
    const [x, y] = toScreen(node);
    const d = (x - sx) ** 2 + (y - sy) ** 2;
    const reach = Math.max(touch ? TAP_REACH : 12, (node.r || RADIUS) + 4);
    if (d < reach * reach && d < bestD) { best = node; bestD = d; }
  }
  return best;
}

function linkAt(sx, sy) {
  let best = null, bestD = 6;
  for (const link of links.values()) {
    const a = nodes.get(link.source), b = nodes.get(link.target);
    if (!a || !b || !visible(a) || !visible(b)) continue;
    const [ax, ay] = toScreen(a), [bx, by] = toScreen(b);
    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, ((sx - ax) * dx + (sy - ay) * dy) / len2)) : 0;
    const d = Math.hypot(sx - (ax + t * dx), sy - (ay + t * dy));
    if (d < bestD) { best = link; bestD = d; }
  }
  return best;
}

function showTooltip(node, sx, sy, link) {
  if (!node && !link) { tooltip.style.display = "none"; return; }
  tooltip.replaceChildren();
  const title = document.createElement("div");
  title.className = "title";
  const meta = document.createElement("div");
  meta.className = "meta";
  if (node) {
    title.textContent = node.label;
    meta.textContent = [node.type, node.alias, node.status === "active" ? null : node.status].filter(Boolean).join(" · ");
  } else {
    const a = nodes.get(link.source), b = nodes.get(link.target);
    title.textContent = link.rel;
    meta.textContent = `${a ? a.label : "?"} → ${b ? b.label : "?"}`;
  }
  tooltip.append(title, meta);
  tooltip.style.display = "block";
  const left = Math.min(sx + 14, canvas.clientWidth - tooltip.offsetWidth - 8);
  tooltip.style.left = Math.max(8, left) + "px";
  tooltip.style.top = Math.max(8, sy + 14) + "px";
}

// Zooms by ``factor`` around the screen point sx, sy.
function zoomAt(sx, sy, factor) {
  const k = Math.min(8, Math.max(MIN_ZOOM, view.k * factor));
  const [wx, wy] = toWorld(sx, sy);
  view.k = k;
  view.x = sx - canvas.clientWidth / 2 - wx * k;
  view.y = sy - canvas.clientHeight / 2 - wy * k;
}

let drag = null;
const canvasTouch = touchGestures(canvas, {
  start() {
    if (drag && drag.node) drag.node.fixed = false;
    drag = null;
    autoFit = false;
    hover = null;
    hoverLink = null;
    showTooltip(null);
  },
  move(x, y, factor, dx, dy) {
    view.x += dx;
    view.y += dy;
    zoomAt(x, y, factor);
  },
  rest(id, x, y) { drag = {id, node: null, x, y, vx: view.x, vy: view.y, moved: true}; },
  fit() { autoFit = true; fit(); },
});
canvas.addEventListener("pointerdown", event => {
  if (canvasTouch.active) return;
  const node = nodeAt(event.offsetX, event.offsetY, event.pointerType === "touch");
  drag = {id: event.pointerId, node, x: event.offsetX, y: event.offsetY, vx: view.x, vy: view.y, moved: false};
  if (!node) autoFit = false;
  if (node) { node.fixed = true; alpha = Math.max(alpha, 0.1); }
  canvas.setPointerCapture(event.pointerId);
  canvas.classList.add("dragging");
});
canvas.addEventListener("pointermove", event => {
  if (canvasTouch.active || (drag && event.pointerId !== drag.id)) return;
  if (drag && Math.hypot(event.offsetX - drag.x, event.offsetY - drag.y) > 4) drag.moved = true;
  if (drag && drag.node) {
    [drag.node.x, drag.node.y] = toWorld(event.offsetX, event.offsetY);
    alpha = Math.max(alpha, 0.1);
  } else if (drag) {
    view.x = drag.vx + event.offsetX - drag.x;
    view.y = drag.vy + event.offsetY - drag.y;
  }
  hover = nodeAt(event.offsetX, event.offsetY);
  hoverLink = hover ? null : linkAt(event.offsetX, event.offsetY);
  showTooltip(hover, event.offsetX, event.offsetY, hoverLink);
});
function endDrag(event) {
  if (drag && event.pointerId !== drag.id) return;
  if (drag && drag.node) drag.node.fixed = false;
  if (drag && !drag.moved && event.type === "pointerup") {
    const link = drag.node ? null : linkAt(event.offsetX, event.offsetY);
    if (drag.node) select({kind: "node", id: drag.node.id});
    else if (link) select({kind: "link", id: link.id});
    else if (selected) select(null);
  }
  drag = null;
  canvas.classList.remove("dragging");
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
canvas.addEventListener("pointerleave", () => { hover = null; hoverLink = null; showTooltip(null); });
canvas.addEventListener("wheel", event => {
  event.preventDefault();
  autoFit = false;
  zoomAt(event.offsetX, event.offsetY, Math.exp(-event.deltaY * 0.0015));
}, {passive: false});
canvas.addEventListener("dblclick", () => { autoFit = true; fit(); });
showInactive.addEventListener("change", refilter);
onlyMatches.addEventListener("change", refilter);
scaleNodes.addEventListener("change", () => { alpha = Math.max(alpha, 0.3); });
document.getElementById("search").addEventListener("input", event => {
  query = event.target.value.trim().toLowerCase();
  refilter();
});
document.getElementById("search").addEventListener("keydown", event => {
  if (event.key !== "Enter" || !query) return;
  const first = [...nodes.values()].find(n => visible(n) && matches(n));
  if (first) select({kind: "node", id: first.id});
});
addEventListener("resize", resize);
new ResizeObserver(resize).observe(canvas);

narrow.addEventListener("change", fitToScreen);
fitToScreen();
foldPanel(storedPanel() === "folded", false);
resize();
// A graph named in the URL opens at once; else the list says which one --graph named, and
// without one the list opens, on a narrow screen too: there is nothing else to show.
if (new URLSearchParams(location.search).get("graph")) {
  showGraph(wantedGraph(), wantedTab(), wantedItem());
  loadGraphs();
} else {
  showGraph(null);
  loadGraphs().then(() => {
    if (current) return;
    if (defaultGraph) showGraph(defaultGraph);
    else setSidebar(true, false);
  });
}
requestAnimationFrame(frame);
