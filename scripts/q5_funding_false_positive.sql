-- =====================================================================
-- Q5: Does a funding-rate signal predict DRAWDOWNS?
--     Full history 2019-09 → present — TWO bear markets (2022, 2026),
--     not one cycle. Best-powered free test this project has.
--
-- PROTOCOL (same one that killed the ETF-flow signal)
--   sweep a grid + compare against the UNCONDITIONAL BASE RATE.
--   A hit rate near baseline = no information. Isolated winning cells
--   = noise. Only a CONTIGUOUS region counts.
--
-- DIRECTION MATTERS: this is a DEFENSIVE signal, so the outcome is a
--   DRAWDOWN (min close ≤ −T within H days), not an upside move. The
--   flow test measured upside; this measures downside. Do not mix them.
--
-- TWO VARIANTS (per PLAN_leverage_defensive.md Step 2)
--   F1  LEVEL:    funding persistently negative (longs capitulating)
--   F2  MOMENTUM: sharp DROP in funding vs its trailing baseline
--
--   MEASURED REASON F2 IS FAVOURED: Binance funding saturates at the
--   ±0.01%/8h cap (0.0001). In Aug-2026 it pinned there for three days
--   straight — LEVEL cannot express "more extreme" in exactly the
--   regimes that matter. MOMENTUM has no such ceiling.
--   (see docs/FINDINGS_aug2026_rally.md, Finding 3)
--
-- PREREQ
--   python -m collectors.funding_backfill --create-table
--   python -m collectors.funding_backfill --symbols BTCUSDT,ETHUSDT \
--       --start 2019-09-01
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q5-pre. COVERAGE CHECK — run FIRST.
-- Must span 2019-09 → now with the 2022 crash months populated.
-- Funding posts every 8h → expect ~3 rows/day (~1095/yr).
-- ---------------------------------------------------------------------
SELECT symbol,
       MIN(funding_time)::date AS first_day,
       MAX(funding_time)::date AS last_day,
       COUNT(*)                AS n_rows,
       COUNT(*) FILTER (WHERE funding_time BETWEEN '2022-05-01' AND '2022-12-01')
                               AS rows_2022_crash,
       COUNT(*) FILTER (WHERE funding_time BETWEEN '2026-05-01' AND '2026-07-01')
                               AS rows_2026_crash
FROM funding_history
GROUP BY symbol;

-- Sanity on saturation: how often does funding sit AT the cap?
-- If this is a large fraction, the LEVEL variant is structurally
-- handicapped and the F2/momentum result is the one to trust.
SELECT symbol,
       COUNT(*)                                                  AS n,
       COUNT(*) FILTER (WHERE funding_rate >=  0.0000999)        AS at_pos_cap,
       COUNT(*) FILTER (WHERE funding_rate <= -0.0000999)        AS at_neg_cap,
       ROUND((100.0 * COUNT(*) FILTER (WHERE ABS(funding_rate) >= 0.0000999)
              / COUNT(*))::numeric, 2)                           AS pct_at_cap
FROM funding_history
GROUP BY symbol;


