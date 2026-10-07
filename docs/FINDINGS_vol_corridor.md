# Findings: The Volatility Corridor — Forecastable, With a Conditional Caveat

**Status:** **POSITIVE — the first result in this project to pass its
pre-registered test.** Filtered historical simulation (FHS) produces a price
corridor that is calibrated on average: Kupiec p > 0.05 in every cell, on
both assets, both horizons, both periods.

**But it is conditionally miscalibrated.** In calm regimes a band labelled
90% covered only ~78%; in storms it covered ~96%. The two errors cancel in the
average. **The corridor is least reliable exactly when it looks narrowest** —
and on 2026-10-07 it looked narrow.

**Date:** 2026-10-07
**Test:** `scripts/test_vol_corridor.py`
**Data:** `price_snapshots` (2020-11 → 2026-10-07), `taker_flow` spot closes
(2017-08 → 2026-08-31), `eth_options_chain` (Deribit, 2026-04 → present)

---

## Why this question

Ten signal candidates failed to forecast **direction** (README ledger). The
pivot: volatility clusters — large moves follow large moves, calm follows
calm — and is among the most reliably forecastable quantities in finance. So
ask *how far* price is likely to travel, not *which way*. That is a corridor.

A corridor only has value if it is **calibrated**: a band claiming 90% must
contain the outcome about 90% of the time. The probability-theory version of
a backtest is therefore a **coverage test**.

---

## Pre-registration (written before running on real data)

Four methods, each forecast at the close of day t for t+h:

| method | construction |
|---|---|
| `gauss_ewma` | Gaussian band, EWMA volatility (RiskMetrics, λ=0.94) |
| `fhs` | empirical quantiles of **past standardised** h-day returns, rescaled by current EWMA vol |
| `raw_emp` | raw empirical quantiles of past h-day returns, no vol scaling (naive baseline) |
| `implied` | Gaussian band from Deribit ATM implied vol — ETH only, short sample |

**PRIMARY:** FHS, 14 days, 90% band, BTC and ETH, Kupiec test on
non-overlapping forecasts.

**Decision rule:** a method is CALIBRATED if Kupiec p > 0.05 at 80, 90 and
95% on the non-overlapping sample for BOTH assets.

### Design choices carried over from earlier lessons

- **No lookahead.** Bands at t use prices through t only; quantiles use h-day
  returns completed by t. Verified in self-test.
- **Overlapping windows = pseudo-replication.** Daily 14-day forecasts overlap
  by 13 days. Coverage is shown on all days, but the Kupiec test uses
  **non-overlapping forecasts only** — "count events, not days," applied to
  coverage.
- **Calibrate the calibration.** Before running on real data, the Kupiec test
  was checked on 40 correctly-specified simulated datasets of the same length:
  rejection rate 0% (FHS) and 7.5% (Gaussian) against a nominal 5% — both
  within sampling noise. Mean coverage was 88.9% at a nominal 90%, the cost of
  estimating rather than knowing volatility. **A point or so below nominal is
  normal.**

---

## Result: FHS passes everywhere

Non-overlapping coverage and Kupiec p for FHS:

| | 80% | 90% | 95% |
|---|---|---|---|
| BTC 7d (2020+) | 82.1% (p .39) | 90.1% (p .97) | 93.3% (p .23) |
| BTC 14d (2020+) | 84.0% (p .25) | 88.8% (p .66) | 95.2% (p .92) |
| ETH 7d (2020+) | 82.5% (p .31) | 90.1% (p .97) | 94.4% (p .69) |
| ETH 14d (2020+) | 79.2% (p .82) | 88.8% (p .66) | 95.2% (p .92) |
| BTC 7d (2017+) | 81.4% (p .47) | 89.6% (p .79) | 94.0% (p .35) |
| BTC 14d (2017+) | 81.1% (p .70) | 87.4% (p .23) | 92.7% (p .16) |
| ETH 7d (2017+) | 80.2% (p .92) | 89.9% (p .92) | 94.7% (p .77) |
| ETH 14d (2017+) | 81.6% (p .57) | 87.9% (p .32) | 94.2% (p .60) |

Also passes at 50% and 99%: **40 of 40 cells**.

### How much independent evidence that really is

Applying the project's own revised rule (contiguity counts only across
independent evidence): the 40 cells are **not** 40 confirmations. The 2020+
run is contained within the 2017+ run; the 7d and 14d horizons use the same
prices; BTC and ETH are highly correlated. The genuinely independent support
is **two assets** (correlated) and **the added 2017–2020 period**, which
includes the 2018 bear market. That is still the strongest evidence any result
in this project has had — but it is two-to-three pieces, not forty.

---

## Hypothesis scorecard

| | prediction | result |
|---|---|---|
| **H1** | Gaussian fails at the tails, breaches skewed **down** | **Half right.** Fails at 95% and 99% in all 8 asset×horizon×period combinations. But breaches skew **upward** in all 8 |
| **H2** | FHS calibrated | **Confirmed** — 40/40 cells |
| **H3** | Raw empirical collapses in storms | **Wrong.** Over-covers everywhere (~95% at nominal 90%); storm coverage 93.6–97.0%. Too wide, not too narrow |
| **H4** | Implied vol > realised (variance risk premium) | **Not supported.** Ratio 1.00–1.03 on ~8–10 independent 14-day windows |

