# Plan: Leverage / Positioning Defensive Signal

**Goal.** Test whether a *derivatives-leverage* signal — funding rate and/or
positioning (long/short ratio, open-interest dynamics) — can drive the
DEFENSIVE transition of the three-state allocation model earlier or more
reliably than the price-based vol/drawdown breaker (which was a null:
4/36 configs improved risk-adjusted return) and the news-systemic flag
(which hit a data-availability wall).

**Origin.** This candidate came from working *backwards* from the live
June-2026 crash (BTC −51% from its Oct-2025 high, ETH ~−66%). The
`eth-capture` data over 2026-05-11 → 2026-06-05 showed a clear leverage
signature: long/short ratio rising from ~1.75 to ~4.0 *into* the decline
(crowded longs catching the knife), open interest bleeding from $4.8B to
$3.8B (deleveraging), and funding flipping from positive to negative on the
final capitulation day (June 5). The question is whether that signature is a
*predictive* signal or just a description of one crash.

---

## The honest framing (read before building anything)

Working backwards from a known crash is **hypothesis generation, not
validation.** After a crash you can always find indicators that were
"elevated." The only test that matters is the one a single crash cannot
answer: **how often does the signal fire WITHOUT a crash following?** A
long/short ratio of 4 looks damning in June 2026, but crowded longs are also
common in healthy bull markets. The entire value of this experiment is
estimating the **false-positive rate** across many non-crash periods — not
re-confirming that the signal was present in the one crash we already know
about.

This is the same discipline that killed the bull-only momentum result
(K=1 winners that collapsed out-of-sample) and the news-systemic flag (fired
hardest in the calm control). If the leverage signal fires in most bull
months too, it is a false-alarm machine regardless of how good it looks in
June 2026.

---

## The data asymmetry (the central constraint)

The three leverage metrics differ sharply in historical availability, and
this shapes the whole experiment:

| Metric | Historical backfill? | In this crash it was… |
|---|---|---|
| **Funding rate** | YES — Binance `/fapi/v1/fundingRate` serves history to contract launch (2019-20), free, no key | COINCIDENT (flipped negative *during* capitulation, not before) |
| **Open interest** | NO — Binance `/futures/data/openInterestHist` retains only ~30 days | mildly leading (bled down through the decline) |
| **Long/short ratio** | NO — same ~30-day retention; 3rd-party history is paid, not reconstructed | MOST leading (rose 1.75→4.0 into the decline) |

The cruel irony: the metric that looked **most leading** (long/short ratio)
is the one that **cannot be backfilled** for the 2022 bear from free sources.
The metric that backfills cleanly (funding) was **coincident, not leading**,
in the one crash we observed. So a free, historical, multi-cycle backtest can
only really test funding — the weakest of the three as a *leading* signal.

Three honest ways to live with this:

1. **Funding-only historical backtest (free, now).** Test whether funding-rate
   extremes / sign-flips flag the known crashes (2022 bear, 2021 top) without
   false-firing in bull runs. Accept that funding may be coincident and this
   may null — but it's free and uses the clean-numeric path that escapes the
   headline walls.
2. **Forward-only positioning capture (eth-capture, already running).** OI and
   long/short can't be backfilled, but `eth-capture` is recording them
   *forward* at 5-min cadence across all three exchanges. Over the coming
   months/cycles this accumulates the positioning history no free archive has.
   This is the long game — the right role for live capture, same as for de-peg.
3. **Pay for positioning history (CoinAPI flat files etc.).** Only if the
   funding-only test is promising enough to justify the cost. Not first.

---

## STEP 1 — Funding-rate historical backfill (free, gating)

New collector `collectors/funding_backfill.py`, modelled on
`collectors/price_backfill.py`:
- Endpoint: `GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime=…&endTime=…&limit=1000`
- Pull BTCUSDT + ETHUSDT (and the rest of the TRADEABLE_UNIVERSE perps that
  existed) from 2020-11 → now, to match the momentum price panel.
- Funding posts every 8h, so this is small data (~3 rows/day/symbol) — no
  rate-limit or volume concern, unlike the GDELT pull.
- Store in a new `funding_history(symbol, funding_time, funding_rate)` table,
  `UNIQUE(symbol, funding_time)`, idempotent.

**CHECK 1:** funding rows span 2020-11 → now for BTC/ETH with no large gaps,
and the 2022-05/06/11 crash windows are populated.

---

## STEP 2 — Define the leverage flag (funding variant, swept)

