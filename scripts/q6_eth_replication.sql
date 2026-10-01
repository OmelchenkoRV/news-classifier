-- =====================================================================
-- Q6: ETH replication of the F2 funding-momentum signal
--
-- WHY THIS TEST
--   F2 on BTCUSDT produced the project's first contiguous winning
--   region (docs/FINDINGS_funding_defensive.md). The load-bearing
--   evidence is 32 firings in one era, found post-hoc across a swept
--   grid. Replicating on a SECOND ASSET with the SAME parameters, no
--   re-tuning, is the cheapest near-independent check available.
--
--   PASS  → ETH shows a comparable contiguous edge → real effect
--   FAIL  → BTC result was probably grid-fitting → joins the null pile
--
-- HONEST LIMIT ON "INDEPENDENCE"
--   BTC and ETH are ~0.8 correlated and crash together. This is a
--   REPLICATION, not an independent sample. Q6d below quantifies the
--   overlap: if ETH fires on the same days as BTC, a matching result is
--   nearly the same observation twice, and should be discounted
--   accordingly. Read Q6d BEFORE celebrating Q6b.
--
-- FROZEN PARAMETERS — do not re-tune for ETH. Re-tuning would convert
-- this from a test into a second fitting exercise.
--     signal:    fr <= trailing_mean(base_win) - 0.0001
--     base_win:  7, 14, 30   (as swept on BTC)
--     horizon:   7, 14, 21, 30 days
--     drawdown:  10%, 15%, 20%
--
-- METHOD FIX carried in from Q5: era baselines differ enormously
-- (31.5% / 11.4% / 16.0% on BTC). A single pooled baseline HIDES the
-- effect. Every comparison below is era-specific.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q6a. Coverage + distribution sanity for ETH.
-- Confirms ETH funding looks structurally like BTC's (resting default
-- at 0.0001, wide tails) rather than something categorically different.
-- ---------------------------------------------------------------------
SELECT symbol,
       MIN(funding_time)::date AS first_day,
       MAX(funding_time)::date AS last_day,
       COUNT(*)                AS n_rows,
       ROUND(MIN(funding_rate)::numeric, 6) AS min_fr,
       ROUND(MAX(funding_rate)::numeric, 6) AS max_fr,
       COUNT(*) FILTER (WHERE funding_rate = 0.0001) AS at_default,
       COUNT(*) FILTER (WHERE funding_rate < 0)      AS negative
FROM funding_history
GROUP BY symbol ORDER BY symbol;


