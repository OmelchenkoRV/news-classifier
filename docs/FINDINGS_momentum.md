# Findings: Cross-Sectional Momentum Rotation

> ## ⚠ CENTRAL CLAIM UNRELIABLE (2026-08-22)
>
> This document's headline finding — that diversified momentum **beat
> buy-and-hold BTC on both total and risk-adjusted return** — is **not
> reliable**. The universe below consists entirely of tokens that survived to
> 2026.
>
> Adding known-dead tokens degrades the headline 30/5/7 config in every
> version of the test: **−74.9%** with 7 dead tokens, **−57.8%** with 9
> (including the recovered original Terra LUNA and UST series). 23–36 of 36
> configs degrade depending on the run.
>
> **But the magnitude is NOT identified.** Adding *more* dead tokens made the
> measured damage *smaller*, and several configs improved by over 300%,
> because dead tokens were often excellent holdings before they died and
> because enlarging the universe changes which names rank into the top-K at
> every rebalance. The confound is larger than the effect.
>
> **The honest statement: this result is materially inflated by survivorship;
> the true magnitude is unidentified with available data.** A real correction
> needs point-in-time universe membership, which free sources do not provide.
>
> **See `FINDINGS_survivorship.md`.** Read everything below as the
> survivor-only result it is.
>
> What still stands: the **bull-only → full-cycle sign flip** (K=1 winners
> collapsing out-of-sample) remains a genuine methodological finding. Also
> notable — the **cash floor worked**: momentum rotated out of LUNA before the
> May-2022 collapse because its trailing return turned negative, which is a
> real property of the strategy, not an artifact.

**Status:** ~~Strong, regime-robust result with serious caveats. A lead worth
keeping, not yet a tradeable strategy.~~ **Survivor-only result; central claim
unreliable, correction magnitude unidentified.** Documented and paused.

**Date of analysis:** 2026-06-03
**Backtest window:** 2020-11-01 → 2026-06-03 (2,041 daily observations)

> **WINDOW UPDATE (2026-08-22).** All figures below are as of the 2026-06-03
> cutoff. Re-running the same 30/5/7 config on data through **2026-08-22**
> (2,121 days) gives **18.91× / CAGR 67.1% / ret-vol 0.90 / maxDD −73.5%**
> — the August rally lifted the terminal value. The conclusions are unchanged
> (the drawdown is marginally *worse*), but **do not quote the 14.7× / 0.83
> figures alongside post-June results without stating the window.** Later
> documents — `FINDINGS_voltarget.md` in particular — use the 2026-08-22
> baseline.
**Universe:** 11 liquid Binance USDT majors (BTC, ETH, BNB, SOL, XRP, ADA,
AVAX, LINK, DOT, LTC, ATOM)
**Reproduce:** `python -m scripts.backtest_momentum`

---

## Background: how this came about

This project began as a news-classifier testing whether headlines predict
crypto returns. After detrending, that hypothesis returned a clean null
(~49% directional accuracy, coin-flip) that held across a regime flip, and a
follow-on attempt to route headlines to news-sensitive assets (oil, gold) was
under-powered and inconclusive. The conclusion was that *news reaction* is not
a capturable edge at the latencies and granularity available here.

That null prompted a different, better-supported hypothesis for the actual
goal (managing a crypto portfolio): does *price itself* — cross-sectional
relative strength — predict which tokens to hold? This document records the
result of testing that.

---

## The strategy tested

At each rebalance date:

1. Rank the 11-token universe by trailing N-day return ("momentum").
2. Hold the top K, equally weighted.
3. **Cash floor:** a token is eligible only if its momentum is positive. If
   fewer than K tokens are positive, the remainder sits in cash (0% return).
   In a broad decline the book goes fully to cash.
4. Hold H days, then rebalance. Turnover is charged a transaction cost.

A 36-cell grid was swept over N ∈ {14, 30, 60, 90}, K ∈ {1, 3, 5},
H ∈ {7, 14, 30} days, at two cost assumptions (10 bps and 25 bps per unit
turnover). The full grid is always reported — no cherry-picking the best cell.

Benchmarks: buy-and-hold BTC (the strict bar), equal-weight-all-11, and
risk-adjusted return (CAGR ÷ annualised volatility) for every config.

---

## Headline result

Over the full cycle (which **includes the 2022 bear market**: the 2021 top,
LUNA, Celsius, 3AC, and FTX), a **diversified** momentum basket beat
buy-and-hold BTC on both total and risk-adjusted return:

| Config (N/K/H) | Final | CAGR | Vol | Ret/Vol | Max DD | vs BTC |
|---|---|---|---|---|---|---|
| **30 / 5 / 7** | **14.7×** | 63.0% | 75.9% | **0.83** | −72.9% | beats on both |
| 30 / 3 / 7 | 14.2× | 62.0% | 80.0% | 0.77 | −71.5% | beats on both |
| 30 / 5 / 30 | 12.6× | 58.4% | 80.6% | 0.73 | −70.4% | beats on both |
| *hold BTC* | *4.8×* | *32.4%* | *58.5%* | *0.55* | *−76.6%* | *benchmark* |
| *equal-weight* | *6.1×* | *38.4%* | *76.8%* | *0.50* | *−80.8%* | *benchmark* |

