from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import time

from bot.common import setup_logging
from bot.config import BotConfig
from bot.backtest_dataset import load_backtest_samples, load_historical_backtest_samples
from bot.backtest_engine import BacktestStrategyParams
from bot.backtest_reporting import (
    build_backtest_report,
    format_backtest_report,
    write_backtest_json,
    write_backtest_markdown,
)
from bot.calibration import (
    build_calibration_report,
    format_calibration_report,
    write_calibration_json,
    write_calibration_markdown,
)
from bot.cli import build_parser
from bot.evidence_collector import EvidenceCollector
from bot.execution_engine import build_trade_idea
from bot.market_parser import parse_market, utc_now
from bot.market_scanner import load_live_markets, load_live_markets_by_slugs, load_sample_markets
from bot.portfolio_risk import filter_shadow_fills_for_portfolio, load_portfolio_risk_state
from bot.risk_engine import allow_market, allow_signal
from bot.reporting import (
    build_dashboard_report,
    format_dashboard_report,
    write_dashboard_html,
    write_dashboard_json,
    write_dashboard_markdown,
)
from bot.settlement_validation import (
    format_settlement_validation,
    validate_settlements,
    write_settlement_validation_json,
)
from bot.shadow import append_shadow_fills, build_shadow_fills
from bot.shadow_replay import format_shadow_replay_report, load_settlements, replay_shadow_pnl, write_replay_json
from bot.signal_engine import build_signal, build_debate_signal, is_debate_signal_tradeable
from bot.storage import WatchlistStore
from bot.watchlist import (
    append_watchlist_alerts,
    append_watchlist_snapshots,
    build_watchlist_alerts,
    build_watchlist_report,
    build_watchlist_snapshot,
    load_latest_watchlist_snapshots,
    load_watchlist,
)
from bot.historical_fetcher import HistoricalFetcher
from bot.historical_storage import HistoricalStore
from bot.historical_snapshot_builder import build_historical_snapshots


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()

    # ---- Historical data modes (independent of shadow bot state) ----
    if args.fetch_history:
        _run_fetch_history(args)
        return

    if args.build_history_snapshots:
        _run_build_history_snapshots(args)
        return

    if args.history_backtest:
        _run_history_backtest(args)
        return

    if args.history_calibrate:
        _run_history_calibrate(args)
        return

    if args.validate_settlements:
        if not args.settlement_file:
            parser.error("--validate-settlements requires --settlement-file.")
        settlements = load_settlements(args.settlement_file)
        report = validate_settlements(args.db_file, settlements)
        for line in format_settlement_validation(report):
            print(line)
        if args.settlement_validation_json:
            write_settlement_validation_json(args.settlement_validation_json, report)
            print(f"wrote_settlement_validation_json={args.settlement_validation_json}")
        return

    if args.shadow_replay:
        settlements = load_settlements(args.settlement_file)
        replay = replay_shadow_pnl(args.db_file, settlements)
        for line in format_shadow_replay_report(replay):
            print(line)
        if args.replay_json:
            write_replay_json(args.replay_json, replay)
            print(f"wrote_replay_json={args.replay_json}")
        return

    if args.calibration_report:
        settlements = load_settlements(args.settlement_file)
        report = build_calibration_report(args.db_file, settlements, args.calibration_min_samples)
        for line in format_calibration_report(report):
            print(line)
        if args.calibration_file:
            write_calibration_markdown(args.calibration_file, report)
            print(f"wrote_calibration_file={args.calibration_file}")
        if args.calibration_json:
            write_calibration_json(args.calibration_json, report)
            print(f"wrote_calibration_json={args.calibration_json}")
        return

    if args.backtest:
        settlements = load_settlements(args.settlement_file)
        params = BacktestStrategyParams(
            min_net_edge=args.backtest_min_net_edge,
            max_spread=args.backtest_max_spread,
            model_profile=args.backtest_profile,
            event_type=args.backtest_event_type,
        )
        samples = load_backtest_samples(
            args.db_file,
            settlements,
            params,
            target_source=args.backtest_target_source,
            start_date=args.backtest_from,
            end_date=args.backtest_to,
        )
        report = build_backtest_report(samples, args.db_file, args.backtest_min_samples)
        for line in format_backtest_report(report):
            print(line)
        if args.backtest_report:
            write_backtest_markdown(args.backtest_report, report)
            print(f"wrote_backtest_report={args.backtest_report}")
        if args.backtest_json:
            write_backtest_json(args.backtest_json, report)
            print(f"wrote_backtest_json={args.backtest_json}")
        return

    config = _build_config(args)
    if args.dashboard_report:
        settlements = load_settlements(args.settlement_file)
        report = build_dashboard_report(args.db_file, args.report_limit, config, settlements)
        for line in format_dashboard_report(report):
            print(line)
        if args.report_file:
            write_dashboard_markdown(args.report_file, report)
            print(f"wrote_report_file={args.report_file}")
        if args.report_json:
            write_dashboard_json(args.report_json, report)
            print(f"wrote_report_json={args.report_json}")
        if args.report_html:
            write_dashboard_html(args.report_html, report)
            print(f"wrote_report_html={args.report_html}")
        return

    # ---- Automatic Discovery Engine (initialized here, runs inside loop) ----
    discovery_engine = None
    if getattr(args, "auto_discover", False):
        from bot.market_discovery import DiscoveryEngine
        discovery_engine = DiscoveryEngine(
            watchlist_path=args.watchlist or "data/watchlist.json",
            gamma_cache_seconds=args.gamma_cache_seconds,
        )
        print("auto_discovery_engine=initialized")

    collector = EvidenceCollector(
        args.evidence_sources,
        cache_path=args.cache_file,
        cache_seconds=args.rss_cache_seconds,
        rate_limit_seconds=args.rss_rate_limit_seconds,
        source_finder=_build_source_finder(args),
        llm_source_policy=args.llm_source_policy,
    )
    watchlist_items = load_watchlist(args.watchlist) if args.watchlist else []
    if watchlist_items:
        watchlist_config = _build_config(args, max_days_to_expiry=args.watchlist_max_days)
        _run_watchlist_loop(args, watchlist_config, collector, watchlist_items, discovery_engine=discovery_engine)
        return

    now = utc_now().astimezone(timezone.utc)
    if args.live:
        markets = load_live_markets(
            limit=args.limit,
            cache_path=args.cache_file,
            gamma_cache_seconds=args.gamma_cache_seconds,
            book_cache_seconds=args.book_cache_seconds,
            rate_limit_seconds=args.api_rate_limit_seconds,
        )
    elif args.sample_data:
        markets = load_sample_markets(args.sample_data)
    else:
        parser.error("Provide --watchlist, or either --live or --sample-data.")

    print("PolyMarket shadow bot")
    print(f"loaded_markets={len(markets)}")

    ideas = []
    for market in markets:
        parsed = parse_market(market, now)
        if parsed is None:
            continue

        allowed_market, market_reasons = allow_market(parsed, config)
        evidence = collector.collect(parsed, now)
        signal = build_signal(parsed, evidence, config)
        allowed_signal, signal_reasons = allow_signal(signal, config)

        if not allowed_market:
            print(f"skip_market={market.market_id} reasons={','.join(market_reasons)}")
            continue
        if not allowed_signal:
            print(f"skip_signal={market.market_id} reasons={','.join(signal_reasons)}")
            continue

        ideas.append(build_trade_idea(parsed, signal))

    if not ideas:
        print("no_trade_ideas")
        return

    for idea in ideas:
        print(
            "trade_idea "
            f"market_id={idea.market_id} "
            f"side={idea.side} "
            f"price={idea.target_price:.4f} "
            f"net_edge={idea.net_edge:.4f} "
            f"title={idea.title}"
        )
        for reason in idea.reasons:
            print(f"  reason={reason}")

