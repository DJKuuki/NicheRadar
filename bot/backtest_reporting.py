from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from bot.backtest_dataset import BacktestSample, samples_to_dicts
from bot.backtest_metrics import build_backtest_metrics


def build_backtest_report(samples: list[BacktestSample], db_path: str, min_samples: int = 20) -> dict[str, object]:
    metrics = build_backtest_metrics(samples, min_samples)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "min_samples": min_samples,
        "samples": samples_to_dicts(samples),
        **metrics,
    }


def format_backtest_report(report: dict[str, object]) -> list[str]:
    summary = _dict(report.get("summary"))
    lines = [
        "backtest_report",
        f"db_path={report.get('db_path')}",
        f"samples={summary.get('samples', 0)}",
        f"settled_samples={summary.get('settled_samples', 0)}",
        f"settled_sample_coverage={_pct(summary.get('settled_sample_coverage'))}",
        f"shadow_fills={summary.get('shadow_fills', 0)}",
        f"total_pnl={_fmt(summary.get('total_pnl'))}",
        f"brier={_fmt(summary.get('brier_score'))}",
        f"market_mid_brier={_fmt(summary.get('market_mid_brier_score'))}",
        f"reliability_status={summary.get('reliability_status')}",
    ]
    sources = _dict(report.get("target_source_counts"))
    if sources:
        lines.append("target_sources=" + ",".join(f"{key}:{value}" for key, value in sorted(sources.items())))
    if summary.get("reliability_status") == "insufficient":
        lines.append("warning=insufficient_settled_samples_no_parameter_changes")
    return lines


def write_backtest_json(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_backtest_markdown(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_markdown(report), encoding="utf-8")


def _markdown(report: dict[str, object]) -> str:
    summary = _dict(report.get("summary"))
    lines = [
        "# Backtest Report",
        "",
        "## Summary",
        "",
        f"- Samples: `{summary.get('samples', 0)}`",
        f"- Settled samples: `{summary.get('settled_samples', 0)}`",
        f"- Settled sample coverage: `{_pct(summary.get('settled_sample_coverage'))}`",
        f"- Mark-only samples: `{summary.get('mark_only_samples', 0)}`",
        f"- Shadow fills: `{summary.get('shadow_fills', 0)}`",
        f"- Total PnL: `{_fmt(summary.get('total_pnl'))}`",
        f"- Avg PnL: `{_fmt(summary.get('avg_pnl'))}`",
        f"- Win rate: `{_pct(summary.get('win_rate'))}`",
        f"- Brier score: `{_fmt(summary.get('brier_score'))}`",
        f"- Market mid Brier score: `{_fmt(summary.get('market_mid_brier_score'))}`",
        f"- Calibration error: `{_fmt(summary.get('calibration_error'))}`",
        f"- Reliability status: `{summary.get('reliability_status')}`",
        "",
        "## Data Quality",
        "",
        "| target source | count |",
        "| --- | ---: |",
    ]
    for source, count in sorted(_dict(report.get("target_source_counts")).items()):
        lines.append(f"| {source} | {count} |")
    lines.extend(
        [
            "",
            "## Calibration By Profile",
            "",
            "| profile | count | settled | avg p_model | observed YES | error | brier |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _list(report.get("calibration_by_profile")):
        lines.append(
            f"| {row.get('model_profile')} | {row.get('count')} | {row.get('settled')} | "
            f"{_fmt(row.get('avg_p_model'))} | {_fmt(row.get('observed_yes_rate'))} | "
            f"{_fmt(row.get('error'))} | {_fmt(row.get('brier'))} |"
        )
    lines.extend(
        [
            "",
            "## PnL By Profile",
            "",
            "| profile | fills | total pnl | avg pnl | win rate | max drawdown |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _list(report.get("pnl_by_profile")):
        lines.append(
            f"| {row.get('model_profile')} | {row.get('fills')} | {_fmt(row.get('total_pnl'))} | "
            f"{_fmt(row.get('avg_pnl'))} | {_pct(row.get('win_rate'))} | {_fmt(row.get('max_drawdown'))} |"
        )
    lines.extend(_bucket_section("Net Edge Buckets", "net edge bucket", _list(report.get("net_edge_buckets"))))
    lines.extend(_bucket_section("Evidence Buckets", "evidence bucket", _list(report.get("evidence_buckets"))))
    lines.extend(
        [
            "",
            "## Failure Cases",
            "",
            "| slug | profile | side | p_model | fill price | close price | pnl | reason |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in _list(report.get("failure_cases")):
        lines.append(
            f"| {row.get('slug')} | {row.get('profile')} | {row.get('side')} | {_fmt(row.get('p_model'))} | "
            f"{_fmt(row.get('fill_price'))} | {_fmt(row.get('close_price'))} | {_fmt(row.get('pnl'))} | {row.get('reason')} |"
        )
    lines.extend(["", "## Recommendations", ""])
    for recommendation in _str_list(report.get("recommendations")):
        lines.append(f"- {recommendation}")
    lines.append("")
    return "\n".join(lines)


def _bucket_section(title: str, label: str, rows: list[dict[str, object]]) -> list[str]:
    lines = [
        "",
        f"## {title}",
        "",
        f"| {label} | count | fills | avg pnl | win rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('bucket')} | {row.get('count')} | {row.get('fills')} | "
            f"{_fmt(row.get('avg_pnl'))} | {_pct(row.get('win_rate'))} |"
        )
    return lines


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[dict[str, object]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _str_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _fmt(value: object) -> str:
    return "none" if not isinstance(value, (int, float)) else f"{float(value):.4f}"


def _pct(value: object) -> str:
    return "none" if not isinstance(value, (int, float)) else f"{float(value):.2%}"
