# news-classifier

A research project that set out to build a **three-state capital-allocation
system** for crypto (YIELD / DIRECTIONAL / DEFENSIVE) and, over the course of
testing, answered its own question in the negative.

**Short version:** the DEFENSIVE state was supposed to be driven by a signal
that detects crashes early. Ten candidates were tested across three data
modalities. None worked. What *did* work required no prediction at all —
sizing exposure inversely to trailing realised volatility, which produces the
DIRECTIONAL/YIELD split mechanically.

The project's value is the **record of what was ruled out and why**, not a
strategy. Every claim below is qualified by what is actually known.

---

## The ledger

| # | Candidate | Result | Doc |
|---|---|---|---|
| 1 | News → return prediction | **NULL** — ~49% directional accuracy after detrending; coin-flip, held across a regime flip | — |
| 2 | Asset routing (oil/gold) | **NULL** — fatally under-powered, n=1–2 per cell | — |
| 3 | Price vol/drawdown breaker | **NULL** — 4/36 configs improved risk-adjusted return; cut drawdown but paid in whipsaw. *A lookahead bug first produced a fake 746× result* | — |
| 4 | News-systemic flag | **DATA WALL** — GDELT GKG has no titles; classifier fed URL-slug salad reverted to a volume-correlated baseline. Fired **hardest in the calm control** | `FINDINGS_news_systemic_null.md` |
| 5 | ETF inflow crossovers → rallies (BTC) | **NULL** — below the unconditional base rate in **22/36 cells**. The hypothesis rested on the single largest outcome in 27 firings. *Scope: inflows→rallies only; outflows→drawdowns is #10* | `FINDINGS_aug2026_rally.md` |
| 6 | Funding rate — level (F1) | **INVERTED** — below baseline in *every* cell. Funding goes negative *after* capitulation, so it fires near bottoms | `FINDINGS_funding_defensive.md` |
| 7 | Funding rate — momentum (F2) | **PARTIAL, then closed** — first contiguous winning region in the project; the decisive era replicated on ETH (+17.3pp, n=33). But the largest-n era failed to replicate, and the effect **inverts at 20% drawdowns**. Flags routine volatility, not crashes | `FINDINGS_funding_defensive.md` |
| 8 | Liquidation cascade | **NO EFFECT** — elasticity does not rise with move size (sharpest down bucket: 0.92). Deleveraging is proportional, not explosive. Depth capture also truncated | `FINDINGS_liquidation_cascade.md` |
| 9 | Taker buy/sell ratio | **NULL** — pre-registered on a CryptoQuant chart's own claim ("most bearish since June"). Bearish edge −1.7pp, z=−0.23. Contrarian hint reverses across eras, flips sign at the adjacent threshold, and disagrees between ETH and BTC futures. Spot null | `FINDINGS_taker_ratio.md` |
| 10 | ETF outflow extremes → drawdowns | **NULL** — ETH edge +0.8pp (z=+0.07), BTC +0.2pp (z=+0.03); both on the unconditional rate. Grid showed 42/54 positive cells — an artifact of cells sharing events. Expanding thresholds and publication lag built in | `FINDINGS_etf_outflows.md` |

**One structural result — substantially withdrawn on rerun:**

| | Result | Doc |
|---|---|---|
| Volatility targeting | Cuts drawdown mechanically (15/18 configs) — that part is real. But rerun on the **survivorship-corrected universe**, **0/18 configs improve risk-adjusted return**, and the whole construction is **dominated by holding BTC** (0.46 vs 0.60 ret/vol). It compresses risk on any return stream; it cannot rescue a poor one | `FINDINGS_voltarget.md` |

**And one self-inflicted correction:**

| | Result | Doc |
|---|---|---|
| Momentum (the former headline) | Claimed to beat buy-and-hold BTC across a full cycle. **Materially inflated by survivorship; true magnitude unidentified.** Degrades in every version of the test (−74.9% / −57.8%) but the confound is now larger than the effect | `FINDINGS_momentum.md`, `FINDINGS_survivorship.md` |

---

## What is actually believed, and how strongly

**Strongly held**

- Crash-leading signals are **not extractable from freely available data** at
  the granularity tried here. Ten candidates, three modalities, consistent
  failure. This is unsurprising once stated plainly: a genuinely predictive
  crash signal would be enormously valuable, so the prior that it sits in free
  public data was always low.
