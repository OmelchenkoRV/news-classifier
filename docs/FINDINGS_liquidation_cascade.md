# Findings: Liquidation Cascade — No Amplification Found

**Status:** Negative result on the cascade hypothesis, with one useful
structural fact recovered. The model as designed is dead for two independent
reasons: (a) the data shows **no amplification non-linearity**, and (b) the
orderbook depth capture is **truncated** and cannot serve as a denominator.
A third, conceptual problem undermines the arithmetic regardless.

**Date:** 2026-08-22
**Data:** `eth_derivatives` + `eth_snapshots` + `eth_orderbook_depth`
(binance, 2026-04-25 → 2026-08-22, 33,513 snapshots at 5-min cadence)
**Query:** `scripts/q7_liquidation_cascade.sql`

---

## What was being tested

Unlike Q1–Q6 (all "does X *predict* a crash?", all failed), this asked a
**structural** question: given current positioning, how severe would a cascade
be *if* a move started? Intended as a **position-sizing** input, not a timing
signal — the first candidate that did not require prediction to be useful.

The cascade hypothesis: a price drop forces liquidations → forced selling
consumes bid depth → further price drop → more liquidation. If real, the
signature is **elasticity rising with move magnitude**.

---

## Finding 1 — no non-linearity. Deleveraging is proportional.

Elasticity = %ΔOI ÷ %Δprice, hourly, binance:

| move bucket | n hours | elasticity |
|---|---|---|
| **sharp down (≤ −2%)** | 6 | **0.92** |
| down 1–2% | 43 | 1.30 |
| down 0.25–1% | 479 | 1.21 |
| up 0.25–1% | 480 | 1.04 |
| up 1–2% | 44 | 1.03 |
| sharp up (≥ +2%) | 12 | 0.97 |

**The prediction fails.** Elasticity does not rise with magnitude — the
sharpest down bucket has the *lowest* down-move elasticity (0.92), below the
mild buckets. Tick-level (Q7a2) confirms scatter rather than structure: the
largest single drop (−3.45%, 2026-06-05 06:00) gave elasticity **0.85**, while
several ~1.5% moves gave 2.0–2.5.

**What IS real: a mild asymmetry.** Down moves ~1.2–1.3, up moves ~1.0. OI
unwinds modestly faster than it builds. That is proportional deleveraging with
a slight downside skew — **not self-amplification**.

This independently corroborates the June-2026 observation (no flash crash, a
steady grind), now across ~530 down-hours rather than one episode. **Crypto
deleveraging in this sample is roughly proportional, not explosive.** That is
the one durable fact this test produced, and it is mildly reassuring for
sizing.

---

## Finding 2 — the depth capture is truncated, so the denominator is invalid

Binance bid depth by distance from mid:

| pct_from_mid | avg depth (USD m) |
|---|---|
| 0.5% | 5.49 |
| 1% | 6.50 |
| **2%** | **6.57** |
| **5%** | **6.57** |

**Identical at 2% and 5%.** The book is not 6.57M deep at 5% — the collector
fetches a fixed number of levels and never reaches out that far. The figures
are top-of-book, truncated.

That makes the cascade multiplier meaningless: **$6.57M of measured "depth"
against $5.85B of open interest**. A 10% move at elasticity 1.2 implies ~$700M
of unwind against a $6.6M book → multiplier ≈ 107. That is not a finding, it is
a broken measurement.

Q7c is unusable for the same reason, and notably points the *wrong* way for the
alarming story: June-crash bid depth (6.83) was slightly **higher** than calm
periods (6.59) — almost certainly a truncation artifact, not real resilience.

---

## Finding 3 — the conceptual flaw (flagged late, applies regardless)

**OI falling ≠ one-directional selling.** When OI drops, longs *and* shorts
both close: a closing long sells, a closing short buys. Liquidations are
one-sided, but voluntary closes largely net out.

So "OI destroyed × price" was never a clean estimate of forced sell pressure.
This was under-stated when the approach was proposed. The **elasticity
measurement survives** this criticism (it measures position unwind, which is
what it claims); the **cascade arithmetic does not**.

---

## Verdict

The cascade model is closed. Three independent reasons — no amplification in
the data, an invalid depth denominator, and a flawed OI→flow link — and only
the first would need to be wrong for the others to still sink it.

**Recoverable if desired:** raising the orderbook collector's depth limit so
2% and 5% levels are genuinely populated would fix Finding 2, and the model
could be revisited in a few months. It would not fix Findings 1 or 3.
**Not recommended** — Finding 1 says there is no amplification to measure even
with a perfect denominator.

**Recorded as useful:** deleveraging is proportional (~1.2 down / ~1.0 up),
not explosive, in this sample. Crashes here are grinds, not cascades.

**Known limits:** 4 months, one exchange (okx captures no OI at all; bybit has
OI but no long/short), three episodes, and elasticity is an upper bound on the
forced component per Finding 3.

---

## Where this points

This was the first attempt at risk management that did not require prediction,
and the specific mechanism did not survive contact with the data. The approach
remains right; the instrument was wrong.

Next: **volatility targeting** — size the DIRECTIONAL book inversely to
trailing realised volatility. No forecast required, no new data required
(`price_snapshots` spans 2020-11 → present, 2,121 days), and it attacks the
problem that actually blocks the one real lead: the −73% drawdown that makes
the momentum basket untradeable (`FINDINGS_momentum.md`).
