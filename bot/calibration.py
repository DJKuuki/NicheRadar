from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from bot.shadow_replay import Settlement
from bot.signal_engine import MODEL_PROFILES


@dataclass(frozen=True)
class CalibrationSample:
    fill_id: int
    slug: str
    side: str
    profile: str
    event_type: str
    platform: str
    action: str
    p_model: float
    target_yes_probability: float
    target_source: str
    evidence_score: float | None
    preheat_score: float | None
    cadence_score: float | None
    partner_score: float | None
    days_to_expiry: float | None
    spread: float | None


def build_calibration_report(
    db_path: str,
    settlements: list[Settlement] | None = None,
    min_samples: int = 5,
) -> dict[str, object]:
    samples = load_calibration_samples(db_path, settlements or [])
    groups = _group_by_profile(samples)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "min_samples": min_samples,
        "sample_count": len(samples),
        "target_source_counts": dict(Counter(sample.target_source for sample in samples)),
        "profiles": [_calibrate_profile(profile, rows, min_samples) for profile, rows in groups.items()],
    }


def load_calibration_samples(db_path: str, settlements: list[Settlement]) -> list[CalibrationSample]:
    settlement_index = _settlement_index(settlements)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        fills = connection.execute(
            """
            SELECT id, timestamp_utc, slug, side, raw_json
            FROM shadow_fills
            ORDER BY timestamp_utc, id
            """
        ).fetchall()
        samples = [
            sample
            for fill in fills
            if (sample := _sample_from_fill(connection, fill, settlement_index)) is not None
        ]
    return samples


def format_calibration_report(report: dict[str, object]) -> list[str]:
    lines = [
        "calibration_report",
        f"db_path={report.get('db_path')}",
        f"sample_count={report.get('sample_count', 0)}",
        f"min_samples={report.get('min_samples', 0)}",
    ]
    source_counts = _dict(report.get("target_source_counts"))
    if source_counts:
        source_text = ",".join(f"{key}:{value}" for key, value in sorted(source_counts.items()))
        lines.append(f"target_sources={source_text}")

    for row in _list(report.get("profiles")):
        lines.append(
            "calibration_profile "
            f"profile={row.get('profile')} "
            f"status={row.get('status')} "
            f"samples={row.get('sample_count')} "
            f"avg_p_model={_fmt(row.get('avg_p_model'))} "
            f"avg_target={_fmt(row.get('avg_target_yes_probability'))} "
            f"calibration_error={_fmt(row.get('calibration_error'))} "
            f"brier={_fmt(row.get('brier_score'))}"
        )
        suggestion = _dict(row.get("suggested_profile"))
        if suggestion:
            lines.append(
                "calibration_suggestion "
                f"profile={row.get('profile')} "
                f"base_logit={_fmt(suggestion.get('base_logit'))} "
                f"evidence_weight={_fmt(suggestion.get('evidence_weight'))} "
                f"note={row.get('recommendation_note')}"
            )
    return lines


