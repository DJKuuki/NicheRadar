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
3. **Overdispersion Exploitation**: Social post arrival processes exhibit significant burstiness ($\sigma^2 / \mu \approx 9.3\times$). Simple Poisson distribution models underestimate tail variance. NicheRadar parameterizes continuous **Negative Binomial distributions** calibrated against 300+ days of historical post data.

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
* **Maker Edge**: $\text{Edge}_{maker} = P_{fair} - (\text{Market Bid} + 0.01)$
* **Position Sizing**: Quarter-Kelly criterion ($f^* \times 0.25$) to conservatively manage drawdown risk.
* **Fee Protection**: Defaulting to passive Maker quotes to completely bypass the 2% Taker fee penalty.

---

## 📂 Project Structure

```text
NicheRadar/
├── bot/
│   ├── __init__.py
│   ├── xtracker_client.py       # Official XTracker REST API client
│   ├── tweet_poisson_model.py   # Negative Binomial & Poisson pricing engine
│   └── tweet_market_scanner.py  # Polymarket Gamma API scanner & bracket matcher
├── scripts/
│   └── run_tweet_arbitrage.py   # CLI entrypoint for live market scanning
├── tests/
│   └── test_tweet_strategy.py   # Comprehensive unit & regression tests
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

### 2. Run Unit Tests
```bash
pytest tests/test_tweet_strategy.py -v
```

### 3. Run Live Market Arbitrage Scanner
```bash
python scripts/run_tweet_arbitrage.py --scan
```

### 4. Export Scanned Signals as JSON
```bash
python scripts/run_tweet_arbitrage.py --scan --json-out signals.json
```

---

## 🛡️ License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
