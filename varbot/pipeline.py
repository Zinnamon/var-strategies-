"""Orchestrates the sources into TokenReports."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from varbot.analysis import impact as im
from varbot.analysis import pumps as pumps_mod
from varbot.analysis.classify import classify
from varbot.analysis.scoring import score_all
from varbot.models import AssetClassification, Listing, TokenFundamentals, TokenReport
from varbot.research.business import research_token
from varbot.sources import binance, coingecko, telegram, variational, venues, x_api

DEFAULT_VENUES = ["binance", "bybit_spot", "okex", "gdax", "kraken", "kucoin", "gate", "mxc", "bitget", "huobi", "crypto_com"]

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def crypto_listings(listings: Iterable[Listing]) -> list[tuple[Listing, AssetClassification]]:
    out = []
    for l in listings:
        c = classify(l)
        if c.asset_class == "crypto":
            out.append((l, c))
    return out


def select_symbols(listings: list[Listing], symbols: list[str] | None) -> list[Listing]:
    if not symbols:
        return listings
    wanted = {s.upper().replace("-PERP", "") for s in symbols}
    return [l for l in listings if l.symbol.upper() in wanted or classify(l).base_symbol in wanted]


def build_reports(
    listings: list[Listing],
    *,
    small_cap_max_usd: float = 100_000_000,
    only_small_caps: bool = False,
    cap_basis: str = "either",  # fdv | mcap | either
    with_pumps: bool = True,
    pump_min_gain_pct: float = 50.0,
    pump_window_days: int = 21,
    sizes_usd=im.DEFAULT_SIZES_USD,
    moves_pct=im.DEFAULT_MOVES_PCT,
    with_social: bool = True,
    with_research: bool = False,
    with_binance: bool = True,
    venue_ids: list[str] | None = None,
    limit: int | None = None,
    progress: Progress = lambda msg: None,
    workers: int = 4,
) -> list[TokenReport]:
    pairs = crypto_listings(listings)
    progress(f"{len(pairs)} crypto listings out of {len(listings)} markets")

    progress("resolving CoinGecko ids and market data")
    fundamentals = coingecko.fundamentals_for([(c.base_symbol, l.name) for l, c in pairs], with_detail=False)

    reports: list[TokenReport] = []
    for l, c in pairs:
        f = fundamentals.get(c.base_symbol) or TokenFundamentals()
        # The CoinGecko cross-check: a name that resolved to nothing is suspicious but
        # still treated as crypto (new listings lag CoinGecko by days).
        r = TokenReport(listing=l, classification=c, fundamentals=f, small_cap_threshold_usd=small_cap_max_usd)
        if cap_basis == "fdv":
            cap = f.fdv_usd or f.market_cap_usd
        elif cap_basis == "mcap":
            cap = f.market_cap_usd or f.fdv_usd
        else:
            cap = f.market_cap_usd or f.fdv_usd
        r.is_small_cap = bool(cap) and cap <= small_cap_max_usd
        if cap is None:
            r.is_small_cap = True  # unknown cap: treat as small so it gets the impact check
        r.variational_impact = variational.variational_impact(l)
        reports.append(r)

    if only_small_caps:
        reports = [r for r in reports if r.is_small_cap]
    reports.sort(key=lambda r: (r.fundamentals.market_cap_usd or r.fundamentals.fdv_usd or 0))
    if limit:
        reports = reports[:limit]
    progress(f"{len(reports)} tokens selected for deep analysis")

    venue_ids = DEFAULT_VENUES if venue_ids is None else venue_ids
    quote_prices = {"BTC": binance.quote_price_usd("BTC"), "ETH": binance.quote_price_usd("ETH")} if venue_ids else {}

    def enrich(r: TokenReport) -> TokenReport:
        sym = r.classification.base_symbol
        coingecko.add_detail(r.fundamentals)  # links, community and dev data for kept tokens only
        r.exchanges = coingecko.exchange_listing_for(r.fundamentals.coingecko_id, sym)
        if with_binance:
            r.binance = binance.binance_impact(sym, sizes_usd=sizes_usd, moves_pct=moves_pct)
        if venue_ids and r.exchanges.available:
            others = [v for v in venue_ids if v != "binance"]
            r.venues = venues.all_venue_impacts(sym, r.exchanges.tickers, others, sizes_usd=sizes_usd, moves_pct=moves_pct,
                                                quote_prices=quote_prices)
        if with_social:
            r.x = x_api.x_metrics(r.fundamentals.twitter_handle)
            r.telegram = telegram.telegram_metrics(r.fundamentals.telegram_channel)
        if with_research:
            r.research = research_token(sym, r.fundamentals, r.listing.name)
        if with_pumps and r.fundamentals.coingecko_id:
            chart = coingecko.fetch_market_chart(r.fundamentals.coingecko_id)
            r.pumps = pumps_mod.analyse(chart, min_gain_pct=pump_min_gain_pct, window_days=pump_window_days) if chart else pumps_mod.PumpStats(note="price history unavailable")
        r.scores = score_all(r.fundamentals, r.binance, r.x, r.telegram, r.research, r.exchanges, r.pumps)
        progress(f"done {r.listing.ticker}")
        return r

    if workers > 1 and len(reports) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            reports = list(pool.map(enrich, reports))
    else:
        reports = [enrich(r) for r in reports]
    return reports
