from __future__ import annotations

from datetime import datetime, timezone

from bot.evidence_collector import EvidenceCollector, EvidenceSource
from bot.evidence_source_finder import SourceSuggestion
from bot.market_parser import parse_market
from bot.models import Market


class StubFinder:
    def __init__(self) -> None:
        self.calls = 0

    def find_sources(self, parsed):
        self.calls += 1
        return [
            SourceSuggestion(
                source_type="rss",
                url="https://example.com/openai-wearable.xml",
                keywords=["wearable", "necklace"],
                reliability=0.8,
                rationale="market-specific source",
            )
        ]


class StubCollector(EvidenceCollector):
    def __init__(self, *args, entries, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._entries = entries

    def _fetch_feed_entries(self, url: str):
        return self._entries[url]


def _parsed_market():
    parsed = parse_market(
        Market(
            market_id="mkt-1",
            title="Will OpenAI announce a necklace-style wearable in 2026?",
            description="Official OpenAI announcement required.",
            rules="",
            category="technology",
            closes_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            volume=10000,
            yes_bid=0.18,
            yes_ask=0.20,
            no_bid=0.80,
            no_ask=0.82,
        ),
        datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert parsed is not None
    return parsed


def test_collector_uses_llm_source_when_registry_source_mismatches_market() -> None:
    finder = StubFinder()
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    collector = StubCollector(
        source_finder=finder,
        llm_source_policy="missing_or_mismatch",
        entries={
            "https://example.com/openai-wearable.xml": [
                {
                    "title": "OpenAI wearable necklace prototype reportedly advances",
                    "summary": "Hardware work continues on a necklace-style wearable.",
                    "published": datetime(2026, 5, 10, tzinfo=timezone.utc),
                }
            ],
            "https://example.com/openai-ipo.xml": [
                {
                    "title": "OpenAI IPO valuation update",
                    "summary": "IPO market cap speculation.",
                    "published": datetime(2026, 5, 10, tzinfo=timezone.utc),
                }
            ],
        },
    )
    collector.sources = [
        EvidenceSource(
            subject="OpenAI",
            platform="openai",
            source_type="rss",
            url="https://example.com/openai-ipo.xml",
            keywords=["ipo", "market cap"],
            reliability=0.78,
        )
    ]

    evidence = collector.collect(_parsed_market(), now)

    assert finder.calls == 1
    assert evidence.source_url == "https://example.com/openai-wearable.xml"
    assert evidence.mode == "source"
    assert evidence.keyword_hits_30d == 1
    assert "evidence_source_origin=llm" in evidence.reasons


def test_collector_skips_mismatched_registry_when_llm_has_no_result() -> None:
    finder = StubFinder()
    finder.find_sources = lambda parsed: []
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    collector = StubCollector(
        source_finder=finder,
        llm_source_policy="missing_or_mismatch",
        entries={
            "https://example.com/openai-ipo.xml": [
                {
                    "title": "OpenAI IPO valuation update",
                    "summary": "IPO market cap speculation.",
                    "published": datetime(2026, 5, 10, tzinfo=timezone.utc),
                }
            ]
        },
    )
    collector.sources = [
        EvidenceSource(
            subject="OpenAI",
            platform="openai",
            source_type="rss",
            url="https://example.com/openai-ipo.xml",
            keywords=["ipo", "market cap"],
            reliability=0.78,
        )
    ]

    evidence = collector.collect(_parsed_market(), now)

    assert evidence.mode == "fallback"
    assert evidence.source_url is None
    assert "llm_source_finder=no_sources" in evidence.reasons


def test_collector_does_not_call_llm_when_registry_source_matches_market() -> None:
    finder = StubFinder()
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    collector = StubCollector(
        source_finder=finder,
        llm_source_policy="missing_or_mismatch",
        entries={
            "https://example.com/openai-wearable.xml": [
                {
                    "title": "OpenAI wearable necklace update",
                    "summary": "Official announcement watch.",
                    "published": datetime(2026, 5, 10, tzinfo=timezone.utc),
                }
            ]
        },
    )
    collector.sources = [
        EvidenceSource(
            subject="OpenAI",
            platform="openai",
            source_type="rss",
            url="https://example.com/openai-wearable.xml",
            keywords=["wearable", "necklace"],
            reliability=0.8,
        )
    ]

    evidence = collector.collect(_parsed_market(), now)

    assert finder.calls == 0
    assert evidence.source_url == "https://example.com/openai-wearable.xml"
    assert "evidence_source_origin=registry" in evidence.reasons
