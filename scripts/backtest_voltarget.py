"""
Volatility-targeting overlay on the momentum basket.

THE PROBLEM THIS ATTACKS
------------------------
`docs/FINDINGS_momentum.md`: diversified cross-sectional momentum (30/5/7)
beat buy-and-hold BTC across a full cycle — 14.7x, ret/vol 0.83 — but drew
down **−72.9%** at the 2022 bottom. That drawdown is why it is "a lead, not
a tradeable strategy": almost nobody sits through −73%.

The DEFENSIVE state was meant to fix that by DETECTING when to flee. Seven
candidates were tested and all failed (news-return, price vol/drawdown
breaker, news-systemic, ETF flows, funding level, funding momentum,
liquidation cascade). See docs/FINDINGS_*.md.

WHY THIS IS DIFFERENT
---------------------
Volatility targeting requires **no forecast**. It does not predict crashes;
it sizes the book inversely to *trailing realised* volatility, which is
observable. Crypto vol is strongly autocorrelated — high-vol periods cluster
— so scaling down after vol rises mechanically reduces exposure into
turbulent regimes without ever calling a top.

    exposure_t = clip(target_vol / realised_vol(t-1), 0, max_exposure)

The rest of the book sits in cash earning zero (same convention as the
momentum cash floor).

NO-LOOKAHEAD DISCIPLINE
-----------------------
The vol estimate for day t uses returns through **t-1 only**. This is the
same bug that produced the fake 746x price-breaker result
(`breaker.shift(1)` was the fix). Here it is enforced by construction:
`_trailing_vol()` shifts the rolling window by one day, and there is an
explicit assertion in the self-test.

Vol is estimated from the **unscaled** basket returns — sizing must not
depend on its own output, or the feedback loop makes the result
uninterpretable.

USAGE
-----
    python -m scripts.backtest_voltarget
    python -m scripts.backtest_voltarget --cost-bps 25
    python -m scripts.backtest_voltarget --self-test     # no DB needed
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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtest_voltarget")

TRADING_DAYS_PER_YEAR = 365

# Base momentum config: the full-cycle winner from FINDINGS_momentum.md.
# FIXED, not swept — this experiment tests the OVERLAY, not the base.
BASE_N, BASE_K, BASE_H = 30, 5, 7

# Overlay grid (full grid always reported — no cherry-picking).
TARGET_VOLS = (0.30, 0.50, 0.70)      # annualised
VOL_LOOKBACKS = (14, 30, 60)          # days of trailing returns
MAX_EXPOSURES = (1.0, 1.5)            # 1.0 = never lever; 1.5 = mild lever


def _trailing_vol(returns: pd.Series, lookback: int) -> pd.Series:
    """Annualised trailing vol, SHIFTED so day t only sees through t-1.

    The shift is the no-lookahead guarantee. Without it the sizing for
    day t would know day t's own return — the exact class of bug that
    produced the bogus 746x price-breaker result.
    """
    return (returns.rolling(lookback).std()
            * np.sqrt(TRADING_DAYS_PER_YEAR)).shift(1)


def base_basket_returns(closes: pd.DataFrame, n: int, k: int, h: int,
                        delist_map: dict | None = None,
                        delist_loss: float = 1.0) -> tuple:
    """Daily return series of the UNSCALED momentum basket.

    Mirrors run_strategy() in backtest_momentum.py: rank by trailing-N
    return on strictly-past closes, hold top-K with a positive-momentum
    cash floor, rebalance every H days. Returned as a daily series so
    the overlay can scale it; transaction costs are applied later so
    turnover and exposure costs don't get double-counted.

    DELISTING (delist_map): when a held token's price series ENDS while
    still held, it takes `delist_loss` and leaves the basket. Without
    this branch the NaN would be dropna()'d and the position would
    silently VANISH for free — the survivorship escape hatch documented
    in FINDINGS_survivorship.md. Pass delist_map={} for the
    survivors-only run, where no series ends early.
    """
    delist_map = delist_map or {}
    daily_ret = closes.pct_change()
    dates = closes.index
    out_dates, out_rets, out_turnover = [], [], []

    held: list[str] = []
    i = n
    while i < len(dates) - 1:
        now_px, past_px = closes.iloc[i], closes.iloc[i - n]
        trailing = (now_px / past_px - 1.0).dropna()
        ranked = trailing.sort_values(ascending=False)
        eligible = ranked[ranked > 0]
        new_held = list(eligible.index[:k])

        turnover = (len(set(held).symmetric_difference(set(new_held)))
                    / (2 * max(k, 1)))
        held = new_held

        end = min(i + h, len(dates) - 1)
        for pos, j in enumerate(range(i + 1, end + 1)):
            day = dates[j]
            if held:
                basket_size = len(held)
                contribs, still_held = [], []
                for sym in held:
                    dd = delist_map.get(sym)
                    if dd is not None and day > dd:
                        contribs.append(-delist_loss)   # charged once
                        continue
                    r = daily_ret.iloc[j].get(sym, np.nan)
                    if not pd.isna(r):
                        contribs.append(float(r))
                        still_held.append(sym)
                # divide by ORIGINAL basket size so a wiped name dilutes
                day_ret = (sum(contribs) / basket_size) if contribs else 0.0
                held = still_held
            else:
                day_ret = 0.0
            out_dates.append(day)
            out_rets.append(day_ret)
            out_turnover.append(turnover if pos == 0 else 0.0)
        i = end

    idx = pd.DatetimeIndex(out_dates)
    rets = pd.Series(out_rets, index=idx)
    turn = pd.Series(out_turnover, index=idx)
    rets = rets[~rets.index.duplicated(keep="last")]
    turn = turn[~turn.index.duplicated(keep="last")]
    return rets, turn


def run_voltarget(base_rets: pd.Series, turnover: pd.Series,
                  target_vol: float, lookback: int, max_exposure: float,
                  cost_rate: float, lend_apy: float = 0.0,
                  borrow_apy: float = 0.0) -> dict:
    """Apply the vol-target overlay to the base basket return series.

    YIELD LEG (lend_apy): when exposure < 1, the undeployed fraction is
    lent at `lend_apy` — this is the YIELD state of the three-state
    model, and it falls out of the risk process rather than requiring a
    trigger.

    BORROW COST (borrow_apy): when exposure > 1 the book is LEVERED, so
    there is no idle capital to lend and the excess is borrowed. Without
    this, the 1.5x configs would collect leverage AND yield for free,
    which would badly flatter them. Borrow is charged above lend rate,
    as in reality.

    NOTE ON RISK: stablecoin yield is NOT risk-free. It carries de-peg
    and protocol-failure tail risk — the specific catastrophic exposure
    of the YIELD state. These returns are credited as if certain; they
    are not. See docs/FINDINGS_news_systemic_null.md for why de-peg was
    itself a defensive-signal candidate.
    """
    rv = _trailing_vol(base_rets, lookback)

    exposure = (target_vol / rv).clip(upper=max_exposure)
    exposure = exposure.fillna(0.0)          # warm-up: no position
    exposure = exposure.clip(lower=0.0)

    scaled = base_rets * exposure

    # Costs: (a) basket turnover, scaled by how much book is deployed;
    #        (b) changing the exposure itself day to day.
    exposure_change = exposure.diff().abs().fillna(0.0)
    cost = (turnover * exposure + exposure_change) * cost_rate

    # YIELD leg: lend what isn't deployed, pay for what's borrowed.
    idle = (1.0 - exposure).clip(lower=0.0)
    borrowed = (exposure - 1.0).clip(lower=0.0)
    carry = idle * (lend_apy / 365.0) - borrowed * (borrow_apy / 365.0)

    curve = (1.0 + scaled + carry - cost).cumprod()
    m = _metrics_from_curve(curve)
    m["avg_exposure"] = float(exposure.mean())
    m["avg_idle"] = float(idle.mean())
    m["pct_days_reduced"] = float((exposure < 0.99).mean())
    return m


def _metrics_from_curve(curve: pd.Series) -> dict:
    """CAGR, annualised vol, return/vol, max drawdown, final."""
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
    max_dd = float((curve / running_max - 1.0).min())
    return {"final": final, "cagr": cagr, "vol": vol,
            "ret_vol": ret_vol, "max_dd": max_dd}


def self_test() -> int:
    """Verify the no-lookahead shift and the overlay mechanics. No DB."""
    rng = np.random.default_rng(0)
    idx = pd.date_range("2021-01-01", periods=600, freq="D")

    # Regime series: calm, then violent, then calm.
    vol_regime = np.concatenate([
        rng.normal(0, 0.01, 200),
        rng.normal(-0.002, 0.06, 200),   # high-vol drawdown regime
        rng.normal(0, 0.01, 200),
    ])
    rets = pd.Series(vol_regime, index=idx)
    turn = pd.Series(0.0, index=idx)

    # 1. shift correctness: vol at t must equal std of returns ending t-1
    rv = _trailing_vol(rets, 30)
    manual = rets.iloc[:-1].tail(30).std() * np.sqrt(365)
    assert abs(rv.iloc[-1] - manual) < 1e-12, "no-lookahead shift is wrong"
    print("  [ok] vol estimate at t uses only returns through t-1")

    # 2. exposure must fall in the high-vol regime
    r = run_voltarget(rets, turn, 0.30, 30, 1.0, 0.0)
    exp_series = (0.30 / _trailing_vol(rets, 30)).clip(upper=1.0).fillna(0)
    calm = exp_series.iloc[60:200].mean()
    storm = exp_series.iloc[230:390].mean()
    print(f"  [ok] exposure calm={calm:.2f} storm={storm:.2f}")
    assert storm < calm * 0.6, "overlay failed to de-risk in high vol"

    # 3. drawdown must improve vs unscaled
    unscaled = _metrics_from_curve((1.0 + rets).cumprod())
    print(f"  [ok] maxDD unscaled={unscaled['max_dd']:.1%} "
          f"targeted={r['max_dd']:.1%}")
    assert r["max_dd"] > unscaled["max_dd"], "overlay did not cut drawdown"

    # 4. warm-up period holds no position
    assert exp_series.iloc[:30].sum() == 0, "traded before warm-up complete"
    print("  [ok] no position during vol warm-up")

    # 5. YIELD leg: idle capital must ADD return, monotonically in APY
    y0 = run_voltarget(rets, turn, 0.30, 30, 1.0, 0.0, 0.00, 0.10)
    y5 = run_voltarget(rets, turn, 0.30, 30, 1.0, 0.0, 0.05, 0.10)
    y9 = run_voltarget(rets, turn, 0.30, 30, 1.0, 0.0, 0.09, 0.10)
    assert y5["cagr"] > y0["cagr"] and y9["cagr"] > y5["cagr"], \
        "yield leg not monotone in lend APY"
    print(f"  [ok] yield leg monotone: CAGR {y0['cagr']:.1%} → "
          f"{y5['cagr']:.1%} → {y9['cagr']:.1%} at 0/5/9% APY")

    # 6. Levered configs must PAY, not earn: with max_exposure > 1 and a
    #    punitive borrow rate, CAGR must fall vs zero borrow cost.
    lev_free = run_voltarget(rets, turn, 0.30, 30, 3.0, 0.0, 0.05, 0.00)
    lev_paid = run_voltarget(rets, turn, 0.30, 30, 3.0, 0.0, 0.05, 0.50)
    assert lev_paid["cagr"] < lev_free["cagr"], \
        "borrow cost not charged on levered exposure"
    print(f"  [ok] borrow charged when levered: CAGR "
          f"{lev_free['cagr']:.1%} → {lev_paid['cagr']:.1%}")

    # 7. Yield must not silently change the risk profile
    assert abs(y5["max_dd"] - y0["max_dd"]) < 0.05, \
        "yield leg distorted drawdown implausibly"
    print("  [ok] yield leg leaves drawdown broadly unchanged")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Vol-targeting overlay backtest")
    p.add_argument("--cost-bps", type=float, default=10.0,
                   help="Transaction cost in bps per unit turnover.")
    p.add_argument("--lend-apy", type=float, default=0.05,
                   help="APY earned on idle (undeployed) capital. "
                        "Default 0.05. Use 0 to disable the YIELD leg.")
    p.add_argument("--borrow-apy", type=float, default=0.10,
                   help="APY paid on borrowed capital when exposure > 1. "
                        "Default 0.10 (above lend rate, as in reality).")
    p.add_argument("--universe", choices=["survivors", "extended"],
                   default="survivors",
                   help="'survivors' = the 11 live tokens (INFLATED — every "
                        "published figure used this). 'extended' adds the "
                        "known-dead tokens.")
    p.add_argument("--delist-loss", type=float, default=1.0,
                   help="Loss when a held token's series ends (extended "
                        "universe only). 1.0 = total, 0.0 = exit at last "
                        "close.")
    p.add_argument("--self-test", action="store_true",
                   help="Run mechanics checks with synthetic data, no DB.")
    args = p.parse_args()

    if args.self_test:
        return self_test()

    from scripts.backtest_momentum import run_benchmark_hold
    from scripts.backtest_survivorship import (
        load_closes, build_delist_map, DEAD_CANDIDATES)
    from config.universe import universe

    cost_rate = args.cost_bps / 10_000.0
    survivors = list(universe())

    if args.universe == "extended":
        closes = load_closes(survivors + DEAD_CANDIDATES)
        dead_present = [s for s in DEAD_CANDIDATES if s in closes.columns]
        delist_map = build_delist_map(closes)
        if not dead_present:
            logger.error("--universe extended requested but no dead-token "
                         "data found. Run collectors/binance_archive.py "
                         "first; otherwise this is just the survivor run.")
            return 1
        logger.info("EXTENDED universe: %d survivors + %d dead %s",
                    len(survivors), len(dead_present), dead_present)
        logger.info("delisted: %s",
                    {k: str(v.date()) for k, v in delist_map.items()})
    else:
        closes = load_closes(survivors)
        delist_map = {}
        logger.info("SURVIVORS-ONLY universe: %d symbols "
                    "(inflated — see FINDINGS_survivorship.md)",
                    len(survivors))

    logger.info("Loaded %d days x %d symbols (%s … %s)",
                len(closes), closes.shape[1],
                closes.index[0].date(), closes.index[-1].date())

    base_rets, turnover = base_basket_returns(
        closes, BASE_N, BASE_K, BASE_H, delist_map, args.delist_loss)

    # Benchmarks
    # The extended panel starts earlier (2020-09) than BTCUSDT's history
    # (2020-11), so a naive hold-BTC curve begins on NaN and propagates.
    # Compute the benchmark on BTC's own valid range.
    btc_closes = closes[["BTCUSDT"]].dropna() if "BTCUSDT" in closes.columns \
        else closes
    btc = run_benchmark_hold(btc_closes, "BTCUSDT")
    base_curve = ((1.0 + base_rets - turnover * cost_rate).cumprod())
    base = _metrics_from_curve(base_curve)

    print(f"\n{'BENCHMARKS':-^92}")
    print(f"  universe: {args.universe.upper()}"
          + (f"  (delist_loss={args.delist_loss:.0%})"
             if args.universe == "extended" else
             "  — INFLATED, see FINDINGS_survivorship.md"))
    print(f"{'config':<28}{'final':>9}{'CAGR':>9}{'vol':>8}"
          f"{'ret/vol':>9}{'maxDD':>9}{'avgExp':>9}")
    print(f"{'hold BTC':<28}{btc['final']:>9.2f}{btc['cagr']:>9.1%}"
          f"{btc['vol']:>8.1%}{btc['ret_vol']:>9.2f}{btc['max_dd']:>9.1%}"
          f"{'-':>9}")
    print(f"{'momentum 30/5/7 (base)':<28}{base['final']:>9.2f}"
          f"{base['cagr']:>9.1%}{base['vol']:>8.1%}{base['ret_vol']:>9.2f}"
          f"{base['max_dd']:>9.1%}{1.0:>9.2f}")

    print(f"\n{'VOL-TARGET OVERLAY + YIELD LEG':-^104}")
    print(f"  lend APY on idle capital: {args.lend_apy:.1%}   "
          f"borrow APY when levered: {args.borrow_apy:.1%}")
    print(f"{'tgt/look/maxExp':<24}{'final':>9}{'CAGR':>9}{'vol':>8}"
          f"{'ret/vol':>9}{'maxDD':>9}{'avgExp':>9}{'avgIdle':>9}"
          f"{'noYield':>10}{'yieldAdd':>10}")

    rows = []
    for tv, lb, mx in product(TARGET_VOLS, VOL_LOOKBACKS, MAX_EXPOSURES):
        m0 = run_voltarget(base_rets, turnover, tv, lb, mx, cost_rate,
                           0.0, 0.0)
        m = run_voltarget(base_rets, turnover, tv, lb, mx, cost_rate,
                          args.lend_apy, args.borrow_apy)
        rows.append(((tv, lb, mx), m, m0))
        label = f"{tv:.0%}/{lb}d/{mx:.1f}x"
        print(f"{label:<24}{m['final']:>9.2f}{m['cagr']:>9.1%}"
              f"{m['vol']:>8.1%}{m['ret_vol']:>9.2f}{m['max_dd']:>9.1%}"
              f"{m['avg_exposure']:>9.2f}{m['avg_idle']:>9.2f}"
              f"{m0['cagr']:>10.1%}{m['cagr']-m0['cagr']:>+10.1%}")

    n_better_dd = sum(1 for _, m, _ in rows if m["max_dd"] > base["max_dd"])
    n_better_rv = sum(1 for _, m, _ in rows if m["ret_vol"] > base["ret_vol"])
    print(f"\n{len(rows)} configs: {n_better_dd} cut drawdown vs base, "
          f"{n_better_rv} improved ret/vol vs base.")
    print("noYield = same config with the YIELD leg off; yieldAdd = CAGR "
          "the idle capital contributes.")
    print("Read the FULL grid. A contiguous winning region is the test; "
          "isolated cells are noise (the price-breaker had 4/36).")
    print("\nCAVEAT: stablecoin yield is NOT risk-free. It carries de-peg "
          "and protocol tail risk,\ncredited here as if certain. A flat APY "
          "across 2020-2026 is also a simplification —\nreal rates ranged "
          "from ~20% (2021) to ~2% (2023). Sweep --lend-apy to see "
          "sensitivity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
