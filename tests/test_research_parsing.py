from varbot.models import TokenFundamentals
from varbot.research.business import SCHEMA, _extract_json, build_prompt, to_research


def test_extract_json_tolerates_prose():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('Here you go:\n{"a": {"b": [1,2]}}\nthanks') == {"a": {"b": [1, 2]}}
    assert _extract_json("nothing here") is None


def test_to_research_keeps_only_valid_developments():
    data = {
        "summary": "s", "activity_score": 7.0, "confidence": "high",
        "recent_developments": [
            {"date": "2026-10-01", "headline": "Shipped v2", "kind": "product", "source_url": "https://x.y"},
            {"headline": ""}, "garbage",
        ],
        "sources": ["https://x.y"],
    }
    r = to_research(data, "claude-opus-5-5")
    assert r.available and r.activity_score == 7
    assert len(r.recent_developments) == 1 and r.recent_developments[0].kind == "product"
    assert r.model == "claude-opus-5-5"


def test_prompt_only_includes_known_facts():
    f = TokenFundamentals(name="StakeStone", homepage="https://stakestone.io", twitter_handle="Stake_Stone")
    p = build_prompt("STO", f, None)
    assert "stakestone.io" in p and "Stake_Stone" in p
    assert "market_cap_usd" not in p  # unknown fields are omitted, not sent as null


def test_research_results_are_cached(tmp_path, monkeypatch):
    from varbot.models import BusinessResearch
    from varbot.research import business

    monkeypatch.setattr(business.SETTINGS, "cache_dir", tmp_path)
    monkeypatch.setattr(business.SETTINGS, "offline", False)
    monkeypatch.setattr(business.SETTINGS, "anthropic_api_key", "k")
    calls = []

    def fake_live(symbol, f, name, *, max_turns):
        calls.append(symbol)
        return BusinessResearch(available=True, summary="fresh", activity_score=6, model="m")

    monkeypatch.setattr(business, "_research_live", fake_live)
    f = TokenFundamentals(coingecko_id="x-coin")
    first = business.research_token("X", f)
    second = business.research_token("X", f)
    assert first.summary == "fresh" and second.summary == "fresh" and second.note == "cached"
    assert calls == ["X"]  # the second call did not pay for a new run
    business.research_token("X", f, force=True)
    assert calls == ["X", "X"]
    # failures are not cached
    monkeypatch.setattr(business, "_research_live", lambda *a, **k: BusinessResearch(available=False, note="boom"))
    assert business.research_token("Y", TokenFundamentals(coingecko_id="y")).note == "boom"
    assert not (tmp_path / "research" / "y.json").exists()


def test_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


def test_schema_has_no_numeric_bounds():
    """The API rejects minimum/maximum on numeric fields in output_config.format.schema."""
    def walk(node):
        if isinstance(node, dict):
            assert "minimum" not in node and "maximum" not in node, node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SCHEMA)
    assert to_research({"activity_score": 14}, "m").activity_score == 10
    assert to_research({"activity_score": -2}, "m").activity_score == 0
