# Findings: Volatility Targeting — Risk Rescaling Works, Alpha Does Not

**Status:** **Substantially withdrawn.** Rerun on the survivorship-corrected
universe, the overlay's apparent benefit does not survive. **0 of 18 configs
improve risk-adjusted return** (3/18 did on the inflated basket), and the whole
construction — momentum base + vol-target + yield leg — is **dominated by
simply holding BTC** on both total and risk-adjusted return.

What survives: vol targeting still cuts drawdown (15/18 configs) because that
is a mechanical property. What does not: any claim that it *improves* the
strategy. It compresses risk on any return stream; it cannot rescue a poor one.

**A prediction failed here and is recorded below.** Before the rerun the
expectation was stated that "the overlay's relative effect should hold, since
vol targeting is a mechanical transform." It did not hold in the way that
mattered.

**Date:** 2026-08-22
**Window:** 2020-11-01 → 2026-08-22 (2,121 days, 11 symbols)
**Script:** `scripts/backtest_voltarget.py`
**Base:** momentum 30/5/7, FIXED (the overlay is what is being tested)

---

## Why this approach was reached

Seven candidates tried to **detect** when to go DEFENSIVE. All failed:
news-return, price vol/drawdown breaker, news-systemic (data wall), ETF flows
(below baseline in 22/36 cells), funding level (inverted), funding momentum
(right band, wrong thresholds), liquidation cascade (no amplification exists).

Volatility targeting requires **no forecast**. It sizes inversely to *trailing
realised* vol, which is observable. Crypto vol clusters, so exposure falls
after turbulence begins — without ever calling a top.

    exposure_t = clip(target_vol / realised_vol(through t-1), 0, max_exposure)

No-lookahead is enforced by construction (`_trailing_vol()` shifts by one day)
and asserted in `--self-test` against a manual calculation. This is the bug
class that produced the bogus 746× price-breaker result.

---

## Results (10 bps; 25 bps in brackets where it differs materially)

| config | final | CAGR | vol | ret/vol | maxDD | avgExp |
|---|---|---|---|---|---|---|
| *hold BTC* | *5.60* | *34.5%* | *57.9%* | *0.60* | *−76.6%* | — |
| **momentum 30/5/7 (base)** | **18.91** | **67.1%** | **74.8%** | **0.90** | **−73.5%** | 1.00 |
| 30%/14d/1.0x | 5.02 | 32.6% | 34.5% | 0.94 | **−48.8%** | 0.58 |
| 30%/14d/1.5x | 7.03 | 40.6% | 37.4% | **1.09** | **−48.8%** | 0.65 |
| 30%/30d/1.0x | 3.99 | 27.3% | 33.5% | 0.82 | −50.3% | 0.54 |
| 30%/30d/1.5x | 4.73 | 31.2% | 35.1% | 0.89 | −49.5% | 0.57 |
| 30%/60d/1.0x | 3.89 | 26.8% | 32.3% | 0.83 | −49.1% | 0.50 |
| 30%/60d/1.5x | 4.33 | 29.2% | 32.9% | 0.89 | −49.1% | 0.51 |
| 50%/60d/1.0x | 6.21 | 37.6% | 49.4% | 0.76 | −61.7% | 0.75 |
| 70%/14d/1.5x | 7.06 | 40.7% | 71.4% | 0.57 | −80.8% | 1.14 |

15 of 18 configs cut drawdown vs base; **2 of 18** improved ret/vol (1 of 18
at 25 bps).

### The drawdown effect is mechanical and robust

| target vol | maxDD range | realised vol | avg exposure |
|---|---|---|---|
| 30% | −48.8% … −50.3% | 33–37% | 0.50–0.65 |
| 50% | −61.7% … −70.0% | 49–56% | 0.75–0.93 |
| 70% | −69.3% … −80.8% | 60–71% | 0.88–1.14 |

**Contiguous across all six 30%-target cells, monotone in target vol, stable
across lookback {14, 30, 60}, and essentially unchanged at 25 bps.** That is
the signature of a mechanical effect rather than a fitted one — appropriate,
since nothing here forecasts anything.

### But ret/vol is a wash

Base 0.90. The 30% block: 0.94, 1.09, 0.82, 0.89, 0.83, 0.89 — centred on
0.90. At 25 bps: base 0.86 vs 0.85, 0.97, 0.75, 0.82, 0.78, 0.84.

Vol falls 74.8% → ~33%; CAGR falls 67% → 27–40%; the ratio does not move.
**The drawdown was bought with returns at roughly fair exchange.** This was
predicted in advance and is the honest reading: no free lunch, and none was
expected.

---

