"""
Corridor v2: does a mean-reverting volatility forecast fix the calm-regime
under-coverage of the v1 (EWMA) corridor?

MOTIVATION
----------
v1 (scripts/test_vol_corridor.py, FINDINGS_vol_corridor.md) is calibrated on
average but not regime by regime: a "90%" 14-day band covered ~78% in calm
regimes and ~96% in storms, and --moves found the 7-14 day 1-in-20 levels
beaten 8-14% of the time in calm regimes. Mechanism: EWMA assumes today's
volatility persists for the whole horizon; real volatility mean-reverts.

v2 changes ONE thing — the volatility forecast. Everything else (filtered
historical simulation, expanding pool of past standardised returns, 365-day
warm-up, test dates, regime split) is identical, so any difference is the
vol model.

PRE-REGISTRATION (written 2026-10-08, before running on real data)
------------------------------------------------------------------
Vol models
  v1  EWMA, lambda=0.94. h-day variance = h * sigma_t^2 (flat).
  v2  GARCH(1,1) with variance targeting to a TRAILING 365-day mean of r^2:
        v[t] = (1-a-b) * LR[t] + a * r[t]^2 + b * v[t-1]
      h-day variance = h*LR + (v - LR) * (1 - phi^h) / (1 - phi), phi = a+b.
      In calm spells (v < LR) the forecast widens toward the long-run level.
      Trailing (not full-sample) LR because crypto vol has fallen secularly
      since 2017; a fixed long-run level from 2017-2020 would be stale.

Fit / test split (out of sample)
  a, b fitted by Gaussian QMLE on taker_flow spot closes up to 2020-10-31,
  per asset, then FROZEN. Test: 2020-11-01 to the last date in taker_flow.
  Both models are scored on the same dates, with the same FHS pool start.

PRIMARY: 14d, 90% band, BTC and ETH. Regimes = terciles of EWMA vol on the
test dates (the same split for both models — it is what a user sees).

Hypotheses and prior, on record
  H5  v2 calm-regime coverage is closer to 90% than v1's.     prior: yes
  H6  v2 storm-regime coverage is closer to 90% than v1's.    prior: unsure
      (trailing LR stays high for a year after a storm, which may keep v2
      wide after the storm has passed)
  H7  v2 keeps unconditional calibration (Kupiec p > 0.05).   prior: yes
  H8  fitted half-life of vol shocks is 1-4 weeks (phi ~0.95-0.98).
                                                             prior: roughly

Decision rule, fixed in advance. v2 is ADOPTED only if, on BOTH assets:
  (a) calm improvement  D = |cov_calm(v1) - 90%| - |cov_calm(v2) - 90%|
      exceeds T_CALM, the 95th percentile of D on synthetic data where v1 is
      the correct model (see --calibrate; T_CALM below);
  (b) conditional calibration error CCE = mean over calm/mid/storm of
      |coverage - 90%| is lower for v2 than v1;
  (c) v2 passes Kupiec (p > 0.05) at 80, 90 and 95%, 14d, non-overlapping.

Calibration of the rule (synthetic, 200 paths each, run before real data;
single asset, 3,300 days, fit on the first 1,170, t(4) innovations):
  null  EWMA-like GARCH (a=.06, b=.939): v1 is the right model.
        D median +0.5 pts, 95th pct +4.12 -> T_CALM = 4.12 pts.
        Full rule passes 4.5% of the time = false-positive rate per asset.
  alt   mean-reverting GARCH (a=.10, b=.85, half-life ~2 weeks).
        D median +2.8 pts; full rule passes 28.5% = power per asset.
  POWER IS LOW. The alt world's calm miscalibration (v1 84% at nominal 90%)
  is milder than the real data's (~78%), so real power is probably higher —
  but "NOT ADOPTED" is weak evidence that v2 is useless, and will be
  recorded as such. Kupiec alone fails ~15% of the time for a correct model
  (three levels at 5% each); that cost to power is accepted because v1 was
  held to the same standard.

Supplementary, reported but not part of the rule: McNemar exact test on
non-overlapping calm windows; 7d results; storm coverage; band width;
--moves-style dip/run exceedance in the calm regime.

NO RESCUE: if v2 is not adopted, alternatives thought of afterwards (other
LR windows, other models) are recorded as limitations, not rerun as tests.
--lr-window exists for a labelled SENSITIVITY run only.

USAGE
-----
    python -m scripts.test_vol_corridor_v2 --self-test
    python -m scripts.test_vol_corridor_v2 --calibrate      # synthetic, ~3 min
    python -m scripts.test_vol_corridor_v2                  # the test

Refresh taker_flow first (last refreshed 2026-08-31):
    python -m collectors.taker_flow_backfill --market spot
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.test_vol_corridor import (WARMUP, ewma_vol,  # noqa: E402
                                       excursions, h_returns, kupiec,
                                       load_closes, log_returns)

FIT_END = pd.Timestamp("2020-10-31")
TEST_START = pd.Timestamp("2020-11-01")
LR_WINDOW = 365
LEVELS = (0.80, 0.90, 0.95)
HORIZONS = (14, 7)
PRIMARY_H, PRIMARY_LV = 14, 0.90
REGIMES = ("calm", "mid", "storm")
# 95th percentile of the calm improvement D under the null (v1 correct),
# from `--calibrate` with 200 synthetic paths, set before any real data.
T_CALM = 0.0412                         # 4.12 pts; frozen 2026-10-08


# ------------------------------------------------------------ vol models
def trailing_lr(r: np.ndarray, window: int = LR_WINDOW) -> np.ndarray:
    """Mean of r^2 over the trailing window ending at t (known at close t)."""
    return (pd.Series(r * r).rolling(window, min_periods=window).mean()
            .to_numpy())


def garch_var(r: np.ndarray, lr: np.ndarray, a: float, b: float) -> np.ndarray:
    """v[t] = one-day-ahead variance forecast made at close t (uses r[t])."""
    n = len(r)
    v = np.full(n, np.nan)
    ok = np.flatnonzero(np.isfinite(lr))
    if len(ok) == 0:
        return v
    s = ok[0]
    v[s] = lr[s]
    w = 1.0 - a - b
    for t in range(s + 1, n):
        v[t] = w * lr[t] + a * r[t] * r[t] + b * v[t - 1]
    return v


def _qmle_grid(r, lr, end, A, B):
    """Vectorised Gaussian QMLE over (a, b) pairs, using r up to index end."""
    ok = np.flatnonzero(np.isfinite(lr))
    s = ok[0]
    v = np.full(len(A), lr[s])
    ll = np.zeros(len(A))
    w = 1.0 - A - B
    for t in range(s + 1, end + 1):
        ll += -0.5 * (np.log(v) + r[t] * r[t] / v)
        v = w * lr[t] + A * r[t] * r[t] + B * v
    return ll


def fit_garch(r: np.ndarray, lr: np.ndarray, end: int) -> tuple[float, float]:
    """(a, b) by QMLE on data through index `end` only. Coarse grid, then a
    fine grid around the best point. Constraint a + b < 0.999."""
    ok = np.flatnonzero(np.isfinite(lr))
    if len(ok) == 0 or end - ok[0] < 200:
        raise ValueError("not enough data to fit")
    aa, bb = np.meshgrid(np.arange(0.01, 0.305, 0.01),
                         np.arange(0.50, 0.9951, 0.005))
    m = (aa + bb) < 0.999
    A, B = aa[m], bb[m]
    ll = _qmle_grid(r, lr, end, A, B)
    a0, b0 = A[np.argmax(ll)], B[np.argmax(ll)]
    aa, bb = np.meshgrid(np.arange(max(0.001, a0 - 0.01), a0 + 0.0101, 0.001),
                         np.arange(max(0.40, b0 - 0.005), b0 + 0.00501, 0.0005))
    m = (aa + bb) < 0.999
    A, B = aa[m], bb[m]
    ll = _qmle_grid(r, lr, end, A, B)
    return float(A[np.argmax(ll)]), float(B[np.argmax(ll)])


def hvar(v: np.ndarray, lr: np.ndarray, phi: float, h: int) -> np.ndarray:
    """Sum of expected daily variances over t+1..t+h (GARCH term structure)."""
    if abs(1 - phi) < 1e-12:
        return h * v
    return h * lr + (v - lr) * (1 - phi ** h) / (1 - phi)


def model_scales(p: np.ndarray, h: int, a: float, b: float,
                 lr_window: int = LR_WINDOW) -> dict:
    """h-day forecast st.dev. per model, plus EWMA vol (regime variable)."""
    r = log_returns(p)
    sig = ewma_vol(r)
    lr = trailing_lr(r, lr_window)
    v = garch_var(r, lr, a, b)
    return {"v1": sig * math.sqrt(h),
            "v2": np.sqrt(hvar(v, lr, a + b, h)),
            "sig": sig, "lr": lr, "v": v}


# ------------------------------------------------------------ evaluation
def _forecasts(p, sh, h, t_idx, pool_start):
    """FHS levels at each t with this model's scale. Returns dict of arrays
    aligned to t_idx (NaN where the pool is too small)."""
    R = h_returns(p, h)
    D, U = excursions(p, h)
    Z, Dz, Uz = R / sh, D / sh, U / sh
    qs = [(1 - lv) / 2 for lv in LEVELS] + [(1 + lv) / 2 for lv in LEVELS]
    k = len(LEVELS)
    out = {"lo": np.full((len(t_idx), k), np.nan),
           "hi": np.full((len(t_idx), k), np.nan),
           "dip": np.full((len(t_idx), 2), np.nan),
           "run": np.full((len(t_idx), 2), np.nan)}
    for i, t in enumerate(t_idx):
        end = t - h + 1
        z = Z[pool_start:end]; z = z[np.isfinite(z)]
        if len(z) < WARMUP or not np.isfinite(sh[t]):
            continue
        q = np.quantile(z, qs) * sh[t]
        out["lo"][i], out["hi"][i] = q[:k], q[k:]
        d = Dz[pool_start:end]; d = d[np.isfinite(d)]
        u = Uz[pool_start:end]; u = u[np.isfinite(u)]
        out["dip"][i] = np.quantile(d, [0.8, 0.95]) * sh[t]
        out["run"][i] = np.quantile(u, [0.8, 0.95]) * sh[t]
    out["R"], out["D"], out["U"] = R[t_idx], D[t_idx], U[t_idx]
    return out


def mcnemar_one_sided(b: int, c: int) -> float:
    """P(X >= b), X ~ Bin(b+c, 0.5): v2 fixes more misses than it creates."""
    n = b + c
    if n == 0:
        return float("nan")
    return sum(math.comb(n, k) for k in range(b, n + 1)) / 2 ** n


def compare(p: np.ndarray, dates: pd.DatetimeIndex, h: int, a: float,
            b: float, test_start=TEST_START, lr_window: int = LR_WINDOW
            ) -> dict:
    """Score v1 and v2 on identical test dates and an identical FHS pool."""
    sc = model_scales(p, h, a, b, lr_window)
    n = len(p)
    first = [np.flatnonzero(np.isfinite(sc[m]))[0] for m in ("v1", "v2")]
    pool_start = max(first)
    t0 = max(int(np.searchsorted(dates, test_start)), pool_start + WARMUP + h)
    t_idx = np.arange(t0, n - h)
    if len(t_idx) < 2 * h:
        return {}
    fc = {m: _forecasts(p, sc[m], h, t_idx, pool_start) for m in ("v1", "v2")}
    valid = np.all(np.isfinite(fc["v1"]["lo"]), axis=1) & \
        np.all(np.isfinite(fc["v2"]["lo"]), axis=1)
    t_idx = t_idx[valid]
    for m in fc:
        for key in fc[m]:
            fc[m][key] = fc[m][key][valid]
    sig = sc["sig"][t_idx]
    terc = np.quantile(sig, [1 / 3, 2 / 3])
    regime = np.where(sig <= terc[0], 0, np.where(sig <= terc[1], 1, 2))
    no = ((t_idx - t_idx[0]) % h) == 0

    res = {"n": len(t_idx), "start": dates[t_idx[0]].date(),
           "end": dates[t_idx[-1] + h].date(), "regime_counts":
           [int((regime == g).sum()) for g in range(3)],
           "calm_no": int((no & (regime == 0)).sum())}
    hits = {}
    for m in ("v1", "v2"):
        f = fc[m]
        R = f["R"]
        mm = {}
        for j, lv in enumerate(LEVELS):
            hit = (R >= f["lo"][:, j]) & (R <= f["hi"][:, j])
            hits[(m, lv)] = hit
            x = int((~hit[no]).sum()); nn = int(no.sum())
            cond = [float(hit[regime == g].mean()) for g in range(3)]
            mm[lv] = {"cov": float(hit.mean()), "n_no": nn,
                      "cov_no": 1 - x / nn, "kupiec": kupiec(nn, x, 1 - lv),
                      "width": float(np.mean(np.exp(f["hi"][:, j])
                                             - np.exp(f["lo"][:, j])) * 100),
                      "cond": cond,
                      "cce": float(np.mean([abs(c - lv) for c in cond]))}
        calm = regime == 0
        mm["dip_calm"] = (float((f["D"][calm] > f["dip"][calm, 0]).mean()),
                          float((f["D"][calm] > f["dip"][calm, 1]).mean()))
        mm["run_calm"] = (float((f["U"][calm] > f["run"][calm, 0]).mean()),
                          float((f["U"][calm] > f["run"][calm, 1]).mean()))
        res[m] = mm
    lv = PRIMARY_LV
    c1, c2 = res["v1"][lv]["cond"][0], res["v2"][lv]["cond"][0]
    res["D_calm"] = abs(c1 - lv) - abs(c2 - lv)
    sel = no & (regime == 0)
    h1, h2 = hits[("v1", lv)][sel], hits[("v2", lv)][sel]
    bb, cc = int((h2 & ~h1).sum()), int((h1 & ~h2).sum())
    res["mcnemar"] = (bb, cc, mcnemar_one_sided(bb, cc))
    return res


def verdict(res: dict, t_calm: float) -> tuple[bool, list[str]]:
    lv = PRIMARY_LV
    a = res["D_calm"] > t_calm
    b = res["v2"][lv]["cce"] < res["v1"][lv]["cce"]
    c = all(res["v2"][x]["kupiec"] > 0.05 for x in LEVELS)
    why = [f"(a) calm improvement {res['D_calm'] * 100:+.1f} pts vs "
           f"threshold {t_calm * 100:.1f}: {'PASS' if a else 'FAIL'}",
           f"(b) CCE v2 {res['v2'][lv]['cce'] * 100:.1f} vs v1 "
           f"{res['v1'][lv]['cce'] * 100:.1f}: {'PASS' if b else 'FAIL'}",
           f"(c) v2 Kupiec p>0.05 at 80/90/95: {'PASS' if c else 'FAIL'}"]
    return a and b and c, why


# ----------------------------------------------------------- calibration
def _sim(n, a, b, nu, seed, daily_vol=0.035):
    """GARCH(1,1) with Student-t innovations, unconditional vol daily_vol."""
    rng = np.random.default_rng(seed)
    vbar = daily_vol ** 2
    w = vbar * (1 - a - b)
    v, r = vbar, np.empty(n)
    e = rng.standard_t(nu, n) / math.sqrt(nu / (nu - 2))
    for i in range(n):
        r[i] = math.sqrt(v) * e[i]
        v = w + a * r[i] ** 2 + b * v
    return 100 * np.exp(np.cumsum(r))


SIM_N, SIM_FIT_END = 3300, 1170         # ~2017-08..2026-08, fit to 2020-10
NULL = dict(a=0.06, b=0.939, nu=4)      # EWMA(0.94)-like: v1 is correct
ALT = dict(a=0.10, b=0.85, nu=4)        # mean-reverting: half-life ~2 weeks


def _sim_once(spec, seed, h=PRIMARY_H):
    p = _sim(SIM_N, spec["a"], spec["b"], spec["nu"], seed)
    dates = pd.date_range("2017-08-17", periods=SIM_N)
    r = log_returns(p)
    a, b = fit_garch(r, trailing_lr(r), SIM_FIT_END)
    res = compare(p, dates, h, a, b, test_start=dates[SIM_FIT_END + 1])
    return res, a + b


def calibrate(n_sims: int = 200, seed0: int = 1000) -> dict:
    out = {}
    for name, spec in (("null", NULL), ("alt", ALT)):
        D, cce_win, kup, phis = [], [], [], []
        for i in range(n_sims):
            res, phi = _sim_once(spec, seed0 + i)
            D.append(res["D_calm"])
            cce_win.append(res["v2"][PRIMARY_LV]["cce"]
                           < res["v1"][PRIMARY_LV]["cce"])
            kup.append(all(res["v2"][x]["kupiec"] > 0.05 for x in LEVELS))
            phis.append(phi)
        out[name] = {"D": np.array(D), "cce_win": np.array(cce_win),
                     "kupiec_ok": np.array(kup), "phi": np.array(phis)}
    t = float(np.quantile(out["null"]["D"], 0.95))
    for name in out:
        o = out[name]
        o["adopt"] = (o["D"] > t) & o["cce_win"] & o["kupiec_ok"]
    out["T_CALM"] = t
    return out


def report_calibration(out: dict):
    t = out["T_CALM"]
    print(f"\nT_CALM = 95th pct of calm improvement under the null: "
          f"{t * 100:.2f} pts")
    for name in ("null", "alt"):
        o = out[name]
        print(f"  {name:<5} n={len(o['D'])}  D median {np.median(o['D']) * 100:+.1f}"
              f" pts  [5-95%: {np.quantile(o['D'], .05) * 100:+.1f} .. "
              f"{np.quantile(o['D'], .95) * 100:+.1f}]  CCE won "
              f"{o['cce_win'].mean():.0%}  Kupiec ok {o['kupiec_ok'].mean():.0%}"
              f"  fitted phi median {np.median(o['phi']):.3f}"
              f"  -> ADOPT rate {o['adopt'].mean():.1%}")
    print("  null ADOPT rate = false-positive rate per asset (target <= 5%);"
          "\n  alt ADOPT rate = power per asset when v2's premise is true.")


# -------------------------------------------------------------- report
def load_spliced(sym: str) -> tuple[pd.Series, pd.Timestamp | None]:
    """taker_flow spot (2017+) extended with price_snapshots after its last
    date — used ONLY for today's readout, never for the test."""
    tf = load_closes(sym, True)
    ps = load_closes(sym, False)
    if tf.empty:
        return ps, None
    tail = ps[ps.index > tf.index[-1]]
    return pd.concat([tf, tail]), (tf.index[-1] if len(tail) else None)


