from __future__ import annotations

from collections import Counter, defaultdict
import math

from bot.backtest_dataset import BacktestSample


def build_backtest_metrics(samples: list[BacktestSample], min_samples: int = 20) -> dict[str, object]:
    filled = [sample for sample in samples if sample.fill_eligible and sample.realized_pnl is not None]
    settled = [sample for sample in samples if sample.target_source == "settlement_file"]
    return {
        "summary": {
            "samples": len(samples),
            "settled_samples": len(settled),
            "settled_sample_coverage": _round(len(settled) / len(samples)) if samples else None,
            "mark_only_samples": len([sample for sample in samples if sample.target_source == "latest_mark"]),
            "shadow_fills": len(filled),
            "total_pnl": _round(sum(float(sample.realized_pnl) for sample in filled)),
            "avg_pnl": _avg([float(sample.realized_pnl) for sample in filled]),
            "win_rate": _win_rate(filled),
            "brier_score": brier_score(samples),
            "market_mid_brier_score": market_mid_brier_score(samples),
            "log_loss": log_loss(samples),
            "calibration_error": calibration_error(samples),
            "reliability_status": reliability_status(samples, min_samples),
        },
        "target_source_counts": dict(Counter(sample.target_source for sample in samples)),
        "calibration_bins": calibration_bins(samples, bins=5),
        "calibration_by_profile": group_calibration(samples, "model_profile"),
        "pnl_by_profile": group_pnl(samples, "model_profile"),
        "pnl_by_event_type": group_pnl(samples, "event_type"),
        "pnl_by_platform": group_pnl(samples, "platform"),
        "pnl_by_preferred_side": group_pnl(samples, "preferred_side"),
        "pnl_by_model_side": group_pnl(samples, "model_side"),
        "net_edge_buckets": bucket_performance(samples, "net_edge"),
        "evidence_buckets": bucket_performance(samples, "evidence_score"),
        "failure_cases": failure_cases(samples),
        "recommendations": recommendations(samples, min_samples),
    }


def brier_score(samples: list[BacktestSample]) -> float | None:
    values = [
        (sample.p_model - sample.target_yes_probability) ** 2
        for sample in samples
        if sample.p_model is not None and sample.target_yes_probability is not None
    ]
    return _avg(values)


def market_mid_brier_score(samples: list[BacktestSample]) -> float | None:
    values = [
        (sample.p_mid - sample.target_yes_probability) ** 2
        for sample in samples
        if sample.p_mid is not None and sample.target_yes_probability is not None
    ]
    return _avg(values)


def log_loss(samples: list[BacktestSample]) -> float | None:
    values: list[float] = []
    for sample in samples:
        if sample.p_model is None or sample.target_yes_probability is None:
            continue
        p = min(0.99, max(0.01, sample.p_model))
        y = min(0.99, max(0.01, sample.target_yes_probability))
        values.append(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)))
    return _avg(values)


def calibration_error(samples: list[BacktestSample]) -> float | None:
    pairs = [
        (sample.p_model, sample.target_yes_probability)
        for sample in samples
        if sample.p_model is not None and sample.target_yes_probability is not None
    ]
    if not pairs:
        return None
    return _round(sum(target - prediction for prediction, target in pairs) / len(pairs))


def calibration_bins(samples: list[BacktestSample], bins: int = 5) -> list[dict[str, object]]:
    grouped: list[list[BacktestSample]] = [[] for _ in range(bins)]
    for sample in samples:
        if sample.p_model is None or sample.target_yes_probability is None:
            continue
        index = min(bins - 1, int(sample.p_model * bins))
        grouped[index].append(sample)

    rows: list[dict[str, object]] = []
    for index, rows_in_bin in enumerate(grouped):
        low = index / bins
        high = (index + 1) / bins
        avg_model = _avg([sample.p_model for sample in rows_in_bin if sample.p_model is not None])
        observed = _avg([sample.target_yes_probability for sample in rows_in_bin if sample.target_yes_probability is not None])
        rows.append(
            {
                "bin": f"{low:.2f}-{high:.2f}",
                "count": len(rows_in_bin),
                "avg_p_model": avg_model,
                "observed_yes_rate": observed,
                "error": _round(observed - avg_model) if avg_model is not None and observed is not None else None,
            }
        )
    return rows


