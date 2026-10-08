"""
Live order-book reconstruction — were walls TRADED AWAY or CANCELLED?

WHY
---
collectors/wall_capture.py photographs the spot book once a minute. It can
see that a wall vanished and whether price reached it, but not WHY its size
went: filled by trades, or cancelled. This service rebuilds the book from
Binance's change stream and matches every reduction against the trades that
hit it, so a wall's disappearance splits exactly into traded and cancelled.
It also covers perpetual futures, whose REST book reaches only ~0.5% (ETH)
/ ~0.2% (BTC) from price.

HOW
---
Two websocket connections (spot, USD-M futures), each subscribed to
<sym>@depth@100ms (every book change) and <sym>@aggTrade (every trade) for
ETHUSDT and BTCUSDT. One local book per (market, symbol), synchronised by
Binance's rules for a local order book:
  spot     buffer events; REST snapshot (limit 5000); retry if
           lastUpdateId < first buffered U; drop events with u <= lastUpdateId;
           then each event must start at most 1 after the book's update id
           (U <= id+1), else restart.
  futures  buffer events; REST snapshot (limit 1000); drop events with
           u < lastUpdateId; first applied event must have
           U <= lastUpdateId <= u; afterwards each event's pu must equal the
           previous event's u, else restart.
Quantities in events are absolute; 0 removes the level.
Safety: resync on a crossed book, on reconnect, and every 24 hours. Every
15 minutes an EXACT check: each price level remembers the update id that
last changed it; a fresh REST snapshot (top 20, lastUpdateId L) is compared
only on levels NOT changed after L, once the book has processed past L.
Those must match exactly — expected mismatches: 0. Two consecutive checks
with any mismatch (BOOK_CHECK_TOLERANCE, default 0) trigger a resync.

Attribution: a reduction of size at a price is "removed"; a trade with
buyer-is-maker (m=true) hit a resting BID at that price, m=false a resting
ASK — that is "traded". Per price bucket and minute:
    cancelled = max(0, removed - traded)
Traded can exceed removed (hidden / iceberg size, futures RPI orders that
the depth stream excludes, or size replenished within the same 100 ms);
the analysis clips and reports it. The 100 ms stream carries only each
level's FINAL size per batch: an order added and cancelled inside one batch
is invisible. Trades are attributed only while the book is synced, so
traded and removed cover the same span — except during the snapshot round
trip at each sync (well under a second), where trades are attributed by
arrival rather than by update id; minutes with a resync are censored.

WHAT IS STORED — ws_book_1m, one row per (market, symbol, minute):
  base, width        bucket id = floor(price / width); arrays cover buckets
                     base .. base+N-1 (3% beyond the minute's range each side)
  rest_bid/rest_ask  USD resting per bucket at the end of the minute
  add_*/rem_*/trd_*  USD added / removed / traded per bucket during the minute
  mid, best_bid, best_ask, hi, lo (trades), trade_usd
  events, resyncs, synced_frac, snap_lo/snap_hi (REST snapshot coverage —
  outside it a futures book only knows levels that have changed since)
~1.5-3 KB per row; 4 streams -> ~10 MB/day. Rows older than RETAIN_DAYS
(default 120) are deleted hourly.

USAGE
-----
    python -m collectors.book_stream --self-test     # no network, no DB
    python -m collectors.book_stream                 # run (the container)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("book_stream")

SYMBOLS = ("ETHUSDT", "BTCUSDT")
WIDTHS = {"ETHUSDT": 2.0, "BTCUSDT": 50.0}
MARKETS = {
    "spot": {"ws": os.getenv("SPOT_WS", "wss://stream.binance.com:9443/stream"),
             "rest": os.getenv("SPOT_REST", "https://api.binance.com") + "/api/v3/depth",
             "limit": 5000, "futures": False},
    "futures": {"ws": os.getenv("FUT_WS", "wss://fstream.binance.com/stream"),
                "rest": os.getenv("FUT_REST", "https://fapi.binance.com") + "/fapi/v1/depth",
                "limit": 1000, "futures": True},
}
GRID_PCT = 3.0
FLUSH_SECONDS = int(os.getenv("BOOK_FLUSH_SECONDS", "60"))
RESYNC_SECONDS = int(os.getenv("BOOK_RESYNC_SECONDS", str(24 * 3600)))
CHECK_SECONDS = int(os.getenv("BOOK_CHECK_SECONDS", "900"))
# exact-check mismatches tolerated; above this in two consecutive checks -> resync
CHECK_TOLERANCE = int(os.getenv("BOOK_CHECK_TOLERANCE", "0"))
RETAIN_DAYS = int(os.getenv("BOOK_RETAIN_DAYS", "120"))
MAX_BUFFER = 20_000
EPS = 1e-9

DDL = """
CREATE TABLE IF NOT EXISTS ws_book_1m (
    market TEXT NOT NULL, symbol TEXT NOT NULL, minute TIMESTAMPTZ NOT NULL,
    mid DOUBLE PRECISION, best_bid DOUBLE PRECISION, best_ask DOUBLE PRECISION,
    width DOUBLE PRECISION NOT NULL, base BIGINT NOT NULL,
    rest_bid REAL[], rest_ask REAL[], add_bid REAL[], add_ask REAL[],
    rem_bid REAL[], rem_ask REAL[], trd_bid REAL[], trd_ask REAL[],
    hi DOUBLE PRECISION, lo DOUBLE PRECISION, trade_usd DOUBLE PRECISION,
    off_grid_usd DOUBLE PRECISION, events INT, resyncs INT,
    synced_frac DOUBLE PRECISION, snap_lo DOUBLE PRECISION,
    snap_hi DOUBLE PRECISION,
    PRIMARY KEY (market, symbol, minute));
