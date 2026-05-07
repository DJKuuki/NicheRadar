from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import html
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from bot.backtest_dataset import load_backtest_samples
from bot.backtest_reporting import build_backtest_report
from bot.config import BotConfig
from bot.portfolio_risk import load_portfolio_risk_state, state_to_dict
from bot.shadow_replay import Settlement, replay_shadow_pnl


def build_dashboard_report(
    db_path: str,
    limit: int = 10,
    config: BotConfig | None = None,
    settlements: list[Settlement] | None = None,
) -> dict[str, object]:
    config = config or BotConfig()
    settlements = settlements or []
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        snapshots = [_row_dict(row) for row in connection.execute("SELECT * FROM watchlist_snapshots ORDER BY id").fetchall()]
        alerts = [_row_dict(row) for row in connection.execute("SELECT * FROM watchlist_alerts ORDER BY id").fetchall()]
        fills = [_row_dict(row) for row in connection.execute("SELECT * FROM shadow_fills ORDER BY id").fetchall()]

    replay = replay_shadow_pnl(db_path, settlements)
    backtest = build_backtest_report(load_backtest_samples(db_path, settlements), db_path)
    portfolio_state = load_portfolio_risk_state(db_path, config)
    latest = _latest_snapshots_by_slug(snapshots)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "counts": {
            "snapshots": len(snapshots),
            "markets": len(latest),
            "alerts": len(alerts),
            "shadow_fills": len(fills),
            "shadow_positions": replay["record_count"],
        },
        "latest_snapshot_time_utc": _max_value(snapshot.get("timestamp_utc") for snapshot in snapshots),
        "edge_by_event_type": _edge_by_event_type(snapshots),
        "latest_markets": _latest_market_rows(latest, limit),
        "top_edges": _top_edges(latest, limit),
        "alert_summary": _alert_summary(alerts),
        "recent_alerts": _recent_alerts(alerts, limit),
        "shadow_summary_by_event_type": replay["summary_by_event_type"],
        "shadow_positions": replay["records"],
        "backtest_summary": backtest["summary"],
        "backtest_target_source_counts": backtest["target_source_counts"],
        "backtest_calibration_by_profile": backtest["calibration_by_profile"],
        "backtest_pnl_by_profile": backtest["pnl_by_profile"],
        "portfolio_risk": state_to_dict(portfolio_state),
    }


