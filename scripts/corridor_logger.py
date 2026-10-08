"""
Corridor logger — forward test of the v1, v3 and c1 corridors.

WHY
---
Every corridor result (docs/FINDINGS_vol_corridor.md) rests on a few dozen
independent windows from 2018-2026, a period now examined many times. The
only clean evidence left is data that does not exist yet. This service
writes each day's forecasts BEFORE the outcome is known, never rewrites
them, and scores each one when its window closes.

WHAT IT DOES (each cycle; idempotent, so it can run hourly)
--------------------------------------------------------
1. Daily closes for the 11 universe coins into `corridor_closes`
   (seeded once from taker_flow's archive closes, then topped up from the
   Binance public REST API; completed UTC days only).
2. DVOL for BTC/ETH refreshed into `dvol_daily` (collectors.dvol_backfill).
3. Forecasts for every completed day not yet logged (catch-up up to 7 days
   if the container was down), for h = 7 and 14 days:
       v1  pooled FHS, EWMA vol                      — all 11 coins
       c1  regime-conditional FHS (adopted, 9 alts)  — all 11 coins
       v3  FHS scaled by DVOL (adopted, BTC/ETH)     — BTC, ETH
   with 80/90/95% bands, 1-in-5 / 1-in-20 dip and run, the REAL-TIME
   regime label (as --moves shows it), EWMA vol, and DVOL/EWMA ratio.
   Written once (ON CONFLICT DO NOTHING): a forecast is never revised.
4. Scores every forecast whose window has closed into `corridor_outcomes`.

The models are the TESTED ones, not re-implementations of the idea: the
self-test checks that the levels here equal those produced by
test_vol_corridor_cfhs (v1, c1) and test_vol_corridor_iv (v3) for the same
day, to floating-point precision.

TABLES
------
corridor_closes    (symbol, d, close, source)
corridor_forecasts (forecast_date, symbol, model, h) -> close, lo/hi 80/90/95
                   (log returns), dip5/dip20/run5/run20 (positive log sizes),
                   pool_n, sig_ann, regime, regime_pct, iv_ann, iv_ratio,
                   code_version, created_at
corridor_outcomes  (forecast_date, symbol, model, h) -> end_date, ret, dip,
                   run, hit80/90/95, dip_gt5/20, run_gt5/20, scored_at

USAGE
-----
    python -m scripts.corridor_logger --self-test   # no DB, no network
    python -m scripts.corridor_logger --once        # one cycle
    python -m scripts.corridor_logger --loop        # the container
    python -m scripts.corridor_logger --report      # forward results so far
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.test_vol_corridor import (WARMUP, ewma_vol, excursions,  # noqa: E402
                                       h_returns, log_returns)
from scripts.test_vol_corridor_cfhs import MIN_POOL, realtime_labels  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("corridor_logger")

CODE_VERSION = "2026-10-08"
HORIZONS = (7, 14)
LEVELS = (0.80, 0.90, 0.95)
QS = [(1 - lv) / 2 for lv in LEVELS] + [(1 + lv) / 2 for lv in LEVELS]
IV_COINS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
CATCHUP_DAYS = 7
H12_RATIO = 1.2       # forward H12 split, fixed now (backtest medians 1.19-1.26)
REGIME_NAMES = {0: "calm", 1: "mid", 2: "storm"}
REST_BASES = ("https://api.binance.com", "https://data-api.binance.vision")
DAY_MS = 86_400_000

DDL = """
CREATE TABLE IF NOT EXISTS corridor_closes (
    symbol TEXT NOT NULL, d DATE NOT NULL, close DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, d));
