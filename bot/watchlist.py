from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.models import Evidence, Market, ParsedMarket, Signal


@dataclass(frozen=True)
class WatchlistItem:
    slug: str
    label: str
    preferred_side: str
    entry_band_low: float
    entry_band_high: float
    note: str


def load_watchlist(path: str) -> list[WatchlistItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items: list[WatchlistItem] = []
    for item in payload:
        preferred_side = str(item["preferred_side"])
        if preferred_side not in {"BUY_YES", "BUY_NO"}:
            raise ValueError(f"Unsupported preferred_side={preferred_side}")
        entry_band_low = float(item["entry_band_low"])
        entry_band_high = float(item["entry_band_high"])
        if entry_band_low > entry_band_high:
            raise ValueError(f"Invalid entry band for slug={item['slug']}")
        items.append(
            WatchlistItem(
                slug=str(item["slug"]),
                label=str(item["label"]),
                preferred_side=preferred_side,
                entry_band_low=entry_band_low,
                entry_band_high=entry_band_high,
                note=str(item["note"]),
            )
        )
    return items


def build_watchlist_report(
    item: WatchlistItem,
    market: Market,
    parsed: ParsedMarket | None,
    signal: Signal | None,
    market_ok: bool,
    market_reasons: list[str],
    signal_ok: bool,
    signal_reasons: list[str],
) -> list[str]:
    yes_mid = market.mid_probability
    no_mid = market.no_mid_probability
    preferred_price = yes_mid if item.preferred_side == "BUY_YES" else no_mid
    in_band = item.entry_band_low <= preferred_price <= item.entry_band_high
    lines = [
        f"watchlist_market slug={item.slug} label={item.label}",
        f"  title={market.title}",
        f"  preferred_side={item.preferred_side}",
        f"  yes_bid={market.yes_bid:.4f} yes_ask={market.yes_ask:.4f} yes_mid={yes_mid:.4f}",
        f"  no_bid={market.no_bid:.4f} no_ask={market.no_ask:.4f} no_mid={no_mid:.4f}",
        f"  preferred_price={preferred_price:.4f} target_band={item.entry_band_low:.4f}-{item.entry_band_high:.4f}",
        f"  in_target_band={str(in_band).lower()}",
        f"  note={item.note}",
    ]

    if parsed is not None:
        lines.append(
            f"  parsed subject={parsed.subject} platform={parsed.platform} event_type={parsed.event_type} days_to_expiry={parsed.days_to_expiry:.2f}"
        )

    if signal is not None:
        lines.append(
            f"  model side={signal.side} p_model={signal.p_model:.4f} p_mid={signal.p_mid:.4f} net_edge={signal.net_edge:.4f} confidence={signal.confidence:.4f}"
        )
        for reason in signal.reasons:
            lines.append(f"  reason={reason}")

    if not market_ok:
        lines.append(f"  market_filter={','.join(market_reasons)}")
    if not signal_ok:
        lines.append(f"  signal_filter={','.join(signal_reasons)}")
    return lines


def build_watchlist_snapshot(
    item: WatchlistItem,
    market: Market,
    parsed: ParsedMarket | None,
    evidence: Evidence | None,
    signal: Signal | None,
    market_ok: bool,
    market_reasons: list[str],
    signal_ok: bool,
    signal_reasons: list[str],
) -> dict[str, object]:
    yes_mid = market.mid_probability
    no_mid = market.no_mid_probability
    preferred_price = yes_mid if item.preferred_side == "BUY_YES" else no_mid
    snapshot: dict[str, object] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "slug": item.slug,
        "label": item.label,
        "market_id": market.market_id,
        "title": market.title,
        "preferred_side": item.preferred_side,
        "target_band_low": item.entry_band_low,
        "target_band_high": item.entry_band_high,
        "preferred_price": preferred_price,
        "in_target_band": item.entry_band_low <= preferred_price <= item.entry_band_high,
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "yes_mid": yes_mid,
        "no_bid": market.no_bid,
        "no_ask": market.no_ask,
        "no_mid": no_mid,
        "yes_spread": market.spread,
        "no_spread": market.no_spread,
        "spread": market.spread_for_side(item.preferred_side),
        "volume": market.volume,
        "note": item.note,
        "market_ok": market_ok,
        "market_reasons": market_reasons,
        "signal_ok": signal_ok,
        "signal_reasons": signal_reasons,
    }
    if parsed is not None:
        snapshot.update(
            {
                "subject": parsed.subject,
                "platform": parsed.platform,
                "event_type": parsed.event_type,
                "days_to_expiry": parsed.days_to_expiry,
            }
        )
    if evidence is not None:
        snapshot.update(
            {
                "evidence_score": evidence.score,
                "evidence_confidence": evidence.confidence,
                "evidence_reasons": evidence.reasons,
                "evidence_mode": evidence.mode,
                "evidence_source_url": evidence.source_url,
                "evidence_source_type": evidence.source_type,
                "evidence_recent_entries_30d": evidence.recent_entries_30d,
                "evidence_keyword_hits_30d": evidence.keyword_hits_30d,
                "evidence_latest_entry_age_days": evidence.latest_entry_age_days,
                "evidence_preheat_score": evidence.preheat_score,
                "evidence_cadence_score": evidence.cadence_score,
                "evidence_partner_score": evidence.partner_score,
                "evidence_source_reliability": evidence.source_reliability,
            }
        )
    if signal is not None:
        snapshot.update(
            {
                "model_side": signal.side,
                "p_model": signal.p_model,
                "p_mid": signal.p_mid,
                "edge": signal.edge,
                "net_edge": signal.net_edge,
                "max_entry_price": signal.max_entry_price,
                "model_confidence": signal.confidence,
                "signal_reasons_detail": signal.reasons,
            }
        )
    return snapshot


