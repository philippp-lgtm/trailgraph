"use strict";

// ---- narrow screens --------------------------------------------------

const narrow = matchMedia("(max-width: 720px)");
const panel = document.getElementById("panel");
const panelToggle = document.getElementById("panel_toggle");

function setPanel(open) {
  panel.classList.toggle("minimized", !open);
  panelToggle.setAttribute("aria-expanded", String(open));
  panelToggle.textContent = open ? "Hide details" : "Details";
}
panelToggle.addEventListener("click", () => setPanel(panel.classList.contains("minimized")));

// A wide screen folds the panel to a strip at the right; ``remember``: the user's choice.
const panelFold = document.getElementById("panel_fold");
const PANEL_KEY = "graph-view.panel";

function foldPanel(folded, remember) {
  panel.classList.toggle("folded", folded);
  panelFold.setAttribute("aria-expanded", String(!folded));
  const label = folded ? "Show the details" : "Hide the details";
  panelFold.setAttribute("aria-label", label);
  panelFold.title = label;
  if (remember) {
    try { localStorage.setItem(PANEL_KEY, folded ? "folded" : "open"); } catch (error) { /* storage off */ }
  }
}
panelFold.addEventListener("click", () => foldPanel(!panel.classList.contains("folded"), true));

function setStatus(text, live) {
  statusLine.textContent = text;
  statusLine.classList.toggle("live", live);
}

// The stream of the shown graph only: the view sends each graph's events to its own clients.
// Every (re)connect loads the graph again; events up to its seq are in it already.
function connect(gen) {
  const source = new EventSource(api("events"));
  stream = source;
  source.onopen = () => {
    if (gen !== generation) return;
    setStatus("live", true);
    loadGraph(gen).catch(error => { if (gen === generation) failGraph(error); });
  };
  source.onmessage = message => { if (gen === generation) handle(JSON.parse(message.data)); };
  source.onerror = () => {
    if (gen !== generation) return;
    if (source.readyState !== EventSource.CLOSED) { setStatus("reconnecting", false); return; }
    // Refused for good (an unknown graph, a denial; the browser does not retry after an
    // error status): stay closed, and let the graph route say why, once.
    source.close();
    loadGraph(gen).then(() => setStatus("not live, reload the page", false))
      .catch(error => { if (gen === generation) failGraph(error); });
  };
}

// ---- graphs ----------------------------------------------------------

const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar_toggle");
const graphListBox = document.getElementById("graph_list");
const graphFolder = document.getElementById("graph_folder");
const viewList = document.getElementById("view_list");
const sidebarBack = document.getElementById("sidebar_back");
const graphsHeading = document.getElementById("graphs_heading");
const graphsStatus = document.getElementById("graphs_status");
const emptyShow = document.getElementById("empty_show");
const SIDEBAR_KEY = "graph-view.sidebar";
const LIST_REFRESH_MS = 60000;

// ``remember``: a choice on a wide screen; a narrow one always starts with the side bar folded.
function setSidebar(open, remember) {
  sidebar.classList.toggle("collapsed", !open);
  sidebarToggle.setAttribute("aria-expanded", String(open));
  const label = open ? "Hide the list of graphs" : "Show the list of graphs";
  sidebarToggle.setAttribute("aria-label", label);
  sidebarToggle.title = label;
  showFolder();
  emptyShow.hidden = open;
  if (remember && !narrow.matches) {
    try { localStorage.setItem(SIDEBAR_KEY, open ? "open" : "collapsed"); } catch (error) { /* storage off */ }
  }
}

function storedSidebar() {
  try { return localStorage.getItem(SIDEBAR_KEY); } catch (error) { return null; }
}

function storedPanel() {
  try { return localStorage.getItem(PANEL_KEY); } catch (error) { return null; }
}

function fitToScreen() {
  setSidebar(!narrow.matches && storedSidebar() !== "collapsed", false);
  setPanel(!narrow.matches);
}

function isCurrent(graph) {
  return current !== null && (graph.name === current || graph.id === current || graph.id === currentId);
}

let renderedList = "";    // the list the side bar shows, as JSON: rebuilt only when it changed
let inFolder = false;     // the side bar shows the views of the shown graph instead of the list

// The list of graphs, or the shown graph as a folder: its views and Back to the list.
function showFolder() {
  const open = !sidebar.classList.contains("collapsed");
  const folder = inFolder && current !== null;
  graphListBox.hidden = !open || folder;
  graphFolder.hidden = !open || !folder;
  sidebarBack.hidden = !folder;
  graphsHeading.hidden = folder;
  graphsStatus.hidden = folder;
}

// Into the folder of the shown graph, or ``false``: back to the list; ``focus`` moves there.
function enterFolder(open, focus) {
  inFolder = open;
  showFolder();
  if (!focus) return;
  const target = open
    ? viewList.querySelector('button[aria-current="page"]') || viewList.querySelector("button")
    : graphListBox.querySelector('button[aria-current="true"]') || graphListBox.querySelector("button");
  if (target) target.focus();
}

