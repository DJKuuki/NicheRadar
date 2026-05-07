from __future__ import annotations

from dataclasses import dataclass
import math

from bot.config import BotConfig
from bot.models import Evidence, ParsedMarket, Signal


@dataclass(frozen=True)
class ModelProfile:
    name: str
    base_logit: float
    negated_action_base_logit: float | None
    evidence_weight: float
    preheat_weight: float
    cadence_weight: float
    partner_weight: float
    time_weight: float
    spread_penalty_weight: float


MODEL_PROFILES: dict[str, ModelProfile] = {
    "music_release": ModelProfile(
        name="music_release",
        base_logit=-4.65,  # Calibrated from -0.35 (historical actual 1%)
        negated_action_base_logit=None,
        evidence_weight=1.15,
        preheat_weight=0.55,
        cadence_weight=0.25,
        partner_weight=0.20,
        time_weight=1.10,
        spread_penalty_weight=0.50,
    ),
    "product_release": ModelProfile(
        name="product_release",
        base_logit=-2.25,
        negated_action_base_logit=None,
        evidence_weight=0.75,
        preheat_weight=0.30,
        cadence_weight=0.15,
        partner_weight=0.55,
        time_weight=0.75,
        spread_penalty_weight=0.65,
    ),
    "ipo_event": ModelProfile(
        name="ipo_event",
        base_logit=-3.06,  # Calibrated from -1.25 (historical actual 4%)
        negated_action_base_logit=0.65,
        evidence_weight=0.95,
        preheat_weight=0.50,
        cadence_weight=0.25,
        partner_weight=0.25,
        time_weight=0.60,
        spread_penalty_weight=0.55,
    ),
    "default_content": ModelProfile(
        name="default_content",
        base_logit=-0.37,  # Calibrated from -0.15 (historical actual 40%)
        negated_action_base_logit=None,
        evidence_weight=1.00,
        preheat_weight=0.45,
        cadence_weight=0.35,
        partner_weight=0.20,
        time_weight=1.00,
        spread_penalty_weight=0.50,
    ),
}


def build_signal(parsed: ParsedMarket, evidence: Evidence, config: BotConfig) -> Signal:
    market = parsed.market
    profile = _select_profile(parsed)
    time_bonus = _time_score(parsed.days_to_expiry) * profile.time_weight
    market_penalty = max(0.0, market.spread - 0.06) * profile.spread_penalty_weight
    profile_evidence_score = _profile_evidence_score(evidence, profile)
    evidence_effect = -profile_evidence_score if parsed.action.startswith("not_") else profile_evidence_score
    evidence_effect *= profile.evidence_weight
    model_logit = _base_logit(parsed, profile) + evidence_effect + time_bonus - market_penalty
    p_model = _sigmoid(model_logit)
    p_mid = market.mid_probability
    total_buffer = config.fee_buffer + config.uncertainty_buffer

    yes_edge = p_model - p_mid
    side = "BUY_YES" if yes_edge >= 0 else "BUY_NO"
    model_price = p_model if side == "BUY_YES" else 1 - p_model
    market_price = market.mid_for_side(side)
    edge = model_price - market_price
    net_edge = edge - total_buffer
    max_entry_price = model_price - total_buffer

    reasons = [
        f"model_profile={profile.name}",
        f"event_type={parsed.event_type}",
        f"platform={parsed.platform}",
        f"action={parsed.action}",
        f"profile_evidence_score={profile_evidence_score:.3f}",
        f"evidence_effect={evidence_effect:.3f}",
        f"time_bonus={time_bonus:.3f}",
        f"yes_mid={p_mid:.4f}",
        f"side_market_mid={market_price:.4f}",
        f"market_spread={market.spread_for_side(side):.3f}",
        *evidence.reasons,
    ]
    return Signal(
        market_id=market.market_id,
        side=side,
        p_model=round(p_model, 4),
        p_mid=round(p_mid, 4),
        edge=round(edge, 4),
        net_edge=round(net_edge, 4),
        max_entry_price=round(max(0.01, max_entry_price), 4),
        confidence=evidence.confidence,
        reasons=reasons,
    )


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _base_logit(parsed: ParsedMarket, profile: ModelProfile) -> float:
    if parsed.action.startswith("not_") and profile.negated_action_base_logit is not None:
        return profile.negated_action_base_logit
    return profile.base_logit


def _select_profile(parsed: ParsedMarket) -> ModelProfile:
    if parsed.event_type == "ipo_event":
        return MODEL_PROFILES["ipo_event"]
    if parsed.event_type == "content_release" and _is_product_release(parsed):
        return MODEL_PROFILES["product_release"]
    if parsed.event_type == "content_release" and _is_music_release(parsed):
        return MODEL_PROFILES["music_release"]
    return MODEL_PROFILES["default_content"]


def _profile_evidence_score(evidence: Evidence, profile: ModelProfile) -> float:
    components = (evidence.preheat_score, evidence.cadence_score, evidence.partner_score)
    if any(component is None for component in components):
        return evidence.score
    assert evidence.preheat_score is not None
    assert evidence.cadence_score is not None
    assert evidence.partner_score is not None
    return (
        evidence.preheat_score * profile.preheat_weight
        + evidence.cadence_score * profile.cadence_weight
        + evidence.partner_score * profile.partner_weight
    )


def _is_product_release(parsed: ParsedMarket) -> bool:
    if parsed.platform in {"apple", "tesla"}:
        return True
    text = f"{parsed.market.title} {parsed.market.description}".lower()
    return any(keyword in text for keyword in ("macbook", "optimus", "hardware", "device"))


def _is_music_release(parsed: ParsedMarket) -> bool:
    if parsed.platform == "streaming":
        return True
    text = f"{parsed.market.title} {parsed.market.description}".lower()
    return any(keyword in text for keyword in ("album", "song", "single", "music", "spotify", "apple music"))


def _time_score(days_to_expiry: float) -> float:
    if days_to_expiry < 1:
        return -0.25
    if days_to_expiry <= 3:
        return 0.18
    if days_to_expiry <= 7:
        return 0.08
    return -0.02
