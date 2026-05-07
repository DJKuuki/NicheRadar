# PolyMarket Shadow Report

- Generated UTC: `2026-04-29T08:26:30.034044+00:00`
- Database: `logs/watchlist.sqlite`
- Latest snapshot UTC: `2026-04-27T05:41:32.576852+00:00`

## Counts

| snapshots | markets | alerts | shadow fills | shadow positions |
| ---: | ---: | ---: | ---: | ---: |
| 72 | 6 | 5 | 2 | 2 |

## Edge By Event Type

| event type | count | signal ok | avg net edge | max net edge | avg evidence |
| --- | ---: | ---: | ---: | ---: | ---: |
| content_release | 61 | 48 | 0.0614 | 0.5544 | 0.4063 |
| ipo_event | 11 | 11 | 0.1633 | 0.1774 | 0.7800 |

## Alert Summary

| reason | count |
| --- | ---: |
| signal_turned_ok | 4 |
| entered_target_band | 1 |

## Shadow PnL

| event type | count | open | closed | total PnL | realized | unrealized | win rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| content_release | 1 | 1 | 0 | -0.0050 | 0.0000 | -0.0050 | 0.00% |
| ipo_event | 1 | 1 | 0 | -0.0100 | 0.0000 | -0.0100 | 0.00% |

## Backtest Summary

| samples | settled | settled coverage | mark-only | fills | total PnL | brier | status |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 72 | 0 | 0.00% | 2 | 20 | -0.1450 | 0.0274 | insufficient |

### Backtest Target Sources

| target source | count |
| --- | ---: |
| latest_mark | 2 |
| snapshot_mid | 70 |

## Backtest Calibration By Profile

| profile | count | settled | avg p_model | observed YES | error | brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| default_content | 12 | 0 | 0.5455 | 0.6050 | 0.0595 | 0.0035 |
| ipo_event | 11 | 0 | 0.4767 | 0.6900 | 0.2133 | 0.0462 |
| music_release | 26 | 0 | 0.4888 | 0.5650 | 0.0762 | 0.0063 |
| product_release | 23 | 0 | 0.1985 | 0.0636 | -0.1349 | 0.0546 |

## Backtest PnL By Profile

| profile | fills | total PnL | avg PnL | win rate | max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| default_content | 0 | 0.0000 | none | none | none |
| ipo_event | 9 | -0.0900 | -0.0100 | 0.00% | -0.0900 |
| music_release | 11 | -0.0550 | -0.0050 | 0.00% | -0.0550 |
| product_release | 0 | 0.0000 | none | none | none |

## Portfolio Risk

| bankroll | open positions | total exposure | exposure % | unrealized PnL | unrealized % | circuit breaker |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1000.0000 | 2 | 40.0000 | 4.00% | -0.8424 | -0.08% | False |

### Exposure By Event Type

| event type | exposure |
| --- | ---: |
| content_release | 20.0000 |
| ipo_event | 20.0000 |

## Top Edges

| slug | event type | model side | net edge | signal ok |
| --- | --- | --- | ---: | --- |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.1557 | True |
| new-playboi-carti-album-before-gta-vi-421 | content_release | BUY_NO | 0.0645 | True |
| new-rhianna-album-before-gta-vi-926 | content_release | BUY_NO | 0.0547 | True |
| will-apple-release-a-macbook-with-cellular-connectivity-by-june-30 | content_release | BUY_YES | 0.0486 | True |
| will-tesla-release-optimus-by-june-30-2026 | content_release | BUY_YES | 0.0358 | True |
| will-gpt-6-be-released | content_release | BUY_NO | 0.0095 | False |
