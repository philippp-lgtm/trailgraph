"use strict";
const token = new URLSearchParams(location.search).get("t") || "";
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const tooltip = document.getElementById("tooltip");
const statusLine = document.getElementById("status");
const eventList = document.getElementById("events");
const showInactive = document.getElementById("inactive");
const onlyMatches = document.getElementById("only_matches");
const scaleNodes = document.getElementById("scale_nodes");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const FLASH_MS = 1500;
// Reads glow: an entity or edge a read touched lights up and fades, and a pulse runs along
// every walked edge, one step after the other, so an agent's walk flickers through the graph.
const GLOW_MS = 900;       // a glow falls to 1/e in this time
const MAX_GLOW = 1.6;      // reads in quick succession add up to this
const PULSE_MS = 380;      // a pulse's travel time along one edge
const STEP_MS = 70;        // the gap between two walked edges of one read
const SPREAD_MS = 1600;    // every step of one read starts within this time
const MAX_PULSES = 800;
const LABEL_LIT = 30;      // up to this many glowing entities show their labels
const SESSION_COLORS = ["read", "read-2", "read-3", "read-4", "read-5"];
const MAX_EVENTS = 30;
const RADIUS = 5;
const MAX_RADIUS = 18;   // radius of the most connected node when scaling by connections
const DOT_MIN = 1.2;     // radius of a dot in a crowded view, at least
const DOT_SHARE = 0.2;   // in a crowded view a dot's radius is at most this share of the mean gap between dots

let current = null;      // the shown graph as the URL names it (name or id), or null
let currentId = null;    // its id, once loaded
let generation = 0;      // bumped on every switch: answers for an older graph are dropped
let stream = null;       // the EventSource of the shown graph
let graphList = [];      // the graphs of the side bar: {id, name, title, language, state}
let listed = false;      // the list has been loaded once
let defaultGraph = null; // the graph of --graph, shown when the URL names none
let nodes = new Map();   // id -> {id, label, type, alias, status, x, y, vx, vy, flash, fixed}
let links = new Map();   // edge id -> {id, source, target, rel}
let seq = -1;            // the seq of the loaded graph; events at or below it are in it already
let pending = [];        // events that arrive while the graph loads
let loading = false;
let alpha = 1;           // layout temperature
let view = {x: 0, y: 0, k: 1};
let hover = null;
let hoverLink = null;
let query = "";          // lower-case search text; non-matching entities are dimmed
let hiddenTypes = new Set();
let selected = null;     // {kind: "node", id} or {kind: "link", id}
let colors = {};
let autoFit = true;      // fit the graph into view until the user pans or zooms
let lit = new Set();     // nodes with a glow: node.glow = {e, color}
let litLinks = new Map(); // edge id -> {e, color}
let pulses = [];         // {link, from, to, t0, color, started, arrived}
let sparks = [];         // {node, t0, color}: entities of a read that no walked edge reaches
let sessions = new Map(); // session -> color name, in order of appearance
let ambient = 0;         // 0..1: how much the rest of the graph steps back while reads glow
let lastFrame = performance.now();
const dark = matchMedia("(prefers-color-scheme: dark)");
let isDark = false;      // the theme in effect: data-theme on <html>, else the device
const LABEL_ALL = 80;    // up to this many visible nodes every label shows
const MIN_ZOOM = 0.02;   // far enough out for a few thousand nodes

// Applies the theme and reads the colors the canvas draws with. A host page may call it
// after it changed data-theme; the view also does, on that change and on the device's.
function readColors() {
  const theme = document.documentElement.getAttribute("data-theme");
  isDark = theme === "dark" || (theme !== "light" && dark.matches);
  document.body.classList.toggle("theme-dark", isDark);
  const style = getComputedStyle(document.body);
  for (const name of ["node", "node-inactive", "link", "created", "updated", ...SESSION_COLORS, "text-primary", "text-secondary", "surface-1"]) {
    colors[name] = style.getPropertyValue("--" + name).trim();
  }
}
readColors();
dark.addEventListener("change", readColors);
new MutationObserver(readColors).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});