def write_calibration_json(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_calibration_markdown(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_markdown_report(report), encoding="utf-8")


def _sample_from_fill(
    connection: sqlite3.Connection,
    fill: sqlite3.Row,
    settlement_index: dict[tuple[str, str | None], Settlement],
) -> CalibrationSample | None:
    fill_raw = _json_dict(fill["raw_json"])
    slug = str(fill["slug"])
    side = str(fill["side"])
    snapshot = _snapshot_for_fill(connection, slug, fill_raw.get("snapshot_timestamp_utc"), fill["timestamp_utc"])
    if not snapshot:
        return None

    p_model = _float(snapshot.get("p_model"))
    if p_model is None:
        return None

    settlement = settlement_index.get((slug, side)) or settlement_index.get((slug, None))
    target = _settlement_target_yes_probability(settlement, side) if settlement is not None else None
    target_source = "settlement_file" if target is not None else ""
    if target is None:
        mark = connection.execute(
            """
            SELECT mark_price
            FROM shadow_marks
            WHERE fill_id = ?
            ORDER BY timestamp_utc DESC, id DESC
            LIMIT 1
            """,
            (fill["id"],),
        ).fetchone()
        if mark is not None:
            target = _side_price_to_yes_probability(side, _float(mark["mark_price"]))
            target_source = "latest_mark"

    if target is None:
        target = _float(snapshot.get("yes_mid"))
        target_source = "snapshot_yes_mid"
    if target is None:
        return None

    reasons = _string_list(snapshot.get("signal_reasons_detail"))
    return CalibrationSample(
        fill_id=int(fill["id"]),
        slug=slug,
        side=side,
        profile=_profile_name(snapshot, reasons),
        event_type=str(snapshot.get("event_type") or "unknown"),
        platform=str(snapshot.get("platform") or "unknown"),
        action=_reason_value(reasons, "action") or str(snapshot.get("action") or "unknown"),
        p_model=_clip_probability(p_model),
        target_yes_probability=_clip_probability(target),
        target_source=target_source,
        evidence_score=_float(snapshot.get("evidence_score")),
        preheat_score=_coalesce_float(_float(snapshot.get("evidence_preheat_score")), _reason_float(reasons, "preheat_score")),
        cadence_score=_coalesce_float(_float(snapshot.get("evidence_cadence_score")), _reason_float(reasons, "cadence_score")),
        partner_score=_coalesce_float(_float(snapshot.get("evidence_partner_score")), _reason_float(reasons, "partner_score")),
        days_to_expiry=_float(snapshot.get("days_to_expiry")),
        spread=_float(snapshot.get("spread")),
    )


def _snapshot_for_fill(
    connection: sqlite3.Connection,
    slug: str,
    snapshot_timestamp: object,
    fill_timestamp: object,
) -> dict[str, object]:
    if isinstance(snapshot_timestamp, str) and snapshot_timestamp:
        row = connection.execute(
            """
            SELECT raw_json FROM watchlist_snapshots
            WHERE slug = ? AND timestamp_utc = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (slug, snapshot_timestamp),
        ).fetchone()
        if row is not None:
            return _json_dict(row["raw_json"])

    row = connection.execute(
        """
        SELECT raw_json FROM watchlist_snapshots
        WHERE slug = ? AND timestamp_utc <= ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
        """,
        (slug, fill_timestamp),
    ).fetchone()
    if row is not None:
        return _json_dict(row["raw_json"])

    row = connection.execute(
        """
        SELECT raw_json FROM watchlist_snapshots
        WHERE slug = ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    return _json_dict(row["raw_json"]) if row is not None else {}


def _calibrate_profile(profile: str, samples: list[CalibrationSample], min_samples: int) -> dict[str, object]:
    current = MODEL_PROFILES.get(profile)
    avg_p_model = _avg([sample.p_model for sample in samples])
    avg_target = _avg([sample.target_yes_probability for sample in samples])
    brier = _avg([(sample.p_model - sample.target_yes_probability) ** 2 for sample in samples])
    enough = len(samples) >= min_samples
    row: dict[str, object] = {
        "profile": profile,
        "sample_count": len(samples),
        "status": "ok" if enough else "insufficient_samples",
        "target_source_counts": dict(Counter(sample.target_source for sample in samples)),
        "event_type_counts": dict(Counter(sample.event_type for sample in samples)),
        "action_counts": dict(Counter(sample.action for sample in samples)),
        "avg_p_model": avg_p_model,
        "avg_target_yes_probability": avg_target,
        "calibration_error": round(avg_target - avg_p_model, 4) if avg_p_model is not None and avg_target is not None else None,
        "brier_score": brier,
        "current_profile": _profile_dict(current),
        "suggested_profile": {},
        "fitted_effective_weights": {},
        "recommendation_note": "",
    }
    if current is None:
        row["status"] = "unknown_profile"
        row["recommendation_note"] = "profile was not found in MODEL_PROFILES"
        return row
    if not enough:
        row["recommendation_note"] = "collect more settled or marked shadow samples before changing live parameters"
        return row

    base_delta = _logit(avg_target) - _logit(avg_p_model) if avg_p_model is not None and avg_target is not None else 0.0
    scalar = _fit_evidence_scalar(samples)
    suggested_evidence_weight = _bounded(current.evidence_weight * scalar, 0.05, 3.0)
    suggested = {
        "base_logit": round(current.base_logit + base_delta, 4),
        "evidence_weight": round(suggested_evidence_weight, 4),
    }
    fitted = _fit_effective_component_weights(samples)
    if fitted:
        row["fitted_effective_weights"] = fitted
    row["suggested_profile"] = suggested
    row["recommendation_note"] = (
        "treat latest_mark and snapshot_yes_mid targets as provisional; prefer settlement_file samples for production changes"
    )
    return row


def _fit_evidence_scalar(samples: list[CalibrationSample]) -> float:
    pairs: list[tuple[float, float]] = []
    for sample in samples:
        evidence_score = sample.evidence_score
        if evidence_score is None:
            continue
        target_logit = _logit(sample.target_yes_probability)
        model_logit = _logit(sample.p_model)
        residual = target_logit - model_logit
        signed_evidence = -evidence_score if sample.action.startswith("not_") else evidence_score
        if abs(signed_evidence) > 1e-9:
            pairs.append((signed_evidence, residual))
    if not pairs:
        return 1.0
    numerator = sum(x * y for x, y in pairs)
    denominator = sum(x * x for x, _ in pairs)
    return _bounded(1.0 + numerator / denominator, 0.25, 2.5) if denominator else 1.0


def _fit_effective_component_weights(samples: list[CalibrationSample]) -> dict[str, float]:
    complete = [
        sample
        for sample in samples
        if sample.preheat_score is not None and sample.cadence_score is not None and sample.partner_score is not None
    ]
    if len(complete) < 6:
        return {}

    rows: list[list[float]] = []
    targets: list[float] = []
    for sample in complete:
        sign = -1.0 if sample.action.startswith("not_") else 1.0
        rows.append(
            [
                1.0,
                sign * float(sample.preheat_score),
                sign * float(sample.cadence_score),
                sign * float(sample.partner_score),
                _time_score(sample.days_to_expiry),
                -max(0.0, (sample.spread or 0.0) - 0.06),
            ]
        )
        targets.append(_logit(sample.target_yes_probability))

    coefficients = _ridge_regression(rows, targets, ridge=0.25)
    if not coefficients:
        return {}
    keys = ["intercept", "preheat", "cadence", "partner", "time", "spread_penalty"]
    return {key: round(value, 4) for key, value in zip(keys, coefficients)}


def _ridge_regression(rows: list[list[float]], targets: list[float], ridge: float) -> list[float]:
    width = len(rows[0])
    matrix = [[0.0 for _ in range(width)] for _ in range(width)]
    vector = [0.0 for _ in range(width)]
    for row, target in zip(rows, targets):
        for i in range(width):
            vector[i] += row[i] * target
            for j in range(width):
                matrix[i][j] += row[i] * row[j]
    for i in range(1, width):
        matrix[i][i] += ridge
    return _solve_linear_system(matrix, vector)


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            return []
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def _markdown_report(report: dict[str, object]) -> str:
    lines = [
        "# Model Calibration Report",
        "",
        f"- Generated UTC: `{report.get('generated_at_utc')}`",
        f"- Database: `{report.get('db_path')}`",
        f"- Samples: `{report.get('sample_count', 0)}`",
        f"- Minimum samples per profile: `{report.get('min_samples', 0)}`",
        "",
        "## Profiles",
        "",
        "| profile | status | samples | avg model | avg target | error | brier | suggested base | suggested evidence |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in _list(report.get("profiles")):
        suggestion = _dict(row.get("suggested_profile"))
        lines.append(
            f"| {row.get('profile')} | {row.get('status')} | {row.get('sample_count')} | "
            f"{_fmt(row.get('avg_p_model'))} | {_fmt(row.get('avg_target_yes_probability'))} | "
            f"{_fmt(row.get('calibration_error'))} | {_fmt(row.get('brier_score'))} | "
            f"{_fmt(suggestion.get('base_logit'))} | {_fmt(suggestion.get('evidence_weight'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _group_by_profile(samples: list[CalibrationSample]) -> dict[str, list[CalibrationSample]]:
    groups: dict[str, list[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.profile].append(sample)
    return dict(sorted(groups.items()))


def _profile_name(snapshot: dict[str, object], reasons: list[str]) -> str:
    reason_profile = _reason_value(reasons, "model_profile")
    if reason_profile:
        return reason_profile
    event_type = str(snapshot.get("event_type") or "")
    platform = str(snapshot.get("platform") or "")
    title = str(snapshot.get("title") or "").lower()
    if event_type == "ipo_event":
        return "ipo_event"
    if event_type == "content_release" and (platform in {"apple", "tesla"} or any(word in title for word in ("macbook", "optimus", "hardware", "device"))):
        return "product_release"
    if event_type == "content_release" and (platform == "streaming" or any(word in title for word in ("album", "song", "single", "music", "spotify", "apple music"))):
        return "music_release"
    return "default_content"


def _settlement_index(settlements: list[Settlement]) -> dict[tuple[str, str | None], Settlement]:
    return {(settlement.slug, settlement.side): settlement for settlement in settlements}


def _settlement_target_yes_probability(settlement: Settlement | None, fill_side: str) -> float | None:
    if settlement is None:
        return None
    if settlement.winning_side == "BUY_YES":
        return 1.0
    if settlement.winning_side == "BUY_NO":
        return 0.0
    if settlement.close_price is None:
        return None
    settlement_side = settlement.side or fill_side
    return _side_price_to_yes_probability(settlement_side, settlement.close_price)


def _side_price_to_yes_probability(side: str, side_price: float | None) -> float | None:
    if side_price is None:
        return None
    if side == "BUY_YES":
        return side_price
    if side == "BUY_NO":
        return 1.0 - side_price
    return None


def _profile_dict(profile: object) -> dict[str, float]:
    if profile is None:
        return {}
    return {
        "base_logit": profile.base_logit,
        "negated_action_base_logit": profile.negated_action_base_logit,
        "evidence_weight": profile.evidence_weight,
        "preheat_weight": profile.preheat_weight,
        "cadence_weight": profile.cadence_weight,
        "partner_weight": profile.partner_weight,
        "time_weight": profile.time_weight,
        "spread_penalty_weight": profile.spread_penalty_weight,
    }


def _time_score(days_to_expiry: float | None) -> float:
    if days_to_expiry is None:
        return 0.0
    if days_to_expiry < 1:
        return -0.25
    if days_to_expiry <= 3:
        return 0.18
    if days_to_expiry <= 7:
        return 0.08
    return -0.02


def _reason_value(reasons: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for reason in reasons:
        if reason.startswith(prefix):
            return reason[len(prefix) :]
    return None


def _reason_float(reasons: list[str], key: str) -> float | None:
    value = _reason_value(reasons, key)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _json_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coalesce_float(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _clip_probability(value: float) -> float:
    return min(0.99, max(0.01, float(value)))


def _logit(value: float | None) -> float:
    probability = _clip_probability(value if value is not None else 0.5)
    return math.log(probability / (1.0 - probability))


def _bounded(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[dict[str, object]]:
    return [row for row in value] if isinstance(value, list) else []


def _fmt(value: object) -> str:
    number = _float(value)
    return "none" if number is None else f"{number:.4f}"
