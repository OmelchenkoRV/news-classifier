"""
Does the taker buy/sell ratio predict anything?

PRE-REGISTRATION (written before any data was collected)
---------------------------------------------------------
Source: a CryptoQuant chart annotating the Binance ETH taker buy/sell ratio
7d MA at ~0.95 as "the most bearish level since June" (2026-09).

Two competing claims, both tested, neither favoured:

  BEARISH  (the chart's framing)   low ratio → DRAWDOWN follows
  CONTRARIAN (the chart's own precedent) low ratio → RALLY follows
      — the chart's arrow points back to June 2026, which was the cycle
        BOTTOM (~$1,600) followed by a rally to ~$2,680.

PRIMARY CELL, fixed in advance — this is THE result; the grid is context:
    market=futures, symbol=ETHUSDT, MA=7d, threshold=0.95,
    event = 7d MA crosses DOWN through 0.95,
    horizon = 14 days, magnitude = 10%.

PRIOR, on record: null or contrarian. Funding level (F1), the closest
analogue — a reflexive flow/positioning level — fired below baseline in
every cell because it goes extreme AFTER capitulation
(FINDINGS_funding_defensive.md). Taker selling should behave the same way.

METHOD (rules this project arrived at)
--------------------------------------
* EVENTS ARE CROSSINGS, NOT DAYS. Q5 counted every day a signal was on, so
  a 10-day cluster became 10 "firings" with overlapping outcome windows —
  pseudo-replication that inflates n and significance. Here a firing is a
  downward crossing, plus a refractory period so one episode counts once.
* Era-specific baselines (pooled baselines hid the funding era structure).
* Contiguity over the full grid, not peaks.
* Cross-asset (ETH, BTC) and cross-market (futures, spot) replication.
* No lookahead: signal uses data through close of day t; outcome window is
  strictly (t, t+H].

USAGE
-----
    python -m scripts.test_taker_ratio --self-test
    python -m scripts.test_taker_ratio
    python -m scripts.test_taker_ratio --market spot
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

PRIMARY = dict(market="futures", symbol="ETHUSDT", ma=7, thr=0.95,
               h=14, mag=0.10)

GRID_MA = (7, 14)
GRID_THR = (0.94, 0.95, 0.96, 0.97)
GRID_H = (7, 14, 30)
GRID_MAG = (0.10, 0.15)
REFRACTORY_DAYS = 14     # one episode counts once


def era_of(d: pd.Timestamp) -> str:
    if d < pd.Timestamp("2020-01-01"):
        return "2017-2019"
    if d < pd.Timestamp("2023-01-01"):
        return "2020-2022"
    return "2023-2026"


def load(market: str, symbol: str) -> pd.DataFrame:
    from config.database import get_connection, get_cursor
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT d, close, volume, taker_buy_base
            FROM taker_flow
            WHERE market = %s AND symbol = %s
            ORDER BY d
        """, (market, symbol))
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows) if isinstance(rows[0], dict)
          else pd.DataFrame(rows, columns=["d", "close", "volume",
                                           "taker_buy_base"]))
    df["d"] = pd.to_datetime(df["d"])
    return df.set_index("d").sort_index()


def add_ratio(df: pd.DataFrame) -> pd.DataFrame:
    sell = df["volume"] - df["taker_buy_base"]
    df = df.copy()
    df["ratio"] = np.where(sell > 0, df["taker_buy_base"] / sell, np.nan)
    return df


def forward_outcomes(close: pd.Series, h: int) -> pd.DataFrame:
    """Max gain and max drawdown over (t, t+h], strictly after t."""
    c = close.values
    n = len(c)
    up = np.full(n, np.nan)
    dn = np.full(n, np.nan)
    for i in range(n):
        j_end = i + h
        if j_end >= n:
            break
        window = c[i + 1: j_end + 1]          # excludes day t itself
        up[i] = window.max() / c[i] - 1.0
        dn[i] = window.min() / c[i] - 1.0
    return pd.DataFrame({"up": up, "dn": dn}, index=close.index)


def crossings(ma: pd.Series, thr: float, refractory: int) -> pd.DatetimeIndex:
    """Days the MA crosses DOWN through thr, de-clustered."""
    below = ma < thr
    cross = below & ~below.shift(1, fill_value=False)
    days = list(ma.index[cross.fillna(False)])
    out, last = [], None
    for d in days:
        if last is None or (d - last).days >= refractory:
            out.append(d)
            last = d
    return pd.DatetimeIndex(out)


