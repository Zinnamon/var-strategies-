# varbot

Research bot for the tokens listed on [Variational Omni](https://docs.variational.io).
For every crypto perp Variational quotes it pulls:

- **Market data** from Variational's public stats API: mark price, 24h volume, long/short open
  interest, funding, and the OLP's own quotes at $1k / $100k / $1m notional.
- **Fundamentals** from CoinGecko: market cap, FDV, volume, supply and float, ATH drawdown,
  categories, chains, official links, and the community and developer snapshots.
- **Exchange listings**: every spot pair the token trades on (CEX and DEX) from CoinGecko's
  tickers, with volume share, trust score and which of the major CEXs list it: Binance,
  Bybit, OKX, Coinbase, Kraken, KuCoin, Gate, MEXC, Bitget, HTX, Crypto.com, Upbit and more.
- **Spot price impact on every major venue**: sweeps the public order book of each major CEX
  that lists the token (Binance 5000 levels, Gate 1000, Kraken 500, OKX 400, Bybit 200,
  Bitget/HTX/Crypto.com 150, KuCoin 100, Coinbase 50) and reports, for each buy size, the
  average fill, where the price ends up, and how many dollars it takes to push the price
  1 / 2 / 5 / 10 / 20 %.
- **Real time** (`varbot watch`): keeps live order books over WebSockets for Binance, Bybit
  and OKX, recomputes the impact table every couple of seconds, serves it as JSON over HTTP,
  rewrites a JSON file, and prints alerts when impact crosses a threshold. Variational's
  own stats are re-polled every 30 s, the lifetime of its cache.
- **X activity**: followers, posting cadence, likes / reposts / replies per post, engagement
  rate, days since the last post. Needs an X API bearer token.
- **Telegram activity**: subscriber or member count, posts per 30 days, average views, days
  since the last post. No credentials needed (public web preview).
- **Price history**: twelve months of daily prices per token. Counts the runs of 50 %+ within
  three weeks (tunable), their size, speed and give-back, where the price sits against the
  last peak and the 12-month range, volatility, and whether volume is picking up. The
  report's highlights list the repeat pumpers and the most active small caps.
- **Business activity** (optional, `--research`): Claude searches the web and fetches primary
  sources to list what the project shipped, announced, raised, listed or broke in the last 90
  days, with URLs, red flags and a 0-10 activity score.

Everything is combined into 0-10 scores (liquidity, social, fundamentals, business), ranked,
and written as Markdown, JSON and CSV. A `notify` command pushes a report to a Telegram chat.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in the keys you have; every key is optional
```

| Key | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | `--research` (Claude web research) |
| `X_BEARER_TOKEN` | X follower and engagement metrics |
| `COINGECKO_API_KEY` | faster CoinGecko calls (public API works but is slow) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `varbot notify` |

## Use

```bash
varbot markets                       # every crypto perp Variational quotes, with its own $100k buy premium
varbot markets --all                 # include equities, ETFs, commodities, swaps

varbot scan --out reports/           # small caps (<= $100M mcap): fundamentals + Binance impact + socials
varbot scan --cap-basis fdv --max-mcap 100m --out reports/   # everything under $100M FDV
varbot scan --max-mcap 50m --limit 25 --research --out reports/
varbot scan --pump-gain 80 --pump-window 14   # stricter definition of a pump
varbot scan --symbols STO,ERA,SPX --sizes 10k,50k,100k,500k --moves 1,2,5,10

varbot scan --venues binance,bybit,okx   # restrict the order-book venues (default: all supported)
varbot token STO --research          # one token, full report to stdout
varbot impact PEPE --sizes 100k,1m   # Binance book sweep only
varbot impact PEPE --venue okx       # same on another venue

varbot watch STO,ERA,SPX --serve 8080 --json-out live.json --alert-pct 3
                                      # live WebSocket books; GET http://localhost:8080/ returns the latest JSON

varbot notify reports/scan.md        # push to Telegram
varbot --offline scan                # recorded fixtures, no network (used by the tests)
```

### Real-time mode

`varbot watch` subscribes to the diff-depth streams of Binance (snapshot plus deltas, with
the sequence checks Binance documents), Bybit v5 (`orderbook.200`) and OKX (`books`, 400
levels), keeps a local book per venue, and every `--interval` seconds recomputes the same
impact table the scan produces. Output options:

- the console table (`--quiet` to print alerts only),
- `--json-out live.json`, rewritten atomically on each tick,
- `--serve 8080`, a JSON endpoint you can poll from a dashboard or another bot,
- `--alert-pct 3 --alert-size 100k`, prints when the $100k buy impact reaches 3 % and when it clears.

Each venue row shows the age of the last update and a state: `live`, `stale` (no update
for 30 s) or `sync` (reconnecting or re-syncing after a sequence gap). Books that go out
of sequence are rebuilt automatically. Other venues (Coinbase, Kraken, KuCoin, Gate, MEXC,
Bitget, HTX, Crypto.com) are REST-only and appear in `scan` and `impact`, not in `watch`.

Reports land in `reports/scan.md`, `scan.json` and `scan.csv`. The CSV has one row per
token with the impact numbers, social counts and scores, ready for a spreadsheet.

### How the price impact is computed

For a buy of `N` dollars the bot walks the ask side of Binance's spot book, filling each
level until `N` is spent. It reports:

- average fill price versus the best ask (what you pay on average),
- the price of the last level touched versus the best ask and versus mid (where the market
  is after your order),
- whether the visible book ran out before `N` was filled.

"Dollars to move X %" sums every ask up to `best_ask × (1 + X/100)`. If the visible book ends
before that price the figure is marked as beyond the visible book.

Two caveats. The book is a snapshot: market makers re-quote within milliseconds of a large
fill, so these numbers are the order of magnitude, not a quote. And tokens priced in BTC or
another non-stable quote on Binance are converted to USD at the current BTCUSDT price.

Variational's own quote tiers are shown next to the Binance table so you can compare the
premium the OLP charges at $100k with what the spot book would charge.

### Scores

All thresholds live in `varbot/analysis/scoring.py`. Liquidity weighs the $100k impact,
the asks within 2 % and 24h spot volume. Social weighs followers, cadence and engagement
(X) and members and cadence (Telegram). Fundamentals weighs market cap, volume-to-cap and
float. Business is the research activity score. Flags call out the usual problems: book
exhaustion, silent socials, low float, 90 %+ drawdowns, suspiciously low engagement for the
follower count, no commits.

## Running it on a server

See `deploy/README.md`. In short: `.github/workflows/scan.yml` runs the daily scan on
GitHub Actions for free (without Binance, Bybit and OKX, which refuse US runner IPs), and
`deploy/setup.sh` sets up a Hetzner or any Ubuntu VPS with a systemd timer for the scan
and an always-on `watch` service that sends alerts to Telegram.

A plain cron line works too:

```
0 7 * * * cd /path/to/var-strategies- && .venv/bin/varbot scan --max-mcap 100m --out reports/ && .venv/bin/varbot notify reports/scan.md --max-messages 3
```

## Data sources and limits

| Source | Endpoint | Notes |
|---|---|---|
| Variational | `omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats` | documented at docs.variational.io/technical-documentation/api; one call returns every market |
| CoinGecko | `/coins/list`, `/search`, `/coins/markets`, `/coins/{id}`, `/coins/{id}/tickers` | public tier ~30 req/min; the bot spaces calls and caches details for 24h, tickers for 30 min |
| Binance | `/api/v3/exchangeInfo`, `/depth?limit=5000`, `/ticker/24hr`; WS `<sym>@depth@100ms` | public, no key |
| Bybit, OKX | REST order book; WS `orderbook.200`, `books` | public, no key |
| Coinbase, Kraken, KuCoin, Gate, MEXC, Bitget, HTX, Crypto.com | REST order book | public, no key; level caps listed above |
| X | `/2/users/by/username`, `/2/users/{id}/tweets` | bearer token; the free tier is very limited, Basic or above recommended |
| Telegram | `t.me/s/<channel>`, `t.me/<name>` | public preview; groups expose member count only |
| Claude | Messages API with `web_search` and `web_fetch` server tools | model set by `VARBOT_MODEL`, default `claude-opus-5-5` |

Responses are cached under `.varbot-cache/` (override with `VARBOT_CACHE_DIR`). Delete the
directory to force fresh data.

Variational's API publishes no asset class, so equities, ETFs, commodities and pre-IPO names
are separated from crypto by `data/asset_classes.json` plus a name heuristic. Symbol-to-
CoinGecko mapping is automatic (name match, then best market-cap rank); fix a wrong match by
adding a line to `data/coingecko_overrides.json`.

## Tests

```bash
VARBOT_OFFLINE=1 pytest
```

The suite runs without network: a recorded Variational stats payload, a synthetic thin
order book, sample X API JSON and Telegram preview HTML.

## Layout

```
varbot/
  cli.py             commands: markets, scan, token, impact, notify
  pipeline.py        orchestration
  report.py          markdown / json / csv
  sources/           variational, coingecko, binance, venues (other CEX books), x_api, telegram
  live/              book (local order book), feeds (Binance/Bybit/OKX WebSockets), runner (watch)
  analysis/          classify (crypto vs tradfi), impact (book maths), scoring
  research/          business.py (Claude web research)
data/                asset classes and CoinGecko id overrides
tests/               offline tests and fixtures
```
