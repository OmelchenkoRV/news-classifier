# Architecture

This document goes a level deeper than the [README](../README.md): it explains *why* the system is shaped the way it is, where the hard problems were, and the design decisions that aren't obvious from reading the code top-to-bottom.

The one-line version: **news-classifier is a deterministic signal-generation pipeline that emits measurable alerts onto a Postgres boundary, then measures its own predictive accuracy honestly enough to tell you when it isn't working.**

---

## The core problem

Markets react to the *first* credible appearance of a narrative, not the hundredth article about it. A naive "classify news, alert on market-moving categories" system fails immediately: during a single geopolitical escalation it fired alerts on **37.6% of all headlines**, because it treated 500 articles about one event as 500 events.

Everything downstream of classification exists to solve some facet of this:

- **Novelty** — is this headline a genuinely new event, or coverage of one already seen?
- **Direction** — does "Iran threatens to *close* Hormuz" mean the same thing as "Iran offers to *reopen* Hormuz"? (No — and a system that conflates them cancels its own signal.)
- **Measurement** — given an alert fired, what did the market *actually* do, and can we learn a prior from it?
- **Honesty** — is any learned prior real, or just an artefact of the market regime it was measured in?

---

## Data flow, stage by stage

### Ingestion → classification → direction

RSS feeds are collected and deduplicated by title-hash. Each surviving headline passes through a fine-tuned **DistilBERT** classifier (`inference/classifier.py`) that assigns a category (geopolitical, regulatory, macro, …) and a confidence. Only certain categories are event-eligible.

Eligible headlines go through **Stage 2 directional extraction** (`inference/verb_extractor.py`): a spaCy dependency parse plus a hand-built [verb taxonomy](../config/verb_taxonomy.py) (12 categories, ~114 lemmas) resolves a *direction* — `escalation` / `de-escalation` / `neutral` / `context-dependent`.

The non-obvious part is the **complement-verb resolver**. In "Iran threatens to close the strait," the head verb is *threatens* (which alone is escalatory but vague); the directional meaning lives in the complement *close*. The extractor walks the dependency tree to the complement and resolves direction there, with explicit negation handling ("agreed *not* to close"). This is the fix for the original directional-ambiguity bug.

### Novelty / the trigger system

A **trigger** is a narrative thread — a cluster of headlines about the same evolving event. New headlines are matched against active triggers using **hybrid Jaccard similarity**: 70% named-entity overlap + 30% keyword overlap, after canonicalising aliases (`federal reserve → fed`, `strait of hormuz → hormuz`). The match threshold is `SIMILARITY_THRESHOLD = 0.4`, with a tighter `CORE_ANCHOR_THRESHOLD` when two headlines share a core entity pair.

Triggers have a lifecycle driven by age:

| State | Age | Behaviour |
|-------|-----|-----------|
| `ACTIVE`   | 0–24h | Fully live; participates in matching |
| `FADING`   | 1–7d  | Still matches, lower impact |
| `STALE`    | 7–30d | Matches but won't fire new alerts |
| `ARCHIVED` | 30d+  | Inert |

Crucially, **only the first appearance of a narrative tightens gates and fires an alert** — follow-up coverage is recognised as belonging to an existing trigger and suppressed. Two triggers with the same entities but *opposite strong directions* (escalation vs de-escalation) are deliberately kept as separate threads. This combination is what brought the alert rate down from 37.6% to a defensible fraction.

### The Postgres boundary

When a novel trigger fires, two things happen, both landing in Postgres:

1. **Outcomes are scheduled** — rows in `trigger_outcomes` for each `(asset, horizon)` in `TRACKED_ASSETS = (BTCUSDT, ETHUSDT)` × `TRACKED_HORIZONS_MIN = (60, 240, 1440)`. Each starts `pending`.
2. **An alert is inserted** — into `alerts`, with computed priors attached as a JSONB column.

Postgres is the **single source of truth and the integration boundary** between this producer and any downstream consumer. The alert insert triggers a `NOTIFY` on channel `alerts_new` — but notably, **the NOTIFY is fired by a Postgres trigger function (`notify_alert_inserted`), not by application code**. This is deliberate: application code can forget to publish; a database trigger cannot. A consumer `LISTEN`s on the channel and fetches new alerts by id. The producer never makes a trading decision — it emits measurable signals and stops.

### Measurement & the feedback loop

An **outcomes worker** (`scripts/outcomes_worker.py`) polls for due `pending` outcomes and resolves each against hourly OHLCV in `price_snapshots`, within `PRICE_MATCH_TOLERANCE_MIN = 90` minutes of the target timestamp, giving up after `MAX_RESOLUTION_ATTEMPTS = 5`. It records both the raw return and the **excess return** over the asset's prior-window drift (see detrending below).

The **forecaster** (`pipeline/forecaster.py`) aggregates resolved outcomes into priors keyed by `(signature_root, direction, asset, horizon_minutes)`, dropping any cell with fewer than `MIN_N = 5` samples. Those priors attach to the *next* alert with the same signature — closing the loop. The system gets more informative as it accumulates outcomes, without any retraining step.

---

## Design decisions that matter

### As-of-fire-time priors (the anti-lookahead guarantee)

