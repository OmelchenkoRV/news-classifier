"""
Cross-sectional momentum rotation backtest.

THE QUESTION
------------
Can rotating a crypto portfolio into recently-strong tokens beat simply
holding BTC, after transaction costs and on a risk-adjusted basis?

This is the rotation hypothesis for the portfolio goal. It is a
DIFFERENT and better-supported hypothesis than the news-reaction one
the forecaster falsified: here the signal is price itself (relative
strength), not headlines.

THE STRATEGY
------------
At each rebalance date:
  1. Rank the universe by trailing N-day return ("momentum").
  2. Hold the top K tokens, equally weighted.
  3. CASH FLOOR: a token is only eligible if its momentum is POSITIVE.
     If fewer than K tokens have positive momentum, the rest of the
     book sits in cash (USDT, zero return). In a broad drawdown where
     everything is falling, the book goes fully to cash — the
     protective behaviour pure relative-momentum can't provide.
  4. Hold for H days, then rebalance.

Transaction costs are charged on turnover at each rebalance.

HONESTY MECHANISMS (this is the whole point)
--------------------------------------------
  - No lookahead: momentum at a rebalance date uses only closes STRICTLY
    BEFORE that date; returns are earned over the FOLLOWING H days.
  - Full grid reported, not the best cell. If only one (N,K,H) corner
    beats BTC, that's overfitting; if most of the grid does, the effect
    is robust. We sort and show everything.
  - Three benchmarks: hold-BTC (the strict bar), equal-weight-all, and
    risk-adjusted return (CAGR / annualised volatility) for every
    config and benchmark.
  - Known biases stated plainly: the price window (Jun 2023+) is almost
    entirely a bull/recovery regime, and the universe is survivorship-
    filtered. Both flatter momentum. Results are an UPPER BOUND.

USAGE
-----
    python -m scripts.backtest_momentum
    python -m scripts.backtest_momentum --cost-bps 20        # 0.20%/trade
    python -m scripts.backtest_momentum --json results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor
from config.universe import universe

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtest_momentum")


# Grid to sweep. Reported in full — no cherry-picking the winner.
LOOKBACKS_N = (14, 30, 60, 90)     # trailing-return window, days
HOLD_TOP_K = (1, 3, 5)             # how many tokens to hold
REBALANCE_H = (7, 14, 30)          # days between rebalances

TRADING_DAYS_PER_YEAR = 365        # crypto trades every day


# ── Data loading ─────────────────────────────────────────────────────
def load_daily_closes() -> pd.DataFrame:
    """Load hourly closes from price_snapshots and resample to daily
    (last close of each UTC day) per universe symbol. Returns a
    DataFrame indexed by date, one column per symbol."""
    syms = list(universe())
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT symbol, timestamp, close
            FROM price_snapshots
            WHERE symbol = ANY(%s)
            ORDER BY symbol, timestamp ASC
        """, (syms,))
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise SystemExit("No price data found for the universe. Run "
                         "`python -m collectors.price_backfill --universe` first.")

    df = pd.DataFrame(rows, columns=["symbol", "timestamp", "close"]
                      if not isinstance(rows[0], dict) else None)
    if isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["close"] = df["close"].astype(float)
    df["date"] = df["timestamp"].dt.floor("D")

    # last close per (symbol, day), then pivot to wide
    daily = (df.sort_values("timestamp")
               .groupby(["symbol", "date"])["close"].last()
               .reset_index())
    wide = daily.pivot(index="date", columns="symbol", values="close")
    wide = wide.sort_index()
    # Forward-fill small gaps (a missing daily candle). We do NOT
    # dropna across all columns here — that would chop the whole panel
    # back to the youngest token's listing date and throw away early
    # history for everyone. Instead the backtest ranks per-date over
    # only the tokens that have data on that date (see run_strategy),
    # so a token that hasn't listed yet simply isn't in that day's
    # cross-section — which is realistic: you couldn't have held it.
    wide = wide.ffill()
    # Drop only leading rows where NO symbol has data yet.
    wide = wide.dropna(how="all")
    return wide


