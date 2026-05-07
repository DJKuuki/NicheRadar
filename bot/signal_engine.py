from __future__ import annotations

# NOTE: debate_models is imported lazily inside build_debate_signal to avoid
# circular imports and to keep this module usable without the debate subsystem.

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


# ---------------------------------------------------------------------------
# AI辩论信号构建（替代启发式logit模型）
# ---------------------------------------------------------------------------

def build_debate_signal(
    debate_result,          # DebateResult（懒导入避免循环依赖）
    parsed: ParsedMarket,
    config: "BotConfig",
) -> Signal:
    """
    将AI辩论的Research Manager决策（DebateResult）转化为标准Signal对象。

    p_yes_estimate来自AI对市场分析后给出的结算概率估计，
    用它替代原来硬编码logit模型计算的p_model。
    其余Edge/风控逻辑保持与build_signal()完全一致。
    """
    market = parsed.market
    p_model = debate_result.p_yes_estimate          # AI估计的YES结算概率
    p_mid = market.mid_probability                  # 市场当前中间价
    total_buffer = config.fee_buffer + config.uncertainty_buffer

    yes_edge = p_model - p_mid
    side = "BUY_YES" if yes_edge >= 0 else "BUY_NO"
    model_price = p_model if side == "BUY_YES" else 1 - p_model
    market_price = market.mid_for_side(side)
    edge = model_price - market_price
    net_edge = edge - total_buffer
    max_entry_price = model_price - total_buffer

    reasons = [
        f"signal_source=debate",
        f"debate_id={debate_result.debate_id}",
        f"debate_direction={debate_result.direction}",
        f"debate_confidence={debate_result.confidence:.2f}",
        f"ai_p_yes={p_model:.4f}",
        f"market_p_yes={p_mid:.4f}",
        f"yes_edge={yes_edge:.4f}",
        f"rounds_completed={debate_result.rounds_completed}",
        f"judge_iterations={debate_result.judge_iterations}",
        # 截断推理文本至160字符，防止reasons字段过长
        f"reasoning={debate_result.reasoning[:160].replace(chr(10), ' ')}",
    ]

    return Signal(
        market_id=market.market_id,
        side=side,
        p_model=round(p_model, 4),
        p_mid=round(p_mid, 4),
        edge=round(edge, 4),
        net_edge=round(net_edge, 4),
        max_entry_price=round(max(0.01, max_entry_price), 4),
        confidence=debate_result.confidence,
        reasons=reasons,
    )


def is_debate_signal_tradeable(debate_result, config: "BotConfig") -> tuple[bool, list[str]]:
    """
    快速检查AI辩论结果是否值得进入完整风控/shadow流程。
    在调用build_debate_signal之前使用，过滤掉明显不值得交易的情况。

    返回 (allowed: bool, reasons: list[str])
    """
    reasons = []

    if not debate_result.is_valid:
        return False, ["debate_result_invalid"]

    if debate_result.direction == "SKIP":
        return False, ["debate_direction=SKIP"]

    if debate_result.confidence < 0.55:
        reasons.append(f"debate_confidence_too_low={debate_result.confidence:.2f}")
        return False, reasons

    return True, []
