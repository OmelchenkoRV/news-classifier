# Findings: ETF Outflow Extremes → Drawdowns — NULL

**Status:** NULL on both assets, as predicted. ETF outflow extremes are
followed by ≥10% drawdowns at essentially the unconditional rate — ETH edge
+0.8pp (z = +0.07), BTC edge +0.2pp (z = +0.03). Candidate #10.

The more durable output is methodological. Building this test surfaced two
lessons that apply to every grid in the project: **grid contiguity is not
independent evidence when cells share events**, and **a noise-floor method
must itself be calibrated before it is trusted** — one built here failed and
was removed.

**Date:** 2026-10-07
**Test:** `scripts/test_etf_outflows.py`
**Data:** `eth_etf_flows` (ETH from 2024-07-23, BTC from 2024-01-11),
`price_snapshots` daily closes

---

## Why this was tested — and why it wasn't already covered

On 2026-10-06 the ETH spot-ETF 5-day net flow reached **−$354M**, the **6.9th
percentile** of its history, after five consecutive outflow days. The outflows
occurred during US hours *before* that evening's liquidation flush, so they
preceded the drop rather than reacting to it.

Candidate #5 (`FINDINGS_aug2026_rally.md`) had been described as covering ETF
flows. **It did not cover this.** That test asked whether **BTC inflow
crossovers predict rallies**. Whether **outflow extremes predict drawdowns** —
for either asset — had never been tested. The earlier summary overstated its
scope; this test closes the gap.

---

## Pre-registration (written before running against real data)

    asset=ETH, 5-day trading-window net flow,
    event = 5d sum crosses DOWN into its bottom 10% (expanding percentile),
    horizon 14 days, magnitude 10% drawdown. Both directions reported.
    BTC as replication.

**Prior on record:** uninformative or null — flows were coincident with price
in both the August and September 2026 moves, and ETH ETF history is ~2 years.

### Two traps specific to this test

1. **No full-sample percentile.** "Bottom decile of history" over the whole
   sample would judge a 2024 event against 2026 flows. The threshold is an
   **expanding percentile of values strictly before each date**, with a
   60-trading-day warm-up. Verified in self-test: a large outflow appended
   later does not move earlier thresholds.
2. **Publication lag.** Flows settle after the US close and are often
   published the next morning. **Entry is the close of the following day**;
   the outcome window is strictly after entry.

Carried over: crossings not days (14-day refractory), era-specific baselines.

---

## Result

| asset | n | drawdown ≥10% | baseline | edge | z |
|---|---|---|---|---|---|
| **ETH** (primary) | 14 | 4/14 = 28.6% | 27.8% | **+0.8pp** | **+0.07** |
| **BTC** (replication) | 21 | 3/21 = 14.3% | 14.1% | **+0.2pp** | **+0.03** |

Rally edges: ETH −4.0pp (z −0.32), BTC −3.7pp (z −0.44).

Two assets, two independent histories, both landing almost exactly on the
unconditional rate. The ETH primary is also flagged uninformative on n=14 —
but the hit rate sits on the baseline, so there is no edge for the small n to
be hiding.

### The event list shows no direction

Outflow extremes were followed by both outcomes:

- **Drawdowns:** Jan 2026 −38.2%, Feb 2025 −20.2%, Jun 2026 −14.8%,
  May 2026 −10.6%
- **Rallies:** Aug 2025 +31.6%, Sep 2025 +16.7%, Nov 2024 +14.4%,
  Aug 2025 +13.1%

The pattern matches funding, taker ratio and long/short: flow and positioning
metrics **describe** what is happening; they do not anticipate it.

---

## The grid: 42/54 positive — and why that means nothing

The drawdown column shows **42 of 54 cells positive**, with the largest edges
**+28 to +53pp**. Under the rule "contiguity over peaks," that would look like
support. It is not:

- The two genuinely independent pieces of evidence — the ETH primary and the
  BTC replication — are both null.
- The striking cells are all `win=10`, with **n = 7 to 9**: the smallest
  samples in the grid, nested inside one another, and visibly carried by a
  single event (**2026-01-22, −38.2%**).
- At n≈7 the noise spread is roughly ±15pp, and those cells share almost all
  of their events. Twelve cells built from the same seven events are close to
  **one observation repeated twelve times**.

---

## Methodological lesson 1: grid contiguity is not independent evidence

While building the harness, a render on **pure synthetic noise** — flows and
price independent by construction — produced:

    PRIMARY: edge +32.9pp, z = +3.10
    grid drawdown column: +15 to +35pp across many cells

A 60-seed Monte Carlo showed the harness is **unbiased** (mean edge −0.5pp,
z spread 0.83, 1.7% beyond +1.96 vs 2.5% nominal). That render was a rare
draw. But it exposed the mechanism: **neighbouring grid cells share most of
their events**, so one lucky event set propagates through the whole grid and
reads as contiguity.

**Rule revision.** "Contiguity over peaks" holds only across **independent**
evidence — other assets, other eras, non-overlapping events. Adjacent cells
that share events are not confirmation.

**Retrospective check.** The funding F2 result (`FINDINGS_funding_defensive.md`)
showed 32/36 positive cells. Its conclusions survive this revision because its
decisive evidence was the **era split and the ETH replication**, not the grid.
Any future result resting mainly on grid contiguity should be treated as
unconfirmed.

---

## Methodological lesson 2: calibrate the calibration

To measure the noise floor on real data rather than estimate it, a
**circular-shift permutation null** was added: shift the flow series against
price (preserving each series' own structure while destroying alignment) and
compare the real edge to the shifted ones.

It was checked on synthetic noise before use — and **failed**:

| check | expected under noise | permutation null |
|---|---|---|
| mean p-value | 0.50 | **0.38** |
| share p < 0.05 | ~5% | **12.5%** |
| share p < 0.10 | ~10% | **17.5%** |

It flagged pure noise as significant at **more than twice** the nominal rate.
The likely cause is an interaction between the circular wrap and the
expanding-window threshold; it was not fully pinned down, and with a
calibrated alternative available, the hunt was time-boxed.

**It was removed rather than left in the codebase**, where it could later be
trusted. The binomial z, which passed calibration, is used instead, and
`--calibrate` re-measures the noise band on demand:

    60 noise datasets, mean n = 16.5
    drawdown edge under noise: mean +0.1pp, sd 10.1pp
    z under noise: mean +0.03, sd 0.91; 3.3% exceed +1.96 (nominal 2.5%)

A side-finding from the first self-test: a **perfectly periodic** planted
signal also fooled the permutation check, because circular shifts realign
periodic clusters. Synthetic tests need irregular structure.

---

## Limitation (stated so it is not later used as a rescue)

Flows are in dollars, and ETF assets grew substantially over two years — the
ETH threshold drifted from **−$92M to −$312M**. Scaling flows by assets under
management would arguably be the better measure.

Proposing that **after** observing a null is the forking-paths move this
project guards against. It is recorded as a limitation, not a reason to rerun.

---

## Verdict

**NULL** on both assets. The −$354M ETH reading on 2026-10-06 has no
demonstrated drawdown information. That event's own 14-day outcome becomes
observable around 2026-10-21 and will enter the sample then.

**Prior confirmed:** null.

**What the project keeps:**

- A tested harness for flow-extreme events with expanding thresholds and
  publication lag built in.
- A revised rule: contiguity counts only across independent evidence.
- A `--calibrate` mode, and the precedent that a noise-floor method gets
  checked against known noise before it is used.
