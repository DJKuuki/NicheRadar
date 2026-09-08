"""Tweet Count Probability Model - Negative Binomial & Poisson Forecasting.

Calculates exact forward probability distributions across Polymarket bracket markets
based on current cumulative count, remaining window hours, and historical arrival rates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class BracketSpec:
    low: int
    high: int | None  # None indicates open-ended high bracket like "500+"
    name: str
    token_id: str | None = None
    market_slug: str | None = None


@dataclass
class BracketEvaluation:
    bracket: BracketSpec
    fair_prob: float
    market_ask: float | None
    market_bid: float | None
    edge_buy: float | None
    edge_maker: float | None
    recommendation: str
    target_maker_price: float | None = None
    kelly_fraction: float | None = None


class TweetProbabilityModel:
    def __init__(
        self,
        mean_daily_rate: float = 35.0,
        var_daily_rate: float = 120.0,
        min_edge_threshold: float = 0.04,
        kelly_scale: float = 0.25,  # Quarter-Kelly for risk conservation
    ):
        self.mean_daily = max(1.0, mean_daily_rate)
        self.var_daily = max(self.mean_daily * 1.01, var_daily_rate)
        self.min_edge = min_edge_threshold
        self.kelly_scale = kelly_scale

    def update_historical_parameters(self, mean_daily: float, var_daily: float) -> None:
        """Update daily mean and variance from observed data."""
        self.mean_daily = max(1.0, mean_daily)
        self.var_daily = max(self.mean_daily * 1.01, var_daily)

    def forecast_pmf_remaining(
        self, remaining_hours: float, max_x: int = 600
    ) -> list[float]:
        """Compute the Probability Mass Function (PMF) for remaining tweets.

        Uses Negative Binomial if overdispersed, else Poisson.
        """
        if remaining_hours <= 0.001:
            pmf = [0.0] * (max_x + 1)
            pmf[0] = 1.0
            return pmf

        frac_days = remaining_hours / 24.0
        mu_rem = self.mean_daily * frac_days
        var_rem = self.var_daily * frac_days

        if var_rem > mu_rem:
            # Negative Binomial parameterization
            # r = mu^2 / (var - mu), p = mu / var
            r = (mu_rem ** 2) / (var_rem - mu_rem)
            p = mu_rem / var_rem
            return self._negbinom_pmf(r, p, max_x)
        else:
            return self._poisson_pmf(mu_rem, max_x)

    def evaluate_bracket(
        self,
        bracket: BracketSpec,
        current_count: int,
        remaining_hours: float,
        market_ask: float | None = None,
        market_bid: float | None = None,
    ) -> BracketEvaluation:
        """Calculate the exact model probability and trading edge for a specific bracket."""
        max_k = max(600, (bracket.high or 500) + 100)
        pmf = self.forecast_pmf_remaining(remaining_hours, max_x=max_k)

        low = bracket.low
        high = bracket.high

        # Probability that Final Count (current_count + X) falls in [low, high]
        prob = 0.0
        if high is None:
            # Open-ended bracket, e.g. 500+
            need_min = max(0, low - current_count)
            # Sum from need_min to infinity = 1 - sum from 0 to need_min - 1
            if need_min == 0:
                prob = 1.0
            else:
                prob = max(0.0, 1.0 - sum(pmf[:need_min]))
        else:
            # Closed bracket [low, high]
            need_min = max(0, low - current_count)
            need_max = high - current_count
            if need_max >= 0 and need_min <= need_max:
                prob = sum(pmf[need_min : min(len(pmf), need_max + 1)])
            else:
                prob = 0.0

        prob = min(1.0, max(0.0, prob))

        # Edge calculations
        edge_buy = None
        edge_maker = None
        target_maker = None
        rec = "PASS"
        kelly_f = 0.0

        if market_ask is not None and 0.0 < market_ask < 1.0:
            edge_buy = prob - market_ask

        if market_bid is not None and 0.0 < market_bid < 1.0:
            # Maker price set 1 cent above bid, or inside spread
            target_maker = round(market_bid + 0.01, 2)
            if market_ask is not None and target_maker >= market_ask:
                target_maker = round(market_ask - 0.01, 2)
            if target_maker is not None and target_maker > 0:
                edge_maker = prob - target_maker

        # Determine recommendation
        if edge_maker is not None and edge_maker >= self.min_edge:
            rec = "MAKER_BUY"
            # Kelly sizing: f* = (p * b - q) / b where b = (1 - price) / price
            b = (1.0 - target_maker) / (target_maker if target_maker > 0 else 0.01)
            q = 1.0 - prob
            raw_kelly = max(0.0, (prob * b - q) / b) if b > 0 else 0.0
            kelly_f = round(raw_kelly * self.kelly_scale, 4)
        elif edge_buy is not None and edge_buy >= (self.min_edge + 0.02):
            rec = "TAKER_BUY"
            b = (1.0 - market_ask) / (market_ask if market_ask > 0 else 0.01)
            q = 1.0 - prob
            raw_kelly = max(0.0, (prob * b - q) / b) if b > 0 else 0.0
            kelly_f = round(raw_kelly * self.kelly_scale, 4)
        elif market_ask is not None and (market_ask - prob) >= (self.min_edge + 0.05):
            rec = "OVERPRICED"

        return BracketEvaluation(
            bracket=bracket,
            fair_prob=round(prob, 4),
            market_ask=market_ask,
            market_bid=market_bid,
            edge_buy=round(edge_buy, 4) if edge_buy is not None else None,
            edge_maker=round(edge_maker, 4) if edge_maker is not None else None,
            recommendation=rec,
            target_maker_price=target_maker,
            kelly_fraction=kelly_f,
        )

    def evaluate_all_brackets(
        self,
        brackets: Sequence[BracketSpec],
        current_count: int,
        remaining_hours: float,
        market_quotes: dict[str, tuple[float | None, float | None]] | None = None,
    ) -> list[BracketEvaluation]:
        """Evaluate a collection of brackets against market quotes."""
        evals = []
        for b in brackets:
            ask, bid = None, None
            if market_quotes and b.name in market_quotes:
                ask, bid = market_quotes[b.name]
            evals.append(
                self.evaluate_bracket(
                    b,
                    current_count=current_count,
                    remaining_hours=remaining_hours,
                    market_ask=ask,
                    market_bid=bid,
                )
            )
        return evals

    @staticmethod
    def _poisson_pmf(lam: float, max_k: int) -> list[float]:
        pmf = [0.0] * (max_k + 1)
        if lam <= 0:
            pmf[0] = 1.0
            return pmf
        # Log-space accumulation to prevent overflow
        log_lam = math.log(lam)
        for k in range(max_k + 1):
            log_prob = k * log_lam - lam - math.lgamma(k + 1)
            pmf[k] = math.exp(log_prob)
        return pmf

    @staticmethod
    def _negbinom_pmf(r: float, p: float, max_k: int) -> list[float]:
        # P(X = k) = Gamma(k + r)/(k! Gamma(r)) * p^r * (1-p)^k
        pmf = [0.0] * (max_k + 1)
        log_p = math.log(p)
        log_1_p = math.log(1.0 - p) if p < 1.0 else -100.0
        log_gamma_r = math.lgamma(r)

        for k in range(max_k + 1):
            log_comb = math.lgamma(k + r) - (math.lgamma(k + 1) + log_gamma_r)
            log_prob = log_comb + r * log_p + k * log_1_p
            pmf[k] = math.exp(log_prob)
        return pmf
