"use strict";

// ---- views: model --------------------------------------------------------

function buildModelNav() {
  if (browseNav.dataset.built === "model") return;
  browseNav.dataset.built = "model";
  browseNav.replaceChildren();
  const top = el("ul");
  const overview = el("li");
  overview.append(navEntry(null, "Overview"));
  top.append(overview);
  browseNav.append(top);
  for (const [title, modules] of [["Modules", model.modules.filter(m => !m.builtin)], ["Built-in modules", model.modules.filter(m => m.builtin)]]) {
    if (!modules.length) continue;
    browseNav.append(el("h2", null, title));
    const list = el("ul");
    for (const m of modules) {
      const row = el("li");
      row.append(navEntry(m.id, m.id, swatch(m.id), m.types, "module"));
      const types = Object.values(model.type).filter(t => t.module === m.id);
      if (types.length) {
        const sub = el("ul");
        for (const t of types) {
          const entry = el("li");
          entry.append(navEntry(t.curie, typeName(t.curie), null, entityCount(t.curie), t.abstract ? "inherited" : null));
          sub.append(entry);
        }
        row.append(sub);
      }
      list.append(row);
    }
    browseNav.append(list);
  }
}

function renderModel() {
  buildModelNav();
  markNav();
  const doc = el("div", "doc");
  if (!item) modelOverview(doc);
  else if (model.type[item]) typeDoc(doc, model.type[item]);
  else if (model.module[item]) moduleDoc(doc, model.module[item]);
  else doc.append(el("p", "muted", `${item} is not part of this ontology.`));
  showDoc(doc);
}

function moduleEntities(id) {
  return Object.values(model.type).filter(t => t.module === id).reduce((sum, t) => sum + (model.entities[t.curie] || 0), 0);
}

function moduleTable(modules) {
  return table(
    ["Module", "Prefix", "Version", "Types", "Relations", "Functions", "Entities", "Summary"],
    modules.map(m => [frag(swatch(m.id), " ", moduleLink(m.id)), code(m.prefix), m.version, m.types, m.relations,
      m.functions === undefined ? "–" : m.functions, moduleEntities(m.id), m.summary]),
    [3, 4, 5, 6],
  );
}

function modelOverview(doc) {
  const total = Object.values(model.entities).reduce((sum, n) => sum + n, 0);
  doc.append(el("h1", null, `Ontology of ${model.graph.name}`));
  doc.append(el("div", "meta", `${model.ontology.bundle} ${model.ontology.version} · ${Object.keys(model.type).length} types · ${model.relations.length} relations · ${total} active entities`));
  doc.append(actions(actionButton("Show the type graph", () => chooseTab("types"))));
  const own = model.modules.filter(m => !m.builtin), builtin = model.modules.filter(m => m.builtin);
  section(doc, "Modules", own.length);
  doc.append(moduleTable(own));
  if (builtin.length) {
    section(doc, "Built-in modules", builtin.length);
    doc.append(moduleTable(builtin));
  }
}

function linkRows(links) {
  return links.map(l => [code(l.relation), typeLinks(l.from), typeLinks(l.to), moduleLink(l.module), summary(l.summary, l.deprecated)]);
}

function moduleDoc(doc, m) {
  const head = el("h1");
  head.append(swatch(m.id), m.id);
  doc.append(head);
  doc.append(frag(badge(`prefix ${m.prefix}`), " ", badge(`version ${m.version}`), m.builtin ? " " : null, m.builtin ? badge("built-in") : null));
  if (m.summary) doc.append(el("p", "lead", m.summary));
  section(doc, "Types", m.types.length);
  if (!m.types.length) doc.append(el("div", "small", "none"));
  else {
    doc.append(table(["Type", "Kind", "Entities", "Summary"], m.types.map(t => [
      typeLink(t.curie),
      frag(t.abstract ? badge("abstract") : null, t.extends ? frag(t.abstract ? " " : null, "extends ", typeLink(t.extends)) : null),
      entityCount(t.curie),
      summary(t.summary, t.deprecated),
    ]), [2]));
  }
  if (m.relations.length) {
    section(doc, "Relations inside the module", m.relations.length);
    doc.append(table(["Relation", "From", "To", "Cardinality", "Summary"], m.relations.map(r => [
      code(r.curie), typeLinks(r.domain), typeLinks(r.range), `${r.cardinality.out} out · ${r.cardinality.in} in`, summary(r.summary, r.deprecated),
    ])));
  }
  if (m.links_out.length) {
    section(doc, "Links to other modules", m.links_out.length);
    doc.append(table(["Relation", "From", "To", "Declared in", "Summary"], linkRows(m.links_out)));
  }
  if (m.links_in.length) {
    section(doc, "Links from other modules", m.links_in.length);
    doc.append(table(["Relation", "From", "To", "Declared in", "Summary"], linkRows(m.links_in)));
  }
  if (m.rules.length) {
    section(doc, "Rules", m.rules.length);
    doc.append(table(["Rule", "Severity", "Summary"], m.rules.map(r => [code(r.id), r.severity, summary(r.summary, r.deprecated)])));
  }
  if (m.functions && m.functions.length) {
    section(doc, "Functions", m.functions.length);
    doc.append(table(["Function", "Parameters", "Summary"], m.functions.map(f => [functionLink(f.name), code(f.signature || "–"), f.summary])));
  }
  const listed = model.modules.find(x => x.id === m.id);
  if (listed && listed.links.length) {
    section(doc, "Connected modules", listed.links.length);
    doc.append(table(["Module", "Relations to it", "Relations from it"], listed.links.map(l => [frag(swatch(l.module), " ", moduleLink(l.module)), l.out, l.in]), [1, 2]));
  }
}

