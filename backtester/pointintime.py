"""Point-in-time data access - the core anti-lookahead mechanism.

Every function here answers "what did we know as of unix timestamp T?" by
querying for the candle row with the largest end_period_ts <= T. Nothing in
this module (or anywhere in the engine) may read a candle with
end_period_ts > T, or the `result` column, before a position's close_ts.
"""
import sqlite3


def price_and_oi_as_of(conn: sqlite3.Connection, ticker: str, period_interval: int, as_of_ts: int):
    """Returns (yes_bid, yes_ask, open_interest) from the latest candle at or
    before as_of_ts, or None if no such candle exists (candles not yet
    fetched for this ticker that far back, or the market didn't exist yet)."""
    row = conn.execute(
        """
        SELECT yes_bid_close, yes_ask_close, open_interest
        FROM market_candles
        WHERE ticker = ? AND period_interval = ? AND end_period_ts <= ?
        ORDER BY end_period_ts DESC
        LIMIT 1
        """,
        (ticker, period_interval, as_of_ts),
    ).fetchone()
    if row is None:
        return None
    yes_bid, yes_ask, open_interest = row
    if yes_bid is None or yes_ask is None:
        return None
    return yes_bid, yes_ask, (open_interest or 0)


def price_path(conn: sqlite3.Connection, ticker: str, period_interval: int, start_ts: int, end_ts: int):
    """Returns (end_period_ts, yes_bid_close, yes_ask_close) rows strictly
    after start_ts up to and including end_ts, in ascending time order.

    This is used to walk a position's OWN price history forward after it has
    already been opened (e.g. for stop-loss timing), the same way `result` is
    read only after entry to compute P&L. It must never be used to decide
    whether to admit a candidate at its own entry_ts."""
    return conn.execute(
        """
        SELECT end_period_ts, yes_bid_close, yes_ask_close
        FROM market_candles
        WHERE ticker = ? AND period_interval = ? AND end_period_ts > ? AND end_period_ts <= ?
        ORDER BY end_period_ts ASC
        """,
        (ticker, period_interval, start_ts, end_ts),
    ).fetchall()
