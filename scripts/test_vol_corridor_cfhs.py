"""
Fix 1: regime-conditional FHS. Can the calm-regime gap be closed by
calibrating calm days against calm history, without forecasting anything?

MOTIVATION
----------
Every corridor so far (v1 EWMA, v2 GARCH, v3 implied vol) under-covers in
calm regimes: a 90% band holds ~76-80% on calm days. Part is a selection
effect (a low vol estimate is disproportionately an under-estimate), part is
calm spells ending in jumps nobody forecasts. Neither needs forecasting to be
priced in: past windows that STARTED calm already contain both. So draw a
calm day's band from past calm-start windows only (and likewise mid, storm).

c1 changes ONE thing relative to v1 — which past windows enter the FHS pool.
Scale (EWMA, lambda 0.94), standardisation, warm-up, levels, test dates and
the evaluation regime split are identical.

PRE-REGISTRATION (written 2026-10-08, before running on real data)
------------------------------------------------------------------
Why other coins. Conditional FHS was first written down as a limitation
after v2's null on BTC/ETH (rule 12: not to be tested on data already
examined). The project's own universe (config/universe.py) minus BTC/ETH is
9 coins whose corridor calibration has never been examined:
    BNB SOL XRP ADA AVAX LINK DOT LTC ATOM
The list is fixed by that file, not chosen here. Prices: taker_flow spot
daily closes (Binance archive), from each coin's listing.

Models (forecast at close t for t+h)
  v1  pooled FHS: quantiles of ALL past standardised h-day returns
      Z[s] = R_h[s] / (sigma_s sqrt(h)), s <= t-h, times sigma_t sqrt(h).
  c1  regime-conditional FHS: same, but only windows whose start s had the
      SAME real-time regime label as t.
Real-time label: percentile of sigma_s among all EWMA values up to s
(expanding; needs 365 values): calm < 1/3, storm > 2/3, else mid. Known at
s — no lookahead. Pools need >= 365 windows (v1: total; c1: per regime).
Both models are scored only on dates where both have a forecast.

PRIMARY: 14d, 90%, POOLED across the 9 coins. Evaluation regimes = each
coin's EWMA terciles over its test dates (the split used for v1-v3).

Hypotheses and prior, on record
  H13  pooled calm coverage closer to 90% under c1.          prior: yes
  H14  pooled storm coverage closer to 90% under c1.         prior: yes
  H15  c1 does not worsen unconditional calibration.         prior: yes
  H16  the calm gain is broad, not one or two coins.         prior: yes

Decision rule, fixed in advance. c1 is ADOPTED only if ALL hold (14d, 90%):
  (a) pooled calm improvement D = |cov_calm(v1)-90%| - |cov_calm(c1)-90%|
      has a one-sided 95% lower bound > 0 by JOINT-TIME moving-block
      bootstrap (90-day calendar blocks shared by all coins, so cross-coin
      correlation is kept; 2,000 resamples; seed 20261008);
  (b) pooled conditional calibration error (mean |cov - 90%| over calm,
      mid, storm) is lower for c1;
  (c) c1 fails Kupiec (p < 0.05, non-overlapping) in at most 2 more of the
      27 coin x {80,90,95} cells than v1 does;
  (d) D > 0 in at least 6 of the 9 coins.
(d) was added because v3's pass rested on a handful of episodes: a pooled
result driven by one or two coins should not pass.

REVISION BEFORE REAL DATA (2026-10-08). (c) originally read "no more
Kupiec failures than v1". Synthetic calibration showed c1's per-regime pools
(a third the size) add per-coin noise even when c1 is the better model:
that condition alone cut power from ~84% to 42.5%. Tolerance set to +2
cells, which a correct c1 exceeds 11% of the time and a useless one never
did. Changed after synthetic runs only; no real data had been loaded.

Calibration of the final rule (synthetic 9-coin panels: common Student-t
factor, correlation ~0.6, own GARCH(0.10, 0.85) per coin, staggered listings
like the real universe; seeds 7000+):
  null  c1 with RANDOM regime labels (pure noise): D mean -0.4 pts.
        (a) and (d) together pass 1.5% of 200 panels -> false positives
        <= 1.5%; full rule 0 of 100.
  alt   c1 with real labels: calm 84.3% -> 87.6%, storm 94.2% -> 89.6%.
        Full rule passes 75% of 100 panels = power.
  The joint bootstrap's lower bound sat below the true mean in 96% of alt
  panels but only 82% of null panels: it ignores the noise of which windows
  land in each pool. Rely on the synthetic false-positive rate, not on the
  bootstrap alone.

Supplementary, outside the rule: 7d; per-coin table; pooled McNemar on
non-overlapping calm windows; band widths.

Limitations on record: survivors only (dead tokens excluded; their calm
spells ended worst); coins move with BTC, so 9 coins are not 9 independent
tests; one shared calendar 2018-2026. NO RESCUE: ideas formed after the
result are recorded as limitations, not run.

USAGE
-----
    python -m collectors.taker_flow_backfill --market spot \
        --symbols BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,DOTUSDT,LTCUSDT,ATOMUSDT
    python -m scripts.test_vol_corridor_cfhs --self-test
    python -m scripts.test_vol_corridor_cfhs --calibrate      # synthetic, ~15 min
    python -m scripts.test_vol_corridor_cfhs                  # the test
"""

