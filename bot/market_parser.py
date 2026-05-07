from __future__ import annotations

from datetime import datetime, timezone
import re

from bot.models import Market, ParsedMarket


SOCIAL_KEYWORDS = (
    "tweet",
    "post",
    "announce",
    "upload",
    "release",
    "released",
    "album",
    "song",
    "video",
    "trailer",
    "gpt",
    "chatgpt",
    "model",
    "ipo",
    "market cap",
    "optimus",
    "macbook",
    "cellular",
)
PLATFORM_KEYWORDS = {
    "x": ("twitter", "tweet", "on x", "post on x", "post on twitter"),
    "youtube": ("youtube", "video", "upload"),
    "instagram": ("instagram", "ig"),
    "streaming": ("album", "song", "spotify", "apple music"),
    "openai": ("openai", "gpt", "chatgpt"),
    "tesla": ("tesla", "optimus"),
    "apple": ("apple", "macbook", "cellular"),
    "finance": ("ipo", "market cap"),
}


def parse_market(market: Market, now: datetime) -> ParsedMarket | None:
    title_lower = market.title.lower()
    if not any(keyword in title_lower for keyword in SOCIAL_KEYWORDS):
        return None

    platform = "unknown"
    description_lower = market.description.lower()
    rules_lower = market.rules.lower()
    combined_text = f"{title_lower} {description_lower} {rules_lower}"
    for name, aliases in PLATFORM_KEYWORDS.items():
        if _contains_alias(combined_text, aliases):
            platform = name
            break

    event_type = "social_activity"
    action = "post"
    if "not ipo" in combined_text:
        event_type = "ipo_event"
        action = "not_ipo"
    elif "ipo" in combined_text or "market cap" in combined_text:
        event_type = "ipo_event"
        action = "ipo"
    elif "announce" in title_lower:
        event_type = "announcement"
        action = "announce"
    elif (
        "release" in title_lower
        or "released" in title_lower
        or "upload" in title_lower
        or "album" in title_lower
        or "song" in title_lower
        or "gpt" in combined_text
        or "optimus" in combined_text
        or "macbook" in combined_text
    ):
        event_type = "content_release"
        action = "release"

    subject = market.metadata.get("subject", "unknown")
    if subject == "unknown":
        subject = _extract_subject(market.title)

    days_to_expiry = (market.closes_at - now).total_seconds() / 86400
    return ParsedMarket(
        market=market,
        event_type=event_type,
        subject=subject,
        platform=platform,
        action=action,
        days_to_expiry=round(days_to_expiry, 2),
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_subject(title: str) -> str:
    patterns = (
        r"^will\s+(.+?)\s+be\s+released\b",
        r"^will\s+(.+?)\s+not\s+ipo\b",
        r"^will\s+(.+?)\s+ipo\b",
        r"^will\s+(.+?)\s+release\b",
        r"^will\s+(.+?)[＊'’]s\s+market\s+cap\b",
        r"^will\s+(.+?)\s+(announce|release|upload|post|tweet)\b",
        r"^new\s+(.+?)\s+(album|song|video|trailer)\b",
    )
    normalized = title.strip()
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return match.group(1).strip(" ?")
    return normalized.split(" ")[0]


def _contains_alias(text: str, aliases: tuple[str, ...]) -> bool:
    for alias in aliases:
        if " " in alias:
            if alias in text:
                return True
            continue
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return True
    return False