### H1 detail: the fat-tail fingerprint, and an upside skew

The Gaussian band **over-covers at 50%** (55–56%) while **under-covering at
95% and 99%** (93–95% at nominal 99%). Too much probability in the centre, too
little in the tails: the signature of fat-tailed (leptokurtic) returns.

At 95%, Gaussian breaches above the band exceeded breaches below in every one
of the 8 combinations (e.g. BTC 14d 2017+: 4.6% below, 6.8% above). In this
sample the extreme moves a normal distribution misses were more often
**rallies than crashes**. FHS learns this from history, so its bands are
**asymmetric** — BTC's current 14-day 95% corridor runs roughly −18% to +23%.

### H3 detail: wasteful, not dangerous

Raw empirical bands were calibrated against a history containing 2018, 2021
and 2022, so a fixed band is wide enough to cover even storms. Its failure mode
is **over-coverage**: at 14d/90%, BTC width 47.0% versus 38.6% for FHS, for
coverage of 95% instead of 90%. Calibrated wrong in the safe direction, but
paying for unnecessary width.

### H4 detail: fair level, wrong shape

Implied ATM volatility averaged 49.6% against 48.3% subsequently realised —
no measurable premium in this short sample. Yet implied **bands** under-covered
(82.2% at nominal 90%, 84.9% at 95%, 14d). The level of volatility was fair;
the **Gaussian shape** imposed on it missed the tails, exactly as with
`gauss_ewma`. With ~10 independent windows this is descriptive only.

---

## The finding that matters most: conditional miscalibration

FHS passes on average. Split by the current volatility regime:

| 90% band, 14d | calm | mid | storm |
|---|---|---|---|
| FHS — BTC (2020+) | **77.3%** | 92.6% | 96.2% |
| FHS — ETH (2020+) | **78.5%** | 88.6% | 95.7% |
| FHS — BTC (2017+) | **78.2%** | 92.2% | 96.4% |
| FHS — ETH (2017+) | **82.6%** | 92.4% | 95.8% |
| `gauss_ewma` (all four) | **73.9–75.0%** | 86.0–91.2% | 89.3–91.4% |

**In calm regimes, a band labelled 90% covered about 78%** — breached more
than twice as often as it claims. In storms it is too wide. The errors cancel
in the unconditional average, which is what Kupiec scores.

**Mechanism.** Calm periods end abruptly, and volatility mean-reverts after
spikes faster than an EWMA with λ=0.94 assumes. The method therefore
under-reacts at both ends: too narrow before a breakout, too wide after a
shock has passed.

**Practical consequence.** The corridor is least reliable **exactly when it
looks narrowest**.

### This applied on the day of the test

On 2026-10-07 the 14-day 80% FHS bands were about **19% wide for BTC** and
**25% for ETH**, against historical averages of **28%** and **36%**. Current
volatility was well below average — the calm regime, where the method
under-covers. Today's corridor should be read as **narrower than its label**.

### Path touches

At 90%/14d, price touched a band edge at some point within the window in
13.8–16.9% of cases (FHS), against ~10–12% terminal breaches. A corridor
describes where price is likely to **end**, not a fence it will not touch.

---

## The corridor on 2026-10-07 (default run)

| | 14d 80% (FHS) | 14d 95% (FHS) | 14d 95% (implied) |
|---|---|---|---|
| **BTC** $83,595 | $76,486 – $92,607 | $68,784 – $103,225 | — |
| **ETH** $2,581 | $2,311 – $2,947 | $2,069 – $3,251 | $2,196 – $3,033 |

The `--long` run's current corridor is **stale** — `taker_flow` was last
refreshed on 2026-08-31.

For scale: order-book walls found the same day sat within 1% of price. The
realistic two-week range is ±10–20%.

---

## Methodological lesson: passing the test is not the same as working

The pre-registered test scored **unconditional** coverage, and FHS passed it
cleanly. The miscalibration appeared only in a supplementary table the test did
not score. A method can be right on average by being wrong in opposite
directions in different regimes.

**Rule:** after a method passes the test you designed, look at what that test
does not measure — conditional coverage by regime, breach clustering, tail
asymmetry — before relying on it.

---

## Verdict

**The volatility corridor is forecastable**, which the ten direction
candidates were not. FHS is unconditionally calibrated across both assets, both
horizons and two overlapping periods including 2018. This is consistent with
the project's summary principle: managing second moments works; predicting
first moments mostly doesn't.

**It is not reliable regime by regime.** Treat calm-regime corridors as
narrower than labelled and storm-regime corridors as wider.

**What a corridor is for:** sizing — how much exposure fits a given range of
outcomes. It says how far, never which way. It is not a trading signal.

### If resumed

A volatility model that mean-reverts faster than EWMA (GARCH, or EWMA blended
with long-run volatility) is the principled fix for the conditional problem.
Tuning one until the conditional table looks good on these same ~2,100 days
would be overfitting. **Pre-register it, fit on 2017–2020, test on 2020–2026**,
and score conditional coverage as part of the primary test — not as a
supplement.