CREATE TABLE IF NOT EXISTS corridor_forecasts (
    forecast_date DATE NOT NULL, symbol TEXT NOT NULL, model TEXT NOT NULL,
    h INT NOT NULL, close DOUBLE PRECISION NOT NULL,
    lo80 DOUBLE PRECISION, hi80 DOUBLE PRECISION,
    lo90 DOUBLE PRECISION, hi90 DOUBLE PRECISION,
    lo95 DOUBLE PRECISION, hi95 DOUBLE PRECISION,
    dip5 DOUBLE PRECISION, dip20 DOUBLE PRECISION,
    run5 DOUBLE PRECISION, run20 DOUBLE PRECISION,
    pool_n INT, sig_ann DOUBLE PRECISION, regime TEXT,
    regime_pct DOUBLE PRECISION, iv_ann DOUBLE PRECISION,
    iv_ratio DOUBLE PRECISION, code_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (forecast_date, symbol, model, h));
CREATE TABLE IF NOT EXISTS corridor_outcomes (
    forecast_date DATE NOT NULL, symbol TEXT NOT NULL, model TEXT NOT NULL,
    h INT NOT NULL, end_date DATE NOT NULL, ret DOUBLE PRECISION,
    dip DOUBLE PRECISION, run DOUBLE PRECISION,
    hit80 BOOLEAN, hit90 BOOLEAN, hit95 BOOLEAN,
    dip_gt5 BOOLEAN, dip_gt20 BOOLEAN, run_gt5 BOOLEAN, run_gt20 BOOLEAN,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (forecast_date, symbol, model, h));
