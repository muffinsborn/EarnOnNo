"""Stats, breakdowns, drawdown analysis, and CSV/report output."""
import csv
import math
import statistics
from datetime import datetime, timezone

PRICE_BUCKETS = [(0.0, 0.025), (0.025, 0.05), (0.05, 0.08), (0.08, 0.10), (0.10, 0.15), (0.15, 1.0)]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"total_trades": 0}

    pnls = [t["pnl"] for t in trades]
    costs = [t["total_cost"] for t in trades]
    returns = [t["pnl"] / t["total_cost"] for t in trades if t["total_cost"] > 0]
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(trades)

    sharpe_like = None
    if len(returns) > 1:
        stdev = statistics.stdev(returns)
        if stdev > 0:
            sharpe_like = statistics.mean(returns) / stdev

    # Edge computed directly rather than inferred separately from win rate
    # and price. A NO contract bought at price p pays $1/contract on a win,
    # $0 on a loss, so expected profit = P(win) - p: breakeven is exactly
    # the (fee-inclusive) average price paid, contract-weighted so heavier
    # positions count proportionally more - NOT "1 - price", which would
    # invert the sign (these are longshot-YES markets, so the NO price
    # paid is close to $1, and breakeven should be close to the ~90% win
    # rates observed, not close to 0%). win_rate above already reflects any
    # stop-loss that cut an eventual winner short (pnl > 0, not result ==
    # "no"), so edge_bps nets that cost in too, not just fees.
    total_contracts = sum(t["contracts"] for t in trades)
    avg_entry_price_no = None
    breakeven_win_rate = None
    edge_bps = None
    if total_contracts > 0:
        avg_entry_price_no = sum(t["effective_price_no"] * t["contracts"] for t in trades) / total_contracts
        breakeven_win_rate = sum(t["total_cost"] for t in trades) / total_contracts
        edge_bps = (win_rate - breakeven_win_rate) * 10000

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": win_rate,
        "total_pnl": round(sum(pnls), 2),
        "total_cost_deployed": round(sum(costs), 2),
        "total_fees": round(sum(t["fee"] for t in trades), 2),
        "roi_pct": round(100 * sum(pnls) / sum(costs), 2) if sum(costs) else None,
        "sharpe_like_per_trade": round(sharpe_like, 3) if sharpe_like is not None else None,
        "avg_entry_price_no": round(avg_entry_price_no, 4) if avg_entry_price_no is not None else None,
        "breakeven_win_rate": round(breakeven_win_rate, 4) if breakeven_win_rate is not None else None,
        "edge_bps": round(edge_bps, 1) if edge_bps is not None else None,
    }


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion (default
    z=1.96 = 95%). Used instead of the naive normal-approximation interval
    (phat +/- z*sqrt(phat(1-phat)/n)) because that one is unreliable
    exactly where this data lives - proportions away from 0.5 and/or
    moderate n - since it can produce bounds outside [0,1] and understates
    width. No scipy dependency needed."""
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    half = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    return (max(0.0, (center - half) / denom), min(1.0, (center + half) / denom))


def edge_confidence(trades: list[dict], z: float = 1.96) -> dict:
    """Is the apparent edge in summarize() statistically credible, or could
    it just be noise from a small sample? Builds a Wilson interval around
    the observed win rate and checks whether breakeven_win_rate falls
    outside it - if breakeven is still inside the interval, the sample
    can't distinguish this result from a coin flip at that price."""
    stats = summarize(trades)
    if stats.get("total_trades", 0) == 0 or stats.get("breakeven_win_rate") is None:
        return {
            **stats, "win_rate_ci_low": None, "win_rate_ci_high": None,
            "edge_bps_ci_low": None, "edge_bps_ci_high": None, "edge_significant": None,
        }
    ci_low, ci_high = wilson_ci(stats["wins"], stats["total_trades"], z=z)
    breakeven = stats["breakeven_win_rate"]
    return {
        **stats,
        "win_rate_ci_low": round(ci_low, 4),
        "win_rate_ci_high": round(ci_high, 4),
        "edge_bps_ci_low": round((ci_low - breakeven) * 10000, 1),
        "edge_bps_ci_high": round((ci_high - breakeven) * 10000, 1),
        "edge_significant": bool(breakeven < ci_low or breakeven > ci_high),
    }


def _exit_ts(t: dict) -> int:
    """A trade's realization time - exit_ts if it was actually stopped out
    or resolved by the engine, falling back to close_ts for any trade
    produced by older code that predates the exit_ts field."""
    return t.get("exit_ts", t["close_ts"])