def _run_watchlist_loop(
    args: argparse.Namespace,
    config: BotConfig,
    collector: EvidenceCollector,
    watchlist_items,
    discovery_engine=None,
) -> None:
    iterations = max(1, args.iterations)
    poll_seconds = max(0, args.poll_seconds)
    # 使用可变列表以便循环中动态追加新发现的市场
    watchlist_items = list(watchlist_items)
    slugs = [item.slug for item in watchlist_items]
    previous_by_slug = load_latest_watchlist_snapshots(args.log_file)
    store = WatchlistStore(args.db_file) if args.db_file else None
    # 自动发现每 N 轮触发一次（默认每 10 轮）
    discovery_interval = getattr(args, "discovery_interval", 10)

    # 初始化 AI 辩论调度器（仅在 --debate-mode 开启时）
    orchestrator = None
    if getattr(args, "debate_mode", False):
        from bot.debate_orchestrator import DebateOrchestrator
        orchestrator = DebateOrchestrator(
            judge_iterations=getattr(args, "debate_rounds", 1),
            mode=getattr(args, "debate_mode_type", "terminal"),
            inbox_dir=getattr(args, "debate_inbox", "logs/ai_inbox"),
            outbox_dir=getattr(args, "debate_outbox", "logs/ai_outbox"),
        )
        print(
            f"debate_mode=enabled "
            f"judge_iterations={orchestrator.judge_iterations} "
            f"mode={orchestrator.mode}"
        )

    for iteration in range(1, iterations + 1):
        now = utc_now().astimezone(timezone.utc)

        # ---- 周期性自动发现（每 discovery_interval 轮执行一次，第1轮也执行）----
        if discovery_engine is not None and (iteration == 1 or iteration % discovery_interval == 0):
            added = discovery_engine.discover_and_append(limit=args.discovery_limit)
            if added:
                print(f"auto_discovery_added count={len(added)} slugs={','.join(added)}")
                # 动态加载新市场到当前运行时列表
                from bot.watchlist import load_watchlist
                try:
                    updated_items = load_watchlist(args.watchlist)
                    new_slugs = [s for s in added if s not in slugs]
                    for new_item in updated_items:
                        if new_item.slug in new_slugs:
                            watchlist_items.append(new_item)
                            slugs.append(new_item.slug)
                    if new_slugs:
                        print(f"auto_discovery_appended_to_runtime count={len(new_slugs)}")
                except Exception as disc_err:
                    print(f"auto_discovery_runtime_append_failed err={disc_err}")
            else:
                print("auto_discovery_no_new_markets")

        markets = load_live_markets_by_slugs(
            slugs,
            cache_path=args.cache_file,
            gamma_cache_seconds=args.gamma_cache_seconds,
            book_cache_seconds=args.book_cache_seconds,
            rate_limit_seconds=args.api_rate_limit_seconds,
        )
        market_by_slug = {str(market.metadata.get("slug", "")): market for market in markets}
        snapshots: list[dict[str, object]] = []

        print("PolyMarket shadow bot")
        print(f"watchlist_iteration={iteration}/{iterations} timestamp_utc={datetime.now(timezone.utc).isoformat()}")
        print(f"loaded_markets={len(markets)}")

        if not market_by_slug:
            print("no_watchlist_markets")
        for item in watchlist_items:
            market = market_by_slug.get(item.slug)
            if market is None:
                print(f"missing_watchlist_market slug={item.slug}")
                continue

            parsed = parse_market(market, now)
            if parsed is None:
                print(f"unparsed_watchlist_market slug={item.slug}")
                continue

            allowed_market, market_reasons = allow_market(parsed, config)
            evidence = collector.collect(parsed, now)
            signal = build_signal(parsed, evidence, config)
            allowed_signal, signal_reasons = allow_signal(signal, config)

            # ----------------------------------------------------------------
            # AI辩论模式：对通过初步过滤的候选市场，调用AI辩论生成更可靠的信号
            # ----------------------------------------------------------------
            if orchestrator is not None and allowed_market:
                debate_min_edge = getattr(args, "debate_min_edge", 0.05)
                # 只对初步logit信号显示有价値的市场运行辩论（net_edge > 0，避免对无机会市场浪费交互）
                if signal.net_edge > 0:
                    print(
                        f"debate_trigger slug={item.slug} "
                        f"logit_net_edge={signal.net_edge:.4f} "
                        f"p_mid={signal.p_mid:.4f}"
                    )
                    # 构建辩论输入所需的证据文本
                    evidence_text = _build_evidence_text(evidence, parsed)
                    debate_result = orchestrator.run_debate(
                        market_slug=item.slug,
                        market_title=market.title,
                        market_description=market.description,
                        settlement_date=market.closes_at.strftime("%Y-%m-%d"),
                        days_to_expiry=parsed.days_to_expiry,
                        current_yes_price=market.mid_probability,
                        current_no_price=market.no_mid_probability,
                        spread=market.spread,
                        event_type=parsed.event_type,
                        platform=parsed.platform,
                        evidence_text=evidence_text,
                        evidence_score=evidence.score,
                    )
                    # 将辩论结果落库
                    if store is not None:
                        try:
                            store.insert_debate_result(item.slug, debate_result)
                        except Exception as e:
                            print(f"debate_record_insert_error slug={item.slug} err={e}")

                    # 用辩论信号替换logit信号
                    tradeable, debate_block_reasons = is_debate_signal_tradeable(debate_result, config)
                    if tradeable:
                        debate_signal = build_debate_signal(debate_result, parsed, config)
                        # 追加辩论 edge 过滤
                        if abs(debate_signal.edge) >= debate_min_edge:
                            signal = debate_signal
                            allowed_signal, signal_reasons = allow_signal(signal, config)
                            print(
                                f"debate_signal_accepted slug={item.slug} "
                                f"p_yes={debate_result.p_yes_estimate:.3f} "
                                f"direction={debate_result.direction} "
                                f"edge={debate_signal.edge:.4f} "
                                f"confidence={debate_result.confidence:.2f}"
                            )
                        else:
                            allowed_signal = False
                            signal_reasons = [
                                f"debate_edge_too_small={abs(debate_signal.edge):.4f}",
                                f"min_required={debate_min_edge}",
                            ]
                            print(
                                f"debate_signal_skipped_edge slug={item.slug} "
                                f"edge={debate_signal.edge:.4f} min={debate_min_edge}"
                            )
                    else:
                        allowed_signal = False
                        signal_reasons = debate_block_reasons
                        print(
                            f"debate_signal_blocked slug={item.slug} "
                            f"reasons={','.join(debate_block_reasons)}"
                        )
                else:
                    print(
                        f"debate_skip_no_edge slug={item.slug} "
                        f"logit_net_edge={signal.net_edge:.4f} — 辩论仅对有初始优势的市场运行"
                    )

            snapshots.append(
                build_watchlist_snapshot(
                    item,
                    market,
                    parsed,
                    evidence,
                    signal,
                    allowed_market,
                    market_reasons,
                    allowed_signal,
                    signal_reasons,
                )
            )
            for line in build_watchlist_report(
                item,
                market,
                parsed,
                signal,
                allowed_market,
                market_reasons,
                allowed_signal,
                signal_reasons,
            ):
                print(line)

        if snapshots:
            alerts = build_watchlist_alerts(previous_by_slug, snapshots, args.alert_evidence_jump)
            shadow_fills = build_shadow_fills(snapshots, config)
            if store is not None:
                shadow_fills = store.filter_new_shadow_fills(shadow_fills)
            portfolio_state = load_portfolio_risk_state(args.db_file if store is not None else None, config)
            print(
                "portfolio_risk "
                f"open_positions={portfolio_state.open_positions} "
                f"total_exposure={portfolio_state.total_exposure:.4f} "
                f"total_exposure_pct={portfolio_state.total_exposure_pct:.2%} "
                f"unrealized_pnl={portfolio_state.unrealized_pnl:.4f} "
                f"circuit_breaker={str(portfolio_state.circuit_breaker_active).lower()}"
            )
            blocked_shadow_fills: list[dict[str, object]] = []
            if shadow_fills:
                candidate_fills = shadow_fills
                candidate_count = len(shadow_fills)
                shadow_fills, _ = filter_shadow_fills_for_portfolio(args.db_file if store is not None else None, candidate_fills, config)
                blocked_shadow_fills = [fill for fill in candidate_fills if fill.get("portfolio_risk_ok") is False]
                print(f"portfolio_candidates={candidate_count} accepted={len(shadow_fills)} blocked={candidate_count - len(shadow_fills)}")
            append_watchlist_snapshots(args.log_file, snapshots)
            print(f"appended_snapshots={len(snapshots)} log_file={args.log_file}")
            if store is not None:
                store.insert_snapshots(snapshots)
                store.insert_evidence_runs(snapshots)
            previous_by_slug.update({str(snapshot["slug"]): snapshot for snapshot in snapshots})
            if alerts:
                append_watchlist_alerts(args.alert_file, alerts)
                print(f"appended_alerts={len(alerts)} alert_file={args.alert_file}")
                if store is not None:
                    store.insert_alerts(alerts)
                for alert in alerts:
                    print(f"watchlist_alert slug={alert['slug']} reasons={','.join(alert['alert_reasons'])}")
            if shadow_fills:
                append_shadow_fills(args.shadow_file, shadow_fills)
                print(f"appended_shadow_fills={len(shadow_fills)} shadow_file={args.shadow_file}")
                if store is not None:
                    store.insert_shadow_fills(shadow_fills)
                for fill in shadow_fills:
                    print(
                        "shadow_fill "
                        f"slug={fill['slug']} side={fill['side']} "
                        f"fill_price={float(fill['fill_price']):.4f} "
                        f"risk_amount={float(fill.get('portfolio_risk_amount') or fill.get('risk_amount') or 0):.4f} "
                        f"max_entry_price={float(fill['max_entry_price']):.4f}"
                    )
            for fill in blocked_shadow_fills:
                print(
                    "shadow_fill_blocked "
                    f"slug={fill.get('slug')} side={fill.get('side')} "
                    f"reasons={','.join(str(reason) for reason in fill.get('portfolio_risk_reasons', []))}"
                )
            if store is not None:
                inserted_marks = store.insert_shadow_marks(snapshots)
                if inserted_marks:
                    print(f"inserted_shadow_marks={inserted_marks} db_file={args.db_file}")

        if iteration < iterations and poll_seconds > 0:
            time.sleep(poll_seconds)



