"use strict";

// ---- views: type graph -----------------------------------------------------

function svgEl(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  if (parent) parent.append(node);
  return node;
}

function arrowMarker(defs, id, className) {
  const marker = svgEl("marker", {id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "9", markerHeight: "9", markerUnits: "userSpaceOnUse", orient: "auto"}, defs);
  svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", class: className}, marker);
}

// Types as dots (colored by module, sized by active entities, hollow when abstract), relations as
// arrows from domain to range (one per pair of types, named by its relations), dashed arrows to
// the type a type extends. Relations open to any type have no end to draw and are listed instead.
function buildTypeGraph() {
  tg = {
    nodes: new Map(), edges: [], open: [], view: {x: 0, y: 0, k: 1}, alpha: 1, autoFit: true,
    hiddenModules: new Set(), selected: null, selectedEdge: null, hover: null, hoverEdge: null,
    query: "", inherit: true, sizes: true, dirty: true, stopped: false, drag: null,
  };
  const modules = model.modules.filter(m => m.types > 0);
  const spread = modules.length > 1 ? 140 + 30 * modules.length : 0;
  modules.forEach((m, index) => {
    const angle = 2 * Math.PI * index / modules.length;
    for (const t of Object.values(model.type).filter(type => type.module === m.id)) {
      tg.nodes.set(t.curie, {
        id: t.curie, module: t.module, abstract: t.abstract, count: model.entities[t.curie] || 0,
        x: Math.cos(angle) * spread + (Math.random() - 0.5) * 80, y: Math.sin(angle) * spread + (Math.random() - 0.5) * 80,
        vx: 0, vy: 0, fixed: false, r: 7,
      });
    }
  });
  const pairs = new Map();
  for (const r of model.relations) {
    if (!r.domain.length || !r.range.length) { tg.open.push(r); continue; }
    for (const from of r.domain) for (const to of r.range) {
      if (!tg.nodes.has(from) || !tg.nodes.has(to)) continue;
      const key = from + " " + to;
      if (!pairs.has(key)) pairs.set(key, {kind: "relation", source: from, target: to, rels: []});
      pairs.get(key).rels.push(r.curie);
    }
  }
  tg.edges = [...pairs.values()];
  for (const t of Object.values(model.type)) {
    if (t.extends && tg.nodes.has(t.extends)) tg.edges.push({kind: "extends", source: t.curie, target: t.extends, rels: []});
  }
  const box = svgEl("svg", {id: "typegraph", role: "img", "aria-label": `Type graph of ${browseGraph.name}: types as dots, relations as arrows`});
  const defs = svgEl("defs", {}, box);
  arrowMarker(defs, "tg_arrow", "arrow");
  arrowMarker(defs, "tg_arrow_hi", "arrow hi");
  arrowMarker(defs, "tg_extends", "arrow open");
  const edgeLayer = svgEl("g", {}, box), nodeLayer = svgEl("g", {}, box);
  tg.edges.forEach((edge, index) => {
    edge.id = "e" + index;
    edge.g = svgEl("g", {class: edge.kind === "extends" ? "edge extends" : "edge"}, edgeLayer);
    edge.g.dataset.edge = edge.id;
    edge.line = svgEl("path", {class: "line", "marker-end": edge.kind === "extends" ? "url(#tg_extends)" : "url(#tg_arrow)"}, edge.g);
    edge.hit = svgEl("path", {class: "hit"}, edge.g);
    edge.text = svgEl("text", {"text-anchor": "middle", dy: "-3"}, edge.g);
    edge.text.textContent = edge.kind === "extends" ? "extends" : edge.rels.map(typeName).join(", ");
  });
  for (const node of tg.nodes.values()) {
    node.g = svgEl("g", {class: node.abstract ? "node abstract" : "node"}, nodeLayer);
    node.g.dataset.node = node.id;
    node.circle = svgEl("circle", {r: node.r}, node.g);
    node.circle.style.fill = moduleColor(node.module);
    if (node.abstract) node.circle.style.stroke = moduleColor(node.module);
    node.text = svgEl("text", {dy: "0.35em"}, node.g);
    node.text.textContent = typeName(node.id);
  }
  tg.box = box;
  tg.tooltip = el("div");
  tg.tooltip.id = "tg_tooltip";
  tg.tooltip.setAttribute("role", "tooltip");
  browseMain.replaceChildren(box, tg.tooltip);
  tgRadii();
  tgEvents(box);
  buildTypesNav();
  const self = tg;
  requestAnimationFrame(() => tgFrame(self));
}

