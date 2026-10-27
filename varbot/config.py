"""Settings, read once from the environment (and a .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DATA_DIR = REPO_ROOT / "data"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name, "0").lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    x_bearer_token: str = field(default_factory=lambda: _env("X_BEARER_TOKEN"))
    coingecko_api_key: str = field(default_factory=lambda: _env("COINGECKO_API_KEY"))
    coingecko_pro: bool = field(default_factory=lambda: _flag("COINGECKO_PRO"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    offline: bool = field(default_factory=lambda: _flag("VARBOT_OFFLINE"))
    cache_dir: Path = field(
        default_factory=lambda: Path(_env("VARBOT_CACHE_DIR") or (REPO_ROOT / ".varbot-cache"))
    )
    # Model used for the business-activity research. Opus 5.5 is the default
    # Anthropic recommends for new work; override with VARBOT_MODEL.
    research_model: str = field(default_factory=lambda: _env("VARBOT_MODEL", "claude-opus-5-5"))

    # Public endpoints. The Variational stats host is the one the docs publish
    # (docs.variational.io/technical-documentation/api) and the only one that
    # answers from a datacenter IP.
    variational_stats_url: str = (
        "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
    )
    binance_base: str = "https://api.binance.com"
    x_api_base: str = "https://api.x.com/2"
    telegram_web_base: str = "https://t.me"

    @property
    def coingecko_base(self) -> str:
        if self.coingecko_pro and self.coingecko_api_key:
            return "https://pro-api.coingecko.com/api/v3"
        return "https://api.coingecko.com/api/v3"


SETTINGS = Settings()