function renderGraphs() {
  graphsStatus.textContent = listed && !graphList.length ? "No graphs to show." : "";
  const key = JSON.stringify(graphList);
  if (key === renderedList) {  // keep the buttons, so a click in progress or the focus stays
    for (const button of graphListBox.querySelectorAll("button[data-graph]")) {
      const graph = graphList.find(g => g.name === button.dataset.graph);
      if (graph && isCurrent(graph)) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
    renderTabs();
    return;
  }
  renderedList = key;
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.graph : null;
  graphListBox.replaceChildren();
  for (const graph of graphList) {
    const item = el("li");
    const button = el("button");
    button.type = "button";
    button.dataset.graph = graph.name;
    if (isCurrent(graph)) button.setAttribute("aria-current", "true");
    const name = el("span", "name", graph.name);
    if (graph.state !== "active") name.append(el("span", "state", graph.state));
    button.append(name);
    if (graph.title) button.append(el("span", "title", graph.title));
    button.title = graph.title ? `${graph.name}: ${graph.title}` : graph.name;
    button.addEventListener("click", () => choose(graph.name));
    item.append(button);
    graphListBox.append(item);
    if (focused === graph.name) button.focus();
  }
  renderTabs();
}

async function loadGraphs() {
  try {
    const response = await fetch("graphs?t=" + encodeURIComponent(token), {cache: "no-store"});
    if (!response.ok) throw new Error(await problem(response));
    const data = await response.json();
    graphList = data.graphs;
    defaultGraph = data.default;
    listed = true;
    renderGraphs();
    if (!current) showEmpty();
  } catch (error) {
    graphsStatus.textContent = `cannot list the graphs: ${error.message}`;
  }
}

function resetGraph() {
  nodes = new Map();
  links = new Map();
  seq = -1;
  pending = [];
  loading = false;
  lit = new Set();
  litLinks = new Map();
  pulses = [];
  sparks = [];
  sessions = new Map();
  ambient = 0;
  hover = null;
  hoverLink = null;
  showTooltip(null);
  selected = null;
  detail.hidden = true;
  closePreview();
  hiddenTypes = new Set();
  query = "";
  document.getElementById("search").value = "";
  autoFit = true;
  view = {x: 0, y: 0, k: 1};
  alpha = 1;
  currentId = null;
  eventList.replaceChildren();
  for (const id of ["types", "counts", "matches", "reads"]) document.getElementById(id).replaceChildren();
  document.getElementById("title").textContent = current || "graph view";
}

function showEmpty(title, text) {
  document.body.classList.add("no-graph");
  document.getElementById("empty_title").textContent = title || "No graph selected";
  document.getElementById("empty_text").textContent = text
    || (listed && !graphList.length ? "There is no graph you may read." : "Pick a graph from the list on the left.");
}

function failGraph(error) {
  if (stream) { stream.close(); stream = null; }
  setStatus("not live", false);
  showEmpty(`Cannot show ${current}`, error.message);
}

// Shows graph ``name`` (as the URL names it), or none, in view ``wanted`` (default the type
// graph) with ``target`` picked there.
function showGraph(name, wanted, target) {
  generation++;
  if (stream) { stream.close(); stream = null; }
  if ((name || null) !== current) inFolder = Boolean(name);
  current = name || null;
  tab = !current ? "graph" : TABS.some(([key]) => key === wanted) ? wanted : "types";
  item = target || null;
  if (tab !== "graph") focusTypes = null;
  resetGraph();
  resetBrowse();
  renderGraphs();
  document.body.classList.toggle("browsing", tab !== "graph");
  for (const [key] of TABS) document.body.classList.toggle("tab-" + key, key === tab);
  if (!current) {
    document.title = "graph view";
    setStatus("no graph selected", false);
    showEmpty();
    return;
  }
  document.body.classList.remove("no-graph");
  document.title = `${current}${tab === "graph" ? "" : " · " + tabLabel(tab).toLowerCase()} · graph view`;
  if (tab !== "graph") { openBrowse(generation); return; }
  setStatus("connecting", false);
  resize();
  connect(generation);
}

// A graph picked in the side bar opens its folder there and shows it: a new history entry,
// so Back returns to the previous one. The shown graph picked again keeps its view.
function choose(name) {
  inFolder = true;
  if (name === current) { enterFolder(true, true); return; }
  const params = new URLSearchParams(location.search);
  params.set("graph", name);
  params.delete("tab");
  params.delete("item");
  history.pushState(null, "", "?" + params);
  showGraph(name);
  enterFolder(true, true);
}

function wantedGraph() {
  return new URLSearchParams(location.search).get("graph") || defaultGraph;
}

function wantedTab() {
  return new URLSearchParams(location.search).get("tab") || "types";
}

function wantedItem() {
  return new URLSearchParams(location.search).get("item");
}

sidebarToggle.addEventListener("click", () => setSidebar(sidebar.classList.contains("collapsed"), true));
sidebarBack.addEventListener("click", () => enterFolder(false, true));
emptyShow.addEventListener("click", () => { setSidebar(true, true); sidebarToggle.focus(); });
addEventListener("popstate", () => {
  const name = wantedGraph(), wanted = wantedTab();
  // Back and Forward between the items of one tab keep what the tab loaded.
  if (current && name === current && wanted === tab && tab !== "graph" && (model || functionList || policies)) showItem(wantedItem());
  else showGraph(name, wanted, wantedItem());
});
addEventListener("focus", loadGraphs);
setInterval(() => { if (!document.hidden) loadGraphs(); }, LIST_REFRESH_MS);

