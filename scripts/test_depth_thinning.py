"""
Does THIN futures depth near price warn of bigger moves than the corridor
expects?

MOTIVATION
----------
Every corridor fix (v2 mean reversion, v3 implied vol, c1 calm-history
calibration) left calm-regime coverage at ~79-83% for a nominal 90%: calm
spells end in jumps that past returns cannot see. Fix 3 asked for data that
might flag WHICH calm spells break. Thin order books are a candidate: when
resting liquidity near price is unusually low, a given flow of market
orders moves price further. This asks about the SIZE of the next move, not
its direction — the one kind of question this project has found answerable.

PRE-REGISTRATION (written 2026-10-08, before any bookDepth data was loaded)
--------------------------------------------------------------------------
Data: Binance USD-M futures bookDepth archive (collectors/bookdepth_backfill.py),
BTCUSDT and ETHUSDT, from its first day; prices: taker_flow spot closes.

Predictor at the close of UTC day t:
  D_t  = log of the day's mean USD notional within +-1% (bid + ask), from
         15-minute buckets of quality-checked snapshots; needs >= 80 of 96
         buckets, else missing.
  z_t  = (D_t - median of D over days t-90..t-1) / (1.4826 x MAD of the
         same), needing >= 60 valid days. Negative z = thinner than usual.
         Trailing only: no lookahead, and it removes slow trends in depth.
Outcome over the next h days:
  S_t  = log( sqrt(mean of r^2 over t+1..t+h) / sigma_t ), sigma_t = EWMA
         daily vol (lambda 0.94) at the close of t — the vol SURPRISE
         relative to what the v1 corridor assumes. PRIMARY h = 7.

Hypotheses and prior, on record
  D1 (PRIMARY) Spearman rho(z_t, S_t) < 0: thinner depth precedes
     higher-than-expected vol. Claim requires the 95% upper bound < 0 on
     BOTH BTC and ETH (moving-block bootstrap over days, 30-day blocks,
     2,000 resamples, seed 20261008).                  prior: yes, small (|rho| ~0.05-0.15)
  D2 stronger on calm days (real-time calm label): rho_calm < rho_all.
                                                         prior: unsure
  D3 survives controlling for implied vol: partial rho(z, S | log(DVOL/
     sigma)) < 0, upper bound < 0, on days with DVOL.    prior: weak
  D4 (descriptive) on calm days, v1 7-day 90% band breach rate when z < 0
     vs z >= 0.
Secondary also reported: h = 14; +-2% depth instead of +-1%.

Calibration of the D1 rule (synthetic, run 2026-10-08 before real data;
GARCH(0.10, 0.85, t4) prices, z an AR(1) with persistence 0.98 like real
depth, 1,350 days):
  null  z uninformative: median rho -0.004; rule passes 2.0% per asset
        (target 2.5%, one-sided) — the block bootstrap holds up despite
        the persistence of both series.
  power per asset by true effect:  rho ~ -0.11 -> 32.5% (200 runs)
                                   rho ~ -0.16 -> 61%   (100 runs)
                                   rho ~ -0.21 -> 85%   (100 runs)
  Both assets are required, so joint power is lower still. NOT SUPPORTED
  cannot rule out a small effect (|rho| ~ 0.1); SUPPORTED needs a
  moderate one.

CAVEATS ON RECORD
  * 2023-2026 has been examined for corridor behaviour; depth is a new
    information source, not a rescue of an earlier model, but the period
    is not fresh.
  * Depth falls when volatility rises (market makers widen). S is measured
    against EWMA vol, which already reflects recent volatility, so D1 asks
    for information BEYOND that — but some mechanical overlap remains.
  * bookDepth has known bad stretches (issue #431); snapshots failing the
    quality checks are excluded, and the share of bad days is reported.
NO RESCUE: thresholds and definitions are not changed after the result.

USAGE
-----
    python -m scripts.test_depth_thinning --self-test
    python -m scripts.test_depth_thinning --calibrate      # ~2 min
    python -m scripts.test_depth_thinning                  # the test
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.test_vol_corridor import ewma_vol, load_closes, log_returns  # noqa: E402

SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
WIN, MIN_WIN, MIN_BUCKETS = 90, 60, 80
HS = (7, 14)
BLOCK, N_BOOT, SEED = 30, 2000, 20261008


# ------------------------------------------------------------ measures
def daily_depth(df15: pd.DataFrame, pct: int = 1) -> pd.Series:
    """D_t from 15-minute buckets (columns ts, pct, notional_mean, n_good)."""
    x = df15[(df15["pct"].abs() == pct) & (df15["n_good"] > 0)]
    both = x.pivot_table(index="ts", columns="pct", values="notional_mean")
    both = both.dropna()
    if both.empty:
        return pd.Series(dtype=float)
    tot = both.sum(axis=1)
    day = pd.to_datetime(tot.index, utc=True).normalize().tz_localize(None)
    g = tot.groupby(day)
    mean, cnt = g.mean(), g.size()
    return np.log(mean.where(cnt >= MIN_BUCKETS))


def thinness(D: pd.Series) -> pd.Series:
    """Robust z vs the trailing 90 days (t-90..t-1)."""
    D = D.asfreq("D")
    prior = D.shift(1).rolling(WIN, min_periods=MIN_WIN)
    med = prior.median()
    mad = prior.apply(lambda a: np.nanmedian(np.abs(a - np.nanmedian(a))), raw=True)
    return (D - med) / (1.4826 * mad)


def surprise(px: pd.Series, h: int) -> tuple[pd.Series, pd.Series]:
    p = px.values.astype(float)
    r = log_returns(p)
    sig = ewma_vol(r)
    r2 = pd.Series(r * r, index=px.index)
    fwd = r2[::-1].rolling(h, min_periods=h).mean()[::-1].shift(-1)   # t+1..t+h
    S = 0.5 * np.log(fwd) - np.log(pd.Series(sig, index=px.index))
    return S, pd.Series(sig, index=px.index)


# -------------------------------------------------------------- stats
def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 10:
        return float("nan")
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    return float(np.corrcoef(rx, ry)[0, 1])


def partial_spearman(x, y, c) -> float:
    rx, ry, rc = (pd.Series(v).rank().values for v in (x, y, c))
    def resid(a, b):
        b1 = np.c_[np.ones(len(b)), b]
        return a - b1 @ np.linalg.lstsq(b1, a, rcond=None)[0]
    return float(np.corrcoef(resid(rx, rc), resid(ry, rc))[0, 1])


def block_ci(stat, arrays: list[np.ndarray], n_boot=N_BOOT, block=BLOCK, seed=SEED):
    n = len(arrays[0])
    if n < 2 * block:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    k = -(-n // block)
    vals = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, k)
        idx = (starts[:, None] + np.arange(block)).ravel()[:n]
        v = stat(*[a[idx] for a in arrays])
        if np.isfinite(v):
            vals.append(v)
    return float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))


def build_frame(z: pd.Series, px: pd.Series, h: int) -> pd.DataFrame:
    from scripts.test_vol_corridor_cfhs import realtime_labels
    S, sig = surprise(px, h)
    lab = pd.Series(realtime_labels(sig.values), index=px.index)
    df = pd.DataFrame({"z": z, "S": S, "sig": sig, "lab": lab}).dropna()
    return df[df["lab"] >= 0]


# --------------------------------------------------------------- data
def load_depth(sym: str) -> pd.DataFrame:
    from config.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT ts, pct, notional_mean, n_good FROM bookdepth_15m
                       WHERE symbol = %s AND abs(pct) IN (1, 2)""", (sym,))
        rows = cur.fetchall()
        cur.execute("""SELECT count(*), sum((bad > 0.05 * snapshots)::int)
                       FROM bookdepth_days WHERE symbol = %s""", (sym,))
        q = cur.fetchone()
    finally:
        conn.close()
    df = pd.DataFrame([tuple(r.values()) if isinstance(r, dict) else r for r in rows],
                      columns=["ts", "pct", "notional_mean", "n_good"])
    df.attrs["days"], df.attrs["bad_days"] = (q[0], q[1]) if not isinstance(q, dict) \
        else (q["count"], q["sum"])
    return df


