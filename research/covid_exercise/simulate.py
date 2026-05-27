"""
Retrospective BDBA Surveillance
=================================

Walks day-by-day through the historical news + market data and simulates
what the BDBA system would have alerted on, had it been running.

For each day in the window:
  1. Count headlines matching the BDBA topic in last 24h
  2. Compute baseline (mean + std) over previous 30 days
  3. Compute z-score and source diversification metrics
  4. Decide: would alert fire? At what severity?
  5. Look up forward market returns (5d, 30d) from this day
  6. Persist to bdba_retrospective

This produces a timeline you can visualize and compare to actual events.

Usage:
    python -m research.covid_exercise.simulate \\
        --topic covid \\
        --bdba-topic pandemic_signal \\
        --start 2019-12-01 --end 2020-04-30
"""

import os
import sys
import argparse
import logging
from datetime import datetime, date, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def fetch_topic_config(cur, topic_name: str) -> dict:
    cur.execute("""
        SELECT name, baseline_window_days, alert_z_threshold, min_baseline_count
        FROM bdba_topics WHERE name = %s
    """, (topic_name,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"BDBA topic not found: {topic_name}")
    return dict(row)


def count_headlines_24h(cur, research_topic: str, snapshot_date: date) -> dict:
    """
    Count DISTINCT STORIES (not raw headlines) for a single day.
    
    Wire syndication can publish the same AP story to 50+ local outlets.
    We count by story_hash — same hash = same story regardless of how many
    outlets reprinted it.
    
    Source diversification (tier1_count, unique_domains) is still computed
    on the raw headline rows because picking up a story IS editorially
    meaningful for outlet diversity.
    """
    start = datetime.combine(snapshot_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    
    cur.execute("""
        SELECT
            -- Distinct stories, not raw headlines
            COUNT(DISTINCT story_hash) AS total,
            -- But for source diversification, count distinct domains
            COUNT(DISTINCT source_domain) AS unique_domains,
            -- For tier metrics, count distinct stories that hit each tier
            COUNT(DISTINCT story_hash) FILTER (WHERE source_tier = 'tier1') AS tier1_count,
            COUNT(DISTINCT story_hash) FILTER (WHERE source_tier = 'tier2') AS tier2_count,
            -- For diagnostics: how many raw headlines did we get
            COUNT(*) AS raw_headline_count
        FROM historical_headlines
        WHERE research_topic = %s
          AND published_at >= %s
          AND published_at < %s
          AND story_hash IS NOT NULL
    """, (research_topic, start, end))
    return dict(cur.fetchone())


def count_headlines_window(cur, research_topic: str,
                           start_date: date, end_date: date) -> int:
    """Total DISTINCT stories over a date range (used for 7d count)."""
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
    
    cur.execute("""
        SELECT COUNT(DISTINCT story_hash) AS total
        FROM historical_headlines
        WHERE research_topic = %s
          AND published_at >= %s
          AND published_at < %s
          AND story_hash IS NOT NULL
    """, (research_topic, start, end))
    return cur.fetchone()["total"]


def compute_baseline(cur, research_topic: str, snapshot_date: date,
                     window_days: int) -> dict:
    """
    Compute the baseline statistics over the previous `window_days` days,
    EXCLUDING the snapshot day itself (to avoid leak).
    
    Counts distinct stories per day (deduplicated for wire syndication).
    """
    end = datetime.combine(snapshot_date, datetime.min.time(), tzinfo=timezone.utc)
    start = end - timedelta(days=window_days)
    
    cur.execute("""
        WITH daily AS (
            SELECT date_trunc('day', published_at)::date AS day,
                   COUNT(DISTINCT story_hash) AS n,
                   COUNT(DISTINCT story_hash) FILTER (WHERE source_tier = 'tier1') AS tier1
            FROM historical_headlines
            WHERE research_topic = %s
              AND published_at >= %s
              AND published_at < %s
              AND story_hash IS NOT NULL
            GROUP BY 1
        ),
        full_range AS (
            -- include zero-count days
            SELECT generate_series(%s::date, %s::date, '1 day')::date AS day
        )
        SELECT
            AVG(COALESCE(daily.n, 0)) AS mean_count,
            STDDEV(COALESCE(daily.n, 0)) AS std_count,
            AVG(COALESCE(daily.tier1, 0)) AS mean_tier1
        FROM full_range
        LEFT JOIN daily ON full_range.day = daily.day
    """, (research_topic, start, end, start.date(), (end - timedelta(days=1)).date()))
    
    row = cur.fetchone()
    return {
        "mean": float(row["mean_count"] or 0),
        "std": float(row["std_count"] or 0),
        "mean_tier1": float(row["mean_tier1"] or 0),
    }


def fetch_market_levels(cur, snapshot_date: date) -> dict:
    """Get SPY, VIX, BTC closes for snapshot day and forward returns."""
    cur.execute("""
        SELECT ticker, trade_date, close_price
        FROM historical_market_daily
        WHERE ticker IN ('SPY', '^VIX', 'BTC-USD')
          AND trade_date BETWEEN %s AND %s
    """, (snapshot_date - timedelta(days=2),
          snapshot_date + timedelta(days=35)))
    rows = cur.fetchall()
    
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], {})[r["trade_date"]] = r["close_price"]
    
    def get_close(ticker, target_date):
        """Get the closest available trading day on or after target_date."""
        d = target_date
        for _ in range(7):  # try up to 7 days forward (covers weekends/holidays)
            if d in by_ticker.get(ticker, {}):
                return by_ticker[ticker][d]
            d += timedelta(days=1)
        return None
    
    spy_close = get_close("SPY", snapshot_date)
    spy_5d = get_close("SPY", snapshot_date + timedelta(days=5))
    spy_30d = get_close("SPY", snapshot_date + timedelta(days=30))
    
    btc_close = get_close("BTC-USD", snapshot_date)
    btc_5d = get_close("BTC-USD", snapshot_date + timedelta(days=5))
    
    vix_close = get_close("^VIX", snapshot_date)
    
    return {
        "spy_close": spy_close,
        "spy_return_5d": (spy_5d / spy_close - 1) if spy_close and spy_5d else None,
        "spy_return_30d": (spy_30d / spy_close - 1) if spy_close and spy_30d else None,
        "vix_close": vix_close,
        "btc_close": btc_close,
        "btc_return_5d": (btc_5d / btc_close - 1) if btc_close and btc_5d else None,
    }


