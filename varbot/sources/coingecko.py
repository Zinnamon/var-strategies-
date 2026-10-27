"""Token fundamentals and official links from CoinGecko.

Endpoints used (public API v3, demo or pro key optional):
- /search?query=            resolve a Variational symbol + name to a coin id
- /coins/markets?ids=       market cap, FDV, volume, supply, ATH, price changes (batched)
- /coins/{id}               links (X handle, Telegram channel, homepage, repos), categories,
                            community and developer snapshots
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from varbot.config import DATA_DIR, SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import TokenFundamentals

log = logging.getLogger(__name__)

MARKETS_TTL = 10 * 60
DETAIL_TTL = 24 * 3600
SEARCH_TTL = 7 * 24 * 3600


def _headers() -> dict:
    if not SETTINGS.coingecko_api_key:
        return {}
    if SETTINGS.coingecko_pro:
        return {"x-cg-pro-api-key": SETTINGS.coingecko_api_key}
    return {"x-cg-demo-api-key": SETTINGS.coingecko_api_key}


def _get(path: str, params: dict | None = None, ttl: float = MARKETS_TTL):
    return http().get(f"{SETTINGS.coingecko_base}{path}", params=params, headers=_headers(), ttl=ttl)


@lru_cache(maxsize=1)
def overrides() -> dict[str, str]:
    path = DATA_DIR / "coingecko_overrides.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k.upper(): v for k, v in raw.items() if not k.startswith("_")}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@lru_cache(maxsize=1)
def coin_list() -> list[dict]:
    """Every CoinGecko coin (id, symbol, name): one call, cached for a week."""
    try:
        return _get("/coins/list", ttl=SEARCH_TTL)
    except (HttpError, OfflineError) as exc:
        log.warning("coingecko /coins/list unavailable: %s", exc)
        return []


def resolve_id(base_symbol: str, name: str | None) -> tuple[str | None, str]:
    """Map a Variational symbol (+ listing name) to a CoinGecko id.

    Returns (id or None, note). Ambiguous symbols are common (dozens of coins share a
    3-letter symbol), so: override table, then exact name match in the full coin list,
    then a /search call whose results carry market-cap rank, best rank wins.
    """
    sym = base_symbol.upper()
    if sym in overrides():
        return overrides()[sym], "override table"

    same_symbol = [c for c in coin_list() if (c.get("symbol") or "").upper() == sym]
    if name:
        by_name = [c for c in same_symbol if _norm(c.get("name")) == _norm(name)]
        if by_name:
            return by_name[0]["id"], "symbol and name match"
    if len(same_symbol) == 1:
        return same_symbol[0]["id"], "unique symbol match"

    # Several coins (or none in the cached list): ask /search, which ranks by market cap.
    candidates: list[dict] = []
    for query in [q for q in (name, sym) if q]:
        try:
            res = _get("/search", {"query": query}, ttl=SEARCH_TTL)
        except (HttpError, OfflineError) as exc:
            log.warning("coingecko search %r failed: %s", query, exc)
            continue
        candidates.extend(res.get("coins", []))
    ranked = [c for c in candidates if (c.get("symbol") or "").upper() == sym]
    if not ranked:
        if same_symbol:
            return same_symbol[0]["id"], f"symbol match, unranked (ambiguous among {len(same_symbol)})"
        return None, f"no CoinGecko coin with symbol {sym}"
    if name:
        by_name = [c for c in ranked if _norm(c.get("name")) == _norm(name)]
        if by_name:
            return by_name[0]["id"], "symbol and name match"
    ranked.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
    note = "symbol match, best market-cap rank"
    ids = sorted({c["id"] for c in ranked})
    if len(ids) > 1:
        note += f" (ambiguous: {', '.join(ids[:5])})"
    return ranked[0]["id"], note


def fetch_markets(ids: list[str]) -> dict[str, dict]:
    """/coins/markets for many ids at once, keyed by id."""
    out: dict[str, dict] = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        try:
            rows = _get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "ids": ",".join(chunk),
                    "per_page": 250,
                    "price_change_percentage": "24h,7d,30d",
                    "sparkline": "false",
                },
            )
        except (HttpError, OfflineError) as exc:
            log.warning("coingecko markets failed: %s", exc)
            continue
        for row in rows:
            out[row["id"]] = row
    return out


def fetch_detail(coin_id: str) -> dict | None:
    try:
        return _get(
            f"/coins/{coin_id}",
            {
                "localization": "false",
                "tickers": "false",
                "market_data": "false",
                "community_data": "true",
                "developer_data": "true",
                "sparkline": "false",
            },
            ttl=DETAIL_TTL,
        )
    except (HttpError, OfflineError) as exc:
        log.warning("coingecko detail %s failed: %s", coin_id, exc)
        return None


CHART_TTL = 12 * 3600


def fetch_market_chart(coin_id: str, days: int = 365) -> dict | None:
    """Daily prices and volumes for the last `days` (CoinGecko picks daily granularity above 90 days)."""
    try:
        return _get(f"/coins/{coin_id}/market_chart", {"vs_currency": "usd", "days": days}, ttl=CHART_TTL)
    except (HttpError, OfflineError) as exc:
        log.warning("coingecko market_chart %s failed: %s", coin_id, exc)
        return None


def _first(items) -> str | None:
    for x in items or []:
        if x:
            return x
    return None


def _telegram_from_links(links: dict) -> str | None:
    ident = links.get("telegram_channel_identifier")
    if ident:
        return ident.strip().lstrip("@")
    for url in (links.get("chat_url") or []) + (links.get("official_forum_url") or []):
        m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", url or "")
        if m:
            return m.group(1)
    return None


def build_fundamentals(coin_id: str | None, note: str, market: dict | None, detail: dict | None) -> TokenFundamentals:
    f = TokenFundamentals(coingecko_id=coin_id, resolution_note=note)
    if market:
        f.name = market.get("name")
        f.symbol = (market.get("symbol") or "").upper() or None
        f.price_usd = market.get("current_price")
        f.market_cap_usd = market.get("market_cap") or None
        f.market_cap_rank = market.get("market_cap_rank")
        f.fdv_usd = market.get("fully_diluted_valuation")
        f.volume_24h_usd = market.get("total_volume")
        f.circulating_supply = market.get("circulating_supply")
        f.total_supply = market.get("total_supply")
        f.max_supply = market.get("max_supply")
        f.ath_usd = market.get("ath")
        f.ath_change_pct = market.get("ath_change_percentage")
        f.ath_date = market.get("ath_date")
        f.change_24h_pct = market.get("price_change_percentage_24h_in_currency", market.get("price_change_percentage_24h"))
        f.change_7d_pct = market.get("price_change_percentage_7d_in_currency")
        f.change_30d_pct = market.get("price_change_percentage_30d_in_currency")
    if detail:
        f.name = f.name or detail.get("name")
        f.symbol = f.symbol or (detail.get("symbol") or "").upper() or None
        f.categories = [c for c in detail.get("categories") or [] if c]
        desc = (detail.get("description") or {}).get("en") or ""
        f.description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", desc)).strip()[:1500] or None
        links = detail.get("links") or {}
        f.homepage = _first(links.get("homepage"))
        f.twitter_handle = (links.get("twitter_screen_name") or "").strip().lstrip("@") or None
        f.telegram_channel = _telegram_from_links(links)
        f.discord_url = _first([u for u in links.get("chat_url") or [] if "discord" in (u or "")])
        repos = (links.get("repos_url") or {}).get("github") or []
        f.github_repos = [r for r in repos if r][:5]
        f.chains = sorted((detail.get("platforms") or {}).keys() - {""})
        f.genesis_date = detail.get("genesis_date")
        community = detail.get("community_data") or {}
        f.twitter_followers = community.get("twitter_followers")
        f.telegram_users = community.get("telegram_channel_user_count")
        f.reddit_subscribers = community.get("reddit_subscribers")
        dev = detail.get("developer_data") or {}
        f.github_stars = dev.get("stars")
        f.github_forks = dev.get("forks")
        f.commits_4w = dev.get("commit_count_4_weeks")
    return f


TICKERS_TTL = 30 * 60

# CoinGecko exchange identifiers for the centralised venues we track order books on.
MAJOR_CEX = {
    "binance": "Binance",
    "bybit_spot": "Bybit",
    "okex": "OKX",
    "gdax": "Coinbase",
    "kraken": "Kraken",
    "kucoin": "KuCoin",
    "gate": "Gate",
    "mxc": "MEXC",
    "bitget": "Bitget",
    "huobi": "HTX",
    "crypto_com": "Crypto.com",
    "upbit": "Upbit",
    "bithumb": "Bithumb",
    "bitfinex": "Bitfinex",
    "bitstamp": "Bitstamp",
    "hitbtc": "HitBTC",
    "lbank": "LBank",
}


def fetch_tickers(coin_id: str, pages: int = 2) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, pages + 1):
        try:
            body = _get(f"/coins/{coin_id}/tickers", {"page": page, "depth": "false", "include_exchange_logo": "false"}, ttl=TICKERS_TTL)
        except (HttpError, OfflineError) as exc:
            log.warning("coingecko tickers %s page %d failed: %s", coin_id, page, exc)
            break
        batch = body.get("tickers") or []
        rows.extend(batch)
        if len(batch) < 100:
            break
    return rows


def build_exchange_listing(raw: list[dict], base_symbol: str | None = None) -> "ExchangeListing":
    """Turn CoinGecko tickers into the per-exchange listing summary.

    Only spot tickers whose base is the token are kept (CoinGecko also lists pairs where
    the token is the quote side). Stale and anomalous tickers stay in the list but are
    marked, and do not count towards volume.
    """
    from varbot.models import ExchangeListing, ExchangeTicker

    tickers: list[ExchangeTicker] = []
    for t in raw:
        market = t.get("market") or {}
        base = (t.get("base") or "").upper()
        target = (t.get("target") or "").upper()
        # Contract addresses show up as base/target for DEX pairs; prefer the coin_id check.
        if base_symbol and base != base_symbol.upper() and t.get("coin_id") and t.get("coin_id") != t.get("target_coin_id"):
            # coin is the base by coin_id even if the symbol differs (e.g. address); keep
            pass
        elif base_symbol and base != base_symbol.upper():
            continue
        conv_last = (t.get("converted_last") or {}).get("usd")
        conv_vol = (t.get("converted_volume") or {}).get("usd")
        tickers.append(
            ExchangeTicker(
                exchange_id=market.get("identifier") or "",
                exchange_name=market.get("name") or market.get("identifier") or "?",
                base=base,
                target=target,
                pair=f"{base}/{target}",
                last_usd=float(conv_last) if conv_last is not None else None,
                volume_usd=float(conv_vol) if conv_vol is not None else None,
                spread_pct=t.get("bid_ask_spread_percentage"),
                trust_score=t.get("trust_score"),
                is_stale=bool(t.get("is_stale")),
                is_anomaly=bool(t.get("is_anomaly")),
                trade_url=t.get("trade_url"),
            )
        )
    listing = ExchangeListing(available=bool(raw), tickers=tickers)
    if not raw:
        listing.note = "no tickers returned"
        return listing
    vol_by_ex: dict[str, float] = {}
    name_by_id: dict[str, str] = {}
    for t in tickers:
        name_by_id[t.exchange_id] = t.exchange_name
        if not (t.is_stale or t.is_anomaly):
            vol_by_ex[t.exchange_id] = vol_by_ex.get(t.exchange_id, 0.0) + (t.volume_usd or 0.0)
        else:
            vol_by_ex.setdefault(t.exchange_id, 0.0)
    ordered = sorted(vol_by_ex, key=lambda k: -vol_by_ex[k])
    listing.exchange_ids = ordered
    listing.exchanges = [name_by_id[k] for k in ordered]
    listing.major_cex = [MAJOR_CEX[k] for k in ordered if k in MAJOR_CEX]
    total = sum(vol_by_ex.values())
    listing.total_spot_volume_usd = total
    listing.top_exchange_share_pct = (vol_by_ex[ordered[0]] / total * 100) if ordered and total else None
    dex_ids = {t.exchange_id for t in tickers if (t.trade_url or "").startswith(("https://app.", "https://dexscreener", "https://raydium", "https://jup", "https://pancakeswap", "https://app.uniswap"))}
    # CoinGecko marks DEX pairs with contract-address targets or known DEX identifiers.
    for t in tickers:
        if any(k in t.exchange_id for k in ("uniswap", "pancake", "raydium", "orca", "curve", "sushi", "jupiter", "aerodrome", "meteora", "balancer", "trader_joe", "camelot", "velodrome", "quickswap", "pumpswap", "_v2", "_v3", "_v4")):
            dex_ids.add(t.exchange_id)
    listing.dex_count = len([e for e in ordered if e in dex_ids])
    listing.cex_count = len(ordered) - listing.dex_count
    return listing


def exchange_listing_for(coin_id: str | None, base_symbol: str | None) -> "ExchangeListing":
    from varbot.models import ExchangeListing

    if not coin_id:
        return ExchangeListing(available=False, note="no CoinGecko id")
    return build_exchange_listing(fetch_tickers(coin_id), base_symbol)


def fundamentals_for(symbols: list[tuple[str, str | None]], *, with_detail: bool = False) -> dict[str, TokenFundamentals]:
    """symbols: list of (base_symbol, listing name). Returns {base_symbol: fundamentals}.

    Market data comes in batches of 100 ids. Per-coin details (links, community, dev)
    cost one call each, so they are off by default; call `add_detail` for the tokens
    you actually keep.
    """
    resolved: dict[str, tuple[str | None, str]] = {}
    for base, name in symbols:
        resolved[base] = resolve_id(base, name)
    markets = fetch_markets([cid for cid, _ in resolved.values() if cid])
    out: dict[str, TokenFundamentals] = {}
    for base, (cid, note) in resolved.items():
        market = markets.get(cid) if cid else None
        detail = fetch_detail(cid) if (cid and with_detail) else None
        out[base] = build_fundamentals(cid, note, market, detail)
    return out


def add_detail(f: TokenFundamentals) -> TokenFundamentals:
    """Fill links, community and developer fields in place (one CoinGecko call)."""
    if not f.coingecko_id:
        return f
    detail = fetch_detail(f.coingecko_id)
    if not detail:
        return f
    enriched = build_fundamentals(f.coingecko_id, f.resolution_note, None, detail)
    for field_name, value in enriched.model_dump().items():
        if value not in (None, [], "") and getattr(f, field_name) in (None, [], ""):
            setattr(f, field_name, value)
    return f
