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
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Volatility corridor coverage test")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--long", action="store_true",
                    help="Use taker_flow spot closes (2017+) instead of "
                         "price_snapshots (2020-11+). More regimes.")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    src = "taker_flow spot (2017+)" if a.long else "price_snapshots (2020-11+)"
    print(f"Price source: {src}")
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
