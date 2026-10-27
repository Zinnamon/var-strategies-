# Running varbot on a server

Two ways, which can be combined:

| | GitHub Actions | VPS (Hetzner) |
|---|---|---|
| Daily scan | yes, free | yes |
| 24/7 `watch` with live books | no (jobs stop after 6 h) | yes |
| Binance / Bybit / OKX data | no, US runner IPs are refused | yes, pick an EU location |
| Setup | add secrets, enable the workflow | `deploy/setup.sh` |

## GitHub Actions

The workflow is `.github/workflows/scan.yml`. It runs every day at 07:17 UTC and on demand.

1. In the repository go to Settings, Secrets and variables, Actions, and add:
   `COINGECKO_API_KEY`, `ANTHROPIC_API_KEY`, `X_BEARER_TOKEN`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. Leave out any you do not have; that source is skipped.
2. Merge or set the `varbot` branch as the default branch. Scheduled workflows only
   run from the default branch. Manual runs work from any branch.
3. Actions tab, "daily scan", "Run workflow" to try it. The report is attached to the
   run as an artifact (`reports/scan.md`, `.json`, `.csv`) and the first three
   Telegram messages of the summary are sent if the bot secrets are set.

Because the hosted runners sit in the US, the workflow sweeps Coinbase, Kraken,
KuCoin, Gate, MEXC, Bitget, HTX and Crypto.com only. Binance, Bybit and OKX refuse
API calls from US IPs. For those figures run the scan on the VPS.

## Hetzner VPS

### Create the server

1. https://console.hetzner.cloud, create a project, "Add server".
2. Location: Falkenstein, Nuremberg or Helsinki (EU IPs reach every exchange).
3. Image: Ubuntu 24.04.
4. Type: Shared vCPU, x86, CX22 (2 vCPU, 4 GB). That is more than enough.
5. SSH key: add your public key (`cat ~/.ssh/id_ed25519.pub`; create one with
   `ssh-keygen -t ed25519` if you have none). Skip the password option.
6. Create. Note the IPv4 address.

### Install

```bash
ssh root@YOUR_IP
curl -fsSL https://raw.githubusercontent.com/Zinnamon/var-strategies-/varbot/deploy/setup.sh | bash
```

The script installs Python and git, creates a `varbot` system user, clones the
`varbot` branch to `/opt/varbot/app`, builds a venv, installs the systemd units,
enables the daily scan timer, locks the firewall down to SSH, and runs
`varbot markets` once as a smoke test.

### Configure

```bash
nano /opt/varbot/app/.env
```

Fill in the keys you have and the watch settings:

```
WATCH_SYMBOLS=STO,ERA,SPX
ALERT_PCT=3
ALERT_SIZE=100k
SCAN_MAX_MCAP=100m
SCAN_EXTRA_ARGS=
```

`SCAN_EXTRA_ARGS` takes anything `varbot scan` accepts, for example
`--research --limit 30` to add Claude research for the 30 smallest caps.

### Start

```bash
systemctl start varbot-watch           # live books and alerts, restarts on failure
systemctl start varbot-scan            # one scan right now; the timer repeats it daily
systemctl status varbot-watch varbot-scan.timer
journalctl -u varbot-watch -f          # alerts and reconnects
journalctl -u varbot-scan --since today
```

Reports: `/opt/varbot/reports/scan.md|json|csv`. Live JSON: `curl 127.0.0.1:8080/`
on the box, or from your laptop through a tunnel:
`ssh -L 8080:127.0.0.1:8080 root@YOUR_IP` then open http://localhost:8080/.

### Update

```bash
curl -fsSL https://raw.githubusercontent.com/Zinnamon/var-strategies-/varbot/deploy/setup.sh | bash
systemctl restart varbot-watch
```

The script pulls the branch, reinstalls, and leaves `.env` alone.

### Change the schedule

`systemctl edit varbot-scan.timer`, then set `OnCalendar=` (for example
`*-*-* 06,18:00:00 UTC` for twice a day), and `systemctl daemon-reload`.
