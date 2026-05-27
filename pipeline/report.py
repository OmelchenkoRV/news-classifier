"""
Weekly Report Analyzer
========================

Analyzes pipeline behavior over the test period. Run this after letting
the pipeline collect for a week to get a performance breakdown.

Usage:
    python -m pipeline.report              # last 7 days
    python -m pipeline.report --days 3
    python -m pipeline.report --export report.csv
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def print_section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def analyze_pipeline(days: int = 7, export_csv: str = None):
    """Generate comprehensive report on pipeline behavior."""
    conn = get_connection()
    cur = get_cursor(conn)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    print_section(f"NEWS CLASSIFIER PIPELINE REPORT — Last {days} days")
    print(f"Period: {since.date()} to {datetime.now(timezone.utc).date()}")

    # ── Overall volume ──────────────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE is_causal) as causal,
               COUNT(*) FILTER (WHERE should_tighten_gates) as tighten,
               AVG(processing_time_ms) as avg_ms,
               MIN(classified_at) as first_seen,
               MAX(classified_at) as last_seen
        FROM classifications
        WHERE classified_at >= %s
    """, (since,))
    row = cur.fetchone()

    if not row or row["total"] == 0:
        print("\n  No classifications found in this period.")
        conn.close()
        return

    print_section("OVERALL VOLUME")
    print(f"  Total classified:        {row['total']:>8,}")
    print(f"  Causal headlines:        {row['causal']:>8,}  ({row['causal']/row['total']*100:.1f}%)")
    print(f"  Gate-tightening alerts:  {row['tighten']:>8,}  ({row['tighten']/row['total']*100:.1f}%)")
    print(f"  Avg processing time:     {row['avg_ms']:>7.0f} ms")
    print(f"  First classification:    {row['first_seen']}")
    print(f"  Last classification:     {row['last_seen']}")

    # ── Hourly rate ─────────────────────────────────────────────
    hours = (row["last_seen"] - row["first_seen"]).total_seconds() / 3600
    if hours > 0:
        print(f"  Avg headlines/hour:      {row['total']/hours:>7.1f}")
        print(f"  Avg gate alerts/day:     {row['tighten']/days:>7.1f}")

    # ── Category distribution ───────────────────────────────────
    print_section("CATEGORY DISTRIBUTION")
    cur.execute("""
        SELECT category, 
               COUNT(*) as cnt,
               AVG(category_confidence) as avg_conf,
               COUNT(*) FILTER (WHERE should_tighten_gates) as tighten_cnt
        FROM classifications
        WHERE classified_at >= %s
        GROUP BY category
        ORDER BY cnt DESC
    """, (since,))
    print(f"  {'Category':25s}  {'Count':>8s}  {'% Total':>8s}  {'Avg Conf':>9s}  {'Tighten':>8s}")
    print(f"  {'-'*25}  {'-'*8}  {'-'*8}  {'-'*9}  {'-'*8}")
    for r in cur.fetchall():
        pct = r['cnt'] / row['total'] * 100
        print(f"  {r['category']:25s}  {r['cnt']:>8,}  {pct:>7.1f}%  {r['avg_conf']:>8.2f}   {r['tighten_cnt']:>8,}")

    # ── Gate tightening events (the critical alerts) ────────────
    print_section("GATE-TIGHTENING ALERTS (catalyst events)")
    cur.execute("""
        SELECT c.category, COUNT(*) as cnt
        FROM classifications c
        WHERE c.classified_at >= %s
          AND c.should_tighten_gates = TRUE
        GROUP BY c.category
        ORDER BY cnt DESC
    """, (since,))
    tighten_by_cat = cur.fetchall()
    for r in tighten_by_cat:
        print(f"  {r['category']:20s}: {r['cnt']:>5,} alerts")

    # Show recent gate-tightening examples
    cur.execute("""
        SELECT h.title, h.source_name, c.category, c.category_confidence,
               c.classified_at, h.published_at
        FROM classifications c
        JOIN headlines h ON h.id = c.headline_id
        WHERE c.classified_at >= %s
          AND c.should_tighten_gates = TRUE
        ORDER BY c.classified_at DESC
        LIMIT 20
    """, (since,))
    recent = cur.fetchall()
    if recent:
        print(f"\n  Most recent 20 gate-tightening headlines:")
        for r in recent:
            print(f"\n  [{r['published_at'].strftime('%Y-%m-%d %H:%M')}] "
                  f"{r['category']:15s} conf={r['category_confidence']:.2f} "
                  f"src={r['source_name']}")
            print(f"    {r['title'][:100]}")

    # ── Source analysis ─────────────────────────────────────────
    print_section("SOURCE PRODUCTIVITY")
    cur.execute("""
        SELECT h.source_name,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE c.is_causal) as causal,
               COUNT(*) FILTER (WHERE c.should_tighten_gates) as tighten
        FROM classifications c
        JOIN headlines h ON h.id = c.headline_id
        WHERE c.classified_at >= %s
        GROUP BY h.source_name
        ORDER BY tighten DESC, causal DESC
        LIMIT 20
    """, (since,))
    print(f"  {'Source':25s}  {'Total':>7s}  {'Causal':>7s}  {'Tighten':>8s}  {'Signal %':>9s}")
    print(f"  {'-'*25}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*9}")
    for r in cur.fetchall():
        signal_pct = (r['tighten'] / r['total'] * 100) if r['total'] > 0 else 0
        print(f"  {r['source_name']:25s}  {r['total']:>7,}  {r['causal']:>7,}  {r['tighten']:>8,}   {signal_pct:>7.1f}%")

    # ── Daily activity pattern ──────────────────────────────────
    print_section("DAILY ACTIVITY")
    cur.execute("""
        SELECT DATE(classified_at) as day,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE is_causal) as causal,
               COUNT(*) FILTER (WHERE should_tighten_gates) as tighten
        FROM classifications
        WHERE classified_at >= %s
        GROUP BY DATE(classified_at)
        ORDER BY day
    """, (since,))
    print(f"  {'Date':12s}  {'Total':>7s}  {'Causal':>7s}  {'Tighten':>8s}")
    print(f"  {'-'*12}  {'-'*7}  {'-'*7}  {'-'*8}")
    for r in cur.fetchall():
        print(f"  {str(r['day']):12s}  {r['total']:>7,}  {r['causal']:>7,}  {r['tighten']:>8,}")

    # ── Hourly pattern ──────────────────────────────────────────
    print_section("HOURLY PATTERN (gate-tightening alerts by hour UTC)")
    cur.execute("""
        SELECT EXTRACT(HOUR FROM classified_at)::int as hour,
               COUNT(*) FILTER (WHERE should_tighten_gates) as tighten_cnt
        FROM classifications
        WHERE classified_at >= %s
        GROUP BY EXTRACT(HOUR FROM classified_at)
        ORDER BY hour
    """, (since,))
    max_cnt = 1
    hours_data = cur.fetchall()
    if hours_data:
        max_cnt = max(max(r['tighten_cnt'] for r in hours_data), 1)
    for r in hours_data:
        bar = "█" * int(r['tighten_cnt'] * 40 / max_cnt)
        print(f"  {r['hour']:02d}:00  {r['tighten_cnt']:>4d}  {bar}")

    # ── Confidence distribution ─────────────────────────────────
    print_section("CONFIDENCE DISTRIBUTION")
    cur.execute("""
        SELECT 
            CASE
                WHEN category_confidence >= 0.9 THEN '0.9-1.0'
                WHEN category_confidence >= 0.8 THEN '0.8-0.9'
                WHEN category_confidence >= 0.7 THEN '0.7-0.8'
                WHEN category_confidence >= 0.6 THEN '0.6-0.7'
                WHEN category_confidence >= 0.5 THEN '0.5-0.6'
                ELSE '<0.5'
            END as conf_bucket,
            COUNT(*) as cnt
        FROM classifications
        WHERE classified_at >= %s
        GROUP BY conf_bucket
        ORDER BY conf_bucket DESC
    """, (since,))
    for r in cur.fetchall():
        pct = r['cnt'] / row['total'] * 100
        bar = "█" * int(pct)
        print(f"  {r['conf_bucket']:10s}  {r['cnt']:>7,}  ({pct:>5.1f}%)  {bar}")

    # ── Export CSV if requested ─────────────────────────────────
    if export_csv:
        print_section(f"EXPORTING TO {export_csv}")
        import csv
        cur.execute("""
            SELECT h.published_at, h.source_name, h.title,
                   c.category, c.category_confidence,
                   c.impact_level, c.impact_confidence,
                   c.is_causal, c.should_tighten_gates, c.news_impact_score
            FROM classifications c
            JOIN headlines h ON h.id = c.headline_id
            WHERE c.classified_at >= %s
            ORDER BY h.published_at DESC
        """, (since,))
        rows = cur.fetchall()
        with open(export_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        print(f"  Exported {len(rows):,} rows to {export_csv}")

    conn.close()
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze pipeline behavior")
    parser.add_argument("--days", type=int, default=7, help="Look back N days")
    parser.add_argument("--export", type=str, help="Export to CSV file")
    args = parser.parse_args()

    analyze_pipeline(days=args.days, export_csv=args.export)
