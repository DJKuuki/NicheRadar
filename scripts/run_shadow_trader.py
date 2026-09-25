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


def print_report(storage: ShadowStorage, mark_to_market: bool = False) -> None:
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
    print(f" Equity (at cost)    : ${account.total_equity:,.2f} USDC")
    print(f" Net Realized PnL    : ${metrics.total_realized_pnl:+,.2f} USDC")
    if mark_to_market:
        positions = storage.get_filled_orders()
        values = ShadowEngine(storage).liquidation_values()
        unrealized = sum(values[o.order_id] - o.cost_usdc for o in positions if o.order_id in values)
        print(f" Bid-depth coverage : {len(values)}/{len(positions)} positions (before fees)")
        if len(values) == len(positions):
            print(f" Unrealized PnL      : ${unrealized:+,.2f}")
            print(f" Equity (bid depth)  : ${account.total_equity + unrealized:,.2f}")
        else:
            print(f" Priced subset PnL   : ${unrealized:+,.2f}; total market equity unavailable")
    else:
        print(" Unrealized PnL      : Not valued; use --report --mark-to-market")
    print("-" * 70)
    print(f" Total Orders Placed : {metrics.total_orders_placed}")
    print(f" Resting Limit (OPEN): {metrics.open_orders_count}")
    print(f" Active Filled Pos   : {metrics.filled_positions_count}")
    print(f" Settled Trades (N)  : {metrics.settled_trades_count}")
    print(f" Execution Fill Rate : {metrics.fill_rate_pct:.1f}%")
    print(f" Win Rate            : {metrics.win_rate_pct:.1f}% ({metrics.winning_trades}W / {metrics.losing_trades}L)")
    print(f" Profit Factor       : {metrics.profit_factor if metrics.profit_factor is not None else 'N/A'}")
    print(f" Realized Drawdown   : ${metrics.max_drawdown_usdc:,.2f} ({metrics.max_drawdown_pct:.1f}%)")
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
    versions = sorted({analytics.order_version(o) for o in storage.get_all_orders()})
    for version in versions:
        cohort = analytics.evaluate(strategy_version=version)
        print(f" Strategy {version}: {cohort.settled_trades_count} settled, "
              f"{cohort.winning_trades}W/{cohort.losing_trades}L, "
              f"realized ${cohort.total_realized_pnl:+.2f}, "
              f"{cohort.filled_positions_count} awaiting settlement")

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
    min_price: float = 0.10,
    max_price: float = 0.65,
    max_tenor_days: float = 8.0,
    max_stale_hours: float = 12.0,
) -> None:
    # 1. Sync settlements first (releases cash & margin from resolved events)
    print("Syncing market settlements from Gamma API...")
    settlements = monitor.check_and_sync_settlements()
    if settlements:
        print(f"🏁 {len(settlements)} orders SETTLED:")
        for s in settlements:
            print(f"  * SETTLED: {s.order_id} ({s.bracket_name}) -> Terminal: {s.terminal_price:.1f}, PnL: ${s.realized_pnl:+.2f}")
    else:
        print("No new market resolutions.")

    # 2. Cancel stale resting limit orders (releases cash, prevents adverse selection)
    print("Checking and canceling stale resting limit orders...")
    stale_cancelled = engine.cancel_stale_orders(max_age_hours=max_stale_hours)
    if stale_cancelled:
        print(f"🧹 Canceled {len(stale_cancelled)} stale resting limit orders (margin refunded to cash):")
        for sc in stale_cancelled:
            print(f"  - CANCELED: {sc.order_id} ({sc.bracket_name}) @ {sc.limit_price:.3f}, refunded ${sc.cost_usdc:.2f}")
    else:
        print("No stale resting orders found.")

    # 3. Scan markets with tenor filtering
    target_names = ", ".join(handles) if handles else "default targets"
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Scanning Polymarket Social Markets (tenor <= {max_tenor_days:.1f}d) for: {target_names}...")
    scan_results = scanner.scan_and_evaluate(handles=handles, max_tenor_days=max_tenor_days)
    
    # Generate all candidate evaluations for hysteresis revalidation (unconstrained across all brackets)
    all_candidates = candidates_from_scan_results(
        scan_results,
        min_edge=0.01,
        min_entry_price=min_price,
        max_entry_price=max_price,
        top_k_per_event=None,
    )
    
    # Generate Top-1 candidate per event for new order placement
    candidates = candidates_from_scan_results(
        scan_results,
        min_edge=min_edge,
        min_entry_price=min_price,
        max_entry_price=max_price,
        top_k_per_event=1,
    )
    print(f"Found {len(candidates)} trade candidates (safety corridor {min_price:.2f}-{max_price:.2f}, Top-1 per event, edge >= {min_edge:.1%}).")

    # 4. Revalidate resting orders using full candidate pool with hysteresis, then place new Top-1 orders
    engine.revalidate_open_orders(all_candidates)
    new_orders = engine.evaluate_and_place_orders(candidates)
    if new_orders:
        print(f"Placed {len(new_orders)} new shadow limit orders:")
        for o in new_orders:
            print(f"  + [{o.side}] {o.event_slug} ({o.bracket_name}) @ {o.limit_price:.3f}, {o.size_shares} shares (${o.cost_usdc:.2f})")
    else:
        print("No new orders placed (either no edge, below min size, or already active).")

    # 5. Check fills
    print("Checking fill conditions against Polymarket orderbooks & trades...")
    fills = engine.check_and_update_fills()
    if fills:
        print(f"🎉 {len(fills)} orders FILLED:")
        for f in fills:
            print(f"  * FILLED: {f.order_id} ({f.bracket_name}) @ {f.fill_price:.3f}")
    else:
        print("No new fills triggered.")


