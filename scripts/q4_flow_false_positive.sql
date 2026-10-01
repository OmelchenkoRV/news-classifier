-- =====================================================================
-- Q4: False-positive rate of the ETF-flow crossover signal
--     Full history: 2024-01-11 → present (602 trading days)
--
-- WHY A SWEEP, NOT ONE CELL
--   FINDINGS_aug2026_rally.md showed the verdict flipping between
--   FALSE POSITIVE and HIT on a ONE-DAY change to the forward window
--   (Aug-4 firing vs Aug-17 firing). A single hard-coded cell is
--   therefore worthless. We sweep and demand a CONTIGUOUS winning
--   region — the same discipline that killed the K=1 momentum result.
--
-- WHY A BASELINE IS MANDATORY (Q4b)
--   "55% false positives" means NOTHING alone. If 55% of *all* days
--   precede a 10% move, the signal carries zero information. The test
--   is whether the signal's hit rate BEATS the unconditional base rate.
--   Run Q4b and compare. Skipping it is how people fool themselves.
--
-- SCHEMA (verified)
--   price_snapshots(symbol, timestamp, open, high, low, close, volume)
--       hourly candles → collapsed to daily last-close below
--   eth_etf_flows(flow_date, ticker, net_flow_usd, ...)
--
-- SIGNAL   N-day BTC net-flow sum crosses negative → positive
-- OUTCOME  max close within H days ≥ threshold above close at signal
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q4-pre. COVERAGE CHECK — run this FIRST.
-- Confirms price_snapshots actually spans the flow history. If BTCUSDT
-- starts after 2024-01-11, Q4 silently evaluates a truncated sample.
-- ---------------------------------------------------------------------
SELECT symbol,
       MIN(timestamp)::date AS first_day,
       MAX(timestamp)::date AS last_day,
       COUNT(DISTINCT timestamp::date) AS n_days
FROM price_snapshots
WHERE symbol IN ('BTCUSDT','ETHUSDT')
GROUP BY symbol;