"""

ROW_COLS = ("market", "symbol", "minute", "mid", "best_bid", "best_ask", "width",
            "base", "rest_bid", "rest_ask", "add_bid", "add_ask", "rem_bid",
            "rem_ask", "trd_bid", "trd_ask", "hi", "lo", "trade_usd",
            "off_grid_usd", "events", "resyncs", "synced_frac", "snap_lo",
            "snap_hi")


def bucket_of(price: float, width: float) -> int:
    return math.floor(price / width + EPS)


class Book:
    """One local order book with Binance's sync rules and flow accounting."""

    def __init__(self, market: str, symbol: str, futures: bool, width: float):
        self.market, self.symbol, self.futures, self.width = market, symbol, futures, width
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.level_u: dict[tuple, int] = {}     # (side, price) -> last update id
        self.base_L = 0
        self.bad_checks = 0
        self.uid: int | None = None
        self.synced = False
        self.first_after_snap = False
        self.buffer: list[dict] = []
        self.snap_lo = self.snap_hi = None
        self.synced_at = 0.0
        self.resyncs_total = 0
        self._reset_flows()

    # ----------------------------------------------------------- flows
    def _reset_flows(self):
        self.flow = {k: {} for k in ("add_bid", "add_ask", "rem_bid", "rem_ask",
                                     "trd_bid", "trd_ask")}
        self.hi = self.lo = None
        self.trade_usd = 0.0
        self.events = 0
        self.resyncs = 0
        self.synced_time = 0.0
        self.window_start = time.time()
        self._last_tick = self.window_start

    def _acc(self, key: str, price: float, usd: float):
        b = bucket_of(price, self.width)
        d = self.flow[key]
        d[b] = d.get(b, 0.0) + usd

    def tick(self):
        """Accumulate time spent in sync (called on every message)."""
        now = time.time()
        if self.synced:
            self.synced_time += now - self._last_tick
        self._last_tick = now

    # ------------------------------------------------------------ sync
    def desync(self, why: str):
        if self.synced:
            logger.info("%s %s: resync (%s)", self.market, self.symbol, why)
        self.synced = False
        self.uid = None
        self.buffer = []
        self.resyncs += 1
        self.resyncs_total += 1

    def on_depth(self, ev: dict) -> str | None:
        """Returns 'resync' when the book must be rebuilt."""
        self.tick()
        if not self.synced:
            self.buffer.append(ev)
            if len(self.buffer) > MAX_BUFFER:
                self.buffer = self.buffer[-MAX_BUFFER:]
            return None
        return self._apply(ev)

    def load_snapshot(self, snap: dict) -> str:
        """Try to start from a REST snapshot. Returns 'ok', 'retry' (snapshot
        too old for the buffered stream: fetch again) or 'restart'."""
        L = int(snap["lastUpdateId"])
        buf = self.buffer
        if buf and not self.futures and L < int(buf[0]["U"]):
            return "retry"
        if self.futures:
            buf = [e for e in buf if int(e["u"]) >= L]
            if buf and int(buf[0]["U"]) > L:
                return "retry"
        else:
            buf = [e for e in buf if int(e["u"]) > L]
            if buf and not (int(buf[0]["U"]) <= L + 1 <= int(buf[0]["u"])):
                return "restart"
        self.bids = {float(p): float(q) for p, q in snap["bids"] if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in snap["asks"] if float(q) > 0}
        self.snap_lo = min(self.bids) if self.bids else None
        self.snap_hi = max(self.asks) if self.asks else None
        self.uid = L
        self.base_L = L                       # changes before this are untracked
        self.level_u = {}
        self.synced = True
        self.first_after_snap = True
        self.synced_at = time.time()
        self.buffer = []
        for e in buf:
            if self._apply(e) == "resync":
                return "restart"
        return "ok"

    def _apply(self, ev: dict) -> str | None:
        U, u = int(ev["U"]), int(ev["u"])
        if self.futures:
            if self.first_after_snap:
                if u < self.uid:
                    return None
                if not (U <= self.uid <= u):
                    self.desync("first event does not straddle snapshot")
                    return "resync"
                self.first_after_snap = False
            elif int(ev["pu"]) != self.uid:
                self.desync(f"pu {ev['pu']} != previous u {self.uid}")
                return "resync"
        else:
            if u <= self.uid:
                return None
            if U > self.uid + 1:
                self.desync(f"gap: U {U} > id+1 {self.uid + 1}")
                return "resync"
            self.first_after_snap = False
        self.events += 1
        for side, levels in (("bid", ev["b"]), ("ask", ev["a"])):
            book = self.bids if side == "bid" else self.asks
            for p_s, q_s in levels:
                p, q = float(p_s), float(q_s)
                old = book.get(p, 0.0)
                if q > old:
                    self._acc("add_" + side, p, (q - old) * p)
                elif q < old:
                    self._acc("rem_" + side, p, (old - q) * p)
                if q == 0.0:
                    book.pop(p, None)
                else:
                    book[p] = q
                self.level_u[(side, p)] = u
        self.uid = u
        if self.events % 20 == 0 and self.bids and self.asks and \
                max(self.bids) >= min(self.asks):
            self.desync("crossed book")
            return "resync"
        return None

    def on_trade(self, ev: dict):
        self.tick()
        p, q = float(ev["p"]), float(ev["q"])
        if self.synced:      # removals are only seen while synced; keep both
            self._acc("trd_bid" if ev["m"] else "trd_ask", p, p * q)  # on one clock
        self.trade_usd += p * q
        self.hi = p if self.hi is None else max(self.hi, p)
        self.lo = p if self.lo is None else min(self.lo, p)

    # ----------------------------------------------------------- flush
    def flush(self, minute: datetime) -> tuple | None:
        """Row for ws_book_1m (None if the book is not usable), then reset."""
        self.tick()
        span = max(1e-9, time.time() - self.window_start)
        synced_frac = min(1.0, self.synced_time / span)
        row = None
        if self.synced and self.bids and self.asks:
            bb, ba = max(self.bids), min(self.asks)
            mid = (bb + ba) / 2
            lo_ref = min(mid, self.lo or mid) * (1 - GRID_PCT / 100)
            hi_ref = max(mid, self.hi or mid) * (1 + GRID_PCT / 100)
            base = bucket_of(lo_ref, self.width)
            n = bucket_of(hi_ref, self.width) - base + 1
            arr = {k: [0.0] * n for k in ("rest_bid", "rest_ask", *self.flow)}
            for p, q in self.bids.items():
                if p >= lo_ref:
                    arr["rest_bid"][bucket_of(p, self.width) - base] += p * q
            for p, q in self.asks.items():
                if p <= hi_ref:
                    arr["rest_ask"][bucket_of(p, self.width) - base] += p * q
            off = 0.0
            for k, d in self.flow.items():
                for b, usd in d.items():
                    if 0 <= b - base < n:
                        arr[k][b - base] += usd
                    else:
                        off += usd
            row = (self.market, self.symbol, minute, mid, bb, ba, self.width, base,
                   arr["rest_bid"], arr["rest_ask"], arr["add_bid"], arr["add_ask"],
                   arr["rem_bid"], arr["rem_ask"], arr["trd_bid"], arr["trd_ask"],
                   self.hi, self.lo, self.trade_usd, off, self.events,
                   self.resyncs, synced_frac, self.snap_lo, self.snap_hi)
        self._reset_flows()
        return row


