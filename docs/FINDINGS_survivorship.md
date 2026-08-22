# Findings: Survivorship Bias — The Momentum Lead Does Not Survive

**Status:** Survivorship bias in the momentum result is **real, large, and
mechanistically confirmed** — but its **precise magnitude is NOT identified**
by this design. Two runs, with different dead-token sets, gave −74.9% and
−57.8% on the headline config. Adding *more* dead tokens made the measured
damage *smaller*, and several configs **improved**. The confound (changing
universe composition) is now larger than the effect being measured.

**What is solid:** the headline degrades in every version of the test, 23–24 of
36 configs degrade, and the trade log shows exactly the mechanism. **What is
not solid:** any specific number.

**Date:** 2026-08-22
**Script:** `scripts/backtest_survivorship.py`
**Archive collector:** `collectors/binance_archive.py`

---

## The bias

The 11-token universe consists entirely of tokens that **survived to 2026**.
The universe that actually existed in 2021 included LUNA, FTT and others that
went to zero. Momentum rotation is *especially* exposed: it buys whatever ran
hardest, which is precisely the profile of tokens that later collapsed.

Every headline number in this project inherited this bias. It had never been
measured.

---

## A bug caught first (worth recording)

The initial run reported a −85% penalty. It was wrong. `build_delist_map`
compared each symbol's last date against the **panel** end, and the Docker
`price-backfill` loop only refreshes BTCUSDT/ETHUSDT (`SYMBOLS = ["BTCUSDT",
"ETHUSDT"]`). Nine *living* tokens (ADA, SOL, XRP, BNB, DOT, LINK, LTC, ATOM,
AVAX) were stale at 2026-06-03 and were therefore treated as **delisted and
charged −100%**.

Fixed by refreshing the universe (`price_backfill --universe`). The delist map
now contains only genuinely dead names.

Same class of error as the 746× price-breaker: a dramatic result produced by a
mechanical fault rather than by the world. **The instinct to distrust a
striking number applies to bad news as much as to good.**

---

## Run 1 — 7 dead tokens (live-API relistings only)

| config | survivors-only | + dead | Δ | surv ret/vol | ext ret/vol |
|---|---|---|---|---|---|
| **30/5/7 (headline)** | 15.38× | **3.87×** | **−74.9%** | 0.84 | 0.33 |
| 30/3/7 | 17.53× | 4.43× | −74.7% | 0.84 | 0.34 |
| *hold BTC* | *5.60×* | — | — | *0.60* | — |

32/36 configs degraded. This run was **missing the two defining collapses of
the cycle**: LUNC data starts 2022-09 and USTC starts 2023-03 — both *after*
their respective deaths — because the live REST API only serves relisted
tickers.

---

## Recovering the missing series (the free route worked)

`collectors/binance_archive.py` pulls from `data.binance.vision`, which retains
monthly kline dumps for **delisted** pairs the REST API no longer serves:

| symbol | recovered | span |
|---|---|---|
| `LUNAUSDT_DEAD` | **560 rows, 19/19 months** | 2020-11-01 → **2022-05-31** |
| `USTUSDT_DEAD` | 141 rows | 2021-12-24 → **2022-05-13** |

May-2022 returns 14 and 13 rows respectively — the archive records trading
stopping mid-month, exactly where the tokens died.

**Ticker-reuse handling:** Binance recycled `LUNAUSDT` for Terra 2.0 from
mid-2022. The original series is cut at 2022-05 and stored under an alias, so
it can never be joined to the modern token. Splicing would fabricate a
99.9% collapse followed by a recovery — making survivorship look *better* than
reality.

**Free cross-validation:** FTT, SRM and WAVES inserted **0 new rows** — the
archive agreed with the previously-collected live-API data on every overlapping
bar.

---

## Run 2 — 9 dead tokens (including the real Terra collapse)

| config | survivors-only | + dead | Δ | surv ret/vol | ext ret/vol |
|---|---|---|---|---|---|
| **30/5/7 (headline)** | 15.39× | **6.49×** | **−57.8%** | 0.84 | 0.46 |
| 30/3/7 | 17.53× | 9.12× | −48.0% | 0.84 | 0.52 |
| 30/3/14 | 5.21× | 21.57× | **+314.0%** | 0.43 | 0.80 |
| 30/5/14 | 6.93× | 18.56× | **+167.7%** | 0.55 | 0.81 |
| 14/1/7 (loss=0) | 0.79× | 5.96× | **+658.2%** | −0.04 | 0.29 |

23–24/36 degraded — **but the penalty SHRANK** (−74.9% → −57.8%) after adding
the two most catastrophic tokens in crypto history, and several configs
**improved substantially**.

---

## The trade log: mechanism confirmed

86 entries into doomed tokens at 30/5/7. LUNA was bought **seven times**:

