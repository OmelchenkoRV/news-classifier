"""
Tone-Drift BDBA Surveillance
==============================

Companion to simulate.py — adds tone-trajectory analysis on top of
frequency monitoring. The combined signal is what we want for BDBA:

  WARM_UP detected when:
    - Volume above baseline (existing frequency check) AND
    - Tone drifting negative over rolling window AND
    - Cascade vocabulary frequency rising

This script computes daily tone aggregates and stores them in a new
table, then re-evaluates BDBA alerts using the combined criteria.

Usage:
    # Aggregate daily tone (after tone_score has run)
    python -m research.covid_exercise.tone_drift --topic covid

    # Compare topics
    python -m research.covid_exercise.tone_drift --compare
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


# Cascade vocabulary — words that signal escalating/spreading impact
# These are domain-agnostic so they work for pandemic, supply chain,
# infrastructure, etc.
CASCADE_VOCABULARY = {
    # Tier 1 — strong cascade signals (each occurrence matters)
    "tier1": {
        "unprecedented": 3.0, "no precedent": 3.0, "first time": 2.0,
        "no alternative": 3.0, "single source": 3.0, "irreplaceable": 3.0,
        "global": 1.5, "worldwide": 1.5, "international": 1.0,
        "supply chain": 2.5, "manufacturing": 2.0, "factory": 1.5,
        "longer than expected": 2.0, "worse than feared": 2.5,
        "system-wide": 2.5, "cascading": 3.0, "domino": 3.0,
        "longer recovery": 2.0, "no quick fix": 2.5,
    },
    # Tier 2 — moderate cascade signals (frequency matters)
    "tier2": {
        "spreading": 1.5, "expanding": 1.0, "broader": 1.0,
        "industry-wide": 2.0, "across": 0.5, "throughout": 0.5,
        "ripple effect": 2.0, "knock-on": 1.5,
        "sustained": 1.0, "prolonged": 1.5, "extended": 0.8,
    },
    # Tier 3 — weak cascade signals (background frequency)
    "tier3": {
        "concern": 0.5, "warning": 0.7, "risk": 0.5, "threat": 0.7,
        "pressure": 0.5, "strain": 0.7, "stress": 0.5,
        "uncertainty": 0.5, "uncertain": 0.5, "questions": 0.3,
        "fears": 0.7, "alarm": 1.0, "worry": 0.5,
    },
}

# Flatten for matching
ALL_CASCADE_WORDS = {}
for tier, words in CASCADE_VOCABULARY.items():
    for word, weight in words.items():
        ALL_CASCADE_WORDS[word] = (tier, weight)


def add_tone_aggregate_table():
    """Create the daily tone aggregates table."""
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bdba_daily_tone (
            id              SERIAL PRIMARY KEY,
            research_topic  TEXT NOT NULL,
            tone_date       DATE NOT NULL,
            n_headlines     INTEGER NOT NULL,
            mean_tone       DOUBLE PRECISION,
            std_tone        DOUBLE PRECISION,
            pct_negative    DOUBLE PRECISION,
            pct_positive    DOUBLE PRECISION,
            cascade_score   DOUBLE PRECISION,
            cascade_word_count INTEGER,
            UNIQUE(research_topic, tone_date)
        );
        CREATE INDEX IF NOT EXISTS idx_bdba_tone_topic_date
            ON bdba_daily_tone(research_topic, tone_date);
    """)
    conn.commit()
    conn.close()


def compute_cascade_score(titles: list[str]) -> tuple[float, int]:
    """
    Compute cascade vocabulary score for a list of titles.
    
    Returns:
      - cascade_score: weighted sum (higher = more escalation language)
      - cascade_word_count: total cascade words found
    """
    total_weight = 0.0
    word_count = 0
    
    for title in titles:
        text = title.lower()
        for word, (tier, weight) in ALL_CASCADE_WORDS.items():
            if word in text:
                total_weight += weight
                word_count += 1
    
    return total_weight, word_count


