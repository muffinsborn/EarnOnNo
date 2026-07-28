# EarnOnNo — Kalshi Data Pipeline (Phase 1)

Phase 1 of a systematic "bet NO on low-priced YES" strategy: this is the
**data pipeline only**. It is read-only — no order placement, no signal
generation, no backtesting logic lives here yet.

It pulls from the Kalshi API into a local SQLite database:
- All series (with category) and events (linking markets to series)
- All resolved (`settled`) markets, with final result
- All currently open markets
- Hourly price candlesticks for every market pulled

## 1. Get your Kalshi API key

1. Log in at https://kalshi.com and go to **Profile Settings** → https://kalshi.com/account/profile
2. Find **API Keys** → click **Create New API Key**
3. Kalshi shows you two things **once** — save both immediately:
   - **Key ID**
   - **Private Key** (auto-downloaded as a `.txt`/`.key` file, RSA format)
4. Move the downloaded private key file somewhere **outside this repo**, e.g. `~/secrets/kalshi_private_key.txt`.

Every request (even read-only market data) must be signed with this key —
there's no anonymous tier.

## 2. Set up `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in:
```
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.txt
```

`.env` is already in `.gitignore` — verify with:
```bash
git check-ignore -v .env
```
Expected output: a line showing `.gitignore:1:.env` matched `.env`. If you see
no output, **stop** and tell me before adding real keys — it means `.env`
isn't actually ignored.

## 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Verify credentials with a small test run

```bash
python pipeline.py --max-markets 20 --skip-candles
```

Expected output: log lines like
```
... INFO kalshi_pipeline: Fetching series list...
... INFO kalshi_pipeline: Stored 143 series.
... INFO kalshi_pipeline: Fetching events (for series linkage)...
... INFO kalshi_pipeline: Stored 1042 events.
... INFO kalshi_pipeline: Fetching resolved markets (status=settled)...
... INFO kalshi_pipeline: Stored 20 resolved markets.
... INFO kalshi_pipeline: Fetching open markets (status=open)...
... INFO kalshi_pipeline: Stored 20 open markets.
... INFO kalshi_pipeline: Done in ...s.
```
If instead you see a `KalshiAPIError` with a 401/403 status, your key ID or
private key path is wrong — double check `.env`.

Sanity-check the data landed:
```bash
sqlite3 data/kalshi.db "select ticker, status, result from markets limit 5;"
sqlite3 data/kalshi.db "select category, count(*) from market_overview group by category;"
```

## 5. Full run

Once the small run looks right, pull everything (this will take a while —
it fetches every resolved market plus hourly candlesticks for each one):
```bash
python pipeline.py
```

Useful flags:
- `--skip-candles` — metadata only, much faster
- `--max-markets N` — cap how many resolved/open markets to pull (testing)
- `--resolved-only` / `--open-only` — pull just one side
- `--candle-interval {1,60,1440}` — candlestick granularity in minutes (default 60/hourly)

## Schema

See docstring at the top of `kalshi_pipeline/db.py` for full column docs.
Quick reference:
- `series` — category, title, per Kalshi series
- `events` — links markets to series via `series_ticker`
- `markets` — one row per market: status, final `result`, volume, open interest, timestamps
- `market_candles` — OHLC price time series per market (query for "price at open" / "price ~24h before close")
- `market_overview` — a VIEW joining the three above for easy querying

Example: YES price ~24h before close for a settled market
```sql
SELECT price_close, end_period_ts
FROM market_candles
WHERE ticker = 'SOME-TICKER'
ORDER BY ABS(end_period_ts - (strftime('%s', (SELECT close_time FROM markets WHERE ticker='SOME-TICKER')) - 86400))
LIMIT 1;
```

## What's NOT in this phase

No order placement, no signal generation, no backtester. That's phases 2-4.