def exact_check(book: Book, snap: dict) -> tuple[int, int, list]:
    """Compare book and a REST snapshot (lastUpdateId L) on every level in the
    snapshot's price range that the book has NOT changed after L. Valid only
    when book.base_L <= L <= book.uid (the book was loaded no later than the
    snapshot and has processed past it); otherwise returns None.
    Returns (compared, skipped, mismatches[(side, p, ours, rest)])."""
    L = int(snap["lastUpdateId"])
    if not book.synced or book.uid is None or not (book.base_L <= L <= book.uid):
        return None
    rest = {"bid": {float(p): float(q) for p, q in snap["bids"]},
            "ask": {float(p): float(q) for p, q in snap["asks"]}}
    n = skipped = 0
    bad = []
    for side, mine in (("bid", book.bids), ("ask", book.asks)):
        r = rest[side]
        if not r:
            continue
        edge = min(r) if side == "bid" else max(r)
        inside = (lambda p: p >= edge) if side == "bid" else (lambda p: p <= edge)
        for p in set(r) | {p for p in mine if inside(p)}:
            if book.level_u.get((side, p), 0) > L:
                skipped += 1
                continue
            n += 1
            a, b = mine.get(p, 0.0), r.get(p, 0.0)
            if abs(a - b) > 1e-9 * max(1.0, b):
                bad.append((side, p, a, b))
    return n, skipped, bad


