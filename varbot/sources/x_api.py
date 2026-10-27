"""X (Twitter) activity for a project account, via the X API v2 with an app bearer token.

Endpoints:
- GET /2/users/by/username/{handle}  (public_metrics, created_at, verified, description)
- GET /2/users/{id}/tweets            (last 100 own posts, no retweets, with public_metrics)

The metric maths lives in `summarise_tweets` so it can be tested from recorded JSON.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from varbot.config import SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import XMetrics

log = logging.getLogger(__name__)

USER_FIELDS = "public_metrics,created_at,verified,verified_type,description"
TWEET_FIELDS = "public_metrics,created_at"
TTL = 6 * 3600


def _auth() -> dict:
    return {"Authorization": f"Bearer {SETTINGS.x_bearer_token}"}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarise_tweets(user: dict, tweets: list[dict], now: datetime | None = None) -> XMetrics:
    now = now or datetime.now(timezone.utc)
    pm = user.get("public_metrics") or {}
    m = XMetrics(
        handle=user.get("username"),
        available=True,
        user_id=user.get("id"),
        display_name=user.get("name"),
        followers=pm.get("followers_count"),
        following=pm.get("following_count"),
        tweet_count=pm.get("tweet_count"),
        verified=user.get("verified"),
        account_created_at=user.get("created_at"),
    )
    if user.get("created_at"):
        m.account_age_days = (now - _parse_ts(user["created_at"])).days
    cutoff7 = now - timedelta(days=7)
    cutoff30 = now - timedelta(days=30)
    likes = reposts = replies = views = 0
    n30 = 0
    latest: datetime | None = None
    for t in tweets:
        if not t.get("created_at"):
            continue
        ts = _parse_ts(t["created_at"])
        latest = ts if latest is None or ts > latest else latest
        if ts >= cutoff7:
            m.posts_7d += 1
        if ts >= cutoff30:
            m.posts_30d += 1
            n30 += 1
            tm = t.get("public_metrics") or {}
            likes += tm.get("like_count", 0)
            reposts += tm.get("retweet_count", 0) + tm.get("quote_count", 0)
            replies += tm.get("reply_count", 0)
            views += tm.get("impression_count", 0)
    if n30:
        m.avg_likes_30d = likes / n30
        m.avg_reposts_30d = reposts / n30
        m.avg_replies_30d = replies / n30
        m.avg_views_30d = views / n30 if views else None
        if m.followers:
            m.engagement_rate_pct = (likes + reposts + replies) / n30 / m.followers * 100
    if latest:
        m.last_post_at = latest.isoformat(timespec="seconds")
        m.days_since_last_post = (now - latest).total_seconds() / 86400
    m.recent_posts = [(t.get("text") or "").replace("\n", " ")[:200] for t in tweets[:5]]
    return m


def x_metrics(handle: str | None) -> XMetrics:
    if not handle:
        return XMetrics(available=False, note="no X handle known for this token")
    if not SETTINGS.x_bearer_token:
        return XMetrics(handle=handle, available=False, note="X_BEARER_TOKEN not set")
    base = SETTINGS.x_api_base
    try:
        user = http().get(f"{base}/users/by/username/{handle}", params={"user.fields": USER_FIELDS}, headers=_auth(), ttl=TTL)
    except (HttpError, OfflineError) as exc:
        return XMetrics(handle=handle, available=False, note=f"user lookup failed: {exc}")
    data = user.get("data")
    if not data:
        return XMetrics(handle=handle, available=False, note=f"account not found: {user.get('errors')}")
    try:
        tweets = http().get(
            f"{base}/users/{data['id']}/tweets",
            params={"max_results": 100, "tweet.fields": TWEET_FIELDS, "exclude": "retweets"},
            headers=_auth(),
            ttl=TTL,
        )
    except (HttpError, OfflineError) as exc:
        m = summarise_tweets(data, [])
        m.note = f"timeline unavailable: {exc}"
        return m
    return summarise_tweets(data, tweets.get("data") or [])
