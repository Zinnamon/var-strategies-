import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stats_body() -> dict:
    return json.loads((FIXTURES / "variational_stats_v2.json").read_text())


@pytest.fixture
def thin_book() -> dict:
    """A synthetic thin order book for a $0.03 token (STO-like). Quantities in base units.

    Ask side holds ~$110k within 10% of best, then a gap.
    """
    asks = []
    price = 0.0319
    for i in range(40):
        asks.append([f"{price:.6f}", f"{(300_000 if i < 5 else 120_000):.0f}"])
        price *= 1.0025  # 0.25% steps -> 40 levels covers ~10%
    asks.append(["0.0400", "5000000"])  # a lone deep level 25% away
    bids = []
    price = 0.0318
    for i in range(40):
        bids.append([f"{price:.6f}", "150000"])
        price /= 1.0025
    return {"lastUpdateId": 1, "bids": bids, "asks": asks}


@pytest.fixture
def tme_channel_html() -> str:
    return (FIXTURES / "tme_channel.html").read_text()


@pytest.fixture
def tme_group_html() -> str:
    return (FIXTURES / "tme_group.html").read_text()


@pytest.fixture
def x_user() -> dict:
    return json.loads((FIXTURES / "x_user.json").read_text())["data"]


@pytest.fixture
def x_tweets() -> list[dict]:
    return json.loads((FIXTURES / "x_tweets.json").read_text())["data"]
