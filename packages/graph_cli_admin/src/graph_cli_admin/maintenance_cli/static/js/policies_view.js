"use strict";

// ---- views: policies -------------------------------------------------------

// The groups of compiled rules, by the kind the server names, in the order they are shown.
const RULE_KINDS = [
  ["grant", "Grants"], ["maintainer", "Maintainers"], ["maintain", "Project admins"], ["scope", "Scopes"],
  ["deny", "Deny rules"], ["type_limit", "Type limits"], ["builtin", "Built-in rules"], ["rule", "Other rules"],
];
const FACETS = {
  entrypoint: "through", principal_id: "principal", principal_name: "user", project: "project",
  agent_type: "agent type", agent_id: "agent", session_id: "session", device_id: "device",
};
const LAYER_ITEMS = {installation: "Installation policy", graph: "Graph policy"};

function buildPoliciesNav() {
  if (browseNav.dataset.built === "policies") return;
  browseNav.dataset.built = "policies";
  const list = el("ul");
  for (const [target, text] of [[null, "Access"], ["installation", LAYER_ITEMS.installation], ["graph", LAYER_ITEMS.graph]]) {
    const layer = target && policies.layers.find(l => l.layer === target);
    const row = el("li");
    row.append(navEntry(target, text, null, layer ? layer.rules.length : null));
    list.append(row);
  }
  browseNav.replaceChildren(list);
}

function renderPolicies() {
  buildPoliciesNav();
  markNav();
  const doc = el("div", "doc");
  const layer = item ? policies.layers.find(l => l.layer === item) : null;
  if (!item) policiesOverview(doc);
  else if (layer) layerDoc(doc, layer);
  else doc.append(el("p", "muted", `There is no policy layer ${item}.`));
  showDoc(doc);
}

function shortHash(hash) {
  return hash ? code(hash.slice(0, 19) + "…") : el("span", "muted", "none");
}

function accessLevel(caller) {
  const box = frag(code(caller.level));
  if (caller.withheld.length) box.append(" ", el("span", "small", "withheld: "), frag(...caller.withheld.flatMap((a, i) => i ? [", ", code(a)] : [code(a)])));
  if (caller.reads) box.append(readsText(caller.reads));
  if (caller.markings) box.append(markingsText(caller.markings));
  if (caller.writes) box.append(writesText(caller.writes));
  return box;
}

// The types a caller sees when a read limit leaves it some of them only.
function readsText(reads) {
  const box = el("span", "small", "reads:");
  box.append(el("br"));
  if (!reads.length) box.append("no type");
  reads.forEach((type, index) => box.append(...(index ? [", "] : []), code(type)));
  return box;
}

// The markings a caller holds when the graph's model uses some it lacks.
function markingsText(markings) {
  const box = el("span", "small", "markings:");
  box.append(el("br"));
  if (!markings.length) box.append("none");
  markings.forEach((marking, index) => box.append(...(index ? [", "] : []), code(marking)));
  return box;
}

// What a write allow list lets a caller write: per type its verbs; a verb with a list of
// terms shows how many, the terms in the tooltip.
function writesText(writes) {
  const box = el("span", "small", "writes:");
  for (const [type, verbs] of Object.entries(writes)) {
    box.append(el("br"), code(type), " ");
    Object.entries(verbs).forEach(([verb, right], index) => {
      if (index) box.append(", ");
      const part = el("span", null, right === true ? verb : `${verb} (${right.length ? right.length : "none"})`);
      if (right !== true) part.title = right.join(", ");
      box.append(part);
    });
  }
  return box;
}

function callerName(caller) {
  if (caller.token) {
    return frag("token ", code(caller.token), caller.scope === "admin" ? " " : null, caller.scope === "admin" ? badge("admin") : null,
      caller.expired ? " " : null, caller.expired ? badge("expired", true) : null,
      caller.roles && caller.roles.length ? " " : null,
      caller.roles && caller.roles.length ? el("span", "small", `roles ${caller.roles.join(", ")}`) : null);
  }
  return caller.project ? frag("you from project ", code(caller.project)) : el("span", null, "you without a project");
}

function policiesOverview(doc) {
  doc.append(el("h1", null, `Policies of ${policies.graph.name}`));
  doc.append(el("div", "small", "policy set "));
  doc.lastChild.append(shortHash(policies.policy_hash));
  section(doc, "Access", policies.callers.length);
  doc.append(el("p", "secondary", "What graph-admin policy explain answers for this graph: your own calls with graph from each project of the scopes and from none, and a request with each token."));
  const through = {app: "graph", server: "server API", maint: "admin API"};
  doc.append(table(["Caller", "Project", "Through", "Level", "Maintains"], policies.callers.map(c => [
    callerName(c), c.project ? code(c.project) : el("span", "muted", "none"), through[c.entrypoint] || c.entrypoint,
    // an admin token maintains graphs; the levels of graph and the server API are not its
    c.entrypoint === "maint" ? el("span", "muted", "–") : accessLevel(c), c.entrypoint === "maint" ? (c.maintains ? "yes" : "no") : "",
  ])));
  section(doc, "Layers", policies.layers.length);
  doc.append(table(["Layer", "Installed", "Rules for this graph", "Obligations", "Hash"], policies.layers.map(l => [
    linkButton(LAYER_ITEMS[l.layer], null, () => go(l.layer)),
    l.installed ? "yes" : (l.layer === "installation" ? "no, built-in policy" : "no"),
    l.rules.length, l.obligations.length, shortHash(l.hash),
  ]), [2, 3]));
}

