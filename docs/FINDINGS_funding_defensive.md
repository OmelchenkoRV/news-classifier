# Findings: Funding-Rate Defensive Signal — Promising, Not Validated

**Status:** **Partial replication; closed for defensive use.** F2 produced the
project's first contiguous winning region on BTC, and the decisive era
reproduced on ETH (+17.3pp, n=33). But the largest-n era **failed** to
replicate (BTC +13.2 vs ETH −3.3 on ~120 firings each), and the effect is
confined to the 10% drawdown threshold — it **inverts at 20%**. The signal
flags routine volatility, not crashes, which is the wrong instrument for the
DEFENSIVE state. Not a null; not a validation; not fit for purpose.

**Date:** 2026-08-22
**Data:** `funding_history` — BTCUSDT 7,615 rows (2019-09-10 → 2026-08-22),
ETHUSDT 7,381 rows. 2022 crash window: 642 rows. 2026 crash window: 184 rows.
**Queries:** `scripts/q5_funding_false_positive.sql`
**Collector:** `collectors/funding_backfill.py`

Why this test is better-powered than everything before it: **two bear markets**
(2022 and 2026) instead of one cycle. Every prior candidate was limited by data
availability; this one is not.

---

## Correction carried in from the previous note

`FINDINGS_aug2026_rally.md` claimed Binance funding **saturates** at a
±0.01%/8h cap (`0.0001`), and used that to argue for the momentum variant.
The backfill disproves it:

| symbol | min | max | exactly 0.0001 | above 0.0001 | negative |
|---|---|---|---|---|---|
| BTCUSDT | −0.003 | +0.003 | 2,693 | 1,069 | 1,093 |
| ETHUSDT | −0.00356 | +0.00375 | 2,563 | 1,226 | 1,014 |

Funding spans **±0.003 — 30× the supposed cap**. `0.0001` is the resting
**interest-rate component**: where funding settles when the premium index is
near zero. 52% of observations sitting there means *neutral is the common
state*, not *pinned at an extreme*.

The F2-over-F1 recommendation survived, but the stated reason was wrong.
**Lesson recorded: a metric's observed mode is not evidence of a bound.**

---

## F1 (level) — worse than random, coherently so

Signal: funding negative three days running (longs capitulating).

| horizon / drawdown | F1 hit % | baseline % | edge |
|---|---|---|---|
| 7d / 10% | 2.7 | 10.1 | **−7.4** |
| 14d / 10% | 10.8 | 19.8 | **−9.0** |
| 21d / 10% | 13.5 | 27.6 | **−14.1** |
| 30d / 10% | 24.3 | 34.7 | **−10.4** |
| 30d / 15% | 9.5 | 21.7 | **−12.2** |

Below baseline in **every cell**, n=74. Not noise — a coherent inversion:
funding goes negative *after* capitulation, so F1 fires near local bottoms.

**As a defensive trigger it is backwards.** As a contrarian bottom-marker it
may have value — a separate hypothesis, not tested here, and one that would
need its own pre-registration to mean anything.

Confirms the June-2026 observation (funding flipped negative only on the
capitulation day) across seven years.

*Query bug, cosmetic:* F1's three `base_win` rows are identical because F1
never uses `base_win`; the CROSS JOIN replicated it. No effect on results.

---

## F2 (momentum) — the first contiguous region this project has produced

Signal: daily funding ≤ (trailing baseline − 0.0001), i.e. a sharp drop
relative to its own recent level. Edge over baseline, in percentage points:

| fwd / thresh | win=7 (n=127) | win=14 (n=157) | win=30 (n=193) |
|---|---|---|---|
| 7d / 10% | +16.7 | +12.2 | +6.5 |
| 14d / 10% | **+20.4** | +15.2 | +12.8 |
| 21d / 10% | **+20.4** | +16.3 | +13.9 |
| 30d / 10% | **+24.4** | +19.4 | +17.6 |
| 30d / 15% | +12.2 | +12.7 | +10.9 |
| 30d / 20% | +9.6 | +10.8 | +7.2 |

- Positive in **32 of 36 cells**
- **Contiguous** across the whole 10% column and most of 15%
- **Monotone in window**: tighter baseline → stronger edge (a structural
  gradient, not a lucky cell)
- Well-powered: 127–193 firings per cell

This is categorically different from the ETF-flow result, whose two "best"
cells were isolated, non-monotone, and rested on n=11.

---

## The era split — the test that killed the flow signal

Prediction on record before running: *the aggregate edge would collapse into
the 2022 bear, when drawdowns were common for everyone.* **That prediction was
wrong.**

| era | n_fired | hit % | era baseline % | edge | rough significance |
|---|---|---|---|---|---|
| 2019–2022 | 123 | 37.4 | 31.5 | **+5.9** | 46 vs 38.7 expected ≈ 1.4 sd |
| 2023–2025H1 | 32 | 28.1 | 11.4 | **+16.7** | 9 vs 3.6 expected ≈ 2.9 sd |
| 2025H2–2026 | 2 | 0 | 16.0 | — | n too small |

The bear era contributes **almost nothing** (1.4 sd — not significant). The
edge lives in the **calm** era, which is the opposite of a
drawdowns-were-common artifact and the opposite of regime-fitting to 2022.

