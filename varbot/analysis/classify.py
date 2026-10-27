"""Separate crypto tokens from the equities, ETFs, commodities and pre-IPO names that
Variational lists under the same `perpetual` contract type, and normalise tickers.

The API publishes no asset class. Three signals are used, in order:
1. an explicit table in data/asset_classes.json (maintained by hand),
2. the listing name (company / fund wording),
3. the CoinGecko cross-check done later in the pipeline (a crypto perp whose symbol
   resolves to a real coin with a matching name).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from varbot.config import DATA_DIR
from varbot.models import AssetClassification, Listing

TRADFI_NAME = re.compile(
    r"\b(Inc|Corp|Corporation|Company|Holdings?|Ltd|Limited|plc|N\.V|A/S|Oyj|ETF|Trust|"
    r"Common Stock|Class [A-Z]|Index|Futures?)\b\.?",
    re.IGNORECASE,
)
MULTIPLIER = re.compile(r"^(?P<mult>1000000|100000|10000|1000|1M)(?P<rest>[A-Z][A-Z0-9]{1,})$")


@lru_cache(maxsize=1)
def asset_class_table() -> dict[str, str]:
    path = DATA_DIR / "asset_classes.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    table: dict[str, str] = {}
    for klass, symbols in raw.items():
        for s in symbols:
            table[s.upper()] = klass
    return table


def split_multiplier(symbol: str) -> tuple[str, int]:
    """'1000BONK' -> ('BONK', 1000); '1INCH' -> ('1INCH', 1)."""
    m = MULTIPLIER.match(symbol)
    if not m:
        return symbol, 1
    mult = m.group("mult")
    n = 1_000_000 if mult == "1M" else int(mult)
    return m.group("rest"), n


def base_symbol(symbol: str) -> str:
    """Strip a multiplier prefix and any `_DISAMBIGUATOR` suffix: 'OPN_OPINION' -> 'OPN'."""
    head = symbol.split("_", 1)[0]
    return split_multiplier(head)[0]


def classify(listing: Listing) -> AssetClassification:
    symbol = listing.symbol
    base, mult = split_multiplier(symbol.split("_", 1)[0])
    table = asset_class_table()
    name = listing.name or ""

    if listing.contract_type == "swap":
        klass = table.get(symbol, "commodity")
        return AssetClassification(
            ticker=listing.ticker, symbol=symbol, base_symbol=base, multiplier=mult,
            asset_class=klass if klass in _ALLOWED else "unknown",
            reason="swap contract (TradFi financing-rate product)",
        )
    if symbol in table:
        klass = table[symbol]
        return AssetClassification(
            ticker=listing.ticker, symbol=symbol, base_symbol=base, multiplier=mult,
            asset_class=klass if klass in _ALLOWED else "unknown", reason="explicit table",
        )
    if TRADFI_NAME.search(name):
        return AssetClassification(
            ticker=listing.ticker, symbol=symbol, base_symbol=base, multiplier=mult,
            asset_class="equity", reason=f"name reads like a company or fund: {name!r}",
        )
    return AssetClassification(
        ticker=listing.ticker, symbol=symbol, base_symbol=base, multiplier=mult,
        asset_class="crypto", reason="default (confirmed later by CoinGecko match)",
    )


_ALLOWED = {"crypto", "equity", "etf", "commodity", "index", "fx", "pre_ipo", "unknown"}
