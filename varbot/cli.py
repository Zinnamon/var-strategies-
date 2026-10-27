"""Command line entry point: `varbot --help`."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from varbot.analysis import impact as im
from varbot.analysis.classify import classify
from varbot.config import SETTINGS
from varbot.pipeline import build_reports, select_symbols
from varbot.report import full_markdown, to_csv, to_json, token_markdown, usd
from varbot.sources import variational


def _sizes(text: str | None) -> tuple[float, ...]:
    if not text:
        return im.DEFAULT_SIZES_USD
    return tuple(float(x.replace("k", "e3").replace("m", "e6").replace("M", "e6").replace("K", "e3")) for x in text.split(","))


def _moves(text: str | None) -> tuple[float, ...]:
    return im.DEFAULT_MOVES_PCT if not text else tuple(float(x) for x in text.split(","))


def _venues(text: str | None) -> list[str] | None:
    from varbot.sources.venues import venue_by_name

    if text is None:
        return None  # pipeline default: every supported venue
    if text.strip().lower() == "none":
        return []
    out = []
    for name in text.split(","):
        v = venue_by_name(name)
        if not v:
            raise click.BadParameter(f"unknown venue {name!r}")
        out.append(v.id)
    return out


def _write(out_dir: Path | None, stem: str, md: str, reports) -> None:
    if not out_dir:
        click.echo(md)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.md").write_text(md)
    (out_dir / f"{stem}.json").write_text(to_json(reports))
    (out_dir / f"{stem}.csv").write_text(to_csv(reports))
    click.echo(f"wrote {out_dir / stem}.md, .json, .csv", err=True)


@click.group()
@click.option("--offline", is_flag=True, help="Never touch the network; use recorded fixtures and cache.")
@click.option("-v", "--verbose", is_flag=True)
def main(offline: bool, verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if offline:
        SETTINGS.offline = True


@main.command()
@click.option("--all", "show_all", is_flag=True, help="Include equities, ETFs, commodities and swaps.")
@click.option("--sort", type=click.Choice(["volume", "oi", "spread", "ticker"]), default="volume")
def markets(show_all: bool, sort: str) -> None:
    """List the markets Variational currently quotes."""
    platform, listings = variational.fetch_stats()
    rows = []
    for l in listings:
        c = classify(l)
        if not show_all and c.asset_class != "crypto":
            continue
        rows.append((l, c))
    key = {
        "volume": lambda lc: -lc[0].volume_24h,
        "oi": lambda lc: -lc[0].open_interest_usd,
        "spread": lambda lc: -(lc[0].base_spread_bps or 0),
        "ticker": lambda lc: lc[0].ticker,
    }[sort]
    rows.sort(key=key)
    click.echo(
        f"Variational: {platform.get('num_markets')} markets, 24h volume {usd(platform.get('total_volume_24h'))}, "
        f"OI {usd(platform.get('open_interest'))}, TVL {usd(platform.get('tvl'))}\n"
    )
    click.echo(f"{'ticker':<18}{'class':<10}{'name':<34}{'mark':>14}{'24h vol':>12}{'OI':>12}{'spread':>9}{'$100k buy':>11}")
    for l, c in rows:
        q100k = next((q for q in l.quotes if q.tier == "size_100k"), None)
        prem = f"{(q100k.ask - l.mark_price) / l.mark_price * 100:+.2f}%" if (q100k and l.mark_price) else "n/a"
        click.echo(
            f"{l.ticker:<18}{c.asset_class:<10}{(l.name or '')[:33]:<34}{l.mark_price:>14,.6g}{usd(l.volume_24h):>12}"
            f"{usd(l.open_interest_usd):>12}{(l.base_spread_bps or 0):>8.1f}b{prem:>11}"
        )


@main.command()
@click.option("--max-mcap", default="100m", show_default=True, help="Small-cap threshold in USD, e.g. 100m or 50000000.")
@click.option("--cap-basis", type=click.Choice(["either", "fdv", "mcap"]), default="either", show_default=True,
              help="Compare the threshold against FDV, market cap, or market cap with FDV as fallback.")
@click.option("--all-caps", is_flag=True, help="Analyse every crypto listing, not just small caps.")
@click.option("--no-pumps", is_flag=True, help="Skip the 12-month price-history / pump analysis.")
@click.option("--pump-gain", default=50.0, show_default=True, help="A run counts as a pump when it gains this % from the low of the window.")
@click.option("--pump-window", default=21, show_default=True, help="Days in which the gain must happen.")
@click.option("--sizes", default=None, help="Buy sizes in USD, comma separated, e.g. 10k,50k,100k,1m")
@click.option("--moves", default=None, help="Price moves in %, comma separated, e.g. 1,2,5,10")
@click.option("--no-social", is_flag=True, help="Skip X and Telegram.")
@click.option("--research", is_flag=True, help="Run Claude web research on business activity (costs API usage).")
@click.option("--limit", type=int, default=None, help="Only the N smallest caps.")
@click.option("--symbols", default=None, help="Comma separated tickers to restrict to, e.g. STO,ERA,BONK")
@click.option("--venues", default=None, help="Comma separated venues for order-book impact (default: all supported). 'none' for Binance only.")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Directory for .md/.json/.csv (default: print markdown).")
@click.option("--workers", type=int, default=4)
def scan(max_mcap, cap_basis, all_caps, no_pumps, pump_gain, pump_window, sizes, moves, no_social, research, limit, symbols, venues, out, workers) -> None:
    """Full pass: fundamentals, exchange listings, order-book impact on every venue, social, price history, optional research."""
    platform, listings = variational.fetch_stats()
    listings = select_symbols(listings, symbols.split(",") if symbols else None)
    max_mcap = _sizes(str(max_mcap))[0]
    reports = build_reports(
        listings,
        small_cap_max_usd=max_mcap,
        only_small_caps=not all_caps,
        cap_basis=cap_basis,
        with_pumps=not no_pumps,
        pump_min_gain_pct=pump_gain,
        pump_window_days=pump_window,
        sizes_usd=_sizes(sizes),
        moves_pct=_moves(moves),
        with_social=not no_social,
        with_research=research,
        venue_ids=_venues(venues),
        limit=limit,
        progress=lambda m: click.echo(m, err=True),
        workers=workers,
    )
    reports.sort(key=lambda r: -(r.scores.overall or -1))
    basis = {"fdv": "FDV", "mcap": "market cap", "either": "cap"}[cap_basis]
    md = full_markdown(reports, platform, title=f"Variational token scan ({basis} <= {usd(max_mcap)})" if not all_caps else "Variational token scan")
    _write(out, "scan", md, reports)


@main.command()
@click.argument("symbol")
@click.option("--sizes", default=None)
@click.option("--moves", default=None)
@click.option("--no-social", is_flag=True)
@click.option("--research", is_flag=True)
@click.option("--venues", default=None)
@click.option("--out", type=click.Path(path_type=Path), default=None)
def token(symbol, sizes, moves, no_social, research, venues, out) -> None:
    """Deep report on one listed token."""
    _, listings = variational.fetch_stats()
    chosen = select_symbols(listings, [symbol])
    if not chosen:
        click.echo(f"{symbol} is not listed on Variational", err=True)
        sys.exit(1)
    reports = build_reports(
        chosen, only_small_caps=False, sizes_usd=_sizes(sizes), moves_pct=_moves(moves),
        with_social=not no_social, with_research=research, venue_ids=_venues(venues),
        progress=lambda m: click.echo(m, err=True), workers=1,
    )
    md = "\n\n".join(token_markdown(r) for r in reports)
    _write(out, f"token-{symbol.upper()}", md, reports)


@main.command()
@click.argument("symbol")
@click.option("--sizes", default=None)
@click.option("--moves", default=None)
@click.option("--venue", default="binance", help="binance, bybit, okx, coinbase, kraken, kucoin, gate, mexc, bitget, htx, cryptocom")
@click.option("--quote", default="USDT")
def impact(symbol, sizes, moves, venue, quote) -> None:
    """Spot order-book impact on one venue (no Variational listing needed)."""
    from varbot.sources.binance import binance_impact
    from varbot.sources.venues import venue_by_name, venue_impact

    if venue.lower() == "binance":
        b = binance_impact(symbol.upper(), sizes_usd=_sizes(sizes), moves_pct=_moves(moves))
    else:
        v = venue_by_name(venue)
        if not v:
            raise click.BadParameter(f"unknown venue {venue!r}")
        b = venue_impact(v, symbol.upper(), quote.upper(), sizes_usd=_sizes(sizes), moves_pct=_moves(moves))
    if not b.listed or not b.fills:
        click.echo(b.note)
        sys.exit(1)
    click.echo(f"{b.venue_name} {b.symbol}: bid {b.best_bid} ask {b.best_ask} spread {b.spread_bps:.1f} bps, 24h vol {usd(b.volume_24h_usd)}, asks within 2% {usd(b.ask_depth_2pct_usd)}, {b.book_depth_levels} levels (cap {b.levels_cap})")
    for f in b.fills:
        click.echo(
            f"  {f.side:<4} {usd(f.notional_usd):>9}: avg {f.avg_price}, final {f.final_price}, "
            f"impact {f.impact_pct_vs_best:+.3f}% vs best, {f.impact_pct_vs_mid:+.3f}% vs mid{' (book exhausted)' if f.exhausted else ''}"
        )
    for m in b.move_costs:
        click.echo(f"  move {m.side} {m.move_pct:>4.0f}%: {usd(m.notional_usd)}{' (beyond visible book)' if m.beyond_visible_book else ''}")


@main.command()
@click.argument("symbols")
@click.option("--venues", default="binance,bybit,okx", show_default=True, help="Live-capable venues: binance, bybit, okx")
@click.option("--sizes", default="10k,100k,1m", show_default=True)
@click.option("--moves", default=None)
@click.option("--interval", default=2.0, show_default=True, help="Seconds between recomputes.")
@click.option("--json-out", type=click.Path(path_type=Path), default=None, help="Rewrite this file with the latest payload every tick.")
@click.option("--serve", "port", type=int, default=None, help="Serve the latest payload as JSON on this port (GET /).")
@click.option("--alert-pct", type=float, default=None, help="Print an alert when the --alert-size buy impact reaches this %.")
@click.option("--alert-size", default="100k", show_default=True)
@click.option("--duration", type=float, default=None, help="Stop after N seconds (default: run until Ctrl-C).")
@click.option("--quiet", is_flag=True, help="Do not print the table each tick (alerts still print).")
@click.option("--alert-telegram", is_flag=True, help="Also send alerts to the configured Telegram chat.")
def watch(symbols, venues, sizes, moves, interval, json_out, port, alert_pct, alert_size, duration, quiet, alert_telegram) -> None:
    """Real-time order books over WebSockets: live impact table, JSON file and HTTP endpoint.

    SYMBOLS is comma separated. `smallest:50` expands to the 50 smallest caps at start.

    Example: varbot watch STO,ERA --serve 8080 --json-out live.json --alert-pct 3
    """
    import asyncio

    from varbot.live.runner import watch as _watch

    if alert_telegram and not (SETTINGS.telegram_bot_token and SETTINGS.telegram_chat_id):
        raise click.UsageError("--alert-telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    def tick(text: str) -> None:
        is_alert = text.startswith(("ALERT", "clear"))
        if is_alert and alert_telegram:
            from varbot.notify import send_telegram

            try:
                send_telegram(text)
            except Exception as exc:  # noqa: BLE001 - never let delivery kill the watch loop
                click.echo(f"telegram delivery failed: {exc}", err=True)
        if quiet and not is_alert:
            return
        click.echo(text + ("" if is_alert else "\n"))

    try:
        asyncio.run(_watch(
            symbols.split(","), venue_ids=_venues(venues), sizes_usd=_sizes(sizes), moves_pct=_moves(moves), interval=interval,
            json_out=json_out, port=port, alert_pct=alert_pct, alert_size=_sizes(alert_size)[0], duration=duration, on_tick=tick,
        ))
    except KeyboardInterrupt:
        click.echo("stopped", err=True)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


@main.command()
@click.argument("n", type=int, default=50)
def smallest(n: int) -> None:
    """Print the N smallest-cap crypto listings as a comma-separated list (for WATCH_SYMBOLS)."""
    from varbot.live.runner import smallest_symbols

    click.echo(",".join(smallest_symbols(n)))


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--chat-id", default=None)
@click.option("--max-messages", type=int, default=None, help="Send at most N Telegram messages (4000 chars each); the rest is dropped.")
def notify(path: Path, chat_id, max_messages) -> None:
    """Send a report file to the configured Telegram chat."""
    from varbot.notify import send_telegram

    n = send_telegram(path.read_text(), chat_id=chat_id, max_messages=max_messages)
    click.echo(f"sent {n} message(s)")


if __name__ == "__main__":
    main()
