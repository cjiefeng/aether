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
        group.querySelectorAll("button").forEach(function (b) {
          b.classList.remove("active");
          b.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        renderOverview(el, chart, btn.dataset.range);
      });
    }
    renderOverview(el, chart, el.dataset.range || "1y");
  }

  // ------------------------------------------------------------------ single ticker

  // Event markers (M9): classified events at their anchor session t0, coloured by class.
  var MARKER_CLASSES = [
    { cls: "SIGNAL", name: "SIGNAL events", color: "--ok", symbol: "triangle" },
    { cls: "RISK", name: "RISK events", color: "--bad", symbol: "pin" },
    { cls: "NOISE", name: "NOISE events", color: "--muted", symbol: "circle" },
  ];

  function markerText(m) {
    var z = m.z5 == null ? "z5 n/a (" + m.status + ")" : "z5 " + (m.z5 >= 0 ? "+" : "") + m.z5.toFixed(1);
    var agree = m.agreement ? " · market " + m.agreement : "";
    return m.cls + " · " + m.category.replace(/_/g, " ") + " · m" + m.materiality + " · " + z + agree +
      "\n" + m.title;
  }

  function setupTicker(el) {
    var chart = init(el);
    var markersReq = el.dataset.markers
      ? getJSON(el.dataset.markers).catch(function () { return { markers: [] }; })
      : Promise.resolve({ markers: [] });
    Promise.all([getJSON(el.dataset.src), markersReq])
      .then(function (bodies) {
        var body = bodies[0];
        var markers = bodies[1].markers || [];
        if (body.bars.length === 0) {
          message(el, "No price data yet.");
          return;
        }
        var t = theme();
        var close = body.bars.map(function (b) { return [b.d, b.c]; });
        var vol = body.bars.map(function (b) { return [b.d, b.v]; });
        var opt = baseOption(t);
        opt.legend.show = false;
        var fmt = function (v) {
          return v == null ? "" : (v >= 1e5 ? Math.round(v).toLocaleString() : v.toFixed(2));
        };
        opt.tooltip.valueFormatter = fmt;
        if (markers.length) {
          opt.tooltip.formatter = function (params) {
            var lines = [];
            params.forEach(function (p) {
              if (p.data && p.data.ev) {
                lines.push(markerText(p.data.ev));
              } else if (p.seriesName === "Close" || p.seriesName === "Volume") {
                lines.push(p.seriesName + ": " + fmt(Array.isArray(p.value) ? p.value[1] : p.value));
              }
            });
            return (params.length ? params[0].axisValueLabel + "\n" : "") + lines.join("\n");
          };
        }
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
        if (markers.length) {
          var closeOn = {};
          close.forEach(function (c) { closeOn[c[0]] = c[1]; });
          MARKER_CLASSES.forEach(function (mc) {
            var data = markers
              .filter(function (m) { return m.cls === mc.cls && closeOn[m.d] != null; })
              .map(function (m) { return { value: [m.d, closeOn[m.d]], ev: m }; });
            if (data.length) {
              opt.series.push({
                name: mc.name, type: "scatter", data: data, xAxisIndex: 0, yAxisIndex: 0,
                symbol: mc.symbol, symbolSize: 9, itemStyle: { color: cssVar(mc.color) }, z: 5,
              });
            }
          });
          opt.legend.show = true;
          opt.legend.data = MARKER_CLASSES.map(function (mc) { return mc.name; });
          opt.grid[0].top = 36;
        }
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  // ------------------------------------------------------------------ shares outstanding

  function setupDilution(el) {
    var chart = init(el);
    getJSON(el.dataset.src)
      .then(function (body) {
        var series = body.series.filter(function (s) { return s.data.length > 0; });
        if (series.length === 0) {
          message(el, "No XBRL share counts yet.");
          return;
        }
        var t = theme();
        var opt = baseOption(t);
        opt.grid = { left: 72, right: 16, top: 48, bottom: 32 };
        opt.xAxis = Object.assign({ type: "time" }, axisStyle(t), { splitLine: { show: false } });
        opt.yAxis = Object.assign({ type: "value", scale: true }, axisStyle(t), {
          axisLabel: {
            color: t.muted,
            formatter: function (v) { return (v / 1e6).toFixed(0) + "M"; },
          },
        });
        opt.tooltip.valueFormatter = function (v) {
          return v == null ? "" : Math.round(v).toLocaleString();
        };
        opt.series = series.map(function (s) {
          return { name: s.name, type: "line", showSymbol: true, symbolSize: 5, data: s.data };
        });
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  // ------------------------------------------------------------------ strategies (M4)

  function setupStrategy(el, key) {
    var chart = init(el);
    getJSON(el.dataset.src)
      .then(function (body) {
        var series = body[key].filter(function (s) { return s.data.length > 0; });
        if (series.length === 0) {
          message(el, "No backtest curves yet.");
          return;
        }
        var t = theme();
        var opt = baseOption(t);
        opt.title = {
          text: key === "equity" ? "Equity (start = 100)" : "Drawdown (%)",
          left: "center", bottom: 0, textStyle: { color: t.muted, fontSize: 12, fontWeight: "normal" },
        };
        opt.grid = { left: 48, right: 16, top: 36, bottom: 40 };
        opt.xAxis = Object.assign({ type: "time" }, axisStyle(t), { splitLine: { show: false } });
        opt.yAxis = Object.assign({ type: "value", scale: key === "equity" }, axisStyle(t));
        opt.tooltip.valueFormatter = function (v) { return v == null ? "" : v.toFixed(1); };
        opt.series = series.map(function (s) {
          var line = { name: s.name, type: "line", showSymbol: false, data: s.data };
          if (key === "drawdown") line.areaStyle = { opacity: 0.08 };
          return line;
        });
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  // ------------------------------------------------------------------ catalysts timeline (M8)

  function setupCatalysts(el) {
    var chart = init(el);
    getJSON(el.dataset.src)
      .then(function (body) {
        if (body.items.length === 0) {
          message(el, "No upcoming catalysts.");
          return;
        }
        var t = theme();
        var symbols = [];
        body.items.forEach(function (it) {
          if (symbols.indexOf(it.symbol) < 0) symbols.push(it.symbol);
        });
        var kinds = [];
        body.items.forEach(function (it) {
          if (kinds.indexOf(it.kind) < 0) kinds.push(it.kind);
        });
        var opt = baseOption(t);
        opt.grid = { left: 64, right: 16, top: 36, bottom: 32 };
        opt.tooltip.trigger = "item";
        opt.tooltip.formatter = function (p) {
          var it = p.data.item;
          return it.start + (it.end && it.end !== it.start ? " to " + it.end : "") + "\n" +
            it.symbol + ": " + it.title;
        };
        opt.xAxis = Object.assign({ type: "time" }, axisStyle(t), { splitLine: { show: false } });
        opt.yAxis = Object.assign({ type: "category", data: symbols, inverse: true }, axisStyle(t));
        opt.series = kinds.map(function (k) {
          return {
            name: k,
            type: "scatter",
            symbolSize: 12,
            data: body.items.filter(function (it) { return it.kind === k; }).map(function (it) {
              return { value: [it.start, it.symbol], item: it };
            }),
          };
        });
        chart.setOption(opt, true);
      })
      .catch(function (err) { message(el, "Chart unavailable (" + err.message + ")."); });
  }

  function start() {
    if (typeof echarts === "undefined") return;
    document.querySelectorAll('[data-chart="overview"]').forEach(setupOverview);
    document.querySelectorAll('[data-chart="ticker"]').forEach(setupTicker);
    document.querySelectorAll('[data-chart="dilution"]').forEach(setupDilution);
    document.querySelectorAll('[data-chart="catalysts"]').forEach(setupCatalysts);
    document.querySelectorAll('[data-chart="strategy-equity"]').forEach(function (el) {
      setupStrategy(el, "equity");
    });
    document.querySelectorAll('[data-chart="strategy-drawdown"]').forEach(function (el) {
      setupStrategy(el, "drawdown");
    });
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
