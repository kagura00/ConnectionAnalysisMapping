const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { buildLayout, annotationsFor, contextPositions } = require("../src/connection_map/web/exploration.js");
const { diagnosticPage } = require("../src/connection_map/web/exploration.js");

test("diagnostics prioritize errors beyond the first chunk and paginate file filters", () => {
  const diagnostics = Array.from({length: 2500}, (_, i) => ({severity: "warning", file: `src/${i}.ts`}));
  diagnostics.push({severity: "error", file: "src/login.html"}, {severity: "error", file: "src/chat.html"});
  const first = diagnosticPage(diagnostics);
  assert.equal(first.diagnostics[0].severity, "error");
  assert.equal(first.diagnostics[1].severity, "error");
  assert.equal(first.next_offset, 200);
  const filtered = diagnosticPage(diagnostics, {severity: "error", file: "LOGIN"});
  assert.equal(filtered.total, 1);
  assert.equal(filtered.diagnostics[0].file, "src/login.html");
  assert.equal(filtered.next_offset, null);
  const last = diagnosticPage(diagnostics, {offset: 2400});
  assert.equal(last.diagnostics.length, 102);
  assert.equal(last.next_offset, null);
});

test("the viewer load/save path retains notes and rejects invalid layouts without losing existing notes", () => {
  const source = fs.readFileSync(path.join(__dirname, "../src/connection_map/web/app.js"), "utf8").replace(/\r\n/g, "\n");
  function extract(name) {
    const start = source.indexOf(`  function ${name}(`);
    assert.ok(start >= 0, name);
    return source.slice(start, source.indexOf("\n  }", start) + 4);
  }
  const state = { document: { schema_version: "1.0" }, layoutOverrides: new Map(),
    nodeById: new Map([["f", {}]]), positionById: new Map(), camera: {}, focusActive: false };
  const scope = { state, ConnectionMapExploration: { buildLayout }, ZOOM_MIN: 0.15, ZOOM_MAX: 4 };
  vm.createContext(scope);
  vm.runInContext(["isFiniteNumber", "applyLayout", "buildLayoutDocument"].map(extract).join("\n"), scope);
  vm.runInContext(`applyLayout({ format: "connection-analysis-layout", schema_version: "1.0",
    annotations: [{ text: "keep this note" }], nodes: { f: { x: 1, y: 2 } } });
    state.layoutOverrides.set("f", { x: 8, y: 9 });`, scope);
  let saved = JSON.parse(vm.runInContext("JSON.stringify(buildLayoutDocument())", scope));
  assert.equal(saved.annotations[0].text, "keep this note");
  assert.deepEqual(saved.nodes.f, { x: 8, y: 9 });
  assert.throws(() => vm.runInContext(`applyLayout({format: "connection-analysis-layout", schema_version: "1.0", annotations: {}})`, scope));
  saved = JSON.parse(vm.runInContext("JSON.stringify(buildLayoutDocument())", scope));
  assert.equal(saved.annotations[0].text, "keep this note");
});

test("layout edits preserve annotations, unknown extensions, unloaded nodes and node metadata", () => {
  const original = { annotations: [{ text: "Human note", node_id: "f", extension: [1, 2] }],
    extensions: { owner: "user" }, nodes: { f: { x: 1, y: 2, color: "blue" }, unloaded: { x: 8, y: 9 } },
    camera: { x: 0, y: 0, zoom: 1, extra: true } };
  const saved = buildLayout(original, "1.0", { x: 5, y: 6, zoom: 2 }, new Map([["f", { x: 3, y: 4 }]]));
  assert.deepEqual(saved.annotations, original.annotations);
  assert.deepEqual(saved.extensions, original.extensions);
  assert.deepEqual(saved.nodes.unloaded, original.nodes.unloaded);
  assert.deepEqual(saved.nodes.f, { x: 3, y: 4, color: "blue" });
  assert.equal(saved.camera.extra, true);
  saved.annotations[0].extension.push(3);
  assert.deepEqual(original.annotations[0].extension, [1, 2]);
});

test("manual and layout notes are selected by node, edge and global scope", () => {
  const layout = { annotations: [{ text: "global" }, { node_id: "n", text: "node" }, { node_id: "other", text: "other" }] };
  const meta = { extensions: { manual_overlay: { annotations: [{ edge_id: "e", text: "edge" }] } } };
  assert.deepEqual(annotationsFor(layout, meta, "n").map((n) => n.text), ["global", "node"]);
  assert.deepEqual(annotationsFor(layout, meta, null, "e").map((n) => n.text), ["global", "edge"]);
});

test("focus layout separates callers and callees without changing saved graph coordinates", () => {
  const result = { focus_id: "b", nodes: ["a", "b", "c", "d"].map((id) => ({ id })),
    edges: [{ source_id: "a", target_id: "b" }, { source_id: "b", target_id: "c" }, { source_id: "c", target_id: "d" }] };
  const points = contextPositions(result);
  assert.equal(points.size, 4);
  assert.ok(points.get("a").x < points.get("b").x);
  assert.ok(points.get("c").x > points.get("b").x);
  assert.deepEqual(result.nodes[0], { id: "a" });
});
