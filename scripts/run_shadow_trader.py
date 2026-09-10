"""CLI runner and dashboard monitor for the Shadow Paper Trading system.

Supports one-shot execution, continuous background daemon, and performance reporting.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Configure Windows console for UTF-8 output
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Ensure package root is in sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.performance_analytics import PerformanceAnalytics
from bot.settlement_monitor import SettlementMonitor
from bot.shadow_engine import EngineConfig, ShadowEngine, candidates_from_scan_results
from bot.shadow_storage import ShadowStorage
from bot.tweet_market_scanner import TweetMarketScanner
from bot.xtracker_client import XTrackerClient


def print_report(storage: ShadowStorage) -> None:
    analytics = PerformanceAnalytics(storage)
    metrics = analytics.evaluate()
    account = storage.get_account_summary()

    print("\n" + "=" * 70)
    print(" 🎯 NICHERADAR SHADOW PAPER TRADING DASHBOARD")
    print("=" * 70)
    print(f" Timestamp (UTC)     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Initial Bankroll    : ${account.initial_bankroll:,.2f} USDC")
    print(f" Cash Balance        : ${account.cash_balance:,.2f} USDC")
    print(f" Locked Margin       : ${account.locked_collateral:,.2f} USDC")
    print(f" Total Equity        : ${account.total_equity:,.2f} USDC")
    print(f" Net Realized PnL    : ${metrics.total_realized_pnl:+,.2f} USDC")
    print("-" * 70)
    print(f" Total Orders Placed : {metrics.total_orders_placed}")
    print(f" Resting Limit (OPEN): {metrics.open_orders_count}")
    print(f" Active Filled Pos   : {metrics.filled_positions_count}")
    print(f" Settled Trades (N)  : {metrics.settled_trades_count}")
    print(f" Execution Fill Rate : {metrics.fill_rate_pct:.1f}%")
    print(f" Win Rate            : {metrics.win_rate_pct:.1f}% ({metrics.winning_trades}W / {metrics.losing_trades}L)")
    print(f" Profit Factor       : {metrics.profit_factor if metrics.profit_factor is not None else 'N/A'}")
    print(f" Max Drawdown        : ${metrics.max_drawdown_usdc:,.2f} ({metrics.max_drawdown_pct:.1f}%)")
    print("-" * 70)
    print(" 📊 PROBABILITY CALIBRATION (Brier Score)")
    if metrics.model_brier_score is not None:
        print(f" Model Brier Score   : {metrics.model_brier_score:.4f} (lower is better)")
        print(f" Market Brier Score  : {metrics.market_brier_score:.4f}")
        print(f" Brier Skill Score   : {metrics.brier_skill_score:+.4f} ({'Edge Confirmed' if (metrics.brier_skill_score or 0) > 0 else 'No Advantage'})")
    else:
        print(" (Requires settled trades with model probability logs to compute)")
    print("-" * 70)
    print(f" 🚦 PHASE-2 LIVE TRADING GATE: {'[ PASSED ]' if metrics.phase2_ready else '[ PENDING ]'}")
    for gate in metrics.passed_gates:
        print(f"   [PASS] {gate}")
    for gate in metrics.failed_gates:
        print(f"   [FAIL] {gate}")
    print("=" * 70 + "\n")

    # Show recent active orders
    open_orders = storage.get_open_orders()
    if open_orders:
        print(" Active Resting Limit Orders (OPEN):")
        print(f" {'ID':<14} {'Event / Bracket':<28} {'Side':<8} {'Price':<7} {'Shares':<8} {'Cost':<9}")
        print(" " + "-" * 68)
        for o in open_orders[:5]:
            desc = f"{o.event_slug[:16]} [{o.bracket_name}]"
            print(f" {o.order_id:<14} {desc:<28} {o.side:<8} {o.limit_price:<7.3f} {o.size_shares:<8.1f} ${o.cost_usdc:<8.2f}")
        if len(open_orders) > 5:
            print(f" ... and {len(open_orders) - 5} more.")
        print()

    filled_orders = storage.get_filled_orders()
    if filled_orders:
        print(" Active Filled Positions (Awaiting Settlement):")
        print(f" {'ID':<14} {'Event / Bracket':<28} {'Side':<8} {'Fill Px':<8} {'Shares':<8} {'Cost':<9}")
        print(" " + "-" * 68)
        for o in filled_orders[:5]:
            desc = f"{o.event_slug[:16]} [{o.bracket_name}]"
            p = o.fill_price or o.limit_price
            print(f" {o.order_id:<14} {desc:<28} {o.side:<8} {p:<8.3f} {o.size_shares:<8.1f} ${o.cost_usdc:<8.2f}")
        if len(filled_orders) > 5:
            print(f" ... and {len(filled_orders) - 5} more.")
        print()


def run_one_cycle(
    storage: ShadowStorage,
    engine: ShadowEngine,
    monitor: SettlementMonitor,
    scanner: TweetMarketScanner,
    handles: list[str] | None = None,
    min_edge: float = 0.05,
) -> None:
    target_names = ", ".join(handles) if handles else "default targets"
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Scanning Polymarket Social Markets for: {target_names}...")
    scan_results = scanner.scan_and_evaluate(handles=handles)
    candidates = candidates_from_scan_results(scan_results, min_edge=min_edge)
    print(f"Found {len(candidates)} trade candidates with positive edge >= {min_edge:.1%}.")

    # 1. Place orders
    new_orders = engine.evaluate_and_place_orders(candidates)
    if new_orders:
        print(f"Placed {len(new_orders)} new shadow limit orders:")
        for o in new_orders:
            print(f"  + [{o.side}] {o.event_slug} ({o.bracket_name}) @ {o.limit_price:.3f}, {o.size_shares} shares (${o.cost_usdc:.2f})")
    else:
        print("No new orders placed (either no edge, below min size, or already active).")

    # 2. Check fills
    print("Checking fill conditions against Polymarket orderbooks & trades...")
    fills = engine.check_and_update_fills()
    if fills:
        print(f"🎉 {len(fills)} orders FILLED:")
        for f in fills:
            print(f"  * FILLED: {f.order_id} ({f.bracket_name}) @ {f.fill_price:.3f}")
    else:
        print("No new fills triggered.")

    # 3. Check settlements
    print("Syncing market settlements from Gamma API...")
    settlements = monitor.check_and_sync_settlements()
    if settlements:
        print(f"🏁 {len(settlements)} orders SETTLED:")
        for s in settlements:
            print(f"  * SETTLED: {s.order_id} ({s.bracket_name}) -> Terminal: {s.terminal_price:.1f}, PnL: ${s.realized_pnl:+.2f}")
    else:
        print("No new market resolutions.")


def main() -> None:
    parser = argparse.ArgumentParser(description="NicheRadar Shadow Paper Trading Engine")
    parser.add_argument("--db-path", default="data/shadow_trading.sqlite", help="SQLite database path")
    parser.add_argument("--once", action="store_true", help="Run a single scan & fill cycle then exit")
    parser.add_argument("--report", action="store_true", help="Display performance report and exit")
    parser.add_argument("--settle-only", action="store_true", help="Only run settlement sync")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a background daemon")
    parser.add_argument("--scan-interval", type=int, default=60, help="Seconds between market scans (daemon mode)")
    parser.add_argument("--fill-interval", type=int, default=30, help="Seconds between fill checks (daemon mode)")
    parser.add_argument("--settle-interval", type=int, default=300, help="Seconds between settlement syncs (daemon mode)")
    parser.add_argument("--min-edge", type=float, default=0.05, help="Minimum net edge required to place orders")
    parser.add_argument("--fill-mode", default="MAKER_STRICT", choices=["MAKER_STRICT", "MAKER_TOUCH", "TAKER"], help="Fill simulation realism mode")
    parser.add_argument(
        "--handles",
        nargs="+",
        default=["elonmusk", "realDonaldTrump", "cz_binance", "WhiteHouse"],
        help="Target handles to monitor (default: elonmusk realDonaldTrump cz_binance WhiteHouse)",
    )

    args = parser.parse_args()

    storage = ShadowStorage(args.db_path)
    if args.report:
        print_report(storage)
        return

    monitor = SettlementMonitor(storage)
    if args.settle_only:
        print("Running settlement sync...")
        res = monitor.check_and_sync_settlements()
        print(f"Settled {len(res)} orders.")
        print_report(storage)
        return

    xtracker = XTrackerClient()
    scanner = TweetMarketScanner(xtracker_client=xtracker)
    cfg = EngineConfig(min_edge=args.min_edge, default_fill_mode=args.fill_mode)
    engine = ShadowEngine(storage=storage, config=cfg)

    if args.once or not args.daemon:
        run_one_cycle(storage, engine, monitor, scanner, handles=args.handles, min_edge=args.min_edge)
        print_report(storage)
        return

    # Daemon loop
    targets_str = ", ".join(args.handles)
    print(f"Starting NicheRadar Shadow Trading Daemon for targets: {targets_str} (Ctrl+C to stop)...")
    print(f"Scan Interval: {args.scan_interval}s | Fill Interval: {args.fill_interval}s | Settle Interval: {args.settle_interval}s")
    last_scan = 0.0
    last_fill = 0.0
    last_settle = 0.0

    try:
        while True:
            now = time.monotonic()
            if now - last_scan >= args.scan_interval:
                print(f"\n--- [CYCLE SCAN] {datetime.now(timezone.utc).isoformat()} ---")
                try:
                    scan_results = scanner.scan_and_evaluate(handles=args.handles)
                    candidates = candidates_from_scan_results(scan_results, min_edge=args.min_edge)
                    new_orders = engine.evaluate_and_place_orders(candidates)
                    if new_orders:
                        print(f"Placed {len(new_orders)} new shadow orders.")
                except Exception as exc:
                    print(f"⚠️ [SCAN WARNING] {type(exc).__name__}: {exc} (will retry next cycle)")
                last_scan = now

            if now - last_fill >= args.fill_interval:
                try:
                    fills = engine.check_and_update_fills()
                    if fills:
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {len(fills)} orders FILLED.")
                except Exception as exc:
                    print(f"⚠️ [FILL WARNING] {type(exc).__name__}: {exc}")
                last_fill = now

            if now - last_settle >= args.settle_interval:
                try:
                    settled = monitor.check_and_sync_settlements()
                    if settled:
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {len(settled)} orders SETTLED.")
                    print_report(storage)
                except Exception as exc:
                    print(f"⚠️ [SETTLE WARNING] {type(exc).__name__}: {exc}")
                last_settle = now

            time.sleep(2)
    except KeyboardInterrupt:
        print("\nDaemon stopped by user.")
        print_report(storage)


if __name__ == "__main__":
    main()
