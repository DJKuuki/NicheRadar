from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestStrategyParams:
    min_net_edge: float = 0.0
    max_spread: float | None = None
    entry_price_mode: str = "ask"
    model_profile: str | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class BacktestEntry:
    eligible: bool
    fill_price: float | None
    reason: str


def evaluate_shadow_entry(snapshot: dict[str, object], params: BacktestStrategyParams | None = None) -> BacktestEntry:
    strategy = params or BacktestStrategyParams()
    if strategy.entry_price_mode != "ask":
        raise ValueError("Only entry_price_mode='ask' is supported.")

    if strategy.model_profile and _profile_name(snapshot) != strategy.model_profile:
        return BacktestEntry(False, None, "profile_filter")
    if strategy.event_type and str(snapshot.get("event_type") or "unknown") != strategy.event_type:
        return BacktestEntry(False, None, "event_type_filter")
    if snapshot.get("market_ok") is not True:
        return BacktestEntry(False, None, "market_not_ok")
    if snapshot.get("signal_ok") is not True:
        return BacktestEntry(False, None, "signal_not_ok")

    side = str(snapshot.get("model_side") or "")
    if side not in {"BUY_YES", "BUY_NO"}:
        return BacktestEntry(False, None, "unsupported_side")
    if side != snapshot.get("preferred_side"):
        return BacktestEntry(False, None, "model_side_not_preferred")

    net_edge = _float(snapshot.get("net_edge"))
    if net_edge is None or net_edge < strategy.min_net_edge:
        return BacktestEntry(False, None, "net_edge_below_min")

    spread = _side_spread(snapshot, side)
    if strategy.max_spread is not None and (spread is None or spread > strategy.max_spread):
        return BacktestEntry(False, None, "spread_above_max")

    max_entry_price = _float(snapshot.get("max_entry_price"))
    ask_price = _side_ask(snapshot, side)
    if max_entry_price is None or ask_price is None:
        return BacktestEntry(False, None, "missing_entry_price")
    if ask_price > max_entry_price:
        return BacktestEntry(False, None, "ask_above_max_entry")

    return BacktestEntry(True, ask_price, "ask_at_or_below_max_entry")


def _side_ask(snapshot: dict[str, object], side: str) -> float | None:
    if side == "BUY_YES":
        return _float(snapshot.get("yes_ask"))
    if side == "BUY_NO":
        return _float(snapshot.get("no_ask"))
    return None


def _side_spread(snapshot: dict[str, object], side: str) -> float | None:
    if side == "BUY_YES":
        return _float(snapshot.get("yes_spread")) or _spread(snapshot.get("yes_bid"), snapshot.get("yes_ask"))
    if side == "BUY_NO":
        return _float(snapshot.get("no_spread")) or _spread(snapshot.get("no_bid"), snapshot.get("no_ask"))
    return None


def _spread(bid: object, ask: object) -> float | None:
    bid_float = _float(bid)
    ask_float = _float(ask)
    if bid_float is None or ask_float is None:
        return None
    return round(ask_float - bid_float, 4)


def _profile_name(snapshot: dict[str, object]) -> str:
    reasons = _string_list(snapshot.get("signal_reasons_detail"))
    profile = _reason_value(reasons, "model_profile")
    if profile:
        return profile
    event_type = str(snapshot.get("event_type") or "")
    platform = str(snapshot.get("platform") or "")
    title = str(snapshot.get("title") or "").lower()
    if event_type == "ipo_event":
        return "ipo_event"
    if event_type == "content_release" and (
        platform in {"apple", "tesla"} or any(word in title for word in ("macbook", "optimus", "hardware", "device"))
    ):
        return "product_release"
    if event_type == "content_release" and (
        platform == "streaming" or any(word in title for word in ("album", "song", "single", "music", "spotify", "apple music"))
    ):
        return "music_release"
    return "default_content"


def _reason_value(reasons: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for reason in reasons:
        if reason.startswith(prefix):
            return reason[len(prefix) :]
    return None


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