from __future__ import annotations

import argparse
import bisect
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.test_vol_corridor import (WARMUP, ewma_vol, h_returns,  # noqa: E402
                                       kupiec, load_closes, log_returns)
from scripts.test_vol_corridor_v2 import mcnemar_one_sided  # noqa: E402

LEVELS = (0.80, 0.90, 0.95)
HORIZONS = (14, 7)
PRIMARY_H, PRIMARY_LV = 14, 0.90
MIN_POOL = WARMUP                      # 365, per regime for c1
LABEL_WARMUP = 365
BLOCK, N_BOOT, BOOT_SEED = 90, 2000, 20261008
MIN_POSITIVE = 6
KUPIEC_TOL = 2
EXCLUDE = ("BTCUSDT", "ETHUSDT")


def coin_list() -> list[str]:
    from config.universe import TRADEABLE_UNIVERSE
    return [s for s in TRADEABLE_UNIVERSE if s not in EXCLUDE]


# ------------------------------------------------------------- mechanics
def realtime_labels(sig: np.ndarray, warm: int = LABEL_WARMUP) -> np.ndarray:
    """Regime at s from the percentile of sig[s] among sig[0..s] (finite
    values only). -1 until `warm` values exist. Uses nothing after s."""
    lab = np.full(len(sig), -1, dtype=np.int8)
    seen: list[float] = []
    for s, x in enumerate(sig):
        if not np.isfinite(x):
            continue
        bisect.insort(seen, x)
        if len(seen) < warm:
            continue
        pct = bisect.bisect_left(seen, x) / len(seen)
        lab[s] = 0 if pct < 1 / 3 else (2 if pct > 2 / 3 else 1)
    return lab


def coin_forecasts(p: np.ndarray, h: int, labels: np.ndarray | None = None
                   ) -> dict:
    """v1 and c1 FHS levels on every date where both exist."""
    r = log_returns(p)
    sig = ewma_vol(r)
    sh = sig * math.sqrt(h)
    R = h_returns(p, h)
    Z = R / sh
    lab = realtime_labels(sig) if labels is None else labels
    fin = np.flatnonzero(np.isfinite(sh))
    if len(fin) == 0:
        return {}
    ps = int(fin[0])
    zok = np.isfinite(Z)
    idx = [np.flatnonzero((lab == g) & zok) for g in range(3)]
    qs = [(1 - lv) / 2 for lv in LEVELS] + [(1 + lv) / 2 for lv in LEVELS]
    k = len(LEVELS)
    T, L1, H1, L2, H2 = [], [], [], [], []
    for t in range(ps + WARMUP + h, len(p) - h):
        g = lab[t]
        if g < 0 or not np.isfinite(sh[t]):
            continue
        end = t - h + 1
        ig = idx[g]
        m = int(np.searchsorted(ig, end))
        if m < MIN_POOL:
            continue
        z1 = Z[ps:end]
        z1 = z1[np.isfinite(z1)]
        q1 = np.quantile(z1, qs) * sh[t]
        q2 = np.quantile(Z[ig[:m]], qs) * sh[t]
        T.append(t); L1.append(q1[:k]); H1.append(q1[k:])
        L2.append(q2[:k]); H2.append(q2[k:])
    if not T:
        return {}
    T = np.array(T)
    return {"t": T, "R": R[T], "sig": sig[T],
            "v1": (np.array(L1), np.array(H1)),
            "c1": (np.array(L2), np.array(H2))}


