"""Performance Analytics & Statistical Calibration Gatekeeper.

Calculates empirical trading metrics (Win Rate, Profit Factor, Drawdown)
and probabilistic calibration (Brier Score vs Market Baseline) to evaluate
Phase-2 readiness for real capital.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from bot.shadow_storage import ShadowOrder, ShadowStorage


@dataclass
class PerformanceMetrics:
    total_orders_placed: int
    open_orders_count: int
    filled_positions_count: int
    settled_trades_count: int
    fill_rate_pct: float
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    total_realized_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: Optional[float]
    max_drawdown_usdc: float
    max_drawdown_pct: float
    model_brier_score: Optional[float]
    market_brier_score: Optional[float]
    brier_skill_score: Optional[float]
    phase2_ready: bool
    passed_gates: list[str]
    failed_gates: list[str]


class PerformanceAnalytics:
    def __init__(self, storage: ShadowStorage) -> None:
        self.storage = storage

    def evaluate(self) -> PerformanceMetrics:
        all_orders = self.storage.get_all_orders(limit=1000)
        account = self.storage.get_account_summary()

        total_orders = len(all_orders)
        open_orders = [o for o in all_orders if o.status == "OPEN"]
        filled_orders = [o for o in all_orders if o.status == "FILLED"]
        settled_orders = [o for o in all_orders if o.status == "SETTLED"]

        # Fill rate: orders that were filled (or settled after being filled) / total orders
        actually_filled_count = len(filled_orders) + len(settled_orders)
        fill_rate = (actually_filled_count / total_orders * 100.0) if total_orders > 0 else 0.0

        # PnL & Win rate
        winners = [o for o in settled_orders if (o.realized_pnl or 0.0) > 0]
        losers = [o for o in settled_orders if (o.realized_pnl or 0.0) < 0]
        settled_count = len(settled_orders)
        win_rate = (len(winners) / settled_count * 100.0) if settled_count > 0 else 0.0

        gross_profit = sum((o.realized_pnl or 0.0) for o in winners)
        gross_loss = abs(sum((o.realized_pnl or 0.0) for o in losers))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else None)

        total_pnl = sum((o.realized_pnl or 0.0) for o in settled_orders)

        # Max drawdown calculation
        max_dd_usdc = 0.0
        max_dd_pct = 0.0
        if settled_orders:
            # Sort settled orders chronologically
            sorted_settled = sorted(settled_orders, key=lambda x: x.settled_at_utc or "")
            peak = 0.0
            running_pnl = 0.0
            for o in sorted_settled:
                running_pnl += (o.realized_pnl or 0.0)
                if running_pnl > peak:
                    peak = running_pnl
                dd = peak - running_pnl
                if dd > max_dd_usdc:
                    max_dd_usdc = dd
            bankroll = account.initial_bankroll
            max_dd_pct = (max_dd_usdc / bankroll * 100.0) if bankroll > 0 else 0.0

        # Brier Score Calibration
        model_brier = None
        market_brier = None
        skill_score = None

        calibrated_pairs: list[tuple[float, float, float]] = []  # (p_model, p_market, y_actual)
        for o in settled_orders:
            if not o.metadata_json:
                continue
            try:
                meta = json.loads(o.metadata_json)
                p_model = meta.get("model_prob")
                p_mkt = meta.get("market_prob")
                if p_model is not None and p_mkt is not None and o.settled_price is not None:
                    # For BUY_YES, y=1 if YES won (settled_price=1.0).
                    # For BUY_NO, p_model was prob of NO, so y=1 if NO won.
                    y_actual = 1.0 if o.settled_price == 1.0 else 0.0
                    calibrated_pairs.append((float(p_model), float(p_mkt), y_actual))
            except Exception:
                continue

        if calibrated_pairs:
            n = len(calibrated_pairs)
            model_brier = round(sum((p_m - y) ** 2 for p_m, _, y in calibrated_pairs) / n, 4)
            market_brier = round(sum((p_k - y) ** 2 for _, p_k, y in calibrated_pairs) / n, 4)
            if market_brier > 0:
                skill_score = round(1.0 - (model_brier / market_brier), 4)

        # Phase-2 Gate Validation
        passed_gates: list[str] = []
        failed_gates: list[str] = []

        # Gate 1: Sample Size (N >= 20)
        if settled_count >= 20:
            passed_gates.append(f"Sample Size (N={settled_count} >= 20)")
        else:
            failed_gates.append(f"Sample Size (N={settled_count} < 20)")

        # Gate 2: Net Realized Profit (> $0)
        if total_pnl > 0:
            passed_gates.append(f"Net Profit (${total_pnl:+.2f} > $0)")
        else:
            failed_gates.append(f"Net Profit (${total_pnl:+.2f} <= $0)")

        # Gate 3: Profit Factor (>= 1.25)
        if profit_factor is not None and profit_factor >= 1.25:
            passed_gates.append(f"Profit Factor ({profit_factor:.2f} >= 1.25)")
        else:
            pf_str = f"{profit_factor:.2f}" if profit_factor is not None else "N/A"
            failed_gates.append(f"Profit Factor ({pf_str} < 1.25)")

        # Gate 4: Brier Skill Score (> 0, outperforms market consensus)
        if skill_score is not None and skill_score > 0:
            passed_gates.append(f"Brier Calibration Advantage (BSS={skill_score:+.4f} > 0)")
        else:
            bss_str = f"{skill_score:+.4f}" if skill_score is not None else "N/A"
            failed_gates.append(f"Brier Calibration Advantage (BSS={bss_str} <= 0)")

        # Gate 5: Max Drawdown (< 20%)
        if max_dd_pct < 20.0:
            passed_gates.append(f"Max Drawdown ({max_dd_pct:.1f}% < 20.0%)")
        else:
            failed_gates.append(f"Max Drawdown ({max_dd_pct:.1f}% >= 20.0%)")

        phase2_ready = len(failed_gates) == 0

        return PerformanceMetrics(
            total_orders_placed=total_orders,
            open_orders_count=len(open_orders),
            filled_positions_count=len(filled_orders),
            settled_trades_count=settled_count,
            fill_rate_pct=round(fill_rate, 2),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate_pct=round(win_rate, 2),
            total_realized_pnl=round(total_pnl, 4),
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            profit_factor=profit_factor,
            max_drawdown_usdc=round(max_dd_usdc, 4),
            max_drawdown_pct=round(max_dd_pct, 2),
            model_brier_score=model_brier,
            market_brier_score=market_brier,
            brier_skill_score=skill_score,
            phase2_ready=phase2_ready,
            passed_gates=passed_gates,
            failed_gates=failed_gates,
        )
