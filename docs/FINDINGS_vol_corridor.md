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

## Move sizes by direction (`--moves`, 2026-10-08)

The corridor answers "how wide". `--moves` answers the question as people
actually ask it: **if it falls, how far? If it rises, how far?** It splits the
FHS distribution by sign. It does not say which way.

Two views, each at 1/3/7/14 days:

- **Terminal:** where price *ends*, split into falls and rises. "1-in-5 if
  it falls" = of the windows that ended down, 1 in 5 fell further than this.
- **Path:** how far price *travels* inside the window: deepest dip and highest
  run on daily closes. Both usually happen in the same window, and neither
  needs a view on direction, so this is the view for stops and liquidation
  distance. Intraday wicks go further.

Every level is backtested out of sample: expanding window, every day since
2021-11, overlapping windows (descriptive, no significance test), overall and
split by the vol tercile at the forecast date. On synthetic GARCH data the
check returns 19–22% / 5–6% against targets of 20% / 5%.

### Levels on 2026-10-08 (14 days)

BTC $83,046, vol 36% annualised (17th percentile) — **calm**.
ETH $2,570.66, vol 45% (10th percentile) — **calm**.

| 14d | typical | 1-in-5 | 1-in-20 |
|---|---|---|---|
| BTC if it falls | −3.6% | −8.9% ($75.7k) | −18.0% ($68.1k) |
| BTC if it rises | +4.5% | +10.2% ($91.5k) | +21.5% ($100.9k) |
| BTC deepest dip | −2.7% | −7.0% ($77.2k) | −14.9% ($70.7k) |
| BTC highest run | +3.4% | +8.0% ($89.7k) | +16.3% ($96.6k) |
| ETH if it falls | −5.0% | −10.5% ($2,301) | −20.3% ($2,048) |
| ETH if it rises | +6.3% | +13.9% ($2,929) | +25.8% ($3,234) |
| ETH deepest dip | −3.4% | −9.1% ($2,338) | −18.0% ($2,108) |
| ETH highest run | +4.1% | +10.6% ($2,843) | +22.8% ($3,156) |

### What it showed

1. **Calibrated on average.** Across all regimes the 1-in-5 level was beaten
   18–22% of the time and the 1-in-20 level 5–7%, for every horizon, view
   and asset.
2. **Direction is a coin flip.** The historical up-share is 50–53% at every
   horizon — consistent with the ten failed direction candidates.
3. **Rises and falls are nearly the same size.** The larger percentages on
   the rise side are mostly arithmetic: in log terms the 14d 1-in-20 levels
   are symmetric (BTC −0.198 / +0.195; ETH −0.227 / +0.230). ETH shows a
   mild upside tilt in the body (typical −5.0% vs +6.3%, log −0.051 / +0.061).
4. **In calm regimes the 7–14 day sizes are too small** — the same
   conditional failure as the corridor, now measured on the levels people
   would use:

| beaten % (target 20 / 5), calm regime | fall | rise | dip | run |
|---|---|---|---|---|
| BTC 1d | 23/7 | 22/7 | 22/6 | 21/7 |
| BTC 7d | 26/10 | 29/12 | 25/8 | 25/8 |
| BTC 14d | 32/11 | 25/14 | 27/11 | 26/10 |
| ETH 1d | 22/6 | 22/8 | 21/6 | 20/6 |
| ETH 7d | 28/8 | 31/10 | 23/7 | 25/10 |
| ETH 14d | 29/12 | 34/9 | 24/8 | 30/12 |

   At 1 day the levels hold. The error grows with horizon, which is what the
   mechanism predicts: EWMA assumes today's low vol lasts the whole window,
   but calm spells end within days to weeks. At 14 days in a calm regime the
   "1-in-20" levels behaved like 1-in-8 to 1-in-12 events, and the "1-in-5"
   levels like 1-in-3 to 1-in-4.

### How strong this is

- **Direction of the error: believable.** All 16 calm-regime cells at 7 and
  14 days exceed target; it matches the corridor's conditional table and a
  known mechanism.
- **Magnitudes: not.** The calm tercile holds roughly 40 non-overlapping
  14-day windows (fewer genuinely separate calm spells). Each 1-in-20 rate
  rests on two or three episodes.
- **Not independent confirmations.** BTC and ETH are highly correlated; dips
  and falls share episodes.
