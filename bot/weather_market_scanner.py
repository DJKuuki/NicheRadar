"""Weather Temperature Market Scanner & Multi-Model Probability Engine.

Scans Polymarket Daily Highest Temperature bracket markets (e.g. NYC, Chicago, London,
Seoul, Dallas, Miami, Toronto, Buenos Aires, etc.), queries multi-model meteorological
forecasts (GFS, ECMWF, ICON via Open-Meteo) at the official airport station coordinates,
computes continuous Gaussian / ensemble bracket probabilities with half-degree station
rounding continuity corrections, and generates TradeCandidate signals for ShadowEngine.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


@dataclass(frozen=True)
class WeatherStationSpec:
    city_key: str
    station_code: str
    latitude: float
    longitude: float
    unit: str  # "fahrenheit" or "celsius"
    timezone: str


# Official airport stations used by Polymarket / Wunderground temperature resolutions
WEATHER_STATIONS: dict[str, WeatherStationSpec] = {
    "nyc": WeatherStationSpec("nyc", "KLGA", 40.7769, -73.8740, "fahrenheit", "America/New_York"),
    "new york": WeatherStationSpec("nyc", "KLGA", 40.7769, -73.8740, "fahrenheit", "America/New_York"),
    "chicago": WeatherStationSpec("chicago", "KORD", 41.9742, -87.9073, "fahrenheit", "America/Chicago"),
    "dallas": WeatherStationSpec("dallas", "KDAL", 32.8471, -96.8518, "fahrenheit", "America/Chicago"),
    "miami": WeatherStationSpec("miami", "KMIA", 25.7959, -80.2870, "fahrenheit", "America/New_York"),
    "atlanta": WeatherStationSpec("atlanta", "KATL", 33.6407, -84.4277, "fahrenheit", "America/New_York"),
    "seattle": WeatherStationSpec("seattle", "KSEA", 47.4502, -122.3088, "fahrenheit", "America/Los_Angeles"),
    "los angeles": WeatherStationSpec("los angeles", "KLAX", 33.9416, -118.4085, "fahrenheit", "America/Los_Angeles"),
    "london": WeatherStationSpec("london", "EGLC", 51.5048, 0.0495, "celsius", "Europe/London"),
    "seoul": WeatherStationSpec("seoul", "RKSI", 37.4602, 126.4407, "celsius", "Asia/Seoul"),
    "toronto": WeatherStationSpec("toronto", "CYYZ", 43.6777, -79.6248, "celsius", "America/Toronto"),
    "buenos aires": WeatherStationSpec("buenos aires", "SAEZ", -34.8222, -58.5358, "celsius", "America/Argentina/Buenos_Aires"),
    "ankara": WeatherStationSpec("ankara", "LTAC", 40.1281, 32.9951, "celsius", "Europe/Istanbul"),
    "paris": WeatherStationSpec("paris", "LFPG", 49.0097, 2.5479, "celsius", "Europe/Paris"),
    "wellington": WeatherStationSpec("wellington", "NZWN", -41.3272, 174.8053, "celsius", "Pacific/Auckland"),
}


@dataclass(frozen=True)
class TempBracketSpec:
    low: float | None   # None means (-inf, high]
    high: float | None  # None means [low, +inf)
    unit: str           # "F" or "C"
    name: str
    market_slug: str = ""


@dataclass
class WeatherForecastSummary:
    station: WeatherStationSpec
    target_date: date
    metric: str  # "max" or "min"
    mean_temp: float
    std_temp: float
    model_values: list[float]

    @property
    def mean_max_temp(self) -> float:
        return self.mean_temp

    @property
    def std_max_temp(self) -> float:
        return self.std_temp


@dataclass
class TempBracketEvaluation:
    bracket: TempBracketSpec
    fair_prob: float
    market_ask: float | None
    market_bid: float | None
    edge_buy: float | None
    edge_maker: float | None
    recommendation: str
    target_maker_price: float | None = None
    kelly_fraction: float | None = None


@dataclass
class ScannedWeatherEvent:
    event_id: str
    title: str
    slug: str
    station: WeatherStationSpec
    target_date: date
    end_date: datetime
    markets: list[dict[str, Any]]
    brackets: list[TempBracketSpec]
    metric: str = "max"  # "max" (highest temperature) or "min" (lowest temperature)


class WeatherProbabilityModel:
    """Computes calibrated temperature bracket probabilities from multi-model forecasts."""

    def __init__(self, min_edge_threshold: float = 0.05, kelly_scale: float = 0.25):
        self.min_edge = min_edge_threshold
        self.kelly_scale = kelly_scale

    @staticmethod
    def _norm_cdf(x: float, mu: float, sigma: float) -> float:
        if sigma <= 0:
            return 1.0 if x >= mu else 0.0
        return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))

    def bracket_probability(
        self, bracket: TempBracketSpec, mean_temp: float, std_temp: float
    ) -> float:
        """Compute probability that integer-rounded station max temperature lands in bracket.

        Uses continuity correction: integer interval [L, H] corresponds to [L - 0.5, H + 0.5).
        """
        sigma = max(0.5, std_temp)
        low_bound = -math.inf if bracket.low is None else (bracket.low - 0.5)
        high_bound = math.inf if bracket.high is None else (bracket.high + 0.5)

        cdf_high = 1.0 if math.isinf(high_bound) else self._norm_cdf(high_bound, mean_temp, sigma)
        cdf_low = 0.0 if math.isinf(low_bound) else self._norm_cdf(low_bound, mean_temp, sigma)
        return max(0.0, min(1.0, cdf_high - cdf_low))

    def evaluate_bracket(
        self,
        bracket: TempBracketSpec,
        mean_temp: float,
        std_temp: float,
        market_ask: float | None = None,
        market_bid: float | None = None,
        tick_size: float = 0.01,
    ) -> TempBracketEvaluation:
        fair_prob = self.bracket_probability(bracket, mean_temp, std_temp)
        edge_buy = (fair_prob - market_ask) if market_ask is not None else None

        target_maker_price: float | None = None
        edge_maker: float | None = None
        if market_bid is not None and market_ask is not None:
            candidate_bid = round(market_bid + tick_size, 4)
            if candidate_bid >= market_ask:
                candidate_bid = market_bid
            target_maker_price = candidate_bid
            edge_maker = fair_prob - target_maker_price

        rec = "PASS"
        kelly = 0.0
        exec_price = target_maker_price if target_maker_price is not None else market_ask
        exec_edge = edge_maker if edge_maker is not None else edge_buy

        if exec_price is not None and exec_edge is not None and 0.01 < exec_price < 0.99:
            if exec_edge >= self.min_edge:
                b = (1.0 - exec_price) / exec_price
                q = 1.0 - fair_prob
                raw_kelly = max(0.0, (b * fair_prob - q) / b)
                kelly = round(raw_kelly * self.kelly_scale, 4)
                rec = "BUY_YES_MAKER" if target_maker_price is not None else "BUY_YES_TAKER"

        return TempBracketEvaluation(
            bracket=bracket,
            fair_prob=round(fair_prob, 4),
            market_ask=market_ask,
            market_bid=market_bid,
            edge_buy=round(edge_buy, 4) if edge_buy is not None else None,
            edge_maker=round(edge_maker, 4) if edge_maker is not None else None,
            recommendation=rec,
            target_maker_price=target_maker_price,
            kelly_fraction=kelly,
        )


class WeatherMarketScanner:
    def __init__(
        self,
        prob_model: WeatherProbabilityModel | None = None,
        timeout_sec: int = 15,
    ):
        self.model = prob_model or WeatherProbabilityModel()
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheRadar-WeatherScanner/1.0",
            "Accept": "application/json",
        })
        self._forecast_cache: dict[tuple[str, str], WeatherForecastSummary] = {}

    @classmethod
    def parse_weather_event_title(
        cls, title: str, slug: str = "", ref_year: int = 2026
    ) -> tuple[WeatherStationSpec, date, str] | None:
        """Extract (WeatherStationSpec, target_date, metric) from a temperature event title/slug."""
        combined = f"{title} {slug.replace('-', ' ')}".lower()
        if "temperature" not in combined:
            return None

        metric = "min" if "lowest" in combined else "max"

        matched_station: WeatherStationSpec | None = None
        for city_name, spec in sorted(WEATHER_STATIONS.items(), key=lambda x: -len(x[0])):
            if re.search(rf"\b{re.escape(city_name)}\b", combined):
                matched_station = spec
                break
        if matched_station is None:
            return None

        date_re = re.compile(
            r"\b(?:on\s+)?(?P<month>january|february|march|april|may|june|july|august|september|sept|october|november|december|"
            r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{2}))?",
            re.IGNORECASE,
        )
        m = date_re.search(combined)
        if not m:
            return None

        month_num = MONTH_MAP.get(m.group("month").lower())
        if not month_num:
            return None
        day_num = int(m.group("day"))
        year_num = int(m.group("year")) if m.group("year") else ref_year
        try:
            return matched_station, date(year_num, month_num, day_num), metric
        except ValueError:
            return None

    @classmethod
    def parse_temperature_bracket(cls, question: str, group_title: str = "", slug: str = "") -> TempBracketSpec | None:
        """Parse a temperature bracket question or groupItemTitle into TempBracketSpec."""
        for text in (group_title.strip(), question.strip()):
            if not text:
                continue

            # 1. Range pattern: "56-57°F", "-2--1°C", "between 60-61°F", "60 to 61°F"
            m_range = re.search(
                r"(?P<low>-?\d+)\s*(?:°\s*[FCfc])?\s*(?:[-–]|to)\s*(?P<high>-?\d+)\s*°?\s*(?P<unit>[FCfc])\b",
                text,
            )
            if m_range:
                low_v = float(m_range.group("low"))
                high_v = float(m_range.group("high"))
                if low_v <= high_v:
                    return TempBracketSpec(
                        low=low_v,
                        high=high_v,
                        unit=m_range.group("unit").upper(),
                        name=question or group_title,
                        market_slug=slug,
                    )

            # 2. Upper open tail: "62°F or higher", "18°C or above", "62°F or more"
            m_high = re.search(
                r"(?P<low>-?\d+)\s*°?\s*(?P<unit>[FCfc])\s*(?:or\s+(?:higher|above|more|warmer)|\+)",
                text,
                re.IGNORECASE,
            )
            if m_high:
                return TempBracketSpec(
                    low=float(m_high.group("low")),
                    high=None,
                    unit=m_high.group("unit").upper(),
                    name=question or group_title,
                    market_slug=slug,
                )

            # 3. Lower open tail: "49°F or below", "10°C or lower", "49°F or less", "under 49°F"
            m_low = re.search(
                r"(?P<high>-?\d+)\s*°?\s*(?P<unit>[FCfc])\s*or\s+(?:below|lower|less|colder)",
                text,
                re.IGNORECASE,
            )
            if m_low:
                return TempBracketSpec(
                    low=None,
                    high=float(m_low.group("high")),
                    unit=m_low.group("unit").upper(),
                    name=question or group_title,
                    market_slug=slug,
                )

            # 4. Exact single-degree bucket: "be 14°C on", "14°C"
            m_exact = re.search(
                r"(?:^|\bbe\s+)(?P<val>-?\d+)\s*°\s*(?P<unit>[FCfc])\b",
                text,
                re.IGNORECASE,
            )
            if m_exact:
                val = float(m_exact.group("val"))
                return TempBracketSpec(
                    low=val,
                    high=val,
                    unit=m_exact.group("unit").upper(),
                    name=question or group_title,
                    market_slug=slug,
                )

        return None

    def fetch_station_forecast(
        self, station: WeatherStationSpec, target_date: date, metric: str = "max"
    ) -> WeatherForecastSummary:
        """Query multi-model daily max or min temperature forecast from Open-Meteo."""
        daily_var = "temperature_2m_min" if metric == "min" else "temperature_2m_max"
        cache_key = (f"{station.city_key}_{metric}", target_date.isoformat())
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]

        params = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "daily": daily_var,
            "temperature_unit": station.unit,
            "timezone": station.timezone,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "models": "gfs_seamless,ecmwf_ifs025,icon_seamless",
        }
        resp = self.session.get(OPEN_METEO_FORECAST_URL, params=params, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})

        model_vals: list[float] = []
        for key, vals in daily.items():
            if key.startswith(daily_var) and isinstance(vals, list) and vals:
                v = vals[0]
                if isinstance(v, (int, float)) and math.isfinite(v):
                    model_vals.append(float(v))

        if not model_vals:
            raise ValueError(f"No valid temperature forecast returned for {station.city_key} on {target_date}")

        mean_val = sum(model_vals) / len(model_vals)
        if len(model_vals) > 1:
            model_var = sum((x - mean_val) ** 2 for x in model_vals) / (len(model_vals) - 1)
        else:
            model_var = 0.0

        # Combine inter-model variance with irreducible meteorological station variance
        today = datetime.now(timezone.utc).date()
        lead_days = max(0, (target_date - today).days)
        base_std = (1.8 + 0.4 * lead_days) if station.unit == "fahrenheit" else (1.0 + 0.25 * lead_days)
        combined_std = math.sqrt(model_var + base_std ** 2)

        summary = WeatherForecastSummary(
            station=station,
            target_date=target_date,
            metric=metric,
            mean_temp=round(mean_val, 2),
            std_temp=round(combined_std, 2),
            model_values=[round(v, 2) for v in model_vals],
        )
        self._forecast_cache[cache_key] = summary
        return summary

    def fetch_active_weather_events(self, max_pages: int = 8) -> list[ScannedWeatherEvent]:
        """Scan Gamma API for active daily highest/lowest temperature events."""
        raw_events: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # Query weather/temperature events via tag or general active scan
        for tag_slug in ("weather", "temperature", None):
            for page in range(max_pages if tag_slug is None else 2):
                params: dict[str, Any] = {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": page * 100,
                }
                if tag_slug:
                    params["tag_slug"] = tag_slug
                try:
                    resp = self.session.get(GAMMA_EVENTS_URL, params=params, timeout=self.timeout_sec)
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception:
                    break
                if not isinstance(batch, list) or not batch:
                    break
                for e in batch:
                    eid = str(e.get("id", ""))
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        raw_events.append(e)
                if len(batch) < 100:
                    break

        now = datetime.now(timezone.utc)
        scanned: list[ScannedWeatherEvent] = []
        for e in raw_events:
            title = str(e.get("title", ""))
            slug = str(e.get("slug", ""))
            parsed = self.parse_weather_event_title(title, slug, ref_year=now.year)
            if not parsed:
                continue
            station, target_dt, metric = parsed
            if target_dt < now.date():
                continue

            end_iso = e.get("endDate")
            try:
                end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00")) if end_iso else now
            except Exception:
                end_dt = now

            markets = e.get("markets", [])
            brackets: list[TempBracketSpec] = []
            for m in markets:
                spec = self.parse_temperature_bracket(
                    str(m.get("question", "")),
                    str(m.get("groupItemTitle", "")),
                    str(m.get("slug", "")),
                )
                if spec:
                    m["_parsed_temp_bracket"] = spec
                    brackets.append(spec)

            if brackets:
                scanned.append(
                    ScannedWeatherEvent(
                        event_id=str(e.get("id")),
                        title=title,
                        slug=slug,
                        station=station,
                        target_date=target_dt,
                        end_date=end_dt,
                        markets=markets,
                        brackets=brackets,
                        metric=metric,
                    )
                )
        return scanned

    def fetch_market_quote(self, market: dict[str, Any]) -> tuple[float, float] | None:
        """Read executable YES ask and bid from CLOB order book."""
        try:
            tokens = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", ["Yes", "No"])
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            token = tokens[[str(x).lower() for x in outcomes].index("yes")]
            resp = self.session.get(
                CLOB_BOOK_URL, params={"token_id": token}, timeout=self.timeout_sec
            )
            resp.raise_for_status()
            book = resp.json()
            asks = [
                float(x["price"])
                for x in book.get("asks", [])
                if float(x["size"]) > 0 and 0 < float(x["price"]) < 1
            ]
            bids = [
                float(x["price"])
                for x in book.get("bids", [])
                if float(x["size"]) > 0 and 0 < float(x["price"]) < 1
            ]
            if not asks or not bids or max(bids) >= min(asks):
                return None
            market["_yes_token_id"] = str(token)
            market["_tick_size"] = float(book.get("tick_size", "0.01"))
            market["_quote_at_utc"] = datetime.now(timezone.utc).isoformat()
            return min(asks), max(bids)
        except Exception:
            return None

    def scan_and_evaluate(
        self, max_pages: int = 8
    ) -> list[tuple[ScannedWeatherEvent, WeatherForecastSummary, list[TempBracketEvaluation]]]:
        """Full pipeline: scan active temperature events, query Open-Meteo multi-model forecasts, evaluate brackets."""
        events = self.fetch_active_weather_events(max_pages=max_pages)
        results: list[tuple[ScannedWeatherEvent, WeatherForecastSummary, list[TempBracketEvaluation]]] = []

        for ev in events:
            try:
                forecast = self.fetch_station_forecast(ev.station, ev.target_date, metric=ev.metric)
            except Exception as exc:
                logger.warning("Skipping weather event %s: forecast error: %s", ev.slug, exc)
                continue

            evals: list[TempBracketEvaluation] = []
            for m in ev.markets:
                if m.get("closed") or m.get("acceptingOrders") is False:
                    continue
                bracket: TempBracketSpec | None = m.get("_parsed_temp_bracket")
                if bracket is None:
                    continue
                if self.model.bracket_probability(bracket, forecast.mean_temp, forecast.std_temp) < 0.015:
                    continue
                quote = self.fetch_market_quote(m)
                if quote is None:
                    continue
                ask, bid = quote
                ev_res = self.model.evaluate_bracket(
                    bracket=bracket,
                    mean_temp=forecast.mean_temp,
                    std_temp=forecast.std_temp,
                    market_ask=ask,
                    market_bid=bid,
                    tick_size=m.get("_tick_size", 0.01),
                )
                now_iso = datetime.now(timezone.utc).isoformat()
                m["_model_context"] = {
                    "station_code": ev.station.station_code,
                    "target_date": ev.target_date.isoformat(),
                    "window_end": ev.end_date.isoformat(),
                    "count_observed_at": now_iso,
                    "metric": ev.metric,
                    "mean_temp": forecast.mean_temp,
                    "std_temp": forecast.std_temp,
                    "model_values": forecast.model_values,
                    "quote_at": m.get("_quote_at_utc") or now_iso,
                    "quote_ask": ask,
                    "quote_bid": bid,
                    "rewardsMinSize": m.get("rewardsMinSize", 0),
                    "rewardsMaxSpread": m.get("rewardsMaxSpread", 0),
                    "holdingRewardsEnabled": m.get("holdingRewardsEnabled", False),
                    "feesEnabled": m.get("feesEnabled", False),
                    "feeType": m.get("feeType", "zero_fees"),
                    "feeSchedule": m.get("feeSchedule", {}),
                }
                evals.append(ev_res)
            results.append((ev, forecast, evals))

        return results


def candidates_from_weather_results(
    scan_results: list[tuple[ScannedWeatherEvent, WeatherForecastSummary, list[TempBracketEvaluation]]],
    min_edge: float = 0.05,
    min_entry_price: float = 0.10,
    max_entry_price: float = 0.65,
    top_k_per_event: int | None = 1,
) -> list[Any]:
    """Convert WeatherMarketScanner evaluation tuples into ShadowEngine TradeCandidate objects."""
    from bot.shadow_engine import TradeCandidate

    candidates_by_event: dict[str, list[TradeCandidate]] = {}

    for ev, _, evals in scan_results:
        market_by_slug = {m["slug"]: m for m in ev.markets if m.get("slug")}
        market_by_q = {m["question"]: m for m in ev.markets if m.get("question")}
        candidates_by_event.setdefault(ev.event_id, [])

        for b_eval in evals:
            edge = b_eval.edge_maker if b_eval.edge_maker is not None else (b_eval.edge_buy or 0.0)
            if edge < min_edge or not b_eval.recommendation.startswith("BUY_YES"):
                continue

            m = market_by_slug.get(b_eval.bracket.market_slug) or market_by_q.get(b_eval.bracket.name)
            if not m:
                continue

            target_px = (
                b_eval.target_maker_price
                or b_eval.market_bid
                or b_eval.market_ask
                or round(b_eval.fair_prob - edge, 3)
            )
            if not (min_entry_price <= target_px <= max_entry_price):
                continue

            mkt_prob = b_eval.market_ask or b_eval.market_bid or target_px
            candidates_by_event[ev.event_id].append(
                TradeCandidate(
                    event_id=ev.event_id,
                    event_slug=ev.slug,
                    market_id=str(m.get("id") or ""),
                    condition_id=str(m.get("conditionId") or ""),
                    clob_token_id=str(m.get("_yes_token_id") or ""),
                    bracket_name=b_eval.bracket.name,
                    side="BUY_YES",
                    model_prob=round(b_eval.fair_prob, 4),
                    market_prob=round(mkt_prob, 4),
                    net_edge=round(edge, 4),
                    kelly_fraction=round(b_eval.kelly_fraction or 0.05, 4),
                    target_price=round(target_px, 6),
                    metadata=m.get("_model_context", {}),
                )
            )

    selected: list[TradeCandidate] = []
    for _, cands in candidates_by_event.items():
        cands.sort(key=lambda c: (c.net_edge, c.model_prob), reverse=True)
        if top_k_per_event is not None:
            selected.extend(cands[:top_k_per_event])
        else:
            selected.extend(cands)
    return selected

