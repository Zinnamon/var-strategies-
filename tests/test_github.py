from datetime import datetime, timezone

from varbot.sources import github as gh


def test_parse_repo_url():
    assert gh.parse_repo_url("https://github.com/phala-network/phala-blockchain") == ("phala-network", "phala-blockchain")
    assert gh.parse_repo_url("https://github.com/MinaProtocol/mina/tree/develop") == ("MinaProtocol", "mina")
    assert gh.parse_repo_url("https://github.com/iExecBlockchainComputing") == ("iExecBlockchainComputing", None)
    assert gh.parse_repo_url("https://github.com/org/repo.git") == ("org", "repo")
    assert gh.parse_repo_url("https://gitlab.com/x/y") is None


def test_summarise():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    info = {"full_name": "org/repo", "pushed_at": "2026-10-20T10:00:00Z", "stargazers_count": 321, "archived": False}
    commits = [{"commit": {"committer": {"date": "2026-10-20T10:00:00Z"}}}, {"commit": {"committer": {"date": "2026-10-02T10:00:00Z"}}}]
    a = gh.summarise(info, commits, now=now)
    assert a.available and a.repo == "org/repo" and a.days_since_push == 7 and a.commits_30d == 2
    assert a.last_commit == "2026-10-20T10:00:00Z" and a.stars == 321


def test_github_activity_falls_back_to_org_repo(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        if path == "/repos/org/gone":
            from varbot.http import HttpError
            raise HttpError(404, path, "nope")
        if path == "/orgs/org/repos":
            return [{"full_name": "org/live"}]
        if path == "/repos/org/live":
            return {"full_name": "org/live", "pushed_at": "2026-10-27T00:00:00Z", "stargazers_count": 5, "archived": False}
        if path == "/repos/org/live/commits":
            return [{"commit": {"committer": {"date": "2026-10-27T00:00:00Z"}}}]
        raise AssertionError(path)

    monkeypatch.setattr(gh, "_get", fake_get)
    a = gh.github_activity(["https://github.com/org/gone"])
    assert a.available and a.repo == "org/live" and a.commits_30d == 1


def test_activity_score_uses_code_signal():
    from varbot.analysis.scoring import activity_score
    from varbot.models import BusinessResearch, ExchangeListing, GitHubActivity, PumpStats, TelegramMetrics, TokenFundamentals, XMetrics

    fresh = GitHubActivity(available=True, repo="a/b", days_since_push=3, commits_30d=20)
    dead = GitHubActivity(available=True, repo="a/b", days_since_push=200, commits_30d=0)
    s1, f1 = activity_score(TokenFundamentals(), XMetrics(), TelegramMetrics(), ExchangeListing(), BusinessResearch(), PumpStats(), fresh)
    s2, f2 = activity_score(TokenFundamentals(), XMetrics(), TelegramMetrics(), ExchangeListing(), BusinessResearch(), PumpStats(), dead)
    assert s1 == 10.0 and s2 == 1.0
    assert any("no GitHub push" in f for f in f2)
    s3, f3 = activity_score(TokenFundamentals(), XMetrics(), TelegramMetrics(), ExchangeListing(available=True, cex_count=5), BusinessResearch(), PumpStats())
    assert s3 is not None and any("trading only" in f for f in f3)
