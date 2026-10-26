"""Polymarket Liquidity Rewards, Maker Rebates & Holding Rewards Analyzer.

Extracts incentive parameters from Gamma API market payloads (`rewardsMinSize`,
`rewardsMaxSpread`, `holdingRewardsEnabled`, `feeSchedule`) and computes:
1. Liquidity Reward eligibility & quadratic proximity score S(s, v) = ((v - s) / v)^2
2. Maker fee rebate estimation on fee-enabled markets (`rebateRate`)
3. Order size optimization ("reward bump") when an order is slightly below `rewardsMinSize`
   and bumping to `rewardsMinSize` stays within risk budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketRewardSpec:
    rewards_min_size: float
    rewards_max_spread_cents: float
    holding_rewards_enabled: bool
    fees_enabled: bool
    fee_type: str
    taker_fee_rate: float
    fee_exponent: float
    maker_rebate_rate: float

    @property
    def max_spread_prob(self) -> float:
        """Maximum allowed distance from midpoint in probability units (0.00 to 1.00)."""
        return self.rewards_max_spread_cents / 100.0

    @property
    def has_liquidity_rewards(self) -> bool:
        return self.rewards_min_size > 0 and self.rewards_max_spread_cents > 0


@dataclass(frozen=True)
class OrderRewardEvaluation:
    qualifies_for_liquidity_reward: bool
    distance_from_mid: float
    max_spread_prob: float
    quadratic_score_factor: float  # ((v - s) / v)^2 in [0.0, 1.0]
    weighted_reward_units: float   # size_shares * quadratic_score_factor
    maker_rebate_usdc_on_fill: float
    holding_rewards_eligible: bool
    recommended_size_shares: float
    size_bumped_for_reward: bool


class RewardsAnalyzer:
    """Evaluates Polymarket maker incentives and optimizes order sizing for reward capture."""

    @staticmethod
    def extract_reward_spec(market: dict[str, Any]) -> MarketRewardSpec:
        """Parse reward and fee schedule fields from a Gamma API market dictionary."""
        def _flt(val: Any, default: float = 0.0) -> float:
            try:
                f = float(val)
                return f if math.isfinite(f) else default
            except (TypeError, ValueError):
                return default

        min_size = _flt(market.get("rewardsMinSize"), 0.0)
        max_spread_cents = _flt(market.get("rewardsMaxSpread"), 0.0)
        holding_enabled = bool(market.get("holdingRewardsEnabled", False))
        fees_enabled = bool(market.get("feesEnabled", False))
        fee_type = str(market.get("feeType") or "zero_fees")

        sched = market.get("feeSchedule")
        if not isinstance(sched, dict):
            sched = {}
        taker_rate = _flt(sched.get("rate"), 0.0)
        exponent = _flt(sched.get("exponent"), 1.0)
        rebate_rate = _flt(sched.get("rebateRate"), 0.0)

        return MarketRewardSpec(
            rewards_min_size=min_size,
            rewards_max_spread_cents=max_spread_cents,
            holding_rewards_enabled=holding_enabled,
            fees_enabled=fees_enabled,
            fee_type=fee_type,
            taker_fee_rate=taker_rate,
            fee_exponent=exponent,
            maker_rebate_rate=rebate_rate,
        )

    @classmethod
    def calculate_taker_fee_usdc(
        cls, price: float, size_shares: float, spec: MarketRewardSpec
    ) -> float:
        """Calculate Polymarket taker fee in USDC given contract price and share count.

        On Polymarket, dynamic taker fee scales with p * (1 - p) and `spec.taker_fee_rate`.
        """
        if not spec.fees_enabled or spec.taker_fee_rate <= 0 or not (0.0 < price < 1.0):
            return 0.0
        notional = price * size_shares
        # Effective fee rate peaks near p=0.50 and tapers near 0 or 1
        variance_factor = (4.0 * price * (1.0 - price)) ** max(0.0, spec.fee_exponent)
        return round(notional * spec.taker_fee_rate * variance_factor, 4)

    @classmethod
    def evaluate_maker_order(
        cls,
        limit_price: float,
        size_shares: float,
        quote_bid: float,
        quote_ask: float,
        spec: MarketRewardSpec,
        max_cost_usdc: float | None = None,
    ) -> OrderRewardEvaluation:
        """Evaluate a passive maker order's Liquidity Reward score and Maker Rebate."""
        midpoint = 0.5 * (quote_bid + quote_ask)
        dist = abs(limit_price - midpoint)
        v = spec.max_spread_prob

        rec_shares = size_shares
        bumped = False
        if (
            spec.has_liquidity_rewards
            and dist < v
            and 0 < size_shares < spec.rewards_min_size
            and max_cost_usdc is not None
        ):
            needed_cost = spec.rewards_min_size * limit_price
            if needed_cost <= max_cost_usdc:
                rec_shares = spec.rewards_min_size
                bumped = True

        within_spread = spec.has_liquidity_rewards and (dist < v)
        meets_size = rec_shares >= spec.rewards_min_size if spec.has_liquidity_rewards else False
        qualifies = within_spread and meets_size

        if within_spread and v > 0:
            q_score = ((v - dist) / v) ** 2
        else:
            q_score = 0.0

        weighted_units = (rec_shares * q_score) if qualifies else 0.0

        taker_fee = cls.calculate_taker_fee_usdc(limit_price, rec_shares, spec)
        maker_rebate = round(taker_fee * spec.maker_rebate_rate, 4) if spec.maker_rebate_rate > 0 else 0.0

        return OrderRewardEvaluation(
            qualifies_for_liquidity_reward=qualifies,
            distance_from_mid=round(dist, 4),
            max_spread_prob=round(v, 4),
            quadratic_score_factor=round(q_score, 4),
            weighted_reward_units=round(weighted_units, 2),
            maker_rebate_usdc_on_fill=maker_rebate,
            holding_rewards_eligible=spec.holding_rewards_enabled,
            recommended_size_shares=round(rec_shares, 2),
            size_bumped_for_reward=bumped,
        )