"""

FC_COLS = ("forecast_date", "symbol", "model", "h", "close", "lo80", "hi80",
           "lo90", "hi90", "lo95", "hi95", "dip5", "dip20", "run5", "run20",
           "pool_n", "sig_ann", "regime", "regime_pct", "iv_ann", "iv_ratio",
           "code_version")


def coins() -> list[str]:
    from config.universe import TRADEABLE_UNIVERSE
    return list(TRADEABLE_UNIVERSE)


# ------------------------------------------------------------- forecasting
def _levels(Z, D, U, pool, sh_t):
    """FHS levels from standardised windows at indices `pool`."""
    q = np.quantile(Z[pool], QS) * sh_t
    k = len(LEVELS)
    dip = np.quantile(D[pool], [0.8, 0.95]) * sh_t
    run = np.quantile(U[pool], [0.8, 0.95]) * sh_t
    return {"lo": q[:k], "hi": q[k:], "dip": dip, "run": run, "n": len(pool)}


def forecast_day(p: np.ndarray, t: int, iv_ann: np.ndarray | None = None
                 ) -> list[dict]:
    """All model forecasts at close of index t, using p[:t+1] (and iv_ann[:t+1])
    only. Returns one dict per (model, h) that has enough history."""
    p = np.asarray(p[:t + 1], float)
    r = log_returns(p)
    sig = ewma_vol(r)
    if not np.isfinite(sig[t]):
        return []
    lab = realtime_labels(sig)
    hist = sig[np.isfinite(sig)]
    pct = float(np.mean(hist < sig[t]))
    ctx = {"close": float(p[t]), "sig_ann": float(sig[t] * math.sqrt(365)),
           "regime": REGIME_NAMES.get(int(lab[t])), "regime_pct": pct,
           "iv_ann": None, "iv_ratio": None}
    iv = None
    if iv_ann is not None:
        iv = np.asarray(iv_ann[:t + 1], float)
        if np.isfinite(iv[t]):
            ctx["iv_ann"] = float(iv[t])
            ctx["iv_ratio"] = float(iv[t] / math.sqrt(365) / sig[t])
    out = []
    for h in HORIZONS:
        R = h_returns(p, h)
        D, U = excursions(p, h)
        end = t - h + 1
        sh1 = sig * math.sqrt(h)
        ps = int(np.flatnonzero(np.isfinite(sh1))[0])
        Z1, D1, U1 = R / sh1, D / sh1, U / sh1
        base = np.arange(ps, max(ps, end))
        base = base[np.isfinite(Z1[base])]
        # v1: all past windows
        if len(base) >= WARMUP:
            out.append({"model": "v1", "h": h, **ctx,
                        **_levels(Z1, D1, U1, base, sh1[t])})
        # c1: past windows that started in the same real-time regime
        if lab[t] >= 0:
            pool = base[lab[base] == lab[t]]
            if len(pool) >= MIN_POOL:
                out.append({"model": "c1", "h": h, **ctx,
                            **_levels(Z1, D1, U1, pool, sh1[t])})
        # v3: DVOL scale; pool from the first day both scales exist
        if iv is not None and np.isfinite(iv[t]):
            sh3 = iv * math.sqrt(h / 365.0)
            f3 = np.flatnonzero(np.isfinite(sh3))
            if len(f3):
                ps3 = max(ps, int(f3[0]))
                Z3, D3, U3 = R / sh3, D / sh3, U / sh3
                pool = np.arange(ps3, max(ps3, end))
                pool = pool[np.isfinite(Z3[pool])]
                if len(pool) >= WARMUP:
                    out.append({"model": "v3", "h": h, **ctx,
                                **_levels(Z3, D3, U3, pool, sh3[t])})
    return out


def to_row(fc: dict, d: date, symbol: str) -> tuple:
    lo, hi, dip, run = fc["lo"], fc["hi"], fc["dip"], fc["run"]
    vals = {"forecast_date": d, "symbol": symbol, "model": fc["model"],
            "h": fc["h"], "close": fc["close"],
            "lo80": lo[0], "hi80": hi[0], "lo90": lo[1], "hi90": hi[1],
            "lo95": lo[2], "hi95": hi[2], "dip5": dip[0], "dip20": dip[1],
            "run5": run[0], "run20": run[1], "pool_n": fc["n"],
            "sig_ann": fc["sig_ann"], "regime": fc["regime"],
            "regime_pct": fc["regime_pct"], "iv_ann": fc["iv_ann"],
            "iv_ratio": fc["iv_ratio"], "code_version": CODE_VERSION}
    return tuple(_py(vals[c]) for c in FC_COLS)


def _py(x):
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    return x


def score(closes: pd.Series, fc: dict) -> dict | None:
    """Outcome of one logged forecast, or None if the window is still open.
    closes: daily series (DatetimeIndex) for the symbol."""
    d0 = pd.Timestamp(fc["forecast_date"])
    end = d0 + pd.Timedelta(days=int(fc["h"]))
    if end > closes.index[-1] or d0 not in closes.index:
        return None
    path = closes[(closes.index > d0) & (closes.index <= end)]
    if len(path) < int(fc["h"]) or path.index[-1] != end:
        return None
    rel = np.log(path.values / closes.loc[d0])
    ret = float(rel[-1])
    dip = float(max(0.0, -rel.min()))
    run = float(max(0.0, rel.max()))
    g = lambda k: float(fc[k])
    return {"end_date": end.date(), "ret": ret, "dip": dip, "run": run,
            "hit80": g("lo80") <= ret <= g("hi80"),
            "hit90": g("lo90") <= ret <= g("hi90"),
            "hit95": g("lo95") <= ret <= g("hi95"),
            "dip_gt5": dip > g("dip5"), "dip_gt20": dip > g("dip20"),
            "run_gt5": run > g("run5"), "run_gt20": run > g("run20")}


# --------------------------------------------------------------- database
def _rows(cur):
    rows = cur.fetchall()
    if rows and not isinstance(rows[0], dict):
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in rows]
    return rows


def ensure_tables(conn):
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()


def load_series(conn, symbol: str) -> pd.Series:
    cur = conn.cursor()
    cur.execute("SELECT d, close FROM corridor_closes WHERE symbol = %s "
                "ORDER BY d", (symbol,))
    rows = _rows(cur)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([r["close"] for r in rows],
                  index=pd.to_datetime([r["d"] for r in rows]), dtype=float)
    s = s[s > 0]
    full = pd.date_range(s.index[0], s.index[-1], freq="D")
    return s.reindex(full).ffill(limit=3).dropna()     # as load_closes()


def load_dvol(conn, currency: str) -> pd.Series:
    cur = conn.cursor()
    cur.execute("SELECT d, close FROM dvol_daily WHERE currency = %s ORDER BY d",
                (currency,))
    rows = _rows(cur)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([r["close"] for r in rows],
                  index=pd.to_datetime([r["d"] for r in rows]), dtype=float)
    return s / 100.0 if s.median() > 1.5 else s


def seed_from_archive(conn, symbol: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM corridor_closes WHERE symbol = %s",
                (symbol,))
    n = _rows(cur)[0]["n"]
    if n:
        return 0
    cur.execute("""INSERT INTO corridor_closes (symbol, d, close, source)
                   SELECT symbol, d, close, 'archive' FROM taker_flow
                   WHERE market = 'spot' AND symbol = %s AND close > 0
                   ON CONFLICT DO NOTHING""", (symbol,))
    conn.commit()
    return cur.rowcount


def fetch_klines(symbol: str, start_ms: int) -> list[tuple]:
    """Completed daily candles from start_ms: [(date, close)]."""
    import requests
    now_ms = int(time.time() * 1000)
    out, errors = [], []
    for base in REST_BASES:
        try:
            cur_ms = start_ms
            out = []
            while cur_ms < now_ms:
                resp = requests.get(base + "/api/v3/klines", timeout=30, params={
                    "symbol": symbol, "interval": "1d", "startTime": cur_ms,
                    "limit": 1000})
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                for k in data:
                    if int(k[6]) < now_ms:                     # closed candle
                        d = datetime.fromtimestamp(int(k[0]) / 1000,
                                                   tz=timezone.utc).date()
                        out.append((d, float(k[4])))
                cur_ms = int(data[-1][0]) + DAY_MS
                if len(data) < 1000:
                    break
                time.sleep(0.2)
            return out
        except Exception as e:                                # noqa: BLE001
            errors.append(f"{base}: {e}")
    raise RuntimeError("klines failed: " + " | ".join(errors))


def update_closes(conn, symbol: str) -> int:
    seeded = seed_from_archive(conn, symbol)
    cur = conn.cursor()
    cur.execute("SELECT max(d) AS m FROM corridor_closes WHERE symbol = %s",
                (symbol,))
    last = _rows(cur)[0]["m"]
    start = (last - timedelta(days=3)) if last else date(2017, 8, 1)
    start_ms = int(datetime(start.year, start.month, start.day,
                            tzinfo=timezone.utc).timestamp() * 1000)
    rows = fetch_klines(symbol, start_ms)
    from psycopg2.extras import execute_values
    if rows:
        execute_values(cur, """INSERT INTO corridor_closes (symbol, d, close, source)
                               VALUES %s ON CONFLICT DO NOTHING""",
                       [(symbol, d, c, "rest") for d, c in rows])
    conn.commit()
    return seeded + max(cur.rowcount, 0)


def update_dvol():
    try:
        from collectors import dvol_backfill as dv
        now_ms = int(time.time() * 1000)
        for cur in IV_COINS.values():
            dv.run(cur, now_ms - 10 * DAY_MS, now_ms, write=True)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("DVOL refresh failed (v3 forecasts may lag): %s", e)


def logged_dates(conn, symbol: str, model: str, h: int) -> set:
    cur = conn.cursor()
    cur.execute("""SELECT forecast_date FROM corridor_forecasts
                   WHERE symbol = %s AND model = %s AND h = %s
                     AND forecast_date >= %s""",
                (symbol, model, h, date.today() - timedelta(days=CATCHUP_DAYS + 2)))
    return {r["forecast_date"] for r in _rows(cur)}


def first_logged(conn, symbol: str) -> date | None:
    cur = conn.cursor()
    cur.execute("SELECT min(forecast_date) AS m FROM corridor_forecasts "
                "WHERE symbol = %s", (symbol,))
    return _rows(cur)[0]["m"]


def models_for(symbol: str) -> tuple[str, ...]:
    return ("v1", "c1", "v3") if symbol in IV_COINS else ("v1", "c1")


def write_forecasts(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    execute_values(cur, f"""INSERT INTO corridor_forecasts ({', '.join(FC_COLS)})
                            VALUES %s ON CONFLICT DO NOTHING""", rows)
    conn.commit()
    return max(cur.rowcount, 0)


def forecast_symbol(conn, symbol: str) -> tuple[int, list[dict]]:
    px = load_series(conn, symbol)
    if len(px) < WARMUP + 60:
        logger.warning("%s: not enough closes (%d)", symbol, len(px))
        return 0, []
    iv_full = None
    if symbol in IV_COINS:
        dv = load_dvol(conn, IV_COINS[symbol])
        if not dv.empty:
            iv_full = dv.reindex(px.index).to_numpy()
    age = (date.today() - px.index[-1].date()).days
    if age > 2:
        logger.warning("%s: latest close is %s (%d days old) — REST top-up "
                       "failing?", symbol, px.index[-1].date(), age)
    done = {(m, h): logged_dates(conn, symbol, m, h)
            for m in models_for(symbol) for h in HORIZONS}
    # Forward only: the first cycle logs the latest completed day; catch-up
    # fills gaps AFTER the first logged day (container downtime), never
    # before it.
    first = first_logged(conn, symbol)
    n = len(px)
    t0 = n - 1 if first is None else max(
        n - CATCHUP_DAYS, int(np.searchsorted(px.index, pd.Timestamp(first))))
    rows, latest = [], []
    for t in range(max(0, t0), n):
        d = px.index[t].date()
        todo = [k for k, v in done.items() if d not in v]
        if not todo:
            continue
        fcs = forecast_day(px.values, t, iv_full)
        for fc in fcs:
            if (fc["model"], fc["h"]) in todo:
                rows.append(to_row(fc, d, symbol))
        if t == n - 1:
            latest = fcs
    return write_forecasts(conn, rows), latest


def score_open(conn) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT f.* FROM corridor_forecasts f
                   LEFT JOIN corridor_outcomes o USING (forecast_date, symbol, model, h)
                   WHERE o.forecast_date IS NULL
                     AND f.forecast_date + f.h <= CURRENT_DATE""")
    open_fc = _rows(cur)
    series, out = {}, []
    for fc in open_fc:
        s = series.setdefault(fc["symbol"], load_series(conn, fc["symbol"]))
        res = score(s, fc) if len(s) else None
        if res:
            out.append((fc["forecast_date"], fc["symbol"], fc["model"], fc["h"],
                        res["end_date"], res["ret"], res["dip"], res["run"],
                        res["hit80"], res["hit90"], res["hit95"],
                        res["dip_gt5"], res["dip_gt20"], res["run_gt5"],
                        res["run_gt20"]))
    if out:
        from psycopg2.extras import execute_values
        execute_values(cur, """INSERT INTO corridor_outcomes
            (forecast_date, symbol, model, h, end_date, ret, dip, run, hit80,
             hit90, hit95, dip_gt5, dip_gt20, run_gt5, run_gt20)
            VALUES %s ON CONFLICT DO NOTHING""", out)
    conn.commit()
    return len(out)