- **Positioning predicts fragility, not direction.** Three episodes in ten
  weeks: crowded longs → crash (June), crowded shorts → +22% rally (Aug 19),
  crowded longs → flush (Aug 22). A "crowded → DEFENSIVE" flag is right about
  half the time on the thing a defensive signal must get right.
- **Volatility targeting cuts drawdown mechanically.** It requires no forecast
  and the drawdown reduction is not a fitted result — it holds on both the
  inflated and the corrected universe.

**Held with reservations**

- **Nothing built here beats buy-and-hold BTC.** On the corrected universe,
  BTC returns 5.61× at ret/vol 0.60; the best overlay config manages 1.99× at
  0.38, and base momentum 6.49× at 0.46. Stated with reservation only because
  the corrected universe is itself imperfect (see below) — not because there
  is reason to think the ordering flips.
- The yield leg's contribution assumes a flat APY across 2020–2026. Real rates
  ranged ~20% (2021) to ~2% (2023), and idle capital peaks in the bear years
  when rates were *lowest*. On the corrected universe yield carries a third of
  total return, so this assumption now matters more, not less.

**Explicitly not known**

- The true magnitude of survivorship bias. Requires point-in-time universe
  membership; free sources do not provide it.
- Whether any of this is tradeable. Nothing here has been traded, and −48% is
  survivable, not comfortable.

---

## Methodological rules this project arrived at

These were learned the expensive way and are the most portable output.

1. **Sweep + baseline, always.** A hit rate means nothing without the
   unconditional base rate over the same window. "55% false positives" is
   meaningless if 55% of random days qualify.
2. **Contiguity, not peaks — across INDEPENDENT evidence only.** Isolated
   winning cells are noise. But neighbouring grid cells usually share most of
   their events, so a run of positive cells can be one lucky event set
   repeated — pure noise produced +32.9pp, z=+3.10 with a contiguous-looking
   column. Contiguity counts only across other assets, other eras, and
   non-overlapping events.
3. **Era-specific baselines.** Pooled baselines hide the effect. BTC's
   drawdown base rate was 31.5% / 11.4% / 16.0% across three eras — a pooled
   figure would have been meaningless.
4. **Distrust striking results in BOTH directions.** A fake 746× came from a
   lookahead bug (`breaker.shift(1)` was the fix). A fake −85% survivorship
   penalty came from nine *live* tokens being misread as delisted. The
   instinct that catches good news must also catch bad.
5. **One episode generates a hypothesis; it cannot validate one.** Working
   backwards from a known crash always finds "elevated" indicators.
6. **Sourcing discipline.** Signals from shared infographics enter as untested
   candidates, never findings. Nobody shares a working signal — and that cuts
   against one's own search too.
7. **Check for silent escape hatches.** `dropna()` on a held position lets a
   delisted token vanish for free instead of taking the loss.
8. **Watch for recycled tickers.** Binance reused `LUNAUSDT` for Terra 2.0;
   splicing would fabricate a collapse-then-recovery. Same class as MATIC→POL.
9. **Count events, not days.** A signal that stays on for ten days is one
   event, not ten. Counting days produces overlapping outcome windows —
   pseudo-replication that inflates n and significance. Count crossings, with
   a refractory period.
10. **Pre-register on the claim's own terms.** When testing a shared chart,
    fix the primary cell to the exact threshold the chart cites, before
    collecting data. It gives the claim its best shot and removes any
    accusation of cherry-picking — and a refutation on those terms is final.
11. **Calibrate the calibration.** A noise-floor method must be checked on
    data where the answer is known before it is used. A permutation null built
    for the ETF test flagged pure noise as significant at more than twice the
    nominal rate — it was removed, not kept. A tool known to be wrong is worse
    than no tool, because someone will trust it later.
12. **No rescue analyses after a null.** Plausible improvements proposed only
    after seeing a null (e.g. normalising ETF flows by assets under management)
    are recorded as limitations, not rerun. Otherwise every null becomes a
    search for the version that works.

---

## Repository layout

```
capture/       live market-state capture (eth-capture: derivatives,
               orderbook, options, ETF flows)
collectors/    historical backfills — prices, funding, GDELT archive,
               Binance delisted-symbol archive
config/        universe definition, DB, asset routing, migrations
inference/     the news classifier (categories + impact)
pipeline/      live classification path, triggers, outcomes
scripts/       backtests and analysis queries
docs/          findings and plans — START HERE
tests/         92 passing, 35 skipped
```

