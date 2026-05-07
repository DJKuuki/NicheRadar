from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settlement:
    slug: str
    side: str | None
    status: str
    close_price: float | None
    winning_side: str | None
    timestamp_utc: str | None
    note: str | None


def load_settlements(path: str | None) -> list[Settlement]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Settlement file must contain a JSON list.")

    settlements: list[Settlement] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Each settlement row must be an object.")
        slug = str(row.get("slug", "")).strip()
        if not slug:
            raise ValueError("Settlement row missing slug.")
        side = _optional_side(row.get("side"))
        winning_side = _optional_side(row.get("winning_side"))
        close_price = _float_or_none(row.get("close_price"))
        if winning_side is None and close_price is None:
            raise ValueError(f"Settlement row for slug={slug} needs winning_side or close_price.")
        status = str(row.get("status") or ("settled" if winning_side else "closed"))
        if status not in {"closed", "settled"}:
            raise ValueError(f"Unsupported settlement status={status}")
        settlements.append(
            Settlement(
                slug=slug,
                side=side,
                status=status,
                close_price=close_price,
                winning_side=winning_side,
                timestamp_utc=str(
                    row.get("timestamp_utc")
                    or row.get("closed_at_utc")
                    or row.get("settled_at_utc")
                    or ""
                )
                or None,
                note=str(row.get("note") or "") or None,
            )
        )
    return settlements


def replay_shadow_pnl(db_path: str, settlements: list[Settlement] | None = None) -> dict[str, object]:
    settlement_index = _settlement_index(settlements or [])
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        fills = connection.execute(
            """
            SELECT id, timestamp_utc, slug, label, market_id, side,
                   fill_price, max_entry_price, net_edge, raw_json
            FROM shadow_fills
            ORDER BY timestamp_utc, id
            """
        ).fetchall()
        records = [
            _replay_fill(connection, fill, settlement_index)
            for fill in fills
        ]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
        "summary_by_event_type": _summarize(records, "event_type"),
        "summary_by_status": _summarize(records, "status"),
    }


def format_shadow_replay_report(replay: dict[str, object]) -> list[str]:
    records = list(replay.get("records", []))
    lines = [
        "shadow_replay",
        f"record_count={replay.get('record_count', 0)}",
    ]
    for row in replay.get("summary_by_event_type", []):
        assert isinstance(row, dict)
        lines.append(_format_summary_row("event_type", row))
    for record in records:
        assert isinstance(record, dict)
        pnl = record.get("pnl")
        pnl_text = "none" if pnl is None else f"{float(pnl):.4f}"
        price = record.get("current_price")
        price_text = "none" if price is None else f"{float(price):.4f}"
        lines.append(
            "shadow_position "
            f"fill_id={record.get('fill_id')} "
            f"slug={record.get('slug')} "
            f"event_type={record.get('event_type')} "
            f"side={record.get('side')} "
            f"status={record.get('status')} "
            f"fill_price={float(record.get('fill_price', 0)):.4f} "
            f"current_price={price_text} "
            f"pnl={pnl_text}"
        )
    return lines