def fetch_tone_signals(cur, research_topic: str, snapshot_date: date,
                        window_days: int = 14) -> dict:
    """
    Fetch the cascade_ratio and tone_drift signals for a snapshot day.
    
    Computes 14-day weighted recent vs 14-day weighted prior for both
    tone and cascade. Returns None for either signal if not enough data.
    """
    cur.execute("""
        SELECT tone_date, n_headlines, mean_tone, cascade_score
        FROM bdba_daily_tone
        WHERE research_topic = %s
          AND tone_date <= %s
          AND tone_date >= %s
        ORDER BY tone_date
    """, (research_topic, snapshot_date,
          snapshot_date - timedelta(days=window_days * 2)))
    rows = cur.fetchall()
    
    if len(rows) < window_days * 2:
        return {"cascade_ratio": None, "tone_drift": None}
    
    # Most recent window
    recent = [r for r in rows if r["tone_date"] > snapshot_date - timedelta(days=window_days)]
    prior = [r for r in rows
             if r["tone_date"] <= snapshot_date - timedelta(days=window_days)
             and r["tone_date"] > snapshot_date - timedelta(days=window_days * 2)]
    
    if not recent or not prior:
        return {"cascade_ratio": None, "tone_drift": None}
    
    # Weighted tone means
    def weighted_mean(rs, field):
        total_w = sum(r["n_headlines"] for r in rs)
        if total_w == 0:
            return 0.0
        return sum(r[field] * r["n_headlines"] for r in rs) / total_w
    
    recent_tone = weighted_mean(recent, "mean_tone")
    prior_tone = weighted_mean(prior, "mean_tone")
    tone_drift = recent_tone - prior_tone
    
    # Cascade ratio (recent avg / prior avg)
    recent_cascade_avg = sum(r["cascade_score"] for r in recent) / len(recent)
    prior_cascade_avg = sum(r["cascade_score"] for r in prior) / len(prior)
    
    if prior_cascade_avg > 0.01:
        cascade_ratio = recent_cascade_avg / prior_cascade_avg
    elif recent_cascade_avg > 0.01:
        cascade_ratio = 99.0
    else:
        cascade_ratio = 1.0
    
    return {
        "cascade_ratio": cascade_ratio,
        "tone_drift": tone_drift,
    }


