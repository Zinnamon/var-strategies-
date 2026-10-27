"""Order-book price impact. Pure functions over a list of (price, quantity) levels so the
math is testable without a network.

Two questions are answered for each token:
1. "If I market-buy $N of spot on Binance, what do I pay and where does the price end?"
   (`sweep`), and
2. "How many dollars does it take to push the marginal price up by X%?" (`cost_to_move`).

Both use the visible book only. Binance returns at most 5000 levels per side, and a
market order also attracts new quotes and cancellations in real time, so the figures are
a lower bound on cost for small sizes and an upper bound on what the visible book can
absorb for large ones.
"""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

from varbot.models import FillResult, MoveCost

Level = tuple[float, float]  # (price, base quantity)

DEFAULT_SIZES_USD = (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
DEFAULT_MOVES_PCT = (1.0, 2.0, 5.0, 10.0, 20.0)


def parse_levels(raw: Iterable[Sequence]) -> list[Level]:
    out: list[Level] = []
    for item in raw:
        price, qty = float(item[0]), float(item[1])
        if price > 0 and qty > 0:
            out.append((price, qty))
    return out


def mid_price(bids: Sequence[Level], asks: Sequence[Level]) -> float:
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2
    return asks[0][0] if asks else (bids[0][0] if bids else 0.0)


def sweep(levels: Sequence[Level], notional_usd: float, side: Literal["buy", "sell"], mid: float | None = None) -> FillResult:
    """Walk one side of the book (asks for a buy, bids for a sell) with a USD notional."""
    if not levels:
        return FillResult(
            side=side, notional_usd=notional_usd, filled_usd=0.0, filled_base=0.0, avg_price=None,
            final_price=None, best_price=0.0, mid_price=mid or 0.0, impact_pct_vs_best=None,
            impact_pct_vs_mid=None, avg_slippage_pct=None, levels_consumed=0, exhausted=True,
        )
    best = levels[0][0]
    mid = mid or best
    remaining = notional_usd
    cost = 0.0
    base = 0.0
    final = best
    consumed = 0
    for price, qty in levels:
        if remaining <= 1e-9:
            break
        level_usd = price * qty
        take_usd = min(remaining, level_usd)
        cost += take_usd
        base += take_usd / price
        remaining -= take_usd
        final = price
        consumed += 1
    exhausted = remaining > 1e-6
    avg = cost / base if base else None
    sign = 1 if side == "buy" else -1
    return FillResult(
        side=side,
        notional_usd=notional_usd,
        filled_usd=cost,
        filled_base=base,
        avg_price=avg,
        final_price=final,
        best_price=best,
        mid_price=mid,
        impact_pct_vs_best=sign * (final - best) / best * 100,
        impact_pct_vs_mid=sign * (final - mid) / mid * 100 if mid else None,
        avg_slippage_pct=sign * (avg - best) / best * 100 if avg else None,
        levels_consumed=consumed,
        exhausted=exhausted,
    )


def cost_to_move(levels: Sequence[Level], move_pct: float, side: Literal["buy", "sell"]) -> MoveCost:
    """USD needed to consume every level up to best * (1 +/- move_pct)."""
    if not levels:
        return MoveCost(side=side, move_pct=move_pct, notional_usd=0.0, beyond_visible_book=True)
    best = levels[0][0]
    target = best * (1 + move_pct / 100) if side == "buy" else best * (1 - move_pct / 100)
    total = 0.0
    reached = False
    for price, qty in levels:
        if (side == "buy" and price > target) or (side == "sell" and price < target):
            reached = True
            break
        total += price * qty
    # If we never saw a level beyond the target, the visible book ends before the move.
    return MoveCost(side=side, move_pct=move_pct, notional_usd=total, beyond_visible_book=not reached)


def depth_within(levels: Sequence[Level], pct: float, side: Literal["buy", "sell"], ref: float) -> float:
    """USD resting within pct of a reference price (usually mid)."""
    limit = ref * (1 + pct / 100) if side == "buy" else ref * (1 - pct / 100)
    total = 0.0
    for price, qty in levels:
        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
            break
        total += price * qty
    return total


def total_depth(levels: Sequence[Level]) -> float:
    return sum(p * q for p, q in levels)