### Key entry points

| Command | Purpose |
|---|---|
| `python -m scripts.backtest_momentum` | Base momentum grid (survivor-only) |
| `python -m scripts.backtest_voltarget` | Vol-target overlay + yield leg |
| `python -m scripts.backtest_voltarget --self-test` | 7 mechanics checks, no DB |
| `python -m scripts.backtest_survivorship` | Survivorship test + dead-token trade log |
| `python -m collectors.binance_archive --probe` | Recover delisted symbol history |
| `python -m collectors.funding_backfill` | Funding history to 2019 |
| `python -m collectors.taker_flow_backfill` | Taker buy/sell volume from the archive (spot + futures) |
| `python -m scripts.test_taker_ratio` | Pre-registered taker-ratio test, both directions |
| `python -m scripts.test_etf_outflows` | ETF outflow extremes → drawdowns; `--calibrate` measures the noise band |
| `python -m scripts.orderbook_walls` | Full Binance order book, resting-order clusters by price |
| `python -m capture.etf_flows` | Daily ETF flows (idempotent) |

### Analysis queries

`scripts/q4_flow_false_positive.sql` · `q5_funding_false_positive.sql` ·
`q6_eth_replication.sql` · `q7_liquidation_cascade.sql` ·
`queries_aug2026_rally.sql`

---

## Data assets

| Table | Coverage |
|---|---|
| `price_snapshots` | 11 survivors + 9 dead tokens, 2020-11 → present |
| `funding_history` | BTCUSDT/ETHUSDT, 2019-09 → present (~15k rows, **two bear markets**) |
| `taker_flow` | Taker buy/sell volume, ETH/BTC — futures from 2020, spot from 2017 |
| `eth_etf_flows` | BTC/ETH/SOL/HYPE daily net flows from ETF launch (602 days) |
| `eth_snapshots` + `eth_derivatives` | 5-min live capture, 2026-04 → present |
| `headlines` + `classifications` | ~1.39M GDELT archive headlines, classified |

**Known data limits:** okx captures no open interest; bybit has OI but no
long/short ratio — all positioning work is effectively **single-exchange
(Binance)**. Orderbook depth is truncated (identical at 2% and 5% from mid).

---

## If this is resumed

1. **Rerun vol targeting on the survivorship-extended universe.** The one
   place a stated conclusion rests on numbers known to be wrong. One-line
   change; the relative effect probably holds.
2. **Term-structure yield** — replace the flat APY with historical lending
   rates. Listed first among improvements *because it should reduce the
   result.*
3. **Point-in-time universe membership** — the only data purchase with a
   genuine case. It fixes a known, measured defect affecting every result,
   unlike paid positioning history which would test a hypothesis the evidence
   already argues against.
4. **Do not** add more dead tokens (makes the universe less representative),
   and **do not** grid-search further on the same 2,121 days — that
   manufactures overfit rather than discovering anything.

### Housekeeping

- Two junk directories from a shell brace-expansion mishap:
  `{config,collectors,pipeline,scripts,tests}/` and
  `{config,pipeline,scripts,tests}/` — safe to delete.
- `commodity_backfill` fails each cycle in the price-backfill container
  (missing `yfinance`). **Harmless** — isolated by `|| echo`, crypto backfill
  unaffected, and the commodity experiment is a documented null.
- The Docker price-backfill loop refreshes **only BTCUSDT/ETHUSDT**. The other
  nine universe symbols need `--universe` runs, and stale data here caused a
  real analysis error (see rule 4).
- GDELT archive checkpoint marks a slot done even on failure — unfixed footgun.

---

## The honest summary

Set out to build a signal-driven allocation system. Found that the signal half
doesn't exist in reachable data, that the one apparent edge was substantially
a counting artifact, and that the risk-management layer — while mechanically
real — does not turn a poor return stream into a good one.

On the survivorship-corrected universe, **nothing built here beats holding
bitcoin**. Momentum, vol targeting and the yield leg are all dominated by a
buy-and-hold benchmark on risk-adjusted return.

That is a complete answer to the original question, arrived at from this
project's own data. Three stated conclusions were overturned by later tests
run against them — momentum beating BTC, funding momentum as a defensive
signal, and vol targeting as an improvement rather than a rescaling. Each was
overturned because the test that could kill it was actually run.

Managing second moments works. Predicting first moments mostly doesn't. And a
benchmark you can't beat is information, not failure.
