from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bot.shadow_replay import Settlement


def validate_settlements(db_path: str, settlements: list[Settlement]) -> dict[str, object]:
    fills = _load_fills(db_path)
    fill_keys = {(str(fill["slug"]), str(fill["side"])) for fill in fills}
    fill_slugs = {str(fill["slug"]) for fill in fills}
    filled_by_slug = _filled_sides_by_slug(fills)

    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    seen: dict[tuple[str, str | None], Settlement] = {}

    for settlement in settlements:
        key = (settlement.slug, settlement.side)
        if key in seen:
            errors.append(_issue("duplicate_settlement", settlement, "duplicate slug + side settlement row"))
        else:
            seen[key] = settlement

        if settlement.slug not in fill_slugs:
            warnings.append(_issue("unknown_slug", settlement, "settlement slug has no matching shadow fill"))
        elif settlement.side is not None and (settlement.slug, settlement.side) not in fill_keys:
            warnings.append(_issue("unknown_side", settlement, "settlement side has no matching shadow fill for this slug"))

        if settlement.close_price is not None and not 0.0 <= settlement.close_price <= 1.0:
            errors.append(_issue("close_price_out_of_range", settlement, "close_price must be between 0 and 1"))

        if settlement.status == "settled" and settlement.winning_side is None and settlement.close_price not in {0.0, 1.0}:
            warnings.append(_issue("settled_without_binary_outcome", settlement, "settled rows should usually provide winning_side or binary close_price"))

        if settlement.winning_side is not None and settlement.close_price is not None:
            expected = _expected_close_price(settlement)
            if expected is not None and abs(settlement.close_price - expected) > 1e-9:
                errors.append(_issue("winning_side_close_price_conflict", settlement, "winning_side conflicts with close_price"))

    covered_fill_ids = _covered_fill_ids(fills, settlements)
    unsettled = [
        {
            "fill_id": fill["id"],
            "slug": fill["slug"],
            "side": fill["side"],
            "timestamp_utc": fill["timestamp_utc"],
        }
        for fill in fills
        if int(fill["id"]) not in covered_fill_ids
    ]
    duplicate_slugs = {
        slug: sorted(sides)
        for slug, sides in filled_by_slug.items()
        if len(sides) > 1
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "shadow_fill_count": len(fills),
        "settlement_count": len(settlements),
        "covered_fill_count": len(covered_fill_ids),
        "coverage_pct": round(len(covered_fill_ids) / len(fills), 4) if fills else None,
        "unsettled_fill_count": len(unsettled),
        "status_counts": dict(Counter(settlement.status for settlement in settlements)),
        "side_counts": dict(Counter(settlement.side or "all_sides" for settlement in settlements)),
        "duplicate_side_slugs": duplicate_slugs,
        "errors": errors,
        "warnings": warnings,
        "unsettled_fills": unsettled,
        "valid": not errors,
    }


def format_settlement_validation(report: dict[str, object]) -> list[str]:
    lines = [
        "settlement_validation",
        f"db_path={report.get('db_path')}",
        f"valid={str(report.get('valid')).lower()}",
        f"shadow_fills={report.get('shadow_fill_count', 0)}",
        f"settlements={report.get('settlement_count', 0)}",
        f"covered_fills={report.get('covered_fill_count', 0)}",
        f"coverage={_fmt_pct(report.get('coverage_pct'))}",
        f"errors={len(_list(report.get('errors')))}",
        f"warnings={len(_list(report.get('warnings')))}",
    ]
    for issue in _list(report.get("errors")):
        lines.append(_format_issue("error", issue))
    for issue in _list(report.get("warnings")):
        lines.append(_format_issue("warning", issue))
    return lines


def write_settlement_validation_json(path: str, report: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_fills(db_path: str) -> list[dict[str, object]]:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, timestamp_utc, slug, side
            FROM shadow_fills
            ORDER BY timestamp_utc, id
            """
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _covered_fill_ids(fills: list[dict[str, object]], settlements: list[Settlement]) -> set[int]:
    exact = {(settlement.slug, settlement.side) for settlement in settlements if settlement.side is not None}
    slug_wide = {settlement.slug for settlement in settlements if settlement.side is None}
    covered: set[int] = set()
    for fill in fills:
        slug = str(fill["slug"])
        side = str(fill["side"])
        if (slug, side) in exact or slug in slug_wide:
            covered.add(int(fill["id"]))
    return covered


def _filled_sides_by_slug(fills: list[dict[str, object]]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for fill in fills:
        grouped[str(fill["slug"])].add(str(fill["side"]))
    return grouped


def _expected_close_price(settlement: Settlement) -> float | None:
    side = settlement.side
    if side is None:
        return None
    return 1.0 if settlement.winning_side == side else 0.0


def _issue(code: str, settlement: Settlement, message: str) -> dict[str, object]:
    return {
        "code": code,
        "slug": settlement.slug,
        "side": settlement.side,
        "status": settlement.status,
        "message": message,
    }


def _format_issue(level: str, issue: dict[str, object]) -> str:
    return (
        f"{level} "
        f"code={issue.get('code')} "
        f"slug={issue.get('slug')} "
        f"side={issue.get('side') or 'all_sides'} "
        f"message={issue.get('message')}"
    )


def _list(value: object) -> list[dict[str, object]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _fmt_pct(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "none"
    return f"{float(value):.2%}"
