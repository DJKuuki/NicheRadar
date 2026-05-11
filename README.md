# NicheRadar

**Shadow trading and signal research bot for Polymarket niche event markets.**

NicheRadar does **not** execute real orders. It fetches public market data, scores external evidence, generates signals, simulates "shadow fills", and produces auditable reports — all without touching real capital. The goal is to validate an evidence-based pricing model against live market prices before considering any real execution.

---

## What It Does

| Layer | Description |
|---|---|
| **Market data** | Fetches metadata from the Polymarket Gamma API and live orderbooks from the CLOB API |
| **Classification** | Parses market titles and rules into structured event types (`content_release`, `announcement`, `ipo_event`, `social_activity`, …) |
| **Evidence scoring** | Scrapes RSS/Atom/Google News feeds and converts matches into a 0–1 evidence score |
| **Signal engine** | Combines a logit-based probability model with evidence scores to produce `p_model`, direction, edge, and `max_entry_price` |
| **Risk filters** | Single-market filters (volume, spread, expiry, edge, confidence) and portfolio-level exposure limits |
| **Shadow fills** | Records simulated entries when all conditions align; tracks floating PnL via `shadow_marks` |
| **AI debate** | Optional multi-round Bull / Bear / Judge debate orchestrated over LLM file-exchange to generate an independent signal |
| **Backtesting** | Offline replay engine against historical SQLite snapshots |
| **Calibration** | Generates suggested `base_logit` / weight adjustments from accumulated shadow samples |
| **Discovery** | Automated niche-market discovery that appends qualifying markets to the watchlist |
| **Dashboard** | Standalone HTML frontend for edge, alerts, shadow PnL, and backtest results |

---

## Strategy Focus

NicheRadar deliberately avoids BTC/ETH price action, high-frequency arbitrage, whale tracking, and settlement sniping. It targets:

- **Content releases** — albums, songs, films, game launches
- **Corporate & AI announcements** — product reveals, earnings, regulatory milestones
- **IPO events** — listing dates, first-day price targets
- **Social activity** — follower counts, viral metrics with clear resolution rules

These markets tend to have moderate volume, verifiable external evidence, and relatively low adverse-selection risk.

---

## Project Structure

```
NicheRadar/
├── bot/
│   ├── common.py                  # Shared coercion, formatting, and logging helpers
│   ├── llm_file_exchange.py       # Unified LLM file-exchange (inbox/outbox, prompt/result)
│   ├── api.py                     # Polymarket Gamma + CLOB API client
│   ├── cli.py                     # argparse CLI definition (30+ flags)
│   ├── config.py                  # BotConfig dataclass with all tunable parameters
│   ├── models.py                  # Core dataclasses (Market, Signal, Evidence, …)
│   ├── market_scanner.py          # Market normalisation and orderbook retrieval
│   ├── market_parser.py           # Title/rules parsing → event_type + platform
│   ├── market_discovery.py        # Auto-discovery and watchlist appending
│   ├── evidence_collector.py      # RSS scraping and evidence scoring
│   ├── evidence_source_finder.py  # LLM-assisted evidence source discovery
│   ├── signal_engine.py           # Probability model and signal generation
│   ├── risk_engine.py             # Per-market risk and signal filters
│   ├── portfolio_risk.py          # Multi-market exposure and circuit breakers
│   ├── shadow.py                  # Shadow fill simulation and mark-to-market
│   ├── shadow_replay.py           # PnL replay with settlement support
│   ├── watchlist.py               # Watchlist polling loop
│   ├── storage.py                 # SQLite persistence (watchlist, marks, alerts)
│   ├── http_cache.py              # SQLite-backed HTTP response cache
│   ├── debate_models.py           # Data structures for the AI debate engine
│   ├── debate_orchestrator.py     # Bull/Bear/Judge/Research Manager orchestration
│   ├── debate_prompts.py          # Prompt templates for each debate role
│   ├── backtest_engine.py         # Offline backtest replay
│   ├── backtest_dataset.py        # Backtest dataset loading and filtering
│   ├── backtest_reporting.py      # Backtest report generation
│   ├── backtest_metrics.py        # Metric calculations (win rate, Sharpe, …)
│   ├── calibration.py             # Model-profile calibration reports
│   ├── reporting.py               # Dashboard Markdown/JSON report generation
│   ├── settlement_validation.py   # Settlement file coverage and conflict checks
│   ├── historical_fetcher.py      # Historical CLOB data crawler
│   ├── historical_storage.py      # SQLite store for historical markets + prices
│   ├── historical_snapshot_builder.py  # Reconstruct market states from price history
│   ├── execution_engine.py        # (Stub) Future real-order execution layer
│   └── main.py                    # Entry point — wires all modes together
│
├── frontend/
│   ├── index.html                 # Single-page dashboard (no build step required)
│   ├── app.js                     # Chart.js charts, tab switching, table rendering
│   ├── styles.css                 # Dark-theme CSS with responsive grid
│   └── README.md                  # How to serve the dashboard locally
│
├── data/
│   ├── watchlist.json             # Markets to monitor (slug, label, entry bands, …)
│   ├── evidence_sources.json      # RSS/Atom feed registry per market slug
│   ├── sample_markets.json        # Offline sample data for dev/testing
│   └── shadow_settlements.example.json  # Example settlement file format
│
├── tests/                         # pytest suite (93 tests)
├── logs/                          # Runtime outputs — gitignored
│   ├── watchlist.sqlite
│   ├── historical.sqlite
│   ├── http_cache.sqlite
│   ├── shadow_fills.jsonl
│   ├── watchlist_snapshots.jsonl
│   ├── watchlist_alerts.jsonl
│   └── dashboard_report.json      # Consumed by the frontend dashboard
│
├── pyproject.toml
└── .gitignore
```