- On 2026-10-08 both assets sat deeper in calm (10th–17th percentile) than
  the average calm-tercile day, so the understatement may be larger than the
  table. Untested.

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
supplement. → Done: see Corridor v2 below.

---

## Corridor v2 — pre-registration

**Written 2026-10-08, before running on real data.** The rule and threshold
below are frozen in `scripts/test_vol_corridor_v2.py`. Results will be added
underneath without editing this section.

### What changes

Only the volatility forecast. FHS, the expanding pool of past standardised
returns, the 365-day warm-up, the test dates and the regime split are the
same for both models, so any difference comes from the vol model.

| | h-day variance forecast |
|---|---|
| **v1** | EWMA λ=0.94; flat: h × σ²ₜ |
| **v2** | GARCH(1,1) with variance targeting to a **trailing 365-day** mean of r²: vₜ = (1−a−b)·LRₜ + a·rₜ² + b·vₜ₋₁. Term structure h·LR + (v−LR)·(1−φʰ)/(1−φ), φ=a+b. In calm spells (v < LR) the forecast widens toward the long-run level |

The long-run level is trailing rather than full-sample because crypto vol has
fallen since 2017. A long-run level fixed from 2017–2020 would be stale and
would over-widen every later band.

### Design

- **Out of sample.** a, b fitted by Gaussian QMLE on `taker_flow` spot closes
  through **2020-10-31**, per asset, then frozen. Test: **2020-11-01 → last
  date** in `taker_flow`. Same dates and same FHS pool for both models.
- **PRIMARY:** 14d, 90% band, BTC and ETH. Regimes = terciles of EWMA vol on
  the test dates — the same split for both models.

### Hypotheses and priors

| | prediction | prior |
|---|---|---|
| **H5** | v2 calm-regime coverage closer to 90% than v1 | yes |
| **H6** | v2 storm-regime coverage closer to 90% | unsure — a trailing LR stays high for a year after a storm and may keep v2 too wide |
| **H7** | v2 keeps unconditional calibration (Kupiec p > 0.05) | yes |
| **H8** | half-life of a vol shock 1–4 weeks (φ ≈ 0.95–0.98) | roughly |

### Decision rule

v2 is **ADOPTED** only if, on **both** assets:

- **(a)** calm improvement D = |cov_calm(v1) − 90%| − |cov_calm(v2) − 90%|
  exceeds **T_CALM = 4.12 points**;
- **(b)** conditional calibration error (mean |coverage − 90%| over calm, mid
  and storm) is lower for v2;
- **(c)** v2 passes Kupiec (p > 0.05) at 80, 90 and 95%, 14d,
  non-overlapping — the standard v1 was held to.

Supplementary, outside the rule: McNemar on non-overlapping calm windows, 7d
results, storm coverage, band width, calm-regime dip/run exceedance.

**No rescue.** If v2 is not adopted, alternatives thought of afterwards
(other long-run windows, other models) are recorded as limitations, not run
as new tests. `--lr-window` exists only for a labelled sensitivity run, which
gives no verdict.

### Calibrating the rule (synthetic, before real data)

200 paths per world, each 3,300 days, fitted on the first 1,170 (matching
2017-08 → 2020-10), Student-t(4) innovations, single asset:

| world | what it is | D median | rule passes |
|---|---|---|---|
| **null** | EWMA-like GARCH (a=.06, b=.939): v1 is the correct model | +0.5 pts (95th pct **+4.12** → T_CALM) | **4.5%** = false positives per asset |
| **alt** | mean-reverting GARCH (a=.10, b=.85; half-life ≈ 2 weeks) | +2.8 pts | **28.5%** = power per asset |

The fit recovers persistence on these paths (median φ 0.955 vs true 0.95;
0.994 vs 0.999), but single ~800-day fits are noisy: about 1 in 10 lands
below 0.90, and one seed gave 0.66. The real run prints a warning if a fit
hits a grid boundary.

**Power is low, and that limits what a negative result can mean.** In the
synthetic "alt" world, v1's calm coverage was 84%; in the real data it was
~78%. The real effect is larger, so real power is probably higher than
28.5%. Even so, **NOT ADOPTED would be weak evidence that v2 is useless** and
will be recorded that way, not as "mean reversion doesn't matter". Kupiec
alone fails a correct model ~15% of the time (three levels at 5% each). That
cost to power is accepted because v1 was held to the same standard.

### Results (run 2026-10-08; `taker_flow` refreshed through 2026-09-30)