def score_coin(fc: dict, dates: pd.DatetimeIndex, h: int) -> dict:
    """Per-coin hits, evaluation regimes and Kupiec, aligned to dates."""
    t = fc["t"]
    terc = np.quantile(fc["sig"], [1 / 3, 2 / 3])
    reg = np.where(fc["sig"] <= terc[0], 0,
                   np.where(fc["sig"] <= terc[1], 1, 2))
    no = ((t - t[0]) % h) == 0
    out = {"day": dates[t].values.astype("datetime64[D]"), "reg": reg,
           "no": no, "start": dates[t[0]].date(), "end": dates[t[-1] + h].date()}
    for m in ("v1", "c1"):
        lo, hi = fc[m]
        hits = (fc["R"][:, None] >= lo) & (fc["R"][:, None] <= hi)
        out[m] = {"hits": hits,
                  "width": np.mean(np.exp(hi) - np.exp(lo), axis=0) * 100,
                  "kupiec": [kupiec(int(no.sum()), int((~hits[no, j]).sum()),
                                    1 - lv) for j, lv in enumerate(LEVELS)]}
    j = LEVELS.index(PRIMARY_LV)
    calm = reg == 0
    out["D"] = (abs(out["v1"]["hits"][calm, j].mean() - PRIMARY_LV)
                - abs(out["c1"]["hits"][calm, j].mean() - PRIMARY_LV))
    return out


def pooled(scored: dict[str, dict]) -> dict:
    """Pool coin-days; coverage by evaluation regime; CCE; per-day sums for
    the joint-time bootstrap."""
    j = LEVELS.index(PRIMARY_LV)
    reg = np.concatenate([s["reg"] for s in scored.values()])
    res = {}
    for m in ("v1", "c1"):
        hits = np.concatenate([s[m]["hits"] for s in scored.values()])
        res[m] = {}
        for jj, lv in enumerate(LEVELS):
            cond = [float(hits[reg == g, jj].mean()) for g in range(3)]
            res[m][lv] = {"cov": float(hits[:, jj].mean()), "cond": cond,
                          "cce": float(np.mean([abs(c - lv) for c in cond]))}
        res[m]["width"] = np.mean([s[m]["width"] for s in scored.values()], axis=0)
    lv = PRIMARY_LV
    res["D"] = (abs(res["v1"][lv]["cond"][0] - lv)
                - abs(res["c1"][lv]["cond"][0] - lv))
    # per-calendar-day sums over coins, calm days only
    days = np.concatenate([s["day"] for s in scored.values()])
    calm = reg == 0
    h1 = np.concatenate([s["v1"]["hits"][:, j] for s in scored.values()])
    h2 = np.concatenate([s["c1"]["hits"][:, j] for s in scored.values()])
    d0 = days.min()
    di = (days - d0).astype(int)
    nday = int(di.max()) + 1
    res["day_calm"] = np.bincount(di, weights=calm, minlength=nday)
    res["day_h1"] = np.bincount(di, weights=calm & h1, minlength=nday)
    res["day_h2"] = np.bincount(di, weights=calm & h2, minlength=nday)
    # McNemar on pooled non-overlapping calm windows (supplementary)
    no = np.concatenate([s["no"] for s in scored.values()])
    sel = no & calm
    res["mcnemar"] = (int((h2[sel] & ~h1[sel]).sum()),
                      int((h1[sel] & ~h2[sel]).sum()))
    res["mcnemar"] += (mcnemar_one_sided(*res["mcnemar"]),)
    return res