def format_dashboard_report(report: dict[str, object]) -> list[str]:
    counts = _dict(report.get("counts"))
    lines = [
        "dashboard_report",
        f"db_path={report.get('db_path')}",
        f"generated_at_utc={report.get('generated_at_utc')}",
        "counts "
        f"snapshots={counts.get('snapshots', 0)} "
        f"markets={counts.get('markets', 0)} "
        f"alerts={counts.get('alerts', 0)} "
        f"shadow_fills={counts.get('shadow_fills', 0)} "
        f"shadow_positions={counts.get('shadow_positions', 0)}",
    ]
    for row in _list(report.get("edge_by_event_type")):
        lines.append(
            "edge_summary "
            f"event_type={row.get('event_type')} "
            f"count={row.get('count')} "
            f"signal_ok={row.get('signal_ok_count')} "
            f"avg_net_edge={_fmt(row.get('avg_net_edge'))} "
            f"max_net_edge={_fmt(row.get('max_net_edge'))} "
            f"avg_evidence={_fmt(row.get('avg_evidence_score'))}"
        )
    for row in _list(report.get("alert_summary")):
        lines.append(f"alert_summary reason={row.get('reason')} count={row.get('count')}")
    for row in _list(report.get("shadow_summary_by_event_type")):
        lines.append(
            "shadow_summary "
            f"event_type={row.get('event_type')} "
            f"count={row.get('count')} "
            f"total_pnl={_fmt(row.get('total_pnl'))} "
            f"realized_pnl={_fmt(row.get('realized_pnl'))} "
            f"unrealized_pnl={_fmt(row.get('unrealized_pnl'))} "
            f"win_rate={_fmt_pct(row.get('win_rate'))}"
        )
    backtest = _dict(report.get("backtest_summary"))
    lines.append(
        "backtest_summary "
        f"samples={backtest.get('samples', 0)} "
        f"settled_samples={backtest.get('settled_samples', 0)} "
        f"settled_sample_coverage={_fmt_pct(backtest.get('settled_sample_coverage'))} "
        f"shadow_fills={backtest.get('shadow_fills', 0)} "
        f"total_pnl={_fmt(backtest.get('total_pnl'))} "
        f"brier={_fmt(backtest.get('brier_score'))} "
        f"market_mid_brier={_fmt(backtest.get('market_mid_brier_score'))} "
        f"reliability_status={backtest.get('reliability_status')}"
    )
    portfolio = _dict(report.get("portfolio_risk"))
    lines.append(
        "portfolio_risk "
        f"bankroll={_fmt(portfolio.get('bankroll'))} "
        f"open_positions={portfolio.get('open_positions', 0)} "
        f"total_exposure={_fmt(portfolio.get('total_exposure'))} "
        f"total_exposure_pct={_fmt_pct(portfolio.get('total_exposure_pct'))} "
        f"unrealized_pnl={_fmt(portfolio.get('unrealized_pnl'))} "
        f"circuit_breaker={str(portfolio.get('circuit_breaker_active')).lower()}"
    )
    for row in _list(report.get("top_edges")):
        lines.append(
            "top_edge "
            f"slug={row.get('slug')} "
            f"event_type={row.get('event_type')} "
            f"side={row.get('model_side')} "
            f"net_edge={_fmt(row.get('net_edge'))} "
            f"signal_ok={str(row.get('signal_ok')).lower()}"
        )
    return lines


def write_dashboard_markdown(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_markdown_report(report), encoding="utf-8")