## What this actually buys

−49% vs −73.5% is the difference between "very hard to sit through" and
"almost impossible." `FINDINGS_momentum.md` named the −73% drawdown as the
single thing standing between a backtest result and a tradeable one. This
narrows that gap materially without requiring any signal to exist.

It does **not** make the strategy better in risk-adjusted terms. Anyone
claiming vol targeting adds alpha here would be misreading the grid.

### Two observations worth carrying forward

**1. The 1.5× column beats 1.0× at the 30% target with no drawdown penalty**
(1.09 vs 0.94, 0.89 vs 0.82, 0.89 vs 0.83; maxDD identical). That is levering
up when vol is *low*. The gradient is consistent enough to investigate — but
it **inverts at the 70% target** (−80.8%, worse than base), so it is
regime-dependent, not a general result. Do not generalise it.

**2. Average exposure 0.50–0.65 means a third to half of capital sits idle.**
That is precisely what the YIELD state is for — and **vol targeting produces
the DIRECTIONAL/YIELD split mechanically, with no prediction**, which is what
seven failed candidates were trying to achieve by forecasting.

---

## The YIELD leg (added 2026-08-22)

Idle capital is lent at `--lend-apy`; when exposure exceeds 1 the book is
**levered**, so there is no idle capital and the excess is charged
`--borrow-apy` (default 10%, above the lend rate, as in reality). Without that
asymmetry the 1.5× configs would collect leverage *and* carry for free.

### Mechanics verified

`yieldAdd` scales with idle capital, as it must:

| target | avgIdle | yieldAdd @5% | yieldAdd @10% |
|---|---|---|---|
| 30% | 0.42–0.50 | +2.0 … +3.2pp | +5.0 … +6.5pp |
| 50% | 0.22–0.25 | +0.1 … +1.8pp | +1.0 … +3.5pp |
| 70% | 0.11–0.12 | +0.8 … **−2.6pp** | +1.6 … **−1.9pp** |

The **negative** entries are the borrow leg biting: every 70%/1.5× config runs
avgExp > 1, so it pays rather than earns. Running with
`--lend-apy 0 --borrow-apy 0` reproduces the pre-yield numbers exactly,
confirming nothing else drifted.

### The result: ret/vol now genuinely improves

At 5% APY the six 30%-target configs give ret/vol **1.03, 1.14, 0.90, 0.96,
0.93, 0.98** against base **0.90**. At 10% APY: **1.11, 1.22, 1.00, 1.05,
1.03, 1.09**. Configs beating base on ret/vol: **2/18 → 6/18**.

Contiguous across the whole 30% block and monotone in APY. The mechanism is
honest — a near-zero-vol return stream is added to a de-risked book, so the
numerator rises while the denominator does not.

**Best cell:** 30%/14d/1.5× at 5% APY → **7.62× final, CAGR 42.6%, ret/vol
1.14, maxDD −48.9%**, versus base 18.91× / 0.90 / −73.5%.

**Read that honestly: you end with far less money.** Better risk-adjusted,
lower absolute. Whether the trade is worth taking depends entirely on whether
you would actually have held through −73.5% — and the answer for most people
is no, which is the whole point.

---

## The two caveats that matter most

**1. The flat APY flatters the bear years — the periods that dominate the
result.** Idle capital peaks exactly when vol is high, i.e. 2022. That is
precisely when real stablecoin yields *collapsed* toward ~2%, having been
10–20% in 2021. So the credited yield arrives disproportionately in the window
where the assumed rate is most wrong. **The true figure likely sits below the
5% column, not between 5% and 10%.** A term-structure-aware rerun (actual
historical lending rates by period) would be the honest version.

**2. The tail risk is booked as exactly zero.** The high-idle stretch of 2022
is when UST went to zero and Celsius froze withdrawals. A backtest crediting
smooth carry through that window assumes away the YIELD state's specific
catastrophic risk — the very thing de-peg detection was meant to address, and
which remains **untested** (`FINDINGS_news_systemic_null.md`). A single
de-peg event during a high-idle period would remove several years of
accumulated carry.

Taken together: the ret/vol improvement is real and correctly implemented, but
it rests on a yield assumption that is optimistic in magnitude and silent on
tail risk. Treat 6/18 beating base as an upper bound, not a headline.

---

## THE CORRECTED-UNIVERSE RERUN (2026-08-22) — the decisive test

Everything above was measured on the **survivor-only** basket. Rerun on the
extended universe (11 survivors + 9 dead tokens, including the archive-
recovered original Terra LUNA and UST):

