"""Business-activity research with Claude and its server-side web search / web fetch.

For each token the model is asked to find what the project has actually shipped,
announced, raised or broken in the last ~90 days and to return a fixed JSON shape with
source URLs. The reply is validated into `BusinessResearch`; anything the model could
not source stays empty rather than guessed.
"""

from __future__ import annotations

import json
import logging
import re

from varbot.config import SETTINGS
from varbot.models import BusinessResearch, Development, TokenFundamentals

log = logging.getLogger(__name__)

SYSTEM = """You are a crypto due-diligence analyst. You research one token project at a time
using web search and page fetches, then report only what you could verify in a source you
actually opened. Prefer primary sources (the project's own blog, docs, X account, GitHub,
governance forum, exchange announcements, funding press releases) and reputable news.
Never invent dates, partners, investors or numbers. If something cannot be sourced, leave
it out. Keep every text field short and factual."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences on what the project is and its current state."},
        "what_it_does": {"type": "string"},
        "token_utility": {"type": "string", "description": "What the token is used for (fees, staking, governance, nothing)."},
        "team_and_backers": {"type": "string", "description": "Known team, investors, and raises, with amounts if sourced."},
        "recent_developments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD or YYYY-MM if known"},
                    "headline": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["product", "partnership", "funding", "listing", "token", "governance", "incident", "team", "other"],
                    },
                    "source_url": {"type": ["string", "null"]},
                },
                "required": ["date", "headline", "kind", "source_url"],
                "additionalProperties": False,
            },
        },
        "partnerships": {"type": "array", "items": {"type": "string"}},
        "funding": {"type": "array", "items": {"type": "string"}},
        "positives": {"type": "array", "items": {"type": "string"}},
        "red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exploits, delistings, team departures, unlock cliffs, regulatory actions, abandoned roadmap items, inactive socials.",
        },
        "activity_score": {
            "type": "integer",
            "description": "Whole number from 0 to 10. 0 = no sign of life in 90 days, 10 = shipping, growing, well-funded, visibly active.",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs you opened and relied on."},
    },
    "required": [
        "summary", "what_it_does", "token_utility", "team_and_backers", "recent_developments",
        "partnerships", "funding", "positives", "red_flags", "activity_score", "confidence", "sources",
    ],
    "additionalProperties": False,
}


def build_prompt(symbol: str, f: TokenFundamentals, listing_name: str | None) -> str:
    facts = {
        "symbol": symbol,
        "name": f.name or listing_name,
        "coingecko_id": f.coingecko_id,
        "homepage": f.homepage,
        "x_handle": f.twitter_handle,
        "telegram": f.telegram_channel,
        "github": f.github_repos[:2],
        "categories": f.categories[:6],
        "chains": f.chains[:4],
        "market_cap_usd": f.market_cap_usd,
        "fdv_usd": f.fdv_usd,
        "description_snippet": (f.description or "")[:600] or None,
    }
    facts = {k: v for k, v in facts.items() if v}
    return (
        "Research the business activity of this crypto project over the last 90 days and "
        "its overall standing. Known facts:\n"
        + json.dumps(facts, indent=2)
        + "\n\nLook for: product launches and releases, partnerships and integrations, funding "
        "rounds, exchange listings or delistings, token events (unlocks, buybacks, burns, "
        "emissions changes), governance decisions, security incidents, team changes, and "
        "regulatory news. Also note whether the official channels are active. Then fill the "
        "JSON schema. Use at most ~8 searches and ~6 page fetches."
    )


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def to_research(data: dict, model: str) -> BusinessResearch:
    devs = []
    for d in data.get("recent_developments") or []:
        if isinstance(d, dict) and d.get("headline"):
            devs.append(Development(date=d.get("date"), headline=d["headline"], kind=d.get("kind") or "other", source_url=d.get("source_url")))
    score = data.get("activity_score")
    return BusinessResearch(
        available=True,
        summary=data.get("summary"),
        what_it_does=data.get("what_it_does"),
        token_utility=data.get("token_utility"),
        team_and_backers=data.get("team_and_backers"),
        recent_developments=devs,
        partnerships=[str(x) for x in data.get("partnerships") or []],
        funding=[str(x) for x in data.get("funding") or []],
        positives=[str(x) for x in data.get("positives") or []],
        red_flags=[str(x) for x in data.get("red_flags") or []],
        activity_score=max(0, min(10, int(score))) if isinstance(score, (int, float)) else None,
        confidence=data.get("confidence"),
        sources=[str(x) for x in data.get("sources") or []],
        model=model,
    )


def research_token(symbol: str, f: TokenFundamentals, listing_name: str | None = None, *, max_turns: int = 6) -> BusinessResearch:
    if SETTINGS.offline:
        return BusinessResearch(available=False, note="offline mode")
    if not SETTINGS.anthropic_api_key:
        return BusinessResearch(available=False, note="ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except ImportError:
        return BusinessResearch(available=False, note="anthropic SDK not installed")

    client = anthropic.Anthropic()
    model = SETTINGS.research_model
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6},
    ]
    messages: list[dict] = [{"role": "user", "content": build_prompt(symbol, f, listing_name)}]
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        tools=tools,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    try:
        for _ in range(max_turns):
            with client.beta.messages.stream(
                messages=messages,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            ) as stream:
                resp = stream.get_final_message()
            if resp.stop_reason == "pause_turn":
                # Server tools ran out of time in this turn; continue from where it stopped.
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason == "refusal":
                return BusinessResearch(available=False, note="model declined the request", model=model)
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            data = _extract_json(text)
            if not data:
                return BusinessResearch(available=False, note="no JSON in model reply", model=model)
            return to_research(data, model)
        return BusinessResearch(available=False, note="research did not finish within the turn limit", model=model)
    except anthropic.RateLimitError as exc:
        return BusinessResearch(available=False, note=f"rate limited: {exc}", model=model)
    except anthropic.APIStatusError as exc:
        return BusinessResearch(available=False, note=f"API error {exc.status_code}: {exc.message}", model=model)
    except anthropic.APIConnectionError as exc:
        return BusinessResearch(available=False, note=f"connection error: {exc}", model=model)