def _build_evidence_text(evidence, parsed) -> str:
    """
    将 Evidence 对象和 ParsedMarket 的关键字段转化为
    AI辩论Prompt中"可阅读的证据摘要"文本。

    evidence: bot.models.Evidence
    parsed:   bot.models.ParsedMarket
    """
    lines = []

    # 基础信号信息
    lines.append(f"[Evidence Summary]")
    lines.append(f"Event Type: {parsed.event_type}")
    lines.append(f"Subject: {parsed.subject}")
    lines.append(f"Platform: {parsed.platform}")
    lines.append(f"Action: {parsed.action}")
    lines.append(f"Days to Expiry: {parsed.days_to_expiry:.1f}")
    lines.append(f"Evidence Score (0-1): {evidence.score:.3f}")
    lines.append(f"Evidence Confidence (0-1): {evidence.confidence:.3f}")
    lines.append(f"Evidence Mode: {evidence.mode}")
    lines.append("")

    # 子分数（如果有）
    if evidence.preheat_score is not None:
        lines.append(f"Preheat Score (recent buzz): {evidence.preheat_score:.3f}")
    if evidence.cadence_score is not None:
        lines.append(f"Cadence Score (news volume): {evidence.cadence_score:.3f}")
    if evidence.partner_score is not None:
        lines.append(f"Partner Score (official sources): {evidence.partner_score:.3f}")
    if evidence.source_reliability is not None:
        lines.append(f"Source Reliability: {evidence.source_reliability:.3f}")

    # 数量指标
    if evidence.recent_entries_30d is not None:
        lines.append(f"Recent News Entries (30d): {evidence.recent_entries_30d}")
    if evidence.keyword_hits_30d is not None:
        lines.append(f"Keyword Hits (30d): {evidence.keyword_hits_30d}")
    if evidence.latest_entry_age_days is not None:
        lines.append(f"Latest Entry Age (days): {evidence.latest_entry_age_days:.1f}")
    if evidence.source_url:
        lines.append(f"Primary Source URL: {evidence.source_url}")
    if evidence.source_type:
        lines.append(f"Source Type: {evidence.source_type}")

    lines.append("")
    lines.append("[Evidence Reasons / Raw Signals]")
    for reason in (evidence.reasons or []):
        lines.append(f"  - {reason}")

    # 原始新闻详情
    if hasattr(evidence, "raw_entries") and evidence.raw_entries:
        lines.append("")
        lines.append("[Detailed News Entries]")
        for i, entry in enumerate(evidence.raw_entries, 1):
            title = entry.get("title", "No Title")
            summary = entry.get("summary", "")[:200] # 截断摘要
            pub_date = entry.get("published")
            lines.append(f"{i}. {title}")
            if pub_date:
                lines.append(f"   Date: {pub_date}")
            if summary:
                lines.append(f"   Summary: {summary}...")
            lines.append("")

    return "\n".join(lines)


