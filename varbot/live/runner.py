"""`varbot watch`: keep live order books for a set of tokens across venues, recompute the
impact table every interval, print it, write it to JSON, serve it over HTTP, and raise
alerts when a threshold is crossed. Variational's stats are re-polled every 30 seconds
(the venue's own cache lifetime) so the mark price and OLP quotes stay current too."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from varbot.analysis import impact as im
from varbot.live.book import LocalBook
from varbot.live.feeds import RUNNERS
from varbot.models import ExchangeTicker, Listing
from varbot.report import pct, usd
from varbot.sources import binance, coingecko, variational
from varbot.sources.venues import VENUES, pick_pair

log = logging.getLogger(__name__)

LIVE_VENUES = ["binance", "bybit_spot", "okex"]


@dataclass
class WatchTarget:
    symbol: str  # base symbol, e.g. STO
    listing: Listing | None = None
    coingecko_id: str | None = None
    tickers: list[ExchangeTicker] = field(default_factory=list)
    books: list[LocalBook] = field(default_factory=list)


@dataclass
class WatchState:
    targets: list[WatchTarget]
    sizes_usd: tuple
    moves_pct: tuple
    latest: dict = field(default_factory=dict)  # last computed payload
    lock: threading.Lock = field(default_factory=threading.Lock)


def resolve_targets(symbols: list[str], venue_ids: list[str], *, offline: bool = False) -> list[WatchTarget]:
    """Work out which pairs to subscribe to, from Variational + CoinGecko + Binance info."""
    _, listings = variational.fetch_stats()
    by_sym = {l.symbol.upper(): l for l in listings}
    quote_prices = {"BTC": binance.quote_price_usd("BTC"), "ETH": binance.quote_price_usd("ETH")}
    targets: list[WatchTarget] = []
    for raw in symbols:
        sym = raw.upper().replace("-PERP", "")
        t = WatchTarget(symbol=sym, listing=by_sym.get(sym))
        name = t.listing.name if t.listing else None
        t.coingecko_id, _ = coingecko.resolve_id(sym, name)
        if t.coingecko_id:
            t.tickers = coingecko.exchange_listing_for(t.coingecko_id, sym).tickers
        for vid in venue_ids:
            venue = VENUES[vid]
            pair = None
            if vid == "binance":
                bsym, quote = binance.resolve_symbol(sym)
                if bsym:
                    pair = (sym, quote, 1.0 if quote in binance.STABLE_QUOTES else quote_prices.get(quote, 0.0))
            if pair is None and t.tickers:
                picked = pick_pair(t.tickers, vid)
                if picked:
                    b, q, qusd, _ = picked
                    pair = (b, q, qusd or quote_prices.get(q, 0.0))
            if pair is None or not pair[2]:
                continue
            b, q, qusd = pair
            t.books.append(LocalBook(venue=venue, symbol=venue.pair(b, q), quote=q, quote_usd=qusd))
        targets.append(t)
    return targets


def compute_payload(state: WatchState, platform: dict | None, listings: dict[str, Listing]) -> dict:
    rows = []
    for t in state.targets:
        listing = listings.get(t.symbol) or t.listing
        venues_out = []
        for book in t.books:
            rec = book.impact(state.sizes_usd, state.moves_pct)
            d = rec.model_dump(mode="json")
            d["synced"] = book.synced
            d["age_seconds"] = round(book.age_seconds(), 3) if book.updated_at else None
            d["updates"] = book.updates
            d["resyncs"] = book.resyncs
            venues_out.append(d)
        rows.append({
            "symbol": t.symbol,
            "variational": {
                "mark_price": listing.mark_price if listing else None,
                "volume_24h": listing.volume_24h if listing else None,
                "open_interest_usd": listing.open_interest_usd if listing else None,
                "quotes": [q.model_dump() for q in listing.quotes] if listing else [],
                "impact": [v.model_dump() for v in variational.variational_impact(listing)] if listing else [],
            },
            "venues": venues_out,
        })
    return {"generated_at": time.time(), "platform": platform, "sizes_usd": list(state.sizes_usd), "moves_pct": list(state.moves_pct), "tokens": rows}


def render_table(payload: dict) -> str:
    lines = [time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(payload["generated_at"]))]
    sizes = payload["sizes_usd"]
    head = f"{'token':<8}{'venue':<10}{'pair':<12}{'best ask':>12}{'spread':>9}" + "".join(f"{'buy ' + usd(s):>14}" for s in sizes) + f"{'$ to +5%':>12}{'age':>7}{'state':>10}"
    lines.append(head)
    for tok in payload["tokens"]:
        v = tok["variational"]
        if v["mark_price"]:
            prem = next((x for x in v["impact"] if x["tier"] == "size_100k"), None)
            tail = f"  $100k OLP ask {pct(prem['buy_premium_pct_vs_mark'], 2)} vs mark" if prem else ""
            mark = v["mark_price"]
            lines.append(f"{tok['symbol']:<8}{'Variat.':<10}{'mark':<12}{mark:>12.6g}" + " " * (9 + 14 * len(sizes) + 12 + 7) + tail)
        for ven in tok["venues"]:
            fills = {f["notional_usd"]: f for f in ven["fills"] if f["side"] == "buy"}
            mv = next((m for m in ven["move_costs"] if m["side"] == "buy" and m["move_pct"] == 5.0), None)
            cells = ""
            for s in sizes:
                f = fills.get(s)
                cell = "n/a" if not f or f["impact_pct_vs_best"] is None else pct(f["impact_pct_vs_best"]) + ("*" if f["exhausted"] else "")
                cells += f"{cell:>14}"
            age = ven["age_seconds"]
            synced = ven["synced"]
            state = "live" if synced and age is not None and age < 30 else ("stale" if synced else "sync")
            spread = "n/a" if ven["spread_bps"] is None else f"{ven['spread_bps']:.1f}b"
            move = "n/a" if not mv else usd(mv["notional_usd"]) + ("+" if mv["beyond_visible_book"] else "")
            age_text = "-" if age is None else f"{age:.1f}s"
            pair = ven["binance_symbol"] or ""
            best_ask = ven["best_ask"] or 0
            lines.append(
                f"{tok['symbol']:<8}{ven['venue_name']:<10}{pair:<12}{best_ask:>12.6g}{spread:>9}{cells}{move:>12}{age_text:>7}{state:>10}"
            )
    lines.append("* = visible book exhausted; + = beyond visible book")
    return "\n".join(lines)


class _Handler(BaseHTTPRequestHandler):
    state: WatchState

    def do_GET(self):  # noqa: N802
        with self.state.lock:
            body = json.dumps(self.state.latest).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


def serve(state: WatchState, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _binance_snapshot(symbol: str) -> dict:
    loop = asyncio.get_running_loop()
    # Bypass the disk cache: a snapshot for sync must be fresh.
    from varbot.config import SETTINGS
    from varbot.http import http

    def fetch():
        return http().get(f"{SETTINGS.binance_base}/api/v3/depth", params={"symbol": symbol, "limit": 5000}, ttl=0)

    return await loop.run_in_executor(None, fetch)


def alerts_for(payload: dict, threshold_pct: float, size_usd: float, seen: set) -> list[str]:
    out = []
    for tok in payload["tokens"]:
        for ven in tok["venues"]:
            f = next((x for x in ven["fills"] if x["side"] == "buy" and x["notional_usd"] == size_usd), None)
            key = (tok["symbol"], ven["venue"])
            if f and f["impact_pct_vs_best"] is not None and ven["synced"]:
                if f["impact_pct_vs_best"] >= threshold_pct and key not in seen:
                    seen.add(key)
                    out.append(f"ALERT {tok['symbol']} on {ven['venue_name']}: {usd(size_usd)} buy now moves price {f['impact_pct_vs_best']:.2f}% (>= {threshold_pct}%)")
                elif f["impact_pct_vs_best"] < threshold_pct * 0.8 and key in seen:
                    seen.discard(key)
                    out.append(f"clear {tok['symbol']} on {ven['venue_name']}: impact back to {f['impact_pct_vs_best']:.2f}%")
    return out


async def watch(
    symbols: list[str],
    *,
    venue_ids: list[str] | None = None,
    sizes_usd=im.DEFAULT_SIZES_USD,
    moves_pct=im.DEFAULT_MOVES_PCT,
    interval: float = 2.0,
    json_out: Path | None = None,
    port: int | None = None,
    alert_pct: float | None = None,
    alert_size: float = 100_000,
    duration: float | None = None,
    on_tick: Callable[[str], None] = print,
) -> WatchState:
    venue_ids = [v for v in (venue_ids or LIVE_VENUES) if v in RUNNERS]
    targets = resolve_targets(symbols, venue_ids)
    state = WatchState(targets=targets, sizes_usd=tuple(sizes_usd), moves_pct=tuple(moves_pct))
    if not any(t.books for t in targets):
        raise RuntimeError("none of the requested tokens has a live-capable spot pair on " + ", ".join(VENUES[v].name for v in venue_ids))
    stop = asyncio.Event()
    tasks = []
    for t in targets:
        for book in t.books:
            if book.venue.id == "binance":
                tasks.append(asyncio.create_task(RUNNERS["binance"](book, _binance_snapshot, stop)))
            else:
                tasks.append(asyncio.create_task(RUNNERS[book.venue.id](book, stop)))
    server = serve(state, port) if port else None
    seen: set = set()
    platform: dict | None = None
    listings: dict[str, Listing] = {t.symbol: t.listing for t in targets if t.listing}
    last_var = 0.0
    started = time.time()
    loop = asyncio.get_running_loop()
    try:
        while True:
            if time.time() - last_var >= 30:
                last_var = time.time()
                try:
                    platform, fresh = await loop.run_in_executor(None, lambda: variational.fetch_stats())
                    listings = {l.symbol.upper(): l for l in fresh if l.symbol.upper() in listings or l.symbol.upper() in {t.symbol for t in targets}}
                except Exception as exc:  # noqa: BLE001
                    log.warning("variational refresh failed: %s", exc)
            payload = compute_payload(state, platform, listings)
            with state.lock:
                state.latest = payload
            if json_out:
                tmp = json_out.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload))
                tmp.replace(json_out)
            on_tick(render_table(payload))
            if alert_pct is not None:
                for line in alerts_for(payload, alert_pct, alert_size, seen):
                    on_tick(line)
            if duration is not None and time.time() - started >= duration:
                break
            await asyncio.sleep(interval)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        if server:
            server.shutdown()
    return state
