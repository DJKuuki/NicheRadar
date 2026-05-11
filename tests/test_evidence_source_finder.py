from __future__ import annotations

from datetime import datetime, timezone

from bot.evidence_source_finder import (
    LlmEvidenceSourceFinder,
    build_source_finder_prompt,
    parse_source_suggestions,
)
from bot.market_parser import parse_market
from bot.models import Market


def _parsed_market():
    parsed = parse_market(
        Market(
            market_id="mkt-1",
            title="Will OpenAI announce a necklace-style wearable in 2026?",
            description="Resolves YES if OpenAI announces a wearable hardware device worn as a necklace.",
            rules="Official OpenAI announcement required.",
            category="technology",
            closes_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            volume=10000,
            yes_bid=0.18,
            yes_ask=0.20,
            no_bid=0.80,
            no_ask=0.82,
            metadata={"slug": "openai-necklace"},
        ),
        datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    assert parsed is not None
    return parsed


def test_parse_source_suggestions_from_markdown_json() -> None:
    response = '''```json
{
  "sources": [
    {
      "source_type": "rss",
      "url": "https://news.google.com/rss/search?q=OpenAI+wearable+necklace&hl=en-US&gl=US&ceid=US:en",
      "keywords": ["openai wearable", "necklace", "hardware"],
      "reliability": 0.77,
      "rationale": "specific to the target market"
    },
    {
      "source_type": "html",
      "url": "file:///bad",
      "keywords": ["bad"],
      "reliability": 1.0
    }
  ]
}
```'''

    suggestions = parse_source_suggestions(response)

    assert len(suggestions) == 1
    assert suggestions[0].source_type == "rss"
    assert "necklace" in suggestions[0].keywords
    assert suggestions[0].reliability == 0.77


def test_build_source_finder_prompt_contains_market_specific_terms() -> None:
    prompt = build_source_finder_prompt(_parsed_market(), max_sources=2)

    assert "necklace-style wearable" in prompt
    assert "OpenAI" in prompt
    assert "Return ONLY valid JSON" in prompt
    assert "Google News RSS" in prompt


def test_batch_source_finder_writes_prompt_when_result_missing(tmp_path) -> None:
    finder = LlmEvidenceSourceFinder(
        mode="batch",
        inbox_dir=str(tmp_path / "inbox"),
        outbox_dir=str(tmp_path / "outbox"),
    )

    suggestions = finder.find_sources(_parsed_market())

    assert suggestions == []
    prompt_files = list((tmp_path / "inbox").glob("*.txt"))
    assert len(prompt_files) == 1
    assert "necklace-style wearable" in prompt_files[0].read_text(encoding="utf-8")


def test_batch_source_finder_reads_result(tmp_path) -> None:
    parsed = _parsed_market()
    finder = LlmEvidenceSourceFinder(
        mode="batch",
        inbox_dir=str(tmp_path / "inbox"),
        outbox_dir=str(tmp_path / "outbox"),
    )
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "source_openai-necklace_result.json").write_text(
        '{"sources":[{"source_type":"rss","url":"https://example.com/feed.xml","keywords":["openai wearable"],"reliability":0.8}]}',
        encoding="utf-8",
    )

    suggestions = finder.find_sources(parsed)

    assert len(suggestions) == 1
    assert suggestions[0].url == "https://example.com/feed.xml"
