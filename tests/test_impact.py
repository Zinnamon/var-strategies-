import pytest

from varbot.analysis import impact as im
from varbot.sources.binance import impact_from_book


def test_small_buy_fills_at_best_ask(thin_book):
    asks = im.parse_levels(thin_book["asks"])
    r = im.sweep(asks, 1_000, "buy")
    assert r.levels_consumed == 1
    assert r.avg_price == pytest.approx(asks[0][0])
    assert r.impact_pct_vs_best == 0
    assert not r.exhausted


def test_large_buy_walks_the_book(thin_book):
    asks = im.parse_levels(thin_book["asks"])
    r = im.sweep(asks, 100_000, "buy")
    assert r.levels_consumed > 5
    assert r.final_price > asks[0][0]
    assert r.impact_pct_vs_best > 1.0
    assert r.avg_slippage_pct > 0
    assert r.avg_slippage_pct < r.impact_pct_vs_best  # average is better than the last fill
    assert r.filled_usd == pytest.approx(100_000)


def test_buy_beyond_visible_book_is_flagged(thin_book):
    asks = im.parse_levels(thin_book["asks"])
    total = im.total_depth(asks)
    r = im.sweep(asks, total * 2, "buy")
    assert r.exhausted
    assert r.filled_usd == pytest.approx(total)


def test_cost_to_move_increases_with_move(thin_book):
    asks = im.parse_levels(thin_book["asks"])
    c1 = im.cost_to_move(asks, 1.0, "buy")
    c5 = im.cost_to_move(asks, 5.0, "buy")
    c50 = im.cost_to_move(asks, 50.0, "buy")
    assert 0 < c1.notional_usd < c5.notional_usd
    assert not c1.beyond_visible_book and not c5.beyond_visible_book
    assert c50.beyond_visible_book  # the book ends before +50%


def test_sell_side_mirrors_buy_side(thin_book):
    bids = im.parse_levels(thin_book["bids"])
    bids.sort(key=lambda l: -l[0])
    r = im.sweep(bids, 50_000, "sell")
    assert r.final_price < bids[0][0]
    assert r.impact_pct_vs_best > 0  # reported as a positive magnitude


def test_impact_record_from_book(thin_book):
    rec = impact_from_book(thin_book, symbol="STOUSDT", quote="USDT", sizes_usd=(10_000, 100_000), moves_pct=(1.0, 5.0))
    assert rec.listed and rec.binance_symbol == "STOUSDT"
    assert rec.spread_bps > 0
    assert rec.ask_depth_2pct_usd < rec.ask_depth_usd
    buys = [f for f in rec.fills if f.side == "buy"]
    assert [f.notional_usd for f in buys] == [10_000, 100_000]
    assert buys[1].impact_pct_vs_best > buys[0].impact_pct_vs_best
    assert {m.move_pct for m in rec.move_costs if m.side == "buy"} == {1.0, 5.0}


def test_quote_conversion_scales_prices(thin_book):
    usd = impact_from_book(thin_book, symbol="STOUSDT", quote="USDT", quote_usd=1.0, sizes_usd=(10_000,), moves_pct=(1.0,))
    btc = impact_from_book(thin_book, symbol="STOBTC", quote="BTC", quote_usd=2.0, sizes_usd=(10_000,), moves_pct=(1.0,))
    assert btc.best_ask == pytest.approx(usd.best_ask * 2)
    assert btc.ask_depth_usd == pytest.approx(usd.ask_depth_usd * 2)
    # Each level now holds twice the dollars, so the same $10k consumes fewer levels.
    assert btc.fills[0].impact_pct_vs_best <= usd.fills[0].impact_pct_vs_best
    assert btc.fills[0].levels_consumed <= usd.fills[0].levels_consumed
