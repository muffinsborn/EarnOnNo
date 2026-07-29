"""Kalshi NO-on-longshots backtest - parameter sweep mode.

Runs the same no-lookahead simulation as backtest.py across a grid of
(price-max, days-to-expiry, category-cap) combinations in one batch and
writes a single comparison CSV - one row per combination - so results can
be sorted/filtered without re-running anything.

discover_candidates() only depends on price_min/price_max/days_to_expiry/
entry-offsets, not on category_cap_pct, so it's computed once per unique
(price_max, days_to_expiry) pair and reused across every category_cap_pct
value in the grid. A position's stop-loss exit is likewise cached by
ticker (backtester/engine.py's exit_cache) across every category_cap_pct
variant of the same candidate pool, since neither the stop-loss check nor
the entry price it depends on changes with the category cap.

Usage:
    python sweep.py
    python sweep.py --price-max-grid 0.03,0.05,0.08 --days-to-expiry-grid 7,14
    python sweep.py --out-dir results/2026-07-29
"""
import argparse
import itertools
import logging
import os
import sqlite3
import time

from backtester.engine import discover_candidates, simulate_portfolio
from backtester.report import drawdown_analysis, stop_loss_exit_count, summarize, validate_bankroll_never_exhausted

logger = logging.getLogger("backtester.sweep")


def _parse_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _parse_ints(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Kalshi NO-on-longshots backtest sweep (grid search, no lookahead)")
    parser.add_argument("--db", default="data/kalshi.db", help="Path to the SQLite DB built by pipeline.py")
    parser.add_argument("--price-min", type=float, default=0.025, help="Min YES price (fixed across the sweep)")
    parser.add_argument("--price-max-grid", default="0.03,0.05,0.08,0.10,0.15",
                         help="Comma-separated YES price upper bounds to sweep (fractions, e.g. 0.03 = 3c)")
    parser.add_argument("--days-to-expiry-grid", default="3,7,14",
                         help="Comma-separated max days-to-expiry values to sweep")
    parser.add_argument("--category-cap-grid", default="0.20,0.30,0.40",
                         help="Comma-separated category-cap fractions to sweep")
    parser.add_argument("--entry-offset-hours-sports", type=float, default=0.5)
    parser.add_argument("--entry-offset-hours-other", type=float, default=12)
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--position-pct", type=float, default=0.02)
    parser.add_argument("--max-concurrent", type=int, default=25)
    parser.add_argument("--slippage-pct-of-spread", type=float, default=0.5)
    parser.add_argument("--stop-loss-pct", type=float, default=0.25,
                         help="Per-position stop-loss (default 0.25 = 25%%). Pass 0 to disable.")
    parser.add_argument("--out-dir", default="backtest_results", help="Directory for the sweep results CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not os.path.isfile(args.db):
        raise SystemExit(f"Database not found at {args.db} - run pipeline.py first (Phase 1).")

    price_max_grid = _parse_floats(args.price_max_grid)
    days_grid = _parse_ints(args.days_to_expiry_grid)
    cap_grid = _parse_floats(args.category_cap_grid)
    stop_loss_pct = args.stop_loss_pct or None

    conn = sqlite3.connect(args.db, timeout=30)
    entry_offset_hours = {"Sports": args.entry_offset_hours_sports, "_default": args.entry_offset_hours_other}

    rows = []
    total_combos = len(price_max_grid) * len(days_grid) * len(cap_grid)
    combo_i = 0
    t_start = time.time()

    for price_max, days_to_expiry in itertools.product(price_max_grid, days_grid):
        candidates = discover_candidates(
            conn,
            price_min=args.price_min,
            price_max=price_max,
            days_to_expiry=days_to_expiry,
            entry_offset_hours=entry_offset_hours,
        )
        exit_cache: dict = {}

        for category_cap_pct in cap_grid:
            combo_i += 1
            trades = simulate_portfolio(
                candidates,
                bankroll=args.bankroll,
                position_pct=args.position_pct,
                category_cap_pct=category_cap_pct,
                max_concurrent=args.max_concurrent,
                slippage_pct_of_spread=args.slippage_pct_of_spread,
                conn=conn,
                stop_loss_pct=stop_loss_pct,
                exit_cache=exit_cache,
            )
            stats = summarize(trades)
            dd = drawdown_analysis(trades, bankroll=args.bankroll)
            bankroll_check = validate_bankroll_never_exhausted(trades, bankroll=args.bankroll)

            rows.append({
                "price_max_cents": round(price_max * 100, 2),
                "days_to_expiry": days_to_expiry,
                "category_cap_pct": round(category_cap_pct * 100, 1),
                "trades": stats.get("total_trades", 0),
                "win_rate_pct": round(stats["win_rate"] * 100, 2) if stats.get("total_trades") else None,
                "total_pnl": stats.get("total_pnl"),
                "roi_pct": stats.get("roi_pct"),
                "total_fees": stats.get("total_fees"),
                "max_drawdown_dollars": dd["max_drawdown_dollars"],
                "max_drawdown_pct_of_bankroll": dd.get("max_drawdown_pct_of_bankroll"),
                "sharpe_like_per_trade": stats.get("sharpe_like_per_trade"),
                "stop_loss_exits": stop_loss_exit_count(trades),
                "bankroll_exhausted": bankroll_check["exhausted"],
                "min_running_equity": bankroll_check["min_equity_dollars"],
            })
            logger.info(
                "[%d/%d] price_max=%.1fc days=%d cap=%.0f%%: %d trades, P&L $%s, win %s%%",
                combo_i, total_combos, price_max * 100, days_to_expiry, category_cap_pct * 100,
                stats.get("total_trades", 0), stats.get("total_pnl"),
                round(stats["win_rate"] * 100, 1) if stats.get("total_trades") else "n/a",
            )

    conn.close()

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "sweep_results.csv")
    fieldnames = list(rows[0].keys()) if rows else []
    import csv as _csv
    with open(csv_path, "w", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r["price_max_cents"], r["days_to_expiry"], r["category_cap_pct"])):
            writer.writerow(row)

    elapsed = time.time() - t_start
    logger.info("Sweep done: %d combinations in %.1fs. Results written to %s", total_combos, elapsed, csv_path)


if __name__ == "__main__":
    main()
