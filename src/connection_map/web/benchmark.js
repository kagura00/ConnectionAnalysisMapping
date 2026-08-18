(() => {
  "use strict";

  if (new URLSearchParams(window.location.search).get("benchmark") !== "1") return;

  const output = document.createElement("pre");
  output.id = "benchmark-output";
  output.hidden = true;
  output.setAttribute("aria-hidden", "true");
  document.body.append(output);

  const startedAt = performance.now();
  const metrics = {
    startedAt,
    memory: {
      source: performance.memory ? "performance.memory" : "unavailable",
      samples: [],
    },
    frameGaps: [],
    eventDurations: [],
    eventDurationsByName: {},
    navigation: null,
  };

  function percentile(values, ratio) {
    if (!values.length) return null;
    const sorted = [...values].sort((left, right) => left - right);
    const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(sorted.length * ratio) - 1));
    return Number(sorted[index].toFixed(3));
  }

  function summary(values) {
    return {
      count: values.length,
      p50Ms: percentile(values, 0.5),
      p95Ms: percentile(values, 0.95),
      p99Ms: percentile(values, 0.99),
      maxMs: values.length ? Number(Math.max(...values).toFixed(3)) : null,
    };
  }

  function memorySummary() {
    const samples = metrics.memory.samples;
    if (!samples.length) return { source: metrics.memory.source, samples: 0 };
    const used = samples.map((sample) => sample.usedJSHeapSize);
    return {
      source: metrics.memory.source,
      samples: samples.length,
      firstUsedBytes: samples[0].usedJSHeapSize,
      lastUsedBytes: samples.at(-1).usedJSHeapSize,
      minUsedBytes: Math.min(...used),
      maxUsedBytes: Math.max(...used),
      deltaBytes: samples.at(-1).usedJSHeapSize - samples[0].usedJSHeapSize,
      maxGrowthFromFirstBytes: Math.max(...used) - samples[0].usedJSHeapSize,
      heapLimitBytes: samples.at(-1).jsHeapSizeLimit,
    };
  }

  function snapshot() {
    const resourceEntries = performance.getEntriesByType("resource");
    return {
      elapsedMs: Number((performance.now() - startedAt).toFixed(3)),
      memory: memorySummary(),
      frameGap: summary(metrics.frameGaps),
      eventDuration: summary(metrics.eventDurations),
      eventDurationByName: Object.fromEntries(
        Object.entries(metrics.eventDurationsByName).map(([name, values]) => [name, summary(values)]),
      ),
      navigation: metrics.navigation,
      resources: {
        count: resourceEntries.length,
        durationMs: Number(resourceEntries.reduce((total, entry) => total + entry.duration, 0).toFixed(3)),
        transferBytes: resourceEntries.reduce((total, entry) => total + (entry.transferSize || 0), 0),
      },
    };
  }

  function render() {
    output.textContent = JSON.stringify(snapshot(), null, 2);
  }

  function sampleMemory() {
    if (!performance.memory) return;
    metrics.memory.samples.push({
      atMs: Number((performance.now() - startedAt).toFixed(3)),
      usedJSHeapSize: performance.memory.usedJSHeapSize,
      totalJSHeapSize: performance.memory.totalJSHeapSize,
      jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
    });
  }

  function observeEvents() {
    if (typeof PerformanceObserver === "undefined") return;
    const supported = PerformanceObserver.supportedEntryTypes || [];
    if (!supported.includes("event")) return;
    const observer = new PerformanceObserver((list) => {
      list.getEntries().forEach((entry) => {
        const duration = Number(entry.duration);
        if (!Number.isFinite(duration) || duration <= 0 || duration > 10000) return;
        metrics.eventDurations.push(duration);
        if (!metrics.eventDurationsByName[entry.name]) metrics.eventDurationsByName[entry.name] = [];
        metrics.eventDurationsByName[entry.name].push(duration);
      });
    });
    observer.observe({ type: "event", buffered: true, durationThreshold: 0 });
  }

  function collectNavigation() {
    const entry = performance.getEntriesByType("navigation")[0];
    if (!entry) return;
    metrics.navigation = {
      domContentLoadedMs: Number(entry.domContentLoadedEventEnd.toFixed(3)),
      loadMs: Number(entry.loadEventEnd.toFixed(3)),
      responseEndMs: Number(entry.responseEnd.toFixed(3)),
    };
  }

  let previousFrameAt = startedAt;
  function collectFrame(now) {
    const gap = now - previousFrameAt;
    previousFrameAt = now;
    if (gap > 0 && gap < 1000) metrics.frameGaps.push(gap);
    requestAnimationFrame(collectFrame);
  }

  sampleMemory();
  collectNavigation();
  observeEvents();
  requestAnimationFrame(collectFrame);
  const memoryTimer = setInterval(sampleMemory, 250);
  const renderTimer = setInterval(render, 250);
  window.__connectionMapBenchmark = {
    snapshot,
    stop() {
      clearInterval(memoryTimer);
      clearInterval(renderTimer);
      render();
      return snapshot();
    },
  };
  render();
})();
