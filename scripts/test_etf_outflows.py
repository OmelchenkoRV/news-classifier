"""
Do ETF outflow extremes precede drawdowns?

PRE-REGISTRATION (written before running against real data, 2026-10-07)
-----------------------------------------------------------------------
Trigger: on 2026-10-06 the ETH spot-ETF 5-day net flow reached -$354M, the
6.9th percentile of its history, after five consecutive outflow days — and
the outflows occurred during US hours BEFORE that evening's liquidation
flush. The question raised: do ETF outflow extremes precede drawdowns?

This is NOT what candidate #5 tested. That test (q4_flow_false_positive.sql)
asked whether BTC INFLOW crossovers predict RALLIES. Outflow extremes as a
DRAWDOWN predictor were never tested, for either asset.

PRIMARY CELL, fixed in advance:
    asset=ETH, 5-day trading-window net flow,
    event = 5d sum crosses DOWN into its bottom 10% (expanding percentile),
    horizon = 14 days, magnitude = 10% drawdown.

Both directions are reported (drawdown and rally), as in the taker test.
BTC is the replication.

PRIOR, on record: uninformative or null. ETF flows were coincident with
price in both the August and September 2026 moves, and ETH ETF history
starts 2024-07-23 — roughly two years and one cycle, so the primary cell
will have perhaps a dozen independent events. It may be uninformative on
n alone, and that is an acceptable outcome.

METHOD — two traps specific to this test
----------------------------------------
1. NO FULL-SAMPLE PERCENTILE. "Bottom decile of history" computed over the
   whole sample would judge a 2024 event against flows from 2026. The
   threshold at date t is the expanding percentile of 5d sums strictly
   BEFORE t, with a 60-trading-day warm-up.
2. PUBLICATION LAG. Flows settle after the US close and are often published
   the next morning. Entry is the close of the FOLLOWING calendar day, and
   the outcome window is strictly after entry.

Plus the rules carried over: crossings not days (14-day refractory),
era-specific baselines, cross-asset replication.

WHAT WAS TRIED AND REMOVED
--------------------------
While building this, a render on PURE NOISE showed +32.9pp, z=+3.10 in the
primary cell, with a drawdown column that looked contiguous across the
grid. A 60-seed check showed the harness is unbiased (mean edge -0.5pp,
z sd 0.83) — that render was a rare draw. Two lessons:

* Grid contiguity is NOT independent evidence here: neighbouring cells
  share most of their events, so one lucky event set propagates through
  the whole grid. Only independent evidence counts — other assets, other
  eras, non-overlapping events.
* A circular-shift permutation null was added to measure the noise floor
  on real data. It FAILED calibration on synthetic noise (mean p 0.38 vs
  0.50 expected; 12.5% of datasets below p=0.05 vs 5%) and was removed.
  The binomial z, which passed, is used instead. `--calibrate` reruns that
  check on demand.

USAGE
-----
    python -m scripts.test_etf_outflows --self-test
    python -m scripts.test_etf_outflows
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ASSETS = {"ETH": ("ETHEREUM-TOTAL", "ETHUSDT"),
          "BTC": ("BITCOIN-TOTAL", "BTCUSDT")}
PRIMARY = dict(asset="ETH", win=5, pct=0.10, h=14, mag=0.10)

GRID_WIN = (3, 5, 10)
GRID_PCT = (0.05, 0.10, 0.20)
GRID_H = (7, 14, 30)
GRID_MAG = (0.10, 0.15)

WARMUP = 60          # trading days before a threshold is trusted
REFRACTORY = 14      # calendar days; one episode counts once
ENTRY_LAG = 1        # calendar days after flow_date before entry


def era_of(d: pd.Timestamp) -> str:
    return "2024H2-2025H1" if d < pd.Timestamp("2025-07-01") else "2025H2-2026"


# ---------------------------------------------------------------- data
def load(asset: str) -> tuple[pd.Series, pd.Series]:
    from config.database import get_connection, get_cursor
    ticker, symbol = ASSETS[asset]
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("""SELECT flow_date, net_flow_usd FROM eth_etf_flows
                       WHERE ticker = %s ORDER BY flow_date""", (ticker,))
        f = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, close
            FROM price_snapshots WHERE symbol = %s
            ORDER BY timestamp::date, timestamp DESC""", (symbol,))
        p = cur.fetchall()
    finally:
        conn.close()

    def ser(rows, k, v):
        if not rows:
            return pd.Series(dtype=float)
        if isinstance(rows[0], dict):
            idx = [r[k] for r in rows]; val = [r[v] for r in rows]
        else:
            idx = [r[0] for r in rows]; val = [r[1] for r in rows]
        return pd.Series(val, index=pd.to_datetime(idx), dtype=float)

    return ser(f, "flow_date", "net_flow_usd"), ser(p, "d", "close")


