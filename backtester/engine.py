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
from .pointintime import price_and_oi_as_of, price_path

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
    categories: set[str] | None = None,
) -> list[dict]:
    """Walks every finalized, traded market and evaluates it at its own
    fixed entry point (close_time - entry_offset_hours(category)). Returns
    only markets that (a) existed that far before close, (b) have candle
    data reaching back that far, and (c) had a YES price within
    [price_min, price_max] at that point in time.

    categories, if given, restricts to just those category names (matched
    against the same "Uncategorized" fallback used everywhere else) -
    filtered before the point-in-time candle lookup so excluded markets
    don't cost a query."""
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
    skipped_no_history, skipped_too_long, skipped_no_candle, skipped_price, skipped_bad_result = 0, 0, 0, 0, 0

    for ticker, event_ticker, open_time, close_time, result, category in rows:
        close_ts = to_unix_ts(close_time)
        open_ts = to_unix_ts(open_time)
        if close_ts is None or open_ts is None:
            continue

        # days_to_expiry bounds the market's own total lifetime (open to
        # close), not the entry offset - entry_ts is close_ts minus a fixed
        # few-hour category offset, so close_ts - entry_ts is always just
        # that offset and can never exceed a multi-day horizon regardless
        # of days_to_expiry's value.
        if close_ts - open_ts > max_horizon_s:
            skipped_too_long += 1
            continue

        cat = category or "Uncategorized"
        if categories is not None and cat not in categories:
            continue
        entry_offset_s = _offset_hours_for_category(entry_offset_hours, cat) * 3600
        entry_ts = close_ts - entry_offset_s
        if entry_ts < open_ts:
            skipped_no_history += 1
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
        "Discovered %d eligible candidates (skipped: %d too-long-dated, %d too-young, %d no candle data yet, "
        "%d outside price band, %d unresolved/bad result).",
        len(candidates), skipped_too_long, skipped_no_history, skipped_no_candle, skipped_price, skipped_bad_result,
    )
    return candidates


def _resolve_position_exit(
    conn: sqlite3.Connection,
    cand: dict,
    effective_price_no: float,
    stop_loss_pct: float | None,
    exit_cache: dict | None,
):
    """Determines when/how a position that was just opened actually leaves
    the portfolio: either stopped out early on a mark-to-market drawdown, or
    held to its own resolution.

    Reads candles strictly after entry_ts - this is the position's own
    future, read only to time ITS exit/P&L, exactly like `result` is read
    only after entry. It never affects which candidates get admitted at
    their own entry_ts (see simulate_portfolio).

    "Drawdown" is mark-to-market against what the position could be sold
    for right now - the NO bid, i.e. 1 - yes_ask - compared to what it cost
    to enter. Cached by ticker since, within one discover_candidates() call,
    a given ticker's entry price/path is identical across sweep runs that
    only vary category_cap_pct."""
    cache_key = cand["ticker"]
    if exit_cache is not None and cache_key in exit_cache:
        return exit_cache[cache_key]

    result_exit_ts, result_exit_price, result_reason = cand["close_ts"], None, "resolution"
    if stop_loss_pct:
        for end_ts, _yes_bid, yes_ask in price_path(conn, cand["ticker"], PERIOD_INTERVAL, cand["entry_ts"], cand["close_ts"]):
            if yes_ask is None:
                continue
            mark_price_no = 1 - yes_ask
            drawdown = 1 - (mark_price_no / effective_price_no)
            if drawdown >= stop_loss_pct:
                result_exit_ts, result_exit_price, result_reason = end_ts, mark_price_no, "stop_loss"
                break

    if result_reason == "resolution":
        result_exit_price = 1.0 if cand["result"] == "no" else 0.0

    outcome = (result_exit_ts, result_exit_price, result_reason)
    if exit_cache is not None:
        exit_cache[cache_key] = outcome
    return outcome