const TYPES_OPTIONS_KEY = "graph-view.types-options";

// A narrow screen folds the type graph's options below its search, at first and as the user
// left them; a wide one always shows them.
function typesOptionsOpen() {
  try { return localStorage.getItem(TYPES_OPTIONS_KEY) === "open"; } catch (error) { return false; }
}

function buildTypesNav() {
  browseNav.dataset.built = "types";
  browseNav.replaceChildren();
  const search = el("input");
  search.type = "search";
  search.placeholder = "Search types";
  search.value = tg.query;
  search.setAttribute("aria-label", "Search types");
  search.addEventListener("input", () => {
    tg.query = search.value.trim().toLowerCase();
    for (const node of tg.nodes.values()) node.g.classList.toggle("dim", Boolean(tg.query) && !tgMatches(node));
  });
  search.addEventListener("keydown", event => {
    if (event.key !== "Enter" || !tg.query) return;
    const first = [...tg.nodes.values()].find(node => tgMatches(node));
    if (first) go(first.id, false);
  });
  const fold = el("button");
  fold.type = "button";
  fold.id = "types_fold";
  fold.setAttribute("aria-controls", "types_options");
  const options = el("div");
  options.id = "types_options";
  const chevron = svgEl("svg", {viewBox: "0 0 12 12", "aria-hidden": "true"});
  svgEl("polyline", {points: "2.5,4.5 6,8 9.5,4.5"}, chevron);
  // the button names the hidden modules, so a folded filter does not go unnoticed
  const label = () => {
    const hidden = tg.hiddenModules.size;
    fold.replaceChildren(hidden ? `Options · ${hidden} hidden` : "Options", chevron);
  };
  const setOpen = (open, remember) => {
    options.classList.toggle("folded", !open);
    fold.setAttribute("aria-expanded", String(open));
    if (remember) {
      try { localStorage.setItem(TYPES_OPTIONS_KEY, open ? "open" : "folded"); } catch (error) { /* storage off */ }
    }
  };
  fold.addEventListener("click", () => setOpen(options.classList.contains("folded"), true));
  label();
  setOpen(typesOptionsOpen(), false);
  const bar = el("div", "bar");
  bar.append(search, fold);
  browseNav.append(bar, options);
  options.append(el("h2", null, "Modules"));
  for (const m of model.modules.filter(x => x.types > 0)) {
    const row = el("label");
    const input = el("input");
    input.type = "checkbox";
    input.checked = !tg.hiddenModules.has(m.id);
    input.addEventListener("change", () => {
      if (input.checked) tg.hiddenModules.delete(m.id); else tg.hiddenModules.add(m.id);
      label();
      tg.alpha = Math.max(tg.alpha, 0.3);
      tg.autoFit = true;
      tg.dirty = true;
    });
    row.append(input, swatch(m.id), el("span", null, m.id), el("span", "count", String(m.types)));
    options.append(row);
  }
  options.append(el("h2", null, "Show"));
  const toggle = (text, checked, onChange) => {
    const row = el("label");
    const input = el("input");
    input.type = "checkbox";
    input.checked = checked;
    input.addEventListener("change", () => { onChange(input.checked); tg.dirty = true; });
    row.append(input, text);
    options.append(row);
  };
  toggle("inheritance (extends)", tg.inherit, on => { tg.inherit = on; tg.alpha = Math.max(tg.alpha, 0.3); });
  toggle("size by active entities", tg.sizes, on => { tg.sizes = on; tgRadii(); });
  toggle("relation names", tg.box.classList.contains("names"), on => tg.box.classList.toggle("names", on));
  options.append(el("div", "note muted", "Arrows run from the type a relation starts at to the type it points to; dashed arrows to the type a type extends. Hollow dots are abstract types. Drag to pan, wheel or pinch to zoom, double-click or double-tap to fit."));
  if (tg.open.length) {
    const note = el("div", "note muted", "Not drawn, open to any type: ");
    tg.open.forEach((r, index) => note.append(index ? ", " : "", code(r.curie)));
    options.append(note);
  }
}