def analyse(sym: str):
    from scripts.test_vol_corridor_iv import load_dvol
    df15 = load_depth(sym)
    px = load_closes(sym, True)
    print(f"\n{'=' * 78}\n{sym}\n{'=' * 78}")
    if df15.empty or px.empty:
        print("  NO DATA — run collectors.bookdepth_backfill (and taker_flow for prices)")
        return None
    print(f"  bookDepth days loaded {df15.attrs['days']}, with > 5% bad snapshots: "
          f"{df15.attrs['bad_days']}")
    res = {}
    for pct in (1, 2):
        D = daily_depth(df15, pct)
        z = thinness(D)
        for h in HS:
            f = build_frame(z, px, h)
            if len(f) < 100:
                continue
            x, y = f["z"].values, f["S"].values
            rho = spearman(x, y)
            lo, hi = block_ci(spearman, [x, y])
            calm = f[f["lab"] == 0]
            rc = spearman(calm["z"].values, calm["S"].values)
            tag = "PRIMARY" if (pct, h) == (1, 7) else "secondary"
            print(f"  +-{pct}% depth, h={h:>2}d ({tag}): n={len(f)} "
                  f"{f.index[0].date()}…{f.index[-1].date()}  rho {rho:+.3f} "
                  f"(95%: {lo:+.3f} to {hi:+.3f}); calm days rho {rc:+.3f} (n={len(calm)})")
            res[(pct, h)] = (rho, lo, hi, rc, f)
    if (1, 7) not in res:
        print("  too little overlap for the primary test")
        return None
    rho, lo, hi, rc, f = res[(1, 7)]
    # D3: control for implied vol, where DVOL exists
    dv = load_dvol(SYMBOLS[sym])
    if not dv.empty:
        g = f.join(dv.rename("iv"), how="inner").dropna()
        if len(g) > 100:
            c = np.log(g["iv"] / math.sqrt(365) / g["sig"]).values
            pr = partial_spearman(g["z"].values, g["S"].values, c)
            plo, phi = block_ci(partial_spearman, [g["z"].values, g["S"].values, c])
            print(f"  D3 partial rho given DVOL/EWMA: {pr:+.3f} (95%: {plo:+.3f} to {phi:+.3f}; n={len(g)})")
    # D4: calm-day corridor breaches, thin vs thick
    from scripts.test_vol_corridor_cfhs import coin_forecasts
    fc = coin_forecasts(px.values.astype(float), 7)
    if fc:
        j = 1                                           # LEVELS index of 90%
        lo_, hi_ = fc["v1"][0][:, j], fc["v1"][1][:, j]
        hit = (fc["R"] >= lo_) & (fc["R"] <= hi_)
        b = pd.DataFrame({"hit": hit}, index=px.index[fc["t"]]).join(f[["z", "lab"]], how="inner")
        b = b[b["lab"] == 0]
        if len(b):
            thin, thick = b[b["z"] < 0], b[b["z"] >= 0]
            print(f"  D4 calm days, v1 7d 90% band breached: thin depth "
                  f"{1 - thin['hit'].mean():.1%} (n={len(thin)}) vs thick "
                  f"{1 - thick['hit'].mean():.1%} (n={len(thick)})")
    ok = hi < 0
    print(f"  D1 (PRIMARY) {sym}: rho {rho:+.3f}, upper bound {hi:+.3f} -> "
          f"{'PASS' if ok else 'FAIL'} (needs < 0); D2 calm stronger: "
          f"{'yes' if rc < rho else 'no'}")
    return ok


