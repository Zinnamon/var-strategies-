"""WebSocket order-book feeds. Each feed has a pure `handle(book, message)` that applies
one decoded message to a LocalBook, plus an async `run` that owns the connection.

Binance   wss://stream.binance.com:9443/ws/<symbol>@depth@100ms, synced against a REST
          snapshot the way Binance documents it (drop events with u <= lastUpdateId; the
          first kept event must straddle lastUpdateId+1; each next event's U must equal
          the previous u + 1, otherwise resync).
Bybit v5  wss://stream.bybit.com/v5/public/spot, topic orderbook.<depth>.<symbol>:
          a "snapshot" then "delta" messages; size 0 deletes; `u` must increase by 1.
OKX       wss://ws.okx.com:8443/ws/v5/public, channel "books" (400 levels): action
          "snapshot" then "update"; sz 0 deletes; prevSeqId must equal last seqId.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from varbot.live.book import LocalBook

log = logging.getLogger(__name__)

SnapshotFn = Callable[[str], Awaitable[dict]]


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

@dataclass
class BinanceSync:
    """State machine for Binance's snapshot + diff-stream synchronisation."""

    buffer: list[dict] = field(default_factory=list)
    snapshot_id: int | None = None
    first_applied: bool = False


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


async def run_binance(book: LocalBook, snapshot_fn: SnapshotFn, stop: asyncio.Event) -> None:
    import websockets

    url = f"wss://stream.binance.com:9443/ws/{book.symbol.lower()}@depth@100ms"
    sync = BinanceSync()
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                book.desync("connecting")
                sync = BinanceSync()
                need_snapshot = True
                async for raw in ws:
                    if stop.is_set():
                        break
                    ev = json.loads(raw)
                    status = binance_handle(book, sync, ev)
                    if status == "resync":
                        need_snapshot = True
                    if need_snapshot and sync.buffer:
                        need_snapshot = False
                        try:
                            snap = await snapshot_fn(book.symbol)
                            binance_apply_snapshot(book, sync, snap)
                            log.info("binance %s synced at %s", book.symbol, book.last_update_id)
                        except Exception as exc:  # noqa: BLE001 - keep the feed alive
                            log.warning("binance snapshot %s failed: %s", book.symbol, exc)
                            need_snapshot = True
                            await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            book.desync(f"connection error: {exc}")
            log.warning("binance ws %s: %s; reconnecting", book.symbol, exc)
            await asyncio.sleep(3)


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


async def run_bybit(book: LocalBook, stop: asyncio.Event, depth: int = 200) -> None:
    import websockets

    url = "wss://stream.bybit.com/v5/public/spot"
    topic = f"orderbook.{depth}.{book.symbol}"
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                last_ping = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        msg = json.loads(raw)
                        if bybit_handle(book, msg) == "resync":
                            await ws.send(json.dumps({"op": "unsubscribe", "args": [topic]}))
                            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    if time.time() - last_ping > 20:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = time.time()
        except Exception as exc:  # noqa: BLE001
            book.desync(f"connection error: {exc}")
            log.warning("bybit ws %s: %s; reconnecting", book.symbol, exc)
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


async def run_okx(book: LocalBook, stop: asyncio.Event) -> None:
    import websockets

    url = "wss://ws.okx.com:8443/ws/v5/public"
    arg = {"channel": "books", "instId": book.symbol}
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [arg]}))
                last_ping = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None and raw != "pong":
                        msg = json.loads(raw)
                        if okx_handle(book, msg) == "resync":
                            await ws.send(json.dumps({"op": "unsubscribe", "args": [arg]}))
                            await ws.send(json.dumps({"op": "subscribe", "args": [arg]}))
                    if time.time() - last_ping > 20:
                        await ws.send("ping")
                        last_ping = time.time()
        except Exception as exc:  # noqa: BLE001
            book.desync(f"connection error: {exc}")
            log.warning("okx ws %s: %s; reconnecting", book.symbol, exc)
            await asyncio.sleep(3)


RUNNERS = {"binance": run_binance, "bybit_spot": run_bybit, "okex": run_okx}
