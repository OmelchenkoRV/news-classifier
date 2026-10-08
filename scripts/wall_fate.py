"""
Do order-book walls disappear before price reaches them?

Reads ob_book / ob_minutes (collectors/wall_capture.py) and follows every
wall from the moment it appears to the moment it goes.

PRE-REGISTRATION (written 2026-10-08, before any wall data was captured)
-----------------------------------------------------------------------
Definitions (fixed):
  Grid       absolute buckets, ETH $2, BTC $50 (set by the capture).
  Distance   bid: (mid - bucket top) / mid; ask: (bucket bottom - mid) / mid.
  Baseline   median USD of the NON-EMPTY buckets 0.25%-2% from mid, per side,
             per snapshot.
  Wall       a bucket 0.25%-3% from mid holding >= 5x baseline and >= $250k
             (ETH) / $1M (BTC). The touch zone (< 0.25%) never STARTS a wall.
  Episode    starts when a bucket first qualifies; tracks that absolute
             bucket. It is PRESENT while it holds >= 50% of its peak USD.
             It ENDS after 2 consecutive snapshots below that (one-snapshot
             dips are flicker). A capture gap > 3 intervals CENSORS it.
  Fate, judged over (last present snapshot, first absent snapshot], at
  1-minute resolution: the candle containing the first absent snapshot
  counts, so a wall pulled seconds before price arrived scores REACHED
  (a bias AGAINST W1):
    REACHED  price traded into the bucket (1-minute low < bucket top for
             bids; high >= bucket bottom for asks)
    MOVED    not reached, and a bucket within +-3 buckets on the same side
             holds >= 50% of the wall's peak and did not at the last present
             snapshot (re-quoted, not withdrawn)
    PULLED   not reached and not moved
    CENSORED capture gap, or minute data missing for the window
    ACTIVE   still present at the end of the data

Questions and prior, on record. Evaluated after >= 28 days of capture, per
symbol and per side; 95% intervals by bootstrap over calendar DAYS (2,000
resamples, seed 20261008), because walls cluster in time.
  W1  Most walls that end are PULLED rather than REACHED:
      pulled / (pulled + reached) > 50%, lower bound > 0.50.   prior: yes
  W2  Walls are pulled more often as price approaches: the per-snapshot
      pull rate within 0.5% of price exceeds the rate at 1-3%; ratio
      lower bound > 1.                                          prior: yes
  W3  A wall is support (resistance) beyond what any level gives: when a
      new 60-minute low runs into a present bid wall, price rises 0.5%
      before falling 0.5% (within 60 min) more often than for new lows
      with no wall; difference interval excludes 0. Mirror for asks.
                                                                prior: small/none
MOVED walls are reported but excluded from W1 (they did not leave).
NO RESCUE: thresholds are not tuned after seeing results; any variant run
later is labelled exploratory.

USAGE
-----
    python -m scripts.wall_fate --self-test
    python -m scripts.wall_fate                    # all captured data
    python -m scripts.wall_fate --symbol ETHUSDT --days 7
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOUCH_PCT, BASE_MAX_PCT, WALL_MAX_PCT = 0.25, 2.0, 3.0
WALL_X = 5.0
MIN_USD = {"ETHUSDT": 250_000.0, "BTCUSDT": 1_000_000.0}
KEEP_FRAC = 0.5
MISS_TO_END = 2
MOVE_BUCKETS = 3
GAP_INTERVALS = 3
BANDS = ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99.0))
NEAR, FAR = (0.0, 0.5), (1.0, 99.0)
FP_PCT, FP_MIN, NEWLOW_MIN = 0.5, 60, 60
N_BOOT, SEED = 2000, 20261008


@dataclass
class Snap:
    t: float                 # epoch seconds
    mid: float
    width: float
    bid_base: int
    ask_base: int
    bids: np.ndarray
    asks: np.ndarray

    def usd(self, side: str, k: int) -> float | None:
        """USD in absolute bucket k; None if outside the observed book."""
        if side == "bid":
            i = self.bid_base - k
            arr = self.bids
        else:
            i = k - self.ask_base
            arr = self.asks
        if i < 0:
            return 0.0                  # bucket is now on the other side of mid
        return float(arr[i]) if i < len(arr) else None

    def dist(self, side: str, k: int) -> float:
        if side == "bid":
            return (self.mid - (k + 1) * self.width) / self.mid * 100
        return (k * self.width - self.mid) / self.mid * 100


@dataclass
class Episode:
    side: str
    k: int
    start_i: int
    last_i: int
    peak: float
    dist0: float
    miss: int = 0
    absent_i: int | None = None
    fate: str = ""
    exposures: list = field(default_factory=list)   # (snapshot index, dist)


def side_buckets(s: Snap, side: str):
    arr = s.bids if side == "bid" else s.asks
    base = s.bid_base if side == "bid" else s.ask_base
    ks = base - np.arange(len(arr)) if side == "bid" else base + np.arange(len(arr))
    if side == "bid":
        d = (s.mid - (ks + 1) * s.width) / s.mid * 100
    else:
        d = (ks * s.width - s.mid) / s.mid * 100
    return ks, d, arr


def wall_candidates(s: Snap, side: str, min_usd: float) -> dict:
    ks, d, arr = side_buckets(s, side)
    band = (d >= TOUCH_PCT) & (d <= BASE_MAX_PCT) & (arr > 0)
    if band.sum() < 3:
        return {}
    base = float(np.median(arr[band]))
    ok = (d >= TOUCH_PCT) & (d <= WALL_MAX_PCT) & (arr >= WALL_X * base) & (arr >= min_usd)
    return {int(k): (float(u), float(x)) for k, u, x in zip(ks[ok], arr[ok], d[ok])}


def reached(side: str, k: int, width: float, minutes: pd.DataFrame,
            t0: float, t1: float) -> bool | None:
    """Did price trade into bucket k during (t0, t1]? None if no minute data."""
    m = minutes[(minutes.index > t0 - 60) & (minutes.index < t1)]
    if m.empty:
        return None
    if side == "bid":
        return bool((m["low"] < (k + 1) * width).any())
    return bool((m["high"] >= k * width).any())


def track(snaps: list[Snap], minutes: pd.DataFrame, min_usd: float) -> list[Episode]:
    """Follow every wall episode through the snapshots (both sides)."""
    if not snaps:
        return []
    dt = np.diff([s.t for s in snaps])
    interval = float(np.median(dt)) if len(dt) else 60.0
    done: list[Episode] = []
    active: dict[tuple, Episode] = {}

    def close(ep: Episode, fate: str):
        ep.fate = fate
        done.append(ep)

    for i, s in enumerate(snaps):
        if i and s.t - snaps[i - 1].t > GAP_INTERVALS * interval:
            for ep in active.values():
                close(ep, "CENSORED")
            active = {}
        for side in ("bid", "ask"):
            cands = wall_candidates(s, side, min_usd)
            # update existing episodes on this side
            for key in [k for k in active if k[0] == side]:
                ep = active[key]
                u = s.usd(side, ep.k)
                if u is None:                          # bucket left the book
                    close(ep, "CENSORED"); del active[key]; continue
                if u >= KEEP_FRAC * ep.peak:
                    ep.last_i, ep.miss, ep.absent_i = i, 0, None
                    ep.peak = max(ep.peak, u)
                    ep.exposures.append((i, s.dist(side, ep.k)))
                    continue
                ep.miss += 1
                if ep.miss == 1:
                    ep.absent_i = i
                if ep.miss >= MISS_TO_END:
                    close(ep, classify(ep, snaps, minutes)); del active[key]
            # new episodes
            for k, (u, d) in cands.items():
                if (side, k) not in active:
                    ep = Episode(side, k, i, i, u, d)
                    ep.exposures.append((i, d))
                    active[(side, k)] = ep
    for ep in active.values():
        close(ep, "ACTIVE")
    return done


def classify(ep: Episode, snaps: list[Snap], minutes: pd.DataFrame) -> str:
    a, b = snaps[ep.last_i], snaps[ep.absent_i]
    r = reached(ep.side, ep.k, a.width, minutes, a.t, b.t)
    if r is None:
        return "CENSORED"
    if r:
        return "REACHED"
    for dk in range(-MOVE_BUCKETS, MOVE_BUCKETS + 1):
        if dk == 0:
            continue
        nb = b.usd(ep.side, ep.k + dk)
        was = a.usd(ep.side, ep.k + dk)
        if nb is not None and nb >= KEEP_FRAC * ep.peak and \
                (was is None or was < KEEP_FRAC * ep.peak):
            return "MOVED"
    return "PULLED"


def hazard_table(eps: list[Episode], snaps: list[Snap]) -> dict:
    """Exposure (wall-snapshots) and pull events by distance band and day."""
    rows = []
    for ep in eps:
        if ep.fate not in ("PULLED", "REACHED", "MOVED", "ACTIVE"):
            continue
        for i, d in ep.exposures:
            ev = ep.fate == "PULLED" and i == ep.last_i
            rows.append((ep.side, d, ev, day_of(snaps[i].t)))
    return pd.DataFrame(rows, columns=["side", "dist", "event", "day"])


def day_of(t: float) -> str:
    return pd.Timestamp(t, unit="s").strftime("%Y-%m-%d")


def touch_outcomes(eps: list[Episode], snaps: list[Snap], minutes: pd.DataFrame,
                   width: float) -> pd.DataFrame:
    """W3: new 60-minute lows (highs) — did a present wall sit in the swept
    range? Then first passage +-0.5% from the extreme within 60 minutes."""
    if minutes.empty:
        return pd.DataFrame(columns=["side", "wall", "outcome", "day"])
    t_snap = np.array([s.t for s in snaps])
    m = minutes.sort_index()
    lows, highs, idx = m["low"].values, m["high"].values, m.index.values
    present: dict[int, list] = {}
    for ep in eps:
        for i, _ in ep.exposures:
            present.setdefault(i, []).append(ep)
    rows = []
    for j in range(NEWLOW_MIN, len(m) - 1):
        si = int(np.searchsorted(t_snap, idx[j], side="right")) - 1
        if si < 0:
            continue
        walls = present.get(si, [])
        for side in ("bid", "ask"):
            if side == "bid":
                prev = lows[j - NEWLOW_MIN:j].min()
                if lows[j] >= prev:
                    continue
                L = lows[j]
                wall = any(e.side == "bid" and (e.k + 1) * width > L and
                           e.k * width <= prev for e in walls)
                up, dn = L * (1 + FP_PCT / 100), L * (1 - FP_PCT / 100)
                good, bad = highs, lows
            else:
                prev = highs[j - NEWLOW_MIN:j].max()
                if highs[j] <= prev:
                    continue
                L = highs[j]
                wall = any(e.side == "ask" and e.k * width < L and
                           (e.k + 1) * width > prev for e in walls)
                up, dn = L * (1 - FP_PCT / 100), L * (1 + FP_PCT / 100)
                good, bad = lows, highs
            out = "none"
            for q in range(j + 1, min(j + 1 + FP_MIN, len(m))):
                hold = good[q] >= up if side == "bid" else good[q] <= up
                brk = bad[q] <= dn if side == "bid" else bad[q] >= dn
                if hold and brk:
                    out = "both"; break
                if hold:
                    out = "hold"; break
                if brk:
                    out = "break"; break
            rows.append((side, wall, out, day_of(idx[j])))
    return pd.DataFrame(rows, columns=["side", "wall", "outcome", "day"])


# ------------------------------------------------------------- bootstrap
def day_bootstrap(df: pd.DataFrame, stat, n: int = N_BOOT, seed: int = SEED):
    days = df["day"].unique()
    if len(days) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    groups = {d: g for d, g in df.groupby("day")}
    vals = []
    for _ in range(n):
        pick = rng.choice(days, len(days), replace=True)
        v = stat(pd.concat([groups[d] for d in pick]))
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    return float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))


def pulled_share(df):
    x = df[df["fate"].isin(["PULLED", "REACHED"])]
    return (x["fate"] == "PULLED").mean() if len(x) else float("nan")


def hazard_ratio(h):
    near = h[(h["dist"] >= NEAR[0]) & (h["dist"] < NEAR[1])]
    far = h[(h["dist"] >= FAR[0]) & (h["dist"] < FAR[1])]
    if not len(near) or not len(far) or far["event"].mean() == 0:
        return float("nan")
    return near["event"].mean() / far["event"].mean()


def hold_diff(t):
    t = t[t["outcome"].isin(["hold", "break"])]
    w, nw = t[t["wall"]], t[~t["wall"]]
    if not len(w) or not len(nw):
        return float("nan")
    return (w["outcome"] == "hold").mean() - (nw["outcome"] == "hold").mean()


# ------------------------------------------------------------------ data
def load(symbol: str, days: int | None):
    from config.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cond = "AND captured_at >= now() - %s::interval" if days else ""
        args = (symbol, f"{days} days") if days else (symbol,)
        cur.execute(f"""SELECT captured_at, mid, width, bid_base, ask_base, bids, asks
                        FROM ob_book WHERE symbol = %s {cond} ORDER BY captured_at""",
                    args)
        snaps = [Snap(r[0].timestamp(), r[1], r[2], int(r[3]), int(r[4]),
                      np.asarray(r[5], float), np.asarray(r[6], float))
                 for r in cur.fetchall()]
        cond = cond.replace("captured_at", "minute")
        cur.execute(f"""SELECT minute, low, high FROM ob_minutes
                        WHERE symbol = %s {cond} ORDER BY minute""", args)
        rows = cur.fetchall()
    finally:
        conn.close()
    minutes = pd.DataFrame([(r[0].timestamp(), r[1], r[2]) for r in rows],
                           columns=["t", "low", "high"]).set_index("t")
    return snaps, minutes


def analyse(symbol: str, snaps: list[Snap], minutes: pd.DataFrame) -> dict:
    eps = track(snaps, minutes, MIN_USD.get(symbol, 0.0))
    ep_df = pd.DataFrame([{"side": e.side, "fate": e.fate, "peak": e.peak,
                           "dist0": e.dist0,
                           "life_min": (snaps[e.last_i].t - snaps[e.start_i].t) / 60,
                           "dist_end": snaps[e.last_i].dist(e.side, e.k),
                           "day": day_of(snaps[e.start_i].t)} for e in eps])
    width = snaps[0].width if snaps else 1.0
    return {"eps": ep_df, "haz": hazard_table(eps, snaps),
            "touch": touch_outcomes(eps, snaps, minutes, width)}


def report(symbol: str, res: dict, snaps: list[Snap]):
    print(f"\n{'=' * 78}\n{symbol}: {len(snaps)} snapshots, "
          f"{day_of(snaps[0].t)} … {day_of(snaps[-1].t)}"
          f" ({len(set(day_of(s.t) for s in snaps))} days)\n{'=' * 78}")
    ep = res["eps"]
    if ep.empty:
        print("  no walls found")
        return
    for side in ("bid", "ask"):
        e = ep[ep["side"] == side]
        if e.empty:
            continue
        c = e["fate"].value_counts()
        print(f"\n  {side.upper()} walls: {len(e)} episodes — " + ", ".join(
            f"{f} {c.get(f, 0)}" for f in ("PULLED", "REACHED", "MOVED", "ACTIVE", "CENSORED")))
        ended = e[e["fate"].isin(["PULLED", "REACHED"])]
        if len(ended):
            lo, hi = day_bootstrap(e, pulled_share)
            print(f"  W1 pulled share {pulled_share(e):.0%} of {len(ended)} "
                  f"({_ci(lo, hi, '{:.0%}')}; claim needs lower > 50%)")
            for f in ("PULLED", "REACHED"):
                x = e[e["fate"] == f]
                if len(x):
                    print(f"     {f:<8} median life {x['life_min'].median():.0f} min, "
                          f"peak ${x['peak'].median():,.0f}, distance when last "
                          f"seen {x['dist_end'].median():.2f}%")
        h = res["haz"][res["haz"]["side"] == side]
        if len(h):
            print("  W2 pull rate per snapshot by distance from price:")
            for a, b in BANDS:
                x = h[(h["dist"] >= a) & (h["dist"] < b)]
                if len(x):
                    print(f"     {a:>4.2f}–{min(b, 3.0):<4.2f}%  {x['event'].mean():6.2%} "
                          f"({int(x['event'].sum())} pulls / {len(x)} wall-snapshots)")
            lo, hi = day_bootstrap(h, hazard_ratio)
            print(f"     near (<0.5%) / far (1-3%) = {hazard_ratio(h):.2f} "
                  f"({_ci(lo, hi, '{:.2f}')}; claim needs lower > 1)")
        t = res["touch"][res["touch"]["side"] == side]
        tt = t[t["outcome"].isin(["hold", "break"])]
        if len(tt):
            w, nw = tt[tt["wall"]], tt[~tt["wall"]]
            lo, hi = day_bootstrap(t, hold_diff)
            label = "new lows" if side == "bid" else "new highs"
            rate = lambda x: f"{(x['outcome'] == 'hold').mean():.0%}" if len(x) else "n/a"
            d = hold_diff(t)
            print(f"  W3 {label}: reversal 0.5% before extension 0.5% — "
                  f"with wall {rate(w)} (n={len(w)}), without {rate(nw)} "
                  f"(n={len(nw)}); diff "
                  f"{'n/a' if not np.isfinite(d) else f'{d * 100:+.0f} pts'} "
                  f"({_ci(lo * 100, hi * 100, '{:+.0f}')})")


def _ci(lo, hi, fmt):
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return "95% interval needs >= 2 days of data"
    return f"day-bootstrap 95%: {fmt.format(lo)} to {fmt.format(hi)}"


# ------------------------------------------------------------- self-test
def _book(mid, width, walls_bid=None, walls_ask=None, base=50_000.0, n=60):
    bid_base = math.floor((mid - width / 2) / width)
    ask_base = math.floor((mid + width / 2) / width)
    bids = np.full(n, base); asks = np.full(n, base)
    for k, u in (walls_bid or {}).items():
        if 0 <= bid_base - k < n:
            bids[bid_base - k] = u
    for k, u in (walls_ask or {}).items():
        if 0 <= k - ask_base < n:
            asks[k - ask_base] = u
    return bid_base, ask_base, bids, asks


def _scenario(steps, width=2.0):
    """steps: list of (mid, {bid_k: usd}, {ask_k: usd}, minute_low, minute_high)."""
    snaps, mins = [], []
    for i, (mid, wb, wa, lo, hi) in enumerate(steps):
        bb, ab, b, a = _book(mid, width, wb, wa)
        t = 1_760_000_000 + 60 * i
        snaps.append(Snap(t, mid, width, bb, ab, b, a))
        mins.append((t - 3, lo, hi))   # candle opens ~3 s before the snapshot
    return snaps, pd.DataFrame(mins, columns=["t", "low", "high"]).set_index("t")


def self_test() -> int:
    W = 2.0
    k_far = 1176          # bucket [2352, 2354): ~2% below 2400
    k_near = 1192         # bucket [2384, 2386): ~0.6% below 2400
    big = 1_000_000.0
    # A: far wall pulled while price stays put
    st = [(2400, {k_far: big}, {}, 2399, 2401)] * 3 + [(2400, {}, {}, 2399, 2401)] * 3
    eps = track(*_scenario(st), 250_000)
    assert [e.fate for e in eps] == ["PULLED"], [e.fate for e in eps]
    # B: price falls into the near wall, the wall is eaten
    st = [(2400, {k_near: big}, {}, 2399, 2401), (2395, {k_near: big}, {}, 2392, 2400),
          (2388, {k_near: big}, {}, 2386.5, 2395), (2385, {}, {}, 2383, 2388),
          (2384, {}, {}, 2382, 2386), (2384, {}, {}, 2382, 2386)]
    eps = track(*_scenario(st), 250_000)
    assert [e.fate for e in eps] == ["REACHED"], [e.fate for e in eps]
    # C: wall re-quoted one bucket lower -> MOVED (then the new one is ACTIVE)
    st = [(2400, {k_far: big}, {}, 2399, 2401)] * 3 + \
         [(2400, {k_far - 1: big}, {}, 2399, 2401)] * 3
    eps = track(*_scenario(st), 250_000)
    assert sorted(e.fate for e in eps) == ["ACTIVE", "MOVED"], [e.fate for e in eps]
    # D: a one-snapshot flicker does not end the wall
    st = [(2400, {k_far: big}, {}, 2399, 2401)] * 2 + [(2400, {}, {}, 2399, 2401)] + \
         [(2400, {k_far: big}, {}, 2399, 2401)] * 2
    eps = track(*_scenario(st), 250_000)
    assert [e.fate for e in eps] == ["ACTIVE"] and eps[0].last_i == 4
    # E: capture gap censors; F: below the $ floor is not a wall
    snaps, mins = _scenario([(2400, {k_far: big}, {}, 2399, 2401)] * 4)
    snaps[2].t += 3600; snaps[3].t += 3600
    assert "CENSORED" in [e.fate for e in track(snaps, mins, 250_000)]
    st = [(2400, {k_far: 200_000.0}, {}, 2399, 2401)] * 3
    assert track(*_scenario(st), 250_000) == []
    # ask side mirror: price rises into an ask wall
    ka = 1207              # [2414, 2416): ~0.6% above
    st = [(2400, {}, {ka: big}, 2399, 2401), (2408, {}, {ka: big}, 2400, 2412),
          (2413, {}, {ka: big}, 2405, 2413.5), (2415, {}, {}, 2410, 2416),
          (2416, {}, {}, 2412, 2418), (2416, {}, {}, 2412, 2418)]
    assert [e.fate for e in track(*_scenario(st), 250_000)] == ["REACHED"]
    print("  [ok] fates: far wall vanishing -> PULLED; price trading in -> "
          "REACHED (bid and ask); re-quote -> MOVED; flicker ignored; gap -> "
          "CENSORED; $ floor applied")

    # G: hazard by distance — pulls 4x likelier near price -> ratio > 1;
    #    and the same pull rate at every distance -> interval covers 1
    rng = np.random.default_rng(1)
    eps, snaps = [], []
    for day in range(10):
        for w in range(40):
            d = rng.uniform(0.3, 2.9)
            ep = Episode("bid", 0, 0, 0, big, d, fate="ACTIVE")
            for s in range(20):
                ep.exposures.append((len(snaps), d))
                snaps.append(Snap(day * 86400 + w * 1200 + s * 60, 2400, W, 0, 0,
                                  np.zeros(1), np.zeros(1)))
            if rng.random() < (0.8 if d < 0.5 else 0.2):
                ep.fate, ep.last_i = "PULLED", ep.exposures[-1][0]
            eps.append(ep)
    h = hazard_table(eps, snaps)
    r = hazard_ratio(h)
    lo, _ = day_bootstrap(h, hazard_ratio, n=300)
    assert r > 1 and (np.isnan(lo) or lo > 1), (r, lo)
    for ep in eps:
        if ep.fate == "PULLED" or ep.fate == "ACTIVE":
            ep.fate, ep.last_i = ("PULLED" if rng.random() < 0.4 else "ACTIVE",
                                  ep.exposures[-1][0])
    h0 = hazard_table(eps, snaps)
    lo0, hi0 = day_bootstrap(h0, hazard_ratio, n=300)
    assert lo0 < 1 < hi0, (lo0, hi0)
    print(f"  [ok] hazard: pulls 4x likelier near price -> ratio {r:.1f} "
          f"(lower {lo:.1f}); equal rates -> interval {lo0:.2f}–{hi0:.2f} covers 1")

    # H: first passage on a hand path — wall swept, then +0.5% first -> hold
    snaps, _ = _scenario([(2400, {k_near: big}, {}, 2399, 2401)] * 3)
    ep = Episode("bid", k_near, 0, 2, big, 0.6, fate="ACTIVE",
                 exposures=[(0, .6), (1, .6), (2, .6)])
    t0 = snaps[0].t
    lows = [2390.0] * 60 + [2385.0] + [2386.0, 2398.0]
    highs = [2395.0] * 60 + [2389.0] + [2390.0, 2398.5]
    m = pd.DataFrame({"low": lows, "high": highs},
                     index=[t0 + 60 * q for q in range(len(lows))])
    snaps = [Snap(t0 + 60 * q, 2392, W, *(_book(2392, W, {k_near: big})))
             for q in range(len(lows))]
    ep.exposures = [(q, .3) for q in range(len(lows))]
    t = touch_outcomes([ep], snaps, m, W)
    x = t[(t["side"] == "bid")]
    assert len(x) == 1 and bool(x["wall"].iloc[0]) and x["outcome"].iloc[0] == "hold", x
    print("  [ok] W3: new low swept through a wall, +0.5% came first -> hold")
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Wall fate: pulled or reached?")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    syms = [a.symbol] if a.symbol else list(MIN_USD)
    for sym in syms:
        snaps, minutes = load(sym, a.days)
        if len(snaps) < 10:
            print(f"{sym}: only {len(snaps)} snapshots — let the capture run first")
            continue
        report(sym, analyse(sym, snaps, minutes), snaps)
    print("\nPre-registered read: after >= 28 days of capture (see docstring).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
