"""
Can we forecast the CORRIDOR, even though we cannot forecast direction?

MOTIVATION
----------
Ten signal candidates failed to predict direction (README ledger). But
volatility clusters — big moves follow big moves, calm follows calm — and is
among the most reliably forecastable quantities in finance. The question
shifts from "which way?" to "how far?": a probability corridor.

A corridor is only worth anything if it is CALIBRATED: a band claiming 90%
must contain the outcome ~90% of the time. The probability-theory version of
a backtest is therefore a COVERAGE TEST.

PRE-REGISTRATION (written 2026-10-07, before running on real data)
-------------------------------------------------------------------
Four corridor methods, each forecast at the close of day t for t+h:

  gauss_ewma  Gaussian band, EWMA volatility (RiskMetrics, lambda=0.94)
  fhs         filtered historical simulation: empirical quantiles of PAST
              standardised h-day returns, rescaled by current EWMA vol.
              Captures fat tails and skew, adapts to the current regime.
  raw_emp     raw empirical quantiles of past h-day returns (no vol
              scaling) — the naive baseline.
  implied     Gaussian band from Deribit ATM implied volatility (ETH only;
              eth-capture options data starts 2026-04, so SHORT SAMPLE,
              descriptive only).

PRIMARY: method=fhs, h=14, 90% central band, BTC and ETH, Kupiec test on
NON-OVERLAPPING forecasts.

Hypotheses and prior, on record:
  H1  gauss_ewma mis-covers at the extremes (fat tails): under-covers at
      99%, with breaches skewed to the DOWNSIDE.             prior: yes
  H2  fhs is calibrated (Kupiec p > 0.05) at 80/90/95% on both assets.
                                                             prior: roughly
  H3  raw_emp fails CONDITIONALLY: breaches concentrate in high-vol
      regimes, because a fixed band is too narrow in storms and too wide
      in calm.                                               prior: yes
  H4  implied vol exceeds subsequently realised vol (variance risk premium),
      so implied bands over-cover.                           prior: yes

Decision rule fixed in advance: a method is CALIBRATED if Kupiec p > 0.05
at 80, 90 and 95% on the non-overlapping sample for BOTH assets.

METHOD NOTES
------------
* NO LOOKAHEAD. Every band at t uses prices through t only. FHS and raw_emp
  quantiles use h-day returns that COMPLETED by t (start s <= t-h).
  Expanding windows, 365-day warm-up. Verified in self-test.
* OVERLAP. Daily forecasts of 14-day outcomes overlap by 13 days, so hits
  are not independent. Coverage is shown on all days (stable estimate) but
  the Kupiec test uses non-overlapping forecasts only (every h-th day) —
  the coverage-test version of "count events, not days".
* TERMINAL vs PATH. Calibration is on the terminal price at t+h. The path
  touch rate (did price touch either edge at any point inside h?) is shown
  separately; it always exceeds the terminal breach rate, by about
  1.5-2x when measured on daily closes.
* Crypto trades every day: h is calendar days, annualisation uses 365.

USAGE
-----
    python -m scripts.test_vol_corridor --self-test
    python -m scripts.test_vol_corridor                # price_snapshots, 2020-11+
    python -m scripts.test_vol_corridor --long         # taker_flow spot, 2017+
    python -m scripts.test_vol_corridor --moves        # move sizes up / down

--moves answers "if it falls, how far? if it rises, how far?" from the
latest close (sizes only — it does not say which way). Use the default
price source for current numbers; taker_flow is only as fresh as its last
backfill.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from statistics import NormalDist

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LAMBDA = 0.94
SEED_DAYS = 30
WARMUP = 365
LEVELS = (0.50, 0.80, 0.90, 0.95, 0.99)
HORIZONS = (7, 14)
METHODS = ("gauss_ewma", "fhs", "raw_emp")
PRIMARY = dict(method="fhs", h=14, level=0.90)
ND = NormalDist()


# ---------------------------------------------------------------- data
def load_closes(symbol: str, long_history: bool) -> pd.Series:
    from config.database import get_connection, get_cursor
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        if long_history:
            cur.execute("""SELECT d, close FROM taker_flow
                           WHERE market = 'spot' AND symbol = %s
                           ORDER BY d""", (symbol,))
        else:
            cur.execute("""
                SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, close
                FROM price_snapshots WHERE symbol = %s
                ORDER BY timestamp::date, timestamp DESC""", (symbol,))
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
    s = s[s > 0]
    full = pd.date_range(s.index[0], s.index[-1], freq="D")
    return s.reindex(full).ffill(limit=3).dropna()


def load_iv_surface() -> pd.DataFrame:
    """Last eth-capture snapshot of each day, with its options chain."""
    from config.database import get_connection, get_cursor
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("""
            WITH day_snap AS (
                SELECT DISTINCT ON (s.captured_at::date)
                       s.captured_at::date AS d, s.id, s.spot_price
                FROM eth_snapshots s
                ORDER BY s.captured_at::date, s.captured_at DESC
            )
            SELECT ds.d, ds.spot_price, o.expiry_date, o.strike,
                   o.option_type, o.iv
            FROM day_snap ds
            JOIN eth_options_chain o ON o.snapshot_id = ds.id
            WHERE o.iv IS NOT NULL AND o.iv > 0""")
        rows = cur.fetchall()
    finally:
        conn.close()
    cols = ["d", "spot", "expiry", "strike", "otype", "iv"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = (pd.DataFrame(rows) if isinstance(rows[0], dict)
          else pd.DataFrame(rows, columns=cols))
    df.columns = cols
    df["d"] = pd.to_datetime(df["d"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


# ------------------------------------------------------------ mechanics
def log_returns(p: np.ndarray) -> np.ndarray:
    r = np.full(len(p), np.nan)
    r[1:] = np.log(p[1:] / p[:-1])
    return r


def ewma_vol(r: np.ndarray, lam: float = LAMBDA, seed: int = SEED_DAYS
             ) -> np.ndarray:
    """Daily EWMA vol. sig[t] uses returns through t (known at close t)."""
    n = len(r)
    sig = np.full(n, np.nan)
    if n <= seed + 1:
        return sig
    v = float(np.nanvar(r[1:seed + 1]))
    for i in range(seed + 1, n):
        v = lam * v + (1 - lam) * r[i] ** 2
        sig[i] = math.sqrt(v)
    return sig


def h_returns(p: np.ndarray, h: int) -> np.ndarray:
    """R[s] = log(P[s+h]/P[s]); NaN where s+h is beyond the data."""
    R = np.full(len(p), np.nan)
    R[:-h] = np.log(p[h:] / p[:-h])
    return R


def band(method: str, t: int, h: int, level: float, sig: np.ndarray,
         R: np.ndarray, Z: np.ndarray) -> tuple[float, float] | None:
    """Central log-return band at forecast date t. Uses data <= t only."""
    lo_q, hi_q = (1 - level) / 2, (1 + level) / 2
    s_t = sig[t]
    if method == "gauss_ewma":
        if not np.isfinite(s_t):
            return None
        z = ND.inv_cdf(hi_q)
        w = z * s_t * math.sqrt(h)
        return -w, w
    hist_end = t - h + 1                       # starts s <= t-h completed by t
    if hist_end < WARMUP:
        return None
    if method == "fhs":
        zh = Z[:hist_end]
        zh = zh[np.isfinite(zh)]
        if len(zh) < WARMUP or not np.isfinite(s_t):
            return None
        q = np.quantile(zh, [lo_q, hi_q])
        scale = s_t * math.sqrt(h)
        return float(q[0] * scale), float(q[1] * scale)
    if method == "raw_emp":
        rh = R[:hist_end]
        rh = rh[np.isfinite(rh)]
        if len(rh) < WARMUP:
            return None
        q = np.quantile(rh, [lo_q, hi_q])
        return float(q[0]), float(q[1])
    raise ValueError(method)


def kupiec(n: int, x: int, p: float) -> float:
    """Kupiec proportion-of-failures test p-value (chi2, 1 df)."""
    if n == 0:
        return float("nan")
    def ll(q):
        a = (n - x) * math.log(1 - q) if q < 1 else (0 if x == n else -1e300)
        b = x * math.log(q) if q > 0 else (0 if x == 0 else -1e300)
        return a + b
    lr = max(0.0, -2 * (ll(p) - ll(x / n)))
    return math.erfc(math.sqrt(lr / 2))


def evaluate(prices: pd.Series, method: str, h: int, level: float) -> dict:
    p = prices.values.astype(float)
    n = len(p)
    r = log_returns(p)
    sig = ewma_vol(r)
    R = h_returns(p, h)
    Z = R / (sig * math.sqrt(h))
    rows = []
    for t in range(n - h):
        b = band(method, t, h, level, sig, R, Z)
        if b is None or not np.isfinite(R[t]):
            continue
        lo, hi = b
        path = np.log(p[t + 1:t + h + 1] / p[t])
        rows.append((t, lo, hi, R[t], path.min(), path.max(), sig[t]))
    if not rows:
        return {"n_all": 0}
    a = np.array(rows)
    t_idx, lo, hi, out, pmin, pmax, sg = a.T
    below, above = out < lo, out > hi
    hit = ~below & ~above
    touch = (pmin < lo) | (pmax > hi)

    # non-overlapping subsample: every h-th forecast from the first one
    first = int(t_idx[0])
    no = ((t_idx - first) % h) == 0
    n_no, x_no = int(no.sum()), int((~hit[no]).sum())

    # conditional coverage by current-vol tercile (evaluation split only)
    terc = np.quantile(sg, [1 / 3, 2 / 3])
    regime = np.where(sg <= terc[0], "calm",
                      np.where(sg <= terc[1], "mid", "storm"))
    cond = {g: float(hit[regime == g].mean()) for g in ("calm", "mid", "storm")}

    return {
        "n_all": len(a), "cov_all": float(hit.mean()),
        "below_all": float(below.mean()), "above_all": float(above.mean()),
        "n_no": n_no, "breaches_no": x_no,
        "cov_no": 1 - x_no / n_no if n_no else float("nan"),
        "kupiec_p": kupiec(n_no, x_no, 1 - level),
        "width_pct": float(np.mean(np.exp(hi) - np.exp(lo)) * 100),
        "touch": float(touch.mean()), "cond": cond,
        "start": prices.index[int(t_idx[0])].date(),
    }


def current_band(prices: pd.Series, method: str, h: int, level: float):
    p = prices.values.astype(float)
    r = log_returns(p)
    sig = ewma_vol(r)
    R = h_returns(p, h)
    Z = R / (sig * math.sqrt(h))
    b = band(method, len(p) - 1, h, level, sig, R, Z)
    if b is None:
        return None
    last = p[-1]
    return last * math.exp(b[0]), last * math.exp(b[1])


# ---------------------------------------------------- implied volatility
def normalise_iv_units(iv: pd.Series) -> pd.Series:
    """Deribit reports mark IV in percent (e.g. 55.3). Detect and scale."""
    return iv / 100.0 if iv.median() > 3 else iv


def atm_term_structure(surface: pd.DataFrame) -> dict:
    """{date: [(tenor_days, atm_iv), ...]} — ATM = strike nearest spot,
    averaging call and put IV at that strike."""
    if surface.empty:
        return {}
    surface = surface.copy()
    surface["iv"] = normalise_iv_units(surface["iv"].astype(float))
    out = {}
    for d, g in surface.groupby("d"):
        spot = float(g["spot"].iloc[0])
        ts = []
        for exp, ge in g.groupby("expiry"):
            T = (exp - d).days
            if T < 2:
                continue
            k = ge.loc[(ge["strike"] - spot).abs().idxmin(), "strike"]
            ts.append((T, float(ge.loc[ge["strike"] == k, "iv"].mean())))
        if ts:
            out[d] = sorted(ts)
    return out


def implied_vol_at(ts: list, h: int) -> float:
    """Interpolate ATM IV to tenor h, linear in total variance."""
    if h <= ts[0][0]:
        return ts[0][1]
    if h >= ts[-1][0]:
        return ts[-1][1]
    for (t1, v1), (t2, v2) in zip(ts, ts[1:]):
        if t1 <= h <= t2:
            w1, w2 = v1 * v1 * t1, v2 * v2 * t2
            w = w1 + (w2 - w1) * (h - t1) / (t2 - t1)
            return math.sqrt(w / h)
    return ts[-1][1]


def evaluate_implied(prices: pd.Series, terms: dict, h: int, level: float
                     ) -> dict:
    z = ND.inv_cdf((1 + level) / 2)
    hits, below, above, ivs, rvs, widths = [], 0, 0, [], [], []
    for d, ts in sorted(terms.items()):
        end = d + pd.Timedelta(days=h)
        if d not in prices.index or end not in prices.index:
            continue
        iv = implied_vol_at(ts, h)
        w = z * iv * math.sqrt(h / 365)
        out = math.log(prices.loc[end] / prices.loc[d])
        hits.append(-w <= out <= w)
        below += out < -w
        above += out > w
        widths.append((math.exp(w) - math.exp(-w)) * 100)
        win = prices.loc[d:end]
        rv = float(np.std(np.diff(np.log(win.values)), ddof=1) * math.sqrt(365))
        ivs.append(iv); rvs.append(rv)
    n = len(hits)
    if n == 0:
        return {"n": 0}
    return {"n": n, "cov": float(np.mean(hits)), "below": below / n,
            "above": above / n, "width_pct": float(np.mean(widths)),
            "iv_mean": float(np.mean(ivs)), "rv_mean": float(np.mean(rvs)),
            "start": min(terms).date(), "end": max(terms).date()}


# -------------------------------------------------------------- report
def report_asset(name: str, prices: pd.Series):
    print(f"\n{'=' * 84}\n{name}\n{'=' * 84}")
    if prices.empty:
        print("  NO DATA")
        return
    print(f"  {len(prices)} days, {prices.index[0].date()} … "
          f"{prices.index[-1].date()}; forecasts start after "
          f"{WARMUP}-day warm-up")

    for h in HORIZONS:
        print(f"\n  HORIZON {h}d — coverage (all days) | non-overlapping test "
              f"| width | breaches below/above")
        print(f"  {'method':<11}{'level':>6}{'cov':>7}{'n_no':>6}{'cov_no':>8}"
              f"{'kupiec_p':>10}{'width%':>8}{'below':>7}{'above':>7}")
        for m in METHODS:
            for lv in LEVELS:
                e = evaluate(prices, m, h, lv)
                if not e.get("n_all"):
                    continue
                flag = ""
                if (m, h, lv) == (PRIMARY["method"], PRIMARY["h"],
                                  PRIMARY["level"]):
                    flag = "  ◀ PRIMARY"
                elif e["kupiec_p"] < 0.05:
                    flag = "  ✗ miscalibrated"
                print(f"  {m:<11}{lv:>6.0%}{e['cov_all']:>7.1%}{e['n_no']:>6}"
                      f"{e['cov_no']:>8.1%}{e['kupiec_p']:>10.3f}"
                      f"{e['width_pct']:>8.1f}{e['below_all']:>7.1%}"
                      f"{e['above_all']:>7.1%}{flag}")
            print()

    print(f"  CONDITIONAL coverage at 90%, 14d, by current-vol tercile "
          f"(H3: raw_emp should fail in storms):")
    print(f"  {'method':<11}{'calm':>8}{'mid':>8}{'storm':>8}{'path touch':>13}")
    for m in METHODS:
        e = evaluate(prices, m, 14, 0.90)
        if e.get("n_all"):
            c = e["cond"]
            print(f"  {m:<11}{c['calm']:>8.1%}{c['mid']:>8.1%}"
                  f"{c['storm']:>8.1%}{e['touch']:>13.1%}")
    print("  (path touch = price touched either band edge at ANY point "
          "within 14d;\n   always above the terminal breach rate — about "
          "1.5-2x with daily closes)")

    print(f"\n  CURRENT CORRIDOR from {prices.index[-1].date()} close "
          f"${prices.iloc[-1]:,.2f}:")
    for h in HORIZONS:
        for lv in (0.80, 0.95):
            parts = []
            for m in ("gauss_ewma", "fhs"):
                b = current_band(prices, m, h, lv)
                if b:
                    parts.append(f"{m} ${b[0]:,.0f}–${b[1]:,.0f}")
            print(f"    {h:>2}d {lv:.0%}:  " + "   ".join(parts))


def report_implied(prices: pd.Series):
    print(f"\n{'=' * 84}\nETH IMPLIED VOLATILITY (Deribit ATM)  "
          f"— SHORT SAMPLE, DESCRIPTIVE ONLY\n{'=' * 84}")
    surf = load_iv_surface()
    terms = atm_term_structure(surf)
    if not terms:
        print("  no options data found")
        return
    for h in HORIZONS:
        for lv in (0.80, 0.90, 0.95):
            e = evaluate_implied(prices, terms, h, lv)
            if not e.get("n"):
                continue
            print(f"  {h:>2}d {lv:.0%}: coverage {e['cov']:.1%} "
                  f"(n={e['n']} overlapping, ~{e['n'] // h} independent)  "
                  f"width {e['width_pct']:.1f}%  "
                  f"below {e['below']:.1%} above {e['above']:.1%}")
    e = evaluate_implied(prices, terms, 14, 0.90)
    if e.get("n"):
        ratio = e["iv_mean"] / e["rv_mean"] if e["rv_mean"] else float("nan")
        print(f"\n  H4 — mean 14d implied vol {e['iv_mean']:.1%} vs subsequently "
              f"realised {e['rv_mean']:.1%}  (ratio {ratio:.2f}; >1 = "
              f"market overpays for protection)")
        print(f"  sample {e['start']} … {e['end']}")
    last_d = max(terms)
    if last_d in prices.index:
        spot = prices.loc[last_d]
        for h in HORIZONS:
            iv = implied_vol_at(terms[last_d], h)
            for lv in (0.80, 0.95):
                w = ND.inv_cdf((1 + lv) / 2) * iv * math.sqrt(h / 365)
                print(f"    implied {h:>2}d {lv:.0%} from {last_d.date()}: "
                      f"${spot * math.exp(-w):,.0f}–${spot * math.exp(w):,.0f}"
                      f"   (ATM IV {iv:.1%})")


# ------------------------------------------------- move sizes by direction
# "If it falls, how far? If it rises, how far?" — the FHS distribution split
# by sign. This does NOT forecast direction (ten candidates failed at that);
# it gives conditional sizes. Two views:
#   terminal  where price ENDS after h days, split into falls and rises
#   path      how far it TRAVELS inside h days: deepest dip and highest run
#             (both usually happen in the same window), on daily closes
# Every level is checked out of sample (move_backtest) overall and in the
# current vol regime, because the corridor backtest found calm regimes
# under-cover.
MOVE_HORIZONS = (1, 3, 7, 14)
MOVE_Q = (0.50, 0.80, 0.95)            # typical, 1-in-5, 1-in-20
KINDS = ("fall", "rise", "dip", "run")


def excursions(p: np.ndarray, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Deepest dip and highest run (log, >= 0) over closes s+1..s+h relative
    to the close at s. NaN where s+h is beyond the data."""
    n = len(p)
    dip = np.full(n, np.nan)
    run = np.full(n, np.nan)
    if n > h:
        lp = np.log(p)
        rel = (np.lib.stride_tricks.sliding_window_view(lp[1:], h)
               - lp[:n - h, None])
        dip[:n - h] = np.maximum(0.0, -rel.min(axis=1))
        run[:n - h] = np.maximum(0.0, rel.max(axis=1))
    return dip, run