// A resource glob of a rule, with graph names for graph ids.
function resourceText(pattern) {
  if (pattern === "*") return el("span", null, "everything");
  if (pattern === "graph:*") return el("span", null, "every graph");
  if (pattern === "installation") return el("span", null, "the installation");
  const marking = /^marking:([0-9a-f-]{36})\/(.+)$/.exec(pattern);
  if (marking) {
    const owner = policies.graphs[marking[1]] || marking[1];
    return frag("marking ", code(marking[2]), " in ", marking[1] === policies.graph.id ? el("strong", null, owner) : el("span", null, owner));
  }
  const found = /^graph:([0-9a-f-]{36})(\*|\/type:([^/]+)(?:\/(attr|rel):(.+))?)?$/.exec(pattern);
  if (!found) return code(pattern);
  const name = policies.graphs[found[1]] || found[1];
  const graph = found[1] === policies.graph.id ? el("strong", null, name) : el("span", null, name);
  if (!found[3]) return graph;
  const type = found[1] === policies.graph.id && !found[3].endsWith(":*") ? typeLink(found[3]) : code(found[3]);
  if (found[4]) {
    // below a type: one attribute or relation of it, or all of them
    const what = found[4] === "attr" ? "attribute" : "relation";
    const term = found[5] === "*" ? el("span", null, `every ${what}`) : frag(`${what} `, code(found[5]));
    return frag(term, " of type ", type, " in ", graph);
  }
  return frag("type ", type, " in ", graph);
}

function listOf(values, render) {
  const box = el("span");
  values.forEach((value, index) => {
    if (index) box.append(", ");
    box.append(render(value));
  });
  return box;
}

function asList(value) {
  return value === undefined || value === null ? [] : Array.isArray(value) ? value : [value];
}

function whoText(principal) {
  const keys = Object.keys(principal || {});
  if (!keys.length) return el("span", "muted", "everyone");
  const box = el("span");
  keys.forEach((key, index) => {
    if (index) box.append(el("br"));
    box.append(el("span", "small", (FACETS[key] || key) + " "), listOf(asList(principal[key]), v => code(v)));
  });
  return box;
}

function actionText(rule) {
  if (rule.level) return frag(badge(`level ${rule.level}`));
  return listOf(asList(rule.action), a => code(a));
}

function unlessText(rule) {
  const conditions = rule.unless || [];
  if (!conditions.length) return "";
  const box = el("span");
  conditions.forEach((condition, index) => {
    if (index) box.append(el("br"), el("span", "small", "or "));
    if (condition.principal) box.append(whoText(condition.principal));
    if (condition.principal && condition.resource) box.append(el("span", "small", " on "));
    if (condition.resource) box.append(listOf(asList(condition.resource), resourceText));
  });
  return box;
}

function layerDoc(doc, layer) {
  doc.append(el("h1", null, LAYER_ITEMS[layer.layer]));
  doc.append(frag(badge(layer.installed ? "installed" : "not installed"), " ", el("span", "small", "hash "), shortHash(layer.hash)));
  if (!layer.installed) {
    doc.append(el("p", "secondary", layer.layer === "installation"
      ? "No installation policy is installed: the built-in policy decides, every graph readable and writable through graph."
      : "This graph has no policy of its own: the installation policy alone decides."));
  }
  if (layer.layer === "graph" && Object.keys(layer.types).length) {
    section(doc, "Type limits", Object.keys(layer.types).length);
    doc.append(table(["Type", "Limit"], Object.entries(layer.types).map(([type, limit]) => [
      type.endsWith(":*") ? code(type) : typeLink(type), code(limit),
    ])));
  }
  for (const [kind, title] of RULE_KINDS) {
    const found = layer.rules.filter(r => r.kind === kind);
    if (!found.length || (kind === "type_limit" && Object.keys(layer.types).length)) continue;  // listed above
    section(doc, title, found.length);
    doc.append(table(["Rule", "Who", "Actions", "Resources", "Unless"], found.map(r => [
      frag(code(r.id), " ", badge(r.effect, r.effect === "deny")), whoText(r.principal), actionText(r),
      listOf(asList(r.resource), resourceText), unlessText(r),
    ])));
  }
  if (layer.installed && !layer.rules.length) doc.append(el("p", "secondary", "No rule of this layer decides on this graph."));
  if (layer.obligations.length) {
    section(doc, "Obligations", layer.obligations.length);
    doc.append(table(["Actions", "Reason required", "Max batch"], layer.obligations.map(o => [
      listOf(asList(o.action), a => code(a)), o.require_reason ? "yes" : "", o.max_batch || "",
    ])));
  }
}