# ---------------------------------------------------------- calibration
def sim(seed: int, n: int = 1350, w: float = 0.0):
    """GARCH(0.10, 0.85, t4) prices and an AR(1) z (phi 0.98). w > 0 mixes in
    the future vol surprise so that rho(z, S7) ~ -w."""
    from scripts.test_vol_corridor_v2 import _sim
    rng = np.random.default_rng(seed)
    p = _sim(n + 400, 0.10, 0.85, 4, seed)
    idx = pd.date_range("2022-01-01", periods=len(p))
    px = pd.Series(p, index=idx)
    S, _ = surprise(px, 7)
    e = np.empty(len(p)); e[0] = rng.normal()
    for i in range(1, len(p)):
        e[i] = 0.98 * e[i - 1] + math.sqrt(1 - 0.98 ** 2) * rng.normal()
    s = S.fillna(0).rank().values
    s = (s - s.mean()) / s.std()
    z = pd.Series(math.sqrt(1 - w * w) * e - w * s, index=idx)
    z.iloc[:400] = np.nan
    return z, px


def calibrate(n: int = 200, n_boot: int = 500) -> None:
    for label, w in (("null (z uninformative)", 0.0), ("alt (rho ~ -0.10)", 0.10)):
        passes, rhos = 0, []
        for i in range(n):
            z, px = sim(9000 + i, w=w)
            f = build_frame(z, px, 7)
            x, y = f["z"].values, f["S"].values
            rhos.append(spearman(x, y))
            _, hi = block_ci(spearman, [x, y], n_boot=n_boot)
            passes += hi < 0
        print(f"  {label}: median rho {np.median(rhos):+.3f}; rule passes "
              f"{passes / n:.1%} per asset (n={n})")
    print("  Both assets are required, so the joint false-positive rate is at most "
          "the per-asset rate (lower if the two are independent).")