**Verdict: v2 NOT ADOPTED.** Condition (a) failed on both assets; (b) and (c)
passed on both.

**Fit (2017-08 → 2020-10, then frozen):** BTC a=0.147, b=0.768, φ=0.915,
vol-shock half-life **7.9 days**. ETH a=0.108, b=0.825, φ=0.933, **10.0
days**. No boundary warnings.

**PRIMARY: 14d, 90%, test 2020-11-01 → 2026-09-30 (2,146 days):**

| | calm | mid | storm | CCE | overall | width |
|---|---|---|---|---|---|---|
| BTC v1 | 79.7% | 95.2% | 95.7% | 7.1 | 90.2% | 47.8% |
| BTC v2 | 82.5% | 93.8% | **92.0%** | **4.4** | 89.5% | **40.3%** |
| ETH v1 | 83.1% | 92.2% | 95.2% | 4.8 | 90.2% | 61.3% |
| ETH v2 | 84.1% | 91.0% | **91.9%** | **3.0** | 89.0% | **54.7%** |

| rule | BTC | ETH |
|---|---|---|
| (a) calm improvement > 4.12 pts | +2.8 — **FAIL** | +1.0 — **FAIL** |
| (b) CCE lower | 7.1 → 4.4 — pass | 4.8 → 3.0 — pass |
| (c) v2 Kupiec p > 0.05 at 80/90/95 | 0.41 / 0.50 / 0.91 — pass | 0.30 / 0.87 / 0.91 — pass |

### Hypothesis scorecard

| | prediction | result |
|---|---|---|
| **H5** | calm closer to 90% | **Right direction, too small.** +2.8 and +1.0 pts — inside the range the null produces (median +0.5, 95th +4.12). Calm coverage under v2 is still 82.5% / 84.1% |
| **H6** | storm closer to 90% (prior: unsure) | **Yes, on both, and by more than H5:** 95.7% → 92.0% and 95.2% → 91.9% |
| **H7** | unconditional calibration kept | **Yes** |
| **H8** | half-life 1–4 weeks | **Roughly — at the fast end:** 7.9 and 10.0 days (φ 0.915 / 0.933, below the 0.95–0.98 guessed) |

### Supplementary (outside the rule; cannot change the verdict)

- **7d** shows the same pattern: calm 82.9% → 84.5% (BTC), 85.2% → 85.8%
  (ETH); storm 94.7% → 91.9%, 95.7% → 93.6%. McNemar on BTC 7d calm windows:
  4 misses fixed, 0 created, p=0.062 — not significant.
- **Calm-regime dip/run, 14d** (beaten %, target 20/5): BTC dip 29/11 → 24/9,
  run 27/8 → 22/8; ETH dip 25/7 → 20/7, run 30/10 → 27/10.
- **Width:** v2's 90% bands are 11–16% narrower on average (80%: 7–11%; 95%:
  13–22%), with overall coverage within about 1 point of v1's.

### What it means

1. **Mean reversion explains the storm side, not the calm side.** After a
   spike, volatility decays with a half-life of about 8–10 days, faster than
   EWMA assumes. v2 captures that and removes most of the storm over-coverage.
   Calm spells do not end by gradual reversion. They end in jumps, and nothing
   in past returns anticipates a jump.
2. **Part of the calm shortfall is a selection effect, not a model failure.**
   In the synthetic world where EWMA is exactly right, v1's calm coverage was
   still **85.7%** at a nominal 90%. Conditioning on a low vol *estimate*
   picks days where the estimate is too low. BTC's calm coverage here (79.7%)
   sits about 6 pts beyond that; ETH's (83.1%) about 3.
3. **The calm figure itself moves with the setup.** ETH calm coverage was
   78.5% in v1's own run (`price_snapshots`, terciles over 2020+) and 83.1%
   here (`taker_flow`, terciles over the test period, pool from 2018). A
   several-point swing from setup alone is another reason not to lean on the
   exact calm numbers.
4. **The storm and width gains are not adopted (rule 12).** They were not the
   registered criterion, and the data that showed them cannot also confirm
   them. They are a hypothesis for data not yet seen.

### What is used now

**v1 stays.** For sizing in a calm regime, the `--moves` calm-regime table is
the practical correction: at 7–14 days, read the "1-in-20" levels as roughly
1-in-10 and the "1-in-5" levels as roughly 1-in-3 or 1-in-4.

### Limitations — recorded, not rerun