function tgMatches(node) {
  return node.id.toLowerCase().includes(tg.query);
}

function tgVisible(node) {
  return !tg.hiddenModules.has(node.module);
}

function tgEdgeVisible(edge) {
  return (edge.kind !== "extends" || tg.inherit) && tgVisible(tg.nodes.get(edge.source)) && tgVisible(tg.nodes.get(edge.target));
}

function tgRadii() {
  const max = Math.max(1, ...[...tg.nodes.values()].map(node => node.count));
  for (const node of tg.nodes.values()) {
    node.r = node.abstract ? 8 : tg.sizes ? 6 + 11 * Math.sqrt(node.count / max) : 7;
    node.circle.setAttribute("r", node.r);
    node.text.setAttribute("x", node.r + 4);
  }
}

function tgTick() {
  if (tg.alpha < 0.004) return false;
  const alpha = tg.alpha;
  const list = [...tg.nodes.values()].filter(tgVisible);
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const a = list[i], b = list[j];
      let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
      const force = (-2600 * alpha) / d2;
      a.vx += dx * force; a.vy += dy * force;
      b.vx -= dx * force; b.vy -= dy * force;
    }
  }
  for (const edge of tg.edges) {
    const a = tg.nodes.get(edge.source), b = tg.nodes.get(edge.target);
    if (a === b || !tgEdgeVisible(edge)) continue;
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const force = ((d - (edge.kind === "extends" ? 80 : 130)) / d) * 0.05 * alpha;
    a.vx += dx * force; a.vy += dy * force;
    b.vx -= dx * force; b.vy -= dy * force;
  }
  // each module holds together a little, so its types stay a cluster
  const centers = new Map();
  for (const node of list) {
    const c = centers.get(node.module) || {x: 0, y: 0, n: 0};
    c.x += node.x; c.y += node.y; c.n++;
    centers.set(node.module, c);
  }
  for (const node of list) {
    const c = centers.get(node.module);
    node.vx += (c.x / c.n - node.x) * 0.02 * alpha - node.x * 0.004 * alpha;
    node.vy += (c.y / c.n - node.y) * 0.02 * alpha - node.y * 0.004 * alpha;
    if (node.fixed) { node.vx = node.vy = 0; continue; }
    node.vx *= 0.6; node.vy *= 0.6;
    const speed = Math.hypot(node.vx, node.vy);
    if (speed > 30) { node.vx *= 30 / speed; node.vy *= 30 / speed; }
    node.x += node.vx; node.y += node.vy;
  }
  tg.alpha *= 0.98;
  return true;
}

function tgScreen(node) {
  return [browseMain.clientWidth / 2 + tg.view.x + node.x * tg.view.k, browseMain.clientHeight / 2 + tg.view.y + node.y * tg.view.k];
}

function tgWorld(sx, sy) {
  return [(sx - browseMain.clientWidth / 2 - tg.view.x) / tg.view.k, (sy - browseMain.clientHeight / 2 - tg.view.y) / tg.view.k];
}

function toward(x, y, tx, ty, dist) {
  const dx = tx - x, dy = ty - y, len = Math.hypot(dx, dy) || 1;
  return [x + dx / len * dist, y + dy / len * dist];
}