def _build_config(args: argparse.Namespace, max_days_to_expiry: float | None = None) -> BotConfig:
    return BotConfig(
        max_days_to_expiry=max_days_to_expiry if max_days_to_expiry is not None else BotConfig.max_days_to_expiry,
        shadow_bankroll=args.shadow_bankroll,
        shadow_position_risk_pct=args.shadow_position_risk_pct,
        max_total_risk_pct=args.max_total_risk_pct,
        max_market_risk_pct=args.max_market_risk_pct,
        max_event_type_risk_pct=args.max_event_type_risk_pct,
        circuit_breaker_loss_pct=args.circuit_breaker_loss_pct,
        max_open_shadow_positions=args.max_open_shadow_positions,
    )


def _build_source_finder(args: argparse.Namespace):
    if not getattr(args, "llm_source_finder", False):
        return None
    from bot.evidence_source_finder import LlmEvidenceSourceFinder

    return LlmEvidenceSourceFinder(
        mode=args.llm_source_mode,
        inbox_dir=args.llm_source_inbox,
        outbox_dir=args.llm_source_outbox,
        max_sources=args.llm_source_max_sources,
    )


# ---------------------------------------------------------------------------
# Historical data helpers
# ---------------------------------------------------------------------------

def _run_fetch_history(args: argparse.Namespace) -> None:
    """Phase 1: Fetch resolved markets + CLOB price history."""
    keywords = [k.strip() for k in args.history_keywords.split(",") if k.strip()] or None
    fetcher = HistoricalFetcher(rate_limit_seconds=0.15)
    store = HistoricalStore(args.history_db)

    markets = fetcher.fetch_resolved_markets(
        keywords=keywords,
        min_volume=args.history_min_volume,
        start_date=args.history_start_date,
        max_markets=args.history_max_markets,
    )
    if not markets:
        print("historical_fetch no_markets_matched")
        return

    inserted = store.insert_markets(markets)
    print(f"historical_markets_inserted={inserted} total_in_db={store.market_count()}")

    already_fetched = store.market_ids_with_prices()
    to_fetch = [m for m in markets if m.market_id not in already_fetched]
    print(f"price_history_to_fetch={len(to_fetch)} already_have={len(already_fetched)}")

    total_bars = 0
    for i, market in enumerate(to_fetch):
        history = fetcher.fetch_price_history(market, fidelity_minutes=args.history_fidelity)
        for side, bars in history.items():
            if bars:
                inserted_bars = store.insert_prices(market.market_id, side, bars)
                total_bars += inserted_bars
        if (i + 1) % 25 == 0:
            print(f"price_fetch_progress={i+1}/{len(to_fetch)} bars_inserted_so_far={total_bars}")

    print(
        f"historical_fetch_done markets={store.market_count()} "
        f"price_bars={store.price_bar_count()} "
        f"history_db={args.history_db}"
    )


