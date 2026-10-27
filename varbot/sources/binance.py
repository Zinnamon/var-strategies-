"""Binance spot: symbol resolution, order book depth and 24h ticker, turned into the
price-impact table for a token.

Endpoints (public, no key): /api/v3/exchangeInfo, /api/v3/depth (limit 5000),
/api/v3/ticker/24hr, /api/v3/ticker/price.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache

from varbot.analysis import impact as im
from varbot.config import SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import BinanceImpact

log = logging.getLogger(__name__)

QUOTE_PREFERENCE = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
# Quote assets priced in USD without conversion
STABLE_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD1"}


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
) -> BinanceImpact:
    """Pure: build the impact record from a depth payload (and optional 24h ticker)."""
    # Convert the book into USD terms so all notionals are dollars.
    bids = [(p * quote_usd, q) for p, q in im.parse_levels(depth.get("bids", []))]
    asks = [(p * quote_usd, q) for p, q in im.parse_levels(depth.get("asks", []))]
    bids.sort(key=lambda l: -l[0])
    asks.sort(key=lambda l: l[0])
    mid = im.mid_price(bids, asks)
    rec = BinanceImpact(
        binance_symbol=symbol,
        quote_asset=quote,
        listed=True,
        best_bid=bids[0][0] if bids else None,
        best_ask=asks[0][0] if asks else None,
        book_depth_levels=max(len(bids), len(asks)),
        ask_depth_usd=im.total_depth(asks),
        bid_depth_usd=im.total_depth(bids),
        ask_depth_2pct_usd=im.depth_within(asks, 2.0, "buy", mid) if mid else None,
        bid_depth_2pct_usd=im.depth_within(bids, 2.0, "sell", mid) if mid else None,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if rec.best_bid and rec.best_ask:
        rec.spread_bps = (rec.best_ask - rec.best_bid) / mid * 1e4
    if ticker:
        try:
            rec.volume_24h_usd = float(ticker.get("quoteVolume", 0)) * quote_usd
            rec.trades_24h = int(ticker.get("count", 0))
        except (TypeError, ValueError):
            pass
    for n in sizes_usd:
        rec.fills.append(im.sweep(asks, float(n), "buy", mid))
    for n in sizes_usd:
        rec.fills.append(im.sweep(bids, float(n), "sell", mid))
    for pct in moves_pct:
        rec.move_costs.append(im.cost_to_move(asks, pct, "buy"))
    for pct in moves_pct:
        rec.move_costs.append(im.cost_to_move(bids, pct, "sell"))
    if not rec.ask_depth_usd:
        rec.note = "empty ask side"
    return rec


def binance_impact(base: str, *, sizes_usd=im.DEFAULT_SIZES_USD, moves_pct=im.DEFAULT_MOVES_PCT) -> BinanceImpact:
    symbol, quote = resolve_symbol(base)
    if not symbol:
        if not exchange_info():
            return BinanceImpact(listed=False, note="Binance exchangeInfo unavailable (network or offline)")
        return BinanceImpact(listed=False, note=f"{base} has no Binance spot market")
    depth = fetch_depth(symbol)
    if depth is None:
        return BinanceImpact(binance_symbol=symbol, quote_asset=quote, listed=True, note="order book unavailable")
    quote_usd = quote_price_usd(quote or "USDT")
    return impact_from_book(
        depth, symbol=symbol, quote=quote or "", quote_usd=quote_usd, sizes_usd=sizes_usd, moves_pct=moves_pct,
        ticker=fetch_ticker(symbol),
    )
