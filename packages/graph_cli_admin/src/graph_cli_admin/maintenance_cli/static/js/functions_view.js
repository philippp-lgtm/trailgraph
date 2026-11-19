"use strict";

// ---- views: functions ------------------------------------------------------

function buildFunctionsNav() {
  if (browseNav.dataset.built === "functions") return;
  browseNav.dataset.built = "functions";
  browseNav.replaceChildren();
  const search = el("input");
  search.type = "search";
  search.placeholder = "Search functions";
  search.setAttribute("aria-label", "Search functions");
  browseNav.append(search);
  const top = el("ul");
  const overview = el("li");
  overview.append(navEntry(null, "All functions", null, functionList.length));
  top.append(overview);
  browseNav.append(top);
  const groups = new Map();
  for (const f of functionList) {
    const key = f.module || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  for (const key of [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b))) {
    const title = el("h2", null, key || "no module");
    const list = el("ul");
    for (const f of groups.get(key).sort((a, b) => a.name.localeCompare(b.name))) {
      const row = el("li");
      row.dataset.search = `${f.name} ${f.summary} ${f.use_when}`.toLowerCase();
      row.append(navEntry(f.name, f.name, null, f.status === "deprecated" ? "deprecated" : null, f.status === "deprecated" ? "inherited" : null));
      list.append(row);
    }
    browseNav.append(title, list);
  }
  if (!functionList.length) browseNav.append(el("div", "note muted", "No functions."));
  search.addEventListener("input", () => {
    const text = search.value.trim().toLowerCase();
    for (const row of browseNav.querySelectorAll("li[data-search]")) row.hidden = Boolean(text) && !row.dataset.search.includes(text);
  });
  search.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    const first = browseNav.querySelector("li[data-search]:not([hidden]) button");
    if (first) first.click();
  });
}

function renderFunctions() {
  buildFunctionsNav();
  markNav();
  const doc = el("div", "doc");
  const found = item ? functionList.find(f => f.name === item) : null;
  if (!item) functionsOverview(doc);
  else if (found) functionDoc(doc, found);
  else doc.append(el("p", "muted", `There is no function ${item}.`));
  showDoc(doc);
}

function functionsOverview(doc) {
  doc.append(el("h1", null, `Functions of ${browseGraph.name}`));
  if (!functionList.length) {
    doc.append(el("p", "secondary", "This graph has no published functions, or you may not read them (read.function)."));
    return;
  }
  const active = functionList.filter(f => f.status !== "deprecated").length;
  doc.append(el("div", "meta", `${active} active · ${functionList.length - active} deprecated · run one with graph run`));
  doc.append(table(["Function", "Module", "Parameters", "Summary"], functionList.map(f => [
    frag(functionLink(f.name), f.featured ? " " : null, f.featured ? badge("featured") : null),
    moduleLink(f.module), code(f.signature || "–"), summary(f.summary, f.status === "deprecated"),
  ])));
}

function paramType(p) {
  const many = p.many ? "list of " : "";
  if (p.type === "ref") return frag(many, "ref ", typeLink(p.class));
  if (p.type === "enum") return frag(many, "enum ", code(typeName(p.enum)));
  return frag(many, code(p.type));
}

function runCommand(f) {
  const parts = [`graph run ${f.name}`];
  for (const p of f.params) {
    const value = p.type === "ref" ? `<${typeName(p.class)} id>` : `<${p.type === "enum" ? typeName(p.enum) : p.type}>`;
    parts.push(`--param ${p.name}=${value}`);
  }
  parts.push(`--graph ${browseGraph.name}`);
  return parts.join(" ");
}

function functionDoc(doc, f) {
  doc.append(el("h1", null, f.name));
  doc.append(frag(
    "module ", moduleLink(f.module), " ",
    badge(f.status, f.status === "deprecated"),
    f.featured ? " " : null, f.featured ? badge("featured") : null,
  ));
  if (f.summary) doc.append(el("p", "lead", f.summary));
  if (f.replaced_by) doc.append(frag("Replaced by ", functionLink(f.replaced_by)));
  section(doc, "Use when");
  doc.append(el("p", null, f.use_when));
  section(doc, "Run");
  doc.append(el("pre", null, runCommand(f)));
  section(doc, "Parameters", f.params.length);
  if (!f.params.length) doc.append(el("div", "small", "none"));
  else doc.append(table(["Parameter", "Type", "Default", "Summary"], f.params.map(p => [
    code(p.name), paramType(p), p.default === null ? "" : code(JSON.stringify(p.default)), p.summary,
  ])));
  section(doc, "Returns", f.returns.length);
  doc.append(table(["Column", "Summary"], f.returns.map(r => [code(r.column), r.summary])));
  if (f.examples.length) {
    section(doc, "Example questions", f.examples.length);
    const list = el("ul", "plain");
    for (const example of f.examples) {
      const row = el("li", null, example.question);
      const params = Object.keys(example.params || {});
      if (params.length) row.append(" ", code(params.map(k => `${k}=${JSON.stringify(example.params[k])}`).join(" ")));
      list.append(row);
    }
    doc.append(list);
  }
  section(doc, "Reads");
  const list = el("dl");
  const types = el("dd"), relations = el("dd");
  types.append(typeLinks(f.touches.types.map(t => t.curie)));
  relations.append(f.touches.relations.length ? frag(...f.touches.relations.flatMap((r, i) => i ? [", ", code(r.curie)] : [code(r.curie)])) : "none");
  list.append(el("dt", null, "types"), types, el("dt", null, "relations"), relations);
  doc.append(list);
  section(doc, "Query");
  doc.append(el("pre", null, f.query));
  doc.append(el("div", "small", `hash ${f.hash}`));
}