def _move_arrays(p: np.ndarray, h: int):
    """h-day quantities standardised by EWMA vol at the window start."""
    sig = ewma_vol(log_returns(p))
    scale = sig * math.sqrt(h)
    dip, run = excursions(p, h)
    return sig, h_returns(p, h) / scale, dip / scale, run / scale


def _move_levels(Z, D, U, hist_end: int) -> dict:
    """Standardised size quantiles from windows completed by t (start
    s <= t-h, i.e. indices < hist_end). No lookahead."""
    z = Z[:hist_end]; z = z[np.isfinite(z)]
    d = D[:hist_end]; d = d[np.isfinite(d)]
    u = U[:hist_end]; u = u[np.isfinite(u)]
    nan = np.full(len(MOVE_Q), np.nan)
    q = lambda a: np.quantile(a, MOVE_Q) if len(a) else nan
    return {"n": len(z),
            "p_up": float(np.mean(z > 0)) if len(z) else float("nan"),
            "fall": q(-z[z < 0]), "rise": q(z[z > 0]),
            "dip": q(d), "run": q(u)}


def move_profile(prices: pd.Series, h: int) -> dict | None:
    """Move sizes in each direction as of the last close, in fractions
    (fall/dip negative, rise/run positive). p_up is the historical share of
    h-day windows that ended up — a base rate, NOT a prediction."""
    p = prices.values.astype(float)
    sig, Z, D, U = _move_arrays(p, h)
    t = len(p) - 1
    hist_end = t - h + 1
    if hist_end < WARMUP or not np.isfinite(sig[t]):
        return None
    m = _move_levels(Z, D, U, hist_end)
    if m["n"] < WARMUP:
        return None
    s = sig[t] * math.sqrt(h)
    hist = sig[np.isfinite(sig)]
    return {"last": float(p[-1]), "date": prices.index[-1].date(), "h": h,
            "p_up": m["p_up"],
            "fall": np.exp(-m["fall"] * s) - 1, "rise": np.exp(m["rise"] * s) - 1,
            "dip": np.exp(-m["dip"] * s) - 1, "run": np.exp(m["run"] * s) - 1,
            "sig_ann": float(sig[t] * math.sqrt(365)),
            "sig_pct": float(np.mean(hist < sig[t]))}