def write_replay_json(path: str, replay: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(replay, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _replay_fill(
    connection: sqlite3.Connection,
    fill: sqlite3.Row,
    settlement_index: dict[tuple[str, str | None], Settlement],
) -> dict[str, object]:
    fill_raw = _json_dict(fill["raw_json"])
    side = str(fill["side"])
    slug = str(fill["slug"])
    snapshot = _snapshot_for_fill(connection, slug, fill_raw.get("snapshot_timestamp_utc"))
    latest_mark = connection.execute(
        """
        SELECT timestamp_utc, mark_price, unrealized_pnl, unrealized_pnl_pct
        FROM shadow_marks
        WHERE fill_id = ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
        """,
        (fill["id"],),
    ).fetchone()
    settlement = settlement_index.get((slug, side)) or settlement_index.get((slug, None))

    fill_price = float(fill["fill_price"])
    event_type = str(snapshot.get("event_type") or fill_raw.get("event_type") or "unknown")
    platform = str(snapshot.get("platform") or fill_raw.get("platform") or "unknown")
    status = "open_marked" if latest_mark is not None else "open_unmarked"
    current_price = float(latest_mark["mark_price"]) if latest_mark is not None else None
    pnl = float(latest_mark["unrealized_pnl"]) if latest_mark is not None else None
    pnl_pct = float(latest_mark["unrealized_pnl_pct"]) if latest_mark is not None and latest_mark["unrealized_pnl_pct"] is not None else None
    closed_at_utc = None
    close_source = "latest_mark" if latest_mark is not None else None

    if settlement is not None:
        status = settlement.status
        current_price = _settlement_close_price(settlement, side)
        assert current_price is not None
        pnl = round(current_price - fill_price, 4)
        pnl_pct = round(pnl / fill_price, 6) if fill_price > 0 else None
        closed_at_utc = settlement.timestamp_utc
        close_source = "settlement_file"

    return {
        "fill_id": fill["id"],
        "slug": slug,
        "label": fill["label"],
        "market_id": fill["market_id"],
        "event_type": event_type,
        "platform": platform,
        "side": side,
        "status": status,
        "opened_at_utc": fill["timestamp_utc"],
        "closed_at_utc": closed_at_utc,
        "fill_price": fill_price,
        "current_price": current_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "max_entry_price": _float_or_none(fill["max_entry_price"]),
        "net_edge": _float_or_none(fill["net_edge"]),
        "close_source": close_source,
        "settlement_note": settlement.note if settlement else None,
    }


def _snapshot_for_fill(connection: sqlite3.Connection, slug: str, snapshot_timestamp: object) -> dict[str, object]:
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
        WHERE slug = ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 1
        """,
        (slug,),
    ).fetchone()
    return _json_dict(row["raw_json"]) if row is not None else {}


def _summarize(records: list[dict[str, object]], key: str) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for record in records:
        groups.setdefault(str(record.get(key) or "unknown"), []).append(record)

    summary: list[dict[str, object]] = []
    for group_key in sorted(groups):
        rows = groups[group_key]
        pnl_values = [float(row["pnl"]) for row in rows if row.get("pnl") is not None]
        closed = [row for row in rows if row.get("status") in {"closed", "settled"}]
        open_rows = [row for row in rows if str(row.get("status", "")).startswith("open_")]
        wins = [pnl for pnl in pnl_values if pnl > 0]
        summary.append(
            {
                key: group_key,
                "count": len(rows),
                "open_count": len(open_rows),
                "closed_count": len(closed),
                "marked_count": len(pnl_values),
                "total_pnl": round(sum(pnl_values), 4),
                "avg_pnl": round(sum(pnl_values) / len(pnl_values), 4) if pnl_values else None,
                "win_rate": round(len(wins) / len(pnl_values), 4) if pnl_values else None,
                "realized_pnl": round(sum(float(row["pnl"]) for row in closed if row.get("pnl") is not None), 4),
                "unrealized_pnl": round(sum(float(row["pnl"]) for row in open_rows if row.get("pnl") is not None), 4),
            }
        )
    return summary


def _format_summary_row(key: str, row: dict[str, object]) -> str:
    avg = row.get("avg_pnl")
    win_rate = row.get("win_rate")
    avg_text = "none" if avg is None else f"{float(avg):.4f}"
    win_text = "none" if win_rate is None else f"{float(win_rate):.2%}"
    return (
        "shadow_summary "
        f"{key}={row.get(key)} "
        f"count={row.get('count')} "
        f"open={row.get('open_count')} "
        f"closed={row.get('closed_count')} "
        f"total_pnl={float(row.get('total_pnl', 0)):.4f} "
        f"realized_pnl={float(row.get('realized_pnl', 0)):.4f} "
        f"unrealized_pnl={float(row.get('unrealized_pnl', 0)):.4f} "
        f"avg_pnl={avg_text} "
        f"win_rate={win_text}"
    )


def _settlement_index(settlements: list[Settlement]) -> dict[tuple[str, str | None], Settlement]:
    indexed: dict[tuple[str, str | None], Settlement] = {}
    for settlement in settlements:
        indexed[(settlement.slug, settlement.side)] = settlement
    return indexed


def _settlement_close_price(settlement: Settlement, side: str) -> float | None:
    if settlement.close_price is not None:
        return settlement.close_price
    if settlement.winning_side is None:
        return None
    return 1.0 if settlement.winning_side == side else 0.0


def _optional_side(value: object) -> str | None:
    if value is None or value == "":
        return None
    side = str(value)
    if side not in {"BUY_YES", "BUY_NO"}:
        raise ValueError(f"Unsupported side={side}")
    return side


def _json_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
