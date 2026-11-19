"use strict";

// ---- details ---------------------------------------------------------

const detail = document.getElementById("detail");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function when(ts) {
  const date = new Date(ts);
  return isNaN(date) ? ts : date.toLocaleString();
}

function url(path, id) {
  return api(path, {id});
}

function size(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KiB";
  return (bytes / 1048576).toFixed(1) + " MiB";
}

function show(value) {
  if (value === null || value === undefined) return "–";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function selectedSource() {
  if (!selected || selected.kind !== "link") return null;
  const link = links.get(selected.id);
  return link ? link.source : null;
}

function select(target) {
  selected = target;
  if (target && narrow.matches) setPanel(true);
  if (target && panel.classList.contains("folded")) foldPanel(false, false);
  showSelection();
}

function nodeLink(id, label) {
  const button = el("button", "link", label);
  button.type = "button";
  button.addEventListener("click", () => select({kind: "node", id}));
  return button;
}

function qualifiers(values) {
  const keys = Object.keys(values || {});
  return keys.length ? keys.map(k => `${k}: ${show(values[k])}`).join(", ") : "";
}

function closeButton() {
  const button = el("button", "close", "×");
  button.type = "button";
  button.setAttribute("aria-label", "Close details");
  button.addEventListener("click", () => select(null));
  return button;
}

function actorText(actor) {
  if (!actor) return "unknown";
  const parts = [];
  for (const name of ["agent", "principal"]) {
    const facet = actor[name];
    if (facet) parts.push(`${name} ${facet.name || ""}${facet.assurance === "asserted" ? " (asserted)" : ""}`.trim());
  }
  if (actor.roles) parts.push(`roles ${actor.roles.join(", ")}`);
  return parts.length ? parts.join(" · ") : actor.label;
}

function historySection(history) {
  const box = el("div");
  box.append(el("h4", null, "History and agents"));
  const list = el("ul");
  for (const item of history) {
    const row = el("li");
    row.append(el("div", null, `rev ${item.rev} · ${item.status} · ${when(item.ts)}`));
    row.append(el("div", "meta", actorText(item.actor)));
    const how = [item.actor && item.actor.session ? `session ${item.actor.session.name || ""}` : null,
      item.actor ? item.actor.client : null, item.command].filter(Boolean).join(" · ");
    if (how) row.append(el("div", "small", how));
    if (item.reason) row.append(el("div", "small", `reason: ${item.reason}`));
    list.append(row);
  }
  box.append(list);
  return box;
}

function edgeList(title, items, outgoing) {
  const box = el("div");
  box.append(el("h4", null, `${title} (${items.length})`));
  if (!items.length) { box.append(el("div", "small", "none")); return box; }
  const list = el("ul");
  for (const item of items) {
    const row = el("li");
    const rel = el("span", "secondary", outgoing ? `${item.rel} → ` : `← ${item.rel} · `);
    row.append(rel, nodeLink(item.other.id, item.other.label));
    const extra = qualifiers(item.qualifiers);
    if (extra) row.append(el("div", "small", extra));
    list.append(row);
  }
  box.append(list);
  return box;
}

function fileSection(d) {
  const box = el("div");
  box.append(el("h4", null, "File"));
  const info = d.file;
  box.append(el("div", "meta", [info.media_type, size(info.size)].filter(Boolean).join(" · ")));
  if (info.stored === null) { box.append(el("div", "small", "The stored copy cannot be checked now: its object store is unavailable.")); return box; }
  if (!info.stored) { box.append(el("div", "small", "The stored copy is missing.")); return box; }
  const actions = el("div", "actions");
  if (info.inline) {
    const preview = el("button", null, "Preview");
    preview.type = "button";
    preview.addEventListener("click", () => openPreview(d));
    actions.append(preview);
  }
  const open = el("a", "button", info.inline ? "Open in tab" : "Download");
  open.href = url("blob", d.id);
  open.target = "_blank";
  open.rel = "noopener";
  actions.append(open);
  box.append(actions);
  return box;
}

function renderNode(d) {
  detail.replaceChildren(closeButton(), el("h3", null, d.label));
  const meta = el("div", "meta");
  const type = el("button", "link", d.type);
  type.type = "button";
  type.title = "Describe this type";
  type.addEventListener("click", () => chooseTab("model", d.type));
  meta.append(type, [" ", d.alias, d.status === "active" ? null : d.status, `rev ${d.rev}`].filter(Boolean).join(" · "));
  detail.append(meta);
  detail.append(el("div", "small", `${d.id}`));
  if (d.file) detail.append(fileSection(d));
  detail.append(el("h4", null, "Attributes"));
  const keys = Object.keys(d.attrs);
  if (!keys.length) detail.append(el("div", "small", "none"));
  else {
    const list = el("dl");
    for (const key of keys) list.append(el("dt", null, key), el("dd", null, show(d.attrs[key])));
    detail.append(list);
  }
  const valid = [d.valid_from ? `from ${d.valid_from}` : null, d.valid_to ? `to ${d.valid_to}` : null].filter(Boolean);
  if (valid.length) detail.append(el("div", "small", `valid ${valid.join(" ")}`));
  detail.append(edgeList("Outgoing edges", d.outgoing, true), edgeList("Incoming edges", d.incoming, false), historySection(d.history));
}

function renderLink(link, source) {
  const a = nodes.get(link.source), b = nodes.get(link.target);
  detail.replaceChildren(closeButton(), el("h3", null, link.rel));
  detail.append(el("div", "meta", "edge"));
  const list = el("dl");
  const from = nodeLink(link.source, a ? a.label : link.source);
  const to = nodeLink(link.target, b ? b.label : link.target);
  const fromDd = el("dd"), toDd = el("dd");
  fromDd.append(from); toDd.append(to);
  list.append(el("dt", null, "from"), fromDd, el("dt", null, "to"), toDd);
  const values = link.qualifiers || {};
  for (const key of Object.keys(values)) list.append(el("dt", null, key), el("dd", null, show(values[key])));
  detail.append(list, el("div", "small", link.id));
  if (source) {
    detail.append(el("h4", null, "Written by (history of the source entity)"));
    detail.append(historySection(source.history).lastChild);
  }
}

async function fetchEntity(id) {
  const response = await fetch(url("entity", id), {cache: "no-store"});
  if (!response.ok) throw new Error(await problem(response));
  return response.json();
}

function closePreview() {
  document.getElementById("preview").style.display = "none";
  document.getElementById("preview_body").replaceChildren();
}

function openPreview(d) {
  const node = document.createElement("img");  // images only: the server offers nothing else inline
  node.src = url("blob", d.id);
  node.alt = d.label;
  document.getElementById("preview_body").replaceChildren(node);
  document.getElementById("preview_name").textContent = d.label;
  document.getElementById("preview").style.display = "flex";
}

async function showSelection() {
  const target = selected;
  closePreview();
  if (!target) { detail.hidden = true; return; }
  detail.hidden = false;
  try {
    if (target.kind === "node") {
      const d = await fetchEntity(target.id);
      if (selected === target) renderNode(d);
    } else {
      const link = links.get(target.id);
      if (!link) { select(null); return; }
      const source = await fetchEntity(link.source).catch(() => null);
      if (selected === target) renderLink(link, source);
    }
  } catch (error) {
    if (selected === target) {
      detail.replaceChildren(closeButton(), el("div", "muted", `cannot load the details: ${error.message}`));
    }
  }
}

document.getElementById("preview_close").addEventListener("click", closePreview);
addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (tab === "types" && tg && (tg.selected || tg.selectedEdge)) go(null, false);
  else if (document.getElementById("preview").style.display === "flex") closePreview();
  else if (selected) select(null);
  else if (narrow.matches && !sidebar.classList.contains("collapsed")) { setSidebar(false, false); sidebarToggle.focus(); }
  else if (narrow.matches && !panel.classList.contains("minimized")) setPanel(false);
});

