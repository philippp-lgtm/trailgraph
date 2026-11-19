"use strict";

// ---- views: model, type graph, functions -----------------------------

const TABS = [["types", "Type graph"], ["model", "Model"], ["functions", "Functions"], ["policies", "Policies"], ["graph", "Graph"]];
const MODULE_COLORS = 8;  // the categorical palette --m1 .. --m8 in fixed order; further modules stay neutral
const SVG_NS = "http://www.w3.org/2000/svg";
let tab = "graph";        // the view of the shown graph, a key of TABS; "graph" while none is shown
let item = null;          // what the view shows: a module or type (model, type graph) or a function
let focusTypes = null;    // the types "show in graph" leaves visible once the graph has loaded
let model = null;         // the /model answer of the shown graph
let functionList = null;  // the functions of the /functions answer
let policies = null;      // the /policies answer of the shown graph
let browseGraph = null;   // {id, name} of the graph the browse answers are about
let tg = null;            // the type graph: nodes, edges, view, selection
const tabbar = document.getElementById("tabbar");
const browseNav = document.getElementById("browse_nav");
const browseMain = document.getElementById("browse_main");
const browseSide = document.getElementById("browse_side");

function tabLabel(key) {
  return TABS.find(([k]) => k === key)[1];
}

function tabButtons() {
  return TABS.map(([key, label]) => {
    const button = el("button", null, label);
    button.type = "button";
    button.dataset.tab = key;
    if (key === tab) button.setAttribute("aria-current", "page");
    // the shown view picked again returns to its start; on a narrow screen it folds the list
    button.addEventListener("click", () => {
      if (key !== tab || item) chooseTab(key);
      else if (narrow.matches) setSidebar(false, false);
    });
    return button;
  });
}

// The views of the shown graph: in its folder in the side bar, and in the bar at the top
// while the side bar is folded.
function renderTabs() {
  const active = document.activeElement;
  const focused = active && active.dataset ? active.dataset.tab : null;
  const where = focused && active.closest("#tabbar") ? tabbar : viewList;
  tabbar.replaceChildren(...(current ? tabButtons() : []));
  const graph = graphList.find(isCurrent);
  const name = document.getElementById("folder_name");
  name.replaceChildren(graph ? graph.name : current || "");
  if (graph && graph.state !== "active") name.append(el("span", "state", graph.state));
  document.getElementById("folder_title").textContent = graph ? graph.title || "" : "";
  viewList.setAttribute("aria-label", `Views of ${current}`);
  viewList.replaceChildren(...(current ? tabButtons() : []).map(button => {
    const row = el("li");
    row.append(button);
    return row;
  }));
  showFolder();
  if (focused) {
    const again = where.querySelector(`button[data-tab="${focused}"]`);
    if (again) again.focus();
  }
}

// A view picked: a new history entry, so Back returns to the previous one.
function chooseTab(key, target) {
  const params = new URLSearchParams(location.search);
  params.set("graph", current);
  if (key === "types") params.delete("tab"); else params.set("tab", key);
  if (target) params.set("item", target); else params.delete("item");
  history.pushState(null, "", "?" + params);
  if (narrow.matches) setSidebar(false, false);
  showGraph(current, key, target);
}

// An item picked inside the shown view; ``push`` adds a history entry, else the address follows.
function go(target, push = true) {
  const params = new URLSearchParams(location.search);
  if (target) params.set("item", target); else params.delete("item");
  if (push && (target || null) !== item) history.pushState(null, "", "?" + params);
  else history.replaceState(null, "", "?" + params);
  showItem(target);
}

function resetBrowse() {
  model = null;
  functionList = null;
  policies = null;
  browseGraph = null;
  if (tg) tg.stopped = true;
  tg = null;
  delete browseNav.dataset.built;
  browseNav.replaceChildren();
  browseMain.replaceChildren();
  browseSide.replaceChildren();
  browseSide.hidden = true;
}

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(await problem(response));
  return response.json();
}

async function openBrowse(gen) {
  const route = tab === "functions" || tab === "policies" ? tab : "model";
  browseMain.replaceChildren(el("div", "doc muted", "Loading…"));
  let data;
  try {
    data = await getJson(api(route));
  } catch (error) {
    if (gen === generation) {
      browseMain.replaceChildren(el("div", "doc muted", `Cannot show the ${tabLabel(tab).toLowerCase()} of ${current}: ${error.message}`));
    }
    return;
  }
  if (gen !== generation) return;
  browseGraph = data.graph;
  currentId = data.graph.id;
  renderGraphs();
  if (route === "functions") functionList = data.functions;
  else if (route === "policies") policies = data;
  else model = data;
  showItem(item);
}

