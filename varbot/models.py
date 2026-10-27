"""Typed records passed between the modules and written out as JSON."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class QuoteTier(BaseModel):
    tier: str  # base | size_1k | size_100k | size_1m
    notional_usd: Optional[float]
    bid: float
    ask: float

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 1e4 if mid else 0.0


class Listing(BaseModel):
    """One market from Variational's /metadata/v2/stats."""

    ticker: str  # e.g. BTC-PERP, XAU-SWAP
    contract_type: str  # perpetual | swap
    name: Optional[str] = None
    mark_price: float
    volume_24h: float
    long_oi_usd: float = 0.0
    short_oi_usd: float = 0.0
    funding_interval_rate: Optional[float] = None
    funding_annualized_rate: Optional[float] = None
    funding_interval_seconds: Optional[int] = None
    base_spread_bps: Optional[float] = None
    quotes: list[QuoteTier] = Field(default_factory=list)
    quotes_updated_at: Optional[str] = None

    @property
    def symbol(self) -> str:
        """Ticker without the -PERP / -SWAP suffix."""
        for suffix in ("-PERP", "-SWAP"):
            if self.ticker.endswith(suffix):
                return self.ticker[: -len(suffix)]
        return self.ticker

    @property
    def open_interest_usd(self) -> float:
        return self.long_oi_usd + self.short_oi_usd


class AssetClassification(BaseModel):
    ticker: str
    symbol: str
    base_symbol: str  # multiplier prefix stripped: 1000BONK -> BONK
    multiplier: int = 1
    asset_class: Literal["crypto", "equity", "etf", "commodity", "index", "fx", "pre_ipo", "unknown"]
    reason: str


class TokenFundamentals(BaseModel):
    coingecko_id: Optional[str] = None
    name: Optional[str] = None
    symbol: Optional[str] = None
    price_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    market_cap_rank: Optional[int] = None
    fdv_usd: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    circulating_supply: Optional[float] = None
    total_supply: Optional[float] = None
    max_supply: Optional[float] = None
    ath_usd: Optional[float] = None
    ath_change_pct: Optional[float] = None
    ath_date: Optional[str] = None
    change_24h_pct: Optional[float] = None
    change_7d_pct: Optional[float] = None
    change_30d_pct: Optional[float] = None
    categories: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    homepage: Optional[str] = None
    twitter_handle: Optional[str] = None
    telegram_channel: Optional[str] = None
    discord_url: Optional[str] = None
    github_repos: list[str] = Field(default_factory=list)
    chains: list[str] = Field(default_factory=list)
    genesis_date: Optional[str] = None
    # CoinGecko community / developer snapshots
    twitter_followers: Optional[int] = None
    telegram_users: Optional[int] = None
    reddit_subscribers: Optional[int] = None
    github_stars: Optional[int] = None
    github_forks: Optional[int] = None
    commits_4w: Optional[int] = None
    resolution_note: str = ""

    @property
    def float_ratio(self) -> Optional[float]:
        """Circulating / total supply; low values mean heavy future unlocks."""
        if self.circulating_supply and self.total_supply:
            return self.circulating_supply / self.total_supply
        return None

    @property
    def volume_to_mcap(self) -> Optional[float]:
        if self.volume_24h_usd and self.market_cap_usd:
            return self.volume_24h_usd / self.market_cap_usd
        return None


class FillResult(BaseModel):
    """Result of sweeping one side of an order book with a notional buy or sell."""

    side: Literal["buy", "sell"]
    notional_usd: float
    filled_usd: float
    filled_base: float
    avg_price: Optional[float]
    final_price: Optional[float]  # price of the last level touched
    best_price: float  # best ask (buy) or best bid (sell) before the sweep
    mid_price: float
    impact_pct_vs_best: Optional[float]  # (final - best) / best
    impact_pct_vs_mid: Optional[float]
    avg_slippage_pct: Optional[float]  # (avg - best)/best
    levels_consumed: int
    exhausted: bool  # the visible book ran out before the notional was filled


class MoveCost(BaseModel):
    """USD notional needed to push the marginal price by `move_pct`."""

    side: Literal["buy", "sell"]
    move_pct: float
    notional_usd: float
    beyond_visible_book: bool