def drawdown_analysis(trades: list[dict], bankroll: float) -> dict:
    """Builds a realized equity curve ordered by exit time (P&L realizes
    when a position actually leaves the portfolio - at a stop-loss or at
    resolution, not at entry) and finds the single worst peak-to-trough
    stretch - the stress view, not just an average.

    Drawdown % is expressed against starting bankroll, not against the
    peak-so-far: early in a short trade series, the peak can be a tiny
    dollar amount (a handful of small wins), and dividing by that produces
    a nonsensical percentage (e.g. >1000%) even though the dollar drawdown
    itself is unremarkable. % of bankroll is what actually answers "how
    much of my capital did the worst stretch put at risk"."""
    if not trades:
        return {"max_drawdown_dollars": 0.0, "max_drawdown_pct_of_bankroll": 0.0, "worst_stretch_trades": []}

    ordered = sorted(trades, key=_exit_ts)

    equity = 0.0
    peak = 0.0
    peak_ts = _exit_ts(ordered[0])
    max_dd = 0.0
    dd_peak_ts, dd_trough_ts = peak_ts, peak_ts

    for t in ordered:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
            peak_ts = _exit_ts(t)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            dd_peak_ts = peak_ts
            dd_trough_ts = _exit_ts(t)

    max_dd_pct = (max_dd / bankroll * 100) if bankroll > 0 else None

    worst_stretch = [
        t for t in ordered
        if dd_peak_ts <= _exit_ts(t) <= dd_trough_ts
    ]

    return {
        "max_drawdown_dollars": round(max_dd, 2),
        "max_drawdown_pct_of_bankroll": round(max_dd_pct, 2) if max_dd_pct is not None else None,
        "worst_stretch_start": _iso(dd_peak_ts),
        "worst_stretch_end": _iso(dd_trough_ts),
        "worst_stretch_trades": worst_stretch,
    }


def validate_bankroll_never_exhausted(trades: list[dict], bankroll: float) -> dict:
    """Independent post-hoc check (belt-and-suspenders alongside the
    capital guard baked into simulate_portfolio): replays realized P&L in
    exit-time order and confirms running equity (bankroll + realized P&L)
    never went negative. Should always report exhausted=False given the
    guard in engine.py - if it ever doesn't, that's a bug in the guard,
    not a strategy-risk finding."""
    ordered = sorted(trades, key=_exit_ts)
    equity = bankroll
    min_equity = bankroll
    min_equity_ts = None
    for t in ordered:
        equity += t["pnl"]
        if equity < min_equity:
            min_equity = equity
            min_equity_ts = _exit_ts(t)
    return {
        "exhausted": min_equity < 0,
        "min_equity_dollars": round(min_equity, 2),
        "min_equity_ts": _iso(min_equity_ts) if min_equity_ts is not None else None,
    }


def stop_loss_exit_count(trades: list[dict]) -> int:
    return sum(1 for t in trades if t.get("exit_reason") == "stop_loss")


def breakdown_by_category(trades: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = {}
    for t in trades:
        by_cat.setdefault(t["category"], []).append(t)
    return {cat: summarize(ts) for cat, ts in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))}


def breakdown_by_price_bucket(trades: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        p = t["midpoint_yes"]
        for lo, hi in PRICE_BUCKETS:
            if lo <= p < hi:
                label = f"{lo*100:g}-{hi*100:g}c"
                buckets.setdefault(label, []).append(t)
                break
    return {label: summarize(ts) for label, ts in buckets.items()}


def breakdown_by_price_cent(trades: list[dict], low_cents: int = 2, high_cents: int = 15) -> dict:
    """Same idea as breakdown_by_price_bucket() but at 1-cent resolution,
    using edge_confidence() (win-rate CI included) per bucket instead of
    plain summarize() - built for pinpointing exactly where an edge turns
    from credible to noise-consistent or negative, rather than only seeing
    it averaged out across a wide band. Buckets are [c, c+1) cents for
    c in [low_cents, high_cents], keyed as e.g. "7c"; round() before floor
    guards against float noise (0.15 stored as ~0.14999999999999999)."""
    buckets: dict[int, list[dict]] = {}
    for t in trades:
        cents = int(math.floor(round(t["midpoint_yes"] * 100, 6)))
        if low_cents <= cents <= high_cents:
            buckets.setdefault(cents, []).append(t)
    return {f"{cents}c": edge_confidence(ts) for cents, ts in sorted(buckets.items())}


def save_trades_csv(trades: list[dict], path: str) -> None:
    if not trades:
        with open(path, "w") as f:
            f.write("no trades\n")
        return
    fieldnames = [
        "ticker", "category", "entry_ts", "close_ts", "midpoint_yes", "entry_price_no",
        "effective_price_no", "spread", "slippage_cost", "open_interest", "contracts",
        "cost_before_fee", "entry_fee", "total_cost", "exit_ts", "exit_reason", "exit_price_no",
        "exit_fee", "fee", "result", "payout", "proceeds", "pnl",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in sorted(trades, key=lambda t: t["entry_ts"]):
            row = dict(t)
            row["entry_ts"] = _iso(t["entry_ts"])
            row["close_ts"] = _iso(t["close_ts"])
            row["exit_ts"] = _iso(_exit_ts(t))
            writer.writerow(row)


def print_summary(title: str, stats: dict) -> None:
    print(f"\n=== {title} ===")
    if stats.get("total_trades", 0) == 0:
        print("No trades.")
        return
    print(f"  trades:        {stats['total_trades']}")
    print(f"  win rate:      {stats['win_rate']*100:.1f}%  ({stats['wins']}W / {stats['losses']}L)")
    print(f"  total P&L:     ${stats['total_pnl']:,.2f}")
    print(f"  capital deployed: ${stats['total_cost_deployed']:,.2f}")
    print(f"  ROI:           {stats['roi_pct']}%")
    print(f"  total fees:    ${stats['total_fees']:,.2f}")
    print(f"  avg NO price:  {stats['avg_entry_price_no']}  |  breakeven win rate: {stats['breakeven_win_rate']}"
          f"  |  edge: {stats['edge_bps']} bps")
    print(f"  sharpe-like:   {stats['sharpe_like_per_trade']} (per-trade return mean/stdev, not time-annualized)")