// Screen coordinates, so text, lines and arrows keep their size at every zoom.
function tgRender() {
  for (const node of tg.nodes.values()) {
    const shown = tgVisible(node);
    node.g.style.display = shown ? "" : "none";
    if (!shown) continue;
    const [x, y] = tgScreen(node);
    node.g.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
  }
  for (const edge of tg.edges) {
    const shown = tgEdgeVisible(edge);
    edge.g.style.display = shown ? "" : "none";
    if (!shown) continue;
    const a = tg.nodes.get(edge.source), b = tg.nodes.get(edge.target);
    const [ax, ay] = tgScreen(a), [bx, by] = tgScreen(b);
    let path, lx, ly;
    if (a === b) {  // a relation from a type to itself: a loop above it
      const r = a.r;
      path = `M ${ax - r * 0.7} ${ay - r * 0.7} C ${ax - 30} ${ay - r - 40}, ${ax + 30} ${ay - r - 40}, ${ax + r * 0.7} ${ay - r * 0.7}`;
      lx = ax; ly = ay - r - 30;
    } else {
      // relations bend to one side of their direction, so two in opposite directions stay apart
      const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
      const bend = edge.kind === "extends" ? 0 : Math.min(40, 0.12 * len);
      const cx = (ax + bx) / 2 - dy / len * bend, cy = (ay + by) / 2 + dx / len * bend;
      const [sx, sy] = toward(ax, ay, cx, cy, a.r + 1), [ex, ey] = toward(bx, by, cx, cy, b.r + 2);
      path = `M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}`;
      lx = 0.25 * sx + 0.5 * cx + 0.25 * ex; ly = 0.25 * sy + 0.5 * cy + 0.25 * ey;
    }
    edge.line.setAttribute("d", path);
    edge.hit.setAttribute("d", path);
    edge.text.setAttribute("x", lx.toFixed(1));
    edge.text.setAttribute("y", ly.toFixed(1));
  }
}

function tgFit() {
  const list = [...tg.nodes.values()].filter(tgVisible);
  const width = browseMain.clientWidth, height = browseMain.clientHeight;
  if (!list.length || !width || !height) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const node of list) {
    x0 = Math.min(x0, node.x); x1 = Math.max(x1, node.x);
    y0 = Math.min(y0, node.y); y1 = Math.max(y1, node.y);
  }
  const pad = 50, labelRoom = 110;
  const k = Math.min((width - 2 * pad - labelRoom) / Math.max(x1 - x0, 1), (height - 2 * pad) / Math.max(y1 - y0, 1), 1.5);
  tg.view.k = Math.max(0.05, k);
  tg.view.x = -((x0 + x1) / 2) * tg.view.k - labelRoom / 2;
  tg.view.y = -((y0 + y1) / 2) * tg.view.k;
}

function tgFrame(self) {
  if (self !== tg || self.stopped) return;
  const moved = tgTick();
  if (moved || tg.dirty) {
    if (tg.autoFit) tgFit();
    tgRender();
    tg.dirty = false;
  }
  requestAnimationFrame(() => tgFrame(self));
}

// The selected or hovered type with its relations and neighbours, or the selected or hovered edge
// with its ends, stands out; everything else steps back.
function tgHighlight() {
  const node = tg.hover || (tg.selected && tg.nodes.get(tg.selected));
  const edge = !node && (tg.hoverEdge || tg.edges.find(e => e.id === tg.selectedEdge));
  const lit = new Set(), litEdges = new Set();
  if (node) {
    lit.add(node.id);
    for (const e of tg.edges) {
      if (!tgEdgeVisible(e) || (e.source !== node.id && e.target !== node.id)) continue;
      litEdges.add(e);
      lit.add(e.source);
      lit.add(e.target);
    }
  } else if (edge) {
    litEdges.add(edge);
    lit.add(edge.source);
    lit.add(edge.target);
  }
  tg.box.classList.toggle("focus", lit.size > 0);
  for (const n of tg.nodes.values()) {
    n.g.classList.toggle("hi", lit.has(n.id));
    n.g.classList.toggle("selected", n.id === tg.selected);
  }
  for (const e of tg.edges) {
    const on = litEdges.has(e);
    e.g.classList.toggle("hi", on);
    e.g.classList.toggle("selected", e.id === tg.selectedEdge);
    if (e.kind !== "extends") e.line.setAttribute("marker-end", on ? "url(#tg_arrow_hi)" : "url(#tg_arrow)");
  }
}

