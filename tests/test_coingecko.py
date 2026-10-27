from varbot.models import TokenFundamentals
from varbot.sources import coingecko as cg


def test_resolve_prefers_override_then_name(monkeypatch):
    monkeypatch.setattr(cg, "coin_list", lambda: [
        {"id": "zzq-a", "symbol": "zzq", "name": "Zzq Protocol"},
        {"id": "zzq-real", "symbol": "zzq", "name": "Zed Zed Q"},
        {"id": "lonely", "symbol": "lnly", "name": "Lonely"},
    ])
    monkeypatch.setattr(cg, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network expected")))
    assert cg.resolve_id("BTC", "Bitcoin") == ("bitcoin", "override table")
    assert cg.resolve_id("ZZQ", "Zed-Zed Q") == ("zzq-real", "symbol and name match")
    assert cg.resolve_id("LNLY", "Whatever") == ("lonely", "unique symbol match")


def test_resolve_ambiguous_uses_search_rank(monkeypatch):
    monkeypatch.setattr(cg, "coin_list", lambda: [
        {"id": "a-token", "symbol": "abc", "name": "A Token"},
        {"id": "b-token", "symbol": "abc", "name": "B Token"},
    ])
    monkeypatch.setattr(cg, "_get", lambda path, params=None, ttl=None: {
        "coins": [
            {"id": "b-token", "symbol": "ABC", "name": "B Token", "market_cap_rank": 300},
            {"id": "a-token", "symbol": "ABC", "name": "A Token", "market_cap_rank": 2000},
        ]
    })
    cid, note = cg.resolve_id("ABC", "Unknown Name")
    assert cid == "b-token"
    assert "ambiguous" in note


def test_build_fundamentals_extracts_links_and_snapshots():
    market = {"id": "x", "name": "X Coin", "symbol": "x", "current_price": 0.5, "market_cap": 5e6,
              "fully_diluted_valuation": 2e7, "total_volume": 1e5, "circulating_supply": 1e7, "total_supply": 4e7,
              "ath": 5.0, "ath_change_percentage": -90.0, "price_change_percentage_24h_in_currency": 1.5}
    detail = {"name": "X Coin", "symbol": "x", "categories": ["DeFi", None],
              "description": {"en": "<p>Hello <b>world</b></p>"},
              "links": {"homepage": ["https://x.io", ""], "twitter_screen_name": "@xcoin",
                        "telegram_channel_identifier": "", "chat_url": ["https://t.me/xcoin_chat", "https://discord.gg/abc"],
                        "repos_url": {"github": ["https://github.com/x/x"]}},
              "platforms": {"ethereum": "0x1", "": ""},
              "community_data": {"twitter_followers": 1234, "telegram_channel_user_count": 99},
              "developer_data": {"stars": 10, "forks": 2, "commit_count_4_weeks": 7}}
    f = cg.build_fundamentals("x", "n", market, detail)
    assert f.market_cap_usd == 5e6 and f.float_ratio == 0.25
    assert f.description == "Hello world"
    assert f.twitter_handle == "xcoin"
    assert f.telegram_channel == "xcoin_chat"
    assert f.discord_url == "https://discord.gg/abc"
    assert f.chains == ["ethereum"]
    assert f.commits_4w == 7 and f.twitter_followers == 1234


def test_add_detail_fills_blanks_only(monkeypatch):
    f = TokenFundamentals(coingecko_id="x", name="Keep Me", market_cap_usd=1.0)
    monkeypatch.setattr(cg, "fetch_detail", lambda cid: {"name": "Other", "links": {"twitter_screen_name": "h"}})
    cg.add_detail(f)
    assert f.name == "Keep Me"
    assert f.twitter_handle == "h"
    assert f.market_cap_usd == 1.0