| entry | price | hold return |
|---|---|---|
| 2020-12-03 | 0.50 | −5.5% |
| 2021-01-28 | 1.45 | **+83.5%** |
| 2021-09-30 | 38.83 | +20.0% |
| 2021-12-02 | 66.28 | +0.2% |
| **2022-03-03** | **90.52** | **+11.8%** |

Plus UST bought twice (2022-03-10, 2022-04-21) weeks before the de-peg, and FTT
repeatedly through 2021–22. **This is the trap made concrete**: a trailing-return
ranker loading into the cycle's best performers, which were disproportionately
the tokens that later died.

### But the last LUNA entry is 2022-03-03 — the collapse was 2022-05-09

There is **no entry in April or early May**. The strategy rotated out before
the death, because the positive-momentum **cash floor** made LUNA ineligible
once its trailing 30-day return turned negative during the March–April
drawdown.

That is a genuine property of the strategy, not an artifact — and it means the
LUNA collapse contributes **almost nothing** to the penalty. This directly
contradicts the prediction made before the run ("expect the survivorship
penalty to get worse; LUNA is the canonical momentum trap"). **The prediction
was wrong, and the cash floor is the reason.**

---

## Why the magnitude is NOT identified

Adding nine tokens to an eleven-token universe changes **which names rank into
the top-K at every rebalance**. That is no longer a survivorship correction —
it is a *different, larger universe*.

The dead tokens were frequently **excellent holdings before they died**:
WAVES +92.3%, SRM +73.7%, LUNA +83.5%. Including them adds their run-ups as
well as their deaths, and in several configs the run-ups dominate — hence
+314%.

Two further signs the design is confounded:

- **30/5/7 is identical (6.49×) under `--delist-loss 1.0` and `0.0`**, while
  14/1/7 swings from 0.00× to 5.96×. The delisting convention matters
  enormously in some cells and not at all in others.
- The dead fraction is **9 dead against 11 survivors (45%)** — far above real
  mortality. A proper universe would add many more survivors too.

**The confound is now larger than the effect.**

---

## Honest conclusion

**Solid:** survivorship bias materially inflates the momentum result. The
headline degrades in both runs (−74.9%, −57.8%), most configs degrade in both,
and the trade log demonstrates the exact mechanism — momentum did buy the
doomed tokens, repeatedly, near their tops.

**Not solid:** any specific magnitude. The two runs disagree by 17 percentage
points, the direction reverses in several cells, and the confound is
structural rather than fixable by adding more names.

**Therefore:** `FINDINGS_momentum.md`'s central claim is **not reliable**, but
neither is any corrected figure produced here. The correct statement is *"the
momentum result is materially inflated by survivorship; the true magnitude is
unidentified with available data."*

### What a real correction needs

**Point-in-time universe membership** — at each rebalance date, rank only the
tokens that were actually in the liquid top-N *on that date*, including ones
that later died and excluding ones not yet listed. That requires historical
listing/liquidity snapshots, which free sources do not provide.

This is the one data purchase with a genuine case behind it: unlike paid
positioning history (which would test a hypothesis the evidence already
argues against), it fixes a **known, measured defect** affecting every result
in the project.

**Stop adding dead tokens.** Each addition makes the universe less
representative, not more.


---

## Known discrepancy in absolute figures

This script reports survivors-only 30/5/7 at **15.38×**, while
`backtest_voltarget.py` reports **18.91×** on the same data. Cause:
`run_strategy` here divides by the **original** basket size, so a not-yet-listed
token dilutes the average, whereas `backtest_momentum.run_strategy` uses
`dropna().mean()`.

The within-script survivors-vs-extended comparison is apples-to-apples and the
Δ column is valid. **These absolutes are not directly comparable to the
published figures elsewhere.**

---

## What this does to the project

`FINDINGS_momentum.md` called diversified momentum "the strongest positive
finding this project produced" and "a lead worth keeping." That assessment
rested on an unmeasured bias which, once measured, removes the comparison it
was built on.

What survives:

1. **The bull-only → full-cycle sign flip is still real.** Concentrated K=1
   winners collapsing out-of-sample was a genuine methodological finding, and
   this result reinforces it.
2. **Volatility targeting is unaffected as a *mechanism*.** It requires no
   forecast and cuts drawdown mechanically. But it was measured on the biased
   basket, so `FINDINGS_voltarget.md`'s absolute figures inherit this bias —
   the overlay's *relative* effect should hold, the levels should not.
3. **The methodology.** Eight honest nulls, one bug caught in each direction,
   and now a self-inflicted correction that removes the project's headline
   claim. That record is the actual asset.

**Standing:** the project has one demonstrated mechanism (vol targeting), no
demonstrated alpha, and a much better-calibrated view of what its numbers were
worth.