def decide_alert(headlines_24h: int, baseline_mean: float, baseline_std: float,
                 tier1_count_24h: int, tier1_baseline: float,
                 z_threshold: float, min_baseline: float,
                 cascade_ratio: float = None,
                 tone_drift: float = None) -> dict:
    """
    Decide whether an alert would fire and what severity.
    
    PRIMARY SIGNAL: cascade vocabulary ratio (>=2.0x)
        Validated against COVID vs Ebola — COVID ratios were 1.9-5.3x, 
        Ebola were 0.05-0.07x. Strong separator.
    
    SECONDARY SIGNAL: frequency anomaly + tier1 diversification
        Original signals; useful but high false positive rate.
    
    TERTIARY SIGNAL: tone drift
        Confirmation only; weak on its own (FinBERT picks up surface
        tone, not market relevance).
    
    Returns {would_alert, severity, reasons, z_score}.
    """
    reasons = []
    
    # Z-score on raw count
    if baseline_std > 0.1:
        z = (headlines_24h - baseline_mean) / baseline_std
    elif baseline_mean > 0:
        z = headlines_24h / max(baseline_mean, 0.5)
    else:
        z = headlines_24h * 1.0
    
    # SIGNAL 1: cascade vocabulary ratio (PRIMARY)
    cascade_alert = False
    cascade_strong = False
    if cascade_ratio is not None:
        if cascade_ratio >= 2.0:
            cascade_alert = True
            reasons.append(f"cascade_ratio={cascade_ratio:.1f}x")
        if cascade_ratio >= 3.5:
            cascade_strong = True
    
    # SIGNAL 2: frequency anomaly
    frequency_alert = False
    if z >= z_threshold and headlines_24h >= 3:
        frequency_alert = True
        reasons.append("frequency")
    
    # SIGNAL 3: tier1 diversification
    tier1_alert = False
    if tier1_count_24h >= 3 and tier1_count_24h >= 2 * max(tier1_baseline, 0.5):
        tier1_alert = True
        reasons.append("tier1_diversification")
    
    # SIGNAL 4: tone drift (confirmation only)
    drift_confirmation = False
    if tone_drift is not None and tone_drift <= -0.10:
        drift_confirmation = True
        reasons.append(f"tone_drift={tone_drift:+.2f}")
    
    # Severity ladder — cascade ratio is now the primary driver
    severity = None
    would_alert = False
    
    if cascade_strong and (frequency_alert or tier1_alert):
        # Strong cascade + supporting signal = highest concern
        severity = "hot"
        would_alert = True
    elif cascade_strong:
        # Cascade vocabulary screaming, even alone
        severity = "warm"
        would_alert = True
    elif cascade_alert and (frequency_alert or tier1_alert or drift_confirmation):
        # Moderate cascade + any other signal
        severity = "warm"
        would_alert = True
    elif cascade_alert:
        # Just cascade alone (worth watching)
        severity = "watch"
        would_alert = True
    elif frequency_alert and tier1_alert:
        # Old criteria still triggers but not primary
        severity = "watch"
        would_alert = True
        reasons.append("legacy_freq_tier1")
    
    baseline_reliable = baseline_mean >= min_baseline
    
    return {
        "would_alert": would_alert,
        "severity": severity,
        "reasons": reasons,
        "z_score": z,
        "cascade_ratio": cascade_ratio,
        "tone_drift": tone_drift,
        "baseline_reliable": baseline_reliable,
    }