Era baselines differ enormously (31.5% / 11.4% / 16.0%), which is exactly why
the single pooled baseline in Q5b could not have detected this. **Any future
test must use era-specific baselines.**

---

## Why this is still NOT a finding

1. **Multiple comparisons.** 36 cells × 3 eras were swept. A nominal ~2.9 sd
   in one era, identified *after* seeing the grid, is worth far less than it
   appears. Properly corrected it is marginal at best.

2. **n=32 carries the result.** Nine hits against 3.6 expected. Five fewer
   hits and the edge halves. The whole conclusion rests on a handful of events.

3. **It has stopped firing.** Two firings in 14 months, zero hits. Whatever
   regime produced the signal is not the current one — so even if real, it is
   not actionable today.

4. **Threshold is arbitrary.** The `−0.0001` drop was chosen because it
   equalled the value mistakenly believed to be the cap. It happens to be
   ~1/30th of the observed range. It was never optimised, which is *good*
   (no fitting) but also means it is unmotivated.

5. **Drawdown ≠ tradeable.** A 10% drawdown *somewhere* within 30 days is not
   an exit rule. Converting this to a DEFENSIVE trigger requires an overlay
   backtest with costs, hysteresis, and whipsaw accounting — where the
   price-breaker died.

---

## The ETH replication (Q6) — the test that decided it

`scripts/q6_eth_replication.sql`. Parameters **frozen**, no re-tuning.

### Independence was better than expected

| BTC fires | ETH fires | same day | % of ETH overlapping | within ±2 days |
|---|---|---|---|---|
| 206 | 211 | 119 | **56.4%** | 155 (73.5%) |

~26% of ETH firings are genuinely distinct events, and even overlapping
firings are scored against a *different price path*. Weak-to-moderate
independence — enough that agreement is not automatic, and enough that
**disagreement is meaningful**.

### Headline cell (base_win=7, 14d, 10%), era by era

| era | BTC edge | ETH edge |
|---|---|---|
| 2019–2022 | +13.2 (n=103) | **−3.3** (n=119) |
| 2023–2025H1 | **+10.3** (n=23) | **+17.3** (n=33) |
| 2025H2–2026 | 0 (n=1) | −3.1 (n=4) |

**The decisive era replicated.** 2023–2025H1 — the era carrying the entire BTC
result — reproduced on ETH at +17.3pp on 33 firings (13 hits vs 7.3 expected,
≈2.4 sd). That was the pre-stated pass condition, and it passed.

**But the largest-n era did not.** 2019–2022: BTC +13.2, ETH −3.3, on ~120
firings each. Despite overlapping firing dates, outcomes diverge. The
best-powered era contradicts.

### The threshold structure is the real problem

ETH, 2023–2025H1, edge in pp across all windows and horizons:

| base_win | 10% column | 15% column | 20% column |
|---|---|---|---|
| 7 | +6.6, +17.3, +10.9, +14.4 | +1.9, −3.1, +8.2 | −3.9, −6.5, −6.4 |
| 14 | +8.4, +12.2, +11.4, +25.6 | −0.6, −4.5, −3.8, +3.6 | −1.1, −3.9, −6.5, −6.8 |
| 30 | +5.6, +10.7, +9.9, +15.1 | −3.5, −6.8, −8.3, −6.6 | −1.1, −3.9, −6.5, −9.1 |

**All 12 of the 10% cells positive. Nearly every 20% cell negative.**

The effect is confined to the shallowest threshold and **inverts** at the
deepest. That is coherent — and fatal for the intended use. The signal appears
to flag **routine volatility**, not crashes.

---

## Verdict: closed for defensive use

The DEFENSIVE state exists to avoid the −73% drawdowns that make the momentum
lead untradeable (`FINDINGS_momentum.md`). A signal that predicts 10% dips
while **anti-predicting** 20% falls is the wrong instrument for that job —
arguably worse than nothing, since it would rotate to cash during noise and
leave the book exposed during the events that actually matter.

What honestly survives, stated narrowly: *funding-momentum drops weakly
predict ~10% drawdowns in calm regimes, on one asset, in one era, from a rule
that has fired 5 times in the last 14 months.*

Three independent reasons not to carry this forward as a defensive trigger:

1. The largest-n era fails to replicate across assets.
2. The effect does not generalise across thresholds, and reverses at the
   magnitudes that matter.
3. It has effectively stopped firing, so it is not actionable regardless.

**This closes the leverage family for defensive purposes.** Funding was the
best-powered free candidate available — two bear markets, seven years, clean
numeric data, no data-availability wall. It was given the strongest test this
project can run and did not survive it.

### What the accumulated record now says

Seven candidates, one lead. Every attempt to *predict* the DEFENSIVE
transition has failed: news-return, price vol/drawdown breaker, news-systemic,
ETF flows, funding level (inverted), funding momentum (wrong threshold band),
and positioning (predicts fragility, not direction).

That consistency is itself the finding. The reasonable inference is not "keep
trying candidate #8" but that **reliable crash-leading signals are not
extractable from freely available data** — which is unsurprising once stated
plainly, and consistent with this project's own sourcing principle: nobody
shares a working signal, and that cuts against one's own search too.

The remaining honest direction is risk management that **requires no
forecast** — volatility targeting, permanent partial cash, exposure caps on
the momentum basket. These attack the same drawdown problem from a direction
that does not depend on a predictive signal existing. All are testable with
data already in hand.
