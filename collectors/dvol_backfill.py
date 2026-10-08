"""
DVOL backfill — Deribit's 30-day implied-volatility index, BTC and ETH.

WHY
---
Corridor v2 (docs/FINDINGS_vol_corridor.md) found that calm spells end in
jumps that no model built on past returns can anticipate. Implied volatility
is the options market's forward-looking price of that risk. The live
eth-capture only holds ETH option chains from 2026-04, and BTC options are
not captured. DVOL gives a daily implied-vol series for BOTH assets from
2021-03, free, no API key.

WHAT DVOL IS
------------
Deribit's volatility index: a constant 30-day implied volatility built from
the whole option smile (strikes with |delta| < 0.05 excluded), annualised,
quoted in percent. Daily sigma = DVOL / 100 / sqrt(365). Published since
March 2021. Deribit lists the most liquid crypto options, so DVOL is the
standard "crypto VIX".

ENDPOINT (public)
-----------------
GET {base}/public/get_volatility_index_data
    ?currency=BTC&start_timestamp=<ms>&end_timestamp=<ms>&resolution=1D
Returns result.data = [[ts_ms, open, high, low, close], ...] and
result.continuation: when not null, request again with
end_timestamp=continuation (pages BACKWARDS in time).
Base URLs tried in order: https://www.deribit.com/api/v2, then
https://drb.coinbase.com/api/v2 (the Coinbase-hosted address in the
current docs, after Coinbase acquired Deribit).

TABLE
-----
dvol_daily (currency, d, open, high, low, close), primary key (currency, d).
d = UTC date of the candle start; close = DVOL at the end of that UTC day,
the same moment as the daily close in taker_flow. The current, unfinished
day is NOT stored. Re-runs are idempotent (upsert).

USAGE
-----
    python -m collectors.dvol_backfill --self-test   # no network, no DB
    python -m collectors.dvol_backfill --probe       # last 10 days, no DB
    python -m collectors.dvol_backfill               # BTC+ETH, 2021-03-01 → yesterday

Check afterwards:
    SELECT currency, count(*), min(d), max(d), round(avg(close)::numeric, 1)
    FROM dvol_daily GROUP BY currency;
"""

from __future__ import annotations

import argparse
import logging
import os
import statistics
import sys
import time
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dvol_backfill")

BASES = ("https://www.deribit.com/api/v2", "https://drb.coinbase.com/api/v2")
PATH = "/public/get_volatility_index_data"
REQUEST_PAUSE = 0.25
MAX_PAGES = 500
DAY_MS = 86_400_000

DDL = """
CREATE TABLE IF NOT EXISTS dvol_daily (
    currency   TEXT             NOT NULL,
    d          DATE             NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION NOT NULL,
    fetched_at TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (currency, d)
);
"""

_base_in_use: str | None = None