-- ---------------------------------------------------------------------
-- Q4. THE SWEEP
--   flow window N ∈ {7, 14, 30} trading rows
--   horizon    H ∈ {7, 14, 21, 30} calendar days
--   threshold  T ∈ {5%, 10%, 15%}
-- ---------------------------------------------------------------------
WITH daily_px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d,
           close          AS btc_close
    FROM price_snapshots
    WHERE symbol = 'BTCUSDT'
    ORDER BY timestamp::date, timestamp DESC   -- last close of each day
),
flows AS (
    SELECT flow_date,
           SUM(net_flow_usd) OVER (ORDER BY flow_date
               ROWS BETWEEN  6 PRECEDING AND CURRENT ROW) AS w7,
           SUM(net_flow_usd) OVER (ORDER BY flow_date
               ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS w14,
           SUM(net_flow_usd) OVER (ORDER BY flow_date
               ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS w30
    FROM eth_etf_flows
    WHERE ticker = 'BITCOIN-TOTAL'
),
long_form AS (
    SELECT flow_date, 7  AS win, w7  AS val,
           LAG(w7)  OVER (ORDER BY flow_date) AS prev FROM flows
    UNION ALL
    SELECT flow_date, 14, w14,
           LAG(w14) OVER (ORDER BY flow_date) FROM flows
    UNION ALL
    SELECT flow_date, 30, w30,
           LAG(w30) OVER (ORDER BY flow_date) FROM flows
),
fires AS (
    SELECT flow_date, win
    FROM long_form
    WHERE val > 0 AND prev <= 0            -- negative → positive crossover
),
grid AS (
    SELECT h, t
    FROM unnest(ARRAY[7,14,21,30])            AS h
    CROSS JOIN unnest(ARRAY[0.05,0.10,0.15])  AS t
),
evaluated AS (
    SELECT f.flow_date, f.win, g.h, g.t,
           p.btc_close AS px0,
           (SELECT MAX(x.btc_close)
              FROM daily_px x
             WHERE x.d >  f.flow_date
               AND x.d <= f.flow_date + (g.h || ' days')::interval) AS px_max
    FROM fires f
    JOIN daily_px p ON p.d = f.flow_date
    CROSS JOIN grid g
)
SELECT win                                        AS flow_window,
       h                                          AS fwd_days,
       ROUND((100*t)::numeric, 0)                 AS thresh_pct,
       COUNT(*)                                   AS n_fired,
       COUNT(*) FILTER (WHERE px_max/px0 - 1 >= t) AS n_hit,
       ROUND((100.0 * COUNT(*) FILTER (WHERE px_max/px0 - 1 >= t)
              / NULLIF(COUNT(*),0))::numeric, 1)  AS hit_rate_pct,
       ROUND((100.0 * COUNT(*) FILTER (WHERE px_max/px0 - 1 <  t)
              / NULLIF(COUNT(*),0))::numeric, 1)  AS false_pos_pct
FROM evaluated
WHERE px_max IS NOT NULL          -- drop firings too recent to evaluate
GROUP BY win, h, t
ORDER BY win, h, t;


-- ---------------------------------------------------------------------
-- Q4b. THE BASELINE — unconditional base rate.
--   For EVERY trading day in the same span, how often did a ≥T move
--   occur within H days? Q4's hit_rate_pct must BEAT these numbers,
--   materially and across a contiguous region, or the signal is noise.
-- ---------------------------------------------------------------------
WITH daily_px AS (
    SELECT DISTINCT ON (timestamp::date)
           timestamp::date AS d, close AS btc_close
    FROM price_snapshots
    WHERE symbol = 'BTCUSDT'
    ORDER BY timestamp::date, timestamp DESC
),
span AS (   -- restrict to the ETF-era window so it's comparable
    SELECT d, btc_close FROM daily_px
    WHERE d >= (SELECT MIN(flow_date) FROM eth_etf_flows)
      AND d <= (SELECT MAX(flow_date) FROM eth_etf_flows)
),
grid AS (
    SELECT h, t
    FROM unnest(ARRAY[7,14,21,30])            AS h
    CROSS JOIN unnest(ARRAY[0.05,0.10,0.15])  AS t
),
evaluated AS (
    SELECT s.d, g.h, g.t, s.btc_close AS px0,
           (SELECT MAX(x.btc_close) FROM span x
             WHERE x.d >  s.d
               AND x.d <= s.d + (g.h || ' days')::interval) AS px_max
    FROM span s CROSS JOIN grid g
)
SELECT h                                          AS fwd_days,
       ROUND((100*t)::numeric, 0)                 AS thresh_pct,
       COUNT(*)                                   AS n_days,
       ROUND((100.0 * COUNT(*) FILTER (WHERE px_max/px0 - 1 >= t)
              / NULLIF(COUNT(*),0))::numeric, 1)  AS base_rate_pct
FROM evaluated
WHERE px_max IS NOT NULL
GROUP BY h, t
ORDER BY h, t;


-- ---------------------------------------------------------------------
-- Q4c. (optional) Every individual firing, for eyeballing.
--   Fixed at the plan's original cell (7d window) so you can see which
--   dates fired and what followed — including the Aug-4 / Aug-17 pair
--   that motivated the sweep.
-- ---------------------------------------------------------------------
-- WITH daily_px AS (
--     SELECT DISTINCT ON (timestamp::date)
--            timestamp::date AS d, close AS btc_close
--     FROM price_snapshots WHERE symbol='BTCUSDT'
--     ORDER BY timestamp::date, timestamp DESC
-- ), flows AS (
--     SELECT flow_date,
--            SUM(net_flow_usd) OVER (ORDER BY flow_date
--                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS w7
--     FROM eth_etf_flows WHERE ticker='BITCOIN-TOTAL'
-- ), fires AS (
--     SELECT flow_date, w7,
--            LAG(w7) OVER (ORDER BY flow_date) AS prev FROM flows
-- )
-- SELECT f.flow_date,
--        ROUND((f.w7/1e6)::numeric,1) AS flow_7d_musd,
--        ROUND(p.btc_close::numeric,0) AS px0,
--        ROUND((100*((SELECT MAX(x.btc_close) FROM daily_px x
--                      WHERE x.d > f.flow_date
--                        AND x.d <= f.flow_date + INTERVAL '14 days')
--                    / p.btc_close - 1))::numeric,1) AS max_gain_14d_pct
-- FROM fires f JOIN daily_px p ON p.d = f.flow_date
-- WHERE f.w7 > 0 AND f.prev <= 0
-- ORDER BY f.flow_date;


-- ---------------------------------------------------------------------
-- HOW TO READ THE RESULT
--
--  * Compare Q4.hit_rate_pct against Q4b.base_rate_pct cell by cell.
--    Signal hit rate ≈ base rate  → NO INFORMATION. Null.
--    Signal materially above base → real, IF contiguous across cells.
--    One or two isolated winning cells → noise (the price-breaker had
--    4 lucky cells out of 36 and was correctly rejected).
--
--  * n_fired matters. A cell firing 3 times can hit 100% by luck.
--    Treat anything under ~15 firings as uninformative.
--
--  * Prediction on record before running: hit rates within a few points
--    of baseline, no contiguous region. If a cell looks spectacular,
--    suspect a lookahead bug FIRST — that is how the 746× price-breaker
--    result started.
--
--  * STRUCTURAL CAVEAT that no sweep can fix: this history is 602 days
--    covering ONE cycle, with NO 2022-bear coverage (ETFs launched
--    Jan-2024). Even a clean result here is a far weaker claim than the
--    funding test, which backfills to 2019.
-- ---------------------------------------------------------------------
