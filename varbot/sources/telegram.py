"""Telegram activity without any API credentials.

Public channels expose a web preview at https://t.me/s/<channel> that carries the
subscriber count and the latest ~20 posts with timestamps and view counts. Groups and
channels without a preview only expose https://t.me/<name>, which still shows the
member count. Both are parsed here. `parse_channel_html` is pure so it is testable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from varbot.config import SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import TelegramMetrics

log = logging.getLogger(__name__)
TTL = 6 * 3600


def parse_count(text: str | None) -> int | None:
    """'12.3K' -> 12300, '1 234' -> 1234, '2M' -> 2000000."""
    if not text:
        return None
    t = text.strip().replace(" ", "").replace(" ", "").replace(",", "")
    m = re.match(r"^([\d.]+)([KkMm]?)$", t)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).upper()
    if unit == "K":
        n *= 1_000
    elif unit == "M":
        n *= 1_000_000
    return int(n)


def parse_channel_html(html: str, channel: str, now: datetime | None = None) -> TelegramMetrics:
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    m = TelegramMetrics(channel=channel, available=True)

    title_el = soup.select_one(".tgme_channel_info_header_title, .tgme_page_title")
    if title_el:
        m.title = title_el.get_text(" ", strip=True)

    # Preview page (channels): counters like "12.3K subscribers"
    for counter in soup.select(".tgme_channel_info_counter"):
        value = counter.select_one(".counter_value")
        kind = counter.select_one(".counter_type")
        if value and kind and "subscriber" in kind.get_text().lower():
            m.members = parse_count(value.get_text())
            m.kind = "channel"

    # Plain page (groups, or channels without preview): "1 234 members, 56 online"
    if m.members is None:
        extra = soup.select_one(".tgme_page_extra")
        if extra:
            text = extra.get_text(" ", strip=True)
            mm = re.search(r"([\d\s.,KkMm]+)\s+(members|subscribers)", text)
            if mm:
                m.members = parse_count(mm.group(1))
                m.kind = "group" if mm.group(2) == "members" else "channel"
            mo = re.search(r"([\d\s.,KkMm]+)\s+online", text)
            if mo:
                m.online = parse_count(mo.group(1))

    cutoff7 = now - timedelta(days=7)
    cutoff30 = now - timedelta(days=30)
    views_total = 0
    views_n = 0
    latest: datetime | None = None
    posts: list[str] = []
    for msg in soup.select(".tgme_widget_message"):
        time_el = msg.select_one("time[datetime]")
        if not time_el:
            continue
        try:
            ts = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
        except ValueError:
            continue
        latest = ts if latest is None or ts > latest else latest
        if ts >= cutoff7:
            m.posts_7d += 1
        if ts >= cutoff30:
            m.posts_30d += 1
            views_el = msg.select_one(".tgme_widget_message_views")
            v = parse_count(views_el.get_text()) if views_el else None
            if v is not None:
                views_total += v
                views_n += 1
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el and len(posts) < 5:
            posts.append(text_el.get_text(" ", strip=True)[:200])
    if views_n:
        m.avg_views_30d = views_total / views_n
    if latest:
        m.last_post_at = latest.isoformat(timespec="seconds")
        m.days_since_last_post = (now - latest).total_seconds() / 86400
    m.recent_posts = list(reversed(posts))  # newest first
    if m.members is None and not m.posts_30d:
        m.available = False
        m.note = "page had no recognisable channel data (private, renamed, or layout changed)"
    elif m.kind is None:
        m.kind = "channel" if m.posts_30d else "unknown"
    return m


def telegram_metrics(channel: str | None) -> TelegramMetrics:
    if not channel:
        return TelegramMetrics(available=False, note="no Telegram channel known for this token")
    channel = channel.strip().lstrip("@").split("/")[-1]
    base = SETTINGS.telegram_web_base
    try:
        html = http().get(f"{base}/s/{channel}", kind="text", ttl=TTL)
    except (HttpError, OfflineError) as exc:
        return TelegramMetrics(channel=channel, available=False, note=f"fetch failed: {exc}")
    metrics = parse_channel_html(html, channel)
    if metrics.members is None:
        # The /s/ preview redirects to the plain page for groups; fetch it explicitly.
        try:
            html2 = http().get(f"{base}/{channel}", kind="text", ttl=TTL)
            plain = parse_channel_html(html2, channel)
            if plain.members is not None:
                metrics.members, metrics.online, metrics.kind = plain.members, plain.online, plain.kind
                metrics.title = metrics.title or plain.title
                metrics.available = True
                metrics.note = "group: member count only, posts are not public" if plain.kind == "group" else metrics.note
        except (HttpError, OfflineError):
            pass
    return metrics