def binom_z(hits: int, n: int, p: float) -> float:
    if n == 0 or p <= 0 or p >= 1:
        return float("nan")
    return (hits - n * p) / math.sqrt(n * p * (1 - p))


def evaluate(df: pd.DataFrame, ma_win: int, thr: float, h: int,
             mag: float) -> dict:
    ma = df["ratio"].rolling(ma_win).mean()
    fo = forward_outcomes(df["close"], h)
    ev = crossings(ma, thr, REFRACTORY_DAYS)
    ev = ev[fo.loc[ev, "up"].notna().values] if len(ev) else ev

    valid = fo.dropna()
    eras = valid.index.map(era_of)
    res = {"n": len(ev), "eras": {}}
    for direction, col, test in (("bear", "dn", lambda x: x <= -mag),
                                 ("contra", "up", lambda x: x >= mag)):
        base_by_era = valid.groupby(eras)[col].apply(lambda s: test(s).mean())
        hits = test(fo.loc[ev, col]) if len(ev) else pd.Series(dtype=bool)
        hit_rate = float(hits.mean()) if len(ev) else float("nan")
        # Expected hits under era-matched baselines
        exp = sum(base_by_era.get(era_of(d), np.nan) for d in ev)
        res[f"{direction}_hit"] = hit_rate
        res[f"{direction}_exp"] = exp / len(ev) if len(ev) else float("nan")
        res[f"{direction}_edge"] = (hit_rate - exp / len(ev)) * 100 \
            if len(ev) else float("nan")
        res[f"{direction}_hits"] = int(hits.sum()) if len(ev) else 0
        res[f"{direction}_z"] = (binom_z(int(hits.sum()), len(ev),
                                         exp / len(ev))
                                 if len(ev) else float("nan"))
        for era in sorted(set(eras)):
            e_ev = [d for d in ev if era_of(d) == era]
            if not e_ev:
                continue
            e_hits = int(test(fo.loc[e_ev, col]).sum())
            b = float(base_by_era.get(era, np.nan))
            res["eras"].setdefault(era, {})[direction] = (
                len(e_ev), e_hits, b)
    res["events"] = list(ev)
    return res


def report(df, label, primary_only=False):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    if df.empty:
        print("  NO DATA — run collectors/taker_flow_backfill.py first")
        return
    print(f"  {len(df)} days, {df.index[0].date()} … {df.index[-1].date()}, "
          f"ratio mean {df['ratio'].mean():.3f}, "
          f"7d-MA range {df['ratio'].rolling(7).mean().min():.3f}"
          f"–{df['ratio'].rolling(7).mean().max():.3f}")

    p = PRIMARY
    r = evaluate(df, p["ma"], p["thr"], p["h"], p["mag"])
    print(f"\n  PRIMARY (pre-registered): MA{p['ma']} ↓{p['thr']}, "
          f"{p['h']}d, {p['mag']:.0%}   n={r['n']} crossings")
    for d, name in (("bear", "BEARISH  (drawdown ≥10%)"),
                    ("contra", "CONTRARIAN (rally ≥10%) ")):
        print(f"    {name}: {r[d+'_hits']}/{r['n']} = {r[d+'_hit']:.1%} "
              f"vs era-baseline {r[d+'_exp']:.1%}  "
              f"edge {r[d+'_edge']:+.1f}pp  z={r[d+'_z']:+.2f}")
    print("    by era (n, hits, baseline):")
    for era, dd in sorted(r["eras"].items()):
        s = "  ".join(f"{k}: n={v[0]} hits={v[1]} base={v[2]:.1%}"
                      for k, v in dd.items())
        print(f"      {era}: {s}")
    if r["n"] < 15:
        print(f"    ⚠ n={r['n']} < 15 — uninformative regardless of edge")

    if primary_only:
        return
    print(f"\n  GRID (edge pp vs era baseline; context, not the result)")
    print(f"  {'MA':>3}{'thr':>6}{'H':>4}{'mag':>5}{'n':>5}"
          f"{'bear':>8}{'contra':>8}")
    pos_b = pos_c = cells = 0
    for ma, thr, h, mag in product(GRID_MA, GRID_THR, GRID_H, GRID_MAG):
        g = evaluate(df, ma, thr, h, mag)
        if g["n"] == 0:
            continue
        cells += 1
        pos_b += g["bear_edge"] > 0
        pos_c += g["contra_edge"] > 0
        print(f"  {ma:>3}{thr:>6.2f}{h:>4}{mag:>5.0%}{g['n']:>5}"
              f"{g['bear_edge']:>+8.1f}{g['contra_edge']:>+8.1f}")
    print(f"\n  cells with positive edge: bearish {pos_b}/{cells}, "
          f"contrarian {pos_c}/{cells}")


