"""Kalshi NO-on-longshots backtest - Phase 2.

Reads the SQLite database built by pipeline.py (read-only, never writes to
it). Simulates buying NO on markets whose YES price was below a threshold
at a fixed point in time before their own close, holding to expiry.

NO LOOKAHEAD: every eligibility check and entry price comes from
backtester.pointintime.price_and_oi_as_of(), which only reads candle rows
with end_period_ts <= the simulated entry timestamp. The `result` column
(final resolution) is read only after a position is already opened, purely
to compute payout - never for selection or ranking. See backtester/engine.py
for the exact mechanism.

Usage:
    python backtest.py                    # full run with defaults below
    python backtest.py --price-threshold 0.05 --days-to-expiry 7
    python backtest.py --out-dir results/2026-07-29

No trading/execution logic here - this only reads historical data and
simulates. Nothing in this script places or could place a live order.
"""
import argparse
import logging
import os
import sqlite3

from backtester.engine import discover_candidates, simulate_portfolio
from backtester.report import (
    breakdown_by_category,
    breakdown_by_price_bucket,
    drawdown_analysis,
    print_summary,
    save_trades_csv,
    summarize,
)

logger = logging.getLogger("backtester")


def main():
    parser = argparse.ArgumentParser(description="Kalshi NO-on-longshots backtest (Phase 2, no lookahead)")
    parser.add_argument("--db", default="data/kalshi.db", help="Path to the SQLite DB built by pipeline.py")
    parser.add_argument("--price-min", type=float, default=0.025,
                         help="Min YES price (0-1 fraction) for eligibility (default 0.025 = 2.5c - excludes "
                              "degenerate near-zero contracts)")
    parser.add_argument("--price-max", type=float, default=0.15,
                         help="Max YES price (0-1 fraction) to consider a market a longshot (default 0.15 = 15c)")
    parser.add_argument("--days-to-expiry", type=int, default=14,
                         help="Max days-to-expiry window for eligibility (default 14)")
    parser.add_argument("--entry-offset-hours-sports", type=float, default=0.5,
                         help="Fixed hours before close at which Sports-category markets are evaluated/entered "
                              "(default 0.5 = 30min - sports markets resolve fast, so a short offset lets most "
                              "of them qualify while still giving a real pre-resolution window)")
    parser.add_argument("--entry-offset-hours-other", type=float, default=12,
                         help="Fixed hours before close for every non-Sports category (default 12 - these tend "
                              "to be slower-moving political/macro/economic markets)")
    parser.add_argument("--bankroll", type=float, default=10000.0, help="Starting bankroll for sizing math")
    parser.add_argument("--position-pct", type=float, default=0.02,
                         help="Flat fraction of starting bankroll per position (default 0.02 = 2%%, i.e. $200 on a $10k bankroll)")
    parser.add_argument("--category-cap-pct", type=float, default=0.25,
                         help="Max fraction of bankroll allowed in any single category at once (default 0.25)")
    parser.add_argument("--max-concurrent", type=int, default=25,
                         help="Max concurrent open positions in the portfolio (default 25)")
    parser.add_argument("--slippage-pct-of-spread", type=float, default=0.5,
                         help="Fraction of the quoted bid-ask spread added to the taker price as slippage "
                              "(default 0.5 = pay through half the spread). Since Kalshi's fee is itself a "
                              "function of price, this also shifts the fee, not just the raw cost.")
    parser.add_argument("--out-dir", default="backtest_results", help="Directory for the trades CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not os.path.isfile(args.db):
        raise SystemExit(f"Database not found at {args.db} - run pipeline.py first (Phase 1).")

    # Read-only against a live database Phase 1 may still be writing to;
    # WAL mode (enabled once on the db file) lets reads and writes coexist,
    # and this timeout is a defense-in-depth backstop against brief locks.
    conn = sqlite3.connect(args.db, timeout=30)

    entry_offset_hours = {"Sports": args.entry_offset_hours_sports, "_default": args.entry_offset_hours_other}
    candidates = discover_candidates(
        conn,
        price_min=args.price_min,
        price_max=args.price_max,
        days_to_expiry=args.days_to_expiry,
        entry_offset_hours=entry_offset_hours,
    )

    trades = simulate_portfolio(
        candidates,
        bankroll=args.bankroll,
        position_pct=args.position_pct,
        category_cap_pct=args.category_cap_pct,
        max_concurrent=args.max_concurrent,
        slippage_pct_of_spread=args.slippage_pct_of_spread,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "trades.csv")
    save_trades_csv(trades, csv_path)

    overall = summarize(trades)
    print_summary("OVERALL", overall)

    dd = drawdown_analysis(trades, bankroll=args.bankroll)
    print(f"\n=== DRAWDOWN (worst stretch) ===")
    print(f"  max drawdown:  ${dd['max_drawdown_dollars']:,.2f} ({dd.get('max_drawdown_pct_of_bankroll')}% of starting bankroll)")
    if dd["worst_stretch_trades"]:
        print(f"  window:        {dd['worst_stretch_start']} -> {dd['worst_stretch_end']}")
        print(f"  trades in window: {len(dd['worst_stretch_trades'])}")

    print("\n=== BY CATEGORY ===")
    for cat, stats in breakdown_by_category(trades).items():
        print_summary(cat, stats)

    print("\n=== BY PRICE BUCKET (YES price at entry) ===")
    for bucket, stats in breakdown_by_price_bucket(trades).items():
        print_summary(bucket, stats)

    print(f"\nFull trade log written to {csv_path}")

    conn.close()


if __name__ == "__main__":
    main()
