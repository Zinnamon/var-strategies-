"""Binance spot: symbol resolution, order book depth and 24h ticker, turned into the
price-impact table for a token. Binance is the primary venue because its public book is
the deepest (5000 levels) and it needs no listing lookup: exchangeInfo tells us directly.

Endpoints (public, no key): /api/v3/exchangeInfo, /api/v3/depth (limit 5000),
/api/v3/ticker/24hr, /api/v3/ticker/price.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from varbot.analysis import impact as im
from varbot.config import SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import VenueImpact
from varbot.sources.venues import VENUES, impact_from_levels

log = logging.getLogger(__name__)

QUOTE_PREFERENCE = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
STABLE_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD1"}
BINANCE = VENUES["binance"]


@lru_cache(maxsize=1)
def exchange_info() -> dict[str, dict]:
    """Spot symbols currently TRADING, keyed by symbol."""
    try:
        body = http().get(f"{SETTINGS.binance_base}/api/v3/exchangeInfo", ttl=6 * 3600)
    except (HttpError, OfflineError) as exc:
        log.warning("binance exchangeInfo unavailable: %s", exc)
        return {}
    return {s["symbol"]: s for s in body.get("symbols", []) if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)}


def resolve_symbol(base: str) -> tuple[str | None, str | None]:
    """Best spot pair for a base asset: (symbol, quote asset)."""
    info = exchange_info()
    base = base.upper()
    for quote in QUOTE_PREFERENCE:
        sym = f"{base}{quote}"
        if sym in info:
            return sym, quote
    for sym, s in info.items():
        if s.get("baseAsset") == base:
            return sym, s.get("quoteAsset")
    return None, None


def quote_price_usd(quote: str) -> float:
    if quote in STABLE_QUOTES:
        return 1.0
    try:
        body = http().get(f"{SETTINGS.binance_base}/api/v3/ticker/price", params={"symbol": f"{quote}USDT"}, ttl=60)
        return float(body["price"])
    except (HttpError, OfflineError, KeyError, ValueError) as exc:
        log.warning("could not price %s in USD: %s", quote, exc)
        return 1.0


def fetch_depth(symbol: str, limit: int = 5000) -> dict | None:
    try:
        return http().get(f"{SETTINGS.binance_base}/api/v3/depth", params={"symbol": symbol, "limit": limit}, ttl=60)
    except (HttpError, OfflineError) as exc:
        log.warning("binance depth %s unavailable: %s", symbol, exc)
        return None


def fetch_ticker(symbol: str) -> dict | None:
    try:
        return http().get(f"{SETTINGS.binance_base}/api/v3/ticker/24hr", params={"symbol": symbol}, ttl=120)
    except (HttpError, OfflineError) as exc:
        log.warning("binance ticker %s unavailable: %s", symbol, exc)
        return None


def impact_from_book(
    depth: dict,
    *,
    symbol: str,
    quote: str,
    quote_usd: float = 1.0,
    sizes_usd=im.DEFAULT_SIZES_USD,
    moves_pct=im.DEFAULT_MOVES_PCT,
    ticker: dict | None = None,
) -> VenueImpact:
    """Pure: build the impact record from a Binance depth payload (and optional 24h ticker)."""
    vol = trades = None
    if ticker:
        try:
            vol = float(ticker.get("quoteVolume", 0)) * quote_usd
            trades = int(ticker.get("count", 0))
        except (TypeError, ValueError):
            pass
    return impact_from_levels(
        depth.get("bids", []), depth.get("asks", []), venue=BINANCE, symbol=symbol, quote=quote, quote_usd=quote_usd,
        sizes_usd=sizes_usd, moves_pct=moves_pct, volume_24h_usd=vol, trades_24h=trades,
    )


def binance_impact(base: str, *, sizes_usd=im.DEFAULT_SIZES_USD, moves_pct=im.DEFAULT_MOVES_PCT) -> VenueImpact:
    symbol, quote = resolve_symbol(base)
    if not symbol:
        if not exchange_info():
            return VenueImpact(listed=False, note="Binance exchangeInfo unavailable (network or offline)")
        return VenueImpact(listed=False, note=f"{base} has no Binance spot market")
    depth = fetch_depth(symbol)
    if depth is None:
        return VenueImpact(binance_symbol=symbol, quote_asset=quote, listed=True, note="order book unavailable")
    quote_usd = quote_price_usd(quote or "USDT")
    return impact_from_book(
        depth, symbol=symbol, quote=quote or "", quote_usd=quote_usd, sizes_usd=sizes_usd, moves_pct=moves_pct,
        ticker=fetch_ticker(symbol),
    )
