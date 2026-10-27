from datetime import datetime, timezone

from varbot.sources.telegram import parse_channel_html, parse_count
from varbot.sources.x_api import summarise_tweets

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_parse_count_units():
    assert parse_count("12.3K") == 12300
    assert parse_count("9 800") == 9800
    assert parse_count("2M") == 2_000_000
    assert parse_count("1,234") == 1234
    assert parse_count("") is None


def test_telegram_channel_preview(tme_channel_html):
    m = parse_channel_html(tme_channel_html, "examplecoin", now=NOW)
    assert m.available and m.kind == "channel"
    assert m.title == "Example Coin Announcements"
    assert m.members == 12300
    assert m.posts_7d == 1
    assert m.posts_30d == 2
    assert m.avg_views_30d == (15200 + 9800) / 2
    assert 3.0 < m.days_since_last_post < 3.2
    assert m.recent_posts[0].startswith("AMA")


def test_telegram_group_page(tme_group_html):
    m = parse_channel_html(tme_group_html, "examplecoin_chat", now=NOW)
    assert m.available and m.kind == "group"
    assert m.members == 4521
    assert m.online == 231
    assert m.posts_30d == 0


def test_x_summary(x_user, x_tweets):
    m = summarise_tweets(x_user, x_tweets, now=NOW)
    assert m.available and m.handle == "examplecoin"
    assert m.followers == 50000
    assert m.posts_7d == 1  # the Oct 20 post is 7 days and 2 hours old
    assert m.posts_30d == 3
    assert m.avg_likes_30d == (200 + 60 + 120) / 3
    assert m.avg_reposts_30d == (45 + 11 + 27) / 3
    assert m.engagement_rate_pct is not None and 0.2 < m.engagement_rate_pct < 0.4
    assert 2.0 < m.days_since_last_post < 2.2
    assert m.account_age_days > 1000