- **Power.** As registered, 28.5% per asset in the synthetic alt world. NOT
  ADOPTED is weak evidence about the calm side, and the storm result shows
  mean reversion matters, just not where it was needed.
- **Ideas that came after the null**, and so are not to be tested on
  2020–2026: conditional FHS (residuals drawn only from same-regime windows),
  other long-run windows, regime-switching or jump models.
- **What could anticipate a calm breakout** is a forward-looking price of
  risk: implied volatility. The Deribit capture is ETH-only and began
  2026-04, too short to test; BTC options are not captured.
- **The only clean test of v2's storm and width advantage is a forward
  test** on data not yet seen. At 14 days that is about 26 independent
  windows a year.

---

## Corridor v3 (implied volatility, DVOL) — pre-registration

**Written 2026-10-08, after loading DVOL but before comparing it with any
price outcome.** Frozen in `scripts/test_vol_corridor_iv.py`. Results will be
added underneath without editing this section.

### Why

v2 showed calm spells end in jumps that past returns cannot anticipate.
Implied volatility is the options market's forward-looking price of risk —
the one available input that could, in principle, see a breakout coming.
`collectors/dvol_backfill.py` loaded Deribit's DVOL (30-day implied vol) for
BTC and ETH: 2,024 days each, 2021-03-24 → 2026-10-07, no gaps, means 59.7%
and 73.7%.

### What changes

Only the volatility scale, again. v3 = FHS with scale DVOLₜ/100 × √(h/365);
v1 = EWMA σₜ × √h. Same FHS machinery, pool start (first DVOL day), 365-day
warm-up, test dates (~2022-04 → 2026-09) and regime split (EWMA terciles).
FHS absorbs any variance risk premium and the 30-day-vs-14-day tenor
mismatch, because the pool holds returns standardised by the same scale.

### Hypotheses and priors

| | prediction | prior |
|---|---|---|
| **H9** | v3 calm coverage closer to 90% than v1 | yes |
| **H10** | v3 storm coverage closer to 90% | yes — implied vol reacts less than EWMA to a spike |
| **H11** | v3 keeps unconditional calibration | yes |
| **H12** | (supplementary) on calm days, a high DVOL/EWMA ratio flags more v1 breaches | yes |

### Decision rule

v3 is **ADOPTED** only if, on **both** assets (14d, 90%):

- **(a)** the calm improvement D = |cov_calm(v1) − 90%| − |cov_calm(v3) − 90%|
  has a one-sided 95% lower bound **> 0**, by moving-block bootstrap over
  test days (block 90 days, 2,000 resamples, seed 20261008);
- **(b)** conditional calibration error is lower for v3;
- **(c)** v3 passes Kupiec (p > 0.05) at 80, 90 and 95%.

The threshold is the data's own sampling noise rather than a synthetic null
(as in v2), because the information in real implied vol cannot be simulated
credibly. The *procedure* is checked on synthetic data instead.

### Calibrating the rule (synthetic, before real data)

200 paths per world; GARCH t(4) prices; a synthetic "DVOL" of 2,020 days;
test ≈ 1,620 days, matching the real layout.

| world | synthetic DVOL | D mean | rule passes | bootstrap honest? |
|---|---|---|---|---|
| **null** | EWMA vol × noise — nothing beyond past returns | +0.2 pts (sd 2.2) | **3.5%** = false positives per asset | lower bound below true mean in 96.5% |
| **alt** | true expected 30-day vol × small noise — perfect knowledge of the vol process | +4.2 pts (calm 83.3% → 89.4%) | **19.0%** = power per asset | 97.0% |

**Power is low.** Even a perfectly informed implied vol, which almost
closes the calm gap on average, passes only one time in five per asset.
There are about 40 independent 14-day calm windows in 4.5 years, and that
is not enough to confirm a 4-point improvement.

- **ADOPTED would be strong evidence.**
- **NOT ADOPTED will say little** and will be recorded that way.

The real test of implied vol is forward tracking.

### Caveat on record

2022–2026 has already been examined for calm-regime behaviour (v1, v2). v3
uses a new information source and was not tuned on that period. Even so,
evidence from it counts for less than a forward test. **No rescue:** ideas
formed after seeing the result are recorded as limitations.

### Results (run 2026-10-08; prices `taker_flow` to 2026-09-30)

**Verdict: v3 ADOPTED** — all three conditions passed on both assets. **The
evidence is thinner than the label**; see "How much to trust it" before
relying on it.