def aggregate_topic(topic: str):
    """Aggregate daily tone + cascade for a topic."""
    add_tone_aggregate_table()
    
    conn = get_connection()
    cur = get_cursor(conn)
    
    # Pull all scored headlines for this topic
    cur.execute("""
        SELECT 
            published_at::date AS day,
            tone_score,
            tone_label,
            title
        FROM historical_headlines
        WHERE research_topic = %s
          AND tone_score IS NOT NULL
        ORDER BY day
    """, (topic,))
    
    rows = cur.fetchall()
    if not rows:
        log.warning(f"No tone-scored data for topic={topic}")
        conn.close()
        return
    
    log.info(f"Aggregating {len(rows):,} scored headlines for {topic}")
    
    # Group by day in Python
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    
    upsert_cur = conn.cursor()
    inserted = 0
    
    for day, day_rows in sorted(by_day.items()):
        scores = [r["tone_score"] for r in day_rows]
        titles = [r["title"] for r in day_rows]
        
        n = len(scores)
        mean_tone = sum(scores) / n
        if n > 1:
            mean_dev_sq = sum((s - mean_tone) ** 2 for s in scores) / (n - 1)
            std_tone = mean_dev_sq ** 0.5
        else:
            std_tone = 0.0
        
        n_neg = sum(1 for r in day_rows if r["tone_label"] == "negative")
        n_pos = sum(1 for r in day_rows if r["tone_label"] == "positive")
        
        cascade_score, cascade_count = compute_cascade_score(titles)
        
        upsert_cur.execute("""
            INSERT INTO bdba_daily_tone
                (research_topic, tone_date, n_headlines,
                 mean_tone, std_tone,
                 pct_negative, pct_positive,
                 cascade_score, cascade_word_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (research_topic, tone_date) DO UPDATE SET
                n_headlines = EXCLUDED.n_headlines,
                mean_tone = EXCLUDED.mean_tone,
                std_tone = EXCLUDED.std_tone,
                pct_negative = EXCLUDED.pct_negative,
                pct_positive = EXCLUDED.pct_positive,
                cascade_score = EXCLUDED.cascade_score,
                cascade_word_count = EXCLUDED.cascade_word_count
        """, (
            topic, day, n,
            mean_tone, std_tone,
            n_neg / n, n_pos / n,
            cascade_score, cascade_count,
        ))
        inserted += 1
    
    conn.commit()
    conn.close()
    log.info(f"Aggregated {inserted} days for {topic}")


def detect_drift(topic: str, window_days: int = 14) -> list[dict]:
    """
    For each day, compute drift metrics:
      - 14d rolling mean of tone (recent window)
      - 28d rolling mean of tone (prior window)
      - drift = recent - prior (negative = getting worse)
      - cascade ratio = recent cascade frequency vs prior
    
    Returns list of daily records with drift indicators.
    """
    conn = get_connection()
    cur = get_cursor(conn)
    
    cur.execute("""
        SELECT tone_date, n_headlines, mean_tone, std_tone,
               pct_negative, pct_positive,
               cascade_score, cascade_word_count
        FROM bdba_daily_tone
        WHERE research_topic = %s
        ORDER BY tone_date
    """, (topic,))
    rows = cur.fetchall()
    conn.close()
    
    if len(rows) < window_days * 2:
        log.warning(f"Need at least {window_days*2} days of data, have {len(rows)}")
        return []
    
    results = []
    for i in range(len(rows)):
        record = dict(rows[i])
        record["drift"] = None
        record["recent_mean_tone"] = None
        record["prior_mean_tone"] = None
        record["cascade_ratio"] = None
        
        if i >= window_days * 2 - 1:
            recent = rows[i - window_days + 1 : i + 1]
            prior = rows[i - 2*window_days + 1 : i - window_days + 1]
            
            # Weighted mean by headline count (avoids days with few headlines dominating)
            def weighted_mean(rs):
                total_w = sum(r["n_headlines"] for r in rs)
                if total_w == 0:
                    return 0.0
                return sum(r["mean_tone"] * r["n_headlines"] for r in rs) / total_w
            
            recent_tone = weighted_mean(recent)
            prior_tone = weighted_mean(prior)
            
            record["recent_mean_tone"] = recent_tone
            record["prior_mean_tone"] = prior_tone
            record["drift"] = recent_tone - prior_tone
            
            # Cascade ratio
            recent_cascade = sum(r["cascade_score"] for r in recent) / window_days
            prior_cascade = sum(r["cascade_score"] for r in prior) / window_days
            if prior_cascade > 0:
                record["cascade_ratio"] = recent_cascade / prior_cascade
            elif recent_cascade > 0:
                record["cascade_ratio"] = 99.0  # large finite number
            else:
                record["cascade_ratio"] = 1.0
        
        results.append(record)
    
    return results


