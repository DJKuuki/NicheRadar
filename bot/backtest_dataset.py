from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import sqlite3
from contextlib import closing

from bot.backtest_engine import BacktestStrategyParams, evaluate_shadow_entry
from bot.common import as_float as _float, as_str_list as _string_list
from bot.shadow_replay import Settlement


@dataclass(frozen=True)
class BacktestSample:
    timestamp_utc: str
    slug: str
    market_id: str | None
    event_type: str
    platform: str
    model_profile: str
    preferred_side: str | None
    model_side: str | None
    p_model: float | None
    p_mid: float | None
    net_edge: float | None
    evidence_score: float | None
    preheat_score: float | None
    cadence_score: float | None
    partner_score: float | None
    market_price: float | None
    fill_eligible: bool
    fill_price: float | None
    target_price: float | None
    target_yes_probability: float | None
    target_source: str
    realized_pnl: float | None


def load_backtest_samples(
    db_path: str,
    settlements: list[Settlement] | None = None,
    params: BacktestStrategyParams | None = None,
    target_source: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[BacktestSample]:
    strategy = params or BacktestStrategyParams()
    settlement_index = _settlement_index(settlements or [])
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, timestamp_utc, slug, market_id, raw_json
            FROM watchlist_snapshots
            ORDER BY timestamp_utc, id
            """
        ).fetchall()
        samples = [
            sample
            for row in rows
            if _date_in_range(str(row["timestamp_utc"]), start_date, end_date)
            if (sample := _sample_from_snapshot(connection, row, settlement_index, strategy)) is not None
        ]

    if target_source:
        samples = [sample for sample in samples if sample.target_source == target_source]
    return samples


def samples_to_dicts(samples: list[BacktestSample]) -> list[dict[str, object]]:
    return [asdict(sample) for sample in samples]


def _sample_from_snapshot(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    settlement_index: dict[tuple[str, str | None], Settlement],
    strategy: BacktestStrategyParams,
) -> BacktestSample | None:
    snapshot = _json_dict(row["raw_json"])
    if not snapshot:
        snapshot = dict(row)
    slug = str(row["slug"])
    timestamp_utc = str(row["timestamp_utc"])
    side = _optional_side(snapshot.get("model_side")) or _optional_side(snapshot.get("preferred_side"))
    profile = _profile_name(snapshot)
    entry = evaluate_shadow_entry(snapshot, strategy)
    stored_fill = _matching_fill(connection, slug, timestamp_utc, side)
    fill_eligible = entry.eligible or stored_fill is not None
    fill_price = _float(stored_fill["fill_price"]) if stored_fill is not None else entry.fill_price

    target_price, target_yes_probability, target_source = _target_for_sample(
        connection,
        snapshot,
        stored_fill,
        settlement_index.get((slug, side)) or settlement_index.get((slug, None)),
        side,
    )
    realized_pnl = round(target_price - fill_price, 4) if fill_eligible and fill_price is not None and target_price is not None else None

    return BacktestSample(
        timestamp_utc=timestamp_utc,
        slug=slug,
        market_id=_optional_string(row["market_id"] or snapshot.get("market_id")),
        event_type=str(snapshot.get("event_type") or "unknown"),
        platform=str(snapshot.get("platform") or "unknown"),
        model_profile=profile,
        preferred_side=_optional_side(snapshot.get("preferred_side")),
        model_side=_optional_side(snapshot.get("model_side")),
        p_model=_clip_probability(_float(snapshot.get("p_model"))),
        p_mid=_clip_probability(_float(snapshot.get("p_mid"))),
        net_edge=_float(snapshot.get("net_edge")),
        evidence_score=_float(snapshot.get("evidence_score")),
        preheat_score=_float(snapshot.get("evidence_preheat_score")),
        cadence_score=_float(snapshot.get("evidence_cadence_score")),
        partner_score=_float(snapshot.get("evidence_partner_score")),
        market_price=_side_mid(snapshot, side),
        fill_eligible=fill_eligible,
        fill_price=fill_price,
        target_price=target_price,
        target_yes_probability=target_yes_probability,
        target_source=target_source,
        realized_pnl=realized_pnl,
    )


def _matching_fill(connection: sqlite3.Connection, slug: str, snapshot_timestamp: str, side: str | None) -> sqlite3.Row | None:
    if side is None:
        return None
    rows = connection.execute(
        """
        SELECT id, timestamp_utc, side, fill_price, raw_json
        FROM shadow_fills
        WHERE slug = ? AND side = ?
        ORDER BY timestamp_utc, id
        """,
        (slug, side),
    ).fetchall()
    for fill in rows:
        if _json_dict(fill["raw_json"]).get("snapshot_timestamp_utc") == snapshot_timestamp:
            return fill
    return None


def _target_for_sample(
    connection: sqlite3.Connection,
    snapshot: dict[str, object],
    fill: sqlite3.Row | None,
    settlement: Settlement | None,
    side: str | None,
) -> tuple[float | None, float | None, str]:
    if side is None:
        return None, _clip_probability(_float(snapshot.get("yes_mid"))), "snapshot_mid"

    settlement_price = _settlement_side_price(settlement, side)
    if settlement_price is not None:
        return settlement_price, _side_price_to_yes_probability(side, settlement_price), "settlement_file"

    if fill is not None:
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
            mark_price = _float(mark["mark_price"])
            return mark_price, _side_price_to_yes_probability(side, mark_price), "latest_mark"

    side_mid = _side_mid(snapshot, side)
    return side_mid, _side_price_to_yes_probability(side, side_mid), "snapshot_mid"


def _settlement_side_price(settlement: Settlement | None, side: str) -> float | None:
    if settlement is None:
        return None
    if settlement.close_price is not None:
        settlement_side = settlement.side or side
        yes_probability = _side_price_to_yes_probability(settlement_side, settlement.close_price)
        if yes_probability is None:
            return None
        return yes_probability if side == "BUY_YES" else 1.0 - yes_probability
    if settlement.winning_side is None:
        return None
    return 1.0 if settlement.winning_side == side else 0.0


def _side_price_to_yes_probability(side: str, side_price: float | None) -> float | None:
    if side_price is None:
        return None
    if side == "BUY_YES":
        return _clip_probability(side_price)
    if side == "BUY_NO":
        return _clip_probability(1.0 - side_price)
    return None


def _side_mid(snapshot: dict[str, object], side: str | None) -> float | None:
    if side == "BUY_YES":
        return _float(snapshot.get("yes_mid"))
    if side == "BUY_NO":
        return _float(snapshot.get("no_mid"))
    return None


def _settlement_index(settlements: list[Settlement]) -> dict[tuple[str, str | None], Settlement]:
    return {(settlement.slug, settlement.side): settlement for settlement in settlements}


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


def _date_in_range(timestamp: str, start_date: str | None, end_date: str | None) -> bool:
    day = timestamp[:10]
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _json_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_side(value: object) -> str | None:
    if value in {"BUY_YES", "BUY_NO"}:
        return str(value)
    return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _reason_value(reasons: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for reason in reasons:
        if reason.startswith(prefix):
            return reason[len(prefix) :]
    return None


def _clip_probability(value: float | None) -> float | None:
    if value is None:
        return None
    return min(0.99, max(0.01, float(value)))


# ---------------------------------------------------------------------------
# Historical backtest loader (Phase 3)
# ---------------------------------------------------------------------------

def load_historical_backtest_samples(
    history_db_path: str,
    params: BacktestStrategyParams | None = None,
    target_source: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_days_before_close: float | None = None,
    min_days_before_close: float | None = None,
) -> list[BacktestSample]:
    """
    Load backtest samples from the historical_snapshots table in *history_db_path*.

    Unlike load_backtest_samples(), settlement is derived automatically from
    the ``outcome_yes`` column written by historical_snapshot_builder — no
    shadow fills or external settlement file required.

    Parameters
    ----------
    history_db_path
        Path to logs/historical.sqlite.
    params
        Optional BacktestStrategyParams for entry eligibility filtering.
    target_source
        If given (e.g. "outcome_yes"), filter to only that source.
    start_date / end_date
        ISO date strings (YYYY-MM-DD) for snapshot timestamp filtering.
    max_days_before_close / min_days_before_close
        Optional window relative to market resolution date.
        E.g. max_days_before_close=14 keeps only snapshots within 2 weeks of close.
    """
    import sqlite3
    from contextlib import closing

    strategy = params or BacktestStrategyParams()

    with closing(sqlite3.connect(history_db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM historical_snapshots
            WHERE outcome_yes IS NOT NULL
            ORDER BY timestamp_utc
            """
        ).fetchall()

    samples: list[BacktestSample] = []
    for row in rows:
        ts = str(row["timestamp_utc"])
        if not _date_in_range(ts, start_date, end_date):
            continue

        days_before = _float(row["days_before_close"])
        if max_days_before_close is not None and days_before is not None:
            if days_before > max_days_before_close:
                continue
        if min_days_before_close is not None and days_before is not None:
            if days_before < min_days_before_close:
                continue

        snapshot = _json_dict(row["raw_json"])
        if not snapshot:
            # Fall back to column values if raw_json is missing
            snapshot = dict(row)

        sample = _historical_sample_from_row(row, snapshot, strategy)
        if sample is None:
            continue
        if target_source and sample.target_source != target_source:
            continue
        samples.append(sample)

    return samples