-- ---------------------------------------------------------------------
-- Q6b. THE REPLICATION — ETH sweep with ERA-SPECIFIC baselines built in.
--   Each row shows the signal's hit rate AND the matching era baseline,
--   so no post-hoc joining is needed.
-- ---------------------------------------------------------------------
WITH px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d, close AS c
    FROM price_snapshots
    WHERE symbol = 'ETHUSDT'
    ORDER BY timestamp::date, timestamp DESC
),
fund AS (
    SELECT funding_time::date AS d, AVG(funding_rate) AS fr
    FROM funding_history
    WHERE symbol = 'ETHUSDT'
    GROUP BY 1
),
enriched AS (
    SELECT d, fr,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN  7 PRECEDING AND 1 PRECEDING) AS b7,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS b14,
           AVG(fr) OVER (ORDER BY d ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING) AS b30
    FROM fund
),
fires AS (
    SELECT d,  7 AS base_win FROM enriched WHERE b7  IS NOT NULL AND fr <= b7  - 0.0001
    UNION ALL
    SELECT d, 14            FROM enriched WHERE b14 IS NOT NULL AND fr <= b14 - 0.0001
    UNION ALL
    SELECT d, 30            FROM enriched WHERE b30 IS NOT NULL AND fr <= b30 - 0.0001
),
grid AS (
    SELECT h, t FROM unnest(ARRAY[7,14,21,30]) AS h
    CROSS JOIN unnest(ARRAY[0.10,0.15,0.20])   AS t
),
era_of AS (
    SELECT d,
           CASE WHEN d < '2023-01-01' THEN '2019-2022'
                WHEN d < '2025-07-01' THEN '2023-2025H1'
                ELSE '2025H2-2026' END AS era
    FROM px
),
-- outcome for EVERY day (used for both signal rows and baselines)
outcomes AS (
    SELECT p.d, e.era, g.h, g.t,
           CASE WHEN 1 - (SELECT MIN(x.c) FROM px x
                           WHERE x.d > p.d
                             AND x.d <= p.d + (g.h || ' days')::interval)
                        / p.c >= g.t
                THEN 1 ELSE 0 END AS hit
    FROM px p
    JOIN era_of e ON e.d = p.d
    CROSS JOIN grid g
    WHERE (SELECT MIN(x.c) FROM px x
            WHERE x.d > p.d
              AND x.d <= p.d + (g.h || ' days')::interval) IS NOT NULL
),
baselines AS (
    SELECT era, h, t,
           COUNT(*) AS n_days,
           ROUND((100.0*SUM(hit)/COUNT(*))::numeric,1) AS base_rate_pct
    FROM outcomes GROUP BY era, h, t
),
signal AS (
    SELECT f.base_win, o.era, o.h, o.t,
           COUNT(*) AS n_fired,
           SUM(o.hit) AS n_hit,
           ROUND((100.0*SUM(o.hit)/COUNT(*))::numeric,1) AS hit_rate_pct
    FROM fires f
    JOIN outcomes o ON o.d = f.d
    GROUP BY f.base_win, o.era, o.h, o.t
)
SELECT s.base_win, s.era, s.h AS fwd_days,
       ROUND((100*s.t)::numeric,0) AS drawdown_pct,
       s.n_fired, s.n_hit, s.hit_rate_pct,
       b.base_rate_pct,
       ROUND((s.hit_rate_pct - b.base_rate_pct)::numeric,1) AS edge_pp
FROM signal s
JOIN baselines b ON b.era = s.era AND b.h = s.h AND b.t = s.t
ORDER BY s.base_win, s.era, s.h, s.t;