function sideDoc() {
  const doc = el("div", "doc");
  const close = el("button", "close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close the selection");
  close.addEventListener("click", () => go(null, false));
  doc.append(close);
  browseSide.replaceChildren(doc);
  browseSide.hidden = false;
  browseSide.scrollTop = 0;
  return doc;
}

function tgSelect(curie) {
  const node = curie ? tg.nodes.get(curie) : null;
  tg.selected = node ? node.id : null;
  tg.selectedEdge = null;
  if (node && tg.hiddenModules.has(node.module)) {
    tg.hiddenModules.delete(node.module);
    buildTypesNav();
    tg.alpha = Math.max(tg.alpha, 0.3);
  }
  if (node) typeDoc(sideDoc(), model.type[node.id]);
  else if (curie) sideDoc().append(el("p", "muted", `${curie} is not a type of this ontology.`));
  else { browseSide.hidden = true; browseSide.replaceChildren(); }
  tgHighlight();
  tg.dirty = true;
}

function tgSelectEdge(edge) {
  if (item) {
    const params = new URLSearchParams(location.search);
    params.delete("item");
    history.replaceState(null, "", "?" + params);
    item = null;
  }
  tg.selected = null;
  tg.selectedEdge = edge.id;
  const doc = sideDoc();
  if (edge.kind === "extends") {
    doc.append(el("h1", null, "extends"));
    doc.append(frag(typeLink(edge.source), " extends ", typeLink(edge.target)));
  } else {
    for (const curie of edge.rels) {
      const r = model.relations.find(x => x.curie === curie);
      doc.append(el("h1", null, typeName(r.curie)), el("div", "meta", r.curie));
      if (r.summary) doc.append(el("p", "lead", r.summary));
      const list = el("dl");
      const row = (term, value) => {
        const dd = el("dd");
        dd.append(value);
        list.append(el("dt", null, term), dd);
      };
      row("from", typeLinks(r.domain));
      row("to", typeLinks(r.range));
      row("cardinality", `${r.cardinality.out} out · ${r.cardinality.in} in`);
      if (r.characteristics.length) row("characteristics", r.characteristics.join(", "));
      if (r.qualifiers.length) row("qualifiers", r.qualifiers.join(", "));
      if (r.inverse_label) row("inverse", r.inverse_label);
      row("target inactive", r.on_target_inactive);
      if (r.deprecated) row("status", badge("deprecated", true));
      doc.append(list);
    }
  }
  tgHighlight();
  tg.dirty = true;
}

function tgTooltip(sx, sy) {
  const tip = tg.tooltip;
  const node = tg.hover, edge = tg.hoverEdge;
  if (!node && !edge) { tip.style.display = "none"; return; }
  tip.replaceChildren();
  if (node) {
    const t = model.type[node.id];
    tip.append(el("div", "title", typeName(node.id)));
    tip.append(el("div", "meta", `${node.id} · ${node.module}${t.abstract ? " · abstract" : ""} · ${entityCount(node.id)} active`));
    if (t.summary) tip.append(el("div", null, t.summary));
  } else {
    tip.append(el("div", "title", edge.kind === "extends" ? "extends" : edge.rels.join(", ")));
    tip.append(el("div", "meta", `${typeName(edge.source)} → ${typeName(edge.target)}`));
  }
  tip.style.display = "block";
  tip.style.left = Math.max(8, Math.min(sx + 14, browseMain.clientWidth - tip.offsetWidth - 8)) + "px";
  tip.style.top = Math.max(8, sy + 14) + "px";
}

