"""SQLite schema and upsert helpers for the Kalshi data pipeline.

Tables
------
series          One row per Kalshi series (e.g. "Highest temp in NYC").
                Carries the `category` field (Politics, Sports, Economics, ...).
events          One row per event (an instance of a series, e.g. a specific
                week's jobs report). Links to `series` via series_ticker.
markets         One row per market (a single yes/no contract within an
                event). Links to `events` via event_ticker. Holds the final
                `result` (yes/no/'') once settled.
market_candles  Time series of OHLC price data per market, at whatever
                period_interval (minutes) the pipeline was run with.
                Query this for "price at open" (MIN end_period_ts) or
                "price ~24h before close" (row nearest close_time - 86400).

`market_overview` is a convenience VIEW joining markets -> events -> series
so you can query category/result/timing without writing the joins yourself.
"""
import os
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS series (
    ticker      TEXT PRIMARY KEY,
    category    TEXT,
    title       TEXT,
    frequency   TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker    TEXT PRIMARY KEY,
    series_ticker    TEXT REFERENCES series(ticker),
    title           TEXT,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_series_ticker ON events(series_ticker);

CREATE TABLE IF NOT EXISTS markets (
    ticker              TEXT PRIMARY KEY,
    event_ticker        TEXT REFERENCES events(event_ticker),
    title               TEXT,
    status              TEXT,
    result              TEXT,
    open_time           TEXT,
    close_time          TEXT,
    settlement_ts       TEXT,
    volume              INTEGER,
    open_interest       INTEGER,
    last_price_dollars  REAL,
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_event_ticker ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_close_time ON markets(close_time);

CREATE TABLE IF NOT EXISTS market_candles (
    ticker           TEXT NOT NULL REFERENCES markets(ticker),
    period_interval  INTEGER NOT NULL,
    end_period_ts    INTEGER NOT NULL,
    yes_bid_close    REAL,
    yes_ask_close    REAL,
    price_close      REAL,
    volume           INTEGER,
    open_interest    INTEGER,
    PRIMARY KEY (ticker, period_interval, end_period_ts)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key    TEXT PRIMARY KEY,
    cursor TEXT
);

CREATE VIEW IF NOT EXISTS market_overview AS
SELECT
    m.ticker,
    m.title,
    s.category,
    m.status,
    m.result,
    m.open_time,
    m.close_time,
    m.volume,
    m.open_interest,
    m.last_price_dollars
FROM markets m
LEFT JOIN events e ON e.event_ticker = m.event_ticker
LEFT JOIN series s ON s.ticker = e.series_ticker;
"""


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Not enforced: Kalshi's own API data isn't guaranteed referentially
    # consistent (e.g. events can reference a series_ticker that's since
    # been filtered out of /series), and we'd rather keep every row the API
    # gives us than silently drop children of a missing parent.
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def upsert_series(conn: sqlite3.Connection, rows: list[dict], fetched_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO series (ticker, category, title, frequency, fetched_at)
        VALUES (:ticker, :category, :title, :frequency, :fetched_at)
        ON CONFLICT(ticker) DO UPDATE SET
            category=excluded.category,
            title=excluded.title,
            frequency=excluded.frequency,
            fetched_at=excluded.fetched_at
        """,
        [
            {
                "ticker": r["ticker"],
                "category": r.get("category"),
                "title": r.get("title"),
                "frequency": r.get("frequency"),
                "fetched_at": fetched_at,
            }
            for r in rows
        ],
    )
    conn.commit()


def upsert_events(conn: sqlite3.Connection, rows: list[dict], fetched_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO events (event_ticker, series_ticker, title, fetched_at)
        VALUES (:event_ticker, :series_ticker, :title, :fetched_at)
        ON CONFLICT(event_ticker) DO UPDATE SET
            series_ticker=excluded.series_ticker,
            title=excluded.title,
            fetched_at=excluded.fetched_at
        """,
        [
            {
                "event_ticker": r["event_ticker"],
                "series_ticker": r.get("series_ticker"),
                "title": r.get("title"),
                "fetched_at": fetched_at,
            }
            for r in rows
        ],
    )
    conn.commit()


def upsert_markets(conn: sqlite3.Connection, rows: list[dict], fetched_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO markets (
            ticker, event_ticker, title, status, result, open_time, close_time,
            settlement_ts, volume, open_interest, last_price_dollars, fetched_at
        )
        VALUES (
            :ticker, :event_ticker, :title, :status, :result, :open_time, :close_time,
            :settlement_ts, :volume, :open_interest, :last_price_dollars, :fetched_at
        )
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker,
            title=excluded.title,
            status=excluded.status,
            result=excluded.result,
            open_time=excluded.open_time,
            close_time=excluded.close_time,
            settlement_ts=excluded.settlement_ts,
            volume=excluded.volume,
            open_interest=excluded.open_interest,
            last_price_dollars=excluded.last_price_dollars,
            fetched_at=excluded.fetched_at
        """,
        [
            {
                "ticker": r["ticker"],
                "event_ticker": r.get("event_ticker"),
                "title": r.get("title") or r.get("yes_sub_title"),
                "status": r.get("status"),
                "result": r.get("result"),
                "open_time": r.get("open_time"),
                "close_time": r.get("close_time"),
                "settlement_ts": r.get("settlement_ts") or r.get("settled_time"),
                "volume": int(float(r["volume_fp"])) if r.get("volume_fp") is not None else None,
                "open_interest": int(float(r["open_interest_fp"])) if r.get("open_interest_fp") is not None else None,
                "last_price_dollars": float(r["last_price_dollars"]) if r.get("last_price_dollars") is not None else None,
                "fetched_at": fetched_at,
            }
            for r in rows
        ],
    )
    conn.commit()


def upsert_candles(conn: sqlite3.Connection, ticker: str, period_interval: int, rows: list[dict]) -> None:
    def _dollars(candle: dict, key: str):
        block = candle.get(key) or {}
        return block.get("close_dollars")

    conn.executemany(
        """
        INSERT INTO market_candles (
            ticker, period_interval, end_period_ts, yes_bid_close, yes_ask_close,
            price_close, volume, open_interest
        )
        VALUES (:ticker, :period_interval, :end_period_ts, :yes_bid_close, :yes_ask_close,
                :price_close, :volume, :open_interest)
        ON CONFLICT(ticker, period_interval, end_period_ts) DO UPDATE SET
            yes_bid_close=excluded.yes_bid_close,
            yes_ask_close=excluded.yes_ask_close,
            price_close=excluded.price_close,
            volume=excluded.volume,
            open_interest=excluded.open_interest
        """,
        [
            {
                "ticker": ticker,
                "period_interval": period_interval,
                "end_period_ts": c["end_period_ts"],
                "yes_bid_close": _dollars(c, "yes_bid"),
                "yes_ask_close": _dollars(c, "yes_ask"),
                "price_close": _dollars(c, "price"),
                "volume": int(float(c["volume_fp"])) if c.get("volume_fp") is not None else None,
                "open_interest": int(float(c["open_interest_fp"])) if c.get("open_interest_fp") is not None else None,
            }
            for c in rows
        ],
    )
    conn.commit()


def get_sync_cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("select cursor from sync_state where key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_sync_cursor(conn: sqlite3.Connection, key: str, cursor: str | None) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, cursor) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor",
        (key, cursor),
    )
    conn.commit()


def clear_sync_cursor(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    conn.commit()