def self_test() -> int:
    rng = np.random.default_rng(1)
    idx = pd.date_range("2018-01-01", periods=1500, freq="D")

    # 1. Planted CONTRARIAN effect: ratio dips, then price rallies.
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 1500)))
    ratio_raw = rng.normal(1.0, 0.03, 1500)
    for start in range(100, 1400, 120):
        ratio_raw[start:start + 10] = 0.88
        close[start + 11:] *= 1.25            # rally after the dip
    vol = np.full(1500, 1000.0)
    tb = vol * ratio_raw / (1 + ratio_raw)    # invert ratio → taker_buy
    df = add_ratio(pd.DataFrame({"close": close, "volume": vol,
                                 "taker_buy_base": tb}, index=idx))
    assert abs(df["ratio"].iloc[50] - ratio_raw[50]) < 1e-9, \
        "ratio reconstruction wrong"
    print("  [ok] ratio = taker_buy / (volume - taker_buy) reconstructs exactly")

    r = evaluate(df, 7, 0.95, 14, 0.10)
    print(f"  planted contrarian: n={r['n']}, contra edge "
          f"{r['contra_edge']:+.1f}pp, bear edge {r['bear_edge']:+.1f}pp")
    assert r["contra_edge"] > 20, "failed to detect a planted effect"

    # 2. Pure noise: no edge expected.
    close2 = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 1500)))
    ratio2 = rng.normal(1.0, 0.03, 1500)
    df2 = add_ratio(pd.DataFrame({"close": close2, "volume": vol,
                                  "taker_buy_base": vol * ratio2 / (1 + ratio2)},
                                 index=idx))
    r2 = evaluate(df2, 7, 0.97, 14, 0.10)
    print(f"  noise: n={r2['n']}, contra edge {r2['contra_edge']:+.1f}pp, "
          f"bear edge {r2['bear_edge']:+.1f}pp")
    assert abs(r2["contra_edge"]) < 20 and abs(r2["bear_edge"]) < 20

    # 3. No lookahead: outcome must exclude day t.
    c = pd.Series([100.0, 50.0, 100.0, 100.0], index=idx[:4])
    fo = forward_outcomes(c, 2)
    assert fo["dn"].iloc[0] == -0.5 and fo["up"].iloc[1] == 1.0
    c2 = pd.Series([100.0, 100.0, 100.0, 100.0], index=idx[:4])
    assert forward_outcomes(c2, 2)["dn"].iloc[0] == 0.0
    print("  [ok] outcome window is strictly (t, t+h]")

    # 4. Crossings are de-clustered.
    ma = pd.Series([1.0, 0.9, 0.9, 1.0, 0.9, 1.0, 1.0] + [1.0] * 20
                   + [0.9], index=idx[:28])
    cx = crossings(ma, 0.95, 14)
    assert len(cx) == 2, cx           # day1 counted, day4 suppressed, day27 counted
    print("  [ok] crossings de-clustered with refractory period")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Taker buy/sell ratio test")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--market", choices=["futures", "spot"], default=None,
                    help="Restrict to one market (default: both).")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    markets = [args.market] if args.market else ["futures", "spot"]
    for mkt in markets:
        for sym in ("ETHUSDT", "BTCUSDT"):
            primary = (mkt == PRIMARY["market"] and sym == PRIMARY["symbol"])
            df = load(mkt, sym)
            if not df.empty:
                df = add_ratio(df)
            report(df, f"{mkt.upper()} {sym}"
                   + ("  ← PRIMARY" if primary else "  (replication)"),
                   primary_only=not primary)
    print("\nRead the PRIMARY cell first. The grid and replications are "
          "context.\nA result on one market or one asset only is a weaker "
          "claim.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