---

## Installation

```powershell
# Python 3.11+ required
python -m pip install -e .[dev]
```

No external runtime dependencies beyond the standard library. `pytest>=8` is installed as a dev dependency.

---

## Running Tests

```powershell
python -m pytest
# 93 passed
```

Log level is controlled by the `NICHERADAR_LOG_LEVEL` environment variable (default: `INFO`).

---

## Usage

### 1 — Offline sample run

```powershell
python -m bot.main --sample-data data/sample_markets.json
```

### 2 — Live market scan

```powershell
python -m bot.main --live --limit 30
```

### 3 — Watchlist monitoring (continuous polling)

```powershell
python -m bot.main \
  --watchlist data/watchlist.json \
  --iterations 12 \
  --poll-seconds 300 \
  --db-file logs/watchlist.sqlite
```

### 4 — Auto-discovery (append qualifying markets to watchlist)

```powershell
python -m bot.main \
  --watchlist data/watchlist.json \
  --discover \
  --discover-limit 100 \
  --discover-min-volume 5000
```

### 5 — Shadow replay with manual settlements

```powershell
python -m bot.main \
  --shadow-replay \
  --settlement-file data/shadow_settlements.json \
  --replay-json logs/shadow_replay.json \
  --db-file logs/watchlist.sqlite
```

### 6 — Dashboard report

```powershell
# Generate the JSON consumed by the HTML frontend
python -m bot.main \
  --dashboard-report \
  --report-json logs/dashboard_report.json \
  --db-file logs/watchlist.sqlite

# Serve the frontend
cd frontend && python -m http.server 8000
# Open http://localhost:8000
```

### 7 — Calibration report

```powershell
python -m bot.main \
  --calibration-report \
  --calibration-json logs/calibration_report.json \
  --db-file logs/watchlist.sqlite
```

### 8 — Offline backtest

```powershell
python -m bot.main \
  --backtest \
  --backtest-json logs/backtest_report.json \
  --db-file logs/watchlist.sqlite
```

---

## AI Debate Mode

When `--debate-mode` is enabled, instead of using the deterministic signal engine, the bot runs a structured multi-round debate for each market:

1. **Researcher round** — Bull and Bear arguments are generated
2. **Judge critique** (optional, up to `--debate-judge-rounds` iterations) — a Judge asks targeted follow-up questions to both sides
3. **Research Manager** — synthesises all arguments into a final `p_yes` estimate and direction

