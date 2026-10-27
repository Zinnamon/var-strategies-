from varbot.models import ExchangeTicker
from varbot.sources import venues
from varbot.sources.coingecko import build_exchange_listing


def _ticker(ex_id, name, base="STO", target="USDT", vol=1000.0, **kw):
    return {
        "base": base, "target": target, "market": {"identifier": ex_id, "name": name},
        "converted_last": {"usd": 0.03}, "converted_volume": {"usd": vol}, "bid_ask_spread_percentage": 0.5,
        "trust_score": kw.get("trust", "green"), "is_stale": kw.get("stale", False), "is_anomaly": kw.get("anomaly", False),
        "trade_url": kw.get("url"),
    }


RAW = [
    _ticker("binance", "Binance", vol=500_000),
    _ticker("bybit_spot", "Bybit", vol=200_000),
    _ticker("gate", "Gate", vol=50_000),
    _ticker("gate", "Gate", target="BTC", vol=5_000),
    _ticker("uniswap_v3", "Uniswap V3 (Ethereum)", target="WETH", vol=80_000),
    _ticker("shady", "Shady Exchange", vol=9_000_000, trust="red", anomaly=True),
    _ticker("binance", "Binance", base="USDT", target="STO", vol=1),  # token on the quote side: ignored
]


def test_exchange_listing_summary():
    e = build_exchange_listing(RAW, "STO")
    assert e.available
    assert e.exchanges[:3] == ["Binance", "Bybit", "Uniswap V3 (Ethereum)"]  # anomalous volume excluded from ranking
    assert "Shady Exchange" in e.exchanges
    assert e.major_cex == ["Binance", "Bybit", "Gate"]
    assert e.dex_count == 1 and e.cex_count == 4
    assert e.total_spot_volume_usd == 500_000 + 200_000 + 55_000 + 80_000
    assert round(e.top_exchange_share_pct) == round(500_000 / 835_000 * 100)
    assert len(e.tickers) == 6


def test_pick_pair_prefers_stable_quote():
    e = build_exchange_listing(RAW, "STO")
    base, quote, quote_usd, vol = venues.pick_pair(e.tickers, "gate")
    assert (base, quote, quote_usd, vol) == ("STO", "USDT", 1.0, 50_000)
    assert venues.pick_pair(e.tickers, "kraken") is None


def test_venue_parsers():
    assert venues.VENUES["bybit_spot"].parse({"result": {"b": [["1", "2"]], "a": [["3", "4"]]}}) == ([["1", "2"]], [["3", "4"]])
    assert venues.VENUES["okex"].parse({"data": [{"bids": [["1", "2", "0", "1"]], "asks": [["3", "4", "0", "2"]]}]}) == ([["1", "2"]], [["3", "4"]])
    assert venues.VENUES["kraken"].parse({"result": {"XSTOZUSD": {"bids": [["1", "2", 0]], "asks": [["3", "4", 0]]}}}) == ([["1", "2"]], [["3", "4"]])
    assert venues.VENUES["huobi"].parse({"tick": {"bids": [[1, 2]], "asks": [[3, 4]]}}) == ([[1, 2]], [[3, 4]])
    assert venues.VENUES["crypto_com"].parse({"result": {"data": [{"bids": [["1", "2", "1"]], "asks": [["3", "4", "1"]]}]}}) == ([["1", "2"]], [["3", "4"]])
    assert venues.VENUES["kucoin"].parse({"data": {"bids": [["1", "2"]], "asks": [["3", "4"]]}}) == ([["1", "2"]], [["3", "4"]])


def test_pair_spelling():
    assert venues.VENUES["okex"].pair("STO", "USDT") == "STO-USDT"
    assert venues.VENUES["gate"].pair("STO", "USDT") == "STO_USDT"
    assert venues.VENUES["kraken"].pair("BTC", "USD") == "XBTUSD"
    assert venues.VENUES["huobi"].pair("STO", "USDT") == "stousdt"
    assert venues.venue_by_name("OKX").id == "okex" and venues.venue_by_name("coinbase").id == "gdax"


def test_impact_from_levels_marks_capped_books(thin_book):
    v = venues.VENUES["gdax"]  # cap 50
    bids, asks = thin_book["bids"], thin_book["asks"]
    rec = venues.impact_from_levels(bids[:50], asks[:50], venue=v, symbol="STO-USD", quote="USD")
    assert rec.venue_name == "Coinbase" and rec.listed
    assert "capped" not in rec.note  # 41 asks < 50 cap
    rec2 = venues.impact_from_levels(bids, asks * 2, venue=v, symbol="STO-USD", quote="USD")
    assert "capped" in rec2.note


def test_all_venue_impacts_only_listed_venues(monkeypatch):
    e = build_exchange_listing(RAW, "STO")
    calls = []

    def fake_fetch(venue, symbol):
        calls.append((venue.id, symbol))
        return [["0.03", "1000"]], [["0.031", "1000"]]

    monkeypatch.setattr(venues, "fetch_book", fake_fetch)
    out = venues.all_venue_impacts("STO", e.tickers, ["bybit_spot", "okex", "gate"], sizes_usd=(10,), moves_pct=(1.0,))
    assert [(c[0], c[1]) for c in calls] == [("bybit_spot", "STOUSDT"), ("gate", "STO_USDT")]
    assert [v.venue_name for v in out] == ["Bybit", "Gate"]
    assert all(v.fills for v in out)


def test_non_stable_quote_needs_price(monkeypatch):
    tickers = [ExchangeTicker(exchange_id="gate", exchange_name="Gate", base="STO", target="BTC", pair="STO/BTC", volume_usd=5.0)]
    monkeypatch.setattr(venues, "fetch_book", lambda venue, symbol: ([["0.000001", "10"]], [["0.0000011", "10"]]))
    none = venues.all_venue_impacts("STO", tickers, ["gate"], quote_prices={})
    assert none[0].listed and not none[0].fills and "no USD price" in none[0].note
    priced = venues.all_venue_impacts("STO", tickers, ["gate"], quote_prices={"BTC": 100_000.0}, sizes_usd=(1,), moves_pct=(1.0,))
    assert priced[0].best_ask == 0.11