# --------------------------------------------------------------- the cycle
def run_once() -> None:
    from config.database import get_connection
    conn = get_connection()
    try:
        ensure_tables(conn)
        for sym in coins():
            try:
                update_closes(conn, sym)
            except Exception as e:                            # noqa: BLE001
                conn.rollback()
                logger.warning("%s: close update failed: %s", sym, e)
        update_dvol()
        written, summary = 0, []
        for sym in coins():
            try:
                w, latest = forecast_symbol(conn, sym)
                written += w
                if w and latest:
                    summary.append((sym, latest))
            except Exception as e:                            # noqa: BLE001
                conn.rollback()
                logger.exception("%s: forecast failed: %s", sym, e)
        scored = score_open(conn)
        logger.info("cycle done: %d forecast rows written, %d outcomes scored",
                    written, scored)
        for sym, latest in summary:
            _log_today(sym, latest)
    finally:
        conn.close()


def _log_today(sym: str, latest: list[dict]):
    """One line per coin: regime and the working corridor (14d)."""
    pick = {fc["model"]: fc for fc in latest if fc["h"] == 14}
    work = pick.get("v3") or pick.get("c1") or pick.get("v1")
    if not work:
        return
    c = work["close"]
    lo90, hi90 = c * math.exp(work["lo"][1]), c * math.exp(work["hi"][1])
    lo95, hi95 = c * math.exp(work["lo"][2]), c * math.exp(work["hi"][2])
    tip = "  (calm: use the 95% band)" if work["regime"] == "calm" else ""
    logger.info("%-9s %-5s %-5s 14d 90%% %s–%s  95%% %s–%s%s", sym,
                work["regime"] or "?", work["model"], f"{lo90:,.4g}",
                f"{hi90:,.4g}", f"{lo95:,.4g}", f"{hi95:,.4g}", tip)


