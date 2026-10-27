"""WebSocket order-book feeds. One connection per venue carries every watched symbol.
Each venue has a pure `*_handle(book, message)` that applies one decoded message to a
LocalBook (unit-tested with synthetic messages) and an async `run_*` that owns the
connection and routes messages to the right book.

Binance   wss://stream.binance.com:9443/stream?streams=<a>@depth@100ms/<b>@depth@100ms
          Each book is synced against a REST snapshot the way Binance documents it:
          drop events with u <= lastUpdateId, the first kept event must straddle
          lastUpdateId+1, and each later event's U must equal the previous u + 1,
          otherwise resync.
Bybit v5  wss://stream.bybit.com/v5/public/spot, topics orderbook.<depth>.<symbol>
          (10 per subscribe message): a "snapshot" then "delta"s; size 0 deletes;
          `u` must increase by 1.
OKX       wss://ws.okx.com:8443/ws/v5/public, channel "books" (400 levels) per instId:
          action "snapshot" then "update"; sz 0 deletes; prevSeqId must equal the
          last seqId.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from varbot.live.book import LocalBook

log = logging.getLogger(__name__)

SnapshotFn = Callable[[str], Awaitable[dict]]
BINANCE_STREAMS_PER_CONNECTION = 100  # Binance allows up to 1024; keep reconnects cheap
BYBIT_ARGS_PER_MESSAGE = 10


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

@dataclass
class BinanceSync:
    """State machine for Binance's snapshot + diff-stream synchronisation."""

    buffer: list[dict] = field(default_factory=list)
    snapshot_id: int | None = None
    first_applied: bool = False
    fetching: bool = False


def binance_apply_snapshot(book: LocalBook, sync: BinanceSync, snapshot: dict) -> None:
    sync.snapshot_id = int(snapshot["lastUpdateId"])
    book.reset(snapshot.get("bids", []), snapshot.get("asks", []), update_id=sync.snapshot_id)
    sync.first_applied = False
    # Replay buffered events that arrived while the snapshot was in flight.
    pending, sync.buffer = sync.buffer, []
    for ev in pending:
        binance_handle(book, sync, ev)


def binance_handle(book: LocalBook, sync: BinanceSync, ev: dict) -> str:
    """Apply one depthUpdate. Returns 'applied' | 'buffered' | 'dropped' | 'resync'."""
    if ev.get("e") != "depthUpdate":
        return "dropped"
    first, last = int(ev["U"]), int(ev["u"])
    if sync.snapshot_id is None or not book.synced:
        sync.buffer.append(ev)
        if len(sync.buffer) > 5000:
            del sync.buffer[:-5000]
        return "buffered"
    if last <= book.last_update_id:
        return "dropped"
    if not sync.first_applied:
        if not (first <= book.last_update_id + 1 <= last):
            book.desync(f"first event U={first} u={last} does not straddle snapshot {book.last_update_id}")
            sync.snapshot_id = None
            return "resync"
        sync.first_applied = True
    elif first != book.last_update_id + 1:
        book.desync(f"gap: expected U={book.last_update_id + 1}, got {first}")
        sync.snapshot_id = None
        return "resync"
    book.apply(ev.get("b", []), ev.get("a", []))
    book.last_update_id = last
    return "applied"


async def _binance_sync_one(book: LocalBook, sync: BinanceSync, snapshot_fn: SnapshotFn) -> None:
    if sync.fetching:
        return
    sync.fetching = True
    try:
        snap = await snapshot_fn(book.symbol)
        binance_apply_snapshot(book, sync, snap)
        log.info("binance %s synced at %s", book.symbol, book.last_update_id)
    except Exception as exc:  # noqa: BLE001 - keep the feed alive
        log.warning("binance snapshot %s failed: %s", book.symbol, exc)
        await asyncio.sleep(2)
    finally:
        sync.fetching = False


async def run_binance(books: Sequence[LocalBook], snapshot_fn: SnapshotFn, stop: asyncio.Event) -> None:
    import websockets

    by_symbol = {b.symbol.upper(): b for b in books}
    streams = "/".join(f"{b.symbol.lower()}@depth@100ms" for b in books)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    while not stop.is_set():
        syncs = {s: BinanceSync() for s in by_symbol}
        pending: set[asyncio.Task] = set()
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
                for b in books:
                    b.desync("connecting")
                async for raw in ws:
                    if stop.is_set():
                        break
                    msg = json.loads(raw)
                    ev = msg.get("data", msg)
                    sym = (ev.get("s") or "").upper()
                    book = by_symbol.get(sym)
                    if book is None:
                        continue
                    sync = syncs[sym]
                    status = binance_handle(book, sync, ev)
                    if status in ("buffered", "resync") and not book.synced and not sync.fetching:
                        task = asyncio.create_task(_binance_sync_one(book, sync, snapshot_fn))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
        except Exception as exc:  # noqa: BLE001
            for b in books:
                b.desync(f"connection error: {exc}")
            log.warning("binance ws (%d symbols): %s; reconnecting", len(books), exc)
            await asyncio.sleep(3)
        finally:
            for task in pending:
                task.cancel()


