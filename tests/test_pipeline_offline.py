"""End-to-end offline: fixtures in, markdown/CSV/JSON out, no network."""

import json

from click.testing import CliRunner

from varbot import cli
from varbot.analysis.scoring import score_all
from varbot.models import BinanceImpact, BusinessResearch, TelegramMetrics, TokenFundamentals, XMetrics
from varbot.research.business import to_research
from varbot.sources.binance import impact_from_book


def test_markets_command_offline():
    runner = CliRunner()
    res = runner.invoke(cli.main, ["--offline", "markets"])
    assert res.exit_code == 0, res.output
    assert "BTC-PERP" in res.output and "STO-PERP" in res.output
    assert "AAPL-PERP" not in res.output  # equities hidden by default
    res_all = runner.invoke(cli.main, ["--offline", "markets", "--all"])
    assert "AAPL-PERP" in res_all.output


def test_scan_command_offline_produces_files(tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli.main, ["--offline", "scan", "--no-social", "--out", str(tmp_path), "--workers", "1"])
    assert res.exit_code == 0, res.output
    md = (tmp_path / "scan.md").read_text()
    assert "# Variational token scan" in md
    assert "STO-PERP" in md
    data = json.loads((tmp_path / "scan.json").read_text())
    assert any(r["listing"]["ticker"] == "ERA-PERP" for r in data)
    csv_text = (tmp_path / "scan.csv").read_text()
    assert csv_text.splitlines()[0].startswith("ticker,name,coingecko_id")
    # Offline: Binance not reachable, so every token says so rather than inventing numbers.
    assert all(not r["binance"]["listed"] for r in data)


def test_scores_and_flags(thin_book):
    f = TokenFundamentals(market_cap_usd=8_000_000, fdv_usd=40_000_000, volume_24h_usd=200_000,
                          circulating_supply=200e6, total_supply=1e9, ath_change_pct=-95, github_repos=["x"], commits_4w=0)
    b = impact_from_book(thin_book, symbol="STOUSDT", quote="USDT")
    x = XMetrics(available=True, handle="t", followers=120_000, posts_30d=2, engagement_rate_pct=0.005, days_since_last_post=20)
    tg = TelegramMetrics(available=True, kind="channel", members=3000, posts_30d=1, days_since_last_post=25)
    r = to_research({"activity_score": 3, "red_flags": ["token unlock in November"], "confidence": "medium"}, "m")
    s = score_all(f, b, x, tg, r)
    assert s.overall is not None and 0 <= s.overall <= 10
    joined = " ".join(s.flags)
    assert "unlock overhang" in joined
    assert "all-time high" in joined
    assert "silent" in joined
    assert "bought followers" in joined
    assert "research: token unlock" in joined
    assert "$100k Binance buy moves price" in joined or "exhausts" in joined


def test_unlisted_binance_scores_none():
    s = score_all(TokenFundamentals(), BinanceImpact(listed=False, note="no market"), XMetrics(), TelegramMetrics(), BusinessResearch())
    assert s.liquidity is None and s.overall is None
    assert "not on Binance spot" in s.flags