def append_watchlist_snapshots(path: str, snapshots: list[dict[str, object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for snapshot in snapshots:
            handle.write(json.dumps(snapshot, ensure_ascii=True) + "\n")


def load_latest_watchlist_snapshots(path: str) -> dict[str, dict[str, object]]:
    target = Path(path)
    if not target.exists():
        return {}

    latest: dict[str, dict[str, object]] = {}
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                snapshot = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(snapshot, dict):
                continue
            slug = snapshot.get("slug")
            if isinstance(slug, str) and slug:
                latest[slug] = snapshot
    return latest


def build_watchlist_alerts(
    previous_by_slug: dict[str, dict[str, object]],
    snapshots: list[dict[str, object]],
    evidence_jump_threshold: float,
) -> list[dict[str, object]]:
    alerts: list[dict[str, object]] = []
    for snapshot in snapshots:
        slug = str(snapshot.get("slug", ""))
        previous = previous_by_slug.get(slug)
        if not slug or previous is None:
            continue

        reasons: list[str] = []
        previous_in_band = _bool_value(previous.get("in_target_band"))
        current_in_band = _bool_value(snapshot.get("in_target_band"))
        if previous_in_band is False and current_in_band is True:
            reasons.append("entered_target_band")

        previous_signal_ok = _bool_value(previous.get("signal_ok"))
        current_signal_ok = _bool_value(snapshot.get("signal_ok"))
        if previous_signal_ok is False and current_signal_ok is True:
            reasons.append("signal_turned_ok")

        previous_score = _float_value(previous.get("evidence_score"))
        current_score = _float_value(snapshot.get("evidence_score"))
        if (
            previous_score is not None
            and current_score is not None
            and current_score - previous_score >= evidence_jump_threshold
        ):
            reasons.append("evidence_score_jump")

        if not reasons:
            continue

        alerts.append(
            {
                "record_type": "alert",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "label": snapshot.get("label"),
                "market_id": snapshot.get("market_id"),
                "title": snapshot.get("title"),
                "alert_reasons": reasons,
                "previous_timestamp_utc": previous.get("timestamp_utc"),
                "current_timestamp_utc": snapshot.get("timestamp_utc"),
                "previous": _alert_snapshot_summary(previous),
                "current": _alert_snapshot_summary(snapshot),
            }
        )
    return alerts


def append_watchlist_alerts(path: str, alerts: list[dict[str, object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert, ensure_ascii=True) + "\n")


def _alert_snapshot_summary(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "preferred_price": snapshot.get("preferred_price"),
        "in_target_band": snapshot.get("in_target_band"),
        "evidence_score": snapshot.get("evidence_score"),
        "signal_ok": snapshot.get("signal_ok"),
        "model_side": snapshot.get("model_side"),
        "net_edge": snapshot.get("net_edge"),
        "yes_mid": snapshot.get("yes_mid"),
    }


def _bool_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return None


def _float_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
