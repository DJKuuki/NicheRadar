"""
bot/debate_models.py

数据类：支持AI辩论机制的核心数据结构。
程序与AI（我）之间通过这些结构化数据进行交互。

辩论流程：
  1. DebatePacket    → 程序构建后输出给AI分析
  2. DebateRoundResult → AI回复中的单轮结果（bull/bear论点）
  3. JudgeCritique   → AI裁判追问（可选轮次）
  4. DebateResult    → Research Manager综合结论（含p_yes估计）
  5. DebateSignal    → 最终交易信号（扩展自Signal）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 辩论包：程序 → AI
# ---------------------------------------------------------------------------

@dataclass
class DebatePacket:
    """
    发送给AI分析的完整辩论包。
    包含市场信息、当前证据原文，以及辩论历史（多轮时使用）。
    """
    debate_id: str                    # 唯一标识，格式: {slug}_{timestamp}
    market_slug: str                  # Polymarket市场slug
    market_title: str                 # 市场问题标题
    market_description: str           # 市场完整描述（含结算规则）
    settlement_date: str              # 结算日期 YYYY-MM-DD
    days_to_expiry: float             # 距结算天数
    current_yes_price: float          # YES当前中间价
    current_no_price: float           # NO当前中间价
    spread: float                     # 买卖价差
    event_type: str                   # 事件类型（music_release / product_release / ipo_event 等）
    platform: str                     # 平台标识
    evidence_text: str                # RSS/News证据原文摘要（完整文本，由evidence_collector提供）
    evidence_score: float             # 证据原始分数（0-1）

    # 辩论历史（多轮时填充）
    bull_history: str = ""            # 多头论点历史
    bear_history: str = ""            # 空头论点历史
    judge_history: str = ""           # 裁判追问历史
    judge_critique_bull: str = ""     # 上一轮裁判对多头的追问
    judge_critique_bear: str = ""     # 上一轮裁判对空头的追问

    # 当前状态
    debate_round: int = 1             # 当前辩论轮次（1-based）
    judge_count: int = 0              # 已完成的裁判轮次
    task: str = "researcher_round"   # 当前任务类型


# ---------------------------------------------------------------------------
# AI回复：researcher_round
# ---------------------------------------------------------------------------

@dataclass
class DebateRoundResult:
    """AI对researcher_round任务的回复：Bull和Bear初始/后续论点"""
    debate_id: str
    bull_argument: str
    bear_argument: str
    raw_response: str = ""            # AI的原始回复文本（用于审计）


# ---------------------------------------------------------------------------
# AI回复：judge_critique
# ---------------------------------------------------------------------------

@dataclass
class JudgeCritique:
    """AI对judge_critique任务的回复：裁判对双方的定向追问"""
    debate_id: str
    bull_directive: str               # 对多头的追问/指令
    bear_directive: str               # 对空头的追问/指令
    raw_response: str = ""


# ---------------------------------------------------------------------------
# AI回复：research_manager（核心决策）
# ---------------------------------------------------------------------------

@dataclass
class ResearchManagerDecision:
    """AI对research_manager任务的回复：综合辩论历史给出方向判断"""
    debate_id: str
    p_yes_estimate: float             # 模型估计的YES概率（0.0-1.0）
    direction: str                    # "BUY_YES" | "BUY_NO" | "SKIP"
    confidence: float                 # 信心度（0.5-1.0）
    reasoning: str                    # 详细推理过程
    key_bull_points: str = ""         # 关键多头论点摘要
    key_bear_points: str = ""         # 关键空头论点摘要
    unresolved_conflicts: str = ""    # 未解决的分歧（影响confidence）
    raw_response: str = ""


# ---------------------------------------------------------------------------
# 最终辩论结果（汇总所有轮次）
# ---------------------------------------------------------------------------

@dataclass
class DebateResult:
    """
    一次完整辩论过程的汇总结果。
    由DebateOrchestrator在所有轮次完成后填充。
    """
    debate_id: str
    market_slug: str

    # 研究阶段
    bull_history: str = ""
    bear_history: str = ""
    judge_history: str = ""

    # 最终决策
    p_yes_estimate: float = 0.5
    direction: str = "SKIP"           # "BUY_YES" | "BUY_NO" | "SKIP"
    confidence: float = 0.5
    reasoning: str = ""

    # 元数据
    rounds_completed: int = 0
    judge_iterations: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # 是否有效（False时使用fallback的logit信号）
    is_valid: bool = False


# ---------------------------------------------------------------------------
# 辩论信号（整合进交易决策）
# ---------------------------------------------------------------------------

@dataclass
class DebateSignal:
    """
    AI辩论产生的交易信号，与原有Signal兼容。
    由build_debate_signal()从DebateResult构建。
    """
    market_id: str
    side: str                         # "BUY_YES" | "BUY_NO"
    p_model: float                    # AI估计的YES概率
    p_mid: float                      # 市场当前中间价
    edge: float                       # p_model - p_mid（或1-p_model-p_no_mid）
    net_edge: float                   # edge - fee_buffer - uncertainty_buffer
    max_entry_price: float            # 最高可接受入场价
    confidence: float                 # AI信心度
    debate_id: str = ""               # 关联的辩论ID
    reasoning: str = ""               # AI推理摘要
    reasons: list = field(default_factory=list)  # 与Signal兼容的reasons列表
    source: str = "debate"            # 信号来源标识


# ---------------------------------------------------------------------------
# 解析辅助：从AI文本回复中提取结构化数据
# ---------------------------------------------------------------------------

DIRECTION_ALIASES = {
    "buy_yes": "BUY_YES",
    "buy yes": "BUY_YES",
    "yes": "BUY_YES",
    "buy_no": "BUY_NO",
    "buy no": "BUY_NO",
    "no": "BUY_NO",
    "skip": "SKIP",
    "hold": "SKIP",
    "pass": "SKIP",
}


def parse_direction(text: str) -> str:
    """从AI回复文本中解析方向，容错处理多种表达方式"""
    lower = text.lower().strip()
    return DIRECTION_ALIASES.get(lower, "SKIP")