def regime_of(pct: float) -> str:
    return "calm" if pct < 1 / 3 else ("storm" if pct > 2 / 3 else "mid")


def move_backtest(prices: pd.Series, h: int) -> dict:
    """Out-of-sample check: how often did the realised size exceed the
    1-in-5 and 1-in-20 levels forecast at t (expanding window, every day)?
    Falls are judged only against fall levels, rises against rise levels.
    Targets 20% and 5%. Windows overlap, so rates are descriptive; split by
    the vol tercile at t (an evaluation split, as in evaluate())."""
    p = prices.values.astype(float)
    sig, Z, D, U = _move_arrays(p, h)
    hist = sig[np.isfinite(sig)]
    if len(hist) == 0:
        return {}
    terc = np.quantile(hist, [1 / 3, 2 / 3])
    rows = []                                   # (regime, kind, >1in5, >1in20)
    for t in range(len(p) - h):
        hist_end = t - h + 1
        if hist_end < WARMUP or not (np.isfinite(sig[t]) and np.isfinite(Z[t])):
            continue
        m = _move_levels(Z, D, U, hist_end)
        if m["n"] < WARMUP:
            continue
        g = 0 if sig[t] <= terc[0] else (1 if sig[t] <= terc[1] else 2)
        if Z[t] < 0:
            rows.append((g, 0, -Z[t] > m["fall"][1], -Z[t] > m["fall"][2]))
        elif Z[t] > 0:
            rows.append((g, 1, Z[t] > m["rise"][1], Z[t] > m["rise"][2]))
        rows.append((g, 2, D[t] > m["dip"][1], D[t] > m["dip"][2]))
        rows.append((g, 3, U[t] > m["run"][1], U[t] > m["run"][2]))
    if not rows:
        return {}
    a = np.array(rows, dtype=float)
    out = {"start": prices.index[WARMUP + h - 1].date()}
    for k, kind in enumerate(KINDS):
        sel = a[:, 1] == k
        out[kind] = {"all": (a[sel, 2].mean(), a[sel, 3].mean(), int(sel.sum()))}
        for g, name in enumerate(("calm", "mid", "storm")):
            s2 = sel & (a[:, 0] == g)
            out[kind][name] = ((a[s2, 2].mean(), a[s2, 3].mean(), int(s2.sum()))
                               if s2.any() else (float("nan"),) * 2 + (0,))
    return out


