"""
Binance USD-M futures `bookDepth` archive — cumulative depth at +-1..5%.

WHY
---
The public futures book snapshot reaches only ~0.5% (ETH) / ~0.2% (BTC) from
price. Binance's public archive publishes, per day, snapshots of cumulative
depth at 1-5% either side, computed from its FULL book. Two uses:
  1. A free history (since ~2023) of how thick the futures book was near
     price — for the pre-registered test scripts/test_depth_thinning.py.
  2. A check of collectors/book_stream.py: does our rebuilt futures book
     hold the size Binance says is there at +-1%? (gate for futures walls
     beyond snapshot coverage; docs/PLAN_wall_fate.md)

SOURCE
------
https://data.binance.vision/data/futures/um/daily/bookDepth/{SYM}/{SYM}-bookDepth-YYYY-MM-DD.zip
CSV columns: timestamp, percentage (-5..-1, 1..5), depth (base asset,
cumulative), notional (USD, cumulative). Available days are LISTED from the
S3 bucket (same endpoints as collectors/binance_symbols.py), not guessed.

DATA QUALITY
------------
Users have reported implied prices (notional/depth) far from the market,
clustered in April-May 2025 (binance-public-data issue #431). Every snapshot
is checked; failures are excluded from the aggregates and counted:
  * internal: depth and notional non-decreasing with |percentage| per side;
    implied price at -1% below implied price at +1%
  * external: sqrt(implied(-1%) x implied(+1%)) within 10% of that day's
    futures close (taker_flow), when available

STORED
------
bookdepth_15m   (symbol, ts, pct) -> notional_mean, notional_min, depth_mean,
                n_good, n_bad      (15-minute buckets, good snapshots only)
bookdepth_days  (symbol, d) -> snapshots, bad, loaded_at   (files loaded)

USAGE
-----
    python -m collectors.bookdepth_backfill --self-test
    python -m collectors.bookdepth_backfill --probe          # list + parse one day
    python -m collectors.bookdepth_backfill                  # BTC + ETH, all days
    python -m collectors.bookdepth_backfill --check-stream   # vs ws_book_1m futures
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bookdepth")

SYMBOLS = ("BTCUSDT", "ETHUSDT")
LIST_ENDPOINTS = ("https://s3-ap-northeast-1.amazonaws.com/data.binance.vision",
                  "https://data.binance.vision.s3.amazonaws.com")
FILE_BASE = "https://data.binance.vision/"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
PREFIX = "data/futures/um/daily/bookDepth/{sym}/"
EXT_TOL = 0.10
BUCKET_MIN = 15

DDL = """
CREATE TABLE IF NOT EXISTS bookdepth_15m (
    symbol TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, pct SMALLINT NOT NULL,
    notional_mean DOUBLE PRECISION, notional_min DOUBLE PRECISION,
    depth_mean DOUBLE PRECISION, n_good INT, n_bad INT,
    PRIMARY KEY (symbol, ts, pct));
CREATE TABLE IF NOT EXISTS bookdepth_days (
    symbol TEXT NOT NULL, d DATE NOT NULL, snapshots INT, bad INT,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (symbol, d));