# ------------------------------------------------------------- runtime
def _rest_get(url: str, params: dict) -> dict:
    import requests
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


class Writer:
    def __init__(self):
        self.conn = None
        self.last_prune = 0.0

    def _conn(self):
        if self.conn is None or self.conn.closed:
            from config.database import get_connection
            self.conn = get_connection()
            self.conn.cursor().execute(DDL)
            self.conn.commit()
        return self.conn

    def write(self, rows: list[tuple]):
        if not rows:
            return
        try:
            conn = self._conn()
            from psycopg2.extras import execute_values
            cur = conn.cursor()
            execute_values(cur, f"INSERT INTO ws_book_1m ({', '.join(ROW_COLS)}) "
                                "VALUES %s ON CONFLICT DO NOTHING", rows)
            if time.time() - self.last_prune > 3600 and RETAIN_DAYS > 0:
                cur.execute("DELETE FROM ws_book_1m WHERE minute < now() - %s::interval",
                            (f"{RETAIN_DAYS} days",))
                self.last_prune = time.time()
            conn.commit()
        except Exception as e:                                # noqa: BLE001
            logger.warning("write failed (%d rows lost): %s", len(rows), e)
            try:
                self.conn.close()
            except Exception:                                 # noqa: BLE001
                pass
            self.conn = None


