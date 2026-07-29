"""Kalshi data pipeline - Phase 1: read-only historical + current market data.

Populates a local SQLite database (see kalshi_pipeline/db.py for schema docs)
with:
  - series metadata (category, title)
  - event metadata (links markets -> series)
  - resolved (settled) markets, with final result
  - currently open markets
  - price candlesticks for each market, at --candle-interval minutes

No order placement or trading logic lives here - every API call is a GET.

Usage:
    python pipeline.py                          # full run, everything available
    python pipeline.py --skip-candles           # metadata only, fast, good first check
    python pipeline.py --max-markets 20         # small test run
    python pipeline.py --resolved-only          # skip open markets
    python pipeline.py --open-only              # skip resolved markets
    python pipeline.py --resync-events          # force a full /events re-pull (normally skipped once populated)

Safe to interrupt and re-run: every write is an idempotent upsert, and the
one-time bulk /events pull (the slowest phase) is skipped automatically once
events already exist locally, so a restart goes straight to markets/candles.
"""
import argparse
import logging
import time
from datetime import datetime, timezone

from kalshi_pipeline import db
from kalshi_pipeline.client import KalshiAPIError, KalshiClient
from kalshi_pipeline.config import ConfigError, load_config
from kalshi_pipeline.timeutil import now_unix_ts, to_unix_ts

logger = logging.getLogger("kalshi_pipeline")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_series(client: KalshiClient, conn) -> None:
    logger.info("Fetching series list...")
    series = client.get_series_list()
    db.upsert_series(conn, series, _now_iso())
    logger.info("Stored %d series.", len(series))


def sync_events(client: KalshiClient, conn) -> dict:
    """Returns event_ticker -> series_ticker map, needed later for candlesticks."""
    logger.info("Fetching events (for series linkage)...")
    event_to_series = {}
    batch = []
    count = 0
    for event in client.iter_events():
        event_to_series[event["event_ticker"]] = event.get("series_ticker")
        batch.append(event)
        count += 1
        if len(batch) >= 500:
            db.upsert_events(conn, batch, _now_iso())
            batch = []
            logger.info("...%d events fetched so far", count)
    if batch:
        db.upsert_events(conn, batch, _now_iso())
    logger.info("Stored %d events.", count)
    return event_to_series


def load_event_to_series(conn) -> dict:
    return {
        row[0]: row[1]
        for row in conn.execute("select event_ticker, series_ticker from events").fetchall()
    }


def backfill_missing_events(client: KalshiClient, conn, markets: list[dict], event_to_series: dict) -> None:
    """Some event types (e.g. multivariate-event combo markets) don't appear
    in the bulk /events listing at all, even paginated to completion, but do
    exist via the single-event endpoint. Fetch those individually so their
    markets still resolve a category."""
    missing = sorted({
        m["event_ticker"] for m in markets
        if m.get("event_ticker") and m["event_ticker"] not in event_to_series
    })
    if not missing:
        return
    logger.info("Backfilling %d events missing from the bulk /events listing...", len(missing))
    fetched = []
    for i, event_ticker in enumerate(missing, start=1):
        try:
            event = client.get_event(event_ticker)
        except KalshiAPIError as exc:
            logger.warning("Could not backfill event %s: %s", event_ticker, exc)
            continue
        if not event:
            continue
        event_to_series[event["event_ticker"]] = event.get("series_ticker")
        fetched.append(event)
        if i % 100 == 0 or i == len(missing):
            logger.info("...backfilled %d/%d missing events", i, len(missing))
    if fetched:
        db.upsert_events(conn, fetched, _now_iso())
    logger.info("Backfilled %d/%d missing events.", len(fetched), len(missing))


DONE_MARKER = "DONE"