def loop(interval: int) -> None:
    logger.info("corridor logger started (code %s, every %ds)", CODE_VERSION,
                interval)
    while True:
        try:
            run_once()
        except Exception as e:                                # noqa: BLE001
            logger.exception("cycle failed: %s", e)
        time.sleep(interval)


# ------------------------------------------------------------------ report
def report() -> int:
    from config.database import get_connection
    conn = get_connection()
    try:
        ensure_tables(conn)
        cur = conn.cursor()
        cur.execute("""SELECT f.forecast_date, f.symbol, f.model, f.h, f.regime,
                              f.iv_ratio, f.created_at, o.*
                       FROM corridor_forecasts f
                       JOIN corridor_outcomes o USING (forecast_date, symbol, model, h)""")
        df = pd.DataFrame(_rows(cur))
        cur.execute("SELECT count(*) AS n, min(forecast_date) AS a, "
                    "max(forecast_date) AS b FROM corridor_forecasts")
        meta = _rows(cur)[0]
    finally:
        conn.close()
    print(f"Logged forecasts: {meta['n']} rows, {meta['a']} … {meta['b']}")
    if df.empty:
        print("No windows have closed yet. 7-day windows close a week after "
              "the first forecast, 14-day after two.")
        return 0
    df = df.loc[:, ~df.columns.duplicated()]
    late = (pd.to_datetime(df["created_at"]).dt.tz_localize(None).dt.normalize()
            > pd.to_datetime(df["forecast_date"]) + pd.Timedelta(days=1))
    print(f"Scored: {len(df)} rows ({int(late.sum())} logged late via catch-up)")
    for h in HORIZONS:
        sub = df[df["h"] == h]
        if sub.empty:
            continue
        print(f"\n{h}d — coverage (target 80/90/95) and dip/run beaten "
              f"(target 20/5); n = forecast-days, ~n/{h} independent per coin")
        print(f"  {'model':<7}{'regime':<7}{'n':>6}{'80%':>7}{'90%':>7}{'95%':>7}"
              f"{'dip':>9}{'run':>9}")
        for (m, g), x in sub.groupby(["model", sub["regime"].fillna("?")]):
            print(f"  {m:<7}{g:<7}{len(x):>6}{x.hit80.mean():>7.0%}"
                  f"{x.hit90.mean():>7.0%}{x.hit95.mean():>7.0%}"
                  f"{x.dip_gt5.mean():>5.0%}/{x.dip_gt20.mean():<3.0%}"
                  f"{x.run_gt5.mean():>5.0%}/{x.run_gt20.mean():<3.0%}")
        calm = sub[(sub["model"] == "v1") & (sub["regime"] == "calm")
                   & sub["iv_ratio"].notna()]
        if len(calm):
            hi = calm[calm["iv_ratio"] > H12_RATIO]
            lo = calm[calm["iv_ratio"] <= H12_RATIO]
            rate = lambda x: f"{1 - x.hit90.mean():.0%}" if len(x) else "n/a"
            print(f"  H12 (BTC/ETH calm, v1 90% breach): DVOL/EWMA > {H12_RATIO}: "
                  f"{rate(hi)} (n={len(hi)}) vs <= {H12_RATIO}: {rate(lo)} "
                  f"(n={len(lo)})")
    print("\nForward evidence accumulates slowly: ~26 independent 14-day "
          "windows per coin per year.")
    return 0