def show_drift_summary(topic: str):
    """Print tone drift summary."""
    print(f"\n{'='*70}")
    print(f"  TONE DRIFT SUMMARY — {topic}")
    print(f"{'='*70}")
    
    drifts = detect_drift(topic, window_days=14)
    if not drifts:
        return
    
    # Find worst drift days
    valid_drifts = [d for d in drifts if d["drift"] is not None]
    if not valid_drifts:
        print("  Insufficient data")
        return
    
    print(f"\n  Days with computed drift: {len(valid_drifts)}")
    
    # Overall stats
    drift_values = [d["drift"] for d in valid_drifts]
    print(f"  Drift range: {min(drift_values):.3f} to {max(drift_values):.3f}")
    print(f"  Mean drift: {sum(drift_values)/len(drift_values):.3f}")
    
    # Worst (most negative) drift days
    print(f"\n  Top 10 days with negative drift (tone deteriorating):")
    print(f"  {'Date':<12s} {'Recent':>8s} {'Prior':>8s} {'Drift':>8s} "
          f"{'Cascade':>9s}")
    sorted_by_drift = sorted(valid_drifts, key=lambda d: d["drift"])[:10]
    for d in sorted_by_drift:
        cascade_str = f"{d['cascade_ratio']:.2f}x" if d['cascade_ratio'] else "-"
        print(f"  {d['tone_date']!s:<12} "
              f"{d['recent_mean_tone']:>8.3f} "
              f"{d['prior_mean_tone']:>8.3f} "
              f"{d['drift']:>+8.3f} "
              f"{cascade_str:>9}")
    
    # Top 10 days with highest cascade ratio (cascade vocab spiking)
    cascade_valid = [d for d in valid_drifts if d.get("cascade_ratio") is not None]
    if cascade_valid:
        print(f"\n  Top 10 days with cascade vocabulary spike:")
        print(f"  {'Date':<12s} {'Cascade':>9s} {'Tone':>8s} {'Drift':>8s}")
        sorted_by_cascade = sorted(cascade_valid,
                                   key=lambda d: -d["cascade_ratio"])[:10]
        for d in sorted_by_cascade:
            print(f"  {d['tone_date']!s:<12} "
                  f"{d['cascade_ratio']:>8.2f}x "
                  f"{d['recent_mean_tone']:>8.3f} "
                  f"{d['drift']:>+8.3f}")
    
    # NEW alert criteria — cascade ratio is the primary signal.
    # Tone drift is the secondary confirmation. Either alone can trigger watch;
    # both together trigger warm.
    cascade_alerts = [
        d for d in valid_drifts
        if d.get("cascade_ratio") is not None and d["cascade_ratio"] >= 2.0
    ]
    drift_alerts = [
        d for d in valid_drifts
        if d.get("drift") is not None and d["drift"] <= -0.15
    ]
    combined_alerts = [
        d for d in valid_drifts
        if d.get("cascade_ratio") is not None and d["cascade_ratio"] >= 2.0
        and d.get("drift") is not None and d["drift"] <= -0.10
    ]
    
    print(f"\n  Alert summary using new criteria:")
    print(f"    Cascade-only (cascade >= 2.0x):              {len(cascade_alerts)} days")
    print(f"    Drift-only (drift <= -0.15):                 {len(drift_alerts)} days")
    print(f"    Combined warm-up (cascade>=2.0 + drift<=-0.10): {len(combined_alerts)} days")
    
    if cascade_alerts:
        first_cascade = min(cascade_alerts, key=lambda d: d["tone_date"])
        print(f"\n  First cascade alert: {first_cascade['tone_date']} "
              f"(cascade={first_cascade['cascade_ratio']:.2f}x, "
              f"drift={first_cascade['drift']:+.3f})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default=None)
    parser.add_argument("--compare", action="store_true",
                        help="Show drift summary for all topics")
    args = parser.parse_args()
    
    if args.compare:
        conn = get_connection()
        cur = get_cursor(conn)
        cur.execute("""
            SELECT DISTINCT research_topic FROM bdba_daily_tone
            ORDER BY research_topic
        """)
        topics = [r["research_topic"] for r in cur.fetchall()]
        conn.close()
        
        for t in topics:
            show_drift_summary(t)
    elif args.topic:
        aggregate_topic(args.topic)
        show_drift_summary(args.topic)
    else:
        # Aggregate all topics
        conn = get_connection()
        cur = get_cursor(conn)
        cur.execute("""
            SELECT DISTINCT research_topic FROM historical_headlines
            WHERE tone_score IS NOT NULL
            ORDER BY research_topic
        """)
        topics = [r["research_topic"] for r in cur.fetchall()]
        conn.close()
        
        for t in topics:
            aggregate_topic(t)
            show_drift_summary(t)


if __name__ == "__main__":
    main()
