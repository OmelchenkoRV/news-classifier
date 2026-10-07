"""
Where are the resting orders? Pull the full Binance order book and find
the price levels where size clusters ("walls").

WHY NOT eth_orderbook_depth
---------------------------
The eth-capture depth collector stores cumulative depth at fixed distances
from mid and fetches a limited number of levels — depth reads identical at
2% and 5% (FINDINGS_liquidation_cascade.md). It shows top-of-book balance,
not WHERE size sits. This script fetches the deepest book the API allows
and buckets it by price.

READ THE OUTPUT WITH THESE CAVEATS
----------------------------------
* A snapshot. Books change by the second; large resting orders are often
  pulled as price approaches (spoofing, or simply re-quoting). A wall is
  where size sits NOW, not a guarantee of support.
* Coverage is limited. Spot returns up to 5000 levels per side, futures up
  to 1000. The script reports how far from mid the book actually reaches —
  walls beyond that distance are invisible, not absent.
* One venue. Binance is the largest, not the whole market.
* "Support levels" quoted in the news are mostly chart-derived or
  liquidation-heatmap estimates, not observed orders. This shows the latter.

USAGE
-----
    python -m scripts.orderbook_walls                     # ETHUSDT spot
    python -m scripts.orderbook_walls --symbol BTCUSDT
    python -m scripts.orderbook_walls --market futures
    python -m scripts.orderbook_walls --bucket-pct 0.25 --top 8
"""

from __future__ import annotations

import argparse
import sys

import requests

ENDPOINTS = {
    "spot":    ("https://api.binance.com/api/v3/depth", 5000),
    "futures": ("https://fapi.binance.com/fapi/v1/depth", 1000),
}


