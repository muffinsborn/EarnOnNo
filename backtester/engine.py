"""Backtest engine: candidate discovery + event-driven portfolio simulation.

No lookahead: every candidate's eligibility and entry price come from
price_and_oi_as_of() evaluated at entry_ts (close_time minus the fixed
offset), which only ever reads candles with end_period_ts <= entry_ts. The
`result` column is read only for P&L after a position is already opened -
never for selection, ranking, or eligibility.
"""
import logging
import sqlite3

from kalshi_pipeline.timeutil import to_unix_ts

from .fees import taker_fee_dollars
from .pointintime import price_and_oi_as_of

logger = logging.getLogger("backtester")

PERIOD_INTERVAL = 60  # hourly candles


def _offset_hours_for_category(entry_offset_hours, category: str) -> float:
    """entry_offset_hours may be a single number (applied uniformly) or a
    dict of {category: hours} with a "_default" key for anything else -
    e.g. {"Sports": 0.5, "_default": 12} for a fast-moving-sports vs.
    slower-moving-everything-else split."""
    if isinstance(entry_offset_hours, dict):
        return entry_offset_hours.get(category, entry_offset_hours["_default"])
    return entry_offset_hours


def discover_candidates(
    conn: sqlite3.Connection,
    price_min: float,
    price_max: float,
    days_to_expiry: int,
    entry_offset_hours,
) -> list[dict]:
    """Walks every finalized, traded market and evaluates it at its own
    fixed entry point (close_time - entry_offset_hours(category)). Returns
    only markets that (a) existed that far before close, (b) have candle
    data reaching back that far, and (c) had a YES price within
    [price_min, price_max] at that point in time."""
    rows = conn.execute(
        """
        SELECT m.ticker, m.event_ticker, m.open_time, m.close_time, m.result, s.category
        FROM markets m
        LEFT JOIN events e ON e.event_ticker = m.event_ticker
        LEFT JOIN series s ON s.ticker = e.series_ticker
        WHERE m.status = 'finalized' AND m.volume > 0
        """
    ).fetchall()

    max_horizon_s = days_to_expiry * 86400

    candidates = []
    skipped_no_history, skipped_no_candle, skipped_price, skipped_bad_result = 0, 0, 0, 0

    for ticker, event_ticker, open_time, close_time, result, category in rows:
        close_ts = to_unix_ts(close_time)
        open_ts = to_unix_ts(open_time)
        if close_ts is None or open_ts is None:
            continue

        cat = category or "Uncategorized"
        entry_offset_s = _offset_hours_for_category(entry_offset_hours, cat) * 3600
        entry_ts = close_ts - entry_offset_s
        if entry_ts < open_ts:
            skipped_no_history += 1
            continue
        if close_ts - entry_ts > max_horizon_s:
            continue

        pit = price_and_oi_as_of(conn, ticker, PERIOD_INTERVAL, entry_ts)
        if pit is None:
            skipped_no_candle += 1
            continue
        yes_bid, yes_ask, open_interest = pit

        midpoint_yes = (yes_bid + yes_ask) / 2
        if not (price_min <= midpoint_yes <= price_max):
            skipped_price += 1
            continue

        entry_price_no = 1 - yes_bid
        if not (0 < entry_price_no < 1):
            continue

        if result not in ("yes", "no"):
            skipped_bad_result += 1
            continue

        candidates.append({
            "ticker": ticker,
            "category": category or "Uncategorized",
            "entry_ts": entry_ts,
            "close_ts": close_ts,
            "midpoint_yes": midpoint_yes,
            "entry_price_no": entry_price_no,
            "spread": yes_ask - yes_bid,
            "open_interest": open_interest,
            "result": result,
        })

    logger.info(
        "Discovered %d eligible candidates (skipped: %d too-young, %d no candle data yet, "
        "%d outside price band, %d unresolved/bad result).",
        len(candidates), skipped_no_history, skipped_no_candle, skipped_price, skipped_bad_result,
    )
    return candidates


def simulate_portfolio(
    candidates: list[dict],
    bankroll: float,
    position_pct: float,
    category_cap_pct: float,
    max_concurrent: int,
    slippage_pct_of_spread: float = 0.5,
) -> list[dict]:
    """Event-driven simulation ordered by entry time (ties broken by open
    interest descending, so contended portfolio slots go to the more liquid
    candidate - the concrete meaning of "rank by open interest" here).

    Slippage is modeled as a fraction of the quoted bid-ask spread, added on
    top of the quoted taker price (1 - yes_bid) to get the effective
    execution price. Because Kalshi's fee is itself a function of price
    (fee = 0.07 * contracts * price * (1-price)), slippage doesn't just
    add a flat cost - it also shifts the fee, which is what "slippage will
    bite into fees" means concretely here: fee is computed on the
    post-slippage price, not the quoted one."""
    position_dollars = bankroll * position_pct
    category_cap_dollars = bankroll * category_cap_pct

    ordered = sorted(candidates, key=lambda c: (c["entry_ts"], -c["open_interest"]))

    open_positions: list[dict] = []
    trades = []

    for cand in ordered:
        open_positions = [p for p in open_positions if p["close_ts"] > cand["entry_ts"]]

        if len(open_positions) >= max_concurrent:
            continue

        category_committed = sum(p["cost"] for p in open_positions if p["category"] == cand["category"])
        if category_committed + position_dollars > category_cap_dollars:
            continue

        quoted_price = cand["entry_price_no"]
        slippage_per_contract = cand["spread"] * slippage_pct_of_spread
        effective_price = min(0.99, quoted_price + slippage_per_contract)

        contracts = int(position_dollars // effective_price)
        if contracts <= 0:
            continue

        cost_before_fee = contracts * effective_price
        fee = taker_fee_dollars(contracts, effective_price)
        total_cost = cost_before_fee + fee

        payout = float(contracts) if cand["result"] == "no" else 0.0
        pnl = payout - total_cost

        trade = {
            **cand,
            "contracts": contracts,
            "effective_price_no": round(effective_price, 4),
            "slippage_cost": round(contracts * (effective_price - quoted_price), 4),
            "cost_before_fee": round(cost_before_fee, 4),
            "fee": fee,
            "total_cost": round(total_cost, 4),
            "payout": payout,
            "pnl": round(pnl, 4),
        }
        trades.append(trade)
        open_positions.append({"close_ts": cand["close_ts"], "category": cand["category"], "cost": total_cost})

    return trades