def _to_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_page(currency: str, start_ms: int, end_ms: int) -> dict:
    """One request. Returns result dict {data, continuation}. Tries each base
    URL once; remembers the first that works."""
    import requests
    global _base_in_use
    params = {"currency": currency, "start_timestamp": start_ms,
              "end_timestamp": end_ms, "resolution": "1D"}
    bases = (_base_in_use,) if _base_in_use else BASES
    errors = []
    for base in bases:
        try:
            resp = requests.get(base + PATH, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise ValueError(f"API error: {body['error']}")
            res = body.get("result")
            if not isinstance(res, dict) or "data" not in res:
                raise ValueError(f"unexpected response shape: {str(body)[:200]}")
            if _base_in_use != base:
                _base_in_use = base
                logger.info("using %s", base)
            return res
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{base}: {e}")
    raise RuntimeError("all endpoints failed:\n  " + "\n  ".join(errors))


def fetch_all(currency: str, start_ms: int, end_ms: int, get=fetch_page,
              max_pages: int = MAX_PAGES) -> list[list]:
    """Page backwards via `continuation` until exhausted. Deduplicates by
    timestamp; returns candles sorted oldest first."""
    seen: dict[int, list] = {}
    cur_end = end_ms
    for page in range(max_pages):
        res = get(currency, start_ms, cur_end)
        for c in res.get("data") or []:
            if len(c) >= 5 and c[4] is not None:
                seen[int(c[0])] = c
        cont = res.get("continuation")
        if cont is None:
            break
        cont = int(cont)
        if cont >= cur_end or cont <= start_ms:     # no progress / done
            break
        cur_end = cont
        time.sleep(REQUEST_PAUSE)
    else:
        logger.warning("%s: stopped after %d pages (cap)", currency, max_pages)
    return [seen[k] for k in sorted(seen)]


def to_rows(currency: str, candles: list[list], today: date) -> tuple[list, dict]:
    """[(currency, d, o, h, l, c)], dropping the unfinished current day.
    Also returns diagnostics: alignment to 00:00 UTC and units."""
    rows, misaligned = [], 0
    for ts, o, h, lo, c in (x[:5] for x in candles):
        if ts % DAY_MS:
            misaligned += 1
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if d >= today:
            continue
        rows.append((currency, d, _f(o), _f(h), _f(lo), float(c)))
    closes = [r[5] for r in rows]
    med = statistics.median(closes) if closes else float("nan")
    diag = {"misaligned": misaligned,
            "median_close": med,
            "units": ("percent" if med > 1.5 else "fraction") if closes else "n/a",
            "gaps": _gaps([r[1] for r in rows])}
    return rows, diag


def _f(x):
    return None if x is None else float(x)


def _gaps(days: list[date]) -> int:
    return sum(1 for a, b in zip(days, days[1:]) if (b - a).days > 1)


def store(rows: list) -> int:
    from config.database import get_connection, get_cursor
    from psycopg2.extras import execute_values
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(DDL)                 # self-healing, idempotent
        conn.commit()
        cur.execute("SELECT count(*) AS n FROM dvol_daily WHERE currency = %s",
                    (rows[0][0],))
        r = cur.fetchone()
        before = r["n"] if isinstance(r, dict) else r[0]
        execute_values(cur, """
            INSERT INTO dvol_daily (currency, d, open, high, low, close)
            VALUES %s
            ON CONFLICT (currency, d) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high,
                low = EXCLUDED.low, close = EXCLUDED.close,
                fetched_at = NOW()
        """, rows, page_size=1000)
        conn.commit()
        cur.execute("SELECT count(*) AS n FROM dvol_daily WHERE currency = %s",
                    (rows[0][0],))
        r = cur.fetchone()
        after = r["n"] if isinstance(r, dict) else r[0]
        return after - before
    finally:
        conn.close()


def run(currency: str, start_ms: int, end_ms: int, write: bool) -> dict:
    candles = fetch_all(currency, start_ms, end_ms)
    today = datetime.now(timezone.utc).date()
    rows, diag = to_rows(currency, candles, today)
    if not rows:
        logger.warning("%s: no complete days returned", currency)
        return {"currency": currency, "rows": 0}
    logger.info("%s: %d days, %s … %s, median close %.2f (%s), gaps %d, "
                "candles not on 00:00 UTC %d", currency, len(rows), rows[0][1],
                rows[-1][1], diag["median_close"], diag["units"], diag["gaps"],
                diag["misaligned"])
    if diag["misaligned"]:
        logger.warning("%s: candles not aligned to 00:00 UTC — d is the UTC "
                       "date of the candle start; check before joining to "
                       "daily closes", currency)
    out = {"currency": currency, "rows": len(rows), "first": str(rows[0][1]),
           "last": str(rows[-1][1]), "units": diag["units"]}
    if write:
        out["inserted"] = store(rows)
        logger.info("  inserted %d new rows", out["inserted"])
    else:
        for r in rows[-5:]:
            print("   ", r)
    return out


# ----------------------------------------------------------- self-test
def self_test() -> int:
    t0 = _to_ms("2021-03-24")
    days = [t0 + i * DAY_MS for i in range(10)]
    candle = lambda ts, v: [ts, v, v + 1, v - 1, v + 0.5]

    # two pages, backwards, overlapping by one candle
    def fake(cur, start, end):
        if end > days[5]:
            return {"data": [candle(t, 80 + i) for i, t in enumerate(days)
                             if t >= days[5]], "continuation": days[5]}
        return {"data": [candle(t, 80 + i) for i, t in enumerate(days)
                         if t <= days[5]], "continuation": None}
    c = fetch_all("BTC", t0, days[-1] + DAY_MS, get=fake)
    assert [x[0] for x in c] == days, "pagination/dedupe failed"
    print("  [ok] backwards pagination via continuation, overlap deduped")

    # stuck continuation must not loop forever
    calls = []
    def stuck(cur, start, end):
        calls.append(end)
        return {"data": [candle(days[0], 80)], "continuation": end}
    fetch_all("BTC", t0, days[-1], get=stuck)
    assert len(calls) == 1
    print("  [ok] non-advancing continuation stops after one call")

    # unfinished day dropped; units and alignment detected
    today = datetime.fromtimestamp(days[-1] / 1000, tz=timezone.utc).date()
    rows, diag = to_rows("BTC", c, today)
    assert len(rows) == 9 and rows[-1][1] < today
    assert diag["units"] == "percent" and diag["misaligned"] == 0
    assert diag["gaps"] == 0
    _, d2 = to_rows("BTC", [[days[0] + 3600_000, .5, .6, .4, .55]], today)
    assert d2["misaligned"] == 1 and d2["units"] == "fraction"
    print("  [ok] current day dropped; percent/fraction and 00:00 UTC "
          "alignment detected")

    # gaps counted
    gap = [x for i, x in enumerate(c) if i != 3]
    _, d3 = to_rows("BTC", gap, today)
    assert d3["gaps"] == 1
    print("  [ok] missing days counted as gaps")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Deribit DVOL daily backfill")
    p.add_argument("--currencies", default="BTC,ETH")
    p.add_argument("--start", default="2021-03-01",
                   help="YYYY-MM-DD (DVOL published from March 2021)")
    p.add_argument("--probe", action="store_true",
                   help="fetch the last 10 days, print, don't write")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 10 * DAY_MS if a.probe else _to_ms(a.start)
    stats = [run(c.strip().upper(), start_ms, now_ms, write=not a.probe)
             for c in a.currencies.split(",") if c.strip()]
    print(f"\nResult: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
