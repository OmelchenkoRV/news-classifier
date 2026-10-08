# Plan: Does Thin Futures Depth Warn of Bigger Moves?

**Status:** PRE-REGISTERED 2026-10-08, before any `bookDepth` data was
loaded.
**Data:** `collectors/bookdepth_backfill.py` (Binance USD-M futures
`bookDepth` archive)
**Test:** `scripts/test_depth_thinning.py`

## Why

Every corridor fix left calm-regime coverage around 79–83% for a nominal
90%:

- **v2** (mean reversion): not adopted;
- **v3** (implied vol): adopted narrowly;
- **c1** (calm-history calibration): adopted, small gain.

Calm spells end in jumps that past returns cannot see. Fix 3 asked for data
that might flag **which** calm spells break.

Thin order books are a natural candidate. When resting liquidity near price
is unusually low, the same flow of market orders moves price further. The
question is about the **size** of the next move, not its direction, which
is the only kind of question this project has found answerable.

The free archive gives cumulative futures depth at ±1–5% from price every
few tens of seconds, computed from Binance's **full** book (the public
snapshot reaches only about 0.5%), from about 2023.

## Measures (fixed now)

| | definition |
|---|---|
| **D_t** | log of the day's mean USD notional within ±1% (bid + ask), from 15-minute buckets of quality-checked snapshots; needs ≥ 80 of 96 buckets |
| **z_t** | (D_t − median of D over t−90…t−1) ÷ (1.4826 × MAD), needs ≥ 60 valid days; negative = thinner than usual; trailing only |
| **S_t** | log( realised vol over t+1…t+h ÷ EWMA vol at t ): the surprise relative to what the v1 corridor assumes; **h = 7 primary** |

**Data quality.** Snapshots must have depth non-decreasing with distance,
and the implied price at −1% must sit below that at +1%. The implied mid
must also be within 10% of the day's futures close (spot if missing).
Failures are excluded and counted. This targets the bad stretch reported in
binance-public-data issue #431.

## Hypotheses

| | prediction | claim requires | prior |
|---|---|---|---|
| **D1** (primary) | Spearman ρ(z, S₇) < 0: thinner depth precedes higher-than-expected vol | 95% upper bound < 0 on **both** BTC and ETH (moving-block bootstrap over days, 30-day blocks, 2,000 resamples, seed 20261008) | yes, small (\|ρ\| ≈ 0.05–0.15) |
| **D2** | stronger on calm days (real-time label) | ρ_calm < ρ_all | unsure |
| **D3** | survives controlling for implied vol | partial ρ(z, S \| log(DVOL/EWMA)) upper bound < 0 | weak |
| **D4** | (descriptive) calm days: v1 7-day 90% band breach rate when z < 0 vs ≥ 0 | — | — |

Secondary results also reported: h = 14, and ±2% depth.

## Calibration of the rule (synthetic, before real data)

The synthetic data used GARCH(0.10, 0.85, t4) prices and a depth z with
persistence 0.98, like real depth, over 1,350 days.

| world | median ρ | rule passes (per asset) |
|---|---|---|
| null: z uninformative | −0.004 | **2.0%** (target 2.5%) |
| ρ ≈ −0.11 | −0.112 | 32.5% |
| ρ ≈ −0.16 | −0.157 | 61% |
| ρ ≈ −0.21 | −0.211 | 85% |

**What it means for the verdict:**
- **SUPPORTED** needs a moderate effect on both assets.
- **NOT SUPPORTED** cannot rule out a small one.

**Persistence check:** even with both series this persistent, false
positives stayed at 2% per asset.

## Checks done before any real data

- **Downloader self-test:**
  - parsing and 15-minute buckets;
  - a snapshot 20% off the market excluded and counted;
  - non-monotone depth and crossed implied prices rejected;
  - text and epoch timestamps.
- **End-to-end through Postgres:**
  - S3-listed days, download, idempotent rerun, bad snapshot flagged;
  - synthetic multi-year depth carrying real information was detected by
    D1–D4.
- **Deep-book check for the futures walls (part 1's gate),** on known
  inputs: matching depth → 1.00, PASS; 60% depth → 0.60, FAIL.
- **Two bugs found and fixed in testing:**
  - mixed time-zone offsets from Postgres crashed the daily aggregation;
  - the check chose the archive file by London rather than UTC date.

## Caveats on record

- **Not a fresh period.** 2023–2026 has been examined for corridor
  behaviour. Depth is a new information source, not a rescue of an earlier
  model, but the evidence counts for less than a forward test.
- **Mechanical overlap with volatility.** Depth thins when volatility
  rises (market makers widen). S is measured against EWMA vol, which
  already reflects recent volatility, so D1 asks for information beyond
  that. Some overlap remains.
- **Only Binance futures, BTC and ETH.**
- **No rescue:** definitions are not changed after the result.

## Results

*(to be added after the real run)*
