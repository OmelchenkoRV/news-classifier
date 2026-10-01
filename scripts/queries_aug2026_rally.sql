-- =====================================================================
-- Queries: August-2026 rally — ETF flows, positioning, and the
--          false-positive gate.
--
-- CONTEXT
--   The Aug-19→21 2026 rally (BTC +22% to ~$77k, ETH +25%) was reported
--   as (a) a Treasury-buyback liquidity shock, (b) a short squeeze, and
--   (c) returning ETF demand — with commentators claiming the ETF flow
--   rebuild "pointed to" the breakout. Q1–Q3 check that claim against
--   our own data. Q4 is the one that actually matters: the
--   false-positive rate across the full 602-day flow history.
--
-- BEFORE RUNNING
--   eth_etf_flows is only populated through the last collector run.
--   Refresh it first or the August windows come back empty:
--       python -m capture.etf_flows
--
-- SCHEMA USED (verified)
--   eth_etf_flows(captured_at, flow_date, ticker, net_flow_usd, aum_usd)
--       ticker ∈ {BITCOIN-TOTAL, ETHEREUM-TOTAL, SOLANA-TOTAL,
--                 HYPERLIQUID-TOTAL}
--   eth_snapshots(id, captured_at, spot_price, btc_price, eth_btc_ratio)
--   eth_derivatives(snapshot_id, exchange, funding_rate,
--                   open_interest_usd, long_short_ratio, mark_price)
--
--   NOTE: flows are trading-day only (no weekends/holidays), so a
--   "7 preceding rows" window is ~1.5 calendar weeks, not 7 days.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q0. Find the crypto candles table (name predates this analysis).
--     Q4 needs it for forward returns. Substitute the result into
--     the placeholders marked <<PRICE_TABLE>> below.
-- ---------------------------------------------------------------------
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name NOT LIKE 'eth_%'
ORDER BY table_name;

-- Then inspect its columns:
-- SELECT column_name, data_type FROM information_schema.columns
-- WHERE table_name = '<<PRICE_TABLE>>' ORDER BY ordinal_position;


-- ---------------------------------------------------------------------
-- Q1. LEAD/LAG — did ETF flows turn positive BEFORE the Aug-19 breakout?
--     Read for where flow_7d crosses negative → positive. If that
--     crossover precedes Aug 19, flows led. On/after = hindsight.
-- ---------------------------------------------------------------------
SELECT flow_date,
       ticker,
       net_flow_usd / 1e6 AS net_flow_musd,
       SUM(net_flow_usd) OVER (
           PARTITION BY ticker ORDER BY flow_date
           ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
       ) / 1e6 AS flow_3d_musd,
       SUM(net_flow_usd) OVER (
           PARTITION BY ticker ORDER BY flow_date
           ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
       ) / 1e6 AS flow_7d_musd
FROM eth_etf_flows
WHERE ticker IN ('BITCOIN-TOTAL', 'ETHEREUM-TOTAL')
  AND flow_date BETWEEN '2026-07-15' AND '2026-08-22'
ORDER BY ticker, flow_date;


-- ---------------------------------------------------------------------
-- Q2. POSITIONING through the squeeze, hourly-binned.
--
--     Short-squeeze fingerprint (mirror of the June crash):
--       long_short_ratio LOW (<~1, crowded shorts) BEFORE the move,
--       open interest FALLING as price RISES (shorts force-closed),
--       funding flipping positive as longs take over.
--     Contrast: if L/S was neutral and OI ROSE into the rally, that's
--     new leveraged longs entering — real demand, not a squeeze.
-- ---------------------------------------------------------------------
SELECT date_trunc('hour', s.captured_at) AS hour,
       d.exchange,
       AVG(s.spot_price)              AS eth_price,
       AVG(s.btc_price)               AS btc_price,
       AVG(d.funding_rate)            AS funding_rate,
       AVG(d.open_interest_usd) / 1e9 AS oi_busd,
       AVG(d.long_short_ratio)        AS long_short_ratio
FROM eth_snapshots s
JOIN eth_derivatives d ON d.snapshot_id = s.id
WHERE s.captured_at >= '2026-08-17'
  AND s.captured_at <  '2026-08-23'
GROUP BY 1, 2
ORDER BY 1, 2;


-- ---------------------------------------------------------------------
-- Q3. SIDE BY SIDE — daily price + positioning + flows, one row per day.
--     The shape you actually eyeball for lead/lag.
-- ---------------------------------------------------------------------
WITH flows AS (
    SELECT flow_date,
           SUM(CASE WHEN ticker = 'BITCOIN-TOTAL'  THEN net_flow_usd END) / 1e6 AS btc_flow_m,
           SUM(CASE WHEN ticker = 'ETHEREUM-TOTAL' THEN net_flow_usd END) / 1e6 AS eth_flow_m
    FROM eth_etf_flows
    WHERE flow_date >= '2026-08-01'
    GROUP BY flow_date
),
px AS (
    SELECT s.captured_at::date     AS d,
           AVG(s.spot_price)       AS eth_close,
           AVG(s.btc_price)        AS btc_close,
           AVG(d.long_short_ratio) AS ls_ratio,
           AVG(d.funding_rate)     AS funding
    FROM eth_snapshots s
    JOIN eth_derivatives d ON d.snapshot_id = s.id
    WHERE s.captured_at >= '2026-08-01'
    GROUP BY 1
)
SELECT px.d, px.eth_close, px.btc_close, px.ls_ratio, px.funding,
       flows.btc_flow_m, flows.eth_flow_m