# ------------------------------------------------------------ mechanics
def rolling_sum(flows: pd.Series, win: int) -> pd.Series:
    """Trading-day rolling sum. Requires a full window — partial windows
    at the start of history would read as artificially small."""
    return flows.rolling(win, min_periods=win).sum()


def expanding_threshold(s: pd.Series, pct: float) -> pd.Series:
    """Percentile of values STRICTLY BEFORE each date. No lookahead."""
    thr = s.expanding(min_periods=WARMUP).quantile(pct).shift(1)
    return thr


def events(s: pd.Series, thr: pd.Series) -> pd.DatetimeIndex:
    below = (s < thr) & thr.notna()
    cross = below & ~below.shift(1, fill_value=False)
    out, last = [], None
    for d in s.index[cross.fillna(False)]:
        if last is None or (d - last).days >= REFRACTORY:
            out.append(d); last = d
    return pd.DatetimeIndex(out)


def outcome(price: pd.Series, d: pd.Timestamp, h: int) -> tuple | None:
    """Entry at close of d+ENTRY_LAG; window strictly after entry."""
    entry_day = d + pd.Timedelta(days=ENTRY_LAG)
    if entry_day not in price.index:
        later = price.index[price.index >= entry_day]
        if len(later) == 0:
            return None
        entry_day = later[0]
    end = entry_day + pd.Timedelta(days=h)
    if price.index[-1] < end:
        return None                      # outcome not yet observable
    p0 = price.loc[entry_day]
    w = price.loc[(price.index > entry_day) & (price.index <= end)]
    if w.empty or not p0:
        return None
    return w.min() / p0 - 1.0, w.max() / p0 - 1.0


_BASE_CACHE: dict = {}


def baselines(price: pd.Series, start: pd.Timestamp, h: int, mag: float
              ) -> dict:
    """Unconditional rates per era, over every calendar day in the span.

    Cached: baselines depend only on price, start, h and mag — not on the
    flows — so the permutation null reuses them across shifts.
    """
    key = (id(price), start, h, mag)
    if key in _BASE_CACHE:
        return _BASE_CACHE[key]
    rows = []
    for d in price.index[price.index >= start]:
        o = outcome(price, d - pd.Timedelta(days=ENTRY_LAG), h)
        if o is not None:
            rows.append((era_of(d), o[0] <= -mag, o[1] >= mag))
    df = pd.DataFrame(rows, columns=["era", "dd", "ru"])
    out = {e: (g["dd"].mean(), g["ru"].mean(), len(g))
           for e, g in df.groupby("era")}
    _BASE_CACHE[key] = out
    return out


def binom_z(hits, n, p):
    if n == 0 or not (0 < p < 1):
        return float("nan")
    return (hits - n * p) / math.sqrt(n * p * (1 - p))


def evaluate(flows, price, win, pct, h, mag) -> dict:
    s = rolling_sum(flows, win)
    thr = expanding_threshold(s, pct)
    ev = events(s, thr)
    start = thr.first_valid_index()
    base = baselines(price, start, h, mag) if start is not None else {}

    res = {"n": 0, "events": [], "eras": {}}
    for k in ("dd", "ru"):
        res[k + "_hits"] = 0; res[k + "_exp"] = 0.0
    for d in ev:
        o = outcome(price, d, h)
        era = era_of(d)
        if o is None or era not in base:
            continue
        dd_hit, ru_hit = o[0] <= -mag, o[1] >= mag
        b_dd, b_ru, _ = base[era]
        res["n"] += 1
        res["dd_hits"] += dd_hit; res["ru_hits"] += ru_hit
        res["dd_exp"] += b_dd;    res["ru_exp"] += b_ru
        res["events"].append((d, s.loc[d], thr.loc[d], o[0], o[1]))
        e = res["eras"].setdefault(era, [0, 0, 0, b_dd, b_ru])
        e[0] += 1; e[1] += dd_hit; e[2] += ru_hit
    n = res["n"]
    for k in ("dd", "ru"):
        if n:
            rate, exp = res[k + "_hits"] / n, res[k + "_exp"] / n
            res[k + "_rate"], res[k + "_base"] = rate, exp
            res[k + "_edge"] = (rate - exp) * 100
            res[k + "_z"] = binom_z(res[k + "_hits"], n, exp)
        else:
            res[k + "_rate"] = res[k + "_base"] = float("nan")
            res[k + "_edge"] = res[k + "_z"] = float("nan")
    return res


