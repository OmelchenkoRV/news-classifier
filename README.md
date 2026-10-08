# news-classifier

A research project that set out to build a **three-state capital-allocation
system** for crypto (YIELD / DIRECTIONAL / DEFENSIVE) and, over the course of
testing, answered its own question in the negative.

**Short version:** the DEFENSIVE state was supposed to be driven by a signal
that detects crashes early. Ten candidates were tested across three data
modalities. None worked. What *did* work required no directional prediction:
**volatility is forecastable even though direction isn't.** A filtered
historical simulation corridor passed its pre-registered calibration test on
both assets — with a conditional caveat: it is too narrow in calm regimes.

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

**The one positive result — forecasting the corridor, not the direction:**

| | Result | Doc |
|---|---|---|
| Volatility corridor (FHS) | **Calibrated** — Kupiec p > 0.05 in all 40 cells (BTC/ETH, 7d/14d, 5 levels, 2017+ and 2020+). Gaussian bands fail at 95%/99%. **But conditionally miscalibrated:** a "90%" band covered ~78% in calm regimes and ~96% in storms. Least reliable exactly when it looks narrowest | `FINDINGS_vol_corridor.md` |
| Move sizes by direction (`--moves`) | "If it falls / if it rises, how far" and the deepest dip / highest run inside the window. Calibrated on average (1-in-5 beaten 18–22%, 1-in-20 5–7%); up-share 50–53% = coin flip. **Same calm-regime failure:** at 7–14d the 1-in-20 levels were beaten 8–14% of the time | `FINDINGS_vol_corridor.md` |
| Corridor v2 (mean-reverting vol) | **NOT ADOPTED** under its pre-registered rule. GARCH fitted 2017–2020, tested 2020–2026. Calm coverage improved only +2.8 / +1.0 pts (threshold 4.12). It **did** fix the storm side (95.7% → 92.0% BTC, 95.2% → 91.9% ETH; vol-shock half-life 8–10 days) with 11–16% narrower bands — supplementary, not adopted. Calm spells end in jumps that past returns cannot anticipate | `FINDINGS_vol_corridor.md` |
| Corridor v3 (implied vol, Deribit DVOL) | **ADOPTED — narrowly, with reservations.** Passed all three pre-registered conditions on both assets: calm 75.8% → 78.8% (BTC), 77.3% → 79.9% (ETH); storm closer; bands 6–10% narrower. But calm is **not fixed** (~79%), the gain rests on ~1 independent window per asset, and the bootstrap interval was tighter than any synthetic world. v2 got a similar calm gain under a stricter rule. Supplementary H12: calm days with high implied/realised vol saw v1 breaches 31–36% vs 12–14% — a lead, small counts | `FINDINGS_vol_corridor.md` |
| Fix 1: regime-conditional FHS (c1) | **ADOPTED — broad but small.** On the 9 never-examined universe coins: pooled calm 80.6% → 82.9% (14d, 90%), storm 95.9% → 93.3%; better calm coverage in 8/9 coins (14d) and 9/9 (7d). Closes ~24% of the calm gap; mid regime got slightly worse. Unproven on BTC/ETH (forward only). **Across v2, v3 and c1, no fix gets calm within 7 pts of 90%** — in calm, use the 95% band for a 90% need | `FINDINGS_vol_corridor.md` |
| Depth thinning → vol surprise (futures `bookDepth`) | **NOT SUPPORTED** (2026-10-08). Does unusually thin ±1% futures depth precede higher-than-expected volatility? ρ(z, S₇) +0.05 BTC / +0.09 ETH, needed upper bound < 0: wrong sign. Positive 14-day secondaries fit vol mean reversion and vanish after controlling for implied vol. A moderate effect (ρ ≈ −0.15) is ruled out. Fix 3 closed; calm gap unchanged | `PLAN_depth_thinning.md` |

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
- **The volatility corridor is forecastable on average.** FHS passed its
  pre-registered coverage test across both assets, both horizons and the
  2017+ period including 2018. Independent support is two correlated assets
  plus the added 2017–2020 span — strong by this project's standards, but
  not forty confirmations.

**Held with reservations**

