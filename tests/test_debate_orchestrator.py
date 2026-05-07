"""
tests/test_debate_orchestrator.py

测试辩论调度器的核心解析逻辑，使用mock回复验证，无需真实AI交互。
"""
from __future__ import annotations

import pytest
from bot.debate_models import (
    DebatePacket,
    DebateResult,
    parse_direction,
)
from bot.debate_orchestrator import (
    _parse_researcher_round,
    _parse_judge_critique,
    _parse_research_manager,
)
from bot.debate_prompts import build_full_debate_prompt


# ---------------------------------------------------------------------------
# 辅助：构建最小化测试用DebatePacket
# ---------------------------------------------------------------------------

def _make_packet(task: str = "researcher_round") -> DebatePacket:
    return DebatePacket(
        debate_id="test-debate-001",
        market_slug="will-apple-release-iphone-17-by-sep",
        market_title="Will Apple release iPhone 17 by September 30, 2025?",
        market_description=(
            "Resolves YES if Apple officially announces and begins shipping iPhone 17 "
            "by September 30, 2025. Announcement must appear on apple.com."
        ),
        settlement_date="2025-09-30",
        days_to_expiry=45.0,
        current_yes_price=0.72,
        current_no_price=0.28,
        spread=0.04,
        event_type="product_release",
        platform="apple",
        evidence_text=(
            "[Evidence Summary]\n"
            "Event Type: product_release\n"
            "Subject: apple iphone 17\n"
            "Preheat Score (recent buzz): 0.85\n"
            "Recent News Entries (30d): 12\n"
            "Keyword Hits (30d): 8\n"
            "[Evidence Reasons / Raw Signals]\n"
            "  - rss_hit: Apple iPhone 17 Pro leak confirms titanium frame\n"
            "  - rss_hit: Apple September event reportedly scheduled for Sept 9\n"
        ),
        evidence_score=0.73,
        task=task,
    )


# ---------------------------------------------------------------------------
# 测试 parse_direction
# ---------------------------------------------------------------------------

class TestParseDirection:
    def test_buy_yes_variants(self):
        assert parse_direction("BUY_YES") == "BUY_YES"
        assert parse_direction("buy yes") == "BUY_YES"
        assert parse_direction("YES") == "BUY_YES"

    def test_buy_no_variants(self):
        assert parse_direction("BUY_NO") == "BUY_NO"
        assert parse_direction("buy no") == "BUY_NO"
        assert parse_direction("no") == "BUY_NO"

    def test_skip_variants(self):
        assert parse_direction("SKIP") == "SKIP"
        assert parse_direction("hold") == "SKIP"
        assert parse_direction("pass") == "SKIP"

    def test_unknown_defaults_to_skip(self):
        assert parse_direction("RANDOM_TEXT") == "SKIP"
        assert parse_direction("") == "SKIP"


# ---------------------------------------------------------------------------
# 测试 _parse_researcher_round
# ---------------------------------------------------------------------------

class TestParseResearcherRound:
    def test_standard_format(self):
        """标准格式：含明确分隔符"""
        response = (
            "BULL_ANALYST:\n"
            "Apple has a strong historical track record of September iPhone releases. "
            "The September event is reportedly confirmed. Evidence strongly supports YES.\n"
            "BULL_P_YES_ESTIMATE: 0.82\n\n"
            "BEAR_ANALYST:\n"
            "Supply chain disruptions may delay availability beyond September. "
            "Shipping by Sep 30 is tight. Market may be overpricing YES.\n"
            "BEAR_P_YES_ESTIMATE: 0.58\n"
        )
        result = _parse_researcher_round(response, "test-001")
        assert "Apple" in result.bull_argument
        assert "September" in result.bull_argument
        assert "Supply chain" in result.bear_argument
        assert result.debate_id == "test-001"
        assert result.raw_response == response

    def test_fallback_no_separators(self):
        """降级：没有分隔符，自动从中间分割"""
        response = "A" * 100 + "B" * 100
        result = _parse_researcher_round(response, "test-002")
        assert len(result.bull_argument) > 0
        assert len(result.bear_argument) > 0

    def test_case_insensitive_separator(self):
        """分隔符大小写不敏感"""
        response = (
            "bull_analyst:\nBull argument here\nBULL_P_YES_ESTIMATE: 0.75\n\n"
            "bear_analyst:\nBear argument here\nBEAR_P_YES_ESTIMATE: 0.45\n"
        )
        result = _parse_researcher_round(response, "test-003")
        assert "Bull argument" in result.bull_argument
        assert "Bear argument" in result.bear_argument


# ---------------------------------------------------------------------------
# 测试 _parse_judge_critique
# ---------------------------------------------------------------------------

class TestParseJudgeCritique:
    def test_valid_xml(self):
        """标准XML格式"""
        response = (
            "After reviewing both arguments, I issue the following directives:\n\n"
            "<bull_directive>\n"
            "Please identify which section of the evidence supports your claim that "
            "Apple has confirmed a September 9 event date.\n"
            "</bull_directive>\n\n"
            "<bear_directive>\n"
            "The Bull Analyst has cited 12 news entries in 30 days. Please explain "
            "why you believe these entries reflect rumor rather than confirmed plans.\n"
            "</bear_directive>"
        )
        result = _parse_judge_critique(response, "test-001")
        assert "September 9" in result.bull_directive
        assert "12 news entries" in result.bear_directive
        assert result.debate_id == "test-001"

    def test_missing_xml_fallback(self):
        """缺少XML标签时降级处理"""
        response = "Bull should clarify X. " * 50 + "Bear should address Y. " * 50
        result = _parse_judge_critique(response, "test-002")
        # 降级后不应为空
        assert len(result.bull_directive) > 0
        assert len(result.bear_directive) > 0


