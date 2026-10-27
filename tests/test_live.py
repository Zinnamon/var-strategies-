import asyncio
import json

from varbot.live import feeds
from varbot.live.book import LocalBook
from varbot.live.runner import WatchState, WatchTarget, alerts_for, compute_payload, render_table
from varbot.sources.venues import VENUES


def _book(venue="binance", symbol="STOUSDT"):
    return LocalBook(venue=VENUES[venue], symbol=symbol, quote="USDT")


def _ev(U, u, bids=(), asks=()):
    return {"e": "depthUpdate", "s": "STOUSDT", "U": U, "u": u, "b": [list(x) for x in bids], "a": [list(x) for x in asks]}


def test_binance_sync_buffers_then_replays():
    book, sync = _book(), feeds.BinanceSync()
    assert feeds.binance_handle(book, sync, _ev(1, 5, asks=[("0.031", "1")])) == "buffered"
    assert feeds.binance_handle(book, sync, _ev(6, 12, asks=[("0.032", "7")])) == "buffered"
    assert not book.synced
    feeds.binance_apply_snapshot(book, sync, {"lastUpdateId": 10, "bids": [["0.030", "100"]], "asks": [["0.031", "50"]]})
    # Event 1-5 dropped (u <= 10); event 6-12 straddles 11 and is applied.
    assert book.synced and book.last_update_id == 12
    assert book.asks == {0.031: 50.0, 0.032: 7.0}
    assert feeds.binance_handle(book, sync, _ev(13, 14, asks=[("0.031", "0")])) == "applied"
    assert 0.031 not in book.asks
    assert feeds.binance_handle(book, sync, _ev(16, 17)) == "resync"  # gap: expected 15
    assert not book.synced and book.resyncs >= 1
    assert feeds.binance_handle(book, sync, _ev(18, 19)) == "buffered"


def test_binance_first_event_must_straddle_snapshot():
    book, sync = _book(), feeds.BinanceSync()
    feeds.binance_handle(book, sync, _ev(20, 25))
    feeds.binance_apply_snapshot(book, sync, {"lastUpdateId": 10, "bids": [], "asks": [["1", "1"]]})
    # buffered event 20-25 does not straddle 11 -> resync requested
    assert not book.synced


def test_bybit_snapshot_and_delta():
    book = _book("bybit_spot")
    assert feeds.bybit_handle(book, {"op": "subscribe", "success": True}) == "dropped"
    snap = {"topic": "orderbook.200.STOUSDT", "type": "snapshot", "data": {"s": "STOUSDT", "b": [["0.030", "10"]], "a": [["0.031", "5"]], "u": 100, "seq": 1}}
    assert feeds.bybit_handle(book, snap) == "applied" and book.synced
    delta = {"topic": "orderbook.200.STOUSDT", "type": "delta", "data": {"s": "STOUSDT", "b": [], "a": [["0.031", "0"], ["0.033", "9"]], "u": 101, "seq": 2}}
    assert feeds.bybit_handle(book, delta) == "applied"
    assert book.asks == {0.033: 9.0}
    bad = {"topic": "orderbook.200.STOUSDT", "type": "delta", "data": {"b": [], "a": [], "u": 105, "seq": 3}}
    assert feeds.bybit_handle(book, bad) == "resync"


def test_okx_snapshot_update_and_gap():
    book = _book("okex", "STO-USDT")
    assert feeds.okx_handle(book, {"event": "subscribe", "arg": {}}) == "dropped"
    snap = {"arg": {"channel": "books"}, "action": "snapshot", "data": [{"bids": [["0.030", "10", "0", "1"]], "asks": [["0.031", "5", "0", "1"]], "seqId": 7, "prevSeqId": -1}]}
    assert feeds.okx_handle(book, snap) == "applied" and book.last_update_id == 7
    upd = {"arg": {"channel": "books"}, "action": "update", "data": [{"bids": [["0.030", "0", "0", "0"]], "asks": [], "seqId": 8, "prevSeqId": 7}]}
    assert feeds.okx_handle(book, upd) == "applied" and book.bids == {}
    gap = {"arg": {"channel": "books"}, "action": "update", "data": [{"bids": [], "asks": [], "seqId": 12, "prevSeqId": 11}]}
    assert feeds.okx_handle(book, gap) == "resync"


