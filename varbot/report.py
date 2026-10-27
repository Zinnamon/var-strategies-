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


def _exchanges_section(r: TokenReport) -> list[str]:
    e = r.exchanges
    if not e.available:
        return [f"- Exchanges: {e.note or 'not checked'}"]
    lines = [
        f"- Listed on {len(e.exchange_ids)} exchanges ({e.cex_count} CEX, {e.dex_count} DEX), spot volume {usd(e.total_spot_volume_usd)}/24h"
        + (f", top venue holds {e.top_exchange_share_pct:.0f}%" if e.top_exchange_share_pct is not None else ""),
        f"- Major CEX: {', '.join(e.major_cex) if e.major_cex else 'none'}",
    ]
    rest = [x for x in e.exchanges if x not in e.major_cex][:12]
    if rest:
        lines.append(f"- Other venues: {', '.join(rest)}" + (" …" if len(e.exchanges) - len(e.major_cex) > 12 else ""))
    bad = [t for t in e.tickers if t.trust_score == "red" or t.is_anomaly]
    if bad:
        lines.append(f"- {len(bad)} pair(s) flagged red / anomalous by CoinGecko")
    return lines


def _venues_table(r: TokenReport) -> list[str]:
    rows = [v for v in r.venues if v.listed]
    if not rows:
        return []
    lines = ["", "| Venue | Pair | Spread | Asks within 2% | $10k buy | $100k buy | $1m buy | $ to move 5% | Levels |", "|---|---|---|---|---|---|---|---|---|"]
    for v in ([r.binance] if r.binance.listed else []) + rows:
        def f(n):
            x = next((x for x in v.fills if x.side == "buy" and x.notional_usd == n), None)
            return ("n/a" if not x else pct(x.impact_pct_vs_best) + (" (exh.)" if x.exhausted else ""))
        mv = next((m for m in v.move_costs if m.side == "buy" and m.move_pct == 5.0), None)
        lines.append(
            f"| {v.venue_name} | {v.symbol or ''} | {f'{v.spread_bps:.1f} bps' if v.spread_bps is not None else v.note or 'n/a'} | "
            f"{usd(v.ask_depth_2pct_usd)} | {f(10_000)} | {f(100_000)} | {f(1_000_000)} | "
            f"{usd(mv.notional_usd) + ('+' if mv.beyond_visible_book else '') if mv else 'n/a'} | {v.book_depth_levels}/{v.levels_cap or '?'} |"
        )
    unavailable = [v for v in r.venues if v.listed and not v.fills]
    if unavailable:
        lines.append("")
        lines.append("Books not retrieved: " + ", ".join(f"{v.venue_name} ({v.note})" for v in unavailable))
    return lines


def _variational_table(r: TokenReport) -> list[str]:
    if not r.variational_impact:
        return []
    lines = ["", "| Variational OLP quote tier | Buy premium vs mark | Sell discount vs mark | Spread |", "|---|---|---|---|"]
    for v in r.variational_impact:
        size = usd(v.notional_usd) if v.notional_usd else "min size"
        lines.append(f"| {size} | {pct(v.buy_premium_pct_vs_mark, 3)} | {pct(v.sell_discount_pct_vs_mark, 3)} | {v.spread_bps:.1f} bps |")
    return lines


def _pumps_section(r: TokenReport) -> list[str]:
    p = r.pumps
    if not p.available:
        return [f"- {p.note or 'not run'}"]
    lines = [
        f"- 12 months: {p.pumps_12m} run(s) of 50%+ ({p.pumps_ytd} this year), best {pct(p.max_gain_pct, 0)}, "
        f"median {pct(p.median_gain_pct, 0)} over ~{p.avg_days_to_peak:.0f} days, median give-back {pct(-(p.median_retrace_pct or 0), 0)}"
        if p.pumps_12m else "- 12 months: no run of 50%+ within 3 weeks",
        f"- Now {pct(p.current_vs_high_12m_pct, 0)} from 12m high, {pct(p.current_vs_low_12m_pct, 0)} above 12m low, "
        f"{pct(p.current_vs_low_90d_pct, 0)} above 90d low; YTD {pct(p.change_ytd_pct, 0)}, 90d {pct(p.change_90d_pct, 0)}",
        f"- Volatility {p.annualised_vol_pct:.0f}% annualised, {p.days_over_20pct} days with a 20%+ move; "
        f"30d volume {p.volume_30d_vs_180d:.1f}x its 6-month median" if p.annualised_vol_pct is not None and p.volume_30d_vs_180d is not None
        else "- Volatility n/a",
        f"- Pump propensity {score(p.propensity)}/10"
        + (f" · last peak {p.last_peak_date} ({p.days_since_last_peak}d ago), now {pct(p.current_vs_last_peak_pct, 0)} vs that peak" if p.last_peak_date else ""),
    ]
    for e in sorted(p.episodes, key=lambda e: e.peak_date, reverse=True)[:6]:
        lines.append(f"  - {e.trough_date} → {e.peak_date}: {pct(e.gain_pct, 0)} in {e.days_to_peak}d, then gave back {e.retrace_pct:.0f}% by {e.end_date}")
    return lines


