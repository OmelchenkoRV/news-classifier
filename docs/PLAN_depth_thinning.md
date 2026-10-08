# Plan: Does Thin Futures Depth Warn of Bigger Moves?

**Status:** PRE-REGISTERED 2026-10-08, before any `bookDepth` data was
loaded. **Result (run 2026-10-08): D1 NOT SUPPORTED** — see Results.
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

## Results (run 2026-10-08)

**Data:** `bookDepth` 2023-01-01 → 2026-10-07. BTC: 1,373 days loaded, 42
with more than 5% bad snapshots. ETH: 1,374 days, 96. Test days (h = 7)
2023-03-12 → 2026-09-23 after the 90-day warm-up and quality filters:
BTC n = 1,248, ETH n = 1,122.

| | BTC | ETH |
|---|---|---|
| **D1: ρ(z, S₇), ±1% (primary)** | **+0.048** (−0.039 to +0.163) | **+0.089** (−0.018 to +0.225) |
| rule: upper bound < 0 | FAIL | FAIL |
| ±1%, h = 14 | +0.133 (+0.014 to +0.269) | +0.160 (+0.014 to +0.324) |
| ±2%, h = 7 | +0.034 (−0.051 to +0.145) | +0.088 (+0.003 to +0.206) |
| ±2%, h = 14 | +0.112 (−0.003 to +0.254) | +0.146 (+0.024 to +0.275) |
| D2: calm days ρ (±1%, h = 7) | +0.038 (n = 959) | +0.057 (n = 791) |
| D3: partial ρ given log(DVOL/EWMA) | −0.051 (−0.145 to +0.058) | −0.020 (−0.140 to +0.122) |
| D4: calm-day v1 7d 90% breaches, thin vs thick | 9.8% (n = 409) vs 13.1% (n = 550) | 9.3% (n = 313) vs 14.0% (n = 478) |

**Verdict (pre-registered, both assets required): D1 NOT SUPPORTED.** Both
point estimates have the wrong sign.

### Hypothesis scorecard

| | result |
|---|---|
| D1 | **Not supported.** ρ positive on both assets. |
| D2 | The written criterion (ρ_calm < ρ_all) is met on both, but only because both are **positive** and the calm one is less so. It was meant as "the warning is stronger on calm days"; with no warning, it says nothing. **Lesson:** a conditional criterion must state the sign (here: ρ_calm < ρ_all < 0). |
| D3 | **Not supported.** Intervals include 0. |
| D4 | (descriptive) On calm days, thin depth went with **fewer** band breaches, not more. |

### The secondary results point the other way

At 14 days (±1%) ρ is positive with intervals clear of zero on both assets:
thinner-than-usual depth was followed by **lower**-than-expected
volatility. This is not a finding to act on:

- it is opposite to the hypothesis, and the 4 of 6 secondary intervals
  that clear zero do so by less than 0.03;
- it fits the mechanical overlap already listed under Caveats. Depth thins
  when volatility is high, EWMA is then high too, and volatility mean-
  reverts, so the following weeks come in below EWMA (S < 0) on thin days.
  More time means more reversion, so the effect is larger at 14 days than 7.
  Controlling for implied vol relative to EWMA (D3) removes it: partial ρ
  −0.05 / −0.02. **This explanation is post hoc and was not tested.**
- Mean-reverting volatility was already tested as corridor v2 and not
  adopted.

### What the result rules out

The rule had 85% power at ρ ≈ −0.21. The D1 lower bounds are −0.04 (BTC)
and −0.02 (ETH). Even after controlling for implied vol (D3) they are
−0.145 and −0.140. **A moderate warning effect (ρ ≈ −0.15 or stronger) is
not in this data.** A small one cannot be excluded, but at that size it
would not move corridor coverage.

### Consequence

- **Fix 3 is closed.** Thin futures depth does not flag which calm spells
  break. The calm gap stands, and so does the practical rule: in a calm
  regime, use the 95% band when you need 90%.
- **No rescue (rule 12):** no other distances, horizons, thresholds or
  subsets will be tried on this data.
- The `bookDepth` tables stay in use as the deep-book gate for the futures
  wall analysis (`--check-stream`).
