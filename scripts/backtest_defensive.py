"""
Defensive circuit-breaker overlay on the momentum strategy.

THE QUESTION
------------
The full-cycle momentum finding (docs/FINDINGS_momentum.md) was strong but
carried a −73% max drawdown that the strategy's own positive-momentum cash
floor barely tamed, because momentum lags: by the time trailing return turns
negative, much of the crash has happened.

This tests whether a FASTER risk-off signal — a circuit breaker that flattens
to cash on a volatility spike OR a drawdown breach — cuts that drawdown
WITHOUT destroying the return. This is the DEFENSIVE transition of the
three-state allocation model (YIELD / DIRECTIONAL / DEFENSIVE), and the
priority rule DEFENSIVE > DIRECTIONAL > YIELD: when the breaker fires, it
overrides momentum and goes to cash.

DESIGN DISCIPLINE (to avoid manufacturing a pretty backtest)
------------------------------------------------------------
  - The base momentum config is FIXED at the full-cycle winner (30/5/7).
    We do NOT re-sweep momentum jointly with the overlay — that would
    over-fit both at once. We isolate the single question: does the
    breaker help, holding the strategy constant?
  - The overlay grid is reported in FULL. We judge by whether a
    CONTIGUOUS region improves risk-adjusted return, not whether one
    magic cell does.
  - The honest success metric is NOT "lower drawdown" (trivial — just sit
    in cash). It is: does ret/vol improve AFTER paying for every whipsaw,
    and does the drawdown reduction survive transaction costs? A breaker
    that halves drawdown but also halves return has made you a worse BTC.

THE BREAKER (fires if EITHER condition is true → flatten to cash)
-----------------------------------------------------------------
  Volatility spike: realized vol over the last V days exceeds M times the
                    trailing 30-day baseline vol.
  Drawdown breach:  the market proxy (BTC) is down more than D% from its
                    rolling high over the last DD_LOOKBACK days.

HYSTERESIS (tested both ways)
-----------------------------
  no-hysteresis: re-enter the moment the breaker condition clears.
  hysteresis:    after the breaker clears, require RECOVER_DAYS of calm
                 before re-entering — reduces whipsaw at the cost of
                 re-entering later. We measure the trade-off directly.

USAGE
-----
    python -m scripts.backtest_defensive
    python -m scripts.backtest_defensive --cost-bps 25
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.backtest_momentum import (
    load_daily_closes, run_benchmark_hold, _metrics_from_curve,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtest_defensive")


# Fixed base momentum config — the full-cycle winner. NOT swept here.
BASE_N, BASE_K, BASE_H = 30, 5, 7

# Defensive overlay grid.
VOL_WINDOW_V = (3, 7)          # short realized-vol window, days
VOL_MULTIPLE_M = (1.5, 2.0, 3.0)   # spike = V-day vol > M * 30d baseline
DRAWDOWN_D = (0.10, 0.15, 0.20)    # breach = BTC down >D from rolling high
DD_LOOKBACK = 14               # rolling-high window for drawdown, days

# Hysteresis: days of "all-clear" required before re-entering after the
# breaker fires. 0 == no hysteresis (re-enter immediately on clear).
RECOVER_DAYS = (0, 7)

TRADING_DAYS_PER_YEAR = 365
MARKET_PROXY = "BTCUSDT"


def _compute_breaker_flags(closes: pd.DataFrame, v: int, m: float,
                           d: float, dd_lookback: int) -> pd.Series:
    """For each date, True if the defensive breaker condition holds
    (vol spike OR drawdown breach). Uses BTC as the market proxy and
    is computed on past data only (rolling windows are backward-looking,
    so no lookahead)."""
    btc = closes[MARKET_PROXY]
    daily_ret = btc.pct_change()

    # Volatility spike: V-day realized vol vs 30-day baseline.
    vol_short = daily_ret.rolling(v).std()
    vol_base = daily_ret.rolling(30).std()
    vol_spike = vol_short > (m * vol_base)

    # Drawdown breach: BTC vs its rolling-high over dd_lookback days.
    rolling_high = btc.rolling(dd_lookback).max()
    drawdown = btc / rolling_high - 1.0
    dd_breach = drawdown < -d

    breaker = (vol_spike | dd_breach).fillna(False)
    return breaker


def run_with_overlay(closes: pd.DataFrame, v: int, m: float, d: float,
                     recover_days: int, cost_rate: float) -> dict:
    """Run the fixed 30/5/7 momentum strategy with the defensive overlay.

    Each day, if the breaker is active (or we're inside the post-breaker
    recovery window), the book is in cash regardless of momentum.
    Otherwise the momentum basket is held as normal. The breaker check
    uses only backward-looking rolling windows.
    """
    n, k, h = BASE_N, BASE_K, BASE_H
    daily_ret = closes.pct_change()
    dates = closes.index
    breaker = _compute_breaker_flags(closes, v, m, d, DD_LOOKBACK)
    # CRITICAL no-lookahead shift: the breaker flag computed from day t's
    # close can only inform the decision for day t+1. If we used
    # breaker.iloc[j] to decide whether to hold day j, we'd be using
    # day j's own close (which is inside the rolling vol/drawdown
    # windows) to avoid day j's return — i.e. selling a down day with
    # perfect foresight of that day's move. Shift forward by one day so
    # the decision for day j uses only information through j-1.
    breaker = breaker.shift(1).fillna(False)

    equity = [1.0]
    equity_dates = [dates[n]]
    held: list[str] = []
    in_recovery_until = -1     # index until which we stay defensive

    i = n
    while i < len(dates) - 1:
        # Momentum rank on strictly-past data (same as base strategy).
        now_px = closes.iloc[i]
        past_px = closes.iloc[i - n]
        trailing = (now_px / past_px - 1.0).dropna()
        ranked = trailing.sort_values(ascending=False)
        eligible = ranked[ranked > 0]
        momentum_basket = list(eligible.index[:k])

        # Hold for H days, but re-check the breaker DAILY (the breaker
        # is fast — it shouldn't wait for the next rebalance).
        end = min(i + h, len(dates) - 1)
        # Determine the basket for this hold period's start (turnover
        # cost charged once at rebalance, as in the base strategy).
        prev = set(held)
        new = set(momentum_basket)
        turnover = len(prev.symmetric_difference(new)) / (2 * max(k, 1))
        equity_now = equity[-1] * (1.0 - turnover * cost_rate)
        held = momentum_basket

        for j in range(i + 1, end + 1):
            breaker_active = bool(breaker.iloc[j])
            if breaker_active:
                # Enter / extend defensive. Charge a one-off exit cost
                # the first day we flip to cash from a held basket.
                if held:
                    equity_now *= (1.0 - cost_rate)  # liquidation cost
                    held = []
                in_recovery_until = j + recover_days
                day_ret = 0.0
            elif j <= in_recovery_until:
                # Post-breaker recovery hold — stay in cash (hysteresis).
                day_ret = 0.0
            else:
                # Normal: hold the momentum basket (re-acquire if we were
                # in cash — charge re-entry cost once).
                if not held and momentum_basket:
                    equity_now *= (1.0 - cost_rate)
                    held = momentum_basket
                if held:
                    rets = daily_ret.iloc[j][held].dropna()
                    day_ret = rets.mean() if len(rets) else 0.0
                else:
                    day_ret = 0.0
            equity_now *= (1.0 + day_ret)
            equity.append(equity_now)
            equity_dates.append(dates[j])
        i = end

    curve = pd.Series(equity, index=pd.DatetimeIndex(equity_dates))
    curve = curve[~curve.index.duplicated(keep="last")]
    metrics = _metrics_from_curve(curve)
    # Fraction of days spent defensive (in cash) — the "cost" of safety.
    metrics["pct_defensive"] = float(breaker.iloc[n:].mean())
    return metrics


def run_base_momentum(closes: pd.DataFrame, cost_rate: float) -> dict:
    """The fixed 30/5/7 strategy with NO overlay, for comparison.
    Reuses the base strategy's logic via a recover_days that never
    triggers — simplest is to call run_with_overlay with an
    impossible breaker, but to stay honest we reimplement the plain
    hold here by importing the original."""
    from scripts.backtest_momentum import run_strategy
    return run_strategy(closes, BASE_N, BASE_K, BASE_H, cost_rate)


def main() -> int:
    parser = argparse.ArgumentParser(description="Defensive overlay backtest")
    parser.add_argument("--cost-bps", type=float, default=10.0,
                        help="Transaction cost in bps per unit turnover "
                             "and per defensive flip (default 10).")
    args = parser.parse_args()
    cost_rate = args.cost_bps / 10000.0

    logger.info("Loading daily closes...")
    closes = load_daily_closes()
    logger.info("Loaded %d days, %s to %s", len(closes),
                closes.index[0].date(), closes.index[-1].date())

    bench_btc = run_benchmark_hold(closes, MARKET_PROXY)
    base = run_base_momentum(closes, cost_rate)

    results = []
    for v, m, d, rec in product(VOL_WINDOW_V, VOL_MULTIPLE_M,
                                DRAWDOWN_D, RECOVER_DAYS):
        r = run_with_overlay(closes, v, m, d, rec, cost_rate)
        r.update({"V": v, "M": m, "D": d, "rec": rec})
        r["beats_base_riskadj"] = r["ret_vol"] > base["ret_vol"]
        r["cuts_drawdown"] = r["max_dd"] > base["max_dd"]  # less negative
        results.append(r)

    # Sort by risk-adjusted return — the metric that actually matters.
    results.sort(key=lambda r: (r["ret_vol"] if not np.isnan(r["ret_vol"])
                                else -99), reverse=True)

    print()
    print("=" * 100)
    print("DEFENSIVE CIRCUIT-BREAKER OVERLAY  (base momentum fixed at 30/5/7)")
    print("=" * 100)
    print(f"Window: {closes.index[0].date()} to {closes.index[-1].date()}   "
          f"cost={args.cost_bps:.0f}bps")
    print()
    print("REFERENCE")
    print(f"  {'hold BTC':<26} final={bench_btc['final']:.2f}x  "
          f"ret/vol={bench_btc['ret_vol']:.2f}  maxDD={bench_btc['max_dd']*100:6.1f}%")
    print(f"  {'momentum 30/5/7 (no overlay)':<26} final={base['final']:.2f}x  "
          f"ret/vol={base['ret_vol']:.2f}  maxDD={base['max_dd']*100:6.1f}%")
    print()
    print("OVERLAY GRID (sorted by ret/vol; full grid)")
    print(f"  {'V':>2} {'M':>4} {'D':>5} {'rec':>4}  {'final':>7}  "
          f"{'ret/vol':>7}  {'maxDD':>7}  {'%cash':>6}  {'>base ra':>8} {'<DD':>4}")
    print("  " + "-" * 80)
    for r in results:
        print(f"  {r['V']:>2} {r['M']:>4.1f} {r['D']*100:>4.0f}% {r['rec']:>4}  "
              f"{r['final']:>6.2f}x  {r['ret_vol']:>7.2f}  "
              f"{r['max_dd']*100:>6.1f}%  {r['pct_defensive']*100:>5.1f}%  "
              f"{'Y' if r['beats_base_riskadj'] else '.':>8} "
              f"{'Y' if r['cuts_drawdown'] else '.':>4}")

    n_better_ra = sum(r["beats_base_riskadj"] for r in results)
    n_cuts_dd = sum(r["cuts_drawdown"] for r in results)
    total = len(results)
    print()
    print("SUMMARY")
    print(f"  base momentum (no overlay): final={base['final']:.2f}x  "
          f"ret/vol={base['ret_vol']:.2f}  maxDD={base['max_dd']*100:.1f}%")
    print(f"  {n_cuts_dd}/{total} overlay configs reduce max drawdown")
    print(f"  {n_better_ra}/{total} overlay configs improve risk-adjusted return")
    print()
    if n_better_ra >= total * 0.6:
        print("  READ: most overlay configs improve risk-adjusted return — the")
        print("  circuit breaker robustly helps, not just one tuned cell. The")
        print("  defensive transition has real value over momentum's own lag.")
    elif n_better_ra <= total * 0.25:
        print("  READ: few configs improve risk-adjusted return. The breaker cuts")
        print("  drawdown but pays for it in lost return/whipsaw — it's not a free")
        print("  improvement. Momentum's lag may be cheaper to live with than the")
        print("  whipsaw of a fast breaker on this data.")
    else:
        print("  READ: mixed. Look for a contiguous region (e.g. a drawdown band")
        print("  with hysteresis) that improves ret/vol, not isolated cells.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
