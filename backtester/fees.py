"""Kalshi's actual taker fee schedule.

fee = ceil_to_cent(0.07 * contracts * price * (1 - price))

No maker-fill simulation (would require modeling whether a resting limit
order actually fills) and no settlement/resolution fee (Kalshi charges none -
holding a winner to expiry costs $0 beyond the entry fee).
"""
import math

TAKER_FEE_RATE = 0.07


def taker_fee_dollars(contracts: int, price: float) -> float:
    if contracts <= 0:
        return 0.0
    raw = TAKER_FEE_RATE * contracts * price * (1 - price)
    # Round off floating-point noise (e.g. 0.07*100*0.5*0.5 lands on
    # 1.7500000000000002, not exactly 1.75) before ceiling, or exact-cent
    # amounts spuriously get bumped up a cent.
    cents = round(raw * 100, 6)
    return math.ceil(cents) / 100
