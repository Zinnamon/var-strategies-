"""A local order book kept current from exchange WebSocket deltas, and the impact figures
recomputed from it on demand. Pure Python, no I/O, so the feed adapters' sequencing
logic can be unit-tested with synthetic messages."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from varbot.analysis import impact as im
from varbot.models import VenueImpact
from varbot.sources.venues import Venue, impact_from_levels


@dataclass
class LocalBook:
    venue: Venue
    symbol: str
    quote: str
    quote_usd: float = 1.0
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    synced: bool = False
    last_update_id: int | None = None  # venue sequence of the last applied event
    updated_at: float = 0.0  # time.time() of last applied message
    updates: int = 0
    resyncs: int = 0
    note: str = ""

    # -- mutation ------------------------------------------------------------
    def reset(self, bids=(), asks=(), update_id: int | None = None) -> None:
        self.bids = {}
        self.asks = {}
        for p, q in bids:
            self.set("bid", float(p), float(q))
        for p, q in asks:
            self.set("ask", float(p), float(q))
        self.last_update_id = update_id
        self.synced = True
        self.updated_at = time.time()

    def set(self, side: str, price: float, qty: float) -> None:
        book = self.bids if side == "bid" else self.asks
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty

    def apply(self, bids=(), asks=()) -> None:
        for p, q in bids:
            self.set("bid", float(p), float(q))
        for p, q in asks:
            self.set("ask", float(p), float(q))
        self.updates += 1
        self.updated_at = time.time()

    def desync(self, why: str) -> None:
        self.synced = False
        self.resyncs += 1
        self.note = why

    # -- views -----------------------------------------------------------------
    def levels(self) -> tuple[list[list[float]], list[list[float]]]:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])
        return [[p, q] for p, q in bids], [[p, q] for p, q in asks]

    def best(self) -> tuple[float | None, float | None]:
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)

    def age_seconds(self) -> float:
        return time.time() - self.updated_at if self.updated_at else float("inf")

    def crossed(self) -> bool:
        bid, ask = self.best()
        return bid is not None and ask is not None and bid >= ask

    def impact(self, sizes_usd=im.DEFAULT_SIZES_USD, moves_pct=im.DEFAULT_MOVES_PCT) -> VenueImpact:
        bids, asks = self.levels()
        rec = impact_from_levels(bids, asks, venue=self.venue, symbol=self.symbol, quote=self.quote, quote_usd=self.quote_usd,
                                 sizes_usd=sizes_usd, moves_pct=moves_pct)
        if not self.synced:
            rec.note = f"resyncing ({self.note})"
        elif self.crossed():
            rec.note = "book crossed; waiting for next update"
        return rec