# ── The backtest ─────────────────────────────────────────────────────
def run_strategy(closes: pd.DataFrame, n: int, k: int, h: int,
                 cost_rate: float) -> dict:
    """Run one (N, K, H) momentum-rotation config over the full window.

    Returns a dict of performance metrics. The daily equity curve is
    built by, at each rebalance, choosing the held basket from momentum
    ranked on closes up to (and including) the rebalance date, then
    applying the realised daily returns of that basket over the next H
    days. Cash positions earn zero.
    """
    daily_ret = closes.pct_change()
    dates = closes.index
    symbols = list(closes.columns)

    # Equity curve in daily steps; start at 1.0
    equity = [1.0]
    equity_dates = [dates[n]]   # first rebalance can't be before we have N days

    held: list[str] = []        # current basket (symbols); [] == all cash
    i = n                       # start once we have N days of history
    while i < len(dates) - 1:
        # Rebalance: rank by trailing-N return using closes[i-n .. i]
        # (strictly past data — no lookahead into the holding period).
        # Ragged-panel aware: a token is only rankable if it has a
        # valid close both now and N days ago. Tokens not yet listed
        # have NaN and are excluded from this date's cross-section.
        now_px = closes.iloc[i]
        past_px = closes.iloc[i - n]
        trailing = (now_px / past_px - 1.0).dropna()
        ranked = trailing.sort_values(ascending=False)

        # Cash floor: only positive-momentum tokens are eligible.
        eligible = ranked[ranked > 0]
        new_held = list(eligible.index[:k])

        # Turnover cost: fraction of book that changes hands.
        prev = set(held)
        new = set(new_held)
        turnover = len(prev.symmetric_difference(new)) / (2 * max(k, 1))
        cost = turnover * cost_rate

        held = new_held
        equity_now = equity[-1] * (1.0 - cost)

        # Hold for H days, earning the equal-weighted daily return of
        # the held basket; cash earns 0. If a held token has a NaN
        # return on some day (shouldn't happen post-listing, but guard
        # anyway), it's dropped from that day's mean.
        end = min(i + h, len(dates) - 1)
        for j in range(i + 1, end + 1):
            if held:
                rets = daily_ret.iloc[j][held].dropna()
                day_ret = rets.mean() if len(rets) else 0.0
            else:
                day_ret = 0.0   # fully in cash
            equity_now = equity_now * (1.0 + day_ret)
            equity.append(equity_now)
            equity_dates.append(dates[j])
        i = end

    curve = pd.Series(equity, index=pd.DatetimeIndex(equity_dates))
    curve = curve[~curve.index.duplicated(keep="last")]
    return _metrics_from_curve(curve)


def run_benchmark_hold(closes: pd.DataFrame, symbol: str | None) -> dict:
    """Buy-and-hold benchmark. symbol=None means equal-weight all."""
    daily_ret = closes.pct_change().dropna()
    if symbol is None:
        port_ret = daily_ret.mean(axis=1)          # equal-weight all
    else:
        port_ret = daily_ret[symbol]
    curve = (1.0 + port_ret).cumprod()
    return _metrics_from_curve(curve)


