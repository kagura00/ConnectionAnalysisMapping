(() => {
  "use strict";

  const ZOOM_MIN = 0.15;
  const ZOOM_MAX = 4.0;
  const NODE_LABEL_ZOOM = 0.55;
  const COMPACT_NODE_WIDTH = 112;
  const COMPACT_LABEL_LENGTH = 18;
  const NODE_HIT_SLOP = 2;
  const EDGE_HIT_RADIUS = 7;
  const EXPLORATION_PIN_LIMIT = 24;
  const EXPLORATION_MODULE_LIMIT = 4;
  // A large viewport must not block the main thread while every visible
  // relation is painted.  The render limit is a safety ceiling, while the
  // hit-test limit keeps pointer selection bounded even when the canvas has
  // many more painted edges.
  const EDGE_RENDER_LIMIT = 24000;
  const EDGE_HIT_LIMIT = EDGE_RENDER_LIMIT;
  const EDGE_RENDER_FRAME_BUDGET_MS = 8;
  const MAX_DIAGNOSTIC_CHUNKS = 8;
  const REPOSITORY_STORAGE_KEY = "connection-map.activeRepositoryId";
  const NODE_COLORS = {
    module: "#64748b",
    namespace: "#94a3b8",
    class: "#c084fc",
    function: "#38bdf8",
    method: "#22d3ee",
    lambda: "#2dd4bf",
    interface: "#a78bfa",
    type: "#8b5cf6",
    element: "#f472b6",
    style_rule: "#fb923c",
    external: "#f59e0b",
    unknown: "#fb7185",
  };
  const EDGE_STYLES = {
    contains: { color: "#64748b", dash: [2, 5], head: "triangle" },
    imports: { color: "#22d3ee", dash: [], head: "triangle" },
    dynamic_imports: { color: "#f59e0b", dash: [8, 5], head: "diamond" },
    calls: { color: "#818cf8", dash: [], head: "triangle" },
    inherits: { color: "#c084fc", dash: [], head: "open" },
    uses: { color: "#2dd4bf", dash: [4, 4], head: "triangle" },
    exports: { color: "#a78bfa", dash: [3, 3], head: "triangle" },
    references: { color: "#f472b6", dash: [5, 3], head: "diamond" },
    handles: { color: "#fb7185", dash: [8, 4], head: "diamond" },
    styles: { color: "#fb923c", dash: [2, 3], head: "triangle" },
    reads: { color: "#60a5fa", dash: [6, 3], head: "triangle" },
    writes: { color: "#f97316", dash: [], head: "triangle" },
    joins: { color: "#a78bfa", dash: [2, 2], head: "diamond" },
    defines: { color: "#34d399", dash: [], head: "open" },
    triggers: { color: "#e879f9", dash: [8, 3], head: "diamond" },
  };
  const KIND_LABELS = {
    module: "モジュール",
    namespace: "名前空間",
    class: "クラス",
    function: "関数",
    method: "メソッド",
    lambda: "ラムダ",
    interface: "インターフェース",
    type: "型",
    element: "HTML要素",
    style_rule: "CSSルール",
    external: "外部",
    unknown: "未解決",
  };
  const EDGE_LABELS = {
    contains: "包含",
    imports: "読み込み",
    dynamic_imports: "動的読み込み",
    calls: "呼び出し",
    inherits: "継承",
    uses: "利用",
    exports: "公開",
    references: "参照",
    handles: "イベント処理",
    styles: "スタイル適用",
    reads: "読み取り",
    writes: "書き込み",
    joins: "結合",
    defines: "定義",
    triggers: "トリガー",
  };
  const EXECUTION_LABELS = {
    sync: "同期",
    async: "非同期",
    generator: "ジェネレータ",
    async_generator: "非同期ジェネレータ",
    suspend: "中断可能",
  };
  const RESOLUTION_LABELS = {
    resolved: "解決済み",
    external: "外部",
    unresolved: "未解決",
    unsupported: "未対応",
  };
  const PROVENANCE_LABELS = {
    ast: "構文解析",
    lsp: "言語サービス",
    runtime: "実行時情報",
    manual: "手動追加",
    unknown: "不明",
  };
  const LANGUAGE_LABELS = {
    python: "Python",
    html: "HTML",
    css: "CSS",
    javascript: "JavaScript",
    typescript: "TypeScript",
    vbnet: "Visual Basic .NET",
    vba: "VBA",
    lua: "Lua",
    haskell: "Haskell",
    perl: "Perl",
    matlab: "MATLAB",
    cobol: "COBOL",
    fortran: "Fortran",
    r: "R",
    "objective-c": "Objective-C",
    cuda: "CUDA C/C++",
    groovy: "Groovy",
    fsharp: "F#",
    assembly: "Assembly",
    hcl: "HCL",
    gdscript: "GDScript",
    elixir: "Elixir",
    zig: "Zig",
    julia: "Julia",
    pascal: "Delphi / Object Pascal",
    erlang: "Erlang",
    c: "C",
    cpp: "C++",
    java: "Java",
    csharp: "C#",
    go: "Go",
    rust: "Rust",
    php: "PHP",
    ruby: "Ruby",
    kotlin: "Kotlin",
    swift: "Swift",
    bash: "Bash",
    "posix-shell": "POSIX Shell",
    powershell: "PowerShell",
    dart: "Dart",
    scala: "Scala",
    mysql: "MySQL",
    postgresql: "PostgreSQL",
    sqlite: "SQLite",
    sqlserver: "SQL Server / T-SQL",
    oracle: "Oracle",
  };

  const canvas = document.getElementById("graph");
  const context = canvas.getContext("2d");
  const statusElement = document.getElementById("status");
  const statsElement = document.getElementById("stats");
  const searchElement = document.getElementById("search");
  const searchResultsElement = document.getElementById("search-results");
  const tocElement = document.getElementById("toc");
  const appShell = document.querySelector(".app-shell");
  const detailsElement = document.getElementById("details");
  const toggleDetailsElement = document.getElementById("toggle-details");
  const tocSection = document.getElementById("toc-section");
  const legendSection = document.getElementById("legend-section");
  const diagnosticsSection = document.getElementById("diagnostics-section");
  const diagnosticsSummaryElement = document.getElementById("diagnostics-summary");
  const diagnosticsElement = document.getElementById("diagnostics");
  const languageFilterElement = document.getElementById("language-filter");
  const repositorySelectorElement = document.getElementById("repository-selector");
  const repositorySelectorLabelElement = document.querySelector(".repository-selector-label");
  const validationStatusElement = document.getElementById("validation-status");
  const layoutTools = document.getElementById("layout-tools");
  const saveLayoutElement = document.getElementById("save-layout");
  const loadLayoutElement = document.getElementById("load-layout");
  const layoutFileElement = document.getElementById("layout-file");
  let detailsReturnFocus = null;
  const state = {
    dataBase: "",
    workspaceMode: false,
    repositoryCatalog: null,
    activeRepositoryId: null,
    repositoryRequestId: 0,
    document: null,
    bundle: null,
    nodes: [],
    edges: [],
    overviewEdges: [],
    nodeById: new Map(),
    positionById: new Map(),
    moduleByNodeId: new Map(),
    modules: [],
    nodesByModule: new Map(),
    moduleBounds: new Map(),
    modulePairEdges: new Map(),
    modulePairKeysByModule: new Map(),
    lowEdgeGroups: new Map(),
    visibleNodes: [],
    visibleEdges: [],
    selectedNodeId: null,
    selectedEdgeId: null,
    searchMatches: new Set(),
    layoutOverrides: new Map(),
    camera: { x: 0, y: 0, zoom: 1 },
    pointer: null,
    width: 0,
    height: 0,
    devicePixelRatio: 1,
    loadedNodeChunks: new Map(),
    loadedEdgeChunks: new Map(),
    loadedOverviewEdgeChunks: new Map(),
    oversizedChunkKey: null,
    pendingChunks: new Map(),
    chunkCache: null,
    loadingTimer: null,
    lastLoadSignature: null,
    searchRequestId: 0,
    searchResultTruncated: false,
    searchNodeHints: new Map(),
    pinnedNodeIds: new Set(),
    focusedModuleIds: new Set(),
    pendingBundleChunks: { node: 0, edge: 0, overview: 0 },
    omittedEdgeGroups: 0,
    edgeRenderFrame: null,
    edgeRenderToken: 0,
    edgeRenderState: null,
    edgeRenderStatus: null,
    diagnostics: [],
    diagnosticsLoaded: false,
    diagnosticsTruncated: false,
    diagnosticsLoading: null,
    selectionRequestId: 0,
    selectionLoad: null,
    availableLanguages: [],
    activeLanguages: new Set(),
    languageByNodeId: new Map(),
    virtualModules: new Map(),
  };

  class LruChunkCache {
    constructor(maxBytes, maxEntries, onEvict, isPinned = () => false) {
      this.maxBytes = maxBytes;
      this.maxEntries = maxEntries;
      this.onEvict = onEvict;
      this.isPinned = isPinned;
      this.entries = new Map();
      this.bytes = 0;
    }

    get(key) {
      const entry = this.entries.get(key);
      if (!entry) return null;
      this.entries.delete(key);
      this.entries.set(key, entry);
      return entry.value;
    }

    has(key) {
      return this.entries.has(key);
    }

    set(key, value, bytes) {
      const previous = this.entries.get(key);
      if (previous) this.bytes -= previous.bytes;
      this.entries.delete(key);
      this.entries.set(key, { value, bytes });
      this.bytes += bytes;
      this.evict();
      return this.entries.has(key);
    }

    evict() {
      while (this.entries.size > this.maxEntries || this.bytes > this.maxBytes) {
        // Pins are an eviction preference, not a license to exceed the
        // configured cache ceiling.  A selected node may span many chunks;
        // evicting the oldest pinned chunk is safe because it can be fetched
        // again when the user follows that relation.
        const oldest = [...this.entries.keys()].find((key) => !this.isPinned(key))
          ?? this.entries.keys().next().value;
        if (oldest === undefined) return;
        const entry = this.entries.get(oldest);
        this.entries.delete(oldest);
        this.bytes -= entry.bytes;
        this.onEvict(oldest);
      }
    }
  }

  state.chunkCache = new LruChunkCache(12 * 1024 * 1024, 8, (key) => evictChunk(key), (key) => isChunkPinned(key));

  function resetChunkCache() {
    if (state.loadingTimer !== null) {
      clearTimeout(state.loadingTimer);
      state.loadingTimer = null;
    }
    state.pendingChunks.clear();
    state.lastLoadSignature = null;
    state.oversizedChunkKey = null;
    state.chunkCache = new LruChunkCache(12 * 1024 * 1024, 8, (key) => evictChunk(key), (key) => isChunkPinned(key));
  }

  function cancelEdgeRender() {
    if (state.edgeRenderFrame !== null) {
      cancelAnimationFrame(state.edgeRenderFrame);
      state.edgeRenderFrame = null;
    }
    state.edgeRenderToken += 1;
    state.edgeRenderState = null;
  }

  function setStatus(message, isError = false) {
    statusElement.textContent = message;
    statusElement.className = isError ? "status severity-error" : "status";
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("ja-JP");
  }

  function normalizeSearchText(value) {
    return String(value).normalize("NFKC").toLowerCase();
  }

  function setDetailsOpen(open) {
    const wasOpen = detailsElement.classList.contains("is-open");
    if (open && !wasOpen && document.activeElement instanceof HTMLElement && document.activeElement !== document.body) {
      detailsReturnFocus = document.activeElement;
    }
    detailsElement.classList.toggle("is-open", open);
    detailsElement.classList.toggle("is-closed", !open);
    appShell.classList.toggle("details-closed", !open);
    toggleDetailsElement.setAttribute("aria-expanded", String(open));
    toggleDetailsElement.textContent = open ? "詳細を隠す" : "詳細を表示";
    detailsElement.setAttribute("aria-hidden", String(!open));
    detailsElement.inert = !open;
    if (!open && detailsReturnFocus instanceof HTMLElement && document.contains(detailsReturnFocus)) {
      detailsReturnFocus.focus();
      detailsReturnFocus = null;
    }
  }

  function setStats(visibleNodes = null, visibleEdges = null) {
    const counts = state.document?.meta?.counts || {};
    const statusParts = [];
    if (visibleNodes !== null) {
      const pending = state.pendingBundleChunks;
      const pendingCount = pending.node + pending.edge + pending.overview;
      if (pendingCount) statusParts.push(`未読込 ${formatNumber(pendingCount)}チャンク`);
      const edgeRender = state.edgeRenderStatus;
      if (edgeRender?.active) {
        statusParts.push(`接続描画中 ${formatNumber(edgeRender.drawn)} / ${formatNumber(edgeRender.total)}`);
      }
      if (state.omittedEdgeGroups) statusParts.push(`表示上限で${formatNumber(state.omittedEdgeGroups)}接続群省略`);
    }
    const suffix = visibleNodes === null
      ? ""
      : ` · ズーム ${state.camera.zoom.toFixed(2)} · 表示 ${formatNumber(visibleNodes)}ノード / ${formatNumber(visibleEdges)}線${statusParts.length ? ` · ${statusParts.join(" · ")}` : ""}`;
    statsElement.textContent = `${formatNumber(counts.nodes ?? state.nodes.length)} ノード · ${formatNumber(counts.edges ?? state.edges.length)} 接続 · ${formatNumber(counts.diagnostics ?? state.diagnostics.length)} 診断${suffix}`;
  }

  function evictChunk(key) {
    if (state.oversizedChunkKey === key) state.oversizedChunkKey = null;
    const [kind, indexText] = key.split(":");
    const index = Number(indexText);
    if (!Number.isInteger(index)) return;
    let changed = false;
    if (kind === "node") {
      const chunk = state.loadedNodeChunks.get(index);
      changed = state.loadedNodeChunks.delete(index);
      if (changed) forgetChunkIndexes("node", index, chunk || []);
    }
    if (kind === "edge") {
      const chunk = state.loadedEdgeChunks.get(index);
      changed = state.loadedEdgeChunks.delete(index);
      if (changed) forgetChunkIndexes("edge", index, chunk || []);
    }
    if (kind === "overview-edge") changed = state.loadedOverviewEdgeChunks.delete(index);
    if (changed && state.bundle) rebuildBundleGraph();
  }

  function forgetChunkIndexes(kind, index, chunk) {
    if (!state.bundle) return;
    if (kind === "node") {
      const range = state.bundle.nodeChunkRanges[index];
      chunk.forEach((node, offset) => {
        const ordinal = range ? range.start + offset : null;
        if (state.bundle.nodeOrdinalById.get(node.id) === ordinal) state.bundle.nodeOrdinalById.delete(node.id);
      });
      return;
    }
    if (kind === "edge") {
      chunk.forEach((edge) => {
        if (state.bundle.edgeChunkIndexById.get(edge.id) === index) state.bundle.edgeChunkIndexById.delete(edge.id);
        for (const nodeId of [edge.source_id, edge.target_id]) {
          const indices = state.bundle.edgeChunkIndicesByNodeId.get(nodeId);
          if (!indices) continue;
          indices.delete(index);
          if (!indices.size) state.bundle.edgeChunkIndicesByNodeId.delete(nodeId);
        }
      });
    }
  }

  function isChunkPinned(key) {
    if (!state.pinnedNodeIds.size && !state.focusedModuleIds.size && !state.selectedEdgeId) return false;
    const [kind, indexText] = key.split(":");
    const index = Number(indexText);
    if (!Number.isInteger(index)) return false;
    if (kind === "node") {
      return (state.loadedNodeChunks.get(index) || []).some((node) => (
        state.pinnedNodeIds.has(node.id)
      ));
    }
    if (kind === "edge") {
      return (state.loadedEdgeChunks.get(index) || []).some((edge) => (
        edge.id === state.selectedEdgeId
        || state.pinnedNodeIds.has(edge.source_id)
        || state.pinnedNodeIds.has(edge.target_id)
      ));
    }
    if (kind === "overview-edge" && state.bundle) {
      return (state.loadedOverviewEdgeChunks.get(index) || []).some((group) => {
        const edge = overviewGroupToEdge(group);
        return edge?.id === state.selectedEdgeId
          || state.focusedModuleIds.has(edge?.source_id)
          || state.focusedModuleIds.has(edge?.target_id);
      });
    }
    return false;
  }

  function pinNode(nodeId) {
    if (!nodeId) return;
    state.pinnedNodeIds.delete(nodeId);
    state.pinnedNodeIds.add(nodeId);
    while (state.pinnedNodeIds.size > EXPLORATION_PIN_LIMIT) {
      state.pinnedNodeIds.delete(state.pinnedNodeIds.values().next().value);
    }
  }

  function pinModule(moduleId) {
    if (!moduleId) return;
    state.focusedModuleIds.delete(moduleId);
    state.focusedModuleIds.add(moduleId);
    while (state.focusedModuleIds.size > EXPLORATION_MODULE_LIMIT) {
      state.focusedModuleIds.delete(state.focusedModuleIds.values().next().value);
    }
  }

  function pinEdgeEndpoints(edge) {
    pinNode(edge.source_id);
    pinNode(edge.target_id);
  }

  function hashText(value) {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function nodeLabel(node) {
    return node.display_name || node.qualified_name || node.id;
  }

  function nodeVisualLabel(node) {
    if (node.kind === "module" && node.file) return node.file;
    return nodeLabel(node);
  }

  function compactNodeLabel(node) {
    const label = nodeVisualLabel(node);
    if (label.length <= COMPACT_LABEL_LENGTH) return label;
    if (node.kind !== "module") return truncate(label, COMPACT_LABEL_LENGTH);
    return `…${label.slice(-(COMPACT_LABEL_LENGTH - 1))}`;
  }

  function kindLabel(kind) {
    return KIND_LABELS[kind] || kind || "不明";
  }

  function nodeKindLabel(node) {
    const sqlKind = node?.extensions?.sql_object_type;
    if (typeof sqlKind === "string" && sqlKind) {
      return {
        table: "SQLテーブル",
        view: "SQLビュー",
        materialized_view: "SQLマテリアライズドビュー",
        function: "SQL関数",
        procedure: "SQLプロシージャ",
        trigger: "SQLトリガー",
        cte: "SQL共通テーブル式",
        subquery: "SQLサブクエリ",
        select: "SQL SELECT",
        insert: "SQL INSERT",
        update: "SQL UPDATE",
        delete: "SQL DELETE",
        merge: "SQL MERGE",
      }[sqlKind] || `SQL ${sqlKind}`;
    }
    return kindLabel(node?.kind);
  }

  function edgeLabel(relationType) {
    return EDGE_LABELS[relationType] || relationType || "その他";
  }

  function returnLabel(value) {
    return {
      no_explicit_return: "明示的なreturnなし",
      returns_value: "値を返す",
      returns_none: "Noneを返す",
      mixed: "値とNoneが混在",
      unknown: "不明",
    }[value] || value;
  }

  function executionLabel(value) {
    return EXECUTION_LABELS[value] || value || "不明";
  }

  function resolutionLabel(value) {
    return RESOLUTION_LABELS[value] || value || "不明";
  }

  function provenanceLabel(value) {
    return PROVENANCE_LABELS[value] || value || "不明";
  }

  function languageLabel(value) {
    return LANGUAGE_LABELS[value] || value || "不明";
  }

  function nodeLanguage(node) {
    const extensionLanguage = node?.extensions?.language;
    if (typeof extensionLanguage === "string" && extensionLanguage) return extensionLanguage;
    if (typeof node?.language === "string" && node.language) return node.language;
    const indexedLanguage = node?.id ? state.languageByNodeId.get(node.id) : null;
    if (indexedLanguage) return indexedLanguage;
    const documentLanguage = state.document?.meta?.language;
    return typeof documentLanguage === "string" && documentLanguage !== "mixed" ? documentLanguage : null;
  }

  function indexNodeLanguages(nodes) {
    nodes.forEach((node) => {
      const language = nodeLanguage(node);
      if (language) state.languageByNodeId.set(node.id, language);
    });
  }

  function isLanguageVisible(language) {
    return !state.activeLanguages.size || !language || state.activeLanguages.has(language);
  }

  function isNodeLanguageVisible(node) {
    return isLanguageVisible(nodeLanguage(node));
  }

  function isEdgeLanguageVisible(edge, sourceId = edge.source_id, targetId = edge.target_id) {
    const source = state.nodeById.get(sourceId);
    const target = state.nodeById.get(targetId);
    const sourceLanguage = source ? nodeLanguage(source) : state.languageByNodeId.get(sourceId);
    const targetLanguage = target ? nodeLanguage(target) : state.languageByNodeId.get(targetId);
    return isLanguageVisible(sourceLanguage) && isLanguageVisible(targetLanguage);
  }

  function setupLanguageFilter(meta) {
    const fromMeta = [
      ...(Array.isArray(meta?.languages) ? meta.languages : []),
      ...(typeof meta?.language === "string" && meta.language !== "mixed" ? [meta.language] : []),
    ];
    const inferred = state.nodes
      .map((node) => nodeLanguage(node))
      .filter((language) => typeof language === "string" && language);
    state.availableLanguages = [...new Set([...fromMeta, ...inferred])];
    state.activeLanguages.clear();
    renderLanguageFilter();
  }

  function renderLanguageFilter() {
    languageFilterElement.replaceChildren();
    if (!state.availableLanguages.length) {
      languageFilterElement.append(emptyList("言語情報なし"));
      return;
    }
    const allChoice = document.createElement("label");
    const allInput = document.createElement("input");
    allInput.type = "checkbox";
    allInput.checked = state.activeLanguages.size === 0;
    allInput.addEventListener("change", () => {
      state.activeLanguages.clear();
      applyLanguageFilter();
    });
    allChoice.append(allInput, document.createTextNode("すべて"));
    languageFilterElement.append(allChoice);
    state.availableLanguages.forEach((language) => {
      const choice = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = state.activeLanguages.has(language);
      input.addEventListener("change", () => {
        if (input.checked) state.activeLanguages.add(language);
        else state.activeLanguages.delete(language);
        if (state.activeLanguages.size === state.availableLanguages.length) state.activeLanguages.clear();
        applyLanguageFilter();
      });
      choice.append(input, document.createTextNode(languageLabel(language)));
      languageFilterElement.append(choice);
    });
  }

  function applyLanguageFilter() {
    const selectedNode = state.selectedNodeId ? state.nodeById.get(state.selectedNodeId) : null;
    const selectedEdge = state.selectedEdgeId
      ? state.edges.find((edge) => edge.id === state.selectedEdgeId)
        || state.overviewEdges.find((edge) => edge.id === state.selectedEdgeId)
        || state.visibleEdges.find((item) => item.edge.id === state.selectedEdgeId)?.edge
      : null;
    if ((selectedNode && !isNodeLanguageVisible(selectedNode)) || (selectedEdge && !isEdgeLanguageVisible(selectedEdge))) {
      clearSelection();
    }
    renderLanguageFilter();
    renderToc();
    void renderSearchResults(searchElement.value);
    const summary = state.activeLanguages.size
      ? [...state.activeLanguages].map(languageLabel).join(" / ")
      : "すべて";
    setStatus(`表示言語: ${summary}`);
    draw();
  }

  function parentModuleId(nodeId) {
    let current = state.nodeById.get(nodeId);
    const visited = new Set();
    while (current && current.parent_id && !visited.has(current.id)) {
      visited.add(current.id);
      current = state.nodeById.get(current.parent_id);
    }
    return current && current.kind === "module" ? current.id : null;
  }

  function virtualModuleForNode(node) {
    const kind = node?.kind === "external" ? "external" : "unresolved";
    const language = nodeLanguage(node) || "unknown";
    const label = kind === "external" ? "外部" : "未解決";
    const id = `app:virtual-module:${kind}:${language}`;
    return {
      id,
      kind: "module",
      qualified_name: `<${label}>:${language}`,
      display_name: `${label} (${language})`,
      file: null,
      span: null,
      parent_id: null,
      visibility: "unknown",
      extensions: { language, virtual: true, virtual_scope: kind },
    };
  }

  function buildPresentationNodeIndex() {
    const moduleByFile = new Map(
      state.nodes
        .filter((node) => node.kind === "module" && node.file)
        .map((node) => [node.file, node.id]),
    );
    const virtualModules = new Map();
    state.moduleByNodeId.clear();
    state.nodes.forEach((node) => {
      let moduleId = node.kind === "module" ? node.id : parentModuleId(node.id);
      if (!moduleId && node.file) moduleId = moduleByFile.get(node.file) || null;
      if (!moduleId && node.kind !== "module") {
        const virtual = virtualModuleForNode(node);
        moduleId = virtual.id;
        virtualModules.set(virtual.id, virtual);
      }
      if (moduleId) state.moduleByNodeId.set(node.id, moduleId);
    });
    state.virtualModules = virtualModules;
    const existingIds = new Set(state.nodes.map((node) => node.id));
    const additions = [...virtualModules.values()].filter((node) => !existingIds.has(node.id));
    if (additions.length) {
      state.nodes = [...state.nodes, ...additions];
      additions.forEach((node) => state.nodeById.set(node.id, node));
      indexNodeLanguages(additions);
    }
  }

  function buildBundleNodeIndex() {
    state.moduleByNodeId.clear();
    state.nodes.forEach((node) => {
      if (node.kind === "module") state.moduleByNodeId.set(node.id, node.id);
    });
    if (state.bundle) {
      state.loadedNodeChunks.forEach((chunk, chunkIndex) => {
        const range = state.bundle.nodeChunkRanges[chunkIndex];
        chunk.forEach((node, offset) => {
          const ordinal = range ? range.start + offset : null;
          const moduleId = ordinal === null ? null : state.bundle.moduleIdByNodeOrdinal.get(ordinal);
          if (moduleId) state.moduleByNodeId.set(node.id, moduleId);
        });
      });
    }
    state.nodes.forEach((node) => {
      if (state.moduleByNodeId.has(node.id)) return;
      const moduleId = parentModuleId(node.id);
      if (moduleId) state.moduleByNodeId.set(node.id, moduleId);
    });
  }

  function moduleIdsForNodeId(nodeId) {
    const moduleIds = new Set();
    const known = state.moduleByNodeId.get(nodeId);
    if (known) moduleIds.add(known);
    const hint = state.searchNodeHints.get(nodeId);
    if (hint?.moduleId) moduleIds.add(hint.moduleId);
    if (!state.bundle) return moduleIds;
    const nodeChunkIndexes = new Set();
    if (hint?.nodeChunkIndex !== undefined) nodeChunkIndexes.add(hint.nodeChunkIndex);
    state.loadedNodeChunks.forEach((chunk, index) => {
      if (chunk.some((node) => node.id === nodeId)) nodeChunkIndexes.add(index);
    });
    nodeChunkIndexes.forEach((index) => {
      state.bundle.moduleIdsByNodeChunk.get(index)?.forEach((moduleId) => moduleIds.add(moduleId));
    });
    if (!moduleIds.size) {
      const suffix = ":module";
      const module = state.bundle.moduleNodes.find((candidate) => {
        if (nodeId === candidate.id) return true;
        const prefix = candidate.id.endsWith(suffix)
          ? `${candidate.id.slice(0, -suffix.length)}:`
          : `${candidate.id}:`;
        return nodeId.startsWith(prefix);
      });
      if (module) moduleIds.add(module.id);
    }
    return moduleIds;
  }

  function moduleIdForNodeId(nodeId) {
    return moduleIdsForNodeId(nodeId).values().next().value || null;
  }

  function moduleIdsForEdge(edge) {
    const moduleIds = new Set([
      ...moduleIdsForNodeId(edge?.source_id),
      ...moduleIdsForNodeId(edge?.target_id),
    ]);
    if (!state.bundle || !edge?.id) return moduleIds;
    const edgeChunkIndices = [...state.loadedEdgeChunks.entries()]
      .filter(([, chunk]) => chunk.some((candidate) => candidate.id === edge.id))
      .map(([index]) => index);
    edgeChunkIndices.forEach((index) => {
      state.bundle.moduleIdsByEdgeChunk.get(index)?.forEach((moduleId) => moduleIds.add(moduleId));
    });
    return moduleIds;
  }

  function buildPositions() {
    state.positionById = new Map();
    const modules = state.nodes.filter((node) => node.kind === "module");
    const children = new Map();
    state.nodes.forEach((node) => {
      if (node.parent_id) {
        if (!children.has(node.parent_id)) children.set(node.parent_id, []);
        children.get(node.parent_id).push(node);
      }
    });
    children.forEach((items) => items.sort((a, b) => a.id.localeCompare(b.id)));
    const columns = Math.max(1, Math.ceil(Math.sqrt(Math.max(modules.length, 1))));
    modules.forEach((node, index) => {
      state.positionById.set(node.id, {
        x: (index % columns) * 820,
        y: Math.floor(index / columns) * 620,
      });
    });
    state.nodes
      .filter((node) => node.kind !== "module")
      .sort((a, b) => depthOf(a) - depthOf(b) || a.id.localeCompare(b.id))
      .forEach((node) => {
        const parentPosition = state.positionById.get(node.parent_id) || state.positionById.get(state.moduleByNodeId.get(node.id));
        const base = parentPosition || { x: 0, y: 0 };
        const siblings = children.get(node.parent_id) || [];
        const index = Math.max(0, siblings.findIndex((item) => item.id === node.id));
        const angle = ((hashText(node.id) % 360) * Math.PI) / 180;
        const radius = 150 + (index % 5) * 34;
        state.positionById.set(node.id, {
          x: base.x + Math.cos(angle) * radius + (index % 4) * 45,
          y: base.y + Math.sin(angle) * radius + Math.floor(index / 4) * 45,
        });
      });
    state.layoutOverrides.forEach((position, nodeId) => {
      if (state.nodeById.has(nodeId)) state.positionById.set(nodeId, { x: position.x, y: position.y });
    });
  }

  function depthOf(node) {
    let depth = 0;
    let current = node;
    const visited = new Set();
    while (current && current.parent_id && !visited.has(current.id)) {
      visited.add(current.id);
      depth += 1;
      current = state.nodeById.get(current.parent_id);
    }
    return depth;
  }

  function isFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function applyLayout(layout) {
    if (!layout) return false;
    if (layout.format !== "connection-analysis-layout" || layout.schema_version !== "1.0") {
      throw new Error("layout.json の形式または schema version が不正です");
    }
    if (layout.analysis_schema_version && layout.analysis_schema_version !== state.document.schema_version) {
      throw new Error("layout.json と analysis.json の schema version が一致しません");
    }
    if (layout.annotations !== undefined && !Array.isArray(layout.annotations)) {
      throw new Error("layout.json の annotations が不正です");
    }
    if (layout.nodes !== undefined && (layout.nodes === null || typeof layout.nodes !== "object" || Array.isArray(layout.nodes))) {
      throw new Error("layout.json の nodes が不正です");
    }
    const positions = Object.entries(layout.nodes || {});
    positions.forEach(([nodeId, position]) => {
      if (!position || typeof position !== "object" || !isFiniteNumber(position.x) || !isFiniteNumber(position.y)) {
        throw new Error(`layout.json の node 座標が不正です: ${nodeId}`);
      }
    });
    const camera = layout.camera;
    if (camera !== undefined && camera !== null && (typeof camera !== "object" || Array.isArray(camera) || !isFiniteNumber(camera.x) || !isFiniteNumber(camera.y) || !isFiniteNumber(camera.zoom) || camera.zoom <= 0)) {
      throw new Error("layout.json の camera が不正です");
    }
    state.layoutOverrides.clear();
    positions.forEach(([nodeId, position]) => {
      const override = { x: position.x, y: position.y };
      state.layoutOverrides.set(nodeId, override);
      if (state.nodeById.has(nodeId)) state.positionById.set(nodeId, override);
    });
    if (camera === undefined || camera === null) return false;
    state.camera = {
      x: camera.x,
      y: camera.y,
      zoom: Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, camera.zoom)),
    };
    return true;
  }

  function setupDocument(document, layout) {
    resetChunkCache();
    // A repository switch must not reuse unsaved coordinates or camera state
    // from a different graph when that repository has no layout snapshot.
    state.layoutOverrides.clear();
    state.camera = { x: 0, y: 0, zoom: 1 };
    state.bundle = null;
    state.document = document;
    state.diagnostics = Array.isArray(document.diagnostics) ? document.diagnostics : [];
    state.diagnosticsLoaded = true;
    state.diagnosticsTruncated = false;
    state.diagnosticsLoading = null;
    state.pendingBundleChunks = { node: 0, edge: 0, overview: 0 };
    state.omittedEdgeGroups = 0;
    state.focusedModuleIds.clear();
    state.nodes = document.nodes || [];
    state.edges = document.edges || [];
    state.overviewEdges = [];
    state.searchNodeHints.clear();
    state.searchResultTruncated = false;
    state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));
    state.languageByNodeId.clear();
    indexNodeLanguages(state.nodes);
    buildPresentationNodeIndex();
    state.searchNodeHints.forEach((hint, nodeId) => {
      if (hint.moduleId && state.nodeById.has(nodeId)) state.moduleByNodeId.set(nodeId, hint.moduleId);
    });
    buildPositions();
    buildRenderIndexes();
    setupLanguageFilter(document.meta);
    const hasLayoutCamera = applyLayout(layout);
    renderToc();
    renderSearchResults("");
    renderDiagnostics();
    setStats();
    setStatus(`解析完了 · ${document.meta?.analyzer?.name || "解析器不明"}`);
    setDetailsOpen(window.innerWidth > 1000);
    if (hasLayoutCamera) draw();
    else fitView();
  }

  function setupBundle(index, overview, layout) {
    resetChunkCache();
    // Layouts are repository-scoped; clear the previous session before
    // building positions so a missing layout starts from a clean fit view.
    state.layoutOverrides.clear();
    state.camera = { x: 0, y: 0, zoom: 1 };
    state.loadedNodeChunks.clear();
    state.loadedEdgeChunks.clear();
    state.loadedOverviewEdgeChunks.clear();
    state.focusedModuleIds.clear();
    state.pinnedNodeIds.clear();
    state.pendingBundleChunks = { node: 0, edge: 0, overview: 0 };
    state.omittedEdgeGroups = 0;
    const meta = { ...(index.meta || {}), counts: index.counts };
    state.bundle = {
      index,
      overview,
      moduleNodes: overview.modules || [],
      moduleIndexById: new Map((overview.modules || []).map((node, ordinal) => [node.id, ordinal])),
      nodeChunkEntries: index.chunks.nodes || [],
      edgeChunkEntries: index.chunks.edges || [],
      diagnosticChunkEntries: index.chunks.diagnostics || [],
      overviewEdgeChunkEntries: overview.edge_group_chunks || [],
      searchRecordEntries: index.search?.record_chunks || [],
      nodeChunkRanges: [],
      searchRecordRanges: [],
      moduleIdsByNodeChunk: new Map(),
      moduleIdsByEdgeChunk: new Map(),
      moduleIdByNodeOrdinal: new Map(),
      nodeChunkIndicesByOrdinal: new Map(),
      nodeChunkIndicesByNodeId: new Map(),
      edgeChunkIndicesByOrdinal: new Map(),
      nodeOrdinalById: new Map(),
      edgeChunkIndicesByNodeId: new Map(),
      edgeChunkIndexById: new Map(),
      loadedNodeChunks: state.loadedNodeChunks,
      loadedEdgeChunks: state.loadedEdgeChunks,
      loadedOverviewEdgeChunks: state.loadedOverviewEdgeChunks,
    };
    Object.entries(overview.module_by_node || {}).forEach(([ordinal, moduleIndex]) => {
      const module = state.bundle.moduleNodes[Number(moduleIndex)];
      if (module) state.bundle.moduleIdByNodeOrdinal.set(Number(ordinal), module.id);
    });
    Object.entries(overview.node_chunks_by_node || {}).forEach(([ordinal, indices]) => {
      state.bundle.nodeChunkIndicesByOrdinal.set(Number(ordinal), new Set(indices));
    });
    Object.entries(overview.node_chunks_by_id || {}).forEach(([nodeId, indices]) => {
      state.bundle.nodeChunkIndicesByNodeId.set(nodeId, new Set(indices));
    });
    Object.entries(overview.edge_chunks_by_node || {}).forEach(([ordinal, indices]) => {
      state.bundle.edgeChunkIndicesByOrdinal.set(Number(ordinal), new Set(indices));
    });
    Object.entries(overview.node_chunks_by_module || {}).forEach(([ordinal, indices]) => {
      const module = state.bundle.moduleNodes[Number(ordinal)];
      if (!module) return;
      indices.forEach((index) => {
        if (!state.bundle.moduleIdsByNodeChunk.has(index)) state.bundle.moduleIdsByNodeChunk.set(index, new Set());
        state.bundle.moduleIdsByNodeChunk.get(index).add(module.id);
      });
    });
    Object.entries(overview.edge_chunks_by_module || {}).forEach(([ordinal, indices]) => {
      const module = state.bundle.moduleNodes[Number(ordinal)];
      if (!module) return;
      indices.forEach((index) => {
        if (!state.bundle.moduleIdsByEdgeChunk.has(index)) state.bundle.moduleIdsByEdgeChunk.set(index, new Set());
        state.bundle.moduleIdsByEdgeChunk.get(index).add(module.id);
      });
    });
    let start = 0;
    state.bundle.nodeChunkEntries.forEach((entry, indexNumber) => {
      const end = start + entry.count;
      state.bundle.nodeChunkRanges.push({ index: indexNumber, start, end });
      start = end;
    });
    start = 0;
    state.bundle.searchRecordEntries.forEach((entry, indexNumber) => {
      const end = start + entry.count;
      state.bundle.searchRecordRanges.push({ index: indexNumber, start, end });
      start = end;
    });
    state.document = {
      format: "connection-analysis-map",
      schema_version: index.analysis_schema_version,
      meta,
      nodes: state.bundle.moduleNodes,
      edges: [],
      diagnostics: [],
    };
    state.diagnostics = [];
    state.diagnosticsLoaded = false;
    state.diagnosticsTruncated = false;
    state.diagnosticsLoading = null;
    state.nodes = state.bundle.moduleNodes.slice();
    state.edges = [];
    state.overviewEdges = [];
    state.searchNodeHints.clear();
    state.searchResultTruncated = false;
    state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));
    state.languageByNodeId.clear();
    indexNodeLanguages(state.nodes);
    state.moduleByNodeId.clear();
    state.nodes.forEach((node) => state.moduleByNodeId.set(node.id, node.id));
    buildPositions();
    buildRenderIndexes();
    setupLanguageFilter(meta);
    const hasLayoutCamera = applyLayout(layout);
    renderToc();
    renderSearchResults("");
    renderDiagnostics();
    if (diagnosticsSection.open) void loadBundleDiagnostics();
    setStats();
    setStatus("概要を表示中 · 必要な範囲を読み込みます");
    setDetailsOpen(window.innerWidth > 1000);
    if (hasLayoutCamera) draw();
    else fitView();
  }

  function diagnosticSeverityRank(severity) {
    return { error: 0, warning: 1, info: 2 }[severity] ?? 3;
  }

  function diagnosticLocation(diagnostic) {
    const span = diagnostic?.span;
    if (!span) return diagnostic?.file || "位置不明";
    const location = `${span.start_line ?? "?"}:${span.start_col ?? "?"}`;
    return diagnostic.file ? `${diagnostic.file}:${location}` : location;
  }

  async function focusDiagnostic(diagnostic) {
    const module = state.nodes.find((node) => (
      node.kind === "module"
      && diagnostic.file
      && node.file === diagnostic.file
      && isNodeLanguageVisible(node)
    ));
    if (!module) {
      setStatus(`${diagnostic.file || "対象ファイル"}に対応するノードがありません`);
      return;
    }
    await selectNode(module.id, true);
    setStatus(`診断位置を表示中 · ${diagnosticLocation(diagnostic)}`);
  }

  function renderDiagnostics() {
    const count = state.document?.meta?.counts?.diagnostics ?? state.diagnostics.length;
    if (diagnosticsSummaryElement) diagnosticsSummaryElement.textContent = `解析診断（${formatNumber(count)}件）`;
    if (!diagnosticsElement) return;
    diagnosticsElement.replaceChildren();
    if (state.bundle && !state.diagnosticsLoaded) {
      diagnosticsElement.append(emptyList("開くと診断の詳細を読み込みます。"));
      return;
    }
    if (!state.diagnostics.length) {
      diagnosticsElement.append(emptyList("診断はありません"));
      return;
    }
    const ordered = state.diagnostics
      .slice()
      .sort((left, right) => diagnosticSeverityRank(left.severity) - diagnosticSeverityRank(right.severity));
    const limit = 200;
    ordered.slice(0, limit).forEach((diagnostic) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `diagnostic-item severity-${diagnostic.severity || "info"}`;
      const heading = document.createElement("strong");
      heading.textContent = diagnostic.code || "diagnostic";
      const location = document.createElement("small");
      location.textContent = diagnosticLocation(diagnostic);
      const message = document.createElement("span");
      message.textContent = diagnostic.message || "";
      button.append(heading, location, message);
      button.title = "クリックして該当ファイルへ移動";
      button.addEventListener("click", () => { void focusDiagnostic(diagnostic); });
      diagnosticsElement.append(button);
    });
    if (ordered.length > limit || state.diagnosticsTruncated) {
      const note = document.createElement("div");
      note.className = "empty-list";
      note.textContent = state.diagnosticsTruncated
        ? `先頭${formatNumber(Math.min(limit, ordered.length))}件を表示（診断チャンクの読み込み上限）`
        : `先頭${formatNumber(limit)}件を表示（全${formatNumber(ordered.length)}件）`;
      diagnosticsElement.append(note);
    }
  }

  async function loadBundleDiagnostics() {
    if (!state.bundle || state.diagnosticsLoaded) return;
    if (state.diagnosticsLoading) return state.diagnosticsLoading;
    const repositoryGeneration = state.repositoryRequestId;
    state.diagnosticsLoading = (async () => {
      try {
        const diagnostics = [];
        const entries = state.bundle.diagnosticChunkEntries;
        for (let index = 0; index < entries.length && index < MAX_DIAGNOSTIC_CHUNKS; index += 1) {
          const entry = entries[index];
          diagnostics.push(...await fetchJson(`bundle/${entry.path}`, `diagnostics:${index}`));
          if (diagnostics.length >= 200) break;
        }
        state.diagnostics = diagnostics;
        state.diagnosticsTruncated = diagnostics.length < state.document.meta.counts.diagnostics;
        state.document.diagnostics = state.diagnostics;
        state.diagnosticsLoaded = true;
        renderDiagnostics();
        setStats();
      } catch (error) {
        if (repositoryGeneration !== state.repositoryRequestId) return;
        diagnosticsElement?.replaceChildren(emptyList("診断の読み込みに失敗しました"));
        setStatus(`診断の読み込みに失敗しました: ${error.message}`, true);
      } finally {
        if (repositoryGeneration === state.repositoryRequestId) state.diagnosticsLoading = null;
      }
    })();
    return state.diagnosticsLoading;
  }

  function rebuildBundleGraph() {
    if (!state.bundle) return;
    const detailNodes = [...state.loadedNodeChunks.values()]
      .flat()
      .filter((node) => node.kind !== "module");
    state.nodes = [...state.bundle.moduleNodes, ...detailNodes];
    state.edges = [...state.loadedEdgeChunks.values()].flat();
    state.overviewEdges = [...state.loadedOverviewEdgeChunks.values()]
      .flat()
      .map((group) => overviewGroupToEdge(group))
      .filter(Boolean);
    state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));
    indexNodeLanguages(state.nodes);
    const retainedLanguageIds = new Set([
      ...state.nodes.map((node) => node.id),
      ...state.searchNodeHints.keys(),
    ]);
    state.languageByNodeId.forEach((_, nodeId) => {
      if (!retainedLanguageIds.has(nodeId)) state.languageByNodeId.delete(nodeId);
    });
    buildBundleNodeIndex();
    buildPositions();
    buildRenderIndexes();
  }

  function overviewGroupToEdge(group) {
    if (!state.bundle || !Number.isInteger(group.source) || !Number.isInteger(group.target)) return null;
    const source = state.bundle.moduleNodes[group.source];
    const target = state.bundle.moduleNodes[group.target];
    if (!source || !target) return null;
    const id = `bundle:module-group:${group.source}:${group.target}:${group.relation}:${group.status}`;
    return {
      id,
      source_id: source.id,
      target_id: target.id,
      relation_type: group.relation,
      resolution_status: group.status,
      provenance: "unknown",
      confidence: null,
      source_span: null,
      detail: {
        aggregate: true,
        count: group.count,
        confidence_min: group.confidence_min,
        confidence_max: group.confidence_max,
        representative_edge_id: group.representative_edge_id || null,
        representative_edge_chunk: Number.isInteger(group.representative_edge_chunk)
          ? group.representative_edge_chunk
          : null,
      },
    };
  }

  function buildRenderIndexes() {
    state.modules = state.nodes.filter((node) => node.kind === "module");
    state.nodesByModule = new Map(state.modules.map((module) => [module.id, []]));
    state.nodes.forEach((node) => {
      const moduleId = state.moduleByNodeId.get(node.id);
      if (moduleId && state.nodesByModule.has(moduleId)) state.nodesByModule.get(moduleId).push(node);
    });
    state.modulePairEdges = new Map();
    state.modulePairKeysByModule = new Map(state.modules.map((module) => [module.id, new Set()]));
    state.lowEdgeGroups = new Map();
    state.edges.forEach((edge) => {
      const sourceModule = state.moduleByNodeId.get(edge.source_id);
      const targetModule = state.moduleByNodeId.get(edge.target_id);
      if (!sourceModule || !targetModule) return;
      const pairKey = `${sourceModule}|${targetModule}`;
      if (!state.modulePairEdges.has(pairKey)) state.modulePairEdges.set(pairKey, []);
      state.modulePairEdges.get(pairKey).push(edge);
      state.modulePairKeysByModule.get(sourceModule)?.add(pairKey);
      state.modulePairKeysByModule.get(targetModule)?.add(pairKey);
    });
    const lowEdges = state.bundle ? state.overviewEdges : state.edges;
    lowEdges.forEach((edge) => {
      const sourceModule = state.moduleByNodeId.get(edge.source_id);
      const targetModule = state.moduleByNodeId.get(edge.target_id);
      if (!sourceModule || !targetModule || sourceModule === targetModule) return;
      const lowKey = `${sourceModule}|${targetModule}|${edge.relation_type}|${edge.resolution_status}`;
      if (!state.lowEdgeGroups.has(lowKey)) state.lowEdgeGroups.set(lowKey, { edge, sourceId: sourceModule, targetId: targetModule, count: 0 });
      state.lowEdgeGroups.get(lowKey).count += edge.detail?.count || 1;
    });
    buildModuleBounds();
  }

  function buildModuleBounds() {
    state.moduleBounds = new Map();
    state.modules.forEach((module) => {
      const nodes = [module, ...(state.nodesByModule.get(module.id) || [])];
      const positions = nodes.map((node) => state.positionById.get(node.id)).filter(Boolean);
      if (!positions.length) return;
      const bounds = positions.reduce((current, position) => ({
        minX: Math.min(current.minX, position.x),
        maxX: Math.max(current.maxX, position.x),
        minY: Math.min(current.minY, position.y),
        maxY: Math.max(current.maxY, position.y),
      }), { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
      state.moduleBounds.set(module.id, {
        ...bounds,
      });
    });
  }

  function bundleEntries(kind) {
    if (!state.bundle) return [];
    if (kind === "node") return state.bundle.nodeChunkEntries;
    if (kind === "edge") return state.bundle.edgeChunkEntries;
    return state.bundle.overviewEdgeChunkEntries;
  }

  function dataUrl(url) {
    if (/^(?:https?:)?\/\//.test(url) || url.startsWith("/")) return url;
    return state.dataBase ? `${state.dataBase}/${url.replace(/^\/+/, "")}` : url;
  }

  async function fetchJson(url, cacheKey = null) {
    const repositoryGeneration = state.repositoryRequestId;
    if (cacheKey) {
      const cached = state.chunkCache.get(cacheKey);
      if (cached !== null) return cached;
      if (state.pendingChunks.has(cacheKey)) return state.pendingChunks.get(cacheKey);
    }
    const request = fetch(dataUrl(url), { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${dataUrl(url)}`);
        return response.text();
      })
      .then((text) => {
        if (repositoryGeneration !== state.repositoryRequestId) {
          throw new Error("解析対象が切り替わりました");
        }
        const value = JSON.parse(text);
        if (cacheKey) {
          // JSON text is a lower bound for the parsed object.  The multiplier
          // keeps the browser cache conservative without pretending that JS
          // object memory can be measured exactly from application code.
          const textBytes = new TextEncoder().encode(text).byteLength;
          state.chunkCache.set(cacheKey, value, Math.ceil(textBytes * 2.5));
        }
        return value;
      });
    if (cacheKey) state.pendingChunks.set(cacheKey, request);
    try {
      return await request;
    } finally {
      if (cacheKey && state.pendingChunks.get(cacheKey) === request) state.pendingChunks.delete(cacheKey);
    }
  }

  async function loadBundleChunk(kind, index, { rebuild = true } = {}) {
    if (!state.bundle) return [];
    const targetMap = kind === "node"
      ? state.loadedNodeChunks
      : kind === "edge" ? state.loadedEdgeChunks : state.loadedOverviewEdgeChunks;
    const key = `${kind === "overview" ? "overview-edge" : kind}:${index}`;
    if (targetMap.has(index)) {
      // Accessing a loaded chunk is also a cache access.  Without this call,
      // a frequently followed relation could be evicted before colder data.
      state.chunkCache.get(key);
      return targetMap.get(index);
    }
    const entry = bundleEntries(kind)[index];
    if (!entry) return [];
    if (state.oversizedChunkKey && state.oversizedChunkKey !== key) {
      evictChunk(state.oversizedChunkKey);
    }
    const value = await fetchJson(`bundle/${entry.path}`, key);
    if (!state.chunkCache.has(key)) {
      // A single chunk larger than the cache ceiling cannot be accounted for
      // by the LRU.  Keep only that one chunk as a transient view so a large
      // repository remains navigable; the next chunk replaces it.
      state.oversizedChunkKey = key;
    }
    if (kind === "node") {
      const range = state.bundle.nodeChunkRanges[index];
      value.forEach((node, offset) => {
        const ordinal = range ? range.start + offset : null;
        if (ordinal === null) return;
        state.bundle.nodeOrdinalById.set(node.id, ordinal);
      });
    } else if (kind === "edge") {
      value.forEach((edge) => {
        state.bundle.edgeChunkIndexById.set(edge.id, index);
        for (const nodeId of [edge.source_id, edge.target_id]) {
          if (!state.bundle.edgeChunkIndicesByNodeId.has(nodeId)) state.bundle.edgeChunkIndicesByNodeId.set(nodeId, new Set());
          state.bundle.edgeChunkIndicesByNodeId.get(nodeId).add(index);
        }
      });
    }
    targetMap.set(index, value);
    if (rebuild) rebuildBundleGraph();
    return value;
  }

  function chunkIndexForOrdinal(ordinal) {
    return state.bundle?.nodeChunkRanges.find((range) => range.start <= ordinal && ordinal < range.end)?.index;
  }

  function searchRecordIndexForOrdinal(ordinal) {
    return state.bundle?.searchRecordRanges.find((range) => range.start <= ordinal && ordinal < range.end)?.index;
  }

  async function loadBundleDataForModules(
    moduleIds,
    {
      details = true,
      overview = false,
      maxNodeChunks = 12,
      maxEdgeChunks = 16,
      maxOverviewChunks = 8,
      nodeOffset = 0,
      edgeOffset = 0,
      overviewOffset = 0,
    } = {},
  ) {
    if (!state.bundle) return { nodeTotal: 0, edgeTotal: 0, overviewTotal: 0, nextNodeOffset: 0, nextEdgeOffset: 0, nextOverviewOffset: 0 };
    const nodeIndices = new Set();
    const edgeIndices = new Set();
    const overviewIndices = new Set();
    moduleIds.forEach((moduleId) => {
      const ordinal = state.bundle.moduleIndexById.get(moduleId);
      if (ordinal === undefined) return;
      const key = String(ordinal);
      if (details) {
        (state.bundle.overview.node_chunks_by_module[key] || []).forEach((index) => nodeIndices.add(index));
        (state.bundle.overview.edge_chunks_by_module[key] || []).forEach((index) => edgeIndices.add(index));
      }
      if (overview) {
        (state.bundle.overview.module_edge_chunks_by_module[key] || []).forEach((index) => overviewIndices.add(index));
      }
    });
    const orderedNodeIndices = [...nodeIndices].sort((a, b) => a - b);
    const orderedEdgeIndices = [...edgeIndices].sort((a, b) => a - b);
    const orderedOverviewIndices = [...overviewIndices].sort((a, b) => a - b);
    const availableNodeIndices = orderedNodeIndices.filter((index) => !state.loadedNodeChunks.has(index));
    const availableEdgeIndices = orderedEdgeIndices.filter((index) => !state.loadedEdgeChunks.has(index));
    const availableOverviewIndices = orderedOverviewIndices.filter((index) => !state.loadedOverviewEdgeChunks.has(index));
    // Offsets are progress counters for the UI, not indexes into the
    // already-filtered list. Applying them here would skip chunks after a
    // previous request had removed loaded entries from the list.
    const selectedNodeIndices = availableNodeIndices.slice(0, maxNodeChunks);
    const selectedEdgeIndices = availableEdgeIndices.slice(0, maxEdgeChunks);
    const selectedOverviewIndices = availableOverviewIndices.slice(0, maxOverviewChunks);
    await Promise.all([
      ...selectedNodeIndices.map((index) => loadBundleChunk("node", index, { rebuild: false })),
      ...selectedEdgeIndices.map((index) => loadBundleChunk("edge", index, { rebuild: false })),
      ...selectedOverviewIndices.map((index) => loadBundleChunk("overview", index, { rebuild: false })),
    ]);
    rebuildBundleGraph();
    return {
      nodeTotal: orderedNodeIndices.length,
      edgeTotal: orderedEdgeIndices.length,
      overviewTotal: orderedOverviewIndices.length,
      nextNodeOffset: orderedNodeIndices.length - Math.max(0, availableNodeIndices.length - selectedNodeIndices.length),
      nextEdgeOffset: orderedEdgeIndices.length - Math.max(0, availableEdgeIndices.length - selectedEdgeIndices.length),
      nextOverviewOffset: orderedOverviewIndices.length - Math.max(0, availableOverviewIndices.length - selectedOverviewIndices.length),
    };
  }

  async function loadBundleDataForNodeIds(
    nodeIds,
    { maxNodeChunks = 12, maxEdgeChunks = 16, nodeOffset = 0, edgeOffset = 0 } = {},
  ) {
    if (!state.bundle) return { nodeTotal: 0, edgeTotal: 0, nextNodeOffset: 0, nextEdgeOffset: 0 };
    const nodeIndices = new Set();
    const edgeIndices = new Set();
    const fallbackModules = new Set();
    const primaryNodeIndices = new Set();
    const primaryEdgeIndices = new Set();
    nodeIds.filter(Boolean).forEach((nodeId) => {
      const hint = state.searchNodeHints.get(nodeId);
      const ordinal = hint?.ordinal ?? state.bundle.nodeOrdinalById.get(nodeId);
      (state.bundle.nodeChunkIndicesByNodeId.get(nodeId) || new Set()).forEach((index) => {
        nodeIndices.add(index);
        primaryNodeIndices.add(index);
      });
      if (Number.isInteger(ordinal)) {
        (state.bundle.nodeChunkIndicesByOrdinal.get(ordinal) || new Set()).forEach((index) => {
          nodeIndices.add(index);
          primaryNodeIndices.add(index);
        });
        (state.bundle.edgeChunkIndicesByOrdinal.get(ordinal) || new Set()).forEach((index) => {
          edgeIndices.add(index);
          primaryEdgeIndices.add(index);
        });
      }
      const moduleId = moduleIdForNodeId(nodeId) || hint?.moduleId;
      if (moduleId) fallbackModules.add(moduleId);
    });
    if (!nodeIndices.size && fallbackModules.size) {
      return loadBundleDataForModules([...fallbackModules], {
        details: true,
        overview: false,
        maxNodeChunks,
        maxEdgeChunks,
        nodeOffset,
        edgeOffset,
      });
    }
    const prioritizedIndices = (indices, preferred) => [
      ...[...preferred].sort((a, b) => a - b),
      ...[...indices].filter((index) => !preferred.has(index)).sort((a, b) => a - b),
    ];
    const orderedNodeIndices = prioritizedIndices(nodeIndices, primaryNodeIndices);
    const orderedEdgeIndices = prioritizedIndices(edgeIndices, primaryEdgeIndices);
    const availableNodeIndices = orderedNodeIndices.filter((index) => !state.loadedNodeChunks.has(index));
    const availableEdgeIndices = orderedEdgeIndices.filter((index) => !state.loadedEdgeChunks.has(index));
    const selectedNodeIndices = availableNodeIndices
      .slice(0, maxNodeChunks);
    const selectedEdgeIndices = availableEdgeIndices
      .slice(0, maxEdgeChunks);
    await Promise.all(
      selectedNodeIndices
        .map((index) => loadBundleChunk("node", index, { rebuild: false })),
    );
    await Promise.all(
      selectedEdgeIndices
        .map((index) => loadBundleChunk("edge", index, { rebuild: false })),
    );
    // Load the endpoints of the selected local edges, but keep this bounded
    // so selecting one function never turns into a full-bundle download.
    const endpointNodeIndices = new Set(nodeIndices);
    state.loadedEdgeChunks.forEach((chunk) => chunk.forEach((edge) => {
      if (!nodeIds.includes(edge.source_id) && !nodeIds.includes(edge.target_id)) return;
      for (const endpoint of [edge.source_id, edge.target_id]) {
        (state.bundle.nodeChunkIndicesByNodeId.get(endpoint) || new Set()).forEach((index) => endpointNodeIndices.add(index));
        const ordinal = state.bundle.nodeOrdinalById.get(endpoint);
        if (Number.isInteger(ordinal)) {
          (state.bundle.nodeChunkIndicesByOrdinal.get(ordinal) || new Set()).forEach((index) => endpointNodeIndices.add(index));
        }
      }
    }));
    const endpointNodeIndicesOrdered = prioritizedIndices(endpointNodeIndices, primaryNodeIndices);
    const availableEndpointNodeIndices = endpointNodeIndicesOrdered
      .filter((index) => !state.loadedNodeChunks.has(index));
    await Promise.all(
      availableEndpointNodeIndices.slice(0, maxNodeChunks)
        .map((index) => loadBundleChunk("node", index, { rebuild: false })),
    );
    rebuildBundleGraph();
    return {
      nodeTotal: endpointNodeIndicesOrdered.length,
      edgeTotal: orderedEdgeIndices.length,
      nextNodeOffset: endpointNodeIndicesOrdered.length - Math.max(
        0,
        availableEndpointNodeIndices.length - Math.min(maxNodeChunks, availableEndpointNodeIndices.length),
      ),
      nextEdgeOffset: orderedEdgeIndices.length - Math.max(0, availableEdgeIndices.length - selectedEdgeIndices.length),
    };
  }

  function bundleChunkStatus(moduleIds, { details = true, overview = false } = {}) {
    const status = { node: 0, edge: 0, overview: 0 };
    if (!state.bundle) return status;
    const nodeIndices = new Set();
    const edgeIndices = new Set();
    const overviewIndices = new Set();
    moduleIds.forEach((moduleId) => {
      const ordinal = state.bundle.moduleIndexById.get(moduleId);
      if (ordinal === undefined) return;
      const key = String(ordinal);
      if (details) {
        (state.bundle.overview.node_chunks_by_module[key] || []).forEach((index) => nodeIndices.add(index));
        (state.bundle.overview.edge_chunks_by_module[key] || []).forEach((index) => edgeIndices.add(index));
      }
      if (overview) {
        (state.bundle.overview.module_edge_chunks_by_module[key] || []).forEach((index) => overviewIndices.add(index));
      }
    });
    status.node = [...nodeIndices].filter((index) => !state.loadedNodeChunks.has(index)).length;
    status.edge = [...edgeIndices].filter((index) => !state.loadedEdgeChunks.has(index)).length;
    status.overview = [...overviewIndices].filter((index) => !state.loadedOverviewEdgeChunks.has(index)).length;
    return status;
  }

  function scheduleBundleLoads() {
    if (!state.bundle || state.loadingTimer !== null) return;
    const moduleIds = [...visibleModules()];
    const tier = state.camera.zoom < 0.55 ? "overview" : "details";
    const signature = `${tier}:${Math.round(state.width / 100)}:${Math.round(state.height / 100)}:${moduleIds.sort().join(",")}`;
    if (signature === state.lastLoadSignature) return;
    state.lastLoadSignature = signature;
    const repositoryGeneration = state.repositoryRequestId;
    state.loadingTimer = setTimeout(async () => {
      state.loadingTimer = null;
      if (!moduleIds.length) return;
      try {
        setStatus("表示範囲を読み込んでいます…");
        const loadedBefore = state.loadedNodeChunks.size + state.loadedEdgeChunks.size + state.loadedOverviewEdgeChunks.size;
        await loadBundleDataForModules(moduleIds, {
          details: state.camera.zoom >= 0.55,
          overview: state.camera.zoom < 0.55,
          maxNodeChunks: 12,
          maxEdgeChunks: 16,
          maxOverviewChunks: 8,
        });
        if (repositoryGeneration !== state.repositoryRequestId) return;
        const remaining = bundleChunkStatus(
          moduleIds,
          state.camera.zoom < 0.55 ? { details: false, overview: true } : { details: true, overview: false },
        );
        const remainingCount = remaining.node + remaining.edge + remaining.overview;
        setStatus(remainingCount
          ? `表示範囲を一部読み込み済み · 未読込${formatNumber(remainingCount)}チャンク`
          : "解析完了 · 表示範囲を読み込み済み");
        draw();
        const loadedAfter = state.loadedNodeChunks.size + state.loadedEdgeChunks.size + state.loadedOverviewEdgeChunks.size;
        if (remainingCount && loadedAfter > loadedBefore) {
          // Continue in bounded batches.  If the cache cannot retain another
          // batch, loadedAfter stops growing and the status remains explicit
          // instead of spinning through the same chunks forever.
          state.lastLoadSignature = null;
          scheduleBundleLoads();
        }
      } catch (error) {
        if (repositoryGeneration !== state.repositoryRequestId) return;
        state.lastLoadSignature = null;
        setStatus(`表示範囲の読み込みに失敗しました: ${error.message}`, true);
      }
    }, 0);
  }

  async function searchBundle(query, requestId = state.searchRequestId) {
    state.searchResultTruncated = false;
    const normalized = normalizeSearchText(query.trim());
    if (!normalized || !state.bundle) return [];
    const key = `u${normalized.codePointAt(0).toString(16)}`;
    const shard = state.bundle.index.search?.shards?.find((item) => item.key === key);
    if (!shard) return [];
    const ordinals = new Set();
    for (const entry of shard.chunks || []) {
      const records = await fetchJson(`bundle/${entry.path}`, `search:${entry.path}`);
      if (requestId !== state.searchRequestId) return [];
      records.forEach((ordinal) => ordinals.add(ordinal));
    }
    if (state.bundle.index.search?.record_format === "node_records" && state.bundle.searchRecordEntries.length) {
      const recordIndices = new Set([...ordinals].map((ordinal) => searchRecordIndexForOrdinal(ordinal)).filter((index) => index !== undefined));
      const matches = [];
      for (const index of [...recordIndices].sort((a, b) => a - b)) {
        const entry = state.bundle.searchRecordEntries[index];
        const records = await fetchJson(`bundle/${entry.path}`, `search-record:${index}`);
        if (requestId !== state.searchRequestId) return [];
        for (const record of records) {
          if (!ordinals.has(record.ordinal)) continue;
          const ordinal = record.ordinal;
          const values = {
            id: String(record.id || ""),
            qualified_name: String(record.qualified_name || ""),
            display_name: String(record.display_name || ""),
            file: String(record.file || ""),
          };
          const language = typeof record.language === "string" && record.language ? record.language : null;
          const haystack = normalizeSearchText(Object.values(values).join(" "));
          if (!haystack.includes(normalized)) continue;
          if (language) state.languageByNodeId.set(values.id, language);
          const moduleId = Number.isInteger(record.module) ? state.bundle.moduleNodes[record.module]?.id : null;
          const node = {
            ...values,
            kind: record.kind || "unknown",
            ...(language ? { extensions: { language } } : {}),
          };
          if (!isNodeLanguageVisible(node)) continue;
          state.searchNodeHints.set(node.id, {
            moduleId,
            nodeChunkIndex: chunkIndexForOrdinal(ordinal),
            ordinal,
          });
          if (moduleId) state.moduleByNodeId.set(node.id, moduleId);
          matches.push(node);
          if (matches.length >= 80) {
            state.searchResultTruncated = true;
            break;
          }
        }
        if (matches.length >= 80) break;
      }
      return matches;
    }
    const chunkIndices = new Set([...ordinals].map((ordinal) => chunkIndexForOrdinal(ordinal)).filter((index) => index !== undefined));
    await Promise.all([...chunkIndices].map((index) => loadBundleChunk("node", index, { rebuild: false })));
    rebuildBundleGraph();
    const matches = [];
    [...ordinals].sort((a, b) => a - b).some((ordinal) => {
      const node = nodeAtOrdinal(ordinal);
      if (!node) return false;
      const haystack = normalizeSearchText(`${node.id} ${node.qualified_name} ${node.display_name || ""} ${node.file || ""}`);
      if (isNodeLanguageVisible(node) && haystack.includes(normalized)) matches.push(node);
      if (matches.length >= 80) state.searchResultTruncated = true;
      return matches.length >= 80;
    });
    return matches;
  }

  function nodeAtOrdinal(ordinal) {
    const chunkIndex = chunkIndexForOrdinal(ordinal);
    if (chunkIndex === undefined) return null;
    const range = state.bundle.nodeChunkRanges[chunkIndex];
    const chunk = state.loadedNodeChunks.get(chunkIndex);
    return chunk ? chunk[ordinal - range.start] || null : null;
  }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    state.width = Math.max(1, rect.width);
    state.height = Math.max(1, rect.height);
    state.devicePixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(state.width * state.devicePixelRatio);
    canvas.height = Math.floor(state.height * state.devicePixelRatio);
    context.setTransform(state.devicePixelRatio, 0, 0, state.devicePixelRatio, 0, 0);
    draw();
  }

  function worldToScreen(position) {
    return {
      x: state.width / 2 + (position.x - state.camera.x) * state.camera.zoom,
      y: state.height / 2 + (position.y - state.camera.y) * state.camera.zoom,
    };
  }

  function screenToWorld(x, y) {
    return {
      x: state.camera.x + (x - state.width / 2) / state.camera.zoom,
      y: state.camera.y + (y - state.height / 2) / state.camera.zoom,
    };
  }

  function fitView() {
    if (!state.nodes.length) return;
    const positions = state.nodes
      .filter((node) => isNodeLanguageVisible(node))
      .map((node) => state.positionById.get(node.id))
      .filter(Boolean);
    if (!positions.length) return;
    const bounds = positions.reduce((current, position) => ({
      minX: Math.min(current.minX, position.x),
      maxX: Math.max(current.maxX, position.x),
      minY: Math.min(current.minY, position.y),
      maxY: Math.max(current.maxY, position.y),
    }), { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
    const { minX, maxX, minY, maxY } = bounds;
    state.camera.x = (minX + maxX) / 2;
    state.camera.y = (minY + maxY) / 2;
    const width = Math.max(700, maxX - minX + 500);
    const height = Math.max(500, maxY - minY + 400);
    state.camera.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.min(state.width / width, state.height / height)));
    draw();
  }

  function displayAncestor(nodeId) {
    const node = state.nodeById.get(nodeId);
    if (!node) return null;
    if (state.camera.zoom < 0.55) return state.moduleByNodeId.get(nodeId);
    if (state.camera.zoom < 1.25 && ["function", "method", "lambda"].includes(node.kind)) {
      return node.parent_id && state.nodeById.get(node.parent_id)?.kind === "class" ? node.parent_id : state.moduleByNodeId.get(nodeId);
    }
    return nodeId;
  }

  function isInViewport(position, margin = 120) {
    const screen = worldToScreen(position);
    return screen.x >= -margin && screen.x <= state.width + margin && screen.y >= -margin && screen.y <= state.height + margin;
  }

  function isBoundsInViewport(bounds, margin = 120) {
    if (!bounds) return false;
    const topLeft = worldToScreen({ x: bounds.minX, y: bounds.minY });
    const bottomRight = worldToScreen({ x: bounds.maxX, y: bounds.maxY });
    return bottomRight.x >= -margin
      && topLeft.x <= state.width + margin
      && bottomRight.y >= -margin
      && topLeft.y <= state.height + margin;
  }

  function edgePriority(group) {
    const edge = group.edge;
    if (state.selectedEdgeId && edge.id === state.selectedEdgeId) return 0;
    if (state.selectedNodeId && (edge.source_id === state.selectedNodeId || edge.target_id === state.selectedNodeId)) return 0;
    if ([...state.pinnedNodeIds].some((nodeId) => edge.source_id === nodeId || edge.target_id === nodeId)) return 0;
    if (state.focusedModuleIds.has(group.sourceId) || state.focusedModuleIds.has(group.targetId)) return 1;
    return 2;
  }

  function scheduleEdgeRender(candidates, visibleNodes, token) {
    if (!candidates.length) {
      state.edgeRenderStatus = null;
      setStats(visibleNodes.length, 0);
      return;
    }
    const hitCandidates = candidates.filter((candidate) => candidate.priority === 0)
      .concat(candidates.filter((candidate) => candidate.priority !== 0))
      .slice(0, EDGE_HIT_LIMIT);
    const hitSet = new Set(hitCandidates);
    const renderState = {
      candidates,
      cursor: 0,
      drawn: 0,
      hitSet,
      token,
      visibleNodes,
    };
    state.edgeRenderState = renderState;
    state.edgeRenderStatus = { active: true, drawn: 0, total: candidates.length };

    const renderFrame = () => {
      if (state.edgeRenderState !== renderState || state.edgeRenderToken !== token) return;
      const startedAt = performance.now();
      while (renderState.cursor < renderState.candidates.length) {
        const candidate = renderState.candidates[renderState.cursor];
        drawEdge(candidate.edge, candidate.start, candidate.end, candidate.count);
        if (renderState.hitSet.has(candidate)) state.visibleEdges.push(candidate);
        renderState.cursor += 1;
        renderState.drawn += 1;
        // Always paint at least one edge; thereafter yield before the frame
        // budget is exceeded so pointer and wheel events remain responsive.
        if (renderState.drawn > 1 && performance.now() - startedAt >= EDGE_RENDER_FRAME_BUDGET_MS) break;
      }
      const complete = renderState.cursor >= renderState.candidates.length;
      state.edgeRenderStatus = {
        active: !complete,
        drawn: renderState.drawn,
        total: renderState.candidates.length,
      };
      setStats(visibleNodes.length, renderState.drawn);
      if (!complete) {
        state.edgeRenderFrame = requestAnimationFrame(renderFrame);
        return;
      }
      state.edgeRenderFrame = null;
      state.edgeRenderState = null;
      // Edges are painted incrementally to keep the UI responsive.  Paint
      // nodes once more after completion so labels remain above the lines.
      visibleNodes.forEach(drawNode);
      setStats(visibleNodes.length, renderState.drawn);
    };

    state.edgeRenderFrame = requestAnimationFrame(renderFrame);
  }

  function draw() {
    // Resize events can fire before the analysis document finishes loading.
    // Keep the canvas blank until setupDocument has initialized the indexes.
    if (!state.document) return;
    cancelEdgeRender();
    context.clearRect(0, 0, state.width, state.height);
    drawGrid();
    state.visibleNodes = [];
    state.visibleEdges = [];
    state.omittedEdgeGroups = 0;
    state.edgeRenderStatus = null;
    const visibleModuleIds = visibleModules();
    const edgeGroups = new Map();
    if (state.camera.zoom < 0.55) {
      state.lowEdgeGroups.forEach((group) => {
        if (!visibleModuleIds.has(group.sourceId) && !visibleModuleIds.has(group.targetId)) return;
        if (!isEdgeLanguageVisible(group.edge, group.sourceId, group.targetId)) return;
        edgeGroups.set(`${group.sourceId}|${group.targetId}|${group.edge.relation_type}|${group.edge.resolution_status}`, group);
      });
    } else {
      const pairKeys = new Set();
      visibleModuleIds.forEach((moduleId) => state.modulePairKeysByModule.get(moduleId)?.forEach((key) => pairKeys.add(key)));
      pairKeys.forEach((pairKey) => {
        (state.modulePairEdges.get(pairKey) || []).forEach((edge) => {
          const sourceId = displayAncestor(edge.source_id);
          const targetId = displayAncestor(edge.target_id);
          if (!sourceId || !targetId || sourceId === targetId) return;
          if (!isEdgeLanguageVisible(edge, sourceId, targetId)) return;
          const key = `${sourceId}|${targetId}|${edge.relation_type}|${edge.resolution_status}`;
          if (!edgeGroups.has(key)) edgeGroups.set(key, { edge, sourceId, targetId, count: 0 });
          edgeGroups.get(key).count += 1;
        });
      });
    }
    const candidates = [];
    edgeGroups.forEach((group) => {
      const sourcePosition = state.positionById.get(group.sourceId);
      const targetPosition = state.positionById.get(group.targetId);
      if (!sourcePosition || !targetPosition || (!isInViewport(sourcePosition) && !isInViewport(targetPosition))) return;
      candidates.push({
        ...group,
        end: worldToScreen(targetPosition),
        priority: edgePriority(group),
        start: worldToScreen(sourcePosition),
      });
    });
    // Selected and pinned relations are always at the front of the render
    // queue.  This keeps the graph useful even when a dense viewport reaches
    // the safety ceiling, without sorting every relation in a large bundle.
    const prioritizedCandidates = [
      ...candidates.filter((candidate) => candidate.priority === 0),
      ...candidates.filter((candidate) => candidate.priority === 1),
      ...candidates.filter((candidate) => candidate.priority === 2),
    ];
    const renderCandidates = prioritizedCandidates.slice(0, EDGE_RENDER_LIMIT);
    state.omittedEdgeGroups = Math.max(0, prioritizedCandidates.length - renderCandidates.length);

    const visibleNodes = [];
    if (state.camera.zoom < 0.55) {
      visibleModuleIds.forEach((moduleId) => {
        const module = state.nodeById.get(moduleId);
        if (module && isNodeLanguageVisible(module)) visibleNodes.push(module);
      });
    } else {
      visibleModuleIds.forEach((moduleId) => {
        (state.nodesByModule.get(moduleId) || []).forEach((node) => {
          if (!isNodeLanguageVisible(node)) return;
          if (state.camera.zoom < 1.25 && ["function", "method", "lambda"].includes(node.kind) && !state.searchMatches.has(node.id) && node.id !== state.selectedNodeId) return;
          const position = state.positionById.get(node.id);
          if (position && isInViewport(position)) visibleNodes.push(node);
        });
      });
    }
    state.visibleNodes = visibleNodes;
    state.pendingBundleChunks = state.bundle
      ? bundleChunkStatus(
        [...visibleModuleIds],
        state.camera.zoom < 0.55 ? { details: false, overview: true } : { details: true, overview: false },
      )
      : { node: 0, edge: 0, overview: 0 };
    visibleNodes.forEach(drawNode);
    const token = state.edgeRenderToken;
    setStats(visibleNodes.length, 0);
    scheduleEdgeRender(renderCandidates, visibleNodes, token);
    scheduleBundleLoads();
  }

  function visibleModules() {
    const result = new Set();
    const pinnedModules = new Set();
    if (state.selectedNodeId) {
      const selectedModule = state.moduleByNodeId.get(state.selectedNodeId);
      if (selectedModule) pinnedModules.add(selectedModule);
    }
    state.searchMatches.forEach((nodeId) => {
      const searchModule = state.moduleByNodeId.get(nodeId);
      if (searchModule) pinnedModules.add(searchModule);
    });
    state.pinnedNodeIds.forEach((nodeId) => {
      const pinnedModule = moduleIdForNodeId(nodeId);
      if (pinnedModule) pinnedModules.add(pinnedModule);
    });
    state.modules.forEach((module) => {
      if (!isNodeLanguageVisible(module)) return;
      const position = state.positionById.get(module.id);
      const bounds = state.moduleBounds.get(module.id);
      if (position && (pinnedModules.has(module.id) || isBoundsInViewport(bounds, 160) || isInViewport(position, 260))) result.add(module.id);
    });
    return result;
  }

  function drawGrid() {
    const step = Math.max(20, Math.min(120, 80 * state.camera.zoom));
    context.strokeStyle = "rgba(99, 122, 157, .12)";
    context.lineWidth = 1;
    const offsetX = ((state.width / 2 - state.camera.x * state.camera.zoom) % step + step) % step;
    const offsetY = ((state.height / 2 - state.camera.y * state.camera.zoom) % step + step) % step;
    context.beginPath();
    for (let x = offsetX; x < state.width; x += step) { context.moveTo(x, 0); context.lineTo(x, state.height); }
    for (let y = offsetY; y < state.height; y += step) { context.moveTo(0, y); context.lineTo(state.width, y); }
    context.stroke();
  }

  function drawEdge(edge, start, end, count) {
    const style = EDGE_STYLES[edge.relation_type] || EDGE_STYLES.uses;
    context.save();
    context.strokeStyle = edge.resolution_status === "unresolved" || edge.resolution_status === "unsupported" ? "#fb7185" : style.color;
    context.globalAlpha = edge.id === state.selectedEdgeId ? 1 : 0.72;
    context.lineWidth = edge.id === state.selectedEdgeId ? 3 : 1.3;
    context.setLineDash(edge.resolution_status === "unresolved" || edge.resolution_status === "unsupported" ? [3, 5] : style.dash);
    context.beginPath();
    context.moveTo(start.x, start.y);
    context.lineTo(end.x, end.y);
    context.stroke();
    drawArrowHead(start, end, style.head);
    if (count > 1 && state.camera.zoom >= 0.55) {
      const middle = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
      context.fillStyle = "#dbeafe";
      context.font = "10px system-ui";
      context.fillText(String(count), middle.x + 4, middle.y - 4);
    }
    context.restore();
  }

  function drawArrowHead(start, end, type) {
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    const size = 8;
    const x = end.x - Math.cos(angle) * 4;
    const y = end.y - Math.sin(angle) * 4;
    context.save();
    context.translate(x, y);
    context.rotate(angle);
    context.beginPath();
    if (type === "diamond") {
      context.moveTo(0, 0);
      context.lineTo(-size / 2, size / 2);
      context.lineTo(-size, 0);
      context.lineTo(-size / 2, -size / 2);
      context.closePath();
      context.fillStyle = context.strokeStyle;
      context.fill();
    } else {
      context.moveTo(0, 0);
      context.lineTo(-size, size / 2);
      context.lineTo(-size, -size / 2);
      context.closePath();
      if (type === "open") { context.stroke(); } else {
        context.fillStyle = context.strokeStyle;
        context.fill();
      }
    }
    context.restore();
  }

  function drawNode(node) {
    const metrics = nodeMetrics(node);
    if (!metrics) return;
    const { screen, width, height, isCompact } = metrics;
    const color = NODE_COLORS[node.kind] || "#94a3b8";
    context.save();
    context.globalAlpha = state.selectedNodeId && state.selectedNodeId !== node.id ? 0.35 : 1;
    context.fillStyle = "#14213a";
    context.strokeStyle = state.selectedNodeId === node.id ? "#f8fafc" : color;
    context.lineWidth = state.selectedNodeId === node.id ? 3 : 1.5;
    roundRect(screen.x - width / 2, screen.y - height / 2, width, height, 5);
    context.fill();
    context.stroke();
    if (!isCompact) {
      context.fillStyle = "#e5edf8";
      context.font = "12px system-ui";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(truncate(nodeVisualLabel(node), 29), screen.x, screen.y - 5);
      context.fillStyle = color;
      context.font = "10px system-ui";
      context.fillText(nodeKindLabel(node), screen.x, screen.y + 11);
      drawReturnBadge(node, screen.x + width / 2 - 7, screen.y - height / 2 + 7);
    } else {
      context.fillStyle = "#dbeafe";
      context.font = "10px system-ui";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(compactNodeLabel(node), screen.x, screen.y + 0.5);
    }
    context.restore();
  }

  function nodeMetrics(node) {
    const position = state.positionById.get(node.id);
    if (!position) return null;
    const isCompact = state.camera.zoom < NODE_LABEL_ZOOM;
    return {
      screen: worldToScreen(position),
      width: isCompact ? COMPACT_NODE_WIDTH : Math.min(220, Math.max(70, nodeVisualLabel(node).length * 7 + 28)),
      height: isCompact ? 26 : 42,
      isCompact,
    };
  }

  function drawReturnBadge(node, x, y) {
    if (!node.return_behavior) return;
    const label = node.return_behavior === "returns_value" ? "R" : node.return_behavior === "mixed" ? "M" : "N";
    context.fillStyle = "#0b1222";
    context.strokeStyle = "#dbeafe";
    context.lineWidth = 1;
    context.beginPath();
    context.arc(x, y, 7, 0, Math.PI * 2);
    context.fill();
    context.stroke();
    context.fillStyle = "#dbeafe";
    context.font = "9px system-ui";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(label, x, y + .5);
  }

  function roundRect(x, y, width, height, radius) {
    context.beginPath();
    context.moveTo(x + radius, y);
    context.arcTo(x + width, y, x + width, y + height, radius);
    context.arcTo(x + width, y + height, x, y + height, radius);
    context.arcTo(x, y + height, x, y, radius);
    context.arcTo(x, y, x + width, y, radius);
    context.closePath();
  }

  function truncate(value, maximum) {
    return value.length > maximum ? `${value.slice(0, maximum - 1)}…` : value;
  }

  function renderToc() {
    tocElement.replaceChildren();
    const modules = state.nodes
      .filter((node) => node.kind === "module" && isNodeLanguageVisible(node))
      .sort((a, b) => a.qualified_name.localeCompare(b.qualified_name));
    if (!modules.length) { tocElement.append(emptyList("モジュールなし")); return; }
    modules.slice(0, 300).forEach((node) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = node.qualified_name;
      button.title = node.file || node.id;
      button.addEventListener("click", () => selectNode(node.id, true));
      tocElement.append(button);
    });
    if (modules.length > 300) {
      tocElement.append(emptyList(`先頭300件を表示（全${formatNumber(modules.length)}件）`));
    }
  }

  async function renderSearchResults(query) {
    const normalized = normalizeSearchText(query.trim());
    const requestId = ++state.searchRequestId;
    state.searchMatches.clear();
    state.searchNodeHints.clear();
    if (!normalized) {
      searchResultsElement.replaceChildren();
      draw();
      return;
    }
    searchResultsElement.replaceChildren(emptyList("検索中…"));
    let matches;
    try {
      if (state.bundle) {
        matches = await searchBundle(normalized, requestId);
      } else {
        const allMatches = state.nodes.filter((node) => isNodeLanguageVisible(node) && normalizeSearchText(`${node.id} ${node.qualified_name} ${node.display_name || ""} ${node.file || ""}`).includes(normalized));
        state.searchResultTruncated = allMatches.length > 80;
        matches = allMatches.slice(0, 80);
      }
    } catch (error) {
      if (requestId !== state.searchRequestId) return;
      searchResultsElement.replaceChildren(emptyList("検索に失敗しました"));
      setStatus(`検索に失敗しました: ${error.message}`, true);
      return;
    }
    if (requestId !== state.searchRequestId) return;
    matches.forEach((node) => state.searchMatches.add(node.id));
    searchResultsElement.replaceChildren();
    if (!matches.length) { searchResultsElement.append(emptyList("該当なし")); draw(); return; }
    matches.forEach((node) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = nodeLabel(node);
      button.title = node.id;
      const small = document.createElement("small");
      small.textContent = `${languageLabel(nodeLanguage(node))} · ${nodeKindLabel(node)} · ${node.file || "外部"}`;
      button.append(small);
      button.addEventListener("click", () => selectNode(node.id, true));
      searchResultsElement.append(button);
    });
    if (state.searchResultTruncated) {
      const note = document.createElement("div");
      note.className = "empty-list";
      note.textContent = "先頭80件を表示しています。検索語を追加して絞り込んでください。";
      searchResultsElement.append(note);
    }
    draw();
  }

  function emptyList(message) {
    const element = document.createElement("div");
    element.className = "empty-list";
    element.textContent = message;
    return element;
  }

  function centerNode(nodeId, moduleId = null) {
    const position = state.positionById.get(nodeId) || (moduleId && state.positionById.get(moduleId));
    if (!position) return;
    state.camera.x = position.x;
    state.camera.y = position.y;
    state.camera.zoom = Math.max(state.camera.zoom, 1.25);
  }

  function setSelectionLoad(kind, nodeIds, result, details) {
    if (!result || (result.nodeTotal <= result.nextNodeOffset && result.edgeTotal <= result.nextEdgeOffset)) {
      state.selectionLoad = null;
      return;
    }
    state.selectionLoad = {
      kind,
      nodeIds: [...new Set(nodeIds.filter(Boolean))],
      nodeOffset: result.nextNodeOffset,
      edgeOffset: result.nextEdgeOffset,
      nodeTotal: result.nodeTotal,
      edgeTotal: result.edgeTotal,
      ...details,
    };
  }

  function selectionLoadControl() {
    const selection = state.selectionLoad;
    if (!selection) return null;
    const remainingNodes = Math.max(0, selection.nodeTotal - selection.nodeOffset);
    const remainingEdges = Math.max(0, selection.edgeTotal - selection.edgeOffset);
    if (!remainingNodes && !remainingEdges) return null;
    const container = document.createElement("div");
    container.className = "selection-load-control";
    const summary = document.createElement("p");
    summary.className = "muted";
    summary.textContent = `追加読込可能: ノード${formatNumber(remainingNodes)}チャンク / 接続${formatNumber(remainingEdges)}チャンク`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "接続データを追加読み込み";
    button.addEventListener("click", () => { void loadMoreSelection(); });
    container.append(summary, button);
    return container;
  }

  async function loadMoreSelection() {
    const selection = state.selectionLoad;
    if (!selection) return;
    const requestId = ++state.selectionRequestId;
    try {
      setStatus("選択範囲の続きを読み込んでいます…");
      const result = await loadBundleDataForNodeIds(selection.nodeIds, {
        maxNodeChunks: 12,
        maxEdgeChunks: 16,
        nodeOffset: selection.nodeOffset,
        edgeOffset: selection.edgeOffset,
      });
      if (requestId !== state.selectionRequestId) return;
      setSelectionLoad(selection.kind, selection.nodeIds, result, {
        focusNodeId: selection.focusNodeId,
        focusEdge: selection.focusEdge,
        aggregateCount: selection.aggregateCount,
        displaySourceId: selection.displaySourceId,
        displayTargetId: selection.displayTargetId,
      });
      if (selection.kind === "node") {
        const node = state.nodeById.get(selection.focusNodeId);
        if (node) renderNodeDetails(node);
      } else if (selection.focusEdge) {
        renderEdgeDetails(
          selection.focusEdge,
          selection.aggregateCount,
          selection.displaySourceId,
          selection.displayTargetId,
        );
      }
      const remainingNodes = Math.max(0, result.nodeTotal - result.nextNodeOffset);
      const remainingEdges = Math.max(0, result.edgeTotal - result.nextEdgeOffset);
      setStatus(remainingNodes || remainingEdges
        ? `選択範囲を一部読み込み済み · 未読込 ノード${formatNumber(remainingNodes)} / 接続${formatNumber(remainingEdges)}チャンク`
        : "選択範囲を読み込み済み");
      draw();
    } catch (error) {
      if (requestId === state.selectionRequestId) setStatus(`選択範囲の読み込みに失敗しました: ${error.message}`, true);
    }
  }

  async function selectRepresentativeEdge(aggregateEdge) {
    const detail = aggregateEdge?.detail;
    const edgeId = detail?.representative_edge_id;
    const chunkIndex = detail?.representative_edge_chunk;
    if (!state.bundle || typeof edgeId !== "string" || !Number.isInteger(chunkIndex)) return;
    try {
      setStatus("集約された接続の代表例を読み込んでいます…");
      await loadBundleChunk("edge", chunkIndex);
      const edge = (state.loadedEdgeChunks.get(chunkIndex) || []).find((item) => item.id === edgeId);
      if (!edge) {
        setStatus("代表接続を読み込めませんでした", true);
        return;
      }
      await selectEdge(edge);
    } catch (error) {
      setStatus(`代表接続の読み込みに失敗しました: ${error.message}`, true);
    }
  }

  async function selectNode(nodeId, center) {
    const selectionRequestId = ++state.selectionRequestId;
    const hint = state.searchNodeHints.get(nodeId);
    const moduleIds = [...moduleIdsForNodeId(nodeId)];
    const moduleId = moduleIds[0] || hint?.moduleId;
    if (hint?.moduleId && !moduleIds.includes(hint.moduleId)) moduleIds.push(hint.moduleId);
    if (!state.nodeById.has(nodeId) && !hint && !moduleId) return;
    pinNode(nodeId);
    moduleIds.forEach(pinModule);
    state.selectedNodeId = nodeId;
    state.selectedEdgeId = null;
    state.selectionLoad = null;
    if (center) centerNode(nodeId, moduleId || hint?.moduleId);
    if (state.bundle) {
      if (moduleId) {
        try {
          setStatus("選択範囲を読み込んでいます…");
          if (hint?.nodeChunkIndex !== undefined) await loadBundleChunk("node", hint.nodeChunkIndex);
          // Use the node-local index first.  Selecting one function should
          // fetch its own node and incident edge chunks, not every chunk that
          // happens to belong to the same file/module.
          const loadResult = await loadBundleDataForNodeIds([nodeId], { maxNodeChunks: 12, maxEdgeChunks: 16 });
          if (hint?.nodeChunkIndex !== undefined) await loadBundleChunk("node", hint.nodeChunkIndex);
          setSelectionLoad("node", [nodeId], loadResult, { focusNodeId: nodeId });
        } catch (error) {
          if (selectionRequestId !== state.selectionRequestId) return;
          setStatus(`選択範囲の読み込みに失敗しました: ${error.message}`, true);
        }
      }
    }
    if (selectionRequestId !== state.selectionRequestId) return;
    if (center) centerNode(nodeId, moduleIdForNodeId(nodeId) || moduleId || hint?.moduleId);
    const node = state.nodeById.get(nodeId);
    if (!node) {
      setStatus("選択したノードを読み込めませんでした", true);
      return;
    }
    renderNodeDetails(node);
    if (state.bundle) {
      const remainingNodes = Math.max(0, (state.selectionLoad?.nodeTotal || 0) - (state.selectionLoad?.nodeOffset || 0));
      const remainingEdges = Math.max(0, (state.selectionLoad?.edgeTotal || 0) - (state.selectionLoad?.edgeOffset || 0));
      setStatus(remainingNodes || remainingEdges
        ? `ノードを一部読み込み済み · 未読込 ノード${formatNumber(remainingNodes)} / 接続${formatNumber(remainingEdges)}チャンク`
        : "ノードを読み込み済み");
    } else {
      setStatus("ノードを選択中");
    }
    setDetailsOpen(true);
    draw();
  }

  async function selectEdge(item) {
    const selectionRequestId = ++state.selectionRequestId;
    const edge = item?.edge || item;
    if (!edge) return;
    const sourceId = item?.sourceId || edge.source_id;
    const targetId = item?.targetId || edge.target_id;
    const moduleIds = [...new Set([
      ...moduleIdsForEdge(edge),
      ...moduleIdsForNodeId(sourceId),
      ...moduleIdsForNodeId(targetId),
    ])];
    pinEdgeEndpoints(edge);
    moduleIds.forEach(pinModule);
    state.selectedNodeId = null;
    state.selectedEdgeId = edge.id;
    state.selectionLoad = null;
    if (state.bundle && moduleIds.length) {
      try {
        setStatus("接続元と接続先を読み込んでいます…");
        const loadResult = await loadBundleDataForNodeIds([sourceId, targetId], { maxNodeChunks: 12, maxEdgeChunks: 16 });
        setSelectionLoad("edge", [sourceId, targetId], loadResult, {
          focusEdge: edge,
          aggregateCount: item?.count || edge.detail?.count || null,
          displaySourceId: sourceId,
          displayTargetId: targetId,
        });
        const remainingNodes = Math.max(0, loadResult.nodeTotal - loadResult.nextNodeOffset);
        const remainingEdges = Math.max(0, loadResult.edgeTotal - loadResult.nextEdgeOffset);
        setStatus(remainingNodes || remainingEdges
          ? `接続を一部読み込み済み · 未読込 ノード${formatNumber(remainingNodes)} / 接続${formatNumber(remainingEdges)}チャンク`
          : "接続を読み込み済み");
      } catch (error) {
        if (selectionRequestId !== state.selectionRequestId) return;
        setStatus(`接続先の読み込みに失敗しました: ${error.message}`, true);
      }
    }
    if (selectionRequestId !== state.selectionRequestId) return;
    renderEdgeDetails(edge, item?.count || edge.detail?.count || null, sourceId, targetId);
    setDetailsOpen(true);
    draw();
  }

  function clearSelection() {
    state.selectionRequestId += 1;
    state.selectedNodeId = null;
    state.selectedEdgeId = null;
    state.selectionLoad = null;
    state.pinnedNodeIds.clear();
    state.focusedModuleIds.clear();
    detailsElement.replaceChildren(detailsHeader("詳細"), emptyList("ノードまたは線を選択すると詳細が表示されます。"));
    if (window.innerWidth <= 1000) setDetailsOpen(false);
    draw();
  }

  function detailsHeader(title) {
    const header = document.createElement("div");
    header.className = "details-head";
    const heading = document.createElement("h2");
    heading.textContent = title;
    const close = document.createElement("button");
    close.type = "button";
    close.id = "close-details";
    close.textContent = "閉じる";
    header.append(heading, close);
    return header;
  }

  function renderNodeDetails(node) {
    detailsElement.replaceChildren();
    detailsElement.append(detailsHeader("ノード詳細"));
    const title = document.createElement("h2");
    title.className = "detail-title";
    title.textContent = nodeLabel(node);
    detailsElement.append(title);
    if (node.qualified_name && node.qualified_name !== nodeLabel(node)) {
      const subtitle = document.createElement("p");
      subtitle.className = "detail-subtitle";
      subtitle.textContent = node.qualified_name;
      detailsElement.append(subtitle);
    }
    const fields = [
      ["言語", languageLabel(nodeLanguage(node))],
      ["種類", nodeKindLabel(node)],
      ["ファイル", node.file || "外部"],
      ["位置", formatSpan(node.span)],
      ["戻り値", returnLabel(node.return_behavior)],
      ["実行", executionLabel(node.execution_kind)],
      ["識別子", node.id],
    ];
    detailsElement.append(detailGrid(fields));
    const loadControl = selectionLoadControl();
    if (loadControl) detailsElement.append(loadControl);
    if (node.signature) detailsElement.append(codeBlock(node.signature));
  }

  function canNavigateToNode(nodeId) {
    return state.nodeById.has(nodeId) || Boolean(state.searchNodeHints.get(nodeId)) || Boolean(moduleIdForNodeId(nodeId));
  }

  function appendNodeNavigation(container, label, nodeId, node) {
    const row = document.createElement("div");
    row.className = "detail-navigation";
    const name = document.createElement("span");
    name.className = "detail-navigation-label";
    const summary = node
      ? [nodeLabel(node), node.file || "外部", formatSpan(node.span)].filter(Boolean).join(" · ")
      : nodeId;
    name.textContent = `${label}: ${summary}`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${label}へ移動`;
    button.title = nodeId;
    button.disabled = !canNavigateToNode(nodeId);
    if (!button.disabled) button.addEventListener("click", () => { void selectNode(nodeId, true); });
    row.append(name, button);
    container.append(row);
  }

  function renderEdgeDetails(edge, aggregateCount = null, displaySourceId = edge.source_id, displayTargetId = edge.target_id) {
    detailsElement.replaceChildren();
    detailsElement.append(detailsHeader("接続の詳細"));
    const title = document.createElement("h2");
    title.className = "detail-title";
    title.textContent = edgeLabel(edge.relation_type);
    if (edge.detail?.expression) {
      const subtitle = document.createElement("p");
      subtitle.className = "detail-subtitle";
      subtitle.textContent = edge.detail.expression;
      detailsElement.append(title, subtitle);
    } else {
      detailsElement.append(title);
    }
    const source = state.nodeById.get(displaySourceId);
    const target = state.nodeById.get(displayTargetId);
    const originalSource = state.nodeById.get(edge.source_id);
    const originalTarget = state.nodeById.get(edge.target_id);
    const fields = [
      ["元ノード", source ? nodeLabel(source) : displaySourceId],
      ["先ノード", target ? nodeLabel(target) : displayTargetId],
      ["状態", resolutionLabel(edge.resolution_status)],
      ["確度", edgeConfidenceLabel(edge)],
      ["出典", provenanceLabel(edge.provenance)],
      ["位置", formatSpan(edge.source_span)],
      ["識別子", edge.id],
    ];
    if (aggregateCount > 1) fields.splice(2, 0, ["集約された接続", `${formatNumber(aggregateCount)}件`]);
    if (displaySourceId !== edge.source_id) fields.splice(1, 0, ["実際の元ノード", originalSource ? nodeLabel(originalSource) : edge.source_id]);
    if (displayTargetId !== edge.target_id) fields.splice(2, 0, ["実際の接続先", originalTarget ? nodeLabel(originalTarget) : edge.target_id]);
    detailsElement.append(detailGrid(fields));
    if (edge.detail?.aggregate && edge.detail.representative_edge_id) {
      const representative = document.createElement("button");
      representative.type = "button";
      representative.textContent = "代表接続を表示";
      representative.title = edge.detail.representative_edge_id;
      representative.addEventListener("click", () => { void selectRepresentativeEdge(edge); });
      detailsElement.append(representative);
    }
    const loadControl = selectionLoadControl();
    if (loadControl) detailsElement.append(loadControl);
    const navigation = document.createElement("div");
    navigation.className = "detail-navigation-list";
    appendNodeNavigation(navigation, "元ノード", edge.source_id, originalSource);
    appendNodeNavigation(navigation, "接続先", edge.target_id, originalTarget);
    if (displaySourceId !== edge.source_id) appendNodeNavigation(navigation, "表示上の元ノード", displaySourceId, state.nodeById.get(displaySourceId));
    if (displayTargetId !== edge.target_id) appendNodeNavigation(navigation, "表示上の接続先", displayTargetId, state.nodeById.get(displayTargetId));
    detailsElement.append(navigation);
    if (edge.detail) detailsElement.append(codeBlock(JSON.stringify(edge.detail, null, 2)));
  }

  function detailGrid(fields) {
    const grid = document.createElement("dl");
    grid.className = "detail-grid";
    fields.forEach(([name, value]) => {
      if (value === undefined || value === null || value === "") return;
      const term = document.createElement("dt");
      term.textContent = name;
      const description = document.createElement("dd");
      description.textContent = String(value);
      grid.append(term, description);
    });
    return grid;
  }

  function edgeConfidenceLabel(edge) {
    if (!edge?.detail?.aggregate) return String(edge?.confidence ?? "不明");
    const minimum = edge.detail.confidence_min;
    const maximum = edge.detail.confidence_max;
    if (typeof minimum === "number" && typeof maximum === "number") {
      return minimum === maximum ? `集約全件 ${minimum}` : `集約範囲 ${minimum}–${maximum}`;
    }
    return "集約（代表接続を参照）";
  }

  function codeBlock(value) {
    const block = document.createElement("pre");
    block.className = "detail-code";
    block.textContent = value;
    return block;
  }

  function formatSpan(span) {
    return span ? `${span.start_line}:${span.start_col}–${span.end_line}:${span.end_col}` : "";
  }

  function buildLayoutDocument() {
    return {
      format: "connection-analysis-layout",
      schema_version: "1.0",
      analysis_schema_version: state.document.schema_version,
      camera: { ...state.camera },
      nodes: Object.fromEntries(
        [...state.layoutOverrides.entries()].map(([nodeId, position]) => [nodeId, { x: position.x, y: position.y }]),
      ),
      annotations: [],
    };
  }

  function saveLayout() {
    if (!state.document) return;
    const payload = `${JSON.stringify(buildLayoutDocument(), null, 2)}\n`;
    const blob = new Blob([payload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "layout-v1.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    setStatus("レイアウトを保存しました");
  }

  async function loadLayoutFile(file) {
    if (!file || !state.document) return;
    try {
      const layout = JSON.parse(await file.text());
      const hasLayoutCamera = applyLayout(layout);
      if (!hasLayoutCamera) {
        state.camera = { x: 0, y: 0, zoom: 1 };
        fitView();
      } else {
        draw();
      }
      setStatus(`レイアウトを読み込みました: ${file.name}`);
    } catch (error) {
      setStatus(`レイアウトの読み込みに失敗しました: ${error.message}`, true);
    } finally {
      layoutFileElement.value = "";
    }
  }

  function pointerPosition(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function hitTestNode(x, y) {
    // Use the same nodes and screen rectangles that were drawn for this frame.
    // Testing the full index here allowed hidden children to win over a visible
    // node, especially in the aggregated zoom tiers.
    const candidates = state.visibleNodes.slice().reverse();
    for (const node of candidates) {
      const metrics = nodeMetrics(node);
      if (!metrics) continue;
      const halfWidth = metrics.width / 2 + NODE_HIT_SLOP;
      const halfHeight = metrics.height / 2 + NODE_HIT_SLOP;
      if (Math.abs(x - metrics.screen.x) <= halfWidth && Math.abs(y - metrics.screen.y) <= halfHeight) return node;
    }
    return null;
  }

  function hitTestEdge(x, y) {
    let best = null;
    let distance = EDGE_HIT_RADIUS;
    state.visibleEdges.forEach((item) => {
      const candidate = pointToSegmentDistance(x, y, item.start.x, item.start.y, item.end.x, item.end.y);
      if (candidate < distance) { distance = candidate; best = item; }
    });
    return best;
  }

  function pointToSegmentDistance(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    if (dx === 0 && dy === 0) return Math.hypot(px - x1, py - y1);
    const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  canvas.addEventListener("pointerdown", (event) => {
    canvas.focus();
    const point = pointerPosition(event);
    const dragNode = hitTestNode(point.x, point.y);
    state.pointer = { ...point, lastX: point.x, lastY: point.y, moved: false, dragNodeId: dragNode?.id || null };
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!state.pointer) return;
    const point = pointerPosition(event);
    const dx = point.x - state.pointer.lastX;
    const dy = point.y - state.pointer.lastY;
    if (Math.abs(point.x - state.pointer.x) + Math.abs(point.y - state.pointer.y) > 3) state.pointer.moved = true;
    if (state.pointer.dragNodeId) {
      const position = state.positionById.get(state.pointer.dragNodeId);
      if (position) {
        position.x += dx / state.camera.zoom;
        position.y += dy / state.camera.zoom;
        state.layoutOverrides.set(state.pointer.dragNodeId, { x: position.x, y: position.y });
      }
    } else {
      state.camera.x -= dx / state.camera.zoom;
      state.camera.y -= dy / state.camera.zoom;
    }
    state.pointer.lastX = point.x;
    state.pointer.lastY = point.y;
    draw();
  });

  canvas.addEventListener("pointerup", (event) => {
    if (!state.pointer) return;
    const point = pointerPosition(event);
    const pointer = state.pointer;
    const moved = pointer.moved;
    state.pointer = null;
    if (moved) {
      if (pointer.dragNodeId) buildModuleBounds();
      draw();
      return;
    }
    const node = pointer.dragNodeId ? state.nodeById.get(pointer.dragNodeId) : hitTestNode(point.x, point.y);
    if (node) { selectNode(node.id, false); return; }
    const edge = hitTestEdge(point.x, point.y);
    if (edge) { void selectEdge(edge); return; }
    clearSelection();
  });

  canvas.addEventListener("dblclick", (event) => {
    const point = pointerPosition(event);
    const node = hitTestNode(point.x, point.y);
    if (!node || !["module", "namespace", "class", "type", "interface", "function", "method", "lambda"].includes(node.kind)) return;
    event.preventDefault();
    void selectNode(node.id, true);
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const point = pointerPosition(event);
    const before = screenToWorld(point.x, point.y);
    const scale = Math.exp(-event.deltaY * 0.0015);
    state.camera.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, state.camera.zoom * scale));
    const after = screenToWorld(point.x, point.y);
    state.camera.x += before.x - after.x;
    state.camera.y += before.y - after.y;
    draw();
  }, { passive: false });

  canvas.addEventListener("keydown", (event) => { if (event.key === "Escape") clearSelection(); });
  searchElement.addEventListener("input", () => renderSearchResults(searchElement.value));
  document.getElementById("fit-view").addEventListener("click", fitView);
  document.getElementById("reset-view").addEventListener("click", () => { state.camera = { x: 0, y: 0, zoom: 1 }; fitView(); });
  toggleDetailsElement.addEventListener("click", () => setDetailsOpen(!detailsElement.classList.contains("is-open")));
  diagnosticsSection.addEventListener("toggle", () => {
    if (diagnosticsSection.open) void loadBundleDiagnostics();
  });
  detailsElement.addEventListener("click", (event) => {
    if (event.target.closest("#close-details")) setDetailsOpen(false);
  });
  saveLayoutElement.addEventListener("click", saveLayout);
  loadLayoutElement.addEventListener("click", () => layoutFileElement.click());
  layoutFileElement.addEventListener("change", () => loadLayoutFile(layoutFileElement.files?.[0]));
  window.addEventListener("resize", resizeCanvas);

  tocSection.open = window.innerWidth >= 900;
  legendSection.open = false;
  layoutTools.open = window.innerWidth > 680;

  function fetchAnalysis() {
    return fetch(dataUrl("analysis.json"), { cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); });
  }

  async function fetchBundleSource() {
    const response = await fetch(dataUrl("bundle/index.json"), { cache: "no-store" });
    if (response.status === 404) {
      if (state.workspaceMode) throw new Error("中央ワークスペースのbundle/index.jsonが見つかりません");
      return null;
    }
    if (!response.ok) throw new Error(`bundle/index.json HTTP ${response.status}`);
    const index = await response.json();
    if (!index.overview?.path) return null;
    const overview = await fetchJson(`bundle/${index.overview.path}`);
    if (overview.analysis_schema_version !== index.analysis_schema_version) {
      throw new Error("bundle overviewとindexのschema versionが一致しません");
    }
    return { index, overview };
  }

  function fetchOptionalLayout() {
    return fetch(dataUrl("layout.json"), { cache: "no-store" }).then((response) => {
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(`layout.json HTTP ${response.status}`);
      return response.json();
    });
  }

  function renderValidationStatus(validation) {
    if (!validationStatusElement) return;
    const labels = { pending: "検証待ち", running: "全量検証中", valid: "検証済み", invalid: "検証失敗", cancelled: "検証中止" };
    const status = validation?.status || "pending";
    validationStatusElement.hidden = !state.workspaceMode;
    validationStatusElement.className = `validation-status validation-${status}`;
    validationStatusElement.textContent = `データ: ${labels[status] || status}`;
    if (validation?.message) validationStatusElement.title = validation.message;
  }

  function resetRepositoryView() {
    resetChunkCache();
    state.loadedNodeChunks.clear();
    state.loadedEdgeChunks.clear();
    state.loadedOverviewEdgeChunks.clear();
    clearSelection();
    state.document = null;
    state.bundle = null;
    state.nodes = [];
    state.edges = [];
    state.overviewEdges = [];
    state.nodeById.clear();
    state.positionById.clear();
    state.moduleByNodeId.clear();
    state.modules = [];
    state.nodesByModule.clear();
    state.moduleBounds.clear();
    state.modulePairEdges.clear();
    state.modulePairKeysByModule.clear();
    state.lowEdgeGroups.clear();
    state.visibleNodes = [];
    state.visibleEdges = [];
    state.searchMatches.clear();
    state.searchNodeHints.clear();
    state.diagnostics = [];
    state.diagnosticsLoaded = false;
    state.diagnosticsTruncated = false;
    state.diagnosticsLoading = null;
    state.availableLanguages = [];
    state.activeLanguages.clear();
    state.languageByNodeId.clear();
    state.virtualModules.clear();
    state.camera = { x: 0, y: 0, zoom: 1 };
    renderLanguageFilter();
    renderToc();
    renderSearchResults("");
    renderDiagnostics();
    setStats(0, 0);
    draw();
  }

  async function refreshValidation(repositoryId, requestId) {
    if (!state.workspaceMode || repositoryId !== state.activeRepositoryId) return;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      if (requestId !== state.repositoryRequestId) return;
      try {
        const response = await fetch(dataUrl("validation"), { cache: "no-store" });
        if (!response.ok) return;
        const validation = await response.json();
        renderValidationStatus(validation);
        if (!["pending", "running"].includes(validation.status)) return;
      } catch (_error) {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  function renderRepositorySelector(catalog) {
    state.repositoryCatalog = catalog;
    state.workspaceMode = Boolean(catalog);
    if (!repositorySelectorElement) return;
    repositorySelectorElement.replaceChildren();
    const repositories = catalog?.repositories || [];
    repositories.forEach((repository) => {
      const option = document.createElement("option");
      option.value = repository.repository_id;
      option.textContent = repository.display_name || repository.repository_id;
      option.title = repository.absolute_path || "";
      repositorySelectorElement.append(option);
    });
    repositorySelectorElement.hidden = !catalog || repositories.length === 0;
    repositorySelectorElement.disabled = repositories.length < 2;
    if (repositorySelectorLabelElement) repositorySelectorLabelElement.hidden = repositorySelectorElement.hidden;
    renderValidationStatus({ status: "pending" });
  }

  async function loadRepository(repositoryId) {
    const requestId = ++state.repositoryRequestId;
    state.activeRepositoryId = repositoryId;
    try {
      window.localStorage.setItem(REPOSITORY_STORAGE_KEY, repositoryId);
    } catch (_error) {
      // Private browsing or a disabled storage backend must not block loading.
    }
    state.dataBase = `/api/repositories/${encodeURIComponent(repositoryId)}`;
    resetRepositoryView();
    searchElement.value = "";
    renderValidationStatus({ status: "pending" });
    setStatus("解析結果を読み込んでいます…");
    try {
      const [bundleSource, layout] = await Promise.all([fetchBundleSource(), fetchOptionalLayout()]);
      if (requestId !== state.repositoryRequestId) return;
      if (bundleSource) {
        setupBundle(bundleSource.index, bundleSource.overview, layout);
      } else {
        setupDocument(await fetchAnalysis(), layout);
      }
      resizeCanvas();
      void refreshValidation(repositoryId, requestId);
    } catch (error) {
      if (requestId !== state.repositoryRequestId) return;
      renderValidationStatus({ status: "invalid", message: error.message });
      setStatus(`解析結果の読み込みに失敗しました: ${error.message}`, true);
      detailsElement.replaceChildren(detailsHeader("読み込みエラー"), codeBlock("connection-map analyze --root <repository>\nconnection-map serve"));
    }
  }

  async function bootstrap() {
    let catalog = null;
    try {
      const response = await fetch("/api/repositories", { cache: "no-store" });
      if (response.ok) catalog = await response.json();
    } catch (_error) {
      catalog = null;
    }
    if (catalog && Array.isArray(catalog.repositories)) {
      renderRepositorySelector(catalog);
      let storedRepositoryId = null;
      try {
        storedRepositoryId = window.localStorage.getItem(REPOSITORY_STORAGE_KEY);
      } catch (_error) {
        storedRepositoryId = null;
      }
      const selected = [
        storedRepositoryId,
        catalog.active_repository_id,
        catalog.repositories[0]?.repository_id,
      ].find((candidate) => candidate && catalog.repositories.some((item) => item.repository_id === candidate));
      if (!selected) throw new Error("中央ワークスペースに解析結果がありません");
      repositorySelectorElement.value = selected;
      await loadRepository(selected);
      return;
    }
    renderRepositorySelector(null);
    state.dataBase = "";
    const [bundleSource, layout] = await Promise.all([fetchBundleSource(), fetchOptionalLayout()]);
    if (bundleSource) setupBundle(bundleSource.index, bundleSource.overview, layout);
    else setupDocument(await fetchAnalysis(), layout);
    resizeCanvas();
  }

  repositorySelectorElement?.addEventListener("change", () => {
    if (repositorySelectorElement.value) void loadRepository(repositorySelectorElement.value);
  });
  bootstrap().catch((error) => {
    setStatus(`解析結果の読み込みに失敗しました: ${error.message}`, true);
    detailsElement.replaceChildren(detailsHeader("読み込みエラー"), codeBlock("connection-map analyze --root <repository>\nconnection-map serve"));
  });
})();
