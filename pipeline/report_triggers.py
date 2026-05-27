"""
Trigger-Aware Report
======================

Analyzes pipeline behavior with a focus on the event novelty system.
Shows how many gate-tightening alerts were NOVEL vs suppressed as follow-ups.

Usage:
    python -m pipeline.report_triggers --days 7
"""

import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor


def print_section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def report(days: int = 7):
    conn = get_connection()
    cur = get_cursor(conn)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    print_section(f"TRIGGER-AWARE PIPELINE REPORT — Last {days} days")

    # ── Volume & novelty ──────────────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE is_causal) as causal,
               COUNT(*) FILTER (WHERE should_tighten_gates) as tighten,
               COUNT(*) FILTER (WHERE is_novel_event) as novel,
               COUNT(*) FILTER (WHERE trigger_id IS NOT NULL
                               AND NOT is_novel_event
                               AND NOT should_tighten_gates) as suppressed
        FROM classifications
        WHERE classified_at >= %s
    """, (since,))
    r = cur.fetchone()

    if not r or r["total"] == 0:
        print("No classifications in this period.")
        return

    print_section("VOLUME & NOVELTY")
    print(f"  Total classified:        {r['total']:>8,}")
    print(f"  Causal headlines:        {r['causal']:>8,}  ({r['causal']/r['total']*100:.1f}%)")
    print(f"  Novel events created:    {r['novel']:>8,}  ({r['novel']/r['total']*100:.2f}%)")
    print(f"  Gate-tighten alerts:     {r['tighten']:>8,}  ({r['tighten']/r['total']*100:.2f}%)")
    print(f"  Suppressed (follow-ups): {r['suppressed']:>8,}  ({r['suppressed']/r['total']*100:.2f}%)")

    if r['suppressed'] + r['tighten'] > 0:
        reduction = r['suppressed'] / (r['suppressed'] + r['tighten']) * 100
        print(f"\n  Noise reduction from triggers: {reduction:.1f}%")
        print(f"  (alerts without triggers would be {r['suppressed'] + r['tighten']:,},")
        print(f"   triggers reduced to {r['tighten']:,})")

    # ── Most mentioned triggers ──────────────────────────────────
    print_section("TOP TRIGGERS (most covered narratives)")
    cur.execute("""
        SELECT signature, display_name, category, state, impact_score,
               mention_count, first_seen_at, last_seen_at
        FROM triggers
        WHERE last_seen_at >= %s
        ORDER BY mention_count DESC
        LIMIT 20
    """, (since,))
    print(f"  {'Signature':30s} {'State':8s} {'Score':5s} {'Mentions':>8s}  First seen")
    print(f"  {'-'*30} {'-'*8} {'-'*5} {'-'*8}  {'-'*20}")
    for row in cur.fetchall():
        sig = row['signature'][:29]
        print(f"  {sig:30s} {row['state']:8s} {row['impact_score']:.2f}  {row['mention_count']:>8,}  {row['first_seen_at'].strftime('%Y-%m-%d %H:%M')}")

    # ── State distribution ───────────────────────────────────────
    print_section("TRIGGER STATE DISTRIBUTION")
    cur.execute("""
        SELECT state, COUNT(*) as cnt,
               AVG(mention_count) as avg_mentions,
               AVG(impact_score) as avg_score
        FROM triggers
        WHERE last_seen_at >= %s
        GROUP BY state
        ORDER BY state
    """, (since,))
    print(f"  {'State':10s} {'Count':>6s}  {'Avg Mentions':>13s}  {'Avg Score':>10s}")
    for row in cur.fetchall():
        print(f"  {row['state']:10s} {row['cnt']:>6,}  {row['avg_mentions']:>13.1f}  {row['avg_score']:>10.2f}")

    # ── Category breakdown ───────────────────────────────────────
    print_section("NOVEL EVENTS BY CATEGORY")
    cur.execute("""
        SELECT category, COUNT(*) as total_novel,
               COUNT(DISTINCT trigger_id) as unique_triggers
        FROM classifications
        WHERE classified_at >= %s
          AND is_novel_event = TRUE
        GROUP BY category
        ORDER BY total_novel DESC
    """, (since,))
    for row in cur.fetchall():
        print(f"  {row['category']:20s}: {row['total_novel']:>4,} novel  →  {row['unique_triggers']:>4,} triggers")

    # ── Recent novel events ──────────────────────────────────────
    print_section("RECENT 20 NOVEL EVENTS (the alerts that matter)")
    cur.execute("""
        SELECT c.classified_at, h.title, h.source_name,
               t.signature, c.category
        FROM classifications c
        JOIN headlines h ON h.id = c.headline_id
        LEFT JOIN triggers t ON t.id = c.trigger_id
        WHERE c.is_novel_event = TRUE
          AND c.classified_at >= %s
        ORDER BY c.classified_at DESC
        LIMIT 20
    """, (since,))
    for row in cur.fetchall():
        sig = (row['signature'] or '-')[:25]
        print(f"  [{row['classified_at'].strftime('%m-%d %H:%M')}] {row['category']:14s} {sig:26s} {row['title'][:70]}")

    # ── Suppression examples (proving the system works) ──────────
    print_section("EXAMPLES OF SUPPRESSED FOLLOW-UPS (noise we avoided)")
    cur.execute("""
        SELECT c.classified_at, h.title, t.signature, t.state, t.mention_count
        FROM classifications c
        JOIN headlines h ON h.id = c.headline_id
        JOIN triggers t ON t.id = c.trigger_id
        WHERE c.classified_at >= %s
          AND c.is_novel_event = FALSE
          AND c.should_tighten_gates = FALSE
          AND t.mention_count > 3
        ORDER BY t.mention_count DESC, c.classified_at DESC
        LIMIT 15
    """, (since,))
    for row in cur.fetchall():
        sig = row['signature'][:25]
        print(f"  [{row['state']:8s} x{row['mention_count']:>3}] {sig:26s} {row['title'][:80]}")

    conn.close()
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trigger-aware pipeline report")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    report(days=args.days)