At 10 bps, **17 of 36** configs beat BTC on total return and **6 of 36** on a
risk-adjusted basis; at 25 bps, 14 of 36 and 6 of 36. The result is not
fragile to the cost assumption.

---

## Why this result is more credible than the first version

An earlier backtest on a **bull-only** window (Jun 2023 → Jun 2026) produced a
*different* set of winners: the top cells were all **K=1** (hold a single
token), led by N=90/K=1 at 7.25×. That looked spectacular and was almost
certainly **overfit** — a concentrated bet on whichever token had run hardest,
in a window with no sustained downturn to punish concentration.

Extending the data back through the 2022 bear **inverted that finding**:

- The bull-only K=1 winners **collapsed**. Across the full cycle, N=14/K=1/H=30
  lost 75% (0.25×); several other K=1 configs lost money outright. Concentrated
  single-token momentum got destroyed by the bear.
- The full-cycle winners migrated to the **diversified** corner (K=3–5), which
  is what a *real* effect should look like — robustness to holding more names,
  not dependence on one lucky pick.

This sign-flip is the most important thing in the analysis. It is direct
evidence that **the bull-only result was a regime artifact**, and that testing
across a full cycle is what separated a mirage from a signal. The diversified
momentum effect surviving its worst historical regime is the strongest
positive finding this project produced.

---

## The serious caveats (why this is a lead, not a strategy)

1. **Catastrophic drawdowns.** Even the best config drew down **−72.9%** at the
   2022 bottom. Several winning configs hit −83% to −91%. A −73% drawdown is,
   in practice, almost impossible to sit through — most holders capitulate near
   the bottom, converting a paper loss into a realised one. "Beats BTC in a
   backtest" and "tradeable by a human" are different claims, and this gap is
   the main thing standing between the two.

2. **The cash floor under-delivered.** It was meant to cut the bear drawdown by
   rotating to cash. It barely did: −72.9% vs BTC's −76.6% for the best config,
   and *worse* than BTC for several others. The reason is structural —
   momentum lags. By the time trailing return turns negative enough to trigger
   the floor, much of the crash has already happened. The floor protects
   against slow, sustained declines, not fast initial legs down. This is a
   measured limitation, not a tuning problem.

3. **One bear market is N=1.** Momentum has worked across many asset classes
   and decades in the academic literature, which gives prior reason to believe
   it. But this validation rests on exactly **one** crypto cycle. "Survived
   2022" is not "robust across all regimes."

4. **Backtest optimism.** Three known upward biases remain: (a) **survivorship**
   — all 11 tokens survived to today; the LUNAs and FTTs that went to zero are
   absent, inflating returns; (b) **fills** — close-to-close, no intraday
   slippage on rebalance; (c) **behavioural** — assumes mechanical execution of
   every rotation straight through a −73% drawdown without flinching. Real
   returns would be materially lower than the backtest.

---

## Honest conclusion

There is a **real, regime-robust, diversified cross-sectional momentum effect**
in this universe: a 30-day-lookback, top-3-to-5, weekly-to-monthly-rebalanced
basket beat buy-and-hold BTC across a full cycle on both total and
risk-adjusted return, and the result survived the test that killed the
bull-only version. That is a genuine finding and the strongest this project has
produced.

It is **not yet a strategy you could trade**, primarily because of drawdowns in
the −70% range that the cash floor does not adequately tame, plus the standard
backtest-optimism gap. The single most valuable next experiment — explicitly
deferred here — is whether a **faster risk-off signal** (news-based and/or
volatility/drawdown-based) can exit ahead of the crashes that trailing-return
momentum is too slow to dodge. That is a sharply-defined, falsifiable question,
and both the momentum engine and a news-event detector now exist to test it.

> **OUTCOME OF THAT DEFERRED EXPERIMENT (2026-08-22).** It was run, and the
> answer was no. **Seven** signal candidates were tested for the DEFENSIVE
> transition — news-return, price vol/drawdown breaker, news-systemic, ETF
> flows, funding level, funding momentum, liquidation cascade — and none
> produced a usable crash-leading signal. See the `FINDINGS_*.md` series.
>
> What *did* work was abandoning prediction entirely: **volatility targeting**
> cuts this drawdown from −73.5% to ~−49% mechanically, with no forecast
> (`FINDINGS_voltarget.md`). It does not improve risk-adjusted return — ret/vol
> stays ~0.90 — so it is a risk *rescaling*, not an edge. The gap between
> "beats BTC in a backtest" and "tradeable by a human" is narrowed, not closed.

For now: the finding is recorded, the methodology (honest grid, full-cycle data,
no-lookahead, reported drawdowns, stated biases) is the point, and the work is
paused at a clean stopping point rather than pushed into over-fitting.