def sync_markets(
    client: KalshiClient, conn, status: str, label: str, max_markets: int | None, force_resync: bool = False
) -> None:
    """Paginates /markets, checkpointing the cursor after every page so a
    restart resumes instead of re-walking from page 1 (this listing alone
    can be millions of rows - re-walking it on every restart in a session
    that recycles every few minutes would never finish).

    Once a pass completes naturally (cursor exhausted), the checkpoint is set
    to DONE_MARKER rather than cleared, so a later invocation of this script
    knows the listing is already complete and doesn't re-walk it from page 1
    for no reason - only an in-progress state should trigger a resume, and
    only --resync-markets/force_resync should trigger a full do-over."""
    checkpoint_key = f"markets_cursor:{status}"
    state = db.get_sync_cursor(conn, checkpoint_key)

    if state == DONE_MARKER and not force_resync and not max_markets:
        logger.info(
            "Skipping %s markets sync - already fully listed in a previous run "
            "(pass --resync-markets to force a full refresh).", label,
        )
        return

    start_cursor = state if state and state != DONE_MARKER and not force_resync else None
    if start_cursor:
        logger.info("Resuming %s markets sync from a saved checkpoint...", label)
    logger.info("Fetching %s markets (status=%s)...", label, status)

    count = 0
    hit_cap = False
    for markets, cursor in client.iter_market_pages(status=status, start_cursor=start_cursor):
        if markets:
            db.upsert_markets(conn, markets, _now_iso())
            count += len(markets)
        db.set_sync_cursor(conn, checkpoint_key, cursor)
        logger.info("...pulled %d %s markets so far (checkpointed)", count, label)
        if max_markets and count >= max_markets:
            hit_cap = True
            break
        if not cursor or not markets:
            break

    if not hit_cap:
        db.set_sync_cursor(conn, checkpoint_key, DONE_MARKER)
    logger.info("Stored %d %s markets this run.", count, label)


def load_markets_for_candles(conn, since_ts: int | None = None) -> list[dict]:
    """Pulls the market set straight from the DB (cumulative across every run
    to date) rather than relying on any single run's in-memory list, since a
    resumed sync_markets run only yields the pages it fetched THIS time -
    markets stored by earlier, interrupted runs still need candles.

    Excludes zero-volume markets: the resolved-market listing turned out to
    be dominated by millions of auto-generated, near-instant sub-markets
    (e.g. 15-minute crypto strike ladders) with no trades at all - an
    untraded market has no meaningful YES price series to backtest against,
    and pulling hourly candles for every one of them isn't tractable (would
    take weeks of API calls for no analytical value). Metadata/resolution is
    still kept for every market regardless.

    since_ts, if set, further limits candle-fetching to markets that closed
    on/after that unix timestamp, or are still active (open markets always
    qualify - they're current by definition). Metadata for older markets is
    untouched; this only scopes which markets get the (much more expensive)
    price time series pulled."""
    cols = ["ticker", "event_ticker", "status", "open_time", "close_time"]
    query = f"select {', '.join(cols)} from markets where volume is not null and volume > 0"
    params: list = []
    if since_ts is not None:
        query += " and (status = 'active' or CAST(strftime('%s', close_time) AS INTEGER) >= ?)"
        params.append(since_ts)
    rows = conn.execute(query, params).fetchall()
    return [dict(zip(cols, row)) for row in rows]


def sync_candles(
    client: KalshiClient,
    conn,
    markets: list[dict],
    event_to_series: dict,
    period_interval: int,
) -> None:
    total = len(markets)
    logger.info("Fetching %d-minute candlesticks for %d markets...", period_interval, total)
    now_ts = now_unix_ts()

    # A finalized market's price history never changes, so if we already have
    # candles for it (e.g. from a run interrupted partway through), skip it -
    # this is what makes restarts cheap instead of re-pulling everything.
    already_done = {
        row[0] for row in conn.execute(
            "select distinct ticker from market_candles where period_interval = ?", (period_interval,)
        ).fetchall()
    }
    skipped = 0

    for i, market in enumerate(markets, start=1):
        ticker = market["ticker"]
        if ticker in already_done and market.get("status") == "finalized":
            skipped += 1
            continue
        series_ticker = event_to_series.get(market.get("event_ticker"))
        if not series_ticker:
            logger.warning("Skipping candles for %s: no series_ticker found via event_ticker=%s",
                            ticker, market.get("event_ticker"))
            continue

        start_ts = to_unix_ts(market.get("open_time"))
        end_ts = to_unix_ts(market.get("close_time")) or now_ts
        if start_ts is None:
            logger.warning("Skipping candles for %s: no open_time", ticker)
            continue
        end_ts = min(end_ts, now_ts)
        if end_ts <= start_ts:
            continue

        try:
            candles = list(client.iter_candlesticks(series_ticker, ticker, start_ts, end_ts, period_interval))
        except KalshiAPIError as exc:
            logger.error("Failed to fetch candles for %s: %s", ticker, exc)
            continue

        if candles:
            db.upsert_candles(conn, ticker, period_interval, candles)

        if i % 100 == 0 or i == total:
            logger.info("...candles pulled for %d/%d markets (%d already done, skipped)", i, total, skipped)

    logger.info("Candles done: %d fetched, %d already had data and were skipped.", total - skipped, skipped)


