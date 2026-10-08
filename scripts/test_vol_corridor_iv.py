"""
Corridor v3: does IMPLIED volatility (Deribit DVOL) fix the calm-regime
under-coverage that past-return models cannot?

MOTIVATION
----------
v1 (EWMA) under-covers in calm regimes. v2 (mean-reverting GARCH) fixed the
storm side but not the calm side and was NOT ADOPTED: calm spells end in
jumps, and nothing in past returns anticipates a jump
(docs/FINDINGS_vol_corridor.md). Implied volatility is the options market's
forward-looking price of risk — the one input here that could, in principle,
see a breakout coming. DVOL (collectors/dvol_backfill.py) gives a daily
30-day implied vol for BTC and ETH from 2021-03-24.

v3 changes ONE thing relative to v1 — the volatility scale. Filtered
historical simulation, pooling, warm-up, levels, test dates and the regime
split are identical. Any variance risk premium or 30-day-vs-14-day tenor
mismatch is absorbed by FHS: the pool is of returns standardised by the
same implied scale.

PRE-REGISTRATION (written 2026-10-08, before DVOL is compared with outcomes)
-------------------------------------------------------------------------
Models (h-day scale at close of day t)
  v1  EWMA lambda=0.94:  sigma_t * sqrt(h)
  v3  DVOL:              DVOL_t / 100 * sqrt(h / 365)
DVOL_t = close of the 1D candle for UTC day t (24:00 UTC), the same moment
as the taker_flow daily close.

Sample. Prices: taker_flow spot closes. FHS pool for BOTH models starts at
the first DVOL day (2021-03-24); 365-day warm-up, so tests start ~2022-04.
Test runs to the last day with both price and DVOL.

PRIMARY: 14d, 90% band, BTC and ETH. Regimes = terciles of EWMA vol over
the test dates (the same split as v1/v2; what a user sees).

Hypotheses and prior, on record
  H9   v3 calm-regime coverage is closer to 90% than v1's.     prior: yes
  H10  v3 storm-regime coverage is closer to 90% than v1's.    prior: yes
       (implied vol reacts less than EWMA to a spike)
  H11  v3 keeps unconditional calibration (Kupiec p > 0.05).   prior: yes
  H12  (supplementary) among calm days, those with a high DVOL/EWMA ratio
       see more v1 breaches than those with a low ratio.       prior: yes

Decision rule, fixed in advance. v3 is ADOPTED only if, on BOTH assets:
  (a) calm improvement D = |cov_calm(v1) - 90%| - |cov_calm(v3) - 90%| has a
      one-sided 95% lower bound > 0, by moving-block bootstrap over test
      days (block 90 days, 2,000 resamples, seed 20261008);
  (b) conditional calibration error (mean |coverage - 90%| over calm, mid,
      storm) is lower for v3;
  (c) v3 passes Kupiec (p > 0.05) at 80, 90 and 95%, 14d, non-overlapping.
The threshold is the data's own sampling noise rather than a synthetic
null: v3's information source cannot be simulated credibly. The procedure
IS checked on synthetic data (see --calibrate and the findings doc) for its
false-positive rate, power and bootstrap coverage.

Calibration (synthetic, 200 paths each, run 2026-10-08 before real data;
GARCH t(4) prices, 2,020 'DVOL' days, test ~1,620 days):
  null  DVOL = EWMA vol x noise (no information beyond past returns):
        D mean +0.2 pts (sd 2.2); rule passes 3.5% = false positives/asset.
  alt   DVOL = TRUE expected 30-day vol x small noise (perfect knowledge of
        the vol process): v3 calm coverage 89.4% vs v1 83.3%, D mean +4.2
        pts — yet the rule passes only 19.0% = power per asset.
  Bootstrap lower bound sat below the true mean D in 96.5% / 97.0% of runs
  (target >= 95%): the interval is honest.
  POWER IS LOW: ~40 independent 14-day calm windows cannot reliably confirm
  even a perfectly informed implied vol. NOT ADOPTED will therefore say
  little; ADOPTED would say a lot. Forward tracking is the real test.

Supplementary, outside the rule: 7d; H12; McNemar on non-overlapping calm
windows; band width; calm-regime dip/run exceedance.

CAVEAT ON RECORD. 2022-2026 is a period whose calm-regime behaviour has
already been examined (v1, v2). v3 uses a new information source and was
not tuned on it, but evidence from this period is weaker than a forward
test. NO RESCUE: ideas formed after seeing the result are recorded as
limitations, not run as tests.

USAGE
-----
    python -m scripts.test_vol_corridor_iv --self-test
    python -m scripts.test_vol_corridor_iv --calibrate        # ~5 min
    python -m scripts.test_vol_corridor_iv                    # the test
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
                                       load_closes, log_returns, kupiec)
from scripts.test_vol_corridor_v2 import (_forecasts, load_spliced,  # noqa: E402
                                          mcnemar_one_sided)

LEVELS = (0.80, 0.90, 0.95)
HORIZONS = (14, 7)
PRIMARY_H, PRIMARY_LV = 14, 0.90
BLOCK, N_BOOT, BOOT_SEED = 90, 2000, 20261008
MODELS = ("v1", "v3")


# ---------------------------------------------------------------- data
def load_dvol(currency: str) -> pd.Series:
    """DVOL closes as an annualised FRACTION (0.55 = 55%)."""
    from config.database import get_connection, get_cursor
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT d, close FROM dvol_daily WHERE currency = %s "
                    "ORDER BY d", (currency,))
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.Series(dtype=float)
    if isinstance(rows[0], dict):
        idx = [r["d"] for r in rows]; val = [r["close"] for r in rows]
    else:
        idx = [r[0] for r in rows]; val = [r[1] for r in rows]
    s = pd.Series(val, index=pd.to_datetime(idx), dtype=float)
    return s / 100.0 if s.median() > 1.5 else s


def scales(p: np.ndarray, iv_ann: np.ndarray, h: int) -> dict:
    """h-day scale per model; iv_ann aligned to p (NaN where missing)."""
    sig = ewma_vol(log_returns(p))
    return {"v1": sig * math.sqrt(h),
            "v3": iv_ann * math.sqrt(h / 365.0),
            "sig": sig, "iv_daily": iv_ann / math.sqrt(365.0)}


# ------------------------------------------------------------ evaluation
def block_bootstrap_D(h1, h2, calm, lv, n_boot=N_BOOT, block=BLOCK,
                      seed=BOOT_SEED):
    """One-sided 95% lower bound of D by moving-block bootstrap over days."""
    n = len(h1)
    L = min(block, n)
    rng = np.random.default_rng(seed)
    k = -(-n // L)
    starts = rng.integers(0, n - L + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(L)).reshape(n_boot, -1)[:, :n]
    c = calm[idx]
    cnt = c.sum(1)
    ok = cnt > 0
    m1 = (h1[idx] & c).sum(1)[ok] / cnt[ok]
    m2 = (h2[idx] & c).sum(1)[ok] / cnt[ok]
    D = np.abs(m1 - lv) - np.abs(m2 - lv)
    return float(np.quantile(D, 0.05))


def compare(p: np.ndarray, dates: pd.DatetimeIndex, iv_ann: np.ndarray,
            h: int, n_boot: int = N_BOOT) -> dict:
    sc = scales(p, iv_ann, h)
    n = len(p)
    pool_start = max(int(np.flatnonzero(np.isfinite(sc[m]))[0]) for m in MODELS)
    t0 = pool_start + WARMUP + h
    t_idx = np.arange(t0, n - h)
    if len(t_idx) < 4 * h:
        return {}
    fc = {m: _forecasts(p, sc[m], h, t_idx, pool_start) for m in MODELS}
    valid = np.ones(len(t_idx), bool)
    for m in MODELS:
        valid &= np.all(np.isfinite(fc[m]["lo"]), axis=1)
    t_idx = t_idx[valid]
    for m in MODELS:
        for k in fc[m]:
            fc[m][k] = fc[m][k][valid]
    sig = sc["sig"][t_idx]
    terc = np.quantile(sig, [1 / 3, 2 / 3])
    regime = np.where(sig <= terc[0], 0, np.where(sig <= terc[1], 1, 2))
    no = ((t_idx - t_idx[0]) % h) == 0
    res = {"n": len(t_idx), "start": dates[t_idx[0]].date(),
           "end": dates[t_idx[-1] + h].date(),
           "regime_counts": [int((regime == g).sum()) for g in range(3)],
           "calm_no": int((no & (regime == 0)).sum())}
    hits = {}
    for m in MODELS:
        f = fc[m]
        mm = {}
        for j, lv in enumerate(LEVELS):
            hit = (f["R"] >= f["lo"][:, j]) & (f["R"] <= f["hi"][:, j])
            hits[(m, lv)] = hit
            x, nn = int((~hit[no]).sum()), int(no.sum())
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
    h1, h3 = hits[("v1", lv)], hits[("v3", lv)]
    calm = regime == 0
    res["D_calm"] = (abs(res["v1"][lv]["cond"][0] - lv)
                     - abs(res["v3"][lv]["cond"][0] - lv))
    res["D_lb"] = block_bootstrap_D(h1, h3, calm, lv, n_boot=n_boot)
    sel = no & calm
    b, c = int((h3[sel] & ~h1[sel]).sum()), int((h1[sel] & ~h3[sel]).sum())
    res["mcnemar"] = (b, c, mcnemar_one_sided(b, c))
    # H12: within calm days, does a high implied/EWMA ratio flag v1 breaches?
    ratio = sc["iv_daily"][t_idx] / sig
    rc = ratio[calm]
    med = float(np.median(rc))
    hi_r, lo_r = calm & (ratio > med), calm & (ratio <= med)
    res["h12"] = {"median_ratio": med,
                  "breach_hi": float(1 - h1[hi_r].mean()),
                  "breach_lo": float(1 - h1[lo_r].mean()),
                  "n_hi_no": int((hi_r & no).sum()),
                  "n_lo_no": int((lo_r & no).sum())}
    return res


def verdict(res: dict) -> tuple[bool, list[str]]:
    lv = PRIMARY_LV
    a = res["D_lb"] > 0
    b = res["v3"][lv]["cce"] < res["v1"][lv]["cce"]
    c = all(res["v3"][x]["kupiec"] > 0.05 for x in LEVELS)
    why = [f"(a) calm improvement {res['D_calm'] * 100:+.1f} pts, bootstrap "
           f"95% lower bound {res['D_lb'] * 100:+.1f}: "
           f"{'PASS' if a else 'FAIL'} (needs > 0)",
           f"(b) CCE v3 {res['v3'][lv]['cce'] * 100:.1f} vs v1 "
           f"{res['v1'][lv]['cce'] * 100:.1f}: {'PASS' if b else 'FAIL'}",
           f"(c) v3 Kupiec p>0.05 at 80/90/95: {'PASS' if c else 'FAIL'}"]
    return a and b and c, why


# ----------------------------------------------------------- calibration
def sim_pair(kind: str, seed: int, n_burn: int = 400, n: int = 2020,
             a: float = 0.10, b: float = 0.85, nu: float = 4,
             daily_vol: float = 0.035):
    """Synthetic prices (GARCH, Student-t) and a synthetic 'DVOL'.
    kind='null': DVOL = EWMA vol x premium x AR(1) noise — no information
                 beyond what past returns already give.
    kind='alt':  DVOL = TRUE expected 30-day vol x premium x small noise —
                 the market knows the vol process exactly."""
    rng = np.random.default_rng(seed)
    N = n_burn + n
    vbar = daily_vol ** 2
    w = vbar * (1 - a - b)
    e = rng.standard_t(nu, N) / math.sqrt(nu / (nu - 2))
    r = np.empty(N); v_next = np.empty(N)
    v = vbar
    for i in range(N):
        r[i] = math.sqrt(v) * e[i]
        v = w + a * r[i] ** 2 + b * v
        v_next[i] = v                        # variance of day i+1, known at i
    p = 100 * np.exp(np.cumsum(r))
    sd, rho = (0.20, 0.97) if kind == "null" else (0.10, 0.97)
    x = np.empty(N); x[0] = rng.normal(0, sd)
    for i in range(1, N):
        x[i] = rho * x[i - 1] + rng.normal(0, sd * math.sqrt(1 - rho ** 2))
    if kind == "null":
        base = ewma_vol(log_returns(p))
    else:
        phi, k = a + b, np.arange(30)
        avg = vbar + (v_next[:, None] - vbar) * (phi ** k)[None, :]
        base = np.sqrt(avg.mean(1))
    iv = base * 1.05 * np.exp(x) * math.sqrt(365)
    iv[:n_burn] = np.nan                     # "DVOL" starts after burn-in
    dates = pd.date_range("2020-02-08", periods=N)
    return p, iv, dates


def calibrate(n_sims: int = 200, n_boot: int = 1000, seed0: int = 5000):
    out = {}
    for kind in ("null", "alt"):
        rows = []
        for i in range(n_sims):
            p, iv, dates = sim_pair(kind, seed0 + i)
            res = compare(p, dates, iv, PRIMARY_H, n_boot=n_boot)
            ok, _ = verdict(res)
            rows.append((res["D_calm"], res["D_lb"], ok,
                         res["v1"][PRIMARY_LV]["cond"][0],
                         res["v3"][PRIMARY_LV]["cond"][0],
                         res["v3"][PRIMARY_LV]["cce"] < res["v1"][PRIMARY_LV]["cce"]))
        a = np.array(rows, dtype=float)
        D, lb = a[:, 0], a[:, 1]
        out[kind] = {"D_mean": D.mean(), "D_sd": D.std(),
                     "lb_covers": float(np.mean(lb <= D.mean())),
                     "adopt": float(a[:, 2].mean()),
                     "lb_pos": float(np.mean(lb > 0)),
                     "calm_v1": float(np.median(a[:, 3])),
                     "calm_v3": float(np.median(a[:, 4])),
                     "cce_win": float(a[:, 5].mean()), "n": n_sims}
    return out


def report_calibration(out: dict):
    for kind in ("null", "alt"):
        o = out[kind]
        print(f"  {kind:<5} n={o['n']}  D mean {o['D_mean'] * 100:+.1f} pts "
              f"(sd {o['D_sd'] * 100:.1f})  calm cov v1 {o['calm_v1']:.1%} "
              f"v3 {o['calm_v3']:.1%}  CCE won {o['cce_win']:.0%}")
        print(f"        lower bound > 0 in {o['lb_pos']:.1%}; full rule "
              f"passes {o['adopt']:.1%}; lower bound below the true mean in "
              f"{o['lb_covers']:.1%} (target >= 95%)")
    print("  null pass rate = false positives per asset; alt = power per asset.")


# -------------------------------------------------------------- report
def report_asset(name: str, sym: str, cur: str):
    print(f"\n{'=' * 84}\n{name}  ({sym}, DVOL {cur})\n{'=' * 84}")
    px = load_closes(sym, True)
    dv = load_dvol(cur)
    if px.empty or dv.empty:
        print("  NO DATA (taker_flow or dvol_daily)")
        return None
    last = min(px.index[-1], dv.index[-1])
    px = px[:last]
    iv = dv.reindex(px.index).to_numpy()
    p = px.values.astype(float)
    print(f"  prices {px.index[0].date()} … {last.date()}; DVOL "
          f"{dv.index[0].date()} … {dv.index[-1].date()}, mean "
          f"{np.nanmean(iv):.0%}")
    out = None
    for h in HORIZONS:
        res = compare(p, px.index, iv, h)
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
        for m in MODELS:
            for lv in LEVELS:
                e = res[m][lv]; c = e["cond"]
                print(f"  {m:<6}{lv:>6.0%}{e['cov']:>7.1%}{e['n_no']:>6}"
                      f"{e['cov_no']:>8.1%}{e['kupiec']:>10.3f}"
                      f"{e['width']:>8.1f}{c[0]:>8.1%}{c[1]:>7.1%}"
                      f"{c[2]:>7.1%}{e['cce'] * 100:>6.1f}")
            print()
        b, c, pm = res["mcnemar"]
        print(f"  calm, 90%, non-overlapping ({res['calm_no']} windows): v3 "
              f"fixed {b} v1 misses, created {c}; McNemar one-sided "
              f"p={pm:.3f}  (supplementary)")
        g = res["h12"]
        print(f"  H12 calm days split at median DVOL/EWMA ratio "
              f"{g['median_ratio']:.2f}: v1 90% breach rate high-ratio "
              f"{g['breach_hi']:.1%} vs low-ratio {g['breach_lo']:.1%} "
              f"(~{g['n_hi_no']}/{g['n_lo_no']} independent windows)")
        for m in MODELS:
            d, u = res[m]["dip_calm"], res[m]["run_calm"]
            print(f"  {m} calm-regime dip beaten {d[0]:.0%}/{d[1]:.0%}, "
                  f"run beaten {u[0]:.0%}/{u[1]:.0%}  (targets 20%/5%)")
        if h == PRIMARY_H:
            ok, why = verdict(res)
            print(f"\n  DECISION RULE ({name}):")
            for w in why:
                print(f"    {w}")
            print(f"    -> {'v3 BETTER on this asset' if ok else 'v3 NOT better on this asset'}")
            out = (ok, res)

    # today: spliced prices, latest day with both price and DVOL
    sp, _ = load_spliced(sym)
    d_last = min(sp.index[-1], dv.index[-1])
    sp = sp[:d_last]
    q = sp.values.astype(float)
    ivq = dv.reindex(sp.index).to_numpy()
    h = PRIMARY_H
    sc = scales(q, ivq, h)
    t = len(q) - 1
    ps = max(int(np.flatnonzero(np.isfinite(sc[m]))[0]) for m in MODELS)
    print(f"\n  TODAY ({d_last.date()}): ${q[-1]:,.2f}; DVOL {ivq[-1]:.0%}, "
          f"EWMA {sc['sig'][t] * math.sqrt(365):.0%} — descriptive; use v3 "
          f"only if ADOPTED")
    for m in MODELS:
        f = _forecasts(q, sc[m], h, np.array([t]), ps)
        lo80, lo90, lo95 = f["lo"][0]; hi80, hi90, hi95 = f["hi"][0]
        d5, d20 = f["dip"][0]; u5, u20 = f["run"][0]
        x = q[-1]
        print(f"  {m} 14d 80% ${x * math.exp(lo80):,.0f}–${x * math.exp(hi80):,.0f}"
              f"   95% ${x * math.exp(lo95):,.0f}–${x * math.exp(hi95):,.0f}"
              f"   dip {math.exp(-d5) - 1:+.1%}/{math.exp(-d20) - 1:+.1%}"
              f"   run {math.exp(u5) - 1:+.1%}/{math.exp(u20) - 1:+.1%}")
    return out


# ----------------------------------------------------------- self-test
def self_test() -> int:
    # 1. Scale units: 73% annual DVOL over 14 days.
    sc = scales(np.full(50, 100.0), np.full(50, 0.73), 14)
    assert abs(sc["v3"][0] - 0.73 * math.sqrt(14 / 365)) < 1e-12
    print(f"  [ok] DVOL 73% -> 14d scale {sc['v3'][0]:.4f}")

    # 2. Bootstrap: identical models -> D = 0, lower bound <= 0; a large
    #    clean improvement -> lower bound > 0.
    rng = np.random.default_rng(1)
    n = 1600
    calm = rng.random(n) < 1 / 3
    h1 = rng.random(n) < 0.80
    assert block_bootstrap_D(h1, h1, calm, 0.9) <= 0
    h3 = h1 | (rng.random(n) < 0.5)
    lb = block_bootstrap_D(h1, h3, calm, 0.9)
    assert lb > 0, lb
    print(f"  [ok] block bootstrap: identical models lb<=0; clear "
          f"improvement lb={lb:+.3f}")

    # 3. No lookahead: altering prices or DVOL after t0 leaves v3 levels
    #    at t0 unchanged.
    p, iv, dates = sim_pair("alt", 3)
    t0 = 1500
    p2, iv2 = p.copy(), iv.copy()
    p2[t0 + 1:] *= 2.0; iv2[t0 + 1:] *= 3.0
    s1, s2 = scales(p, iv, 14), scales(p2, iv2, 14)
    f1 = _forecasts(p, s1["v3"], 14, np.array([t0 - 30, t0]), 400)
    f2 = _forecasts(p2, s2["v3"], 14, np.array([t0 - 30, t0]), 400)
    for k in ("lo", "hi", "dip", "run"):
        assert np.array_equal(f1[k], f2[k], equal_nan=True), k
    print("  [ok] no lookahead: v3 levels at t unchanged when later prices "
          "and DVOL change")

    # 4. An informed 'DVOL' beats EWMA in calm; an uninformed one does not
    #    (averaged over a few seeds).
    dn, da = [], []
    for s in range(30, 34):
        for kind, acc in (("null", dn), ("alt", da)):
            pp, ii, dd = sim_pair(kind, s)
            acc.append(compare(pp, dd, ii, 14, n_boot=200)["D_calm"])
    print(f"  calm improvement D: informed DVOL {np.mean(da) * 100:+.1f} pts, "
          f"uninformed {np.mean(dn) * 100:+.1f} pts")
    assert np.mean(da) > np.mean(dn)
    print("  [ok] informed implied vol improves calm coverage more than "
          "uninformed")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Corridor v3: implied vol (DVOL)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--calibrate", type=int, nargs="?", const=200, default=None,
                    metavar="N")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.calibrate:
        report_calibration(calibrate(a.calibrate))
        return 0
    print("Prices: taker_flow spot; implied vol: dvol_daily (Deribit DVOL)")
    v = {}
    for name, sym, cur in (("BTC", "BTCUSDT", "BTC"), ("ETH", "ETHUSDT", "ETH")):
        v[name] = report_asset(name, sym, cur)
    print(f"\n{'=' * 84}")
    if any(x is None for x in v.values()):
        print("VERDICT: incomplete — an asset could not be tested.")
        return 1
    adopted = all(x[0] for x in v.values())
    lv = PRIMARY_LV
    print(f"VERDICT (pre-registered, both assets required): v3 "
          f"{'ADOPTED' if adopted else 'NOT ADOPTED'}")
    for hyp, g in (("H9 calm closer", 0), ("H10 storm closer", 2)):
        print(f"{hyp}: " + ", ".join(
            f"{k} {'yes' if abs(x[1]['v3'][lv]['cond'][g] - lv) < abs(x[1]['v1'][lv]['cond'][g] - lv) else 'no'}"
            for k, x in v.items()))
    print("H12 high ratio -> more breaches: " + ", ".join(
        f"{k} {'yes' if x[1]['h12']['breach_hi'] > x[1]['h12']['breach_lo'] else 'no'}"
        for k, x in v.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