def group_calibration(samples: list[BacktestSample], key: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for value, group in _groups(samples, key).items():
        avg_model = _avg([sample.p_model for sample in group if sample.p_model is not None])
        observed = _avg([sample.target_yes_probability for sample in group if sample.target_yes_probability is not None])
        rows.append(
            {
                key: value,
                "count": len(group),
                "settled": len([sample for sample in group if sample.target_source == "settlement_file"]),
                "avg_p_model": avg_model,
                "observed_yes_rate": observed,
                "error": _round(observed - avg_model) if avg_model is not None and observed is not None else None,
                "brier": brier_score(group),
            }
        )
    return rows


def group_pnl(samples: list[BacktestSample], key: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for value, group in _groups(samples, key).items():
        filled = [sample for sample in group if sample.fill_eligible and sample.realized_pnl is not None]
        rows.append(
            {
                key: value,
                "fills": len(filled),
                "total_pnl": _round(sum(float(sample.realized_pnl) for sample in filled)),
                "avg_pnl": _avg([float(sample.realized_pnl) for sample in filled]),
                "win_rate": _win_rate(filled),
                "max_drawdown": max_drawdown([float(sample.realized_pnl) for sample in filled]),
                "avg_entry_price": _avg([sample.fill_price for sample in filled if sample.fill_price is not None]),
                "avg_exit_price": _avg([sample.target_price for sample in filled if sample.target_price is not None]),
            }
        )
    return rows


def bucket_performance(samples: list[BacktestSample], field: str) -> list[dict[str, object]]:
    bucketed: dict[str, list[BacktestSample]] = defaultdict(list)
    for sample in samples:
        value = getattr(sample, field)
        bucketed[_bucket(value)].append(sample)
    return [
        {
            "bucket": bucket,
            "count": len(group),
            "fills": len([sample for sample in group if sample.fill_eligible]),
            "avg_p_model": _avg([sample.p_model for sample in group if sample.p_model is not None]),
            "observed_yes_rate": _avg([sample.target_yes_probability for sample in group if sample.target_yes_probability is not None]),
            "avg_pnl": _avg([float(sample.realized_pnl) for sample in group if sample.realized_pnl is not None]),
            "win_rate": _win_rate([sample for sample in group if sample.realized_pnl is not None]),
        }
        for bucket, group in sorted(bucketed.items())
    ]


def max_drawdown(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return _round(drawdown)


def reliability_status(samples: list[BacktestSample], min_samples: int = 20) -> str:
    settled_count = len([sample for sample in samples if sample.target_source == "settlement_file"])
    if settled_count < max(30, min_samples):
        return "insufficient"
    if settled_count < 100:
        return "promising_candidate"
    return "reliable_enough_for_paper_trading_expansion"


def failure_cases(samples: list[BacktestSample], limit: int = 10) -> list[dict[str, object]]:
    failures = [
        sample
        for sample in samples
        if sample.fill_eligible and sample.realized_pnl is not None and sample.realized_pnl < 0
    ]
    failures.sort(key=lambda sample: float(sample.realized_pnl or 0))
    return [
        {
            "slug": sample.slug,
            "profile": sample.model_profile,
            "side": sample.model_side,
            "p_model": sample.p_model,
            "fill_price": sample.fill_price,
            "close_price": sample.target_price,
            "pnl": sample.realized_pnl,
            "reason": sample.target_source,
        }
        for sample in failures[:limit]
    ]


def recommendations(samples: list[BacktestSample], min_samples: int) -> list[str]:
    settled_count = len([sample for sample in samples if sample.target_source == "settlement_file"])
    if settled_count < max(30, min_samples):
        return [
            "Insufficient settled samples; use this report for diagnostics only.",
            "Do not auto-tune model_profile parameters until each profile has at least 30 settled samples.",
            "Keep settlement_file, latest_mark, and snapshot_mid results separated.",
        ]
    return [
        "Review profiles with positive settled PnL across multiple windows before expanding paper trading.",
        "Treat latest_mark rows as provisional and prefer settlement_file rows for parameter changes.",
    ]


def _groups(samples: list[BacktestSample], key: str) -> dict[str, list[BacktestSample]]:
    grouped: dict[str, list[BacktestSample]] = defaultdict(list)
    for sample in samples:
        grouped[str(getattr(sample, key) or "unknown")].append(sample)
    return dict(sorted(grouped.items()))


def _bucket(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    number = float(value)
    if number < -0.05:
        return "<-0.05"
    if number < 0.0:
        return "-0.05-0.00"
    if number < 0.02:
        return "0.00-0.02"
    if number < 0.05:
        return "0.02-0.05"
    return ">=0.05"


def _win_rate(samples: list[BacktestSample]) -> float | None:
    pnls = [float(sample.realized_pnl) for sample in samples if sample.realized_pnl is not None]
    if not pnls:
        return None
    return _round(len([pnl for pnl in pnls if pnl > 0]) / len(pnls))


def _avg(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return _round(sum(clean) / len(clean)) if clean else None


def _round(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None else None