**PRIMARY: 14d, 90%, test 2022-04-07 → 2026-09-30 (1,624 days):**

| | calm | mid | storm | CCE | overall | width |
|---|---|---|---|---|---|---|
| BTC v1 | 75.8% | 89.8% | 95.2% | 6.5 | 86.9% | 35.6% |
| BTC v3 | **78.8%** | 90.8% | 93.9% | **5.3** | 87.8% | **33.4%** |
| ETH v1 | 77.3% | 88.5% | 95.6% | 6.6 | 87.1% | 49.5% |
| ETH v3 | **79.9%** | 87.4% | 93.2% | **5.3** | 86.8% | **44.3%** |

| rule | BTC | ETH |
|---|---|---|
| (a) calm improvement, bootstrap lower bound > 0 | +3.0 pts, LB +1.3 — pass | +2.6 pts, LB +1.6 — pass |
| (b) CCE lower | 6.5 → 5.3 — pass | 6.6 → 5.3 — pass |
| (c) v3 Kupiec p > 0.05 at 80/90/95 | 0.39 / 0.31 / 0.37 — pass | 0.39 / 0.31 / 0.21 — pass |

### Hypothesis scorecard

| | prediction | result |
|---|---|---|
| **H9** | calm closer to 90% | **Yes, both — but small.** +3.0 / +2.6 pts; calm coverage still 78.8% / 79.9% |
| **H10** | storm closer to 90% | **Yes, both:** 95.2% → 93.9%, 95.6% → 93.2% |
| **H11** | unconditional calibration kept | **Yes** |
| **H12** | high DVOL/EWMA ratio flags v1 calm breaches | **Yes, both assets, both horizons, large** — see below |

Width: v3 bands are 6–10% narrower at every level, with overall coverage
unchanged. v3 is at least as good as v1 in nearly every column, and sharper.

### How much to trust it

1. **It does not fix the calm problem.** v3 improves calm coverage by about
   3 points; a 90% band still covers about 79–80% in calm regimes.
2. **The improvement rests on very few episodes.** On non-overlapping calm
   windows (39 BTC, 34 ETH) v3 and v1 differ on **one window per asset**
   (BTC: v3 fixed 0 misses, created 1; ETH: fixed 1, created 0). The all-days
   statistic passes because overlapping days add resolution, but the
   independent events behind it are a handful.
3. **The real-data bootstrap interval was suspiciously tight.** D minus its
   lower bound was 1.7 (BTC) and 1.0 (ETH) pts, against 3.3–6.3 in every
   synthetic world. Percentile bootstraps are known to be over-confident
   when the effect sits in few clusters. The pass on (a) is less solid than
   "95%" implies.
4. **v2 achieved a similar calm gain and was not adopted.** v2 improved BTC
   calm coverage by +2.8 pts (on 2020-11+) under a stricter rule calibrated
   against a synthetic null. Part of the different verdicts is the different
   rules, not the models. Implied vol stays elevated relative to realised
   vol in calm periods, so v3 may largely capture the same mean reversion as
   v2 rather than genuine breakout information. Whether implied vol adds
   anything beyond mean reversion is untested (see limitations).
5. **v1's calm coverage depends on the period.** 75.8% / 77.3% here
   (2022-04+), 79.7% / 83.1% in the v2 test (2020-11+), 77–78% in v1's own
   run. Exact calm figures move by several points with the sample.

**Practical adoption:** v3 replaces v1 as the working corridor for BTC and
ETH, on the grounds that it is pre-registered-adopted, sharper and no worse
anywhere — **not** because it solves calm regimes. The calm caveat stands.
It requires a daily DVOL refresh (`collectors.dvol_backfill`, idempotent).

### H12 — the strongest signal, and why it is still only a lead

On calm days, split at the median DVOL/EWMA ratio, v1's 90% band was
breached far more often when implied vol sat high above realised vol:

| v1 breach rate, calm days | high ratio | low ratio | split at | ~indep. windows (hi/lo) |
|---|---|---|---|---|
| BTC 14d | **36.2%** | 12.2% | 1.26 | 15 / 24 |
| ETH 14d | **31.0%** | 14.4% | 1.19 | 14 / 20 |
| BTC 7d | **27.1%** | 11.0% | 1.27 | 33 / 40 |
| ETH 7d | **25.3%** | 8.4% | 1.19 | 34 / 38 |