def write_dashboard_json(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_dashboard_html(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_html_report(report), encoding="utf-8")


def _markdown_report(report: dict[str, object]) -> str:
    counts = _dict(report.get("counts"))
    lines = [
        "# PolyMarket Shadow Report",
        "",
        f"- Generated UTC: `{report.get('generated_at_utc')}`",
        f"- Database: `{report.get('db_path')}`",
        f"- Latest snapshot UTC: `{report.get('latest_snapshot_time_utc')}`",
        "",
        "## Counts",
        "",
        "| snapshots | markets | alerts | shadow fills | shadow positions |",
        "| ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {counts.get('snapshots', 0)} | {counts.get('markets', 0)} | "
            f"{counts.get('alerts', 0)} | {counts.get('shadow_fills', 0)} | {counts.get('shadow_positions', 0)} |"
        ),
        "",
        "## Edge By Event Type",
        "",
        "| event type | count | signal ok | avg net edge | max net edge | avg evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in _list(report.get("edge_by_event_type")):
        lines.append(
            f"| {row.get('event_type')} | {row.get('count')} | {row.get('signal_ok_count')} | "
            f"{_fmt(row.get('avg_net_edge'))} | {_fmt(row.get('max_net_edge'))} | {_fmt(row.get('avg_evidence_score'))} |"
        )

    lines.extend(
        [
            "",
            "## Alert Summary",
            "",
            "| reason | count |",
            "| --- | ---: |",
        ]
    )
    for row in _list(report.get("alert_summary")):
        lines.append(f"| {row.get('reason')} | {row.get('count')} |")

    lines.extend(
        [
            "",
            "## Shadow PnL",
            "",
            "| event type | count | open | closed | total PnL | realized | unrealized | win rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _list(report.get("shadow_summary_by_event_type")):
        lines.append(
            f"| {row.get('event_type')} | {row.get('count')} | {row.get('open_count')} | {row.get('closed_count')} | "
            f"{_fmt(row.get('total_pnl'))} | {_fmt(row.get('realized_pnl'))} | {_fmt(row.get('unrealized_pnl'))} | "
            f"{_fmt_pct(row.get('win_rate'))} |"
        )

    backtest = _dict(report.get("backtest_summary"))
    target_sources = _dict(report.get("backtest_target_source_counts"))
    lines.extend(
        [
            "",
            "## Backtest Summary",
            "",
            "| samples | settled | settled coverage | mark-only | fills | total PnL | brier | status |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            (
                f"| {backtest.get('samples', 0)} | {backtest.get('settled_samples', 0)} | "
                f"{_fmt_pct(backtest.get('settled_sample_coverage'))} | "
                f"{backtest.get('mark_only_samples', 0)} | {backtest.get('shadow_fills', 0)} | "
                f"{_fmt(backtest.get('total_pnl'))} | {_fmt(backtest.get('brier_score'))} | "
                f"{backtest.get('reliability_status')} |"
            ),
            "",
            "### Backtest Target Sources",
            "",
            "| target source | count |",
            "| --- | ---: |",
        ]
    )
    for source, count in sorted(target_sources.items()):
        lines.append(f"| {source} | {count} |")
    lines.extend(
        [
            "",
            "## Backtest Calibration By Profile",
            "",
            "| profile | count | settled | avg p_model | observed YES | error | brier |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _list(report.get("backtest_calibration_by_profile")):
        lines.append(
            f"| {row.get('model_profile')} | {row.get('count')} | {row.get('settled')} | "
            f"{_fmt(row.get('avg_p_model'))} | {_fmt(row.get('observed_yes_rate'))} | "
            f"{_fmt(row.get('error'))} | {_fmt(row.get('brier'))} |"
        )
    lines.extend(
        [
            "",
            "## Backtest PnL By Profile",
            "",
            "| profile | fills | total PnL | avg PnL | win rate | max drawdown |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _list(report.get("backtest_pnl_by_profile")):
        lines.append(
            f"| {row.get('model_profile')} | {row.get('fills')} | {_fmt(row.get('total_pnl'))} | "
            f"{_fmt(row.get('avg_pnl'))} | {_fmt_pct(row.get('win_rate'))} | {_fmt(row.get('max_drawdown'))} |"
        )

    lines.extend(
        [
            "",
            "## Portfolio Risk",
            "",
            "| bankroll | open positions | total exposure | exposure % | unrealized PnL | unrealized % | circuit breaker |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    portfolio = _dict(report.get("portfolio_risk"))
    lines.append(
        f"| {_fmt(portfolio.get('bankroll'))} | {portfolio.get('open_positions', 0)} | "
        f"{_fmt(portfolio.get('total_exposure'))} | {_fmt_pct(portfolio.get('total_exposure_pct'))} | "
        f"{_fmt(portfolio.get('unrealized_pnl'))} | {_fmt_pct(portfolio.get('unrealized_pnl_pct'))} | "
        f"{portfolio.get('circuit_breaker_active')} |"
    )
    exposure_by_event_type = _dict(portfolio.get("exposure_by_event_type"))
    if exposure_by_event_type:
        lines.extend(
            [
                "",
                "### Exposure By Event Type",
                "",
                "| event type | exposure |",
                "| --- | ---: |",
            ]
        )
        for event_type, exposure in exposure_by_event_type.items():
            lines.append(f"| {event_type} | {_fmt(exposure)} |")

    lines.extend(
        [
            "",
            "## Top Edges",
            "",
            "| slug | event type | model side | net edge | signal ok |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for row in _list(report.get("top_edges")):
        lines.append(
            f"| {row.get('slug')} | {row.get('event_type')} | {row.get('model_side')} | "
            f"{_fmt(row.get('net_edge'))} | {row.get('signal_ok')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _html_report(report: dict[str, object]) -> str:
    counts = _dict(report.get("counts"))
    portfolio = _dict(report.get("portfolio_risk"))
    latest_markets = _list(report.get("latest_markets"))
    top_edges = _list(report.get("top_edges"))
    edge_rows = _list(report.get("edge_by_event_type"))
    shadow_rows = _list(report.get("shadow_summary_by_event_type"))
    backtest = _dict(report.get("backtest_summary"))
    target_sources = _dict(report.get("backtest_target_source_counts"))
    backtest_calibration = _list(report.get("backtest_calibration_by_profile"))
    backtest_pnl = _list(report.get("backtest_pnl_by_profile"))
    positions = _list(report.get("shadow_positions"))
    recent_alerts = _list(report.get("recent_alerts"))
    exposure_by_event_type = _dict(portfolio.get("exposure_by_event_type"))
    html_parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>PolyMarket Shadow Dashboard</title>",
        "<style>",
        _dashboard_css(),
        "</style>",
        "</head>",
        "<body>",
        '<main class="shell">',
        '<header class="page-head">',
        "<div>",
        '<p class="eyebrow">PolyMarket Shadow Bot</p>',
        "<h1>Shadow Dashboard</h1>",
        f'<p class="meta">Generated UTC: {_e(report.get("generated_at_utc"))} &middot; DB: {_e(report.get("db_path"))}</p>',
        f'<p class="meta">Latest snapshot UTC: {_e(report.get("latest_snapshot_time_utc"))}</p>',
        "</div>",
        _status_badge("Circuit breaker", bool(portfolio.get("circuit_breaker_active")), invert=True),
        "</header>",
        '<section class="metric-grid" aria-label="summary metrics">',
        _metric("Snapshots", counts.get("snapshots", 0)),
        _metric("Markets", counts.get("markets", 0)),
        _metric("Alerts", counts.get("alerts", 0)),
        _metric("Shadow fills", counts.get("shadow_fills", 0)),
        _metric("Open positions", portfolio.get("open_positions", 0)),
        _metric("Unrealized PnL", _fmt(portfolio.get("unrealized_pnl")), tone=_tone_number(portfolio.get("unrealized_pnl"))),
        _metric("Backtest status", backtest.get("reliability_status", "unknown")),
        _metric("Settled sample coverage", _fmt_pct(backtest.get("settled_sample_coverage"))),
        "</section>",
        '<section class="split">',
        '<div class="panel">',
        "<h2>Portfolio Risk</h2>",
        '<div class="risk-line"><span>Bankroll</span><strong>' + _fmt(portfolio.get("bankroll")) + "</strong></div>",
        '<div class="risk-line"><span>Total exposure</span><strong>' + _fmt(portfolio.get("total_exposure")) + "</strong></div>",
        _bar("Exposure", portfolio.get("total_exposure_pct"), 1.0),
        _bar("Unrealized PnL", portfolio.get("unrealized_pnl_pct"), 0.05, signed=True),
        "</div>",
        '<div class="panel">',
        "<h2>Exposure By Event Type</h2>",
        _exposure_list(exposure_by_event_type),
        "</div>",
        "</section>",
        '<section class="panel">',
        "<h2>Edge By Event Type</h2>",
        _table(
            ["event type", "count", "market ok", "signal ok", "avg net edge", "max net edge", "avg evidence"],
            [
                [
                    row.get("event_type"),
                    row.get("count"),
                    row.get("market_ok_count"),
                    row.get("signal_ok_count"),
                    _fmt(row.get("avg_net_edge")),
                    _fmt(row.get("max_net_edge")),
                    _fmt(row.get("avg_evidence_score")),
                ]
                for row in edge_rows
            ],
        ),
        "</section>",
        '<section class="split">',
        '<div class="panel">',
        "<h2>Backtest Summary</h2>",
        '<div class="risk-line"><span>Samples</span><strong>' + _e(backtest.get("samples", 0)) + "</strong></div>",
        '<div class="risk-line"><span>Settled samples</span><strong>' + _e(backtest.get("settled_samples", 0)) + "</strong></div>",
        '<div class="risk-line"><span>Settled sample coverage</span><strong>' + _fmt_pct(backtest.get("settled_sample_coverage")) + "</strong></div>",
        '<div class="risk-line"><span>Shadow fills</span><strong>' + _e(backtest.get("shadow_fills", 0)) + "</strong></div>",
        '<div class="risk-line"><span>Total PnL</span><strong>' + _fmt(backtest.get("total_pnl")) + "</strong></div>",
        '<div class="risk-line"><span>Brier score</span><strong>' + _fmt(backtest.get("brier_score")) + "</strong></div>",
        '<div class="risk-line"><span>Market mid Brier</span><strong>' + _fmt(backtest.get("market_mid_brier_score")) + "</strong></div>",
        "<h2>Target Sources</h2>",
        _target_source_list(target_sources),
        "</div>",
        '<div class="panel">',
        "<h2>Backtest Calibration By Profile</h2>",
        _table(
            ["profile", "count", "settled", "avg p_model", "observed YES", "error", "brier"],
            [
                [
                    row.get("model_profile"),
                    row.get("count"),
                    row.get("settled"),
                    _fmt(row.get("avg_p_model")),
                    _fmt(row.get("observed_yes_rate")),
                    _fmt(row.get("error")),
                    _fmt(row.get("brier")),
                ]
                for row in backtest_calibration
            ],
        ),
        "</div>",
        "</section>",
        '<section class="panel">',
        "<h2>Backtest PnL By Profile</h2>",
        _table(
            ["profile", "fills", "total PnL", "avg PnL", "win rate", "max drawdown"],
            [
                [
                    row.get("model_profile"),
                    row.get("fills"),
                    _fmt(row.get("total_pnl")),
                    _fmt(row.get("avg_pnl")),
                    _fmt_pct(row.get("win_rate")),
                    _fmt(row.get("max_drawdown")),
                ]
                for row in backtest_pnl
            ],
        ),
        "</section>",
        '<section class="panel">',
        "<h2>Latest Markets</h2>",
        _market_table(latest_markets),
        "</section>",
        '<section class="panel">',
        "<h2>Top Edges</h2>",
        _market_table(top_edges),
        "</section>",
        '<section class="split">',
        '<div class="panel">',
        "<h2>Shadow PnL</h2>",
        _table(
            ["event type", "count", "open", "closed", "total PnL", "realized", "unrealized", "win rate"],
            [
                [
                    row.get("event_type"),
                    row.get("count"),
                    row.get("open_count"),
                    row.get("closed_count"),
                    _fmt(row.get("total_pnl")),
                    _fmt(row.get("realized_pnl")),
                    _fmt(row.get("unrealized_pnl")),
                    _fmt_pct(row.get("win_rate")),
                ]
                for row in shadow_rows
            ],
        ),
        "</div>",
        '<div class="panel">',
        "<h2>Recent Alerts</h2>",
        _alert_list(recent_alerts),
        "</div>",
        "</section>",
        '<section class="panel">',
        "<h2>Shadow Positions</h2>",
        _table(
            ["slug", "event type", "side", "status", "fill", "current", "PnL", "net edge"],
            [
                [
                    row.get("slug"),
                    row.get("event_type"),
                    row.get("side"),
                    row.get("status"),
                    _fmt(row.get("fill_price")),
                    _fmt(row.get("current_price")),
                    _fmt(row.get("pnl")),
                    _fmt(row.get("net_edge")),
                ]
                for row in positions
            ],
        ),
        "</section>",
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(html_parts)


def _dashboard_css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f6f8fb;
  --surface: #ffffff;
  --line: #d9e0ea;
  --ink: #17202a;
  --muted: #64748b;
  --blue: #1d4ed8;
  --green: #047857;
  --red: #b91c1c;
  --amber: #b45309;
  --teal: #0f766e;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Segoe UI", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
.shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 24px 0 40px; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.eyebrow { margin: 0 0 4px; color: var(--blue); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0; }
h1 { margin: 0; font-size: 28px; line-height: 1.15; letter-spacing: 0; }
h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }
.meta { margin: 3px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.metric-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.metric, .panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.metric { padding: 12px; min-height: 78px; }
.metric span { display: block; color: var(--muted); font-size: 12px; }
.metric strong { display: block; margin-top: 6px; font-size: 22px; line-height: 1.1; overflow-wrap: anywhere; }
.metric.positive strong, .positive { color: var(--green); }
.metric.negative strong, .negative { color: var(--red); }
.panel { padding: 14px; overflow: hidden; }
.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; margin-bottom: 14px; }
section.panel { margin-bottom: 14px; }
.badge { display: inline-flex; align-items: center; gap: 8px; min-height: 30px; padding: 5px 10px; border: 1px solid var(--line); border-radius: 999px; background: var(--surface); font-weight: 700; white-space: nowrap; }
.badge.ok { color: var(--green); }
.badge.warn { color: var(--red); }
.dot { width: 8px; height: 8px; border-radius: 999px; background: currentColor; }
.risk-line, .exposure-row { display: flex; justify-content: space-between; gap: 12px; margin: 8px 0; }
.risk-line span, .exposure-row span { color: var(--muted); }
.bar { margin-top: 12px; }
.bar-head { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
.track { height: 9px; margin-top: 5px; background: #eef2f7; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }
.fill { height: 100%; background: var(--teal); }
.fill.negative { background: var(--red); }
.fill.positive { background: var(--green); }
.table-wrap { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 720px; }
th, td { padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.slug { max-width: 300px; overflow-wrap: anywhere; }
.pill { display: inline-block; padding: 2px 7px; border-radius: 999px; border: 1px solid var(--line); font-size: 12px; white-space: nowrap; }
.pill.good { color: var(--green); border-color: #9fd4bd; background: #f0fdf7; }
.pill.bad { color: var(--red); border-color: #efb4b4; background: #fff5f5; }
.alert-list { margin: 0; padding: 0; list-style: none; }
.alert-list li { padding: 8px 0; border-bottom: 1px solid var(--line); }
.alert-list li:last-child { border-bottom: 0; }
.alert-title { display: block; font-weight: 700; overflow-wrap: anywhere; }
.alert-meta { color: var(--muted); font-size: 12px; }
.empty { color: var(--muted); margin: 0; }
@media (max-width: 880px) {
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .split { grid-template-columns: 1fr; }
  .page-head { flex-direction: column; }
}
""".strip()


def _metric(label: str, value: object, tone: str | None = None) -> str:
    tone_class = f" {tone}" if tone else ""
    return f'<div class="metric{tone_class}"><span>{_e(label)}</span><strong>{_e(value)}</strong></div>'


def _status_badge(label: str, active: bool, invert: bool = False) -> str:
    ok = not active if invert else active
    class_name = "ok" if ok else "warn"
    text = "off" if ok and invert else "on" if active else "off"
    return f'<div class="badge {class_name}"><span class="dot"></span>{_e(label)}: {_e(text)}</div>'


def _bar(label: str, value: object, max_abs: float, signed: bool = False) -> str:
    number = _float(value) or 0.0
    width = min(100.0, abs(number) / max_abs * 100.0) if max_abs > 0 else 0.0
    tone = ""
    if signed:
        tone = " negative" if number < 0 else " positive" if number > 0 else ""
    return (
        '<div class="bar">'
        f'<div class="bar-head"><span>{_e(label)}</span><strong>{_fmt_pct(value)}</strong></div>'
        f'<div class="track"><div class="fill{tone}" style="width: {width:.2f}%"></div></div>'
        "</div>"
    )


def _exposure_list(exposures: dict[str, object]) -> str:
    if not exposures:
        return '<p class="empty">No open exposure.</p>'
    total = sum(abs(_float(value) or 0.0) for value in exposures.values()) or 1.0
    rows: list[str] = []
    for event_type, exposure in sorted(exposures.items()):
        width = min(100.0, abs(_float(exposure) or 0.0) / total * 100.0)
        rows.append(
            '<div class="exposure-row">'
            f'<span>{_e(event_type)}</span><strong>{_fmt(exposure)}</strong>'
            "</div>"
            f'<div class="track"><div class="fill" style="width: {width:.2f}%"></div></div>'
        )
    return "\n".join(rows)


def _target_source_list(target_sources: dict[str, object]) -> str:
    if not target_sources:
        return '<p class="empty">No target sources.</p>'
    total = sum(int(value) for value in target_sources.values() if isinstance(value, int)) or 1
    rows = []
    for source, count in sorted(target_sources.items()):
        pct = int(count) / total if isinstance(count, int) else 0.0
        rows.append(
            '<div class="risk-line">'
            f'<span>{_e(source)}</span><strong>{_e(count)} ({_fmt_pct(pct)})</strong>'
            "</div>"
        )
    return "\n".join(rows)


def _market_table(rows: list[dict[str, object]]) -> str:
    return _table(
        ["slug", "event type", "platform", "preferred", "model", "price", "net edge", "evidence", "signal", "market"],
        [
            [
                row.get("slug"),
                row.get("event_type"),
                row.get("platform"),
                row.get("preferred_side"),
                row.get("model_side"),
                _fmt(row.get("preferred_price")),
                _fmt(row.get("net_edge")),
                _fmt(row.get("evidence_score")),
                _bool_pill(row.get("signal_ok")),
                _bool_pill(row.get("market_ok")),
            ]
            for row in rows
        ],
        raw_columns={8, 9},
        slug_columns={0},
    )


def _table(
    headers: list[str],
    rows: list[list[object]],
    raw_columns: set[int] | None = None,
    slug_columns: set[int] | None = None,
) -> str:
    raw_columns = raw_columns or set()
    slug_columns = slug_columns or set()
    if not rows:
        return '<p class="empty">No rows.</p>'
    header_html = "".join(f"<th>{_e(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            class_name = "num" if _looks_numeric(value) else ""
            if index in slug_columns:
                class_name = f"{class_name} slug".strip()
            class_attr = f' class="{class_name}"' if class_name else ""
            cell = str(value) if index in raw_columns else _e(value)
            cells.append(f"<td{class_attr}>{cell}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<div class="table-wrap"><table><thead><tr>' + header_html + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"


def _alert_list(alerts: list[dict[str, object]]) -> str:
    if not alerts:
        return '<p class="empty">No recent alerts.</p>'
    items = []
    for alert in alerts:
        reasons = ", ".join(str(reason) for reason in alert.get("alert_reasons", []))
        items.append(
            "<li>"
            f'<span class="alert-title">{_e(alert.get("label") or alert.get("slug"))}</span>'
            f'<span class="alert-meta">{_e(alert.get("timestamp_utc"))} &middot; {_e(reasons)}</span>'
            "</li>"
        )
    return '<ul class="alert-list">' + "".join(items) + "</ul>"


def _bool_pill(value: object) -> str:
    ok = value is True
    class_name = "good" if ok else "bad"
    text = "ok" if ok else "blocked"
    return f'<span class="pill {class_name}">{text}</span>'


def _tone_number(value: object) -> str | None:
    number = _float(value)
    if number is None or number == 0:
        return None
    return "positive" if number > 0 else "negative"


def _looks_numeric(value: object) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.rstrip("%"))
            return True
        except ValueError:
            return False
    return False


def _e(value: object) -> str:
    if value is None:
        return "none"
    return html.escape(str(value), quote=True)


def _latest_snapshots_by_slug(snapshots: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for row in snapshots:
        slug = str(row.get("slug") or "")
        if slug:
            latest[slug] = _snapshot_payload(row)
    return latest


def _edge_by_event_type(snapshots: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in snapshots:
        payload = _snapshot_payload(row)
        grouped[str(payload.get("event_type") or "unknown")].append(payload)

    summary: list[dict[str, object]] = []
    for event_type in sorted(grouped):
        rows = grouped[event_type]
        net_edges = [_float(row.get("net_edge")) for row in rows if _float(row.get("net_edge")) is not None]
        evidence_scores = [_float(row.get("evidence_score")) for row in rows if _float(row.get("evidence_score")) is not None]
        summary.append(
            {
                "event_type": event_type,
                "count": len(rows),
                "signal_ok_count": sum(1 for row in rows if row.get("signal_ok") is True),
                "market_ok_count": sum(1 for row in rows if row.get("market_ok") is True),
                "avg_net_edge": _avg(net_edges),
                "max_net_edge": max(net_edges) if net_edges else None,
                "avg_evidence_score": _avg(evidence_scores),
            }
        )
    return summary


def _latest_market_rows(latest: dict[str, dict[str, object]], limit: int) -> list[dict[str, object]]:
    rows = sorted(latest.values(), key=lambda row: str(row.get("slug") or ""))[: max(0, limit)]
    return [_market_row(row) for row in rows]


def _top_edges(latest: dict[str, dict[str, object]], limit: int) -> list[dict[str, object]]:
    rows = [row for row in latest.values() if _float(row.get("net_edge")) is not None]
    rows.sort(key=lambda row: _float(row.get("net_edge")) or float("-inf"), reverse=True)
    return [_market_row(row) for row in rows[: max(0, limit)]]


def _market_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "slug": row.get("slug"),
        "label": row.get("label"),
        "event_type": row.get("event_type") or "unknown",
        "platform": row.get("platform") or "unknown",
        "preferred_side": row.get("preferred_side"),
        "model_side": row.get("model_side"),
        "preferred_price": _float(row.get("preferred_price")),
        "net_edge": _float(row.get("net_edge")),
        "signal_ok": row.get("signal_ok"),
        "market_ok": row.get("market_ok"),
        "evidence_score": _float(row.get("evidence_score")),
        "p_model": _float(row.get("p_model")),
    }


def _alert_summary(alerts: list[dict[str, object]]) -> list[dict[str, object]]:
    counter: Counter[str] = Counter()
    for row in alerts:
        reasons = str(row.get("alert_reasons") or "")
        for reason in [part.strip() for part in reasons.split(",") if part.strip()]:
            counter[reason] += 1
    return [{"reason": reason, "count": count} for reason, count in counter.most_common()]


def _recent_alerts(alerts: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    recent = list(reversed(alerts))[: max(0, limit)]
    return [
        {
            "timestamp_utc": row.get("timestamp_utc"),
            "slug": row.get("slug"),
            "label": row.get("label"),
            "alert_reasons": str(row.get("alert_reasons") or "").split(",") if row.get("alert_reasons") else [],
        }
        for row in recent
    ]


def _snapshot_payload(row: dict[str, object]) -> dict[str, object]:
    raw = row.get("raw_json")
    if isinstance(raw, str) and raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    return row


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _max_value(values: Any) -> object:
    items = [value for value in values if value is not None]
    return max(items) if items else None


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[dict[str, object]]:
    return [row for row in value] if isinstance(value, list) else []


def _fmt(value: object) -> str:
    number = _float(value)
    return "none" if number is None else f"{number:.4f}"


def _fmt_pct(value: object) -> str:
    number = _float(value)
    return "none" if number is None else f"{number:.2%}"