def fetch_book(market: str, symbol: str) -> tuple[list, list]:
    url, limit = ENDPOINTS[market]
    r = requests.get(url, params={"symbol": symbol, "limit": limit},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    bids = [(float(p), float(q)) for p, q in data["bids"]]
    asks = [(float(p), float(q)) for p, q in data["asks"]]
    return bids, asks


def bucket(levels: list, mid: float, width_pct: float, reach_pct: float
           ) -> dict:
    """Sum USD notional into buckets indexed by distance from mid.

    Bucket n covers distances [n*w, (n+1)*w) on EITHER side — the same
    convention for bids and asks. The outermost bucket may be only
    partially observed (the book ends inside it); it is KEPT, because a
    partial bucket can only UNDER-state size — a large reading there is
    still real — but analyse() flags it and excludes it from the median.
    """
    out = {}
    for price, qty in levels:
        dist_pct = abs(price - mid) / mid * 100
        n = int(dist_pct / width_pct)
        out[n] = out.get(n, 0.0) + price * qty
    return out


def auto_width(reach_pct: float) -> float:
    """Pick a bucket width giving ~8+ buckets across the observed book."""
    for w in (0.25, 0.1, 0.05, 0.025, 0.01):
        if reach_pct / w >= 8:
            return w
    return 0.01


def _median(xs: list) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def analyse(bids: list, asks: list, width_pct: float | None, top: int
            ) -> dict:
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    reach_bid = (mid - bids[-1][0]) / mid * 100
    reach_ask = (asks[-1][0] - mid) / mid * 100
    if width_pct is None:
        width_pct = auto_width(min(reach_bid, reach_ask))

    def cum(levels, pct, side):
        lim = mid * (1 - pct / 100) if side == "bid" else mid * (1 + pct / 100)
        ok = (lambda p: p >= lim) if side == "bid" else (lambda p: p <= lim)
        return sum(p * q for p, q in levels if ok(p))

    bb = bucket(bids, mid, width_pct, reach_bid)
    ab = bucket(asks, mid, width_pct, reach_ask)

    # The TOUCH bucket (n=0) always holds the densest liquidity, so it is
    # reported separately and excluded from wall ranking. Including it is
    # what produced the fake "7x walls at +0.00%" in the first version.
    # Baseline is the MEDIAN of the remaining buckets, robust to the
    # walls being measured.
    def walls(bk, reach):
        partial_n = int(reach / width_pct)          # book ends inside this one
        rest = {n: v for n, v in bk.items() if n >= 1}
        full = [v for n, v in rest.items() if n < partial_n]
        base = _median(full) if full else _median(list(rest.values()))
        ranked = sorted(rest.items(), key=lambda kv: -kv[1])[:top]
        ranked = [(n, v, n >= partial_n) for n, v in ranked]
        return bk.get(0, 0.0), base, ranked

    bt, bbase, bw = walls(bb, reach_bid)
    at, abase, aw = walls(ab, reach_ask)
    return {
        "mid": mid, "spread": best_ask - best_bid, "w": width_pct,
        "reach_bid": reach_bid, "reach_ask": reach_ask,
        "cum": {pct: (cum(bids, pct, "bid"), cum(asks, pct, "ask"))
                for pct in (0.5, 1, 2, 5)},
        "bid_touch": bt, "ask_touch": at,
        "bid_base": bbase, "ask_base": abase,
        "bid_walls": bw, "ask_walls": aw,
    }


def report(a: dict, symbol: str, market: str, width_pct: float) -> None:
    mid = a["mid"]
    print(f"\n{symbol} {market.upper()}  mid ${mid:,.2f}  "
          f"spread ${a['spread']:.2f}")
    print(f"Book reaches {a['reach_bid']:.2f}% below / "
          f"{a['reach_ask']:.2f}% above mid — walls beyond that are "
          f"INVISIBLE, not absent.")

    print(f"\nCumulative depth (USD):")
    print(f"  {'dist':>6}{'bids':>16}{'asks':>16}{'bid/ask':>10}")
    for pct, (b, s) in a["cum"].items():
        covered = pct <= min(a["reach_bid"], a["reach_ask"])
        ratio = f"{b / s:.2f}" if s else "—"
        flag = "" if covered else "   (beyond book reach)"
        print(f"  {pct:>5}%{b:>16,.0f}{s:>16,.0f}{ratio:>10}{flag}")

    w = a["w"]
    for side, sign, touch, base, walls in (
            ("BID", -1, a["bid_touch"], a["bid_base"], a["bid_walls"]),
            ("ASK", +1, a["ask_touch"], a["ask_base"], a["ask_walls"])):
        label = "buy orders below" if side == "BID" else "sell orders above"
        print(f"\n{side} side ({label}) — {w}% buckets")
        print(f"  touch (within {w}% of mid): ${touch:,.0f}  "
              f"[excluded from walls — always densest]")
        if not walls:
            print("  no fully-observed buckets beyond the touch — book "
                  "too shallow to locate walls")
            continue
        print(f"  median bucket beyond touch: ${base:,.0f}")
        for n, usd, partial in walls:
            lo, hi = n * w, (n + 1) * w
            p_near = mid * (1 + sign * lo / 100)
            p_far = mid * (1 + sign * hi / 100)
            p1, p2 = sorted((p_near, p_far))
            mult = usd / base if base else 0
            mark = "  ◀ WALL" if mult >= 3 else ""
            if partial:
                mark += "  (partial bucket: true size ≥ shown)"
            print(f"  ${p1:>11,.2f}–{p2:>11,.2f}  "
                  f"({sign * lo:+5.2f}%…{sign * hi:+5.2f}%)  "
                  f"${usd:>13,.0f}  {mult:4.1f}x median{mark}")
    print("\nSnapshot only: large resting orders are often pulled as price "
          "approaches.\nRun several times; walls that persist mean more "
          "than walls that flicker.")


def self_test() -> int:
    mid = 2500.0
    bids = [(mid - 0.5 - i * 0.5, 2.0) for i in range(400)]
    asks = [(mid + 0.5 + i * 0.5, 2.0) for i in range(400)]
    # Dense touch liquidity — the case that fooled the first version.
    for i in range(10):
        bids[i] = (bids[i][0], 60.0)
        asks[i] = (asks[i][0], 60.0)
    bids[100] = (bids[100][0], 400.0)          # real bid wall at -2.02%
    asks[50] = (asks[50][0], 300.0)            # real ask wall at +1.02%
    a = analyse(bids, asks, 0.25, 3)

    n_bid = a["bid_walls"][0][0]
    n_ask = a["ask_walls"][0][0]
    assert a["w"] == 0.25
    assert n_bid == int(2.02 / 0.25), n_bid    # bucket 8: -2.00…-2.25%
    assert n_ask == int(1.02 / 0.25), n_ask    # bucket 4: +1.00…+1.25%
    print(f"  [ok] real walls found: bid bucket {n_bid} "
          f"(-{n_bid*0.25:.2f}%), ask bucket {n_ask} (+{n_ask*0.25:.2f}%)")

    assert all(n >= 1 for n, _, _ in a["bid_walls"] + a["ask_walls"]), \
        "touch bucket must never be ranked as a wall"
    assert a["bid_touch"] > 0 and a["ask_touch"] > 0
    print("  [ok] dense touch liquidity reported separately, not as a wall")

    # Regression: a cluster in the PARTIAL edge bucket must still be found
    # (the second version dropped it — and that is where BTC's ~$83k bid
    # cluster sat).
    m2 = 83812.82
    b2 = [(m2 - 0.01 - i * 2.0, 0.6) for i in range(410)]   # reach ~0.98%
    a2 = [(m2 + 0.01 + i * 2.2, 0.25) for i in range(410)]
    for i in range(8):
        b2[i] = (b2[i][0], 25.0); a2[i] = (a2[i][0], 25.0)
    b2[380] = (b2[380][0], 170.0)                            # ~ -0.91%
    r2 = analyse(b2, a2, None, 4)
    top_n, top_v, top_partial = r2["bid_walls"][0]
    dist = top_n * r2["w"]
    assert 0.8 <= dist <= 0.95, (r2["w"], top_n)
    assert r2["w"] < 0.25, "auto width must shrink for a shallow book"
    print(f"  [ok] shallow book: auto width {r2['w']}%, edge cluster at "
          f"-{dist:.2f}% found (partial={top_partial})")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Find order-book walls")
    p.add_argument("--symbol", default="ETHUSDT")
    p.add_argument("--market", choices=["spot", "futures"], default="spot")
    p.add_argument("--bucket-pct", type=float, default=None,
                   help="Bucket width as %% of mid. Default: auto-sized to "
                        "give ~8 buckets across the observed book.")
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    bids, asks = fetch_book(a.market, a.symbol.upper())
    res = analyse(bids, asks, a.bucket_pct, a.top)
    report(res, a.symbol.upper(), a.market, res["w"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
