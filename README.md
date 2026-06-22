# mm

Cloud-hosted limit-order bot for Kalshi (signed REST API). Quotes both sides of a
configured market's central strikes with inventory-aware pricing and hard risk
controls: per-order size, inventory cap, total-exposure cap, daily-loss
kill-switch, and auto-expiring orders.

Nothing sensitive lives in source — credentials and the target market are
injected from the environment at runtime.

## Run

```bash
python -m mm.bot --status        # read-only: balance, positions, resting orders
python -m mm.bot --dry           # dry-run (default): prints intended orders, places none
LIVE=1 python -m mm.bot --live   # places real orders (guarded by LIVE=1)
python -m mm.bot --flatten       # cancel all resting orders
```

Default is dry-run. Live trading requires `LIVE=1` plus the environment below.

## Configuration (environment only)

| var | purpose |
|-----|---------|
| `KALSHI_API_KEY_ID` | Kalshi API key id |
| `KALSHI_PRIVATE_KEY` / `KALSHI_PRIVATE_KEY_PATH` | RSA private key, PEM inline or file path |
| `MM_SERIES` | target series ticker |
| `MM_QTY`, `MM_INV_CAP`, `MM_MAX_STRIKES`, `MM_MAX_USD` | sizing / caps |
| `MM_SKEW`, `MM_KILL_LOSS`, `MM_ORDER_EXPIRY_S` | pricing / risk |

## Deploy

GitHub Actions (`.github/workflows/bot.yml`), manual dispatch only. Repository
secrets: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `MM_SERIES`.

> On a public repository, Actions run logs are public. The bot masks the target
> ticker in its output, but treat run logs as visible.

## Security

Keys are never committed — they are injected as repository secrets. `.gitignore`
excludes `*.pem`, `.env`, and databases.