Consistent in all four cells and far larger than the synthetic "perfect
information" world produced (median +3.8 pts). Reasons for caution:

- **Small counts.** At 14d, roughly 5 breaches against 3 in independent
  windows. The four cells are not independent (correlated assets,
  overlapping horizons).
- **Larger than "perfect knowledge" of a GARCH process** — striking, so
  distrusted. A plausible real mechanism exists (options price scheduled
  events and jump risk that a GARCH world lacks), but so does a confound:
  the lowest-EWMA days are where EWMA most under-estimates, and they also
  have the highest ratio because implied vol has a floor. The split may
  partly measure "how deep in calm" rather than option-market information.

**On 2026-10-07 both assets were in the LOW-ratio group** (BTC DVOL 37% vs
EWMA 36%; ETH 48% vs 45%), where v1's calm breach rate was 12–14% at 14d —
near nominal. If H12 holds, today's calm is not one the options market
expects to break. A lead, not a result.

### Today's corridor under v3 (2026-10-07)

| | 14d 80% | 14d 95% | 1-in-5 / 1-in-20 dip | 1-in-5 / 1-in-20 run |
|---|---|---|---|---|
| BTC $83,201 | $77,206–$90,576 | $71,655–$98,146 | −6.2% / −12.3% | +6.7% / +13.9% |
| ETH $2,565.50 | $2,303–$2,900 | $2,103–$3,159 | −9.2% / −17.1% | +10.0% / +20.3% |

### Limitations — recorded, not rerun

- **v3 vs v2 head to head** (does implied vol add anything beyond mean
  reversion?) and **H12 controlled for depth of calm** are new questions
  formed after seeing this result. They are for forward data, not for
  2022–2026.
- **Forward tracking is the real test** for v3, v2 and H12 alike: log each
  day's corridors and score them as windows complete.
- DVOL is a 30-day tenor; the 14d/7d mismatch is absorbed by FHS on
  average, not regime by regime.

---

## Fix 1: regime-conditional FHS (c1) — pre-registration

**Written 2026-10-08, before running on real data** (the coins' price
history had not yet been loaded). Frozen in
`scripts/test_vol_corridor_cfhs.py`. Results will be added underneath
without editing this section.

### The idea

Every model so far tried to *forecast* volatility better, and calm coverage
stayed at ~76–80%. Calm spells end in jumps that cannot be forecast — but
their **frequency** can be measured. Past windows that *started* calm already
contain both the jumps and the selection effect (a low vol estimate is
disproportionately an under-estimate). So c1 builds a calm day's band only
from past calm-start windows, and likewise for mid and storm.

c1 changes one thing relative to v1: which past windows enter the FHS pool.
The regime label at each past start uses only data up to that day: the
percentile of that day's EWMA vol among all EWMA values so far (calm
< 1/3, storm > 2/3, needs 365 values). Pools need ≥ 365 windows per regime.

### Why other coins

Conditional FHS was first written down as a limitation after v2's null on
BTC/ETH, so rule 12 bars testing it on BTC/ETH 2020–2026. The project's own
universe (`config/universe.py`) minus BTC/ETH gives **9 coins whose corridor
calibration has never been examined**: BNB, SOL, XRP, ADA, AVAX, LINK, DOT,
LTC, ATOM. The list comes from that file, not from a choice made here.
Prices: `taker_flow` spot daily closes from each coin's listing.

### Hypotheses and priors

| | prediction | prior |
|---|---|---|
| **H13** | pooled calm coverage closer to 90% under c1 | yes |
| **H14** | pooled storm coverage closer to 90% | yes |
| **H15** | unconditional calibration not materially worse | yes |
| **H16** | calm gain is broad, not one or two coins | yes |

### Decision rule (14d, 90%, pooled across the 9 coins)

c1 is **ADOPTED** only if all hold:

- **(a)** pooled calm improvement D has a one-sided 95% lower bound > 0 by
  **joint-time** block bootstrap (90-day calendar blocks shared by all
  coins, so cross-coin correlation is kept; 2,000 resamples);
- **(b)** pooled conditional calibration error is lower for c1;
- **(c)** c1 fails Kupiec in **at most 2 more** of the 27 coin × level cells
  than v1;
- **(d)** D > 0 in **at least 6 of 9** coins — added because v3's pass rested
  on a handful of episodes.