def report_asset(name: str, sym: str, t_calm: float, lr_window: int):
    print(f"\n{'=' * 84}\n{name}  ({sym})\n{'=' * 84}")
    px = load_closes(sym, True)
    if px.empty or px.index[0] > FIT_END - pd.Timedelta(days=LR_WINDOW + 200):
        print("  NO / TOO LITTLE taker_flow history before 2020-10-31")
        return None
    p = px.values.astype(float)
    r = log_returns(p)
    end = int(np.searchsorted(px.index, FIT_END, side="right")) - 1
    a, b = fit_garch(r, trailing_lr(r, lr_window), end)
    phi = a + b
    print(f"  data {px.index[0].date()} … {px.index[-1].date()}; fit through "
          f"{px.index[end].date()} ({end - lr_window} days after LR warm-up)")
    print(f"  fitted a={a:.3f}  b={b:.3f}  phi={phi:.3f}  half-life of a vol "
          f"shock {math.log(.5) / math.log(phi):.1f} days   (H8: 7-28 days)")
    if b <= 0.5005 or phi < 0.80 or phi > 0.998:
        print("  WARNING: fit at a grid boundary or implausible persistence. "
              "On synthetic data\n  ~800-day fits land like this a few % of "
              "the time. The decision rule still\n  applies as registered; "
              "read the result with this in mind.")

    verdict_out = None
    for h in HORIZONS:
        res = compare(p, px.index, h, a, b, lr_window=lr_window)
        if not res:
            print(f"  {h}d: not enough test data")
            continue
        tag = "PRIMARY" if h == PRIMARY_H else "secondary"
        print(f"\n  {h}d ({tag}) — test {res['start']} … {res['end']}, "
              f"{res['n']} days; regime days calm/mid/storm "
              f"{'/'.join(map(str, res['regime_counts']))}")
        print(f"  {'model':<6}{'level':>6}{'cov':>7}{'n_no':>6}{'cov_no':>8}"
              f"{'kupiec_p':>10}{'width%':>8}{'calm':>8}{'mid':>7}"
              f"{'storm':>7}{'CCE':>6}")
        for m in ("v1", "v2"):
            for lv in LEVELS:
                e = res[m][lv]
                c = e["cond"]
                print(f"  {m:<6}{lv:>6.0%}{e['cov']:>7.1%}{e['n_no']:>6}"
                      f"{e['cov_no']:>8.1%}{e['kupiec']:>10.3f}"
                      f"{e['width']:>8.1f}{c[0]:>8.1%}{c[1]:>7.1%}"
                      f"{c[2]:>7.1%}{e['cce'] * 100:>6.1f}")
            print()
        bb, cc, pm = res["mcnemar"]
        print(f"  calm, 90%, non-overlapping ({res['calm_no']} windows): v2 "
              f"fixed {bb} v1 misses, created {cc} new misses; McNemar "
              f"one-sided p={pm:.3f}  (supplementary)")
        for m in ("v1", "v2"):
            d, u = res[m]["dip_calm"], res[m]["run_calm"]
            print(f"  {m} calm-regime dip beaten {d[0]:.0%}/{d[1]:.0%}, "
                  f"run beaten {u[0]:.0%}/{u[1]:.0%}  (targets 20%/5%)")
        if h == PRIMARY_H:
            ok, why = verdict(res, t_calm)
            print(f"\n  DECISION RULE ({name}):")
            for w in why:
                print(f"    {w}")
            print(f"    -> {'v2 BETTER on this asset' if ok else 'v2 NOT better on this asset'}")
            verdict_out = (ok, res)

    # today's readout: spliced series, frozen parameters
    sp, spliced_after = load_spliced(sym)
    q = sp.values.astype(float)
    last = q[-1]
    note = (f" (taker_flow to {spliced_after.date()}, then price_snapshots)"
            if spliced_after is not None else "")
    print(f"\n  TODAY{note}: ${last:,.2f} on {sp.index[-1].date()} — "
          "descriptive; use v2 only if ADOPTED")
    h = PRIMARY_H
    sc = model_scales(q, h, a, b, lr_window)
    t = len(q) - 1
    pool_start = max(np.flatnonzero(np.isfinite(sc[m]))[0] for m in ("v1", "v2"))
    R = h_returns(q, h); D, U = excursions(q, h)
    print(f"  LR vol {math.sqrt(sc['lr'][t] * 365):.0%} ann., EWMA "
          f"{sc['sig'][t] * math.sqrt(365):.0%}, v2 14d-avg "
          f"{sc['v2'][t] / math.sqrt(h) * math.sqrt(365):.0%}")
    for m in ("v1", "v2"):
        s = sc[m]
        end = t - h + 1
        z = (R / s)[pool_start:end]; z = z[np.isfinite(z)]
        d = (D / s)[pool_start:end]; d = d[np.isfinite(d)]
        u = (U / s)[pool_start:end]; u = u[np.isfinite(u)]
        lo80, hi80, lo95, hi95 = np.quantile(z, [.1, .9, .025, .975]) * s[t]
        d5, d20 = np.quantile(d, [.8, .95]) * s[t]
        u5, u20 = np.quantile(u, [.8, .95]) * s[t]
        print(f"  {m} 14d 80% ${last * math.exp(lo80):,.0f}–"
              f"${last * math.exp(hi80):,.0f}   95% ${last * math.exp(lo95):,.0f}"
              f"–${last * math.exp(hi95):,.0f}   dip 1-in-5/1-in-20 "
              f"{math.exp(-d5) - 1:+.1%}/{math.exp(-d20) - 1:+.1%}   run "
              f"{math.exp(u5) - 1:+.1%}/{math.exp(u20) - 1:+.1%}")
    return verdict_out