-- ---------------------------------------------------------------------
-- Q5. THE SWEEP — both variants, one grid.
--   baseline window B ∈ {7, 14, 30} days   (trailing mean of funding)
--   horizon        H ∈ {7, 14, 21, 30} days
--   drawdown       T ∈ {10%, 15%, 20%}
-- ---------------------------------------------------------------------
WITH daily_px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d, close AS btc_close
    FROM price_snapshots
    WHERE symbol = 'BTCUSDT'
    ORDER BY timestamp::date, timestamp DESC
),
daily_funding AS (
    -- collapse 3 fundings/day to a daily mean
    SELECT funding_time::date AS d,
           AVG(funding_rate)  AS fr
    FROM funding_history
    WHERE symbol = 'BTCUSDT'
    GROUP BY 1
),
enriched AS (
    SELECT d, fr,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN  7 PRECEDING AND 1 PRECEDING) AS base7,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS base14,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING) AS base30,
           -- persistence: was funding negative on each of the last 3 days?
           (fr < 0
            AND LAG(fr,1) OVER (ORDER BY d) < 0
            AND LAG(fr,2) OVER (ORDER BY d) < 0) AS neg3
    FROM daily_funding
),
-- long-form: one row per (date, variant, baseline-window)
fires AS (
    -- F1 LEVEL: funding negative 3 days running (longs capitulating)
    SELECT d, 'F1_level'::text AS variant, b AS base_win
    FROM enriched CROSS JOIN unnest(ARRAY[7,14,30]) AS b
    WHERE neg3
    UNION ALL
    -- F2 MOMENTUM: funding drops ≥ 1.5 std-equivalents below its own
    -- trailing baseline. Using a simple multiplicative rule: today's
    -- funding is at least 0.0001 BELOW the trailing mean (i.e. a full
    -- cap-width swing down) — scale-free w.r.t. the saturation ceiling.
    SELECT d, 'F2_momentum', 7  FROM enriched WHERE base7  IS NOT NULL AND fr <= base7  - 0.0001
    UNION ALL
    SELECT d, 'F2_momentum', 14 FROM enriched WHERE base14 IS NOT NULL AND fr <= base14 - 0.0001
    UNION ALL
    SELECT d, 'F2_momentum', 30 FROM enriched WHERE base30 IS NOT NULL AND fr <= base30 - 0.0001
),
grid AS (
    SELECT h, t FROM unnest(ARRAY[7,14,21,30]) AS h
    CROSS JOIN unnest(ARRAY[0.10,0.15,0.20])   AS t
),
evaluated AS (
    SELECT f.d, f.variant, f.base_win, g.h, g.t,
           p.btc_close AS px0,
           (SELECT MIN(x.btc_close) FROM daily_px x
             WHERE x.d >  f.d
               AND x.d <= f.d + (g.h || ' days')::interval) AS px_min
    FROM fires f
    JOIN daily_px p ON p.d = f.d
    CROSS JOIN grid g
)
SELECT variant,
       base_win,
       h                                            AS fwd_days,
       ROUND((100*t)::numeric, 0)                   AS drawdown_pct,
       COUNT(*)                                     AS n_fired,
       COUNT(*) FILTER (WHERE 1 - px_min/px0 >= t)  AS n_hit,
       ROUND((100.0 * COUNT(*) FILTER (WHERE 1 - px_min/px0 >= t)
              / NULLIF(COUNT(*),0))::numeric, 1)    AS hit_rate_pct
FROM evaluated
WHERE px_min IS NOT NULL
GROUP BY variant, base_win, h, t
ORDER BY variant, base_win, h, t;


-- ---------------------------------------------------------------------
-- Q5b. THE BASELINE — unconditional drawdown rate, same span.
--   Q5.hit_rate_pct must materially BEAT these, across a contiguous
--   region, or funding carries no defensive information.
-- ---------------------------------------------------------------------
WITH daily_px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d, close AS btc_close
    FROM price_snapshots
    WHERE symbol = 'BTCUSDT'
    ORDER BY timestamp::date, timestamp DESC
),
span AS (
    SELECT d, btc_close FROM daily_px
    WHERE d >= (SELECT MIN(funding_time)::date FROM funding_history)
      AND d <= (SELECT MAX(funding_time)::date FROM funding_history)
),
grid AS (
    SELECT h, t FROM unnest(ARRAY[7,14,21,30]) AS h
    CROSS JOIN unnest(ARRAY[0.10,0.15,0.20])   AS t
),
evaluated AS (
    SELECT s.d, g.h, g.t, s.btc_close AS px0,
           (SELECT MIN(x.btc_close) FROM span x
             WHERE x.d >  s.d
               AND x.d <= s.d + (g.h || ' days')::interval) AS px_min
    FROM span s CROSS JOIN grid g
)
SELECT h                                            AS fwd_days,
       ROUND((100*t)::numeric, 0)                   AS drawdown_pct,
       COUNT(*)                                     AS n_days,
       ROUND((100.0 * COUNT(*) FILTER (WHERE 1 - px_min/px0 >= t)
              / NULLIF(COUNT(*),0))::numeric, 1)    AS base_rate_pct
