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


def test_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