The flag is computed per-day, **shifted one day forward** (the no-lookahead
discipline the price-breaker bug taught us — a flag from day t's funding may
only inform day t+1's position).

**Variant F1 — funding sign / extreme:** fire DEFENSIVE when funding is
persistently negative (longs capitulating) OR when funding's deviation from
its trailing baseline exceeds a threshold (over-leverage in either direction).

**Variant F2 — funding momentum:** fire on a sharp *drop* in funding over a
short window (the positive→negative transition this crash showed), which may
catch the regime change even if the level itself is coincident.

**Grid:** baseline window ∈ {7,14,30}d, threshold multiples, sign-persistence
∈ {1,3} fundings, hysteresis recover_days ∈ {0,7}. Full grid reported.

---

## STEP 3 — Overlay backtest + the false-positive test

New script `scripts/backtest_defensive_funding.py`, structurally identical to
`scripts/backtest_defensive.py` (fixed 30/5/7 momentum base, breaker fires on
the funding flag, same shift(1), same costs). Reuse the shared backtest
helpers.

Report per grid cell: final, ret/vol, maxDD, %cash, and — the metric that
actually matters here —

  **false-positive rate: of all days the flag fired DEFENSIVE, what fraction
  were NOT followed by a meaningful drawdown (say >10% within 14 days)?**

A flag that improves ret/vol *and* has a low false-positive rate is real. A
flag that improves ret/vol only by sitting in cash through a bull run (high
false-positive rate, lucky timing on the one bear) is the momentum-K=1
mirage in new clothes.

---

## STEP 4 — Read honestly (decision rules fixed in advance)

- **Improves ret/vol over base, beats the price-breaker, low false-positive
  rate, contiguous winning region** → funding is a real defensive signal;
  proceed toward the live model and add positioning (OI, L/S) from the
  forward `eth-capture` data as it accumulates.
- **Cuts drawdown but high false-positive rate / no ret/vol gain** → funding
  is coincident not leading (as it was in June 2026); another honest null.
  Document. The positioning metrics (forward-captured) become the only
  remaining hope, on a multi-month horizon.
- **Mixed** → contiguous-region test, as always; one lucky cell is not a
  signal.

**Known biases to state in any writeup:**
- Funding backfills but was coincident in the one crash we've seen.
- Coinbase premium backfills and is closer-to-cause, but is reflexive/noisy —
  negative premium is common in calm markets, so expect a high false-positive
  rate to clear before trusting it.
- OI / long-short cannot be backfilled free — the most promising metrics are
  forward-capture-only, so their real test is months away (N=1 until then).
- One-and-a-bit bear markets (2022, and now 2026) — still essentially N=1-2.
- Survivorship + close-to-close, inherited from the momentum backtest.

---

## Why this sits alongside (not before) de-peg

De-peg and leverage are the two remaining clean-numeric defensive candidates,
both escaping the headline/title walls. Order by effort/availability:

1. **Funding backfill (this plan, Step 1-3):** free, immediate, but tests the
   weakest-leading of the leverage metrics.
2. **De-peg:** needs historical stablecoin price backfill (USDC/USDT/DAI/UST
   through 2022/2023) — also clean numeric, also free, arguably more relevant
   to a lending book.
3. **Coinbase premium:** the price spread between Coinbase (BTC-USD, the
   US-regulated venue where ETF/institutional flow lands) and Binance
   (BTCUSDT, global/retail). Positive = strong US institutional demand;
   negative/falling = weak or selling US institutions. CLEAN NUMERIC and
   FREE-BACKFILLABLE — one leg (BTCUSDT) is already in the price backfill;
   the other is Coinbase BTC-USD spot history, freely available. Conceptually
   closer to *cause* than funding: it measures the demand of the specific
   cohort (US institutions via ETFs) that drove the June-2026 crash, rather
   than leverage that reacts to price. Surfaced by the June-2026 crash
   infographic (negative Coinbase premium flagged "очень высокое"); corroborates
   but does not validate. Belongs in the free/backfillable tier alongside
   funding and de-peg — same false-positive-rate gate. THE CATCH: it is also
   reflexive and noisy — premium widens/narrows constantly in calm markets, so
   a negative premium is common WITHOUT a crash following. Sensible-sounding is
   not the bar; the false-positive test is (it killed four prior such ideas).
4. **Positioning (OI/L-S):** forward-capture only via eth-capture; the
   multi-month accumulation play.

The live crash of June 2026 is, in effect, the first forward data point for
all of these — `eth-capture` recorded its leverage signature in full. That
single episode is why these candidates exist, and also why they cannot yet be
trusted: one crash generates a hypothesis; it cannot validate one. NOTE on
sourcing: the candidate list is partly seeded from shared crash infographics
(e.g. the June-2026 Telegram "key factors" board). Treat those as hypothesis
*sources*, never validation — nobody shares a working leverage signal; what
gets shared is narrative or already-dead edges. Every factor lifted from such
a chart enters here as an untested candidate, not a finding.
