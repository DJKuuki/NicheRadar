/* =========================================================
   NicheRadar Dashboard — app.js
   Client-side renderer for logs/dashboard_report.json
   ========================================================= */

(() => {
  "use strict";

  // ---------- DOM helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  };

  // ---------- Formatting ----------
  const fmt = {
    num(v, digits = 2) {
      if (v == null || Number.isNaN(v)) return "—";
      return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
    },
    int(v) {
      if (v == null || Number.isNaN(v)) return "—";
      return Number(v).toLocaleString();
    },
    pct(v, digits = 1) {
      if (v == null || Number.isNaN(v)) return "—";
      return `${(Number(v) * 100).toFixed(digits)}%`;
    },
    signedPct(v, digits = 2) {
      if (v == null || Number.isNaN(v)) return "—";
      const p = Number(v) * 100;
      return `${p >= 0 ? "+" : ""}${p.toFixed(digits)}%`;
    },
    signed(v, digits = 4) {
      if (v == null || Number.isNaN(v)) return "—";
      const n = Number(v);
      return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}`;
    },
    time(s) {
      if (!s) return "—";
      try {
        const d = new Date(s);
        if (Number.isNaN(d.getTime())) return s;
        return d.toLocaleString();
      } catch { return s; }
    },
    short(s, n = 40) {
      if (!s) return "";
      return s.length > n ? s.slice(0, n - 1) + "…" : s;
    },
  };

  const sideBadge = (side) => {
    if (!side) return el("span", { class: "badge badge-muted" }, "—");
    const cls = side === "BUY_YES" ? "badge-buy-yes" : side === "BUY_NO" ? "badge-buy-no" : "badge-muted";
    return el("span", { class: `badge ${cls}` }, side);
  };

  const flagsCell = (row) => {
    const wrap = el("span");
    wrap.appendChild(el("span", {
      class: `badge ${row.signal_ok ? "badge-ok" : "badge-bad"}`,
      style: "margin-right:4px"
    }, row.signal_ok ? "signal" : "no-signal"));
    wrap.appendChild(el("span", {
      class: `badge ${row.market_ok ? "badge-ok" : "badge-bad"}`
    }, row.market_ok ? "market" : "no-market"));
    return wrap;
  };

  const marketCell = (row) => {
    return el("div", { class: "market-cell" }, [
      el("span", {}, row.label || row.slug || "—"),
      row.slug ? el("span", { class: "slug" }, row.slug) : null,
    ]);
  };

  const signedNumCell = (v, digits = 4) => {
    const cls = v == null || Number.isNaN(v) ? "" : v >= 0 ? "pos" : "neg";
    return el("td", { class: `num ${cls}` }, fmt.signed(v, digits));
  };

  const signedPctCell = (v, digits = 2) => {
    const cls = v == null || Number.isNaN(v) ? "" : v >= 0 ? "pos" : "neg";
    return el("td", { class: `num ${cls}` }, fmt.signedPct(v, digits));
  };

  // ---------- Chart defaults ----------
  if (window.Chart) {
    Chart.defaults.color = "#9aa7bd";
    Chart.defaults.borderColor = "rgba(255,255,255,0.06)";
    Chart.defaults.font.family = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    Chart.defaults.font.size = 11;
  }

  const palette = ["#60a5fa", "#a78bfa", "#34d399", "#fbbf24", "#f87171", "#2dd4bf", "#f472b6", "#fb923c"];

  // ---------- Chart store (so re-renders don't leak instances) ----------
  const charts = {};
  const destroyChart = (key) => {
    if (charts[key]) { try { charts[key].destroy(); } catch {} delete charts[key]; }
  };

  // ---------- State ----------
  const state = {
    data: null,
    filtered: { markets: [] },
    autoTimer: null,
  };

  // =========================================================
  // RENDERERS
  // =========================================================

  function setStatus(kind, text) {
    const pill = $("#dataStatus");
    pill.classList.remove("ok", "err", "loading");
    if (kind) pill.classList.add(kind);
    $("#dataStatusText").textContent = text;
  }

  function renderKpis(data) {
    const counts = data.counts || {};
    const back = data.backtest_summary || {};
    const port = data.portfolio_risk || {};

    const kpis = [
      { label: "Markets", value: fmt.int(counts.markets), sub: `${fmt.int(counts.snapshots)} snapshots` },
      { label: "Alerts", value: fmt.int(counts.alerts), sub: "watchlist signals" },
      { label: "Shadow Fills", value: fmt.int(counts.shadow_fills), sub: `${fmt.int(counts.shadow_positions)} positions` },
      { label: "Total PnL", value: fmt.signed(back.total_pnl, 4), sub: `win ${fmt.pct(back.win_rate)}`,
        tone: back.total_pnl == null ? "" : back.total_pnl >= 0 ? "pos" : "neg" },
      { label: "Bankroll Exposure", value: fmt.pct(port.total_exposure_pct ?? 0), sub: `$${fmt.num(port.total_exposure, 2)} / $${fmt.num(port.bankroll, 0)}` },
      { label: "Circuit Breaker", value: port.circuit_breaker_active ? "ACTIVE" : "ok",
        sub: (port.circuit_breaker_reasons || []).join(", ") || "no triggers",
        tone: port.circuit_breaker_active ? "neg" : "pos" },
    ];

    const target = $("#kpis");
    target.innerHTML = "";
    for (const k of kpis) {
      target.appendChild(el("div", { class: `kpi ${k.tone || ""}` }, [
        el("div", { class: "kpi-label" }, k.label),
        el("div", { class: "kpi-value" }, String(k.value)),
        el("div", { class: "kpi-sub" }, k.sub || ""),
      ]));
    }
  }

  // ---------- Overview ----------
  function renderEdgeChart(data) {
    const rows = data.edge_by_event_type || [];
    destroyChart("edge");
    const ctx = $("#edgeChart");
    if (!rows.length) return;
    charts.edge = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map(r => r.event_type),
        datasets: [
          {
            label: "Avg Net Edge",
            data: rows.map(r => r.avg_net_edge),
            backgroundColor: palette[0],
            borderRadius: 6,
          },
          {
            label: "Max Net Edge",
            data: rows.map(r => r.max_net_edge),
            backgroundColor: palette[1],
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "top", labels: { boxWidth: 10 } } },
        scales: {
          y: { ticks: { callback: (v) => (v * 100).toFixed(0) + "%" } },
        },
      },
    });
  }

  function renderCoverageChart(data) {
    const rows = data.edge_by_event_type || [];
    destroyChart("coverage");
    const ctx = $("#coverageChart");
    if (!rows.length) return;
    charts.coverage = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map(r => r.event_type),
        datasets: [
          {
            label: "Snapshots",
            data: rows.map(r => r.count),
            backgroundColor: palette[2],
            borderRadius: 6,
          },
          {
            label: "Signal OK",
            data: rows.map(r => r.signal_ok_count),
            backgroundColor: palette[3],
            borderRadius: 6,
          },
          {
            label: "Market OK",
            data: rows.map(r => r.market_ok_count),
            backgroundColor: palette[5],
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "top", labels: { boxWidth: 10 } } },
      },
    });
  }

  function renderTopEdges(data) {
    const rows = data.top_edges || [];
    const tbody = $("#topEdgesTable tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: 9, class: "empty" }, "No top edges available.")));
      return;
    }
    for (const r of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", {}, marketCell(r)));
      tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.event_type || "—")));
      tr.appendChild(el("td", {}, r.platform || "—"));
      tr.appendChild(el("td", {}, sideBadge(r.preferred_side)));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.p_model, 4)));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.preferred_price, 4)));
      tr.appendChild(signedNumCell(r.net_edge, 4));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.evidence_score, 3)));
      tr.appendChild(el("td", {}, flagsCell(r)));
      tbody.appendChild(tr);
    }
  }

  // ---------- Markets ----------
  function populateMarketFilters(data) {
    const sel = $("#marketTypeFilter");
    const current = sel.value;
    sel.innerHTML = '<option value="">All event types</option>';
    const types = Array.from(new Set((data.latest_markets || []).map(m => m.event_type).filter(Boolean)));
    for (const t of types) sel.appendChild(el("option", { value: t }, t));
    if (types.includes(current)) sel.value = current;
  }

  function applyMarketFilters() {
    if (!state.data) return;
    const q = $("#marketSearch").value.trim().toLowerCase();
    const type = $("#marketTypeFilter").value;
    const status = $("#marketStatusFilter").value;

    let rows = state.data.latest_markets || [];
    if (q) rows = rows.filter(r => (r.label || "").toLowerCase().includes(q) || (r.slug || "").toLowerCase().includes(q));
    if (type) rows = rows.filter(r => r.event_type === type);
    if (status === "signal_ok") rows = rows.filter(r => r.signal_ok);
    else if (status === "market_ok") rows = rows.filter(r => r.market_ok);
    else if (status === "both") rows = rows.filter(r => r.signal_ok && r.market_ok);

    state.filtered.markets = rows;
    renderMarketsTable(rows);
  }

  function renderMarketsTable(rows) {
    const tbody = $("#marketsTable tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: 10, class: "empty" }, "No markets match the current filter.")));
      return;
    }
    for (const r of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", {}, marketCell(r)));
      tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.event_type || "—")));
      tr.appendChild(el("td", {}, r.platform || "—"));
      tr.appendChild(el("td", {}, sideBadge(r.preferred_side)));
      tr.appendChild(el("td", {}, sideBadge(r.model_side)));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.preferred_price, 4)));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.p_model, 4)));
      tr.appendChild(signedNumCell(r.net_edge, 4));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.evidence_score, 3)));
      tr.appendChild(el("td", {}, flagsCell(r)));
      tbody.appendChild(tr);
    }
  }

  // ---------- Shadow ----------
  function renderShadowSummary(data) {
    const rows = data.shadow_summary_by_event_type || [];
    const tbody = $("#shadowSummaryTable tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: 8, class: "empty" }, "No shadow summary data.")));
    } else {
      for (const r of rows) {
        const tr = el("tr");
        tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.event_type)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.count)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.open_count)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.closed_count)));
        tr.appendChild(el("td", { class: "num" }, fmt.pct(r.win_rate)));
        tr.appendChild(signedNumCell(r.total_pnl, 4));
        tr.appendChild(signedNumCell(r.realized_pnl, 4));
        tr.appendChild(signedNumCell(r.unrealized_pnl, 4));
        tbody.appendChild(tr);
      }
    }

    destroyChart("shadowPnl");
    if (rows.length) {
      charts.shadowPnl = new Chart($("#shadowPnlChart"), {
        type: "bar",
        data: {
          labels: rows.map(r => r.event_type),
          datasets: [
            { label: "Realized",   data: rows.map(r => r.realized_pnl ?? 0),   backgroundColor: palette[2], stack: "pnl", borderRadius: 6 },
            { label: "Unrealized", data: rows.map(r => r.unrealized_pnl ?? 0), backgroundColor: palette[3], stack: "pnl", borderRadius: 6 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: "top", labels: { boxWidth: 10 } } },
          scales: { x: { stacked: true }, y: { stacked: true } },
        },
      });
    }
  }

  function renderShadowPositions(data) {
    const rows = data.shadow_positions || [];
    const tbody = $("#shadowPositionsTable tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: 11, class: "empty" }, "No shadow positions.")));
      return;
    }
    for (const r of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "num" }, "#" + (r.fill_id ?? "—")));
      tr.appendChild(el("td", {}, marketCell(r)));
      tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.event_type || "—")));
      tr.appendChild(el("td", {}, sideBadge(r.side)));
      tr.appendChild(el("td", {}, el("span", { class: "badge badge-status" }, r.status || "—")));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.fill_price, 4)));
      tr.appendChild(el("td", { class: "num" }, fmt.num(r.current_price, 4)));
      tr.appendChild(signedNumCell(r.pnl, 4));
      tr.appendChild(signedPctCell(r.pnl_pct, 2));
      tr.appendChild(signedNumCell(r.net_edge, 4));
      tr.appendChild(el("td", {}, fmt.time(r.opened_at_utc)));
      tbody.appendChild(tr);
    }
  }

  // ---------- Backtest ----------
  function renderBacktest(data) {
    const back = data.backtest_summary || {};
    const targets = data.backtest_target_source_counts || {};

    const kpis = [
      { label: "Samples", value: fmt.int(back.samples), sub: `settled ${fmt.int(back.settled_samples)} · marks ${fmt.int(back.mark_only_samples)}` },
      { label: "Brier Score", value: fmt.num(back.brier_score, 4),
        sub: `mid ${fmt.num(back.market_mid_brier_score, 4)}`,
        tone: back.brier_score != null && back.market_mid_brier_score != null && back.brier_score < back.market_mid_brier_score ? "pos" : "" },
      { label: "Calibration Err", value: fmt.num(back.calibration_error, 4), sub: `log loss ${fmt.num(back.log_loss, 4)}` },
      { label: "Reliability", value: (back.reliability_status || "—").toUpperCase(),
        sub: `coverage ${fmt.pct(back.settled_sample_coverage ?? 0)}`,
        tone: back.reliability_status === "reliable" ? "pos" : back.reliability_status === "insufficient" ? "warn" : "" },
    ];
    const target = $("#backtestKpis");
    target.innerHTML = "";
    for (const k of kpis) {
      target.appendChild(el("div", { class: `kpi ${k.tone || ""}` }, [
        el("div", { class: "kpi-label" }, k.label),
        el("div", { class: "kpi-value" }, String(k.value)),
        el("div", { class: "kpi-sub" }, k.sub),
      ]));
    }

    const cal = data.backtest_calibration_by_profile || [];
    const calTbody = $("#calibrationTable tbody");
    calTbody.innerHTML = "";
    if (!cal.length) {
      calTbody.appendChild(el("tr", {}, el("td", { colspan: 7, class: "empty" }, "No calibration data.")));
    } else {
      for (const r of cal) {
        const tr = el("tr");
        tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.model_profile)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.count)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.settled)));
        tr.appendChild(el("td", { class: "num" }, fmt.num(r.avg_p_model, 4)));
        tr.appendChild(el("td", { class: "num" }, fmt.num(r.observed_yes_rate, 4)));
        tr.appendChild(signedNumCell(r.error, 4));
        tr.appendChild(el("td", { class: "num" }, fmt.num(r.brier, 4)));
        calTbody.appendChild(tr);
      }
    }

    destroyChart("calibration");
    if (cal.length) {
      charts.calibration = new Chart($("#calibrationChart"), {
        type: "bar",
        data: {
          labels: cal.map(r => r.model_profile),
          datasets: [
            { label: "Avg p_model",      data: cal.map(r => r.avg_p_model),       backgroundColor: palette[0], borderRadius: 6 },
            { label: "Observed YES rate", data: cal.map(r => r.observed_yes_rate), backgroundColor: palette[2], borderRadius: 6 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: "top", labels: { boxWidth: 10 } } },
          scales: { y: { min: 0, max: 1, ticks: { callback: (v) => (v * 100).toFixed(0) + "%" } } },
        },
      });
    }

    const pnl = data.backtest_pnl_by_profile || [];
    const pnlTbody = $("#profilePnlTable tbody");
    pnlTbody.innerHTML = "";
    if (!pnl.length) {
      pnlTbody.appendChild(el("tr", {}, el("td", { colspan: 8, class: "empty" }, "No profile PnL data.")));
    } else {
      for (const r of pnl) {
        const tr = el("tr");
        tr.appendChild(el("td", {}, el("span", { class: "badge badge-type" }, r.model_profile)));
        tr.appendChild(el("td", { class: "num" }, fmt.int(r.fills)));
        tr.appendChild(signedNumCell(r.total_pnl, 4));
        tr.appendChild(signedNumCell(r.avg_pnl, 4));
        tr.appendChild(el("td", { class: "num" }, r.win_rate == null ? "—" : fmt.pct(r.win_rate)));
        tr.appendChild(signedNumCell(r.max_drawdown, 4));
        tr.appendChild(el("td", { class: "num" }, fmt.num(r.avg_entry_price, 4)));
        tr.appendChild(el("td", { class: "num" }, fmt.num(r.avg_exit_price, 4)));
        pnlTbody.appendChild(tr);
      }
    }

    destroyChart("profilePnl");
    if (pnl.length) {
      charts.profilePnl = new Chart($("#profilePnlChart"), {
        type: "bar",
        data: {
          labels: pnl.map(r => r.model_profile),
          datasets: [
            {
              label: "Total PnL",
              data: pnl.map(r => r.total_pnl ?? 0),
              backgroundColor: pnl.map(r => (r.total_pnl ?? 0) >= 0 ? palette[2] : palette[4]),
              borderRadius: 6,
            },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
        },
      });
    }
  }

  // ---------- Risk ----------
  function renderRisk(data) {
    const port = data.portfolio_risk || {};
    const kpis = [
      { label: "Bankroll", value: "$" + fmt.num(port.bankroll, 0), sub: `${fmt.int(port.open_positions)} open positions` },
      { label: "Total Exposure", value: "$" + fmt.num(port.total_exposure, 2), sub: fmt.pct(port.total_exposure_pct ?? 0) + " of bankroll" },
      { label: "Unrealized PnL", value: fmt.signed(port.unrealized_pnl, 4),
        sub: fmt.signedPct(port.unrealized_pnl_pct ?? 0, 4),
        tone: (port.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg" },
      { label: "Circuit Breaker", value: port.circuit_breaker_active ? "ACTIVE" : "ok",
        sub: (port.circuit_breaker_reasons || []).join(", ") || "no triggers",
        tone: port.circuit_breaker_active ? "neg" : "pos" },
    ];
    const target = $("#riskKpis");
    target.innerHTML = "";
    for (const k of kpis) {
      target.appendChild(el("div", { class: `kpi ${k.tone || ""}` }, [
        el("div", { class: "kpi-label" }, k.label),
        el("div", { class: "kpi-value" }, String(k.value)),
        el("div", { class: "kpi-sub" }, k.sub),
      ]));
    }

    const byType = port.exposure_by_event_type || {};
    destroyChart("exposureType");
    if (Object.keys(byType).length) {
      charts.exposureType = new Chart($("#exposureTypeChart"), {
        type: "doughnut",
        data: {
          labels: Object.keys(byType),
          datasets: [{
            data: Object.values(byType),
            backgroundColor: palette,
            borderColor: "rgba(0,0,0,0)",
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: "right", labels: { boxWidth: 10 } } },
        },
      });
    }

    const bySlug = port.exposure_by_slug || {};
    destroyChart("exposureMarket");
    if (Object.keys(bySlug).length) {
      const labels = Object.keys(bySlug);
      charts.exposureMarket = new Chart($("#exposureMarketChart"), {
        type: "bar",
        data: {
          labels: labels.map(s => fmt.short(s, 28)),
          datasets: [{
            label: "Exposure ($)",
            data: labels.map(s => bySlug[s]),
            backgroundColor: palette[0],
            borderRadius: 6,
          }],
        },
        options: {
          indexAxis: "y",
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
        },
      });
    }

    const cb = $("#circuitBreakerBody");
    cb.innerHTML = "";
    if (port.circuit_breaker_active) {
      cb.appendChild(el("div", { class: "badge badge-bad", style: "font-size:13px; padding:6px 12px" },
        "ACTIVE — trading paused"));
      const reasons = port.circuit_breaker_reasons || [];
      if (reasons.length) {
        cb.appendChild(el("ul", { style: "margin:10px 0 0; padding-left:20px; color:var(--ink-dim)" },
          reasons.map(r => el("li", {}, r))));
      }
    } else {
      cb.appendChild(el("div", { class: "badge badge-ok", style: "font-size:13px; padding:6px 12px" },
        "no triggers — circuit breaker inactive"));
    }
  }

  // ---------- Alerts ----------
  function renderAlerts(data) {
    const rows = data.recent_alerts || [];
    const tbody = $("#alertsTable tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: 3, class: "empty" }, "No recent alerts.")));
      return;
    }
    for (const r of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", {}, fmt.time(r.timestamp_utc)));
      tr.appendChild(el("td", {}, marketCell(r)));
      const reasons = el("td");
      for (const reason of r.alert_reasons || []) {
        reasons.appendChild(el("span", { class: "badge badge-warn", style: "margin-right:4px" }, reason));
      }
      tr.appendChild(reasons);
      tbody.appendChild(tr);
    }
  }

  // ---------- Raw ----------
  function renderRaw(data) {
    $("#rawJson").textContent = JSON.stringify(data, null, 2);
    $("#rawMeta").textContent = `generated ${fmt.time(data.generated_at_utc)} · db ${data.db_path || "—"}`;
  }

  // =========================================================
  // MAIN
  // =========================================================

  function renderAll(data) {
    state.data = data;
    renderKpis(data);

    renderEdgeChart(data);
    renderCoverageChart(data);
    renderTopEdges(data);

    populateMarketFilters(data);
    applyMarketFilters();

    renderShadowSummary(data);
    renderShadowPositions(data);

    renderBacktest(data);
    renderRisk(data);
    renderAlerts(data);
    renderRaw(data);
  }

  async function loadData() {
    const url = $("#reportSource").value;
    setStatus("loading", "Loading…");
    try {
      const res = await fetch(url + "?_=" + Date.now(), { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAll(data);
      setStatus("ok", `Loaded · ${fmt.time(data.generated_at_utc)}`);
    } catch (err) {
      console.error(err);
      setStatus("err", `Failed: ${err.message}. Serve via HTTP from the project root (e.g. \`python -m http.server\`).`);
    }
  }

  // ---------- Wire up UI ----------
  function setupTabs() {
    $$(".tab").forEach(btn => {
      btn.addEventListener("click", () => {
        $$(".tab").forEach(b => b.classList.remove("active"));
        $$(".tab-panel").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        const target = btn.dataset.tab;
        const panel = document.querySelector(`.tab-panel[data-panel="${target}"]`);
        if (panel) panel.classList.add("active");
      });
    });
  }

  function setupControls() {
    $("#reloadBtn").addEventListener("click", loadData);
    $("#reportSource").addEventListener("change", loadData);

    $("#autoRefresh").addEventListener("change", (e) => {
      if (state.autoTimer) { clearInterval(state.autoTimer); state.autoTimer = null; }
      if (e.target.checked) state.autoTimer = setInterval(loadData, 30_000);
    });

    const debounce = (fn, ms = 150) => {
      let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    };
    $("#marketSearch").addEventListener("input", debounce(applyMarketFilters));
    $("#marketTypeFilter").addEventListener("change", applyMarketFilters);
    $("#marketStatusFilter").addEventListener("change", applyMarketFilters);
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupTabs();
    setupControls();
    loadData();
  });
})();
