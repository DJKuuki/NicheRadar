# Backtest Report

## Summary

- Samples: `72`
- Settled samples: `0`
- Settled sample coverage: `0.00%`
- Mark-only samples: `2`
- Shadow fills: `20`
- Total PnL: `-0.1450`
- Avg PnL: `-0.0072`
- Win rate: `0.00%`
- Brier score: `0.0274`
- Market mid Brier score: `0.0000`
- Calibration error: `0.0269`
- Reliability status: `insufficient`

## Data Quality

| target source | count |
| --- | ---: |
| latest_mark | 2 |
| snapshot_mid | 70 |

## Calibration By Profile

| profile | count | settled | avg p_model | observed YES | error | brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| default_content | 12 | 0 | 0.5455 | 0.6050 | 0.0595 | 0.0035 |
| ipo_event | 11 | 0 | 0.4767 | 0.6900 | 0.2133 | 0.0462 |
| music_release | 26 | 0 | 0.4888 | 0.5650 | 0.0762 | 0.0063 |
| product_release | 23 | 0 | 0.1985 | 0.0636 | -0.1349 | 0.0546 |

## PnL By Profile

| profile | fills | total pnl | avg pnl | win rate | max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| default_content | 0 | 0.0000 | none | none | none |
| ipo_event | 9 | -0.0900 | -0.0100 | 0.00% | -0.0900 |
| music_release | 11 | -0.0550 | -0.0050 | 0.00% | -0.0550 |
| product_release | 0 | 0.0000 | none | none | none |

## Net Edge Buckets

| net edge bucket | count | fills | avg pnl | win rate |
| --- | ---: | ---: | ---: | ---: |
| -0.05-0.00 | 1 | 0 | none | none |
| 0.00-0.02 | 31 | 8 | -0.0050 | 0.00% |
| 0.02-0.05 | 8 | 0 | none | none |
| >=0.05 | 32 | 12 | -0.0087 | 0.00% |

## Evidence Buckets

| evidence bucket | count | fills | avg pnl | win rate |
| --- | ---: | ---: | ---: | ---: |
| >=0.05 | 72 | 20 | -0.0072 | 0.00% |

## Failure Cases

| slug | profile | side | p_model | fill price | close price | pnl | reason |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | latest_mark |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4626 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4843 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4843 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| will-openai-not-ipo-by-december-31-2026 | ipo_event | BUY_NO | 0.4843 | 0.3200 | 0.3100 | -0.0100 | snapshot_mid |
| new-playboi-carti-album-before-gta-vi-421 | music_release | BUY_NO | 0.4775 | 0.4600 | 0.4550 | -0.0050 | snapshot_mid |

## Recommendations

- Insufficient settled samples; use this report for diagnostics only.
- Do not auto-tune model_profile parameters until each profile has at least 30 settled samples.
- Keep settlement_file, latest_mark, and snapshot_mid results separated.
