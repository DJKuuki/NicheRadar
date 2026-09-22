# NicheRadar — Tweet Markets Statistical Arbitrage Engine

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)]()
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

An industrial quantitative research and statistical arbitrage engine for **Polymarket Tweet Count Markets** (such as `@elonmusk` and other social media prediction brackets).

---

## 💡 The Edge & Market Inefficiencies

Unlike high-frequency crypto price markets (e.g. BTC 5-minute Up/Down) that suffer from severe adverse selection by HFT bots, **Tweet Count Markets** are predominantly traded by emotional retail participants:
1. **Misconception of Burst Probabilities**: Retail traders systematically overprice tail intervals and panic-buy low-probability high brackets when a minor burst of tweets occurs.
2. **Deterministic Resolution Source**: Polymarket resolves these markets exclusively against its dedicated, official tracker: [`https://xtracker.polymarket.com`](https://xtracker.polymarket.com). This repo interfaces directly with the official public REST endpoints to obtain exact cumulative counts without third-party scraping risks.
3. **Overdispersion Modeling**: NicheRadar fits a negative binomial count distribution using up to 90 completed calendar days. At least 20 daily observations are required. This is a forecasting assumption, not evidence of a profitable trading edge.

---

## 📐 Mathematical Formulation

### 1. Arrival Process Calibration
Given daily mean arrival rate $\mu_{daily}$ and empirical variance $\sigma^2_{daily}$ from historical post series:

$$\text{Overdispersion Index } D = \frac{\sigma^2_{daily}}{\mu_{daily}}$$

For remaining window duration $H_{rem}$ hours:

$$\mu_{rem} = \mu_{daily} \cdot \frac{H_{rem}}{24.0}, \quad \sigma^2_{rem} = \sigma^2_{daily} \cdot \frac{H_{rem}}{24.0}$$

When $\sigma^2_{rem} > \mu_{rem}$, we fit a Negative Binomial distribution with parameters $(r, p)$:

$$r = \frac{\mu_{rem}^2}{\sigma^2_{rem} - \mu_{rem}}, \quad p = \frac{\mu_{rem}}{\sigma^2_{rem}}$$

The Probability Mass Function for remaining posts $X_{rem}$ is:

$$P(X_{rem} = k) = \frac{\Gamma(k + r)}{k! \, \Gamma(r)} p^r (1 - p)^k$$

### 2. Bracket Probability Evaluation
For a closed bracket $[A, B]$ and current observed cumulative count $K$:

$$P(A \le K + X_{rem} \le B) = \sum_{x = \max(0, A - K)}^{B - K} P(X_{rem} = x)$$

For an open-ended high bracket $[A, +]$:

$$P(K + X_{rem} \ge A) = 1 - \sum_{x = 0}^{A - K - 1} P(X_{rem} = x)$$

### 3. Execution & Sizing
* **Maker Edge**: Model probability minus a quote computed from the actual CLOB bid, ask, and tick size.
* **Position Sizing**: Quarter-Kelly criterion ($f^* \times 0.25$) to conservatively manage drawdown risk.
* **Execution**: Defaults to simulated maker orders. Reported results exclude fees; paper fills do not reproduce exchange queue priority or guarantee live execution.

---

## 📂 Project Structure

```text
NicheRadar/
├── bot/
│   ├── __init__.py
│   ├── xtracker_client.py       # Official XTracker REST API client
│   ├── tweet_poisson_model.py   # Negative Binomial & Poisson pricing engine
│   ├── tweet_market_scanner.py  # Polymarket Gamma API scanner & bracket matcher
│   ├── shadow_storage.py        # SQLite persistence layer for virtual accounts & orders
│   ├── shadow_engine.py         # Realistic Maker/Taker order placement & fill simulator
│   ├── settlement_monitor.py    # Auto-settlement sync with Gamma API & XTracker
│   └── performance_analytics.py # Empirical PnL, Drawdown, and Brier Score calibration gate
├── scripts/
│   ├── run_tweet_arbitrage.py   # CLI entrypoint for live market scanning
│   └── run_shadow_trader.py     # CLI runner & real-time dashboard for Shadow Paper Trading
├── tests/
│   ├── test_tweet_strategy.py   # Probability & bracket pricing tests
│   └── test_shadow_trading.py   # Fills, settlements, and Brier score tests
├── requirements.txt
├── .gitignore
├── LICENSE
└── README.md
```

---

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run All Unit Tests
```bash
pytest tests/ -v
```

### 3. Run Live Market Arbitrage Scanner
```bash
python scripts/run_tweet_arbitrage.py --scan
```

### 4. Run Shadow Paper Trading Engine (One-Shot)
```bash
python scripts/run_shadow_trader.py --once
```

### 5. View Real-Time Dashboard & Brier Calibration Score
```bash
python scripts/run_shadow_trader.py --report
```

### 6. Run Continuous Shadow Daemon
```bash
python scripts/run_shadow_trader.py --daemon --scan-interval 60 --fill-interval 30
```

### Data integrity version (`v3-data-integrity`)

* Match XTracker by the exact event link and validate the ending timestamp. Gamma `startDate` is a listing date, not the counting-window start. Future windows use their full counting duration without adding time before the window opens.
* Read the live tracking `stats.total`; reject missing/invalid counts or tracker synchronization older than 15 minutes. Missing historical data also disables signals instead of falling back to generic model parameters. Today's incomplete daily count is excluded.
* Open-ended probabilities include the entire CDF below the threshold, including thresholds above 600.
* Use real two-sided CLOB books. Orders record tracking identity, window, current count, model parameters, quotes, and strategy version. Scans cancel unsupported open orders; TTL is also checked before fills.
* Full simulated fills require enough book depth or qualifying, timestamped sell volume after order placement. Strict tape fills require trading below the bid. Duplicate prints are not counted twice within a poll, and volume is not accumulated across polls. Partial fills and queue priority remain unmodeled; this intentionally conservative simulation may miss real fills.
* Taker fills use depth-weighted prices and refund unused collateral. Settlement requires official resolution and exact binary terminal prices.
* Reports separate legacy orders from new-version orders. The standard equity and drawdown figures are cost-based equity and realized-only drawdown. They are not a live liquidation valuation.

To request a fresh gross liquidation estimate using current bid depth:

```bash
python scripts/run_shadow_trader.py --report --mark-to-market
```

If any position lacks sufficient bid depth, the report displays pricing coverage and the priced subset's PnL; it does not invent a complete market equity value. Fees are excluded.

Existing settled records and filled positions are not rewritten by this upgrade. Restart an existing daemon to load the new code; on its next scan, unsupported resting orders are canceled. Statistical calibration, intraday/burst modeling, and out-of-sample profitability still require research: fixing data and execution errors does not establish an edge.

---

## 🛡️ License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