def simulate(research_topic: str, bdba_topic: str,
             start_date: date, end_date: date):
    conn = get_connection()
    cur = get_cursor(conn)
    
    config = fetch_topic_config(cur, bdba_topic)
    log.info(f"BDBA topic: {config['name']}")
    log.info(f"  Z threshold: {config['alert_z_threshold']}")
    log.info(f"  Baseline window: {config['baseline_window_days']} days")
    log.info(f"  Min baseline count: {config['min_baseline_count']}")
    log.info(f"Simulating {start_date} to {end_date} for {research_topic}")
    
    current = start_date
    alerts_fired = 0
    days_processed = 0
    
    while current <= end_date:
        days_processed += 1
        
        # Counts
        today = count_headlines_24h(cur, research_topic, current)
        seven_d = count_headlines_window(
            cur, research_topic,
            current - timedelta(days=7), current,
        )
        baseline = compute_baseline(
            cur, research_topic, current,
            config["baseline_window_days"],
        )
        
        # Tone + cascade signals (returns None,None if data not available)
        tone_signals = fetch_tone_signals(cur, research_topic, current)
        
        # Alert decision
        decision = decide_alert(
            headlines_24h=today["total"],
            baseline_mean=baseline["mean"],
            baseline_std=baseline["std"],
            tier1_count_24h=today["tier1_count"],
            tier1_baseline=baseline["mean_tier1"],
            z_threshold=config["alert_z_threshold"],
            min_baseline=config["min_baseline_count"],
            cascade_ratio=tone_signals["cascade_ratio"],
            tone_drift=tone_signals["tone_drift"],
        )
        
        # Markets
        market = fetch_market_levels(cur, current)
        
        # Persist
        cur.execute("""
            INSERT INTO bdba_retrospective
                (research_topic, bdba_topic, snapshot_date,
                 headlines_24h, headlines_7d, baseline_30d, baseline_std, z_score,
                 unique_domains_24h, tier1_count_24h, tier1_count_baseline,
                 would_alert, alert_severity, alert_reasons,
                 spy_close, spy_return_5d_fwd, spy_return_30d_fwd,
                 vix_close, btc_close, btc_return_5d_fwd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (research_topic, bdba_topic, snapshot_date) DO UPDATE SET
                headlines_24h = EXCLUDED.headlines_24h,
                headlines_7d = EXCLUDED.headlines_7d,
                baseline_30d = EXCLUDED.baseline_30d,
                baseline_std = EXCLUDED.baseline_std,
                z_score = EXCLUDED.z_score,
                unique_domains_24h = EXCLUDED.unique_domains_24h,
                tier1_count_24h = EXCLUDED.tier1_count_24h,
                tier1_count_baseline = EXCLUDED.tier1_count_baseline,
                would_alert = EXCLUDED.would_alert,
                alert_severity = EXCLUDED.alert_severity,
                alert_reasons = EXCLUDED.alert_reasons,
                spy_close = EXCLUDED.spy_close,
                spy_return_5d_fwd = EXCLUDED.spy_return_5d_fwd,
                spy_return_30d_fwd = EXCLUDED.spy_return_30d_fwd,
                vix_close = EXCLUDED.vix_close,
                btc_close = EXCLUDED.btc_close,
                btc_return_5d_fwd = EXCLUDED.btc_return_5d_fwd
        """, (
            research_topic, bdba_topic, current,
            today["total"], seven_d,
            baseline["mean"], baseline["std"], decision["z_score"],
            today["unique_domains"], today["tier1_count"], baseline["mean_tier1"],
            decision["would_alert"], decision["severity"], decision["reasons"],
            market["spy_close"], market["spy_return_5d"], market["spy_return_30d"],
            market["vix_close"], market["btc_close"], market["btc_return_5d"],
        ))
        
        if decision["would_alert"]:
            alerts_fired += 1
            cr = decision.get("cascade_ratio")
            cr_str = f"cascade={cr:.1f}x" if cr is not None else "cascade=n/a"
            td = decision.get("tone_drift")
            td_str = f"drift={td:+.2f}" if td is not None else "drift=n/a"
            log.info(
                f"  ALERT [{decision['severity']:5s}] {current} | "
                f"24h={today['total']:3d} stories ({today['raw_headline_count']} raw) "
                f"z={decision['z_score']:.1f} tier1={today['tier1_count']} "
                f"{cr_str} {td_str} | "
                f"reasons={','.join(decision['reasons'])}"
            )
        
        if days_processed % 30 == 0:
            log.info(f"  ... processed {days_processed} days")
            conn.commit()
        
        current += timedelta(days=1)
    
    conn.commit()
    conn.close()
    
    log.info(f"")
    log.info(f"Simulation complete:")
    log.info(f"  Days processed: {days_processed}")
    log.info(f"  Alerts fired:   {alerts_fired}")
    log.info(f"  Alert rate:     {alerts_fired/days_processed*100:.1f}% of days")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True,
                        help="research_topic (covid, ebola_2014, etc)")
    parser.add_argument("--bdba-topic", required=True,
                        help="bdba_topics.name (pandemic_signal)")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    
    simulate(
        args.topic, args.bdba_topic,
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
    )


if __name__ == "__main__":
    main()