# ----------------------------------------------------------- self-test
def self_test() -> int:
    # 1. Term structure: h=1 equals one-step; v=LR is flat; long h -> LR.
    v, lr = np.array([1.0, 4.0]), np.array([2.0, 2.0])
    assert np.allclose(hvar(v, lr, 0.95, 1), v)
    assert np.allclose(hvar(lr, lr, 0.95, 14), 14 * lr)
    assert np.allclose(hvar(v, lr, 0.95, 5000) / 5000, lr, rtol=1e-2)
    assert hvar(v, lr, 0.95, 14)[0] > 14 * v[0]           # calm widens
    assert hvar(v, lr, 0.95, 14)[1] < 14 * v[1]           # storm narrows
    print("  [ok] GARCH term structure: h=1 exact, flat at LR, calm widens, "
          "storm narrows")

    # 2. QMLE recovers parameters on a long simulated GARCH path.
    p = _sim(6000, 0.10, 0.85, 1000, 7)                   # ~normal innov
    r = log_returns(p)
    a, b = fit_garch(r, trailing_lr(r), 5999)
    assert abs(a - 0.10) < 0.04 and abs(b - 0.85) < 0.06, (a, b)
    print(f"  [ok] QMLE recovers a=0.10 b=0.85 -> a={a:.3f} b={b:.3f}")

    # 3. No lookahead: fit ignores data after `end`; scales and levels at t0
    #    ignore prices after t0.
    p1 = _sim(2500, 0.10, 0.85, 4, 8)
    p2 = p1.copy(); p2[1501:] *= 3.0
    r1, r2 = log_returns(p1), log_returns(p2)
    assert fit_garch(r1, trailing_lr(r1), 1500) == fit_garch(r2, trailing_lr(r2), 1500)
    s1, s2 = model_scales(p1, 14, .1, .85), model_scales(p2, 14, .1, .85)
    for m in ("v1", "v2"):
        assert np.array_equal(s1[m][:1501], s2[m][:1501], equal_nan=True), m
    t_idx = np.array([1400, 1500])
    for m in ("v1", "v2"):
        f1 = _forecasts(p1, s1[m], 14, t_idx, 400)
        f2 = _forecasts(p2, s2[m], 14, t_idx, 400)
        for k in ("lo", "hi", "dip", "run"):
            assert np.array_equal(f1[k], f2[k], equal_nan=True), (m, k)
    print("  [ok] no lookahead: fit, scales and FHS levels unchanged when "
          "later prices change")

    # 4. McNemar sanity.
    assert mcnemar_one_sided(0, 0) != mcnemar_one_sided(0, 0)       # nan
    assert abs(mcnemar_one_sided(5, 5) - 0.623) < 0.001
    assert mcnemar_one_sided(10, 0) < 0.001
    print("  [ok] McNemar one-sided: 5 vs 5 -> 0.62; 10 vs 0 -> <0.001")

    # 5. Where v2's premise holds (fast mean reversion), v2 beats v1 in calm,
    #    averaged over seeds (single fits on ~800 days are noisy).
    c1s, c2s, phis = [], [], []
    for seed in range(20, 26):
        res, phi = _sim_once(ALT, seed)
        c1s.append(res["v1"][PRIMARY_LV]["cond"][0])
        c2s.append(res["v2"][PRIMARY_LV]["cond"][0])
        phis.append(phi)
    c1, c2 = float(np.mean(c1s)), float(np.mean(c2s))
    print(f"  mean-reverting sims (6): calm coverage v1 {c1:.1%}, v2 {c2:.1%}, "
          f"fitted phi median {np.median(phis):.3f} (true 0.95)")
    assert c1 < 0.88 and abs(c2 - 0.90) < abs(c1 - 0.90)
    assert 0.90 < np.median(phis) < 0.99
    print("  [ok] v2 closer to 90% in calm when vol mean-reverts; phi recovered")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Corridor v2: mean-reverting vol")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--calibrate", type=int, nargs="?", const=200, default=None,
                    metavar="N", help="synthetic null/alt runs (default 200)")
    ap.add_argument("--lr-window", type=int, default=LR_WINDOW,
                    help="SENSITIVITY ONLY; the pre-registered test uses 365")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.calibrate:
        report_calibration(calibrate(a.calibrate))
        return 0
    if a.lr_window != LR_WINDOW:
        print(f"*** SENSITIVITY RUN (lr-window {a.lr_window}) — NOT the "
              f"pre-registered test; the decision rule does not apply ***")
    print("Price source for the test: taker_flow spot (2017+)")
    verdicts = {}
    for name, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        verdicts[name] = report_asset(name, sym, T_CALM, a.lr_window)
    print(f"\n{'=' * 84}")
    if a.lr_window != LR_WINDOW:
        print("Sensitivity run: no verdict.")
        return 0
    if any(v is None for v in verdicts.values()):
        print("VERDICT: incomplete — an asset could not be tested.")
        return 1
    adopted = all(v[0] for v in verdicts.values())
    print(f"VERDICT (pre-registered, both assets required): v2 "
          f"{'ADOPTED' if adopted else 'NOT ADOPTED'}")
    print("H5 calm closer: " + ", ".join(
        f"{k} {'yes' if v[1]['D_calm'] > 0 else 'no'}" for k, v in verdicts.items()))
    print("H6 storm closer: " + ", ".join(
        f"{k} {'yes' if abs(v[1]['v2'][PRIMARY_LV]['cond'][2] - PRIMARY_LV) < abs(v[1]['v1'][PRIMARY_LV]['cond'][2] - PRIMARY_LV) else 'no'}"
        for k, v in verdicts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
