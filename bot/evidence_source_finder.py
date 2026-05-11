from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import quote_plus, urlparse

from bot.llm_file_exchange import (
    LlmFileExchange,
    read_pasted_response,
    strip_markdown_code_blocks,
)
from bot.models import ParsedMarket


@dataclass(frozen=True)
class SourceSuggestion:
    source_type: str
    url: str
    keywords: list[str]
    reliability: float
    rationale: str = ""


class LlmEvidenceSourceFinder:
    def __init__(
        self,
        mode: str = "batch",
        inbox_dir: str = "logs/source_inbox",
        outbox_dir: str = "logs/source_outbox",
        max_sources: int = 3,
    ) -> None:
        if mode not in {"terminal", "batch"}:
            raise ValueError(f"Unknown LLM source finder mode: {mode}")
        self.mode = mode
        # Source-finder convention: prompts are .txt, results are JSON
        # (e.g. ``<id>_result.json``). The exchange object encapsulates that.
        self._exchange = LlmFileExchange(
            inbox_dir,
            outbox_dir,
            prompt_suffix=".txt",
            result_suffix="_result.json",
        )
        self.max_sources = max(1, max_sources)

    @property
    def inbox_dir(self):  # backwards-compat for callers reading the path
        return self._exchange.inbox_dir

    @property
    def outbox_dir(self):
        return self._exchange.outbox_dir

    def find_sources(self, parsed: ParsedMarket) -> list[SourceSuggestion]:
        prompt = build_source_finder_prompt(parsed, self.max_sources)
        request_id = _request_id(parsed)

        if self.mode == "terminal":
            response = _prompt_via_terminal(prompt, request_id)
            return parse_source_suggestions(response, self.max_sources)

        prompt_path = self._exchange.write_prompt(request_id, prompt)
        result = self._exchange.read_result(request_id)
        if result is None:
            result_path = self._exchange.outbox_path(request_id)
            print(f"llm_source_prompt_written path={prompt_path}")
            print(f"llm_source_result_expected path={result_path}")
            return []
        return parse_source_suggestions(result, self.max_sources)


def build_source_finder_prompt(parsed: ParsedMarket, max_sources: int = 3) -> str:
    market = parsed.market
    query_terms = _default_query_terms(parsed)
    fallback_url = _google_news_rss_url(query_terms)
    return f"""You are helping a Polymarket evidence bot find reliable public evidence sources.

Market title: {market.title}
Market description: {market.description or "(empty)"}
Market rules: {market.rules or "(empty)"}
Subject: {parsed.subject}
Platform: {parsed.platform}
Event type: {parsed.event_type}
Action: {parsed.action}
Days to expiry: {parsed.days_to_expiry}

Task:
Find up to {max_sources} highly relevant public RSS or Atom feeds that can monitor whether this exact event is becoming more likely.
Prefer official sources, company blogs, SEC/investor feeds for IPOs, product/newsroom RSS feeds, and precise Google News RSS queries.
Avoid generic sources whose keywords are about a different market for the same subject.

If you use Google News RSS, make the query specific to this market. A reasonable fallback query would be:
{fallback_url}

Return ONLY valid JSON in this schema:
{{
  "sources": [
    {{
      "source_type": "rss",
      "url": "https://...",
      "keywords": ["keyword or phrase", "another phrase"],
      "reliability": 0.75,
      "rationale": "why this source is relevant"
    }}
  ]
}}
"""


def parse_source_suggestions(response: str, max_sources: int = 3) -> list[SourceSuggestion]:
    if not response.strip():
        return []
    payload = _load_json_object(strip_markdown_code_blocks(response))
    rows = payload.get("sources", [])
    if not isinstance(rows, list):
        return []

    suggestions: list[SourceSuggestion] = []
    seen_urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_type = str(row.get("source_type") or "rss").lower().strip()
        if source_type not in {"rss", "atom"}:
            continue
        url = str(row.get("url") or "").strip()
        if not _is_safe_http_url(url) or url in seen_urls:
            continue
        raw_keywords = row.get("keywords", [])
        if not isinstance(raw_keywords, list):
            raw_keywords = []
        keywords = []
        for keyword in raw_keywords:
            cleaned = str(keyword).lower().strip()
            if cleaned and cleaned not in keywords:
                keywords.append(cleaned)
        if not keywords:
            continue
        reliability = _bounded_float(row.get("reliability"), 0.65, 0.1, 0.95)
        rationale = str(row.get("rationale") or "").strip()[:300]
        suggestions.append(
            SourceSuggestion(
                source_type=source_type,
                url=url,
                keywords=keywords[:12],
                reliability=reliability,
                rationale=rationale,
            )
        )
        seen_urls.add(url)
        if len(suggestions) >= max(1, max_sources):
            break
    return suggestions


def _prompt_via_terminal(prompt_text: str, request_id: str) -> str:
    print("\n" + "=" * 70)
    print(f"  LLM source finder request | request_id={request_id}")
    print("=" * 70)
    print(prompt_text)
    print("\nPaste the LLM JSON response below, then enter END_OF_AI_RESPONSE:")
    return read_pasted_response()


def _default_query_terms(parsed: ParsedMarket) -> str:
    market = parsed.market
    text = " ".join(
        part for part in [parsed.subject, parsed.platform, parsed.action, market.title] if part and part != "unknown"
    )
    return " ".join(text.split())


def _google_news_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


def _request_id(parsed: ParsedMarket) -> str:
    slug = str(parsed.market.metadata.get("slug") or parsed.market.market_id or parsed.market.title)
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", slug).strip("-").lower()
    return f"source_{slug[:80]}"


def _load_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _is_safe_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _bounded_float(value: object, default: float, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))
