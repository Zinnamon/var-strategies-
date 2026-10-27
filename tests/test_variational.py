from varbot.analysis.classify import base_symbol, classify, split_multiplier
from varbot.sources.variational import parse_stats, variational_impact


def test_parse_stats_reads_every_listing(stats_body):
    platform, listings = parse_stats(stats_body)
    assert platform["num_markets"] == stats_body["num_markets"] > 500
    assert len(listings) == len(stats_body["listings"])
    btc = next(l for l in listings if l.ticker == "BTC-PERP")
    assert btc.symbol == "BTC"
    assert btc.mark_price > 1000
    assert btc.open_interest_usd == btc.long_oi_usd + btc.short_oi_usd > 0
    assert btc.funding_interval_seconds == 28800
    assert {q.tier for q in btc.quotes} >= {"base", "size_1k", "size_100k"}


def test_variational_impact_is_premium_over_mark(stats_body):
    _, listings = parse_stats(stats_body)
    sto = next(l for l in listings if l.ticker == "STO-PERP")
    rows = variational_impact(sto)
    tier = {r.tier: r for r in rows}
    # The $100k quote must be worse than the $1k quote on a thin token.
    assert tier["size_100k"].buy_premium_pct_vs_mark > tier["size_1k"].buy_premium_pct_vs_mark
    assert tier["size_100k"].spread_bps > tier["base"].spread_bps


def test_classification_separates_tradfi_from_crypto(stats_body):
    _, listings = parse_stats(stats_body)
    by = {l.ticker: classify(l) for l in listings}
    assert by["BTC-PERP"].asset_class == "crypto"
    assert by["STO-PERP"].asset_class == "crypto"
    assert by["SPX-PERP"].asset_class == "crypto"  # SPX6900 token, not the index
    assert by["AAPL-PERP"].asset_class == "equity"
    assert by["QQQ-PERP"].asset_class == "etf"
    assert by["XAU-PERP"].asset_class == "commodity"
    assert by["ANTHROPIC-PERP"].asset_class == "pre_ipo"
    assert by["US500-PERP"].asset_class == "etf"
    # Unknown company name falls back to the name heuristic
    assert by["QNTX-PERP"].asset_class == "equity"


def test_multiplier_and_disambiguator_handling():
    assert split_multiplier("1000BONK") == ("BONK", 1000)
    assert split_multiplier("1MPEPE") == ("PEPE", 1_000_000)
    assert split_multiplier("1INCH") == ("1INCH", 1)
    assert base_symbol("OPN_OPINION") == "OPN"
    assert base_symbol("1000SHIB") == "SHIB"