def token_markdown(r: TokenReport) -> str:
    l, f, x, tg, rs, s = r.listing, r.fundamentals, r.x, r.telegram, r.research, r.scores
    name = f.name or l.name or l.symbol
    out = [f"## {name} ({l.ticker})", ""]
    out.append(
        f"**Overall {score(s.overall)}/10** · activity {score(s.activity)} · liquidity {score(s.liquidity)} · social {score(s.social)} · "
        f"fundamentals {score(s.fundamentals)} · business {score(s.business)} · pump propensity {score(s.pump_propensity)}"
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
    out += ["", "### Exchanges"]
    out += _exchanges_section(r)
    out += ["", "### Price impact of a spot buy"]
    out += _impact_table(r)
    out += _venues_table(r)
    out += _variational_table(r)
    out += ["", "### Price history (12 months)"]
    out += _pumps_section(r)
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
        "| # | Token | Mcap | FDV | Var. 24h vol | Exchanges | Binance | $100k buy impact | $ to move 5% | X followers | TG members | Pumps 12m (best) | Activity | Overall |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(reports, 1):
        f, b, e = r.fundamentals, r.binance, r.exchanges
        buy = next((x for x in b.fills if x.side == "buy" and x.notional_usd == 100_000), None)
        mv = next((x for x in b.move_costs if x.side == "buy" and x.move_pct == 5.0), None)
        ex = (", ".join(e.major_cex[:4]) + (f" +{len(e.exchange_ids) - min(len(e.major_cex), 4)}" if len(e.exchange_ids) > min(len(e.major_cex), 4) else "")) if e.available else "n/a"
        rows.append(
            f"| {i} | {r.listing.symbol} | {usd(f.market_cap_usd)} | {usd(f.fdv_usd)} | {usd(r.listing.volume_24h)} | {ex or 'none'} | "
            f"{b.binance_symbol or 'no'} | {pct(buy.impact_pct_vs_best) if buy else 'n/a'}{' (exh.)' if buy and buy.exhausted else ''} | "
            f"{usd(mv.notional_usd) if mv else 'n/a'}{'+' if mv and mv.beyond_visible_book else ''} | "
            f"{num(r.x.followers) if r.x.available else num(f.twitter_followers)} | "
            f"{num(r.telegram.members) if r.telegram.available else num(f.telegram_users)} | "
            f"{(str(r.pumps.pumps_12m) + (' (' + pct(r.pumps.max_gain_pct, 0) + ')' if r.pumps.max_gain_pct else '')) if r.pumps.available else 'n/a'} | "
            f"{score(r.scores.activity)} | {score(r.scores.overall)} |"
        )
    return "\n".join(rows)


def highlights(reports: list[TokenReport]) -> str:
    """Two ranked shortlists: the small caps that look alive, and the repeat pumpers."""
    out: list[str] = []
    alive = sorted([r for r in reports if r.scores.activity is not None], key=lambda r: -(r.scores.activity or 0))[:15]
    if alive:
        out += ["### Most active (signs of life: posts, commits, listings, volume, research)", "",
                "| Token | Activity | FDV | Posts 30d (X / TG) | Commits 4w | CEXs | Spot vol 24h | Business | Notes |", "|---|---|---|---|---|---|---|---|---|"]
        for r in alive:
            f, e = r.fundamentals, r.exchanges
            xp = str(r.x.posts_30d) if r.x.available else "-"
            tp = str(r.telegram.posts_30d) if r.telegram.available else "-"
            note = (r.research.summary or "")[:90] if r.research.available else "; ".join(r.scores.flags[:2])[:90]
            out.append(f"| {r.listing.symbol} | {score(r.scores.activity)} | {usd(f.fdv_usd)} | {xp} / {tp} | {f.commits_4w if f.commits_4w is not None else '-'} | "
                       f"{e.cex_count if e.available else '-'} | {usd(e.total_spot_volume_usd) if e.available else 'n/a'} | {score(r.scores.business)} | {note} |")
        out.append("")
    pumpers = sorted([r for r in reports if r.pumps.available and r.pumps.pumps_12m >= 2],
                     key=lambda r: (-(r.pumps.propensity or 0), -r.pumps.pumps_12m))[:15]
    if pumpers:
        out += ["### Repeat pumpers (2+ runs of 50%+ within 3 weeks in the last 12 months)", "",
                "| Token | Runs 12m / YTD | Best | Median | Last peak | Now vs last peak | Now vs 12m low | Vol 30d/180d | Propensity | Activity |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in pumpers:
            p = r.pumps
            out.append(f"| {r.listing.symbol} | {p.pumps_12m} / {p.pumps_ytd} | {pct(p.max_gain_pct, 0)} | {pct(p.median_gain_pct, 0)} | {p.last_peak_date} ({p.days_since_last_peak}d) | "
                       f"{pct(p.current_vs_last_peak_pct, 0)} | {pct(p.current_vs_low_12m_pct, 0)} | {p.volume_30d_vs_180d:.1f}x | {score(p.propensity)} | {score(r.scores.activity)} |"
                       if p.volume_30d_vs_180d is not None else
                       f"| {r.listing.symbol} | {p.pumps_12m} / {p.pumps_ytd} | {pct(p.max_gain_pct, 0)} | {pct(p.median_gain_pct, 0)} | {p.last_peak_date} ({p.days_since_last_peak}d) | "
                       f"{pct(p.current_vs_last_peak_pct, 0)} | {pct(p.current_vs_low_12m_pct, 0)} | n/a | {score(p.propensity)} | {score(r.scores.activity)} |")
        out.append("")
        out.append("Propensity is a transparent 0-10 from run count, recency, distance below the last peak and volume pick-up. It describes the past pattern, not a forecast.")
        out.append("")
    return "\n".join(out)


def full_markdown(reports: list[TokenReport], platform: dict | None = None, title: str = "Variational token scan") -> str:
    out = [f"# {title}", ""]
    if reports:
        out.append(f"Generated {reports[0].generated_at}. {len(reports)} tokens.")
    if platform:
        out.append(
            f"Variational platform: {platform.get('num_markets')} markets, 24h volume {usd(platform.get('total_volume_24h'))}, "
            f"open interest {usd(platform.get('open_interest'))}, TVL {usd(platform.get('tvl'))}."
        )
    out += ["", "Impact figures sweep the public spot order book of each venue at the time of the scan (Binance up to 5000 levels; "
            "other venues cap lower, see the Levels column). Real fills attract new quotes and cancellations, so treat them as the "
            "order of magnitude, not a quote. For continuously updated figures run `varbot watch`.",
            "", highlights(reports), summary_table(reports), ""]
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
        "exchange_count", "major_cex", "all_exchanges", "spot_volume_24h_usd", "top_exchange_share_pct",
        "binance_symbol", "binance_volume_24h_usd", "spread_bps", "ask_depth_2pct_usd",
        "buy_10k_impact_pct", "buy_100k_impact_pct", "buy_1m_impact_pct", "usd_to_move_1pct", "usd_to_move_5pct", "usd_to_move_10pct",
        "other_venues_100k_impact_pct",
        "x_followers", "x_posts_30d", "x_engagement_pct", "tg_members", "tg_posts_30d",
        "pumps_12m", "pumps_ytd", "pump_max_gain_pct", "pump_median_gain_pct", "last_peak_date", "now_vs_last_peak_pct",
        "now_vs_12m_high_pct", "now_vs_12m_low_pct", "change_ytd_pct", "annualised_vol_pct", "volume_30d_vs_180d", "pump_propensity",
        "business_activity_score", "activity_score", "liquidity_score", "social_score", "fundamentals_score", "overall_score", "flags",
    ])

    def fill(r: TokenReport, n: float):
        f = next((x for x in r.binance.fills if x.side == "buy" and x.notional_usd == n), None)
        return f.impact_pct_vs_best if f else None

    def move(r: TokenReport, p: float):
        m = next((x for x in r.binance.move_costs if x.side == "buy" and x.move_pct == p), None)
        return m.notional_usd if m else None

    for r in reports:
        f, b, e, p = r.fundamentals, r.binance, r.exchanges, r.pumps
        others = "; ".join(
            f"{v.venue_name}={x.impact_pct_vs_best:.2f}%"
            for v in r.venues for x in v.fills if x.side == "buy" and x.notional_usd == 100_000 and x.impact_pct_vs_best is not None
        )
        w.writerow([
            r.listing.ticker, f.name or r.listing.name, f.coingecko_id, f.market_cap_usd, f.fdv_usd, r.listing.volume_24h,
            r.listing.open_interest_usd,
            len(e.exchange_ids) if e.available else None, "; ".join(e.major_cex), "; ".join(e.exchanges), e.total_spot_volume_usd, e.top_exchange_share_pct,
            b.binance_symbol, b.volume_24h_usd, b.spread_bps, b.ask_depth_2pct_usd,
            fill(r, 10_000), fill(r, 100_000), fill(r, 1_000_000), move(r, 1.0), move(r, 5.0), move(r, 10.0), others,
            r.x.followers if r.x.available else f.twitter_followers, r.x.posts_30d if r.x.available else None, r.x.engagement_rate_pct,
            r.telegram.members if r.telegram.available else f.telegram_users, r.telegram.posts_30d if r.telegram.available else None,
            p.pumps_12m if p.available else None, p.pumps_ytd if p.available else None, p.max_gain_pct, p.median_gain_pct, p.last_peak_date, p.current_vs_last_peak_pct,
            p.current_vs_high_12m_pct, p.current_vs_low_12m_pct, p.change_ytd_pct, p.annualised_vol_pct, p.volume_30d_vs_180d, p.propensity,
            r.research.activity_score, r.scores.activity, r.scores.liquidity, r.scores.social, r.scores.fundamentals, r.scores.overall,
            "; ".join(r.scores.flags),
        ])
    return buf.getvalue()