"""


# ------------------------------------------------------------- listing
def list_keys(prefix: str) -> list[str]:
    """All object keys under prefix; only a COMPLETE listing is accepted."""
    import requests
    for base in LIST_ENDPOINTS:
        for version in ("v2", "v1"):
            keys, token, complete = [], None, False
            for _ in range(500):
                params = {"prefix": prefix, "max-keys": "1000"}
                if version == "v2":
                    params["list-type"] = "2"
                    if token:
                        params["continuation-token"] = token
                elif token:
                    params["marker"] = token
                try:
                    r = requests.get(base, params=params, timeout=45)
                except Exception as e:                        # noqa: BLE001
                    logger.warning("listing %s failed: %s", base, e)
                    break
                if r.status_code != 200 or "<ListBucketResult" not in r.text:
                    break
                root = ET.fromstring(r.text)
                batch = [c.findtext(f"{S3_NS}Key") or "" for c in root.findall(f"{S3_NS}Contents")]
                keys.extend(batch)
                truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
                if not truncated:
                    complete = True
                    break
                token = (root.findtext(f"{S3_NS}NextContinuationToken") if version == "v2"
                         else (root.findtext(f"{S3_NS}NextMarker") or (batch[-1] if batch else None)))
                if not token:
                    break
            if complete:
                return keys
    raise RuntimeError(f"no complete S3 listing for {prefix}")


def available_days(sym: str) -> list[date]:
    out = []
    for k in list_keys(PREFIX.format(sym=sym)):
        if k.endswith(".zip"):
            try:
                out.append(datetime.strptime(k[-14:-4], "%Y-%m-%d").date())
            except ValueError:
                pass
    return sorted(out)


# ------------------------------------------------------------- parsing
def _ts(s: str) -> datetime:
    s = s.strip()
    if s.isdigit():
        v = int(s)
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=timezone.utc)
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_zip(raw: bytes) -> dict[datetime, dict[int, tuple[float, float]]]:
    """{timestamp: {pct: (depth, notional)}}"""
    snaps: dict = defaultdict(dict)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            if not name.endswith(".csv"):
                continue
            text = z.read(name).decode("utf-8", "replace")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 4 or not row[1].strip().lstrip("-").isdigit():
                    continue                                  # header / junk
                snaps[_ts(row[0])][int(row[1])] = (float(row[2]), float(row[3]))
    return snaps


def snapshot_ok(levels: dict[int, tuple[float, float]], ref: float | None) -> bool:
    for sign in (1, -1):
        prev_d = prev_n = 0.0
        for p in range(1, 6):
            if sign * p not in levels:
                continue
            d, n = levels[sign * p]
            if d < prev_d - 1e-9 or n < prev_n - 1e-6 or d <= 0 or n <= 0:
                return False
            prev_d, prev_n = d, n
    if -1 not in levels or 1 not in levels:
        return False
    lo = levels[-1][1] / levels[-1][0]
    hi = levels[1][1] / levels[1][0]
    if not lo < hi:
        return False
    if ref:
        mid = math.sqrt(lo * hi)
        if abs(mid / ref - 1) > EXT_TOL:
            return False
    return True


def aggregate(snaps: dict, ref: float | None):
    """15-minute buckets of good snapshots: rows + (n_snapshots, n_bad)."""
    acc = defaultdict(lambda: {"n": 0.0, "nmin": math.inf, "d": 0.0, "good": 0, "bad": 0})
    bad_total = 0
    for ts, lv in snaps.items():
        b = ts.replace(minute=(ts.minute // BUCKET_MIN) * BUCKET_MIN, second=0, microsecond=0)
        ok = snapshot_ok(lv, ref)
        if not ok:
            bad_total += 1
        for pct, (d, n) in lv.items():
            a = acc[(b, pct)]
            if ok:
                a["n"] += n; a["d"] += d; a["good"] += 1
                a["nmin"] = min(a["nmin"], n)
            else:
                a["bad"] += 1
    rows = []
    for (b, pct), a in sorted(acc.items()):
        g = a["good"]
        rows.append((b, pct, a["n"] / g if g else None, a["nmin"] if g else None,
                     a["d"] / g if g else None, g, a["bad"]))
    return rows, len(snaps), bad_total


# --------------------------------------------------------------- loading
def _ref_closes(conn, sym: str) -> dict[date, float]:
    """Daily reference close: futures where available, spot fills the gaps
    (the two differ by the basis, far inside the 10% tolerance)."""
    cur = conn.cursor()
    out: dict[date, float] = {}
    for market in ("spot", "futures"):                     # futures overwrite spot
        try:
            cur.execute("""SELECT d, close FROM taker_flow WHERE symbol = %s
                           AND market = %s AND close > 0""", (sym, market))
            for r in cur.fetchall():
                out[r["d"] if isinstance(r, dict) else r[0]] = \
                    float(r["close"] if isinstance(r, dict) else r[1])
        except Exception:                                     # noqa: BLE001
            conn.rollback()
    return out


def load_symbol(conn, sym: str, start: date | None, end: date | None) -> dict:
    import requests
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    cur.execute(DDL); conn.commit()
    days = available_days(sym)
    if start:
        days = [d for d in days if d >= start]
    if end:
        days = [d for d in days if d <= end]
    cur.execute("SELECT d FROM bookdepth_days WHERE symbol = %s", (sym,))
    done = {r[0] for r in cur.fetchall()}
    refs = _ref_closes(conn, sym)
    todo = [d for d in days if d not in done]
    logger.info("%s: %d days in archive (%s … %s), %d to load",
                sym, len(days), days[0] if days else "-", days[-1] if days else "-", len(todo))
    loaded = bad_days = 0
    for i, d in enumerate(todo):
        key = f"{PREFIX.format(sym=sym)}{sym}-bookDepth-{d.isoformat()}.zip"
        try:
            r = requests.get(FILE_BASE + key, timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            rows, n, bad = aggregate(parse_zip(r.content), refs.get(d))
        except Exception as e:                                # noqa: BLE001
            logger.warning("%s %s: %s", sym, d, e)
            continue
        if rows:
            execute_values(cur, """INSERT INTO bookdepth_15m (ts, pct, notional_mean,
                notional_min, depth_mean, n_good, n_bad, symbol) VALUES %s
                ON CONFLICT DO NOTHING""", [r + (sym,) for r in rows])
        cur.execute("""INSERT INTO bookdepth_days (symbol, d, snapshots, bad)
                       VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""", (sym, d, n, bad))
        conn.commit()
        loaded += 1
        bad_days += bad > 0.05 * max(n, 1)
        if (i + 1) % 50 == 0:
            logger.info("  %s: %d/%d days", sym, i + 1, len(todo))
    return {"symbol": sym, "days_listed": len(days), "loaded": loaded,
            "days_over_5pct_bad": bad_days,
            "first": str(days[0]) if days else None, "last": str(days[-1]) if days else None}


# ------------------------------------------------------- stream check
def check_stream(conn, days_back: int = 7) -> int:
    """Futures book from book_stream vs Binance bookDepth at +-1%, per minute.
    Pass (pre-registered): ours >= 80% of bookDepth on >= 80% of matched minutes."""
    import requests
    cur = conn.cursor()
    out = 0
    for sym in SYMBOLS:
        cur.execute("""SELECT minute, mid, width, base, rest_bid, rest_ask FROM ws_book_1m
                       WHERE market = 'futures' AND symbol = %s AND resyncs = 0
                         AND minute >= now() - %s::interval ORDER BY minute""",
                    (sym, f"{days_back} days"))
        ours = {}
        for minute, mid, w, base, rb, ra in cur.fetchall():
            tot = 0.0
            for i, (b, a) in enumerate(zip(rb, ra)):
                lo, hi = (base + i) * w, (base + i + 1) * w
                if lo >= mid * 0.99 and hi <= mid:
                    tot += b
                if lo >= mid and hi <= mid * 1.01:
                    tot += a
            ours[minute.replace(second=0, microsecond=0)] = tot
        if not ours:
            print(f"{sym}: no futures rows from book-stream yet")
            continue
        ratios = []
        for d in sorted({m.astimezone(timezone.utc).date() for m in ours}):   # archive is by UTC day
            key = f"{PREFIX.format(sym=sym)}{sym}-bookDepth-{d.isoformat()}.zip"
            r = requests.get(FILE_BASE + key, timeout=60)
            if r.status_code != 200:
                continue                      # archive publishes with a lag
            for ts, lv in parse_zip(r.content).items():
                m = (ts + timedelta(seconds=30)).replace(second=0, microsecond=0) - timedelta(minutes=1)
                if m in ours and -1 in lv and 1 in lv and snapshot_ok(lv, None):
                    ref = lv[-1][1] + lv[1][1]
                    if ref > 0:
                        ratios.append(ours[m] / ref)
        if not ratios:
            print(f"{sym}: no overlapping bookDepth days yet (the archive lags ~1 day)")
            continue
        ratios.sort()
        share = sum(x >= 0.8 for x in ratios) / len(ratios)
        ok = share >= 0.8
        out |= not ok
        print(f"{sym}: {len(ratios)} matched minutes; ours/bookDepth at +-1%: median "
              f"{ratios[len(ratios) // 2]:.2f}; >= 0.8 on {share:.0%} -> "
              f"{'PASS: deep futures walls can be primary' if ok else 'FAIL: keep beyond-snapshot futures walls exploratory'}")
    return out


# ------------------------------------------------------------ self-test
def _zip(rows: list[list]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        csv.writer(s).writerows([["timestamp", "percentage", "depth", "notional"]] + rows)
        z.writestr("X-bookDepth-2025-01-01.csv", s.getvalue())
    return buf.getvalue()


def self_test() -> int:
    good = []
    for ts in ("2025-01-01 00:00:10", "2025-01-01 00:07:40", "2025-01-01 00:16:00"):
        for p in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
            price = 3000 * (1 + p / 200)               # avg price of the band
            dep = 100.0 * abs(p)
            good.append([ts, p, dep, dep * price])
    bad = [["2025-01-01 00:08:00", p, 100.0 * abs(p), 100.0 * abs(p) * 2400]
           for p in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5)]     # implied ~20% off
    snaps = parse_zip(_zip(good + bad))
    assert len(snaps) == 4 and len(snaps[_ts("2025-01-01 00:00:10")]) == 10
    rows, n, nbad = aggregate(snaps, ref=3000.0)
    assert n == 4 and nbad == 1, (n, nbad)
    first = [r for r in rows if r[0] == _ts("2025-01-01 00:00:00") and r[1] == -1][0]
    assert first[5] == 2 and first[6] == 1                 # 2 good, 1 bad in bucket
    assert abs(first[2] - 100 * 3000 * (1 - 1 / 200)) < 1e-6
    print("  [ok] parse + 15-min buckets; the 20%-off snapshot is excluded and counted")
    lv = {p: (100.0 * abs(p), 100.0 * abs(p) * 3000) for p in (-2, -1, 1, 2)}
    assert snapshot_ok(lv, 3000) is False                  # equal implied prices
    lv[2] = (50.0, 50.0 * 3000)
    assert snapshot_ok({**lv, 1: (100.0, 100 * 3010), -1: (100.0, 100 * 2990)}, 3000) is False
    print("  [ok] quality checks: non-monotone depth and crossed implied prices fail")
    assert _ts("1735689600000") == datetime(2025, 1, 1, tzinfo=timezone.utc)
    print("  [ok] timestamps: text and epoch-ms both parsed")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance futures bookDepth archive")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--check-stream", action="store_true")
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.probe:
        import requests
        for sym in syms:
            days = available_days(sym)
            print(f"{sym}: {len(days)} days, {days[0]} … {days[-1]}" if days else f"{sym}: none")
            if days:
                key = f"{PREFIX.format(sym=sym)}{sym}-bookDepth-{days[-1].isoformat()}.zip"
                snaps = parse_zip(requests.get(FILE_BASE + key, timeout=60).content)
                ts = sorted(snaps)
                gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
                print(f"  {days[-1]}: {len(ts)} snapshots, median spacing "
                      f"{sorted(gaps)[len(gaps) // 2] if gaps else 0:.0f}s; first: "
                      f"{ts[0]} {dict(sorted(snaps[ts[0]].items()))}")
        return 0
    from config.database import get_connection
    conn = get_connection()
    try:
        if a.check_stream:
            return check_stream(conn)
        s = date.fromisoformat(a.start) if a.start else None
        e = date.fromisoformat(a.end) if a.end else None
        print("\nResult:", [load_symbol(conn, sym, s, e) for sym in syms])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