**Revision before real data.** (c) originally read "no more Kupiec failures
than v1". Synthetic calibration showed c1's per-regime pools, a third the
size, add per-coin noise even when c1 is the better model, and that
condition alone halved power (84% → 42.5%). The tolerance was set to +2,
which a correct c1 exceeded 11% of the time and a useless one never did.

### Calibrating the rule (synthetic, before real data)

The synthetic coin panels were 9 coins built to resemble the real ones:
- a common Student-t factor giving a correlation of about 0.6;
- their own GARCH(0.10, 0.85) each;
- staggered listing dates like the real universe.

| world | what c1 sees | result | rule passes |
|---|---|---|---|
| **null** | **random** regime labels (pure noise) | D −0.4 pts | **≤ 1.5%** — false positives |
| **alt** | real labels | calm 84.3% → 87.6%; storm 94.2% → 89.6% | **75%** — power |

**Caveat on the bootstrap.** Its lower bound sat below the true mean in 96%
of alt panels but only 82% of null panels. It ignores the noise of which
windows land in each pool, so the synthetic false-positive rate is the
number to trust, not the bootstrap alone. The rule's protection comes
mostly from requiring (a) **and** (d).

### Limitations on record

- **Survivors only.** Dead tokens are excluded, and their calm spells ended
  worst, so the test likely understates calm-period risk.
- **Not nine independent tests.** The coins move with BTC and share one
  calendar (2018–2026); the joint bootstrap accounts for that, while a
  per-coin count does not.
- **Shorter tests for newer coins.** SOL, DOT and AVAX were listed in 2020,
  and c1 needs about 4 years of history before it can forecast, so their
  tests are short.
- **BTC/ETH evidence for c1 must come from forward tracking**, as must any
  combination with v3's implied-vol scale.
- **No rescue:** ideas formed after the result are recorded, not run.

### Results (run 2026-10-08; `taker_flow` spot to 2026-09-30)

**Verdict: c1 ADOPTED** — all four conditions passed. Broad, consistent and
**small**: it closes about a quarter of the calm gap.

**PRIMARY: 14d, 90%, pooled over 9 coins:**

| | calm | mid | storm | CCE | overall | width |
|---|---|---|---|---|---|---|
| v1 | 80.6% | 92.8% | 95.9% | 6.0 | 89.8% | 55.1% |
| c1 | **82.9%** | 94.6% | **93.3%** | **5.0** | 90.2% | 54.3% |

| rule | result |
|---|---|
| (a) pooled calm improvement, joint-bootstrap lower bound > 0 | +2.2 pts, LB +1.4 — pass |
| (b) pooled CCE lower | 6.0 → 5.0 — pass |
| (c) Kupiec failures ≤ v1 + 2 | c1 1, v1 0 (of 27) — pass |
| (d) D > 0 in ≥ 6 of 9 coins | 8 of 9 — pass |

**Per coin (14d, calm coverage v1 → c1, D):** BNB 82.2 → 84.3 (+2.2) · SOL
78.8 → 79.1 (+0.3) · XRP 80.8 → 84.5 (+3.7) · ADA 80.6 → 82.0 (+1.3) · AVAX
78.9 → 78.0 (−1.0) · LINK 81.5 → 82.2 (+0.7) · DOT 77.2 → 79.2 (+2.1) · LTC
83.0 → 85.5 (+2.5) · ATOM 79.1 → 86.3 (+7.1).

**7d (secondary):** calm 82.5% → 84.8%, storm 95.2% → 93.0%, CCE 4.3 → 3.4;
D > 0 in **9 of 9** coins. Pooled non-overlapping calm windows: c1 fixed 18
v1 misses and created 3 (14d: 7 vs 2). The McNemar p-values (0.001 and 0.09)
are optimistic, because the coins are correlated.

### Hypothesis scorecard

| | prediction | result |
|---|---|---|
| **H13** | calm closer to 90% | **Yes — small.** +2.2 pts pooled; calm still 82.9% |
| **H14** | storm closer | **Yes:** 95.9% → 93.3% |
| **H15** | unconditional calibration not materially worse | **Yes:** overall 89.8% → 90.2%; Kupiec failures 0 → 1 of 27 |
| **H16** | gain is broad | **Yes:** 8/9 coins at 14d, 9/9 at 7d. Without ATOM (the largest, +7.1) the mean per-coin gain is +1.5 pts, positive in 7 of 8 |

### How much to trust it

