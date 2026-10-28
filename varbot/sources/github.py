"""Code activity straight from GitHub, for the repos CoinGecko links to a token.

Unauthenticated the API allows 60 calls an hour per IP; a free personal token
(GITHUB_TOKEN, no scopes needed) lifts that to 5000. Two calls per repo: the repo
record (last push, archived flag, stars) and the commits of the last 30 days.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from varbot.config import SETTINGS
from varbot.http import HttpError, OfflineError, http
from varbot.models import GitHubActivity

log = logging.getLogger(__name__)

TTL = 12 * 3600
API = "https://api.github.com"


def parse_repo_url(url: str) -> tuple[str, str | None] | None:
    """'https://github.com/org/repo/tree/main' -> ('org', 'repo'); 'https://github.com/org' -> ('org', None)."""
    m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?", url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo and repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo or None


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if SETTINGS.github_token:
        h["Authorization"] = f"Bearer {SETTINGS.github_token}"
    return h


def _get(path: str, params: dict | None = None):
    return http().get(f"{API}{path}", params=params, headers=_headers(), ttl=TTL)


def most_recent_repo(owner: str) -> str | None:
    """For an org or user link, the repo pushed to most recently."""
    for kind in ("orgs", "users"):
        try:
            repos = _get(f"/{kind}/{owner}/repos", {"sort": "pushed", "per_page": 5, "type": "public"})
        except (HttpError, OfflineError):
            continue
        if repos:
            return repos[0]["full_name"]
    return None


def summarise(repo_info: dict, commits: list[dict], now: datetime | None = None) -> GitHubActivity:
    now = now or datetime.now(timezone.utc)
    a = GitHubActivity(available=True, repo=repo_info.get("full_name"), archived=bool(repo_info.get("archived")),
                       stars=repo_info.get("stargazers_count"))
    pushed = repo_info.get("pushed_at")
    if pushed:
        a.last_push = pushed
        a.days_since_push = (now - datetime.fromisoformat(pushed.replace("Z", "+00:00"))).days
    a.commits_30d = len(commits)
    if commits:
        dates = [c.get("commit", {}).get("committer", {}).get("date") or c.get("commit", {}).get("author", {}).get("date") for c in commits]
        dates = [d for d in dates if d]
        if dates:
            a.last_commit = max(dates)
    return a


def github_activity(repo_urls: list[str]) -> GitHubActivity:
    if not repo_urls:
        return GitHubActivity(available=False, note="no GitHub link known")
    parsed = [p for p in (parse_repo_url(u) for u in repo_urls) if p]
    if not parsed:
        return GitHubActivity(available=False, note="GitHub link not parseable")
    owner, repo = parsed[0]
    is_org = repo is None
    full = f"{owner}/{repo}" if repo else most_recent_repo(owner)
    if not full:
        return GitHubActivity(available=False, note=f"no public repos under {owner}", is_org=True)
    try:
        info = _get(f"/repos/{full}")
    except HttpError as exc:
        if exc.status == 404 and repo:
            # The linked repo is gone; fall back to the owner's most recent one.
            alt = most_recent_repo(owner)
            if alt and alt != full:
                return github_activity([f"https://github.com/{alt}"])
        return GitHubActivity(available=False, note=f"GitHub {exc.status} for {full}", repo=full)
    except OfflineError as exc:
        return GitHubActivity(available=False, note=str(exc), repo=full)
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    try:
        commits = _get(f"/repos/{full}/commits", {"since": since, "per_page": 100})
    except (HttpError, OfflineError) as exc:
        log.warning("github commits %s failed: %s", full, exc)
        commits = []
    a = summarise(info, commits if isinstance(commits, list) else [])
    a.is_org = is_org
    return a
