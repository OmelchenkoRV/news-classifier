-- =====================================================================
-- Q7: Liquidation-cascade inputs — OI elasticity and orderbook depth
--
-- PURPOSE (different in kind from Q1-Q6)
--   Every prior test asked "does X PREDICT a crash?" — seven candidates,
--   all failed. This asks a STRUCTURAL question instead:
--
--     "Given current positioning, how severe would a cascade be IF a
--      move started?"
--
--   That is a position-sizing input, not a timing signal. It does not
--   require prediction to be useful — which is the whole point after
--   docs/FINDINGS_funding_defensive.md closed the leverage family.
--
-- WHAT WE CANNOT DO
--   Proper cascade modelling needs the LIQUIDATION-PRICE DISTRIBUTION:
--   where each position gets force-closed, which depends on entry price
--   and leverage per position. No free source provides this. Binance's
--   forceOrders endpoint retains ~7 days; Coinglass liquidation history
--   is paid. So "sum the positions liquidating between −5% and −10%" is
--   NOT available.
--
-- WHAT WE CAN DO INSTEAD — measure the same thing empirically
--   Q7a: ELASTICITY. How much OI is destroyed per 1% price move?
--        Measured from eth_derivatives across the episodes we captured.
--   Q7b: DEPTH. How much USD sits in the book within X% of mid?
--        From eth_orderbook_depth (cumulative_usd by pct_from_mid).
--
--   Cascade multiplier ≈ (forced sell volume) / (absorbing depth).
--   >1 means selling exceeds the book and the move self-amplifies.
--
-- HARD LIMITS — read before trusting any number out of this
--   * eth-capture starts ~2026-05. ~3.5 months, THREE episodes
--     (June crash, Aug squeeze, Aug-22 flush). Elasticity is a RANGE,
--     not a constant.
--   * DEPTH IS NOT STATIC. Resting bids are pulled during a crash. A
--     snapshot OVERSTATES absorption, so the model UNDERSTATES cascade
--     severity. Err on that side deliberately; never present the output
--     as a floor on how bad things get.
--   * OI falling does not distinguish FORCED liquidation from VOLUNTARY
--     closing. Elasticity conflates the two and is an upper bound on
--     the forced component.
--   * Binance only: okx has NULL oi in this capture, bybit has oi but
--     no long_short_ratio.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q7-pre. What episodes do we actually have?
-- ---------------------------------------------------------------------
SELECT d.exchange,
       MIN(s.captured_at)::date AS first_day,
       MAX(s.captured_at)::date AS last_day,
       COUNT(*)                          AS n_snapshots,
       COUNT(d.open_interest_usd)        AS n_with_oi,
       COUNT(d.long_short_ratio)         AS n_with_ls
FROM eth_snapshots s
JOIN eth_derivatives d ON d.snapshot_id = s.id
GROUP BY d.exchange ORDER BY d.exchange;


-- ---------------------------------------------------------------------
-- Q7a. ELASTICITY — ΔOI% per Δprice%, hourly, binance.
--   Sign convention: we care about DOWN moves destroying OI.
--   elasticity = pct_oi_change / pct_price_change
--   For a down move: both negative → positive ratio.
--   Ratio > 1 → OI falls FASTER than price (deleveraging amplifies).
-- ---------------------------------------------------------------------
WITH hourly AS (
    SELECT date_trunc('hour', s.captured_at) AS h,
           AVG(s.spot_price)                 AS px,
           AVG(d.open_interest_usd)          AS oi
    FROM eth_snapshots s
    JOIN eth_derivatives d ON d.snapshot_id = s.id
    WHERE d.exchange = 'binance'
      AND d.open_interest_usd IS NOT NULL
    GROUP BY 1
),
deltas AS (
    SELECT h, px, oi,
           100.0 * (px / NULLIF(LAG(px) OVER (ORDER BY h),0) - 1) AS px_chg_pct,
           100.0 * (oi / NULLIF(LAG(oi) OVER (ORDER BY h),0) - 1) AS oi_chg_pct
    FROM hourly
)
SELECT
    CASE WHEN px_chg_pct <= -2.0 THEN 'sharp down (<= -2%)'
         WHEN px_chg_pct <= -1.0 THEN 'down 1-2%'
         WHEN px_chg_pct <  -0.25 THEN 'down 0.25-1%'
         WHEN px_chg_pct <   0.25 THEN 'flat'
         WHEN px_chg_pct <   1.0 THEN 'up 0.25-1%'
         WHEN px_chg_pct <   2.0 THEN 'up 1-2%'
         ELSE 'sharp up (>= 2%)' END                    AS move_bucket,
    COUNT(*)                                            AS n_hours,
    ROUND(AVG(px_chg_pct)::numeric, 3)                  AS avg_px_chg_pct,
    ROUND(AVG(oi_chg_pct)::numeric, 3)                  AS avg_oi_chg_pct,
    ROUND((AVG(oi_chg_pct)/NULLIF(AVG(px_chg_pct),0))::numeric, 2) AS elasticity,
    ROUND(MIN(oi_chg_pct)::numeric, 2)                  AS worst_oi_drop_pct