def main():
    parser = argparse.ArgumentParser(description="Kalshi data pipeline (Phase 1: read-only)")
    parser.add_argument("--db", help="Override KALSHI_DB_PATH from .env")
    parser.add_argument("--skip-candles", action="store_true", help="Skip candlestick time-series pull (fast, metadata only)")
    parser.add_argument("--candle-interval", type=int, default=60, choices=[1, 60, 1440],
                         help="Candlestick period in minutes (default 60 = hourly)")
    parser.add_argument("--max-markets", type=int, default=None,
                         help="Cap number of resolved/open markets pulled - use for a quick test run")
    parser.add_argument("--resolved-only", action="store_true", help="Only pull resolved (settled) markets")
    parser.add_argument("--open-only", action="store_true", help="Only pull currently open markets")
    parser.add_argument("--resync-markets", action="store_true",
                         help="Force a full re-walk of /markets even if a previous run already completed it "
                              "(by default, a completed listing is skipped entirely on later runs - open markets "
                              "change constantly so you'll want this periodically to pick up newly-settled ones)")
    parser.add_argument("--resync-events", action="store_true",
                         help="Force a full re-pull of the bulk /events listing even if we already have events "
                              "stored (by default, once events exist locally we skip this ~5-6 min bulk pull and "
                              "rely on per-market backfill for anything missing - much more resilient to restarts)")
    parser.add_argument("--since-days", type=int, default=30,
                         help="Only fetch candles for markets that closed within this many days, plus any "
                              "currently active market (default 30). Metadata/resolution is still pulled for "
                              "every market regardless - this only scopes the price time series. Kalshi's settled "
                              "market history via this listing only spans ~90 days back in practice, so anything "
                              "90+ is equivalent to unlimited. Use 0 for no limit.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    db_path = args.db or config.db_path
    conn = db.connect(db_path)
    db.init_schema(conn)
    logger.info("Using database: %s", db_path)

    client = KalshiClient(config)

    start = time.monotonic()
    sync_series(client, conn)

    existing_event_count = conn.execute("select count(*) from events").fetchone()[0]
    if args.resync_events or existing_event_count == 0:
        event_to_series = sync_events(client, conn)
    else:
        logger.info(
            "Skipping bulk /events resync - %d events already stored locally "
            "(pass --resync-events to force a full refresh).",
            existing_event_count,
        )
        event_to_series = load_event_to_series(conn)

    if not args.open_only:
        sync_markets(client, conn, status="settled", label="resolved", max_markets=args.max_markets,
                     force_resync=args.resync_markets)
    if not args.resolved_only:
        sync_markets(client, conn, status="open", label="open", max_markets=args.max_markets,
                     force_resync=args.resync_markets)

    since_ts = now_unix_ts() - args.since_days * 86400 if args.since_days > 0 else None
    all_markets = load_markets_for_candles(conn, since_ts=since_ts)
    logger.info("%d markets qualify for candles (since_days=%s).", len(all_markets), args.since_days or "unlimited")
    backfill_missing_events(client, conn, all_markets, event_to_series)

    if not args.skip_candles:
        sync_candles(client, conn, all_markets, event_to_series, args.candle_interval)
    else:
        logger.info("Skipping candlestick pull (--skip-candles).")

    conn.close()
    logger.info("Done in %.1fs.", time.monotonic() - start)


if __name__ == "__main__":
    main()