def calibrate(n_datasets: int = 60, seed: int = 1000) -> dict:
    """Measure the noise band of the PRIMARY cell on synthetic data where
    flows and price are independent by construction.

    Reports the spread of edges and z-scores under pure noise, so the real
    result can be judged against a measured floor rather than intuition.
    The binomial z passed this check (sd ~0.83, ~2% beyond 1.96 — slightly
    conservative). A circular-shift permutation null was also built and
    FAILED it (mean p 0.38, 12.5% of noise datasets below 0.05) and was
    removed; see the module docstring.
    """
    days = pd.bdate_range("2024-07-23", "2026-10-06")
    cal = pd.date_range(days[0], "2026-10-07")
    p = PRIMARY
    edges, zs, ns = [], [], []
    for i in range(n_datasets):
        rng = np.random.default_rng(seed + i)
        f = pd.Series(rng.normal(10e6, 90e6, len(days)), index=days)
        px = pd.Series(2500 * np.exp(np.cumsum(rng.normal(0, 0.03, len(cal)))),
                       index=cal)
        r = evaluate(f, px, p["win"], p["pct"], p["h"], p["mag"])
        if r["n"]:
            edges.append(r["dd_edge"]); zs.append(r["dd_z"]); ns.append(r["n"])
    e, z = np.array(edges), np.array(zs)
    return {"runs": len(e), "n_mean": float(np.mean(ns)),
            "edge_mean": float(e.mean()), "edge_sd": float(e.std()),
            "z_mean": float(z.mean()), "z_sd": float(z.std()),
            "fp_196": float(np.mean(z > 1.96))}


# -------------------------------------------------------------- report
def report(asset: str, flows, price, primary: bool):
    tag = "  ← PRIMARY" if primary else "  (replication)"
    print(f"\n{'=' * 78}\n{asset} ETF flows{tag}\n{'=' * 78}")
    if flows.empty or price.empty:
        print("  NO DATA — refresh with: python -m capture.etf_flows")
        return
    print(f"  flows {flows.index[0].date()} … {flows.index[-1].date()} "
          f"({len(flows)} trading days); prices to {price.index[-1].date()}")

    p = PRIMARY
    r = evaluate(flows, price, p["win"], p["pct"], p["h"], p["mag"])
    print(f"\n  PRIMARY: {p['win']}d sum ↓ bottom {p['pct']:.0%} "
          f"(expanding), {p['h']}d, {p['mag']:.0%}   n={r['n']} events")
    for k, name in (("dd", "DRAWDOWN ≥10%"), ("ru", "RALLY ≥10%   ")):
        print(f"    {name}: {r[k+'_hits']}/{r['n']} = {r[k+'_rate']:.1%} "
              f"vs era-baseline {r[k+'_base']:.1%}   "
              f"edge {r[k+'_edge']:+.1f}pp  z={r[k+'_z']:+.2f}")
    print("    by era  (n, drawdown hits, rally hits | baselines dd/ru):")
    for era, (n, dh, rh, bd, br) in sorted(r["eras"].items()):
        print(f"      {era}: n={n} dd={dh} ru={rh} | {bd:.1%} / {br:.1%}")
    if r["n"] < 15:
        print(f"    ⚠ n={r['n']} < 15 — UNINFORMATIVE regardless of edge")

    print("    noise floor: binomial z calibrated on synthetic noise "
          "(run --calibrate);\n    |z| < 2 is within noise.")

    print("\n    events (flow_date, 5d sum, threshold, worst, best):")
    for d, sv, tv, lo, hi in r["events"]:
        print(f"      {d.date()}  {sv/1e6:>8.1f}M  thr {tv/1e6:>8.1f}M  "
              f"worst {lo:+6.1%}  best {hi:+6.1%}")

    if not primary:
        return
    print(f"\n  GRID (edge pp vs era baseline; context, not the result)")
    print("  NB: neighbouring cells share most of their events, so a run of "
          "positive\n  cells is NOT independent confirmation.")
    print(f"  {'win':>4}{'pct':>6}{'H':>4}{'mag':>5}{'n':>4}"
          f"{'drawdown':>10}{'rally':>8}")
    pos_dd = pos_ru = cells = 0
    for win, pct, h, mag in product(GRID_WIN, GRID_PCT, GRID_H, GRID_MAG):
        g = evaluate(flows, price, win, pct, h, mag)
        if g["n"] == 0:
            continue
        cells += 1
        pos_dd += g["dd_edge"] > 0
        pos_ru += g["ru_edge"] > 0
        flag = "  n<15" if g["n"] < 15 else ""
        print(f"  {win:>4}{pct:>6.0%}{h:>4}{mag:>5.0%}{g['n']:>4}"
              f"{g['dd_edge']:>+10.1f}{g['ru_edge']:>+8.1f}{flag}")
    print(f"\n  cells with positive edge: drawdown {pos_dd}/{cells}, "
          f"rally {pos_ru}/{cells}")