def _run_build_history_snapshots(args: argparse.Namespace) -> None:
    """Phase 2: Generate virtual snapshots from historical prices."""
    store = HistoricalStore(args.history_db)
    market_count = store.market_count()
    if market_count == 0:
        print("historical_snapshot_builder no_markets_in_db run --fetch-history first")
        return
    print(f"historical_snapshot_builder markets_in_db={market_count} price_bars={store.price_bar_count()}")
    total = build_historical_snapshots(store, config=None, verbose=True)
    print(
        f"build_history_snapshots_done total_snapshots={store.snapshot_count()} "
        f"newly_inserted={total} history_db={args.history_db}"
    )


def _run_history_backtest(args: argparse.Namespace) -> None:
    """Phase 3a: Backtest report from historical snapshots."""
    params = BacktestStrategyParams(
        min_net_edge=args.backtest_min_net_edge,
        max_spread=args.backtest_max_spread,
        model_profile=args.backtest_profile,
        event_type=args.backtest_event_type,
    )
    samples = load_historical_backtest_samples(
        history_db_path=args.history_db,
        params=params,
        start_date=args.backtest_from,
        end_date=args.backtest_to,
        max_days_before_close=args.history_max_days_before_close,
    )
    print(f"history_backtest_samples_loaded={len(samples)}")
    if not samples:
        print("history_backtest no_samples_found")
        return

    report = build_backtest_report(samples, args.history_db, args.backtest_min_samples)
    for line in format_backtest_report(report):
        print(line)

    if args.history_backtest_report:
        write_backtest_markdown(args.history_backtest_report, report)
        print(f"wrote_history_backtest_report={args.history_backtest_report}")
    if args.history_backtest_json:
        write_backtest_json(args.history_backtest_json, report)
        print(f"wrote_history_backtest_json={args.history_backtest_json}")