# --------------------------------------------------------------- self-test
def self_test() -> int:
    from scripts.test_vol_corridor_cfhs import coin_forecasts, sim_panel
    from scripts.test_vol_corridor_iv import sim_pair, scales
    from scripts.test_vol_corridor_v2 import _forecasts

    # 1. v1 and c1 levels equal the tested implementation at the same day.
    dates, p = list(sim_panel(3).values())[0]
    for h in HORIZONS:
        ref = coin_forecasts(p, h)
        for t in (int(ref["t"][len(ref["t"]) // 2]), int(ref["t"][-1])):
            i = int(np.flatnonzero(ref["t"] == t)[0])
            got = {fc["model"]: fc for fc in forecast_day(p, t) if fc["h"] == h}
            for m in ("v1", "c1"):
                lo, hi = ref[m][0][i], ref[m][1][i]
                assert np.allclose(got[m]["lo"], lo, rtol=0, atol=1e-12), (m, h)
                assert np.allclose(got[m]["hi"], hi, rtol=0, atol=1e-12), (m, h)
    print("  [ok] v1 and c1 levels identical to test_vol_corridor_cfhs")

    # 2. v3 levels equal the tested implementation (incl. dip/run).
    p3, iv, _ = sim_pair("alt", 9)
    for h in HORIZONS:
        sc = scales(p3, iv, h)
        ps = max(int(np.flatnonzero(np.isfinite(sc[m]))[0]) for m in ("v1", "v3"))
        t = len(p3) - 1
        ref = _forecasts(p3, sc["v3"], h, np.array([t]), ps)
        got = [fc for fc in forecast_day(p3, t, iv) if fc["model"] == "v3"
               and fc["h"] == h][0]
        for k in ("lo", "hi", "dip", "run"):
            assert np.allclose(got[k], ref[k][0], rtol=0, atol=1e-12), (k, h)
    print("  [ok] v3 levels (bands, dip, run) identical to test_vol_corridor_iv")

    # 3. No lookahead: forecasts at t ignore everything after t.
    t = len(p) - 200
    p2 = p.copy(); p2[t + 1:] *= 3.0
    a, b = forecast_day(p, t), forecast_day(p2, t)
    assert len(a) == len(b) and len(a) >= 4
    for x, y in zip(a, b):
        assert all(np.array_equal(x[k], y[k]) for k in ("lo", "hi", "dip", "run"))
    print("  [ok] no lookahead: day-t forecasts unchanged by later prices")

    # 4. Scoring on a hand-made path.
    idx = pd.date_range("2026-01-01", periods=10)
    closes = pd.Series([100, 95, 90, 104, 110, 108, 107, 106, 105, 104.0], index=idx)
    fc = {"forecast_date": date(2026, 1, 1), "h": 7,
          "lo80": -0.05, "hi80": 0.05, "lo90": -0.10, "hi90": 0.10,
          "lo95": -0.2, "hi95": 0.2, "dip5": 0.05, "dip20": 0.2,
          "run5": 0.05, "run20": 0.2}
    s = score(closes, fc)
    assert s["end_date"] == date(2026, 1, 8)
    assert abs(s["ret"] - math.log(1.06)) < 1e-12
    assert abs(s["dip"] - math.log(100 / 90)) < 1e-12
    assert abs(s["run"] - math.log(1.10)) < 1e-12
    assert (s["hit80"], s["hit90"], s["dip_gt5"], s["run_gt5"], s["run_gt20"]) == \
        (False, True, True, True, False)
    assert score(closes, {**fc, "forecast_date": date(2026, 1, 5)}) is None
    print("  [ok] scoring: return, deepest dip, highest run, hits; open "
          "windows left unscored")

    # 5. Row shape matches the table.
    row = to_row(a[0], date(2026, 10, 7), "BTCUSDT")
    assert len(row) == len(FC_COLS)
    assert all(not isinstance(v, (np.floating, np.integer)) for v in row)
    print("  [ok] forecast rows match the table columns")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily corridor logger")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("CORRIDOR_INTERVAL_SECONDS", "3600")))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.report:
        return report()
    if a.loop:
        loop(a.interval)
        return 0
    run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