def _metrics_from_curve(curve: pd.Series) -> dict:
    """CAGR, annualised vol, return/vol ratio, max drawdown, final."""
    if len(curve) < 2:
        return {"final": float("nan"), "cagr": float("nan"),
                "vol": float("nan"), "ret_vol": float("nan"),
                "max_dd": float("nan")}
    total_days = (curve.index[-1] - curve.index[0]).days or 1
    years = total_days / 365.0
    final = float(curve.iloc[-1])
    cagr = final ** (1.0 / years) - 1.0 if final > 0 else -1.0

    daily = curve.pct_change().dropna()
    vol = float(daily.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
    ret_vol = cagr / vol if vol > 0 else float("nan")

    running_max = curve.cummax()
    drawdown = curve / running_max - 1.0
    max_dd = float(drawdown.min())

    return {"final": final, "cagr": cagr, "vol": vol,
            "ret_vol": ret_vol, "max_dd": max_dd}


def main() -> int:
    parser = argparse.ArgumentParser(description="Momentum rotation backtest")
    parser.add_argument("--cost-bps", type=float, default=10.0,
                        help="Round-trip-ish transaction cost in basis "
                             "points per unit turnover (default 10 = 0.10%%).")
    parser.add_argument("--json", type=str, default=None,
                        help="Write full results to this JSON path.")
    args = parser.parse_args()

    cost_rate = args.cost_bps / 10000.0

    logger.info("Loading daily closes for %d symbols...", len(universe()))
    closes = load_daily_closes()
    logger.info("Loaded %d trading days, %s to %s",
                len(closes), closes.index[0].date(), closes.index[-1].date())

    # Benchmarks
    bench_btc = run_benchmark_hold(closes, "BTCUSDT")
    bench_eqw = run_benchmark_hold(closes, None)

    # Grid sweep
    results = []
    for n, k, h in product(LOOKBACKS_N, HOLD_TOP_K, REBALANCE_H):
        m = run_strategy(closes, n, k, h, cost_rate)
        m.update({"N": n, "K": k, "H": h})
        m["beats_btc"] = m["final"] > bench_btc["final"]
        m["beats_eqw"] = m["final"] > bench_eqw["final"]
        m["beats_btc_riskadj"] = (
            not np.isnan(m["ret_vol"]) and m["ret_vol"] > bench_btc["ret_vol"]
        )
        results.append(m)

    results.sort(key=lambda r: r["final"], reverse=True)

    # ── Report ──
    print()
    print("=" * 96)
    print("MOMENTUM ROTATION BACKTEST")
    print("=" * 96)
    print(f"Window: {closes.index[0].date()} to {closes.index[-1].date()} "
          f"({len(closes)} days)   universe={len(universe())} tokens   "
          f"cost={args.cost_bps:.0f}bps/turnover")
    print()
    print("BENCHMARKS")
    print(f"  {'hold BTC':<22} final={bench_btc['final']:.2f}x  "
          f"CAGR={bench_btc['cagr']*100:6.1f}%  vol={bench_btc['vol']*100:5.1f}%  "
          f"ret/vol={bench_btc['ret_vol']:.2f}  maxDD={bench_btc['max_dd']*100:6.1f}%")
    print(f"  {'equal-weight all':<22} final={bench_eqw['final']:.2f}x  "
          f"CAGR={bench_eqw['cagr']*100:6.1f}%  vol={bench_eqw['vol']*100:5.1f}%  "
          f"ret/vol={bench_eqw['ret_vol']:.2f}  maxDD={bench_eqw['max_dd']*100:6.1f}%")
    print()
    print("STRATEGY GRID (sorted by final equity; full grid, no cherry-picking)")
    print(f"  {'N':>3} {'K':>2} {'H':>3}  {'final':>7}  {'CAGR':>7}  "
          f"{'vol':>6}  {'ret/vol':>7}  {'maxDD':>7}  {'>BTC':>4} {'>EQW':>4} {'>BTCra':>6}")
    print("  " + "-" * 78)
    for r in results:
        print(f"  {r['N']:>3} {r['K']:>2} {r['H']:>3}  "
              f"{r['final']:>6.2f}x  {r['cagr']*100:>6.1f}%  "
              f"{r['vol']*100:>5.1f}%  {r['ret_vol']:>7.2f}  "
              f"{r['max_dd']*100:>6.1f}%  "
              f"{'Y' if r['beats_btc'] else '.':>4} "
              f"{'Y' if r['beats_eqw'] else '.':>4} "
              f"{'Y' if r['beats_btc_riskadj'] else '.':>6}")

    n_beat_btc = sum(r["beats_btc"] for r in results)
    n_beat_btc_ra = sum(r["beats_btc_riskadj"] for r in results)
    total = len(results)
    print()
    print("SUMMARY")
    print(f"  {n_beat_btc}/{total} configs beat hold-BTC on total return")
    print(f"  {n_beat_btc_ra}/{total} configs beat hold-BTC risk-adjusted (ret/vol)")
    print()
    if n_beat_btc <= total * 0.25:
        print("  READ: few configs beat BTC — likely no robust rotation edge;")
        print("  the ones that win are probably overfit to this window.")
    elif n_beat_btc >= total * 0.6:
        print("  READ: most configs beat BTC — the effect looks robust to")
        print("  parameter choice (less likely to be overfitting). Still")
        print("  conditioned on a bull-regime, survivorship-filtered window.")
    else:
        print("  READ: mixed — some configs beat BTC, many don't. Treat any")
        print("  single winner with suspicion; look for a contiguous region")
        print("  of the grid that wins, not isolated cells.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "window": [str(closes.index[0].date()), str(closes.index[-1].date())],
                "benchmarks": {"hold_btc": bench_btc, "equal_weight": bench_eqw},
                "grid": results,
            }, f, indent=2, default=str)
        print(f"\n  full results → {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