def report_moves(name: str, prices: pd.Series):
    print(f"\n{'=' * 84}\n{name}\n{'=' * 84}")
    if prices.empty:
        print("  NO DATA")
        return
    prof = {h: move_profile(prices, h) for h in MOVE_HORIZONS}
    p0 = next((v for v in prof.values() if v), None)
    if p0 is None:
        print("  not enough history")
        return
    last, d = p0["last"], p0["date"]
    age = (pd.Timestamp.today().normalize() - pd.Timestamp(d)).days
    stale = f"   WARNING: {age} days old" if age > 3 else ""
    reg = regime_of(p0["sig_pct"])
    print(f"  latest price ${last:,.2f} on {d}{stale}")
    print(f"  volatility now {p0['sig_ann']:.0%} annualised, "
          f"{p0['sig_pct']:.0%} percentile of its history -> "
          f"{reg.upper()} regime")

    def cells(v, sign):
        return "".join(f"{sign + format(abs(x), '.1%'):>9}" for x in v)

    def prices_(v):
        return "".join(f"{'$' + format(last * (1 + x), ',.0f'):>9}" for x in v)

    print("\n  WHERE IT ENDS after h days, split by direction.  "
          "'1-in-5' = of the times it\n  went that way, 1 in 5 went "
          "further than this.  up-share = history, NOT a forecast.")
    print(f"  {'':<5}{'up-share':>9}   {'IF IT FALLS':<27}   {'IF IT RISES':<27}")
    print(f"  {'':<5}{'':>9}   {'typical':>9}{'1-in-5':>9}{'1-in-20':>9}   "
          f"{'typical':>9}{'1-in-5':>9}{'1-in-20':>9}")
    for h, m in prof.items():
        if not m:
            continue
        print(f"  {str(h) + 'd':<5}{m['p_up']:>9.0%}   {cells(m['fall'], '-')}   "
              f"{cells(m['rise'], '+')}")
        print(f"  {'':<5}{'':>9}   {prices_(m['fall'])}   {prices_(m['rise'])}")

    print("\n  HOW FAR IT TRAVELS inside h days: deepest dip and highest run "
          "on daily closes\n  (intraday wicks go further). Both usually "
          "happen in the same window. These are\n  over ALL windows, "
          "including ones that ended the other way, so 'typical' can be\n"
          "  smaller than the typical fall/rise above.")
    print(f"  {'':<5}{'':>9}   {'DEEPEST DIP':<27}   {'HIGHEST RUN':<27}")
    print(f"  {'':<5}{'':>9}   {'typical':>9}{'1-in-5':>9}{'1-in-20':>9}   "
          f"{'typical':>9}{'1-in-5':>9}{'1-in-20':>9}")
    for h, m in prof.items():
        if not m or h == 1:                     # 1d path == 1d terminal
            continue
        print(f"  {str(h) + 'd':<5}{'':>9}   {cells(m['dip'], '-')}   "
              f"{cells(m['run'], '+')}")
        print(f"  {'':<5}{'':>9}   {prices_(m['dip'])}   {prices_(m['run'])}")

    bts = {h: move_backtest(prices, h) for h in MOVE_HORIZONS}
    start = next((b["start"] for b in bts.values() if b), None)
    print(f"\n  BACKTEST: % of cases exceeding the 1-in-5 / 1-in-20 level "
          f"(targets 20 / 5)\n  expanding window, every day since {start}, "
          f"overlapping windows — descriptive")
    hdr = "".join(f"{k:>9}" for k in KINDS)
    print(f"  {'':<5}{'ALL REGIMES':<36}   {reg.upper() + ' REGIMES (= today)':<36}")
    print(f"  {'':<5}{hdr}   {hdr}")
    fmt = lambda c: (f"{f'{c[0] * 100:.0f}/{c[1] * 100:.0f}':>9}" if c[2]
                     else f"{'n/a':>9}")
    for h, b in bts.items():
        if not b:
            continue
        left = "".join(fmt(b[k]["all"]) for k in KINDS)
        right = "".join(fmt(b[k][reg]) for k in KINDS)
        print(f"  {str(h) + 'd':<5}{left}   {right}")
    if reg == "calm":
        print("  Today is CALM. If the right-hand rates sit above 20/5, the "
              "sizes above are\n  UNDERSTATED for today: vol tends to rise "
              "out of calm spells faster than EWMA expects.")
    elif reg == "storm":
        print("  Today is STORMY. If the right-hand rates sit below 20/5, the "
              "sizes above are\n  OVERSTATED for today: storms tend to fade "
              "faster than EWMA expects.")


