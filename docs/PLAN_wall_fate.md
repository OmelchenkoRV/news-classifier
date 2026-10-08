# Plan: Do Order-Book Walls Disappear Before Price Arrives?

**Status:** PRE-REGISTERED, capture running from 2026-10-08. First read
after **≥ 28 days** (~2026-11-05).
**Capture:** `collectors/wall_capture.py` (docker service `wall-capture`)
**Analysis:** `scripts/wall_fate.py`

## Why

On 2026-10-08 ETH fell through $2,500 in one hour (2,529 → 2,437; ETH open
interest −$460M). The capture's depth-within-1% series showed three
episodes where $100–140M of bids appeared for 1–3 hours and vanished
**without price trading into them**. In the hour before the break, bid
depth was the thinnest of the 36-hour window. The reading "walls are pulled
before price arrives" fits, but it rests on hourly averages of depth
measured by distance from mid. That measure cannot follow one wall.

The new capture stores where size sits by **absolute price** every minute,
plus 1-minute highs and lows. A wall can therefore be followed from birth to
disappearance, and we can see whether price reached it.

## Definitions (fixed now)

| term | definition |
|---|---|
| Grid | absolute buckets: ETH $2, BTC $50 |
| Baseline | median USD of non-empty buckets 0.25–2% from mid, per side, per snapshot |
| Wall | bucket 0.25–3% from mid with ≥ 5× baseline and ≥ $250k (ETH) / $1M (BTC) |
| Present | holds ≥ 50% of its peak USD |
| Ended | 2 consecutive snapshots below that; one-snapshot dips are flicker |
| REACHED | price traded into the bucket between the last present and first absent snapshot (1-minute candles) |
| MOVED | not reached; a bucket within ±3 buckets now holds ≥ 50% of the peak and did not before (re-quoted) |
| PULLED | not reached, not moved |
| CENSORED | capture gap > 3 intervals, or missing candle data |

**Built-in bias, stated in advance:** the candle containing the first
absent snapshot counts. A wall pulled seconds before price arrived
therefore scores REACHED, which works **against** W1.

## Questions, priors and pass criteria

Each is judged per symbol and side. The 95% intervals come from a
bootstrap over calendar **days** (walls cluster in time; 2,000 resamples,
seed 20261008).

| | question | claim requires | prior |
|---|---|---|---|
| **W1** | Do most walls that end get PULLED rather than REACHED? | pulled / (pulled + reached) lower bound > 50% | yes |
| **W2** | Are walls pulled more as price approaches? | pull rate per snapshot within 0.5% ÷ rate at 1–3%, lower bound > 1 | yes |
| **W3** | Is a wall support (resistance) beyond any level? | when a new 60-min low sweeps a present bid wall, price rises 0.5% before falling 0.5% more often than for new lows without a wall; difference interval excludes 0 (mirror for asks) | small or none |

MOVED walls are reported but excluded from W1.

## Checks done before any real data

- Self-test with known answers:
  - a far wall that vanishes → PULLED;
  - price trading in → REACHED, on both bid and ask sides;
  - a re-quote → MOVED;
  - flicker ignored, a gap → CENSORED, the $ floor applied;
  - a 4× near-price pull rate is detected (ratio 4.2, lower bound 3.2), and
    equal rates give an interval covering 1 (0.50–1.50);
  - the first-passage logic is checked on a hand-built path.
- End-to-end: six hours of simulated books ran through the capture into
  Postgres and back through the analysis. Walls programmed to be pulled
  near price came out at a near/far pull ratio of 7–16.

## Limits on record

- **Spot only.** Binance spot, and only two coins. Perpetual-futures
  books, where most leverage sits, are not captured.
- **One-minute resolution.** Walls that live and die within a minute are
  invisible, and the true time of a pull is known only to within a minute.
- **Pulled doesn't mean spoofed.** Market makers routinely re-quote. MOVED
  catches nearby re-quotes, not every legitimate cancellation.
- **REACHED doesn't separate filled from cancelled at the touch.** That
  needs trade-by-trade data at the wall's price, which isn't captured.
- **No rescue:** thresholds are not tuned after seeing results. Variants
  run afterwards are labelled exploratory.

---

## Part 2: traded vs cancelled, from the rebuilt book (spot + futures)

**Added 2026-10-08, before any stream data was captured.**
**Capture:** `collectors/book_stream.py` (docker service `book-stream`)
**Analysis:** `scripts/wall_fate_stream.py`