def test_local_book_impact_and_crossed_detection():
    book = _book()
    book.reset(bids=[("0.030", "1000")], asks=[("0.031", "1000"), ("0.032", "1000")], update_id=1)
    rec = book.impact(sizes_usd=(10,), moves_pct=(1.0,))
    assert rec.listed and rec.best_ask == 0.031 and rec.fills[0].impact_pct_vs_best == 0
    book.apply(bids=[("0.035", "1")])
    assert book.crossed()
    assert "crossed" in book.impact(sizes_usd=(10,), moves_pct=(1.0,)).note


def test_payload_table_and_alerts():
    book = _book()
    book.reset(bids=[("0.030", "100000")], asks=[("0.031", "1000"), ("0.040", "100000")], update_id=1)
    t = WatchTarget(symbol="STO", books=[book])
    state = WatchState(targets=[t], sizes_usd=(10_000, 100_000), moves_pct=(5.0,))
    payload = compute_payload(state, {"num_markets": 1}, {})
    assert payload["tokens"][0]["venues"][0]["synced"] is True
    text = render_table(payload)
    assert "STO" in text and "Binance" in text and "live" in text
    json.dumps(payload)  # serialisable
    seen: set = set()
    first = alerts_for(payload, threshold_pct=5.0, size_usd=100_000, seen=seen)
    assert first and first[0].startswith("ALERT STO on Binance")
    assert alerts_for(payload, 5.0, 100_000, seen) == []  # no repeat while above threshold


def test_smallest_and_expand(monkeypatch):
    from varbot.live import runner
    from varbot.models import Listing, TokenFundamentals

    listings = [
        Listing(ticker="BIG-PERP", name="Big", mark_price=1, volume_24h=1, contract_type="perpetual"),
        Listing(ticker="TINY-PERP", name="Tiny", mark_price=1, volume_24h=1, contract_type="perpetual"),
        Listing(ticker="MID-PERP", name="Mid", mark_price=1, volume_24h=1, contract_type="perpetual"),
        Listing(ticker="AAPL-PERP", name="Apple Inc.", mark_price=1, volume_24h=1, contract_type="perpetual"),
        Listing(ticker="NOCAP-PERP", name="Nocap", mark_price=1, volume_24h=1, contract_type="perpetual"),
    ]
    caps = {"BIG": 5e9, "TINY": 2e6, "MID": 4e7}
    monkeypatch.setattr(runner.coingecko, "fundamentals_for", lambda syms, with_detail=False: {
        s: TokenFundamentals(market_cap_usd=caps.get(s)) for s, _ in syms
    })
    assert runner.smallest_symbols(2, listings) == ["TINY", "MID"]
    monkeypatch.setattr(runner, "smallest_symbols", lambda n, listings=None: ["TINY", "MID"][:n])
    assert runner.expand_symbols(["STO", "smallest:2", "TINY"]) == ["STO", "TINY", "MID"]


def test_bybit_topic_routing():
    assert feeds.bybit_topic_symbol("orderbook.200.STOUSDT") == "STOUSDT"


def test_run_binance_reconnect_loop_stops(monkeypatch):
    """The feed loop must exit when the stop event is set, even if connecting fails."""
    import types

    class FakeConnect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise OSError("no network")

        async def __aexit__(self, *a):
            return False

    fake_ws = types.SimpleNamespace(connect=FakeConnect)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(feeds.asyncio, "sleep", lambda s: real_sleep(0))

    async def go():
        book = _book()
        stop = asyncio.Event()

        async def snap(sym):
            return {}

        task = asyncio.create_task(feeds.run_binance([book], snap, stop))
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1)
        assert book.resyncs >= 1 and "no network" in book.note

    asyncio.run(go())