def joint_bootstrap_lb(day_calm, day_h1, day_h2, lv=PRIMARY_LV,
                       n_boot=N_BOOT, block=BLOCK, seed=BOOT_SEED) -> float:
    """One-sided 95% lower bound of pooled D; calendar blocks shared by all
    coins (per-day sums), so cross-coin correlation is preserved."""
    n = len(day_calm)
    L = min(block, n)
    rng = np.random.default_rng(seed)
    k = -(-n // L)
    starts = rng.integers(0, n - L + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(L)).reshape(n_boot, -1)[:, :n]
    c = day_calm[idx].sum(1)
    ok = c > 0
    m1 = day_h1[idx].sum(1)[ok] / c[ok]
    m2 = day_h2[idx].sum(1)[ok] / c[ok]
    return float(np.quantile(np.abs(m1 - lv) - np.abs(m2 - lv), 0.05))


def kupiec_failures(scored: dict[str, dict], m: str) -> int:
    return sum(int(p < 0.05) for s in scored.values() for p in s[m]["kupiec"])


def decide(scored: dict[str, dict], pool: dict, n_boot: int = N_BOOT) -> dict:
    lv = PRIMARY_LV
    lb = joint_bootstrap_lb(pool["day_calm"], pool["day_h1"], pool["day_h2"],
                            n_boot=n_boot)
    kf1, kf2 = kupiec_failures(scored, "v1"), kupiec_failures(scored, "c1")
    npos = sum(int(s["D"] > 0) for s in scored.values())
    need = min(MIN_POSITIVE, len(scored))
    a = lb > 0
    b = pool["c1"][lv]["cce"] < pool["v1"][lv]["cce"]
    c = kf2 <= kf1 + KUPIEC_TOL
    d = npos >= need
    return {"lb": lb, "kf": (kf1, kf2), "npos": npos, "ok": a and b and c and d,
            "why": [f"(a) pooled calm improvement {pool['D'] * 100:+.1f} pts, "
                    f"joint bootstrap 95% lower bound {lb * 100:+.1f}: "
                    f"{'PASS' if a else 'FAIL'} (needs > 0)",
                    f"(b) pooled CCE c1 {pool['c1'][lv]['cce'] * 100:.1f} vs v1 "
                    f"{pool['v1'][lv]['cce'] * 100:.1f}: {'PASS' if b else 'FAIL'}",
                    f"(c) Kupiec failures c1 {kf2} vs v1 {kf1} (of "
                    f"{3 * len(scored)}; allowed v1+{KUPIEC_TOL}): "
                    f"{'PASS' if c else 'FAIL'}",
                    f"(d) coins with D > 0: {npos} of {len(scored)} (need "
                    f"{need}): {'PASS' if d else 'FAIL'}"]}


# ----------------------------------------------------------- calibration
SIM_LENGTHS = (3250, 3200, 3100, 3050, 2800, 2700, 2230, 2230, 2200)


def sim_panel(seed: int, a: float = 0.10, b: float = 0.85, nu: float = 4,
              rho: float = 0.6, daily_vol: float = 0.045, N: int = 3300):
    """9 coins: common Student-t factor (correlation ~rho), own GARCH each,
    listed at staggered dates like the real universe."""
    rng = np.random.default_rng(seed)
    sc = math.sqrt(nu / (nu - 2))
    z = rng.standard_t(nu, N) / sc
    vbar = daily_vol ** 2
    w = vbar * (1 - a - b)
    dates = pd.date_range(end="2026-09-30", periods=N)
    coins = {}
    for i, Lc in enumerate(SIM_LENGTHS):
        e = rng.standard_t(nu, N) / sc
        shock = rho * z + math.sqrt(1 - rho ** 2) * e
        r = np.empty(N); v = vbar
        for t in range(N):
            r[t] = math.sqrt(v) * shock[t]
            v = w + a * r[t] ** 2 + b * v
        p = 100 * np.exp(np.cumsum(r[N - Lc:]))
        coins[f"SIM{i}"] = (dates[N - Lc:], p)
    return coins


def run_panel(coins: dict, h: int, label_mode: str = "real", seed: int = 0,
              n_boot: int = N_BOOT):
    rng = np.random.default_rng(seed)
    scored = {}
    for name, (dates, p) in coins.items():
        labels = None
        if label_mode == "random":
            real = realtime_labels(ewma_vol(log_returns(p)))
            labels = np.where(real >= 0, rng.integers(0, 3, len(p)), -1).astype(np.int8)
        fc = coin_forecasts(p, h, labels)
        if fc:
            scored[name] = score_coin(fc, dates, h)
    pool = pooled(scored)
    return scored, pool, decide(scored, pool, n_boot=n_boot)


def calibrate(n_panels: int = 200, n_boot: int = 1000, seed0: int = 7000):
    out = {"null": [], "alt": []}
    for i in range(n_panels):
        coins = sim_panel(seed0 + i)
        for kind, mode in (("null", "random"), ("alt", "real")):
            sc, pool, dec = run_panel(coins, PRIMARY_H, mode, seed=seed0 + i,
                                      n_boot=n_boot)
            out[kind].append((pool["D"], dec["lb"], dec["ok"], dec["npos"],
                              pool["v1"][PRIMARY_LV]["cond"][0],
                              pool["c1"][PRIMARY_LV]["cond"][0],
                              pool["v1"][PRIMARY_LV]["cond"][2],
                              pool["c1"][PRIMARY_LV]["cond"][2]))
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def report_calibration(out: dict):
    for kind, label in (("null", "random labels (c1 uninformative)"),
                        ("alt", "real labels, mean-reverting GARCH")):
        a = out[kind]
        D, lb = a[:, 0], a[:, 1]
        print(f"  {kind:<5} {label}, n={len(a)}")
        print(f"        D mean {D.mean() * 100:+.1f} pts (sd {D.std() * 100:.1f}); "
              f"calm v1 {np.median(a[:, 4]):.1%} -> c1 {np.median(a[:, 5]):.1%}; "
              f"storm v1 {np.median(a[:, 6]):.1%} -> c1 {np.median(a[:, 7]):.1%}")
        print(f"        lb > 0 in {np.mean(lb > 0):.1%}; full rule passes "
              f"{np.mean(a[:, 2]):.1%}; lb below true mean in "
              f"{np.mean(lb <= D.mean()):.1%} (target >= 95%); median coins "
              f"D>0: {np.median(a[:, 3]):.0f}/9")


# -------------------------------------------------------------- report
def report(h: int, coins: dict) -> tuple[dict, dict, dict]:
    scored = {}
    for sym, (dates, p) in coins.items():
        fc = coin_forecasts(p, h)
        if fc:
            scored[sym] = score_coin(fc, dates, h)
        else:
            print(f"  {sym}: too little history for a {h}d test")
    pool = pooled(scored)
    dec = decide(scored, pool)
    tag = "PRIMARY" if h == PRIMARY_H else "secondary"
    j = LEVELS.index(PRIMARY_LV)
    print(f"\n  {h}d ({tag}) — per coin, 90% band (evaluation regimes)")
    print(f"  {'coin':<10}{'test from':>12}{'days':>6}{'calm v1':>9}{'c1':>7}"
          f"{'storm v1':>10}{'c1':>7}{'D pts':>8}{'Kupiec fails v1/c1':>20}")
    for sym, s in scored.items():
        calm, storm = s["reg"] == 0, s["reg"] == 2
        k1 = sum(p < .05 for p in s["v1"]["kupiec"])
        k2 = sum(p < .05 for p in s["c1"]["kupiec"])
        print(f"  {sym:<10}{str(s['start']):>12}{len(s['reg']):>6}"
              f"{s['v1']['hits'][calm, j].mean():>9.1%}"
              f"{s['c1']['hits'][calm, j].mean():>7.1%}"
              f"{s['v1']['hits'][storm, j].mean():>10.1%}"
              f"{s['c1']['hits'][storm, j].mean():>7.1%}"
              f"{s['D'] * 100:>+8.1f}{k1:>12}/{k2}")
    print(f"\n  POOLED ({len(scored)} coins)")
    print(f"  {'model':<6}{'level':>6}{'cov':>7}{'calm':>8}{'mid':>7}"
          f"{'storm':>7}{'CCE':>6}{'width%':>8}")
    for m in ("v1", "c1"):
        for jj, lv in enumerate(LEVELS):
            e = pool[m][lv]; c = e["cond"]
            print(f"  {m:<6}{lv:>6.0%}{e['cov']:>7.1%}{c[0]:>8.1%}{c[1]:>7.1%}"
                  f"{c[2]:>7.1%}{e['cce'] * 100:>6.1f}{pool[m]['width'][jj]:>8.1f}")
        print()
    b, c, pm = pool["mcnemar"]
    print(f"  pooled non-overlapping calm windows: c1 fixed {b} v1 misses, "
          f"created {c}; McNemar one-sided p={pm:.3f} (supplementary; coins "
          f"are correlated, so p is optimistic)")
    if h == PRIMARY_H:
        print("\n  DECISION RULE:")
        for w in dec["why"]:
            print(f"    {w}")
    return scored, pool, dec


# ----------------------------------------------------------- self-test
def self_test() -> int:
    from scripts.test_vol_corridor import _garch_sim
    # 1. Labels: real time only; ~1/3 each on stationary data.
    s = _garch_sim(3000, lambda g: g.standard_normal(), 4)
    sig = ewma_vol(log_returns(s.values))
    lab = realtime_labels(sig)
    sig2 = sig.copy(); sig2[2000:] *= 5
    assert np.array_equal(lab[:2000], realtime_labels(sig2)[:2000])
    share = np.bincount(lab[lab >= 0], minlength=3) / (lab >= 0).sum()
    assert np.all(np.abs(share - 1 / 3) < 0.08), share
    assert np.all(lab[:LABEL_WARMUP] == -1)
    print(f"  [ok] real-time labels: unchanged by later data; shares "
          f"{np.round(share, 2)}")

    # 2. One constant label everywhere -> c1 pool == v1 pool -> identical.
    p = s.values
    const = np.zeros(len(p), dtype=np.int8)
    fc = coin_forecasts(p, 14, const)
    assert np.allclose(fc["v1"][0], fc["c1"][0]) and np.allclose(fc["v1"][1], fc["c1"][1])
    print("  [ok] a single regime label reproduces v1 exactly")

    # 3. No lookahead: forecasts at t unchanged when later prices change.
    p2 = p.copy(); p2[2200:] *= 2.0
    f1, f2 = coin_forecasts(p, 14), coin_forecasts(p2, 14)
    keep1, keep2 = f1["t"] <= 2199, f2["t"] <= 2199
    assert np.array_equal(f1["t"][keep1], f2["t"][keep2])
    for m in ("v1", "c1"):
        for q in (0, 1):
            assert np.array_equal(f1[m][q][keep1], f2[m][q][keep2]), m
    print("  [ok] no lookahead: c1 and v1 levels unchanged when later prices "
          "change")

    # 4. Joint bootstrap: identical models -> lb <= 0; per-day sums correct.
    rng = np.random.default_rng(2)
    dc = rng.integers(0, 4, 1500).astype(float)
    h1 = np.minimum(dc, rng.binomial(dc.astype(int), 0.8)).astype(float)
    assert joint_bootstrap_lb(dc, h1, h1) <= 0
    h2 = np.minimum(dc, h1 + rng.binomial(1, 0.3, 1500))
    assert joint_bootstrap_lb(dc, h1, h2) > 0
    print("  [ok] joint bootstrap: identical models lb <= 0; clear gain lb > 0")

    # 5. Mechanism on a small synthetic panel: real labels help calm,
    #    random labels do not (averaged over 2 panels).
    dr, dn = [], []
    for sd in (11, 12):
        coins = dict(list(sim_panel(sd).items())[:4])
        dr.append(run_panel(coins, 14, "real", n_boot=200)[1]["D"])
        dn.append(run_panel(coins, 14, "random", seed=sd, n_boot=200)[1]["D"])
    print(f"  calm improvement D: real labels {np.mean(dr) * 100:+.1f} pts, "
          f"random labels {np.mean(dn) * 100:+.1f} pts")
    assert np.mean(dr) > np.mean(dn) and np.mean(dr) > 0
    print("  [ok] conditioning on the real regime helps calm; random labels don't")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fix 1: regime-conditional FHS")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--calibrate", type=int, nargs="?", const=200, default=None,
                    metavar="N")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.calibrate:
        report_calibration(calibrate(a.calibrate))
        return 0
    syms = coin_list()
    print(f"Coins (config/universe.py minus BTC/ETH): {', '.join(syms)}")
    coins, missing = {}, []
    for sym in syms:
        px = load_closes(sym, True)
        if px.empty:
            missing.append(sym)
            continue
        coins[sym] = (px.index, px.values.astype(float))
        age = (pd.Timestamp.today().normalize() - px.index[-1]).days
        print(f"  {sym:<10}{px.index[0].date()} … {px.index[-1].date()}"
              f"{'   WARNING: ' + str(age) + ' days old' if age > 45 else ''}")
    if missing:
        print(f"\nNO taker_flow spot data for: {', '.join(missing)}. Run:\n"
              f"  python -m collectors.taker_flow_backfill --market spot "
              f"--symbols {','.join(missing)}")
        return 1
    verdict = None
    for h in HORIZONS:
        _, pool, dec = report(h, coins)
        if h == PRIMARY_H:
            verdict = (dec, pool)
    dec, pool = verdict
    lv = PRIMARY_LV
    print(f"\n{'=' * 84}\nVERDICT (pre-registered): c1 "
          f"{'ADOPTED' if dec['ok'] else 'NOT ADOPTED'}")
    print(f"H13 calm closer: {'yes' if pool['D'] > 0 else 'no'}; H14 storm "
          f"closer: {'yes' if abs(pool['c1'][lv]['cond'][2] - lv) < abs(pool['v1'][lv]['cond'][2] - lv) else 'no'}; "
          f"H15 Kupiec failures c1 {dec['kf'][1]} vs v1 {dec['kf'][0]}; "
          f"H16 coins with D>0 {dec['npos']}/{len(coins)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