The minute snapshots above can tell that a wall vanished and whether price
got there, but not **why** its size went. The book stream rebuilds the book
from Binance's 100 ms change stream and its trade stream. Every reduction
of size at a price is matched against the trades that hit resting orders
there, so a wall's disappearance splits exactly into **traded** and
**cancelled**. It also covers perpetual futures, whose public book snapshot
reaches only about 0.5% (ETH) and 0.2% (BTC) from price.

### Definitions (fixed now)

| | touch zone | baseline band | wall zone | multiple | min USD (ETH / BTC) |
|---|---|---|---|---|---|
| spot | < 0.25% | 0.25–2% | 0.25–3% | 5× | $250k / $1M |
| futures | < 0.05% | 0.05–0.5% | 0.05–3% | 5× | $2M / $5M |

The futures book near price is a dense, smooth ladder, about 6× deeper
than spot, so its zones are tighter. Episodes (present at ≥ 50% of peak;
ended after 2 minutes below) are the same as Part 1.

**Fate,** from the flows in the end window:
- **FILLED:** traded ≥ 50% of the removed size;
- **MOVED:** mostly cancelled and re-posted within ±3 buckets;
- **CANCELLED:** otherwise;
- **CENSORED:** any resync or incomplete sync in the window.

### Futures coverage gate

The local futures book is complete only inside the last REST snapshot.
Beyond it, only levels that have changed since are known. Primary futures
results use walls inside the snapshot range. Walls beyond it become primary
only if the deep book passes a check against Binance's `bookDepth` archive:
our book must hold ≥ 80% of `bookDepth`'s ±1% notional on ≥ 80% of
matched minutes.

### Questions

These are read after ≥ 28 days, per market, symbol and side, with
day-bootstrap 95% intervals.

| | question | claim requires | prior |
|---|---|---|---|
| **S1** | Most walls that end are CANCELLED, not FILLED | cancelled share lower bound > 50% | yes |
| **S2** | Cancellation is likelier as price approaches | cancel-end rate near ÷ far, lower bound > 1 (spot near < 0.5%, far 1–3%; futures near < 0.1%, far 0.2–0.5%) | yes |
| **S3** | (descriptive) Of cancelled walls, share pulled before price arrived vs at the touch; share of filled walls with hidden size (traded > removed) | — | — |

### Checks done before any real data

- **Sync rules, self-test:**
  - spot stale snapshot retried, buffered events applied, gap → resync;
  - futures `u < lastUpdateId` dropped, first event straddles the
    snapshot, broken `pu` chain → resync;
  - exact check: a level changed after the check snapshot is skipped, a
    corrupted untouched level is caught, a check older than the last
    resync is refused.
- **Against a fake exchange with a known true book:**
  - all four books (spot and futures, ETH and BTC) ended **identical to
    the truth**, including after injected dropped messages, which were
    detected and resynced within 0.5 s;
  - added, removed and traded totals matched the truth exactly in steady
    state. The only shortfall was size more than 3% from price, which
    equalled the recorded off-grid total to the dollar;
  - the exact check reported 0 mismatches on every check of a clean run;
    a phantom level planted in one book was reported on the next two
    checks, which triggered a resync that restored the true book.
- **Live, first 35 minutes (2026-10-08):** all four books synced on the
  first try with no resyncs. The original top-40 comparison showed spot
  0/40 and futures 5–8/40 differing; the futures count was attributed to
  timing, which the exact check (added the same day) now tests.
- **Analysis self-test:**
  - cancelled far → CANCELLED (not reached);
  - eaten → FILLED;
  - pulled at the touch → CANCELLED (reached);
  - resync → CENSORED, re-quote → MOVED;
  - hidden size counted;
  - futures zones applied.

### Limits on record

- **100 ms aggregation.** The stream carries only each level's final size
  per 100 ms batch, so an order added and cancelled inside one batch is
  invisible.
- **Excluded order types.** Futures "Retail Price Improvement" orders are
  left out of the depth stream; trades against them show up as traded >
  removed.
- **Resync windows.** The minute a resync completes mixes trades and
  removals from slightly different spans, so those minutes are censored.
  Trades arriving during the snapshot round trip (well under a second per
  sync) are attributed by arrival, not by update id.
- **Book checks.** Every 15 minutes the top 20 levels per side are checked
  exactly against a REST snapshot, compared only on levels unchanged since
  that snapshot. Any mismatch twice in a row forces a resync. Scheduled
  resyncs are every 24 hours, so learned deep futures levels survive the
  day. Deep levels outside the check's range are validated only by the
  `bookDepth` gate.
- **Market coverage.** Binance only, ETH and BTC only.
