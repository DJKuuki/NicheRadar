"""
bot/debate_orchestrator.py

核心调度器：协调多轮AI辩论，以终端IO方式与AI（用户侧的Claude/Gemini等）交互。

交互模式（终端IO）：
  1. 程序构建辩论Prompt并打印到终端
  2. 用户将终端内容复制发给AI
  3. 用户将AI的回复粘贴回终端的waiting input
  4. 程序解析结构化回复，继续下一步

批量模式（--debate-batch）：
  - 一次性将所有待分析市场的所有Prompt写出到ai_inbox目录
  - 用户批量处理后，将所有结果写入ai_outbox目录
  - 程序从ai_outbox读取并继续执行
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from bot.debate_models import (
    DebatePacket,
    DebateResult,
    DebateRoundResult,
    JudgeCritique,
    ResearchManagerDecision,
    parse_direction,
)
from bot.debate_prompts import build_full_debate_prompt

logger = logging.getLogger(__name__)


# ===========================================================================
# 解析函数：从AI自由文本中提取结构化数据
# ===========================================================================

def _parse_researcher_round(response: str, debate_id: str) -> DebateRoundResult:
    """从AI回复中解析Bull和Bear的论点"""
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
        raw_response=response,
    )


def _parse_judge_critique(response: str, debate_id: str) -> JudgeCritique:
    """从AI回复中解析裁判的XML格式指令"""
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
        raw_response=response,
    )


def _parse_research_manager(response: str, debate_id: str) -> ResearchManagerDecision:
    """从AI回复中解析Research Manager的结构化决策"""
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
        raw_response=response,
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
      3. 将AI回复粘贴到此处的input提示后，按Ctrl+D（Unix）或输入EOF标记结束
    """
    print("\n" + "█" * 70)
    print(f"  AI辩论请求 | debate_id={debate_id} | task={task}")
    print("█" * 70)
    print("\n以下是需要发给AI的Prompt内容，请完整复制：\n")
    print(prompt_text)
    print("\n" + "─" * 70)
    print("请将AI的完整回复粘贴到下方（Windows: 粘贴后按 Enter 再输入 END_OF_AI_RESPONSE，Linux/Mac: 按 Ctrl+D 结束）：")
    print("─" * 70 + "\n")

    lines = []
    try:
        while True:
            line = input()
            if line.strip() == "END_OF_AI_RESPONSE":
                break
            lines.append(line)
    except EOFError:
        pass

    response = "\n".join(lines).strip()
    if not response:
        logger.warning("debate_id=%s task=%s: empty response received from terminal", debate_id, task)
    return response


# ===========================================================================
# 文件IO：批量模式
# ===========================================================================

def _write_inbox_packet(inbox_dir: str, packet: DebatePacket, prompt_text: str) -> None:
    """将辩论包和Prompt写入ai_inbox目录"""
    path = Path(inbox_dir)
    path.mkdir(parents=True, exist_ok=True)

    packet_file = path / f"{packet.debate_id}_{packet.task}.json"
    prompt_file = path / f"{packet.debate_id}_{packet.task}.txt"

    with open(packet_file, "w", encoding="utf-8") as f:
        # 简单序列化packet（不含复杂类型）
        json.dump(
            {k: v for k, v in vars(packet).items()},
            f,
            ensure_ascii=False,
            indent=2,
        )

    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt_text)

    logger.info("debate_inbox_written debate_id=%s task=%s path=%s", packet.debate_id, packet.task, prompt_file)


def _read_outbox_result(outbox_dir: str, debate_id: str, task: str) -> Optional[str]:
    """从ai_outbox目录读取AI回复"""
    path = Path(outbox_dir) / f"{debate_id}_{task}_result.txt"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ===========================================================================
# 核心调度器
# ===========================================================================

class DebateOrchestrator:
    """
    协调完整的多轮辩论流程。
    
    模式：
      - terminal（默认）：每轮交互在终端完成
      - batch：一次性写入ai_inbox，从ai_outbox读取结果
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
        self.inbox_dir = inbox_dir
        self.outbox_dir = outbox_dir

    def _get_ai_response(self, packet: DebatePacket) -> str:
        """根据模式获取AI回复"""
        prompt_text = build_full_debate_prompt(packet)

        if self.mode == "terminal":
            return _prompt_ai_via_terminal(prompt_text, packet.debate_id, packet.task)

        elif self.mode == "batch":
            # 写入inbox，尝试从outbox读取（批量模式下由外部流程处理）
            _write_inbox_packet(self.inbox_dir, packet, prompt_text)
            result = _read_outbox_result(self.outbox_dir, packet.debate_id, packet.task)
            if result:
                return result
            # 如果outbox还没有结果，进入等待循环
            print(f"\n[批量模式] 已写入 {self.inbox_dir}/{packet.debate_id}_{packet.task}.txt")
            print(f"请处理后将结果写入: {self.outbox_dir}/{packet.debate_id}_{packet.task}_result.txt")
            print("按 Enter 重试读取，或输入 SKIP 跳过此市场...")
            while True:
                cmd = input("> ").strip().upper()
                if cmd == "SKIP":
                    return ""
                result = _read_outbox_result(self.outbox_dir, packet.debate_id, packet.task)
                if result:
                    return result
                print("仍未找到结果文件，再次重试...")

        else:
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
        debate_id = f"{market_slug}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

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
            task="researcher_round",
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
                task="judge_critique",
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
                task="researcher_round",
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
        批量运行多个市场的辩论（批量模式）。
        先写入所有inbox，再等待用户处理，最后读取所有outbox。
        """
        results = []
        for inp in debate_inputs:
            result = self.run_debate(**inp)
            results.append(result)
        return results
