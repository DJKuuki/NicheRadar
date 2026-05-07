from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    min_days_to_expiry: float = 1.0
    max_days_to_expiry: float = 14.0
    min_volume: float = 500.0
    max_spread: float = 0.12
    fee_buffer: float = 0.02
    uncertainty_buffer: float = 0.03
    min_net_edge: float = 0.01
    shadow_bankroll: float = 1000.0
    shadow_position_risk_pct: float = 0.02
    max_total_risk_pct: float = 0.20
    max_market_risk_pct: float = 0.02
    max_event_type_risk_pct: float = 0.08
    circuit_breaker_loss_pct: float = 0.05
    max_open_shadow_positions: int = 10