`priors_for_trigger(before=fire_time)` filters the outcome history to only those outcomes whose trigger fired *strictly before* the alert being priced. Without this single constraint, any backtest silently leaks future information into past predictions and every prior looks brilliant. This one parameter is the difference between an evaluation you can trust and one that lies to you. Live alerts pass wall-clock now; backtest/replay alerts pass the historical fire time.

### Detrending (separating signal from regime)

A prior that says "after geopolitical-escalation news, BTC rises 0.5% at 24h" is worthless if BTC was rising 0.5% *anyway* because of a bull regime. So each outcome stores three numbers: the raw `return_pct`, the `baseline_return_pct` (what the asset did in the H-minute window *before* the trigger fired), and the `excess_return_pct` (raw minus baseline). The forecaster can aggregate over either.

This matters because of what it revealed: raw priors showed **59.5% sign-agreement**, but detrended priors dropped to **49.4%** — coin-flip. Roughly ten points of apparent signal was market drift, not headline-conditional knowledge. The detrending column is what makes that confound visible instead of hidden. (See the [calibration discussion](#calibration-the-does-this-work-question).)

### Historical replay with time-mocking

To evaluate without waiting months for live data, the system replays historical headlines through the **exact same** `process_headline` code path, injecting `now=published_at` so the trigger state machine computes age and decay against the historical clock rather than wall time. Replay runs against an isolated `replay` Postgres schema (selected via `search_path`), so it never contaminates live data, and is resumable from a checkpoint table.

The key property: **production code is unchanged**. The `now` parameter defaults to wall-clock and is a no-op for live callers; only replay passes it. There's no separate "backtest mode" with its own subtly-different logic to drift out of sync — the thing being tested is the thing that runs in production.

### Idempotent, additive migrations

Schema changes are plain SQL (`CREATE … IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) applied on every container start by the entrypoint. Re-running is a sub-second no-op, so a redeploy self-migrates and there's no out-of-band "remember to run the migration" step. New columns are added *alongside* existing ones with a backfill script, never replacing them, so historical data stays comparable across schema versions. Detrending was added exactly this way.

### Connection-per-cycle for long-running workers

The outcomes worker opens a **fresh DB connection each loop cycle** rather than holding one open. This came from a production incident: a worker held a single connection across a 9-hour loop, the connection silently degraded, and `SELECT`s began returning empty result sets *without raising an error* — the worker cheerfully logged `scanned=0` while real work sat undone. Opening a fresh connection per cycle costs nothing at this poll rate and is immune to the entire bug class. A related fix: the worker now commits explicitly and increments its counters only after the commit, so its reported stats reflect what's actually persisted, not what it intended to persist.

---

## Calibration: the "does this work?" question

The whole system is built to answer one falsifiable question, and a [calibration harness](../scripts/calibrate_forecaster.py) answers it on a schedule. It does a **chronological** train/test split (never random — random splits leak the future), builds priors from the train set, and scores them against the test set on sign-agreement and MAE. It can aggregate at two granularities (`signature_root` for production keys, `category` for a coarser diagnostic view) and over either metric (`return_pct` or `excess_return_pct`).

A daily `calibration-history` job records per-cell results into `calibration_runs` / `calibration_cells`, so the signal/noise question is answered from a *trend over time and across regimes* rather than a single hopeful snapshot. This is explicitly a **temporary diagnostic** — the migration documents the teardown procedure for when the question is settled.

Current verdict: detrended priors are **not yet predictive** on the data collected so far. This is the expected outcome on a short window dominated by one narrative regime, and the system reports it plainly rather than surfacing a number that only looks good in-regime.

---

## Services

The system runs as five Docker Compose services sharing one image:

| Service | Role |
|---------|------|
| `news-classifier`     | The live pipeline: collect → classify → direction → trigger → schedule + alert |
| `outcomes-worker`     | Resolves pending outcomes against price data each cycle |
| `price-backfill`      | Keeps `price_snapshots` fresh (hourly, idempotent) so outcomes can resolve |
| `calibration-history` | Daily calibration snapshots (temporary diagnostic) |
| `eth-capture`         | Market-state snapshots: spot, orderbook, derivatives, options |

Migrations run on startup via the shared entrypoint, so any service that starts can bring the schema up to date; the `IF NOT EXISTS` guards make concurrent startup safe.

---

## What I'd build next

In rough priority order, and deliberately *not* yet built because the calibration verdict should come first:

1. **The consumer** (`crypto-yield-management-system`) — reads alerts via `LISTEN/NOTIFY` and applies deterministic trader + risk-manager logic. The signal/decision/risk separation is intentional: this repo generates signals; decisions live downstream.
2. **On-chain features** — condition outcomes on regime descriptors (realized profit, MVRV, derivatives long/short skew) via a provider like Glassnode, so priors can be regime-aware. Only worth it if calibration first shows headline priors carry *any* signal.
3. **A deterministic technical-analysis feature layer** — record RSI, volume-vs-average, and trend position at fire time as additional conditioning variables on outcomes.

The throughline: every extension stays **deterministic and measurable**. The value of this system is not any single model — it's that it can tell you the truth about whether it works.
