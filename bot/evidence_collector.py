from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from bot.http_cache import HttpCache, RateLimiter
from bot.models import Evidence, ParsedMarket


@dataclass(frozen=True)
class EvidenceSource:
    subject: str
    platform: str
    source_type: str
    url: str
    keywords: list[str]
    reliability: float


class EvidenceCollector:
    def __init__(
        self,
        registry_path: str | None = None,
        timeout: int = 10,
        retries: int = 3,
        retry_backoff: float = 0.75,
        cache_path: str | None = None,
        cache_seconds: float = 0,
        rate_limit_seconds: float = 0,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.cache_seconds = cache_seconds
        self.cache = HttpCache(cache_path) if cache_path else None
        self.rate_limiter = RateLimiter(rate_limit_seconds)
        self.sources = self._load_registry(registry_path)

    def collect(self, parsed: ParsedMarket, now: datetime) -> Evidence:
        source = self._find_source(parsed)
        if source is None:
            return self._fallback_evidence(parsed)

        if source.source_type not in {"rss", "atom"}:
            return self._fallback_evidence(
                parsed,
                source=source,
                extra_reason=f"unsupported_source_type={source.source_type}",
            )

        try:
            entries = self._fetch_feed_entries(source.url)
        except Exception as exc:
            return self._fallback_evidence(parsed, source=source, extra_reason=f"source_fetch_failed={type(exc).__name__}")

        recent_entries = [entry for entry in entries if self._days_since(entry["published"], now) <= 30]
        keyword_hits = sum(1 for entry in recent_entries if self._entry_matches(entry, source.keywords))
        latest_age = min((self._days_since(entry["published"], now) for entry in recent_entries), default=365.0)

        cadence = min(0.95, len(recent_entries) / 10)
        preheat = min(0.95, keyword_hits / 3) if source.keywords else min(0.5, len(recent_entries) / 10)
        partner = self._partner_score(parsed)
        score = (preheat * 0.45) + (cadence * 0.35) + (partner * 0.20)
        confidence = min(0.95, 0.45 + source.reliability * 0.35 + min(0.15, len(recent_entries) * 0.01))

        reasons = [
            f"evidence_source={source.url}",
            f"recent_entries_30d={len(recent_entries)}",
            f"keyword_hits_30d={keyword_hits}",
            f"latest_entry_age_days={latest_age:.1f}",
            f"preheat_score={preheat:.2f}",
            f"cadence_score={cadence:.2f}",
            f"partner_score={partner:.2f}",
            f"source_reliability={source.reliability:.2f}",
        ]
        return Evidence(
            score=round(score, 4),
            confidence=round(confidence, 4),
            reasons=reasons,
            mode="source",
            source_url=source.url,
            source_type=source.source_type,
            recent_entries_30d=len(recent_entries),
            keyword_hits_30d=keyword_hits,
            latest_entry_age_days=round(latest_age, 1),
            preheat_score=round(preheat, 4),
            cadence_score=round(cadence, 4),
            partner_score=round(partner, 4),
            source_reliability=round(source.reliability, 4),
        )

    def _find_source(self, parsed: ParsedMarket) -> EvidenceSource | None:
        subject = parsed.subject.lower()
        platform = parsed.platform.lower()
        for source in self.sources:
            if source.subject.lower() == subject and source.platform.lower() == platform:
                return source
        return None

    def _load_registry(self, registry_path: str | None) -> list[EvidenceSource]:
        if not registry_path:
            return []
        path = Path(registry_path)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources: list[EvidenceSource] = []
        for row in payload:
            sources.append(
                EvidenceSource(
                    subject=str(row["subject"]),
                    platform=str(row["platform"]),
                    source_type=str(row["source_type"]),
                    url=str(row["url"]),
                    keywords=[str(item).lower() for item in row.get("keywords", [])],
                    reliability=float(row.get("reliability", 0.6)),
                )
            )
        return sources

    def _fetch_feed_entries(self, url: str) -> list[dict[str, Any]]:
        cached = self.cache.get(url) if self.cache is not None else None
        if cached is not None:
            raw_text = cached
        else:
            raw_text = self._fetch_url_text(url)
            if self.cache is not None:
                self.cache.set(url, raw_text, self.cache_seconds)
        root = ET.fromstring(raw_text)
        entries: list[dict[str, Any]] = []

        for item in root.findall(".//item"):
            entries.append(
                {
                    "title": self._xml_text(item, "title"),
                    "summary": self._xml_text(item, "description"),
                    "published": self._parse_feed_date(self._xml_text(item, "pubDate")),
                }
            )

        atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", atom_ns):
            entries.append(
                {
                    "title": self._xml_text(entry, "atom:title", atom_ns),
                    "summary": self._xml_text(entry, "atom:summary", atom_ns) or self._xml_text(entry, "atom:content", atom_ns),
                    "published": self._parse_feed_date(
                        self._xml_text(entry, "atom:published", atom_ns) or self._xml_text(entry, "atom:updated", atom_ns)
                    ),
                }
            )
        return [entry for entry in entries if entry["published"] is not None]

    def _fetch_url_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "PolyMarketShadowBot/0.1"})
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self.rate_limiter.wait()
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.retry_backoff * attempt)
        assert last_error is not None
        raise last_error

    def _fallback_evidence(
        self,
        parsed: ParsedMarket,
        source: EvidenceSource | None = None,
        extra_reason: str | None = None,
    ) -> Evidence:
        preheat = self._infer_preheat(parsed)
        cadence = self._infer_cadence(parsed)
        partner = self._partner_score(parsed)
        reliability = float(parsed.market.metadata.get("source_reliability", 0.45))

        score = (preheat * 0.4) + (cadence * 0.3) + (partner * 0.3)
        confidence = min(0.8, 0.35 + reliability * 0.5)
        reasons = [
            "evidence_mode=fallback",
            f"preheat_score={preheat:.2f}",
            f"cadence_score={cadence:.2f}",
            f"partner_score={partner:.2f}",
            f"source_reliability={reliability:.2f}",
        ]
        if extra_reason:
            reasons.append(extra_reason)
        return Evidence(
            score=round(score, 4),
            confidence=round(confidence, 4),
            reasons=reasons,
            mode="fallback",
            source_url=source.url if source else None,
            source_type=source.source_type if source else None,
            preheat_score=round(preheat, 4),
            cadence_score=round(cadence, 4),
            partner_score=round(partner, 4),
            source_reliability=round(reliability, 4),
        )

    def _entry_matches(self, entry: dict[str, Any], keywords: list[str]) -> bool:
        haystack = f"{entry['title']} {entry['summary']}".lower()
        return any(keyword in haystack for keyword in keywords)

    def _days_since(self, published: datetime | None, now: datetime) -> float:
        if published is None:
            return 365.0
        return max(0.0, (now - published).total_seconds() / 86400)

    def _parse_feed_date(self, raw: str) -> datetime | None:
        if not raw:
            return None
        cleaned = raw.strip()
        patterns = (
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        for pattern in patterns:
            try:
                parsed = datetime.strptime(cleaned, pattern)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                continue
        if cleaned.endswith(" GMT"):
            return self._parse_feed_date(cleaned.replace(" GMT", " +0000"))
        return None

    def _xml_text(self, node: ET.Element, path: str, namespace: dict[str, str] | None = None) -> str:
        found = node.find(path, namespace or {})
        if found is None or found.text is None:
            return ""
        return found.text.strip()

    def _infer_preheat(self, parsed: ParsedMarket) -> float:
        text = f"{parsed.market.title} {parsed.market.description}".lower()
        keywords = ("official", "new", "before", "release", "announce", "album", "video")
        hits = sum(1 for keyword in keywords if keyword in text)
        return min(0.9, 0.1 * hits)

    def _infer_cadence(self, parsed: ParsedMarket) -> float:
        if parsed.event_type == "ipo_event":
            return 0.25
        if parsed.event_type == "content_release":
            return 0.45
        if parsed.event_type == "announcement":
            return 0.35
        return 0.25

    def _partner_score(self, parsed: ParsedMarket) -> float:
        text = parsed.market.description.lower()
        if "official" in text and ("spotify" in text or "apple music" in text or "youtube" in text):
            return 0.4
        return 0.1