def main() -> None:
    parser = argparse.ArgumentParser(description="NicheRadar Shadow Paper Trading Engine")
    parser.add_argument("--db-path", default="data/shadow_trading.sqlite", help="SQLite database path")
    parser.add_argument("--once", action="store_true", help="Run a single scan & fill cycle then exit")
    parser.add_argument("--report", action="store_true", help="Display performance report and exit")
    parser.add_argument("--mark-to-market", action="store_true", help="With --report, value filled positions against executable bid depth")
    parser.add_argument("--settle-only", action="store_true", help="Only run settlement sync")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a background daemon")
    parser.add_argument("--scan-interval", type=int, default=60, help="Seconds between market scans (daemon mode)")
    parser.add_argument("--fill-interval", type=int, default=30, help="Seconds between fill checks (daemon mode)")
    parser.add_argument("--settle-interval", type=int, default=300, help="Seconds between settlement syncs (daemon mode)")
    parser.add_argument("--min-edge", type=float, default=0.05, help="Minimum net edge required to place orders")
    parser.add_argument("--min-price", type=float, default=0.10, help="Minimum entry price (safety corridor, default 0.10)")
    parser.add_argument("--max-price", type=float, default=0.65, help="Maximum entry price (safety corridor, default 0.65)")
    parser.add_argument("--max-tenor-days", type=float, default=8.0, help="Maximum market tenor in days (default 8.0)")
    parser.add_argument("--stale-hours", type=float, default=12.0, help="Hours before an unfilled resting order is canceled (default 12.0)")
    parser.add_argument("--fill-mode", default="MAKER_STRICT", choices=["MAKER_STRICT", "MAKER_TOUCH", "TAKER"], help="Fill simulation realism mode")
    parser.add_argument(
        "--handles",
        nargs="+",
        default=["cz_binance", "realDonaldTrump", "elonmusk", "WhiteHouse"],
        help="Target handles to monitor (default: elonmusk realDonaldTrump cz_binance WhiteHouse)",
    )

    args = parser.parse_args()

    storage = ShadowStorage(args.db_path)
    if args.report:
        print_report(storage, mark_to_market=args.mark_to_market)
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
    cfg = EngineConfig(
        min_edge=args.min_edge,
        min_entry_price=args.min_price,
        max_entry_price=args.max_price,
        max_stale_order_hours=args.stale_hours,
        default_fill_mode=args.fill_mode,
    )
    engine = ShadowEngine(storage=storage, config=cfg)

    if args.once or not args.daemon:
        run_one_cycle(
            storage,
            engine,
            monitor,
            scanner,
            handles=args.handles,
            min_edge=args.min_edge,
            min_price=args.min_price,
            max_price=args.max_price,
            max_tenor_days=args.max_tenor_days,
            max_stale_hours=args.stale_hours,
        )
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
                    # Cancel stale orders before scan to release cash
                    stale = engine.cancel_stale_orders(max_age_hours=args.stale_hours)
                    if stale:
                        print(f"🧹 Canceled {len(stale)} stale resting limit orders (margin refunded to cash).")

                    scan_results = scanner.scan_and_evaluate(handles=args.handles, max_tenor_days=args.max_tenor_days)
                    all_candidates = candidates_from_scan_results(
                        scan_results,
                        min_edge=0.01,
                        min_entry_price=args.min_price,
                        max_entry_price=args.max_price,
                        top_k_per_event=None,
                    )
                    candidates = candidates_from_scan_results(
                        scan_results,
                        min_edge=args.min_edge,
                        min_entry_price=args.min_price,
                        max_entry_price=args.max_price,
                        top_k_per_event=1,
                    )
                    engine.revalidate_open_orders(all_candidates)
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
