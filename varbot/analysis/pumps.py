"""Pump-episode detection on a daily price series.

An episode starts when the price is at least `min_gain_pct` above the lowest close of
the previous `window_days`, and ends when the price has retraced `end_retrace_pct` from
the running peak (or the series ends). Episodes are the units of "it pumped": how often,
how big, how fast, and how much of it was given back. All thresholds are arguments so the
screen can be tuned from the CLI.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone

from varbot.models import PumpEpisode, PumpStats

Point = tuple[datetime, float]


def parse_market_chart(chart: dict) -> tuple[list[Point], list[Point]]:
    """CoinGecko market_chart -> ([(ts, price)], [(ts, volume)]) in time order."""
    def conv(rows):
        out = []
        for ts_ms, value in rows or []:
            if value is None:
                continue
            out.append((datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc), float(value)))
        out.sort()
        return out

    return conv(chart.get("prices")), conv(chart.get("total_volumes"))


def detect_pumps(prices: list[Point], *, min_gain_pct: float = 50.0, window_days: int = 21, end_retrace_pct: float = 30.0) -> list[PumpEpisode]:
    n = len(prices)
    episodes: list[PumpEpisode] = []
    i = 0
    while i < n:
        ts, p = prices[i]
        lo_idx = i
        for k in range(i, -1, -1):
            if (ts - prices[k][0]).days > window_days:
                break
            if prices[k][1] < prices[lo_idx][1]:
                lo_idx = k
        trough = prices[lo_idx][1]
        if trough > 0 and p >= trough * (1 + min_gain_pct / 100):
            peak_idx = i
            j = i
            while j + 1 < n:
                j += 1
                if prices[j][1] > prices[peak_idx][1]:
                    peak_idx = j
                elif prices[j][1] <= prices[peak_idx][1] * (1 - end_retrace_pct / 100):
                    break
            peak_ts, peak_p = prices[peak_idx]
            end_ts, end_p = prices[j]
            episodes.append(
                PumpEpisode(
                    trough_date=prices[lo_idx][0].date().isoformat(),
                    trough_price=trough,
                    peak_date=peak_ts.date().isoformat(),
                    peak_price=peak_p,
                    gain_pct=(peak_p / trough - 1) * 100,
                    days_to_peak=(peak_ts - prices[lo_idx][0]).days,
                    retrace_pct=(1 - end_p / peak_p) * 100 if peak_p else 0.0,
                    end_date=end_ts.date().isoformat(),
                )
            )
            i = j + 1
            continue
        i += 1
    return episodes


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1) * 100


def analyse(chart: dict, *, now: datetime | None = None, min_gain_pct: float = 50.0, window_days: int = 21, end_retrace_pct: float = 30.0) -> PumpStats:
    prices, volumes = parse_market_chart(chart)
    stats = PumpStats(available=bool(prices))
    if len(prices) < 30:
        stats.available = False
        stats.note = "fewer than 30 daily prices"
        return stats
    now = now or prices[-1][0]
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    episodes = detect_pumps(prices, min_gain_pct=min_gain_pct, window_days=window_days, end_retrace_pct=end_retrace_pct)
    stats.episodes = episodes
    stats.pumps_12m = len(episodes)
    stats.pumps_ytd = sum(1 for e in episodes if datetime.fromisoformat(e.peak_date).replace(tzinfo=timezone.utc) >= year_start)
    gains = [e.gain_pct for e in episodes]
    if gains:
        stats.max_gain_pct = max(gains)
        stats.median_gain_pct = statistics.median(gains)
        stats.median_retrace_pct = statistics.median(e.retrace_pct for e in episodes)
        stats.avg_days_to_peak = sum(e.days_to_peak for e in episodes) / len(episodes)
        last = max(episodes, key=lambda e: e.peak_date)
        stats.last_peak_date = last.peak_date
        stats.days_since_last_peak = (now - datetime.fromisoformat(last.peak_date).replace(tzinfo=timezone.utc)).days
        stats.current_vs_last_peak_pct = _pct(prices[-1][1], last.peak_price)

    closes = [p for _, p in prices]
    cur = closes[-1]
    stats.price_now = cur
    stats.high_12m = max(closes)
    stats.low_12m = min(closes)
    stats.current_vs_high_12m_pct = _pct(cur, stats.high_12m)
    stats.current_vs_low_12m_pct = _pct(cur, stats.low_12m)
    last90 = [p for ts, p in prices if (now - ts).days <= 90]
    if last90:
        stats.low_90d = min(last90)
        stats.current_vs_low_90d_pct = _pct(cur, stats.low_90d)
        stats.change_90d_pct = _pct(cur, last90[0])
    ytd = [p for ts, p in prices if ts >= year_start]
    if ytd:
        stats.change_ytd_pct = _pct(cur, ytd[0])
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes)) if closes[k - 1] > 0 and closes[k] > 0]
    if len(rets) > 5:
        stats.annualised_vol_pct = statistics.pstdev(rets) * math.sqrt(365) * 100
        stats.days_over_20pct = sum(1 for r in rets if abs(r) >= math.log(1.2))
    if volumes:
        vols = [v for _, v in volumes]
        recent = [v for ts, v in volumes if (now - ts).days <= 30]
        base = [v for ts, v in volumes if 30 < (now - ts).days <= 180]
        if recent and base and statistics.median(base) > 0:
            stats.volume_30d_vs_180d = statistics.median(recent) / statistics.median(base)
        stats.volume_median_usd = statistics.median(vols)
    stats.propensity = propensity(stats)
    return stats


def propensity(s: PumpStats) -> float | None:
    """0-10: how much the history looks like a repeat pumper that currently has room.

    Count of episodes, recency of the last one, how far below the last peak the price
    sits, and whether volume is picking up. Transparent on purpose; not a prediction.
    """
    if not s.available:
        return None
    score = 0.0
    score += min(s.pumps_12m, 4) * 1.5  # up to 6 for 4+ episodes
    if s.days_since_last_peak is not None:
        if s.days_since_last_peak <= 120:
            score += 1.5
        elif s.days_since_last_peak <= 240:
            score += 0.75
    if s.current_vs_last_peak_pct is not None and s.current_vs_last_peak_pct <= -50:
        score += 1.5  # gave most of it back: room to run again if it repeats
    if s.volume_30d_vs_180d is not None and s.volume_30d_vs_180d >= 1.5:
        score += 1.0
    return min(score, 10.0)