-- ---------------------------------------------------------------------
-- Q6c. HEADLINE CELL side by side — BTC vs ETH at the frozen parameters
--      (base_win=7, 14d horizon, 10% drawdown), era by era.
--      This is the one table to read first.
-- ---------------------------------------------------------------------
WITH RECURSIVE syms(sym) AS (VALUES ('BTCUSDT'),('ETHUSDT')),
px AS (
    SELECT DISTINCT ON (symbol, timestamp::date)
           symbol, timestamp::date AS d, close AS c
    FROM price_snapshots
    WHERE symbol IN ('BTCUSDT','ETHUSDT')
    ORDER BY symbol, timestamp::date, timestamp DESC
),
fund AS (
    SELECT symbol, funding_time::date AS d, AVG(funding_rate) AS fr
    FROM funding_history GROUP BY 1,2
),
enriched AS (
    SELECT symbol, d, fr,
           AVG(fr) OVER (PARTITION BY symbol ORDER BY d
                         ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS b7
    FROM fund
),
fires AS (
    SELECT symbol, d FROM enriched WHERE b7 IS NOT NULL AND fr <= b7 - 0.0001
),
outcomes AS (
    SELECT p.symbol, p.d,
           CASE WHEN p.d < '2023-01-01' THEN '2019-2022'
                WHEN p.d < '2025-07-01' THEN '2023-2025H1'
                ELSE '2025H2-2026' END AS era,
           CASE WHEN 1 - (SELECT MIN(x.c) FROM px x
                           WHERE x.symbol = p.symbol AND x.d > p.d
                             AND x.d <= p.d + INTERVAL '14 days') / p.c >= 0.10
                THEN 1 ELSE 0 END AS hit
    FROM px p
    WHERE (SELECT MIN(x.c) FROM px x
            WHERE x.symbol = p.symbol AND x.d > p.d
              AND x.d <= p.d + INTERVAL '14 days') IS NOT NULL
)
SELECT o.symbol, o.era,
       COUNT(*) FILTER (WHERE f.d IS NOT NULL)                       AS n_fired,
       SUM(o.hit) FILTER (WHERE f.d IS NOT NULL)                     AS n_hit,
       ROUND((100.0*SUM(o.hit) FILTER (WHERE f.d IS NOT NULL)
              / NULLIF(COUNT(*) FILTER (WHERE f.d IS NOT NULL),0))::numeric,1)
                                                                     AS signal_pct,
       ROUND((100.0*SUM(o.hit)/COUNT(*))::numeric,1)                 AS base_pct
FROM outcomes o
LEFT JOIN fires f ON f.symbol = o.symbol AND f.d = o.d
GROUP BY o.symbol, o.era
ORDER BY o.symbol, o.era;


-- ---------------------------------------------------------------------
-- Q6d. INDEPENDENCE CHECK — read this BEFORE trusting Q6b/Q6c.
--   How often do BTC and ETH fire on the SAME day?
--   High overlap → ETH is not a second sample, it is the same events
--   re-measured, and a matching result adds little evidence.
-- ---------------------------------------------------------------------
WITH fund AS (
    SELECT symbol, funding_time::date AS d, AVG(funding_rate) AS fr
    FROM funding_history GROUP BY 1,2
),
enriched AS (
    SELECT symbol, d, fr,
           AVG(fr) OVER (PARTITION BY symbol ORDER BY d
                         ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS b7
    FROM fund
),
fires AS (
    SELECT symbol, d FROM enriched WHERE b7 IS NOT NULL AND fr <= b7 - 0.0001
),
btc AS (SELECT d FROM fires WHERE symbol='BTCUSDT'),
eth AS (SELECT d FROM fires WHERE symbol='ETHUSDT')
SELECT (SELECT COUNT(*) FROM btc)                                AS btc_fires,
       (SELECT COUNT(*) FROM eth)                                AS eth_fires,
       (SELECT COUNT(*) FROM btc JOIN eth USING (d))             AS same_day_both,
       ROUND((100.0*(SELECT COUNT(*) FROM btc JOIN eth USING (d))
              / NULLIF((SELECT COUNT(*) FROM eth),0))::numeric,1) AS pct_eth_overlapping,
       -- within ±2 days, a looser and more realistic notion of "same event"
       (SELECT COUNT(DISTINCT e.d) FROM eth e
         WHERE EXISTS (SELECT 1 FROM btc b
                        WHERE b.d BETWEEN e.d - 2 AND e.d + 2))    AS eth_within_2d_of_btc;


-- ---------------------------------------------------------------------
-- HOW TO READ IT
--
--  * Q6d FIRST. If >70% of ETH firings coincide with BTC firings, treat
--    a matching Q6b result as ~one observation, not two. Independence
--    is what gives replication its power; without it, agreement is
--    nearly guaranteed and proves little.
--
--  * Then Q6c. The 2023-2025H1 row is decisive — that is the era
--    carrying the entire BTC result (32 firings, +16.7pp). If ETH shows
--    a comparable edge there with reasonable n, the effect is more
--    likely real. If ETH is flat or negative there, the BTC result was
--    probably the grid finding a pocket.
--
--  * Then Q6b for contiguity. One good ETH cell means nothing; the BTC
--    result's strength was 32/36 cells positive and monotone in window.
--    Demand the same shape, not the same peak.
--
--  * PARAMETERS ARE FROZEN. If ETH fails and the temptation arises to
--    "just check base_win=20" — that is the moment this stops being a
--    test. Note the failure and stop.
--
--  * PRIOR on record: BTC/ETH funding regimes are similar enough that I
--    expect substantial overlap (Q6d) and therefore a *directionally
--    similar* ETH result. The informative outcome is if ETH FAILS
--    despite overlap — that would be strong evidence against.
-- ---------------------------------------------------------------------
