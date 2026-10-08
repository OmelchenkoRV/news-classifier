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
