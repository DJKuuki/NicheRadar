# Backtest Report

## Summary

- Samples: `6761`
- Settled samples: `0`
- Settled sample coverage: `0.00%`
- Mark-only samples: `0`
- Shadow fills: `0`
- Total PnL: `0.0000`
- Avg PnL: `none`
- Win rate: `none`
- Brier score: `0.1638`
- Market mid Brier score: `0.3005`
- Calibration error: `0.0000`
- Reliability status: `insufficient`

## Data Quality

| target source | count |
| --- | ---: |
| outcome_yes | 6761 |

## Calibration By Profile

| profile | count | settled | avg p_model | observed YES | error | brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| default_content | 5932 | 0 | 0.4066 | 0.4067 | 0.0001 | 0.1828 |
| ipo_event | 713 | 0 | 0.0446 | 0.0444 | -0.0002 | 0.0323 |
| music_release | 116 | 0 | 0.0105 | 0.0100 | -0.0005 | 0.0000 |

## PnL By Profile

| profile | fills | total pnl | avg pnl | win rate | max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| default_content | 0 | 0.0000 | none | none | none |
| ipo_event | 0 | 0.0000 | none | none | none |
| music_release | 0 | 0.0000 | none | none | none |

## Net Edge Buckets

| net edge bucket | count | fills | avg pnl | win rate |
| --- | ---: | ---: | ---: | ---: |
| -0.05-0.00 | 784 | 0 | none | none |
| >=0.05 | 5977 | 0 | none | none |

## Evidence Buckets

| evidence bucket | count | fills | avg pnl | win rate |
| --- | ---: | ---: | ---: | ---: |
| 0.00-0.02 | 6761 | 0 | none | none |

## Failure Cases

| slug | profile | side | p_model | fill price | close price | pnl | reason |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |

## Recommendations

- Insufficient settled samples; use this report for diagnostics only.
- Do not auto-tune model_profile parameters until each profile has at least 30 settled samples.
- Keep settlement_file, latest_mark, and snapshot_mid results separated.