# ----------------------------------------------------------- self-test
def self_test() -> int:
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2024-07-23", periods=560)
    cal = pd.date_range(days[0], days[-1] + pd.Timedelta(days=40))

    # 1. No lookahead in the threshold: a huge outflow later in the
    #    series must not change the threshold at an earlier date.
    f = pd.Series(rng.normal(0, 100e6, len(days)), index=days)
    s = rolling_sum(f, 5)
    t1 = expanding_threshold(s, 0.10)
    f2 = f.copy(); f2.iloc[-50:] = -5e9
    t2 = expanding_threshold(rolling_sum(f2, 5), 0.10)
    cut = days[-60]
    assert np.allclose(t1.loc[:cut].dropna(), t2.loc[:cut].dropna()), \
        "threshold leaked future data"
    print("  [ok] expanding threshold uses only past data")

    # 2. Entry lag: outcome starts AFTER d+1, so a crash on d+1 itself
    #    is not counted.
    px = pd.Series(100.0, index=cal)
    d0 = days[100]
    px.loc[d0 + pd.Timedelta(days=1)] = 50.0       # crash on entry day
    lo, hi = outcome(px, d0, 14)
    assert lo > -0.6, "entry-day move leaked into outcome"
    print("  [ok] entry is d+1 close; window strictly after entry")

    # 3. Planted effect: big outflows followed by drawdowns is detected.
    flows = pd.Series(rng.normal(20e6, 80e6, len(days)), index=days)
    price = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(cal)))),
                      index=cal)
    # IRREGULAR intervals: a perfectly periodic plant lets a circular
    # shift realign clusters with crashes, which made the first version
    # of the permutation self-test fail for a reason unrelated to method.
    starts, i = [], 80
    while i < 530:
        starts.append(i)
        i += int(rng.integers(22, 55))
    for i in starts:
        flows.iloc[i:i + 4] = -400e6
        d = days[i + 3]
        start = cal.get_loc(d) + 2
        price.iloc[start:] *= 0.85
    r = evaluate(flows, price, 5, 0.10, 14, 0.10)
    print(f"  planted effect: n={r['n']}, drawdown edge "
          f"{r['dd_edge']:+.1f}pp, rally edge {r['ru_edge']:+.1f}pp")
    assert r["n"] >= 8 and r["dd_edge"] > 30

    # 4. Pure noise: no meaningful edge.
    fn = pd.Series(rng.normal(0, 100e6, len(days)), index=days)
    pn = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(cal)))),
                   index=cal)
    rn = evaluate(fn, pn, 5, 0.10, 14, 0.10)
    print(f"  noise: n={rn['n']}, drawdown edge {rn['dd_edge']:+.1f}pp")
    assert abs(rn["dd_edge"]) < 30

    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF outflow → drawdown test")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--calibrate", action="store_true",
                    help="Measure the primary cell's noise band on "
                         "synthetic independent data (~30s).")
    a = ap.parse_args()
    if a.calibrate:
        c = calibrate()
        print(f"{c['runs']} noise datasets, mean n={c['n_mean']:.1f}")
        print(f"  drawdown edge under noise: mean {c['edge_mean']:+.1f}pp, "
              f"sd {c['edge_sd']:.1f}pp")
        print(f"  z under noise: mean {c['z_mean']:+.2f}, sd {c['z_sd']:.2f}; "
              f"{c['fp_196']:.1%} exceed +1.96 (nominal 2.5%)")
        return 0
    if a.self_test:
        return self_test()
    for asset in ("ETH", "BTC"):
        flows, price = load(asset)
        report(asset, flows, price, primary=(asset == PRIMARY["asset"]))
    print("\nRead the PRIMARY cell first; grid and BTC are context. "
          "With ~2 years of\nETF history, an uninformative result on n is "
          "an acceptable outcome.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