| | survivors-only | **extended (corrected)** |
|---|---|---|
| hold BTC | 5.61× / 0.60 / −76.6% | 5.61× / **0.60** / −76.6% |
| momentum base 30/5/7 | 15.40× / 0.84 / −72.0% | **6.49× / 0.46 / −90.5%** |
| best overlay ret/vol | 0.98 | **0.38** (30%/60d) |
| overlay maxDD, 30% block | ~−50% | −57% … −69% |
| configs beating base ret/vol | 3/18 | **0/18** |
| configs cutting drawdown | 14/18 | 15/18 |

### The finding

**Nothing beats holding BTC.** Base momentum on the corrected universe has a
*higher* final (6.49× vs 5.61×) but **worse** risk-adjusted return (0.46 vs
0.60) and a far worse drawdown (−90.5% vs −76.6%). Every vol-target config
lands at ret/vol 0.01–0.38, all below BTC's 0.60 and most below half of it.
The best cell returns 1.99× against BTC's 5.61×.

The three-part construction this project built — momentum base, vol-target
overlay, yield leg — is **dominated on both measures by buy-and-hold bitcoin**
once the universe is corrected.

### The prediction that failed, and why

Stated before the rerun: *"the relative effect probably holds — vol targeting
is a mechanical transform of whatever return stream it is given."*

Half right. The **drawdown reduction did survive** (15/18 configs) — that part
genuinely is mechanical. But the **ret/vol improvement did not** (3/18 → 0/18).

The reason: on the inflated basket, scaling down a *good* return stream during
high-vol periods removed volatility roughly in proportion to the return it
sacrificed, so the ratio held and the yield leg pushed it above base. On the
corrected basket the underlying stream is poor (ret/vol 0.46), and scaling a
poor stream down just yields a smaller poor stream — CAGR collapses 37.8% →
8–13% while drawdown only improves to −58%. **Vol targeting compresses risk on
any return stream but cannot rescue one.** On the inflated basket that
distinction was invisible.

### Two mechanical notes

- `--delist-loss 1.0` and `0.0` produce **byte-identical** output, consistent
  with `FINDINGS_survivorship.md`: the dead tokens did their dying while still
  trading, so the terminal-loss convention is irrelevant.
- The **yield leg now carries a third of the return**. At 30%/60d: 9.4% CAGR
  without it, 12.5% with. That is stablecoin carry booked as risk-free, which
  it is not — and it is doing proportionally far more work than on the
  inflated basket.

### A bug fixed en route (worth recording)

The extended run first reported `nan` for hold-BTC. Cause:
`run_benchmark_hold` calls `closes.pct_change().dropna()`, and
`DataFrame.dropna()` drops rows where **any** column is NaN. On a panel
containing delisted tokens that discards nearly every row after the first
delisting. Fixed by passing only the benchmark column.

**This is the same failure family as the stale-price-backfill bug**: a
correct-looking pipeline silently computing on a fraction of the data. Third
occurrence in this project.

---

## Caveats

- **One bear market.** Same N=1 limitation as everything in this project.
- **Survivorship** — now measured and applied above. The corrected figures are
  themselves imperfect: `FINDINGS_survivorship.md` shows the magnitude is not
  identified, because adding dead tokens also changes which names rank into
  the top-K. Treat the extended column as *indicative*, not exact.
- **Close-to-close fills**, no intraday slippage.
- **Vol targeting trades constantly.** Costs charge both basket turnover and
  daily exposure changes.
- **Stablecoin yield is not risk-free** — de-peg and protocol tail risk are
  booked as zero, in a window (2022) containing UST's collapse and Celsius's
  freeze.
- **Close-to-close fills**, no intraday slippage.
- **Vol targeting trades constantly.** Costs charge both basket turnover and
  daily exposure changes; results held at 25 bps, but a live book would face
  more friction than modelled.
- **−49% is still a −49% drawdown.** Survivable is not comfortable.

---

## Next, if resumed

1. **Term-structure yield** — replace the flat APY with actual historical
   stablecoin lending rates by period. This is the single change most likely
   to *reduce* the headline result, which is why it is first.
2. **Survivorship** — all 11 tokens survived to today. The 2021 universe
   included LUNA and FTT. Adding a few known-dead tokens with their real paths
   to zero would test whether "beats BTC" survives contact with them. Largest
   unmeasured bias in the project.
3. **Vol-target the momentum base jointly** — 30/5/7 was optimised *unscaled*;
   the best base under an overlay may differ. Deprioritised: this is grid
   search on the same 2,121 days and risks manufacturing overfit.
4. **Leave the DEFENSIVE trigger closed.** Eight attempts, one structural
   result. The evidence says risk management here is mechanical, not
   predictive.
