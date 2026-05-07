# NicheRadar

A shadow trading and monitoring bot for Polymarket niche event markets.

**Note**: This project does NOT support real trading/execution. It is designed for public market data retrieval, external evidence scoring, signal generation, risk filtering, watchlist monitoring, alerts, SQLite persistence, and shadow fill simulation.

---

## Objectives

NicheRadar does not seek high-frequency arbitrage, BTC/ETH price action, whale tracking, or settlement sniping. The current strategy focus is:

- **Niche Event Markets**: Focus on markets with clear rules and verifiable external evidence.
- **Category Focus**: Content releases (albums, songs), AI/Corporate milestones, Product launches, and IPO events.
- **Evidence-Based**: Use RSS/Google News feeds to score the likelihood of an event.
- **Shadow Simulation**: Record "shadow fills" only when the model direction, risk parameters, target price bands, and orderbook prices align.
- **Auditability**: All signals and simulated trades are persisted for audit and replay.

---

## Current Capabilities

- **Market Data**: Fetches metadata from Polymarket Gamma API and orderbooks from CLOB API.
- **Token Mapping**: Explicitly maps YES/NO tokens based on `outcomes` and `clobTokenIds`.
- **Classification**: Parses market titles and rules into types like `content_release`, `announcement`, `ipo_event`, and `social_activity`.
- **Evidence Collection**: Scrapes external evidence from RSS/Atom/Google News RSS and assigns scores.
- **Signal Engine**: Calculates `p_model`, direction, edge, net edge, and maximum entry price.
- **Model Profiles**: Separate profiles for Music, Product, and IPO events with specific base probabilities and evidence weights.
- **Risk Management**: Filters by volume, expiry, spread, confidence, and edge.
- **Watchlist Monitoring**: Iterative polling of specific markets with persistence to JSONL and SQLite.
- **Alerting**: Logs alerts for price movements into target bands, signal availability, or significant evidence score jumps.
- **Shadow Fills**: Simulates entries without real capital; updates floating PnL (mark-to-market) via `shadow_marks`.
- **PnL Replay**: Replays shadow performance with support for latest marks, manual settlements, or final market outcomes.
- **Dashboard**: Generates a local HTML dashboard and Markdown reports summarizing edge, alerts, shadow PnL, and backtest results.
- **Portfolio Risk**: Enforces exposure limits per market/event type and circuit breakers on simulated losses.
- **Calibration**: Generates `model_profile` calibration reports suggesting `base_logit` or weight adjustments based on shadow samples.
- **Backtesting**: Offline engine to evaluate strategy rules against historical SQLite snapshots.
- **Reliability**: Request rate-limiting, SQLite-based HTTP caching, and robust error handling.

---

## Historical Backtesting & Model Optimization

NicheRadar includes a dedicated pipeline to fetch historical Polymarket data to calibrate models without waiting for live shadow monitoring.

### Workflow

1.  **Phase 1: Data Fetching**: Retrieve resolved markets and their CLOB price history.
    ```powershell
    python -m bot.main --fetch-history --history-start-date 2023-01-01 --history-min-volume 1000
    ```
2.  **Phase 2: Snapshot Building**: Reconstruct historical market states and re-run the signal engine at each historical price point.
    ```powershell
    python -m bot.main --build-history-snapshots
    ```
3.  **Phase 3: Backtest & Calibrate**: Run analysis on the generated historical snapshots.
    ```powershell
    python -m bot.main --history-backtest
    python -m bot.main --history-calibrate
    ```

**Note**: Historical evidence (RSS feeds) cannot be perfectly reconstructed. By default, historical snapshots use a "zero-evidence" fallback to calibrate the base model's bias and time-decay parameters.

---

## Directory Structure

- `bot/api.py`: Polymarket public API client (Gamma/CLOB).
- `bot/market_scanner.py`: Market normalization and orderbook retrieval.
- `bot/market_parser.py`: Question/Rules parsing and classification.
- `bot/evidence_collector.py`: RSS scraping and scoring.
- `bot/signal_engine.py`: Probability model and trade signal generation.
- `bot/risk_engine.py`: Single-market risk and signal filtering.
- `bot/portfolio_risk.py`: Multi-market exposure and circuit breakers.
- `bot/shadow.py`: Shadow fill simulation logic.
- `bot/historical_fetcher.py`: Historical data crawler.
- `bot/historical_snapshot_builder.py`: Historical state reconstruction.
- `bot/backtest_engine.py`: Offline replay and strategy evaluation.
- `bot/calibration.py`: Model bias and weight optimization.
- `bot/storage.py`: SQLite persistence layer.
- `data/watchlist.json`: markets to monitor.
- `logs/`: logs, snapshots, and SQLite databases.

---

## Usage

### Live Monitoring
```powershell
# Continuous watchlist polling (every 5 mins, 12 iterations)
python -m bot.main --watchlist data/watchlist.json --iterations 12 --poll-seconds 300 --db-file logs/watchlist.sqlite
```

### Analysis & Reporting
```powershell
# Generate HTML Dashboard
python -m bot.main --dashboard-report --report-html logs/dashboard.html

# Run Model Calibration
python -m bot.main --calibration-report --db-file logs/watchlist.sqlite

# Run Offline Backtest
python -m bot.main --backtest --db-file logs/watchlist.sqlite
```

---

## Shadow Fill Conditions

A shadow fill is recorded only if:
1.  `market_ok == true` (volume, spread, expiry within limits).
2.  `signal_ok == true` (edge and confidence thresholds met).
3.  `model_side == preferred_side`.
4.  Orderbook `ask <= max_entry_price`.
5.  Portfolio risk limits are not exceeded.

---

## Current Limitations

- **No Execution**: No authentication for CLOB, no order placement/cancellation logic.
- **Heuristic Model**: Logit-based parameters are currently heuristic and require historical calibration.
- **Evidence Noise**: RSS feeds can be noisy, especially for "rumors" vs "official releases".
- **Sample Size**: Reliability of backtests and calibration depends on accumulating enough "settled" samples (target >30 per profile).

---

## Next Steps

1.  **Accumulate Samples**: Manually maintain `data/shadow_settlements.json` for live shadow fills to increase "settled" sample size.
2.  **Baseline Comparison**: Implement comparisons against "Market Mid" and "Random Entry" baselines in backtest reports.
3.  **Refine Evidence**: Layer evidence sources (Official > Major Media > Rumors) to reduce false positives in Product Release markets.
4.  **Real Execution (Future)**: Only after the model is proven stable across 100+ settled samples, implement CLOB L2 Auth and order lifecycle management.