FROM px
LEFT JOIN flows ON flows.flow_date = px.d
ORDER BY px.d;


-- =====================================================================
-- Q4. THE ONE THAT MATTERS — false-positive rate of the flow signal
--     across the FULL 602-day history (2024-01-11 → today).
--
--     Q1–Q3 describe one episode. This asks the only question a single
--     episode cannot answer: how often does the signal fire WITHOUT the
--     move following? Same gate that killed the price-breaker, the
--     news-systemic flag, and the bull-only momentum result.
--
--     SIGNAL: 7-trading-day BTC flow sum crosses negative → positive.
--     OUTCOME: forward 14-day BTC return > +10% (a "breakout").
--
--     REQUIRES <<PRICE_TABLE>> from Q0 — daily BTC closes over the full
--     history. eth_snapshots only starts ~June 2026, far too short.
--     Substitute your candles table and its column names below.
-- =====================================================================
WITH btc_flows AS (
    SELECT flow_date,
           net_flow_usd,
           SUM(net_flow_usd) OVER (
               ORDER BY flow_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
           ) AS flow_7d
    FROM eth_etf_flows
    WHERE ticker = 'BITCOIN-TOTAL'
),
signal AS (
    SELECT flow_date,
           flow_7d,
           LAG(flow_7d) OVER (ORDER BY flow_date) AS prev_flow_7d,
           -- fires the day the 7d sum crosses up through zero
           (flow_7d > 0 AND LAG(flow_7d) OVER (ORDER BY flow_date) <= 0)
               AS fired
    FROM btc_flows
),
daily_px AS (
    -- SUBSTITUTE: your candles table. Expected shape is one row per
    -- symbol per bar; adjust column names to match Q0's output.
    SELECT ts::date            AS d,
           AVG(close)          AS btc_close
    FROM <<PRICE_TABLE>>
    WHERE symbol = 'BTCUSDT'
    GROUP BY 1
),
evaluated AS (
    SELECT s.flow_date,
           s.flow_7d / 1e6 AS flow_7d_musd,
           p.btc_close     AS px_at_signal,
           -- best close in the following 14 calendar days
           (SELECT MAX(f.btc_close)
              FROM daily_px f
             WHERE f.d >  s.flow_date
               AND f.d <= s.flow_date + INTERVAL '14 days') AS px_max_fwd14,
           (SELECT MIN(f.btc_close)
              FROM daily_px f
             WHERE f.d >  s.flow_date
               AND f.d <= s.flow_date + INTERVAL '14 days') AS px_min_fwd14
    FROM signal s
    JOIN daily_px p ON p.d = s.flow_date
    WHERE s.fired
)
SELECT flow_date,
       ROUND(flow_7d_musd::numeric, 1)                       AS flow_7d_musd,
       ROUND(px_at_signal::numeric, 0)                        AS px_at_signal,
       ROUND((100.0 * (px_max_fwd14 / px_at_signal - 1))::numeric, 1) AS max_gain_pct_14d,
       ROUND((100.0 * (px_min_fwd14 / px_at_signal - 1))::numeric, 1) AS max_draw_pct_14d,
       CASE WHEN px_max_fwd14 / px_at_signal - 1 >= 0.10
            THEN 'HIT' ELSE 'FALSE POSITIVE' END             AS verdict
FROM evaluated
ORDER BY flow_date;


-- ---------------------------------------------------------------------
-- Q4b. The single number: what fraction of firings were false positives?
--      Wrap Q4's `evaluated` CTE and aggregate. A signal that fires 40
--      times and "hits" 6 is a false-alarm machine no matter how good
--      August 2026 looks in isolation.
-- ---------------------------------------------------------------------
-- SELECT COUNT(*)                                        AS n_fired,
--        COUNT(*) FILTER (WHERE px_max_fwd14/px_at_signal - 1 >= 0.10) AS n_hit,
--        ROUND(100.0 * COUNT(*) FILTER (
--            WHERE px_max_fwd14/px_at_signal - 1 < 0.10
--        ) / NULLIF(COUNT(*),0), 1)                      AS false_positive_pct
-- FROM evaluated;


-- ---------------------------------------------------------------------
-- INTERPRETATION NOTES (read before drawing conclusions)
--
--  * Q1–Q3 are DESCRIPTIVE. They characterise one episode. Even a
--    perfect lead/lag result there is a hypothesis, not a finding.
--  * Q4 is the test. Its base rate is the thing that decides whether
--    the flow signal is real. Expect it to be unflattering — negative
--    and positive flow clusters are common, breakouts are rare.
--  * The 602-day history spans ONE bull-to-crash-to-recovery cycle.
--    Even a clean Q4 result is N=1 on regimes.
--  * ETFs launched Jan 2024, so there is no 2022-bear coverage here
--    at all — unlike funding, which backfills to 2019.
--  * Threshold choices (7d window, +10%, 14 days) are free parameters.
--    Sweep them; demand a CONTIGUOUS winning region, not one lucky cell.
-- ---------------------------------------------------------------------
