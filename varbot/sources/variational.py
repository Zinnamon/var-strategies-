"""Variational Omni public stats.

Source: GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats
(documented at docs.variational.io/technical-documentation/api). One response carries
every listing with mark price, 24h volume, long/short open interest, funding terms and
the OLP's indicative quotes at fixed notionals (base, $1k, $100k, $1m). All numbers come
back as strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from varbot.config import FIXTURES_DIR, SETTINGS
from varbot.http import OfflineError, http
from varbot.models import Listing, QuoteTier, VariationalImpact

log = logging.getLogger(__name__)

TIER_NOTIONAL = {"base": None, "size_1k": 1_000.0, "size_100k": 100_000.0, "size_1m": 1_000_000.0}
FIXTURE = FIXTURES_DIR / "variational_stats_v2.json"


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_listing(raw: dict) -> Listing:
    oi = raw.get("open_interest") or {}
    funding = raw.get("funding") or {}
    last = funding.get("last") or {}
    quotes_raw = raw.get("quotes") or {}
    tiers: list[QuoteTier] = []
    for tier, notional in TIER_NOTIONAL.items():
        q = quotes_raw.get(tier)
        if not q:
            continue
        bid, ask = _f(q.get("bid")), _f(q.get("ask"))
        if bid is None or ask is None:
            continue
        tiers.append(QuoteTier(tier=tier, notional_usd=notional, bid=bid, ask=ask))
    return Listing(
        ticker=raw["ticker"],
        contract_type=raw.get("contract_type", "perpetual"),
        name=raw.get("name"),
        mark_price=_f(raw.get("mark_price")) or 0.0,
        volume_24h=_f(raw.get("volume_24h")) or 0.0,
        long_oi_usd=_f(oi.get("long_open_interest")) or 0.0,
        short_oi_usd=_f(oi.get("short_open_interest")) or 0.0,
        funding_interval_rate=_f(last.get("interval_rate")),
        funding_annualized_rate=_f(last.get("annualized_rate")),
        funding_interval_seconds=int(funding["interval_seconds"]) if funding.get("interval_seconds") else None,
        base_spread_bps=_f(raw.get("base_spread_bps")),
        quotes=tiers,
        quotes_updated_at=quotes_raw.get("updated_at"),
    )


def parse_stats(body: dict) -> tuple[dict, list[Listing]]:
    """Returns (platform totals, listings)."""
    listings = []
    for raw in body.get("listings", []):
        try:
            listings.append(parse_listing(raw))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping malformed listing %r: %s", raw.get("ticker"), exc)
    platform = {
        "total_volume_24h": _f(body.get("total_volume_24h")),
        "cumulative_volume": _f(body.get("cumulative_volume")),
        "tvl": _f(body.get("tvl")),
        "open_interest": _f(body.get("open_interest")),
        "num_markets": body.get("num_markets"),
    }
    return platform, listings


def load_fixture(path: Path = FIXTURE) -> dict:
    return json.loads(path.read_text())


def fetch_stats(*, offline: bool | None = None, fixture: Path | None = None) -> tuple[dict, list[Listing]]:
    """Live stats, or the recorded fixture when offline / unreachable."""
    offline = SETTINGS.offline if offline is None else offline
    if offline:
        return parse_stats(load_fixture(fixture or FIXTURE))
    try:
        body = http().get(SETTINGS.variational_stats_url, ttl=60)
    except OfflineError:
        return parse_stats(load_fixture(fixture or FIXTURE))
    return parse_stats(body)


def variational_impact(listing: Listing) -> list[VariationalImpact]:
    """Premium a taker pays at each quoted notional, relative to mark.

    This is Variational's own answer to "what does a $100k buy cost here", so it
    sits next to the Binance figures in the report.
    """
    out = []
    mark = listing.mark_price
    if not mark:
        return out
    for q in listing.quotes:
        out.append(
            VariationalImpact(
                tier=q.tier,
                notional_usd=q.notional_usd,
                buy_premium_pct_vs_mark=(q.ask - mark) / mark * 100,
                sell_discount_pct_vs_mark=(mark - q.bid) / mark * 100,
                spread_bps=q.spread_bps,
            )
        )
    return out
