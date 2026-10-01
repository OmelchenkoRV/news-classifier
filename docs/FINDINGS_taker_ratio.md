# Findings: Taker Buy/Sell Ratio — NULL

**Status:** NULL, as predicted. A pre-registered test of the claim that a low
Binance ETH taker buy/sell ratio is bearish found **no drawdown information**
(edge −1.7pp, z = −0.23). The competing contrarian reading showed a weak
positive on the primary market that **fails** the era split, the threshold
neighbours, and cross-asset and cross-market replication. Candidate #9.

**Date:** 2026-09
**Source of hypothesis:** a shared CryptoQuant chart annotating the Binance
ETH taker buy/sell ratio 7d MA at ~0.95 as "the most bearish level since June."
**Collector:** `collectors/taker_flow_backfill.py`
**Test:** `scripts/test_taker_ratio.py`

---

## Why this one was worth testing

Per the project's sourcing rule, a shared chart is a hypothesis *source*, never
evidence. But this metric was unusually cheap to test properly:

- Binance kline files carry taker-buy volume alongside total volume (columns
  9 and 10), so `ratio = taker_buy / (volume − taker_buy)` is reconstructable
  from the public archive.
- **Futures back to 2020, spot back to 2017**, for any symbol. No vendor, no
  30-day retention wall — better coverage than funding (2019) and far better
  than OI/long-short (forward-capture only).

The chart is almost certainly computed from **futures** taker flow, so futures
ETH was the primary; spot and BTC served as replications.

## An observation from the chart itself

The annotation draws an arrow from the current reading back to **June 2026** —
which was the cycle **bottom** (~$1,600), followed by a rally to ~$2,680. On the
single precedent the chart displays, the "bearish" reading was a contrarian buy.
So two competing claims were tested and neither was favoured.

---

## Pre-registration (fixed in the script before any data existed)

    market=futures, symbol=ETHUSDT, 7d MA crosses DOWN through 0.95,
    horizon 14 days, magnitude 10%, both directions.

**Prior on record:** null or contrarian, by analogy with funding level (F1),
which fired below baseline in every cell because reflexive flow measures go
extreme *after* capitulation.

### Methodological improvement: crossings, not days

Q5 (funding) counted every day a signal was on, so a 10-day cluster became 10
"firings" with overlapping outcome windows — **pseudo-replication** that
inflates n and significance. This test counts **downward crossings** only,
with a 14-day refractory period so one episode counts once. Result: an honest
n=39 on the primary.

---

## Primary result

| claim | hits | baseline | edge | z |
|---|---|---|---|---|
| **BEARISH** (drawdown ≥10%) | 11/39 = 28.2% | 29.9% | **−1.7pp** | **−0.23** |
| CONTRARIAN (rally ≥10%) | 19/39 = 48.7% | 41.6% | +7.1pp | +0.90 |

**The chart's claim is refuted on its own terms.** A crossing below 0.95 is
followed by a ≥10% drawdown at almost exactly the rate of a random day.

### The contrarian hint does not survive the era split

| era | contrarian hits | era baseline | edge |
|---|---|---|---|
| 2020–2022 | 15/20 (75%) | 51.3% | **+23.7pp** |
| 2023–2026 | 4/19 (21%) | 31.5% | **−10.5pp** |

The entire effect lives in one era and **reverses** in the current one — the
same shape as funding momentum: an edge concentrated in a past regime.

---

## The grid: an isolated band, not a region

Contrarian edge, MA7, by threshold:

| threshold | contrarian cells |
|---|---|
| 0.94 | mixed: +1.0, −3.5, +10.9, +5.5, +0.1, −1.2 |
| **0.95** | **all six positive**: +8.5, +4.0, +7.1, +8.2, +7.8, +10.8 |
| 0.96 | **nearly all negative**: −7.5, −2.6, −12.2, −6.0, −3.5, −1.7 |
| 0.97 | around zero |

A genuine effect should strengthen as the threshold becomes more extreme. Here
0.95 is positive and its immediate neighbour 0.96 is negative — **non-monotone
and non-contiguous**, the signature of noise. The pre-registered threshold
happened to land in the one positive row.

Grid totals: bearish **25/48** positive (a coin flip), contrarian 32/48.

**Small-n inflation, live:** the MA14/0.94 row shows contrarian edges up to
**+32.7pp** — on n=10. The self-test produced ±15pp edges from pure noise at
n=4. Same phenomenon.

---

## Replication: four tests, four different answers

| market / asset | n | bearish edge | contrarian edge |
|---|---|---|---|
| **futures ETH** (primary) | 39 | −1.7 (z −0.23) | +7.1 (z +0.90) |
| futures BTC | 25 | **+12.0 (z +1.50)** | −4.2 (z −0.46) |
| spot ETH | 59 | −4.6 (z −0.77) | −3.7 (z −0.59) |
| spot BTC | 79 | −2.3 (z −0.51) | −1.4 (z −0.28) |

ETH futures leans contrarian; BTC futures leans **bearish**; spot is null on
both. The direction does not agree even between the two futures markets.
Across eight primary tests the largest |z| is 1.50 — roughly what the maximum
of eight draws from pure noise would produce.

### Data-quality note

Spot 7d-MA spans **0.38–3.64**, versus 0.85–1.12 for futures. Early or
thin-volume spot days produce absurd ratios. It does not change the verdict,
but spot taker flow is a substantially dirtier series than futures.

---

## Verdict

**NULL.** The bearish claim is refuted on the pre-registered primary. The
contrarian reading fails every robustness check: era split (reverses),
threshold neighbours (sign flips), cross-asset (BTC disagrees), cross-market
(spot null).

**In the current regime (2023–2026) neither direction shows any edge on ETH
futures.** Practically: the September 2026 reading of 0.95 carries no
information about what ETH does next.

### What went right

- **Pre-registering the chart's own number** meant the test gave the claim its
  best shot and could not be accused of cherry-picking.
- **Counting crossings** gave an honest n rather than an inflated day count.
- **The prior was correct**, by analogy with funding level — flow and
  positioning measures behave alike, going extreme with or after the move.

### Reusable asset

`taker_flow` is now a populated table (spot from 2017, futures from 2020, ETH
and BTC). The collector takes any symbol list, so the series is available for
future work without repeating the archive pull.