# ---------------------------------------------------------------------------
# 测试 _parse_research_manager
# ---------------------------------------------------------------------------

class TestParseResearchManager:
    def test_full_structured_signal(self):
        """包含完整RESEARCH_SIGNAL块"""
        response = (
            "**Probability Estimate**: 0.79\n\n"
            "**Direction**: BUY_YES\n\n"
            "**Confidence**: 0.82\n\n"
            "**Reasoning**: The surviving bullish arguments are stronger. "
            "Apple's September event is historically reliable and 12 news entries "
            "confirm strong pre-launch coverage. The bear's supply chain argument "
            "lacks specific sourcing and was partially retracted.\n\n"
            "**Key Bull Points**: Apple September track record; confirmed event coverage.\n\n"
            "**Key Bear Points**: Supply chain risk (weak evidence).\n\n"
            "```\n"
            "RESEARCH_SIGNAL:\n"
            "P_YES: 0.79\n"
            "Direction: BUY_YES\n"
            "Confidence: 0.82\n"
            "```\n"
        )
        result = _parse_research_manager(response, "test-001")
        assert abs(result.p_yes_estimate - 0.79) < 0.001
        assert result.direction == "BUY_YES"
        assert abs(result.confidence - 0.82) < 0.001
        assert "Apple" in result.reasoning

    def test_buy_no_direction(self):
        """空头方向信号"""
        response = (
            "```\n"
            "RESEARCH_SIGNAL:\n"
            "P_YES: 0.41\n"
            "Direction: BUY_NO\n"
            "Confidence: 0.71\n"
            "```\n"
        )
        result = _parse_research_manager(response, "test-002")
        assert abs(result.p_yes_estimate - 0.41) < 0.001
        assert result.direction == "BUY_NO"

    def test_skip_direction(self):
        """SKIP信号"""
        response = (
            "```\n"
            "RESEARCH_SIGNAL:\n"
            "P_YES: 0.51\n"
            "Direction: SKIP\n"
            "Confidence: 0.55\n"
            "```\n"
        )
        result = _parse_research_manager(response, "test-003")
        assert result.direction == "SKIP"

    def test_missing_signal_block_fallback(self):
        """缺少RESEARCH_SIGNAL块时从自由文本提取p_yes"""
        response = "P_YES: 0.67\n\nDirection: BUY_YES\n\nThis is a strong case."
        result = _parse_research_manager(response, "test-004")
        assert abs(result.p_yes_estimate - 0.67) < 0.001

    def test_p_yes_clamped_to_valid_range(self):
        """p_yes边界值钳制"""
        response = (
            "```\n"
            "RESEARCH_SIGNAL:\n"
            "P_YES: 1.50\n"
            "Direction: BUY_YES\n"
            "Confidence: 0.90\n"
            "```\n"
        )
        result = _parse_research_manager(response, "test-005")
        assert result.p_yes_estimate <= 0.99

    def test_confidence_clamped_to_valid_range(self):
        """confidence边界值钳制"""
        response = (
            "```\n"
            "RESEARCH_SIGNAL:\n"
            "P_YES: 0.70\n"
            "Direction: BUY_YES\n"
            "Confidence: 0.20\n"
            "```\n"
        )
        result = _parse_research_manager(response, "test-006")
        assert result.confidence >= 0.50


# ---------------------------------------------------------------------------
# 测试 build_full_debate_prompt（Prompt构建）
# ---------------------------------------------------------------------------

class TestBuildFullDebatePrompt:
    def test_researcher_round_prompt_contains_market_info(self):
        packet = _make_packet("researcher_round")
        prompt = build_full_debate_prompt(packet)
        assert "iPhone 17" in prompt
        assert "0.720" in prompt or "0.72" in prompt
        assert "BULL_ANALYST" in prompt
        assert "BEAR_ANALYST" in prompt
        assert "END_OF_AI_RESPONSE" in prompt or "BULL_P_YES_ESTIMATE" in prompt

    def test_judge_critique_prompt_contains_xml_instruction(self):
        packet = _make_packet("judge_critique")
        packet.bull_history = "Bull Analyst (Round 1): Strong argument for YES."
        packet.bear_history = "Bear Analyst (Round 1): Strong argument for NO."
        prompt = build_full_debate_prompt(packet)
        assert "<bull_directive>" in prompt
        assert "<bear_directive>" in prompt

    def test_research_manager_prompt_contains_signal_block(self):
        packet = _make_packet("research_manager")
        packet.bull_history = "Bull: Strong case."
        packet.bear_history = "Bear: Strong case against."
        prompt = build_full_debate_prompt(packet)
        assert "RESEARCH_SIGNAL" in prompt
        assert "P_YES" in prompt
        assert "Direction" in prompt
        assert "Confidence" in prompt

    def test_unknown_task_raises_error(self):
        packet = _make_packet("unknown_task")
        with pytest.raises(ValueError, match="Unknown task type"):
            build_full_debate_prompt(packet)


# ---------------------------------------------------------------------------
# 集成测试：DebateResult数据类
# ---------------------------------------------------------------------------

class TestDebateResult:
    def test_default_values(self):
        result = DebateResult(debate_id="test-001", market_slug="test-market")
        assert result.p_yes_estimate == 0.5
        assert result.direction == "SKIP"
        assert result.is_valid is False
        assert result.rounds_completed == 0

    def test_created_at_auto_populated(self):
        result = DebateResult(debate_id="test-001", market_slug="test-market")
        assert result.created_at != ""
        assert "T" in result.created_at  # ISO格式