def _run_history_calibrate(args: argparse.Namespace) -> None:
    """Phase 3b: Calibration report from historical snapshots.

    We reuse the shadow calibration pipeline by temporarily inserting
    historical snapshots as shadow_fills in a throwaway in-memory connection,
    or more simply: we call build_calibration_report() against the
    historical.sqlite directly after ensuring it has a shadow_fills-compatible
    view.  The cleanest approach is to call the historical backtest loader and
    build a per-profile summary directly.
    """
    from collections import defaultdict
    import math

    samples = load_historical_backtest_samples(
        history_db_path=args.history_db,
        max_days_before_close=args.history_max_days_before_close,
    )
    print(f"history_calibrate_samples_loaded={len(samples)}")
    if not samples:
        print("history_calibrate no_samples_found")
        return

    # Group by model_profile and compute calibration stats
    by_profile: dict[str, list] = defaultdict(list)
    for s in samples:
        if s.p_model is not None and s.target_yes_probability is not None:
            by_profile[s.model_profile].append(s)

    min_samples = args.calibration_min_samples
    lines = [
        "history_calibration_report",
        f"history_db={args.history_db}",
        f"total_samples={len(samples)}",
        f"min_samples={min_samples}",
    ]

    from pathlib import Path
    md_lines = [
        "# Historical Model Calibration Report",
        "",
        f"- Source: `{args.history_db}`",
        f"- Total samples: `{len(samples)}`",
        f"- Min samples per profile: `{min_samples}`",
        "",
        "| profile | samples | avg p_model | avg target | error | brier | status |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for profile, profile_samples in sorted(by_profile.items()):
        p_models = [s.p_model for s in profile_samples if s.p_model is not None]
        targets = [s.target_yes_probability for s in profile_samples if s.target_yes_probability is not None]
        if not p_models or not targets:
            continue
        n = len(p_models)
        avg_p = round(sum(p_models) / n, 4)
        avg_t = round(sum(targets) / n, 4)
        brier = round(sum((p - t) ** 2 for p, t in zip(p_models, targets)) / n, 4)
        error = round(avg_t - avg_p, 4)
        status = "ok" if n >= min_samples else "insufficient_samples"

        # Logit-space bias correction suggestion
        def _logit(p: float) -> float:
            p = min(0.99, max(0.01, p))
            return math.log(p / (1.0 - p))

        suggested_base_delta = round(_logit(avg_t) - _logit(avg_p), 4) if n >= min_samples else None

        line = (
            f"history_calibration_profile profile={profile} status={status} "
            f"samples={n} avg_p_model={avg_p:.4f} avg_target={avg_t:.4f} "
            f"error={error:.4f} brier={brier:.4f}"
        )
        if suggested_base_delta is not None:
            line += f" suggested_base_logit_delta={suggested_base_delta:.4f}"
        lines.append(line)
        md_lines.append(
            f"| {profile} | {n} | {avg_p:.4f} | {avg_t:.4f} | {error:.4f} | {brier:.4f} | {status} |"
        )

    for line in lines:
        print(line)

    # Write markdown
    if args.history_calibration_file:
        target = Path(args.history_calibration_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        print(f"wrote_history_calibration_file={args.history_calibration_file}")

    # Write JSON
    if args.history_calibration_json:
        import json as _json
        payload = {
            "history_db": args.history_db,
            "total_samples": len(samples),
            "profiles": [
                {
                    "profile": profile,
                    "sample_count": len(ps),
                    "avg_p_model": round(sum(s.p_model for s in ps if s.p_model) / len(ps), 4),
                    "avg_target": round(sum(s.target_yes_probability for s in ps if s.target_yes_probability) / len(ps), 4),
                }
                for profile, ps in sorted(by_profile.items()) if ps
            ],
        }
        Path(args.history_calibration_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.history_calibration_json).write_text(
            _json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        print(f"wrote_history_calibration_json={args.history_calibration_json}")


if __name__ == "__main__":
    main()