class MarketRunner:
    def __init__(self, name: str, cfg: dict, books: dict):
        self.name, self.cfg, self.books = name, cfg, books
        self.pending: set[str] = set()

    async def snapshot(self, sym: str):
        """Fetch a snapshot until the book syncs (or the connection drops)."""
        if sym in self.pending:
            return
        self.pending.add(sym)
        book = self.books[(self.name, sym)]
        try:
            for attempt in range(20):
                await asyncio.sleep(0.5 if attempt == 0 else min(5.0, 0.5 * attempt))
                try:
                    snap = await asyncio.to_thread(
                        _rest_get, self.cfg["rest"], {"symbol": sym, "limit": self.cfg["limit"]})
                except Exception as e:                        # noqa: BLE001
                    logger.warning("%s %s snapshot failed: %s", self.name, sym, e)
                    continue
                status = book.load_snapshot(snap)
                if status == "ok":
                    logger.info("%s %s synced at update %s (%d bids, %d asks)",
                                self.name, sym, book.uid, len(book.bids), len(book.asks))
                    return
                if status == "restart":
                    book.desync("snapshot did not line up with stream")
        finally:
            self.pending.discard(sym)

    async def run(self):
        import websockets
        streams = "/".join(f"{s.lower()}@depth@100ms/{s.lower()}@aggTrade"
                           for s in SYMBOLS)
        url = f"{self.cfg['ws']}?streams={streams}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=60,
                                              max_size=None, open_timeout=20) as ws:
                    logger.info("%s connected", self.name)
                    backoff = 1.0
                    for sym in SYMBOLS:
                        self.books[(self.name, sym)].desync("connect")
                        asyncio.create_task(self.snapshot(sym))
                    async for msg in ws:
                        m = json.loads(msg)
                        d = m.get("data", m)
                        sym = d.get("s")
                        book = self.books.get((self.name, sym))
                        if book is None:
                            continue
                        if d.get("e") == "depthUpdate":
                            book.on_depth(d)
                            if book.synced and time.time() - book.synced_at > RESYNC_SECONDS:
                                book.desync("scheduled")
                            if not book.synced and sym not in self.pending:
                                asyncio.create_task(self.snapshot(sym))
                        elif d.get("e") == "aggTrade":
                            book.on_trade(d)
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                logger.warning("%s connection lost: %s — reconnecting in %.0fs",
                               self.name, e, backoff)
            for sym in SYMBOLS:
                self.books[(self.name, sym)].desync("disconnect")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def check(self):
        """Exact check against a REST snapshot (see module docstring)."""
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            for sym in SYMBOLS:
                book = self.books[(self.name, sym)]
                if not book.synced:
                    continue
                try:
                    snap = await asyncio.to_thread(
                        _rest_get, self.cfg["rest"], {"symbol": sym, "limit": 20})
                except Exception:                             # noqa: BLE001
                    continue
                L = int(snap["lastUpdateId"])
                for _ in range(50):                           # until the book passes L
                    if book.synced and book.uid is not None and book.uid >= L:
                        break
                    await asyncio.sleep(0.1)
                if not (book.synced and book.uid is not None and book.uid >= L):
                    logger.info("%s %s check skipped: book behind snapshot", self.name, sym)
                    continue
                res = exact_check(book, snap)
                if res is None:                               # re-synced meanwhile
                    logger.info("%s %s check skipped: book re-synced during the check",
                                self.name, sym)
                    continue
                n, skipped, bad = res
                logger.info("%s %s exact check: %d mismatches of %d levels compared "
                            "(%d skipped: changed after the snapshot)%s", self.name, sym,
                            len(bad), n, skipped,
                            "" if not bad else " e.g. " + "; ".join(
                                f"{s} {p}: ours {a} vs REST {b}" for s, p, a, b in bad[:3]))
                book.bad_checks = book.bad_checks + 1 if len(bad) > CHECK_TOLERANCE else 0
                if book.bad_checks >= 2:
                    book.bad_checks = 0
                    book.desync("exact check mismatches twice in a row")
                    asyncio.create_task(self.snapshot(sym))