The bot communicates with the LLM via a file-exchange protocol: it writes prompts to `logs/ai_inbox/<session_id>/` and waits for matching `*_result.txt` files in `logs/ai_outbox/<session_id>/`.

```powershell
# Batch mode — writes all prompts first, then waits for results
python -m bot.main \
  --watchlist data/watchlist.json \
  --debate-mode \
  --debate-mode-type batch

# Interactive mode — waits for each result before writing the next prompt
python -m bot.main \
  --watchlist data/watchlist.json \
  --debate-mode \
  --debate-mode-type sequential
```

---

## LLM-Assisted Evidence Source Discovery

When the static evidence registry has no feed for a market (or the existing feed seems mismatched), the bot can ask an LLM to suggest better sources:

```powershell
python -m bot.main \
  --watchlist data/watchlist.json \
  --llm-source-finder \
  --llm-source-mode batch
```

Prompts are written to `logs/source_inbox/`. Paste the LLM JSON response into the matching `*_result.json` file in `logs/source_outbox/` and re-run. Suggested sources are evaluated for that run only and are **not** written back to `data/evidence_sources.json` automatically.

---

## Historical Backtesting Pipeline

```powershell
# Step 1: Fetch resolved markets and price history
python -m bot.main \
  --fetch-history \
  --history-start-date 2023-01-01 \
  --history-min-volume 1000

# Step 2: Reconstruct market states at each price bar
python -m bot.main --build-history-snapshots

# Step 3: Run backtest and calibration on historical snapshots
python -m bot.main --history-backtest
python -m bot.main --history-calibrate
```

Historical snapshots use "zero-evidence" (no RSS data) so that calibration targets the base model's bias and time-decay parameters in isolation.

---

## Shadow Fill Conditions

A shadow fill is recorded only when **all** of the following are true:

| # | Condition |
|---|---|
| 1 | `market_ok == True` (volume, spread, and expiry within configured limits) |
| 2 | `signal_ok == True` (edge and confidence above thresholds) |
| 3 | `model_side == preferred_side` from the watchlist entry |
| 4 | Orderbook `ask ≤ max_entry_price` |
| 5 | Portfolio exposure limits are not exceeded |

---

## Frontend Dashboard

The `frontend/` directory contains a self-contained single-page dashboard with no build step.

**Tabs:** Overview · Markets · Shadow Positions · Backtest · Portfolio Risk · Alerts · Raw JSON

**Charts (Chart.js):** Edge distribution · Signal coverage by event type · Calibration curve · Exposure by market

```powershell
cd frontend
python -m http.server 8000
# Visit http://localhost:8000
```

The dashboard reads `../logs/dashboard_report.json` by default. Use the path dropdown in the UI to load a different report file.

---

## Configuration

All tunable parameters live in `bot/config.py` (`BotConfig` dataclass) and can be overridden via CLI flags. Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `min_volume` | 5 000 | Minimum market volume in USD |
| `max_spread` | 0.12 | Maximum YES ask − YES bid spread |
| `min_days_to_expiry` | 3 | Minimum days before market close |
| `max_days_to_expiry` | 90 | Maximum days before market close |
| `min_edge` | 0.05 | Minimum `p_model − p_mid` to qualify |
| `min_confidence` | 0.55 | Minimum signal confidence |
| `max_exposure_per_market` | 100 | Max simulated USD per open position |
| `max_total_exposure` | 500 | Portfolio-wide simulated USD cap |

---

## Limitations

- **No real execution.** There is no CLOB authentication, no order placement, and no position management.
- **Heuristic model.** Logit parameters are manually seeded. Historical calibration will improve them over time.
- **Evidence noise.** RSS feeds can surface rumours well before official releases, inflating evidence scores prematurely.
- **Sample size.** Calibration and backtest reliability improves significantly above 30 settled samples per model profile.

---

## Roadmap

1. **Accumulate samples** — maintain `data/shadow_settlements.json` for live shadow fills
2. **Baseline comparison** — add "market mid" and "random entry" benchmarks to backtest reports
3. **Evidence layering** — weight Official > Major Media > Rumour to reduce false positives on product-release markets
4. **Real execution** (long-term) — implement CLOB L2 auth and order lifecycle management after 100+ settled samples confirm model stability