def _historical_sample_from_row(
    row: "sqlite3.Row",
    snapshot: dict[str, object],
    strategy: BacktestStrategyParams,
) -> BacktestSample | None:
    """Build a BacktestSample from a historical_snapshots row."""
    side = _optional_side(snapshot.get("model_side")) or _optional_side(snapshot.get("preferred_side"))
    profile = _profile_name(snapshot)
    entry = evaluate_shadow_entry(snapshot, strategy)

    outcome_yes = _float(row["outcome_yes"])
    outcome_no = _float(row["outcome_no"])

    # Determine the target price from the known final outcome.
    # outcome_yes=1.0 means YES resolved, outcome_yes=0.0 means NO resolved.
    target_yes_probability: float | None = None
    target_price: float | None = None
    tgt_source = "outcome_yes"

    if outcome_yes is not None:
        target_yes_probability = _clip_probability(outcome_yes)
        if side == "BUY_YES":
            target_price = target_yes_probability
        elif side == "BUY_NO":
            target_price = _clip_probability(1.0 - outcome_yes) if outcome_yes is not None else None

    fill_price = entry.fill_price
    realized_pnl: float | None = None
    if entry.eligible and fill_price is not None and target_price is not None:
        realized_pnl = round(target_price - fill_price, 4)

    return BacktestSample(
        timestamp_utc=str(row["timestamp_utc"]),
        slug=str(row["slug"] or row["market_id"] or ""),
        market_id=_optional_string(row["market_id"]),
        event_type=str(snapshot.get("event_type") or "unknown"),
        platform=str(snapshot.get("platform") or "unknown"),
        model_profile=profile,
        preferred_side=_optional_side(snapshot.get("preferred_side")),
        model_side=_optional_side(snapshot.get("model_side")),
        p_model=_clip_probability(_float(snapshot.get("p_model"))),
        p_mid=_clip_probability(_float(snapshot.get("yes_mid"))),
        net_edge=_float(snapshot.get("net_edge")),
        evidence_score=_float(snapshot.get("evidence_score")),
        preheat_score=None,
        cadence_score=None,
        partner_score=None,
        market_price=_side_mid(snapshot, side),
        fill_eligible=entry.eligible,
        fill_price=fill_price,
        target_price=target_price,
        target_yes_probability=target_yes_probability,
        target_source=tgt_source,
        realized_pnl=realized_pnl,
    )