class BinanceImpact(BaseModel):
    binance_symbol: Optional[str] = None
    quote_asset: Optional[str] = None
    listed: bool = False
    note: str = ""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread_bps: Optional[float] = None
    book_depth_levels: int = 0
    ask_depth_usd: Optional[float] = None  # total visible ask liquidity in USD
    bid_depth_usd: Optional[float] = None
    ask_depth_2pct_usd: Optional[float] = None  # within 2% of mid
    bid_depth_2pct_usd: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    trades_24h: Optional[int] = None
    fills: list[FillResult] = Field(default_factory=list)
    move_costs: list[MoveCost] = Field(default_factory=list)
    fetched_at: Optional[str] = None


class VariationalImpact(BaseModel):
    """What Variational's own OLP quotes imply for a taker at the published tiers."""

    tier: str
    notional_usd: Optional[float]
    buy_premium_pct_vs_mark: float
    sell_discount_pct_vs_mark: float
    spread_bps: float


class XMetrics(BaseModel):
    handle: Optional[str] = None
    available: bool = False
    note: str = ""
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    tweet_count: Optional[int] = None
    verified: Optional[bool] = None
    account_created_at: Optional[str] = None
    account_age_days: Optional[int] = None
    posts_7d: int = 0
    posts_30d: int = 0
    avg_likes_30d: Optional[float] = None
    avg_reposts_30d: Optional[float] = None
    avg_replies_30d: Optional[float] = None
    avg_views_30d: Optional[float] = None
    engagement_rate_pct: Optional[float] = None  # (likes+reposts+replies)/followers per post
    last_post_at: Optional[str] = None
    days_since_last_post: Optional[float] = None
    recent_posts: list[str] = Field(default_factory=list)


class TelegramMetrics(BaseModel):
    channel: Optional[str] = None
    available: bool = False
    note: str = ""
    kind: Optional[str] = None  # channel | group | unknown
    title: Optional[str] = None
    members: Optional[int] = None
    online: Optional[int] = None
    posts_7d: int = 0
    posts_30d: int = 0
    avg_views_30d: Optional[float] = None
    last_post_at: Optional[str] = None
    days_since_last_post: Optional[float] = None
    recent_posts: list[str] = Field(default_factory=list)


class Development(BaseModel):
    date: Optional[str] = None
    headline: str
    kind: str = "other"  # product | partnership | funding | listing | token | governance | incident | team | other
    source_url: Optional[str] = None


class BusinessResearch(BaseModel):
    available: bool = False
    note: str = ""
    summary: Optional[str] = None
    what_it_does: Optional[str] = None
    token_utility: Optional[str] = None
    team_and_backers: Optional[str] = None
    recent_developments: list[Development] = Field(default_factory=list)
    partnerships: list[str] = Field(default_factory=list)
    funding: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    positives: list[str] = Field(default_factory=list)
    activity_score: Optional[int] = None  # 0-10, how much real business activity in the last 90 days
    confidence: Optional[str] = None  # low | medium | high
    sources: list[str] = Field(default_factory=list)
    model: Optional[str] = None


class Scores(BaseModel):
    liquidity: Optional[float] = None  # 0-10
    social: Optional[float] = None
    fundamentals: Optional[float] = None
    business: Optional[float] = None
    overall: Optional[float] = None
    flags: list[str] = Field(default_factory=list)


class TokenReport(BaseModel):
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    listing: Listing
    classification: AssetClassification
    fundamentals: TokenFundamentals = Field(default_factory=TokenFundamentals)
    is_small_cap: bool = False
    small_cap_threshold_usd: Optional[float] = None
    variational_impact: list[VariationalImpact] = Field(default_factory=list)
    binance: BinanceImpact = Field(default_factory=BinanceImpact)
    x: XMetrics = Field(default_factory=XMetrics)
    telegram: TelegramMetrics = Field(default_factory=TelegramMetrics)
    research: BusinessResearch = Field(default_factory=BusinessResearch)
    scores: Scores = Field(default_factory=Scores)