FROM evaluated
WHERE px_min IS NOT NULL
GROUP BY h, t
ORDER BY h, t;


-- ---------------------------------------------------------------------
-- Q5c. REGIME SPLIT — the check the ETF-flow test COULDN'T do.
--   Flows had 602 days / one cycle. Funding has two bears. If the
--   signal only works in 2022 and not 2026 (or vice versa), it is
--   regime-fitted, not real. This is the out-of-sample test that
--   inverted the bull-only momentum result.
-- ---------------------------------------------------------------------
WITH daily_px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d, close AS btc_close
    FROM price_snapshots WHERE symbol='BTCUSDT'
    ORDER BY timestamp::date, timestamp DESC
),
daily_funding AS (
    SELECT funding_time::date AS d, AVG(funding_rate) AS fr
    FROM funding_history WHERE symbol='BTCUSDT' GROUP BY 1
),
enriched AS (
    SELECT d, fr,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS base14
    FROM daily_funding
),
fires AS (
    SELECT d FROM enriched WHERE base14 IS NOT NULL AND fr <= base14 - 0.0001
),
evaluated AS (
    SELECT f.d,
           CASE WHEN f.d < '2023-01-01' THEN '2019-2022'
                WHEN f.d < '2025-07-01' THEN '2023-2025H1'
                ELSE '2025H2-2026' END AS era,
           p.btc_close AS px0,
           (SELECT MIN(x.btc_close) FROM daily_px x
             WHERE x.d > f.d AND x.d <= f.d + INTERVAL '14 days') AS px_min
    FROM fires f JOIN daily_px p ON p.d = f.d
)
SELECT era,
       COUNT(*)                                          AS n_fired,
       COUNT(*) FILTER (WHERE 1 - px_min/px0 >= 0.10)    AS n_hit_10pct,
       ROUND((100.0 * COUNT(*) FILTER (WHERE 1 - px_min/px0 >= 0.10)
              / NULLIF(COUNT(*),0))::numeric, 1)         AS hit_rate_pct
FROM evaluated
WHERE px_min IS NOT NULL
GROUP BY era ORDER BY era;


-- ---------------------------------------------------------------------
-- HOW TO READ IT
--
--  * Q5 hit_rate vs Q5b base_rate, cell by cell. Near baseline = null.
--  * n_fired < ~15 in a cell → uninformative, ignore it.
--  * Contiguity is the test. The price-breaker had 4/36 lucky cells and
--    was correctly rejected. So did the flow signal's two "best" cells.
--  * Q5c is the real discriminator, and it is the thing the flow test
--    could not perform: if the edge lives in ONE era only, it is
--    regime-fitting. Full-cycle testing is what inverted the bull-only
--    momentum result — the single most valuable check this project has.
--
--  * PRIOR, on record before running: F1 (level) nulls — funding was
--    COINCIDENT in June-2026, flipping negative only on the capitulation
--    day. F2 (momentum) is the live question. Six prior nulls say expect
--    a seventh; the honest reason to run it anyway is that this is the
--    only candidate with two bear markets of clean data behind it.
--
--  * If a cell looks spectacular, suspect lookahead FIRST. All windows
--    here use STRICTLY PRIOR rows (`ROWS BETWEEN n PRECEDING AND 1
--    PRECEDING`) and strictly future outcomes (`x.d > f.d`) — but check
--    anyway. That is how the 746× price-breaker bug was caught.
-- ---------------------------------------------------------------------
