# NicheRadar Frontend Dashboard

An interactive, client-side dashboard for the NicheRadar Polymarket shadow-trading bot.
It reads the JSON report that `python -m bot.main --dashboard-report` writes to
`logs/dashboard_report.json` and renders it with charts, filters, and tabs.

## Quick start

From the project root:

```powershell
# 1. Make sure a report exists
python -m bot.main --dashboard-report --report-html logs/dashboard.html

# 2. Serve the project so the page can fetch ../logs/dashboard_report.json
python -m http.server 8000

# 3. Open the dashboard
#    http://localhost:8000/frontend/
```

Opening `frontend/index.html` directly via `file://` will fail because browsers
block `fetch()` of local files. A simple static server is required.

## Features

- **Overview** — KPI cards, edge-by-event-type chart, top-edges table
- **Markets** — Live filter by event type, status, free-text search
- **Shadow Positions** — Stacked realized/unrealized PnL bars and a per-fill table
- **Backtest** — Calibration (avg p_model vs observed YES rate), profile PnL, Brier/log-loss KPIs
- **Portfolio Risk** — Exposure breakdowns (event type / market) and circuit-breaker status
- **Alerts** — Recent watchlist alerts with reason badges
- **Raw JSON** — The full report for debugging
- **Auto-refresh** — Toggle to re-fetch the JSON every 30 seconds

## Files

| File           | Purpose                                                |
| -------------- | ------------------------------------------------------ |
| `index.html`   | Page layout, tabs, table headers                       |
| `styles.css`   | Dark theme, KPI/card/table styles                      |
| `app.js`       | Loads JSON, renders tables, draws Chart.js charts      |

Chart.js is loaded from a CDN. No build step, no dependencies to install.

## Data source

By default the dashboard reads `../logs/dashboard_report.json` (relative to
`frontend/`). The dropdown in the header can switch to `./dashboard_report.json`
if you want to drop a copy of the report alongside the page (useful for sharing
a single folder).