async def flusher(books: dict, writer: Writer):
    while True:
        now = time.time()
        await asyncio.sleep(FLUSH_SECONDS - (now % FLUSH_SECONDS) + 0.05)
        minute = datetime.fromtimestamp(
            math.floor(time.time() / FLUSH_SECONDS) * FLUSH_SECONDS - FLUSH_SECONDS,
            tz=timezone.utc)
        rows = [r for b in books.values() if (r := b.flush(minute)) is not None]
        await asyncio.to_thread(writer.write, rows)
        if int(time.time() // FLUSH_SECONDS) % max(1, 600 // FLUSH_SECONDS) == 0:
            logger.info("status: " + "; ".join(
                f"{m}/{s} {'synced' if b.synced else 'SYNCING'} "
                f"{len(b.bids)}b/{len(b.asks)}a resyncs={b.resyncs_total}"
                for (m, s), b in books.items()))


async def main_async(markets: list[str]):
    books = {(m, s): Book(m, s, MARKETS[m]["futures"], WIDTHS[s])
             for m in markets for s in SYMBOLS}
    writer = Writer()
    runners = [MarketRunner(m, MARKETS[m], books) for m in markets]
    tasks = [asyncio.create_task(r.run()) for r in runners]
    tasks += [asyncio.create_task(r.check()) for r in runners]
    tasks.append(asyncio.create_task(flusher(books, writer)))
    logger.info("book stream started: %s x %s, flush every %ds",
                ", ".join(markets), ", ".join(SYMBOLS), FLUSH_SECONDS)
    await asyncio.gather(*tasks)


# ----------------------------------------------------------- self-test
def _ev(U, u, b=(), a=(), pu=None):
    e = {"e": "depthUpdate", "U": U, "u": u, "b": [list(x) for x in b],
         "a": [list(x) for x in a]}
    if pu is not None:
        e["pu"] = pu
    return e


def self_test() -> int:
    snap = {"lastUpdateId": 100, "bids": [["2400.0", "10"], ["2398.0", "5"]],
            "asks": [["2402.0", "4"], ["2404.0", "8"]]}
    # spot: stale snapshot -> retry; aligned -> ok; gap -> resync
    bk = Book("spot", "ETHUSDT", False, 2.0)
    bk.on_depth(_ev(150, 155))
    assert bk.load_snapshot(snap) == "retry"
    bk = Book("spot", "ETHUSDT", False, 2.0)
    for e in (_ev(95, 99), _ev(100, 101, b=[("2400.0", "7")]),
              _ev(102, 103, a=[("2402.0", "0")])):
        bk.on_depth(e)
    assert bk.load_snapshot(snap) == "ok" and bk.uid == 103
    assert bk.bids[2400.0] == 7 and 2402.0 not in bk.asks
    assert bk.on_depth(_ev(105, 106)) == "resync" and not bk.synced
    print("  [ok] spot sync: stale snapshot retried; buffered events applied; "
          "gap -> resync")
    # futures: drop u < L; first must straddle L; pu chain enforced
    bk = Book("futures", "ETHUSDT", True, 2.0)
    for e in (_ev(90, 95, pu=89), _ev(96, 104, b=[("2400.0", "3")], pu=95),
              _ev(105, 107, b=[("2398.0", "0")], pu=104)):
        bk.on_depth(e)
    assert bk.load_snapshot(snap) == "ok" and bk.uid == 107
    assert bk.bids == {2400.0: 3.0}
    assert bk.on_depth(_ev(108, 110, pu=107)) is None and bk.uid == 110
    assert bk.on_depth(_ev(112, 113, pu=111)) == "resync"
    bk = Book("futures", "ETHUSDT", True, 2.0)
    bk.on_depth(_ev(101, 104, pu=100))
    assert bk.load_snapshot(snap) == "retry"         # first U > L: missed events
    print("  [ok] futures sync: u<L dropped; first event straddles L; pu chain "
          "break -> resync; missed start -> retry")
    # accounting: adds / removals / trades per bucket
    bk = Book("spot", "ETHUSDT", False, 2.0)
    bk.load_snapshot(snap)
    bk.on_depth(_ev(101, 101, b=[("2400.0", "4"), ("2399.5", "2")], a=[("2404.0", "10")]))
    bk.on_trade({"p": "2400.0", "q": "5", "m": True})     # sell hits the bid
    bk.on_trade({"p": "2402.0", "q": "1", "m": False})    # buy lifts the ask
    b = 1199
    assert abs(bk.flow["rem_bid"][1200] - 6 * 2400.0) < 1e-6
    assert abs(bk.flow["add_bid"][b] - 2 * 2399.5) < 1e-6
    assert abs(bk.flow["add_ask"][1202] - 2 * 2404.0) < 1e-6
    assert abs(bk.flow["trd_bid"][1200] - 5 * 2400.0) < 1e-6
    assert abs(bk.flow["trd_ask"][1201] - 2402.0) < 1e-6
    print("  [ok] attribution: removed 6 at 2400 of which 5 traded (1 cancelled); "
          "adds and lifted ask recorded per bucket")
    row = bk.flush(datetime(2026, 10, 8, tzinfo=timezone.utc))
    r = dict(zip(ROW_COLS, row))
    i = 1200 - r["base"]
    assert abs(r["rest_bid"][i] - 4 * 2400.0) < 1e-6
    assert abs(r["rem_bid"][i] - 6 * 2400.0) < 1e-6 and abs(r["trd_bid"][i] - 12000) < 1e-6
    assert len(r["rest_bid"]) == len(r["trd_ask"]) and r["mid"] == 2401.0
    assert bk.flow["rem_bid"] == {}
    print("  [ok] minute row: arrays aligned on one grid; flows reset after flush")
    # exact check: levels changed after the snapshot are skipped; others must match
    bk = Book("futures", "ETHUSDT", True, 2.0)
    bk.load_snapshot({"lastUpdateId": 100, "bids": [["2400.0", "5"], ["2399.0", "2"]],
                      "asks": [["2401.0", "4"], ["2402.0", "1"]]})
    bk.on_depth(_ev(100, 102, b=[("2399.0", "3")], pu=99))
    bk.on_depth(_ev(103, 105, a=[("2401.0", "6")], pu=102))
    rest_at_102 = {"lastUpdateId": 102, "bids": [["2400.0", "5"], ["2399.0", "3"]],
                   "asks": [["2401.0", "4"], ["2402.0", "1"]]}
    n, sk, bad = exact_check(bk, rest_at_102)
    assert (n, sk, bad) == (3, 1, []), (n, sk, bad)       # 2401 changed at 105 > 102
    bk.bids[2400.0] = 4.0                                  # corrupt an unchanged level
    n, sk, bad = exact_check(bk, rest_at_102)
    assert bad == [("bid", 2400.0, 4.0, 5.0)], bad
    bk.load_snapshot({"lastUpdateId": 200, "bids": [["2400.0", "5"]], "asks": [["2401.0", "4"]]})
    assert exact_check(bk, rest_at_102) is None            # snapshot older than the book's base
    print("  [ok] exact check: level changed after the snapshot skipped; a corrupted "
          "untouched level is caught; a check older than the last resync is refused")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live order-book reconstruction")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--markets", default=os.getenv("BOOK_MARKETS", "spot,futures"))
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    asyncio.run(main_async([m.strip() for m in a.markets.split(",") if m.strip()]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