function showItem(target) {
  item = target || null;
  if (tab === "model") renderModel();
  else if (tab === "types") {
    if (!tg) buildTypeGraph();
    tgSelect(item);
  } else if (tab === "functions") renderFunctions();
  else if (tab === "policies") renderPolicies();
}

// ---- views: building blocks --------------------------------------------

function moduleColor(id) {
  if (!model) return "var(--node)";
  const colored = model.modules.filter(m => !m.builtin && m.types > 0).map(m => m.id);
  const index = colored.indexOf(id);
  return index >= 0 && index < MODULE_COLORS ? `var(--m${index + 1})` : "var(--node)";
}

function swatch(module, abstract) {
  const mark = el("span", abstract ? "swatch open" : "swatch");
  if (abstract) mark.style.borderColor = moduleColor(module);
  else mark.style.background = moduleColor(module);
  mark.setAttribute("aria-hidden", "true");
  return mark;
}

// The active entities of a type and its subtypes.
function entityCount(curie) {
  const type = model.type[curie];
  return [curie, ...(type ? type.subtypes : [])].reduce((sum, c) => sum + (model.entities[c] || 0), 0);
}

function linkButton(text, title, onClick) {
  const button = el("button", "link", text);
  button.type = "button";
  if (title) button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

// A type: in the model and the type graph it shows the type there, elsewhere it opens the model.
function typeLink(curie) {
  if (!curie || curie === "any") return el("span", "muted", "any");
  return linkButton(typeName(curie), curie, () => {
    if (tab === "model") go(curie);
    else if (tab === "types") go(curie, false);
    else chooseTab("model", curie);
  });
}

function typeLinks(curies) {
  const box = el("span");
  (curies.length ? curies : ["any"]).forEach((curie, index) => {
    if (index) box.append(", ");
    box.append(typeLink(curie));
  });
  return box;
}

function moduleLink(id) {
  if (!id) return el("span", "muted", "no module");
  return linkButton(id, `module ${id}`, () => { if (tab === "model") go(id); else chooseTab("model", id); });
}

function functionLink(name) {
  return linkButton(name, null, () => { if (tab === "functions") go(name); else chooseTab("functions", name); });
}

function badge(text, warn) {
  return el("span", warn ? "badge warn" : "badge", text);
}

function code(text) {
  return el("code", null, text);
}

// Inline parts in one element; null and undefined are left out.
function frag(...parts) {
  const box = el("span");
  for (const part of parts) if (part !== null && part !== undefined && part !== "") box.append(part);
  return box;
}

function summary(text, deprecated) {
  return frag(text || "", deprecated ? " " : null, deprecated ? badge("deprecated", true) : null);
}

function table(headers, rows, numeric = []) {
  const box = el("table");
  const head = el("tr");
  headers.forEach((text, index) => head.append(el("th", numeric.includes(index) ? "num" : null, text)));
  box.append(head);
  for (const row of rows) {
    const line = el("tr");
    row.forEach((cell, index) => {
      const td = el("td", numeric.includes(index) ? "num" : null);
      if (cell !== null && cell !== undefined) td.append(typeof cell === "number" ? String(cell) : cell);
      line.append(td);
    });
    box.append(line);
  }
  return box;
}

function section(doc, title, count) {
  doc.append(el("h2", null, count === undefined ? title : `${title} (${count})`));
}

function actions(...buttons) {
  const box = el("div", "actions");
  box.append(...buttons.filter(Boolean));
  return box;
}

function actionButton(text, onClick, disabled) {
  const button = el("button", null, text);
  button.type = "button";
  button.disabled = Boolean(disabled);
  button.addEventListener("click", onClick);
  return button;
}

// A side bar entry: ``target`` is the item it shows (null: the start of the view).
function navEntry(target, text, lead, count, className) {
  const button = el("button", "entry" + (className ? " " + className : ""));
  button.type = "button";
  button.dataset.item = target || "";
  if (lead) button.append(lead);
  button.append(el("span", "text", text));
  if (count !== undefined && count !== null) button.append(el("span", "count", String(count)));
  button.title = target || text;
  button.addEventListener("click", () => go(target));
  return button;
}

function markNav() {
  for (const button of browseNav.querySelectorAll("button.entry")) {
    const on = button.dataset.item === (item || "");
    if (on) {
      button.setAttribute("aria-current", "true");
      button.scrollIntoView({block: "nearest"});
    } else button.removeAttribute("aria-current");
  }
}

function showDoc(doc) {
  browseMain.replaceChildren(doc);
  browseMain.scrollTop = 0;
}