def simulate_portfolio(
    candidates: list[dict],
    bankroll: float,
    position_pct: float,
    category_cap_pct: float,
    max_concurrent: int,
    slippage_pct_of_spread: float = 0.5,
    conn: sqlite3.Connection | None = None,
    stop_loss_pct: float | None = 0.25,
    exit_cache: dict | None = None,
    position_dollars_fixed: float | None = None,
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
    post-slippage price, not the quoted one.

    Capital guard: available capital is tracked as starting bankroll, plus
    P&L realized from positions that have actually exited by the current
    entry_ts, minus cost committed to positions still open at that instant.
    A new position is only opened if its full cost fits within that - this
    makes bankroll exhaustion structurally impossible rather than merely
    checked after the fact (each position's worst-case loss is its own
    committed cost, which was already capital-checked at entry).

    Stop-loss: if stop_loss_pct is set (default 0.25 = 25%), a position
    whose mark-to-market value ever falls that far below its entry cost is
    exited then (at that mark price, minus the same taker fee formula
    applied to the exit trade) instead of held to resolution, freeing its
    committed capital for redeployment at that point in time. Pass
    stop_loss_pct=None to disable and hold every position to resolution.

    position_dollars_fixed, if given, overrides position_pct with a flat
    dollar amount per position (e.g. $200) that does not scale with
    bankroll or with category_cap_pct - use this to isolate sizing as a
    variable independent of both."""
    if stop_loss_pct and conn is None:
        raise ValueError("stop_loss_pct requires conn (needs the position's own future price path)")

    position_dollars = position_dollars_fixed if position_dollars_fixed is not None else bankroll * position_pct
    category_cap_dollars = bankroll * category_cap_pct

    ordered = sorted(candidates, key=lambda c: (c["entry_ts"], -c["open_interest"]))

    open_positions: list[dict] = []
    trades = []
    realized_pnl = 0.0

    for cand in ordered:
        still_open = []
        for p in open_positions:
            if p["exit_ts"] > cand["entry_ts"]:
                still_open.append(p)
            else:
                realized_pnl += p["pnl"]
        open_positions = still_open

        if len(open_positions) >= max_concurrent:
            continue

        category_committed = sum(p["cost"] for p in open_positions if p["category"] == cand["category"])
        if category_committed + position_dollars > category_cap_dollars:
            continue

        capital_committed = sum(p["cost"] for p in open_positions)
        available_capital = bankroll + realized_pnl - capital_committed
        if position_dollars > available_capital:
            continue

        quoted_price = cand["entry_price_no"]
        slippage_per_contract = cand["spread"] * slippage_pct_of_spread
        effective_price = min(0.99, quoted_price + slippage_per_contract)

        contracts = int(position_dollars // effective_price)
        if contracts <= 0:
            continue

        cost_before_fee = contracts * effective_price
        entry_fee = taker_fee_dollars(contracts, effective_price)
        total_cost = cost_before_fee + entry_fee
        if total_cost > available_capital:
            continue

        exit_ts, exit_price_no, exit_reason = _resolve_position_exit(
            conn, cand, effective_price, stop_loss_pct, exit_cache
        )

        proceeds_before_fee = contracts * exit_price_no
        exit_fee = taker_fee_dollars(contracts, exit_price_no) if exit_reason == "stop_loss" else 0.0
        proceeds = proceeds_before_fee - exit_fee
        pnl = proceeds - total_cost

        trade = {
            **cand,
            "contracts": contracts,
            "effective_price_no": round(effective_price, 4),
            "slippage_cost": round(contracts * (effective_price - quoted_price), 4),
            "cost_before_fee": round(cost_before_fee, 4),
            "entry_fee": entry_fee,
            "fee": entry_fee + exit_fee,  # kept for report.py's existing "total fees" summary
            "total_cost": round(total_cost, 4),
            "exit_ts": exit_ts,
            "exit_reason": exit_reason,
            "exit_price_no": round(exit_price_no, 4),
            "exit_fee": exit_fee,
            "payout": round(proceeds_before_fee, 4),
            "proceeds": round(proceeds, 4),
            "pnl": round(pnl, 4),
        }
        trades.append(trade)
        open_positions.append({
            "exit_ts": exit_ts, "category": cand["category"], "cost": total_cost, "pnl": pnl,
        })

    return trades
