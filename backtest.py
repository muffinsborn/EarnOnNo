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

CAPITAL SAFETY: simulate_portfolio() enforces a hard capital guard (a new
position is only opened if bankroll + realized P&L so far - capital tied up
in still-open positions covers its full cost) and a per-position stop-loss
(default 25% mark-to-market drawdown exits the position early, freeing its
capital). Together these make bankroll exhaustion structurally impossible;
validate_bankroll_never_exhausted() re-checks this after the fact.

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
    breakdown_by_price_bucket,
    drawdown_analysis,
    edge_confidence,
    print_summary,
    save_trades_csv,
    stop_loss_exit_count,
    summarize,
    validate_bankroll_never_exhausted,
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
    parser.add_argument("--stop-loss-pct", type=float, default=0.25,
                         help="Exit a position early if its mark-to-market value ever falls this fraction "
                              "below its entry cost (default 0.25 = 25%%), freeing its capital for "
                              "redeployment. Pass 0 to disable and hold every position to resolution.")
    parser.add_argument("--categories", default=None,
                         help="Comma-separated category names to restrict candidate discovery to (e.g. "
                              "'Mentions,Politics,Entertainment'). Omit to include every category.")
    parser.add_argument("--out-dir", default="backtest_results", help="Directory for the trades CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not os.path.isfile(args.db):
        raise SystemExit(f"Database not found at {args.db} - run pipeline.py first (Phase 1).")

    # Read-only against a live database Phase 1 may still be writing to;
    # WAL mode (enabled once on the db file) lets reads and writes coexist,
    # and this timeout is a defense-in-depth backstop against brief locks.
    conn = sqlite3.connect(args.db, timeout=30)

    categories = {c.strip() for c in args.categories.split(",") if c.strip()} if args.categories else None

    entry_offset_hours = {"Sports": args.entry_offset_hours_sports, "_default": args.entry_offset_hours_other}
    candidates = discover_candidates(
        conn,
        price_min=args.price_min,
        price_max=args.price_max,
        days_to_expiry=args.days_to_expiry,
        entry_offset_hours=entry_offset_hours,
        categories=categories,
    )

    trades = simulate_portfolio(
        candidates,
        bankroll=args.bankroll,
        position_pct=args.position_pct,
        category_cap_pct=args.category_cap_pct,
        max_concurrent=args.max_concurrent,
        slippage_pct_of_spread=args.slippage_pct_of_spread,
        conn=conn,
        stop_loss_pct=args.stop_loss_pct or None,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "trades.csv")
    save_trades_csv(trades, csv_path)

    overall = summarize(trades)
    print_summary("OVERALL", overall)

    conf = edge_confidence(trades)
    if conf.get("total_trades", 0) > 0 and conf["win_rate_ci_low"] is not None:
        z_label = "95%"
        print(f"\n=== EDGE CONFIDENCE ({z_label} Wilson interval) ===")
        print(f"  win rate:            {conf['win_rate']*100:.2f}%  "
              f"[{conf['win_rate_ci_low']*100:.2f}%, {conf['win_rate_ci_high']*100:.2f}%]")
        print(f"  breakeven win rate:  {conf['breakeven_win_rate']*100:.2f}%")
        print(f"  edge_bps:            {conf['edge_bps']}  "
              f"(interval: [{conf['edge_bps_ci_low']}, {conf['edge_bps_ci_high']}])")
        verdict = "YES - breakeven falls outside the CI" if conf["edge_significant"] \
            else "NO - breakeven falls inside the CI, indistinguishable from noise at this sample size"
        print(f"  statistically credible edge: {verdict}")

    dd = drawdown_analysis(trades, bankroll=args.bankroll)
    print(f"\n=== DRAWDOWN (worst stretch) ===")
    print(f"  max drawdown:  ${dd['max_drawdown_dollars']:,.2f} ({dd.get('max_drawdown_pct_of_bankroll')}% of starting bankroll)")
    if dd["worst_stretch_trades"]:
        print(f"  window:        {dd['worst_stretch_start']} -> {dd['worst_stretch_end']}")
        print(f"  trades in window: {len(dd['worst_stretch_trades'])}")

    bankroll_check = validate_bankroll_never_exhausted(trades, bankroll=args.bankroll)
    print(f"\n=== CAPITAL SAFETY CHECKS ===")
    status = "VIOLATED - BUG" if bankroll_check["exhausted"] else "OK - never exhausted"
    print(f"  bankroll exhaustion: {status} (min running equity ${bankroll_check['min_equity_dollars']:,.2f}"
          f"{' at ' + bankroll_check['min_equity_ts'] if bankroll_check['min_equity_ts'] else ''})")
    if args.stop_loss_pct:
        print(f"  stop-loss ({args.stop_loss_pct*100:.0f}%) exits: {stop_loss_exit_count(trades)}/{len(trades)} trades")
    else:
        print(f"  stop-loss: disabled (--stop-loss-pct 0)")

    print("\n=== BY CATEGORY ===")
    by_cat: dict[str, list[dict]] = {}
    for t in trades:
        by_cat.setdefault(t["category"], []).append(t)
    for cat, cat_trades in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_conf = edge_confidence(cat_trades)
        print_summary(cat, cat_conf)
        if cat_conf.get("win_rate_ci_low") is not None:
            verdict = "significant" if cat_conf["edge_significant"] else "not significant (noise-consistent)"
            print(f"  win rate 95% CI: [{cat_conf['win_rate_ci_low']*100:.2f}%, {cat_conf['win_rate_ci_high']*100:.2f}%]"
                  f"  vs breakeven {cat_conf['breakeven_win_rate']*100:.2f}%  -> {verdict}")

    print("\n=== BY PRICE BUCKET (YES price at entry) ===")
    for bucket, stats in breakdown_by_price_bucket(trades).items():
        print_summary(bucket, stats)

    print(f"\nFull trade log written to {csv_path}")

    conn.close()


if __name__ == "__main__":
    main()
