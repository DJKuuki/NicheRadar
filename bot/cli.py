from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the PolyMarket shadow bot.")
    parser.add_argument("--sample-data", help="Path to sample market json data.")
    parser.add_argument("--live", action="store_true", help="Fetch live markets from Polymarket.")
    parser.add_argument("--limit", type=int, default=20, help="Live market fetch limit.")
    parser.add_argument("--watchlist", help="Path to watchlist json. Implies focused live market mode.")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Polling interval in seconds for watchlist mode.")
    parser.add_argument("--iterations", type=int, default=1, help="How many polling iterations to run.")
    parser.add_argument("--log-file", default="logs/watchlist_snapshots.jsonl", help="Path to watchlist snapshot log file.")
    parser.add_argument("--alert-file", default="logs/watchlist_alerts.jsonl", help="Path to watchlist alert log file.")
    parser.add_argument("--shadow-file", default="logs/shadow_fills.jsonl", help="Path to shadow fill log file.")
    parser.add_argument("--db-file", default="logs/watchlist.sqlite", help="Path to SQLite watchlist database.")
    parser.add_argument("--shadow-replay", action="store_true", help="Replay shadow fills from SQLite and print PnL summary.")
    parser.add_argument("--settlement-file", help="Optional JSON file with manual shadow close/settlement records.")
    parser.add_argument("--validate-settlements", action="store_true", help="Validate settlement file coverage and conflicts against shadow fills.")
    parser.add_argument("--settlement-validation-json", help="Optional path to write settlement validation JSON.")
    parser.add_argument("--replay-json", help="Optional path to write the full shadow replay JSON report.")
    parser.add_argument("--dashboard-report", action="store_true", help="Build a compact SQLite report for edge, alerts, and shadow PnL.")
    parser.add_argument("--report-file", default="logs/dashboard_report.md", help="Path to write the dashboard markdown report.")
    parser.add_argument("--report-json", help="Optional path to write the dashboard JSON report.")
    parser.add_argument("--report-html", default="logs/dashboard.html", help="Path to write the local HTML dashboard.")
    parser.add_argument("--report-limit", type=int, default=10, help="Maximum rows shown in report detail sections.")
    parser.add_argument("--calibration-report", action="store_true", help="Build a model-profile calibration report from shadow samples.")
    parser.add_argument("--calibration-file", default="logs/calibration_report.md", help="Path to write the calibration markdown report.")
    parser.add_argument("--calibration-json", help="Optional path to write the full calibration JSON report.")
    parser.add_argument("--calibration-min-samples", type=int, default=5, help="Minimum shadow samples required before suggesting parameter changes.")
    parser.add_argument("--backtest", action="store_true", help="Build an offline backtest report from local SQLite history.")
    parser.add_argument("--backtest-report", default="logs/backtest_report.md", help="Path to write the backtest markdown report.")
    parser.add_argument("--backtest-json", default="logs/backtest_report.json", help="Path to write the backtest JSON report.")
    parser.add_argument("--backtest-min-samples", type=int, default=20, help="Minimum settled samples used for reliability warnings.")
    parser.add_argument("--backtest-target-source", choices=["settlement_file", "latest_mark", "snapshot_mid"], help="Optional target source filter.")
    parser.add_argument("--backtest-profile", help="Optional model_profile filter for entry replay.")
    parser.add_argument("--backtest-event-type", help="Optional event_type filter for entry replay.")
    parser.add_argument("--backtest-from", dest="backtest_from", help="Inclusive backtest start date, YYYY-MM-DD.")
    parser.add_argument("--backtest-to", dest="backtest_to", help="Inclusive backtest end date, YYYY-MM-DD.")
    parser.add_argument("--backtest-min-net-edge", type=float, default=0.0, help="Minimum net_edge for replayed shadow entry eligibility.")
    parser.add_argument("--backtest-max-spread", type=float, help="Maximum side spread for replayed shadow entry eligibility.")
    # ---- Historical data arguments ----
    parser.add_argument("--fetch-history", action="store_true", help="Fetch resolved markets + CLOB price history from Polymarket and store in history-db.")
    parser.add_argument("--build-history-snapshots", action="store_true", help="Build virtual snapshots from historical price data and store in history-db.")
    parser.add_argument("--history-backtest", action="store_true", help="Run backtest report using historical snapshots (auto-settled from outcome_yes).")
    parser.add_argument("--history-calibrate", action="store_true", help="Run calibration report using historical snapshots.")
    parser.add_argument("--history-db", default="logs/historical.sqlite", help="Path to historical data SQLite database.")
    parser.add_argument("--history-keywords", default="", help="Comma-separated keywords to filter historical markets (default: built-in list).")
    parser.add_argument("--history-min-volume", type=float, default=1000.0, help="Minimum volume for historical market inclusion.")
    parser.add_argument("--history-start-date", default="2023-01-01", help="Start date (YYYY-MM-DD) for historical market fetch.")
    parser.add_argument("--history-max-markets", type=int, default=5000, help="Maximum number of historical markets to fetch.")
    parser.add_argument("--history-fidelity", type=int, default=360, help="Price bar granularity in minutes (default 360 = 6h).")
    parser.add_argument("--history-max-days-before-close", type=float, default=None, help="Only include historical snapshots within this many days of market close.")
    parser.add_argument("--history-backtest-report", default="logs/history_backtest_report.md", help="Path to write the historical backtest markdown report.")
    parser.add_argument("--history-backtest-json", default="logs/history_backtest_report.json", help="Path to write the historical backtest JSON report.")
    parser.add_argument("--history-calibration-file", default="logs/history_calibration_report.md", help="Path to write the historical calibration markdown report.")
    parser.add_argument("--history-calibration-json", default="", help="Optional path to write the historical calibration JSON report.")
    parser.add_argument("--shadow-bankroll", type=float, default=1000.0, help="Shadow bankroll used for exposure sizing.")
    parser.add_argument("--shadow-position-risk-pct", type=float, default=0.02, help="Bankroll fraction risked per shadow fill.")
    parser.add_argument("--max-total-risk-pct", type=float, default=0.20, help="Maximum total open shadow exposure as bankroll fraction.")
    parser.add_argument("--max-market-risk-pct", type=float, default=0.02, help="Maximum open shadow exposure per market as bankroll fraction.")
    parser.add_argument("--max-event-type-risk-pct", type=float, default=0.08, help="Maximum open shadow exposure per event type as bankroll fraction.")
    parser.add_argument("--circuit-breaker-loss-pct", type=float, default=0.05, help="Pause new shadow fills if unrealized PnL falls below this bankroll fraction.")
    parser.add_argument("--max-open-shadow-positions", type=int, default=10, help="Maximum number of open shadow positions.")
    parser.add_argument("--cache-file", default="logs/http_cache.sqlite", help="Path to HTTP cache SQLite database.")
    parser.add_argument("--gamma-cache-seconds", type=float, default=30.0, help="Gamma API cache TTL in seconds.")
    parser.add_argument("--book-cache-seconds", type=float, default=10.0, help="CLOB book cache TTL in seconds.")
    parser.add_argument("--rss-cache-seconds", type=float, default=900.0, help="RSS/Atom cache TTL in seconds.")
    parser.add_argument("--api-rate-limit-seconds", type=float, default=0.10, help="Minimum delay between Polymarket API requests.")
    parser.add_argument("--rss-rate-limit-seconds", type=float, default=0.25, help="Minimum delay between RSS requests.")
    parser.add_argument(
        "--watchlist-max-days",
        type=float,
        default=120.0,
        help="Maximum days to expiry allowed in watchlist mode.",
    )
    parser.add_argument(
        "--alert-evidence-jump",
        type=float,
        default=0.15,
        help="Minimum evidence score increase needed to write an alert.",
    )
    parser.add_argument(
        "--evidence-sources",
        default="data/evidence_sources.json",
        help="Path to evidence source registry json.",
    )
    # ---- LLM evidence source discovery ----
    parser.add_argument(
        "--llm-source-finder",
        action="store_true",
        help="Use an LLM-assisted file/terminal workflow to find more relevant evidence RSS sources.",
    )
    parser.add_argument(
        "--llm-source-mode",
        choices=["batch", "terminal"],
        default="batch",
        help="LLM source finder interaction mode: batch=file exchange, terminal=paste response.",
    )
    parser.add_argument(
        "--llm-source-policy",
        choices=["missing", "missing_or_mismatch", "always"],
        default="missing_or_mismatch",
        help="When to ask the LLM for source suggestions.",
    )
    parser.add_argument(
        "--llm-source-inbox",
        default="logs/source_inbox",
        help="Directory where LLM source finder prompts are written in batch mode.",
    )
    parser.add_argument(
        "--llm-source-outbox",
        default="logs/source_outbox",
        help="Directory where LLM source finder JSON responses are read in batch mode.",
    )
    parser.add_argument(
        "--llm-source-max-sources",
        type=int,
        default=3,
        help="Maximum LLM-suggested sources to evaluate per market.",
    )
    # ---- AI辩论模式参数 ----
    parser.add_argument(
        "--debate-mode",
        action="store_true",
        help="开启 AI 辩论模式：用AI分析取代启发式 logit 信号引擎。"
             "默认为终端交互模式（每个市场分析要手动操作）。",
    )
    parser.add_argument(
        "--debate-rounds",
        type=int,
        default=1,
        help="AI辩论中 Judge 迨代次数（0=无Judge过滤，默认=1）。",
    )
    parser.add_argument(
        "--debate-mode-type",
        choices=["terminal", "batch"],
        default="terminal",
        help="辩论交互模式：terminal=终端手动交互，batch=文件交换模式。",
    )
    parser.add_argument(
        "--debate-inbox",
        default="logs/ai_inbox",
        help="批量模式下辩论包写入目录。",
    )
    parser.add_argument(
        "--debate-outbox",
        default="logs/ai_outbox",
        help="批量模式下 AI 回复读取目录。",
    )
    parser.add_argument(
        "--debate-min-edge",
        type=float,
        default=0.05,
        help="AI辩论信号要求的最小 Edge（AI估计概率与市场价之差），默认 0.05。",
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        help="在执行观察列表循环前自动发现新的小众市场并加入列表。",
    )
    parser.add_argument(
        "--discovery-limit",
        type=int,
        default=100,
        help="自动发现模式下扫描的市场总数限制（默认100）。",
    )
    return parser
