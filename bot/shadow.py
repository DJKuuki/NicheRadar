from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bot.config import BotConfig


def build_shadow_fills(snapshots: list[dict[str, object]], config: BotConfig | None = None) -> list[dict[str, object]]:
    fills: list[dict[str, object]] = []
    risk_amount = _risk_amount(config)
    for snapshot in snapshots:
        if snapshot.get("market_ok") is not True:
            continue
        if snapshot.get("signal_ok") is not True:
            continue

        side = str(snapshot.get("model_side") or "")
        if side != snapshot.get("preferred_side"):
            continue
        max_entry_price = _float_value(snapshot.get("max_entry_price"))
        ask_price = _side_ask(snapshot, side)
        if side not in {"BUY_YES", "BUY_NO"} or max_entry_price is None or ask_price is None:
            continue

        if ask_price <= max_entry_price:
            fills.append(
                {
                    "record_type": "shadow_fill",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "slug": snapshot.get("slug"),
                    "label": snapshot.get("label"),
                    "market_id": snapshot.get("market_id"),
                    "title": snapshot.get("title"),
                    "event_type": snapshot.get("event_type"),
                    "platform": snapshot.get("platform"),
                    "side": side,
                    "fill_price": ask_price,
                    "share_quantity": round(risk_amount / ask_price, 4) if ask_price > 0 else None,
                    "risk_amount": risk_amount,
                    "max_entry_price": max_entry_price,
                    "net_edge": snapshot.get("net_edge"),
                    "reason": "ask_at_or_below_max_entry",
                    "snapshot_timestamp_utc": snapshot.get("timestamp_utc"),
                }
            )
    return fills


def append_shadow_fills(path: str, fills: list[dict[str, object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for fill in fills:
            handle.write(json.dumps(fill, ensure_ascii=True) + "\n")


def _side_ask(snapshot: dict[str, object], side: str) -> float | None:
    if side == "BUY_YES":
        return _float_value(snapshot.get("yes_ask"))
    if side == "BUY_NO":
        return _float_value(snapshot.get("no_ask"))
    return None


def _float_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _risk_amount(config: BotConfig | None) -> float:
    if config is None:
        return 20.0
    return round(config.shadow_bankroll * config.shadow_position_risk_pct, 4)
