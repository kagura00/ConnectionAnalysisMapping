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

test("AI export downloads the exact bounded source packet from the shared investigation API", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../src/connection_map/web/app.js"), "utf8").replace(/\r\n/g, "\n");
  function extract(name) {
    const start = source.indexOf(`  function ${name}(`);
    assert.ok(start >= 0, name);
    return source.slice(start, source.indexOf("\n  }", start) + 4);
  }
  // Re-encoding large numbers or non-BMP source text can change a character
  // budget; the viewer must preserve the verified API response exactly.
  const text = '{"format":"connection-analysis-investigation","source":"日本語😀","annotation":1e+20}\n';
  const elements = [];
  const blobs = [];
  const clicks = [];
  const errors = [];
  const query = {direction: "in", depth: 2, resolution: "all", max_nodes: 60, max_edges: 120, relations: ["calls"]};
  const scope = { URLSearchParams, state: { contextResult: { focus_id: "method", query, edges: [], truncation: {} } },
    document: { createElement(tag) {
      const element = { tag, addEventListener(event, handler) { this[event] = handler; },
        click() { clicks.push({href: this.href, download: this.download}); } };
      elements.push(element);
      return element;
    } },
    detailsElement: { append() {} }, emptyList: (value) => value,
    dataUrl: (endpoint) => `/api/repositories/repo/${endpoint}`,
    setStatus: (message, failed) => { if (failed) errors.push(message); },
    fetch: async (url) => {
      const [endpoint, params] = url.split("?");
      assert.equal(endpoint, "/api/repositories/repo/investigate");
      const selected = new URLSearchParams(params);
      assert.equal(selected.get("node"), "method");
      assert.equal(selected.get("max_chars"), "12000");
      assert.equal(selected.get("snippets"), "true");
      assert.equal(selected.get("relation"), "calls");
      assert.equal(selected.get("direction"), "in");
      return {ok: true, text: async () => text};
    },
    Blob: class { constructor(parts) { blobs.push(parts.join("")); } },
    URL: { createObjectURL: () => "blob:packet", revokeObjectURL() {} }, setTimeout: (callback) => callback(),
  };
  vm.createContext(scope);
  vm.runInContext(["appendContextDetails", "downloadJson", "downloadJsonText"].map(extract).join("\n"), scope);
  vm.runInContext('appendContextDetails({id:"method"})', scope);
  await elements.find((element) => element.textContent === "AI用JSONを保存").click();
  assert.deepEqual(errors, []);
  assert.deepEqual(blobs, [text]);
  assert.deepEqual(clicks, [{href: "blob:packet", download: "connection-investigation.json"}]);
  vm.runInContext('downloadJson({nodes:{}}, "layout.json")', scope);
  assert.match(blobs[1], /\n  "nodes"/);  // Human layout exports retain their readable formatting.
});
