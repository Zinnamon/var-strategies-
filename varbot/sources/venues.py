"""Public spot order books on the major centralised exchanges, all mapped to the same
(bids, asks) shape so one impact function serves every venue.

Each adapter knows: the CoinGecko exchange identifier it answers for, how to spell a pair,
the REST depth endpoint, the maximum levels that endpoint returns, and how to parse the
reply. No keys needed anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from varbot.analysis import impact as im
from varbot.http import HttpError, OfflineError, http
from varbot.models import VenueImpact

log = logging.getLogger(__name__)

Book = tuple[list[list], list[list]]  # raw bids, raw asks as [[price, qty], ...]

STABLE_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD1", "USD", "USDE", "PYUSD"}
QUOTE_PREFERENCE = ("USDT", "USDC", "USD", "FDUSD", "BTC", "ETH")


@dataclass(frozen=True)
class Venue:
    id: str  # CoinGecko identifier
    name: str
    pair: Callable[[str, str], str]
    url: Callable[[str], tuple[str, dict]]  # symbol -> (url, params)
    parse: Callable[[dict], Book]
    levels_cap: int
    ws: bool = False  # has a live WebSocket adapter in varbot.live


def _levels(rows, price_i=0, qty_i=1) -> list[list]:
    return [[r[price_i], r[qty_i]] for r in rows or []]


# -- adapters ---------------------------------------------------------------

def _binance_url(sym: str):
    return "https://api.binance.com/api/v3/depth", {"symbol": sym, "limit": 5000}


def _mexc_url(sym: str):
    return "https://api.mexc.com/api/v3/depth", {"symbol": sym, "limit": 5000}


def _binance_like(body: dict) -> Book:
    return _levels(body.get("bids")), _levels(body.get("asks"))


def _bybit_url(sym: str):
    return "https://api.bybit.com/v5/market/orderbook", {"category": "spot", "symbol": sym, "limit": 200}


def _bybit_parse(body: dict) -> Book:
    r = body.get("result") or {}
    return _levels(r.get("b")), _levels(r.get("a"))


def _okx_url(sym: str):
    return "https://www.okx.com/api/v5/market/books", {"instId": sym, "sz": 400}


def _okx_parse(body: dict) -> Book:
    d = (body.get("data") or [{}])[0]
    return _levels(d.get("bids")), _levels(d.get("asks"))


def _coinbase_url(sym: str):
    return f"https://api.exchange.coinbase.com/products/{sym}/book", {"level": 2}


def _kraken_url(sym: str):
    return "https://api.kraken.com/0/public/Depth", {"pair": sym, "count": 500}


def _kraken_parse(body: dict) -> Book:
    res = body.get("result") or {}
    if not res:
        raise ValueError(str(body.get("error")))
    book = next(iter(res.values()))
    return _levels(book.get("bids")), _levels(book.get("asks"))


def _kucoin_url(sym: str):
    return "https://api.kucoin.com/api/v1/market/orderbook/level2_100", {"symbol": sym}


def _kucoin_parse(body: dict) -> Book:
    d = body.get("data") or {}
    return _levels(d.get("bids")), _levels(d.get("asks"))


def _gate_url(sym: str):
    return "https://api.gateio.ws/api/v4/spot/order_book", {"currency_pair": sym, "limit": 1000}


def _bitget_url(sym: str):
    return "https://api.bitget.com/api/v2/spot/market/orderbook", {"symbol": sym, "limit": 150}


def _htx_url(sym: str):
    return "https://api.huobi.pro/market/depth", {"symbol": sym, "type": "step0"}


def _htx_parse(body: dict) -> Book:
    t = body.get("tick") or {}
    return _levels(t.get("bids")), _levels(t.get("asks"))


def _cryptocom_url(sym: str):
    return "https://api.crypto.com/exchange/v1/public/get-book", {"instrument_name": sym, "depth": 150}


def _cryptocom_parse(body: dict) -> Book:
    d = ((body.get("result") or {}).get("data") or [{}])[0]
    return _levels(d.get("bids")), _levels(d.get("asks"))


def _kraken_pair(base: str, quote: str) -> str:
    base = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
    return f"{base}{quote}"


VENUES: dict[str, Venue] = {
    "binance": Venue("binance", "Binance", lambda b, q: f"{b}{q}", _binance_url, _binance_like, 5000, ws=True),
    "bybit_spot": Venue("bybit_spot", "Bybit", lambda b, q: f"{b}{q}", _bybit_url, _bybit_parse, 200, ws=True),
    "okex": Venue("okex", "OKX", lambda b, q: f"{b}-{q}", _okx_url, _okx_parse, 400, ws=True),
    "gdax": Venue("gdax", "Coinbase", lambda b, q: f"{b}-{q}", _coinbase_url, _binance_like, 50),
    "kraken": Venue("kraken", "Kraken", _kraken_pair, _kraken_url, _kraken_parse, 500),
    "kucoin": Venue("kucoin", "KuCoin", lambda b, q: f"{b}-{q}", _kucoin_url, _kucoin_parse, 100),
    "gate": Venue("gate", "Gate", lambda b, q: f"{b}_{q}", _gate_url, _binance_like, 1000),
    "mxc": Venue("mxc", "MEXC", lambda b, q: f"{b}{q}", _mexc_url, _binance_like, 5000),
    "bitget": Venue("bitget", "Bitget", lambda b, q: f"{b}{q}", _bitget_url, _kucoin_parse, 150),
    "huobi": Venue("huobi", "HTX", lambda b, q: f"{b}{q}".lower(), _htx_url, _htx_parse, 150),
    "crypto_com": Venue("crypto_com", "Crypto.com", lambda b, q: f"{b}_{q}", _cryptocom_url, _cryptocom_parse, 150),
}

ALIASES = {"bybit": "bybit_spot", "okx": "okex", "coinbase": "gdax", "mexc": "mxc", "htx": "huobi", "cryptocom": "crypto_com"}


def venue_by_name(name: str) -> Venue | None:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return VENUES.get(key)


def impact_from_levels(
    bids_raw, asks_raw, *, venue: Venue, symbol: str, quote: str, quote_usd: float = 1.0,
    sizes_usd=im.DEFAULT_SIZES_USD, moves_pct=im.DEFAULT_MOVES_PCT, volume_24h_usd: float | None = None,
    trades_24h: int | None = None,
) -> VenueImpact:
    """The impact record for any venue from raw [[price, qty]] rows."""
    bids = [(p * quote_usd, q) for p, q in im.parse_levels(bids_raw)]
    asks = [(p * quote_usd, q) for p, q in im.parse_levels(asks_raw)]
    bids.sort(key=lambda l: -l[0])
    asks.sort(key=lambda l: l[0])
    mid = im.mid_price(bids, asks)
    rec = VenueImpact(
        venue=venue.id,
        venue_name=venue.name,
        binance_symbol=symbol,
        quote_asset=quote,
        listed=True,
        levels_cap=venue.levels_cap,
        best_bid=bids[0][0] if bids else None,
        best_ask=asks[0][0] if asks else None,
        book_depth_levels=max(len(bids), len(asks)),
        ask_depth_usd=im.total_depth(asks),
        bid_depth_usd=im.total_depth(bids),
        ask_depth_2pct_usd=im.depth_within(asks, 2.0, "buy", mid) if mid else None,
        bid_depth_2pct_usd=im.depth_within(bids, 2.0, "sell", mid) if mid else None,
        volume_24h_usd=volume_24h_usd,
        trades_24h=trades_24h,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if rec.best_bid and rec.best_ask and mid:
        rec.spread_bps = (rec.best_ask - rec.best_bid) / mid * 1e4
    for n in sizes_usd:
        rec.fills.append(im.sweep(asks, float(n), "buy", mid))
    for n in sizes_usd:
        rec.fills.append(im.sweep(bids, float(n), "sell", mid))
    for pct in moves_pct:
        rec.move_costs.append(im.cost_to_move(asks, pct, "buy"))
    for pct in moves_pct:
        rec.move_costs.append(im.cost_to_move(bids, pct, "sell"))
    if not asks:
        rec.note = "empty ask side"
    elif len(asks) >= venue.levels_cap:
        rec.note = f"book capped at {venue.levels_cap} levels by the venue; deeper liquidity not visible"
    return rec


def fetch_book(venue: Venue, symbol: str) -> Book | None:
    url, params = venue.url(symbol)
    try:
        body = http().get(url, params=params, ttl=60)
        return venue.parse(body)
    except (HttpError, OfflineError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("%s book %s unavailable: %s", venue.name, symbol, exc)
        return None


def venue_impact(
    venue: Venue, base: str, quote: str, *, quote_usd: float = 1.0, sizes_usd=im.DEFAULT_SIZES_USD,
    moves_pct=im.DEFAULT_MOVES_PCT, volume_24h_usd: float | None = None,
) -> VenueImpact:
    symbol = venue.pair(base.upper(), quote.upper())
    book = fetch_book(venue, symbol)
    if book is None:
        return VenueImpact(venue=venue.id, venue_name=venue.name, binance_symbol=symbol, quote_asset=quote, listed=True,
                           levels_cap=venue.levels_cap, note="order book unavailable")
    bids, asks = book
    return impact_from_levels(bids, asks, venue=venue, symbol=symbol, quote=quote, quote_usd=quote_usd,
                              sizes_usd=sizes_usd, moves_pct=moves_pct, volume_24h_usd=volume_24h_usd)


def pick_pair(tickers, exchange_id: str) -> tuple[str, str, float, float | None] | None:
    """From CoinGecko tickers choose the best spot pair on one exchange.

    Returns (base, quote, quote_usd, volume_usd). quote_usd converts the venue's prices
    to dollars (1 for stables; for BTC/ETH quotes it is derived from the ticker itself).
    """
    rows = [t for t in tickers if t.exchange_id == exchange_id and not t.is_stale and not t.is_anomaly]
    if not rows:
        rows = [t for t in tickers if t.exchange_id == exchange_id]
    if not rows:
        return None

    def rank(t):
        pref = QUOTE_PREFERENCE.index(t.target) if t.target in QUOTE_PREFERENCE else len(QUOTE_PREFERENCE)
        return (pref, -(t.volume_usd or 0))

    t = sorted(rows, key=rank)[0]
    quote_usd = 1.0
    if t.target not in STABLE_QUOTES:
        # converted_last.usd / last-in-quote gives the quote's dollar price; CoinGecko's
        # ticker only carries the converted value, so fall back to a venue price later.
        quote_usd = 0.0
    return t.base, t.target, quote_usd, t.volume_usd


def all_venue_impacts(
    base: str, tickers, venue_ids: list[str], *, sizes_usd=im.DEFAULT_SIZES_USD, moves_pct=im.DEFAULT_MOVES_PCT,
    quote_prices: dict[str, float] | None = None,
) -> list[VenueImpact]:
    """Impact on every requested venue that CoinGecko says lists the token."""
    out: list[VenueImpact] = []
    listed_ids = {t.exchange_id for t in tickers}
    for vid in venue_ids:
        venue = VENUES.get(vid)
        if not venue or vid not in listed_ids:
            continue
        picked = pick_pair(tickers, vid)
        if not picked:
            continue
        b, q, quote_usd, vol = picked
        if quote_usd == 0.0:
            quote_usd = (quote_prices or {}).get(q, 0.0)
            if not quote_usd:
                out.append(VenueImpact(venue=vid, venue_name=venue.name, binance_symbol=venue.pair(b, q), quote_asset=q,
                                       listed=True, levels_cap=venue.levels_cap, note=f"no USD price for quote {q}"))
                continue
        out.append(venue_impact(venue, b, q, quote_usd=quote_usd, sizes_usd=sizes_usd, moves_pct=moves_pct, volume_24h_usd=vol))
    return out
