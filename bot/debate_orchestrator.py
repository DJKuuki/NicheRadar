"""
bot/debate_orchestrator.py

核心调度器：协调多轮AI辩论，以终端IO方式与AI（用户侧的Claude/Gemini等）交互。

交互模式（终端IO）：
  1. 程序构建辩论Prompt并打印到终端
  2. 用户将终端内容复制发给AI
  3. 用户将AI的回复粘贴回终端的waiting input
  4. 程序解析结构化回复，继续下一步

文件交换模式（--debate-mode-type batch）：
  - 每个辩论步骤将 Prompt 写出到 ai_inbox/session_id 目录
  - 用户处理后，将对应结果写入 ai_outbox/session_id 目录
  - 程序读取结果并继续下一步；后续 Prompt 会携带上一轮结果
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.debate_models import (
    DebatePacket,
    DebateResult,
    DebateRoundResult,
    JudgeCritique,
    ResearchManagerDecision,
    parse_direction,
)
from bot.debate_prompts import build_full_debate_prompt
from bot.llm_file_exchange import (
    LlmFileExchange,
    read_pasted_response,
    strip_markdown_code_blocks,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# 解析函数：从AI自由文本中提取结构化数据
# ===========================================================================

def _strip_markdown_code_blocks(text: str) -> str:
    """Backwards-compatible alias for :func:`bot.llm_file_exchange.strip_markdown_code_blocks`.

    Kept as a module-level name because the test suite (and any external
    callers) imports it directly from this module.
    """
    return strip_markdown_code_blocks(text)


def _parse_researcher_round(response: str, debate_id: str) -> DebateRoundResult:
    """从AI回复中解析Bull和Bear的论点"""
    raw_response = response
    response = _strip_markdown_code_blocks(response)
    bull_argument = ""
    bear_argument = ""

    # 尝试按分隔符拆分
    bull_match = re.search(
        r"BULL_ANALYST\s*:\s*(.*?)(?=BEAR_ANALYST\s*:|$)",
        response,
        re.DOTALL | re.IGNORECASE,
    )
    bear_match = re.search(
        r"BEAR_ANALYST\s*:\s*(.*?)$",
        response,
        re.DOTALL | re.IGNORECASE,
    )

    if bull_match:
        bull_argument = bull_match.group(1).strip()
    if bear_match:
        bear_argument = bear_match.group(1).strip()

    # 降级：如果没有分隔符，取前半段为bull，后半段为bear
    if not bull_argument and not bear_argument:
        half = len(response) // 2
        bull_argument = response[:half].strip()
        bear_argument = response[half:].strip()
        logger.warning("debate_id=%s researcher_round: could not find BULL/BEAR separators, using halves", debate_id)

    return DebateRoundResult(
        debate_id=debate_id,
        bull_argument=bull_argument,
        bear_argument=bear_argument,
        raw_response=raw_response,
    )


def _parse_judge_critique(response: str, debate_id: str) -> JudgeCritique:
    """从AI回复中解析裁判的XML格式指令"""
    raw_response = response
    response = _strip_markdown_code_blocks(response)
    bull_directive = ""
    bear_directive = ""

    bull_match = re.search(r"<bull_directive>(.*?)</bull_directive>", response, re.DOTALL | re.IGNORECASE)
    bear_match = re.search(r"<bear_directive>(.*?)</bear_directive>", response, re.DOTALL | re.IGNORECASE)

    if bull_match:
        bull_directive = bull_match.group(1).strip()
    if bear_match:
        bear_directive = bear_match.group(1).strip()

    if not bull_directive or not bear_directive:
        logger.warning("debate_id=%s judge_critique: XML directives not found in response", debate_id)
        # 降级：整段回复作为bull指令
        bull_directive = bull_directive or response[:len(response)//2].strip()
        bear_directive = bear_directive or response[len(response)//2:].strip()

    return JudgeCritique(
        debate_id=debate_id,
        bull_directive=bull_directive,
        bear_directive=bear_directive,
        raw_response=raw_response,
    )


def _parse_research_manager(response: str, debate_id: str) -> ResearchManagerDecision:
    """从AI回复中解析Research Manager的结构化决策"""
    raw_response = response
    response = _strip_markdown_code_blocks(response)
    # 优先从RESEARCH_SIGNAL块提取
    p_yes = None
    direction = "SKIP"
    confidence = 0.65

    signal_match = re.search(
        r"RESEARCH_SIGNAL\s*:?\s*\n"
        r"P_YES\s*:\s*([0-9.]+)\s*\n"
        r"Direction\s*:\s*(\w[\w_\s]*?)\s*\n"
        r"Confidence\s*:\s*([0-9.]+)",
        response,
        re.IGNORECASE,
    )

    if signal_match:
        try:
            p_yes = float(signal_match.group(1))
            direction = parse_direction(signal_match.group(2).strip())
            confidence = float(signal_match.group(3))
        except (ValueError, IndexError):
            logger.warning("debate_id=%s research_manager: could not parse RESEARCH_SIGNAL block values", debate_id)

    # 降级：正则从自由文本提取
    if p_yes is None:
        p_match = re.search(r"(?:P_YES|p_yes|Probability Estimate)[:\s]+([0-9.]+)", response, re.IGNORECASE)
        if p_match:
            try:
                p_yes = float(p_match.group(1))
            except ValueError:
                pass

    if direction == "SKIP":
        direction_match = re.search(r"Direction\s*:\s*([^\n]+)", response, re.IGNORECASE)
        if direction_match:
            direction = parse_direction(direction_match.group(1).strip())

    confidence_match = re.search(r"Confidence\s*:\s*([0-9.]+)", response, re.IGNORECASE)
    if confidence_match:
        try:
            confidence = float(confidence_match.group(1))
        except ValueError:
            pass

    if p_yes is None:
        p_yes = 0.5
        logger.warning("debate_id=%s research_manager: p_yes not found, defaulting to 0.5", debate_id)

    # 限制范围
    p_yes = max(0.01, min(0.99, p_yes))
    confidence = max(0.50, min(1.00, confidence))

    # 提取推理文本
    reasoning_match = re.search(r"\*\*Reasoning\*\*\s*:(.*?)(?=\*\*Key|\*\*STRUCTURED|RESEARCH_SIGNAL|$)", response, re.DOTALL | re.IGNORECASE)
    reasoning = reasoning_match.group(1).strip() if reasoning_match else response[:500]

    key_bull_match = re.search(r"\*\*Key Bull Points\*\*\s*:(.*?)(?=\*\*Key Bear|\*\*STRUCTURED|RESEARCH_SIGNAL|$)", response, re.DOTALL | re.IGNORECASE)
    key_bear_match = re.search(r"\*\*Key Bear Points\*\*\s*:(.*?)(?=\*\*STRUCTURED|RESEARCH_SIGNAL|$)", response, re.DOTALL | re.IGNORECASE)

    return ResearchManagerDecision(
        debate_id=debate_id,
        p_yes_estimate=p_yes,
        direction=direction,
        confidence=confidence,
        reasoning=reasoning,
        key_bull_points=key_bull_match.group(1).strip() if key_bull_match else "",
        key_bear_points=key_bear_match.group(1).strip() if key_bear_match else "",
        raw_response=raw_response,
    )


# ===========================================================================
# 终端IO：获取AI回复
# ===========================================================================

def _prompt_ai_via_terminal(prompt_text: str, debate_id: str, task: str) -> str:
    """
    终端IO模式：将Prompt打印到终端，等待用户粘贴AI回复。

    用户操作：
      1. 复制终端输出的Prompt内容
      2. 发给AI（Claude/Gemini/GPT等）
      3. 将AI回复粘贴到此处的input提示后，按Ctrl+D（Unix）或输入END_OF_AI_RESPONSE结束
    """
    print("\n" + "█" * 70)
    print(f"  AI辩论请求 | debate_id={debate_id} | task={task}")
    print("█" * 70)
    print("\n以下是需要发给AI的Prompt内容，请完整复制：\n")
    print(prompt_text)
    print("\n" + "─" * 70)
    print("请将AI的完整回复粘贴到下方（Windows: 粘贴后按 Enter 再输入 END_OF_AI_RESPONSE，Linux/Mac: 按 Ctrl+D 结束）：")
    print("─" * 70 + "\n")

    response = read_pasted_response()
    if not response:
        logger.warning("debate_id=%s task=%s: empty response received from terminal", debate_id, task)
    return response


# ===========================================================================
# 文件IO：批量模式
# ===========================================================================

def _packet_to_sidecar(packet: DebatePacket) -> dict[str, object]:
    """Serialise a DebatePacket for the JSON sidecar in the inbox."""
    if is_dataclass(packet):
        return asdict(packet)
    return {k: v for k, v in vars(packet).items()}


def _request_id(packet: DebatePacket) -> str:
    return f"{packet.debate_id}_{packet.task}"


# ===========================================================================
# 核心调度器
# ===========================================================================

class DebateOrchestrator:
    """
    协调完整的多轮辩论流程。
    
    模式：
      - terminal（默认）：每轮交互在终端完成
      - batch：通过ai_inbox/ai_outbox文件交换完成每轮交互
    """

    def __init__(
        self,
        judge_iterations: int = 1,
        mode: str = "terminal",
        inbox_dir: str = "logs/ai_inbox",
        outbox_dir: str = "logs/ai_outbox",
    ):
        self.judge_iterations = judge_iterations
        self.mode = mode
        # 每次启动生成唯一 session_id，batch 模式下隔离并发实例的文件
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        self._exchange: LlmFileExchange | None = None
        if mode == "batch":
            self._exchange = LlmFileExchange(
                inbox_dir,
                outbox_dir,
                prompt_suffix=".txt",
                result_suffix="_result.txt",
                sidecar_suffix=".json",
                session_id=self.session_id,
            )
            self.inbox_dir = str(self._exchange.inbox_dir)
            self.outbox_dir = str(self._exchange.outbox_dir)
        else:
            self.inbox_dir = inbox_dir
            self.outbox_dir = outbox_dir
        logger.info("debate_orchestrator_init mode=%s session_id=%s", mode, self.session_id)

    def _get_ai_response(self, packet: DebatePacket) -> str:
        """根据模式获取AI回复"""
        prompt_text = build_full_debate_prompt(packet)

        if self.mode == "terminal":
            return _prompt_ai_via_terminal(prompt_text, packet.debate_id, packet.task)

        if self.mode == "batch":
            assert self._exchange is not None  # set in __init__ for batch mode
            request_id = _request_id(packet)
            self._exchange.write_prompt(
                request_id,
                prompt_text,
                sidecar=_packet_to_sidecar(packet),
            )

            def _on_missing(inbox: Path, outbox: Path) -> None:
                print(f"\n[文件交换模式] 已写入 {inbox}")
                print(f"请处理后将结果写入: {outbox}")
                print("按 Enter 重试读取，或输入 SKIP 跳过此市场...")

            result = self._exchange.wait_for_result(
                request_id,
                on_missing=_on_missing,
            )
            return result or ""

        raise ValueError(f"Unknown mode: {self.mode}")

    def run_debate(
        self,
        market_slug: str,
        market_title: str,
        market_description: str,
        settlement_date: str,
        days_to_expiry: float,
        current_yes_price: float,
        current_no_price: float,
        spread: float,
        event_type: str,
        platform: str,
        evidence_text: str,
        evidence_score: float,
    ) -> DebateResult:
        """
        运行完整的辩论流程，返回DebateResult。
        
        流程：
          Round 1: Researcher Round (Bull + Bear同时)
          [可选 Judge轮 × N]
          Final: Research Manager决策
        """
        debate_id = f"{market_slug}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        result = DebateResult(
            debate_id=debate_id,
            market_slug=market_slug,
        )

        # 初始化辩论包基础数据
        base_kwargs = dict(
            debate_id=debate_id,
            market_slug=market_slug,
            market_title=market_title,
            market_description=market_description,
            settlement_date=settlement_date,
            days_to_expiry=days_to_expiry,
            current_yes_price=current_yes_price,
            current_no_price=current_no_price,
            spread=spread,
            event_type=event_type,
            platform=platform,
            evidence_text=evidence_text,
            evidence_score=evidence_score,
        )

        bull_history = ""
        bear_history = ""
        judge_history = ""
        judge_critique_bull = ""
        judge_critique_bear = ""
        judge_count = 0

        # -----------------------------------------------------------------------
        # 初始 Researcher Round
        # -----------------------------------------------------------------------
        logger.info("debate_start debate_id=%s market=%s", debate_id, market_slug)

        packet = DebatePacket(
            **base_kwargs,
            bull_history=bull_history,
            bear_history=bear_history,
            judge_history=judge_history,
            judge_critique_bull=judge_critique_bull,
            judge_critique_bear=judge_critique_bear,
            debate_round=1,
            judge_count=judge_count,
            task="researcher_round_1",
        )

        raw_response = self._get_ai_response(packet)
        if not raw_response:
            logger.warning("debate_id=%s researcher_round: empty response, aborting", debate_id)
            result.is_valid = False
            return result

        round_result = _parse_researcher_round(raw_response, debate_id)
        bull_turn = f"Bull Analyst (Round 1):\n{round_result.bull_argument}"
        bear_turn = f"Bear Analyst (Round 1):\n{round_result.bear_argument}"
        bull_history += "\n" + bull_turn
        bear_history += "\n" + bear_turn

        result.bull_history = bull_history
        result.bear_history = bear_history
        result.rounds_completed += 1

        # -----------------------------------------------------------------------
        # Judge轮（可选，由judge_iterations控制）
        # -----------------------------------------------------------------------
        for judge_iter in range(self.judge_iterations):
            # Judge Critique
            judge_packet = DebatePacket(
                **base_kwargs,
                bull_history=bull_history,
                bear_history=bear_history,
                judge_history=judge_history,
                judge_critique_bull=judge_critique_bull,
                judge_critique_bear=judge_critique_bear,
                debate_round=judge_count + 1,
                judge_count=judge_count,
                task=f"judge_critique_{judge_count + 1}",
            )

            judge_raw = self._get_ai_response(judge_packet)
            if not judge_raw:
                logger.warning("debate_id=%s judge_critique iter=%d: empty response, skipping", debate_id, judge_iter + 1)
                break

            critique = _parse_judge_critique(judge_raw, debate_id)
            judge_critique_bull = critique.bull_directive
            judge_critique_bear = critique.bear_directive
            judge_turn = (
                f"\n--- Judge Critique (Iteration {judge_count + 1}) ---\n"
                f"[To Bull]: {critique.bull_directive}\n"
                f"[To Bear]: {critique.bear_directive}\n"
            )
            judge_history += judge_turn
            judge_count += 1

            # 后续 Researcher Round（回应Judge追问）
            followup_packet = DebatePacket(
                **base_kwargs,
                bull_history=bull_history,
                bear_history=bear_history,
                judge_history=judge_history,
                judge_critique_bull=judge_critique_bull,
                judge_critique_bear=judge_critique_bear,
                debate_round=judge_count + 1,
                judge_count=judge_count,
                task=f"researcher_round_{judge_count + 1}",
            )

            followup_raw = self._get_ai_response(followup_packet)
            if not followup_raw:
                logger.warning("debate_id=%s researcher_round followup iter=%d: empty response, skipping", debate_id, judge_iter + 1)
                break

            followup_result = _parse_researcher_round(followup_raw, debate_id)
            bull_turn = f"Bull Analyst (Round {judge_count + 1}, after Judge iteration {judge_count}):\n{followup_result.bull_argument}"
            bear_turn = f"Bear Analyst (Round {judge_count + 1}, after Judge iteration {judge_count}):\n{followup_result.bear_argument}"
            bull_history += "\n" + bull_turn
            bear_history += "\n" + bear_turn
            result.rounds_completed += 1

        result.bull_history = bull_history
        result.bear_history = bear_history
        result.judge_history = judge_history
        result.judge_iterations = judge_count

        # -----------------------------------------------------------------------
        # Research Manager：最终决策
        # -----------------------------------------------------------------------
        rm_packet = DebatePacket(
            **base_kwargs,
            bull_history=bull_history,
            bear_history=bear_history,
            judge_history=judge_history,
            judge_critique_bull="",
            judge_critique_bear="",
            debate_round=judge_count + 2,
            judge_count=judge_count,
            task="research_manager",
        )

        rm_raw = self._get_ai_response(rm_packet)
        if not rm_raw:
            logger.warning("debate_id=%s research_manager: empty response, aborting", debate_id)
            result.is_valid = False
            return result

        decision = _parse_research_manager(rm_raw, debate_id)
        result.p_yes_estimate = decision.p_yes_estimate
        result.direction = decision.direction
        result.confidence = decision.confidence
        result.reasoning = decision.reasoning
        result.is_valid = True

        logger.info(
            "debate_complete debate_id=%s market=%s p_yes=%.3f direction=%s confidence=%.2f",
            debate_id, market_slug, result.p_yes_estimate, result.direction, result.confidence,
        )

        return result

    def run_batch_debates(self, debate_inputs: list[dict]) -> list[DebateResult]:
        """
        批量运行多个市场的辩论。

        注意：多轮辩论的后续Prompt依赖上一轮AI回复，因此每个市场仍按
        researcher -> judge -> followup -> manager 的顺序推进。batch模式只负责
        用文件交换替代终端粘贴，不承诺预先生成所有后续Prompt。
        """
        results = []
        for inp in debate_inputs:
            result = self.run_debate(**inp)
            results.append(result)
        return results
