/* Pure helpers shared by the viewer and its behavioral regression tests. */
(() => {
  "use strict";
  const clone = (value) => JSON.parse(JSON.stringify(value));

  function buildLayout(base, schemaVersion, camera, positions) {
    const layout = clone(base || {});
    return {
      ...layout,
      format: "connection-analysis-layout",
      schema_version: "1.0",
      analysis_schema_version: schemaVersion,
      camera: { ...(layout.camera || {}), ...camera },
      nodes: {
        ...(layout.nodes || {}),
        ...Object.fromEntries([...positions].map(([id, point]) => [id, { ...layout.nodes?.[id], ...point }])),
      },
      annotations: clone(layout.annotations || []),
    };
  }

  function annotationsFor(layout, meta, nodeId = null, edgeId = null) {
    return [...(layout?.annotations || []), ...(meta?.extensions?.manual_overlay?.annotations || [])]
      .filter((note) => note && typeof note === "object" && (
        (nodeId && note.node_id === nodeId) || (edgeId && note.edge_id === edgeId)
        || (!note.node_id && !note.edge_id)
      ));
  }

  function contextPositions(result) {
    const positions = new Map([[result.focus_id, { x: 0, y: 0 }]]);
    const incoming = new Set(result.edges.filter((edge) => edge.target_id === result.focus_id).map((edge) => edge.source_id));
    const outgoing = new Set(result.edges.filter((edge) => edge.source_id === result.focus_id).map((edge) => edge.target_id));
    const groups = [[], [], []];
    result.nodes.filter((node) => node.id !== result.focus_id).forEach((node) => {
      groups[incoming.has(node.id) ? 0 : outgoing.has(node.id) ? 1 : 2].push(node);
    });
    groups.forEach((nodes, group) => nodes.forEach((node, index) => {
      const column = Math.floor(index / 12);
      const rows = Math.min(12, nodes.length - column * 12);
      positions.set(node.id, {
        x: group === 0 ? -330 - column * 300 : group === 1 ? 330 + column * 300 : (column - 1) * 300,
        y: (index % 12 - (rows - 1) / 2) * 72 + (group === 2 ? 600 : 0),
      });
    }));
    return positions;
  }

  function diagnosticPage(items, { severity = "all", file = "", offset = 0, limit = 200 } = {}) {
    const rank = { error: 0, warning: 1, info: 2 };
    const filtered = items.filter((item) => (severity === "all" || item.severity === severity)
      && (item.file || "").toLowerCase().includes(file.toLowerCase()));
    filtered.sort((a, b) => rank[a.severity] - rank[b.severity]
      || (a.file || "").localeCompare(b.file || "") || (a.span?.start_line || 0) - (b.span?.start_line || 0));
    return { diagnostics: filtered.slice(offset, offset + limit), total: filtered.length, total_all: items.length,
      offset, limit, next_offset: offset + limit < filtered.length ? offset + limit : null };
  }

  const api = { buildLayout, annotationsFor, contextPositions, diagnosticPage };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ConnectionMapExploration = api;
})();