- **Stronger than v3 on breadth.** v3's pass rested on about one
  independent window per asset. c1's gain shows up in 8–9 of 9 coins and at
  both horizons, and the pooled non-overlapping windows favour it 18 to 3 at
  7d. Correlated coins make that less than nine confirmations, but it is
  not a handful of episodes either.
- **Small in size.** It closed 24% of the calm gap (80.6% → 82.9% against
  90%). In the synthetic world it closed 58%. Real calm spells hold more
  surprise than their own history shows.
- **The mid regime got worse.** 92.8% → 94.6% at 90%, and 82.5% → 86.5% at
  80%. CCE still improved overall, but c1 moves error around rather than
  only removing it.
- **Survivors only.** Dead tokens are excluded, so calm-period risk is
  probably understated for every model.

### A likely reason the gain is small — recorded, not tested

c1 labels regimes in real time against each coin's whole history, including
the very volatile months after listing. The evaluation splits each coin's
test period into thirds. As alt-coin vol fell over the years, many days that
are "calm" in the evaluation split were "mid" in real time, so they were
calibrated against the wrong pool. Coverage judged by the **real-time
label** — the one a user actually sees, and the one `--moves` prints — was
not part of the pre-registration. It would be a descriptive check, not a
new verdict.

### What the four fixes add up to

| | calm 14d 90% (before → after) | adopted? |
|---|---|---|
| v2 mean-reverting vol (BTC/ETH, 2020+) | 79.7 → 82.5 / 83.1 → 84.1 | no |
| v3 implied vol (BTC/ETH, 2022+) | 75.8 → 78.8 / 77.3 → 79.9 | yes, narrowly |
| c1 calm-history calibration (9 coins) | 80.6 → 82.9 | yes |

**Each fix moves calm coverage up 1–3 points; none comes within 7 points
of 90%.** The calm gap is stubborn. The practical rule therefore stays:
**in a calm regime, use the 95% band when you need 90%.** Under c1 the 95%
band covered 89.0% of calm days at 14d and 91.1% at 7d (pooled, 9 coins).

### Use and limits

- c1 is the working method for the 9 alt coins.
- **BTC/ETH: c1 is unproven** (rule 12 kept it off their data), as is
  combining it with v3's implied-vol scale. Forward tracking only.

---

## Forward tracking (from 2026-10-08)

Every result above rests on a few dozen independent windows from a period
now examined many times. From 2026-10-08 the `corridor-logger` service
(`scripts/corridor_logger.py`, run from `docker-compose.yml`) records each
day's corridors **before** the outcome exists:

- **What is logged.** v1 and c1 for all 11 universe coins, plus v3 for BTC
  and ETH, at 7 and 14 days. Each forecast has its 80/90/95% bands, its
  1-in-5 / 1-in-20 dip and run, the real-time regime label (the one
  `--moves` prints), EWMA vol and the DVOL/EWMA ratio.
- **Write-once.** A forecast row is never revised (`ON CONFLICT DO
  NOTHING`) and carries `code_version`. Changing a model means a new
  version, reported separately.
- **Forward only.** The first cycle logs the latest completed day. Catch-up
  (up to 7 days) fills only gaps after that, and late rows are counted in
  the report.
- **Same models as tested.** The service's self-test checks that its levels
  equal those of `test_vol_corridor_cfhs` (v1, c1) and
  `test_vol_corridor_iv` (v3) for the same day, to 1e-12.
- **Scoring.** Each forecast is scored when its window closes:
  `corridor_outcomes` records the return, deepest dip and highest run, plus
  a hit or breach flag for each band, dip and run level.

### Evaluation plan, fixed now

- **First formal read at 12 months (2026-10 → 2027-10):** about 26
  independent 14-day windows per coin. It uses the rules already registered:
  - v3 vs v1 on BTC/ETH: the bootstrap rule;
  - c1 vs v1 on all 11 coins: the pooled four-condition rule, which gives
    BTC/ETH their first evidence for c1.
- **Primary forward question, as a user experiences it:** when the
  real-time label says CALM, how often do the 90% and 95% bands hold?
- **H12 forward split:** a DVOL/EWMA ratio of **1.2** (between the
  backtest medians 1.19–1.26), fixed now.
- **Power at 12 months is low,** so the first read is descriptive unless an
  effect is large. Decisions wait for 24 months.

`python -m scripts.corridor_logger --report` prints coverage so far, by
model, horizon and real-time regime.