function fieldType(f) {
  const many = f.many ? "list of " : "";
  if (f.kind === "enum") return frag(many, code(typeName(f.type)), f.values ? el("div", "small", f.values.join(" · ")) : null);
  if (f.kind === "lifecycle") return frag("lifecycle ", code(typeName(f.type)));
  return frag(many, code(f.type), f.base && f.base !== f.type ? el("span", "small", ` (${f.base})`) : null);
}

function fieldFlags(f) {
  const flags = [];
  if (f.required) flags.push("required");
  if (f.unique) flags.push("unique");
  if (f.role) flags.push(f.role.replace("_", " "));
  if (f.mutability && !["content", "lifecycle"].includes(f.mutability)) flags.push(f.mutability);
  if (f.default !== null && f.default !== undefined) flags.push(`default ${JSON.stringify(f.default)}`);
  for (const [key, value] of Object.entries(f.limits || {})) flags.push(`${key} ${JSON.stringify(value)}`);
  return frag(...flags.flatMap((flag, index) => index ? [" ", badge(flag)] : [badge(flag)]));
}

function lifecycleDoc(doc, lc) {
  doc.append(el("h3", null, `${lc.attr ? lc.attr + ": " : ""}${typeName(lc.lifecycle)}`));
  if (lc.summary) doc.append(el("p", null, lc.summary));
  doc.append(table(["State", "", "Summary"], lc.states.map(s => [
    code(s.name),
    frag(lc.initial.includes(s.name) ? badge("initial") : null, lc.initial.includes(s.name) && lc.terminal.includes(s.name) ? " " : null, lc.terminal.includes(s.name) ? badge("terminal") : null),
    s.summary,
  ])));
  if (lc.transitions.length) {
    doc.append(table(["Transition", "Guards"], lc.transitions.map(t => [
      frag(code(t.from), " → ", code(t.to)),
      t.guards.length ? frag(...t.guards.map(g => el("div", null, `${g.id}: ${g.summary}`))) : el("span", "small", "none"),
    ])));
  }
}

// Terms with their types: [[term, [curie, ...]], ...]; a term without types is left out.
function termList(rows) {
  const list = el("dl");
  for (const [term, curies] of rows) {
    if (!curies.length) continue;
    const dd = el("dd");
    dd.append(typeLinks(curies));
    list.append(el("dt", null, term), dd);
  }
  return list;
}

function typeDoc(doc, t) {
  const head = el("h1");
  head.append(swatch(t.module, t.abstract), typeName(t.curie), el("span", "curie", t.curie));
  doc.append(head);
  doc.append(frag(
    "module ", moduleLink(t.module),
    t.extends ? " · extends " : null, t.extends ? typeLink(t.extends) : null,
    t.alias_prefix ? ` · alias prefix ${t.alias_prefix}` : null,
    t.abstract ? " " : null, t.abstract ? badge("abstract") : null,
    t.deprecated ? " " : null, t.deprecated ? badge("deprecated", true) : null,
  ));
  if (t.summary) doc.append(el("p", "lead", t.summary));
  if (t.description) {
    const text = el("p", null, t.description);
    text.style.whiteSpace = "pre-wrap";
    doc.append(text);
  }
  const count = entityCount(t.curie);
  doc.append(actions(
    actionButton(count ? `Show ${count} active ${count === 1 ? "entity" : "entities"} in the graph` : "No active entities", () => {
      focusTypes = new Set([t.curie, ...t.subtypes]);
      chooseTab("graph");
    }, !count),
    tab !== "types" ? actionButton("Show in the type graph", () => chooseTab("types", t.curie)) : actionButton("Open in the model", () => chooseTab("model", t.curie)),
  ));
  if (t.ancestors.length || t.subtypes.length) doc.append(termList([["ancestors", t.ancestors], ["subtypes", t.subtypes]]));
  section(doc, "Fields", t.fields.length);
  doc.append(table(["Field", "Type", "Flags", "Summary"], t.fields.map(f => [
    frag(code(f.name), f.declared_in !== t.curie ? el("div", "small", `from ${typeName(f.declared_in)}`) : null),
    fieldType(f), fieldFlags(f), summary(f.summary, f.deprecated),
  ])));
  for (const lc of t.lifecycles) {
    if (lc === t.lifecycles[0]) section(doc, "Lifecycle");
    lifecycleDoc(doc, lc);
  }
  section(doc, "Relations from this type", t.relations_out.length);
  if (!t.relations_out.length) doc.append(el("div", "small", "none"));
  else doc.append(table(["Relation", "To", "Cardinality", "Summary"], t.relations_out.map(r => [
    frag(code(r.curie), r.qualifiers.length ? el("div", "small", `qualifiers: ${r.qualifiers.join(", ")}`) : null),
    typeLinks(r.range), r.cardinality, summary(r.summary, r.deprecated),
  ])));
  section(doc, "Relations to this type", t.relations_in.length);
  if (!t.relations_in.length) doc.append(el("div", "small", "none"));
  else doc.append(table(["Relation", "From", "Cardinality", "Summary"], t.relations_in.map(r => [
    frag(code(r.curie), r.inverse_label ? el("div", "small", `inverse: ${r.inverse_label}`) : null),
    typeLinks(r.from), r.cardinality, summary(r.summary, r.deprecated),
  ])));
  if (t.rules.length) {
    section(doc, "Rules", t.rules.length);
    doc.append(table(["Rule", "Severity", "Summary"], t.rules.map(r => [code(r.id), r.severity, summary(r.summary, r.deprecated)])));
  }
}

