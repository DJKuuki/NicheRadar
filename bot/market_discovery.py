from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot.api import PolymarketApiClient, GammaMarketQuery
from bot.market_scanner import _market_from_gamma

logger = logging.getLogger(__name__)

class DiscoveryEngine:
    def __init__(
        self,
        watchlist_path: str = "data/watchlist.json",
        gamma_cache_seconds: int = 1800,  # 默认 30 分钟，与主循环一致可在启动时统一配置
    ):
        self.watchlist_path = Path(watchlist_path)
        self.client = PolymarketApiClient(gamma_cache_seconds=gamma_cache_seconds)

    def discover_and_append(
        self, 
        limit: int = 100,
        min_volume: float = 5000.0,       # 最低成交量 $5K（剔除冷清市场）
        min_liquidity: float = 1000.0,    # 最低流动性 $1K（实际可交易的门槛）
        min_days_remaining: int = 7,
        max_spread: float = 0.15,
        preferred_categories: list[str] | None = None
    ) -> list[str]:
        """
        Discover niche markets and append them to the watchlist if they match criteria.
        Returns a list of added slugs.
        过滤逻辑优先级：
          1. 成交量下限（剔除冷清市场）
          2. 流动性下限（确保可实际交易）
          3. 价差上限（剔除流动性差的市场）
          4. 时间剩余
          5. 分类和关键词过滤
        """
        if preferred_categories is None:
            preferred_categories = ["Business", "Technology", "Science", "Politics", "Pop Culture", "Crypto", "Entertainment", "None"]

        # 体育项目排除关键词
        sports_exclude = ["win the", "finals", "cup", "nhl", "nba", "fifa", "mlb", "nfl", "championship", "tournament"]

        # 1. Load existing watchlist
        if not self.watchlist_path.exists():
            existing_watchlist = []
        else:
            try:
                existing_watchlist = json.loads(self.watchlist_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("discovery_error failed_to_load_watchlist path=%s err=%s", self.watchlist_path, e)
                existing_watchlist = []

        existing_slugs = {item["slug"] for item in existing_watchlist if "slug" in item}

        # 2. Fetch live markets
        logger.info("discovery_start limit=%d", limit)
        try:
            raw_markets = self.client.list_markets(GammaMarketQuery(limit=limit, closed=False))
        except Exception as e:
            logger.error("discovery_fetch_failed err=%s", e)
            return []

        added_slugs = []
        new_entries = []

        now = datetime.now(timezone.utc)

        for row in raw_markets:
            slug = row.get("slug")
            if not slug or slug in existing_slugs:
                continue

            market = _market_from_gamma(row)
            if not market:
                continue

            # 3. 成交量下限（剔除无人问津的市场）
            if market.volume < min_volume:
                continue

            # 4. 流动性检查（从原始 row 获取，market 对象中可能没有）
            liquidity = row.get("liquidity") or row.get("liquidityNum")
            if liquidity is not None:
                try:
                    if float(liquidity) < min_liquidity:
                        continue
                except (ValueError, TypeError):
                    pass

            # 5. 类别和标题过滤
            title_lower = market.title.lower()
            if any(se in title_lower for se in sports_exclude):
                continue

            cat = str(market.category or "None")
            if not any(pc.lower() in cat.lower() for pc in preferred_categories):
                continue

            # 6. 剩余时间
            days_left = (market.closes_at - now).total_seconds() / 86400
            if days_left < min_days_remaining:
                continue

            # 7. 价差
            spread = row.get("spread")
            if spread is not None:
                try:
                    if float(spread) > max_spread:
                        continue
                except (ValueError, TypeError):
                    pass

            # 8. 构建 watchlist 条目
            liq_display = f"Liq=${float(liquidity)/1000:.1f}K" if liquidity else "Liq=N/A"
            new_entry = {
                "slug": slug,
                "label": market.title[:50],
                "preferred_side": "BUY_YES",
                "entry_band_low": 0.30,
                "entry_band_high": 0.70,
                "note": f"AUTO_DISCOVERED. Vol=${market.volume/1000:.1f}K. {liq_display}. Cat={cat}. Generated on {now.date()}."
            }
            
            new_entries.append(new_entry)
            added_slugs.append(slug)
            logger.info("discovery_match slug=%s title=%s vol=%f", slug, market.title, market.volume)

        # 9. 保存更新后的 watchlist
        if new_entries:
            combined = existing_watchlist + new_entries
            try:
                self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
                self.watchlist_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info("discovery_success added_count=%d", len(new_entries))
            except Exception as e:
                logger.error("discovery_save_failed err=%s", e)
                return []

        return added_slugs