- **The corridor is wrong regime by regime.** Calm-regime bands under-cover
  (~78% at a nominal 90%); storm-regime bands over-cover (~96%). Read a narrow
  corridor as narrower than its label. A pre-registered mean-reverting vol
  model (v2) fixed the storm side but not the calm side and was not
  adopted: calm spells end in jumps, which no model on past returns sees.
  An implied-vol corridor (v3, Deribit DVOL) was adopted under its own
  pre-registered rule as the working corridor — sharper and no worse — but
  it moves calm coverage only from ~76% to ~79%.
  Calibrating calm days against calm-start history (c1) was adopted on 9
  alt coins: broad (8–9 of 9 coins) but small (+2.2 pts). Every fix moves
  calm coverage 1–3 points; none closes the gap. In calm, use the 95% band
  when you need 90%.

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
13. **Passing the test is not the same as working.** The corridor passed its
    pre-registered unconditional coverage test in every cell, while being
    wrong in opposite directions in calm and stormy regimes. After a method
    passes, check what the test does not measure — conditional coverage,
    breach clustering, tail asymmetry — before relying on it.

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
| `python -m scripts.test_vol_corridor` | Volatility-corridor coverage test + today's corridor; `--long` for 2017+ |
| `python -m scripts.test_vol_corridor --moves` | Move sizes by direction from the latest close, with an out-of-sample check by vol regime |
| `python -m scripts.test_vol_corridor_v2` | Pre-registered corridor v2 test (mean-reverting vol); `--self-test`, `--calibrate` |
| `python -m collectors.dvol_backfill` | Deribit DVOL (30-day implied vol), BTC/ETH daily from 2021-03; `--probe`, `--self-test` |
| `python -m scripts.test_vol_corridor_iv` | Pre-registered corridor v3 test (implied vol); `--self-test`, `--calibrate` |
| `python -m scripts.test_vol_corridor_cfhs` | Pre-registered Fix 1 test (regime-conditional FHS) on the 9 non-BTC/ETH universe coins; `--self-test`, `--calibrate` |
| `python -m scripts.corridor_logger` | Forward test service (docker: `corridor-logger`); `--once`, `--loop`, `--report`, `--self-test` |
| `python -m collectors.wall_capture` | Order book on a fixed price grid every minute (docker: `wall-capture`) |
| `python -m scripts.wall_fate` | Pre-registered wall test: pulled vs reached, pull rate by distance, do walls hold |
| `python -m collectors.book_stream` | Live order-book rebuild, spot + futures (docker: `book-stream`, own image `Dockerfile.stream`) |
| `python -m scripts.wall_fate_stream` | Pre-registered wall test on the rebuilt book: traded vs cancelled, spot and futures |
| `python -m collectors.bookdepth_backfill` | Binance futures `bookDepth` archive (±1–5% depth, BTC/ETH, ~2023+); `--probe`, `--check-stream` |
| `python -m scripts.test_depth_thinning` | Pre-registered test: does thin futures depth predict vol surprise? `--self-test`, `--calibrate` |
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
| `taker_flow` | Taker buy/sell volume — BTC/ETH futures from 2020, spot from 2017; spot for all 11 universe coins from listing |
| `dvol_daily` | Deribit DVOL implied vol, BTC/ETH, 2021-03-24 → present (2,024 days each, no gaps) |
| `corridor_closes` / `corridor_forecasts` / `corridor_outcomes` | Forward test of the corridors, 2026-10-08 → (service-maintained; forecasts write-once) |
| `ob_book` / `ob_minutes` | Binance spot book (ETH, BTC) on a fixed price grid + 1-minute candles, every minute, from 2026-10-08 |
| `ws_book_1m` | Rebuilt order book (spot + futures, ETH/BTC), per minute and price bucket: resting / added / removed / traded USD; 120-day retention |
| `bookdepth_15m` / `bookdepth_days` | Binance futures depth at ±1–5% (15-minute means of quality-checked snapshots), BTC/ETH |
| `eth_etf_flows` | BTC/ETH/SOL/HYPE daily net flows from ETF launch (602 days) |
| `eth_snapshots` + `eth_derivatives` | 5-min live capture, 2026-04 → present |
| `headlines` + `classifications` | ~1.39M GDELT archive headlines, classified |

**Known data limits:** okx captures no open interest; bybit has OI but no
long/short ratio — all positioning work is effectively **single-exchange
(Binance)**. Orderbook depth is truncated (identical at 2% and 5% from mid).

---

## If this is resumed

0. **Forward tracking is running** — the `corridor-logger` service logs
   v1/c1 (11 coins) and v3 (BTC/ETH) daily, write-once, and scores each
   window as it closes. First formal read at 12 months (2027-10) under the
   rules already registered; see FINDINGS_vol_corridor.md "Forward tracking".
   **Wall capture is running too** — read `scripts.wall_fate` after 28 days
   (from ~2026-11-05) under the rules in `docs/PLAN_wall_fate.md`.
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
- `taker_flow` was refreshed to 2026-09-30 (2026-10-08). The archive is
  monthly, so it always lags; re-run `collectors.taker_flow_backfill
  --market spot` before tests that use it.

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

The one positive result came from changing the question. Direction proved
unforecastable across ten candidates; the **range** of likely outcomes did
not. A filtered-historical-simulation corridor is calibrated on average — and
honestly reported as unreliable in calm regimes, where it is too narrow.

Managing second moments works. Predicting first moments mostly doesn't. And a
benchmark you can't beat is information, not failure.
