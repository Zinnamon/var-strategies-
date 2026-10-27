"""Render TokenReports as Markdown, JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable

from varbot.models import TokenReport


def usd(x: float | None, digits: int = 0) -> str:
    if x is None:
        return "n/a"
    if abs(x) >= 1e9:
        return f"${x/1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"${x/1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"${x/1e3:.1f}k"
    return f"${x:,.{digits if digits else 2}f}"


def pct(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def num(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"


def score(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def _impact_table(r: TokenReport) -> list[str]:
    b = r.binance
    lines = []
    if not b.listed:
        lines.append(f"- Binance spot: **not listed** ({b.note})")
        return lines
    lines.append(
        f"- Binance `{b.binance_symbol}`: best bid {num(b.best_bid)} / ask {num(b.best_ask)}, "
        f"spread {b.spread_bps:.1f} bps, 24h volume {usd(b.volume_24h_usd)}, "
        f"asks within 2%: {usd(b.ask_depth_2pct_usd)}, total visible asks: {usd(b.ask_depth_usd)}"
        if b.spread_bps is not None
        else f"- Binance `{b.binance_symbol}`: {b.note or 'book unavailable'}"
    )
    buys = [f for f in b.fills if f.side == "buy"]
    if buys:
        lines.append("")
        lines.append("| Spot buy | Avg fill vs best ask | Price after (vs best ask) | Price after (vs mid) | Levels | Book exhausted |")
        lines.append("|---|---|---|---|---|---|")
        for f in buys:
            lines.append(
                f"| {usd(f.notional_usd)} | {pct(f.avg_slippage_pct)} | {pct(f.impact_pct_vs_best)} | "
                f"{pct(f.impact_pct_vs_mid)} | {f.levels_consumed} | {'yes' if f.exhausted else 'no'} |"
            )
    moves = [m for m in b.move_costs if m.side == "buy"]
    if moves:
        lines.append("")
        lines.append("| Push price up by | USD of buying needed |")
        lines.append("|---|---|")
        for m in moves:
            tail = " (beyond visible book)" if m.beyond_visible_book else ""
            lines.append(f"| {m.move_pct:.0f}% | {usd(m.notional_usd)}{tail} |")
    return lines


def _variational_table(r: TokenReport) -> list[str]:
    if not r.variational_impact:
        return []
    lines = ["", "| Variational OLP quote tier | Buy premium vs mark | Sell discount vs mark | Spread |", "|---|---|---|---|"]
    for v in r.variational_impact:
        size = usd(v.notional_usd) if v.notional_usd else "min size"
        lines.append(f"| {size} | {pct(v.buy_premium_pct_vs_mark, 3)} | {pct(v.sell_discount_pct_vs_mark, 3)} | {v.spread_bps:.1f} bps |")
    return lines


def token_markdown(r: TokenReport) -> str:
    l, f, x, tg, rs, s = r.listing, r.fundamentals, r.x, r.telegram, r.research, r.scores
    name = f.name or l.name or l.symbol
    out = [f"## {name} ({l.ticker})", ""]
    out.append(
        f"**Overall {score(s.overall)}/10** · liquidity {score(s.liquidity)} · social {score(s.social)} · "
        f"fundamentals {score(s.fundamentals)} · business {score(s.business)}"
    )
    if s.flags:
        out.append("")
        out.append("Flags: " + "; ".join(s.flags))
    out += ["", "### Market"]
    out.append(
        f"- Variational: mark {num(l.mark_price)}, 24h volume {usd(l.volume_24h)}, open interest {usd(l.open_interest_usd)} "
        f"(long {usd(l.long_oi_usd)} / short {usd(l.short_oi_usd)}), base spread {l.base_spread_bps or 0:.1f} bps, "
        f"funding {pct((l.funding_annualized_rate or 0) * 100, 1)} annualised"
    )
    out.append(
        f"- CoinGecko `{f.coingecko_id or 'unresolved'}` ({f.resolution_note}): price {usd(f.price_usd, 4)}, "
        f"market cap {usd(f.market_cap_usd)} (rank {f.market_cap_rank or 'n/a'}), FDV {usd(f.fdv_usd)}, "
        f"24h volume {usd(f.volume_24h_usd)}, vol/mcap {pct((f.volume_to_mcap or 0) * 100, 1) if f.volume_to_mcap else 'n/a'}"
    )
    out.append(
        f"- Supply: circulating {num(f.circulating_supply)} / total {num(f.total_supply)} "
        f"({pct((f.float_ratio or 0) * 100, 0) if f.float_ratio else 'n/a'} float), "
        f"24h {pct(f.change_24h_pct)}, 7d {pct(f.change_7d_pct)}, 30d {pct(f.change_30d_pct)}, ATH {pct(f.ath_change_pct, 0)}"
    )
    if f.categories:
        out.append(f"- Categories: {', '.join(f.categories[:6])}" + (f" · chains: {', '.join(f.chains[:4])}" if f.chains else ""))
    out += ["", "### Price impact of a spot buy"]
    out += _impact_table(r)
    out += _variational_table(r)
    out += ["", "### Social"]
    if x.available:
        out.append(
            f"- X @{x.handle}: {num(x.followers)} followers, {x.posts_30d} posts in 30d ({x.posts_7d} in 7d), "
            f"avg {num(x.avg_likes_30d)} likes / {num(x.avg_reposts_30d)} reposts / {num(x.avg_replies_30d)} replies, "
            f"engagement {pct(x.engagement_rate_pct, 3)} per post, last post {x.days_since_last_post:.1f}d ago"
            if x.days_since_last_post is not None
            else f"- X @{x.handle}: {num(x.followers)} followers, no recent posts found"
        )
    else:
        out.append(f"- X: {x.note or 'not run'}" + (f" (CoinGecko snapshot: {num(f.twitter_followers)} followers)" if f.twitter_followers else ""))
    if tg.available:
        out.append(
            f"- Telegram @{tg.channel} ({tg.kind}): {num(tg.members)} members, {tg.posts_30d} posts in 30d, "
            f"avg {num(tg.avg_views_30d)} views"
            + (f", last post {tg.days_since_last_post:.1f}d ago" if tg.days_since_last_post is not None else "")
            + (f" · {tg.note}" if tg.note else "")
        )
    else:
        out.append(f"- Telegram: {tg.note or 'not run'}" + (f" (CoinGecko snapshot: {num(f.telegram_users)} users)" if f.telegram_users else ""))
    if f.github_repos:
        out.append(f"- GitHub: {f.github_repos[0]} · stars {num(f.github_stars)} · commits last 4w: {f.commits_4w if f.commits_4w is not None else 'n/a'}")
    out += ["", "### Business activity"]
    if rs.available:
        out.append(f"- {rs.summary}")
        if rs.what_it_does:
            out.append(f"- What it does: {rs.what_it_does}")
        if rs.token_utility:
            out.append(f"- Token utility: {rs.token_utility}")
        if rs.team_and_backers:
            out.append(f"- Team and backers: {rs.team_and_backers}")
        if rs.recent_developments:
            out.append("- Recent developments:")
            for d in rs.recent_developments[:10]:
                src = f" ([source]({d.source_url}))" if d.source_url else ""
                out.append(f"  - {d.date or 'undated'} · {d.kind}: {d.headline}{src}")
        if rs.partnerships:
            out.append("- Partnerships: " + "; ".join(rs.partnerships[:8]))
        if rs.funding:
            out.append("- Funding: " + "; ".join(rs.funding[:5]))
        if rs.positives:
            out.append("- Positives: " + "; ".join(rs.positives[:6]))
        if rs.red_flags:
            out.append("- Red flags: " + "; ".join(rs.red_flags[:6]))
        out.append(f"- Activity score {rs.activity_score}/10, confidence {rs.confidence}, {len(rs.sources)} sources ({rs.model})")
    else:
        out.append(f"- {rs.note or 'not run (use --research)'}")
        if f.description:
            out.append(f"- CoinGecko description: {f.description[:500]}")
        if f.homepage:
            out.append(f"- Homepage: {f.homepage}")
    return "\n".join(out)


def summary_table(reports: Iterable[TokenReport]) -> str:
    rows = [
        "| # | Token | Mcap | FDV | Var. 24h vol | Binance | $100k buy impact | $ to move 5% | X followers | TG members | Overall |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(reports, 1):
        f, b = r.fundamentals, r.binance
        buy = next((x for x in b.fills if x.side == "buy" and x.notional_usd == 100_000), None)
        mv = next((x for x in b.move_costs if x.side == "buy" and x.move_pct == 5.0), None)
        rows.append(
            f"| {i} | {r.listing.symbol} | {usd(f.market_cap_usd)} | {usd(f.fdv_usd)} | {usd(r.listing.volume_24h)} | "
            f"{b.binance_symbol or 'no'} | {pct(buy.impact_pct_vs_best) if buy else 'n/a'}{' (exh.)' if buy and buy.exhausted else ''} | "
            f"{usd(mv.notional_usd) if mv else 'n/a'}{'+' if mv and mv.beyond_visible_book else ''} | "
            f"{num(r.x.followers) if r.x.available else num(f.twitter_followers)} | "
            f"{num(r.telegram.members) if r.telegram.available else num(f.telegram_users)} | {score(r.scores.overall)} |"
        )
    return "\n".join(rows)


def full_markdown(reports: list[TokenReport], platform: dict | None = None, title: str = "Variational token scan") -> str:
    out = [f"# {title}", ""]
    if reports:
        out.append(f"Generated {reports[0].generated_at}. {len(reports)} tokens.")
    if platform:
        out.append(
            f"Variational platform: {platform.get('num_markets')} markets, 24h volume {usd(platform.get('total_volume_24h'))}, "
            f"open interest {usd(platform.get('open_interest'))}, TVL {usd(platform.get('tvl'))}."
        )
    out += ["", "Impact figures sweep the visible Binance spot order book (up to 5000 levels per side) at the time of the scan. "
            "They are a snapshot: real fills attract new quotes and cancellations, so treat them as the order of magnitude, not a quote.",
            "", summary_table(reports), ""]
    for r in reports:
        out.append(token_markdown(r))
        out.append("")
    return "\n".join(out)


def to_json(reports: list[TokenReport]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in reports], indent=1)


def to_csv(reports: list[TokenReport]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "ticker", "name", "coingecko_id", "market_cap_usd", "fdv_usd", "variational_volume_24h", "variational_oi_usd",
        "binance_symbol", "binance_volume_24h_usd", "spread_bps", "ask_depth_2pct_usd",
        "buy_10k_impact_pct", "buy_100k_impact_pct", "buy_1m_impact_pct", "usd_to_move_1pct", "usd_to_move_5pct", "usd_to_move_10pct",
        "x_followers", "x_posts_30d", "x_engagement_pct", "tg_members", "tg_posts_30d",
        "business_activity_score", "liquidity_score", "social_score", "fundamentals_score", "overall_score", "flags",
    ])

    def fill(r: TokenReport, n: float):
        f = next((x for x in r.binance.fills if x.side == "buy" and x.notional_usd == n), None)
        return f.impact_pct_vs_best if f else None

    def move(r: TokenReport, p: float):
        m = next((x for x in r.binance.move_costs if x.side == "buy" and x.move_pct == p), None)
        return m.notional_usd if m else None

    for r in reports:
        f, b = r.fundamentals, r.binance
        w.writerow([
            r.listing.ticker, f.name or r.listing.name, f.coingecko_id, f.market_cap_usd, f.fdv_usd, r.listing.volume_24h,
            r.listing.open_interest_usd, b.binance_symbol, b.volume_24h_usd, b.spread_bps, b.ask_depth_2pct_usd,
            fill(r, 10_000), fill(r, 100_000), fill(r, 1_000_000), move(r, 1.0), move(r, 5.0), move(r, 10.0),
            r.x.followers if r.x.available else f.twitter_followers, r.x.posts_30d if r.x.available else None, r.x.engagement_rate_pct,
            r.telegram.members if r.telegram.available else f.telegram_users, r.telegram.posts_30d if r.telegram.available else None,
            r.research.activity_score, r.scores.liquidity, r.scores.social, r.scores.fundamentals, r.scores.overall,
            "; ".join(r.scores.flags),
        ])
    return buf.getvalue()
