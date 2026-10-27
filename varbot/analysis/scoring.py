"""Turn the raw records into 0-10 scores and plain-language flags.

The scores are deliberately simple and transparent; every threshold is in this file.
They exist to rank a long list, not to replace reading the report.
"""

from __future__ import annotations

import math

from varbot.models import BinanceImpact, BusinessResearch, ExchangeListing, PumpStats, Scores, TelegramMetrics, TokenFundamentals, XMetrics


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def _log_scale(value: float | None, lo: float, hi: float) -> float | None:
    """0 at `lo`, 10 at `hi`, log-linear in between."""
    if value is None or value <= 0:
        return None
    return _clamp((math.log10(value) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 10)


def liquidity_score(b: BinanceImpact) -> tuple[float | None, list[str]]:
    flags: list[str] = []
    if not b.listed:
        flags.append("not on Binance spot")
        return None, flags
    if not b.fills:
        return None, ["Binance book unavailable"]
    buy100k = next((f for f in b.fills if f.side == "buy" and f.notional_usd == 100_000), None)
    score_parts = []
    if buy100k and buy100k.impact_pct_vs_best is not None:
        imp = buy100k.impact_pct_vs_best
        # 0.05% impact -> 10, 5% impact -> 0
        score_parts.append(_clamp(10 - (math.log10(max(imp, 0.01)) - math.log10(0.05)) / 2 * 10))
        if buy100k.exhausted:
            flags.append("$100k buy exhausts the visible Binance book")
        elif imp > 2:
            flags.append(f"$100k Binance buy moves price {imp:.1f}%")
    if b.ask_depth_2pct_usd is not None:
        score_parts.append(_log_scale(b.ask_depth_2pct_usd, 10_000, 5_000_000) or 0.0)
        if b.ask_depth_2pct_usd < 50_000:
            flags.append(f"only ${b.ask_depth_2pct_usd:,.0f} of asks within 2% on Binance")
    if b.volume_24h_usd is not None:
        score_parts.append(_log_scale(b.volume_24h_usd, 100_000, 100_000_000) or 0.0)
    return (sum(score_parts) / len(score_parts) if score_parts else None), flags


def social_score(x: XMetrics, tg: TelegramMetrics) -> tuple[float | None, list[str]]:
    flags: list[str] = []
    parts: list[float] = []
    if x.available:
        parts.append(_log_scale(x.followers, 1_000, 1_000_000) or 0.0)
        cadence = _clamp(x.posts_30d / 3)  # 30 posts a month -> 10
        parts.append(cadence)
        if x.engagement_rate_pct is not None:
            parts.append(_clamp(x.engagement_rate_pct / 0.5 * 10))  # 0.5% per post -> 10
        if x.days_since_last_post is not None and x.days_since_last_post > 14:
            flags.append(f"X account silent for {x.days_since_last_post:.0f} days")
        if x.followers and x.engagement_rate_pct is not None and x.followers > 50_000 and x.engagement_rate_pct < 0.02:
            flags.append("large X following with very low engagement (possible bought followers)")
    if tg.available:
        if tg.members is not None:
            parts.append(_log_scale(tg.members, 500, 200_000) or 0.0)
        if tg.kind == "channel":
            parts.append(_clamp(tg.posts_30d / 2))
            if tg.days_since_last_post is not None and tg.days_since_last_post > 14:
                flags.append(f"Telegram channel silent for {tg.days_since_last_post:.0f} days")
    if not parts:
        return None, flags
    return sum(parts) / len(parts), flags


def fundamentals_score(f: TokenFundamentals) -> tuple[float | None, list[str]]:
    flags: list[str] = []
    if not f.market_cap_usd:
        return None, ["no market cap data"]
    parts = [_log_scale(f.market_cap_usd, 1_000_000, 10_000_000_000) or 0.0]
    if f.volume_to_mcap is not None:
        v = f.volume_to_mcap
        parts.append(_clamp(v / 0.2 * 10))
        if v > 1.0:
            flags.append(f"24h volume is {v:.1f}x market cap (wash trading or a news spike)")
        elif v < 0.01:
            flags.append("24h volume under 1% of market cap")
    fr = f.float_ratio
    if fr is not None:
        parts.append(_clamp(fr * 10))
        if fr < 0.3:
            flags.append(f"only {fr*100:.0f}% of supply circulating (unlock overhang)")
    if f.ath_change_pct is not None and f.ath_change_pct < -90:
        flags.append(f"{f.ath_change_pct:.0f}% from all-time high")
    if f.commits_4w is not None and f.commits_4w == 0 and f.github_repos:
        flags.append("no GitHub commits in the last 4 weeks")
    return sum(parts) / len(parts), flags


def business_score(r: BusinessResearch) -> tuple[float | None, list[str]]:
    if not r.available or r.activity_score is None:
        return None, []
    flags = [f"research: {x}" for x in r.red_flags[:3]]
    return float(r.activity_score), flags


def activity_score(f: TokenFundamentals, x: XMetrics, tg: TelegramMetrics, e: ExchangeListing, r: BusinessResearch, p: PumpStats) -> tuple[float | None, list[str]]:
    """Is anyone still working on this? Posting, committing, listed widely, traded, and
    (if research ran) shipping. Each signal 0-10, averaged over the signals we have."""
    parts: list[float] = []
    flags: list[str] = []
    if x.available:
        parts.append(_clamp(x.posts_30d / 2))  # 20 posts a month -> 10
    if tg.available and tg.kind == "channel":
        parts.append(_clamp(tg.posts_30d / 1.5))
    if f.commits_4w is not None:
        parts.append(_clamp(f.commits_4w / 4))  # 40 commits a month -> 10
    if e.available:
        parts.append(_clamp(e.cex_count * 1.5))
        if e.total_spot_volume_usd is not None:
            parts.append(_log_scale(e.total_spot_volume_usd, 20_000, 20_000_000) or 0.0)
    if r.available and r.activity_score is not None:
        parts.append(float(r.activity_score))
        parts.append(float(r.activity_score))  # research counts double: it reads the actual news
    if p.available and p.volume_30d_vs_180d is not None:
        parts.append(_clamp(p.volume_30d_vs_180d * 4))  # volume 2.5x its 6-month median -> 10
    if not parts:
        return None, flags
    score = sum(parts) / len(parts)
    silent = (not x.available or x.posts_30d == 0) and (not tg.available or tg.posts_30d == 0)
    if silent and (f.commits_4w or 0) == 0 and (x.available or tg.available or f.commits_4w is not None):
        flags.append("no posts or commits in 30 days")
    return score, flags


def score_all(
    f: TokenFundamentals, b: BinanceImpact, x: XMetrics, tg: TelegramMetrics, r: BusinessResearch,
    e: ExchangeListing | None = None, p: PumpStats | None = None,
) -> Scores:
    e = e or ExchangeListing()
    p = p or PumpStats()
    liq, f1 = liquidity_score(b)
    soc, f2 = social_score(x, tg)
    fun, f3 = fundamentals_score(f)
    bus, f4 = business_score(r)
    act, f5 = activity_score(f, x, tg, e, r, p)
    weights = {"liquidity": (liq, 0.3), "social": (soc, 0.2), "fundamentals": (fun, 0.25), "business": (bus, 0.25)}
    present = [(v, w) for v, w in weights.values() if v is not None]
    overall = sum(v * w for v, w in present) / sum(w for _, w in present) if present else None
    flags = f1 + f2 + f3 + f4 + f5
    if p.available and p.pumps_12m >= 2:
        flags.append(f"repeat pumper: {p.pumps_12m} runs of 50%+ in 12 months, best {p.max_gain_pct:.0f}%")
    return Scores(liquidity=liq, social=soc, fundamentals=fun, business=bus, activity=act,
                  pump_propensity=p.propensity if p.available else None, overall=overall, flags=flags)