// Zooms the type graph by ``factor`` around the screen point sx, sy.
function tgZoomAt(sx, sy, factor) {
  const [wx, wy] = tgWorld(sx, sy);
  tg.view.k = Math.min(6, Math.max(0.05, tg.view.k * factor));
  tg.view.x = sx - browseMain.clientWidth / 2 - wx * tg.view.k;
  tg.view.y = sy - browseMain.clientHeight / 2 - wy * tg.view.k;
  tg.dirty = true;
}

function tgEvents(box) {
  const point = event => {
    const rect = box.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  };
  const targetOf = event => {
    const nodeG = event.target.closest && event.target.closest("[data-node]");
    if (nodeG) return {node: tg.nodes.get(nodeG.dataset.node)};
    const edgeG = event.target.closest && event.target.closest("[data-edge]");
    if (edgeG) return {edge: tg.edges.find(e => e.id === edgeG.dataset.edge)};
    return {};
  };
  const touch = touchGestures(box, {
    start() {
      if (tg.drag && tg.drag.node) tg.drag.node.fixed = false;
      tg.drag = null;
      tg.autoFit = false;
      tg.hover = null;
      tg.hoverEdge = null;
      tgHighlight();
      tgTooltip(0, 0);
    },
    move(x, y, factor, dx, dy) {
      tg.view.x += dx;
      tg.view.y += dy;
      tgZoomAt(x, y, factor);
    },
    rest(id, x, y) { tg.drag = {id, sx: x, sy: y, vx: tg.view.x, vy: tg.view.y, moved: true}; },
    fit() { tg.autoFit = true; tg.dirty = true; },
  });
  box.addEventListener("pointerdown", event => {
    if (touch.active) return;
    const [sx, sy] = point(event);
    const {node, edge} = targetOf(event);
    tg.drag = {id: event.pointerId, node, edge, sx, sy, vx: tg.view.x, vy: tg.view.y, moved: false};
    if (node) { node.fixed = true; tg.alpha = Math.max(tg.alpha, 0.1); } else tg.autoFit = false;
    box.setPointerCapture(event.pointerId);
    box.classList.add("dragging");
  });
  box.addEventListener("pointermove", event => {
    const [sx, sy] = point(event);
    const drag = tg.drag;
    if (touch.active || (drag && event.pointerId !== drag.id)) return;
    if (drag) {
      if (Math.hypot(sx - drag.sx, sy - drag.sy) > 4) drag.moved = true;
      if (!drag.moved) return;
      if (drag.node) {
        [drag.node.x, drag.node.y] = tgWorld(sx, sy);
        tg.alpha = Math.max(tg.alpha, 0.1);
        tg.autoFit = false;
      } else {
        tg.view.x = drag.vx + sx - drag.sx;
        tg.view.y = drag.vy + sy - drag.sy;
      }
      tg.dirty = true;
      return;
    }
    const {node, edge} = targetOf(event);
    if (node !== tg.hover || edge !== tg.hoverEdge) {
      tg.hover = node || null;
      tg.hoverEdge = edge || null;
      tgHighlight();
    }
    tgTooltip(sx, sy);
  });
  const end = event => {
    const drag = tg.drag;
    if (drag && event.pointerId !== drag.id) return;
    tg.drag = null;
    box.classList.remove("dragging");
    if (!drag) return;
    if (drag.node) drag.node.fixed = false;
    if (drag.moved || event.type !== "pointerup") return;
    if (drag.node) go(drag.node.id, false);
    else if (drag.edge) tgSelectEdge(drag.edge);
    else if (tg.selected || tg.selectedEdge) go(null, false);
  };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
  box.addEventListener("pointerleave", () => {
    tg.hover = null;
    tg.hoverEdge = null;
    tgHighlight();
    tgTooltip(0, 0);
  });
  box.addEventListener("wheel", event => {
    event.preventDefault();
    tg.autoFit = false;
    tgZoomAt(...point(event), Math.exp(-event.deltaY * 0.0015));
  }, {passive: false});
  box.addEventListener("dblclick", () => { tg.autoFit = true; tg.dirty = true; });
}

new ResizeObserver(() => { if (tg) tg.dirty = true; }).observe(browseMain);

