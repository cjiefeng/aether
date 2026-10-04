/* Aether charts. Reads data-* attributes, fetches JSON from /api, renders with ECharts.
 * No inline scripts or styles (CSP). Tooltips use renderMode "richText" (drawn on the canvas),
 * so no HTML strings are injected into the page. Colours come from CSS custom properties. */
(function () {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function theme() {
    return {
      fg: cssVar("--fg"),
      muted: cssVar("--muted"),
      line: cssVar("--line"),
      card: cssVar("--card"),
      palette: ["--c1", "--c2", "--c3", "--c4", "--c5"].map(cssVar),
    };
  }

  function baseOption(t) {
    return {
      animation: false,
      color: t.palette,
      textStyle: { color: t.fg },
      tooltip: {
        trigger: "axis",
        renderMode: "richText",
        backgroundColor: t.card,
        borderColor: t.line,
        textStyle: { color: t.fg },
      },
      legend: { top: 0, textStyle: { color: t.fg } },
    };
  }

  function axisStyle(t) {
    return {
      axisLine: { lineStyle: { color: t.line } },
      axisLabel: { color: t.muted },
      splitLine: { lineStyle: { color: t.line } },
    };
  }

  function message(el, text) {
    el.textContent = text;
    el.classList.add("chart-empty");
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  var charts = [];

  function init(el) {
    var c = echarts.init(el, null, { renderer: "canvas" });
    charts.push(c);
    return c;
  }

  // ------------------------------------------------------------------ overview comparison

  function renderOverview(el, chart, range) {
    getJSON(el.dataset.src + "?range=" + encodeURIComponent(range))
      .then(function (body) {
        var series = body.series.filter(function (s) { return s.data.length > 0; });
        if (series.length === 0) {
          chart.clear();
          message(el, "No price data yet.");
          return;
        }
        el.classList.remove("chart-empty");
        var t = theme();
        var opt = baseOption(t);
        opt.grid = { left: 48, right: 16, top: 36, bottom: 32 };
        opt.xAxis = Object.assign({ type: "time" }, axisStyle(t), { splitLine: { show: false } });
        // Log scale: rebased performance lines can differ by 10x+ over two years.
        opt.yAxis = Object.assign({
          type: "log",
          logBase: 10,
          min: function (v) { return Math.floor(v.min * 0.95); },
          max: function (v) { return Math.ceil(v.max * 1.05); },
        }, axisStyle(t));
        opt.tooltip.valueFormatter = function (v) { return v == null ? "" : v.toFixed(1); };
        opt.series = series.map(function (s) {
          return { name: s.name, type: "line", showSymbol: false, data: s.data };
        });
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  function setupOverview(el) {
    var chart = init(el);
    var group = document.querySelector('.range-group[data-for="' + el.id + '"]');
    if (group) {
      group.addEventListener("click", function (ev) {
        var btn = ev.target.closest("button[data-range]");
        if (!btn) return;
        group.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        renderOverview(el, chart, btn.dataset.range);
      });
    }
    renderOverview(el, chart, el.dataset.range || "1y");
  }

  // ------------------------------------------------------------------ single ticker

  function setupTicker(el) {
    var chart = init(el);
    getJSON(el.dataset.src)
      .then(function (body) {
        if (body.bars.length === 0) {
          message(el, "No price data yet.");
          return;
        }
        var t = theme();
        var close = body.bars.map(function (b) { return [b.d, b.c]; });
        var vol = body.bars.map(function (b) { return [b.d, b.v]; });
        var opt = baseOption(t);
        opt.legend.show = false;
        opt.tooltip.valueFormatter = function (v) {
          return v == null ? "" : (v >= 1e5 ? Math.round(v).toLocaleString() : v.toFixed(2));
        };
        opt.axisPointer = { link: [{ xAxisIndex: "all" }] };
        opt.grid = [
          { left: 56, right: 16, top: 16, height: "58%" },
          { left: 56, right: 16, top: "76%", bottom: 56 },
        ];
        opt.xAxis = [
          Object.assign({ type: "time", gridIndex: 0 }, axisStyle(t), { splitLine: { show: false } }),
          Object.assign({ type: "time", gridIndex: 1 }, axisStyle(t), { splitLine: { show: false } }),
        ];
        opt.yAxis = [
          Object.assign({ type: "value", scale: true, gridIndex: 0 }, axisStyle(t)),
          Object.assign({ type: "value", gridIndex: 1, splitNumber: 2 }, axisStyle(t)),
        ];
        opt.dataZoom = [
          { type: "inside", xAxisIndex: [0, 1], startValue: close[Math.max(0, close.length - 252)][0] },
          { type: "slider", xAxisIndex: [0, 1], bottom: 8, height: 24,
            textStyle: { color: t.muted }, borderColor: t.line },
        ];
        opt.series = [
          { name: "Close", type: "line", showSymbol: false, data: close, xAxisIndex: 0, yAxisIndex: 0 },
          { name: "Volume", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1,
            itemStyle: { color: t.muted }, large: true },
        ];
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  function start() {
    if (typeof echarts === "undefined") return;
    document.querySelectorAll('[data-chart="overview"]').forEach(setupOverview);
    document.querySelectorAll('[data-chart="ticker"]').forEach(setupTicker);
    window.addEventListener("resize", function () {
      charts.forEach(function (c) { c.resize(); });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
