# COVID BDBA Validation Exercise

A retrospective test of the Beyond Design Basis Accident surveillance concept against
real historical data. Does generic outbreak-language frequency monitoring detect the
COVID warm-up before markets crashed? And does it stay quiet during 2014 Ebola and
2015 MERS (which didn't crash markets)?

## What this proves (or disproves)

The BDBA layer's core claim: by monitoring keyword frequency for
themes that shouldn't matter, you can detect crowd warm-up before
markets price it in.

This exercise tests that against the gold-standard event:

- **COVID timeline**: Wuhan pneumonia reports late Dec 2019 → S&P high Feb 19, 2020 → bottom Mar 23, 2020 (-34%)
- **The window of interest**: Jan 1 - Feb 19, 2020 — 6 weeks where coverage rose but markets ignored
- **The success criterion**: alerts fire before Feb 19 with reasonable lead time

## What's NOT being tested

This is a single-event proof of concept. It does not test:

- Magnitude prediction (how big the crash will be)
- Direction conviction (sometimes BDBAs trigger melt-ups, e.g. UAP disclosure briefly boosted defense stocks)
- Real-time operational performance (we're using clean GDELT data, not noisy live RSS)
- Other BDBA topic types (UFO, AI incidents, infrastructure attacks)

## Honest limitations

**Keyword choice is the hardest part.** The BDBA list must be defensible
**before** the event. We're using `outbreak`, `pandemic`, `pneumonia outbreak`,
`mystery illness`, `WHO emergency` — generic language that any BDBA system
would reasonably include from day one. We are **not** allowed to use COVID,
coronavirus, SARS-CoV-2, or anything that didn't exist as a concept in
November 2019. If you see those keywords in `topics.py`, the test is rigged.

**GDELT data quality varies.** We're getting headlines + URL + domain + publish
time. We're NOT getting article body text. So matching is title-only —
the actual COVID warm-up included many headlines with generic titles
("Health update from China") whose body text revealed the pneumonia content.
We'll undercount.

**Tier 1 source list is hand-curated.** Reuters, Bloomberg, WSJ, NYT, etc.
This biases toward English-language outlets. Real COVID warm-up was first
visible in Chinese-language sources (Caixin, Sanlian Lifeweek), then
Hong Kong (SCMP), then Western. SCMP is in our Tier 1 list but mainland
Chinese sources aren't.

**Survivorship in baseline.** The 30-day rolling baseline assumes
"normal" baseline conditions. If pandemic chatter was already elevated
in December 2019 (pneumonia of unknown cause reports started Dec 31),
the baseline includes some pre-event signal.

## Files in this directory

```
schema.py              -- creates research tables (idempotent)
topics.py              -- registers BDBA topic definitions in DB
collect_gdelt.py       -- pulls historical news from GDELT API
collect_markets.py     -- pulls daily OHLCV via yfinance
simulate.py            -- runs retrospective day-by-day surveillance
analyze.py             -- generates report + matplotlib timeline charts
run_all.py             -- master orchestrator (8 stages)
README.md              -- this file
```

## How to run

### Option 1: full end-to-end (recommended)

```bash
python -m research.covid_exercise.run_all
```

This takes about 30-90 minutes depending on GDELT response speed.
The slow part is stage 3-5 (GDELT pulls with 1.5s delay between
requests to respect rate limits).

### Option 2: stage-by-stage

```bash
# Setup
python -m research.covid_exercise.run_all --stages 1,2

# Data collection (slow - GDELT throttled)
python -m research.covid_exercise.run_all --stages 3,4,5,6

# Analysis (fast)
python -m research.covid_exercise.run_all --stages 7,8
```

### Option 3: just COVID, skip controls

```bash
python -m research.covid_exercise.run_all --stages 1,2,3,6,7,8
```

This skips the Ebola/MERS control tests. Useful for a first pass to see
if the COVID detection works at all before validating specificity.

## Stages explained

**Stage 1: Schema migration** - creates `historical_headlines`,
`historical_market_daily`, `bdba_topics`, `bdba_retrospective` tables

**Stage 2: Topic registration** - inserts the `pandemic_signal` topic
config (keywords, thresholds) into the database

**Stage 3-5: GDELT collection** - hits the GDELT v2 DOC API for each
research period, walking forward in 7-day chunks. Inserts to
`historical_headlines` with `research_topic` tag.

**Stage 6: Market data** - pulls SPY, QQQ, VIX, TLT, GLD, oil, BTC daily
data via yfinance for the 2014-2020 range

**Stage 7: Retrospective surveillance** - runs the BDBA detection logic
day-by-day for each research period. Computes baselines using only data
**prior** to each snapshot day (no future-data leak). Stores results in
`bdba_retrospective`.

**Stage 8: Analysis** - generates `covid_analysis.md` with summary tables
and lead-time analysis, plus matplotlib charts overlaying news volume
with VIX and SPY for each period.

## Expected outputs

After successful run, in `/mnt/user-data/outputs/`:

- `covid_analysis.md` — markdown report with findings
- `bdba_covid_timeline.png` — timeline chart (news + alerts + market)
- `bdba_ebola_2014_timeline.png` — Ebola control chart
- `bdba_mers_2015_timeline.png` — MERS control chart

## Interpreting results

**A successful run** would show:

| Metric | COVID | Ebola 2014 | MERS 2015 |
|--------|-------|------------|-----------|
| Alert rate | 5-15% | < 5% | < 3% |
| First alert | Late Dec 2019 - Mid Jan 2020 | Late July 2014 | June 2015 |
| Peak VIX | > 40 | ~17 | ~18 |
| 5d lead time before SPY drop | 15-30 days | n/a (no drop) | n/a (no drop) |

**A failed run** would show:

- COVID alert rate similar to Ebola/MERS → false positive problem
- Lead time too short (< 3 days) → not actionable
- Alerts after market already crashed → reactive not predictive

Either outcome is informative. A null result tells us BDBA frequency
monitoring alone is insufficient and needs additional signals (source
diversification weighting, semantic content analysis, etc).

## Future extensions

Once the basic exercise is validated:

1. **Add UAP 2017 disclosure** as a "non-pandemic BDBA" control
2. **Extend to Russia-Ukraine 2022** as another DBA-spanning event
3. **Test specificity against routine flu seasons** (2017-2018 H3N2, etc)
4. **Add sentiment overlay** — positive/negative tone of the matched headlines
5. **Add Chinese-language sources** via the GDELT Translingual API
6. **Test combined signal** — pandemic_signal + economic_signal + supply_chain_signal
   firing together vs alone

## What I expect to find

Based on the COVID timeline, I expect:

- First credible alert: late December 2019 or early January 2020
  (pneumonia outbreak language from Wuhan reports)
- Sustained alerts: mid-January onwards as WHO and CDC start commenting
- Major escalation: Jan 20-23 (first US case, Wuhan lockdown)
- Lead time over first SPY 2% drop: 25-40 days
- Lead time over first SPY 5% drop: 35-50 days
- Ebola 2014: maybe 10-20 alerts at low severity, no market panic
- MERS 2015: maybe 5-10 alerts, brief regional concern

If this expectation holds, the BDBA layer is validated as a viable
decision-support input. If it fails (alerts too late, or false positives
too high), we know the simple frequency approach needs augmentation.