# ------------------------------------------------------------ self-test
def self_test() -> int:
    # 1. daily depth: needs >= 80 buckets; log of the mean of bid+ask notional
    ts = pd.date_range("2025-01-01", periods=96 * 2, freq="15min", tz="UTC")
    rows = [(t, p, 100.0 if p < 0 else 50.0, 5) for t in ts for p in (-1, 1)]
    df = pd.DataFrame(rows, columns=["ts", "pct", "notional_mean", "n_good"])
    df = df[~((df["ts"] >= "2025-01-02") & (df["ts"] < "2025-01-02 05:00"))]   # 20 lost
    D = daily_depth(df, 1)
    assert abs(D.iloc[0] - math.log(150)) < 1e-12 and np.isnan(D.iloc[1]), D
    print("  [ok] daily depth: mean of bid+ask; days with < 80 buckets dropped")
    # 2. thinness uses only the past
    D = pd.Series(np.r_[np.zeros(100), np.ones(5) * -2.0],
                  index=pd.date_range("2025-01-01", periods=105)) + \
        np.random.default_rng(0).normal(0, 0.1, 105)
    z = thinness(D)
    D2 = D.copy(); D2.iloc[101:] = 50.0
    assert np.isclose(z.iloc[100], thinness(D2).iloc[100]) and z.iloc[100] < -5
    print("  [ok] thinness: trailing 90 days only (later data cannot move it); "
          "a sudden drop scores strongly negative")
    # 3. surprise: forward window starts at t+1
    px = pd.Series(np.exp(np.cumsum(np.r_[0, np.full(60, 0.01), np.full(10, 0.05)])),
                   index=pd.date_range("2025-01-01", periods=71))
    S, _ = surprise(px, 7)
    assert S.iloc[60] > S.iloc[50] + 1
    print("  [ok] surprise: realised vol over t+1..t+h vs EWMA at t")
    # 4. the D1 statistic sees an injected effect and not a null one
    z0, p0 = sim(1, w=0.0); z1, p1 = sim(1, w=0.3)
    f0, f1 = build_frame(z0, p0, 7), build_frame(z1, p1, 7)
    r0, r1 = spearman(f0["z"].values, f0["S"].values), spearman(f1["z"].values, f1["S"].values)
    _, hi1 = block_ci(spearman, [f1["z"].values, f1["S"].values], n_boot=300)
    assert abs(r0) < 0.12 and r1 < -0.2 and hi1 < 0, (r0, r1, hi1)
    print(f"  [ok] statistic: uninformative z rho {r0:+.3f}; informative z rho "
          f"{r1:+.3f} with upper bound {hi1:+.3f}")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Depth thinning -> vol surprise")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--calibrate", type=int, nargs="?", const=200, default=None)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.calibrate:
        calibrate(a.calibrate)
        return 0
    v = {s: analyse(s) for s in SYMBOLS}
    print(f"\n{'=' * 78}")
    if any(x is None for x in v.values()):
        print("VERDICT: incomplete")
        return 1
    print(f"VERDICT D1 (pre-registered, both assets required): "
          f"{'SUPPORTED' if all(v.values()) else 'NOT SUPPORTED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