FROM deltas
WHERE px_chg_pct IS NOT NULL AND oi_chg_pct IS NOT NULL
GROUP BY 1
ORDER BY MIN(px_chg_pct);


-- ---------------------------------------------------------------------
-- Q7a2. The extreme hours themselves — the cascade candidates.
--   These are the observations the model should actually be calibrated
--   on; bucket averages wash out the tails that matter.
-- ---------------------------------------------------------------------
WITH hourly AS (
    SELECT date_trunc('hour', s.captured_at) AS h,
           AVG(s.spot_price) AS px, AVG(d.open_interest_usd) AS oi
    FROM eth_snapshots s
    JOIN eth_derivatives d ON d.snapshot_id = s.id
    WHERE d.exchange='binance' AND d.open_interest_usd IS NOT NULL
    GROUP BY 1
),
deltas AS (
    SELECT h, px, oi,
           100.0*(px/NULLIF(LAG(px) OVER (ORDER BY h),0)-1) AS px_chg,
           100.0*(oi/NULLIF(LAG(oi) OVER (ORDER BY h),0)-1) AS oi_chg
    FROM hourly
)
SELECT h, ROUND(px::numeric,1) AS eth_px,
       ROUND((oi/1e9)::numeric,3)   AS oi_busd,
       ROUND(px_chg::numeric,2)     AS px_chg_pct,
       ROUND(oi_chg::numeric,2)     AS oi_chg_pct,
       ROUND((oi_chg/NULLIF(px_chg,0))::numeric,2) AS elasticity,
       ROUND((ABS(oi_chg)/100.0 * oi / 1e6)::numeric,1) AS oi_destroyed_musd
FROM deltas
WHERE ABS(px_chg) >= 1.5
ORDER BY px_chg
LIMIT 40;


-- ---------------------------------------------------------------------
-- Q7b. DEPTH — how much USD absorbs selling within X% of mid?
--   This is the denominator of the cascade multiplier.
--   NOTE: bid side is what absorbs forced SELLING.
-- ---------------------------------------------------------------------
SELECT o.exchange,
       o.side,
       o.pct_from_mid,
       COUNT(*)                                   AS n_obs,
       ROUND((AVG(o.cumulative_usd)/1e6)::numeric,2)    AS avg_depth_musd,
       ROUND((MIN(o.cumulative_usd)/1e6)::numeric,2)    AS min_depth_musd,
       ROUND((MAX(o.cumulative_usd)/1e6)::numeric,2)    AS max_depth_musd
FROM eth_orderbook_depth o
GROUP BY o.exchange, o.side, o.pct_from_mid
ORDER BY o.exchange, o.side, o.pct_from_mid;


-- ---------------------------------------------------------------------
-- Q7c. DEPTH DURING STRESS vs CALM — the assumption-killer.
--   If bid depth COLLAPSES during down moves, the static-depth cascade
--   model is optimistic and must be discounted. Compare the June crash
--   window against a calm stretch.
-- ---------------------------------------------------------------------
SELECT CASE WHEN s.captured_at BETWEEN '2026-06-04' AND '2026-06-06'
                 THEN 'june_crash'
            WHEN s.captured_at BETWEEN '2026-08-19' AND '2026-08-22'
                 THEN 'aug_rally'
            ELSE 'other' END                          AS regime,
       o.side,
       o.pct_from_mid,
       COUNT(*)                                       AS n_obs,
       ROUND((AVG(o.cumulative_usd)/1e6)::numeric,2)  AS avg_depth_musd
FROM eth_orderbook_depth o
JOIN eth_snapshots s ON s.id = o.snapshot_id
WHERE o.exchange = 'binance'
GROUP BY 1,2,3
ORDER BY 2,3,1;


-- ---------------------------------------------------------------------
-- HOW TO USE THE OUTPUT
--
--  * Q7a gives elasticity by move size. Expect it to RISE with move
--    magnitude — that non-linearity IS the cascade effect. If it is
--    flat (~1 everywhere), deleveraging is proportional and there is no
--    amplification to model.
--
--  * Q7a2 is the calibration set. Three episodes is thin; treat the
--    result as a RANGE and carry the worst case, not the mean.
--
--  * Cascade multiplier for a hypothetical X% drop:
--        forced_sell_usd ≈ OI_now × elasticity(X) × X / 100
--        multiplier      ≈ forced_sell_usd / bid_depth_within_X%
--    >1 → selling exceeds the book → the move self-amplifies.
--
--  * Q7c decides how much to trust that. If crash-window bid depth is
--    materially thinner than calm-window depth, the static denominator
--    is wrong and the true multiplier is HIGHER than computed.
--
--  * WHAT THIS IS FOR: sizing, not timing. Output is "a 10% move today
--    would force ~$X of selling against ~$Y of depth" — an input to how
--    large the DIRECTIONAL book should be, not a trigger to exit.
--    Do not let it become a timing signal by the back door; that is the
--    same mistake seven candidates already made.
-- ---------------------------------------------------------------------