# ----------------------------------------------------------- self-test
def _garch_sim(n, innov, seed):
    rng = np.random.default_rng(seed)
    w, a, b = 1e-6, 0.08, 0.90
    v = w / (1 - a - b)
    r = np.empty(n)
    for i in range(n):
        e = innov(rng)
        r[i] = math.sqrt(v) * e
        v = w + a * r[i] ** 2 + b * v
    p = 100 * np.exp(np.cumsum(r))
    return pd.Series(p, index=pd.date_range("2018-01-01", periods=n))


def self_test() -> int:
    # 1. Kupiec sanity.
    assert abs(kupiec(100, 10, 0.10) - 1.0) < 1e-9
    assert kupiec(100, 25, 0.10) < 0.001
    print("  [ok] Kupiec: exact match p=1.0; 25/100 breaches at 10% → p<0.001")

    # 2. No lookahead: altering prices after t0 must not change bands <= t0.
    s = _garch_sim(1200, lambda g: g.standard_normal(), 1)
    p1 = s.values.copy(); p2 = p1.copy(); t0 = 900
    p2[t0 + 1:] *= 3.0
    for p in (p1, p2):
        pass
    def bands_at(p, t):
        r = log_returns(p); sig = ewma_vol(r)
        R = h_returns(p, 14); Z = R / (sig * math.sqrt(14))
        return [band(m, t, 14, 0.9, sig, R, Z) for m in METHODS]
    for t in (t0 - 20, t0):
        assert bands_at(p1, t) == bands_at(p2, t), "band leaked future data"
    print("  [ok] bands at t unchanged when prices after t are altered")

    # 3. Gaussian GARCH: gauss_ewma and fhs both close to nominal at 90%.
    g = _garch_sim(3000, lambda r: r.standard_normal(), 2)
    for m in ("gauss_ewma", "fhs"):
        e = evaluate(g, m, 1, 0.90)
        assert abs(e["cov_all"] - 0.90) < 0.03, (m, e["cov_all"])
    print(f"  [ok] Gaussian GARCH: gauss_ewma and fhs both ~90% at 1d")

    # 4. Fat tails (Student-t, 3 df): gauss under-covers at 99%, fhs closer.
    tnu = 3
    tsim = _garch_sim(3000, lambda r: r.standard_t(tnu) / math.sqrt(tnu / (tnu - 2)), 3)
    eg = evaluate(tsim, "gauss_ewma", 1, 0.99)
    ef = evaluate(tsim, "fhs", 1, 0.99)
    print(f"  fat tails @99%, 1d: gauss_ewma {eg['cov_all']:.1%}, "
          f"fhs {ef['cov_all']:.1%}")
    assert eg["cov_all"] < 0.985, "gauss should under-cover fat tails at 99%"
    assert abs(ef["cov_all"] - 0.99) < abs(eg["cov_all"] - 0.99)
    print("  [ok] gauss under-covers fat tails at 99%; fhs is closer")

    # 5. Implied: units detected, tenor interpolated in total variance.
    iv = normalise_iv_units(pd.Series([55.0, 60.0, 48.0]))
    assert iv.max() < 1
    ts = [(7, 0.60), (30, 0.50)]
    v14 = implied_vol_at(ts, 14)
    assert 0.50 < v14 < 0.60
    print(f"  [ok] IV percent→decimal; 14d interpolated {v14:.3f} between 7d/30d")

    # 6. Excursions by hand: 100 -> 90 -> 120 -> 110, h=2.
    dip, run = excursions(np.array([100.0, 90.0, 120.0, 110.0]), 2)
    assert abs(dip[0] - math.log(100 / 90)) < 1e-12
    assert abs(run[0] - math.log(120 / 100)) < 1e-12
    assert dip[1] == 0.0 and abs(run[1] - math.log(120 / 90)) < 1e-12
    assert np.isnan(dip[2]) and np.isnan(run[3])
    # path always at least as far as the terminal move, same direction
    p = g.values
    D, U = excursions(p, 7)
    R7 = h_returns(p, 7)
    ok = np.isfinite(R7)
    assert np.all(D[ok] >= np.maximum(0, -R7[ok]) - 1e-12)
    assert np.all(U[ok] >= np.maximum(0, R7[ok]) - 1e-12)
    print("  [ok] dip/run match a hand example; path >= terminal move")

    # 7. Symmetric GARCH: up-share ~50%, falls ~ rises, levels monotone,
    #    sizes scale with current vol, no lookahead.
    m = move_profile(g, 14)
    assert 0.40 < m["p_up"] < 0.60, m["p_up"]
    ratio = m["rise"][0] / -m["fall"][0]
    assert 0.7 < ratio < 1.4, ratio
    for k in ("fall", "dip"):
        assert m[k][0] > m[k][1] > m[k][2], (k, m[k])      # negatives
    for k in ("rise", "run"):
        assert m[k][0] < m[k][1] < m[k][2], (k, m[k])
    assert abs(m["dip"][2]) >= abs(m["fall"][2]) * 0.8      # path reaches far
    rng = np.random.default_rng(9)
    tail = g.values[-1] * np.exp(np.cumsum(rng.standard_normal(30) * 0.05))
    g2 = pd.Series(np.r_[g.values, tail],
                   index=pd.date_range(g.index[0], periods=len(g) + 30))
    m2 = move_profile(g2, 14)
    assert abs(m2["fall"][0]) > abs(m["fall"][0]) * 1.5
    assert m2["sig_pct"] > 0.9 and regime_of(m2["sig_pct"]) == "storm"
    # no lookahead: levels at t0 unchanged when prices after t0 are altered
    t0, h = 2000, 14
    pa = g.values.copy(); pb = pa.copy(); pb[t0 + 1:] *= 2.5
    la = _move_levels(*_move_arrays(pa, h)[1:], t0 - h + 1)
    lb = _move_levels(*_move_arrays(pb, h)[1:], t0 - h + 1)
    assert all(np.array_equal(la[k], lb[k]) for k in KINDS), "move levels leaked"
    print(f"  [ok] symmetric GARCH: up-share {m['p_up']:.0%}, rise/fall "
          f"{ratio:.2f}; levels monotone; storm tail widens sizes")

    # 8. Out-of-sample calibration on stationary GARCH: ~20% / ~5%.
    b = move_backtest(g, 7)
    for k in KINDS:
        r5, r20, n = b[k]["all"]
        assert abs(r5 - 0.20) < 0.05 and abs(r20 - 0.05) < 0.03, (k, r5, r20)
    print("  [ok] backtest on GARCH: " + ", ".join(
        f"{k} {b[k]['all'][0]:.0%}/{b[k]['all'][1]:.0%}" for k in KINDS)
        + "  (targets 20%/5%)")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Volatility corridor coverage test")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--long", action="store_true",
                    help="Use taker_flow spot closes (2017+) instead of "
                         "price_snapshots (2020-11+). More regimes.")
    ap.add_argument("--moves", action="store_true",
                    help="Move sizes from the latest close: if it falls, how "
                         "far; if it rises, how far; deepest dip and highest "
                         "run inside the window; with an out-of-sample check.")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    src = "taker_flow spot (2017+)" if a.long else "price_snapshots (2020-11+)"
    print(f"Price source: {src}")
    if a.moves:
        for name, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
            report_moves(f"{name}  ({sym})", load_closes(sym, a.long))
        print("\nSizes, not direction. A sizing input (stops, position size, "
              "liquidation distance),\nnot a trading signal. 'typical' = "
              "median; levels are FHS on EWMA vol, 365-day warm-up.")
        return 0
    eth = None
    for name, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        px = load_closes(sym, a.long)
        report_asset(f"{name}  ({sym})", px)
        if name == "ETH":
            eth = px
    if eth is not None and not eth.empty:
        report_implied(eth)
    print("\nRead PRIMARY first (fhs, 14d, 90%). A method is CALIBRATED only "
          "if Kupiec p > 0.05\nat 80/90/95% on BOTH assets. A corridor "
          "describes how far price may move — not which way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
