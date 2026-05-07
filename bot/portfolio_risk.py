from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import sqlite3
from contextlib import closing
from typing import Any

from bot.config import BotConfig


@dataclass(frozen=True)
class PortfolioRiskState:
    bankroll: float
    open_positions: int
    total_exposure: float
    total_exposure_pct: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    circuit_breaker_active: bool
    circuit_breaker_reasons: list[str]
    exposure_by_event_type: dict[str, float]
    exposure_by_slug: dict[str, float]


def filter_shadow_fills_for_portfolio(
    db_path: str | None,
    candidate_fills: list[dict[str, object]],
    config: BotConfig,
) -> tuple[list[dict[str, object]], PortfolioRiskState]:
    state = load_portfolio_risk_state(db_path, config)
    accepted: list[dict[str, object]] = []
    working_total = state.total_exposure
    working_open_positions = state.open_positions
    working_by_event_type = dict(state.exposure_by_event_type)
    working_by_slug = dict(state.exposure_by_slug)

    for fill in candidate_fills:
        reasons = list(state.circuit_breaker_reasons)
        slug = str(fill.get("slug") or "unknown")
        event_type = str(fill.get("event_type") or "unknown")
        risk_amount = _fill_risk_amount(fill, config)

        if working_open_positions >= config.max_open_shadow_positions:
            reasons.append("max_open_shadow_positions_reached")
        if working_total + risk_amount > config.shadow_bankroll * config.max_total_risk_pct:
            reasons.append("total_exposure_limit")
        if working_by_slug.get(slug, 0.0) + risk_amount > config.shadow_bankroll * config.max_market_risk_pct:
            reasons.append("market_exposure_limit")
        if working_by_event_type.get(event_type, 0.0) + risk_amount > config.shadow_bankroll * config.max_event_type_risk_pct:
            reasons.append("event_type_exposure_limit")

        fill["portfolio_risk_amount"] = risk_amount
        fill["portfolio_total_exposure_before"] = round(working_total, 4)
        fill["portfolio_event_type_exposure_before"] = round(working_by_event_type.get(event_type, 0.0), 4)
        fill["portfolio_slug_exposure_before"] = round(working_by_slug.get(slug, 0.0), 4)

        if reasons:
            fill["portfolio_risk_ok"] = False
            fill["portfolio_risk_reasons"] = reasons
            continue

        fill["portfolio_risk_ok"] = True
        fill["portfolio_risk_reasons"] = []
        accepted.append(fill)
        working_total = round(working_total + risk_amount, 4)
        working_open_positions += 1
        working_by_event_type[event_type] = round(working_by_event_type.get(event_type, 0.0) + risk_amount, 4)
        working_by_slug[slug] = round(working_by_slug.get(slug, 0.0) + risk_amount, 4)

    return accepted, state


def load_portfolio_risk_state(db_path: str | None, config: BotConfig) -> PortfolioRiskState:
    positions = _load_open_positions(db_path, config)
    total_exposure = round(sum(position["risk_amount"] for position in positions), 4)
    unrealized_pnl = round(sum(position["unrealized_pnl"] for position in positions), 4)
    by_event_type: dict[str, float] = defaultdict(float)
    by_slug: dict[str, float] = defaultdict(float)
    for position in positions:
        by_event_type[str(position["event_type"])] += float(position["risk_amount"])
        by_slug[str(position["slug"])] += float(position["risk_amount"])

    reasons: list[str] = []
    if len(positions) >= config.max_open_shadow_positions:
        reasons.append("max_open_shadow_positions_reached")
    if unrealized_pnl <= -config.shadow_bankroll * config.circuit_breaker_loss_pct:
        reasons.append("portfolio_loss_limit")

    return PortfolioRiskState(
        bankroll=config.shadow_bankroll,
        open_positions=len(positions),
        total_exposure=total_exposure,
        total_exposure_pct=round(total_exposure / config.shadow_bankroll, 6) if config.shadow_bankroll > 0 else 0.0,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=round(unrealized_pnl / config.shadow_bankroll, 6) if config.shadow_bankroll > 0 else 0.0,
        circuit_breaker_active=bool(reasons),
        circuit_breaker_reasons=reasons,
        exposure_by_event_type={key: round(value, 4) for key, value in sorted(by_event_type.items())},
        exposure_by_slug={key: round(value, 4) for key, value in sorted(by_slug.items())},
    )


def state_to_dict(state: PortfolioRiskState) -> dict[str, object]:
    return {
        "bankroll": state.bankroll,
        "open_positions": state.open_positions,
        "total_exposure": state.total_exposure,
        "total_exposure_pct": state.total_exposure_pct,
        "unrealized_pnl": state.unrealized_pnl,
        "unrealized_pnl_pct": state.unrealized_pnl_pct,
        "circuit_breaker_active": state.circuit_breaker_active,
        "circuit_breaker_reasons": state.circuit_breaker_reasons,
        "exposure_by_event_type": state.exposure_by_event_type,
        "exposure_by_slug": state.exposure_by_slug,
    }


def _load_open_positions(db_path: str | None, config: BotConfig) -> list[dict[str, Any]]:
    if not db_path:
        return []
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error:
        return []

    with closing(connection):
        connection.row_factory = sqlite3.Row
        try:
            fills = connection.execute(
                """
                SELECT id, slug, side, fill_price, raw_json
                FROM shadow_fills
                ORDER BY id
                """
            ).fetchall()
        except sqlite3.Error:
            return []

        positions: list[dict[str, Any]] = []
        for fill in fills:
            raw = _json_dict(fill["raw_json"])
            latest_mark = connection.execute(
                """
                SELECT unrealized_pnl
                FROM shadow_marks
                WHERE fill_id = ?
                ORDER BY timestamp_utc DESC, id DESC
                LIMIT 1
                """,
                (fill["id"],),
            ).fetchone()
            positions.append(
                {
                    "fill_id": fill["id"],
                    "slug": str(fill["slug"]),
                    "side": str(fill["side"]),
                    "event_type": str(raw.get("event_type") or _event_type_for_slug(connection, str(fill["slug"]))),
                    "risk_amount": _fill_risk_amount(raw, config),
                    "unrealized_pnl": _position_pnl(raw, fill, latest_mark, config),
                }
            )
        return positions


def _event_type_for_slug(connection: sqlite3.Connection, slug: str) -> str:
    row = connection.execute(
        """
        SELECT raw_json FROM watchlist_snapshots
        WHERE slug = ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    if row is None:
        return "unknown"
    return str(_json_dict(row["raw_json"]).get("event_type") or "unknown")


def _fill_risk_amount(fill: dict[str, object], config: BotConfig) -> float:
    value = fill.get("risk_amount") or fill.get("portfolio_risk_amount")
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return round(config.shadow_bankroll * config.shadow_position_risk_pct, 4)


def _position_pnl(raw: dict[str, object], fill: sqlite3.Row, latest_mark: sqlite3.Row | None, config: BotConfig) -> float:
    if latest_mark is None:
        return 0.0
    price_pnl = latest_mark["unrealized_pnl"]
    if not isinstance(price_pnl, (int, float)):
        return 0.0
    share_quantity = raw.get("share_quantity")
    if isinstance(share_quantity, (int, float)):
        return round(float(price_pnl) * float(share_quantity), 4)
    fill_price = fill["fill_price"]
    if isinstance(fill_price, (int, float)) and float(fill_price) > 0:
        return round(float(price_pnl) * (_fill_risk_amount(raw, config) / float(fill_price)), 4)
    return 0.0


def _json_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