# ---------------------------------------------------------------------------
# Bybit v5
# ---------------------------------------------------------------------------

def bybit_handle(book: LocalBook, msg: dict) -> str:
    if msg.get("op") == "subscribe" or "topic" not in msg:
        return "dropped"
    data = msg.get("data") or {}
    seq = int(data.get("u", 0))
    kind = msg.get("type")
    if kind == "snapshot":
        book.reset(data.get("b", []), data.get("a", []), update_id=seq)
        return "applied"
    if kind != "delta":
        return "dropped"
    if not book.synced:
        return "dropped"
    if book.last_update_id is not None and seq != book.last_update_id + 1 and seq != 1:
        # Bybit resets u to 1 when it sends a fresh snapshot after a service restart.
        book.desync(f"gap: expected u={book.last_update_id + 1}, got {seq}")
        return "resync"
    book.apply(data.get("b", []), data.get("a", []))
    book.last_update_id = seq
    return "applied"


def bybit_topic_symbol(topic: str) -> str:
    return topic.rsplit(".", 1)[-1].upper()


async def run_bybit(books: Sequence[LocalBook], stop: asyncio.Event, depth: int = 200) -> None:
    import websockets

    url = "wss://stream.bybit.com/v5/public/spot"
    by_symbol = {b.symbol.upper(): b for b in books}
    topics = [f"orderbook.{depth}.{b.symbol}" for b in books]

    async def subscribe(ws, args):
        for i in range(0, len(args), BYBIT_ARGS_PER_MESSAGE):
            await ws.send(json.dumps({"op": "subscribe", "args": args[i : i + BYBIT_ARGS_PER_MESSAGE]}))

    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2**23) as ws:
                await subscribe(ws, topics)
                last_ping = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        msg = json.loads(raw)
                        topic = msg.get("topic")
                        book = by_symbol.get(bybit_topic_symbol(topic)) if topic else None
                        if book is not None and bybit_handle(book, msg) == "resync":
                            await ws.send(json.dumps({"op": "unsubscribe", "args": [topic]}))
                            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    if time.time() - last_ping > 20:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = time.time()
        except Exception as exc:  # noqa: BLE001
            for b in books:
                b.desync(f"connection error: {exc}")
            log.warning("bybit ws (%d symbols): %s; reconnecting", len(books), exc)
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

def okx_handle(book: LocalBook, msg: dict) -> str:
    if "data" not in msg or msg.get("event"):
        return "dropped"
    action = msg.get("action")
    for d in msg["data"]:
        seq = int(d.get("seqId", 0))
        prev = int(d.get("prevSeqId", -1))
        bids = [(r[0], r[1]) for r in d.get("bids", [])]
        asks = [(r[0], r[1]) for r in d.get("asks", [])]
        if action == "snapshot":
            book.reset(bids, asks, update_id=seq)
            continue
        if action != "update" or not book.synced:
            return "dropped"
        if book.last_update_id is not None and prev != book.last_update_id and prev != -1:
            book.desync(f"gap: expected prevSeqId={book.last_update_id}, got {prev}")
            return "resync"
        book.apply(bids, asks)
        book.last_update_id = seq
    return "applied"


async def run_okx(books: Sequence[LocalBook], stop: asyncio.Event) -> None:
    import websockets

    url = "wss://ws.okx.com:8443/ws/v5/public"
    by_inst = {b.symbol.upper(): b for b in books}
    args = [{"channel": "books", "instId": b.symbol} for b in books]
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2**23) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": args}))
                last_ping = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None and raw != "pong":
                        msg = json.loads(raw)
                        inst = ((msg.get("arg") or {}).get("instId") or "").upper()
                        book = by_inst.get(inst)
                        if book is not None and okx_handle(book, msg) == "resync":
                            arg = {"channel": "books", "instId": book.symbol}
                            await ws.send(json.dumps({"op": "unsubscribe", "args": [arg]}))
                            await ws.send(json.dumps({"op": "subscribe", "args": [arg]}))
                    if time.time() - last_ping > 20:
                        await ws.send("ping")
                        last_ping = time.time()
        except Exception as exc:  # noqa: BLE001
            for b in books:
                b.desync(f"connection error: {exc}")
            log.warning("okx ws (%d symbols): %s; reconnecting", len(books), exc)
            await asyncio.sleep(3)


RUNNERS = {"binance": run_binance, "bybit_spot": run_bybit, "okex": run_okx}
